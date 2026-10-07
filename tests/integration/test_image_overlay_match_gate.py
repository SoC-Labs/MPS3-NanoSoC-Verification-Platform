"""tests/integration/test_image_overlay_match_gate.py

Unit-tests the harness gate ``scripts/harness_gates/check_image_overlay_match.py``:
the bootable image and the shipped overlays must come from ONE implementation run.

Why this gate (and this test) exists: a partial bitstream is bound to the exact
static place-and-route it was built against. Loading overlay partials onto a
DIFFERENT static implementation destroys the whole FPGA configuration — this
happened twice on 2026-07-24 (docs/ICAP_SWAP_PROVEN.md,
docs/PLATFORM_ONE_IMPL.md). ``static_id`` cannot catch it (it identifies the
DESIGN, not the impl; both bitstreams carry the same value), so this gate checks
``BITSTREAM.CONFIG.USERID`` — the 8-hex build commit, which the device reports
back as ``REGISTER.USERCODE`` and which DIFFERS between impl runs. Three things
must agree: the image's UserID, ``board_image.tcl``'s declared ``BIT_USERCODE``,
and every overlay manifest's ``static_usercode``.

The gate lives in ``scripts/harness_gates/`` (standalone pure-stdlib scripts, no
package), so — like ``test_overlay_static_id_gate.py`` — it is loaded BY FILE
PATH. Each case drives ``gate.main(["--repo", TREE, "--overlays", GLOB])`` against
a synthetic tree under ``tmp_path`` and asserts the exit code plus the
MATCH / mismatch / NOTE / SKIP / cannot-identify messaging. No real
``fpga/dfx/overlay**`` or bitstream is read.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "scripts" / "harness_gates" / "check_image_overlay_match.py"

GOOD = "0x5263642C"        # the DFX static's USERID (docs/PLATFORM_ONE_IMPL.md)
OTHER = "0x0EE58A4D"       # a different impl's identity


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_image_overlay_match", _GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


# --- fixture builders ---------------------------------------------------------

def _board_image_tcl(tree: Path, bit_path: str, bit_usercode: str | None) -> None:
    """Write board_scripts/board_image.tcl in the real file's shape: an
    MPS3_BOARD_BIT override branch (with a '$' expansion the parser must ignore)
    and the default `set BIT` literal, plus `set BIT_USERCODE`."""
    d = tree / "src" / "linux_soc" / "hw" / "board_scripts"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "if { [info exists ::env(MPS3_BOARD_BIT)] } {",
        "    set BIT $::env(MPS3_BOARD_BIT)",
        "} else {",
        f"    set BIT {bit_path}",
        "}",
    ]
    if bit_usercode is not None:
        lines.append(f"set BIT_USERCODE {bit_usercode}")
    (d / "board_image.tcl").write_text("\n".join(lines) + "\n")


def _bit(path: Path, userid_token: bytes | None) -> None:
    """Write a fake .bit whose first 200 bytes carry (or omit) a UserID token,
    exactly what usercode_from_bitstream() scans for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = b"\x00\x09\x0f\xf0" + b"a;UserName=x;" + (userid_token or b"") + b";" + b"\xff" * 64
    path.write_bytes(head + b"\xff" * 4096)


def _overlay(tree: Path, rm: str, static_usercode: str | None) -> None:
    d = tree / "fpga" / "dfx" / "overlay_linux" / rm
    d.mkdir(parents=True, exist_ok=True)
    m = {"schema": 1, "static_id": "0x2B082E1B", "rm_id": "0x0100001E", "rm_name": rm,
         "clearing": {"file": f"{rm}_clear.bin", "len": 1, "crc32": "0x0"},
         "partial": {"file": f"{rm}.bin", "len": 1, "crc32": "0x0"}}
    if static_usercode is not None:
        m["static_usercode"] = static_usercode
    (d / "manifest.json").write_text(json.dumps(m) + "\n")


_GLOB = "fpga/dfx/overlay_linux/*/manifest.json"


def _run(gate, tree: Path):
    return gate.main(["--repo", str(tree), "--overlays", _GLOB])


# --- unit tests on the helpers ------------------------------------------------

def test_norm32_normalises(gate):
    # Contract: 0x-prefixed strings (what manifests carry and usercode_from_bitstream
    # emits) and ints, upper-cased and zero-padded. Bare hex is never an input, so
    # int(x, 0) deliberately rejects it -- not tested as accepted.
    assert gate.norm32("0x5263642c") == "0x5263642C"
    assert gate.norm32(0x5263642C) == "0x5263642C"
    assert gate.norm32("0x1e") == "0x0000001E"


def test_usercode_from_bitstream_reads_header(gate, tmp_path):
    p = tmp_path / "x.bit"
    _bit(p, b"UserID=5263642C")
    assert gate.usercode_from_bitstream(str(p)) == "0x5263642C"


def test_usercode_from_bitstream_raises_on_no_token(gate, tmp_path):
    p = tmp_path / "bad.bit"
    _bit(p, None)
    with pytest.raises(ValueError):
        gate.usercode_from_bitstream(str(p))


def test_parse_board_image_tcl_ignores_the_env_override(gate, tmp_path):
    _board_image_tcl(tmp_path, str(tmp_path / "img.bit"), GOOD)
    bit, uc = gate.parse_board_image_tcl(
        str(tmp_path / "src/linux_soc/hw/board_scripts/board_image.tcl"))
    assert bit == str(tmp_path / "img.bit")     # the literal default, not the $env line
    assert uc == GOOD


# --- end-to-end via main() ----------------------------------------------------

def test_matching_triple_passes(gate, tmp_path, capsys):
    """image UserID == BIT_USERCODE == every overlay static_usercode -> exit 0."""
    img = tmp_path / "config_rm_greybox.bit"
    _bit(img, b"UserID=5263642C")
    _board_image_tcl(tmp_path, str(img), GOOD)
    _overlay(tmp_path, "greybox", GOOD)
    _overlay(tmp_path, "led", GOOD)
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK image/overlay match" in out
    assert "2 overlay(s) agree" in out


def test_overlay_mismatch_fails_and_is_named(gate, tmp_path, capsys):
    """One overlay built against a different impl -> exit 1, named, config warning."""
    img = tmp_path / "config_rm_greybox.bit"
    _bit(img, b"UserID=5263642C")
    _board_image_tcl(tmp_path, str(img), GOOD)
    _overlay(tmp_path, "greybox", GOOD)
    _overlay(tmp_path, "led", OTHER)     # wrong impl
    rc = _run(gate, tmp_path)
    err = capsys.readouterr().err
    assert rc == 1
    assert "led/manifest.json" in err
    assert "DIFFERENT implementation" in err


def test_declared_usercode_drift_fails(gate, tmp_path, capsys):
    """board_image.tcl's BIT_USERCODE disagreeing with the actual image -> exit 1."""
    img = tmp_path / "config_rm_greybox.bit"
    _bit(img, b"UserID=5263642C")
    _board_image_tcl(tmp_path, str(img), OTHER)   # declared != actual
    _overlay(tmp_path, "greybox", GOOD)
    rc = _run(gate, tmp_path)
    err = capsys.readouterr().err
    assert rc == 1
    assert "BIT_USERCODE" in err


def test_unstamped_overlay_notes_but_passes(gate, tmp_path, capsys):
    """A legacy overlay with no static_usercode is reported as NOT verified,
    never silently counted as a pass, but does not fail the gate."""
    img = tmp_path / "config_rm_greybox.bit"
    _bit(img, b"UserID=5263642C")
    _board_image_tcl(tmp_path, str(img), GOOD)
    _overlay(tmp_path, "greybox", GOOD)
    _overlay(tmp_path, "led", None)      # no static_usercode
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT verified" in out
    assert "led/manifest.json" in out
    assert "1 overlay(s) agree" in out   # only the stamped one counted


def test_no_board_image_tcl_skips(gate, tmp_path, capsys):
    """No board_image.tcl -> clean SKIP (exit 0)."""
    _overlay(tmp_path, "greybox", GOOD)
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and "SKIP" in out


def test_no_bitstream_skips(gate, tmp_path, capsys):
    """board_image.tcl points at a .bit that isn't present -> SKIP (fresh worktree)."""
    _board_image_tcl(tmp_path, str(tmp_path / "absent.bit"), GOOD)
    _overlay(tmp_path, "greybox", GOOD)
    rc = _run(gate, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and "SKIP" in out


def test_unidentifiable_image_fails_closed(gate, tmp_path, capsys):
    """A .bit present but with no UserID token cannot be certified -> exit 1,
    a clean verdict (not a traceback)."""
    img = tmp_path / "malformed.bit"
    _bit(img, None)                      # no UserID token
    _board_image_tcl(tmp_path, str(img), GOOD)
    _overlay(tmp_path, "greybox", GOOD)
    rc = _run(gate, tmp_path)
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot read UserID" in err
