"""The paced DUT-console writer (ILA-mint finding #17).

The DUT's UART RX FIFO is shallow and has no flow control: line-rate input is
mangled into plausible-looking WRONG commands. ``pyverify.console`` now types at
20 ms/char by default (``send_paced``, ``ConsoleReader.write``/``write_line``, CLI
``pyverify dut-console --send``).

The FIFO test runs on a VIRTUAL clock (a fake socket + fake sleep), so it is
deterministic under any load: the model DUT holds 4 bytes and its firmware takes
one every 10 ms. NEGATIVE CONTROL: the same line unpaced overflows and arrives
mangled.
"""
from __future__ import annotations

import socket
import socketserver
import threading

import pytest

from pyverify import cli as cli_mod
from pyverify.console import DEFAULT_PACE_S, ConsoleReader, send_paced


class _Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s


class _Sock:
    """Records (virtual time, bytes) for every sendall."""

    def __init__(self, clock):
        self.clock = clock
        self.sends = []

    def sendall(self, b):
        self.sends.append((self.clock.t, bytes(b)))

    def settimeout(self, t):
        pass


def _dut_receives(sends, depth=4, drain_s=0.010) -> bytes:
    """A shallow RX FIFO with no flow control: `depth` bytes, the firmware
    takes one every `drain_s`; a byte that arrives to a full FIFO is lost."""
    fifo, got, t_drain = [], bytearray(), 0.0
    for t, chunk in sends:
        for b in chunk:
            while fifo and t_drain + drain_s <= t:     # the firmware caught up
                t_drain += drain_s
                got.append(fifo.pop(0))
            if not fifo:
                t_drain = max(t_drain, t)
            if len(fifo) < depth:
                fifo.append(b)
    got.extend(fifo)
    return bytes(got)


def test_default_pace_is_20ms_per_char():
    assert DEFAULT_PACE_S == 0.020


def test_send_paced_sends_one_byte_per_write_and_sleeps_after_each():
    clk = _Clock()
    s = _Sock(clk)
    assert send_paced(s, b"reset\r", sleep=clk.sleep) == 6
    assert [b for _, b in s.sends] == [b"r", b"e", b"s", b"e", b"t", b"\r"]
    assert [round(t, 3) for t, _ in s.sends] == [0.0, 0.02, 0.04, 0.06, 0.08, 0.1]


def test_a_paced_line_survives_a_shallow_fifo():
    clk = _Clock()
    s = _Sock(clk)
    send_paced(s, b"print(1+1)\r", sleep=clk.sleep)
    assert _dut_receives(s.sends) == b"print(1+1)\r"


def test_negative_control_an_unpaced_line_is_mangled():
    clk = _Clock()
    s = _Sock(clk)
    s.sendall(b"print(1+1)\r")                       # what an ad-hoc sendall does
    got = _dut_receives(s.sends)
    assert got != b"print(1+1)\r" and got == b"prin"
    clk2 = _Clock()
    s2 = _Sock(clk2)
    send_paced(s2, b"print(1+1)\r", 0.0, sleep=clk2.sleep)   # pace 0 = no pacing
    assert _dut_receives(s2.sends) == b"prin"


def test_console_reader_write_line_paces_with_its_own_rate():
    clk = _Clock()
    r = ConsoleReader("h", 6930, pace_s=0.05)
    r._sock = _Sock(clk)                             # a connected reader
    assert r.write_line("ab", sleep=clk.sleep) == 3
    assert [(round(t, 3), b) for t, b in r._sock.sends] == [(0.0, b"a"), (0.05, b"b"), (0.1, b"\r")]
    r._sock = _Sock(clk := _Clock())
    r.write(b"xy", pace_s=0.0, sleep=clk.sleep)
    assert [b for _, b in r._sock.sends] == [b"x", b"y"]


# --------------------------------------------------------------------------- #
# the CLI: pyverify dut-console
# --------------------------------------------------------------------------- #

class _EchoSrv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.got = bytearray()
        self.request.sendall(b"nanosoc> ")
        while True:
            try:
                d = self.request.recv(64)
            except OSError:
                return
            if not d:
                return
            self.server.got.extend(d)
            self.request.sendall(d)


@pytest.fixture
def dut():
    srv = _EchoSrv(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_cli_dut_console_types_paced_and_prints_the_reply(dut, capsysbinary):
    rc = cli_mod.main(["dut-console", "--host", "127.0.0.1", "--port", str(dut.server_address[1]),
                       "--send", "help", "--seconds", "0.6"])
    out, err = capsysbinary.readouterr()
    assert rc == 0
    assert bytes(dut.got) == b"help\r"
    assert b"nanosoc> help\r" in out
    assert b"at 20 ms/char" in err                   # the default pace


def test_cli_dut_console_parser_defaults():
    a = cli_mod.build_parser().parse_args(["dut-console", "--host", "h"])
    assert a.pace_ms == 20.0 and a.uart == 0 and a.port is None and a.send is None


def test_cli_dut_console_unreachable_exits_3(capsys):
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert cli_mod.main(["dut-console", "--host", "127.0.0.1", "--port", str(port)]) == 3
    assert "cannot reach" in capsys.readouterr().err
