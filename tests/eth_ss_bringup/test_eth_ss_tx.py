"""tests/eth_ss_bringup/test_eth_ss_tx.py — ARM=tx: rm_eth_ss TRANSMITS.

THE PROPERTY
------------
Until 2026-09-23 rm_eth_ss could not send a frame by construction: it has no
CPU, its bring-up FSM set TX_BD_NUM=1 and never readied BD0, and the MAC's
frame buffer (the RM-internal DMA SRAM) sits on a bus that FSM cannot reach.
So the shell's DUT->host return path (DUTEGR) has never carried a frame on
silicon — there was nothing to carry.

The FSM now arms the MAC's own 802.3x PAUSE-frame generator (CTRLMODER.TXFLOW)
and writes TXCTRL = TXPAUSERQ | seq once after bring-up and then every
REARM_CYCLES. The MAC builds each frame from registers alone. This bench runs
the UNMODIFIED RM (rp_eth_ss_wrapper: FSM + real OpenCores MAC + rmii_to_mii)
into the shell's virtual PHY, bridge and DUTEGR FIFO (tb_dut_egress.sv, the
composition shell_bd.tcl SECTION 5 builds) and reads the frames back over
DUTEGR's AXI-Lite surface, byte for byte, FCS included:

    dst 01:80:C2:00:00:01  src 32:53:45:4C:53:02  type 0x8808
    opcode 0x0001  pause_time = seq (1, 2, 3 ...)  42 x 0x00  FCS    (64 B)

CONTROL (must be SEEN TO FAIL): the same bench against the pre-2026-09-23 FSM
    make ARM=tx BRINGUP_SV=<old eth_ss_bringup.sv>
reads no frame at DUTEGR and fails.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from frames import eth_fcs
from regmap import (
    AxiLiteMaster,
    DUTEGR_STATUS, DUTEGR_FRAME_LEN, DUTEGR_DATA, DUTEGR_RX_FRAMES,
    DUTEGR_DROP_FULL, DUTEGR_DROP_GIANT,
    DUTEGR_STATUS_FRAME_RDY, DUTEGR_STATUS_DESYNC,
    DUTEGR_DATA_VALID, DUTEGR_DATA_LAST, DUTEGR_FRAME_LEN_TOTAL_SHIFT,
)

_ETH_SS_HOME = os.environ.get("ETH_SS_HOME", "")
NO_RTL = not (_ETH_SS_HOME and os.path.isdir(_ETH_SS_HOME))

PAUSE_DST = bytes.fromhex("0180C2000001")
# Station address as the MAC puts it on the wire: {MAC_ADDR1[15:0], MAC_ADDR0}
# MSB first (eth_transmitcontrol.v: MAC[47:40] first). Wrapper defaults
# MAC_ADDR1=0x3253, MAC_ADDR0=0x454C5302.
DUT_SRC = bytes.fromhex("3253454C5302")
REARM_CYCLES = 4000          # tb_eth_ss_tx.sv defparam
N_FRAMES = 3


def pause_frame(seq: int) -> bytes:
    body = PAUSE_DST + DUT_SRC + b"\x88\x08" + b"\x00\x01" + struct.pack(">H", seq & 0xFFFF)
    body += b"\x00" * (60 - len(body))
    return body + struct.pack("<I", eth_fcs(body))


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.refclk_i, 20, unit="ns").start())      # 50 MHz RMII
    await Timer(7, unit="ns")                                            # skew the DUT clock
    cocotb.start_soon(Clock(dut.dut_clk, 20, unit="ns").start())       # 50 MHz dut_clk
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, unit="ns").start())    # 100 MHz AXI
    dut.rst_i.value = 1
    dut.dut_resetn.value = 0
    dut.s_axi_aresetn.value = 0
    for sig in ("s_axi_awaddr", "s_axi_awvalid", "s_axi_wdata", "s_axi_wstrb",
                "s_axi_wvalid", "s_axi_bready", "s_axi_araddr", "s_axi_arvalid",
                "s_axi_rready"):
        getattr(dut, sig).value = 0
    for _ in range(20):
        await RisingEdge(dut.refclk_i)
    dut.rst_i.value = 0
    dut.s_axi_aresetn.value = 1
    dut.dut_resetn.value = 1
    return AxiLiteMaster.from_dut(dut)


async def _rd(axi, off):
    data, resp = await axi.read(off)
    assert resp == 0, f"RRESP={resp} reading DUTEGR offset {off:#x}"
    return data


async def _read_frame(axi):
    fl = await _rd(axi, DUTEGR_FRAME_LEN)
    total = (fl >> DUTEGR_FRAME_LEN_TOTAL_SHIFT) & 0xFFFF
    out, lasts = bytearray(), []
    for i in range(total):
        d = await _rd(axi, DUTEGR_DATA)
        assert d & DUTEGR_DATA_VALID, f"DATA.VALID dropped at byte {i}/{total}"
        out.append(d & 0xFF)
        lasts.append(bool(d & DUTEGR_DATA_LAST))
    assert lasts and lasts[-1] and not any(lasts[:-1]), "LAST must mark only the final byte"
    return bytes(out)


class _TxPinMonitor:
    """Counts TX_EN rising edges on the RM's RMII TX partition pins."""

    def __init__(self, dut):
        self.dut, self.bursts = dut, 0

    async def run(self):
        prev = 0
        while True:
            await RisingEdge(self.dut.refclk_i)
            await ReadOnly()
            cur = int(self.dut.phy_rmii_tx_en.value)
            if cur and not prev:
                self.bursts += 1
            prev = cur


@cocotb.test(skip=NO_RTL)
async def test_rm_eth_ss_transmits_pause_beacon_to_dutegr(dut):
    """What this proves: with no CPU and nothing driving the RM but its own
    bring-up FSM, rm_eth_ss puts a frame on its RMII TX pins, the shell's
    virtual PHY recovers it, the bridge floods it to its management port and
    DUTEGR captures it — N_FRAMES times, each byte for byte (valid FCS
    included), with the pause-time field counting 1, 2, 3 and RX_FRAMES
    counting with it, nothing dropped."""
    axi = await _bring_up(dut)
    mon = _TxPinMonitor(dut)
    cocotb.start_soon(mon.run())

    # Budget: bring-up + N periods + slack, polled in 2 us steps.
    budget_ns = 20_000 + (N_FRAMES + 1) * REARM_CYCLES * 20
    got = []
    t = 0
    while len(got) < N_FRAMES and t < budget_ns:
        st = await _rd(axi, DUTEGR_STATUS)
        if st & DUTEGR_STATUS_FRAME_RDY:
            got.append(await _read_frame(axi))
            dut._log.info(f"DUTEGR frame {len(got)}: {len(got[-1])} B {got[-1].hex()}")
        else:
            await Timer(2000, unit="ns")
            t += 2000

    rx, full, giant = (await _rd(axi, DUTEGR_RX_FRAMES),
                       await _rd(axi, DUTEGR_DROP_FULL),
                       await _rd(axi, DUTEGR_DROP_GIANT))
    dut._log.info(f"RMII TX bursts={mon.bursts} DUTEGR RX_FRAMES={rx} "
                  f"DROP_FULL={full} DROP_GIANT={giant}")
    assert len(got) == N_FRAMES, (
        f"expected {N_FRAMES} frames at DUTEGR within {budget_ns} ns, read {len(got)} "
        f"(RMII TX bursts seen: {mon.bursts}, RX_FRAMES={rx})")
    for i, frame in enumerate(got):
        exp = pause_frame(i + 1)
        assert frame == exp, (
            f"frame {i} is not the expected PAUSE beacon seq={i + 1}:\n"
            f"  expected {exp.hex()}\n  got      {frame.hex()}")
    assert rx >= N_FRAMES and full == 0 and giant == 0, (
        f"RX_FRAMES={rx} DROP_FULL={full} DROP_GIANT={giant}")
    st = await _rd(axi, DUTEGR_STATUS)
    assert not (st & DUTEGR_STATUS_DESYNC), "DUTEGR desync"
