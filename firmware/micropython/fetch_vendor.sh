#!/usr/bin/env bash
# Fetch the MicroPython upstream clone the NanoSoC port builds against.
# vendor/ is gitignored (59MB upstream tree, not vendored into this repo) —
# run this once before `make` in firmware/micropython/. Pinned to the exact
# rev the port was developed against; do not float it silently.
set -euo pipefail
REV=532428cc6d85b1183a43bf82ca789e3f3f99fd1f
URL=https://github.com/micropython/micropython.git
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HERE/vendor/micropython"
if [ -e "$DEST/.git" ]; then
  cur="$(git -C "$DEST" rev-parse HEAD)"
  if [ "$cur" = "$REV" ]; then echo "vendor/micropython already at $REV"; exit 0; fi
  echo "vendor/micropython at $cur, want $REV — fetching"
  git -C "$DEST" fetch --depth 1 origin "$REV"
  git -C "$DEST" checkout -q "$REV"
else
  mkdir -p "$HERE/vendor"
  git clone "$URL" "$DEST"
  git -C "$DEST" checkout -q "$REV"
fi
echo "vendor/micropython pinned at $REV"
