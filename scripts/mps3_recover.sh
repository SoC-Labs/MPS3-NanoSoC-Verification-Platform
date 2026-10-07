#!/usr/bin/env bash
# mps3_recover.sh — un-dark the MPS3 by JTAG-loading the shell bitstream.
#
# WHY THIS EXISTS
#   The board is repeatedly left DARK — FPGA unconfigured, no network, all ports
#   closed — either because a session ended mid-work or because config-from-SD at
#   the MCC is non-deterministic and a power-cycle did not bring the shell back.
#   Recovery is always the same: JTAG-load the static shell bitstream. This was
#   done ad-hoc ~7 times in one session from a scratchpad .tcl; here it is a
#   tracked, cable-filtered one-liner.
#
#   PROBE BEFORE YOU RUN THIS. "Dark" has two causes that look identical from the
#   network, and only the JTAG config registers separate them:
#     DONE=0 (or CRC_ERROR=1)          -> nothing is configured; this script is
#                                         the right tool.
#     DONE=1, EOS=1, USERCODE=expected -> the fabric IS configured and the baked
#                                         FIRMWARE is hung. Re-loading the same
#                                         .bit changes nothing; re-bake the ELF
#                                         (updatemem). See docs/TROUBLESHOOTING.md
#                                         §2 for the full table.
#   Note `fpgahub target reset --method mcc` sends REBOOT as one burst and the
#   MCC drops burst characters, so it does nothing. A PACED REBOOT on tty_00
#   (one reader only, a bare CR first, one character per 100 ms, and only after
#   any sd_install has finished) DOES reload the FPGA from the SD card with no
#   power-cycle -- proven 2026-09-24, docs/evidence/2026-09-w3/
#   w1_field_remote_20260924.txt. That reload is a fuller recovery than this
#   script's volatile JTAG load, because it is persistent.
#
#   Loading the STATIC shell (greybox RM baked in) is non-destructive to the RP
#   partition contract and re-mints nothing — it is the same bitstream the SD
#   would boot. After this the board pings on 6900 and RMs can be deployed over
#   the wire again. To also run a DUT, deploy an RM afterwards.
#
# SAFETY: the hw_server on <hub-host> is SHARED. This script ALWAYS filters to
#   this board's FT2232 cable serial and REFUSES if no matching target is found —
#   an unfiltered `targets -set` has destroyed a live transfer before. Do not
#   remove the filter. It must match EXACTLY ONE target, with one exception: the
#   hub's hw_server (2025.2) also lists a port HOSTED BY our FPGA,
#   .../xilinx_tcf/Xilinx/jsn-JTAG-HS2-210249B86C47-1390d093-0 (the JTAG context
#   of device 0 on our cable, IDCODE 0x1390D093 = the XCKU115), beside the cable
#   itself, .../xilinx_tcf/Digilent/210249B86C47 (B1, 2026-09-24). That pair is
#   one board: the Digilent target is programmed. Any other multi-match could be
#   two boards and is refused -- the old "first match" could open either. The
#   opened target must carry exactly one xcku115.
#
# Usage:
#   scripts/mps3_recover.sh                       # default shell bit (below)
#   scripts/mps3_recover.sh /path/to/other.bit
#
# You should hold the board lease first (scripts/mps3_board.sh), though JTAG works
# regardless; the lease is courtesy so you don't fight another session.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Vivado: honour $VIVADO; otherwise the first one that ACTUALLY EXISTS here.
# The old hard default was a build-host path (/apps/Xilinx/Vivado/2024.1) that
# does not exist on the hub where this script RUNS -- and the load below used to
# `|| true` the resulting "command not found" into a false "mps3_recover: done",
# so the board silently kept its old image. Resolve for real; fail if there is
# no Vivado rather than pretending a load happened.
resolve_vivado() {
    if [ -n "${VIVADO:-}" ]; then echo "$VIVADO"; return 0; fi
    if command -v vivado >/dev/null 2>&1; then command -v vivado; return 0; fi
    local c
    for c in /tools/Xilinx/*/Vivado/bin/vivado /apps/Xilinx/Vivado/*/bin/vivado; do
        [ -x "$c" ] && { echo "$c"; return 0; }
    done
    return 1
}
VIVADO="$(resolve_vivado || true)"
if [ -z "$VIVADO" ] || [ ! -x "$VIVADO" ]; then
    echo "mps3_recover: no usable Vivado. Set \$VIVADO, or install one on PATH /" >&2
    echo "  under /tools/Xilinx/*/Vivado or /apps/Xilinx/Vivado/*. Board NOT touched." >&2
    exit 3
fi
HW_URL="${MPS3_HW_URL:-localhost:3121}"
CABLE="${MPS3_JTAG_CABLE:-*210249B86C47*}"     # this board's FT2232 serial
# Default = what board 1's SD card boots: the base of the CURRENTLY FIELDED shell,
# static_id 0x44EE76D5 (docs/FIELDED_SHELL.md), the MicroBlaze V / Linux static with
# BOARD 1's stage0 bake in the LMB: sha256 f876e73e412c35b65130a7966819d63cce3ec1f5aa5b6e8f9838f43c7a9660ba,
# stage0 build 0x34956F94 (cold-start fix + identity MPS3-01 / .10.101). The default
# cable filter below is board 1's too. JTAG-loading it is VOLATILE: stage0 then holds
# its 10 s cold settle and boots the user uSD (slot A), or waits in rescue for a push;
# the next power-on boots whatever the config SD holds.
# Board 2 has its own bake (fielded/0x44EE76D5/README.md, per-board table)
# and no cable JTAG: never point this script at board 2 with board 1's bit.
# THE ROLLBACK IMAGE is bare-metal 0x72BB0A36 v0.11, config_rm_greybox_fw_v011.bit
# (md5 2a457f7ebd0b6099f1f7c8fb328c960c, sha256 68f70da13dbe...): pass its path
# explicitly (fielded/0x72BB0A36/, or the hub's staged copy of it).
# Loading another static re-keys the board out from under the deployed overlays,
# which then refuse to load.
# The file is gitignored; fielded/0x44EE76D5/ holds it with its checksum
# (MANIFEST.md5), and fetch_fielded.sh there repopulates it.
BIT="${1:-$REPO/fielded/0x44EE76D5/config_rm_greybox_stage0.bit}"

if [ ! -f "$BIT" ]; then
    echo "mps3_recover: bitstream not found: $BIT" >&2
    echo "  run fielded/0x44EE76D5/fetch_fielded.sh, or pass a path." >&2
    exit 1
fi

# The serial inside the default "*<serial>*" filter: the one the duplicate rule
# below may pair on. Any other filter shape gets no exception (one match only).
case "$CABLE" in
    \**\*) SERIAL="${CABLE#\*}"; SERIAL="${SERIAL%\*}" ;;
    *)      SERIAL="" ;;
esac
case "$SERIAL" in *[!A-Za-z0-9]*) SERIAL="" ;; esac

TCL="$(mktemp /tmp/mps3_recover.XXXXXX.tcl)"
LOG="$(mktemp /tmp/mps3_recover_run.XXXXXX.log)"
trap 'rm -f "$TCL" "$LOG"' EXIT
cat > "$TCL" <<TCL
open_hw_manager
connect_hw_server -url $HW_URL
set mine [get_hw_targets -quiet -filter "NAME =~ $CABLE"]
set tgt ""
if { [llength \$mine] == 1 } {
    set tgt [lindex \$mine 0]
} elseif { [llength \$mine] == 2 && "$SERIAL" ne "" } {
    # the one benign pair (header): Digilent/<serial> + Xilinx/jsn-...-<serial>-<rev>390d093-<n>
    set jsn_re {^jsn-.+-}
    append jsn_re $SERIAL {-[0-9A-Fa-f]390[dD]093-[0-9]+\$}
    set dig {}
    set jsn {}
    foreach t \$mine {
        set drv [file tail [file dirname \$t]]
        if { \$drv eq "Digilent" && [file tail \$t] eq "$SERIAL" } { lappend dig \$t }
        if { \$drv eq "Xilinx" && [regexp \$jsn_re [file tail \$t]] } { lappend jsn \$t }
    }
    if { [llength \$dig] == 1 && [llength \$jsn] == 1 &&
         [file dirname [file dirname [lindex \$dig 0]]] eq [file dirname [file dirname [lindex \$jsn 0]]] } {
        set tgt [lindex \$dig 0]
        puts "INFO: target's twin [lindex \$jsn 0] is a port hosted by this board's FPGA -- not opened"
    }
}
if { \$tgt eq "" } {
    error "not exactly one JTAG target matches $CABLE ([llength \$mine]: \$mine) — refusing to touch the shared hw_server"
}
puts "INFO: target = \$tgt"
open_hw_target \$tgt
set devs [get_hw_devices -quiet *xcku115*]
if { [llength \$devs] != 1 } {
    error "[llength \$devs] xcku115 on \$tgt ([get_hw_devices -quiet]) — not programming"
}
set dev [lindex \$devs 0]
current_hw_device \$dev
if { ![file exists {$BIT}] } { error "missing bitstream: $BIT" }
puts "\n>>> loading [file tail {$BIT}]"
set_property PROGRAM.FILE {$BIT} \$dev
program_hw_devices \$dev
refresh_hw_device -update_hw_probes false \$dev
puts "\nINFO: shell loaded; RM = greybox. Board should ping on 6900 shortly."
close_hw_target
disconnect_hw_server
TCL

echo "mps3_recover: JTAG-loading $(basename "$BIT") via $HW_URL (cable $CABLE) [$VIVADO]"
if ! "$VIVADO" -mode batch -notrace -nojournal -nolog -source "$TCL" >"$LOG" 2>&1; then
    echo "mps3_recover: VIVADO FAILED -- board NOT reconfigured. Full log:" >&2
    cat "$LOG" >&2
    exit 4
fi
grep -E "INFO: target|loading|INFO: shell|ERROR" "$LOG" || true
# vivado can exit 0 having done nothing (e.g. it never reached the program step);
# the success marker is the only proof the fabric was actually written.
if ! grep -q "INFO: shell loaded" "$LOG"; then
    echo "mps3_recover: vivado exited 0 but never reported 'shell loaded' -- board" >&2
    echo "  state UNKNOWN, do not trust it. Full log:" >&2
    cat "$LOG" >&2
    exit 5
fi

echo "mps3_recover: done. Verify from the HUB: pyverify identify --host 192.168.10.101 (Linux: mode run after ~3 min; rescue = push an image) or ping on 6900 (bare metal)."
