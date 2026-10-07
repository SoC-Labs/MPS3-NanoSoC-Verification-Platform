#!/usr/bin/env python3
"""On-silicon proof of the QSPI WRITE path: unlock -> erase -> program -> verify.

Everything proven on hardware so far has been READ-ONLY (CLK_DIV read-back,
JEDEC, 64 B samples). Erase/program has NEVER run on this board, so the flash
programmer is unproven exactly where it is most destructive. This does the
smallest honest end-to-end write and reads it back.

SAFETY RAILS (why this is safe to grant a standing permission for)
------------------------------------------------------------------
* Writes at a SCRATCH offset only. The default 0x100000 (1 MiB) sits between the
  nanoSoC boot map (0x0-0x50000: TABLE0/TABLE1/COUNTER/CPU1 slots/GOLDEN) and the
  clearing STAGE/CACHE regions (0x780000/0x7C0000), colliding with neither
  (docs/QSPI_CLEARING_CACHE_HANDBACK.md:96-104).
* REFUSES any offset overlapping those reserved regions unless --i-know is given.
* Default payload is 256 B (one page). Capped at 4 KB (one sector) -- this is a
  smoke test, not a bulk programmer (bulk needs the M0 loader or the shell path;
  see docs/QSPI_RP_BOARD_BRINGUP.md "MEASURED").
* Never touches the XiP aperture. Reading an unconfigured 0x7000_0000 previously
  hung the shared AXI bus and took the whole shell off the network.

Usage (from the repo root):
    python3 -u scripts/qspi_write_smoke.py                 # 256 B @ 0x100000
    python3 -u scripts/qspi_write_smoke.py --dry-run       # show plan, touch nothing
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "host" / "pyverify"))

from pyverify.qspi_flash import QspiFlashProgrammer, QspiFlashError  # noqa: E402
from pyverify.swd import SwdDebugger  # noqa: E402

#: Regions this script refuses to write. (start, end_exclusive, name)
RESERVED = (
    (0x000000, 0x060000, "nanoSoC boot map / overlay store (TABLE0..GOLDEN)"),
    (0x780000, 0x790000, "clearing STAGE"),
    (0x7C0000, 0x7D0000, "clearing CACHE"),
)
DEFAULT_OFFSET = 0x100000
MAX_BYTES = 4096  # one sector; this is a smoke test, not a bulk programmer


def check_offset(base: int, length: int, forced: bool) -> None:
    end = base + length
    for r0, r1, name in RESERVED:
        if base < r1 and end > r0:
            msg = (
                f"REFUSING: [{base:#08x},{end:#08x}) overlaps {name} "
                f"[{r0:#08x},{r1:#08x})."
            )
            if not forced:
                raise SystemExit(msg + " Use --i-know only if you truly mean it.")
            print("!! " + msg + " --i-know given; proceeding.", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=lambda x: int(x, 0), default=DEFAULT_OFFSET)
    ap.add_argument("--bytes", type=int, default=256, dest="nbytes")
    ap.add_argument("--repo-dir", default=os.environ.get("MPS3_REPO_DIR") or str(REPO),
                    help="repo root ON THE HUB (OpenOCD cfgs resolve against it)")
    ap.add_argument("--clk-div", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--i-know", action="store_true",
                    help="override the reserved-region refusal (don't)")
    a = ap.parse_args()

    if not 0 < a.nbytes <= MAX_BYTES:
        raise SystemExit(f"--bytes must be in 1..{MAX_BYTES} (smoke test, not bulk)")
    check_offset(a.offset, a.nbytes, a.i_know)

    payload = bytes((i * 7 + 0x5A) & 0xFF for i in range(a.nbytes))
    print(f"PLAN: erase+program+verify {a.nbytes} B at {a.offset:#08x} "
          f"(CLK_DIV={a.clk_div})", flush=True)
    if a.dry_run:
        print("--dry-run: nothing touched.")
        return 0

    swd = SwdDebugger(repo_dir=a.repo_dir)
    prog = QspiFlashProgrammer(swd, clk_div=a.clk_div)
    swd.halt()
    t0 = time.time()

    print(f"CLK_DIV read-back = {prog.apply_clk_div()}", flush=True)
    print(f"JEDEC = {prog.assert_sst26vf064b()}", flush=True)

    print("unlock_global (WREN+ULBPR) ...", flush=True)
    prog.unlock_global()

    print(f"erase @ {a.offset:#08x} ...", flush=True)
    n = prog.erase_range(a.offset, a.nbytes)
    print(f"  erased {n} sector(s)  [{time.time() - t0:.0f}s]", flush=True)

    blank = prog.read_back(a.offset, 16)
    print(f"  post-erase first 16 B: {blank.hex()} (expect ff..)", flush=True)
    if not all(b == 0xFF for b in blank):
        print("  !! ERASE DID NOT TAKE - stopping before program", flush=True)
        return 2

    print(f"program {a.nbytes} B ...", flush=True)
    pages = prog.program(a.offset, payload)
    print(f"  programmed {pages} page(s)  [{time.time() - t0:.0f}s]", flush=True)

    print("verify (read-back compare) ...", flush=True)
    res = prog.verify(a.offset, payload)
    if res.ok:
        print(f"  VERIFY OK - write path PROVEN on silicon  [{time.time()-t0:.0f}s]")
        return 0
    print(f"  VERIFY MISMATCH @{res.mismatch_offset:#x} "
          f"exp {res.expected:#04x} got {res.actual:#04x}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except QspiFlashError as e:
        print(f"QSPI ERROR: {e}", file=sys.stderr)
        sys.exit(2)
