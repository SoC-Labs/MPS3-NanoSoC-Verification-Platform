#!/usr/bin/env bash
# tests/shell_cpu_seam/run_seam_gate.sh -- the VIVADO half of the CPU-seam gate.
#
# The pytest beside this file is the board-free, tool-light half and runs in
# `make check`. This half needs Vivado 2024.1 (and 2026.1 for the MBV step), so
# it is run by hand / by FLOW before a mint, never by CI. Each step is a
# validate-only BD build (fpga/shell/tools/shell_bd_dump_run.tcl, <=2 threads,
# nice 19): ~6 min per 2024.1 step, ~15 min for the MBV step (2026.1 IP
# packaging + the DDR4 MIG).
#
#   1. BASELINE   the pre-seam shell_bd.tcl + touch_iic_add.tcl, taken from git
#                 at $BASE_REV, dumped with the mint's flags (SHELL_TOUCH=1). Its
#                 hash must still be golden/baseline_mb.txt's
#                 mb_touch1_body_sha256 (the tool and the dump are unchanged).
#   2. IDENTITY   this tree, SHELL_CPU unset (= mb): against the baseline, its
#                 dump must differ by EXACTLY golden/d13_usd_delta_mb.txt (D13's
#                 usd_spi_0 replacing axi_quad_spi_0 on 0x44A4 / M03) PLUS
#                 golden/inj_dutegr_delta_mb.txt (INJ's DUTEGR inject path onto
#                 the bridge's port-B ingress), and nothing else (every other cell,
#                 CONFIG value, net, net name and address identical) -- and hash
#                 to mb_touch1_inj_body_sha256.
#   3. CONTROL    a scratch copy of fpga/shell/bd/ with ONE CONFIG flipped in
#                 cpu_mb.tcl (microblaze_0 C_USE_DIV 1 -> 0): its dump must
#                 DIFFER from this tree's, and the delta check of step 2 must
#                 FAIL on it. A gate nobody has watched fail is not a gate.
#   4. MBV        SHELL_CPU=mbv under 2026.1: validate + tools/shell_bd_guards.tcl
#                 (the Linux contract read back after propagation) + its clamp
#                 wiring must equal golden/clamp_wiring_mbv.txt, which the
#                 pytest's clamp bench is fed from; and the generated MBV regmap
#                 view (fpga/shell/generated/regmap_mbv.json) must equal the
#                 address map Vivado built, both ways.
#
# BASE_REV is eafe787 because that is the shell_bd.tcl the 2026-10 ILA mint (the
# fielded fallback for the whole Linux programme) was built from. It stays there
# through D13: the bare-metal BD's one deliberate change since -- usd_spi_0 at
# 0x44A4 -- is PINNED as golden/d13_usd_delta_mb.txt (regenerate with
# `seam_dump.py delta <baseline> <seam_mb>` and review every line; the pytest
# holds each line to the D13 cell swap). A further deliberate change: extend the
# delta the same way, or move BASE_REV to the commit that carries it and empty
# the delta -- and say which in the commit. INJ (2026-09-24) did the former:
# golden/inj_dutegr_delta_mb.txt, 23 lines, checked together with D13's.
#
# Usage: tests/shell_cpu_seam/run_seam_gate.sh <scratch_dir> [all|mb|mbv]
set -euo pipefail

SCRATCH=${1:?usage: run_seam_gate.sh <scratch_dir> [all|mb|mbv]}
WHAT=${2:-all}
BASE_REV=${BASE_REV:-eafe787}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
HERE=$REPO/tests/shell_cpu_seam
V2024=${VIVADO_2024:-/apps/Xilinx/Vivado/2024.1/bin/vivado}
V2026=${VIVADO_2026:-/opt/Xilinx/Vivado/2026.1/bin/vivado}
DUMP_TCL=$REPO/fpga/shell/tools/shell_bd_dump_run.tcl
mkdir -p "$SCRATCH"
SCRATCH=$(cd "$SCRATCH" && pwd)

dump() {  # dump <vivado> <out_dir> <shell_bd.tcl> <SHELL_CPU> [<dump driver> <ip repo dir>]
    local viv=$1 out=$2 bd=$3 cpu=$4 drv=${5:-$DUMP_TCL} ipdir=${6:-$SCRATCH}
    rm -rf "$out"; mkdir -p "$out"
    echo "== dump: cpu=$cpu bd=$bd drv=$drv -> $out"
    env SHELL_CPU="$cpu" SHELL_TOUCH=1 SHELL_REALPHY=0 SOCLABS_IP_REPO_DIR="$ipdir" \
        nice -n 19 "$viv" -mode batch -nojournal -log "$out/vivado.log" \
        -source "$drv" -tclargs "$out" "$bd" > "$out/stdout.log" 2>&1 || true
    if [ ! -f "$out/dump_run.ok" ]; then
        echo "FAIL: dump did not complete -- see $out/stdout.log"; tail -20 "$out/stdout.log"; exit 1
    fi
}

if [ "$WHAT" = all ] || [ "$WHAT" = mb ]; then
    # 1. baseline from git -- the BD scripts AND the IP they instantiate. The
    #    packaged soclabs IP is built from RTL, and a dump records every pin of
    #    every cell, so a baseline BD dumped against TODAY's RTL is not the
    #    baseline: INJ (2026-09-24) gave dut_egress four new ports, and the
    #    eafe787 BD then dumps them dangling. So the baseline tree is BASE_REV's
    #    fpga/shell/{bd,ip}, its packaging recipe and fpga/ethernet, driven by
    #    today's dump tooling, packaged into its own IP repo.
    BT=$SCRATCH/baseline_tree
    rm -rf "$BT"; mkdir -p "$BT/fpga/shell"
    git -C "$REPO" archive "$BASE_REV" fpga/shell/bd fpga/shell/ip \
        fpga/shell/ip_packaged/package_csr_ip.tcl fpga/ethernet | tar -x -C "$BT"
    cp -r "$REPO/fpga/shell/tools" "$BT/fpga/shell/tools"
    B=$BT/fpga/shell/bd
    dump "$V2024" "$SCRATCH/baseline" "$B/shell_bd.tcl" "" \
        "$BT/fpga/shell/tools/shell_bd_dump_run.tcl" "$SCRATCH/baseline_ip"
    got=$(grep -v '^#' "$SCRATCH/baseline/shell_bd.dump" | sha256sum | awk '{print $1}')
    want=$(awk '/^mb_touch1_body_sha256/{print $2}' "$HERE/golden/baseline_mb.txt")
    [ "$got" = "$want" ] || { echo "FAIL: baseline ($BASE_REV) dump sha256 $got != golden $want -- the tool or the dump drifted"; exit 1; }

    # 2. identity, modulo the pinned D13 delta
    dump "$V2024" "$SCRATCH/seam_mb" "$REPO/fpga/shell/bd/shell_bd.tcl" mb
    python3 "$HERE/seam_dump.py" compare "$SCRATCH/baseline/shell_bd.dump" "$SCRATCH/seam_mb/shell_bd.dump" \
        --expect-delta "$HERE/golden/d13_usd_delta_mb.txt" --expect-delta "$HERE/golden/inj_dutegr_delta_mb.txt"
    got=$(grep -v '^#' "$SCRATCH/seam_mb/shell_bd.dump" | sha256sum | awk '{print $1}')
    want=$(awk '/^mb_touch1_inj_body_sha256/{print $2}' "$HERE/golden/baseline_mb.txt")
    [ "$got" = "$want" ] || { echo "FAIL: seam_mb dump sha256 $got != golden $want"; exit 1; }
    python3 "$HERE/seam_dump.py" clamp "$SCRATCH/seam_mb/shell_bd.dump" | diff - "$HERE/golden/clamp_wiring_mb.txt"

    # 3. negative control
    N=$SCRATCH/negctl_src/bd
    rm -rf "$N"; mkdir -p "$N"; cp "$REPO"/fpga/shell/bd/*.tcl "$N/"
    sed -i 's/CONFIG.C_USE_DIV {1}/CONFIG.C_USE_DIV {0}/' "$N/cpu_mb.tcl"
    grep -q 'CONFIG.C_USE_DIV {0}' "$N/cpu_mb.tcl" || { echo "FAIL: control mutation did not apply"; exit 1; }
    dump "$V2024" "$SCRATCH/negctl" "$N/shell_bd.tcl" mb
    python3 "$HERE/seam_dump.py" compare "$SCRATCH/seam_mb/shell_bd.dump" "$SCRATCH/negctl/shell_bd.dump" --expect differ
    if python3 "$HERE/seam_dump.py" compare "$SCRATCH/baseline/shell_bd.dump" "$SCRATCH/negctl/shell_bd.dump" \
           --expect-delta "$HERE/golden/d13_usd_delta_mb.txt" --expect-delta "$HERE/golden/inj_dutegr_delta_mb.txt"; then
        echo "FAIL: the pinned-delta check PASSED a dump with an extra change -- it cannot see one"; exit 1
    fi
    echo "OK: the pinned-delta check fails on the control, as it must"
fi

if [ "$WHAT" = all ] || [ "$WHAT" = mbv ]; then
    # 4. the MicroBlaze V variant
    dump "$V2026" "$SCRATCH/seam_mbv" "$REPO/fpga/shell/bd/shell_bd.tcl" mbv
    grep -q "SHELL_BD_GUARDS_OK cpu=mbv" "$SCRATCH/seam_mbv/stdout.log" || { echo "FAIL: MBV guards did not pass"; exit 1; }
    python3 "$HERE/seam_dump.py" clamp "$SCRATCH/seam_mbv/shell_bd.dump" | diff - "$HERE/golden/clamp_wiring_mbv.txt"
    python3 "$HERE/seam_dump.py" regmap "$SCRATCH/seam_mbv/shell_bd.dump" "$REPO/fpga/shell/generated/regmap_mbv.json"
fi
echo "SHELL_CPU_SEAM_GATE PASS ($WHAT)"
