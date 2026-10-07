#!/usr/bin/env bash
# =============================================================================
# dfx_scratch_report.sh -- inventory every gitignored Vivado scratch tree, and
# say which ones are load-bearing.
#
# THE PROBLEM IT SOLVES. There are ~31 build trees under fpga/dfx/build*/ and
# build/shell_proj*/, several GB, all gitignored, all with names that look
# equally disposable. Three of them are not disposable at all:
#
#   * build_mint + build/shell_proj_mint hold the ONLY copy of the fielded
#     shell's locked static -- the sole input to every future `add-rm-*`.
#   * build_qspi_kvm_jtag holds the ONLY record (rm_*_synth/synth.log) of which
#     source trees produced the RM checkpoints that were REUSED in the mint.
#   * build_linux is on a deliberately separate static that overlay_linux is
#     keyed to (docs/PLATFORM_ONE_IMPL.md).
#
# Nothing on disk distinguished those from the other 27. This report does.
#
# IT NEVER DELETES ANYTHING, and takes no argument that could. Deletion is a
# decision reserved for the repo owner; this only makes it a safe one to take.
# DEAD here means "no evidence found that anything depends on it" -- it is a
# finding, not an instruction.
#
#   bash scripts/dfx_scratch_report.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
AUTHORITY="$REPO_ROOT/docs/FIELDED_SHELL.md"

# --- the authority: what is fielded, and what the overlays are keyed to -------
auth_row() { sed -n "s/^|[[:space:]]*\`$1\`[[:space:]]*|[[:space:]]*\`\(0x[0-9A-Fa-f]\{8\}\)\`.*/\1/p" "$AUTHORITY" 2>/dev/null | head -1; }
FIELDED="$(auth_row fielded)"
MINTED="$(auth_row minted)"
: "${FIELDED:=<unparsed>}" "${MINTED:=<unparsed>}"

# The static overlay_linux is deliberately parked on (docs/PLATFORM_ONE_IMPL.md).
LINUX_STATIC="$(grep -ohE '0x2B082E1B' "$REPO_ROOT"/fpga/dfx/overlay_linux/*/manifest.json 2>/dev/null | head -1)"
: "${LINUX_STATIC:=0x2B082E1B}"

# A shell project is LIVE if its synthesised static is byte-for-byte the one
# preserved for the fielded shell. That is a PROOF, not a name match -- and it
# is why fielded/<id>/MANIFEST.md5 is worth keeping in git.
FIELDED_SYNTH_MD5="$(awk '$2=="shell_static_synth.dcp"{print $1}' \
    "$REPO_ROOT/fielded/$FIELDED/MANIFEST.md5" 2>/dev/null | head -1)"

human() {  # KiB -> human
    awk -v k="$1" 'BEGIN{
        split("KiB MiB GiB TiB", u, " ");
        i=1; while (k>=1024 && i<4) { k/=1024; i++ }
        printf (k<10 ? "%.1f%s" : "%.0f%s"), k, u[i]
    }'
}

printf '== DFX scratch inventory -- %s ==\n' "$(date '+%F %T')"
printf '   repo      : %s\n' "$REPO_ROOT"
printf '   authority : docs/FIELDED_SHELL.md  fielded=%s  minted=%s\n' "$FIELDED" "$MINTED"
if [ -n "$FIELDED_SYNTH_MD5" ]; then
    printf '   witness   : fielded/%s/MANIFEST.md5 shell_static_synth.dcp=%s\n' "$FIELDED" "${FIELDED_SYNTH_MD5:0:12}…"
else
    printf '   witness   : (no fielded/%s/MANIFEST.md5 -- shell projects classified by NAME only)\n' "$FIELDED"
fi
printf '\n%-34s %8s %-12s %-16s %s\n' TREE SIZE STATIC_ID MTIME CLASS
printf '%-34s %8s %-12s %-16s %s\n' "----" "----" "---------" "-----" "-----"

DEAD_KB=0; DEAD_N=0; TOTAL_KB=0; TOTAL_N=0
declare -a NOTES

classify() {   # $1=rel path  $2=static_id or '-'  -> echoes "CLASS|note"
    local rel="$1" sid="$2" base; base="$(basename "$rel")"

    case "$base" in
        build_qspi_kvm_jtag)
            echo "PROVENANCE|the ONLY record of which sources produced the RM synth checkpoints REUSED by the mint (rm_*_synth/synth.log); its four RM dcps are byte-identical to build_mint's" ; return ;;
        build_linux)
            echo "PARKED|deliberately on a separate static ($LINUX_STATIC); fpga/dfx/overlay_linux/ is keyed to it (docs/PLATFORM_ONE_IMPL.md)" ; return ;;
        build_clcd)
            # Its static is three mints stale, so by the id rule it is DEAD --
            # but until 2026-09-09 it also held the five mint helpers the mint
            # script sources and cites, which no static_id could have revealed.
            # State the dependency and where it moved, so this stays DEAD for a
            # checkable reason rather than by omission.
            if [ -f "$REPO_ROOT/fpga/dfx/tools/write_mem_info.tcl" ] \
            && [ -f "$REPO_ROOT/fpga/dfx/tools/finish_rekey.sh" ]; then
                echo "DEAD|static_id $sid is stale AND its five mint helpers are now tracked in fpga/dfx/tools/ -- verify those copies before removing this tree"
            else
                echo "PROVENANCE|holds the five mint helpers the flow sources and cites (write_mem_info.tcl, finish_rekey.sh, rekey_recover.sh, finish_partials.tcl, bank_gate.tcl) and fpga/dfx/tools/ does NOT yet have them"
            fi
            return ;;
    esac

    # Shell projects: prove it by the synthesised static's checksum if we can.
    case "$base" in
        shell_proj*)
            local dcp="$REPO_ROOT/$rel/shell_static_synth.dcp" md5=""
            if [ -n "$FIELDED_SYNTH_MD5" ] && [ -f "$dcp" ]; then
                md5="$(md5sum "$dcp" | cut -d' ' -f1)"
                if [ "$md5" = "$FIELDED_SYNTH_MD5" ]; then
                    echo "LIVE|shell_static_synth.dcp is byte-identical to the one preserved for fielded $FIELDED (md5 proof, not a name match)"
                    return
                fi
                echo "DEAD|shell_static_synth.dcp md5 ${md5:0:12}… != the fielded shell's"
                return
            fi
            # No witness available: fall back to pairing by name.
            local paired="${base#shell_proj}"; paired="fpga/dfx/build${paired:-}"
            if [ "$base" = "shell_proj_mint" ]; then
                echo "LIVE|the shell project that produced the fielded static (no md5 witness available -- NAME match only)"
            else
                echo "DEAD|no md5 witness; name pairs with ${paired}"
            fi
            return ;;
    esac

    if [ "$sid" != "-" ]; then
        if [ "${sid^^}" = "${FIELDED^^}" ] && [ "${sid^^}" = "${MINTED^^}" ]; then
            echo "LIVE|static_id == fielded AND minted ($FIELDED): holds prod/static_routed_locked.dcp, the sole input to every future add-rm-*"; return
        elif [ "${sid^^}" = "${FIELDED^^}" ]; then
            echo "LIVE|static_id == the FIELDED shell ($FIELDED)"; return
        elif [ "${sid^^}" = "${MINTED^^}" ]; then
            echo "LIVE|static_id == what the overlays are MINTED to ($MINTED)"; return
        fi
        echo "DEAD|static_id $sid is neither fielded ($FIELDED) nor minted ($MINTED)"; return
    fi
    echo "DEAD|never reached a locked static (no prod/static_id.txt)"
}

scan() {   # $1 = glob dir prefix
    local d rel kb sid mt cls note
    for d in $1; do
        [ -d "$d" ] || continue
        rel="${d#"$REPO_ROOT"/}"; rel="${rel%/}"
        kb="$(du -sk "$d" 2>/dev/null | cut -f1)"; kb="${kb:-0}"
        sid='-'
        [ -f "$d/prod/static_id.txt" ] && sid="$(tr -d '[:space:]' < "$d/prod/static_id.txt")"
        [ -n "$sid" ] || sid='-'
        mt="$(stat -c %y "$d" 2>/dev/null | cut -d. -f1)"
        IFS='|' read -r cls note <<< "$(classify "$rel" "$sid")"
        printf '%-34s %8s %-12s %-16s %s\n' "$rel" "$(human "$kb")" "$sid" "${mt%% *}" "$cls"
        NOTES+=("$cls|$rel|$note")
        TOTAL_KB=$((TOTAL_KB + kb)); TOTAL_N=$((TOTAL_N + 1))
        if [ "$cls" = DEAD ]; then DEAD_KB=$((DEAD_KB + kb)); DEAD_N=$((DEAD_N + 1)); fi
    done
}

scan "$REPO_ROOT/fpga/dfx/build*/"
printf '\n'
scan "$REPO_ROOT/build/shell_proj*/"

printf '\n-- why each non-DEAD tree is kept --\n'
for n in "${NOTES[@]}"; do
    IFS='|' read -r cls rel note <<< "$n"
    [ "$cls" = DEAD ] && continue
    printf '  %-11s %-32s %s\n' "$cls" "$rel" "$note"
done

printf '\n-- DEAD (no dependency found) --\n'
for n in "${NOTES[@]}"; do
    IFS='|' read -r cls rel note <<< "$n"
    [ "$cls" = DEAD ] || continue
    printf '  %-32s %s\n' "$rel" "$note"
done

printf '\n== totals ==\n'
printf '   %2d trees scanned          %s\n' "$TOTAL_N" "$(human "$TOTAL_KB")"
printf '   %2d classified DEAD        %s   <-- reclaimable, IF the owner decides so\n' "$DEAD_N" "$(human "$DEAD_KB")"
printf '   %2d LIVE / PROVENANCE / PARKED  %s\n' "$((TOTAL_N - DEAD_N))" "$(human "$((TOTAL_KB - DEAD_KB))")"
printf '\n'
printf 'This report DELETES NOTHING and has no flag that would. DEAD means no\n'
printf 'dependency was FOUND, which is not the same as none existing -- a tree can\n'
printf 'hold the only copy of a log, a report, or a checkpoint that no rule here\n'
printf 'knows to look for (that is exactly how build_qspi_kvm_jtag nearly read as\n'
printf 'disposable). Before removing any of them, copy out prod/ and *_synth/*.log,\n'
printf 'and re-run this after `bash fielded/%s/verify_fielded.sh` passes.\n' "$FIELDED"
