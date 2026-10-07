"""test_tunnel_checker.py -- THE CONTROL for tests/clcd_demo.

Board-free, simulator-free, licence-free: it runs under `pytest tests` (root
`make check-ci` stage 3) on any machine.

A bench that has only ever been seen to PASS is not evidence. Every rule the
cocotb bench leans on -- the section-5 timing floor, "PD/RS may not move under
CS", "never release CS mid-strobe", "RESERVED bits are 0", and the decoded
command/data sequence itself -- is checked here against a DELIBERATELY BROKEN
trace, and each one must raise. The mutations are the ones that actually happen:

  * a setup/hold below the tunnel's floor -- what you get by taking clcd_core's
    SHELL defaults (2/4/4 cycles, tuned for the 100 MHz shell clock) into a
    50 MHz RM without reading docs/contracts/dut-display-tunnel.md section 5;
  * a SWAPPED command/data line -- the single most likely wiring error in a bit
    map where RS sits next to WR in the same byte, and one that produces a
    perfectly well-formed bus that writes every register index into the
    previously-selected register.

Both are invisible to "did the RM emit 153,600 bytes?".
"""
from __future__ import annotations

import pathlib
import sys
from typing import List, Sequence, Tuple

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from tunnel_model import (  # noqa: E402
    BIT_CS, BIT_RS, BIT_SPARE, BIT_WR, PD_LO, RS_CMD, RS_DATA, Sample,
    TunnelChecker, TunnelViolation, split,
)
import card_model  # noqa: E402


# --------------------------------------------------------------------------
# A synthetic tunnel trace generator -- the same shape clcd_core produces:
# CS asserted for the whole byte, WR pulsed inside it, CS released between
# bytes. Timings are settable so a control can drive them below the floor.
# --------------------------------------------------------------------------
def trace(seq: Sequence[Tuple[int, int]], cs_setup: int = 8, wr: int = 8,
          hold: int = 8, idle: int = 3, reserved: int = 0) -> List[Sample]:
    out: List[Sample] = []
    idle_s = Sample(cs=0, wr=0, rs=0, pd=0, busy=0, req=1, reserved=reserved)
    out += [idle_s] * idle
    for rs, byte in seq:
        s = dict(cs=1, rs=rs, pd=byte, busy=1, req=1, reserved=reserved)
        out += [Sample(wr=0, **s)] * cs_setup
        out += [Sample(wr=1, **s)] * wr
        out += [Sample(wr=0, **s)] * hold
        out += [Sample(cs=0, wr=0, rs=rs, pd=byte, busy=0, req=1,
                       reserved=reserved)] * idle
    return out


SHORT = [(RS_CMD, 0x22), (RS_DATA, 0xAB), (RS_DATA, 0xCD)]


def test_a_well_formed_trace_decodes_exactly():
    ck = TunnelChecker()
    ck.feed(trace(SHORT))
    assert ck.sequence == SHORT
    assert all(s.setup_cycles >= 8 and s.wr_cycles >= 8 and s.hold_cycles >= 8
               for s in ck.strobes)


# ---- CONTROL 1: timing below the contract floor ---------------------------
@pytest.mark.parametrize("kw,word", [
    (dict(cs_setup=2), "setup"),
    (dict(wr=4), "WR was asserted"),
    (dict(hold=2), "hold"),
])
def test_sub_floor_timing_is_rejected(kw, word):
    """clcd_core's SHELL defaults are CS_SETUP=2 / WR_LO=WR_HI=4 -- correct at
    100 MHz, below the tunnel's floor at 50 MHz. Taking them across unchanged
    is the mistake this rule exists to catch."""
    ck = TunnelChecker()
    with pytest.raises(TunnelViolation) as e:
        ck.feed(trace(SHORT, **kw))
    assert word in str(e.value)
    assert "dut-display-tunnel.md" in str(e.value)


def test_the_floor_is_the_thing_being_checked_not_the_stimulus():
    """Same sub-floor stimulus, a checker told to accept it -> no violation.
    Proves the failures above come from the RULE, not from a broken generator."""
    ck = TunnelChecker(phase_floor=1, guard_floor=1)
    ck.feed(trace(SHORT, cs_setup=2, wr=4, hold=2))
    assert ck.sequence == SHORT


# ---- CONTROL 2: a swapped command/data line -------------------------------
def test_swapped_command_data_line_changes_every_byte_type():
    """RS carries "is this a register INDEX or its DATUM". Invert it and the
    bus stays perfectly well-formed -- correct strobes, correct timing, correct
    bytes -- while the panel writes every index into the previously selected
    register. Only a sequence comparison catches it."""
    table, defines = card_model.load_firmware_table()
    want = card_model.init_sequence(table, defines)

    good = TunnelChecker()
    good.feed(trace(want))
    assert good.sequence == want

    swapped = [Sample(cs=s.cs, wr=s.wr, rs=s.rs ^ 1, pd=s.pd, busy=s.busy,
                      req=s.req, reserved=s.reserved) for s in trace(want)]
    bad = TunnelChecker()
    bad.feed(swapped)                     # still a LEGAL bus -- that is the trap
    assert bad.sequence != want
    assert card_model.diff_sequences(bad.sequence, want) != "  (sequences agree)"


def test_swapped_line_is_invisible_to_a_byte_count():
    """The check that would NOT have caught it, stated explicitly."""
    table, defines = card_model.load_firmware_table()
    want = card_model.init_sequence(table, defines)
    swapped = [Sample(cs=s.cs, wr=s.wr, rs=s.rs ^ 1, pd=s.pd, busy=s.busy,
                      req=s.req, reserved=s.reserved) for s in trace(want)]
    bad = TunnelChecker()
    bad.feed(swapped)
    assert len(bad.strobes) == len(want)          # same byte count
    assert [b for _, b in bad.sequence] == [b for _, b in want]  # same bytes


# ---- the rest of the rules, each with its own broken trace ----------------
def test_reserved_bit_set_is_rejected():
    ck = TunnelChecker()
    with pytest.raises(TunnelViolation, match="RESERVED"):
        ck.feed(trace(SHORT, reserved=0b100))


def test_pd_moving_under_cs_is_rejected():
    t = trace(SHORT)
    n = next(i for i, s in enumerate(t) if s.cs and s.wr)
    t[n] = t[n]._replace(pd=(t[n].pd ^ 0xFF))
    with pytest.raises(TunnelViolation, match="PD/RS changed"):
        TunnelChecker().feed(t)


def test_cs_released_mid_strobe_is_rejected():
    t = trace(SHORT)
    n = next(i for i, s in enumerate(t) if s.cs and s.wr)
    t[n + 1] = t[n + 1]._replace(cs=0)
    with pytest.raises(TunnelViolation, match="truncated"):
        TunnelChecker().feed(t)


def test_wr_without_cs_is_rejected():
    t = trace(SHORT)
    t.insert(1, Sample(cs=0, wr=1, rs=0, pd=0, busy=0, req=0, reserved=0))
    with pytest.raises(TunnelViolation, match="WR asserted with CS deasserted"):
        TunnelChecker().feed(t)


# ---- the bit map itself ---------------------------------------------------
def test_split_matches_the_frozen_bit_map():
    """docs/contracts/dut-display-tunnel.md section 2, restated as a test so a
    future edit to `split()` has to disagree with something."""
    o = 0xA5 << PD_LO
    oe = (1 << BIT_CS) | (1 << BIT_WR) | (1 << BIT_RS)
    s = split(o, oe)
    assert (s.pd, s.cs, s.wr, s.rs, s.reserved) == (0xA5, 1, 1, 1, 0)
    assert split(0, 1 << BIT_SPARE).reserved != 0
