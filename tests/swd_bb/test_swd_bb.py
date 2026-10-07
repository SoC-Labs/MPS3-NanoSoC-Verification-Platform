"""tests/swd_bb/test_swd_bb.py

Verifies `fpga/shell/ip/swd_bb/swd_bb.sv` — the SWDBB pin-wiggler backing
the OpenOCD `remote_bitbang` server (shell-regmap.md v0.1 @ 0x44A7_0000,
I5). The block is deliberately dumb (the `swd_server` firmware IS the SWD
engine), so this bench is regmap-level only, per the block README's
"Bench notes for A5" — no SWD protocol modelling needed.

What this proves:
  1. Reset state: all three driven pins are 3'b000 (SWCLK low, SWDIO
     released) before any write, and DRIVE reads back 0 (README flag #3's
     chosen polarity).
  2. DRIVE write-through: every pattern 0-7 lands on the swd_clk_o /
     swd_dio_o_o / swd_dio_oe_o partition pins (combinational from the
     register — checked the moment the write response retires) and reads
     back exactly; write-data bits above [2] are ignored.
  3. SAMPLE: swd_dio_i_i reaches SAMPLE[0] through exactly a 2-FF
     synchronizer (latency pinned by watching the internal `dio_i_sync_q`
     chain edge-by-edge), upper 31 bits read 0, and writing SAMPLE is
     accepted (BRESP=OKAY) with no effect.
  4. Unmapped offsets >= 0x08 are inert: reads return 0, writes are accepted
     (BRESP=OKAY) and touch neither DRIVE nor the SWD pins. (Was RTL ambiguity
     #1 — the decode used to cover only address bit [2], so 0x08 aliased DRIVE
     for reads AND writes, meaning a stray write silently rewrote the SWD pins.
     Found by poc/systemrdl's decode-equivalence bench; RESOLVED 2026-07-09.
     The test below is now the regression guard, not a bug's documentation.)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from dut_presence import rtl_ready
from random_stim import RandomAxiMaster, random_axi_burst, seeded_rng
from regmap import (
    AxiLiteMaster, SWDBB_DRIVE, SWDBB_SAMPLE,
    SWDBB_DRIVE_SWCLK, SWDBB_DRIVE_SWDIO_O, SWDBB_DRIVE_SWDIO_OE,
)

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "swd_bb")
NO_RTL = not rtl_ready(_RTL_DIR, ["swd_bb.sv"])


def _pins(dut) -> int:
    """The three driven partition pins packed DRIVE-style: {oe, dio_o, clk}."""
    return (int(dut.swd_clk_o.value)
            | (int(dut.swd_dio_o_o.value) << 1)
            | (int(dut.swd_dio_oe_o.value) << 2))


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.swd_dio_i_i.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)
    return AxiLiteMaster.from_dut(dut)


@cocotb.test(skip=NO_RTL)
async def test_reset_state_pins_released(dut):
    """What this proves: the pre-connect drive state is 3'b000 — SWCLK
    low, SWDIO released (oe=0) — per README flag #3, and DRIVE reads back
    the same 0 (readback == driven state, before any write)."""
    axi = await _bring_up(dut)
    assert _pins(dut) == 0, "reset pin state must be 3'b000 (SWCLK low, SWDIO released)"
    val, resp = await axi.read(SWDBB_DRIVE)
    assert resp == 0
    assert val == 0, f"DRIVE must read 0 out of reset, got {val:#x}"


@cocotb.test(skip=NO_RTL)
async def test_drive_write_through_all_patterns(dut):
    """What this proves: every DRIVE pattern 0-7 is combinationally wired
    to the three partition pins (visible as soon as the write transaction
    retires — the register commits at the slv_reg_wren edge, BEFORE the
    write response handshake completes, so checking right after write()
    returns is strictly later than the commit) and reads back exactly.
    This is the whole functional contract of the block: remote_bitbang
    '0'..'7' chars become these 8 writes."""
    axi = await _bring_up(dut)
    for v in range(8):
        resp = await axi.write(SWDBB_DRIVE, v)
        assert resp == 0
        assert _pins(dut) == v, (
            f"pins must follow DRIVE={v:#05b}, got {_pins(dut):#05b}")
        # Named-bit spot checks (guards against a scrambled bit order that
        # a pins==v self-consistency check alone could miss if the pack
        # helper made the same mistake).
        assert int(dut.swd_clk_o.value) == (1 if v & SWDBB_DRIVE_SWCLK else 0)
        assert int(dut.swd_dio_o_o.value) == (1 if v & SWDBB_DRIVE_SWDIO_O else 0)
        assert int(dut.swd_dio_oe_o.value) == (1 if v & SWDBB_DRIVE_SWDIO_OE else 0)
        rb, _ = await axi.read(SWDBB_DRIVE)
        assert rb == v, f"DRIVE readback {rb:#x} != written {v:#x}"

    # Bits above [2] of the write data are ignored: only [2:0] land.
    await axi.write(SWDBB_DRIVE, 0xFFFF_FFF8 | 0x5)
    assert _pins(dut) == 0x5
    rb, _ = await axi.read(SWDBB_DRIVE)
    assert rb == 0x5, f"DRIVE must mask write data to [2:0], got {rb:#x}"


@cocotb.test(skip=NO_RTL)
async def test_sample_two_ff_latency_and_readonly(dut):
    """What this proves: swd_dio_i_i reaches SAMPLE[0] through exactly the
    2-FF synchronizer the README documents (the value appears on the
    synchronized tap after the 2nd s_axi_aclk edge, not the 1st — pinned
    on the internal dio_i_sync_q chain), SAMPLE's upper bits read 0, and
    a write to SAMPLE is accepted-no-effect (BRESP=OKAY house convention)."""
    axi = await _bring_up(dut)

    # SAMPLE low with the pin low.
    val, _ = await axi.read(SWDBB_SAMPLE)
    assert val == 0

    # Pin the 2-FF latency: change the input right after an edge, then
    # watch the synchronizer tap (bit [1] = what SAMPLE returns).
    await RisingEdge(dut.s_axi_aclk)
    dut.swd_dio_i_i.value = 1
    await RisingEdge(dut.s_axi_aclk)  # edge 1: value enters FF0
    await ReadOnly()
    assert (int(dut.dio_i_sync_q.value) >> 1) & 1 == 0, (
        "sync tap must NOT show the new value after only 1 edge (2-FF, not 1-FF)")
    await RisingEdge(dut.s_axi_aclk)  # edge 2: value reaches FF1 (the tap)
    await ReadOnly()
    assert (int(dut.dio_i_sync_q.value) >> 1) & 1 == 1, (
        "sync tap must show the new value after 2 edges")
    await RisingEdge(dut.s_axi_aclk)

    # Register-level view agrees, and the upper 31 bits are 0.
    val, _ = await axi.read(SWDBB_SAMPLE)
    assert val == 1, f"SAMPLE must read 1 with the pin high, got {val:#x}"

    dut.swd_dio_i_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 3)
    val, _ = await axi.read(SWDBB_SAMPLE)
    assert val == 0, "SAMPLE must track the pin back low (live level, no latching)"

    # A write to the RO SAMPLE offset: accepted at protocol level, changes
    # nothing (neither the sample path nor DRIVE).
    await axi.write(SWDBB_DRIVE, 0x3)
    resp = await axi.write(SWDBB_SAMPLE, 0xFFFF_FFFF)
    assert resp == 0, "write to RO SAMPLE must still get BRESP=OKAY"
    val, _ = await axi.read(SWDBB_SAMPLE)
    assert val == 0
    rb, _ = await axi.read(SWDBB_DRIVE)
    assert rb == 0x3, "a SAMPLE-offset write must not touch DRIVE"


@cocotb.test(skip=NO_RTL)
async def test_offsets_at_and_above_0x08_are_unmapped(dut):
    """What this proves: unmapped offsets in the page are inert — reads return
    0, writes are accepted (BRESP=OKAY, the contract defines no error cases)
    and have NO effect on DRIVE or the SWD pins.

    History (RTL ambiguity #1, now RESOLVED): swd_bb.sv used to decode only
    address bit [2], so 0x08 aliased DRIVE and 0x0C aliased SAMPLE — for reads
    AND writes. That meant a stray write to 0x08 silently rewrote the SWD pins.
    It was found by the SystemRDL decode-equivalence bench
    (poc/systemrdl/tb/tb_swd_bb_equiv.sv, EQUIV_RESULT.txt), which showed the
    generated full-address decode ignoring exactly the writes the hand decode
    honoured. The decode now compares the full local word address. This test
    was inverted from documenting that bug to preventing its regression."""
    axi = await _bring_up(dut)

    await axi.write(SWDBB_DRIVE, 0x6)
    dut.swd_dio_i_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 3)

    # Reads of unmapped offsets return 0 — no aliasing onto DRIVE/SAMPLE.
    for off in (0x08, 0x0C, 0x10, 0x20, 0x40):
        val, _ = await axi.read(off)
        assert val == 0x0, f"unmapped {off:#04x} must read 0, got {val:#x}"

    # Writes to unmapped offsets are accepted but inert: DRIVE and the pins
    # must be untouched. This is the regression guard for the stray-write bug.
    for off in (0x08, 0x0C, 0x10, 0x20, 0x40):
        resp = await axi.write(off, 0x1)
        assert resp == 0, f"write to {off:#04x} must return BRESP=OKAY"
        assert _pins(dut) == 0x6, (
            f"a write to unmapped {off:#04x} must NOT reach the SWD pins "
            f"(got {_pins(dut):#x}, expected 0x6)")
        rb, _ = await axi.read(SWDBB_DRIVE)
        assert rb == 0x6, f"a write to unmapped {off:#04x} must not touch DRIVE"


@cocotb.test(skip=NO_RTL)
async def test_wstrb_lane0_gates_drive_write(dut):
    """WSTRB byte-lane test (previously untested on EVERY AXI slave — the BFM
    only ever drove strb=0xF). DRIVE's decode is gated on wstrb[0]; a write
    with lane 0 disabled must be ignored (pins + register unchanged). A slave
    that ignores WSTRB (commits the whole word) fails here — and on this
    block a spurious DRIVE write would wiggle SWCLK/SWDIO mid-transaction."""
    axi = await _bring_up(dut)

    await axi.write_bytes(SWDBB_DRIVE, 0x5, 0xF)
    assert _pins(dut) == 0x5, "seed DRIVE=0x5"

    # Lane 0 disabled: ignored, pins hold.
    await axi.write_bytes(SWDBB_DRIVE, 0x2, 0x2)
    assert _pins(dut) == 0x5, "a DRIVE write with wstrb lane0 disabled must not move the pins (WSTRB ignored?)"
    rb, _ = await axi.read(SWDBB_DRIVE)
    assert rb == 0x5, "DRIVE readback must be unchanged"

    # Lane 0 enabled: lands.
    await axi.write_bytes(SWDBB_DRIVE, 0x2, 0x1)
    assert _pins(dut) == 0x2, "a lane-0-enabled DRIVE write must reach the pins"


@cocotb.test(skip=NO_RTL)
async def test_random_aw_en_corners(dut):
    """Constrained-random AXI4-Lite aw_en corner coverage (A5 random wave):
    randomized AW/W arrival order (same / AW-first / W-first), zero-gap
    back-to-back writes, read-during-write, and randomized WSTRB across
    DRIVE (rw) plus reads of SAMPLE and unmapped offsets. Fills the shared
    write-FSM condition-coverage hole; the bound AXI protocol SVA validates
    every handshake. Seed logged for reproduction."""
    axi = await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "swd_bb")
    m = RandomAxiMaster.from_dut(dut)
    waddrs = [SWDBB_DRIVE]
    raddrs = [SWDBB_DRIVE, SWDBB_SAMPLE, 0x08, 0x0C]
    tally = await random_axi_burst(m, rng, waddrs, raddrs, n=40)
    dut._log.info(f"[swd_bb random] orderings/paths exercised: {tally}")
