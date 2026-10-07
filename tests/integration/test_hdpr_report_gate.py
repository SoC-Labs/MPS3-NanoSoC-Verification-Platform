"""check_hdpr_reports.py: the DFX DRC reports as a gate (handover §4.7 item 5).

Synthetic reports in report_drc's own layout; the then-fielded 0x3F1A560F reports
(read-only, only on the workstation that holds that scratch tree) are the
passing fixture when present. Each failing case is a control seen to fail.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "harness_gates" / "check_hdpr_reports.py"
#: The then-fielded mint's prod tree (gitignored build output). MPS3_FIELDED_PROD
#: points elsewhere; absent => the test below skips.
FIELDED_PROD = Path(os.environ.get("MPS3_FIELDED_PROD")
                    or REPO / "fpga" / "dfx" / "build_mint_2026_09" / "prod")

HEAD = """Report DRC

Table of Contents
-----------------
1. REPORT SUMMARY
2. REPORT DETAILS

1. REPORT SUMMARY
-----------------
            Netlist: netlist
             Max violations: <unlimited>
             Violations found: %d
"""


def report(rows):
    total = sum(n for _, _, n in rows)
    text = HEAD % total
    text += "+-----------+------------------+-------------------+------------+\n"
    text += "| Rule      | Severity         | Description       | Violations |\n"
    text += "+-----------+------------------+-------------------+------------+\n"
    for rule, sev, n in rows:
        text += "| %-9s | %-16s | some description  | %-10d |\n" % (rule, sev, n)
    text += "+-----------+------------------+-------------------+------------+\n\n2. REPORT DETAILS\n"
    return text


def run(*args):
    return subprocess.run([sys.executable, str(GATE)] + [str(a) for a in args],
                          capture_output=True, text=True)


def write_pair(d, rm, prelink_rows, routed_rows):
    (d / ("drc_hdpr_%s_prelink.rpt" % rm)).write_text(report(prelink_rows))
    (d / ("drc_%s.rpt" % rm)).write_text(report(routed_rows))


def test_clean_and_warning_only_reports_pass(tmp_path):
    write_pair(tmp_path, "rm_dbg_demo", [],
               [("RTSTAT-10", "Warning", 1), ("PDCN-1569", "Warning", 3),
                ("CFGBVS-1", "Warning", 1), ("NSTD-1", "Critical Warning", 1)])
    r = run("--build-dir", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DFX_HDPR_GATE_OK reports=2" in r.stdout


@pytest.mark.parametrize("rule", ["HDPR-16", "HDPR-18", "HDPR-50"])
@pytest.mark.parametrize("sev", ["Error", "Critical Warning", "Warning", "Advisory"])
def test_control_fatal_hdpr_rule_fails_at_any_severity(tmp_path, rule, sev):
    write_pair(tmp_path, "rm_dbg_demo", [(rule, sev, 1)], [])
    r = run("--build-dir", tmp_path, "--rm", "rm_dbg_demo", "--stage", "prelink")
    assert r.returncode == 1, r.stdout
    assert "DFX_HDPR_GATE_FAILED" in r.stdout and rule in r.stdout


def test_control_any_drc_error_fails(tmp_path):
    write_pair(tmp_path, "rm_led", [], [("UTLZ-1", "Error", 2)])
    r = run("--build-dir", tmp_path, "--rm", "rm_led", "--stage", "routed")
    assert r.returncode == 1
    assert "UTLZ-1 is a DRC Error" in r.stdout, r.stdout


def test_control_missing_report_fails(tmp_path):
    r = run("--build-dir", tmp_path, "--rm", "rm_led")
    assert r.returncode == 1
    assert "MISSING" in r.stdout


def test_control_unparseable_report_fails(tmp_path):
    (tmp_path / "junk.rpt").write_text("ERROR: [Common 17-39] 'report_drc' failed\n")
    r = run(tmp_path / "junk.rpt")
    assert r.returncode == 1
    assert "not a report_drc output" in r.stdout


def test_control_row_count_disagreement_fails(tmp_path):
    text = report([("HDPR-3", "Warning", 1)]).replace("Violations found: 1",
                                                      "Violations found: 4")
    (tmp_path / "drc_rm_x.rpt").write_text(text)
    r = run(tmp_path / "drc_rm_x.rpt")
    assert r.returncode == 1
    assert "format not understood" in r.stdout


def test_nothing_to_check_is_a_usage_error(tmp_path):
    assert run("--build-dir", tmp_path).returncode == 2


@pytest.mark.skipif(not FIELDED_PROD.is_dir(), reason="then-fielded 0x3F1A560F prod tree not on this host")
def test_fielded_reports_pass():
    r = run("--build-dir", FIELDED_PROD)
    assert r.returncode == 0, r.stdout
    assert "DFX_HDPR_GATE_OK reports=22" in r.stdout


def test_build_dfx_calls_the_gate_after_both_reports():
    src = (REPO / "fpga" / "dfx" / "build_dfx.tcl").read_text()
    pre = src.index("drc_hdpr_${rm_key}_prelink.rpt")
    assert src.index("hdpr_report_gate $repo_root $out_dir $rm_key prelink") > pre
    assert src.index("hdpr_report_gate $repo_root $out_dir $rm_key prelink") < src.index("\n    opt_design\n")
    post = src.index("-file $out_dir/drc_${rm_key}.rpt")
    assert src.index("hdpr_report_gate $repo_root $out_dir $rm_key routed") > post
