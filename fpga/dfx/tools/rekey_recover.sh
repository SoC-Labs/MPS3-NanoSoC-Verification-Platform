#!/usr/bin/env bash
# =============================================================================
# rekey_recover.sh -- RECOVER a base re-key that died part-way (the recorded
# case: SIGTERM after 6 of 7 routes), WITHOUT re-minting static_id. It reuses
# the already-locked static, so every overlay already keyed to it stays valid.
# Completes ONLY the base re-key; a finish_rekey.sh daemon waiting in its
# Phase 0 then does firmware/updatemem/gate.
#
# WHY IT MATTERS: the obvious recovery -- re-run `make prod` -- re-mints
# static_id and strands every overlay on the board. This one does not.
#
# Steps:
#   1. finish_partials.tcl -> partials for the already-routed RMs + the greybox
#      FULL config bit + overlay_inputs.txt rows.  [reuse routed dcps, no re-route]
#   2. stage the incremental RM's OOC dcp.
#   3. build_dfx.tcl INCREMENTAL add (DFX_REUSE_LOCKED) -> routes them against
#      the locked static, pr_verify, partials, append rows, DFX_ADD_COMPLETE.
#      [keeps static_id]
#   4. make overlays && verify -> triples + mps3_shell_static_id.c on that id.
# Launch as a TRUE setsid session leader so it survives process cycles.
#
# -----------------------------------------------------------------------------
# TRACKED COPY of fpga/dfx/build_clcd/rekey_recover.sh, moved here 2026-09-09.
# The original is left in place untouched.
#
# WHAT CHANGED vs the original (paths + one guard; the recipe is unchanged):
#   * REPO was one operator's absolute home path, baked in. Now derived from
#     this script's location (tools/ -> ../../..), overridable with REPO=.
#   * DDIR/MC_SRC were pinned to build_clcd + build_1m. Now env vars defaulting
#     to those same values.
#   * The two 0xE4B1C44A literals were the fielded static IN JULY 2026 and are
#     now three mints stale (the shell fielded 2026-08-10 is recorded in
#     docs/FIELDED_SHELL.md). Hardcoding it made the guard a landmine: run this
#     against any later tree and it aborts on a correct static_id, while a run
#     against a tree that HAD drifted would have passed had the literal happened
#     to match. The guard now reads the id from the tree's own static_id.txt and
#     asserts the WHOLE POINT of a recovery -- that the id did not change --
#     against EXPECT_STATIC_ID if you choose to pin one.
# =============================================================================
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$HERE/../../.." && pwd)}"

DDIR="${DDIR:-$REPO/fpga/dfx/build_clcd}"
PROD="${PROD:-$DDIR/prod}"
VIVADO="${VIVADO:-/apps/Xilinx/Vivado/2024.1/bin/vivado}"
RP_INST="${RP_INST:-u_rp_dut}"
WLOG="${WLOG:-$DDIR/rekey_recover.console}"
STATUS="${STATUS:-$DDIR/rekey_recover.status}"

# The RM(s) to fold in incrementally, and where to find the staged OOC dcp.
ADD_RMS="${ADD_RMS:-rm_eth_ss rm_nanosoc_multicore}"
MC_SRC="${MC_SRC:-$REPO/fpga/dfx/build_1m/rm_nanosoc_multicore_synth/rm_nanosoc_multicore_synth.dcp}"
MC_ALT="${MC_ALT:-$REPO/fpga/dfx/build_1m/prod/rm_nanosoc_multicore_synth.dcp}"
ADD_LOG_BASE="${ADD_LOG_BASE:-add_nanosoc_multicore}"

# Optional belt-and-braces: pin the static_id you EXPECT this tree to carry.
# Leave unset and the run adopts whatever the tree says (and still proves, at
# the end, that it did not change).
EXPECT_STATIC_ID="${EXPECT_STATIC_ID:-}"

mkdir -p "$PROD"
exec >>"$WLOG" 2>&1
echo ""; echo "============================================================"
echo "== rekey_recover start $(date) =="
echo "== REPO=$REPO DDIR=$DDIR ADD_RMS=$ADD_RMS =="
echo "============================================================"

bail() { echo "!! RECOVER FAIL: $*"; echo "FAIL $* ($(date))" > "$STATUS"; exit 1; }

[ -f "$PROD/static_id.txt" ] || bail "no $PROD/static_id.txt -- this tree was never locked; there is nothing to recover"
SID=$(tr -d '[:space:]' < "$PROD/static_id.txt")
[ -n "$SID" ] || bail "empty static_id.txt in $PROD"
if [ -n "$EXPECT_STATIC_ID" ] && [ "${SID^^}" != "${EXPECT_STATIC_ID^^}" ]; then
  bail "tree static_id '$SID' != EXPECT_STATIC_ID '$EXPECT_STATIC_ID' -- refuse to proceed"
fi
echo "-- recovering tree at static_id=$SID (will be REUSED, never recomputed) --"
[ -f "$PROD/static_routed_locked.dcp" ] || bail "locked static missing"
[ -f "$PROD/config_rm_greybox_routed.dcp" ] || bail "greybox routed dcp missing"

# ---- Step 1: partials for the already-routed RMs + greybox full bit ----
echo "-- Step 1: finish_partials (routed RMs -> partials + greybox full bit) $(date) --"
"$VIVADO" -mode batch -source "$HERE/finish_partials.tcl" \
  -journal "$DDIR/finish_partials.jou" -log "$DDIR/finish_partials.log" \
  -tclargs "$REPO" "$PROD" "$RP_INST" ${BASE_RMS:-} || bail "finish_partials.tcl failed"
grep -q "FINISH_PARTIALS_OK" "$DDIR/finish_partials.log" || bail "finish_partials did not confirm OK"
[ -f "$PROD/config_rm_greybox.bit" ] || bail "greybox full config bit not produced"
echo "   base partials + config_rm_greybox.bit done"

# ---- Step 2: stage the incremental RM's OOC dcp (reuse, no re-synth) ----
echo "-- Step 2: stage rm_nanosoc_multicore OOC dcp --"
if   [ -f "$MC_SRC" ]; then cp -f "$MC_SRC" "$PROD/rm_nanosoc_multicore_synth.dcp";
elif [ -f "$MC_ALT" ]; then cp -f "$MC_ALT" "$PROD/rm_nanosoc_multicore_synth.dcp";
else bail "multicore OOC dcp not found ($MC_SRC / $MC_ALT)"; fi
echo "   staged $(ls -la "$PROD/rm_nanosoc_multicore_synth.dcp")"

# ---- Step 3: incremental add (reuse locked static) ----
echo "-- Step 3: incremental add {$ADD_RMS} vs locked static $(date) --"
cd "$PROD" || bail "cannot cd $PROD"
DFX_ADD_RMS="$ADD_RMS" \
DFX_REUSE_LOCKED="$PROD/static_routed_locked.dcp" \
DFX_STATIC_ID_FILE="$PROD/static_id.txt" \
DFX_REF_ROUTED="$PROD/config_rm_greybox_routed.dcp" \
"$VIVADO" -mode batch -source "$REPO/fpga/dfx/build_dfx.tcl" \
  -journal "$PROD/$ADD_LOG_BASE.jou" -log "$PROD/$ADD_LOG_BASE.log" \
  -tclargs "$REPO" "$PROD" "$PROD/static_routed_locked.dcp" "" "$RP_INST" || bail "incremental add failed"
grep -q "DFX_ADD_COMPLETE" "$PROD/$ADD_LOG_BASE.log" || bail "incremental add did not reach DFX_ADD_COMPLETE"
echo "   $ADD_RMS routed + partials + rows appended"

# ---- Step 4: overlays + verify (re-key manifests + static_id.c to $SID) ----
echo "-- Step 4: make overlays + verify $(date) --"
make -C "$REPO/fpga/dfx" overlays BUILD="$DDIR" || bail "make overlays failed"
make -C "$REPO/fpga/dfx" verify   BUILD="$DDIR" || bail "make verify failed"

# The recovery's OWN claim, checked: the id must be the one we started from
# (a recovery that re-minted is a failed recovery, however green it looks) and
# the regenerated overlay static_id.c must carry it.
SID_AFTER=$(tr -d '[:space:]' < "$PROD/static_id.txt")
[ "${SID_AFTER^^}" = "${SID^^}" ] || bail "static_id CHANGED during recovery ($SID -> $SID_AFTER) -- the static was re-minted; every fielded overlay is now stale"
grep -qi "$SID" "$REPO/fpga/dfx/overlay/mps3_shell_static_id.c" || bail "overlay static_id.c not re-keyed to $SID"

N=$(ls -d "$REPO"/fpga/dfx/overlay/*/ 2>/dev/null | wc -l)
echo "== RECOVER OK: base re-key complete, $N overlays keyed to $SID; overlay_inputs rows=$(grep -cvE '^#' "$PROD/overlay_inputs.txt") =="
echo "OK static_id=$SID overlays=$N ($(date))" > "$STATUS"
echo "== rekey_recover done $(date); a waiting finish_rekey.sh will now do firmware/updatemem/gate =="
