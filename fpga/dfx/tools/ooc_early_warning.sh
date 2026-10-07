#!/usr/bin/env bash
# =============================================================================
# ooc_early_warning.sh -- which RMs does Vivado 2026.1 break? (LINUX_HARNESS_PLAN
# §6 risk 1: "2026.1 breaks some of the 13 RMs"; FLOW lane deliverable 7.)
#
# OOC-synthesises every RM under the Vivado you name, in a scratch dir OUTSIDE
# the repo, through the SAME recipes a mint uses:
#   * prebuilt RMs -> `make -C fpga/dfx rm-<name>-dcp` (the registered OOC script,
#     its env, its PREP/POST, its COMPLETE-marker check), with every pre-produced
#     checkpoint reuse disabled so nothing 2024.1 is copied in;
#   * inline RMs   -> tools/ooc_inline.tcl (build_dfx.tcl's inline path, lifted).
# Then it writes <out>/REPORT.md: per RM, PASS/FAIL, the first ERROR lines, the
# CRITICAL WARNING count and any IP upgrade/lock messages.
#
# NOTHING HEAVY RUNS UNLESS YOU SAY --run. The default is --list: what each RM
# pulls (mode, OOC script, external trees, Xilinx IP it creates and at which
# version), no Vivado, seconds.
#
# BUILD-HOST RULES (LINUX_HARNESS_PLAN §4): --run refuses unless --mint-log names
# a mint log whose tail says "MINT COMPLETE". Everything runs under `nice -n 19`,
# at most 4 Vivado jobs at once. <out> must not be inside the repo.
#
# A mint that FAILED after its Vivado work ended never prints MINT COMPLETE (the
# 2026-10 ILA mint ended MINT_EXIT=2 at the firmware bake, recovered by hand).
# For that case, and only by name: --mint-finished-no-complete-line. It does
# not trust the operator; it VERIFIES, and refuses unless all three hold:
#   1. the log ends with the mint driver's `MINT_EXIT=<rc>` line (make returned);
#   2. the log has not been written for 10 minutes;
#   3. no vivado / loader / updatemem / xsct / xsdb / make process has the mint
#      worktree (the log's directory) on its command line or as its cwd.
# The evidence is printed and written into REPORT.md. (It replaced an
# unverified --host-is-free switch, 2026-09-23.)
#
#   tools/ooc_early_warning.sh --list
#   tools/ooc_early_warning.sh --run --out /scratch/ooc2026 \
#       --mint-log <mint worktree>/mint_2026_10.log --tools-env <repo>/tools.env
#   tools/ooc_early_warning.sh --run ... --mint-log <log> --mint-finished-no-complete-line
#   tools/ooc_early_warning.sh --report --out /scratch/ooc2026   # re-summarise
#
# Options: --vivado <path|2024.1|2026.1> (default 2026.1)  --jobs N (<=4, default 4)
#          --rms "greybox led ..." (default: the 13 of the plan)
# =============================================================================
set -uo pipefail
# The whole script is ONE compound command ({ ... } below), so bash parses all of
# it before running any: bash reads a plain script as it goes, and an edit made to
# this file while a one-hour run was in flight killed the 2026-09-23 run's report
# step (the synth results survived; --report regenerated it).
{

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DFX="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$DFX/../.." && pwd)"
RMS_DEFAULT="greybox regdemo_a regdemo_b led uart_echo nanosoc eth_ss nanosoc_multicore socscope nanosoc_upy clcd_demo dbg_demo nanosoc_ila"

MODE=list; OUT=""; MINT_LOG=""; MINT_FINISHED=0; TOOLS_ENV=""; JOBS=4; RMS="$RMS_DEFAULT"
GATE_EVIDENCE=""
VIVADO_ARG=2026.1
while [ $# -gt 0 ]; do
    case "$1" in
        --list) MODE=list ;;
        --run) MODE=run ;;
        --report) MODE=report ;;
        --out) OUT="$2"; shift ;;
        --mint-log) MINT_LOG="$2"; shift ;;
        --mint-finished-no-complete-line) MINT_FINISHED=1 ;;
        --tools-env) TOOLS_ENV="$2"; shift ;;
        --jobs) JOBS="$2"; shift ;;
        --rms) RMS="$2"; shift ;;
        --vivado) VIVADO_ARG="$2"; shift ;;
        -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

case "$VIVADO_ARG" in
    2026.1) VIVADO=/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado ;;
    2024.1) VIVADO=/apps/Xilinx/Vivado/2024.1/bin/vivado ;;
    *) VIVADO="$VIVADO_ARG" ;;
esac

mode_of() {  # rm_key -> inline|prebuilt (from rm_list.tcl, the registry)
    printf 'source %s\nputs [rm_field %s synth_mode]\n' "$DFX/rm_list.tcl" "$1" | tclsh 2>/dev/null
}
ooc_script_of() {  # rm_key -> the registered OOC script (Makefile), or ""
    sed -n "s/^RM_SYNTH_TCL_$1[[:space:]]*:=[[:space:]]*\$(REPO_ROOT)\/\(.*\)$/\1/p" "$DFX/Makefile" | head -1
}

# --------------------------------------------------------------------------- list
if [ "$MODE" = list ]; then
    printf '%-18s %-8s %-44s %s\n' RM mode "OOC script" "Xilinx IP it creates (create_ip; version as written)"
    for r in $RMS; do
        k="rm_$r"; m="$(mode_of "$k")"
        if [ "$m" = inline ]; then
            s="(build_dfx.tcl inline: $(printf 'source %s\nputs [rm_field %s wrapper_dir]\n' "$DFX/rm_list.tcl" "$k" | tclsh)/*.sv)"
            ip="none (one self-contained .sv)"
        else
            s="$(ooc_script_of "$k")"
            files="$REPO/$s"
            grep -q "dbg_ip.tcl" "$REPO/$s" 2>/dev/null && files="$files $REPO/fpga/rp/common/dbg_ip.tcl"
            ip="$(grep -hoE 'create_ip -name [a-z_0-9]+( -vendor [a-z.]+)?( -library [a-z]+)?( -version [0-9.]+)?' $files 2>/dev/null \
                  | sed -E 's/ -vendor [a-z.]+//; s/ -library [a-z]+//; s/create_ip -name //' | sort -u | tr '\n' ',' | sed 's/,$//; s/,/, /g')"
            [ -n "$ip" ] || ip="none (RTL only)"
        fi
        printf '%-18s %-8s %-44s %s\n' "$r" "${m:-?}" "$s" "$ip"
    done
    echo
    echo "A version pinned in create_ip is the one a newer catalog can drop; an unpinned one"
    echo "silently takes the new catalog's revision. See FLOW_CONTRACT.md appendix A."
    exit 0
fi

# --------------------------------------------------------------------------- guards
[ -n "$OUT" ] || { echo "error: --out <dir outside the repo> is required" >&2; exit 2; }
OUT="$(realpath -m "$OUT")"
case "$OUT/" in "$REPO"/*) echo "error: --out $OUT is inside the repo ($REPO); use a scratch dir" >&2; exit 2 ;; esac

report() {
    local rep="$OUT/REPORT.md" pass=0 fail=0
    {
        echo "# OOC early warning -- $(date -u +%FT%TZ), Vivado $VIVADO_ARG"
        echo
        echo "| RM | result | CRITICAL WARNINGs | IP upgrade/lock msgs | first ERROR |"
        echo "|---|---|---|---|---|"
        for r in $RMS; do
            local k="rm_$r" log="$OUT/logs/$r.log" dcp res err cw ipm
            if [ "$(mode_of "$k")" = inline ]; then dcp="$OUT/inline/${k}_synth.dcp"; else dcp="$OUT/prod/${k}_synth.dcp"; fi
            if [ ! -f "$log" ]; then res="NOT RUN"; fail=$((fail+1))
            elif [ -f "$dcp" ] && ! grep -qs "^ERROR:" "$log" "$OUT/logs/$r.make.log"; then res=PASS; pass=$((pass+1))
            else res=FAIL; fail=$((fail+1)); fi
            cw="$(grep -c '^CRITICAL WARNING' "$log" 2>/dev/null || true)"
            ipm="$(grep -cE 'IP_Flow 19-(2162|3664|1972|4995)|is locked|upgrade_ip|IP definition .* not found' "$log" 2>/dev/null || true)"
            err="$(grep -h -m1 -E '^ERROR:|^error:' "$log" "$OUT/logs/$r.make.log" 2>/dev/null | head -1 | cut -c1-160 | tr '|' '/')"
            echo "| $r | $res | ${cw:-0} | ${ipm:-0} | ${err:--} |"
        done
        echo
        echo "PASS $pass, FAIL/NOT RUN $fail. Logs: $OUT/logs/<rm>.log; checkpoints: $OUT/prod, $OUT/inline."
        if [ -f "$OUT/GATE_EVIDENCE.txt" ]; then echo; echo "Build-host gate:"; sed 's/^/    /' "$OUT/GATE_EVIDENCE.txt"; fi
    } > "$rep"
    cat "$rep"
}
if [ "$MODE" = report ]; then report; exit 0; fi

mint_finished_evidence() {  # $1 = mint log. Prints evidence; rc 0 only if all 3 hold.
    python3 - "$1" <<'PY'
import os, re, sys, time
log = os.path.realpath(sys.argv[1])
wt = os.path.dirname(log)
ok = True
with open(log, "rb") as fh:
    fh.seek(max(0, os.path.getsize(log) - 4096))
    tail = fh.read().decode("latin-1")
m = re.search(r"^MINT_EXIT=(\d+)\s*\Z", tail, re.M)
if m:
    print("  evidence 1: %s ends MINT_EXIT=%s -- the mint's make returned" % (log, m.group(1)))
else:
    print("  FAIL 1: %s does not end with MINT_EXIT=<rc> -- the mint driver has not returned" % log)
    ok = False
age = time.time() - os.path.getmtime(log)
if age >= 600:
    print("  evidence 2: the log was last written %d min ago" % (age // 60))
else:
    print("  FAIL 2: the log was written %d s ago (< 10 min)" % age)
    ok = False
TOOLS = {"vivado", "loader", "updatemem", "xsct", "xsdb", "make", "gmake"}
busy = []
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) == os.getpid():
        continue
    try:
        argv = [a.decode("latin-1") for a in open("/proc/%s/cmdline" % pid, "rb").read().split(b"\0") if a]
        cwd = os.readlink("/proc/%s/cwd" % pid)
    except OSError:
        continue
    if not argv or not any(os.path.basename(a) in TOOLS for a in argv[:2]):
        continue
    if any(wt in a for a in argv) or cwd == wt or cwd.startswith(wt + os.sep):
        busy.append("pid %s: %s" % (pid, " ".join(argv)[:160]))
if busy:
    print("  FAIL 3: a build tool still references %s:" % wt)
    for b in busy:
        print("    " + b)
    ok = False
else:
    print("  evidence 3: no vivado/loader/updatemem/xsct/xsdb/make process references %s" % wt)
sys.exit(0 if ok else 1)
PY
}
[ -n "$MINT_LOG" ] && [ -f "$MINT_LOG" ] || { echo "error: --run needs --mint-log <the mint's log>: the build host is reserved for the mint until it is done" >&2; exit 2; }
if tail -40 "$MINT_LOG" | grep -q "MINT COMPLETE"; then
    GATE_EVIDENCE="$MINT_LOG ends in MINT COMPLETE"
elif [ "$MINT_FINISHED" = 1 ]; then
    echo "--mint-finished-no-complete-line: verifying the mint is finished, not trusting it:"
    if ! ev="$(mint_finished_evidence "$MINT_LOG")"; then
        echo "$ev"; echo "refusing: the mint is not provably finished (see FAIL above)." >&2; exit 3
    fi
    echo "$ev"
    GATE_EVIDENCE="--mint-finished-no-complete-line, verified:
$ev"
else
    echo "refusing: $MINT_LOG does not end in MINT COMPLETE -- the mint still owns the build host. Ready to run once it does (or, for a mint that failed after its Vivado work, --mint-finished-no-complete-line, which verifies)." >&2
    exit 3
fi
[ "$JOBS" -ge 1 ] && [ "$JOBS" -le 4 ] || { echo "error: --jobs must be 1..4 (plan §4: at most 4 parallel Vivado jobs)" >&2; exit 2; }
[ -x "$VIVADO" ] || { echo "error: no Vivado at $VIVADO" >&2; exit 2; }

# --------------------------------------------------------------------------- run
mkdir -p "$OUT/logs" "$OUT/inline" || exit 2
printf '%s\n' "$GATE_EVIDENCE" > "$OUT/GATE_EVIDENCE.txt"
MK="$OUT/ooc.mk"
{
    echo "# generated by fpga/dfx/tools/ooc_early_warning.sh -- one target per RM, -j$JOBS -k"
    printf 'all:'; for r in $RMS; do printf ' %s.done' "$r"; done; echo
    for r in $RMS; do
        k="rm_$r"
        if [ "$(mode_of "$k")" = inline ]; then
            printf '%s.done:\n\tcd %s/inline && %s -mode batch -source %s -journal %s/logs/%s.jou -log %s/logs/%s.log -tclargs %s %s %s/inline && grep -q "^OOC_INLINE_COMPLETE %s" %s/logs/%s.log && touch %s/$@\n' \
                "$r" "$OUT" "$VIVADO" "$HERE/ooc_inline.tcl" "$OUT" "$r" "$OUT" "$r" "$REPO" "$k" "$OUT" "$k" "$OUT" "$r" "$OUT"
        else
            # every pre-produced / REUSE checkpoint disabled: nothing 2024.1 may be copied in
            printf '%s.done:\n\tenv -u MAKEFLAGS -u MFLAGS make -C %s rm-%s-dcp BUILD=%s VIVADO=%s MINT_HUB= REUSE_DCPS_FROM= RM_SYNTH_REUSE_%s= %s > %s/logs/%s.make.log 2>&1; rc=$$?; cp -f %s/%s_synth/synth.log %s/logs/%s.log 2>/dev/null || cp -f %s/logs/%s.make.log %s/logs/%s.log; test $$rc -eq 0 && touch %s/$@\n' \
                "$r" "$DFX" "${r//_/-}" "$OUT" "$VIVADO" "$k" "${TOOLS_ENV:+TOOLS_ENV=$TOOLS_ENV}" "$OUT" "$r" "$OUT" "$k" "$OUT" "$r" "$OUT" "$r" "$OUT" "$r" "$OUT"
        fi
    done
} > "$MK"
echo "running $(echo $RMS | wc -w) RMs under $VIVADO, -j$JOBS, nice 19 -> $OUT (plan: $MK)"
( cd "$OUT" && nice -n 19 make -f "$MK" -j"$JOBS" -k all ) || true
report
exit 0
}
