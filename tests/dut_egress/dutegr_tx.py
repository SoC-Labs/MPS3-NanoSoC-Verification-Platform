"""tests/dut_egress/dutegr_tx.py — the DUTEGR INJECT side (host -> DUT), for
both arms of this bench.

WHAT IS IN HERE, AND WHY IT IS NOT IN tests/common/regmap.py
------------------------------------------------------------
The TX register OFFSETS belong in ``tests/common/regmap.py``'s generated fence,
and they get there by re-running ``tools/gen_regmap.py`` — which this lane
supplies as a patch (``fpga/shell/ip/dut_egress/INTEGRATION/03-regmap-regen.patch``)
rather than applying, because the generated views are another lane's files.
So until that patch lands this module DERIVES the offsets from the RTL itself,
with the generator's own parser (``gen_regmap.parse_idx_decode``) — never a
hand-typed table — and, once ``regmap`` does carry them, asserts the two agree.
A drift between them is then an import error, not a silent wrong address.

The bit fields are hand-written here, as they are for every block in
``regmap.py`` (nothing in the decode idiom carries bit semantics). The contract
prose for them is ``docs/contracts/shell-regmap.md``'s DUTEGR section — also a
patch from this lane (``04-shell-regmap-prose.patch``).

Also here:
  * :class:`InjectMonitor` — the monitor on ``inj_m_*`` (the bridge's port-B
    INGRESS). It is the bench's independent record of what actually left the
    block: whole frames, total beats, AXI-Stream rule violations, mid-frame
    bubbles, and X on the port. The §3 invariant and every byte-exact check is
    asserted against IT, not against the block's own counters.
  * :func:`expected_on_port` — what a staged frame must look like on the port:
    RAW = 0 is ``tests/common/frames.py``'s ``build_frame`` (pad to 60, IEEE
    FCS appended LSB first); RAW = 1 is the staged bytes verbatim.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "tests", "common"))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

from pathlib import Path  # noqa: E402

import cocotb  # noqa: E402
from cocotb.triggers import ReadOnly, RisingEdge  # noqa: E402
from cocotb.utils import get_sim_time  # noqa: E402

import regmap  # noqa: E402
from frames import build_frame  # noqa: E402
import gen_regmap  # noqa: E402

RTL = Path(_ROOT) / "fpga" / "shell" / "ip" / "dut_egress" / "dut_egress.sv"

# --------------------------------------------------------------------------- #
# Offsets — DERIVED from the RTL's IDX_* decode, the way gen_regmap derives them.
# --------------------------------------------------------------------------- #
_DECODE = {name: off for name, off, _note in gen_regmap.parse_idx_decode(RTL)}

_TX_NAMES = ("TX_CTRL", "TX_STATUS", "TX_SPACE", "TX_DATA", "TX_FRAMES", "TX_REJECT",
             "TX_FLUSHED")
_missing = [n for n in _TX_NAMES if n not in _DECODE]
assert not _missing, f"{RTL} has no IDX_ decode for {_missing}"

TX_CTRL = _DECODE["TX_CTRL"]
TX_STATUS = _DECODE["TX_STATUS"]
TX_SPACE = _DECODE["TX_SPACE"]
TX_DATA = _DECODE["TX_DATA"]
TX_FRAMES = _DECODE["TX_FRAMES"]
TX_REJECT = _DECODE["TX_REJECT"]
TX_FLUSHED = _DECODE["TX_FLUSHED"]

# The RX offsets in the GENERATED view must still be the RTL's (the additive
# change must not have moved one), and once the regen patch lands the TX ones
# must agree too.
for _n, _off in _DECODE.items():
    _gen = getattr(regmap, f"DUTEGR_{_n}", None)
    if _gen is not None:
        assert _gen == _off, (
            f"tests/common/regmap.py DUTEGR_{_n} = {_gen:#x} but the RTL decodes "
            f"it at {_off:#x} — re-run tools/gen_regmap.py")
    else:
        assert _n in _TX_NAMES, (
            f"DUTEGR_{_n} vanished from tests/common/regmap.py — the RX map "
            f"must be unchanged by the inject side")

# --------------------------------------------------------------------------- #
# Bit fields (hand-written, like every block's in regmap.py).
# --------------------------------------------------------------------------- #
TX_CTRL_COMMIT = 1 << 0     # W1, self-clearing: publish the staged bytes as one frame
TX_CTRL_ABORT = 1 << 1      # W1, self-clearing: discard them (wins over COMMIT)
TX_CTRL_CLR_CNT = 1 << 2    # W1, self-clearing: TX_FRAMES, TX_REJECT, TX_FLUSHED, REJ, DESYNC
TX_CTRL_FLUSH = 1 << 3      # W1, self-clearing, bounded: discard committed, unsent (-> TX_FLUSHED)
TX_CTRL_RAW = 1 << 8        # rw: 0 = pad + FCS in hardware, 1 = bytes verbatim

TX_STATUS_ROOM = 1 << 0        # a MAX_FRAME frame can be staged, and a slot is free
TX_STATUS_EMPTY = 1 << 1       # nothing committed is left to send
TX_STATUS_DATA_FULL = 1 << 2   # the next staged byte would overflow
TX_STATUS_DESC_FULL = 1 << 3   # no frame slot left
TX_STATUS_REJ = 1 << 4         # sticky: TX_REJECT has moved (a FLUSH does not set it)
TX_STATUS_FLUSH_BUSY = 1 << 5
TX_STATUS_STAGING = 1 << 6     # uncommitted bytes present
TX_STATUS_DESYNC = 1 << 7      # sticky: end-of-frame bit vs descriptor length

TX_SPACE_BYTES_MASK = 0xFFFF
TX_SPACE_SLOTS_SHIFT = 16

# Shipped sizing (shell_bd.tcl CONFIG.* on dut_egress_0 — the same parameters
# as the RX side, and the same values regmap.py already records).
DATA_DEPTH = regmap.DUTEGR_DATA_DEPTH
FRAME_DEPTH = regmap.DUTEGR_FRAME_DEPTH
MAX_FRAME = regmap.DUTEGR_MAX_FRAME
PAD_TO = 60
# Handover amendment A1 (decided by the project lead 2026-09-23): RAW = 0 staged length is
# 14 .. 1514, so the wire frame is 64 .. 1518 (802.3); RAW = 1 keeps
# 1 .. MAX_FRAME (1536), oversize on purpose.
MIN_NORMAL = 14
MAX_NORMAL = min(1514, MAX_FRAME - 4)
MAX_RAW = MAX_FRAME


def expected_on_port(staged: bytes, raw: bool) -> bytes:
    """The bytes a committed frame must carry on inj_m_*, per frames.py."""
    if raw:
        return bytes(staged)
    ethertype = int.from_bytes(staged[12:14], "big")
    return build_frame(staged[:6], staged[6:12], ethertype, staged[14:])


def commit_is_valid(n: int, raw: bool) -> bool:
    return (1 <= n <= MAX_RAW) if raw else (MIN_NORMAL <= n <= MAX_NORMAL)


# --------------------------------------------------------------------------- #
# Register helpers
# --------------------------------------------------------------------------- #
async def rd(axi, base: int, off: int) -> int:
    data, resp = await axi.read(base + off)
    assert resp == 0, f"RRESP={resp} reading DUTEGR offset {off:#x}"
    return data


async def stage(axi, base: int, data: bytes):
    for b in data:
        await axi.write(base + TX_DATA, b)


async def commit(axi, base: int, raw: bool = False):
    await axi.write(base + TX_CTRL, TX_CTRL_COMMIT | (TX_CTRL_RAW if raw else 0))


async def send(axi, base: int, data: bytes, raw: bool = False):
    await stage(axi, base, data)
    await commit(axi, base, raw)


async def counters(axi, base: int):
    """(TX_FRAMES, TX_REJECT, TX_FLUSHED, frames_queued) — queued from
    TX_SPACE's slots."""
    frames = await rd(axi, base, TX_FRAMES)
    rej = await rd(axi, base, TX_REJECT)
    flushed = await rd(axi, base, TX_FLUSHED)
    space = await rd(axi, base, TX_SPACE)
    queued = FRAME_DEPTH - (space >> TX_SPACE_SLOTS_SHIFT)
    return frames, rej, flushed, queued


async def wait_empty(clk, axi, base: int, max_cycles: int = 200_000):
    """Poll TX_STATUS.EMPTY. The bound is generous: a frame waits on the sink."""
    for _ in range(max_cycles // 32):
        if (await rd(axi, base, TX_STATUS)) & TX_STATUS_EMPTY:
            return
        for _ in range(32):
            await RisingEdge(clk)
    raise AssertionError("TX_STATUS.EMPTY never rose — a committed frame never left")


async def assert_invariant(axi, base: int, commits: int, mon=None, what: str = ""):
    """THE §3 INVARIANT, as amended by A2 (the project lead, 2026-09-23):
    TX_FRAMES + TX_REJECT + TX_FLUSHED + frames_queued == COMMITs since CLR_CNT
    — and TX_FRAMES must equal what the MONITOR saw leave, so the block's own
    counter is never the only witness."""
    frames, rej, flushed, queued = await counters(axi, base)
    total = frames + rej + flushed + queued
    assert total == commits, (
        f"§3 invariant broken{(' ' + what) if what else ''}: TX_FRAMES={frames} + "
        f"TX_REJECT={rej} + TX_FLUSHED={flushed} + queued={queued} = {total}, but "
        f"{commits} COMMITs were issued")
    if mon is not None:
        assert frames == len(mon.frames), (
            f"TX_FRAMES={frames} but the monitor on inj_m_* saw "
            f"{len(mon.frames)} whole frames leave{(' ' + what) if what else ''}")
    return frames, rej, flushed, queued


# --------------------------------------------------------------------------- #
# The monitor on inj_m_*
# --------------------------------------------------------------------------- #
def _bit(sig):
    """int value of a handle, or None when any bit is X/Z (a mutant can drive
    X onto the port; the monitor must report it, not crash on it)."""
    s = str(sig.value).strip()
    if not s or any(c not in "01" for c in s):
        return None
    return int(s, 2)


class InjectMonitor:
    """Records every handshake on the inject port and polices AXI-Stream.

    * ``frames``   — whole frames (tlast seen), in order;
    * ``beats``    — every handshake, including those of an unfinished frame
      and those with X on TDATA/TLAST;
    * ``valid_cycles`` — every cycle TVALID was 1, taken or not;
    * ``violations`` — a presented beat withdrawn or changed before it was
      taken (AXI-Stream: once TVALID is up it stays up, with TDATA/TLAST
      stable, until TREADY);
    * ``bubbles``  — cycles with TVALID low in the MIDDLE of a frame (handover
      §4 rule 1: a committed frame is streamed back to back);
    * ``x_cycles`` — cycles with X/Z on TVALID, or on TDATA/TLAST while valid.
    """

    def __init__(self, clk, tdata, tvalid, tready, tlast):
        self.clk, self.tdata, self.tvalid, self.tready, self.tlast = (
            clk, tdata, tvalid, tready, tlast)
        self.frames: list[bytes] = []
        self.beats = 0
        self.violations: list[str] = []
        self.bubbles = 0
        self.x_cycles = 0
        self.valid_cycles = 0
        self.cur = bytearray()

    @classmethod
    def on(cls, dut, clk, prefix: str = "inj_m_"):
        g = lambda n: getattr(dut, prefix + n)  # noqa: E731
        return cls(clk, g("tdata"), g("tvalid"), g("tready"), g("tlast"))

    def start(self):
        cocotb.start_soon(self._run())
        return self

    async def _run(self):
        pending = None          # (tdata, tlast) presented and NOT taken last cycle
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            v = _bit(self.tvalid)
            r = _bit(self.tready)
            d = _bit(self.tdata)
            last = _bit(self.tlast)
            if v is None:
                self.x_cycles += 1
                pending = None
                continue
            if v == 1:
                self.valid_cycles += 1
                if d is None or last is None:
                    self.x_cycles += 1
            if pending is not None:
                if v != 1:
                    self.violations.append(
                        f"TVALID withdrawn before TREADY at {get_sim_time('ns')} ns")
                elif (d, last) != pending:
                    self.violations.append(
                        f"TDATA/TLAST changed while waiting for TREADY at "
                        f"{get_sim_time('ns')} ns")
            if v == 1 and r == 1:
                # EVERY handshake is a beat, X or not: a byte with an unknown
                # TLAST has still left the block.
                self.beats += 1
                self.cur.append(d if d is not None else 0)
                pending = None
                if last == 1:
                    self.frames.append(bytes(self.cur))
                    self.cur = bytearray()
            elif v == 1:
                pending = (d, last)
            else:
                pending = None
                if self.cur:
                    self.bubbles += 1

    def assert_clean(self, what: str = ""):
        tag = f" ({what})" if what else ""
        assert not self.violations, f"AXI-Stream violations on inj_m_*{tag}: {self.violations[:4]}"
        assert self.x_cycles == 0, f"X/Z on inj_m_*{tag} for {self.x_cycles} cycles"
        assert self.bubbles == 0, (
            f"{self.bubbles} mid-frame bubble cycle(s) on inj_m_*{tag} — a committed "
            f"frame must stream back to back")
        assert not self.cur, (
            f"a partial frame of {len(self.cur)} byte(s) is sitting on inj_m_*{tag}: "
            f"{bytes(self.cur[:32]).hex()}…")
