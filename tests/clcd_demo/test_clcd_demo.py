"""test_clcd_demo.py -- the cocotb bench for RM `clcd_demo`.

DUT = `rp_clcd_demo_wrapper`, the WHOLE RM, elaborated at the real partition
boundary. The bench watches only `dut_gpio_o` / `dut_gpio_oe` -- the two vectors
that actually cross into the shell -- because the tunnel BIT MAP is precisely
the thing that has never been exercised from the RM side, and a bench that
peeked at `u_demo.lcd_cs` would prove nothing about it.

Between the RM and the glass sit two things this bench models
(`tunnel_model.py`): the KVM, which inverts the active-high tunnel strobes to
the panel's active-low pads and would refuse or mangle an unsafe cycle, and the
HX8347-D, which latches the bus on the WR_n RISING edge while CS_n is asserted.

What it asserts, in the order the RM does it:
  1. the byte stream is the FIRMWARE's init table, exactly (card_model, which
     reads firmware/clcd/hx8347_init.c -- nothing here is retyped);
  2. then the address window and the GRAM-write command;
  3. then a whole test card, pixel for pixel;
  4. then the frame counter INCREMENTS -- the difference between "the DUT is
     drawing" and "one stale frame is on the glass";
  5. every strobe meets docs/contracts/dut-display-tunnel.md section 5;
  6. every RESERVED tunnel bit is 0 and `req` rises exactly once;
  7. `busy` and `cs` are simultaneously low between bytes, so the KVM's
     safe-switch gate is satisfied after EVERY byte and no handover has to be
     forced by the 1 ms hung-owner timeout.

The CONTROL -- that these checks can fail -- is `test_tunnel_checker.py` (pure
pytest, no simulator) plus `make MODE=control` here, which rebuilds the RM with
sub-floor 8080 timing and passes only if the bench REJECTS it.

Geometry and clock are shrunk by the Makefile (`-pvalue+`): a 320x240 frame is
153,600 bytes and ~4 million dut_clk cycles.
"""
from __future__ import annotations

import os
import pathlib
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import card_model  # noqa: E402
from tunnel_model import (  # noqa: E402
    RS_CMD, RS_DATA, Sample, TunnelChecker, TunnelViolation, split,
)

# The elaboration the Makefile built. Kept in ONE place so a mismatch between
# the -pvalue+ line and the bench's expectations is a single edit, not a hunt.
PIX_W = int(os.environ.get("CLCD_DEMO_PIX_W", "32"))
PIX_H = int(os.environ.get("CLCD_DEMO_PIX_H", "16"))
CLK_HZ = int(os.environ.get("CLCD_DEMO_CLK_HZ", "1000"))
EXPECT_VIOLATION = os.environ.get("CLCD_DEMO_EXPECT_VIOLATION", "0") == "1"

CLK_PERIOD_NS = 20                 # the bench's dut_clk period; CLK_HZ is what
                                   # the RM BELIEVES it is, and is what scales
                                   # the firmware table's millisecond delays.


class Bench:
    """Clock, reset, and a per-dut_clk-cycle recorder of the two tunnel vectors."""

    def __init__(self, dut):
        self.dut = dut
        self.samples: list[Sample] = []
        self._stop = False

    async def start(self, freeze: bool = False):
        d = self.dut
        cocotb.start_soon(Clock(d.dut_clk, CLK_PERIOD_NS, units="ns").start())
        d.dut_resetn.value = 0
        d.rp_resetn.value = 0
        d.dbg_resetn.value = 0
        # dut_gpio_i[15:8] = USER_SW, [7:0] = a loopback of the DUT's own LED
        # drive (shell_top.sv:463). Bit 8 is USER_SW[0] = freeze.
        d.dut_gpio_i.value = (1 << 8) if freeze else 0
        # Inputs the RM does not consume, driven to something legal anyway.
        for name, val in (("jtag_tck", 0), ("jtag_tms", 0), ("jtag_tdi", 0),
                          ("phy_rmii_ref_clk", 0), ("phy_rmii_crs_dv", 0),
                          ("phy_rmii_rxd", 0), ("mdio_i", 0),
                          ("uart_tx_tready", 0), ("uart_rx_tdata", 0),
                          ("uart_rx_tvalid", 0), ("qspi_io_i", 0)):
            getattr(d, name).value = val
        await ClockCycles(d.dut_clk, 5)
        d.dut_resetn.value = 1
        d.rp_resetn.value = 1
        d.dbg_resetn.value = 1
        cocotb.start_soon(self._record())

    async def _record(self):
        d = self.dut
        while not self._stop:
            await RisingEdge(d.dut_clk)
            await ReadOnly()
            try:
                o, oe = int(d.dut_gpio_o.value), int(d.dut_gpio_oe.value)
            except ValueError:
                continue                      # x/z during reset -- not a sample
            self.samples.append(split(o, oe))

    def stop(self):
        self._stop = True

    def decode(self, **kw) -> TunnelChecker:
        """Decode everything up to the last cycle the bus was released.

        The recorder stops on a wall-clock budget, so its last sample may sit
        inside a live 8080 cycle. Feeding that to the checker would report a
        truncated strobe -- a real fault, but here an artefact of when we
        stopped looking, and it would mask the fault the check exists for."""
        last_idle = max((i for i, s in enumerate(self.samples) if not s.cs),
                        default=len(self.samples) - 1)
        ck = TunnelChecker(**kw)
        ck.feed(self.samples[:last_idle + 1])
        return ck

    def led_mirror(self) -> int:
        """dut_gpio_o[7:0] & dut_gpio_oe[7:0] -- what shell_top turns into
        USER_nLED (shell_top.sv:461-462)."""
        o, oe = int(self.dut.dut_gpio_o.value), int(self.dut.dut_gpio_oe.value)
        return o & oe & 0xFF


def _bytes_per_frame() -> int:
    table, defines = card_model.load_firmware_table()
    return len(card_model.frame_sequence(table, defines, PIX_W, PIX_H, 0))


async def _run_frames(dut, n_frames: int, freeze: bool = False) -> Bench:
    """Run long enough for n_frames complete repaints, plus slack for the
    firmware table's millisecond delays."""
    b = Bench(dut)
    await b.start(freeze=freeze)
    table, defines = card_model.load_firmware_table()
    ms_total = sum(e.val for e in table if e.op == defines["HX_DLY"])
    ticks = max(1, CLK_HZ // 1000)
    per_frame = _bytes_per_frame() * (8 + 8 + 8 + 4) + ms_total * ticks
    await ClockCycles(dut.dut_clk, int(per_frame * n_frames * 1.15) + 500)
    b.stop()
    await ClockCycles(dut.dut_clk, 2)
    return b


# ===========================================================================
# 1-3.  The sequence: firmware init table, address window, one whole test card
# ===========================================================================
@cocotb.test(skip=EXPECT_VIOLATION)
async def test_first_frame_is_the_expected_sequence(dut):
    """Byte for byte: the firmware's init table, then the window program, then
    frame 0 of the test card. Nothing about the expected stream is written in
    this bench -- it comes from card_model, which reads the firmware."""
    b = await _run_frames(dut, 1)
    ck = b.decode()
    table, defines = card_model.load_firmware_table()
    want = card_model.frame_sequence(table, defines, PIX_W, PIX_H, frame=0)
    got = ck.sequence[:len(want)]
    assert got == want, (
        "the RM's byte stream diverges from the firmware-derived expectation:\n"
        + card_model.diff_sequences(got, want))
    dut._log.info(f"first frame OK: {len(want)} bytes "
                  f"({PIX_W}x{PIX_H}), {len(ck.strobes)} strobes decoded")


@cocotb.test(skip=EXPECT_VIOLATION)
async def test_init_table_is_the_firmwares(dut):
    """Stated separately from the frame test so a failure says WHICH half
    broke: a bad init table is a dead panel, a bad card is a wrong picture."""
    b = await _run_frames(dut, 1)
    ck = b.decode()
    table, defines = card_model.load_firmware_table()
    want = card_model.init_sequence(table, defines)
    assert ck.sequence[:len(want)] == want, card_model.diff_sequences(
        ck.sequence[:len(want)], want)
    # ...and the MADCTL rotation the firmware ships, which is what decides
    # whether the photograph is the right way up.
    pairs = [(ck.sequence[i], ck.sequence[i + 1])
             for i in range(len(want) - 1)
             if ck.sequence[i] == (RS_CMD, defines["HX_REG_MADCTL"])]
    assert pairs and pairs[-1][1] == (RS_DATA, defines["HX_MADCTL_VALUE"])


# ===========================================================================
# 4.  Liveness: the counter increments, and the LEDs mirror it
# ===========================================================================
@cocotb.test(skip=EXPECT_VIOLATION)
async def test_frame_counter_increments(dut):
    """THE point of the RM. A stale frame left by the harness and a live DUT
    look identical on the glass unless something changes between two looks."""
    b = await _run_frames(dut, 3)
    ck = b.decode()
    table, defines = card_model.load_firmware_table()

    per_frame = _bytes_per_frame()
    frames = [ck.sequence[k * per_frame:(k + 1) * per_frame]
              for k in range(len(ck.sequence) // per_frame)]
    assert len(frames) >= 2, (
        f"only {len(frames)} complete frame(s) in {len(ck.sequence)} bytes -- "
        f"the bench did not run long enough to see the counter move")

    for n, frame in enumerate(frames[:2]):
        want = card_model.frame_sequence(table, defines, PIX_W, PIX_H, frame=n)
        assert frame == want, (
            f"frame {n} is not the test card for counter value {n}:\n"
            + card_model.diff_sequences(frame, want))
    dut._log.info(f"counter live: {len(frames)} frames, values 0..{len(frames)-1}")


@cocotb.test(skip=EXPECT_VIOLATION)
async def test_leds_mirror_the_counter(dut):
    """The second, independent readout in the same photograph. If the LEDs
    count and the panel does not, the RM is alive and the failure is in the
    tunnel/KVM/panel; if neither moves, the RM never started."""
    b = await _run_frames(dut, 2)
    ck = b.decode()
    per_frame = _bytes_per_frame()
    n_frames = len(ck.sequence) // per_frame
    assert n_frames >= 1
    # dut_gpio_oe[7:0] must be all ones -- otherwise shell_top's
    # `led_drive = pad_o & pad_oe` masks the mirror away entirely.
    assert int(dut.dut_gpio_oe.value) & 0xFF == 0xFF, (
        "dut_gpio_oe[7:0] is not all-ones; USER_nLED is driven from "
        "pad_o & pad_oe (shell_top.sv:461), so the LED mirror would be dark")
    assert b.led_mirror() == n_frames & 0xFF


@cocotb.test(skip=EXPECT_VIOLATION)
async def test_freeze_stops_the_counter_but_not_the_repaint(dut):
    """USER_SW[0] -> dut_gpio_i[8]. The on-board negative control: if flipping
    it does NOT stop the number, the number is not coming from this RM."""
    b = await _run_frames(dut, 3, freeze=True)
    ck = b.decode()
    table, defines = card_model.load_firmware_table()
    per_frame = _bytes_per_frame()
    frames = [ck.sequence[k * per_frame:(k + 1) * per_frame]
              for k in range(len(ck.sequence) // per_frame)]
    assert len(frames) >= 2, "not enough frames to tell a frozen counter apart"
    want0 = card_model.frame_sequence(table, defines, PIX_W, PIX_H, frame=0)
    for n, frame in enumerate(frames[:2]):
        assert frame == want0, (
            f"frame {n} moved while freeze was asserted:\n"
            + card_model.diff_sequences(frame, want0))
    assert b.led_mirror() == 0


# ===========================================================================
# 5-7.  The tunnel's own rules
# ===========================================================================
@cocotb.test(skip=EXPECT_VIOLATION)
async def test_every_strobe_meets_the_contract_floor(dut):
    """docs/contracts/dut-display-tunnel.md section 5. The decode in
    test_first_frame already enforces this (TunnelChecker raises), but stating
    it as its own test makes the failure name the right document."""
    b = await _run_frames(dut, 1)
    ck = b.decode()
    assert ck.strobes, "no 8080 write cycles at all"
    worst_setup = min(s.setup_cycles for s in ck.strobes)
    worst_wr = min(s.wr_cycles for s in ck.strobes)
    worst_hold = min(s.hold_cycles for s in ck.strobes)
    dut._log.info(f"worst-case phases: setup={worst_setup} wr={worst_wr} "
                  f"hold={worst_hold} dut_clk cycles (floor 8)")
    assert worst_setup >= 8 and worst_wr >= 8 and worst_hold >= 8


@cocotb.test(skip=EXPECT_VIOLATION)
async def test_reserved_bits_are_zero_and_req_rises_once(dut):
    """RESERVED bits are 0 -- the same value the decoupler clamps to, which is
    what makes a decoupled RP and this RM indistinguishable to the KVM.
    `req` rises once and never falls: a FALLING edge means "you can have the
    panel back" (docs/contracts/dut-display-tunnel.md section 2), and a proof
    RM must not hand it away on its own."""
    b = await _run_frames(dut, 1)
    ck = b.decode()                              # raises on any RESERVED bit
    assert all(s.reserved == 0 for s in b.samples)
    rises = [e for e in ck.req_edges if e[2] == 1]
    falls = [e for e in ck.req_edges if e[2] == 0]
    assert len(rises) == 1, f"req rose {len(rises)} times, expected exactly 1"
    assert not falls, f"req FELL at cycle(s) {[e[0] for e in falls]}"


@cocotb.test(skip=EXPECT_VIOLATION)
async def test_the_kvm_gets_a_handover_point_after_every_byte(dut):
    """The KVM's safe-switch gate is `!cs && !busy` (clcd_kvm README section 6).
    This RM keeps at most one byte outstanding, so that pair must be true
    between EVERY pair of consecutive bytes. If it is not, every handover away
    from the DUT waits out the 1 ms hung-owner timeout and is FORCED -- which
    still works, but stops being a clean handover and stops being what the KVM
    bench proved."""
    b = await _run_frames(dut, 1)
    quiet = [i for i, s in enumerate(b.samples) if not s.cs and not s.busy]
    ck = b.decode()
    assert len(ck.strobes) > 20
    latch = [s.at_cycle for s in ck.strobes]
    qs = set(quiet)
    gaps = 0
    for a, c in zip(latch, latch[1:]):
        if not any(n in qs for n in range(a, c)):
            gaps += 1
    assert gaps == 0, (
        f"{gaps} consecutive byte pairs with NO quiescent (!cs && !busy) cycle "
        f"between them -- the KVM would have to force those handovers")


# ===========================================================================
# The RTL-level control: `make MODE=control` builds the RM with sub-floor 8080
# timing, and this test passes only if the bench REJECTS it.
# ===========================================================================
@cocotb.test(skip=not EXPECT_VIOLATION)
async def test_control_sub_floor_timing_is_rejected(dut):
    b = await _run_frames(dut, 1)
    try:
        b.decode()
    except TunnelViolation as exc:
        dut._log.info(f"CONTROL OK -- the bench rejected the mutated RM: {exc}")
        return
    raise AssertionError(
        "the RM was built with 8080 phases BELOW the "
        "docs/contracts/dut-display-tunnel.md section 5 floor and the bench "
        "accepted it. The timing check is not load-bearing.")
