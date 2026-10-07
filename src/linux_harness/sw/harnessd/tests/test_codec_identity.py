"""test_codec_identity.py — this lane's net_proto.c changes cannot move a single
bare-metal byte (HARNESSD_CONTRACT.md §9.1).

tests/codec_dump.c encodes 300 x 17 deterministic pseudo-random responses (every
op, success and failure, every usr_access state) through three codecs:
  head  = firmware/common/net_proto.c at the branch point (BASELINE_REV)
  new   = today's net_proto.c with the weak seams  (every bare-metal image)
  linux = today's net_proto.c with strong seams    (mps3-harnessd)

head == new, byte for byte, is the proof that bare metal is untouched. linux is
then held to differing from new ONLY by: `,"id_skew":...` + `,"impl":"linux"`
appended to `version`, `,"os_up_ms":N` appended to `stats`, and the fifteen
omitted `diag` keys removed. The negative control: a strong seam that is NOT
reflected (e.g. an impl the codec ignored) would fail the linux assertions.

v0.15 (the LCD mirror, Linux only) adds three ENGINE seams, all through the same
weak/strong mechanism, so head == new is untouched: linux additionally appends
the engine feature name "lcd_mirror" to `version.features` (after every bit
name), `,"lcd_mirror":{...}` to `version` between id_skew and impl, and
`,"lcd_mirror":{...}` to `stats` before os_up_ms (which stays last).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

TBIN = Path(os.environ.get("HARNESSD_TBIN", Path(__file__).resolve().parent.parent / "build" / "tests"))
OMITTED = {"rx_recover", "rx_dumps", "tx_status_drained", "tx_fifo_full_drops",
           "tx_space_stalls", "tx_iface_errors", "tx_last_status", "rcv_wnd",
           "rcv_ann_wnd", "rx_queued", "pbuf_free", "sndbuf", "snd_wnd",
           "ovlstore_phase", "ovlstore_detail"}


def _dump(name):
    exe = TBIN / name
    if not exe.exists():
        pytest.skip(f"{exe} not built (make tests)")
    return subprocess.run([str(exe)], capture_output=True, check=True).stdout.splitlines()


# v0.13 (D13) moved the bare-metal wire ON PURPOSE, additively and in exactly two
# places: `version.features` gains "usd" (bit 13, APPENDED -- so it is the last
# name whenever the bit is set), and `diag` gains svc_us_6 + usd_boot (diag v9,
# APPENDED as the last two keys). Every other byte of every other line is still
# the branch point's.
V013_DIAG_KEYS = ["svc_us_6", "usd_boot"]
# The codec's names for the bits appended since the branch point, in bit order.
# The dump sets RANDOM feature masks, so any subset of them appears here; a
# bare-metal image never sets the v0.14-amendment bits (weak seams: pinned by
# firmware/test/test_v011_dispatch.c's exact feature array and
# test_slot_dispatch.c's bare-metal build).
APPENDED_FEATURES = ["usd", "slot", "xvc_lock"]


def _appended_names(head: list, new: list) -> list:
    """`new` must be `head` + a non-empty, ordered subset of APPENDED_FEATURES."""
    assert new[: len(head)] == head, (head, new)
    tail = new[len(head):]
    assert tail and tail == [f for f in APPENDED_FEATURES if f in tail], (head, new)
    return tail


def test_bare_metal_bytes_unchanged():
    # The Makefile writes this marker instead of building `head` when BASELINE_REV
    # is not in the repository (a one-commit public export). In the private repo
    # the marker is never written and `head` is built, so this stays a real test.
    marker = TBIN / "codec_dump_head.SKIP"
    if marker.exists() and not (TBIN / "codec_dump_head").exists():
        pytest.skip("SKIP: " + marker.read_text().strip())
    head, new = _dump("codec_dump_head"), _dump("codec_dump_new")
    assert len(head) == len(new) == 300 * 17
    seen = {"features": 0, "diag": 0}
    for i, (a, b) in enumerate(zip(head, new)):
        if a == b:
            continue
        ja, jb = json.loads(a), json.loads(b)
        if "features" in ja:
            _appended_names(ja["features"], jb["features"])
            assert {k: v for k, v in jb.items() if k != "features"} == \
                   {k: v for k, v in ja.items() if k != "features"}
            seen["features"] += 1
        elif "svc_count" in ja:
            assert list(jb) == list(ja) + V013_DIAG_KEYS, (i, a, b)   # appended, order kept
            assert all(ja[k] == jb[k] for k in ja)
            assert a[:-2] == b[: len(a) - 2]                        # the old line is a prefix
            seen["diag"] += 1
        else:
            pytest.fail(f"line {i}: an op v0.13 did not touch changed:\n{a!r}\n{b!r}")
    assert seen["diag"] > 50 and seen["features"] > 50, seen


def test_linux_differs_only_by_the_documented_keys():
    new, lin = _dump("codec_dump_new"), _dump("codec_dump_linux")
    seen = {"version": 0, "stats": 0, "diag": 0}
    for a, b in zip(new, lin):
        if a == b:
            continue
        ja, jb = json.loads(a), json.loads(b)
        if "features" in ja:                                     # version
            assert jb["features"] == ja["features"] + ["lcd_mirror"]   # appended, LAST
            body = re.search(rb'"features":\[([^\]]*)\]', a).group(1)
            a2 = a.replace(b'"features":[' + body + b']',
                           b'"features":[' + body + (b"," if body else b"") + b'"lcd_mirror"]', 1)
            assert b == a2[:-1] + (b',"id_skew":"image 0x0badcafe != fabric 0x5a5a0001"'
                                   b',"lcd_mirror":{"port":6940,"mode":"sw","proto":1}'
                                   b',"impl":"linux"}')
            assert list(jb)[-2:] == ["lcd_mirror", "impl"]
            seen["version"] += 1
        elif "up_ms" in ja and "sid" in ja:                      # stats
            assert b == a[:-1] + (b',"lcd_mirror":{"peer":"127.0.0.1:40123","since":1234,'
                                  b'"fps":4.5,"bytes":987654},"os_up_ms":987654321}')
            seen["stats"] += 1
        elif "svc_count" in ja:                                  # diag
            assert set(ja) - set(jb) == OMITTED and set(jb) <= set(ja)
            assert list(jb) == [k for k in ja if k not in OMITTED]   # order kept
            assert all(ja[k] == jb[k] for k in jb)
            seen["diag"] += 1
        else:
            pytest.fail(f"an op the lane did not touch changed:\n{a!r}\n{b!r}")
    assert all(v > 50 for v in seen.values()), seen
