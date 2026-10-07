#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# pin_nanosoc_snapshot.sh — materialise a clean, PINNED snapshot of the
# nanosoc_m0_soc SoC tree for the DFX RM build (fpga/dfx `make rm-nanosoc-dcp`).
#
# WHY THIS EXISTS
# ---------------
# `make rm-nanosoc-dcp` reads nanosoc RTL from $SOCLABS_NANOSOC_SOC_DIR
# (set in tools.env; fpga/dfx/Makefile has no default). That
# upstream *working tree* is, at time of writing, mid-regeneration by the
# QSPI/micropython track and does NOT compile:
#   * build_soc/rtl/nanosoc.sv is UNCOMMITTED (a QSPI-stack regen, adds an
#     ACCELERATOR_SUBSYSTEM param) and has drifted from pynq/filelist.tcl;
#   * the nanosoc_arch_tech submodule is checked out AHEAD of its recorded
#     gitlink (bbca9ce vs d719b66) and the two are not mutually compatible.
#
# The config that actually built the shipped rm_nanosoc is the *committed*
# HEAD of nanosoc_m0_soc + each submodule at its *recorded* gitlink SHA. This
# script reconstructs exactly that, read-only, into a writable build-local
# directory, by cascading `git archive` down the recorded gitlink SHAs (the
# nested tech submodules are byte-identical between d719b66 and bbca9ce, so the
# cascade is fully pinned to what HEAD records).
#
# It never writes inside either upstream nanosoc tree. It only READS them
# (git archive) and writes to $DEST.
#
# USAGE
#   # rebuild the snapshot and print its path:
#   fpga/rp/nanosoc/pin_nanosoc_snapshot.sh
#
#   # rebuild AND export the env var into the current shell (source it):
#   source fpga/rp/nanosoc/pin_nanosoc_snapshot.sh
#
#   # then build the RM against the pinned snapshot (no Makefile edit needed):
#   make -C fpga/dfx rm-nanosoc-dcp SOCLABS_NANOSOC_SOC_DIR="$NANOSOC_M0_SOC_HOME"
#
# ENV OVERRIDES
#   NANOSOC_SRC   upstream tree to snapshot   (default $SOCLABS_NANOSOC_SOC_DIR, env or tools.env)
#   DEST          where to materialise it     (default <repo>/fpga/dfx/build/nanosoc_snapshot)
#   FORCE=1       wipe+rebuild even if DEST already looks populated
# -----------------------------------------------------------------------------
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_REPO_ROOT="$(cd "$_SCRIPT_DIR/../../.." && pwd)"

_tools_env() { sed -n "s/^[[:space:]]*$1[[:space:]]*[:?]\{0,1\}=[[:space:]]*//p" "$_REPO_ROOT/tools.env" 2>/dev/null | tail -n 1; }
NANOSOC_SRC="${NANOSOC_SRC:-${SOCLABS_NANOSOC_SOC_DIR:-$(_tools_env SOCLABS_NANOSOC_SOC_DIR)}}"
[ -n "$NANOSOC_SRC" ] || { echo "pin_nanosoc_snapshot.sh: set NANOSOC_SRC or SOCLABS_NANOSOC_SOC_DIR (tools.env)" >&2; exit 1; }
DEST="${DEST:-$_REPO_ROOT/fpga/dfx/build/nanosoc_snapshot}"

if [ ! -d "$NANOSOC_SRC/.git" ]; then
    echo "pin_nanosoc_snapshot: error: $NANOSOC_SRC is not a git checkout" >&2
    return 1 2>/dev/null || exit 1
fi

# Recursively export a git tree at a pinned SHA, cascading into every recorded
# gitlink (submodule) using the sub-repo populated under the live source tree.
#   $1 = absolute path to a git working dir that CONTAINS object $2
#   $2 = commit SHA to archive
#   $3 = destination directory (created)
_archive_pinned() {
    local gitdir="$1" sha="$2" dest="$3"
    mkdir -p "$dest"
    git -C "$gitdir" archive --format=tar "$sha" | tar -x -C "$dest"
    # Walk gitlinks recorded in this tree; recurse using the populated sub-repo.
    while read -r subsha subpath; do
        [ -n "$subsha" ] || continue
        if [ ! -e "$gitdir/$subpath/.git" ]; then
            echo "pin_nanosoc_snapshot: error: submodule $gitdir/$subpath is not" \
                 "populated (need it to archive $subsha)" >&2
            return 1
        fi
        _archive_pinned "$gitdir/$subpath" "$subsha" "$dest/$subpath"
    done < <(git -C "$gitdir" ls-tree -r "$sha" | awk '$2=="commit"{print $3, $4}')
}

_head="$(git -C "$NANOSOC_SRC" rev-parse HEAD)"
_stamp="$DEST/.pinned_from"

if [ "${FORCE:-0}" != "1" ] && [ -f "$_stamp" ] && \
   [ "$(cat "$_stamp" 2>/dev/null)" = "$_head" ] && \
   [ -f "$DEST/build_soc/rtl/nanosoc.sv" ] && [ -f "$DEST/pynq/filelist.tcl" ]; then
    echo "pin_nanosoc_snapshot: reusing existing snapshot (HEAD $_head) at $DEST"
else
    echo "pin_nanosoc_snapshot: materialising snapshot of $NANOSOC_SRC @ $_head"
    rm -rf "$DEST"
    _archive_pinned "$NANOSOC_SRC" "$_head" "$DEST"
    echo "$_head" > "$_stamp"
    echo "pin_nanosoc_snapshot: done -> $DEST"
fi

# The stage-0 bootrom (imp/fpga/firmware/stage0/{bootrom.sv,
# nanosoc_region_bootrom.v}) is a GENERATED firmware artifact — imp/ is
# .gitignore'd, so `git archive` never captures it, yet pynq/filelist.tcl
# hard-errors without it. It is orthogonal to the pinned RM boundary (it is IMEM
# ROM content, not part of the exp_*/partition-pin interface), so we copy the
# already-built one from the source tree to keep the snapshot self-contained and
# let the Makefile's default FPGA_BOOTROM_DIR (=<soc>/imp/fpga/firmware/stage0)
# resolve. If the source has no built bootrom, say how to build it.
_bootrom_src="$NANOSOC_SRC/imp/fpga/firmware/stage0"
_bootrom_dst="$DEST/imp/fpga/firmware/stage0"
if [ -f "$_bootrom_src/bootrom.sv" ] && [ -f "$_bootrom_src/nanosoc_region_bootrom.v" ]; then
    if [ ! -f "$_bootrom_dst/bootrom.sv" ] || [ "${FORCE:-0}" = "1" ]; then
        mkdir -p "$_bootrom_dst"
        cp -f "$_bootrom_src"/bootrom.sv "$_bootrom_src"/nanosoc_region_bootrom.v "$_bootrom_dst/"
        echo "pin_nanosoc_snapshot: copied generated stage-0 bootrom into snapshot"
    fi
else
    echo "pin_nanosoc_snapshot: WARNING: no built stage-0 bootrom at $_bootrom_src —" \
         "run 'make -C $NANOSOC_SRC/pynq firmware' (the RM synth will error without it)," \
         "or pass FPGA_BOOTROM_DIR=<dir-with-bootrom> to make." >&2
fi

# Sanity: the snapshot MUST carry the COMMITTED nanosoc.sv (no QSPI stack, exp_*
# as an inverted AHB target), not the uncommitted QSPI regen.
if grep -q 'qspi_flash_ahb' "$DEST/build_soc/rtl/nanosoc.sv"; then
    echo "pin_nanosoc_snapshot: WARNING: snapshot nanosoc.sv contains a QSPI stack —" \
         "HEAD may have committed the QSPI regen; verify filelist matches." >&2
fi

export NANOSOC_M0_SOC_HOME="$DEST"
export SOCLABS_NANOSOC_SOC_DIR="$DEST"
echo "pin_nanosoc_snapshot: NANOSOC_M0_SOC_HOME=$NANOSOC_M0_SOC_HOME"
echo "pin_nanosoc_snapshot: SOCLABS_NANOSOC_SOC_DIR=$SOCLABS_NANOSOC_SOC_DIR"
