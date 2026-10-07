#!/usr/bin/env python3
"""Tier-1 (static) gate: no generated view of a shared truth is stale.

CATCHES: the failure mode the audit called "hand-copied truths" — one fact
written down in four or more files, kept in step by comments saying it was
"cross-checked by hand". Three of them had rotted:

  * the shell REGISTER MAP said "CLCDKVM — RESERVED, NOT INSTANTIATED" in
    ``platform_regs.h`` and in ``docs/contracts/shell-regmap.md`` while
    ``fpga/shell/bd/shell_bd.tcl:578`` instantiated ``clcd_kvm_0``;
  * the DIAG MAILBOX declared 25 counters, transported 14 over the wire and
    listed 20 in each of its two JTAG readers;
  * the 35-port PARTITION BOUNDARY was restated in ~13 places with only the RM
    side gated.

Each is now DERIVED once and rendered into every view by a ``tools/gen_*.py``.
That closes the drift only while the rendered files in the tree actually match a
fresh render — which is what this gate checks, and nothing else could: a
generator nobody re-runs is a comment.

HOW IT WORKS
    For each ``tools/gen_*.py``:
        gen --list                     -> the repo-relative paths it owns
        gen --out-dir <tmp>            -> a fresh render of every one of them
        diff tmp/<path> vs <path>      -> any difference is a FAILURE
    The temp dir is thrown away; this gate NEVER writes into the tree. The fix
    for a failure is always the same and is printed with it: re-run the
    generator and commit the result.

WHY REGENERATE INTO A TEMP DIR rather than trust the generator's own --check:
this gate must fail if the GENERATOR is broken as well as if the tree is stale.
Rendering to a real directory exercises the whole write path, and the diff is
computed here, by this file, from bytes on disk — a generator whose ``--check``
lied would still be caught.

Pure stdlib, seconds, no build, no board.

    python3 check_generated_fresh.py [--repo ROOT]
"""
from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
import tempfile

#: Generators are DISCOVERED, not listed. A new tools/gen_*.py is gated the
#: moment it exists — the opposite of the opt-in tables this whole change
#: replaces (the old regmap conformance test covered 7 of 15 blocks precisely
#: because every block had to be added to a list by hand).
GEN_DIR = "tools"
GEN_PREFIX = "gen_"


def _run(argv: list[str], cwd: str) -> tuple[int, str, str]:
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()
    root = args.repo or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."))
    gen_dir = os.path.join(root, GEN_DIR)

    print("== Tier-1 generated-view freshness gate ==")
    if not os.path.isdir(gen_dir):
        print(f"   SKIP: no {GEN_DIR}/ directory")
        return 0

    gens = sorted(n for n in os.listdir(gen_dir)
                  if n.startswith(GEN_PREFIX) and n.endswith(".py"))
    if not gens:
        # An empty generator set would make every check below pass vacuously,
        # which is the shape of gate this repo has been bitten by twice.
        print("   VIOLATION no tools/gen_*.py found -- either the generators "
              "moved or this gate stopped finding them.")
        print("\nFAIL: nothing to check.")
        return 1

    violations: list[str] = []
    checked = 0

    with tempfile.TemporaryDirectory(prefix="genfresh-") as tmp:
        for gen in gens:
            path = os.path.join(GEN_DIR, gen)
            rc, out, err = _run([sys.executable, path, "--repo", root, "--list"],
                                cwd=root)
            if rc != 0:
                violations.append(f"{path} --list failed (rc={rc}):\n"
                                  f"        {err.strip() or out.strip()}")
                continue
            rels = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if not rels:
                violations.append(f"{path} claims to own NO output files -- a "
                                  "generator that generates nothing cannot go "
                                  "stale, and cannot be a gate either")
                continue

            dest = os.path.join(tmp, gen)
            rc, out, err = _run([sys.executable, path, "--repo", root,
                                 "--out-dir", dest], cwd=root)
            if rc != 0:
                violations.append(f"{path} --out-dir failed (rc={rc}):\n"
                                  f"        {err.strip() or out.strip()}")
                continue

            stale = []
            for rel in rels:
                checked += 1
                tracked_p = os.path.join(root, rel)
                fresh_p = os.path.join(dest, rel)
                if not os.path.isfile(fresh_p):
                    violations.append(f"{path} listed '{rel}' but did not "
                                      "write it")
                    continue
                tracked = (open(tracked_p, errors="replace").read()
                           if os.path.isfile(tracked_p) else "")
                fresh = open(fresh_p, errors="replace").read()
                if tracked == fresh:
                    continue
                stale.append(rel)
                diff = "".join(difflib.unified_diff(
                    tracked.splitlines(keepends=True),
                    fresh.splitlines(keepends=True),
                    fromfile=f"{rel} (in the tree)",
                    tofile=f"{rel} (regenerated)", n=2))
                # Enough to identify the drift without dumping a whole file.
                head = "".join(diff.splitlines(keepends=True)[:40])
                violations.append(
                    f"'{rel}' is STALE with respect to {path}:\n" + head +
                    f"        fix: python3 {path}")
            print(f"   {path}: {len(rels)} view(s), "
                  f"{'STALE: ' + ', '.join(stale) if stale else 'fresh'}")

    for v in violations:
        print(f"   VIOLATION {v}")
    if violations:
        print(f"\nFAIL: {len(violations)} stale/broken generated view(s). "
              "A generated file edited by hand is a fact that will be wrong "
              "somewhere -- change the source and re-run the generator.")
        return 1
    print(f"\nOK: {len(gens)} generator(s), {checked} generated view(s), all "
          "byte-identical to a fresh render")
    return 0


if __name__ == "__main__":
    sys.exit(main())
