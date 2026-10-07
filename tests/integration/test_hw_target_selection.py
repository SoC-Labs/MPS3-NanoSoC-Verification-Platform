"""No script opens "whatever hw_target is listed first" on the SHARED hub hw_server.

``scripts/mps3_hw_target.tcl`` picks the MPS3's target by its cable filter: exactly
one match, or the one benign pair seen on the hub in B1 (2026-09-24) -- our cable
``.../Digilent/210249B86C47`` plus ``.../Xilinx/jsn-JTAG-HS2-210249B86C47-1390d093-0``,
a port hosted by our own XCKU115 -- and then exactly one xcku115 on it. Its three
callers used a bare ``open_hw_target``: ``scripts/mps3_swap_design.sh``,
``firmware/platform/board_xvc_throughput.sh`` and ``fpga/dfx/proof/board_swap.tcl``.

* the helper, over a stub hub in plain tclsh (every listing shape);
* two of the callers end to end over the same stub (the third needs a verified
  shell image and FIRE=1; the repo gate and the helper tests cover it);
* a repo gate: no tracked ``*.sh`` / ``*.tcl`` opens a target without naming it.

``HWSEL_ROOT`` points the suite at another tree (the negative control: at the
pre-fix scripts the gate and both end-to-end "listed the other way" / "two
boards" cases fail).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("HWSEL_ROOT", _REPO))
HELPER = _REPO / "scripts" / "mps3_hw_target.tcl"

pytestmark = pytest.mark.skipif(shutil.which("tclsh") is None, reason="no tclsh")

SERIAL = "210249B86C47"
H = "hub.example:3121/xilinx_tcf"
DIG = f"{H}/Digilent/{SERIAL}"
JSN = f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}-1390d093-0"
OTHERS = [f"{H}/Xilinx/{t}" for t in ("Z2_01_TULA", "XFL1EAUJ5SPOA", "XFL1MHS3ZB1PA", "Z2_02_TULA")]
HUB = [DIG] + OTHERS + [JSN]                    # B1's b1_a_program.txt, in its order
HUB_PORT_FIRST = [JSN] + OTHERS + [DIG]
TWO_BOARDS = [DIG, f"{DIG}A"] + OTHERS

#: Vivado hw_manager over a shared hub: our cable carries the xcku115, a port our
#: FPGA hosts carries none, another board carries its own part. Logs to STUB_LOG.
_STUBS = r'''
set ::opened ""
proc log {args} { set f [open $::env(STUB_LOG) a]; puts $f [join $args]; close $f }
proc open_hw_manager {args} {}
proc connect_hw_server {args} {}
proc disconnect_hw_server {args} {}
proc close_hw_manager {args} {}
proc close_hw_target {args} {}
proc get_hw_targets {args} {
    set i [lsearch -exact $args -filter]
    if {$i < 0} { return $::env(STUB_TARGETS) }
    regexp {^NAME =~ (.*)$} [lindex $args [expr {$i + 1}]] -> glob
    set out {}
    foreach t $::env(STUB_TARGETS) { if {[string match $glob $t]} { lappend out $t } }
    return $out
}
proc current_hw_target {args} { return [lindex $::env(STUB_TARGETS) 0] }
proc open_hw_target {args} {
    set t [lindex $args end]
    if {$t eq "" || [string match -* $t]} { set t [lindex $::env(STUB_TARGETS) 0] }
    set ::opened $t
    log open $t
}
proc get_hw_devices {args} {
    if {$::opened eq ""} { return "" }
    if {[string match */jsn-* $::opened]} { set devs {} } elseif {[string match */Digilent/* $::opened]} {
        set devs xcku115_0 } else { set devs xc7z020_0 }
    if {[info exists ::env(STUB_DEVS)]} { set devs [split $::env(STUB_DEVS) ,] }
    set pat [lindex $args end]
    if {$pat eq "" || [string match -* $pat]} { return $devs }
    set out {}
    foreach d $devs { if {[string match $pat $d]} { lappend out $d } }
    return $out
}
proc current_hw_device {args} { if {[lindex $args end] eq ""} { error "current_hw_device: no device" } }
proc refresh_hw_device {args} {}
proc set_property {args} {}
proc get_property {args} { return 0x07FBA51C }
proc program_hw_devices {args} { log program $::opened }
set script [lindex $::argv 0]
set ::argv [lrange $::argv 1 end]
set ::argc [llength $::argv]
source $script
'''

_VIVADO = '''#!/bin/sh
src=""; targs=""
while [ $# -gt 0 ]; do
    case "$1" in -source) src=$2; shift ;; -tclargs) shift; targs="$*"; break ;; esac
    shift
done
exec tclsh "$STUB_TCL" "$src" $targs
'''


@pytest.fixture
def rig(tmp_path):
    (tmp_path / "stubs.tcl").write_text(_STUBS)
    viv = tmp_path / "vivado"
    viv.write_text(_VIVADO)
    viv.chmod(viv.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _env(rig, targets, **extra):
    log = rig / "stub.log"
    log.write_text("")
    e = dict(os.environ, STUB_TCL=str(rig / "stubs.tcl"), STUB_LOG=str(log),
             STUB_TARGETS=" ".join(targets))
    e.pop("MPS3_JTAG_CABLE", None)
    e.update(extra)
    return e, log


# --------------------------------------------------------------------------- #
# the helper
# --------------------------------------------------------------------------- #

def _helper(rig, targets, cable="*%s*" % SERIAL, **extra):
    drv = rig / "drive.tcl"
    drv.write_text("source {%s}\nputs \"DEV=[mps3_open_hw_target {%s}]\"\n" % (HELPER, cable))
    env, log = _env(rig, targets, **extra)
    r = subprocess.run(["tclsh", str(rig / "stubs.tcl"), str(drv)], capture_output=True, text=True,
                       env=env, timeout=30)
    return r, log.read_text().split("\n")[:-1]


@pytest.mark.parametrize("targets", [HUB, HUB_PORT_FIRST, [DIG]], ids=["hub", "port-first", "alone"])
def test_helper_picks_our_cable(rig, targets):
    r, log = _helper(rig, targets)
    assert r.returncode == 0 and "DEV=xcku115_0" in r.stdout, r.stdout + r.stderr
    assert log == [f"open {DIG}"]


@pytest.mark.parametrize("targets", [
    TWO_BOARDS,                                                    # two cables carry the serial
    [DIG, f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}-23727093-0"],        # a port of another device
    [DIG, f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}A-1390d093-0"],       # a serial that CONTAINS ours
    [DIG, JSN, f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}-1390d093-1"],  # three matches
    [JSN, f"{H}/Xilinx/jsn-JTAG-HS2-{SERIAL}-2390d093-0"],        # no Digilent entry
    OTHERS,                                                        # not there at all
], ids=["two-cables", "other-device", "longer-serial", "three", "no-digilent", "absent"])
def test_helper_refuses_before_opening_anything(rig, targets):
    r, log = _helper(rig, targets)
    assert r.returncode != 0 and "refusing to touch the shared hw_server" in r.stderr, r.stdout + r.stderr
    assert log == []


def test_helper_needs_exactly_one_xcku115(rig):
    for devs in ("xcvu9p_0", "xcku115_0,xcku115_1"):
        r, log = _helper(rig, HUB, STUB_DEVS=devs)
        assert r.returncode != 0 and "xcku115 on" in r.stderr, r.stdout + r.stderr
        assert log == [f"open {DIG}"]


def test_helper_pairs_only_on_a_plain_serial_filter(rig):
    r, log = _helper(rig, HUB, cable="*Digilent/%s" % SERIAL)       # one match: fine
    assert r.returncode == 0 and log == [f"open {DIG}"], r.stderr
    r, log = _helper(rig, HUB, cable="*/*%s*" % SERIAL)             # both match: refused
    assert r.returncode != 0 and log == []


# --------------------------------------------------------------------------- #
# the callers, end to end over the stub hub
# --------------------------------------------------------------------------- #

def _board_swap(rig, targets):
    bits = rig / "bits"
    bits.mkdir(exist_ok=True)
    env, log = _env(rig, targets)
    r = subprocess.run([str(rig / "vivado"), "-mode", "batch", "-source",
                        str(ROOT / "fpga" / "dfx" / "proof" / "board_swap.tcl"), "-tclargs", str(bits)],
                       capture_output=True, text=True, env=env, timeout=60)
    return r, log.read_text().split("\n")[:-1]


def test_board_swap_programs_our_cable_in_either_order(rig):
    for targets in (HUB, HUB_PORT_FIRST):
        r, log = _board_swap(rig, targets)
        assert r.returncode == 0 and "BOARD_SWAP_COMPLETE" in r.stdout, r.stdout + r.stderr
        assert log == [f"open {DIG}"] + [f"program {DIG}"] * 5


def test_board_swap_never_programs_either_of_two_boards(rig):
    r, log = _board_swap(rig, TWO_BOARDS)
    assert r.returncode != 0 and not any(l.startswith("program") for l in log), (r.stdout, log)


def _swap_design(rig, targets):
    prod = rig / "prod"
    prod.mkdir(exist_ok=True)
    for f in ("config_rm_led_pblock_rp_dut_partial.bit", "config_rm_greybox_fw.bit",
              "config_rm_greybox_pblock_rp_dut_partial_clear.bit"):
        (prod / f).write_bytes(b"bit")
    manifest = ROOT / "fpga" / "dfx" / "overlay" / "led" / "manifest.json"
    want = re.search(r'"rm_id"[^,]*?(0x[0-9A-Fa-f]{8})', manifest.read_text()).group(1)
    xsdb = rig / "xsdb"
    xsdb.write_text("#!/bin/sh\necho GOT_RM_ID=%s\n" % want.upper().replace("0X", "0x"))
    xsdb.chmod(0o755)
    env, log = _env(rig, targets, VIVADO=str(rig / "vivado"), XSDB=str(xsdb),
                    MPS3_HW_URL="tcp:hub.example:3121", MPS3_LEASE_TOKEN="tok-test",
                    MPS3_PROD_DIR=str(prod), MPS3_BASE_DIR=str(prod))
    r = subprocess.run(["bash", str(ROOT / "scripts" / "mps3_swap_design.sh"), "led"],
                       capture_output=True, text=True, env=env, timeout=60)
    return r, log.read_text().split("\n")[:-1]


def test_swap_design_programs_our_cable_in_either_order(rig):
    for targets in (HUB, HUB_PORT_FIRST):
        r, log = _swap_design(rig, targets)
        assert r.returncode == 0 and "==> OK" in r.stdout, r.stdout + r.stderr
        assert log == [f"open {DIG}"] + [f"program {DIG}"] * 3


def test_swap_design_never_programs_either_of_two_boards(rig):
    r, log = _swap_design(rig, TWO_BOARDS)
    assert r.returncode != 0 and not any(l.startswith("program") for l in log), (r.stdout, log)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

#: `open_hw_target` with nothing after it (end of line, a closing quote, `;`, `}`)
#: opens the hw_server's FIRST target. Comment lines are prose, not calls, and a
#: test_*.sh / test_*.tcl only stubs or greps for it.
_BARE_OPEN = re.compile(r"\bopen_hw_target\b\s*(?:$|['\";}])")


def _scripts(root: Path):
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "*.sh", "*.tcl"],
                             capture_output=True, text=True, check=True).stdout.split()
        return [root / p for p in out]
    return [p for p in root.rglob("*") if p.suffix in (".sh", ".tcl") and p.is_file()]


def test_no_tracked_script_opens_the_first_hw_target():
    bad = []
    for path in _scripts(ROOT):
        if path.name.startswith("test_"):
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _BARE_OPEN.search(line):
                bad.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not bad, "a bare open_hw_target opens whatever is listed first:\n" + "\n".join(bad)
