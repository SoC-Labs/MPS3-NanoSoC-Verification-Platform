"""The DUT<->harness console contract, co-simulated end to end.

    rm_uart_echo (dut_clk) --AXIS--> uart_bridge async FIFO --> CSR (s_axi_aclk)

Two benches already cover the halves in isolation. Neither covers the join, which
is where the CDC lives and where an integration bug would actually hide. Here the
cocotb test plays the part of the MicroBlaze: it polls FIFO_STATUS, pops U0_DATA,
pushes bytes back, and watches them echo -- the simulation twin of the
over-the-wire demo (swap in rm_uart_echo, read its banner on TCP 6930).

The AXI clock is 10 ns and the DUT clock 7 ns, deliberately non-harmonic, so every
byte crosses a genuine asynchronous boundary through the bridge's gray-pointer
FIFO rather than a conveniently aligned one.
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

# shell_bd.tcl's base for uart_bridge_0. Driving base+offset (not offset alone)
# is what tests/csr_decode_width proved the decode must tolerate.
UARTBR_BASE = 0x44A90000
U0_DATA = UARTBR_BASE + 0x00
FIFO_STATUS = UARTBR_BASE + 0x14

FS_U0_TX_FULL = 1 << 0
FS_U0_RX_EMPTY = 1 << 1

BANNER = b"rm_uart_echo ready\r\n"
# rm_id encoding v2 (docs/VERSIONING_PLAN.md §3.2):
#     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
# uart_echo was RE-NUMBERED 2026-07-14: it used to be 0x4543484F (ASCII "ECHO"),
# which spent all 32 bits and so collided with the new version field. It now has
# design_id 0x0004 at v1.0. It is the ONLY RM whose id changed.
#
# This is a legitimate hard pin: the SUBJECT of test_rm_id_is_visible_to_the_harness
# is the constant the wrapper's fabric actually drives, so it must be stated
# independently of the wrapper (deriving it from the wrapper would make the test
# a tautology). scripts/harness_gates/check_rm_id_literals.py cross-checks this
# literal against fpga/dfx/rm_list.tcl, so a version bump fails `make check` with
# an exact "update this line to X" rather than rotting into a bench failure.
RM_ID_UART_ECHO = 0x01000004  # design 0x0004 @ v1.0


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    cocotb.start_soon(Clock(dut.dut_clk, 7, units="ns").start())

    dut.s_axi_aresetn.value = 0
    dut.dut_resetn.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 2)
    dut.dut_resetn.value = 1          # RM leaves reset -> starts emitting the banner
    await ClockCycles(dut.s_axi_aclk, 5)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


async def _drain_console(axi, dut, max_bytes=256, idle_limit=400):
    """Pop U0_DATA until the RX FIFO stays empty. Returns the bytes collected.

    `idle_limit` bounds the wait for the FIRST byte and for each subsequent one:
    the RM is on a slower, unrelated clock, so 'empty right now' does not mean
    'nothing more is coming'.
    """
    out = bytearray()
    idle = 0
    while len(out) < max_bytes and idle < idle_limit:
        status, _ = await axi.read(FIFO_STATUS)
        if status & FS_U0_RX_EMPTY:
            idle += 1
            await ClockCycles(dut.s_axi_aclk, 1)
            continue
        idle = 0
        word, _ = await axi.read(U0_DATA)
        out.append(word & 0xFF)
    return bytes(out)


async def _push_console(axi, dut, payload: bytes, idle_limit=2000):
    """Write bytes into U0_DATA (host -> DUT), respecting TX-full backpressure."""
    for b in payload:
        waited = 0
        while True:
            status, _ = await axi.read(FIFO_STATUS)
            if not (status & FS_U0_TX_FULL):
                break
            waited += 1
            assert waited < idle_limit, "U0 TX FIFO never drained -- DUT not consuming"
            await ClockCycles(dut.s_axi_aclk, 1)
        await axi.write(U0_DATA, b)


@cocotb.test(skip=NO_RTL)
async def test_rm_id_is_visible_to_the_harness(dut):
    """The DUT identifies itself. This is what the swap FSM verifies post-release."""
    await _bring_up(dut)
    assert int(dut.rm_id.value) == RM_ID_UART_ECHO, (
        "rm_id = 0x%08x, expected 0x%08x (design 0x0004 @ v1.0)"
        % (int(dut.rm_id.value), RM_ID_UART_ECHO)
    )


@cocotb.test(skip=NO_RTL)
async def test_banner_crosses_the_cdc_intact(dut):
    """DUT -> host, with no host->DUT traffic at all.

    This is the single most valuable thing the bench proves: bytes the RM emits on
    dut_clk arrive, in order and complete, at a CSR read on s_axi_aclk. On
    hardware this is the banner appearing on TCP 6930 the moment the partial is
    swapped in -- data, not a register value.
    """
    axi = await _bring_up(dut)
    got = await _drain_console(axi, dut)
    assert got == BANNER, (
        "banner mismatch across the async FIFO.\n  expected %r\n  got      %r" % (BANNER, got)
    )


@cocotb.test(skip=NO_RTL)
async def test_echo_round_trip_through_the_bridge(dut):
    """host -> DUT -> host. Proves both directions of the console pair."""
    axi = await _bring_up(dut)
    await _drain_console(axi, dut)          # consume the banner first

    payload = bytes(range(0x41, 0x41 + 16))  # 'A'..'P'
    await _push_console(axi, dut, payload)

    got = await _drain_console(axi, dut)
    assert got == payload, (
        "echo mismatch.\n  sent %r\n  got  %r" % (payload, got)
    )


@cocotb.test(skip=NO_RTL)
async def test_slow_host_does_not_lose_bytes(dut):
    """The host reads late. Nothing may be dropped.

    The RM holds a 64-deep FIFO and the bridge another. A host that stalls must
    apply backpressure up the chain, not silently lose console output -- losing
    DUT trace under load is exactly the failure this platform must not have.
    Sends 48 bytes (under the RM's depth so the RM itself cannot be the limiter),
    idles long enough for the whole burst to land, then drains.
    """
    axi = await _bring_up(dut)
    await _drain_console(axi, dut)

    payload = bytes((0x30 + (i % 10)) for i in range(48))
    await _push_console(axi, dut, payload)

    await ClockCycles(dut.s_axi_aclk, 2000)   # host asleep while the echo piles up

    got = await _drain_console(axi, dut, max_bytes=len(payload) + 8)
    assert got == payload, (
        "a stalled host lost console bytes.\n  sent %d: %r\n  got  %d: %r"
        % (len(payload), payload, len(got), got)
    )
