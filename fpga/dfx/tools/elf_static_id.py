#!/usr/bin/env python3
"""elf_static_id.py -- the static_id a shell firmware ELF was BAKED FOR.

    python3 elf_static_id.py <shell_fw.elf> [--expect 0x3F1A560F] [--tools DIR]

Prints the value of `mps3_greybox_clearing_static_id` (the constant
gen_greybox_blob.py generates from fpga/dfx/overlay/greybox/manifest.json, the
one the running shell answers `ping` with and matches every pushed overlay
against) by reading it out of the ELF's .rodata with mb-nm + mb-objdump.
With --expect it exits 1 when the two disagree.

WHY THIS EXISTS (2026-09-16). Mint stage 6 bakes `$(FW_ELF)` into the greybox
base bitstream with updatemem. On 2026-09-15 the firmware sub-make wrote its
ELF somewhere else (the parent make had exported BUILD=, which overrides the
firmware Makefile's own output directory), `test -f $(FW_ELF)` found the
previous day's rc4.2 ELF sitting at that path, and updatemem baked THAT: a
firmware whose baked static_id was 0xA8C1C535, into a fabric whose static_id
is 0x3F1A560F. Every gate was green. Had it reached the SD, the shell would
have answered `ping` with the old id on the new fabric and config_agent would
have rejected every overlay pushed to it -- the exact hazard the BLOB_MANIFEST
comment in firmware/platform/Makefile describes, produced by the flow that
exists to prevent it. A bitstream cannot be asked which ELF is inside it; the
ELF can be asked which fabric it is for, and that is this script.

The read is done with the Vitis binutils rather than a Python ELF parser so
the answer comes from the same tools that produced the file and nothing new
is depended on. MicroBlaze here is little-endian (-mlittle-endian in the
firmware Makefile), so the four .rodata bytes are reversed.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

SYMBOL = "mps3_greybox_clearing_static_id"
DEFAULT_TOOLS = "/apps/Xilinx/Vitis/2024.1/gnu/microblaze/lin/bin"


def _run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def symbol_address(elf: str, tools: str) -> int:
    out = _run([os.path.join(tools, "mb-nm"), elf])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == SYMBOL:
            return int(parts[0], 16)
    raise SystemExit("%s: no symbol %s (not a shell firmware ELF, or built "
                     "without the greybox blob)" % (elf, SYMBOL))


def baked_static_id(elf: str, tools: str = DEFAULT_TOOLS) -> int:
    """The uint32 at `mps3_greybox_clearing_static_id`, host-endian corrected."""
    addr = symbol_address(elf, tools)
    out = _run([os.path.join(tools, "mb-objdump"), "-s", "-j", ".rodata",
                "--start-address=0x%x" % addr,
                "--stop-address=0x%x" % (addr + 4), elf])
    # objdump -s line: " 1dba4 0f561a3f   .V.?" -- address then the bytes in
    # memory order. Little-endian target: reverse the four bytes.
    m = re.search(r"^\s*%x\s+([0-9a-fA-F]{8})" % addr, out, re.MULTILINE)
    if not m:
        raise SystemExit("%s: could not read 4 bytes of .rodata at 0x%x:\n%s"
                         % (elf, addr, out))
    raw = bytes.fromhex(m.group(1))
    return int.from_bytes(raw, "little")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("elf")
    ap.add_argument("--expect", default=None,
                    help="static_id (0x...) the ELF must be baked for; "
                         "exit 1 otherwise")
    ap.add_argument("--tools", default=DEFAULT_TOOLS,
                    help="directory holding mb-nm and mb-objdump")
    args = ap.parse_args(argv)
    got = baked_static_id(args.elf, args.tools)
    print("0x%08X" % got)
    if args.expect is not None:
        want = int(args.expect, 16)
        if got != want:
            print("error: %s is baked for static_id 0x%08X, the fabric is "
                  "0x%08X -- a shell built from this ELF would answer ping "
                  "with the wrong id and reject every overlay"
                  % (args.elf, got, want), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
