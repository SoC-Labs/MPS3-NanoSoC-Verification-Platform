"""``ShellClient`` — control-channel client for the shell coordinator.

Speaks the JSON-line request/response protocol on **TCP 6900** described in
``docs/contracts/net-protocol.md`` ("Control channel (TCP 6900) — JSON
lines"). pyverify is the *client*; the MicroBlaze/lwIP shell firmware (A3)
is the *server*.

Framing (net-protocol.md): one JSON object per line, ``\\n``-terminated,
strict request -> response (no pipelining, no server-initiated pushes).
This module implements that framing for real over a plain TCP socket; the
only thing that requires a live shell is actually opening the connection,
which happens in :meth:`ShellClient.connect` / on first use as a context
manager.

Testability: all I/O goes through the :class:`Transport` protocol. The
default (:class:`SocketTransport`) is a real, correct socket implementation.
Tests inject a fake transport (see ``host/pyverify/tests/test_client.py``)
so the request/response framing and response-parsing logic can be verified
with no socket, no board, and no network — only the *framing* is claimed
"REAL" per the task brief; there is no way to exercise "does the real
board answer" without hardware.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

__all__ = [
    "CONTROL_PORT",
    "DEFAULT_CLK_PRESETS",
    "MACGEN_INJECTS",
    "validate_clk_preset",
    "validate_macgen_inject",
    "ShellClient",
    "ShellProtocolError",
    "PingResponse",
    "ResetResponse",
    "SetClkResponse",
    "SwapResponse",
    "LinkResponse",
    "CommitResponse",
    "TelemetryResponse",
    "MacGenResponse",
    "DiagResponse",
    "DisplayResponse",
    "DisplaySettleResponse",
    "DISPLAY_OWNERS",
    "validate_display_owner",
    "Transport",
    "SocketTransport",
]

# net-protocol.md port map: "6900 | TCP | control/status ... | A3 coordinator"
CONTROL_PORT = 6900

#: The closed set of ``macgen`` inject fault names (net-protocol.md "MAC
#: gen/checker control"; mirrors shell-regmap.md GENCHK.INJECT + the firmware
#: ``macgen_inject_bits`` table). ``"none"`` clears/arms nothing.
MACGEN_INJECTS: "tuple[str, ...]" = (
    "none", "bad_fcs", "runt", "giant", "ifg", "dribble",
)

#: The closed set of owner values a ``display`` *request* may name (the CLCD KVM
#: remote flip, net-protocol.md "display"; mirrors the firmware
#: ``coordinator_handle_display`` accepted set minus the read-only ``"query"``,
#: which :meth:`ShellClient.display_owner` sends on its own). ``"toggle"`` flips
#: the requested target the way ``USER_nPB[1]`` does.
DISPLAY_OWNERS: "tuple[str, ...]" = ("harness", "dut", "toggle")

#: Client-side closed set of ``set_clk`` preset names. Source of truth:
#: ``firmware/clkrst/clkrst.c``'s ``clkrst_preset_table`` — currently the
#: explicit *placeholder* table ``"25mhz"/"50mhz"/"100mhz"`` (ids 0/1/2).
#: The contract still doesn't enumerate presets: that's
#: ``docs/contracts/OPEN_ISSUES.md`` **I16** (A6-owned — the real DRP table
#: is A1/A3's to publish; this tuple does not close it). When I16 lands,
#: update this tuple in lockstep with the firmware table. Matching is
#: exact/case-sensitive, mirroring the firmware's ``strcmp`` lookup.
DEFAULT_CLK_PRESETS: "tuple[str, ...]" = ("25mhz", "50mhz", "100mhz")


def validate_clk_preset(
    preset: str, presets: "Optional[Sequence[str]]" = DEFAULT_CLK_PRESETS,
) -> None:
    """Fail a ``set_clk`` typo client-side instead of round-tripping it.

    Raises ``ValueError`` when ``preset`` is not in ``presets``. Escape
    hatch so the closed set can evolve ahead of this client:
    ``presets=None`` skips the check entirely (send anything — useful the
    moment the firmware table grows a name this tuple hasn't caught up
    with), or pass your own sequence to validate against a different set.
    """
    if presets is None:
        return
    if preset not in presets:
        raise ValueError(
            f"set_clk: unknown preset {preset!r} (client-side check; known "
            f"presets: {', '.join(presets)} — placeholder table from "
            "firmware/clkrst/clkrst.c, pending OPEN_ISSUES.md I16; pass "
            "presets=None to send it to the shell anyway)"
        )


def validate_macgen_inject(
    inject: str, injects: "Optional[Sequence[str]]" = MACGEN_INJECTS,
) -> None:
    """Fail a ``macgen`` inject typo client-side instead of round-tripping it.

    Raises ``ValueError`` when ``inject`` is not in ``injects``. Like
    :func:`validate_clk_preset`, ``injects=None`` skips the check entirely so
    the closed set can evolve ahead of this client. The raw
    :meth:`ShellClient.macgen` is wire-transparent by default (sends anything,
    so the conformance suites can prove the *server* rejects an unknown fault);
    :meth:`pyverify.board.Mps3Board.macgen` and :mod:`pyverify.mactest`
    validate up front.
    """
    if injects is None:
        return
    if inject not in injects:
        raise ValueError(
            f"macgen: unknown inject {inject!r} (client-side check; known: "
            f"{', '.join(injects)} — shell-regmap.md GENCHK.INJECT; pass "
            "injects=None to send it to the shell anyway)"
        )


def validate_display_owner(
    owner: str, owners: "Optional[Sequence[str]]" = DISPLAY_OWNERS,
) -> None:
    """Fail a ``display`` owner typo client-side instead of round-tripping it.

    Raises ``ValueError`` when ``owner`` is not in ``owners``. Escape hatch
    ``owners=None`` skips the check (send anything — the server validates too:
    an unknown owner is a clean ``{"ok":false,"err":"bad owner"}``). The
    read-only ``"query"`` value is deliberately NOT in the default set —
    :meth:`ShellClient.display_owner` sends it; a caller flipping ownership
    should not.
    """
    if owners is None:
        return
    if owner not in owners:
        raise ValueError(
            f"display: unknown owner {owner!r} (client-side check; known: "
            f"{', '.join(owners)} — net-protocol.md 'display'; pass owners=None "
            "to send it to the shell anyway)"
        )


class ShellProtocolError(Exception):
    """The shell's response didn't parse, or violated the line-JSON framing.

    Distinct from a verb-level failure (``{"ok": false}``), which is
    reported through the response dataclass's ``ok`` field so callers can
    decide how to react (e.g. ``swap`` failing is often recoverable; a
    malformed line from the shell is not).
    """


#: The EXACT text :class:`SocketTransport` raises when the shell closes the
#: control channel without a reply. Load-bearing outside this package: the
#: Harness Manager (socharness) string-matches ``"closed by peer"`` in it to mean
#: "the board is held by another client" (6900 is single-client, and a second
#: connection is accept-then-EOF). Change the words and that caller silently
#: reads a held board as a fault. Pinned by tests/test_api_stability.py.
CHANNEL_CLOSED_TEXT = "shell control channel closed by peer"


class ShellChannelClosed(ConnectionError):
    """The shell accepted the connection, then closed it with no reply.

    On 6900 that is the single-client REFUSAL (another client holds the
    channel) or a swap parking it -- the board is up and answering, just not
    to us. A subclass of :class:`ConnectionError` raised with
    :data:`CHANNEL_CLOSED_TEXT` as its message, so ``except ConnectionError``
    callers and callers that match the message text both keep working; new
    code can catch this class instead of matching a string.

    NOT raised for a connection RESET (``ConnectionResetError`` propagates
    unchanged): that is a different wire event, and callers already treat it
    in their own way. Under the Linux harness a RESET on connect means
    ``mps3-harnessd`` is not running while the board is (see
    docs/planning/linux_lanes/FPGAHUB_PATCH_NOTE.md).
    """

    def __init__(self, message: str = CHANNEL_CLOSED_TEXT) -> None:
        super().__init__(message)


__all__ += ["CHANNEL_CLOSED_TEXT", "ShellChannelClosed", "VersionResponse",
            "IMPL_BARE_METAL", "IMPL_LINUX"]
# net-protocol v0.13 (D13, the user-microSD overlay store) -- additive.
__all__ += ["UsdResponse", "UsdDefault", "USD_STATES", "USD_COMMITTABLE_STATES",
            "USD_ERRORS", "USD_CONFIRM_FORMAT", "USD_CONFIRM_WIPE",
            "IDENTITY_LOCK_PREFIX", "is_usd_error_name",
            "USD_BOOT_LATCH_MAGIC", "usd_boot_text", "usd_boot_word"]

#: ``version.impl`` values. The key is ADDITIVE (net-protocol.md): a harness
#: that does not send it is the bare-metal MicroBlaze firmware, so absence is
#: read as :data:`IMPL_BARE_METAL`, never as "unknown".
IMPL_BARE_METAL = "bare-metal"
IMPL_LINUX = "linux"


# --------------------------------------------------------------------------- #
# Response shapes (net-protocol.md "Control channel" examples)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PingResponse:
    ok: bool
    shell_id: str = ""
    rm_id: str = ""


@dataclass(frozen=True)
class ResetResponse:
    ok: bool


@dataclass(frozen=True)
class SetClkResponse:
    ok: bool
    locked: bool = False


@dataclass(frozen=True)
class SwapResponse:
    ok: bool
    rm_id: str = ""
    verified: bool = False


@dataclass(frozen=True)
class LinkResponse:
    ok: bool


@dataclass(frozen=True)
class CommitResponse:
    """``commit`` reply. On success ``slot`` is the user-microSD slot the pair
    now lives in (``"A"``/``"B"``, net-protocol.md v0.13). On failure ``err`` is
    a contract error NAME (:data:`USD_ERRORS`, or ``identity lock: <reason>``).
    ``err`` is additive: it defaults to ``""``."""

    ok: bool
    slot: str = ""
    err: str = ""


@dataclass(frozen=True)
class TelemetryResponse:
    """``telemetry`` reply — **always a failure** (net-protocol.md v0.6).

    ``ok`` is always ``False`` and ``err`` is always ``"no power sensor"``:
    this platform has **no power sensor reachable by any path** (the TELEM
    block's sample inputs are tied to ground in the block design, its INA228
    I2C engine was never written, its pads are not on the top level, and the
    MPS3 MCC refuses voltage reads). The verb has no success shape at all.

    **There are deliberately no ``mv``/``ma`` fields.** They are *absent*,
    not zero. Until v0.6 this dataclass carried ``mv: float = 0.0`` /
    ``ma: float = 0.0`` parsed with ``float(resp.get("mv", 0.0))`` — which,
    against a v0.6 shell, would hand every caller a permanent, plausible
    ``mv=0.0`` alongside ``ok=False``: exactly the reading-shaped lie the
    protocol change exists to kill, only host-side. Removing the attributes
    (rather than defaulting or Optional-ing them) means there is nothing for
    a caller to misread: ``resp.mv`` is an ``AttributeError``, and a caller
    who wants to know why gets the explicit :attr:`err`.

    :attr:`lockup` IS real and is carried on the failure line — the single
    documented carve-out to the protocol's uniform failure shape (telemetry
    is its only carrier, because it has no success line to put it on). It is
    the raw ``DFXCTL.RM_STATUS.dut_lockup`` pin and is read regardless of
    ``ok``. Note it is only *meaningful* for RMs that actually drive that pin
    (``nanosoc_multicore`` does; ``nanosoc``, ``eth_ss`` and the OOC RMs
    hard-tie it to 0) — ``lockup=False`` from an RM that ties it off means
    "cannot report lockup", NOT "the DUT is healthy".
    """

    ok: bool
    err: str = ""
    lockup: bool = False


@dataclass(frozen=True)
class DisplayResponse:
    """``display`` reply — the CLCD KVM remote ownership flip (net-protocol.md
    "display"; shell-regmap.md CLCDKVM).

    On success :attr:`owner` is the **committed** panel owner read back from
    ``CLCDKVM.STATUS.owner`` — ``"harness"`` or ``"dut"``. Note it is the
    *committed* owner, which can lag a just-requested flip by the hardware
    handover (drain → reset → settle → grant, ~7–9 ms): immediately after
    ``set_display_owner("dut")`` this may still read ``"harness"``. Re-query
    with :meth:`ShellClient.display_owner` to confirm the landing.

    On a shell whose bitstream has **no** CLCD KVM slave (``MPS3_HAS_CLCD_KVM``
    off — today's board, before the Wave-4 rebuild) the verb decodes but the
    handler declines: ``ok=False`` and :attr:`err` == ``"clcd_kvm not present"``
    (:attr:`owner` empty). Clients key on :attr:`ok`.
    """

    ok: bool
    owner: str = ""
    err: str = ""


@dataclass
class DisplaySettleResponse:
    """The result of a flip that WAITED for the hardware handover to land.

    ``display`` is send-now: the reply carries ``CLCDKVM.STATUS.owner``, the
    *committed* owner, and a flip takes a drain -> panel reset -> settle ->
    grant to commit (~7-9 ms), so the reply to the flip itself routinely names
    the OUTGOING owner (net-protocol.md "Display"). Every caller that actually
    wants "the panel is now the DUT's" therefore has to re-``query`` until it
    is -- which is exactly what a human doing the board proof was doing by
    hand, badly, with no timeout.

    :attr:`landed` is the whole point: True means a follow-up read-only query
    reported the requested owner. False with ``ok=True`` means the shell
    accepted the request and the owner had not committed within the budget --
    an INCONCLUSIVE result, not a failure, and the two must not be confused
    (see docs/CLCD_KVM_PLAN.md "Proving it").
    """

    ok: bool
    requested: str = ""
    owner: str = ""            # the last committed owner observed
    landed: bool = False
    polls: int = 0             # follow-up queries sent
    waited_s: float = 0.0
    err: str = ""


@dataclass(frozen=True)
class MacGenResponse:
    """``macgen`` reply — the three GENCHK counters after the CTRL/INJECT
    write (net-protocol.md "MAC gen/checker control").

    ``err`` here is the ``ERR_CNT`` *counter* (an int), populated only when
    ``ok`` is true; on a failure reply the wire ``"err"`` key is the
    diagnostic *string* (uniform failure shape) and is not parsed into this
    field — callers key on :attr:`ok`.
    """

    ok: bool
    tx: int = 0
    rx: int = 0
    err: int = 0


@dataclass(frozen=True)
class VersionResponse:
    """``version`` reply — the build identity of the RUNNING image
    (net-protocol.md v0.8).

    This is the answer to "which firmware is on this board", which
    :meth:`ShellClient.ping` cannot give: ``ping`` reports ``shell_id`` (the
    fabric's ``static_id``) and ``rm_id`` (what is in the RP), and **one
    static_id serves many harness releases** — a firmware-only bump re-bakes the
    bitstream via ``updatemem`` without changing the static routing. So a board
    can report the expected ``static_id``, pass every acceptance gate, and still
    be running an image built with the wrong flags. That is not hypothetical: an
    image built without ``HWICAP_FIFO=1`` against a FIFO shell pings, reports the
    right ``static_id``, and silently loads no RM.

    - ``harness`` — semantic release, e.g. ``"1.0.0"`` (``"0.0.0"`` = built with
      no generated identity, i.e. "not provisioned" — never a shipped harness);
    - ``ver32`` — the packed ``HARNESS_VER32`` as ``"0x"`` + 8 lowercase hex.
      The SAME 32-bit value is stamped into the bitstream's ``USR_ACCESS``, so
      comparing this against a JTAG ``REGISTER.USR_ACCESS`` read detects
      firmware/bitstream skew with no firmware running on the other side;
    - ``sha``/``dirty`` — build commit (8 hex, ``"unknown"`` if not provisioned)
      and whether the tree had uncommitted changes. ``dirty`` is an ``int``
      (0/1) on the wire, mirroring ``ver32``'s flags byte;
    - ``lmb_kb`` — the LMB size this image was **linked** for. Load-bearing: the
      LMB decode ALIASES, so a 1024 image running on a 512 shell reads the wrong
      diagnostic mailbox and says nothing about it;
    - ``features`` — compile-time flags, a tuple in the firmware's fixed order
      (``clcd``, ``clcd_kvm``, ``touch``, ``hwicap_fifo``, ``windowed``), absent
      ones omitted. The fielded set (``PRODUCT=1``) is all five;
    - ``usr_access``/``skew`` — the **cross-check**. ``ver32`` is what the image
      was BUILT as; ``usr_access`` is what the FABRIC says it is, read back from
      the bitstream's ``USR_ACCESS`` (AXSS) register. One generator
      (``scripts/gen_version.py``) feeds both the firmware constant and
      ``build_dfx.tcl``'s ``BITSTREAM.CONFIG.USR_ACCESS``, so they agree by
      construction unless the ``.bit`` and the image baked into it came from
      different builds — the "flashable base whose ``updatemem`` was never
      re-run" hazard. ``skew`` is the FIRMWARE's verdict on its own two values,
      never computed here.

    **Three states, and the third is not a pass.** ``skew is False`` means the
    image and the bitstream agree. ``skew is True`` means they do not, and the
    board is carrying a firmware/bitstream mismatch. ``skew is None`` (with
    ``usr_access == ""``) means the fabric value could not be read, so **no
    comparison was made** — a shell with no ``DFXCTL.SHELL_USR_ACCESS`` register
    reports exactly this, and reading it as "fine" is the mistake the whole
    verdict exists to prevent. :attr:`skew_verdict` spells the three out.
    """

    ok: bool
    harness: str = ""
    ver32: str = ""
    sha: str = ""
    dirty: bool = False
    lmb_kb: int = 0
    features: tuple[str, ...] = ()
    #: what the FABRIC reports (``"0x01000001"``); ``""`` = could not be read
    usr_access: str = ""
    #: the firmware's verdict: ``False`` agree, ``True`` SKEW, ``None`` not checked
    skew: bool | None = None
    #: WHICH ENGINE answered (additive, net-protocol.md): ``"linux"`` for
    #: ``mps3-harnessd`` on the MicroBlaze V, :data:`IMPL_BARE_METAL` when the
    #: key is absent (every bare-metal image). The wire contract is otherwise
    #: identical, so this is the one place a client may branch on the engine --
    #: e.g. the diag mailbox sits at the 128 KiB LMB anchor (``lmb_kb`` 128) and
    #: a reboot takes Linux-boot time, not firmware-restart time.
    impl: str = IMPL_BARE_METAL
    #: every key this client does not model, verbatim (a Linux image's build
    #: manifest, a newer firmware's additions). Kept rather than dropped so a
    #: newer server never makes this parser raise, and so a caller can read a
    #: field before pyverify grows a name for it. Excluded from ==/hash.
    extra: "dict[str, Any]" = field(default_factory=dict, compare=False, hash=False)
    #: ADDITIVE (net-protocol.md v0.11, Linux): the engine's IDENTITY disagreement,
    #: e.g. ``"image 0x0badcafe != fabric 0x5a5a0001"`` -- present on the wire only
    #: when stage0's fabric static_id and the image's claim (or USR_ACCESS and the
    #: image's ver32) disagree, and while it is, ``swap``/``commit`` are refused
    #: (``identity lock: ...``). ``None`` = none reported (always, on bare metal).
    #: The raw key also stays in :attr:`extra`, where it landed before this field.
    id_skew: Optional[str] = None

    @property
    def is_bare_metal(self) -> bool:
        """True for the MicroBlaze bare-metal firmware (``impl`` absent)."""
        return self.impl == IMPL_BARE_METAL

    @property
    def is_linux(self) -> bool:
        """True when ``mps3-harnessd`` (the Linux engine) answered."""
        return self.impl == IMPL_LINUX

    @property
    def skew_verdict(self) -> str:
        """``"ok"`` | ``"SKEW"`` | ``"unchecked"`` — never a silent default.

        Kept out of ``__bool__`` and out of ``ok`` on purpose: ``version`` still
        answered successfully when it reports a skew, and conflating "the verb
        worked" with "the board is consistent" is how a mismatch stays
        invisible. Callers that gate on consistency test THIS.
        """
        if self.skew is None:
            return "unchecked"
        return "SKEW" if self.skew else "ok"


#: The ``version`` keys :class:`VersionResponse` models by name; anything else
#: on the line lands in :attr:`VersionResponse.extra` instead of being dropped.
_VERSION_KEYS = frozenset({
    "ok", "harness", "ver32", "sha", "dirty", "lmb_kb", "features",
    "usr_access", "skew", "impl",
})


#: ``net_proto.h``'s ``MPS3_DUTRX_CHUNK_MAX``: the most bytes of a frame one
#: ``dutrx`` reply can carry. Exported so a caller can size a read loop, and so
#: a test can assert the client never assumed a different number than the
#: firmware uses -- they are a matched pair.
DUTRX_CHUNK = 256


@dataclass(frozen=True)
class DutRxResponse:
    """``dutrx`` reply — ONE CHUNK of a frame the DUT transmitted
    (net-protocol.md v0.10 "DUT egress"; shell-regmap.md **DUTEGR**).

    This is the DUT's Ethernet RETURN path. Reception has been silicon-proven
    since 2026-07-30; until the capture block existed, every frame the DUT
    transmitted arrived at the bridge's management egress and was drained into a
    constant — **a DUT could be talked to and could not answer**.

    - ``frame_len`` — the head frame's TOTAL length; ``off``/``n`` locate this
      chunk inside it, and ``data`` is the raw bytes (decoded here from the
      wire's lowercase hex). Frames INCLUDE their 4-byte FCS.
    - ``more`` — bytes of THIS frame remain; ask again. ``last`` is the block's
      SECOND, independent end-of-frame record (the ``DATA[9]`` bit on this
      chunk's final byte) — reported raw beside the length, never reconciled
      with it, because their disagreement is what latches ``desync``.
    - ``frames`` — frames still waiting AFTER this chunk.
    - ``rx``/``drop_full``/``drop_giant`` — **read these**. The capture block
      cannot backpressure the bridge (that parks the whole bridge), so it
      DROPS, and ``rx + drop_full + drop_giant`` is its tally against frames
      presented. A caller that reads frames without reading these is counting
      only what survived.
    - ``ovf``/``desync`` — sticky status bits, raw.

    On a shell whose bitstream has **no** DUTEGR slave (the fielded one, minted
    before the block existed) the verb decodes but the handler declines:
    ``ok=False``, ``err == "dut_egress not present"``. That is deliberately NOT
    an empty-looking success: "this fabric cannot capture" must not read the
    same as "the DUT sent nothing".
    """

    ok: bool
    #: wire key ``len`` — renamed only here, where ``len`` would shadow a builtin
    frame_len: int = 0
    off: int = 0
    n: int = 0
    more: bool = False
    last: bool = False
    frames: int = 0
    rx: int = 0
    drop_full: int = 0
    drop_giant: int = 0
    ovf: bool = False
    desync: bool = False
    data: bytes = b""
    err: str = ""

    @property
    def drops(self) -> int:
        """``drop_full + drop_giant`` — frames the block ACCEPTED and could not
        keep. Non-zero means this capture is lossy; it does not mean the DUT
        misbehaved (``drop_full`` is nobody draining fast enough, ``drop_giant``
        is a frame over ``MAX_FRAME``)."""
        return self.drop_full + self.drop_giant


#: ``firmware/common/log_ring.h``'s MPS3_LOG_CHUNK_MAX: most console bytes one
#: ``log`` reply carries (the same 256 as DUTRX_CHUNK, for the same reason).
LOG_CHUNK = 256


@dataclass(frozen=True)
class StatsResponse:
    """``stats`` reply (net-protocol.md v0.11 "Stats") -- the board's state in one
    line, in fpgahub's ``_from_stats_verb`` key shape plus five extras.

    ``raw`` is the decoded reply dict IN WIRE ORDER (a caller that forwards the
    line, e.g. to fpgahub, should forward that). The typed fields are the ones
    this repo's tools read. ``up_ms`` resets on ANY shell restart: it is the
    reboot witness. There are deliberately no power fields."""

    ok: bool
    up_ms: int = 0
    sid: str = ""
    rm: str = ""
    rm_ok: bool = False
    lock: bool = False
    clk_sel: int = 0
    mmcm: bool = False
    clk_alive: bool = False
    dut_rst: bool = False
    rp_rst: bool = False
    decpl: bool = False
    link: bool = False
    spd: int = 0
    fdx: bool = False
    mac: str = ""
    swap: str = ""
    swap_ok: bool = False
    swap_n: int = 0
    icap: int = 0
    rxdrop: int = 0
    txerr: int = 0
    swap_err: str = ""
    clr_ok: bool = False
    dut_mhz: int = 0
    svc_max_us: int = 0
    svc_skipped: int = 0
    err: str = ""
    raw: "dict[str, Any]" = None  # type: ignore[assignment]
    #: ADDITIVE (Linux harness): milliseconds since the KERNEL booted. On
    #: Linux ``up_ms`` is mps3-harnessd's own uptime, which a respawn resets
    #: without a reboot; ``os_up_ms`` is the board's. ``None`` = not sent
    #: (bare metal, where the firmware IS the OS and ``up_ms`` already is it).
    os_up_ms: "Optional[int]" = None

    @property
    def present(self) -> "frozenset[str]":
        """The keys the shell actually sent (see :attr:`DiagResponse.present`:
        an omitted key is "no source", a zero is a reading)."""
        return frozenset(k for k in (self.raw or {}) if k != "ok")

    def has(self, key: str) -> bool:
        return key in self.present


@dataclass(frozen=True)
class LogResponse:
    """``log`` reply (net-protocol.md v0.11 "Log") -- ONE chunk of the shell
    console ring, from stream offset ``off``. Non-destructive: the shell keeps
    no cursor. ``off`` later than asked means bytes were overwritten; ``dropped``
    is the since-boot overrun count and rides every reply."""

    ok: bool
    off: int = 0
    n: int = 0
    more: bool = False
    dropped: int = 0
    data: bytes = b""
    err: str = ""


@dataclass(frozen=True)
class TouchCalResponse:
    """``touch_cal`` reply (net-protocol.md v0.11). ``coeffs`` is
    ``(ax, bx, cx, ay, by, cy, shift)`` for get/set/default; ``raw_*``/``seen``/
    ``x``/``y`` are filled for ``act="raw"``."""

    ok: bool
    coeffs: "tuple[int, ...]" = ()
    raw_x: int = 0
    raw_y: int = 0
    raw_z: int = 0
    seen: int = 0
    x: int = 0
    y: int = 0
    err: str = ""


@dataclass(frozen=True)
class RebootResponse:
    """``reboot`` reply (net-protocol.md v0.11). ``in_ms`` is an UPPER BOUND on
    when the watchdog resets the shell. Refusals: ``EBUSY`` (mid-swap), ``no
    watchdog`` (a shell without WDOG at 0x44B4)."""

    ok: bool
    in_ms: int = 0
    err: str = ""


#: touch_cal coefficient keys, in wire / tuple order.
TOUCH_CAL_KEYS = ("ax", "bx", "cx", "ay", "by", "cy", "shift")


# --------------------------------------------------------------------------- #
# net-protocol v0.13 -- the user microSD (`usd`) and the re-push `commit`
# --------------------------------------------------------------------------- #

#: Every ``usd.state`` value (net-protocol.md v0.13 "User microSD").
USD_STATES: "tuple[str, ...]" = (
    "no_hw", "none", "init", "unsupported", "error",
    "foreign", "empty", "valid", "stale", "bad",
)

#: The states a ``commit`` may write over. ``stale`` is included on purpose:
#: committing over a stale card is how a re-keyed board recovers.
USD_COMMITTABLE_STATES: "frozenset[str]" = frozenset({"empty", "valid", "bad", "stale"})

#: The ``usd`` / ``commit`` error NAMES (net-protocol.md v0.13 "Error names").
#: Names, never errno numbers. ``identity lock: <reason>`` is the one prefixed
#: form outside this set (see :func:`is_usd_error_name`).
USD_ERRORS: "frozenset[str]" = frozenset({
    # card state
    "no card", "no hw", "foreign", "stale key", "unavailable",
    # format / wipe
    "filesystem present", "exists", "partition too small", "confirm required",
    "wipe disabled",
    # commit
    "rm mismatch", "crc",
    # store / device
    "store busy", "io", "timeout",
    # request
    "bad args",
})

#: The identity-lock refusal prefix (net-protocol.md "Identity lock").
IDENTITY_LOCK_PREFIX = "identity lock: "

#: ``usd`` format confirmations: ``"erase"`` is the plain format (rules a/b/c),
#: ``"erase-all"`` the explicit wipe (bare metal only).
USD_CONFIRM_FORMAT = "erase"
USD_CONFIRM_WIPE = "erase-all"


def is_usd_error_name(err: str) -> bool:
    """True when ``err`` is a contract ``usd``/``commit`` error name."""
    return err in USD_ERRORS or err.startswith(IDENTITY_LOCK_PREFIX)


# --------------------------------------------------------------------------- #
# The power-on load LATCH word (diag.h v9 ``usd_boot``; overlay_store.h "THE
# BOOT LATCH"). The firmware keeps the once-per-FPGA-configuration decision in
# one u32 that only a reconfiguration clears, and publishes it in the diag
# mailbox, so a JTAG read (no network) and ``usd.boot`` (the network) can be
# compared. Mirrored here from firmware/overlay_store/overlay_store.h and
# ovlstore_sd.h; host/pyverify/tests/test_usd.py parses those headers and fails
# if the tables below drift from them.
# --------------------------------------------------------------------------- #

#: ``[31:16]`` of a decided latch word (``OVL_BOOT_LATCH_MAGIC``).
USD_BOOT_LATCH_MAGIC = 0xB007
#: ``[3:0]``: ``OVL_BOOT_*``.
USD_BOOT_DECISIONS: "dict[int, str]" = {
    1: "pending", 2: "loaded", 3: "skipped", 4: "none", 5: "failed",
}
#: ``[15:8]`` for a ``failed`` decision, the plain reasons (``OVL_BOOT_WHY_*``).
USD_BOOT_WHY: "dict[int, str]" = {
    0x01: "timeout", 0x02: "identity lock", 0x03: "too big", 0x04: "aborted",
    0x05: "store busy",
}
#: ``OVL_BOOT_WHY_SWAP | state``: the swap FSM state the load failed in
#: (``swap_fsm_state_name()``, indexed by ``mps3_swap_state_t``).
USD_BOOT_WHY_SWAP = 0x80
USD_SWAP_STATE_NAMES: "tuple[str, ...]" = (
    "idle", "gate", "decouple", "stream_clearing", "await_clearing",
    "await_partial", "stream_partial", "verify", "cache_clearing",
    "release", "done", "failed", "reisolate",
)
#: ``OVL_BOOT_WHY_STORE | (-rc & 0x3F)``: a store error, rendered through
#: ``overlay_store_err_name()``. Keyed by ``-rc`` (the ``OVLSD_E*`` codes);
#: anything absent renders as ``"io"`` (that function's default).
USD_BOOT_WHY_STORE = 0x40
USD_STORE_RC_NAMES: "dict[int, str]" = {
    1: "no card", 2: "no hw", 3: "unavailable", 4: "foreign", 5: "stale key",
    6: "stale key", 7: "rm mismatch", 8: "store busy", 9: "bad args",
    10: "bad args", 11: "bad args", 12: "crc", 13: "io", 14: "crc", 15: "crc",
    16: "no card", 17: "unavailable", 18: "unavailable", 19: "io",
    20: "confirm required", 21: "exists", 22: "filesystem present", 23: "exists",
    24: "partition too small", 25: "filesystem present", 26: "wipe disabled",
}


def usd_boot_text(word: int) -> str:
    """Render a ``usd_boot`` latch word as ``usd.boot`` text, exactly as the
    firmware's ``overlay_store_boot_text()`` does: ``0`` (not decided yet in this
    configuration) and a PENDING word are ``"pending"``; a failed decision is
    ``"failed:<why>"``. Raises ``ValueError`` on a non-zero word without the
    latch magic -- that is not a latch, and guessing would invent a decision."""
    word = int(word) & 0xFFFFFFFF
    if word == 0:
        return "pending"
    if (word >> 16) != USD_BOOT_LATCH_MAGIC:
        raise ValueError("0x%08x is not a usd_boot latch word (no 0x%04X magic)"
                         % (word, USD_BOOT_LATCH_MAGIC))
    decision = USD_BOOT_DECISIONS.get(word & 0xF, "pending")
    if decision != "failed":
        return decision
    why = (word >> 8) & 0xFF
    if why & USD_BOOT_WHY_SWAP:
        n = why & 0x7F
        name = USD_SWAP_STATE_NAMES[n] if n < len(USD_SWAP_STATE_NAMES) else "?"
    elif why & USD_BOOT_WHY_STORE:
        name = USD_STORE_RC_NAMES.get(why & 0x3F, "io")
    else:
        name = USD_BOOT_WHY.get(why, "io")
    return "failed:" + name


def usd_boot_word(text: str) -> int:
    """The latch word a decision renders from -- the inverse of
    :func:`usd_boot_text` (a PENDING decision is the word the firmware writes the
    moment it starts deciding, ``0xB0070001``). Raises ``ValueError`` on a text
    no latch word renders as."""
    by_name = {v: k for k, v in USD_BOOT_DECISIONS.items()}
    if text in by_name and text != "failed":
        return (USD_BOOT_LATCH_MAGIC << 16) | by_name[text]
    if text.startswith("failed:"):
        name = text[len("failed:"):]
        why = next((k for k, v in USD_BOOT_WHY.items() if v == name), None)
        if why is None:
            why = next((k | USD_BOOT_WHY_STORE for k, v in sorted(USD_STORE_RC_NAMES.items())
                        if v == name), None)
        if why is None and name in USD_SWAP_STATE_NAMES:
            why = USD_BOOT_WHY_SWAP | USD_SWAP_STATE_NAMES.index(name)
        if why is not None:
            return (USD_BOOT_LATCH_MAGIC << 16) | (why << 8) | 5
    raise ValueError(f"no usd_boot latch word renders as {text!r}")


@dataclass(frozen=True)
class UsdDefault:
    """``usd.default``: the overlay the store would load at power-on."""

    rm_id: str = ""
    static_id: str = ""
    slot: str = ""


@dataclass(frozen=True)
class UsdResponse:
    """``usd`` reply (net-protocol.md v0.13 "User microSD").

    Status (``{"op":"usd"}``) fills ``present``/``state``/``text``/``boot``, plus
    ``card_mb`` when a card is ready and ``default`` when ``state`` is
    ``valid`` or ``stale``. An action (``format``/``clear``/``rescan``) fills
    only ``ok`` and ``state``. A failure fills ``err`` with a contract NAME.

    ``present`` false is NOT an error: the status reply is ``ok`` with no card.
    ``raw`` is the decoded line in wire order."""

    ok: bool
    present: bool = False
    state: str = ""
    text: str = ""
    card_mb: "Optional[int]" = None
    default: "Optional[UsdDefault]" = None
    boot: str = ""
    err: str = ""
    raw: "dict[str, Any]" = field(default_factory=dict, compare=False, hash=False)

    @property
    def ready(self) -> bool:
        """A card is in and initialised (the shell sent ``card_mb``)."""
        return self.card_mb is not None

    @property
    def committable(self) -> bool:
        """A ``commit`` could be accepted in this state (card present, store
        ``empty``/``valid``/``bad``/``stale``)."""
        return self.ok and self.present and self.state in USD_COMMITTABLE_STATES


def _hex32_arg(value: "int | str", name: str) -> str:
    """A u32 as the wire's ``"0x"`` + 8 lowercase hex string. Accepts an int or
    an already-formatted string (re-rendered, so ``"0x1E"`` goes out as
    ``"0x0000001e"``)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an int or a hex string, got {value!r}")
    if isinstance(value, str):
        try:
            value = int(value, 0)
        except ValueError:
            raise ValueError(f"{name} must be a hex string like 0x0100001e, got {value!r}") from None
    if not 0 <= int(value) <= 0xFFFFFFFF:
        raise ValueError(f"{name} out of u32 range: {value!r}")
    return "0x%08x" % int(value)


# --------------------------------------------------------------------------- #
# net-protocol v0.17: presence (`hello`) and the front panel (`panel`) -- the
# Harness Manager's R1/R2 (harness-manager docs/design/CLCD_ALIGNMENT.md §2).
# --------------------------------------------------------------------------- #
#: The request line the harness reads (firmware/common/net_if.h MPS3_NET_LINE_MAX);
#: a `hello` is sent only when its line, newline included, fits.
HELLO_LINE_MAX = 256
#: Per-field caps (printable ASCII, one byte per character): sid, who (user@host),
#: app, the board's N1 name, a lease user (user part only), a job kind.
HELLO_CAPS = {"sid": 8, "who": 20, "app": 12, "name": 16, "user": 12, "job": 8}
#: Numeric clamps: a lease's seconds left, its queue, an open request's seconds to
#: answer; and the TTL range.
HELLO_LIMITS = {"left": 86_400, "q": 99, "rl": 600}
HELLO_TTL = (30, 300)
HELLO_ROLES = ("holder", "owner", "watch")
#: `panel` frame halves: "a" = rows 0-7, "b" = rows 8-14 (a whole frame with its
#: 600 role codes does not fit one 1280-byte reply).
PANEL_FRAME_HALVES = (("a", 0, 8), ("b", 8, 15))
#: A frame's per-cell role CODE is chr(ord("a") + index) in this order (Harness
#: Manager design/tokens.json panel.roles = the generated clcd_palette.h enum).
PANEL_ROLES = ("text", "label", "value", "rule", "chrome", "title", "title-held",
               "title-warn", "ok", "warn", "err", "busy", "unk", "held", "bar", "track",
               "banner-err", "banner-warn", "banner-ok", "banner-busy", "banner-held")


def _ascii_field(value: Any, limit: int) -> str:
    return "".join(ch if " " <= ch <= "~" else "?" for ch in str(value))[:limit]


def _clamp(value: Any, top: int) -> int:
    try:
        return max(0, min(top, int(value)))
    except (TypeError, ValueError):
        return 0


def hello_message(sid: str, who: str, app: str, *, name: str = "", role: str = "watch",
                  lease: "Optional[dict[str, Any]]" = None,
                  job: "Optional[dict[str, Any]]" = None, ttl: int = 90) -> "dict[str, Any]":
    """The v0.17 ``hello`` request, every field capped, in the wire's key order
    (op, v, sid, who, app, [name], role, [lease], [job], ttl) -- byte for byte what
    the Harness Manager's ``core.panel.hello_message`` builds (the one-codec rule).

    ``lease`` = ``{"by", "left", "q", "req", "rl"}`` as the host last read the hub:
    principals are sent as their user part; ``left``/``rl`` are RELATIVE seconds
    (None/absent = unknown); a lease without ``by`` says "behind a hub, nobody holds
    it". ``job`` = ``{"k": kind, "p": percent}``."""
    msg: "dict[str, Any]" = {"op": "hello", "v": 1, "sid": _ascii_field(sid, HELLO_CAPS["sid"]),
                             "who": _ascii_field(who, HELLO_CAPS["who"]),
                             "app": _ascii_field(app, HELLO_CAPS["app"])}
    if name:
        msg["name"] = _ascii_field(name, HELLO_CAPS["name"])
    msg["role"] = role if role in HELLO_ROLES else "watch"
    if lease is not None:
        out: "dict[str, Any]" = {}
        user = HELLO_CAPS["user"]
        if lease.get("by"):
            out["by"] = _ascii_field(str(lease["by"]).split("@", 1)[0], user)
        if lease.get("left") is not None:
            out["left"] = _clamp(lease["left"], HELLO_LIMITS["left"])
        out["q"] = _clamp(lease.get("q", 0), HELLO_LIMITS["q"])
        if lease.get("req"):
            out["req"] = _ascii_field(str(lease["req"]).split("@", 1)[0], user)
            if lease.get("rl") is not None:
                out["rl"] = _clamp(lease["rl"], HELLO_LIMITS["rl"])
        msg["lease"] = out
    if job is not None:
        msg["job"] = {"k": _ascii_field(job.get("k", ""), HELLO_CAPS["job"]),
                      "p": _clamp(job.get("p", 0), 100)}
    msg["ttl"] = max(HELLO_TTL[0], min(HELLO_TTL[1], int(ttl)))
    return msg


def hello_line(msg: "dict[str, Any]") -> bytes:
    """``msg`` as sent (compact JSON + newline); ValueError over HELLO_LINE_MAX."""
    line = (json.dumps(msg, separators=(",", ":")) + "\n").encode("ascii")
    if len(line) > HELLO_LINE_MAX:
        raise ValueError(f"hello is {len(line)} B; the harness reads at most {HELLO_LINE_MAX}")
    return line


__all__ += ["HELLO_LINE_MAX", "HELLO_CAPS", "HELLO_LIMITS", "HELLO_TTL", "HELLO_ROLES",
            "PANEL_FRAME_HALVES", "PANEL_ROLES", "hello_message", "hello_line"]


#: ``firmware/touch/touch.h``'s TOUCH_VERDICT_* -- the panel-continuity probe's
#: four possible answers, in the order the firmware numbers them. "unknown" is a
#: real answer (no probe ran, or it could not complete), never a stand-in for
#: one of the others.
TOUCH_VERDICT_WORDS = {
    0: "unknown",
    1: "chip-misconfigured",
    2: "panel-open",
    3: "panel-present",
}


@dataclass(frozen=True)
class DiagResponse:
    """``diag`` reply — the shell's always-on diagnostic counters (firmware
    ``diag.h``). The over-the-wire-reconfig RX-path instrumentation:

    - ``rx_recover``/``rx_dumps``/``rx_drops`` — LAN9220 RX-overrun recovery
      (all 0 = no MAC overflow, the HW-confirmed case);
    - ``icap_bytes`` — bytes written to HWICAP.WF (== ``got`` => ICAP keeps up);
    - ``got``/``expect`` — in-flight partial receive progress vs total;
    - ``rcv_wnd``/``rcv_ann_wnd`` — the 6910 TCP pcb's receive window (0 while
      stalled => the window is not reopening to the host);
    - ``rx_queued`` — bytes queued in the 6910 conn's pbuf chain;
    - ``pbuf_free`` — free lwIP PBUF_POOL buffers (0 => inbound frames dropped
      for want of a pbuf). ``~0`` (4294967295) means stats are compiled out.
    - ``grants_sent``/``grant_fails`` — windowed grant bytes lwIP ACCEPTED vs
      REFUSED on the 6910 send-back (splits the grant-deadlock: reached-and-
      accepted vs send-refused);
    - ``sndbuf`` — the 6910 pcb ``tcp_sndbuf`` (0 => a grant send is refused);
    - ``snd_wnd`` — the peer's advertised window (0 => an accepted grant can't
      egress).
    - v0.9 appends the 11 counters that were JTAG-only (TX-path
      ``tx_*``, ``icap_sr_last``/``icap_eos_status``, ``ovlstore_*``); an older
      shell simply omits them and they read 0 here.
    - v0.9.2 appends the superloop service telemetry (``svc_*``,
      ``pass_max_us``) — see the field comments below. ``svc_skipped != 0``
      means the loop has taken a service OUT of the rotation for overrunning
      its budget; nothing else in this reply says so.

    NOTE: 6900 is PARKED during a swap, so this verb is only reachable when
    idle; during a swap read the SAME counters from the fixed DMEM mailbox over
    JTAG-MDM (see firmware/common/diag.h)."""

    ok: bool
    rx_recover: int = 0
    rx_dumps: int = 0
    rx_drops: int = 0
    icap_bytes: int = 0
    got: int = 0
    expect: int = 0
    rcv_wnd: int = 0
    rcv_ann_wnd: int = 0
    rx_queued: int = 0
    pbuf_free: int = 0
    grants_sent: int = 0
    grant_fails: int = 0
    sndbuf: int = 0
    snd_wnd: int = 0
    # v0.9 -- the 11 counters that used to be JTAG-only (diag.h X-macro order).
    tx_frames_sent: int = 0
    tx_status_drained: int = 0
    tx_fifo_full_drops: int = 0
    tx_errors: int = 0
    tx_space_stalls: int = 0
    tx_iface_errors: int = 0
    tx_last_status: int = 0
    icap_sr_last: int = 0
    icap_eos_status: int = 0
    ovlstore_phase: int = 0
    ovlstore_detail: int = 0
    # v0.9.1 -- the STMPE811 panel-continuity probe (diag.h v7, X-macro order).
    # ``touch_verdict`` is the answer: 0 unknown / 1 chip-misconfigured /
    # 2 panel-open / 3 panel-present. The other three are its raw evidence --
    # packed register read-backs and 12-bit ADC samples, so read them as HEX
    # (firmware/touch/touch.h documents every field). A shell built without
    # TOUCH=1, or an older shell, leaves all four 0 = "no probe ran".
    touch_regs: int = 0
    touch_adc_x: int = 0
    touch_adc_y: int = 0
    touch_verdict: int = 0
    # DERIVED, not on the wire: the same four numbers spelled the way a human
    # reading `pyverify diag` needs them. A bare ``"touch_verdict": 2`` is a
    # lookup away from meaning anything and a packed register word in decimal is
    # unreadable -- which is how a diagnostic ends up unused. The raw ints above
    # stay authoritative; these are a rendering of them, filled in by
    # :meth:`ShellClient.diag`.
    touch_verdict_word: str = "unknown"
    touch_regs_hex: str = "0x00000000"
    touch_adc_x_hex: str = "0x00000000"
    touch_adc_y_hex: str = "0x00000000"
    # v0.9.2 -- SUPERLOOP SERVICE TELEMETRY (diag.h v8, X-macro order). The
    # superloop is a table (firmware/common/service.h) and it times itself, so
    # "which service ate the pass" is finally a number rather than a guess.
    #   svc_count      services in the table;
    #   pass_max_us    worst FULL pass, microseconds, high-water since boot;
    #   svc_max_us     worst SINGLE service, and svc_max_ix which one it was
    #                  (an INDEX into the table in firmware/platform/src/main.c);
    #   svc_overruns   budget overruns in total;
    #   svc_skips      healthy->sick EDGES (a service that STAYS sick does not
    #                  keep inflating this);
    #   svc_skipped    bitmask of services currently SKIPPED for having blown
    #                  their budget on MPS3_SVC_SICK_K consecutive passes.
    #                  NON-ZERO IS THE HEADLINE: the loop has taken a service
    #                  out of the rotation and is probing it once per cooldown.
    #   svc_us_0..5    per-service worst case, two per word, each a 16-bit
    #                  SATURATING microsecond count (service 2p in bits [15:0]
    #                  of svc_us_<p>, 2p+1 in [31:16]); 0xFFFF means ">= 65535".
    # An older shell omits all thirteen and they read 0 here -- which for a
    # pre-v8 image is the truth: it had no service table.
    svc_count: int = 0
    pass_max_us: int = 0
    svc_max_us: int = 0
    svc_max_ix: int = 0
    svc_overruns: int = 0
    svc_skips: int = 0
    svc_skipped: int = 0
    svc_us_0: int = 0
    svc_us_1: int = 0
    svc_us_2: int = 0
    svc_us_3: int = 0
    svc_us_4: int = 0
    svc_us_5: int = 0
    #: diag.h v9 (D13, additive): service 12 ("usd") in [15:0], and the user-
    #: microSD power-on load latch ([31:16] 0xB007, [15:8] reason, [3:0]
    #: decision; 0 until the decision is taken). Absent on an older shell.
    svc_us_6: int = 0
    usd_boot: int = 0
    #: the counter keys the shell ACTUALLY SENT. Every field above keeps its
    #: 0 default when a key is absent (compatibility), and 0 is also a real
    #: reading -- so "not measured" and "measured zero" look the same through
    #: the fields alone. Under Linux, ``mps3-harnessd`` OMITS the keys it has
    #: no source for (the lwIP/LAN9220-driver counters the kernel now owns --
    #: a codec validity mask, HARNESSD_CONTRACT.md), rather than zeroing them.
    #: Test ``"pbuf_free" in resp.present`` before trusting ``resp.pbuf_free``.
    present: "frozenset[str]" = frozenset()

    def has(self, key: str) -> bool:
        """True if the shell sent ``key`` (a wire key, e.g. ``"pbuf_free"``)."""
        return key in self.present

    @property
    def usd_boot_text(self) -> "Optional[str]":
        """The power-on decision the ``usd_boot`` latch word renders as (the
        same text ``usd.boot`` reports; :func:`usd_boot_text`), or ``None`` when
        the shell did not send the key (older than diag.h v9)."""
        if not self.has("usd_boot"):
            return None
        return usd_boot_text(self.usd_boot)


# --------------------------------------------------------------------------- #
# Transport (real socket implementation + DI seam for tests)
# --------------------------------------------------------------------------- #


@runtime_checkable
class Transport(Protocol):
    """Minimal line-oriented transport ``ShellClient`` needs.

    One "line" == one JSON object per net-protocol.md; the newline is the
    frame delimiter, not part of the payload.
    """

    def send_line(self, payload: bytes) -> None: ...

    def recv_line(self) -> bytes: ...

    def close(self) -> None: ...


class SocketTransport:
    """Real TCP transport: connect, then newline-delimited send/recv.

    Buffers partial reads (TCP gives no framing guarantee — a single
    ``recv`` may return less than one line, or more than one if the shell
    ever pipelines, though net-protocol.md's control channel is strictly
    request/response so that shouldn't happen in practice).
    """

    def __init__(self, host: str, port: int = CONTROL_PORT, timeout: float = 5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""

    def send_line(self, payload: bytes) -> None:
        self._sock.sendall(payload + b"\n")

    def recv_line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                # ShellChannelClosed IS a ConnectionError and carries the same
                # text, so both the except-clause and the string-match callers
                # (socharness: "closed by peer" == board held) keep working.
                raise ShellChannelClosed(CHANNEL_CLOSED_TEXT)
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def close(self) -> None:
        self._sock.close()


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class ShellClient:
    """Client for the shell coordinator's control channel (TCP 6900).

    Usage::

        with ShellClient("192.168.10.101") as shell:
            info = shell.ping()
            shell.set_clk("25mhz")
            shell.swap(rm="nanosoc", src="tftp")

    ``transport`` is an injection seam for tests (see module docstring);
    normal use leaves it ``None`` and lets :meth:`connect` build a real
    :class:`SocketTransport`.
    """

    def __init__(
        self,
        host: str,
        port: int = CONTROL_PORT,
        *,
        timeout: float = 5.0,
        transport: Transport | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._transport = transport

    def connect(self) -> "ShellClient":
        if self._transport is None:
            self._transport = SocketTransport(self.host, self.port, self.timeout)
        return self

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> "ShellClient":
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- core request/response -------------------------------------------- #

    def _send_op(self, op: dict[str, Any]) -> None:
        """Send one request line, WITHOUT awaiting the reply.

        Split out of :meth:`_request` because the ``swap`` verb genuinely needs
        it: the shell PARKS this connection for the whole reconfiguration and
        only listens for the bitstream push on 6910 once the swap FSM has
        reached its await-incoming states. So a deploy must send ``swap``, THEN
        push, THEN collect the reply — see :meth:`swap_begin`/:meth:`swap_await`.
        """
        if self._transport is None:
            raise ShellProtocolError(
                "ShellClient is not connected; call connect() or use "
                "'with ShellClient(...) as shell:'"
            )
        line = json.dumps(op, separators=(",", ":"))
        self._transport.send_line(line.encode("ascii"))

    def _recv_resp(self) -> dict[str, Any]:
        """Read + validate one response line (the half after :meth:`_send_op`)."""
        if self._transport is None:
            raise ShellProtocolError(
                "ShellClient is not connected; call connect() or use "
                "'with ShellClient(...) as shell:'"
            )
        raw = self._transport.recv_line()
        try:
            resp = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ShellProtocolError(
                f"malformed JSON line from shell: {raw!r}"
            ) from exc
        if not isinstance(resp, dict):
            raise ShellProtocolError(
                f"expected a JSON object, got {type(resp).__name__}: {resp!r}"
            )
        if "ok" not in resp:
            raise ShellProtocolError(f"response missing required 'ok' field: {resp!r}")
        return resp

    def _request(self, op: dict[str, Any]) -> dict[str, Any]:
        self._send_op(op)
        return self._recv_resp()

    # -- verbs (net-protocol.md "Control channel" table) ------------------- #

    def ping(self) -> PingResponse:
        """``{"op":"ping"}`` -> shell_id/rm_id identify the running image."""
        resp = self._request({"op": "ping"})
        return PingResponse(
            ok=bool(resp["ok"]),
            shell_id=str(resp.get("shell_id", "")),
            rm_id=str(resp.get("rm_id", "")),
        )

    def reset(self, target: str = "dut") -> ResetResponse:
        """``{"op":"reset","target":"dut"}`` — one of the three shell-driven
        resets (ARCHITECTURE_SPEC.md §5): DUT system reset by default.
        """
        resp = self._request({"op": "reset", "target": target})
        return ResetResponse(ok=bool(resp["ok"]))

    def set_clk(
        self,
        preset: str,
        *,
        presets: "Optional[Sequence[str]]" = None,
    ) -> SetClkResponse:
        """``{"op":"set_clk","preset":"25mhz"}`` — DRP MMCM DUT-clock preset
        (ARCHITECTURE_SPEC.md §5). Client-side preset validation is
        **opt-in** here: pass ``presets=DEFAULT_CLK_PRESETS`` (or any
        sequence) to get a ``ValueError`` on a typo instead of a
        round-trip; the default sends the string verbatim. Deliberate
        split (W-HOST-MISC): ``ShellClient`` is the raw protocol driver —
        the conformance suites (``tests/firmware_logic/test_json_golden.py``,
        ``tests/test_fakeshell.py``) drive *unknown* presets through it on
        purpose to prove the server rejects them cleanly. The user-facing
        :meth:`pyverify.board.Mps3Board.set_clk` facade validates **by
        default** against :data:`DEFAULT_CLK_PRESETS` (the placeholder
        table from ``firmware/clkrst/clkrst.c``; enumeration is still open
        as docs/contracts/OPEN_ISSUES.md I16).
        """
        validate_clk_preset(preset, presets)
        resp = self._request({"op": "set_clk", "preset": preset})
        return SetClkResponse(ok=bool(resp["ok"]), locked=bool(resp.get("locked", False)))

    def swap_begin(self, rm: str, src: str = "tftp") -> None:
        """Send ``{"op":"swap",...}`` and return IMMEDIATELY, without awaiting
        the reply.

        ⚠ THE PUSH MUST HAPPEN BETWEEN THIS AND :meth:`swap_await`. Verified on
        silicon 2026-07-14: the shell's swap FSM only listens for the bitstream
        on 6910 once this RPC has driven it into AWAIT_INCOMING_CLEARING /
        AWAIT_PARTIAL — which is exactly why it PARKS this 6900 connection for
        the duration. Pushing *before* the swap (the old
        ``push_pair()`` -> ``swap()`` order) hits a shell that is not expecting
        data, and it RESETS the 6910 connection (ECONNRESET, ``diag.got`` stays
        0). See :meth:`swap_await` and :mod:`pyverify.swap`.
        """
        self._send_op({"op": "swap", "rm": rm, "src": src})

    def swap_await(self) -> SwapResponse:
        """Collect the parked reply to a :meth:`swap_begin` (after pushing).

        Blocks for the whole server-side clearing -> partial -> verify -> release
        sequence, so this client needs a GENEROUS timeout — the 5 s default
        cannot survive a real reconfiguration.
        """
        resp = self._recv_resp()
        return SwapResponse(
            ok=bool(resp["ok"]),
            rm_id=str(resp.get("rm_id", "")),
            verified=bool(resp.get("verified", False)),
        )

    def swap(self, rm: str, src: str = "tftp") -> SwapResponse:
        """``{"op":"swap","rm":"nanosoc","src":"tftp"}`` — drives the
        server-side clearing->partial->verify->release sequence
        (net-protocol.md "Swap sequence").

        ⚠ This blocking form sends the RPC and waits, leaving NO window in which
        to push the bitstream — so it only works when the shell already holds the
        pair (e.g. a re-swap to a cached RM). For a real deploy use
        :meth:`swap_begin` -> push -> :meth:`swap_await`, which is what
        :meth:`pyverify.swap.SwapOrchestrator.deploy` does.
        """
        self.swap_begin(rm, src)
        return self.swap_await()

    def link(self, event: str) -> LinkResponse:
        """``{"op":"link","event":"down"}`` — virtual-PHY link injection
        (ARCHITECTURE_SPEC.md §8.1: host-injected link up/down/speed events).
        """
        resp = self._request({"op": "link", "event": event})
        return LinkResponse(ok=bool(resp["ok"]))

    def commit(self, rm: str) -> CommitResponse:
        """``{"op":"commit","rm":"nanosoc"}`` — the RETIRED v0.11 form.

        net-protocol.md v0.13 REPLACED ``commit`` with a re-push (see
        :meth:`commit_begin` / :meth:`commit_await`, sequenced by
        :meth:`pyverify.swap.SwapOrchestrator.commit`). A v0.13 shell refuses
        this form with ``{"ok":false,"err":"bad args"}``. Kept, wire-transparent,
        so the conformance suites can prove that refusal.
        """
        resp = self._request({"op": "commit", "rm": rm})
        return CommitResponse(ok=bool(resp["ok"]), slot=str(resp.get("slot", "")),
                              err=str(resp.get("err", "")))

    def commit_begin(
        self,
        rm: str,
        *,
        rm_id: "int | str",
        static_id: "int | str",
        clear_len: int,
        clear_crc: "int | str",
        part_len: int,
        part_crc: "int | str",
        src: str = "tcp",
    ) -> None:
        """Send the v0.13 ``commit`` and return IMMEDIATELY (net-protocol.md
        "User microSD": ``commit`` re-pushes the running pair into the card's
        inactive slot).

        Like :meth:`swap_begin`, this PARKS the control connection: push the
        clearing, then the partial, over 6910, THEN read the reply with
        :meth:`commit_await`. ``rm_id`` must be the live ``DFXCTL.RM_ID`` (the
        verified swap's readback) and ``static_id`` the shell's own; the lengths
        and CRC-32s (zlib) are over the exact payload bytes pushed. A refusal
        (``no card``, ``rm mismatch``, ...) comes back at once, before the park.
        ``src`` is ``"tcp"`` only in v0.13.
        """
        for name, value in (("clear_len", clear_len), ("part_len", part_len)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"commit {name} must be an int, got {value!r}")
        self._send_op({
            "op": "commit", "rm": rm, "src": src,
            "rm_id": _hex32_arg(rm_id, "rm_id"),
            "static_id": _hex32_arg(static_id, "static_id"),
            "clear_len": int(clear_len), "clear_crc": _hex32_arg(clear_crc, "clear_crc"),
            "part_len": int(part_len), "part_crc": _hex32_arg(part_crc, "part_crc"),
        })

    def commit_await(self) -> CommitResponse:
        """Collect the reply to a :meth:`commit_begin` (after pushing, or at once
        on a refusal). The shell writes the pair to the card and reads it back
        before it answers, so this needs a generous client timeout, like
        :meth:`swap_await`."""
        resp = self._recv_resp()
        return CommitResponse(ok=bool(resp["ok"]), slot=str(resp.get("slot", "")),
                              err=str(resp.get("err", "")))

    def telemetry(self) -> TelemetryResponse:
        """``{"op":"telemetry"}`` -> ``{"ok":false,"err":"no power sensor",
        "lockup":<bool>}`` — **always a failure** (net-protocol.md v0.6).

        There is no power sensor on this platform, so there are no ``mv``/
        ``ma`` keys on the wire and no such fields on
        :class:`TelemetryResponse` to hold them. ``err`` is parsed (unlike
        :meth:`macgen`'s polymorphic ``err``, telemetry's is unambiguously
        the diagnostic string — there is no success shape for it to mean
        anything else in) and ``lockup`` is parsed regardless of ``ok``,
        because the failure line is the *only* line and it is what carries
        the real ``dut_lockup`` pin.
        """
        resp = self._request({"op": "telemetry"})
        return TelemetryResponse(
            ok=bool(resp["ok"]),
            err=str(resp.get("err", "")),
            lockup=bool(resp.get("lockup", False)),
        )

    def macgen(
        self,
        *,
        gen: bool = True,
        chk: bool = True,
        inject: str = "none",
        injects: "Optional[Sequence[str]]" = None,
    ) -> MacGenResponse:
        """``{"op":"macgen","gen":..,"chk":..,"inject":".."}`` — drive the
        shell's error-inject gen/checker (GENCHK) and read its counters back
        (net-protocol.md "MAC gen/checker control", I10 tail; spec §8).

        ``gen``/``chk`` set ``GENCHK.CTRL.gen_en``/``chk_en``; ``inject`` arms
        the next-frame fault (one of :data:`MACGEN_INJECTS`; ``"none"`` clears
        it). The reply carries ``tx``/``rx``/``err`` — the ``TX_CNT``/
        ``RX_CNT``/``ERR_CNT`` counters *after* the write.

        Inject validation is **opt-in** here (wire-transparent by default),
        mirroring :meth:`set_clk`: pass ``injects=MACGEN_INJECTS`` (or a
        custom sequence) to raise ``ValueError`` on a typo before any
        round-trip. The conformance suites drive *unknown* injects through
        the default to prove the server's own fail-closed rejection; the
        user-facing :meth:`pyverify.board.Mps3Board.macgen` validates by
        default.
        """
        validate_macgen_inject(inject, injects)
        resp = self._request(
            {"op": "macgen", "gen": bool(gen), "chk": bool(chk), "inject": inject}
        )
        ok = bool(resp["ok"])
        # Counters are meaningful only on success; on failure resp["err"] is
        # the diagnostic *string* (polymorphic key — net-protocol.md), so do
        # NOT int() it. Fail-closed to zeros.
        return MacGenResponse(
            ok=ok,
            tx=int(resp.get("tx", 0)) if ok else 0,
            rx=int(resp.get("rx", 0)) if ok else 0,
            err=int(resp.get("err", 0)) if ok else 0,
        )

    def version(self) -> VersionResponse:
        """``{"op":"version"}`` — the build identity of the running image
        (net-protocol.md v0.8): harness release, packed ``HARNESS_VER32``, build
        sha/dirty, the LMB size it was linked for, and its compile-time feature
        set. Build-time constants on the shell, plus one CSR read (the USRACC
        readback behind ``usr_access``, ``null`` where the fabric does not
        answer), so it still replies when the fabric is otherwise unhappy, and
        it is never held like ``swap``.

        Complements :meth:`ping` rather than replacing it: ``ping`` identifies
        the FABRIC (``static_id``) and the resident RM, ``version`` identifies
        the FIRMWARE — and, since Wave B, cross-checks the two against each
        other via ``usr_access``/``skew``. See :class:`VersionResponse`."""
        resp = self._request({"op": "version"})
        ok = bool(resp["ok"])
        features = resp.get("features", ()) if ok else ()
        if not isinstance(features, (list, tuple)):
            raise ShellProtocolError(
                f"version.features must be a JSON array, got {features!r}"
            )
        # `skew` is tri-state on the wire: true / false / null. `null` and an
        # ABSENT key mean the same thing -- no comparison was made -- and both
        # must survive as None. bool(resp.get("skew", False)) would turn "not
        # checked" into "checked, fine", which is the one reading this field
        # exists to prevent, so the null is carried through explicitly.
        raw_skew = resp.get("skew") if ok else None
        skew = None if raw_skew is None else bool(raw_skew)
        usr_access = resp.get("usr_access") if ok else None
        usr_access = "" if usr_access is None else str(usr_access)
        if skew is not None and not usr_access:
            raise ShellProtocolError(
                f"version reported skew={raw_skew!r} with no usr_access: a "
                f"verdict with nothing behind it is not a verdict"
            )
        # `impl` is ADDITIVE (Linux harness): absent -> the bare-metal engine.
        # A null or empty value is read the same way -- a server that emits the
        # key with nothing in it has not claimed to be anything else. A non-
        # string is a protocol violation, not an engine name.
        raw_impl = resp.get("impl") if ok else None
        if raw_impl is not None and not isinstance(raw_impl, str):
            raise ShellProtocolError(f"version.impl must be a string, got {raw_impl!r}")
        impl = raw_impl or IMPL_BARE_METAL
        extra = {k: v for k, v in resp.items() if k not in _VERSION_KEYS} if ok else {}
        # `id_skew` (additive): a reason string, or absent. null / "" = none; a
        # non-string is carried as its text rather than refused (lenient, additive).
        raw_id_skew = resp.get("id_skew") if ok else None
        id_skew = None if raw_id_skew in (None, "") else str(raw_id_skew)
        return VersionResponse(
            ok=ok,
            harness=str(resp.get("harness", "")),
            ver32=str(resp.get("ver32", "")),
            sha=str(resp.get("sha", "")),
            # Wire form is an int (0/1) beside ver32's flags byte; bool() also
            # accepts a JSON bool from a lenient server without changing meaning.
            dirty=bool(resp.get("dirty", 0)),
            lmb_kb=int(resp.get("lmb_kb", 0)) if ok else 0,
            features=tuple(str(f) for f in features),
            usr_access=usr_access,
            skew=skew,
            impl=impl,
            extra=extra,
            id_skew=id_skew,
        )

    def diag(self) -> DiagResponse:
        """``{"op":"diag"}`` — read back the shell's diagnostic counter mailbox
        (firmware/common/diag.h). Idle-only over 6900 (the control channel is
        parked during a swap); during a swap read the same struct over JTAG-MDM
        at the fixed DMEM address. See :class:`DiagResponse` for the fields."""
        resp = self._request({"op": "diag"})
        _t_regs = int(resp.get("touch_regs", 0))
        _t_adc_x = int(resp.get("touch_adc_x", 0))
        _t_adc_y = int(resp.get("touch_adc_y", 0))
        _t_verdict = int(resp.get("touch_verdict", 0))
        return DiagResponse(
            ok=bool(resp["ok"]),
            rx_recover=int(resp.get("rx_recover", 0)),
            rx_dumps=int(resp.get("rx_dumps", 0)),
            rx_drops=int(resp.get("rx_drops", 0)),
            icap_bytes=int(resp.get("icap_bytes", 0)),
            got=int(resp.get("got", 0)),
            expect=int(resp.get("expect", 0)),
            rcv_wnd=int(resp.get("rcv_wnd", 0)),
            rcv_ann_wnd=int(resp.get("rcv_ann_wnd", 0)),
            rx_queued=int(resp.get("rx_queued", 0)),
            pbuf_free=int(resp.get("pbuf_free", 0)),
            grants_sent=int(resp.get("grants_sent", 0)),
            grant_fails=int(resp.get("grant_fails", 0)),
            sndbuf=int(resp.get("sndbuf", 0)),
            snd_wnd=int(resp.get("snd_wnd", 0)),
            tx_frames_sent=int(resp.get("tx_frames_sent", 0)),
            tx_status_drained=int(resp.get("tx_status_drained", 0)),
            tx_fifo_full_drops=int(resp.get("tx_fifo_full_drops", 0)),
            tx_errors=int(resp.get("tx_errors", 0)),
            tx_space_stalls=int(resp.get("tx_space_stalls", 0)),
            tx_iface_errors=int(resp.get("tx_iface_errors", 0)),
            tx_last_status=int(resp.get("tx_last_status", 0)),
            icap_sr_last=int(resp.get("icap_sr_last", 0)),
            icap_eos_status=int(resp.get("icap_eos_status", 0)),
            ovlstore_phase=int(resp.get("ovlstore_phase", 0)),
            ovlstore_detail=int(resp.get("ovlstore_detail", 0)),
            touch_regs=_t_regs,
            touch_adc_x=_t_adc_x,
            touch_adc_y=_t_adc_y,
            touch_verdict=_t_verdict,
            touch_verdict_word=TOUCH_VERDICT_WORDS.get(
                _t_verdict, f"invalid({_t_verdict})"),
            touch_regs_hex=f"0x{_t_regs:08x}",
            touch_adc_x_hex=f"0x{_t_adc_x:08x}",
            touch_adc_y_hex=f"0x{_t_adc_y:08x}",
            svc_count=int(resp.get("svc_count", 0)),
            pass_max_us=int(resp.get("pass_max_us", 0)),
            svc_max_us=int(resp.get("svc_max_us", 0)),
            svc_max_ix=int(resp.get("svc_max_ix", 0)),
            svc_overruns=int(resp.get("svc_overruns", 0)),
            svc_skips=int(resp.get("svc_skips", 0)),
            svc_skipped=int(resp.get("svc_skipped", 0)),
            svc_us_0=int(resp.get("svc_us_0", 0)),
            svc_us_1=int(resp.get("svc_us_1", 0)),
            svc_us_2=int(resp.get("svc_us_2", 0)),
            svc_us_3=int(resp.get("svc_us_3", 0)),
            svc_us_4=int(resp.get("svc_us_4", 0)),
            svc_us_5=int(resp.get("svc_us_5", 0)),
            svc_us_6=int(resp.get("svc_us_6", 0)),
            usd_boot=int(resp.get("usd_boot", 0)),
            present=frozenset(k for k in resp if k != "ok"),
        )

    def dutrx(self) -> DutRxResponse:
        """``{"op":"dutrx"}`` — ONE CHUNK of the frame at the head of the DUT's
        egress capture FIFO (net-protocol.md v0.10). See :class:`DutRxResponse`.

        Takes no arguments: the block's ``DATA`` port is a DESTRUCTIVE read, so
        the FIFO is its own cursor — there is no offset to send and no state on
        the shell. **Every call CONSUMES** what it returns; a dropped reply is a
        lost frame, which is why this rides TCP and not a datagram.

        :meth:`read_dut_frame` is the loop most callers want."""
        resp = self._request({"op": "dutrx"})
        ok = bool(resp["ok"])
        if not ok:
            # The decline shape ("dut_egress not present" on a bitstream with no
            # 0x44B2 slave). Everything else stays at its zero default rather
            # than being parsed out of a failure line -- there is nothing behind
            # those numbers, and a zeroed counter beside ok=False reads exactly
            # like a healthy idle capture.
            return DutRxResponse(ok=False, err=str(resp.get("err", "")))
        raw = resp.get("data", "")
        if not isinstance(raw, str):
            raise ShellProtocolError(f"dutrx.data must be a hex string, got {raw!r}")
        try:
            data = bytes.fromhex(raw)
        except ValueError as exc:
            raise ShellProtocolError(f"dutrx.data is not hex: {exc}") from None
        n = int(resp.get("n", 0))
        if len(data) != n:
            # A torn or mis-encoded chunk. Refuse it rather than hand back a
            # short frame that would reassemble into a plausible WRONG one.
            raise ShellProtocolError(
                f"dutrx said n={n} but carried {len(data)} bytes of data"
            )
        return DutRxResponse(
            ok=True,
            frame_len=int(resp.get("len", 0)),
            off=int(resp.get("off", 0)),
            n=n,
            more=bool(resp.get("more", False)),
            last=bool(resp.get("last", False)),
            frames=int(resp.get("frames", 0)),
            rx=int(resp.get("rx", 0)),
            drop_full=int(resp.get("drop_full", 0)),
            drop_giant=int(resp.get("drop_giant", 0)),
            ovf=bool(resp.get("ovf", False)),
            desync=bool(resp.get("desync", False)),
            data=data,
        )

    def read_dut_frame(self) -> "tuple[Optional[bytes], DutRxResponse]":
        """Read ONE whole frame out of the DUT-egress FIFO, following ``more``
        across as many :meth:`dutrx` chunks as it takes.

        Returns ``(frame, last_chunk)``. ``frame`` is ``None`` when the FIFO had
        nothing waiting (a RESULT, not an error — an idle DUT looks exactly like
        this) or when the verb declined; ``last_chunk`` always carries the reply
        that ended the read, so the counters and the sticky flags are available
        either way. **Read them**: a capture that dropped frames reports it
        nowhere else.

        Two protocol errors are raised rather than papered over, because both
        produce a frame that LOOKS fine:

        - a chunk whose ``off`` does not continue the previous one — the
          reassembly would splice two frames into one plausible frame;
        - ``more`` with ``n == 0``, which makes no progress and would otherwise
          spin forever against a stuck shell.
        """
        chunk = self.dutrx()
        if not chunk.ok or chunk.n == 0:
            return None, chunk

        parts = [chunk.data]
        want = chunk.frame_len
        got = chunk.n
        while chunk.more:
            prev = chunk
            chunk = self.dutrx()
            if not chunk.ok:
                raise ShellProtocolError(
                    f"dutrx failed mid-frame ({chunk.err!r}): "
                    f"{got} of {want} bytes read"
                )
            if chunk.n == 0:
                raise ShellProtocolError(
                    f"dutrx made no progress mid-frame: more=True with n=0 at "
                    f"offset {got} of {want}"
                )
            if chunk.off != got or chunk.frame_len != want:
                raise ShellProtocolError(
                    f"dutrx chunk does not continue the frame: expected "
                    f"off={got} len={want}, got off={chunk.off} "
                    f"len={chunk.frame_len} (previous chunk ended at "
                    f"{prev.off + prev.n})"
                )
            parts.append(chunk.data)
            got += chunk.n
        return b"".join(parts), chunk

    # -- net-protocol v0.11 ------------------------------------------------ #

    def stats(self) -> StatsResponse:
        """``{"op":"stats"}`` -- the board state in one line (v0.11). A shell
        older than v0.11 answers ``unknown op``: that comes back as
        ``ok=False`` with the error, never raised."""
        resp = self._request({"op": "stats"})
        if not bool(resp["ok"]):
            return StatsResponse(ok=False, err=str(resp.get("err", "")), raw=dict(resp))
        b = lambda k: bool(resp.get(k, False))           # noqa: E731
        i = lambda k: int(resp.get(k, 0))                # noqa: E731
        t = lambda k: str(resp.get(k, ""))               # noqa: E731
        return StatsResponse(
            ok=True, up_ms=i("up_ms"), sid=t("sid"), rm=t("rm"), rm_ok=b("rm_ok"),
            lock=b("lock"), clk_sel=i("clk_sel"), mmcm=b("mmcm"),
            clk_alive=b("clk_alive"), dut_rst=b("dut_rst"), rp_rst=b("rp_rst"),
            decpl=b("decpl"), link=b("link"), spd=i("spd"), fdx=b("fdx"),
            mac=t("mac"), swap=t("swap"), swap_ok=b("swap_ok"), swap_n=i("swap_n"),
            icap=i("icap"), rxdrop=i("rxdrop"), txerr=i("txerr"),
            swap_err=t("swap_err"), clr_ok=b("clr_ok"), dut_mhz=i("dut_mhz"),
            svc_max_us=i("svc_max_us"), svc_skipped=i("svc_skipped"),
            raw=dict(resp),
            os_up_ms=int(resp["os_up_ms"]) if resp.get("os_up_ms") is not None else None,
        )

    def log(self, off: int = 0) -> LogResponse:
        """``{"op":"log","off":N}`` -- one chunk (<= :data:`LOG_CHUNK` bytes) of
        the shell console from stream offset ``off`` (v0.11)."""
        if off < 0:
            raise ValueError(f"log offset must be >= 0, got {off}")
        resp = self._request({"op": "log", "off": int(off)})
        if not bool(resp["ok"]):
            return LogResponse(ok=False, err=str(resp.get("err", "")))
        raw = resp.get("data", "")
        if not isinstance(raw, str):
            raise ShellProtocolError(f"log.data must be a hex string, got {raw!r}")
        try:
            data = bytes.fromhex(raw)
        except ValueError as exc:
            raise ShellProtocolError(f"log.data is not hex: {exc}") from None
        n = int(resp.get("n", 0))
        if len(data) != n:
            raise ShellProtocolError(f"log said n={n} but carried {len(data)} bytes")
        return LogResponse(ok=True, off=int(resp.get("off", 0)), n=n,
                           more=bool(resp.get("more", False)),
                           dropped=int(resp.get("dropped", 0)), data=data)

    def read_log(self, off: int = 0, max_chunks: int = 64) -> "tuple[bytes, LogResponse, int]":
        """Follow ``more`` from ``off`` and return ``(text, last_reply,
        next_off)``. ``next_off`` is where to resume a tail. Raises on a chunk
        that makes no progress while claiming ``more`` (a stuck shell), and caps
        the walk at ``max_chunks`` so a busy console cannot pin the caller."""
        parts = []
        r = self.log(off)
        if not r.ok:
            return b"", r, off
        parts.append(r.data)
        nxt = r.off + r.n
        chunks = 1
        while r.more and chunks < max_chunks:
            r = self.log(nxt)
            if not r.ok:
                break
            if r.n == 0 and r.more:
                raise ShellProtocolError(f"log made no progress at off={nxt}")
            parts.append(r.data)
            nxt = r.off + r.n
            chunks += 1
        return b"".join(parts), r, nxt

    def touch_cal(self, act: str = "get", coeffs: "Optional[Sequence[int]]" = None) -> TouchCalResponse:
        """``{"op":"touch_cal","act":...}`` (v0.11). ``act`` in get / set / raw /
        default; ``set`` needs ``coeffs`` = (ax, bx, cx, ay, by, cy, shift)."""
        if act not in ("get", "set", "raw", "default"):
            raise ValueError(f"touch_cal act must be get/set/raw/default, got {act!r}")
        op: dict[str, Any] = {"op": "touch_cal", "act": act}
        if act == "set":
            if coeffs is None or len(coeffs) != len(TOUCH_CAL_KEYS):
                raise ValueError("touch_cal set needs 7 ints: ax bx cx ay by cy shift")
            op.update({k: int(v) for k, v in zip(TOUCH_CAL_KEYS, coeffs)})
        resp = self._request(op)
        if not bool(resp["ok"]):
            return TouchCalResponse(ok=False, err=str(resp.get("err", "")))
        if "raw_x" in resp:
            return TouchCalResponse(
                ok=True, raw_x=int(resp["raw_x"]), raw_y=int(resp.get("raw_y", 0)),
                raw_z=int(resp.get("raw_z", 0)), seen=int(resp.get("seen", 0)),
                x=int(resp.get("x", 0)), y=int(resp.get("y", 0)))
        return TouchCalResponse(ok=True,
                                coeffs=tuple(int(resp[k]) for k in TOUCH_CAL_KEYS))

    def reboot(self) -> RebootResponse:
        """``{"op":"reboot"}`` (v0.11). On ``ok`` the shell restarts within
        ``in_ms``; this connection will drop. Confirm with a fresh connection:
        ``ping`` answers the same shell_id and ``stats().up_ms`` has restarted."""
        resp = self._request({"op": "reboot"})
        if not bool(resp["ok"]):
            return RebootResponse(ok=False, err=str(resp.get("err", "")))
        return RebootResponse(ok=True, in_ms=int(resp.get("in_ms", 0)))

    # -- net-protocol v0.16: the board identity (Linux harness) ------------ #

    def identity(self) -> "dict[str, Any]":
        """``{"op":"identity"}`` (v0.16): this board's label / hostname / ip / mac,
        each field's source, the stage0 bake, the override in force and what the
        next boot changes (``pending``). The raw reply (``ok`` False on an engine
        without it: ``code`` ``not_supported``)."""
        return self._request({"op": "identity"})

    def identity_set(self, *, clear: bool = False, **fields: str) -> "dict[str, Any]":
        """``{"op":"identity_set",...}`` (v0.16): write the /persist override
        (any of ``label``, ``hostname``, ``ip``, ``mac``; ``""`` drops a key), or
        ``clear=True`` to remove it. Applies at the next boot; claim-locked like the
        slot mutations. The raw reply (``code`` ``locked`` / ``no_persist`` /
        ``invalid`` on a refusal)."""
        unknown = set(fields) - {"label", "hostname", "ip", "mac"}
        if unknown:
            raise ValueError(f"identity_set takes label/hostname/ip/mac, not {sorted(unknown)}")
        op: "dict[str, Any]" = {"op": "identity_set"}
        if clear:
            op["clear"] = True
        op.update(fields)
        return self._request(op)

    def locate(self, seconds: int, who: str = "") -> "dict[str, Any]":
        """``{"op":"locate","s":N[,"who":..]}`` (v0.16): blink the panel backlight
        at 2 Hz and show "IDENTIFY: <who>" for N seconds (1..30); 0 stops. A new one
        replaces the old; a tap on the panel ends it. The raw reply
        (``until_ms`` = ms from now to its end)."""
        op: "dict[str, Any]" = {"op": "locate", "s": int(seconds)}
        if who:
            op["who"] = who
        return self._request(op)

    # -- net-protocol v0.17: presence + the front panel (Linux harness) ----- #

    def hello(self, sid: str, who: str, app: str, *, name: str = "", role: str = "watch",
              lease: "Optional[dict[str, Any]]" = None,
              job: "Optional[dict[str, Any]]" = None, ttl: int = 90) -> "dict[str, Any]":
        """``{"op":"hello",...}`` (v0.17): tell the board who is connected
        (:func:`hello_message` caps every field; ValueError if the line would pass
        the harness's 256 B). The raw reply: ``{ok, op, sessions: N, panel: {page,
        owner, pending, banner, card, seq}, events: [{seq, k, on, ms_ago}]}``;
        ``code`` ``not_supported`` on an engine without feature ``presence``."""
        msg = hello_message(sid, who, app, name=name, role=role, lease=lease, job=job, ttl=ttl)
        hello_line(msg)
        return self._request(msg)

    def panel(self) -> "dict[str, Any]":
        """``{"op":"panel"}`` (v0.17): what the panel shows -- page, owner, pending,
        banner, card, touch {present, cal}, the live sessions [{sid, who, role,
        age_s}], seq and the tap ring. The raw reply."""
        return self._request({"op": "panel"})

    def panel_frame(self) -> "dict[str, Any]":
        """The panel's text grid in its two halves (``frame`` "a" = rows 0-7, then
        "b" = rows 8-14, on this connection): ``{ok, theme, rows: [15 x 40], roles:
        600 codes}`` (a role code is ``chr(ord("a") + i)`` for PANEL_ROLES[i]), or
        the first refusal as it came."""
        rows: "list[str]" = []
        roles = ""
        theme = ""
        for part, _r0, _r1 in PANEL_FRAME_HALVES:
            resp = self._request({"op": "panel", "frame": part})
            if not resp.get("ok"):
                return resp
            rows += list(resp.get("rows") or ())
            roles += str(resp.get("roles") or "")
            theme = str(resp.get("theme") or theme)
        return {"ok": True, "theme": theme, "rows": rows, "roles": roles}

    def panel_page(self, page: str) -> "dict[str, Any]":
        """``{"op":"panel","page":"status"|"apps"}`` (v0.17): turn the panel's page,
        only while the harness owns it (``code`` ``held``: "dut owns the panel").
        CLAIM-LOCKED like ``identity_set`` (``code`` ``locked``). The raw reply."""
        return self._request({"op": "panel", "page": page})

    # -- net-protocol v0.13: the user microSD ------------------------------ #

    @staticmethod
    def _usd_response(resp: "dict[str, Any]") -> UsdResponse:
        if not bool(resp["ok"]):
            return UsdResponse(ok=False, err=str(resp.get("err", "")), raw=dict(resp))
        dflt = resp.get("default")
        default = None
        if isinstance(dflt, dict):
            default = UsdDefault(rm_id=str(dflt.get("rm_id", "")),
                                 static_id=str(dflt.get("static_id", "")),
                                 slot=str(dflt.get("slot", "")))
        elif dflt is not None:
            raise ShellProtocolError(f"usd.default must be an object, got {dflt!r}")
        card_mb = resp.get("card_mb")
        return UsdResponse(
            ok=True,
            present=bool(resp.get("present", False)),
            state=str(resp.get("state", "")),
            text=str(resp.get("text", "")),
            card_mb=int(card_mb) if card_mb is not None else None,
            default=default,
            boot=str(resp.get("boot", "")),
            raw=dict(resp),
        )

    def usd(self) -> UsdResponse:
        """``{"op":"usd"}`` -- the user microSD and its overlay store (v0.13).
        Never held: it answers during a swap. No card is ``ok`` with
        ``present=False``, not an error. A shell older than v0.13 answers
        ``unknown op``: that comes back as ``ok=False``, never raised."""
        return self._usd_response(self._request({"op": "usd"}))

    def usd_format(self, confirm: str) -> UsdResponse:
        """``{"op":"usd","action":"format","confirm":...}``. ``confirm`` is
        :data:`USD_CONFIRM_FORMAT` (``"erase"``: the plain format, which writes
        only into a ``0xDA`` partition or onto a truly blank card) or
        :data:`USD_CONFIRM_WIPE` (``"erase-all"``: the explicit wipe, refused
        with ``wipe disabled`` under Linux). Sent verbatim, so a server's
        ``confirm required`` refusal can be exercised."""
        return self._usd_response(
            self._request({"op": "usd", "action": "format", "confirm": confirm}))

    def usd_clear(self) -> UsdResponse:
        """``{"op":"usd","action":"clear"}`` -- invalidate the default, so the
        next power-on boots the greybox."""
        return self._usd_response(self._request({"op": "usd", "action": "clear"}))

    def usd_rescan(self) -> UsdResponse:
        """``{"op":"usd","action":"rescan"}`` -- re-probe the card now."""
        return self._usd_response(self._request({"op": "usd", "action": "rescan"}))

    def display(
        self,
        owner: str,
        *,
        owners: "Optional[Sequence[str]]" = None,
    ) -> DisplayResponse:
        """``{"op":"display","owner":"dut|harness|toggle"}`` — flip the on-board
        CLCD panel's owner via the shell's existing 6900 control channel (the
        remote arm of the CLCD KVM; the button ``USER_nPB[1]`` and a local CSR
        write are the other two request sources). Writes the frozen
        ``CLCDKVM.CTRL.src_sel`` (+ ``src_sel_we``) and reads ``STATUS.owner``
        back — a plain send-now response, NOT held like ``swap``.

        Owner validation is **opt-in** here (wire-transparent by default),
        mirroring :meth:`set_clk`/:meth:`macgen`: pass ``owners=DISPLAY_OWNERS``
        (or a custom sequence) to raise ``ValueError`` on a typo before any
        round-trip. The user-facing
        :meth:`pyverify.board.Mps3Board.set_display_owner` validates by default.

        See :meth:`display_owner` for the read-only query.
        """
        validate_display_owner(owner, owners)
        resp = self._request({"op": "display", "owner": owner})
        return DisplayResponse(
            ok=bool(resp["ok"]),
            owner=str(resp.get("owner", "")),
            err=str(resp.get("err", "")),
        )

    def display_owner(self) -> DisplayResponse:
        """``{"op":"display","owner":"query"}`` — report the CLCD KVM's current
        committed owner **without moving it** (a read-only ``display``). The
        reply's :attr:`~DisplayResponse.owner` is ``CLCDKVM.STATUS.owner``. On a
        board with no KVM slave this is the same ``clcd_kvm not present``
        failure as :meth:`display`."""
        resp = self._request({"op": "display", "owner": "query"})
        return DisplayResponse(
            ok=bool(resp["ok"]),
            owner=str(resp.get("owner", "")),
            err=str(resp.get("err", "")),
        )

    def display_settled(
        self,
        owner: str,
        *,
        timeout: float = 2.0,
        interval: float = 0.05,
        owners: "Optional[Sequence[str]]" = DISPLAY_OWNERS,
        _clock=None,
        _sleep=None,
    ) -> DisplaySettleResponse:
        """Flip the panel owner and WAIT for the handover to commit.

        `display` + a bounded re-`query` loop, and nothing else: no new wire
        verb, no firmware change, no contract edit. It exists because
        net-protocol.md's own "Display" section ends with *"a client confirms
        the landing with a follow-up query"* -- a step every caller has to get
        right, and the board proof in docs/CLCD_KVM_PLAN.md "Proving it" cannot
        be one command without.

        `"toggle"` is resolved to a CONCRETE target first (one read-only query,
        then the other side), because "did the toggle land?" is otherwise
        unanswerable: the reply and the eventual state can be the same value.

        Returns without raising in every ordinary outcome. `ok=False` is the
        shell declining (`clcd_kvm not present` on a KVM-less bitstream, or a
        bad owner). `ok=True, landed=False` is the honest "requested, did not
        commit in time" -- INCONCLUSIVE, and the caller must say so rather than
        report a flip that may not have happened.

        `_clock` / `_sleep` are seams for the tests; they default to
        `time.monotonic` / `time.sleep`.
        """
        validate_display_owner(owner, owners)
        clock = _clock or time.monotonic
        sleep = _sleep or time.sleep

        target = owner
        if owner == "toggle":
            pre = self.display_owner()
            if not pre.ok:
                return DisplaySettleResponse(ok=False, requested=owner,
                                             err=pre.err)
            target = "dut" if pre.owner == "harness" else "harness"

        t0 = clock()
        first = self.display(owner, owners=owners)
        if not first.ok:
            return DisplaySettleResponse(ok=False, requested=target,
                                         owner=first.owner, err=first.err)
        if first.owner == target:
            return DisplaySettleResponse(ok=True, requested=target,
                                         owner=first.owner, landed=True,
                                         polls=0, waited_s=clock() - t0)

        polls, last = 0, first
        while clock() - t0 < timeout:
            sleep(interval)
            polls += 1
            last = self.display_owner()
            if not last.ok:
                return DisplaySettleResponse(ok=False, requested=target,
                                             owner=last.owner, polls=polls,
                                             waited_s=clock() - t0,
                                             err=last.err)
            if last.owner == target:
                return DisplaySettleResponse(ok=True, requested=target,
                                            owner=last.owner, landed=True,
                                            polls=polls, waited_s=clock() - t0)
        return DisplaySettleResponse(
            ok=True, requested=target, owner=last.owner, landed=False,
            polls=polls, waited_s=clock() - t0,
            err=f"owner did not commit to {target!r} within {timeout}s "
                f"(last committed owner {last.owner!r})")
