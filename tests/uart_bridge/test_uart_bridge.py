"""tests/uart_bridge/test_uart_bridge.py

Verifies `fpga/shell/ip/uart_bridge/uart_bridge.sv` (+ its two `include`d
helpers `uartbr_async_fifo.sv` / `swo_uart_rx.sv`) — the UARTBR
console-stream bridge (shell-regmap.md v0.1 @ 0x44A9_0000, I7). This block
owns the partition-boundary CDC for the whole console group, so the bench
runs `s_axi_aclk` and `dut_clk_i` at genuinely different, unrelated
periods (10 ns vs 7 ns) to make the five gray-pointer async FIFOs earn
their keep — per the block README's "Bench notes for A5".

Register-naming reminder (the crossover documented in the README): the
regmap names registers from the MicroBlaze's perspective, the partition
pins from the DUT's. So the DUT's `uart_tx_*` AXIS OUTPUT lands in the
`U0_RX` read window, and `U0_TX` writes emerge on the DUT's `uart_rx_*`
AXIS INPUT.

What this proves:
  1. U0 DUT→host: AXIS bytes driven on `uart_tx_*` (dut_clk) cross the
     CDC and pop — destructively, in order, `[8]`-valid-qualified — from
     U0_RX; an empty-window read returns {valid=0, 0} WITHOUT popping or
     corrupting anything.
  2. U0 host→DUT: U0_TX writes emerge on `uart_rx_*` in order with proper
     tvalid/tready handshakes; tvalid/tdata hold stable under tready
     backpressure; fill-to-full sets FIFO_STATUS.u0_tx_full and further
     pushes are silently dropped (the documented drop-on-full policy).
  3. U1: same-shape smoke both directions through the reserved seam ports
     (nothing else drives them in v0).
  4. FIFO_STATUS: reset value (all empties set, no fulls), per-stream bit
     positions (RTL ambiguity #4's chosen layout), reserved/unmapped
     in-window offsets read 0, RO writes accepted-no-effect.
  5. SWO path: SWO_CFG divisor+enable in one write; an 8N1 idle-high
     serial stream on `swo_i` at (divisor+1)·dut_clk bit period arrives
     byte-for-byte via SWO_RX; a broken stop bit sets sticky frame_err
     [31] and discards the byte; > FIFO_DEPTH un-drained bytes set sticky
     overflow [30] with drop-newest; both stickies clear on enable=0; a
     new divisor takes effect via the documented enable off/on toggle.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, NextTimeStep, ReadOnly, RisingEdge, Timer

from axis import AxisByteDriver
from dut_presence import rtl_ready
from random_stim import (RandomAxiMaster, random_axi_burst, seeded_rng,
                         coprime_period_pairs)
from regmap import (
    AxiLiteMaster,
    UARTBR_U0_DATA, UARTBR_U1_DATA, UARTBR_SWO_RX, UARTBR_FIFO_STATUS,
    UARTBR_SWO_CFG, UARTBR_DATA_VALID,
    UARTBR_STATUS_U0_TX_FULL, UARTBR_STATUS_U0_RX_EMPTY,
    UARTBR_STATUS_U1_TX_FULL, UARTBR_STATUS_U1_RX_EMPTY,
    UARTBR_STATUS_SWO_RX_EMPTY,
    UARTBR_SWO_CFG_ENABLE, UARTBR_SWO_CFG_OVERFLOW, UARTBR_SWO_CFG_FRAME_ERR,
)

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "uart_bridge")
NO_RTL = not rtl_ready(
    _RTL_DIR, ["uart_bridge.sv", "uartbr_async_fifo.sv", "swo_uart_rx.sv"])

FIFO_DEPTH = 16  # the RTL's default parameter (power of two, >= 4)


async def _bring_up(dut):
    # Deliberately unrelated clock periods (README bench note): the AXI
    # side at 10 ns, the DUT side at 7 ns — every payload byte and both
    # SWO sticky bits must cross the 10/7 boundary through real CDC.
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    cocotb.start_soon(Clock(dut.dut_clk_i, 7, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.uart_tx_tdata_i.value = 0
    dut.uart_tx_tvalid_i.value = 0
    dut.uart_rx_tready_i.value = 0
    dut.swo_i.value = 1            # 8N1 idle-high
    dut.uart1_tx_tdata_i.value = 0
    dut.uart1_tx_tvalid_i.value = 0
    dut.uart1_rx_tready_i.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    # Hold reset > 3 cycles of the SLOWER clock so the dut-domain reset
    # synchronizer has genuinely seen the assertion (README bench note).
    await Timer(60, units="ns")
    dut.s_axi_aresetn.value = 1
    # ... and let the 3-FF dut-domain release synchronizer refill.
    await ClockCycles(dut.dut_clk_i, 5)
    await RisingEdge(dut.s_axi_aclk)
    return AxiLiteMaster.from_dut(dut)


async def _poll_status(axi, mask: int, want_set: bool, tries: int = 100) -> int:
    """Poll FIFO_STATUS until (val & mask) reaches the wanted state —
    empty/full flags are registered in their own domains and cross the
    CDC with a couple cycles of conservative lag, so a single read after
    an event on the other clock would be a race."""
    val = None
    for _ in range(tries):
        val, _ = await axi.read(UARTBR_FIFO_STATUS)
        if bool(val & mask) == want_set:
            return val
    raise AssertionError(
        f"FIFO_STATUS bit {mask:#x} never became {want_set} (last read {val:#x})")


async def _pop_expect(axi, offset: int, expect_byte: int):
    """One DESTRUCTIVE data-window read; assert {valid=1, byte}. Never
    re-reads to 'confirm' (README: every read of 0x00/0x08/0x10 pops)."""
    val, _ = await axi.read(offset)
    assert val & UARTBR_DATA_VALID, (
        f"read of {offset:#x} expected valid=1 + {expect_byte:#04x}, got {val:#x}")
    assert val & 0xFF == expect_byte, (
        f"read of {offset:#x}: expected byte {expect_byte:#04x}, got {val & 0xFF:#04x}")
    assert val & ~0x1FF == 0, f"bits [31:9] must read 0, got {val:#x}"


async def _pop_expect_empty(axi, offset: int):
    val, _ = await axi.read(offset)
    assert val == 0, (
        f"empty read of {offset:#x} must return {{valid=0, 8'h00}}, got {val:#x}")


async def _axis_collect(dut, tdata, tvalid, tready, n: int, timeout: int = 2000):
    """Collect n bytes from a dut_clk-domain AXIS output, driving its
    tready. Signal writes happen only in writable phases (cocotb 2.x).

    Sampling discipline: a beat commits AT a clock edge if tvalid&&tready
    were high going INTO it, so tdata must be sampled in the ReadOnly
    window BEFORE that edge. (Sampling after the edge sees the FWFT FIFO's
    post-pop state — the NEXT head — which systematically drops the first
    byte; found the hard way against this DUT.)"""
    out = []
    await RisingEdge(dut.dut_clk_i)
    tready.value = 1
    for _ in range(timeout):
        await ReadOnly()                 # settled pre-edge values
        beat_valid = int(tvalid.value)
        beat_data = int(tdata.value)
        await RisingEdge(dut.dut_clk_i)  # the beat (if any) commits here
        if beat_valid:
            out.append(beat_data)
            if len(out) == n:
                break
    tready.value = 0  # writable phase (post-edge callback): no extra pop
    assert len(out) == n, f"AXIS collect timed out: got {len(out)}/{n} bytes"
    return out


# --------------------------------------------------------------------------- #
# SWO 8N1 serial driver (idle-high, LSB first, bit period = bit_cycles
# dut_clk cycles = divisor+1 per the swo_uart_rx timing contract).
# --------------------------------------------------------------------------- #
async def _swo_send(dut, byte: int, bit_cycles: int, stop_bit: int = 1):
    dut.swo_i.value = 0                                   # start bit
    await ClockCycles(dut.dut_clk_i, bit_cycles)
    for i in range(8):                                    # data, LSB first
        dut.swo_i.value = (byte >> i) & 1
        await ClockCycles(dut.dut_clk_i, bit_cycles)
    dut.swo_i.value = stop_bit                            # stop bit (0 = frame error)
    await ClockCycles(dut.dut_clk_i, bit_cycles)
    dut.swo_i.value = 1                                   # back to idle
    await ClockCycles(dut.dut_clk_i, max(2, bit_cycles // 2))


@cocotb.test(skip=NO_RTL)
async def test_u0_dut_to_host_destructive_pops(dut):
    """What this proves: DUT console bytes (AXIS on dut_clk) cross the CDC
    and pop from U0_RX in order with [8] valid set; the pops are
    destructive (each byte seen exactly once); an empty-window read
    returns {valid=0, 0} and does NOT pop/disturb the FIFO."""
    axi = await _bring_up(dut)
    payload = [0xA5, 0x5A, 0x00, 0xFF, 0x42]

    drv = AxisByteDriver(dut.dut_clk_i, dut.uart_tx_tdata_i,
                         dut.uart_tx_tvalid_i, dut.uart_tx_tready_o)
    await drv.write(bytes(payload))

    await _poll_status(axi, UARTBR_STATUS_U0_RX_EMPTY, want_set=False)
    for b in payload:
        await _pop_expect(axi, UARTBR_U0_DATA, b)

    # Drained: the empty flag comes back and an extra read is benign.
    await _poll_status(axi, UARTBR_STATUS_U0_RX_EMPTY, want_set=True)
    await _pop_expect_empty(axi, UARTBR_U0_DATA)
    await _pop_expect_empty(axi, UARTBR_U0_DATA)

    # The empty reads must not have disturbed pointer state: the next
    # byte in still pops cleanly.
    await drv.write(bytes([0x77]))
    await _poll_status(axi, UARTBR_STATUS_U0_RX_EMPTY, want_set=False)
    await _pop_expect(axi, UARTBR_U0_DATA, 0x77)


@cocotb.test(skip=NO_RTL)
async def test_u0_host_to_dut_backpressure_and_drop_on_full(dut):
    """What this proves: U0_TX writes emerge on the DUT's uart_rx_* AXIS
    pins in order; tvalid/tdata hold stable while tready is low
    (backpressure); filling to FIFO_DEPTH sets FIFO_STATUS.u0_tx_full and
    further pushes are silently dropped (README loss policy: firmware
    polls tx_full first); after draining, tvalid deasserts."""
    axi = await _bring_up(dut)
    payload = [(0x30 + i) & 0xFF for i in range(FIFO_DEPTH)]

    # tready held low: nothing drains, so exactly FIFO_DEPTH pushes fill it.
    assert int(dut.uart_rx_tvalid_o.value) == 0, "uart_rx_tvalid_o must idle low"
    for b in payload:
        await axi.write(UARTBR_U0_DATA, b)
    await _poll_status(axi, UARTBR_STATUS_U0_TX_FULL, want_set=True)

    # Push-while-full: silently dropped, protocol still OKAY.
    resp = await axi.write(UARTBR_U0_DATA, 0xEE)
    assert resp == 0
    await axi.write(UARTBR_U0_DATA, 0xEF)
    val, _ = await axi.read(UARTBR_FIFO_STATUS)
    assert val & UARTBR_STATUS_U0_TX_FULL, "still full after dropped pushes"

    # Backpressure hold: with data waiting and tready low, tvalid stays
    # high and tdata holds the head byte across dut_clk cycles.
    for _ in range(5):
        await RisingEdge(dut.dut_clk_i)
        await ReadOnly()
        assert int(dut.uart_rx_tvalid_o.value) == 1, "tvalid must hold under backpressure"
        assert int(dut.uart_rx_tdata_o.value) == payload[0], "head tdata must hold under backpressure"
        await NextTimeStep()

    # Drain the first half, pause (tready low again), then the rest: order
    # preserved, the two dropped bytes never appear.
    got = await _axis_collect(dut, dut.uart_rx_tdata_o, dut.uart_rx_tvalid_o,
                              dut.uart_rx_tready_i, FIFO_DEPTH // 2)
    await ClockCycles(dut.dut_clk_i, 4)
    await ReadOnly()
    assert int(dut.uart_rx_tvalid_o.value) == 1, "tvalid must re-hold at the pause"
    assert int(dut.uart_rx_tdata_o.value) == payload[FIFO_DEPTH // 2]
    await NextTimeStep()
    got += await _axis_collect(dut, dut.uart_rx_tdata_o, dut.uart_rx_tvalid_o,
                               dut.uart_rx_tready_i, FIFO_DEPTH - FIFO_DEPTH // 2)
    assert got == payload, f"host->DUT order/content mismatch: {got} != {payload}"

    # Fully drained: tvalid falls, full flag clears (CDC lag allowed).
    await ClockCycles(dut.dut_clk_i, 4)
    assert int(dut.uart_rx_tvalid_o.value) == 0, "tvalid must fall once drained"
    await _poll_status(axi, UARTBR_STATUS_U0_TX_FULL, want_set=False)


@cocotb.test(skip=NO_RTL)
async def test_u1_same_shape_smoke(dut):
    """What this proves: the U1 register/FIFO path (reserved seam — the
    bench drives the uart1_* ports directly, as nothing else will in v0)
    behaves identically to U0 in both directions, including its
    FIFO_STATUS bits [3] (u1_rx_empty) and the U0/U1 windows being
    genuinely independent FIFOs."""
    axi = await _bring_up(dut)

    # host -> DUT through the U1 window.
    tx_payload = [0x11, 0x22, 0x33]
    for b in tx_payload:
        await axi.write(UARTBR_U1_DATA, b)
    got = await _axis_collect(dut, dut.uart1_rx_tdata_o, dut.uart1_rx_tvalid_o,
                              dut.uart1_rx_tready_i, len(tx_payload))
    assert got == tx_payload

    # DUT -> host through the U1 seam; U0's window must stay empty (the
    # two streams may share nothing but the module).
    rx_payload = [0xC1, 0xC2, 0xC3]
    drv = AxisByteDriver(dut.dut_clk_i, dut.uart1_tx_tdata_i,
                         dut.uart1_tx_tvalid_i, dut.uart1_tx_tready_o)
    await drv.write(bytes(rx_payload))
    await _poll_status(axi, UARTBR_STATUS_U1_RX_EMPTY, want_set=False)
    val, _ = await axi.read(UARTBR_FIFO_STATUS)
    assert val & UARTBR_STATUS_U0_RX_EMPTY, "U1 traffic must not appear in U0's window"
    for b in rx_payload:
        await _pop_expect(axi, UARTBR_U1_DATA, b)
    await _poll_status(axi, UARTBR_STATUS_U1_RX_EMPTY, want_set=True)
    await _pop_expect_empty(axi, UARTBR_U1_DATA)


@cocotb.test(skip=NO_RTL)
async def test_fifo_status_reset_value_and_reserved_offsets(dut):
    """What this proves: FIFO_STATUS out of reset reads exactly the
    chosen bit layout (RTL ambiguity #4) with every rx/swo empty flag SET
    and both tx full flags CLEAR; the contract's reserved gaps (0x04/0x0C)
    and the in-window unmapped offset 0x1C read 0; writes to RO offsets
    are accepted-no-effect. (Offsets >= 0x20 alias mod 0x20 — only address
    bits [4:2] decode, house convention; see dut_notes.md. The README's
    'unmapped >= 0x1C read 0' only holds inside the 32-byte window, and a
    read of 0x20 would DESTRUCTIVELY alias U0_RX — pinned in dut_notes.md
    rather than asserted here, flagged for A6.)"""
    axi = await _bring_up(dut)

    expect = (UARTBR_STATUS_U0_RX_EMPTY | UARTBR_STATUS_U1_RX_EMPTY
              | UARTBR_STATUS_SWO_RX_EMPTY)
    val, _ = await axi.read(UARTBR_FIFO_STATUS)
    assert val == expect, (
        f"FIFO_STATUS reset value must be {expect:#04x} "
        f"(empties set, fulls clear), got {val:#04x}")

    for off in (0x04, 0x0C, 0x1C):
        val, _ = await axi.read(off)
        assert val == 0, f"reserved/unmapped offset {off:#x} must read 0, got {val:#x}"

    # RO/reserved writes: accepted at protocol level, no effect anywhere
    # observable (SWO_RX write explicitly called out by the regmap table).
    for off in (UARTBR_SWO_RX, UARTBR_FIFO_STATUS, 0x04):
        resp = await axi.write(off, 0xFFFF_FFFF)
        assert resp == 0
    val, _ = await axi.read(UARTBR_FIFO_STATUS)
    assert val == expect, "RO/reserved writes must not disturb any FIFO state"
    await _pop_expect_empty(axi, UARTBR_SWO_RX)


@cocotb.test(skip=NO_RTL)
async def test_swo_capture_frame_err_overflow_and_sticky_clear(dut):
    """What this proves: the whole SWO path — divisor+enable programmed in
    one 32-bit SWO_CFG write; 8N1 bytes deserialised at the programmed
    (divisor+1)·dut_clk bit period and popped via SWO_RX; a broken stop
    bit sets sticky frame_err[31] and the byte is discarded; more than
    FIFO_DEPTH un-drained bytes set sticky overflow[30] with the
    documented drop-newest policy; both stickies clear once enable=0; and
    a divisor change takes effect through the documented enable off/on
    toggle (the dut-domain divisor is captured on the synchronized enable
    rising edge)."""
    axi = await _bring_up(dut)
    div = 7                      # bit period 8 dut_clk cycles (>= 8x oversampling)
    bit_cycles = div + 1

    await axi.write(UARTBR_SWO_CFG, UARTBR_SWO_CFG_ENABLE | div)
    val, _ = await axi.read(UARTBR_SWO_CFG)
    assert val == UARTBR_SWO_CFG_ENABLE | div, (
        f"SWO_CFG readback: expected {UARTBR_SWO_CFG_ENABLE | div:#x} "
        f"(stickies clear), got {val:#x}")
    await ClockCycles(dut.dut_clk_i, 8)  # enable 2-FF + divisor capture

    # Clean bytes arrive in order.
    payload = [0xC3, 0x3C, 0x81]
    for b in payload:
        await _swo_send(dut, b, bit_cycles)
    await _poll_status(axi, UARTBR_STATUS_SWO_RX_EMPTY, want_set=False)
    for b in payload:
        await _pop_expect(axi, UARTBR_SWO_RX, b)
    await _poll_status(axi, UARTBR_STATUS_SWO_RX_EMPTY, want_set=True)
    await _pop_expect_empty(axi, UARTBR_SWO_RX)

    # Broken stop bit: sticky frame_err, byte discarded.
    await _swo_send(dut, 0x55, bit_cycles, stop_bit=0)
    for _ in range(50):
        val, _ = await axi.read(UARTBR_SWO_CFG)
        if val & UARTBR_SWO_CFG_FRAME_ERR:
            break
    assert val & UARTBR_SWO_CFG_FRAME_ERR, "broken stop bit must set sticky frame_err[31]"
    assert not (val & UARTBR_SWO_CFG_OVERFLOW), "no overflow yet"
    await _pop_expect_empty(axi, UARTBR_SWO_RX)  # the bad byte was discarded

    # Overflow: FIFO_DEPTH+2 un-drained bytes; drop-newest + sticky [30].
    flood = [(0x10 + i) & 0xFF for i in range(FIFO_DEPTH + 2)]
    for b in flood:
        await _swo_send(dut, b, bit_cycles)
    for _ in range(50):
        val, _ = await axi.read(UARTBR_SWO_CFG)
        if val & UARTBR_SWO_CFG_OVERFLOW:
            break
    assert val & UARTBR_SWO_CFG_OVERFLOW, "overrun must set sticky overflow[30]"
    for b in flood[:FIFO_DEPTH]:          # oldest FIFO_DEPTH kept ...
        await _pop_expect(axi, UARTBR_SWO_RX, b)
    await _poll_status(axi, UARTBR_STATUS_SWO_RX_EMPTY, want_set=True)
    await _pop_expect_empty(axi, UARTBR_SWO_RX)  # ... newest 2 dropped

    # Disable clears both stickies (dut-domain clear + 2-FF back-sync).
    await axi.write(UARTBR_SWO_CFG, div)
    for _ in range(50):
        val, _ = await axi.read(UARTBR_SWO_CFG)
        if not (val & (UARTBR_SWO_CFG_FRAME_ERR | UARTBR_SWO_CFG_OVERFLOW)):
            break
    assert val == div, f"stickies must clear while enable=0, got {val:#x}"

    # Divisor change via the documented off->on toggle: 10-cycle bits now.
    new_div = 9
    await axi.write(UARTBR_SWO_CFG, UARTBR_SWO_CFG_ENABLE | new_div)
    await ClockCycles(dut.dut_clk_i, 8)
    await _swo_send(dut, 0x69, new_div + 1)
    await _poll_status(axi, UARTBR_STATUS_SWO_RX_EMPTY, want_set=False)
    await _pop_expect(axi, UARTBR_SWO_RX, 0x69)


@cocotb.test(skip=NO_RTL)
async def test_wstrb_partial_write_swo_cfg_and_data_window(dut):
    """WSTRB byte-lane test (previously untested on EVERY AXI slave — the BFM
    only ever drove strb=0xF). SWO_CFG is UARTBR's richest per-lane register:
    wstrb[0]->divisor[7:0], wstrb[1]->divisor[15:8], wstrb[2]->enable(bit16).
    A partial strobe must update ONLY the enabled lanes; a slave that ignores
    WSTRB (commits the whole word) fails here. Also pins that a data-window
    WRITE with NO lanes enabled does not push a phantom console byte (the
    u0_tx_push strobe is gated on wstrb[0])."""
    axi = await _bring_up(dut)

    # Seed enable + divisor 0xBEEF through a full-word write.
    await axi.write_bytes(UARTBR_SWO_CFG, UARTBR_SWO_CFG_ENABLE | 0xBEEF, 0xF)
    val, _ = await axi.read(UARTBR_SWO_CFG)
    assert val & 0xFFFF == 0xBEEF and (val & UARTBR_SWO_CFG_ENABLE), f"seed {val:#x}"

    # Lane 0 only: divisor[7:0] -> 0x11; [15:8] and enable must survive.
    await axi.write_bytes(UARTBR_SWO_CFG, 0x0000_0011, 0x1)
    val, _ = await axi.read(UARTBR_SWO_CFG)
    assert val & 0xFFFF == 0xBE11, f"wstrb=0x1 must update only div[7:0]; got {val:#x}"
    assert val & UARTBR_SWO_CFG_ENABLE, "enable (lane 2) must be untouched by a lane-0 write"

    # Lane 1 only: divisor[15:8] -> 0x77; [7:0] and enable must survive.
    await axi.write_bytes(UARTBR_SWO_CFG, 0x0000_7700, 0x2)
    val, _ = await axi.read(UARTBR_SWO_CFG)
    assert val & 0xFFFF == 0x7711, f"wstrb=0x2 must update only div[15:8]; got {val:#x}"
    assert val & UARTBR_SWO_CFG_ENABLE, "enable must survive a lane-1 write"

    # Lane 2 only (enable bit 16), data[16]=0: clear enable, keep the divisor.
    await axi.write_bytes(UARTBR_SWO_CFG, 0x0000_0000, 0x4)
    val, _ = await axi.read(UARTBR_SWO_CFG)
    assert not (val & UARTBR_SWO_CFG_ENABLE), "wstrb=0x4 with data[16]=0 must clear enable"
    assert val & 0xFFFF == 0x7711, f"divisor must survive an enable-only write; got {val:#x}"

    # Data-window write with NO lanes enabled: must not push (u0_tx_push needs
    # wstrb[0]); with tready held low a real push would raise uart_rx_tvalid_o.
    assert int(dut.uart_rx_tvalid_o.value) == 0, "U0 host->DUT must start idle"
    await axi.write_bytes(UARTBR_U0_DATA, 0x99, 0x0)
    await ClockCycles(dut.dut_clk_i, 6)  # let any (erroneous) push cross the CDC
    await ReadOnly()
    assert int(dut.uart_rx_tvalid_o.value) == 0, (
        "a U0_TX write with wstrb=0 must NOT push a byte (WSTRB ignored?)")
    await NextTimeStep()


@cocotb.test(skip=NO_RTL)
async def test_read_at_0x20_must_not_pop_u0_rx(dut):
    """REGRESSION GUARD for a destructive-alias footgun, now FIXED (2026-07-09).

    UARTBR used to decode only araddr[4:2], so a read of 0x20 aliased U0_DATA
    (0x00) and POPPED a U0_RX byte — a debugger or speculative sweep of this
    64 KB page would silently eat DUT console data. This test asserted that SAFE
    property as an `expect_fail=True` xfail, with the instruction "when fixed,
    drop it — this test then becomes a normal green guard".

    The UARTBR decode has since been widened to the full local word address (the
    same fix applied across every shell IP block after poc/systemrdl's
    decode-equivalence bench compared each against a generated full-address
    decode). The xfail is dropped; the read side-effect is now gated on the
    decoded, mapped register select, so an unmapped read pops nothing."""
    axi = await _bring_up(dut)
    drv = AxisByteDriver(dut.dut_clk_i, dut.uart_tx_tdata_i,
                         dut.uart_tx_tvalid_i, dut.uart_tx_tready_o)
    await drv.write(bytes([0xD1]))
    await _poll_status(axi, UARTBR_STATUS_U0_RX_EMPTY, want_set=False)

    # SAFE property: a read of the aliased offset 0x20 must NOT pop the byte,
    # so U0_DATA (0x00) must still return it. (It won't — 0x20 aliased+popped.)
    await axi.read(0x20)
    await _pop_expect(axi, UARTBR_U0_DATA, 0xD1)


@cocotb.test(skip=NO_RTL)
async def test_random_aw_en_corners(dut):
    """Constrained-random AXI4-Lite aw_en corner coverage (A5 random wave):
    randomized AW/W arrival order (same / AW-first / W-first), zero-gap
    back-to-back writes, read-during-write, and randomized WSTRB across the
    data-window / SWO_CFG registers. Only BRESP/RRESP=OKAY is asserted here
    (data-window reads are destructive — integrity is proven elsewhere); the
    bound AXI protocol SVA validates every handshake. Write targets stay in
    the mapped window (not the 0x20 destructive alias). Seed logged."""
    await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "uart_bridge")
    m = RandomAxiMaster.from_dut(dut)
    waddrs = [UARTBR_U0_DATA, UARTBR_U1_DATA, UARTBR_SWO_CFG]
    raddrs = [UARTBR_U0_DATA, UARTBR_U1_DATA, UARTBR_SWO_RX,
              UARTBR_FIFO_STATUS, UARTBR_SWO_CFG]
    tally = await random_axi_burst(m, rng, waddrs, raddrs, n=40)
    dut._log.info(f"[uart_bridge random] orderings/paths exercised: {tally}")


async def _cdc_setup(dut, a_period: int, d_period: int, reset_phase_ns: int,
                     clks: list):
    """(Re)start the two clocks at the given coprime periods, hold reset so
    both domains' reset synchronizers see it, then release aresetn after a
    randomized phase offset relative to the running clocks. Kills any clock
    tasks from the previous ratio first (a signal may have only one Clock
    driver at a time). Returns a fresh AxiLiteMaster."""
    for t in clks:
        t.kill()
    clks.clear()

    dut.s_axi_aresetn.value = 0
    dut.uart_tx_tdata_i.value = 0
    dut.uart_tx_tvalid_i.value = 0
    dut.uart_rx_tready_i.value = 0
    dut.swo_i.value = 1
    dut.uart1_tx_tdata_i.value = 0
    dut.uart1_tx_tvalid_i.value = 0
    dut.uart1_rx_tready_i.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    await Timer(1, units="ns")

    clks.append(cocotb.start_soon(
        Clock(dut.s_axi_aclk, a_period, units="ns").start()))
    clks.append(cocotb.start_soon(
        Clock(dut.dut_clk_i, d_period, units="ns").start()))

    await Timer(60, units="ns")            # both reset synchronizers see assert
    if reset_phase_ns:
        await Timer(reset_phase_ns, units="ns")  # randomized reset-release phase
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.dut_clk_i, 5)    # dut-domain release synchronizer
    await RisingEdge(dut.s_axi_aclk)
    return AxiLiteMaster.from_dut(dut)


@cocotb.test(skip=NO_RTL)
async def test_random_cdc_stress(dut):
    """CDC stress across several COPRIME (aclk, dut_clk) period pairs
    (A5 random wave). The audit's top RTL risk is that the five gray-pointer
    async FIFOs are exercised at exactly ONE clock ratio (the fixed 10/7).
    This sweeps a reproducible random sample of coprime integer-ns ratios
    (either channel may be the faster one), each with a RANDOMIZED
    reset-release phase offset, and pushes randomized-length payloads with
    randomized inter-access gaps through the U0 console FIFO in BOTH
    directions — proving byte-exact CDC transport at every ratio. The bound
    cdc_gray_checker SVA (one-bit gray delta + overflow/underflow safety)
    must hold across all of them; a firing is a real CDC bug. Seed logged."""
    rng, _seed = seeded_rng(dut, "uart_bridge")
    menu = coprime_period_pairs(3, 13)
    ratios = rng.sample(menu, k=6)
    clks: list = []
    try:
        for idx, (p, q) in enumerate(ratios):
            a_per, d_per = (p, q) if rng.random() < 0.5 else (q, p)
            phase = rng.randint(0, 2 * max(a_per, d_per))
            axi = await _cdc_setup(dut, a_per, d_per, phase, clks)
            dut._log.info(
                f"[uart_bridge CDC] ratio {idx}: aclk={a_per}ns "
                f"dut_clk={d_per}ns reset_phase={phase}ns")

            # host -> DUT (U0_TX window): random length, random inter-write gaps.
            n = rng.randint(1, FIFO_DEPTH)
            tx = [rng.getrandbits(8) for _ in range(n)]
            for b in tx:
                await axi.write(UARTBR_U0_DATA, b)
                if rng.random() < 0.4:
                    await ClockCycles(dut.s_axi_aclk, rng.randint(1, 3))
            got = await _axis_collect(dut, dut.uart_rx_tdata_o,
                                      dut.uart_rx_tvalid_o,
                                      dut.uart_rx_tready_i, n)
            assert got == tx, (
                f"ratio {idx} ({a_per}/{d_per}): host->DUT CDC mismatch "
                f"{got} != {tx}")

            # DUT -> host (U0_RX window): random length AXIS in, destructive pops.
            mlen = rng.randint(1, FIFO_DEPTH)
            rx = [rng.getrandbits(8) for _ in range(mlen)]
            drv = AxisByteDriver(dut.dut_clk_i, dut.uart_tx_tdata_i,
                                 dut.uart_tx_tvalid_i, dut.uart_tx_tready_o)
            await drv.write(bytes(rx))
            await _poll_status(axi, UARTBR_STATUS_U0_RX_EMPTY, want_set=False)
            for b in rx:
                await _pop_expect(axi, UARTBR_U0_DATA, b)
            await _poll_status(axi, UARTBR_STATUS_U0_RX_EMPTY, want_set=True)
    finally:
        for t in clks:
            t.kill()
