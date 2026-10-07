"""Unit-tests for ``scripts/harness_gates/check_rm_id_encoding.py`` -- the
wrapper-reading half.

Why this file exists (2026-09-14): the RM template (``fpga/rp/_template``)
composes ``rm_id`` from three localparams --

    localparam logic [7:0]  RM_VER_MAJOR = 8'd0;
    localparam logic [7:0]  RM_VER_MINOR = 8'd1;
    localparam logic [15:0] RM_DESIGN_ID = 16'h0007;
    localparam logic [31:0] RM_ID_X = {RM_VER_MAJOR, RM_VER_MINOR, RM_DESIGN_ID};

-- and the gate only knew the flat ``32'h...`` form. Nobody noticed for as
long as no template-scaffolded RM was registered; the first one (clcd_demo)
made the gate die with "no localparam ... defines it", and because that was a
SystemExit it also hid every other finding (the IICE shim had no
``assign rm_id`` at all). These tests pin: both spellings are read and
COMPOSED by the gate (never trusted), a wrong member width is refused, and a
broken wrapper is one collected error among others, not a fatal first.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "scripts" / "harness_gates" / "check_rm_id_encoding.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_rm_id_encoding", _GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


_RM_LIST = """\
set RM_ORDER [list rm_greybox rm_flat rm_composed]
set RM_LIB(rm_greybox,design_id)   "0x0000"
set RM_LIB(rm_greybox,version)     "0.0.0"
set RM_LIB(rm_greybox,rm_name)     "greybox"
set RM_LIB(rm_greybox,wrapper_dir) "fpga/rp/greybox"
set RM_LIB(rm_greybox,top)         "rp_greybox_wrapper"
set RM_LIB(rm_flat,design_id)      "0x001E"
set RM_LIB(rm_flat,version)        "1.0.0"
set RM_LIB(rm_flat,rm_name)        "flat"
set RM_LIB(rm_flat,wrapper_dir)    "fpga/rp/flat"
set RM_LIB(rm_flat,top)            "rp_flat_wrapper"
set RM_LIB(rm_composed,design_id)  "0x0007"
set RM_LIB(rm_composed,version)    "0.1.0"
set RM_LIB(rm_composed,rm_name)    "composed"
set RM_LIB(rm_composed,wrapper_dir) "fpga/rp/composed"
set RM_LIB(rm_composed,top)        "rp_composed_wrapper"
"""

_GREYBOX = "localparam [31:0] RM_ID_GREYBOX = 32'h0000_0000;\nassign rm_id = RM_ID_GREYBOX;\n"
_FLAT = "localparam logic [31:0] RM_ID_FLAT = 32'h0100001E;\nassign rm_id = RM_ID_FLAT;\n"


def _composed(major="8'd0", minor="8'd1", design="16'h0007") -> str:
    return (
        f"  localparam logic [7:0]  RM_VER_MAJOR = {major};\n"
        f"  localparam logic [7:0]  RM_VER_MINOR = {minor};\n"
        f"  localparam logic [15:0] RM_DESIGN_ID = {design};\n"
        "  localparam logic [31:0] RM_ID_COMPOSED =\n"
        "      {RM_VER_MAJOR, RM_VER_MINOR, RM_DESIGN_ID};\n"
        "  assign rm_id = RM_ID_COMPOSED;\n"
    )


def _tree(tmp_path: Path, composed_sv: str, flat_sv: str = _FLAT) -> Path:
    (tmp_path / "fpga/dfx").mkdir(parents=True)
    (tmp_path / "fpga/dfx/rm_list.tcl").write_text(_RM_LIST)
    for d, top, body in (("greybox", "rp_greybox_wrapper", _GREYBOX),
                         ("flat", "rp_flat_wrapper", flat_sv),
                         ("composed", "rp_composed_wrapper", composed_sv)):
        (tmp_path / "fpga/rp" / d).mkdir(parents=True)
        (tmp_path / "fpga/rp" / d / f"{top}.sv").write_text(body)
    return tmp_path


def _run(gate, tree: Path, monkeypatch) -> int:
    # The tclsh cross-check sources the REAL rm_list.tcl's procs; a fixture
    # registry has none, so keep that leg out of these unit tests.
    monkeypatch.setattr(gate, "tcl_rm_ids", lambda _p: (None, None))
    monkeypatch.setattr(sys, "argv", ["check_rm_id_encoding.py", "--repo", str(tree)])
    return gate.main()


def test_composed_form_is_read_and_agrees(gate, tmp_path, monkeypatch, capsys):
    rc = _run(gate, _tree(tmp_path, _composed()), monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "rm_composed" in out and "0x00010007" in out


def test_composed_form_is_composed_not_trusted(gate, tmp_path, monkeypatch, capsys):
    """The template's 1.0 default against a registry saying 0.1.0 is WRAPPER
    DRIFT, exactly as a wrong flat literal would be (the clcd_demo case)."""
    rc = _run(gate, _tree(tmp_path, _composed(major="8'd1", minor="8'd0")), monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1
    assert "WRAPPER DRIFT" in out and "0x01000007" in out and "0x00010007" in out


def test_composed_member_of_wrong_width_is_refused(gate, tmp_path, monkeypatch, capsys):
    rc = _run(gate, _tree(tmp_path, _composed(design="8'h07")), monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1
    assert "8 bits wide" in out and "16 bits" in out


def test_unreadable_wrapper_does_not_hide_other_findings(gate, tmp_path, monkeypatch, capsys):
    """Before 2026-09-14 an unparseable wrapper was a SystemExit on first sight,
    so one bad wrapper hid every other RM's problem."""
    no_assign = "localparam logic [31:0] RM_ID_COMPOSED = 32'h00010007;\n"  # never assigned
    wrong_flat = "localparam logic [31:0] RM_ID_FLAT = 32'h0100_0099;\nassign rm_id = RM_ID_FLAT;\n"
    rc = _run(gate, _tree(tmp_path, no_assign, flat_sv=wrong_flat), monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1
    assert "no `assign rm_id" in out           # the unreadable one is reported...
    assert "rm_flat: WRAPPER DRIFT" in out     # ...and so is the other RM's drift
    assert "UNREADABLE" in out


def test_real_registry_and_wrappers_agree(gate, monkeypatch, capsys):
    """The gate against the real tree (tclsh leg included if tclsh exists)."""
    monkeypatch.setattr(sys, "argv", ["check_rm_id_encoding.py", "--repo", str(_REPO_ROOT)])
    rc = gate.main()
    assert rc == 0, capsys.readouterr().out
