#!/bin/sh
# test_dfx_preflight.sh -- dfx_preflight.sh's JTAG cable filter, board-free.
#
# The Tcl the script generates runs in plain tclsh over a STUB Vivado: VIVADO=
# a shell stub that sources the generated file after stub hw_* procs modelling a
# SHARED hw_server with several boards on it. Each board has its own USERCODE,
# so the verdict says whose device was read. A bare `open_hw_target` (the
# pre-2026-09-24 script) opens the FIRST target listed -- the stub does exactly
# that, which is what makes the negative controls bite:
#
#   1 ours listed SECOND, both configured: MATCH, and the opened target is ours
#   2 the OTHER board (listed first) carries the expected USERCODE, ours does
#     not: MISMATCH -- a script that read the first target would say MATCH
#   3 our cable absent: FAIL CLOSED (exit 2), no target opened at all
#   4 two targets carry the serial: FAIL CLOSED (ambiguous), none opened
#   5 MPS3_CABLE_SERIAL names the other board: that board is read
#   6 a serial that is empty / not alphanumeric: exit 2 before Vivado runs
#   7 --print-expected still needs no board (Vivado never runs)
#   8 OUR FPGA'S OWN PORT BESIDE THE CABLE -- the real hub's 7-target listing
#     from B1 (2026-09-24): Digilent/<serial> + Xilinx/jsn-JTAG-HS2-<serial>-
#     1390d093-0 (device 0's context, the XCKU115 IDCODE). MATCH through the
#     Digilent target; the jsn one is never opened. The pre-fix script refused
#     this (exit 2) -- the case that fails on it.
#   9 the same pair listed jsn-first: still the Digilent target
#  10 Digilent/<serial> + a jsn whose serial only CONTAINS ours: refused
#  11 Digilent/<serial> + two jsn ports (3 matches): refused
#  12 two jsn targets, no Digilent one: refused
#  13 the opened target has no xcku115, or two: exit 2, USERCODE never read
#  14 Digilent/<serial> + a jsn port of ANOTHER device (not the XCKU115): refused
#
#   sh src/linux_soc/hw/board_scripts/test_dfx_preflight.sh [script-under-test]
#
# Run by `make -C src/linux_soc/hw/fw_stage0/test` (check-linux stage 3). Needs
# tclsh (CI installs tcl); without it this FAILS rather than skip.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PRE=${1:-$HERE/dfx_preflight.sh}
command -v tclsh >/dev/null 2>&1 || { echo "FAIL test_dfx_preflight: no tclsh (install tcl)"; exit 1; }
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
pass=0
fail=0
ok()  { pass=$((pass + 1)); echo "  PASS  $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL  $1"; }

OURS=210249B86C47
THEIRS=210249A0FFEE
T_OURS="localhost:3121/xilinx_tcf/Digilent/$OURS"
T_THEIRS="localhost:3121/xilinx_tcf/Digilent/$THEIRS"
EXPECT=5263642c

# the static the partials were built against: a .bit whose header carries UserID
printf '\000\011\017\360\017\360UserID=0X5263642C\000rest-of-header' > "$W/static.bit"

cat > "$W/stubs.tcl" <<'TCL'
# A shared hw_server: STUB_TARGETS in listing order, STUB_UC "target=usercode ...".
set ::opened ""
proc log {args} { set f [open $::env(STUB_LOG) a]; puts $f [join $args]; close $f }
proc open_hw_manager {args} {}
proc connect_hw_server {args} { log connect_hw_server {*}$args }
proc get_hw_targets {args} { return $::env(STUB_TARGETS) }
proc open_hw_target {args} {
    set t [lindex $args end]
    if {$t eq "" || [string match -* $t]} { set t [lindex $::env(STUB_TARGETS) 0] }
    if {[lsearch -exact $::env(STUB_TARGETS) $t] < 0} { error "no such target $t" }
    set ::opened $t
    log open_hw_target $t
}
# STUB_DEVS "target=dev,dev ...": the chain behind a target (default xcku115_0)
proc get_hw_devices {args} {
    if {$::opened eq ""} { return "" }
    set devs xcku115_0
    foreach kv [expr {[info exists ::env(STUB_DEVS)] ? $::env(STUB_DEVS) : ""}] {
        lassign [split $kv =] t d
        if {$t eq $::opened} { set devs [split $d ,] }
    }
    set pat [lindex $args end]
    if {$pat eq "" || [string match -* $pat]} { return $devs }
    set out {}
    foreach d $devs { if {[string match $pat $d]} { lappend out $d } }
    return $out
}
proc current_hw_device {args} {}
proc refresh_hw_device {args} {}
proc get_property {name obj} {
    foreach kv $::env(STUB_UC) {
        lassign [split $kv =] t uc
        if {$t eq $::opened} { return $uc }
    }
    error "no USERCODE"
}
proc close_hw_target {args} {}
proc close_hw_manager {args} {}
source [lindex $argv 0]
TCL

cat > "$W/vivado" <<'SH'
#!/bin/sh
src=""
while [ $# -gt 0 ]; do [ "$1" = "-source" ] && src=$2; shift; done
echo "vivado ran" >> "$STUB_LOG"
exec tclsh "$STUB_TCL" "$src"
SH
chmod +x "$W/vivado"

# run <targets> <usercodes> [env...] -> $RC, $OUT, $LOG
run() {
    targets=$1; ucs=$2; shift 2
    : > "$W/log"
    OUT=$(env VIVADO="$W/vivado" STUB_TCL="$W/stubs.tcl" STUB_LOG="$W/log" \
          STUB_TARGETS="$targets" STUB_UC="$ucs" "$@" bash "$PRE" "$W/static.bit" 2>&1)
    RC=$?
    LOG=$(cat "$W/log")
}
check() { if eval "$2"; then ok "$1"; else bad "$1"; echo "$OUT" | sed 's/^/        | /'; echo "$LOG" | sed 's/^/        log: /'; fi; }

echo "== dfx_preflight.sh cable filter ($PRE)"
run "$T_THEIRS $T_OURS" "$T_THEIRS=0xFFFFFFFF $T_OURS=0x5263642C"
check "1 ours listed second: MATCH on OUR target" \
      '[ $RC = 0 ] && echo "$OUT" | grep -q "PREFLIGHT: MATCH" && echo "$LOG" | grep -qx "open_hw_target $T_OURS"'

run "$T_THEIRS $T_OURS" "$T_THEIRS=0x5263642C $T_OURS=0xFFFFFFFF"
check "2 the other board has the expected USERCODE, ours not: MISMATCH [negative control]" \
      '[ $RC = 1 ] && echo "$OUT" | grep -q "MISMATCH" && ! echo "$LOG" | grep -q "open_hw_target $T_THEIRS"'

run "$T_THEIRS" "$T_THEIRS=0x5263642C"
check "3 our cable absent: exit 2, NO target opened [negative control]" \
      '[ $RC = 2 ] && echo "$OUT" | grep -q "cable $OURS not found" && ! echo "$LOG" | grep -q open_hw_target'

run "$T_OURS ${T_OURS}A" "$T_OURS=0x5263642C ${T_OURS}A=0x5263642C"
check "4 two targets carry the serial: exit 2, none opened" \
      '[ $RC = 2 ] && ! echo "$LOG" | grep -q open_hw_target'

run "$T_OURS $T_THEIRS" "$T_OURS=0xFFFFFFFF $T_THEIRS=0x5263642C" MPS3_CABLE_SERIAL=$THEIRS
check "5 MPS3_CABLE_SERIAL selects that board" \
      '[ $RC = 0 ] && echo "$LOG" | grep -qx "open_hw_target $T_THEIRS"'

for s in "" "210249B86C4*" "a b" '"];exit;#'; do
    run "$T_OURS" "$T_OURS=0x5263642C" MPS3_CABLE_SERIAL="$s"
    check "6 serial '$s' refused before Vivado runs" '[ $RC = 2 ] && [ -z "$LOG" ]'
done

: > "$W/log"
OUT=$(env VIVADO="$W/vivado" STUB_LOG="$W/log" bash "$PRE" --print-expected "$W/static.bit" 2>&1); RC=$?
LOG=$(cat "$W/log")
check "7 --print-expected: no board access" \
      '[ $RC = 0 ] && echo "$OUT" | grep -q "0x$EXPECT" && [ -z "$LOG" ]'

# ---- the cable + our FPGA's own port (B1, the real hub, 2026-09-24) ----
H=hub.example.org:3121/xilinx_tcf
D_OURS="$H/Digilent/$OURS"
J_OURS="$H/Xilinx/jsn-JTAG-HS2-$OURS-1390d093-0"
HUB="$D_OURS $H/Xilinx/Z2_01_TULA $H/Xilinx/XFL1EAUJ5SPOA $H/Xilinx/XFL1MHS3ZB1PA $H/Xilinx/Z2_02_TULA $H/Xilinx/Z2_04_TULA $J_OURS"
UC_HUB="$D_OURS=0x5263642C $J_OURS=0x5263642C"

run "$HUB" "$UC_HUB"
check "8 the real hub: the cable + our FPGA's jsn port: MATCH via Digilent, jsn never opened" \
      '[ $RC = 0 ] && echo "$OUT" | grep -q "PREFLIGHT: MATCH" && echo "$LOG" | grep -qx "open_hw_target $D_OURS" && ! echo "$LOG" | grep -q "open_hw_target $J_OURS" && echo "$OUT" | grep -q "PREFLIGHT_DUPLICATE: $J_OURS"'

run "$J_OURS $D_OURS" "$UC_HUB"
check "9 the same pair, jsn listed first: still the Digilent target" \
      '[ $RC = 0 ] && echo "$LOG" | grep -qx "open_hw_target $D_OURS" && ! echo "$LOG" | grep -q "open_hw_target $J_OURS"'

J_LONGER="$H/Xilinx/jsn-JTAG-HS2-${OURS}A-77aa0000-0"
run "$D_OURS $J_LONGER" "$D_OURS=0x5263642C $J_LONGER=0x5263642C"
check "10 Digilent + a jsn whose serial only CONTAINS ours: exit 2, none opened [negative control]" \
      '[ $RC = 2 ] && ! echo "$LOG" | grep -q open_hw_target'

J_CH1="$H/Xilinx/jsn-JTAG-HS2-$OURS-1390d093-1"
run "$D_OURS $J_OURS $J_CH1" "$UC_HUB $J_CH1=0x5263642C"
check "11 Digilent + two jsn ports (3 matches): exit 2, none opened [negative control]" \
      '[ $RC = 2 ] && ! echo "$LOG" | grep -q open_hw_target'

J_OTHER="$H/Xilinx/jsn-JTAG-HS2-$OURS-2390d093-0"
run "$J_OURS $J_OTHER" "$J_OURS=0x5263642C $J_OTHER=0x5263642C"
check "12 two jsn targets, no Digilent: exit 2, none opened [negative control]" \
      '[ $RC = 2 ] && ! echo "$LOG" | grep -q open_hw_target'

for devs in "xcvu9p_0" "xcku115_0,xcku115_1"; do
    run "$HUB" "$UC_HUB" STUB_DEVS="$D_OURS=$devs"
    check "13 the opened target's chain is '$devs': exit 2, USERCODE not read [negative control]" \
          '[ $RC = 2 ] && echo "$OUT" | grep -q "not exactly one xcku115" && ! echo "$OUT" | grep -q MATCH'
done
run "$T_OURS" "$T_OURS=0x5263642C" STUB_DEVS="$T_OURS=xcvu9p_0"
check "13 one target, no xcku115 on it: exit 2 [negative control]" \
      '[ $RC = 2 ] && echo "$OUT" | grep -q "not exactly one xcku115"'

J_Z7="$H/Xilinx/jsn-JTAG-HS2-$OURS-23727093-0"
run "$D_OURS $J_Z7" "$D_OURS=0x5263642C $J_Z7=0x5263642C"
check "14 Digilent + a port of a device that is not the XCKU115: exit 2, none opened [negative control]" \
      '[ $RC = 2 ] && ! echo "$LOG" | grep -q open_hw_target'

echo "test_dfx_preflight: $pass passed, $fail failed"
[ "$fail" = 0 ]
