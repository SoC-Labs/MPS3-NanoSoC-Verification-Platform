from __future__ import annotations

"""console_bridge.py — pure-Python, ser2net-compatible pty/tcp console bridge
plus registry-driven ser2net/socat config emitters.

Two independent jobs, both driven off the ONE endpoint registry
(``socket_harness.endpoints``) so the hand-maintained ``host/console/*`` files
stop drifting from ``net-protocol.md``:

1. **Be** the bridge in pure Python.  A :class:`ConsoleBridge` copies bytes
   between a reconnecting :class:`~socket_harness.session.RawStreamSession`
   (the shell's raw-TCP console port, e.g. 6930) and a :class:`LocalEndpoint`
   — either a :class:`PtyEndpoint` (a pty whose slave is symlinked to
   ``/tmp/mps3-console/<name>``, exactly like ``socat PTY,link=…,raw,echo=0``)
   or a :class:`TcpListenEndpoint`.  This resolves the README's "pty accepter
   syntax not yet confirmed" caveat with a fallback that needs no ser2net
   install.  ``pump_once()`` is the unit-testable single step; it is exercised
   board-free with a pty loopback + an in-process fake socket.

2. **Emit** the ser2net/socat configs from the same registry
   (:func:`ser2net_yaml`, :func:`socat_argv`) so the bridge is ser2net
   compatible both ways.

Board-free discipline: no sockets are opened at import time, the socket opener
and retry clock are injected, and every pure step (config emit) is a pure
function.  The console byte-plumbing mirrors ``scripts/mps3_console.py``'s raw
8N1 / cfmakeraw handling so the pty behaves identically to the proven driver.
"""

import os
import pty  # noqa: F401  (documents the pty dependency; os.openpty is used)
import select
import socket
import termios
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .endpoints import DEFAULT_SHELL_HOST, EndpointSpec, console_specs
from .retry import RealClock, RetryPolicy
from .session import ByteChannel, EndpointClosed, RawStreamSession  # noqa: F401

__all__ = [
    "LocalEndpoint",
    "PtyEndpoint",
    "TcpListenEndpoint",
    "PumpStat",
    "ConsoleBridge",
    "ser2net_yaml",
    "socat_argv",
]

# Default location for the pty symlinks, matching host/console/socat_consoles.sh
# and host/console/ser2net.yaml.
DEFAULT_LINK_DIR = "/tmp/mps3-console"


# --------------------------------------------------------------------------
# termios raw helper — cfmakeraw equivalent, lifted from scripts/mps3_console.py
# --------------------------------------------------------------------------
def _make_raw(fd: int) -> None:
    """Put ``fd`` into raw 8N1 with echo OFF (cfmakeraw equivalent).

    Byte-for-byte the flag mask used by scripts/mps3_console.py::Console.raw so
    the pty presents the same transparent stream the proven console driver
    relies on: no canonical mode, no echo, no CR/LF translation, no
    parity/xon-xoff mangling.
    """
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
    iflag &= ~(
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
    )
    oflag &= ~termios.OPOST
    lflag &= ~(
        termios.ECHO
        | termios.ECHONL
        | termios.ICANON
        | termios.ISIG
        | termios.IEXTEN
    )
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(
        fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
    )


# --------------------------------------------------------------------------
# Local endpoints (the "accepter" side)
# --------------------------------------------------------------------------
@runtime_checkable
class LocalEndpoint(Protocol):
    """The local (host-facing) side of a bridge: a pty or a TCP listener.

    Structurally the same read/write/close surface as a
    :class:`~socket_harness.session.ByteChannel`, plus ``open``/``fileno`` so a
    :class:`ConsoleBridge` can ``select()`` over it.
    """

    def open(self) -> None:
        ...

    def fileno(self) -> int:
        ...

    def read(self, n: int = 4096) -> bytes:
        ...

    def write(self, data: bytes) -> None:
        ...

    def close(self) -> None:
        ...


class PtyEndpoint:
    """A pty whose slave is symlinked to ``link_path`` — the ``socat
    PTY,link=<path>,raw,echo=0`` equivalent in pure Python.

    The bridge reads/writes the master fd; a consumer (screen, pyserial, a
    notebook) opens ``link_path`` and sees a raw serial-like device.  We keep
    our own handle on the slave open so the master never hits EIO when a
    consumer disconnects and reconnects (we never read the slave, so consumers
    are never starved of bytes).
    """

    def __init__(self, link_path: str):
        self._link_path = link_path
        self._master: int | None = None
        self._slave: int | None = None
        self._pts: str | None = None

    def open(self) -> None:
        if self._master is not None:
            return
        master, slave = os.openpty()
        _make_raw(slave)
        self._master = master
        self._slave = slave
        self._pts = os.ttyname(slave)
        self._install_symlink(self._pts)

    def _install_symlink(self, target: str) -> None:
        link = self._link_path
        parent = os.path.dirname(link)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Replace a stale link/file atomically-ish (mirrors socat re-creating it).
        if os.path.islink(link) or os.path.exists(link):
            try:
                os.unlink(link)
            except OSError:
                pass
        os.symlink(target, link)

    def fileno(self) -> int:
        if self._master is None:
            raise RuntimeError("PtyEndpoint.fileno() before open()")
        return self._master

    def read(self, n: int = 4096) -> bytes:
        if self._master is None:
            raise RuntimeError("PtyEndpoint.read() before open()")
        try:
            return os.read(self._master, n)
        except OSError:
            # Slave-side vanished; treat as EOF for the bridge.
            return b""

    def write(self, data: bytes) -> None:
        if self._master is None:
            raise RuntimeError("PtyEndpoint.write() before open()")
        view = memoryview(data)
        total = 0
        while total < len(view):
            total += os.write(self._master, view[total:])

    def close(self) -> None:
        if os.path.islink(self._link_path):
            try:
                os.unlink(self._link_path)
            except OSError:
                pass
        for fd in (self._master, self._slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._master = None
        self._slave = None
        self._pts = None

    @property
    def pts_path(self) -> str | None:
        """The kernel pts path the symlink points at (mostly for diagnostics)."""
        return self._pts


class TcpListenEndpoint:
    """A localhost TCP listener that accepts ONE client on demand.

    The accept is lazy: ``fileno()`` returns the listen socket until a client
    connects, then the accepted connection.  ``read``/``write`` accept on first
    use.  A client hang-up (recv -> b'') resets the connection so a fresh
    client can attach.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._listen: socket.socket | None = None
        self._conn: socket.socket | None = None

    def open(self) -> None:
        if self._listen is not None:
            return
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((self._host, self._port))
        ls.listen(1)
        self._listen = ls
        # Reflect the OS-assigned port back when port=0 was requested.
        self._port = ls.getsockname()[1]

    @property
    def port(self) -> int:
        return self._port

    def _ensure_conn(self) -> socket.socket:
        if self._listen is None:
            raise RuntimeError("TcpListenEndpoint.read/write before open()")
        if self._conn is None:
            conn, _addr = self._listen.accept()
            self._conn = conn
        return self._conn

    def fileno(self) -> int:
        if self._conn is not None:
            return self._conn.fileno()
        if self._listen is not None:
            return self._listen.fileno()
        raise RuntimeError("TcpListenEndpoint.fileno() before open()")

    def read(self, n: int = 4096) -> bytes:
        conn = self._ensure_conn()
        data = conn.recv(n)
        if data == b"":
            # Client closed; drop it so the next read re-accepts.
            try:
                conn.close()
            except OSError:
                pass
            self._conn = None
        return data

    def write(self, data: bytes) -> None:
        conn = self._ensure_conn()
        conn.sendall(data)

    def close(self) -> None:
        for s in (self._conn, self._listen):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._conn = None
        self._listen = None


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------
@dataclass
class PumpStat:
    """Result of a single :meth:`ConsoleBridge.pump_once` step."""

    to_local: int = 0
    to_remote: int = 0
    reconnected: bool = False


class ConsoleBridge:
    """Copy bytes between a reconnecting remote console socket and a local
    endpoint (pty or tcp listener).

    The remote side is a :class:`~socket_harness.session.RawStreamSession`
    resolved from ``spec`` against ``host`` — every socket/clock/retry seam is
    injected so the whole thing is testable with zero real sockets.
    """

    def __init__(
        self,
        spec: EndpointSpec,
        host: str,
        local: LocalEndpoint,
        *,
        opener=socket.create_connection,
        retry: RetryPolicy = RetryPolicy(),
        clock=RealClock(),
    ):
        self.spec = spec
        self.host = host
        self.local = local
        self.session = RawStreamSession(
            spec.resolve(host),
            opener=opener,
            retry=retry,
            clock=clock,
            auto_reconnect=True,
        )
        self._closed = False

    # -- setup ------------------------------------------------------------
    def _ensure_open(self) -> None:
        self.local.open()
        if not self.session.is_connected:
            self.session.connect()

    def _remote_fileno(self) -> int:
        """Underlying fd of the remote session's socket.

        Prefers a public ``fileno()`` if the session grows one; otherwise reads
        the ``_sock`` the SocketSession contract stores.  Assumes the session
        is connected (callers go through ``_ensure_open`` first).
        """
        fn = getattr(self.session, "fileno", None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
        sock = getattr(self.session, "_sock", None)
        if sock is not None:
            return sock.fileno()
        raise RuntimeError("remote session has no socket to select on")

    # -- the unit-testable single step -----------------------------------
    def pump_once(self, timeout: float) -> PumpStat:
        """Do at most one select()+copy in each direction.

        Returns a :class:`PumpStat` recording how many bytes moved and whether
        the remote had to reconnect.  A remote peer-close (``EndpointClosed``,
        or an empty read) triggers ``session.reconnect()`` — which replays the
        session's own retry policy — and flags ``reconnected``.
        """
        stat = PumpStat()
        self._ensure_open()

        remote_fd = self._remote_fileno()
        local_fd = self.local.fileno()

        readable, _, _ = select.select([remote_fd, local_fd], [], [], timeout)

        if remote_fd in readable:
            try:
                data = self.session.read(4096)
            except EndpointClosed:
                self._reconnect_remote(stat)
            else:
                if data:
                    self.local.write(data)
                    stat.to_local += len(data)
                else:
                    # Half-close surfaced as an empty read rather than raising.
                    self._reconnect_remote(stat)

        if local_fd in readable:
            data = self.local.read(4096)
            if data:
                self.session.write(data)
                stat.to_remote += len(data)

        return stat

    def _reconnect_remote(self, stat: PumpStat) -> None:
        self.session.reconnect()
        stat.reconnected = True

    # -- long-running driver ---------------------------------------------
    def run_forever(self, *, poll: float = 0.5) -> None:
        """Connect both ends and pump until KeyboardInterrupt or ``close()``."""
        self._closed = False
        self._ensure_open()
        try:
            while not self._closed:
                self.pump_once(poll)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self) -> None:
        self._closed = True
        try:
            self.session.close()
        except Exception:
            pass
        try:
            self.local.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Registry-driven config emitters (kill the hand-maintained host/console/*)
# --------------------------------------------------------------------------
def ser2net_yaml(
    specs=None,
    host: str = DEFAULT_SHELL_HOST,
    link_dir: str = DEFAULT_LINK_DIR,
) -> str:
    """Regenerate a ser2net YAML config from the endpoint registry.

    One ``connection:`` block per console spec, wiring a pty ``accepter``
    (symlinked under ``link_dir``) to the shell's raw-TCP ``connector``.
    Structurally compatible with host/console/ser2net.yaml; the host is emitted
    once as the ``&shell_host`` anchor and referenced with ``*shell_host``.
    """
    if specs is None:
        specs = console_specs()
    lines = [
        "# ser2net.yaml — GENERATED by socket_harness.console_bridge.ser2net_yaml()",
        "# from the one endpoint registry (socket_harness.endpoints.REGISTRY).",
        "# Regenerate rather than hand-edit; see host/console/README.md for the",
        "# pty-accepter rationale. Ports are net-protocol.md's console map.",
        "%YAML 1.1",
        "---",
        f"define: &shell_host {host}",
        "",
    ]
    for spec in specs:
        link = f"{link_dir}/{spec.name}"
        lines.append(f"connection: &mps3_{spec.name}")
        lines.append(f"    accepter: pty,{link}")
        lines.append(f"    connector: tcp,*shell_host,{spec.default_port}")
        lines.append("    options:")
        lines.append(f"      trace-both: /var/log/mps3/{spec.name}.trace")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def socat_argv(
    spec: EndpointSpec,
    host: str,
    link_dir: str = DEFAULT_LINK_DIR,
) -> list[str]:
    """The socat invocation bridging one console spec's TCP port to a pty.

    Matches host/console/socat_consoles.sh:
    ``socat -d -d PTY,link=<dir>/<name>,raw,echo=0 TCP:<host>:<port>``.
    """
    return [
        "socat",
        "-d",
        "-d",
        f"PTY,link={link_dir}/{spec.name},raw,echo=0",
        f"TCP:{host}:{spec.default_port}",
    ]
