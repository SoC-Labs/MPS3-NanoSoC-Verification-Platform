#!/usr/bin/env python3
"""check_budget.py -- the stage0 size gate (`make size-gate`).

Reads the GNU ld map file and the ELF symbol table of a stage0 link and
refuses it unless ALL of these hold (plan §3 LMB map, lane STAGE0 row):

  1. the stack top (_estack) is exactly 0x1FE00, the status block base;
  2. >= 48 KiB free between the end of the image (_end) and the bottom of the
     8 KiB stack (__stack_bottom);
  3. .data is empty (a watchdog warm restart does not reload BRAM);
  4. the rescue network stack (net + tftp + ident + rescue + eth glue + the
     smsc911x driver: .text + .rodata) is <= 24 KiB;
  5. no card-WRITE path is linked (stage0 is read-only on the card: the
     symbol usd_write_start must not survive --gc-sections);
  6. with --require-wdog-arm (the shipping build): s0_hw_wdog_arm IS linked,
     i.e. stage0 arms the watchdog before every hand-off, s0_hw_ddr_calib is
     linked (the DDR gate is compiled in, not CALIB=none), and s0_hw_usd_release
     is linked (usd_spi is left EN=0 for the kernel before the jump: D13 handover
     §12.2).

Stdlib only. `make size-gate` also links four negative controls (80 KiB of
padding; a caller of usd_write_start; a WDOG_ARM=0 build; a build whose hand-off
skips the usd_spi release) and requires this script to refuse each.
"""
import argparse
import os
import re
import struct
import sys

LMB_TOP = 0x1FE00
MIN_FREE = 48 * 1024
NET_MAX = 24 * 1024
NET_OBJS = {"stage0_net.o", "stage0_tftp.o", "stage0_ident.o", "stage0_rescue.o",
            "stage0_eth.o", "smsc911x.o"}
FORBIDDEN = ("usd_write_start",)
REQUIRED_SHIPPING = ("s0_hw_wdog_arm", "s0_hw_ddr_calib", "s0_hw_usd_release")

SYM_RE = re.compile(r"^\s+0x([0-9a-fA-F]+)\s+([A-Za-z_.$][\w.$]*)\s+=")
OUT_RE = re.compile(r"^(\.[\w.]+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)")
IN_FULL_RE = re.compile(r"^ (\.[\w.$]+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+(\S+)$")
IN_NAME_RE = re.compile(r"^ (\.[\w.$]+)$")
IN_ADDR_RE = re.compile(r"^\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+(\S+)$")


def parse_map(path):
    syms, outs, contrib = {}, {}, {}
    active = False
    pending = None
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("Linker script and memory map"):
                active = True
                continue
            if not active:
                continue          # skip "Discarded input sections"
            m = SYM_RE.match(line)
            if m:
                syms[m.group(2)] = int(m.group(1), 16)
                continue
            m = OUT_RE.match(line)
            if m:
                outs[m.group(1)] = (int(m.group(2), 16), int(m.group(3), 16))
                continue
            m = IN_FULL_RE.match(line)
            if m:
                contrib.setdefault(os.path.basename(m.group(4)), []).append(
                    (m.group(1), int(m.group(3), 16)))
                pending = None
                continue
            m = IN_NAME_RE.match(line)
            if m:
                pending = m.group(1)
                continue
            m = IN_ADDR_RE.match(line)
            if m and pending:
                contrib.setdefault(os.path.basename(m.group(3)), []).append(
                    (pending, int(m.group(2), 16)))
            pending = None
    return syms, outs, contrib


def elf_symbols(path):
    data = open(path, "rb").read()
    if data[:4] != b"\x7fELF" or data[4] != 1:
        raise ValueError("%s: not an ELF32" % path)
    e_shoff, = struct.unpack_from("<I", data, 32)
    e_shentsize, e_shnum = struct.unpack_from("<HH", data, 46)
    secs = [struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize)
            for i in range(e_shnum)]
    names = set()
    for sh in secs:
        if sh[1] != 2 or not sh[9]:          # SHT_SYMTAB
            continue
        stroff = secs[sh[6]][4]
        for j in range(sh[5] // sh[9]):
            st_name = struct.unpack_from("<I", data, sh[4] + j * sh[9])[0]
            if st_name:
                end = data.index(b"\0", stroff + st_name)
                names.add(data[stroff + st_name:end].decode("latin-1"))
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", required=True)
    ap.add_argument("--elf", required=True)
    ap.add_argument("--require-wdog-arm", action="store_true",
                    help="the shipping build: the watchdog arm, the DDR gate and the "
                         "usd_spi hand-over must be linked")
    ap.add_argument("--report-only", action="store_true",
                    help="print the budget, never fail (the plain `make` build)")
    a = ap.parse_args()

    syms, outs, contrib = parse_map(a.map)
    problems = []
    for need in ("_end", "__stack_bottom", "_estack"):
        if need not in syms:
            problems.append("map has no %s assignment (not a stage0.ld link?)" % need)
    if problems:
        for p in problems:
            print("SIZE GATE FAIL: " + p)
        return 0 if a.report_only else 1

    end, bottom, top = syms["_end"], syms["__stack_bottom"], syms["_estack"]
    free = bottom - end
    data_size = outs.get(".data", (0, 0))[1]
    net = sum(sz for obj, lst in contrib.items() if obj in NET_OBJS
              for sec, sz in lst if sec.startswith((".text", ".rodata", ".srodata")))
    per_obj = {obj: sum(sz for sec, sz in lst if sec.startswith((".text", ".rodata", ".srodata")))
               for obj, lst in contrib.items()}
    elf_syms = elf_symbols(a.elf)

    print("stage0 budget (%s):" % os.path.basename(a.elf))
    print("  image end  0x%05X (%d B)   stack %d B @ 0x%05X..0x%05X"
          % (end, end, top - bottom, bottom, top))
    print("  free       %d B (%.1f KiB)   rule: >= %d KiB" % (free, free / 1024.0, MIN_FREE // 1024))
    print("  .data      %d B               rule: 0" % data_size)
    print("  net stack  %d B (%.1f KiB)   rule: <= %d KiB  [%s]"
          % (net, net / 1024.0, NET_MAX // 1024,
             ", ".join("%s %d" % (o, per_obj[o]) for o in sorted(per_obj) if o in NET_OBJS)))

    if top != LMB_TOP:
        problems.append("stack top _estack = 0x%X, not 0x%X (the status block base)" % (top, LMB_TOP))
    if free < MIN_FREE:
        problems.append("only %d B free below the stack; the plan requires >= %d" % (free, MIN_FREE))
    if data_size:
        problems.append(".data is %d B: an initialised variable is not re-initialised "
                        "by a watchdog warm restart" % data_size)
    if net > NET_MAX:
        problems.append("the rescue network stack is %d B > %d" % (net, NET_MAX))
    for f in FORBIDDEN:
        if f in elf_syms:
            problems.append("%s is linked: stage0 must be READ-ONLY on the card "
                            "(plan §10a S5)" % f)
    if a.require_wdog_arm:
        for r in REQUIRED_SHIPPING:
            if r not in elf_syms:
                problems.append("%s is NOT linked: the shipping stage0 must arm the watchdog "
                                "before the hand-off (WDOG_ARM=1), gate on DDR calibration "
                                "(CALIB=telem) and hand usd_spi to the kernel with EN=0" % r)
    for p in problems:
        print("SIZE GATE FAIL: " + p)
    if problems and not a.report_only:
        return 1
    if not problems:
        print("  all budget rules hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
