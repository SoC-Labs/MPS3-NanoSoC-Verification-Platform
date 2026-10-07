"""Importable Python behaviour model of the firmware swap FSM.

Extracted verbatim (W-FAKESHELL, 2026-07-04) from
``tests/integration/test_swap_sequence.py``, which remains the primary
consumer (it now imports this module) — see that file's docstring for the
full provenance story. In brief: this is a faithful **Python port of the
real firmware state machine**, ``firmware/coordinator/swap_fsm.c`` +
``swap_fsm_transitions.c`` (states, transitions, and register pokes taken
directly from those files; every method cites the C it mirrors), including
the I2-resolved clearing-cache handling (``SWAP_AWAIT_INCOMING_CLEARING`` /
``SWAP_CACHE_CLEARING``) and the I25-fixed real ``DFXCTL.RM_ID`` verify
compare.

Why it lives in the package now: the fake shell
(:mod:`pyverify.testing.fakeshell`) needs the same behaviour model to drive
its ``swap`` verb, so the model must be importable rather than embedded in
a test file. It stays a genuinely independent second implementation of the
C (alongside ``firmware/test/test_swap_fsm_hw.c``'s mock-register harness)
that can catch a divergence in either direction — and, run behind the fake
shell, it doubles as the **executable spec for A3's firmware**.

Register constants: transcribed from ``docs/contracts/shell-regmap.md``
v0.1 (same values as ``tests/common/regmap.py``, which cannot be imported
here — it needs cocotb, and this package is stdlib-only). The integration
test cross-checks the two transcriptions implicitly: its assertions compare
this model's register effects against ``regmap.py``'s constants, so a
divergence in either copy fails the suite.

Stdlib-only; the one non-stdlib-shaped dependency (the bitstream header
framing) is reused from the pusher via :mod:`pyverify.testing._pusher` —
never re-implemented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ._pusher import BitstreamFramingError, BitstreamHeader, BitstreamKind

__all__ = [
    # shell-regmap.md v0.1 constants (subset the model needs)
    "RESET_CTRL_DUT_RESETN",
    "RESET_CTRL_RP_RESETN",
    "RESET_CTRL_DBG_RESETN",
    "DECOUPLE_EN",
    "SHUTDOWN_AXI_SHUTDOWN",
    "DFXCTL_STATUS_DECOUPLED",
    "DFXCTL_STATUS_RP_IN_RESET",
    "DFXCTL_RM_STATUS_RM_ID_VALID",
    "DFXCTL_RM_STATUS_DUT_LOCKUP",
    "LINK_EVENT_FORCE_DOWN",
    "LINK_EVENT_PULSE",
    # the model
    "STATIC_ID",
    "STATES",
    "next_state",
    "MockRegs",
    "ClearingRef",
    "SwapFsm",
    "make_regs_confirmed",
    "greybox_clearing",
    "validate_header_like_config_agent",
]

# --------------------------------------------------------------------------- #
# Register-bit constants — shell-regmap.md v0.1 (see module docstring for why
# these are transcribed here rather than imported from tests/common/regmap.py).
# --------------------------------------------------------------------------- #

# CLKRST.RESET_CTRL (0x44A0_0000 @0x00); 1 = released.
RESET_CTRL_DUT_RESETN = 1 << 0
RESET_CTRL_RP_RESETN = 1 << 1
RESET_CTRL_DBG_RESETN = 1 << 2

# DFXCTL (0x44A1_0000): DECOUPLE @0x00 / SHUTDOWN @0x04 / STATUS @0x08.
DECOUPLE_EN = 1 << 0
SHUTDOWN_AXI_SHUTDOWN = 1 << 0
DFXCTL_STATUS_DECOUPLED = 1 << 0
DFXCTL_STATUS_RP_IN_RESET = 1 << 1

# DFXCTL.RM_STATUS @0x14 (v0.1's real RM-load-verify pair, I8/I25).
DFXCTL_RM_STATUS_RM_ID_VALID = 1 << 0
DFXCTL_RM_STATUS_DUT_LOCKUP = 1 << 1

# VPHY.LINK_EVENT (0x44A3_0000 @0x08).
LINK_EVENT_FORCE_DOWN = 1 << 0
LINK_EVENT_PULSE = 1 << 1

#: Default running-shell static_id used by the model's fixtures/tests
#: (matches the contract examples in overlay-manifest.md).
STATIC_ID = 0xA1B2C3D4


# --------------------------------------------------------------------------- #
# Pure transition table -- Python port of swap_fsm_transitions.c's
# swap_fsm_next_state(). Every branch below cites the C `case` it mirrors;
# see that file for the authoritative version (cross-checked directly
# against a handful of its own test_swap_fsm_transitions.c cases in
# tests/integration/test_swap_sequence.py's
# test_next_state_matches_known_c_transition_cases).
# --------------------------------------------------------------------------- #

STATES = (
    "IDLE", "GATE", "DECOUPLE_ASSERT", "STREAM_CLEARING",
    "AWAIT_INCOMING_CLEARING", "AWAIT_PARTIAL", "STREAM_PARTIAL",
    "VERIFY", "CACHE_CLEARING", "RELEASE", "DONE", "FAILED",
)


def next_state(cur: str, **inputs) -> str:
    """Total function: every state has a defined next state for every
    possible `inputs` (unset booleans default False, matching the C
    struct's `{0}`-initialized `mps3_swap_transition_inputs_t`)."""
    get = inputs.get
    if cur == "IDLE":
        return "IDLE"
    if cur == "GATE":
        return "DECOUPLE_ASSERT"
    if cur == "DECOUPLE_ASSERT":
        return "STREAM_CLEARING" if get("decouple_confirmed") else "DECOUPLE_ASSERT"
    if cur == "STREAM_CLEARING":
        if not get("clearing_cache_valid"):
            return "FAILED"
        return "AWAIT_INCOMING_CLEARING" if get("clearing_stream_done") else "STREAM_CLEARING"
    if cur == "AWAIT_INCOMING_CLEARING":
        # fail-IDLE (swap_fsm_transitions.c): a payload that never arrives must
        # not park the client forever. -> FAILED (still pre-RELEASE, so DECOUPLE
        # is asserted and rp_resetn held: the RP is already inert).
        if get("incoming_clearing_ready"):
            return "AWAIT_PARTIAL"
        return "FAILED" if get("await_timeout") else "AWAIT_INCOMING_CLEARING"
    if cur == "AWAIT_PARTIAL":
        if get("partial_ready"):
            return "STREAM_PARTIAL"
        return "FAILED" if get("await_timeout") else "AWAIT_PARTIAL"
    if cur == "STREAM_PARTIAL":
        return "VERIFY" if get("partial_stream_done") else "STREAM_PARTIAL"
    if cur == "VERIFY":
        return "CACHE_CLEARING" if get("verify_ok") else "FAILED"
    if cur == "CACHE_CLEARING":
        return "RELEASE"
    if cur == "RELEASE":
        return "DONE" if get("release_confirmed") else "RELEASE"
    if cur in ("DONE", "FAILED"):
        return "IDLE"
    raise ValueError(f"unknown state {cur!r}")


# --------------------------------------------------------------------------- #
# Mock register file — just enough of CLKRST/DFXCTL/VPHY for the swap_fsm.c
# ordering to be observable; not a cocotb DUT, just a dict-backed stand-in.
# --------------------------------------------------------------------------- #

@dataclass
class MockRegs:
    dfxctl_decouple: int = 0
    dfxctl_shutdown: int = 0
    dfxctl_status: int = 0          # firmware/test controls this to simulate the vendor IP's own status
    # DFXCTL.RM_ID @0x10 / RM_STATUS @0x14 -- shell-regmap.md v0.1's real
    # (no longer placeholder) RM-load-verify offsets.
    dfxctl_rm_id: int = 0
    dfxctl_rm_status: int = 0
    clkrst_reset_ctrl: int = (RESET_CTRL_DUT_RESETN | RESET_CTRL_RP_RESETN | RESET_CTRL_DBG_RESETN)
    vphy_link_event: int = 0
    calls: list = field(default_factory=list)

    def set_bits(self, reg: str, mask: int):
        setattr(self, reg, getattr(self, reg) | mask)
        self.calls.append(("set", reg, mask))

    def clr_bits(self, reg: str, mask: int):
        setattr(self, reg, getattr(self, reg) & ~mask)
        self.calls.append(("clr", reg, mask))

    def write(self, reg: str, value: int):
        setattr(self, reg, value)
        self.calls.append(("write", reg, value))


@dataclass
class ClearingRef:
    """Python port of `mps3_clearing_ref_t` (swap_fsm.h) -- the ONE
    currently-loaded RM's clearing bitstream reference (I2: no more
    multi-RM keyed cache; exactly one live reference at a time)."""
    valid: bool = False
    rm_id: int = 0
    static_id: int = 0
    len_words: int = 0
    crc32: int = 0


class SwapFsm:
    """Python port of swap_fsm.c's impure `step_*()` functions, each
    gathering inputs the same way the C does, then deferring to
    `next_state()` above (the pure table) exactly like swap_fsm.c defers
    to `swap_fsm_next_state()` -- see swap_fsm.h's top-of-file comment for
    the state list this mirrors.
    """

    def __init__(self, regs: MockRegs, current_clearing: ClearingRef, running_static_id: int = STATIC_ID):
        self.regs = regs
        self.current_clearing = current_clearing   # mirrors g_current_rm_clearing
        self.staged_incoming_clearing = ClearingRef()  # mirrors s_staged_incoming_clearing
        self.running_static_id = running_static_id
        self.state = "IDLE"
        self.gated = {"xvc": False, "swd": False, "uart": False, "link": False}
        self.current_rm_id = 0            # mirrors g_shell_state.current_rm_id
        self.target_rm_id = 0             # mirrors s_target_rm_id (I14/I25: resolved from the partial's own header, not swap_fsm_start()'s rm_name)
        self._words_total = 0
        self._words_done = 0
        self._incoming_clearing_pending = None   # driver-supplied, mirrors config_agent_take_validated_clearing()
        self._partial_pending = None             # driver-supplied, mirrors config_agent_take_validated_partial()
        self.history = []

    def start(self, target_rm_name: str = "nanosoc"):
        assert self.state == "IDLE", "a swap is already in flight (swap_fsm_start() returns -1 in this case)"
        self.target_rm_name = target_rm_name
        self.target_rm_id = 0
        self.staged_incoming_clearing = ClearingRef()
        self._incoming_clearing_pending = None
        self._partial_pending = None
        self.state = "GATE"

    def step(self):
        self.history.append(self.state)
        method = getattr(self, f"_step_{self.state.lower()}", None)
        if method is not None:
            method()

    def _step_gate(self):
        """Mirrors step_gate(): gate XVC/SWD/UART, force VPHY link down,
        unconditional -> DECOUPLE_ASSERT."""
        self.gated = {k: True for k in self.gated}
        self.regs.write("vphy_link_event", LINK_EVENT_FORCE_DOWN)
        self.state = next_state(self.state)

    def _step_decouple_assert(self):
        """Mirrors step_decouple_assert(): assert DECOUPLE+SHUTDOWN, clear
        RESET_CTRL.rp_resetn; advances only once STATUS confirms both
        decoupled+rp_in_reset. On advance, seeds the chunk counters from
        the CACHED outgoing RM's clearing (I2 -- always resident)."""
        self.regs.set_bits("dfxctl_decouple", DECOUPLE_EN)
        self.regs.set_bits("dfxctl_shutdown", SHUTDOWN_AXI_SHUTDOWN)
        self.regs.clr_bits("clkrst_reset_ctrl", RESET_CTRL_RP_RESETN)
        both = DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET
        decouple_confirmed = (self.regs.dfxctl_status & both) == both

        nxt = next_state(self.state, decouple_confirmed=decouple_confirmed)
        if nxt == "STREAM_CLEARING":
            self._words_total = self.current_clearing.len_words
            self._words_done = 0
        self.state = nxt

    def _step_stream_clearing(self):
        """Mirrors step_stream_clearing(): I2's fail-closed guard --
        `g_current_rm_clearing.valid` gates before anything about the
        streaming progress matters. This model completes the "transfer"
        in one call once the cache is valid (the real chunking/timing is
        explicitly not part of the *ordering* this model cares about)."""
        clearing_cache_valid = self.current_clearing.valid
        clearing_stream_done = False
        if clearing_cache_valid:
            self._words_done = self._words_total
            clearing_stream_done = self._words_done >= self._words_total
        self.state = next_state(
            self.state,
            clearing_cache_valid=clearing_cache_valid,
            clearing_stream_done=clearing_stream_done,
        )

    def _step_await_incoming_clearing(self):
        """Mirrors step_await_incoming_clearing(): captures/stages the
        INCOMING RM's clearing bitstream (net-protocol.md step 2's
        counterpart on the "host supplies the incoming pair" side) --
        NOT streamed to HWICAP this swap; only promoted to
        g_current_rm_clearing at SWAP_CACHE_CLEARING, once VERIFY
        succeeds. The driver arranges `deliver_incoming_clearing(...)` to
        simulate config_agent_take_validated_clearing() succeeding."""
        ready = False
        if self._incoming_clearing_pending is not None:
            info = self._incoming_clearing_pending
            self.staged_incoming_clearing = ClearingRef(
                valid=True, rm_id=info["rm_id"], static_id=info["static_id"],
                len_words=info["len_words"], crc32=info["crc32"],
            )
            ready = True
        self.state = next_state(self.state, incoming_clearing_ready=ready)

    def _step_await_partial(self):
        """Mirrors step_await_partial(): waits for config_agent to hand
        off a header-validated partial. I14/I25: the partial's own header
        carries the numeric target rm_id -- captured into
        `self.target_rm_id` here so SWAP_VERIFY has something concrete to
        compare DFXCTL.RM_ID against."""
        ready = False
        if self._partial_pending is not None:
            info = self._partial_pending
            self.target_rm_id = info["rm_id"]
            self._words_total = info["len_words"]
            self._words_done = 0
            ready = True
        self.state = next_state(self.state, partial_ready=ready)

    def _step_stream_partial(self):
        """Mirrors step_stream_partial() (see _step_stream_clearing note
        on the chunking simplification)."""
        self._words_done = self._words_total
        partial_stream_done = self._words_done >= self._words_total
        self.state = next_state(self.state, partial_stream_done=partial_stream_done)

    def _step_verify(self):
        """Mirrors step_verify() **I25-fixed**: reads DFXCTL.RM_ID +
        RM_STATUS.rm_id_valid and does a REAL compare against
        `self.target_rm_id` (captured in _step_await_partial() from the
        partial's own header, I14) -- no more hardcoded `verified = true`.
        On success, adopts the readback as `current_rm_id` (matching
        `g_shell_state.current_rm_id = rm_id` in the real C, run only on
        the CACHE_CLEARING-bound path)."""
        rm_id_readback = self.regs.dfxctl_rm_id
        rm_id_valid = bool(self.regs.dfxctl_rm_status & DFXCTL_RM_STATUS_RM_ID_VALID)
        verify_ok = rm_id_valid and (rm_id_readback == self.target_rm_id)

        nxt = next_state(self.state, verify_ok=verify_ok)
        if nxt == "CACHE_CLEARING":
            self.current_rm_id = rm_id_readback
        self.state = nxt

    def _step_cache_clearing(self):
        """Mirrors step_cache_clearing(): net-protocol.md step 5 -- promote
        the staged incoming clearing (captured during
        AWAIT_INCOMING_CLEARING) into `self.current_clearing`. This is the
        step whose earlier ABSENCE was the I2 gap (a second consecutive
        swap used to fail closed); see the integration suite's
        test_two_consecutive_swaps_succeed_now_that_cache_clearing_is_
        wired_up for the direct positive proof the gap is closed."""
        self.current_clearing = self.staged_incoming_clearing
        self.staged_incoming_clearing = ClearingRef()
        self.state = next_state(self.state)

    def _step_release(self):
        """Mirrors step_release(): release DECOUPLE/SHUTDOWN, set
        RESET_CTRL.rp_resetn; only ungates XVC/SWD/UART + clears
        VPHY_LINK_EVENT once STATUS actually confirms decoupled==0 &&
        rp_in_reset==0 (the real C used to ungate unconditionally; fixed
        alongside I2/I25 -- see swap_fsm.c's comment on this)."""
        self.regs.clr_bits("dfxctl_decouple", DECOUPLE_EN)
        self.regs.clr_bits("dfxctl_shutdown", SHUTDOWN_AXI_SHUTDOWN)
        self.regs.set_bits("clkrst_reset_ctrl", RESET_CTRL_RP_RESETN)
        both = DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET
        release_confirmed = (self.regs.dfxctl_status & both) == 0

        nxt = next_state(self.state, release_confirmed=release_confirmed)
        if nxt == "DONE":
            self.gated = {k: False for k in self.gated}
            self.regs.write("vphy_link_event", 0)
        self.state = nxt

    def _step_done(self):
        """Mirrors step_done_or_failed(true): -> SWAP_IDLE."""
        self.state = next_state(self.state)

    def _step_failed(self):
        """Mirrors step_done_or_failed(false): -> SWAP_IDLE. The safe-
        failure state (decoupled + rp held) was already latched by
        whichever step transitioned to FAILED."""
        self.state = next_state(self.state)

    def deliver_incoming_clearing(self, rm_id: int, len_words: int, static_id: Optional[int] = None, crc32: int = 0):
        """Driver helper simulating config_agent handing off the incoming
        pair's validated CLEARING bitstream (mirrors
        config_agent_take_validated_clearing() returning 0)."""
        self._incoming_clearing_pending = {
            "rm_id": rm_id, "static_id": static_id if static_id is not None else self.running_static_id,
            "len_words": len_words, "crc32": crc32,
        }

    def deliver_partial(self, rm_id: int, len_words: int = 4, static_id: Optional[int] = None, crc32: int = 0):
        """Driver helper simulating config_agent handing off the incoming
        pair's validated PARTIAL (mirrors
        config_agent_take_validated_partial() returning 0)."""
        self._partial_pending = {
            "rm_id": rm_id, "static_id": static_id if static_id is not None else self.running_static_id,
            "len_words": len_words, "crc32": crc32,
        }

    def run_to_completion(self, max_steps: int = 30):
        for _ in range(max_steps):
            if self.state in ("DONE", "FAILED"):
                return self.state
            self.step()
        raise TimeoutError(f"FSM did not reach DONE/FAILED within {max_steps} steps (stuck in {self.state})")


# --------------------------------------------------------------------------- #
# Driver-side helpers (extracted with the model so the fake shell and the
# integration suite share one idealized-hardware stand-in).
# --------------------------------------------------------------------------- #

def make_regs_confirmed() -> MockRegs:
    """A MockRegs whose STATUS confirms decoupled+rp_in_reset the instant
    DECOUPLE/SHUTDOWN are asserted (and clears back once released) --
    models an idealized (instant) vendor Decoupler/Shutdown-Manager IP so
    the FSM can run start-to-finish in a handful of steps."""
    regs = MockRegs()

    orig_set_bits = regs.set_bits

    def set_bits(reg, mask):
        orig_set_bits(reg, mask)
        if reg == "dfxctl_decouple" and regs.dfxctl_decouple & DECOUPLE_EN:
            regs.dfxctl_status |= DFXCTL_STATUS_DECOUPLED
        if reg == "clkrst_reset_ctrl" and (regs.clkrst_reset_ctrl & RESET_CTRL_RP_RESETN):
            # step_release() releases rp_resetn via set_bits (not write()) --
            # a set-bits-to-1 has the same electrical effect as a write that
            # sets it, so this must also confirm RP_IN_RESET clears (else
            # release_confirmed can never become true and RELEASE spins
            # forever the first time a real release_confirmed gate is hit).
            regs.dfxctl_status &= ~DFXCTL_STATUS_RP_IN_RESET

    regs.set_bits = set_bits

    orig_clr_bits = regs.clr_bits

    def clr_bits(reg, mask):
        orig_clr_bits(reg, mask)
        if reg == "clkrst_reset_ctrl" and not (regs.clkrst_reset_ctrl & RESET_CTRL_RP_RESETN):
            regs.dfxctl_status |= DFXCTL_STATUS_RP_IN_RESET
        if reg == "dfxctl_decouple" and not (regs.dfxctl_decouple & DECOUPLE_EN):
            regs.dfxctl_status &= ~DFXCTL_STATUS_DECOUPLED

    regs.clr_bits = clr_bits

    orig_write = regs.write

    def write(reg, value):
        orig_write(reg, value)
        if reg == "clkrst_reset_ctrl" and value & RESET_CTRL_RP_RESETN:
            regs.dfxctl_status &= ~DFXCTL_STATUS_RP_IN_RESET

    regs.write = write
    return regs


def greybox_clearing(len_words: int = 4) -> ClearingRef:
    """I2 boot seed: the greybox's clearing, resident from power-on
    (overlay-manifest.md; swap_fsm_init()'s g_current_rm_clearing seed)."""
    return ClearingRef(valid=True, rm_id=0, static_id=STATIC_ID, len_words=len_words, crc32=0x1111)


# --------------------------------------------------------------------------- #
# Header-validation ordering (config_agent.c's config_agent_validate_header(),
# reusing the pusher's real framing) — the "reject before any ICAP write"
# property, independent of the FSM above. The fake shell's ConfigAgentModel
# (pyverify.testing.fakeshell) is the full stateful port of config_agent.c;
# this standalone function is the minimal per-header check the integration
# suite asserts on.
# --------------------------------------------------------------------------- #

def validate_header_like_config_agent(header: BitstreamHeader, running_static_id: int):
    """Mirrors config_agent_validate_header()'s checks (magic is already
    enforced by BitstreamHeader.unpack() itself; ver/static_id/kind are
    checked here, matching the C function's order)."""
    if header.static_id != running_static_id:
        raise BitstreamFramingError(
            f"static_id mismatch: header={header.static_id:#x} running={running_static_id:#x}"
        )
    if header.kind not in (BitstreamKind.CLEARING, BitstreamKind.PARTIAL):
        raise BitstreamFramingError(f"bad kind {header.kind!r}")
