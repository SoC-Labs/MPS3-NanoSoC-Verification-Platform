"""tests/clcd/test_clcd.py — the `clcd` AXI4-Lite -> 8080 byte-streaming bus
master (shell-regmap.md v0.4 CLCD @ 0x44AC_0000; port contract
`fpga/shell/ip/clcd/README.md`, FROZEN).

The block is protocol-agnostic: firmware pushes {RS, byte} pairs into a FIFO via
CMD/DATA and a bus FSM pops them and drives CS/RS/WR/PD[7:0] with the HX8347-D's
8080 setup/hold timing (parameterised in `s_axi_aclk` cycles). This bench proves
the *block* — not the panel. Per plan §11 and the README init-table seam, the
bench uses its own SYNTHETIC vectors and deliberately does NOT consume the real
`firmware/clcd/hx8347_init.h` table: proving the block streams an arbitrary
sequence faithfully is what a green result should mean; benching the real table
would let a wrong table look green (see dut_notes.md).

The five checks (plan §11):
  1. Init sequence   — a synthetic {RS,byte} vector pushed through CMD/DATA is
                       decoded off the pads by the panel model in exact order.
  2. Bus protocol    — CS asserted around each cycle, PD stable at the WR rising
                       edge, RS per CMD/DATA, and the cycle honours TIMING's
                       wr_lo/wr_hi/cs_setup (>=2 distinct TIMING values).
  3. CTRL pads       — backlight -> clcd_bl_o, reset_n -> clcd_rst_n_o, and
                       fifo_reset self-clears + empties the FIFO.
  4. FIFO backpressure, zero loss — fill to STATUS.fifo_full, fifo_level tracks,
                       writes while full are DROPPED (BRESP=OKAY, not stalled),
                       and every byte pushed while !full arrives in order.
  5. (decode width lives in tests/csr_decode_width/test_decode_width_clcd.py)

READ path (READ @ 0x10): this module elaborates the DEFAULT build (READ_PATH=0,
what ships write-only) and checks READ reads 0 with clcd_rd_n_o held deasserted.
The read-back path itself (READ_PATH=1) is exercised by the SEPARATE elaboration
`test_clcd_readpath.py` (`make READP=1`) — it needs a different parameter value,
and cocotb/VCS bake one parameter per simv. Per the v0.4 contract update, a
panel read is armed by writing CTRL.read_start (bit 4, self-clearing); READ has
**no read side effect** — that module pins it.
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
from clcd_panel_model import PanelBusModel, RS_CMD, RS_DATA

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "clcd")
NO_RTL = not rtl_ready(_RTL_DIR, ["clcd.sv"])

# ---- CLCD register map (shell-regmap.md v0.4 @ 0x44AC_0000) --------------- #
CLCD_CTRL = 0x00
CLCD_CMD = 0x04
CLCD_DATA = 0x08
CLCD_STATUS = 0x0C
CLCD_READ = 0x10
CLCD_TIMING = 0x14

CTRL_ENABLE = 1 << 0
CTRL_BACKLIGHT = 1 << 1     # -> clcd_bl_o
CTRL_RESET_N = 1 << 2       # -> clcd_rst_n_o (0 = panel held in reset)
CTRL_FIFO_RESET = 1 << 3    # self-clearing

STATUS_FIFO_FULL = 1 << 0
STATUS_FIFO_EMPTY = 1 << 1
STATUS_BUSY = 1 << 2
STATUS_LEVEL_SHIFT = 8
STATUS_LEVEL_MASK = 0xFF << STATUS_LEVEL_SHIFT

READ_VALID = 1 << 8

# CTRL value that puts the block in normal streaming operation: FSM enabled and
# the panel released from reset (so it is legal to drive bus cycles at it).
CTRL_RUN = CTRL_ENABLE | CTRL_RESET_N


def _timing(wr_lo, wr_hi, cs_setup):
    """Pack the TIMING register [7:0] wr_lo / [15:8] wr_hi / [23:16] cs_setup."""
    assert 0 <= wr_lo <= 0xFF and 0 <= wr_hi <= 0xFF and 0 <= cs_setup <= 0xFF
    return (cs_setup << 16) | (wr_hi << 8) | wr_lo


def _level(status):
    return (status & STATUS_LEVEL_MASK) >> STATUS_LEVEL_SHIFT


async def _reset(dut):
    """Pulse aresetn low->high, leaving CTRL at its reset value (0x0: FSM off,
    panel held in reset, backlight off) and the panel model quiescent."""
    dut.s_axi_aresetn.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    # Idle every input. READ_PATH defaults to 0 (write-only), so tie the read
    # data bus to 0 per the contract ("tie 8'h00 if READ_PATH=0").
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


async def _push_pair(axi, rs, byte):
    off = CLCD_CMD if rs == RS_CMD else CLCD_DATA
    return await axi.write(off, byte & 0xFF)


async def _wait_drained(dut, axi, max_reads=4000):
    """Poll STATUS until the FIFO is empty and the FSM is idle, then let the last
    cycle's WR-high recovery + CS deassert settle so the trace is complete."""
    for _ in range(max_reads):
        s, _ = await axi.read(CLCD_STATUS)
        if (s & STATUS_FIFO_EMPTY) and not (s & STATUS_BUSY):
            await ClockCycles(dut.s_axi_aclk, 8)
            return
        await ClockCycles(dut.s_axi_aclk, 2)
    raise AssertionError("FIFO never drained / FSM never went idle")


async def _stream(dut, axi, model, pairs, timing=None):
    """Set (optional) TIMING, ensure the FSM is running, push `pairs` through
    CMD/DATA, wait for a full drain, and return the decoded strobe list."""
    if timing is not None:
        await axi.write(CLCD_TIMING, timing)
    await axi.write(CLCD_CTRL, CTRL_RUN)
    await _wait_drained(dut, axi)          # start from a known-idle bus (CS high)
    model.clear()
    for rs, byte in pairs:
        await _push_pair(axi, rs, byte)
    await _wait_drained(dut, axi)
    return list(model.strobes)


async def _timed_burst(dut, axi, model, timing, n=4, first=0x50, rs=RS_CMD):
    """Measure one clean, back-to-back burst at a given TIMING. Park the FSM
    (enable=0) and flush, queue `n` bytes, then release: the queued bytes drain
    with no inter-byte idle, so every strobe's WR-low width, CS-setup interval
    and strobe-to-strobe period is deterministic and identical — exactly what a
    timing measurement needs."""
    await axi.write(CLCD_CTRL, CTRL_FIFO_RESET)   # park (enable=0) + flush
    await axi.write(CLCD_TIMING, timing)
    await ClockCycles(dut.s_axi_aclk, 2)
    model.clear()
    for i in range(n):
        await _push_pair(axi, rs, (first + i) & 0xFF)
    await axi.write(CLCD_CTRL, CTRL_RUN)          # release -> drain back-to-back
    await _wait_drained(dut, axi)
    return list(model.strobes)


# ============================================================================ #
# 1. INIT SEQUENCE — an arbitrary {RS,byte} vector streams faithfully & in order
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_init_sequence_streams_synthetic_vector_in_order(dut):
    """A synthetic init-shaped sequence (mixed CMD/DATA, arbitrary bytes) pushed
    through CMD/DATA must appear on the 8080 bus decoded in EXACTLY that order,
    with the right RS per byte. This is the block's core promise; the values are
    ours, not the real panel table (README init-table seam / plan §11)."""
    axi, model = await _bring_up(dut)

    # Synthetic, panel-agnostic: looks like an init table (a command followed by
    # its data payload bytes) but the values are invented for the bench.
    vector = [
        (RS_CMD, 0x2E), (RS_DATA, 0x89),
        (RS_CMD, 0x2B), (RS_DATA, 0x00), (RS_DATA, 0xFF),
        (RS_CMD, 0x00), (RS_DATA, 0xA5), (RS_DATA, 0x5A), (RS_DATA, 0x12),
        (RS_CMD, 0x22),                    # e.g. a GRAM-write-shaped command
        (RS_DATA, 0xDE), (RS_DATA, 0xAD), (RS_DATA, 0xBE), (RS_DATA, 0xEF),
    ]

    strobes = await _stream(dut, axi, model, vector)

    assert len(strobes) == len(vector), (
        f"emitted {len(strobes)} bus cycles, pushed {len(vector)} bytes: "
        f"{model.sequence}")
    assert model.sequence == vector, (
        "decoded {RS,byte} stream does not match the pushed vector.\n"
        f"  pushed : {vector}\n  decoded: {model.sequence}")

    # Every latched cycle must be a well-formed write: CS asserted, data stable,
    # and (READ_PATH=0) the read strobe never asserted.
    for i, s in enumerate(strobes):
        assert s.cs_low_throughout, f"cycle {i}: CS_n not asserted across the WR pulse"
        assert s.pd_stable, f"cycle {i}: clcd_pd_o changed during the WR low pulse"
        assert s.rd_high, f"cycle {i}: clcd_rd_n_o asserted on a write (READ_PATH=0)"


# ============================================================================ #
# 2. BUS PROTOCOL — CS framing, PD stable at WR rising edge, RS select, timing
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_rs_select_and_pd_stability(dut):
    """RS=0 for CMD writes, RS=1 for DATA writes; the data bus is stable through
    the WR rising edge (the panel's latch instant) on every cycle."""
    axi, model = await _bring_up(dut)

    pairs = [(RS_CMD, 0x10 + i) if i % 2 == 0 else (RS_DATA, 0xA0 + i)
             for i in range(8)]
    strobes = await _stream(dut, axi, model, pairs)

    assert len(strobes) == len(pairs)
    for i, (s, (rs, byte)) in enumerate(zip(strobes, pairs)):
        assert s.rs == rs, (
            f"cycle {i}: RS={s.rs}, expected {rs} "
            f"({'CMD' if rs == RS_CMD else 'DATA'})")
        assert s.byte == byte, f"cycle {i}: latched 0x{s.byte:02X}, pushed 0x{byte:02X}"
        assert s.pd_stable, f"cycle {i}: PD not stable through the WR pulse"
        assert s.cs_low_throughout, f"cycle {i}: CS_n deasserted mid-cycle"


@cocotb.test(skip=NO_RTL)
async def test_timing_register_is_honoured_in_aclk_cycles(dut):
    """The WR-low width, the CS-setup interval, and the strobe-to-strobe period
    each track their TIMING field in whole s_axi_aclk cycles. Tested at FOUR
    distinct TIMING values (a base + one per field), asserting BOTH the absolute
    width AND — the field-isolating, convention-robust proof — that changing
    exactly one field moves exactly the matching interval by exactly that delta.
    All measurements come from clean back-to-back bursts (see _timed_burst)."""
    axi, model = await _bring_up(dut)

    wr_lo0, wr_hi0, cs0 = 4, 4, 3
    base = await _timed_burst(dut, axi, model, _timing(wr_lo0, wr_hi0, cs0))
    wl0 = _consistent([s.wr_low_cycles for s in base], "wr_low @base")
    cs_meas0 = _consistent([s.cs_setup_cycles for s in base], "cs_setup @base")
    per0 = _consistent(_periods(base), "period @base")
    dut._log.info(f"[clcd timing] base (wr_lo={wr_lo0} wr_hi={wr_hi0} cs_setup={cs0}): "
                  f"wr_low={wl0} cs_setup={cs_meas0} period={per0} aclk cyc")

    # WR-low width == wr_lo, and +5 in the register adds exactly 5 aclk cycles.
    assert wl0 == wr_lo0, f"WR-low width {wl0} != wr_lo {wr_lo0}"
    s_wl = await _timed_burst(dut, axi, model, _timing(wr_lo0 + 5, wr_hi0, cs0))
    wl1 = _consistent([s.wr_low_cycles for s in s_wl], "wr_low @wr_lo+5")
    assert wl1 == wr_lo0 + 5, f"WR-low width {wl1} != wr_lo {wr_lo0 + 5}"
    assert wl1 - wl0 == 5, (
        f"WR-low width did not track wr_lo: +5 gave {wl1 - wl0} extra aclk cycles")

    # CS-setup interval == cs_setup, and +4 in the register adds exactly 4.
    assert cs_meas0 == cs0, f"CS-setup {cs_meas0} != cs_setup {cs0}"
    s_cs = await _timed_burst(dut, axi, model, _timing(wr_lo0, wr_hi0, cs0 + 4))
    cs_meas1 = _consistent([s.cs_setup_cycles for s in s_cs], "cs_setup @cs+4")
    assert cs_meas1 == cs0 + 4, f"CS-setup {cs_meas1} != cs_setup {cs0 + 4}"
    assert cs_meas1 - cs_meas0 == 4, (
        f"CS-setup did not track cs_setup: +4 gave {cs_meas1 - cs_meas0} extra cycles")

    # Strobe period responds to wr_hi: +6 in the register adds exactly 6 cycles.
    s_wh = await _timed_burst(dut, axi, model, _timing(wr_lo0, wr_hi0 + 6, cs0))
    per1 = _consistent(_periods(s_wh), "period @wr_hi+6")
    dut._log.info(f"[clcd timing] period: wr_hi={wr_hi0}->{per0}, wr_hi={wr_hi0+6}->{per1} cyc")
    assert per1 - per0 == 6, (
        f"strobe period did not track wr_hi: +6 gave {per1 - per0} extra aclk cycles")


# ============================================================================ #
# 3. CTRL pads — backlight, reset_n, and fifo_reset (self-clearing)
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_ctrl_bits_reach_backlight_and_reset_pads(dut):
    """CTRL.backlight drives clcd_bl_o and CTRL.reset_n drives clcd_rst_n_o.
    Reset value is 0x0: backlight off, panel HELD in reset — matching the legacy
    tie-off (README: 'panel dark and held in reset')."""
    axi, model = await _bring_up(dut)

    # Reset state.
    val, _ = await axi.read(CLCD_CTRL)
    assert val == 0, f"CTRL must reset to 0, got {val:#x}"
    assert int(dut.clcd_bl_o.value) == 0, "backlight must be OFF at reset"
    assert int(dut.clcd_rst_n_o.value) == 0, "panel must be HELD IN RESET at reset (rst_n=0)"

    # Backlight on (leave panel in reset): only clcd_bl_o moves.
    await axi.write(CLCD_CTRL, CTRL_BACKLIGHT)
    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.clcd_bl_o.value) == 1, "CTRL.backlight=1 must drive clcd_bl_o=1"
    assert int(dut.clcd_rst_n_o.value) == 0, "reset_n=0 must keep clcd_rst_n_o=0"

    # Release the panel from reset too.
    await axi.write(CLCD_CTRL, CTRL_BACKLIGHT | CTRL_RESET_N)
    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.clcd_rst_n_o.value) == 1, "CTRL.reset_n=1 must drive clcd_rst_n_o=1"
    assert int(dut.clcd_bl_o.value) == 1

    # Back to dark + held-in-reset.
    await axi.write(CLCD_CTRL, 0)
    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.clcd_bl_o.value) == 0
    assert int(dut.clcd_rst_n_o.value) == 0


@cocotb.test(skip=NO_RTL)
async def test_fifo_reset_self_clears_and_empties_fifo(dut):
    """CTRL.fifo_reset empties the FIFO and reads back 0 (self-clearing).

    Filled with the FSM PARKED (CTRL=0: disabled + panel held in reset), so the
    bytes sit in the FIFO instead of draining — this both isolates fifo_reset and
    asserts the parked-does-not-drain property the backpressure test relies on."""
    axi, model = await _bring_up(dut)

    # Park: reset value already has enable=0 & reset_n=0. Push several bytes.
    n = 6
    for i in range(n):
        await _push_pair(axi, RS_CMD if i % 2 == 0 else RS_DATA, 0x40 + i)
    await ClockCycles(dut.s_axi_aclk, 4)

    s, _ = await axi.read(CLCD_STATUS)
    assert _level(s) == n, (
        f"fifo_level={_level(s)} after pushing {n} bytes while parked "
        f"(parked FSM must not drain — see dut_notes.md)")
    assert not (s & STATUS_FIFO_EMPTY), "FIFO must be non-empty after pushes"
    assert len(model.strobes) == 0, (
        "a parked FSM (enable=0, panel in reset) drove bus cycles — "
        "the backpressure/fifo_reset tests assume parked == no drain")

    # fifo_reset.
    await axi.write(CLCD_CTRL, CTRL_FIFO_RESET)
    await ClockCycles(dut.s_axi_aclk, 2)

    ctrl, _ = await axi.read(CLCD_CTRL)
    assert not (ctrl & CTRL_FIFO_RESET), "fifo_reset must self-clear (read back 0)"

    s, _ = await axi.read(CLCD_STATUS)
    assert s & STATUS_FIFO_EMPTY, "fifo_reset must empty the FIFO (fifo_empty=1)"
    assert _level(s) == 0, f"fifo_reset must zero fifo_level, got {_level(s)}"
    assert not (s & STATUS_FIFO_FULL)


# ============================================================================ #
# 4. FIFO BACKPRESSURE, ZERO LOSS — the check that protects the superloop
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_fifo_backpressure_drops_not_stalls_and_loses_nothing(dut):
    """Fill until STATUS.fifo_full; assert fifo_level tracks; assert writes while
    full are DROPPED (awready/bvalid still complete, BRESP=OKAY) — never stalled,
    because clcd_poll() shares the superloop with the lwIP timers; then release
    and prove every byte pushed while !full arrives on the panel bus, in order,
    none lost, none duplicated.

    'Panel-side stall' is modelled by parking the FSM (CTRL=0): the 8080 pads
    carry no ready/backpressure to the master, so the only faithful stall is to
    stop the FSM popping. The fifo_reset test asserts parked == no drain."""
    axi, model = await _bring_up(dut)

    expected = []          # bytes accepted while !full -> must appear on the bus
    push_i = 0

    def next_pair():
        nonlocal push_i
        rs = RS_CMD if (push_i % 3 == 0) else RS_DATA
        byte = (0x01 + push_i) & 0xFF
        push_i += 1
        return rs, byte

    # ---- Fill (FSM parked: CTRL=0) until STATUS.fifo_full ----------------- #
    prev_level = -1
    guard = 0
    max_push = 4096
    while True:
        s, _ = await axi.read(CLCD_STATUS)
        lvl = _level(s)
        # fifo_level must be monotonic non-decreasing while we only add.
        assert lvl >= prev_level, f"fifo_level went backwards {prev_level}->{lvl} while filling"
        prev_level = lvl
        if s & STATUS_FIFO_FULL:
            break
        assert not (s & STATUS_FIFO_EMPTY) or lvl == 0
        rs, byte = next_pair()
        resp = await _push_pair(axi, rs, byte)
        assert resp == 0, f"a !full CMD/DATA write got BRESP={resp:#x}, expected OKAY"
        expected.append((rs, byte))
        guard += 1
        assert guard < max_push, "FIFO never reported full — is fifo_full wired?"

    full_level = prev_level
    assert full_level >= 1, f"fifo_full asserted at level {full_level}"
    dut._log.info(f"[clcd bp] FIFO full at level {full_level}; "
                  f"{len(expected)} bytes accepted while !full")

    # ---- Drop test: writes while full are accepted (OKAY) but NOT enqueued -#
    drops = 8
    for k in range(drops):
        s, _ = await axi.read(CLCD_STATUS)
        assert s & STATUS_FIFO_FULL, "FIFO drained while parked — drop test invalid"
        lvl_before = _level(s)
        rs = RS_DATA if k % 2 else RS_CMD
        resp = await axi.write(CLCD_DATA if rs == RS_DATA else CLCD_CMD, 0xF0 + k)
        assert resp == 0, (
            f"a write while full got BRESP={resp:#x}: the slave STALLED instead "
            "of dropping — this can wedge the MicroBlaze data bus / drop lwIP")
        s2, _ = await axi.read(CLCD_STATUS)
        assert _level(s2) == lvl_before, (
            f"a dropped write changed fifo_level {lvl_before}->{_level(s2)}: it "
            "was enqueued, not dropped")

    # ---- Release the stall: run + fast timing, drain, collect ------------- #
    model.clear()
    await axi.write(CLCD_TIMING, _timing(wr_lo=1, wr_hi=1, cs_setup=1))
    await axi.write(CLCD_CTRL, CTRL_RUN)
    await _wait_drained(dut, axi)

    got = model.sequence
    assert got == expected, (
        "zero-loss violated across the panel-side stall.\n"
        f"  pushed while !full ({len(expected)}): {expected}\n"
        f"  arrived on the bus  ({len(got)}): {got}")
    # Belt-and-braces: no duplicates, none lost (order already checked above).
    assert len(got) == len(expected), (
        f"count mismatch: {len(got)} bus cycles for {len(expected)} accepted bytes")


# ============================================================================ #
# 5. READ path defaults off (READ_PATH=0): READ reads 0, RD strobe never asserts
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_read_register_and_rd_strobe_default_off(dut):
    """With the default build (READ_PATH=0) the block is write-only: the READ
    register reads 0 and clcd_rd_n_o is held deasserted through a stream (README:
    'READ reads 0 and clcd_rd_n_o is held deasserted')."""
    axi, model = await _bring_up(dut)

    rd, _ = await axi.read(CLCD_READ)
    assert rd == 0, f"READ must read 0 when READ_PATH=0, got {rd:#x}"
    assert not (rd & READ_VALID), "READ.valid must be 0 when READ_PATH=0"
    assert int(dut.clcd_rd_n_o.value) == 1, "clcd_rd_n_o must be deasserted (1) at idle"

    strobes = await _stream(dut, axi, model, [(RS_CMD, 0x01), (RS_DATA, 0x02)])
    for i, s in enumerate(strobes):
        assert s.rd_high, f"cycle {i}: clcd_rd_n_o asserted during a write (READ_PATH=0)"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _consistent(vals, what):
    """Assert every measured value is equal (the FSM is deterministic per TIMING)
    and return the common value."""
    assert vals, f"{what}: no measurements"
    assert all(v == vals[0] for v in vals), f"{what}: not constant across strobes: {vals}"
    return vals[0]


def _periods(strobes):
    """WR-rise -> next WR-rise spacing (aclk cycles) within a burst."""
    return [strobes[i + 1].n_rise - strobes[i].n_rise for i in range(len(strobes) - 1)]
