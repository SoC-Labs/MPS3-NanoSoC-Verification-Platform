"""test_clcd_demo_fullgeom.py -- the one thing a shrunk frame cannot prove.

`make MODE=card` runs the whole bench at 32x16, because a 320x240 repaint is
153,600 bytes and roughly four million dut_clk cycles. Every layout boundary in
`clcd_demo_gen.sv` is a fraction of W/H, so the test card survives being shrunk
-- but the ADDRESS WINDOW does not: the RM recomputes the window END registers
from its own PIX_W/PIX_H, and at 32x16 those are 31 and 15, not the panel's 319
and 239.

So this module elaborates the RM at its SHIPPING geometry (PIX_W = PIX_H = 0,
i.e. "take the geometry from the firmware init table") and runs only far enough
to see the window program and the first pixels. That is a few thousand cycles,
not four million.

If this test and `MODE=card` disagree, the RM is painting into a window the
panel was never set up for -- which on real glass looks like a picture that is
clipped, wrapped, or simply absent, with a perfectly healthy-looking bus.
"""
from __future__ import annotations

import pathlib
import sys

import cocotb
from cocotb.triggers import ClockCycles

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import card_model  # noqa: E402
from test_clcd_demo import CLK_HZ, Bench  # noqa: E402
from tunnel_model import RS_CMD, RS_DATA  # noqa: E402


@cocotb.test()
async def test_window_at_shipping_geometry_is_the_firmwares(dut):
    b = Bench(dut)
    await b.start()

    table, defines = card_model.load_firmware_table()
    ms_total = sum(e.val for e in table if e.op == defines["HX_DLY"])
    n_init = len(card_model.init_sequence(table, defines))
    n_win = len(card_model.window_sequence(320, 240))
    # init + window + a few hundred pixel bytes, at ~28 cycles per byte.
    await ClockCycles(dut.dut_clk,
                      (n_init + n_win + 400) * 28 + ms_total * max(1, CLK_HZ // 1000)
                      + 500)
    b.stop()
    await ClockCycles(dut.dut_clk, 2)

    seq = b.decode().sequence
    w, h = card_model.fw.frame_geometry(table, defines)
    assert (w, h) == (320, 240), f"firmware table implies {w}x{h}"

    want = (card_model.init_sequence(table, defines)
            + card_model.window_sequence(w, h))
    got = seq[:len(want)]
    assert got == want, (
        "at the shipping geometry the RM's init+window stream must equal the "
        "firmware table's exactly:\n" + card_model.diff_sequences(got, want))

    # ...and the window bytes it emitted really are the firmware's own values,
    # not merely self-consistent with the RM's parameters.
    fw_win = dict(card_model.fw.window_writes(table, defines))
    emitted = {}
    for (rs_a, a), (rs_b, bb) in zip(want[n_init:], want[n_init + 1:]):
        if rs_a == RS_CMD and rs_b == RS_DATA and a in fw_win:
            emitted[a] = bb
    assert emitted == fw_win, f"emitted window {emitted} vs firmware {fw_win}"

    # The first pixels are the test card's white frame (top-left corner).
    first_pixels = seq[len(want):len(want) + 8]
    assert first_pixels == [(RS_DATA, 0xFF)] * 8, (
        f"the first GRAM bytes are {first_pixels}, expected white (0xFFFF) "
        f"frame pixels")
    dut._log.info(f"shipping geometry {w}x{h}: window == firmware, "
                  f"{len(seq)} bytes decoded")
