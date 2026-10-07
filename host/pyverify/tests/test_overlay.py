"""Real, no-hardware tests for pyverify.overlay: manifest parsing, CRC32,
and the validation rules from overlay-manifest.md. Everything here is
pure filesystem + JSON + zlib — no sockets, no subprocesses, no board.
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from pyverify.overlay import (
    Overlay,
    OverlayManifest,
    OverlayManifestError,
    OverlayValidationError,
    compute_crc32,
)

STATIC_ID = 0xA1B2C3D4
RM_ID = 1


def _write_overlay(tmp_path: Path, *, corrupt_partial: bool = False,
                    wrong_static_id: bool = False, drop_partial_key: bool = False,
                    ) -> Path:
    overlay_dir = tmp_path / "nanosoc"
    overlay_dir.mkdir()

    clearing_bytes = b"\x00\x01\x02\x03" * 100  # multiple of 4, arbitrary
    partial_bytes = b"\xde\xad\xbe\xef" * 200
    (overlay_dir / "nanosoc_clear.bin").write_bytes(clearing_bytes)
    (overlay_dir / "nanosoc.bin").write_bytes(partial_bytes)

    manifest = {
        "schema": 1,
        "static_id": f"0x{(STATIC_ID if not wrong_static_id else 0xDEADBEEF):08X}",
        "rm_id": f"0x{RM_ID:08X}",
        "rm_name": "nanosoc",
        "clearing": {
            "file": "nanosoc_clear.bin",
            "len": len(clearing_bytes),
            "crc32": f"0x{zlib.crc32(clearing_bytes):08X}",
        },
        "partial": {
            "file": "nanosoc.bin",
            "len": len(partial_bytes) + (1 if corrupt_partial else 0),
            "crc32": f"0x{zlib.crc32(partial_bytes):08X}",
        },
        "built": "2026-07-04",
        "vivado": "2024.1",
    }
    if drop_partial_key:
        del manifest["partial"]
    (overlay_dir / "manifest.json").write_text(json.dumps(manifest))

    if corrupt_partial:
        # Flip a byte so the CRC on disk no longer matches the manifest,
        # independent of the length-mismatch case above.
        data = bytearray((overlay_dir / "nanosoc.bin").read_bytes())
        data[0] ^= 0xFF
        (overlay_dir / "nanosoc.bin").write_bytes(bytes(data))

    return overlay_dir


def test_load_and_validate_good_overlay(tmp_path: Path) -> None:
    overlay_dir = _write_overlay(tmp_path)
    overlay = Overlay.load(overlay_dir)

    assert overlay.manifest.static_id == STATIC_ID
    assert overlay.manifest.rm_id == RM_ID
    assert overlay.manifest.rm_name == "nanosoc"

    overlay.validate(expected_static_id=STATIC_ID)  # must not raise


def test_validate_rejects_static_id_mismatch(tmp_path: Path) -> None:
    overlay_dir = _write_overlay(tmp_path)
    overlay = Overlay.load(overlay_dir)
    with pytest.raises(OverlayValidationError, match="static_id mismatch"):
        overlay.validate(expected_static_id=0x11111111)


def test_validate_rejects_crc_mismatch(tmp_path: Path) -> None:
    overlay_dir = _write_overlay(tmp_path, corrupt_partial=True)
    overlay = Overlay.load(overlay_dir)
    with pytest.raises(OverlayValidationError, match="crc32 mismatch"):
        overlay.validate(expected_static_id=STATIC_ID)


def test_manifest_missing_partial_key_is_invalid(tmp_path: Path) -> None:
    overlay_dir = _write_overlay(tmp_path, drop_partial_key=True)
    with pytest.raises(OverlayManifestError, match="missing required key"):
        Overlay.load(overlay_dir)


def test_manifest_accepts_underscore_grouped_hex(tmp_path: Path) -> None:
    """The subject here is the PARSER (PEP-515 underscore grouping via
    ``int(x, 0)``), so the value is arbitrary — but it is a synthetic one
    (design half 0x7A57, outside rm_list.tcl's allocated range) rather than a
    real RM's id, so it can never go stale on a design version bump."""
    overlay_dir = _write_overlay(tmp_path)
    raw = json.loads((overlay_dir / "manifest.json").read_text())
    raw["rm_id"] = "0x0100_7a57"
    (overlay_dir / "manifest.json").write_text(json.dumps(raw))

    manifest = OverlayManifest.from_dict(raw)
    assert manifest.rm_id == 0x01007A57


def test_compute_crc32_matches_zlib(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    payload = bytes(range(256)) * 17
    p.write_bytes(payload)
    assert compute_crc32(p) == (zlib.crc32(payload) & 0xFFFFFFFF)


# --------------------------------------------------------------------------- #
# Failure arms — the pre-flight validator's rejection paths, previously
# unproven. These are the host-side guards that must fire BEFORE any bitstream
# crosses the wire (pyverify.swap calls overlay.validate() first).
# --------------------------------------------------------------------------- #

def _good_manifest_dict() -> dict:
    """A structurally-valid manifest dict (no files on disk needed) for the
    from_dict() type-guard tests below."""
    return {
        "schema": 1,
        "static_id": f"0x{STATIC_ID:08X}",
        "rm_id": f"0x{RM_ID:08X}",
        "rm_name": "nanosoc",
        "clearing": {"file": "nanosoc_clear.bin", "len": 400, "crc32": "0x1"},
        "partial": {"file": "nanosoc.bin", "len": 800, "crc32": "0x2"},
    }


def test_load_rejects_malformed_manifest_json(tmp_path: Path) -> None:
    """A manifest.json that isn't valid JSON is an OverlayManifestError from
    load(), not a raw json.JSONDecodeError leaking out."""
    overlay_dir = tmp_path / "nanosoc"
    overlay_dir.mkdir()
    (overlay_dir / "manifest.json").write_text('{"schema": 1, "static_id": ,,,}')
    with pytest.raises(OverlayManifestError, match="invalid JSON"):
        Overlay.load(overlay_dir)


def test_load_rejects_missing_manifest(tmp_path: Path) -> None:
    empty = tmp_path / "nanosoc"
    empty.mkdir()
    with pytest.raises(OverlayManifestError, match="no manifest at"):
        Overlay.load(empty)


def test_validate_reports_missing_clearing_and_partial_bin(tmp_path: Path) -> None:
    """manifest.json references .bin files that aren't on disk — validate()
    must report BOTH (it accumulates every problem, not just the first)."""
    overlay_dir = tmp_path / "nanosoc"
    overlay_dir.mkdir()
    (overlay_dir / "manifest.json").write_text(json.dumps(_good_manifest_dict()))
    overlay = Overlay.load(overlay_dir)  # parses fine; files just don't exist
    with pytest.raises(OverlayValidationError) as exc:
        overlay.validate(expected_static_id=STATIC_ID)
    msg = str(exc.value)
    assert "clearing file missing" in msg
    assert "partial file missing" in msg


def test_validate_reports_length_mismatch(tmp_path: Path) -> None:
    overlay_dir = _write_overlay(tmp_path)
    # Rewrite the manifest so clearing.len disagrees with the on-disk size.
    raw = json.loads((overlay_dir / "manifest.json").read_text())
    raw["clearing"]["len"] = raw["clearing"]["len"] + 4
    (overlay_dir / "manifest.json").write_text(json.dumps(raw))
    overlay = Overlay.load(overlay_dir)
    with pytest.raises(OverlayValidationError, match="length mismatch"):
        overlay.validate(expected_static_id=STATIC_ID)


def test_validate_reports_missing_ltx_and_fw(tmp_path: Path) -> None:
    """Optional artefacts referenced by the manifest but absent on disk must
    still be flagged (a manifest promising an .ltx you can't reload later)."""
    overlay_dir = _write_overlay(tmp_path)
    raw = json.loads((overlay_dir / "manifest.json").read_text())
    raw["ltx"] = "nanosoc.ltx"          # referenced, never written
    raw["fw"] = "nanosoc_app.bin"        # referenced, never written
    (overlay_dir / "manifest.json").write_text(json.dumps(raw))
    overlay = Overlay.load(overlay_dir)
    with pytest.raises(OverlayValidationError) as exc:
        overlay.validate(expected_static_id=STATIC_ID)
    msg = str(exc.value)
    assert "ltx referenced but missing" in msg
    assert "fw referenced but missing" in msg


def test_from_dict_rejects_non_object_root() -> None:
    with pytest.raises(OverlayManifestError, match="manifest root must be a JSON object"):
        OverlayManifest.from_dict([1, 2, 3])  # type: ignore[arg-type]


def test_from_dict_rejects_non_object_clearing_partial() -> None:
    d = _good_manifest_dict()
    d["clearing"] = "nanosoc_clear.bin"  # a bare string, not the {file,len,crc32} object
    with pytest.raises(OverlayManifestError, match="must both be objects"):
        OverlayManifest.from_dict(d)


def test_overlayfile_rejects_missing_key() -> None:
    d = _good_manifest_dict()
    del d["partial"]["crc32"]
    with pytest.raises(OverlayManifestError, match="missing required key"):
        OverlayManifest.from_dict(d)


def test_overlayfile_rejects_empty_file_name() -> None:
    d = _good_manifest_dict()
    d["partial"]["file"] = ""
    with pytest.raises(OverlayManifestError, match="must be a non-empty string"):
        OverlayManifest.from_dict(d)


def test_overlayfile_rejects_negative_len() -> None:
    d = _good_manifest_dict()
    d["clearing"]["len"] = -4
    with pytest.raises(OverlayManifestError, match="len must be a non-negative int"):
        OverlayManifest.from_dict(d)


def test_parse_int_rejects_unparseable_hex_string() -> None:
    d = _good_manifest_dict()
    d["static_id"] = "0xZZZZ"
    with pytest.raises(OverlayManifestError, match="cannot parse"):
        OverlayManifest.from_dict(d)


def test_parse_int_rejects_wrong_type() -> None:
    d = _good_manifest_dict()
    d["rm_id"] = [1, 2, 3]  # not int, not hex string
    with pytest.raises(OverlayManifestError, match="expected int or hex string"):
        OverlayManifest.from_dict(d)


def test_static_usercode_parsed_when_present(tmp_path: Path) -> None:
    """``static_usercode`` carries the identity of the static implementation the
    partials are BOUND to (BITSTREAM.CONFIG.USERID, read back from hardware as
    REGISTER.USERCODE). Unlike ``static_id`` — checked against a provisioned file
    on the target, so it reads correct whatever is actually flown — this can be
    verified against the running device before a swap."""
    overlay_dir = _write_overlay(tmp_path)
    raw = json.loads((overlay_dir / "manifest.json").read_text())
    raw["static_usercode"] = "0x5263642C"
    (overlay_dir / "manifest.json").write_text(json.dumps(raw))

    assert Overlay.load(overlay_dir).manifest.static_usercode == 0x5263642C


def test_static_usercode_absent_is_none_not_an_error(tmp_path: Path) -> None:
    """The field is optional: manifests built before the guard existed must keep
    loading. Callers distinguish 'unknown' (None) from a real value and refuse the
    swap rather than assuming the flown static is correct."""
    overlay_dir = _write_overlay(tmp_path)
    assert "static_usercode" not in json.loads((overlay_dir / "manifest.json").read_text())

    assert Overlay.load(overlay_dir).manifest.static_usercode is None
