"""tests/integration/test_swap_sequence.py

Models the DUT-swap ordering at the protocol/regmap level — no cocotb, no
DUT signals, no simulator (this is `pytest`-collectible and runs today).
It exercises a faithful **Python port of the real state machine**,
`firmware/coordinator/swap_fsm.c` + `swap_fsm_transitions.c` (states,
transitions, and register pokes taken directly from those files), plus the
header-validation ordering `firmware/config_agent/config_agent.c`'s
`config_agent_validate_header()` is specified to enforce "BEFORE any ICAP
write" (net-protocol.md's "Bitstream framing" section).

**W-FAKESHELL refactor (2026-07-04):** the model itself (`next_state`,
`MockRegs`, `ClearingRef`, `SwapFsm`, the idealized-vendor-IP regs helper,
and `validate_header_like_config_agent`) used to live in this file; it has
been extracted VERBATIM to the importable module
`host/pyverify/pyverify/testing/swap_model.py` so the fake shell server
(`pyverify.testing.fakeshell`) can drive its `swap` verb with the same
behaviour model. This file now imports it and keeps asserting exactly what
it asserted before — including implicitly cross-checking the model's
embedded register constants (the package is stdlib-only, so it can't
import cocotb-dependent `tests/common/regmap.py`) against `regmap.py`'s
transcription of shell-regmap.md v0.1: the assertions below compare the
model's register effects against `rm.*`, so a divergence in either copy
fails this suite. See swap_model.py's docstring for the full provenance
history (the I2/I25 re-port from the fixed C, the retired `_TODO`
canaries, and the relationship to `firmware/test/test_swap_fsm_hw.c`'s
C-level counterpart).

Reuses ``pyverify.pusher``'s REAL, independently-tested bitstream framing
(`frame_bitstream`/`unframe_bitstream`) rather than re-implementing it.
Both this file and the swap model resolve that framing from the single
canonical ``pyverify.pusher`` module, so the ``BitstreamKind`` /
``BitstreamFramingError`` classes used here and inside the model are the
same objects (``isinstance``/``except`` checks hold across them) — this no
longer depends on the old ``host/pusher/push.py`` path shim being imported
first.

``pyverify`` (the installed ``mps3-pyverify`` package, else the in-tree
``host/pyverify/`` fallback) and ``regmap`` (the ``tests/common`` cocotb
spine) are placed on the import path by ``tests/integration/conftest.py``
— see that file for the boundary rationale. There is deliberately no
``sys.path`` hackery left in this test module.
"""
from __future__ import annotations

import pytest

from pyverify.testing.swap_model import (
    STATES as _STATES,
    STATIC_ID,
    ClearingRef,
    MockRegs,
    SwapFsm,
    greybox_clearing as _greybox_clearing,
    make_regs_confirmed as _make_regs_confirmed,
    next_state,
    validate_header_like_config_agent,
)
from pyverify.pusher import (
    BitstreamFramingError, BitstreamKind,
    frame_bitstream, unframe_bitstream,
)
import regmap as rm


# --------------------------------------------------------------------------- #
# Transition-table tests (the pure half of the model)
# --------------------------------------------------------------------------- #

def test_next_state_is_total_over_every_known_state():
    """Sanity on `next_state()`'s own claimed totality (swap_fsm_transitions.c's
    doc comment: "every mps3_swap_state_t value has a defined next state
    for every possible `in`") -- every state in `_STATES` must produce a
    next state with the all-False default inputs, and it must be one of
    `_STATES` too (no typo'd string sneaking in as an unreachable state)."""
    for state in _STATES:
        nxt = next_state(state)
        assert nxt in _STATES, f"next_state({state!r}) returned {nxt!r}, not a known state"


def test_next_state_matches_known_c_transition_cases():
    """Cross-check this Python port against the SAME fixture cases
    `firmware/test/test_swap_fsm_transitions.c` asserts against the real
    C `swap_fsm_next_state()` (not imported -- ported by hand from reading
    that file, so a divergence here means either this port or the real C
    changed without the other being updated)."""
    assert next_state("IDLE") == "IDLE"
    assert next_state("IDLE", verify_ok=True, partial_ready=True) == "IDLE"  # irrelevant inputs
    assert next_state("GATE") == "DECOUPLE_ASSERT"
    assert next_state("DECOUPLE_ASSERT") == "DECOUPLE_ASSERT"
    assert next_state("DECOUPLE_ASSERT", decouple_confirmed=True) == "STREAM_CLEARING"
    assert next_state("STREAM_CLEARING", clearing_cache_valid=False, clearing_stream_done=True) == "FAILED", (
        "cache validity must gate BEFORE stream_done is even considered"
    )
    assert next_state("STREAM_CLEARING", clearing_cache_valid=True, clearing_stream_done=False) == "STREAM_CLEARING"
    assert next_state("STREAM_CLEARING", clearing_cache_valid=True, clearing_stream_done=True) == "AWAIT_INCOMING_CLEARING"
    assert next_state("AWAIT_INCOMING_CLEARING") == "AWAIT_INCOMING_CLEARING"
    assert next_state("AWAIT_INCOMING_CLEARING", incoming_clearing_ready=True) == "AWAIT_PARTIAL"
    assert next_state("AWAIT_PARTIAL") == "AWAIT_PARTIAL"
    assert next_state("AWAIT_PARTIAL", partial_ready=True) == "STREAM_PARTIAL"
    assert next_state("STREAM_PARTIAL") == "STREAM_PARTIAL"
    assert next_state("STREAM_PARTIAL", partial_stream_done=True) == "VERIFY"
    assert next_state("VERIFY", verify_ok=True) == "CACHE_CLEARING", "I25: real compare, both directions"
    assert next_state("VERIFY", verify_ok=False) == "FAILED", "I25: real compare, both directions"
    assert next_state("CACHE_CLEARING") == "RELEASE"
    assert next_state("RELEASE") == "RELEASE"
    assert next_state("RELEASE", release_confirmed=True) == "DONE"
    assert next_state("DONE") == "IDLE"
    assert next_state("FAILED") == "IDLE"


# --------------------------------------------------------------------------- #
# Full-FSM tests (the impure half, against the mock register file)
# --------------------------------------------------------------------------- #

def test_happy_path_transitions_through_every_state():
    """Mirrors test_swap_fsm_hw.c's test_happy_path_full_swap(): the full
    net-protocol.md 7-step sequence, I2/I25-resolved shape."""
    regs = _make_regs_confirmed()
    fsm = SwapFsm(regs, current_clearing=_greybox_clearing())
    fsm.start("nanosoc")

    fsm.step()  # GATE -> DECOUPLE_ASSERT
    assert fsm.state == "DECOUPLE_ASSERT"
    assert all(fsm.gated.values()), "must be gated immediately entering DECOUPLE_ASSERT"

    fsm.step()  # -> STREAM_CLEARING (regs confirmed instantly)
    assert fsm.state == "STREAM_CLEARING"

    fsm.step()  # cached greybox clearing "streams" in one chunk -> AWAIT_INCOMING_CLEARING
    assert fsm.state == "AWAIT_INCOMING_CLEARING"

    fsm.step()  # nothing armed yet -- stays
    assert fsm.state == "AWAIT_INCOMING_CLEARING"

    fsm.deliver_incoming_clearing(rm_id=1, len_words=8, crc32=0x2222)
    fsm.step()  # -> AWAIT_PARTIAL
    assert fsm.state == "AWAIT_PARTIAL"

    fsm.deliver_partial(rm_id=1, len_words=4)
    fsm.step()  # -> STREAM_PARTIAL
    assert fsm.state == "STREAM_PARTIAL"

    fsm.step()  # -> VERIFY
    assert fsm.state == "VERIFY"

    regs.dfxctl_rm_id = 1
    regs.dfxctl_rm_status = rm.DFXCTL_RM_STATUS_RM_ID_VALID
    fsm.step()  # I25 real compare passes -> CACHE_CLEARING
    assert fsm.state == "CACHE_CLEARING"
    assert fsm.current_rm_id == 1

    fsm.step()  # -> RELEASE; g_current_rm_clearing promoted to the incoming clearing
    assert fsm.state == "RELEASE"
    assert fsm.current_clearing.valid
    assert fsm.current_clearing.rm_id == 1
    assert fsm.current_clearing.len_words == 8
    assert fsm.current_clearing.crc32 == 0x2222
    assert fsm.gated["xvc"], "still gated entering RELEASE, before STATUS confirms"

    fsm.step()  # release_confirmed instantly true with the idealized vendor
                # IP model (_make_regs_confirmed()) -- same one-poll pattern
                # as DECOUPLE_ASSERT -> DONE, ungates
    assert fsm.state == "DONE"
    assert not any(fsm.gated.values())
    assert regs.vphy_link_event == 0, "VPHY link event must be cleared (re-link) at RELEASE->DONE, §6.3"
    assert regs.dfxctl_decouple & rm.DECOUPLE_EN == 0
    assert regs.clkrst_reset_ctrl & rm.RESET_CTRL_RP_RESETN

    fsm.step()  # -> IDLE
    assert fsm.state == "IDLE"

    assert fsm.history[:9] == [
        "GATE", "DECOUPLE_ASSERT", "STREAM_CLEARING", "AWAIT_INCOMING_CLEARING",
        "AWAIT_INCOMING_CLEARING", "AWAIT_PARTIAL", "STREAM_PARTIAL", "VERIFY", "CACHE_CLEARING",
    ]


def test_decouple_assert_blocks_until_status_confirms():
    """What this proves: the FSM does not advance past DECOUPLE_ASSERT
    until STATUS confirms BOTH decoupled and rp_in_reset. A regs stand-in
    that never confirms (a stuck/absent vendor Decoupler IP) must leave
    the FSM parked, not silently proceed."""
    regs = MockRegs()  # STATUS never confirms with this plain stand-in
    fsm = SwapFsm(regs, current_clearing=_greybox_clearing())
    fsm.start("nanosoc")
    fsm.step()  # GATE -> DECOUPLE_ASSERT
    assert fsm.state == "DECOUPLE_ASSERT"
    for _ in range(5):
        fsm.step()
        assert fsm.state == "DECOUPLE_ASSERT", "must not advance without STATUS confirmation"


def test_stream_clearing_fails_closed_on_invalid_cache():
    """Mirrors test_swap_fsm_hw.c's test_fails_closed_when_clearing_cache_
    invalid(): I2 guarantees g_current_rm_clearing is always valid post-
    boot-seed; if the greybox clearing was genuinely unavailable (blob not
    linked in), swap_fsm_init() leaves it invalid, and the FIRST swap
    attempt fails closed at STREAM_CLEARING rather than silently
    proceeding or stalling forever."""
    regs = _make_regs_confirmed()
    fsm = SwapFsm(regs, current_clearing=ClearingRef())  # invalid -- greybox lookup "failed"
    fsm.start("nanosoc")
    final = fsm.run_to_completion()
    assert final == "FAILED"
    assert fsm.history[-1] == "STREAM_CLEARING"


def test_failed_swap_leaves_decoupled_and_never_calls_release():
    """What this proves: swap_fsm.c's documented safe-failure-mode design
    choice -- on failure the RP is left decoupled + held in reset, never
    released. Verified here by checking RELEASE never appears in the
    state history and DECOUPLE/rp_resetn end up in the "held"
    configuration."""
    regs = _make_regs_confirmed()
    fsm = SwapFsm(regs, current_clearing=ClearingRef())  # forces the FAILED path
    fsm.start("nanosoc")
    fsm.run_to_completion()

    assert "RELEASE" not in fsm.history
    assert regs.dfxctl_decouple & rm.DECOUPLE_EN, "DECOUPLE must remain asserted after a failed swap"
    assert not (regs.clkrst_reset_ctrl & rm.RESET_CTRL_RP_RESETN), "rp_resetn must remain held low after a failed swap"


def _run_to_verify(fsm: SwapFsm, regs: MockRegs, *, incoming_rm_id: int, partial_rm_id: int):
    fsm.start("nanosoc")
    fsm.step()  # -> DECOUPLE_ASSERT
    fsm.step()  # -> STREAM_CLEARING
    fsm.step()  # -> AWAIT_INCOMING_CLEARING
    fsm.deliver_incoming_clearing(rm_id=incoming_rm_id, len_words=1)
    fsm.step()  # -> AWAIT_PARTIAL
    fsm.deliver_partial(rm_id=partial_rm_id, len_words=1)
    fsm.step()  # -> STREAM_PARTIAL
    fsm.step()  # -> VERIFY
    assert fsm.state == "VERIFY"


def test_verify_rejects_rm_id_mismatch():
    """I25 (was a documented gap, now fixed): mirrors test_swap_fsm_hw.c's
    test_i25_verify_mismatch_fails_and_stays_decoupled(). DFXCTL.RM_ID
    reads back a DIFFERENT id than the target (the wrong RM landed, or a
    torn/incomplete load) -- must fail closed, not silently succeed like
    the old hardcoded-true behavior this suite used to encode as a canary.
    """
    regs = _make_regs_confirmed()
    fsm = SwapFsm(regs, current_clearing=_greybox_clearing(len_words=1))
    _run_to_verify(fsm, regs, incoming_rm_id=1, partial_rm_id=1)

    regs.dfxctl_rm_id = 99          # wrong -- target is 1
    regs.dfxctl_rm_status = rm.DFXCTL_RM_STATUS_RM_ID_VALID
    fsm.step()
    assert fsm.state == "FAILED", "a mismatched rm_id must fail the swap (I25)"

    # Safe-failure-mode invariant: DECOUPLE must remain asserted, never released.
    assert regs.dfxctl_decouple & rm.DECOUPLE_EN
    assert fsm.gated["xvc"], "never ungated"
    # g_current_rm_clearing must be untouched -- SWAP_CACHE_CLEARING never ran.
    assert fsm.current_clearing.rm_id == 0


def test_verify_rejects_when_rm_id_valid_bit_clear():
    """Extra I25 coverage beyond test_swap_fsm_hw.c's own case: RM_STATUS.
    rm_id_valid=0 must fail VERIFY even if RM_ID happens to already read
    back the right numeric value (e.g. a stale register from the PREVIOUS
    RM that coincidentally matches, sampled before the new RM's bitstream
    monitor asserts valid) -- the AND, not just the numeric compare,
    matters."""
    regs = _make_regs_confirmed()
    fsm = SwapFsm(regs, current_clearing=_greybox_clearing(len_words=1))
    _run_to_verify(fsm, regs, incoming_rm_id=1, partial_rm_id=1)

    regs.dfxctl_rm_id = 1            # numerically correct...
    regs.dfxctl_rm_status = 0        # ...but rm_id_valid is NOT set
    fsm.step()
    assert fsm.state == "FAILED", "rm_id_valid=0 must fail VERIFY regardless of RM_ID's numeric value"


def test_two_consecutive_swaps_succeed_now_that_cache_clearing_is_wired_up():
    """Positive proof the I2 gap this suite used to flag (no code path ever
    cached the incoming RM's clearing, so a SECOND swap always failed
    closed at what was then AWAIT_CLEARING) is now closed: two swaps in a
    row, greybox(0) -> RM1 -> RM2, both succeed, because SWAP_CACHE_
    CLEARING really does promote each swap's staged incoming clearing into
    g_current_rm_clearing before the next swap needs it.
    """
    regs = _make_regs_confirmed()
    fsm = SwapFsm(regs, current_clearing=_greybox_clearing(len_words=1))

    # Swap 1: greybox (0) -> RM 1.
    fsm.start("nanosoc")
    fsm.step(); fsm.step(); fsm.step()  # -> AWAIT_INCOMING_CLEARING
    fsm.deliver_incoming_clearing(rm_id=1, len_words=2, crc32=0xAAAA)
    fsm.step()  # -> AWAIT_PARTIAL
    fsm.deliver_partial(rm_id=1, len_words=1)
    fsm.step(); fsm.step()  # -> VERIFY
    regs.dfxctl_rm_id = 1
    regs.dfxctl_rm_status = rm.DFXCTL_RM_STATUS_RM_ID_VALID
    fsm.step()  # -> CACHE_CLEARING
    fsm.step()  # -> RELEASE
    assert fsm.current_clearing.rm_id == 1 and fsm.current_clearing.len_words == 2
    fsm.step()  # release_confirmed instantly true (idealized vendor IP) -> DONE
    fsm.step()  # -> IDLE
    assert fsm.state == "IDLE"

    # Swap 2: RM 1 -> RM 2. Needs g_current_rm_clearing (now RM1's, cached
    # by swap 1's CACHE_CLEARING) to source STREAM_CLEARING -- this used to
    # be impossible (nothing had ever cached it).
    fsm.start("nanosoc")
    fsm.step()  # -> DECOUPLE_ASSERT
    fsm.step()  # -> STREAM_CLEARING (sources RM1's clearing, len_words=2)
    assert fsm._words_total == 2
    fsm.step()  # -> AWAIT_INCOMING_CLEARING (RM1's clearing successfully streamed)
    assert fsm.state == "AWAIT_INCOMING_CLEARING", (
        "must NOT be FAILED -- proves g_current_rm_clearing was really "
        "cached by the first swap's CACHE_CLEARING step"
    )
    fsm.deliver_incoming_clearing(rm_id=2, len_words=3)
    fsm.step()  # -> AWAIT_PARTIAL
    fsm.deliver_partial(rm_id=2, len_words=1)
    fsm.step(); fsm.step()  # -> VERIFY
    regs.dfxctl_rm_id = 2
    regs.dfxctl_rm_status = rm.DFXCTL_RM_STATUS_RM_ID_VALID
    final = fsm.run_to_completion()
    assert final == "DONE"
    assert fsm.current_rm_id == 2
    assert fsm.current_clearing.rm_id == 2  # cached again, for a hypothetical swap 3


# --------------------------------------------------------------------------- #
# Header-validation ordering (config_agent.c's config_agent_validate_header(),
# reusing A4's real push.py framing) — proves the "reject before any ICAP
# write" property independent of the FSM above.
# --------------------------------------------------------------------------- #

def test_bad_static_id_rejected_before_any_regs_touched():
    """What this proves: config_agent_validate_header()'s job -- reject a
    torn/mismatched transfer BEFORE any ICAP write -- using A4's real,
    independently-tested push.py framing rather than a re-implementation.
    """
    payload = b"\x00" * 64
    frame = frame_bitstream(payload, kind=BitstreamKind.PARTIAL, rm_slot=0,
                             static_id=0xDEADBEEF, rm_id=1)
    header, recovered_payload = unframe_bitstream(frame)  # header-level checks only
    assert recovered_payload == payload

    with pytest.raises(BitstreamFramingError, match="static_id mismatch"):
        validate_header_like_config_agent(header, running_static_id=STATIC_ID)


def test_good_header_accepted_by_config_agent_style_check():
    payload = b"\x01" * 64
    frame = frame_bitstream(payload, kind=BitstreamKind.CLEARING, rm_slot=0,
                             static_id=STATIC_ID, rm_id=0)
    header, _payload = unframe_bitstream(frame)
    validate_header_like_config_agent(header, running_static_id=STATIC_ID)  # must not raise
