"""test_lcd_mirror.py -- the direct cocotb bench for the LCDMIR snooper
(fpga/shell/ip/lcd_mirror/, contract docs/planning/linux_lanes/LCD_MIRROR_FPGA.md).

DUT = tb_lcd_mirror (lcd_mirror at C_S_AXI_ADDR_WIDTH=32 + a pad BFM + an SV
frame reader). Every test drives the tap with stimulus words, mirrors the same
per-cycle trace into the reference (pad_decoder -> hx8347_gram_model) and
compares the WHOLE frame buffer and every CSR, pixel for pixel and bit for bit.
Where an independent oracle exists it is checked too:
  * harness streams  -> the clcd.c cell shadow, font-rendered (streams.py)
  * clcd_demo frames -> tests/clcd_demo/card_model.card_pixel()

`make mutants` rebuilds with deliberately broken RTL (off-by-one AC wrap,
MX/MY swapped, pixel byte order swapped, SNAP not clearing the live map, a
guard removed) and requires the named tests to FAIL.
"""
from __future__ import annotations

import pathlib
import random
import sys

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import hx8347_gram_model as M  # noqa: E402
import pad_decoder as P  # noqa: E402
import streams  # noqa: E402
from lcdmir_bench import (  # noqa: E402
    AC, BYTES, C_CLR_COUNTS, CTRL, DIRTY0, FB_OFF, FRAMES, GEOM, ID, MODE, OOB, RAMWR,
    RDS, REGS0, RESETS, SEQ, SNAP_BBOX_X, SNAP_BBOX_Y, SNAP_SEQ, STATUS, TMIN,
    VALID0, VERSION, VIOL, WIN_X, WIN_Y, Bench, harness_stim,
)

FAST = P.FAST_TIMING
SHIPPED = P.DEFAULT_TIMING


def _pairs_stim(pairs, timing=FAST) -> P.Stim:
    s = P.Stim().timing(*timing)
    s.bytes(pairs)
    return s.idle(8)


def _madctl_pairs(mad: int):
    return [(0, 0x16), (1, mad)]


# --------------------------------------------------------------------------- #
# 1. Reset state: identity, defaults (O3), a black frame, nothing dirty.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_reset_state(dut):
    # FIRST in the file on purpose: cocotb runs tests in order in one
    # simulation, and only the first sees the power-on (never-written) state.
    b = Bench(dut)
    await b.start()
    assert not any(b.model.fb), "power-on frame buffer is not black"
    assert not any(b.model.regs), "power-on REGS is not all-zero"
    assert await b.read(ID) == 0x4C43444D
    assert await b.read(VERSION) == 0x01001001
    assert await b.read(GEOM) == (240 << 16) | 320
    assert await b.read(TMIN) == 0x00010202
    # O3 defaults pinned: portrait-native window, MADCTL 0, COLMOD 6 (18 bpp).
    assert await b.read(WIN_X) == (239 << 16) | 0
    assert await b.read(WIN_Y) == (319 << 16) | 0
    assert await b.read(MODE) == 0x00000600
    got = await b.check_all("after shell reset")
    assert got[SNAP_BBOX_X] == 0x1FF and got[SNAP_BBOX_Y] == 0x1FF, "bbox not empty"
    # Unmapped offsets read 0, inside the CSR page and inside the region.
    for off in (0x03C, 0x05C, 0x07C, 0x0A8, 0x0FC, 0x200, 0x0F000, 0x0FFFC,
                FB_OFF + 4 * M.FB_WORDS, 0x3FFFC):
        assert await b.read(off) == 0, f"unmapped offset 0x{off:05X} reads non-zero"


# --------------------------------------------------------------------------- #
# 2. The harness: the recorded clcd.c stream, all five scenarios, at the
#    4-cycle/byte floor. After each: whole frame vs model AND vs the
#    font-rendered shadow; every CSR; then SNAP and the dirty map.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_harness_stream(dut):
    b = Bench(dut)
    await b.start()
    await b.set_tmin(1, 1, 1)           # the floor timing is legal at TMIN 1/1/1
    scen = streams.record_harness(b.work)
    ctrl = {}
    for sc in scen:
        stim = P.Stim().timing(*FAST)
        harness_stim(sc, stim, ctrl).idle(8)
        await b.run(stim)
        oracle = streams.render_shadow(sc.grid, sc.inv)
        await b.check_all(f"harness/{sc.name}", oracle)
        await b.snap()
        got = await b.check_csrs(f"harness/{sc.name} after SNAP")
        ntiles = sum(bin(got[DIRTY0 + 4 * i]).count("1") for i in range(10))
        dut._log.info(f"harness/{sc.name}: {sc.nbytes} bytes, pixel-exact vs model "
                      f"and shadow oracle; {ntiles} dirty tiles; VIOL {got[VIOL]}")
        assert got[VIOL] == 0
    assert b.model.resets == 1, "the boot reset pulse must have been counted"


# --------------------------------------------------------------------------- #
# 3. The harness boot at the SHIPPED 8080 timing (clcd.c's 2/4/4 + IDLE) with
#    the reset TMIN: exact, and not a single guard violation.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_harness_boot_shipped_timing(dut):
    b = Bench(dut)
    await b.start()
    boot = streams.record_harness(b.work)[0]
    stim = P.Stim().timing(*SHIPPED)
    harness_stim(boot, stim, {}).idle(8)
    await b.run(stim)
    got = await b.check_all("harness boot @ shipped timing",
                            streams.render_shadow(boot.grid, boot.inv))
    assert got[VIOL] == 0, f"VIOL={got[VIOL]} at the shipped timing"
    assert got[STATUS] & (1 << 3), "display_on (R28=0x3C) not reported"
    assert got[STATUS] & (1 << 1), "backlight level not reported"


# --------------------------------------------------------------------------- #
# 4. clcd_demo: whole repaints (re-init + window + 76,800 pixels each), and
#    the two dirty modes (H4). Compare-on-write must flag EXACTLY the tiles in
#    which card frame A and card frame B differ -- computed from card_pixel(),
#    not from the model -- and nothing for a repeated frame; dirty_all flags
#    every tile of a repaint.
# --------------------------------------------------------------------------- #
def _dirty_set(got):
    return {32 * i + k for i in range(10) for k in range(32) if got[DIRTY0 + 4 * i] >> k & 1}


@cocotb.test()
async def test_clcd_demo_frames(dut):
    b = Bench(dut)
    await b.start()
    await b.set_tmin(1, 1, 1)
    await b.set_ctrl(dirty_all=0)
    (seq_a, orc_a), (seq_b, orc_b) = streams.demo_frame(0x0000), streams.demo_frame(0xA5C3)
    await b.run(_pairs_stim(seq_a))
    got = await b.check_all("clcd_demo frame 0x0000", orc_a)
    assert got[FRAMES] == 1
    await b.snap()
    await b.run(_pairs_stim(seq_b))
    got = await b.check_all("clcd_demo frame 0xA5C3", orc_b)
    assert got[FRAMES] == 2
    await b.snap()
    got = await b.check_csrs("clcd_demo A->B, compare-on-write")
    want = {M.tile_of(i % M.W, i // M.W) for i in range(M.W * M.H) if orc_a[i] != orc_b[i]}
    assert 0 < len(want) < 300 and _dirty_set(got) == want, (
        f"compare-on-write flagged {sorted(_dirty_set(got))}, the card differs in {sorted(want)}")
    await b.run(_pairs_stim(seq_b))                      # the same frame again
    await b.snap()
    got = await b.check_csrs("clcd_demo B->B, compare-on-write")
    assert _dirty_set(got) == set(), "a repaint with no change must flag no tile"
    assert got[SEQ] - got[SNAP_SEQ] == 0 and got[FRAMES] == 3
    await b.set_ctrl(dirty_all=1)
    await b.run(_pairs_stim(seq_b))
    await b.snap()
    got = await b.check_csrs("clcd_demo B->B, dirty_all")
    assert _dirty_set(got) == set(range(300)), "dirty_all must flag every repainted tile"
    await b.set_ctrl(dirty_all=0)
    dut._log.info(f"clcd_demo: A->B changed {len(want)} tiles (compare-on-write), "
                  f"B->B 0, dirty_all 300")


# --------------------------------------------------------------------------- #
# 5. MADCTL: all 8 geometries x both O2 conventions, a unique-pixel window.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_madctl_geometries(dut):
    b = Bench(dut)
    await b.start(clear=True)
    frames = {}
    for conv in (0, 1):
        await b.set_ctrl(ac_load=0, flip_conv=conv)
        for mad in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0):
            x0, y0, nx, ny = 13, 7, 37, 23
            pix = [((0x0841 * (i + 1)) & 0xFFFF) or 1 for i in range(nx * ny)]
            win = (_madctl_pairs(mad) + [(0, 0x17), (1, 0x05)]
                   + M.window_pairs(x0, x0 + nx - 1, y0, y0 + ny - 1) + [(0, 0x22)])
            await b.run(_pairs_stim(win + M.pixel_pairs(pix)))
            words = await b.check_fb(f"MADCTL 0x{mad:02X} flip_conv={conv}")
            frames[(conv, mad)] = tuple(words)
            # erase the same window, same geometry: the glass is black again
            await b.run(_pairs_stim(win + M.pixel_pairs([0] * (nx * ny))))
        assert not any(b.model.fb), "erase did not return the model to black"
    # non-vacuity: 8 distinct placements per convention; the two conventions
    # differ exactly on the single-flip landscape values 0x60 and 0xA0.
    for conv in (0, 1):
        assert len({frames[(conv, m)] for m in range(0, 256, 0x20)}) == 8
    for mad in range(0, 256, 0x20):
        same = frames[(0, mad)] == frames[(1, mad)]
        assert same == (mad not in (0x60, 0xA0)), f"O2 conventions at 0x{mad:02X}"
    await b.check_csrs("MADCTL sweep")


# --------------------------------------------------------------------------- #
# 6. O1 -- the address-counter load convention, both settings, on the two
#    sequences that tell them apart: a split 0x22 and a start-register-only
#    write. The two settings must produce DIFFERENT frames (non-vacuity).
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_ac_load_conventions(dut):
    b = Bench(dut)
    await b.start(clear=True)
    frames = {}
    for ac_load in (0, 1):
        await b.set_ctrl(ac_load=ac_load)
        seq = (_madctl_pairs(0x20) + M.window_pairs(40, 55, 20, 27) + [(0, 0x22)]
               + M.pixel_pairs([0xF800] * 20)
               + [(0, 0x22)] + M.pixel_pairs([0x07E0] * 30)          # split 0x22
               + [(0, 0x02), (1, 0x00), (0, 0x03), (1, 100)]         # SC only
               + [(0, 0x22)] + M.pixel_pairs([0x001F] * 10))
        await b.run(_pairs_stim(seq))
        frames[ac_load] = tuple(await b.check_fb(f"ac_load={ac_load}"))
        await b.check_csrs(f"ac_load={ac_load}")
        # erase both variants' footprints for the next pass
        er = (M.window_pairs(40, 139, 20, 27) + [(0, 0x22)]
              + M.pixel_pairs([0] * (100 * 8)))
        await b.run(_pairs_stim(er))
    assert frames[0] != frames[1], "O1: ac_load=0 and ac_load=1 drew the same frame"


# --------------------------------------------------------------------------- #
# 7. Window wrap (FRAMES), an out-of-range window (OOB + sticky), a start
#    beyond the end (the 9-bit walk), clr_sticky and clr_counts.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_window_wrap_and_oob(dut):
    b = Bench(dut)
    await b.start(clear=True)
    await b.clr_counts()
    seq = (_madctl_pairs(0x20)
           + M.window_pairs(100, 104, 50, 52) + [(0, 0x22)]
           + M.pixel_pairs([0x1111 * (i % 15 + 1) for i in range(34)]))   # 2 wraps + 4
    await b.run(_pairs_stim(seq))
    got = await b.check_all("wrap")
    assert got[FRAMES] == 2 and got[OOB] == 0
    seq = M.window_pairs(316, 323, 10, 11) + [(0, 0x22)] + M.pixel_pairs([0xABCD] * 16)
    await b.run(_pairs_stim(seq))
    got = await b.check_all("OOB window")
    assert got[OOB] == 8 and got[STATUS] & (1 << 9), "OOB not counted / not sticky"
    seq = (M.window_pairs(318, 1, 30, 30) + [(0, 0x22)]
           + M.pixel_pairs([0x5A5A + i for i in range(200)]))              # SC > EC
    await b.run(_pairs_stim(seq))
    got = await b.check_all("SC > EC walk")
    await b.clr_sticky()
    got = await b.check_csrs("after clr_sticky")
    assert not got[STATUS] & (1 << 9)
    seq_before = got[SEQ]
    await b.clr_counts()
    got = await b.check_csrs("after clr_counts")
    assert got[FRAMES] == got[OOB] == got[BYTES] == got[RAMWR] == 0
    assert got[SEQ] == seq_before, "clr_counts must not touch SEQ (the seqlock)"


# --------------------------------------------------------------------------- #
# 8. Pixel formats: 18 bpp (exact after truncation, STATUS.approx), an
#    unknown format (fmt_ok=0), a phase reset by a re-issued 0x22.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_pixel_formats(dut):
    b = Bench(dut)
    await b.start(clear=True)
    rnd = random.Random(8)
    pix = [rnd.getrandbits(16) for _ in range(40)]
    seq = ([(0, 0x17), (1, 0x06)] + M.window_pairs(20, 29, 5, 8) + [(0, 0x22)]
           + M.pixel_pairs(pix, fmt18=True))
    await b.run(_pairs_stim(seq))
    got = await b.check_all("18 bpp")
    assert got[STATUS] & (1 << 7) and got[STATUS] & (1 << 6), "approx / fmt_ok for 18 bpp"
    # a partial 18-bpp pixel, then 0x22 again: the phase restarts
    seq = ([(0, 0x22), (1, 0xFF), (1, 0xFF), (0, 0x22)]
           + M.pixel_pairs([0x1234, 0x4321], fmt18=True))
    await b.run(_pairs_stim(seq))
    await b.check_all("18 bpp phase reset")
    seq = ([(0, 0x17), (1, 0x03)] + M.window_pairs(40, 49, 5, 5) + [(0, 0x22)]
           + M.pixel_pairs([0xC0DE] * 10))
    await b.run(_pairs_stim(seq))
    got = await b.check_all("unknown COLMOD 0x03")
    assert not got[STATUS] & (1 << 6), "fmt_ok must clear for COLMOD 0x03"
    await b.run(_pairs_stim([(0, 0x17), (1, 0x05)]))
    got = await b.check_csrs("back to RGB565")
    assert got[STATUS] & (1 << 6) and not got[STATUS] & (1 << 7)


# --------------------------------------------------------------------------- #
# 9. CLCD_RST in the middle of a GRAM stream: defaults return (O3, pinned),
#    VALID clears, RESETS counts, FB and REGS are kept; drawing on after the
#    reset uses the defaults (portrait, 18 bpp). Also: the live view DURING a
#    long reset.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_midstream_reset(dut):
    b = Bench(dut)
    await b.start(clear=True)
    await b.snap()
    s = P.Stim().timing(*FAST)
    s.bytes(_madctl_pairs(0x20) + [(0, 0x17), (1, 0x05)] + M.window_pairs(0, 63, 0, 31)
            + [(0, 0x22)] + M.pixel_pairs([0xFFE0] * 1000))
    s.rst(6000)
    s.bytes([(1, 0x77), (1, 0x78)])                    # idx is 0 after reset -> REGS[0]
    s.bytes([(0, 0x22)] + M.pixel_pairs([0x07FF] * 100, fmt18=True))
    s.idle(8)
    await b.run(s, wait=False)
    # sample the live view while CLCD_RST is held low
    for _ in range(400):
        st = await b.read(STATUS)
        if not st & 1:
            break
        await ClockCycles(dut.s_axi_aclk, 100)
    assert not st & 1, "never saw STATUS.rst_n low during the reset"
    assert await b.read(WIN_X) == (239 << 16) and await b.read(WIN_Y) == (319 << 16)
    assert await b.read(MODE) == 0x00000600
    valid = [await b.read(VALID0 + 4 * i) for i in range(10)]
    assert not any(valid), f"VALID not cleared during CLCD_RST: {valid}"
    await b.wait_bfm()
    got = await b.check_all("after a mid-stream reset")
    assert got[RESETS] == 1
    assert got[REGS0] & 0xFF == 0x78, "a datum after reset goes to REGS[0] (idx default 0)"
    assert got[REGS0 + 4 * (0x16 // 4)] >> 16 & 0xFF == 0x20, "REGS must survive CLCD_RST"


# --------------------------------------------------------------------------- #
# 10. The timing guards, one violation shape at a time. Each case: the RTL's
#     VIOL/BYTES deltas == the pad decoder's == a hand count.
# --------------------------------------------------------------------------- #
def _cyc(s, seq):
    for n, cs_n, wr_n, rs, pd in seq:
        s.raw(n, cs_n, wr_n, rs, pd)
    return s


@cocotb.test()
async def test_viol_guards(dut):
    b = Bench(dut)
    await b.start()
    A, B_ = 0xA1, 0x5E
    cases = [
        # name, raw cycles (n, cs_n, wr_n, rs, pd), hand VIOL delta, hand BYTES delta
        ("legal", [(3, 1, 1, 1, A), (2, 0, 1, 1, A), (3, 0, 0, 1, A), (3, 0, 1, 1, A),
                   (3, 1, 1, 1, A)], 0, 1),
        ("wr_low_1", [(2, 0, 1, 1, A), (1, 0, 0, 1, A), (3, 0, 1, 1, A), (3, 1, 1, 1, A)], 1, 1),
        ("wr_high_1", [(2, 0, 1, 1, A), (3, 0, 0, 1, A), (1, 0, 1, 1, A), (3, 0, 0, 1, A),
                       (3, 0, 1, 1, A), (3, 1, 1, 1, A)], 1, 2),
        ("pd_moves_in_low", [(2, 0, 1, 1, A), (2, 0, 0, 1, A), (2, 0, 0, 1, B_),
                             (3, 0, 1, 1, B_), (3, 1, 1, 1, B_)], 1, 1),
        ("hold_0", [(3, 1, 1, 1, A), (2, 0, 1, 1, A), (3, 0, 0, 1, A), (3, 0, 1, 1, B_),
                    (3, 1, 1, 1, B_)], 1, 1),
        ("cs_rises_with_wr", [(2, 0, 1, 1, A), (3, 0, 0, 1, A), (3, 1, 1, 1, A)], 1, 1),
        ("cs_rises_before_wr", [(2, 0, 1, 1, A), (3, 0, 0, 1, A), (1, 1, 0, 1, A),
                                (3, 1, 1, 1, A)], 1, 0),
        ("setup_0", [(3, 1, 1, 1, A), (3, 0, 0, 1, B_), (3, 0, 1, 1, B_), (3, 1, 1, 1, B_)], 1, 1),
    ]
    for name, cyc, dv, db in cases:
        v0, by0 = await b.read(VIOL), await b.read(BYTES)
        d0 = b.dec.viol
        await b.run(_cyc(P.Stim(), cyc).idle(4))
        v1, by1 = await b.read(VIOL), await b.read(BYTES)
        assert b.dec.viol - d0 == dv, f"{name}: the pad decoder counts {b.dec.viol - d0}, hand {dv}"
        assert (v1 - v0, by1 - by0) == (dv, db), \
            f"{name}: RTL VIOL/BYTES delta {(v1 - v0, by1 - by0)}, want {(dv, db)}"
    got = await b.check_csrs("after the guard cases")
    assert got[STATUS] & (1 << 8), "STATUS.viol not sticky"
    # TMIN is live: at 5/5/3 every shipped-timing byte is two violations
    # (WR-low 4 < 5 at the rise, setup 2 < 3 at the fall).
    await b.set_tmin(5, 5, 3)
    v0 = await b.read(VIOL)
    await b.run(_pairs_stim([(1, 0x10 + i) for i in range(12)], timing=SHIPPED))
    assert await b.read(VIOL) - v0 == 24
    # the 4-cycle floor at the reset TMIN: one violation per byte (WR-low 1 < 2)
    await b.set_tmin(2, 2, 1)
    v0 = await b.read(VIOL)
    await b.run(_pairs_stim([(1, 0x40 + i) for i in range(10)], timing=FAST))
    assert await b.read(VIOL) - v0 == 10
    await b.check_csrs("TMIN sweep")
    await b.clr_sticky()
    await b.clr_counts()
    got = await b.check_csrs("after clr_sticky + clr_counts")
    assert got[VIOL] == 0 and not got[STATUS] & (1 << 8)


# --------------------------------------------------------------------------- #
# 11. CLCD_RD strobes (READ_PATH=1 one day): counted, sticky, clearable.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_rd_strobes(dut):
    b = Bench(dut)
    await b.start()
    s = P.Stim()
    for k in range(3):
        s.raw(2, 0, 1, 0, 0x22, rd_n=1).raw(3, 0, 1, 0, 0x22, rd_n=0).raw(2, 1, 1, 0, 0x22, rd_n=1)
    await b.run(s.idle(4))
    got = await b.check_csrs("RD strobes")
    assert got[RDS] == 3 and got[STATUS] & (1 << 10)
    await b.clr_sticky()
    await b.clr_counts()
    got = await b.check_csrs("RD cleared")
    assert got[RDS] == 0 and not got[STATUS] & (1 << 10)


# --------------------------------------------------------------------------- #
# 12. SNAP: copy + clear in one step; SNAP_SEQ; bbox; and SNAP while the pads
#     are streaming (no tile is ever lost between two snapshots).
# --------------------------------------------------------------------------- #
def _tile_pairs(tx, ty, color, n=256):
    x0, y0 = tx * 16, ty * 16
    return M.window_pairs(x0, x0 + 15, y0, y0 + 15) + [(0, 0x22)] + M.pixel_pairs([color] * n)


@cocotb.test()
async def test_snap_semantics(dut):
    b = Bench(dut)
    await b.start(clear=True)
    await b.snap()
    await b.run(_pairs_stim(_madctl_pairs(0x20) + _tile_pairs(3, 2, 0xF800)))
    await b.snap()
    got = await b.check_csrs("SNAP after tile A")
    assert got[DIRTY0 + 4 * (43 // 32)] == 1 << (43 % 32)
    assert got[SNAP_SEQ] == got[SEQ]
    assert got[SNAP_BBOX_X] == (63 << 16) | 48 and got[SNAP_BBOX_Y] == (47 << 16) | 32
    await b.run(_pairs_stim(_tile_pairs(19, 14, 0x07E0, n=1)))
    await b.snap()
    got = await b.check_csrs("SNAP after tile B -- tile A must be gone")
    assert [got[DIRTY0 + 4 * i] for i in range(10)] == M.GramModel.map_words({299})
    await b.snap()
    got = await b.check_csrs("SNAP with nothing drawn")
    assert not any(got[DIRTY0 + 4 * i] for i in range(10))
    assert got[SNAP_BBOX_X] == 0x1FF and got[SNAP_BBOX_Y] == 0x1FF
    # SNAP racing the pads
    rnd = random.Random(11)
    tiles = rnd.sample(range(300), 60)
    seq = []
    for t in tiles:
        seq += _tile_pairs(t % 20, t // 20, rnd.getrandbits(16) | 1, n=64)   # never black
    touched_before = set(b.model.dirty)
    await b.run(_pairs_stim(seq), wait=False)
    seen = set()
    while int(dut.bfm_busy.value):
        await b.write(0x00C, b.ctrl_bits | (1 << 8))
        for i in range(10):
            w = await b.read(DIRTY0 + 4 * i)
            seen |= {32 * i + k for k in range(32) if w >> k & 1}
        await ClockCycles(dut.s_axi_aclk, 1500)
    await b.wait_bfm()
    await b.write(0x00C, b.ctrl_bits | (1 << 8))
    for i in range(10):
        w = await b.read(DIRTY0 + 4 * i)
        seen |= {32 * i + k for k in range(32) if w >> k & 1}
    assert touched_before == set()
    assert seen == set(tiles), (f"SNAP under traffic lost tiles {sorted(set(tiles) - seen)} "
                                f"or invented {sorted(seen - set(tiles))}")
    # resynchronise the reference: two SNAPs leave both with an empty snapshot
    await b.snap()
    await b.snap()
    await b.check_all("after SNAP under traffic")


# --------------------------------------------------------------------------- #
# 13. STATUS levels and the whole 256-byte REGS log.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_status_and_regs(dut):
    b = Bench(dut)
    await b.start()
    await b.run(P.Stim().level(1, 1).idle(4))
    got = await b.check_csrs("BL=1 owner=1")
    assert got[STATUS] & 0b110 == 0b110
    seq = []
    for idx in range(256):
        if idx != 0x22:
            seq += [(0, idx), (1, idx ^ 0x5A)]
    seq += [(0, 0x28), (1, 0x3C), (0, 0x1F), (1, 0x90)]
    await b.run(_pairs_stim(seq))
    got = await b.check_csrs("all 255 registers written")
    assert got[STATUS] & (1 << 3) and not got[STATUS] & (1 << 4), "display_on / standby"
    assert not got[STATUS] & (1 << 5)
    await b.run(_pairs_stim([(0, 0x22)]))
    got = await b.check_csrs("index 0x22")
    assert got[STATUS] & (1 << 5), "in_gram"
    await b.run(P.Stim().level(0, 0).idle(4))
    await b.check_csrs("BL=0 owner=0")


# --------------------------------------------------------------------------- #
# 14. The AXI slave: cocotb-BFM reads agree with the SV frame reader; RO and
#     frame-buffer writes are ignored; WSTRB honoured; a CTRL write without
#     byte 1 strobed does not SNAP.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_axi_slave(dut):
    b = Bench(dut)
    await b.start()
    rnd = random.Random(5)
    pix = [rnd.getrandbits(16) for _ in range(64 * 32)]
    await b.set_tmin(1, 1, 1)
    await b.run(_pairs_stim(_madctl_pairs(0x20) + [(0, 0x17), (1, 0x05)]
                            + M.window_pairs(100, 163, 60, 91) + [(0, 0x22)]
                            + M.pixel_pairs(pix)))
    words = await b.check_fb("AXI: frame")
    for i in rnd.sample(range(M.FB_WORDS), 48) + [0, M.FB_WORDS - 1, 60 * 160 + 50]:
        w = await b.read(FB_OFF + 4 * i)
        assert w == words[i], f"BFM read of FB word {i}: 0x{w:08X} vs dump 0x{words[i]:08X}"
    for off in (0x000, 0x010, 0x014, 0x040, 0x080, 0x100, FB_OFF, FB_OFF + 0x1000):
        before = await b.read(off)
        await b.write(off, ~before & 0xFFFFFFFF)
        assert await b.read(off) == before, f"write to read-only 0x{off:05X} took effect"
    await b.write(TMIN, 0x00AABBCC, strb=0b0010)
    assert await b.read(TMIN) == 0x0001BB01
    await b.write(TMIN, 0x00010101)
    await b.write(0x00C, (1 << 8) | 0x3, strb=0b0001)     # byte 0 only: no SNAP
    assert await b.read(0x00C) == 0x3
    await b.write(0x00C, 0x0)
    await b.check_csrs("AXI: after the RO/WSTRB pokes")


# --------------------------------------------------------------------------- #
# 15. H2: Harness Manager's recorded streams (hm_fixtures/, copied from HM
#     aae155c) through the RTL, as HM §10.3's history: harness boot ->
#     link_down -> banner -> [KVM reset] clcd_demo #5 -> #6 -> [KVM reset]
#     nanosoc demo -> [KVM reset] regain. Every step: frame vs the golden model
#     and vs its independent oracle (HM's grid font-rendered here / card_pixel
#     / HM's rectangles), and the compare-on-write dirty count after SNAP must
#     be HM's published 179/67/269/291/12/300/297.
# --------------------------------------------------------------------------- #
@cocotb.test()
async def test_hm_fixture_history(dut):
    import hm_fixtures as HF
    HM = HF.hm_decoder()
    b = Bench(dut)
    await b.start(clear=True)
    await b.set_ctrl(dirty_all=0)
    await b.snap()
    steps = [("boot", HF.load_stream("boot"), streams.render_shadow(*HF.load_grid("boot")), 179),
             ("link_down", HF.load_stream("link_down"),
              streams.render_shadow(*HF.load_grid("link_down")), 67),
             ("banner", HF.load_stream("banner"), streams.render_shadow(*HF.load_grid("banner")), 269)]
    for f, n in ((5, 291), (6, 12)):
        seq, orc = streams.demo_frame(f)
        steps.append((f"clcd_demo#{f}", (HF.KVM_RESET if f == 5 else []) + list(seq), orc, n))
    if HM is not None:
        seq, orc = HM.nanosoc_demo()
        steps.append(("nanosoc", HF.KVM_RESET + list(seq), list(orc), 300))
    steps.append(("regain", HF.KVM_RESET + HF.load_stream("regain"),
                  streams.render_shadow(*HF.load_grid("regain")), 297 if HM is not None else None))
    ctrl = {}
    for name, recs, oracle, ndirty in steps:
        stim = HF.to_stim(recs, P.Stim().timing(*FAST), ctrl).idle(8)
        await b.run(stim)
        await b.check_fb(f"HM {name}", oracle)
        await b.snap()
        got = await b.check_csrs(f"HM {name}")
        nd = len(_dirty_set(got))
        dut._log.info(f"HM {name}: {len(recs)} records, pixel-exact vs model + oracle, "
                      f"{nd} changed tiles (HM: {ndirty})")
        if ndirty is not None:
            assert nd == ndirty, f"HM {name}: {nd} dirty tiles, HM's model says {ndirty}"
        assert got[VIOL] == 0
