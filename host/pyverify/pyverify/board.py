"""``Mps3Board`` — the "PYNQ experience" session facade (W-PKG,
docs/NEXT_WAVE_PLAN.md; ARCHITECTURE_SPEC.md §14 phase 10).

One object that composes the whole host stack the way a PYNQ user holds a
``pynq.Overlay``: connect once, then **select DUT -> load firmware -> run
test -> check** as method calls::

    from pyverify import Mps3Board

    with Mps3Board("192.168.10.101") as board:
        board.ping()                          # shell_id / current rm_id
        result = board.deploy("nanosoc")      # validate -> swap -> push -> await
        result.reattach.apply()               # reopen consoles (+ hints)
        board.uart0.assert_contains(b"nanosoc boot", timeout=15.0)
        telem = board.telemetry()             # lockup pin (no power sensor)

Under the hood this is nothing new — :class:`~pyverify.client.ShellClient`
(control channel, TCP 6900), :class:`~pyverify.overlay.Overlay` (manifest
load/validate), :class:`~pyverify.pusher.BitstreamPusher` (TFTP/raw-TCP
push), :class:`~pyverify.swap.SwapOrchestrator` (sequencing) and
:class:`~pyverify.console.ConsoleReader` (6930-6932) — wired together with
the same defaults ``python -m pyverify.cli deploy`` uses. Every
collaborator is injectable (``client=`` / ``pusher=`` /
``console_factory=``) so the facade is unit-testable with zero sockets —
see ``tests/test_board.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

from .client import (
    CONTROL_PORT,
    DEFAULT_CLK_PRESETS,
    DISPLAY_OWNERS,
    MACGEN_INJECTS,
    CommitResponse,
    DisplayResponse,
    LinkResponse,
    MacGenResponse,
    PingResponse,
    ResetResponse,
    SetClkResponse,
    ShellClient,
    TelemetryResponse,
    UsdResponse,
    validate_clk_preset,
    validate_display_owner,
    validate_macgen_inject,
)
from .console import SWO_PORT, UART0_PORT, UART1_PORT, ConsoleReader
from .mactest import MacTestResult, run_mac_test
from .overlay import Overlay
from .pusher import RAW_TCP_PORT, TFTP_PORT, BitstreamPusher, choose_push
from .swap import Pusher, SwapDeployResult, SwapOrchestrator

__all__ = ["Mps3Board"]

#: Anything that builds a console reader: ``(host, port) -> ConsoleReader``-ish
#: (needs ``connect()``/``close()`` and the read/assert API).
ConsoleFactory = Callable[[str, int], Any]


class Mps3Board:
    """Session facade over one shell + its reconfigurable DUT slot.

    Parameters
    ----------
    host:
        Shell host/IP (net-protocol.md; default static ``192.168.10.101``).
    control_port / tftp_port / tcp_push_port:
        The net-protocol.md port map, overridable per-board (mostly for
        pointing at a fake shell on localhost ephemeral ports).
    transport:
        Bitstream push transport: ``"auto"`` (default), ``"tftp"`` or ``"tcp"``.
        ``"auto"`` asks the running shell on the first :meth:`deploy`: a
        shell whose ``version.features`` has ``windowed`` gets a windowed
        tcp push with ``src="tcp"`` (a plain push deadlocks it); any other
        shell gets tftp, the historic default.
    overlay_root:
        Directory :meth:`deploy` resolves bare overlay *names* against
        (``overlay_root/<name>/manifest.json`` — overlay-manifest.md
        "Directory layout").
    client / pusher / console_factory:
        Injection seams for tests; ``None`` builds the real thing.
    """

    def __init__(
        self,
        host: str,
        *,
        control_port: int = CONTROL_PORT,
        tftp_port: int = TFTP_PORT,
        tcp_push_port: int = RAW_TCP_PORT,
        transport: str = "auto",
        timeout: float = 5.0,
        overlay_root: Union[str, Path] = Path("overlay"),
        client: Optional[ShellClient] = None,
        pusher: Optional[Pusher] = None,
        console_factory: ConsoleFactory = ConsoleReader,
        xvc: Optional[Any] = None,
    ) -> None:
        self.host = host
        #: An optional :class:`pyverify.debug.XvcSession`. When set, every
        #: :meth:`deploy` closes its target before the swap RPC, and the
        #: result's ``reattach.apply()`` reopens it with the new RM's ``.ltx``.
        self.xvc = xvc
        self.control_port = control_port
        self.timeout = timeout
        self.overlay_root = Path(overlay_root)
        self._client = client if client is not None else ShellClient(
            host, port=control_port, timeout=timeout,
        )
        self._tftp_port, self._tcp_push_port = tftp_port, tcp_push_port
        #: the src the auto transport chose (None until chosen / when explicit)
        self._auto_src: Optional[str] = None
        self._pusher: Optional[Pusher]
        if pusher is not None:
            self._pusher = pusher
        elif transport == "auto":
            self._pusher = None  # built on the first deploy, from version.features
        else:
            self._pusher = BitstreamPusher(
                host=host,
                transport=transport,  # type: ignore[arg-type]
                tftp_port=tftp_port,
                tcp_port=tcp_push_port,
            )
        self._console_factory = console_factory
        self._consoles: "dict[int, Any]" = {}
        self.last_deploy: Optional[SwapDeployResult] = None

    # -- session lifecycle -------------------------------------------------- #

    def connect(self) -> "Mps3Board":
        self._client.connect()
        return self

    def close(self) -> None:
        for console in self._consoles.values():
            try:
                console.close()
            except Exception:
                pass  # closing a stale console must not mask the real teardown
        self._consoles.clear()
        self._client.close()

    def __enter__(self) -> "Mps3Board":
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- select DUT + load firmware (the inner loop) ------------------------ #

    def deploy(
        self,
        overlay: Union[Overlay, str, Path],
        *,
        overlay_root: "Union[str, Path, None]" = None,
        rm_slot: int = 0,
        src: Optional[str] = None,
        persist: bool = True,
    ) -> SwapDeployResult:
        """Validate -> ``swap`` RPC -> push clearing+partial -> await -> re-attach
        -> persist.

        The order is load-bearing (see :meth:`pyverify.swap.SwapOrchestrator.deploy`):
        the swap RPC is sent first and PARKS the control connection, the pair is
        pushed INTO the parked swap, and only then is the reply read. Pushing
        before the swap hits a shell that is not listening and is reset.

        ``overlay`` may be a loaded :class:`~pyverify.overlay.Overlay`, a
        path to an overlay directory, or a bare RM name resolved against
        ``overlay_root`` (defaulting to this board's). The returned
        :class:`~pyverify.swap.SwapDeployResult`'s ``.reattach`` plan is
        executable (``result.reattach.apply()``) and is also stored on
        :attr:`last_deploy`. Consoles opened before the swap are stale
        afterwards — re-attach reopens them (or use :meth:`reopen_consoles`).

        ``persist`` (default ``True``, net-protocol.md v0.13): after a verified
        swap, the same pair is committed to the user microSD so the board
        reloads it at power-on. No card, or a shell without the ``usd`` feature,
        skips it silently; a commit failure is a warning in
        ``result.persist``, never a deploy failure. ``persist=False`` opts out.
        """
        resolved = self._resolve_overlay(overlay, overlay_root)
        if self._pusher is None:
            self._pusher = self._auto_pusher()
        if src is None:
            src = self._auto_src or "tftp"
        orchestrator = SwapOrchestrator(self._client, self._pusher, xvc=self.xvc)
        result = orchestrator.deploy(resolved, rm_slot=rm_slot, src=src, persist=persist)
        self.last_deploy = result
        return result

    def _auto_pusher(self) -> Pusher:
        """transport="auto": ask the shell what it is (the ONE rule,
        :func:`pyverify.pusher.choose_push`, which ``pyverify deploy`` uses
        too): ``windowed`` => tcp + windowed; ``impl`` linux => tcp, plain,
        30 s inactivity limit (6910 parks a partial that arrives while the
        outgoing clearing still streams into the ICAP; TFTP rejects it); else
        tftp."""
        choice = choose_push(self._client)
        self._auto_src = choice.src
        return choice.pusher(self.host, tftp_port=self._tftp_port,
                             tcp_port=self._tcp_push_port)

    def _resolve_overlay(
        self,
        overlay: Union[Overlay, str, Path],
        overlay_root: "Union[str, Path, None]",
    ) -> Overlay:
        if isinstance(overlay, Overlay):
            return overlay
        root = Path(overlay_root) if overlay_root is not None else self.overlay_root
        candidate = Path(overlay)
        # An existing directory wins (explicit path); anything else is
        # treated as a bare RM name under overlay_root, so the error
        # message on a miss names the root-resolved path that was tried.
        if candidate.is_dir():
            return Overlay.load(candidate)
        return Overlay.load(root / str(overlay))

    # -- consoles (net-protocol.md 6930-6932) -------------------------------- #

    def _console(self, port: int) -> Any:
        if port not in self._consoles:
            reader = self._console_factory(self.host, port)
            reader.connect()
            self._consoles[port] = reader
        return self._consoles[port]

    @property
    def uart0(self) -> Any:
        """UART0 (boot monitor) console reader, connected lazily and cached."""
        return self._console(UART0_PORT)

    @property
    def uart1(self) -> Any:
        """UART1 (application) console reader, connected lazily and cached."""
        return self._console(UART1_PORT)

    @property
    def swo(self) -> Any:
        """SWO/ITM trace console reader, connected lazily and cached."""
        return self._console(SWO_PORT)

    def reopen_consoles(self) -> None:
        """Drop every cached console connection (they are stale after a
        swap — spec §6.3); the next accessor use reconnects fresh."""
        for console in self._consoles.values():
            try:
                console.close()
            except Exception:
                pass
        self._consoles.clear()

    # -- control-channel passthroughs (net-protocol.md verbs) ---------------- #

    def ping(self) -> PingResponse:
        return self._client.ping()

    def reset(self, target: str = "dut") -> ResetResponse:
        return self._client.reset(target)

    def set_clk(
        self,
        preset: str,
        *,
        presets: "Optional[Sequence[str]]" = DEFAULT_CLK_PRESETS,
    ) -> SetClkResponse:
        """Set the DUT clock preset, validated client-side **by default**
        against the firmware's placeholder table
        (:data:`pyverify.client.DEFAULT_CLK_PRESETS`, mirroring
        ``firmware/clkrst/clkrst.c``; enumeration is still open as
        docs/contracts/OPEN_ISSUES.md I16 — A6-owned, tracked there, not
        here). ``ValueError`` on a typo *before* any traffic; escape hatch
        ``presets=None`` (or a custom sequence) so the closed set can
        evolve firmware-side ahead of this client.

        Validation happens here, once, against the *caller's* set
        (:func:`pyverify.client.validate_clk_preset`); the underlying
        client is called with the plain single-argument shape —
        ``ShellClient.set_clk`` is wire-transparent by default (see its
        docstring for why), and injected duck-typed clients need nothing
        beyond ``set_clk(preset)``.
        """
        validate_clk_preset(preset, presets)
        return self._client.set_clk(preset)

    def link(self, event: str) -> LinkResponse:
        return self._client.link(event)

    def commit(self, rm: str) -> CommitResponse:
        """The RETIRED v0.11 ``commit`` form, passed through unchanged (a v0.13
        shell answers ``bad args``). Persisting is now part of :meth:`deploy`
        (``persist=True``), which sends the v0.13 re-push."""
        return self._client.commit(rm)

    def usd(self) -> "UsdResponse":
        """The user microSD and its overlay store (net-protocol.md v0.13
        ``usd``). No card is ``ok`` with ``present=False``."""
        return self._client.usd()

    def set_display_owner(
        self,
        owner: str,
        *,
        owners: "Optional[Sequence[str]]" = DISPLAY_OWNERS,
    ) -> DisplayResponse:
        """Flip the on-board CLCD panel's owner remotely, over the shell's 6900
        control channel (net-protocol.md ``display``) — the same frozen
        ``CLCDKVM.CTRL.src_sel`` the ``USER_nPB[1]`` button and a local CSR
        write drive. ``owner`` is one of :data:`~pyverify.client.DISPLAY_OWNERS`
        (``"harness"`` / ``"dut"`` / ``"toggle"``).

        Validated **by default** against that set (``ValueError`` on a typo
        before any traffic; ``owners=None`` sends it verbatim) — same opt-out
        shape as :meth:`set_clk`/:meth:`macgen`. The reply carries the
        **committed** owner, which can lag the request by the KVM handover
        (~7–9 ms); use :meth:`display_owner` to confirm the landing. On a board
        whose bitstream has no KVM slave the reply is ``ok=False`` /
        ``err="clcd_kvm not present"``."""
        validate_display_owner(owner, owners)
        return self._client.display(owner)

    def display_owner(self) -> DisplayResponse:
        """Report the CLCD panel's current committed owner without moving it
        (read-only ``display``; net-protocol.md). See
        :class:`~pyverify.client.DisplayResponse`."""
        return self._client.display_owner()

    def telemetry(self) -> TelemetryResponse:
        """The DUT-lockup pin. **Always returns ``ok=False``** — there is no
        power sensor on this platform, so the verb has no success shape and
        :class:`~pyverify.client.TelemetryResponse` has no ``mv``/``ma``
        fields (net-protocol.md v0.6). See :meth:`ShellClient.telemetry`."""
        return self._client.telemetry()

    def macgen(
        self,
        *,
        gen: bool = True,
        chk: bool = True,
        inject: str = "none",
        injects: "Optional[Sequence[str]]" = MACGEN_INJECTS,
    ) -> MacGenResponse:
        """Drive the shell's error-inject gen/checker (GENCHK) and read its
        counters (net-protocol.md ``macgen``; spec §8). Inject fault name is
        validated **by default** against
        :data:`~pyverify.client.MACGEN_INJECTS` (``ValueError`` on a typo
        before any traffic; ``injects=None`` sends it verbatim) — same
        opt-out shape as :meth:`set_clk`. See :meth:`mac_test` for the
        scripted scenario."""
        validate_macgen_inject(inject, injects)
        return self._client.macgen(gen=gen, chk=chk, inject=inject)

    def mac_test(
        self,
        *,
        inject: str = "bad_fcs",
        clean_rounds: int = 2,
        link_events: Sequence[str] = ("down", "up"),
    ) -> MacTestResult:
        """Run the MAC-in-operation scenario (:func:`pyverify.mactest.run_mac_test`):
        clean traffic advances the counters with no errors, an injected fault
        raises ``ERR_CNT``, and a VPHY link blip is accepted. Returns a
        :class:`~pyverify.mactest.MacTestResult` (``passed`` + snapshots)."""
        return run_mac_test(
            self._client, inject=inject, clean_rounds=clean_rounds,
            link_events=link_events,
        )
