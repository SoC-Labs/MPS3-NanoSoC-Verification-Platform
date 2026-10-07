"""MPS3 Edge Device API model — ``docs/HARDWARE_HUB_INTEGRATION.md``.

Resolves OPEN_ISSUES **I3**: gives ``pyverify``/``tender`` a real, in-process
model of the per-board **Edge Device API** ("LOW" tier) the Hardware Hub
spec (``nanosoc-multicore-system/docs/hardwarehub.md`` §3/§5) defines, so the
MPS3/KU115 board can be enumerated, status-polled, and reset exactly like the
Hub's existing Zynq fleet.

Three pieces of *real* logic live here, none of which need a socket, a
subprocess, or a board to be correct:

1. :func:`EdgeDeviceApi.enumerate_channels` — the §2 channel table
   (``console``/``dut-uart``/``swo``/``mgmt``/``dut-net``/``jtag``/``xvc``/
   ``swd``), each carrying the MPS3-extended ``handle``/``type``/``gated_by``
   enums (hardwarehub.md §5.2 ``Channel``, widened per integration-doc §2),
   with the ``jtag``<->``xvc`` shared-FT2232-channel conflict marked both
   ways.
2. :meth:`EdgeDeviceApi.status_fpga` — the two-level ``FpgaStatus``
   (integration-doc §3): ``shell.loaded`` derived from a **stubbed** JTAG-DONE
   probe (no real JTAG/OpenOCD session opens here — see
   :func:`stub_jtag_done_probe`), ``rp.loaded``/``rm_id`` derived from the
   **real** shell control-channel client (:class:`pyverify.client.ShellClient`
   -- ``{"op":"ping"}`` -> ``rm_id``, exactly as net-protocol.md and
   integration-doc §3 describe). The ``rp`` half is authoritative and needs no
   JTAG; the ``shell`` half is honestly reported as "unknown" until a real
   probe is injected.
3. :func:`reset` / :meth:`EdgeDeviceApi.reset` — the §4 reset taxonomy
   (``mcc-reconfig``, ``usb-power``, ``dfx-swap``, ``dut-reset``,
   ``uart-soft``, ``system``) and the **RP-scoped vs. all** invalidation-set
   computation that is the whole point of the DFX-shell split: a
   ``dfx-swap`` invalidates the RP-derived channels
   (``swd``, ``dut-net``, ``dut-uart``, ``swo`` — every ``gated_by=rp``
   channel) plus ``xvc`` (shell-gated, but the ILAs behind it live in the
   RM, so the Vivado session must be closed and reopened with the new
   ``.ltx``), while ``mcc-reconfig``/``usb-power`` (the two "rebuild
   everything" kinds) invalidate every channel.

:meth:`EdgeDeviceApi.dispatch` maps these onto the JSON-RPC 2.0 method names
of hardwarehub.md §5.3 (``enumerate.channels``, ``status.fpga``, ``reset``)
so a real ``edge-wrapperd``-equivalent could serve them over
``WRAPPER_SOCK`` unchanged; this module itself never binds a socket or
verifies a grant — that stays the wrapper/arbiter's job (out of scope here,
same "stub only I/O" line every other pyverify module draws).

Open questions flagged for A6 (see also the file-level comments where each
is decided):

- **"dut-uart" vs. "console".** integration-doc §2's channel table folds
  UART0 (boot monitor, 6930) and UART1 (application, 6931) into one
  ``…console`` channel, ``gated_by: shell``. But §4's reset table names
  ``dut-uart`` as one of exactly three channels a ``dfx-swap`` invalidates
  (``swd``, ``dut-net``, ``dut-uart``) — a channel id that doesn't exist in
  §2's table at all, and describes something RP-gated, not shell-gated. The
  only reading that makes §4 literally satisfiable is that UART1
  (application/DUT console) is actually its own RP-gated channel distinct
  from the shell's boot-monitor console on UART0 — so that's what's modelled
  here (``"console"`` = UART0, shell-gated; ``"dut-uart"`` = UART1,
  RP-gated). Confirm with A6/A3 whether §2's table should be corrected to
  split the row, or whether "console" was meant to include the invalidation
  and §4's wording is the typo.
- **``xvc``'s ``handle``.** hardwarehub.md §1.4 calls ``jtag``/``xvc`` "two
  *presentations* of one physical channel" and its base ``Channel.handle``
  enum has no ``xvc`` value at all (only ``uart``/``ps``/``pl``/``jtag``/
  ``swd``) — suggesting the ``xvc`` channel should carry ``handle: jtag``
  (shared leasable capability, different ``type``). But
  integration-doc §2's own table literally lists the xvc row's "Hub handle"
  column as ``xvc``, and its "new enum values needed" paragraph only calls
  out ``mgmt``/``dut-net`` as additions — silently assuming ``xvc`` is
  already a valid handle value, which it isn't in the upstream base schema.
  This module follows integration-doc §2's literal table (dedicated
  ``ChannelHandle.XVC``) since that document is *the* spec for this task,
  but the mutual-exclusion behaviour that actually matters operationally
  (the Hub must never lease both) is captured unambiguously either way via
  each channel's ``conflicts`` field. A6 should rule on which of the two
  ``handle`` schemes the Hub-side registry widening actually adopts.
- **``rm_name`` provenance.** ``{"op":"ping"}`` (net-protocol.md) returns
  only ``shell_id``/``rm_id`` — no ``rm_name``. integration-doc §3's example
  ``FpgaStatus`` nonetheless shows ``rp.rm_name:"nanosoc"``. Since the shell
  itself doesn't hand back a name, this module resolves ``rm_name`` from an
  injected ``known_rm_names: {rm_id: rm_name}`` map (populated, in a real
  deployment, from whichever overlay manifests have been pushed —
  ``overlay-manifest.md``'s ``rm_name`` field keyed by its ``rm_id``) rather
  than inventing a second shell RPC. Confirm with A6 whether the shell
  should grow a name-bearing field instead. The lookup keys on the DESIGN half
  of the id only (``rm_id & 0xFFFF``, :mod:`pyverify.rm_id`) — under the v2
  encoding the top half is a design VERSION, so a full-32-bit match would lose
  the name on every version bump.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .rm_id import design_id as rm_design_id

from .client import ShellProtocolError

__all__ = [
    # channel model
    "ChannelHandle",
    "ChannelType",
    "GatedBy",
    "ChannelEndpoint",
    "Channel",
    "MPS3_CHANNELS",
    "ALL_CHANNEL_IDS",
    "find_conflicts",
    "channel_to_dict",
    # fpga status model
    "JtagDoneResult",
    "stub_jtag_done_probe",
    "ShellStatusClient",
    "ShellLevelStatus",
    "RpLevelStatus",
    "FpgaStatus",
    "fpga_status_to_dict",
    # reset model
    "ResetKind",
    "ResetResult",
    "reset",
    "reset_result_to_dict",
    # board identity
    "DEFAULT_BOARD_NAME",
    "BOARD_SOC",
    "is_valid_board_name",
    # the API surface
    "EdgeApiError",
    "EdgeDeviceApi",
]


# --------------------------------------------------------------------------- #
# Board identity (hardwarehub.md §5.2 `Board`, widened per integration-doc §2:
# "Board.name pattern widened from ^pynq_z2_[0-9]+$ to include ^mps3_[0-9]+$")
# --------------------------------------------------------------------------- #

DEFAULT_BOARD_NAME = "mps3_01"
BOARD_SOC = "xcku115"
_BOARD_NAME_PATTERN = re.compile(r"^mps3_[0-9]+$")


def is_valid_board_name(name: str) -> bool:
    """True if ``name`` matches the MPS3 board-name pattern (integration-doc
    §2). Does not require the board actually exist in any registry — this
    is pure string-shape validation, the same "widened pattern" the Hub's
    ``Board`` schema needs to accept alongside ``^pynq_z2_[0-9]+$``.
    """
    return bool(_BOARD_NAME_PATTERN.match(name))


# --------------------------------------------------------------------------- #
# Channel model (hardwarehub.md §5.2 `Channel`, extended per
# HARDWARE_HUB_INTEGRATION.md §2)
# --------------------------------------------------------------------------- #


class ChannelHandle(str, Enum):
    """Leasable capability a channel presents (hardwarehub.md §5.2
    ``Channel.handle``). Base Hub enum: ``uart``/``ps``/``pl``/``jtag``/
    ``swd``. MPS3-only additions (integration-doc §2): ``mgmt``, ``dut-net``.
    ``ps``/``pl`` are Zynq-only concepts (RTL PS GEM0 / soft-MAC over the PL)
    with no MPS3 analog — omitted here; ``mgmt``/``dut-net`` are their MPS3
    replacements (shell control plane / DUT MAC plane respectively). ``xvc``
    is carried as its own handle value per integration-doc §2's literal
    table — see the module docstring's open-question note on this.
    """

    UART = "uart"
    JTAG = "jtag"
    XVC = "xvc"
    SWD = "swd"
    MGMT = "mgmt"
    DUT_NET = "dut-net"


class ChannelType(str, Enum):
    """Transport/protocol presentation of a channel (hardwarehub.md §5.2
    ``Channel.type``). MPS3-only additions (integration-doc §2):
    ``ethernet-mgmt``, ``ethernet-dut``, ``swd-over-eth``.
    """

    UART = "uart"
    JTAG = "jtag"
    XVC = "xvc"
    SWD = "swd"
    SWD_OVER_ETH = "swd-over-eth"
    ETHERNET_MGMT = "ethernet-mgmt"
    ETHERNET_DUT = "ethernet-dut"


class GatedBy(str, Enum):
    """What must be "up" for a channel to be usable (hardwarehub.md §5.2
    ``Channel.gated_by``). integration-doc §2: "``Channel.gated_by`` gains
    ``shell`` and ``rp`` (replacing the single ``bitstream``)" — the whole
    point of the persistent-shell/DFX-RP split (integration-doc §1): a
    channel gated on ``shell`` survives a ``dfx-swap``; one gated on ``rp``
    does not. ONE exception, by design: ``xvc`` is shell-gated (its server is
    shell firmware) yet a swap invalidates the session on it, because the
    debug cores it reaches are the RM's own.
    """

    SHELL = "shell"
    RP = "rp"
    NONE = "none"


@dataclass(frozen=True)
class ChannelEndpoint:
    """Where a channel is reached, once routed onto ``wg0`` by the Hub
    (hardwarehub.md §5.2 ``Channel.endpoint``). ``ports`` lists the
    net-protocol.md / OpenOCD TCP ports involved (a channel may expose more
    than one, e.g. ``jtag``'s gdb/telnet/tcl trio); ``netns`` is set only
    for the pure-L2 ``dut-net`` channel (VXLAN into ``board-<id>``, mirroring
    the Zynq ``pl``/``ps`` netns model, hardwarehub.md §4.2.2).
    """

    transport: str
    ports: tuple[int, ...] = ()
    netns: str | None = None


@dataclass(frozen=True)
class Channel:
    """One entry of ``enumerate.channels`` (hardwarehub.md §5.2 ``Channel``,
    MPS3 shape per integration-doc §2's table).
    """

    id: str
    handle: ChannelHandle
    type: ChannelType
    gated_by: GatedBy
    backing: str
    endpoint: ChannelEndpoint
    conflicts: tuple[str, ...] = ()
    notes: str = ""


def _build_channels() -> tuple[Channel, ...]:
    """The integration-doc §2 channel table, built once at import time."""
    return (
        Channel(
            id="console",
            handle=ChannelHandle.UART,
            type=ChannelType.UART,
            gated_by=GatedBy.SHELL,
            backing="shell UART-over-Eth (boot monitor) or FT4232",
            endpoint=ChannelEndpoint(transport="tcp", ports=(6930,)),
            notes=(
                "single-user; shell boot-monitor console (net-protocol.md "
                "UART0, port 6930); survives dfx-swap"
            ),
        ),
        Channel(
            id="dut-uart",
            handle=ChannelHandle.UART,
            type=ChannelType.UART,
            gated_by=GatedBy.RP,
            backing="shell UART-over-Eth (application console)",
            endpoint=ChannelEndpoint(transport="tcp", ports=(6931,)),
            notes=(
                "the RP/DUT's own application console (net-protocol.md "
                "UART1, port 6931); modelled as its own RP-gated channel so "
                "that §4's dfx-swap invalidation row (swd/dut-net/dut-uart) "
                "is literally satisfiable -- see module docstring's open "
                "question on §2 vs §4"
            ),
        ),
        Channel(
            id="swo",
            handle=ChannelHandle.UART,
            type=ChannelType.UART,
            gated_by=GatedBy.RP,
            backing="shell SWO relay",
            endpoint=ChannelEndpoint(transport="tcp", ports=(6932,)),
            notes="trace; presentation of the console family",
        ),
        Channel(
            id="mgmt",
            handle=ChannelHandle.MGMT,
            type=ChannelType.ETHERNET_MGMT,
            gated_by=GatedBy.SHELL,
            backing="LAN9220 shell services",
            endpoint=ChannelEndpoint(transport="tcp", ports=(6900, 69, 6910)),
            notes=(
                "control/status/config plane -- the always-up-once-shell "
                "channel (Zynq `ps` analog)"
            ),
        ),
        Channel(
            id="dut-net",
            handle=ChannelHandle.DUT_NET,
            type=ChannelType.ETHERNET_DUT,
            gated_by=GatedBy.RP,
            backing="virtual-PHY <-> DUT MAC <-> LAN9220",
            endpoint=ChannelEndpoint(transport="vxlan-l2", netns="board-<id>"),
            notes="the DUT's MAC-in-operation traffic (Zynq `pl` analog)",
        ),
        Channel(
            id="jtag",
            handle=ChannelHandle.JTAG,
            type=ChannelType.JTAG,
            gated_by=GatedBy.NONE,
            backing="FT2232 ch A -> KU115 config TAP",
            endpoint=ChannelEndpoint(transport="tcp", ports=(3333, 4444, 6666)),
            conflicts=("xvc",),
            notes=(
                "full-bitstream program / config truth; *is* config, so not "
                "gated by shell/rp"
            ),
        ),
        Channel(
            id="xvc",
            handle=ChannelHandle.XVC,
            type=ChannelType.XVC,
            gated_by=GatedBy.SHELL,
            backing="same FT2232 ch A -> shell Debug Bridge",
            endpoint=ChannelEndpoint(transport="tcp", ports=(2542,)),
            conflicts=("jtag",),
            notes=(
                "ILA/VIO; mutually exclusive with jtag (shared FT2232 channel A). "
                "The TCP server is SHELL firmware, but the Vivado session on it "
                "does NOT survive a dfx-swap: the ILAs live inside the RM and "
                "their probe map is per-RM (.ltx), and a shift issued mid-swap "
                "stalls and then runs against the NEW RM. Close the target "
                "before the swap, reopen + reload the new .ltx after "
                "(pyverify.debug.XvcSession, SwapOrchestrator(xvc=...))"
            ),
        ),
        Channel(
            id="swd",
            handle=ChannelHandle.SWD,
            type=ChannelType.SWD_OVER_ETH,
            gated_by=GatedBy.RP,
            backing="J-Link -> soft Cortex-M in the RP, or shell SWD-over-Eth",
            endpoint=ChannelEndpoint(transport="tcp", ports=(3333, 6920)),
            notes="two backings (physical J-Link vs in-shell probe); pick per site",
        ),
    )


#: The static integration-doc §2 channel list. Frozen at import time — the
#: MPS3 channel *shape* doesn't vary per board; a real edge's *state* (which
#: of these are currently gated/leased/down) is layered on top by whoever
#: probes live hardware, not modelled here.
MPS3_CHANNELS: tuple[Channel, ...] = _build_channels()

#: Every channel id, in table order — the "all channels" invalidation set
#: (§4: ``mcc-reconfig``/``usb-power``).
ALL_CHANNEL_IDS: tuple[str, ...] = tuple(c.id for c in MPS3_CHANNELS)


def find_conflicts(channels: Sequence[Channel] | None = None) -> dict[str, tuple[str, ...]]:
    """``{channel_id: conflicting_channel_ids}`` for every channel that has
    at least one conflict (only ``jtag``/``xvc`` today, sharing FT2232
    channel A) — the Hub must never lease both sides of an entry here.
    """
    channels = MPS3_CHANNELS if channels is None else channels
    return {c.id: c.conflicts for c in channels if c.conflicts}


def channel_to_dict(channel: Channel) -> dict[str, Any]:
    """JSON-shaped dict matching hardwarehub.md §5.2 ``Channel`` (as widened
    by integration-doc §2) — what ``enumerate.channels`` would put on the
    wire.
    """
    return {
        "id": channel.id,
        "handle": channel.handle.value,
        "type": channel.type.value,
        "gated_by": channel.gated_by.value,
        "backing": channel.backing,
        "endpoint": {
            "transport": channel.endpoint.transport,
            "ports": list(channel.endpoint.ports),
            "netns": channel.endpoint.netns,
        },
        "conflicts": list(channel.conflicts),
        "notes": channel.notes,
    }


# --------------------------------------------------------------------------- #
# Two-level FpgaStatus (integration-doc §3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JtagDoneResult:
    """What a JTAG-DONE probe reports (hardwarehub.md §3.2 priority order 1/2,
    applied to the KU115 config TAP per integration-doc §3).
    """

    done: bool
    static_id: str | None = None
    usercode: str | None = None


def stub_jtag_done_probe() -> JtagDoneResult:
    """Default (stubbed) JTAG-DONE probe.

    Real JTAG/OpenOCD is out of scope here (no hardware, no subprocess, per
    the task constraints) — the honest default answer is "we don't know
    the shell is configured", not "assume yes". Callers with a real probe
    (an OpenOCD/xsdb wrapper -- see :mod:`pyverify.debug` for the equivalent
    OpenOCD launch-wrapper pattern) inject it via
    ``EdgeDeviceApi(jtag_probe=...)``.
    """
    return JtagDoneResult(done=False, static_id=None, usercode=None)


@runtime_checkable
class ShellStatusClient(Protocol):
    """Structural seam :meth:`EdgeDeviceApi.status_fpga` needs from a shell
    control-channel client — satisfied by a connected
    :class:`pyverify.client.ShellClient` without a hard import dependency
    (mirrors the ``Pusher`` Protocol pattern in :mod:`pyverify.swap`), so
    tests can hand in a bare mock object with just a ``.ping()`` method.
    """

    def ping(self) -> Any: ...


@dataclass(frozen=True)
class ShellLevelStatus:
    """``FpgaStatus.shell`` (integration-doc §3): the static-shell
    configuration level, derived from JTAG DONE/USERCODE — never from the
    shell control channel (that would be circular: if the shell isn't
    configured, there's no control channel to ask).
    """

    loaded: bool
    derived_by: str = "jtag-done"
    static_id: str | None = None


@dataclass(frozen=True)
class RpLevelStatus:
    """``FpgaStatus.rp`` (integration-doc §3): the DFX overlay level, derived
    from the shell control channel's ``{"op":"ping"}`` -> ``rm_id`` — this is
    authoritative and needs no JTAG (integration-doc §3, point 2).
    """

    loaded: bool
    derived_by: str = "shell-control"
    rm_id: str | None = None
    rm_name: str | None = None


@dataclass(frozen=True)
class FpgaStatus:
    """``status.fpga`` result (integration-doc §3 example JSON)."""

    board: str
    shell: ShellLevelStatus
    rp: RpLevelStatus


def fpga_status_to_dict(status: FpgaStatus) -> dict[str, Any]:
    """JSON-shaped dict matching integration-doc §3's example ``status.fpga``
    response.
    """
    return {
        "board": status.board,
        "shell": {
            "loaded": status.shell.loaded,
            "derived_by": status.shell.derived_by,
            "static_id": status.shell.static_id,
        },
        "rp": {
            "loaded": status.rp.loaded,
            "derived_by": status.rp.derived_by,
            "rm_id": status.rp.rm_id,
            "rm_name": status.rp.rm_name,
        },
    }


def _rm_id_indicates_loaded(rm_id: str) -> bool:
    """An empty/all-zero ``rm_id`` means "no RM loaded" (idle/greybox state,
    integration-doc §8A.3's "greybox" ships inside the shell image and isn't
    a real RM); anything else -- numeric-nonzero or a non-numeric identifier
    the shell might use -- is treated as "an RM is loaded", honestly
    favouring "we don't recognise this id but something answered" over
    silently reporting ``False``.

    Still an all-32-bit test after the v2 re-encoding, and deliberately so:
    greybox is the ONE id held at exactly 0x00000000 (design 0 @ v0.0), and no
    real design may take design_id 0 -- so "all zero" remains exactly "nothing
    loaded" and masking here would be wrong.
    """
    if not rm_id:
        return False
    try:
        return int(rm_id, 0) != 0
    except ValueError:
        return True


def _resolve_rm_name(known_rm_names: Mapping[str, str], rm_id: str) -> str | None:
    """Resolve a board-reported ``rm_id`` to an RM name, **keyed on the design
    half only** (``rm_id & 0xFFFF``).

    Since the v2 encoding (docs/VERSIONING_PLAN.md §3.2) an rm_id carries the
    design's VERSION in its top 16 bits, so the full 32-bit value legitimately
    changes on every version bump: nanosoc v1.0 is 0x01000001, v1.1 will be
    0x01010001. A dict lookup keyed on the full id -- which is what this did
    until 2026-07-14 -- therefore answers ``rm_name=None`` for a design it
    knows perfectly well, the moment that design is re-versioned, and the hub
    reports a nameless RM. Identity questions key on the design half; only
    "did the board come up with exactly the artefact I pushed?" wants all 32
    bits. This is the host twin of firmware's ``clcd_rm_name()``, which keys on
    ``CLCD_RM_DESIGN(rm_id)`` for exactly this reason.

    Callers' maps are normalised the same way, so a registry keyed by full
    rm_id (``{"0x01000001": "nanosoc"}``), by a bare design id
    (``{"0x0001": ...}`` / ``{1: ...}``), or by a *different version* of the
    same design all resolve identically. Unparsable keys/ids are skipped rather
    than raised on: an unrecognised id must degrade to ``rm_name=None``, never
    take down the whole ``status.fpga`` call.
    """
    try:
        want = rm_design_id(rm_id)
    except (TypeError, ValueError):
        return None
    for key, name in known_rm_names.items():
        try:
            if rm_design_id(key) == want:
                return name
        except (TypeError, ValueError):
            continue  # a non-numeric key can never match a numeric rm_id
    return None


# --------------------------------------------------------------------------- #
# Reset taxonomy (integration-doc §4, extends hardwarehub.md §6)
# --------------------------------------------------------------------------- #


class ResetKind(str, Enum):
    """The MPS3 ``reset {kind}`` taxonomy (integration-doc §4 table).

    Two of these are new relative to the Hub's Zynq-only taxonomy
    (hardwarehub.md §5.2 ``ResetRequest.kind``): ``mcc-reconfig`` (≈
    Zynq's ``board-reset``, but also re-provisions the *shell*) and
    ``dfx-swap`` (the RP-scoped reset the whole platform is built around —
    no Zynq equivalent exists because Zynq has no persistent shell to
    survive a reconfig). ``dut-reset`` is MPS3's SRST-equivalent
    (``dbg_resetn``, driven by the shell) — unlike Zynq, where ``system``
    fails closed (no SRST wired), the MPS3 shell makes a real soft-reset
    available (integration-doc §1, point 3), so ``system`` here is *not*
    a synonym for failure, just the weakest/no-op class.
    """

    MCC_RECONFIG = "mcc-reconfig"
    USB_POWER = "usb-power"
    DFX_SWAP = "dfx-swap"
    DUT_RESET = "dut-reset"
    UART_SOFT = "uart-soft"
    SYSTEM = "system"


@dataclass(frozen=True)
class _ResetSemantics:
    touches_shell: bool
    touches_rp: bool
    #: Either the literal string ``"all"`` or an explicit tuple of channel
    #: ids -- kept as an explicit union rather than always expanding to
    #: ``ALL_CHANNEL_IDS`` up front so the "which class was it" distinction
    #: (integration-doc §4's closing "design rule") stays visible in one
    #: place instead of being reconstructed by comparing sets after the fact.
    invalidates: tuple[str, ...] | str


#: integration-doc §4 table, condensed to what :func:`reset` needs.
_RESET_SEMANTICS: dict[ResetKind, _ResetSemantics] = {
    ResetKind.MCC_RECONFIG: _ResetSemantics(True, True, "all"),
    ResetKind.USB_POWER: _ResetSemantics(True, True, "all"),
    # xvc is shell-GATED (its server never goes down) but swap-INVALIDATED: the
    # debug cores behind it are the RM's own (RM-internal ILAs, handover
    # HANDOVER_RM_ILA_OVER_XVC.md §4.10), so every session must be closed before
    # the swap and reopened with the new RM's .ltx after it.
    ResetKind.DFX_SWAP: _ResetSemantics(False, True, ("swd", "dut-net", "dut-uart", "swo", "xvc")),
    ResetKind.DUT_RESET: _ResetSemantics(False, False, ()),
    ResetKind.UART_SOFT: _ResetSemantics(False, False, ()),
    ResetKind.SYSTEM: _ResetSemantics(False, False, ()),
}


@dataclass(frozen=True)
class ResetResult:
    """``reset`` result (hardwarehub.md §5.3: ``{kind,invalidated_leases[]}``;
    named ``invalidated_channels`` here since this module operates one layer
    below leases -- the Hub/arbiter is what turns "these channels are
    invalidated" into "tear down these leases"). Also carries the
    integration-doc §4 table's ``touches_shell``/``touches_rp``/
    ``rp_scoped`` columns, since "every reset returns *which class it was*
    and *what it invalidated*" (hardwarehub.md §3.4) means more than just
    the bare channel list.
    """

    kind: str
    invalidated_channels: list[str]
    touches_shell: bool
    touches_rp: bool
    rp_scoped: bool


def reset(kind: "ResetKind | str") -> ResetResult:
    """Compute the invalidation set for a reset ``kind`` (integration-doc
    §4). Pure function of ``kind`` -- no I/O, no live board state -- because
    the *semantics* of each reset class (what it touches, what it
    invalidates) are fixed by the hardware/protocol design, not by anything
    the edge would need to probe live.

    Raises :class:`ValueError` (via ``ResetKind(kind)``) for an unrecognised
    ``kind`` string.
    """
    kind = kind if isinstance(kind, ResetKind) else ResetKind(kind)
    spec = _RESET_SEMANTICS[kind]
    invalidated = list(ALL_CHANNEL_IDS) if spec.invalidates == "all" else list(spec.invalidates)
    return ResetResult(
        kind=kind.value,
        invalidated_channels=invalidated,
        touches_shell=spec.touches_shell,
        touches_rp=spec.touches_rp,
        rp_scoped=(kind is ResetKind.DFX_SWAP),
    )


def reset_result_to_dict(result: ResetResult) -> dict[str, Any]:
    return {
        "kind": result.kind,
        "invalidated_channels": list(result.invalidated_channels),
        "touches_shell": result.touches_shell,
        "touches_rp": result.touches_rp,
        "rp_scoped": result.rp_scoped,
    }


# --------------------------------------------------------------------------- #
# The Edge Device API surface + JSON-RPC method-name mapping (hardwarehub.md §5.3)
# --------------------------------------------------------------------------- #


class EdgeApiError(Exception):
    """A :meth:`EdgeDeviceApi.dispatch` request named an unknown method or
    was missing a required param. Distinct from :class:`ValueError` (raised
    by :func:`reset` for a bad ``kind``) so callers can tell "your JSON-RPC
    envelope was malformed" (this) from "your ``kind`` enum value doesn't
    exist" (that) apart, mirroring hardwarehub.md §5.8's split between
    JSON-RPC protocol faults (``-32601`` method-not-found) and application
    faults (the ``Error`` model in ``error.data``).
    """


@dataclass
class EdgeDeviceApi:
    """The MPS3 Edge Device API, modelled as injectable-seam logic.

    Every piece of *real* I/O this would eventually need (a JTAG/OpenOCD
    session for ``shell.loaded``, a live socket for the shell control
    channel) is an injection point here, never opened by this class itself:

    - ``jtag_probe`` -- zero-arg callable returning :class:`JtagDoneResult`;
      defaults to :func:`stub_jtag_done_probe` (honest "unknown").
    - ``shell`` -- anything satisfying :class:`ShellStatusClient` (a
      connected :class:`pyverify.client.ShellClient`, in real use); ``None``
      means "no control channel available", reported as ``rp.loaded=False``
      rather than raising.
    - ``known_rm_names`` -- ``{rm_id: rm_name}``, since the shell's ``ping``
      doesn't hand back a name (see module docstring's open question). Matched
      on the DESIGN half of the id (``rm_id & 0xFFFF``) by
      :func:`_resolve_rm_name`, so the map does NOT need re-keying when a
      design is re-versioned; keys may be full rm_ids or bare design ids.
    """

    board: str = DEFAULT_BOARD_NAME
    shell: ShellStatusClient | None = None
    jtag_probe: Callable[[], JtagDoneResult] = field(default=stub_jtag_done_probe)
    known_rm_names: Mapping[str, str] = field(default_factory=dict)

    # -- §2: enumerate.channels ------------------------------------------- #

    def enumerate_channels(self) -> list[Channel]:
        """The integration-doc §2 channel list. Static per the MPS3 board
        shape (this model doesn't probe live gate/lease state) -- callers
        wanting "is this channel *currently* gated" combine this with
        :meth:`status_fpga`.
        """
        return list(MPS3_CHANNELS)

    # -- §3: status.fpga ---------------------------------------------------- #

    def status_fpga(self) -> FpgaStatus:
        """Assemble the two-level ``FpgaStatus`` (integration-doc §3)."""
        jtag_result = self.jtag_probe()
        shell_status = ShellLevelStatus(
            loaded=jtag_result.done,
            derived_by="jtag-done",
            static_id=jtag_result.static_id,
        )
        rp_status = self._rp_status_from_shell()
        return FpgaStatus(board=self.board, shell=shell_status, rp=rp_status)

    def _rp_status_from_shell(self) -> RpLevelStatus:
        if self.shell is None:
            return RpLevelStatus(loaded=False, derived_by="shell-control", rm_id=None, rm_name=None)
        try:
            ping = self.shell.ping()
        except (ShellProtocolError, ConnectionError, OSError):
            # Honesty over crashing: "the control channel didn't answer" is
            # itself a valid, reportable rp status (loaded=False), not a
            # reason to blow up the whole status.fpga call.
            return RpLevelStatus(loaded=False, derived_by="shell-control", rm_id=None, rm_name=None)
        if not bool(getattr(ping, "ok", False)):
            return RpLevelStatus(loaded=False, derived_by="shell-control", rm_id=None, rm_name=None)
        rm_id = str(getattr(ping, "rm_id", "") or "")
        loaded = _rm_id_indicates_loaded(rm_id)
        # Design-half lookup: survives a design version bump (see _resolve_rm_name).
        rm_name = _resolve_rm_name(self.known_rm_names, rm_id) if loaded else None
        return RpLevelStatus(
            loaded=loaded,
            derived_by="shell-control",
            rm_id=rm_id or None,
            rm_name=rm_name,
        )

    # -- §4: reset ------------------------------------------------------ #

    def reset(self, kind: "ResetKind | str") -> ResetResult:
        """See module-level :func:`reset` (identical; a method for API
        symmetry with :meth:`enumerate_channels`/:meth:`status_fpga` and so
        :meth:`dispatch` has one uniform call shape).
        """
        return reset(kind)

    # -- JSON-RPC 2.0 method-name mapping (hardwarehub.md §5.3) ------------ #

    def dispatch(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Route a hardwarehub.md §5.3 method name to the logic above and
        return its JSON-shaped result -- i.e. what a real ``edge-wrapperd``
        would put on the wire for that method, minus grant
        verification/transport (out of scope: see class docstring).

        Supported methods: ``enumerate.channels``, ``status.fpga``,
        ``reset``. Unknown methods raise :class:`EdgeApiError` (the
        JSON-RPC ``-32601`` case, hardwarehub.md §5.8); a ``reset`` call
        missing ``params.kind`` also raises :class:`EdgeApiError` (a
        malformed-request/``-32602`` case, not an application fault).
        """
        params = dict(params or {})
        if method == "enumerate.channels":
            return {"channels": [channel_to_dict(c) for c in self.enumerate_channels()]}
        if method == "status.fpga":
            return fpga_status_to_dict(self.status_fpga())
        if method == "reset":
            if "kind" not in params:
                raise EdgeApiError("reset: params.kind is required")
            try:
                result = self.reset(params["kind"])
            except ValueError as exc:
                raise EdgeApiError(f"reset: {exc}") from exc
            return reset_result_to_dict(result)
        raise EdgeApiError(f"unknown method: {method!r}")
