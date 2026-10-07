"""tests/uart2_rx_path/test_uart2_rx_path.py

M1 GATE — "can a host byte reach the DUT's console receiver?"

This is the one hardware unknown standing between this platform and a
MicroPython REPL. The console's DUT->host half (TX) is proven and in daily use;
the host->DUT half (RX) had NEVER been demonstrated into the core, in either the
monolithic or the DFX flow. The monolithic top says so in its own header
(fpga/monolithic/nanosoc_mps3_top.sv:88-91):

    "UART2 RX only reaches the pin mux ... so UART_RX_F[2] is wired for
     completeness/future-proofing but does not yet deliver host-to-DUT bytes."

Nobody had localised WHY. This bench does, and gates the fix.


THE PATH UNDER TEST (all real RTL — no models on the path)

    P1_IN[4]                       <- the pad. In the DFX flow the shell's
      |                               uart_axis_shim drives it; in the monolithic
      |                               flow it is UART_RX_F[2].
      v
    nanosoc_ss_systemctrl
      +-- u_pin_mux (nanosoc_pin_mux)        uart2_rxd = p1_in[4]
      |        |
      |        v
      +-- u_region_soc_peripheral
             u_apb_uart_2 (Arm cmsdk_apb_uart)  .RXD(uart2_rxd)   @ 0x4000_6000


THE BUG THIS BENCH FOUND

    nanosoc_ss_systemctrl instantiated the pin mux with its pad-input ports
    LEFT UNCONNECTED:

        .p1_in ( ), // was(p1_in) now from pad inputs),

    while nanosoc_pin_mux declared `p1_in` as an OUTPUT that it generated
    itself by self-feedback ("port input feedback"):

        assign p1_in[4]  = p1_out_en_mux[4] ? p1_out_mux[4] : 1'b1;
        assign uart2_rxd = p1_in[4];

    Net effect:

        uart2_rxd == (P1_OUTEN[4] ? P1_OUT[4] : 1'b1)

    UART2's receive line was a loopback of the SoC's OWN GPIO drive, or a
    constant idle-high '1'. The real pad value was discarded. A host byte could
    not reach UART2 by any path, on FPGA or ASIC.

    Someone refactored GPIO to take real pad inputs (routing them straight to
    the GPIO blocks) and disconnected the pin mux's synthetic feedback — without
    noticing that the UART receive lines are derived from it.

    FIX: p0_in/p1_in become true inputs; the feedback network is deleted; the
    subsystem connects the real pads. (nanosoc_arch_tech, 2 files.)


FALSIFICATION

    Both tests below FAIL on the unmodified RTL and PASS on the fixed RTL — same
    bench, same stimulus, nothing rewritten between the two runs. That delta is
    the proof the bench has the resolving power to see the bug, and is recorded
    in the commit message.

    (An earlier revision of this bench also asserted the buggy loopback
    behaviour directly, by driving P1_OUT/P1_OUTEN. That was UNSOUND: those are
    OUTPUTS of nanosoc_ss_systemctrl, driven by the CMSDK GPIO blocks, so the
    bench was forcing a value against an internal driver and reading back its
    own force. It was removed rather than fixed — the pre/post delta above is a
    stronger control and does not require poking DUT outputs. The harness now
    pins every port direction in RTL so the elaborator catches that class of
    bench bug for us.)


SCOPE NOTE
    socdebug_usrt_control (the USRT0/USRT1 APB slots) is stubbed — its RTL lives
    in a repo not checked out here. It cannot influence uart2_rxd: the pin mux's
    uart0_rxd/uart1_rxd outputs are unconnected at its only instantiation, and
    nothing in this bench addresses those APB slots. See the stub's header.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

# --- Arm cmsdk_apb_uart register map. UART2 @ 0x4000_6000 -------------------
# (APB decode is DECODE4BIT(i_paddr[15:12]) -> PSEL6 = uart2, so bits[15:12]=6.)
UART2_BASE = 0x4000_6000
UART_DATA = UART2_BASE + 0x00  # RW  TX/RX data
UART_STATE = UART2_BASE + 0x04  # RW  [0]=TX buf full, [1]=RX buf full
UART_CTRL = UART2_BASE + 0x08  # RW  [0]=TX enable, [1]=RX enable
UART_BAUDDIV = UART2_BASE + 0x10  # RW  PCLK cycles per bit; minimum 16

CTRL_RX_EN = 1 << 1
STATE_RX_FULL = 1 << 1

BAUDDIV = 16  # documented minimum; keeps the frame short in simulation

# nanosoc_clkctrl with CLKGATE_PRESENT=0 (the default) gives PCLK = HCLK and
# PCLKEN = 1'b1, so one UART bit == BAUDDIV SYS_HCLK cycles exactly.
CLK_NS = 10

IDLE_PADS = 0xFFFF  # a UART line rests high


# =============================================================================
# Bench plumbing
# =============================================================================
def start_clocks(dut):
    """SYS_CLK feeds the peripheral clock, SYS_HCLK the AHB. Same rate, aligned."""
    cocotb.start_soon(Clock(dut.SYS_CLK, CLK_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.SYS_HCLK, CLK_NS, unit="ns").start())


async def reset_dut(dut):
    dut.SOC_PERIPHERAL_HSEL.value = 0
    dut.SOC_PERIPHERAL_HADDR.value = 0
    dut.SOC_PERIPHERAL_HTRANS.value = 0
    dut.SOC_PERIPHERAL_HWDATA.value = 0
    dut.SOC_PERIPHERAL_HWRITE.value = 0

    dut.P0_IN.value = IDLE_PADS
    dut.P1_IN.value = IDLE_PADS

    dut.SYS_SYSRESETn.value = 0
    dut.SYS_PORESETn.value = 0
    dut.SYS_HRESETn.value = 0
    await ClockCycles(dut.SYS_HCLK, 8)
    dut.SYS_SYSRESETn.value = 1
    dut.SYS_PORESETn.value = 1
    dut.SYS_HRESETn.value = 1
    await ClockCycles(dut.SYS_HCLK, 8)


# Sampling offset after a clock edge: far enough for combinational logic to
# settle, far short of the next edge. Avoids cocotb's ReadOnly phase entirely
# (awaiting ReadOnly from inside a helper that a caller then also awaits is an
# illegal transition).
SETTLE_NS = 1


async def _await_ready(dut):
    """Advance to a settled point at a rising edge where the slave is ready."""
    while True:
        await RisingEdge(dut.SYS_HCLK)
        await Timer(SETTLE_NS, unit="ns")
        if dut.SOC_PERIPHERAL_HREADYOUT.value == 1:
            return


async def ahb_write(dut, addr, data):
    """AHB-Lite single 32-bit write. HREADY<-HREADYOUT is tied in the harness."""
    await _await_ready(dut)
    # Address phase
    dut.SOC_PERIPHERAL_HSEL.value = 1
    dut.SOC_PERIPHERAL_HADDR.value = addr
    dut.SOC_PERIPHERAL_HTRANS.value = 0b10  # NONSEQ
    dut.SOC_PERIPHERAL_HWRITE.value = 1

    await RisingEdge(dut.SYS_HCLK)  # -> data phase
    dut.SOC_PERIPHERAL_HSEL.value = 0
    dut.SOC_PERIPHERAL_HTRANS.value = 0b00  # IDLE
    dut.SOC_PERIPHERAL_HWDATA.value = data

    await _await_ready(dut)  # hold HWDATA through any wait states
    dut.SOC_PERIPHERAL_HWRITE.value = 0


async def ahb_read(dut, addr):
    """AHB-Lite single 32-bit read."""
    await _await_ready(dut)
    # Address phase
    dut.SOC_PERIPHERAL_HSEL.value = 1
    dut.SOC_PERIPHERAL_HADDR.value = addr
    dut.SOC_PERIPHERAL_HTRANS.value = 0b10
    dut.SOC_PERIPHERAL_HWRITE.value = 0

    await RisingEdge(dut.SYS_HCLK)  # -> data phase
    dut.SOC_PERIPHERAL_HSEL.value = 0
    dut.SOC_PERIPHERAL_HTRANS.value = 0b00

    await _await_ready(dut)  # HRDATA is valid once HREADYOUT is high
    return int(dut.SOC_PERIPHERAL_HRDATA.value)


def set_pad(dut, bit, value):
    """Drive one bit of the P1 pad bus, leaving the rest idle-high."""
    cur = int(dut.P1_IN.value)
    cur = (cur | (1 << bit)) if value else (cur & ~(1 << bit) & 0xFFFF)
    dut.P1_IN.value = cur


async def send_uart_frame(dut, byte, bit_cycles):
    """Shift a genuine 8N1 frame onto the P1_IN[4] pad, LSB first."""
    set_pad(dut, 4, 0)  # start bit
    await ClockCycles(dut.SYS_HCLK, bit_cycles)
    for i in range(8):  # 8 data bits, LSB first
        set_pad(dut, 4, (byte >> i) & 1)
        await ClockCycles(dut.SYS_HCLK, bit_cycles)
    set_pad(dut, 4, 1)  # stop bit
    await ClockCycles(dut.SYS_HCLK, bit_cycles)


# =============================================================================
# 1. ROOT-CAUSE GATE — does UART2's receive line follow the pad at all?
# =============================================================================
@cocotb.test()
async def test_uart2_rxd_follows_the_pad(dut):
    """uart2_rxd MUST track the P1_IN[4] pad. This is the whole ballgame.

    Purely combinational: no UART configuration, no baud timing, nothing else
    that could plausibly explain a failure. If this fails, a host byte can never
    reach the console receiver and no amount of firmware will fix it.
    """
    start_clocks(dut)
    await reset_dut(dut)

    failures = []
    for expect in (0, 1, 0, 1, 1, 0, 0, 1):
        set_pad(dut, 4, expect)
        await Timer(1, unit="ns")  # settle combinational logic
        got = int(dut.uart2_rxd.value)
        if got != expect:
            failures.append((expect, got))
        dut._log.info(f"P1_IN[4] pad = {expect}  ->  uart2_rxd = {got}")

    assert not failures, (
        f"uart2_rxd does NOT follow the P1_IN[4] pad: {failures}\n\n"
        "ROOT CAUSE: nanosoc_ss_systemctrl instantiates\n"
        "    nanosoc_pin_mux u_pin_mux ( ... .p1_in ( ), ... )\n"
        "leaving the pad-input port UNCONNECTED, while nanosoc_pin_mux derives\n"
        "the UART receive line from it:\n"
        "    assign uart2_rxd = p1_in[4];                                (:95)\n"
        "    assign p1_in[4]  = p1_out_en_mux[4] ? p1_out_mux[4] : 1'b1; (:156)\n"
        "so uart2_rxd is a loopback of the SoC's OWN drive, never the pad.\n"
        "The host->DUT console path is physically absent."
    )


# =============================================================================
# 2. PRODUCT GATE — does a real byte land in UART2's receive register?
# =============================================================================
@cocotb.test()
async def test_uart2_receives_a_byte(dut):
    """The REPL's actual requirement: a host byte arrives in UART2's DATA reg.

    Configures the genuine Arm cmsdk_apb_uart over AHB, shifts a real 8N1 frame
    onto the pad, reads the byte back out. Everything on the path is real RTL:
    the CMSDK AHB-to-APB bridge, the APB slave mux, the nanosoc pin mux, and the
    CMSDK UART itself.
    """
    start_clocks(dut)
    await reset_dut(dut)

    await ahb_write(dut, UART_BAUDDIV, BAUDDIV)
    await ahb_write(dut, UART_CTRL, CTRL_RX_EN)

    # Guard: if the register path itself is broken, this bench can say nothing
    # about the RX pad path. Fail loudly and distinctly rather than blaming RX.
    ctrl = await ahb_read(dut, UART_CTRL)
    assert ctrl & CTRL_RX_EN, (
        f"UART2 CTRL readback 0x{ctrl:08x} does not show RX enabled. The "
        "AHB/APB register path to UART2 is broken, so this test cannot say "
        "anything about the RX pad path — fix the bench/bus first."
    )
    div = await ahb_read(dut, UART_BAUDDIV)
    dut._log.info(f"UART2 configured: BAUDDIV={div}, CTRL=0x{ctrl:02x}")

    test_byte = 0x5A  # 0b01011010 — asymmetric, catches bit-order errors
    await send_uart_frame(dut, test_byte, BAUDDIV)
    await ClockCycles(dut.SYS_HCLK, BAUDDIV * 3)  # let the receiver settle

    state = await ahb_read(dut, UART_STATE)
    assert state & STATE_RX_FULL, (
        f"UART2 STATE=0x{state:02x}: the RX buffer never filled. The byte "
        f"0x{test_byte:02x} was shifted onto the P1_IN[4] pad as a well-formed "
        "8N1 frame with the receiver enabled, and UART2 never saw it.\n\n"
        "This IS the host->DUT console path failing — the same failure the "
        "monolithic top's header calls 'does not yet deliver host-to-DUT "
        "bytes'. See test_uart2_rxd_follows_the_pad for the root cause."
    )

    got = await ahb_read(dut, UART_DATA) & 0xFF
    assert got == test_byte, (
        f"UART2 received 0x{got:02x}, expected 0x{test_byte:02x} — the byte "
        "arrived but is corrupt (bit order or baud mismatch)."
    )
    dut._log.info(f"UART2 received 0x{got:02x} — host->DUT console path WORKS")
