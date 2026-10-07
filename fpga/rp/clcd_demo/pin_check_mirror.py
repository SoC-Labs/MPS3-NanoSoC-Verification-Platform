#!/usr/bin/env python3
"""pin_check_mirror.py -- run the REAL boundary gate over a SCRATCH MIRROR of
the RM registry with `rm_clcd_demo` registered, without touching
`fpga/dfx/rm_list.tcl`.

    python3 fpga/rp/clcd_demo/pin_check_mirror.py [--keep]

WHY
---
`fpga/dfx/pin_check.py` derives its target set from `RM_ORDER` in
`fpga/dfx/rm_list.tcl` -- that is the whole point of it, and it is why a
registered RM cannot slip past the gate. But rm_list.tcl is the MINT FLOW's
file: registering an RM there commits the next mint to building it and makes
`check_rm_id_encoding.py` demand an overlay manifest that does not exist yet.

So this script builds a throwaway tree that pin_check sees as a repository:

    <tmp>/fpga/dfx/pin_check.py   a fresh COPY of the real gate (not a symlink:
                                  pin_check resolves its own __file__, so a
                                  symlink would resolve back to the real repo
                                  and silently check the real registry -- a
                                  mirror that proves nothing)
    <tmp>/fpga/dfx/rm_list.tcl    the real registry + this RM's snippet
    <tmp>/fpga/rp   -> symlink    the real wrappers
    <tmp>/fpga/dfx/rms -> symlink the real single-file RMs

and runs the copy. The gate code is copied fresh on every run, so it cannot
drift from the one `make check` runs; only the REGISTRY is synthetic.

Exit 0 = every registered wrapper, including this one, conforms.
Stdlib only. Board-free. Writes nothing inside the repository.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RM_KEY = "rm_clcd_demo"


def snippet_entries(snippet: pathlib.Path) -> str:
    """The `set RM_LIB(rm_clcd_demo,...)` lines out of rm_list_snippet.tcl.

    Taken from the snippet rather than restated here, so the thing that is
    PROVEN is the thing a maintainer will paste."""
    lines = [ln for ln in snippet.read_text().splitlines()
             if re.match(rf"\s*set\s+RM_LIB\({re.escape(RM_KEY)},", ln)]
    if not lines:
        raise SystemExit(f"{snippet}: no `set RM_LIB({RM_KEY},...)` lines")
    return "\n".join(lines)


def build_mirror(dest: pathlib.Path) -> pathlib.Path:
    (dest / "fpga" / "dfx").mkdir(parents=True)
    shutil.copy2(ROOT / "fpga/dfx/pin_check.py", dest / "fpga/dfx/pin_check.py")
    (dest / "fpga" / "rp").symlink_to(ROOT / "fpga" / "rp")
    (dest / "fpga" / "dfx" / "rms").symlink_to(ROOT / "fpga" / "dfx" / "rms")

    tcl = (ROOT / "fpga/dfx/rm_list.tcl").read_text()
    m = re.search(r"set\s+RM_ORDER\s+\[list\s+([^\]]+)\]", tcl)
    if not m:
        raise SystemExit("fpga/dfx/rm_list.tcl: no RM_ORDER to mirror")
    if RM_KEY in m.group(1).split():
        print(f"NOTE: {RM_KEY} is ALREADY in the real RM_ORDER -- this mirror "
              f"is redundant; run `make -C fpga/dfx pin-check` instead.")
    order = m.group(1).split() + [RM_KEY]
    tcl = tcl[:m.start()] + "set RM_ORDER [list " + " ".join(order) + "]" + tcl[m.end():]
    tcl += ("\n\n# --- SCRATCH MIRROR ONLY (fpga/rp/clcd_demo/pin_check_mirror.py) ---\n"
            + snippet_entries(HERE / "rm_list_snippet.tcl") + "\n")
    (dest / "fpga/dfx/rm_list.tcl").write_text(tcl)
    return dest / "fpga/dfx/pin_check.py"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true",
                    help="leave the mirror on disk and print its path")
    a = ap.parse_args()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="clcd_demo_pincheck_"))
    try:
        gate = build_mirror(tmp)
        print(f"== scratch registry mirror: {tmp}")
        print(f"   RM_ORDER + {RM_KEY}, entries from "
              f"fpga/rp/clcd_demo/rm_list_snippet.tcl\n")
        rc = subprocess.call([sys.executable, str(gate)])
        if rc == 0:
            print(f"\nOK: the real gate passes with {RM_KEY} registered. "
                  f"fpga/dfx/rm_list.tcl is UNTOUCHED.")
        return rc
    finally:
        if a.keep:
            print(f"(kept: {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
