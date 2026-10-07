"""tests/dut_egress/test_dut_egress_csr.py — the DUTEGR capture block on its own.

WHAT THIS ARM IS FOR
--------------------
`test_dut_egress_e2e.py` proves the whole return path (a UDP datagram out to the
DUT over RMII and its echo back through the FIFO). This arm proves the
PROPERTIES OF THE FIFO ITSELF, without a millisecond of RMII serialisation:

  * the block is elaborated at the parameterisation ``shell_bd.tcl`` SHIPS
    (``C_S_AXI_ADDR_WIDTH = 32``), and driven at BASE+offset — bug #1, the CSR
    decode escape that made every shell register read 0 on silicon, was exactly
    a block benched at the RTL default of 12;
  * an unmapped offset in the page reads 0 and — critically — pops NOTHING
    (``DATA`` is a destructive FIFO port: the UARTBR version of this hazard
    silently ate a byte of DUT console data on stray traffic);
  * a frame comes back BYTE FOR BYTE, in order, with ``LAST`` on its final byte
    and nowhere else;
  * overflow is COUNTED, not silent — for both ways of running out (no room for
    the bytes, and no descriptor slot to record the frame in), and for a giant;
  * the invariant ``RX_FRAMES + DROP_FULL + DROP_GIANT == frames presented``;
  * nothing torn ever becomes readable: every frame that DOES come out is one
    of the frames that went in, whole.

CONTROLS (`make control-nodrop`, and `+EGRESS_CONTROL=byteorder` here):
a green run of the above means nothing unless the checks can fail. Each control
inverts one expectation and MUST be seen to fail.

THE INJECT SIDE (tests i..q, 2026-09-23 — docs/planning/HANDOVER_DUT_INJECT.md §6)
------------------------------------------------------------------------------
The block now also has a host -> DUT path: TX registers at 0x20..0x34, a second
commit/rollback FIFO with its clocks swapped, and a framer on ``inj_m_*``. The
bench plays the bridge's port-B INGRESS as the sink (driving ``inj_m_tready``)
and watches the port with ``dutegr_tx.InjectMonitor`` — the independent record
every TX assertion is made against:

  * the TX decode at BASE+offset, at the shipped width;
  * COMMIT / ABORT / FLUSH / every reject reason, each rolled back WHOLE;
  * the §3 invariant (amended, A2)
    ``TX_FRAMES + TX_REJECT + TX_FLUSHED + frames_queued == COMMITs``, and
    TX_FRAMES == frames the monitor saw — after every scenario, across FLUSH,
    and throughout a seeded random sequence that includes FLUSH;
  * the length limits (amended, A1): RAW = 0 accepts 14..1514 staged bytes
    (64..1518 on the wire), RAW = 1 accepts 1..1536 — both edges of both;
  * RAW = 0: padded to 60 and FCS appended, byte-exact against
    ``tests/common/frames.py``'s ``build_frame``; RAW = 1: verbatim;
  * staged bytes never leave before COMMIT, a started frame streams back to
    back, and the source waits on tready with TVALID/TDATA held (AXI-Stream);
  * TX and RX are independent: nothing on one side perturbs the other.

Mutation controls (tests/dut_egress/mutate.py; ``make control-tornframe``,
``make control-norollback``, ``make control-noflushed``,
``make control-flushasreject``) rebuild this arm against RTL with the property
removed and MUST fail.

Clocks: 100 MHz AXI-Lite, 50 MHz capture port — the real dual-clock FIFO, not a
same-clock shortcut.
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from dut_presence import rtl_ready
from frames import check_frame
import dutegr_tx as tx
from regmap import (
    AxiLiteMaster,
    DUTEGR_BASE, DUTEGR_CTRL, DUTEGR_STATUS, DUTEGR_LEVEL, DUTEGR_FRAME_LEN,
    DUTEGR_DATA, DUTEGR_RX_FRAMES, DUTEGR_DROP_FULL, DUTEGR_DROP_GIANT,
    DUTEGR_CTRL_EN, DUTEGR_CTRL_FLUSH, DUTEGR_CTRL_CLR_CNT,
    DUTEGR_STATUS_FRAME_RDY, DUTEGR_STATUS_EMPTY, DUTEGR_STATUS_OVF,
    DUTEGR_STATUS_DESYNC, DUTEGR_STATUS_EN,
    DUTEGR_DATA_VALID, DUTEGR_DATA_LAST,
    DUTEGR_LEVEL_BYTES_MASK, DUTEGR_LEVEL_FRAMES_SHIFT,
    DUTEGR_FRAME_LEN_REM_MASK, DUTEGR_FRAME_LEN_TOTAL_SHIFT,
    DUTEGR_DATA_DEPTH, DUTEGR_FRAME_DEPTH, DUTEGR_MAX_FRAME,
)

_IP = os.path.join(os.path.dirname(__file__), "..", "..",
                   "fpga", "shell", "ip", "dut_egress")
NO_RTL = not rtl_ready(_IP, ["dut_egress.sv", "dutegr_cfifo.sv"])

BASE = DUTEGR_BASE


def _control():
    """The named control this run is exercising, or None for an honest run."""
    v = cocotb.plusargs.get("EGRESS_CONTROL")
    if isinstance(v, (list, tuple)):
        v = v[-1]
    return None if v is None else str(v).strip()


def _pattern(n: int, seed: int = 0) -> bytes:
    """A frame-shaped byte pattern that is NOT a palindrome and has no repeated
    run — so a reversed, rotated or byte-swapped readout cannot pass by luck."""
    return bytes(((seed * 37 + i * 31 + (i >> 3)) & 0xFF) for i in range(n))


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, unit="ns").start())    # 100 MHz
    cocotb.start_soon(Clock(dut.rmii_clk_i, 20, unit="ns").start())    # 50 MHz

    dut.s_axi_aresetn.value = 0
    dut.s_axi_awaddr.value = 0
    dut.s_axi_awprot.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wdata.value = 0
    dut.s_axi_wstrb.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_araddr.value = 0
    dut.s_axi_arprot.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    dut.frm_tdata_i.value = 0
    dut.frm_tvalid_i.value = 0
    dut.frm_tlast_i.value = 0
    # The inject port's sink (bridge port-B ingress): ready unless a test says
    # otherwise. Driven in EVERY test so the RX-only ones see a defined port.
    dut.inj_m_tready.value = 1

    for _ in range(10):
        await RisingEdge(dut.s_axi_aclk)
    dut.s_axi_aresetn.value = 1
    for _ in range(10):
        await RisingEdge(dut.rmii_clk_i)
    return AxiLiteMaster.from_dut(dut)


async def _push(dut, frame: bytes, gap: int = 16):
    """Present one frame at the capture port. tready is a constant 1 by design
    (the bridge must never be backpressured), so every tvalid cycle is a beat."""
    n = len(frame)
    for i, b in enumerate(frame):
        await RisingEdge(dut.rmii_clk_i)
        dut.frm_tdata_i.value = b
        dut.frm_tvalid_i.value = 1
        dut.frm_tlast_i.value = 1 if i == n - 1 else 0
    await RisingEdge(dut.rmii_clk_i)
    dut.frm_tvalid_i.value = 0
    dut.frm_tlast_i.value = 0
    dut.frm_tdata_i.value = 0
    for _ in range(gap):
        await RisingEdge(dut.rmii_clk_i)


async def _rd(axi, off):
    """AxiLiteMaster.read returns (data, rresp); every register in this block is
    OKAY-always, so assert that and hand back the data."""
    data, resp = await axi.read(BASE + off)
    assert resp == 0, f"RRESP={resp} reading offset {off:#x}"
    return data


async def _counters(axi):
    return (await _rd(axi, DUTEGR_RX_FRAMES),
            await _rd(axi, DUTEGR_DROP_FULL),
            await _rd(axi, DUTEGR_DROP_GIANT))


async def _read_frame(axi):
    """Read one whole frame the way firmware must: FRAME_LEN, then that many
    destructive DATA reads. Asserts the two independent records of where the
    frame ends (the descriptor length and the stored LAST marker) agree."""
    st = await _rd(axi, DUTEGR_STATUS)
    assert st & DUTEGR_STATUS_FRAME_RDY, f"no frame ready, STATUS={st:#010x}"
    fl = await _rd(axi, DUTEGR_FRAME_LEN)
    total = (fl >> DUTEGR_FRAME_LEN_TOTAL_SHIFT) & 0xFFFF
    rem = fl & DUTEGR_FRAME_LEN_REM_MASK
    assert rem == total, f"an untouched head frame must have rem == total, got {rem} vs {total}"

    out = bytearray()
    lasts = []
    for i in range(total):
        d = await _rd(axi, DUTEGR_DATA)
        assert d & DUTEGR_DATA_VALID, (
            f"DATA.VALID dropped at byte {i} of {total} — the head frame is "
            f"store-and-forward, so every one of its bytes must be there")
        out.append(d & 0xFF)
        lasts.append(bool(d & DUTEGR_DATA_LAST))
    assert lasts[-1], "LAST must be set on the frame's final byte"
    assert not any(lasts[:-1]), (
        f"LAST set early, at byte offset(s) {[i for i, v in enumerate(lasts[:-1]) if v]}")
    return bytes(out)


# =========================================================================== #
# (a) the shipped parameterisation, and a decode that pops nothing it shouldn't
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_a_defaults_and_base_offset_decode(dut):
    """What this proves: the block responds at BASE+offset with
    C_S_AXI_ADDR_WIDTH=32 (bug #1), and comes out of reset ENABLED with an empty
    FIFO and zeroed counters."""
    axi = await _bring_up(dut)

    ctrl = await _rd(axi, DUTEGR_CTRL)
    assert ctrl & DUTEGR_CTRL_EN, (
        "CTRL.EN must reset to 1 — a capture block that defaults OFF is one "
        "more way for a live DUT to look dead")

    st = await _rd(axi, DUTEGR_STATUS)
    assert st & DUTEGR_STATUS_EMPTY
    assert st & DUTEGR_STATUS_EN
    assert not (st & DUTEGR_STATUS_FRAME_RDY)
    assert not (st & DUTEGR_STATUS_OVF)
    assert not (st & DUTEGR_STATUS_DESYNC)

    assert await _counters(axi) == (0, 0, 0)
    assert await _rd(axi, DUTEGR_LEVEL) == 0
    assert await _rd(axi, DUTEGR_FRAME_LEN) == 0

    # A read of the empty DATA port returns VALID=0 and pops nothing.
    d = await _rd(axi, DUTEGR_DATA)
    assert not (d & DUTEGR_DATA_VALID), f"DATA on an empty FIFO must be invalid, got {d:#010x}"

    # And CTRL round-trips at BASE+offset (a write that the 12-bit decode of
    # bug #1 would have thrown away).
    await axi.write(BASE + DUTEGR_CTRL, 0)
    assert (await _rd(axi, DUTEGR_CTRL)) & DUTEGR_CTRL_EN == 0
    await axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN)
    assert (await _rd(axi, DUTEGR_CTRL)) & DUTEGR_CTRL_EN


@cocotb.test(skip=NO_RTL)
async def test_b_unmapped_offsets_pop_nothing(dut):
    """What this proves: the destructive-alias hazard is closed. UARTBR once
    decoded only addr[4:2], so a read of an 'unmapped' offset ALIASED onto a
    FIFO data window and silently ate a byte. Here a frame is queued, every
    unmapped offset in reach is read, and the frame must still come out whole."""
    axi = await _bring_up(dut)
    frame = _pattern(64, seed=3)
    await _push(dut, frame)

    # 0x20..0x38 were unmapped until the inject side took them (2026-09-23,
    # TX_FLUSHED @0x38 by amendment A2); the first unmapped word is now 0x3C.
    # Every TX register is read too — a read of one returns TX state, and must
    # pop nothing from the RX FIFO.
    for off in (0x3C, 0x40, 0x44, 0x100, 0x1000, 0x4000, 0xFFFC):
        v = await _rd(axi, off)
        assert v == 0, f"unmapped offset {off:#x} read {v:#010x}, expected 0"
    for off in (tx.TX_CTRL, tx.TX_STATUS, tx.TX_SPACE, tx.TX_DATA,
                tx.TX_FRAMES, tx.TX_REJECT, tx.TX_FLUSHED):
        await _rd(axi, off)

    got = await _read_frame(axi)
    assert got == frame, (
        "a read of an unmapped offset popped a byte — this is the UARTBR "
        f"destructive-alias defect: expected {frame.hex()}, got {got.hex()}")


# =========================================================================== #
# (c) a frame comes back byte for byte  — and the BYTE-ORDER control
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_c_frame_round_trip_byte_exact(dut):
    """What this proves: bytes in == bytes out, in order, with LAST on the final
    byte only, and LEVEL/FRAME_LEN counting down correctly as they are read.

    CONTROL `+EGRESS_CONTROL=byteorder` compares against the frame REVERSED; if
    the comparison below is real, that run fails."""
    axi = await _bring_up(dut)
    frame = _pattern(97, seed=11)
    await _push(dut, frame)

    lvl = await _rd(axi, DUTEGR_LEVEL)
    assert (lvl & DUTEGR_LEVEL_BYTES_MASK) == len(frame), (
        f"LEVEL bytes = {lvl & DUTEGR_LEVEL_BYTES_MASK}, expected {len(frame)}")
    assert (lvl >> DUTEGR_LEVEL_FRAMES_SHIFT) == 1

    got = await _read_frame(axi)

    expect = frame
    if _control() == "byteorder":
        expect = bytes(reversed(frame))
        dut._log.info("CONTROL byteorder: expecting the frame REVERSED — this run must FAIL")
    assert got == expect, (
        f"frame must come back byte-for-byte: expected {expect.hex()}, got {got.hex()}")

    st = await _rd(axi, DUTEGR_STATUS)
    assert st & DUTEGR_STATUS_EMPTY
    assert not (st & DUTEGR_STATUS_FRAME_RDY)
    assert not (st & DUTEGR_STATUS_DESYNC), (
        "DESYNC latched: the stored end-of-frame marker and the descriptor "
        "length disagree about where the frame ends")
    assert await _counters(axi) == (1, 0, 0)


@cocotb.test(skip=NO_RTL)
async def test_d_frames_queue_in_order(dut):
    """What this proves: several frames queue and come back in arrival order
    with their own lengths — the descriptor FIFO is per-frame, not a guess."""
    axi = await _bring_up(dut)
    frames = [_pattern(n, seed=n) for n in (64, 65, 120, 64)]
    for f in frames:
        await _push(dut, f)

    lvl = await _rd(axi, DUTEGR_LEVEL)
    assert (lvl >> DUTEGR_LEVEL_FRAMES_SHIFT) == len(frames)
    assert (lvl & DUTEGR_LEVEL_BYTES_MASK) == sum(len(f) for f in frames)

    for i, f in enumerate(frames):
        got = await _read_frame(axi)
        assert got == f, f"frame {i} came back wrong: {got.hex()} != {f.hex()}"
    assert await _counters(axi) == (len(frames), 0, 0)


# =========================================================================== #
# (e) overflow is COUNTED, not silent  — and the DROP control
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_e_overflow_is_counted_not_silent(dut):
    """What this proves, for BOTH ways of running out of room:

      1. descriptor exhaustion — more frames than FRAME_DEPTH, all of which fit
         byte-wise;
      2. byte exhaustion — fewer frames, but more bytes than DATA_DEPTH;

    ...that the excess is DROPPED WHOLE and COUNTED (DROP_FULL moves,
    STATUS.OVF latches), that the invariant
    RX_FRAMES + DROP_FULL + DROP_GIANT == frames presented holds, and that every
    frame that does come out is one of the frames that went in, INTACT — no
    torn frame ever becomes readable.

    CONTROL `+EGRESS_CONTROL=nodrop` asserts DROP_FULL == 0 instead; if the
    counter is real, that run fails."""
    axi = await _bring_up(dut)
    control = _control() == "nodrop"

    # --- 1. descriptor exhaustion: 20 x 64 B = 1280 B, well under DATA_DEPTH --
    n_frames = DUTEGR_FRAME_DEPTH + 4
    frames = [_pattern(64, seed=i) for i in range(n_frames)]
    for f in frames:
        await _push(dut, f)

    rx, full, giant = await _counters(axi)
    if control:
        dut._log.info("CONTROL nodrop: asserting DROP_FULL == 0 — this run must FAIL")
        assert full == 0, (
            f"CONTROL: DROP_FULL is {full}. The drop counter IS real, which is "
            f"what this control exists to demonstrate.")
    assert full > 0, (
        f"{n_frames} frames into a {DUTEGR_FRAME_DEPTH}-deep descriptor FIFO "
        f"must drop some, and the drop must be COUNTED: DROP_FULL={full}")
    assert giant == 0, "no frame here exceeds MAX_FRAME"
    assert rx + full + giant == n_frames, (
        f"every presented frame must be accounted for exactly once: "
        f"RX={rx} + FULL={full} + GIANT={giant} != {n_frames}")
    st = await _rd(axi, DUTEGR_STATUS)
    assert st & DUTEGR_STATUS_OVF, "STATUS.OVF must latch on any drop"

    # Whatever survived must be a PREFIX of what was sent, each frame whole.
    for i in range(rx):
        got = await _read_frame(axi)
        assert got == frames[i], (
            f"surviving frame {i} is not intact — a dropped frame must be "
            f"dropped WHOLE, never truncated into the readable stream")
    st = await _rd(axi, DUTEGR_STATUS)
    assert st & DUTEGR_STATUS_EMPTY
    assert not (st & DUTEGR_STATUS_DESYNC)

    # --- 2. byte exhaustion ------------------------------------------------
    await axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN | DUTEGR_CTRL_CLR_CNT)
    assert await _counters(axi) == (0, 0, 0)

    big = 500
    n_big = 8
    bigs = [_pattern(big, seed=100 + i) for i in range(n_big)]
    for f in bigs:
        await _push(dut, f)

    rx, full, giant = await _counters(axi)
    assert rx * big <= DUTEGR_DATA_DEPTH, (
        f"{rx} x {big} B committed exceeds the {DUTEGR_DATA_DEPTH} B FIFO")
    assert full > 0, f"{n_big * big} B into a {DUTEGR_DATA_DEPTH} B FIFO must drop"
    assert giant == 0
    assert rx + full + giant == n_big, (
        f"RX={rx} + FULL={full} + GIANT={giant} != {n_big} presented")
    for i in range(rx):
        got = await _read_frame(axi)
        assert got == bigs[i], f"surviving big frame {i} is not intact"


@cocotb.test(skip=NO_RTL)
async def test_f_giant_is_dropped_whole_and_counted(dut):
    """What this proves: a frame longer than MAX_FRAME is rolled back WHOLE and
    counted in its own bucket — never truncated onto the read port, where it
    would look like a real frame with corrupt bytes. A following normal frame
    still arrives, so the rollback leaves the FIFO usable."""
    axi = await _bring_up(dut)

    await _push(dut, _pattern(DUTEGR_MAX_FRAME + 64, seed=7))
    rx, full, giant = await _counters(axi)
    assert (rx, full, giant) == (0, 0, 1), (
        f"a > MAX_FRAME frame must land in DROP_GIANT alone: RX={rx} FULL={full} GIANT={giant}")

    st = await _rd(axi, DUTEGR_STATUS)
    assert not (st & DUTEGR_STATUS_FRAME_RDY), "a giant must not become readable"
    assert st & DUTEGR_STATUS_EMPTY
    assert st & DUTEGR_STATUS_OVF
    d = await _rd(axi, DUTEGR_DATA)
    assert not (d & DUTEGR_DATA_VALID)

    good = _pattern(64, seed=8)
    await _push(dut, good)
    assert await _read_frame(axi) == good, (
        "the FIFO must be usable after a giant is rolled back")


# =========================================================================== #
# (g) the two controls firmware has: EN and FLUSH
# =========================================================================== #
@cocotb.test(skip=NO_RTL)
async def test_g_disabled_captures_nothing(dut):
    """What this proves: CTRL.EN = 0 means NOT LISTENING — a frame presented
    while disabled moves no counter at all (it is not a drop; nothing was
    dropped), and re-enabling mid-stream cannot capture a half frame because EN
    is sampled only at a frame's FIRST beat."""
    axi = await _bring_up(dut)
    await axi.write(BASE + DUTEGR_CTRL, 0)
    for _ in range(8):
        await RisingEdge(dut.rmii_clk_i)
    assert not ((await _rd(axi, DUTEGR_STATUS)) & DUTEGR_STATUS_EN)

    await _push(dut, _pattern(64, seed=21))
    assert await _counters(axi) == (0, 0, 0), (
        "a frame presented while CTRL.EN = 0 must not be counted at all")
    assert not ((await _rd(axi, DUTEGR_STATUS)) & DUTEGR_STATUS_FRAME_RDY)

    await axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN)
    for _ in range(8):
        await RisingEdge(dut.rmii_clk_i)
    frame = _pattern(64, seed=22)
    await _push(dut, frame)
    assert await _read_frame(axi) == frame
    assert await _counters(axi) == (1, 0, 0)


@cocotb.test(skip=NO_RTL)
async def test_h_flush_discards_the_previous_rms_frames(dut):
    """What this proves: CTRL.FLUSH empties the FIFO in bounded time (it is a
    drain in the AXI domain, not a second reset), which is what a swap needs —
    the frames still queued belong to the RM that just went away."""
    axi = await _bring_up(dut)
    for i in range(4):
        await _push(dut, _pattern(64, seed=40 + i))
    assert (await _rd(axi, DUTEGR_LEVEL)) != 0

    await axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN | DUTEGR_CTRL_FLUSH)
    for _ in range(DUTEGR_DATA_DEPTH + DUTEGR_FRAME_DEPTH + 32):
        await RisingEdge(dut.s_axi_aclk)

    st = await _rd(axi, DUTEGR_STATUS)
    assert st & DUTEGR_STATUS_EMPTY, f"FLUSH left bytes behind, STATUS={st:#010x}"
    assert not (st & DUTEGR_STATUS_FRAME_RDY)
    assert (await _rd(axi, DUTEGR_LEVEL)) == 0

    frame = _pattern(70, seed=44)
    await _push(dut, frame)
    assert await _read_frame(axi) == frame, "the FIFO must be usable after a FLUSH"


# =========================================================================== #
# =========================================================================== #
# THE INJECT SIDE (host -> DUT) — handover §6, BLOCK=csr
# =========================================================================== #
# =========================================================================== #
_HDR = bytes.fromhex("020000000002" "020000000001" "88b5")   # dst, src, local-experimental type


def _staged(n: int, seed: int = 0) -> bytes:
    """A frame as software stages it (no FCS): a real header when there is room
    for one, then a non-palindromic pattern."""
    body = _pattern(max(n, 0), seed)
    return (_HDR + body[14:])[:n] if n >= 14 else body


async def _bring_up_tx(dut):
    axi = await _bring_up(dut)
    mon = tx.InjectMonitor.on(dut, dut.rmii_clk_i).start()
    return axi, mon


async def _rmii_cycles(dut, n):
    for _ in range(n):
        await RisingEdge(dut.rmii_clk_i)


async def _wait_frames(dut, mon, n, max_cycles=20000):
    """Wait until the monitor has seen n whole frames."""
    for _ in range(max_cycles):
        if len(mon.frames) >= n:
            return
        await RisingEdge(dut.rmii_clk_i)
    raise AssertionError(f"only {len(mon.frames)} of {n} frames left inj_m_* in {max_cycles} cycles")


async def _tx_quiet(dut, axi, mon, commits, what):
    """Quiescent-point checks every TX scenario ends with: the TX side is empty,
    the §3 invariant holds, the monitor saw a clean port, DESYNC never latched."""
    await tx.wait_empty(dut.s_axi_aclk, axi, BASE)
    await _rmii_cycles(dut, 16)
    await tx.assert_invariant(axi, BASE, commits, mon, what)
    st = await _rd(axi, tx.TX_STATUS)
    assert not (st & tx.TX_STATUS_DESYNC), f"TX DESYNC latched ({what}), TX_STATUS={st:#x}"
    assert not (st & tx.TX_STATUS_STAGING), f"STAGING left set ({what})"
    mon.assert_clean(what)


# --------------------------------------------------------------------------- #
# (i) the TX decode, at the shipped width, and its reset state
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_i_tx_decode_and_defaults(dut):
    """What this proves: the seven TX registers answer at BASE+0x20..0x38 with
    C_S_AXI_ADDR_WIDTH=32; reset state is RAW=0, ROOM|EMPTY, all space free,
    counters zero; TX_DATA reads 0 and stages nothing; RAW is a byte-lane-1 rw
    bit that a lane-0 action write does not disturb; one staged byte shows as
    STAGING and costs one byte of TX_SPACE; ABORT returns it; the first word
    after the TX block is unmapped again."""
    axi, mon = await _bring_up_tx(dut)

    assert await _rd(axi, tx.TX_CTRL) == 0
    st = await _rd(axi, tx.TX_STATUS)
    assert st == (tx.TX_STATUS_ROOM | tx.TX_STATUS_EMPTY), f"TX_STATUS at reset = {st:#x}"
    sp = await _rd(axi, tx.TX_SPACE)
    assert sp == ((tx.FRAME_DEPTH << tx.TX_SPACE_SLOTS_SHIFT) | tx.DATA_DEPTH), (
        f"TX_SPACE at reset = {sp:#010x}")
    assert await _rd(axi, tx.TX_FRAMES) == 0
    assert await _rd(axi, tx.TX_REJECT) == 0
    assert await _rd(axi, tx.TX_FLUSHED) == 0

    # TX_DATA is write-only: a read returns 0 and stages nothing.
    assert await _rd(axi, tx.TX_DATA) == 0
    assert not ((await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_STAGING)
    assert await _rd(axi, tx.TX_SPACE) == sp

    # RAW: lane 1 only.
    await axi.write_bytes(BASE + tx.TX_CTRL, tx.TX_CTRL_RAW, strb=0b0010)
    assert await _rd(axi, tx.TX_CTRL) == tx.TX_CTRL_RAW
    await axi.write_bytes(BASE + tx.TX_CTRL, 0, strb=0b0001)      # lane 0, no action
    assert await _rd(axi, tx.TX_CTRL) == tx.TX_CTRL_RAW, "a lane-0 write disturbed RAW"
    await axi.write(BASE + tx.TX_CTRL, 0)
    assert await _rd(axi, tx.TX_CTRL) == 0

    # A TX_DATA write without lane 0 stages nothing.
    await axi.write_bytes(BASE + tx.TX_DATA, 0xAA, strb=0b1110)
    assert not ((await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_STAGING)

    # One byte staged: STAGING, and it is HELD — one byte of TX_SPACE gone.
    await axi.write(BASE + tx.TX_DATA, 0x55)
    st = await _rd(axi, tx.TX_STATUS)
    assert st & tx.TX_STATUS_STAGING
    assert (await _rd(axi, tx.TX_SPACE)) & tx.TX_SPACE_BYTES_MASK == tx.DATA_DEPTH - 1
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_ABORT)
    assert not ((await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_STAGING)
    assert await _rd(axi, tx.TX_SPACE) == sp

    for off in (0x3C, 0x40):
        assert await _rd(axi, off) == 0, f"offset {off:#x} must be unmapped"

    await _rmii_cycles(dut, 64)
    assert mon.beats == 0, "nothing was committed, yet bytes left inj_m_*"
    await _tx_quiet(dut, axi, mon, commits=0, what="decode/defaults")


# --------------------------------------------------------------------------- #
# (j) RAW = 0: pad to 60 + IEEE FCS, byte-exact against frames.py
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_j_raw0_pads_and_appends_fcs_byte_exact(dut):
    """What this proves: with RAW = 0 the block is a normal NIC. Software stages
    dst+src+type+payload with NO FCS; what leaves on inj_m_* is exactly
    frames.build_frame() of it — zero-padded to 60 bytes when short, then the
    IEEE CRC-32 FCS LSB first — at every length class: header only, one short of
    the pad boundary, on it, one past it, mid, and the RAW = 0 maximum
    (1514 staged -> 1518 on the wire, amendment A1). Every one of them passes
    frames.check_frame's 64..1518 envelope as well as its FCS check."""
    axi, mon = await _bring_up_tx(dut)
    lengths = [14, 15, 42, 59, 60, 61, 100, 1000, tx.MAX_NORMAL]
    sent = []
    for i, n in enumerate(lengths):
        staged = _staged(n, seed=50 + i)
        await tx.send(axi, BASE, staged)
        sent.append(staged)
        await _wait_frames(dut, mon, len(sent))
        got = mon.frames[-1]
        want = tx.expected_on_port(staged, raw=False)
        assert got == want, (
            f"RAW=0, {n} bytes staged: the port carried {len(got)} bytes, expected "
            f"{len(want)} (frames.build_frame):\n  got  {got.hex()}\n  want {want.hex()}")
        fcs_ok, len_ok, why = check_frame(got)
        assert fcs_ok and len_ok, f"RAW=0 frame of {n} staged bytes fails check_frame: {why}"
        assert len(got) == max(n, tx.PAD_TO) + 4
    await _tx_quiet(dut, axi, mon, commits=len(lengths), what="RAW=0 pad+FCS")
    assert await _rd(axi, tx.TX_REJECT) == 0


# --------------------------------------------------------------------------- #
# (k) RAW = 1: verbatim — the fault-injection mode
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_k_raw1_sends_bytes_verbatim(dut):
    """What this proves: with RAW = 1 the bytes go out exactly as written — no
    pad, no FCS — including the things RAW exists for: a 1-byte frame, a runt
    with no header at all, a frame carrying a deliberately WRONG FCS (which must
    survive intact), a correct software-built frame, and MAX_FRAME bytes."""
    axi, mon = await _bring_up_tx(dut)
    good = tx.expected_on_port(_staged(50, seed=7), raw=False)       # 64 B incl. FCS
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    cases = [bytes([0xA5]), _pattern(13, seed=9), bytes(bad), good,
             _pattern(tx.MAX_RAW, seed=11)]
    for i, frame in enumerate(cases):
        await tx.send(axi, BASE, frame, raw=True)
        await _wait_frames(dut, mon, i + 1)
        assert mon.frames[-1] == frame, (
            f"RAW=1 case {i} ({len(frame)} B) was not sent verbatim:\n"
            f"  got  {mon.frames[-1][:64].hex()}…\n  want {frame[:64].hex()}…")
    assert not check_frame(mon.frames[2])[0], "the bad-FCS frame must still be bad on the port"
    assert check_frame(mon.frames[3])[0], "the software-built frame must still be good"
    assert await _rd(axi, tx.TX_CTRL) == tx.TX_CTRL_RAW, (
        "tx.commit(raw=True) writes RAW=1 in the same word as COMMIT, and it sticks")
    await _tx_quiet(dut, axi, mon, commits=len(cases), what="RAW=1 verbatim")


# --------------------------------------------------------------------------- #
# (l) every reject reason, ABORT, and COMMIT|ABORT — each rolled back WHOLE
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_l_rejects_and_abort_roll_back_whole(dut):
    """What this proves: a COMMIT that is empty, shorter than 14 bytes (RAW=0),
    longer than the limit (RAW=0: 1515; RAW=1: 1537), or that overflowed while
    staging is REJECTED — TX_REJECT counts it, REJ latches, and NOT ONE of its
    bytes ever leaves. ABORT, and COMMIT|ABORT in one write, discard without
    counting anything. After every one of them the next good frame goes out
    byte-exact, which is what 'rolled back whole' means from the outside.

    CONTROL `make control-norollback` removes the rollback; this test goes red."""
    axi, mon = await _bring_up_tx(dut)
    commits = 0
    good_sent = []

    async def good(seed):
        nonlocal commits
        f = _staged(64, seed=seed)
        await tx.send(axi, BASE, f)
        commits += 1
        good_sent.append(tx.expected_on_port(f, raw=False))
        await _wait_frames(dut, mon, len(good_sent))
        assert mon.frames == good_sent, (
            f"after a discarded frame, the next good frame came out wrong:\n"
            f"  got  {mon.frames[-1].hex()}\n  want {good_sent[-1].hex()}")

    rejects = [
        ("empty COMMIT", b"", False),
        ("RAW=0, 13 bytes", _staged(13, seed=1), False),
        ("RAW=0, 1515 bytes", _staged(tx.MAX_NORMAL + 1, seed=2), False),
        ("RAW=1, MAX_FRAME+1 bytes", _pattern(tx.MAX_RAW + 1, seed=3), True),
    ]
    for k, (what, frame, raw) in enumerate(rejects):
        beats0 = mon.beats
        await tx.send(axi, BASE, frame, raw=raw)
        commits += 1
        await _rmii_cycles(dut, 64)
        assert mon.beats == beats0, f"{what}: a rejected frame put bytes on inj_m_*"
        assert await _rd(axi, tx.TX_REJECT) == k + 1, f"{what}: TX_REJECT did not count it"
        st = await _rd(axi, tx.TX_STATUS)
        assert st & tx.TX_STATUS_REJ, f"{what}: REJ did not latch"
        assert not (st & tx.TX_STATUS_STAGING)
        await good(seed=10 + k)

    # ABORT mid-stage: no counter, nothing leaves.
    await tx.stage(axi, BASE, _staged(40, seed=20))
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_ABORT)
    await _rmii_cycles(dut, 64)
    await good(seed=21)
    # COMMIT|ABORT in ONE write: ABORT wins, and it is not a commit.
    await tx.stage(axi, BASE, _staged(40, seed=22))
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_COMMIT | tx.TX_CTRL_ABORT)
    await _rmii_cycles(dut, 64)
    await good(seed=23)
    assert await _rd(axi, tx.TX_REJECT) == len(rejects), "ABORT must not count as a reject"

    # Overflow while staging: park a big frame in the FIFO (sink not ready),
    # then stage more than the rest of the FIFO can hold.
    dut.inj_m_tready.value = 0
    big = _staged(1500, seed=30)
    await tx.send(axi, BASE, big)
    commits += 1
    await tx.stage(axi, BASE, _staged(tx.DATA_DEPTH - 1500 + 8, seed=31))
    st = await _rd(axi, tx.TX_STATUS)
    assert st & tx.TX_STATUS_DATA_FULL, f"DATA_FULL not set by an overflow, TX_STATUS={st:#x}"
    assert not (st & tx.TX_STATUS_ROOM)
    await tx.commit(axi, BASE)
    commits += 1
    assert await _rd(axi, tx.TX_REJECT) == len(rejects) + 1, "the overflowed frame was not rejected"
    dut.inj_m_tready.value = 1
    good_sent.append(tx.expected_on_port(big, raw=False))
    await _wait_frames(dut, mon, len(good_sent))
    assert mon.frames == good_sent, "an overflowed frame leaked onto the port"
    await good(seed=32)

    await _tx_quiet(dut, axi, mon, commits, "rejects/abort")

    # CLR_CNT (TX only), at a quiescent point: counters and sticky bits clear.
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_CLR_CNT)
    assert (await tx.counters(axi, BASE)) == (0, 0, 0, 0)
    assert not ((await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_REJ)


# --------------------------------------------------------------------------- #
# (m) staged bytes are invisible until COMMIT — the torn-frame detector
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_m_nothing_leaves_before_commit(dut):
    """What this proves: handover §4 rules 1-2. Bytes sit staged for hundreds of
    cycles with an always-ready sink and NOT ONE leaves until COMMIT; an
    ABORTed frame's bytes never leave at all; the frame after the ABORT is
    exactly itself. A torn frame can never reach the DUT.

    CONTROLS: `make control-tornframe` (the read side starts before COMMIT) and
    `make control-norollback` (the ABORT is not rolled back) both go red here."""
    axi, mon = await _bring_up_tx(dut)

    a = _staged(40, seed=61)
    await tx.stage(axi, BASE, a)
    await _rmii_cycles(dut, 300)
    assert mon.beats == 0 and mon.valid_cycles == 0, (
        f"the read side PRESENTED staged bytes BEFORE COMMIT ({mon.valid_cycles} "
        f"valid cycles, {mon.beats} taken, {mon.x_cycles} with X on TDATA/TLAST — "
        f"it has no descriptor to frame them with): a torn frame reached the "
        f"bridge: {bytes(mon.cur).hex()}")
    await tx.commit(axi, BASE)
    await _wait_frames(dut, mon, 1)
    assert mon.frames == [tx.expected_on_port(a, raw=False)]

    beats = mon.beats
    await tx.stage(axi, BASE, _staged(40, seed=62))
    await _rmii_cycles(dut, 300)
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_ABORT)
    await _rmii_cycles(dut, 300)
    assert mon.beats == beats, "bytes of an ABORTed frame left inj_m_*"

    c = _staged(50, seed=63)
    await tx.send(axi, BASE, c)
    await _wait_frames(dut, mon, 2)
    assert mon.frames[1] == tx.expected_on_port(c, raw=False), (
        "the frame committed after an ABORT is not itself — the ABORTed bytes "
        f"were not rolled back:\n  got  {mon.frames[1].hex()}\n"
        f"  want {tx.expected_on_port(c, raw=False).hex()}")
    await _tx_quiet(dut, axi, mon, commits=2, what="nothing before COMMIT")


# --------------------------------------------------------------------------- #
# (n) a source may wait — and once started it streams back to back
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_n_waits_on_tready_then_streams_back_to_back(dut):
    """What this proves: handover §4 rule 1. With the sink not ready the framer
    WAITS — the frame stays queued and counts in frames_queued (the invariant's
    third term, non-zero here) — and when the sink is ready the frame streams
    with no bubble. With a sink that stalls MID-frame, TVALID/TDATA/TLAST are
    held until taken (AXI-Stream) and the frame still arrives whole."""
    axi, mon = await _bring_up_tx(dut)

    dut.inj_m_tready.value = 0
    f = _staged(100, seed=71)
    await tx.send(axi, BASE, f)
    await _rmii_cycles(dut, 1000)
    assert mon.beats == 0, "a frame was taken while tready was low"
    st = await _rd(axi, tx.TX_STATUS)
    assert not (st & tx.TX_STATUS_EMPTY), "a queued frame must clear EMPTY"
    got = await tx.assert_invariant(axi, BASE, 1, mon, "frame parked")
    assert got == (0, 0, 0, 1), f"(TX_FRAMES, TX_REJECT, TX_FLUSHED, queued) = {got}"

    dut.inj_m_tready.value = 1
    await _wait_frames(dut, mon, 1)
    assert mon.frames[0] == tx.expected_on_port(f, raw=False)

    # A sink that stalls mid-frame.
    stop = False

    async def stall():
        k = 0
        while not stop:
            await RisingEdge(dut.rmii_clk_i)
            k += 1
            dut.inj_m_tready.value = 0 if (k % 17) < 5 else 1
        dut.inj_m_tready.value = 1

    cocotb.start_soon(stall())
    g = _staged(300, seed=72)
    await tx.send(axi, BASE, g)
    await _wait_frames(dut, mon, 2)
    stop = True
    assert mon.frames[1] == tx.expected_on_port(g, raw=False)
    await _tx_quiet(dut, axi, mon, commits=2, what="wait + stall")


# --------------------------------------------------------------------------- #
# (o) FLUSH: bounded, discards committed-but-unsent, never cuts a started frame
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_o_flush_discards_unsent_and_never_cuts_a_started_frame(dut):
    """What this proves: TX_CTRL.FLUSH discards every committed frame not yet
    started, in bounded time; a flushed frame never leaves, and is COUNTED — in
    TX_FLUSHED @0x38, not in TX_REJECT, and REJ does not latch (amendment A2) —
    so the §3 invariant survives the FLUSH; a frame already on the port when
    FLUSH arrives finishes whole (TX_FRAMES); a frame committed in the SAME
    write as the FLUSH is flushed too; a frame committed AFTER the FLUSH is
    not; the block is usable afterwards; CLR_CNT clears TX_FLUSHED.

    CONTROLS `make control-noflushed` (0x38 reads 0) and
    `make control-flushasreject` (the pre-A2 accounting) go red here."""
    axi, mon = await _bring_up_tx(dut)
    commits = 0

    # Three frames parked behind a not-ready sink, then FLUSH.
    dut.inj_m_tready.value = 0
    for s in range(3):
        await tx.send(axi, BASE, _staged(80, seed=80 + s))
        commits += 1
    await tx.assert_invariant(axi, BASE, commits, mon, "3 parked")
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_FLUSH)
    assert (await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_FLUSH_BUSY
    bound = 4 * (tx.DATA_DEPTH + 4 * tx.FRAME_DEPTH)          # AXI cycles
    for waited in range(0, bound, 32):
        if not ((await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_FLUSH_BUSY):
            break
        for _ in range(32):
            await RisingEdge(dut.s_axi_aclk)
    else:
        raise AssertionError(f"FLUSH_BUSY still set after {bound} AXI cycles — not bounded")
    st = await _rd(axi, tx.TX_STATUS)
    assert st & tx.TX_STATUS_EMPTY
    # TX_REJECT first, so each A2 control fails on its OWN property:
    # control-flushasreject here, control-noflushed on the next check.
    assert await _rd(axi, tx.TX_REJECT) == 0, (
        f"a FLUSH moved TX_REJECT to {await _rd(axi, tx.TX_REJECT)}: flushed "
        f"frames are TX_FLUSHED, not rejects (amendment A2)")
    assert not (st & tx.TX_STATUS_REJ), "a FLUSH must not latch REJ (A2)"
    assert await _rd(axi, tx.TX_FLUSHED) == 3, (
        f"TX_FLUSHED @0x38 = {await _rd(axi, tx.TX_FLUSHED)} after flushing 3 "
        f"parked frames — a flushed frame must be COUNTED, in TX_FLUSHED")
    assert (await tx.counters(axi, BASE)) == (0, 0, 3, 0)
    dut.inj_m_tready.value = 1
    await _rmii_cycles(dut, 500)
    assert mon.beats == 0, "a flushed frame left inj_m_*"
    await tx.assert_invariant(axi, BASE, commits, mon, "after FLUSH")

    # A frame already on the port when FLUSH arrives finishes whole; the one
    # queued behind it is discarded.
    j = _staged(800, seed=90)
    await tx.send(axi, BASE, j)
    commits += 1
    for _ in range(4000):
        if mon.beats:
            break
        await RisingEdge(dut.rmii_clk_i)
    assert mon.beats, "the frame never started"
    dut.inj_m_tready.value = 0            # the sink stalls mid-frame
    k = _staged(60, seed=91)
    await tx.send(axi, BASE, k)
    commits += 1
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_FLUSH)
    await _rmii_cycles(dut, 200)
    assert (await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_FLUSH_BUSY, (
        "FLUSH must wait for the frame already on the port, not cut it")
    dut.inj_m_tready.value = 1
    await _wait_frames(dut, mon, 1)
    assert mon.frames == [tx.expected_on_port(j, raw=False)], "the started frame was cut"
    await _rmii_cycles(dut, 200)
    assert not ((await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_FLUSH_BUSY)
    assert len(mon.frames) == 1, "the frame queued behind a FLUSH still left"
    await tx.assert_invariant(axi, BASE, commits, mon, "started + queued, FLUSH")

    # COMMIT|FLUSH in ONE write: that frame is flushed too. A frame committed
    # AFTER the FLUSH write is not owed to it and goes out once it is done.
    dut.inj_m_tready.value = 0
    await tx.stage(axi, BASE, _staged(70, seed=93))
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_COMMIT | tx.TX_CTRL_FLUSH)
    commits += 1
    late = _staged(66, seed=94)
    await tx.send(axi, BASE, late)
    commits += 1
    await _rmii_cycles(dut, 32)
    dut.inj_m_tready.value = 1
    await _wait_frames(dut, mon, 2)
    assert mon.frames[1] == tx.expected_on_port(late, raw=False), (
        "the frame after COMMIT|FLUSH is not the one committed after the FLUSH")
    assert await _rd(axi, tx.TX_FLUSHED) == 5, (
        f"TX_FLUSHED = {await _rd(axi, tx.TX_FLUSHED)}; expected 3 + 1 + 1 "
        f"(the COMMIT|FLUSH frame is owed to its own FLUSH)")

    h = _staged(70, seed=92)
    await tx.send(axi, BASE, h)
    commits += 1
    await _wait_frames(dut, mon, 3)
    assert mon.frames[2] == tx.expected_on_port(h, raw=False)
    await _tx_quiet(dut, axi, mon, commits, "FLUSH")
    assert (await tx.counters(axi, BASE)) == (3, 0, 5, 0)

    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_CLR_CNT)
    assert (await tx.counters(axi, BASE)) == (0, 0, 0, 0), "CLR_CNT must clear TX_FLUSHED too"


# --------------------------------------------------------------------------- #
# (p) TX and RX are independent
# --------------------------------------------------------------------------- #
async def _rx_snapshot(axi):
    return (await _rd(axi, DUTEGR_CTRL), await _rd(axi, DUTEGR_STATUS),
            await _rd(axi, DUTEGR_LEVEL), await _rd(axi, DUTEGR_FRAME_LEN),
            await _counters(axi))


async def _tx_snapshot(axi):
    return (await _rd(axi, tx.TX_CTRL), await _rd(axi, tx.TX_STATUS),
            await _rd(axi, tx.TX_SPACE), await tx.counters(axi, BASE))


@cocotb.test(skip=NO_RTL)
async def test_p_tx_and_rx_are_independent(dut):
    """What this proves: nothing on the TX side perturbs the RX side and vice
    versa. An RX frame sits captured while the TX side stages, aborts, commits,
    is rejected, clears its counters, flushes, and has every register read —
    and the RX side's every register is unchanged and the frame reads back
    whole. Then a TX frame sits staged while the RX side clears its counters,
    flushes, toggles EN, pops DATA and captures another frame — and the TX
    staging is untouched, its counters are not cleared by the RX CLR_CNT, and
    the frame goes out byte-exact. The RX DATA pop decodes ONLY at 0x10."""
    axi, mon = await _bring_up_tx(dut)

    rx_frame = _pattern(80, seed=5)
    await _push(dut, rx_frame)
    before = await _rx_snapshot(axi)

    # --- TX activity of every kind ---
    await tx.stage(axi, BASE, _staged(30, seed=100))
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_ABORT)
    await tx.send(axi, BASE, _staged(64, seed=101))
    await tx.send(axi, BASE, _staged(5, seed=102))                  # rejected
    await tx.wait_empty(dut.s_axi_aclk, axi, BASE)
    await _rmii_cycles(dut, 16)
    await tx.assert_invariant(axi, BASE, 2, mon, "TX burst beside a captured RX frame")
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_CLR_CNT)
    mon.frames.clear()                      # re-based with the counters, at a
    mon.beats = 0                           # quiescent point
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_FLUSH)
    await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_RAW)
    await axi.write(BASE + tx.TX_CTRL, 0)
    for off in (tx.TX_CTRL, tx.TX_STATUS, tx.TX_SPACE, tx.TX_DATA, tx.TX_FRAMES,
                tx.TX_REJECT):
        for _ in range(3):
            await _rd(axi, off)
    await _rmii_cycles(dut, 200)
    after = await _rx_snapshot(axi)
    assert after == before, (
        f"TX activity perturbed the RX side:\n  before {before}\n  after  {after}")
    assert await _read_frame(axi) == rx_frame, "TX activity ate or altered an RX byte"
    assert await _counters(axi) == (1, 0, 0)

    # --- RX activity of every kind, with a TX frame staged ---
    t = _staged(90, seed=103)
    await tx.stage(axi, BASE, t)
    tx_before = await _tx_snapshot(axi)
    assert tx_before[3] == (0, 0, 0, 0)
    await axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN | DUTEGR_CTRL_CLR_CNT)
    await axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN | DUTEGR_CTRL_FLUSH)
    await axi.write(BASE + DUTEGR_CTRL, 0)
    await axi.write(BASE + DUTEGR_CTRL, DUTEGR_CTRL_EN)
    for off in (DUTEGR_CTRL, DUTEGR_STATUS, DUTEGR_LEVEL, DUTEGR_FRAME_LEN,
                DUTEGR_DATA, DUTEGR_RX_FRAMES, DUTEGR_DROP_FULL, DUTEGR_DROP_GIANT):
        await _rd(axi, off)
    await _rmii_cycles(dut, 16)
    rx2 = _pattern(70, seed=6)
    await _push(dut, rx2)
    assert await _read_frame(axi) == rx2
    await _rmii_cycles(dut, 64)
    tx_after = await _tx_snapshot(axi)
    assert tx_after == tx_before, (
        f"RX activity perturbed the TX side:\n  before {tx_before}\n  after  {tx_after}")
    assert mon.beats == 0, "RX activity made staged TX bytes leave"
    await tx.commit(axi, BASE)
    await _wait_frames(dut, mon, 1)
    assert mon.frames == [tx.expected_on_port(t, raw=False)]
    await _tx_quiet(dut, axi, mon, commits=1, what="TX/RX independence")


# --------------------------------------------------------------------------- #
# (q) the §3 invariant under a seeded random sequence
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_q_invariant_holds_under_a_random_sequence(dut):
    """What this proves: TX_FRAMES + TX_REJECT + TX_FLUSHED + frames_queued ==
    COMMITs holds at every quiescent point of a long seeded random mix — RAW=0
    and RAW=1 frames of random length, invalid lengths, empty commits, ABORTs,
    COMMIT|ABORT, FLUSHes (each with at least one frame parked for it to
    discard, and TX_FLUSHED checked to rise by exactly the frames that did not
    leave), and a sink whose tready wanders — and the frames that leave are
    EXACTLY the accepted ones, in order, each byte-exact. The expected
    outcome of every COMMIT is predicted before it is issued (length rules, and
    free space read before staging — space only ever grows while staging, so the
    prediction cannot be raced)."""
    seed = 0xD0E6
    rng = random.Random(seed)
    dut._log.info(f"random sequence seed = {seed:#x}")
    axi, mon = await _bring_up_tx(dut)

    stop = False
    paused = False

    async def wander():
        while not stop:
            if paused:
                dut.inj_m_tready.value = 0
                await RisingEdge(dut.rmii_clk_i)
                continue
            dut.inj_m_tready.value = 1 if rng.random() < 0.7 else 0
            for _ in range(rng.randint(1, 40)):
                await RisingEdge(dut.rmii_clk_i)
                if paused:
                    break
        dut.inj_m_tready.value = 1

    cocotb.start_soon(wander())

    commits = 0
    n_flushes = 0
    expect = []
    for it in range(80):
        op = rng.choices(["good0", "good1", "short", "empty", "abort", "both", "flush"],
                         weights=[40, 20, 8, 4, 10, 4, 6])[0]
        if op == "flush":
            # Park one more good frame behind a stopped sink first, so every
            # FLUSH in the mix has at least one frame it MUST discard.
            n_flushes += 1
            # Room for the parked frame is secured BEFORE the sink stops (space
            # only grows while we stage), so its COMMIT is certain to be accepted.
            sp = await _rd(axi, tx.TX_SPACE)
            if (sp & tx.TX_SPACE_BYTES_MASK) < 40 or (sp >> tx.TX_SPACE_SLOTS_SHIFT) < 1:
                await tx.wait_empty(dut.s_axi_aclk, axi, BASE)
            paused = True
            await _rmii_cycles(dut, 2)
            park = _staged(40, seed=5000 + it)
            await tx.send(axi, BASE, park)
            commits += 1
            expect.append(tx.expected_on_port(park, raw=False))
            flushed0 = await _rd(axi, tx.TX_FLUSHED)
            await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_FLUSH)
            await _rmii_cycles(dut, 8)      # the request reaches the framer
            paused = False                  # (a frame half-way out must finish)
            while (await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_FLUSH_BUSY:
                await _rmii_cycles(dut, 8)
            await _rmii_cycles(dut, 8)
            _f, _r, flushed, queued = await tx.counters(axi, BASE)
            dropped = flushed - flushed0
            assert queued == 0, f"FLUSH finished with {queued} frame(s) still queued"
            assert not mon.cur, "FLUSH finished with a frame half-way out"
            assert mon.frames == expect[:len(mon.frames)], "a frame left out of order or altered"
            # EXACT: every frame committed before the FLUSH either left or was
            # flushed — and TX_FLUSHED says how many were flushed.
            assert len(mon.frames) + dropped == len(expect), (
                f"at step {it}: {len(expect)} frames committed before the FLUSH, "
                f"{len(mon.frames)} left and TX_FLUSHED rose by {dropped}")
            assert dropped >= 1, "the parked frame was not flushed"
            expect = list(mon.frames)
            await tx.assert_invariant(axi, BASE, commits, mon, f"after FLUSH at step {it}")
            continue
        raw = op == "good1"
        n = {"good0": rng.randint(tx.MIN_NORMAL, 300), "good1": rng.randint(1, 300),
             "short": rng.randint(1, tx.MIN_NORMAL - 1), "empty": 0,
             "abort": rng.randint(1, 100), "both": rng.randint(20, 100)}[op]
        frame = _staged(n, seed=1000 + it) if not raw else _pattern(n, seed=1000 + it)
        # Space only GROWS while we stage (only the framer changes it, by
        # popping), and TX_SPACE under-reports — so "enough now" means "enough".
        # If not, drain first: every COMMIT's outcome is then decided by the
        # length rules alone and the model below cannot be raced.
        sp = await _rd(axi, tx.TX_SPACE)
        if (sp & tx.TX_SPACE_BYTES_MASK) < n or (sp >> tx.TX_SPACE_SLOTS_SHIFT) < 1:
            await tx.wait_empty(dut.s_axi_aclk, axi, BASE)
            await _rmii_cycles(dut, 16)
            sp = await _rd(axi, tx.TX_SPACE)
            assert (sp & tx.TX_SPACE_BYTES_MASK) >= n and (sp >> tx.TX_SPACE_SLOTS_SHIFT) >= 1
        await tx.stage(axi, BASE, frame)
        if op == "abort":
            await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_ABORT)
            continue
        if op == "both":
            await axi.write(BASE + tx.TX_CTRL, tx.TX_CTRL_COMMIT | tx.TX_CTRL_ABORT)
            continue
        await tx.commit(axi, BASE, raw=raw)
        commits += 1
        if tx.commit_is_valid(n, raw):
            expect.append(tx.expected_on_port(frame, raw))
        if it % 10 == 9:
            await tx.wait_empty(dut.s_axi_aclk, axi, BASE)
            await _rmii_cycles(dut, 16)
            await tx.assert_invariant(axi, BASE, commits, mon, f"at step {it}")
            assert mon.frames == expect, (
                f"at step {it}: {len(mon.frames)} frames left, {len(expect)} expected; "
                f"first mismatch at index "
                f"{next((i for i, (a, b) in enumerate(zip(mon.frames, expect)) if a != b), '-')}")

    stop = True
    await _tx_quiet(dut, axi, mon, commits, "random sequence")
    assert mon.frames == expect
    frames, rej, flushed, _ = await tx.counters(axi, BASE)
    dut._log.info(f"random sequence: {commits} COMMITs -> TX_FRAMES={frames} "
                  f"TX_REJECT={rej} TX_FLUSHED={flushed} (monitor saw "
                  f"{len(mon.frames)}); {n_flushes} FLUSHes")
    assert n_flushes >= 2 and flushed >= n_flushes, (
        f"the random mix must exercise FLUSH for real: {n_flushes} FLUSHes "
        f"discarded {flushed} frames")


# --------------------------------------------------------------------------- #
# (r) the length limits, at both edges of both modes — amendment A1
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_r_length_limits_at_both_edges(dut):
    """What this proves: handover amendment A1 (decided by the project lead 2026-09-23).
    RAW = 0 accepts 1514 staged bytes — 1518 on the wire, 802.3's maximum,
    FCS and envelope both good — and REJECTS 1515; RAW = 1 accepts MAX_FRAME =
    1536 verbatim (an oversize frame, deliberately, for fault injection) and
    REJECTS 1537. Each reject is staged into an EMPTY FIFO with DATA_FULL
    clear, so it can only have been refused for its length — and it is rolled
    back whole: not one of its bytes leaves."""
    axi, mon = await _bring_up_tx(dut)
    assert tx.MAX_NORMAL == 1514 and tx.MAX_RAW == 1536
    commits = 0
    cases = [(tx.MAX_NORMAL, False, True), (tx.MAX_NORMAL + 1, False, False),
             (tx.MAX_RAW, True, True), (tx.MAX_RAW + 1, True, False)]
    for k, (n, raw, ok) in enumerate(cases):
        await tx.wait_empty(dut.s_axi_aclk, axi, BASE)
        await _rmii_cycles(dut, 16)
        frame = _staged(n, seed=200 + k) if not raw else _pattern(n, seed=200 + k)
        await tx.stage(axi, BASE, frame)
        st = await _rd(axi, tx.TX_STATUS)
        assert not (st & tx.TX_STATUS_DATA_FULL), f"{n} B filled the FIFO — test is not isolating length"
        rej0 = await _rd(axi, tx.TX_REJECT)
        beats0 = mon.beats
        sent0 = len(mon.frames)
        await tx.commit(axi, BASE, raw=raw)
        commits += 1
        what = f"RAW={int(raw)}, {n} staged bytes"
        if ok:
            await _wait_frames(dut, mon, sent0 + 1)
            got = mon.frames[-1]
            assert got == tx.expected_on_port(frame, raw), f"{what}: not sent byte-exact"
            assert await _rd(axi, tx.TX_REJECT) == rej0, f"{what}: must be ACCEPTED"
            if not raw:
                assert len(got) == 1518
                fcs_ok, len_ok, why = check_frame(got)
                assert fcs_ok and len_ok, f"{what}: the wire frame is not a legal 802.3 frame: {why}"
            else:
                assert len(got) == 1536
        else:
            await _rmii_cycles(dut, 300)
            assert await _rd(axi, tx.TX_REJECT) == rej0 + 1, f"{what}: must be REJECTED"
            assert mon.beats == beats0, f"{what}: a rejected frame put bytes on inj_m_*"
            assert (await _rd(axi, tx.TX_STATUS)) & tx.TX_STATUS_REJ
    await _tx_quiet(dut, axi, mon, commits, "length limits")
    assert (await tx.counters(axi, BASE)) == (2, 2, 0, 0)
