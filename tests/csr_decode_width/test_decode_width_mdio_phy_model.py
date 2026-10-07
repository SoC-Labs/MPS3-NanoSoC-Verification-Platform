"""mdio_phy_model (VPHY @ 0x44A3_0000) at a WIDENED C_S_AXI_ADDR_WIDTH=32.

The third copy of bug #1 -- caught before it shipped, not after.

The eight shell CSR blocks each cap their decode with
`LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16`, because
shell_bd.tcl instantiates them at 32 and the interconnect drives the FULL
address. This block had no such cap: `IDX_W = C_S_AXI_ADDR_WIDTH - ADDR_LSB`.
At 32, waddr_idx for VPHY's base 0x44A3_0000 is 0x1128_C000 -- never equal to
IDX_PHY_STATE ('h0). Every write ignored, every read 0.

WHY THIS ARM EXISTS EVEN THOUGH THE BLOCK IS NOT SHIPPED AT 32 (read before
"fixing" the width): eth_mac_test_subsystem hardcodes .C_S_AXI_ADDR_WIDTH(12)
and its s_axi_vphy_* ports are a fixed [11:0], so today's decode is safe only
by accident of that narrow port. The natural tidy-up -- add a width parameter
to the subsystem so assign_bd_address can use clean 64K pages instead of 4K --
widens these ports to 32 and, un-capped, makes every VPHY register dead on
silicon while every existing bench stays green. This is a FORWARD GUARD.

Run with:  make -C tests/csr_decode_width BLOCK=mdio_phy_model
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

VPHY_BASE = 0x44A30000            # shell-regmap.md v0.2 / shell_bd.tcl assign_bd_address

PHY_STATE = 0x00                  # rw
PHY_ID = 0x04                     # rw, reset default = C_PHY_ID_DEFAULT
LINK_EVENT = 0x08                 # rw (pulse)

PHY_ID_DEFAULT = 0x0007C0F1       # mdio_phy_model parameter C_PHY_ID_DEFAULT


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())

    # DUT-side MDIO group idle: the DUT is the MDIO master, and with oe=0 it is
    # not driving. Held idle so nothing perturbs the register file while the
    # decode is under test.
    dut.mdc_i.value = 0
    dut.mdio_o_i.value = 0
    dut.mdio_oe_i.value = 0

    dut.s_axi_aresetn.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """PHY_ID at BASE must read its non-zero reset default, not 0.

    PHY_ID is the ideal canary: it resets to 0x0007_C0F1, a value that cannot be
    confused with the all-zero read of a dead decode. On silicon a dead VPHY
    decode reads 0 for every register, so the host's PHY probe would conclude
    "no PHY present" -- indistinguishable from a wiring fault.
    """
    axi = await _bring_up(dut)

    at_zero, _ = await axi.read(PHY_ID)
    at_base, _ = await axi.read(VPHY_BASE + PHY_ID)

    assert at_zero == at_base, (
        "PHY_ID read 0x%08x at offset-from-zero but 0x%08x at the real base "
        "0x%08x. The decode is comparing the upper address bits the interconnect "
        "supplies, so VPHY is dead on hardware."
        % (at_zero, at_base, VPHY_BASE)
    )
    assert at_base == PHY_ID_DEFAULT, (
        "PHY_ID read back 0x%08x at the real base, expected the reset default "
        "0x%08x. A dead decode reads 0 for everything -- which is exactly how "
        "bug #1 hid on silicon." % (at_base, PHY_ID_DEFAULT)
    )


@cocotb.test(skip=NO_RTL)
async def test_writes_land_at_the_real_base_address(dut):
    """A write at BASE+PHY_ID must stick.

    PHY_ID is R/W: it is how the bench straps a synthetic PHY identity for the
    DUT's MDIO master to read back. If writes are dropped, the DUT always sees
    the compile-time default and no test can vary the PHY identity.
    """
    axi = await _bring_up(dut)

    val_in = 0xDEADBEEF
    await axi.write(VPHY_BASE + PHY_ID, val_in)
    val, _ = await axi.read(VPHY_BASE + PHY_ID)
    assert val == val_in, (
        "wrote 0x%08x to PHY_ID at the real base, read back 0x%08x. Writes are "
        "being ignored because the decode never matches." % (val_in, val)
    )

    await axi.write(VPHY_BASE + PHY_ID, PHY_ID_DEFAULT)
    val, _ = await axi.read(VPHY_BASE + PHY_ID)
    assert val == PHY_ID_DEFAULT, "PHY_ID stuck at 0x%08x after restore" % val


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_phy_id(dut):
    """The fix must not regress into a too-narrow decode.

    Capping at 16 bits decodes the block's own 64 KiB page. If the cap were made
    too narrow (say 5 bits, the pre-fix shape of the sibling blocks), BASE+0x1000
    would alias onto offset 0 and a stray write would silently clobber PHY_ID --
    the destructive-alias failure the original widening was meant to kill.
    """
    axi = await _bring_up(dut)

    await axi.write(VPHY_BASE + PHY_ID, PHY_ID_DEFAULT)

    # Offsets inside the 64 KiB page but far above the 3 real registers.
    for bad in (0x1000, 0x2000, 0x40, 0x80):
        await axi.write(VPHY_BASE + bad, 0xFFFFFFFF)

    val, _ = await axi.read(VPHY_BASE + PHY_ID)
    assert val == PHY_ID_DEFAULT, (
        "PHY_ID became 0x%08x after writes to unmapped offsets -- an unmapped "
        "offset is aliasing onto it (decode too narrow)." % val
    )
