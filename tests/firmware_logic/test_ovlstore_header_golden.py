"""tests/firmware_logic/test_ovlstore_header_golden.py — the cross-language
golden test that LOCKS the OVLSTORE on-flash A/B-slot header's byte layout
(docs/contracts/overlay-manifest.md's "A/B slot store") across its two
independent implementations, so OPEN_ISSUES I15's little-endian resolution
can never silently drift again:

- **Firmware half (C):** ``firmware/overlay_store/ovlstore_codec.c`` — the
  real, host-testable pack/unpack — exercised through
  ``firmware/test/bin/ovlstore_pack`` (built here via the firmware Makefile's
  own recipe, same pattern as ``test_json_golden.py`` drives ``ctrl_echo``).
  ``pack`` mode emits the golden header's raw bytes; ``repack`` mode reads
  raw bytes on stdin, unpacks + re-packs them, and emits the result.
- **Python half:** ``tests/common/ovlstore_header.py`` — the Python model of
  the same struct.

Both are the REAL artifacts, not re-implementations. The golden vector below
is deliberately **asymmetric** — every u16/u32 field carries four distinct,
non-palindromic bytes — so a byte-swap (an accidental big-endian flip on
either side) changes the emitted bytes and fails the compare.

The assertions:
  1. C ``pack`` bytes == Python ``pack_header`` bytes (byte-identical).
  2. Python can round-trip the C bytes: unpack the C output, re-pack, and
     get the same bytes back (and the recovered fields equal the vector).
  3. C can round-trip the Python bytes: feed the Python output to C
     ``repack`` and get the same bytes back.

Import bootstrap: ``tests/common`` is put on ``sys.path`` locally (this
directory has no conftest doing it, unlike ``tests/integration/``).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FW_TEST_DIR = _REPO_ROOT / "firmware" / "test"
_OVLSTORE_PACK = _FW_TEST_DIR / "bin" / "ovlstore_pack"
_TESTS_COMMON = _REPO_ROOT / "tests" / "common"

if str(_TESTS_COMMON) not in sys.path:
    sys.path.insert(0, str(_TESTS_COMMON))

import ovlstore_header as H  # noqa: E402  (path bootstrap must come first)

_MAKE = shutil.which("make")
_CC = shutil.which("gcc") or shutil.which("cc")

pytestmark = [
    pytest.mark.skipif(not _FW_TEST_DIR.is_dir(), reason="firmware/test/ not present"),
    pytest.mark.skipif(_MAKE is None, reason="no `make` on PATH -- cannot build ovlstore_pack"),
    pytest.mark.skipif(_CC is None, reason="no gcc/cc on PATH -- firmware/test/Makefile needs one"),
]

# The GOLDEN header — MUST stay byte-identical to firmware/test/ovlstore_pack.c's
# golden_header(). Asymmetric on purpose: every multi-byte field has four
# distinct bytes so an endianness flip is detectable. ver MUST be H.VER (1) or
# the C repack-mode unpack rejects the input (ver=1 is still LE-distinguishable:
# 01 00 vs 00 01).
GOLDEN_ACTIVE_SLOT = 0
GOLDEN_FLAGS = 0xA5
GOLDEN_SLOT_A = dict(
    static_id=0xA1B2C3D4, rm_id=0x11223344, clear_off=0x0A0B0C0D,
    clear_len=0x10203040, clear_crc=0xDEADBEEF, part_off=0x01020304,
    part_len=0x05060708, part_crc=0xCAFEF00D, valid=1,
)
GOLDEN_SLOT_B = dict(
    static_id=0x0BADF00D, rm_id=0x55667788, clear_off=0x090A0B0C,
    clear_len=0x0D0E0F10, clear_crc=0xFEEDFACE, part_off=0x11121314,
    part_len=0x15161718, part_crc=0x19202122, valid=0,
)

# Field order the C codec serializes (u32*8 then u8) -- pinned so a reorder on
# either side is caught too, not only an endianness flip.
_SLOT_FIELDS = ("static_id", "rm_id", "clear_off", "clear_len", "clear_crc",
                "part_off", "part_len", "part_crc", "valid")


def _python_golden_bytes() -> bytes:
    """Pack the golden header via the REAL Python model."""
    slot_a = H.pack_slot(*(GOLDEN_SLOT_A[k] for k in _SLOT_FIELDS))
    slot_b = H.pack_slot(*(GOLDEN_SLOT_B[k] for k in _SLOT_FIELDS))
    return H.pack_header(GOLDEN_ACTIVE_SLOT, GOLDEN_FLAGS, slot_a, slot_b)


@pytest.fixture(scope="session")
def ovlstore_pack_bin() -> Path:
    """Build bin/ovlstore_pack via the firmware Makefile's own recipe (single
    source of truth for how firmware test tools are built)."""
    result = subprocess.run(
        ["make", "bin/ovlstore_pack"],
        cwd=str(_FW_TEST_DIR),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"building ovlstore_pack failed:\n{result.stdout}\n{result.stderr}"
    )
    assert _OVLSTORE_PACK.exists()
    return _OVLSTORE_PACK


def _run(bin_path: Path, mode: str, stdin: bytes = b"") -> bytes:
    proc = subprocess.run(
        [str(bin_path), mode], input=stdin,
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"ovlstore_pack {mode} exited {proc.returncode}: {proc.stderr!r}"
    )
    return proc.stdout


def test_c_pack_bytes_are_identical_to_python_pack_bytes(ovlstore_pack_bin: Path):
    """The load-bearing assertion: the real C codec and the Python model emit
    the SAME 74 raw bytes for the same asymmetric header. This is what makes
    a future one-sided endianness (or field-order) change fail CI."""
    c_bytes = _run(ovlstore_pack_bin, "pack")
    py_bytes = _python_golden_bytes()
    assert len(c_bytes) == H.HEADER_SIZE == 74
    assert c_bytes == py_bytes, (
        "C ovlstore_codec.c and Python ovlstore_header.py disagree on the "
        f"on-flash header bytes (I15 drift!):\n  C : {c_bytes.hex()}\n"
        f"  py: {py_bytes.hex()}"
    )


def test_first_field_is_little_endian(ovlstore_pack_bin: Path):
    """Pin the actual endianness, not just C/Python agreement (both could be
    wrong together). static_id 0xA1B2C3D4 must serialize LSB-first."""
    c_bytes = _run(ovlstore_pack_bin, "pack")
    # bytes 0..3 = magic "OVLS"; 4..5 = ver LE; 6 = active_slot; 7 = flags;
    # 8..11 = slot A static_id, little-endian.
    assert c_bytes[0:4] == H.MAGIC == b"OVLS"
    assert c_bytes[4:6] == b"\x01\x00"          # ver=1, little-endian
    assert c_bytes[6] == GOLDEN_ACTIVE_SLOT
    assert c_bytes[7] == GOLDEN_FLAGS
    assert c_bytes[8:12] == bytes((0xD4, 0xC3, 0xB2, 0xA1))  # static_id LE


def test_python_round_trips_the_c_bytes(ovlstore_pack_bin: Path):
    """Python (unpack) can consume the C-produced bytes, recover every field
    exactly, and re-pack to the identical bytes."""
    c_bytes = _run(ovlstore_pack_bin, "pack")
    hdr = H.unpack_header(c_bytes)
    assert hdr["magic"] == H.MAGIC
    assert hdr["ver"] == H.VER
    assert hdr["active_slot"] == GOLDEN_ACTIVE_SLOT
    assert hdr["flags"] == GOLDEN_FLAGS
    for got, expected in ((hdr["slots"][0], GOLDEN_SLOT_A),
                          (hdr["slots"][1], GOLDEN_SLOT_B)):
        for k in _SLOT_FIELDS:
            want = bool(expected[k]) if k == "valid" else expected[k]
            assert got[k] == want, f"field {k}: got {got[k]!r} want {want!r}"
    # re-pack the recovered fields and confirm bit-for-bit stability
    slot_a = H.pack_slot(*(hdr["slots"][0][k] for k in _SLOT_FIELDS))
    slot_b = H.pack_slot(*(hdr["slots"][1][k] for k in _SLOT_FIELDS))
    repacked = H.pack_header(hdr["active_slot"], hdr["flags"], slot_a, slot_b)
    assert repacked == c_bytes


def test_c_round_trips_the_python_bytes(ovlstore_pack_bin: Path):
    """The C codec (repack mode: unpack then re-pack) can consume the
    Python-produced bytes and reproduce them exactly."""
    py_bytes = _python_golden_bytes()
    c_repacked = _run(ovlstore_pack_bin, "repack", stdin=py_bytes)
    assert c_repacked == py_bytes, (
        "C codec did not round-trip the Python bytes byte-for-byte:\n"
        f"  in : {py_bytes.hex()}\n  out: {c_repacked.hex()}"
    )
