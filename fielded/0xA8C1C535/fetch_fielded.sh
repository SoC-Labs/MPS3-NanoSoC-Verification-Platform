#!/usr/bin/env bash
# =============================================================================
# fetch_fielded.sh -- populate this directory with the fielded shell's build
# products, from local scratch if it is here, else from the hub over scp, then
# verify every md5 against MANIFEST.md5.
#
# WHY: until 2026-09-09 exactly ONE copy of static_routed_locked.dcp existed, in
# a gitignored Vivado scratch tree. It is the only input that can add an overlay
# to the shell now on the board. A `make clean`, a full disk, or a tidied
# workstation would have closed that shell permanently.
#
#   bash fielded/0xA8C1C535/fetch_fielded.sh            # local, else hub
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

# Local scratch sources (gitignored). Both trees are needed: six files come from
# the DFX prod dir, two from the shell project.
DFX_PROD="${DFX_PROD:-$REPO_ROOT/fpga/dfx/build_mint/prod}"
SHELL_PROJ="${SHELL_PROJ:-$REPO_ROOT/build/shell_proj_mint}"

# Hub fallback (read-only). The hub is reachable as an ssh alias; keep the real
# hostname out of the repo (set_env.local.sh is gitignored for the same reason).
HUB="${HUB:-${MPS3_HUB:-}}"
# This mint predates the per-static_id MINT_HUB layout: its hub copy sat in
# <hub-home>/mint_A8C1C535/fielded_dcp. Set HUB_DIR to it explicitly.
HUB_DIR="${HUB_DIR:-}"

SOURCE="${SOURCE:-auto}"

# file:origin -- which of the two scratch trees each artefact comes from.
DFX_FILES=(static_routed_locked.dcp config_rm_greybox_routed.dcp static_id.txt
           overlay_inputs.txt config_rm_greybox.mmi config_rm_greybox.bit)
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
    # The hub copy is a PARTIAL set: it holds the five DFX artefacts, not the
    # greybox .bit or the two shell-project files. Fetch what is there and say
    # plainly what is not, rather than reporting a green verify over a short set.
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
        note "   NOT on the hub: ${missing[*]}"
        note "   The hub holds the five artefacts that cannot be regenerated."
        note "   The rest come from local scratch, or from a rebuild."
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
