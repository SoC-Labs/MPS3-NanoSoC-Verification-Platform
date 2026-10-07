"""card_model.py -- an INDEPENDENT statement of what the clcd_demo RM must draw,
and of the whole byte sequence it must emit for one frame.

This is the bench's spec, written from `fpga/rp/clcd_demo/clcd_demo_gen.sv`'s
DESCRIPTION rather than from its implementation: the layout rules ("a white
frame one border in from every edge; eight colour bars; a 16-cell binary counter
along the bottom, MSB left") re-derived in Python. If the RTL and this file
disagree, one of them is wrong, and that is the point of having both.

What is NOT re-derived here is the panel init table and the address-window
values: those come out of `firmware/clcd/hx8347_init.c` through
`fpga/rp/clcd_demo/hx8347_table.py`, the SAME parser the RTL's ROM generator
uses. Retyping 124 register writes into a bench would just be a second way to
get them wrong.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Dict, List, Sequence, Tuple

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_ROOT / "fpga" / "rp" / "clcd_demo"))
import hx8347_table as fw  # noqa: E402

from tunnel_model import RS_CMD, RS_DATA  # noqa: E402

# RGB565, in the order the card paints them left to right. Chosen so a BGR
# mix-up swaps yellow<->cyan AND red<->blue -- unmistakable in a photograph.
C_WHITE, C_YELLOW, C_CYAN, C_GREEN = 0xFFFF, 0xFFE0, 0x07FF, 0x07E0
C_MAGENTA, C_RED, C_BLUE, C_BLACK = 0xF81F, 0xF800, 0x001F, 0x0000
C_GREY = 0x4208
BARS = (C_WHITE, C_YELLOW, C_CYAN, C_GREEN, C_MAGENTA, C_RED, C_BLUE, C_BLACK)

NBARS = 8
NCELL = 16          # == the width of the frame counter, in bits


class Layout:
    """The elaboration-time constants the RTL derives from W/H."""

    def __init__(self, w: int, h: int, border: int = 0):
        self.w, self.h = w, h
        self.b = border if border else (4 if h >= 64 else 1)
        self.seph = 4 if h >= 64 else 1
        self.strip_top = (h * 3) // 4
        self.sep_top = self.strip_top - self.seph
        self.gut = 2 if w >= 128 else 0

    def cell(self, x: int) -> Tuple[int, int, int]:
        """-> (index, lo, hi) of the counter cell containing column x."""
        k = min(NCELL - 1, (x * NCELL) // self.w)
        while k + 1 < NCELL and x >= (self.w * (k + 1)) // NCELL:
            k += 1
        return k, (self.w * k) // NCELL, (self.w * (k + 1)) // NCELL

    def bar(self, x: int) -> int:
        k = 0
        for j in range(1, NBARS):
            if x >= (self.w * j) // NBARS:
                k = j
        return k


def card_pixel(x: int, y: int, lay: Layout, frame: int) -> int:
    """RGB565 for one pixel. Priority: frame > counter strip > separator > bars."""
    if x < lay.b or x >= lay.w - lay.b or y < lay.b or y >= lay.h - lay.b:
        return C_WHITE
    if y >= lay.strip_top:
        k, lo, hi = lay.cell(x)
        if x < lo + lay.gut or x >= hi - lay.gut:
            return C_GREY
        return C_WHITE if (frame >> (15 - k)) & 1 else C_BLACK
    if y >= lay.sep_top:
        return C_GREY
    return BARS[lay.bar(x)]


def window_value(reg: int, w: int, h: int) -> int:
    """The datum the RM writes to an address-window register, for a W x H frame.

    The END registers carry the last addressable column/row (the panel counts
    from zero); the START registers put the cursor at the origin.
    """
    return {
        0x08: (h - 1) >> 8, 0x09: (h - 1) & 0xFF,
        0x04: (w - 1) >> 8, 0x05: (w - 1) & 0xFF,
    }.get(reg, 0x00)


def init_sequence(table, defines: Dict[str, int]) -> List[Tuple[int, int]]:
    """The firmware init table as (rs, byte) pairs. HX_DLY entries emit no byte."""
    out = []
    for e in table:
        if e.op == defines["HX_DLY"]:
            continue
        out.append((RS_CMD if e.op == defines["HX_CMD"] else RS_DATA, e.val))
    return out


def window_sequence(w: int, h: int) -> List[Tuple[int, int]]:
    """Address-window program: each register index then its datum, then the
    GRAM-write command. The register list and its ORDER are the firmware's."""
    out: List[Tuple[int, int]] = []
    for reg in fw.WINDOW_REGS:
        out.append((RS_CMD, reg))
        out.append((RS_DATA, window_value(reg, w, h)))
    out.append((RS_CMD, fw.GRAM_WRITE_CMD))
    return out


def pixel_sequence(lay: Layout, frame: int) -> List[Tuple[int, int]]:
    """The GRAM stream for one frame: raster order, HIGH byte of each RGB565
    pixel first (firmware/clcd/hx8347_init.c, PANEL_PROVENANCE.md Q3/Q6)."""
    out: List[Tuple[int, int]] = []
    for y in range(lay.h):
        for x in range(lay.w):
            c = card_pixel(x, y, lay, frame)
            out.append((RS_DATA, (c >> 8) & 0xFF))
            out.append((RS_DATA, c & 0xFF))
    return out


def frame_sequence(table, defines, w: int, h: int, frame: int,
                   border: int = 0) -> List[Tuple[int, int]]:
    """One whole repaint cycle: re-init, re-window, repaint.

    The re-init is not belt-and-braces: every KVM handover HARD-RESETS the panel
    (clcd_kvm README section 10) and there is no grant-back wire telling the DUT
    it was granted (docs/contracts/dut-display-tunnel.md section 6), so an
    unconditional periodic re-init is what makes the picture converge within one
    frame of a handover the RM never saw.
    """
    lay = Layout(w, h, border)
    return (init_sequence(table, defines)
            + window_sequence(w, h)
            + pixel_sequence(lay, frame))


def load_firmware_table(repo_root: pathlib.Path = None):
    return fw.load(repo_root or _ROOT)


def diff_sequences(got: Sequence[Tuple[int, int]],
                   want: Sequence[Tuple[int, int]], limit: int = 6) -> str:
    """A readable first-divergence report -- 30k-entry list diffs are useless."""
    names = {RS_CMD: "CMD", RS_DATA: "DAT"}
    lines = []
    for i in range(min(len(got), len(want))):
        if got[i] != want[i]:
            lines.append(f"  [{i}] got {names[got[i][0]]} 0x{got[i][1]:02X}  "
                         f"want {names[want[i][0]]} 0x{want[i][1]:02X}")
            if len(lines) >= limit:
                break
    if len(got) != len(want):
        lines.append(f"  length: got {len(got)}, want {len(want)}")
    return "\n".join(lines) or "  (sequences agree)"
