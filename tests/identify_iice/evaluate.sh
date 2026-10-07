#!/usr/bin/env bash
# evaluate.sh -- run the whole sim-vs-hardware loop against a REALISTIC injected
# bug, then hand back something a human can judge.
#
#   make -C tests/identify_iice evaluate MODE=stuck|skew|late-divergence|clean
#
# WHY THIS EXISTS, GIVEN `make check` ALREADY PASSES
# --------------------------------------------------
# `make check` answers "is the harness correct?".  It is a pass/fail CI gate and
# its output is an exit code.  It does not answer the question that decides
# whether any of this is worth a board slot:
#
#     "if I had a real hardware bug, would this tooling find it, and would the
#      output tell me where to look?"
#
# So this target injects a bug shaped like one this repo has actually had, runs
# the real pipeline over it, and produces TWO artifacts plus a reading:
#
#   1. build/evaluate/<tag>/compare_report.txt    the machine-checkable verdict
#   2. build/evaluate/<tag>/iice_<tag>.jf         a joined FSDB for nWave, sim and
#                                                 "hardware" side by side
#   3. build/evaluate/<tag>/interpretation.txt    what the pattern is consistent
#                                                 with, and what it is NOT
#
# <tag> is the mode, plus the stall shape when it is not the default
# (`late-divergence-freeze`), because the two shapes exercise different code and
# `evaluate-all` runs both -- see the EVAL_TAG comment below.
#
# EXIT CODES -- NOTE THEY MEAN SOMETHING DIFFERENT FROM compare.sh's
# ------------------------------------------------------------------
#   0  the loop ran and the tooling did its job (found the injected divergence,
#      or reported MATCH for MODE=clean), and the report says what was injected
#   1  the tooling did NOT do its job: it missed an injected divergence, or
#      invented one in MODE=clean, or the report disagrees with the injection
#   2  harness error -- could not run at all (no VCS/Verdi, missing inputs)
#
# compare.sh's 1 means "sim and hardware disagree", which for every injected mode
# here is the DESIRED outcome.  Conflating the two is how you end up with a
# workflow that reports failure when it succeeds, so they are kept apart.

set -u -o pipefail

HERE="${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BUILD="${BUILD:-$HERE/build}"
PYTHON="${PYTHON:-python3}"
MANIFEST="${MANIFEST:-$HERE/signals_selftest.yaml}"
GOLDEN="${GOLDEN:-$HERE/golden}"

SIGNAL_MAP="${SIGNAL_MAP:-$BUILD/signal_map.tsv}"
SIM_FSDB="${SIM_FSDB:-$BUILD/sim_iice.fsdb}"

INJECT="$HERE/inject_divergence.py"
COMPARE_SH="$HERE/compare.sh"

EXIT_OK=0
EXIT_USELESS=1
EXIT_HARNESS=2

MODES="clean stuck skew late-divergence"

die() { echo "HARNESS ERROR (evaluate.sh): $*" >&2; exit $EXIT_HARNESS; }
hr()  { echo "======================================================================"; }

usage() {
  cat >&2 <<EOF
usage: evaluate.sh <mode>
  clean            no injection - the positive control (expect MATCH)
  stuck            one bus bit held constant from a sample onward (LAN8720/RMII class)
  skew             hardware lags the model by one sample (CDC / sampling-phase class)
  late-divergence  hardware tracks the model then stops advancing (boots-then-wedges)
EOF
}

MODE="${1:-}"
[ -n "$MODE" ] || { usage; exit $EXIT_HARNESS; }
case " $MODES " in
  *" $MODE "*) ;;
  *) echo "evaluate.sh: unknown MODE=$MODE" >&2; usage; exit $EXIT_HARNESS ;;
esac

# --- inputs ----------------------------------------------------------------
[ -f "$MANIFEST" ]   || die "manifest not found: $MANIFEST"
[ -f "$SIGNAL_MAP" ] || die "signal_map.tsv not found: $SIGNAL_MAP  (run \`make gen\`)"
[ -f "$SIM_FSDB" ]   || die "sim FSDB not found: $SIM_FSDB  (run \`make sim-fsdb\`)"
[ -s "$SIM_FSDB" ]   || die "sim FSDB is zero bytes: $SIM_FSDB"
[ -f "$INJECT" ]     || die "missing $INJECT"
[ -f "$COMPARE_SH" ] || die "missing $COMPARE_SH"

# The stall shape gets its own output directory: late-divergence/loop and
# late-divergence/freeze exercise DIFFERENT code (the freeze has no value change
# in its tail, so it goes through the end-time-pinned sample grid), and
# `evaluate-all` runs both.  Sharing one directory would make the second run
# silently delete the first run's artifacts and report.
EVAL_TAG="$MODE"
if [ "$MODE" = "late-divergence" ] && [ "${EVAL_STALL_SHAPE:-loop}" != "loop" ]; then
  EVAL_TAG="$MODE-${EVAL_STALL_SHAPE}"
fi
DIR="$BUILD/evaluate/$EVAL_TAG"
LOGS="$DIR/logs"
HW_FSDB="$DIR/hw_iice.fsdb"
REPORT="$DIR/compare_report.txt"
INJECTION="$DIR/injection.json"
INTERP="$DIR/interpretation.txt"

# Idempotent by construction: the whole per-mode directory is rebuilt, so a
# second run cannot read a stale FSDB or a stale report from the first.
rm -rf "$DIR"
mkdir -p "$DIR" "$LOGS"

export HERE BUILD GOLDEN PYTHON MANIFEST SIGNAL_MAP SIM_FSDB

hr
echo "EVALUATE  mode=$EVAL_TAG   manifest=$(basename "$MANIFEST")"
hr

# ---------------------------------------------------------------------------
# 1. inject
# ---------------------------------------------------------------------------
echo
echo "-- 1/5 inject a realistic divergence into the synthetic hardware trace --"
# EVAL_STALL_SHAPE=freeze turns MODE=late-divergence from a spin loop into a
# total stall.  A tail with NO value changes does not survive the FSDB round trip
# intact -- fsdb2vcd stops at the last transition -- so this used to exit 2.
# Since 2026-07-30 crop_trace.sample_grid pins the grid from the FSDB's own end
# time and the stall is reported as a named MISMATCH (compare.sh negctl NC5).
# The spin loop stays the default: it is the better model of the live M0
# question, where the witness tap proved the core keeps fetching.
"$PYTHON" "$INJECT" inject \
    --mode "$MODE" \
    --sim-fsdb "$SIM_FSDB" \
    --signal-map "$SIGNAL_MAP" \
    --manifest "$MANIFEST" \
    --out "$HW_FSDB" \
    --logdir "$LOGS" \
    --json "$INJECTION" \
    --stall-shape "${EVAL_STALL_SHAPE:-loop}" \
  || die "the injector failed (see above)"
[ -s "$HW_FSDB" ] || die "the injector wrote no hardware FSDB at $HW_FSDB"

# ---------------------------------------------------------------------------
# 2. compare -- the REAL pipeline, unmodified
# ---------------------------------------------------------------------------
echo
echo "-- 2/5 compare (crop + rename + diff + nCompare cross-check) ------------"
# No `set -e` anywhere in this script: exit 1 from compare.sh is the EXPECTED
# outcome of every injected mode, and errexit would abort the run at exactly the
# moment the tooling did its job.  Every failure below is therefore checked
# explicitly.
HW_FSDB="$HW_FSDB" REPORT="$REPORT" LOGDIR="$LOGS" \
  bash "$COMPARE_SH" compare >"$DIR/compare_stdout.log" 2>&1
rc=$?
echo "   compare.sh exit code: $rc   (0 match / 1 mismatch / 2 harness error)"

if [ $rc -eq 2 ]; then
  echo
  echo "HARNESS ERROR: the compare step could not run. Nothing here is a"
  echo "statement about the tooling's usefulness -- it did not get that far."
  sed -n '1,40p' "$DIR/compare_stdout.log" | sed 's/^/   | /'
  exit $EXIT_HARNESS
fi

# Digest, not the whole report: the report is the artifact, this is the headline.
grep -E "^VERDICT|^FINDING|MISMATCH COUNT|FIRST DIVERGING SAMPLE|diverging signal|relative to trigger|X-masked samples \(|hardware X/Z|verdict +(AGREES|DISAGREES)" \
     "$REPORT" | sed 's/^/   /' || true

fails=0
case "$MODE:$rc" in
  clean:0) echo "   as expected: no injection, no divergence." ;;
  clean:1)
    fails=$((fails + 1))
    echo
    echo "   *** MODE=clean REPORTED A MISMATCH ***"
    echo "   Nothing was injected, so this is a FALSE POSITIVE in the pipeline."
    echo "   Every red report from this tree is suspect until it is explained."
    ;;
  *:1) echo "   as expected: the injected divergence was REPORTED." ;;
  *:0)
    fails=$((fails + 1))
    echo
    echo "   *** THE INJECTED DIVERGENCE WAS NOT FOUND ***"
    echo "   A divergence shaped like a real bug went in and the pipeline"
    echo "   reported MATCH. That is the answer to \"would this find my bug?\","
    echo "   and the answer is no. See $DIR/compare_stdout.log."
    ;;
esac

# ---------------------------------------------------------------------------
# 3. does the REPORT say what was INJECTED?
# ---------------------------------------------------------------------------
echo
echo "-- 3/5 does the written report say what was injected? -------------------"
"$PYTHON" "$INJECT" check --injection "$INJECTION" --report "$REPORT" \
  | sed 's/^/   /'
chk=${PIPESTATUS[0]}
if [ "$chk" -eq 2 ]; then
  die "the report cross-check could not run"
elif [ "$chk" -ne 0 ]; then
  fails=$((fails + 1))
fi

# ---------------------------------------------------------------------------
# 4. the waveform artifacts
# ---------------------------------------------------------------------------
echo
echo "-- 4/5 waveform artifacts for nWave ------------------------------------"
SIM_WINDOW="$LOGS/sim_window.json"
HW_WINDOW="$LOGS/hw_window.json"
[ -f "$SIM_WINDOW" ] || die "compare did not save $SIM_WINDOW"
[ -f "$HW_WINDOW" ]  || die "compare did not save $HW_WINDOW"
"$PYTHON" "$INJECT" waves \
    --sim-window "$SIM_WINDOW" \
    --hw-window "$HW_WINDOW" \
    --outdir "$DIR" \
    --logdir "$LOGS" \
    --tag "$EVAL_TAG" \
  | sed 's/^/   /' \
  || die "could not build the waveform artifacts"

# ---------------------------------------------------------------------------
# 5. the reading
# ---------------------------------------------------------------------------
echo
echo "-- 5/5 interpretation ---------------------------------------------------"
"$PYTHON" "$INJECT" interpret \
    --sim-window "$SIM_WINDOW" \
    --hw-window "$HW_WINDOW" \
    --injection "$INJECTION" \
    --mode "$MODE" \
    --out "$INTERP" \
  || die "the interpretation step failed"

# ---------------------------------------------------------------------------
# where everything is
# ---------------------------------------------------------------------------
JOINED="$DIR/iice_$EVAL_TAG.jf"
echo
hr
echo "ARTIFACTS  (mode=$EVAL_TAG)"
hr
echo "  verdict + first divergence + per-signal counts:"
echo "      $REPORT"
echo "  the reading above, saved:"
echo "      $INTERP"
echo "  what was injected, and the numbers it predicts:"
echo "      $INJECTION"
echo "  full compare stdout (incl. the nCompare cross-check):"
echo "      $DIR/compare_stdout.log"
echo
echo "  OPEN THE WAVEFORMS -- sim and \"hardware\" on one time axis, where the"
echo "  time stamp IS the sample index on both sides:"
echo
echo "      module load verdi/T-2022.06-SP2   # or X-2025.06-SP2, either reads it"
echo "      unset NOVAS_HOME                  # the login value is a 2017 Verdi"
echo "      nWave -ssf $JOINED &"
echo
echo "  if your nWave will not take a joined file, the two real FSDBs plus the"
echo "  mismatch overlay open the same way and are the same data:"
echo
echo "      nWave -ssf $DIR/sim_window.fsdb \\"
echo "                 $DIR/hw_window.fsdb \\"
echo "                 $DIR/mismatch.fsdb &"
echo
echo "  In nWave: add diff/any_neq FIRST and search for its first rising edge."
echo "  That is the sample the report calls FIRST DIVERGING SAMPLE. Then put"
echo "  sim/<signal> directly above hw/<signal> for the signal(s) named there."
echo "  sim/trigger_marker shows where the trigger sat in the window."
echo

hr
if [ $fails -ne 0 ]; then
  echo "EVALUATE FAILED ($fails problem(s)) -- mode=$EVAL_TAG"
  echo
  echo "This is not a statement about the DUT. It says the TOOLING did not do"
  echo "what it must: an injected, realistic divergence was not reported, or the"
  echo "report did not describe it faithfully. Fix that before reading any"
  echo "compare report from this tree as evidence about hardware."
  hr
  exit $EXIT_USELESS
fi
echo "EVALUATE_OK  mode=$EVAL_TAG"
echo
echo "The loop ran end to end and the tooling localised the injected divergence"
echo "to a signal and a sample. Judge it yourself: open the waveforms above, and"
echo "read EVALUATING.md for what these artifacts do and do NOT prove."
hr
exit $EXIT_OK
