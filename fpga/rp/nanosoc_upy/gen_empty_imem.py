#!/usr/bin/env python3
"""Emit an EMPTY word-hex IMEM image for the flash-boot (XiP) RM variant.

The stock rm_nanosoc_upy bakes firmware/micropython/build/micropython_word.hex
into IMEM via $readmemh — that is what makes it a BRAM scaffold. The flash-boot
variant bakes THIS all-zeros image instead, so a running MicroPython proves the
code came from FLASH, not from a preloaded BRAM. (Proven on silicon 2026-07-21
with a rigorous negative control; see docs/QSPI_RP_BOARD_BRINGUP.md.)

Format matches micropython_word.hex: a leading `@00000000` address directive
then one 32-bit hex word per line. Size defaults to the RM's IMEM depth
(IMEM_RAM_ADDR_W=17 -> 128 KB -> 32768 words); it is a synth-time preload, so
an image shorter than the BRAM is fine ($readmemh fills the rest with x, which
Vivado initialises to 0), but matching the depth keeps it unambiguous.

Usage:
    gen_empty_imem.py [--words N] [-o out.hex]      # default 32768 -> stdout/-o
"""
import argparse
import sys

DEFAULT_WORDS = 32768  # 128 KB / 4; matches IMEM_RAM_ADDR_W=17


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--words", type=int, default=DEFAULT_WORDS)
    ap.add_argument("-o", "--out", default=None, help="output file (default stdout)")
    a = ap.parse_args()
    if a.words <= 0:
        raise SystemExit("--words must be positive")

    fh = open(a.out, "w") if a.out else sys.stdout
    try:
        fh.write("@00000000\n")
        for _ in range(a.words):
            fh.write("00000000\n")
    finally:
        if a.out:
            fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
