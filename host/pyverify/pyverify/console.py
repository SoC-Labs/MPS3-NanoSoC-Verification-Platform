"""TCP console reader — ``docs/contracts/net-protocol.md`` ports 6930-6932.

    6930 TCP  UART0 (boot monitor) — raw byte stream
    6931 TCP  UART1 (application)  — raw byte stream
    6932 TCP  SWO/ITM trace        — raw byte stream

Unlike ``client.py``'s control channel, these are **not** JSON-line framed
— net-protocol.md is explicit that they're raw byte streams (the relay
just taps the DUT's AXI-Stream UART/SWO and forwards bytes 1:1). That
means there's no protocol-level parsing to stub out: the only thing
requiring a live shell is actually opening the socket, so this module's
I/O is written for real (same category as ``client.py``'s "real framing +
socket scaffolding" — nothing here needs hardware to be *correct*, only to
be *exercised*).

Used to scrape boot banners / assert on DUT output (spec §11: "The Python
verification library can also consume the console stream directly (assert
on boot banners, scrape test output) without a pseudo-tty").

WRITING (additive, ILA-mint finding #17). Input to the DUT goes the other way
on the same socket, and it must be PACED: the DUT's UART RX has little or no
buffering and no flow control, so line-rate input is mangled into plausible-looking WRONG
commands (board windows 2026-09-23/24). :data:`DEFAULT_PACE_S` (20 ms/char)
never dropped a byte. :meth:`ConsoleReader.write` / :meth:`~ConsoleReader.write_line`
and :func:`send_paced` pace every byte; the CLI is ``pyverify dut-console
--send TEXT [--pace-ms 20]``. Every tool that types at the DUT uses these rather
than its own sleeps.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Callable, Union

__all__ = [
    "UART0_PORT",
    "UART1_PORT",
    "SWO_PORT",
    "ConsoleReader",
    "DEFAULT_PACE_S",
    "send_paced",
]

# net-protocol.md port map.
UART0_PORT = 6930
UART1_PORT = 6931
SWO_PORT = 6932

#: Seconds between two bytes typed at the DUT (finding #17): the DUT's RX has
#: little or no buffering and no flow control; ~20 ms/char never dropped a byte on
#: silicon. (net-protocol.md "Console streams": pacing is the client's job;
#: harnessd's --uart-pace-ms can also enforce it, default off.)
DEFAULT_PACE_S = 0.020


def send_paced(sock: socket.socket, data: bytes, pace_s: float = DEFAULT_PACE_S, *,
               sleep: Callable[[float], None] = time.sleep) -> int:
    """Write ``data`` to a DUT console socket ONE BYTE at a time, ``pace_s``
    after each. Returns the bytes sent. ``pace_s`` 0 = no pacing (a caller who
    knows the far end has flow control)."""
    for i in range(len(data)):
        sock.sendall(data[i:i + 1])
        if pace_s > 0:
            sleep(pace_s)
    return len(data)


@dataclass
class ConsoleReader:
    """Raw byte-stream reader/scraper for one console TCP port.

    Usage::

        with ConsoleReader(host, UART0_PORT) as uart0:
            uart0.assert_contains(b"ZEPH", timeout=30.0)

    ``read_until``/``assert_contains`` buffer everything read so far and
    consume up to (and including) the matched pattern, so repeated calls
    scrape forward through the stream rather than re-matching stale data.
    """

    host: str
    port: int
    timeout: float = 5.0
    #: per-byte pacing for :meth:`write` (additive; see :data:`DEFAULT_PACE_S`)
    pace_s: float = DEFAULT_PACE_S

    def __post_init__(self) -> None:
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    def connect(self) -> "ConsoleReader":
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        return self

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "ConsoleReader":
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_connected(self) -> socket.socket:
        if self._sock is None:
            raise ConnectionError(
                "ConsoleReader is not connected; call connect() or use "
                "'with ConsoleReader(...) as console:'"
            )
        return self._sock

    def write(self, data: bytes, *, pace_s: "float | None" = None,
              sleep: Callable[[float], None] = time.sleep) -> int:
        """Type ``data`` at the DUT, paced (``pace_s``, default this reader's
        :attr:`pace_s`). Returns the bytes sent."""
        sock = self._require_connected()
        sock.settimeout(self.timeout)
        return send_paced(sock, data, self.pace_s if pace_s is None else pace_s, sleep=sleep)

    def write_line(self, text: Union[str, bytes], *, eol: bytes = b"\r",
                   pace_s: "float | None" = None,
                   sleep: Callable[[float], None] = time.sleep) -> int:
        """:meth:`write` ``text`` + ``eol`` (a CR, as a terminal's Enter sends)."""
        data = text.encode() if isinstance(text, str) else bytes(text)
        return self.write(data + eol, pace_s=pace_s, sleep=sleep)

    def read(self, size: int = 4096) -> bytes:
        """Single non-blocking-ish read (bounded by ``self.timeout``); may
        return fewer bytes than ``size``, or raise ``socket.timeout``."""
        sock = self._require_connected()
        sock.settimeout(self.timeout)
        return sock.recv(size)

    def read_until(self, pattern: bytes, *, timeout: float | None = None) -> bytes:
        """Block until ``pattern`` appears in the stream (or timeout).

        Returns everything read up to and including the first occurrence
        of ``pattern``, and consumes it from the internal buffer so the
        next call starts fresh from just after the match.
        """
        sock = self._require_connected()
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while pattern not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"pattern {pattern!r} not seen within timeout; "
                    f"buffered so far: {bytes(self._buf)!r}"
                )
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("console stream closed by shell")
            self._buf.extend(chunk)
        idx = self._buf.find(pattern) + len(pattern)
        result = bytes(self._buf[:idx])
        del self._buf[:idx]
        return result

    def assert_contains(self, pattern: bytes, *, timeout: float | None = None) -> bytes:
        """Like :meth:`read_until`, but raises :class:`AssertionError` (not
        ``TimeoutError``/``ConnectionError``) on failure — convenient in
        test bodies where a plain assert-style failure is wanted.
        """
        try:
            return self.read_until(pattern, timeout=timeout)
        except (TimeoutError, ConnectionError) as exc:
            raise AssertionError(
                f"expected {pattern!r} on {self.host}:{self.port}, not seen: {exc}"
            ) from exc

    @classmethod
    def for_uart0(cls, host: str, **kwargs: object) -> "ConsoleReader":
        return cls(host, UART0_PORT, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def for_uart1(cls, host: str, **kwargs: object) -> "ConsoleReader":
        return cls(host, UART1_PORT, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def for_swo(cls, host: str, **kwargs: object) -> "ConsoleReader":
        return cls(host, SWO_PORT, **kwargs)  # type: ignore[arg-type]
