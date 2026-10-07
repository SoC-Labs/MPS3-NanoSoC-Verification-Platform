"""Pure-logic unit tests for ovlstore_header.py — the QSPI A/B-slot header
pack/unpack and the §8A.5 "interrupted commit can't brick the default"
invariant.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from ovlstore_header import (  # noqa: E402
    HEADER_SIZE, pack_header, pack_slot, unpack_header,
    validate_active_slot, BadOvlstoreHeader,
)


def _slot(valid, static_id=0xA1B2C3D4, rm_id=1):
    return pack_slot(static_id=static_id, rm_id=rm_id, clear_off=0,
                      clear_len=1024, clear_crc=0x1111, part_off=1024,
                      part_len=2048, part_crc=0x2222, valid=valid)


def test_pack_unpack_roundtrip():
    header_bytes = pack_header(active_slot=0, flags=0, slot_a=_slot(True), slot_b=_slot(False))
    assert len(header_bytes) == HEADER_SIZE
    header = unpack_header(header_bytes)
    assert header["magic"] == b"OVLS"
    assert header["active_slot"] == 0
    assert header["slots"][0]["valid"] is True
    assert header["slots"][0]["static_id"] == 0xA1B2C3D4
    assert header["slots"][1]["valid"] is False


def test_bad_magic_rejected():
    header_bytes = bytearray(pack_header(0, 0, _slot(True), _slot(True)))
    header_bytes[0:4] = b"XXXX"
    with pytest.raises(BadOvlstoreHeader):
        unpack_header(bytes(header_bytes))


def test_short_header_rejected():
    with pytest.raises(BadOvlstoreHeader):
        unpack_header(b"OVLS\x01\x00\x00\x00")


def test_healthy_header_has_no_errors():
    header = unpack_header(pack_header(0, 0, _slot(True), _slot(False)))
    assert validate_active_slot(header) == []


def test_active_slot_pointing_at_invalid_slot_is_the_bricking_bug():
    """This is the exact failure mode §8A.5 exists to prevent: an
    interrupted commit that flipped active_slot before the new slot's CRC
    actually verified."""
    header = unpack_header(pack_header(active_slot=1, flags=0,
                                        slot_a=_slot(True), slot_b=_slot(False)))
    errors = validate_active_slot(header)
    assert errors and "would brick" in errors[0]


def test_out_of_range_active_slot_rejected_at_pack_time():
    with pytest.raises(BadOvlstoreHeader):
        pack_header(active_slot=2, flags=0, slot_a=_slot(True), slot_b=_slot(True))
