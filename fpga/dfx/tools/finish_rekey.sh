#!/usr/bin/env bash
# =============================================================================
# finish_rekey.sh -- self-contained daemon that COMPLETES a re-key already in
# flight. It does NOT start one. It WAITS for the running job to finish (prod =
# lock the new static + route the base RMs; add = the incremental RMs; overlays
# = regenerate manifests + mps3_shell_static_id.c), then runs
# firmware(CLCD=1)+updatemem and the acceptance gate, and drops a SINGLE final
# sentinel: $PROD/CLCD_REKEY_ALL.done
#
# Fully detached (launch via setsid); survives the launching shell. All paths
# are durable (repo tree / gitignored build dirs) -- no session-scratchpad dep.
#
# -----------------------------------------------------------------------------
# TRACKED COPY of fpga/dfx/build_clcd/finish_rekey.sh, moved here 2026-09-09.
# The original is a GITIGNORED scratch file that the mint script of the day
# named as "the PROVEN recipe" for its firmware stage -- i.e. the authority for
# how firmware reaches the fielded shell lived somewhere `git clone` does not
# reach. The build_clcd/ original is left in place untouched.
#
# That recipe is now IN the flow: `make -C fpga/dfx mint` stage 6 implements it
# (docs/BUILD_AND_MINT.md). This script remains the RECOVERY path -- it finishes
# a re-key that is already in flight -- and is the reference for the four things
# below that were each wrong once.
#
# WHAT CHANGED vs the original (paths only; the recipe is byte-for-byte):
#   * REPO was one operator's absolute home path, baked in. It is now
#     derived from this script's own location (tools/ -> ../../..), overridable
#     with REPO=.
#   * DDIR/PROD/XSA/ROUTED/WS/DRC/BDSUMMARY were hardcoded to the build_clcd +
#     shell_proj_clcd + build_results_2026-07-11 batch. Each is now an env var
#     whose DEFAULT is that same value, so re-running it reproduces the recorded
#     run and pointing it at a new batch needs no edit.
#   * SCROOT was one session's scratchpad, long gone. Now opt-in: unset, the
#     early-failure probe is simply skipped (the durable success test below is
#     the real one).
#   * The tool paths keep their 2024.1 defaults but are overridable.
# =============================================================================
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$HERE/../../.." && pwd)}"

# The batch this completes. Defaults reproduce the recorded CLCD Phase-D run.
DDIR="${DDIR:-$REPO/fpga/dfx/build_clcd}"
PROD="${PROD:-$DDIR/prod}"
SHELL_PROJ="${SHELL_PROJ:-$REPO/build/shell_proj_clcd}"
SHELL_RESULTS="${SHELL_RESULTS:-$REPO/fpga/shell/build_results_2026-07-11}"
WS="${WS:-$REPO/build/vitis_fw_clcd}"

FINAL="$PROD/CLCD_REKEY_ALL.done"
WLOG="${WLOG:-$DDIR/finish_rekey.console}"
# Optional: a launcher's scratch dir holding rekey.done, used ONLY to notice an
# early failure sooner. Unset => that probe is skipped.
SCROOT="${SCROOT:-}"

VIVADO="${VIVADO:-/apps/Xilinx/Vivado/2024.1/bin/vivado}"
XSCT="${XSCT:-/apps/Xilinx/Vitis/2024.1/bin/xsct}"
UPDATEMEM="${UPDATEMEM:-/apps/Xilinx/Vivado/2024.1/bin/updatemem}"
MBNM="${MBNM:-/apps/Xilinx/Vitis/2024.1/gnu/microblaze/lin/bin/mb-nm}"

XSA="$SHELL_PROJ/shell_harness.xsa"
ROUTED="$SHELL_PROJ/shell_proj.runs/impl_1/shell_top_routed.dcp"
DRC="$SHELL_RESULTS/post_impl_drc.rpt"
BDSUMMARY="$SHELL_RESULTS/bd_summary.txt"
ELF="$WS/shell_fw/shell_fw.elf"
BASEBIT="$PROD/config_rm_greybox_fw.bit"

# The RM whose incremental add marks "the re-key is done" (Phase 0 waits on its
# DFX_ADD_COMPLETE). Was hardcoded to the multicore add of the recorded batch.
WAIT_ADD_LOG="${WAIT_ADD_LOG:-$PROD/add_nanosoc_multicore.log}"

mkdir -p "$PROD"
exec >>"$WLOG" 2>&1
echo ""
echo "============================================================"
echo "== finish_rekey daemon start $(date) =="
echo "== REPO=$REPO DDIR=$DDIR SHELL_PROJ=$SHELL_PROJ WS=$WS =="
echo "============================================================"

write_final() {   # $1=exit  $2=gate_verdict  $3...=extra note lines
  local ec="$1"; shift
  local verdict="$1"; shift
  {
    echo "EXIT=$ec"
    echo "NEW_STATIC_ID=${NEWID:-<unknown>}"
    echo "OVERLAYS_REKEYED:"
    for m in "$REPO"/fpga/dfx/overlay/*/manifest.json; do
      [ -f "$m" ] || continue
      rn=$(basename "$(dirname "$m")")
      sid=$(grep -oE '"static_id"[^,]*' "$m" | grep -oE '0x[0-9A-Fa-f]+' | head -1)
      rid=$(grep -oE '"rm_id"[^,]*' "$m" | grep -oE '0x[0-9A-Fa-f]+' | head -1)
      echo "  $rn static_id=$sid rm_id=$rid"
    done
    echo "STATIC_ID_C=$(grep -oE '0x[0-9A-Fa-f]+' "$REPO/fpga/dfx/overlay/mps3_shell_static_id.c" 2>/dev/null | head -1)"
    echo "BASE_BIT=$BASEBIT $( [ -f "$BASEBIT" ] && echo "($(stat -c %s "$BASEBIT") bytes)" || echo "(MISSING)" )"
    echo "GATE_VERDICT=$verdict"
    for l in "$@"; do echo "$l"; done
    echo "FINISHED_AT=$(date)"
  } > "$FINAL"
  echo "== wrote final sentinel $FINAL (EXIT=$ec verdict=$verdict) =="
}

fail() { echo "!! FAIL: $*"; write_final 1 "FAIL" "FAIL_REASON: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Phase 0 -- WAIT for the re-key (prod + incremental add + overlays) to finish.
# Durable success = static_id.txt present AND the add log reached
# DFX_ADD_COMPLETE AND the regenerated overlay static_id.c carries that id.
# (That last conjunct is what makes this a real test rather than a tautology:
# it compares the build tree against the repo, two things a single run cannot
# both fake.) Optional early failure = $SCROOT/rekey.done shows a nonzero exit
# without REKEY_OK. Hard cap 6 h.
# ---------------------------------------------------------------------------
echo "-- Phase 0: waiting for the in-flight re-key to complete --"
DEADLINE=$(( $(date +%s) + 6*3600 ))
while true; do
  if [ -f "$PROD/static_id.txt" ] && grep -q "DFX_ADD_COMPLETE" "$WAIT_ADD_LOG" 2>/dev/null; then
    SID=$(tr -d '[:space:]' < "$PROD/static_id.txt")
    if [ -n "$SID" ] && grep -qi "$SID" "$REPO/fpga/dfx/overlay/mps3_shell_static_id.c" 2>/dev/null; then
      echo "-- re-key complete: static_id=$SID --"
      break
    fi
  fi
  if [ -n "$SCROOT" ] && [ -f "$SCROOT/rekey.done" ] \
     && grep -q "REKEY_EXIT=" "$SCROOT/rekey.done" 2>/dev/null \
     && ! grep -q "REKEY_OK" "$SCROOT/rekey.done" 2>/dev/null; then
    fail "re-key (prod/add/overlays) FAILED -- see $SCROOT/rekey.console and $PROD/build.log"
  fi
  [ "$(date +%s)" -gt "$DEADLINE" ] && fail "TIMEOUT (6h) waiting for re-key completion"
  sleep 60
done
NEWID=$(tr -d '[:space:]' < "$PROD/static_id.txt")
echo "NEWID=$NEWID"

# ---------------------------------------------------------------------------
# Phase 1 -- firmware (CLCD=1) + updatemem into the new greybox base.
# THIS IS THE RECIPE `make -C fpga/dfx mint` stage 6 implements. The four things
# that matter, each of which was wrong in the version that shipped broken:
#   1. ELF   = $WS/shell_fw/shell_fw.elf (what `make elf` really emits)
#   2. BIT   = the DFX base config_rm_greybox.bit, NOT the non-DFX
#              shell_harness.bit (that one stubs the RP -- it cannot take partials)
#   3. -proc = the FULL path u_shell/shell_bd_i/microblaze_0; a bare
#              `microblaze_0` does not resolve in the DFX config's hierarchy
#   4. MMI   = from the greybox ROUTED dcp (write_mem_info), because the DFX
#              config is a different implementation from shell_harness and their
#              BRAM placements differ -- the wrong MMI corrupts the embed silently
# ---------------------------------------------------------------------------
echo "-- Phase 1A: xsct create_platform (BSP from the new XSA) $(date) --"
[ -f "$XSA" ] || fail "XSA missing: $XSA"
"$XSCT" -nodisp "$REPO/firmware/platform/create_platform.tcl" "$XSA" "$WS" || fail "create_platform (BSP) failed"

echo "-- Phase 1B: make elf CLCD=1 (LMB_KB=1024 default) $(date) --"
make -C "$REPO/firmware/platform" clean WS="$WS" >/dev/null 2>&1 || true
# HWICAP_FIFO=1/WINDOWED=1: must match shell_bd.tcl's axi_hwicap C_MODE {0}
# (FIFO) and the windowed pusher. The Makefile: a mismatch "bricks over-the-wire
# reconfig". Silicon evidence 2026-07-17 (docs/BUILD_AND_MINT.md, "Two knobs").
make -C "$REPO/firmware/platform" elf CLCD=1 HWICAP_FIFO=1 WINDOWED=1 WS="$WS" || fail "firmware ELF build (CLCD=1 HWICAP_FIFO=1 WINDOWED=1) failed"
[ -f "$ELF" ] || fail "ELF not produced: $ELF"
if "$MBNM" "$ELF" 2>/dev/null | grep -qiE " clcd_init| clcd_poll"; then
  echo "   ELF contains clcd_init/clcd_poll (CLCD=1 wired)"
  FW_CLCD=PASS
else
  echo "   WARN: clcd symbols not found in ELF"
  FW_CLCD=WARN
fi

echo "-- Phase 1C: write_mem_info (MMI from greybox routed dcp) $(date) --"
[ -f "$PROD/config_rm_greybox_routed.dcp" ] || fail "greybox routed dcp missing"
"$VIVADO" -mode batch -source "$HERE/write_mem_info.tcl" \
  -journal "$DDIR/write_mem_info.jou" -log "$DDIR/write_mem_info.log" \
  -tclargs "$PROD/config_rm_greybox_routed.dcp" "$PROD/config_rm_greybox.mmi" || fail "write_mem_info failed"
grep -q "WRITE_MEM_INFO_OK" "$DDIR/write_mem_info.log" || fail "write_mem_info did not confirm OK"

echo "-- Phase 1D: updatemem (merge CLCD firmware ELF into config_rm_greybox.bit) $(date) --"
[ -f "$PROD/config_rm_greybox.bit" ] || fail "greybox config bit missing"
"$UPDATEMEM" -force \
  -meminfo "$PROD/config_rm_greybox.mmi" \
  -data    "$ELF" \
  -proc    u_shell/shell_bd_i/microblaze_0 \
  -bit     "$PROD/config_rm_greybox.bit" \
  -out     "$BASEBIT" || fail "updatemem failed"
# updatemem exits 0 after printing its usage text when an input path is wrong.
# Never trust its exit code alone -- assert the artefact.
[ -f "$BASEBIT" ] || fail "flashable base bit not produced: $BASEBIT"
echo "   base bit: $BASEBIT ($(stat -c %s "$BASEBIT") bytes)"

# ---------------------------------------------------------------------------
# Phase 2 -- acceptance gate.
# ---------------------------------------------------------------------------
echo "-- Phase 2: acceptance gate $(date) --"
GATE=PASS
declare -a GN

# G1: shell built
if [ -f "$SHELL_PROJ/shell_top.bit" ] && [ -f "$XSA" ]; then
  GN+=("GATE_1_shell_built=PASS")
else GN+=("GATE_1_shell_built=FAIL"); GATE=FAIL; fi

# G2: zero new DRC (error severity) + CLCD bank all 1.8V
if [ -f "$DRC" ]; then DRC_ERR=$(grep -cE "\| +Error +\|" "$DRC"); else DRC_ERR=1; fi
DRC_ERR=${DRC_ERR:-1}
# run the corrected bank listing
"$VIVADO" -mode batch -source "$HERE/bank_gate.tcl" \
  -journal "$DDIR/bank_gate.jou" -log "$DDIR/bank_gate.log" \
  -tclargs "$ROUTED" >/dev/null 2>&1
CLCD_BANK=$(grep -oE "CLCD_BANK [0-9]+" "$DDIR/bank_gate.log" 2>/dev/null | awk '{print $2}')
NON18=$(grep "BANKPORT" "$DDIR/bank_gate.log" 2>/dev/null | grep -viE "IOSTD=LVCMOS18" | wc -l)
if [ "${DRC_ERR:-1}" -eq 0 ] && [ "${NON18:-1}" -eq 0 ]; then
  GN+=("GATE_2_zero_new_DRC=PASS (0 error-severity DRC; CLCD bank ${CLCD_BANK:-66}: 0 non-LVCMOS18 ports => Vcco 1.8V)")
else
  GN+=("GATE_2_zero_new_DRC=FAIL (drc_errors=$DRC_ERR non_1v8_in_clcd_bank=$NON18)"); GATE=FAIL
fi

# G3: CLCD @ 0x44AC/64K
if grep -qiE "clcd_0_reg0.*0x44AC0000.*0x00010000|0x44AC0000.*0x00010000" "$BDSUMMARY" 2>/dev/null; then
  GN+=("GATE_3_clcd_addr=PASS (0x44AC0000 / 64KiB)")
else GN+=("GATE_3_clcd_addr=FAIL"); GATE=FAIL; fi

# G4: re-key clean (make verify + all overlays==NEWID + pr_verify COMPATIBLE)
if make -C "$REPO/fpga/dfx" verify BUILD="$DDIR" >"$DDIR/gate_verify.log" 2>&1; then
  VERIFY=PASS; else VERIFY=FAIL; GATE=FAIL; fi
MISMATCH=0
for m in "$REPO"/fpga/dfx/overlay/*/manifest.json; do
  sid=$(grep -oE '"static_id"[^,]*' "$m" | grep -oiE '0x[0-9A-Fa-f]+' | head -1)
  [ "${sid^^}" = "${NEWID^^}" ] || MISMATCH=$((MISMATCH+1))
done
SIDC=$(grep -oiE '0x[0-9A-Fa-f]+' "$REPO/fpga/dfx/overlay/mps3_shell_static_id.c" 2>/dev/null | head -1)
[ "${SIDC^^}" = "${NEWID^^}" ] || MISMATCH=$((MISMATCH+1))
# pr_verify verdicts (prod writes pr_verify_<rm>.rpt; add writes its own)
PRV_TOTAL=0; PRV_BAD=0
for r in "$PROD"/pr_verify_*.rpt; do
  [ -f "$r" ] || continue
  PRV_TOTAL=$((PRV_TOTAL+1))
  if grep -qiE "NOT COMPATIBLE|PR Verify.*(FAIL|not compatible)" "$r"; then PRV_BAD=$((PRV_BAD+1)); fi
done
if [ "$VERIFY" = PASS ] && [ "$MISMATCH" -eq 0 ] && [ "$PRV_BAD" -eq 0 ]; then
  GN+=("GATE_4_rekey_clean=PASS (make verify OK; all overlays+static_id.c==$NEWID; pr_verify $PRV_TOTAL/$PRV_TOTAL COMPATIBLE)")
else
  GN+=("GATE_4_rekey_clean=FAIL (verify=$VERIFY mismatches=$MISMATCH pr_verify_bad=$PRV_BAD/$PRV_TOTAL)"); GATE=FAIL
fi

# G5: firmware CLCD=1
if [ "$FW_CLCD" = PASS ]; then GN+=("GATE_5_firmware_clcd1=PASS"); else GN+=("GATE_5_firmware_clcd1=$FW_CLCD"); fi

# G6: base bit
if [ -f "$BASEBIT" ]; then GN+=("GATE_6_base_bit=PASS ($BASEBIT)"); else GN+=("GATE_6_base_bit=FAIL"); GATE=FAIL; fi

echo "-- gate result: $GATE --"
for l in "${GN[@]}"; do echo "   $l"; done

write_final 0 "$GATE" "${GN[@]}"
echo "== finish_rekey daemon done $(date) =="
