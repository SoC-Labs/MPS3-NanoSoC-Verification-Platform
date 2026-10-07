"""swd_bb (SWDBB bit-bang probe @ 0x44A7_0000) at the width shell_bd.tcl actually
instantiates it with (C_S_AXI_ADDR_WIDTH=32).

The same latent copy of bug #1: shipped at 32, benched only at 12. This block is
the pin-wiggler behind the OpenOCD remote_bitbang SWD server -- EVERY SWCLK edge
is a DRIVE write and every read-back a SAMPLE read. With the decode comparing the
full interconnect address (0x44A7_0000) against 'h0, every DRIVE write is ignored
and every SAMPLE reads 0: the probe can drive nothing and sees nothing, so SWD
bring-up is dead on silicon. This block specifically gates the SWD bring-up work.

Run with:  make -C tests/csr_decode_width BLOCK=swd_bb
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

SWDBB_BASE = 0x44A70000           # shell_bd.tcl assign_bd_address for swd_bb_0

DRIVE = 0x00                      # RW: [0] swclk [1] swdio_o [2] swdio_oe (write-through)
SAMPLE = 0x04                     # RO: [0] swdio_i (2-FF synchronized)

DRIVE_SWCLK = 1 << 0
DRIVE_SWDIO_O = 1 << 1
DRIVE_SWDIO_OE = 1 << 2


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())  # single-domain block

    dut.s_axi_aresetn.value = 0
    # DUT drives SWDIO high during turnaround; hold it high so SAMPLE has a
    # recognisable non-zero read-only value once synchronized.
    dut.swd_dio_i_i.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)   # 2-FF swd_dio_i synchronizer settles

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """A read at BASE+SAMPLE must return the synchronized swdio_i, not 0.

    swd_dio_i_i is held high, so SAMPLE[0] reads 1 -- a value that cannot be
    confused with the all-zero read of a dead decode. On silicon SAMPLE read 0
    unconditionally, so the probe could never read a DUT ACK.
    """
    axi = await _bring_up(dut)

    at_zero, _ = await axi.read(SAMPLE)
    at_base, _ = await axi.read(SWDBB_BASE + SAMPLE)

    assert at_zero == at_base, (
        "SAMPLE read 0x%08x at offset-from-zero but 0x%08x at the real base "
        "0x%08x. The decode is comparing the upper address bits the interconnect "
        "supplies; the SWD probe is blind on hardware."
        % (at_zero, at_base, SWDBB_BASE)
    )
    assert at_base & 0x1, (
        "SAMPLE read back 0 at the real base with swd_dio_i_i driven high -- a "
        "dead decode reads 0 for everything, which is how this bug hid."
    )


@cocotb.test(skip=NO_RTL)
async def test_writes_land_at_the_real_base_address(dut):
    """A write at BASE+DRIVE must stick.

    DRIVE is the only R/W register in this block; it is the write-readback
    canary. On silicon the equivalent silent failure is "every SWCLK edge the
    firmware bit-bangs is dropped".
    """
    axi = await _bring_up(dut)

    val_in = DRIVE_SWCLK | DRIVE_SWDIO_OE      # 0x5: clock high, output-enabled
    await axi.write(SWDBB_BASE + DRIVE, val_in)
    val, _ = await axi.read(SWDBB_BASE + DRIVE)
    assert val == val_in, (
        "wrote 0x%x to DRIVE at the real base, read back 0x%08x. Writes are being "
        "ignored because the decode never matches." % (val_in, val)
    )

    await axi.write(SWDBB_BASE + DRIVE, 0)
    val, _ = await axi.read(SWDBB_BASE + DRIVE)
    assert val == 0, "DRIVE stuck at 0x%08x after writing 0" % val


@cocotb.test(skip=NO_RTL)
async def test_drive_writes_reach_the_swd_partition_pins(dut):
    """A DRIVE register that reads back but drives no pins is no better.

    The three DRIVE bits are wired write-through to the swd_clk / swd_dio_o /
    swd_dio_oe partition pins. If the write lands in the register but the pins
    never move, the DUT's DAP sees nothing and SWD bring-up still fails --
    silently. (Analogous to dfx_ctl's decouple_en_o observation.)
    """
    axi = await _bring_up(dut)

    assert int(dut.swd_clk_o.value) == 0
    assert int(dut.swd_dio_oe_o.value) == 0

    await axi.write(SWDBB_BASE + DRIVE, DRIVE_SWCLK | DRIVE_SWDIO_OE)  # o=0, oe=1, clk=1
    await ClockCycles(dut.s_axi_aclk, 2)

    assert int(dut.swd_clk_o.value) == 1, "DRIVE.swclk set but swd_clk_o pin still 0"
    assert int(dut.swd_dio_oe_o.value) == 1, "DRIVE.swdio_oe set but swd_dio_oe_o pin still 0"
    assert int(dut.swd_dio_o_o.value) == 0, "swd_dio_o_o should follow DRIVE[1]=0, got 1"


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_drive(dut):
    """The fix must not regress into a too-narrow decode.

    DRIVE is offset 0 and write-through to the SWD pins. Under a decode narrow
    enough to let BASE+0x1000 (or an in-page offset) wrap to offset 0, a debugger
    register sweep would rewrite the live SWD pins. This was the pre-fix bug (only
    addr[2] decoded, so 0x08 aliased onto DRIVE). Neither may alias now.
    """
    axi = await _bring_up(dut)

    await axi.write(SWDBB_BASE + 0x08, 0xFFFFFFFF)     # in-page, unmapped (the old alias)
    await axi.write(SWDBB_BASE + 0x1000, 0xFFFFFFFF)   # 64 KB-page width probe
    await ClockCycles(dut.s_axi_aclk, 2)

    drv, _ = await axi.read(SWDBB_BASE + DRIVE)
    assert drv == 0, (
        "a write to an unmapped offset aliased into DRIVE (read 0x%08x): a "
        "register sweep would rewrite the SWD pins." % drv
    )
    assert int(dut.swd_clk_o.value) == 0 and int(dut.swd_dio_oe_o.value) == 0, (
        "an unmapped write disturbed the SWD partition pins"
    )

    gap, _ = await axi.read(SWDBB_BASE + 0x08)
    assert gap == 0, "unmapped in-page offset 0x08 must read 0, got 0x%08x" % gap
    far, _ = await axi.read(SWDBB_BASE + 0x1000)
    assert far == 0, "BASE+0x1000 is unmapped and must read 0, got 0x%08x" % far
