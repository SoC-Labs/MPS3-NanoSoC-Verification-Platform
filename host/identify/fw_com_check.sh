#!/bin/sh
# ---------------------------------------------------------------------------
# fw_com_check.sh -- drive the REAL Synopsys Identify debugger against the REAL
# firmware XVC engine, board-free, and turn the outcome into an exit status.
#
#     host/identify/fw_com_check.sh                 # positive + negative control
#     host/identify/fw_com_check.sh --positive-only # one licence seat, faster
#     host/identify/fw_com_check.sh --negative-only
#
# WHAT THIS CLOSES. firmware/xvc_server/xvc_server.c built for the SWDBB target
# (-DMPS3_XVC_TARGET_SWDBB) was proven only by INFERENCE: unit tests push
# hand-built or previously-captured XVC byte streams through it and check the
# bits. The HOST-side Python server (host/socket_harness/xvc_server.py) HAD been
# driven by real Identify -- which is how the getinfo-is-a-chunk-hint defect was
# found -- but the firmware engine only inherited the fix. This script closes
# that gap with no board, no MicroBlaze and no Vivado:
# firmware/test/bin/xvc_fw_daemon links the same xvc_server.c against real POSIX
# sockets and an in-memory IEEE-1149.1 TAP, and the licensed debugger drives it
# over TCP.
#
# BOTH DIRECTIONS, by default. A positive run alone would not distinguish "the
# firmware engine is correct" from "Identify is easy to please", so the default
# also runs bin/xvc_fw_daemon_ratio1 -- the same daemon with the pre-f93d565
# defect restored -- and asserts that the debugger FAILS against it.
#
# WHAT IT PROVES vs WHAT IT DOES NOT
#   PROVES     the real client's getinfo:/settck:/shift: traffic is parsed,
#              bounded, framed and answered by the real C engine; that the bits
#              it shifts navigate a real 1149.1 controller and read back the
#              right IDCODE; and that the accept-ceiling fix is load-bearing for
#              THIS client. Asserted by grepping the DEBUGGER's own words.
#   DOES NOT   prove anything about silicon, the RM's reinterpretation of the four
#              SWD pins, MicroBlaze AXI timing, or that an IICE is readable. The
#              positive run STOPS at "Checking Hardware ID" with signature
#              0x00000000, because there is no IICE behind the model TAP. That
#              stop is EXPECTED and the script still exits 0.
#
# MEASURED TRAP -- do not build a gate on `com check`'s Tcl status. In BOTH the
# passing and the FAILING run above, `com check` returned success to Tcl (the
# "Error:" text is printed by the tool, not raised). The only reliable signal is
# the debugger's own log text, which is why every assertion here is a grep.
# (host/identify/debug_session.tcl's `if {[catch {com check} err]}` guard is
# therefore unreachable -- see this script's report.)
#
# GUARDS (skip, loudly, exit 0) -- the same shape as `make check`'s
# nCompare-gated IICE stage:
#   * identify_debugger_shell not on PATH   -> `module load identify/2022.09-SP2`
#   * no instrumented project with rev_1_identify/ -> gitignored build output
#     (`make -C fpga/rp/nanosoc_iice ...`), absent in a fresh clone.
# Set MPS3_REQUIRE_IDENTIFY=1 to turn either skip into a hard failure.
#
# NOTE each session checks out an `identdebugger` licence seat for ~20 s.
# ---------------------------------------------------------------------------
set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd)
DAEMON="$REPO/firmware/test/bin/xvc_fw_daemon"
DAEMON_NEG="$REPO/firmware/test/bin/xvc_fw_daemon_ratio1"
TCL="$REPO/host/identify/com_check.tcl"

IDCODE=${MPS3_XVC_FW_IDCODE:-0x6BA00477}
PRJ=${IICE_PRJ:-$REPO/fpga/rp/nanosoc_iice/build/nanosoc_iice.prj}
OUTDIR=${MPS3_XVC_FW_OUTDIR:-}
REQUIRE=${MPS3_REQUIRE_IDENTIFY:-0}
MAXSEC=${MPS3_XVC_FW_MAXSEC:-600}

DO_POS=1
DO_NEG=1
for a in "$@"; do
    case "$a" in
        --positive-only) DO_NEG=0 ;;
        --negative-only) DO_POS=0 ;;
        -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
        *) echo "fw_com_check: unknown argument '$a'" >&2; exit 2 ;;
    esac
done

skip() {
    echo "  SKIP fw_com_check: $1"
    if [ "$REQUIRE" = "1" ]; then
        echo "  ...but MPS3_REQUIRE_IDENTIFY=1, so this is a FAILURE." >&2
        exit 1
    fi
    exit 0
}

# ---- guards ---------------------------------------------------------------
command -v identify_debugger_shell >/dev/null 2>&1 || \
    skip "identify_debugger_shell not on PATH ('module load identify/2022.09-SP2')"

[ -f "$PRJ" ] || \
    skip "no instrumented project at $PRJ (gitignored build output; set IICE_PRJ)"

PRJDIR=$(dirname "$PRJ")
[ -d "$PRJDIR/rev_1_identify" ] || \
    skip "$PRJDIR has no rev_1_identify/ -- the debugger needs that whole directory"

# ---- build the daemons if needed ------------------------------------------
if [ ! -x "$DAEMON" ] || [ ! -x "$DAEMON_NEG" ]; then
    echo "  building the XVC firmware daemons"
    make -C "$REPO/firmware/test" tools >/dev/null || {
        echo "fw_com_check: could not build the daemons" >&2
        exit 1
    }
fi

if [ -z "$OUTDIR" ]; then
    OUTDIR=$(mktemp -d "${TMPDIR:-/tmp}/fw_com_check.XXXXXX") || exit 1
fi
mkdir -p "$OUTDIR"

DPID=""
cleanup() {
    if [ -n "$DPID" ]; then
        kill -TERM "$DPID" 2>/dev/null
        i=0
        while [ $i -lt 50 ] && kill -0 "$DPID" 2>/dev/null; do
            i=$((i + 1))
            sleep 0.1
        done
        kill -KILL "$DPID" 2>/dev/null
        wait "$DPID" 2>/dev/null
        DPID=""
    fi
}
trap cleanup EXIT INT TERM

fails=0

# ---- one session: start a daemon, run the debugger at it, stop the daemon --
# Sets DLOG / ILOG for the assertions that follow.
run_session() {
    _bin=$1
    _tag=$2
    DLOG="$OUTDIR/$_tag.daemon.log"
    ILOG="$OUTDIR/$_tag.identify.log"
    _portf="$OUTDIR/$_tag.port"
    _rundir="$OUTDIR/$_tag.run"
    rm -f "$_portf"
    mkdir -p "$_rundir"

    # --port 0 + --port-file: no fixed port to collide with a stale process or a
    # concurrent run. --max-seconds is a hard backstop so a wedged debugger
    # cannot leave a daemon (or a seat) behind forever.
    "$_bin" --port 0 --port-file "$_portf" --max-seconds "$MAXSEC" \
        >"$DLOG" 2>&1 &
    DPID=$!

    i=0
    while [ $i -lt 100 ] && [ ! -s "$_portf" ]; do
        i=$((i + 1))
        sleep 0.1
    done
    if [ ! -s "$_portf" ]; then
        echo "  FAIL [$_tag] daemon never reported a port; log: $DLOG" >&2
        cat "$DLOG" >&2
        fails=$((fails + 1))
        cleanup
        return 1
    fi
    _port=$(cat "$_portf")
    echo "  [$_tag] $(basename "$_bin") listening on 127.0.0.1:$_port (pid $DPID)"
    echo "  [$_tag] running identify_debugger_shell (checks out an identdebugger seat)"
    (
        cd "$_rundir" || exit 1
        IICE_XVC_HOST=127.0.0.1 IICE_XVC_PORT="$_port" \
        IICE_XVC_SPEED_NS="${IICE_XVC_SPEED_NS:-1000000}" \
            identify_debugger_shell -prj "$PRJ" -f "$TCL"
    ) >"$ILOG" 2>&1
    echo "  [$_tag] debugger exited $? ; session log: $ILOG"
    cleanup
    return 0
}

# Every assertion greps the DEBUGGER's own output. com_check.tcl's own puts lines
# are STRIPPED first, and that is structural, not cosmetic: an earlier draft of
# com_check.tcl printed the debugger's success wording as a triage hint, so the
# NEGATIVE CONTROL passed the cable-check assertion by matching our own log line.
# A gate that can satisfy itself is worse than no gate, so the evidence stream is
# filtered down to lines the tool emitted.
tool_log() { grep -v '^MPS3_COMCHECK' "$ILOG"; }

want() {    # want <label> <fixed-string>   -- must be PRESENT in the TOOL's output
    if tool_log | grep -qF "$2"; then
        echo "  PASS $1"
    else
        echo "  FAIL $1 -- expected from the debugger in $ILOG: $2" >&2
        fails=$((fails + 1))
    fi
}
want_not() { # want_not <label> <fixed-string> -- must be ABSENT from the TOOL's output
    if tool_log | grep -qF "$2"; then
        echo "  FAIL $1 -- must NOT appear in $ILOG: $2" >&2
        fails=$((fails + 1))
    else
        echo "  PASS $1"
    fi
}

# The IDCODE the debugger prints, in the binary form it uses, DERIVED from the
# value we told the daemon's TAP to present -- so a wrong bit order or sampling
# phase fails HERE instead of reading as a mystery device.
IDBIN=$(python3 -c "print(format($IDCODE, '032b'))" 2>/dev/null || true)
if [ -z "$IDBIN" ]; then
    echo "  FAIL could not compute the expected IDCODE bit string" >&2
    fails=$((fails + 1))
fi

# ---- POSITIVE -------------------------------------------------------------
if [ "$DO_POS" = "1" ]; then
    echo "== POSITIVE: real Identify vs the SHIPPED firmware XVC engine ======"
    if run_session "$DAEMON" positive; then
        want "XVC handshake (getinfo: parsed by the firmware engine)" \
             "XVC connection to 'xvcServer_v1.0' established"
        want "shift: traffic reached the firmware engine and answered correctly" \
             "The hardware is responding correctly."
        want "the debugger ran a real DR scan (chain autodetect)" \
             "Auto-detecting the device chain..."
        [ -n "$IDBIN" ] && want "IDCODE read back == $IDCODE" "IDCODE is $IDBIN"

        # The daemon must agree that it really did the shifting, and that
        # Identify's over-advertised 2053-bit shift really was served -- the
        # command that is 524 bytes, two more than the pre-fix accumulator held.
        if grep -q "TCK rising edges" "$DLOG"; then
            edges=$(sed -n 's/.*TCK rising edges *: *\([0-9]*\).*/\1/p'   "$DLOG" | tail -1)
            stray=$(sed -n 's/.*stray accesses *: *\([0-9]*\).*/\1/p'     "$DLOG" | tail -1)
            bursts=$(sed -n 's/.*shift bursts served *: *\([0-9]*\).*/\1/p' "$DLOG" | tail -1)
            echo "  daemon: $bursts shift bursts, ${edges:-?} TCK edges, ${stray:-?} stray"
            if [ "${edges:-0}" -gt 0 ] 2>/dev/null; then
                echo "  PASS the firmware bit-bang loop really ran (>0 TCK edges)"
            else
                echo "  FAIL the daemon saw no TCK edges at all" >&2
                fails=$((fails + 1))
            fi
            if [ "${stray:-1}" = "0" ]; then
                echo "  PASS no access outside DRIVE@0x00 / SAMPLE@0x04"
            else
                echo "  FAIL the firmware touched an offset swd_bb.sv does not decode" >&2
                fails=$((fails + 1))
            fi
            if grep -q "shift: +2053 TCK edges" "$DLOG"; then
                echo "  PASS the 2053-bit over-advertised shift was served (524-byte command)"
            else
                echo "  FAIL no 2053-bit shift in the daemon log -- the client did not" >&2
                echo "       exercise the accept-ceiling path, so the positive result" >&2
                echo "       does not cover the defect it is supposed to cover" >&2
                fails=$((fails + 1))
            fi
        else
            echo "  FAIL the daemon printed no summary (did it crash?); log: $DLOG" >&2
            fails=$((fails + 1))
        fi

        # The EXPECTED stopping point, stated so it cannot be quietly mistaken
        # for a full IICE read. If this line ever DISAPPEARS something changed:
        # either a real IICE appeared behind the TAP (good -- update this script)
        # or the debugger stopped even earlier (bad).
        if grep -qF "Checking Hardware ID" "$ILOG"; then
            echo "  NOTE reached 'Checking Hardware ID' -- as expected, the model TAP"
            echo "       carries no IICE signature, so the debugger stops there. This"
            echo "       is NOT an IICE read and must not be reported as one."
        fi
    fi
fi

# ---- NEGATIVE CONTROL -----------------------------------------------------
if [ "$DO_NEG" = "1" ]; then
    echo "== NEGATIVE CONTROL: the pre-fix accept ceiling must FAIL ==========="
    if run_session "$DAEMON_NEG" negative; then
        # The handshake still works -- the defect is specific to the big shift,
        # which is exactly why it survived so long.
        want "the defective build still completes the getinfo: handshake" \
             "XVC connection to 'xvcServer_v1.0' established"
        # ...and then the cable check must NOT succeed.
        want_not "the cable check must NOT pass against the pre-fix ceiling" \
             "The hardware is responding correctly."
        [ -n "$IDBIN" ] && want_not "no IDCODE may be read against the pre-fix ceiling" \
             "IDCODE is $IDBIN"
        # Prove we really ran the DEFECTIVE build, not a second copy of the good one.
        if grep -q "advertised=2048 accepted=2048" "$DLOG"; then
            echo "  PASS the negative daemon really had the ceiling re-coupled (accepted=2048)"
        else
            echo "  FAIL the negative daemon was not built with MPS3_XVC_ACCEPT_RATIO=1u" >&2
            fails=$((fails + 1))
        fi
        if grep -q "shift: +2053 TCK edges" "$DLOG"; then
            echo "  FAIL the defective build served a 2053-bit shift -- it must fail closed" >&2
            fails=$((fails + 1))
        else
            echo "  PASS the defective build refused the 2053-bit shift (failed closed)"
        fi
    fi
fi

echo "  artifacts: $OUTDIR"
if [ "$fails" -ne 0 ]; then
    echo "fw_com_check: $fails assertion(s) FAILED" >&2
    exit 1
fi
echo "fw_com_check: OK -- real Identify drove the real firmware XVC engine,"
echo "               and provably fails against the pre-fix ceiling"
exit 0
