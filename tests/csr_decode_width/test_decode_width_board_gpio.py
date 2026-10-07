"""board_gpio (GPIO passthrough @ 0x44AA_0000) at the width shell_bd.tcl actually
instantiates it with (C_S_AXI_ADDR_WIDTH=32).

The same latent copy of bug #1: shipped at 32, benched only at 12, so the decode
comparing the FULL interconnect address (0x44AA_0000) against 'h0 was never
driven. On silicon that means the host can never read the board pads (buttons/
LEDs) nor take over a GPIO bit -- every read 0, every write ignored. This bench
elaborates at 32 and drives base+offset, so it fails on the un-fixed decode.

Run with:  make -C tests/csr_decode_width BLOCK=board_gpio
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

GPIO_BASE = 0x44AA0000            # shell_bd.tcl assign_bd_address for board_gpio_0

GPIO_IN = 0x00                    # RO: sampled board pads  -- the non-zero RO canary
GPIO_OUT = 0x04                   # RW: host drive value    -- the write-readback canary
GPIO_OE = 0x08                    # RW
GPIO_OWN = 0x0C                   # RW

PAD_PATTERN = 0x1234              # driven onto board_pad_i so IN reads non-zero


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())  # single-domain block

    dut.s_axi_aresetn.value = 0
    # RP-side drives held at 0 (DUT drives nothing); host pads carry a pattern so
    # the read-only IN register has a recognisable non-zero value.
    dut.dut_gpio_o_i.value = 0
    dut.dut_gpio_oe_i.value = 0
    dut.board_pad_i.value = PAD_PATTERN
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)   # 2-FF pad synchronizer settles

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """A read at BASE+IN must return the sampled pad pattern, not 0.

    board_pad_i is held at 0x1234, so IN (the synchronized pad value) is a
    non-zero read-only value that cannot be confused with the all-zero read of a
    dead decode -- the exact silicon symptom.
    """
    axi = await _bring_up(dut)

    at_zero, _ = await axi.read(GPIO_IN)
    at_base, _ = await axi.read(GPIO_BASE + GPIO_IN)

    assert at_zero == at_base, (
        "IN read 0x%08x at offset-from-zero but 0x%08x at the real base 0x%08x. "
        "The decode is comparing the upper address bits the interconnect supplies; "
        "every GPIO register is unreachable on hardware."
        % (at_zero, at_base, GPIO_BASE)
    )
    assert at_base == PAD_PATTERN, (
        "IN read back 0x%08x at the real base, expected the driven pad pattern "
        "0x%04x -- a dead decode reads 0 for everything." % (at_base, PAD_PATTERN)
    )


@cocotb.test(skip=NO_RTL)
async def test_writes_land_at_the_real_base_address(dut):
    """A write at BASE+OUT must stick.

    OUT is the host's drive-value register (used where the host owns a bit); it
    is the write-readback canary. On silicon the equivalent silent failure is
    "the host can never drive a board pin".
    """
    axi = await _bring_up(dut)

    await axi.write(GPIO_BASE + GPIO_OUT, 0x0000_ABCD)
    val, _ = await axi.read(GPIO_BASE + GPIO_OUT)
    assert val == 0x0000_ABCD, (
        "wrote 0xABCD to OUT at the real base, read back 0x%08x. Writes are being "
        "ignored because the decode never matches." % val
    )

    # OE is likewise R/W; prove a second register is independently reachable.
    await axi.write(GPIO_BASE + GPIO_OE, 0x0000_00FF)
    oe, _ = await axi.read(GPIO_BASE + GPIO_OE)
    assert oe == 0x0000_00FF, "OE read back 0x%08x after writing 0xFF" % oe


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_a_mapped_register(dut):
    """The fix must not regress into a too-narrow decode.

    Under a 12-bit decode BASE+0x1000 would wrap to offset 0 = IN and return the
    live pad pattern instead of 0 (and an in-page write could alias onto OUT).
    Neither an in-page unmapped offset (0x20) nor BASE+0x1000 may alias.
    """
    axi = await _bring_up(dut)

    await axi.write(GPIO_BASE + GPIO_OUT, 0x0000_5A5A)   # known state
    await axi.write(GPIO_BASE + 0x20, 0xFFFFFFFF)        # in-page, unmapped
    await axi.write(GPIO_BASE + 0x1000, 0xFFFFFFFF)      # 64 KB-page width probe
    await ClockCycles(dut.s_axi_aclk, 2)

    out, _ = await axi.read(GPIO_BASE + GPIO_OUT)
    assert out == 0x0000_5A5A, (
        "a write to an unmapped offset aliased into OUT (read 0x%08x): a register "
        "sweep would rewrite the host GPIO drive value." % out
    )

    gap, _ = await axi.read(GPIO_BASE + 0x20)
    assert gap == 0, "unmapped in-page offset 0x20 must read 0, got 0x%08x" % gap
    far, _ = await axi.read(GPIO_BASE + 0x1000)
    assert far == 0, (
        "BASE+0x1000 read back 0x%08x -- a too-narrow decode aliased it onto IN "
        "and returned the live pad pattern." % far
    )
