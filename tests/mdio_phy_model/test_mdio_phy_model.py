"""tests/mdio_phy_model/test_mdio_phy_model.py

Verifies `fpga/ethernet/mdio_phy_model/mdio_phy_model.sv` — the virtual-PHY
MDIO register model. This is one of only two blocks needing real new RTL
(ARCHITECTURE_SPEC.md §12) and the one the DUT's PHY bring-up depends on
directly: "without it the DUT's PHY bring-up/auto-negotiation poll hangs"
(spec §8.1). Per IMPLEMENTATION_PLAN.md WS 5.2 this bench is explicitly
*bench-first* (A5 cocotb before A1 finishes the RTL) — hence the most
detailed bench in this delivery, per the task brief.

Two independent register spaces are exercised together in every test here
(mdio_phy_model/README.md "Two register spaces — don't conflate them"):
  1. AXI-Lite `VPHY` regmap (shell-regmap.md @ 0x44A3_0000) — how the host/
     MicroBlaze configures what the model presents.
  2. MDIO-visible Clause-22 registers (BMCR/BMSR/PHYID1/PHYID2/ANAR/ANLPAR)
     — what the DUT MAC actually reads over MDC/MDIO.
That AXI-Lite-write -> MDIO-read round trip is exactly the "two-sided"
character spec §8.4 asks the whole subsystem to have, applied at this one
block's boundary.

Status: fpga/ethernet/mdio_phy_model/mdio_phy_model.sv exists as a real,
synthesizable-shaped Phase-0 STUB (verified 2026-07-04) -- the AXI-Lite
channel FSMs and the Clause-22 frame engine are `// TODO(A1)`, and
`mdio_i_o` is tied to `1'b1`. Every test below is skip-guarded on
dut_presence.rtl_ready() (removal of the "Phase 0 stub" marker), NOT on
bare file presence -- running these today would hang (AXI awready/arready
never assert; MDIO replies are a constant '1'), not fail cleanly. See
tests/common/dut_presence.py's docstring.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from dut_presence import rtl_ready
from mdio_master import (
    MDIOMaster, REG_BMCR, REG_BMSR, REG_PHYID1, REG_PHYID2, REG_ANAR, REG_ANLPAR,
    BMSR_AUTO_NEG_COMPLETE, BMSR_AUTO_NEG_ABILITY, BMSR_LINK_STATUS,
    BMSR_EXTENDED_CAP, ANLPAR_100BASE_TX_FULL,
    BMCR_RESET, BMCR_AN_ENABLE, BMCR_AN_RESTART,
)
from regmap import (
    AxiLiteMaster, VPHY_PHY_STATE, VPHY_PHY_ID, VPHY_LINK_EVENT,
    PHY_STATE_LINK_UP, PHY_STATE_SPEED100, PHY_STATE_FULL_DUPLEX,
    LINK_EVENT_FORCE_DOWN,
)

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "ethernet", "mdio_phy_model")
NO_RTL = not rtl_ready(_RTL_DIR, ["mdio_phy_model.sv"])

_MDIO_PHYAD = 1  # mdio_phy_model.sv default C_PHY_ADDR = 5'd1


async def _bring_up(dut):
    """Common setup for every test: clock s_axi_aclk, release
    s_axi_aresetn, and return bound (AxiLiteMaster, MDIOMaster) handles."""
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())  # 100 MHz
    dut.s_axi_aresetn.value = 0
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)

    axi = AxiLiteMaster.from_dut(dut)
    mdio = MDIOMaster(dut.mdc_i, dut.mdio_o_i, dut.mdio_oe_i, dut.mdio_i_o)
    return axi, mdio


@cocotb.test(skip=NO_RTL)
async def test_bmcr_bmsr_phyid_anar_anlpar_link_up_defaults(dut):
    """What this proves: out of reset (before the host writes anything to
    PHY_STATE/PHY_ID), the model already reports link-up/100/full over
    MDIO -- mdio_phy_model/README.md "Link-up defaults": "To avoid the
    DUT's bring-up hanging even before the host writes anything ... this
    model should reset into an already-link-up, 100/full state." BMSR bits
    [5,3,2,0] (auto-neg complete, auto-neg capable, link up, extended
    capability) must all be set at reset. This is the single most
    important property in this delivery: it's the difference between DUT
    PHY bring-up completing and hanging forever (spec §8.1).

    PHYID1/2 defaults are explicitly undecided in the README ("TODO(A1):
    pick and document an actual PHYID value with A3/A6") -- rather than
    assert a specific reset value, this test writes PHY_ID via the VPHY
    regmap first and checks the MDIO-visible PHYID1/2 split matches
    (PHY_ID[31:16]/[15:0] per the README table), which is the one thing
    the contract *does* pin down.
    """
    axi, mdio = await _bring_up(dut)

    bmsr = await mdio.read(_MDIO_PHYAD, REG_BMSR)
    for bit, name in ((BMSR_AUTO_NEG_COMPLETE, "auto-neg complete"),
                      (BMSR_AUTO_NEG_ABILITY, "auto-neg capable"),
                      (BMSR_LINK_STATUS, "link status"),
                      (BMSR_EXTENDED_CAP, "extended capability")):
        assert bmsr & bit, f"BMSR.{name} (bit {bit:#x}) must be set at reset, got {bmsr:#06x}"

    anlpar = await mdio.read(_MDIO_PHYAD, REG_ANLPAR)
    assert anlpar & ANLPAR_100BASE_TX_FULL, (
        f"ANLPAR must advertise 100BASE-TX full-duplex at reset (100/full "
        f"link-up default), got {anlpar:#06x}"
    )

    test_phy_id = 0x0007_C0F1  # arbitrary but plausible (SMSC/LAN8720-ID-space per README)
    await axi.write(VPHY_PHY_ID, test_phy_id)
    phyid1 = await mdio.read(_MDIO_PHYAD, REG_PHYID1)
    phyid2 = await mdio.read(_MDIO_PHYAD, REG_PHYID2)
    assert phyid1 == (test_phy_id >> 16) & 0xFFFF
    assert phyid2 == test_phy_id & 0xFFFF


@cocotb.test(skip=NO_RTL)
async def test_link_event_force_down_clears_bmsr_link_status(dut):
    """What this proves: a host-injected LINK_EVENT is observable by the
    DUT over MDIO -- ARCHITECTURE_SPEC.md §8.1: "MicroBlaze-writable so the
    host can inject link events (up/down, speed) to test the DUT's link
    handling." This is the concrete acceptance criterion IMPLEMENTATION_
    PLAN.md WS 5.2 names: "host-injected link-down observed by DUT." The
    AXI-Lite write (host/MicroBlaze side) and the MDIO read (DUT side) are
    two different partition-pin domains -- this is the two-sided check
    spec §8.4 asks for, applied at this block alone.
    """
    axi, mdio = await _bring_up(dut)

    bmsr_before = await mdio.read(_MDIO_PHYAD, REG_BMSR)
    assert bmsr_before & BMSR_LINK_STATUS, "link must start up before the injected-down check means anything"

    await axi.write(VPHY_LINK_EVENT, LINK_EVENT_FORCE_DOWN)
    bmsr_after = await mdio.read(_MDIO_PHYAD, REG_BMSR)
    assert not (bmsr_after & BMSR_LINK_STATUS), (
        f"BMSR.link_status must clear after LINK_EVENT.force_down=1, got {bmsr_after:#06x}"
    )

    # Re-attach path (spec §6.3): "virtual PHY re-asserts link-up" -- clearing
    # LINK_EVENT (as swap_fsm.c's step_release() does: mps3_reg_write32(...,
    # VPHY_LINK_EVENT, 0)) must bring link status back up.
    await axi.write(VPHY_LINK_EVENT, 0)
    bmsr_restored = await mdio.read(_MDIO_PHYAD, REG_BMSR)
    assert bmsr_restored & BMSR_LINK_STATUS, "link status must recover once force_down is cleared"


@cocotb.test(skip=NO_RTL)
async def test_phy_state_speed_duplex_reflected_in_anlpar(dut):
    """What this proves: PHY_STATE.speed100/full_duplex (host-configured)
    drive what the model tells the DUT it's linked to, via ANLPAR --
    README: "100BASE-TX full-duplex bit set when PHY_STATE.speed100 &
    full_duplex ... must be consistent with PHY_STATE." Sets speed100=0 (a
    non-default combination) to prove this is live logic, not a hardcoded
    ANLPAR value.
    """
    axi, mdio = await _bring_up(dut)

    await axi.write(VPHY_PHY_STATE, PHY_STATE_LINK_UP)  # link up, but NOT speed100/full_duplex
    anlpar = await mdio.read(_MDIO_PHYAD, REG_ANLPAR)
    assert not (anlpar & ANLPAR_100BASE_TX_FULL), (
        f"ANLPAR must NOT advertise 100BASE-TX-FD once PHY_STATE drops "
        f"speed100/full_duplex, got {anlpar:#06x}"
    )

    await axi.write(VPHY_PHY_STATE, PHY_STATE_LINK_UP | PHY_STATE_SPEED100 | PHY_STATE_FULL_DUPLEX)
    anlpar = await mdio.read(_MDIO_PHYAD, REG_ANLPAR)
    assert anlpar & ANLPAR_100BASE_TX_FULL, (
        f"ANLPAR must advertise 100BASE-TX-FD once PHY_STATE reasserts "
        f"speed100+full_duplex, got {anlpar:#06x}"
    )


# ANAR default the model reloads on a BMCR soft reset (phy_reg_model.sv
# ANAR_DEFAULT): sel=802.3 | 10BT | 10BT-FD | 100TX | 100TX-FD.
_ANAR_DEFAULT = 0x01E1


@cocotb.test(skip=NO_RTL)
async def test_bmcr_anar_writable_and_soft_reset_reloads_anar(dut):
    """What this proves — the MDIO WRITE path (previously never exercised by
    any bench; MDIOMaster.write() had no caller). A real PHY driver must be
    able to write BMCR/ANAR (spec §8.1 "the DUT's PHY driver drives them").
      * BMCR and ANAR are writable and echo back on read.
      * A BMCR soft reset (bit 15) reloads ANAR to its default advertisement,
        mimicking real PHY reset behaviour (phy_reg_model.sv), and the reset
        bit self-clears (reads back 0)."""
    axi, mdio = await _bring_up(dut)

    # BMCR is writable and persists (AN_ENABLE only — no self-clearing bits).
    await mdio.write(_MDIO_PHYAD, REG_BMCR, BMCR_AN_ENABLE)
    bmcr = await mdio.read(_MDIO_PHYAD, REG_BMCR)
    assert bmcr == BMCR_AN_ENABLE, f"BMCR must echo a plain write; got {bmcr:#06x}"

    # ANAR is writable; drive it OFF its default so the soft-reset reload is
    # observable.
    await mdio.write(_MDIO_PHYAD, REG_ANAR, 0x0021)
    anar = await mdio.read(_MDIO_PHYAD, REG_ANAR)
    assert anar == 0x0021, f"ANAR must echo a write; got {anar:#06x}"

    # BMCR soft reset: reloads ANAR to default, and bit 15 self-clears.
    await mdio.write(_MDIO_PHYAD, REG_BMCR, BMCR_RESET)
    anar = await mdio.read(_MDIO_PHYAD, REG_ANAR)
    assert anar == _ANAR_DEFAULT, (
        f"a BMCR soft reset must reload ANAR to {_ANAR_DEFAULT:#06x}; got {anar:#06x}")
    bmcr = await mdio.read(_MDIO_PHYAD, REG_BMCR)
    assert not (bmcr & BMCR_RESET), f"BMCR.reset must self-clear; got {bmcr:#06x}"


@cocotb.test(skip=NO_RTL)
async def test_bmcr_autoneg_restart_self_clears(dut):
    """What this proves: BMCR.restart-auto-neg (bit 9) behaves like real PHY
    hardware — it self-clears one mdc cycle after being set, while the other
    (non-self-clearing) BMCR bits written in the same frame persist. This is
    the write a PHY driver issues to re-run auto-negotiation."""
    axi, mdio = await _bring_up(dut)

    await mdio.write(_MDIO_PHYAD, REG_BMCR, BMCR_AN_ENABLE | BMCR_AN_RESTART)
    bmcr = await mdio.read(_MDIO_PHYAD, REG_BMCR)
    assert not (bmcr & BMCR_AN_RESTART), f"BMCR.an_restart must self-clear; got {bmcr:#06x}"
    assert bmcr & BMCR_AN_ENABLE, (
        f"non-self-clearing BMCR bits written alongside an_restart must persist; got {bmcr:#06x}")


@cocotb.test(skip=NO_RTL)
async def test_wrong_phyad_write_is_rejected(dut):
    """What this proves: the model answers/commits only for its strapped PHY
    address (C_PHY_ADDR=1) — a BMCR write addressed to a DIFFERENT phyad must
    NOT commit (mdio_slave.sv gates reg_wr_en on phy_match_q). Without this
    filter a multi-drop MDIO bus would corrupt the wrong PHY."""
    axi, mdio = await _bring_up(dut)

    await mdio.write(_MDIO_PHYAD, REG_BMCR, BMCR_AN_ENABLE)
    before = await mdio.read(_MDIO_PHYAD, REG_BMCR)
    assert before == BMCR_AN_ENABLE, f"seed BMCR; got {before:#06x}"

    # Write BMCR at the WRONG phyad (2 != strapped 1): must be ignored.
    wrong_phyad = (_MDIO_PHYAD + 1) & 0x1F
    await mdio.write(wrong_phyad, REG_BMCR, 0x0140)
    after = await mdio.read(_MDIO_PHYAD, REG_BMCR)
    assert after == BMCR_AN_ENABLE, (
        f"a wrong-phyad write must NOT change this model's BMCR; got {after:#06x}")
