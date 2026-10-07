"""tests/rm_uart_echo/test_rm_uart_echo.py

Verifies `fpga/dfx/rms/rm_uart_echo/rm_uart_echo.sv` — the first DFX
Reconfigurable Module that moves DATA across the RP boundary rather than
just exposing a constant register. It drives the partition-pins.md
"Console / trace" AXI-Stream byte pair:

  * uart_tx_* : DUT -> host (this RM is the SOURCE) — banner, then echoes.
  * uart_rx_* : host -> DUT (this RM is the SINK)   — every accepted byte
                is echoed back on uart_tx.

Bench scope note (honesty about coverage): before this bench, the
`fpga/dfx/rms/` RMs (greybox / led / regdemo_a / regdemo_b) had lint-only
coverage — none had a testbench. This is the simplest bench that actually
PROVES the three behaviours the RM exists to demonstrate, all in the single
`dut_clk` domain the RM lives in (the RP-boundary AXIS is already
dut_clk-domain — the shell's uart_bridge owns the CDC, so there is no
CDC to model here):

  1. RESET BANNER (test_reset_banner): out of reset the RM emits the exact
     ASCII banner on uart_tx, with a host that is always ready. Proves
     DUT->host with no host->DUT path.
  2. ECHO ROUND TRIP (test_echo_roundtrip): after the banner drains, bytes
     driven on uart_rx come back byte-for-byte, in order, on uart_tx.
  3. BACKPRESSURE, NO LOSS (test_backpressure_mid_banner_no_loss): stalling
     uart_tx_tready mid-banner holds uart_tx_tvalid HIGH and holds tdata on
     the same head byte (AXIS: tvalid must not drop before tready), and when
     tready returns the banner is intact and in order — nothing lost.
  4. ECHO UNDER BACKPRESSURE (test_echo_under_backpressure_no_loss): a
     payload LONGER than the RM's internal FIFO, echoed while the host
     drain deliberately stalls, so the FIFO fills, uart_rx_tready deasserts,
     the source stalls, and NOT ONE byte is lost — the whole point of the
     skid/FIFO buffer.

What it does NOT cover (stated plainly): this is a functional dut_clk-domain
bench, not a DFX-integration or on-silicon proof. It does not exercise the
shell's uart_bridge async FIFOs / the dut_clk<->s_axi_aclk CDC (that is
tests/uart_bridge's job), does not run the partial-reconfig swap, and does
not assert timing. `swo`, the RMII/MDIO/SWD/GPIO tie-offs and rm_id are not
re-verified here (they are byte-identical to rm_led/rm_greybox, already
covered by lint + the DFX pr_verify boundary check).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import (ClockCycles, NextTimeStep, ReadOnly, RisingEdge,
                             Timer)

from axis import AxisByteDriver
from dut_presence import rtl_ready

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "dfx", "rms", "rm_uart_echo")
NO_RTL = not rtl_ready(_RTL_DIR, ["rm_uart_echo.sv"])

# Must match the RTL's BANNER localparam byte-for-byte.
BANNER = b"rm_uart_echo ready\r\n"


async def _bring_up(dut):
    """Start the single dut_clk, drive an async-assert / sync-release reset
    (the shell delivers dut_resetn already deassert-synchronized; the RM
    uses it directly), and park every unused input at a benign level."""
    cocotb.start_soon(Clock(dut.dut_clk, 10, units="ns").start())

    dut.dut_resetn.value = 0
    dut.rp_resetn.value = 0
    dut.dbg_resetn.value = 0

    # Unused inputs — parked (the RM ties their consumers off inert).
    dut.swd_clk.value = 0
    dut.swd_dio_o.value = 0
    dut.swd_dio_oe.value = 0
    dut.phy_rmii_ref_clk.value = 0
    dut.phy_rmii_crs_dv.value = 0
    dut.phy_rmii_rxd.value = 0
    dut.mdio_i.value = 0
    dut.dut_gpio_i.value = 0

    # Console pins — host idle: not ready to receive, nothing to send.
    dut.uart_tx_tready.value = 0
    dut.uart_rx_tdata.value = 0
    dut.uart_rx_tvalid.value = 0

    await Timer(40, units="ns")
    await RisingEdge(dut.dut_clk)
    dut.dut_resetn.value = 1
    dut.rp_resetn.value = 1
    dut.dbg_resetn.value = 1
    await RisingEdge(dut.dut_clk)


async def _collect_tx(dut, n, timeout=8000):
    """Collect exactly n bytes from the uart_tx AXIS output with an
    always-ready host. Pre-edge sampling (tests/uart_bridge/dut_notes.md):
    a beat commits AT the edge iff tvalid && tready held going INTO it, so
    sample in the ReadOnly window BEFORE the edge. Leaves tready low."""
    out = []
    await RisingEdge(dut.dut_clk)
    dut.uart_tx_tready.value = 1
    for _ in range(timeout):
        await ReadOnly()
        beat_valid = int(dut.uart_tx_tvalid.value)
        beat_data = int(dut.uart_tx_tdata.value)
        await RisingEdge(dut.dut_clk)
        if beat_valid:
            out.append(beat_data)
            if len(out) == n:
                break
    dut.uart_tx_tready.value = 0  # writable phase (post-edge)
    assert len(out) == n, f"uart_tx collect timed out: got {len(out)}/{n}"
    return out


async def _drain_with_stalls(dut, n, timeout=40000):
    """Collect n bytes from uart_tx while deliberately STALLING tready every
    few cycles — creates real backpressure so the RM's FIFO can fill. A beat
    counts only when tvalid && tready both held pre-edge."""
    out = []
    i = 0
    await RisingEdge(dut.dut_clk)
    while len(out) < n and i < timeout:
        i += 1
        dut.uart_tx_tready.value = 0 if (i % 5 == 0) else 1  # stall 1-in-5
        await ReadOnly()
        beat_valid = int(dut.uart_tx_tvalid.value)
        beat_data = int(dut.uart_tx_tdata.value)
        beat_ready = int(dut.uart_tx_tready.value)
        await RisingEdge(dut.dut_clk)
        if beat_valid and beat_ready:
            out.append(beat_data)
    dut.uart_tx_tready.value = 0
    assert len(out) == n, f"uart_tx stalled-drain timed out: got {len(out)}/{n}"
    return out


@cocotb.test(skip=NO_RTL)
async def test_reset_banner(dut):
    """Out of reset, the RM emits exactly BANNER on uart_tx (DUT->host,
    no host->DUT path exercised)."""
    await _bring_up(dut)
    got = await _collect_tx(dut, len(BANNER))
    assert bytes(got) == BANNER, (
        f"reset banner mismatch: got {bytes(got)!r}, want {BANNER!r}")


@cocotb.test(skip=NO_RTL)
async def test_echo_roundtrip(dut):
    """After the banner drains, every uart_rx byte comes back on uart_tx,
    in order (host->DUT->host round trip)."""
    await _bring_up(dut)
    banner = await _collect_tx(dut, len(BANNER))
    assert bytes(banner) == BANNER, "banner must drain before echo phase"

    payload = bytes([0x00, 0x55, 0xAA, 0xFF, 0x41, 0x42, 0x43, 0x0D, 0x0A])
    drv = AxisByteDriver(dut.dut_clk, dut.uart_rx_tdata,
                         dut.uart_rx_tvalid, dut.uart_rx_tready)
    await drv.write(payload)                 # source (fits the FIFO)
    got = await _collect_tx(dut, len(payload))
    assert bytes(got) == payload, (
        f"echo mismatch: got {bytes(got)!r}, want {payload!r}")


@cocotb.test(skip=NO_RTL)
async def test_backpressure_mid_banner_no_loss(dut):
    """Stalling uart_tx_tready mid-banner: uart_tx_tvalid must HOLD high and
    uart_tx_tdata must HOLD the same head byte while stalled (AXIS: tvalid
    can't drop before tready), and the full banner must survive the stall
    intact and in order (no byte lost)."""
    await _bring_up(dut)

    HALT_AT = 6
    first = await _collect_tx(dut, HALT_AT)      # leaves tready low
    assert bytes(first) == BANNER[:HALT_AT]

    # tready is now low with more banner pending: tvalid holds high, tdata
    # holds the next byte (BANNER[HALT_AT]) steady across many cycles.
    for _ in range(10):
        await RisingEdge(dut.dut_clk)
        await ReadOnly()
        assert int(dut.uart_tx_tvalid.value) == 1, (
            "uart_tx_tvalid must stay HIGH under backpressure with data pending")
        assert int(dut.uart_tx_tdata.value) == BANNER[HALT_AT], (
            "uart_tx_tdata head must hold stable while tready is low")
        await NextTimeStep()

    rest = await _collect_tx(dut, len(BANNER) - HALT_AT)
    assert bytes(first) + bytes(rest) == BANNER, (
        f"banner corrupted/lost across the stall: "
        f"{(bytes(first) + bytes(rest))!r} != {BANNER!r}")


@cocotb.test(skip=NO_RTL)
async def test_echo_under_backpressure_no_loss(dut):
    """A payload LONGER than the RM's internal FIFO, echoed while the host
    drain stalls 1-in-5 cycles. The FIFO fills, uart_rx_tready deasserts,
    the source stalls — and every byte still comes back, in order. This is
    the whole reason the RM has a FIFO/skid buffer."""
    await _bring_up(dut)
    banner = await _collect_tx(dut, len(BANNER))
    assert bytes(banner) == BANNER

    # 100 bytes > the RTL's 64-deep FIFO, so the source is guaranteed to hit
    # uart_rx_tready deassertion at least once.
    payload = bytes([(i * 7 + 3) & 0xFF for i in range(100)])

    got_holder = {}

    async def _collector():
        got_holder["got"] = await _drain_with_stalls(dut, len(payload))

    col = cocotb.start_soon(_collector())
    drv = AxisByteDriver(dut.dut_clk, dut.uart_rx_tdata,
                         dut.uart_rx_tvalid, dut.uart_rx_tready)
    await drv.write(payload)
    await col

    got = got_holder["got"]
    assert bytes(got) == payload, (
        f"echo-under-backpressure lost/reordered bytes: got {len(got)} bytes, "
        f"first mismatch at "
        f"{next((i for i in range(min(len(got), len(payload))) if got[i] != payload[i]), 'n/a')}")

    # After the flurry, the RM must settle idle: tvalid low (FIFO drained).
    await ClockCycles(dut.dut_clk, 4)
    await ReadOnly()
    assert int(dut.uart_tx_tvalid.value) == 0, (
        "uart_tx_tvalid must fall once the echo FIFO is drained")
