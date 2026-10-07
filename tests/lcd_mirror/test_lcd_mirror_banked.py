"""test_lcd_mirror_banked.py -- the 64 KiB banked-map fallback (BANKED=1,
LCD_MIRROR_FPGA.md §4 "Fallback"): CSRs at 0x0000, a 32 KiB frame-buffer
window at 0x8000 selected by FB_BANK (0x038, 5 banks). `make MODE=banked`.

Same bench, same reference, same oracles as test_lcd_mirror.py; only the
frame readback goes through the banks.
"""
from __future__ import annotations

import pathlib
import sys

import cocotb

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import hx8347_gram_model as M  # noqa: E402
import pad_decoder as P  # noqa: E402
import streams  # noqa: E402
from lcdmir_bench import FB_BANK, FRAMES, ID, STATUS, Bench  # noqa: E402


@cocotb.test()
async def test_banked_reset_and_bank_register(dut):
    b = Bench(dut, banked=1)
    await b.start()
    assert await b.read(ID) == 0x4C43444D
    assert await b.read(STATUS) & (1 << 15), "STATUS[15] must advertise the banked map"
    for bank in (4, 1, 7, 0):
        await b.write(FB_BANK, 0xFFFFFFF8 | bank)
        assert await b.read(FB_BANK) == bank, "FB_BANK is 3 bits, RW"
    await b.check_all("banked: after reset")


@cocotb.test()
async def test_banked_clcd_demo_frame(dut):
    b = Bench(dut, banked=1)
    await b.start()
    await b.set_tmin(1, 1, 1)
    seq, oracle = streams.demo_frame(0x3C5A)
    s = P.Stim().timing(*P.FAST_TIMING)
    await b.run(s.bytes(seq).idle(8))
    got = await b.check_all("banked: clcd_demo frame", oracle)
    assert got[FRAMES] == 1
    # bank 4 ends at word 38,400: the rest of its window reads 0
    await b.write(FB_BANK, 4)
    tail = await b.dump(0x8000 + 4 * (M.FB_WORDS - 4 * 8192), 8192 - (M.FB_WORDS - 4 * 8192))
    assert not any(tail), "words past the frame in bank 4 must read 0"
    await b.write(FB_BANK, 0)
