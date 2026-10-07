"""The per-RM partial .ltx: the declared-vs-produced gate, the sidecar that binds
an .ltx to its partial, and `make overlays` carrying it into the overlay.

No Vivado, no board. The .ltx fixtures have the shape Vivado 2024.1's
`write_debug_probes -cell` writes (JSON, ltx_root.ltx_data[].debug_cores[]),
cut down from the ILA spike's rm_partial.ltx (handover Appendix A).

Every gate here has a control seen to fail:
  * a debug-1 RM with no .ltx                     -> DFX_LTX_GATE_FAILED
  * a debug-0 RM WITH an .ltx                     -> DFX_LTX_GATE_FAILED
  * an .ltx naming a core outside u_rp_dut         -> DFX_LTX_GATE_FAILED
  * an ILA uuid report_debug_core does not know    -> DFX_LTX_GATE_FAILED
  * a partial replaced after the sidecar was taken -> `make overlays` refuses
  * two debug RMs with the same ILA uuid (one run, add-rm, or --check)
                                                   -> DFX_LTX_GATE_FAILED
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DFX = REPO / "fpga" / "dfx"
SIDECAR = DFX / "tools" / "ltx_sidecar.py"
GEN = DFX / "gen_manifest.py"
MAKE = shutil.which("make") or "make"
TCLSH = shutil.which("tclsh")
PB = "pblock_rp_dut"
UUID = "8214FADECDD55F8DA8EF939C8386B49F"
UUID2 = "B18B3F67433359B4A1D657D53DA82FC9"


def ltx_doc(prefix="u_rp_dut", uuid=UUID, cell="u_rp_dut", ila="u_ila_dbg"):
    return {"ltx_root": {"version": 4, "minor": 0, "ltx_data": [{
        "name": "EDA_PROBESET", "active": True, "cellName": cell,
        "debug_cores": [
            {"type": "XSDB_V3", "name": prefix + "/u_hub/inst/xsdbm",
             "spec": "labtools_xsdbm_v3", "reconfigTop": cell},
            {"type": "ILA_V3", "name": prefix + "/" + ila, "spec": "labtools_ila_v6",
             "ipName": "ila", "reconfigTop": cell, "uuid": uuid,
             "pins": [{"name": "probe0", "id": 0, "type": "DATA_TRIGGER",
                       "nets": [{"name": prefix + "/cnt", "isBus": True}]}]},
        ]}]}}


def report(uuid=UUID, ila="u_ila_dbg"):
    return ("UUID of Debug Core \"%s\"\n+----------------------------------+\n" % ila +
            "| UUID                             |\n+----------------------------------+\n"
            "| %s |\n+----------------------------------+\n" % uuid)


def make_prod(tmp, rms, ltx_for=(), uuids=None, **ltx_kw):
    """A prod dir with a partial pair per RM and an .ltx (+ report) for ltx_for.
    uuids = {rm_key: uuid} overrides the ILA uuid per RM (default UUID for all)."""
    prod = tmp / "build" / "prod"
    prod.mkdir(parents=True)
    (prod / "static_id.txt").write_text("0xDEADBEEF\n")
    rows = ["# rm_key rm_name rm_id partial_bin clearing_bin"]
    for i, (key, name) in enumerate(rms):
        p = "config_%s_%s_partial.bin" % (key, PB)
        c = "config_%s_%s_partial_clear.bin" % (key, PB)
        (prod / p).write_bytes(bytes([i + 1]) * 64)
        (prod / c).write_bytes(bytes([i + 0x41]) * 32)
        rows.append("%s %s 0x0100000%d %s %s" % (key, name, i, p, c))
    (prod / "overlay_inputs.txt").write_text("\n".join(rows) + "\n")
    default = ltx_kw.pop("uuid", UUID)
    for key in ltx_for:
        write_ltx(prod, key, (uuids or {}).get(key, default), **ltx_kw)
    return prod


def write_ltx(prod, key, uuid, **ltx_kw):
    ila = "u_ila_" + key[3:]
    (prod / ("config_%s.ltx" % key)).write_text(
        json.dumps(ltx_doc(uuid=uuid, ila=ila, **ltx_kw), indent=4))
    (prod / ("debug_core_%s.rpt" % key)).write_text(report(uuid, ila))


def gate(prod, rm_keys, debug_keys, check=False):
    cmd = [sys.executable, str(SIDECAR), "gate", "--build-dir", str(prod),
           "--rm-keys", " ".join(rm_keys), "--debug-keys", " ".join(debug_keys)]
    if check:
        cmd.append("--check")
    return subprocess.run(cmd, capture_output=True, text=True)


def test_debug_rm_with_ltx_passes_and_writes_sidecar(tmp_path):
    prod = make_prod(tmp_path, [("rm_greybox", "greybox"), ("rm_dbg", "dbg")],
                     ltx_for=["rm_dbg"])
    r = gate(prod, ["rm_greybox", "rm_dbg"], ["rm_dbg"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DFX_LTX_GATE_OK" in r.stdout
    side = json.loads((prod / "config_rm_dbg.ltx.json").read_text())
    assert side["rm_key"] == "rm_dbg"
    assert side["ila_uuids"] == [UUID]
    assert side["partial"] == "config_rm_dbg_%s_partial.bin" % PB
    r2 = gate(prod, ["rm_greybox", "rm_dbg"], ["rm_dbg"], check=True)
    assert r2.returncode == 0, r2.stdout


def test_control_declared_debug_without_ltx_fails(tmp_path):
    prod = make_prod(tmp_path, [("rm_greybox", "greybox"), ("rm_dbg", "dbg")])
    r = gate(prod, ["rm_greybox", "rm_dbg"], ["rm_dbg"])
    assert r.returncode == 1
    assert "DFX_LTX_GATE_FAILED rm_dbg is declared debug 1" in r.stdout, r.stdout


def test_control_undeclared_rm_with_ltx_fails(tmp_path):
    prod = make_prod(tmp_path, [("rm_greybox", "greybox"), ("rm_led", "led")],
                     ltx_for=["rm_led"])
    r = gate(prod, ["rm_greybox", "rm_led"], [])
    assert r.returncode == 1
    assert "DFX_LTX_GATE_FAILED rm_led is NOT declared debug 1" in r.stdout, r.stdout


def test_control_core_outside_the_rp_fails(tmp_path):
    prod = make_prod(tmp_path, [("rm_dbg", "dbg")], ltx_for=["rm_dbg"],
                     prefix="u_shell/shell_bd_i")
    r = gate(prod, ["rm_dbg"], ["rm_dbg"])
    assert r.returncode == 1
    assert "OUTSIDE u_rp_dut" in r.stdout, r.stdout


def test_control_uuid_unknown_to_report_debug_core_fails(tmp_path):
    prod = make_prod(tmp_path, [("rm_dbg", "dbg")], ltx_for=["rm_dbg"])
    (prod / "debug_core_rm_dbg.rpt").write_text(report("0" * 32))
    r = gate(prod, ["rm_dbg"], ["rm_dbg"])
    assert r.returncode == 1
    assert "NOT in report_debug_core" in r.stdout, r.stdout


def test_debug_rms_with_distinct_uuids_pass(tmp_path):
    rms = [("rm_greybox", "greybox"), ("rm_dbg_demo", "dbg_demo"), ("rm_nanosoc_ila", "nanosoc_ila")]
    prod = make_prod(tmp_path, rms, ltx_for=["rm_dbg_demo", "rm_nanosoc_ila"],
                     uuids={"rm_nanosoc_ila": UUID2})
    # the static's sidecar is not an RM: a uuid in it never collides
    (prod / "config_rm_greybox_static.ltx.json").write_text(
        json.dumps({"rm_key": "rm_greybox", "ila_uuids": [UUID], "uuids": [UUID]}))
    keys = [k for k, _ in rms]
    r = gate(prod, keys, ["rm_dbg_demo", "rm_nanosoc_ila"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (prod / "config_rm_dbg_demo.ltx.json").is_file()
    assert (prod / "config_rm_nanosoc_ila.ltx.json").is_file()
    assert gate(prod, keys, ["rm_dbg_demo", "rm_nanosoc_ila"], check=True).returncode == 0


def test_control_two_debug_rms_sharing_a_uuid_fail_and_get_no_sidecar(tmp_path):
    """The 2026-09-23 ILA mint's shape: dbg_demo and nanosoc_ila, one uuid."""
    rms = [("rm_greybox", "greybox"), ("rm_dbg_demo", "dbg_demo"), ("rm_nanosoc_ila", "nanosoc_ila")]
    prod = make_prod(tmp_path, rms, ltx_for=["rm_dbg_demo", "rm_nanosoc_ila"], uuid=UUID2)
    r = gate(prod, [k for k, _ in rms], ["rm_dbg_demo", "rm_nanosoc_ila"])
    assert r.returncode == 1
    assert ("DFX_LTX_GATE_FAILED ILA uuid %s is shared by debug RMs rm_dbg_demo, rm_nanosoc_ila"
            % UUID2) in r.stdout, r.stdout
    assert not list(prod.glob("config_rm_*.ltx.json")), "a refused RM must get no sidecar"


def test_control_add_rm_sharing_an_earlier_rms_uuid_fails(tmp_path):
    """add-rm gates ONE key; the RMs already in the build dir still count."""
    rms = [("rm_greybox", "greybox"), ("rm_dbg_demo", "dbg_demo"), ("rm_nanosoc_ila", "nanosoc_ila")]
    prod = make_prod(tmp_path, rms, ltx_for=["rm_dbg_demo"], uuid=UUID2)
    debug = ["rm_dbg_demo", "rm_nanosoc_ila"]
    assert gate(prod, ["rm_greybox", "rm_dbg_demo"], debug).returncode == 0
    before = (prod / "config_rm_dbg_demo.ltx.json").read_bytes()
    write_ltx(prod, "rm_nanosoc_ila", UUID2)
    r = gate(prod, ["rm_nanosoc_ila"], debug)
    assert r.returncode == 1
    assert "shared by debug RMs rm_dbg_demo, rm_nanosoc_ila" in r.stdout, r.stdout
    assert not (prod / "config_rm_nanosoc_ila.ltx.json").exists()
    assert (prod / "config_rm_dbg_demo.ltx.json").read_bytes() == before
    # ...and re-adding the SAME rm_key (a rebuild) never collides with itself
    assert gate(prod, ["rm_dbg_demo"], debug).returncode == 0


def test_control_check_refuses_a_pre_rule_prod_tree_with_a_shared_uuid(tmp_path):
    """--check (what `make overlays` runs) over a tree gated before this rule."""
    rms = [("rm_dbg_demo", "dbg_demo"), ("rm_nanosoc_ila", "nanosoc_ila")]
    prod = make_prod(tmp_path, rms, ltx_for=["rm_dbg_demo", "rm_nanosoc_ila"], uuid=UUID2)
    for key in ("rm_dbg_demo", "rm_nanosoc_ila"):   # the sidecars the old gate wrote
        ltx = prod / ("config_%s.ltx" % key)
        part = prod / ("config_%s_%s_partial.bin" % (key, PB))
        side = {"schema": 1, "rm_key": key, "rp_inst": "u_rp_dut", "ltx": ltx.name,
                "ltx_crc32": "0x%08x" % (zlib.crc32(ltx.read_bytes()) & 0xFFFFFFFF),
                "partial": part.name,
                "partial_crc32": "0x%08x" % (zlib.crc32(part.read_bytes()) & 0xFFFFFFFF),
                "ila_uuids": [UUID2]}
        (prod / ("config_%s.ltx.json" % key)).write_text(json.dumps(side))
    r = gate(prod, ["rm_dbg_demo", "rm_nanosoc_ila"], ["rm_dbg_demo", "rm_nanosoc_ila"], check=True)
    assert r.returncode == 1
    assert "shared by debug RMs rm_dbg_demo, rm_nanosoc_ila" in r.stdout, r.stdout


def test_debug_rm_wrappers_name_their_ilas_distinctly():
    """Findings #6: both RMs named the ILA u_ila and shared one uuid. The RTL is
    where the name comes from; the gate above is the backstop."""
    import re
    names = {}
    for rm in ("dbg_demo", "nanosoc_ila"):
        src = (REPO / "fpga" / "rp" / rm / ("rp_%s_wrapper.sv" % rm)).read_text()
        found = re.findall(r"^\s*ila_\w+\s+(\w+)\s*\(", src, re.M)
        assert len(found) == 1, (rm, found)
        names[rm] = found[0]
    assert names["dbg_demo"] != names["nanosoc_ila"], names
    assert "u_ila" not in names.values(), names


def _overlays(prod, root):
    return subprocess.run(
        [MAKE, "-C", str(DFX), "overlays", "BUILD=%s" % prod.parent,
         "OVERLAY_ROOT=%s" % root, "DEBUG_RM_KEYS=rm_dbg"],
        capture_output=True, text=True)


def test_overlays_carries_the_ltx_and_its_uuids(tmp_path):
    prod = make_prod(tmp_path, [("rm_greybox", "greybox"), ("rm_dbg", "dbg")],
                     ltx_for=["rm_dbg"])
    assert gate(prod, ["rm_greybox", "rm_dbg"], ["rm_dbg"]).returncode == 0
    root = tmp_path / "ovl"
    r = _overlays(prod, root)
    assert r.returncode == 0, r.stdout + r.stderr
    m = json.loads((root / "dbg" / "manifest.json").read_text())
    assert m["ltx"] == "dbg.ltx"
    assert m["ltx_uuids"] == [UUID]
    assert (root / "dbg" / "dbg.ltx").is_file()
    assert "ltx" not in json.loads((root / "greybox" / "manifest.json").read_text())
    v = subprocess.run([sys.executable, str(GEN), "verify", str(root / "dbg" / "manifest.json")],
                       capture_output=True, text=True)
    assert v.returncode == 0, v.stdout + v.stderr


def test_control_overlays_refuses_a_partial_rebuilt_after_its_ltx(tmp_path):
    prod = make_prod(tmp_path, [("rm_greybox", "greybox"), ("rm_dbg", "dbg")],
                     ltx_for=["rm_dbg"])
    assert gate(prod, ["rm_greybox", "rm_dbg"], ["rm_dbg"]).returncode == 0
    (prod / ("config_rm_dbg_%s_partial.bin" % PB)).write_bytes(b"\x99" * 64)
    r = _overlays(prod, tmp_path / "ovl")
    assert r.returncode != 0
    assert "DFX_LTX_GATE_FAILED" in r.stdout and "same routed config" in r.stdout, r.stdout


@pytest.mark.skipif(TCLSH is None, reason="tclsh not installed")
def test_rm_debug_declared_defaults_to_zero():
    tcl = ('source %s\nset RM_LIB(a,debug) 1\nset RM_LIB(b,debug) 0\n'
           'puts "[rm_debug_declared a][rm_debug_declared b][rm_debug_declared c]"\n'
           'set RM_LIB(d,debug) yes\nputs [catch {rm_debug_declared d}]\n'
           % (DFX / "tools" / "debug_probes.tcl"))
    r = subprocess.run([TCLSH], input=tcl, capture_output=True, text=True)
    assert r.stdout.split() == ["100", "1"], r.stdout + r.stderr


def test_every_bitstream_site_writes_the_ltx():
    """Both full-flow sites share one call; the incremental path has its own."""
    src = (DFX / "build_dfx.tcl").read_text()
    assert "source $repo_root/fpga/dfx/tools/debug_probes.tcl" in src
    assert src.count("write_rm_debug_probes $out_dir $rp_inst $rm_key") == 2
    probes = (DFX / "tools" / "debug_probes.tcl").read_text()
    assert "write_debug_probes -force -cell $rp_inst $ltx" in probes
    assert "DFX_LTX_GATE_FAILED" in probes


def test_failure_markers_are_grepped_anchored():
    """Vivado ECHOES every sourced proc body into the log, prefixed '## ', so the
    literal `puts "DFX_LTX_GATE_FAILED ..."` is in EVERY build.log. An unanchored
    grep would fail every mint (seen in the 2026-09-23 Vivado smoke)."""
    mk = (DFX / "Makefile").read_text()
    for line in mk.splitlines():
        if "_GATE_FAILED" in line and "grep" in line:
            assert "^DFX_(" in line, "unanchored failure-marker grep:\n%s" % line
