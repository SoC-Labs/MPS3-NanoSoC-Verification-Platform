#!/usr/bin/env bash
# compare.sh -- the compare-pipeline driver for the sim-vs-hardware IICE harness.
#
# Stream C.  Contract: INTERFACES.md §4 (FROZEN exit codes), plan §4 Phase 0.
#
#   compare.sh fake-hw   derive build/hw_iice.fsdb from build/sim_iice.fsdb
#   compare.sh compare    crop + rename + diff -> build/compare_report.txt
#   compare.sh negctl     NEGATIVE CONTROL: prove the comparator can FAIL
#   compare.sh hw-fsdb    pull a REAL trace off the board (gated; needs a lease)
#
# FROZEN exit codes, propagated verbatim from the Python comparator:
#   0 = compared successfully, zero mismatches
#   1 = compared successfully, mismatches found
#   2 = harness error (missing signal, set mismatch, unreadable FSDB, missing tool)
#
# A harness error is NEVER reported as a match.  `negctl` exists because that
# guarantee is only worth anything if the comparator demonstrably fails on bad
# input -- this repo has shipped a verification gate that structurally could not
# fail before, and that is worse than having no gate at all.

set -u -o pipefail

# --- environment ------------------------------------------------------------
# HERE/BUILD/GOLDEN/PYTHON/MANIFEST are exported by tests/identify_iice/Makefile.
# The fallbacks make the script usable standalone.
HERE="${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BUILD="${BUILD:-$HERE/build}"
GOLDEN="${GOLDEN:-$HERE/golden}"
PYTHON="${PYTHON:-python3}"
MANIFEST="${MANIFEST:-$HERE/signals_selftest.yaml}"

SIGNAL_MAP="${SIGNAL_MAP:-$BUILD/signal_map.tsv}"
SIM_FSDB="${SIM_FSDB:-$BUILD/sim_iice.fsdb}"
HW_FSDB="${HW_FSDB:-$BUILD/hw_iice.fsdb}"
REPORT="${REPORT:-$BUILD/compare_report.txt}"
LOGDIR="${LOGDIR:-$BUILD/compare_logs}"
NEG_DIR="${NEG_DIR:-$BUILD/negctl}"

TRACE_COMPARE="$HERE/ncompare/trace_compare.py"
SYNTH_HW="$HERE/synth_hw_fsdb.py"

EXIT_MATCH=0
EXIT_MISMATCH=1
EXIT_HARNESS=2

die() { echo "HARNESS ERROR (compare.sh): $*" >&2; exit $EXIT_HARNESS; }
say() { echo "compare.sh: $*"; }

need_file() { [ -f "$1" ] || die "$2 not found: $1"; }

need_verdi() {
  # Never degrade silently: no FSDB tools means no trustworthy comparison.
  if ! "$PYTHON" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["HERE"])
import fsdb_tools
sys.exit(0 if fsdb_tools.verdi_available() else 1)
PY
  then
    die "the Verdi FSDB utilities (fsdb2vcd/vcd2fsdb) are not on PATH.
  Fix: module load verdi/X-2025.06-SP2
  Refusing to continue -- a compare step that cannot read an FSDB must be a
  harness error (exit 2), never a pass."
  fi
}

export HERE BUILD GOLDEN PYTHON MANIFEST SIGNAL_MAP SIM_FSDB HW_FSDB LOGDIR

# --- provenance (INTERFACES.md §4, amended 2026-07-31) ----------------------
# Every artifact this harness writes carries <artifact>.prov.json naming the
# manifest identity it was produced against; `compare` REFUSES (exit 2) when a
# stamp it can read disagrees with the manifest it is given.
#
#   IICE_STATIC_ID / IICE_STATIC_USERCODE / IICE_RM_ID
#       PRODUCER side -- recorded into the stamp of whatever is being written.
#       Nothing is invented: a real capture only claims a static_id if you say so
#       (fpga/dfx/build/prod/static_id.txt is this tree's; scripts/mps3_state.sh
#       --quiet prints RM_ID= from DFXCTL.RM_ID @ 0x44A1_0010).
#   IICE_EXPECT_STATIC_ID / IICE_EXPECT_STATIC_USERCODE / IICE_EXPECT_RM_ID
#       CONSUMER side -- refuse unless the stamp records these. Opt-in, because
#       which RM was resident is a fact only the caller has.
#   IICE_IDENTIFY_LOG            override the instrumentation log to check against
#   IICE_REQUIRE_IDENTIFY_LOG=1  an absent/other-IICE log becomes a harness error
#
# Absence of a stamp is NOT a mismatch: older captures and every synthetic
# phase-0 FSDB have none, and they must stay comparable. A CONFLICTING stamp is.
#
# The tree's own static_id, offered as the default for a REAL capture only --
# a synthetic trace must never claim to have come off a shell it never touched.
STATIC_ID_FILE="${STATIC_ID_FILE:-$HERE/../../fpga/dfx/build/prod/static_id.txt}"

# ---------------------------------------------------------------------------
cmd_fake_hw() {
  need_file "$MANIFEST" "manifest"
  need_file "$SIGNAL_MAP" "signal_map.tsv (run \`make gen\`)"
  need_file "$SIM_FSDB" "sim FSDB (run \`make sim-fsdb\`)"
  need_verdi
  mkdir -p "$BUILD" "$LOGDIR"
  say "deriving a synthetic hardware FSDB (PHASE 0: no board, no bitstream)"
  "$PYTHON" "$SYNTH_HW" \
      --sim-fsdb "$SIM_FSDB" \
      --signal-map "$SIGNAL_MAP" \
      --manifest "$MANIFEST" \
      --out "$HW_FSDB" \
      --logdir "$LOGDIR" \
      --json "$BUILD/hw_iice.json"
  rc=$?
  [ $rc -eq 0 ] || exit $EXIT_HARNESS
  say "wrote $HW_FSDB"
}

# ---------------------------------------------------------------------------
# run_compare_step <hw_fsdb> <report> <logdir> [signal_map]
# Arguments, not environment: `VAR=x some_function` leaks the assignment past
# the call in bash, which would silently retarget every later comparison.  That
# is why the optional signal-map override (negctl NC8 needs one) is a positional
# argument and not `SIGNAL_MAP=... run_compare_step`.
run_compare_step() {
  local hw="$1" report="$2" logdir="$3" smap="${4:-$SIGNAL_MAP}" rc
  need_file "$MANIFEST" "manifest"
  need_file "$smap" "signal_map.tsv (run \`make gen\`)"
  need_file "$SIM_FSDB" "sim FSDB (run \`make sim-fsdb\`)"
  [ -f "$hw" ] || die "hardware FSDB not found: $hw (run \`make fake-hw\` or \`make hw-fsdb\`)"
  need_verdi
  mkdir -p "$(dirname "$report")" "$logdir"
  # Provenance expectations are read from IICE_EXPECT_* by trace_compare itself
  # (see provenance.expectations_from_env); IICE_IDENTIFY_LOG overrides the log.
  "$PYTHON" "$TRACE_COMPARE" \
      --sim-fsdb "$SIM_FSDB" \
      --hw-fsdb "$hw" \
      --signal-map "$smap" \
      --manifest "$MANIFEST" \
      --report "$report" \
      --logdir "$logdir" \
      ${IICE_NO_NCOMPARE:+--no-ncompare} \
      ${IICE_REQUIRE_IDENTIFY_LOG:+--require-identify-log}
  rc=$?
  case $rc in
    0|1|2) return $rc ;;
    *) die "unexpected exit code $rc from trace_compare.py" ;;
  esac
}

cmd_compare() {
  local rc
  run_compare_step "$HW_FSDB" "$REPORT" "$LOGDIR"
  rc=$?
  case $rc in
    0) say "MATCH         -> $REPORT" ;;
    1) say "MISMATCH      -> $REPORT" ;;
    2) say "HARNESS ERROR -> $REPORT" ;;
  esac
  return $rc
}

# ---------------------------------------------------------------------------
# The negative control.  Three assertions, each of which has failed silently in
# some verification harness somewhere:
#   NC1  a one-bit perturbation MUST be reported (exit 1).  If this exits 0 the
#        comparator is blind and everything built on it is worthless.
#   NC2  a missing signal MUST be a harness error (exit 2), not a smaller
#        comparison that passes.
#   NC3  an unreadable FSDB MUST be a harness error (exit 2), not a match.
#   NC4  an X on the HARDWARE side MUST be a mismatch (exit 1).  The don't-care
#        is one way only; masking it would let a broken capture read as green.
#   NC5  a hardware trace whose TAIL carries NO value change (the wedged-DUT
#        shape) MUST be a MISMATCH (exit 1) whose report NAMES the stall -- not a
#        harness error.  It was exit 2 until 2026-07-30: `fsdb2vcd` stops the VCD
#        at the last transition, so the trailing timestamps vanish and the loader
#        refused to establish the grid.  The grid is now PINNED by the FSDB's own
#        end time.  This assertion is the whole point of that change; without the
#        fix it fails with exit 2.
#   NC6  a trace whose PROVENANCE STAMP names a different probe set MUST be exit
#        2, even when the signal NAMES all match so every other check passes.
#        This is the 2026-07-30 signals_nanosoc retarget (13 signals/113 bits vs a
#        bitstream carrying 6/69): it was caught only by luck, because the names
#        happened to differ. Without the fix this exits 0 -- a confident MATCH
#        across mismatched artifacts.
#   NC7  ANTI-WEAKENING: the SAME trace with NO stamp at all MUST still compare
#        cleanly (exit 0). Absence of provenance is not a mismatch; every capture
#        taken before stamping existed, and every hand-made phase-0 FSDB, is
#        unstamped. If NC6 were implemented by refusing anything unstamped, the
#        whole existing corpus would break and this assertion would fail.
#   NC8  a signal_map.tsv that was NOT generated from $MANIFEST MUST be exit 2.
#        The perturbation used is invisible to every pre-existing check (a changed
#        `sim_path`, same names and widths), so without the fix this exits 0 while
#        silently using a rename table from another manifest -- exactly the
#        2026-07-30 hw-fsdb incident, which the `signal-map` prerequisite fixed
#        for one target only.
#   NC9  the manifest-vs-BUILT check must FIRE on a disagreeing identify.log and
#        must DEGRADE (not fire) when there is no log at all. Both directions,
#        because a check that cannot fail is worthless and a check that cannot
#        skip breaks every fresh clone.
cmd_negctl() {
  need_file "$MANIFEST" "manifest"
  need_file "$SIGNAL_MAP" "signal_map.tsv (run \`make gen\`)"
  need_file "$SIM_FSDB" "sim FSDB (run \`make sim-fsdb\`)"
  need_verdi
  rm -rf "$NEG_DIR"
  mkdir -p "$NEG_DIR"
  local fails=0 rc1 rc2 rc3 rc4 rc5 rc6 rc7 rc8 rc9 drop hwx
  local n_assertions=9

  # ---- NC1: one flipped bit must be detected -----------------------------
  echo
  echo "=== negctl NC1: flip exactly ONE bit, compare MUST exit 1 ==============="
  "$PYTHON" "$SYNTH_HW" \
      --sim-fsdb "$SIM_FSDB" --signal-map "$SIGNAL_MAP" --manifest "$MANIFEST" \
      --out "$NEG_DIR/hw_perturbed.fsdb" --logdir "$NEG_DIR" --perturb-auto \
      | tee "$NEG_DIR/perturb.log"
  [ -f "$NEG_DIR/hw_perturbed.fsdb" ] || die "could not build the perturbed hardware FSDB"
  grep -F "PERTURBED" "$NEG_DIR/perturb.log" | sed 's/^/    /'
  run_compare_step "$NEG_DIR/hw_perturbed.fsdb" \
                   "$NEG_DIR/compare_report_perturbed.txt" \
                   "$NEG_DIR/logs_perturbed" >"$NEG_DIR/nc1.log" 2>&1
  rc1=$?
  echo "    compare exit code: $rc1 (want 1)"
  if [ $rc1 -ne 1 ]; then
    fails=$((fails + 1))
    echo "    *** NC1 FAILED ***"
    if [ $rc1 -eq 0 ]; then
      echo "    The comparator reported a MATCH on a trace with a deliberately"
      echo "    flipped bit.  It cannot fail, therefore it proves nothing, and"
      echo "    every 'MATCH' this harness has ever printed is meaningless."
    else
      echo "    Expected a MISMATCH (1) but got $rc1; the perturbation was not"
      echo "    compared at all."
    fi
    sed -n '1,60p' "$NEG_DIR/nc1.log" | sed 's/^/    | /'
  else
    grep -E "FIRST DIVERGING SAMPLE|MISMATCH COUNT|X-masked samples" \
         "$NEG_DIR/compare_report_perturbed.txt" | sed 's/^/    /'
    echo "    NC1 ok: the one-bit perturbation was detected."
  fi

  # ---- NC2: a missing signal must be exit 2 ------------------------------
  echo
  echo "=== negctl NC2: drop one signal, compare MUST exit 2 ===================="
  drop=$("$PYTHON" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["HERE"])
from crop_trace import read_signal_map, data_signal_names
rows = read_signal_map(os.environ["SIGNAL_MAP"])
names = data_signal_names(rows)
print(names[-1] if len(names) > 1 else "")
PY
) || drop=""
  if [ -z "$drop" ]; then
    echo "    SKIP: the manifest has fewer than 2 data signals, so a signal"
    echo "          cannot be dropped without emptying the compare set."
  else
    "$PYTHON" "$SYNTH_HW" \
        --sim-fsdb "$SIM_FSDB" --signal-map "$SIGNAL_MAP" --manifest "$MANIFEST" \
        --out "$NEG_DIR/hw_missing.fsdb" --logdir "$NEG_DIR" --drop-signal "$drop" \
        >"$NEG_DIR/drop.log" 2>&1 \
      || die "could not build the signal-dropped hardware FSDB (see $NEG_DIR/drop.log)"
    run_compare_step "$NEG_DIR/hw_missing.fsdb" \
                     "$NEG_DIR/compare_report_missing.txt" \
                     "$NEG_DIR/logs_missing" >"$NEG_DIR/nc2.log" 2>&1
    rc2=$?
    echo "    dropped signal:    $drop"
    echo "    compare exit code: $rc2 (want 2)"
    if [ $rc2 -ne 2 ]; then
      fails=$((fails + 1))
      echo "    *** NC2 FAILED *** a missing signal was reported as"
      echo "    $( [ $rc2 -eq 0 ] && echo 'a MATCH' || echo 'a plain mismatch' ),"
      echo "    not as a harness error.  INTERFACES.md §4: a missing signal is"
      echo "    exit 2, never a skipped row."
      sed -n '1,40p' "$NEG_DIR/nc2.log" | sed 's/^/    | /'
    else
      echo "    NC2 ok: the set-equality precheck rejected the trace."
    fi
  fi

  # ---- NC3: an unreadable FSDB must be exit 2 ----------------------------
  echo
  echo "=== negctl NC3: unreadable FSDB, compare MUST exit 2 ===================="
  printf 'this is not an FSDB\n' > "$NEG_DIR/hw_garbage.fsdb"
  run_compare_step "$NEG_DIR/hw_garbage.fsdb" \
                   "$NEG_DIR/compare_report_garbage.txt" \
                   "$NEG_DIR/logs_garbage" >"$NEG_DIR/nc3.log" 2>&1
  rc3=$?
  echo "    compare exit code: $rc3 (want 2)"
  if [ $rc3 -ne 2 ]; then
    fails=$((fails + 1))
    echo "    *** NC3 FAILED *** a corrupt FSDB produced exit $rc3."
    sed -n '1,40p' "$NEG_DIR/nc3.log" | sed 's/^/    | /'
  else
    echo "    NC3 ok: an unreadable FSDB is a harness error."
  fi

  # ---- NC4: a hardware X must be a mismatch, not a don't-care -------------
  # The X don't-care is ONE WAY. sim-X is ignored; hardware-X must be reported,
  # because silicon cannot produce X and an X there means the capture or the
  # loader is broken. This is checked explicitly because a trace with no X in it
  # (the selftest DUT has none) would otherwise never exercise the rule at all.
  echo
  echo "=== negctl NC4: X on the HARDWARE side, compare MUST exit 1 ============="
  "$PYTHON" "$SYNTH_HW" \
      --sim-fsdb "$SIM_FSDB" --signal-map "$SIGNAL_MAP" --manifest "$MANIFEST" \
      --out "$NEG_DIR/hw_x.fsdb" --logdir "$NEG_DIR" --inject-hw-x-auto \
      >"$NEG_DIR/hwx.log" 2>&1
  if [ ! -f "$NEG_DIR/hw_x.fsdb" ]; then
    fails=$((fails + 1))
    echo "    *** NC4 FAILED *** could not build the hardware-X FSDB"
    sed -n '1,20p' "$NEG_DIR/hwx.log" | sed 's/^/    | /'
  else
    grep -F "HW-X" "$NEG_DIR/hwx.log" | sed 's/^/    /'
    run_compare_step "$NEG_DIR/hw_x.fsdb" \
                     "$NEG_DIR/compare_report_hwx.txt" \
                     "$NEG_DIR/logs_hwx" >"$NEG_DIR/nc4.log" 2>&1
    rc4=$?
    echo "    compare exit code: $rc4 (want 1)"
    hwx=$(grep -c "hardware X/Z" "$NEG_DIR/compare_report_hwx.txt" 2>/dev/null || echo 0)
    if [ $rc4 -ne 1 ]; then
      fails=$((fails + 1))
      echo "    *** NC4 FAILED *** an X on the HARDWARE side produced exit $rc4."
      echo "    The X don't-care must be ONE WAY. If hardware X is masked, a"
      echo "    broken capture or a broken FSDB loader reads as a clean pass."
      sed -n '1,40p' "$NEG_DIR/nc4.log" | sed 's/^/    | /'
    else
      grep -E "mismatches caused by hardware X/Z" \
           "$NEG_DIR/compare_report_hwx.txt" | sed 's/^/    /'
      echo "    NC4 ok: hardware X/Z is reported, not masked."
    fi
  fi

  # ---- NC5: a totally quiet tail must be a NAMED MISMATCH, not exit 2 -----
  # The wedged-DUT shape, and the case the whole harness exists for. A tail with
  # no value change at all does not survive `fsdb2vcd` (it "stops the VCD at the
  # last transition"), so the loader used to see fewer timestamps than the
  # manifest depth and refuse to establish the sample grid -- exit 2, naming the
  # symptom. The grid is now pinned by the FSDB's own end time, so the tail is
  # held forward and the verdict is an ordinary mismatch. Two assertions, because
  # exit 1 alone would also be satisfied by a report that says nothing useful:
  # the exit code AND the report naming the stall.
  echo
  echo "=== negctl NC5: FROZEN TAIL, compare MUST exit 1 and NAME the stall ====="
  "$PYTHON" "$SYNTH_HW" \
      --sim-fsdb "$SIM_FSDB" --signal-map "$SIGNAL_MAP" --manifest "$MANIFEST" \
      --out "$NEG_DIR/hw_frozen.fsdb" --logdir "$NEG_DIR" --freeze-tail-auto \
      >"$NEG_DIR/frozen.log" 2>&1
  if [ ! -f "$NEG_DIR/hw_frozen.fsdb" ]; then
    fails=$((fails + 1))
    echo "    *** NC5 FAILED *** could not build the frozen-tail FSDB"
    sed -n '1,20p' "$NEG_DIR/frozen.log" | sed 's/^/    | /'
  else
    grep -F "FROZEN TAIL" "$NEG_DIR/frozen.log" | sed 's/^/    /'
    run_compare_step "$NEG_DIR/hw_frozen.fsdb" \
                     "$NEG_DIR/compare_report_frozen.txt" \
                     "$NEG_DIR/logs_frozen" >"$NEG_DIR/nc5.log" 2>&1
    rc5=$?
    echo "    compare exit code: $rc5 (want 1)"
    if [ $rc5 -ne 1 ]; then
      fails=$((fails + 1))
      echo "    *** NC5 FAILED *** a frozen hardware tail produced exit $rc5."
      if [ $rc5 -eq 2 ]; then
        echo "    A stalled capture is a FINDING, not a harness error: the FSDB's"
        echo "    own end time proves the span, so the tail must be held forward"
        echo "    and reported. See crop_trace.sample_grid and INTERFACES.md §4."
      else
        echo "    A frozen tail that the simulation does not follow must be a"
        echo "    MISMATCH; exit 0 means the tail was not compared at all."
      fi
      sed -n '1,40p' "$NEG_DIR/nc5.log" | sed 's/^/    | /'
    elif ! grep -qF "holds its last value from sample" \
              "$NEG_DIR/compare_report_frozen.txt"; then
      fails=$((fails + 1))
      echo "    *** NC5 FAILED *** exit 1 is right, but the report does NOT name"
      echo "    the stall. 'MISMATCH from sample N' without saying the hardware"
      echo "    stopped moving sends the reader hunting for a value bug that is"
      echo "    not there."
      grep -E "^FINDING|QUIET TAIL" "$NEG_DIR/compare_report_frozen.txt" \
        | sed 's/^/    | /'
    else
      grep -E "^FINDING|FIRST DIVERGING SAMPLE|sample grid established by" \
           "$NEG_DIR/compare_report_frozen.txt" | sed 's/^/    /'
      echo "    NC5 ok: the stall is reported as a mismatch and named."
    fi
  fi

  # ---- NC6: a stamp naming a DIFFERENT probe set must be exit 2 -----------
  # The clean, matching hardware FSDB, with its provenance stamp rewritten to
  # claim a different signal-set digest. NOTHING else changes: the signal names,
  # widths, sample count and values are all still correct, so set-equality, the
  # value diff and nCompare all pass. Only the provenance disagrees -- which is
  # precisely the 2026-07-30 shape where a retargeted manifest was compared
  # against a capture off the old bitstream.
  echo
  echo "=== negctl NC6: hw stamp names a DIFFERENT probe set, MUST exit 2 ======="
  cp "$HW_FSDB" "$NEG_DIR/hw_wrongprov.fsdb"
  "$PYTHON" - "$NEG_DIR/hw_wrongprov.fsdb" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["HERE"])
import provenance
art = sys.argv[1]
src = provenance.read_stamp(os.environ["HW_FSDB"])
if src is None:
    print("NC6 SETUP FAILED: %s carries no stamp, so there is nothing to forge."
          % os.environ["HW_FSDB"], file=sys.stderr)
    sys.exit(1)
stamp = json.loads(json.dumps(src))
stamp["manifest"]["signal_set_sha256"] = "0" * 64
stamp["manifest"]["n_signals"] = 999
stamp["manifest"]["sampled_bits"] = 113           # the real 69-vs-113 number
stamp["manifest"]["path"] = "/somewhere/else/signals_retargeted.yaml"
stamp["artifact"]["bytes"] = os.path.getsize(art)  # so ONLY the digest differs
provenance.write_stamp(art, stamp)
print("    forged stamp: probe set 000000000000, 999 signals, 113 sampled bits")
PY
  if [ ! -f "$NEG_DIR/hw_wrongprov.fsdb.prov.json" ]; then
    fails=$((fails + 1))
    echo "    *** NC6 FAILED *** could not build the mis-stamped FSDB. If"
    echo "    build/hw_iice.fsdb has no stamp, nothing is binding artifacts to"
    echo "    manifests and this assertion cannot run."
  else
    run_compare_step "$NEG_DIR/hw_wrongprov.fsdb" \
                     "$NEG_DIR/compare_report_wrongprov.txt" \
                     "$NEG_DIR/logs_wrongprov" >"$NEG_DIR/nc6.log" 2>&1
    rc6=$?
    echo "    compare exit code: $rc6 (want 2)"
    if [ $rc6 -ne 2 ]; then
      fails=$((fails + 1))
      echo "    *** NC6 FAILED *** a trace produced against a DIFFERENT probe set"
      echo "    compared as $( [ $rc6 -eq 0 ] && echo 'a MATCH' || echo 'a plain mismatch' )."
      echo "    Every value in it is right and every name matches, so no other"
      echo "    check can see the problem: the pairing itself is wrong. That is"
      echo "    the 2026-07-30 failure class -- confident wrong output."
      sed -n '1,40p' "$NEG_DIR/nc6.log" | sed 's/^/    | /'
    elif ! grep -qF "PROVENANCE MISMATCH" "$NEG_DIR/nc6.log"; then
      fails=$((fails + 1))
      echo "    *** NC6 FAILED *** exit 2 is right but for the wrong reason: the"
      echo "    message does not name the provenance mismatch."
      sed -n '1,40p' "$NEG_DIR/nc6.log" | sed 's/^/    | /'
    else
      grep -E "produced against|comparing against" "$NEG_DIR/nc6.log" \
        | head -4 | sed 's/^/    /'
      echo "    NC6 ok: the mismatched pairing was refused, not compared."
    fi
  fi

  # ---- NC7: ANTI-WEAKENING -- no stamp at all must STILL compare ----------
  # The distinction the whole design rests on: ABSENCE of provenance is not a
  # mismatch, a CONFLICTING stamp is. Get this backwards and every capture taken
  # before stamping existed -- plus every synthetic phase-0 FSDB in the corpus --
  # stops being comparable.
  echo
  echo "=== negctl NC7: ANTI-WEAKENING -- NO stamp, compare MUST still exit 0 ==="
  cp "$HW_FSDB" "$NEG_DIR/hw_nostamp.fsdb"
  rm -f "$NEG_DIR/hw_nostamp.fsdb.prov.json"
  run_compare_step "$NEG_DIR/hw_nostamp.fsdb" \
                   "$NEG_DIR/compare_report_nostamp.txt" \
                   "$NEG_DIR/logs_nostamp" >"$NEG_DIR/nc7.log" 2>&1
  rc7=$?
  echo "    compare exit code: $rc7 (want 0)"
  if [ $rc7 -ne 0 ]; then
    fails=$((fails + 1))
    echo "    *** NC7 FAILED *** an UNSTAMPED but otherwise correct trace was"
    echo "    rejected with exit $rc7. Absence of provenance must not be a"
    echo "    mismatch: it would break every pre-2026-07-31 capture and every"
    echo "    hand-made phase-0 FSDB."
    sed -n '1,40p' "$NEG_DIR/nc7.log" | sed 's/^/    | /'
  elif ! grep -qF "carries no provenance stamp" \
            "$NEG_DIR/compare_report_nostamp.txt"; then
    fails=$((fails + 1))
    echo "    *** NC7 FAILED *** exit 0 is right, but the report does not SAY the"
    echo "    trace is unverified. A silently-omitted provenance block reads as"
    echo "    'provenance was checked', which is how three mismatched pairs got"
    echo "    believed on 2026-07-30."
  else
    echo "    NC7 ok: unstamped compares cleanly AND the report says it is"
    echo "    unverified."
  fi

  # ---- NC8: a signal_map.tsv from another manifest must be exit 2 ---------
  # The perturbation is deliberately invisible to every pre-existing check: same
  # signal names, same widths, same radices -- only `sim_path` differs. Sim-side
  # set-equality matches on <scope>/<name>, so it passes; the hardware leaf map is
  # unchanged, so that passes; the values are identical, so the diff passes.
  echo
  echo "=== negctl NC8: signal_map.tsv not from \$MANIFEST, MUST exit 2 ========="
  "$PYTHON" - "$SIGNAL_MAP" "$NEG_DIR/signal_map_foreign.tsv" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
lines = open(src).read().splitlines()
out = []
changed = None
for i, line in enumerate(lines):
    f = line.split("\t")
    if i and len(f) == 5 and f[4] != "__DERIVED__" and changed is None:
        changed = (f[0], f[4])
        f[4] = f[4] + "_from_another_manifest"
    out.append("\t".join(f))
if changed is None:
    print("NC8 SETUP FAILED: no row with a non-derived sim_path", file=sys.stderr)
    sys.exit(1)
open(dst, "w").write("\n".join(out) + "\n")
print("    row %r: sim_path %s -> %s_from_another_manifest"
      % (changed[0], changed[1], changed[1]))
PY
  run_compare_step "$HW_FSDB" \
                   "$NEG_DIR/compare_report_foreignmap.txt" \
                   "$NEG_DIR/logs_foreignmap" \
                   "$NEG_DIR/signal_map_foreign.tsv" >"$NEG_DIR/nc8.log" 2>&1
  rc8=$?
  echo "    compare exit code: $rc8 (want 2)"
  if [ $rc8 -ne 2 ]; then
    fails=$((fails + 1))
    echo "    *** NC8 FAILED *** a signal_map.tsv that was NOT generated from"
    echo "    \$MANIFEST produced exit $rc8. build/signal_map.tsv is written by"
    echo "    whichever manifest last ran \`make gen\`, so this pairing happens by"
    echo "    accident -- on 2026-07-30 it normalised a real nanosoc capture"
    echo "    against the SELFTEST map."
    sed -n '1,40p' "$NEG_DIR/nc8.log" | sed 's/^/    | /'
  elif ! grep -qF "WAS NOT GENERATED FROM THIS MANIFEST" "$NEG_DIR/nc8.log"; then
    fails=$((fails + 1))
    echo "    *** NC8 FAILED *** exit 2 without naming the manifest/signal_map"
    echo "    mismatch sends the reader hunting for a signal-set bug instead."
    sed -n '1,40p' "$NEG_DIR/nc8.log" | sed 's/^/    | /'
  else
    grep -E "row [0-9]+ field|signal_map says|manifest says" "$NEG_DIR/nc8.log" \
      | head -4 | sed 's/^/    /'
    echo "    NC8 ok: the foreign rename table was refused."
  fi

  # ---- NC9: manifest vs BUILT instrumentation, both directions -----------
  # Pure Python, no FSDB and no tools. Two synthetic identify.log files: one that
  # agrees with $MANIFEST and one whose sample-buffer width differs (the 69-vs-113
  # shape). The FIRE direction proves the check works; the DEGRADE direction
  # proves a fresh clone with no build still runs.
  echo
  echo "=== negctl NC9: manifest vs BUILT instrumentation, MUST fire AND skip ==="
  "$PYTHON" - "$NEG_DIR" >"$NEG_DIR/nc9.log" 2>&1 <<'PY'
import os, sys
sys.path.insert(0, os.environ["HERE"])
import provenance as P
neg = sys.argv[1]
ident = P.manifest_identity(os.environ["MANIFEST"])
sampled = [s for s in ident["signals"] if s["sample"]]

def write_log(path, signals, depth, width, clock, iice):
    L = ["Tool: Identify (R) Instrumentor",
         "Setting IICE sample clock to '%s' for IICE named '%s'" % (clock, iice),
         "Setting IICE sampler (sampledepth) to %d for IICE named '%s'" % (depth, iice)]
    for s in signals:
        if s["sample"] and s["trigger"]:
            mode = "trigger and sample"
        elif s["sample"]:
            mode = "sample"
        else:
            mode = "trigger"
        L.append("Instrument Signal %s for %s in %s" % (s["hw"], mode, iice))
    L += [" Generating IICE '%s' for the following settings:" % iice,
          "      Sample Buffer:",
          "          Depth                  %d" % depth,
          "          Width                  %d bits" % width,
          "exit status=0"]
    open(path, "w").write("\n".join(L) + "\n")

good = os.path.join(neg, "identify_agree.log")
bad = os.path.join(neg, "identify_disagree.log")
write_log(good, ident["signals"], ident["depth"], ident["sampled_bits"],
          ident["clock"]["hw"], ident["iice_name"])
# The 69-vs-113 shape: same IICE, same depth, FEWER sampled signals.
write_log(bad, ident["signals"][:-1] if len(ident["signals"]) > 1 else [],
          ident["depth"],
          ident["sampled_bits"] - int(sampled[-1]["width"]),
          ident["clock"]["hw"], ident["iice_name"])

rc = 0
r = P.check_identify_log(ident, P.parse_identify_log(good))
print("AGREE     -> %s %s" % (r["status"], r["discrepancies"]))
if r["status"] != "ok":
    print("*** an AGREEING log was reported as %r" % r["status"]); rc = 1
r = P.check_identify_log(ident, P.parse_identify_log(bad))
print("DISAGREE  -> %s" % r["status"])
for d in r["discrepancies"]:
    print("            * %s" % d)
if r["status"] != "mismatch":
    print("*** a DISAGREEING log was NOT reported as a mismatch"); rc = 1
elif not any("sample-buffer width" in d for d in r["discrepancies"]):
    print("*** the mismatch does not name the sample-buffer width"); rc = 1
r = P.check_identify_log(ident, P.parse_identify_log(os.path.join(neg, "nope.log")))
print("ABSENT    -> %s" % r["status"])
if r["status"] != "absent":
    print("*** an ABSENT log must DEGRADE, not fail: a fresh clone has no build"); rc = 1
sys.exit(rc)
PY
  rc9=$?
  sed -n '1,20p' "$NEG_DIR/nc9.log" | sed 's/^/    /'
  echo "    checker exit code: $rc9 (want 0)"
  if [ $rc9 -ne 0 ]; then
    fails=$((fails + 1))
    echo "    *** NC9 FAILED *** the manifest-vs-BUILT check does not behave in"
    echo "    both directions. It is the check that would have caught the"
    echo "    69-vs-113 confusion instantly, and it must still skip cleanly in a"
    echo "    tree with no instrumented build."
  else
    echo "    NC9 ok: fires on a disagreeing log, degrades with no log."
  fi

  echo
  if [ $fails -ne 0 ]; then
    echo "NEGATIVE CONTROL FAILED ($fails of $n_assertions assertions)."
    echo "Do NOT trust any compare report from this tree until this is fixed."
    exit $EXIT_HARNESS
  fi
  echo "NEGATIVE CONTROL PASSED ($n_assertions assertions): the comparator detects"
  echo "a one-bit perturbation, rejects a missing signal, rejects an unreadable"
  echo "FSDB, refuses to mask an X on the hardware side, reports a frozen tail as a"
  echo "named mismatch rather than a harness error, REFUSES a trace whose"
  echo "provenance names a different probe set, REFUSES a signal_map.tsv that was"
  echo "not generated from this manifest, and still compares an UNSTAMPED trace"
  echo "cleanly -- absence of provenance is not a mismatch, a conflicting stamp is."
  return $EXIT_MATCH
}

# ---------------------------------------------------------------------------
# Pull a REAL hardware trace, or normalise one that has already been captured.
#
# The NORMALISATION half is implemented and tested against a real Identify
# capture (taken over a demo cable, so no board was involved).  The CAPTURE half
# still needs a board plus stream D/E, so the whole target stays gated.
#
# Set IICE_RAW_FSDB=<file> to skip capture and normalise an existing capture --
# that path IS exercised (see tests/test_compare.py).
#
# Capture prerequisites (plan Phase 1/2/4):
#   * an Identify-instrumented RM bitstream loaded on the MPS3   (stream E)
#   * an XVC path to the DUT's debug port                        (stream D)
#   * an `identdebugger` licence seat -- CONFIRMED to exist
#   * an fpgahub lease on mps3_01 -- `acquire` prints the bare token on stdout
#     and BLOCKS; do not wrap it in `timeout`
#
# Two traps already hit with the real debugger, both of which fail the run:
#   * `write fsdb` needs an explicit `-range {0 <depth-1>}`, else
#     "Error: End (maximum) value out of range".
#   * the trigger statemachine must be configured BEFORE `run`, else
#     "Error: Trigger statemachine must be configured for iice <name> before
#     running".  The .idc only SIZES it.  Minimal working form:
#         statemachine clear   -iice <name> -all
#         statemachine addtrans -iice <name> -from 0 -trigger
cmd_hw_fsdb() {
  local raw_fsdb="${IICE_RAW_FSDB:-}"

  if [ -z "$raw_fsdb" ] && [ "${IICE_ALLOW_HW:-0}" != "1" ]; then
    cat >&2 <<EOF
HARNESS ERROR (compare.sh): refusing to run the hw-fsdb CAPTURE step.

  Capturing talks to real hardware: it needs an Identify-instrumented bitstream
  on the MPS3, an XVC/SWD path to the DUT and an fpgahub lease. That half has
  never been executed here (plan Phase 1/2/4).

  To capture anyway:   IICE_ALLOW_HW=1 make -C tests/identify_iice hw-fsdb
  To normalise an ALREADY-captured FSDB (this path is tested):
      IICE_RAW_FSDB=/path/to/capture.fsdb make -C tests/identify_iice hw-fsdb

  Or use \`make fake-hw\`, which proves the whole comparison with no board.
EOF
    exit $EXIT_HARNESS
  fi

  need_file "$MANIFEST" "manifest"
  need_file "$SIGNAL_MAP" "signal_map.tsv (run \`make gen\`)"
  need_verdi
  mkdir -p "$BUILD" "$LOGDIR"

  if [ -z "$raw_fsdb" ]; then
    raw_fsdb="$BUILD/hw_iice_raw.fsdb"
    local shell_bin="${IDENTIFY_DEBUGGER:-identify_debugger_shell}"
    command -v "$shell_bin" >/dev/null 2>&1 \
      || die "$shell_bin not on PATH (module load identify/2022.09-SP2)"
    local prj="${IICE_DEBUG_PRJ:-$HERE/../../host/identify/nanosoc_iice_debug.prj}"
    local tcl="${IICE_DEBUG_TCL:-$HERE/../../host/identify/debug_session.tcl}"
    need_file "$prj" "Identify debugger project (stream D)"
    need_file "$tcl" "Identify debug session script (stream D)"
    say "capturing via $shell_bin (project $prj)"
    # The debugger script owns: com/server/chain setup, statemachine config,
    # arm, `run -timeout`, and `write fsdb -iice <n> -range {0 <depth-1>}`.
    IICE_OUT_FSDB="$raw_fsdb" \
    IICE_MANIFEST="$MANIFEST" \
      "$shell_bin" -play "$tcl" -proj "$prj" 2>&1 | tee "$LOGDIR/hw_capture.log"
    need_file "$raw_fsdb" "captured FSDB (the debugger script did not write it)"
  else
    need_file "$raw_fsdb" "raw capture named by IICE_RAW_FSDB"
    say "normalising an existing capture: $raw_fsdb"
  fi

  # Default the RECORDED static_id from the tree's own sentinel, but only here --
  # this branch is a real capture off a real shell. `fake-hw` deliberately does
  # not, because a synthetic trace claiming a static_id would be a lie that later
  # reads as verified provenance.
  if [ -z "${IICE_STATIC_ID:-}" ] && [ -f "$STATIC_ID_FILE" ]; then
    IICE_STATIC_ID="$(tr -d ' \t\n\r' < "$STATIC_ID_FILE")"
    say "recording static_id $IICE_STATIC_ID (from $STATIC_ID_FILE)"
  fi
  if [ -z "${IICE_RM_ID:-}" ]; then
    say "note: no IICE_RM_ID given, so the stamp will not record which RM was"
    say "      resident. Get it WITHOUT taking a lease:"
    say "        scripts/mps3_state.sh --quiet | grep '^RM_ID='"
    say "      then re-run with IICE_RM_ID=0x........  (nanosoc = 0x01000001)"
  fi
  export IICE_STATIC_ID

  # PREFLIGHT, before a single sample is read. On 2026-07-30 a capture off the
  # 69-bit bitstream was normalised against the 113-bit manifest and the only
  # complaint was the downstream set-equality error -- "signal(s) named in
  # signal_map.tsv are absent from the trace: ['hprot', ...]" -- which names the
  # SYMPTOM (seven probes missing from the capture) and not the CAUSE (this
  # manifest does not describe what was built). Ask identify.log first.
  "$PYTHON" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["HERE"])
import provenance as P
from fsdb_tools import EXIT_HARNESS, HarnessError
try:
    ident = P.manifest_identity(os.environ["MANIFEST"])
    P.check_signal_map(ident, os.environ["SIGNAL_MAP"])
    log_path = os.environ.get("IICE_IDENTIFY_LOG") or P.default_identify_log()
    res = P.check_identify_log(
        ident, P.parse_identify_log(log_path),
        require_iice=bool(os.environ.get("IICE_REQUIRE_IDENTIFY_LOG")))
except HarnessError as exc:
    print("HARNESS ERROR (hw-fsdb preflight): %s" % exc, file=sys.stderr)
    sys.exit(EXIT_HARNESS)
print("hw-fsdb: preflight  manifest %s" % P.identity_headline(ident))
print("hw-fsdb: preflight  signal_map.tsv generated from this manifest: yes")
if res["status"] == "mismatch":
    print("HARNESS ERROR (hw-fsdb preflight): %s"
          % P.identify_log_error(ident, res), file=sys.stderr)
    sys.exit(EXIT_HARNESS)
print("hw-fsdb: preflight  built instrumentation: %s%s"
      % (res["status"], "" if res["status"] == "ok"
         else " -- %s" % res.get("note")))
PY
  rc=$?
  [ $rc -eq 0 ] || exit $EXIT_HARNESS

  # An Identify capture is flat-named, trigger-relative (non-zero time base) and
  # carries two injected signals of Identify's own. Re-emit it as one timestamp
  # per sample at the manifest hw_path names, which is what `compare` reads.
  RAW_FSDB="$raw_fsdb" "$PYTHON" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["HERE"])
sys.path.insert(0, os.path.join(os.environ["HERE"], "ncompare"))
import provenance
from crop_trace import (normalise_hw_capture, read_manifest_iice,
                        read_signal_map)
from fsdb_tools import EXIT_HARNESS, HarnessError
from trace_compare import STRUCTURAL_SIGNALS
try:
    rows = read_signal_map(os.environ["SIGNAL_MAP"])
    iice = read_manifest_iice(os.environ["MANIFEST"])
    desc = normalise_hw_capture(
        os.environ["RAW_FSDB"], os.environ["HW_FSDB"], rows,
        os.environ["LOGDIR"], int(iice["depth"]),
        optional=STRUCTURAL_SIGNALS,
        manifest=os.environ["MANIFEST"],
        ids=provenance.ids_from_env(),
        origin=provenance.ORIGIN_CAPTURE)
except HarnessError as exc:
    print("HARNESS ERROR (hw-fsdb): %s" % exc, file=sys.stderr)
    sys.exit(EXIT_HARNESS)
print("hw-fsdb: %d samples x %d signals" % (desc["n_samples"], len(desc["signals"])))
print("hw-fsdb: raw time base %s..%s, sample period %s (trigger-relative: "
      "absolute time is never used for alignment)"
      % (desc["raw_time_base"][0], desc["raw_time_base"][1], desc["sample_period"]))
print("hw-fsdb: sample grid established by %s" % desc["grid_source"])
if desc["quiet_tail_samples"]:
    print("hw-fsdb: *** QUIET TAIL *** this capture holds its last value from "
          "sample %d of %d -- %d sample(s) carry no value change at all. The "
          "span is PROVED by the FSDB's own end time, not padded. Consistent "
          "with a stalled DUT, a stopped sample clock, or a capture that "
          "stopped being written -- see compare_report.txt."
          % (int(desc["last_change_sample"]) + 1, desc["n_samples"],
             desc["quiet_tail_samples"]))
print("hw-fsdb: signals %s" % ", ".join(desc["signals"]))
# --- provenance: the artifact is now BOUND to the manifest that made it ------
ident = desc.get("manifest_identity")
if desc.get("stamp_file"):
    print("hw-fsdb: PROVENANCE STAMP %s" % desc["stamp_file"])
    print("hw-fsdb:   manifest      %s" % provenance.identity_headline(ident))
    print("hw-fsdb:   raw capture   %s (%s bytes)"
          % (desc["raw_capture"], desc["raw_capture_bytes"]))
    print("hw-fsdb:   samples       %d recovered (manifest depth %s)"
          % (desc["n_samples"], ident["depth"]))
    print("hw-fsdb:   grid          %s" % desc["grid_source"])
    for k in ("static_id", "static_usercode", "rm_id"):
        if desc["provenance"].get(k):
            print("hw-fsdb:   %-13s %s" % (k, desc["provenance"][k]))
    print("hw-fsdb: `make compare` will now REFUSE (exit 2) if it is given a "
          "manifest whose probe set differs from the one above.")
else:
    print("hw-fsdb: *** NO PROVENANCE STAMP WRITTEN *** nothing binds this "
          "artifact to a manifest.")
PY
  rc=$?
  [ $rc -eq 0 ] || exit $EXIT_HARNESS
  say "wrote $HW_FSDB  (now run: make compare)"
}

# ---------------------------------------------------------------------------
# What binds the artifacts in this tree together, and to what was BUILT.
# Board-free, tool-free, read-only: no FSDB is touched, so this runs anywhere.
cmd_identity() {
  local rc=0
  need_file "$MANIFEST" "manifest"
  echo "=== manifest identity ==================================================="
  "$PYTHON" "$HERE/provenance.py" identity --manifest "$MANIFEST" || rc=$?
  echo
  echo "=== signal_map.tsv <-> manifest =========================================="
  if [ -f "$SIGNAL_MAP" ]; then
    "$PYTHON" "$HERE/provenance.py" check-signal-map \
        --manifest "$MANIFEST" --signal-map "$SIGNAL_MAP" || rc=$?
  else
    echo "compare.sh: no $SIGNAL_MAP yet (run \`make gen\` or \`make signal-map\`)"
  fi
  echo
  echo "=== manifest <-> BUILT instrumentation (identify.log) ==================="
  "$PYTHON" "$HERE/provenance.py" check-identify-log \
      --manifest "$MANIFEST" \
      ${IICE_IDENTIFY_LOG:+--identify-log "$IICE_IDENTIFY_LOG"} \
      ${IICE_REQUIRE_IDENTIFY_LOG:+--require-iice} || rc=$?
  echo
  echo "=== artifact stamps ====================================================="
  local any=0 f
  for f in "$HW_FSDB" "$SIM_FSDB"; do
    [ -f "$f" ] || continue
    any=1
    echo "--- $f"
    "$PYTHON" "$HERE/provenance.py" check-stamp \
        --artifact "$f" --manifest "$MANIFEST" \
        ${IICE_EXPECT_RM_ID:+--expect-rm-id "$IICE_EXPECT_RM_ID"} \
        ${IICE_EXPECT_STATIC_ID:+--expect-static-id "$IICE_EXPECT_STATIC_ID"} \
        ${IICE_EXPECT_STATIC_USERCODE:+--expect-static-usercode "$IICE_EXPECT_STATIC_USERCODE"} \
      || rc=$?
  done
  [ $any -eq 1 ] || echo "compare.sh: no FSDBs built yet -- nothing to stamp-check"
  [ $rc -eq 0 ] || exit $EXIT_HARNESS
  return $EXIT_MATCH
}

case "${1:-}" in
  fake-hw)  cmd_fake_hw ;;
  compare)  cmd_compare ;;
  negctl)   cmd_negctl ;;
  hw-fsdb)  cmd_hw_fsdb ;;
  identity) cmd_identity ;;
  *)
    cat >&2 <<EOF
usage: compare.sh {fake-hw|compare|negctl|hw-fsdb|identity}
  fake-hw   derive a synthetic hardware FSDB from the sim FSDB (no board)
  compare   crop + rename + diff -> \$BUILD/compare_report.txt  (exit 0/1/2)
  negctl    NEGATIVE CONTROL: prove the comparator can fail
  hw-fsdb   pull a real trace off the board (gated: IICE_ALLOW_HW=1)
  identity  print + CHECK what binds the artifacts to \$MANIFEST and to the
            instrumentation that was actually built (no tools, no board)
EOF
    exit $EXIT_HARNESS
    ;;
esac
