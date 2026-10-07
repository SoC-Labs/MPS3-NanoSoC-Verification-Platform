"""tests/dfx_ctl/test_dfx_ctl.py

Verifies `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` — the DFXCTL regmap block
(decoupler + AXI Shutdown Manager control + RP-reset gate). This is the
block `firmware/coordinator/swap_fsm.c` pokes first and last in every swap
(`step_decouple_assert()` / `step_release()`): "Decoupling is mandatory
before/through/after a partial load" (ARCHITECTURE_SPEC.md §7, §16).

What this proves:
  1. Writing DECOUPLE.decouple_en asserts decouple_en_o AND (via
     dfx_ctl.sv's own, already-real composition) deasserts
     rp_resetn_gate_o — the exact two actions swap_fsm.c's
     step_decouple_assert() performs together.
  2. STATUS is a read-only mirror of decoupled_i/rp_in_reset_i (the
     vendor Decoupler/Shutdown-Mgr IP's own status), matching
     swap_fsm.c's step_decouple_assert() poll:
     `DFXCTL_STATUS & (DECOUPLED|RP_IN_RESET) == both-set` before advancing.
  3. Writing SHUTDOWN.axi_shutdown asserts axi_shutdown_req_o.
  4. rm_id_i/dut_lockup_i are wired (port-level only — see dut_notes.md's
     ambiguity note on the missing regmap offset for these).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from dut_presence import rtl_ready
from random_stim import RandomAxiMaster, random_axi_burst, seeded_rng
from regmap import (
    AxiLiteMaster, DFXCTL_DECOUPLE, DFXCTL_SHUTDOWN, DFXCTL_STATUS,
    DFXCTL_RM_ID, DFXCTL_RM_STATUS,
    DECOUPLE_EN, SHUTDOWN_AXI_SHUTDOWN,
    DFXCTL_STATUS_DECOUPLED, DFXCTL_STATUS_RP_IN_RESET,
    DFXCTL_RM_STATUS_RM_ID_VALID, DFXCTL_RM_STATUS_DUT_LOCKUP,
    DFXCTL_RM_STATUS_DUT_ETH_IRQ,
)

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "shell", "ip", "dfx_ctl")
NO_RTL = not rtl_ready(_RTL_DIR, ["dfx_ctl.sv"])


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.decoupled_i.value = 0
    dut.axi_shutdown_ack_i.value = 0
    dut.rp_in_reset_i.value = 0
    dut.rm_id_i.value = 0
    dut.dut_lockup_i.value = 0
    dut.dut_eth_irq_i.value = 0   # DUT eth irq_out -> RM_STATUS[2] (2026-07-24)
    # The isolation bit's own resets (added with the shell watchdog, 2026-09-14).
    # ext_por_n_i is the board POR and is the ONLY reset that releases the clamp;
    # wdt_reset_i is the watchdog's, and SETS it. Held at the not-in-POR,
    # not-firing values for every test but the watchdog one below.
    dut.ext_por_n_i.value = 0     # in POR for the duration of s_axi_aresetn
    dut.wdt_reset_i.value = 0
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    dut.ext_por_n_i.value = 1
    # Two clocks for the POR synchroniser, so decouple_en_q leaves its POR
    # branch before any test writes it.
    await ClockCycles(dut.s_axi_aclk, 3)
    await RisingEdge(dut.s_axi_aclk)
    return AxiLiteMaster.from_dut(dut)


@cocotb.test(skip=NO_RTL)
async def test_decouple_write_asserts_pin_and_releases_rp_reset_gate(dut):
    """What this proves: the swap sequence's very first hardware action
    (net-protocol.md step 1: "assert DECOUPLE + hold rp_resetn") — a
    DECOUPLE.decouple_en=1 write must both assert decouple_en_o (to the
    vendor Decoupler IP) and drop rp_resetn_gate_o (which dut_clkrst.sv
    ANDs into the actual rp_resetn partition pin, per both files' header
    comments on rp_resetn composition)."""
    axi = await _bring_up(dut)

    assert int(dut.rp_resetn_gate_o.value) == 1, "rp_resetn_gate_o must start released (not decoupled)"

    await axi.write(DFXCTL_DECOUPLE, DECOUPLE_EN)
    await RisingEdge(dut.s_axi_aclk)

    assert int(dut.decouple_en_o.value) == 1, "decouple_en_o must assert after DECOUPLE.decouple_en=1"
    assert int(dut.rp_resetn_gate_o.value) == 0, (
        "rp_resetn_gate_o must drop (hold RP in reset) once decoupled -- "
        "swap_fsm.c's step_decouple_assert() relies on this composition"
    )


@cocotb.test(skip=NO_RTL)
async def test_status_mirrors_decoupled_and_rp_in_reset_inputs(dut):
    """What this proves: STATUS is a read-only passthrough of the vendor
    IP's own status pins -- exactly the two bits swap_fsm.c's
    step_decouple_assert()/step_release() poll before advancing the swap
    FSM (`DFXCTL_STATUS & (DECOUPLED|RP_IN_RESET)`)."""
    axi = await _bring_up(dut)

    dut.decoupled_i.value = 0
    dut.rp_in_reset_i.value = 0
    await RisingEdge(dut.s_axi_aclk)
    status, _resp = await axi.read(DFXCTL_STATUS)
    assert status & DFXCTL_STATUS_DECOUPLED == 0
    assert status & DFXCTL_STATUS_RP_IN_RESET == 0

    dut.decoupled_i.value = 1
    dut.rp_in_reset_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    status, _resp = await axi.read(DFXCTL_STATUS)
    assert status & DFXCTL_STATUS_DECOUPLED, "STATUS.decoupled must mirror decoupled_i"
    assert status & DFXCTL_STATUS_RP_IN_RESET, "STATUS.rp_in_reset must mirror rp_in_reset_i"


@cocotb.test(skip=NO_RTL)
async def test_shutdown_write_asserts_axi_shutdown_req(dut):
    """What this proves: SHUTDOWN.axi_shutdown drives the AXI Shutdown
    Manager quiesce request -- the other half of step_decouple_assert()'s
    two writes (net-protocol.md step 1)."""
    axi = await _bring_up(dut)
    assert int(dut.axi_shutdown_req_o.value) == 0

    await axi.write(DFXCTL_SHUTDOWN, SHUTDOWN_AXI_SHUTDOWN)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.axi_shutdown_req_o.value) == 1, "axi_shutdown_req_o must assert after SHUTDOWN.axi_shutdown=1"

    await axi.write(DFXCTL_SHUTDOWN, 0)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.axi_shutdown_req_o.value) == 0, "axi_shutdown_req_o must deassert once cleared"


@cocotb.test(skip=NO_RTL)
async def test_rm_id_and_lockup_ports_are_wired(dut):
    """What this proves: rm_id_i/dut_lockup_i are at least electrically
    connected (port-level smoke test only) -- this bench cannot check
    anything regmap-visible for RM-load-verify yet because no offset for
    it is confirmed (dut_notes.md ambiguity note / platform_regs.h
    AMBIGUITY(A6) #4). Update this test once that lands.
    """
    await _bring_up(dut)
    dut.rm_id_i.value = 0xDEADBEEF
    dut.dut_lockup_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    # No assertion beyond "driving these ports doesn't error/X the sim" --
    # intentionally weak pending the regmap ambiguity above.


@cocotb.test(skip=NO_RTL)
async def test_rm_id_and_rm_status_regmap_readback(dut):
    """What this proves — the RM-load-verify readback (RM_ID@0x10 /
    RM_STATUS@0x14, shell-regmap.md v0.1 I8) that swap_fsm.c's step_verify()
    compares against. This is NO LONGER just a port-level smoke test (see the
    sibling test above / dut_notes): dfx_ctl.sv now fully decodes both offsets
    with a 2-FF rm_id synchronizer + an N-cycle stability detector.

      * RM_ID reads back the synchronized rm_id_i.
      * RM_STATUS.rm_id_valid asserts once rm_id_i has been stable long enough
        (RM_ID_SETTLE_CYCLES) — the "trust RM_ID now" qualifier firmware polls.
      * RM_STATUS.dut_lockup mirrors dut_lockup_i (2-FF synchronized)."""
    axi = await _bring_up(dut)

    dut.rm_id_i.value = 0xCAFEF00D
    dut.dut_lockup_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 20)  # > 2-FF sync + 8-cycle settle

    val, _ = await axi.read(DFXCTL_RM_ID)
    assert val == 0xCAFEF00D, f"RM_ID must read the synchronized rm_id_i; got {val:#x}"
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert status & DFXCTL_RM_STATUS_RM_ID_VALID, "rm_id_valid must set once rm_id_i settles"
    assert not (status & DFXCTL_RM_STATUS_DUT_LOCKUP), "dut_lockup must be low"

    # A new, stable rm_id re-settles and reads back.
    dut.rm_id_i.value = 0x12345678
    await ClockCycles(dut.s_axi_aclk, 20)
    val, _ = await axi.read(DFXCTL_RM_ID)
    assert val == 0x12345678, f"RM_ID must track a new stable rm_id_i; got {val:#x}"
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert status & DFXCTL_RM_STATUS_RM_ID_VALID, "rm_id_valid must re-assert after re-settle"

    # dut_lockup is reflected (2-FF synchronized).
    dut.dut_lockup_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 4)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert status & DFXCTL_RM_STATUS_DUT_LOCKUP, "RM_STATUS.dut_lockup must mirror dut_lockup_i"

    # dut_eth_irq (the DUT MAC's irq_out) is reflected at RM_STATUS[2], 2-FF
    # synchronized, INDEPENDENTLY of dut_lockup. This is the on-silicon
    # observability of DUT ethernet activity (e.g. eth_ss raising RX-frame-
    # received once GENCHK generates traffic through the virtual PHY).
    dut.dut_lockup_i.value = 0
    dut.dut_eth_irq_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 4)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert status & DFXCTL_RM_STATUS_DUT_ETH_IRQ, "RM_STATUS[2] must mirror dut_eth_irq_i"
    assert not (status & DFXCTL_RM_STATUS_DUT_LOCKUP), "dut_eth_irq must not alias dut_lockup"
    # ...and clears when the DUT irq deasserts.
    dut.dut_eth_irq_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 4)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert not (status & DFXCTL_RM_STATUS_DUT_ETH_IRQ), "RM_STATUS[2] must clear when dut_eth_irq_i drops"


@cocotb.test(skip=NO_RTL)
async def test_wstrb_lane0_gates_decouple_write(dut):
    """WSTRB byte-lane test (previously untested on EVERY AXI slave — the BFM
    only ever drove strb=0xF). DECOUPLE's register decode is gated on
    wstrb[0]; a write with lane 0 DISABLED must be ignored. A slave that
    ignores WSTRB (commits the whole word) would clear DECOUPLE here and
    fail. This matters: a spurious clear mid-swap would un-gate the RP
    reset (rp_resetn_gate_o) and let the RP run against a half-loaded RM."""
    axi = await _bring_up(dut)

    await axi.write_bytes(DFXCTL_DECOUPLE, DECOUPLE_EN, 0xF)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.decouple_en_o.value) == 1, "DECOUPLE.decouple_en must set with a full-word write"

    # Clear attempt with lane 0 disabled (only lane 1): must be ignored.
    await axi.write_bytes(DFXCTL_DECOUPLE, 0x0, 0x2)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.decouple_en_o.value) == 1, (
        "a DECOUPLE write with wstrb lane0 disabled must NOT change it (WSTRB ignored?)")
    val, _ = await axi.read(DFXCTL_DECOUPLE)
    assert val & DECOUPLE_EN, "DECOUPLE readback must still show set"

    # Lane 0 enabled: the clear now lands.
    await axi.write_bytes(DFXCTL_DECOUPLE, 0x0, 0x1)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.decouple_en_o.value) == 0, "a lane-0-enabled clear must deassert DECOUPLE"


@cocotb.test(skip=NO_RTL)
async def test_shutdown_ack_and_decouple_gate_reversal_sequence(dut):
    """What this proves — the full decouple -> gate -> shutdown -> reversal
    sequence this block runs on EVERY swap (spec §6.2/§7), and the one part
    previously never driven: axi_shutdown_ack_i.

      1. DECOUPLE=1 asserts decouple_en_o AND drops rp_resetn_gate_o (holds RP
         in reset) in the same action.
      2. SHUTDOWN=1 asserts axi_shutdown_req_o; the vendor AXI Shutdown Manager
         acknowledges via axi_shutdown_ack_i (driven here for the first time).
      3. Reversal: clearing DECOUPLE must NOT release the gate while the
         vendor Decoupler still reports decoupled_i=1 (guards against firmware
         clearing DECOUPLE a cycle before the IP re-couples); the gate releases
         only once decoupled_i drops.

    FINDING for the RTL owner (A1/A6) — pinned, not asserted-as-required:
    axi_shutdown_ack_i is accepted but NOT consumed by any logic in
    dfx_ctl.sv (it is `lint_off UNUSED`, and STATUS@0x08 has no shutdown-idle
    bit — see the RTL's own ambiguity #2). Driving the ack changes nothing
    observable: firmware cannot poll shutdown completion. This test drives the
    ack and confirms the gate sequencing (which is real) is unaffected by it,
    documenting the missing handshake path rather than requiring it."""
    axi = await _bring_up(dut)
    assert int(dut.rp_resetn_gate_o.value) == 1, "gate starts released"
    assert int(dut.axi_shutdown_req_o.value) == 0

    # 1. Decouple: pin drops the RP-reset gate.
    await axi.write(DFXCTL_DECOUPLE, DECOUPLE_EN)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.decouple_en_o.value) == 1
    assert int(dut.rp_resetn_gate_o.value) == 0, "DECOUPLE=1 must drop rp_resetn_gate_o"

    # Vendor Decoupler confirms isolation (2-FF synchronized inside).
    dut.decoupled_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 3)

    # 2. Request AXI shutdown, and drive the ack in response.
    await axi.write(DFXCTL_SHUTDOWN, SHUTDOWN_AXI_SHUTDOWN)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.axi_shutdown_req_o.value) == 1, "SHUTDOWN=1 must assert axi_shutdown_req_o"
    dut.axi_shutdown_ack_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 3)
    assert int(dut.rp_resetn_gate_o.value) == 0, (
        "gate stays held through shutdown (ack is not consumed — see finding)")

    # 3. Reversal: clear DECOUPLE; gate must HOLD while decoupled_i still high.
    await axi.write(DFXCTL_DECOUPLE, 0)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.decouple_en_o.value) == 0
    assert int(dut.rp_resetn_gate_o.value) == 0, (
        "gate must hold until the Decoupler confirms re-coupling (decoupled_i still 1)")

    # Decoupler confirms re-coupling: gate releases (after the 2-FF sync of
    # decoupled_i propagates through the gate FSM — poll a few cycles).
    dut.decoupled_i.value = 0
    released = 0
    for _ in range(12):
        await RisingEdge(dut.s_axi_aclk)
        if int(dut.rp_resetn_gate_o.value) == 1:
            released = 1
            break
    assert released, "gate must release once decoupled_i drops (re-coupling confirmed)"

    # Tear down the shutdown request.
    await axi.write(DFXCTL_SHUTDOWN, 0)
    dut.axi_shutdown_ack_i.value = 0
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.axi_shutdown_req_o.value) == 0, "clearing SHUTDOWN deasserts the request"


@cocotb.test(skip=NO_RTL)
async def test_random_aw_en_corners(dut):
    """Constrained-random AXI4-Lite aw_en corner coverage (A5 random wave):
    randomized AW/W arrival order (same / AW-first / W-first), zero-gap
    back-to-back writes, read-during-write, and randomized WSTRB across
    DFXCTL's rw offsets. Fills the shared write-FSM condition-coverage hole;
    the bound AXI protocol SVA validates every handshake. Seed logged."""
    await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "dfx_ctl")
    m = RandomAxiMaster.from_dut(dut)
    waddrs = [DFXCTL_DECOUPLE, DFXCTL_SHUTDOWN]
    raddrs = waddrs + [DFXCTL_STATUS, DFXCTL_RM_ID, DFXCTL_RM_STATUS]
    tally = await random_axi_burst(m, rng, waddrs, raddrs, n=48)
    dut._log.info(f"[dfx_ctl random] orderings/paths exercised: {tally}")


@cocotb.test(skip=NO_RTL)
async def test_rm_id_valid_gated_off_during_decouple_clamp_and_rp_reset(dut):
    """R4/R1 interaction: rm_id_valid must NEVER latch a clamped/decoupled id.

    Background: the R1 DFX decoupler clamps rm_id to 0 while DECOUPLE is
    asserted, and rm_id is meaningless while the RP is held in reset. dfx_ctl's
    rm_id_gate holds the settle counter cleared (and forces rm_id_valid low)
    whenever decouple_en_q / decoupled_i / rp_in_reset_i say the id is not the
    running RM's. Without this, the settle detector would see the clamped 0
    hold "stable" for 8 cycles and assert rm_id_valid=1 with VALUE 0 — firmware
    would then trust 0 as a real RM id.

    Firmware consequence (verified here): step_verify() can only trust RM_ID
    AFTER releasing DECOUPLE and the RP leaving reset — never before.
    """
    axi = await _bring_up(dut)

    # Baseline: a real, stable rm_id settles valid (gate all-clear).
    dut.rm_id_i.value = 0xABCD1234
    await ClockCycles(dut.s_axi_aclk, 16)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert status & DFXCTL_RM_STATUS_RM_ID_VALID, "baseline: valid must set on a stable id"

    # Assert DECOUPLE and model the R1 clamp (rm_id -> 0). valid must drop and
    # must NOT re-assert even though the clamped 0 is 'stable' for many cycles.
    await axi.write(DFXCTL_DECOUPLE, DECOUPLE_EN)
    dut.rm_id_i.value = 0x00000000              # decoupler clamp
    await ClockCycles(dut.s_axi_aclk, 32)       # >> RM_ID_SETTLE_CYCLES (8)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert not (status & DFXCTL_RM_STATUS_RM_ID_VALID), \
        "clamped-0 during DECOUPLE must NOT be latched as a valid id"
    val, _ = await axi.read(DFXCTL_RM_ID)
    assert val == 0, "RM_ID reads the clamped value; rm_id_valid is what gates trust"

    # Release DECOUPLE; the new running-RM id settles and valid re-asserts.
    await axi.write(DFXCTL_DECOUPLE, 0)
    dut.rm_id_i.value = 0x0000A1B2              # post-release real id
    await ClockCycles(dut.s_axi_aclk, 16)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert status & DFXCTL_RM_STATUS_RM_ID_VALID, "valid must re-assert after release+settle"
    val, _ = await axi.read(DFXCTL_RM_ID)
    assert val == 0x0000A1B2, f"RM_ID must track the post-release id; got {val:#x}"

    # rp_in_reset leg: holding the RP in reset also gates rm_id_valid off,
    # then it recovers once the RP leaves reset (id unchanged, re-settles).
    dut.rp_in_reset_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 8)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert not (status & DFXCTL_RM_STATUS_RM_ID_VALID), "rp_in_reset must gate rm_id_valid off"
    dut.rp_in_reset_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 16)
    status, _ = await axi.read(DFXCTL_RM_STATUS)
    assert status & DFXCTL_RM_STATUS_RM_ID_VALID, "valid must recover once the RP leaves reset"


# =============================================================================
# THE WATCHDOG UN-CLAMP HAZARD (docs/planning/SERVICES_PARTITION.md §5.4)
#
# Found by reading the RTL, not by a board: `dfx_ctl_0/s_axi_aresetn` is
# `shell_aresetn` = `proc_sys_reset_shell/peripheral_aresetn`, which is exactly
# what `aux_reset_in` -- the shell watchdog's reset -- pulses. While
# `decouple_en_q` was reset by it, a watchdog fire during an ICAP write CLEARED
# decouple_en and un-clamped the partition boundary at the moment the RP is
# transient garbage. Latent until 2026-09-14 only because nothing drove
# `aux_reset_in`; wiring `axi_timebase_wdt_0/wdt_reset` there is what makes it
# live, so the fix and the watchdog land together.
#
# SEEN TO FAIL ON THE OLD RTL:  make -C tests/dfx_ctl control-wdt-unclamp
# (that target rebuilds this same bench against a copy of dfx_ctl.sv whose
# decouple_en_q is put back on s_axi_aresetn -- ports unchanged, ONLY the reset
# reverted -- and the two tests below go red.)
# =============================================================================

async def _fire_watchdog(dut, wdt_cycles: int = 4, reset_tail: int = 12):
    """Model `axi_timebase_wdt_0/wdt_reset` -> `proc_sys_reset_shell/aux_reset_in`.

    Two facts the timing depends on, both measured against the real IP in
    Vivado 2024.1 rather than assumed:
      * proc_sys_reset:5.0's `C_AUX_RESET_HIGH` defaults to 1, so `wdt_reset`
        (active high) connects straight through with no inverter;
      * `C_AUX_RST_WIDTH` is 4, and `peripheral_aresetn` is held LOW for a tail
        AFTER `aux_reset_in` deasserts. That tail is the reason a naive
        "clamp while wdt_reset is high" fix does not work: the reset branch
        would run afterwards and clear the bit again.
    """
    dut.wdt_reset_i.value = 1
    dut.s_axi_aresetn.value = 0
    await ClockCycles(dut.s_axi_aclk, wdt_cycles)
    dut.wdt_reset_i.value = 0
    # ...and peripheral_aresetn stays low for a while longer. THE window.
    for _ in range(reset_tail):
        await ClockCycles(dut.s_axi_aclk, 1)
        assert int(dut.decouple_en_o.value) == 1, (
            "decouple_en_o dropped while peripheral_aresetn was still low -- "
            "the boundary un-clamped during the reset tail"
        )
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 3)


@cocotb.test(skip=NO_RTL)
async def test_watchdog_reset_must_not_unclamp_a_decoupled_boundary(dut):
    """Mid-swap, the watchdog fires. The boundary must STAY clamped.

    This is the hazard, reproduced: DECOUPLE is asserted (the FSM is between
    step_decouple_assert() and step_release(), i.e. the RP is mid-ICAP and its
    outputs are transient garbage), and a watchdog reset arrives. Every hazard
    the decoupler exists to prevent -- a stuck phy_rmii_tx_en injecting a
    runaway frame, a stuck uart_tx_tvalid flooding the console FIFO, a driven
    dut_gpio_oe fighting a board pad, a spurious irq_out -- goes live if
    decouple_en_o drops here.
    """
    axi = await _bring_up(dut)

    await axi.write(DFXCTL_DECOUPLE, DECOUPLE_EN)
    await RisingEdge(dut.s_axi_aclk)
    assert int(dut.decouple_en_o.value) == 1, "precondition: decoupled"
    assert int(dut.rp_resetn_gate_o.value) == 0, "precondition: RP held in reset"

    await _fire_watchdog(dut)

    assert int(dut.decouple_en_o.value) == 1, (
        "THE HAZARD: a watchdog reset cleared decouple_en and un-clamped the "
        "RP boundary mid-swap"
    )
    assert int(dut.rp_resetn_gate_o.value) == 0, (
        "the RP-reset hold was released while the boundary was still clamped"
    )
    val, _ = await axi.read(DFXCTL_DECOUPLE)
    assert val & DECOUPLE_EN, (
        "DECOUPLE reads back 0 after a watchdog reset while the pin is still "
        "asserted -- firmware would be told the opposite of the truth"
    )

    # And firmware can still get out of it the normal way: the RMW clear that
    # swap_fsm.c's step_release() performs releases the clamp.
    await axi.write(DFXCTL_DECOUPLE, 0)
    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.decouple_en_o.value) == 0, (
        "firmware could not clear the clamp after a watchdog reset -- the board "
        "would be permanently isolated"
    )


@cocotb.test(skip=NO_RTL)
async def test_watchdog_reset_asserts_the_clamp_even_when_the_rp_was_fine(dut):
    """Set-dominant, not merely preserved.

    The study offered two fixes -- give decouple_en_q a POR-only reset, OR make
    the watchdog reset assert decouple on its way out -- and preferred the
    second because it ASSERTS isolation rather than only keeping it. Both are
    implemented: POR is the only reset that releases, and wdt_reset_i sets.
    A supervisor that has just died cannot vouch for the RP, whatever the RP
    was doing a moment ago.
    """
    axi = await _bring_up(dut)

    val, _ = await axi.read(DFXCTL_DECOUPLE)
    assert (val & DECOUPLE_EN) == 0, "precondition: NOT decoupled"
    assert int(dut.decouple_en_o.value) == 0

    await _fire_watchdog(dut)

    assert int(dut.decouple_en_o.value) == 1, (
        "a watchdog reset left the boundary open on a board whose supervisor "
        "had just stopped answering"
    )


@cocotb.test(skip=NO_RTL)
async def test_only_por_releases_the_clamp(dut):
    """POR is the one reset that may release it, and it does.

    A POR reconfigures the whole device, so there is no RP state left to
    protect and no reason to come up isolated -- the power-on value must stay
    what it has always been. (This is also what keeps the change invisible to
    every existing shell: with wdt_reset_i tied low, the block behaves exactly
    as before except that a PERIPHERAL reset no longer clears the clamp.)
    """
    axi = await _bring_up(dut)
    await axi.write(DFXCTL_DECOUPLE, DECOUPLE_EN)
    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.decouple_en_o.value) == 1

    dut.ext_por_n_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 4)   # 2 for the synchroniser, 2 spare
    assert int(dut.decouple_en_o.value) == 0, (
        "POR did not release the clamp -- a power-on shell would come up with "
        "its DUT isolated and no firmware yet running to notice"
    )
    dut.ext_por_n_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 4)
    assert int(dut.decouple_en_o.value) == 0, "and it stays released"
