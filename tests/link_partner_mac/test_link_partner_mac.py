"""tests/link_partner_mac/test_link_partner_mac.py

Verifies `fpga/ethernet/link_partner_mac/link_partner_mac.sv` PAIRED with
`fpga/ethernet/rmii_phy_if/rmii_phy_if.sv` (via the bench-local wrapper
`tb_link_partner_pair.sv` — see its header for why the pair, not the leaf,
is the meaningful unit here): the shell-side "link partner" MAC of
ARCHITECTURE_SPEC.md §8.1, driven/observed only at the outer interfaces
(RMII partition pins on one side, bridge-facing AXI-Stream on the other).

What this proves:
  1. A frame written on s_axis_tx_* appears on the DUT-facing RMII pins as
     7x0x55 preamble + 0xD5 SFD + the frame bytes, dibit-exact.
  2. A DUT TX frame (preamble+SFD+frame driven on phy_rmii_txd/tx_en) is
     recovered on m_axis_rx_* with preamble/SFD stripped and tuser=0.
  3. The MAC's independent FCS check: the same frame with a corrupted FCS
     recovers byte-identical but with m_axis_rx_tuser=1 (the error tap
     gen_checker cross-checks, spec §8.4).

AXIS frames include the FCS in both directions (the convention shared with
gen_checker and tests/common/frames.py — see link_partner_mac.sv header).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer

from dut_presence import rtl_ready
from frames import build_frame
from rmii import RmiiFrameDriver, RmiiFrameMonitor
from axis import AxisByteDriver, AxisByteMonitor

_ETH_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "ethernet")
# Gated on BOTH RTL blocks the wrapper elaborates (list_benches.py's entry
# can only gate on this block's own dir; the pairing needs rmii_phy_if too).
NO_RTL = not (rtl_ready(os.path.join(_ETH_DIR, "link_partner_mac"), ["link_partner_mac.sv"])
              and rtl_ready(os.path.join(_ETH_DIR, "rmii_phy_if"), ["rmii_phy_if.sv"]))

DST = bytes.fromhex("001122334455")
SRC = bytes.fromhex("aabbccddeeff")
PREAMBLE_SFD = b"\x55" * 7 + b"\xd5"


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.refclk_i, 20, unit="ns").start())  # 50 MHz
    dut.rst_i.value = 1
    dut.phy_rmii_txd_i.value = 0
    dut.phy_rmii_tx_en_i.value = 0
    dut.s_axis_tx_tdata.value = 0
    dut.s_axis_tx_tvalid.value = 0
    dut.s_axis_tx_tlast.value = 0
    dut.m_axis_rx_tready.value = 0
    await Timer(100, unit="ns")
    dut.rst_i.value = 0
    await Timer(50, unit="ns")


@cocotb.test(skip=NO_RTL)
async def test_axis_tx_frame_reaches_rmii_with_preamble(dut):
    """What this proves: the MAC frames an AXIS packet (preamble+SFD
    insertion, nibble serialization) and rmii_phy_if serializes it onto the
    DUT-facing RMII pins — recovered dibit stream == preamble+SFD+frame."""
    await _bring_up(dut)
    drv = AxisByteDriver(dut.refclk_i, dut.s_axis_tx_tdata, dut.s_axis_tx_tvalid,
                         dut.s_axis_tx_tready, tlast=dut.s_axis_tx_tlast)
    mon = RmiiFrameMonitor(dut.phy_rmii_ref_clk_o, dut.phy_rmii_crs_dv_o,
                           dut.phy_rmii_rxd_o, bits_per_symbol=2)

    frame = build_frame(DST, SRC, 0x0800, b"mac-tx-path")
    cocotb.start_soon(drv.write(frame))
    recovered = await mon.recv_frame()

    assert recovered is not None, "no carrier observed on phy_rmii_{crs_dv,rxd}"
    assert recovered == PREAMBLE_SFD + frame, (
        f"RMII must carry preamble+SFD+frame: expected "
        f"{(PREAMBLE_SFD + frame).hex()}, got {recovered.hex()}"
    )


@cocotb.test(skip=NO_RTL)
async def test_rmii_frame_recovered_as_axis_with_good_fcs(dut):
    """What this proves: a DUT-TX frame (with real preamble/SFD, as the DUT
    MAC would send) is deserialized by rmii_phy_if and recovered by the MAC
    on m_axis_rx_* with framing stripped, byte-exact, tuser=0."""
    await _bring_up(dut)
    drv = RmiiFrameDriver(dut.phy_rmii_ref_clk_o, dut.phy_rmii_tx_en_i,
                          dut.phy_rmii_txd_i, bits_per_symbol=2)
    mon = AxisByteMonitor(dut.refclk_i, dut.m_axis_rx_tdata, dut.m_axis_rx_tvalid,
                          dut.m_axis_rx_tready, tlast=dut.m_axis_rx_tlast,
                          tuser=dut.m_axis_rx_tuser)

    frame = build_frame(DST, SRC, 0x0800, b"mac-rx-path")
    cocotb.start_soon(drv.send_frame(PREAMBLE_SFD + frame))
    got, user = await mon.read_packet()

    assert got == frame, (
        f"recovered AXIS frame must be the wire frame minus preamble/SFD: "
        f"sent {frame.hex()}, got {got.hex()}"
    )
    assert user == 0, "a good-FCS frame must not set m_axis_rx_tuser"


@cocotb.test(skip=NO_RTL)
async def test_rmii_bad_fcs_sets_tuser(dut):
    """What this proves: the MAC's independent FCS check — the same frame
    with a corrupted FCS still recovers byte-identical (nothing dropped)
    but arrives with m_axis_rx_tuser=1, the per-frame error tap the bridge
    passes through and gen_checker cross-checks (spec §8.4)."""
    await _bring_up(dut)
    drv = RmiiFrameDriver(dut.phy_rmii_ref_clk_o, dut.phy_rmii_tx_en_i,
                          dut.phy_rmii_txd_i, bits_per_symbol=2)
    mon = AxisByteMonitor(dut.refclk_i, dut.m_axis_rx_tdata, dut.m_axis_rx_tvalid,
                          dut.m_axis_rx_tready, tlast=dut.m_axis_rx_tlast,
                          tuser=dut.m_axis_rx_tuser)

    frame = build_frame(DST, SRC, 0x0800, b"mac-rx-path", bad_fcs=True)
    cocotb.start_soon(drv.send_frame(PREAMBLE_SFD + frame))
    got, user = await mon.read_packet()

    assert got == frame, "a bad-FCS frame must still be delivered intact"
    assert user == 1, "a bad-FCS frame must set m_axis_rx_tuser"
