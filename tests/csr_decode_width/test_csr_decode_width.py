"""Every shell CSR block must decode its registers when the interconnect hands it
the FULL system address.

WHY THIS BENCH EXISTS
---------------------
The shell's CSR blocks are packaged with `C_S_AXI_ADDR_WIDTH = 12` as their RTL
default ("local offset decode only"), but shell_bd.tcl instantiates every one of
them with **32**. A Xilinx AXI slave whose address port is 32 bits wide receives
the *full* system address from the interconnect -- `0x44A9_0000`, not `0x0`.

The decode was widened (correctly, to stop a read of 0x20 aliasing U0_DATA and
POPPING a console byte) to compare `addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]`. At the
RTL default of 12 that is right. At the BD's 32 it compares the full address
against 'h0 and never matches: on silicon every CSR write was ignored and every
read returned 0, across all six blocks. HWICAP -- a Xilinx IP on the same bus --
worked fine, which is what isolated it.

Simulation could not see this, for two compounding reasons:
  1. Every block bench elaborates the IP at the RTL DEFAULT width (12), not at
     the width the BD instantiates (32). The parameter under test was never the
     parameter that ships.
  2. Every bench drives register offsets from base 0 (`axi.write(0x00)`), so
     `addr[31:2]` is 0 and the comparison matches anyway.

So this bench does the two things the others do not: it elaborates at
**C_S_AXI_ADDR_WIDTH=32** and it drives **base + offset**, exactly as the
interconnect does. It fails on the un-fixed RTL and passes on the fixed RTL.

A block bench that elaborates a different parameterisation than the BD is not
verifying the design that ships.
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

# The base shell_bd.tcl assigns to uart_bridge_0. The value itself is not what is
# under test -- ANY non-zero base reproduces the bug -- but using the real one
# keeps the failure recognisable against the silicon symptom.
UARTBR_BASE = 0x44A90000

UARTBR_U0_DATA = 0x00
UARTBR_FIFO_STATUS = 0x14
UARTBR_SWO_CFG = 0x18


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    cocotb.start_soon(Clock(dut.dut_clk_i, 7, units="ns").start())  # unrelated period: real CDC

    dut.s_axi_aresetn.value = 0
    dut.uart_tx_tdata_i.value = 0
    dut.uart_tx_tvalid_i.value = 0
    dut.uart_rx_tready_i.value = 0
    dut.swo_i.value = 1            # 8N1 idle-high
    dut.uart1_tx_tdata_i.value = 0
    dut.uart1_tx_tvalid_i.value = 0
    dut.uart1_rx_tready_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """A read at BASE+0x14 must return FIFO_STATUS, not 0.

    This is the exact silicon symptom: reads returned 0 for every register.
    FIFO_STATUS is chosen because it is read-only and non-destructive, and at
    reset it must report BOTH RX FIFOs empty -- a value that cannot be confused
    with the all-zero read of a dead decode.
    """
    axi = await _bring_up(dut)

    at_zero, _ = await axi.read(UARTBR_FIFO_STATUS)
    at_base, _ = await axi.read(UARTBR_BASE + UARTBR_FIFO_STATUS)

    assert at_zero == at_base, (
        "FIFO_STATUS read 0x%08x at offset-from-zero but 0x%08x at the real base "
        "0x%08x. The decode is comparing the upper address bits the interconnect "
        "supplies. Every CSR register is unreachable on hardware."
        % (at_zero, at_base, UARTBR_BASE)
    )
    assert at_base != 0, (
        "FIFO_STATUS read back 0 at the real base -- a dead decode reads 0 for "
        "everything, which is precisely how this bug hid."
    )


@cocotb.test(skip=NO_RTL)
async def test_writes_land_at_the_real_base_address(dut):
    """A write at BASE+SWO_CFG must stick.

    SWO_CFG is the only R/W register in this block with an observable value, so
    it is the write-readback canary. On silicon, `DECOUPLE <- 1` read back 0 in
    dfx_ctl; this is the same test on the UART block.
    """
    axi = await _bring_up(dut)

    await axi.write(UARTBR_BASE + UARTBR_SWO_CFG, 0x0000_0037)
    val, _ = await axi.read(UARTBR_BASE + UARTBR_SWO_CFG)

    assert val == 0x0000_0037, (
        "wrote 0x37 to SWO_CFG at the real base, read back 0x%08x. Writes are "
        "being ignored because the decode never matches." % val
    )


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offset_in_the_page_still_does_not_alias(dut):
    """The fix must not regress the property the widened decode bought us.

    A read of 0x20 used to alias U0_DATA (0x00) and POP a byte from the DUT's
    console FIFO -- a debugger sweeping this page silently ate data. Decoding the
    local page (not the full system address) must keep 0x20 unmapped.
    """
    axi = await _bring_up(dut)

    status_before, _ = await axi.read(UARTBR_BASE + UARTBR_FIFO_STATUS)
    unmapped, _ = await axi.read(UARTBR_BASE + 0x20)
    status_after, _ = await axi.read(UARTBR_BASE + UARTBR_FIFO_STATUS)

    assert unmapped == 0, "an unmapped offset must read 0, got 0x%08x" % unmapped
    assert status_before == status_after, (
        "reading unmapped 0x20 changed FIFO_STATUS (0x%08x -> 0x%08x): it aliased "
        "a data register and popped a byte." % (status_before, status_after)
    )


@cocotb.test(skip=NO_RTL)
async def test_next_page_does_not_alias_into_this_block(dut):
    """BASE + 64 KB is a DIFFERENT slave's page and must not decode here.

    This is what pins the decode width down. Decode too few bits (say 12) and
    BASE+0x1000 aliases offset 0 = U0_DATA, popping a console byte -- the same
    footgun, moved. The blocks are mapped 64 KB apart, so the local page is 16
    bits wide.
    """
    axi = await _bring_up(dut)

    status_before, _ = await axi.read(UARTBR_BASE + UARTBR_FIFO_STATUS)
    far, _ = await axi.read(UARTBR_BASE + 0x1000)   # inside the 64 KB page, unmapped
    status_after, _ = await axi.read(UARTBR_BASE + UARTBR_FIFO_STATUS)

    assert far == 0, "BASE+0x1000 is unmapped and must read 0, got 0x%08x" % far
    assert status_before == status_after, (
        "reading BASE+0x1000 disturbed FIFO_STATUS -- it aliased a mapped "
        "register. The decode is too narrow."
    )
