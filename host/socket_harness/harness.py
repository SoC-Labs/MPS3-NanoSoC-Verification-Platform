from __future__ import annotations

"""``SocketHarness`` — the unified board-free facade over the MPS3 nanoSoC
host-facing endpoints.

This is the keystone module: it composes the frozen endpoint registry
(:mod:`socket_harness.endpoints`), the reconnecting session spine
(:mod:`socket_harness.session`), the xsdb/CSR register backend
(:mod:`socket_harness.xsdb` / :mod:`socket_harness.registers`) and the
console bridge (:mod:`socket_harness.console_bridge`) into one object, and
wires the pyverify verbs onto it *for free*.

The interop keystone (see the README): ``socket_harness.session.LineChannel``
is structurally identical to ``pyverify.client.Transport``, so a
:class:`~socket_harness.session.LineSession` is a drop-in transport for
``pyverify.client.ShellClient``. :meth:`SocketHarness.shell` therefore hands
back a real ``ShellClient`` whose ``ping``/``swap``/``set_clk``/``macgen``/
``display``/``diag`` verbs ride a *reconnecting* session with no adapter and
no per-verb re-plumbing.

Honest liveness. :meth:`SocketHarness.probe` follows the discipline of
``pyverify.edge.status_fpga`` / ``TelemetryResponse``: it NEVER fabricates a
plausible zero. An endpoint is reported ``'ok'`` only when it affirmatively
answered, ``'declined'`` when it answered-but-said-no (or cannot be asserted
board-free), and ``'unreachable'`` when the connection could not be made or
was closed. ``probe`` is exit-code-friendly — it never raises for a dead
endpoint.

Board-free discipline: every socket/serial/xsdb touch rides an injected
seam (``opener``, ``runner``, ``retry``), so the whole facade is exercised
in-process against the ``socket_harness.loopback`` fakes with zero real
sockets and no board.

Note (critique fix #1): ``DEFAULT_HUB_URL`` here is the FQDN form
``tcp:<hub-fqdn>:3121`` that xsdb requires; it is
intentionally a *different string* from ``pyverify.swd.DEFAULT_HUB`` (bare
``<hub-host>``) and no equality between the two is assumed anywhere.
"""

import socket
from dataclasses import dataclass, replace

from .endpoints import (
    REGISTRY,
    by_name,
    EndpointSpec,
    Address,
    DEFAULT_SHELL_HOST,
    DEFAULT_HUB_URL,
    Family,
    Framing,
)
from .session import (
    LineSession,
    RawStreamSession,
    SerialSession,
    EndpointClosed,
)
from .retry import RetryPolicy, ConnectFailed
from .xsdb import XsdbRegisterEndpoint, XsdbConfig, TargetNotFound
from .registers import RegisterAccess
from .console_bridge import ConsoleBridge, LocalEndpoint

from pyverify.client import ShellClient, ShellProtocolError
from pyverify.console import ConsoleReader
from pyverify.swd import SwdDebugger, SwdError
from pyverify.debug import SubprocessRunner

__all__ = ["ProbeResult", "SocketHarness"]


# --------------------------------------------------------------------------- #
# Small pure helpers (no I/O)
# --------------------------------------------------------------------------- #


def _hexword(val: object) -> str:
    """Format an integer register value as ``0xAABBCCDD`` (fallback: str)."""
    try:
        return f"0x{int(val):08X}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(val)


def _safe_close(obj: object) -> None:
    """Close ``obj`` swallowing any error — used in probe cleanup so a close
    failure never masks the probe result (honesty over tidiness)."""
    try:
        obj.close()  # type: ignore[attr-defined]
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Probe result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeResult:
    """One honest liveness verdict for a single endpoint.

    ``status`` is one of:

    * ``'ok'``          — the endpoint affirmatively answered (``value`` holds
                          the meaningful reading, e.g. an ``rm_id`` or DPIDR).
    * ``'declined'``    — reachable/answered but not affirmatively alive
                          (``ping ok=false``; a connected-but-silent console;
                          a board-free dry-run preview with no runner; a
                          family with no defined liveness probe).
    * ``'unreachable'`` — could not connect, or the peer closed the stream.

    ``value`` is deliberately ``None`` unless a real reading was obtained — the
    probe never fills it with a fabricated zero.
    """

    name: str
    status: str
    detail: str = ""
    value: object = None


# --------------------------------------------------------------------------- #
# The facade
# --------------------------------------------------------------------------- #


class SocketHarness:
    """Unified, board-free-testable facade over every host-facing endpoint.

    Usage::

        h = SocketHarness()                       # .101, FQDN hub
        with h.shell() as shell:                  # pyverify ShellClient, free
            print(shell.ping().rm_id)
        print(h.probe("control"))                 # honest liveness
        regs = h.registers()                      # CSR ops over xsdb

    Every network touch rides an injected seam so tests use zero real sockets:

    * ``opener``  — the socket factory the sessions call (default
      ``socket.create_connection``); tests pass a loopback opener.
    * ``runner``  — the subprocess seam the xsdb/SWD backends use (default
      ``None`` => pyverify's ``default_runner``); tests pass a fake runner.
    * ``retry``   — the connect/reconnect backoff policy (frozen, shared).

    ``overrides`` is a per-name TCP-port override map that kills the
    ``192.168.10.101``/port hardcodes scattered across the old scripts: e.g.
    ``overrides={"uart0": 16930}`` when a relay is remapped. Unknown names are
    rejected up front against the registry.
    """

    def __init__(
        self,
        host: str = DEFAULT_SHELL_HOST,
        *,
        hub_url: str = DEFAULT_HUB_URL,
        overrides: "dict[str, int] | None" = None,
        opener=socket.create_connection,
        runner: "SubprocessRunner | None" = None,
        retry: RetryPolicy = RetryPolicy(),
    ) -> None:
        self.host = host
        self.hub_url = hub_url
        self._overrides: "dict[str, int]" = dict(overrides) if overrides else {}
        self._opener = opener
        self._runner = runner
        self._retry = retry
        if self._overrides:
            known = {spec.name for spec in REGISTRY}
            unknown = sorted(set(self._overrides) - known)
            if unknown:
                raise ValueError(
                    "unknown endpoint override name(s): "
                    f"{unknown}; known: {sorted(known)}"
                )

    # -- registry lookup ---------------------------------------------------- #

    def spec(self, name: str) -> EndpointSpec:
        """Resolve ``name`` to its :class:`EndpointSpec`, applying any port
        override (a frozen copy with ``default_port`` replaced)."""
        base = by_name(name)  # ValueError (with known names) on a typo
        if name in self._overrides:
            return replace(base, default_port=self._overrides[name])
        return base

    def _resolve(self, name: str) -> Address:
        """The endpoint's concrete :class:`Address` for this harness's host."""
        return self.spec(name).resolve(self.host)

    # -- native sessions ---------------------------------------------------- #

    def line(self, name: str = "control") -> LineSession:
        """Build + connect a reconnecting :class:`LineSession` on a JSON-line
        (CONTROL) endpoint. This is the transport the pyverify ``ShellClient``
        rides — see :meth:`shell`."""
        session = LineSession(
            self._resolve(name),
            opener=self._opener,
            retry=self._retry,
            auto_reconnect=True,
        )
        return session.connect()

    def stream(self, name: str) -> RawStreamSession:
        """Build + connect a native :class:`RawStreamSession` on a raw-byte
        CONSOLE endpoint (``uart0``/``uart1``/``swo``)."""
        session = RawStreamSession(
            self._resolve(name),
            opener=self._opener,
            retry=self._retry,
        )
        return session.connect()

    def serial(self, name: str = "mcc_console") -> SerialSession:
        """Build + connect a :class:`SerialSession` over the named tty (the
        MCC console lives on ``addr.device`` from the registry)."""
        session = SerialSession(self._resolve(name), retry=self._retry)
        return session.connect()

    # -- CSR / register backend --------------------------------------------- #

    def registers(self) -> RegisterAccess:
        """A backend-agnostic :class:`RegisterAccess` bound to the xsdb/CSR
        endpoint (FQDN hub, injected runner)."""
        backend = XsdbRegisterEndpoint(
            XsdbConfig(hub_url=self.hub_url), runner=self._runner
        )
        return RegisterAccess(backend)

    # -- console bridge ----------------------------------------------------- #

    def bridge(self, name: str, local: LocalEndpoint) -> ConsoleBridge:
        """A ser2net-compatible :class:`ConsoleBridge` pumping between the
        given ``local`` endpoint (pty/tcp) and the named remote console."""
        return ConsoleBridge(
            self.spec(name),
            self.host,
            local,
            opener=self._opener,
            retry=self._retry,
        )

    # -- pyverify interop (the keystone) ------------------------------------ #

    def shell(self) -> ShellClient:
        """A pyverify ``ShellClient`` riding this harness's reconnecting
        :class:`LineSession`.

        No adapter: ``LineChannel`` is byte-identical to
        ``pyverify.client.Transport``, so the client gets ``ping``/``swap``/
        ``set_clk``/``macgen``/``display``/``diag`` over a reconnecting
        session for free.
        """
        return ShellClient(self.host, transport=self.line("control"))

    def console_reader(self, name: str) -> ConsoleReader:
        """A pyverify ``ConsoleReader`` (unchanged) for the named console port.

        The native alternative is :meth:`stream`, which returns a
        ``RawStreamSession`` — both are exposed so callers can pick the
        pyverify scraper or the harness's own reconnecting session.
        """
        return ConsoleReader(self.host, self._resolve(name).port)

    def swd(self) -> SwdDebugger:
        """A pyverify ``SwdDebugger`` (unchanged) bound to this host + runner."""
        return SwdDebugger(shell_host=self.host, runner=self._runner)

    # -- honest liveness ---------------------------------------------------- #

    def probe(self, name: str) -> ProbeResult:
        """Honest, exit-code-friendly liveness for one endpoint.

        Dispatches by family and NEVER fabricates a plausible zero; never
        raises for an endpoint that is simply dead. An unknown ``name`` is a
        usage error and DOES raise (``ValueError``) before any probing.
        """
        spec = self.spec(name)  # usage error for a typo — propagate, do not mask
        try:
            fam = spec.family
            if fam is Family.CONTROL:
                return self._probe_control(spec)
            if fam is Family.CONSOLE:
                return self._probe_console(spec)
            if fam is Family.DEBUG_TOOL:
                return self._probe_debug_tool(spec)
            if fam is Family.CSR:
                return self._probe_csr(spec)
            return ProbeResult(
                spec.name,
                "declined",
                detail=f"no liveness probe defined for family {fam.name}",
            )
        except Exception as exc:  # final honesty net: probe never raises for a dead endpoint
            return ProbeResult(
                spec.name, "unreachable", detail=f"{type(exc).__name__}: {exc}"
            )

    def _probe_control(self, spec: EndpointSpec) -> ProbeResult:
        """CONTROL: ``ping`` over the shell — ok -> 'ok' (rm_id), ok=false ->
        'declined', connect/protocol failure -> 'unreachable'."""
        try:
            shell = self.shell()
        except (OSError, ConnectFailed, ShellProtocolError) as exc:
            return ProbeResult(spec.name, "unreachable", detail=f"connect failed: {exc}")
        try:
            resp = shell.ping()
        except (OSError, ConnectFailed, ShellProtocolError) as exc:
            return ProbeResult(spec.name, "unreachable", detail=f"ping failed: {exc}")
        finally:
            _safe_close(shell)
        if resp.ok:
            return ProbeResult(
                spec.name,
                "ok",
                detail=f"shell_id={resp.shell_id} rm_id={resp.rm_id}",
                value=resp.rm_id,
            )
        return ProbeResult(spec.name, "declined", detail="shell answered ok=false")

    def _probe_console(self, spec: EndpointSpec) -> ProbeResult:
        """CONSOLE: drain a few bytes — any -> 'ok', peer-close -> 'unreachable',
        connected-but-silent -> 'declined' (reachable, no data to assert on)."""
        try:
            sess = self.stream(spec.name)
        except (OSError, ConnectFailed) as exc:
            return ProbeResult(spec.name, "unreachable", detail=f"connect failed: {exc}")
        try:
            data = sess.read(64)
        except EndpointClosed as exc:  # ConnectionError subclass — before OSError
            return ProbeResult(spec.name, "unreachable", detail=f"peer closed: {exc}")
        except TimeoutError:  # OSError subclass — connected but no bytes in time
            return ProbeResult(
                spec.name, "declined", detail="connected, no bytes within timeout"
            )
        except (OSError, ConnectFailed) as exc:
            return ProbeResult(spec.name, "unreachable", detail=f"read failed: {exc}")
        finally:
            _safe_close(sess)
        if data:
            return ProbeResult(
                spec.name, "ok", detail=f"drained {len(data)} byte(s)", value=data
            )
        return ProbeResult(spec.name, "unreachable", detail="empty read (peer closed)")

    def _probe_debug_tool(self, spec: EndpointSpec) -> ProbeResult:
        """DEBUG_TOOL: only the SWD (OpenOCD remote-bitbang) endpoint has a
        liveness op — a real ``dpidr()`` when a runner is present ->
        'ok'/'unreachable', otherwise a board-free dry-run argv preview
        reported as 'declined' (a preview is not liveness). Other debug tools
        in this family (e.g. XVC) have no board-free probe -> 'declined'."""
        if spec.framing is not Framing.OPENOCD_RBB:
            return ProbeResult(
                spec.name,
                "declined",
                detail=f"no board-free liveness probe for {spec.framing.name} debug tool",
            )
        if self._runner is not None:
            dbg = SwdDebugger(shell_host=self.host, runner=self._runner)
            try:
                val = dbg.dpidr()
            except (SwdError, OSError, ConnectFailed) as exc:
                return ProbeResult(spec.name, "unreachable", detail=f"dpidr failed: {exc}")
            return ProbeResult(
                spec.name, "ok", detail=f"DPIDR={_hexword(val)}", value=val
            )
        # No runner: cannot reach the board board-free. Hand back the argv the
        # probe WOULD run, but do not claim liveness.
        argv = SwdDebugger(shell_host=self.host, dry_run=True).dpidr()
        return ProbeResult(
            spec.name,
            "declined",
            detail="no runner: board-free dry-run argv preview only",
            value=argv,
        )

    def _probe_csr(self, spec: EndpointSpec) -> ProbeResult:
        """CSR: read ``DFXCTL.RM_ID`` over xsdb — value -> 'ok', no target ->
        'unreachable'.

        Note (critique fix #4): ``rm_id()`` reads the always-instantiated
        DFXCTL block, so it probes whether the register plane is reachable; it
        deliberately does NOT read the RESERVED/not-instantiated blocks
        (``vphy``/``genchk``/``clcdkvm``), which are surfaced in ``detail``
        rather than assumed to respond.
        """
        regs = self.registers()
        reserved = "RESERVED" in (spec.notes or "").upper()
        try:
            val = regs.rm_id()
        except TargetNotFound as exc:
            return ProbeResult(spec.name, "unreachable", detail=f"no target: {exc}")
        except (OSError, RuntimeError, ValueError) as exc:
            return ProbeResult(
                spec.name, "unreachable", detail=f"register read failed: {exc}"
            )
        detail = f"rm_id={_hexword(val)} (register plane reachable)"
        if reserved:
            detail += (
                "; NOTE this block is RESERVED/not-instantiated on the shipped "
                "static — rm_id was read via DFXCTL, not this block"
            )
        return ProbeResult(spec.name, "ok", detail=detail, value=val)
