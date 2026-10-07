"""tests/rmii_phy_if/test_rmii_phy_if.py

Verifies `fpga/ethernet/rmii_phy_if/rmii_phy_if.sv` — the RMII (DUT-facing,
2-bit@50MHz) <-> MII (shell-facing, 4-bit@25MHz) datapath conversion that
lets the shell act as the DUT's "virtual PHY" (ARCHITECTURE_SPEC.md §8.1).

What this proves: a byte sequence driven in on one side of the conversion
(as `frames.build_frame()` bytes) is recoverable, unchanged, on the other
side — once for each direction:
  1. MII-in (as if from link_partner_mac's TX) -> RMII-out (toward the
     DUT MAC's RX, `phy_rmii_{crs_dv,rxd}`).
  2. RMII-in (as if from the DUT MAC's TX, `phy_rmii_{txd,tx_en}` — this is
     also where the HDPR-29 re-register stage lives, see dut_notes.md) ->
     MII-out (toward link_partner_mac's RX).

Bit order (old TODO(A5) — resolved 2026-07-06, W-RTL-ETH): the landed
conversion RTL is LSB-first within each byte on both sides (RMII dibit0 =
bits[1:0], MII nibble0 = bits[3:0]) per IEEE 802.3 Annex 22B, which is
exactly the order tests/common/rmii.py's driver/monitor pair already
emitted/decoded — so this bench's round trip now checks the real contract,
not merely driver/monitor self-consistency. See dut_notes.md.
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

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "ethernet", "rmii_phy_if")
NO_RTL = not rtl_ready(_RTL_DIR, ["rmii_phy_if.sv"])

DST = bytes.fromhex("001122334455")
SRC = bytes.fromhex("aabbccddeeff")


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.refclk_i, 20, units="ns").start())  # 50 MHz board ref
    dut.rst_i.value = 1
    dut.mii_txd_i.value = 0
    dut.mii_tx_en_i.value = 0
    dut.phy_rmii_txd_i.value = 0
    dut.phy_rmii_tx_en_i.value = 0
    await Timer(100, units="ns")
    dut.rst_i.value = 0
    await Timer(50, units="ns")


@cocotb.test(skip=NO_RTL)
async def test_mii_tx_path_reaches_rmii_rx_pins(dut):
    """What this proves: a frame driven in on the MII side (mii_txd_i/
    mii_tx_en_i, as if from link_partner_mac's TX) is serialized out onto
    the DUT-facing RMII pins (phy_rmii_crs_dv_o/phy_rmii_rxd_o) with its
    byte content intact.
    """
    await _bring_up(dut)
    driver = RmiiFrameDriver(dut.mii_tx_clk_o, dut.mii_tx_en_i, dut.mii_txd_i, bits_per_symbol=4)
    monitor = RmiiFrameMonitor(dut.phy_rmii_ref_clk_o, dut.phy_rmii_crs_dv_o,
                                dut.phy_rmii_rxd_o, bits_per_symbol=2)

    frame = build_frame(DST, SRC, 0x0800, b"mii-to-rmii")
    cocotb.start_soon(driver.send_frame(frame))
    recovered = await monitor.recv_frame()

    assert recovered is not None, "no frame observed on phy_rmii_{crs_dv,rxd} within timeout"
    assert recovered == frame, (
        f"RMII-side bytes must match the MII-side input: "
        f"sent {frame.hex()}, recovered {recovered.hex()}"
    )


@cocotb.test(skip=NO_RTL)
async def test_rmii_tx_path_reaches_mii_rx_pins(dut):
    """What this proves: a frame driven in on the DUT-facing RMII pins
    (phy_rmii_txd_i/phy_rmii_tx_en_i, as if from the DUT MAC's TX -- this
    is also where the HDPR-29 static-side re-register stage samples first,
    see dut_notes.md) is de-serialized onto the shell-internal MII side
    (mii_rxd_o/mii_rx_dv_o) toward link_partner_mac, with its byte content
    intact.
    """
    await _bring_up(dut)
    driver = RmiiFrameDriver(dut.phy_rmii_ref_clk_o, dut.phy_rmii_tx_en_i,
                              dut.phy_rmii_txd_i, bits_per_symbol=2)
    monitor = RmiiFrameMonitor(dut.mii_rx_clk_o, dut.mii_rx_dv_o, dut.mii_rxd_o, bits_per_symbol=4)

    frame = build_frame(DST, SRC, 0x0800, b"rmii-to-mii")
    cocotb.start_soon(driver.send_frame(frame))
    recovered = await monitor.recv_frame()

    assert recovered is not None, "no frame observed on mii_{rx_dv,rxd} within timeout"
    assert recovered == frame, (
        f"MII-side bytes must match the RMII-side input: "
        f"sent {frame.hex()}, recovered {recovered.hex()}"
    )
