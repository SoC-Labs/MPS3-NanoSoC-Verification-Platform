"""Regression tests for the `.ll` -> readback-stream mapping.

Board-free and Vivado-free: every fixture is a handful of synthetic ASCII lines
built in a tmpdir. The point is to freeze the *arithmetic* that took a full
investigation to pin down, so that a later "simplification" cannot silently
un-solve it.

The three things most at risk, and the tests that guard them:

  * `PAD_WORDS_SLR0 == 133`               -> test_pad_constant, test_verify_*
  * column rule `31 - offset % 32`        -> test_column_rule_is_reversed,
                                            test_refuted_identity_column_differs
  * block-RAM byte packing LSB-first      -> test_bram_byte_order_spells_text

Run:
    python3 -m pytest host/readback/tests -q
    python3 host/readback/tests/test_ll_mapping.py     # no pytest needed
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import decode_readback as dr  # noqa: E402


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

def write_stream(path, bits, n_words, header=True, fill="0"):
    """Write an ASCII readback whose flat stream bit `i` is `bits.get(i, fill)`.

    Flat stream bit `i` == character `i % 32` of word `i // 32`, left-most
    character first -- the same indexing `ll_offset_to_stream_bit` returns.
    """
    rows = []
    for w in range(n_words):
        chars = []
        for c in range(dr.WORD_BITS):
            v = bits.get(w * dr.WORD_BITS + c, fill)
            chars.append(str(v))
        rows.append("".join(chars))
    body = "\n".join(rows) + "\n"
    head = ""
    if header:
        head = ("Xilinx ASCII Bitstream\nPart:\txcku115-flvb1760-1-c\n"
                "Type:\tmask\nBits:\t%d\n" % (n_words * dr.WORD_BITS))
    with open(path, "w") as fh:
        fh.write(head + body)
    return path


def write_ll(path, lines):
    """`lines` = iterable of `(offset, frame, frame_off, slr, info)`."""
    with open(path, "w") as fh:
        fh.write("Revision 4\n; synthetic\n")
        for off, fr, fo, slr, info in lines:
            fh.write("Bit %10d 0x%08x %5d SLR%d %d %s\n"
                     % (off, fr, fo, slr, slr, info))
    return path


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------

def test_pad_constant():
    # 133 = one 123-word dummy frame + 10 pipeline words. Measured on XCKU115:
    # block-RAM mask coverage is exactly 1.0 here and < 1.0 at every other pad.
    assert dr.PAD_WORDS_SLR0 == 133
    assert dr.FRAME_WORDS == 123
    assert dr.FRAME_BITS == 3936
    assert dr.PAD_WORDS_BY_SLR[1] is None, "SLR1's base is NOT established"


def test_offset_zero_lands_on_last_column_of_the_pad_word():
    w, c = dr.ll_offset_to_word_col(0)
    assert (w, c) == (133, 31)


def test_column_rule_is_reversed():
    # offset % 32 is an LSB-first bit number; the line is printed MSB-first.
    assert dr.ll_offset_to_word_col(0) == (133, 31)
    assert dr.ll_offset_to_word_col(1) == (133, 30)
    assert dr.ll_offset_to_word_col(31) == (133, 0)
    assert dr.ll_offset_to_word_col(32) == (134, 31)
    assert dr.ll_offset_to_word_col(63) == (134, 0)


def test_frame_boundary_advances_123_words():
    w0, c0 = dr.ll_offset_to_word_col(0)
    w1, c1 = dr.ll_offset_to_word_col(dr.FRAME_BITS)
    assert w1 - w0 == dr.FRAME_WORDS
    assert c0 == c1


def test_frame_decomposition_matches_word_arithmetic():
    for k in (0, 1, 2610, 12316, 48643):
        for fo in (0, 1, 31, 32, 1104, 3932, 3935):
            off = k * dr.FRAME_BITS + fo
            assert dr.frame_of_offset(off) == (k, fo)
            w, c = dr.ll_offset_to_word_col(off)
            assert w == dr.PAD_WORDS_SLR0 + k * dr.FRAME_WORDS + fo // 32
            assert c == 31 - (fo % 32)


def test_stream_bit_round_trip():
    for off in (0, 1, 31, 32, 33, 3935, 3936, 10274064, 191460447):
        b = dr.ll_offset_to_stream_bit(off)
        assert dr.stream_bit_to_ll_offset(b) == off


def test_pad_is_a_pure_word_shift():
    for off in (0, 5, 3936, 10274064):
        a = dr.ll_offset_to_stream_bit(off, 0)
        b = dr.ll_offset_to_stream_bit(off, 133)
        assert b - a == 133 * dr.WORD_BITS


def test_negative_offset_rejected():
    try:
        dr.ll_offset_to_stream_bit(-1)
    except ValueError:
        return
    raise AssertionError("negative offset should raise")


def test_refuted_identity_column_differs():
    """Guard: the *refuted* model used `column = offset % 32`.

    If someone 'simplifies' the reversal away, these must stop agreeing --
    otherwise the regression would be invisible.
    """
    for off in (0, 1, 5, 33):
        _, c = dr.ll_offset_to_word_col(off)
        assert c != (off % dr.WORD_BITS) or (off % dr.WORD_BITS) == 15.5


# ---------------------------------------------------------------------------
# AsciiReadback
# ---------------------------------------------------------------------------

def test_reads_the_character_the_mapping_points_at(tmp_path):
    # set exactly the bits that .ll offsets 0, 1 and 32 map to
    want = [0, 1, 32]
    bits = {dr.ll_offset_to_stream_bit(o): 1 for o in want}
    p = write_stream(tmp_path / "s.rbd", bits, n_words=140)
    with dr.AsciiReadback(p) as rb:
        for o in want:
            assert rb.bit(o) == 1, o
        for o in (2, 3, 31, 33):
            assert rb.bit(o) == 0, o


def test_header_detection_both_flavours(tmp_path):
    a = write_stream(tmp_path / "hdr.rbd", {}, n_words=140, header=True)
    b = write_stream(tmp_path / "raw.rdbk", {}, n_words=140, header=False)
    with dr.AsciiReadback(a) as rb:
        assert rb.header_len > 0
        assert rb.n_words == 140
        assert rb.declared_bits == 140 * 32
    with dr.AsciiReadback(b) as rb:
        assert rb.header_len == 0          # readback_hw_device writes no header
        assert rb.n_words == 140


def test_bad_geometry_is_rejected(tmp_path):
    p = tmp_path / "short.rdbk"
    p.write_text("0" * 31 + "\n")          # 31 chars, not 32
    try:
        dr.AsciiReadback(p)
    except ValueError:
        return
    raise AssertionError("a non-33-byte line length must be rejected")


def test_off_the_end_returns_none(tmp_path):
    p = write_stream(tmp_path / "s.rdbk", {}, n_words=134, header=False)
    with dr.AsciiReadback(p) as rb:
        assert rb.bit(0) == 0
        assert rb.bit(10_000_000) is None


def test_word_accessor(tmp_path):
    bits = {dr.ll_offset_to_stream_bit(0): 1}
    p = write_stream(tmp_path / "s.rdbk", bits, n_words=140, header=False)
    with dr.AsciiReadback(p) as rb:
        assert rb.word(133) == "0" * 31 + "1"
        assert rb.word(9999) is None


# ---------------------------------------------------------------------------
# classification -- the distinction that unblocked the whole thing
# ---------------------------------------------------------------------------

def test_classify():
    assert dr.classify("AQ2", None, "SLICE_X48Y18") == dr.CLB_FF
    assert dr.classify("HQ", None, "SLICE_X48Y25") == dr.CLB_FF
    # `Latch=DO*` is a block-RAM OUTPUT REGISTER, not a SLICE flop. Lumping
    # these in with CLB flops is what made the flop statistics incoherent.
    assert dr.classify("DOAL0", None, "RAMB36_X8Y15") == dr.BRAM_LATCH
    assert dr.classify("DOPAU0", None, "RAMB36_X8Y15") == dr.BRAM_LATCH
    assert dr.classify(None, "B:BIT0", "RAMB36_X8Y15") == dr.BRAM
    assert dr.classify(None, "B:PARBIT7", "RAMB36_X8Y15") == dr.BRAM
    assert dr.classify(None, "A:54", "SLICE_X70Y77") == dr.LUTRAM
    assert dr.classify(None, None, "SLICE_X0Y0") == dr.OTHER


def test_only_clb_flops_invert():
    mk = lambda cls: dr.LlEntry(0, 0, 0, 0, None, None, None, None, cls)
    assert mk(dr.CLB_FF).inverts
    assert not mk(dr.BRAM).inverts
    assert not mk(dr.LUTRAM).inverts
    assert not mk(dr.BRAM_LATCH).inverts


def test_parse_ll_handles_uppercase_RAM_and_lowercase_Ram(tmp_path):
    p = write_ll(tmp_path / "x.ll", [
        (0, 0x3204, 0, 0, "Block=RAMB36_X8Y15 RAM=B:BIT0"),
        (4, 0x3204, 4, 0, "Block=SLICE_X70Y77 Ram=A:54"),
        (8, 0x3204, 8, 0, "Block=SLICE_X48Y18 Latch=AQ2 Net=top/q[0]"),
        (12, 0x3204, 12, 0, "Block=RAMB36_X8Y15 Latch=DOAL0 Net=top/d[0]"),
    ])
    got = {e.cls: e for e in dr.parse_ll(p)}
    assert set(got) == {dr.BRAM, dr.LUTRAM, dr.CLB_FF, dr.BRAM_LATCH}
    assert got[dr.CLB_FF].net == "top/q[0]"


def test_parse_ll_class_and_net_filters(tmp_path):
    p = write_ll(tmp_path / "x.ll", [
        (0, 0x1, 0, 0, "Block=SLICE_X0Y0 Latch=AQ Net=a/keep[0]"),
        (4, 0x1, 4, 0, "Block=SLICE_X0Y0 Latch=BQ Net=a/drop[0]"),
        (8, 0x1, 8, 0, "Block=SLICE_X0Y0 Ram=A:0"),
    ])
    nets = [e.net for e in dr.parse_ll(p, net_filter="keep",
                                       classes=(dr.CLB_FF,))]
    assert nets == ["a/keep[0]"]
    assert len(list(dr.parse_ll(p, classes=(dr.LUTRAM,)))) == 1
    assert len(list(dr.parse_ll(p, require_net=True))) == 2


def test_parse_ll_keeps_slr_number(tmp_path):
    p = write_ll(tmp_path / "x.ll", [
        (0, 0x1, 0, 0, "Block=SLICE_X0Y0 Latch=AQ Net=a[0]"),
        (0, 0x1, 0, 1, "Block=SLICE_X0Y0 Latch=AQ Net=b[0]"),
    ])
    assert sorted(e.slr for e in dr.parse_ll(p)) == [0, 1]


# ---------------------------------------------------------------------------
# verify_mapping / solve_pad -- the fail-closed gate
# ---------------------------------------------------------------------------

def _synthetic_pair(tmp_path, pad):
    """A `.ll` + `.msd` consistent at exactly one pad.

    Mask=1 at the block-RAM and LUTRAM offsets, mask=0 at the flop offsets --
    which is what Vivado actually emits (measured: RAM 1.0, flops 0.0).

    `lutram16` is the case that actually *discriminates* the intra-word column
    rule, and it is why this fixture is shaped the way it is. Whole-word-aligned
    RAM runs land inside the same masked words under either column rule, so they
    prove nothing. On real hardware 188 words were found in which Vivado lists
    only `Ram=` residues 0..15 while masking columns 16..31 -- i.e. a 16-bit
    distributed RAM / SRL16 occupying half a word. Only the reversed rule maps
    residues 0..15 onto columns 31..16. Reproduce exactly that here.
    """
    bram = [i for i in range(0, 512)]
    lutram = [i for i in range(1024, 1088)]
    lutram16 = [2048 + i for i in range(16)]        # residues 0..15 of word 64
    flops = [1536 + 4 * i for i in range(64)]
    lines = []
    for i, o in enumerate(bram):
        lines.append((o, 0x820400, o, 0, "Block=RAMB36_X8Y15 RAM=B:BIT%d" % i))
    for i, o in enumerate(lutram + lutram16):
        lines.append((o, 0x3204, o, 0, "Block=SLICE_X70Y77 Ram=A:%d" % i))
    for i, o in enumerate(flops):
        lines.append((o, 0x3204, o, 0,
                      "Block=SLICE_X48Y18 Latch=AQ2 Net=top/q[%d]" % i))
    ll = write_ll(tmp_path / "s.ll", lines)
    bits = {}
    for o in bram + lutram + lutram16:
        bits[dr.ll_offset_to_stream_bit(o, pad)] = 1
    msd = write_stream(tmp_path / "s.msd", bits, n_words=pad + 200)
    return ll, msd, flops


def test_verify_passes_at_the_right_pad(tmp_path):
    ll, msd, _ = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    ok, got = dr.verify_mapping(ll, msd, dr.PAD_WORDS_SLR0)
    assert ok
    assert got[dr.BRAM]["frac"] == 1.0
    assert got[dr.LUTRAM]["frac"] == 1.0
    assert got[dr.CLB_FF]["frac"] == 0.0


def test_verify_fails_one_word_off(tmp_path):
    ll, msd, _ = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    for bad in (dr.PAD_WORDS_SLR0 - 1, dr.PAD_WORDS_SLR0 + 1):
        ok, _ = dr.verify_mapping(ll, msd, bad)
        assert not ok, "pad %d must not verify" % bad


def test_verify_fails_with_the_refuted_identity_column(tmp_path, monkeypatch):
    """The negative control that matters: swap in the refuted column rule and
    the gate must reject it."""
    ll, msd, _ = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    orig = dr.ll_offset_to_stream_bit
    monkeypatch.setattr(
        dr, "ll_offset_to_stream_bit",
        lambda off, pad=dr.PAD_WORDS_SLR0: (pad + off // 32) * 32 + off % 32)
    ok, _ = dr.verify_mapping(ll, msd, dr.PAD_WORDS_SLR0)
    monkeypatch.setattr(dr, "ll_offset_to_stream_bit", orig)
    assert not ok, "identity column order was REFUTED and must fail the gate"


def test_solve_pad_finds_it_uniquely(tmp_path):
    ll, msd, _ = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    scores = dr.solve_pad(ll, msd, 0, 200)
    assert scores[0] == (1.0, dr.PAD_WORDS_SLR0)
    assert len([p for f, p in scores if f == 1.0]) == 1


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------

def test_decode_applies_clb_inversion(tmp_path):
    ll, msd, flops = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    # raw 1 at every flop cell => decoded 0 with inversion, 1 without
    bits = {dr.ll_offset_to_stream_bit(o): 1 for o in flops}
    rdbk = write_stream(tmp_path / "s.rdbk", bits,
                        n_words=dr.PAD_WORDS_SLR0 + 200, header=False)
    vals, _, st = dr.decode_nets(ll, rdbk, invert_clb=True)
    assert st["decoded"] == len(flops)
    assert dr.as_word(vals["top/q"])[0] == 0
    vals, _, _ = dr.decode_nets(ll, rdbk, invert_clb=False)
    assert dr.as_word(vals["top/q"])[0] == (1 << len(flops)) - 1


def test_decode_ref_counts_differences(tmp_path):
    ll, msd, flops = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    nw = dr.PAD_WORDS_SLR0 + 200
    same = write_stream(tmp_path / "a.rdbk",
                        {dr.ll_offset_to_stream_bit(o): 1 for o in flops},
                        n_words=nw, header=False)
    # flip exactly 3 flop cells
    flipped = {dr.ll_offset_to_stream_bit(o): 1 for o in flops[3:]}
    other = write_stream(tmp_path / "b.rdbk", flipped, n_words=nw, header=False)
    _, refs, st = dr.decode_nets(ll, other, ref_path=same)
    assert st["ref_differs"] == 3
    assert dr.as_word(refs["top/q"])[0] == 0


def test_decode_skips_other_slr(tmp_path):
    p = write_ll(tmp_path / "x.ll", [
        (0, 0x1, 0, 0, "Block=SLICE_X0Y0 Latch=AQ Net=a[0]"),
        (4, 0x1, 4, 1, "Block=SLICE_X0Y0 Latch=AQ Net=b[0]"),
    ])
    rdbk = write_stream(tmp_path / "s.rdbk", {},
                        n_words=dr.PAD_WORDS_SLR0 + 4, header=False)
    vals, _, st = dr.decode_nets(p, rdbk)
    assert set(vals) == {"a"}
    assert st["skipped_other_slr"] == 1


def test_as_word():
    assert dr.as_word({}) == (0, 0, False)
    assert dr.as_word({0: 1, 1: 0, 2: 1}) == (5, 3, True)
    v, w, contig = dr.as_word({0: 1, 3: 1})       # bit 1,2 missing
    assert (v, w, contig) == (9, 4, False)


# ---------------------------------------------------------------------------
# block RAM byte order
# ---------------------------------------------------------------------------

def test_bram_byte_order_spells_text(tmp_path):
    """`B:BIT<n>` is bit `n % 8` of byte `n // 8` (LSB-first).

    This is the convention under which a real firmware ROM came back as
    `SoCLabs NanoSoC'25 ARM-CM0+ADP+`; MSB-first, and every other intra-word
    column rule tried, returns the same bytes scrambled. Freeze it.
    """
    text = b"SoCLabs!"
    lines = []
    bits = {}
    for byte_i, ch in enumerate(text):
        for k in range(8):
            n = byte_i * 8 + k
            off = 1024 + n                      # arbitrary, contiguous
            lines.append((off, 0x820400, off, 0,
                          "Block=RAMB18_X10Y30 RAM=B:BIT%d" % n))
            if (ch >> k) & 1:
                bits[dr.ll_offset_to_stream_bit(off)] = 1
    ll = write_ll(tmp_path / "b.ll", lines)
    rdbk = write_stream(tmp_path / "b.rdbk", bits,
                        n_words=dr.PAD_WORDS_SLR0 + 200, header=False)
    assert dr.decode_bram(ll, rdbk, "RAMB18_X10Y30") == text


def test_bram_parity_is_separate(tmp_path):
    lines = [
        (1024, 0x820400, 1024, 0, "Block=RAMB18_X10Y30 RAM=B:BIT0"),
        (1025, 0x820400, 1025, 0, "Block=RAMB18_X10Y30 RAM=B:PARBIT0"),
    ]
    ll = write_ll(tmp_path / "b.ll", lines)
    bits = {dr.ll_offset_to_stream_bit(1025): 1}
    rdbk = write_stream(tmp_path / "b.rdbk", bits,
                        n_words=dr.PAD_WORDS_SLR0 + 200, header=False)
    assert dr.decode_bram(ll, rdbk, "RAMB18_X10Y30") == b"\x00"
    assert dr.decode_bram(ll, rdbk, "RAMB18_X10Y30", parity=True) == b"\x01"


def test_bram_unknown_site_is_empty(tmp_path):
    ll = write_ll(tmp_path / "b.ll", [
        (1024, 0x820400, 1024, 0, "Block=RAMB18_X10Y30 RAM=B:BIT0")])
    rdbk = write_stream(tmp_path / "b.rdbk", {},
                        n_words=dr.PAD_WORDS_SLR0 + 200, header=False)
    assert dr.decode_bram(ll, rdbk, "RAMB36_X0Y0") == b""


# ---------------------------------------------------------------------------
# CLI fail-closed behaviour
# ---------------------------------------------------------------------------

def test_cli_refuses_unestablished_slr(tmp_path):
    ll = write_ll(tmp_path / "x.ll", [
        (0, 0x1, 0, 1, "Block=SLICE_X0Y0 Latch=AQ Net=a[0]")])
    rdbk = write_stream(tmp_path / "s.rdbk", {},
                        n_words=dr.PAD_WORDS_SLR0 + 4, header=False)
    assert dr.main(["--ll", str(ll), "--rdbk", str(rdbk), "--slr", "1"]) == 2


def test_cli_verify_exit_codes(tmp_path):
    ll, msd, _ = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    assert dr.main(["--ll", str(ll), "--mask", str(msd), "--verify"]) == 0
    assert dr.main(["--ll", str(ll), "--mask", str(msd), "--verify",
                    "--pad", "132"]) == 2


def test_cli_decode_refuses_when_mask_check_fails(tmp_path):
    ll, msd, flops = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    rdbk = write_stream(tmp_path / "s.rdbk", {},
                        n_words=dr.PAD_WORDS_SLR0 + 200, header=False)
    assert dr.main(["--ll", str(ll), "--rdbk", str(rdbk), "--mask", str(msd),
                    "--pad", "134"]) == 2
    assert dr.main(["--ll", str(ll), "--rdbk", str(rdbk),
                    "--mask", str(msd)]) == 0


def test_cli_solve_pad(tmp_path):
    ll, msd, _ = _synthetic_pair(tmp_path, dr.PAD_WORDS_SLR0)
    assert dr.main(["--ll", str(ll), "--mask", str(msd),
                    "--solve-pad", "0", "200"]) == 0
    # a window that cannot contain the answer must refuse rather than guess
    assert dr.main(["--ll", str(ll), "--mask", str(msd),
                    "--solve-pad", "0", "10"]) == 2


# ---------------------------------------------------------------------------
# pytest-free runner
# ---------------------------------------------------------------------------

def _main():
    import inspect
    import tempfile

    class _MP(object):
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in fns:
        params = inspect.signature(fn).parameters
        with tempfile.TemporaryDirectory() as td:
            kw = {}
            if "tmp_path" in params:
                kw["tmp_path"] = pathlib.Path(td)
            if "monkeypatch" in params:
                kw["monkeypatch"] = _MP()
            try:
                fn(**kw)
            except Exception as exc:                    # noqa: BLE001
                failed.append((name, exc))
    for name, exc in failed:
        sys.stderr.write("FAIL %s: %r\n" % (name, exc))
    print("%d/%d passed" % (len(fns) - len(failed), len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
