"""lcdmirror_client.py -- the REFERENCE CLIENT for the LCD mirror's 6940 wire
(net-protocol.md "LCD mirror (TCP 6940)": LCD_MIRROR_FPGA.md §6.2 + its six
amendments + Harness Manager's H1/H3, in HM's §6.1 byte-level reading), plus
the font oracles the harnessd tests judge the mirror by: ocr() for today's
white/black/red screen, ocr_cells() for the aligned one (HM's palette + status
glyphs 0x80-0x86, harnessd's default).

Stdlib only, written from the contract text -- not from lcdmirror_main.c:

    c = MirrorClient(port)            # HELLO parsed, or Refused(line) raised
    c.send_key()                      # amendment 6: KEY on start
    c.pump_until(lambda c, u: u.snap_last and c.valid_count() == 300)
    c.frame[y * 320 + x]              # RGB565, viewer order, as of the last snap_last
    c.send_rate(10); c.send_ping(7)

Every message: 'L' 'M' u8 type, u8 rsvd, u32 len (LE), then len bytes; HELLO's
max_msg bounds the whole message, header included. A refused connection gets
ONE JSON line instead of HELLO, then EOF. The client ACKs every UPDATE by
default (H1: the board keeps at most 2 unACKed); ack=False turns that off.
"""
from __future__ import annotations

import json
import re
import socket
import struct
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

W, H, TILE, TX, TY, NTILES = 320, 240, 16, 20, 15, 300
HDR = struct.Struct("<2sBBI")            # 'L','M', type, rsvd, len
UPD = struct.Struct("<IIIIIB38s")        # seq t_ms frames resets status owner valid[38]
TREC = struct.Struct("<HBH")             # idx enc len
T_HELLO, T_UPDATE, T_KEY, T_RATE, T_PING, T_PONG, T_ACK = 0x01, 0x02, 0x10, 0x11, 0x12, 0x13, 0x14
ENC_FILL, ENC_PAL1, ENC_PAL2, ENC_RLE16, ENC_RAW = 0, 1, 2, 3, 4
ST_RST_N, ST_BL, ST_OWNER, ST_DISPLAY_ON, ST_STANDBY = 1, 2, 4, 8, 16
ST_IN_GRAM, ST_FMT_OK, ST_APPROX, ST_VIOL, ST_OOB, ST_RD = 32, 64, 128, 256, 512, 1024
S_EXACT, S_TEXT_ONLY, S_BLIND = 1 << 16, 1 << 17, 1 << 18
S_KEY, S_KEY_FIRST, S_KEY_LAST, S_SNAP_LAST = 1 << 24, 1 << 25, 1 << 26, 1 << 27
OWNER_HARNESS, OWNER_DUT, OWNER_UNKNOWN = 0, 1, 3


class Refused(Exception):
    """The board sent its one-line refusal instead of HELLO."""

    def __init__(self, line: bytes):
        self.line = line
        try:
            self.reply = json.loads(line)
        except ValueError:
            self.reply = None
        super().__init__(line.decode(errors="replace"))


class ProtocolError(Exception):
    pass


def decode_tile(enc: int, p: bytes) -> List[int]:
    """One tile payload -> 256 RGB565 values, row-major. Raises ProtocolError on
    anything the contract does not allow."""
    def c16(off: int) -> int:
        return p[off] | (p[off + 1] << 8)
    if enc == ENC_FILL:
        if len(p) != 2:
            raise ProtocolError(f"FILL len {len(p)}")
        return [c16(0)] * 256
    if enc == ENC_PAL1:
        if len(p) != 36:
            raise ProtocolError(f"PAL1 len {len(p)}")
        pal = (c16(0), c16(2))
        out = []
        for y in range(16):
            row = c16(4 + 2 * y)
            out += [pal[(row >> x) & 1] for x in range(16)]
        return out
    if enc == ENC_PAL2:
        if len(p) != 72:
            raise ProtocolError(f"PAL2 len {len(p)}")
        pal = [c16(2 * k) for k in range(4)]
        out = []
        for y in range(16):
            (row,) = struct.unpack_from("<I", p, 8 + 4 * y)
            out += [pal[(row >> (2 * x)) & 3] for x in range(16)]
        return out
    if enc == ENC_RLE16:
        out, i = [], 0
        while i < len(p):
            t = p[i]
            i += 1
            if t & 0x80:
                if i + 2 > len(p):
                    raise ProtocolError("RLE16 run truncated")
                out += [c16(i)] * ((t & 0x7F) + 1)
                i += 2
            else:
                n = t + 1
                if i + 2 * n > len(p):
                    raise ProtocolError("RLE16 literals truncated")
                out += [c16(i + 2 * k) for k in range(n)]
                i += 2 * n
        if len(out) != 256:
            raise ProtocolError(f"RLE16 covers {len(out)} pixels")
        return out
    if enc == ENC_RAW:
        if len(p) != 512:
            raise ProtocolError(f"RAW len {len(p)}")
        return list(struct.unpack("<256H", p))
    raise ProtocolError(f"unknown encoding {enc}")


@dataclass
class Update:
    seq: int
    t_ms: int
    frames: int
    resets: int
    status: int
    owner: int
    valid: int                     # bit t = tile t
    regs: Optional[bytes]          # key_first only
    mode: Optional[int]            # otherwise: R16 | R17<<8 | R36<<16 | R01<<24
    tiles: List[Tuple[int, int, int]] = field(default_factory=list)   # (idx, enc, len)
    size: int = 0                  # the whole message, header included

    key = property(lambda s: bool(s.status & S_KEY))
    key_first = property(lambda s: bool(s.status & S_KEY_FIRST))
    key_last = property(lambda s: bool(s.status & S_KEY_LAST))
    snap_last = property(lambda s: bool(s.status & S_SNAP_LAST))
    blind = property(lambda s: bool(s.status & S_BLIND))
    text_only = property(lambda s: bool(s.status & S_TEXT_ONLY))
    exact = property(lambda s: bool(s.status & S_EXACT))


def parse_update(b: bytes, size: int = 0) -> Tuple[Update, List[Tuple[int, int, bytes]]]:
    seq, t_ms, frames, resets, status, owner, valid = UPD.unpack_from(b, 0)
    o = UPD.size
    regs = mode = None
    if status & S_KEY_FIRST:
        regs = bytes(b[o:o + 256])
        o += 256
    else:
        (mode,) = struct.unpack_from("<I", b, o)
        o += 4
    (ntiles,) = struct.unpack_from("<H", b, o)
    o += 2
    u = Update(seq, t_ms, frames, resets, status, owner, int.from_bytes(valid, "little"),
               regs, mode, size=size)
    recs = []
    for _ in range(ntiles):
        idx, enc, ln = TREC.unpack_from(b, o)
        o += TREC.size
        if idx >= NTILES:
            raise ProtocolError(f"tile index {idx}")
        recs.append((idx, enc, bytes(b[o:o + ln])))
        u.tiles.append((idx, enc, ln))
        o += ln
    if o != len(b):
        raise ProtocolError(f"{len(b) - o} trailing bytes in UPDATE")
    return u, recs


class MirrorClient:
    def __init__(self, port: int, host: str = "127.0.0.1", *, bind: Optional[str] = None,
                 timeout: float = 5.0, ack: bool = True):
        self.sock = socket.create_connection((host, port), timeout=timeout,
                                             source_address=(bind, 0) if bind else None)
        self.sock.settimeout(timeout)
        self.ack = ack
        self._buf = b""
        self.hello: Optional[dict] = None
        self.frame = array("H", bytes(W * H * 2))   # composited at each snap_last
        self.live = array("H", bytes(W * H * 2))    # every tile as it arrives
        self.valid = 0
        self.regs = bytes(256)
        self.last: Optional[Update] = None
        self.updates: List[Update] = []
        self.pongs: List[int] = []
        self.rate_echoes: List[int] = []
        self.gaps = 0
        first = self._need(1)
        if first[:1] == b"{":
            raise Refused(self._line())
        t, body, _ = self.recv_msg()
        if t != T_HELLO:
            raise ProtocolError(f"first message type {t:#x}, not HELLO")
        self.hello = json.loads(body)
        if not 4096 <= self.hello["max_msg"] <= 65536:
            raise ProtocolError(f"max_msg {self.hello['max_msg']}")

    # ---- transport ----------------------------------------------------------
    def _need(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("mirror closed the connection")
            self._buf += chunk
        return self._buf[:n]

    def _take(self, n: int) -> bytes:
        self._need(n)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def recv_msg(self) -> Tuple[int, bytes, int]:
        magic, typ, _rsvd, n = HDR.unpack(self._take(HDR.size))
        if magic != b"LM":
            raise ProtocolError(f"bad magic {magic!r}")
        if self.hello is not None and n + HDR.size > self.hello["max_msg"]:
            raise ProtocolError(f"message of {n + HDR.size} B > max_msg {self.hello['max_msg']}")
        return typ, self._take(n), n + HDR.size

    def send(self, mtype: int, body: bytes = b"") -> None:
        self.sock.sendall(HDR.pack(b"LM", mtype, 0, len(body)) + body)

    def send_key(self) -> None:
        self.send(T_KEY)

    def send_rate(self, hz: int) -> None:
        self.send(T_RATE, bytes([hz & 0xFF]))

    def send_ping(self, token: int) -> None:
        self.send(T_PING, struct.pack("<I", token & 0xFFFFFFFF))

    def send_ack(self, seq: int) -> None:
        self.send(T_ACK, struct.pack("<I", seq & 0xFFFFFFFF))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- messages -----------------------------------------------------------
    def _apply(self, b: bytes, size: int) -> Update:
        u, recs = parse_update(b, size)
        for idx, enc, payload in recs:
            px = decode_tile(enc, payload)
            x0, y0 = (idx % TX) * TILE, (idx // TX) * TILE
            for yy in range(TILE):
                base = (y0 + yy) * W + x0
                self.live[base:base + TILE] = array("H", px[yy * TILE:(yy + 1) * TILE])
        if self.last is not None and u.seq != (self.last.seq + 1) & 0xFFFFFFFF:
            self.gaps += 1
        self.last = u
        if u.regs is not None:
            self.regs = u.regs
        if u.snap_last:                                 # H1: show whole SNAPs only
            self.frame[:] = self.live
            self.valid = u.valid
        self.updates.append(u)
        if self.ack:
            self.send_ack(u.seq)
        return u

    def next_update(self, *, apply: bool = True) -> Update:
        """The next UPDATE (PONGs and RATE echoes on the way are recorded).
        apply=False reads and DROPS it (still ACKed when ack is on) -- how a test
        makes a sequence gap on a reliable stream."""
        while True:
            t, body, size = self.recv_msg()
            if t == T_PONG:
                self.pongs.append(struct.unpack("<I", body[:4])[0])
                continue
            if t == T_RATE:
                self.rate_echoes.append(body[0])
                continue
            if t != T_UPDATE:
                raise ProtocolError(f"unexpected message {t:#x}")
            if not apply:
                u, _ = parse_update(body, size)
                if self.ack:
                    self.send_ack(u.seq)
                return u
            return self._apply(body, size)

    def pump_until(self, cond: Callable[["MirrorClient", Update], bool], timeout: float = 10.0) -> Update:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("condition not met")
            self.sock.settimeout(max(0.05, left))
            u = self.next_update()
            if cond(self, u):
                return u

    def pump_for(self, seconds: float) -> List[Update]:
        out: List[Update] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                out.append(self.next_update())
            except socket.timeout:
                break
        return out

    def valid_count(self) -> int:
        return bin(self.valid).count("1")


# --------------------------------------------------------------------------- #
# The font oracle: what clcd.c MEANS to draw (40x15 cells of font8x16, white on
# black, white on red for an inverted row), independent of every C path.
# --------------------------------------------------------------------------- #
WHITE, BLACK, RED = 0xFFFF, 0x0000, 0xF800


def load_font(path: Path) -> dict:
    """firmware/clcd/font8x16.h -> {char: 16 row bytes}; bit 7 = leftmost pixel."""
    rows = re.findall(r"\{((?:\s*0x[0-9a-fA-F]{2}\s*,?){16})\}", Path(path).read_text())
    glyphs = [bytes(int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{2})", r)) for r in rows]
    if len(glyphs) != 95:
        raise ValueError(f"{path}: {len(glyphs)} glyphs, expected 95")
    return {chr(0x20 + i): g for i, g in enumerate(glyphs)}


def render_grid(font: dict, grid: List[str], inv: List[bool]) -> array:
    fb = array("H", bytes(W * H * 2))
    for r in range(15):
        bg = RED if inv[r] else BLACK
        for c in range(40):
            g = font.get(grid[r][c], font[" "])
            for yy in range(16):
                bits = g[yy]
                base = (r * 16 + yy) * W + c * 8
                for xx in range(8):
                    fb[base + xx] = WHITE if bits & (0x80 >> xx) else bg
    return fb


def ocr(font: dict, fb) -> Tuple[List[str], List[bool]]:
    """Read the 40x15 text grid back off a frame. Every cell must be EXACTLY one
    glyph in white on one row-wide background (black or red); anything else
    raises ValueError naming the first offending cell."""
    lookup = {g: ch for ch, g in font.items()}
    grid: List[str] = []
    inv: List[bool] = []
    for r in range(15):
        bgs = set()
        line = ""
        for c in range(40):
            pat = bytearray(16)
            colours = set()
            for yy in range(16):
                base = (r * 16 + yy) * W + c * 8
                for xx in range(8):
                    px = fb[base + xx]
                    colours.add(px)
                    if px == WHITE:
                        pat[yy] |= 0x80 >> xx
            bg = colours - {WHITE}
            if len(bg) > 1 or (bg and next(iter(bg)) not in (BLACK, RED)):
                raise ValueError(f"cell r{r} c{c}: colours {sorted(hex(x) for x in colours)}")
            if bg:
                bgs.add(next(iter(bg)))
            ch = lookup.get(bytes(pat))
            if ch is None:
                raise ValueError(f"cell r{r} c{c}: not a glyph {bytes(pat).hex()}")
            line += ch
        if len(bgs) > 1:
            raise ValueError(f"row {r}: mixed backgrounds")
        grid.append(line)
        inv.append(RED in bgs)
    return grid, inv


# --------------------------------------------------------------------------- #
# The ALIGNED oracle (net-protocol v0.17; Harness Manager's tokens, decision D3 a,
# harnessd's default since lane PANEL-FINISH): every cell is ONE glyph -- ASCII
# from font8x16.h or a status glyph 0x80-0x86 from HM's clcd_glyphs.h -- in ONE
# of HM's role colour pairs (clcd_palette.h), per CELL rather than per row. Both
# files are read here as text, never through clcd.c.
# --------------------------------------------------------------------------- #
#: the panel verb's role letters: 'a' + enum clcd_role (clcd_palette.h order)
ROLE_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def load_font_ext(path: Path) -> dict:
    """HM's design/generated/clcd_glyphs.h (vendored as firmware/clcd/clcd_glyphs.h)
    -> {chr(0x80 + i): 16 row bytes}; bit 7 = leftmost pixel, as font8x16.h."""
    text = Path(path).read_text()
    first = int(re.search(r"#define CLCD_FONT_EXT_FIRST\s+(0x[0-9a-fA-F]+)", text).group(1), 16)
    count = int(re.search(r"#define CLCD_FONT_EXT_COUNT\s+(\d+)", text).group(1))
    body = text[text.index("font8x16_ext[CLCD_FONT_EXT_COUNT][16] = {"):].split("};")[0]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = body[body.index("{") + 1:]
    vals = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{2})\b", body)]
    if len(vals) != 16 * count:
        raise ValueError(f"{path}: {len(vals)} scanlines, expected {16 * count}")
    return {chr(first + i): bytes(vals[16 * i:16 * i + 16]) for i in range(count)}


def load_palette(path: Path) -> List[Tuple[int, int]]:
    """HM's clcd_palette.h -> [(fg, bg)] indexed by enum clcd_role (the enum's
    order, which is also the panel verb's letter order)."""
    text = Path(path).read_text()
    enum = re.search(r"enum clcd_role \{(.*?)\};", text, re.S).group(1)
    names = [n for n in re.findall(r"CLCD_ROLE_([A-Z_]+)", enum) if n != "COUNT"]
    words = dict(re.findall(r"#define CLCD_RGB565_([A-Z_]+)\s+0x([0-9A-Fa-f]{4})u", text))
    return [(int(words[f"{n}_FG"], 16), int(words[f"{n}_BG"], 16)) for n in names]


def render_cells(font: dict, grid: List[str], cells) -> array:
    """40x15 cells, each drawn in its own (fg, bg); fg None = a blank cell."""
    fb = array("H", bytes(W * H * 2))
    for r in range(15):
        for c in range(40):
            fg, bg = cells[r * 40 + c]
            g = font.get(grid[r][c], font[" "])
            for yy in range(16):
                bits = g[yy]
                base = (r * 16 + yy) * W + c * 8
                for xx in range(8):
                    fb[base + xx] = fg if bits & (0x80 >> xx) else bg
    return fb


def render_roles(font: dict, grid: List[str], roles: str, palette) -> array:
    """The panel verb's frame (rows + one role letter a cell) in a palette."""
    if len(roles) != 600:
        raise ValueError(f"{len(roles)} role letters, expected 600")
    return render_cells(font, grid, [palette[ROLE_LETTERS.index(x)] for x in roles])


def ocr_cells(font: dict, fb, pairs) -> Tuple[List[str], List[Tuple[Optional[int], int]]]:
    """Read the 40x15 grid back off a frame drawn in per-cell colour PAIRS (the
    aligned theme: pairs = load_palette(...)). Every cell must be EXACTLY one glyph
    of `font` in one (fg, bg) of `pairs`; a one-colour cell is a blank in some
    pair's bg (its fg reads None). Returns (grid, cells), cells[r*40+c] = (fg, bg).
    Raises ValueError naming the first offending cell (a glyph caught mid-draw)."""
    lookup = {g: ch for ch, g in font.items()}
    pairset = set(pairs)
    bgs = {bg for _fg, bg in pairset}
    grid: List[str] = []
    cells: List[Tuple[Optional[int], int]] = []
    for r in range(15):
        line = ""
        for c in range(40):
            px = [fb[(r * 16 + yy) * W + c * 8 + xx] for yy in range(16) for xx in range(8)]
            colours = set(px)
            if len(colours) == 1:
                (bg,) = colours
                if bg not in bgs:
                    raise ValueError(f"cell r{r} c{c}: {bg:#06x} is no role's background")
                line += " "
                cells.append((None, bg))
                continue
            if len(colours) != 2:
                raise ValueError(f"cell r{r} c{c}: colours {sorted(hex(x) for x in colours)}")
            a, b = sorted(colours)
            found = []
            for fg, bg in ((a, b), (b, a)):
                if (fg, bg) not in pairset:
                    continue
                pat = bytes(sum(0x80 >> xx for xx in range(8) if px[yy * 8 + xx] == fg)
                            for yy in range(16))
                ch = lookup.get(pat)
                if ch is not None:
                    found.append((ch, fg, bg))
            if len(found) != 1:
                raise ValueError(f"cell r{r} c{c}: {'no' if not found else 'two'} glyph readings "
                                 f"in colours {a:#06x}/{b:#06x}")
            ch, fg, bg = found[0]
            line += ch
            cells.append((fg, bg))
        grid.append(line)
    return grid, cells


def cells_match_roles(cells, roles: str, palette) -> List[int]:
    """The cells (ocr_cells) whose colours are NOT their role's pair (a blank cell
    only has to show its role's bg). [] = every cell agrees."""
    bad = []
    for i, (fg, bg) in enumerate(cells):
        want = palette[ROLE_LETTERS.index(roles[i])]
        if bg != want[1] or (fg is not None and fg != want[0]):
            bad.append(i)
    return bad


def read_grid_file(path: Path) -> Tuple[List[str], List[bool]]:
    """A clcd_preview-style grid ("INVrr |<40>|" / "   rr |<40>|"), as Harness
    Manager's fixtures carry it."""
    grid, inv = [], []
    for line in Path(path).read_text().splitlines():
        m = re.match(r"^(INV|   )(\d\d) \|(.{40})\|$", line)
        if m:
            inv.append(m.group(1) == "INV")
            grid.append(m.group(3))
    if len(grid) != 15:
        raise ValueError(f"{path}: {len(grid)} rows")
    return grid, inv
