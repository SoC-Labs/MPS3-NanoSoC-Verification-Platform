"""test_jtag_io_e2e.py -- 6921 on the REAL host harnessd (MOCK HAL: a fake TAP behind
JTAGBB) over real kernel sockets, batched (the default) against --jtag-io per-byte
(jtag_server.c's own loop): the same OpenOCD-shaped streams, sent in random chunk
sizes, must come back as the same bytes. tests/test_jtag_batch.c proves the
register-level equivalence; this proves the socket path and the flag.
"""
from __future__ import annotations

import os
import random
import socket
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harnessd_mock import TAP_IDCODE, Harnessd  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")


def _w(tck, tms, tdi):
    return bytes([ord("0") + (tck << 2 | tms << 1 | tdi)])


def _clk(out, tms, tdi=0, read=False):
    out += _w(0, tms, tdi)
    if read:
        out += b"R"
    out += _w(1, tms, tdi)
    return out


def _idcode_stream():
    out = b""
    for _ in range(5):
        out = _clk(out, 1)               # Test-Logic-Reset (selects IDCODE)
    for tms in (0, 1, 0, 0):
        out = _clk(out, tms)             # RTI -> Select-DR -> Capture-DR -> Shift-DR
    for i in range(32):
        out = _clk(out, 1 if i == 31 else 0, read=True)
    for tms in (1, 0):
        out = _clk(out, tms)             # Update-DR -> RTI
    return out + _w(0, 0, 0)


def _exchange(port, stream, chunks, want):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    got, off, k = b"", 0, 0
    while off < len(stream):
        n = chunks[k % len(chunks)]
        k += 1
        s.sendall(stream[off:off + n])
        off += n
        s.settimeout(0.002)
        try:
            got += s.recv(65536)
        except socket.timeout:
            pass
    s.settimeout(5)
    while len(got) < want:
        d = s.recv(65536)
        assert d, "6921 closed early"
        got += d
    s.sendall(b"Q")
    s.close()
    return got


def _idcode(bits):
    return sum((1 if ch == ord("1") else 0) << i for i, ch in enumerate(bits[:32]))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_batched_and_per_byte_answer_the_same_bytes(tmp_path, seed):
    rng = random.Random(seed)
    one = _idcode_stream()
    stream = one * 40                                  # 40 IDCODE reads back to back
    chunks = [rng.randint(1, 400) for _ in range(16)]
    want = 32 * 40
    results = {}
    for mode in ("batched", "per-byte"):
        h = Harnessd(BIN, tmp_path / mode, extra=["--jtag-io", mode]).start()
        try:
            t0 = time.monotonic()
            results[mode] = (_exchange(h.port(6921), stream, chunks, want), time.monotonic() - t0)
        finally:
            h.stop()
    a, b = results["batched"][0], results["per-byte"][0]
    assert a == b, "the reply stream changed with the I/O mode"
    for k in range(40):
        assert _idcode(a[32 * k:32 * k + 32]) == TAP_IDCODE
    print(f"\n  40 IDCODE reads: batched {results['batched'][1] * 1e3:.1f} ms, "
          f"per-byte {results['per-byte'][1] * 1e3:.1f} ms (x86 loopback)")


def test_jtag_io_rejects_a_bad_mode(tmp_path):
    import subprocess
    p = subprocess.run([BIN, "--jtag-io", "sideways", "--no-hw"], capture_output=True, text=True,
                       timeout=10)
    assert p.returncode != 0 and "--jtag-io batched|per-byte" in p.stderr
