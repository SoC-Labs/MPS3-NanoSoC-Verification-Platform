"""harnessd_mock.py — run mps3-harnessd (MOCK HAL) as a test subject, and read
or poke its behavioural fabric from Python.

The fabric file layout is hal_mock.c's (change both together):
  0x00000  header: magic "HDMF" @0x00, rm_loaded @0x10, icap_words @0x1C,
           wdog_kicks @0x20, mmcm_loads @0x2C, usr_access @0x30,
           usr_access_valid @0x34, mmcm_khz @0x3C (what the modelled MMCM runs)
  0x01000 + i*0x10000  the register page of block i, in hal_front.c's fixed
           hal_expected_blocks() order (BLOCKS below). The lmb-tail page holds
           the stage0 status block at +0xE00 and the diag mailbox at +0xF00.

Stdlib only; shared by test_harnessd_e2e.py and test_conformance_harnessd.py.
"""
from __future__ import annotations

import json
import mmap
import os
import signal
import socket
import struct
import subprocess
import time
import zlib
from pathlib import Path
from typing import List, Optional

BLOCKS = [
    ("clkrst", 0x44A00000), ("dfxctl", 0x44A10000), ("hwicap", 0x44A20000),
    ("vphy", 0x44A30000), ("genchk", 0x44A60000), ("jtag-bb", 0x44A70000),
    ("dbgbr", 0x44A80000), ("uartbr", 0x44A90000), ("gpio", 0x44AA0000),
    ("mmcm-drp", 0x44AB0000), ("clcd", 0x44AC0000), ("clcd-kvm", 0x44AD0000),
    ("touch-iic", 0x44AE0000), ("dutegr", 0x44B20000), ("usracc", 0x44B30000),
    ("wdog", 0x44B40000), ("lmb-tail", 0x0001F000),
]
_HDR = 0x1000
_SLOT = 0x10000
_NBYTES = _HDR + len(BLOCKS) * _SLOT

S0_MAGIC = 0x54533053
S0_CONFIRM_MAGIC = 0x4B4F3053          # "S0OK" (STAGE0_CONTRACT §3)
S0_FABRIC_SID, S0_FABRIC_VER32, S0_BOOT_COUNT, S0_PHASE, S0_ATT_CONFIRM = 0x10, 0x14, 0x18, 0x1C, 0x48
MARKER = 0x524D4944          # hal_mock.c: "RMID" + rm_id in the ICAP stream
HDR_MMCM_LOADS, HDR_MMCM_KHZ = 0x2C, 0x3C   # hal_mock.c: the modelled clk_wiz_dut MMCM
CLKRST, MMCM_DRP = 0x44A00000, 0x44AB0000
MMCM_CFG0, MMCM_CFG2 = 0x200, 0x208
MMCM_CFG0_POR, MMCM_CFG2_POR = (20 << 8) | 1, 20   # hal_mock.c's post-reset register file
_UARTQ = {0: 0x40, 1: 0x40 + 8 + 256}               # hal_mock.c mock_q_t u0, u1
TAP_IDCODE = 0x6BA00477

# The diag keys harnessd OMITS (version_linux.c mps3_proto_diag_omit).
LINUX_DIAG_OMITTED = frozenset({
    "rx_recover", "rx_dumps", "tx_status_drained", "tx_fifo_full_drops",
    "tx_space_stalls", "tx_iface_errors", "tx_last_status",
    "rcv_wnd", "rcv_ann_wnd", "rx_queued", "pbuf_free", "sndbuf", "snd_wnd",
    "ovlstore_phase", "ovlstore_detail",
})

# PRODUCT=1 minus windowed (Makefile / HARNESSD_CONTRACT §9.5), then the bits that
# follow a linked provider rather than a flag (HM_ANSWERS 2026-09-26): slot (14),
# xvc_lock (15), then the engine feature names (net_proto.h v0.15
# mps3_proto_features_extra): "lcd_mirror", then (v0.16, lane IDENT) "identity" and
# "locate" (a build with the panel), then (v0.17, lane PANEL-PROTO) "presence" and
# "panel" (a build with the panel).
HOST_FEATURES = ["clcd", "clcd_kvm", "touch", "hwicap_fifo", "dut_egress",
                 "jtag_server", "xvc_dbgbr", "stats", "log", "reboot", "touch_cal", "usd",
                 "slot", "xvc_lock", "lcd_mirror", "identity", "locate", "presence", "panel"]


class Fabric:
    """mmap view of a --mock-fabric file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists() or self.path.stat().st_size < _NBYTES:
            with open(self.path, "ab") as f:
                f.truncate(_NBYTES)
        self._f = open(self.path, "r+b")
        self._m = mmap.mmap(self._f.fileno(), _NBYTES)

    def close(self):
        self._m.close()
        self._f.close()

    def _addr(self, base: int, off: int) -> int:
        for i, (_, b) in enumerate(BLOCKS):
            if b == base:
                return _HDR + i * _SLOT + off
        raise KeyError(hex(base))

    def rd(self, base: int, off: int) -> int:
        return struct.unpack_from("<I", self._m, self._addr(base, off))[0]

    def wr(self, base: int, off: int, val: int) -> None:
        struct.pack_into("<I", self._m, self._addr(base, off), val & 0xFFFFFFFF)

    def hdr(self, off: int) -> int:
        return struct.unpack_from("<I", self._m, off)[0]

    # stage0 status block @ LMB 0x1FE00, mailbox @ 0x1FF00
    def s0(self, off: int) -> int:
        return self.rd(0x1F000, 0xE00 + off)

    def s0_write_block(self, static_id: int, *, boot_count: int = 1, garbage: bool = False,
                       phase: int = 6, fabric_ver32: int = 0):
        for w in range(64):
            self.wr(0x1F000, 0xE00 + 4 * w, 0)
        if garbage:
            for w in range(64):
                self.wr(0x1F000, 0xE00 + 4 * w, 0xDEAD0000 + w)
            return
        self.wr(0x1F000, 0xE00 + 0x00, S0_MAGIC)
        self.wr(0x1F000, 0xE00 + 0x04, 1)
        self.wr(0x1F000, 0xE00 + 0x08, 0x100)
        self.wr(0x1F000, 0xE00 + S0_FABRIC_SID, static_id)
        self.wr(0x1F000, 0xE00 + S0_FABRIC_VER32, fabric_ver32)
        self.wr(0x1F000, 0xE00 + S0_BOOT_COUNT, boot_count)
        self.wr(0x1F000, 0xE00 + S0_PHASE, phase)
        self.wr(0x1F000, 0xE00 + 0xFC, S0_MAGIC)

    def uart_inject(self, stream: int, data: bytes) -> None:
        """Bytes the DUT 'sends' on U0/U1: pushed into the bridge's DUT->host
        FIFO model (hal_mock.c mock_q_t: head @+0, tail @+4, q[256] @+8)."""
        q = _UARTQ[stream]
        for b in data:
            head = self.hdr(q)
            assert head - self.hdr(q + 4) < 256, "mock UART queue full"
            self._m[q + 8 + head % 256] = b
            struct.pack_into("<I", self._m, q, (head + 1) & 0xFFFFFFFF)

    def uart_pending(self, stream: int) -> int:
        q = _UARTQ[stream]
        return (self.hdr(q) - self.hdr(q + 4)) & 0xFFFFFFFF

    def fabric_reset(self) -> None:
        """What peripheral_aresetn (the POR button, the shell watchdog) does to the
        blocks harnessd reads here: CLKRST back to all-resets-asserted, and
        clk_wiz_dut's register file back to the IP's 50 MHz values -- while the
        MMCM (hdr mmcm_khz) keeps its DRP-written preset (clk_linux.c)."""
        self.wr(CLKRST, 0x00, 0)          # RESET_CTRL
        self.wr(CLKRST, 0x04, 0)          # DUT_CLK_SEL
        self.wr(MMCM_DRP, MMCM_CFG0, MMCM_CFG0_POR)
        self.wr(MMCM_DRP, MMCM_CFG2, MMCM_CFG2_POR)

    def mailbox(self) -> List[int]:
        return [self.rd(0x1F000, 0xF00 + 4 * w) for w in range(64)]


def free_port_offset() -> int:
    """An offset N such that N+69 .. N+6932 are very likely free: probe a few."""
    import random
    for _ in range(50):
        n = random.randrange(20000, 50000, 100)
        ok = True
        for p in (6900, 6910, 6921, 2542, 6930, 6931, 6932, 6940):   # 6940: the LCD mirror
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", n + p))
            except OSError:
                ok = False
            finally:
                s.close()
        for p in (69, 6899):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(("127.0.0.1", n + p))
            except OSError:
                ok = False
            finally:
                s.close()
        if ok:
            return n
    raise RuntimeError("no free port offset")


class Harnessd:
    """One mps3-harnessd process on loopback with every port at off+port."""

    def __init__(self, binary: str, workdir: Path, *, static_id: int = 0x5A5A0001,
                 claim: Optional[int] = None, s0: Optional[int] = None,
                 s0_garbage: bool = False, greybox: Optional[bytes] = b"\x20\0\0\0" * 16,
                 extra: Optional[List[str]] = None, offset: Optional[int] = None,
                 fabric_name: str = "fabric", run_marker: Optional[Path] = None,
                 usd_dev: Optional[Path] = None, healthy: bool = True):
        self.bin = binary
        self.dir = Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.off = offset if offset is not None else free_port_offset()
        self.static_id = static_id
        self.fabric_path = self.dir / fabric_name
        self.fabric = Fabric(self.fabric_path)
        if s0_garbage:
            self.fabric.s0_write_block(0, garbage=True)
        elif s0 is not None or (s0 is None and claim is None):
            self.fabric.s0_write_block(static_id if s0 is None else s0)
        (self.dir / "static_id").write_text(f"0x{(static_id if claim is None else claim):08x}\n")
        if greybox is not None:
            (self.dir / "greybox.bin").write_bytes(greybox)
        # IMAGE's boot-health verdict (S99mps3health): healthy unless a test says not.
        # healthy=False: harnessd never CONFIRMS this boot to stage0, so it never
        # stamps the booted slot's record either (slot_linux.c) -- for a test that
        # hashes the card around writes of its own.
        (self.dir / "boot-health").write_text("healthy=1\npersist=card\n" if healthy
                                              else "healthy=0\nreasons=test\n")
        (self.dir / "persist.state").write_text("backing=card dev=/dev/test storage=ok\n")
        self.log_path = self.dir / "harnessd.log"
        self.args = [
            binary, "--loopback", "--port-offset", str(self.off),
            "--mock-fabric", str(self.fabric_path),
            "--static-id-file", str(self.dir / "static_id"),
            "--version-file", str(self.dir / "version"),
            "--state-dir", str(self.dir / "state"),
            "--run-marker", str(run_marker or (self.dir / "run.marker")),
            "--authorized-keys", str(self.dir / "ssh" / "authorized_keys"),
            "--host-key", str(self.dir / "hostkey"),
            "--boot-health", str(self.dir / "boot-health"),
            "--net-state", str(self.dir / "net.state"),
            "--host-key-fp", str(self.dir / "host_key_sha256"),
            "--keys-sync", str(self.dir / "keys-sync"),
            "--netif", "harnessd-test-none0",
            # the BOARD identity (v0.16): this boot's run file (absent unless a test
            # writes it = the image defaults), identity_set's override, and IMAGE's
            # persist.state (card-backed unless a test says otherwise)
            "--identity", str(self.dir / "identity"),
            "--identity-override", str(self.dir / "persist" / "etc" / "mps3" / "identity"),
            "--persist-state", str(self.dir / "persist.state"),
        ] + (["--greybox", str(self.dir / "greybox.bin")] if greybox is not None else []) \
          + (["--usd-dev", str(usd_dev)] if usd_dev is not None else []) \
          + (extra or [])
        self.proc: Optional[subprocess.Popen] = None

    def port(self, p: int) -> int:
        return self.off + p

    def start(self, wait: bool = True):
        self._logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(self.args, stdout=self._logf, stderr=subprocess.STDOUT)
        if wait:
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"harnessd exited rc={self.proc.returncode}:\n{self.log()}")
                if "shell up:" in self.log():
                    try:
                        with socket.create_connection(("127.0.0.1", self.port(6900)), 0.5):
                            pass
                        time.sleep(0.05)
                        return self
                    except OSError:
                        pass
                time.sleep(0.05)
            self.stop(signal.SIGKILL)       # never leave a half-started harnessd behind
            raise RuntimeError("harnessd did not come up:\n" + self.log())
        return self

    def log(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def stop(self, sig=signal.SIGTERM, timeout=5.0) -> int:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        rc = self.proc.returncode if self.proc else 0
        if getattr(self, "_logf", None):
            self._logf.close()
        return rc

    # ---- 6900 ----
    def ctl(self) -> "Ctl":
        return Ctl(self.port(6900))

    def req(self, obj) -> dict:
        """One request on a fresh, ADOPTED connection (:class:`Ctl`): the single-
        client refusal is retried there, before the request is sent, so the
        request itself is sent exactly once."""
        with self.ctl() as c:
            return c.req(obj)


class Ctl:
    """One persistent 6900 connection (single-client server: keep it open),
    ADOPTED before it is handed out.

    6900 refuses a new connection (accept-then-close) while it still holds the
    previous client, and it drops that client only when a later pass reads its
    EOF -- accept runs FIRST in a pass (coordinator_net_poll), and a parked verb
    stops the read altogether. The old fixed 50 ms pause before connecting was a
    guess about the server's scheduling, and under load it lost: the request sat
    unread in a refused socket, the close sent RST, and _swap/_commit got
    ConnectionResetError (the harnessd e2e flakes of 2026-09-24). So the
    connection is opened with a ping and retried until a reply comes back. A
    refused connection is never read, so the retry repeats nothing; once the ping
    is answered the server holds THIS connection until it is closed.

    2026-09-28: the server now reaps a previous client whose peer has already
    closed before it refuses (reap before refuse, net_if.h mps3_net_peer_closed),
    so the close-then-reconnect case above no longer refuses. The retry stays: a
    previous client that is still connected, or one that closed with a request
    still unread, still wins the slot -- and the tests that must see no refusal
    at all use raw sockets (test_harnessd_e2e.py, "reap before refuse")."""

    def __init__(self, port: int, timeout: float = 10.0, adopt_s: float = 10.0):
        deadline = time.monotonic() + adopt_s
        while True:
            s = f = None
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
                f = s.makefile("rb")
                s.sendall(b'{"op":"ping"}\n')
                if f.readline():
                    self.s, self.f = s, f
                    return
                last: Exception = ConnectionError("accepted, then closed unread")
            except (ConnectionError, socket.timeout) as exc:   # refused / reset / EOF
                last = exc
            for x in (f, s):
                try:
                    if x is not None:
                        x.close()
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise ConnectionError(f"6900 never adopted a connection in {adopt_s:.0f} s: {last}")
            time.sleep(0.05)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self.f.close()
            self.s.close()
        except OSError:
            pass

    def send(self, obj) -> None:
        line = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode()
        self.s.sendall(bytes(line) + b"\n")

    def line(self) -> bytes:
        ln = self.f.readline()
        if not ln:
            raise ConnectionError("6900 closed")
        return ln.rstrip(b"\n")

    def req(self, obj) -> dict:
        self.send(obj)
        return json.loads(self.line())


def frame(payload: bytes, *, kind: int, static_id: int, rm_id: int) -> bytes:
    """The 24-byte big-endian MPS3 header + payload (net-protocol.md "Bitstream
    framing"; pyverify.pusher.frame_bitstream is the reference)."""
    hdr = struct.pack(">4sHBBIIII", b"MPS3", 1, kind, 0, static_id, rm_id,
                      len(payload) // 4, zlib.crc32(payload) & 0xFFFFFFFF)
    return hdr + payload


def partial_payload(rm_id: int, words: int = 1024) -> bytes:
    """A 'partial' the mock fabric recognises: NOPs, the RMID marker, rm_id, NOPs
    (big-endian words, as the .bin is)."""
    body = [0x20000000] * 8 + [MARKER, rm_id] + [0x20000000] * max(0, words - 10)
    return b"".join(struct.pack(">I", w) for w in body)


def clearing_payload(words: int = 64) -> bytes:
    return struct.pack(">I", 0x20000000) * words
