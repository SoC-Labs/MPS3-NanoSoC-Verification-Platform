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
DFX_PROD="${DFX_PROD:-$REPO_ROOT/fpga/dfx/build_mint3_rc2/prod}"
SHELL_PROJ="${SHELL_PROJ:-$REPO_ROOT/fpga/dfx/build_mint3_rc2/shell_proj}"

# Hub fallback (read-only), reachable as an ssh alias; keep the real hostname
# out of the repo. HUB_DIR = the mint's stage-8 MINT_HUB destination -- VERIFY it
# by listing the hub before trusting this default (fielded/0x3F1A560F/README.md
# "Every place a copy lives": the layout has changed between mints before).
HUB="${HUB:-${MPS3_HUB:-}}"
HUB_DIR="${HUB_DIR:-${MPS3_MINT_ARCHIVE:+$MPS3_MINT_ARCHIVE/$STATIC_ID}}"

# THE FIELDED SET (MANIFEST.md5 part 2): the per-board config-SD bits, their stage0s and the
# release image. None of it is in the mint's build dir. Its hub home is HUB_FIELD_DIR, which
# the cutover's C3 step creates (VERIFY it by listing it; until then the files sit in
# <hub-home>/pv_field/ under other names). FIELD_DIR is an optional local copy.
HUB_FIELD_DIR="${HUB_FIELD_DIR:-$HUB_DIR/fielded}"
FIELD_DIR="${FIELD_DIR:-}"
FIELD_FILES=(config_rm_greybox_stage0.bit stage0_field_b1.elf
             config_rm_greybox_stage0_b2.bit stage0_b2.elf
             config_rm_greybox_stage0_field_b2.bit stage0_field_b2.elf
             linux_slot.img linux_slot_v7n.img linux_legal_info.tar)
is_field() { local f; for f in "${FIELD_FILES[@]}"; do [ "$f" = "$1" ] && return 0; done; return 1; }

# Names this record gives a MINT file that the build dir and the hub archive store under
# another name: the cutover renamed the mint's own bake, because config_rm_greybox_stage0.bit
# and stage0_bake.json in this record are now what BOARD 1 boots (linux_bundle.json names them).
src_name() {
    case "$1" in
        config_rm_greybox_stage0_mint.bit) printf 'config_rm_greybox_stage0.bit' ;;
        stage0_bake_mint.json)             printf 'stage0_bake.json' ;;
        *)                                 printf '%s' "$1" ;;
    esac
}

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

# One MANIFEST.md5 entry is not in prod/: static_canon.json, which a Linux
# (SHELL_CPU=mbv) mint writes at the BUILD root (build_mint3_rc2/static_canon.json).
# It is tracked in this directory as well, so it is never lost; the lookup below
# only lets the local fetch complete instead of falling back to the hub, which
# does not hold it (nor config_rm_greybox.bit: stage 8 copies the flashable
# stage0 bit, not the unbaked base). A local candidate counts only if its md5 is
# the one MANIFEST.md5 records (the rule of fielded/0x72BB0A36, 12f8103).
want_md5() { awk -v n="$1" '$1 !~ /^#/ && $2 == n { print $1; exit }' "$HERE/MANIFEST.md5"; }

# A file already here with the md5 MANIFEST.md5 records is never fetched again. That
# keeps the TRACKED records (stage0_bake.json, linux_bundle.json, ...) from being
# overwritten by a same-named file of another content at the source.
already_ok() { [ -f "$HERE/$1" ] && [ "$(md5sum "$HERE/$1" | cut -d' ' -f1)" = "$(want_md5 "$1")" ]; }

dfx_src() {
    local d want s
    want="$(want_md5 "$1")"
    if is_field "$1"; then
        [ -n "$FIELD_DIR" ] || return 1
        set -- "$1" "$FIELD_DIR"
    else
        set -- "$1" "$DFX_PROD" "$DFX_PROD/.."
    fi
    s="$(src_name "$1")"
    local name="$1"; shift
    for d in "$@"; do
        [ -f "$d/$s" ] || continue
        [ "$(md5sum "$d/$s" | cut -d' ' -f1)" = "$want" ] && { printf '%s' "$d/$s"; return 0; }
    done
    return 1
}

have_local() {
    local f
    for f in "${DFX_FILES[@]}";  do already_ok "$f" || dfx_src "$f" >/dev/null || return 1; done
    for f in "${PROJ_FILES[@]}"; do already_ok "$f" || [ -f "$SHELL_PROJ/$f" ] || return 1; done
    return 0
}

fetch_local() {
    note "== source: LOCAL scratch =="
    note "   $DFX_PROD"
    note "   $SHELL_PROJ"
    [ -n "$FIELD_DIR" ] && note "   $FIELD_DIR (the fielded set)"
    local f
    for f in "${DFX_FILES[@]}";  do already_ok "$f" || cp -p "$(dfx_src "$f")" "$HERE/$f"; done
    for f in "${PROJ_FILES[@]}"; do already_ok "$f" || cp -p "$SHELL_PROJ/$f" "$HERE/$f"; done
}

fetch_hub() {
    [ -n "$HUB" ] && [ -n "$HUB_DIR" ] || die "hub fallback needs HUB (or MPS3_HUB) and HUB_DIR (or MPS3_MINT_ARCHIVE) -- see the top of this file"
    note "== source: HUB $HUB:$HUB_DIR, fielded set $HUB_FIELD_DIR (read-only) =="
    # Fetch what is there and say plainly what is not, rather than reporting a
    # green verify over a short set.
    local f src missing=()
    for f in "${DFX_FILES[@]}" "${PROJ_FILES[@]}"; do
        if already_ok "$f"; then continue; fi
        if is_field "$f"; then src="$HUB_FIELD_DIR/$f"; else src="$HUB_DIR/$(src_name "$f")"; fi
        if scp -q "$HUB:$src" "$HERE/$f" 2>/dev/null; then
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
