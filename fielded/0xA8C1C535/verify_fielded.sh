#!/usr/bin/env bash
# =============================================================================
# verify_fielded.sh -- two checks, both of which must pass:
#
#   1. Every artefact present here matches MANIFEST.md5.
#   2. This directory's static_id.txt equals the `fielded` row of
#      docs/FIELDED_SHELL.md -- i.e. these really are the build products of the
#      shell that is on the board, and not a preserved copy of some other mint.
#
# Check 2 is the one that earns its keep. A preserved DCP whose id nobody
# compared is a DCP you will discover is the wrong one at the moment you need
# it -- which is, by construction, the moment the original is gone.
#
# Exit 0 clean, 1 on any mismatch. Read-only; writes nothing anywhere.
#
#   bash fielded/0xA8C1C535/verify_fielded.sh
#   ALLOW_PARTIAL=1 bash .../verify_fielded.sh   # tolerate absent files,
#                                                # still fail on a WRONG one
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
AUTHORITY="$REPO_ROOT/docs/FIELDED_SHELL.md"
MANIFEST="$HERE/MANIFEST.md5"
DIR_ID="$(basename "$HERE")"
RC=0

fail() { printf '  FAIL: %s\n' "$*"; RC=1; }
ok()   { printf '  ok:   %s\n' "$*"; }

printf '== verify_fielded %s ==\n' "$DIR_ID"
[ -f "$MANIFEST" ] || { printf 'FATAL: no MANIFEST.md5 in %s\n' "$HERE" >&2; exit 1; }

# --- 1. md5 -----------------------------------------------------------------
printf '\n-- 1. md5 vs MANIFEST.md5 --\n'
present=0; absent=0
while read -r sum name; do
    case "$sum" in ''|\#*) continue ;; esac
    if [ ! -f "$HERE/$name" ]; then
        absent=$((absent+1))
        if [ "${ALLOW_PARTIAL:-0}" = "1" ]; then
            printf '  --    %s (absent; ALLOW_PARTIAL=1)\n' "$name"
        else
            fail "$name is MISSING (run fetch_fielded.sh)"
        fi
        continue
    fi
    present=$((present+1))
    got="$(md5sum "$HERE/$name" | cut -d' ' -f1)"
    if [ "$got" = "$sum" ]; then ok "$name"; else fail "$name md5 $got != $sum"; fi
done < "$MANIFEST"
printf '  (%d present, %d absent)\n' "$present" "$absent"
if [ "$present" -eq 0 ]; then
    fail "NOTHING is present -- this directory has never been populated. Run fetch_fielded.sh."
fi

# --- 2. static_id agrees with the authority ---------------------------------
printf '\n-- 2. static_id vs %s --\n' "docs/FIELDED_SHELL.md"
if [ ! -r "$AUTHORITY" ]; then
    fail "cannot read the authority $AUTHORITY"
else
    auth_id="$(sed -n 's/^|[[:space:]]*`fielded`[[:space:]]*|[[:space:]]*`\(0x[0-9A-Fa-f]\{8\}\)`.*/\1/p' "$AUTHORITY" | head -1)"
    if [ -z "$auth_id" ]; then
        fail "could not parse the 'fielded' row out of $AUTHORITY"
    elif [ ! -f "$HERE/static_id.txt" ]; then
        if [ "${ALLOW_PARTIAL:-0}" = "1" ]; then
            printf '  --    no static_id.txt here (absent; ALLOW_PARTIAL=1); authority says %s\n' "$auth_id"
        else
            fail "no static_id.txt here to compare against the authority ($auth_id)"
        fi
    else
        here_id="$(tr -d '[:space:]' < "$HERE/static_id.txt")"
        if [ "${here_id^^}" = "${auth_id^^}" ]; then
            ok "static_id.txt $here_id == FIELDED_SHELL.md fielded $auth_id"
        else
            fail "static_id.txt $here_id != FIELDED_SHELL.md fielded $auth_id"
            printf '        These artefacts are NOT the fielded shell. Either the board was\n'
            printf '        re-flashed and the authority updated without preserving the new\n'
            printf '        mint, or this directory holds a superseded one. Preserve the\n'
            printf '        CURRENT static before doing anything else.\n'
        fi
        # The directory NAME is a third, independent assertion of the same fact.
        if [ "${DIR_ID^^}" != "${here_id^^}" ]; then
            fail "directory name $DIR_ID != static_id.txt $here_id"
        fi
    fi
fi

printf '\n'
if [ "$RC" -eq 0 ]; then printf 'VERIFY_FIELDED_OK %s\n' "$DIR_ID"
else printf 'VERIFY_FIELDED_FAILED %s\n' "$DIR_ID"; fi
exit "$RC"
