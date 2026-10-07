"""random_stim.py — seeded, reproducible constrained-random stimulus helpers
for the per-block cocotb benches (A5 verification-confidence backbone).

WHY THIS FILE EXISTS
--------------------
Every bench in this suite drives fixed, hand-picked vectors — "confidence is
exactly as wide as the author's imagination." The VCS code-coverage pass
measured the price of that: condition coverage ~55% across the AXI4-Lite
slaves (concentrated on the shared Xilinx-template FSM's `aw_en` corner
branches — the AW/W arrival-order interleavings a same-cycle-only BFM can
never reach) and toggle coverage ~40% on the wide GPIO / telemetry buses.
This module adds *targeted* constrained-random stimulus to close those holes
without ever masking a real failure.

REPRODUCIBILITY CONTRACT
------------------------
`seeded_rng()` seeds from the `SEED` environment variable when set (so a CI
failure can be replayed bit-for-bit with `SEED=<n>`), otherwise from a fresh
OS-random 32-bit value — and it **always logs the seed** (to the DUT logger
and stdout, both captured in the sim log). A randomized bench that fails
therefore always prints the exact `SEED=<n>` needed to reproduce it. Do NOT
narrow the randomization to hide a flaky bench — fix the bench; if a seed
exposes a real RTL bug, report the seed + the offending sequence.

WHAT IS PROVIDED
----------------
  * `seeded_rng(dut, name)`               — the seeded, seed-logging RNG.
  * `RandomAxiMaster`                     — an AXI4-Lite master with explicit
        AW/W arrival-order control (`write_ordered`, order in
        {"same","aw_first","w_first"}), zero-gap back-to-back writes, and a
        concurrent read-during-write helper. Wraps the reference
        `regmap.AxiLiteMaster` for its plain read()/write() (so it inherits
        the ReadOnly-phase-safe handshake logic verbatim).
  * `random_axi_burst(...)`               — one call that hammers a slave with
        a randomized mix of orderings / gaps / WSTRB lane patterns /
        read-during-write, asserting BRESP=OKAY on every beat (the bound
        SVA AXI checker validates the protocol itself).
  * WSTRB / gap / order / data / address pickers.
  * `rand_frame_len` / `rand_payload`     — Ethernet frame randomization,
        including the exact 64 / 1518 boundaries.
  * `rand_coprime_periods`                — coprime (aclk, dut_clk) period
        pairs for CDC stress (the audit's top RTL risk is CDC exercised at
        exactly one clock ratio).

Self-contained: the only project import is `regmap.AxiLiteMaster`; it must
NOT edit regmap.py (another track owns it mid-edit).
"""
from __future__ import annotations

import math
import os
import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from regmap import AxiLiteMaster


# --------------------------------------------------------------------------- #
# Seeded, always-logged RNG.
# --------------------------------------------------------------------------- #
def seeded_rng(dut=None, name: str = "stim"):
    """Return `(rng, seed)`.

    Seed source: `os.environ["SEED"]` if set (accepts decimal or 0x-hex),
    else a fresh OS-random 32-bit value. The seed is ALWAYS logged so any
    failure is reproducible with `SEED=<seed> make -C tests/<block>`.
    """
    env = os.environ.get("SEED", "").strip()
    if env:
        try:
            seed = int(env, 0)
        except ValueError:
            # Non-numeric SEED: hash it to a stable 32-bit int (still logged).
            seed = int.from_bytes(env.encode(), "little") & 0xFFFF_FFFF
    else:
        seed = random.SystemRandom().randrange(1 << 32)

    rng = random.Random(seed)
    msg = (f"[random_stim:{name}] SEED={seed} "
           f"(reproduce with: SEED={seed})")
    # Log everywhere useful — dut logger (cocotb) and stdout (VCS log).
    if dut is not None:
        try:
            dut._log.info(msg)
        except Exception:  # pragma: no cover - logger shape varies
            pass
    print(msg, flush=True)
    return rng, seed


# --------------------------------------------------------------------------- #
# Randomization pickers.
# --------------------------------------------------------------------------- #
_ORDERS = ("same", "aw_first", "w_first")


def rand_order(rng) -> str:
    """AW/W arrival order for a write: same-cycle, AW-first, or W-first.
    The interleaved orders are what fill the FSM `aw_en` condition holes."""
    return rng.choice(_ORDERS)


def rand_gap(rng, lo: int = 0, hi: int = 4) -> int:
    """Random inter-transaction (or inter-channel) gap in clock cycles.
    `lo=0` deliberately allows zero-gap back-to-back."""
    return rng.randint(lo, hi)


def rand_wstrb(rng, nbytes: int = 4) -> int:
    """Random byte-lane strobe with a distribution that deliberately hits
    full-word, all-zero (bare address touch), single-lane, and arbitrary
    multi-lane subsets — every WSTRB shape a slave's per-lane decode must
    handle."""
    r = rng.random()
    full = (1 << nbytes) - 1
    if r < 0.40:
        return full                       # full word
    if r < 0.55:
        return 0                          # no lanes (bare touch)
    if r < 0.80:
        return 1 << rng.randrange(nbytes)  # exactly one lane
    return rng.randint(1, full)           # arbitrary non-empty subset


def rand_data(rng, width: int = 32) -> int:
    return rng.getrandbits(width)


def rand_addr(rng, addrs):
    return rng.choice(list(addrs))


# --------------------------------------------------------------------------- #
# Ethernet frame randomization (gen_checker / ethernet benches).
# --------------------------------------------------------------------------- #
def rand_frame_len(rng, lo: int = 64, hi: int = 1518,
                   hit_boundaries: bool = True) -> int:
    """Random total frame length in [lo, hi]. With `hit_boundaries` the
    exact 64 (min) and 1518 (untagged max) edges are drawn with extra
    weight — the off-by-one length-envelope corners the checker must get
    right."""
    if hit_boundaries and rng.random() < 0.30:
        return rng.choice([lo, hi])
    return rng.randint(lo, hi)


def rand_payload(rng, n: int) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(n))


# --------------------------------------------------------------------------- #
# Coprime clock-ratio selection (CDC stress).
# --------------------------------------------------------------------------- #
def rand_coprime_periods(rng, lo: int = 3, hi: int = 17):
    """Return a coprime `(period_a, period_b)` integer-ns pair with
    period_a != period_b and gcd == 1 — so the two clocks share no common
    sub-period and the async-FIFO gray pointers cross at a genuinely
    unrelated ratio."""
    for _ in range(10_000):
        a = rng.randint(lo, hi)
        b = rng.randint(lo, hi)
        if a != b and math.gcd(a, b) == 1:
            return a, b
    return 10, 7  # unreachable fallback, still coprime


def coprime_period_pairs(lo: int = 3, hi: int = 17):
    """Every coprime (a, b) integer-ns pair in [lo, hi], a < b — a fixed
    menu a CDC sweep can `rng.sample()` from for a reproducible spread of
    ratios."""
    return [(a, b) for a in range(lo, hi + 1) for b in range(a + 1, hi + 1)
            if math.gcd(a, b) == 1]


# --------------------------------------------------------------------------- #
# AXI4-Lite master with explicit AW/W arrival-order control.
# --------------------------------------------------------------------------- #
class RandomAxiMaster:
    """AXI4-Lite master that can present the AW and W channels in any
    arrival order (`same` / `aw_first` / `w_first`), do zero-gap
    back-to-back writes, and run a read concurrently with a write.

    Plain `read()` / `write()` delegate to the reference
    `regmap.AxiLiteMaster` (inheriting its cocotb-2.x ReadOnly-phase-safe
    handshake). `write_ordered()` is the new capability the shared BFM
    lacks: it drives the two write channels independently so the shell's
    Xilinx-template FSM sees `s_axi_awvalid` and `s_axi_wvalid` take
    independent values — the single biggest AXI condition-coverage hole.

    All methods must be called from a writable phase (i.e. right after a
    `RisingEdge`/`ClockCycles`/another BFM call), never parked in ReadOnly.
    """

    def __init__(self, base: AxiLiteMaster):
        self.base = base
        self.clk = base.clk
        self.awaddr, self.awvalid, self.awready = base.awaddr, base.awvalid, base.awready
        self.wdata, self.wstrb, self.wvalid, self.wready = (
            base.wdata, base.wstrb, base.wvalid, base.wready)
        self.bresp, self.bvalid, self.bready = base.bresp, base.bvalid, base.bready
        self.araddr, self.arvalid, self.arready = base.araddr, base.arvalid, base.arready
        self.rdata, self.rresp, self.rvalid, self.rready = (
            base.rdata, base.rresp, base.rvalid, base.rready)

    @classmethod
    def from_dut(cls, dut, prefix: str = "s_axi_"):
        return cls(AxiLiteMaster.from_dut(dut, prefix))

    async def read(self, addr: int):
        return await self.base.read(addr)

    async def write(self, addr: int, data: int, strb: int = 0xF) -> int:
        return await self.base.write(addr, data, strb)

    async def write_ordered(self, addr: int, data: int, strb: int = 0xF,
                            order: str = "same", sep: int = 1) -> int:
        """Drive one write with an explicit AW/W arrival order.

          order="same"     : AWVALID and WVALID asserted the same cycle
                             (what the reference BFM always did).
          order="aw_first" : AWVALID asserted, held `sep` cycles with WVALID
                             LOW, then WVALID asserted. Exercises the FSM
                             condition `s_axi_awvalid=1 & s_axi_wvalid=0`.
          order="w_first"  : the mirror — WVALID first, then AWVALID.

        VALID lines are held stable until their handshake and through B
        capture (BREADY held high), matching the reference BFM's timing so
        the same-domain slaves see identical acceptance to today.
        Returns BRESP.
        """
        sep = max(1, sep) if order != "same" else 0
        self.awaddr.value = addr
        self.wdata.value = data
        self.wstrb.value = strb
        self.bready.value = 1

        if order == "aw_first":
            self.awvalid.value = 1
            self.wvalid.value = 0
            for _ in range(sep):
                await RisingEdge(self.clk)
            self.wvalid.value = 1
        elif order == "w_first":
            self.wvalid.value = 1
            self.awvalid.value = 0
            for _ in range(sep):
                await RisingEdge(self.clk)
            self.awvalid.value = 1
        else:  # same
            self.awvalid.value = 1
            self.wvalid.value = 1

        aw_seen = w_seen = False
        resp = None
        while resp is None or not (aw_seen and w_seen):
            await RisingEdge(self.clk)
            await ReadOnly()
            aw_seen = aw_seen or bool(int(self.awready.value))
            w_seen = w_seen or bool(int(self.wready.value))
            if resp is None and int(self.bvalid.value):
                resp = int(self.bresp.value)
        await RisingEdge(self.clk)          # retire the last handshake
        self.awvalid.value = 0              # writable (post-edge) phase
        self.wvalid.value = 0
        self.bready.value = 0
        return resp

    async def write_read_concurrent(self, waddr: int, wdata: int, raddr: int,
                                    strb: int = 0xF, order: str = "same",
                                    sep: int = 1):
        """Run a read on the AR/R channels CONCURRENTLY with a write on the
        AW/W/B channels (independent RTL always-blocks; the channels share
        no signal). Exercises the read-during-write corner. Returns
        `((rdata, rresp), bresp)`."""
        wr = cocotb.start_soon(
            self.write_ordered(waddr, wdata, strb, order, sep))
        rd = await self.read(raddr)
        bresp = await wr
        return rd, bresp


async def random_axi_burst(master: "RandomAxiMaster", rng, waddrs, raddrs,
                           n: int = 40, data_width: int = 32, nbytes: int = 4,
                           read_during_write_p: float = 0.30):
    """Hammer an AXI4-Lite slave with `n` randomized write transactions:
    random AW/W arrival order, random inter-/intra-transaction gaps
    (including zero-gap back-to-back), random WSTRB lane patterns, random
    data, random address from `waddrs`, and — with probability
    `read_during_write_p` — a concurrent read from `raddrs`. Interleaves
    stand-alone reads too so the AR/R path and the read-data mux toggle.

    Asserts BRESP=OKAY on every write (the contract's OKAY-only convention);
    the bound `axi4lite_protocol_checker` SVA validates the handshake
    protocol itself and $fatal()s the bench on any violation. Returns a
    small dict of how many of each ordering was issued (for the report).
    """
    waddrs = list(waddrs)
    raddrs = list(raddrs)
    tally = {"same": 0, "aw_first": 0, "w_first": 0, "read_during_write": 0,
             "zero_gap": 0}
    for i in range(n):
        order = rand_order(rng)
        sep = rand_gap(rng, 1, 4) if order != "same" else 0
        addr = rand_addr(rng, waddrs)
        data = rand_data(rng, data_width)
        strb = rand_wstrb(rng, nbytes)
        tally[order] += 1

        if rng.random() < read_during_write_p:
            raddr = rand_addr(rng, raddrs)
            (rdata, rresp), bresp = await master.write_read_concurrent(
                addr, data, raddr, strb, order, sep)
            assert rresp == 0, (
                f"iter {i}: concurrent read RRESP != OKAY (addr={raddr:#x})")
            tally["read_during_write"] += 1
        else:
            bresp = await master.write_ordered(addr, data, strb, order, sep)

        assert bresp == 0, (
            f"iter {i}: BRESP != OKAY "
            f"(order={order}, sep={sep}, addr={addr:#x}, strb={strb:#x})")

        # Stand-alone read (AR/R path + read-data-mux toggle).
        if rng.random() < 0.5:
            raddr = rand_addr(rng, raddrs)
            rdata, rresp = await master.read(raddr)
            assert rresp == 0, f"iter {i}: read RRESP != OKAY (addr={raddr:#x})"

        # Either an idle gap or a true zero-gap back-to-back next iteration.
        if rng.random() < 0.5:
            await ClockCycles(master.clk, rng.randint(1, 3))
        else:
            tally["zero_gap"] += 1
    return tally
