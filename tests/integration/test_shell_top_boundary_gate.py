"""check_shell_top_boundary.py: shell_top.sv's u_rp_dut map and shell_bd.tcl's
rp_* ports against fpga/shell/boundary.yaml. The real tree must pass; each
mutated copy is a control that must fail."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "harness_gates" / "check_shell_top_boundary.py"
SHELL_TOP = REPO / "fpga" / "shell" / "shell_top.sv"
BD = REPO / "fpga" / "shell" / "bd" / "shell_bd.tcl"

pytest.importorskip("yaml")


def run(shell_top=SHELL_TOP, bd=BD):
    return subprocess.run([sys.executable, str(GATE), "--repo", str(REPO),
                           "--shell-top", str(shell_top), "--bd", str(bd)],
                          capture_output=True, text=True)


def test_real_tree_passes():
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_control_port_dropped_from_shell_top_fails(tmp_path):
    text = SHELL_TOP.read_text()
    mutated, n = re.subn(r"\n\s*\.dbg_bscan_tdo\s*\([^)]*\),?", "\n", text, count=1)
    assert n == 1, "fixture: .dbg_bscan_tdo not found in shell_top.sv"
    f = tmp_path / "shell_top.sv"
    f.write_text(mutated)
    r = run(shell_top=f)
    assert r.returncode == 1, r.stdout
    assert "MISSING .dbg_bscan_tdo" in r.stdout, r.stdout


def test_control_extra_port_in_shell_top_fails(tmp_path):
    text = SHELL_TOP.read_text().replace(
        "rp_dut u_rp_dut (", "rp_dut u_rp_dut (\n    .dbg_extra (1'b0),", 1)
    f = tmp_path / "shell_top.sv"
    f.write_text(text)
    r = run(shell_top=f)
    assert r.returncode == 1
    assert ".dbg_extra, which boundary.yaml does NOT declare" in r.stdout, r.stdout


def test_control_bd_port_dropped_fails(tmp_path):
    text = BD.read_text()
    mutated, n = re.subn(r"\n\s*create_bd_port -dir I rp_dbg_bscan_tdo\s*\n", "\n", text, count=1)
    assert n == 1, "fixture: rp_dbg_bscan_tdo not in shell_bd.tcl"
    f = tmp_path / "shell_bd.tcl"
    f.write_text(mutated)
    r = run(bd=f)
    assert r.returncode == 1
    assert "rp_dbg_bscan_tdo" in r.stdout, r.stdout


def test_control_bd_direction_flipped_fails(tmp_path):
    text = BD.read_text().replace("create_bd_port -dir O rp_dbg_bscan_tck",
                                  "create_bd_port -dir I rp_dbg_bscan_tck", 1)
    f = tmp_path / "shell_bd.tcl"
    f.write_text(text)
    r = run(bd=f)
    assert r.returncode == 1
    assert "rp_dbg_bscan_tck is -dir I, boundary.yaml says O" in r.stdout, r.stdout


def test_control_bd_width_changed_fails(tmp_path):
    text = BD.read_text().replace("-from 15 -to 0 rp_dut_gpio_o", "-from 7 -to 0 rp_dut_gpio_o", 1)
    f = tmp_path / "shell_bd.tcl"
    f.write_text(text)
    r = run(bd=f)
    assert r.returncode == 1
    assert "rp_dut_gpio_o is 8 bit(s) wide, boundary.yaml says 16" in r.stdout, r.stdout
