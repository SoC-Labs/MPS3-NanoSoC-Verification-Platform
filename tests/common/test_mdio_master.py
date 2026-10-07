"""Pure-logic unit tests for mdio_master.py's Clause-22 framing — no
cocotb, no DUT, no simulator. Proves the frame encode/decode math is
correct before it's ever bit-banged onto a real mdio_i_o/mdc_i pair in
tests/mdio_phy_model/test_mdio_phy_model.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from mdio_master import (  # noqa: E402
    ST, OP_WRITE, OP_READ, REG_BMCR,
    BMCR_AN_ENABLE, BMCR_DUPLEX_FULL, BMCR_SPEED_LSB,
    build_frame_bits, decode_read_reply,
)


def _bits_to_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | (b & 1)
    return v


def test_write_frame_field_layout():
    data = BMCR_AN_ENABLE | BMCR_SPEED_LSB | BMCR_DUPLEX_FULL
    bits = build_frame_bits(OP_WRITE, phyad=0x03, regad=REG_BMCR, data=data,
                             include_preamble=False)
    # ST(2) OP(2) PHYAD(5) REGAD(5) TA(2) DATA(16) = 32 bits, no preamble.
    assert len(bits) == 32
    assert _bits_to_int(bits[0:2]) == ST
    assert _bits_to_int(bits[2:4]) == OP_WRITE
    assert _bits_to_int(bits[4:9]) == 0x03
    assert _bits_to_int(bits[9:14]) == REG_BMCR
    assert bits[14:16] == [1, 0]           # TA driven "10" by the station on a write
    assert _bits_to_int(bits[16:32]) == data


def test_read_frame_releases_ta_and_data():
    bits = build_frame_bits(OP_READ, phyad=0x03, regad=REG_BMCR, include_preamble=False)
    assert len(bits) == 32
    assert bits[14] is None and bits[15] is None   # TA undriven on a read
    assert all(b is None for b in bits[16:32])     # DATA undriven (PHY drives it)


def test_preamble_is_32_ones_by_default():
    bits = build_frame_bits(OP_WRITE, 0, 0, 0)
    assert bits[:32] == [1] * 32
    assert len(bits) == 32 + 32


def test_decode_read_reply_roundtrip():
    # Simulate the corrected model's reply: one turnaround bit-time, then DATA
    # (MSB-first) at sampled indices 1..16, then a trailing idle bit-time —
    # matching a real PHY read one MDC period after the turnaround.
    sampled = [1] + [int(c) for c in format(0xBEEF, "016b")] + [1]
    value, ta = decode_read_reply(sampled)
    assert value == 0xBEEF
    assert ta == 1


def test_decode_read_reply_requires_18_bits():
    import pytest
    with pytest.raises(AssertionError):
        decode_read_reply([0, 1, 2])
