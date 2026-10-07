"""``pyverify.linux`` — reaching the MicroBlaze V Linux harness: SSH, console, rescue.

Three ways in, one per failure depth (docs/LINUX_HARNESS.md is the user guide):

``ssh``
    A root shell on the board, KEY-ONLY. The board sits on the hub's private
    link (``192.168.10.101``), so every connection jumps through the hub. The
    user's ``~/.ssh/config`` carries that as a ``Host mps3-linux`` stanza
    (:func:`ssh_config_stanza` prints it); :func:`ssh_argv` builds the call and
    refuses password and keyboard-interactive auth outright, so a missing key
    fails fast instead of hanging on a prompt nobody can answer.
``console``
    The serial console, FPGA UART lane 2 -> host port ``/dev/<target>/tty_02``
    at 115200, SHARED over TCP by fpgahub on the hub (``fpgahub share start``).
    This is the only way in when Linux is not up (stage0 banner, kernel panic,
    the getty that runs ``mps3-unclaim``). INPUT IS PACED: the uartlite RX FIFO
    is 16 bytes deep and nothing drains it faster than the CPU services it, so
    a paste overruns it. :data:`DEFAULT_PACE_S` per byte is the measured-safe
    rate from the 2026-09-23 board window.
``netboot``
    stage0's TFTP rescue: when neither µSD slot boots, stage0 runs a TFTP-WRQ
    server on UDP 69 and boots whatever blob is pushed to it (CRC-checked). The
    push tool is STAGE0's (``src/linux_soc/hw/fw_stage0/stage0_push.py``); this
    wraps it and can run it ON the hub, which is the only host on the board's
    link unless a standalone PC is cabled in.

NO SITE HOST IS BAKED IN (same rule as :mod:`pyverify.lease`): the hub comes
from ``MPS3_HUB``; ``MPS3_ON_HUB=1`` means "we are already there". The board's
own address is not a site secret -- it is the shell's static fallback and is
already the default everywhere in pyverify.
"""
from __future__ import annotations

import os
import re
import select
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .lease import board_tty, default_target

__all__ = [
    "SSH_ALIAS",
    "BOARD_IP",
    "CONSOLE_TTY",
    "CONSOLE_BAUD",
    "DEFAULT_PACE_S",
    "LinuxHarnessError",
    "ssh_argv",
    "ssh_config_stanza",
    "ConsoleShare",
    "parse_share_endpoint",
    "stage0_push_argv",
    "default_stage0_push_tool",
    "STAGE0_PUSH_EXIT",
]

#: stage0_push.py's exit codes (STAGE0_CONTRACT.md §9). ``pyverify netboot``
#: exits with the SAME code -- STAGE0's contract owns them -- and prints this.
STAGE0_PUSH_EXIT = {
    0: "accepted: stage0 verified the image and is booting it",
    1: "bad local image (stage0_pack.py --check it)",
    2: "REJECTED by stage0 (its status block says why: pyverify netboot --status)",
    3: "not stage0, or unreachable (is the board in rescue? pyverify identify)",
    4: "transfer failed mid-push",
}

#: The ``Host`` alias docs/LINUX_HARNESS.md tells the user to add.
SSH_ALIAS = "mps3-linux"
#: The board's static fallback address on the hub-private link (DHCP first,
#: this second -- IMAGE lane). Not a site secret; see the module docstring.
BOARD_IP = "192.168.10.101"
#: fpgahub's stable symlink for FPGA UART lane 2 (the re-pinned shell console),
#: ``/dev/<MPS3_LEASE_TARGET>/tty_02`` (see :func:`pyverify.lease.board_tty`).
CONSOLE_TTY = board_tty(2)
CONSOLE_BAUD = 115200
#: Per-byte input pacing. The uartlite RX FIFO is 16 deep; ~20 ms/char was the
#: rate that never dropped a byte on the 2026-09-23 board window.
DEFAULT_PACE_S = 0.02

#: Options every non-interactive call gets: key-only, never a prompt.
_KEY_ONLY = (
    "-o", "PreferredAuthentications=publickey",
    "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
)


class LinuxHarnessError(Exception):
    """A Linux-harness helper could not do what was asked (no hub configured,
    no share found, the push tool is missing, ...)."""


def _hub_from_env(hub: Optional[str]) -> str:
    hub = hub or os.environ.get("MPS3_HUB") or os.environ.get("FPGAHUB_HOST")
    if not hub:
        raise LinuxHarnessError(
            "no hub configured: set MPS3_HUB=<hub-host> (the host on the board's "
            "private link). There is deliberately no default host.")
    return hub


def ssh_argv(
    target: str = SSH_ALIAS,
    *,
    direct: bool = False,
    hub: Optional[str] = None,
    board_ip: str = BOARD_IP,
    user: str = "root",
    batch: bool = False,
    tty: bool = False,
) -> List[str]:
    """Build an ``ssh`` argv to the Linux harness. Pure; nothing is run.

    ``target`` defaults to the ``mps3-linux`` alias (the user's ssh config does
    the ProxyJump). ``direct=True`` needs no config: ``-J <MPS3_HUB>
    root@192.168.10.101``. ``batch=True`` adds ``BatchMode`` (for scripted
    reads, where even a key passphrase prompt must fail rather than wait).
    Password and keyboard-interactive auth are refused either way (DL5:
    key-only root; the password exists only on the serial console).
    """
    argv = ["ssh", *_KEY_ONLY]
    if batch:
        argv += ["-o", "BatchMode=yes"]
    if tty:
        argv.append("-t")
    if direct:
        argv += ["-J", _hub_from_env(hub), "%s@%s" % (user, board_ip)]
    else:
        argv.append(target)
    return argv


def ssh_config_stanza(hub: str = "<hub-host>", *, alias: str = SSH_ALIAS,
                      board_ip: str = BOARD_IP,
                      identity: str = "~/.ssh/id_ed25519") -> str:
    """The ``~/.ssh/config`` block docs/LINUX_HARNESS.md asks for.

    ``HostKeyAlias`` + a dedicated ``UserKnownHostsFile`` keep the board's
    pinned host key (baked per board, persisted on the µSD -- DL5) apart from
    whatever else has ever answered on 192.168.10.101, so a bare-metal/Linux
    swap or a re-imaged card is a LOUD host-key mismatch, not a silent trust.
    """
    return "\n".join([
        "Host %s" % alias,
        "    HostName %s" % board_ip,
        "    User root",
        "    ProxyJump %s" % hub,
        "    IdentityFile %s" % identity,
        "    IdentitiesOnly yes",
        "    PreferredAuthentications publickey",
        "    PasswordAuthentication no",
        "    KbdInteractiveAuthentication no",
        "    HostKeyAlias %s" % alias,
        "    UserKnownHostsFile ~/.ssh/known_hosts_%s" % alias.replace("-", "_"),
        "    StrictHostKeyChecking ask",
        "    ServerAliveInterval 5",
        "    ServerAliveCountMax 3",
        "",
    ])


# --------------------------------------------------------------------------- #
# Console: fpgahub's TTY share of lane 2
# --------------------------------------------------------------------------- #

#: ``fpgahub share start`` prints ``share <tty> → <host>:<port>`` (rich's arrow;
#: ``->`` accepted too, in case a renderer downgrades it).
_SHARE_LINE = re.compile(r"(?P<tty>/\S+)\s+(?:→|->|\S)\s+(?P<host>[\w.\-]+):(?P<port>\d+)")


def parse_share_endpoint(text: str, tty: str = CONSOLE_TTY) -> Tuple[str, int]:
    """Pull ``(host, port)`` for ``tty`` out of ``fpgahub share start|list``
    output. Raises :class:`LinuxHarnessError` when that tty is not in it -- a
    share for a DIFFERENT lane must never be mistaken for the console."""
    for line in text.splitlines():
        m = _SHARE_LINE.search(line)
        if m and m.group("tty") == tty:
            return m.group("host"), int(m.group("port"))
    raise LinuxHarnessError(
        "no fpgahub share for %s in:\n%s" % (tty, text.strip() or "(no output)"))


@dataclass
class ConsoleShare:
    """Find-or-start the fpgahub share of the console tty, then attach to it.

    ``runner`` is a :class:`pyverify.lease.HubRunner` (or any
    ``(argv, timeout=None) -> RunResult``); omitted, it is built from the
    environment exactly as the lease verbs build theirs, so ``MPS3_HUB`` /
    ``MPS3_ON_HUB`` mean the same thing everywhere.
    """

    #: fpgahub target; ``None`` => ``MPS3_LEASE_TARGET`` / the neutral default.
    target: Optional[str] = None
    #: host tty; ``None`` => lane 2 of ``target`` (:func:`pyverify.lease.board_tty`).
    tty: Optional[str] = None
    baud: int = CONSOLE_BAUD
    runner: Optional[Callable] = None
    hub: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.target:
            self.target = default_target()
        if not self.tty:
            self.tty = board_tty(2, self.target)

    def _runner(self):
        if self.runner is None:
            from .lease import hub_runner_from_env
            self.runner = hub_runner_from_env()
        return self.runner

    def endpoint(self) -> Tuple[str, int]:
        """The share's ``(host, port)``: an existing share of this tty is
        reused (fpgahub allows many readers, one writer); otherwise one is
        started."""
        run = self._runner()
        listed = run(["fpgahub", "share", "list", self.target], timeout=30)
        try:
            return parse_share_endpoint(listed.text if hasattr(listed, "text") else str(listed), self.tty)
        except LinuxHarnessError:
            pass
        started = run(["fpgahub", "share", "start", self.target, self.tty,
                       "--baud", str(self.baud)], timeout=30)
        text = started.text if hasattr(started, "text") else str(started)
        if getattr(started, "returncode", 0) != 0:
            raise LinuxHarnessError("fpgahub share start failed: %s" % text.strip())
        return parse_share_endpoint(text, self.tty)

    def attach_argv(self, host: str, port: int) -> Optional[List[str]]:
        """How to reach ``host:port`` from HERE. ``None`` = connect a socket
        directly (we are on the hub, or the share binds a routable address).
        Otherwise an ``ssh -W`` netcat-mode argv through the hub -- no ``nc``
        needed on either end."""
        on_hub = bool(os.environ.get("MPS3_ON_HUB"))
        loopback = host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")
        if on_hub or not loopback:
            return None
        hub = _hub_from_env(self.hub)
        dest = "127.0.0.1" if host == "0.0.0.0" else host
        return ["ssh", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes",
                "-W", "%s:%d" % (dest, port), hub]


class _Stream:
    """A byte stream over either a socket or an ``ssh -W`` subprocess."""

    def __init__(self, host: str, port: int, argv: Optional[List[str]],
                 popen: Callable = subprocess.Popen):
        self._sock = None
        self._proc = None
        if argv is None:
            self._sock = socket.create_connection((host, port), timeout=10)
            self._sock.setblocking(False)
        else:
            self._proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)

    def fileno(self) -> int:
        return self._sock.fileno() if self._sock else self._proc.stdout.fileno()

    def send(self, data: bytes) -> None:
        if self._sock:
            self._sock.setblocking(True)
            self._sock.sendall(data)
            self._sock.setblocking(False)
        else:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def recv(self, n: int = 4096) -> bytes:
        if self._sock:
            try:
                return self._sock.recv(n)
            except BlockingIOError:
                return b""
        return os.read(self._proc.stdout.fileno(), n)

    def close(self) -> None:
        if self._sock:
            self._sock.close()
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:  # pragma: no cover
                self._proc.kill()


def send_paced(stream, data: bytes, pace_s: float = DEFAULT_PACE_S,
               sleep: Callable[[float], None] = time.sleep) -> int:
    """Write ``data`` one byte at a time, ``pace_s`` apart. Returns bytes sent.
    The pacing is the whole point: see :data:`DEFAULT_PACE_S`."""
    for i in range(len(data)):
        stream.send(data[i:i + 1])
        if pace_s > 0:
            sleep(pace_s)
    return len(data)


def read_for(stream, seconds: float, out=None, *,
             now: Callable[[], float] = time.monotonic) -> bytes:
    """Collect whatever the console prints for ``seconds``; echo to ``out``."""
    buf = bytearray()
    deadline = now() + seconds
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            break
        ready, _, _ = select.select([stream], [], [], min(remaining, 0.2))
        if not ready:
            continue
        chunk = stream.recv(4096)
        if not chunk:
            if stream._sock is None:
                break          # ssh -W exited: the share went away
            continue
        buf.extend(chunk)
        if out is not None:
            out.write(chunk)
            out.flush()
    return bytes(buf)


# --------------------------------------------------------------------------- #
# netboot: STAGE0's TFTP rescue push
# --------------------------------------------------------------------------- #

def default_stage0_push_tool() -> Path:
    """STAGE0's push tool, found beside this checkout (``MPS3_STAGE0_PUSH``
    overrides -- e.g. a copy staged on the hub)."""
    env = os.environ.get("MPS3_STAGE0_PUSH")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "src" / "linux_soc" / "hw" / "fw_stage0" / "stage0_push.py"
        if cand.is_file():
            return cand
    # Not found: return the canonical path so the error names what is missing.
    return here.parents[3] / "src" / "linux_soc" / "hw" / "fw_stage0" / "stage0_push.py"


#: stage0_push.py imports these from its own directory, so a copy staged on the
#: hub needs them beside it.
_STAGE0_PUSH_SIBLINGS = ("stage0_pack.py", "stage0_status.py")


def stage0_push_argv(blob: Optional[str], *, host: str = BOARD_IP, tool: Optional[Path] = None,
                     python: str = "python3", extra: Sequence[str] = (),
                     via_hub: Optional[str] = None,
                     remote_dir: str = "/tmp/pyverify_netboot") -> List[List[str]]:
    """The command(s) that push ``blob`` to stage0's rescue server.

    The push tool's CLI is STAGE0's (``STAGE0_CONTRACT.md`` §9:
    ``stage0_push.py HOST IMAGE [--port N] [--blksize N] [--no-ping]``); this
    passes ``extra`` through verbatim, so a flag the tool grows needs no
    pyverify change. Local: one command. ``via_hub``: copy the tool and blob to
    the hub, then run it there -- the board's rescue server is reachable only
    from its private link.
    """
    tool = Path(tool) if tool is not None else default_stage0_push_tool()
    tail = [host] + ([str(blob)] if blob is not None else [])
    if via_hub is None:
        return [[python, str(tool), *extra, *tail]]
    rtool = "%s/%s" % (remote_dir, tool.name)
    rtail = [host] + (["%s/%s" % (remote_dir, Path(blob).name)] if blob is not None else [])
    remote = " ".join(shlex.quote(a) for a in [python, rtool, *extra, *rtail])
    files = [str(tool)] + [str(tool.parent / s) for s in _STAGE0_PUSH_SIBLINGS
                           if (tool.parent / s).is_file()]
    if blob is not None:
        files.append(str(blob))
    return [
        ["ssh", "-o", "BatchMode=yes", via_hub, "mkdir -p %s" % shlex.quote(remote_dir)],
        ["scp", "-q", "-o", "BatchMode=yes", *files, "%s:%s/" % (via_hub, remote_dir)],
        ["ssh", "-o", "BatchMode=yes", via_hub, remote],
    ]
