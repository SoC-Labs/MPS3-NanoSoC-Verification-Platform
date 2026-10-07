#!/usr/bin/env bash
#
# xvc_loopback_throughput.sh — measure the firmware XVC engine's OWN throughput
# ceiling, board-free, in one command.
#
# WHAT IT DOES
#   1. builds bin/xvc_fw_daemon + bin/xvc_throughput (`make tools`)
#   2. starts the daemon: the REAL firmware/xvc_server/xvc_server.c built
#      -DMPS3_XVC_TARGET_SWDBB, on a real kernel TCP socket, with the SWDBB
#      register page routed to an in-memory IEEE-1149.1 TAP
#   3. drives it with bin/xvc_throughput at several shift sizes, including
#      Identify's real 2053-bit shape and the 101-shift count of the silicon
#      baseline
#   4. CROSS-CHECKS the result against the server's own TCK rising-edge counter:
#      bits requested must equal edges clocked. This is what stops the number
#      from being "how fast can a socket say yes".
#
# WHAT THE NUMBER MEANS — and does not
#   It is an UPPER BOUND on the MicroBlaze firmware path, not a prediction of
#   it. Present: the whole protocol engine, the 3n+1 access loop, one TCP round
#   trip per shift. Absent: MicroBlaze AXI-Lite timing (the accesses land on
#   memory here, not on a bus), lwIP, and the shell superloop's other work.
#   Locality — the entire reason the SWDBB target exists — is a property of the
#   real bus and is NOT measured here. The board number will be LOWER.
#   The comparison target is the MEASURED ~340 bit/s of the host XVC path
#   (101 shifts x 2051 bits in ~605 s on silicon), whose cost is ~6,154 remote
#   xsdb -> hw_server -> MDM round trips per shift.
#
# The daemon runs with --quiet deliberately: its per-shift progress printf is
# not in the firmware, so leaving it on would measure stdout.
#
# Board-free, tool-free (gcc only), no lease, touches no hardware.
#
#   firmware/test/xvc_loopback_throughput.sh [--shifts N] [--keep]
#
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIFTS=101
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --shifts) SHIFTS="$2"; shift 2 ;;
        --keep)   KEEP=1; shift ;;
        -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

RUN="$(mktemp -d "${TMPDIR:-/tmp}/xvctp.XXXXXX")"
DAEMON_PID=""
cleanup() {
    [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null
    [ -n "$DAEMON_PID" ] && wait "$DAEMON_PID" 2>/dev/null
    if [ "$KEEP" = "1" ]; then
        echo "logs kept in $RUN"
    else
        rm -rf "$RUN"
    fi
}
trap cleanup EXIT INT TERM

echo "=== building tools ==="
if ! make -C "$HERE" tools >"$RUN/build.log" 2>&1; then
    echo "FATAL: make -C firmware/test tools failed. Log:" >&2
    cat "$RUN/build.log" >&2
    exit 1
fi
for b in xvc_fw_daemon xvc_throughput; do
    [ -x "$HERE/bin/$b" ] || { echo "FATAL: $HERE/bin/$b was not built" >&2; exit 1; }
done

echo "=== starting bin/xvc_fw_daemon (REAL xvc_server.c, SWDBB target) ==="
"$HERE/bin/xvc_fw_daemon" --port 0 --port-file "$RUN/port" --quiet \
    --max-seconds 900 >"$RUN/daemon.log" 2>&1 &
DAEMON_PID=$!

# Wait for the bound port. The daemon writes it only after a successful listen,
# so a missing file after the timeout means it failed to bind -- report its log
# rather than a confusing connect error.
PORT=""
for _ in $(seq 1 100); do
    if [ -s "$RUN/port" ]; then PORT="$(cat "$RUN/port")"; break; fi
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then break; fi
    sleep 0.05
done
if [ -z "$PORT" ]; then
    echo "FATAL: daemon never reported a bound port. Its log:" >&2
    cat "$RUN/daemon.log" >&2
    exit 1
fi
echo "daemon listening on 127.0.0.1:$PORT (pid $DAEMON_PID)"
echo

# --------------------------------------------------------------------------
# The runs. 2053 bits x 101 shifts is the headline: exactly the shape and the
# count of the silicon baseline, so the two numbers differ only in the path.
# --------------------------------------------------------------------------
TOTAL_BITS=0
run_case() {
    local label="$1"; shift
    echo "----------------------------------------------------------------"
    echo "CASE: $label"
    local out
    if ! out="$("$HERE/bin/xvc_throughput" --host 127.0.0.1 --port "$PORT" "$@" 2>&1)"; then
        echo "$out"
        echo "FATAL: xvc_throughput failed for case '$label'" >&2
        exit 1
    fi
    echo "$out" | grep -v '^XVC_TP '
    local line bits num warm
    line="$(echo "$out" | grep '^XVC_TP ' | tail -1)"
    # ANCHORED parses. A greedy 's/.*bits=.../' matches num_bits= instead of
    # total_bits= and silently under-counts -- that bug once made this gate
    # accuse a correct server. Keep these anchored to the exact key.
    bits="$(echo "$line" | sed -n 's/^XVC_TP total_bits=\([0-9]*\) .*/\1/p')"
    num="$(echo "$line"  | sed -n 's/^XVC_TP .* num_bits=\([0-9]*\) .*/\1/p')"
    if [ -z "$bits" ] || [ -z "$num" ]; then
        echo "FATAL: could not parse the XVC_TP line for '$label': $line" >&2
        exit 1
    fi
    # The warmup shifts are excluded from the RATE but they do clock the TAP, so
    # they must be counted in the edge cross-check. --warmup default is 2.
    warm=$(( 2 * num ))
    TOTAL_BITS=$(( TOTAL_BITS + bits + warm ))
    echo
}

run_case "Identify's real shape, baseline count (2053 bits x $SHIFTS shifts)" \
         --bits 2053 --shifts "$SHIFTS" --quiet
run_case "maximal accepted shift (8192 bits x 64)" \
         --bits 8192 --shifts 64 --quiet
run_case "small shifts, round-trip-dominated (41 bits x 500)" \
         --bits 41 --shifts 500 --quiet
run_case "IICE trace download: 923 samples x 69 probe bits" \
         --trace 923:69 --quiet

# --------------------------------------------------------------------------
# THE CROSS-CHECK. Stop the daemon and compare its own TCK rising-edge counter
# against the bits we asked for. Equal => every requested bit was actually
# clocked through the TAP, so the rate describes shifting and not accepting.
# --------------------------------------------------------------------------
echo "================================================================"
echo "CROSS-CHECK: server-side TCK edges vs client-side bits requested"
kill "$DAEMON_PID" 2>/dev/null
wait "$DAEMON_PID" 2>/dev/null
DAEMON_PID=""

EDGES="$(sed -n 's/.*TCK rising edges *: *\([0-9]*\).*/\1/p' "$RUN/daemon.log" | tail -1)"
STRAY="$(sed -n 's/.*stray accesses *: *\([0-9]*\).*/\1/p' "$RUN/daemon.log" | tail -1)"
DRIVES="$(sed -n 's/.*DRIVE writes *: *\([0-9]*\).*/\1/p' "$RUN/daemon.log" | tail -1)"
SAMPLES="$(sed -n 's/.*SAMPLE reads *: *\([0-9]*\).*/\1/p' "$RUN/daemon.log" | tail -1)"

if [ -z "$EDGES" ]; then
    echo "FATAL: daemon printed no summary. Its log:" >&2
    tail -30 "$RUN/daemon.log" >&2
    exit 1
fi

echo "  bits requested (incl. warmup) : $TOTAL_BITS"
echo "  TCK rising edges clocked      : $EDGES"
echo "  SAMPLE reads                  : $SAMPLES   (must equal the edges)"
echo "  DRIVE writes                  : $DRIVES   (must be 2x the edges + parks)"
echo "  stray accesses                : $STRAY   (must be 0)"

FAIL=0
[ "$EDGES"  = "$TOTAL_BITS" ] || { echo "  MISMATCH: bits requested != TCK edges"; FAIL=1; }
[ "$SAMPLES" = "$EDGES" ]     || { echo "  MISMATCH: SAMPLE reads != TCK edges"; FAIL=1; }
[ "$STRAY"  = "0" ]           || { echo "  DEFECT: $STRAY accesses outside DRIVE/SAMPLE"; FAIL=1; }

if [ "$FAIL" != "0" ]; then
    echo
    echo "RESULT: the throughput numbers above are NOT TRUSTWORTHY -- the server"
    echo "        did not shift what the client asked for."
    exit 1
fi

echo
echo "RESULT: every requested bit was clocked through the TAP. The rates above"
echo "        describe shifting, and are the engine's board-free ceiling."
exit 0
