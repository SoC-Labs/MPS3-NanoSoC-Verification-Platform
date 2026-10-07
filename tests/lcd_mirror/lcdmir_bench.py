"""lcdmir_bench.py -- the cocotb side of the direct LCDMIR bench (tb_lcd_mirror.sv).

One object owns the DUT's three drivers and the reference:
  * the pad BFM   (stimulus words -> tap, in SV; see pad_decoder.Stim),
  * the AXI-Lite master for CSR pokes (tests/common/regmap.AxiLiteMaster),
  * the SV frame reader (tb_axil_dump.sv) for whole-frame readback,
  * the reference = pad_decoder (tap + guards, fed the IDENTICAL per-cycle
    trace) -> hx8347_gram_model (the GRAM + LCDMIR model).

Every check compares the RTL against the reference; the tests add the
independent oracles (font-rendered shadow, card_pixel) on top.
"""
from __future__ import annotations

import os
import pathlib
import sys
from typing import Dict, Iterable, List, Optional

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "common"))
sys.path.insert(0, str(_HERE))

from regmap import AxiLiteMaster  # noqa: E402
import hx8347_gram_model as M  # noqa: E402
import pad_decoder as P  # noqa: E402
import streams  # noqa: E402

BASE = 0x44B8_0000          # LCD_MIRROR_FPGA.md §4 -- full system addresses
FB_OFF = 0x1_0000
CLK_NS = 10

# CSR offsets
ID, VERSION, GEOM, CTRL, STATUS = 0x000, 0x004, 0x008, 0x00C, 0x010
SEQ, FRAMES, RAMWR, RESETS, BYTES = 0x014, 0x018, 0x01C, 0x020, 0x024
VIOL, OOB, RDS, TMIN, FB_BANK = 0x028, 0x02C, 0x030, 0x034, 0x038
WIN_X, WIN_Y, AC, MODE = 0x040, 0x044, 0x048, 0x04C
SNAP_SEQ, SNAP_BBOX_X, SNAP_BBOX_Y = 0x050, 0x054, 0x058
DIRTY0, VALID0, REGS0 = 0x080, 0x0C0, 0x100

C_SNAP, C_CLR_STICKY, C_CLR_COUNTS = 1 << 8, 1 << 9, 1 << 10

NAMES = {ID: "ID", VERSION: "VERSION", GEOM: "GEOM", CTRL: "CTRL", STATUS: "STATUS",
         SEQ: "SEQ", FRAMES: "FRAMES", RAMWR: "RAMWR", RESETS: "RESETS",
         BYTES: "BYTES", VIOL: "VIOL", OOB: "OOB", RDS: "RDS", TMIN: "TMIN",
         FB_BANK: "FB_BANK", WIN_X: "WIN_X", WIN_Y: "WIN_Y", AC: "AC", MODE: "MODE",
         SNAP_SEQ: "SNAP_SEQ", SNAP_BBOX_X: "SNAP_BBOX_X", SNAP_BBOX_Y: "SNAP_BBOX_Y"}


def csr_name(off: int) -> str:
    if off in NAMES:
        return NAMES[off]
    if DIRTY0 <= off < DIRTY0 + 40:
        return f"DIRTY[{(off - DIRTY0) // 4}]"
    if VALID0 <= off < VALID0 + 40:
        return f"VALID[{(off - VALID0) // 4}]"
    if REGS0 <= off < REGS0 + 256:
        return f"REGS[{(off - REGS0) // 4}]"
    return f"0x{off:03X}"


class Bench:
    def __init__(self, dut, banked: int = 0):
        self.dut = dut
        self.banked = banked
        self.work = streams.work_dir()
        self.stim_path = self.work / "stim.hex"
        self.dump_path = self.work / "fb_dump.hex"
        self.model = M.GramModel()
        self.dec = P.PadDecoder()
        self.exp = P.Expander()
        self.tmin = (2, 2, 1)
        self.viol_sticky = 0
        self.rds_sticky = 0
        self.viol_off = 0         # decoder counts at the last clr_counts
        self.rds_off = 0
        self.ctrl_bits = 0        # ac_load | flip_conv << 1 | dirty_all << 2
        self.axi: Optional[AxiLiteMaster] = None
        self.nstreams = 0

    # ------------------------------------------------------------ bring-up
    async def start(self, clear: bool = False):
        """Shell reset + bring-up. The frame buffer and REGS survive a shell
        reset by design (BRAM/LUTRAM, and "FB pixels are kept"), and cocotb
        runs every test in ONE simulation, so the reference is SEEDED from the
        RTL's retained state rather than assumed black. `clear=True` then paints
        the whole glass black through the pads, for tests that want a known
        background."""
        d = self.dut
        # s_axi_aclk is generated inside tb_lcd_mirror.sv (10 ns).
        d.s_axi_aresetn.value = 0
        for nm in ("awvalid", "wvalid", "bready", "arvalid", "rready"):
            getattr(d, f"s_axi_{nm}").value = 0
        for nm in ("awaddr", "awprot", "wdata", "wstrb", "araddr", "arprot"):
            getattr(d, f"s_axi_{nm}").value = 0
        d.bfm_load.value = 0
        d.dump_go.value = 0
        d.dump_base.value = 0
        d.dump_count.value = 0
        await ClockCycles(d.s_axi_aclk, 2)
        # A previous test that failed mid-stream can leave the BFM running.
        while int(d.bfm_busy.value):
            await FallingEdge(d.bfm_busy)
        # Park the pad BFM (its timing, held PD/RS and levels persist from the
        # previous test) at the reference's initial state, while in reset.
        init = (P.Stim().timing(*P.DEFAULT_TIMING).level(0, 0)
                .raw(2, 1, 1, 0, 0, 1, 1).end())
        init.write_hex(str(self.stim_path))
        prev = int(d.bfm_done.value)
        await self._pulse(d.bfm_load)
        while int(d.bfm_done.value) == prev:
            await ClockCycles(d.s_axi_aclk, 1)
        await ClockCycles(d.s_axi_aclk, 8)
        d.s_axi_aresetn.value = 1
        await ClockCycles(d.s_axi_aclk, 5)
        self.axi = AxiLiteMaster.from_dut(d)
        # The reference sees the same first samples: the idle pads, rst_n high.
        self.dec.step(P.IDLE_PADS)
        self.dec.gap()
        # CTRL's reset value says which dirty mode this build starts in.
        self.ctrl_bits = await self.read(CTRL)
        assert self.ctrl_bits & 0x3 == 0, f"CTRL reset 0x{self.ctrl_bits:X}"
        self.model.dirty_all = (self.ctrl_bits >> 2) & 1
        # Seed the retained state.
        words = await self.dump_fb()
        fb = self.model.fb
        for i, w in enumerate(words):
            fb[2 * i], fb[2 * i + 1] = w & 0xFFFF, w >> 16
        regs = await self.dump(REGS0, 64)
        for k, w in enumerate(regs):
            for j in range(4):
                self.model.regs[4 * k + j] = (w >> (8 * j)) & 0xFF
        if self.banked:
            await self.write(FB_BANK, 0)
        if clear:
            await self.set_tmin(1, 1, 1)
            s = P.Stim().timing(*P.FAST_TIMING)
            s.bytes([(0, 0x16), (1, 0x20), (0, 0x17), (1, 0x05)]
                    + M.window_pairs(0, 319, 0, 239) + [(0, 0x22)]
                    + M.pixel_pairs([0] * (M.W * M.H)))
            await self.run(s.idle(8))
            assert not any(self.model.fb)

    # ------------------------------------------------------------ drivers
    async def _pulse(self, sig):
        await RisingEdge(self.dut.s_axi_aclk)
        await Timer(1, "ns")
        sig.value = 1
        await RisingEdge(self.dut.s_axi_aclk)
        await Timer(1, "ns")
        sig.value = 0

    async def run(self, stim: P.Stim, wait: bool = True):
        """Execute a stimulus on the pads; mirror it into the reference."""
        if not stim.words or (stim.words[-1] >> 28) != P.OP_END:
            stim.end()
        stim.write_hex(str(self.stim_path))
        prev = int(self.dut.bfm_done.value)
        self._bfm_prev = prev
        await self._pulse(self.dut.bfm_load)
        # reference: the identical trace
        v0, r0 = self.dec.viol, self.dec.rds
        for ev in P.decode_stim(stim.words, self.dec, self.exp):
            if ev[0] == "B":
                self.model.byte(ev[1], ev[2])
            elif ev[0] == "RST":
                self.model.panel_reset()
        self.viol_sticky |= int(self.dec.viol != v0)
        self.rds_sticky |= int(self.dec.rds != r0)
        self.nstreams += 1
        if wait:
            await self.wait_bfm(prev)

    async def wait_bfm(self, prev: int = None):
        d = self.dut
        if prev is None:
            prev = self._bfm_prev
        while int(d.bfm_busy.value):
            await FallingEdge(d.bfm_busy)
        await ClockCycles(d.s_axi_aclk, 8)      # drain the snooper's pipeline
        assert int(d.bfm_done.value) == ((prev + 1) & 0xFFFF), "pad BFM did not complete"
        assert self.exp.last.cs_n == 1 and self.exp.last.wr_n == 1, \
            "a stream must end with the pads idle"
        self.dec.gap()

    async def write(self, off: int, val: int, strb: int = 0xF):
        await self.axi.write(BASE + off, val, strb)

    async def read(self, off: int) -> int:
        data, resp = await self.axi.read(BASE + off)
        assert resp == 0, f"RRESP={resp} at {csr_name(off)}"
        return data

    async def set_ctrl(self, ac_load: int = 0, flip_conv: int = 0, dirty_all: int = None):
        if dirty_all is None:
            dirty_all = self.model.dirty_all
        self.ctrl_bits = (ac_load & 1) | ((flip_conv & 1) << 1) | ((dirty_all & 1) << 2)
        await self.write(CTRL, self.ctrl_bits)
        self.model.ac_load, self.model.flip_conv = ac_load, flip_conv
        self.model.dirty_all = dirty_all

    async def snap(self):
        await self.write(CTRL, self.ctrl_bits | C_SNAP)
        self.model.snap()

    async def clr_sticky(self):
        await self.write(CTRL, self.ctrl_bits | C_CLR_STICKY)
        self.viol_sticky = self.rds_sticky = 0
        self.model.oob_sticky = 0

    async def clr_counts(self):
        await self.write(CTRL, self.ctrl_bits | C_CLR_COUNTS)
        m = self.model
        m.frames = m.ramwr = m.resets = m.bytes = m.oob = 0
        self.viol_off, self.rds_off = self.dec.viol, self.dec.rds

    async def set_tmin(self, lo: int, hi: int, guard: int):
        await self.write(TMIN, (guard << 16) | (hi << 8) | lo)
        self.tmin = (lo, hi, guard)
        self.dec.set_tmin(lo, hi, guard)

    # ------------------------------------------------------------ readback
    async def dump(self, off: int, count: int) -> List[int]:
        d = self.dut
        d.dump_base.value = BASE + off
        d.dump_count.value = count
        await self._pulse(d.dump_go)
        while int(d.dump_busy.value):
            await FallingEdge(d.dump_busy)
        words = [int(x, 16) for x in self.dump_path.read_text().split()]
        assert len(words) == count, f"dump returned {len(words)} words, want {count}"
        return words

    async def dump_fb(self) -> List[int]:
        if not self.banked:
            return await self.dump(FB_OFF, M.FB_WORDS)
        words: List[int] = []
        for bank in range(5):
            await self.write(FB_BANK, bank)
            words += await self.dump(0x8000, 8192)
        return words[:M.FB_WORDS]

    async def dump_csrs(self) -> Dict[int, int]:
        w = await self.dump(0x000, 128)
        return {4 * i: v for i, v in enumerate(w)}

    # ------------------------------------------------------------ checks
    def expected_csrs(self) -> Dict[int, int]:
        viol = (self.dec.viol - self.viol_off) & 0xFFFFFFFF
        rds = (self.dec.rds - self.rds_off) & 0xFFFFFFFF
        last = self.exp.last
        exp = self.model.csrs(viol=viol, rds=rds, rst_n=last.rst_n, bl=self.exp.bl,
                              owner=self.exp.owner, viol_sticky=self.viol_sticky,
                              rds_sticky=self.rds_sticky, banked=self.banked)
        exp[TMIN] = (self.tmin[2] << 16) | (self.tmin[1] << 8) | self.tmin[0]
        exp[FB_BANK] = None            # banked: whatever dump_fb left; else 0
        return exp

    async def check_csrs(self, ctx: str):
        got = await self.dump_csrs()
        exp = self.expected_csrs()
        bad = []
        for off in range(0, 0x200, 4):
            want = exp.get(off, 0)
            if want is None:
                continue
            if got[off] != want:
                bad.append(f"  {csr_name(off)} @0x{off:03X}: got 0x{got[off]:08X} "
                           f"want 0x{want:08X}")
        assert not bad, f"{ctx}: CSR mismatch vs the golden model:\n" + "\n".join(bad[:24])
        return got

    async def check_fb(self, ctx: str, oracle: Optional[List[int]] = None):
        words = await self.dump_fb()
        want = self.model.fb_words()
        diff = [i for i in range(M.FB_WORDS) if words[i] != want[i]]
        if diff:
            lines = []
            for i in diff[:8]:
                p = 2 * i
                lines.append(f"  (vx={p % 320},vy={p // 320}) word {i}: got 0x{words[i]:08X} "
                             f"want 0x{want[i]:08X}")
            raise AssertionError(f"{ctx}: frame buffer differs from the golden model in "
                                 f"{len(diff)} of {M.FB_WORDS} words:\n" + "\n".join(lines))
        if oracle is not None:
            ow = [oracle[2 * i] | (oracle[2 * i + 1] << 16) for i in range(M.FB_WORDS)]
            odiff = [i for i in range(M.FB_WORDS) if words[i] != ow[i]]
            assert not odiff, (f"{ctx}: frame buffer differs from the INDEPENDENT oracle in "
                               f"{len(odiff)} words (first at word {odiff[0]})")
        return words

    async def check_all(self, ctx: str, oracle: Optional[List[int]] = None):
        await self.check_fb(ctx, oracle)
        return await self.check_csrs(ctx)


# --------------------------------------------------------------------------- #
# Stimulus builders
# --------------------------------------------------------------------------- #
def harness_stim(scen: streams.Scenario, stim: P.Stim, ctrl_state: dict,
                 rst_cycles: int = 16) -> P.Stim:
    """A recorded harness scenario -> pad stimulus. CTRL writes become what the
    pads show: reset_n 1->0 is a CLCD_RST pulse, backlight is the BL level."""
    for ev in scen.events:
        if ev[0] == "B":
            stim.byte(ev[1], ev[2])
        else:
            v = ev[1]
            rst_n, bl = (v >> 2) & 1, (v >> 1) & 1
            if ctrl_state.get("rst_n", 1) and not rst_n:
                stim.rst(rst_cycles)
            if bl != ctrl_state.get("bl", 0):
                stim.level(bl, ctrl_state.get("owner", 0))
            ctrl_state["rst_n"], ctrl_state["bl"] = rst_n, bl
    return stim
