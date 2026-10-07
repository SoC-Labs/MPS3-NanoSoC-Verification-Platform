"""tests/integration/test_overlay_ip_class_gate.py

`ip_class` end to end: the per-RM source (``RM_LIB(<rm>,ip_class)`` in
``fpga/dfx/rm_list.tcl``), the writer (``fpga/dfx/gen_manifest.py build``), the
reader's check (``gen_manifest.py verify``) and the gate
(``scripts/harness_gates/check_overlay_ip_class.py``).

Why it matters: the release split (Arm Academic Access overlays private,
everything else public) is only mechanical if every overlay says which it is,
and Harness Manager shows "unknown" for a manifest without the key.

Synthetic trees only, under ``tmp_path``: RM names ``demo_open`` / ``demo_arm``
and rm_ids whose design half (0x7A5x) is outside the allocated range, so no
fixture here can be mistaken for, or go stale with, a real RM. The last two
tests read the REAL tree and write nothing.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO / "scripts" / "harness_gates" / "check_overlay_ip_class.py"

RM_LIST_TCL = """\
set RM_LIB(rm_demo_open,wrapper_dir) "rtl/demo_open"
set RM_LIB(rm_demo_open,rm_name)     "demo_open"
set RM_LIB(rm_demo_open,ip_class)    "{open_cls}"
set RM_LIB(rm_demo_arm,wrapper_dir)  "rtl/demo_arm"
set RM_LIB(rm_demo_arm,rm_name)      "demo_arm"
set RM_LIB(rm_demo_arm,ip_class)     "arm-aaa"
set RM_ORDER [list rm_demo_open rm_demo_arm]
"""


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_overlay_ip_class", _GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


def _tree(root: Path, open_cls: str = "open", manifests=None, overlay="overlay") -> Path:
    """A repo-shaped tree: rm_list.tcl, both wrapper dirs, and overlay manifests.
    ``manifests`` maps rm_name -> ip_class (None = key absent); default = correct."""
    dfx = root / "fpga" / "dfx"
    dfx.mkdir(parents=True)
    (dfx / "rm_list.tcl").write_text(RM_LIST_TCL.format(open_cls=open_cls))
    for rm in ("demo_open", "demo_arm"):
        (root / "rtl" / rm).mkdir(parents=True)
        (root / "rtl" / rm / ("rp_%s_wrapper.sv" % rm)).write_text("module rp_%s_wrapper; endmodule\n" % rm)
    if manifests is None:
        manifests = {"demo_open": "open", "demo_arm": "arm-aaa"}
    for i, (name, cls) in enumerate(manifests.items()):
        d = {"schema": 1, "static_id": "0x7A57C0DE", "rm_id": "0x01007a5%d" % i,
             "rm_name": name}
        if cls is not None:
            d["ip_class"] = cls
        (dfx / overlay / name).mkdir(parents=True)
        (dfx / overlay / name / "manifest.json").write_text(json.dumps(d, indent=2) + "\n")
    return root


def _run(gate, root, capsys):
    rc = gate.main(["--repo", str(root)])
    return rc, capsys.readouterr().out


# --- the gate -------------------------------------------------------------------------

def test_clean_tree_passes(gate, tmp_path, capsys):
    rc, out = _run(gate, _tree(tmp_path), capsys)
    assert rc == 0, out
    assert "OK: 2 RM(s)" in out


def test_overlay_linux_is_gated_too(gate, tmp_path, capsys):
    rc, out = _run(gate, _tree(tmp_path, manifests={"demo_open": None}, overlay="overlay_linux"),
                   capsys)
    assert rc == 1 and "overlay_linux/demo_open/manifest.json: no top-level ip_class" in out, out


@pytest.mark.parametrize("manifests, needle", [
    ({"demo_open": None}, "no top-level ip_class"),
    ({"demo_open": "proprietary"}, "'proprietary' is not one of open|arm-aaa"),
    ({"demo_open": ["open"]}, "is not one of open|arm-aaa"),
    ({"demo_arm": "open"}, "ip_class 'open' but rm_list.tcl says 'arm-aaa'"),
    ({"demo_ghost": "open"}, "rm_name 'demo_ghost' is not registered"),
])
def test_bad_manifest_fails(gate, tmp_path, capsys, manifests, needle):
    rc, out = _run(gate, _tree(tmp_path, manifests=manifests), capsys)
    assert rc == 1 and needle in out, out


@pytest.mark.parametrize("open_cls, needle", [
    ("public", "rm_list.tcl ip_class 'public' is not one of"),
    ("", "rm_list.tcl ip_class '' is not one of"),
])
def test_bad_rm_list_value_fails(gate, tmp_path, capsys, open_cls, needle):
    rc, out = _run(gate, _tree(tmp_path, open_cls=open_cls, manifests={}), capsys)
    assert rc == 1 and needle in out, out


def test_rm_without_ip_class_fails(gate, tmp_path, capsys):
    root = _tree(tmp_path, manifests={})
    tcl = root / "fpga" / "dfx" / "rm_list.tcl"
    tcl.write_text("\n".join(l for l in tcl.read_text().splitlines()
                             if "rm_demo_arm,ip_class" not in l) + "\n")
    rc, out = _run(gate, root, capsys)
    assert rc == 1 and "rm_demo_arm: rm_list.tcl declares no ip_class" in out, out


def test_open_rm_that_names_arm_ip_trips(gate, tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "rtl" / "demo_open" / "filelist.tcl").write_text(
        'read_verilog "$::env(CMSDK_DIR)/logical/x.v"\n')
    rc, out = _run(gate, root, capsys)
    assert rc == 1 and "rtl/demo_open/filelist.tcl:1: CMSDK_DIR" in out, out


def test_tripwire_ignores_comments_and_arm_rms(gate, tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "rtl" / "demo_open" / "filelist.tcl").write_text(
        "# unlike rm_nanosoc, nothing here reads $CMSDK_DIR\nread_verilog a.sv ;# not /research/AAA\n")
    (root / "rtl" / "demo_open" / "w.sv").write_text("// no cmsdk_ahb_to_apb here\nmodule w; endmodule\n")
    (root / "rtl" / "demo_arm" / "filelist.tcl").write_text('read_verilog "$::env(CMSDK_DIR)/x.v"\n')
    rc, out = _run(gate, root, capsys)
    assert rc == 0, out


# --- gen_manifest.py: the writer and verify --------------------------------------------

def _pair(tmp_path: Path):
    (tmp_path / "p.bin").write_bytes(b"\x11" * 64)
    (tmp_path / "c.bin").write_bytes(b"\x22" * 32)


def _build(gen_manifest, tmp_path, rm_name, *extra):
    return gen_manifest.main([
        "build", "--rm-name", rm_name, "--rm-id", "0x01007a50", "--static-id", "0x7A57C0DE",
        "--partial", str(tmp_path / "p.bin"), "--clearing", str(tmp_path / "c.bin"),
        "--rm-list", str(tmp_path / "fpga" / "dfx" / "rm_list.tcl"),
        "--copy", "--out-root", str(tmp_path / "out"), *extra])


@pytest.mark.parametrize("rm_name, want", [("demo_open", "open"), ("demo_arm", "arm-aaa")])
def test_build_takes_ip_class_from_rm_list(gen_manifest, tmp_path, rm_name, want):
    _tree(tmp_path, manifests={})
    _pair(tmp_path)
    assert _build(gen_manifest, tmp_path, rm_name) == 0
    m = json.loads((tmp_path / "out" / rm_name / "manifest.json").read_text())
    assert m["ip_class"] == want
    assert list(m)[:5] == ["schema", "static_id", "rm_id", "rm_name", "ip_class"]


def test_build_refuses_an_override_that_disagrees(gen_manifest, tmp_path, capsys):
    _tree(tmp_path, manifests={})
    _pair(tmp_path)
    assert _build(gen_manifest, tmp_path, "demo_arm", "--ip-class", "open") == 1
    assert "disagrees with" in capsys.readouterr().err


def test_build_unregistered_rm_records_the_restrictive_class(gen_manifest, tmp_path, capsys):
    _tree(tmp_path, manifests={})
    _pair(tmp_path)
    assert _build(gen_manifest, tmp_path, "demo_unlisted") == 0
    assert "not registered" in capsys.readouterr().err
    m = json.loads((tmp_path / "out" / "demo_unlisted" / "manifest.json").read_text())
    assert m["ip_class"] == "arm-aaa"


def test_build_refuses_a_registered_rm_with_no_class(gen_manifest, tmp_path, capsys):
    _tree(tmp_path, open_cls="", manifests={})
    _pair(tmp_path)
    assert _build(gen_manifest, tmp_path, "demo_open") == 1
    assert "ip_class" in capsys.readouterr().err


@pytest.mark.parametrize("mutate, ok", [
    (lambda m: None, True),
    (lambda m: m.pop("ip_class"), False),
    (lambda m: m.__setitem__("ip_class", "unknown"), False),
    (lambda m: m.__setitem__("ip_class", "open"), False),      # rm_list says arm-aaa
])
def test_verify_requires_a_valid_agreeing_ip_class(gen_manifest, tmp_path, mutate, ok):
    _tree(tmp_path, manifests={})
    _pair(tmp_path)
    assert _build(gen_manifest, tmp_path, "demo_arm") == 0
    mp = tmp_path / "out" / "demo_arm" / "manifest.json"
    m = json.loads(mp.read_text())
    mutate(m)
    mp.write_text(json.dumps(m, indent=2) + "\n")
    rc = gen_manifest.main(["verify", str(mp), "--rm-list",
                            str(tmp_path / "fpga" / "dfx" / "rm_list.tcl")])
    assert (rc == 0) is ok


# --- the real tree (read-only) ---------------------------------------------------------

def test_real_tree_passes_the_gate(gate, capsys):
    rc, out = _run(gate, _REPO, capsys)
    assert rc == 0, out


def test_real_arm_rms_stay_arm_aaa(gen_manifest):
    """A licence decision, pinned: these RMs build Arm Academic Access IP into the
    partial (Cortex-M0/M0+, CMSDK, SoC-400; eth_ss via CMSDK_DIR). Flipping one to
    `open` must be a deliberate edit here too, not a one-token slip in rm_list.tcl."""
    classes = gen_manifest.rm_ip_classes()
    for name in ("nanosoc", "nanosoc_upy", "nanosoc_ila", "nanosoc_multicore",
                 "nanosoc_iice", "eth_ss"):
        assert classes.get(name) == "arm-aaa", (name, classes.get(name))
