#!/bin/bash
# dfx_preflight.sh — REFUSE a DFX swap unless the FLOWN static matches the partials.
#
# WHY: a partial bitstream is bound to the exact static implementation it was built
# against. Loading the overlay_linux partials onto a different impl (e.g. phaseB's
# shell_linux_top.bit) DESTROYS the whole FPGA configuration. That happened twice on
# 2026-07-24. The swap's own static_id check CANNOT catch it — it compares the overlay
# manifest against the provisioned FILE /etc/mps3/static_id, which reads 0x2B082E1B
# either way. See docs/ICAP_SWAP_PROVEN.md / docs/ICAP_SWAP_FAILURE_ANALYSIS.md.
#
# THIS CHECK IS HARDWARE-DERIVED: it reads REGISTER.USERCODE from the running device
# over JTAG and compares it to the UserID stamped in the static bitstream the partials
# came from. Distinguishing values observed on this platform:
#     config_rm_greybox.bit (DFX-capable, matches overlay_linux) -> UserID=5263642C
#     shell_linux_top.bit   (phaseB, NOT swap-compatible)        -> UserID=0XFFFFFFFF
#
# NOTE 0xffffffff means "a bitstream with NO UserID stamp" — that is phaseB, but also
# the SD-resident shell image and any other unstamped design. The check is exact-match
# against the expected value, so it refuses ALL of them. That also makes it a board-
# contention detector: if another session (or an MCC reload from SD) has reprogrammed
# the FPGA under you, USERCODE stops matching and the swap is refused. Observed for
# real on 2026-07-24 — a concurrent lease holder reprogrammed the device mid-session
# and this check caught it. ALWAYS hold the fpgahub lease (`fpgahub lease show mps3_01`)
# for the whole campaign; a lapsed lease is exactly how foreign images appear.
#
# ONE BOARD ONLY (lane HARDEN, 2026-09-24). The hw_server is SHARED: other boards'
# JTAG targets hang off it too, and a bare `open_hw_target` opens whichever target
# it lists first -- reading ANOTHER board's USERCODE (a false MATCH, or a MISMATCH
# blamed on the wrong board). The target is therefore picked by the cable's serial,
# MPS3_CABLE_SERIAL (default 210249B86C47, this MPS3's FT2232, as
# scripts/mps3_recover.sh), and the check FAILS CLOSED (exit 2, nothing opened)
# when no target -- or more than one -- carries that serial. There is no "any
# cable" setting.
#
# ONE EXCEPTION, OUR FPGA'S OWN PORT (B1 on the real hub, 2026-09-24). Beside the
# cable, .../xilinx_tcf/Digilent/210249B86C47 (the target mps3_recover.sh
# programs through), the hub's Vivado 2025.2 hw_server lists
# .../xilinx_tcf/Xilinx/jsn-JTAG-HS2-210249B86C47-1390d093-0: a port named after
# the JTAG context of device 0 on our cable, whose IDCODE 0x1390D093 is the
# XCKU115 (0x0390D093 in our bitstreams, revision 1) -- a port HOSTED BY our FPGA
# (a chain behind its BSCAN), not a second cable. Seven targets, two "match", and
# the old rule refused the board it had just programmed. So: when EXACTLY two
# targets carry the serial, one is Digilent/<serial> exactly and the other is
# Xilinx/jsn-...-<serial>-<rev>390d093-<n> (the serial as a whole token, the
# XCKU115's IDCODE), both on the one hw_server, that is one board -- the Digilent
# target is opened, never the jsn one (it carries no xcku115). Any other
# multi-match (two Digilent targets, a serial that only CONTAINS ours, another
# device's port, a third match, ...) could be two boards: still refused. On the
# opened target exactly one xcku115 must answer, or nothing is read.
# scripts/mps3_recover.sh applies the same rule.
#
# Usage:  dfx_preflight.sh [overlay_dir | manifest.json | expected_static.bit]
#         dfx_preflight.sh --print-expected <arg>   # resolve identity only, no board access
#   env:  MPS3_CABLE_SERIAL (the JTAG cable to use), VIVADO, HW_URL / MPS3_HW_URL
#
# Preferred form is the OVERLAY being deployed: the manifest's static_usercode
# records the static those partials were built against, so the check is data-driven
# and travels with the artefacts (gen_manifest.py --static-bit). Falls back to
# reading UserID straight out of a full .bit header.
#
# Exit :  0 = MATCH, safe to swap · 1 = MISMATCH, DO NOT SWAP · 2 = could not determine
set -u
VIVADO=${VIVADO:-/research/CAD/Xilinx/Vivado/2025.2/Vivado/bin/vivado}   # must match the hw_server version
HW_URL=${HW_URL:-${MPS3_HW_URL:-localhost:3121}}                      # BARE host:port (no tcp: prefix)
CABLE=${MPS3_CABLE_SERIAL-210249B86C47}                               # this board's JTAG cable serial
PRINT_ONLY=0
if [ "${1:-}" = "--print-expected" ]; then PRINT_ONLY=1; shift; fi
TARGET=${1:-$(cd "$(dirname "$0")/../../../.." && pwd)/fpga/dfx/build_linux/prod/config_rm_greybox.bit}

# 1. expected identity — from an overlay manifest where possible, else a .bit header
MANIFEST=""
case "$TARGET" in
    *.json) MANIFEST=$TARGET ;;
    *)      [ -d "$TARGET" ] && MANIFEST=$TARGET/manifest.json ;;
esac

if [ -n "$MANIFEST" ]; then
    [ -r "$MANIFEST" ] || { echo "PREFLIGHT: FAIL cannot read manifest: $MANIFEST"; exit 2; }
    EXPECT=$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
v=d.get("static_usercode")
print(format(int(str(v),0),"08x") if v is not None else "")' "$MANIFEST" 2>/dev/null)
    SRC="manifest $MANIFEST"
    if [ -z "$EXPECT" ]; then
        cat <<MSG
PREFLIGHT: FAIL $MANIFEST has no "static_usercode".
It predates the guard. Rebuild it with the static these partials were built against:
  gen_manifest.py build ... --static-bit <full_static.bit>
or pass that .bit to this script directly. Do NOT assume the flown static is correct.
MSG
        exit 2
    fi
else
    [ -r "$TARGET" ] || { echo "PREFLIGHT: FAIL cannot read static bitstream: $TARGET"; exit 2; }
    EXPECT=$(head -c 200 "$TARGET" | strings | grep -oE 'UserID=0?[Xx]?[0-9A-Fa-f]+' | head -1 | sed 's/.*=//' | tr 'A-Z' 'a-z' | sed 's/^0x//')
    SRC="$(basename "$TARGET")"
    [ -n "$EXPECT" ] || { echo "PREFLIGHT: FAIL no UserID in $TARGET header"; exit 2; }
fi

if [ "$PRINT_ONLY" = 1 ]; then echo "PREFLIGHT_EXPECTED: 0x$EXPECT (from $SRC)"; exit 0; fi

# 2. actual identity = USERCODE read from the FLOWN device over JTAG -- on THIS
#    board's cable only. The serial goes into a Tcl glob, so it must be plain
#    alphanumerics: anything else (empty included) is refused before any access.
case "$CABLE" in
    ''|*[!A-Za-z0-9]*)
        echo "PREFLIGHT: FAIL MPS3_CABLE_SERIAL='$CABLE' is not a cable serial (letters/digits only) -- refusing to pick a JTAG target"
        exit 2 ;;
esac
TCL=$(mktemp /tmp/dfx_preflight.XXXXXX.tcl)
cat > "$TCL" <<TCLEOF
open_hw_manager
connect_hw_server -url $HW_URL
set all {}
catch {set all [get_hw_targets]}
set mine {}
foreach t \$all { if {[string match "*$CABLE*" [file tail \$t]]} { lappend mine \$t } }
puts "PREFLIGHT_TARGETS: [llength \$all] on the hw_server, [llength \$mine] match cable $CABLE"
set pick ""
if {[llength \$mine] == 1} {
    set pick [lindex \$mine 0]
} elseif {[llength \$mine] == 2} {
    # the one benign pair (header): Digilent/<serial> + Xilinx/jsn-...-<serial>-<rev>390d093-<n>
    set jsn_re {^jsn-.+-}
    append jsn_re $CABLE {-[0-9A-Fa-f]390[dD]093-[0-9]+\$}
    set dig {}
    set jsn {}
    foreach t \$mine {
        set drv [file tail [file dirname \$t]]
        if {\$drv eq "Digilent" && [file tail \$t] eq "$CABLE"} { lappend dig \$t }
        if {\$drv eq "Xilinx" && [regexp \$jsn_re [file tail \$t]]} { lappend jsn \$t }
    }
    if {[llength \$dig] == 1 && [llength \$jsn] == 1 &&
        [file dirname [file dirname [lindex \$dig 0]]] eq [file dirname [file dirname [lindex \$jsn 0]]]} {
        set pick [lindex \$dig 0]
        puts "PREFLIGHT_DUPLICATE: [lindex \$jsn 0] is a port hosted by this board's FPGA -- not opened"
    }
}
if {\$pick eq ""} {
    puts "PREFLIGHT_NO_CABLE: \$all"
    catch {close_hw_manager}
    exit
}
puts "PREFLIGHT_TARGET: \$pick"
open_hw_target \$pick
set devs [get_hw_devices -quiet xcku115*]
if {[llength \$devs] != 1} {
    puts "PREFLIGHT_NO_DEVICE: [llength \$devs] xcku115 on \$pick (devices: [get_hw_devices -quiet])"
    catch {close_hw_target}
    catch {close_hw_manager}
    exit
}
set dev [lindex \$devs 0]
puts "PREFLIGHT_DEVICE: \$dev"
current_hw_device \$dev
refresh_hw_device -quiet \$dev
if {![catch {set uc [get_property REGISTER.USERCODE.SLR0 \$dev]}]} { puts "PREFLIGHT_USERCODE: \$uc" }
catch {close_hw_target}
catch {close_hw_manager}
TCLEOF
OUT=$("$VIVADO" -mode batch -nojournal -notrace -source "$TCL" -log /dev/null 2>&1)
rm -f "$TCL"
if echo "$OUT" | grep -q '^PREFLIGHT_NO_CABLE'; then
    echo "PREFLIGHT: FAIL JTAG cable $CABLE not found (or not unique) on hw_server $HW_URL -- NOT opening any other board's target"
    echo "$OUT" | grep -E '^PREFLIGHT_(TARGETS|NO_CABLE)' | sed 's/^/  /'
    exit 2
fi
TARGET_OPENED=$(echo "$OUT" | grep -oE '^PREFLIGHT_TARGET: .*' | head -1 | sed 's/^PREFLIGHT_TARGET: //')
case "$TARGET_OPENED" in
    *"$CABLE"*) ;;
    *) echo "PREFLIGHT: FAIL no target on cable $CABLE was opened (Vivado said: ${TARGET_OPENED:-nothing})"; exit 2 ;;
esac
echo "$OUT" | grep -E '^PREFLIGHT_DUPLICATE' | sed 's/^/  /'
if echo "$OUT" | grep -q '^PREFLIGHT_NO_DEVICE'; then
    echo "PREFLIGHT: FAIL could not read USERCODE: not exactly one xcku115 on $TARGET_OPENED"
    echo "$OUT" | grep -E '^PREFLIGHT_NO_DEVICE' | sed 's/^/  /'
    exit 2
fi
ACTUAL=$(echo "$OUT" | grep -oE 'PREFLIGHT_USERCODE: 0x[0-9a-fA-F]+' | head -1 | sed 's/.*0x//' | tr 'A-Z' 'a-z')
[ -n "$ACTUAL" ] || { echo "PREFLIGHT: FAIL could not read USERCODE from $TARGET_OPENED (is it configured / hw_server up?)"; exit 2; }

# 3. verdict
if [ "$EXPECT" = "$ACTUAL" ]; then
    echo "PREFLIGHT: MATCH  flown USERCODE=0x$ACTUAL == 0x$EXPECT (from $SRC, cable $CABLE)  -> safe to swap"
    exit 0
fi
cat <<MSG
PREFLIGHT: *** MISMATCH — DO NOT SWAP ***
  flown device USERCODE : 0x$ACTUAL  (cable $CABLE: $TARGET_OPENED)
  partials' static      : 0x$EXPECT  ($SRC)
The board is NOT running the static these partials were built against. Swapping now
would DESTROY the FPGA configuration. Boot the matching static first:
  xsdb src/linux_soc/hw/board_scripts/dfx_swap_boot.tcl
MSG
exit 1
