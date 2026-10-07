"""tests/uart_axis_shim/test_uart_axis_shim.py

NEW leaf bench (A5 verification-confidence pass). Verifies
`fpga/rp/nanosoc/uart_axis_shim.sv` — the bit-serial UART <-> AXI-Stream byte
shim that bridges nanosoc's raw CMSDK UART2 (TXD/RXD on GPIO P1) to the DFX
partition-pins.md console AXIS group. This is real sequential logic (two 8N1
UART state machines + baud generators) that had ZERO verification before this
bench.

DIV note: the Makefile overrides CLK_HZ/BAUD to 800000/100000 so the baud
divisor DIV = CLK_HZ/BAUD = 8 clk cycles per bit (the 25 MHz/115200 default
would be DIV=217 — ~2170 cycles/byte, painfully slow in sim). `DIV` below
MUST match that override.

What this proves:
  1. RX (serial -> AXIS): an 8N1 byte clocked onto dut_txd_i is deserialised
     and presented on uart_tx_* (tdata + a held tvalid), LSB-first, correct.
  2. TX (AXIS -> serial): a byte accepted on uart_rx_* is serialised onto
     dut_rxd_o as a well-formed 8N1 frame (start / 8 data LSB-first / stop),
     decoded back here.
  3. Round-trip + backpressure: with dut_rxd_o looped back into dut_txd_i, a
     byte pushed on uart_rx_* emerges on uart_tx_*; and while the AXIS
     consumer holds uart_tx_tready low, the shim holds tvalid+tdata stable
     (no drop, no corruption) until the byte is accepted.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from dut_presence import rtl_ready
from axis import AxisByteDriver

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "rp", "nanosoc")
NO_RTL = not rtl_ready(_RTL_DIR, ["uart_axis_shim.sv"])

DIV = 8  # clk cycles per bit — MUST match the Makefile's CLK_HZ/BAUD override.


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.resetn.value = 0
    dut.dut_txd_i.value = 1          # serial idle-high (mark)
    dut.uart_tx_tready.value = 0
    dut.uart_rx_tdata.value = 0
    dut.uart_rx_tvalid.value = 0
    await Timer(50, units="ns")
    dut.resetn.value = 1
    await RisingEdge(dut.clk)


async def _serial_send_byte(dut, byte: int, stop: int = 1):
    """Drive dut_txd_i with one 8N1 frame at DIV clk cycles per bit."""
    dut.dut_txd_i.value = 0                       # start bit
    await ClockCycles(dut.clk, DIV)
    for i in range(8):                           # 8 data bits, LSB first
        dut.dut_txd_i.value = (byte >> i) & 1
        await ClockCycles(dut.clk, DIV)
    dut.dut_txd_i.value = stop                    # stop bit
    await ClockCycles(dut.clk, DIV)
    dut.dut_txd_i.value = 1                       # back to idle


async def _axis_recv_byte(dut, timeout: int = 8000) -> int:
    """Collect one byte from the shim's uart_tx_* AXIS output (a held
    register, not a FWFT FIFO): drive tready and sample the beat that
    commits (tvalid&&tready pre-edge)."""
    dut.uart_tx_tready.value = 1
    for _ in range(timeout):
        await ReadOnly()
        valid = int(dut.uart_tx_tvalid.value)
        data = int(dut.uart_tx_tdata.value)
        await RisingEdge(dut.clk)
        if valid:
            return data
    raise TimeoutError("no byte on uart_tx_* AXIS output")


async def _serial_recv_byte(dut, timeout: int = 8000) -> int:
    """Decode one 8N1 frame off dut_rxd_o: hunt for the start bit, then
    center-sample 8 data bits at DIV intervals."""
    for _ in range(timeout):
        await ReadOnly()
        line = int(dut.dut_rxd_o.value)
        if line == 0:                             # start bit detected
            break
        await RisingEdge(dut.clk)
    else:
        raise TimeoutError("no start bit on dut_rxd_o")
    # We are in the ReadOnly of the cycle where the line is low. Advance to
    # the middle of the first DATA bit: finish the start bit (DIV) then move
    # a half-bit in.
    await RisingEdge(dut.clk)
    await ClockCycles(dut.clk, DIV + DIV // 2 - 1)
    val = 0
    for i in range(8):
        await ReadOnly()
        val |= (int(dut.dut_rxd_o.value) & 1) << i
        await RisingEdge(dut.clk)
        await ClockCycles(dut.clk, DIV - 1)
    return val


async def _loopback(dut):
    """Wire the shim's transmit line back into its own receive line."""
    while True:
        await RisingEdge(dut.clk)                 # post-edge: writable phase
        dut.dut_txd_i.value = int(dut.dut_rxd_o.value)


@cocotb.test(skip=NO_RTL)
async def test_rx_serial_to_axis(dut):
    """What this proves: an 8N1 byte on dut_txd_i is deserialised (LSB-first)
    and emerges on the uart_tx_* AXIS output."""
    await _bring_up(dut)
    for b in (0xA5, 0x00, 0xFF, 0x3C):
        send = cocotb.start_soon(_serial_send_byte(dut, b))
        got = await _axis_recv_byte(dut)
        await send
        assert got == b, f"RX serial->AXIS: expected {b:#04x}, got {got:#04x}"
        await ClockCycles(dut.clk, DIV)


@cocotb.test(skip=NO_RTL)
async def test_tx_axis_to_serial(dut):
    """What this proves: a byte accepted on uart_rx_* is serialised onto
    dut_rxd_o as a well-formed 8N1 frame (decoded back here)."""
    await _bring_up(dut)
    drv = AxisByteDriver(dut.clk, dut.uart_rx_tdata, dut.uart_rx_tvalid,
                         dut.uart_rx_tready)
    for b in (0x5A, 0x81, 0x7E):
        recv = cocotb.start_soon(_serial_recv_byte(dut))
        await drv.write(bytes([b]))
        got = await recv
        assert got == b, f"TX AXIS->serial: expected {b:#04x}, got {got:#04x}"
        await ClockCycles(dut.clk, DIV)


@cocotb.test(skip=NO_RTL)
async def test_roundtrip_with_backpressure(dut):
    """What this proves: with dut_rxd_o looped back to dut_txd_i, a byte
    pushed on uart_rx_* round-trips out uart_tx_*; and while the AXIS
    consumer holds tready LOW, the shim's received byte is held stable
    (tvalid high, tdata unchanged) until accepted — no drop, no corruption."""
    await _bring_up(dut)
    cocotb.start_soon(_loopback(dut))
    drv = AxisByteDriver(dut.clk, dut.uart_rx_tdata, dut.uart_rx_tvalid,
                         dut.uart_rx_tready)

    for b in (0xC3, 0x42):
        dut.uart_tx_tready.value = 0              # back-pressure the RX output
        await drv.write(bytes([b]))              # serialise + loop + deserialise

        # Wait for the round-tripped byte to arrive and stall (held) on the
        # output with tready low.
        held = 0
        for _ in range(8000):
            await ReadOnly()
            valid = int(dut.uart_tx_tvalid.value)
            await RisingEdge(dut.clk)   # always leave ReadOnly before looping/break
            if valid:
                held = 1
                break
        assert held, f"round-trip byte {b:#04x} never reached uart_tx_*"

        # tvalid + tdata must stay stable across several cycles of back-pressure.
        for _ in range(5):
            await ReadOnly()
            assert int(dut.uart_tx_tvalid.value) == 1, "tvalid must hold under back-pressure"
            assert int(dut.uart_tx_tdata.value) == b, (
                f"tdata must hold {b:#04x} under back-pressure, got "
                f"{int(dut.uart_tx_tdata.value):#04x}")
            await RisingEdge(dut.clk)

        # Release: the byte is accepted and tvalid falls.
        got = await _axis_recv_byte(dut)
        assert got == b, f"round-trip mismatch: expected {b:#04x}, got {got:#04x}"
        await ClockCycles(dut.clk, DIV)
