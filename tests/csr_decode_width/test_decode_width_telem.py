"""telem (INA228 power telemetry @ 0x44A5_0000) at the width shell_bd.tcl actually
instantiates it with (C_S_AXI_ADDR_WIDTH=32).

The same latent copy of bug #1: shipped at 32, benched only at 12, so the decode
comparing the FULL interconnect address (0x44A5_0000) against 'h0 was never
driven. On silicon that means firmware can never enable sampling nor read a
voltage/current/power word -- every read 0, every write ignored. This bench
elaborates at 32 and drives base+offset, so it fails on the un-fixed decode.

shell_bd.tcl also sets CONFIG.SIM_FAKE_DATA {0}, which equals telem.sv's RTL
default, so this bench elaborates in the shipped SEAM mode and drives the
ina228_* sample seam directly to give the read-only registers real values.

Run with:  make -C tests/csr_decode_width BLOCK=telem
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

TELEM_BASE = 0x44A50000           # shell_bd.tcl assign_bd_address for telem_0

CTRL = 0x00                       # RW: [0] enable [1] alarm_en  -- the write-readback canary
BUS_MV = 0x04                     # RO: latched bus voltage (mV)
CURR_UA = 0x08                    # RO: latched current (uA)
POWER_MW = 0x0C                   # RO: latched power (mW)
STATUS = 0x10                     # RO: [0] alarm (live) [1] i2c_err (sticky)

CTRL_ENABLE = 1 << 0
CTRL_ALARM_EN = 1 << 1
STATUS_I2C_ERR = 1 << 1


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())  # single-domain block

    dut.s_axi_aresetn.value = 0
    # INA228 sample seam (SIM_FAKE_DATA=0 ships, so these ports are live): tie to
    # a quiescent 'no engine attached' state; individual tests drive them.
    dut.ina228_sample_valid_i.value = 0
    dut.ina228_bus_mv_i.value = 0
    dut.ina228_curr_ua_i.value = 0
    dut.ina228_power_mw_i.value = 0
    dut.ina228_i2c_err_i.value = 0
    dut.alarm_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """A read at BASE+STATUS must return STATUS, not 0.

    STATUS.i2c_err is the sticky I2C-failure bit, latched straight from the
    engine seam -- it is set independently of the CTRL/enable write path, so
    this test isolates 'reads decode at the real base' with no dependence on
    writes also working. A pulse on ina228_i2c_err_i latches it to 1; a dead
    decode would still read 0.
    """
    axi = await _bring_up(dut)

    dut.ina228_i2c_err_i.value = 1        # engine reports an I2C failure...
    await ClockCycles(dut.s_axi_aclk, 3)
    dut.ina228_i2c_err_i.value = 0        # ...which STATUS.i2c_err latches (sticky)
    await ClockCycles(dut.s_axi_aclk, 2)

    at_zero, _ = await axi.read(STATUS)
    at_base, _ = await axi.read(TELEM_BASE + STATUS)

    assert at_zero == at_base, (
        "STATUS read 0x%08x at offset-from-zero but 0x%08x at the real base "
        "0x%08x. The decode is comparing the upper address bits the interconnect "
        "supplies; every TELEM register is unreachable on hardware."
        % (at_zero, at_base, TELEM_BASE)
    )
    assert at_base & STATUS_I2C_ERR, (
        "STATUS.i2c_err read back 0 at the real base after latching it -- a dead "
        "decode reads 0 for everything, which is how this bug hid."
    )


@cocotb.test(skip=NO_RTL)
async def test_sample_readings_read_at_the_real_base_address(dut):
    """The RO measurement words must be readable at BASE+offset.

    This is the representative TELEM read: enable sampling (CTRL), inject one
    atomic sample through the engine seam, and read the three latched
    measurement registers at their real base addresses. On silicon every one of
    these read 0.
    """
    axi = await _bring_up(dut)

    await axi.write(TELEM_BASE + CTRL, CTRL_ENABLE)     # sampling on

    dut.ina228_bus_mv_i.value = 0x0000_1200             # 4608 mV
    dut.ina228_curr_ua_i.value = 0x0000_03E8            # 1000 uA
    dut.ina228_power_mw_i.value = 0x0000_0042           # 66 mW
    dut.ina228_sample_valid_i.value = 1                 # one atomic-latch strobe
    await ClockCycles(dut.s_axi_aclk, 1)
    dut.ina228_sample_valid_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 2)

    mv, _ = await axi.read(TELEM_BASE + BUS_MV)
    ua, _ = await axi.read(TELEM_BASE + CURR_UA)
    mw, _ = await axi.read(TELEM_BASE + POWER_MW)

    assert mv == 0x0000_1200, "BUS_MV read 0x%08x at the real base, expected 0x1200" % mv
    assert ua == 0x0000_03E8, "CURR_UA read 0x%08x at the real base, expected 0x3E8" % ua
    assert mw == 0x0000_0042, "POWER_MW read 0x%08x at the real base, expected 0x42" % mw


@cocotb.test(skip=NO_RTL)
async def test_writes_land_and_reach_the_engine_enables(dut):
    """A write at BASE+CTRL must stick AND drive the engine enables.

    CTRL is the write-readback canary; ina228_enable_o/alarm_en_o are its
    outputs. A register that reads back but drives nothing is no better -- if
    CTRL.enable never reaches the engine, sampling never starts. (Analogous to
    dfx_ctl's decouple_en_o observation.)
    """
    axi = await _bring_up(dut)

    assert int(dut.ina228_enable_o.value) == 0
    assert int(dut.ina228_alarm_en_o.value) == 0

    await axi.write(TELEM_BASE + CTRL, CTRL_ENABLE | CTRL_ALARM_EN)
    val, _ = await axi.read(TELEM_BASE + CTRL)
    assert val == (CTRL_ENABLE | CTRL_ALARM_EN), (
        "wrote CTRL=0x3 at the real base, read back 0x%08x. Writes are being "
        "ignored because the decode never matches." % val
    )

    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.ina228_enable_o.value) == 1, "CTRL.enable set but ina228_enable_o still 0"
    assert int(dut.ina228_alarm_en_o.value) == 1, "CTRL.alarm_en set but ina228_alarm_en_o still 0"


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_ctrl(dut):
    """The fix must not regress into a too-narrow decode.

    CTRL is offset 0. Under a decode narrow enough to let BASE+0x1000 (or an
    in-page offset) wrap to offset 0, a debugger sweep would toggle
    ina228_enable_o/alarm_en_o (and clear the sticky i2c_err). This was the
    pre-fix bug (only addr[4:2] decoded, so 0x20 aliased onto CTRL). Neither may
    alias now.
    """
    axi = await _bring_up(dut)

    await axi.write(TELEM_BASE + CTRL, CTRL_ENABLE)     # known state: enable=1
    await axi.write(TELEM_BASE + 0x20, 0xFFFFFFFF)      # in-page, unmapped (old alias)
    await axi.write(TELEM_BASE + 0x1000, 0xFFFFFFFF)    # 64 KB-page width probe
    await ClockCycles(dut.s_axi_aclk, 2)

    ctrl, _ = await axi.read(TELEM_BASE + CTRL)
    assert ctrl == CTRL_ENABLE, (
        "a write to an unmapped offset aliased into CTRL (read 0x%08x): a "
        "register sweep would toggle the engine enables." % ctrl
    )
    assert int(dut.ina228_enable_o.value) == 1, "unmapped write disturbed ina228_enable_o"

    gap, _ = await axi.read(TELEM_BASE + 0x20)
    assert gap == 0, "unmapped in-page offset 0x20 must read 0, got 0x%08x" % gap
    far, _ = await axi.read(TELEM_BASE + 0x1000)
    assert far == 0, "BASE+0x1000 is unmapped and must read 0, got 0x%08x" % far
