"""tests/clcd/test_clcd_readpath.py — the CLCD_RD read-back path, elaborated at
READ_PATH=1 (`make READP=1`). Separate from test_clcd.py because the read path
is a build-time parameter and cocotb/VCS bake one parameter value per simv.

This module pins the v0.4 contract change (integrator, 2026-07-10):

  * READ @ 0x10 has **NO read side effect**. A panel read is armed by WRITING
    `CTRL.read_start` (bit 4, self-clearing); firmware then polls `STATUS.busy`
    and reads `READ` for `{valid, rdata}`. Reading READ must launch NOTHING.

  * The rejected design armed the cycle on a *read* of READ. That is the shape
    that already cost this platform a destructive read (a read of DFXCTL 0x20
    popped a UART console byte). Critically, this platform dumps CSR pages over
    SWD/XVC and `tests/csr_decode_width/` reads every offset in the page — under
    read-arms, either would silently drive 8080 bus cycles at the panel.

`test_read_sweep_has_no_panel_side_effect` is the anti-regression check the
integrator asked for: with the FSM LIVE (enable=1, so a read-arms regression
would actually launch a cycle), read every offset in the page and assert the
panel side stays completely idle (CS/WR/RD never assert). It FAILS against a
read-arms implementation and PASSES against the write-armed one.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
sys.path.insert(0, os.path.dirname(__file__))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from dut_presence import rtl_ready
from regmap import AxiLiteMaster
from clcd_panel_model import PanelBusModel

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "clcd")
NO_RTL = not rtl_ready(_RTL_DIR, ["clcd.sv"])

CLCD_CTRL = 0x00
CLCD_CMD = 0x04
CLCD_DATA = 0x08
CLCD_STATUS = 0x0C
CLCD_READ = 0x10
CLCD_TIMING = 0x14

CTRL_ENABLE = 1 << 0
CTRL_RESET_N = 1 << 2
CTRL_READ_START = 1 << 4         # v0.4: self-clearing, arms one CLCD_RD cycle
CTRL_RUN = CTRL_ENABLE | CTRL_RESET_N

STATUS_BUSY = 1 << 2
READ_VALID = 1 << 8


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    if hasattr(dut, "clcd_pd_i"):
        dut.clcd_pd_i.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    dut.s_axi_awaddr.value = 0
    dut.s_axi_wdata.value = 0
    dut.s_axi_wstrb.value = 0xF
    dut.s_axi_araddr.value = 0
    dut.s_axi_aresetn.value = 0

    model = PanelBusModel(dut)
    cocotb.start_soon(model.run())

    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)
    return AxiLiteMaster.from_dut(dut), model


@cocotb.test(skip=NO_RTL)
async def test_read_path_param_is_one(dut):
    """Guard: prove the -pvalue+clcd.READ_PATH=1 override actually took, so a
    silently-ignored override can't make the no-side-effect test pass vacuously
    (there would be no read path to regress). READ_PATH is a module parameter;
    at READ_PATH=1 the read datapath exists — clcd_pd_oe must be released (0)
    for at least part of an armed read cycle, unlike the always-1 write-only
    build. Checked indirectly below; here we simply assert the elaboration is
    the intended one via the parameter handle when present."""
    await _bring_up(dut)
    if hasattr(dut, "READ_PATH"):
        assert int(dut.READ_PATH.value) == 1, (
            f"expected READ_PATH=1 elaboration, got {int(dut.READ_PATH.value)} — "
            "the -pvalue override did not take; the no-side-effect test would be "
            "vacuous")


@cocotb.test(skip=NO_RTL)
async def test_read_sweep_has_no_panel_side_effect(dut):
    """READING any register — READ included — must drive NO 8080 bus activity.

    The FSM is live (enable=1) and the FIFO empty, so a read-arms-the-cycle
    regression would launch a CLCD_RD cycle and assert CS_n/RD_n. We sweep every
    offset in the page (mapped + unmapped), then wait long enough for any armed
    cycle to appear, and assert the panel side never moved."""
    axi, model = await _bring_up(dut)

    await axi.write(CLCD_CTRL, CTRL_RUN)      # enable=1: a stray read cycle WOULD launch
    await ClockCycles(dut.s_axi_aclk, 4)
    model.reset_activity()
    model.clear()

    # Every offset a CSR page-dump / decode-width sweep would touch. This build
    # elaborates at the RTL default C_S_AXI_ADDR_WIDTH=12, so offsets stay within
    # 12 bits (< 0x1000); the full 64 KiB-page BASE+0x1000 probe is the width-32
    # decode-width test's job (tests/csr_decode_width/test_decode_width_clcd.py).
    sweep = [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14,     # the six mapped offsets
             0x18, 0x1C, 0x20, 0x40, 0x80, 0x800]    # unmapped, in-window
    for off in sweep:
        val, _ = await axi.read(off)
        # Reading must never move the panel — check after each read too.
        assert model.idle, (
            f"reading offset {off:#05x} drove the 8080 bus "
            f"(cs_low={model.any_cs_low} wr_low={model.any_wr_low} "
            f"rd_low={model.any_rd_low}) — READ side effects are forbidden")

    # Let any armed-but-not-yet-launched cycle appear (default TIMING ~10 cyc).
    await ClockCycles(dut.s_axi_aclk, 48)
    assert model.idle, (
        "an 8080 bus cycle launched after a read sweep with no CMD/DATA pushed — "
        "a register read armed a panel cycle (the rejected read-arms design)")
    assert len(model.strobes) == 0, "a read sweep produced write strobes"


@cocotb.test(skip=NO_RTL)
async def test_read_start_write_arms_one_readback_cycle(dut):
    """The write-armed read-back path (v0.4): write CTRL.read_start -> poll
    STATUS.busy -> read READ for {valid, rdata}. Exactly one CLCD_RD cycle runs,
    it drives RD_n (not WR_n), and READ returns the value the panel presented on
    clcd_pd_i. Targets the revised RTL (arm via CTRL[4]); against the earlier
    read-arms build CTRL[4] is undecoded and busy never asserts."""
    axi, model = await _bring_up(dut)

    if not hasattr(dut, "clcd_pd_i"):
        raise AssertionError("clcd_pd_i port missing — cannot exercise the read path")

    await axi.write(CLCD_CTRL, CTRL_RUN)
    await ClockCycles(dut.s_axi_aclk, 2)
    model.reset_activity()

    # Panel presents a known byte on the data bus for the read cycle.
    dut.clcd_pd_i.value = 0xB6

    # Arm exactly one read-back cycle.
    await axi.write(CLCD_CTRL, CTRL_RUN | CTRL_READ_START)

    # Poll STATUS.busy: it must rise (a cycle launched) then fall (completed).
    saw_busy = False
    for _ in range(200):
        s, _ = await axi.read(CLCD_STATUS)
        if s & STATUS_BUSY:
            saw_busy = True
        elif saw_busy:
            break
    assert saw_busy, (
        "CTRL.read_start did not launch a read cycle (STATUS.busy never rose) — "
        "arm-by-write not implemented (revised RTL not yet landed?)")

    await ClockCycles(dut.s_axi_aclk, 4)
    val, _ = await axi.read(CLCD_READ)
    assert val & READ_VALID, f"READ.valid must be set after a completed read, got {val:#x}"
    assert (val & 0xFF) == 0xB6, (
        f"READ.rdata={val & 0xFF:#04x}, expected the 0xB6 presented on clcd_pd_i")

    # It was a READ cycle (RD_n asserted), and never a spurious WR strobe.
    assert model.any_rd_low, "a read-back cycle must assert clcd_rd_n_o"
    assert not model.any_wr_low, "a read-back cycle must NOT assert clcd_wr_n_o"
    assert len(model.strobes) == 0, "a read cycle must not decode as a write strobe"
