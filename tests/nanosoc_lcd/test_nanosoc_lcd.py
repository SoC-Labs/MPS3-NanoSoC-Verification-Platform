"""tests/nanosoc_lcd/test_nanosoc_lcd.py — the DUT-side display accelerator:
`ahb_clcd` inside `nanosoc_exp_socket`, the reference block filling nanosoc's
`exp_*` hole at 0x6000_0000.

Contract (FROZEN, and what this bench is written against — NOT the RTL):
  * `fpga/rp/nanosoc_exp/README.md` v1.0 — the socket port list (§2), AHB-Lite
    (§3), THE ONE RULE (§4), the register map and reference block (§7).
  * `docs/contracts/dut-display-tunnel.md` — the wire encoding out to the shell.
  * `docs/CLCD_PANEL_FACTS.md` — the panel that is actually lit.

This is the sibling of `tests/clcd/test_clcd.py` (the shell's AXI4-Lite CLCD) and
deliberately mirrors its five-test shape. The two blocks share `clcd_core.sv` —
the FIFO + 8080 strobe FSM — so this bench is what shows the DUT-side block
INHERITS the shell block's proof across a different bus front end.

Three things make this bench different from tests/clcd's, and each is a place a
student will trip:

  1. POLARITY. The socket's display pins are ACTIVE-HIGH; the panel's pads are
     ACTIVE-LOW. `panel_shim.PanelPads` does that one inversion so the panel
     model (`tests/clcd/clcd_panel_model.py`, reused VERBATIM) sees what the real
     panel sees. The RTL must NOT invert — see panel_shim.py's header, and §6.
  2. THE BUS. AHB-Lite, not AXI4-Lite: pipelined, and the slave's `hreadyout` is
     load-bearing for the whole SoC. Test 5 and test 7 exist for that alone.
  3. NO PANEL READ. No `lcd_rd`, no `lcd_pd_oe` (§2) — the panel is write-only
     in this platform. So there is no READ register and no read-path variant.

The tests:
  1. Streaming fidelity  — a synthetic {RS,byte} vector via CMD/DATA arrives at
                           the panel in order, intact.
  2. RS select + PD stability through the WR edge (where the panel latches).
  3. TIMING honoured     — cycle counts in hclk cycles, field-isolated.
  4. CTRL -> pads        — enable/lcd_en/lcd_req/lcd_busy, self-clearing
                           fifo_reset, and inert display pins in reset.
  5. FIFO BACKPRESSURE   — *** THE CRITICAL ONE *** pushes on a full FIFO are
                           DROPPED and the AHB bus is NEVER STALLED; zero loss
                           once it drains.
  6. NO READ SIDE EFFECTS — read every register; the panel bus must not move.
  7. AHB-LITE LEGALITY   — back-to-back (pipelined) transfers, plus the bound
                           SVA checker (bind_nanosoc_lcd.sv).

WHY A SYNTHETIC VECTOR, NOT THE REAL HX8347 INIT TABLE — this is inherited
policy, not laziness (tests/clcd/dut_notes.md:39-49). The block is
protocol-agnostic: it knows about 8080 bus cycles and nothing about Himax
registers. Proving it streams an ARBITRARY {RS,byte} sequence faithfully is what
a green result should mean. Benching the real table would let a WRONG table look
green — "the block streams what firmware pushed" and "those are the right bytes
for this panel" are separate questions, settled in different places (sim vs. the
board).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_TESTS, "common"))
sys.path.insert(0, os.path.join(_TESTS, "clcd"))     # the panel model — read-only reuse
sys.path.insert(0, _HERE)

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, with_timeout

# SimTimeoutError MOVED between cocotb majors: cocotb.result (1.x) ->
# cocotb.triggers (2.x). This repo runs BOTH: `make check`'s pytest collection
# uses cocotb 1.7.2 under the system python3.8, while the VCS benches use
# cocotb 2.0.1 under miniconda python3.10. Importing from only the 2.x location
# made `make check` stage 3 fail at COLLECTION -- a hard error that aborted the
# whole pytest stage -- on a bench that runs fine in simulation. Import
# version-tolerantly so the gate is usable under either.
try:
    from cocotb.triggers import SimTimeoutError    # cocotb >= 2.0
except ImportError:                                # cocotb 1.x
    from cocotb.result import SimTimeoutError

from dut_presence import rtl_ready
from clcd_panel_model import PanelBusModel, RS_CMD, RS_DATA   # tests/clcd/ — AS-IS
from panel_shim import PanelPads

# --------------------------------------------------------------------------- #
# Readiness gating. Both halves of this bench's dependencies can be absent:
#   * the RTL      (fpga/rp/nanosoc_exp/, owned by W2-D)
#   * the AHB BFM  (tests/common/ahb_lite.py, owned by W2-B)
# Either missing => SKIP LOUDLY rather than fail cryptically or hang, per the
# tests/common/dut_presence.py convention.
# --------------------------------------------------------------------------- #
_RTL_DIR = os.environ.get(
    "NANOSOC_LCD_RTL_DIR",
    os.path.join(_TESTS, "..", "fpga", "rp", "nanosoc_exp"),
)
# Presence gate is the two files that live in THIS dir. clcd_core.sv is a
# compile-time dep from fpga/shell/ip/clcd (added to VERILOG_SOURCES by the
# Makefile), not a file here -- listing it wrongly SKIPped the whole bench.
_RTL_FILES = ["nanosoc_exp_socket.sv", "ahb_clcd.sv"]
NO_RTL = not rtl_ready(_RTL_DIR, _RTL_FILES)

try:
    from ahb_lite import AhbLiteMaster
    NO_BFM = False
except ImportError:                                  # pragma: no cover
    AhbLiteMaster = None
    NO_BFM = True

SKIP = NO_RTL or NO_BFM
_SKIP_WHY = (
    ("RTL absent under %s (need %s)" % (_RTL_DIR, ", ".join(_RTL_FILES)) if NO_RTL else "")
    + ("; " if NO_RTL and NO_BFM else "")
    + ("tests/common/ahb_lite.py (AhbLiteMaster) absent" if NO_BFM else "")
)
if SKIP:
    print("[nanosoc_lcd] SKIPPING ALL TESTS -- " + _SKIP_WHY)

# ---- ahb_clcd register map (socket README §7, base 0x6000_0000) ------------ #
# Decode only the low bits (§5): the bench drives bare offsets, which is what the
# socket sees once nanosoc's matrix has decoded the 0x6 region and asserted hsel.
LCD_CTRL = 0x00
LCD_CMD = 0x04      # W: [7:0] -> push {RS=0, byte}
LCD_DATA = 0x08     # W: [7:0] -> push {RS=1, byte}
LCD_STATUS = 0x0C   # RO
LCD_TIMING = 0x10   # RW  (NB: 0x10, not the shell block's 0x14 — no READ reg)

CTRL_ENABLE = 1 << 0
CTRL_FIFO_RESET = 1 << 1     # self-clearing
CTRL_REQ = 1 << 2            # -> lcd_req ("I would like the panel, please")

STATUS_FIFO_FULL = 1 << 0
STATUS_FIFO_EMPTY = 1 << 1
STATUS_BUSY = 1 << 2
STATUS_LEVEL_SHIFT = 8
STATUS_LEVEL_MASK = 0xFF << STATUS_LEVEL_SHIFT

# Every register offset in the block, for the read-sweep (test 6). The block
# aliases the rest of its 256 MB region, so a handful of unmapped offsets are
# swept too — they must read 0 and, above all, drive nothing at the panel.
ALL_OFFSETS = [LCD_CTRL, LCD_CMD, LCD_DATA, LCD_STATUS, LCD_TIMING,
               0x14, 0x18, 0x1C, 0x20, 0x3C, 0x40, 0xFC]

# CTRL value for normal streaming operation. Unlike the shell block there is no
# backlight and no panel reset_n here — the KVM owns BL/RST (tunnel contract §1).
CTRL_RUN = CTRL_ENABLE

HCLK_NS = 20        # 50 MHz — the DUT clock as shipped (§2, §6 timing floor)


def _timing(wr_lo, wr_hi, cs_setup):
    """Pack TIMING: [7:0] wr_lo, [15:8] wr_hi, [23:16] cs_setup — in hclk cycles."""
    assert 0 <= wr_lo <= 0xFF and 0 <= wr_hi <= 0xFF and 0 <= cs_setup <= 0xFF
    return (cs_setup << 16) | (wr_hi << 8) | wr_lo


def _level(status):
    return (status & STATUS_LEVEL_MASK) >> STATUS_LEVEL_SHIFT


# --------------------------------------------------------------------------- #
# HREADYOUT monitor — the guard that makes test 5 mean something.
#
# "The AHB bus is never stalled" is a property of every cycle, not of a return
# value, so it needs a continuous watcher. This samples hreadyout once per hclk
# posedge (ReadOnly) and records the longest run of consecutive LOW cycles. The
# contract's reference block is zero-wait-state ("hreadyout is 1'b1, always" —
# §7), so the expected answer is a hard ZERO.
#
# It is belt-and-braces with the bound SVA (bind_nanosoc_lcd.sv A_LIVENESS): the
# SVA catches an UNBOUNDED stall, this catches ANY stall and reports how long.
# --------------------------------------------------------------------------- #
class ReadyMonitor:
    def __init__(self, dut):
        self.dut = dut
        self._stop = False
        self.max_low_run = 0
        self.total_low = 0
        self._run = 0
        self.window_low = 0     # low cycles since the last arm()

    def arm(self):
        self.window_low = 0

    def stop(self):
        self._stop = True

    async def run(self):
        while not self._stop:
            await RisingEdge(self.dut.hclk)
            await ReadOnly()
            if int(self.dut.hresetn.value) == 0:
                self._run = 0
                continue
            try:
                rdy = int(self.dut.hreadyout.value)
            except ValueError:
                continue                       # x/z — A_READYOUT_KNOWN owns that
            if rdy == 0:
                self._run += 1
                self.total_low += 1
                self.window_low += 1
                self.max_low_run = max(self.max_low_run, self._run)
            else:
                self._run = 0


async def _bring_up(dut):
    """Clock, reset, start the panel model (through the shim) and the hreadyout
    monitor, then bind W2-B's AhbLiteMaster. Returns (ahb, model, rdy).

    HREADY binding — the one thing to get right for a single-slave AHB-Lite unit
    bench. `hready` (the GLOBAL bus ready, an INPUT to the slave) is what the bus
    matrix drives back from the selected slave's `hreadyout`; with exactly one
    slave, `hready = hreadyout` IS the matrix (README §3). The BFM does that
    mirror itself when given a `hready_drive` handle, so we bind:

        hready       = dut.hreadyout   # the BFM READS this as global-ready
        hready_drive = dut.hready      # the BFM DRIVES the slave's hready input

    Passing `hready=` explicitly ALSO steps around a bug in this BFM's from_dut
    (see dut_notes.md "W2-B reconciliation"): its fallback
    `get("hready") or get("hreadyout")` evaluates a cocotb handle in a boolean
    context, which cocotb 2.x forbids — and it fires precisely BECAUSE the frozen
    socket has an `hready` port. Supplying `hready=` pops it before that line
    runs, so the bug never executes. If W2-B fixes from_dut, this call still works
    unchanged."""
    cocotb.start_soon(Clock(dut.hclk, HCLK_NS, units="ns").start())

    # Hold reset and park the bus while the BFM does not yet exist. Once created,
    # the BFM OWNS hsel/haddr/htrans/hwrite/hsize/hburst/hprot/hmastlock/hwdata
    # (its _driver/_park drive them) and dut.hready (via its hready mirror), so we
    # touch none of those by hand after this point.
    dut.hresetn.value = 0
    dut.hsel.value = 0
    dut.haddr.value = 0
    dut.htrans.value = 0          # IDLE
    dut.hwrite.value = 0
    dut.hsize.value = 0b010       # word
    dut.hburst.value = 0          # SINGLE
    dut.hprot.value = 0
    dut.hmastlock.value = 0
    dut.hwdata.value = 0
    dut.hready.value = 1

    # The panel model watches the PADS, through the active-high -> active-low
    # shim. It is tests/clcd/'s file and is used unmodified.
    model = PanelBusModel(PanelPads(dut))
    cocotb.start_soon(model.run())

    rdy = ReadyMonitor(dut)
    cocotb.start_soon(rdy.run())

    await ClockCycles(dut.hclk, 5)
    dut.hresetn.value = 1
    await RisingEdge(dut.hclk)

    ahb = AhbLiteMaster.from_dut(dut, hready=dut.hreadyout, hready_drive=dut.hready)
    await ClockCycles(dut.hclk, 2)
    return ahb, model, rdy


async def _push_pair(ahb, rs, byte):
    off = LCD_CMD if rs == RS_CMD else LCD_DATA
    return await ahb.write(off, byte & 0xFF)


async def _wait_drained(dut, ahb, max_polls=4000):
    """Poll STATUS until the FIFO is empty AND the FSM is idle, then let the last
    cycle's WR-high recovery + CS deassert settle so the trace is complete.

    Robust to either reading of STATUS.busy (see dut_notes.md "the STATUS.busy
    ambiguity"): `fifo_empty && !busy` means fully drained under BOTH the shell's
    `state != IDLE` convention and the socket's `busy || !fifo_empty` one."""
    for _ in range(max_polls):
        s, _r = await ahb.read(LCD_STATUS)
        if (s & STATUS_FIFO_EMPTY) and not (s & STATUS_BUSY):
            await ClockCycles(dut.hclk, 8)
            return
        await ClockCycles(dut.hclk, 2)
    raise AssertionError("FIFO never drained / FSM never went idle")


async def _stream(dut, ahb, model, pairs, timing=None):
    """Set (optional) TIMING, run the FSM, push `pairs` through CMD/DATA, wait for
    a full drain, and return the decoded strobe list."""
    if timing is not None:
        await ahb.write(LCD_TIMING, timing)
    await ahb.write(LCD_CTRL, CTRL_RUN)
    await _wait_drained(dut, ahb)          # start from a known-idle bus (CS idle)
    model.clear()
    for rs, byte in pairs:
        await _push_pair(ahb, rs, byte)
    await _wait_drained(dut, ahb)
    return list(model.strobes)


async def _timed_burst(dut, ahb, model, timing, n=4, first=0x50, rs=RS_CMD):
    """Measure one clean back-to-back burst at a given TIMING. Park the FSM
    (enable=0) and flush, queue `n` bytes, then release: the queued bytes drain
    with no inter-byte idle, so every strobe's WR-low width, CS-setup interval and
    strobe-to-strobe period is deterministic and identical — which is exactly what
    a timing measurement needs. (Same trick as tests/clcd/_timed_burst.)"""
    await ahb.write(LCD_CTRL, CTRL_FIFO_RESET)    # park (enable=0) + flush
    await ahb.write(LCD_TIMING, timing)
    await ClockCycles(dut.hclk, 2)
    model.clear()
    for i in range(n):
        await _push_pair(ahb, rs, (first + i) & 0xFF)
    await ahb.write(LCD_CTRL, CTRL_RUN)           # release -> drain back-to-back
    await _wait_drained(dut, ahb)
    return list(model.strobes)


# ============================================================================ #
# 1. STREAMING FIDELITY — an arbitrary {RS,byte} vector arrives in order, intact
# ============================================================================ #
@cocotb.test(skip=SKIP, timeout_time=2, timeout_unit="ms")
async def test_streams_synthetic_vector_in_order(dut):
    """A synthetic init-shaped sequence (mixed CMD/DATA, arbitrary bytes) pushed
    through CMD/DATA over AHB-Lite must appear on the 8080 bus decoded in EXACTLY
    that order, with the right RS per byte, and nothing else.

    This is the block's core promise, and the whole point of reusing the panel
    model: if this HX8347-D monitor decodes the byte stream, the real panel will
    too. The values are the bench's own invention (see the module header)."""
    ahb, model, rdy = await _bring_up(dut)

    vector = [
        (RS_CMD, 0x2E), (RS_DATA, 0x89),
        (RS_CMD, 0x2B), (RS_DATA, 0x00), (RS_DATA, 0xFF),
        (RS_CMD, 0x00), (RS_DATA, 0xA5), (RS_DATA, 0x5A), (RS_DATA, 0x12),
        (RS_CMD, 0x22),                       # e.g. a GRAM-write-shaped command
        (RS_DATA, 0xDE), (RS_DATA, 0xAD), (RS_DATA, 0xBE), (RS_DATA, 0xEF),
    ]

    strobes = await _stream(dut, ahb, model, vector, timing=_timing(4, 4, 3))

    assert len(strobes) == len(vector), (
        f"emitted {len(strobes)} 8080 cycles for {len(vector)} pushed bytes: "
        f"{model.sequence}")
    assert model.sequence == vector, (
        "the decoded {RS,byte} stream does not match the pushed vector.\n"
        f"  pushed : {vector}\n  decoded: {model.sequence}")

    for i, s in enumerate(strobes):
        assert s.cs_low_throughout, (
            f"cycle {i}: CS was not asserted across the whole WR pulse "
            "(the panel would not have latched this byte)")
        assert s.pd_stable, f"cycle {i}: lcd_pd changed during the WR pulse"
        assert s.rd_high, f"cycle {i}: a read strobe asserted — the panel is WRITE-ONLY"

    # The AHB side must have stayed out of the way throughout (§4).
    assert rdy.max_low_run == 0, (
        f"hreadyout went low for up to {rdy.max_low_run} cycles during a plain "
        "stream — the reference block is zero-wait-state (README §7)")


# ============================================================================ #
# 2. RS SELECT + PD STABILITY through the WR rising edge (the latch instant)
# ============================================================================ #
@cocotb.test(skip=SKIP, timeout_time=2, timeout_unit="ms")
async def test_rs_select_and_pd_stability_through_wr_edge(dut):
    """CMD writes must carry RS=0, DATA writes RS=1, and lcd_pd/lcd_rs must be
    stable through the WR edge on EVERY cycle — the panel latches {RS, byte}
    there and nowhere else (README §6; the pad is active-low, so the panel's
    latching rising edge is this block's lcd_wr FALLING edge, which is exactly
    what the shim + panel model reconstruct).

    Interleaved CMD/DATA so an RS that lags its byte by one cycle — a very easy
    bug in the AHB pipeline — shows up as a swapped RS, not as a silent pass."""
    ahb, model, rdy = await _bring_up(dut)

    pairs = [(RS_CMD, 0x10 + i) if i % 2 == 0 else (RS_DATA, 0xA0 + i)
             for i in range(8)]
    strobes = await _stream(dut, ahb, model, pairs, timing=_timing(4, 4, 3))

    assert len(strobes) == len(pairs), (
        f"{len(strobes)} cycles for {len(pairs)} bytes: {model.sequence}")
    for i, (s, (rs, byte)) in enumerate(zip(strobes, pairs)):
        assert s.rs == rs, (
            f"cycle {i}: latched RS={s.rs}, expected {rs} "
            f"({'CMD' if rs == RS_CMD else 'DATA'}) for byte 0x{byte:02X}")
        assert s.byte == byte, (
            f"cycle {i}: latched 0x{s.byte:02X}, pushed 0x{byte:02X}")
        assert s.pd_stable, f"cycle {i}: lcd_pd not stable through the WR pulse"
        assert s.cs_low_throughout, f"cycle {i}: lcd_cs deasserted mid-cycle"

    # CS must idle BETWEEN bytes — the KVM's safe-switch gate depends on this
    # quiescent point existing per byte (README §6; CLCD_PANEL_FACTS.md §6).
    # If CS were held across the whole burst, consecutive strobes would share one
    # CS assertion and cs_setup_cycles would only be measurable on the first.
    for i, s in enumerate(strobes):
        assert s.cs_setup_cycles is not None and s.cs_setup_cycles >= 1, (
            f"cycle {i}: no CS assertion of its own (cs_setup={s.cs_setup_cycles}). "
            "lcd_cs must be DEASSERTED BETWEEN BYTES — the KVM's safe-switch "
            "point is `!lcd_cs && !lcd_busy`, so a block that holds CS across a "
            "burst can only be preempted by the 1 ms timeout (README §6)")


# ============================================================================ #
# 3. TIMING register — honoured, in hclk cycles, field-isolated
# ============================================================================ #
@cocotb.test(skip=SKIP, timeout_time=5, timeout_unit="ms")
async def test_timing_register_is_honoured_in_hclk_cycles(dut):
    """WR-low width, CS-setup interval and strobe-to-strobe period each track
    their TIMING field in whole hclk cycles. Measured at FOUR distinct TIMING
    values (a base + one per field), asserting BOTH the absolute width AND — the
    field-isolating, convention-robust proof — that moving exactly one field moves
    exactly the matching interval by exactly that delta.

    The absolute check pins the convention `clcd_core` inherits from the shell's
    clcd.sv (`phase_load(v) = v-1`, so a phase lasts exactly its field's cycles).
    The delta check would still hold under an off-by-one convention, so if the
    absolute assert fires while the deltas pass, the finding is 'clcd_core was NOT
    lifted unchanged', which is precisely the thing worth knowing.

    The §6 timing FLOOR (every phase >= 8 hclk cycles, because the tunnel is a
    CDC into the shell's 100 MHz domain) is a constraint on the DRIVER's chosen
    TIMING value, not on the block: the block must honour whatever it is given.
    So this test deliberately sweeps values BELOW the floor too — the register has
    to be faithful, and it is firmware's job to program >= 8."""
    ahb, model, rdy = await _bring_up(dut)

    wr_lo0, wr_hi0, cs0 = 4, 4, 3
    base = await _timed_burst(dut, ahb, model, _timing(wr_lo0, wr_hi0, cs0))
    wl0 = _consistent([s.wr_low_cycles for s in base], "wr_low @base")
    cs_meas0 = _consistent([s.cs_setup_cycles for s in base], "cs_setup @base")
    per0 = _consistent(_periods(base), "period @base")
    dut._log.info(
        f"[nanosoc_lcd timing] base (wr_lo={wr_lo0} wr_hi={wr_hi0} cs_setup={cs0}): "
        f"wr_low={wl0} cs_setup={cs_meas0} period={per0} hclk cycles")

    # WR-low width == wr_lo, and +5 in the register adds exactly 5 hclk cycles.
    assert wl0 == wr_lo0, f"WR-low width {wl0} != wr_lo {wr_lo0}"
    s_wl = await _timed_burst(dut, ahb, model, _timing(wr_lo0 + 5, wr_hi0, cs0))
    wl1 = _consistent([s.wr_low_cycles for s in s_wl], "wr_low @wr_lo+5")
    assert wl1 == wr_lo0 + 5, f"WR-low width {wl1} != wr_lo {wr_lo0 + 5}"
    assert wl1 - wl0 == 5, (
        f"WR-low width did not track wr_lo: +5 in the register gave {wl1 - wl0} "
        "extra hclk cycles")

    # CS-setup interval == cs_setup, and +4 adds exactly 4.
    assert cs_meas0 == cs0, f"CS-setup {cs_meas0} != cs_setup {cs0}"
    s_cs = await _timed_burst(dut, ahb, model, _timing(wr_lo0, wr_hi0, cs0 + 4))
    cs_meas1 = _consistent([s.cs_setup_cycles for s in s_cs], "cs_setup @cs+4")
    assert cs_meas1 == cs0 + 4, f"CS-setup {cs_meas1} != cs_setup {cs0 + 4}"
    assert cs_meas1 - cs_meas0 == 4, (
        f"CS-setup did not track cs_setup: +4 gave {cs_meas1 - cs_meas0} extra cycles")

    # The strobe period responds to wr_hi (which has no width of its own to
    # measure — it only shows up as the gap to the next cycle): +6 adds exactly 6.
    s_wh = await _timed_burst(dut, ahb, model, _timing(wr_lo0, wr_hi0 + 6, cs0))
    per1 = _consistent(_periods(s_wh), "period @wr_hi+6")
    dut._log.info(f"[nanosoc_lcd timing] period: wr_hi={wr_hi0}->{per0}, "
                  f"wr_hi={wr_hi0 + 6}->{per1} cycles")
    assert per1 - per0 == 6, (
        f"strobe period did not track wr_hi: +6 gave {per1 - per0} extra hclk cycles")

    # And at the §6 NORMATIVE floor (8/8/8), one byte must cost >= 24 hclk cycles
    # (~480 ns @ 50 MHz). This is the value the DUT driver actually programs; a
    # block that silently clamped TIMING would break the tunnel's CDC.
    s_floor = await _timed_burst(dut, ahb, model, _timing(8, 8, 8))
    wl_f = _consistent([s.wr_low_cycles for s in s_floor], "wr_low @floor")
    cs_f = _consistent([s.cs_setup_cycles for s in s_floor], "cs_setup @floor")
    per_f = _consistent(_periods(s_floor), "period @floor")
    assert wl_f >= 8 and cs_f >= 8 and per_f >= 24, (
        f"at the §6 timing floor (8/8/8) a byte took wr_low={wl_f} cs_setup={cs_f} "
        f"period={per_f} hclk cycles; the tunnel CDC needs every phase >= 8")
    dut._log.info(f"[nanosoc_lcd timing] §6 floor (8/8/8): period={per_f} hclk "
                  f"cycles = {per_f * HCLK_NS} ns/byte @ 50 MHz")


# ============================================================================ #
# 4. CTRL -> pads, lcd_busy/lcd_req semantics, self-clearing fifo_reset
# ============================================================================ #
@cocotb.test(skip=SKIP, timeout_time=2, timeout_unit="ms")
async def test_ctrl_pads_req_busy_and_reset_values(dut):
    """The socket's §2 reset values, CTRL.req -> lcd_req, and lcd_en.

    The reset values are a SAFETY property, not housekeeping: these wires cross
    the RP boundary, and `hreadyout` must reset to 1 (§4) or the bus hangs the
    moment the matrix routes hready through this slave. The display strobes must
    reset to 0 (inert), which is what makes the decoupler's 0x0 clamp safe."""
    ahb, model, rdy = await _bring_up(dut)

    # ---- §2 reset values, sampled while still in reset ---------------------- #
    dut.hresetn.value = 0
    await ClockCycles(dut.hclk, 3)
    await ReadOnly()
    assert int(dut.hreadyout.value) == 1, (
        "hreadyout MUST reset to 1, not 0 (README §4) — a slave that comes out of "
        "reset holding the bus low hangs the Cortex-M0 with no error")
    for name in ("lcd_cs", "lcd_wr", "lcd_busy", "lcd_req"):
        assert int(getattr(dut, name).value) == 0, (
            f"{name} must be 0 (inert) in reset — the display strobes are "
            "ACTIVE-HIGH precisely so that the decoupler's 0x0 clamp means "
            "'all strobes idle' by construction (§6)")
    assert int(dut.hresp.value) == 0, "hresp must reset to 0 (OKAY)"
    await RisingEdge(dut.hclk)
    dut.hresetn.value = 1
    await ClockCycles(dut.hclk, 3)

    # ---- CTRL reset value + lcd_en ------------------------------------------ #
    ctrl, _ = await ahb.read(LCD_CTRL)
    assert ctrl == 0, f"CTRL must reset to 0, got {ctrl:#x}"

    await ahb.write(LCD_CTRL, CTRL_RUN)
    await ClockCycles(dut.hclk, 2)
    assert int(dut.lcd_en.value) == 1, (
        "lcd_en must be 1 while the block is driving the display pins (§6). With "
        "lcd_en=0 the RM wrapper routes the M0's raw GPIO onto the tunnel instead "
        "and NOTHING this block emits reaches the panel")

    # ---- CTRL.req -> lcd_req ------------------------------------------------ #
    assert int(dut.lcd_req.value) == 0, "lcd_req must be 0 with CTRL.req clear"
    await ahb.write(LCD_CTRL, CTRL_RUN | CTRL_REQ)
    await ClockCycles(dut.hclk, 2)
    assert int(dut.lcd_req.value) == 1, "CTRL.req=1 must drive lcd_req=1 (§7)"
    await ahb.write(LCD_CTRL, CTRL_RUN)
    await ClockCycles(dut.hclk, 2)
    assert int(dut.lcd_req.value) == 0, "clearing CTRL.req must drop lcd_req"

    # ---- lcd_busy = (fsm != IDLE) || !fifo_empty ---------------------------- #
    # This one is worth its own assertion because the socket's definition is
    # STRICTLY WIDER than the shell block's STATUS.busy (`state != IDLE`), and
    # §6 says "get this right — the KVM uses it to decide when it is safe to
    # switch". A block that reported only fsm-busy would look quiescent to the
    # KVM while it still had a queue to drain, and the handover would cut its
    # byte stream in half.
    await ahb.write(LCD_CTRL, CTRL_FIFO_RESET)     # park (enable=0) + flush
    await ClockCycles(dut.hclk, 3)
    assert int(dut.lcd_busy.value) == 0, "lcd_busy must be 0 when idle and empty"

    for i in range(4):                              # queue with the FSM PARKED
        await _push_pair(ahb, RS_DATA, 0x70 + i)
    await ClockCycles(dut.hclk, 3)
    s, _ = await ahb.read(LCD_STATUS)
    assert _level(s) == 4, (
        f"fifo_level={_level(s)} after 4 parked pushes — a parked FSM must NOT "
        "drain (see dut_notes.md, 'the one behavioural assumption')")
    assert len(model.strobes) == 0, (
        "a parked FSM (enable=0) drove 8080 cycles — the backpressure and "
        "fifo_reset tests both assume parked == no drain")
    assert int(dut.lcd_busy.value) == 1, (
        "lcd_busy must be 1 with a NON-EMPTY FIFO even though the FSM is idle: "
        "§6 defines it as `(fsm != IDLE) || !fifo_empty` ('a byte in flight OR "
        "QUEUED'). Reporting only fsm-busy lets the KVM take the panel away "
        "mid-stream")

    # ---- fifo_reset self-clears and empties --------------------------------- #
    await ahb.write(LCD_CTRL, CTRL_FIFO_RESET)
    await ClockCycles(dut.hclk, 3)
    ctrl, _ = await ahb.read(LCD_CTRL)
    assert not (ctrl & CTRL_FIFO_RESET), "CTRL.fifo_reset must self-clear (read back 0)"
    s, _ = await ahb.read(LCD_STATUS)
    assert s & STATUS_FIFO_EMPTY, "fifo_reset must empty the FIFO (fifo_empty=1)"
    assert _level(s) == 0, f"fifo_reset must zero fifo_level, got {_level(s)}"
    assert not (s & STATUS_FIFO_FULL)
    assert int(dut.lcd_busy.value) == 0, "lcd_busy must drop once the FIFO is flushed"
    assert rdy.max_low_run == 0, f"hreadyout stalled {rdy.max_low_run} cycles"


# ============================================================================ #
# 5. *** THE CRITICAL TEST ***
#    FIFO full -> pushes are DROPPED, and the AHB BUS IS NEVER STALLED
# ============================================================================ #
@cocotb.test(skip=SKIP, timeout_time=10, timeout_unit="ms")
async def test_fifo_full_drops_and_never_stalls_ahb(dut):
    """Fill the FIFO to STATUS.fifo_full, then keep writing.

    Every write must COMPLETE (hresp=OKAY, hreadyout asserted) and be silently
    DROPPED. `hreadyout` must be asserted on EVERY cycle of the whole test.

    THIS IS THE ONE THAT MATTERS. `hreadyout = !fifo_full` is the obvious, wrong,
    "helpful" implementation, and README §4 names it a TRAP: the FIFO drains only
    as the 8080 FSM pops it, but if the CPU is what would eventually stop pushing
    — and the CPU is now stalled mid-write, waiting for hreadyout — the system is
    DEADLOCKED. Not slow: dead. No exception, no watchdog, nothing on the screen.
    A student debugging that has no thread to pull.

    So the block must drop, exactly as the shell's clcd.sv does
    (`fpga/shell/ip/clcd/clcd.sv:31-36`), and publish STATUS.fifo_full for
    firmware to poll.

    Three independent guards, because this must not be able to pass by accident:
      a) `with_timeout` on each write-while-full — a slave that stalls HANGS the
         bus, and a hang is not an assertion failure unless something makes it
         one. This turns the deadlock into a clean, named test failure.
      b) the ReadyMonitor — hreadyout must have ZERO low cycles.
      c) the bound SVA A_LIVENESS (bind_nanosoc_lcd.sv) — fires on an unbounded
         stall independently of the Python side.
    Then zero loss is proved once the FIFO drains."""
    ahb, model, rdy = await _bring_up(dut)

    expected = []          # bytes accepted while !full -> must ALL reach the panel
    push_i = 0

    def next_pair():
        nonlocal push_i
        rs = RS_CMD if (push_i % 3 == 0) else RS_DATA
        byte = (0x01 + push_i) & 0xFF
        push_i += 1
        return rs, byte

    # ---- Fill with the FSM PARKED (CTRL=0) until STATUS.fifo_full ----------- #
    # The 8080 pads carry no ready/backpressure from panel -> master, so the only
    # faithful way to model "the panel side is not draining" is to park the FSM.
    # Test 4 asserts that parked really does mean no drain.
    prev_level = -1
    guard = 0
    while True:
        s, _ = await ahb.read(LCD_STATUS)
        lvl = _level(s)
        assert lvl >= prev_level, (
            f"fifo_level went backwards {prev_level}->{lvl} while only pushing")
        prev_level = lvl
        if s & STATUS_FIFO_FULL:
            break
        rs, byte = next_pair()
        resp = await ahb.write(LCD_CMD if rs == RS_CMD else LCD_DATA, byte)
        assert resp == 0, f"a !full CMD/DATA write returned hresp={resp:#x}, expected OKAY"
        expected.append((rs, byte))
        guard += 1
        assert guard < 4096, "the FIFO never reported full — is STATUS.fifo_full wired?"

    full_level = prev_level
    assert full_level >= 1, f"fifo_full asserted at level {full_level}"
    dut._log.info(f"[nanosoc_lcd bp] FIFO full at level {full_level}; "
                  f"{len(expected)} bytes accepted while !full")

    # ---- Writes while FULL: must complete, must drop, must NOT stall -------- #
    rdy.arm()
    drops = 8
    for k in range(drops):
        s, _ = await ahb.read(LCD_STATUS)
        assert s & STATUS_FIFO_FULL, "the FIFO drained while parked — drop test invalid"
        lvl_before = _level(s)

        off = LCD_DATA if k % 2 else LCD_CMD
        try:
            # (a) A stalling slave HANGS here. Make that a failure, not a hang.
            resp = await with_timeout(ahb.write(off, 0xF0 + k), 4 * HCLK_NS * 64, "ns")
        except SimTimeoutError:
            raise AssertionError(
                f"write #{k} to a FULL FIFO NEVER COMPLETED: the slave is holding "
                "hreadyout LOW to back-pressure the bus.\n"
                "  *** THIS IS THE DEADLOCK README §4 FORBIDS. ***\n"
                "  On silicon the AHB matrix stalls, the Cortex-M0 stalls, and the "
                "board dies with no exception and nothing on screen.\n"
                "  Writes to a full FIFO must be DROPPED (hreadyout stays 1, "
                "hresp=OKAY) and firmware polls STATUS.fifo_full — exactly what "
                "fpga/shell/ip/clcd/clcd.sv:31-36 does.")

        assert resp == 0, (
            f"a write while full returned hresp={resp:#x}; it must complete OKAY "
            "and be dropped")
        s2, _ = await ahb.read(LCD_STATUS)
        assert _level(s2) == lvl_before, (
            f"a dropped write changed fifo_level {lvl_before}->{_level(s2)}: it was "
            "ENQUEUED, not dropped — the FIFO has overrun and older bytes are gone")

    # (b) Not one stalled cycle anywhere in the drop window.
    assert rdy.window_low == 0, (
        f"hreadyout was LOW for {rdy.window_low} cycle(s) while the FIFO was full. "
        "The block is back-pressuring the AHB bus instead of dropping — README §4: "
        "'Do not back-pressure the bus on a FIFO.'")

    # ---- Release the panel-side stall; drain; prove ZERO LOSS --------------- #
    model.clear()
    await ahb.write(LCD_TIMING, _timing(wr_lo=1, wr_hi=1, cs_setup=1))
    await ahb.write(LCD_CTRL, CTRL_RUN)
    await _wait_drained(dut, ahb)

    got = model.sequence
    assert got == expected, (
        "ZERO-LOSS violated across the panel-side stall.\n"
        f"  pushed while !full ({len(expected)}): {expected[:16]}...\n"
        f"  arrived at the panel ({len(got)}): {got[:16]}...")

    # (b) again, over the WHOLE test: the reference block is zero-wait-state.
    assert rdy.max_low_run == 0, (
        f"hreadyout went low for up to {rdy.max_low_run} consecutive cycles during "
        f"the test ({rdy.total_low} low cycles in total). README §7: 'hreadyout is "
        "1'b1, always.'")
    dut._log.info(f"[nanosoc_lcd bp] {len(expected)} bytes streamed with zero loss; "
                  f"hreadyout low for {rdy.total_low} cycles (must be 0)")


# ============================================================================ #
# 6. NO READ SIDE EFFECTS — read every register; the panel bus must not move
# ============================================================================ #
@cocotb.test(skip=SKIP, timeout_time=2, timeout_unit="ms")
async def test_read_sweep_has_no_panel_side_effect(dut):
    """Read every register (and some unmapped aliases) with the FSM LIVE, and
    assert ZERO panel-side activity results: no CS, no WR, no strobe, nothing.

    This is a platform-wide invariant, not a nicety. The platform sweeps every
    offset of every CSR page — over SWD/XVC, and in tests/csr_decode_width/ — so a
    register whose READ has a side effect gets fired by a debugger memory dump. It
    has already cost this platform once: a read of DFXCTL 0x20 popped a UART
    console byte through a decode alias (dfx_ctl.sv:229-258). Here the side effect
    would be 8080 cycles sprayed at a live panel.

    Run with `enable=1` (the FSM armed and hungry) so a read that armed a bus cycle
    would actually launch one — a read sweep against a parked block would pass
    vacuously and prove nothing."""
    ahb, model, rdy = await _bring_up(dut)

    # Arm the FSM and let any startup activity settle.
    await ahb.write(LCD_TIMING, _timing(4, 4, 3))
    await ahb.write(LCD_CTRL, CTRL_RUN)
    await _wait_drained(dut, ahb)
    await ClockCycles(dut.hclk, 8)

    # Non-vacuity: prove the panel model CAN see this block right now, so that a
    # clean sweep below means "nothing happened", not "the monitor was deaf".
    model.reset_activity()
    await _push_pair(ahb, RS_CMD, 0x5A)
    await _wait_drained(dut, ahb)
    assert not model.idle, (
        "sanity: pushing a byte produced NO panel activity — the panel monitor is "
        "not watching this DUT, so the read sweep below would pass vacuously")
    assert model.sequence[-1] == (RS_CMD, 0x5A)

    # ---- The sweep. From here on the panel must not move at all. ------------ #
    model.clear()
    model.reset_activity()
    n_strobes_before = len(model.strobes)

    for off in ALL_OFFSETS:
        val, resp = await ahb.read(off)
        assert resp == 0, f"read @{off:#04x} returned hresp={resp:#x}, expected OKAY"
        await ClockCycles(dut.hclk, 4)     # give any (illegal) launched cycle time
        assert model.idle, (
            f"reading offset {off:#04x} drove the PANEL BUS "
            f"(cs={model.any_cs_low} wr={model.any_wr_low} rd={model.any_rd_low}).\n"
            "  A read must have NO side effect: the platform dumps whole CSR pages "
            "over SWD/XVC and in tests/csr_decode_width/, so a debugger memory read "
            "would spray 8080 cycles at a live panel.")

    await ClockCycles(dut.hclk, 32)
    assert model.idle, "panel activity appeared after the read sweep settled"
    assert len(model.strobes) == n_strobes_before, (
        f"{len(model.strobes) - n_strobes_before} 8080 write cycles were emitted by "
        "a read sweep")

    # And reading did not disturb the FIFO / state either.
    s, _ = await ahb.read(LCD_STATUS)
    assert s & STATUS_FIFO_EMPTY, "the read sweep left bytes in the FIFO"
    assert rdy.max_low_run == 0, f"hreadyout stalled {rdy.max_low_run} cycles on reads"


# ============================================================================ #
# 7. AHB-LITE PROTOCOL LEGALITY — including BACK-TO-BACK (pipelined) transfers
# ============================================================================ #
@cocotb.test(skip=SKIP, timeout_time=5, timeout_unit="ms")
async def test_ahb_back_to_back_pipelined_transfers(dut):
    """Drive genuinely PIPELINED, back-to-back AHB-Lite transfers — transfer B's
    ADDRESS phase overlapping transfer A's DATA phase, with no IDLE between — and
    prove the block still streams every byte, in order, with hreadyout never low.

    Driven through W2-B's BFM (`init_write` to queue, `pipeline` to await) —
    NOT raw, because the BFM runs a continuous background bus driver and a second
    hand-driver on the same handles would collide. Queuing several transfers and
    awaiting them together is exactly how the BFM emits GENUINELY gapless
    transfers: transfer n+1's address phase lands in the same cycle as transfer
    n's data phase. We assert on `overlapped_cycles` to prove the overlap really
    happened rather than trusting the name.

    Deliberately alternating CMD/DATA so a one-transfer data/address skew — the
    classic bug of latching hwdata in the ADDRESS phase (README §3), one cycle too
    early — shows up as swapped RS AND wrong bytes, not a silent pass.

    Yes — an always-ready zero-wait-state slave passes this trivially, and that is
    fine: the contract asks for exactly such a slave (§4). The test exists so a
    FUTURE student's block that inserts wait states, mishandles hready, or samples
    hwdata a cycle early is caught here rather than on a board that just stops.

    (The bound SVA checker in bind_nanosoc_lcd.sv is watching every cycle of every
    test in this file, not only this one.)"""
    ahb, model, rdy = await _bring_up(dut)

    await ahb.write(LCD_TIMING, _timing(2, 2, 2))
    await ahb.write(LCD_CTRL, CTRL_FIFO_RESET)     # park + flush: nothing drains
    await ClockCycles(dut.hclk, 3)
    model.clear()

    pairs = [(RS_CMD if i % 2 == 0 else RS_DATA, (0xB0 + i) & 0xFF) for i in range(12)]

    ovl_before = ahb.overlapped_cycles
    txns = [ahb.init_write(LCD_CMD if rs == RS_CMD else LCD_DATA, b)
            for rs, b in pairs]                    # queued -> emitted back-to-back
    await ahb.pipeline(txns)
    for t in txns:
        assert t.resp == 0, f"a pipelined write to {t.addr:#04x} returned ERROR"

    # The BFM must actually have overlapped address/data phases, or this test
    # proves nothing about pipelining.
    assert ahb.overlapped_cycles > ovl_before, (
        "the BFM emitted NO overlapped address/data cycles — these transfers were "
        "not pipelined, so a slave that mishandles the overlap would slip through")

    # With the FSM parked, none can have drained: the level is the exact count the
    # pipeline enqueued. A short count == a transfer lost in the overlap.
    await ClockCycles(dut.hclk, 4)
    s, _ = await ahb.read(LCD_STATUS)
    assert _level(s) == len(pairs), (
        f"back-to-back pipelined writes enqueued {_level(s)} of {len(pairs)} bytes. "
        "The classic AHB-Lite bug: sampling hwdata in the ADDRESS phase (it is "
        "valid in the DATA phase, one cycle LATER — README §3), or dropping a "
        "transfer whose address phase overlapped the previous data phase")
    assert len(model.strobes) == 0, "the parked FSM drained during the pipelined run"

    # Release and prove the ORDER and CONTENT survived the pipeline.
    await ahb.write(LCD_CTRL, CTRL_RUN)
    await _wait_drained(dut, ahb)
    assert model.sequence == pairs, (
        "pipelined back-to-back writes did not stream faithfully.\n"
        f"  pushed : {pairs}\n  decoded: {model.sequence}\n"
        "A one-place shift here means the block used hwdata from the WRONG data "
        "phase (README §3's 'classic bug').")

    # Interleave reads and writes back-to-back too: a read data phase overlapping
    # the next write's address phase is where a slave that muxes hrdata off the
    # LIVE (rather than the registered) address falls over.
    for _ in range(3):
        s, resp = await ahb.read(LCD_STATUS)
        assert resp == 0
        r = await ahb.write(LCD_CMD, 0x33)
        assert r == 0
    await _wait_drained(dut, ahb)

    assert rdy.max_low_run == 0, (
        f"hreadyout went low for up to {rdy.max_low_run} cycles under pipelined "
        f"traffic ({rdy.total_low} low cycles total) — README §4")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _consistent(vals, what):
    """Assert every measurement is equal (the FSM is deterministic for a given
    TIMING) and return the common value."""
    assert vals, f"{what}: no measurements — no 8080 cycles were decoded at all"
    assert all(v == vals[0] for v in vals), (
        f"{what}: not constant across the burst: {vals}")
    return vals[0]


def _periods(strobes):
    """WR-rise -> next WR-rise spacing, in hclk cycles, within one burst."""
    return [strobes[i + 1].n_rise - strobes[i].n_rise for i in range(len(strobes) - 1)]
