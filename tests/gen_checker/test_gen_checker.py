"""tests/gen_checker/test_gen_checker.py

Verifies `fpga/ethernet/gen_checker/gen_checker.sv` — the error-injection
traffic generator/checker. This is the second of only two blocks needing
real new RTL (ARCHITECTURE_SPEC.md §12) and "what makes it MAC
*verification* rather than 'it pings'" (spec §8.1, §8.4).

REGMAP: this bench targets the **shell-regmap.md v0.2 GENCHK table**
(CTRL@0x00 {gen_en,chk_en} / INJECT@0x04 {bad_fcs,runt,giant,ifg,dribble} /
TX_CNT@0x08 / RX_CNT@0x0C / ERR_CNT@0x10). The previous revision of this
bench used the Phase-0 DRAFT layout (mode-field GEN_CTRL / packed
CHK_STATUS / CHK_CLEAR) that predates the v0.2 contract table; per
established practice the bench was fixed to follow the contract
(see dut_notes.md). `tests/common/regmap.py` still carries the stale draft
constants — flagged for A6 (out of this bench's write scope) — so the v0.2
constants are defined locally below.

The generator and checker are tested as **two independent functions**
(gen_checker/README.md), each at its own AXI-Stream port — this bench does
not assume any internal loopback between `gen_m_*` and `chk_s_*`.

Counter usage: all counter assertions are DELTA-based (read-before /
read-after), so they hold regardless of the RTL's clear-on-enable-rise v1
semantics (a gen_checker.sv addition where the v0.2 contract is silent).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, Timer

from dut_presence import rtl_ready
from frames import build_frame, build_runt, build_giant, check_frame, eth_fcs, \
    IFG_MIN_BITS, ifg_violation
from axis import AxisByteMonitor, AxisByteDriver
from random_stim import (RandomAxiMaster, random_axi_burst, seeded_rng,
                         rand_frame_len, rand_payload)
from regmap import AxiLiteMaster

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "ethernet", "gen_checker")
NO_RTL = not rtl_ready(_RTL_DIR, ["gen_checker.sv"])

# --------------------------------------------------------------------------- #
# GENCHK regmap constants — shell-regmap.md v0.2 (local: regmap.py still has
# the stale Phase-0 draft, see module docstring).
# --------------------------------------------------------------------------- #
GENCHK_CTRL = 0x00     # [0] gen_en, [1] chk_en
GENCHK_INJECT = 0x04   # [0] bad_fcs, [1] runt, [2] giant, [3] ifg, [4] dribble
GENCHK_TX_CNT = 0x08   # ro
GENCHK_RX_CNT = 0x0C   # ro
GENCHK_ERR_CNT = 0x10  # ro
GENCHK_DUT_IP = 0x14      # ro: sniffed DUT IPv4, network order (byte0 in [31:24])
GENCHK_DUT_STATUS = 0x18  # ro: [0] ip_seen, [31:16] last-seen ethertype

STATUS_IP_SEEN = 1 << 0

CTRL_GEN_EN = 1 << 0
CTRL_CHK_EN = 1 << 1

INJ_BAD_FCS = 1 << 0
INJ_RUNT = 1 << 1
INJ_GIANT = 1 << 2
INJ_IFG = 1 << 3
INJ_DRIBBLE = 1 << 4

DST = bytes.fromhex("001122334455")
SRC = bytes.fromhex("aabbccddeeff")

CLK_NS = 10
BITS_PER_CYCLE = 8  # byte-wide AXIS at 100 Mb/s: 1 idle cycle = 8 bit-times


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, CLK_NS, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.gen_m_tready.value = 0
    dut.chk_s_tvalid.value = 0
    dut.chk_s_tdata.value = 0
    dut.chk_s_tlast.value = 0
    dut.chk_s_tuser.value = 0
    await Timer(50, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)
    axi = AxiLiteMaster.from_dut(dut)
    gen_mon = AxisByteMonitor(dut.s_axi_aclk, dut.gen_m_tdata, dut.gen_m_tvalid,
                              dut.gen_m_tready, tlast=dut.gen_m_tlast)
    chk_drv = AxisByteDriver(dut.s_axi_aclk, dut.chk_s_tdata, dut.chk_s_tvalid,
                             dut.chk_s_tready, tlast=dut.chk_s_tlast, tuser=dut.chk_s_tuser)
    return axi, gen_mon, chk_drv


async def _gap_cycles_until_next_frame(dut, timeout_cycles: int = 200) -> int:
    """Counts idle cycles (gen_m_tvalid low immediately before the edge)
    between the just-committed tlast beat and the first beat of the next
    frame. Pre-edge sampling, same discipline as tests/common/axis.py."""
    gap = 0
    for _ in range(timeout_cycles):
        await ReadOnly()
        valid = bool(int(dut.gen_m_tvalid.value))
        await RisingEdge(dut.s_axi_aclk)
        if valid:
            return gap
        gap += 1
    raise TimeoutError(f"no next frame within {timeout_cycles} cycles")


# --------------------------------------------------------------------------- #
# Generator cases — each pends a fault via INJECT, enables the generator,
# captures gen_m_*, and independently re-checks it with frames.check_frame()
# (the SAME malformation properties the DUT MAC's own RX path must detect).
# --------------------------------------------------------------------------- #

@cocotb.test(skip=NO_RTL)
async def test_generator_good_frame(dut):
    """What this proves: with no INJECT faults pending, gen_en emits a
    well-formed frame (valid FCS, 64..1518B) — the baseline every malformed
    case is a deliberate deviation from — and TX_CNT counts it."""
    axi, gen_mon, _chk = await _bring_up(dut)
    tx_before, _ = await axi.read(GENCHK_TX_CNT)
    await axi.write(GENCHK_CTRL, CTRL_GEN_EN)
    frame, _user = await gen_mon.read_packet()
    fcs_ok, length_ok, reason = check_frame(frame)
    assert fcs_ok and length_ok, f"good frame failed its own check: {reason}"
    tx_after, _ = await axi.read(GENCHK_TX_CNT)
    assert tx_after > tx_before, "TX_CNT must count generated frames"
    await axi.write(GENCHK_CTRL, 0)


@cocotb.test(skip=NO_RTL)
async def test_generator_bad_fcs_consumed_on_next_frame(dut):
    """What this proves: INJECT.bad_fcs corrupts exactly the NEXT frame's
    FCS (spec §8.1's "bad FCS" — the case exercising the DUT MAC's RX
    CRC-error path), the pending bit reads back as consumed, and the frame
    after that is clean again (per-frame one-shot semantics, v0.2 "fault to
    inject on next frame")."""
    axi, gen_mon, _chk = await _bring_up(dut)
    await axi.write(GENCHK_INJECT, INJ_BAD_FCS)
    pend, _ = await axi.read(GENCHK_INJECT)
    assert pend == INJ_BAD_FCS, "INJECT must read back pending faults"
    await axi.write(GENCHK_CTRL, CTRL_GEN_EN)

    frame1, _ = await gen_mon.read_packet()
    fcs_ok, _length_ok, reason = check_frame(frame1)
    assert not fcs_ok, f"bad-fcs injection must corrupt the FCS: {reason}"

    frame2, _ = await gen_mon.read_packet()
    fcs_ok2, length_ok2, reason2 = check_frame(frame2)
    assert fcs_ok2 and length_ok2, f"fault must be one-shot; frame 2: {reason2}"

    pend_after, _ = await axi.read(GENCHK_INJECT)
    assert pend_after == 0, "INJECT pending bits must clear once consumed"
    await axi.write(GENCHK_CTRL, 0)


@cocotb.test(skip=NO_RTL)
async def test_generator_runt(dut):
    """What this proves: INJECT.runt emits a frame under the 64B minimum
    (spec §8.1's "runt")."""
    axi, gen_mon, _chk = await _bring_up(dut)
    await axi.write(GENCHK_INJECT, INJ_RUNT)
    await axi.write(GENCHK_CTRL, CTRL_GEN_EN)
    frame, _user = await gen_mon.read_packet()
    _fcs_ok, length_ok, reason = check_frame(frame)
    assert not length_ok and "runt" in reason, f"runt mode must produce <64B: {reason}"
    await axi.write(GENCHK_CTRL, 0)


@cocotb.test(skip=NO_RTL)
async def test_generator_giant(dut):
    """What this proves: INJECT.giant emits a frame over the 1518B untagged
    maximum (spec §8.1's "giant")."""
    axi, gen_mon, _chk = await _bring_up(dut)
    await axi.write(GENCHK_INJECT, INJ_GIANT)
    await axi.write(GENCHK_CTRL, CTRL_GEN_EN)
    frame, _user = await gen_mon.read_packet()
    _fcs_ok, length_ok, reason = check_frame(frame)
    assert not length_ok and "giant" in reason, f"giant mode must produce >1518B: {reason}"
    await axi.write(GENCHK_CTRL, 0)


@cocotb.test(skip=NO_RTL)
async def test_generator_dribble(dut):
    """What this proves: INJECT.dribble appends one trailing byte AFTER a
    frame whose body+FCS are otherwise valid (gen_checker.sv's documented
    byte-granular approximation of dribble bits): stripping the final byte
    leaves a correct FCS over the nominal frame, while the frame as
    delivered fails a naive FCS-over-last-4-bytes check."""
    axi, gen_mon, _chk = await _bring_up(dut)
    await axi.write(GENCHK_INJECT, INJ_DRIBBLE)
    await axi.write(GENCHK_CTRL, CTRL_GEN_EN)
    frame, _user = await gen_mon.read_packet()
    fcs_ok, _length_ok, _reason = check_frame(frame)
    assert not fcs_ok, "dribble frame must not FCS-check as delivered"
    nominal = frame[:-1]
    import struct
    fcs_field = struct.unpack("<I", nominal[-4:])[0]
    assert fcs_field == eth_fcs(nominal[:-4]), \
        "stripping the dribble byte must leave a valid frame underneath"
    await axi.write(GENCHK_CTRL, 0)


@cocotb.test(skip=NO_RTL)
async def test_generator_ifg_violation(dut):
    """What this proves: INJECT.ifg compresses the inter-frame gap AFTER
    the injected frame below IEEE 802.3's 96 bit-times (spec §8.1's "IFG
    violation"), and the following gap is back to legal — measured in AXIS
    byte-cycles (8 bit-times each; gen_checker.sv's documented modelling
    level) and scored with frames.ifg_violation()."""
    axi, gen_mon, _chk = await _bring_up(dut)
    await axi.write(GENCHK_INJECT, INJ_IFG)
    await axi.write(GENCHK_CTRL, CTRL_GEN_EN)

    _frame1, _ = await gen_mon.read_packet()   # gap after this one is short
    gap_short = await _gap_cycles_until_next_frame(dut)
    bit_ns = CLK_NS / BITS_PER_CYCLE
    violated, gap_bits = ifg_violation(0.0, gap_short * CLK_NS, bit_ns)
    assert violated, (
        f"INJECT.ifg must compress the following gap below {IFG_MIN_BITS} "
        f"bit-times (saw {gap_bits})")

    _frame2, _ = await gen_mon.read_packet()   # clean frame, normal gap after
    gap_normal = await _gap_cycles_until_next_frame(dut)
    violated2, gap_bits2 = ifg_violation(0.0, gap_normal * CLK_NS, bit_ns)
    assert not violated2, (
        f"gap after a non-injected frame must be >= {IFG_MIN_BITS} bit-times "
        f"(saw {gap_bits2})")
    await axi.write(GENCHK_CTRL, 0)


# --------------------------------------------------------------------------- #
# Checker cases — independently drive known-good/malformed frames onto
# chk_s_* and confirm RX_CNT/ERR_CNT move correctly (spec §8.4's
# "independently check the DUT MAC's TX framing/CRC" half).
# --------------------------------------------------------------------------- #

async def _counts(axi):
    rx, _ = await axi.read(GENCHK_RX_CNT)
    err, _ = await axi.read(GENCHK_ERR_CNT)
    return rx, err


@cocotb.test(skip=NO_RTL)
async def test_checker_detects_bad_fcs(dut):
    """What this proves: the checker independently flags a bad-FCS frame
    fed in on chk_s_* with tuser=0 (link_partner_mac did NOT already flag
    it) — the checker must catch what the upstream tap missed, not just
    rubber-stamp it (spec §8.4)."""
    axi, _gen, chk_drv = await _bring_up(dut)
    await axi.write(GENCHK_CTRL, CTRL_CHK_EN)
    rx0, err0 = await _counts(axi)

    await chk_drv.write(build_frame(DST, SRC, 0x0800, b"payload", bad_fcs=True), user=0)
    await Timer(100, units="ns")

    rx1, err1 = await _counts(axi)
    assert rx1 == rx0 + 1, "RX_CNT must count every checked frame"
    assert err1 == err0 + 1, "checker must increment ERR_CNT on a bad-FCS frame"


@cocotb.test(skip=NO_RTL)
async def test_checker_detects_runt_and_giant(dut):
    """What this proves: the checker flags both length-envelope violations
    (spec §8.1's "runt"/"giant"), not just FCS errors."""
    axi, _gen, chk_drv = await _bring_up(dut)
    await axi.write(GENCHK_CTRL, CTRL_CHK_EN)
    _rx0, err0 = await _counts(axi)

    await chk_drv.write(build_runt(DST, SRC), user=0)
    await Timer(100, units="ns")
    _rx1, err1 = await _counts(axi)
    assert err1 == err0 + 1, "checker must flag a runt frame"

    await chk_drv.write(build_giant(DST, SRC), user=0)
    await Timer(100, units="ns")
    _rx2, err2 = await _counts(axi)
    assert err2 == err1 + 1, "checker must flag a giant frame"


@cocotb.test(skip=NO_RTL)
async def test_checker_good_frame_not_flagged(dut):
    """What this proves: the checker doesn't false-positive on a
    well-formed frame — the negative control every positive case above
    depends on being meaningful."""
    axi, _gen, chk_drv = await _bring_up(dut)
    await axi.write(GENCHK_CTRL, CTRL_CHK_EN)
    rx0, err0 = await _counts(axi)

    await chk_drv.write(build_frame(DST, SRC, 0x0800, b"hello"), user=0)
    await Timer(100, units="ns")

    rx1, err1 = await _counts(axi)
    assert rx1 == rx0 + 1, "a well-formed frame must still increment RX_CNT"
    assert err1 == err0, "a well-formed frame must not increment ERR_CNT"


@cocotb.test(skip=NO_RTL)
async def test_checker_flags_link_partner_mac_tuser_error(dut):
    """What this proves: chk_s_tuser (link_partner_mac's own frame-error
    tap) is cross-checked, not ignored — a structurally well-formed frame
    arriving with tuser=1 still counts as an error, exercising that this
    input is actually wired in (gen_checker.sv: "a cross-check, not the
    sole source of truth")."""
    axi, _gen, chk_drv = await _bring_up(dut)
    await axi.write(GENCHK_CTRL, CTRL_CHK_EN)
    rx0, err0 = await _counts(axi)

    await chk_drv.write(build_frame(DST, SRC, 0x0800, b"hello"), user=1)
    await Timer(100, units="ns")

    rx1, err1 = await _counts(axi)
    assert rx1 == rx0 + 1
    assert err1 == err0 + 1, \
        "chk_s_tuser=1 must be reflected in ERR_CNT even for an otherwise-good frame"


@cocotb.test(skip=NO_RTL)
async def test_checker_disabled_does_not_count(dut):
    """What this proves: with CTRL.chk_en=0 the sink still accepts beats
    (tready is unconditional — the tap must never backpressure the
    datapath) but counts nothing."""
    axi, _gen, chk_drv = await _bring_up(dut)
    rx0, err0 = await _counts(axi)
    await chk_drv.write(build_frame(DST, SRC, 0x0800, b"uncounted"), user=0)
    await Timer(100, units="ns")
    rx1, err1 = await _counts(axi)
    assert (rx1, err1) == (rx0, err0), "chk_en=0 must gate all counting"


@cocotb.test(skip=NO_RTL)
async def test_wstrb_lane0_gates_register_write(dut):
    """WSTRB byte-lane test (previously untested on EVERY AXI slave — the BFM
    only ever drove strb=0xF). gen_checker's whole rw register decode is gated
    on wstrb[0] (`aw_accept && s_axi_wstrb[0]`). A partial-strobe write with
    lane 0 disabled must be ignored on both CTRL and INJECT; a slave that
    ignores WSTRB (commits the whole word) fails here. gen_en stays 0
    throughout so the generator never consumes the pending INJECT bits."""
    axi, _gen, _chk = await _bring_up(dut)

    # Seed all five INJECT bits with a full-word write.
    await axi.write_bytes(GENCHK_INJECT, 0x1F, 0xF)
    val, _ = await axi.read(GENCHK_INJECT)
    assert val == 0x1F, f"INJECT seed {val:#x}"

    # Lane 0 disabled: ignored (pending bits survive).
    await axi.write_bytes(GENCHK_INJECT, 0x00, 0x2)
    val, _ = await axi.read(GENCHK_INJECT)
    assert val == 0x1F, "INJECT write with wstrb lane0 disabled must be ignored (WSTRB ignored?)"

    # Lane 0 enabled: the clear lands.
    await axi.write_bytes(GENCHK_INJECT, 0x00, 0x1)
    val, _ = await axi.read(GENCHK_INJECT)
    assert val == 0x00, "a lane-0-enabled INJECT write must land"

    # Same gating on CTRL: seed chk_en, then a lane-0-disabled clear is ignored.
    await axi.write_bytes(GENCHK_CTRL, CTRL_CHK_EN, 0xF)
    val, _ = await axi.read(GENCHK_CTRL)
    assert val & CTRL_CHK_EN, "CTRL.chk_en seed"
    await axi.write_bytes(GENCHK_CTRL, 0x0, 0x2)
    val, _ = await axi.read(GENCHK_CTRL)
    assert val & CTRL_CHK_EN, "a CTRL write with wstrb lane0 disabled must be ignored"


@cocotb.test(skip=NO_RTL)
async def test_random_aw_en_corners(dut):
    """Constrained-random AXI4-Lite aw_en corner coverage (A5 random wave):
    randomized AW/W arrival order (same / AW-first / W-first), zero-gap
    back-to-back writes, read-during-write, and randomized WSTRB across the
    CTRL/INJECT rw registers plus reads of the RO counters. gen_checker uses
    the accept-on-valid response style (awready+bvalid same edge) — this is
    the bench where the SVA cover c_resp_same_cycle fires. Seed logged."""
    _axi, _gen, _chk = await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "gen_checker")
    m = RandomAxiMaster.from_dut(dut)
    waddrs = [GENCHK_CTRL, GENCHK_INJECT]
    raddrs = [GENCHK_CTRL, GENCHK_INJECT, GENCHK_TX_CNT, GENCHK_RX_CNT, GENCHK_ERR_CNT]
    tally = await random_axi_burst(m, rng, waddrs, raddrs, n=48)
    dut._log.info(f"[gen_checker random] orderings/paths exercised: {tally}")


@cocotb.test(skip=NO_RTL)
async def test_random_checker_frames(dut):
    """Constrained-random checker-side stimulus (A5 random wave). The fixed
    benches feed a handful of hand-built frames; this drives MANY frames of
    randomized length (including the exact 64 / 1518 length-envelope
    boundaries), random payloads, a random mix of clean / bad-FCS / runt /
    giant malformations, and random link_partner tuser flags — then confirms
    RX_CNT counts every frame and ERR_CNT increments on exactly the frames an
    independent re-check (frames.check_frame) or tuser marks bad. Randomized
    clean/fault interleaving is inherent in the random `kind` sequence. Seed
    logged so any mismatch is reproducible."""
    axi, _gen, chk_drv = await _bring_up(dut)
    rng, _seed = seeded_rng(dut, "gen_checker")
    await axi.write(GENCHK_CTRL, CTRL_CHK_EN)

    async def _drive_and_check(frame, tuser, label):
        rx0, err0 = await _counts(axi)
        await chk_drv.write(frame, user=tuser)
        await Timer(150, units="ns")
        rx1, err1 = await _counts(axi)
        fcs_ok, length_ok, _reason = check_frame(frame)
        exp_err = (not fcs_ok) or (not length_ok) or bool(tuser)
        assert rx1 == rx0 + 1, f"{label}: RX_CNT must count the frame (Δ={rx1-rx0})"
        assert (err1 - err0) == (1 if exp_err else 0), (
            f"{label}: ERR_CNT Δ={err1-err0} != {1 if exp_err else 0} "
            f"(fcs_ok={fcs_ok} len_ok={length_ok} tuser={tuser}, len={len(frame)})")

    # Exact length-envelope boundaries (64 min, 1518 untagged max), good frames.
    for total in (64, 1518):
        payload = rand_payload(rng, total - 14 - 4)
        frame = build_frame(DST, SRC, 0x0800, payload, pad_to_min=False)
        assert len(frame) == total
        await _drive_and_check(frame, tuser=0, label=f"boundary-{total}")

    # Randomized frames: random length / payload / malformation / tuser.
    for i in range(14):
        kind = rng.choice(["good", "good", "bad_fcs", "runt", "giant"])
        tuser = rng.randint(0, 1)
        if kind in ("good", "bad_fcs"):
            total = rand_frame_len(rng, 64, 1518)
            payload = rand_payload(rng, total - 14 - 4)
            frame = build_frame(DST, SRC, 0x0800, payload,
                                bad_fcs=(kind == "bad_fcs"), pad_to_min=False)
        elif kind == "runt":
            frame = build_runt(DST, SRC, payload=rand_payload(rng, rng.randint(0, 20)))
        else:  # giant
            frame = build_giant(DST, SRC, extra=rng.randint(1, 96))
        await _drive_and_check(frame, tuser, label=f"rand-{i}-{kind}")


# --------------------------------------------------------------------------- #
# DUT-IP sniffer cases (Phase-3, DUT_IP@0x14 / DUT_STATUS@0x18). The sniffer
# passively snoops the SAME chk_s_* tap, latching the DUT's own IPv4 address so
# the shell firmware can learn it WITHOUT arming the checker (always-on,
# independent of chk_en). Untagged frames only (a VLAN tag shifts the offsets —
# gen_checker.sv documented limitation).
#
# frames.py has no ARP/IPv4 builder, so minimal ones live here (as the task
# allows). Both reuse build_frame() so the eth header (dst/src/ethertype) and
# FCS come from the proven behavioural spec; only the payload differs.
# --------------------------------------------------------------------------- #
import struct  # noqa: E402


def _ip_bytes(dotted: str) -> bytes:
    """'192.168.13.37' -> b'\\xc0\\xa8\\x0d\\x25' (network order)."""
    parts = [int(x) for x in dotted.split(".")]
    assert len(parts) == 4 and all(0 <= p <= 255 for p in parts)
    return bytes(parts)


def _ip_word(dotted: str) -> int:
    """Expected DUT_IP readback: first octet in bits[31:24] (network order)."""
    return int.from_bytes(_ip_bytes(dotted), "big")


def build_ipv4(dst_mac, src_mac, src_ip, dst_ip="0.0.0.0", proto=17,
               payload=b"payload-data"):
    """Untagged IPv4 frame. Minimal 20-byte IPv4 header; src IP lands at frame
    bytes 26..29 (eth header 14 + IPv4 src-addr field offset 12). Header
    checksum is left 0 — the gen_checker sniffer never inspects it."""
    total_len = 20 + len(payload)
    ipv4 = bytes([0x45, 0x00]) + struct.pack(">H", total_len)  # ver/IHL, DSCP, len
    ipv4 += b"\x00\x00" + b"\x00\x00"                          # id, flags/frag
    ipv4 += bytes([64, proto]) + b"\x00\x00"                   # TTL, proto, checksum
    ipv4 += _ip_bytes(src_ip) + _ip_bytes(dst_ip)             # src@26..29, dst@30..33
    return build_frame(dst_mac, src_mac, 0x0800, ipv4 + payload)


def build_arp(dst_mac, src_mac, sender_ip, sender_mac=None,
              target_ip="0.0.0.0", oper=1):
    """Untagged ARP frame (request by default). Sender-protocol-address (SPA)
    lands at frame bytes 28..31 (eth header 14 + ARP SPA offset 14)."""
    if sender_mac is None:
        sender_mac = src_mac
    arp = struct.pack(">HHBBH", 0x0001, 0x0800, 6, 4, oper)   # htype ptype hlen plen oper
    arp += sender_mac + _ip_bytes(sender_ip)                  # SHA@22..27, SPA@28..31
    arp += dst_mac + _ip_bytes(target_ip)                     # THA, TPA
    return build_frame(dst_mac, src_mac, 0x0806, arp)


async def _read_sniffer(axi):
    ip, _ = await axi.read(GENCHK_DUT_IP)
    status, _ = await axi.read(GENCHK_DUT_STATUS)
    seen = bool(status & STATUS_IP_SEEN)
    etype = (status >> 16) & 0xFFFF
    return ip, seen, etype


@cocotb.test(skip=NO_RTL)
async def test_sniffer_offsets_are_frame_helper_correct(dut):
    """Sanity-check the local ARP/IPv4 builders put the address where the RTL
    reads it: IPv4 src at frame bytes 26..29, ARP SPA at 28..31, ethertype at
    12..13. A bench bug here would make the sniffer tests vacuous."""
    ipf = build_ipv4(DST, SRC, "192.168.13.37")
    assert ipf[12:14] == b"\x08\x00", "IPv4 ethertype must be at bytes 12..13"
    assert ipf[26:30] == _ip_bytes("192.168.13.37"), "IPv4 src must be at bytes 26..29"
    arpf = build_arp(DST, SRC, "10.0.2.15")
    assert arpf[12:14] == b"\x08\x06", "ARP ethertype must be at bytes 12..13"
    assert arpf[28:32] == _ip_bytes("10.0.2.15"), "ARP SPA must be at bytes 28..31"


@cocotb.test(skip=NO_RTL)
async def test_sniffer_latches_ipv4_src(dut):
    """What this proves: the always-on sniffer extracts the DUT's IPv4 SOURCE
    address (frame bytes 26..29) from an IPv4 frame it transmits, presents it
    at DUT_IP in network order (first octet in [31:24]), sets DUT_STATUS.ip_seen,
    and records ethertype 0x0800. IP_SEEN starts clear (reset baseline)."""
    axi, _gen, chk_drv = await _bring_up(dut)

    ip0, seen0, _ = await _read_sniffer(axi)
    assert ip0 == 0 and not seen0, f"post-reset: DUT_IP={ip0:#010x} seen={seen0}"

    src = "192.168.13.37"
    await chk_drv.write(build_ipv4(DST, SRC, src), user=0)
    await Timer(100, units="ns")

    ip, seen, etype = await _read_sniffer(axi)
    assert seen, "DUT_STATUS.ip_seen must set once an IPv4 src is latched"
    assert ip == _ip_word(src), (
        f"DUT_IP={ip:#010x} != expected network-order {_ip_word(src):#010x} "
        f"({src}) — byte order wrong")
    assert etype == 0x0800, f"DUT_STATUS ethertype={etype:#06x}, expected 0x0800"


@cocotb.test(skip=NO_RTL)
async def test_sniffer_latches_arp_spa(dut):
    """What this proves: for an ARP frame (ethertype 0x0806) the sniffer takes
    the sender-protocol-address (frame bytes 28..31, NOT the IPv4 offset),
    presents it at DUT_IP in network order, and records ethertype 0x0806. ARP
    is the address a freshly-booted DUT reveals first (gratuitous ARP)."""
    axi, _gen, chk_drv = await _bring_up(dut)

    spa = "10.0.2.15"
    await chk_drv.write(build_arp(DST, SRC, spa), user=0)
    await Timer(100, units="ns")

    ip, seen, etype = await _read_sniffer(axi)
    assert seen, "DUT_STATUS.ip_seen must set once an ARP SPA is latched"
    assert ip == _ip_word(spa), (
        f"DUT_IP={ip:#010x} != expected network-order {_ip_word(spa):#010x} "
        f"({spa}) — wrong offset (IPv4 26..29 vs ARP 28..31?) or byte order")
    assert etype == 0x0806, f"DUT_STATUS ethertype={etype:#06x}, expected 0x0806"


@cocotb.test(skip=NO_RTL)
async def test_sniffer_ignores_non_ip_frame(dut):
    """What this proves: an untagged non-IP frame (the generator's own
    experimental ethertype 0x88B5, neither 0x0800 nor 0x0806) does NOT disturb
    a previously-latched DUT_IP and leaves ip_seen set — the ethertype gate
    means only ARP/IPv4 frames update the address. (The last-seen ethertype
    field does track 0x88B5; it is a debug aid, not the address.)"""
    axi, _gen, chk_drv = await _bring_up(dut)

    # First latch a known IPv4 address.
    src = "172.16.5.9"
    await chk_drv.write(build_ipv4(DST, SRC, src), user=0)
    await Timer(100, units="ns")
    ip_before, seen_before, _ = await _read_sniffer(axi)
    assert seen_before and ip_before == _ip_word(src), "precondition: IPv4 latched"

    # Now feed a non-IP frame carrying bytes at the IP offsets that would be a
    # DIFFERENT address if the ethertype gate were absent.
    await chk_drv.write(build_frame(DST, SRC, 0x88B5, b"\xde\xad\xbe\xef" * 8), user=0)
    await Timer(100, units="ns")

    ip_after, seen_after, etype = await _read_sniffer(axi)
    assert ip_after == ip_before, (
        f"non-IP frame changed DUT_IP {ip_before:#010x} -> {ip_after:#010x} "
        f"(ethertype gate missing?)")
    assert seen_after, "ip_seen must stay set across a non-IP frame"
    assert etype == 0x88B5, f"last-seen ethertype should track 0x88B5, got {etype:#06x}"


@cocotb.test(skip=NO_RTL)
async def test_sniffer_is_always_on_without_chk_en(dut):
    """What this proves: the sniffer is INDEPENDENT of CTRL.chk_en — the DUT IP
    appears with the checker disabled (chk_en left 0 the whole test), and no
    frame is counted (RX_CNT unchanged), confirming the sniffer taps the stream
    directly rather than piggy-backing the checker's enable."""
    axi, _gen, chk_drv = await _bring_up(dut)
    rx0, _ = await axi.read(GENCHK_RX_CNT)

    src = "8.8.4.4"
    await chk_drv.write(build_ipv4(DST, SRC, src), user=0)
    await Timer(100, units="ns")

    ip, seen, _ = await _read_sniffer(axi)
    assert seen and ip == _ip_word(src), (
        f"sniffer must latch with chk_en=0: DUT_IP={ip:#010x} seen={seen}")
    rx1, _ = await axi.read(GENCHK_RX_CNT)
    assert rx1 == rx0, "chk_en=0: the sniffer must not count frames (RX_CNT moved)"


@cocotb.test(skip=NO_RTL)
async def test_sniffer_last_frame_wins(dut):
    """What this proves: successive IP-bearing frames overwrite DUT_IP (last
    frame wins) — a DUT that renumbers is reflected, and the shift register
    doesn't accumulate stale octets across frames."""
    axi, _gen, chk_drv = await _bring_up(dut)

    await chk_drv.write(build_ipv4(DST, SRC, "1.2.3.4"), user=0)
    await Timer(100, units="ns")
    await chk_drv.write(build_arp(DST, SRC, "255.254.253.252"), user=0)
    await Timer(100, units="ns")

    ip, seen, etype = await _read_sniffer(axi)
    assert seen and ip == _ip_word("255.254.253.252"), (
        f"last frame must win: DUT_IP={ip:#010x}")
    assert etype == 0x0806, "last-seen ethertype should be the ARP frame's"
