"""test_lcdmirror_e2e.py -- the LCD mirror's interim software mode, end to end
(LCD_MIRROR_FPGA.md §6-§8; wire: net-protocol.md "LCD mirror (TCP 6940)").

Three layers, each judged by something it did not produce:

1. PURE (no harnessd). The checked-in wire vectors are what the board's
   builders emit today, and the reference client decodes them to the source
   pictures (manifest CRCs). The C port of the GRAM model (lcdmirror_replay)
   equals, pixel for pixel and CSR for CSR, the platform golden model
   (tests/lcd_mirror/hx8347_gram_model.py) on shared synthetic vectors, and
   Harness Manager's recorded clcd.c streams rebuild HM's grids exactly (H2),
   agreeing with HM's own bus model too.
2. HARNESSD (host build, MOCK fabric) + its mps3-lcdmirror child over REAL
   sockets: the decoded frame equals the font rendering of the text clcd.c
   draws -- in the DEFAULT look, HM's aligned palette + status glyphs 0x80-0x86
   (ocr_cells: one glyph in one of HM's role pairs a cell; and the panel verb's
   rows + role letters drawn in HM's palette ARE the mirror's pixels), and with
   `--panel-theme today` today's white/black/red screen -- and the aperture
   harnessd's tap writes; the contract fields; the
   refusals; KEY after a gap; RATE clamp/echo/pause; PING at any time; the ACK
   window; owner -> blind -> regain; the per-pass budget; child supervision.
3. NEGATIVE CONTROLS: an encoder mutation (tile index, PAL1 bit order, RLE run
   length, pixel byte order) injected into the running server MUST fail the
   comparison; a corrupted vector must fail its CRC; a mismatched golden-model
   convention must differ.
"""
from __future__ import annotations

import json
import mmap
import os
import random
import signal
import socket
import struct
import subprocess
import sys
import time
import zlib
from array import array
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[4]

from harnessd_mock import Harnessd, free_port_offset  # noqa: E402
from lcdmirror_client import (  # noqa: E402
    ENC_PAL1, ENC_RAW, HDR, NTILES, S_BLIND, S_EXACT, S_KEY, S_KEY_FIRST, S_KEY_LAST,
    S_SNAP_LAST, S_TEXT_ONLY, T_HELLO, MirrorClient, ProtocolError, Refused, cells_match_roles,
    decode_tile, load_font, load_font_ext, load_palette, ocr, ocr_cells, parse_update,
    read_grid_file, render_cells, render_grid, render_roles,
)

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
LCDM = str(Path(BIN).parent / "mps3-lcdmirror")
TBIN = Path(os.environ.get("HARNESSD_TBIN", HERE.parent / "build" / "tests"))
FIX_WIRE = HERE / "fixtures" / "lcdmirror_wire"
FIX_HM = HERE / "fixtures" / "lcd_mirror_hm"
GOLDEN = REPO / "tests" / "lcd_mirror" / "hx8347_gram_model.py"
FONT = load_font(REPO / "firmware" / "clcd" / "font8x16.h")
#: the aligned look (harnessd's default): ASCII + HM's status glyphs, HM's role pairs
FONT_ALL = {**FONT, **load_font_ext(REPO / "firmware" / "clcd" / "clcd_glyphs.h")}
PALETTE = load_palette(REPO / "firmware" / "clcd" / "clcd_palette.h")
SID = 0x5A5A0001
W, H = 320, 240
CLCDKVM = 0x44AD0000
KVM_STATUS, KVM_EVENT = 0x04, 0x08

need_hd = pytest.mark.skipif(not Path(BIN).exists() or not Path(LCDM).exists(),
                             reason="make host (mps3-harnessd + mps3-lcdmirror)")


def _tool(name: str) -> Path:
    exe = TBIN / name
    if not exe.exists():
        pytest.skip(f"{exe} not built (make tests)")
    return exe


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class Aperture:
    """mmap of the aperture file (lcdmirror.h: the LCDMIR register layout)."""

    def __init__(self, path: Path):
        self._f = open(path, "r+b")
        self.m = mmap.mmap(self._f.fileno(), 0x40000)

    def word(self, off: int) -> int:
        return struct.unpack_from("<I", self.m, off)[0]

    def put(self, off: int, v: int) -> None:
        struct.pack_into("<I", self.m, off, v & 0xFFFFFFFF)

    def fb(self) -> array:
        a = array("H")
        a.frombytes(self.m[0x10000:0x10000 + W * H * 2])
        return a

    def valid(self) -> int:
        return int.from_bytes(self.m[0xC0:0xC0 + 40], "little") & ((1 << 300) - 1)

    def close(self):
        self.m.close()
        self._f.close()


def tile_of(fb, t: int) -> list:
    x0, y0 = (t % 20) * 16, (t // 20) * 16
    return [fb[(y0 + yy) * W + x0 + xx] for yy in range(16) for xx in range(16)]


def start_hd(tmp: Path, extra=None, **kw) -> Harnessd:
    return Harnessd(BIN, tmp, static_id=SID,
                    extra=["--lcdmirror-shm", str(tmp / "lcdm.shm")] + (extra or []), **kw).start()


def connect(port: int, timeout: float = 10.0, **kw) -> MirrorClient:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return MirrorClient(port, **kw)
        except (ConnectionRefusedError, ConnectionResetError, EOFError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def keyed(port: int, rate: int = 30, **kw) -> MirrorClient:
    c = connect(port, **kw)
    if rate != 5:
        c.send_rate(rate)
    c.send_key()
    return c


def painted(c: MirrorClient, timeout: float = 15.0):
    return c.pump_until(lambda c, u: u.snap_last and c.valid_count() == 300, timeout)


def read_screen(frame, theme: str = "aligned"):
    """One frame through the theme's oracle: aligned -> (grid, cells), every cell one
    glyph in one of HM's role pairs; today -> (grid, inv), white on black/red rows."""
    if theme == "today":
        return ocr(FONT, frame)
    return ocr_cells(FONT_ALL, frame, PALETTE)


def screen_ok(c: MirrorClient, timeout: float = 10.0, theme: str = "aligned"):
    """Pump until the composited frame reads as the harness status screen (every
    cell one glyph). harnessd's default look is "aligned": returns (grid, cells);
    theme="today" (`--panel-theme today`) returns (grid, inv)."""
    deadline = time.monotonic() + timeout
    err = None
    while time.monotonic() < deadline:
        c.pump_until(lambda c, u: u.snap_last, max(0.2, deadline - time.monotonic()))
        try:
            return read_screen(c.frame, theme)
        except ValueError as e:     # a glyph caught mid-draw: the next SNAP converges
            err = e
    raise AssertionError(f"never a whole screen: {err}")


def panel_frame(h: Harnessd):
    """clcd.c's COMMITTED frame through the v0.17 panel verb: (rows, roles, theme)."""
    rows, roles, theme = [], "", ""
    with h.ctl() as ctl:
        for part in "ab":
            r = ctl.req({"op": "panel", "frame": part})
            rows += r["rows"]
            roles += r["roles"]
            theme = r["theme"]
    return rows, roles, theme


def cell_px(fb, i: int) -> list:
    r, c = divmod(i, 40)
    return [fb[(r * 16 + yy) * W + c * 8 + xx] for yy in range(16) for xx in range(8)]


def verb_equals_mirror(c: MirrorClient, h: Harnessd, window: float = 0.6, timeout: float = 15.0):
    """The panel verb's text + role letters, drawn from the fonts in HM's palette as
    THIS test reads clcd_palette.h, equal the mirror's decoded pixels -- on every
    cell that held still across a window of SNAPs (the heartbeat turns at 4 Hz,
    the uptime ticks at 1 Hz). Returns (rows, roles, grid, cells, n_stable)."""
    deadline = time.monotonic() + timeout
    why = "no attempt"
    while time.monotonic() < deadline:
        r1, ro1, _t = panel_frame(h)
        c.pump_for(window)
        r2, ro2, theme = panel_frame(h)
        assert theme == "aligned", theme
        try:
            grid, cells = ocr_cells(FONT_ALL, c.frame, PALETTE)
        except ValueError as e:                  # mid-draw: the next window
            why = str(e)
            continue
        stable = [i for i in range(600) if r1[i // 40][i % 40] == r2[i // 40][i % 40]
                  and ro1[i] == ro2[i]]
        want = render_roles(FONT_ALL, r2, ro2, PALETTE)
        bad = [i for i in stable if grid[i // 40][i % 40] != r2[i // 40][i % 40]
               or cell_px(c.frame, i) != cell_px(want, i)]
        bad += [i for i in cells_match_roles(cells, ro2, PALETTE) if i in stable]
        if not bad and len(stable) >= 590:
            return r2, ro2, grid, cells, len(stable)
        why = f"{len(stable)} stable cells, mismatches at {sorted(set(bad))[:10]}"
    raise AssertionError(f"the panel verb's frame never equalled the mirror's: {why}")


def converge(c: MirrorClient, ap: Aperture, timeout: float = 10.0) -> int:
    """The decoded frame equals the aperture on every valid tile that held still
    across a window of SNAPs. Returns how many tiles were compared."""
    deadline = time.monotonic() + timeout
    bad = None
    while time.monotonic() < deadline:
        a = ap.fb()
        c.pump_for(0.4)
        b, valid = ap.fb(), ap.valid()
        stable = [t for t in range(NTILES) if valid >> t & 1 and tile_of(a, t) == tile_of(b, t)]
        bad = [t for t in stable if tile_of(c.frame, t) != tile_of(b, t)]
        if not bad and len(stable) >= 250:
            return len(stable)
    raise AssertionError(f"decoded frame != aperture on tiles {bad[:20] if bad else bad}")


def fresh_aperture(path: Path, frame, *, static_id=SID) -> Aperture:
    path.write_bytes(bytes(0x40000))
    ap = Aperture(path)
    ap.put(0x000, 0x4C43444D)
    ap.put(0x1010, static_id)
    ap.put(0x1014, 1)
    ap.put(0x010, 0x4B)                                  # rst_n bl display_on fmt_ok
    for w in range(10):
        ap.put(0xC0 + 4 * w, 0xFFFFFFFF if w < 9 else 0xFFF)
    ap.m[0x10000:0x10000 + W * H * 2] = array("H", frame).tobytes()
    ap.put(0x1000, 0x57534D4C)                           # SW_MAGIC last
    return ap


def noise(seed: int = 7) -> list:
    r = random.Random(seed)
    return [r.getrandbits(16) for _ in range(W * H)]


class Standalone:
    """mps3-lcdmirror alone against a test-written aperture (no harnessd)."""

    def __init__(self, tmp: Path, frame, env=None):
        self.ap = fresh_aperture(tmp / "ap.shm", frame)
        off = free_port_offset()
        self.port = off + 6940
        self.proc = subprocess.Popen([LCDM, "--shm", str(tmp / "ap.shm"), "--port", str(self.port)],
                                     env={**os.environ, **(env or {})}, stderr=subprocess.PIPE)

    def close(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.ap.close()


# --------------------------------------------------------------------------- #
# 1. PURE: the wire vectors
# --------------------------------------------------------------------------- #
def test_wire_vectors_are_what_the_board_builds(tmp_path):
    subprocess.run([str(_tool("lcdmirror_vectors")), str(tmp_path)], check=True, capture_output=True)
    want = sorted(p.name for p in FIX_WIRE.iterdir())
    assert sorted(p.name for p in tmp_path.iterdir()) == want
    for name in want:
        assert (tmp_path / name).read_bytes() == (FIX_WIRE / name).read_bytes(), \
            f"{name}: the builders drifted from the checked-in vector (make lcdmirror-vectors)"


def _crc_px(px) -> int:
    return zlib.crc32(array("H", px).tobytes()) & 0xFFFFFFFF


def _decode_file(path: Path, max_msg: int = 65536):
    raw = path.read_bytes()
    magic, typ, rsvd, n = HDR.unpack_from(raw)
    assert magic == b"LM" and rsvd == 0 and len(raw) == 8 + n <= max_msg
    return typ, raw[8:]


def test_wire_vectors_decode_to_the_source_pictures():
    man = json.loads((FIX_WIRE / "manifest.json").read_text())["vectors"]
    kinds = {v["kind"] for v in man}
    assert kinds == {"hello", "update", "keyframe", "refusal"}
    encs = set()
    for v in man:
        if v["kind"] == "hello":
            typ, body = _decode_file(FIX_WIRE / v["file"])
            hello = json.loads(body)
            assert typ == T_HELLO and hello == {
                "proto": 1, "w": 320, "h": 240, "fmt": "rgb565le", "tile": 16, "mode": "sw",
                "static_id": "0x5a5a0001", "max_msg": 65536,
                "boot_id": "00000000-0000-0000-0000-000000000000", "rate": 5, "rate_max": 30,
                "clients_max": 2}
        elif v["kind"] == "update":
            typ, body = _decode_file(FIX_WIRE / v["file"])
            u, recs = parse_update(body)
            assert (u.seq, u.t_ms, u.status, u.owner) == (v["seq"], v["t_ms"], v["status"], v["owner"])
            assert u.snap_last and not u.key and u.text_only and not u.exact
            assert [(i, e, len(p)) for i, e, p in recs] == [tuple(t[:3]) for t in v["tiles"]]
            for (idx, enc, payload), t in zip(recs, v["tiles"]):
                assert _crc_px(decode_tile(enc, payload)) == t[3], v["file"]
                encs.add(enc)
        elif v["kind"] == "keyframe":
            frame = array("H", bytes(W * H * 2))
            seen, seqs = set(), []
            for i, name in enumerate(v["parts"]):
                typ, body = _decode_file(FIX_WIRE / name)
                u, recs = parse_update(body)
                last = i == len(v["parts"]) - 1
                assert u.key and u.key_first == (i == 0) and u.key_last == last and u.snap_last == last
                assert (u.regs is not None) == (i == 0)          # REGS ride on key_first only
                seqs.append(u.seq)
                for idx, enc, payload in recs:
                    px = decode_tile(enc, payload)
                    x0, y0 = (idx % 20) * 16, (idx // 20) * 16
                    for yy in range(16):
                        frame[(y0 + yy) * W + x0:(y0 + yy) * W + x0 + 16] = array("H", px[yy * 16:yy * 16 + 16])
                    seen.add(idx)
            assert len(v["parts"]) >= 3 and seqs == list(range(v["seq_first"], v["seq_first"] + len(seqs)))
            assert len(seen) == v["valid_count"] and zlib.crc32(frame.tobytes()) & 0xFFFFFFFF == v["frame_crc"]
        else:
            line = (FIX_WIRE / v["file"]).read_bytes()
            assert line.endswith(b"\n") and line.count(b"\n") == 1 and line[:1] == b"{"
            r = json.loads(line)
            assert r["ok"] is False and r["err"].startswith("lcd_mirror: ")
    assert encs == {0, 1, 2, 3, 4}, "one UPDATE per encoding"


def test_negative_control_a_corrupted_vector_fails_its_crc():
    man = {v["file"]: v for v in json.loads((FIX_WIRE / "manifest.json").read_text())["vectors"]}
    for name in ("update_pal1.bin", "update_rle16.bin", "update_raw.bin"):
        _, body = _decode_file(FIX_WIRE / name)
        _, recs = parse_update(body)
        idx, enc, payload = recs[0]
        bad = bytearray(payload)
        bad[len(bad) // 2] ^= 0x01                          # one bit, mid-payload
        try:
            crc = _crc_px(decode_tile(enc, bytes(bad)))
        except ProtocolError:
            continue                                        # malformed is also caught
        assert crc != man[name]["tiles"][0][3], f"{name}: a flipped bit went unnoticed"


# --------------------------------------------------------------------------- #
# 1. PURE: the C port vs the golden models (H2 + LCD_MIRROR_FPGA.md §8.1)
# --------------------------------------------------------------------------- #
def _replay(tmp: Path, pairs, ctrl: int = 0) -> Aperture:
    src = tmp / f"s{random.getrandbits(32):08x}.bin"
    src.write_bytes(bytes(b for rs, d in pairs for b in (rs, d)))
    out = src.with_suffix(".ap")
    subprocess.run([str(_tool("lcdmirror_replay")), "--ctrl", str(ctrl), str(src), str(out)], check=True)
    # ... and through the bulk decoder the CLCD bulk tap uses (lane CLCD-SPEED):
    # every shared vector must leave the SAME aperture both ways, byte for byte
    bulk = src.with_suffix(".bulk.ap")
    subprocess.run([str(_tool("lcdmirror_replay")), "--ctrl", str(ctrl), "--bulk", str(src), str(bulk)],
                   check=True)
    assert bulk.read_bytes() == out.read_bytes(), "lcdm_model_bytes() != lcdm_model_byte() on a vector"
    return Aperture(out)


def _hm_stream(name: str):
    raw = zlib.decompress((FIX_HM / f"{name}.stream.z").read_bytes())
    return list(zip(raw[0::2], raw[1::2]))


def _golden():
    if not GOLDEN.exists():
        pytest.skip("tests/lcd_mirror/hx8347_gram_model.py not in this tree")
    sys.path.insert(0, str(GOLDEN.parent))
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # tests/lcd_mirror is LCDMIR-RTL's
    try:
        import hx8347_gram_model as gm   # noqa: E402
    finally:
        sys.dont_write_bytecode = keep
    return gm


def _hm_model():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hm_lcd_mirror_decoder", FIX_HM / "lcd_mirror_decoder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hm_lcd_mirror_decoder"] = mod
    spec.loader.exec_module(mod)
    return mod


HM_PULSE = [(2, 0x01), (2, 0x07)]      # HM's ctrl_reset_pulse(): the KVM's S_RST on the pads


def _hm_stages():
    s = []
    acc = []
    for name, pre in (("boot", []), ("link_down", []), ("banner", []), ("regain", HM_PULSE)):
        acc = acc + pre + _hm_stream(name)
        s.append((name, list(acc)))
    return s


def test_hm_streams_through_the_c_port_rebuild_hms_grids(tmp_path):
    """H2: HM's recorded clcd.c streams, cumulative on one panel, through the C
    port: every stage equals the font rendering of HM's grid, pixel for pixel."""
    for name, pairs in _hm_stages():
        ap = _replay(tmp_path, pairs)
        grid, inv = read_grid_file(FIX_HM / f"{name}.grid")
        want = render_grid(FONT, grid, inv)
        got = ap.fb()
        bad = sum(1 for i in range(W * H) if got[i] != want[i])
        assert bad == 0, f"{name}: {bad} px differ from HM's grid"
        assert ap.valid() == (1 << 300) - 1, name
        ap.close()


def test_hm_streams_c_port_equals_hms_model_and_the_golden_model(tmp_path):
    hm = _hm_model()
    gm = _golden()
    for name, pairs in _hm_stages():
        ap = _replay(tmp_path, pairs)
        got = ap.fb()
        shadow = hm.Hx8347dShadow()
        shadow.feed(pairs)
        assert list(shadow.viewer()) == list(got), f"{name}: C port != HM's Hx8347dShadow"
        g = gm.GramModel()
        rst_n = 1
        for rs, d in pairs:
            if rs == 2:
                if rst_n and not d & 0x04:
                    g.panel_reset()
                rst_n = d >> 2 & 1
            else:
                g.byte(rs, d)
        assert g.fb == list(got), f"{name}: C port != golden model frame"
        assert g.map_words(g.valid) == [ap.word(0xC0 + 4 * i) for i in range(10)]
        ap.close()


def _golden_vectors(gm):
    """Shared synthetic vectors (LCD_MIRROR_FPGA.md §8.3 stressors), as
    (name, ctrl, pairs-with-rs3-resets)."""
    wp, pp = gm.window_pairs, gm.pixel_pairs
    r = random.Random(1)
    px = [r.getrandbits(16) for _ in range(700)]
    v = []
    for mad in range(0, 0x100, 0x20):
        lw, lh = (320, 240) if mad & 0x20 else (240, 320)
        for conv in (0, 1):
            pairs = [(0, 0x17), (1, 0x05), (0, 0x16), (1, mad)] + wp(lw - 40, lw + 3, lh - 9, lh - 1) \
                + [(0, 0x22)] + pp(px[:400])
            v.append((f"madctl_{mad:02x}_conv{conv}", conv << 1, pairs))
    v.append(("fmt18", 0, [(0, 0x16), (1, 0x20), (0, 0x17), (1, 0x06)] + wp(10, 40, 5, 9)
              + [(0, 0x22)] + pp(px[:120], fmt18=True)))
    v.append(("ac_load", 1, [(0, 0x17), (1, 0x05), (0, 0x16), (1, 0x20)] + wp(50, 60, 50, 60)
              + [(0, 0x22)] + pp(px[:5]) + [(0, 0x03), (1, 55), (0, 0x2C), (0, 0x22)] + pp(px[5:30])))
    v.append(("start_after_end_wraps_9bit", 0, [(0, 0x17), (1, 0x05), (0, 0x16), (1, 0x20)]
              + wp(300, 20, 230, 10) + [(0, 0x22)] + pp(px[:600])))
    v.append(("split_ramwr", 0, [(0, 0x17), (1, 0x05), (0, 0x16), (1, 0x20)] + wp(0, 319, 0, 239)
              + [(0, 0x22), (1, 0x12), (0, 0x22)] + pp(px[:40])))
    v.append(("midstream_reset", 0, [(0, 0x17), (1, 0x05), (0, 0x16), (1, 0x20)] + wp(0, 63, 0, 31)
              + [(0, 0x22)] + pp(px[:300]) + [(3, 0)] + [(1, 0x77), (1, 0x78), (0, 0x22)]
              + pp(px[300:400], fmt18=True)))
    return v


def _feed_golden(g, pairs):
    for rs, d in pairs:
        if rs == 3:
            g.panel_reset()
        else:
            g.byte(rs, d)


GOLDEN_SKIP = {0x050, 0x054, 0x058} | {0x080 + 4 * i for i in range(10)}   # SNAP-side: the child's


def test_c_port_equals_the_golden_model_on_shared_vectors(tmp_path):
    gm = _golden()
    for name, ctrl, pairs in _golden_vectors(gm):
        ap = _replay(tmp_path, pairs, ctrl)
        g = gm.GramModel(ac_load=ctrl & 1, flip_conv=ctrl >> 1 & 1)
        _feed_golden(g, pairs)
        want = g.csrs()
        for off, val in want.items():
            if off in GOLDEN_SKIP:
                continue
            assert ap.word(off) == val, f"{name}: CSR {off:#05x} = {ap.word(off):#x}, golden {val:#x}"
        words = [ap.word(0x10000 + 4 * i) for i in range(W * H // 2)]
        assert words == g.fb_words(), f"{name}: frame buffer differs from the golden model"
        ap.close()


def test_negative_control_the_other_flip_convention_differs(tmp_path):
    gm = _golden()
    name, ctrl, pairs = next(x for x in _golden_vectors(gm) if x[0] == "madctl_60_conv0")
    ap = _replay(tmp_path, pairs, 0x2)                  # the C port told conv 1 ...
    g = gm.GramModel(flip_conv=0)                       # ... the golden model conv 0
    _feed_golden(g, pairs)
    words = [ap.word(0x10000 + 4 * i) for i in range(W * H // 2)]
    assert words != g.fb_words(), "a flip-convention mismatch went unnoticed"
    ap.close()


# --------------------------------------------------------------------------- #
# 2. HARNESSD + mps3-lcdmirror
# --------------------------------------------------------------------------- #
@need_hd
def test_hello_then_nothing_until_key_then_a_whole_keyframe(tmp_path):
    h = start_hd(tmp_path)
    try:
        c = connect(h.port(6940))
        assert c.hello["mode"] == "sw" and c.hello["static_id"] == f"0x{SID:08x}"
        assert c.hello["max_msg"] <= 65536 and c.hello["clients_max"] == 2
        assert c.hello["rate"] == 5 and c.hello["rate_max"] == 30 and c.hello["boot_id"]
        c.sock.settimeout(0.6)
        with pytest.raises(socket.timeout):
            c.next_update()                             # amendment 6: KEY starts it
        c.send_key()
        u = c.pump_until(lambda c, u: True, 5)
        assert u.key and u.key_first and u.regs is not None and u.seq == 1
        painted(c)
        k = [x for x in c.updates if x.key]
        assert all(x.key for x in k) and k[-1].key_last and k[-1].snap_last
        c.close()
    finally:
        h.stop()


@need_hd
def test_decoded_frame_equals_the_golden_screen_pixel_for_pixel(tmp_path):
    """The DEFAULT look (aligned, HM's tokens): no --panel-theme."""
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940))
        painted(c)
        grid, cells = screen_ok(c)
        # the golden frame: the font rendering (ASCII + HM's glyphs) of the text the
        # oracle read, each cell in the one HM role pair it found ...
        assert list(c.frame) == list(render_cells(FONT_ALL, grid, cells))
        # ... and clcd.c's committed frame (the panel verb: rows + role letters),
        # drawn in HM's palette, IS the mirror's frame, pixel for pixel
        rows, roles, grid, cells, _n = verb_equals_mirror(c, h)
        # which is the screen harnessd's fixed scenario must show, in HM's words:
        # row 0 = the BOARD identity's label (the image default "MPS3") on the title
        # chrome; the rules; the shell row; the link-down fault banner (banner-err,
        # rows 10-12, the err glyph 0x81 decoded off the glass); the mac footer
        # (role letters = 'a' + enum clcd_role: title 5, rule 3, label 1, chrome 4,
        # banner-err 16)
        L = {"title": "f", "rule": "d", "label": "b", "chrome": "e", "banner_err": "q"}
        assert grid[0].strip() == "MPS3" and set(roles[0:40]) == {L["title"]}
        assert grid[1] == "-" * 40 == grid[13] and set(roles[40:80]) == {L["rule"]}
        assert grid[4].startswith("shell  0x5A5A0001") and roles[160:165] == L["label"] * 5
        assert grid[11].strip() == "\x81 NETWORK LINK DOWN", repr(grid[11])
        assert set(roles[400:520]) == {L["banner_err"]}
        assert cells[11 * 40 + grid[11].index("\x81")] == PALETTE[16]      # banner-err pair
        assert grid[14].startswith(" mac 02:00:00:4D:50:53") and set(roles[560:600]) == {L["chrome"]}
        assert sum(1 for fg, bg in cells if (fg, bg) == (0xFFFF, 0x0000)) == 0, \
            "no white-on-black cell: this is not today's screen"
        ap = Aperture(tmp_path / "lcdm.shm")
        assert converge(c, ap) >= 250
        u = c.last
        assert u.text_only and not u.exact and not u.blind and u.owner == 0
        assert (u.status ^ ap.word(0x010)) & 0x7DF == 0
        ap.close()
        # NEGATIVE CONTROL: today's oracle cannot read this frame
        with pytest.raises(ValueError):
            ocr(FONT, c.frame)
        c.close()
    finally:
        h.stop()


@need_hd
def test_today_theme_decoded_frame_equals_the_golden_screen(tmp_path):
    """`--panel-theme today`: today's white/black/red screen, exactly as before."""
    h = start_hd(tmp_path, extra=["--panel-theme", "today"])
    try:
        c = keyed(h.port(6940))
        painted(c)
        grid, inv = screen_ok(c, theme="today")
        # the golden frame: the font rendering of the text clcd.c draws ...
        golden = render_grid(FONT, grid, inv)
        assert list(c.frame) == list(golden)
        # NEGATIVE CONTROL: HM's palette cannot read today's white-on-black cells
        with pytest.raises(ValueError):
            ocr_cells(FONT_ALL, c.frame, PALETTE)
        assert panel_frame(h)[2] == "today"
        # ... which is the screen harnessd's fixed scenario must show
        # row 0 = the BOARD identity's label (identity_linux.c): with no
        # /run/mps3/identity the image default "MPS3" (v0.16, lane IDENT; the old
        # compile-time "MPS3-01" is only the bare-metal fallback now)
        assert grid[0][:20] == "MPS3".ljust(20) and grid[0][20:35] == "nanoSoC harness"
        assert grid[1] == "-" * 40 and grid[13] == "-" * 40
        assert grid[4].startswith("SID : 0x5A5A0001")
        assert grid[11].strip() == "NETWORK LINK DOWN" and inv[10:13] == [True] * 3
        assert grid[14].startswith("MAC 02:00:00:4D:50:53")
        assert not any(inv[:10]) and not any(inv[13:])
        # and the aperture the tap wrote, tile for tile
        ap = Aperture(tmp_path / "lcdm.shm")
        assert converge(c, ap) >= 250
        u = c.last
        assert u.text_only and not u.exact and not u.blind and u.owner == 0
        assert (u.status ^ ap.word(0x010)) & 0x7DF == 0      # the CSR STATUS rides along (in_gram aside)
        ap.close()
        c.close()
    finally:
        h.stop()


@need_hd
def test_aligned_glyphs_badge_and_page_change_through_the_mirror(tmp_path):
    """The aligned look's moving parts, through the mirror: a Harness Manager hello
    puts the lease badge on row 0 (the held glyph 0x83, title-held); `panel page
    apps` (v0.17) repaints the other page -- the verb's frame in HM's palette equals
    the decoded pixels before and after (the board-2 golden capture's own steps)."""
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940))
        painted(c)
        r = h.req({"op": "hello", "v": 1, "sid": "a1b2c3d4", "who": "alice@lab-pc01",
                   "app": "hm/0.1.0", "role": "holder",
                   "lease": {"by": "alice", "left": 4332, "q": 1}, "ttl": 90})
        assert r["ok"] is True
        deadline = time.monotonic() + 10
        while "\x83" not in panel_frame(h)[0][0] and time.monotonic() < deadline:
            time.sleep(0.05)
        rows, roles, grid, cells, _n = verb_equals_mirror(c, h)
        b0 = grid[0].index("\x83")
        assert grid[0].rstrip().endswith("\x83 alice 1h12m, 1 waiting"), repr(grid[0])
        assert set(roles[b0:39]) == {"g"} and cells[b0] == PALETTE[6]     # title-held
        status = grid
        assert h.req({"op": "panel", "page": "apps"}) == {"ok": True, "op": "panel", "page": "apps"}
        deadline = time.monotonic() + 10
        while panel_frame(h)[0][2] == status[2] and time.monotonic() < deadline:
            time.sleep(0.05)
        rows, roles, grid, cells, _n = verb_equals_mirror(c, h)
        assert grid[2:10] != status[2:10], "the apps page is a different picture"
        assert h.req({"op": "panel"})["page"] == "apps"
        c.close()
    finally:
        h.stop()


@need_hd
def test_contract_fields_version_and_stats(tmp_path):
    h = start_hd(tmp_path)
    try:
        with h.ctl() as ctl:
            ctl.send({"op": "version"})
            raw = ctl.line()
            ver = json.loads(raw)
            # engine names after the bits, in a fixed order (v0.16 adds "identity"
            # and "locate", v0.17 "presence" and "panel")
            assert ver["features"][-5:] == ["lcd_mirror", "identity", "locate", "presence",
                                            "panel"]
            assert ver["lcd_mirror"] == {"port": 6940, "mode": "sw", "proto": 1}
            assert list(ver)[-2:] == ["lcd_mirror", "impl"] and raw.endswith(b',"impl":"linux"}')
            st = ctl.req({"op": "stats"})
            assert st["lcd_mirror"] == {"peer": None, "since": 0, "fps": 0, "bytes": 0}
            assert list(st)[-2:] == ["lcd_mirror", "os_up_ms"]
            c = keyed(h.port(6940))
            painted(c)
            c.pump_for(1.0)
            time.sleep(0.3)                             # the status page: every 250 ms
            st = ctl.req({"op": "stats"})
            lm = st["lcd_mirror"]
            assert lm["peer"] == f"127.0.0.1:{c.sock.getsockname()[1]}"
            assert 0 < lm["since"] <= st["up_ms"] and lm["bytes"] > 5000 and lm["fps"] > 0
            c.close()
    finally:
        h.stop()


@need_hd
def test_lcdmirror_none_disables_everything(tmp_path):
    h = start_hd(tmp_path, extra=["--lcdmirror", "none"])
    try:
        ver = h.req({"op": "version"})
        assert "lcd_mirror" not in ver["features"] and "lcd_mirror" not in ver
        assert "lcd_mirror" not in h.req({"op": "stats"})
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", h.port(6940)), timeout=1).close()
    finally:
        h.stop()


@need_hd
def test_refusal_non_loopback_peer(tmp_path):
    """--mock-trusted-peer 127.0.0.2: only that address counts as loopback, so a
    client from 127.0.0.1 is the 'remote' peer."""
    h = start_hd(tmp_path, extra=["--mock-trusted-peer", "127.0.0.2"])
    try:
        port = h.port(6940)
        connect(port, bind="127.0.0.2").close()        # the trusted one is served
        with pytest.raises(Refused) as e:
            MirrorClient(port, bind="127.0.0.1")
        assert e.value.reply == {"ok": False, "err": "lcd_mirror: not loopback (use an ssh port forward)"}
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        data = b""
        while True:                                     # ONE line, then EOF
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        assert data.count(b"\n") == 1 and data.endswith(b"\n")
    finally:
        h.stop()


@need_hd
def test_refusal_third_client_then_a_free_slot(tmp_path):
    h = start_hd(tmp_path)
    try:
        port = h.port(6940)
        a, b = connect(port), connect(port)
        with pytest.raises(Refused) as e:
            MirrorClient(port)
        assert e.value.reply == {"ok": False, "err": "lcd_mirror: busy (2 clients)"}
        a.close()
        deadline = time.monotonic() + 5
        while True:
            try:
                c = MirrorClient(port)
                break
            except Refused:
                assert time.monotonic() < deadline
                time.sleep(0.05)
        b.send_key()
        c.send_key()
        painted(b)
        painted(c)
        b.close()
        c.close()
    finally:
        h.stop()


@need_hd
def test_key_after_a_seq_gap_recovers_exactly(tmp_path):
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940))
        painted(c)
        c.pump_until(lambda c, u: not u.key and u.snap_last, 5)
        dropped = c.next_update(apply=False)            # lost on the way
        u = c.next_update()
        assert c.gaps == 1 and u.seq == dropped.seq + 1
        c.send_key()                                    # amendment 6
        k = c.pump_until(lambda c, u: u.key_first, 5)
        parts = [k]
        while not parts[-1].key_last:
            parts.append(c.next_update())
        assert all(p.key for p in parts) and parts[-1].snap_last
        assert [p.seq for p in parts] == list(range(parts[0].seq, parts[0].seq + len(parts)))
        tiles = {t for p in parts for t, _, _ in p.tiles}
        assert tiles == {t for t in range(300) if parts[0].valid >> t & 1} and len(tiles) == 300
        ap = Aperture(tmp_path / "lcdm.shm")
        converge(c, ap)
        ap.close()
        c.close()
    finally:
        h.stop()


@need_hd
def test_rate_clamp_is_echoed_and_rate_0_pauses(tmp_path):
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940), rate=5)
        painted(c)
        for asked, want in ((200, 30), (7, 7), (0, 0), (1, 1)):
            c.send_rate(asked)
            deadline = time.monotonic() + 3
            while not c.rate_echoes or c.rate_echoes[-1] != want:
                assert time.monotonic() < deadline, (asked, c.rate_echoes)
                try:
                    c.sock.settimeout(0.2)
                    c.next_update()
                except socket.timeout:
                    pass
            if want == 0:                               # paused: nothing, for longer than 1 s
                c.sock.settimeout(1.5)
                with pytest.raises(socket.timeout):
                    c.next_update()
        c.close()
    finally:
        h.stop()


@need_hd
def test_ack_window_holds_two_updates_and_ping_is_answered_anyway(tmp_path):
    h = start_hd(tmp_path)
    try:
        c = connect(h.port(6940), ack=False)            # never ACKs by itself
        c.send_rate(30)
        c.send_key()
        got = []
        c.sock.settimeout(1.5)
        try:
            while True:
                got.append(c.next_update())
        except socket.timeout:
            pass
        assert len(got) == 2, [u.seq for u in got]      # H1: at most 2 unACKed
        c.send_ping(0xC0FFEE)
        c.sock.settimeout(2)
        t, body, _ = c.recv_msg()                       # PONG, window or not
        assert t == 0x13 and struct.unpack("<I", body)[0] == 0xC0FFEE
        c.send_ack(got[0].seq)                          # room for exactly one more
        u = c.next_update()
        assert u.seq == got[1].seq + 1
        c.sock.settimeout(1.0)
        with pytest.raises(socket.timeout):
            c.next_update()
        c.ack = True
        c.send_ack(u.seq)                               # everything ACKed: it flows again
        painted(c)
        c.close()
    finally:
        h.stop()


@need_hd
def test_owner_dut_is_blind_and_the_regain_refills(tmp_path):
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940))
        painted(c)
        resets = c.last.resets
        h.fabric.wr(CLCDKVM, KVM_STATUS, 0x1)           # the DUT owns the panel
        u = c.pump_until(lambda c, u: u.snap_last and u.blind, 5)
        assert u.owner == 1 and u.valid == 0 and u.text_only and not u.exact
        h.fabric.wr(CLCDKVM, KVM_STATUS, 0x0)           # back to the harness ...
        h.fabric.wr(CLCDKVM, KVM_EVENT, 0x81)           # ... through the KVM's reset
        u = c.pump_until(lambda c, u: u.snap_last and not u.blind and c.valid_count() == 300, 15)
        assert u.owner == 0 and u.resets > resets
        screen_ok(c)                                    # clcd_regain()'s full repaint
        c.close()
    finally:
        h.stop()


@need_hd
def test_per_pass_cpu_budget(tmp_path):
    """The tap is cheap per byte, its per-pass time fits inside the clcd drain's
    budget (harnessd.h HARNESSD_CLCD_DRAIN_US: a pass runs clcd_poll() -- <= 1024
    B each, the harnessd build's CLCD_BYTES_PER_PASS -- repeatedly for up to 12 ms,
    and the tap runs inside those writes, fed whole runs by the bus seam's bulk
    tap), the publish is cheap, and the clcd service row never goes sick. (Lane
    CLCD-SPEED moved these from 256 B / 4 ms; tests/test_clcd_speed.c is the
    model behind the new numbers.)"""
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940))
        painted(c)
        c.pump_for(2.0)
        ap = Aperture(tmp_path / "lcdm.shm")
        pass_bytes, pub_ns, tap_bytes = ap.word(0x1024), ap.word(0x1020), ap.word(0x1028)
        ps_byte = ap.word(0x1030)                       # harnessd's start-up calibration
        tap_us = (pass_bytes * ps_byte / 1e6) + pub_ns / 1e3
        d = h.req({"op": "diag"})
        print(f"\n  per pass: <= {pass_bytes} CLCD bytes tapped x {ps_byte / 1000:.2f} ns "
              f"+ publish <= {pub_ns / 1000:.1f} us = <= {tap_us:.1f} us of mirror work "
              f"(the clcd row's budget is 30000 us); {tap_bytes} bytes tapped in all")
        assert "ns/byte => <=" in h.log()
        # bytes per PASS = per drain: more than one clcd_poll's 1024 B proves
        # svc_clcd drains (clcd_poll_drain) rather than polling once a pass
        assert pass_bytes > 1024
        assert tap_bytes >= 164000                      # the whole boot screen went through it
        assert 0 < ps_byte < 200_000                    # < 200 ns/byte even on a loaded host
        # the tap runs inside the drain's writes, so its time is bounded by the
        # drain budget (12 ms) + one clcd_poll's overshoot, never added to it
        assert pass_bytes * ps_byte / 1e6 < 12000.0 + 1024 * ps_byte / 1e6
        assert pub_ns / 1e3 < 1000.0                    # the publish: < 1 ms even here
        assert d["svc_skipped"] == 0                    # no service went sick
        ap.close()
        c.close()
    finally:
        h.stop()


@need_hd
def test_child_is_supervised_and_dies_with_harnessd(tmp_path):
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940))
        painted(c)
        ap = Aperture(tmp_path / "lcdm.shm")
        pid = ap.word(0x1108)
        os.kill(pid, signal.SIGKILL)
        with pytest.raises((EOFError, ConnectionError, OSError)):
            c.pump_for(3.0)
            c.next_update()
        c2 = keyed(h.port(6940))                        # respawned (1 s backoff)
        painted(c2)
        assert ap.word(0x1108) != pid
        assert "lcd mirror: child killed by signal 9" in h.log()
        c2.close()
        newpid = ap.word(0x1108)
    finally:
        h.stop()
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(newpid, 0)                              # no orphan holding 6940
    ap.close()


# --------------------------------------------------------------------------- #
# the split keyframe and dirty-only updates, on incompressible content
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not Path(LCDM).exists(), reason="make host")
def test_max_msg_split_keyframe_of_noise(tmp_path):
    frame = noise()
    s = Standalone(tmp_path, frame)
    try:
        c = keyed(s.port, rate=30)
        parts = [c.pump_until(lambda c, u: u.key_first, 5)]
        while not parts[-1].key_last:
            parts.append(c.next_update())
        assert len(parts) >= 3 and all(p.size <= c.hello["max_msg"] for p in parts)
        assert [p.snap_last for p in parts] == [False] * (len(parts) - 1) + [True]
        assert len({(p.t_ms, p.frames) for p in parts}) == 1          # H1: one SNAP header
        assert {e for p in parts for _, e, _ in p.tiles} == {ENC_RAW}
        assert list(c.frame) == frame
        c.close()
    finally:
        s.close()


@pytest.mark.skipif(not Path(LCDM).exists(), reason="make host")
def test_dirty_tiles_only_and_dirty_is_not_changed(tmp_path):
    frame = noise(3)
    s = Standalone(tmp_path, frame)
    try:
        c = keyed(s.port, rate=30)
        c.pump_until(lambda c, u: u.key_last, 5)
        # tile 7 changes, tile 8 is marked dirty but rewritten identically
        for yy in range(16):
            for xx in range(16):
                frame[yy * W + 7 * 16 + xx] = 0x1234
        s.ap.m[0x10000:0x10000 + W * H * 2] = array("H", frame).tobytes()
        s.ap.put(0x1040, (1 << 7) | (1 << 8))           # the live dirty map
        u = c.pump_until(lambda c, u: u.tiles, 5)
        assert [(t, e) for t, e, _ in u.tiles] == [(7, 0)]              # FILL, and only 7
        assert list(c.frame) == frame
        c.close()
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 3. NEGATIVE CONTROLS: a broken encoder in the running server must be caught
# --------------------------------------------------------------------------- #
@need_hd
@pytest.mark.parametrize("mutation", ["tile_idx", "pal_bits", "rle_len", "px_endian"])
def test_negative_control_encoder_mutation_is_caught(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv("MPS3_LCDMIRROR_MUTATE", mutation)
    h = start_hd(tmp_path)
    try:
        c = keyed(h.port(6940))
        ap = Aperture(tmp_path / "lcdm.shm")
        caught = None
        try:
            painted(c)
            converge(c, ap, timeout=3.0)
        except (AssertionError, ProtocolError, TimeoutError) as e:
            caught = e
        assert caught is not None, f"mutation {mutation} was NOT caught"
        ap.close()
        c.close()
    finally:
        h.stop()
    assert f"TEST MUTATION {mutation}" in h.log()
