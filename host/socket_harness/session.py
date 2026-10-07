"""socket_harness.session — session lifecycle, channel protocols, concrete sessions.

The shared spine every endpoint rides on. It defines:

* three structural channel Protocols — :class:`LineChannel` (JSON-line
  request/response over TCP 6900), :class:`ByteChannel` (raw console stream /
  tty), and :class:`RegisterBackend` (word-addressed CSR access, implemented
  by :mod:`socket_harness.xsdb` and driven by :mod:`socket_harness.registers`);
* an abstract :class:`Session` owning connect / reconnect / close, with an
  injected ``opener`` + :class:`~socket_harness.retry.RetryPolicy` + ``clock``
  so every proof runs against in-process fakes with **zero real sockets and no
  board**;
* the concrete :class:`LineSession`, :class:`RawStreamSession` and
  :class:`SerialSession`.

INTEROP KEYSTONE
----------------
:class:`LineChannel` is **deliberately byte-identical** to
``pyverify.client.Transport`` (the same three methods: ``send_line(bytes) ->
None``, ``recv_line() -> bytes``, ``close() -> None``). A :class:`LineSession`
is therefore a drop-in ``ShellClient(host, transport=...)`` with **no
adapter** — the harness gets ping/swap/set_clk/macgen/diag over a
*reconnecting* session for free. Because both are ``runtime_checkable``
Protocols, ``isinstance(LineSession(...), pyverify.client.Transport)`` is True
(structural — method names only). ``test_session.py`` pins that invariant.

PROVENANCE OF THE I/O ALGORITHMS
--------------------------------
The recv/reassembly and forward-scrape algorithms are lifted (in behaviour)
from ``pyverify.client.SocketTransport.recv_line`` and
``pyverify.console.ConsoleReader.read_until``; the tty path adopts
``scripts/mps3_console.py``'s ``Console`` — raw 8N1, ``VMIN=VTIME=0``, and
char-paced writes (default 4 ms/char, env ``MPS3_CONSOLE_PACE``) to survive the
shallow uartlite RX FIFO that has no flow control.

No I/O happens at import time.
"""
from __future__ import annotations

import abc
import os
import select
import socket
import termios
import time
from typing import Any, Callable, Protocol, runtime_checkable

from .endpoints import Address
from .retry import Clock, RealClock, RetryPolicy, connect_with_retry

__all__ = [
    "LineChannel",
    "ByteChannel",
    "RegisterBackend",
    "Session",
    "SocketSession",
    "LineSession",
    "RawStreamSession",
    "SerialSession",
    "EndpointClosed",
    "NotConnected",
]

# Opener seam: a callable that acquires the underlying transport. The default
# is ``socket.create_connection`` and is called as
# ``opener((addr.host, addr.port), timeout=self.timeout)``. Tests inject a
# ``fake_opener`` (see ``socket_harness.loopback``) so no real socket is ever
# opened — mirrors pyverify's Transport dependency-injection seam.
OpenerT = Callable[..., Any]

# tty char-pacing — adopted from scripts/mps3_console.py (env-overridable but
# read per-instance, never at import time, to keep the module import pure).
_PACE_ENV = "MPS3_CONSOLE_PACE"
_DEFAULT_PACE_S = 0.004  # scripts/mps3_console.py DEFAULT_PACE_S (the handover figure)
_BAUD_ENV = "MPS3_CONSOLE_BAUD"
_DEFAULT_BAUD = 115200

# scripts/mps3_console.py::_BAUD_CONST — the supported termios baud constants.
_BAUD_CONST = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class NotConnected(RuntimeError):
    """A channel op was attempted before :meth:`Session.connect` (or after close)."""


class EndpointClosed(ConnectionError):
    """The peer half-closed the stream (``recv``/``os.read`` returned ``b''``).

    A subclass of :class:`ConnectionError` **on purpose**: this lets
    :func:`socket_harness.retry.connect_with_retry` catch it via its plain
    ``(OSError, ConnectionError)`` clause without importing this module — the
    documented way that ``retry.py`` stays free of a ``session`` import cycle.
    """


# --------------------------------------------------------------------------- #
# Channel protocols (the three shapes every endpoint speaks)
# --------------------------------------------------------------------------- #


@runtime_checkable
class LineChannel(Protocol):
    """Line-oriented transport — one ``\\n``-terminated frame per call.

    DELIBERATELY byte-identical to ``pyverify.client.Transport`` so a
    :class:`LineSession` *is* a ``ShellClient`` transport with no adapter (the
    interop keystone; see the module docstring). Do not add, rename, or reorder
    methods here without changing ``pyverify.client.Transport`` in lockstep.
    """

    def send_line(self, payload: bytes) -> None: ...

    def recv_line(self) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class ByteChannel(Protocol):
    """Raw byte stream — the console (6930-6932) and serial tty shape."""

    def read(self, n: int = 4096) -> bytes: ...

    def read_until(self, pattern: bytes, *, timeout: float | None = None) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class RegisterBackend(Protocol):
    """Word-addressed CSR/register access.

    Implemented for real by :class:`socket_harness.xsdb.XsdbRegisterEndpoint`
    (over xsdb/hw_server) and by in-process fakes in tests; consumed by
    :class:`socket_harness.registers.RegisterAccess`.
    """

    def read_word(self, addr: int) -> int: ...

    def write_word(self, addr: int, val: int) -> None: ...

    def read_block(self, addr: int, nwords: int) -> list[int]: ...


# --------------------------------------------------------------------------- #
# Session base
# --------------------------------------------------------------------------- #


class Session(abc.ABC):
    """Connect / reconnect / close lifecycle shared by every concrete session.

    The ``opener`` (transport factory), ``clock`` and ``retry`` policy are all
    injected, so the reconnect spine is exercised end-to-end with fakes and
    without a board. :meth:`connect` runs the opener through
    :func:`~socket_harness.retry.connect_with_retry`; subclasses only implement
    :meth:`_wrap` (install their buffering + stash the raw transport).
    """

    def __init__(
        self,
        addr: Address,
        *,
        timeout: float = 5.0,
        opener: OpenerT = socket.create_connection,
        clock: Clock = RealClock(),
        retry: RetryPolicy = RetryPolicy(),
        auto_reconnect: bool = False,
    ) -> None:
        self._addr = addr
        self._timeout = timeout
        self._opener = opener
        self._clock = clock
        self._retry = retry
        self._auto_reconnect = auto_reconnect
        # ``_raw`` is the base bookkeeping handle (socket or fd); ``_buf`` always
        # exists so the channel ops can be called (and fail cleanly with
        # NotConnected) before connect().
        self._raw: Any = None
        self._buf = bytearray()
        self._connected = False

    # -- introspection ----------------------------------------------------- #

    @property
    def addr(self) -> Address:
        return self._addr

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _peer(self) -> str:
        """Human label for error messages."""
        if self._addr.device:
            return self._addr.device
        return f"{self._addr.host}:{self._addr.port}"

    # -- lifecycle --------------------------------------------------------- #

    def _open(self) -> Any:
        """Acquire the raw transport (retried by :meth:`connect`).

        Default: the socket path — ``opener((host, port), timeout=...)``.
        :class:`SerialSession` overrides this to ``os.open`` the tty so a busy
        device is retried inside the same backoff envelope.
        """
        return self._opener((self._addr.host, self._addr.port), timeout=self._timeout)

    def connect(self) -> "Session":
        """Open (with backoff) and wrap the transport. Idempotent."""
        if self._connected:
            return self
        raw = connect_with_retry(self._open, self._retry, self._clock)
        self._wrap(raw)
        self._connected = True
        return self

    def reconnect(self) -> "Session":
        """``close()`` then ``connect()``.

        NOTE: :meth:`_wrap` installs a *fresh* read buffer, so a reconnect
        DISCARDS any partially-buffered bytes. That is deliberate — a dropped
        connection loses its in-flight bytes and they cannot be recovered; a
        caller that reconnects mid-line/mid-scrape resumes from scratch on the
        new connection.
        """
        self.close()
        return self.connect()

    def close(self) -> None:
        if not self._connected:
            return
        try:
            self._teardown()
        finally:
            self._raw = None
            self._connected = False

    def __enter__(self) -> "Session":
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- subclass seam ----------------------------------------------------- #

    @abc.abstractmethod
    def _wrap(self, raw: Any) -> None:
        """Install per-subclass buffering and stash ``raw`` (into ``self._raw``)."""

    def _teardown(self) -> None:
        """Release the raw transport. Default closes anything with ``.close()``
        (sockets); :class:`SerialSession` overrides for ``os.close`` on the fd."""
        raw = self._raw
        if raw is not None and hasattr(raw, "close"):
            raw.close()

    def _require(self) -> Any:
        """Return the live transport or raise :class:`NotConnected`."""
        if not self._connected or self._raw is None:
            raise NotConnected(
                f"session to {self._peer()} is not connected; call connect() "
                "or use it as a context manager"
            )
        return self._raw


# --------------------------------------------------------------------------- #
# Socket sessions
# --------------------------------------------------------------------------- #


class SocketSession(Session):
    """Common base for the TCP sessions: a socket + a reassembly ``bytearray``."""

    def _wrap(self, sock: Any) -> None:
        self._raw = self._sock = sock
        self._buf = bytearray()

    def fileno(self) -> int:
        """Underlying socket fd — lets a bridge ``select()`` on this session."""
        return self._require().fileno()

    def _recv_more(self, timeout: float | None) -> bytes:
        """One bounded ``recv``; the auto-reconnect-aware recv primitive.

        Raises :class:`EndpointClosed` when the peer half-closes (``recv`` ->
        ``b''``). When ``auto_reconnect`` is set, a half-close triggers ONE
        ``reconnect()`` + retry before giving up — so a scraping caller
        (:meth:`recv_line`, :meth:`read_until`) resumes on the fresh
        connection. A ``recv`` *timeout* is NOT a close and propagates as
        ``socket.timeout`` (== ``TimeoutError`` on 3.10+) for the caller's
        deadline logic to handle.
        """
        sock = self._require()
        sock.settimeout(timeout)
        chunk = sock.recv(4096)
        if chunk:
            return chunk
        if self._auto_reconnect:
            self.reconnect()
            sock = self._require()
            sock.settimeout(timeout)
            chunk = sock.recv(4096)
            if chunk:
                return chunk
        raise EndpointClosed(f"{self._peer()} closed by peer")


class LineSession(SocketSession):
    """:class:`LineChannel` over TCP — the ``ShellClient`` transport twin.

    ``send_line``/``recv_line``/``close`` match ``pyverify.client.Transport``
    exactly, so ``ShellClient(host, transport=LineSession(...))`` works with no
    shim. The reassembly loop is
    ``pyverify.client.SocketTransport.recv_line``, extended only with the
    injected reconnect via :meth:`_recv_more`.
    """

    def send_line(self, payload: bytes) -> None:
        self._sendall(payload + b"\n")

    def _sendall(self, data: bytes) -> None:
        sock = self._require()
        try:
            sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A write onto a peer that dropped an idle control connection: when
            # asked to auto-reconnect, re-open and resend ONCE (the shell closes
            # idle 6900 sockets — this is what makes the LineSession a genuinely
            # "reconnecting" ShellClient transport). Otherwise surface it.
            if self._auto_reconnect:
                self.reconnect()
                self._require().sendall(data)
                return
            raise

    def recv_line(self) -> bytes:
        # pyverify.client.SocketTransport.recv_line: buffer partial reads,
        # return one line, keep the remainder for the next call.
        while b"\n" not in self._buf:
            # Call _recv_more() FIRST (it may reconnect and rebind self._buf to a
            # fresh bytearray), THEN look up self._buf and extend it — never
            # ``self._buf.extend(self._recv_more(...))``, which would bind
            # ``.extend`` to the pre-reconnect (orphaned) buffer.
            chunk = self._recv_more(self._timeout)
            self._buf.extend(chunk)
        idx = self._buf.find(b"\n")
        line = bytes(self._buf[:idx])
        del self._buf[: idx + 1]  # drop the line and its delimiter
        return line


class RawStreamSession(SocketSession):
    """:class:`ByteChannel` over a raw TCP console stream (6930/6931/6932)."""

    def read(self, n: int = 4096) -> bytes:
        """One ``settimeout``'d ``recv`` (draining any buffered remainder first).

        Unlike :meth:`read_until`, ``read`` does NOT auto-reconnect: it raises
        :class:`EndpointClosed` on a half-close so
        :meth:`socket_harness.console_bridge.ConsoleBridge.pump_once` can
        *observe* the drop and count the reconnect (``PumpStat.reconnected``).
        """
        if self._buf:
            data = bytes(self._buf[:n])
            del self._buf[:n]
            return data
        sock = self._require()
        sock.settimeout(self._timeout)
        chunk = sock.recv(n)
        if not chunk:
            raise EndpointClosed(f"{self._peer()} closed by peer")
        return chunk

    def read_until(self, pattern: bytes, *, timeout: float | None = None) -> bytes:
        """Forward-scrape until ``pattern`` appears, then consume up to+including it.

        pyverify.console.ConsoleReader.read_until: accumulate into ``self._buf``,
        return everything up to and including the first match, and keep the
        remainder so the next call continues *after* the match. Raises
        :class:`EndpointClosed` on a peer close (unless ``auto_reconnect``, in
        which case :meth:`_recv_more` reconnects and the scrape resumes) and
        ``TimeoutError`` when the deadline passes.
        """
        self._require()
        limit = timeout if timeout is not None else self._timeout
        # Real wall time, NOT self._clock: this deadline bounds a blocking recv,
        # which consumes real time. The injected clock is for retry backoff /
        # write pacing (a FakeClock only advances on sleep(), so it would freeze
        # this loop forever). Matches pyverify.console.ConsoleReader.read_until.
        deadline = time.monotonic() + limit
        while pattern not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"pattern {pattern!r} not seen on {self._peer()} within "
                    f"{limit}s; buffered so far: {bytes(self._buf)!r}"
                )
            try:
                chunk = self._recv_more(remaining)
            except (socket.timeout, TimeoutError):
                # A recv deadline, not a close — re-check the wall deadline.
                continue
            # Extend AFTER the call: _recv_more may have reconnected and rebound
            # self._buf to a fresh bytearray, so bind ``.extend`` to it now.
            self._buf.extend(chunk)
        idx = self._buf.find(pattern) + len(pattern)
        result = bytes(self._buf[:idx])
        del self._buf[:idx]
        return result

    def write(self, data: bytes) -> None:
        self._require().sendall(data)


# --------------------------------------------------------------------------- #
# Serial session (tty)
# --------------------------------------------------------------------------- #


class SerialSession(Session):
    """:class:`ByteChannel` over a serial tty (e.g. the MPS3 MCC ``/dev/ttyUSB6``).

    Adopts ``scripts/mps3_console.py``'s ``Console``: raw 8N1 via
    ``os.open(dev, O_RDWR|O_NOCTTY|O_NONBLOCK)`` + a cfmakeraw-equivalent
    termios config (``VMIN=VTIME=0``), reads via ``select``+``os.read``, and
    **char-paced** writes (default 4 ms/char, env ``MPS3_CONSOLE_PACE``) — the
    guard against overrunning the shallow, flow-control-less uartlite RX FIFO,
    which otherwise silently mangles a command and reads like a wiring fault.

    The socket ``opener`` is unused here; ``addr.device`` names the tty.
    """

    def __init__(
        self,
        addr: Address,
        *,
        pace_s: float | None = None,
        baud: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(addr, **kwargs)
        # Read env defaults per-instance (never at import) to keep imports pure.
        if pace_s is None:
            pace_s = float(os.environ.get(_PACE_ENV, _DEFAULT_PACE_S))
        if baud is None:
            baud = int(os.environ.get(_BAUD_ENV, _DEFAULT_BAUD))
        self._pace_s = pace_s
        self._baud = baud
        self._fd: int | None = None

    def _open(self) -> int:
        device = self._addr.device
        if not device:
            # A config error, not a transient — raise NotConnected (a
            # RuntimeError) so connect_with_retry does NOT swallow/retry it.
            raise NotConnected("SerialSession requires addr.device (the tty path)")
        return os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    def _wrap(self, fd: int) -> None:
        self._raw = self._fd = fd
        self._buf = bytearray()
        self._configure_raw(fd)

    def _configure_raw(self, fd: int) -> None:
        """Raw 8N1, no flow control — scripts/mps3_console.py::Console.raw."""
        if not os.isatty(fd):
            return  # a plain pipe/fd needs no termios (already byte-transparent)
        if self._baud not in _BAUD_CONST:
            raise ValueError(
                f"unsupported baud {self._baud}; known: {sorted(_BAUD_CONST)}"
            )
        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(fd)
        # cfmakeraw equivalent: no canonical mode, no echo, no signal chars, no
        # CR/LF translation, no parity/xon-xoff mangling of the byte stream.
        iflag &= ~(
            termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
            | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
        )
        oflag &= ~termios.OPOST
        lflag &= ~(
            termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
            | termios.IEXTEN
        )
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL  # CLOCAL: ignore modem lines
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        speed = _BAUD_CONST[self._baud]
        termios.tcsetattr(
            fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc]
        )

    def _teardown(self) -> None:
        fd = self._raw
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def fileno(self) -> int:
        """The tty fd — lets a bridge ``select()`` on this session."""
        return self._require()

    def read(self, n: int = 4096) -> bytes:
        if self._buf:
            data = bytes(self._buf[:n])
            del self._buf[:n]
            return data
        fd = self._require()
        r, _, _ = select.select([fd], [], [], self._timeout)
        if not r:
            raise TimeoutError(f"no data from {self._peer()} within {self._timeout}s")
        try:
            chunk = os.read(fd, n)
        except (BlockingIOError, InterruptedError):
            return b""  # transient (readable then EAGAIN) — nothing to forward
        if not chunk:
            raise EndpointClosed(f"{self._peer()} (tty) hung up")
        return chunk

    def read_until(self, pattern: bytes, *, timeout: float | None = None) -> bytes:
        # scripts/mps3_console.py::Console._pump — select + os.read into a
        # rolling buffer, consume up to+including the match (bytes, not regex).
        fd = self._require()
        limit = timeout if timeout is not None else self._timeout
        # Real wall time (see RawStreamSession.read_until) — this loop blocks on
        # select()/os.read(), so its deadline tracks real elapsed time.
        deadline = time.monotonic() + limit
        while pattern not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"pattern {pattern!r} not seen on {self._peer()} within "
                    f"{limit}s; buffered so far: {bytes(self._buf)!r}"
                )
            r, _, _ = select.select([fd], [], [], min(0.2, remaining))
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            if not chunk:
                raise EndpointClosed(f"{self._peer()} (tty) hung up")
            self._buf.extend(chunk)
        idx = self._buf.find(pattern) + len(pattern)
        result = bytes(self._buf[:idx])
        del self._buf[:idx]
        return result

    def write(self, data: bytes) -> None:
        """Char-paced write: one byte, ``pace_s`` apart, via the injected clock.

        Byte-at-a-time is the whole point — a single ``os.write`` of the same
        string is what overruns the uartlite RX FIFO. Pacing through
        ``self._clock.sleep`` keeps it deterministic under a fake clock.
        """
        fd = self._require()
        for i in range(len(data)):
            os.write(fd, data[i : i + 1])
            if self._pace_s > 0:
                self._clock.sleep(self._pace_s)
