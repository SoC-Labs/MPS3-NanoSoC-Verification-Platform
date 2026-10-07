"""``pyverify.slot`` — the Linux harness's user-microSD boot slots, over Ethernet.

net-protocol.md v0.14 "Slot images" (Linux harness plan §10a S10). Two halves:

* a PUSH of a stage0 S0LB boot image (``linux_slot.img``) into the INACTIVE slot:
  the ordinary 24-byte MPS3 framing on 6910 (or TFTP 69) with header kind 2,
  ``rm_id`` 0 and ``rm_slot`` = the slot the tool expects to write (0 = "whichever
  is inactive"). The harness writes it to the card, then reads it back through
  stage0's own loader in the background;
* the 6900 verb ``{"op":"slot","act":"status|commit|rollback|verify"[,"slot":"A|B"]}``,
  which answers every act with the same status object (the state AFTER the act).

The flow a tool runs::

    st = slot_status(host)                         # which slot is the target?
    sid = bundle["targets"]["ethernet"]["provisioned"]["static_id"]   # the IMAGE's, never
    push_slot_image(image, host, static_id=int(sid, 16))              # the board's own
    wait_job(host)                                 # the card read-back: ok / failed
    slot_request(host, "commit")                   # the default becomes the pushed slot
    # then the `reboot` verb, and identify/version to see the new image

A bare-metal harness answers every act ``{"ok":false,"err":"slot not supported"}``
and closes a kind-2 push unread, exactly as it treats any unknown kind.

THE LOCK (the project lead, 2026-09-24): once the board's SSH is CLAIMED (``identify``'s
``ssh.claimed``), the mutations -- the push, ``commit``, ``rollback`` -- are refused
from any peer that is not the board itself (``slot locked: board claimed (use
ssh)``; TFTP error 2). ``status`` and ``verify`` stay open. The owner goes through
SSH: :class:`SshTunnel` forwards local ports to the board's 127.0.0.1:6900/6910,
so the harness sees a local peer; ``pyverify slot`` does it automatically when the
board reports claimed.

Stdlib only. Nothing here parses more of the image than the push needs to be
refused locally for an obvious mistake (not an S0LB v2 file); the harness runs
stage0's full check (``stage0_pack.check_image`` semantics) and is the authority.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import struct
import subprocess
import time
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client import CONTROL_PORT
from .localport import busy_ports, port_free, wait_ports_free
from .pusher import RAW_TCP_PORT, TFTP_PORT, PushError, tcp_send, tftp_put

__all__ = [
    "SLOT_IMAGE_KIND", "SLOT_ACTS", "SLOT_LOCKED_ERR", "SlotError", "image_info",
    "frame_slot_image", "push_slot_image", "slot_request", "slot_status", "wait_job",
    "ssh_tunnel_argv", "SshTunnel", "SshTunnelError", "SSH_FORWARD_OPTS",
]

SLOT_IMAGE_KIND = 2            # net_proto.h MPS3_BIN_KIND_SLOT_IMAGE
SLOT_LOCKED_ERR = "slot locked: board claimed (use ssh)"   # net-protocol.md "The lock"
SLOT_ACTS = ("status", "commit", "rollback", "verify")
_S0LB_MAGIC = 0x424C3053       # "S0LB" (stage0_boot.h)
_S0LB_VERSION = 2
_HDR = struct.Struct(">4sHBBIIII")   # the MPS3 framing header (pusher.BitstreamHeader)


class SlotError(Exception):
    """The harness refused a slot act (``err`` is its reason), or a job failed."""

    def __init__(self, message: str, reply: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.reply = reply


class SshTunnelError(SlotError):
    """The ssh tunnel to the board would not come up (the CLI's exit 3)."""


def image_info(image: bytes) -> Dict[str, int]:
    """The S0LB header facts a tool needs: ``hdr_crc`` (the table CRC stored at
    offset 28 -- the image's identity everywhere: the slot status, stage0's
    ``image_hdr_crc``), ``entries``, ``entry_pc``. Raises :class:`SlotError` for a
    file that is not an S0LB v2 image (the harness would refuse it anyway)."""
    if len(image) < 32:
        raise SlotError("not a stage0 image: shorter than the 32-byte header")
    magic, ver, n, pc, _a0, _a1, _flags, hcrc = struct.unpack_from("<8I", image, 0)
    if magic != _S0LB_MAGIC:
        raise SlotError("not a stage0 S0LB image (bad magic) -- pack it with stage0_pack.py")
    if ver != _S0LB_VERSION:
        raise SlotError(f"S0LB version {ver}; the harness takes {_S0LB_VERSION}")
    if not 1 <= n <= 8 or len(image) < 32 + 16 * n:
        raise SlotError("S0LB entry table out of range / truncated")
    table = bytearray(image[:32 + 16 * n])
    table[28:32] = b"\0\0\0\0"
    if zlib.crc32(bytes(table)) & 0xFFFFFFFF != hcrc:
        raise SlotError("S0LB table CRC mismatch (corrupt image)")
    return {"hdr_crc": hcrc, "entries": n, "entry_pc": pc}


def frame_slot_image(image: bytes, *, static_id: int, slot: Optional[str] = None) -> bytes:
    """Header + payload for a slot push. The payload is the image zero-padded to a
    whole word (the framing counts 32-bit words; stage0 never reads the pad).
    ``static_id`` is the static the image was PROVISIONED for (its bundle's
    ``targets.ethernet.provisioned.static_id``); the harness refuses it unless it
    equals the fabric's."""
    if slot not in (None, "A", "B"):
        raise SlotError(f"slot must be A, B or None, not {slot!r}")
    pad = (-len(image)) % 4
    payload = bytes(image) + b"\0" * pad
    hdr = _HDR.pack(b"MPS3", 1, SLOT_IMAGE_KIND, {None: 0, "A": 1, "B": 2}[slot],
                    static_id & 0xFFFFFFFF, 0, len(payload) // 4,
                    zlib.crc32(payload) & 0xFFFFFFFF)
    return hdr + payload


def push_slot_image(image: bytes, host: str, *, static_id: int, slot: Optional[str] = None,
                    via: str = "tcp", port: Optional[int] = None,
                    timeout_s: float = 30.0) -> int:
    """Push ``image`` into the harness's inactive slot. Returns bytes sent.

    The push only DELIVERS the bytes: 6910 has no in-band verdict (the harness
    closing is the only signal), so the card verdict is :func:`wait_job`'s. TFTP's
    final ACK means the bytes arrived intact and the image is self-consistent; the
    card read-back still follows.

    ``timeout_s`` is a STALL limit, never a bound on the whole transfer: over tcp
    it bounds the connect and each 64 KiB chunk (:data:`pusher.TCP_SEND_CHUNK`),
    over tftp each packet's ACK (capped at 5 s). A 24 MB image over a slow ssh
    tunnel takes as long as it takes (the first-install rehearsal was cut at 30 s
    when this bounded the whole ``sendall``)."""
    image_info(image)
    frame = frame_slot_image(image, static_id=static_id, slot=slot)
    if via == "tcp":
        return tcp_send(frame, host, port or RAW_TCP_PORT, timeout_s=timeout_s)
    if via == "tftp":
        return tftp_put(frame, host, port or TFTP_PORT, filename="linux_slot.img",
                        timeout_s=min(timeout_s, 5.0))
    raise SlotError(f"via must be 'tcp' or 'tftp', not {via!r}")


def _one_line(host: str, port: int, obj: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """One request on a fresh 6900 connection. 6900 is single-client and answers a
    second connection accept-then-EOF while it still holds the first, so an EOF
    (or a reset) before any byte is retried a few times, like every client does.
    A connect that fails (nothing listening, no route) is NOT retried."""
    last: Optional[Exception] = None
    for _ in range(20):
        s = socket.create_connection((host, port), timeout=timeout)   # OSError: unreachable
        try:
            s.sendall(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if buf:
                return json.loads(buf)
            last = ConnectionError("shell control channel closed by peer")
        except (ConnectionResetError, BrokenPipeError) as exc:
            last = exc
        finally:
            s.close()
        time.sleep(0.05)
    raise ConnectionError(f"6900 at {host}:{port} kept refusing: {last}")


def slot_request(host: str, act: str, slot: Optional[str] = None, *, port: int = CONTROL_PORT,
                 timeout: float = 5.0) -> Dict[str, Any]:
    """``{"op":"slot","act":act[,"slot":slot]}``. Returns the status object; raises
    :class:`SlotError` (with ``.reply``) on ``ok:false``."""
    if act not in SLOT_ACTS:
        raise SlotError(f"act must be one of {SLOT_ACTS}, not {act!r}")
    req: Dict[str, Any] = {"op": "slot", "act": act}
    if slot is not None:
        req["slot"] = slot
    reply = _one_line(host, port, req, timeout)
    if not reply.get("ok"):
        raise SlotError(f"slot {act}: {reply.get('err', '?')}", reply)
    return reply


def slot_status(host: str, *, port: int = CONTROL_PORT, timeout: float = 5.0) -> Dict[str, Any]:
    return slot_request(host, "status", port=port, timeout=timeout)


def wait_job(host: str, *, port: int = CONTROL_PORT, timeout_s: float = 180.0,
             poll_s: float = 0.5) -> Dict[str, Any]:
    """Poll ``status`` until the card job is no longer ``writing``/``verifying``.
    Returns the final status; raises :class:`SlotError` if the job failed (its
    ``err`` says why) or it did not finish in ``timeout_s``. The MBV's SPI card
    reads ~1 MB/s, so a 24 MB image's read-back takes ~30 s on the board."""
    deadline = time.monotonic() + timeout_s
    while True:
        st = slot_status(host, port=port)
        job = st.get("job", {})
        if job.get("state") not in ("writing", "verifying"):
            if job.get("state") == "failed":
                where = f" {job['slot']}" if job.get("slot") else ""
                raise SlotError(f"slot {job.get('act')}{where}: {job.get('err')}", st)
            return st
        if time.monotonic() >= deadline:
            raise SlotError(f"slot job still {job.get('state')} after {timeout_s:.0f} s", st)
        time.sleep(poll_s)


# --------------------------------------------------------------------------- #
# The owner's way past the lock: an ssh tunnel to the board's own 127.0.0.1
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: Options every FORWARDING ssh gets (ILA-mint finding #20, 2026-09-24). An ssh
#: ControlMaster keeps a ``-L`` forward bound after the ``ssh -N`` that asked for
#: it has exited: on a dev box local 2542/2547 stayed bound to a master (pid
#: 2327753) long after their sessions ended, and the user's ~/.ssh/config uses a
#: ControlMaster for the hub. A forward left bound to the board's 127.0.0.1
#: 6900/6910 would pass the claim lock for ANY local process. So: never a master,
#: never a mux client (the forward lives and dies with this process), and a port
#: that cannot be bound is a failure, not a warning. Command-line ``-o`` beats the
#: config file (ssh uses the first value it obtains).
SSH_FORWARD_OPTS = ("-o", "ControlMaster=no", "-o", "ControlPath=none",
                    "-o", "ExitOnForwardFailure=yes")


def ssh_tunnel_argv(target: str, forwards: Sequence[Tuple[int, int]], *,
                    ssh: Optional[str] = None) -> List[str]:
    """``ssh -N -L 127.0.0.1:<local>:127.0.0.1:<board port> ... <target>``. The
    target is normally the ``mps3-linux`` alias, whose ssh config carries the
    ProxyJump (docs/LINUX_HARNESS.md §2.1); key-only and never a prompt, like
    every non-interactive pyverify ssh, and never through a ControlMaster
    (:data:`SSH_FORWARD_OPTS`). ``$MPS3_SSH`` overrides the command (split like a
    shell would, so it may carry arguments)."""
    from .linux import _KEY_ONLY
    argv = [*shlex.split(ssh or os.environ.get("MPS3_SSH", "ssh")), *_KEY_ONLY,
            "-o", "BatchMode=yes", *SSH_FORWARD_OPTS, "-N"]
    for local, remote in forwards:
        argv += ["-L", "127.0.0.1:%d:127.0.0.1:%d" % (local, remote)]
    argv.append(target)
    return argv


class SshTunnel:
    """``with SshTunnel("mps3-linux", (6900, 6910)) as t: t.local(6900)`` -- local
    ports forwarded over ssh to the BOARD's loopback, so the harness sees a local
    peer (the lock lets it mutate). Ready when EVERY forward is listening: the
    control port (``remote_ports[0]``) answers a connect through it, and every other
    local port is held by a listener. Closed on exit.

    Why every forward (the slot-lock e2e flake, 2026-09-24): readiness used to be
    the control port alone, so under load ``slot push`` connected to the 6910
    forward before its listener existed -- ECONNREFUSED, exit 3. A forwarder owes
    no ordering between its listeners (the e2e test's fake binds each in its own
    thread). The other ports are checked LOCALLY (a bind probe), never by a
    connect: through ssh a connect opens a board-side connection, and 6910 is one
    transfer at a time -- a probe could hold the session the real push then finds
    busy. The control forward also goes LAST on the ssh command line: OpenSSH binds
    its ``-L`` listeners one after another, in argv order, before it accepts
    anything, so once the control port answers every other forward is already
    bound and the bind probe never races ssh's own bind.

    The local ports are checked TWICE (finding #20): before ssh starts, every one
    must be free -- something already listening there would answer the readiness
    probe and carry this tool's mutations to wherever IT goes (a forward left by a
    ControlMaster, possibly to another board); and after ssh has exited, every one
    must be free again -- a port still bound is a forward that outlived its
    process, and it would pass the claim lock for anyone on this host.
    ``local_ports`` (additive) pins the local side, one per ``remote_ports``
    entry; the default is a fresh ephemeral port each."""

    #: How long :meth:`close` waits for the local ports to be released.
    RELEASE_S = 3.0

    def __init__(self, target: str = "mps3-linux", remote_ports: Sequence[int] = (6900, 6910), *,
                 ssh: Optional[str] = None, timeout_s: float = 20.0,
                 local_ports: Optional[Sequence[int]] = None) -> None:
        self.target = target
        self.remote_ports = list(remote_ports)
        self.ssh = ssh
        self.timeout_s = timeout_s
        if local_ports is not None and len(local_ports) != len(self.remote_ports):
            raise ValueError("local_ports needs one port per remote port")
        self.local_ports = list(local_ports) if local_ports is not None else None
        #: Ports still bound after the last :meth:`close` (``[]`` = released).
        self.leaked: List[int] = []
        self._map: Dict[int, int] = {}
        self._proc: Optional[subprocess.Popen] = None

    def local(self, remote_port: int) -> int:
        return self._map[remote_port]

    def __enter__(self) -> "SshTunnel":
        if self.local_ports is not None:
            self._map = dict(zip(self.remote_ports, self.local_ports))
        else:
            self._map = {rp: _free_port() for rp in self.remote_ports}
        busy = busy_ports(self._map.values())
        if busy:
            raise SshTunnelError(
                f"local port(s) {', '.join(map(str, busy))} already bound: refusing to tunnel "
                f"to {self.target} (a forward that outlived its ssh -- a ControlMaster? -- would "
                f"answer in the tunnel's place). Free it (`ssh -O exit <host>` for a master) "
                f"or choose other local ports")
        probe_rp, others = self.remote_ports[0], self.remote_ports[1:]
        argv = ssh_tunnel_argv(self.target, [(self._map[rp], rp) for rp in (*others, probe_rp)],
                               ssh=self.ssh)
        self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        first = self._map[probe_rp]
        rest = [self._map[rp] for rp in others]
        answered = False
        deadline = time.monotonic() + self.timeout_s
        while True:
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
                raise SshTunnelError(f"ssh tunnel to {self.target} failed (rc {self._proc.returncode}): "
                                f"{err or 'no output'}")
            if not answered:
                try:
                    with socket.create_connection(("127.0.0.1", first), timeout=1.0):
                        answered = True        # once: each probe is a board-side connection
                except OSError:
                    pass
            # A listener makes a SO_REUSEADDR bind fail on Linux/macOS. Windows lets
            # that bind through a listener, so there the probe cannot see one and the
            # argv order above is the whole guarantee.
            late = [p for p in rest if port_free(p)] if answered and os.name != "nt" else []
            if answered and not late:
                return self
            if time.monotonic() >= deadline:
                self.close(check=False)
                what = (f"local port(s) {', '.join(map(str, late))} not listening" if answered
                        else f"local port {first} not answering")
                raise SshTunnelError(f"ssh tunnel to {self.target} not up after "
                                     f"{self.timeout_s:.0f} s ({what})")
            time.sleep(0.1)

    def close(self, *, check: bool = True) -> List[int]:
        """Stop ssh, then wait up to :attr:`RELEASE_S` for the local ports to be
        released. Returns the ports still bound; with ``check`` (the default)
        raises :class:`SshTunnelError` when there are any."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self._proc is not None and self._proc.stderr is not None:
            self._proc.stderr.close()
        had = self._proc is not None
        self._proc = None
        self.leaked = wait_ports_free(self._map.values(), self.RELEASE_S) if had else []
        if self.leaked and check:
            raise SshTunnelError(
                f"local port(s) {', '.join(map(str, self.leaked))} still bound after the ssh "
                f"tunnel to {self.target} exited: a forward outlived its process (a "
                f"ControlMaster?) and still reaches the board's loopback -- close it now "
                f"(`ssh -O exit <host>`)")
        return self.leaked

    def __exit__(self, exc_type: object, *exc: object) -> None:
        # A leak is raised only when nothing else is already propagating: the
        # first error is the one the caller needs; ``leaked`` keeps the second.
        self.close(check=exc_type is None)

