"""Sanity-checks regmap.py's transcribed constants against
docs/contracts/shell-regmap.md and firmware/common/platform_regs.h — both
were read and cross-checked by hand when this file was written; these
tests exist to catch a *transcription* typo here, not to re-derive the
values from anywhere machine-readable (no such source exists yet).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import regmap as rm  # noqa: E402


def test_block_base_addresses():
    assert rm.CLKRST_BASE == 0x44A00000
    assert rm.DFXCTL_BASE == 0x44A10000
    assert rm.HWICAP_BASE == 0x44A20000
    assert rm.VPHY_BASE == 0x44A30000
    assert rm.USD_BASE == 0x44A40000
    assert rm.TELEM_BASE == 0x44A50000
    assert rm.GENCHK_BASE == 0x44A60000
    # Each block is a 64 KiB page per shell-regmap.md ("each block gets a
    # 64 KB page"); bases should be evenly spaced by that much.
    bases = [rm.CLKRST_BASE, rm.DFXCTL_BASE, rm.HWICAP_BASE, rm.VPHY_BASE,
             rm.USD_BASE, rm.TELEM_BASE, rm.GENCHK_BASE]
    for a, b in zip(bases, bases[1:]):
        assert b - a == 0x10000


def test_reset_ctrl_bit_positions():
    assert rm.RESET_CTRL_DUT_RESETN == 0x1
    assert rm.RESET_CTRL_RP_RESETN == 0x2
    assert rm.RESET_CTRL_DBG_RESETN == 0x4


def test_dfxctl_status_bit_positions():
    assert rm.DFXCTL_STATUS_DECOUPLED == 0x1
    assert rm.DFXCTL_STATUS_RP_IN_RESET == 0x2


def test_vphy_link_event_bits():
    assert rm.LINK_EVENT_FORCE_DOWN == 0x1
    assert rm.LINK_EVENT_PULSE == 0x2


def test_axi_lite_master_from_dut_binds_standard_names():
    """Every s_axi_* name AxiLiteMaster.from_dut() looks up must match the
    identical naming convention used across all four s_axi_* RTL blocks
    (verified by reading dut_clkrst.sv/dfx_ctl.sv/mdio_phy_model.sv/
    gen_checker.sv directly)."""

    class FakeDut:
        pass

    names = [
        "aclk", "awaddr", "awvalid", "awready", "wdata", "wstrb", "wvalid",
        "wready", "bresp", "bvalid", "bready", "araddr", "arvalid", "arready",
        "rdata", "rresp", "rvalid", "rready",
    ]
    dut = FakeDut()
    for n in names:
        setattr(dut, f"s_axi_{n}", object())

    master = rm.AxiLiteMaster.from_dut(dut)
    assert master.clk is dut.s_axi_aclk
    assert master.awaddr is dut.s_axi_awaddr
    assert master.rdata is dut.s_axi_rdata
