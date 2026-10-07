#!/usr/bin/env python3
"""Tier-0 gate: the firmware's two clearing buffers must hold every RM's clearing,
so no clearing ever reaches the QSPI flash.

CATCHES: the silicon bug recorded in 82244ac -- "a 51,952 B clearing against a
4,096 B arena, so regdemo_b -> regdemo_a could never work". Two independent
buffers must each be >= the largest clearing:

  MPS3_CFG_AGENT_CLEARING_RAM_BYTES  (firmware/platform/Makefile CLEARING_RAM_BYTES)
      config_agent's receive slot. config_agent.c:359 routes a payload to a sink
      only when it OVERFLOWS this slot, so a clearing that fits stays in RAM.
      UNSET => config_agent.h falls back to STAGING_BYTES.

  MPS3_SWAP_CLEARING_ARENA_BYTES     (firmware/platform/Makefile SWAP_CLEARING_ARENA_BYTES)
      swap_fsm's resident-clearing arena. If the incoming clearing does not fit,
      step_cache_clearing() refuses to cache it and marks it invalid, so the NEXT
      swap-AWAY fails closed at SWAP_STREAM_CLEARING.

Either being too small means QSPI writes, or a swap-away that cannot work. QSPI
has never worked on silicon and D16 (ARCHITECTURE_SPEC.md §15) says keep the
interface idle -- one physical 8 MiB part backs both the DFX overlay store and
the nanoSoC boot map, and overlay_store_bp_unlock() strips its write protection.

Pure stdlib, sub-second, no tools. Skips cleanly when no overlays are built yet.

    python3 check_clearing_fits.py [--repo ROOT]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def _mk_var(text: str, name: str) -> str | None:
    """Value of a `NAME ?= value` / `NAME := value` assignment, or None."""
    m = re.search(r"^%s\s*[:?]?=\s*(.*?)\s*(?:#.*)?$" % re.escape(name), text, re.M)
    if not m:
        return None
    return m.group(1).strip()


def _as_int(v: str | None) -> int | None:
    if not v:
        return None
    try:
        return int(v, 0)
    except ValueError:
        return None


def load_waivers(path: str | None) -> dict[str, str]:
    """`<rm_name>  # reason / owner` per line. Mirrors param_parity_waivers.txt."""
    out: dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return out
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, reason = line.partition("#")
        name = name.strip()
        if name:
            out[name] = reason.strip()
    return out


# ---------------------------------------------------------------------------
# --what-if: LMB clearing-fit decision table (advisory, opt-in). NOT used by the
# gate's pass/fail above.
#
# The gate proper compares each clearing against the two firmware buffer sizes
# read from the Makefile; it does NOT model the shell's local LMB. This block
# reconstructs the LMB-fit model that SIZED those buffers so the burn-down
# options can be compared live instead of only in a commit message. The model is
# documented (as prose, not machine-readable assignments) in three places that
# agree with each other:
#   firmware/platform/Makefile              (~L96)
#   fpga/shell/bd/shell_bd.tcl              (~L319, "core ~= 226,652 B")
#   scripts/harness_gates/clearing_fit_waivers.txt (~L12)
#
#   Both clearing buffers must COEXIST in the LMB, so decoded LMB usage is
#       dec = FIXED + 2*X          (X = per-buffer cap)
#   and the largest X that still links is
#       X = (usable(LMB_KB) - FIXED) // 2.
#
# FIXED and `usable` are build-map-measured figures, NOT Makefile `NAME=value`
# assignments, so they are named constants here with their source cited. What CAN
# be derived from source is derived: LMB_KB/BLOB come from the Makefile via
# _mk_var, and the BLOB=placeholder saving is recomputed from the live greybox
# manifest (the same clearing.len the gate already reads) minus the 64-NOP
# placeholder, rather than pasting a literal.
FIXED_REAL_BYTES = 226652        # "core" non-buffer LMB decode with BLOB=real.
                                 # Makefile L96 / shell_bd.tcl L319 / waivers L12.
LMB_DECODE_OVERHEAD = 208        # usable = LMB_KB*1024 - 208. Reproduces both
                                 # documented usables exactly:
                                 #   512  -> 524,288-208 =   524,080  (Makefile L96)
                                 #   1024 -> 1,048,576-208 = 1,048,368 (Makefile L98)
PLACEHOLDER_BLOB_BYTES = 64 * 4  # gen_greybox_blob.py: PLACEHOLDER_WORDS=64 ICAP
                                 # type-1 NOPs, 4 B each. BLOB=placeholder swaps
                                 # the baked greybox clearing for these NOPs.


def _lmb_usable(lmb_kb: int) -> int:
    return lmb_kb * 1024 - LMB_DECODE_OVERHEAD


def _buffer_cap(usable: int, fixed: int) -> int:
    """Largest per-buffer clearing X s.t. FIXED + 2X still fits `usable`."""
    return (usable - fixed) // 2


def _print_what_if(root: str) -> None:
    """Advisory decision table: per-buffer cap and each RM's fit under the three
    burn-down options. Never affects the gate's exit code or normal report."""
    # Reuse the same manifest source the gate reads, so the RM list/sizes match.
    rms: list[tuple[str, int]] = []
    for m in sorted(glob.glob(os.path.join(root, "fpga", "dfx", "overlay", "*", "manifest.json"))):
        try:
            d = json.load(open(m))
        except (OSError, ValueError):
            continue
        clr = (d.get("clearing") or {}).get("len")
        if clr is not None:
            rms.append((os.path.basename(os.path.dirname(m)), int(clr)))
    rms.sort(key=lambda t: t[1])
    greybox = dict(rms).get("greybox")  # baked blob = greybox clearing under BLOB=real

    # BLOB=placeholder frees (baked greybox clearing - 64-NOP placeholder) of FIXED.
    if greybox is not None:
        freed = greybox - PLACEHOLDER_BLOB_BYTES
        fixed_placeholder = FIXED_REAL_BYTES - freed
        placeholder_note = "= real - (greybox %s - %s NOPs)" % (
            format(greybox, ","), PLACEHOLDER_BLOB_BYTES)
    else:
        freed = None
        fixed_placeholder = None
        placeholder_note = "(greybox manifest missing -- cannot derive)"

    # (label, lmb_kb, fixed) for each burn-down option.
    options = [
        ("BLOB=real,        LMB_KB=512",  512,  FIXED_REAL_BYTES),
        ("BLOB=placeholder, LMB_KB=512",  512,  fixed_placeholder),
        ("BLOB=real,        LMB_KB=1024", 1024, FIXED_REAL_BYTES),
    ]

    # The config actually selected today, read live from the Makefile.
    cur_kb = cur_blob = "?"
    mk = os.path.join(root, "firmware", "platform", "Makefile")
    if os.path.isfile(mk):
        mtext = open(mk).read()
        cur_kb = _mk_var(mtext, "LMB_KB") or "?"
        cur_blob = _mk_var(mtext, "BLOB") or "?"

    print()
    print("== --what-if: per-buffer clearing cap under three LMB burn-down options ==")
    print("   Advisory only -- does NOT change the gate result above. Model: both")
    print("   clearing buffers coexist in the shell LMB, so decoded LMB = FIXED + 2*cap;")
    print("   cap = (usable - FIXED) // 2,  usable = LMB_KB*1024 - %d." % LMB_DECODE_OVERHEAD)
    print("     FIXED(BLOB=real)        = %9s B   (build-map core; Makefile L96 / shell_bd.tcl L319)"
          % format(FIXED_REAL_BYTES, ","))
    print("     FIXED(BLOB=placeholder) = %9s B   %s"
          % (format(fixed_placeholder, ",") if fixed_placeholder is not None else "n/a",
             placeholder_note))
    print("     currently configured    : LMB_KB=%s, BLOB=%s  (firmware/platform/Makefile)"
          % (cur_kb, cur_blob))
    print()
    print("   %-30s %12s %12s %14s" % ("option", "usable", "FIXED", "per-buffer cap"))
    caps: list[int | None] = []
    for label, kb, fixed in options:
        usable = _lmb_usable(kb)
        if fixed is None:
            caps.append(None)
            print("   %-30s %12s %12s %14s" % (label, format(usable, ","), "n/a", "n/a"))
        else:
            cap = _buffer_cap(usable, fixed)
            caps.append(cap)
            print("   %-30s %12s %12s %14s"
                  % (label, format(usable, ","), format(fixed, ","), format(cap, ",")))

    if not rms:
        print("\n   (no overlay manifests scanned -- per-RM fit table omitted)")
        return
    print()
    print("   per-RM fit  (headroom = cap - clearing; negative = OVERFLOW -> QSPI)")
    print("   %-12s %10s %16s %16s %16s"
          % ("RM", "clearing", "real@512", "placeholder@512", "real@1024"))
    for name, clr in sorted(rms, key=lambda t: -t[1]):
        cells = []
        for cap in caps:
            if cap is None:
                cells.append("n/a")
            else:
                head = cap - clr
                cells.append(("FIT +%s" % format(head, ",")) if head >= 0
                             else ("OVER %s" % format(head, ",")))
        print("   %-12s %10s %16s %16s %16s"
              % (name, format(clr, ","), cells[0], cells[1], cells[2]))


def _report(root: str, waived: dict[str, str]) -> int:
    mk = os.path.join(root, "firmware", "platform", "Makefile")
    if not os.path.isfile(mk):
        print("FAIL: cannot find %s" % mk, file=sys.stderr)
        return 1
    text = open(mk).read()

    staging = _as_int(_mk_var(text, "STAGING_BYTES"))
    arena = _as_int(_mk_var(text, "SWAP_CLEARING_ARENA_BYTES"))
    # UNSET CLEARING_RAM_BYTES falls back to STAGING_BYTES (config_agent.h).
    clearing_raw = _mk_var(text, "CLEARING_RAM_BYTES")
    clearing = _as_int(clearing_raw)
    fell_back = False
    if clearing is None:
        clearing, fell_back = staging, True

    print("== Tier-0 clearing-buffer fit gate (no clearing may reach QSPI) ==")
    if arena is None or clearing is None:
        print("FAIL: could not parse SWAP_CLEARING_ARENA_BYTES / CLEARING_RAM_BYTES "
              "from firmware/platform/Makefile", file=sys.stderr)
        return 1

    print("   CLEARING_RAM_BYTES        = %-8d %s" % (
        clearing, "(UNSET -> falls back to STAGING_BYTES)" if fell_back else ""))
    print("   SWAP_CLEARING_ARENA_BYTES = %d" % arena)

    manifests = sorted(glob.glob(os.path.join(root, "fpga", "dfx", "overlay", "*", "manifest.json")))
    if not manifests:
        print("\nSKIP: no fpga/dfx/overlay/*/manifest.json yet — nothing to size against "
              "(run 'make -C fpga/dfx overlays')")
        return 0

    rms: list[tuple[str, int]] = []
    for m in manifests:
        try:
            d = json.load(open(m))
        except (OSError, ValueError) as exc:
            print("FAIL: cannot read %s: %s" % (m, exc), file=sys.stderr)
            return 1
        clr = (d.get("clearing") or {}).get("len")
        if clr is None:
            print("FAIL: %s has no clearing.len" % m, file=sys.stderr)
            return 1
        rms.append((os.path.basename(os.path.dirname(m)), int(clr)))
    rms.sort(key=lambda t: t[1])

    largest_name, largest = rms[-1]
    print("   scanned %d overlay(s); largest clearing = %s (%d B)\n" % (len(rms), largest_name, largest))

    bad_ram = [(n, c) for n, c in rms if c > clearing and n not in waived]
    bad_arena = [(n, c) for n, c in rms if c > arena and n not in waived]

    for name, clr in rms:
        flags = []
        if clr > clearing:
            flags.append("-> QSPI (exceeds CLEARING_RAM_BYTES)")
        if clr > arena:
            flags.append("no swap-away (exceeds arena)")
        if not flags:
            tag = "ok  "
        elif name in waived:
            tag = "WAIV"
        else:
            tag = "BAD "
        print("   %-6s %-12s %8d B  %s" % (tag, name, clr, "; ".join(flags)))

    hit = [n for n in waived if any(n == r and (c > clearing or c > arena) for r, c in rms)]
    if hit:
        print()
        for n in sorted(hit):
            print("   WAIVED  %-12s %s" % (n, waived[n] or "(no reason given)"))
        print("   A waiver is NOT a fix. It keeps this gate green on a KNOWN, tracked gap.")

    if not bad_ram and not bad_arena:
        if hit:
            print("\nOK: every non-waived clearing fits both buffers. %d RM(s) WAIVED — see above."
                  % len(hit))
        else:
            print("\nOK: both buffers hold every clearing (largest %d B); no clearing reaches QSPI." % largest)
        return 0

    print()
    if bad_ram:
        print("FAIL: %d clearing(s) exceed CLEARING_RAM_BYTES=%d and would be staged to the QSPI"
              % (len(bad_ram), clearing))
        print("      clearing-STAGE region: %s" % ", ".join(n for n, _ in bad_ram))
    if bad_arena:
        print("FAIL: %d clearing(s) exceed SWAP_CLEARING_ARENA_BYTES=%d; swap-AWAY fails closed"
              % (len(bad_arena), arena))
        print("      at SWAP_STREAM_CLEARING for: %s" % ", ".join(n for n, _ in bad_arena))
    need = largest
    print("\n      Set both to >= %d in firmware/platform/Makefile. Use a margin, not an exact fit:"
          % need)
    print("      config_agent.c:359 routes on `payload > cap`, so %d clears the flash path by one byte."
          % need)
    print("      QSPI has never worked on silicon and D16 says keep that interface idle.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--waivers", default=None,
                    help="file of RMs known not to fit; keeps the gate green on a TRACKED gap")
    ap.add_argument("--what-if", action="store_true",
                    help="after the normal report, print an advisory table of the per-buffer "
                         "clearing cap and each RM's fit under the three LMB burn-down options "
                         "(BLOB=real/placeholder @512, LMB_KB=1024); does NOT affect the exit code")
    args = ap.parse_args()
    root = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    waived = load_waivers(args.waivers)

    rc = _report(root, waived)
    if args.what_if:
        _print_what_if(root)
    return rc


if __name__ == "__main__":
    sys.exit(main())
