"""Tests for :class:`pyverify.console.ConsoleReader` — the raw byte-stream
console scraper (net-protocol.md 6930/6931/6932).

The happy banner path is exercised by ``test_fakeshell.py``; this file pins the
**error and edge paths** that were previously unproven:

- ``read_until`` / ``assert_contains`` **timeout** (pattern never arrives);
- ``read_until`` / ``assert_contains`` on a **peer-closed** stream;
- the forward-scraping buffer semantics (consume up to+including the match,
  next call continues from just after it), including a pattern split across
  multiple ``recv`` chunks;
- the not-connected guard and a single ``read`` timing out.

Uses a tiny scripted TCP server on 127.0.0.1 (an ephemeral port) so each
behaviour — send-and-hold, send-then-close — is deterministic without a board.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from pyverify.console import (
    SWO_PORT,
    UART0_PORT,
    UART1_PORT,
    ConsoleReader,
)


# --------------------------------------------------------------------------- #
# A one-shot scripted TCP server.
# --------------------------------------------------------------------------- #

class _MiniServer:
    """Accepts a single connection and runs ``handler(conn, stop)`` in a
    daemon thread. ``stop`` is set on teardown so a holding handler releases
    immediately (fast, deterministic teardown)."""

    def __init__(self, handler):
        self._handler = handler
        self.stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            self._handler(conn, self.stop)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def _hold(conn, stop):
    """Send nothing; keep the connection open until teardown."""
    stop.wait(2.0)


def _send_then_hold(data: bytes):
    def handler(conn, stop):
        conn.sendall(data)
        stop.wait(2.0)
    return handler


def _send_then_close(data: bytes):
    def handler(conn, stop):
        conn.sendall(data)
        # returning closes the connection (EOF to the client)
    return handler


def _send_chunks(chunks, gap: float = 0.05):
    def handler(conn, stop):
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(gap)
            conn.sendall(chunk)
        stop.wait(2.0)
    return handler


# --------------------------------------------------------------------------- #
# Buffer / forward-scraping semantics (happy)
# --------------------------------------------------------------------------- #

def test_read_until_consumes_up_to_and_including_and_scrapes_forward():
    with _MiniServer(_send_then_hold(b"hello world\nZEPHYR boot\n")) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=2.0) as console:
            first = console.read_until(b"\n")
            assert first == b"hello world\n"
            # the next call continues from just after the first match
            second = console.read_until(b"boot")
            assert second == b"ZEPHYR boot"


def test_read_until_matches_pattern_split_across_recv_chunks():
    with _MiniServer(_send_chunks([b"ZE", b"PH", b"YR\n"])) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=2.0) as console:
            assert console.read_until(b"ZEPHYR") == b"ZEPHYR"


def test_assert_contains_returns_match_on_success():
    with _MiniServer(_send_then_hold(b"...nanosoc boot\n")) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=2.0) as console:
            got = console.assert_contains(b"nanosoc")
            assert got.endswith(b"nanosoc")


# --------------------------------------------------------------------------- #
# Timeout paths
# --------------------------------------------------------------------------- #

def test_read_until_timeout_raises_timeouterror():
    """The deadline-expiry branch (``remaining <= 0`` -> the module's own
    ``TimeoutError``). Uses ``timeout=0.0`` so the branch fires deterministically
    on the first loop turn, independent of peer timing and of the Python
    version's ``socket.timeout`` vs ``TimeoutError`` distinction (a silent peer
    instead surfaces ``socket.timeout`` out of ``recv`` — a separate quirk)."""
    with _MiniServer(_hold) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=2.0) as console:
            with pytest.raises(TimeoutError) as exc:
                console.read_until(b"NEVER", timeout=0.0)
            assert "NEVER" in str(exc.value)  # the diagnostic names the pattern


def test_assert_contains_timeout_raises_assertionerror():
    """assert_contains wraps read_until's TimeoutError as an AssertionError
    naming the pattern and the host:port."""
    with _MiniServer(_hold) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=2.0) as console:
            with pytest.raises(AssertionError) as exc:
                console.assert_contains(b"BANNER", timeout=0.0)
    msg = str(exc.value)
    assert "BANNER" in msg
    assert f"{srv.host}:{srv.port}" in msg


# --------------------------------------------------------------------------- #
# Peer-closed paths
# --------------------------------------------------------------------------- #

def test_read_until_peer_close_raises_connectionerror():
    with _MiniServer(_send_then_close(b"partial output, then EOF")) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=2.0) as console:
            with pytest.raises(ConnectionError, match="closed by shell"):
                console.read_until(b"NOT-PRESENT")


def test_assert_contains_peer_close_raises_assertionerror():
    with _MiniServer(_send_then_close(b"partial")) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=2.0) as console:
            with pytest.raises(AssertionError, match="not seen"):
                console.assert_contains(b"NOT-PRESENT")


# --------------------------------------------------------------------------- #
# Not-connected guard + single read timeout
# --------------------------------------------------------------------------- #

def test_read_and_read_until_before_connect_raise():
    console = ConsoleReader("127.0.0.1", 6930, timeout=0.5)
    with pytest.raises(ConnectionError, match="not connected"):
        console.read_until(b"x")
    with pytest.raises(ConnectionError, match="not connected"):
        console.read()


def test_single_read_times_out():
    with _MiniServer(_hold) as srv:
        with ConsoleReader(srv.host, srv.port, timeout=0.3) as console:
            with pytest.raises(socket.timeout):
                console.read()


def test_classmethod_helpers_pick_the_right_ports():
    assert ConsoleReader.for_uart0("h").port == UART0_PORT
    assert ConsoleReader.for_uart1("h").port == UART1_PORT
    assert ConsoleReader.for_swo("h").port == SWO_PORT
