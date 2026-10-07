"""tests/integration/test_manifest_roundtrip.py

Ties the DFX flow's manifest generator (A2, `fpga/dfx/gen_manifest.py`,
real working Python) to the host verification library's manifest validator
(A4, `host/pyverify/pyverify/overlay.py`, real — imported directly, not
stubbed, since it already exists as of this writing) — exactly the
cross-agent integration `overlay-manifest.md` describes: "Produced by the
DFX flow (A2), consumed by the host pusher/pyverify (A4)."

Why this adds value beyond each agent's own tests: `host/pyverify/tests/
test_overlay.py` (A4) hand-builds its manifest.json fixtures inline and
never touches gen_manifest.py; `fpga/dfx/gen_manifest.py`'s own `verify`
subcommand re-checks its own output with its own CRC logic, not
pyverify's. Neither proves gen_manifest.py's *actual output* parses and
validates cleanly through pyverify.overlay.Overlay — that's what this
file does, plus the two rejection paths overlay-manifest.md is most
insistent about: the UltraScale-mandatory clearing+partial pairing (C2)
and a `static_id` mismatch after a simulated shell rebuild.

Also covers the QSPI A/B-slot store invariant (§8A.5) via
`tests/common/ovlstore_header.py`, which has no A4/pyverify counterpart
(that on-flash struct is firmware-only, `overlay_store.h`) — new, A5-owned
protocol-level coverage, not a duplicate of anyone else's tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# `pyverify` (installed package, else in-tree fallback), `ovlstore_header`
# (tests/common spine), and the `gen_manifest` fixture are all wired up by
# tests/integration/conftest.py — see that file for the boundary rationale.
from pyverify.overlay import (
    Overlay, OverlayManifest, OverlayManifestError, OverlayValidationError,
)
from ovlstore_header import (
    pack_slot, pack_header, unpack_header, validate_active_slot,
)

STATIC_ID = 0xA1B2C3D4

#: A SYNTHETIC rm_id for this codec round-trip. The value is opaque to what is
#: under test here (gen_manifest.py writes it, pyverify.overlay reads it back);
#: what matters is that it does not pretend to be a real design.
#:
#: Its design half (``rm_id & 0xFFFF`` = 0x7A57, encoding v2 --
#: docs/VERSIONING_PLAN.md §3.2) is deliberately OUTSIDE the allocated
#: design_id range in fpga/dfx/rm_list.tcl. It used to be 0x00000001, which
#: both claimed to BE nanosoc and -- post-v2 -- is not even a well-formed
#: nanosoc id (nanosoc v1.0 is 0x01000001). A fixture that LARPs as a real RM
#: is a trap: it goes stale on that RM's next version bump for no reason, since
#: this test never cared which design it was.
RM_ID = 0x0100_7A57


def _write_bin(path: Path, size: int, fill: bytes = b"\xAB") -> Path:
    path.write_bytes(fill * size)
    return path


def test_gen_manifest_output_validates_through_pyverify(tmp_path: Path, gen_manifest):
    """The core round trip: A2's gen_manifest.py builds a manifest.json for
    a {clearing, partial} pair; A4's pyverify.overlay.Overlay loads and
    validates it (schema + on-disk CRC/size + static_id match) with no
    massaging in between.
    """
    clearing_bin = _write_bin(tmp_path / "src_clear.bin", 256, b"\x00\x01\x02\x03")
    partial_bin = _write_bin(tmp_path / "src_partial.bin", 512, b"\xDE\xAD\xBE\xEF")

    rc = gen_manifest.main([
        "build", "--rm-name", "nanosoc", "--rm-id", f"0x{RM_ID:08x}",
        "--static-id", f"0x{STATIC_ID:08x}",
        "--clearing", str(clearing_bin), "--partial", str(partial_bin),
        "--vivado", "2024.1", "--copy", "--out-root", str(tmp_path / "overlay"),
    ])
    assert rc == 0

    overlay_dir = tmp_path / "overlay" / "nanosoc"
    assert (overlay_dir / "manifest.json").is_file()
    assert (overlay_dir / "nanosoc.bin").is_file()
    assert (overlay_dir / "nanosoc_clear.bin").is_file()

    overlay = Overlay.load(overlay_dir)
    assert overlay.manifest.static_id == STATIC_ID
    assert overlay.manifest.rm_id == RM_ID
    assert overlay.manifest.rm_name == "nanosoc"
    overlay.validate(expected_static_id=STATIC_ID)  # must not raise


def test_gen_manifest_refuses_a_lone_partial(tmp_path: Path, gen_manifest):
    """overlay-manifest.md's single biggest correctness rule (C2, the
    UltraScale-mandatory pairing): gen_manifest.py itself must refuse to
    emit a manifest when the clearing bitstream doesn't exist, not defer
    that check to pyverify downstream."""
    partial_bin = _write_bin(tmp_path / "src_partial.bin", 128)
    rc = gen_manifest.main([
        "build", "--rm-name", "nanosoc", "--rm-id", f"0x{RM_ID:08x}",
        "--static-id", f"0x{STATIC_ID:08x}",
        "--clearing", str(tmp_path / "does_not_exist_clear.bin"),
        "--partial", str(partial_bin),
        "--out-root", str(tmp_path / "overlay"),
    ])
    assert rc == 1
    assert not (tmp_path / "overlay" / "nanosoc" / "manifest.json").exists()


def test_pyverify_rejects_static_id_mismatch_after_simulated_shell_rebuild(tmp_path: Path, gen_manifest):
    """overlay-manifest.md: "A shell rebuild changes static_id and
    invalidates every stored partial — the pusher must refuse a mismatch."
    Builds a manifest for one static_id, then validates it against a
    DIFFERENT one (as if the shell had been rebuilt since)."""
    clearing_bin = _write_bin(tmp_path / "c.bin", 64)
    partial_bin = _write_bin(tmp_path / "p.bin", 64, b"\x11")
    gen_manifest.main([
        "build", "--rm-name", "nanosoc", "--rm-id", f"0x{RM_ID:08x}",
        "--static-id", f"0x{STATIC_ID:08x}",
        "--clearing", str(clearing_bin), "--partial", str(partial_bin),
        "--copy", "--out-root", str(tmp_path / "overlay"),
    ])
    overlay = Overlay.load(tmp_path / "overlay" / "nanosoc")
    with pytest.raises(OverlayValidationError, match="static_id mismatch"):
        overlay.validate(expected_static_id=0xDEADBEEF)


def test_pyverify_rejects_manifest_with_missing_partial_key(tmp_path: Path):
    """Direct schema check (not routed through gen_manifest.py this time):
    a manifest.json missing the 'partial' key entirely must be rejected at
    parse time, per overlay-manifest.md "Rules"."""
    overlay_dir = tmp_path / "nanosoc"
    overlay_dir.mkdir()
    manifest = {
        "schema": 1, "static_id": f"0x{STATIC_ID:08x}", "rm_id": f"0x{RM_ID:08x}",
        "rm_name": "nanosoc",
        "clearing": {"file": "c.bin", "len": 4, "crc32": "0x0"},
        # 'partial' deliberately omitted
    }
    (overlay_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(OverlayManifestError, match="missing required key"):
        Overlay.load(overlay_dir)


# --------------------------------------------------------------------------- #
# §8A.5 A/B-slot store invariant — new A5 coverage, no pyverify counterpart
# (overlay_store.h's on-flash struct is firmware-only).
# --------------------------------------------------------------------------- #

def test_ab_slot_commit_cannot_brick_the_default():
    """What this proves: overlay_store_commit()'s documented safety
    property ("An interrupted/failed write cannot brick the default — it
    falls back to the last-good slot") reduces to one checkable invariant
    on the header: active_slot must always point at a slot marked valid.
    Simulates an interrupted commit (active_slot flipped before the new
    slot's CRC actually verified) and confirms it's caught.
    """
    slot_a = pack_slot(static_id=STATIC_ID, rm_id=0, clear_off=0, clear_len=1024,
                        clear_crc=0x1111, part_off=1024, part_len=2048,
                        part_crc=0x2222, valid=True)   # last-good greybox default
    slot_b_interrupted = pack_slot(static_id=STATIC_ID, rm_id=RM_ID, clear_off=0,
                                    clear_len=1024, clear_crc=0x1111, part_off=1024,
                                    part_len=2048, part_crc=0x2222, valid=False)  # CRC never confirmed

    header = unpack_header(pack_header(active_slot=1, flags=0, slot_a=slot_a, slot_b=slot_b_interrupted))
    errors = validate_active_slot(header)
    assert errors, "an active_slot pointing at an invalid slot must be flagged"

    # The safe recovery: fall back to slot A (still valid).
    header_recovered = unpack_header(pack_header(active_slot=0, flags=0, slot_a=slot_a, slot_b=slot_b_interrupted))
    assert validate_active_slot(header_recovered) == []
