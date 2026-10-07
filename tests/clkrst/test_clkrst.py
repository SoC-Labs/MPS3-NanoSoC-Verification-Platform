"""tests/clkrst/test_clkrst.py

Verifies `fpga/shell/ip/clkrst/dut_clkrst.sv` — the DUT clock (DRP MMCM
front-end) + 3-reset regmap block (ARCHITECTURE_SPEC.md §5). This is one
of two blocks (with dfx_ctl) that together own the three partition-pin
resets; this bench specifically exercises the cross-block composition
documented in both files: `rp_resetn_o` must be gated by `dfx_ctl`'s
`rp_resetn_gate_i`, not just this block's own `RESET_CTRL` bit.

What this proves:
  1. RESET_CTRL bit writes drive the corresponding *_resetn_o pin (the
     host-controllable "DUT system reset" primitive, spec §5 reset #1).
  2. ext_por_n_i (board power-on reset) forces ALL THREE resets low
     regardless of RESET_CTRL — the async-assert source, spec §5 "asserted
     asynchronously."
  3. rp_resetn_gate_i (from dfx_ctl.sv) gates rp_resetn_o independently of
     RESET_CTRL.rp_resetn — the cross-block composition dfx_ctl's own
     bench (tests/dfx_ctl/) exercises from the other side.
  4. dbg_reset_req_i (OpenOCD srst, spec §5 reset #3) forces dbg_resetn_o
     low regardless of RESET_CTRL.
  5. STATUS.mmcm_locked mirrors mmcm_locked_i (the DRP MMCM lock signal a
     host polls after a set_clk request, net-protocol.md `set_clk` ->
     `{"locked":true}`).
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
    AxiLiteMaster, CLKRST_RESET_CTRL, CLKRST_DUT_CLK_SEL, CLKRST_DUT_CLK_DRP,
    CLKRST_STATUS,
    RESET_CTRL_DUT_RESETN, RESET_CTRL_RP_RESETN, RESET_CTRL_DBG_RESETN,
    CLKRST_STATUS_MMCM_LOCKED, CLKRST_STATUS_DUT_CLK_ALIVE,
)

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "shell", "ip", "clkrst")
NO_RTL = not rtl_ready(_RTL_DIR, ["dut_clkrst.sv"])

_ALL_RELEASED = RESET_CTRL_DUT_RESETN | RESET_CTRL_RP_RESETN | RESET_CTRL_DBG_RESETN

# Bench fix (2026-07-04, W-SIM — matches the "IMPORTANT NOTE FOR A6" in
# dut_clkrst.sv's header): the original _bring_up() never drove dut_clk_i
# with a Clock and sampled *_resetn_o one s_axi_aclk edge after a
# RESET_CTRL write. That contradicts the CONTRACT, not just the RTL:
# partition-pins.md's "Clock/reset domain rule" requires the three resets to
# deassert SYNCHRONIZED to dut_clk (async-assert/sync-deassert), so a
# release is only observable after 2-3 dut_clk_i edges — which needs
# dut_clk_i to actually tick. The RTL implements exactly that (3-FF
# Cummings-pattern generators clocked by dut_clk_i); the bench was fixed to
# match. Assert-direction checks stay single-edge (assertion is async by
# the same rule); every release/recovery check now waits _SETTLE dut_clk_i
# cycles instead.
_SETTLE = 5  # > 3-FF sync depth, small enough to keep the bench snappy


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    # 20 ns dut_clk: deliberately different from (and slower than) the AXI
    # clock so a same-domain shortcut in the RTL couldn't pass by accident.
    cocotb.start_soon(Clock(dut.dut_clk_i, 20, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.ext_por_n_i.value = 1
    dut.rp_resetn_gate_i.value = 1
    dut.dbg_reset_req_i.value = 0
    dut.mmcm_locked_i.value = 0
    dut.drp_do_i.value = 0
    dut.drp_drdy_i.value = 0
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)
    axi = AxiLiteMaster.from_dut(dut)
    await axi.write(CLKRST_RESET_CTRL, _ALL_RELEASED)
    # sync-deassert: releases propagate through the dut_clk-domain 3-FF
    # chains, so wait dut_clk cycles (not one AXI edge) before sampling.
    await ClockCycles(dut.dut_clk_i, _SETTLE)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reset_ctrl_write_releases_dut_resetn(dut):
    """What this proves: a host write to RESET_CTRL.dut_resetn (1=
    released) drives the dut_resetn partition pin -- the "reset the DUT
    from a host register" acceptance criterion (spec §14 phase 4)."""
    axi = await _bring_up(dut)
    assert int(dut.dut_resetn_o.value) == 1, "dut_resetn_o must be released after RESET_CTRL write"

    # assert direction is ASYNC (partition-pins.md rule) -- observable
    # immediately after the register write commits; one AXI edge is enough.
    await axi.write(CLKRST_RESET_CTRL, _ALL_RELEASED & ~RESET_CTRL_DUT_RESETN)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.dut_resetn_o.value) == 0, "clearing RESET_CTRL.dut_resetn must assert dut_resetn_o"


@cocotb.test(skip=NO_RTL)
async def test_ext_por_n_forces_all_resets_low(dut):
    """What this proves: ext_por_n_i is the async-assert source and wins
    over every RESET_CTRL bit -- spec §5 "asserted asynchronously" (all
    three resets must go low together on a board power-on-reset event,
    regardless of what the host had programmed)."""
    axi = await _bring_up(dut)
    for sig in (dut.dut_resetn_o, dut.rp_resetn_o, dut.dbg_resetn_o):
        assert int(sig.value) == 1

    dut.ext_por_n_i.value = 0
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.dut_resetn_o.value) == 0
    assert int(dut.rp_resetn_o.value) == 0
    assert int(dut.dbg_resetn_o.value) == 0

    dut.ext_por_n_i.value = 1
    # release is SYNC to dut_clk (async-assert/sync-deassert) -- allow the
    # 3-FF chains to refill before sampling.
    await ClockCycles(dut.dut_clk_i, _SETTLE)
    assert int(dut.dut_resetn_o.value) == 1, "resets must recover once ext_por_n_i deasserts (RESET_CTRL still all-released)"


@cocotb.test(skip=NO_RTL)
async def test_rp_resetn_gated_by_dfx_ctl(dut):
    """What this proves: rp_resetn_o also depends on rp_resetn_gate_i
    (driven by dfx_ctl.sv's rp_resetn_gate_o during a swap) independently
    of RESET_CTRL.rp_resetn -- this is the cross-block composition both
    files' headers document ("the two combine static-side")."""
    axi = await _bring_up(dut)
    assert int(dut.rp_resetn_o.value) == 1

    dut.rp_resetn_gate_i.value = 0  # as if dfx_ctl asserted DECOUPLE
    # R7: the assert path is now REGISTERED -- an FF, not a LUT, drives the async
    # reset (LUTAR-1). rp_resetn_gate_i is s_axi_aclk-domain (it comes from
    # dfx_ctl.sv), so it is sampled on an s_axi_aclk edge and then asynchronously
    # clears the dut_clk sync chain. Allow the edge + one delta for the NBA
    # update to reach that async clear. ext_por_n_i remains the ONLY
    # asynchronous assert source, and is still tested as such above.
    await RisingEdge(dut.s_axi_aclk)
    await Timer(1, "ns")
    assert int(dut.rp_resetn_o.value) == 0, "rp_resetn_gate_i=0 must hold rp_resetn_o low even with RESET_CTRL.rp_resetn=1"
    assert int(dut.dut_resetn_o.value) == 1, "the gate must be rp_resetn-specific, not affect dut_resetn_o"

    dut.rp_resetn_gate_i.value = 1
    await ClockCycles(dut.dut_clk_i, _SETTLE)  # sync-deassert, see _SETTLE note
    assert int(dut.rp_resetn_o.value) == 1, "rp_resetn_o must recover once the gate releases"


@cocotb.test(skip=NO_RTL)
async def test_dbg_reset_req_asserts_dbg_resetn_low(dut):
    """What this proves: dbg_reset_req_i (OpenOCD srst via the SWD path,
    spec §5 reset #3) forces dbg_resetn_o low independently of
    RESET_CTRL.dbg_resetn."""
    axi = await _bring_up(dut)
    assert int(dut.dbg_resetn_o.value) == 1

    dut.dbg_reset_req_i.value = 1
    # R7: registered assert path -- see the note in test_rp_resetn_gated_by_dfx_ctl.
    await RisingEdge(dut.s_axi_aclk)
    await Timer(1, "ns")
    assert int(dut.dbg_resetn_o.value) == 0, "dbg_reset_req_i=1 must assert dbg_resetn_o"

    dut.dbg_reset_req_i.value = 0
    await ClockCycles(dut.dut_clk_i, _SETTLE)  # sync-deassert, see _SETTLE note
    assert int(dut.dbg_resetn_o.value) == 1, "dbg_resetn_o must recover once srst releases"


@cocotb.test(skip=NO_RTL)
async def test_status_mmcm_locked_mirrors_input(dut):
    """What this proves: STATUS.mmcm_locked mirrors mmcm_locked_i -- the
    bit a host polls after set_clk (net-protocol.md: `{"op":"set_clk",...}`
    -> `{"ok":true,"locked":true}`)."""
    axi = await _bring_up(dut)

    dut.mmcm_locked_i.value = 0
    await RisingEdge(dut.s_axi_aclk)
    status, _ = await axi.read(CLKRST_STATUS)
    assert status & CLKRST_STATUS_MMCM_LOCKED == 0

    dut.mmcm_locked_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    status, _ = await axi.read(CLKRST_STATUS)
    assert status & CLKRST_STATUS_MMCM_LOCKED, "STATUS.mmcm_locked must mirror mmcm_locked_i=1"


@cocotb.test(skip=NO_RTL)
async def test_wstrb_partial_write_dut_clk_drp(dut):
    """WSTRB byte-lane test (previously untested on EVERY AXI slave — the BFM
    only ever drove strb=0xF). DUT_CLK_DRP is CLKRST's one register with a
    real per-lane decode (RTL: wstrb[0]->[7:0], wstrb[1]->[15:8]). A partial
    strobe must update ONLY the enabled lane; a slave that ignores WSTRB
    (commits the whole word regardless) fails here."""
    axi = await _bring_up(dut)

    # Seed the full 16-bit window.
    await axi.write_bytes(CLKRST_DUT_CLK_DRP, 0xABCD, 0xF)
    val, _ = await axi.read(CLKRST_DUT_CLK_DRP)
    assert val == 0xABCD, f"seed readback {val:#x}"

    # Low lane only: [7:0] -> 0x11; [15:8] must stay 0xAB (NOT become 0x22).
    await axi.write_bytes(CLKRST_DUT_CLK_DRP, 0x2211, 0x1)
    val, _ = await axi.read(CLKRST_DUT_CLK_DRP)
    assert val == 0xAB11, f"wstrb=0x1 must update only [7:0]; got {val:#06x} (WSTRB ignored?)"

    # High lane only: [15:8] -> 0x77; [7:0] must stay 0x11.
    await axi.write_bytes(CLKRST_DUT_CLK_DRP, 0x7799, 0x2)
    val, _ = await axi.read(CLKRST_DUT_CLK_DRP)
    assert val == 0x7711, f"wstrb=0x2 must update only [15:8]; got {val:#06x}"

    # No lanes enabled: a bare address touch changes nothing.
    await axi.write_bytes(CLKRST_DUT_CLK_DRP, 0x0000, 0x0)
    val, _ = await axi.read(CLKRST_DUT_CLK_DRP)
    assert val == 0x7711, f"wstrb=0x0 must change nothing; got {val:#06x}"

    # DUT_CLK_SEL is a single-lane (wstrb[0]) register: a lane-0-disabled
    # write must not disturb it either.
    await axi.write_bytes(CLKRST_DUT_CLK_SEL, 0x5A, 0x1)
    val, _ = await axi.read(CLKRST_DUT_CLK_SEL)
    assert val == 0x5A, f"DUT_CLK_SEL seed {val:#x}"
    await axi.write_bytes(CLKRST_DUT_CLK_SEL, 0xA5, 0x2)  # lane 0 disabled
    val, _ = await axi.read(CLKRST_DUT_CLK_SEL)
    assert val == 0x5A, f"DUT_CLK_SEL wstrb without lane0 must not change it; got {val:#x}"


async def _drp_responder(dut, served):
    """A minimal DRP slave model for the CLKRST DRP master port. Watches
    drp_den_o and, two cycles after any transaction start, drives a read-data
    word on drp_do_i + a one-cycle drp_drdy_i completion strobe — i.e. what a
    real clk_wiz DRP port would answer. `served['n']` counts transactions
    seen. All signal writes happen in writable (post-edge) phases."""
    dut.drp_do_i.value = 0
    dut.drp_drdy_i.value = 0
    while True:
        await ReadOnly()
        den = int(dut.drp_den_o.value)
        await RisingEdge(dut.s_axi_aclk)   # post-edge callback = writable phase
        if den:
            served['n'] += 1
            await ClockCycles(dut.s_axi_aclk, 2)
            dut.drp_do_i.value = 0x5A5A
            dut.drp_drdy_i.value = 1
            await RisingEdge(dut.s_axi_aclk)
            dut.drp_drdy_i.value = 0


@cocotb.test(skip=NO_RTL)
async def test_dut_clk_sel_drp_writes_and_drp_master_is_idle(dut):
    """What this proves (CLKRST DRP path — previously ENTIRELY untested; the
    DRP return was tied 0 and the DRP master left a documented placeholder):
    DUT_CLK_SEL (preset id) and DUT_CLK_DRP (arbitrary MMCM DRP window)
    register writes store and read back, AND the DRP master port stays idle —
    drp_den_o/drp_dwe_o never assert, so the DRP-responder model attached here
    is never triggered.

    This PINS the current, documented behaviour (dut_clkrst.sv header: "The DRP
    master sequencing itself is left as a documented placeholder ... only the
    regmap decode of DUT_CLK_SEL/DUT_CLK_DRP is real") and is the regression
    guard that will flip the day A1 wires a real preset-ROM + DRP FSM: at that
    point served['n'] goes non-zero and this test must be updated to check the
    actual MMCM reconfiguration. FLAGGED FOR THE RTL OWNER: DUT_CLK_SEL/DRP
    writes do not yet reconfigure the DUT clock."""
    axi = await _bring_up(dut)
    served = {"n": 0}
    cocotb.start_soon(_drp_responder(dut, served))

    await axi.write(CLKRST_DUT_CLK_SEL, 0x03)
    val, _ = await axi.read(CLKRST_DUT_CLK_SEL)
    assert val == 0x03, f"DUT_CLK_SEL must store the preset id; got {val:#x}"

    await axi.write(CLKRST_DUT_CLK_DRP, 0x1234)
    val, _ = await axi.read(CLKRST_DUT_CLK_DRP)
    assert val == 0x1234, f"DUT_CLK_DRP must store the DRP window; got {val:#x}"

    # Give the (placeholder) DRP master ample time to (not) issue anything.
    await ClockCycles(dut.s_axi_aclk, 40)
    assert int(dut.drp_den_o.value) == 0, "drp_den_o must stay idle (placeholder DRP master)"
    assert int(dut.drp_dwe_o.value) == 0, "drp_dwe_o must stay idle"
    assert served["n"] == 0, (
        "the tied-idle DRP master must not issue any DRP transaction yet — if "
        "this becomes non-zero the real DRP FSM landed and this test needs "
        "updating (see docstring)")


@cocotb.test(skip=NO_RTL)
async def test_dut_clk_alive_heartbeat_tracks_dut_clk(dut):
    """What this proves (STATUS.dut_clk_alive — the liveness heartbeat a host
    polls after set_clk; previously untested): with dut_clk_i toggling the bit
    reads 1; once dut_clk_i STOPS it saturates its stale-timeout and clears to
    0 (a real liveness detector, not a latch-once); and it recovers to 1 when
    dut_clk_i restarts.

    FINDING for the RTL owner (A1/A6): the heartbeat generator flop
    `dutclk_toggle_q` in dut_clkrst.sv has NO reset and no initial value, so in
    4-state RTL simulation it powers up to X and stays X forever (`~x == x`) —
    the whole dut_clk_alive feature is dead/unverifiable in sim. On a Xilinx
    FPGA global-set-reset inits it to 0 so it works in hardware; this bench
    mirrors that GSR init with a one-time deposit below so the real liveness
    logic CAN be exercised. Recommend giving the flop an explicit reset (or an
    `= 1'b0` initial) so simulation matches silicon without the deposit."""
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.ext_por_n_i.value = 1
    dut.rp_resetn_gate_i.value = 1
    dut.dbg_reset_req_i.value = 0
    dut.mmcm_locked_i.value = 0
    dut.drp_do_i.value = 0
    dut.drp_drdy_i.value = 0
    # GSR-mirroring seed for the reset-less toggle flop (see docstring finding).
    dut.dutclk_toggle_q.value = 0
    dclk = cocotb.start_soon(Clock(dut.dut_clk_i, 20, units="ns").start())
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)
    axi = AxiLiteMaster.from_dut(dut)

    # Toggling dut_clk -> alive.
    alive = 0
    for _ in range(60):
        status, _ = await axi.read(CLKRST_STATUS)
        if status & CLKRST_STATUS_DUT_CLK_ALIVE:
            alive = 1
            break
    assert alive, "dut_clk_alive must set while dut_clk_i toggles"

    # Stop dut_clk: after > ALIVE_TIMEOUT(255) s_axi cycles the bit clears.
    dclk.kill()
    dut.dut_clk_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 300)
    status, _ = await axi.read(CLKRST_STATUS)
    assert not (status & CLKRST_STATUS_DUT_CLK_ALIVE), (
        "dut_clk_alive must clear once dut_clk_i stops (stale-timeout, not latch-once)")

    # Restart dut_clk: heartbeat recovers.
    cocotb.start_soon(Clock(dut.dut_clk_i, 20, units="ns").start())
    recovered = 0
    for _ in range(60):
        status, _ = await axi.read(CLKRST_STATUS)
        if status & CLKRST_STATUS_DUT_CLK_ALIVE:
            recovered = 1
            break
    assert recovered, "dut_clk_alive must recover once dut_clk_i restarts"


@cocotb.test(skip=NO_RTL)
async def test_random_aw_en_corners(dut):
    """Constrained-random AXI4-Lite aw_en corner coverage (A5 random wave).

    The single biggest condition-coverage hole is the shared Xilinx-template
    write FSM: a same-cycle-only BFM never presents AWVALID and WVALID in
    different cycles, so `s_axi_awvalid`/`s_axi_wvalid` never take independent
    values in the accept condition. This hammers the slave with randomized
    AW/W arrival orders (same / AW-first / W-first), zero-gap back-to-back
    writes, read-during-write, and randomized WSTRB. The bound
    axi4lite_protocol_checker SVA validates the handshake protocol on every
    beat; a violation $fatal()s the bench. Seed is logged for reproduction."""
    await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "clkrst")
    m = RandomAxiMaster.from_dut(dut)
    waddrs = [CLKRST_RESET_CTRL, CLKRST_DUT_CLK_SEL, CLKRST_DUT_CLK_DRP]
    raddrs = waddrs + [CLKRST_STATUS]
    tally = await random_axi_burst(m, rng, waddrs, raddrs, n=48)
    dut._log.info(f"[clkrst random] orderings/paths exercised: {tally}")
