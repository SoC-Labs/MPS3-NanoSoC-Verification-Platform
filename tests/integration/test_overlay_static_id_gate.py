"""tests/integration/test_overlay_static_id_gate.py

Unit-tests the R9 harness gate
``scripts/harness_gates/check_overlay_static_id.py``: every
``fpga/dfx/overlay/<rm>/manifest.json``'s ``static_id`` must match the one
authoritative shell id encoded in ``fpga/dfx/overlay/mps3_shell_static_id.c``.

Why this gate (and this test) exists: ``gen_manifest.py verify`` only
round-trips each manifest's CRC/size against its own payloads (see
``test_manifest_roundtrip.py``); it never compares a manifest's ``static_id``
against the shell's. So an overlay set keyed to a DEAD shell (a shell rebuild
re-mints ``static_id`` and invalidates every stored partial —
``overlay-manifest.md``) sailed through ``make check`` stage [8/8] and only
surfaced two stages later as a confusing pyverify e2e mismatch. This is the
documented risk **R9** in ``docs/PLATFORM_STATUS_AND_ROADMAP.md``.

The gate lives in ``scripts/harness_gates/`` (a directory of standalone
pure-stdlib gate scripts with no package structure), so — exactly like
``conftest.load_gen_manifest`` does for ``fpga/dfx/gen_manifest.py`` — it is
loaded BY FILE PATH rather than inventing a ``sys.path`` package relationship
the script never asks for. Each case drives ``gate.main(["--repo", TREE])``
against a synthetic overlay tree under ``tmp_path`` and asserts the exit code
plus the named-stale / SKIP / missing-shell messaging on stdout. No real
``fpga/dfx/overlay/**`` file is read or written.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "scripts" / "harness_gates" / "check_overlay_static_id.py"

SHELL_ID = 0x3A8BBA62
DEAD_ID = 0xECCEDBF3


def _load_gate():
    """Load the standalone gate script by path (see module docstring)."""
    spec = importlib.util.spec_from_file_location("check_overlay_static_id", _GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


def _shell_c(overlay_dir: Path, static_id: int = SHELL_ID) -> None:
    """Write a mps3_shell_static_id.c stub in the generated file's exact
    shape (function body with a `return 0x...UL;`)."""
    overlay_dir.mkdir(parents=True, exist_ok=True)
    (overlay_dir / "mps3_shell_static_id.c").write_text(
        "uint32_t mps3_shell_static_id(void)\n{\n"
        "    return 0x%08XUL;\n}\n" % static_id
    )


def _manifest(overlay_dir: Path, rm_name: str, static_id: str, rm_id: str = "0x01007a57") -> None:
    """Write overlay/<rm>/manifest.json carrying a given static_id string
    (payload keys elided — this gate reads only static_id).

    The default rm_id is SYNTHETIC: its design half (0x7A57, encoding v2) sits
    outside the allocated design_id range in fpga/dfx/rm_list.tcl on purpose, so
    this fixture cannot be mistaken for — or go stale with — a real RM. This
    gate does not read rm_id at all; the field is present only to keep the
    manifest schema-valid.
    """
    rm_dir = overlay_dir / rm_name
    rm_dir.mkdir(parents=True, exist_ok=True)
    (rm_dir / "manifest.json").write_text(json.dumps({
        "schema": 1, "static_id": static_id, "rm_id": rm_id, "rm_name": rm_name,
    }) + "\n")


def _run(gate, tree: Path):
    return gate.main(["--repo", str(tree)])


def test_all_match_passes(gate, tmp_path, capsys):
    """Every overlay keyed to the shell id -> exit 0. Underscore-grouped hex
    (overlay-manifest.md's own rm_id grouping) must normalise equal."""
    ov = tmp_path / "fpga" / "dfx" / "overlay"
    _shell_c(ov)
    _manifest(ov, "greybox", "0x3A8BBA62")
    _manifest(ov, "nanosoc", "0x0000_3A8B_BA62")  # same value, grouped
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: all 2 overlay static_ids match" in out
    assert "STALE" not in out


def test_one_stale_fails_and_is_named(gate, tmp_path, capsys):
    """A single overlay on a dead shell -> exit 1, that overlay named, with
    the actionable re-mint remediation."""
    ov = tmp_path / "fpga" / "dfx" / "overlay"
    _shell_c(ov)
    _manifest(ov, "greybox", "0x3A8BBA62")
    _manifest(ov, "nanosoc", "0xECCEDBF3")  # keyed to a dead shell
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE  nanosoc" in out
    assert "keyed to a DEAD shell: nanosoc" in out
    assert "greybox" in out  # the good one is still reported...
    assert "STALE  greybox" not in out  # ...but not flagged
    assert "re-mint against 0x3A8BBA62" in out
    assert "invalidates every stored partial" in out


def test_missing_shell_file_fails(gate, tmp_path, capsys):
    """Manifests present but no mps3_shell_static_id.c -> exit 1 (cannot
    certify the set against any shell)."""
    ov = tmp_path / "fpga" / "dfx" / "overlay"
    _manifest(ov, "greybox", "0x3A8BBA62")  # no _shell_c()
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "authoritative shell id file missing" in out


def test_unparseable_shell_file_fails(gate, tmp_path, capsys):
    """A shell C file with no parseable return literal -> exit 1."""
    ov = tmp_path / "fpga" / "dfx" / "overlay"
    ov.mkdir(parents=True, exist_ok=True)
    (ov / "mps3_shell_static_id.c").write_text("/* nothing useful here */\n")
    _manifest(ov, "greybox", "0x3A8BBA62")
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "cannot parse shell static_id" in out


def test_no_overlays_skips(gate, tmp_path, capsys):
    """No manifests yet -> clean SKIP (exit 0), matching the Makefile's
    check-overlays guard."""
    (tmp_path / "fpga" / "dfx" / "overlay").mkdir(parents=True, exist_ok=True)
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIP" in out
