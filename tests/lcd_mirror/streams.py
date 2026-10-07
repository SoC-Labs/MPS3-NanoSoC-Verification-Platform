"""streams.py -- the recorded 8080 streams the LCDMIR bench replays, and the
INDEPENDENT oracles it checks the frame buffer against
(docs/planning/linux_lanes/LCD_MIRROR_FPGA.md §8.3-8.4).

  * The HARNESS stream is recorded from the real firmware/clcd/clcd.c through
    mock registers (tools/clcd_stream_dump.c, built and run here with the host
    gcc). Its oracle is the driver's own cell SHADOW, font-rendered here from
    firmware/clcd/font8x16.h with this file's own cell placement and colours
    -- it never sees the byte stream, so it cannot share a decoding mistake
    with the golden model or the RTL.
  * The clcd_demo stream comes from tests/clcd_demo/card_model.py (the init
    table read out of the firmware by the SAME parser the RM's ROM generator
    uses) and its oracle is card_model.card_pixel().

Stdlib only.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = pathlib.Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
FW = ROOT / "firmware"

W, H = 320, 240
COLS, ROWS = 40, 15
GLYPH_W, GLYPH_H = 8, 16
FG = 0xFFFF          # firmware/clcd/clcd.c CLCD_RGB565_WHITE
BG = 0x0000          # CLCD_RGB565_BLACK
BG_INV = 0xF800      # CLCD_RGB565_RED (inverted rows)


# --------------------------------------------------------------------------- #
# The harness: record, parse, render.
# --------------------------------------------------------------------------- #
def build_dump_tool(workdir: pathlib.Path) -> pathlib.Path:
    workdir.mkdir(parents=True, exist_ok=True)
    exe = workdir / "clcd_stream_dump"
    srcs = [_HERE / "tools" / "clcd_stream_dump.c", FW / "clcd" / "clcd.c",
            FW / "clcd" / "hx8347_init.c", FW / "test" / "mock_regs.c"]
    if exe.exists() and all(exe.stat().st_mtime >= s.stat().st_mtime for s in srcs):
        return exe
    cmd = ["gcc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror",
           "-O1", "-DMPS3_HAL_MOCK", "-DMPS3_HAS_CLCD", "-DMPS3_CLCD_TEST_HOOKS",
           f"-I{FW / 'common'}", f"-I{FW / 'test'}", *map(str, srcs), "-o", str(exe)]
    subprocess.run(cmd, check=True)
    return exe


class Scenario:
    def __init__(self, name: str):
        self.name = name
        self.events: List[tuple] = []           # ('B', rs, d) | ('CTRL', v)
        self.grid: List[int] = []                # 600 cell codes
        self.inv: List[int] = []                 # 15 row flags

    @property
    def nbytes(self) -> int:
        return sum(1 for e in self.events if e[0] == "B")


def record_harness(workdir: pathlib.Path) -> List[Scenario]:
    exe = build_dump_tool(workdir)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    (workdir / "harness_stream.txt").write_text(out)
    return parse_harness(out)


def parse_harness(text: str) -> List[Scenario]:
    scen: List[Scenario] = []
    cur: Optional[Scenario] = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        tag, _, rest = line.partition(" ")
        if tag == "SCEN":
            cur = Scenario(rest.strip())
            scen.append(cur)
        elif tag == "X":
            for tok in rest.split():
                cur.events.append(("B", int(tok[0]), int(tok[1:], 16)))
        elif tag == "CTRL":
            cur.events.append(("CTRL", int(rest, 16)))
        elif tag == "SHADOW":
            assert cur is not None and rest.strip() == cur.name
        elif tag == "ROW":
            r, inv, hx = rest.split()
            assert int(r) == len(cur.inv)
            cur.inv.append(int(inv))
            cur.grid += list(bytes.fromhex(hx))
        elif tag == "END":
            break
        else:
            raise ValueError(f"harness stream: unknown line {line!r}")
    for s in scen:
        assert len(s.grid) == COLS * ROWS and len(s.inv) == ROWS, s.name
    return scen


_FONT: Optional[List[List[int]]] = None


def load_font() -> List[List[int]]:
    """font8x16[ch-0x20][row] from firmware/clcd/font8x16.h (bit 7 = left)."""
    global _FONT
    if _FONT is None:
        src = (FW / "clcd" / "font8x16.h").read_text()
        body = src[src.index("font8x16[CLCD_FONT_COUNT][16]"):]
        rows = re.findall(r"\{\s*((?:0x[0-9a-fA-F]{2}\s*,\s*){15}0x[0-9a-fA-F]{2})\s*\}", body)
        _FONT = [[int(v, 16) for v in r.split(",")] for r in rows]
        assert len(_FONT) == 95, f"font8x16.h parsed {len(_FONT)} glyphs, want 95"
    return _FONT


def render_shadow(grid: Sequence[int], inv: Sequence[int]) -> List[int]:
    """The glass for a cell shadow, in viewer order (MADCTL 0x20 = identity)."""
    font = load_font()
    fb = [0] * (W * H)
    for r in range(ROWS):
        bg = BG_INV if inv[r] else BG
        for c in range(COLS):
            ch = grid[r * COLS + c]
            if not 0x20 <= ch <= 0x7E:
                ch = 0x20
            glyph = font[ch - 0x20]
            for yy in range(GLYPH_H):
                bits = glyph[yy]
                row = (r * GLYPH_H + yy) * W + c * GLYPH_W
                for xx in range(GLYPH_W):
                    fb[row + xx] = FG if bits & (0x80 >> xx) else bg
    return fb


# --------------------------------------------------------------------------- #
# clcd_demo: the test card, via tests/clcd_demo/card_model.py.
# --------------------------------------------------------------------------- #
def _card_model():
    p = str(ROOT / "tests" / "clcd_demo")
    if p not in sys.path:
        sys.path.insert(0, p)
    import card_model  # noqa: E402
    return card_model


def demo_frame(frame: int) -> Tuple[List[Tuple[int, int]], List[int]]:
    """(the RM's whole repaint byte stream, its card_pixel oracle) at 320x240."""
    cm = _card_model()
    table, defines = cm.load_firmware_table()
    w, h = cm.fw.frame_geometry(table, defines)
    assert (w, h) == (W, H), (w, h)
    seq = cm.frame_sequence(table, defines, w, h, frame)
    lay = cm.Layout(w, h)
    oracle = [cm.card_pixel(x, y, lay, frame) for y in range(h) for x in range(w)]
    return list(seq), oracle


def init_pairs() -> List[Tuple[int, int]]:
    cm = _card_model()
    table, defines = cm.load_firmware_table()
    return list(cm.init_sequence(table, defines))


def work_dir() -> pathlib.Path:
    d = os.environ.get("LCDMIR_WORK")
    p = pathlib.Path(d) if d else (_HERE / "sim_build_py")
    p.mkdir(parents=True, exist_ok=True)
    return p
