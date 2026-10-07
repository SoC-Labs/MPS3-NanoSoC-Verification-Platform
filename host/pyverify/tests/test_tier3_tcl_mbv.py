"""Board-free runs of the tier-3 xsdb scripts against a STUB xsdb.

scripts/mps3_diag.tcl and scripts/harness_gates/tier3_csr_liveness{,_mbv}.tcl
only ever call five xsdb commands (connect, ta, targets, mrd, mwr). This file
defines those five in plain tclsh over a fake target table + memory, then
sources the REAL scripts, so their target selection, anchor scan, MicroBlaze V
fallback and exit codes are exercised with no hw_server and no board:

* bare metal (classic "MicroBlaze" targets) -- the path that must not change;
* MicroBlaze V with a readable hart -- mailbox at 0x1FF00 + stage0 block;
* MicroBlaze V whose hart cannot be read while running (no system-bus access,
  the likely B1 outcome) -- must EXIT 2 and name the ssh fallback, never pass.

tclsh is a hard requirement here, as it is for pin-check: CI installs it
(.github/workflows/ci.yml), so a missing tclsh is a FAILURE, not a skip.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from pyverify.fielded import repo_root

ROOT = repo_root()
TCLSH = shutil.which("tclsh")

STUB = r'''
# ---- stub xsdb: targets = {id name cable readable}, mem(id,addr) = hexword ----
proc connect {args} {}
proc ta {args} {
    set filter ""
    set i [lsearch $args -filter]
    if {$i >= 0} { set filter [lindex $args [expr {$i + 1}]] }
    set pat "*"
    set cpat "*"
    regexp {(?:^|[^_])name =~ "([^"]*)"} $filter -> pat
    regexp {jtag_cable_name =~ "([^"]*)"} $filter -> cpat
    set out {}
    foreach t $::TARGETS {
        lassign $t id name cable readable
        if {[string match $pat $name] && [string match $cpat $cable]} {
            lappend out [dict create target_id $id name $name jtag_cable_name $cable]
        }
    }
    return $out
}
proc targets {args} { if {[llength $args]} { set ::CUR [lindex $args 0] }; return }
proc _readable {} {
    foreach t $::TARGETS { if {[lindex $t 0] == $::CUR} { return [lindex $t 3] } }
    return 0
}
proc mrd {args} {
    if {![_readable]} { error "Cannot access memory while the core is running (no system bus access)" }
    set args [lsearch -all -inline -not -exact $args -force]
    set addr [lindex $args 0]
    set n [expr {[llength $args] > 1 ? [lindex $args 1] : 1}]
    set out {}
    for {set k 0} {$k < $n} {incr k} {
        set a [expr {($addr + 4*$k) & 0xFFFFFFFF}]
        set key "$::CUR,[format %X $a]"
        set v 00000000
        if {[info exists ::MEM($key)]} { set v $::MEM($key) }   ;# NOT expr: 000037 is octal
        lappend out "[format %X $a]:" $v
    }
    return $out
}
proc mwr {args} {
    if {![_readable]} { error "Cannot access memory while the core is running" }
    set args [lsearch -all -inline -not -exact $args -force]
    set a [expr {[lindex $args 0] & 0xFFFFFFFF}]
    set v [format %08X [expr {[lindex $args 1] & 0xFFFFFFFF}]]
    set key "$::CUR,[format %X $a]"
    if {![info exists ::DEAD_CSR]} { set ::MEM($key) $v }
}
'''


def _poke(target, addr, words):
    return "".join('set ::MEM(%s,%X) %08X\n' % (target, addr + 4 * i, w)
                   for i, w in enumerate(words))


def _run(script, targets, mem_tcl="", env=None, extra=""):
    assert TCLSH, "tclsh is required (CI installs it; apt install tcl)"
    prog = STUB + "set ::TARGETS {%s}\n" % " ".join(
        "{%s {%s} {%s} %d}" % t for t in targets) + mem_tcl + extra
    prog += "source {%s}\n" % (ROOT / script)
    e = dict(os.environ)
    e["MPS3_HW_URL"] = "tcp:stub:3121"
    for k in ("MPS3_HARNESS_CPU", "MPS3_JTAG_CABLE"):
        e.pop(k, None)
    e.update(env or {})
    p = subprocess.run([TCLSH], input=prog, capture_output=True, text=True, env=e, timeout=60)
    return p.returncode, p.stdout + p.stderr


MAGIC = 0xD1A6C0DE
MB_V8 = [(7, "MicroBlaze #0", "cable1", 1)]
MBV_OK = [(3, "MicroBlaze V Debug Module at USER2", "cable1", 0), (4, "Hart #0", "cable1", 1)]
MBV_NOSBA = [(3, "MicroBlaze V Debug Module at USER2", "cable1", 0), (4, "Hart #0", "cable1", 0)]


def test_diag_bare_metal_path_is_unchanged():
    rc, out = _run("scripts/mps3_diag.tcl", MB_V8, _poke(7, 0xFFF00, [MAGIC, 8] + [3] * 62))
    assert rc == 0, out
    assert "cpu=mb  mailbox=0x000FFF00" in out and "256 B / 64 words (v8)" in out
    assert "stage0 status" not in out


def test_diag_mbv_reads_the_128k_anchor_and_stage0_block():
    s0 = [0x54533053, 1, 0x100] + [0] * 60 + [0x54533053]
    rc, out = _run("scripts/mps3_diag.tcl", MBV_OK,
                   _poke(4, 0x1FF00, [MAGIC, 8] + [1] * 62) + _poke(4, 0x1FE00, s0))
    assert rc == 0, out
    assert "cpu=mbv  mailbox=0x0001FF00" in out
    assert "stage0 status @ 0x0001FE00 (layout: stage0_status.h)" in out
    assert "magic              = 0x54533053" in out


def test_diag_mbv_without_system_bus_access_exits_2_and_names_ssh():
    rc, out = _run("scripts/mps3_diag.tcl", MBV_NOSBA)
    assert rc == 2, out
    assert "NOJTAG" in out and "pyverify mailbox" in out


def test_diag_forced_mb_never_looks_at_harts():
    rc, out = _run("scripts/mps3_diag.tcl", MBV_OK, _poke(4, 0x1FF00, [MAGIC, 8] + [0] * 62),
                   env={"MPS3_HARNESS_CPU": "mb"})
    assert rc == 1 and "no MicroBlaze target has magic" in out


def test_diag_rejects_an_unknown_cpu_value():
    rc, out = _run("scripts/mps3_diag.tcl", MB_V8, env={"MPS3_HARNESS_CPU": "arm"})
    assert rc == 1 and "expected mb or mbv" in out


def test_tier3_bare_metal_unchanged():
    rc, out = _run("scripts/harness_gates/tier3_csr_liveness.tcl", MB_V8,
                   _poke(7, 0xFFF00, [MAGIC, 8]))
    assert rc == 0 and "decode is live on hardware." in out, out


def test_tier3_bare_metal_catches_a_dead_decode():
    rc, out = _run("scripts/harness_gates/tier3_csr_liveness.tcl", MB_V8,
                   _poke(7, 0xFFF00, [MAGIC, 8]), extra="set ::DEAD_CSR 1\n")
    assert rc == 1 and "CSR decode DEAD" in out, out


def test_tier3_dispatches_to_the_mbv_gate():
    rc, out = _run("scripts/harness_gates/tier3_csr_liveness.tcl", MBV_OK,
                   env={"MPS3_HARNESS_CPU": "mbv"})
    assert rc == 0, out
    assert "MicroBlaze V, target 4" in out and "only hart" in out


def test_tier3_mbv_dead_decode_fails():
    rc, out = _run("scripts/harness_gates/tier3_csr_liveness_mbv.tcl", MBV_OK,
                   extra="set ::DEAD_CSR 1\n")
    assert rc == 1 and "CSR decode DEAD" in out, out


def test_tier3_mbv_no_system_bus_access_is_exit_2_not_a_pass():
    rc, out = _run("scripts/harness_gates/tier3_csr_liveness_mbv.tcl", MBV_NOSBA)
    assert rc == 2, out
    assert "pyverify csr-liveness" in out


def test_tier3_mbv_refuses_to_guess_between_two_harts():
    two = MBV_OK + [(9, "Hart #0", "cable2", 1)]
    rc, out = _run("scripts/harness_gates/tier3_csr_liveness_mbv.tcl", two)
    assert rc == 1 and "MPS3_JTAG_CABLE" in out
    rc, out = _run("scripts/harness_gates/tier3_csr_liveness_mbv.tcl", two,
                   env={"MPS3_JTAG_CABLE": "cable2"})
    assert rc == 0 and "target 9" in out, out
