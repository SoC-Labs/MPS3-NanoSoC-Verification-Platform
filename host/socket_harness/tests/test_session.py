"""Line/byte reassembly, reconnect, and the not-connected / peer-close guards
for :mod:`socket_harness.session` — with ZERO real sockets.

Every session is driven through the injected ``opener`` seam using
:class:`socket_harness.loopback.FakeSocket` / :func:`fake_opener`, so the
framing, buffering, reconnect and error mapping are all proven board-free
(the same DI discipline as ``pyverify`` ``tests/test_client.py`` /
``tests/test_console.py``, which this module generalises to the base
``Session`` spine).

The load-bearing interop assertion here is that a :class:`LineSession` is
STRUCTURALLY a ``pyverify.client.Transport`` — that identity is the keystone
that lets ``SocketHarness.shell()`` hand a reconnecting session straight to
``ShellClient`` with no adapter.
"""
from __future__ import annotations

import pytest

from socket_harness.endpoints import Address
from socket_harness.session import (
    EndpointClosed,
    LineSession,
    NotConnected,
    RawStreamSession,
)
from socket_harness.loopback import FakeSocket, fake_opener

from pyverify.client import Transport as PyverifyTransport


# --------------------------------------------------------------------------- #
# LineSession — JSON-line reassembly (byte-identical to
# pyverify.client.SocketTransport.recv_line)
# --------------------------------------------------------------------------- #


def _line_session(*scripts: list[bytes]) -> LineSession:
    sess = LineSession(Address(host="shell.example", port=6900), opener=fake_opener(*scripts))
    sess.connect()
    return sess


def test_recv_line_reassembles_one_line_split_across_two_recv_chunks() -> None:
    # A single logical line arrives as two separate recv() chunks; the newline
    # frame delimiter only completes on the second read.
    sess = _line_session([b"hel", b"lo\n"])
    assert sess.recv_line() == b"hello"


def test_recv_line_returns_two_buffered_lines_one_per_call() -> None:
    # Two whole lines land in ONE recv chunk; the second must come from the
    # retained buffer WITHOUT a further read (partition-on-\n, keep remainder —
    # the exact SocketTransport.recv_line contract).
    sess = _line_session([b"foo\nbar\n"])
    assert sess.recv_line() == b"foo"
    assert sess.recv_line() == b"bar"


def test_recv_line_strips_the_newline_delimiter() -> None:
    # The newline is the frame delimiter, never part of the payload.
    sess = _line_session([b'{"op":"ping"}\n'])
    assert sess.recv_line() == b'{"op":"ping"}'


def test_recv_line_raises_endpoint_closed_on_peer_half_close() -> None:
    # recv() -> b'' with no complete line buffered is a peer half-close, which
    # maps to EndpointClosed (a ConnectionError subclass) — NOT a hang.
    sess = _line_session([b"partial-no-newline"])
    with pytest.raises(EndpointClosed):
        sess.recv_line()


# --------------------------------------------------------------------------- #
# RawStreamSession.read_until — forward-scraping byte stream
# (algorithm lifted from pyverify.console.ConsoleReader.read_until)
# --------------------------------------------------------------------------- #


def _stream_session(*scripts: list[bytes], auto_reconnect: bool = False) -> RawStreamSession:
    sess = RawStreamSession(
        Address(host="shell.example", port=6930),
        opener=fake_opener(*scripts),
        auto_reconnect=auto_reconnect,
    )
    sess.connect()
    return sess


def test_read_until_forward_scrapes_consuming_up_to_and_including_match() -> None:
    # Consume up to+including the first match; the NEXT call resumes after it
    # (repeated scrapes walk forward through the stream, never re-matching
    # stale bytes).
    sess = _stream_session([b"hello MARKER world MARKER end"])
    assert sess.read_until(b"MARKER") == b"hello MARKER"
    assert sess.read_until(b"MARKER") == b" world MARKER"


def test_read_until_matches_a_pattern_split_across_chunks() -> None:
    # The pattern straddles two recv() chunks; it must only match once the
    # buffer has been reassembled across the boundary.
    sess = _stream_session([b"aa MA", b"RK bb"])
    assert sess.read_until(b"MARK") == b"aa MARK"


def test_read_until_raises_endpoint_closed_on_peer_close() -> None:
    # recv() -> b'' before the pattern arrives is a peer close: EndpointClosed,
    # not a silent truncation.
    sess = _stream_session([b"partial"])
    with pytest.raises(EndpointClosed):
        sess.read_until(b"ZZZ", timeout=1.0)


def test_read_until_raises_timeout_error_on_deadline_and_does_not_hang() -> None:
    """NEGATIVE CONTROL (the mps3_console.py:265 timeout proof, generalised):
    a pattern that never arrives must raise TimeoutError bounded by the injected
    timeout, not block forever.

    A peer that keeps delivering bytes that never contain the pattern drives the
    DEADLINE branch directly (rather than a recv-exception, whose mapping is
    version/implementation dependent — socket.timeout is only TimeoutError on
    py3.10+). The forward-scan self-throttles the buffer, so this is bounded by
    the injected 0.05s, never a hang."""

    class _StarvingSock:
        def __init__(self) -> None:
            self.sent = bytearray()

        def settimeout(self, t: float | None) -> None:
            pass

        def sendall(self, b: bytes) -> None:
            self.sent += b

        def recv(self, n: int = 4096) -> bytes:
            return b"."  # never contains the sought pattern

        def shutdown(self, how: int) -> None:
            pass

        def close(self) -> None:
            pass

    def _opener(_addr, timeout=None):  # type: ignore[no-untyped-def]
        return _StarvingSock()

    sess = RawStreamSession(Address(host="h", port=6930), opener=_opener)
    sess.connect()
    with pytest.raises(TimeoutError):
        sess.read_until(b"NEVER_APPEARS", timeout=0.05)


def test_read_returns_a_chunk_after_connect() -> None:
    sess = _stream_session([b"chunk-one"])
    assert sess.read(4096) == b"chunk-one"


# --------------------------------------------------------------------------- #
# auto_reconnect — a peer close mid-stream is recovered transparently
# --------------------------------------------------------------------------- #


def test_auto_reconnect_resumes_read_until_after_a_peer_close() -> None:
    # First connection closes immediately (empty script -> recv b''); with
    # auto_reconnect the session reconnects to the SECOND scripted socket and
    # resumes the scrape. Proves fake_opener hands out sockets in order across
    # successive opener() calls.
    sess = _stream_session([], [b"AA MARK BB"], auto_reconnect=True)
    assert sess.read_until(b"MARK", timeout=1.0) == b"AA MARK"


# --------------------------------------------------------------------------- #
# NotConnected guard — no I/O before connect()
# --------------------------------------------------------------------------- #


def test_recv_line_before_connect_raises_not_connected() -> None:
    sess = LineSession(Address(host="h", port=6900), opener=fake_opener([b"x\n"]))
    with pytest.raises(NotConnected):
        sess.recv_line()


def test_read_before_connect_raises_not_connected() -> None:
    sess = RawStreamSession(Address(host="h", port=6930), opener=fake_opener([b"x"]))
    with pytest.raises(NotConnected):
        sess.read(16)


# --------------------------------------------------------------------------- #
# The interop keystone: LineChannel is STRUCTURALLY pyverify's Transport
# --------------------------------------------------------------------------- #


def test_line_session_is_structurally_a_pyverify_transport() -> None:
    # runtime_checkable Protocol: LineSession exposes send_line/recv_line/close
    # with the identical shape, so ShellClient(transport=LineSession(...)) needs
    # NO adapter. This is the drop-in keystone.
    sess = LineSession(Address(host="h", port=6900), opener=fake_opener([b"x\n"]))
    assert isinstance(sess, PyverifyTransport)


def test_send_line_appends_the_newline_frame_delimiter() -> None:
    sess = _line_session([b"x\n"])
    sess.send_line(b'{"op":"ping"}')
    # FakeSocket records everything written on its `sent` bytearray; the frame
    # delimiter is appended by the session, not the caller.
    assert isinstance(sess._sock, FakeSocket)
    assert bytes(sess._sock.sent) == b'{"op":"ping"}\n'
