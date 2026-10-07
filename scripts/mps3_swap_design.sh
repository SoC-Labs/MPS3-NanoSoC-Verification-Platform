#!/usr/bin/env bash
#
# mps3_swap_design.sh — swap the DUT design in the MPS3/KU115 harness, over JTAG.
#
#   scripts/mps3_swap_design.sh <design>        # swap to <design>, verify, release
#   scripts/mps3_swap_design.sh --list          # what can I swap to?
#   scripts/mps3_swap_design.sh <design> --keep-base   # skip the base reload (faster)
#
# e.g.  scripts/mps3_swap_design.sh nanosoc_multicore
#
# WHAT IT DOES
#   1. Takes the fpgahub board lease (the board is shared — never skip this),
#      OR reuses one the caller already holds via MPS3_LEASE_TOKEN (see below).
#   2. Loads the base bitstream (shell + greybox in the RP), unless --keep-base.
#      ^ REQUIRED after any board JTAG reload: a JTAG-loaded shell is VOLATILE,
#        so a power-cycle reverts the board to whatever the config SD holds. If
#        `shell_id` comes back as anything other than the id in
#        docs/FIELDED_SHELL.md, that is what happened — reload the base.
#   3. Clears the resident RM, then loads the new design's partial bitstream.
#      (UltraScale DFX rule: clearing-FIRST, always. Skipping it corrupts the RP.)
#   4. Reads DFXCTL.RM_ID back over JTAG and checks it against the manifest.
#      Under the v2 encoding that register is {major, minor, design_id}, so a
#      correct read proves BOTH which design and which version is live.
#   5. Releases the lease (even on failure — trap EXIT), UNLESS the caller
#      supplied it via MPS3_LEASE_TOKEN, in which case it is the caller's to
#      release and this script leaves it alone.
#
# Env: MPS3_PROD_DIR   where config_rm_<design>_..._partial.bit lives
#      MPS3_BASE_DIR   where config_rm_greybox_fw.bit + greybox clearing live
#                      (BOTH must belong to the SAME locked static as the
#                       partial — compare prod/static_id.txt, and for real
#                       certainty md5sum prod/static_routed_locked.dcp)
#      MPS3_LEASE_TOKEN / MPS3_LEASE_HOLDER   reuse a caller-held lease
#
# WHY JTAG AND NOT THE OVER-THE-WIRE PATH: the `pyverify deploy` flow (push to
# 6910 + swap over 6900) is the intended production path, but as of 2026-07-14 it
# does not work end-to-end — see docs/ and the notes at the bottom of this file.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# No build-dir literal here. The two defaults this file used to carry
# (build_v2enc, build_clcd) named directories that had been dead for two mints
# and do not exist in a fresh clone; pyverify.fielded resolves the FIELDED
# shell's artefact dir from docs/FIELDED_SHELL.md + fielded/<static_id>/, and
# MPS3_PROD_DIR / MPS3_BASE_DIR still override it for a built-but-not-fielded mint.
fielded_prod() {
  PYTHONPATH="$REPO/host/pyverify${PYTHONPATH:+:$PYTHONPATH}" "${PYTHON:-python3}" -c \
    'import sys; from pyverify.fielded import load; sys.stdout.write(str(load().prod_dir))'
}
PROD="${MPS3_PROD_DIR:-$(fielded_prod)}"
BASE_PROD="${MPS3_BASE_DIR:-$PROD}"   # base bit + greybox clearing: SAME locked static
BASE_BIT="$BASE_PROD/config_rm_greybox_fw.bit"
# hw_server address, host:port. MPS3_HW_SERVER wins; otherwise derived from the
# tree's one hub setting, MPS3_HW_URL (tcp:<hub-fqdn>:3121 -- host/socket_harness/
# endpoints.py:65, scripts/mps3_diag.tcl). No site host is baked in: a public tree
# with a literal default that cannot be right anywhere else is a guard that reads
# as a value (docs/HARNESS_REGRESSION.md). Unset => refuse before touching xsdb.
HW_URL="${MPS3_HW_SERVER:-${MPS3_HW_URL#tcp:}}"
[ -n "$HW_URL" ] || { echo "ERROR: set MPS3_HW_URL=tcp:<hub-fqdn>:3121 (or MPS3_HW_SERVER=<hub-fqdn>:3121)" >&2; exit 2; }
VIVADO="${VIVADO:-/apps/Xilinx/Vivado/2024.1/bin/vivado}"
XSDB="${XSDB:-/apps/Xilinx/Vivado/2024.1/bin/xsdb}"
# The JTAG cable filter (a Vivado glob on the target name): this MPS3's serial.
# The hw_server is SHARED -- scripts/mps3_hw_target.tcl never opens "the first".
CABLE="${MPS3_JTAG_CABLE:-*210249B86C47*}"
HOLDER="${MPS3_LEASE_HOLDER:-$(whoami)-swap}"

list_designs() {
  echo "designs (from fpga/dfx/overlay/*/manifest.json):"
  for m in "$REPO"/fpga/dfx/overlay/*/manifest.json; do
    n=$(basename "$(dirname "$m")")
    id=$(grep -oE '"rm_id"[^,]*' "$m" | grep -oE '0x[0-9A-Fa-f]{8}')
    printf '  %-20s rm_id=%s\n' "$n" "$id"
  done
}

[ "${1:-}" = "--list" ] && { list_designs; exit 0; }
DESIGN="${1:?usage: $0 <design> [--keep-base]   (try --list)}"
KEEP_BASE=0; [ "${2:-}" = "--keep-base" ] && KEEP_BASE=1

MANIFEST="$REPO/fpga/dfx/overlay/$DESIGN/manifest.json"
[ -f "$MANIFEST" ] || { echo "no such design: $DESIGN"; list_designs; exit 2; }
WANT_RM_ID=$(grep -oE '"rm_id"[^,]*' "$MANIFEST" | grep -oE '0x[0-9A-Fa-f]{8}')

PARTIAL="$PROD/config_rm_${DESIGN}_pblock_rp_dut_partial.bit"
[ -f "$PARTIAL" ] || { echo "no partial built for $DESIGN at $PARTIAL"; exit 2; }
# UltraScale: clear the RESIDENT RM before loading a new one. After a base load the
# resident RM is greybox, so that is the clearing we use.
CLEAR="$BASE_PROD/config_rm_greybox_pblock_rp_dut_partial_clear.bit"

echo "==> swapping to '$DESIGN'  (expect DFXCTL.RM_ID = $WANT_RM_ID)"

# Lease. MPS3_LEASE_TOKEN lets a CALLER that already holds the board hand its
# token in, which matters for two reasons:
#   * this script's own acquire path is scripts/mps3_board.sh, which is documented
#     (scripts/mps3_lease_acquire.sh's header) NOT to survive a CONTENDED board:
#     it passes --json, and this fpgahub's queued response carries no token, so a
#     queued acquire errors out and strands a queue entry. A caller that waited
#     the board out with mps3_lease_acquire.sh has a perfectly good token already.
#   * without this, a multi-step board session (load base -> swap -> capture) had
#     to drop and retake the lease between steps, and anything could slip in.
# When the token comes from outside, we must NOT release it on exit -- it is not
# ours to release, and the caller's later steps still need it.
if [ -n "${MPS3_LEASE_TOKEN:-}" ]; then
  TOKEN="$MPS3_LEASE_TOKEN"
  echo "    lease supplied by caller ($TOKEN) -- will NOT be released here"
else
  TOKEN=$("$REPO/scripts/mps3_board.sh" acquire "$HOLDER") || {
    echo "could not take the board lease — is fpgahubd up? (ssh \"\$MPS3_HUB\" systemctl status fpgahubd)"
    echo "  if the board is CONTENDED, mps3_board.sh acquire cannot queue; instead:"
    echo "    TOKEN=\$(scripts/mps3_lease_acquire.sh my-holder) \\"
    echo "      && MPS3_LEASE_TOKEN=\$TOKEN MPS3_LEASE_HOLDER=my-holder $0 $DESIGN"
    exit 1; }
  trap '"$REPO/scripts/mps3_board.sh" release "$TOKEN" "$HOLDER" >/dev/null 2>&1 || true' EXIT
  echo "    lease held ($TOKEN)"
fi

TCL=$(mktemp /tmp/mps3_swap.XXXX.tcl)
{
  echo 'open_hw_manager'
  echo "connect_hw_server -url $HW_URL"
  echo "source {$REPO/scripts/mps3_hw_target.tcl}"
  echo "set dev [mps3_open_hw_target {$CABLE}]"
  echo 'refresh_hw_device -update_hw_probes false $dev'
  BITS=""
  [ "$KEEP_BASE" = 0 ] && BITS="{$BASE_BIT} {$CLEAR}" || BITS="{$CLEAR}"
  echo "foreach bit [list $BITS {$PARTIAL}] {"
  echo '  puts "LOADING=$bit"'
  echo '  set_property PROGRAM.FILE $bit $dev'
  echo '  program_hw_devices $dev'
  echo '  refresh_hw_device $dev'
  echo '}'
  echo 'close_hw_target'
  echo 'disconnect_hw_server'
  echo 'exit'
} > "$TCL"

echo "    programming (base + clearing + partial) ..."
"$VIVADO" -mode batch -notrace -source "$TCL" 2>&1 \
  | grep -E 'LOADING=|End of startup|ERROR' | sed 's/^/      /'
rm -f "$TCL"

# Read the id back from the DFX controller.
# ⚠ Pick the MicroBlaze that is a DESCENDANT OF THE xcku115. Do NOT take the
# lowest-numbered "MicroBlaze #0": several boards on this hw_server expose one
# (one has FOUR), and the enumeration order CHANGES between sessions. The
# lowest-id heuristic silently reads the WRONG board -- every register comes back
# 0x00000000, which looks exactly like "greybox resident" and will have you
# debugging a swap that actually succeeded.
RTCL=$(mktemp /tmp/mps3_rmid.XXXX.tcl)
cat > "$RTCL" <<EOF
connect -url tcp:$HW_URL
after 2500
set mbid ""
set seen_ku 0
foreach line [split [targets] "\n"] {
  if {[regexp {xcku115} \$line]} { set seen_ku 1 ; continue }
  if {\$seen_ku && [regexp {([0-9]+)\**\s+MicroBlaze #0} \$line -> id]} { set mbid \$id ; break }
}
if {\$mbid eq ""} { puts "GOT_RM_ID=NO_KU115_MICROBLAZE" ; exit 1 }
targets \$mbid
stop
puts "GOT_RM_ID=[format 0x%08X [mrd -value 0x44A10010]]"
con
disconnect
exit
EOF
GOT=$("$XSDB" "$RTCL" 2>/dev/null | grep -oE 'GOT_RM_ID=0x[0-9A-F]{8}' | cut -d= -f2)
rm -f "$RTCL"

echo
if [ "${GOT,,}" = "${WANT_RM_ID,,}" ]; then
  echo "==> OK: DFXCTL.RM_ID = $GOT   ($DESIGN, v$((0x${GOT:2:2})).$((0x${GOT:4:2})))"
  exit 0
fi
echo "==> MISMATCH: DFXCTL.RM_ID = ${GOT:-<no read>}, expected $WANT_RM_ID"
echo "    If the shell reports an id other than the one in docs/FIELDED_SHELL.md,"
echo "    the board rebooted onto whatever the config SD holds — rerun without --keep-base."
exit 1

# ---------------------------------------------------------------------------
# KNOWN GAPS (2026-07-14) — why this uses JTAG rather than the network path:
#
#  1. The over-the-wire flow (`pyverify deploy`, push 6910 + swap 6900) does NOT
#     work end-to-end yet. Two defects found:
#       a) The firmware Makefile defaults HWICAP_FIFO unset (lite-mode ICAP
#          writers), but the shipped shell builds axi_hwicap with C_MODE=0 =
#          FIFO (shell_bd.tcl). The mismatch makes the swap HANG. Building with
#          HWICAP_FIFO=1 fixes the hang -- the swap then *answers*.
#       b) With (a) fixed the swap is still REJECTED (ok=false). Not root-caused.
#     Also: pyverify's ShellClient defaults to a 5 s timeout, which a real swap
#     (streaming a clearing + partial into the ICAP) cannot meet -- the shell
#     PARKS the control connection for the whole swap. It only passes in tests
#     because the FakeShell answers instantly.
#
#  2. A JTAG-loaded shell is VOLATILE: a power-cycle reverts the board to
#     whatever the config SD holds. Persisting it needs the `sd_install` path
#     (now `pyverify sd write`, via scripts/mps3_sd_update.sh).
# ---------------------------------------------------------------------------
