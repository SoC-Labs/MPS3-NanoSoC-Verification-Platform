"""The diag mailbox layout is ONE list, and every reader is derived from it.

WHAT WENT WRONG BEFORE
----------------------
The layout of ``mps3_diag_t`` was asserted by hand in four places:

  * the struct in ``firmware/common/diag.h`` — 25 counters;
  * the 6900 ``diag`` verb (``net_proto.h``'s ``diag_*`` response members,
    ``net_proto.c``'s snprintf, ``coordinator.c``'s copy) — 14 of them;
  * ``host/socket_harness/xsdb.py``'s ``DIAG_FIELDS`` — 20 of them;
  * ``scripts/mps3_diag.tcl``'s ``FIELDS`` — the same 20.

Nothing in the tree could see the disagreement. A field that exists in the
mailbox and is transported by nobody reads back its init value forever, which
is bug #6 ("a diagnostic that lied") wearing a different hat — the same defect
that cost an evening when ``tx_last_status`` was published and read back zero.

WHAT THIS FILE PINS
-------------------
``MPS3_DIAG_FIELDS(X)`` in ``diag.h`` is the single source; ``tools/gen_diag.py``
renders the struct and both JTAG readers from it. These tests fail on DRIFT, and
every count they compare is PARSED, never written down here — a test that names
"25" in an assertion is a fifth hand-written copy of the very thing under test.

The controls that make the round-trip meaningful:

  * REGENERATE → ZERO DIFF against the tracked files (freshness), and
  * a MUTATION control: a phantom field injected into the parsed list must
    change every generated view and turn the freshness check red. Without it,
    a generator that emitted a constant string would pass the round-trip.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GEN_PATH = REPO / "tools" / "gen_diag.py"
DIAG_H = REPO / "firmware" / "common" / "diag.h"
XSDB_PY = REPO / "host" / "socket_harness" / "xsdb.py"
DIAG_TCL = REPO / "scripts" / "mps3_diag.tcl"


def _load_gen():
    """Load ``tools/gen_diag.py`` BY PATH — ``tools/`` is a directory of
    scripts with no package structure (the same boundary spelling
    ``conftest.load_gen_manifest`` uses for ``fpga/dfx/gen_manifest.py``)."""
    if not GEN_PATH.is_file():
        pytest.fail(f"{GEN_PATH} does not exist — the diag generator is the "
                    "single source these tests exist to pin")
    spec = importlib.util.spec_from_file_location("gen_diag", GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: gen_diag uses `from __future__ import annotations`
    # + @dataclass, and dataclasses resolves the string annotations through
    # sys.modules[cls.__module__] (python 3.8). A by-path load that skips this
    # blows up inside dataclasses, not in the test.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _load_gen()


@pytest.fixture(scope="module")
def fields(gen):
    return gen.parse_fields(DIAG_H.read_text())


# --------------------------------------------------------------------------- #
# Independent parsers — deliberately NOT gen_diag's own, so a generator bug
# cannot mark its own homework.
# --------------------------------------------------------------------------- #
def _struct_members(text: str) -> list[str]:
    """``mps3_diag_t``'s members, in declaration order.

    This is the SAME parse ``scripts/harness_gates/check_diag_field_parity.py``
    does (``declared_fields()``). If the struct ever stops being literal C
    declarations, that gate goes vacuous — it reports "covers every counter"
    over an empty set — so this test owns a copy of the regex and fails loudly
    instead.
    """
    src = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    m = re.search(r"typedef\s+struct\s*\{(.*?)\}\s*mps3_diag_t\s*;", src, flags=re.S)
    assert m, "mps3_diag_t typedef not found in diag.h"
    return re.findall(r"\b(?:u?int(?:8|16|32|64)_t|char|unsigned|int|long)\s+"
                      r"(\w+)\s*(?:\[[^\]]*\])?\s*;", m.group(1))


def _xsdb_diag_fields(text: str) -> list[tuple[int, str]]:
    """``DIAG_FIELDS`` out of xsdb.py, by AST — no import, no pyverify."""
    tree = ast.parse(text)
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "DIAG_FIELDS":
            return [tuple(t) for t in ast.literal_eval(node.value)]
    raise AssertionError("DIAG_FIELDS not found in xsdb.py")


def _tcl_fields(text: str) -> list[tuple[int, str]]:
    m = re.search(r"set\s+FIELDS\s*\{(.*?)\}", text, flags=re.S)
    assert m, "set FIELDS { ... } not found in mps3_diag.tcl"
    toks = m.group(1).split()
    assert len(toks) % 2 == 0, "FIELDS is not a flat {index name ...} list"
    return [(int(toks[i]), toks[i + 1]) for i in range(0, len(toks), 2)]


def _documented_offsets(text: str) -> dict[str, int]:
    """The ``+0xNN 0xADDR field`` table in diag.h's prose header.

    That table is what a human reads before typing ``mrd``; if it disagrees
    with the list the tooling is generated from, one of them is lying.
    """
    out: dict[str, int] = {}
    for off, name in re.findall(r"\+0x([0-9A-Fa-f]{2})\s+0x[0-9A-Fa-f]+\s+(\w+)", text):
        out.setdefault(name, int(off, 16))
    return out


# --------------------------------------------------------------------------- #
# 1. The list, the struct and the readers agree — by parsing, not by assertion
# --------------------------------------------------------------------------- #
def test_xmacro_rows_are_exactly_the_struct_members(fields):
    assert [f.name for f in fields] == _struct_members(DIAG_H.read_text())


def test_class_census_is_meta_plus_counters_plus_one_pad(gen, fields):
    counters = [f for f in fields if f.cls == gen.COUNT]
    meta = [f for f in fields if f.cls == gen.META]
    pad = [f for f in fields if f.cls == gen.PAD]
    # No literal counts: the invariant is that every member carries exactly one
    # class and the three partitions cover the struct.
    assert len(counters) + len(meta) + len(pad) == len(fields)
    assert [f.name for f in meta] == ["magic", "version"]
    assert [f.name for f in pad] == ["reserved"]
    assert counters, "no COUNT rows — the gate that reads this would go vacuous"


def test_diag_fields_transports_every_non_pad_row(gen, fields):
    expect = [(f.word_offset, f.name) for f in fields if f.cls != gen.PAD]
    assert _xsdb_diag_fields(XSDB_PY.read_text()) == expect


def test_tcl_and_python_word_offsets_are_equal_elementwise(gen, fields):
    py = _xsdb_diag_fields(XSDB_PY.read_text())
    tcl = _tcl_fields(DIAG_TCL.read_text())
    assert tcl == py
    assert [o for o, _ in tcl] == [f.word_offset for f in fields if f.cls != gen.PAD]


# --------------------------------------------------------------------------- #
# 2. Offsets: the layout the JTAG readers hard-code must not move
# --------------------------------------------------------------------------- #
def test_word_offsets_match_the_offset_table_documented_in_diag_h(gen, fields):
    doc = _documented_offsets(DIAG_H.read_text())
    checked = 0
    for f in fields:
        if f.name in doc:
            assert doc[f.name] == 4 * f.word_offset, (
                f"{f.name}: diag.h's comment table says +0x{doc[f.name]:02X}, the "
                f"field list puts it at word {f.word_offset} (+0x{4*f.word_offset:02X})")
            checked += 1
    assert checked >= len(fields) - 1, "the diag.h offset table stopped covering the struct"


def test_the_mailbox_is_a_whole_sixty_four_words_with_the_pad_last(gen, fields):
    """The mailbox is a POWER-OF-TWO block, and the pad is what makes it one.

    Since diag v8 that block is 64 words (256 B). The size is not cosmetic: it
    is the ``0x100`` reservation in ``firmware/platform/lscript.ld.in``, the
    top-anchoring arithmetic that puts the base at ``LMB_end - sizeof``, and
    the read length both JTAG readers derive from the anchor they hit. A
    ``_Static_assert`` in diag.h ties the C struct to 256 bytes; this ties the
    LIST the struct is rendered from to the same number, so the two cannot
    drift in opposite directions between a regeneration and a build."""
    total = sum(f.words for f in fields)
    assert total == 64, f"mailbox is {total} words — the JTAG readers read 64"
    pad = fields[-1]
    assert pad.cls == gen.PAD and pad.word_offset == 46 and pad.words == 18, (
        "reserved[] must still pad the struct out to its last word (0xFC)")


#: THE SHIPPED LAYOUT, word by word. This is the ONE table in this file that is
#: not derived from ``diag.h`` — deliberately, and it is the only check here
#: that can catch the failure that actually costs an evening.
#:
#: Every other test above compares two views RENDERED FROM THE SAME LIST, so a
#: row reordered or inserted in that list moves the struct, xsdb.py and the Tcl
#: together and all of them stay consistent. The thing that does NOT move is the
#: JTAG reader that has ``mrd <base+0x58>`` typed into it, or a wedged board's
#: mailbox dumped last month. Offsets are wire contract: rows are APPEND-ONLY.
#:
#: Append a row here when you append one to MPS3_DIAG_FIELDS. Never renumber.
FROZEN_WORD_OFFSETS: tuple[tuple[int, str], ...] = (
    (0, "magic"), (1, "version"),
    (2, "rx_recover_events"), (3, "rx_recover_dumps"), (4, "rx_drop_frames"),
    (5, "icap_bytes"), (6, "rx_payload_got"), (7, "rx_payload_expect"),
    (8, "tcp_rcv_wnd"), (9, "tcp_rcv_ann_wnd"), (10, "rx_queued"),
    (11, "pbuf_free"), (12, "win_windows_drained"), (13, "win_grant_send_fails"),
    (14, "tcp_sndbuf"), (15, "tcp_snd_wnd"),
    (16, "tx_frames_sent"), (17, "tx_status_drained"), (18, "tx_fifo_full_drops"),
    (19, "tx_errors"), (20, "tx_space_stalls"), (21, "tx_iface_errors"),
    (22, "tx_last_status"), (23, "icap_sr_last"), (24, "icap_eos_status"),
    (25, "ovlstore_phase"), (26, "ovlstore_detail"),
    # v7 / net-proto v0.9.1 — the STMPE811 panel-continuity probe.
    (27, "touch_probe_regs"), (28, "touch_probe_adc_x"),
    (29, "touch_probe_adc_y"), (30, "touch_probe_verdict"),
    # v8 / net-proto v0.9.2 — the superloop service telemetry. These are the
    # thirteen words the struct GREW 128 -> 256 B to hold (reserved[] was down
    # to one word), which moved the top-anchored base 0x3FF80 -> 0x3FF00. Note
    # what did NOT move: every offset above. That is the whole discipline -- a
    # base move is survivable because both readers scan both anchors; an OFFSET
    # move would silently hand every reader the wrong counter.
    (31, "svc_count"), (32, "svc_pass_max_us"), (33, "svc_worst_us"),
    (34, "svc_worst_ix"), (35, "svc_overrun_events"), (36, "svc_skip_events"),
    (37, "svc_skipped_mask"),
    (38, "svc_max_us_0"), (39, "svc_max_us_1"), (40, "svc_max_us_2"),
    (41, "svc_max_us_3"), (42, "svc_max_us_4"), (43, "svc_max_us_5"),
    (44, "svc_max_us_6"), (45, "usd_boot"),   # v9 (D13): appended, nothing moved
)


def test_every_shipped_field_keeps_its_word_offset(fields):
    """No field that has ever shipped may MOVE. The mailbox is read by address
    (``mrd base+off``), so an offset is as much wire contract as a JSON key —
    and unlike a key, nothing fails loudly when it changes: the reader returns
    the wrong counter's value, in range, forever."""
    at = {f.name: f.word_offset for f in fields}
    for off, name in FROZEN_WORD_OFFSETS:
        assert name in at, (
            f"{name!r} was at word {off} in a shipped mailbox and is now gone "
            "from MPS3_DIAG_FIELDS — rows are append-only; a JTAG reader and "
            "every already-captured dump still expect it there")
        assert at[name] == off, (
            f"{name!r} shipped at word {off} (+0x{4*off:02X}) and the field "
            f"list now puts it at word {at[name]} (+0x{4*at[name]:02X}). "
            "Reordering the mailbox re-renders every view consistently and "
            "silently breaks every reader that hard-codes an address")
    # ... and the pad is still what fills the rest out to the 64-word block the
    # readers (and lscript.ld.in's 0x100 reservation) assume.
    assert sum(f.words for f in fields) == 64


def test_frozen_wire_keys_survive(gen, fields):
    # These two spellings are wire compatibility, not description: the JSON key
    # "grants_sent" carries win_windows_drained since the window-as-grant
    # repurpose, and "grant_fails" carries win_grant_send_fails.
    key = {f.name: f.key for f in fields}
    assert key["win_windows_drained"] == "grants_sent"
    assert key["win_grant_send_fails"] == "grant_fails"
    counters = [f for f in fields if f.cls == gen.COUNT]
    assert len({f.key for f in counters}) == len(counters), "duplicate wire key"


# --------------------------------------------------------------------------- #
# 3. Round trip + the mutation control that makes it mean something
# --------------------------------------------------------------------------- #
def test_regenerating_reproduces_the_tracked_files_byte_for_byte(gen):
    outputs = gen.build(REPO)
    assert outputs, "the generator claims to own no files"
    for rel, want in sorted(outputs.items()):
        got = (REPO / rel).read_text()
        assert got == want, f"{rel} is stale — re-run tools/gen_diag.py"


def test_check_mode_is_green_on_the_tracked_tree():
    r = subprocess.run([sys.executable, str(GEN_PATH), "--check"],
                       cwd=str(REPO), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_a_phantom_field_changes_every_view_and_turns_the_check_red(gen, fields):
    """MUTATION CONTROL. Without this, a generator that emitted a frozen string
    would pass the round-trip test above and prove nothing."""
    tracked = gen.build(REPO)
    phantom = gen.Field(name="phantom_counter", cls=gen.COUNT, key="phantom",
                        comment="", pre_comments=(), words=1, word_offset=0)
    mutated_fields = gen.renumber(list(fields)[:-1] + [phantom, fields[-1]])
    mutated = gen.render_outputs(REPO, mutated_fields)

    assert set(mutated) == set(tracked)
    for rel in sorted(tracked):
        assert mutated[rel] != tracked[rel], (
            f"{rel} did not change when a field was added — it is not derived "
            "from the field list")
        assert "phantom_counter" in mutated[rel] or "phantom" in mutated[rel]
        # ... and the freshness comparison (the gate's own test) goes red.
        assert mutated[rel] != (REPO / rel).read_text()


# --------------------------------------------------------------------------- #
# 4. The BASE MOVE (v8): both anchors are scanned, and the older one still reads
# --------------------------------------------------------------------------- #
XSDB_MODULE = REPO / "host" / "socket_harness" / "xsdb.py"


def _xsdb_candidates(text: str) -> tuple:
    """``XsdbConfig.candidates``' default, by AST — no import, no pyverify."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "XsdbConfig":
            for stmt in node.body:
                if (isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == "candidates"):
                    return tuple(ast.literal_eval(stmt.value))
    raise AssertionError("XsdbConfig.candidates not found in xsdb.py")


def _tcl_candidates(text: str) -> tuple:
    m = re.search(r"set\s+CANDIDATES\s+\[list(.*?)\]", text, flags=re.S)
    assert m, "set CANDIDATES [list ...] not found in mps3_diag.tcl"
    return tuple(int(t, 16) for t in re.findall(r"0x[0-9A-Fa-f]+", m.group(1)))


def test_both_jtag_readers_scan_the_same_candidate_bases_ascending():
    """The mailbox is anchored at ``LMB_end - sizeof``, so growing the struct
    MOVES it. v8 (256 B) sits at ``...F00``; the v5..v7 (128 B) mailbox a board
    in the field is still running sits at ``...F80``. Both readers must scan
    both, ASCENDING — the LMB decode aliases, and on a v8 image ``0x0003FF80``
    (a v7 image's magic address) is an ordinary counter word."""
    py = _xsdb_candidates(XSDB_MODULE.read_text())
    tcl = _tcl_candidates(DIAG_TCL.read_text())
    assert py == tcl, "the Python and Tcl readers scan different bases"
    assert list(py) == sorted(py), "candidates must be ASCENDING"
    for lmb_end in (0x00040000, 0x00080000, 0x00100000):
        assert lmb_end - 0x100 in py, f"v8 anchor for LMB end {lmb_end:#x} missing"
        assert lmb_end - 0x080 in py, f"v5-v7 anchor for LMB end {lmb_end:#x} missing"


def test_the_struct_size_is_what_the_linker_reserves_and_the_base_follows():
    """diag.h, the linker script and the readers must agree on ONE number.

    ``sizeof(mps3_diag_t)`` (asserted in C), lscript.ld.in's reservation, and
    the low byte of every v8 candidate base are the same 0x100. This is the
    check that would have caught a struct grown without the reservation
    following it — which does not fail at build time, it fails by putting the
    mailbox on top of the stack."""
    ld = (REPO / "firmware" / "platform" / "lscript.ld.in").read_text()
    assert "LENGTH = @LMB_LENGTH@ - 0x100" in ld
    assert ".mps3_diag (0x50 + @LMB_LENGTH@ - 0x100)" in ld

    words = sum(f.words for f in _load_gen().parse_fields(DIAG_H.read_text()))
    assert 4 * words == 0x100, "the field list and the linker reservation disagree"

    h = DIAG_H.read_text()
    assert "sizeof(mps3_diag_t) == 256u" in h
