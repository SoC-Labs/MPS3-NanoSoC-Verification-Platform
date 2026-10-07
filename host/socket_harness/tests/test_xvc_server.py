"""XVC 1.0 framing, JTAG bit order / TDO phase, and the batching contract for
:mod:`socket_harness.xvc_server` — board-free, Vivado-free, xsdb-free.

The headline proof is :func:`test_shift_reads_idcode_out_of_the_fake_tap`: a
complete JTAG IDCODE read is pushed through the real ``shift:`` path into an
in-memory IEEE-1149.1 TAP and the 32-bit value comes back out. That single test
covers three conventions that are individually invisible until board day, and
each is separately falsified by a negative control here:

  * XVC vectors are LSB-first within each byte, both directions
    (:func:`test_tdo_packing_is_lsb_first_within_bytes`,
     :func:`test_msb_first_tdo_packing_would_not_recover_the_idcode`);
  * TDO is sampled in the TCK-low phase BEFORE the rising edge
    (:func:`test_sampling_after_the_rising_edge_yields_idcode_shifted_by_one`);
  * TMS/TDI must be settled with TCK low, and only a 0->1 DRIVE[0] transition is
    an edge (:func:`test_drive_word_sequence_is_low_sample_high_per_bit`).

Prior art for why this matters: ``OPEN_ISSUES.md`` §I22 — the SWD bit order was
closed by *reading the host driver* and then pinned by a test that RE-DERIVES the
mapping, precisely so an inverted convention fails a test rather than a bring-up.

Nothing here touches a board, spawns ``xsdb``, or opens a socket beyond
``127.0.0.1``.
"""
from __future__ import annotations

import base64
import hashlib
import pathlib
import re
import socket
import zlib

import pytest

from socket_harness.endpoints import DEFAULT_HUB_URL
from socket_harness.xvc_server import (
    DRIVE_TCK,
    DRIVE_TDI,
    DRIVE_TMS,
    SWDBB_BASE,
    SWDBB_DRIVE,
    SWDBB_SAMPLE,
    XVC_ACCEPT_RATIO,
    XVC_ACCEPT_VECTOR_BITS,
    XVC_MAX_VECTOR_BITS,
    FakeJtagTap,
    FakeSwdbbBackend,
    PersistentXsdbSession,
    RegOp,
    ScriptedXsdbProcess,
    SwdbbJtagShifter,
    XsdbSwdbbBackend,
    XsdbTimeout,
    XvcBackendError,
    XvcListenEndpoint,
    XvcProtocolError,
    XvcServer,
    XvcSession,
    XvcTrace,
    bits_to_vector,
    build_batch_script,
    build_shift_script,
    main,
    parse_marked_words,
    vector_bit,
    vector_to_bits,
)
from socket_harness.xsdb import XsdbConfig

IDCODE = 0x6BA00477  # the DUT's real JTAG-DP TAP id (host/openocd/nanosoc_mps3_jtag.cfg)


# --------------------------------------------------------------------------- #
# Building a JTAG IDCODE read as an XVC shift (the shared fixture)
# --------------------------------------------------------------------------- #
def idcode_read_sequence() -> "tuple":
    """``(num_bits, tms_bits, tdi_bits, first_data_bit)`` for an IDCODE read.

    Derived from the IEEE-1149.1 controller graph, not copied from a capture, so
    it is an independent statement of what the TAP must do:

    ==============  ===  ==========================================
    rising edge      TMS  resulting state
    ==============  ===  ==========================================
    1..5              1  Test-Logic-Reset (5 ones from ANY state);
                         TLR selects the IDCODE instruction
    6                 0  Run-Test/Idle
    7                 1  Select-DR-Scan
    8                 0  Capture-DR
    9                 0  Shift-DR  (the DR was loaded with IDCODE on
                         this same edge, in Capture-DR)
    10..41            0  shift, last one with TMS=1 -> Exit1-DR
    ==============  ===  ==========================================

    TDO for shift bit ``i`` is read while TCK is low, i.e. BEFORE edge ``i+1``.
    Bit 9 is therefore the first read taken while in Shift-DR, and bits 9..40
    carry IDCODE bits 0..31.
    """
    tms = [1, 1, 1, 1, 1, 0, 1, 0, 0]  # edges 1..9
    first_data_bit = len(tms)  # index 9
    tms += [0] * 31 + [1]  # edges 10..41; exit on the last shift
    tdi = [0] * len(tms)  # IDCODE read shifts in don't-cares
    return (len(tms), tms, tdi, first_data_bit)


def _shift_via_fake_tap(tap: FakeJtagTap):
    """Run the IDCODE sequence through the real shift path; return the pieces."""
    num_bits, tms_bits, tdi_bits, first = idcode_read_sequence()
    backend = FakeSwdbbBackend(tap)
    shifter = SwdbbJtagShifter(backend)
    tdo = shifter.shift(num_bits, bits_to_vector(tms_bits), bits_to_vector(tdi_bits))
    return backend, shifter, tdo, num_bits, first


def _recover(tdo: bytes, first: int, width: int = 32) -> int:
    """Reassemble a value from ``width`` TDO bits starting at ``first``, LSB first."""
    value = 0
    for k in range(width):
        value |= vector_bit(tdo, first + k) << k
    return value


# --------------------------------------------------------------------------- #
# Pure bit helpers
# --------------------------------------------------------------------------- #
def test_vector_bit_is_lsb_first_within_each_byte() -> None:
    # xvc_server.c:85-94 -- "vector bit (8*byte_off + j) -> word bit j".
    vec = bytes([0b0000_0001, 0b1000_0000])
    assert vector_bit(vec, 0) == 1
    assert vector_bit(vec, 1) == 0
    assert vector_bit(vec, 8) == 0
    assert vector_bit(vec, 15) == 1


def test_tdo_packing_is_lsb_first_within_bytes() -> None:
    # bits_to_vector must be the exact inverse of vector_bit, or a client
    # unpacking our TDO gets a bit-reversed answer per byte.
    bits = [1, 0, 1, 1, 0, 0, 0, 1, 0, 1]
    vec = bits_to_vector(bits)
    assert vec[0] == 0b1000_1101
    assert vector_to_bits(vec, len(bits)) == bits


def test_bits_to_vector_pads_the_final_partial_byte_with_zeros() -> None:
    assert bits_to_vector([1, 1, 1]) == bytes([0b0000_0111])
    assert len(bits_to_vector([0] * 9)) == 2


# --------------------------------------------------------------------------- #
# The fake TAP itself (a broken model would make every other proof worthless)
# --------------------------------------------------------------------------- #
def test_fake_tap_five_tms_ones_reach_test_logic_reset_from_anywhere() -> None:
    tap = FakeJtagTap(IDCODE)
    for _ in range(3):  # wander off into the DR column
        tap.tick(tms=True, tdi=False)
        tap.tick(tms=False, tdi=False)
    for _ in range(5):
        tap.tick(tms=True, tdi=False)
    assert tap.state == "TLR"
    assert tap.ir == FakeJtagTap.IR_IDCODE  # TLR must select IDCODE


def test_fake_tap_captures_idcode_on_the_edge_that_enters_shift_dr() -> None:
    tap = FakeJtagTap(IDCODE)
    for tms in (1, 1, 1, 1, 1, 0, 1, 0, 0):
        tap.tick(tms=bool(tms), tdi=False)
    assert tap.state == "SHIFT_DR"
    # The DR now holds IDCODE, and TDO presents its LSB *before* the next edge.
    assert tap.tdo == IDCODE & 1


def test_fake_tap_shifts_an_instruction_into_the_ir_and_selects_bypass() -> None:
    tap = FakeJtagTap(IDCODE)
    # TLR -> RTI -> Select-DR -> Select-IR -> Capture-IR -> Shift-IR
    for tms in (1, 1, 1, 1, 1, 0, 1, 1, 0, 0):
        tap.tick(tms=bool(tms), tdi=False)
    assert tap.state == "SHIFT_IR"
    # Shift 4 ones (BYPASS), the last with TMS=1 to exit, then Update-IR.
    for k in range(4):
        tap.tick(tms=(k == 3), tdi=True)
    tap.tick(tms=True, tdi=False)  # Exit1-IR -> Update-IR
    tap.tick(tms=False, tdi=False)  # Update-IR latches, -> RTI
    assert tap.ir == FakeJtagTap.IR_BYPASS


# --------------------------------------------------------------------------- #
# THE headline test: a real IDCODE read through the real shift path
# --------------------------------------------------------------------------- #
def test_shift_reads_idcode_out_of_the_fake_tap() -> None:
    tap = FakeJtagTap(IDCODE)
    _backend, _shifter, tdo, num_bits, first = _shift_via_fake_tap(tap)

    assert len(tdo) == (num_bits + 7) // 8
    assert _recover(tdo, first) == IDCODE
    assert tap.state == "EXIT1_DR"  # the last shift bit carried TMS=1
    assert tap.rising_edges == num_bits  # exactly one edge per shift bit


def test_a_second_idcode_read_on_the_same_session_still_lands(
) -> None:
    # Back-to-back shifts must not smear: the park write leaves TCK low, so the
    # next shift's first write creates no spurious edge.
    tap = FakeJtagTap(IDCODE)
    backend = FakeSwdbbBackend(tap)
    shifter = SwdbbJtagShifter(backend)
    num_bits, tms_bits, tdi_bits, first = idcode_read_sequence()
    for _ in range(2):
        tdo = shifter.shift(
            num_bits, bits_to_vector(tms_bits), bits_to_vector(tdi_bits)
        )
        assert _recover(tdo, first) == IDCODE
    assert tap.rising_edges == 2 * num_bits


def test_a_different_idcode_comes_back_different() -> None:
    # Guards against the model "returning IDCODE" because the test asked for it.
    for value in (0x0BB11477, 0xFFFFFFFF, 0x00000001, 0xDEADBEEF):
        tap = FakeJtagTap(value)
        _b, _s, tdo, _n, first = _shift_via_fake_tap(tap)
        assert _recover(tdo, first) == value


# --------------------------------------------------------------------------- #
# Negative controls — each convention, falsified
# --------------------------------------------------------------------------- #
def test_sampling_after_the_rising_edge_yields_idcode_shifted_by_one() -> None:
    """The TDO *phase* control.

    Re-run the same shift with the sample moved to AFTER the rising edge (the
    natural-looking mistake) and show it recovers ``IDCODE >> 1``, not IDCODE.
    If the production ordering were wrong, this test would be the one that
    passes -- which is why both are here.
    """

    class LateSampleBackend(FakeSwdbbBackend):
        def shift_drive_sample(self, low_values, park_value, *, drive_addr, sample_addr):
            ops = []
            for value in low_values:
                ops.append(RegOp.write(drive_addr, value & ~DRIVE_TCK))
                ops.append(RegOp.write(drive_addr, value | DRIVE_TCK))
                ops.append(RegOp.read(sample_addr))  # <-- WRONG: after the edge
            ops.append(RegOp.write(drive_addr, park_value & ~DRIVE_TCK))
            return [s & 1 for s in self.run_batch(ops)]

    num_bits, tms_bits, tdi_bits, first = idcode_read_sequence()
    shifter = SwdbbJtagShifter(LateSampleBackend(FakeJtagTap(IDCODE)))
    tdo = shifter.shift(num_bits, bits_to_vector(tms_bits), bits_to_vector(tdi_bits))

    late = _recover(tdo, first)
    assert late != IDCODE
    # Only the top bit is lost (the TAP left Shift-DR), so the low 31 bits are
    # IDCODE >> 1 -- an off-by-one-bit trace, the silent failure this guards.
    assert late & 0x7FFF_FFFF == (IDCODE >> 1) & 0x7FFF_FFFF


def test_msb_first_tdo_packing_would_not_recover_the_idcode() -> None:
    """The bit-order control: pack the same TDO bits MSB-first and it breaks."""
    tap = FakeJtagTap(IDCODE)
    _b, _s, tdo, num_bits, first = _shift_via_fake_tap(tap)
    bits = vector_to_bits(tdo, num_bits)

    msb_first = bytearray((num_bits + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            msb_first[i >> 3] |= 1 << (7 - (i & 7))
    assert _recover(bytes(msb_first), first) != IDCODE


def test_drive_word_sequence_is_low_sample_high_per_bit() -> None:
    """The DRIVE-word control: TMS/TDI settled with TCK low, then one edge."""
    backend = FakeSwdbbBackend(FakeJtagTap(IDCODE))
    shifter = SwdbbJtagShifter(backend)
    # 4 bits: TMS = 1,0,1,1  TDI = 0,1,1,0
    tms = bits_to_vector([1, 0, 1, 1])
    tdi = bits_to_vector([0, 1, 1, 0])
    shifter.shift(4, tms, tdi)

    expect = []
    for tms_bit, tdi_bit in ((1, 0), (0, 1), (1, 1), (1, 0)):
        word = (DRIVE_TMS if tms_bit else 0) | (DRIVE_TDI if tdi_bit else 0)
        expect.append(word)  # TCK low, data settled
        expect.append(word | DRIVE_TCK)  # rising edge
    expect.append(DRIVE_TMS)  # park: last TMS/TDI, TCK low
    assert backend.drive_writes == expect
    # Every write hit DRIVE only; SAMPLE is read-only in swd_bb.sv.
    assert backend.op_count == 3 * 4 + 1


def test_drive_values_is_pure_and_applies_the_swdbb_pin_reuse() -> None:
    shifter = SwdbbJtagShifter(FakeSwdbbBackend())
    words = shifter.drive_values(3, bits_to_vector([1, 0, 1]), bits_to_vector([0, 1, 1]))
    assert words == [DRIVE_TMS, DRIVE_TDI, DRIVE_TMS | DRIVE_TDI]
    assert all(not (w & DRIVE_TCK) for w in words)  # TCK always clear


def test_shifter_addresses_come_off_the_swdbb_base() -> None:
    shifter = SwdbbJtagShifter(FakeSwdbbBackend(base=0x44A70000))
    assert shifter.drive_addr == SWDBB_BASE + SWDBB_DRIVE == 0x44A70000
    assert shifter.sample_addr == SWDBB_BASE + SWDBB_SAMPLE == 0x44A70004


def test_a_relocated_swdbb_base_is_honoured_end_to_end() -> None:
    base = 0x5000_0000
    backend = FakeSwdbbBackend(FakeJtagTap(IDCODE), base=base)
    shifter = SwdbbJtagShifter(backend, base=base)
    num_bits, tms_bits, tdi_bits, first = idcode_read_sequence()
    tdo = shifter.shift(num_bits, bits_to_vector(tms_bits), bits_to_vector(tdi_bits))
    assert _recover(tdo, first) == IDCODE


def test_fake_backend_rejects_an_access_outside_the_two_mapped_offsets() -> None:
    # swd_bb.sv decodes ONLY 0x00 and 0x04; everything else reads 0 / ignores
    # writes. A backend that silently accepted 0x08 would hide exactly the
    # aliasing bug swd_bb's RESOLVED ambiguity #1 was about.
    backend = FakeSwdbbBackend()
    with pytest.raises(XvcBackendError):
        backend.run_batch([RegOp.write(SWDBB_BASE + 0x08, 1)])
    with pytest.raises(XvcBackendError):
        backend.run_batch([RegOp.read(SWDBB_BASE + 0x08)])


# --------------------------------------------------------------------------- #
# Batching contract — one backend call per shift, whatever the length
# --------------------------------------------------------------------------- #
def test_one_maximal_shift_costs_exactly_one_batched_backend_call() -> None:
    backend = FakeSwdbbBackend(FakeJtagTap(IDCODE))
    shifter = SwdbbJtagShifter(backend)
    n = XVC_MAX_VECTOR_BITS
    zeros = bytes((n + 7) // 8)
    shifter.shift(n, zeros, zeros)

    # THE requirement: not one call per bit. 2048 separate xsdb spawns would be
    # hours (module header); one batch is one round trip.
    assert backend.batch_calls == 1
    assert backend.op_count == 3 * n + 1 == shifter.ops_per_shift(n)


def test_xsdb_backend_issues_exactly_one_eval_per_shift() -> None:
    proc = ScriptedXsdbProcess(responder=_tcl_shift_responder(FakeJtagTap(IDCODE)))
    session = PersistentXsdbSession(
        XsdbConfig(), spawn=lambda: proc, connect=False
    ).start()
    backend = XsdbSwdbbBackend(session)
    shifter = SwdbbJtagShifter(backend)

    num_bits, tms_bits, tdi_bits, first = idcode_read_sequence()
    tdo = shifter.shift(num_bits, bits_to_vector(tms_bits), bits_to_vector(tdi_bits))

    assert session.eval_calls == 1  # ONE script crossed the boundary
    assert backend.batch_calls == 1
    assert _recover(tdo, first) == IDCODE  # ...and it was the right script


def test_ten_shifts_are_ten_evals_not_ten_thousand() -> None:
    proc = ScriptedXsdbProcess(responder=_tcl_shift_responder(FakeJtagTap(IDCODE)))
    session = PersistentXsdbSession(
        XsdbConfig(), spawn=lambda: proc, connect=False
    ).start()
    shifter = SwdbbJtagShifter(XsdbSwdbbBackend(session))
    num_bits, tms_bits, tdi_bits, _first = idcode_read_sequence()
    for _ in range(10):
        shifter.shift(num_bits, bits_to_vector(tms_bits), bits_to_vector(tdi_bits))
    assert session.eval_calls == 10
    assert len(proc.sent) == 10


# --------------------------------------------------------------------------- #
# Generated Tcl: shape, and semantic equality with the reference op expansion
# --------------------------------------------------------------------------- #
def _extract_foreach_values(script: str) -> "list":
    hit = re.search(r"foreach _v \{([^}]*)\}", script)
    assert hit is not None, f"no foreach value list in:\n{script}"
    return [int(tok) for tok in hit.group(1).split()]


def _tcl_shift_responder(tap: FakeJtagTap, base: int = SWDBB_BASE):
    """A :class:`ScriptedXsdbProcess` responder that *interprets* our shift Tcl.

    Pulls the ``foreach`` value list straight out of the emitted script and runs
    it through :class:`FakeSwdbbBackend`'s DRIVE/SAMPLE semantics — the same
    reference expansion the abstract backend uses. That is what pins the compact
    Tcl to the generic op path: if ``build_shift_script`` emitted the wrong value
    list, or the wrong per-bit order, the recovered IDCODE would be wrong.
    """
    inner = FakeSwdbbBackend(tap, base=base)
    drive = base + SWDBB_DRIVE
    sample = base + SWDBB_SAMPLE

    def respond(script: str) -> str:
        if "foreach _v" not in script:
            return ""  # e.g. the `catch {connect ...}` preamble
        values = _extract_foreach_values(script)
        bits = SwdbbBackendReference(inner).run(values, drive, sample)
        return "MPS3XVC_TDO " + " ".join(f"{b:08X}" for b in bits)

    return respond


class SwdbbBackendReference:
    """Drive the reference three-access-per-bit sequence into a fake backend."""

    def __init__(self, backend: FakeSwdbbBackend) -> None:
        self.backend = backend

    def run(self, low_values, drive_addr, sample_addr) -> "list":
        ops = []
        for value in low_values:
            ops.append(RegOp.write(drive_addr, value & ~DRIVE_TCK))
            ops.append(RegOp.read(sample_addr))
            ops.append(RegOp.write(drive_addr, value | DRIVE_TCK))
        ops.append(RegOp.write(drive_addr, low_values[-1] & ~DRIVE_TCK))
        return self.backend.run_batch(ops)


def test_shift_script_value_list_equals_the_pure_drive_words() -> None:
    shifter = SwdbbJtagShifter(FakeSwdbbBackend())
    tms = bits_to_vector([1, 0, 1, 1, 0])
    tdi = bits_to_vector([0, 1, 1, 0, 1])
    lows = shifter.drive_values(5, tms, tdi)
    script = build_shift_script(
        shifter.drive_addr, shifter.sample_addr, lows, lows[-1]
    )
    assert _extract_foreach_values(script) == lows


def test_shift_script_is_far_smaller_than_the_generic_expansion() -> None:
    # The compact form is why a maximal shift is ~27 KB of Tcl, not ~500 KB.
    lows = [0] * XVC_MAX_VECTOR_BITS
    compact = build_shift_script(0x44A70000, 0x44A70004, lows, 0)
    empty, nreads = build_batch_script([])
    assert nreads == 0 and empty  # sanity: the generic builder handles an empty batch
    ops = []
    for value in lows:
        ops.append(RegOp.write(0x44A70000, value))
        ops.append(RegOp.read(0x44A70004))
        ops.append(RegOp.write(0x44A70000, value | DRIVE_TCK))
    big, big_reads = build_batch_script(ops)
    assert big_reads == XVC_MAX_VECTOR_BITS
    assert len(compact) * 5 < len(big)


def test_generated_tcl_never_halts_the_microblaze() -> None:
    # The mps3_diag.tcl:15 trap, inherited: `stop`/`con` mid-swap corrupts the
    # transfer. Only mwr / mrd -force may appear.
    script = build_shift_script(0x44A70000, 0x44A70004, [0, 2, 4], 4)
    generic, _ = build_batch_script(
        [RegOp.write(0x44A70000, 1), RegOp.read(0x44A70004)]
    )
    for tcl in (script, generic):
        assert "stop" not in tcl
        assert re.search(r"\bcon\b", tcl) is None
        assert "-force" in tcl


def test_parse_marked_words_extracts_the_value_list() -> None:
    text = "xsdb% MPS3XVC_TDO 00000001 00000000 00000001"
    assert parse_marked_words(text, "MPS3XVC_TDO", 3) == [1, 0, 1]


def test_parse_marked_words_raises_on_the_error_marker() -> None:
    with pytest.raises(XvcBackendError) as exc:
        parse_marked_words("MPS3XVC_ERR Memory write error at 0x44A70000", "MPS3XVC_TDO", 4)
    assert "Memory write error" in str(exc.value)


def test_parse_marked_words_never_zero_pads_a_short_reply() -> None:
    # A batch that aborted part-way must be an error, not 4 bits of which 2 were
    # invented -- an all-zero TDO reads as "TAP in BYPASS", i.e. silently wrong.
    with pytest.raises(XvcBackendError):
        parse_marked_words("MPS3XVC_TDO 00000001 00000000", "MPS3XVC_TDO", 4)


def test_parse_marked_words_raises_when_the_marker_is_missing() -> None:
    with pytest.raises(XvcBackendError):
        parse_marked_words("no target found\nxsdb%", "MPS3XVC_TDO", 1)


# --------------------------------------------------------------------------- #
# PersistentXsdbSession framing
# --------------------------------------------------------------------------- #
def test_session_reads_up_to_the_end_sentinel_and_strips_prompt_noise() -> None:
    proc = ScriptedXsdbProcess(
        replies=["MPS3XVC_VALS 0000002A"],
        banner="rlwrap: xsdb\nxsdb% ",
    )
    session = PersistentXsdbSession(
        XsdbConfig(), spawn=lambda: proc, connect=False
    ).start()
    text = session.eval("mrd -value -force 0x44A70004 1")
    assert "MPS3XVC_VALS 0000002A" in text
    assert "MPS3XVC_END" not in text  # the sentinel is consumed, not returned
    assert parse_marked_words(text, "MPS3XVC_VALS", 1) == [0x2A]


def test_session_appends_its_own_end_sentinel_to_every_script() -> None:
    proc = ScriptedXsdbProcess(replies=["MPS3XVC_VALS"])
    session = PersistentXsdbSession(
        XsdbConfig(), spawn=lambda: proc, connect=False
    ).start()
    session.eval("puts hello")
    assert proc.sent[-1].endswith('puts "MPS3XVC_END"\n')


def test_session_connects_once_with_the_default_hub_url() -> None:
    proc = ScriptedXsdbProcess(replies=[""])
    cfg = XsdbConfig()
    session = PersistentXsdbSession(cfg, spawn=lambda: proc).start()
    # ONE connect, at start(), for the whole life of the process -- that is the
    # entire performance argument. The hub is env-supplied (MPS3_HW_URL) and the
    # public default is host-agnostic (endpoints.DEFAULT_HUB_URL): assert the
    # tcp:...:3121 SHAPE xsdb requires (mps3_diag.tcl:21), never a site host.
    assert cfg.hub_url == DEFAULT_HUB_URL
    assert cfg.hub_url.startswith("tcp:") and cfg.hub_url.endswith(":3121")
    assert f"connect -url {cfg.hub_url}" in proc.sent[0]
    session.start()  # idempotent: no second process, no second connect
    assert len(proc.sent) == 1


def test_session_eval_before_start_is_an_error() -> None:
    session = PersistentXsdbSession(XsdbConfig(), spawn=lambda: ScriptedXsdbProcess())
    with pytest.raises(Exception):
        session.eval("puts hi")


def test_session_times_out_rather_than_hanging_on_a_silent_process() -> None:
    class SilentProcess:
        def send(self, data: bytes) -> None:
            pass

        def recv(self, timeout: float) -> bytes:
            return b""

        def close(self) -> None:
            pass

    session = PersistentXsdbSession(
        XsdbConfig(), spawn=lambda: SilentProcess(), timeout_s=0.05, connect=False
    ).start()
    with pytest.raises(XsdbTimeout):
        session.eval("puts hi")


def test_session_close_closes_the_process() -> None:
    proc = ScriptedXsdbProcess(replies=[""])
    session = PersistentXsdbSession(XsdbConfig(), spawn=lambda: proc, connect=False)
    session.start()
    assert session.started
    session.close()
    assert proc.closed and not session.started


# --------------------------------------------------------------------------- #
# XVC 1.0 protocol framing (pure, no sockets)
# --------------------------------------------------------------------------- #
def _session(tap: "FakeJtagTap | None" = None) -> "tuple":
    backend = FakeSwdbbBackend(tap if tap is not None else FakeJtagTap(IDCODE))
    shifter = SwdbbJtagShifter(backend)
    return XvcSession(shifter), backend


def test_getinfo_framing_matches_the_firmware_exactly() -> None:
    xvc, _ = _session()
    # firmware/xvc_server/xvc_server.c:221 -- "xvcServer_v1.0:%u\n"
    assert xvc.feed(b"getinfo:") == b"xvcServer_v1.0:2048\n"
    assert xvc.max_bits == XVC_MAX_VECTOR_BITS == 2048


def test_getinfo_split_across_two_reads_is_reassembled() -> None:
    xvc, _ = _session()
    assert xvc.feed(b"getin") == b""
    assert xvc.pending == 5
    assert xvc.feed(b"fo:") == b"xvcServer_v1.0:2048\n"
    assert xvc.pending == 0


def test_two_pipelined_getinfos_in_one_read_get_two_replies() -> None:
    xvc, _ = _session()
    assert xvc.feed(b"getinfo:getinfo:") == b"xvcServer_v1.0:2048\n" * 2
    assert xvc.commands == 2


def test_settck_echoes_the_requested_period_unchanged() -> None:
    xvc, _ = _session()
    period = (1000).to_bytes(4, "little")
    assert xvc.feed(b"settck:" + period) == period
    assert xvc.tck_period_ns == 1000
    # Advisory only: TCK is a register bit, there is no divider to program.


def test_settck_waits_for_all_four_period_bytes() -> None:
    xvc, _ = _session()
    assert xvc.feed(b"settck:\x01\x02") == b""
    assert xvc.feed(b"\x03\x04") == bytes([1, 2, 3, 4])
    assert xvc.tck_period_ns == 0x04030201


def test_shift_round_trips_the_idcode_through_the_wire_protocol() -> None:
    """End-to-end through the *protocol*: an XVC ``shift:`` frame in, TDO out."""
    xvc, _backend = _session()
    num_bits, tms_bits, tdi_bits, first = idcode_read_sequence()
    vec = (num_bits + 7) // 8
    frame = (
        b"shift:"
        + num_bits.to_bytes(4, "little")
        + bits_to_vector(tms_bits)
        + bits_to_vector(tdi_bits)
    )
    tdo = xvc.feed(frame)
    assert len(tdo) == vec
    assert _recover(tdo, first) == IDCODE
    assert (xvc.shifts, xvc.bits_shifted) == (1, num_bits)


def test_shift_waits_for_both_vectors_before_touching_the_backend() -> None:
    xvc, backend = _session()
    num_bits, tms_bits, tdi_bits, _first = idcode_read_sequence()
    vec = (num_bits + 7) // 8
    body = bits_to_vector(tms_bits) + bits_to_vector(tdi_bits)
    header = b"shift:" + num_bits.to_bytes(4, "little")

    assert xvc.feed(header) == b""
    assert backend.batch_calls == 0  # nothing driven on a partial command
    assert xvc.feed(body[:-1]) == b""
    assert backend.batch_calls == 0
    tdo = xvc.feed(body[-1:])
    assert len(tdo) == vec
    assert backend.batch_calls == 1


def test_shift_of_zero_bits_is_a_protocol_violation() -> None:
    xvc, _ = _session()
    with pytest.raises(XvcProtocolError):
        xvc.feed(b"shift:" + (0).to_bytes(4, "little"))


# --------------------------------------------------------------------------- #
# The advertised size is a CHUNK HINT, not a ceiling on num_bits
#
# Regression group for the bug that made Synopsys Identify die with
#   Error: Couldn't shift do data from xvcServer
# on its first real scan. The server bounded `shift:`'s num_bits by the value it
# advertised in `getinfo:` and dropped the connection when a request exceeded it.
# Identify instead sizes the shift PAYLOAD against the advertisement and then
# adds its TAP state-navigation TMS bits, so num_bits legitimately overshoots.
# Measured against the real debugger (T-2022.09-SP2) over --fake-tap:
#     advertise 1024 -> asks for 1029      (+5 navigation bits)
#     advertise 2048 -> asks for 2053      (+5)
#     advertise 8192 -> asks for 3206      (whole scan fits, no chunking)
# --------------------------------------------------------------------------- #

#: The VERBATIM bytes Synopsys Identify T-2022.09-SP2 sent to this server during
#: `com check`, captured with `--trace-file ... --trace-raw` (zlib+base64 of the
#: 567-byte stream, sha256
#: 5a7905106e881e4c50384520926f4e7038c0fe0ad4dc6ec158b61985a5f8fd60).
#:
#: Recorded from a real client on purpose. A hand-written frame would only ever
#: pin somebody's *reading* of the XVC spec -- and the reading is exactly what
#: was wrong here. This pins what the client actually does:
#: getinfo: / shift 5 / shift 6 / settck:100 / shift 2053.
IDENTIFY_COM_CHECK_RX = (
    "eNpLTy3JzEvLtyrOyEwrsWJlYGCQZ4Cw2SDs1JKS5GyrFCAHqoSDgYGZYWQDjuHikQ//"
    "yQP8AJNjRYQ="
)


def identify_com_check_stream() -> bytes:
    return zlib.decompress(base64.b64decode(IDENTIFY_COM_CHECK_RX))


def test_the_captured_stream_is_the_one_identify_actually_sent() -> None:
    """Guard the fixture itself: a silent edit must not weaken the tests below."""
    rx = identify_com_check_stream()
    assert len(rx) == 567
    assert (
        hashlib.sha256(rx).hexdigest()
        == "5a7905106e881e4c50384520926f4e7038c0fe0ad4dc6ec158b61985a5f8fd60"
    )
    # The command that broke us: shift: with num_bits=2053 and two 257-byte
    # vectors -- i.e. well formed, just 5 bits over what we advertised.
    assert rx[43:49] == b"shift:"
    assert int.from_bytes(rx[49:53], "little") == 2053
    assert len(rx) - 53 == 2 * ((2053 + 7) // 8)


def test_the_firmware_test_pins_the_same_captured_bytes() -> None:
    """The host and firmware suites must not drift onto different recordings.

    ``firmware/test/test_xvc_identify_stream.c`` embeds the same capture as a C
    array (it cannot decompress a base64 blob). Parse it back out and compare:
    two servers claiming to be interchangeable behind one client have to be
    pinned against the SAME observed client behaviour, or "interchangeable" is
    just an assertion. Skips rather than fails if the firmware tree is absent,
    so this package stays independently installable.
    """
    src = (
        pathlib.Path(__file__).resolve().parents[3]
        / "firmware" / "test" / "test_xvc_identify_stream.c"
    )
    if not src.is_file():
        pytest.skip(f"firmware tree not present at {src}")
    text = src.read_text()
    body = re.search(
        r"s_identify_rx\[\d+\]\s*=\s*\{(.*?)\};", text, re.DOTALL
    )
    assert body, "could not find s_identify_rx[] in the firmware test"
    embedded = bytes(int(b, 16) for b in re.findall(r"0x([0-9A-Fa-f]{2})", body.group(1)))
    assert embedded == identify_com_check_stream()


def test_the_real_identify_com_check_stream_is_served_end_to_end() -> None:
    """THE regression test: replay the real client bytes, expect real replies."""
    xvc, _ = _session()
    reply = xvc.feed(identify_com_check_stream())

    assert xvc.commands == 5
    assert xvc.shifts == 3
    assert xvc.bits_shifted == 5 + 6 + 2053
    # getinfo(20) + 1-byte TDO + 1-byte TDO + settck echo(4) + 257-byte TDO
    assert len(reply) == 20 + 1 + 1 + 4 + 257
    assert reply.startswith(b"xvcServer_v1.0:2048\n")

    tdo = reply[-257:]
    assert len(tdo) == (2053 + 7) // 8  # ceil(num_bits/8), the XVC 1.0 rule
    # Not merely the right length: TMS walks RTI->...->Shift-IR, so shift bit 4
    # is the first IR bit out, and 1149.1 mandates Capture-IR loads ...01.
    # An all-zero reply -- what a dead path returns -- would fail here.
    assert vector_bit(tdo, 4) == 1
    assert any(tdo)


def test_the_same_stream_fragmented_across_recv_boundaries_is_identical() -> None:
    """Chunked at 7 bytes, so boundaries land inside the header AND the vectors."""
    rx = identify_com_check_stream()
    whole, _ = _session()
    expect = whole.feed(rx)

    xvc, _ = _session()
    got = bytearray()
    for i in range(0, len(rx), 7):
        got += xvc.feed(rx[i : i + 7])
    assert bytes(got) == expect
    assert xvc.shifts == 3
    assert xvc.pending == 0


def test_bounding_a_shift_by_the_advertised_size_is_what_broke_identify() -> None:
    """NEGATIVE CONTROL: the old rule, on the real bytes, dies where it died.

    ``accept_bits=max_bits`` restores the pre-fix behaviour exactly. Keeping it
    as a control is the point: it proves this suite can still SEE the bug, so a
    future "tidy-up" that re-couples the two numbers fails here rather than at a
    board session.
    """
    backend = FakeSwdbbBackend(FakeJtagTap(IDCODE))
    xvc = XvcSession(
        SwdbbJtagShifter(backend), accept_bits=XVC_MAX_VECTOR_BITS
    )
    with pytest.raises(XvcProtocolError, match="2053"):
        xvc.feed(identify_com_check_stream())
    # It got through getinfo + two small shifts + settck first -- which is why
    # the client's log said "debug IP state... ok." and only then failed.
    assert (xvc.commands, xvc.shifts) == (4, 2)


def test_getinfo_advertises_the_chunk_hint_not_the_accept_ceiling() -> None:
    xvc, _ = _session()
    assert xvc.max_bits == XVC_MAX_VECTOR_BITS
    assert xvc.accept_bits == XVC_ACCEPT_VECTOR_BITS == XVC_ACCEPT_RATIO * 2048
    assert xvc.accept_bits > xvc.max_bits
    # The advertisement stays the firmware's number, so the C server remains
    # drop-in interchangeable behind the same client.
    assert xvc.feed(b"getinfo:") == b"xvcServer_v1.0:2048\n"


def test_a_shift_just_over_the_advertised_size_is_accepted() -> None:
    """The +5 overshoot, minimally: advertise N, accept N+1..4N."""
    xvc, backend = _session()
    n = XVC_MAX_VECTOR_BITS + 5
    vec = bytes((n + 7) // 8)
    tdo = xvc.feed(b"shift:" + n.to_bytes(4, "little") + vec + vec)
    assert len(tdo) == (n + 7) // 8
    assert backend.batch_calls == 1


def test_a_shift_above_the_hard_accept_ceiling_still_fails_closed() -> None:
    """Leniency has a limit: past accept_bits we drop, never truncate.

    A truncated TDO is indistinguishable to the client from real captured data,
    so silently shortening a scan is worse than refusing it.
    """
    xvc, backend = _session()
    with pytest.raises(XvcProtocolError):
        xvc.feed(b"shift:" + (XVC_ACCEPT_VECTOR_BITS + 1).to_bytes(4, "little"))
    assert backend.batch_calls == 0


def test_a_maximal_length_shift_is_accepted() -> None:
    xvc, backend = _session()
    n = XVC_MAX_VECTOR_BITS
    vec = bytes(n // 8)
    tdo = xvc.feed(b"shift:" + n.to_bytes(4, "little") + vec + vec)
    assert len(tdo) == n // 8
    assert backend.batch_calls == 1


def test_garbage_is_a_protocol_violation_never_a_resync_guess() -> None:
    xvc, _ = _session()
    with pytest.raises(XvcProtocolError):
        xvc.feed(b"HELO\r\n")


def test_a_valid_prefix_of_a_command_is_not_yet_a_violation() -> None:
    for partial in (b"g", b"getinfo", b"s", b"se", b"sh", b"shift"):
        xvc, _ = _session()
        assert xvc.feed(partial) == b""  # still possibly valid -> wait


def test_shift_without_a_shifter_is_refused_rather_than_faked() -> None:
    xvc = XvcSession(None)
    assert xvc.feed(b"getinfo:") == b"xvcServer_v1.0:2048\n"
    with pytest.raises(XvcProtocolError):
        xvc.feed(b"shift:" + (8).to_bytes(4, "little") + b"\x00\x00")


def test_a_backend_failure_propagates_and_is_not_a_zero_tdo() -> None:
    class DeadBackend(FakeSwdbbBackend):
        def run_batch(self, ops):
            raise XvcBackendError("xsdb exited (stdout EOF)")

    xvc = XvcSession(SwdbbJtagShifter(DeadBackend()))
    with pytest.raises(XvcBackendError):
        xvc.feed(b"shift:" + (8).to_bytes(4, "little") + b"\x00\x00")


# --------------------------------------------------------------------------- #
# TCP front end (localhost only)
# --------------------------------------------------------------------------- #
def _server_and_client(tap: "FakeJtagTap | None" = None) -> "tuple":
    backend = FakeSwdbbBackend(tap if tap is not None else FakeJtagTap(IDCODE))
    server = XvcServer(SwdbbJtagShifter(backend), host="127.0.0.1", port=0)
    server.local.open()
    client = socket.create_connection(("127.0.0.1", server.port), timeout=5.0)
    # The accept is its own pump step, on purpose: a client that connects and
    # then stays silent (exactly what a cable client does on attach) must not
    # block the server inside recv().
    stat = server.pump_once(2.0)
    assert stat.accepted and not stat.rx
    return server, client, backend


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def test_server_answers_getinfo_over_a_real_localhost_socket() -> None:
    server, client, _ = _server_and_client()
    try:
        client.sendall(b"getinfo:")
        stat = server.pump_once(2.0)
        assert stat.commands == 1 and stat.tx > 0
        assert _recv_exactly(client, 20) == b"xvcServer_v1.0:2048\n"
    finally:
        client.close()
        server.close()


def test_server_shifts_the_idcode_over_a_real_localhost_socket() -> None:
    tap = FakeJtagTap(IDCODE)
    server, client, _ = _server_and_client(tap)
    try:
        num_bits, tms_bits, tdi_bits, first = idcode_read_sequence()
        vec = (num_bits + 7) // 8
        client.sendall(
            b"shift:"
            + num_bits.to_bytes(4, "little")
            + bits_to_vector(tms_bits)
            + bits_to_vector(tdi_bits)
        )
        stat = server.pump_once(2.0)
        assert stat.shifts == 1 and stat.bits == num_bits
        tdo = _recv_exactly(client, vec)
        assert _recover(tdo, first) == IDCODE
    finally:
        client.close()
        server.close()


def test_server_drops_a_client_that_speaks_garbage_but_keeps_listening() -> None:
    server, client, _ = _server_and_client()
    try:
        client.sendall(b"not-xvc-at-all")
        stat = server.pump_once(2.0)
        assert stat.dropped and "not an XVC" in stat.error
        assert not server.local.has_client
        # The LISTENER survives: Identify is configured against this port.
        port = server.port
        second = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            assert server.pump_once(2.0).accepted
            second.sendall(b"getinfo:")
            server.pump_once(2.0)
            assert _recv_exactly(second, 20) == b"xvcServer_v1.0:2048\n"
        finally:
            second.close()
    finally:
        client.close()
        server.close()


def test_server_survives_a_client_hangup_and_forgets_a_partial_command() -> None:
    server, client, _ = _server_and_client()
    try:
        client.sendall(b"getin")  # deliberately half a command
        server.pump_once(2.0)
        assert server.session.pending == 5
        client.close()
        stat = server.pump_once(2.0)
        assert stat.client_closed
        assert server.session.pending == 0  # fresh session for the next client
    finally:
        server.close()


def test_server_returns_an_empty_stat_when_nothing_arrives() -> None:
    server, client, _ = _server_and_client()
    try:
        stat = server.pump_once(0.05)
        assert (stat.rx, stat.tx, stat.commands) == (0, 0, 0)
    finally:
        client.close()
        server.close()


def test_listen_endpoint_drop_client_leaves_the_listener_bound() -> None:
    endpoint = XvcListenEndpoint("127.0.0.1", 0)
    endpoint.open()
    try:
        client = socket.create_connection(("127.0.0.1", endpoint.port), timeout=5.0)
        client.sendall(b"x")
        assert endpoint.read(1) == b"x"
        assert endpoint.has_client
        endpoint.drop_client()
        assert not endpoint.has_client
        client.close()
        # Still bound -> a new client can attach on the same port.
        second = socket.create_connection(("127.0.0.1", endpoint.port), timeout=5.0)
        second.close()
    finally:
        endpoint.close()


# --------------------------------------------------------------------------- #
# Per-command trace
#
# These exist because the ABSENCE of a trace is what turned a five-bit protocol
# bug into a long hunt: the diagnostics were `print(file=sys.stderr)`, the server
# ran with stderr on a pipe, and block buffering meant not one shift record ever
# reached the log before the client tore the session down.
# --------------------------------------------------------------------------- #
def test_a_trace_record_is_on_disk_before_the_next_command(tmp_path) -> None:
    """THE property. Readable by another process mid-session, not just at exit."""
    path = tmp_path / "xvc.trace"
    trace = XvcTrace(str(path))
    xvc = XvcSession(SwdbbJtagShifter(FakeSwdbbBackend(FakeJtagTap(IDCODE))),
                     trace=trace)
    xvc.feed(b"getinfo:")
    # No close(), no flush() by the caller, no process exit.
    assert "getinfo" in path.read_text()


def test_a_shift_record_carries_the_bits_and_all_three_vector_lengths(tmp_path) -> None:
    path = tmp_path / "xvc.trace"
    trace = XvcTrace(str(path))
    xvc = XvcSession(SwdbbJtagShifter(FakeSwdbbBackend(FakeJtagTap(IDCODE))),
                     trace=trace)
    num_bits, tms_bits, tdi_bits, _first = idcode_read_sequence()
    xvc.feed(
        b"shift:" + num_bits.to_bytes(4, "little")
        + bits_to_vector(tms_bits) + bits_to_vector(tdi_bits)
    )
    line = [ln for ln in path.read_text().splitlines() if " shift " in ln][0]
    vec = (num_bits + 7) // 8
    # Everything needed to tell a length bug from a JTAG-navigation bug, in one
    # line, without a second run of the client.
    assert f"bits={num_bits}" in line
    assert f"tms_len={vec}" in line
    assert f"tdi_len={vec}" in line
    assert f"tdo_len={vec}" in line


def test_the_trace_records_an_over_ceiling_rejection_with_both_limits(tmp_path) -> None:
    path = tmp_path / "xvc.trace"
    trace = XvcTrace(str(path))
    xvc = XvcSession(SwdbbJtagShifter(FakeSwdbbBackend(FakeJtagTap(IDCODE))),
                     accept_bits=XVC_MAX_VECTOR_BITS, trace=trace)
    with pytest.raises(XvcProtocolError):
        xvc.feed(identify_com_check_stream())
    text = path.read_text()
    # A rejection must name the request AND both limits, or the log cannot
    # distinguish "client asked for too much" from "we advertised too little".
    assert "shift-reject" in text
    assert "bits=2053" in text
    assert "advertised=2048" in text
    assert f"accept_bits={XVC_MAX_VECTOR_BITS}" in text


def test_raw_capture_reproduces_the_client_stream_byte_for_byte(tmp_path) -> None:
    """What makes a future client's behaviour turnable into a fixture."""
    path = tmp_path / "xvc.trace"
    trace = XvcTrace(str(path), raw=True)
    rx = identify_com_check_stream()
    server = XvcServer(
        SwdbbJtagShifter(FakeSwdbbBackend(FakeJtagTap(IDCODE))),
        host="127.0.0.1", port=0, trace=trace,
    )
    server.local.open()
    client = socket.create_connection(("127.0.0.1", server.port), timeout=5.0)
    try:
        assert server.pump_once(2.0).accepted
        client.sendall(rx)
        while server.session.shifts < 3:
            assert not server.pump_once(2.0).dropped
        assert (tmp_path / "xvc.trace.rx").read_bytes() == rx
        assert len((tmp_path / "xvc.trace.tx").read_bytes()) == 20 + 1 + 1 + 4 + 257
    finally:
        client.close()
        server.close()
        trace.close()


def test_edges_elides_the_middle_so_a_record_stays_one_line() -> None:
    assert XvcTrace.edges(b"") == "-"
    assert XvcTrace.edges(b"\x01\x02") == "0102"
    long = bytes(range(32))
    shown = XvcTrace.edges(long)
    assert ".." in shown and shown.startswith("0001") and shown.endswith("1e1f")
    assert len(shown) < 2 * len(long)


def test_a_server_without_a_trace_touches_no_filesystem(tmp_path) -> None:
    """Tracing is opt-in: the default path must stay allocation- and IO-free."""
    xvc, _ = _session()
    assert xvc.trace is None
    xvc.feed(b"getinfo:")
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_dry_run_prints_the_plan_without_spawning_anything(capsys) -> None:
    rc = main(["--dry-run", "--port", "2542"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0x44A70000" in out and "0x44A70004" in out
    assert "6145" in out  # 3 * 2048 + 1 register accesses
    assert "ESTIMATE ONLY" in out
    assert "foreach _v" in out  # the emitted Tcl is shown


def test_dry_run_honours_an_overridden_swdbb_base(capsys) -> None:
    main(["--dry-run", "--swdbb-base", "0x50000000"])
    out = capsys.readouterr().out
    assert "0x50000000" in out and "0x50000004" in out
