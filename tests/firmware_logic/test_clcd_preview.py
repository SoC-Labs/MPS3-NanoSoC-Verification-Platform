"""firmware/clcd/tools/clcd_preview: its --json is valid JSON on every frame (HM
CLCD_ALIGNMENT §1.4: the heartbeat's backslash once broke it), it carries the
colour roles, and --png draws the aligned look from the real render loop.

Builds the preview with the host gcc exactly as its header says, into tmp_path.
Board-free, stdlib + pytest.
"""
from __future__ import annotations

import json
import pathlib
import struct
import subprocess
import zlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FW = ROOT / "firmware"


@pytest.fixture(scope="module")
def preview(tmp_path_factory) -> pathlib.Path:
    exe = tmp_path_factory.mktemp("preview") / "clcd_preview"
    subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror",
                    "-DMPS3_HAL_MOCK", "-DMPS3_CLCD_TEST_HOOKS",
                    f"-I{FW / 'common'}", f"-I{FW / 'test'}",
                    str(FW / "clcd" / "tools" / "clcd_preview.c"), str(FW / "clcd" / "clcd.c"),
                    str(FW / "clcd" / "hx8347_init.c"), str(FW / "test" / "mock_regs.c"),
                    "-o", str(exe)], check=True)
    return exe


def test_json_parses_and_carries_roles(preview):
    out = subprocess.run([str(preview), "--json"], check=True, capture_output=True, text=True).stdout
    frames = json.loads(out)                       # the whole document, every frame
    names = [f["name"] for f in frames]
    assert "healthy" in names and "aligned-program" in names and len(names) == len(set(names))
    saw_backslash = saw_glyph = False
    for f in frames:
        assert f["theme"] == ("aligned" if f["name"].startswith("aligned-") else "today")
        assert len(f["rows"]) == 15
        for row in f["rows"]:
            assert len(row["t"]) == 40, (f["name"], row["t"])
            assert len(row["roles"]) == 40 and set(row["roles"]) <= set("abcdefghijklmnopqrstu")
            saw_backslash |= "\\" in row["t"]
            saw_glyph |= any(0x80 <= ord(ch) <= 0x86 for ch in row["t"])
            assert all(0x20 <= ord(ch) <= 0x7E or 0x80 <= ord(ch) <= 0x86 for ch in row["t"])
            if row["inv"]:                          # an inverted row is a banner role
                assert set(row["roles"]) <= set("qrstu"), (f["name"], row)
    assert saw_backslash, "no frame caught the '\\' spinner phase: the escape is untested"
    assert saw_glyph
    today = next(f for f in frames if f["name"] == "healthy")
    assert all(set(r["roles"]) == {"a"} for r in today["rows"])   # bare metal: plain text
    prog = next(f for f in frames if f["name"] == "aligned-program")
    assert prog["rows"][3]["t"].startswith("prog   pushing nanosoc") and "42%" in prog["rows"][3]["t"]
    bar = prog["rows"][10]["roles"]
    assert bar == "o" * 17 + "p" * 23                # 42 % of 40 cells = 17, rounded


def _png_pixels(path: pathlib.Path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    i, idat, w, h = 8, b"", 0, 0
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        tag, body = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        crc = struct.unpack(">I", data[i + 8 + n:i + 12 + n])[0]
        assert zlib.crc32(tag + body) & 0xFFFFFFFF == crc
        if tag == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        if tag == b"IDAT":
            idat += body
        i += 12 + n
    raw = zlib.decompress(idat)
    stride = 1 + 3 * w
    return w, h, lambda x, y: tuple(raw[y * stride + 1 + 3 * x: y * stride + 4 + 3 * x])


def test_png_is_the_aligned_look(preview, tmp_path):
    r = subprocess.run([str(preview), "--png", str(tmp_path), "--only", "aligned-program,healthy"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    w, h, px = _png_pixels(tmp_path / "aligned-program.png")
    assert (w, h) == (320, 240)
    assert px(4, 168) == (0x7B, 0x9E, 0xF7)          # the bar: accent 0x7CFE on the glass
    assert px(300, 168) == (0x18, 0x20, 0x29)        # the track: surface-2 0x1905
    assert px(300, 4) == (0x18, 0x20, 0x29)          # the title bar
    w2, h2, _ = _png_pixels(tmp_path / "aligned-program@2x.png")
    assert (w2, h2) == (640, 480)
    _, _, today = _png_pixels(tmp_path / "healthy.png")
    colours = {today(x, y) for x in range(0, 320, 3) for y in range(0, 240, 3)}
    assert colours <= {(0, 0, 0), (255, 255, 255)}   # bare metal: white on black only
