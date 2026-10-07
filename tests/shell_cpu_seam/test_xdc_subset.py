"""tests/shell_cpu_seam/test_xdc_subset.py

No XDC in the tree may contain a Tcl command Vivado drops.

An XDC is not a Tcl script. Vivado supports a subset and DROPS any other command
-- `if`, `catch`, `puts`, `foreach` -- WITH ITS WHOLE BODY, under a CRITICAL
WARNING [Designutils 20-1307], then closes timing without it. The CLCD pad
window, the user-microSD pad window + MISO multicycle and the entire QSPI pad
model all sat inside `if`s in mps3_harness_timing.xdc and so never applied to
any shell, fielded ones included (FLOW, RC1 shell impl, 2026-09-24).

This is the board-free half of the gate; fpga/shell/build_shell.tcl runs the
same scanner (fpga/shell/tools/xdc_gate.tcl) before synth and checks the run
logs for 20-1307 after. The command list is READ from that Tcl file, so the two
cannot drift apart.

Scope: every tracked-or-new .xdc under fpga/ -- a file that some flow reads as
constraints. A file that is instead `source`d as a Tcl script may use full Tcl;
those are listed in SOURCED_AS_TCL with the script that sources them, and the
test checks that claim both ways (it IS sourced; nothing reads it as an XDC).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE_TCL = REPO / "fpga" / "shell" / "tools" / "xdc_gate.tcl"

#: .xdc files that are `source`d as Tcl, never read as constraints -> the script
#: that sources each. Full Tcl is legal there (they build pblocks in loops).
SOURCED_AS_TCL = {
    "fpga/dfx/dfx_floorplan.xdc": "fpga/dfx/build_dfx.tcl",
    "fpga/shell/constraints/optional/services_pblock_proposal.xdc": "tests/services_rp/prove_pblock.tcl",
}
#: Retired trees nothing builds (the July Linux fork, deleted with the fork).
RETIRED_PREFIXES = ("src/linux_harness/impl/",)


def forbidden() -> list[str]:
    m = re.search(r"^set ::SOCLABS_XDC_FORBIDDEN \{([^}]*)\}", GATE_TCL.read_text(), re.M)
    assert m, "xdc_gate.tcl no longer declares ::SOCLABS_XDC_FORBIDDEN"
    return m.group(1).split()


def scan(text: str, name: str = "x.xdc") -> list[str]:
    """Python twin of soclabs_xdc_scan: `file:line: cmd` per forbidden command."""
    bad, cont, words = [], False, set(forbidden())
    for n, line in enumerate(text.splitlines(), 1):
        was_cont, cont = cont, bool(re.search(r"\\\s*$", line))
        if was_cont:
            continue
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        for seg in t.split(";"):
            seg = seg.strip().lstrip("}").strip()
            if not seg or seg.startswith("#"):
                continue
            m = re.match(r"[A-Za-z_:]+", seg)
            if m and m.group(0) in words:
                bad.append(f"{name}:{n}: {m.group(0)}")
    return bad


def xdc_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                          "*.xdc"], cwd=REPO, capture_output=True, text=True)
    files = [f for f in out.stdout.split() if f.startswith("fpga/")]
    return sorted(f for f in files if (REPO / f).is_file())


def test_the_forbidden_list_is_real():
    words = forbidden()
    for w in ("if", "else", "catch", "puts", "foreach", "proc"):
        assert w in words


def test_scan_finds_the_files_it_should():
    files = xdc_files()
    assert "fpga/shell/constraints/mps3_harness_timing.xdc" in files
    assert "fpga/shell/constraints_realphy/mps3_realphy_timing.xdc" in files
    assert len(files) >= 10, files


def test_no_constraint_xdc_uses_tcl_vivado_drops():
    bad = []
    for f in xdc_files():
        if f in SOURCED_AS_TCL or f.startswith(RETIRED_PREFIXES):
            continue
        bad += scan((REPO / f).read_text(), f)
    assert not bad, ("XDC command(s) Vivado would DROP with their bodies "
                     "(CRITICAL WARNING Designutils 20-1307):\n  " + "\n  ".join(bad))


@pytest.mark.parametrize("xdc,script", sorted(SOURCED_AS_TCL.items()))
def test_sourced_exemptions_are_really_sourced_and_never_read_as_xdc(xdc, script):
    base = Path(xdc).name
    assert re.search(rf"\bsource\b[^\n]*{re.escape(base)}|\$xdc\b",
                     (REPO / script).read_text()), f"{script} does not source {xdc}"
    hits = subprocess.run(["git", "grep", "-nE", rf"(read_xdc|add_files)[^\n]*{re.escape(base)}"],
                          cwd=REPO, capture_output=True, text=True).stdout
    assert not hits.strip(), f"{xdc} is exempt as sourced Tcl, but is read as an XDC:\n{hits}"


def test_control_the_scanner_sees_what_vivado_drops():
    bad_xdc = ("set p [get_ports a] ;# an if in a comment is fine\n"
               "if {[llength $p] == 0} {\n"
               "    puts \"none\"\n"
               "} else {\n"
               "    set_false_path -from $p\n"
               "}\n"
               "set_property A B \\\n"
               "    if_is_an_argument_here\n"
               "catch {create_clock -period 10 [get_ports c]}\n")
    got = [h.split(": ")[1] for h in scan(bad_xdc)]
    assert got == ["if", "puts", "else", "catch"], got


def test_control_the_old_timing_xdc_would_have_failed():
    """The committed pre-fix file (HEAD~ of this change is not reachable in a
    depth-1 CI clone, so the shape is reproduced): the CLCD block as it was."""
    old_clcd = ("set clcd_strobed [get_ports -quiet {CLCD_PD[*] CLCD_RS CLCD_CS CLCD_WR_SCL}]\n"
                "if {[llength $clcd_strobed] == 0} {\n"
                "    puts \"WARNING: no CLCD ports\"\n"
                "} else {\n"
                "    set_output_delay -clock $clk_shell -max  8.0 $clcd_strobed\n"
                "    set_output_delay -clock $clk_shell -min -2.0 $clcd_strobed\n"
                "}\n")
    assert scan(old_clcd), "the scanner passed the block Vivado dropped on every shell"
