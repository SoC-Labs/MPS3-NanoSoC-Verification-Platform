"""gen_checker (GENCHK @ 0x44A6_0000) at a WIDENED C_S_AXI_ADDR_WIDTH=32.

The fourth copy of bug #1 -- see test_decode_width_mdio_phy_model.py for the
full rationale; this block had the identical un-capped decode
(`IDX_W = C_S_AXI_ADDR_WIDTH - ADDR_LSB`, no LOCAL_ADDR_W). At 32, waddr_idx for
GENCHK's base 0x44A6_0000 is 0x1129_8000, never equal to IDX_CTRL ('h0).

FORWARD GUARD, not a ships-match check: eth_mac_test_subsystem currently
hardcodes .C_S_AXI_ADDR_WIDTH(12) with fixed [11:0] ports. Widening those ports
(the proposed 64K-page tidy-up) without the cap kills the block silently.

A dead GENCHK is particularly nasty because it fails in the *safe-looking*
direction: gen_en/chk_en read back 0, so error injection appears "off" and the
counters read 0 -- which is indistinguishable from "the link is clean". A test
campaign would report zero errors and zero frames and look like a pass.

Run with:  make -C tests/csr_decode_width BLOCK=gen_checker
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

GENCHK_BASE = 0x44A60000          # shell-regmap.md v0.2 GENCHK

CTRL = 0x00                       # rw: [0] gen_en, [1] chk_en
INJECT = 0x04                     # rw: [0] bad_fcs [1] runt [2] giant [3] ifg [4] dribble
TX_CNT = 0x08                     # ro
RX_CNT = 0x0C                     # ro
ERR_CNT = 0x10                    # ro

INJ_BAD_FCS = 1 << 0
INJ_GIANT = 1 << 2


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())

    # AXI-Stream sides held idle/ready so the generator cannot stall and the
    # checker sees no frames -- the decode is what is under test here, not the
    # datapath (tests/eth_mac_subsystem covers that).
    dut.gen_m_tready.value = 1
    dut.chk_s_tdata.value = 0
    dut.chk_s_tvalid.value = 0
    dut.chk_s_tlast.value = 0
    dut.chk_s_tuser.value = 0

    dut.s_axi_aresetn.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_writes_land_at_the_real_base_address(dut):
    """A write at BASE+INJECT must stick.

    INJECT is the cleanest R/W canary in this block: its readback is the
    still-pending fault bits, and with gen_en=0 nothing consumes them, so a
    written value must persist verbatim. On silicon, dropped writes mean the
    host can never arm a fault -- and the campaign silently tests nothing.
    """
    axi = await _bring_up(dut)

    val_in = INJ_BAD_FCS | INJ_GIANT           # 0x5
    await axi.write(GENCHK_BASE + INJECT, val_in)
    val, _ = await axi.read(GENCHK_BASE + INJECT)
    assert val == val_in, (
        "wrote 0x%x to INJECT at the real base 0x%08x, read back 0x%08x. Writes "
        "are being ignored because the decode never matches."
        % (val_in, GENCHK_BASE, val)
    )

    await axi.write(GENCHK_BASE + INJECT, 0)
    val, _ = await axi.read(GENCHK_BASE + INJECT)
    assert val == 0, "INJECT stuck at 0x%08x after writing 0" % val


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """Reads at BASE must return the same register as reads at offset-from-zero.

    Compares the two addressing modes directly: with a correct cap they are the
    same register; with a dead decode the base-relative read returns 0 while the
    offset-only read still works, which is precisely how this class of bug
    survives simulation.
    """
    axi = await _bring_up(dut)

    await axi.write(GENCHK_BASE + INJECT, INJ_BAD_FCS)

    at_base, _ = await axi.read(GENCHK_BASE + INJECT)
    at_zero, _ = await axi.read(INJECT)

    assert at_base == at_zero == INJ_BAD_FCS, (
        "INJECT read 0x%08x at the real base but 0x%08x at offset-from-zero "
        "(expected 0x%x for both). The decode is comparing the upper address "
        "bits the interconnect supplies." % (at_base, at_zero, INJ_BAD_FCS)
    )


@cocotb.test(skip=NO_RTL)
async def test_ctrl_write_reaches_the_generator_enable(dut):
    """A CTRL register that reads back but enables nothing is no better.

    gen_en must actually start the generator -- readback alone does not prove
    the write reached the datapath. Asserts the generator begins offering bytes
    on gen_m_* after gen_en is set. (Analogous to swd_bb's DRIVE->pins check and
    dfx_ctl's decouple_en_o observation.)
    """
    axi = await _bring_up(dut)

    assert int(dut.gen_m_tvalid.value) == 0, "generator ran before gen_en was set"

    await axi.write(GENCHK_BASE + CTRL, 0x1)             # gen_en
    val, _ = await axi.read(GENCHK_BASE + CTRL)
    assert val & 0x1, "CTRL.gen_en read back 0x%08x after write" % val

    # Give the generator time to start a frame and offer its first byte.
    saw_valid = False
    for _ in range(200):
        await RisingEdge(dut.s_axi_aclk)
        if int(dut.gen_m_tvalid.value):
            saw_valid = True
            break
    assert saw_valid, (
        "CTRL.gen_en reads back set but gen_m_tvalid never asserted -- the write "
        "landed in the register without reaching the generator."
    )


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_inject(dut):
    """The fix must not regress into a too-narrow decode.

    The block's README records exactly this failure once already: only addr[4:2]
    were decoded, so 0x20 aliased onto CTRL and a stray write silently toggled
    gen_en/chk_en and cleared the counters.
    """
    axi = await _bring_up(dut)

    await axi.write(GENCHK_BASE + INJECT, INJ_BAD_FCS)

    for bad in (0x20, 0x40, 0x1000, 0x2000):
        await axi.write(GENCHK_BASE + bad, 0xFFFFFFFF)

    val, _ = await axi.read(GENCHK_BASE + INJECT)
    assert val == INJ_BAD_FCS, (
        "INJECT became 0x%08x after writes to unmapped offsets -- an unmapped "
        "offset is aliasing onto it (decode too narrow). This is the 0x20->CTRL "
        "alias the README records." % val
    )
