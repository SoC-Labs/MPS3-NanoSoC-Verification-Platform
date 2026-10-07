#!/usr/bin/env python3
"""Tier-2 gate: assert on the IMPLEMENTED shell's report ARTIFACTS, not the
build log.

CATCHES (the artifact-vs-model class behind several escapes):
  - timing WNS > 0                     -- a shipped shell that does not close
                                          timing is a silent field failure.
  - 0 DRC *errors*                     -- routed-design legality.
  - report_methodology LUTAR-1 == 0    -- a LUT driving an async reset is the
                                          exact reset-path smell the R7 reset-fix
                                          wave (bug #5's neighbourhood) chased;
                                          the task lists "0 LUTAR-1" as a hard
                                          artifact assertion.

Reads fpga/shell/build_results_*/ (newest, or --dir). Pure stdlib, no Vivado --
it parses the reports a build already emitted. The Tier-2 Tcl gate
(tier2_dcp_assert.tcl) covers the checks that need the DCP open (fixed-RTL
present, ASYNC_REG count, decoupler boundary pins); this covers the ones a text
report already answers.

    python3 check_impl_reports.py [--dir build_results_DIR] [--strict-methodology]
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys


def newest_results(root: str) -> str | None:
    dirs = sorted(glob.glob(os.path.join(root, "fpga", "shell", "build_results_*")))
    return dirs[-1] if dirs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--dir", default=None, help="a build_results_* directory")
    ap.add_argument("--strict-methodology", action="store_true",
                    help="treat LUTAR-1 > 0 as a hard failure (default: advisory)")
    args = ap.parse_args()
    root = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    d = args.dir or newest_results(root)

    print("== Tier-2 implemented-shell report gate ==")
    if not d or not os.path.isdir(d):
        print("SKIP: no build_results_* directory found (no implemented shell to "
              "assert on yet).")
        return 0
    print("   reports: %s" % os.path.relpath(d, root))

    fails: list[str] = []

    # --- timing WNS > 0 ---
    tpath = os.path.join(d, "post_impl_timing_summary.rpt")
    if os.path.isfile(tpath):
        t = open(tpath).read()
        m = re.search(r"WNS\(ns\).*?\n\s*-+.*?\n\s*(-?\d+\.\d+)", t, re.S)
        if not m:
            fails.append("could not parse WNS from post_impl_timing_summary.rpt")
        else:
            wns = float(m.group(1))
            status = "OK" if wns > 0 else "FAIL"
            print("   %s  timing WNS = %+.3f ns" % (status, wns))
            if wns <= 0:
                fails.append("WNS = %+.3f ns (design does not close timing)" % wns)
        # methodology LUTAR-1
        lm = re.search(r"LUTAR-1\s+\w+\s+.*?\s(\d+)\s*$", t, re.M)
        lutar = int(lm.group(1)) if lm else 0
        if lutar > 0:
            print("   %s  report_methodology LUTAR-1 = %d (LUT drives async reset)"
                  % ("FAIL" if args.strict_methodology else "ADVISORY", lutar))
            if args.strict_methodology:
                fails.append("LUTAR-1 = %d (LUT drives async reset)" % lutar)
        else:
            print("   OK  report_methodology LUTAR-1 = 0")

        # --- check_timing: nothing may ship UNCONSTRAINED -------------------
        # WHY (the eth_ss lesson): WNS > 0 is NOT sufficient. An undeclared
        # clock domain has no timed paths at all, so it contributes nothing to
        # WNS and the headline stays green while the domain ships unconstrained.
        # That is exactly how eth_ss shipped its MII domain UNCLOCKED --
        # partition-timing.md records no_clock going 386 -> 0 after the fix, a
        # number nothing was gating. The virtual-PHY integration adds a 50 MHz
        # domain plus an RM-generated `mdc` clock crossing RP->static, so this
        # is the assertion that stops the same mistake landing again.
        #
        # Parse the DETAIL section's prose line, not the heading: the report
        # prints "N. checking <name> (count)" twice -- once in a Table of
        # Contents and once as the real section -- and a regex that matches the
        # ToC would still be "reading the report" while proving nothing.
        for label, rx, human in (
            ("no_clock",
             r"There are (\d+) register/latch pins with no clock",
             "register/latch pins with NO CLOCK"),
            ("unconstrained_internal_endpoints",
             r"There are (\d+) pins that are not constrained for maximum delay",
             "pins NOT CONSTRAINED for max delay"),
        ):
            cm = re.search(rx, t)
            if not cm:
                # Absence must never read as success -- if the report format
                # moves, fail loudly rather than silently stop checking.
                print("   FAIL  could not parse check_timing '%s' from the report"
                      % label)
                fails.append("could not parse check_timing '%s' (report format "
                             "changed? the assertion is no longer running)" % label)
                continue
            n = int(cm.group(1))
            print("   %s  check_timing %s = %d" % ("OK" if n == 0 else "FAIL", label, n))
            if n:
                fails.append(
                    "check_timing %s = %d %s. WNS is meaningless for these -- an "
                    "unconstrained domain has no timed paths, so the headline stays "
                    "green while the domain ships unconstrained (the eth_ss MII "
                    "failure). Declare the clock/constraint, do not waive."
                    % (label, n, human))
    else:
        print("   SKIP  no post_impl_timing_summary.rpt")

    # --- DRC: 0 errors (warnings reported, not failed) ---
    dpath = os.path.join(d, "post_impl_drc.rpt")
    if os.path.isfile(dpath):
        drc = open(dpath).read()
        rows = re.findall(r"^\|\s*([\w-]+)\s*\|\s*(\w+)\s*\|.*?\|\s*(\d+)\s*\|", drc, re.M)
        errs = [(r, s, n) for (r, s, n) in rows if s.lower() == "error"]
        warns = [(r, s, n) for (r, s, n) in rows if s.lower() == "warning"]
        if errs:
            for r, s, n in errs:
                print("   FAIL  DRC %s (Error) x%s" % (r, n))
                fails.append("DRC error %s x%s" % (r, n))
        else:
            print("   OK  0 DRC errors (%d warning rule(s): %s)"
                  % (len(warns), ", ".join(r for r, _, _ in warns) or "none"))
    else:
        print("   SKIP  no post_impl_drc.rpt")

    if fails:
        print("\nFAIL: %d implemented-shell assertion(s) failed:" % len(fails))
        for f in fails:
            print("   - %s" % f)
        return 1
    print("\nOK: implemented shell passes timing/DRC artifact assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
