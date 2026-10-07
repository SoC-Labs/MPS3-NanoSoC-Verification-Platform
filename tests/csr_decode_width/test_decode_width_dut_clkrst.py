"""dut_clkrst (CLKRST @ 0x44A0_0000) at the width shell_bd.tcl actually
instantiates it with (C_S_AXI_ADDR_WIDTH=32).

Same latent copy of bug #1 that took the platform down: shell_bd.tcl ships this
block at 32, the RTL defaults to 12, every existing bench elaborated at 12 and
drove offsets from base 0 -- so the decode comparing the FULL system address
(0x44A0_0000) against 'h0 was never exercised. On silicon that means every CLKRST
write ignored and every read 0: the DUT/RP/dbg resets can never be released and
the DUT clock never gets configured. This bench elaborates at 32 and drives
base+offset, so it fails on the un-fixed decode and passes on the fixed one.

See tests/csr_decode_width/test_csr_decode_width.py for the full post-mortem.

Run with:  make -C tests/csr_decode_width BLOCK=dut_clkrst
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

CLKRST_BASE = 0x44A00000          # shell_bd.tcl assign_bd_address for dut_clkrst_0

RESET_CTRL = 0x00                 # RW: [0] dut_resetn_req [1] rp [2] dbg (offset 0)
DUT_CLK_SEL = 0x04                # RW: [7:0] preset id  -- the write-readback canary
DUT_CLK_DRP = 0x08                # RW: [15:0]
STATUS = 0x0C                     # RO: [0] mmcm_locked [1] dut_clk_alive

STATUS_MMCM_LOCKED = 1 << 0


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    # dut_clk_i is a real second domain (heartbeat + reset synchronizers); give
    # it its own unrelated period so the CDC is genuinely asynchronous.
    cocotb.start_soon(Clock(dut.dut_clk_i, 7, units="ns").start())

    dut.s_axi_aresetn.value = 0
    # Non-AXI inputs tied to safe, known values.
    dut.ext_por_n_i.value = 1          # power-on reset deasserted
    dut.rp_resetn_gate_i.value = 1     # dfx_ctl not gating the RP reset
    dut.dbg_reset_req_i.value = 0      # no OpenOCD srst
    dut.mmcm_locked_i.value = 1        # MMCM locked -> STATUS[0]=1 (a non-zero RO)
    dut.drp_do_i.value = 0
    dut.drp_drdy_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)   # mmcm_locked_sync chain settles high

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """A read at BASE+STATUS must return STATUS, not 0.

    mmcm_locked_i is held high, so STATUS reads MMCM_LOCKED=1 -- a value that
    cannot be confused with the all-zero read of a dead decode. This is the exact
    silicon symptom: every register read 0 because the decode compared the full
    interconnect address against 'h0.
    """
    axi = await _bring_up(dut)

    at_zero, _ = await axi.read(STATUS)
    at_base, _ = await axi.read(CLKRST_BASE + STATUS)

    assert at_zero == at_base, (
        "STATUS read 0x%08x at offset-from-zero but 0x%08x at the real base "
        "0x%08x. The decode is comparing the upper address bits the interconnect "
        "supplies; every CLKRST register is unreachable on hardware."
        % (at_zero, at_base, CLKRST_BASE)
    )
    assert at_base & STATUS_MMCM_LOCKED, (
        "STATUS.MMCM_LOCKED read back 0 at the real base (0x%08x) with "
        "mmcm_locked_i driven high -- a dead decode reads 0 for everything, "
        "which is precisely how this bug hid." % at_base
    )


@cocotb.test(skip=NO_RTL)
async def test_writes_land_at_the_real_base_address(dut):
    """A write at BASE+DUT_CLK_SEL must stick.

    DUT_CLK_SEL is a clean R/W register with no side effects, so it is the
    write-readback canary. On silicon `DECOUPLE <- 1` read back 0 in dfx_ctl;
    this is the same test on the clock/reset block, where the equivalent silent
    failure is "the DUT clock preset can never be selected".
    """
    axi = await _bring_up(dut)

    await axi.write(CLKRST_BASE + DUT_CLK_SEL, 0x0000_00A5)
    val, _ = await axi.read(CLKRST_BASE + DUT_CLK_SEL)
    assert val == 0x0000_00A5, (
        "wrote 0xA5 to DUT_CLK_SEL at the real base, read back 0x%08x. Writes are "
        "being ignored because the decode never matches." % val
    )

    # ...and genuinely writable both ways, not stuck.
    await axi.write(CLKRST_BASE + DUT_CLK_SEL, 0x0000_0000)
    val, _ = await axi.read(CLKRST_BASE + DUT_CLK_SEL)
    assert val == 0, "DUT_CLK_SEL stuck at 0x%08x after writing 0" % val


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_a_mapped_register(dut):
    """The fix must not regress into a too-narrow decode.

    Offset 0 is RESET_CTRL -- the register that holds the DUT/RP/dbg reset
    requests. A decode narrow enough to let BASE+0x1000 (or an in-page unmapped
    offset) wrap to offset 0 would let a debugger sweep silently rewrite the
    reset bits. Neither an in-page unmapped offset (0x20) nor BASE+0x1000 may
    alias a mapped register.
    """
    axi = await _bring_up(dut)

    await axi.write(CLKRST_BASE + 0x20, 0xFFFFFFFF)     # in-page, unmapped
    await axi.write(CLKRST_BASE + 0x1000, 0xFFFFFFFF)   # 64 KB-page width probe
    await ClockCycles(dut.s_axi_aclk, 2)

    rc, _ = await axi.read(CLKRST_BASE + RESET_CTRL)
    assert (rc & 0x7) == 0, (
        "a write to an unmapped offset aliased into RESET_CTRL (read 0x%08x): a "
        "register sweep would rewrite the DUT/RP/dbg reset requests." % rc
    )

    gap, _ = await axi.read(CLKRST_BASE + 0x20)
    assert gap == 0, "unmapped in-page offset 0x20 must read 0, got 0x%08x" % gap
    far, _ = await axi.read(CLKRST_BASE + 0x1000)
    assert far == 0, "BASE+0x1000 is unmapped and must read 0, got 0x%08x" % far
