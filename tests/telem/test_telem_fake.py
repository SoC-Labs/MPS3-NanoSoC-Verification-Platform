"""tests/telem/test_telem_fake.py — FAKE MODE (`make FAKE=1`:
SIM_FAKE_DATA=1, FAKE_PERIOD=16, both set by VCS `-pvalue+` parameter
override — see this directory's Makefile for the mechanics and for why
seam mode is the default bench instead).

Verifies telem.sv's SIM_FAKE_DATA self-stimulus generator against the
formulas its README documents as deterministic and bench-checkable:

    BUS_MV   = 1200
    CURR_UA  = 1000 + 10 * sample_index
    POWER_MW = CURR_UA >> 10        (placeholder math, NOT physics)

plus: samples only strobe while CTRL.enable=1, readings freeze on disable,
the index keeps advancing across enable cycles (the period counter resets
on disable; the index register does not), and fake mode never raises
STATUS.i2c_err (there is no I2C to fail).

Anti-tearing note: FAKE_PERIOD=16 is shorter than three back-to-back AXI
reads, so a new sample could land between the CURR_UA and POWER_MW reads
of one checking pass. Every formula check below therefore DISABLES
sampling first (disable holds the readings — proven in seam mode) and
reads a frozen, self-consistent triple.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from dut_presence import rtl_ready
from regmap import (
    AxiLiteMaster,
    TELEM_CTRL, TELEM_BUS_MV, TELEM_CURR_UA, TELEM_POWER_MW, TELEM_STATUS,
    TELEM_CTRL_ENABLE, TELEM_STATUS_I2C_ERR,
)

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "telem")
NO_RTL = not rtl_ready(_RTL_DIR, ["telem.sv"])

FAKE_PERIOD = 16  # must match the Makefile's -pvalue+telem/FAKE_PERIOD


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    # Seam inputs are dead logic in fake mode (constant-parameter muxing),
    # but init them anyway so nothing in the build is X-driven.
    dut.ina228_sample_valid_i.value = 0
    dut.ina228_bus_mv_i.value = 0
    dut.ina228_curr_ua_i.value = 0
    dut.ina228_power_mw_i.value = 0
    dut.ina228_i2c_err_i.value = 0
    dut.alarm_i.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)
    return AxiLiteMaster.from_dut(dut)


async def _frozen_triple(dut, axi):
    """Disable sampling, then read a tear-free triple (see module note)."""
    await axi.write(TELEM_CTRL, 0)
    await ClockCycles(dut.s_axi_aclk, 2)
    bus, _ = await axi.read(TELEM_BUS_MV)
    curr, _ = await axi.read(TELEM_CURR_UA)
    powr, _ = await axi.read(TELEM_POWER_MW)
    return bus, curr, powr


@cocotb.test(skip=NO_RTL)
async def test_fake_generator_formulas_and_no_i2c_err(dut):
    """What this proves: the documented fake-mode formulas hold on a real
    sample (BUS_MV=1200, CURR_UA=1000+10k with k>=1, POWER_MW=CURR_UA>>10),
    and fake mode never raises STATUS.i2c_err."""
    axi = await _bring_up(dut)

    # Parameter really did override (guards against a silently ignored
    # -pvalue: with SIM_FAKE_DATA=0 this whole bench would vacuously pass
    # its "no samples while disabled" checks).
    assert int(dut.SIM_FAKE_DATA.value) == 1, "-pvalue+telem/SIM_FAKE_DATA=1 did not take"
    assert int(dut.FAKE_PERIOD.value) == FAKE_PERIOD

    # Nothing arrives while disabled (reset state).
    await ClockCycles(dut.s_axi_aclk, 3 * FAKE_PERIOD)
    bus, _ = await axi.read(TELEM_BUS_MV)
    assert bus == 0, "no fake samples may land while CTRL.enable=0"

    # Enable, let a few samples land, freeze, check the formulas.
    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)
    await ClockCycles(dut.s_axi_aclk, 4 * FAKE_PERIOD)
    bus, curr, powr = await _frozen_triple(dut, axi)

    assert bus == 1200, f"BUS_MV must be the documented 1200, got {bus}"
    assert curr >= 1010 and (curr - 1000) % 10 == 0, (
        f"CURR_UA must be 1000 + 10*k (k>=1), got {curr}")
    assert powr == curr >> 10, (
        f"POWER_MW must be CURR_UA>>10 ({curr >> 10}), got {powr}")

    status, _ = await axi.read(TELEM_STATUS)
    assert not (status & TELEM_STATUS_I2C_ERR), "fake mode must never raise i2c_err"


@cocotb.test(skip=NO_RTL)
async def test_fake_index_advances_and_freezes_on_disable(dut):
    """What this proves: the sample index keeps ramping while enabled
    (each re-enable resumes from where it left off — the index register
    survives disable; only reset zeroes it) and readings stay frozen while
    disabled."""
    axi = await _bring_up(dut)

    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)
    await ClockCycles(dut.s_axi_aclk, 4 * FAKE_PERIOD)
    _, curr1, _ = await _frozen_triple(dut, axi)
    assert curr1 >= 1010

    # Frozen while disabled: two spaced reads agree.
    await ClockCycles(dut.s_axi_aclk, 3 * FAKE_PERIOD)
    again, _ = await axi.read(TELEM_CURR_UA)
    assert again == curr1, "readings must stay frozen while disabled"

    # Re-enable: the ramp continues past the earlier value.
    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)
    await ClockCycles(dut.s_axi_aclk, 4 * FAKE_PERIOD)
    _, curr2, _ = await _frozen_triple(dut, axi)
    assert curr2 > curr1, (
        f"sample index must keep advancing across enable cycles "
        f"({curr2} !> {curr1})")
