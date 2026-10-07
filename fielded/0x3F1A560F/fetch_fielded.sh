#!/usr/bin/env bash
# =============================================================================
# fetch_fielded.sh -- populate this directory with the fielded shell's build
# products, from local scratch if it is here, else from the hub over scp, then
# verify every md5 against MANIFEST.md5.
#
# WHY: until 2026-09-09 exactly ONE copy of the previous shell's
# static_routed_locked.dcp existed, in a gitignored Vivado scratch tree. It is
# the only input that can add an overlay to the shell now on the board. A `make
# clean`, a full disk, or a tidied workstation would have closed that shell
# permanently. This directory is the arrangement that stops it happening again.
#
#   bash fielded/0x3F1A560F/fetch_fielded.sh            # local, else hub
#   SOURCE=hub   bash .../fetch_fielded.sh              # force the hub
#   SOURCE=local bash .../fetch_fielded.sh              # force local scratch
#
# Read-only at both ends: it copies FROM the scratch tree and FROM the hub, and
# writes only inside this directory. It never writes to the hub and never
# touches a board.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
STATIC_ID="$(basename "$HERE")"

# Local scratch sources (gitignored). This mint kept the SHELL PROJECT INSIDE
# the build tree (SHELL_PROJ=fpga/dfx/build_mint_2026_09/shell_proj), which
# 0xA8C1C535 did not -- that one used build/shell_proj_mint/. Both are still
# two trees, so both variables stay.
DFX_PROD="${DFX_PROD:-$REPO_ROOT/fpga/dfx/build_mint_2026_09/prod}"
SHELL_PROJ="${SHELL_PROJ:-$REPO_ROOT/fpga/dfx/build_mint_2026_09/shell_proj}"

# Hub fallback (read-only). The hub is reachable as an ssh alias; keep the real
# hostname out of the repo (set_env.local.sh is gitignored for the same reason).
#
# HUB_DIR IS VERIFIED (2026-09-22, by listing the hub). <MINT_HUB> holds
# this shell's archive: static_id.txt reads 0x3F1A560F and
# static_routed_locked.dcp is there at 10,027,747 bytes -- the only input from
# which an overlay can ever be added to this shell once it is fielded, which is
# the whole reason this directory and that copy exist.
#
# Note the layout CHANGED with this mint and the old shape still exists beside
# it: 0xA8C1C535 used <hub-home>/mint_A8C1C535/fielded_dcp, one directory per
# mint, while the flow's stage 8 now rsyncs to a single MINT_HUB (tools.env,
# = <MINT_HUB>). So the previous shell's archive is NOT where this one
# is, and a script that assumes either layout for both will find nothing. The
# mint staging dir <hub-home>/mint_3F1A560F is a third thing again: the
# overlays and pusher staged for a board window, not the archive.
#
# MOVED 2026-09-23: the next mint's stage 8 (0x72BB0A36) rsynced FLAT into
# <MINT_HUB> and overwrote every file of the same name, including this
# shell's static_routed_locked.dcp. The full set was restored the same evening
# from the local build (every file checked against MANIFEST.md5 first) into
# <MINT_HUB>/0x3F1A560F/, and stage 8 now writes one subdirectory per
# static_id, so the default above points there. The flat <MINT_HUB>
# files are 0x72BB0A36's, not this shell's.
HUB="${HUB:-${MPS3_HUB:-}}"
HUB_DIR="${HUB_DIR:-${MPS3_MINT_ARCHIVE:+$MPS3_MINT_ARCHIVE/$STATIC_ID}}"

SOURCE="${SOURCE:-auto}"

# file:origin -- which of the two scratch trees each artefact comes from.
#
# config_rm_greybox_fw.bit is in this list and was NOT in 0xA8C1C535's: it is
# the exact image on the config SD, its firmware was baked from a dirty tree,
# and it is therefore not reproducible from any commit (README, "The artefacts").
DFX_FILES=(static_routed_locked.dcp config_rm_greybox_routed.dcp static_id.txt
           overlay_inputs.txt config_rm_greybox.mmi config_rm_greybox.bit
           config_rm_greybox_fw.bit static_stamp.json)
PROJ_FILES=(shell_static_synth.dcp shell_harness.xsa)

die()  { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

have_local() {
    local f
    for f in "${DFX_FILES[@]}";  do [ -f "$DFX_PROD/$f" ]   || return 1; done
    for f in "${PROJ_FILES[@]}"; do [ -f "$SHELL_PROJ/$f" ] || return 1; done
    return 0
}

fetch_local() {
    note "== source: LOCAL scratch =="
    note "   $DFX_PROD"
    note "   $SHELL_PROJ"
    local f
    for f in "${DFX_FILES[@]}";  do cp -p "$DFX_PROD/$f"   "$HERE/$f"; done
    for f in "${PROJ_FILES[@]}"; do cp -p "$SHELL_PROJ/$f" "$HERE/$f"; done
}

fetch_hub() {
    [ -n "$HUB" ] && [ -n "$HUB_DIR" ] || die "hub fallback needs HUB (or MPS3_HUB) and HUB_DIR (or MPS3_MINT_ARCHIVE) -- see the top of this file"
    note "== source: HUB $HUB:$HUB_DIR (read-only) =="
    # Fetch what is there and say plainly what is not, rather than reporting a
    # green verify over a short set.
    local f missing=()
    for f in "${DFX_FILES[@]}" "${PROJ_FILES[@]}"; do
        if scp -q "$HUB:$HUB_DIR/$f" "$HERE/$f" 2>/dev/null; then
            note "   got $f"
        else
            missing+=("$f")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        note ""
        note "   NOT at $HUB:$HUB_DIR: ${missing[*]}"
        note "   Either the hub copy is partial, or HUB_DIR is wrong -- see the"
        note "   note on HUB_DIR at the top of this file before concluding the"
        note "   archive is lost."
    fi
}

case "$SOURCE" in
    local) have_local || die "SOURCE=local but the scratch trees are incomplete ($DFX_PROD, $SHELL_PROJ)"; fetch_local ;;
    hub)   fetch_hub ;;
    auto)
        if have_local; then fetch_local
        else
            note "== local scratch incomplete -- falling back to the hub =="
            fetch_hub
        fi ;;
    *) die "SOURCE must be auto|local|hub (got '$SOURCE')" ;;
esac

note ""
note "== verifying against MANIFEST.md5 =="
exec bash "$HERE/verify_fielded.sh"
