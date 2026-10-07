#!/usr/bin/env python3
"""Tier-0 gate: the packaged-IP staleness guard must be present, AND (advisory)
no packaged CSR IP may be older than its RTL source.

CATCHES: bug #2 (stale packaged IP). The shell synthesizes from
fpga/shell/ip_packaged/, not fpga/shell/ip/. package_csr_ip.tcl used to skip
repackaging whenever component.xml existed, so an RTL edit was silently dropped:
a fresh static_id for a bitstream whose RTL had not changed. The fix is an mtime
staleness check. This gate:
  (1) HARD-fails if that mtime guard has been removed from package_csr_ip.tcl
      (i.e. a revert to the unconditional skip that shipped bug #2), and
  (2) reports (advisory) any ip_packaged/<name>/component.xml currently OLDER than
      its source .sv -- a rebuild MUST repackage it. This is advisory because the
      source is often edited between builds; the Tier-2 artifact gate turns the
      same check HARD after a build (the shipped DCP's IP must not be stale).

Pure stdlib, seconds, no tools.

    python3 check_packaged_ip_fresh.py [--repo ROOT]
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

BLOCKS = {  # packaged-name -> ip source subdir
    "dut_clkrst": "clkrst",
    "dfx_ctl": "dfx_ctl",
    "board_gpio": "board_gpio",
    "swd_bb": "swd_bb",
    "telem": "telem",
    "uart_bridge": "uart_bridge",
    "usd_spi": "usd_spi",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()
    root = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    shell = os.path.join(root, "fpga", "shell")
    pkg_tcl = os.path.join(shell, "ip_packaged", "package_csr_ip.tcl")

    print("== Tier-0 packaged-IP freshness gate (bug #2) ==")

    # (1) the staleness guard must still be there.
    tcl = open(pkg_tcl).read() if os.path.isfile(pkg_tcl) else ""
    has_guard = bool(re.search(r"file\s+mtime", tcl)) and "STALE" in tcl.upper()
    if not has_guard:
        print("   VIOLATION  package_csr_ip.tcl has no mtime staleness guard "
              "(reverted to the unconditional skip that shipped bug #2).")
        print("\nFAIL: the repackage-when-source-newer guard is missing.")
        return 1
    print("   OK  package_csr_ip.tcl retains the mtime staleness guard")

    # (2) advisory: currently-stale packaged components.
    stale = []
    for name, subdir in BLOCKS.items():
        comp = os.path.join(shell, "ip_packaged", name, "component.xml")
        srcs = glob.glob(os.path.join(shell, "ip", subdir, "*.sv"))
        if not os.path.isfile(comp) or not srcs:
            continue
        cm = os.path.getmtime(comp)
        newer = [s for s in srcs if os.path.getmtime(s) > cm]
        if newer:
            stale.append((name, [os.path.basename(s) for s in newer]))
    for name, files in stale:
        print("   ADVISORY  %s is STALE: %s newer than component.xml -- the next "
              "shell build must repackage it (guard will)." % (name, ",".join(files)))
    if not stale:
        print("   OK  no packaged CSR IP is older than its source right now")

    print("\nOK: staleness guard present"
          + (" (%d block(s) currently stale -- advisory)" % len(stale) if stale else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
