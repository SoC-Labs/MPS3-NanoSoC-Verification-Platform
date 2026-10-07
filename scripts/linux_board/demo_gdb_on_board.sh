#!/bin/bash
# demo_gdb_on_board.sh -- the on-board GDB server's measurement session
# (OPENOCD_ON_HARNESS_SCOPE §6): OpenOCD ON the board vs OpenOCD on the host, on one
# claimed Linux board, about an hour. It changes nothing on the board but /tmp, and it
# never writes the DUT's flash (the DUT's DMEM is READ; the core is halted and resumed).
#
# NEEDS (the operator's, in a booked slot -- this script takes no lease itself):
#   * david's OK and the fpgahub lease for the board, the board CLAIMED by the key
#     below, and Harness Manager NOT attached to it (6900 and 6921 are single-client);
#   * a design with a DAP loaded (nanosoc / nanosoc_upy; DESIGN= to name it);
#   * for the host baseline: an OpenOCD with remote_bitbang on THIS host (the hub's
#     xPack; the SoC Labs build has none): HOST_OPENOCD=; optional arm-none-eabi-gdb.
#
#   B=root@<board-ip> SSH_OPTS="-J <hub> -i <claim key>" scripts/linux_board/demo_gdb_on_board.sh
#
# Environment (defaults in []):
#   B               ssh destination of the board (root@...)                    [required]
#   SSH_OPTS        extra ssh options (jump host, key)                          []
#   MODE            image | tmp: use the image's /usr/bin/openocd + mps3-debug, or copy
#                   OCD_BIN + the cfgs (+ MPS3_DEBUG_BIN) to /tmp/mps3dbg on the board  [image]
#   OCD_BIN         rv32 openocd for MODE=tmp (scratchpad/ocd_rv32/openocd.stripped)
#   MPS3_DEBUG_BIN  rv32 mps3-debug for MODE=tmp (optional: without it step 5 is skipped)
#   DESIGN          the recipe for mps3-debug / the cfg set                     [nanosoc]
#   HOST_OPENOCD    host OpenOCD with remote_bitbang ("" = skip the baseline)   [openocd]
#   GDB             arm-none-eabi-gdb for the GDB steps ("" = skip)             [auto]
#   RUNS            timed repeats per path                                      [3]
#   OUT             where the logs go                                           [./demo_gdb_<ts>]
#   L_CTRL L_RBB L_GDB   local forward ports                          [16900 16921 13333]
#
# Steps (each timed by OpenOCD's own `ms` clock -- scripts/linux_board/mps3_timing.tcl --
# so ssh and process start-up are not in the numbers; a 6900 `ping` loop runs through
# every step to show what the board's control plane sees):
#   0. facts: image version, harnessd pid + OS uptime (a WDOG reset or a harnessd
#      respawn shows up as a change at the end), identify, 6921 idle
#   1. BASELINE: host OpenOCD -> the claim's forward -> 6921: init, halt, reg, 4 KiB DMEM
#   2. ON-BOARD: the same probe run by OpenOCD on the board, nice 10, to 127.0.0.1:6921
#   3. (GDB) host GDB -> host OpenOCD's 3333 (baseline): info registers + 4 KiB dump
#   4. (GDB) mps3-debug up; host GDB -> forwarded 3333 -> on-board OpenOCD: the same
#   5. mps3-debug status/down; nothing left running; harnessd + uptime unchanged
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
: "${B:?set B=root@<board-ip> (and SSH_OPTS for the jump host / claim key)}"
SSH_OPTS=${SSH_OPTS:-}
MODE=${MODE:-image}
DESIGN=${DESIGN:-nanosoc}
HOST_OPENOCD=${HOST_OPENOCD-openocd}
RUNS=${RUNS:-3}
L_CTRL=${L_CTRL:-16900}; L_RBB=${L_RBB:-16921}; L_GDB=${L_GDB:-13333}; L_HGDB=${L_HGDB:-13334}
OUT=${OUT:-$PWD/demo_gdb_$(date -u +%Y%m%dT%H%M%SZ)}
CFG_DIR=$REPO/host/openocd
TIMING=$HERE/mps3_timing.tcl
if [ -z "${GDB+x}" ]; then GDB=$(command -v arm-none-eabi-gdb || command -v gdb-multiarch || true); fi
mkdir -p "$OUT"
LOG=$OUT/demo.log
exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%H:%M:%SZ; }
say() { echo "[$(ts)] $*"; }
die() { say "STOP: $*"; exit 1; }

CTL=$OUT/ssh.ctl
# shellcheck disable=SC2086
SSHB() { ssh -S "$CTL" $SSH_OPTS "$B" "$@"; }
cleanup() {
    [ -n "${PING_PID:-}" ] && kill "$PING_PID" 2>/dev/null
    [ -n "${HOCD_PID:-}" ] && kill "$HOCD_PID" 2>/dev/null
    ssh -S "$CTL" -O exit "$B" 2>/dev/null
}
trap cleanup EXIT

say "== one SSH connection (a dropbear login costs ~13 s on the MBV; every step reuses it)"
# shellcheck disable=SC2086
ssh -M -S "$CTL" -fN -o ControlPersist=yes -o ServerAliveInterval=15 $SSH_OPTS \
    -L "127.0.0.1:$L_CTRL:127.0.0.1:6900" -L "127.0.0.1:$L_RBB:127.0.0.1:6921" \
    -L "127.0.0.1:$L_GDB:127.0.0.1:3333" "$B" || die "ssh to $B failed"
SSHB true || die "ssh multiplexing failed"

ctrl() {   # one 6900 request through the forward -> the reply line
    python3 - "$L_CTRL" "$1" <<'PY'
import socket, sys
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=10)
s.sendall(sys.argv[2].encode() + b"\n")
buf = b""
while not buf.endswith(b"\n"):
    d = s.recv(4096)
    if not d:
        break
    buf += d
print(buf.decode(errors="replace").strip())
PY
}

ping_loop() {   # ping_loop <tag>: 6900 ping RTTs until killed -> $OUT/ping_<tag>.txt
    python3 - "$L_CTRL" "$OUT/ping_$1.txt" <<'PY' &
import signal, socket, sys, time
port, out = int(sys.argv[1]), sys.argv[2]
rtts, errs = [], 0
def done(*_):
    r = sorted(rtts)
    q = lambda p: r[min(len(r) - 1, int(p * len(r)))] if r else float("nan")
    with open(out, "w") as f:
        f.write("n=%d errors=%d p50_ms=%.1f p99_ms=%.1f max_ms=%.1f\n"
                % (len(r), errs, q(0.5), q(0.99), r[-1] if r else float("nan")))
    sys.exit(0)
signal.signal(signal.SIGTERM, done)
s = None
while True:
    try:
        if s is None:
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
        t0 = time.monotonic()
        s.sendall(b'{"op":"ping"}\n')
        buf = b""
        while not buf.endswith(b"\n"):
            d = s.recv(4096)
            if not d:
                raise OSError("closed")
            buf += d
        rtts.append((time.monotonic() - t0) * 1e3)
    except OSError:
        errs += 1
        s = None
        time.sleep(0.5)
    time.sleep(0.2)
PY
    PING_PID=$!
}
ping_stop() { kill "$PING_PID" 2>/dev/null; wait "$PING_PID" 2>/dev/null; PING_PID=""; cat "$OUT/ping_$1.txt"; }

facts() {   # facts <tag>: what would show a reset or a respawn
    SSHB 'echo "uptime=$(cut -d" " -f1 /proc/uptime) harnessd_pid=$(pidof mps3-harnessd) 6921_estab=$(awk '"'"'$4=="01" && $2 ~ /:1B09$/'"'"' /proc/net/tcp | wc -l)"' \
        | tee "$OUT/facts_$1.txt"
}

say "== 0. facts (board $B, MODE=$MODE, DESIGN=$DESIGN)"
SSHB 'grep -E "^(harness|sha|dirty|build_date)=" /etc/mps3/version; command -v mps3-debug openocd' | tee "$OUT/image.txt"
facts before
ctrl '{"op":"ping"}' | tee "$OUT/ping0.json"
ctrl '{"op":"version"}' > "$OUT/version.json"
grep -q '"ok":true' "$OUT/ping0.json" || die "6900 does not answer through the forward (is HM attached?)"

if [ "$MODE" = tmp ]; then
    say "== copy OpenOCD + the cfgs to /tmp/mps3dbg on the board (RAM; gone at reboot)"
    [ -f "${OCD_BIN:-}" ] || die "MODE=tmp needs OCD_BIN (the rv32 openocd)"
    SSHB 'mkdir -p /tmp/mps3dbg && cat > /tmp/mps3dbg/openocd && chmod 755 /tmp/mps3dbg/openocd' < "$OCD_BIN"
    for f in "$CFG_DIR"/*.cfg "$CFG_DIR"/*.tcl "$TIMING"; do
        SSHB "cat > /tmp/mps3dbg/$(basename "$f")" < "$f"
    done
    if [ -f "${MPS3_DEBUG_BIN:-}" ]; then
        SSHB 'cat > /tmp/mps3dbg/mps3-debug && chmod 755 /tmp/mps3dbg/mps3-debug' < "$MPS3_DEBUG_BIN"
        SSHB "cat > /tmp/mps3dbg/designs.conf" < "$REPO/src/linux_harness/sw/br2_external/package/mps3-debug/designs.conf"
        echo "tmp copy (MODE=tmp)" | SSHB 'cat > /tmp/mps3dbg/VERSION'
    fi
    B_OCD=/tmp/mps3dbg/openocd; B_CFG=/tmp/mps3dbg
    B_DBG="MPS3_DEBUG_OPENOCD=/tmp/mps3dbg/openocd MPS3_DEBUG_SHARE=/tmp/mps3dbg MPS3_DEBUG_USER= /tmp/mps3dbg/mps3-debug"
    [ -f "${MPS3_DEBUG_BIN:-}" ] || B_DBG=""
else
    B_OCD=/usr/bin/openocd; B_CFG=/usr/share/mps3/openocd
    SSHB "cat > /tmp/mps3_timing.tcl" < "$TIMING"
    B_DBG=mps3-debug
fi
B_TIMING=$([ "$MODE" = tmp ] && echo /tmp/mps3dbg/mps3_timing.tcl || echo /tmp/mps3_timing.tcl)
case "$DESIGN" in
    nanosoc|nanosoc_upy|nanosoc_ila) CFGS=(nanosoc_mps3_jtag.cfg nanosoc_ops.tcl) ;;
    nanosoc_iice) CFGS=(nanosoc_iice_chain.cfg nanosoc_ops.tcl) ;;
    *) die "DESIGN=$DESIGN: the timed probe knows nanosoc, nanosoc_upy, nanosoc_ila, nanosoc_iice" ;;
esac

timing_line() { grep -a '^TIMING' "$1" | tail -1; }

say "== 1. BASELINE: host OpenOCD -> the claim's forward -> 6921 (today's path)"
if [ -n "$HOST_OPENOCD" ] && command -v "$HOST_OPENOCD" >/dev/null 2>&1; then
    ping_loop host
    for r in $(seq 1 "$RUNS"); do
        "$HOST_OPENOCD" -s "$CFG_DIR" -c "set RBB_HOST 127.0.0.1" -c "set RBB_PORT $L_RBB" \
            -c "set TRANSPORT_MODE rbb" -f "${CFGS[0]}" -f "${CFGS[1]}" -f "$TIMING" \
            -c "gdb_port disabled; telnet_port disabled; tcl_port disabled" \
            -c "mps3_timed $OUT/dmem_host_$r.bin" -c shutdown > "$OUT/host_run$r.log" 2>&1
        rc=$?
        say "   host run $r: $(timing_line "$OUT/host_run$r.log") (rc $rc)"
        sleep 2          # 6921 accepts before it drains the last Q (HM's cooldown)
    done
    say "   6900 during the host runs: $(ping_stop host)"
else
    say "   SKIP: no host OpenOCD with remote_bitbang (HOST_OPENOCD=$HOST_OPENOCD)"
fi

say "== 2. ON-BOARD: the same probe, OpenOCD on the board (nice 10) -> 127.0.0.1:6921"
ping_loop board
for r in $(seq 1 "$RUNS"); do
    SSHB "cd $B_CFG && nice -n 10 $B_OCD -s $B_CFG -c 'set RBB_HOST 127.0.0.1' -c 'set RBB_PORT 6921' \
        -c 'set TRANSPORT_MODE rbb' -f ${CFGS[0]} -f ${CFGS[1]} -f $B_TIMING \
        -c 'gdb_port disabled; telnet_port disabled; tcl_port disabled' \
        -c 'mps3_timed /tmp/dmem_board_$r.bin' -c shutdown 2>&1" > "$OUT/board_run$r.log"
    say "   board run $r: $(timing_line "$OUT/board_run$r.log")"
    sleep 2
done
say "   6900 during the on-board runs: $(ping_stop board)"
SSHB 'cat /tmp/dmem_board_1.bin' > "$OUT/dmem_board_1.bin"
if [ -f "$OUT/dmem_host_1.bin" ]; then
    say "   4 KiB DMEM host vs board: $(cmp -s "$OUT/dmem_host_1.bin" "$OUT/dmem_board_1.bin" && echo IDENTICAL || echo 'differ (a running core writes DMEM between runs: expected unless it is idle)')"
fi

gdb_probe() {   # gdb_probe <port> <tag>: registers + a 4 KiB dump, wall-clock timed
    local t0 t1 rc
    t0=$(date +%s.%N)
    "$GDB" -batch -nx -ex "set remotetimeout 60" -ex "target extended-remote 127.0.0.1:$1" \
        -ex "info registers" -ex "dump binary memory $OUT/dmem_gdb_$2.bin 0x18000000 0x18001000" \
        -ex "detach" > "$OUT/gdb_$2.log" 2>&1
    rc=$?
    t1=$(date +%s.%N)
    echo "gdb $2: $(python3 -c "print('%.2f s' % ($t1 - $t0))") (rc $rc, $(grep -c '^r[0-9]\|^pc\|^sp' "$OUT/gdb_$2.log") register lines)"
}

if [ -n "$GDB" ] && [ -n "$HOST_OPENOCD" ] && command -v "$HOST_OPENOCD" >/dev/null 2>&1; then
    say "== 3. GDB via the HOST OpenOCD (baseline)"
    "$HOST_OPENOCD" -s "$CFG_DIR" -c "set RBB_HOST 127.0.0.1" -c "set RBB_PORT $L_RBB" \
        -c "set TRANSPORT_MODE rbb" -f "${CFGS[0]}" -f "${CFGS[1]}" \
        -c "nanosoc.cpu0 configure -event gdb-attach nanosoc_halt_examine" \
        -c "gdb_port $L_HGDB; telnet_port disabled; tcl_port disabled; bindto 127.0.0.1" \
        > "$OUT/host_ocd_gdb.log" 2>&1 &
    HOCD_PID=$!
    sleep 8
    say "   $(gdb_probe "$L_HGDB" host)"
    kill "$HOCD_PID" 2>/dev/null; wait "$HOCD_PID" 2>/dev/null; HOCD_PID=""
    sleep 2
else
    say "== 3. SKIP (no GDB or no host OpenOCD)"
fi

if [ -n "$B_DBG" ]; then
    say "== 4. mps3-debug up --rm $DESIGN (OpenOCD on the board, as the openocd user)"
    SSHB "$B_DBG up --rm $DESIGN --json; echo RC=\$?" | tee "$OUT/mps3_debug_up.json"
    if [ -n "$GDB" ]; then
        ping_loop gdb
        say "   $(gdb_probe "$L_GDB" board)"
        say "   6900 during on-board GDB: $(ping_stop gdb)"
    fi
    say "== 5. mps3-debug status + down"
    SSHB "$B_DBG status --json" | tee "$OUT/mps3_debug_status.json"
    SSHB "$B_DBG down --json" | tee "$OUT/mps3_debug_down.json"
    SSHB 'ps | grep -v grep | grep -c openocd' | sed 's/^/   openocd processes left: /'
else
    say "== 4/5. SKIP (MODE=tmp without MPS3_DEBUG_BIN)"
fi

say "== after"
facts after
python3 - "$OUT/facts_before.txt" "$OUT/facts_after.txt" <<'PY'
import sys
b = dict(kv.split("=", 1) for kv in open(sys.argv[1]).read().split())
a = dict(kv.split("=", 1) for kv in open(sys.argv[2]).read().split())
ok = float(a["uptime"]) > float(b["uptime"]) and a["harnessd_pid"] == b["harnessd_pid"]
print("   %s: OS uptime %s -> %s s, harnessd pid %s -> %s (no WDOG reset, no respawn)"
      % ("OK" if ok else "CHANGED", b["uptime"], a["uptime"], b["harnessd_pid"], a["harnessd_pid"]))
PY
say "== summary"
for f in "$OUT"/host_run*.log "$OUT"/board_run*.log; do
    [ -f "$f" ] && printf '   %-14s %s\n' "$(basename "$f" .log)" "$(timing_line "$f")"
done
for f in "$OUT"/ping_*.txt; do [ -f "$f" ] && printf '   6900 %-8s %s\n' "$(basename "$f" .txt | sed 's/ping_//')" "$(cat "$f")"; done
say "logs: $OUT"
