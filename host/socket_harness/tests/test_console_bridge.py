"""Board-free proofs for :mod:`socket_harness.console_bridge` — the headline
deliverable: a pure-Python ser2net-compatible pty/tcp console bridge, plus the
registry-driven ser2net/socat config emitters.

Everything here is socket-free-to-a-board: the REMOTE console end is an
in-process ``socket.socketpair`` (or a bespoke localhost listener for the
reconnect proof), and the LOCAL end is either a raw socketpair endpoint or the
genuine pty device from :func:`socket_harness.loopback.pty_loopback` (the same
``'hello'->WORLD_42`` / junk``->'unknown'`` rig lifted verbatim from
``scripts/mps3_console.py``'s self-test — a device that answered unconditionally
could not fake a pass, so the negative control is load-bearing).

Coverage:

* ``ser2net_yaml()`` / ``socat_argv()`` regenerate ``host/console/*`` from the
  ONE registry — asserted by substring (the ports 6930/6931/6932 and the
  ``/tmp/mps3-console/<name>`` pty links), never by byte-identity.
* ``ConsoleBridge.pump_once`` copies BOTH directions; the two directions are
  asserted independently so the documented mutation (delete the remote->local
  copy) reddens ONLY the "marker reached the local end" assertion while the
  write-direction assertion still passes.
* A full request/response round trip through the pty device with the
  scripts/mps3_console.py negative control.
* Reconnect: killing the remote mid-stream flips ``PumpStat.reconnected`` and
  the stream resumes.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from socket_harness.console_bridge import (
    ConsoleBridge,
    PumpStat,
    ser2net_yaml,
    socat_argv,
)
from socket_harness.endpoints import DEFAULT_SHELL_HOST, by_name, console_specs
from socket_harness.loopback import pty_loopback
from socket_harness.retry import RealClock, RetryPolicy

# A retry policy whose backoffs are all 0.0 so a reconnect is instant and the
# proof never actually sleeps (RealClock.sleep(0.0) is a no-op-ish).
_FAST = RetryPolicy(attempts=5, base=0.0, factor=1.0, cap=0.0)


def _until(seconds: float):
    """Yield until ``seconds`` of wall clock have elapsed.

    The pump loops below used a fixed iteration count as a stand-in for elapsed
    time -- ``for _ in range(120)`` alongside ``pump_once(0.05)``, i.e. "about
    six seconds". A ``pump_once`` can take arbitrarily longer than its timeout
    on a loaded machine, so the count is a time budget that SHRINKS exactly when
    the work needs more of it, and the test then fails on contention rather than
    on a defect. Observed 2026-09-09: this file's reconnect proof failed a
    check-ci run while a Vivado build, an overlay sweep and four agents shared
    the box, and passed 6/6 in isolation immediately after.

    A deadline keeps the fast path fast -- every one of these loops breaks as
    soon as its condition holds -- while letting a slow machine take the time it
    needs. The assertions are unchanged; only the patience is.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        yield


# --------------------------------------------------------------------------- #
# Local doubles (test-local glue around real fds — NOT reimplementations of the
# loopback fakes, which have no request/response reconnecting-device variant).
# --------------------------------------------------------------------------- #


class _FdLocal:
    """A :class:`LocalEndpoint` over a raw fd (the pty controller/master).

    Bytes written here surface on the pty's slave (read by the fake device);
    bytes the device writes to the slave surface here to be read back.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def open(self) -> None:  # already open — pty_loopback owns the fd
        pass

    def fileno(self) -> int:
        return self._fd

    def read(self, n: int = 4096) -> bytes:
        try:
            return os.read(self._fd, n)
        except (BlockingIOError, OSError):
            return b""

    def write(self, data: bytes) -> None:
        os.write(self._fd, data)

    def close(self) -> None:  # pty_loopback's teardown closes the fds
        pass


class _PairLocal:
    """A :class:`LocalEndpoint` backed by one end of a ``socket.socketpair``.

    ``.a`` is the endpoint the bridge drives; ``.b`` is the test's handle onto
    the far side (what a ser2net client would see / type).
    """

    def __init__(self) -> None:
        self.a, self.b = socket.socketpair()

    def open(self) -> None:
        pass

    def fileno(self) -> int:
        return self.a.fileno()

    def read(self, n: int = 4096) -> bytes:
        return self.a.recv(n)

    def write(self, data: bytes) -> None:
        self.a.sendall(data)

    def close(self) -> None:
        for s in (self.a, self.b):
            try:
                s.close()
            except OSError:
                pass


class _ListenServer:
    """A real localhost TCP server that accepts REPEATEDLY (so a reconnect can
    land on a fresh connection) and pushes a fixed prompt on every accept.

    PUBLISH-BEFORE-SEND, and why it is not a style preference
    --------------------------------------------------------
    ``_serve`` registers each accepted socket in ``self.conns`` BEFORE it writes
    the prompt into it.  The obvious order -- send, then register -- opens a
    window in which the test has seen the prompt but the server thread has not
    yet reached the ``append``, and in that window ``close_current()`` found an
    EMPTY list and did nothing at all.  The remote was never killed, the bridge
    had nothing to reconnect from, and the reconnect test burned its whole
    20-second budget before failing on ``assert reconnected`` with no clue why.

    MEASURED (2026-09-11, 32 CPU hogs on a 16-core box, 300 runs of the
    reconnect scenario instrumented to record ``len(srv.conns)`` at the instant
    ``close_current()`` was called): 298 passes, ALL with 1 connection
    registered; 2 failures, BOTH with 0 -- and in both the append had landed by
    the end of the run.  Perfect separation; no other mechanism was implicated,
    and ``ConsoleBridge`` itself was never at fault.

    Registering first makes the window impossible rather than unlikely: the
    prompt bytes cannot reach the peer before the socket is in the list, so any
    test that has observed the prompt is guaranteed to be able to close it.  The
    ``sendall`` is after, and ``close_current`` now refuses to be a silent no-op,
    so a future regression fails immediately and by name instead of timing out.
    ``sendall`` may raise (a peer that closed instantly); the socket stays
    registered, which is correct -- ``close()`` still has to reap it.
    """

    def __init__(self, prompt: bytes = b"HELLO\n") -> None:
        self.prompt = prompt
        self._ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ls.bind(("127.0.0.1", 0))
        self._ls.listen(4)
        self.host, self.port = self._ls.getsockname()
        self.conns: list[socket.socket] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self) -> None:
        self._ls.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # PUBLISH BEFORE SEND -- see the class docstring. list.append is
            # atomic under the GIL, so no lock is needed, only this order.
            self.conns.append(conn)
            try:
                conn.sendall(self.prompt)
            except OSError:
                pass

    def close_current(self) -> None:
        """Kill the most recent connection. NEVER silently a no-op.

        This used to be ``if self.conns:`` and nothing else, so with an empty
        list it returned success having done nothing -- which is precisely how
        the reconnect flake presented (see the class docstring). The publish-
        before-send order makes an empty list impossible here; raising says so
        out loud if that ever stops being true, instead of handing the caller a
        20-second wait and an assertion that names the wrong thing.
        """
        if not self.conns:
            raise AssertionError(
                "_ListenServer.close_current(): no connection has been "
                "registered, so there is nothing to kill and the reconnect "
                "under test cannot happen. _serve() registers each socket "
                "BEFORE sending the prompt, so a caller that has seen the "
                "prompt must find one here.")
        c = self.conns[-1]
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            c.close()
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass
        try:
            self._ls.close()
        except OSError:
            pass
        self._t.join(timeout=1.0)


def _connect(bridge: ConsoleBridge) -> None:
    """Best-effort establish local+remote before pumping. ``pump_once`` is the
    unit-testable step; if the bridge exposes a public ``connect()`` (the first
    line of ``run_forever``) use it, else rely on lazy connect inside
    ``pump_once``."""
    fn = getattr(bridge, "connect", None)
    if callable(fn):
        fn()


def _drain(sock: socket.socket, acc: bytearray) -> None:
    sock.setblocking(False)
    try:
        while True:
            b = sock.recv(4096)
            if not b:
                break
            acc.extend(b)
    except (BlockingIOError, OSError):
        pass


# --------------------------------------------------------------------------- #
# Registry-driven config emitters (kill the hand-maintained host/console/*).
# Substring-only per the critique: NEVER byte-identity.
# --------------------------------------------------------------------------- #


def test_ser2net_yaml_contains_all_console_ports_and_pty_links() -> None:
    out = ser2net_yaml()
    for port in ("6930", "6931", "6932"):
        assert port in out
    for name in ("uart0", "uart1", "swo"):
        assert "/tmp/mps3-console/%s" % name in out
    # ser2net "connection" shape: a pty accepter fed by a tcp connector.
    assert "accepter" in out
    assert "connector" in out


def test_socat_argv_for_each_console_spec() -> None:
    for spec in console_specs():
        argv = socat_argv(spec, DEFAULT_SHELL_HOST)
        assert argv[0] == "socat"
        # matches host/console/socat_consoles.sh exactly.
        assert "PTY,link=/tmp/mps3-console/%s,raw,echo=0" % spec.name in argv
        assert "TCP:%s:%d" % (DEFAULT_SHELL_HOST, spec.default_port) in argv


def test_socat_argv_uart0_is_the_boot_monitor_on_6930() -> None:
    argv = socat_argv(by_name("uart0"), DEFAULT_SHELL_HOST)
    assert argv == [
        "socat", "-d", "-d",
        "PTY,link=/tmp/mps3-console/uart0,raw,echo=0",
        "TCP:%s:6930" % DEFAULT_SHELL_HOST,
    ]


# --------------------------------------------------------------------------- #
# pump_once: both directions, asserted INDEPENDENTLY (the mutation target).
# --------------------------------------------------------------------------- #


def test_pump_copies_both_directions_independently() -> None:
    """remote->local and local->remote are separate, independent assertions.

    MUTATION (documented): deleting the remote->local copy in ``pump_once``
    reddens ONLY the ``R2L`` assertion; the ``L2R`` write-direction assertion
    still passes — proving the two copies are load-bearing and specific.
    """
    local = _PairLocal()
    rem_bridge, rem_test = socket.socketpair()

    def opener(address=None, timeout=None):
        return rem_bridge

    bridge = ConsoleBridge(
        by_name("uart1"), DEFAULT_SHELL_HOST, local,
        opener=opener, retry=_FAST, clock=RealClock(),
    )
    try:
        _connect(bridge)

        # remote -> local
        rem_test.sendall(b"R2L")
        got_local = bytearray()
        stat = None
        for _ in _until(8.0):
            stat = bridge.pump_once(0.05)
            _drain(local.b, got_local)
            if b"R2L" in got_local:
                break
        assert b"R2L" in got_local
        assert isinstance(stat, PumpStat)

        # local -> remote (an INDEPENDENT direction — survives the R2L mutation)
        local.b.sendall(b"L2R")
        got_remote = bytearray()
        for _ in _until(8.0):
            bridge.pump_once(0.05)
            _drain(rem_test, got_remote)
            if b"L2R" in got_remote:
                break
        assert b"L2R" in got_remote
    finally:
        local.close()
        for s in (rem_bridge, rem_test):
            try:
                s.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Full round trip through the genuine pty device + the negative control.
# --------------------------------------------------------------------------- #


def test_round_trip_through_pty_device_with_negative_control() -> None:
    """A full line 'hello' pumped remote->local->DEVICE elicits WORLD_42, which
    is pumped back local->remote. The device answers WORLD_42 ONLY to 'hello'
    (scripts/mps3_console.py:279-288 control) — junk 'goodbye' yields 'unknown',
    never WORLD_42, so a device answering unconditionally could not fake this.
    """
    ctrl_fd, stop, thread = pty_loopback()
    local = _FdLocal(ctrl_fd)
    rem_bridge, rem_test = socket.socketpair()

    def opener(address=None, timeout=None):
        return rem_bridge

    bridge = ConsoleBridge(
        by_name("uart0"), DEFAULT_SHELL_HOST, local,
        opener=opener, retry=_FAST, clock=RealClock(),
    )
    try:
        _connect(bridge)

        # remote sends a full line -> device -> WORLD_42 comes back to remote
        rem_test.sendall(b"hello\r\n")
        acc = bytearray()
        for _ in _until(20.0):
            bridge.pump_once(0.05)
            _drain(rem_test, acc)
            if b"WORLD_42" in acc:
                break
        assert b"WORLD_42" in acc

        # NEGATIVE CONTROL: junk must produce 'unknown', never WORLD_42.
        rem_test.sendall(b"goodbye\r\n")
        acc2 = bytearray()
        for _ in _until(20.0):
            bridge.pump_once(0.05)
            _drain(rem_test, acc2)
            if b"unknown" in acc2:
                break
        assert b"unknown" in acc2
        assert b"WORLD_42" not in acc2
    finally:
        stop.set()
        thread.join(timeout=1.0)
        for s in (rem_bridge, rem_test):
            try:
                s.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Reconnect: kill the remote mid-stream, the bridge reconnects and resumes.
# --------------------------------------------------------------------------- #


def test_reconnect_sets_reconnected_flag_and_resumes_stream() -> None:
    srv = _ListenServer(prompt=b"HELLO\n")
    local = _PairLocal()

    def opener(address=None, timeout=None):
        return socket.create_connection((srv.host, srv.port), timeout=timeout or 1.0)

    bridge = ConsoleBridge(
        by_name("uart0"), DEFAULT_SHELL_HOST, local,
        opener=opener, retry=_FAST, clock=RealClock(),
    )
    try:
        _connect(bridge)

        got = bytearray()
        for _ in _until(8.0):
            bridge.pump_once(0.05)
            _drain(local.b, got)
            if got.count(b"HELLO") >= 1:
                break
        assert b"HELLO" in got  # first prompt reached the local end

        # kill the remote mid-stream
        srv.close_current()

        reconnected = False
        for _ in _until(20.0):
            stat = bridge.pump_once(0.05)
            if stat.reconnected:
                reconnected = True
            _drain(local.b, got)
            if reconnected and got.count(b"HELLO") >= 2:
                break
        assert reconnected              # PumpStat.reconnected flipped
        assert got.count(b"HELLO") >= 2  # the stream resumed after the reconnect
    finally:
        srv.close()
        local.close()
