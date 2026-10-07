"""tests/eth_mac_subsystem/test_eth_mac_subsystem.py

Integration bench for `fpga/ethernet/eth_mac_test_subsystem.sv` — the
ARCHITECTURE_SPEC.md §8 MAC-in-operation verification subsystem, exercised
END TO END through the wired datapath (roadmap step 9 / Phase 5). Unlike the
per-block benches, this drives the subsystem as a *system*: the bench plays
the DUT MAC on the RMII + MDIO partition pins (using the hand-rolled
tests/common helpers — cocotbext-eth is not installed), plays the host on the
LAN9220 uplink AXI-Stream, and plays the MicroBlaze on the VPHY/GENCHK
AXI-Lite control surfaces.

Datapath under test (spec §8.2), wired by the subsystem:
  DUT MAC ⇄ RMII+MDIO pins ⇄ rmii_phy_if + mdio_phy_model (virtual PHY)
          ⇄ link_partner_mac ⇄ {eth_bridge_3port, gen_checker tap+inject}
          ⇄ uplink/mgmt AXI-Stream (LAN9220 host + MicroBlaze).

Scenarios (task brief):
  (a) MDIO PHY bring-up — the DUT reads BMSR/PHYID/ANLPAR, sees autoneg
      complete + link up; host overrides PHY_ID and the DUT re-reads it.
  (b) frame exchange — DUT→host (RMII → MAC → bridge → uplink) and host→DUT
      (uplink → bridge → arbiter → MAC → RMII), integrity checked.
  (c) host-injected link up/down/pulse via VPHY LINK_EVENT, observed by the
      DUT over MDIO.
  (d) gen_checker injects bad_fcs/runt toward the DUT (reaches the DUT RMII,
      TX_CNT moves) and independently scores DUT-TX frames (good/bad_fcs/
      runt/giant → RX_CNT/ERR_CNT).
  (e) a clean-traffic run: N good DUT-TX frames, zero checker errors.

Clocking (genuinely different periods where CDC exists — spec §5):
  * refclk_i        = 50 MHz  — the RMII reference + whole datapath domain
                                (rmii_phy_if / link_partner_mac / bridge /
                                gen_checker incl. its GENCHK AXI-Lite).
  * s_axi_vphy_aclk = 100 MHz — VPHY AXI-Lite; crosses into the mdc domain
                                inside mdio_phy_model (the genuine CDC).
  * mdc             = 2.5 MHz — DUT-driven MDIO clock (bit-banged here).
Three unrelated periods → the VPHY AXI⇄mdc 2-flop synchronizers are exercised
under a real clock-ratio, not a same-clock shortcut.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from dut_presence import rtl_ready
from frames import build_frame, build_runt, build_giant, check_frame
from rmii import RmiiFrameDriver, RmiiFrameMonitor
from axis import AxisByteDriver, AxisByteMonitor
from mdio_master import (
    MDIOMaster, build_frame_bits, decode_read_reply, OP_READ,
    REG_BMSR, REG_PHYID1, REG_PHYID2, REG_ANLPAR,
    BMSR_AUTO_NEG_COMPLETE, BMSR_AUTO_NEG_ABILITY, BMSR_LINK_STATUS,
    BMSR_EXTENDED_CAP, ANLPAR_100BASE_TX_FULL,
)
from regmap import (
    AxiLiteMaster, VPHY_PHY_ID, VPHY_LINK_EVENT,
    LINK_EVENT_FORCE_DOWN, LINK_EVENT_PULSE,
    # GENCHK — the FROZEN shell-regmap.md / platform_regs.h layout (regmap.py
    # was updated from its dead Phase-0 draft as part of this workstream), so
    # this bench, the firmware, and the pyverify macgen driver share one set.
    GENCHK_CTRL, GENCHK_INJECT, GENCHK_TX_CNT, GENCHK_RX_CNT, GENCHK_ERR_CNT,
    GENCHK_CTRL_GEN_EN, GENCHK_CTRL_CHK_EN,
    GENCHK_INJECT_BAD_FCS, GENCHK_INJECT_RUNT,
)

# --------------------------------------------------------------------------- #
# Readiness gate: this bench elaborates ALL FIVE ethernet blocks plus the
# integration module. list_benches.py's schema gates on one dir/file (the
# subsystem); the test's own NO_RTL additionally requires every wired block to
# be de-stubbed (same belt-and-braces pattern as tests/link_partner_mac).
# --------------------------------------------------------------------------- #
_ETH = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "ethernet")
_BLOCKS = [
    ("rmii_phy_if", "rmii_phy_if.sv"),
    ("link_partner_mac", "link_partner_mac.sv"),
    ("bridge", "eth_bridge_3port.sv"),
    ("gen_checker", "gen_checker.sv"),
    ("mdio_phy_model", "mdio_phy_model.sv"),
]
NO_RTL = not (
    all(rtl_ready(os.path.join(_ETH, d), [f]) for d, f in _BLOCKS)
    and rtl_ready(_ETH, ["eth_mac_test_subsystem.sv"])
)

# Subsystem strap defaults (eth_mac_test_subsystem.sv parameters).
VPHY_PHY_ADDR = 1
VPHY_PHY_ID_DEFAULT = 0x0007_C0F1
BRIDGE_DUT_MAC = bytes.fromhex("020000000002")

# Frame actors.
BCAST = b"\xff" * 6
HOST_SRC = bytes.fromhex("020000000010")   # the LAN9220/host side
DUT_SRC = bytes.fromhex("020000000002")     # frames the DUT MAC sends
PREAMBLE_SFD = b"\x55" * 7 + b"\xd5"


class _Env:
    """Bound handles the bench plays each actor with."""
    __slots__ = ("axi_vphy", "genchk", "mdio", "rmii_tx", "rmii_rx",
                 "uplink_drv", "uplink_mon", "mgmt_mon")


async def _bring_up(dut):
    """Start the three clocks, reset both domains, and return bound actors."""
    cocotb.start_soon(Clock(dut.refclk_i, 20, unit="ns").start())          # 50 MHz
    cocotb.start_soon(Clock(dut.s_axi_vphy_aclk, 10, unit="ns").start())   # 100 MHz

    # Idle every input the bench owns before releasing reset.
    dut.rst_i.value = 1
    dut.s_axi_vphy_aresetn.value = 0
    dut.phy_rmii_txd_i.value = 0
    dut.phy_rmii_tx_en_i.value = 0
    dut.mdc_i.value = 0
    dut.mdio_o_i.value = 0
    dut.mdio_oe_i.value = 0
    for p in ("uplink", "mgmt"):
        getattr(dut, f"{p}_s_tdata").value = 0
        getattr(dut, f"{p}_s_tvalid").value = 0
        getattr(dut, f"{p}_s_tlast").value = 0
    for pfx in ("s_axi_vphy_", "s_axi_genchk_"):
        for n in ("awaddr", "awprot", "awvalid", "wdata", "wstrb", "wvalid",
                  "bready", "araddr", "arprot", "arvalid", "rready"):
            getattr(dut, f"{pfx}{n}").value = 0

    await Timer(200, unit="ns")
    dut.rst_i.value = 0
    dut.s_axi_vphy_aresetn.value = 1
    await RisingEdge(dut.refclk_i)

    env = _Env()
    env.axi_vphy = AxiLiteMaster.from_dut(dut, prefix="s_axi_vphy_")
    # GENCHK AXI-Lite is on refclk_i (gen_checker fuses control+datapath clock,
    # subsystem R4) — no s_axi_genchk_aclk port, so bind the BFM by hand.
    g = lambda n: getattr(dut, f"s_axi_genchk_{n}")  # noqa: E731
    env.genchk = AxiLiteMaster(
        clk=dut.refclk_i,
        awaddr=g("awaddr"), awvalid=g("awvalid"), awready=g("awready"),
        wdata=g("wdata"), wstrb=g("wstrb"), wvalid=g("wvalid"), wready=g("wready"),
        bresp=g("bresp"), bvalid=g("bvalid"), bready=g("bready"),
        araddr=g("araddr"), arvalid=g("arvalid"), arready=g("arready"),
        rdata=g("rdata"), rresp=g("rresp"), rvalid=g("rvalid"), rready=g("rready"))

    # DUT plays the MDIO master + the RMII MAC (drives TX, samples RX).
    env.mdio = MDIOMaster(dut.mdc_i, dut.mdio_o_i, dut.mdio_oe_i, dut.mdio_i_o)
    env.rmii_tx = RmiiFrameDriver(dut.phy_rmii_ref_clk_o, dut.phy_rmii_tx_en_i,
                                  dut.phy_rmii_txd_i, bits_per_symbol=2)
    env.rmii_rx = RmiiFrameMonitor(dut.phy_rmii_ref_clk_o, dut.phy_rmii_crs_dv_o,
                                   dut.phy_rmii_rxd_o, bits_per_symbol=2)

    # Host on the uplink; bridge egress monitors are always-ready so the
    # (store-and-forward) bridge always drains, whether or not Python reads.
    env.uplink_drv = AxisByteDriver(dut.refclk_i, dut.uplink_s_tdata,
                                    dut.uplink_s_tvalid, dut.uplink_s_tready,
                                    tlast=dut.uplink_s_tlast)
    env.uplink_mon = AxisByteMonitor(dut.refclk_i, dut.uplink_m_tdata,
                                     dut.uplink_m_tvalid, dut.uplink_m_tready,
                                     tlast=dut.uplink_m_tlast)
    env.mgmt_mon = AxisByteMonitor(dut.refclk_i, dut.mgmt_m_tdata,
                                   dut.mgmt_m_tvalid, dut.mgmt_m_tready,
                                   tlast=dut.mgmt_m_tlast)
    return env


async def _mdio_read_short(mdio, phyad, regad):
    """A no-preamble Clause-22 read (mdio_slave.sv tolerates zero preamble):
    32 mdc cycles total, so DATA is sampled ~cycles 16-31 — INSIDE the
    32-mdc-cycle LINK_EVENT.pulse hold that a normal 64-cycle read (with its
    32-bit preamble) would outlast. Used only to catch the transient pulse-down
    on the DUT side; a normal read is used everywhere else.
    """
    bits = build_frame_bits(OP_READ, phyad, regad, include_preamble=False)
    header_len = len(bits) - 18
    for b in bits[:header_len]:
        await mdio._clock_bit(b)
    sampled = []
    for _ in range(18):
        mdio.mdio_oe.value = 0
        mdio.mdc.value = 0
        await Timer(mdio._half, unit="ns")
        mdio.mdc.value = 1
        sampled.append(int(mdio.mdio_i.value))
        await Timer(mdio._half, unit="ns")
    value, _ = decode_read_reply(sampled)
    return value


async def _read_counts(genchk):
    rx, _ = await genchk.read(GENCHK_RX_CNT)
    err, _ = await genchk.read(GENCHK_ERR_CNT)
    return rx, err


async def _dut_tx_and_score(dut, env, frame, settle_polls: int = 400):
    """The DUT transmits `frame` on RMII; wait until the checker has scored it
    (RX_CNT moves at the bridge-ingress tlast), then return the (rx, err)
    deltas. dst=DUT_MAC so the bridge drops it to its own ingress (no slow
    flood-forward), leaving recv_q ready for the next frame quickly."""
    rx0, err0 = await _read_counts(env.genchk)
    await env.rmii_tx.send_frame(PREAMBLE_SFD + frame)
    rx = rx0
    for _ in range(settle_polls):
        rx, _ = await env.genchk.read(GENCHK_RX_CNT)
        if rx != rx0:
            break
    else:
        raise TimeoutError("checker never counted the DUT-TX frame")
    _rx, err = await _read_counts(env.genchk)
    return rx - rx0, err - err0


# =========================================================================== #
# (a) MDIO PHY bring-up
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_a_mdio_phy_bringup(dut):
    """What this proves: through the wired subsystem, the DUT's PHY bring-up
    completes — reading the virtual PHY over MDIO returns auto-neg-complete +
    link-up + a 100/full ANLPAR (spec §8.1: without this the DUT's poll hangs),
    and a host PHY_ID override via the VPHY AXI-Lite surface is seen by the DUT
    on its next MDIO read (the AXI⇄mdc CDC, exercised at a real clock ratio)."""
    env = await _bring_up(dut)

    bmsr = await env.mdio.read(VPHY_PHY_ADDR, REG_BMSR)
    for bit, name in ((BMSR_AUTO_NEG_COMPLETE, "auto-neg complete"),
                      (BMSR_AUTO_NEG_ABILITY, "auto-neg capable"),
                      (BMSR_LINK_STATUS, "link status"),
                      (BMSR_EXTENDED_CAP, "extended capability")):
        assert bmsr & bit, f"BMSR.{name} must be set at bring-up, got {bmsr:#06x}"

    phyid1 = await env.mdio.read(VPHY_PHY_ADDR, REG_PHYID1)
    phyid2 = await env.mdio.read(VPHY_PHY_ADDR, REG_PHYID2)
    assert (phyid1 << 16 | phyid2) == VPHY_PHY_ID_DEFAULT, (
        f"default PHYID must be {VPHY_PHY_ID_DEFAULT:#010x}, "
        f"got {(phyid1 << 16 | phyid2):#010x}")

    anlpar = await env.mdio.read(VPHY_PHY_ADDR, REG_ANLPAR)
    assert anlpar & ANLPAR_100BASE_TX_FULL, (
        f"ANLPAR must advertise 100BASE-TX full-duplex at bring-up, got {anlpar:#06x}")

    # Host overrides PHY_ID over the VPHY AXI-Lite surface; DUT re-reads it.
    await env.axi_vphy.write(VPHY_PHY_ID, 0x1234_ABCD)
    phyid1 = await env.mdio.read(VPHY_PHY_ADDR, REG_PHYID1)
    phyid2 = await env.mdio.read(VPHY_PHY_ADDR, REG_PHYID2)
    assert phyid1 == 0x1234 and phyid2 == 0xABCD, (
        f"host PHY_ID override must reach the DUT's MDIO read, "
        f"got {phyid1:#06x}/{phyid2:#06x}")


# =========================================================================== #
# (b) frame exchange
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_b_frame_exchange_dut_to_host(dut):
    """What this proves the DUT→host half of the datapath: a frame the DUT MAC
    transmits on RMII is deserialized by rmii_phy_if, recovered by
    link_partner_mac, and forwarded by the bridge onto the LAN9220 uplink
    byte-identical (preamble/SFD stripped, FCS intact)."""
    env = await _bring_up(dut)
    frame = build_frame(BCAST, DUT_SRC, 0x0800, b"dut-to-host-uplink")

    cocotb.start_soon(env.rmii_tx.send_frame(PREAMBLE_SFD + frame))
    got, _user = await env.uplink_mon.read_packet()
    assert got == frame, (
        f"DUT→host frame must reach the uplink intact: sent {frame.hex()}, "
        f"got {got.hex()}")


@cocotb.test(skip=NO_RTL)
async def test_b_frame_exchange_host_to_dut(dut):
    """What this proves the host→DUT half: a frame the host injects on the
    LAN9220 uplink, addressed to the DUT MAC, is routed by the bridge, wins the
    TX arbiter, is framed by link_partner_mac (preamble+SFD), and appears on
    the DUT-facing RMII pins as a real wire frame."""
    env = await _bring_up(dut)
    frame = build_frame(BRIDGE_DUT_MAC, HOST_SRC, 0x0800, b"host-to-dut-rmii")

    cocotb.start_soon(env.uplink_drv.write(frame))
    recovered = await env.rmii_rx.recv_frame()
    assert recovered is not None, "no carrier appeared on the DUT-facing RMII pins"
    assert recovered == PREAMBLE_SFD + frame, (
        f"host→DUT frame must arrive on RMII as preamble+SFD+frame: expected "
        f"{(PREAMBLE_SFD + frame).hex()}, got {recovered.hex()}")


# =========================================================================== #
# (c) host-injected link events (observed on the DUT side over MDIO)
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_c_link_event_force_down_and_recover(dut):
    """What this proves: a host LINK_EVENT.force_down (VPHY AXI-Lite) clears
    BMSR.link_status as seen by the DUT over MDIO, and clearing it restores
    link-up — the down/up injection + the spec §6.3 post-swap re-attach path
    (swap_fsm.c step_release writes VPHY_LINK_EVENT=0)."""
    env = await _bring_up(dut)

    bmsr = await env.mdio.read(VPHY_PHY_ADDR, REG_BMSR)
    assert bmsr & BMSR_LINK_STATUS, "link must start up for the down-event to mean anything"

    await env.axi_vphy.write(VPHY_LINK_EVENT, LINK_EVENT_FORCE_DOWN)
    bmsr = await env.mdio.read(VPHY_PHY_ADDR, REG_BMSR)
    assert not (bmsr & BMSR_LINK_STATUS), (
        f"DUT must see link down after host force_down, got BMSR={bmsr:#06x}")

    await env.axi_vphy.write(VPHY_LINK_EVENT, 0)
    bmsr = await env.mdio.read(VPHY_PHY_ADDR, REG_BMSR)
    assert bmsr & BMSR_LINK_STATUS, (
        f"DUT must see link recover after force_down clears, got BMSR={bmsr:#06x}")


@cocotb.test(skip=NO_RTL)
async def test_c_link_event_pulse(dut):
    """What this proves: a single host LINK_EVENT.pulse injects a transient
    link-down (no matching follow-up write) that the DUT observes over MDIO and
    that then auto-releases — the link-change-interrupt stimulus of spec §8.1.
    The transient is caught with a no-preamble MDIO read (see _mdio_read_short);
    a following normal read confirms auto-recovery."""
    env = await _bring_up(dut)

    bmsr = await env.mdio.read(VPHY_PHY_ADDR, REG_BMSR)
    assert bmsr & BMSR_LINK_STATUS, "link must start up before the pulse"

    await env.axi_vphy.write(VPHY_LINK_EVENT, LINK_EVENT_PULSE)
    bmsr_down = await _mdio_read_short(env.mdio, VPHY_PHY_ADDR, REG_BMSR)
    assert not (bmsr_down & BMSR_LINK_STATUS), (
        f"DUT must see the pulse's transient link-down, got BMSR={bmsr_down:#06x}")

    bmsr_up = await env.mdio.read(VPHY_PHY_ADDR, REG_BMSR)
    assert bmsr_up & BMSR_LINK_STATUS, (
        f"pulse must auto-release with no second host write, got BMSR={bmsr_up:#06x}")


# =========================================================================== #
# (d) gen_checker in-line: inject toward the DUT + independently score DUT-TX
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_d_generator_injects_bad_fcs_to_dut(dut):
    """What this proves: an INJECT.bad_fcs frame from gen_checker traverses the
    real TX path (arbiter → link_partner_mac → rmii_phy_if) and lands on the
    DUT-facing RMII pins as a genuinely corrupted-FCS frame, with TX_CNT
    counting it — the injection half of spec §8.4."""
    env = await _bring_up(dut)
    await env.genchk.write(GENCHK_INJECT, GENCHK_INJECT_BAD_FCS)
    await env.genchk.write(GENCHK_CTRL, GENCHK_CTRL_GEN_EN)          # free-run start

    recovered = await env.rmii_rx.recv_frame()
    await env.genchk.write(GENCHK_CTRL, 0)                    # stop after first
    assert recovered is not None, "generator frame never reached the DUT RMII"

    frame = recovered[len(PREAMBLE_SFD):]                     # strip preamble+SFD
    fcs_ok, _len_ok, _reason = check_frame(frame)
    assert not fcs_ok, "injected bad_fcs frame must reach the DUT with a corrupt FCS"

    tx, _ = await env.genchk.read(GENCHK_TX_CNT)
    assert tx > 0, "TX_CNT must count frames the generator sent toward the DUT"


@cocotb.test(skip=NO_RTL)
async def test_d_generator_injects_runt_to_dut(dut):
    """What this proves: INJECT.runt reaches the DUT RMII as a sub-64-byte
    frame through the same wired TX path (a length-envelope violation the DUT
    MAC's RX path must reject)."""
    env = await _bring_up(dut)
    await env.genchk.write(GENCHK_INJECT, GENCHK_INJECT_RUNT)
    await env.genchk.write(GENCHK_CTRL, GENCHK_CTRL_GEN_EN)

    recovered = await env.rmii_rx.recv_frame()
    await env.genchk.write(GENCHK_CTRL, 0)
    assert recovered is not None, "runt generator frame never reached the DUT RMII"

    frame = recovered[len(PREAMBLE_SFD):]
    _fcs_ok, length_ok, reason = check_frame(frame)
    assert not length_ok and "runt" in reason, (
        f"injected runt must arrive under 64 bytes on the DUT RMII: {reason} "
        f"(len={len(frame)})")


@cocotb.test(skip=NO_RTL)
async def test_d_checker_scores_dut_tx_frames(dut):
    """What this proves the scoring half of spec §8.4: the checker,
    tapped off the DUT-TX-recovered stream at the bridge ingress, counts every
    DUT frame (RX_CNT) and independently flags the malformed ones (ERR_CNT) —
    a good frame passes, bad-FCS/runt/giant each score an error. This
    exercises the checker-tap gate reconciliation (subsystem R1: the tap sees
    only bridge-committed beats)."""
    env = await _bring_up(dut)
    await env.genchk.write(GENCHK_CTRL, GENCHK_CTRL_CHK_EN)   # enable (clears counters)

    d_rx, d_err = await _dut_tx_and_score(
        dut, env, build_frame(BRIDGE_DUT_MAC, DUT_SRC, 0x0800, b"clean"))
    assert (d_rx, d_err) == (1, 0), f"good DUT frame: expected (1,0), got ({d_rx},{d_err})"

    d_rx, d_err = await _dut_tx_and_score(
        dut, env, build_frame(BRIDGE_DUT_MAC, DUT_SRC, 0x0800, b"crc", bad_fcs=True))
    assert (d_rx, d_err) == (1, 1), f"bad-FCS DUT frame: expected (1,1), got ({d_rx},{d_err})"

    d_rx, d_err = await _dut_tx_and_score(dut, env, build_runt(BRIDGE_DUT_MAC, DUT_SRC))
    assert (d_rx, d_err) == (1, 1), f"runt DUT frame: expected (1,1), got ({d_rx},{d_err})"

    d_rx, d_err = await _dut_tx_and_score(
        dut, env, build_giant(BRIDGE_DUT_MAC, DUT_SRC, extra=4))
    assert (d_rx, d_err) == (1, 1), f"giant DUT frame: expected (1,1), got ({d_rx},{d_err})"


# =========================================================================== #
# (e) clean traffic — zero errors
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_e_clean_traffic_zero_errors(dut):
    """What this proves: a run of well-formed DUT-TX frames of varied length
    is scored with RX_CNT advancing once per frame and ERR_CNT never moving —
    the negative control that makes every positive case above meaningful."""
    env = await _bring_up(dut)
    await env.genchk.write(GENCHK_CTRL, GENCHK_CTRL_CHK_EN)

    total_rx = total_err = 0
    for payload in (b"a" * 20, b"b" * 46, b"c" * 100, b"payload-four"):
        d_rx, d_err = await _dut_tx_and_score(
            dut, env, build_frame(BRIDGE_DUT_MAC, DUT_SRC, 0x0800, payload))
        total_rx += d_rx
        total_err += d_err

    assert total_rx == 4, f"every clean frame must be counted: RX delta {total_rx} != 4"
    assert total_err == 0, f"clean traffic must score zero errors, got {total_err}"
