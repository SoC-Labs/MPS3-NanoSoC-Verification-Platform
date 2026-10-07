"""frames.py — Ethernet frame construction/CRC/error-injection helpers for
the `fpga/ethernet/gen_checker/gen_checker.sv` bench (ARCHITECTURE_SPEC.md
§8.1 malformation modes: bad FCS, runt, giant, IFG violation, dribble;
§8.4 "two-sided verification").

Pure functions — no cocotb/DUT dependency — so tests/common/test_frames.py
exercises these directly, with no simulator, proving the malformation
logic itself is correct before any RTL uses it as a reference.
"""
from __future__ import annotations

import struct
import zlib

MIN_FRAME_LEN = 64     # dest(6)+src(6)+type(2)+payload+fcs(4), untagged
MAX_FRAME_LEN = 1518   # untagged max, no preamble/SFD
IFG_MIN_BITS = 96      # IEEE 802.3 minimum inter-frame gap, bit-times


def eth_fcs(frame_without_fcs: bytes) -> int:
    """Ethernet FCS = CRC-32/ISO-HDLC — the same algorithm as `zlib.crc32`
    (poly 0x04C11DB7 reflected, init/final all-ones)."""
    return zlib.crc32(frame_without_fcs) & 0xFFFFFFFF


def build_frame(dst_mac: bytes, src_mac: bytes, ethertype: int, payload: bytes,
                 bad_fcs: bool = False, pad_to_min: bool = True) -> bytes:
    """Builds dst+src+ethertype+payload(+pad)+fcs. FCS is transmitted
    LSB-first on the wire (`<I`), matching standard Ethernet framing."""
    assert len(dst_mac) == 6 and len(src_mac) == 6
    header = dst_mac + src_mac + struct.pack(">H", ethertype)
    body = header + payload
    if pad_to_min and len(body) + 4 < MIN_FRAME_LEN:
        body += b"\x00" * (MIN_FRAME_LEN - 4 - len(body))
    fcs = eth_fcs(body)
    if bad_fcs:
        fcs ^= 0xFFFFFFFF  # deterministic, obviously-wrong CRC
    return body + struct.pack("<I", fcs)


def build_runt(dst_mac: bytes, src_mac: bytes, ethertype: int = 0x0800,
               payload: bytes = b"\x00" * 8) -> bytes:
    """< MIN_FRAME_LEN total (no padding) — violates the 64B minimum."""
    return build_frame(dst_mac, src_mac, ethertype, payload, pad_to_min=False)


def build_giant(dst_mac: bytes, src_mac: bytes, ethertype: int = 0x0800,
                extra: int = 64) -> bytes:
    """> MAX_FRAME_LEN total by `extra` bytes."""
    payload = b"\xAA" * (MAX_FRAME_LEN - 4 - 14 + extra)
    return build_frame(dst_mac, src_mac, ethertype, payload, pad_to_min=False)


def check_frame(frame: bytes):
    """Independent checker side: recompute FCS over all-but-last-4 bytes
    and compare to the transmitted (LSB-first) FCS field, and check the
    length envelope. Returns (fcs_ok, length_ok, reason). Mirrors what
    gen_checker.sv's checker function (TODO(A1)) and, ultimately, the
    real DUT MAC's own RX CRC check must do.
    """
    if len(frame) < 4:
        return False, False, "frame shorter than FCS field"
    body, fcs_wire = frame[:-4], frame[-4:]
    fcs_rx = struct.unpack("<I", fcs_wire)[0]
    fcs_ok = fcs_rx == eth_fcs(body)
    length_ok = MIN_FRAME_LEN <= len(frame) <= MAX_FRAME_LEN
    reasons = []
    if not fcs_ok:
        reasons.append("bad FCS")
    if len(frame) < MIN_FRAME_LEN:
        reasons.append("runt")
    if len(frame) > MAX_FRAME_LEN:
        reasons.append("giant")
    return fcs_ok, length_ok, ",".join(reasons) or "ok"


def ifg_violation(prev_frame_end_ns: float, next_frame_start_ns: float,
                   bit_time_ns: float):
    """IFG violation check (spec §8.1): the gap between the end of one
    frame and the start of the next, in bit-times, must be >= 96
    (IEEE 802.3's minimum inter-frame gap). Returns (violated, gap_bits).
    """
    gap_bits = (next_frame_start_ns - prev_frame_end_ns) / bit_time_ns
    return gap_bits < IFG_MIN_BITS, gap_bits
