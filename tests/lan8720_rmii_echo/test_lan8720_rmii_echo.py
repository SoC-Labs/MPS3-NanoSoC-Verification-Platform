"""test_lan8720_rmii_echo.py -- cocotb bench: the DUT MAC's RMII path against a
REAL LAN8720 behavioral model (lan8720_model.sv), NOT the repo's mdio_phy_model.

Proves board-free the property the SHELL_REALPHY shell variant needs: a frame
driven at the RMII pads is RECEIVED and ECHOED through rmii_to_mii -> MAC -> DMA.

DUT = ethmac_ahb_rmii (the same real OpenCores MAC + rmii_to_mii the rm_eth_ss RM
carries). cocotb is the "firmware": it programs the MAC over AHB
(tests/common/ahb_lite.py), preloads/reads frames in the bench DMA RAM, injects
di-bits at the RMII pads, and puts the LAN8720 model in near-end loopback for the
round trip.

Tests (default run, mode_speed=1 => 100 Mbps):
  1. reset + MODER default            -- AHB path sane
  2. MDIO PHYID via the MAC's eth_miim -- the model's Clause-22 framing is
                                          eth_miim-correct (real LAN8720 IDs)
  3. external RX (ARP frame at the pads) -> MAC RX -> DMA  (RECEIVED)
  4. PHY loopback: MAC TX -> PHY -> MAC RX -> DMA          (ECHOED round trip)
  5. mode_speed is WIRED to the MAC (guard); +MODE_SPEED=0 run inverts test 3
     to assert RX does NOT complete (mode_speed is load-bearing).

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
import os
import sys
import zlib
import struct

import cocotb
from cocotb.triggers import RisingEdge, FallingEdge, Timer, ClockCycles

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from ahb_lite import AhbLiteMaster  # noqa: E402

# ── OpenCores ethmac register map (byte offsets) ────────────────────────────
MODER, INT_SOURCE, INT_MASK = 0x00, 0x04, 0x08
IPGT, IPGR1, IPGR2, PACKETLEN = 0x0C, 0x10, 0x14, 0x18
TX_BD_NUM = 0x20
MIIMODER, MIICOMMAND, MIIADDRESS = 0x28, 0x2C, 0x30
MIITX_DATA, MIIRX_DATA, MIISTATUS = 0x34, 0x38, 0x3C
MAC_ADDR0, MAC_ADDR1 = 0x40, 0x44
BD_BASE = 0x400

MODER_RXEN, MODER_TXEN, MODER_PRO = 0x1, 0x2, 0x20
MODER_FULLD, MODER_CRCEN, MODER_PAD = 0x400, 0x2000, 0x8000
MODER_DEFAULT = 0xA000                       # = PAD|CRCEN at reset
MODER_TXRX = 0xA423                          # PAD|CRCEN|FULLD|PRO|TXEN|RXEN

TXF_RD, TXF_IRQ, TXF_WR, TXF_PAD, TXF_CRC = 0x8000, 0x4000, 0x2000, 0x1000, 0x0800
RXF_E, RXF_IRQ, RXF_WR = 0x8000, 0x4000, 0x2000

# MIICOMMAND bits: [0]=SCANSTAT [1]=RSTAT(read) [2]=WCTRLDATA(write)
MIICMD_RSTAT = 0x2
PHY_ADDR = 1

TX_BUF, RX_BUF = 0x0000, 0x0800          # in the bench DMA RAM


def _plusarg_int(name, default):
    v = cocotb.plusargs.get(name)
    if v is None:
        return default
    if isinstance(v, (list, tuple)):
        v = v[-1]
    try:
        return int(str(v).strip(), 0)
    except ValueError:
        return default


def eth_fcs(body: bytes) -> bytes:
    """Ethernet FCS: zlib.crc32 (reflected CRC-32) appended LSB-byte-first,
    byte-identical to tb_lan8720_dp.sv's eth_crc32 (self-tested there)."""
    return struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def build_eth(dst: bytes, src: bytes, ethertype: int, payload: bytes) -> bytes:
    return dst + src + struct.pack(">H", ethertype) + payload


def build_arp_request(sha: bytes, spa: str, tpa: str) -> bytes:
    def ip(s):
        return bytes(int(x) for x in s.split("."))
    arp = (struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1) +
           sha + ip(spa) + b"\x00\x00\x00\x00\x00\x00" + ip(tpa))
    return build_eth(b"\xff" * 6, sha, 0x0806, arp)


# ── DMA-RAM hierarchical access (big-endian byte layout, per tb_lan8720_dp) ──
def ram_word(dut, w):
    return int(dut.ram[w].value)


def ram_set_word(dut, w, val):
    dut.ram[w].value = val & 0xFFFFFFFF


def ram_put_bytes(dut, byte_addr, data: bytes):
    words = {}
    for i, b in enumerate(data):
        a = byte_addr + i
        w = a >> 2
        sh = 24 - (a & 3) * 8
        words.setdefault(w, ram_word(dut, w))
        words[w] = (words[w] & ~(0xFF << sh)) | (b << sh)
    for w, v in words.items():
        ram_set_word(dut, w, v)


def ram_get_bytes(dut, byte_addr, n) -> bytes:
    out = bytearray()
    for i in range(n):
        a = byte_addr + i
        w = a >> 2
        sh = 24 - (a & 3) * 8
        out.append((ram_word(dut, w) >> sh) & 0xFF)
    return bytes(out)


# ── bring-up helpers ─────────────────────────────────────────────────────────
async def reset(dut):
    dut.hresetn.value = 0
    dut.hsel_s.value = 0
    dut.htrans_s.value = 0
    dut.loopback_en.value = 0
    dut.rx_inj_en.value = 0
    dut.rx_inj_dibit.value = 0
    dut.rx_inj_crs.value = 0
    await ClockCycles(dut.hclk, 16)
    dut.hresetn.value = 1
    await ClockCycles(dut.hclk, 16)


async def rd(mac, addr):
    """AhbLiteMaster.read returns (data, hresp); we only want data here."""
    data, _ = await mac.read(addr)
    return data & 0xFFFFFFFF


def new_master(dut):
    return AhbLiteMaster(
        clk=dut.hclk, haddr=dut.haddr_s, htrans=dut.htrans_s,
        hwrite=dut.hwrite_s, hwdata=dut.hwdata_s, hrdata=dut.hrdata_s,
        hready=dut.hreadyout_s, hsel=dut.hsel_s, hsize=dut.hsize_s,
        hburst=dut.hburst_s, hprot=dut.hprot_s, hresp=dut.hresp_s,
        hresetn=dut.hresetn, name="mac")


async def program_common(mac):
    await mac.write(IPGT, 0x15)
    await mac.write(IPGR1, 0x0C)
    await mac.write(IPGR2, 0x12)
    await mac.write(PACKETLEN, (64 << 16) | 1518)   # MINFL=64, MAXFL=1518
    await mac.write(TX_BD_NUM, 1)                    # BD0=TX, BD1=RX
    await mac.write(INT_MASK, 0x1F)                  # TXB|TXE|RXB|RXE|BUSY


async def arm_rx_bd(mac, buf):
    await mac.write(BD_BASE + 1 * 8 + 4, buf)                 # pointer first
    await mac.write(BD_BASE + 1 * 8 + 0, RXF_E | RXF_IRQ | RXF_WR)


async def poll_rx_filled(mac, tries=6000):
    for _ in range(tries):
        w0 = await rd(mac, BD_BASE + 1 * 8 + 0)
        if ((w0 >> 15) & 1) == 0:                    # Empty cleared => filled
            return w0
    return None


# ── RMII external RX injection (di-bits at the pads, NATURAL rxd order) ──────
async def rmii_inject(dut, body: bytes, bad_fcs=False):
    fcs = bytearray(eth_fcs(body))
    if bad_fcs:
        fcs[0] ^= 0xFF
    wire = bytes([0x55] * 7 + [0xD5]) + body + bytes(fcs)
    await FallingEdge(dut.ref_clk)
    dut.rx_inj_en.value = 1
    dut.rx_inj_crs.value = 1
    for b in wire:
        for shift in (0, 2, 4, 6):
            dut.rx_inj_dibit.value = (b >> shift) & 0x3
            await FallingEdge(dut.ref_clk)
    dut.rx_inj_crs.value = 0
    dut.rx_inj_en.value = 0
    dut.rx_inj_dibit.value = 0
    for _ in range(8):
        await FallingEdge(dut.ref_clk)


# ════════════════════════════════════════════════════════════════════════════
@cocotb.test()
async def test_reset_and_moder(dut):
    """AHB path sane: MODER reads its 0x0000A000 reset default."""
    await reset(dut)
    mac = new_master(dut)
    try:
        moder = await rd(mac, MODER)
        dut._log.info("MODER reset default = 0x%08x", moder)
        assert moder == MODER_DEFAULT, f"MODER default 0x{moder:08x} != 0x0000A000"
    finally:
        mac.stop()


@cocotb.test()
async def test_mdio_phyid(dut):
    """MAC eth_miim reads the LAN8720 model's IDs with CORRECT Clause-22 framing
    (the model is NOT mdio_phy_model; its turnaround interoperates with eth_miim,
    so PHYID reads back exact, no >>1 shift)."""
    await reset(dut)
    mac = new_master(dut)
    try:
        await mac.write(MIIMODER, 0x08)     # ClkDiv=8 -> MDC ~1.56 MHz (sim)

        async def mdio_read(regad):
            await mac.write(MIIADDRESS, (PHY_ADDR & 0x1F) | ((regad & 0x1F) << 8))
            # RStat is a self-clearing command (eth_miim's RStatStart resets it);
            # do NOT manually write 0 or it races the start edge.
            await mac.write(MIICOMMAND, MIICMD_RSTAT)
            # MIISTATUS[1]=Busy (NOT [0]=LinkFail). Wait for Busy to assert then
            # clear -- that brackets the whole ~60us MDIO frame.
            seen_busy = False
            for _ in range(20000):
                busy = (await rd(mac, MIISTATUS) >> 1) & 1
                if busy:
                    seen_busy = True
                elif seen_busy:
                    break
                await ClockCycles(dut.hclk, 2)
            return await rd(mac, MIIRX_DATA) & 0xFFFF

        phyid1 = await mdio_read(2)
        phyid2 = await mdio_read(3)
        anar = await mdio_read(4)
        dut._log.info("MDIO PHYID1=0x%04x PHYID2=0x%04x ANAR=0x%04x",
                      phyid1, phyid2, anar)
        assert phyid1 == 0x0007, f"PHYID1 0x{phyid1:04x} != 0x0007 (MDIO framing?)"
        assert phyid2 == 0xC0F1, f"PHYID2 0x{phyid2:04x} != 0xC0F1"
        assert anar == 0x01E1, f"ANAR 0x{anar:04x} != 0x01E1"
    finally:
        mac.stop()


@cocotb.test()
async def test_rx_external_arp(dut):
    """A frame driven at the RMII pads (external media) is RECEIVED by the real
    MAC through rmii_to_mii -> DMA. Also the mode_speed guard's positive case."""
    mode_speed = _plusarg_int("MODE_SPEED", 1)
    await reset(dut)
    mac = new_master(dut)
    try:
        await program_common(mac)
        await arm_rx_bd(mac, RX_BUF)
        await mac.write(MODER, MODER_DEFAULT | MODER_RXEN | MODER_PRO | MODER_FULLD)

        arp = build_arp_request(b"\x00\x11\x22\x33\x44\x55",
                                "192.168.1.1", "192.168.1.101")
        # pad body to 60 so it clears MINFL like a real short frame on the wire
        body = arp + b"\x00" * max(0, 60 - len(arp))
        await rmii_inject(dut, body)
        w0 = await poll_rx_filled(mac)

        if mode_speed == 0:
            # guard (negative): with mode_speed tied 0 the 100M RX cannot lock.
            assert w0 is None, ("mode_speed=0 but RX still filled -- the "
                                "load-bearing speed select was bypassed")
            dut._log.info("mode_speed=0: RX correctly did NOT complete (guard).")
            return

        assert w0 is not None, "RX BD never filled -- pad-driven frame not received"
        rlen = (w0 >> 16) & 0xFFFF
        crc_err = (w0 >> 1) & 1
        dut._log.info("RX BD filled: len=%d status=0x%04x int_o=%d",
                      rlen, w0 & 0xFFFF, int(dut.int_o.value))
        assert crc_err == 0, "RX CRC-error set -- FCS rejected"
        got = ram_get_bytes(dut, RX_BUF, 14)
        assert got == body[:14], f"RX header mismatch: {got.hex()} != {body[:14].hex()}"
        assert int(dut.int_o.value) == 1, "int_o did not assert on RX completion"
        dut._log.info("RX header byte-exact through rmii_to_mii + real MAC.")
    finally:
        mac.stop()


@cocotb.test()
async def test_echo_loopback_roundtrip(dut):
    """ECHO: MAC transmits a frame, the LAN8720 model loops it back at the RMII
    pads (near-end loopback), and the SAME MAC receives it -> DMA. Exercises BOTH
    rmii_to_mii directions (TX split + RX SFD-align/EOF ride-through) + the real
    MAC TX DMA and RX DMA in one round trip."""
    mode_speed = _plusarg_int("MODE_SPEED", 1)
    if mode_speed == 0:
        dut._log.info("mode_speed=0 run: skipping loopback echo (covered by RX guard)")
        return
    await reset(dut)
    dut.loopback_en.value = 1                 # PHY near-end RMII loopback
    mac = new_master(dut)
    try:
        await program_common(mac)
        await arm_rx_bd(mac, RX_BUF)

        # TX frame body: recognisable dst/src/type + marker payload
        body = build_eth(b"\x02\x00\x5e\x00\x01\x01",
                         b"\x02\x00\x00\x00\x00\x01", 0x88B5,
                         b"ECHO-LAN8720-RMII")
        body = body + b"\x00" * max(0, 60 - len(body))
        ram_put_bytes(dut, TX_BUF, body)
        await mac.write(BD_BASE + 0 + 4, TX_BUF)
        await mac.write(BD_BASE + 0 + 0,
                        (len(body) << 16) | (TXF_RD | TXF_IRQ | TXF_WR | TXF_PAD | TXF_CRC))
        await mac.write(MODER, MODER_TXRX)     # TX + RX + loopback path live

        w0 = await poll_rx_filled(mac)
        assert w0 is not None, ("echo round trip failed: MAC did not receive its "
                                "own looped-back frame (RX BD stayed empty)")
        rlen = (w0 >> 16) & 0xFFFF
        crc_err = (w0 >> 1) & 1
        tx_bd = await rd(mac, BD_BASE + 0 + 0)
        dut._log.info("ECHO: RX len=%d status=0x%04x, TX BD RD=%d, tx_frames=%d",
                      rlen, w0 & 0xFFFF, (tx_bd >> 15) & 1, int(dut.tx_frames.value))
        assert crc_err == 0, "echoed frame failed FCS on RX -- loopback corrupted it"
        got = ram_get_bytes(dut, RX_BUF, 14)
        assert got == body[:14], (f"echoed header mismatch: {got.hex()} != "
                                  f"{body[:14].hex()}")
        assert ((tx_bd >> 15) & 1) == 0, "TX BD Ready not cleared -- MAC never transmitted"
        dut._log.info("ECHO round trip OK: frame TX'd, PHY-looped, RX'd byte-exact.")
    finally:
        dut.loopback_en.value = 0
        mac.stop()


@cocotb.test()
async def test_mode_speed_wired(dut):
    """Guard: mode_speed is WIRED to the MAC (the multicore-wrapper bug left it
    unconnected -> tied 0 -> RX dead). Assert the TB drives it AND the rm_eth_ss
    RM wraps it live (not tied off)."""
    expect = _plusarg_int("MODE_SPEED", 1)
    got = int(dut.mode_speed.value)
    assert got == expect, f"dut.mode_speed={got} != expected {expect} (not driven?)"

    # structural: rm_eth_ss must connect .mode_speed to a live value, not GND.
    here = os.path.dirname(__file__)
    rm = os.path.join(here, "..", "..", "fpga", "rp", "eth_ss", "rp_eth_ss_wrapper.sv")
    with open(rm) as fh:
        txt = fh.read()
    import re
    m = re.search(r"\.mode_speed\s*\(\s*([^)]*?)\s*\)", txt)
    assert m, "rp_eth_ss_wrapper.sv: no .mode_speed connection found"
    conn = m.group(1).strip()
    assert conn not in ("1'b0", "0", ""), \
        f"rm_eth_ss ties mode_speed to '{conn}' -- would kill 100M RX"
    dut._log.info("mode_speed wired: TB drives %d; rm_eth_ss connects .mode_speed(%s)",
                  got, conn)
