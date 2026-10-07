"""tests/bridge/test_bridge.py

Verifies `fpga/ethernet/bridge/eth_bridge_3port.sv` — the fixed 3-port L2
bridge (management / DUT-MAC / LAN9220-uplink), dest-MAC parse + small
forwarding table + flood-on-miss (ARCHITECTURE_SPEC.md §9, decision D9).
Deliberately not a learning switch/VLAN/STP core — see the block's README.

What this proves:
  1. A frame addressed to a known MAC routes to exactly one egress port
     (the one behind that MAC), not the others.
  2. A frame with an unrecognized destination MAC floods to every port
     *except* the one it arrived on (flood-on-miss, D9) — it must not be
     reflected back out its own ingress port.

MAC addresses used here are placeholders standing in for whatever compile-
time constants A1 picks (see dut_notes.md's ambiguity note — there is no
regmap block to configure these at runtime in v0).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer

from dut_presence import rtl_ready
from frames import build_frame
from axis import AxisByteDriver, AxisByteMonitor

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "ethernet", "bridge")
NO_RTL = not rtl_ready(_RTL_DIR, ["eth_bridge_3port.sv"])

# Placeholders -- see dut_notes.md ambiguity note.
MGMT_MAC = bytes.fromhex("020000000001")
DUT_MAC = bytes.fromhex("020000000002")
UNKNOWN_MAC = bytes.fromhex("020000000099")
SRC = bytes.fromhex("aabbccddeeff")

_NO_FRAME_TIMEOUT_CYCLES = 200


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.clk_i, 10, units="ns").start())
    dut.rst_i.value = 1
    await Timer(50, units="ns")
    dut.rst_i.value = 0
    await Timer(20, units="ns")

    ports = {}
    for name in ("mgmt", "dut_mac", "uplink"):
        s_tuser = getattr(dut, f"{name}_s_tuser", None)
        ports[name] = {
            "drv": AxisByteDriver(dut.clk_i, getattr(dut, f"{name}_s_tdata"),
                                   getattr(dut, f"{name}_s_tvalid"), getattr(dut, f"{name}_s_tready"),
                                   tlast=getattr(dut, f"{name}_s_tlast"), tuser=s_tuser),
            "mon": AxisByteMonitor(dut.clk_i, getattr(dut, f"{name}_m_tdata"),
                                    getattr(dut, f"{name}_m_tvalid"), getattr(dut, f"{name}_m_tready"),
                                    tlast=getattr(dut, f"{name}_m_tlast")),
        }
    return ports


async def _expect_no_frame(mon: AxisByteMonitor, port_name: str):
    try:
        got, _user = await mon.read_packet(timeout_cycles=_NO_FRAME_TIMEOUT_CYCLES)
    except TimeoutError:
        return
    raise AssertionError(f"unexpected frame on {port_name}_m_*: {got.hex()}")


@cocotb.test(skip=NO_RTL)
async def test_known_dest_mac_routes_to_single_port(dut):
    """What this proves: a frame arriving on the uplink port addressed to
    the DUT's MAC egresses ONLY on dut_mac_m_*, not mgmt_m_* (and not
    reflected back on uplink_m_*)."""
    ports = await _bring_up(dut)
    frame = build_frame(DUT_MAC, SRC, 0x0800, b"to-dut")

    cocotb.start_soon(ports["uplink"]["drv"].write(frame))
    got, _user = await ports["dut_mac"]["mon"].read_packet()
    assert got == frame, "frame addressed to DUT_MAC must arrive intact on dut_mac_m_*"

    await _expect_no_frame(ports["mgmt"]["mon"], "mgmt")
    await _expect_no_frame(ports["uplink"]["mon"], "uplink")


@cocotb.test(skip=NO_RTL)
async def test_unknown_dest_mac_floods_other_ports(dut):
    """What this proves: a frame with an unrecognized destination MAC
    floods to every port except the one it arrived on (D9's flood-on-
    miss default) — arriving on mgmt, it must reach both dut_mac_m_* and
    uplink_m_*, and must NOT be reflected back out mgmt_m_*."""
    ports = await _bring_up(dut)
    frame = build_frame(UNKNOWN_MAC, SRC, 0x0800, b"flood-me")

    cocotb.start_soon(ports["mgmt"]["drv"].write(frame))

    got_dut, _ = await ports["dut_mac"]["mon"].read_packet()
    got_uplink, _ = await ports["uplink"]["mon"].read_packet()
    assert got_dut == frame, "flood-on-miss must reach dut_mac_m_*"
    assert got_uplink == frame, "flood-on-miss must reach uplink_m_*"

    await _expect_no_frame(ports["mgmt"]["mon"], "mgmt")


@cocotb.test(skip=NO_RTL)
async def test_dut_mac_tuser_error_flag_does_not_break_routing(dut):
    """What this proves: dut_mac_s_tuser (link_partner_mac's frame-error
    passthrough) doesn't corrupt or block routing of an otherwise-
    well-formed frame -- a minimal smoke check given there's no contract
    text specifying what the bridge should *do* with tuser beyond
    accepting it (see dut_notes.md ambiguity note)."""
    ports = await _bring_up(dut)
    frame = build_frame(MGMT_MAC, SRC, 0x0800, b"error-flagged")

    cocotb.start_soon(ports["dut_mac"]["drv"].write(frame, user=1))
    got, _user = await ports["mgmt"]["mon"].read_packet()
    assert got == frame, "a tuser=1 frame addressed to a known MAC must still route correctly"
