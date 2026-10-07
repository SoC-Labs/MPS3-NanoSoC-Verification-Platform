#!/usr/bin/env bash
# mps3_rp_reset_release.sh — release the DUT/RP resets after a JTAG partial load,
# then PROVE the release took.
#
#   scripts/mps3_rp_reset_release.sh          # release + verify
#   scripts/mps3_rp_reset_release.sh --check  # verify only, no write
#
# WHY THIS EXISTS
#   A JTAG partial load does NOT release rp_resetn -- only a shell-mediated ICAP
#   swap does (which is why an OTW swap of eth_ss let the bring-up FSM run and a
#   JTAG partial of the same RM did not). So after `scripts/mps3_swap_design.sh`
#   the RM is resident but the DUT is HELD IN RESET, and anything functional
#   fails for THAT reason rather than because the RM is broken. On 2026-07-30
#   that cost 22 minutes of IICE readout capturing a reset vector, and the trace
#   looked exactly like a wedged CPU. scripts/mps3_state.sh diagnoses the state;
#   this script fixes it.
#
#   The write itself is one register. The VALUE of the script is the verification
#   after it: a blind `mwr` tells you nothing, and DECOUPLE.en force-holds
#   rp_resetn low regardless of RESET_CTRL, so a write can be accepted and have
#   no effect. We therefore assert on the CONSEQUENCES, not on the write.
#
# WHAT IT ASSERTS (docs/contracts/shell-regmap.md)
#   write CLKRST 0x44A0_0000 RESET_CTRL = 0x7   [0]dut_resetn [1]rp_resetn [2]dbg_resetn
#   then, all of:
#     RESET_CTRL      reads back 0x7 in bits [2:0]
#     DFXCTL.STATUS   0x44A1_0008 bit[1] rp_in_reset  == 0   (CLEARED)
#     DFXCTL.RM_STATUS 0x44A1_0014 bit[0] rm_id_valid == 1   (SET, i.e. the id is
#                                                             stable, not merely 0)
#   Exit 0 only if every one of those holds. Exit 1 otherwise, naming which.
#
# THIS SCRIPT WRITES TO THE BOARD. Hold the lease first.
#   scripts/mps3_board.sh preflight   (or supply MPS3_LEASE_TOKEN via the caller)
set -uo pipefail

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# hw_server address, host:port. MPS3_HW_SERVER wins; otherwise derived from the
# tree's one hub setting, MPS3_HW_URL (tcp:<hub-fqdn>:3121 -- host/socket_harness/
# endpoints.py:65, scripts/mps3_diag.tcl). No site host is baked in: a public tree
# with a literal default that cannot be right anywhere else is a guard that reads
# as a value (docs/HARNESS_REGRESSION.md). Unset => refuse before touching xsdb.
HW_URL="${MPS3_HW_SERVER:-${MPS3_HW_URL#tcp:}}"
[ -n "$HW_URL" ] || { echo "ERROR: set MPS3_HW_URL=tcp:<hub-fqdn>:3121 (or MPS3_HW_SERVER=<hub-fqdn>:3121)" >&2; exit 2; }
XSDB="${XSDB:-/apps/Xilinx/Vivado/2024.1/bin/xsdb}"

[ -x "$XSDB" ] || { echo "ERROR: no xsdb at $XSDB" >&2; exit 1; }

TCL=$(mktemp "${TMPDIR:-/tmp}/mps3_rst.XXXX.tcl")
trap 'rm -f "$TCL"' EXIT

# ⚠ THE SHARED-hw_server TRAP. Pick the MicroBlaze that is a DESCENDANT OF THE
# xcku115. Four other boards live on this hw_server, several expose a
# "MicroBlaze #0" (one has FOUR), and the enumeration order CHANGES between
# sessions. Taking the lowest-numbered one silently drives the WRONG board:
# every register reads 0x00000000, which is indistinguishable from "greybox
# resident" / "still in reset" -- so a bogus PASS or a bogus FAIL, with no way
# to tell. This walk is deliberately IDENTICAL to the one in
# scripts/mps3_state.sh and scripts/mps3_swap_design.sh;
# tests/silicon/test_silicon_sweep_plan.py asserts all three stay byte-identical
# so a fix to one can never leave the others behind.
cat > "$TCL" <<EOF
connect -url tcp:$HW_URL
after 2500
set mbid ""
set seen_ku 0
foreach line [split [targets] "\n"] {
  if {[regexp {xcku115} \$line]} { set seen_ku 1 ; continue }
  if {\$seen_ku && [regexp {([0-9]+)\**\s+MicroBlaze #0} \$line -> id]} { set mbid \$id ; break }
}
if {\$mbid eq ""} { puts "ERR=NO_KU115_MICROBLAZE" ; exit 1 }
targets \$mbid
stop
puts "PRE_RESET_CTRL=[format 0x%08X [mrd -value 0x44A00000]]"
puts "PRE_STATUS=[format 0x%08X [mrd -value 0x44A10008]]"
if { $CHECK_ONLY == 0 } {
  mwr 0x44A00000 0x7
  after 200
}
puts "RESET_CTRL=[format 0x%08X [mrd -value 0x44A00000]]"
puts "DECOUPLE=[format 0x%08X [mrd -value 0x44A10000]]"
puts "STATUS=[format 0x%08X [mrd -value 0x44A10008]]"
puts "RM_ID=[format 0x%08X [mrd -value 0x44A10010]]"
puts "RM_STATUS=[format 0x%08X [mrd -value 0x44A10014]]"
con
disconnect
exit
EOF

OUT=$("$XSDB" "$TCL" 2>/dev/null \
      | grep -E '^(PRE_RESET_CTRL|PRE_STATUS|RESET_CTRL|DECOUPLE|STATUS|RM_ID|RM_STATUS|ERR)=')

if printf '%s' "$OUT" | grep -q '^ERR='; then
  echo "$OUT" >&2
  echo "FAIL: no MicroBlaze descendant of the xcku115 on $HW_URL." >&2
  echo "      The shell is probably not loaded (it is JTAG-VOLATILE; a power-cycle" >&2
  echo "      reverts the board to the OLDER shell on the SD card). Reload the base." >&2
  exit 1
fi
[ -n "$OUT" ] || { echo "FAIL: no registers read (hw_server at $HW_URL?)" >&2; exit 1; }

printf '%s\n' "$OUT"

get()  { printf '%s' "$OUT" | grep "^$1=" | cut -d= -f2; }
bit()  { echo $(( ( $(printf '%d' "$1") >> $2 ) & 1 )); }

RC_V="$(get RESET_CTRL)"; DC="$(get DECOUPLE)"; ST="$(get STATUS)"
RMS="$(get RM_STATUS)";  RMID="$(get RM_ID)"

echo
echo "---- verdict ----"
bad=0

if [ "$(( $(printf '%d' "$RC_V") & 0x7 ))" = 7 ]; then
  echo "  OK   RESET_CTRL[2:0] = 0b111 (dut_resetn|rp_resetn|dbg_resetn RELEASED)"
else
  echo "  FAIL RESET_CTRL = $RC_V -- bits[2:0] did not read back as 0b111"
  bad=1
fi

if [ "$(bit "$ST" 1)" = 0 ]; then
  echo "  OK   DFXCTL.STATUS[1] rp_in_reset CLEARED"
else
  echo "  FAIL DFXCTL.STATUS[1] rp_in_reset is STILL SET ($ST)"
  if [ "$(bit "$DC" 0)" = 1 ]; then
    echo "       CAUSE: DECOUPLE.en is SET ($DC) -- it force-holds rp_resetn low"
    echo "              regardless of RESET_CTRL. The RP is still isolated from the"
    echo "              shell, i.e. a swap did not complete. Reload the base."
  fi
  bad=1
fi

if [ "$(bit "$RMS" 0)" = 1 ]; then
  echo "  OK   RM_STATUS[0] rm_id_valid SET -- RM_ID $RMID is STABLE, not just readable"
else
  echo "  FAIL RM_STATUS[0] rm_id_valid CLEAR ($RMS) -- RM_ID $RMID is not certified"
  echo "       stable by the shell's stability detector, so a matching readback"
  echo "       proves nothing."
  bad=1
fi

[ "$(bit "$RMS" 1)" = 0 ] || echo "  WARN RM_STATUS[1] dut_lockup is SET -- the DUT core is in lockup"

if [ "$bad" = 0 ]; then
  echo "OK: DUT/RP out of reset with a stable RM_ID ($RMID)"
  exit 0
fi
echo "FAIL: reset release did not take"
exit 1
