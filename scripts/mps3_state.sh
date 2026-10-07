#!/usr/bin/env bash
# mps3_state.sh — read the harness's own state and say what it MEANS.
#
#   scripts/mps3_state.sh            # read + interpret
#   scripts/mps3_state.sh --quiet    # machine-readable KEY=VALUE only
#
# WHY THIS EXISTS
#   On 2026-07-30 a silicon IICE capture came back with HADDR constant at
#   0x00000004 and HTRANS permanently IDLE. That reads exactly like a wedged CPU,
#   and the recorded expectation was that the M0 "boots and executes
#   continuously" -- so it looked like a major finding. It was not: the DUT was
#   HELD IN RESET, because a JTAG partial load does not release rp_resetn (only a
#   shell-mediated ICAP swap does). 22 minutes of readout and a chunk of a board
#   window went into capturing a reset vector.
#
#   The probe set could not have told us -- the as-built IICE has no hresetn. The
#   answer was three register reads away the whole time. So: read them FIRST,
#   every time, and print the interpretation rather than the hex.
#
#   READ-ONLY with respect to the REGISTERS: it writes none of them and takes no
#   lease.
#
#   BUT IT IS *NOT* SAFE TO RUN CONCURRENTLY WITH SOMEONE ELSE'S WORK, and an
#   earlier version of this header wrongly said it was. It does `stop`/`con` the
#   MicroBlaze around the reads (the same pattern scripts/mps3_swap_design.sh
#   uses), and scripts/mps3_board.sh's header records a `stop` having DESTROYED A
#   LIVE 1.31 MB TRANSFER. So: take the lease first, then preflight. Order matters.
#
#   EXIT CODE IS A "READY TO CAPTURE" VERDICT, NOT A HEALTH CHECK -- do not gate a
#   sweep on it. It returns 1 when the DUT is held in reset or RM_ID == 0, and
#   both are perfectly normal states to START from: greybox straight after a base
#   load IS RM_ID == 0, and a JTAG partial load ALWAYS leaves the DUT in reset.
#   Gating a regression on this would refuse to begin on a healthy board. Use
#   `--quiet` and interpret the KEY=VALUEs yourself; --quiet exits non-zero only
#   when the KU115's MicroBlaze cannot be found, which is the genuine refusal.
#   (Both points found by the silicon-sweep work, which does exactly that.)
#
# REGISTERS (docs/contracts/shell-regmap.md)
#   CLKRST 0x44A0_0000  RESET_CTRL  [0] dut_resetn [1] rp_resetn [2] dbg_resetn
#                                   1 = RELEASED
#   DFXCTL 0x44A1_0000  DECOUPLE    [0] decouple_en (1 = RP isolated)
#          0x44A1_0008  STATUS      [0] decoupled [1] rp_in_reset
#          0x44A1_0010  RM_ID       the RP's rm_id partition pin
#          0x44A1_0014  RM_STATUS   [0] rm_id_valid [1] dut_lockup [2] dut_eth_irq
set -u

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

# hw_server address, host:port. MPS3_HW_SERVER wins; otherwise derived from the
# tree's one hub setting, MPS3_HW_URL (tcp:<hub-fqdn>:3121 -- host/socket_harness/
# endpoints.py:65, scripts/mps3_diag.tcl). No site host is baked in: a public tree
# with a literal default that cannot be right anywhere else is a guard that reads
# as a value (docs/HARNESS_REGRESSION.md). Unset => refuse before touching xsdb.
HW_URL="${MPS3_HW_SERVER:-${MPS3_HW_URL#tcp:}}"
[ -n "$HW_URL" ] || { echo "ERROR: set MPS3_HW_URL=tcp:<hub-fqdn>:3121 (or MPS3_HW_SERVER=<hub-fqdn>:3121)" >&2; exit 2; }
XSDB="${XSDB:-/apps/Xilinx/Vivado/2024.1/bin/xsdb}"

[ -x "$XSDB" ] || { echo "ERROR: no xsdb at $XSDB" >&2; exit 1; }

TCL=$(mktemp /tmp/mps3_state.XXXX.tcl)
trap 'rm -f "$TCL"' EXIT

# ⚠ Pick the MicroBlaze that is a DESCENDANT OF THE xcku115. Several boards on
# this shared hw_server expose a "MicroBlaze #0" and the enumeration order
# changes between sessions; the lowest-id heuristic silently reads the WRONG
# board and every register comes back 0x00000000 -- which is indistinguishable
# from "greybox resident". Same walk as scripts/mps3_swap_design.sh.
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
puts "RESET_CTRL=[format 0x%08X [mrd -value 0x44A00000]]"
puts "DECOUPLE=[format 0x%08X [mrd -value 0x44A10000]]"
puts "STATUS=[format 0x%08X [mrd -value 0x44A10008]]"
puts "RM_ID=[format 0x%08X [mrd -value 0x44A10010]]"
puts "RM_STATUS=[format 0x%08X [mrd -value 0x44A10014]]"
con
disconnect
exit
EOF

OUT=$("$XSDB" "$TCL" 2>/dev/null | grep -E '^(RESET_CTRL|DECOUPLE|STATUS|RM_ID|RM_STATUS|ERR)=')
if echo "$OUT" | grep -q '^ERR='; then
  echo "$OUT" >&2
  echo "Could not find the KU115's MicroBlaze. Is the shell loaded? A power-cycle" >&2
  echo "reverts to the SD shell, and our shell is JTAG-VOLATILE -- reload the base." >&2
  exit 1
fi
[ -n "$OUT" ] || { echo "ERROR: no registers read (hw_server at $HW_URL?)" >&2; exit 1; }

echo "$OUT"
[ "$QUIET" = 1 ] && exit 0

get() { echo "$OUT" | grep "^$1=" | cut -d= -f2; }
hex2d() { printf '%d' "$1"; }

RC=$(hex2d "$(get RESET_CTRL)")
DC=$(hex2d "$(get DECOUPLE)")
ST=$(hex2d "$(get STATUS)")
RMID=$(get RM_ID)
RMS=$(hex2d "$(get RM_STATUS)")

bit() { echo $(( ( $1 >> $2 ) & 1 )); }

echo
echo "---- interpretation ----------------------------------------------------"

DUT_RST=$(bit $RC 0); RP_RST=$(bit $RC 1); DBG_RST=$(bit $RC 2)
printf "  dut_resetn  %s\n" "$([ "$DUT_RST" = 1 ] && echo RELEASED || echo 'ASSERTED (DUT held in reset)')"
printf "  rp_resetn   %s\n" "$([ "$RP_RST"  = 1 ] && echo RELEASED || echo 'ASSERTED (RP held in reset)')"
printf "  dbg_resetn  %s\n" "$([ "$DBG_RST" = 1 ] && echo RELEASED || echo ASSERTED)"

RP_IN_RESET=$(bit $ST 1)
DECOUPLED=$(bit $ST 0)
[ "$DECOUPLED" = 1 ] && echo "  DFXCTL      RP is DECOUPLED (isolated from the shell)"
[ "$(bit $DC 0)" = 1 ] && echo "  DECOUPLE.en is SET -- rp_resetn is force-held low regardless of RESET_CTRL"

echo "  RM_ID       $RMID  $(case "$RMID" in
  0x00000000) echo '(greybox / no RM, or the WRONG board was read)';;
  0x01000001) echo '(nanosoc v1.0)';;
  0x01000002) echo '(eth_ss)';;
  0x01000003) echo '(nanosoc_multicore)';;
  0x01000005) echo '(nanosoc_upy)';;
  0x0100001E) echo '(led)';;
  *) echo '(see fpga/dfx/overlay/*/manifest.json)';; esac)"
printf "  RM_STATUS   rm_id_valid=%d dut_lockup=%d dut_eth_irq=%d\n" \
  "$(bit $RMS 0)" "$(bit $RMS 1)" "$(bit $RMS 2)"

echo
echo "---- VERDICT -----------------------------------------------------------"
RC=0
if [ "$RP_IN_RESET" = 1 ] || [ "$RP_RST" = 0 ] || [ "$DUT_RST" = 0 ]; then
  cat <<'MSG'
  *** THE DUT IS HELD IN RESET ***
  Any trace captured now shows RESET VALUES, not execution. An IICE capture will
  look like a wedged CPU (constant address, HTRANS IDLE) and it will be lying.

  A JTAG partial load does NOT release rp_resetn -- only a shell-mediated ICAP
  swap does. To release by hand, over the config TAP:
      mwr 0x44A00000 0x7      # dut_resetn | rp_resetn | dbg_resetn
  then re-read: DFXCTL.STATUS[1] must clear and RM_STATUS[0] (rm_id_valid) set.

  This state IS however exactly what a coherent configuration READBACK wants:
  every DUT flip-flop is static despite the running clock (host/readback/).
MSG
  RC=1
elif [ "$RMID" = "0x00000000" ]; then
  cat <<'MSG'
  *** NO RM LOADED (RM_ID = 0) ***
  Either greybox is resident, or the shell was reverted by a power-cycle (our
  shell is JTAG-VOLATILE; the SD card holds an older one), or the wrong board was
  read. Load a design: scripts/mps3_swap_design.sh <design>
MSG
  RC=1
else
  echo "  DUT is out of reset with $RMID resident. A capture now reflects execution."
  [ "$(bit $RMS 1)" = 1 ] && { echo "  WARNING: dut_lockup is SET -- the CPU is in lockup."; RC=1; }
fi
exit $RC
