"""Reusable board-free test doubles for the socket harness.

These are the fakes that let every proof in the package run with **zero real
sockets to a board and zero external processes** — the same discipline the
pyverify tests already follow, promoted here into one importable place so the
CLI ``selftest`` and the ``tests/`` suite share exactly one set of doubles.

What lives here (all pure Python, deterministic, no import-time I/O):

* :class:`FakeSocket` / :func:`fake_opener` — an in-memory socket-like plus an
  opener seam, so :class:`~socket_harness.session.Session` /
  :class:`~socket_harness.console_bridge.ConsoleBridge` never touch the network.
  Mirrors ``pyverify`` ``tests/test_client.py``'s ``FakeTransport`` dependency
  injection.
* :class:`FakeLineServer` — a JSON-line responder (promotes pyverify's
  ``FakeTransport`` to a reusable server). Usable **both** as a drop-in pyverify
  ``Transport`` *and* behind a real :class:`LineSession` over
  ``socket.socketpair()`` — the interop keystone proof.
* :class:`FakeByteServer` — a localhost scripted TCP server (promotes
  ``_MiniServer`` from pyverify ``tests/test_console.py``) with send-and-hold /
  send-then-close handlers for the byte-stream/console tests. Loopback only
  (127.0.0.1), never a board.
* :class:`FakeXsdbRunner` — a ``SubprocessRunner``-shaped callable that answers
  ``mrd``/``mwr`` from a ``{addr: value}`` dict, supports the ascending
  LMB-alias magic scan, and a wrong-magic / exit-1 negative-control mode for
  the ``TargetNotFound`` path. Never launches a subprocess.
* :class:`FakeClock` — a virtual monotonic clock for the deterministic retry
  backoff assertions.
* :func:`pty_loopback` — the ``os.openpty()`` + fake-device rig lifted verbatim
  from ``scripts/mps3_console.py``'s ``_self_test`` (``hello`` -> ``WORLD_42``,
  anything else -> ``unknown``; the negative control is baked in).
"""
from __future__ import annotations

import collections
import json
import os
import re
import select
import socket
import subprocess
import termios
import threading

__all__ = [
    "FakeSocket",
    "fake_opener",
    "FakeLineServer",
    "FakeByteServer",
    "FakeXsdbRunner",
    "FakeClock",
    "pty_loopback",
    "CLOSE",
    "TIMEOUT",
]


# --------------------------------------------------------------------------- #
# Script sentinels for FakeSocket recv scripting.
# --------------------------------------------------------------------------- #
class _Sentinel:
    """A named, identity-comparable script marker (not data)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<loopback.{self.name}>"


#: In a FakeSocket script, ``recv`` returns ``b''`` at this point (peer close).
CLOSE = _Sentinel("CLOSE")
#: In a FakeSocket script, ``recv`` raises ``socket.timeout`` at this point.
TIMEOUT = _Sentinel("TIMEOUT")


def _raise_timeout() -> "bytes":
    # socket.timeout IS TimeoutError on py>=3.10, so read_until's deadline
    # branch and this raise land on the same exception type by design.
    raise socket.timeout("FakeSocket scripted timeout")


# --------------------------------------------------------------------------- #
# FakeSocket + fake_opener — the socket-level DI seam.
# --------------------------------------------------------------------------- #
class FakeSocket:
    """An in-memory ``socket``-like with a scripted ``recv`` queue and a
    ``sent`` byte log.

    ``recv`` pops the next scripted item:

    * ``bytes``           -> returned (respecting ``n`` — a longer chunk is
                             split and the remainder pushed back, like a real
                             ``recv``);
    * ``b''`` / :data:`CLOSE` -> returns ``b''`` (a peer half-close, which the
                             sessions map to ``EndpointClosed``);
    * :data:`TIMEOUT`     -> raises ``socket.timeout``;
    * an ``Exception`` instance/class -> raised (e.g. ``socket.timeout()`` for a
                             deadline test).

    When the queue is exhausted the socket falls back to ``on_exhaust`` (default
    ``b''`` = peer close). Set ``on_exhaust=TIMEOUT`` for a socket that keeps
    "timing out" so a ``read_until`` hits its deadline deterministically.
    """

    def __init__(
        self,
        script=None,
        *,
        on_exhaust: object = b"",
        peername=("fake", 0),
    ) -> None:
        self._queue: "collections.deque" = collections.deque()
        self.extend(script)
        self.sent = bytearray()
        self.on_exhaust = on_exhaust
        self.closed = False
        self.timeout = None
        self.shutdown_how = None
        self._peername = peername

    # -- scripting --------------------------------------------------------- #
    def extend(self, script) -> "FakeSocket":
        """Append more scripted items (bytes / sentinels / exceptions)."""
        if script is None:
            return self
        if isinstance(script, (bytes, bytearray)):
            self._queue.append(bytes(script))
            return self
        for item in script:
            self._queue.append(item)
        return self

    # -- socket surface ---------------------------------------------------- #
    def settimeout(self, t) -> None:
        self.timeout = t

    def gettimeout(self):
        return self.timeout

    def setsockopt(self, *args, **kwargs) -> None:  # tolerated no-op
        pass

    def sendall(self, data) -> None:
        if self.closed:
            raise BrokenPipeError("sendall on a closed FakeSocket")
        self.sent += bytes(data)

    def send(self, data) -> int:
        self.sendall(data)
        return len(data)

    def recv(self, n: int = 4096) -> bytes:
        if self._queue:
            item = self._queue.popleft()
            return self._yield(item, n)
        return self._yield(self.on_exhaust, n, exhausting=True)

    def _yield(self, item, n: int, *, exhausting: bool = False) -> bytes:
        if item is TIMEOUT:
            return _raise_timeout()
        if item is CLOSE:
            return b""
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        data = bytes(item)
        if data == b"":
            return b""
        if n is not None and len(data) > n:
            # short read: hand back n bytes, keep the tail for next recv.
            if not exhausting:
                self._queue.appendleft(data[n:])
            else:
                # exhaust source is a single blob; keep serving its tail.
                self.on_exhaust = data[n:]
            return data[:n]
        return data

    def shutdown(self, how) -> None:
        self.shutdown_how = how

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:  # not selectable; FakeSocket is for recv-only paths
        return -1

    def getpeername(self):
        return self._peername


def fake_opener(*scripts, on_exhaust: object = b""):
    """Return an ``opener((host, port), timeout=...)`` that hands out one
    :class:`FakeSocket` per call, built from ``scripts`` in order.

    Each positional ``script`` becomes one connection: pass ``bytes`` for a
    single chunk, a list/tuple of chunks/sentinels/exceptions for a sequence,
    or a ready-made :class:`FakeSocket`. A script that *is* an exception makes
    the opener itself raise (a connect failure). Successive opener calls (e.g.
    a reconnect) consume the next script — ``fake_opener(a, b)`` closes on ``a``
    then serves ``b``. Exhausting the scripts raises ``ConnectionRefusedError``.

    The returned opener carries ``.sockets`` (every FakeSocket handed out) and
    ``.remaining()`` for test introspection. No real sockets are ever created.
    """
    pending = collections.deque(scripts)
    handed_out: list = []

    def _open(address=None, timeout=None, *args, **kwargs):
        if not pending:
            raise ConnectionRefusedError(
                f"fake_opener: no more scripted connections "
                f"(had {len(scripts)})"
            )
        spec = pending.popleft()
        if isinstance(spec, BaseException):
            raise spec
        if isinstance(spec, type) and issubclass(spec, BaseException):
            raise spec()
        if isinstance(spec, FakeSocket):
            sock = spec
        else:
            sock = FakeSocket(spec, on_exhaust=on_exhaust)
        if timeout is not None:
            sock.settimeout(timeout)
        handed_out.append(sock)
        return sock

    _open.sockets = handed_out
    _open.remaining = lambda: len(pending)
    return _open


# --------------------------------------------------------------------------- #
# FakeLineServer — JSON-line responder (Transport + real socketpair server).
# --------------------------------------------------------------------------- #
class FakeLineServer:
    """A scripted JSON-line responder keyed by request ``op``.

    Two ways to use it — both drive the *same* op->response table and record
    every parsed request in ``.requests``:

    1. **As a pyverify** ``Transport`` — plug straight into
       ``ShellClient(host, transport=server)``. ``send_line`` parses the request
       and queues the mapped response; ``recv_line`` pops it. No thread, no
       socket.

    2. **Behind a real** :class:`LineSession` — call :meth:`opener` (or
       :meth:`start`) and it spins a ``socket.socketpair()`` with a daemon
       server thread that reads request lines and writes response lines, so a
       genuine ``LineSession``/``ShellClient`` round-trips over an in-process
       socket (the ``LineChannel`` == ``Transport`` interop proof).

    ``responses`` maps an op string to a response ``dict`` (or a callable
    ``req -> dict``). Unmapped ops fall back to ``default`` (``{"ok": True}``).
    """

    def __init__(self, responses=None, *, default=None) -> None:
        self.responses = dict(responses or {})
        self.default = default if default is not None else {"ok": True}
        self.requests: list = []
        self.closed = False
        self._outbox: "collections.deque" = collections.deque()
        self._srv = None
        self._cli = None
        self._thread = None
        self._stop = threading.Event()

    # -- shared op lookup -------------------------------------------------- #
    def _respond(self, req) -> dict:
        op = req.get("op") if isinstance(req, dict) else None
        resp = self.responses.get(op, self.default)
        if callable(resp):
            resp = resp(req)
        return resp

    # -- Transport protocol (direct, thread-free) -------------------------- #
    def send_line(self, payload: bytes) -> None:
        req = json.loads(bytes(payload).decode("utf-8"))
        self.requests.append(req)
        self._outbox.append(json.dumps(self._respond(req)).encode("ascii"))

    def recv_line(self) -> bytes:
        if not self._outbox:
            raise AssertionError(
                "FakeLineServer: no response queued — call send_line first"
            )
        return self._outbox.popleft()

    def close(self) -> None:
        self.closed = True
        self.stop()

    # -- real socketpair server (behind a LineSession) --------------------- #
    def start(self) -> "FakeLineServer":
        if self._thread is not None:
            return self
        self._srv, self._cli = socket.socketpair()
        self._thread = threading.Thread(
            target=self._serve, name="FakeLineServer", daemon=True
        )
        self._thread.start()
        return self

    def _serve(self) -> None:
        srv = self._srv
        srv.settimeout(0.25)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = srv.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue
                req = json.loads(line.decode("utf-8"))
                self.requests.append(req)
                out = json.dumps(self._respond(req)).encode("ascii") + b"\n"
                try:
                    srv.sendall(out)
                except OSError:
                    return

    def client_socket(self):
        """The client end of the socketpair (starts the server on first use)."""
        if self._cli is None:
            self.start()
        return self._cli

    def opener(self):
        """An ``opener`` that hands a :class:`LineSession` the socketpair client
        end (ignoring the requested address — it always reaches this server)."""

        def _open(address=None, timeout=None, *args, **kwargs):
            sock = self.client_socket()
            if timeout is not None:
                try:
                    sock.settimeout(timeout)
                except OSError:
                    pass
            return sock

        return _open

    def stop(self) -> None:
        self._stop.set()
        for s in (self._cli, self._srv):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "FakeLineServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# --------------------------------------------------------------------------- #
# FakeByteServer — localhost scripted TCP server (promoted _MiniServer).
# --------------------------------------------------------------------------- #
class FakeByteServer:
    """A one-or-more-shot scripted TCP server on ``127.0.0.1:0``.

    Promotes pyverify ``tests/test_console.py``'s ``_MiniServer``: it accepts up
    to ``max_conns`` connections (``max_conns<=0`` = unlimited, for reconnect
    tests) and runs ``handler(conn, stop)`` for each in a daemon thread.
    ``stop`` is set on teardown so a holding handler releases immediately.

    Use the ready-made handler factories (:meth:`hold`, :meth:`send_then_hold`,
    :meth:`send_then_close`, :meth:`send_chunks`, :meth:`echo`,
    :meth:`line_responder`) or pass your own. :meth:`opener` returns an opener
    that connects here regardless of the requested address, so a
    :class:`RawStreamSession`/:class:`ConsoleBridge` built for port 6930 lands
    on this ephemeral port instead. Loopback only — never a board.
    """

    def __init__(self, handler, *, host: str = "127.0.0.1", max_conns: int = 1) -> None:
        self._handler = handler
        self._max_conns = max_conns
        self.stop = threading.Event()
        self.conns = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self._sock.listen(max(1, max_conns))
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(
            target=self._serve, name="FakeByteServer", daemon=True
        )

    def _serve(self) -> None:
        served = 0
        self._sock.settimeout(0.25)
        while not self.stop.is_set() and (self._max_conns <= 0 or served < self._max_conns):
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            served += 1
            self.conns = served
            try:
                self._handler(conn, self.stop)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def address(self):
        return (self.host, self.port)

    def opener(self):
        def _open(address=None, timeout=None, *args, **kwargs):
            return socket.create_connection((self.host, self.port), timeout=timeout)

        return _open

    def __enter__(self) -> "FakeByteServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)

    # -- handler factories (the send-and-hold / send-then-close family) ----- #
    @staticmethod
    def hold(conn, stop) -> None:
        """Send nothing; keep the connection open until teardown."""
        stop.wait(2.0)

    @staticmethod
    def send_then_hold(data: bytes):
        def handler(conn, stop) -> None:
            conn.sendall(data)
            stop.wait(2.0)

        return handler

    @staticmethod
    def send_then_close(data: bytes):
        def handler(conn, stop) -> None:
            conn.sendall(data)
            # returning closes the connection -> EOF to the client.

        return handler

    @staticmethod
    def send_chunks(chunks, gap: float = 0.05):
        def handler(conn, stop) -> None:
            for i, chunk in enumerate(chunks):
                if i and gap:
                    stop.wait(gap)
                conn.sendall(chunk)
            stop.wait(2.0)

        return handler

    @staticmethod
    def echo(conn, stop, *, record: "bytearray | None" = None) -> None:
        conn.settimeout(0.25)
        while not stop.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            if record is not None:
                record.extend(data)
            try:
                conn.sendall(data)
            except OSError:
                return

    @staticmethod
    def line_responder(mapping, *, default: bytes = b"unknown\r\n",
                       record: "bytearray | None" = None):
        """A socket twin of the pty fake device: read whole lines, reply with
        ``mapping[stripped_line]`` (else ``default``), recording every received
        byte into ``record`` for host->remote forwarding assertions."""
        table = {k: v for k, v in mapping.items()}

        def handler(conn, stop) -> None:
            conn.settimeout(0.25)
            buf = ""
            while not stop.is_set():
                try:
                    raw = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not raw:
                    return
                if record is not None:
                    record.extend(raw)
                buf += raw.decode("utf-8", "replace")
                while True:
                    idx = _first_newline(buf)
                    if idx < 0:
                        break
                    line, buf = buf[:idx], buf[idx + 1:]
                    key = line.strip()
                    if not key:
                        continue
                    reply = table.get(key, default)
                    try:
                        conn.sendall(reply)
                    except OSError:
                        return

        return handler


def _first_newline(s: str) -> int:
    cr, lf = s.find("\r"), s.find("\n")
    both = [i for i in (cr, lf) if i >= 0]
    return min(both) if both else -1


# --------------------------------------------------------------------------- #
# FakeXsdbRunner — a SubprocessRunner-shaped double for xsdb.
# --------------------------------------------------------------------------- #
#: One recorded invocation, for test introspection.
XsdbCall = collections.namedtuple("XsdbCall", "argv timeout_s input_text tcl")

_MRD_RE = re.compile(r"\bmrd\b([^\]\n]*)", re.IGNORECASE)
_MWR_RE = re.compile(
    r"\bmwr\b\s+(0x[0-9A-Fa-f]+)\s+(0x[0-9A-Fa-f]+)", re.IGNORECASE
)
_HEXADDR_RE = re.compile(r"0x[0-9A-Fa-f]+")


class FakeXsdbRunner:
    """A ``SubprocessRunner``-shaped callable that never launches a process.

    Called as ``runner(argv, *, timeout_s, input_text=None)`` (the
    ``pyverify.debug`` runner seam) and returns a
    ``subprocess.CompletedProcess``. It reads the tcl out of the argv
    (``[xsdb, '-eval', <tcl>]``) and answers each concrete ``mrd`` from a
    ``{addr: value}`` dict:

    * ``mrd -value ... <hex> <n>`` -> bare ``00000001``-style hex words;
    * ``mrd ... <hex> <n>``        -> labelled ``AABBCCDD:   00000001`` lines;
    * ``mwr <hex> <hex>``          -> no stdout (and updates the value table so a
                                      later read reflects the write).

    The ascending LMB-alias magic scan is driven Python-side by
    :class:`~socket_harness.xsdb.DiagMailbox` reading each candidate via
    ``read_word``; this runner just returns the magic value at whatever
    address(es) the test seeds (put the magic at *both* aliasing candidates to
    reproduce the 256 KB-shell trap). ``wrong_magic=True`` forces a non-zero
    return code and never yields the magic — the ``TargetNotFound`` negative
    control. Variable-address scan tcl (``mrd -force $base 1``) has no concrete
    address, so it is harmlessly ignored.
    """

    def __init__(
        self,
        values=None,
        *,
        default: int = 0,
        magic: str = "D1A6C0DE",
        wrong_magic: bool = False,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        self.values = {int(k): int(v) & 0xFFFFFFFF for k, v in (values or {}).items()}
        self.default = int(default) & 0xFFFFFFFF
        self.magic = magic
        self.wrong_magic = bool(wrong_magic)
        self.forced_returncode = returncode
        self.forced_stderr = stderr
        self.calls: list = []
        self.reads: list = []
        self.writes: list = []

    @property
    def invoked(self) -> int:
        return len(self.calls)

    def __call__(self, argv, *, timeout_s=None, input_text=None):
        tcl = self._extract_tcl(argv, input_text)
        self.calls.append(
            XsdbCall(argv=list(argv), timeout_s=timeout_s, input_text=input_text, tcl=tcl)
        )

        # Apply any writes first so a read-after-write in the same tcl is seen.
        for m in _MWR_RE.finditer(tcl):
            addr = int(m.group(1), 16)
            val = int(m.group(2), 16) & 0xFFFFFFFF
            self.values[addr] = val
            self.writes.append((addr, val))

        out_lines: list[str] = []
        for args in _MRD_RE.findall(tcl):
            hexes = _HEXADDR_RE.findall(args)
            if not hexes:
                continue  # e.g. `mrd -force $base 1` — variable, not concrete
            addr = int(hexes[0], 16)
            nwords = self._word_count(args)
            valued = "-value" in args.lower()
            words = [self._read(addr + 4 * i) for i in range(nwords)]
            out_lines.append(self._format(addr, words, valued))

        if self.forced_returncode is not None:
            rc = self.forced_returncode
        elif self.wrong_magic:
            rc = 1
        else:
            rc = 0
        stderr = self.forced_stderr
        if self.wrong_magic and not stderr:
            stderr = "no target matched magic"

        stdout = "\n".join(out_lines)
        if stdout:
            stdout += "\n"
        return subprocess.CompletedProcess(
            args=list(argv), returncode=rc, stdout=stdout, stderr=stderr
        )

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _extract_tcl(argv, input_text) -> str:
        argv = list(argv)
        if "-eval" in argv:
            i = argv.index("-eval")
            if i + 1 < len(argv):
                return argv[i + 1]
        if input_text:
            return input_text
        return " ".join(argv)

    @staticmethod
    def _word_count(args: str) -> int:
        # trailing bare decimal token = word count; default 1.
        n = 1
        for tok in args.split():
            if tok.isdigit():
                n = int(tok)
        return max(1, n)

    def _read(self, addr: int) -> int:
        self.reads.append(addr)
        if self.wrong_magic:
            # never surface the magic value anywhere.
            v = self.values.get(addr, self.default)
            if f"{v & 0xFFFFFFFF:08X}".upper() == self.magic.upper():
                return 0
            return v & 0xFFFFFFFF
        return self.values.get(addr, self.default) & 0xFFFFFFFF

    @staticmethod
    def _format(addr: int, words: list[int], valued: bool) -> str:
        if valued:
            return " ".join(f"{w & 0xFFFFFFFF:08X}" for w in words)
        lines = []
        for i in range(0, len(words), 4):
            chunk = words[i:i + 4]
            label = f"{(addr + i * 4) & 0xFFFFFFFF:08X}:"
            lines.append(label + "   " + " ".join(f"{w & 0xFFFFFFFF:08X}" for w in chunk))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# FakeClock — deterministic virtual clock for retry backoff assertions.
# --------------------------------------------------------------------------- #
class FakeClock:
    """A :class:`~socket_harness.retry.Clock` with virtual time.

    ``sleep(s)`` records ``s`` and advances ``monotonic()`` by exactly ``s`` —
    no wall-clock passes — so ``connect_with_retry`` timing is exact and fast.
    ``sleeps`` is the ordered list of slept intervals; total elapsed is
    ``monotonic() - start``.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)
        self._start = float(start)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._t += seconds

    @property
    def elapsed(self) -> float:
        return self._t - self._start


# --------------------------------------------------------------------------- #
# pty_loopback — the board-free pty rig (verbatim from mps3_console._self_test).
# --------------------------------------------------------------------------- #
def pty_loopback():
    """Open a pty pair with a fake-device thread on the peripheral end.

    Returns ``(controller_fd, stop_event, thread)``. Writing a full line to
    ``controller_fd`` gets a deterministic answer back on the same fd:

    * ``hello``            -> ``b"WORLD_42\\r\\n"``
    * any other non-empty  -> ``b"unknown\\r\\n"`` (the baked-in negative
                              control: a device that answered unconditionally
                              could not fake a pass)

    Set ``stop_event`` and ``join`` the thread to tear down; the thread closes
    the peripheral fd on exit, the caller closes ``controller_fd``. Lifted
    verbatim from ``scripts/mps3_console.py::_self_test``.
    """
    controller, peripheral = os.openpty()
    for fd in (controller, peripheral):
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO  # lflag: no echo, or we match our own writes
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    stop = threading.Event()

    def fake_device() -> None:
        """Minimal DUT: on a full line, reply with a deterministic marker."""
        buf = ""
        try:
            while not stop.is_set():
                r, _, _ = select.select([peripheral], [], [], 0.1)
                if not r:
                    continue
                try:
                    data = os.read(peripheral, 1024).decode("utf-8", "replace")
                except OSError:
                    return
                if not data:
                    continue
                for ch in data:
                    if ch in "\r\n":
                        if buf.strip() == "hello":
                            os.write(peripheral, b"WORLD_42\r\n")
                        elif buf.strip():
                            os.write(peripheral, b"unknown\r\n")
                        buf = ""
                    else:
                        buf += ch
        finally:
            try:
                os.close(peripheral)
            except OSError:
                pass

    t = threading.Thread(target=fake_device, name="pty_loopback_device", daemon=True)
    t.start()
    return controller, stop, t
