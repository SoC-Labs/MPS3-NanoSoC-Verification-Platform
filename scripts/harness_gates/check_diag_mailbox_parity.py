#!/usr/bin/env python3
"""Tier-0 gate: the JTAG diag-mailbox address the tooling reads must match the
address the firmware LMB size puts it at.

CATCHES: bug #4 (a hardware change moved a firmware address). Growing the LMB
256 KB -> 512 KB moved the diag mailbox 0x0003FF80 -> 0x0007FF80. Every xsdb
script hardcoded the old address; worse, the LMB decode ALIASES, so reading the
new address on an old shell silently returns the old mailbox. scripts/mps3_diag.tcl
scans a CANDIDATES list by magic instead of hardcoding -- this gate verifies that
list still CONTAINS the address implied by the CURRENT firmware LMB_KB, so a future
LMB change that nobody reflected in the scanner fails here, cheaply, not on the
bench with a garbage readout.

The mailbox base is TOP-anchored (firmware/platform/lscript.ld.in):
    base = 0x50 + (LMB_KB*1024 - 0x50) - DIAG_RESERVE  =  LMB_KB*1024 - DIAG_RESERVE
with DIAG_RESERVE the `.mps3_diag` reservation (0x100 at diag v8).

THE MICROBLAZE V (Linux harness, 2026-09-23). The same anchor formula at the
MBV's LMB size, which SHELL's fpga/shell/bd/cpu_mbv.tcl owns (local_ram
Write_Depth_A x 4 B, and the LMB `-range` in its `addr` stage -- the two must
agree). THREE consumers restate the resulting address and each is held to it:
scripts/mps3_diag.tcl `CANDIDATES_MBV`, the MBV tier-3 gate's `MBV_MAILBOX`,
and pyverify.mailbox's `MBV_LMB_KB`. A reader that declares an MBV anchor while
no cpu_mbv.tcl exists FAILS -- that is drift, not something to skip.

Pure stdlib, seconds, no tools.

    python3 check_diag_mailbox_parity.py [--repo ROOT]
"""
from __future__ import annotations

import argparse
import os
import re
import sys


def _read(p: str) -> str:
    return open(p).read() if os.path.isfile(p) else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()
    root = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    plat_mk = os.path.join(root, "firmware", "platform", "Makefile")
    lds = os.path.join(root, "firmware", "platform", "lscript.ld.in")
    diag_tcl = os.path.join(root, "scripts", "mps3_diag.tcl")
    bd_tcl = os.path.join(root, "fpga", "shell", "bd", "shell_bd.tcl")

    # LMB_KB default from the platform Makefile (`LMB_KB ?= 256`).
    m = re.search(r"^\s*LMB_KB\s*[:?]?=\s*(\d+)", _read(plat_mk), re.M)
    if not m:
        print("FAIL: could not read LMB_KB from %s" % plat_mk, file=sys.stderr)
        return 1
    lmb_kb = int(m.group(1))

    # `.mps3_diag` reservation from the linker template
    #   local_lmb : ORIGIN = 0x50, LENGTH = @LMB_LENGTH@ - 0x80
    r = re.search(r"@LMB_LENGTH@\s*-\s*(0x[0-9a-fA-F]+)", _read(lds))
    reserve = int(r.group(1), 16) if r else 0x80

    base = lmb_kb * 1024 - reserve
    expect = "0x%08X" % base

    tcl = _read(diag_tcl)
    cand_m = re.search(r"set\s+CANDIDATES\s+\[list([^\]]*)\]", tcl)
    candidates = re.findall(r"0x[0-9a-fA-F]+", cand_m.group(1)) if cand_m else []
    cand_norm = {int(c, 16) for c in candidates}

    print("== Tier-0 diag-mailbox address parity (bug #4) ==")
    print("   firmware LMB_KB = %d  ->  mailbox base = %s (LMB_KB*1024 - 0x%X)"
          % (lmb_kb, expect, reserve))
    print("   mps3_diag.tcl CANDIDATES = %s" % (", ".join(candidates) or "<none>"))

    if base not in cand_norm:
        print("\nFAIL: the current firmware puts the diag mailbox at %s, but the "
              "JTAG scanner's CANDIDATES list does not include it." % expect)
        print("      A JTAG read would hit a stale/aliased address (bug #4). Add %s "
              "to scripts/mps3_diag.tcl CANDIDATES." % expect)
        return 1

    # Advisory: any OTHER script hardcoding a single mailbox literal is the
    # anti-pattern bug #4 came from -- scan by magic instead. Warn, don't fail.
    lit_rx = re.compile(r"0x000[37]FF80", re.I)
    warns = []
    scripts_dir = os.path.join(root, "scripts")
    gates_dir = os.path.join(scripts_dir, "harness_gates")
    for dp, _dn, fns in os.walk(scripts_dir):
        if os.path.abspath(dp).startswith(os.path.abspath(gates_dir)):
            continue  # the gates themselves reference the literal by design
        for fn in fns:
            if not fn.endswith((".tcl", ".py", ".sh")):
                continue
            fp = os.path.join(dp, fn)
            if os.path.samefile(fp, diag_tcl):
                continue
            if lit_rx.search(_read(fp)):
                warns.append(os.path.relpath(fp, root))
    for w in warns:
        print("   WARN: %s hardcodes a diag-mailbox literal -- prefer the "
              "magic-scan in mps3_diag.tcl (bug #4 class)." % w)

    # LMB lockstep advisory (bug #4 class): the shell BRAM the BD instantiates
    # (blk_mem_gen Write_Depth_A words x 4 B) must equal the firmware's LMB_KB,
    # or the firmware links to a different top-of-LMB than the hardware provides
    # and the mailbox anchor (and every hardcoded xsdb address) drifts. The BD
    # and firmware default disagree today (a documented in-flight 256->512
    # migration -- shell_bd.tcl's "FIRMWARE LOCKSTEP" note). Advisory here (the
    # shipping shell/firmware PAIR is chosen at build time); the Tier-2 gate turns
    # this hard by comparing the actual DCP's BRAM against the actual ELF.
    # After the CPU seam (SHELL, 2026-09-23) the classic CPU block and its
    # local_ram live in cpu_mb.tcl; before it, in shell_bd.tcl itself.
    cpu_mb = os.path.join(root, "fpga", "shell", "bd", "cpu_mb.tcl")
    dm = re.search(r"Write_Depth_A\s*\{(\d+)\}", _read(cpu_mb)) or \
        re.search(r"Write_Depth_A\s*\{(\d+)\}", _read(bd_tcl))
    if dm:
        bd_kb = int(dm.group(1)) * 4 // 1024
        if bd_kb != lmb_kb:
            print("   WARN: shell_bd.tcl BRAM = %d KiB but firmware LMB_KB = %d KiB "
                  "-- shell/firmware LMB out of lockstep (bug #4 class). Confirm the "
                  "shipping PAIR agrees before flashing." % (bd_kb, lmb_kb))
        else:
            print("   OK  shell BRAM (%d KiB) == firmware LMB_KB (%d KiB)" % (bd_kb, lmb_kb))

    rc = _check_mbv(root, tcl, reserve)
    if rc:
        return rc
    print("\nOK: mailbox address %s is covered by the magic-scan CANDIDATES" % expect)
    return 0


def _check_mbv(root: str, diag_tcl_text: str, reserve: int) -> int:
    """The MicroBlaze V half: derive the anchor from cpu_mbv.tcl and hold the
    three consumers to it. Returns a process exit code (0 = OK)."""
    cpu_mbv = os.path.join(root, "fpga", "shell", "bd", "cpu_mbv.tcl")
    mbv_tcl = os.path.join(root, "scripts", "harness_gates", "tier3_csr_liveness_mbv.tcl")
    pv_mbx = os.path.join(root, "host", "pyverify", "pyverify", "mailbox.py")
    cand_m = re.search(r"set\s+CANDIDATES_MBV\s+\[list([^\]]*)\]", diag_tcl_text)
    if not os.path.isfile(cpu_mbv):
        if cand_m:
            print("\nFAIL: scripts/mps3_diag.tcl declares CANDIDATES_MBV but there is no "
                  "fpga/shell/bd/cpu_mbv.tcl to derive the MicroBlaze V LMB size from.")
            return 1
        print("   (no MicroBlaze V variant in this tree: fpga/shell/bd/cpu_mbv.tcl absent)")
        return 0
    text = _read(cpu_mbv)
    depth = re.search(r"Write_Depth_A\s*\{(\d+)\}", text)
    rng = re.search(r"-range\s+(\d+)K\s+\[get_bd_addr_segs\s+\{dlmb_bram_if_cntlr", text)
    if not depth:
        print("\nFAIL: cannot read local_ram Write_Depth_A from %s" % cpu_mbv)
        return 1
    kb = int(depth.group(1)) * 4 // 1024
    if rng and int(rng.group(1)) != kb:
        print("\nFAIL: cpu_mbv.tcl's LMB BRAM is %d KiB but its DLMB address range is %sK"
              % (kb, rng.group(1)))
        return 1
    anchor = kb * 1024 - reserve
    print("   MicroBlaze V: cpu_mbv.tcl LMB = %d KiB  ->  mailbox base = 0x%08X" % (kb, anchor))
    bad = []
    cands = {int(c, 16) for c in re.findall(r"0x[0-9a-fA-F]+", cand_m.group(1))} if cand_m else set()
    if anchor not in cands:
        bad.append("scripts/mps3_diag.tcl CANDIDATES_MBV %s" % (sorted(hex(c) for c in cands) or "<none>"))
    m = re.search(r"set\s+MBV_MAILBOX\s+(0x[0-9a-fA-F]+)", _read(mbv_tcl))
    if not m or int(m.group(1), 16) != anchor:
        bad.append("scripts/harness_gates/tier3_csr_liveness_mbv.tcl MBV_MAILBOX %s"
                   % (m.group(1) if m else "<missing>"))
    m = re.search(r"^MBV_LMB_KB\s*=\s*(\d+)", _read(pv_mbx), re.M)
    if not m or int(m.group(1)) != kb:
        bad.append("host/pyverify/pyverify/mailbox.py MBV_LMB_KB %s" % (m.group(1) if m else "<missing>"))
    if bad:
        print("\nFAIL: the MicroBlaze V mailbox is at 0x%08X (%d KiB LMB), but:" % (anchor, kb))
        for b in bad:
            print("      %s" % b)
        return 1
    print("   OK  MicroBlaze V anchor 0x%08X agrees across mps3_diag.tcl, the MBV tier-3 "
          "gate and pyverify" % anchor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
