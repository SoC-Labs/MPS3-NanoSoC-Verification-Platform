"""tests/rm_socscope/test_rm_socscope.py

The two Stage-C features `rm_socscope` gained, proved board-free, plus the
control that says each was needed rather than assumed.

  EGRESS (MPS3_BRINGUP_TESTS.md C2). The egress moved off `dut_clk` onto
  `phy_rmii_ref_clk`, the shell's free-running 50 MHz RMII reference. The point
  of the split is that bytes captured before the DUT clock stops can still LEAVE
  while it is stopped -- so the measurement is exactly that: stop the DUT clock
  mid-capture and require whole, CRC-good frames to keep arriving.

  Its CONTROL is the same run on `TRACE_CLK_FREE=0`, the B1 single-clock tie,
  where `swo` must go dead the instant the clock stops. C2 asks for this in so
  many words: "run it once without the domain split and confirm it fails ...
  the cheapest possible demonstration that the split was needed."

  FREEZE (C1). HW-013 says no clock buffer can be placed in this RP, so the gate
  is always somewhere else. THE BENCH PLAYS THE GATE -- it drives `dut_clk`
  itself and gates it, which is where the gate really lives (static-side) and
  also means the step count is measured on the GATED CLOCK rather than on the
  engine's own counter, as C1 requires.

  `FREEZE_IN_RM=0` (the shipped default, the replan's decision) has no engine at
  all: the bench simply stops the clock, and the RM's obligation is to survive
  it. `FREEZE_IN_RM=1` builds the other HW-013 option -- engine in the RM, enable
  out on `dut_gpio_o[4]` -- and the exactness tests below are what that option
  has to answer for.

WHAT THIS BENCH DOES NOT COVER, stated plainly: it is a functional bench of ONE
RM against ITS boundary. It does not run the DFX swap, does not model the DFX
decoupler (the fail-safe polarity of the hold bit is checked as a LEVEL, not by
clamping it), does not exercise the shell's `swo_uart_rx` / 16-byte FIFO (that is
tests/uart_bridge, and HW-007 is a board measurement), and asserts no timing.

One thing the oracle says about these captures and it is a property of the BENCH,
not of the RM: "the capture carries NO timebase announcement". `socscope_timebase`
announces its tick every ANNOUNCE_EVERY=256 records and these runs recover ~9, so no
announcement is due. Proving the announcement needs 256 records, which at any pacing
this bench can afford is minutes of wall time -- it is SoCScope's own
`tb_socscope_timebase`'s job, and HW-010 is the board measurement. The oracle passes
the capture and says so loudly, which is the right behaviour on both sides.

THE ORACLE IS NOT WRITTEN HERE. The decode is SoCScope's own
`hw/bench/check_selftest.py::check_stream` -- the same function that reads a
board capture -- over its own `socscope.decode.framing.deframe`. A bench that
reimplements its DUT's framing agrees with it by construction; SoCScope's
mutation gate found five framer mutants that survived exactly that mistake.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.simtime import get_sim_time
from cocotb.triggers import Edge, FallingEdge, Timer

# --- the configuration this elaboration was built with -----------------------
# Read from the environment the Makefile exported, not inferred from the RTL: a
# bench that derives its own parameters cannot fail when the Makefile and the
# elaboration disagree, which is the one thing a parameterised bench must catch.
CFG = os.environ.get("CFG", "split")
TRACE_CLK_FREE = int(os.environ.get("RM_SOCSCOPE_TRACE_CLK_FREE", "1"))
FREEZE_IN_RM = int(os.environ.get("RM_SOCSCOPE_FREEZE_IN_RM", "0"))
STEP_N = int(os.environ.get("RM_SOCSCOPE_STEP_N", "1"))
WDOG = int(os.environ.get("RM_SOCSCOPE_WDOG", "64000000"))
DIV = int(os.environ.get("RM_SOCSCOPE_DIV", "24"))

SOCSCOPE_HOME = os.environ.get(
    "SOCSCOPE_HOME",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "SoCScope"))

# The SoCScope oracle. Absent checkout => the bench cannot have elaborated at
# all, so this is an error, not a skip -- but import it lazily-ish so the failure
# names the tree instead of dying in a traceback about a module nobody has heard of.
_ORACLE_ERR = None
try:
    sys.path.insert(0, os.path.join(SOCSCOPE_HOME, "hw", "bench"))
    sys.path.insert(0, os.path.join(SOCSCOPE_HOME, "host", "src"))
    from socscope.decode.framing import deframe          # noqa: E402
    from check_selftest import check_stream              # noqa: E402
except Exception as exc:                                  # pragma: no cover
    _ORACLE_ERR = "%s: %s" % (type(exc).__name__, exc)

# --- timing ------------------------------------------------------------------
TRACE_PERIOD_NS = 20.0            # phy_rmii_ref_clk: the shell's 50 MHz, free-running
DUT_PERIOD_NS = 20.0              # dut_clk at its default 50 MHz
DUT_SKEW_NS = 7.0                 # deliberate phase offset: the two are asynchronous
BIT_NS = (DIV + 1) * TRACE_PERIOD_NS   # 8N1 bit period, in TRACE clocks now

# dut_gpio_o bit assignment (rm_socscope.sv "Everything else: inert")
G_CONFIGURED, G_FROZEN, G_STEPPING, G_WDOG_FIRED, G_HOLD = 0, 1, 2, 3, 4

# dut_gpio_i[10:8] command encoding (socscope_freeze_ctl: 0 run, 1 freeze, 2 step)
C_RUN, C_FREEZE, C_STEP = 0, 1, 2


def now_ns():
    return get_sim_time(unit="ns")


def _bit(sig, n):
    """Bit n of a vector, X-safe: an X reads as 0 and says so by returning None."""
    try:
        return (int(sig.value) >> n) & 1
    except ValueError:
        return None


class GatedDutClock:
    """dut_clk, driven by the bench -- because the gate is not in the RP.

    HW-013: the BUFGCE sites for this RP's clock regions are in the column
    carrying the shell's own I/O, so a clock gate can only ever live static-side.
    This coroutine IS that static-side gate, and counting its delivered rising
    edges is the C1 measurement ("measured on a real gated clock rather than on
    the engine's own counter").
    """

    def __init__(self, dut, period_ns=DUT_PERIOD_NS, hold=None):
        self.dut = dut
        self.period_ns = period_ns
        self.hold = hold or (lambda: False)
        self.edges = 0

    def set_period(self, period_ns):
        self.period_ns = period_ns

    async def run(self):
        self.dut.dut_clk.value = 0
        await Timer(DUT_SKEW_NS, unit="ns")
        while True:
            half = self.period_ns / 2.0
            if not self.hold():
                self.dut.dut_clk.value = 1
                self.edges += 1
            await Timer(half, unit="ns")
            self.dut.dut_clk.value = 0
            await Timer(half, unit="ns")


class SwoRx:
    """An 8N1 receiver on `swo`, and a transition counter beside it.

    The byte stream is what the oracle reads. The TRANSITION count is what makes
    the C2 control unambiguous: a stopped transmitter does not merely send wrong
    bytes, it stops moving the wire at all, and "zero edges after t" is a
    measurement that cannot be argued with.
    """

    def __init__(self, dut, bit_ns=BIT_NS):
        self.dut = dut
        self.bit_ns = bit_ns
        self.bytes = []          # (t_done_ns, value)
        self.edges = []          # transition times

    def stream_after(self, t_ns):
        return bytes(v for (t, v) in self.bytes if t > t_ns)

    def stream(self):
        return bytes(v for (_t, v) in self.bytes)

    def edges_after(self, t_ns):
        return [t for t in self.edges if t > t_ns]

    async def watch_edges(self):
        while True:
            await Edge(self.dut.swo)
            self.edges.append(now_ns())

    async def run(self):
        while True:
            await FallingEdge(self.dut.swo)          # start bit
            t0 = now_ns()
            await Timer(self.bit_ns * 1.5, unit="ns")   # into the middle of bit 0
            v = 0
            for i in range(8):
                b = _bit(self.dut.swo, 0)
                v |= (b or 0) << i
                await Timer(self.bit_ns, unit="ns")
            self.bytes.append((t0 + 10 * self.bit_ns, v))


async def bring_up(dut, hold=None, dut_period_ns=DUT_PERIOD_NS):
    """Reset the RM, start both clocks and the receiver, wait for `configured`."""
    for name, val in (("dut_resetn", 0), ("rp_resetn", 0), ("dbg_resetn", 0),
                      ("jtag_tck", 0), ("jtag_tms", 0), ("jtag_tdi", 0),
                      ("phy_rmii_crs_dv", 0), ("phy_rmii_rxd", 0), ("mdio_i", 0),
                      ("uart_tx_tready", 1), ("uart_rx_tdata", 0), ("uart_rx_tvalid", 0),
                      ("dut_gpio_i", 0), ("qspi_io_i", 0)):
        getattr(dut, name).value = val

    cocotb.start_soon(Clock(dut.phy_rmii_ref_clk, TRACE_PERIOD_NS, unit="ns").start())
    clk = GatedDutClock(dut, dut_period_ns, hold)
    cocotb.start_soon(clk.run())

    await Timer(500, unit="ns")
    dut.dut_resetn.value = 1
    dut.rp_resetn.value = 1
    dut.dbg_resetn.value = 1

    # Only now start the receiver: `swo` idles HIGH out of reset
    # (socscope_egress_serial "txd <= 1'b1; // idle HIGH"), and starting a
    # falling-edge detector while the wire is still X manufactures a byte.
    await Timer(200, unit="ns")
    rx = SwoRx(dut)
    cocotb.start_soon(rx.run())
    cocotb.start_soon(rx.watch_edges())
    return clk, rx


async def wait_for_configured(dut, limit_ns=200_000):
    t0 = now_ns()
    while _bit(dut.dut_gpio_o, G_CONFIGURED) != 1:
        await Timer(1000, unit="ns")
        assert now_ns() - t0 < limit_ns, (
            "socscope_selftest never asserted `configured` -- the CSR writes never "
            "completed, so nothing was ever going to reach the wire")


async def issue(dut, cmd):
    """Issue a freeze command on the DIP-switch bits, RISING edge of [10]."""
    dut.dut_gpio_i.value = (cmd & 0x3) << 8              # strobe low, command set up
    await Timer(4 * TRACE_PERIOD_NS, unit="ns")
    dut.dut_gpio_i.value = ((cmd & 0x3) | 0x4) << 8      # strobe high -> issue
    await Timer(8 * TRACE_PERIOD_NS, unit="ns")          # 2-FF sync + freeze_ctl's own
    dut.dut_gpio_i.value = (cmd & 0x3) << 8


# =============================================================================
# CFG=split -- the shipped build
# =============================================================================
@cocotb.test(skip=(CFG != "split"))
async def test_the_stream_decodes_through_socscopes_own_oracle(dut):
    """The split egress still delivers the self-test stream, and SoCScope's own
    board oracle says so -- records, filter and timebase, not just bytes."""
    assert _ORACLE_ERR is None, "SoCScope oracle unavailable (%s)" % _ORACLE_ERR
    _clk, rx = await bring_up(dut)
    await wait_for_configured(dut)

    t0 = now_ns()
    while len(rx.bytes) < 16 * 10 and now_ns() - t0 < 600_000:
        await Timer(2000, unit="ns")

    data = rx.stream()
    assert len(data) >= 16 * 9, "only %d bytes on swo in %.0f us" % (len(data), (now_ns() - t0) / 1000)

    # Drop everything before the first delimiter: the capture is joined
    # mid-stream exactly as a board capture is, and a leading fragment is a
    # malformed frame the oracle would (correctly) complain about.
    first = data.find(0x00)
    assert first >= 0, "no COBS delimiter in %d bytes -- the wire carried no frame" % len(data)
    ok, msgs = check_stream(data[first + 1:], min_records=8)
    for m in msgs:
        dut._log.info("oracle: %s", m)
    assert ok, "SoCScope's check_stream rejected the capture: %s" % "; ".join(msgs)


@cocotb.test(skip=(CFG != "split"))
async def test_gpio_is_bit_for_bit_what_the_fielded_overlay_drives(dut):
    """FREEZE_IN_RM=0 must change NOTHING the shell can see on the GPIO bus.

    The state-plane bits exist in this RTL; at the shipped parameterisation they
    must not be driven, or the back-out is a diff rather than a parameter -- and
    dut_gpio_o/oe[15:8] are the CLCD display tunnel, which an RM with no display
    drives 0 (dut-display-tunnel.md)."""
    await bring_up(dut)
    await wait_for_configured(dut)
    assert int(dut.dut_gpio_oe.value) == 0x0001, (
        "dut_gpio_oe = 0x%04X, fielded build drives 0x0001" % int(dut.dut_gpio_oe.value))
    assert int(dut.dut_gpio_o.value) == 0x0001, (
        "dut_gpio_o = 0x%04X, fielded build drives 0x0001 once configured"
        % int(dut.dut_gpio_o.value))


@cocotb.test(skip=(CFG != "split"))
async def test_identity_and_the_inert_boundary(dut):
    """rm_id is the one signal this RM must drive correctly and permanently, and
    the flash pads must sit at SAFE IDLE while it is resident."""
    await bring_up(dut)
    await wait_for_configured(dut)
    assert int(dut.rm_id.value) == 0x01000006, "rm_id = 0x%08X" % int(dut.rm_id.value)
    assert int(dut.qspi_csn.value) == 1, "QSPI flash not deselected"
    assert int(dut.qspi_io_oe.value) == 0, "QSPI lanes driven by a trace-plane RM"
    assert int(dut.qspi_sclk.value) == 0
    assert int(dut.uart_tx_tvalid.value) == 0
    assert int(dut.phy_rmii_tx_en.value) == 0


# =============================================================================
# CFG=burst_split / burst_tied -- C2 and its control
# =============================================================================
async def _run_until_streaming(dut):
    clk, rx = await bring_up(dut)
    await wait_for_configured(dut)
    t0 = now_ns()
    while len(rx.bytes) < 40 and now_ns() - t0 < 400_000:
        await Timer(1000, unit="ns")
    assert len(rx.bytes) >= 40, "the wire never reached a steady stream (%d bytes)" % len(rx.bytes)
    return clk, rx


@cocotb.test(skip=(CFG != "burst_split"))
async def test_the_egress_keeps_draining_while_the_dut_clock_is_stopped(dut):
    """C2. Stop dut_clk mid-capture -- the bench is the static-side gate -- and
    require WHOLE, CRC-GOOD frames to arrive afterwards. Bytes alone would not
    do: a transmitter clocked by a stopped clock emits a stuck level, and a
    receiver sampling a stuck level reports bytes."""
    assert _ORACLE_ERR is None, "SoCScope oracle unavailable (%s)" % _ORACLE_ERR
    clk, rx = await _run_until_streaming(dut)

    stopped = {"v": False}
    clk.hold = lambda: stopped["v"]
    stopped["v"] = True
    t_stop = now_ns()
    edges_before = clk.edges

    await Timer(300_000, unit="ns")
    assert clk.edges == edges_before, "the bench gate leaked %d dut_clk edges" % (clk.edges - edges_before)

    after = rx.stream_after(t_stop)
    assert len(rx.edges_after(t_stop)) > 0, (
        "swo did not move at all after the DUT clock stopped -- the egress is still "
        "on the clock that stops")
    assert len(after) >= 16, (
        "only %d bytes completed after the DUT clock stopped; a frame is 16" % len(after))

    first = after.find(0x00)
    frames, stats, _f = deframe(after[first + 1:])
    assert stats.frames_bad_crc == 0, "%d bad-CRC frames delivered during the freeze" % stats.frames_bad_crc
    assert len(frames) >= 1, (
        "no complete frame decoded from the %d bytes delivered after the DUT clock "
        "stopped" % len(after))
    dut._log.info("C2: %d bytes / %d good frames delivered with dut_clk STOPPED",
                  len(after), len(frames))


@cocotb.test(skip=(CFG != "burst_tied"))
async def test_control_the_single_clock_tie_goes_dead_when_the_dut_clock_stops(dut):
    """C2's negative control, on TRACE_CLK_FREE=0 -- the B1 build. The same stop
    must kill the wire outright. If this ever passes the test above without
    failing here, the domain split was never the thing being measured."""
    clk, rx = await _run_until_streaming(dut)

    stopped = {"v": False}
    clk.hold = lambda: stopped["v"]
    stopped["v"] = True
    t_stop = now_ns()

    await Timer(300_000, unit="ns")
    moved = rx.edges_after(t_stop)
    assert len(moved) == 0, (
        "swo moved %d times after the DUT clock stopped in a SINGLE-CLOCK build -- "
        "the control did not control anything, so the split test proves nothing"
        % len(moved))
    dut._log.info("control: 0 swo transitions in 300 us with dut_clk stopped (tied build)")


# =============================================================================
# CFG=freeze -- the in-RM state plane (the HW-013 alternative)
# =============================================================================
def _hold_of(dut):
    return lambda: _bit(dut.dut_gpio_o, G_HOLD) == 1


@cocotb.test(skip=(CFG != "freeze"))
async def test_the_hold_bit_is_fail_safe_in_the_clamped_direction(dut):
    """The decoupler clamps dut_gpio_o to 0 during a swap. The exported bit is
    HOLD, not ENABLE, so the clamped value means RUN -- otherwise every partial
    reconfiguration would stop the DUT's clock, including the one replacing this
    RM."""
    clk, _rx = await bring_up(dut, hold=_hold_of(dut))
    await wait_for_configured(dut)
    assert _bit(dut.dut_gpio_o, G_HOLD) == 0, "HOLD asserted with nothing frozen"
    assert _bit(dut.dut_gpio_o, G_FROZEN) == 0
    assert _bit(dut.dut_gpio_oe, G_HOLD) == 1, "the hold bit is not driven at all"
    assert int(dut.dut_gpio_oe.value) == 0x001F, (
        "dut_gpio_oe = 0x%04X; FREEZE_IN_RM=1 drives exactly [4:0]" % int(dut.dut_gpio_oe.value))


@cocotb.test(skip=(CFG != "freeze"))
async def test_freeze_stops_the_gated_clock_and_run_restarts_it(dut):
    """FREEZE from the switches stops the gated clock; RUN brings it back. The
    command plane is on the TRACE clock, so the release works WHILE frozen --
    HW-009's trap is that a command surface on the stopped clock can freeze
    exactly once, for ever."""
    clk, _rx = await bring_up(dut, hold=_hold_of(dut))
    await wait_for_configured(dut)

    await issue(dut, C_FREEZE)
    await Timer(200, unit="ns")
    assert _bit(dut.dut_gpio_o, G_FROZEN) == 1, "FREEZE did not take"
    assert _bit(dut.dut_gpio_o, G_HOLD) == 1, "frozen, but the exported hold says run"

    before = clk.edges
    await Timer(3000, unit="ns")            # well inside the watchdog (600 trace clocks)
    assert clk.edges == before, "%d dut_clk edges delivered while frozen" % (clk.edges - before)

    await issue(dut, C_RUN)
    await Timer(1000, unit="ns")
    assert clk.edges > before, "RUN did not restart the clock -- issued from the trace domain"
    assert _bit(dut.dut_gpio_o, G_FROZEN) == 0


@cocotb.test(skip=(CFG != "freeze"))
async def test_step_delivers_exactly_one_cycle(dut):
    """C1 at N=1, measured on the gated clock. `step_rem` decrements on DELIVERED
    edges rather than requested ones, and this is the measurement that can tell
    the difference."""
    clk, _rx = await bring_up(dut, hold=_hold_of(dut))
    await wait_for_configured(dut)

    await issue(dut, C_FREEZE)
    await Timer(200, unit="ns")
    before = clk.edges
    await issue(dut, C_STEP)
    await Timer(2000, unit="ns")
    delivered = clk.edges - before
    assert delivered == STEP_N, "STEP delivered %d dut_clk cycles, asked for %d" % (delivered, STEP_N)
    await issue(dut, C_RUN)


@cocotb.test(skip=(CFG != "freeze"))
async def test_no_freeze_outlasts_the_watchdog(dut):
    """The whole safety argument for a state plane whose only command path is
    three DIP switches: a freeze nobody releases releases itself, and the capture
    says it was a timeout rather than a request."""
    clk, _rx = await bring_up(dut, hold=_hold_of(dut))
    await wait_for_configured(dut)

    await issue(dut, C_FREEZE)
    await Timer(200, unit="ns")
    before = clk.edges
    # WDOG is in TRACE clocks; wait comfortably past it and never send RUN.
    await Timer((WDOG + 400) * TRACE_PERIOD_NS, unit="ns")
    assert clk.edges > before, "the watchdog did not release the freeze after %d trace clocks" % WDOG
    assert _bit(dut.dut_gpio_o, G_WDOG_FIRED) == 1, (
        "the clock ran again but wdog_fired is clear -- a capture cannot tell a "
        "timeout from a release")
    assert _bit(dut.dut_gpio_o, G_HOLD) == 0


# =============================================================================
# CFG=freeze_n -- C1's exactness, and the condition it depends on
# =============================================================================
@cocotb.test(skip=(CFG != "freeze_n"))
async def test_step_delivers_exactly_n_while_the_two_clocks_match(dut):
    """C1 at N=1000 with dut_clk at its 50 MHz default: exactly N."""
    clk, _rx = await bring_up(dut, hold=_hold_of(dut))
    await wait_for_configured(dut)

    await issue(dut, C_FREEZE)
    await Timer(200, unit="ns")
    before = clk.edges
    await issue(dut, C_STEP)
    await Timer((STEP_N + 500) * TRACE_PERIOD_NS, unit="ns")
    delivered = clk.edges - before
    assert delivered == STEP_N, "STEP delivered %d dut_clk cycles, asked for %d" % (delivered, STEP_N)


@cocotb.test(skip=(CFG != "freeze_n"))
async def test_step_is_NOT_exact_once_the_dut_clock_is_retuned(dut):
    """THE FINDING, and it is the one that decides HW-013's open question.

    socscope_freeze counts DELIVERED ENABLE CYCLES on `clk_free`. In the in-RM
    option `clk_free` is `phy_rmii_ref_clk`, which is NOT the clock being gated:
    the gate sits on dut_clk, a DRP-RECONFIGURABLE output of a different MMCM
    (partition-pins.md). "N enable cycles" is then a WINDOW of N trace periods,
    and the number of dut_clk edges inside it is N only while the two clocks are
    the same frequency. Retune dut_clk -- which this platform does; HW-008
    records a set_clk to 25 MHz on this very board -- and the step silently
    delivers a different number of cycles, which is precisely the failure C1's
    acceptance test exists to catch.

    Static-side, the engine runs on the ungated source of the clock it gates and
    the question does not arise. This test is the measurement behind the
    recommendation, not an opinion about it."""
    clk, _rx = await bring_up(dut, hold=_hold_of(dut), dut_period_ns=2 * DUT_PERIOD_NS)
    await wait_for_configured(dut)

    await issue(dut, C_FREEZE)
    await Timer(400, unit="ns")
    before = clk.edges
    await issue(dut, C_STEP)
    await Timer((STEP_N + 500) * TRACE_PERIOD_NS, unit="ns")
    delivered = clk.edges - before
    dut._log.info("retuned dut_clk (25 MHz): STEP %d delivered %d cycles", STEP_N, delivered)
    assert delivered != STEP_N, (
        "the retuned clock delivered exactly N -- if this ever passes, the in-RM "
        "freeze is exact after all and the finding must be retracted")
    assert abs(delivered - STEP_N // 2) <= 2, (
        "expected about N/2 = %d cycles at half rate, got %d" % (STEP_N // 2, delivered))
