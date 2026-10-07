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
#   bash fielded/<static_id>/fetch_fielded.sh            # local, else hub
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

# TEMPLATE (fielded/_template/, 2026-09-23). Instantiate per mint with the
# recipe in fielded/README.md, which fills the four __PLACEHOLDERS__ below.
# Unlike the per-mint copies before it, the FILE LIST IS NOT WRITTEN HERE: it is
# every name in this directory's MANIFEST.md5, which `make -C fpga/dfx
# fielded-files` generated -- so an RM-internal ILA's .ltx, its .ltx.json
# sidecar and the partial pair it describes are fetched whenever the mint made
# them, and a new artefact kind cannot be preserved by one script and forgotten
# by the other.
DFX_PROD="${DFX_PROD:-$REPO_ROOT/fpga/dfx/build_mint_2026_10/prod}"
SHELL_PROJ="${SHELL_PROJ:-$REPO_ROOT/fpga/dfx/build_mint_2026_10/shell_proj}"

# Hub fallback (read-only), reachable as an ssh alias; keep the real hostname
# out of the repo. HUB_DIR = the mint's stage-8 MINT_HUB destination -- VERIFY it
# by listing the hub before trusting this default (fielded/0x3F1A560F/README.md
# "Every place a copy lives": the layout has changed between mints before).
HUB="${HUB:-${MPS3_HUB:-}}"
HUB_DIR="${HUB_DIR:-${MPS3_MINT_ARCHIVE:+$MPS3_MINT_ARCHIVE/$STATIC_ID}}"

SOURCE="${SOURCE:-auto}"

# The two shell-project artefacts live in SHELL_PROJ; everything else in DFX_PROD.
PROJ_FILES=(shell_static_synth.dcp shell_harness.xsa)
DFX_FILES=()
[ -f "$HERE/MANIFEST.md5" ] || { printf 'FATAL: no MANIFEST.md5 in %s\n' "$HERE" >&2; exit 1; }
while read -r _sum _name; do
    case "$_sum" in ''|\#*) continue ;; esac
    case " ${PROJ_FILES[*]} " in *" $_name "*) continue ;; esac
    DFX_FILES+=("$_name")
done < "$HERE/MANIFEST.md5"

die()  { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

# Three MANIFEST.md5 entries are not in prod/: the v0.11 re-bake
# (config_rm_greybox_fw_v011.bit, shell_fw_v011.elf) that updatemem wrote to
# prod/v011/, and the mint's own shell_fw.elf, which stage 6 leaves in the
# firmware workspace (fpga/dfx/Makefile FW_WS). A flat prod/ lookup never found
# them, so the local fetch always fell back to the hub (ILA handoff defect 8).
# A local candidate counts only if its md5 is the one MANIFEST.md5 records: a
# firmware workspace is rebuilt in place, and a stale ELF there must send auto
# mode to the hub (flat and complete), not into a failing verify.
FW_DIR="${FW_DIR:-$REPO_ROOT/build/vitis_fw/shell_fw}"
dfx_src() {
    local d want
    want="$(awk -v n="$1" '$1 !~ /^#/ && $2 == n { print $1; exit }' "$HERE/MANIFEST.md5")"
    for d in "$DFX_PROD" "$DFX_PROD/v011" "$FW_DIR"; do
        [ -f "$d/$1" ] || continue
        [ "$(md5sum "$d/$1" | cut -d' ' -f1)" = "$want" ] && { printf '%s' "$d/$1"; return 0; }
    done
    return 1
}

have_local() {
    local f
    for f in "${DFX_FILES[@]}";  do dfx_src "$f" >/dev/null || return 1; done
    for f in "${PROJ_FILES[@]}"; do [ -f "$SHELL_PROJ/$f" ] || return 1; done
    return 0
}

fetch_local() {
    note "== source: LOCAL scratch =="
    note "   $DFX_PROD"
    note "   $SHELL_PROJ"
    local f
    for f in "${DFX_FILES[@]}";  do cp -p "$(dfx_src "$f")" "$HERE/$f"; done
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
