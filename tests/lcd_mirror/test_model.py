"""test_model.py -- the LCDMIR golden model (hx8347_gram_model.py) and the pad
decoder against INDEPENDENT oracles, and against Harness Manager's golden model.
Pure Python, no simulator: `make pytest` (or `python3 -m pytest test_model.py`).

  1. the platform's recorded clcd.c stream   vs the font-rendered cell shadow
  2. HM's recorded clcd.c streams (fixtures) vs the font-rendered HM grids
  3. clcd_demo repaints                      vs card_model.card_pixel()
  4. MADCTL 0x20 / 0xE0                      vs identity / 180-degree rotation
  5. HM's Hx8347dShadow on the same streams  -> identical viewer frames, and
     compare-on-write dirty sets == HM's `changed` sets
  6. the KNOWN model disagreements with HM, pinned (so a change on either
     side is noticed and re-reported), plus the pad decoder's legal/illegal
     traces.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import hm_fixtures as HF  # noqa: E402
import hx8347_gram_model as M  # noqa: E402
import pad_decoder as P  # noqa: E402
import streams  # noqa: E402

HM = HF.hm_decoder()
needs_hm = pytest.mark.skipif(HM is None, reason=f"HM checkout not at {HF.HM_ROOT}")


def _diff(a, b):
    bad = [i for i in range(len(a)) if a[i] != b[i]]
    return len(bad), (bad[0] % M.W, bad[0] // M.W) if bad else None


@pytest.fixture(scope="module")
def platform_harness():
    return streams.record_harness(streams.work_dir())


# 1 ------------------------------------------------------------------------
def test_platform_harness_stream_vs_shadow(platform_harness):
    m = M.GramModel()
    for sc in platform_harness:
        for e in sc.events:
            if e[0] == "B":
                m.byte(e[1], e[2])
            elif not (e[1] >> 2) & 1:
                m.panel_reset()
        n, at = _diff(m.fb, streams.render_shadow(sc.grid, sc.inv))
        assert n == 0, f"{sc.name}: {n} pixels differ from the shadow oracle, first {at}"


# 2 ------------------------------------------------------------------------
def test_hm_fixture_streams_vs_grids():
    m = M.GramModel()
    for name in HF.SCENARIOS:
        recs = (HF.KVM_RESET if name == "regain" else []) + HF.load_stream(name)
        HF.feed_model(m, recs)
        n, at = _diff(m.fb, streams.render_shadow(*HF.load_grid(name)))
        assert n == 0, f"HM {name}: {n} pixels differ from HM's grid, first {at}"
    assert m.resets == 2        # boot's CTRL reset + the regain KVM pulse


# 3 ------------------------------------------------------------------------
@pytest.mark.parametrize("frame", [0x0000, 0x0005, 0xA5C3])
def test_clcd_demo_vs_card(frame):
    seq, oracle = streams.demo_frame(frame)
    m = M.GramModel()
    m.feed_bytes(seq)
    assert _diff(m.fb, oracle)[0] == 0
    assert m.frames == 1 and m.oob == 0


# 4 ------------------------------------------------------------------------
@pytest.mark.parametrize("conv", [0, 1])
def test_madctl_anchor_and_rotation(conv, platform_harness):
    boot = [(e[1], e[2]) for e in platform_harness[0].events if e[0] == "B"]
    ref = M.GramModel(flip_conv=conv)
    ref.feed_bytes(boot)
    k = next(i for i in range(len(boot) - 1) if boot[i] == (0, 0x16))
    rot = boot[:k + 1] + [(1, 0xE0)] + boot[k + 2:]
    m = M.GramModel(flip_conv=conv)
    m.feed_bytes(rot)
    want = [ref.fb[(M.H - 1 - y) * M.W + (M.W - 1 - x)] for y in range(M.H) for x in range(M.W)]
    assert m.fb == want, "MADCTL 0xE0 must be the 180-degree rotation of 0x20"


def test_flip_conventions_differ_only_on_single_flip_landscape():
    for mad in range(0, 256, 0x20):
        for x, y in ((0, 0), (5, 7), (100, 3), (200, 150)):
            a = M.viewer_xy(x, y, mad, 0)
            b = M.viewer_xy(x, y, mad, 1)
            if mad in (0x60, 0xA0):
                assert a != b
            else:
                assert a == b


# 5 ------------------------------------------------------------------------
def _hm_view(shadow):
    return list(shadow.viewer())


@needs_hm
def test_hm_model_agrees_on_every_stream(platform_harness):
    """Both golden models, fed the same records, show the same picture."""
    mine, theirs = M.GramModel(ac_load=1), HM.Hx8347dShadow()
    history = []
    for name in HF.SCENARIOS:
        history.append((f"hm:{name}", (HF.KVM_RESET if name == "regain" else [])
                        + HF.load_stream(name)))
    for f in (5, 6):
        history.append((f"clcd_demo#{f}", HF.KVM_RESET + list(streams.demo_frame(f)[0])))
    history.append(("nanosoc_ahb_clcd", HF.KVM_RESET + list(HM.nanosoc_demo()[0])))
    plat = []
    for sc in platform_harness:
        for e in sc.events:
            plat.append((2, e[1] & 0xFF) if e[0] == "CTRL" else (e[1], e[2]))
    history.append(("platform:harness", HF.KVM_RESET + plat))
    for label, recs in history:
        HF.feed_model(mine, recs)
        theirs.feed(recs)
        n, at = _diff(mine.fb, _hm_view(theirs))
        assert n == 0, f"{label}: the two golden models differ in {n} pixels, first {at}"


@needs_hm
def test_compare_on_write_dirty_equals_hm_changed():
    """HM §10.3's dirty counts: our compare-on-write dirty set == HM's `changed`
    set, snapshot by snapshot, over HM's whole history."""
    mine, theirs = M.GramModel(dirty_all=0), HM.Hx8347dShadow()
    gw, tile = HM.GW, HM.TILE
    vm = HM.Hx8347dShadow._view_map()
    g2v = {}
    for vi, gi in enumerate(vm):
        g2v[(gi // gw // tile) * (gw // tile) + (gi % gw) // tile] = M.tile_of(vi % M.W, vi // M.W)
    steps = [(n, (HF.KVM_RESET if n == "regain" else []) + HF.load_stream(n)) for n in HF.SCENARIOS]
    steps[3:3] = [("clcd_demo#5", HF.KVM_RESET + list(streams.demo_frame(5)[0])),
                  ("clcd_demo#6", list(streams.demo_frame(6)[0])),
                  ("nanosoc", HF.KVM_RESET + list(HM.nanosoc_demo()[0]))]
    counts = {}
    for label, recs in steps:
        theirs.changed.clear()
        HF.feed_model(mine, recs)
        theirs.feed(recs)
        mine.snap()
        hm_set = {g2v[t] for t in theirs.changed}
        assert mine.snap_dirty == hm_set, f"{label}: dirty {len(mine.snap_dirty)} vs HM {len(hm_set)}"
        counts[label] = len(hm_set)
    # the numbers HM's design doc §10.3 publishes
    assert counts == {"boot": 179, "link_down": 67, "banner": 269, "clcd_demo#5": 291,
                      "clcd_demo#6": 12, "nanosoc": 300, "regain": 297}, counts


# 6 ------------------------------------------------------------------------
def _run_both(recs, **kw):
    mine, theirs = M.GramModel(**kw), HM.Hx8347dShadow()
    HF.feed_model(mine, HF.KVM_RESET + recs)
    theirs.feed(HF.KVM_RESET + recs)
    return mine, theirs


_BASE = [(0, 0x16), (1, 0x20), (0, 0x17), (1, 0x05)]


@needs_hm
def test_known_disagreements_with_hm_model():
    """Where the two golden models differ. Each is an open item, not a bug in
    either; pinned so that a change on either side forces a re-report."""
    win = M.window_pairs(10, 17, 4, 7) + [(0, 0x22)]
    # (a) O1 split 0x22: HM restarts on 0x22 = our ac_load=1; ac_load=0 continues.
    split = _BASE + win + M.pixel_pairs([0xF800] * 5) + [(0, 0x22)] + M.pixel_pairs([0x07E0] * 3)
    mine, theirs = _run_both(split, ac_load=1)
    assert mine.fb == _hm_view(theirs), "HM's AC reload == ac_load=1 (pixel-wise)"
    mine, theirs = _run_both(split, ac_load=0)
    assert mine.fb != _hm_view(theirs), "ac_load=0 is the reading HM does not implement"
    # (b) wrap test: we wrap x at x == EC (9-bit walk when SC > EC); HM at x > EC.
    walk = _BASE + M.window_pairs(318, 1, 30, 30) + [(0, 0x22)] + M.pixel_pairs([0x1234] * 4)
    mine, theirs = _run_both(walk, ac_load=1)
    assert mine.fb != _hm_view(theirs)
    # (c) COLMOD: we decode IFPF = R17[2:0] (0x55 is 16 bpp); HM needs 0x05 exactly.
    cm = ([(0, 0x16), (1, 0x20), (0, 0x17), (1, 0x55)] + win + M.pixel_pairs([0xABCD] * 4))
    mine, theirs = _run_both(cm, ac_load=1)
    assert mine.fb != _hm_view(theirs)
    # (d) display_on: GON.DTE.D (R28 & 0x3C, LCD_MIRROR_FPGA.md §4) vs HM's D only
    #     (R28 & 0x0C); (e) R1F default: STB=1 (ours, O3) vs 0 (HM's).
    m = M.GramModel()
    m.byte(0, 0x28)
    m.byte(1, 0x0C)
    assert m.display_on() == 0
    assert M.DEFAULTS["r1f"] & 1 == 1 and HM._defaults()[0x1F] & 1 == 0
    # (f) a datum before any index after reset: ours -> REGS[0] (idx resets to 0),
    #     HM drops it. No pixel is affected.
    m = M.GramModel()
    m.byte(1, 0x42)
    assert m.regs[0] == 0x42


# --- the pad decoder ------------------------------------------------------
def test_pad_decoder_legal_streams_have_no_violation(platform_harness):
    for timing, tmin in ((P.DEFAULT_TIMING, (2, 2, 1)), (P.FAST_TIMING, (1, 1, 1))):
        dec, exp = P.PadDecoder(*tmin), P.Expander()
        dec.step(P.IDLE_PADS)
        s = P.Stim().timing(*timing)
        pairs = [(e[1], e[2]) for e in platform_harness[1].events if e[0] == "B"]
        s.bytes(pairs).idle(4).end()
        ev = P.decode_stim(s.words, dec, exp)
        assert [(e[1], e[2]) for e in ev] == pairs and dec.viol == 0


def test_pad_decoder_fast_timing_at_reset_tmin_violates_once_per_byte():
    dec, exp = P.PadDecoder(), P.Expander()
    dec.step(P.IDLE_PADS)
    s = P.Stim().timing(*P.FAST_TIMING).bytes([(1, i) for i in range(20)]).idle(4).end()
    P.decode_stim(s.words, dec, exp)
    assert dec.viol == 20
