"""tests/dut_egress/test_dut_egress_e2e.py — a UDP echo, end to end, and read
back over the DUTEGR register interface.

THE PROPERTY THIS PLATFORM DID NOT HAVE
---------------------------------------
DUT *reception* has been silicon-proven since 2026-07-30: the shell's virtual
PHY drives the DUT's RMII and `DFXCTL.RM_STATUS[2]` was seen going 0 -> 1 under
gen_checker traffic on the fielded static. The RETURN half of Option C
(`docs/DUT_ETHERNET_EGRESS.md`) was DESIGNED and never BUILT — `shell_bd.tcl`
SECTION 5 tied `eth_bridge_3port`'s management egress to `mgmt_m_tready = 1`
with nothing behind it, so every frame the DUT transmitted was drained into a
constant. A DUT could be talked to and could not answer.

This bench closes that, through the whole composition and nothing mocked in the
middle — and since 2026-09-23 BOTH ENDS ARE REGISTERS (the inject side,
docs/planning/HANDOVER_DUT_INJECT.md §6):

    MicroBlaze (the bench) writing DUTEGR.TX_DATA, then TX_CTRL.COMMIT
                  -> dut_egress inject FIFO -> framer (pad + IEEE FCS)
                  -> inj_m_* -> bridge mgmt INGRESS (the §5 BD delta)
                  -> arbiter -> link_partner_mac TX
                  -> rmii_phy_if -> RMII partition pins
                  -> DUT MAC MODEL (the bench, playing the DUT: it checks the
                     FCS, then echoes the datagram back by swapping L2/L3/L4
                     src and dst)
                  -> RMII pins -> rmii_phy_if -> link_partner_mac RX
                  -> bridge (dst == MGMT_MAC, so it routes, not floods)
                  -> mgmt egress -> dut_egress capture FIFO
                  -> MicroBlaze (the bench) reading DUTEGR.DATA over AXI4-Lite

`tb_dut_egress.sv` wires the two RTL halves exactly as `shell_bd.tcl` SECTION 5
does — the bench elaborates the INTEGRATION, because integration is what has
gone wrong on this platform, not per-block RTL.

A NOTE ON THE ECHO, which is what makes "UDP" mean something here: the DUT model
swaps the Ethernet src/dst, the IPv4 src/dst and the UDP source/destination
ports. Because the Internet checksum is a one's-complement SUM, swapping src and
dst leaves BOTH the IPv4 header checksum and the UDP checksum bit-for-bit
unchanged — so the bench recomputes only the Ethernet FCS, and then VERIFIES
both Internet checksums on the frame that comes back out of the FIFO. A
datagram that survived the round trip with its checksums intact did not merely
have the right number of bytes.

CONTROLS — a green run above proves nothing unless these are seen to fail:
  +EGRESS_CONTROL=byteorder   compare the echo against its bytes REVERSED
  +EGRESS_CONTROL=nodrop      assert DROP_FULL == 0 after a deliberate overrun
  +INJECT_CONTROL=raw         (`make control-raw`) test_a COMMITs with RAW
                              FORCED TO 1: no FCS is appended, and the DUT MAC
                              model must fail the frame on its FCS check

Clocks: 50 MHz RMII/datapath, 100 MHz AXI-Lite — the real crossing.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly

from dut_presence import rtl_ready
from frames import build_frame, check_frame, eth_fcs
from rmii import RmiiFrameDriver, RmiiFrameMonitor
import dutegr_tx as tx
from regmap import (
    AxiLiteMaster,
    DUTEGR_STATUS, DUTEGR_LEVEL, DUTEGR_FRAME_LEN, DUTEGR_DATA,
    DUTEGR_RX_FRAMES, DUTEGR_DROP_FULL, DUTEGR_DROP_GIANT, DUTEGR_CTRL,
    DUTEGR_CTRL_EN, DUTEGR_CTRL_CLR_CNT,
    DUTEGR_STATUS_FRAME_RDY, DUTEGR_STATUS_OVF, DUTEGR_STATUS_DESYNC,
    DUTEGR_DATA_VALID, DUTEGR_DATA_LAST,
    DUTEGR_LEVEL_FRAMES_SHIFT, DUTEGR_FRAME_LEN_TOTAL_SHIFT,
    DUTEGR_DATA_DEPTH,
    GENCHK_CTRL, GENCHK_RX_CNT, GENCHK_ERR_CNT, GENCHK_CTRL_CHK_EN,
)

# --------------------------------------------------------------------------- #
# Readiness: this bench elaborates the whole §8 subsystem PLUS the capture
# block, so it gates on both (the belt-and-braces pattern of
# tests/eth_mac_subsystem and tests/link_partner_mac).
# --------------------------------------------------------------------------- #
_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_ETH = os.path.join(_ROOT, "fpga", "ethernet")
_IP = os.path.join(_ROOT, "fpga", "shell", "ip", "dut_egress")
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
    and rtl_ready(_IP, ["dut_egress.sv", "dutegr_cfifo.sv"])
)

# The bridge's compile-time forwarding table (eth_mac_test_subsystem.sv's
# BRIDGE_MGMT_MAC / BRIDGE_DUT_MAC defaults, which tb_dut_egress.sv leaves
# alone). A frame addressed to MGMT_MAC is ROUTED to the management egress,
# not flooded — which is the path this block sits on.
MGMT_MAC = bytes.fromhex("020000000001")
DUT_MAC = bytes.fromhex("020000000002")
PREAMBLE_SFD = b"\x55" * 7 + b"\xd5"

HOST_IP = bytes([192, 168, 10, 1])
DUT_IP = bytes([192, 168, 10, 42])
HOST_PORT = 0xC350        # 50000
DUT_PORT = 0x1F90         # 8080

# DUTEGR's base is irrelevant to this TB (one slave, no interconnect), but the
# block decodes its whole 64 KiB page, so driving BASE+offset here is free and
# keeps the bench honest about the address firmware actually uses.
BASE = 0


def _control(name="EGRESS_CONTROL"):
    v = cocotb.plusargs.get(name)
    if isinstance(v, (list, tuple)):
        v = v[-1]
    return None if v is None else str(v).strip()


# --------------------------------------------------------------------------- #
# IPv4 / UDP — built here rather than imported, because the POINT of the echo
# is that these two checksums are invariant under a src/dst swap.
# --------------------------------------------------------------------------- #
def _inet_csum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_udp_datagram(src_ip, dst_ip, sport, dport, payload: bytes) -> bytes:
    """One IPv4 packet carrying one UDP datagram, both checksums computed."""
    udp_len = 8 + len(payload)
    pseudo = src_ip + dst_ip + b"\x00\x11" + struct.pack(">H", udp_len)
    udp = struct.pack(">HHH", sport, dport, udp_len) + b"\x00\x00" + payload
    csum = _inet_csum(pseudo + udp)
    udp = udp[:6] + struct.pack(">H", csum) + payload

    total_len = 20 + udp_len
    ip = (b"\x45\x00" + struct.pack(">H", total_len)
          + b"\x00\x01\x00\x00\x40\x11" + b"\x00\x00" + src_ip + dst_ip)
    ip = ip[:10] + struct.pack(">H", _inet_csum(ip)) + ip[12:]
    return ip + udp


def check_udp_datagram(pkt: bytes):
    """(ip_csum_ok, udp_csum_ok, sport, dport, payload) for an IPv4/UDP packet.
    Verifying by RECOMPUTING over the received bytes: a one's-complement sum
    over a header whose own checksum field is present must come out 0."""
    ihl = (pkt[0] & 0x0F) * 4
    ip_ok = _inet_csum(pkt[:ihl]) == 0
    total_len = struct.unpack(">H", pkt[2:4])[0]
    src_ip, dst_ip = pkt[12:16], pkt[16:20]
    udp = pkt[ihl:total_len]
    sport, dport, udp_len = struct.unpack(">HHH", udp[:6])
    pseudo = src_ip + dst_ip + b"\x00\x11" + struct.pack(">H", udp_len)
    udp_ok = _inet_csum(pseudo + udp[:udp_len]) == 0
    return ip_ok, udp_ok, sport, dport, udp[8:udp_len]


def dut_mac_echo(frame: bytes) -> bytes:
    """THE DUT MAC MODEL. Takes the frame off the wire and echoes the datagram:
    Ethernet src<->dst, IPv4 src<->dst, UDP sport<->dport. Both Internet
    checksums are invariant under those swaps (the one's-complement sum is
    commutative), so only the Ethernet FCS is recomputed."""
    body = frame[:-4]
    dst, src, ethertype, rest = body[:6], body[6:12], body[12:14], body[14:]
    assert ethertype == b"\x08\x00", f"the model only echoes IPv4, got {ethertype.hex()}"

    ihl = (rest[0] & 0x0F) * 4
    ip = bytearray(rest[:ihl])
    ip[12:16], ip[16:20] = rest[16:20], rest[12:16]
    total_len = struct.unpack(">H", rest[2:4])[0]
    udp = bytearray(rest[ihl:total_len])
    udp[0:2], udp[2:4] = rest[ihl + 2:ihl + 4], rest[ihl:ihl + 2]
    tail = rest[total_len:]                      # any Ethernet pad, echoed as-is

    new_body = src + dst + ethertype + bytes(ip) + bytes(udp) + tail
    return new_body + struct.pack("<I", eth_fcs(new_body))


class _Env:
    __slots__ = ("axi", "genchk", "rmii_rx", "rmii_tx", "seen", "inj", "dutmac")


class _DutMacModel:
    """THE DUT MAC, receive half: every frame the shell puts on the RMII pins is
    recovered, its preamble/SFD checked, its FCS CHECKED (a real MAC drops a bad
    one — this model records it as a failure), and kept in arrival order.

    Runs for the whole test, so a frame that should NOT reach the DUT (a
    MGMT_MAC-addressed injection the bridge drops) is caught if it does."""

    def __init__(self, monitor):
        self.mon = monitor
        self.frames = []          # frames WITHOUT preamble/SFD, FCS included
        self.bad = []             # (frame, why) for anything a MAC would reject

    async def run(self):
        while True:
            on_wire = await self.mon.recv_frame(timeout_cycles=1 << 30)
            if on_wire is None:
                continue
            if on_wire[:8] != PREAMBLE_SFD:
                self.bad.append((on_wire, "no preamble/SFD"))
                self.frames.append(on_wire)
                continue
            frame = on_wire[8:]
            fcs_ok, _len_ok, why = check_frame(frame)
            if not fcs_ok:
                self.bad.append((frame, why))
            self.frames.append(frame)

    async def wait(self, dut, n, timeout_cycles=60000):
        for _ in range(timeout_cycles):
            if len(self.frames) >= n:
                return
            await RisingEdge(dut.refclk_i)
        raise AssertionError(
            f"only {len(self.frames)} of {n} frames reached the DUT's RMII pins")

    def assert_fcs_ok(self, i):
        frame = self.frames[i]
        fcs_ok, _l, why = check_frame(frame)
        assert fcs_ok, (
            f"DUT MAC MODEL: injected frame {i} arrived with a BAD FCS ({why}) — a "
            f"real MAC drops it. Wire bytes: {frame.hex()}")


class _MgmtEgressMonitor:
    """Counts and records the frames PRESENTED to the capture block.

    This is the honest denominator for the block's invariant. Frames can also
    be lost UPSTREAM of it — link_partner_mac's RX has no FIFO and the bridge
    head-of-line blocks by design — so asserting against "frames the DUT sent"
    would attribute somebody else's drop to this block.
    """

    def __init__(self, dut):
        self.dut = dut
        self.frames = []

    async def run(self):
        cur = bytearray()
        while True:
            await ReadOnly()
            beat = (int(self.dut.mgmt_m_tvalid.value)
                    and int(self.dut.mgmt_m_tready.value))
            if beat:
                cur.append(int(self.dut.mgmt_m_tdata.value))
                last = int(self.dut.mgmt_m_tlast.value)
            await RisingEdge(self.dut.refclk_i)
            if beat and last:
                self.frames.append(bytes(cur))
                cur = bytearray()


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.refclk_i, 20, unit="ns").start())      # 50 MHz
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, unit="ns").start())    # 100 MHz

    dut.rst_i.value = 1
    dut.s_axi_aresetn.value = 0
    dut.phy_rmii_txd.value = 0
    dut.phy_rmii_tx_en.value = 0
    dut.mgmt_s_tdata.value = 0
    dut.mgmt_s_tvalid.value = 0
    dut.mgmt_s_tlast.value = 0
    for sig in ("s_axi_awaddr", "s_axi_awvalid", "s_axi_wdata", "s_axi_wstrb",
                "s_axi_wvalid", "s_axi_bready", "s_axi_araddr", "s_axi_arvalid",
                "s_axi_rready", "g_axi_awaddr", "g_axi_awvalid", "g_axi_wdata",
                "g_axi_wstrb", "g_axi_wvalid", "g_axi_bready", "g_axi_araddr",
                "g_axi_arvalid", "g_axi_rready"):
        getattr(dut, sig).value = 0

    for _ in range(20):
        await RisingEdge(dut.refclk_i)
    dut.rst_i.value = 0
    dut.s_axi_aresetn.value = 1
    for _ in range(20):
        await RisingEdge(dut.refclk_i)

    env = _Env()
    env.axi = AxiLiteMaster.from_dut(dut)
    env.genchk = AxiLiteMaster(
        clk=dut.refclk_i,
        awaddr=dut.g_axi_awaddr, awvalid=dut.g_axi_awvalid, awready=dut.g_axi_awready,
        wdata=dut.g_axi_wdata, wstrb=dut.g_axi_wstrb, wvalid=dut.g_axi_wvalid,
        wready=dut.g_axi_wready, bresp=dut.g_axi_bresp, bvalid=dut.g_axi_bvalid,
        bready=dut.g_axi_bready, araddr=dut.g_axi_araddr, arvalid=dut.g_axi_arvalid,
        arready=dut.g_axi_arready, rdata=dut.g_axi_rdata, rresp=dut.g_axi_rresp,
        rvalid=dut.g_axi_rvalid, rready=dut.g_axi_rready)
    env.rmii_rx = RmiiFrameMonitor(dut.refclk_i, dut.phy_rmii_crs_dv,
                                   dut.phy_rmii_rxd, 2)
    env.rmii_tx = RmiiFrameDriver(dut.refclk_i, dut.phy_rmii_tx_en,
                                  dut.phy_rmii_txd, 2)
    env.seen = _MgmtEgressMonitor(dut)
    cocotb.start_soon(env.seen.run())
    # The inject side: the monitor on dut_egress_0/inj_m_* (== the bridge's
    # mgmt_s_*, the §5 delta), and the DUT MAC's receive half.
    env.inj = tx.InjectMonitor.on(dut, dut.refclk_i).start()
    env.dutmac = _DutMacModel(env.rmii_rx)
    cocotb.start_soon(env.dutmac.run())
    return env


def _frame_to(dst: bytes, src: bytes, ethertype: int, payload: bytes) -> bytes:
    """What SOFTWARE stages for a RAW = 0 injection: header + payload, no pad, no
    FCS (the hardware adds both)."""
    return dst + src + struct.pack(">H", ethertype) + payload


async def _inject(env, staged: bytes, raw: bool = False):
    await tx.send(env.axi, BASE, staged, raw=raw)


async def _tx_quiet(dut, env, commits, what):
    await tx.wait_empty(dut.s_axi_aclk, env.axi, BASE)
    for _ in range(16):
        await RisingEdge(dut.refclk_i)
    await tx.assert_invariant(env.axi, BASE, commits, env.inj, what)
    st = await _rd(env.axi, tx.TX_STATUS)
    assert not (st & tx.TX_STATUS_DESYNC), f"TX DESYNC latched ({what})"
    env.inj.assert_clean(what)


# --------------------------------------------------------------------------- #
# Register helpers (the MicroBlaze's view)
# --------------------------------------------------------------------------- #
async def _rd(axi, off):
    data, resp = await axi.read(BASE + off)
    assert resp == 0, f"RRESP={resp} reading DUTEGR offset {off:#x}"
    return data


async def _counters(axi):
    return (await _rd(axi, DUTEGR_RX_FRAMES),
            await _rd(axi, DUTEGR_DROP_FULL),
            await _rd(axi, DUTEGR_DROP_GIANT))


async def _wait_frame_ready(dut, axi, timeout_cycles: int = 40000):
    for _ in range(timeout_cycles // 20):
        st = await _rd(axi, DUTEGR_STATUS)
        if st & DUTEGR_STATUS_FRAME_RDY:
            return st
        for _ in range(20):
            await RisingEdge(dut.s_axi_aclk)
    raise AssertionError(
        "no frame ever became readable at DUTEGR — the DUT's reply did not "
        "reach the capture FIFO")


async def _read_frame(axi):
    fl = await _rd(axi, DUTEGR_FRAME_LEN)
    total = (fl >> DUTEGR_FRAME_LEN_TOTAL_SHIFT) & 0xFFFF
    out = bytearray()
    lasts = []
    for i in range(total):
        d = await _rd(axi, DUTEGR_DATA)
        assert d & DUTEGR_DATA_VALID, f"DATA.VALID dropped at byte {i}/{total}"
        out.append(d & 0xFF)
        lasts.append(bool(d & DUTEGR_DATA_LAST))
    assert lasts and lasts[-1] and not any(lasts[:-1]), (
        "LAST must mark the final byte and only the final byte")
    return bytes(out)


# =========================================================================== #
# (a) THE HEADLINE — the whole loop, entirely over registers
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_a_udp_echo_end_to_end(dut):
    """What this proves (handover §6, BLOCK=e2e): the host can TALK to the DUT
    and the DUT can ANSWER, with registers at both ends and nothing mocked in
    between. A UDP datagram written to DUTEGR.TX_DATA and COMMITted (RAW = 0)
    leaves through the inject port, crosses the bridge and the link-partner
    MAC, and reaches the DUT's RMII pins as preamble + SFD + the frame with a
    CORRECT FCS appended by the hardware; the DUT MAC model checks that FCS and
    echoes the datagram; the echo is recovered by the virtual PHY, routed to the
    bridge's management egress, captured by DUTEGR, and read back out of
    DUTEGR.DATA byte-identical, with both Internet checksums re-verified on the
    bytes that came out.

    CONTROLS: `+EGRESS_CONTROL=byteorder` compares the echo REVERSED;
    `+INJECT_CONTROL=raw` (`make control-raw`) COMMITs with RAW forced to 1, so
    no FCS is appended and the DUT MAC model fails the frame."""
    env = await _bring_up(dut)
    raw = _control("INJECT_CONTROL") == "raw"
    if raw:
        dut._log.info("CONTROL raw: COMMIT with RAW forced to 1 — no FCS appended; "
                      "the DUT MAC model must flag a BAD FCS and this run must FAIL")

    payload = bytes(range(0x40, 0x60))          # 32 B: no Ethernet padding needed
    datagram = build_udp_datagram(HOST_IP, DUT_IP, HOST_PORT, DUT_PORT, payload)
    staged = _frame_to(DUT_MAC, MGMT_MAC, 0x0800, datagram)
    request = build_frame(DUT_MAC, MGMT_MAC, 0x0800, datagram)   # what must reach the DUT

    # host -> DUT, over registers
    await _inject(env, staged, raw=raw)
    await env.dutmac.wait(dut, 1)
    env.dutmac.assert_fcs_ok(0)                  # the DUT MAC's own verdict first
    on_wire = env.dutmac.frames[0]
    assert on_wire == request, (
        f"the request must reach the DUT's RMII as frames.build_frame() of what "
        f"was staged (FCS appended by the hardware):\n"
        f"  expected {request.hex()}\n  got      {on_wire.hex()}")
    assert env.inj.frames == [request], "the inject port did not carry exactly the request"

    # the DUT answers
    echo = dut_mac_echo(request)
    await env.rmii_tx.send_frame(PREAMBLE_SFD + echo)

    # the MicroBlaze reads it back
    await _wait_frame_ready(dut, env.axi)
    got = await _read_frame(env.axi)

    expect = echo
    if _control() == "byteorder":
        expect = bytes(reversed(echo))
        dut._log.info("CONTROL byteorder: expecting the echo REVERSED — this run must FAIL")
    assert got == expect, (
        f"the echo must come back byte-for-byte:\n  expected {expect.hex()}\n"
        f"  got      {got.hex()}")

    # ...and it is still a valid UDP datagram addressed back to the host.
    assert got[:6] == MGMT_MAC and got[6:12] == DUT_MAC, (
        f"the echo's L2 addresses are wrong: dst={got[:6].hex()} src={got[6:12].hex()}")
    ip_ok, udp_ok, sport, dport, rx_payload = check_udp_datagram(got[14:-4])
    assert ip_ok, "the IPv4 header checksum does not verify on the returned frame"
    assert udp_ok, "the UDP checksum does not verify on the returned frame"
    assert (sport, dport) == (DUT_PORT, HOST_PORT), (
        f"ports not swapped by the echo: {sport}->{dport}")
    assert rx_payload == payload, (
        f"payload corrupted: sent {payload.hex()}, got {rx_payload.hex()}")

    rx, full, giant = await _counters(env.axi)
    assert (rx, full, giant) == (1, 0, 0), f"RX={rx} FULL={full} GIANT={giant}"
    st = await _rd(env.axi, DUTEGR_STATUS)
    assert not (st & DUTEGR_STATUS_DESYNC)
    assert not (st & DUTEGR_STATUS_OVF)
    await _tx_quiet(dut, env, commits=1, what="UDP echo")
    assert await tx.counters(env.axi, BASE) == (1, 0, 0, 0)
    assert not env.dutmac.bad


@cocotb.test(skip=NO_RTL)
async def test_b_several_echoes_queue_in_order(dut):
    """What this proves: the loop is not a one-shot. Three datagrams of
    different lengths — one SHORT enough that the hardware must pad it to 60
    bytes — are injected over registers and echoed back to back; they queue in
    the capture FIFO and all three come back in arrival order, each with its own
    length, its own payload and both checksums intact. The padded one proves
    the pad bytes are covered by the appended FCS (the DUT model checks it) and
    survive the round trip."""
    env = await _bring_up(dut)

    sent = []
    for i, n in enumerate((32, 48, 4)):
        payload = bytes(((i * 53 + k * 17) & 0xFF) for k in range(n))
        datagram = build_udp_datagram(HOST_IP, DUT_IP, HOST_PORT, DUT_PORT + i, payload)
        await _inject(env, _frame_to(DUT_MAC, MGMT_MAC, 0x0800, datagram))
        request = build_frame(DUT_MAC, MGMT_MAC, 0x0800, datagram)
        await env.dutmac.wait(dut, i + 1)
        env.dutmac.assert_fcs_ok(i)
        assert env.dutmac.frames[i] == request, (
            f"request {i} ({n} B payload) wrong on the wire:\n"
            f"  {env.dutmac.frames[i].hex()}\n  {request.hex()}")
        echo = dut_mac_echo(request)
        await env.rmii_tx.send_frame(PREAMBLE_SFD + echo)
        sent.append((payload, echo))
    assert len(env.dutmac.frames[2]) == 64, "the 4-byte-payload request was not padded to 60+4"

    for _ in range(2000):
        await RisingEdge(dut.s_axi_aclk)

    lvl = await _rd(env.axi, DUTEGR_LEVEL)
    assert (lvl >> DUTEGR_LEVEL_FRAMES_SHIFT) == len(sent), (
        f"LEVEL says {lvl >> DUTEGR_LEVEL_FRAMES_SHIFT} frames waiting, sent {len(sent)}")

    for i, (payload, echo) in enumerate(sent):
        await _wait_frame_ready(dut, env.axi)
        got = await _read_frame(env.axi)
        assert got == echo, f"echo {i} came back wrong:\n  {got.hex()}\n  {echo.hex()}"
        ip_ok, udp_ok, _s, _d, rx_payload = check_udp_datagram(got[14:-4])
        assert ip_ok and udp_ok, f"echo {i}: checksums do not verify on the returned frame"
        assert rx_payload == payload, f"echo {i} payload corrupted"

    rx, full, giant = await _counters(env.axi)
    assert (rx, full, giant) == (len(sent), 0, 0)
    await _tx_quiet(dut, env, commits=len(sent), what="three echoes")
    assert not env.dutmac.bad


# =========================================================================== #
# (c) overflow through the REAL path — counted, not silent
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_c_overflow_is_visible_in_a_counter(dut):
    """What this proves, through the whole datapath rather than at the block's
    port: a DUT that transmits more than the FIFO can hold while nobody drains
    it loses frames — and every lost frame is COUNTED. A silently dropping FIFO
    would be worse than none, and this is the check that says it is not one.

    The denominator is the bench's monitor on the capture port, NOT the number
    of frames the DUT sent: link_partner_mac's RX has no FIFO and the bridge
    head-of-line blocks, so upstream loss is somebody else's and must not be
    charged to this block.

    CONTROL `+EGRESS_CONTROL=nodrop` asserts DROP_FULL == 0 instead."""
    env = await _bring_up(dut)
    control = _control() == "nodrop"

    n_frames = 5
    big = 1024                                   # 5 x 1024 B >> DATA_DEPTH
    for i in range(n_frames):
        payload = bytes(((i * 7 + k) & 0xFF) for k in range(big - 14 - 4))
        frame = build_frame(MGMT_MAC, DUT_MAC, 0x0801, payload, pad_to_min=False)
        await env.rmii_tx.send_frame(PREAMBLE_SFD + frame)

    for _ in range(4000):
        await RisingEdge(dut.s_axi_aclk)

    presented = len(env.seen.frames)
    assert presented >= 3, (
        f"only {presented} frames reached the capture port; the overflow "
        f"scenario needs more than the {DUTEGR_DATA_DEPTH} B FIFO can hold")

    rx, full, giant = await _counters(env.axi)
    if control:
        dut._log.info("CONTROL nodrop: asserting DROP_FULL == 0 — this run must FAIL")
        assert full == 0, (
            f"CONTROL: DROP_FULL is {full}. The drop counter IS real, which is "
            f"what this control exists to demonstrate.")
    assert full > 0, (
        f"{presented} x ~{big} B presented into a {DUTEGR_DATA_DEPTH} B FIFO with "
        f"nobody reading must drop, and the drop must be COUNTED: "
        f"RX={rx} DROP_FULL={full}")
    assert rx + full + giant == presented, (
        f"every frame presented must be accounted for exactly once: "
        f"RX={rx} + FULL={full} + GIANT={giant} != {presented}")
    st = await _rd(env.axi, DUTEGR_STATUS)
    assert st & DUTEGR_STATUS_OVF, "STATUS.OVF must latch on any drop"

    # And nothing torn is readable: what survived is whole, and in order.
    for i in range(rx):
        await _wait_frame_ready(dut, env.axi)
        got = await _read_frame(env.axi)
        assert got == env.seen.frames[i], (
            f"surviving frame {i} is not byte-identical to the frame presented "
            f"at the capture port — a dropped frame must be dropped WHOLE")
    assert not ((await _rd(env.axi, DUTEGR_STATUS)) & DUTEGR_STATUS_DESYNC)


# =========================================================================== #
# (d) the two halves of DoD §5.2 item 4, in ONE run
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_d_scored_and_egressed_agree(dut):
    """What this proves: the on-chip SCORING of the DUT's transmitted frames
    (GENCHK, "scored by macgen") and their EGRESS through this block are talking
    about the same frames. `docs/OPTION_C_EGRESS_STATUS.md` §5 step 2 asked for
    exactly this — the two were separately proven and never bound together in
    one scenario.

    GENCHK counts at the DUT-TX tap, upstream of the bridge; this block counts
    what reached the bridge's management egress. On clean, spaced traffic with
    room in the FIFO they must agree exactly, and GENCHK must score no errors."""
    env = await _bring_up(dut)
    await env.genchk.write(GENCHK_CTRL, GENCHK_CTRL_CHK_EN)

    n = 3
    frames = []
    for i in range(n):
        payload = bytes(((i * 11 + k * 3) & 0xFF) for k in range(46))
        frames.append(build_frame(MGMT_MAC, DUT_MAC, 0x0800, payload))
    for f in frames:
        await env.rmii_tx.send_frame(PREAMBLE_SFD + f)
        for _ in range(200):
            await RisingEdge(dut.refclk_i)

    for _ in range(2000):
        await RisingEdge(dut.s_axi_aclk)

    chk_rx, _ = await env.genchk.read(GENCHK_RX_CNT)
    chk_err, _ = await env.genchk.read(GENCHK_ERR_CNT)
    rx, full, giant = await _counters(env.axi)

    assert chk_err == 0, f"GENCHK scored {chk_err} errors on clean DUT traffic"
    assert chk_rx == n, f"GENCHK scored {chk_rx} DUT frames, {n} were transmitted"
    assert (rx, full, giant) == (n, 0, 0), (
        f"the frames GENCHK scored ({chk_rx}) and the frames that egressed "
        f"(RX={rx}, dropped {full}+{giant}) must be the same {n} frames")

    for f in frames:
        await _wait_frame_ready(dut, env.axi)
        assert await _read_frame(env.axi) == f

    # A clean read-out leaves the block ready for the next RM.
    await env.axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN | DUTEGR_CTRL_CLR_CNT)
    assert await _counters(env.axi) == (0, 0, 0)


# =========================================================================== #
# (e) routing reality — handover §4 rule 4, through the real bridge
# =========================================================================== #
class _UplinkMonitor:
    """Frames the bridge floods to port A (the uplink, drained: tready = 1)."""

    def __init__(self, dut):
        self.dut = dut
        self.frames = []

    async def run(self):
        cur = bytearray()
        while True:
            await RisingEdge(self.dut.refclk_i)
            await ReadOnly()
            if int(self.dut.uplink_m_tvalid.value):          # tready is a constant 1
                cur.append(int(self.dut.uplink_m_tdata.value))
                if int(self.dut.uplink_m_tlast.value):
                    self.frames.append(bytes(cur))
                    cur = bytearray()


@cocotb.test(skip=NO_RTL)
async def test_e_routing_reality(dut):
    """What this proves: the fixed, learning-free forwarding table does what the
    handover says to an injected frame, and TX_FRAMES counts what DUTEGR handed
    the bridge — not what the bridge did with it:

      * the DUT's REAL MAC (not in the table) misses and FLOODS: it reaches the
        DUT's RMII pins AND the tied-off uplink (drained);
      * broadcast floods the same way;
      * the table's DUT_MAC routes to the DUT only;
      * MGMT_MAC resolves only to its own ingress port, so the bridge DROPS it
        silently — it reaches neither the DUT nor the uplink nor DUTEGR's own
        capture side — and TX_FRAMES still counts it (expected, not a bug)."""
    env = await _bring_up(dut)
    up = _UplinkMonitor(dut)
    cocotb.start_soon(up.run())

    real_dut_mac = bytes.fromhex("02123456789a")
    bcast = b"\xff" * 6
    body = bytes(range(46))
    cases = [(real_dut_mac, "flood"), (bcast, "flood"), (DUT_MAC, "route"),
             (MGMT_MAC, "drop")]
    want_dut, want_up = [], []
    for dst, fate in cases:
        staged = _frame_to(dst, MGMT_MAC, 0x88B5, body)
        await _inject(env, staged)
        wire = tx.expected_on_port(staged, raw=False)
        if fate in ("flood", "route"):
            want_dut.append(wire)
        if fate == "flood":
            want_up.append(wire)
        for _ in range(3000):
            await RisingEdge(dut.refclk_i)

    assert env.dutmac.frames == want_dut, (
        f"frames at the DUT's RMII pins: got {len(env.dutmac.frames)}, expected "
        f"{len(want_dut)} (real-MAC flood, broadcast flood, table route)")
    assert up.frames == want_up, (
        f"frames flooded to the uplink: got {len(up.frames)}, expected "
        f"{len(want_up)} (the two floods only)")
    assert await _counters(env.axi) == (0, 0, 0), (
        "the MGMT_MAC frame came back into DUTEGR's own capture side")
    await _tx_quiet(dut, env, commits=len(cases), what="routing")
    assert await tx.counters(env.axi, BASE) == (len(cases), 0, 0, 0), (
        "TX_FRAMES must count all four, the silently-dropped MGMT_MAC one included")
    assert not env.dutmac.bad


# =========================================================================== #
# (f) RAW = 1 reaches the wire verbatim — the fault-injection mode, end to end
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_f_raw1_fault_injection_reaches_the_wire_verbatim(dut):
    """What this proves: RAW = 1 is what makes the inject side a FAULT injector.
    A frame carrying a deliberately wrong FCS, and a runt with a correct one,
    reach the DUT's RMII pins byte for byte as written — and the DUT MAC model
    judges them exactly as a real MAC would (bad FCS; runt). A RAW = 0 frame
    after them is clean again, so the mode is per-commit."""
    env = await _bring_up(dut)
    bad_fcs = build_frame(DUT_MAC, MGMT_MAC, 0x0800, bytes(range(50)), bad_fcs=True)
    runt = build_frame(DUT_MAC, MGMT_MAC, 0x0800, bytes(range(10)), pad_to_min=False)
    await _inject(env, bad_fcs, raw=True)
    await _inject(env, runt, raw=True)
    normal = _frame_to(DUT_MAC, MGMT_MAC, 0x0800, bytes(range(20)))
    await _inject(env, normal, raw=False)
    await env.dutmac.wait(dut, 3)

    assert env.dutmac.frames[0] == bad_fcs, "the bad-FCS frame was altered on its way"
    assert env.dutmac.frames[1] == runt, "the runt was altered (padded?) on its way"
    assert env.dutmac.frames[2] == tx.expected_on_port(normal, raw=False)
    fcs0, _l0, why0 = check_frame(env.dutmac.frames[0])
    fcs1, len1, why1 = check_frame(env.dutmac.frames[1])
    assert not fcs0, f"the DUT model must see the injected bad FCS, it saw: {why0}"
    assert fcs1 and not len1, f"the DUT model must see a good-FCS runt, it saw: {why1}"
    env.dutmac.assert_fcs_ok(2)
    await _tx_quiet(dut, env, commits=3, what="RAW=1 fault injection")


# =========================================================================== #
# (g) both directions at once
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_g_inject_and_capture_at_the_same_time(dut):
    """What this proves: the inject path and the return path share one bridge
    and one register page, and run at the same time without disturbing each
    other — the bridged-Linux case (handover §7). The DUT transmits six frames
    to MGMT_MAC while the host injects six datagrams to the DUT; every injected
    frame reaches the DUT's pins with a good FCS, in order, and every DUT frame
    is read back out of DUTEGR.DATA byte-exact.

    Spaced, not line-rate: the bridge head-of-line blocks by design and
    link_partner_mac's RX has no FIFO, so back-to-back full-duplex traffic loses
    DUT frames UPSTREAM of this block (tests/dut_egress test_c's note). This
    test is about correctness under concurrency, not throughput."""
    env = await _bring_up(dut)
    n = 6
    dut_frames = [build_frame(MGMT_MAC, DUT_MAC, 0x0800,
                              bytes(((i * 29 + k) & 0xFF) for k in range(100)))
                  for i in range(n)]
    requests = []

    async def dut_talks():
        for f in dut_frames:
            await env.rmii_tx.send_frame(PREAMBLE_SFD + f)
            for _ in range(1500):
                await RisingEdge(dut.refclk_i)

    talker = cocotb.start_soon(dut_talks())
    for i in range(n):
        payload = bytes(((i * 13 + k * 7) & 0xFF) for k in range(40))
        datagram = build_udp_datagram(HOST_IP, DUT_IP, HOST_PORT, DUT_PORT + i, payload)
        await _inject(env, _frame_to(DUT_MAC, MGMT_MAC, 0x0800, datagram))
        requests.append(build_frame(DUT_MAC, MGMT_MAC, 0x0800, datagram))
        for _ in range(700):
            await RisingEdge(dut.refclk_i)
    await talker
    await env.dutmac.wait(dut, n)
    for _ in range(3000):
        await RisingEdge(dut.s_axi_aclk)

    assert env.dutmac.frames == requests, "the injected frames did not all reach the DUT intact, in order"
    assert not env.dutmac.bad, f"the DUT model rejected {len(env.dutmac.bad)} injected frame(s)"
    assert env.seen.frames == dut_frames, (
        f"{len(env.seen.frames)} of {n} DUT frames reached the capture port; the "
        f"spacing here is meant to rule out upstream loss")
    for i, f in enumerate(dut_frames):
        await _wait_frame_ready(dut, env.axi)
        assert await _read_frame(env.axi) == f, f"DUT frame {i} came back wrong"
    assert await _counters(env.axi) == (n, 0, 0)
    await _tx_quiet(dut, env, commits=n, what="full duplex")
