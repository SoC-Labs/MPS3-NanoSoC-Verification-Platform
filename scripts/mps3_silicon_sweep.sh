#!/usr/bin/env bash
#
# mps3_silicon_sweep.sh — the scripted SILICON regression. For EVERY
# reconfigurable module in the RM library: swap it into the real KU115, verify
# DFXCTL.RM_ID against that RM's manifest, record the result, and CARRY ON to
# the next one. Emits a machine-readable JSON summary plus a human table, and
# exits non-zero if any RM failed.
#
#   scripts/mps3_silicon_sweep.sh                        # DRY RUN (default; touches nothing)
#   scripts/mps3_silicon_sweep.sh --allow-board          # the real thing
#   scripts/mps3_silicon_sweep.sh --rms led,regdemo_a    # a subset
#   scripts/mps3_silicon_sweep.sh --json /tmp/sweep.json
#
# OPTIONS
#   --allow-board          actually touch the board. WITHOUT THIS NOTHING IS
#                          TOUCHED: no lease, no hw_server, no bitstream.
#   --dry-run              explicit dry run (the default)
#   --prod DIR             prod dir holding config_rm_<rm>_..._partial.bit
#   --base DIR             prod dir holding the base bitstream + greybox clearing.
#                          MUST be the SAME locked static as --prod; checked.
#   --overlay-dir DIR      the RM catalogue (default fpga/dfx/overlay)
#   --rms a,b,c            sweep only these (refuses on an unknown name)
#   --skip a,b             exclude these (refuses if it empties the sweep)
#   --max N                cap the number of RMs; prints what it dropped. 0 = no cap.
#   --json PATH            write the machine-readable summary here
#   --logdir DIR           keep per-step logs here (default: a temp dir)
#   --holder NAME          fpgahub lease holder (default <user>-silicon-sweep)
#   --require-artefacts    a missing partial is a FAILURE, not a skip. Use this on
#                          a lab box, where every partial MUST exist.
#   --strict-manifest-static  refuse when a manifest's static_id != the prod dir's
#                          (default: warn -- see the reasoning at the check)
#   --no-release-reset     do NOT release rp_resetn after each swap (weaker verdict)
#   --ping                 also read shell_id off TCP 6900 (needs the dataplane)
#   --no-restore           leave the LAST swept RM resident instead of greybox
#
# ENV: MPS3_PROD_DIR MPS3_BASE_DIR MPS3_OVERLAY_DIR MPS3_LEASE_HOLDER
#      MPS3_LEASE_TOKEN (reuse a caller-held lease; then it is NOT released here)
#      MPS3_BOARD_HOST MPS3_HW_SERVER XSDB VIVADO PY
#
# WHY THIS EXISTS
#   "Does the platform still work on real hardware?" has been a manual
#   afternoon: load a base, remember that the clearing goes FIRST, remember that
#   the SD card holds an older shell, read a register, squint at it, repeat nine
#   times, lose the lease somewhere in the middle. This makes it one command with
#   an audit trail.
#
# WHAT IT IS *NOT*
#   This proves CONFIGURATION, not FUNCTION. A PASS means "that RM's partial
#   landed in the RP and the fabric is driving the rm_id this repo says it
#   should" — it does NOT mean the DUT boots, the MAC links, or MicroPython
#   answers. Those are per-RM functional gates (scripts/harness_gates/
#   console_check.py, swd_check.py, the tier-3 gates in harness_regression.sh)
#   and are deliberately NOT folded in here: a sweep that tries to do everything
#   for nine RMs cannot be trusted or finished in one board window. Read the
#   NOT-PROVEN block this script prints at the end; it is not boilerplate.
#
# RELATIONSHIP TO scripts/harness_regression.sh
#   harness_regression.sh tier 3 is a DEEP, ORDERED smoke of a NEWLY MINTED
#   harness image: four hand-picked RMs, real functional joins (console, SWD),
#   over the OVER-THE-WIRE pyverify/ICAP path, and it STOPS LOUDLY at the first
#   failing gate (that is the right shape for "should this image ship at all?").
#   This script is the complementary BREADTH sweep: every RM, over JTAG, with
#   per-RM isolation so one bad RM cannot hide the other eight. Neither
#   subsumes the other; run tier 0..2 first, then this, then tier 3 if a
#   functional join is in question.
#
# COMPOSES (does not reimplement)
#   scripts/mps3_lease_acquire.sh  — poll-based acquire that survives contention
#   scripts/mps3_swap_design.sh    — base + clearing-FIRST + partial + RM_ID readback
#   scripts/mps3_state.sh --quiet  — read-only board state (the preflight)
#   scripts/mps3_rp_reset_release.sh — release rp_resetn after a JTAG partial
#   scripts/harness_gates/ping_check.py — optional shell_id identity (--ping)
#   fpga/dfx/overlay/*/manifest.json — the RM catalogue (rm_id per design)
#
# ---------------------------------------------------------------------------
# THE FIVE TRAPS THIS SCRIPT ENCODES (all learned the hard way on this board)
# ---------------------------------------------------------------------------
# 1. THE SHELL IS JTAG-VOLATILE. The SD card holds an OLDER shell; any
#    power-cycle or `fpgahub reset --method mcc` silently reverts to it. So the
#    base is reloaded at the START of every step. We therefore NEVER pass
#    --keep-base — see trap 2 for the second, independent reason.
#
# 2. `mps3_swap_design.sh --keep-base` IS UNSAFE IN A SWEEP. That script's
#    clearing bitstream is hard-wired to GREYBOX's
#    (config_rm_greybox_pblock_rp_dut_partial_clear.bit), which is correct
#    exactly when the resident RM is greybox — i.e. straight after a base load.
#    With --keep-base on step N>1 the resident RM is the PREVIOUS design, so
#    greybox's clearing would be used to clear something else: an UltraScale DFX
#    violation that corrupts the RP. Full base reload per RM costs ~1 min of
#    JTAG and removes the whole failure class. Do not "optimise" it away.
#
# 3. A JTAG PARTIAL LOAD DOES NOT RELEASE rp_resetn (only a shell-mediated ICAP
#    swap does). The DUT is left HELD IN RESET. RM_ID still reads correctly —
#    it is a wrapper localparam tie-off across the partition boundary, not a
#    resettable register — so the verdict is sound either way. But anything
#    FUNCTIONAL would fail for the reset, not for the RM. So by default we
#    release it (`mwr 0x44A00000 0x7`) and re-verify that DFXCTL.STATUS[1]
#    clears and RM_STATUS[0] rm_id_valid sets, which upgrades the claim from
#    "the bitstream loaded" to "the RM is out of reset with a STABLE id".
#    --no-release-reset opts out.
#
# 4. THE hw_server IS SHARED WITH FOUR OTHER BOARDS. Every register read here
#    goes through mps3_state.sh / mps3_swap_design.sh, which walk `targets` for
#    the MicroBlaze that is a DESCENDANT of the xcku115. Never the
#    lowest-numbered "MicroBlaze #0" — that belongs to another board and returns
#    all-zeros, which is INDISTINGUISHABLE from "greybox resident". Which is
#    exactly why trap 5 exists.
#
# 5. GREYBOX CANNOT BE VERIFIED BY AN RM_ID READBACK ALONE. greybox's rm_id is
#    0x00000000 — and so is the DFX decoupler's DECOUPLED_VALUE, and so is a
#    read of the WRONG board on the shared hw_server, and so is a shell that
#    reverted. "Swap to greybox, assert RM_ID == 0" therefore passes for at
#    least four wrong reasons: it is structurally unfalsifiable, and this repo
#    treats a gate that cannot fail as worse than no gate. So every zero-id RM
#    is verified as a TRANSITION instead: it is scheduled AFTER a non-zero RM,
#    and it PASSES only if the immediately-preceding step read a NON-ZERO id
#    from this same board in this same session and this step then reads zero.
#    Without that positive control its verdict is UNPROVABLE, which counts as a
#    failure. Look for verify=transition in the table.
#
# ---------------------------------------------------------------------------
# EXIT CODES
#   0  every selected RM verified (board mode), or the plan is coherent (dry run)
#   1  at least one RM FAILED, was UNPROVABLE, or the plan is incoherent
#      (bad manifest / colliding rm_id / --require-artefacts with none built)
#   2  REFUSED — the sweep never started (bad dirs, static mismatch, no lease,
#      board unreachable, base load failed). Nothing downstream is meaningful,
#      so no per-RM verdicts are claimed.
# ---------------------------------------------------------------------------
#
# NOT -e ON PURPOSE: per-RM isolation is the whole point. One bad RM must not
# abort the sweep, so failures are captured per step and reported, never fatal.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-python3}"

# --- knobs (mirror scripts/mps3_swap_design.sh so the two never disagree) ----
PROD="${MPS3_PROD_DIR:-$REPO/fpga/dfx/build_v2enc/prod}"
BASE="${MPS3_BASE_DIR:-$REPO/fpga/dfx/build_clcd/prod}"
OVERLAY_DIR="${MPS3_OVERLAY_DIR:-$REPO/fpga/dfx/overlay}"
HOLDER="${MPS3_LEASE_HOLDER:-$(whoami)-silicon-sweep}"
BOARD_HOST="${MPS3_BOARD_HOST:-192.168.10.101}"

ALLOW_BOARD=0
RELEASE_RESET=1
DO_PING=0
DO_RESTORE=1
REQUIRE_ARTEFACTS=0
STRICT_MANIFEST_STATIC=0
WANT_RMS=""
SKIP_RMS=""
MAXN=0
JSON_OUT=""
LOGDIR=""

# Print the header block, however long it grows. A hard `sed -n '2,140p'` silently
# spilled the arg-parser into --help output once the traps were documented; this
# stops at the first non-comment line instead, so it cannot drift again.
usage() { awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --allow-board)        ALLOW_BOARD=1; shift ;;
    --dry-run)            ALLOW_BOARD=0; shift ;;
    --no-release-reset)   RELEASE_RESET=0; shift ;;
    --ping)               DO_PING=1; shift ;;
    --no-restore)         DO_RESTORE=0; shift ;;
    --require-artefacts)  REQUIRE_ARTEFACTS=1; shift ;;
    --strict-manifest-static) STRICT_MANIFEST_STATIC=1; shift ;;
    --rms)                WANT_RMS="$2"; shift 2 ;;
    --skip)               SKIP_RMS="$2"; shift 2 ;;
    --max)                MAXN="$2"; shift 2 ;;
    --json)               JSON_OUT="$2"; shift 2 ;;
    --prod)               PROD="$2"; shift 2 ;;
    --base)               BASE="$2"; shift 2 ;;
    --overlay-dir)        OVERLAY_DIR="$2"; shift 2 ;;
    --holder)             HOLDER="$2"; shift 2 ;;
    --logdir)             LOGDIR="$2"; shift 2 ;;
    -h|--help)            usage; exit 0 ;;
    *) echo "unknown arg: $1  (try --help)" >&2; exit 2 ;;
  esac
done

MODE="dry-run"; [ "$ALLOW_BOARD" = 1 ] && MODE="board"

# Scratch: per-step logs + the record file the JSON is rendered from.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mps3_sweep.XXXXXX")"
[ -n "$LOGDIR" ] || LOGDIR="$WORK/logs"
mkdir -p "$LOGDIR"
REC="$WORK/records.tsv"
: > "$REC"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
hr()   { printf '%s\n' "-----------------------------------------------------------------------"; }
say()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*"; }
bytes() { [ -f "$1" ] && stat -c %s "$1" 2>/dev/null || echo -1; }
commas() { $PY -c 'import sys;n=int(sys.argv[1]);print("%s"%("{:,}".format(n) if n>=0 else "absent"))' "$1"; }

# int(x,0)-equivalent normalisation to 0xXXXXXXXX, matching the manifest
# consumers (host/pyverify/pyverify/overlay.py:_parse_int) rather than inventing
# a third parser. Manifests in this tree mix "0xA1B2C3D4" and underscore-grouped
# "0x0000_0001".
norm_id() { $PY -c 'import sys
v=sys.argv[1].strip()
try: print("0x%08X"%int(v,0))
except Exception: print("BAD")' "$1"; }

read_static_id() {  # read_static_id <prod-dir> -> 0xXXXXXXXX | ""
  local f="$1/static_id.txt"
  [ -f "$f" ] || { echo ""; return; }
  norm_id "$(tr -d ' \t\r\n' < "$f")"
}

# TSV record. Fields are positional and consumed by the python JSON renderer
# below; keep them in step.
#  1 rm  2 expected_rm_id  3 verify_mode  4 plan_status  5 plan_reason
#  6 partial  7 partial_bytes  8 manifest_static  9 run_status 10 run_reason
# 11 got_rm_id 12 duration_s 13 log
rec() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$@" >> "$REC"; }

# ---------------------------------------------------------------------------
say "======================================================================="
say " MPS3 / KU115 SILICON SWEEP                        mode: $MODE"
say "======================================================================="
say "  repo        $REPO"
say "  overlays    $OVERLAY_DIR"
say "  prod        $PROD"
say "  base        $BASE"
say "  holder      $HOLDER"
say "  logs        $LOGDIR"
say ""

# ---------------------------------------------------------------------------
# PLAN PHASE — entirely board-free, always runs, and is where nearly every
# real-world failure is actually catchable. A dry run is this phase plus the
# printed command sequence.
# ---------------------------------------------------------------------------
say "==== PLAN: resolve artefacts + validate the RM catalogue ===="

REFUSE=0
refuse() { say "!!! REFUSE: $*"; REFUSE=1; }

for d in "$OVERLAY_DIR" "$PROD" "$BASE"; do
  [ -d "$d" ] || refuse "no such directory: $d"
done

if [ "$REFUSE" = 0 ]; then
  # The base and the partials MUST belong to the SAME locked static. Two
  # independent comparisons, per scripts/mps3_swap_design.sh's header: the cheap
  # one (static_id.txt) and the one that cannot be faked (the md5 of
  # static_routed_locked.dcp). A mismatch means the partials were routed against
  # a different static's frames; loading them is how you corrupt an RP.
  PROD_SID="$(read_static_id "$PROD")"
  BASE_SID="$(read_static_id "$BASE")"
  say "  static_id   prod=${PROD_SID:-<missing static_id.txt>}  base=${BASE_SID:-<missing static_id.txt>}"
  if [ -z "$PROD_SID" ] || [ -z "$BASE_SID" ]; then
    refuse "cannot read static_id.txt from both prod and base; refusing to guess"
  elif [ "$PROD_SID" != "$BASE_SID" ]; then
    refuse "prod static_id $PROD_SID != base static_id $BASE_SID -- the base and the
            partials belong to DIFFERENT locked statics. Loading them together is the
            RP-corruption case scripts/mps3_swap_design.sh's header warns about."
  fi

  PROD_DCP="$PROD/static_routed_locked.dcp"
  BASE_DCP="$BASE/static_routed_locked.dcp"
  if [ -f "$PROD_DCP" ] && [ -f "$BASE_DCP" ]; then
    PM="$(md5sum "$PROD_DCP" | cut -d' ' -f1)"
    BM="$(md5sum "$BASE_DCP" | cut -d' ' -f1)"
    if [ "$PM" = "$BM" ]; then
      say "  locked dcp  md5 MATCH ($PM) -- same locked static, not merely the same id"
    else
      refuse "static_routed_locked.dcp md5 DIFFERS: prod=$PM base=$BM.
            static_id.txt can agree while the checkpoints do not (a re-run that
            happened to hash the same id, a hand-copied file). The md5 is the
            authority -- refusing."
    fi
  else
    # Not fatal: a staged/trimmed prod dir legitimately ships without the dcp.
    # Say so by name rather than passing silently.
    warn "no static_routed_locked.dcp to compare (prod:$([ -f "$PROD_DCP" ] && echo yes || echo no) base:$([ -f "$BASE_DCP" ] && echo yes || echo no));"
    warn "     relying on static_id.txt alone. Stage the dcp for a stronger check."
  fi

  BASE_BIT="$BASE/config_rm_greybox_fw.bit"
  CLEAR_BIT="$BASE/config_rm_greybox_pblock_rp_dut_partial_clear.bit"
  [ -f "$BASE_BIT" ]  || warn "base bitstream absent: $BASE_BIT (NOT_BUILT for every RM)"
  [ -f "$CLEAR_BIT" ] || warn "greybox clearing absent: $CLEAR_BIT (NOT_BUILT for every RM)"
fi

if [ "$REFUSE" = 1 ]; then
  say ""
  say "  candidate prod dirs in this tree (those carrying a static_id.txt):"
  found=0
  while IFS= read -r f; do
    printf '    %-58s %s\n' "$(dirname "$f")" "$(tr -d ' \t\r\n' < "$f")"; found=1
  done < <(find "$REPO/fpga/dfx" -maxdepth 3 -name static_id.txt 2>/dev/null | sort)
  [ "$found" = 1 ] || say "    (none -- nothing has been implemented in this worktree)"
  say ""
  say "  Point --prod/--base (or MPS3_PROD_DIR/MPS3_BASE_DIR) at ONE of them."
  say "  Build dirs are gitignored, so a fresh clone/worktree has none."
  say ""
  say "SWEEP REFUSED (exit 2). Nothing was touched."
  exit 2
fi

# --- enumerate the RM catalogue --------------------------------------------
ALL_RMS=()
while IFS= read -r m; do ALL_RMS+=("$(basename "$(dirname "$m")")"); done \
  < <(find "$OVERLAY_DIR" -mindepth 2 -maxdepth 2 -name manifest.json | sort)

if [ "${#ALL_RMS[@]}" = 0 ]; then
  say "!!! REFUSE: no $OVERLAY_DIR/*/manifest.json -- there is no RM catalogue to sweep."
  say "    (run 'make -C fpga/dfx overlays')"
  exit 2
fi
say "  catalogue   ${#ALL_RMS[@]} RM(s): ${ALL_RMS[*]}"

# --- selection (and say out loud what selection removed) -------------------
in_csv() { case ",$2," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
SEL=(); EXCLUDED=()
for rm in "${ALL_RMS[@]}"; do
  if [ -n "$WANT_RMS" ] && ! in_csv "$rm" "$WANT_RMS"; then
    EXCLUDED+=("$rm:not-in---rms"); continue; fi
  if [ -n "$SKIP_RMS" ] && in_csv "$rm" "$SKIP_RMS"; then
    EXCLUDED+=("$rm:--skip"); continue; fi
  SEL+=("$rm")
done
if [ -n "$WANT_RMS" ]; then
  # A typo in --rms must not silently shrink the sweep to nothing.
  for want in ${WANT_RMS//,/ }; do
    hit=0; for rm in "${ALL_RMS[@]}"; do [ "$rm" = "$want" ] && hit=1; done
    [ "$hit" = 1 ] || { say "!!! REFUSE: --rms names '$want', which is not in the catalogue."; exit 2; }
  done
fi
# A sweep of ZERO RMs exits 0 having verified nothing -- the canonical
# gate-that-cannot-fail. Refuse rather than report a vacuous success.
if [ "${#SEL[@]}" = 0 ]; then
  say "!!! REFUSE: --rms/--skip selected ZERO RMs out of ${#ALL_RMS[@]}."
  say "    A sweep of no RMs would exit 0 having verified nothing."
  say "    catalogue: ${ALL_RMS[*]}"
  exit 2
fi
# Likewise a --max of 0-or-less would schedule nothing. (--max 0 means "no cap".)
if [ "$MAXN" -lt 0 ]; then
  say "!!! REFUSE: --max $MAXN schedules nothing. Use --max 0 (or omit it) for no cap."
  exit 2
fi

# --- read each manifest ----------------------------------------------------
# Parallel arrays indexed together (bash 4.4; no assoc-array ordering games).
declare -a P_RM P_ID P_VERIFY P_STATUS P_REASON P_PARTIAL P_BYTES P_MSTATIC
STALE_MANIFESTS=()
for rm in "${SEL[@]}"; do
  man="$OVERLAY_DIR/$rm/manifest.json"
  info="$($PY - "$man" <<'EOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("ERR\tunparseable manifest: %s" % e); raise SystemExit(0)
def pid(v):
    try: return "0x%08X" % int(str(v).strip(), 0)
    except Exception: return "BAD"
rid = pid(d.get("rm_id", ""))
sid = pid(d.get("static_id", ""))
name = d.get("rm_name", "")
if rid == "BAD":
    print("ERR\tno parseable rm_id in manifest"); raise SystemExit(0)
print("OK\t%s\t%s\t%s" % (rid, sid, name))
EOF
)"
  kind="$(printf '%s' "$info" | cut -f1)"
  if [ "$kind" != "OK" ]; then
    P_RM+=("$rm"); P_ID+=("BAD"); P_VERIFY+=("-"); P_STATUS+=("BAD_MANIFEST")
    P_REASON+=("$(printf '%s' "$info" | cut -f2)"); P_PARTIAL+=("-"); P_BYTES+=(-1); P_MSTATIC+=("-")
    continue
  fi
  rid="$(printf '%s' "$info" | cut -f2)"
  sid="$(printf '%s' "$info" | cut -f3)"
  mname="$(printf '%s' "$info" | cut -f4)"

  # rm_name must match the directory, or --rms/--skip and the RM_ID lookup
  # tables disagree about which design you asked for.
  [ "$mname" = "$rm" ] || warn "$rm: manifest rm_name='$mname' != directory name '$rm'"

  partial="$PROD/config_rm_${rm}_pblock_rp_dut_partial.bit"
  b="$(bytes "$partial")"

  verify="readback"
  [ "$rid" = "0x00000000" ] && verify="transition"

  status="READY"; reason="-"
  if [ "$b" -lt 0 ]; then
    status="NOT_BUILT"; reason="$(basename "$partial") not in the prod dir"
  elif [ ! -f "$BASE/config_rm_greybox_fw.bit" ] || \
       [ ! -f "$BASE/config_rm_greybox_pblock_rp_dut_partial_clear.bit" ]; then
    status="NOT_BUILT"; reason="base bitstream and/or greybox clearing absent in the base dir"
  fi
  if [ "$sid" != "$PROD_SID" ]; then
    # Deliberately a WARN, not a block, and here is exactly why:
    #   * what actually gets LOADED is the .bit out of $PROD, which we have
    #     already proven belongs to the same locked static as the base;
    #   * the EXPECTATION we check is rm_id, which rm_list.tcl assigns at
    #     RM-DESIGN time ("assigned HERE ... not computed from the bitstream")
    #     and which `make check` stage 2 already gates three ways
    #     (wrapper localparam == rm_list.tcl == manifest, check_rm_id_encoding.py).
    #   * the manifest's static_id matters to the OVER-THE-WIRE pusher, which
    #     refuses on mismatch -- not to this JTAG path, which never reads the
    #     manifest's payloads at all.
    # So the rm_id is still trustworthy; the manifest is simply keyed to an
    # older shell. --strict-manifest-static escalates this to a block.
    if [ "$STRICT_MANIFEST_STATIC" = 1 ]; then
      status="STALE_MANIFEST"; reason="manifest static_id $sid != prod $PROD_SID (--strict-manifest-static)"
    else
      # Collected and reported ONCE below -- nine identical WARN lines is how a
      # real warning gets scrolled past.
      STALE_MANIFESTS+=("$rm:$sid")
    fi
  fi

  P_RM+=("$rm"); P_ID+=("$rid"); P_VERIFY+=("$verify"); P_STATUS+=("$status")
  P_REASON+=("$reason"); P_PARTIAL+=("$partial"); P_BYTES+=("$b"); P_MSTATIC+=("$sid")
done

# --- stale-manifest report (once, not per RM) ------------------------------
if [ "${#STALE_MANIFESTS[@]}" -gt 0 ]; then
  say ""
  warn "${#STALE_MANIFESTS[@]} of ${#SEL[@]} manifest(s) are keyed to a DIFFERENT static than the prod dir:"
  for e in "${STALE_MANIFESTS[@]}"; do
    printf '        %-20s manifest static_id %s != prod %s\n' "${e%%:*}" "${e##*:}" "$PROD_SID"
  done
  warn "  This is a WARNING, not a block, and the reason is specific:"
  warn "    * what gets LOADED is the .bit out of the prod dir, already proven to"
  warn "      belong to the same locked static as the base (static_id + dcp md5);"
  warn "    * what gets CHECKED is rm_id, which rm_list.tcl assigns at RM-DESIGN"
  warn "      time (\"assigned HERE ... not computed from the bitstream\") and which"
  warn "      make check stage 2 already gates three ways (check_rm_id_encoding.py:"
  warn "      wrapper localparam == rm_list.tcl == manifest);"
  warn "    * a manifest's static_id gates the OVER-THE-WIRE pusher, which refuses"
  warn "      on mismatch -- not this JTAG path, which never reads the manifest's"
  warn "      payloads at all."
  warn "  So the rm_id expectations are sound; the manifests simply describe an"
  warn "  older shell. Re-mint them with 'make -C fpga/dfx overlays' against"
  warn "  $PROD_SID, or pass --strict-manifest-static to refuse instead."
fi

# --- rm_id collision check -------------------------------------------------
# The ONLY thing this sweep verifies is DFXCTL.RM_ID. If two selected RMs share
# an id, that verification cannot tell them apart -- the check would pass with
# the wrong RM resident, i.e. it structurally cannot fail. Catch it here.
DUPS="$($PY - <<'EOF' "${P_ID[@]}"
import sys, collections
ids = sys.argv[1:]
c = collections.Counter(i for i in ids if i not in ("BAD",))
print(" ".join(k for k, v in c.items() if v > 1))
EOF
)"
if [ -n "$DUPS" ]; then
  for i in "${!P_RM[@]}"; do
    for d in $DUPS; do
      if [ "${P_ID[$i]}" = "$d" ]; then
        P_STATUS[$i]="DUP_RM_ID"
        P_REASON[$i]="rm_id $d is shared with another selected RM -- an RM_ID readback cannot distinguish them"
      fi
    done
  done
fi

# --- schedule: non-zero ids FIRST, zero-id (transition) RMs LAST -----------
# Trap 5. A zero-id RM is only provable as a transition from a non-zero read, so
# it must never be scheduled first. Stable within each group.
ORDER=()
for i in "${!P_RM[@]}"; do [ "${P_VERIFY[$i]}" = "transition" ] || ORDER+=("$i"); done
for i in "${!P_RM[@]}"; do [ "${P_VERIFY[$i]}" = "transition" ] && ORDER+=("$i"); done

# --- cap (and print exactly what the cap removed) --------------------------
CAPPED=()
if [ "$MAXN" -gt 0 ] && [ "${#ORDER[@]}" -gt "$MAXN" ]; then
  KEEP=("${ORDER[@]:0:$MAXN}")
  for i in "${ORDER[@]:$MAXN}"; do CAPPED+=("${P_RM[$i]}"); done
  ORDER=("${KEEP[@]}")
fi

# --- the plan table --------------------------------------------------------
say ""
printf '  %-20s %-12s %-11s %-14s %s\n' RM rm_id verify plan partial
hr
READY_N=0; NOTBUILT_N=0; BAD_N=0
for i in "${ORDER[@]}"; do
  printf '  %-20s %-12s %-11s %-14s %s\n' \
    "${P_RM[$i]}" "${P_ID[$i]}" "${P_VERIFY[$i]}" "${P_STATUS[$i]}" \
    "$([ "${P_BYTES[$i]}" -ge 0 ] && commas "${P_BYTES[$i]}" || echo "-")"
  [ "${P_REASON[$i]}" = "-" ] || printf '  %-20s   ^ %s\n' "" "${P_REASON[$i]}"
  case "${P_STATUS[$i]}" in
    READY)     READY_N=$((READY_N+1)) ;;
    NOT_BUILT) NOTBUILT_N=$((NOTBUILT_N+1)) ;;
    *)         BAD_N=$((BAD_N+1)) ;;
  esac
done
hr
say "  $READY_N READY, $NOTBUILT_N NOT_BUILT, $BAD_N incoherent  (of ${#ORDER[@]} scheduled)"
[ "${#EXCLUDED[@]}" = 0 ] || say "  excluded by selection: ${EXCLUDED[*]}"
[ "${#CAPPED[@]}"   = 0 ] || say "  *** CAPPED by --max $MAXN, NOT swept: ${CAPPED[*]} ***"

# Order note, printed every time so the schedule is never mysterious.
TRANS=(); for i in "${ORDER[@]}"; do [ "${P_VERIFY[$i]}" = "transition" ] && TRANS+=("${P_RM[$i]}"); done
if [ "${#TRANS[@]}" -gt 0 ]; then
  say "  scheduling: ${TRANS[*]} moved LAST -- zero rm_id is unfalsifiable on its own"
  say "              (trap 5), so they are proven as a non-zero -> zero TRANSITION."
fi

# ---------------------------------------------------------------------------
# DRY RUN — print the exact command sequence, then stop.
# ---------------------------------------------------------------------------
plan_cmd_block() {  # plan_cmd_block <rm> <expected-id> <partial> <label>
  local rm="$1" want="$2" partial="$3" label="$4"
  say ""
  say "  ---- $label ----"
  say "    MPS3_LEASE_TOKEN=\$TOKEN MPS3_LEASE_HOLDER=$HOLDER \\"
  say "    MPS3_PROD_DIR=$PROD \\"
  say "    MPS3_BASE_DIR=$BASE \\"
  say "      $REPO/scripts/mps3_swap_design.sh $rm"
  say "        (NOTE: no --keep-base, ever -- trap 2)"
  say "      loads, in this order:"
  printf '        [1] base     %-70s %s B\n' "$BASE/config_rm_greybox_fw.bit" "$(commas "$(bytes "$BASE/config_rm_greybox_fw.bit")")"
  printf '        [2] clearing %-70s %s B\n' "$BASE/config_rm_greybox_pblock_rp_dut_partial_clear.bit" "$(commas "$(bytes "$BASE/config_rm_greybox_pblock_rp_dut_partial_clear.bit")")"
  printf '        [3] partial  %-70s %s B\n' "$partial" "$(commas "$(bytes "$partial")")"
  say "      then reads DFXCTL.RM_ID @ 0x44A10010, expects $want"
  if [ "$RELEASE_RESET" = 1 ]; then
    say "    $REPO/scripts/mps3_rp_reset_release.sh          # *** BOARD WRITE ***"
    say "      mwr 0x44A00000 0x7   (dut_resetn|rp_resetn|dbg_resetn)"
    say "      then re-reads: DFXCTL.STATUS[1] must CLEAR, RM_STATUS[0] rm_id_valid must SET"
  fi
  say "    $REPO/scripts/mps3_state.sh                      # read-only post-state"
}

if [ "$ALLOW_BOARD" != 1 ]; then
  say ""
  say "==== DRY RUN: the command sequence this sweep WOULD execute ===="
  say ""
  say "  0. lease (poller -- survives a contended board; mps3_board.sh acquire does NOT):"
  say "       TOKEN=\$($REPO/scripts/mps3_lease_acquire.sh $HOLDER) || exit 1"
  say "       trap 'scripts/mps3_board.sh release \"\$TOKEN\" $HOLDER' EXIT   # never leak a lease"
  say "  1. preflight (read-only):  $REPO/scripts/mps3_state.sh --quiet"
  [ "$DO_PING" = 1 ] && say "  2. identity:               $PY scripts/harness_gates/ping_check.py $BOARD_HOST"
  plan_cmd_block greybox 0x00000000 "$PROD/config_rm_greybox_pblock_rp_dut_partial.bit" \
    "step 0: BASE SMOKE (a REFUSE gate, not a verdict -- see trap 5)"
  n=0
  for i in "${ORDER[@]}"; do
    n=$((n+1))
    if [ "${P_STATUS[$i]}" != "READY" ]; then
      say ""
      say "  ---- step $n: ${P_RM[$i]} -- WOULD BE SKIPPED (${P_STATUS[$i]}) ----"
      say "      ${P_REASON[$i]}"
      continue
    fi
    plan_cmd_block "${P_RM[$i]}" "${P_ID[$i]}" "${P_PARTIAL[$i]}" \
      "step $n: ${P_RM[$i]}  (verify=${P_VERIFY[$i]})"
  done
  if [ "$DO_RESTORE" = 1 ]; then
    say ""
    say "  ---- restore: leave the board on a known base (greybox resident) ----"
    say "    MPS3_LEASE_TOKEN=\$TOKEN ... scripts/mps3_swap_design.sh greybox"
  fi
  say ""
  say "  heartbeat between steps: scripts/mps3_board.sh heartbeat \$TOKEN $HOLDER"
  say "    (lease TTL is ${MPS3_LEASE_TTL:-3600}s; one step is a base+clearing+partial"
  say "     JTAG load, order of a minute, so a per-step heartbeat has ample margin)"

  # Record the plan so a dry run also produces the machine-readable artefact.
  for i in "${ORDER[@]}"; do
    rs="PLANNED"; [ "${P_STATUS[$i]}" = "READY" ] || rs="WOULD_SKIP"
    rec "${P_RM[$i]}" "${P_ID[$i]}" "${P_VERIFY[$i]}" "${P_STATUS[$i]}" "${P_REASON[$i]}" \
        "${P_PARTIAL[$i]}" "${P_BYTES[$i]}" "${P_MSTATIC[$i]}" "$rs" "-" "-" "0" "-"
  done
fi

# ---------------------------------------------------------------------------
# BOARD PHASE
# ---------------------------------------------------------------------------
TOKEN=""
OWN_LEASE=0
# Idempotent (OWN_LEASE is cleared) so it is safe to reach twice -- which it
# does on a signal: the signal handler releases and exits, and the EXIT trap then
# fires too. Only releases a lease we OWN; a caller-supplied MPS3_LEASE_TOKEN is
# never ours to release.
release_lease() {
  [ "$OWN_LEASE" = 1 ] || return 0
  say ""
  say "  releasing lease ($TOKEN as $HOLDER)"
  "$REPO/scripts/mps3_board.sh" release "$TOKEN" "$HOLDER" >/dev/null 2>&1 \
    || warn "lease release reported a problem -- CHECK: scripts/mps3_board.sh status"
  OWN_LEASE=0
}

# A SIGNAL HANDLER MUST EXIT. Trapping INT/TERM and merely releasing the lease
# would resume the sweep on the next line -- i.e. Ctrl-C would drop the lease and
# then carry on driving a SHARED board with NO LEASE HELD, which is precisely the
# failure scripts/mps3_board.sh exists to prevent. So: release, then leave.
on_signal() {
  say ""
  say "!!! interrupted -- aborting the sweep and releasing the board"
  release_lease
  exit 130
}

PING_SHELL_ID="-"
BASELINE="-"

if [ "$ALLOW_BOARD" = 1 ]; then
  say ""
  say "==== BOARD: acquire the lease ===="
  if [ -n "${MPS3_LEASE_TOKEN:-}" ]; then
    TOKEN="$MPS3_LEASE_TOKEN"
    say "  reusing the caller's lease ($TOKEN) -- will NOT be released here"
  else
    # The POLLER, not mps3_board.sh acquire: the latter passes --json and this
    # fpgahub's QUEUED response carries no token, so a contended acquire errors
    # out AND strands a queue entry.
    TOKEN="$("$REPO/scripts/mps3_lease_acquire.sh" "$HOLDER")" || {
      say "!!! REFUSE: could not acquire the board lease as $HOLDER."
      say "    ssh ${MPS3_HUB:-<hub-host>} 'fpgahub lease show ${MPS3_LEASE_TARGET:-mps3_pl}'"
      exit 2; }
    OWN_LEASE=1
    trap 'release_lease' EXIT
    trap 'on_signal' INT TERM
    say "  lease held ($TOKEN)"
  fi

  # --- preflight ----------------------------------------------------------
  # NOTE ON scripts/mps3_state.sh's EXIT CODE. Its non-quiet exit 1 means "not
  # ready to CAPTURE" -- it fires when the DUT is held in reset or RM_ID == 0.
  # Both are PERFECTLY NORMAL states to begin a sweep from (greybox resident
  # after a base load is literally RM_ID == 0, and a JTAG partial always leaves
  # the DUT in reset). Gating the sweep on that exit code would refuse to start
  # on a healthy board. So we use --quiet, which exits 0 whenever the registers
  # were actually read and 1 only when the KU115's MicroBlaze could not be found
  # or nothing came back -- which IS the right refusal predicate here -- and we
  # apply our own interpretation to the values.
  say ""
  say "==== BOARD: preflight (read-only) ===="
  ST="$("$REPO/scripts/mps3_state.sh" --quiet 2>&1)"; st_rc=$?
  printf '%s\n' "$ST" | sed 's/^/    /'
  if [ "$st_rc" != 0 ]; then
    say "!!! REFUSE: cannot read the harness registers on the KU115."
    say "    Either the shell is not loaded (a power-cycle reverts to the OLDER SD"
    say "    shell -- it is JTAG-VOLATILE), or the hw_server walk found no MicroBlaze"
    say "    descendant of the xcku115. Do not proceed: every register would read"
    say "    0x00000000, which is indistinguishable from 'greybox resident'."
    exit 2
  fi
  BASELINE="$(printf '%s' "$ST" | tr '\n' ' ' | sed 's/  */ /g')"
  say "    (baseline recorded; RM_ID == 0 and 'held in reset' here are NORMAL"
  say "     sweep starting states -- see the note in this script)"

  if [ "$DO_PING" = 1 ]; then
    say ""
    say "==== BOARD: shell identity (ping 6900) ===="
    if PO="$($PY "$REPO/scripts/harness_gates/ping_check.py" "$BOARD_HOST" 2>&1)"; then
      PING_SHELL_ID="$(printf '%s' "$PO" | grep -oE 'shell_id=[^ ]+' | cut -d= -f2)"
      say "    $PO"
      say "    (expected static_id for this prod dir: $PROD_SID)"
    else
      warn "ping failed: $PO"
      warn "     NOT a refusal: the board's dataplane IP is hub-local, so a ping"
      warn "     failure here usually means 'wrong host', not 'dead shell'."
    fi
  fi

  # --- step 0: base smoke = the refusal gate ------------------------------
  # If the base does not load, nothing downstream means anything: every later
  # RM would fail for the same single reason and the report would blame nine
  # RMs for one broken base. Refuse instead.
  say ""
  say "==== BOARD: step 0 -- base smoke (REFUSE gate) ===="
  say "    reload the base FIRST: the shell is JTAG-VOLATILE and the SD card holds"
  say "    an OLDER one, so whatever was resident is not necessarily ours."
  s0log="$LOGDIR/step0_base_smoke.log"
  if MPS3_LEASE_TOKEN="$TOKEN" MPS3_LEASE_HOLDER="$HOLDER" \
     MPS3_PROD_DIR="$PROD" MPS3_BASE_DIR="$BASE" \
     "$REPO/scripts/mps3_swap_design.sh" greybox > "$s0log" 2>&1; then
    say "    base smoke OK (log: $s0log)"
  else
    say "!!! REFUSE: the base + greybox load FAILED. Tail of $s0log:"
    tail -25 "$s0log" | sed 's/^/      /'
    say "    Nothing downstream would be meaningful, so no RM verdicts are claimed."
    exit 2
  fi

  # --- the sweep ----------------------------------------------------------
  say ""
  say "==== BOARD: sweep ${#ORDER[@]} RM(s) -- per-RM isolation, no early abort ===="
  PREV_NONZERO=""      # the last non-zero rm_id positively read this session
  n=0
  for i in "${ORDER[@]}"; do
    n=$((n+1))
    rm="${P_RM[$i]}"; want="${P_ID[$i]}"; vmode="${P_VERIFY[$i]}"
    say ""
    say "---- step $n/${#ORDER[@]}: $rm  (expect $want, verify=$vmode) ----"

    if [ "${P_STATUS[$i]}" != "READY" ]; then
      say "    SKIP (${P_STATUS[$i]}): ${P_REASON[$i]}"
      rec "$rm" "$want" "$vmode" "${P_STATUS[$i]}" "${P_REASON[$i]}" \
          "${P_PARTIAL[$i]}" "${P_BYTES[$i]}" "${P_MSTATIC[$i]}" \
          "SKIPPED" "${P_REASON[$i]}" "-" "0" "-"
      continue
    fi

    # Keep the lease alive. One step is ~a minute against a 3600 s TTL, so this
    # has enormous margin -- but a stranded board is the single most expensive
    # thing this script can do to a colleague, so it is unconditional.
    "$REPO/scripts/mps3_board.sh" heartbeat "$TOKEN" "$HOLDER" >/dev/null 2>&1 \
      || warn "heartbeat failed -- the lease may expire mid-sweep"

    log="$LOGDIR/step${n}_${rm}.log"
    t0=$(date +%s)
    MPS3_LEASE_TOKEN="$TOKEN" MPS3_LEASE_HOLDER="$HOLDER" \
    MPS3_PROD_DIR="$PROD" MPS3_BASE_DIR="$BASE" \
      "$REPO/scripts/mps3_swap_design.sh" "$rm" > "$log" 2>&1
    swap_rc=$?
    dt=$(( $(date +%s) - t0 ))
    got="$(grep -oE 'DFXCTL\.RM_ID = 0x[0-9A-Fa-f]{8}' "$log" | tail -1 | grep -oE '0x[0-9A-Fa-f]{8}')"
    got="${got:--}"
    say "    swap exit=$swap_rc  RM_ID read=$got  (${dt}s, log: $log)"

    status="FAIL"; reason="-"
    if [ "$swap_rc" != 0 ]; then
      reason="mps3_swap_design.sh exited $swap_rc; RM_ID read=$got expected $want"
      say "    FAIL: $reason"
      tail -12 "$log" | sed 's/^/      | /'
    elif [ "${got,,}" != "${want,,}" ]; then
      reason="RM_ID mismatch: read $got, expected $want"
      say "    FAIL: $reason"
    elif [ "$vmode" = "transition" ] && [ -z "$PREV_NONZERO" ]; then
      # Trap 5. The readback agreed, but agreeing with ZERO proves nothing on
      # its own. Say so instead of banking a free pass.
      status="UNPROVABLE"
      reason="rm_id 0x00000000 read, but NO preceding non-zero rm_id was read this session, so this is not a transition proof (see trap 5)"
      say "    UNPROVABLE: $reason"
    else
      status="PASS"
      if [ "$vmode" = "transition" ]; then
        reason="transition proven: $PREV_NONZERO -> $got on the same board, same session"
        say "    PASS ($reason)"
      else
        reason="RM_ID readback == manifest"
        say "    PASS"
      fi
    fi

    # A positive non-zero read is the control that makes a later transition
    # provable -- and it also proves we are talking to the RIGHT board on the
    # shared hw_server (trap 4).
    [ "$status" = "PASS" ] && [ "$got" != "0x00000000" ] && PREV_NONZERO="$got"

    # Release rp_resetn (trap 3) and re-verify. Only worth doing when the RM
    # actually landed.
    if [ "$RELEASE_RESET" = 1 ] && [ "$status" = "PASS" ]; then
      rlog="$LOGDIR/step${n}_${rm}_reset.log"
      if "$REPO/scripts/mps3_rp_reset_release.sh" > "$rlog" 2>&1; then
        say "    rp_resetn released; STATUS[1] clear, rm_id_valid set (log: $rlog)"
        reason="$reason; out of reset with a stable id"
      else
        status="FAIL"
        reason="$reason; BUT releasing rp_resetn failed to clear DFXCTL.STATUS[1] / set rm_id_valid"
        say "    FAIL: $reason"
        tail -12 "$rlog" | sed 's/^/      | /'
      fi
    fi

    rec "$rm" "$want" "$vmode" "${P_STATUS[$i]}" "${P_REASON[$i]}" \
        "${P_PARTIAL[$i]}" "${P_BYTES[$i]}" "${P_MSTATIC[$i]}" \
        "$status" "$reason" "$got" "$dt" "$log"
  done

  # --- restore ------------------------------------------------------------
  if [ "$DO_RESTORE" = 1 ]; then
    say ""
    say "==== BOARD: restore -- leave the board on a known base (greybox) ===="
    rlog="$LOGDIR/restore_greybox.log"
    if MPS3_LEASE_TOKEN="$TOKEN" MPS3_LEASE_HOLDER="$HOLDER" \
       MPS3_PROD_DIR="$PROD" MPS3_BASE_DIR="$BASE" \
       "$REPO/scripts/mps3_swap_design.sh" greybox > "$rlog" 2>&1; then
      say "    restored (log: $rlog)"
    else
      warn "restore FAILED -- the board is left with the LAST swept RM resident."
      warn "     Not a sweep verdict, but tell the next user. Log: $rlog"
    fi
  else
    say ""
    say "  --no-restore: the LAST swept RM is left resident."
  fi
fi

# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------
say ""
say "======================================================================="
say " SUMMARY  ($MODE)"
say "======================================================================="
printf '  %-20s %-12s %-11s %-12s %s\n' RM rm_id verify result detail
hr
$PY - "$REC" <<'EOF'
import sys
for line in open(sys.argv[1]):
    f = line.rstrip("\n").split("\t")
    if len(f) < 13: continue
    rm, rid, vm, plan, preason, part, pb, ms, rs, rr, got, dt, log = f[:13]
    detail = rr if rr != "-" else preason
    print("  %-20s %-12s %-11s %-12s %s" % (rm, rid, vm, rs, detail[:120]))
EOF
hr

# Tallies + exit rule, computed in one place from the record file.
TALLY="$($PY - "$REC" <<'EOF'
import sys, collections
c = collections.Counter()
for line in open(sys.argv[1]):
    f = line.rstrip("\n").split("\t")
    if len(f) < 13: continue
    c[f[8]] += 1
print(" ".join("%s=%d" % (k, v) for k, v in sorted(c.items())))
EOF
)"
say "  $TALLY"

FAILN="$($PY - "$REC" <<'EOF'
import sys
n = 0
for line in open(sys.argv[1]):
    f = line.rstrip("\n").split("\t")
    if len(f) < 13: continue
    if f[8] in ("FAIL", "UNPROVABLE"): n += 1
print(n)
EOF
)"

# JSON
if [ -n "$JSON_OUT" ]; then
  MODE="$MODE" REC="$REC" JSON_OUT="$JSON_OUT" PROD="$PROD" BASE="$BASE" \
  PROD_SID="$PROD_SID" HOLDER="$HOLDER" BASELINE="$BASELINE" \
  PING_SHELL_ID="$PING_SHELL_ID" OVERLAY_DIR="$OVERLAY_DIR" LOGDIR="$LOGDIR" \
  EXCLUDED="${EXCLUDED[*]:-}" CAPPED="${CAPPED[*]:-}" MAXN="$MAXN" \
  STALE="${STALE_MANIFESTS[*]:-}" \
  RELEASE_RESET="$RELEASE_RESET" REQUIRE_ARTEFACTS="$REQUIRE_ARTEFACTS" \
  $PY - <<'EOF'
import datetime, json, os
recs = []
for line in open(os.environ["REC"]):
    f = line.rstrip("\n").split("\t")
    if len(f) < 13: continue
    recs.append({
        "rm_name": f[0], "expected_rm_id": f[1], "verify_mode": f[2],
        "plan_status": f[3], "plan_reason": None if f[4] == "-" else f[4],
        "partial": f[5], "partial_bytes": int(f[6]),
        "manifest_static_id": f[7],
        "result": f[8], "detail": None if f[9] == "-" else f[9],
        "got_rm_id": None if f[10] == "-" else f[10],
        "duration_s": int(f[11]), "log": None if f[12] == "-" else f[12],
    })
out = {
    "schema": 1,
    "tool": "scripts/mps3_silicon_sweep.sh",
    "generated": datetime.datetime.utcnow().isoformat() + "Z",
    "mode": os.environ["MODE"],
    "holder": os.environ["HOLDER"],
    "overlay_dir": os.environ["OVERLAY_DIR"],
    "prod_dir": os.environ["PROD"],
    "base_dir": os.environ["BASE"],
    "static_id": os.environ["PROD_SID"],
    "ping_shell_id": None if os.environ["PING_SHELL_ID"] == "-" else os.environ["PING_SHELL_ID"],
    "preflight_baseline": None if os.environ["BASELINE"] == "-" else os.environ["BASELINE"],
    "release_reset": os.environ["RELEASE_RESET"] == "1",
    "require_artefacts": os.environ["REQUIRE_ARTEFACTS"] == "1",
    "log_dir": os.environ["LOGDIR"],
    "max": int(os.environ["MAXN"]),
    "excluded_by_selection": os.environ["EXCLUDED"].split() if os.environ["EXCLUDED"] else [],
    "capped_not_swept": os.environ["CAPPED"].split() if os.environ["CAPPED"] else [],
    # rm:static_id pairs whose manifest is keyed to another shell. A warning, not
    # a block -- see the block this script prints, and the comment beside it.
    "stale_manifest_rms": os.environ["STALE"].split() if os.environ["STALE"] else [],
    # What a PASS in this file does and does not mean. Written into the
    # artefact so a consumer three months from now cannot over-read it.
    "proves": "the RM's partial bitstream landed in the RP and the fabric drives "
              "the rm_id recorded in fpga/dfx/overlay/<rm>/manifest.json"
              + ("; and that rp_resetn released with a stable id" if os.environ["RELEASE_RESET"] == "1" else ""),
    "does_not_prove": ["DUT boot", "console/UART traffic", "Ethernet link or frames",
                       "SWD/JTAG debug access to the DUT core", "firmware ICAP swap path"],
    "counts": {},
    "rms": recs,
}
for r in recs:
    out["counts"][r["result"]] = out["counts"].get(r["result"], 0) + 1
out["failed"] = sum(1 for r in recs if r["result"] in ("FAIL", "UNPROVABLE"))
with open(os.environ["JSON_OUT"], "w") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("  json -> %s" % os.environ["JSON_OUT"])
EOF
fi

say ""
say "  NOT PROVEN by this sweep (say so out loud, every run):"
say "    DUT boot / console traffic / Ethernet frames / DUT-core debug /"
say "    the firmware ICAP (over-the-wire) swap path."
say "    Those are harness_regression.sh tier 3 + the per-RM gates in"
say "    scripts/harness_gates/. A green sweep means the fabric is configurable"
say "    and identifiable -- nothing more."

RC=0
if [ "$BAD_N" -gt 0 ]; then
  say ""
  say "  FAIL: $BAD_N selected RM(s) are INCOHERENT (BAD_MANIFEST / DUP_RM_ID /"
  say "        STALE_MANIFEST). The catalogue itself is wrong; fix it before"
  say "        trusting any sweep result."
  RC=1
fi
if [ "$FAILN" -gt 0 ]; then
  say "  FAIL: $FAILN RM(s) FAILED or were UNPROVABLE."
  RC=1
fi
if [ "$NOTBUILT_N" -gt 0 ]; then
  say ""
  say "  $NOTBUILT_N RM(s) were NOT_BUILT (partial .bit absent -- build output is"
  say "  gitignored, so a fresh clone/worktree has none). They are named in the"
  say "  table above; nothing was silently dropped."
  if [ "$REQUIRE_ARTEFACTS" = 1 ]; then
    say "  --require-artefacts: treating that as a FAILURE."
    RC=1
  else
    say "  Pass --require-artefacts on a lab box, where every partial MUST exist."
  fi
fi

say ""
if [ "$ALLOW_BOARD" != 1 ]; then
  say "DRY RUN COMPLETE -- no lease taken, no board touched, nothing programmed."
  say "  Real sweep:  scripts/mps3_silicon_sweep.sh --allow-board --require-artefacts \\"
  say "                 --prod <prod-dir> --base <prod-dir> --json sweep.json"
fi
# Spelt as if/else, not `A && B || C`: this is the line a human reads to decide
# whether the platform is healthy, and `&&/||` can run the FAILED branch when the
# OK branch's own command fails. No ambiguity here.
if [ "$RC" = 0 ]; then
  say "SWEEP OK ($MODE)"
else
  say "SWEEP FAILED ($MODE) -- exit $RC"
fi
exit $RC
