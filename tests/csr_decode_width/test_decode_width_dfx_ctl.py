"""dfx_ctl at the width shell_bd.tcl actually instantiates it with.

dfx_ctl is THE block whose failure took the platform down: on silicon
`DECOUPLE <- 1` read back 0, so the swap FSM asserted DECOUPLE, never saw its
confirmation, and timed out to FAILED with icap_bytes=0. Every CSR register in
every block was unreachable, because the decode compared the full system address
the interconnect supplies (0x44A1_0000) against 'h0.

tests/csr_decode_width/test_csr_decode_width.py covers uart_bridge. This covers
dfx_ctl, because a regression here is not "a console byte is lost" -- it is "no
DUT can ever be swapped in again".

Run with:  make -C tests/csr_decode_width BLOCK=dfx_ctl
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

DFXCTL_BASE = 0x44A10000          # shell_bd.tcl's assignment for dfx_ctl_0
DECOUPLE = 0x00                   # RW
SHUTDOWN = 0x04                   # RW
STATUS = 0x08                     # RO: bit0 decoupled, bit1 rp_in_reset
RM_ID = 0x10                      # RO
RM_STATUS = 0x14                  # RO: bit0 rm_id_valid

DECOUPLE_EN = 1 << 0
STATUS_DECOUPLED = 1 << 0
STATUS_RP_IN_RESET = 1 << 1


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())

    dut.s_axi_aresetn.value = 0
    # RP-side inputs, held at a known state so STATUS/RM_ID are predictable.
    dut.decoupled_i.value = 0
    dut.rp_in_reset_i.value = 0
    dut.axi_shutdown_ack_i.value = 0
    dut.rm_id_i.value = 0
    dut.dut_lockup_i.value = 0
    # The isolation bit's own resets (dfx_ctl.sv, 2026-09-14, the shell-watchdog
    # un-clamp fix). decouple_en_q no longer sits on s_axi_aresetn, so these two
    # must be driven or it stays X and every DECOUPLE readback below is X.
    # ext_por_n_i is held low through reset -- that IS its reset now.
    dut.ext_por_n_i.value = 0
    dut.wdt_reset_i.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    dut.ext_por_n_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_decouple_write_readback_at_the_real_base(dut):
    """THE silicon symptom, reproduced: DECOUPLE <- 1 must read back 1.

    Measured over JTAG on 2026-07-09 against the broken decode:
        before:     DECOUPLE=00000000
        after wr 1: DECOUPLE=00000000   <-- write silently ignored
    """
    axi = await _bring_up(dut)

    await axi.write(DFXCTL_BASE + DECOUPLE, DECOUPLE_EN)
    val, _ = await axi.read(DFXCTL_BASE + DECOUPLE)
    assert val & DECOUPLE_EN, (
        "wrote DECOUPLE=1 at the real base 0x%08x, read back 0x%08x. The decode "
        "never matched, so the swap FSM can never assert DECOUPLE and every swap "
        "fails closed." % (DFXCTL_BASE, val)
    )

    # ...and it must be genuinely writable both ways, not stuck high.
    await axi.write(DFXCTL_BASE + DECOUPLE, 0)
    val, _ = await axi.read(DFXCTL_BASE + DECOUPLE)
    assert (val & DECOUPLE_EN) == 0, "DECOUPLE stuck asserted: read 0x%08x" % val


@cocotb.test(skip=NO_RTL)
async def test_decouple_en_o_actually_drives_the_decoupler(dut):
    """A register that reads back but drives nothing is no better.

    The R1 decoupler's DECOUPLE input comes from decouple_en_o. If the write
    lands in the register but the output never asserts, the RP is not isolated
    and reconfiguration corrupts the static -- a silent, far worse failure than
    the swap timing out.
    """
    axi = await _bring_up(dut)

    assert int(dut.decouple_en_o.value) == 0
    await axi.write(DFXCTL_BASE + DECOUPLE, DECOUPLE_EN)
    await ClockCycles(dut.s_axi_aclk, 2)
    assert int(dut.decouple_en_o.value) == 1, (
        "DECOUPLE register set but decouple_en_o still 0 -- the RP would not be "
        "isolated during reconfiguration."
    )


@cocotb.test(skip=NO_RTL)
async def test_status_is_readable_at_the_real_base(dut):
    """STATUS is what the FSM polls for its decouple confirmation."""
    axi = await _bring_up(dut)

    dut.decoupled_i.value = 1
    dut.rp_in_reset_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 6)      # through the ASYNC_REG sync chains

    val, _ = await axi.read(DFXCTL_BASE + STATUS)
    assert val & STATUS_DECOUPLED, "STATUS.decoupled not visible: 0x%08x" % val
    assert val & STATUS_RP_IN_RESET, "STATUS.rp_in_reset not visible: 0x%08x" % val


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_decouple(dut):
    """0x0C is a documented gap, and BASE+0x1000 belongs to nobody here.

    Neither may alias DECOUPLE (offset 0). A decode narrow enough to let
    BASE+0x1000 wrap to 0 would let a debugger sweep ISOLATE THE RP mid-run.
    """
    axi = await _bring_up(dut)

    await axi.write(DFXCTL_BASE + 0x0C, 0xFFFFFFFF)
    await axi.write(DFXCTL_BASE + 0x1000, 0xFFFFFFFF)
    await ClockCycles(dut.s_axi_aclk, 2)

    val, _ = await axi.read(DFXCTL_BASE + DECOUPLE)
    assert (val & DECOUPLE_EN) == 0, (
        "a write to an unmapped offset aliased into DECOUPLE (read 0x%08x). A "
        "register sweep would isolate the RP." % val
    )
    assert int(dut.decouple_en_o.value) == 0

    gap, _ = await axi.read(DFXCTL_BASE + 0x0C)
    assert gap == 0, "unmapped 0x0C must read 0, got 0x%08x" % gap
