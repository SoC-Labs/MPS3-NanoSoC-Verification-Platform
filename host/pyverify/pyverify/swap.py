"""Swap orchestrator — ARCHITECTURE_SPEC.md §6.2 (swap sequence) / §6.3
(re-attach).

Sequences a full overlay deploy: **validate -> swap -> push -> await -> re-attach
-> persist** (the swap RPC is sent first and PARKS, the pair is pushed into it, the
reply is read last — see the ORDERING note below and :meth:`SwapOrchestrator.deploy`).

PERSIST (net-protocol.md v0.13, handover D1 = re-push): after a VERIFIED swap the
same pair is committed to the user microSD, so the shell reloads it at power-on.
``commit`` parks exactly like ``swap`` — ``commit_begin`` -> push clearing, then
partial, over 6910 -> ``commit_await`` — and carries the verified ``rm_id``, the
manifest's ``static_id`` and the lengths + CRC-32s of the exact bytes pushed. No
card or no ``usd`` feature skips it silently; any failure is a logged WARNING in
:attr:`SwapDeployResult.persist`, never a deploy failure.
The sequencing itself is real; the network work for each step is delegated
to injected collaborators (:class:`~pyverify.client.ShellClient` and a
pusher — see :class:`Pusher` below), both of which are themselves real
implementations awaiting only a live shell to point at.

Packaging note (RESOLVED — this used to be a flagged-for-A6 open
question): the bitstream pusher, historically a deliberately separate
sibling directory (``host/pusher/push.py``) reachable only via the
structural :class:`Pusher` protocol, is now part of this package as
:mod:`pyverify.pusher` (W-PKG, docs/NEXT_WAVE_PLAN.md). ``host/pusher/
push.py`` survives only as a thin re-export shim for path-based
consumers. The :class:`Pusher` Protocol below is *kept* — no longer as a
package boundary, but as the dependency-injection seam that lets tests
(and :class:`pyverify.board.Mps3Board`) substitute fake pushers without
monkeypatching.

ORDERING — RESOLVED ON SILICON (2026-07-14). This module used to *assume* the
host pushes the pair **ahead of** the ``swap`` RPC, and said so with a caveat:
"confirm with A3 before this is load-bearing." **That assumption was wrong.**
The real sequence, verified end-to-end against the KU115 (led swapped over
Ethernet, ``DFXCTL.RM_ID`` = 0x0100001E), is:

    swap_begin()  ->  push_pair()  ->  swap_await()

The shell only listens for the bitstream on 6910 once the ``swap`` RPC has driven
its FSM into AWAIT_INCOMING_CLEARING / AWAIT_PARTIAL — which is precisely *why* it
PARKS the 6900 control connection for the whole reconfiguration. Push first and the
shell is not expecting data: it **resets** the 6910 connection (ECONNRESET, its
``diag.got`` counter stays 0), and the swap then reports ``"swap failed"`` because
no partial was ever staged.

The bug survived because :class:`pyverify.testing.fakeshell.FakeShell` encoded the
same wrong assumption (it accepts a push in any state), so the end-to-end tests were
green against a fake **more permissive than the firmware**. The fake now enforces the
real ordering.

A real board additionally needs the matched pair on both sides — firmware built
``HWICAP_FIFO=1 WINDOWED=1`` and a windowed pusher (``BitstreamPusher(windowed=True)``);
a fire-and-hose push starves the single-threaded MicroBlaze's RX poll and the shell
resets the connection. See ``docs/OVER_THE_WIRE_DEPLOY_STATUS.md``.
"""
from __future__ import annotations

import dataclasses
import logging
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from .client import CommitResponse, ShellClient, SwapResponse
from .overlay import Overlay, OverlayValidationError

__all__ = [
    "Pusher",
    "XvcHolder",
    "SwapError",
    "ReattachPlan",
    "ReattachExecutor",
    "SwapDeployResult",
    "SwapOrchestrator",
    "PersistResult",
    "PERSIST_COMMITTED",
    "PERSIST_SKIPPED",
    "PERSIST_FAILED",
    "PERSIST_OFF",
]

_log = logging.getLogger(__name__)

#: :attr:`PersistResult.status` values.
PERSIST_COMMITTED = "committed"   # the pair is on the card; `slot` says where
PERSIST_SKIPPED = "skipped"       # nothing was sent (no card, no `usd` feature, ...)
PERSIST_FAILED = "failed"         # a commit was attempted and did not land
PERSIST_OFF = "off"               # deploy(persist=False)


@runtime_checkable
class Pusher(Protocol):
    """Structural interface this orchestrator needs from a bitstream
    pusher. Satisfied by :class:`pyverify.pusher.BitstreamPusher` (the
    real one) and by any test fake with a matching ``push_pair`` — kept as
    the injection seam even now that the real pusher lives inside this
    package (see module docstring).
    """

    def push_pair(self, overlay: Overlay, *, rm_slot: int = 0) -> tuple[Any, Any]:
        ...


@runtime_checkable
class XvcHolder(Protocol):
    """What the orchestrator needs from a live Vivado XVC session
    (:class:`pyverify.debug.XvcSession`): close the target before a swap,
    reopen it with the new RM's probes after one."""

    def close_target(self) -> bool:
        ...

    def reattach(self, ltx_path: Optional[str]) -> Any:
        ...


class SwapError(Exception):
    """Any step of validate -> swap -> push -> await failed."""


#: One re-attach step: takes the plan, returns that step's outcome (opened
#: console readers, a hint string, a subprocess result, ...).
ReattachExecutor = Callable[["ReattachPlan"], Any]


@dataclass(frozen=True)
class ReattachPlan:
    """Client-side re-attach steps after a swap (spec §6.3).

    Descriptive first (the fields tell the caller what to do and in what
    shape, based on what the manifest/response carried), and now also
    *executable* via :meth:`apply` — with injectable per-step executors,
    because each step needs a different tool: reloading the ``.ltx`` is a
    Vivado/XVC action (:class:`pyverify.debug.XvcTarget`), the SWD line
    reset + DP connect is an OpenOCD action
    (:class:`pyverify.debug.OpenOcdRemoteBitbangConfig`), and only the
    console reopen is trivially a fresh TCP connect
    (:class:`pyverify.console.ConsoleReader`). :meth:`apply`'s defaults
    reflect exactly that split: console reopen is done for real, the
    tool-bound steps return their hint unless the caller injects a real
    executor.
    """

    ltx_path: str | None
    swd_reconnect_hint: str
    console_reopen_ports: tuple[int, ...]
    vphy_relink_hint: str
    #: The shell host the swap ran against — recorded so :meth:`apply`'s
    #: default console executor knows where to reconnect. ``None`` (e.g. a
    #: hand-built plan, or an injected client without a ``host``) degrades
    #: the console step to returning the ports as a hint.
    shell_host: str | None = None
    #: The XVC session the orchestrator CLOSED before the swap RPC (see
    #: :class:`SwapOrchestrator`), or ``None``. When set, :meth:`apply`'s
    #: default ``ltx`` step REOPENS it and loads :attr:`ltx_path` -- the
    #: re-attach is then done, not described.
    xvc: Any = field(default=None, compare=False, repr=False)

    #: Step names :meth:`apply` recognises, in execution order (spec §6.3's
    #: three tool-specific steps + the VPHY re-link nudge).
    STEP_NAMES = ("ltx", "swd", "console", "vphy")

    # -- default executors -------------------------------------------------- #

    def _default_ltx(self) -> Any:
        """With an XVC session recorded (:attr:`xvc`): REOPEN the target and
        load :attr:`ltx_path` (``None`` = reopen with no probes: the new RM
        carries no ILA) and return what :meth:`XvcSession.reattach` reports.
        Without one: the descriptive hint, as before."""
        if self.xvc is not None:
            return self.xvc.reattach(self.ltx_path)
        if self.ltx_path is None:
            return None
        return (
            f"reload probes file {self.ltx_path!r} over XVC — see "
            "pyverify.debug.XvcSession.reattach() -- or pass SwapOrchestrator(xvc=...) "
            "and this step does it -- or XvcTarget.refresh_ila_tcl() with "
            "launch_vivado_xvc_tcl() for a one-shot batch run"
        )

    def _default_swd(self) -> str:
        """Return-hint default: the DP reconnect needs OpenOCD."""
        return self.swd_reconnect_hint

    def _default_console(self) -> Any:
        """Real default: consoles reopen with a plain TCP connect. Returns
        a tuple of connected :class:`~pyverify.console.ConsoleReader`\\ s
        (caller owns closing them); degrades to returning the port tuple
        as a hint when no ``shell_host`` was recorded."""
        if self.shell_host is None:
            return self.console_reopen_ports
        from .console import ConsoleReader  # local: avoid import cycle at module load

        readers = []
        try:
            for port in self.console_reopen_ports:
                readers.append(ConsoleReader(self.shell_host, port).connect())
        except Exception:
            for r in readers:
                r.close()
            raise
        return tuple(readers)

    def _default_vphy(self) -> str:
        """Return-hint default: re-linking is a live control-channel verb
        (``client.link(event="up")``) the caller times deliberately."""
        return self.vphy_relink_hint

    def apply(
        self, executors: Mapping[str, ReattachExecutor] | None = None,
    ) -> dict[str, Any]:
        """Execute the re-attach steps, returning ``{step: outcome}``.

        ``executors`` overrides any subset of :attr:`STEP_NAMES` with a
        callable taking this plan. Defaults: ``console`` reopens the TCP
        consoles for real (when :attr:`shell_host` is known); ``ltx``,
        ``swd`` and ``vphy`` return their descriptive hint, since doing
        them for real requires Vivado/OpenOCD/a live control channel the
        caller must supply (inject e.g.
        ``{"swd": lambda plan: launch_openocd(cfg)}``).
        """
        defaults: dict[str, ReattachExecutor] = {
            "ltx": lambda plan: plan._default_ltx(),
            "swd": lambda plan: plan._default_swd(),
            "console": lambda plan: plan._default_console(),
            "vphy": lambda plan: plan._default_vphy(),
        }
        overrides = dict(executors) if executors else {}
        unknown = sorted(set(overrides) - set(self.STEP_NAMES))
        if unknown:
            raise ValueError(
                f"unknown re-attach step(s) {unknown}; valid: {list(self.STEP_NAMES)}"
            )
        merged = {**defaults, **overrides}
        return {name: merged[name](self) for name in self.STEP_NAMES}


@dataclass(frozen=True)
class PersistResult:
    """What :meth:`SwapOrchestrator.deploy`'s persist step did (net-protocol.md
    v0.13, handover D1 = re-push).

    ``status`` is one of :data:`PERSIST_COMMITTED` (``slot`` holds the card
    slot), :data:`PERSIST_SKIPPED` (nothing was sent: no card, no ``usd``
    feature, a foreign card ...; ``reason`` says which), :data:`PERSIST_FAILED`
    (a commit was sent and did not land; ``err`` is the shell's error NAME, or
    the host-side failure) or :data:`PERSIST_OFF` (``persist=False``).

    A failure here never fails the deploy: the swap already succeeded, and the
    previous default on the card is left intact by the shell."""

    status: str
    slot: str = ""
    reason: str = ""
    err: str = ""
    #: True when this outcome was logged as a WARNING (a failure, or a skip the
    #: operator should act on, e.g. a foreign card). False for the silent skips
    #: (no card, no ``usd`` feature) and for success.
    warning: bool = False

    @property
    def committed(self) -> bool:
        return self.status == PERSIST_COMMITTED


@dataclass(frozen=True)
class SwapDeployResult:
    rm_id: str
    verified: bool
    reattach: ReattachPlan
    #: The persist step's outcome (v0.13). ``None`` only for a result built by
    #: hand; :meth:`SwapOrchestrator.deploy` always fills it.
    persist: Optional[PersistResult] = None


class SwapOrchestrator:
    """Sequences an overlay deploy end to end.

    Parameters
    ----------
    client:
        A connected :class:`~pyverify.client.ShellClient`.
    pusher:
        Anything satisfying :class:`Pusher` — typically
        ``pusher.push.BitstreamPusher(host=...)``.
    """

    #: TCP ports the UART0/UART1/SWO consoles listen on (net-protocol.md).
    CONSOLE_PORTS: tuple[int, ...] = (6930, 6931, 6932)

    def __init__(
        self, client: ShellClient, pusher: Pusher, *, xvc: Optional[XvcHolder] = None,
        commit_pusher: Optional[Pusher] = None,
    ) -> None:
        self._client = client
        self._pusher = pusher
        #: An open :class:`pyverify.debug.XvcSession`, or None. When given, its
        #: target is CLOSED before the swap RPC and the returned plan's ``ltx``
        #: step reopens it (handover §4.10: a swap invalidates XVC).
        self._xvc = xvc
        #: The pusher a ``commit`` pushes through. ``commit`` takes its pair over
        #: 6910 ONLY (net-protocol.md v0.13: ``src`` is ``"tcp"``), so a TFTP
        #: swap pusher cannot be reused. ``None`` derives one: see
        #: :meth:`_commit_pusher_for`.
        self._commit_pusher = commit_pusher

    def deploy(
        self, overlay: Overlay, *, rm_slot: int = 0, src: str = "tftp",
        persist: bool = True,
    ) -> SwapDeployResult:
        """Validate -> push clearing+partial -> ``swap`` RPC -> re-attach plan
        -> persist.

        Raises :class:`SwapError` (chaining the original exception) if
        validation fails or the shell rejects the swap.

        ``persist`` (default ``True``, handover D1): after a VERIFIED swap,
        commit the same pair to the user microSD so the shell reloads it at
        power-on (:meth:`commit`). Skipped silently when the shell has no
        ``usd`` feature or no card is in (``usd.present`` false). A commit
        failure is a logged WARNING and lands in the result's ``persist``; it
        never fails the deploy. ``persist=False`` opts out.
        """
        # 1. Learn the shell's static_id from a live ping; overlay-manifest.md
        #    requires the manifest's static_id to match the *running* shell
        #    ("a shell rebuild invalidates every stored partial").
        ping = self._client.ping()
        expected_static_id: int | None
        if not ping.shell_id:
            # The shell reported no id at all — no info to check against.
            # (net-protocol.md says ping always returns shell_id, so this is
            # itself unusual, but it is not the dangerous case below.)
            expected_static_id = None
        else:
            try:
                expected_static_id = int(ping.shell_id, 0)
            except ValueError as exc:
                # The shell reported a NON-EMPTY but unparseable shell_id.
                # Silently skipping static_id validation here (the old
                # behaviour) would push a partial to a shell whose static we
                # could not verify — exactly what overlay-manifest.md's
                # static_id rule exists to prevent. Fail loud instead.
                raise SwapError(
                    f"shell reported an unparseable shell_id {ping.shell_id!r}; "
                    "refusing to deploy without a verifiable static_id "
                    "(a partial is only valid against its exact static)"
                ) from exc

        try:
            overlay.validate(expected_static_id=expected_static_id)
        except OverlayValidationError as exc:
            raise SwapError(f"overlay validation failed: {exc}") from exc

        # 2. Issue the swap RPC FIRST — WITHOUT awaiting its reply.
        #
        #    ⚠⚠ THE ORDER IS LOAD-BEARING. PROVEN ON SILICON 2026-07-14.
        #    The shell only listens for the bitstream on 6910 once this RPC has
        #    driven its swap FSM into AWAIT_INCOMING_CLEARING / AWAIT_PARTIAL —
        #    which is exactly WHY it parks the 6900 connection for the whole
        #    reconfiguration. This module used to push FIRST and swap second (the
        #    "design assumption ... confirm with A3 before this is load-bearing"
        #    flagged in the module docstring). That assumption was WRONG: a push
        #    to a shell that is not awaiting one is RESET (ECONNRESET; the shell's
        #    `diag.got` stays 0), and the swap then fails "swap failed" because no
        #    partial was ever staged. It survived because FakeShell encoded the
        #    same wrong assumption — the tests were green against a fake more
        #    permissive than the firmware.
        #    ⚠ CLOSE THE XVC TARGET FIRST. The firmware gates XVC for the whole
        #    swap: a `shift:` that arrives while gated STALLS and then runs
        #    against the NEW RM (firmware/xvc_server/xvc_server.c, swap_fsm.c;
        #    handover §2 F10, §8 trap 3), and the old RM's probe map means
        #    nothing to the new one. So no target is open across a swap; the
        #    re-attach plan reopens it once the swap is DONE. A failed swap
        #    leaves it closed -- reopening against an unknown RM is not safe.
        if self._xvc is not None:
            self._xvc.close_target()
        self._client.swap_begin(rm=overlay.manifest.rm_name, src=src)

        # 3. Push clearing then partial INTO the parked swap — the shell is now
        #    awaiting exactly this.
        #    ⚠ A real board also needs the matched pair on BOTH sides: firmware
        #    built `HWICAP_FIFO=1 WINDOWED=1`, and a windowed pusher
        #    (`BitstreamPusher(windowed=True)`). A fire-and-hose push starves the
        #    single-threaded MicroBlaze's RX poll and the shell resets the
        #    connection. See docs/OVER_THE_WIRE_DEPLOY_STATUS.md.
        #    ⚠ THE BUSY-ICAP RACE (silicon B1 v4 2026-09-25). The shell streams the
        #    OUTGOING RM's cached clearing into the ICAP before it can take the
        #    partial; a DAP RM's (nanosoc, 167,308 B) outlasts the incoming
        #    clearing's push, so the partial arrives early. 6910 parks it; TFTP
        #    rejects it at DATA block 1. The 6900 channel is parked (and
        #    single-client), so this side cannot poll stats.swap: the pusher's
        #    transport handles it -- pyverify.pusher.choose_push picks 6910 for the
        #    Linux harness, and a TFTP pusher with partial_retry_s re-pushes.
        self._pusher.push_pair(overlay, rm_slot=rm_slot)

        # 4. Collect the parked reply. This blocks for the whole server-side
        #    gate -> decouple -> clear -> load -> verify -> release sequence, so
        #    the client needs a GENEROUS timeout (the 5 s default cannot survive
        #    a real reconfiguration).
        try:
            resp: SwapResponse = self._client.swap_await()
        except OSError as exc:
            # A socket timeout/reset HERE means the control channel WAS reachable
            # (we ping'd it in step 1) but the swap did not complete in time —
            # almost always the client timeout being shorter than a real swap.
            # Left bare, cli.py mislabels an OSError as "cannot reach the control
            # channel"; that misdiagnosis cost real debugging time. Raise a
            # precise SwapError so the true cause surfaces.
            raise SwapError(
                f"swap did not complete for rm={overlay.manifest.rm_name!r} "
                f"(client timeout {getattr(self._client, 'timeout', '?')}s): {exc}. "
                "The shell parks the 6900 control connection for the entire swap, "
                "so a real reconfiguration needs a longer ShellClient(timeout=…). "
                "This is NOT an unreachable control channel."
            ) from exc
        if not resp.ok:
            raise SwapError(
                f"swap RPC rejected for rm={overlay.manifest.rm_name!r}: {resp}"
            )

        # 4. Re-attach plan (spec §6.3) — descriptive, and executable via
        #    ReattachPlan.apply() with injectable per-step executors.
        reattach = ReattachPlan(
            # RESOLVED against the overlay dir: the manifest's `ltx` is a name
            # relative to overlay/<rm>/ (overlay-manifest.md), and Vivado's
            # PROBES.FILE needs a path it can open from wherever it runs.
            ltx_path=str(overlay.ltx_path()) if overlay.ltx_path() is not None else None,
            swd_reconnect_hint=(
                "re-run SWD line reset + DP connect (read DPIDR) over the "
                "remote_bitbang endpoint; DUT must be clocked and out of "
                "reset first (see pyverify.debug.OpenOcdRemoteBitbangConfig)"
            ),
            console_reopen_ports=self.CONSOLE_PORTS,
            vphy_relink_hint=(
                "client.link(event='up') once the DUT's MAC has had time to "
                "restart PHY bring-up against the virtual PHY (spec §6.3)"
            ),
            shell_host=getattr(self._client, "host", None),
            xvc=self._xvc,
        )

        # 5. Persist (v0.13, D1 = re-push): commit the SAME pair to the user
        #    microSD. After the swap reply, so it can never slow or fail the swap.
        if persist:
            persisted = self._persist(overlay, resp, rm_slot=rm_slot)
        else:
            persisted = PersistResult(status=PERSIST_OFF, reason="persist=False")
        return SwapDeployResult(rm_id=resp.rm_id, verified=resp.verified, reattach=reattach,
                                persist=persisted)

    # ------------------------------------------------------------------ #
    # Persist: the v0.13 re-push `commit` (net-protocol.md "User microSD")
    # ------------------------------------------------------------------ #

    def commit(
        self,
        overlay: Overlay,
        *,
        rm_id: int,
        static_id: int,
        rm_slot: int = 0,
        features: "tuple[str, ...]" = (),
    ) -> CommitResponse:
        """Re-push ``overlay``'s pair into the card's inactive slot.

        ``commit_begin`` (the shell parks 6900) -> push clearing, then partial,
        over 6910 -> ``commit_await``. The lengths and CRC-32s sent are computed
        from the exact files the pusher reads, so they describe the bytes that
        cross the wire, not the manifest's claim about them. ``rm_id`` must be
        the live ``DFXCTL.RM_ID`` and ``static_id`` the shell's own: only what
        is running can be persisted.

        A push that fails (the shell refused before parking and reset the
        6910 connection, or the card went away) is not raised by itself: the
        parked reply is read anyway, because it carries the shell's reason and
        leaving it unread would desynchronise the control channel. Raises only
        when that reply cannot be read either (``OSError``), with the push
        failure chained.
        """
        clearing = Path(overlay.clearing_path()).read_bytes()
        partial = Path(overlay.partial_path()).read_bytes()
        self._client.commit_begin(  # type: ignore[attr-defined]
            overlay.manifest.rm_name,
            rm_id=rm_id,
            static_id=static_id,
            clear_len=len(clearing),
            clear_crc=zlib.crc32(clearing) & 0xFFFFFFFF,
            part_len=len(partial),
            part_crc=zlib.crc32(partial) & 0xFFFFFFFF,
            src="tcp",
        )
        push_exc: Optional[BaseException] = None
        try:
            self._commit_pusher_for(features).push_pair(overlay, rm_slot=rm_slot)
        except Exception as exc:   # PushError / OSError: read the reason below
            push_exc = exc
        try:
            resp = self._client.commit_await()  # type: ignore[attr-defined]
        except OSError as exc:
            raise OSError(
                f"commit reply not received ({exc}); the control channel may be out "
                "of step with the shell"
                + (f" (the push had already failed: {push_exc})" if push_exc else "")
            ) from (push_exc or exc)
        if push_exc is not None and resp.ok:
            # The shell says it committed a pair we failed to push. Believe the
            # push failure: report it, never a success that cannot be true.
            return CommitResponse(ok=False, err=f"push failed: {push_exc}")
        if push_exc is not None and not resp.err:
            return CommitResponse(ok=False, err=f"push failed: {push_exc}")
        return resp

    def _commit_pusher_for(self, features: "tuple[str, ...]") -> Pusher:
        """The pusher for a commit: 6910 only.

        An explicit ``commit_pusher`` wins. A :class:`pyverify.pusher.
        BitstreamPusher` already on ``tcp`` is reused as-is (its windowing is
        what the swap proved). A TFTP one is re-pointed at 6910, windowed iff
        the shell reports ``windowed`` (the same rule as the deploy
        transport choice: a WINDOWED shell deadlocks on a plain push). Any
        other pusher (a test double) is trusted as given."""
        if self._commit_pusher is not None:
            return self._commit_pusher
        from .pusher import BitstreamPusher   # local: swap stays importable without it
        pusher = self._pusher
        if isinstance(pusher, BitstreamPusher):
            if pusher.transport == "tcp":
                return pusher
            return dataclasses.replace(pusher, transport="tcp",
                                       windowed="windowed" in features)
        return pusher

    def _persist(self, overlay: Overlay, resp: SwapResponse, *, rm_slot: int) -> PersistResult:
        """The persist step of :meth:`deploy`. NEVER raises (short of a
        ``KeyboardInterrupt``): every failure is a warning in the result."""
        rm = overlay.manifest.rm_name

        def skipped(reason: str, *, warn: bool) -> PersistResult:
            if warn:
                _log.warning("persist: not committing rm=%r to the user microSD: %s", rm, reason)
            else:
                _log.debug("persist: skipped for rm=%r: %s", rm, reason)
            return PersistResult(status=PERSIST_SKIPPED, reason=reason, warning=warn)

        def failed(err: str) -> PersistResult:
            _log.warning("persist: commit of rm=%r to the user microSD FAILED: %s "
                         "(the swap stands; the card keeps its previous default)", rm, err)
            return PersistResult(status=PERSIST_FAILED, err=err, warning=True)

        # Only a VERIFIED swap is persisted, under the id the shell read back.
        if not resp.verified:
            return skipped("the swap reply did not say verified", warn=True)
        try:
            live_rm_id = int(resp.rm_id, 0)
        except (TypeError, ValueError):
            return skipped(f"the swap reply carried no usable rm_id ({resp.rm_id!r})",
                           warn=True)
        if live_rm_id != overlay.manifest.rm_id:
            return skipped(
                f"the verified rm_id 0x{live_rm_id:08x} is not the manifest's "
                f"0x{overlay.manifest.rm_id:08x}", warn=True)

        # Does this shell have the store, and is a card in?
        version_fn = getattr(self._client, "version", None)
        usd_fn = getattr(self._client, "usd", None)
        if version_fn is None or usd_fn is None:
            return skipped("the client cannot ask for version/usd", warn=False)
        try:
            ver = version_fn()
            features = tuple(ver.features) if ver.ok else ()
            if "usd" not in features:
                return skipped("the shell reports no 'usd' feature", warn=False)
            status = usd_fn()
        except Exception as exc:
            return skipped(f"could not read the card state: {exc}", warn=True)
        if not status.ok:
            return skipped(f"usd status refused: {status.err}", warn=True)
        if not status.present:
            return skipped("no card in the user microSD slot", warn=False)
        if not status.committable:
            hint = (" (a card with no 0xDA store: `pyverify usd format`)"
                    if status.state == "foreign" else "")
            return skipped(f"the card is {status.state!r} ({status.text!r}){hint}", warn=True)

        try:
            reply = self.commit(overlay, rm_id=live_rm_id,
                                static_id=overlay.manifest.static_id,
                                rm_slot=rm_slot, features=features)
        except Exception as exc:
            return failed(f"{type(exc).__name__}: {exc}")
        if not reply.ok:
            return failed(reply.err or "commit refused")
        _log.info("persist: rm=%r committed to user-microSD slot %s", rm, reply.slot)
        return PersistResult(status=PERSIST_COMMITTED, slot=reply.slot)
