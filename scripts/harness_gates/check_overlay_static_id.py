#!/usr/bin/env python3
"""Tier-0 gate: every overlay/<rm>/manifest.json's static_id must match the
one authoritative shell static_id (fpga/dfx/overlay/mps3_shell_static_id.c).

CATCHES: risk R9 ("one locked static_id per release",
docs/PLATFORM_STATUS_AND_ROADMAP.md). A shell rebuild re-mints static_id and
invalidates EVERY stored partial (overlay-manifest.md). gen_manifest.py's own
`verify` only round-trips each manifest's CRC/size against its own payloads --
it never compares a manifest's static_id against the shell's, so an overlay set
keyed to a DEAD shell sails through the gate and only surfaces two stages later
as a confusing pyverify e2e failure ("manifest=0x3a8bba62 running shell=
0xeccedbf3"). This closes that hole cheaply.

The authoritative id is parsed out of the GENERATED mps3_shell_static_id.c --
the strong override A3 compiles+links into the shell firmware, so the running
shell's ping reports exactly this value (overlay-manifest.md; net-protocol.md
ping.shell_id). If any overlay manifest disagrees, that overlay is stale.

static_id literals are normalised with int(x, 0), matching the manifest
consumers (host/pyverify/pyverify/overlay.py:_parse_int and the worked example
in overlay-manifest.md, which mix plain hex "0xA1B2C3D4" and underscore-grouped
"0x0000_0001") -- not a third bespoke parser.

Pure stdlib, seconds, no tools.

    python3 check_overlay_static_id.py [--repo ROOT]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

# The generated file is a fixed C stub: `uint32_t mps3_shell_static_id(void) {
# return 0x3A8BBA62UL; }`. Anchor on the function body (so the two prose
# mentions of "mps3_shell_static_id()" in the header comment -- which are not
# followed by a `{` -- never match) and capture the hex literal, ignoring any
# U/L integer-suffix and an optional wrapping paren.
_SHELL_ID_RX = re.compile(
    r"mps3_shell_static_id\s*\([^)]*\)\s*\{"
    r".*?\breturn\b\s*\(?\s*(0[xX][0-9a-fA-F_]+)[uUlL]*\s*\)?\s*;",
    re.S,
)


def _parse_id(value: object) -> int:
    """Normalise a static_id literal to an int with int(x, 0) -- the same
    spelling the manifest consumers use (pyverify.overlay._parse_int), so plain
    hex and PEP-515 underscore-grouped hex both parse."""
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def _shell_static_id(path: str) -> tuple[int, str]:
    """Parse the authoritative id out of mps3_shell_static_id.c.

    Returns (id_int, id_text). Raises ValueError if the file has no parseable
    mps3_shell_static_id() return value."""
    text = open(path).read()
    m = _SHELL_ID_RX.search(text)
    if not m:
        raise ValueError("no parseable `return 0x...;` in mps3_shell_static_id()")
    return _parse_id(m.group(1)), m.group(1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    args = ap.parse_args(argv)
    root = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    overlay_dir = os.path.join(root, "fpga", "dfx", "overlay")
    shell_c = os.path.join(overlay_dir, "mps3_shell_static_id.c")
    manifests = sorted(glob.glob(os.path.join(overlay_dir, "*", "manifest.json")))

    print("== overlay static_id lockstep vs shell (R9) ==")

    # SKIP cleanly if no overlays exist yet -- mirrors the root Makefile's
    # check-overlays guard (nothing to verify until `make -C fpga/dfx overlays`
    # has emitted triples).
    if not manifests:
        print("   SKIP: no fpga/dfx/overlay/*/manifest.json yet "
              "(run 'make -C fpga/dfx overlays')")
        return 0

    # The authoritative id: the generated strong override the running shell
    # reports. Missing/unparseable is a HARD fail -- without it the set cannot
    # be certified against any shell.
    if not os.path.isfile(shell_c):
        print("   scanned %d overlay manifest(s)" % len(manifests))
        print("\nFAIL: authoritative shell id file missing: %s" % shell_c)
        print("      cannot verify overlay static_ids without it "
              "(regenerate via 'make -C fpga/dfx overlays').")
        return 1
    try:
        shell_id, shell_txt = _shell_static_id(shell_c)
    except (OSError, ValueError) as exc:
        print("   scanned %d overlay manifest(s)" % len(manifests))
        print("\nFAIL: cannot parse shell static_id from %s: %s" % (shell_c, exc))
        return 1

    print("   shell static_id = 0x%08X  (%s -> %s)"
          % (shell_id, os.path.relpath(shell_c, root), shell_txt))
    print("   scanned %d overlay manifest(s):" % len(manifests))

    stale: list[str] = []
    bad: list[str] = []
    for m in manifests:
        rm = os.path.basename(os.path.dirname(m))
        try:
            sid = _parse_id(json.loads(open(m).read())["static_id"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print("   BADID  %-12s <unparseable static_id: %s>" % (rm, exc))
            bad.append(rm)
            continue
        if sid == shell_id:
            print("   OK     %-12s 0x%08X" % (rm, sid))
        else:
            print("   STALE  %-12s 0x%08X  != shell 0x%08X" % (rm, sid, shell_id))
            stale.append(rm)

    if stale or bad:
        if bad:
            print("\nFAIL: %d overlay(s) with an unparseable static_id: %s"
                  % (len(bad), ", ".join(bad)))
        if stale:
            print("\nFAIL: %d overlay(s) keyed to a DEAD shell: %s"
                  % (len(stale), ", ".join(stale)))
            print("      re-mint against 0x%08X (re-run 'make -C fpga/dfx overlays' "
                  "for those RMs); a shell rebuild re-mints static_id and "
                  "invalidates every stored partial (overlay-manifest.md)." % shell_id)
        return 1

    print("\nOK: all %d overlay static_ids match the shell (0x%08X)"
          % (len(manifests), shell_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
