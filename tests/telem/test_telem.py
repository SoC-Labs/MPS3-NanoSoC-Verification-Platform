"""tests/telem/test_telem.py — SEAM MODE (SIM_FAKE_DATA=0, the default and
the synthesis configuration).

Verifies `fpga/shell/ip/telem/telem.sv` — the TELEM board-power-telemetry
CSR block (shell-regmap.md v0.1 @ 0x44A5_0000, I9). The INA228 I2C engine
is a documented follow-up module (`ina228_i2c_master`, not yet written);
this bench plays that engine's role by driving the `ina228_*` sample-
injection seam ports directly, per the "engine seam contract" table in the
block README — so these tests double as the seam's specification tests for
whoever writes the engine. (Why seam mode is the default bench and fake
mode the secondary `make FAKE=1` pass: see this directory's Makefile
header.)

What this proves:
  1. CTRL[1:0] writes reach the engine seam (`ina228_enable_o`/
     `ina228_alarm_en_o`) and read back; reset value 0 (sampling off).
  2. Atomic triple-register latch: all three readings latch TOGETHER on
     the 1-cycle `ina228_sample_valid_i` strobe; changing the seam data
     WITHOUT a strobe changes nothing (firmware never sees old/new mixes).
  3. Enable gating: strobes while CTRL.enable=0 are ignored; disabling
     HOLDS the last readings (only reset zeroes them).
  4. STATUS.i2c_err: sticky on a 1-cycle error strobe, cleared by ANY
     CTRL write, and SET-DOMINANT (an error coinciding with the clearing
     CTRL write is not lost).
  5. STATUS.alarm: live `alarm_i` level (2-FF synchronized) AND-gated by
     CTRL.alarm_en — not sticky.
  6. House conventions: RO/unmapped writes accepted-no-effect (and,
     specifically, a write to an RO offset must NOT clear i2c_err — only
     a CTRL write does); unmapped offsets inside the decode window read 0.
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
    AxiLiteMaster,
    TELEM_CTRL, TELEM_BUS_MV, TELEM_CURR_UA, TELEM_POWER_MW, TELEM_STATUS,
    TELEM_CTRL_ENABLE, TELEM_CTRL_ALARM_EN,
    TELEM_STATUS_ALARM, TELEM_STATUS_I2C_ERR,
)

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "telem")
NO_RTL = not rtl_ready(_RTL_DIR, ["telem.sv"])


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    # Engine seam idle: no engine attached would mean all-zero inputs —
    # exactly the "visibly no data source" posture the README documents.
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


async def _inject_sample(dut, bus_mv: int, curr_ua: int, power_mw: int):
    """Play the ina228_i2c_master role: present all three readings and
    strobe sample_valid for exactly one s_axi_aclk cycle (the seam
    contract's atomic-set semantics)."""
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_bus_mv_i.value = bus_mv
    dut.ina228_curr_ua_i.value = curr_ua
    dut.ina228_power_mw_i.value = power_mw
    dut.ina228_sample_valid_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_sample_valid_i.value = 0
    await RisingEdge(dut.s_axi_aclk)


async def _read_triple(axi):
    bus, _ = await axi.read(TELEM_BUS_MV)
    curr, _ = await axi.read(TELEM_CURR_UA)
    powr, _ = await axi.read(TELEM_POWER_MW)
    return bus, curr, powr


@cocotb.test(skip=NO_RTL)
async def test_ctrl_reaches_engine_seam_and_reads_back(dut):
    """What this proves: CTRL resets to 0 (sampling off until firmware
    turns it on) and its two bits drive the ina228_enable_o/
    ina228_alarm_en_o seam outputs the engine will latch; write data above
    [1:0] is ignored."""
    axi = await _bring_up(dut)

    val, _ = await axi.read(TELEM_CTRL)
    assert val == 0, "CTRL must reset to 0"
    assert int(dut.ina228_enable_o.value) == 0
    assert int(dut.ina228_alarm_en_o.value) == 0

    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)
    assert int(dut.ina228_enable_o.value) == 1
    assert int(dut.ina228_alarm_en_o.value) == 0
    val, _ = await axi.read(TELEM_CTRL)
    assert val == TELEM_CTRL_ENABLE

    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE | TELEM_CTRL_ALARM_EN)
    assert int(dut.ina228_enable_o.value) == 1
    assert int(dut.ina228_alarm_en_o.value) == 1

    # Bits above [1:0] ignored.
    await axi.write(TELEM_CTRL, 0xFFFF_FFFC)
    val, _ = await axi.read(TELEM_CTRL)
    assert val == 0, f"CTRL must mask write data to [1:0], got {val:#x}"
    assert int(dut.ina228_enable_o.value) == 0


@cocotb.test(skip=NO_RTL)
async def test_atomic_triple_latch_on_sample_valid(dut):
    """What this proves: the three RO readings update TOGETHER on the
    sample_valid strobe and ONLY then — between strobes the registers hold
    a consistent set even while the seam inputs churn (the README's
    "firmware never sees old/new mixes")."""
    axi = await _bring_up(dut)
    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)

    # Reset state: visibly "no data source", never plausible garbage.
    assert await _read_triple(axi) == (0, 0, 0)

    await _inject_sample(dut, bus_mv=1195, curr_ua=250_000, power_mw=299)
    assert await _read_triple(axi) == (1195, 250_000, 299)

    # Churn the seam data WITHOUT a strobe: nothing may move. This is the
    # atomicity check — a non-atomic implementation (e.g. registers fed
    # combinationally, or latched on different conditions) would tear here.
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_bus_mv_i.value = 9999
    dut.ina228_curr_ua_i.value = 1
    dut.ina228_power_mw_i.value = 77777
    await ClockCycles(dut.s_axi_aclk, 5)
    assert await _read_triple(axi) == (1195, 250_000, 299), (
        "readings must hold between strobes regardless of seam churn")

    # Second strobe: all three flip to the new set together.
    await _inject_sample(dut, bus_mv=1204, curr_ua=260_500, power_mw=313)
    assert await _read_triple(axi) == (1204, 260_500, 313)


@cocotb.test(skip=NO_RTL)
async def test_enable_gates_sample_accept_and_disable_holds(dut):
    """What this proves: samples strobed while CTRL.enable=0 are ignored,
    and disabling holds the last accepted readings (firmware can read the
    final values after stopping the engine; only reset zeroes them)."""
    axi = await _bring_up(dut)

    # enable=0 (reset state): a strobe must be ignored.
    await _inject_sample(dut, bus_mv=111, curr_ua=222, power_mw=333)
    assert await _read_triple(axi) == (0, 0, 0), (
        "samples while enable=0 must be ignored")

    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)
    await _inject_sample(dut, bus_mv=1200, curr_ua=300_000, power_mw=360)
    assert await _read_triple(axi) == (1200, 300_000, 360)

    # Disable, then strobe a different set: held, not latched.
    await axi.write(TELEM_CTRL, 0)
    await _inject_sample(dut, bus_mv=444, curr_ua=555, power_mw=666)
    assert await _read_triple(axi) == (1200, 300_000, 360), (
        "disable must HOLD the last readings, not accept new ones")


@cocotb.test(skip=NO_RTL)
async def test_i2c_err_sticky_ctrl_clear_and_set_dominance(dut):
    """What this proves: STATUS.i2c_err latches on a 1-cycle engine error
    strobe, stays set across reads (sticky, not clear-on-read), clears on
    ANY CTRL write (the chosen semantics — RTL ambiguity #2), and is
    set-dominant: an error level overlapping the clearing CTRL write must
    not be lost."""
    axi = await _bring_up(dut)

    # 1-cycle strobe -> sticky.
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 0
    await RisingEdge(dut.s_axi_aclk)

    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, "i2c_err must latch sticky on a 1-cycle strobe"
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, "i2c_err must survive a read (sticky, not clear-on-read)"

    # Any CTRL write clears — even one that rewrites the same value.
    await axi.write(TELEM_CTRL, 0)
    val, _ = await axi.read(TELEM_STATUS)
    assert not (val & TELEM_STATUS_I2C_ERR), "any CTRL write must clear i2c_err"

    # Set-dominance: hold the error level across a CTRL write; the write's
    # clear must lose to the concurrent set (the error is re-latched the
    # same cycle, so it can never be lost).
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 1
    await axi.write(TELEM_CTRL, 0)
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, (
        "set must dominate a coincident CTRL-write clear (error never lost)")

    dut.ina228_i2c_err_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 2)
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, "still sticky once the level drops"
    await axi.write(TELEM_CTRL, 0)
    val, _ = await axi.read(TELEM_STATUS)
    assert not (val & TELEM_STATUS_I2C_ERR)


@cocotb.test(skip=NO_RTL)
async def test_alarm_live_level_gated_by_alarm_en(dut):
    """What this proves: STATUS.alarm is the LIVE 2-FF-synchronized
    alarm_i level AND-gated by CTRL.alarm_en — visible only while both are
    true, and NOT sticky (RTL ambiguity #3: alarm reads as a condition,
    not an event)."""
    axi = await _bring_up(dut)

    # alarm_i high but alarm_en=0: gated off.
    dut.alarm_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 4)  # > 2-FF sync latency
    val, _ = await axi.read(TELEM_STATUS)
    assert not (val & TELEM_STATUS_ALARM), "alarm must be gated off while alarm_en=0"

    # Gate on: level shows.
    await axi.write(TELEM_CTRL, TELEM_CTRL_ALARM_EN)
    await ClockCycles(dut.s_axi_aclk, 4)
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_ALARM, "alarm_i=1 & alarm_en=1 must read as STATUS.alarm=1"

    # Live level, not sticky: drops when the pad drops (2-FF latency ok).
    dut.alarm_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 4)
    val, _ = await axi.read(TELEM_STATUS)
    assert not (val & TELEM_STATUS_ALARM), "alarm must follow the level back down (not sticky)"

    # And gates off again even with the pad high.
    dut.alarm_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 4)
    await axi.write(TELEM_CTRL, 0)
    val, _ = await axi.read(TELEM_STATUS)
    assert not (val & TELEM_STATUS_ALARM), "clearing alarm_en must gate the live level off"


@cocotb.test(skip=NO_RTL)
async def test_ro_and_unmapped_offsets_house_convention(dut):
    """What this proves: writes to RO offsets are accepted (BRESP=OKAY, no
    effect) and — critically — do NOT clear the sticky i2c_err (only a
    CTRL write does); unmapped offsets inside the 3-bit decode window
    (0x14/0x18/0x1C) read 0. NOTE: offsets >= 0x20 alias mod 0x20 (only
    address bits [4:2] are decoded — house convention shared by dfx_ctl/
    dut_clkrst/board_gpio); the README's "unmapped offsets read 0" is
    accurate only within the 32-byte window. Documented in dut_notes.md;
    deliberately not asserted as a requirement either way here beyond
    pinning the in-window behaviour."""
    axi = await _bring_up(dut)
    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)
    await _inject_sample(dut, bus_mv=1200, curr_ua=1000, power_mw=1)

    # Arm the sticky error.
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 0

    # RO write: accepted, readings unchanged, sticky err NOT cleared.
    resp = await axi.write(TELEM_BUS_MV, 0xDEAD_BEEF)
    assert resp == 0, "RO write must still get BRESP=OKAY"
    assert await _read_triple(axi) == (1200, 1000, 1)
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, (
        "a write to an RO offset must NOT clear i2c_err (CTRL-write-only clear)")
    resp = await axi.write(TELEM_STATUS, 0xFFFF_FFFF)
    assert resp == 0
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, "a STATUS write must not clear i2c_err either"

    # Unmapped offsets inside the decode window read 0.
    for off in (0x14, 0x18, 0x1C):
        val, _ = await axi.read(off)
        assert val == 0, f"unmapped offset {off:#x} must read 0, got {val:#x}"


@cocotb.test(skip=NO_RTL)
async def test_wstrb_lane0_gates_ctrl_write(dut):
    """WSTRB byte-lane test (previously untested on EVERY AXI slave — the BFM
    only ever drove strb=0xF). CTRL's write (`ctrl_wr`) is gated on wstrb[0];
    a write with lane 0 disabled must be ignored — it must neither change
    enable/alarm_en NOR clear the sticky i2c_err (that clear rides ctrl_wr
    too). A slave that ignores WSTRB fails all three."""
    axi = await _bring_up(dut)

    await axi.write_bytes(TELEM_CTRL, TELEM_CTRL_ENABLE | TELEM_CTRL_ALARM_EN, 0xF)
    val, _ = await axi.read(TELEM_CTRL)
    assert val == (TELEM_CTRL_ENABLE | TELEM_CTRL_ALARM_EN), f"seed {val:#x}"
    assert int(dut.ina228_enable_o.value) == 1

    # Arm the sticky i2c_err so we can watch the lane-0-disabled write NOT clear it.
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 0
    await RisingEdge(dut.s_axi_aclk)
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, "i2c_err must be armed"

    # Lane-0-disabled CTRL write: ignored everywhere.
    await axi.write_bytes(TELEM_CTRL, 0x0, 0x2)
    val, _ = await axi.read(TELEM_CTRL)
    assert val == (TELEM_CTRL_ENABLE | TELEM_CTRL_ALARM_EN), (
        "a CTRL write with wstrb lane0 disabled must not change CTRL (WSTRB ignored?)")
    assert int(dut.ina228_enable_o.value) == 1
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, "a lane-0-disabled CTRL write must NOT clear sticky i2c_err"

    # Lane-0-enabled CTRL write lands: clears CTRL and the sticky error.
    await axi.write_bytes(TELEM_CTRL, 0x0, 0x1)
    val, _ = await axi.read(TELEM_CTRL)
    assert val == 0, "a lane-0-enabled CTRL write must land"
    val, _ = await axi.read(TELEM_STATUS)
    assert not (val & TELEM_STATUS_I2C_ERR), "a lane-0-enabled CTRL write clears i2c_err"


@cocotb.test(skip=NO_RTL)
async def test_write_at_0x20_must_not_clear_i2c_err(dut):
    """REGRESSION GUARD for a destructive-alias footgun, now FIXED (2026-07-09).

    TELEM used to decode only awaddr[4:2], so a write of 0x20 aliased CTRL
    (0x00): it cleared the sticky i2c_err (and rewrote CTRL) even though 0x20 is
    not a named register. A stray write anywhere in this 64 KB page silently
    lost a latched I2C error. This test asserted that SAFE property as an
    `expect_fail=True` xfail, with the instruction "when fixed, drop it".

    The TELEM decode has since been widened to the full local word address (the
    same fix applied across every shell IP block after poc/systemrdl's
    decode-equivalence bench compared each against a generated full-address
    decode). The xfail is dropped; this is now a normal green guard."""
    axi = await _bring_up(dut)
    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)

    # Arm the sticky i2c_err.
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.ina228_i2c_err_i.value = 0
    await RisingEdge(dut.s_axi_aclk)
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, "precondition: i2c_err must be armed"

    # SAFE property: a write at the aliased offset 0x20 must NOT clear i2c_err.
    # (It will — 0x20 aliases CTRL and ctrl_wr clears the sticky bit.)
    await axi.write(0x20, 0x0)
    val, _ = await axi.read(TELEM_STATUS)
    assert val & TELEM_STATUS_I2C_ERR, (
        "a write at 0x20 must not clear sticky i2c_err (SAFE property)")


@cocotb.test(skip=NO_RTL)
async def test_random_aw_en_corners(dut):
    """Constrained-random AXI4-Lite aw_en corner coverage (A5 random wave):
    randomized AW/W arrival order (same / AW-first / W-first), zero-gap
    back-to-back writes, read-during-write, and randomized WSTRB. Write
    targets stay inside the mapped 0x00..0x10 window (CTRL rw + RO offsets,
    all accepted BRESP=OKAY) — deliberately NOT the 0x20 destructive alias
    (its own XFAIL test owns that). Fills the shared write-FSM condition-
    coverage hole; the bound AXI protocol SVA validates every handshake."""
    await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "telem")
    m = RandomAxiMaster.from_dut(dut)
    waddrs = [TELEM_CTRL, TELEM_BUS_MV, TELEM_STATUS]
    raddrs = [TELEM_CTRL, TELEM_BUS_MV, TELEM_CURR_UA, TELEM_POWER_MW, TELEM_STATUS]
    tally = await random_axi_burst(m, rng, waddrs, raddrs, n=48)
    dut._log.info(f"[telem random] orderings/paths exercised: {tally}")


@cocotb.test(skip=NO_RTL)
async def test_random_wide_sample_toggle(dut):
    """Toggle-coverage attack on the wide 32-bit sample registers (A5 random
    wave). Baseline toggle coverage was ~39% here — the fixed samples
    (1195, 250000, 299 ...) drove only a few bits of the 32-bit BUS_MV /
    CURR_UA / POWER_MW datapaths. This injects fully-random 32-bit readings
    through the `ina228_*` seam over many iterations so every bit of every
    wide sample register toggles, and reads each triple back to prove the
    atomic latch carries the full 32-bit value verbatim. Seed logged."""
    axi = await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "telem")
    await axi.write(TELEM_CTRL, TELEM_CTRL_ENABLE)
    for i in range(40):
        bus = rng.getrandbits(32)
        curr = rng.getrandbits(32)
        powr = rng.getrandbits(32)
        await _inject_sample(dut, bus_mv=bus, curr_ua=curr, power_mw=powr)
        got = await _read_triple(axi)
        assert got == (bus, curr, powr), (
            f"iter {i}: random sample readback mismatch: "
            f"got {tuple(hex(x) for x in got)} "
            f"exp {(hex(bus), hex(curr), hex(powr))}")
