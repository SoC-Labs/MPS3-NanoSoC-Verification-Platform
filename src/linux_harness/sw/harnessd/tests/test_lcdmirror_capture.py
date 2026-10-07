"""test_lcdmirror_capture.py -- scripts/linux_board/lcdmirror_capture.py, the
golden-fixture capture for Harness Manager's LCD-reflector replay test
(docs/planning/linux_lanes/LCDMIRROR_GOLDEN_CAPTURE.md), against the REAL
harnessd host build + its mps3-lcdmirror child over real sockets:

  - key, apps, status and mid-swap scenes from ONE recorded 6940 connection; the
    page changes go through `panel page` (v0.17) on 6900, the swap through a
    --swap-cmd that pushes a clearing + partial like pyverify does;
  - THE REPLAY, judged by code the capture did not use: stream.bin through
    tests/lcdmirror_client.py's decoder (not lcdmirror_grab.py's) up to each
    scene's stream_end equals the scene's .rgb565 AND its PNG (a stdlib PNG
    reader), at the recorded seq;
  - the scenes are what they say: apps and status read (ocr_cells, HM's palette)
    as those pages, a mid-swap frame differs from the status frame before it;
  - --verify passes on the capture; NEGATIVE CONTROLS: one tile byte flipped in
    stream.bin (manifest sha fixed up) fails --verify and the replay, and a
    stream cut short loses the scenes after the cut;
  - a non-empty --out is refused (never overwrite a capture).
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import textwrap
from array import array
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[4]
CAPTURE = REPO / "scripts" / "linux_board" / "lcdmirror_capture.py"

from harnessd_mock import Harnessd  # noqa: E402
from lcdmirror_client import (  # noqa: E402
    HDR, S_SNAP_LAST, T_HELLO, T_UPDATE, decode_tile, load_font, load_font_ext, load_palette,
    ocr_cells, parse_update,
)
from test_lcdmirror_grab import read_png_rgb  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
LCDM = str(Path(BIN).parent / "mps3-lcdmirror")
SID = 0x5A5A0001
W, H = 320, 240
FONT_ALL = {**load_font(REPO / "firmware" / "clcd" / "font8x16.h"),
            **load_font_ext(REPO / "firmware" / "clcd" / "clcd_glyphs.h")}
PALETTE = load_palette(REPO / "firmware" / "clcd" / "clcd_palette.h")

need_hd = pytest.mark.skipif(not Path(BIN).exists() or not Path(LCDM).exists(),
                             reason="make host (mps3-harnessd + mps3-lcdmirror)")

#: the --swap-cmd for the mock: pyverify's order (swap on 6900, then the clearing and
#: the partial on 6910), with pauses so the panel shows the swap at 30 SNAPs/s
SWAP_CMD = textwrap.dedent("""\
    import json, socket, struct, sys, time, zlib
    sys.path.insert(0, {tests!r}); sys.path.insert(0, {pyverify!r})
    from harnessd_mock import frame, clearing_payload, partial_payload
    from pyverify import pusher
    ctrl, push, rm = int(sys.argv[1]), int(sys.argv[2]), 0x0100001E
    s = socket.create_connection(("127.0.0.1", ctrl), timeout=30)
    f = s.makefile("rb")
    for _ in range(100):
        s.sendall(b'{{"op":"ping"}}\\n')
        if f.readline():
            break
        s.close(); time.sleep(0.1)
        s = socket.create_connection(("127.0.0.1", ctrl), timeout=30); f = s.makefile("rb")
    s.sendall(json.dumps({{"op": "swap", "rm": "rm%d" % rm, "src": "tcp"}}).encode() + b"\\n")
    time.sleep(1.0)
    pusher.tcp_send(frame(clearing_payload(), kind=0, static_id={sid}, rm_id=rm), "127.0.0.1", push)
    time.sleep(1.0)
    pusher.tcp_send(frame(partial_payload(rm), kind=1, static_id={sid}, rm_id=rm), "127.0.0.1", push)
    print(f.readline().decode().strip())
""")


def _capture(*args, timeout: float = 240.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CAPTURE)] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=timeout)


def _replay(stream: bytes, ends: dict) -> dict:
    """stream.bin through lcdmirror_client's decoder (NOT the capture's): the frame
    composited at each snap_last, kept at the offsets in `ends` -> {end: (seq, px)}."""
    live = array("H", bytes(W * H * 2))
    frame = array("H", bytes(W * H * 2))
    out, o, first = {}, 0, True
    while o + HDR.size <= len(stream):
        magic, typ, _r, n = HDR.unpack_from(stream, o)
        assert magic == b"LM", f"offset {o}"
        if o + HDR.size + n > len(stream):
            break
        body = stream[o + HDR.size:o + HDR.size + n]
        o += HDR.size + n
        if first:
            assert typ == T_HELLO
            first = False
            continue
        if typ != T_UPDATE:
            continue
        u, recs = parse_update(body, n + HDR.size)
        for idx, enc, payload in recs:
            px = decode_tile(enc, payload)
            x0, y0 = (idx % 20) * 16, (idx // 20) * 16
            for yy in range(16):
                live[(y0 + yy) * W + x0:(y0 + yy) * W + x0 + 16] = array("H", px[yy * 16:(yy + 1) * 16])
        if u.status & S_SNAP_LAST:
            frame[:] = live
        if o in ends:
            out[o] = (u.seq, frame.tobytes())
    return out


def _rgb888(raw: bytes) -> bytes:
    px = array("H")
    px.frombytes(raw)
    out = bytearray()
    for v in px:
        r, g, b = v >> 11 & 0x1F, v >> 5 & 0x3F, v & 0x1F
        out += bytes([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)])
    return bytes(out)


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("capture")
    if not Path(BIN).exists() or not Path(LCDM).exists():
        pytest.skip("make host (mps3-harnessd + mps3-lcdmirror)")
    h = Harnessd(BIN, tmp / "hd", static_id=SID,
                 extra=["--lcdmirror-shm", str(tmp / "lcdm.shm")]).start()
    try:
        swap = tmp / "swap_mock.py"
        swap.write_text(SWAP_CMD.format(tests=str(HERE), pyverify=str(REPO / "host" / "pyverify"),
                                        sid=SID))
        r = _capture("--port", h.port(6940), "--ctrl-port", h.port(6900), "--out", tmp / "out",
                     "--swap-cmd", f"{sys.executable} {swap} {h.port(6900)} {h.port(6910)}")
        log = h.log()
    finally:
        h.stop()
    return tmp / "out", r, log


@need_hd
def test_capture_writes_every_scene_and_verifies(capture):
    out, r, log = capture
    assert r.returncode == 0, r.stderr + log[-2000:]
    man = json.loads((out / "manifest.json").read_text())
    assert man["format"] == "mps3-lcdmirror-capture/1"
    names = [s["name"] for s in man["scenes"]]
    assert names[:3] == ["key", "apps", "status"] and names[-1] == "after_swap", names
    assert any(n.startswith("midswap_") for n in names), names
    assert man["hello"]["static_id"] == f"0x{SID:08x}"
    assert man["panel"] == {"theme": "aligned", "page_at_start": "status"}
    seqs = [s["seq"] for s in man["scenes"]]
    assert seqs == sorted(seqs) and len(set(s["stream_end"] for s in man["scenes"])) >= 4
    assert '"ok": true' in (out / "swap_cmd.log").read_text().replace('"ok":true', '"ok": true')
    for s in man["scenes"]:
        assert s["valid_tiles"] == 300 and s["owner"] == 0, s
    assert "verify key" in r.stderr and "MISMATCH" not in r.stderr
    v = _capture("--verify", out)
    assert v.returncode == 0, v.stderr


@need_hd
def test_replay_through_an_independent_decoder_equals_png_and_raw(capture):
    """HM's replay test in miniature: the stream -> a decoder the capture did not
    use -> the scene's picture, at the scene's seq."""
    out, r, _ = capture
    assert r.returncode == 0, r.stderr
    man = json.loads((out / "manifest.json").read_text())
    stream = (out / "stream.bin").read_bytes()
    assert hashlib.sha256(stream).hexdigest() == man["stream"]["sha256"]
    got = _replay(stream, {s["stream_end"]: s for s in man["scenes"]})
    for s in man["scenes"]:
        seq, px = got[s["stream_end"]]
        raw = (out / s["rgb565"]).read_bytes()
        assert seq == s["seq"], s["name"]
        assert px == raw, f"{s['name']}: replay != .rgb565"
        assert hashlib.sha256(raw).hexdigest() == s["frame_sha256"]
        assert read_png_rgb(out / s["png"]) == _rgb888(raw), f"{s['name']}: PNG != .rgb565"


@need_hd
def test_the_scenes_are_what_they_say(capture):
    out, r, _ = capture
    assert r.returncode == 0, r.stderr
    man = json.loads((out / "manifest.json").read_text())
    frames = {}
    for s in man["scenes"]:
        a = array("H")
        a.frombytes((out / s["rgb565"]).read_bytes())
        frames[s["name"]] = a
    apps, _ = ocr_cells(FONT_ALL, frames["apps"], PALETTE)
    status, _ = ocr_cells(FONT_ALL, frames["status"], PALETTE)
    assert apps[0].strip().startswith("apps & ports"), apps[0]
    assert status[2].startswith("design ") and status[0].strip() == "MPS3", status[:3]
    mids = [n for n in frames if n.startswith("midswap_")]
    assert all(frames[n] != frames["status"] for n in mids), "a mid-swap frame is a new picture"
    for n in mids:
        grid, _ = ocr_cells(FONT_ALL, frames[n], PALETTE)       # a whole screen mid-swap
        assert grid[0].strip().startswith("MPS3")


@need_hd
def test_negative_controls_corrupted_or_short_stream(capture, tmp_path):
    out, r, _ = capture
    assert r.returncode == 0, r.stderr
    man = json.loads((out / "manifest.json").read_text())
    stream = bytearray((out / "stream.bin").read_bytes())
    apps = next(s for s in man["scenes"] if s["name"] == "apps")
    # one pixel byte of a tile payload inside the apps scene's last UPDATE
    last = next(m for m in man["messages"] if m.get("seq") == apps["seq"])
    k = last["off"] + last["len"] - 1
    bad = tmp_path / "bad"
    bad.mkdir()
    for f in out.iterdir():
        (bad / f.name).write_bytes(f.read_bytes())
    stream[k] ^= 0x5A
    (bad / "stream.bin").write_bytes(bytes(stream))
    m2 = dict(man, stream=dict(man["stream"], sha256=hashlib.sha256(bytes(stream)).hexdigest()))
    (bad / "manifest.json").write_text(json.dumps(m2))
    v = _capture("--verify", bad)
    assert v.returncode == 1 and "MISMATCH" in v.stderr, v.stderr
    got = _replay(bytes(stream), {apps["stream_end"]: apps})
    assert got[apps["stream_end"]][1] != (out / "apps.rgb565").read_bytes()
    # the stale sha alone is caught too
    (bad / "manifest.json").write_text(json.dumps(man))
    assert _capture("--verify", bad).returncode == 1
    # a stream cut short: the scenes after the cut are missing
    short = tmp_path / "short"
    short.mkdir()
    for f in out.iterdir():
        (short / f.name).write_bytes(f.read_bytes())
    cut = bytes((out / "stream.bin").read_bytes()[:apps["stream_end"]])
    (short / "stream.bin").write_bytes(cut)
    (short / "manifest.json").write_text(json.dumps(
        dict(man, stream=dict(man["stream"], sha256=hashlib.sha256(cut).hexdigest()))))
    v = _capture("--verify", short)
    assert v.returncode == 1 and "scenes found" in v.stderr, v.stderr


def test_a_non_empty_out_is_refused(tmp_path):
    (tmp_path / "x").write_text("keep")
    r = _capture("--out", tmp_path, "--port", 1, "--ctrl-port", 1)
    assert r.returncode == 2 and "never overwrite" in r.stderr
    assert (tmp_path / "x").read_text() == "keep"
