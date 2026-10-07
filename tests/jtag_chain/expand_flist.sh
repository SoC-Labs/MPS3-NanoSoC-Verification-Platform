#!/bin/bash
#-----------------------------------------------------------------------------
# expand_flist.sh — Recursively expand -f includes and $(VAR) → ${VAR}
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
# Usage:  ./scripts/expand_flist.sh <input.flist> [output.flist]
#
# Many nanosoc_arch_tech submodule flists use Makefile-style $(VAR) path
# variables and nested `-f <path>` includes. VCS only expands ${VAR} and
# $VAR (env-var syntax) and reads -f literally without variable substitution
# inside nested files.
#
# This script:
#   1. Reads the input flist line by line
#   2. On `-f <path>` lines: expands the path using env vars, then
#      recursively processes the included flist
#   3. Converts all $(VAR) references to ${VAR} so VCS can expand them
#   4. Strips comments and blank lines
#   5. Outputs a flat flist to stdout (or to the output file if given)
#-----------------------------------------------------------------------------
set -euo pipefail

expand_flist() {
    local flist_file="$1"
    local dir
    dir="$(dirname "$flist_file")"

    while IFS= read -r line || [ -n "$line" ]; do
        # Strip leading/trailing whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"

        # Skip empty lines and full-line comments
        [[ -z "$line" || "$line" == //* ]] && continue

        # Convert $(VAR) → ${VAR}
        line="$(echo "$line" | sed 's/\$(\([^)]*\))/${\1}/g')"

        # Handle -f includes (recursive)
        if [[ "$line" == -f\ * ]]; then
            local inc_path="${line#-f }"
            inc_path="${inc_path#"${inc_path%%[![:space:]]*}"}"
            # Expand env vars in the path
            inc_path="$(eval echo "$inc_path" 2>/dev/null || echo "$inc_path")"
            if [ -f "$inc_path" ]; then
                expand_flist "$inc_path"
            else
                echo "// WARNING: cannot find included flist: $inc_path" >&2
            fi
            continue
        fi

        echo "$line"
    done < "$flist_file"
}

if [ $# -lt 1 ]; then
    echo "Usage: $0 <input.flist> [output.flist]" >&2
    exit 1
fi

input="$1"
if [ ! -f "$input" ]; then
    echo "Error: $input not found" >&2
    exit 1
fi

if [ $# -ge 2 ]; then
    expand_flist "$input" > "$2"
else
    expand_flist "$input"
fi
