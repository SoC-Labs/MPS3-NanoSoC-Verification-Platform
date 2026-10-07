"""scripts/mps3_recover.sh picks THE MPS3's JTAG target on a SHARED hw_server.

B1 on the real hub (2026-09-24) listed seven targets, two of them "ours":
``.../xilinx_tcf/Digilent/210249B86C47`` (the cable) and
``.../xilinx_tcf/Xilinx/jsn-JTAG-HS2-210249B86C47-1390d093-0`` (a port hosted by
our FPGA: device 0's JTAG context, IDCODE 0x1390D093 = the XCKU115). The old
script took the FIRST match. Listed Digilent-first it worked by luck; listed the
other way it opened the FPGA's own port (no xcku115 there: Vivado fails), and
with two DIFFERENT boards matching it programmed whichever came first.

The Tcl the script generates runs in plain tclsh over a STUB Vivado that models
the shared hw_server (``STUB_TARGETS`` in listing order; a jsn port carries no
xcku115 unless ``STUB_DEVS`` says so) and logs what was opened and programmed.
``MPS3_RECOVER_UNDER_TEST`` points the suite at another copy of the script (the
negative control: the pre-fix script fails cases 2, 3 and 4).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_RECOVER = Path(os.environ.get("MPS3_RECOVER_UNDER_TEST", _REPO / "scripts" / "mps3_recover.sh"))

pytestmark = pytest.mark.skipif(shutil.which("tclsh") is None, reason="no tclsh")

SERIAL = "210249B86C47"
H = "hub.example:3121/xilinx_tcf"
DIG = f"{H}/Digilent/{SERIAL}"
JSN = f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}-1390d093-0"
#: the hub's listing in B1's evidence (b1_a_program.txt), in its order
HUB = [DIG, f"{H}/Xilinx/Z2_01_TULA", f"{H}/Xilinx/XFL1EAUJ5SPOA", f"{H}/Xilinx/XFL1MHS3ZB1PA",
       f"{H}/Xilinx/Z2_02_TULA", f"{H}/Xilinx/Z2_04_TULA", JSN]

_STUBS = r'''
set ::opened ""
proc log {args} { set f [open $::env(STUB_LOG) a]; puts $f [join $args]; close $f }
proc open_hw_manager {args} {}
proc connect_hw_server {args} {}
proc get_hw_targets {args} {
    set i [lsearch -exact $args -filter]
    if {$i < 0} { return $::env(STUB_TARGETS) }
    if {![regexp {^NAME =~ (.*)$} [lindex $args [expr {$i + 1}]] -> glob]} { error "stub: filter" }
    set out {}
    foreach t $::env(STUB_TARGETS) { if {[string match $glob $t]} { lappend out $t } }
    return $out
}
proc open_hw_target {args} {
    set t [lindex $args end]
    if {$t eq "" || [string match -* $t]} { set t [lindex $::env(STUB_TARGETS) 0] }
    if {[lsearch -exact $::env(STUB_TARGETS) $t] < 0} { error "no such target $t" }
    set ::opened $t
    log open $t
}
# a jsn port is hosted BY the FPGA: no xcku115 behind it; any other target: xcku115_0
proc get_hw_devices {args} {
    if {$::opened eq ""} { return "" }
    set devs [expr {[string match */jsn-* $::opened] ? "" : "xcku115_0"}]
    foreach kv [expr {[info exists ::env(STUB_DEVS)] ? $::env(STUB_DEVS) : ""}] {
        set k [string range $kv 0 [expr {[string last = $kv] - 1}]]
        if {$k eq $::opened} { set devs [split [string range $kv [expr {[string last = $kv] + 1}] end] ,] }
    }
    set pat [lindex $args end]
    if {$pat eq "" || [string match -* $pat]} { return $devs }
    set out {}
    foreach d $devs { if {[string match $pat $d]} { lappend out $d } }
    return $out
}
proc current_hw_device {args} { if {[lindex $args end] eq ""} { error "current_hw_device: no device" } }
proc set_property {args} {}
proc program_hw_devices {args} { log program $::opened [lindex $args end] }
proc refresh_hw_device {args} {}
proc close_hw_target {args} {}
proc disconnect_hw_server {args} {}
source [lindex $argv 0]
'''

_VIVADO = '''#!/bin/sh
src=""
while [ $# -gt 0 ]; do [ "$1" = "-source" ] && src=$2; shift; done
exec tclsh "$STUB_TCL" "$src"
'''


@pytest.fixture
def rig(tmp_path):
    (tmp_path / "stubs.tcl").write_text(_STUBS)
    viv = tmp_path / "vivado"
    viv.write_text(_VIVADO)
    viv.chmod(viv.stat().st_mode | stat.S_IXUSR)
    bit = tmp_path / "shell.bit"
    bit.write_bytes(b"\0\x09bitstream")
    return tmp_path


def recover(rig, targets, cable="*%s*" % SERIAL, devs=None):
    log = rig / "stub.log"
    log.write_text("")
    env = dict(os.environ, VIVADO=str(rig / "vivado"), STUB_TCL=str(rig / "stubs.tcl"),
               STUB_LOG=str(log), STUB_TARGETS=" ".join(targets), MPS3_JTAG_CABLE=cable,
               MPS3_HW_URL="hub.example:3121")
    if devs:
        env["STUB_DEVS"] = devs
    r = subprocess.run(["bash", str(_RECOVER), str(rig / "shell.bit")], capture_output=True,
                       text=True, env=env, timeout=60)
    return r, log.read_text().splitlines()


def test_1_the_real_hub_listing_programs_the_cable(rig):
    r, log = recover(rig, HUB)
    assert r.returncode == 0, r.stdout + r.stderr
    assert log == [f"open {DIG}", f"program {DIG} xcku115_0"]


def test_2_listed_the_other_way_round_still_the_cable(rig):
    """The pre-fix script opened the first match -- the FPGA's own port -- and failed."""
    r, log = recover(rig, [JSN] + HUB[:-1])
    assert r.returncode == 0, r.stdout + r.stderr
    assert log == [f"open {DIG}", f"program {DIG} xcku115_0"]
    assert f"twin {JSN}" in r.stdout


def test_3_two_digilent_cables_carrying_the_serial_are_refused(rig):
    """Negative control: this could be two boards. The pre-fix script programmed one."""
    r, log = recover(rig, [DIG, f"{DIG}A"] + HUB[1:-1])
    assert r.returncode == 4 and "refusing" in r.stderr, r.stdout + r.stderr
    assert log == []


def test_4_a_port_of_another_device_is_not_our_twin(rig):
    """Negative control: a jsn port whose device is not the XCKU115 (0x23727093)."""
    other = f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}-23727093-0"
    r, log = recover(rig, [DIG, other])
    assert r.returncode == 4 and "refusing" in r.stderr, r.stdout + r.stderr
    assert log == []


@pytest.mark.parametrize("extra", [f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}A-1390d093-0",   # contains ours
                                   f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}-1390d093-1"])   # a third match
def test_5_other_multi_matches_are_refused(rig, extra):
    targets = [DIG, extra] if extra.endswith("A-1390d093-0") else [DIG, JSN, extra]
    r, log = recover(rig, targets)
    assert r.returncode == 4 and "refusing" in r.stderr, r.stdout + r.stderr
    assert log == []


def test_6_no_match_is_refused(rig):
    r, log = recover(rig, HUB[1:-1])
    assert r.returncode == 4 and "refusing" in r.stderr
    assert log == []


def test_7_a_target_without_exactly_one_xcku115_is_never_programmed(rig):
    for devs in ("xcvu9p_0", "xcku115_0,xcku115_1"):
        r, log = recover(rig, HUB, devs=f"{DIG}={devs}")
        assert r.returncode == 4, r.stdout + r.stderr
        assert log == [f"open {DIG}"]                    # opened, never programmed


def test_8_a_filter_that_names_no_plain_serial_gets_no_pairing_exception(rig):
    """Only the "*<serial>*" shape names a serial to pair on; any other filter must
    match exactly one target."""
    r, log = recover(rig, HUB, cable="*Digilent/%s" % SERIAL)          # one match: fine
    assert r.returncode == 0 and log == [f"open {DIG}", f"program {DIG} xcku115_0"], r.stderr
    r, log = recover(rig, HUB, cable="*/*%s*" % SERIAL)                 # both match: refused
    assert r.returncode == 4 and log == [], r.stdout + r.stderr
