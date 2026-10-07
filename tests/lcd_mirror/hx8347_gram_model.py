"""hx8347_gram_model.py -- the GOLDEN MODEL of the HX8347-D GRAM as the harness
and the DUT drive it over the 8-bit 8080 bus, and of the LCDMIR block's view of
it (docs/planning/linux_lanes/LCD_MIRROR_FPGA.md §2.3-2.7, §4, §8.1).

It is the SPEC for two implementations, and both are checked against it on the
same vectors:
  * the RTL snooper, fpga/shell/ip/lcd_mirror/ (bench: tests/lcd_mirror/);
  * the §7 C port in harnessd's interim software mode (lane LCDMIR-SW).

It consumes DECODED bus events -- ``byte(rs, d)``, ``panel_reset()``, ``snap()``
-- not pad samples. Turning pad samples into those events (the WR_n rising
edge rule and the VIOL/RDS guards) is ``pad_decoder.py``'s job, deliberately a
separate file: the byte-level model must not know how strobes are timed.

Stdlib only. Nothing here reads RTL.

THE MODEL, in the order the doc states it
-----------------------------------------
Index byte (RS=0)      idx <- B; the pixel byte phase resets; B == 0x22 counts
                       RAMWR and, with ``ac_load=1`` (O1), loads AC <- (SC, SP).
Data byte, idx != 0x22 REGS[idx] <- D (the raw log, all 256 indices); decode
                       02/03 SC, 04/05 EC, 06/07 SP, 08/09 EP (9-bit: the high
                       register's bit 0 is bit 8), 16 MADCTL, 17 COLMOD, and the
                       recorded 01/1F/28/36; with ``ac_load=0`` a write to 02/03
                       loads AC.x <- SC and 06/07 loads AC.y <- SP.
Data byte, idx == 0x22 assemble a pixel -- COLMOD[2:0] == 6: three bytes (18 bpp,
                       6 bits each in D[7:2]) truncated to RGB565; anything
                       else: two bytes, HIGH byte first -- write it at AC, then
                       advance AC: x runs SC..EC then returns to SC with y+1;
                       after EP, y returns to SP and FRAMES++. (Equality tests,
                       9-bit wrap: a start beyond the end walks through 511.)
Viewer mapping         write-time, so the frame buffer is in viewer order.
                       exchange: MV ? (g0,s0)=(x,y) : (g0,s0)=(y,x)
                       flips (O2, ``flip_conv``): 0 = physical axes, MY flips g
                       and MX flips s; 1 = logical axes, MX flips x and MY flips
                       y (with MV set: MX flips g, MY flips s).
                       range: g0 <= 319 and s0 <= 239, else OOB (not written).
                       vx = g, vy = s.  0x20 -> identity, 0xE0 -> 180 degrees.
CLCD_RST               every decoded field returns to DEFAULTS (O3); VALID is
                       cleared; RESETS++; FB pixels, REGS, DIRTY and the
                       counters are kept.
SNAP                   snapshot <- live dirty map and bbox, SNAP_SEQ <- SEQ,
                       live maps cleared.
Dirty (CTRL[2])        ``dirty_all=0`` (the RTL reset, compare-on-write, HM
                       LCD_MIRROR.md §4.2): a tile/bbox is marked only when a
                       write CHANGES the stored pixel. ``dirty_all=1``: on every
                       in-range write (LCD_MIRROR_FPGA.md §2.7 as written).
                       VALID and SEQ count every in-range write either way.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

W, H = 320, 240
TILE = 16
TX, TY = W // TILE, H // TILE          # 20 x 15
NTILES = TX * TY                       # 300
FB_WORDS = W * H // 2                  # 38,400
IDX_RAMWR = 0x22

#: Register defaults after CLCD_RST -- open item O3. The window defaults are
#: the controller's portrait-native GRAM (240 columns x 320 rows), which is a
#: fact of the glass. MADCTL/COLMOD/R01/R1F/R28/R36 are ASSUMED until the Himax
#: datasheet is in hand; the RTL parameters RST_* state the same numbers and the
#: bench pins them (test_reset_defaults). Only a DUT that draws without
#: programming these registers can tell the difference.
DEFAULTS: Dict[str, int] = dict(
    sc=0, ec=239, sp=0, ep=319,
    r01=0x00, r16=0x00, r17=0x06, r1f=0x01, r28=0x00, r36=0x00,
)

BB_EMPTY = (0x1FF, 0x000)              # (min, max) of an empty box


def tile_of(vx: int, vy: int) -> int:
    return (vy // TILE) * TX + (vx // TILE)


def viewer_xy(x: int, y: int, madctl: int, flip_conv: int) -> Optional[Tuple[int, int]]:
    """Address counter (x, y) -> viewer (vx, vy), or None if outside GRAM."""
    mv = (madctl >> 5) & 1
    mx = (madctl >> 6) & 1
    my = (madctl >> 7) & 1
    g0, s0 = (x, y) if mv else (y, x)
    if g0 > W - 1 or s0 > H - 1:
        return None
    if flip_conv:
        flip_g, flip_s = (mx, my) if mv else (my, mx)
    else:
        flip_g, flip_s = my, mx
    g = (W - 1 - g0) if flip_g else g0
    s = (H - 1 - s0) if flip_s else s0
    return g, s


class GramModel:
    """Byte-level HX8347-D + LCDMIR model. See the module docstring."""

    def __init__(self, ac_load: int = 0, flip_conv: int = 0,
                 defaults: Dict[str, int] = None, dirty_all: int = 0):
        self.defaults = dict(DEFAULTS if defaults is None else defaults)
        self.ac_load = ac_load
        self.flip_conv = flip_conv
        self.dirty_all = dirty_all
        self.fb: List[int] = [0] * (W * H)       # viewer order, RGB565
        self.regs: List[int] = [0] * 256          # raw log, never reset
        # counters (32-bit, wrap)
        self.seq = self.frames = self.ramwr = self.resets = 0
        self.bytes = self.oob = 0
        # tile maps
        self.valid: set = set()
        self.dirty: set = set()
        self.snap_dirty: set = set()
        self.bbox = [BB_EMPTY[0], BB_EMPTY[1], BB_EMPTY[0], BB_EMPTY[1]]
        self.snap_bbox = list(self.bbox)
        self.snap_seq = 0
        self.oob_sticky = 0
        self._decoded_defaults()

    # ------------------------------------------------------------------ state
    def _decoded_defaults(self):
        d = self.defaults
        self.idx = 0
        self.phase = 0
        self.b0 = self.b1 = 0
        self.sc, self.ec, self.sp, self.ep = d["sc"], d["ec"], d["sp"], d["ep"]
        self.r01, self.r16, self.r17 = d["r01"], d["r16"], d["r17"]
        self.r1f, self.r28, self.r36 = d["r1f"], d["r28"], d["r36"]
        self.acx = self.acy = 0

    # ----------------------------------------------------------------- events
    def panel_reset(self):
        """CLCD_RST asserted (one pulse, whatever its length)."""
        self._decoded_defaults()
        self.valid.clear()
        self.resets = (self.resets + 1) & 0xFFFFFFFF

    def snap(self):
        self.snap_dirty = set(self.dirty)
        self.dirty = set()
        self.snap_bbox = list(self.bbox)
        self.bbox = [BB_EMPTY[0], BB_EMPTY[1], BB_EMPTY[0], BB_EMPTY[1]]
        self.snap_seq = self.seq

    def byte(self, rs: int, d: int):
        d &= 0xFF
        self.bytes = (self.bytes + 1) & 0xFFFFFFFF
        if not rs:
            self.idx = d
            self.phase = 0
            if d == IDX_RAMWR:
                self.ramwr = (self.ramwr + 1) & 0xFFFFFFFF
                if self.ac_load:
                    self.acx, self.acy = self.sc, self.sp
            return
        if self.idx != IDX_RAMWR:
            self._reg_write(self.idx, d)
            return
        fmt18 = (self.r17 & 0x7) == 6
        last = 2 if fmt18 else 1
        if self.phase < last:
            if self.phase == 0:
                self.b0 = d
            else:
                self.b1 = d
            self.phase += 1
            return
        self.phase = 0
        if fmt18:
            px = ((self.b0 >> 3) << 11) | ((self.b1 >> 2) << 5) | (d >> 3)
        else:
            px = (self.b0 << 8) | d
        self._write_pixel(self.acx, self.acy, px)
        self._advance()

    def feed(self, events: Iterable):
        """Apply a list of ('B', rs, d) / ('RST',) / ('SNAP',) /
        ('CTRL', ac_load, flip_conv) tuples."""
        for ev in events:
            k = ev[0]
            if k == "B":
                self.byte(ev[1], ev[2])
            elif k == "RST":
                self.panel_reset()
            elif k == "SNAP":
                self.snap()
            elif k == "CTRL":
                self.ac_load, self.flip_conv = ev[1], ev[2]
                if len(ev) > 3:
                    self.dirty_all = ev[3]
            else:
                raise ValueError(f"unknown model event {ev!r}")

    def feed_bytes(self, pairs: Iterable[Tuple[int, int]]):
        for rs, d in pairs:
            self.byte(rs, d)

    # -------------------------------------------------------------- internals
    def _reg_write(self, idx: int, d: int):
        self.regs[idx] = d
        if idx == 0x01:
            self.r01 = d
        elif idx == 0x02:
            self.sc = ((d & 1) << 8) | (self.sc & 0xFF)
            if not self.ac_load:
                self.acx = self.sc
        elif idx == 0x03:
            self.sc = (self.sc & 0x100) | d
            if not self.ac_load:
                self.acx = self.sc
        elif idx == 0x04:
            self.ec = ((d & 1) << 8) | (self.ec & 0xFF)
        elif idx == 0x05:
            self.ec = (self.ec & 0x100) | d
        elif idx == 0x06:
            self.sp = ((d & 1) << 8) | (self.sp & 0xFF)
            if not self.ac_load:
                self.acy = self.sp
        elif idx == 0x07:
            self.sp = (self.sp & 0x100) | d
            if not self.ac_load:
                self.acy = self.sp
        elif idx == 0x08:
            self.ep = ((d & 1) << 8) | (self.ep & 0xFF)
        elif idx == 0x09:
            self.ep = (self.ep & 0x100) | d
        elif idx == 0x16:
            self.r16 = d
        elif idx == 0x17:
            self.r17 = d
        elif idx == 0x1F:
            self.r1f = d
        elif idx == 0x28:
            self.r28 = d
        elif idx == 0x36:
            self.r36 = d

    def _advance(self):
        if self.acx == self.ec:
            self.acx = self.sc
            if self.acy == self.ep:
                self.acy = self.sp
                self.frames = (self.frames + 1) & 0xFFFFFFFF
            else:
                self.acy = (self.acy + 1) & 0x1FF
        else:
            self.acx = (self.acx + 1) & 0x1FF

    def _write_pixel(self, x: int, y: int, px: int):
        v = viewer_xy(x, y, self.r16, self.flip_conv)
        if v is None:
            self.oob = (self.oob + 1) & 0xFFFFFFFF
            self.oob_sticky = 1
            return
        vx, vy = v
        changed = self.fb[vy * W + vx] != px
        self.fb[vy * W + vx] = px
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        t = tile_of(vx, vy)
        self.valid.add(t)
        if not (self.dirty_all or changed):
            return
        self.dirty.add(t)
        b = self.bbox
        b[0] = min(b[0], vx)
        b[1] = max(b[1], vx)
        b[2] = min(b[2], vy)
        b[3] = max(b[3], vy)

    # ------------------------------------------------------------ the views
    def fb_words(self) -> List[int]:
        """The FB aperture as 38,400 32-bit words: even x in [15:0]."""
        f = self.fb
        return [f[2 * i] | (f[2 * i + 1] << 16) for i in range(FB_WORDS)]

    @staticmethod
    def map_words(tiles: set) -> List[int]:
        out = [0] * 10
        for t in tiles:
            out[t // 32] |= 1 << (t % 32)
        return out

    def display_on(self) -> int:
        return int((self.r28 & 0x3C) == 0x3C)

    def status(self, rst_n: int = 1, bl: int = 0, owner: int = 0,
               viol_sticky: int = 0, rds_sticky: int = 0, banked: int = 0) -> int:
        fmt = self.r17 & 0x7
        return ((rst_n & 1)
                | (bl & 1) << 1
                | (owner & 1) << 2
                | self.display_on() << 3
                | (self.r1f & 1) << 4
                | int(self.idx == IDX_RAMWR) << 5
                | int(fmt in (5, 6)) << 6
                | int(fmt == 6) << 7
                | (viol_sticky & 1) << 8
                | (self.oob_sticky & 1) << 9
                | (rds_sticky & 1) << 10
                | (banked & 1) << 15)

    def csrs(self, viol: int = 0, rds: int = 0, **status_kw) -> Dict[int, int]:
        """Expected value of every model-determined CSR, by byte offset."""
        viol_st = status_kw.pop("viol_sticky", int(viol != 0))
        rds_st = status_kw.pop("rds_sticky", int(rds != 0))
        out = {
            0x000: 0x4C43444D,
            0x004: 0x01001001,
            0x008: (H << 16) | W,
            0x00C: (self.dirty_all << 2) | (self.flip_conv << 1) | self.ac_load,
            0x010: self.status(viol_sticky=viol_st, rds_sticky=rds_st, **status_kw),
            0x014: self.seq,
            0x018: self.frames,
            0x01C: self.ramwr,
            0x020: self.resets,
            0x024: self.bytes,
            0x028: viol & 0xFFFFFFFF,
            0x02C: self.oob,
            0x030: rds & 0xFFFFFFFF,
            0x040: (self.ec << 16) | self.sc,
            0x044: (self.ep << 16) | self.sp,
            0x048: (self.acy << 16) | self.acx,
            0x04C: (self.r01 << 24) | (self.r36 << 16) | (self.r17 << 8) | self.r16,
            0x050: self.snap_seq,
            0x054: (self.snap_bbox[1] << 16) | self.snap_bbox[0],
            0x058: (self.snap_bbox[3] << 16) | self.snap_bbox[2],
        }
        for i, w in enumerate(self.map_words(self.snap_dirty)):
            out[0x080 + 4 * i] = w
        for i, w in enumerate(self.map_words(self.valid)):
            out[0x0C0 + 4 * i] = w
        for k in range(64):
            r = self.regs
            out[0x100 + 4 * k] = (r[4 * k] | r[4 * k + 1] << 8
                                  | r[4 * k + 2] << 16 | r[4 * k + 3] << 24)
        return out


# --------------------------------------------------------------------------- #
# Stream helpers shared by the bench and (as fixtures) by harnessd's C port.
# --------------------------------------------------------------------------- #
def window_pairs(x0: int, x1: int, y0: int, y1: int,
                 order: Sequence[int] = (0x02, 0x03, 0x04, 0x05,
                                         0x06, 0x07, 0x08, 0x09)
                 ) -> List[Tuple[int, int]]:
    """Index/datum pairs programming the window, in the given register order."""
    val = {0x02: x0 >> 8, 0x03: x0 & 0xFF, 0x04: x1 >> 8, 0x05: x1 & 0xFF,
           0x06: y0 >> 8, 0x07: y0 & 0xFF, 0x08: y1 >> 8, 0x09: y1 & 0xFF}
    out: List[Tuple[int, int]] = []
    for r in order:
        out += [(0, r), (1, val[r])]
    return out


def pixel_pairs(pixels: Iterable[int], fmt18: bool = False) -> List[Tuple[int, int]]:
    """RGB565 pixels -> data bytes, high byte first (18 bpp: R6/G6/B6 in D[7:2])."""
    out: List[Tuple[int, int]] = []
    for p in pixels:
        if fmt18:
            r5, g6, b5 = (p >> 11) & 0x1F, (p >> 5) & 0x3F, p & 0x1F
            out += [(1, (r5 << 3) | 0x4), (1, g6 << 2), (1, (b5 << 3) | 0x4)]
        else:
            out += [(1, (p >> 8) & 0xFF), (1, p & 0xFF)]
    return out
