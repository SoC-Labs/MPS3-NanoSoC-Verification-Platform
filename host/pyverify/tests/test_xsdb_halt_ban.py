"""The xsdb halt ban (pyverify.lease): no script run against a LIVE harness may
halt its CPU. On bare metal an ``xsdb stop`` has already destroyed a live
1.31 MB partial push; on the MicroBlaze V it halts the whole of Linux (SSH,
mps3-harnessd, the watchdog kick -- and then the WDOG reboots the board).

Two halves: the detector itself (with the Tcl shapes it must and must not
flag), and a scan of every xsdb script this tree points at a running board.
"""
from __future__ import annotations

import pytest

from pyverify.fielded import repo_root
from pyverify.lease import (XSDB_HALTING_COMMANDS, LeaseError, XsdbHaltError,
                            assert_xsdb_script_safe, xsdb_halting_commands)

ROOT = repo_root()


@pytest.mark.parametrize("script,expect", [
    ("stop", [(1, "stop")]),
    ("targets 3\nstop\ncon", [(2, "stop"), (3, "con")]),
    ("targets 3; rst -processor", [(1, "rst")]),
    ("if {$x} { stop }", [(1, "stop")]),
    ("set pc [stop]", [(1, "stop")]),
    ("dow -data blob.bin 0x80000000", [(1, "dow")]),
    ("bpadd -addr 0x1000", [(1, "bpadd")]),
    ("fpga -file x.bit", [(1, "fpga")]),
])
def test_detector_flags_halting_commands(script, expect):
    assert xsdb_halting_commands(script) == expect
    with pytest.raises(XsdbHaltError):
        assert_xsdb_script_safe(script)


@pytest.mark.parametrize("script", [
    "# NEVER stop / con here",
    'puts "do not stop the core"',
    "mrd -force 0x1FF00 64",
    "set stopwatch 1",                 # a word that merely STARTS with stop
    "foreach t [ta -filter {name =~ \"*Hart*\"}] { puts $t }",
    "mwr -force 0x44A90018 0x37",
])
def test_detector_leaves_read_only_scripts_alone(script):
    assert xsdb_halting_commands(script) == []
    assert_xsdb_script_safe(script)


def test_the_ban_is_a_lease_error_and_names_the_mbv_consequence():
    assert issubclass(XsdbHaltError, LeaseError)
    with pytest.raises(XsdbHaltError) as ei:
        assert_xsdb_script_safe("stop", name="probe.tcl")
    msg = str(ei.value)
    assert "probe.tcl" in msg and "Linux" in msg and "watchdog" in msg
    assert {"stop", "con", "rst", "dow"} <= XSDB_HALTING_COMMANDS


#: Every xsdb script that runs against a LIVE harness (tier-3 gates, the JTAG
#: diag reader, the touch probe). Vivado batch scripts (tier2_dcp_assert.tcl,
#: rm_netlist_check.tcl) are scanned too -- they must not grow these either.
def _live_scripts():
    gates = sorted((ROOT / "scripts" / "harness_gates").glob("*.tcl"))
    extra = [ROOT / "scripts" / "mps3_diag.tcl", ROOT / "scripts" / "mps3_touch_finger.tcl"]
    return gates + [p for p in extra if p.is_file()]


def test_the_scan_finds_the_scripts_it_is_meant_to_guard():
    names = {p.name for p in _live_scripts()}
    # a scan that finds nothing passes vacuously -- name the ones that must be here
    assert {"mps3_diag.tcl", "tier3_csr_liveness.tcl", "dut_rx_check.tcl"} <= names
    assert "tier3_csr_liveness_mbv.tcl" in names, (
        "the MicroBlaze V tier-3 variant is missing from scripts/harness_gates/")


@pytest.mark.parametrize("path", _live_scripts(), ids=lambda p: p.name)
def test_no_live_xsdb_script_halts_the_cpu(path):
    assert_xsdb_script_safe(path.read_text(), name=str(path.relative_to(ROOT)))
