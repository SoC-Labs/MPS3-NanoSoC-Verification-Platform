"""Pure-logic unit tests for frames.py — no cocotb, no DUT, no simulator.
Proves the error-injection frame-building/checking logic gen_checker's
bench relies on is correct before any RTL exists to check it against.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from frames import (  # noqa: E402
    MIN_FRAME_LEN, MAX_FRAME_LEN,
    build_frame, build_runt, build_giant, check_frame, ifg_violation,
)

DST = bytes.fromhex("001122334455")
SRC = bytes.fromhex("aabbccddeeff")


def test_good_frame_passes_checker():
    frame = build_frame(DST, SRC, 0x0800, b"hello world")
    fcs_ok, length_ok, reason = check_frame(frame)
    assert fcs_ok and length_ok and reason == "ok"
    assert len(frame) == MIN_FRAME_LEN  # padded up to the 64B minimum


def test_bad_fcs_detected():
    frame = build_frame(DST, SRC, 0x0800, b"hello world", bad_fcs=True)
    fcs_ok, length_ok, reason = check_frame(frame)
    assert not fcs_ok
    assert length_ok  # bad_fcs alone shouldn't also look like a length fault
    assert "bad FCS" in reason


def test_runt_detected():
    frame = build_runt(DST, SRC)
    fcs_ok, length_ok, reason = check_frame(frame)
    assert len(frame) < MIN_FRAME_LEN
    assert fcs_ok            # runt-but-well-formed: FCS over the short body still checks out
    assert not length_ok
    assert "runt" in reason


def test_giant_detected():
    frame = build_giant(DST, SRC)
    fcs_ok, length_ok, reason = check_frame(frame)
    assert len(frame) > MAX_FRAME_LEN
    assert not length_ok
    assert "giant" in reason


def test_giant_and_bad_fcs_combine():
    frame = build_giant(DST, SRC)
    frame = frame[:-4] + bytes(b ^ 0xFF for b in frame[-4:])  # corrupt FCS in place
    fcs_ok, length_ok, reason = check_frame(frame)
    assert not fcs_ok and not length_ok
    assert "bad FCS" in reason and "giant" in reason


def test_ifg_violation_flagged():
    bit_time_ns = 10  # 100 Mb/s
    violated, gap_bits = ifg_violation(
        prev_frame_end_ns=1000, next_frame_start_ns=1000 + 50 * bit_time_ns,
        bit_time_ns=bit_time_ns,
    )
    assert violated and gap_bits == 50


def test_ifg_exactly_at_minimum_not_flagged():
    bit_time_ns = 10
    violated, gap_bits = ifg_violation(
        prev_frame_end_ns=1000, next_frame_start_ns=1000 + 96 * bit_time_ns,
        bit_time_ns=bit_time_ns,
    )
    assert not violated and gap_bits == 96
