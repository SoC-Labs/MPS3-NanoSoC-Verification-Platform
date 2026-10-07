#!/usr/bin/env python3
"""check_hdpr_reports.py -- the DFX DRC reports as a GATE, not a file nobody reads.

build_dfx.tcl writes two DRC reports per config:

  drc_hdpr_<rm_key>_prelink.rpt   report_drc -checks [get_drc_checks HDPR*]
                                  after the RM is linked, BEFORE opt/place/route
  drc_<rm_key>.rpt                the full report_drc on the routed config

Until 2026-09-23 nothing parsed either (handover §4.7 item 5; U5 asked whether an
HDPR violation even stops the Tcl -- it does not, report_drc only writes). This
fails on:

  * HDPR-16  a debug core in the RP looking for a hub on a BSCANE2 (an ILA with
             no RM-side bridge -- handover trap 2)
  * HDPR-18  a clock buffer / BSCAN site pulled into the RP pblock (trap 1, F7)
  * HDPR-50  the shell's I/O column dragged into the RP (what HDPR-18 turns into)
             -- each at ANY severity, in either report;
  * any rule at severity `Error` in either report.

Warnings are NOT failures: the then-fielded 0x3F1A560F reports carry REQP-1934,
BUFC-1, PDRC-153, PLHOLDVIO-2 and DSP pipelining advisories on every config, and
a mode-1 debug RM adds RTSTAT-10 (unused BSCAN legs such as drck/capture have no
routable load), PDCN-1569 and CFGBVS-1. A report that does not parse as a
report_drc output FAILS -- an unreadable report is not a clean one.

    check_hdpr_reports.py --build-dir <prod>                 # every report there
    check_hdpr_reports.py --build-dir <prod> --rm rm_dbg_demo [--stage prelink|routed]
    check_hdpr_reports.py <report.rpt> ...                   # explicit files

Called by fpga/dfx/build_dfx.tcl right after each report is written (so the mint
stops at the config that broke), and runnable standalone. Python 3.8, stdlib.
Exit 0 pass, 1 fail, 2 usage / nothing to check.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

#: Rules that fail at any severity.
FATAL_RULES = ("HDPR-16", "HDPR-18", "HDPR-50")

#: A summary-table row: | RULE | Severity | Description | N |
_ROW = re.compile(r"^\|\s*([A-Z][A-Z0-9]*-\d+)\s*\|\s*([A-Za-z][A-Za-z ]*?)\s*\|(.*)\|\s*(\d+)\s*\|\s*$")
_FOUND = re.compile(r"Violations found:\s*(\d+)")

_WHY = {
    "HDPR-16": "a debug core in the RP has no RM-side hub (ILA => mode-1 bridge)",
    "HDPR-18": "a clock/BSCAN site pulled into the RP pblock (no BUFG may live in the RP)",
    "HDPR-50": "the RP pblock now overlaps the shell's I/O column",
}


def parse_report(path):
    """-> (rows, errors). rows = [(rule, severity, count)]."""
    try:
        with open(path, errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return [], ["%s: cannot read: %s" % (path, exc)]
    if "Report DRC" not in text or "REPORT SUMMARY" not in text:
        return [], ["%s: not a report_drc output (no 'Report DRC' / 'REPORT SUMMARY')" % path]
    rows = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if m:
            rows.append((m.group(1), m.group(2), int(m.group(4))))
    errors = []
    found = _FOUND.search(text)
    if found is not None:
        total = int(found.group(1))
        listed = sum(n for _, _, n in rows)
        if total != listed:
            errors.append("%s: 'Violations found: %d' but the summary table lists %d -- "
                          "report format not understood" % (path, total, listed))
    return rows, errors


def judge(path):
    rows, errors = parse_report(path)
    fails = list(errors)
    for rule, sev, n in rows:
        if rule in FATAL_RULES:
            fails.append("%s: %s (%s) x%d -- %s" % (path, rule, sev, n, _WHY[rule]))
        elif sev.lower() == "error":
            fails.append("%s: %s is a DRC Error x%d" % (path, rule, n))
    return rows, fails


def reports_in(build_dir, rm=None, stage=None):
    if rm:
        pats = []
        if stage in (None, "prelink"):
            pats.append("drc_hdpr_%s_prelink.rpt" % rm)
        if stage in (None, "routed"):
            pats.append("drc_%s.rpt" % rm)
        return [os.path.join(build_dir, p) for p in pats]
    out = sorted(glob.glob(os.path.join(build_dir, "drc_hdpr_*_prelink.rpt")))
    out += sorted(glob.glob(os.path.join(build_dir, "drc_rm_*.rpt")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reports", nargs="*", help="explicit report files")
    ap.add_argument("--build-dir", help="a build_dfx.tcl out_dir (e.g. <BUILD>/prod)")
    ap.add_argument("--rm", help="only this rm_key's reports (needs --build-dir)")
    ap.add_argument("--stage", choices=("prelink", "routed"),
                    help="with --rm: only the prelink HDPR report, or only the routed one")
    args = ap.parse_args(argv)

    files = list(args.reports)
    if args.build_dir:
        files += reports_in(args.build_dir, args.rm, args.stage)
    elif args.rm:
        ap.error("--rm needs --build-dir")
    if not files:
        print("check_hdpr_reports: nothing to check (no reports given / found)")
        return 2

    failures = []
    checked = 0
    for f in files:
        if not os.path.isfile(f):
            failures.append("%s: MISSING (build_dfx.tcl writes it; a missing report "
                            "is not a clean one)" % f)
            continue
        rows, fails = judge(f)
        checked += 1
        failures += fails
        if not fails:
            seen = ", ".join("%s x%d" % (r, n) for r, s, n in rows) or "0 violations"
            print("  ok   %s  (%s)" % (os.path.basename(f), seen))
    if failures:
        for f in failures:
            print("DFX_HDPR_GATE_FAILED %s" % f)
        return 1
    print("DFX_HDPR_GATE_OK reports=%d (fatal: %s, or any Error)"
          % (checked, " ".join(FATAL_RULES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
