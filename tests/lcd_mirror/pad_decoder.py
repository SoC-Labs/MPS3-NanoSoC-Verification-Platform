"""pad_decoder.py -- the pad-level half of the LCDMIR spec: how a trace of 8080
pad samples becomes decoded bus events, and what the VIOL / RDS guards count
(docs/planning/linux_lanes/LCD_MIRROR_FPGA.md §2.2, §4 TMIN).

Also the STIMULUS format the bench's SystemVerilog pad BFM executes
(tb_lcd_mirror.sv), with a Python expander that produces the identical
per-cycle trace -- so the expected VIOL count for any stimulus comes from the
same trace the RTL sees, not from a hand count.

THE RULES (per s_axi_aclk sample; t1 = this sample, t2 = the previous one):
  byte event   WR_n rose (t2 low, t1 high) while CS_n was low at t2, and
               CLCD_RST high at both -> commit {RS, PD} as sampled at t2.
  VIOL         at most one count per cycle, if any of:
                 * byte event with WR-low width < TMIN.lo
                 * WR_n fell with CS_n low and WR-high width < TMIN.hi
                 * WR_n fell with CS_n low and PD/RS stable < TMIN.guard cycles
                 * PD/RS changed while WR_n stayed low (CS_n low)
                 * PD/RS changed within TMIN.guard cycles after WR_n rose
                 * CS_n rose while WR_n was low at t2
               all gated by CLCD_RST high at both samples.
  RDS          CLCD_RD fell (t2 high, t1 low), CLCD_RST high.
  RST          CLCD_RST fell -> one panel_reset() event.
Widths saturate at 255; WR-high starts saturated after reset.
"""
from __future__ import annotations

from typing import Iterable, Iterator, List, NamedTuple, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Stimulus words (32-bit), executed by tb_lcd_mirror.sv's BFM.
# --------------------------------------------------------------------------- #
OP_BYTE = 0x0     # [8] rs, [7:0] d        : one write cycle at the current timing
OP_RST = 0x1      # [23:0] n               : CLCD_RST low for n cycles (pads idle)
OP_IDLE = 0x2     # [23:0] n               : n idle cycles
OP_TIMING = 0x3   # [27:24] idle [23:16] setup [15:8] wr_lo [7:0] wr_hi (all >= 1)
OP_RAW = 0x4      # [27:16] n, [12] rst_n [11] rd_n [10] cs_n [9] wr_n [8] rs [7:0] pd
OP_LEVEL = 0x5    # [1] bl, [0] owner      : static levels (no cycles)
OP_END = 0xF

DEFAULT_TIMING = (2, 4, 4, 1)     # setup, wr_lo, wr_hi, idle -- clcd.c's 2/4/4 + IDLE
FAST_TIMING = (1, 1, 1, 1)        # the 4-cycle-per-byte floor (clcd_core 0/0/0)


class Stim:
    """Build a stimulus: a list of 32-bit BFM words."""

    def __init__(self):
        self.words: List[int] = []

    def timing(self, setup: int, wr_lo: int, wr_hi: int, idle: int = 1) -> "Stim":
        assert all(1 <= v <= 255 for v in (setup, wr_lo, wr_hi)) and 1 <= idle <= 15
        self.words.append((OP_TIMING << 28) | (idle << 24) | (setup << 16)
                          | (wr_lo << 8) | wr_hi)
        return self

    def byte(self, rs: int, d: int) -> "Stim":
        self.words.append((OP_BYTE << 28) | ((rs & 1) << 8) | (d & 0xFF))
        return self

    def bytes(self, pairs: Iterable[Tuple[int, int]]) -> "Stim":
        for rs, d in pairs:
            self.byte(rs, d)
        return self

    def rst(self, n: int = 20) -> "Stim":
        assert 1 <= n < (1 << 24)
        self.words.append((OP_RST << 28) | n)
        return self

    def idle(self, n: int) -> "Stim":
        assert 1 <= n < (1 << 24)
        self.words.append((OP_IDLE << 28) | n)
        return self

    def raw(self, n: int, cs_n: int, wr_n: int, rs: int, pd: int,
            rd_n: int = 1, rst_n: int = 1) -> "Stim":
        assert 1 <= n < (1 << 12)
        self.words.append((OP_RAW << 28) | (n << 16) | ((rst_n & 1) << 12)
                          | ((rd_n & 1) << 11) | ((cs_n & 1) << 10)
                          | ((wr_n & 1) << 9) | ((rs & 1) << 8) | (pd & 0xFF))
        return self

    def level(self, bl: int, owner: int) -> "Stim":
        self.words.append((OP_LEVEL << 28) | ((bl & 1) << 1) | (owner & 1))
        return self

    def end(self) -> "Stim":
        self.words.append(OP_END << 28)
        return self

    def write_hex(self, path: str):
        with open(path, "w") as f:
            f.write("\n".join(f"{w:08x}" for w in self.words))
            f.write("\n")


class Pads(NamedTuple):
    cs_n: int
    wr_n: int
    rs: int
    pd: int
    rd_n: int
    rst_n: int


IDLE_PADS = Pads(1, 1, 0, 0, 1, 1)


class Expander:
    """Execute stimulus words exactly as the SV BFM does, yielding one Pads per
    s_axi_aclk sample. State (timing, held RS/PD, levels) persists across
    ``run()`` calls, like the BFM between streams."""

    def __init__(self):
        self.timing = DEFAULT_TIMING
        self.rs = 0
        self.pd = 0
        self.bl = 0
        self.owner = 0
        self.last = IDLE_PADS          # the last sample yielded (the pads now)

    def run(self, words: Sequence[int]) -> Iterator[Pads]:
        for p in self._run(words):
            self.last = p
            yield p

    def _run(self, words: Sequence[int]) -> Iterator[Pads]:
        for w in words:
            op = (w >> 28) & 0xF
            if op == OP_END:
                return
            if op == OP_BYTE:
                self.rs, self.pd = (w >> 8) & 1, w & 0xFF
                s, lo, hi, idl = self.timing
                for _ in range(s):
                    yield Pads(0, 1, self.rs, self.pd, 1, 1)
                for _ in range(lo):
                    yield Pads(0, 0, self.rs, self.pd, 1, 1)
                for _ in range(hi):
                    yield Pads(0, 1, self.rs, self.pd, 1, 1)
                for _ in range(idl):
                    yield Pads(1, 1, self.rs, self.pd, 1, 1)
            elif op == OP_RST:
                for _ in range(w & 0xFFFFFF):
                    yield Pads(1, 1, self.rs, self.pd, 1, 0)
            elif op == OP_IDLE:
                for _ in range(w & 0xFFFFFF):
                    yield Pads(1, 1, self.rs, self.pd, 1, 1)
            elif op == OP_TIMING:
                self.timing = ((w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF,
                               (w >> 24) & 0xF)
            elif op == OP_RAW:
                n = (w >> 16) & 0xFFF
                self.rs, self.pd = (w >> 8) & 1, w & 0xFF
                p = Pads((w >> 10) & 1, (w >> 9) & 1, self.rs, self.pd,
                         (w >> 11) & 1, (w >> 12) & 1)
                for _ in range(n):
                    yield p
            elif op == OP_LEVEL:
                self.bl, self.owner = (w >> 1) & 1, w & 1
            else:
                raise ValueError(f"bad stimulus op 0x{op:X} in 0x{w:08X}")


class PadDecoder:
    """The RTL tap + guards, per sample. Produces model events and counts."""

    def __init__(self, tmin_lo: int = 2, tmin_hi: int = 2, tmin_guard: int = 1):
        self.tmin_lo, self.tmin_hi, self.tmin_guard = tmin_lo, tmin_hi, tmin_guard
        # t1 (the shell-reset value: pads idle, panel IN RESET)
        self.t1 = Pads(1, 1, 0, 0, 1, 0)
        self.lo_w = 0
        self.hi_w = 255
        self.stab = 255
        self.hold_rem = 0
        self.viol = 0
        self.rds = 0
        self.events: List[tuple] = []

    def set_tmin(self, lo: int, hi: int, guard: int):
        self.tmin_lo, self.tmin_hi, self.tmin_guard = lo, hi, guard

    def gap(self):
        """Model the long idle stretch between two streams (AXI traffic only):
        widths saturate, no PD/RS change happens, any hold window expires."""
        t = self.t1
        assert t.cs_n == 1 and t.wr_n == 1, "a gap must start with the pads idle"
        self.hi_w = 255
        self.stab = 255
        self.hold_rem = 0

    def feed(self, samples: Iterable[Pads]):
        for s in samples:
            self.step(s)

    def step(self, s: Pads):
        t2, t1 = self.t1, s          # shift: the new sample becomes t1
        same = (t1.rs, t1.pd) == (t2.rs, t2.pd)
        live = t1.rst_n and t2.rst_n
        wr_fall = t2.wr_n and not t1.wr_n
        wr_rise = (not t2.wr_n) and t1.wr_n
        byte_evt = wr_rise and (not t2.cs_n) and live
        setup_now = min(self.stab + 1, 255) if same else 0
        g = self.tmin_guard

        v_lo = byte_evt and self.lo_w < self.tmin_lo
        v_hi = wr_fall and not t1.cs_n and self.hi_w < self.tmin_hi
        v_setup = wr_fall and not t1.cs_n and setup_now < g
        v_chg = (not t1.wr_n) and (not t2.wr_n) and (not t1.cs_n) and not same
        v_hold0 = byte_evt and (not same) and g != 0
        v_hold = (not byte_evt) and self.hold_rem != 0 and not same
        v_cs = (not t2.cs_n) and t1.cs_n and (not t2.wr_n)
        if live and (v_lo or v_hi or v_setup or v_chg or v_hold0 or v_hold or v_cs):
            self.viol += 1
        if live and t2.rd_n and not t1.rd_n:
            self.rds += 1
        if t2.rst_n and not t1.rst_n:
            self.events.append(("RST",))
        if byte_evt:
            self.events.append(("B", t2.rs, t2.pd))

        # register updates (the RTL's always_ff, using the pre-update values)
        if not t1.wr_n:
            self.lo_w = 1 if t2.wr_n else min(self.lo_w + 1, 255)
        else:
            self.hi_w = min(self.hi_w + 1, 255) if t2.wr_n else 1
        self.stab = min(self.stab + 1, 255) if same else 0
        if byte_evt:
            self.hold_rem = (g - 1) if (same and g > 1) else 0
        elif self.hold_rem:
            self.hold_rem = self.hold_rem - 1 if same else 0
        self.t1 = t1

    def take_events(self) -> List[tuple]:
        ev, self.events = self.events, []
        return ev


def decode_stim(words: Sequence[int], dec: PadDecoder, exp: Expander) -> List[tuple]:
    """Expand + decode one stimulus; return the model events it produced."""
    dec.feed(exp.run(words))
    return dec.take_events()
