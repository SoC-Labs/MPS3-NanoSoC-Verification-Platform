"""clcd (QVGA HX8347-D 8080 bus master @ 0x44AC_0000) at the width shell_bd.tcl
actually instantiates it with (C_S_AXI_ADDR_WIDTH=32).

Same latent bug as every other shell CSR block: the RTL default is 12 but the BD
instantiates 32, so the interconnect hands the slave the FULL system address
(0x44AC_0000, not 0x0). A decode of addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] compares
the full address against 'h0 and never matches -- on silicon every CLCD register
would be unreachable (STATUS reads 0, CMD/DATA/CTRL/TIMING writes ignored, the
panel never initialises). clcd.sv decodes its own 64 KiB page
(LOCAL_ADDR_W=min(width,16)); this bench elaborates at 32 and drives base+offset,
so it fails on the un-fixed decode and passes on the fixed one.

It also pins the property the widened decode must NOT regress: BASE+0x1000 (and
in-page unmapped offsets) must NOT alias CTRL @ offset 0. If they did, a CSR
page-dump over SWD/XVC could toggle the backlight/panel-reset pads.

Reading every offset here is SAFE by contract (v0.4): READ @ 0x10 has no read
side effect (a panel read-back is armed by CTRL.read_start), and this build is
READ_PATH=0 anyway, so no offset in the page can launch an 8080 cycle.

Run with:  make -C tests/csr_decode_width BLOCK=clcd
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

CLCD_BASE = 0x44AC0000            # shell_bd.tcl assign_bd_address for clcd_0

CTRL = 0x00                       # RW: [1] backlight [2] reset_n (-> pads)
STATUS = 0x0C                     # RO: [1] fifo_empty set at reset
TIMING = 0x14                     # RW: [7:0] wr_lo [15:8] wr_hi [23:16] cs_setup

CTRL_BACKLIGHT = 1 << 1
CTRL_RESET_N = 1 << 2
STATUS_FIFO_EMPTY = 1 << 1


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())

    dut.s_axi_aresetn.value = 0
    # READ_PATH default 0 ships write-only: tie the read data bus quiescent.
    if hasattr(dut, "clcd_pd_i"):
        dut.clcd_pd_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """A read at BASE+STATUS must return STATUS, not 0.

    STATUS.fifo_empty is set at reset (the FIFO is empty), independent of any
    write path -- so this isolates 'reads decode at the real base'. A dead decode
    (full-address compare against 'h0) reads 0 for everything, which is how the
    silicon bug hid."""
    axi = await _bring_up(dut)

    at_zero, _ = await axi.read(STATUS)
    at_base, _ = await axi.read(CLCD_BASE + STATUS)

    assert at_zero == at_base, (
        "STATUS read 0x%08x at offset-from-zero but 0x%08x at the real base "
        "0x%08x. The decode compares the upper address bits the interconnect "
        "supplies; every CLCD register is unreachable on hardware."
        % (at_zero, at_base, CLCD_BASE)
    )
    assert at_base & STATUS_FIFO_EMPTY, (
        "STATUS.fifo_empty read back 0 at the real base -- a dead decode reads 0 "
        "for everything, which is precisely how this bug hid."
    )


@cocotb.test(skip=NO_RTL)
async def test_ctrl_writes_land_and_reach_the_pads(dut):
    """A write at BASE+CTRL must stick AND drive the panel pads.

    CTRL is the write-readback canary; clcd_bl_o/clcd_rst_n_o are its outputs. A
    register that reads back but drives nothing is no better -- if CTRL never
    reaches the pads, the backlight/reset never move (analogous to dfx_ctl's
    decouple_en_o / telem's ina228_enable_o observation)."""
    axi = await _bring_up(dut)

    assert int(dut.clcd_bl_o.value) == 0
    assert int(dut.clcd_rst_n_o.value) == 0     # reset value: panel held in reset

    await axi.write(CLCD_BASE + CTRL, CTRL_BACKLIGHT | CTRL_RESET_N)
    val, _ = await axi.read(CLCD_BASE + CTRL)
    assert val == (CTRL_BACKLIGHT | CTRL_RESET_N), (
        "wrote CTRL=0x%x at the real base, read back 0x%08x. Writes are being "
        "ignored because the decode never matches."
        % (CTRL_BACKLIGHT | CTRL_RESET_N, val)
    )

    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.clcd_bl_o.value) == 1, "CTRL.backlight set but clcd_bl_o still 0"
    assert int(dut.clcd_rst_n_o.value) == 1, "CTRL.reset_n set but clcd_rst_n_o still 0"


@cocotb.test(skip=NO_RTL)
async def test_timing_is_writable_at_the_real_base_address(dut):
    """TIMING is a plain RW register -- a second write-readback canary at a
    non-zero offset, so the proof isn't resting on CTRL (offset 0) alone."""
    axi = await _bring_up(dut)

    await axi.write(CLCD_BASE + TIMING, 0x0007_0B0C)   # cs_setup=7 wr_hi=0x0B wr_lo=0x0C
    val, _ = await axi.read(CLCD_BASE + TIMING)
    assert val == 0x0007_0B0C, (
        "wrote TIMING=0x00070B0C at the real base, read back 0x%08x. Writes are "
        "being ignored because the decode never matches." % val
    )


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_ctrl(dut):
    """The fix must not regress into a too-narrow decode.

    CTRL is offset 0. Under a decode narrow enough to let BASE+0x1000 (or an
    in-page unmapped offset) wrap to offset 0, a CSR page-dump would toggle the
    backlight/panel-reset pads. Neither may alias now."""
    axi = await _bring_up(dut)

    await axi.write(CLCD_BASE + CTRL, CTRL_RESET_N)      # known state: reset_n=1, backlight=0
    await axi.write(CLCD_BASE + 0x20, 0xFFFFFFFF)        # in-page, unmapped
    await axi.write(CLCD_BASE + 0x1000, 0xFFFFFFFF)      # 64 KiB-page width probe
    await ClockCycles(dut.s_axi_aclk, 2)

    ctrl, _ = await axi.read(CLCD_BASE + CTRL)
    assert ctrl == CTRL_RESET_N, (
        "a write to an unmapped offset aliased into CTRL (read 0x%08x): a "
        "register sweep would toggle the panel pads." % ctrl
    )
    assert int(dut.clcd_bl_o.value) == 0, "unmapped write disturbed clcd_bl_o (backlight)"
    assert int(dut.clcd_rst_n_o.value) == 1, "unmapped write disturbed clcd_rst_n_o"

    gap, _ = await axi.read(CLCD_BASE + 0x20)
    assert gap == 0, "unmapped in-page offset 0x20 must read 0, got 0x%08x" % gap
    far, _ = await axi.read(CLCD_BASE + 0x1000)
    assert far == 0, "BASE+0x1000 is unmapped and must read 0, got 0x%08x" % far
