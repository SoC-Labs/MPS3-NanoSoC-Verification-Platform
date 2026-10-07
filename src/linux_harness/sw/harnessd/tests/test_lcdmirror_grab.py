"""test_lcdmirror_grab.py -- scripts/linux_board/lcdmirror_grab.py (the host
tool that grabs one PNG of the harness CLCD from the LCD mirror's 6940 wire),
and the 6940 client policy the grab-then-display order depends on.

1. THE DECODER, judged by something it did not produce: the checked-in wire
   vectors (tests/fixtures/lcdmirror_wire, made by lcdmirror_vectors.c from
   known pictures) carry per-tile and whole-frame CRCs in manifest.json. The
   grab tool's own decoder (it shares no code with lcdmirror_client.py) must
   reproduce every one of them -- per encoding, and the split keyframe served
   over a REAL socket by a fake board. A flipped payload bit must change the
   frame (negative control). Its PNG (Pillow and the zlib fallback) and PPM
   must carry exactly the RGB888 widening of that frame.
2. CONNECT RETRIES against a fake board: nothing listening yet, a reset before
   HELLO, the "busy (2 clients)" line -- each retried; "not loopback" is not.
3. LIVE: host harnessd (MOCK fabric) + its mps3-lcdmirror child. The grab reads
   the harness status screen (every cell one glyph of clcd.c's font) and equals
   the aperture; grab-then-display back to back never sees busy; a closed
   client's slot is reusable AT ONCE (no retry); a busy grab waits for a slot;
   `version.features` names "lcd_mirror" only when the server binary exists.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from array import array
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[4]
GRAB = REPO / "scripts" / "linux_board" / "lcdmirror_grab.py"

from harnessd_mock import Harnessd  # noqa: E402
from lcdmirror_client import (MirrorClient, Refused, load_font, load_font_ext,  # noqa: E402
                              load_palette, ocr_cells)

_spec = importlib.util.spec_from_file_location("lcdmirror_grab", GRAB)
grab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grab)

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
LCDM = str(Path(BIN).parent / "mps3-lcdmirror")
FIX_WIRE = HERE / "fixtures" / "lcdmirror_wire"
MANIFEST = {v["kind"] + ":" + v["file"]: v
            for v in json.loads((FIX_WIRE / "manifest.json").read_text())["vectors"]}
KEYFRAME = next(v for v in MANIFEST.values() if v["kind"] == "keyframe")
FONT = load_font(REPO / "firmware" / "clcd" / "font8x16.h")
#: harnessd's default look: ASCII + HM's status glyphs in HM's role pairs
FONT_ALL = {**FONT, **load_font_ext(REPO / "firmware" / "clcd" / "clcd_glyphs.h")}
PALETTE = load_palette(REPO / "firmware" / "clcd" / "clcd_palette.h")
SID = 0x5A5A0001
W, H = 320, 240

need_hd = pytest.mark.skipif(not Path(BIN).exists() or not Path(LCDM).exists(),
                             reason="make host (mps3-harnessd + mps3-lcdmirror)")

try:
    import PIL.Image  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def _crc(px) -> int:
    return zlib.crc32(array("H", px).tobytes()) & 0xFFFFFFFF


def _body(name: str) -> bytes:
    raw = (FIX_WIRE / name).read_bytes()
    magic, _typ, _rsvd, n = struct.unpack_from("<2sBBI", raw)
    assert magic == b"LM" and len(raw) == 8 + n
    return raw[8:]


def run_grab(*args, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GRAB)] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=timeout)


def attempts_of(stderr: str) -> int:
    for tok in ("connect attempts", "connect attempt"):
        i = stderr.find(tok)
        if i > 0:
            return int(stderr[:i].split()[-1])
    raise AssertionError(f"no attempt count in: {stderr}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def read_png_rgb(path: Path) -> bytes:
    """A stdlib PNG reader for 8-bit RGB, non-interlaced, any of the 5 filters
    (Pillow's writer uses them): the pixels, row-major."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    o, idat, ihdr = 8, b"", None
    while o < len(data):
        (n,) = struct.unpack_from(">I", data, o)
        tag, body = data[o + 4:o + 8], data[o + 8:o + 8 + n]
        (crc,) = struct.unpack_from(">I", data, o + 8 + n)
        assert zlib.crc32(tag + body) & 0xFFFFFFFF == crc, tag
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        o += 12 + n
    w, h, depth, ctype, _c, _f, interlace = ihdr
    assert (w, h, depth, ctype, interlace) == (W, H, 8, 2, 0)
    raw = zlib.decompress(idat)
    stride, bpp = w * 3, 3
    out, prev, o = bytearray(), bytearray(stride), 0
    for _ in range(h):
        f, line = raw[o], bytearray(raw[o + 1:o + 1 + stride])
        o += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out += line
        prev = line
    return bytes(out)


# --------------------------------------------------------------------------- #
# a fake board: serves the checked-in vectors over a real socket
# --------------------------------------------------------------------------- #
class FakeBoard:
    """Listens on 127.0.0.1:port. For connection k (0-based) `plan(k)` names the
    behaviour: "serve" (HELLO + the keyframe parts, then waits for the client to
    leave), "reset" (accept, RST), "busy" / "not_loopback" (the refusal line)."""

    def __init__(self, port: int, plan=lambda k: "serve", parts=None):
        self.port, self.plan = port, plan
        self.parts = parts if parts is not None else \
            [(FIX_WIRE / p).read_bytes() for p in KEYFRAME["parts"]]
        self.hello = (FIX_WIRE / "hello.bin").read_bytes()
        self.conns, self.received = 0, []
        self.lsock = socket.socket()
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(("127.0.0.1", port))
        self.lsock.listen(8)
        self.lsock.settimeout(0.2)
        self._stop = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self._stop:
            try:
                c, _ = self.lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            k = self.conns
            self.conns += 1
            what = self.plan(k)
            try:
                if what == "reset":
                    c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                elif what in ("busy", "not_loopback"):
                    c.sendall((FIX_WIRE / f"refusal_{what}.txt").read_bytes())
                else:
                    c.sendall(self.hello)
                    c.settimeout(0.5)
                    got = b""
                    try:                                  # the KEY first (amendment 6)
                        got = c.recv(64)
                    except socket.timeout:
                        pass
                    for p in self.parts:
                        c.sendall(p)
                    c.settimeout(5)
                    while True:                           # drain ACKs until it leaves
                        chunk = c.recv(4096)
                        if not chunk:
                            break
                        got += chunk
                    self.received.append(got)
            except OSError:
                pass
            finally:
                c.close()

    def close(self):
        self._stop = True
        self.t.join(2)
        self.lsock.close()


def _client_msgs(raw: bytes):
    out, o = [], 0
    while o + 8 <= len(raw):
        magic, typ, _r, n = struct.unpack_from("<2sBBI", raw, o)
        assert magic == b"LM"
        out.append((typ, raw[o + 8:o + 8 + n]))
        o += 8 + n
    return out


# --------------------------------------------------------------------------- #
# 1. the decoder vs the vectors
# --------------------------------------------------------------------------- #
def test_grab_decoder_reproduces_every_vector_tile_crc():
    encs = set()
    for v in MANIFEST.values():
        if v["kind"] != "update":
            continue
        hdr, recs = grab.parse_update(_body(v["file"]))
        assert (hdr["seq"], hdr["t_ms"], hdr["status"], hdr["owner"]) == \
            (v["seq"], v["t_ms"], v["status"], v["owner"])
        assert [(i, e, len(p)) for i, e, p in recs] == [tuple(t[:3]) for t in v["tiles"]]
        for (idx, enc, payload), t in zip(recs, v["tiles"]):
            assert _crc(grab.decode_tile(enc, payload)) == t[3], (v["file"], idx, enc)
            encs.add(enc)
    assert encs == {grab.ENC_FILL, grab.ENC_PAL1, grab.ENC_PAL2, grab.ENC_RLE16, grab.ENC_RAW}


def test_grab_decoder_rejects_malformed_payloads():
    for enc, payload in ((grab.ENC_FILL, b"\0"), (grab.ENC_PAL1, bytes(35)), (grab.ENC_PAL2, bytes(71)),
                         (grab.ENC_RAW, bytes(510)), (grab.ENC_RLE16, b"\x80\x01"),
                         (grab.ENC_RLE16, b"\xff\0\0"), (7, b"")):
        with pytest.raises(grab.GrabError):
            grab.decode_tile(enc, payload)


def test_grab_rebuilds_the_keyframe_vector_over_a_socket(tmp_path):
    port = free_port()
    fb = FakeBoard(port)
    try:
        r = run_grab("--port", port, "-o", tmp_path / "k.png", "--raw", tmp_path / "k.raw",
                     "--no-pillow", "--timeout", 10)
    finally:
        fb.close()
    assert r.returncode == 0, r.stderr
    raw = (tmp_path / "k.raw").read_bytes()
    assert len(raw) == W * H * 2
    assert zlib.crc32(raw) & 0xFFFFFFFF == KEYFRAME["frame_crc"]
    assert "300/300 tiles valid" in r.stderr and "1 connect attempt" in r.stderr
    frame = array("H")
    frame.frombytes(raw)
    assert read_png_rgb(tmp_path / "k.png") == grab.rgb888(frame)          # the zlib writer
    # the client spoke the wire: KEY first, then one ACK per UPDATE, in order
    msgs = _client_msgs(fb.received[0])
    assert msgs[0] == (grab.T_KEY, b"")
    seqs = [struct.unpack("<I", b)[0] for t, b in msgs[1:] if t == grab.T_ACK]
    first = KEYFRAME["seq_first"]
    assert seqs == list(range(first, first + len(KEYFRAME["parts"])))


def test_negative_control_a_flipped_payload_bit_changes_the_frame(tmp_path):
    parts = [(FIX_WIRE / p).read_bytes() for p in KEYFRAME["parts"]]
    # find the first RAW tile in part 2 and flip one bit mid-payload
    b = bytearray(parts[1])
    body = b[8:]
    hdr, recs = grab.parse_update(bytes(body))
    o = grab.UPD.size + (256 if hdr["regs"] is not None else 4) + 2
    for idx, enc, payload in recs:
        o += grab.TREC.size
        if enc == grab.ENC_RAW:
            b[8 + o + len(payload) // 2 + 1] ^= 0x01    # one bit of one pixel
            break
        o += len(payload)
    else:
        pytest.fail("no RAW tile in part 2")
    parts[1] = bytes(b)
    port = free_port()
    fb = FakeBoard(port, parts=parts)
    try:
        r = run_grab("--port", port, "-o", tmp_path / "k.png", "--raw", tmp_path / "k.raw",
                     "--timeout", 10)
    finally:
        fb.close()
    assert r.returncode == 0, r.stderr
    assert zlib.crc32((tmp_path / "k.raw").read_bytes()) & 0xFFFFFFFF != KEYFRAME["frame_crc"]


def _as_keyframe(names):
    """The five per-encoding UPDATE vectors, re-flagged as ONE keyframe: only
    header words change (seq, the key/snap bits, an all-valid map, REGS in place
    of MODE on the first part) -- every tile record is the C builder's bytes."""
    out = []
    for i, name in enumerate(names):
        body = _body(name)
        seq, t_ms, frames, resets, status, owner, _valid = grab.UPD.unpack_from(body, 0)
        status &= ~(grab.S_KEY | grab.S_KEY_FIRST | grab.S_KEY_LAST | grab.S_SNAP_LAST)
        status |= grab.S_KEY
        if i == 0:
            status |= grab.S_KEY_FIRST
        if i == len(names) - 1:
            status |= grab.S_KEY_LAST | grab.S_SNAP_LAST
        valid = b"\xff" * 37 + b"\x0f"
        rest = body[grab.UPD.size + 4:]                   # after MODE: ntiles + records
        mid = bytes(256) if i == 0 else body[grab.UPD.size:grab.UPD.size + 4]
        new = grab.UPD.pack(100 + i, t_ms, frames, resets, status, owner, valid) + mid + rest
        out.append(struct.pack("<2sBBI", b"LM", grab.T_UPDATE, 0, len(new)) + new)
    return out


def test_grab_decodes_every_encoding_over_a_socket(tmp_path):
    updates = [v for v in MANIFEST.values() if v["kind"] == "update"]
    assert {t[1] for v in updates for t in v["tiles"]} == {0, 1, 2, 3, 4}
    port = free_port()
    fb = FakeBoard(port, parts=_as_keyframe([v["file"] for v in updates]))
    try:
        r = run_grab("--port", port, "-o", tmp_path / "e.png", "--raw", tmp_path / "e.raw")
    finally:
        fb.close()
    assert r.returncode == 0, r.stderr
    frame = array("H")
    frame.frombytes((tmp_path / "e.raw").read_bytes())
    seen = set()
    for v in updates:
        for idx, enc, _ln, crc in v["tiles"]:
            x0, y0 = (idx % 20) * 16, (idx // 20) * 16
            px = [frame[(y0 + yy) * W + x0 + xx] for yy in range(16) for xx in range(16)]
            assert _crc(px) == crc, (v["file"], idx, enc)
            seen.add(idx)
    assert all(frame[i] == 0 for i in range(W * H)
               if ((i // W) // 16) * 20 + (i % W) // 16 not in seen)   # nothing else drawn


@pytest.mark.skipif(not HAVE_PIL, reason="Pillow not installed")
def test_pillow_png_and_ppm_carry_the_same_pixels(tmp_path):
    port = free_port()
    fb = FakeBoard(port)
    try:
        r1 = run_grab("--port", port, "-o", tmp_path / "p.png", "--raw", tmp_path / "p.raw")
        r2 = run_grab("--port", port, "-o", tmp_path / "p.ppm")
    finally:
        fb.close()
    assert r1.returncode == 0 and r2.returncode == 0, r1.stderr + r2.stderr
    assert "png (Pillow)" in r1.stderr
    frame = array("H")
    frame.frombytes((tmp_path / "p.raw").read_bytes())
    want = grab.rgb888(frame)
    from PIL import Image
    assert Image.open(tmp_path / "p.png").convert("RGB").tobytes() == want
    assert read_png_rgb(tmp_path / "p.png") == want
    ppm = (tmp_path / "p.ppm").read_bytes()
    assert ppm.startswith(b"P6\n320 240\n255\n") and ppm.endswith(want)


def test_rgb565_widening_is_exact_at_the_rails():
    assert grab.rgb888([0xFFFF, 0x0000, 0xF800, 0x07E0, 0x001F]) == bytes(
        [255, 255, 255, 0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255])


# --------------------------------------------------------------------------- #
# 2. connect retries (fake board)
# --------------------------------------------------------------------------- #
def test_grab_retries_until_the_port_listens(tmp_path):
    port = free_port()
    p = subprocess.Popen([sys.executable, str(GRAB), "--port", str(port), "-o", str(tmp_path / "a.png"),
                          "--retries", "8"], stderr=subprocess.PIPE, text=True)
    time.sleep(1.2)                                   # refused at least twice
    fb = FakeBoard(port)
    try:
        _, err = p.communicate(timeout=30)
    finally:
        fb.close()
    assert p.returncode == 0, err
    assert "Connection refused" in err or "Errno 111" in err
    assert attempts_of(err) >= 2


@pytest.mark.parametrize("what", ["reset", "busy"])
def test_grab_retries_a_reset_and_a_busy_line(tmp_path, what):
    port = free_port()
    fb = FakeBoard(port, plan=lambda k: what if k < 2 else "serve")
    try:
        r = run_grab("--port", port, "-o", tmp_path / "a.png", "--raw", tmp_path / "a.raw")
    finally:
        fb.close()
    assert r.returncode == 0, r.stderr
    assert attempts_of(r.stderr) == 3 and fb.conns == 3
    assert ("busy (2 clients)" in r.stderr) == (what == "busy")
    assert zlib.crc32((tmp_path / "a.raw").read_bytes()) & 0xFFFFFFFF == KEYFRAME["frame_crc"]


def test_grab_does_not_retry_not_loopback(tmp_path):
    port = free_port()
    fb = FakeBoard(port, plan=lambda k: "not_loopback")
    try:
        r = run_grab("--port", port, "-o", tmp_path / "a.png", "--retries", "5")
    finally:
        fb.close()
    assert r.returncode == 1 and "not loopback" in r.stderr and "retry" not in r.stderr
    assert fb.conns == 1 and not (tmp_path / "a.png").exists()


def test_grab_gives_up_after_its_retries(tmp_path):
    r = run_grab("--port", free_port(), "-o", tmp_path / "a.png", "--retries", "2")
    assert r.returncode == 1 and "gave up after 3 attempts" in r.stderr


# --------------------------------------------------------------------------- #
# 3. live: host harnessd + mps3-lcdmirror
# --------------------------------------------------------------------------- #
def start_hd(tmp: Path, extra=None) -> Harnessd:
    return Harnessd(BIN, tmp, static_id=SID,
                    extra=["--lcdmirror-shm", str(tmp / "lcdm.shm")] + (extra or [])).start()


def wait_listening(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            MirrorClient(port).close()
            return
        except Refused:
            return                          # a refusal line: it listens
        except (ConnectionRefusedError, ConnectionResetError, EOFError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _aperture_fb(path: Path) -> array:
    a = array("H")
    with open(path, "rb") as f:
        f.seek(0x10000)
        a.frombytes(f.read(W * H * 2))
    return a


def _tile(fb, t: int) -> list:
    x0, y0 = (t % 20) * 16, (t // 20) * 16
    return [fb[(y0 + yy) * W + x0 + xx] for yy in range(16) for xx in range(16)]


@need_hd
def test_grab_live_reads_the_harness_screen(tmp_path):
    h = start_hd(tmp_path)
    try:
        port = h.port(6940)
        wait_listening(port)
        err = None
        for _ in range(5):                  # a keyframe can catch a glyph mid-draw
            before = _aperture_fb(tmp_path / "lcdm.shm")
            r = run_grab("--port", port, "-o", tmp_path / "live.png", "--raw", tmp_path / "live.raw",
                         "--rate", 30)
            assert r.returncode == 0, r.stderr + h.log()[-2000:]
            after = _aperture_fb(tmp_path / "lcdm.shm")
            got = array("H")
            got.frombytes((tmp_path / "live.raw").read_bytes())
            stable = [t for t in range(300) if _tile(before, t) == _tile(after, t)]
            bad = [t for t in stable if _tile(got, t) != _tile(after, t)]
            assert not bad, f"grabbed frame != the aperture harnessd's tap wrote, tiles {bad[:10]}"
            if len(stable) < 250:
                err = f"only {len(stable)} tiles held still"
                continue
            try:
                grid, _cells = ocr_cells(FONT_ALL, got, PALETTE)
                break
            except ValueError as e:
                err = e
        else:
            pytest.fail(f"never a still, whole screen in 5 grabs: {err}")
        assert "300/300 tiles valid" in r.stderr and "text_only" in r.stderr
        assert "0x5a5a0001" in r.stderr
        assert read_png_rgb(tmp_path / "live.png") == grab.rgb888(got)
        assert any(line.strip() for line in grid)
    finally:
        h.stop()


@need_hd
def test_grab_then_display_back_to_back_never_sees_busy(tmp_path):
    """Today's order: the grab tool connects for a few seconds, disconnects, then
    Harness Manager's display connects -- at once, with no retry of its own."""
    h = start_hd(tmp_path)
    try:
        port = h.port(6940)
        wait_listening(port)
        for i in range(4):
            r = run_grab("--port", port, "-o", tmp_path / f"g{i}.png", "--retries", "0", "--rate", 30)
            assert r.returncode == 0, r.stderr
            assert attempts_of(r.stderr) == 1
            with MirrorClient(port) as c:              # no retry: must be served now
                c.send_rate(30)
                c.send_key()
                c.pump_until(lambda c, u: u.key_last, 10)
        deadline = time.monotonic() + 2
        while h.req({"op": "stats"})["lcd_mirror"]["peer"] is not None:
            assert time.monotonic() < deadline, "the last client was never reaped"
            time.sleep(0.1)
    finally:
        h.stop()


@need_hd
def test_a_closed_clients_slot_is_reusable_at_once(tmp_path):
    """Both slots held; close one and connect again IMMEDIATELY, 25 times: never
    the busy line (the hang-up is reaped before the next accept)."""
    h = start_hd(tmp_path)
    try:
        port = h.port(6940)
        wait_listening(port)
        a, b = MirrorClient(port), MirrorClient(port)
        for _ in range(25):
            a.close()
            a = MirrorClient(port)                     # raises Refused on "busy"
        with pytest.raises(Refused):                   # still exactly two slots
            MirrorClient(port)
        for c in (a, b):
            c.send_key()
            c.pump_until(lambda c, u: u.key_last, 10)
            c.close()
    finally:
        h.stop()


@need_hd
def test_grab_waits_for_a_slot_when_the_mirror_is_busy(tmp_path):
    h = start_hd(tmp_path)
    try:
        port = h.port(6940)
        wait_listening(port)
        a, b = MirrorClient(port), MirrorClient(port)
        p = subprocess.Popen([sys.executable, str(GRAB), "--port", str(port), "-o",
                              str(tmp_path / "w.png"), "--retries", "10"],
                             stderr=subprocess.PIPE, text=True)
        time.sleep(0.8)
        a.close()
        _, err = p.communicate(timeout=30)
        b.close()
        assert p.returncode == 0, err
        assert "busy (2 clients)" in err and attempts_of(err) >= 2
    finally:
        h.stop()


@need_hd
def test_grab_live_not_loopback_fails_at_once(tmp_path):
    h = start_hd(tmp_path, extra=["--mock-trusted-peer", "127.0.0.2"])
    try:
        wait_listening(h.port(6940))
        r = run_grab("--port", h.port(6940), "-o", tmp_path / "n.png", "--retries", "5")
        assert r.returncode == 1 and "not loopback" in r.stderr and "retry" not in r.stderr
    finally:
        h.stop()


@need_hd
def test_feature_named_only_when_the_server_binary_exists(tmp_path):
    h = start_hd(tmp_path / "with")
    try:
        ver = h.req({"op": "version"})
        assert "lcd_mirror" in ver["features"] and ver["lcd_mirror"]["port"] == 6940
    finally:
        h.stop()
    not_exec = tmp_path / "not_exec"
    not_exec.write_text("#!/bin/sh\n")
    not_exec.chmod(0o644)
    for path in (tmp_path / "missing" / "mps3-lcdmirror", not_exec):
        h = start_hd(tmp_path / ("hd_" + path.name), extra=["--lcdmirror", str(path)])
        try:
            ver = h.req({"op": "version"})
            assert "lcd_mirror" not in ver["features"] and "lcd_mirror" not in ver
            assert "lcd_mirror" not in h.req({"op": "stats"})
            assert "lcd mirror: " + str(path) in h.log() and "-- disabled" in h.log()
        finally:
            h.stop()
