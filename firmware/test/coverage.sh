#!/usr/bin/env bash
# firmware/test/coverage.sh -- ADVISORY gcov line-coverage for the critical
# swap-path firmware files. Board-free, no lcov (text gcov only).
#
# WHY THIS SHAPE. The host-gcc harness (Makefile) is direct-compile: each test
# binary compiles the unit-under-test from source, and DIFFERENT binaries compile
# the SAME file with DIFFERENT -D flags (windowed / staging / hal-mock). Naive
# `--coverage` across the whole suite yields .gcda/.gcno mismatches. So instead we
# define COVERAGE GROUPS: one flag set + the tests that share it. Per group we
# compile the target file(s) ONCE into a shared instrumented .o, link the group's
# same-flag tests against it, run them (their .gcda accumulate into that one .o),
# then gcov the target. A file measured under one config only counts the lines
# that config compiles in (#ifdef'd-out variants simply don't appear) -- so a file
# can legitimately show more than one row, one per configuration.
#
# ADVISORY by default: prints a table and a per-row OK/LOW vs COV_FLOOR, and exits
# 0 regardless. Set COV_STRICT=1 to make a below-floor row a hard failure.
#
#   ./coverage.sh                 # advisory, floor = $COV_FLOOR (default 75)
#   COV_FLOOR=85 COV_STRICT=1 ./coverage.sh   # gate: fail if any row < 85%
set -uo pipefail
cd "$(dirname "$0")"

CC=${CC:-gcc}
CFLAGS="-std=c11 -Wall -Wextra -Wno-unused-parameter -g -O0"
FLOOR=${COV_FLOOR:-75}
STRICT=${COV_STRICT:-0}
COMMON=../common
COV=cov

echo "== firmware swap-path coverage (advisory, gcov text; floor ${FLOOR}%) =="

if ! command -v gcov >/dev/null 2>&1; then
  echo "  SKIP: gcov not found (advisory stage -- install gcc's gcov to enable)"
  exit 0
fi

rm -rf "$COV"; mkdir -p "$COV"
ROWS=()          # "file|config|pct|nlines"
LOW=0

# run_group NAME "FLAGS" "TARGET_SRCS" "DEP_SRCS" "TESTS"
#   TARGET_SRCS: instrumented + reported (space-separated, paths rel to firmware/test)
#   DEP_SRCS:    linked, not reported
#   TESTS:       test source basenames (no .c), all sharing FLAGS + DEP_SRCS
run_group() {
  local name="$1" flags="$2" tsrcs="$3" deps="$4" tests="$5"
  local gdir="$COV/$name"; mkdir -p "$gdir"
  local objs="" ts o
  for ts in $tsrcs; do
    o="$gdir/$(basename "${ts%.c}").o"
    if ! $CC --coverage $CFLAGS $flags -c "$ts" -o "$o" 2>"$gdir/build.log"; then
      echo "  ERROR building $ts for group $name (see $gdir/build.log)"; return 1
    fi
    objs="$objs $o"
  done
  local t
  for t in $tests; do
    if ! $CC --coverage $CFLAGS $flags -o "$gdir/$t" "$t.c" $objs $deps 2>>"$gdir/build.log"; then
      echo "  ERROR linking $t for group $name (see $gdir/build.log)"; return 1
    fi
    if ! "$gdir/$t" >/dev/null 2>&1; then
      echo "  ERROR: $t (group $name) did not pass -- coverage run aborted"; return 1
    fi
  done
  # gcov each target file against the shared object dir; parse the summary line.
  for ts in $tsrcs; do
    local out pct nl
    out=$(gcov -o "$gdir" "$ts" 2>/dev/null)
    pct=$(printf '%s\n' "$out" | grep -A1 "File '.*$(basename "$ts")'" | grep -oE 'Lines executed:[0-9.]+%' | grep -oE '[0-9.]+' | head -1)
    nl=$(printf '%s\n' "$out"  | grep -A1 "File '.*$(basename "$ts")'" | grep -oE 'of [0-9]+' | grep -oE '[0-9]+' | head -1)
    [ -z "$pct" ] && pct="0.0"
    [ -z "$nl" ] && nl="?"
    ROWS+=("$(basename "$ts")|$name|$pct|$nl")
  done
  rm -f ./*.gcov
}

# --- Groups -----------------------------------------------------------------
# G1: config_agent under the DEFAULT (fire-hose) config.
run_group "config_agent-default" "" \
  "../config_agent/config_agent.c" \
  "$COMMON/crc32.c $COMMON/net_proto.c $COMMON/net_if.c fake_net_if.c" \
  "test_config_agent test_config_agent_net test_config_agent_keepalive" || true

# G2: the swap coordinator FSM under -DMPS3_HAL_MOCK.
run_group "swap_fsm-halmock" "-DMPS3_HAL_MOCK" \
  "../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c" \
  "$COMMON/hwicap_writer.c mock_regs.c fake_config_agent.c fake_overlay_store.c" \
  "test_swap_fsm_hw test_swap_fsm_faults" || true

# G3: config_agent + FSM under the ICAP-direct/defer config -- this is the path
# that carries the silicon DAP-teardown fix (RECV_PENDING_ICAP_BEGIN defer).
run_group "config_agent+fsm-icap" "-DMPS3_HAL_MOCK -DMPS3_CFG_AGENT_STAGING_BYTES=4096" \
  "../config_agent/config_agent.c ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c" \
  "$COMMON/hwicap_writer.c $COMMON/crc32.c $COMMON/net_proto.c $COMMON/net_if.c mock_regs.c fake_net_if.c fake_overlay_store.c" \
  "test_swap_icap_direct test_swap_icap_defer" || true

# G4/G5: THE HWICAP writer, once per write-path protocol. Two rows because the
# LITE and FIFO halves are different compiled code -- a single row would report
# whichever half the default build happens to select and say nothing about the
# other, which is how the FIFO half (the one the FIELDED shell runs) went from
# "written" to "shipped" without ever being compiled by a host binary.
run_group "hwicap_writer-lite" \
  "-DMPS3_HAL_MOCK -DMPS3_HWICAP_MSB_FIRST=1 -DHWICAP_WRITER_TEST_NAME=\"\\\"cov\\\"\"" \
  "$COMMON/hwicap_writer.c" "mock_regs.c" "test_hwicap_writer" || true

run_group "hwicap_writer-fifo" \
  "-DMPS3_HAL_MOCK -DMPS3_HWICAP_FIFO=1 -DMPS3_HWICAP_MSB_FIRST=1 -DHWICAP_WRITER_TEST_NAME=\"\\\"cov\\\"\"" \
  "$COMMON/hwicap_writer.c" "mock_regs.c" "test_hwicap_writer" || true

# --- Report -----------------------------------------------------------------
# Detail: every (file, config) row measured. A file shows once per config; a
# config only counts the lines it compiles in, so a narrow group reads low for a
# file that a broader group covers well -- that is why the FLOOR is applied to the
# BEST row per file below, not to every row.
echo ""
echo "  detail -- coverage per configuration:"
printf "  %-26s %-28s %8s %8s\n" "FILE" "CONFIG" "LINES%" "NLINES"
printf "  %-26s %-28s %8s %8s\n" "----" "------" "------" "------"
for row in "${ROWS[@]}"; do
  IFS='|' read -r f cfg pct nl <<<"$row"
  printf "  %-26s %-28s %7s%% %8s\n" "$f" "$cfg" "$pct" "$nl"
done

if [ "${#ROWS[@]}" -eq 0 ]; then
  echo ""; echo "  (no coverage rows produced -- treated as advisory skip)"; exit 0
fi

# Summary: best line-% per file, which is what the floor gates on.
declare -A BEST
for row in "${ROWS[@]}"; do
  IFS='|' read -r f cfg pct nl <<<"$row"
  cur="${BEST[$f]:-0}"
  awk "BEGIN{exit !($pct > $cur)}" && BEST[$f]="$pct"
done
echo ""
echo "  summary -- best coverage per file (gated on this):"
printf "  %-26s %8s  %s\n" "FILE" "BEST%" "STATUS"
printf "  %-26s %8s  %s\n" "----" "-----" "------"
for f in "${!BEST[@]}"; do
  pct="${BEST[$f]}"; status="OK"
  if awk "BEGIN{exit !($pct < $FLOOR)}"; then status="LOW"; LOW=$((LOW+1)); fi
  printf "  %-26s %7s%%  %s\n" "$f" "$pct" "$status"
done | sort
echo ""
if [ "$LOW" -gt 0 ]; then
  echo "  $LOW file(s) below the ${FLOOR}% floor in every config (advisory)."
  [ "$STRICT" = "1" ] && { echo "  COV_STRICT=1 -> failing."; exit 1; }
else
  echo "  all files at/above the ${FLOOR}% floor in their best config."
fi
echo "  (advisory: exit 0; set COV_STRICT=1 to gate. Per-file .gcov under $COV/)"
exit 0
