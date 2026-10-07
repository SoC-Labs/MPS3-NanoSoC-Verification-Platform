"""tests/board_gpio/test_board_gpio.py

Forward-looking cocotb bench skeleton for `fpga/shell/ip/board_gpio/
board_gpio.sv` — the GPIO passthrough OWN-mux block new in
`docs/contracts/shell-regmap.md` **v0.1** (I4, `0x44AA_0000`). See
`dut_notes.md` in this directory for the full port-list rationale and the
important caveat this bench is written against: **no RTL for this block
exists yet** (unlike every other per-block bench in this suite, which all
bind against at least a real Phase-0 stub). `tests/common/
dut_presence.rtl_ready()` returns `False` for a missing file exactly like
it does for a present-but-still-stubbed one, so `NO_RTL` below is `True`
today and every test is permanently skipped — never a hang, per
`dut_presence.py`'s own docstring on why "file exists" isn't the gate.

The pure-logic OWN-mux truth table this bench is expected to reproduce
once real RTL lands is independently derived (not imported from here, or
from anywhere) in `tests/board_gpio/test_gpio_mux_logic.py` — run that one
today; this file documents the *hardware* shape the same logic must
eventually be proven against.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from dut_presence import rtl_ready
from random_stim import RandomAxiMaster, random_axi_burst, seeded_rng
from regmap import AxiLiteMaster, GPIO_IN, GPIO_OUT, GPIO_OE, GPIO_OWN, GPIO_NGPIO_DEFAULT

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "shell", "ip", "board_gpio")
NO_RTL = not rtl_ready(_RTL_DIR, ["board_gpio.sv"])

NGPIO = GPIO_NGPIO_DEFAULT


async def _bring_up(dut):
    """Common setup: clock s_axi_aclk, release s_axi_aresetn, park the RP
    side and the physical-pad side quiescent, return a bound AxiLiteMaster.
    See dut_notes.md's "Assumed port list" for the RP-facing/pad-facing
    signal names this presumes -- update alongside the real RTL.
    """
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.dut_gpio_o_i.value = 0
    dut.dut_gpio_oe_i.value = 0
    dut.board_pad_i.value = 0
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)
    return AxiLiteMaster.from_dut(dut)


@cocotb.test(skip=NO_RTL)
async def test_own_resets_to_dut_owns_every_bit(dut):
    """What this proves: shell-regmap.md's "0 = DUT owns the bit
    (default)" is the RESET value, not just a documentation convention --
    a host that reads OWN immediately after reset must see 0 across the
    whole bus (dut_notes.md's ambiguity note -- confirm this is really
    what A1 built once the RTL lands)."""
    axi = await _bring_up(dut)
    own, _resp = await axi.read(GPIO_OWN)
    assert own == 0, "OWN must reset to 0 (DUT owns every bit by default)"


@cocotb.test(skip=NO_RTL)
async def test_default_own_dut_drives_the_pad_host_out_ignored(dut):
    """Default (OWN=0): the DUT's dut_gpio_o/dut_gpio_oe reach the board
    pad; host OUT/OE (even if staged) have no effect -- the hardware
    counterpart of tests/board_gpio/test_gpio_mux_logic.py's
    test_default_own_all_zero_gives_dut_full_control()."""
    axi = await _bring_up(dut)
    await axi.write(GPIO_OUT, 0xFFFF)
    await axi.write(GPIO_OE, 0xFFFF)  # host garbage staged, but OWN still 0

    dut.dut_gpio_o_i.value = 0xBEEF
    dut.dut_gpio_oe_i.value = 0xFFFF
    await RisingEdge(dut.s_axi_aclk)

    assert int(dut.board_pad_o.value) == 0xBEEF
    assert int(dut.board_pad_oe.value) == 0xFFFF


@cocotb.test(skip=NO_RTL)
async def test_own_override_per_bit_hands_that_bit_to_host(dut):
    """OWN=1 on a subset of bits: those bits are driven by host OUT/OE;
    the remaining bits stay DUT-owned -- the hardware counterpart of
    test_gpio_mux_logic.py's test_mixed_own_mask_selects_strictly_per_bit()."""
    axi = await _bring_up(dut)
    await axi.write(GPIO_OWN, 0x000F)   # host owns bits [3:0]
    await axi.write(GPIO_OUT, 0x0005)
    await axi.write(GPIO_OE, 0x0003)

    dut.dut_gpio_o_i.value = 0b1010_1010_1010_1010
    dut.dut_gpio_oe_i.value = 0b1111_1111_0000_0000
    await RisingEdge(dut.s_axi_aclk)

    expect_drive = (0x0005 & 0x000F) | (0b1010_1010_1010_1010 & ~0x000F & 0xFFFF)
    expect_oe = (0x0003 & 0x000F) | (0b1111_1111_0000_0000 & ~0x000F & 0xFFFF)
    assert int(dut.board_pad_o.value) == expect_drive
    assert int(dut.board_pad_oe.value) == expect_oe


@cocotb.test(skip=NO_RTL)
async def test_gpio_in_and_dut_gpio_i_both_sample_the_resolved_pad(dut):
    """GPIO.IN (host-readable) and dut_gpio_i (RP-facing) must agree --
    partition-pins.md: the shell brokers one physical net to both sides,
    so neither reader can tell who (if anyone) is driving it."""
    axi = await _bring_up(dut)
    dut.board_pad_i.value = 0x1234
    await RisingEdge(dut.s_axi_aclk)

    gpio_in, _resp = await axi.read(GPIO_IN)
    dut_gpio_i = int(dut.dut_gpio_i_o.value)
    assert gpio_in == dut_gpio_i == 0x1234


@cocotb.test(skip=NO_RTL)
async def test_random_aw_en_corners(dut):
    """Constrained-random AXI4-Lite aw_en corner coverage (A5 random wave):
    randomized AW/W arrival order (same / AW-first / W-first), zero-gap
    back-to-back writes, read-during-write, and randomized WSTRB across the
    OUT/OE/OWN rw registers plus reads of IN. Fills the shared write-FSM
    condition-coverage hole; the bound AXI protocol SVA validates every
    handshake. Seed logged for reproduction."""
    await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "board_gpio")
    m = RandomAxiMaster.from_dut(dut)
    waddrs = [GPIO_OUT, GPIO_OE, GPIO_OWN]
    raddrs = [GPIO_IN, GPIO_OUT, GPIO_OE, GPIO_OWN]
    tally = await random_axi_burst(m, rng, waddrs, raddrs, n=48)
    dut._log.info(f"[board_gpio random] orderings/paths exercised: {tally}")


@cocotb.test(skip=NO_RTL)
async def test_random_wide_bus_toggle(dut):
    """Toggle-coverage attack on the wide NGPIO buses (A5 random wave).

    Baseline toggle coverage was ~41% here: the fixed hand-picked vectors
    drove only a handful of the 16 bits of OUT/OE/OWN and the DUT-side /
    pad-side buses. This drives fully-random NGPIO-wide values into OWN,
    OUT, OE (via AXI), dut_gpio_o_i, dut_gpio_oe_i, and board_pad_i over
    many iterations so every bit of every wide bus toggles both ways, and
    re-derives the OWN-mux truth independently on each iteration:
        board_pad_o  = (own & out) | (~own & dut_o)
        board_pad_oe = (own & oe)  | (~own & dut_oe)
    plus IN / dut_gpio_i_o both equal the (2-FF synchronized) pad. A real
    mux/decode/synchronizer bug would fail one of these. Seed logged."""
    axi = await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "board_gpio")
    mask = (1 << NGPIO) - 1
    for i in range(40):
        own = rng.getrandbits(NGPIO)
        out = rng.getrandbits(NGPIO)
        oe = rng.getrandbits(NGPIO)
        # Full-word writes so out_q/oe_q/own_q take the full value (WSTRB
        # partial-lane behaviour is covered by test_random_aw_en_corners).
        await axi.write(GPIO_OWN, own)
        await axi.write(GPIO_OUT, out)
        await axi.write(GPIO_OE, oe)

        duto = rng.getrandbits(NGPIO)
        dutoe = rng.getrandbits(NGPIO)
        pad = rng.getrandbits(NGPIO)
        dut.dut_gpio_o_i.value = duto
        dut.dut_gpio_oe_i.value = dutoe
        dut.board_pad_i.value = pad
        # 3 cycles: register commit is done; the 2-FF pad synchronizer has
        # seen `pad` stable for >= 2 edges so IN / dut_gpio_i_o track it.
        await ClockCycles(dut.s_axi_aclk, 3)

        exp_o = ((own & out) | (~own & duto)) & mask
        exp_oe = ((own & oe) | (~own & dutoe)) & mask
        assert int(dut.board_pad_o.value) == exp_o, (
            f"iter {i}: board_pad_o mux mismatch "
            f"own={own:#06x} out={out:#06x} duto={duto:#06x} "
            f"got={int(dut.board_pad_o.value):#06x} exp={exp_o:#06x}")
        assert int(dut.board_pad_oe.value) == exp_oe, (
            f"iter {i}: board_pad_oe mux mismatch")
        assert int(dut.dut_gpio_i_o.value) == pad, (
            f"iter {i}: dut_gpio_i_o must equal synchronized pad {pad:#06x}")

        gpio_in, _ = await axi.read(GPIO_IN)
        assert gpio_in == pad, (
            f"iter {i}: GPIO.IN {gpio_in:#06x} must equal synchronized pad {pad:#06x}")
