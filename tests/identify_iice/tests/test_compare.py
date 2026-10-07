#!/usr/bin/env python3
"""Stream C unit tests: the compare pipeline's pure logic, with NO VCS and NO Verdi.

Contract: INTERFACES.md §4.  Plan §4 Phase 0 step 6.

The point of this file is that the comparator is only worth having if it can
FAIL.  So the tests that matter most are the negative ones: a one-bit
perturbation must be detected, a missing signal must be a harness error, and the
one-way X don't-care must not swallow a real difference.

Everything here operates on hand-built traces and hand-written VCD text, so it
runs on a box with no simulator and no licence.  The handful of tests that need
Verdi are marked with an explicit ``skipif`` and are the only ones that touch a
real FSDB.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_HERE = os.path.dirname(_TESTS)
for _p in (_HERE, os.path.join(_HERE, "ncompare")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import crop_trace as ct  # noqa: E402
import fsdb_tools as ft  # noqa: E402
import synth_hw_fsdb as sh  # noqa: E402
import trace_compare as tc  # noqa: E402

def _verdi_usable():
    """True only if the FSDB utilities are present AND actually run.

    Presence is not enough: the Verdi on the bare login PATH
    (``VERDI_2022.06-SP2``) has a broken ``.wrapper`` and every utility exits
    127.  Probing for real keeps these tests honest -- they skip instead of
    failing, and they never "pass" against a toolchain that cannot run.
    """
    if not ft.verdi_available():
        return False
    import tempfile
    d = tempfile.mkdtemp(prefix="iice_probe_")
    try:
        vcd = os.path.join(d, "p.vcd")
        ft.write_vcd(vcd, [("s/a", 1)], [{"s/a": "0"}, {"s/a": "1"}])
        ft.vcd2fsdb(vcd, os.path.join(d, "p.fsdb"), d)
        return True
    except ft.HarnessError:
        return False
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


VERDI = _verdi_usable()
needs_verdi = pytest.mark.skipif(
    not VERDI,
    reason="Verdi FSDB utilities unusable here (module load verdi/X-2025.06-SP2)",
)


# ==========================================================================
# Fixtures: a tiny IICE described entirely in-line
# ==========================================================================

DEPTH = 8
TRIGGER_TIME = "middle"
N_EDGES = 20
#: absolute sim sample at which the trigger CONDITION holds
TRIGGER_SAMPLE = 9

SIGNAL_MAP_TSV = "\n".join([
    "\t".join(["name", "width", "radix", "hw_path", "sim_path"]),
    "\t".join(["haddr", "4", "hex", "/rp_top/u_dut/HADDR", "tb.u_dut.haddr"]),
    "\t".join(["flag", "1", "bin", "/rp_top/u_dut/FLAG", "tb.u_dut.flag"]),
    "\t".join(["trigger_marker", "1", "bin", "__DERIVED__", "__DERIVED__"]),
    "\t".join(["sample_clk", "1", "bin", "/rp_top/dut_clk", "tb.u_dut.clk"]),
]) + "\n"

MANIFEST_YAML = textwrap.dedent(
    """
    iice:
      name: IICE_UNITTEST
      depth: %d
      trigger_time: %s
      controller: statemachine
      trigger_conditions: 2
      trigger_states: 2
      clock:
        hw:   /rp_top/dut_clk
        sim:  tb.u_dut.clk
        edge: positive
    signals:
      - {name: haddr, width: 4, hw: /rp_top/u_dut/HADDR, sim: tb.u_dut.haddr,
         sample: true, trigger: true, radix: hex}
      - {name: flag,  width: 1, hw: /rp_top/u_dut/FLAG,  sim: tb.u_dut.flag,
         sample: true, radix: bin}
    trigger:
      expr: "haddr == 4'h9"
    """
    % (DEPTH, TRIGGER_TIME)
)


def sim_sample_values(i):
    """The (haddr, flag) the fixture's shadow mirrors hold at sample *i*.

    Samples 0-2 are X on both signals (reset), and there are two X islands
    INSIDE the eventual window so the masking path is genuinely exercised.
    """
    if i <= 2:
        return "xxxx", "x"
    haddr = format(i % 16, "04b")
    if i == 7:
        haddr = haddr[0] + "x" + haddr[2:]   # one X bit inside the window
    flag = "1" if i % 3 == 0 else "0"
    if i == 6:
        flag = "x"                            # a fully-X sample inside the window
    return haddr, flag


def sim_marker(i):
    """trigger_marker at sample i: high one sample AFTER the condition held."""
    return "1" if i == TRIGGER_SAMPLE + 1 else ("x" if i == 0 else "0")


def make_sim_vcd_text():
    """A continuous, clocked VCD exactly as the generated shadow module dumps it.

    Sample k lands at time ``10*(k+1)``: the clock rises and the already-
    registered mirrors take their new value in the same timestep, which is why
    the sampler must read state *after* applying all changes at an edge.
    """
    L = [
        "$date\n\tunit test\n$end",
        "$version\n\thandmade\n$end",
        "$timescale\n\t1ns\n$end",
        "$scope module iice $end",
        "$var wire 4 ! haddr [3:0] $end",
        "$var wire 1 \" flag $end",
        "$var wire 1 # trigger_marker $end",
        "$var wire 1 $ sample_clk $end",
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "$dumpvars",
        "bxxxx !",
        'x"',
        "x#",
        "0$",
        "$end",
    ]
    prev = {"!": "xxxx", '"': "x", "#": "x"}
    for k in range(N_EDGES):
        t = 10 * (k + 1)
        haddr, flag = sim_sample_values(k)
        marker = sim_marker(k)
        L.append("#%d" % t)
        L.append("1$")
        for ident, val, width in (("!", haddr, 4), ('"', flag, 1), ("#", marker, 1)):
            if val != prev[ident]:
                L.append(("b%s %s" % (val, ident)) if width > 1 else ("%s%s" % (val, ident)))
                prev[ident] = val
        L.append("#%d" % (t + 5))
        L.append("0$")
    return "\n".join(L) + "\n"


@pytest.fixture
def fixture_dir(tmp_path):
    """signal_map.tsv + manifest + a hand-written continuous sim VCD."""
    d = tmp_path / "iice_fixture"
    d.mkdir()
    (d / "signal_map.tsv").write_text(SIGNAL_MAP_TSV)
    (d / "signals_unittest.yaml").write_text(MANIFEST_YAML)
    (d / "sim_iice.vcd").write_text(make_sim_vcd_text())
    (d / "logs").mkdir()
    return d


def load_fixture_sim_trace(fixture_dir):
    """The fixture's sim trace, via the pure VCD path (no tools)."""
    rows = ct.read_signal_map(str(fixture_dir / "signal_map.tsv"))
    variables, changes = ft.parse_vcd(str(fixture_dir / "sim_iice.vcd"))
    mapping = {"iice/%s" % r.name: r.name for r in rows}
    trace = ct.samples_from_clock_edges(variables, changes, mapping, ct.SAMPLE_CLK, "positive")
    return rows, trace


# ==========================================================================
# 1. VCD value normalisation -- silently corrupts every wide signal if wrong
# ==========================================================================

@pytest.mark.parametrize("raw,width,want", [
    ("0011", 4, "0011"),
    ("11", 4, "0011"),          # zero-extend a shortened value
    ("1", 4, "0001"),
    ("x", 4, "xxxx"),           # x-extend, per IEEE 1364
    ("z", 4, "zzzz"),
    ("x01", 4, "xx01"),
    ("0", 1, "0"),
    ("0000011", 4, "0011"),     # redundant leading zeros tolerated
])
def test_normalise_vcd_value(raw, width, want):
    assert ft.normalise_vcd_value(raw, width) == want


def test_normalise_vcd_value_rejects_overwide():
    with pytest.raises(ft.HarnessError):
        ft.normalise_vcd_value("1111", 2)


def test_normalise_vcd_value_rejects_junk():
    with pytest.raises(ft.HarnessError):
        ft.normalise_vcd_value("01q1", 4)


# ==========================================================================
# 2. VCD writer/reader round trip -- pure Python, no tool
# ==========================================================================

def test_write_then_parse_vcd_round_trip(tmp_path):
    signals = [("iice/haddr", 4), ("iice/flag", 1)]
    frames = [
        {"iice/haddr": "0000", "iice/flag": "0"},
        {"iice/haddr": "0011", "iice/flag": "1"},
        {"iice/haddr": "0011", "iice/flag": "1"},   # no change at all
        {"iice/haddr": "1x01", "iice/flag": "0"},
    ]
    p = str(tmp_path / "rt.vcd")
    ft.write_vcd(p, signals, frames)
    variables, changes = ft.parse_vcd(p)
    assert {v.path for v in variables} == {"iice/haddr", "iice/flag"}
    mapping = {"haddr": "haddr", "flag": "flag"}
    trace = ct.samples_from_timestamp_ordinals(
        variables, changes, mapping, len(frames), by_leaf=True)
    assert trace.values["haddr"] == ["0000", "0011", "0011", "1x01"]
    assert trace.values["flag"] == ["0", "1", "1", "0"]


# --- hardware-side time base: sample index is the timestamp ORDINAL ----------
# Measured against a real Identify capture: 1024 timestamps over #10240..#20470
# step 10, for a -depth 1024 IICE.  Absolute time is arbitrary and must never be
# used for alignment.

def test_sample_grid_uses_the_timestamps_themselves_when_the_count_matches():
    ts = list(range(10240, 10240 + 1024 * 10, 10))
    assert len(ts) == 1024
    assert ct.sample_grid(ts, 1024) == ts


def test_sample_grid_does_not_assume_time_starts_at_zero():
    assert ct.sample_grid([10240, 10250, 10260], 3) == [10240, 10250, 10260]


def test_sample_grid_fills_interior_holes_when_the_span_pins_the_grid():
    """A sample carrying no value change is legitimately absent from an FSDB.
    Here 10260 and 10270 are missing, but 10250 pins the period at 10 and the
    span then matches the depth exactly, so the grid is unambiguous."""
    assert ct.sample_grid([10240, 10250, 10280], 5) == [
        10240, 10250, 10260, 10270, 10280]


def test_sample_grid_REFUSES_an_ambiguous_every_other_sample_trace():
    """The dangerous case, and why the span check is '==' not '<='.

    True period 10 with only every other sample present infers a period of 20
    and a span of 3.  Padding the remaining 2 would compare the wrong cycles
    against each other and still report green, and there is no way to tell it
    apart from a genuine 3-sample trace.  So: refuse.
    """
    with pytest.raises(ft.HarnessError) as e:
        ct.sample_grid([100, 120, 140], 5)
    assert "Refusing to guess" in str(e.value)


def test_sample_grid_rejects_more_timestamps_than_depth():
    with pytest.raises(ft.HarnessError) as e:
        ct.sample_grid([0, 10, 20, 30], 3)
    assert "keep_last_time" in str(e.value)


def test_sample_grid_accepts_non_uniform_spacing_when_the_count_matches():
    """When there is exactly one timestamp per sample, the ORDINAL is
    authoritative and the spacing is irrelevant -- a gated sample clock can
    legitimately produce uneven gaps.  No inference happens in this case."""
    ts = [0, 10, 21, 30, 40]
    assert ct.sample_grid(ts, 5) == ts


def test_sample_grid_rejects_a_trailing_only_gap_it_cannot_pin():
    """Trailing samples with no changes cannot be distinguished from a shorter
    trace, so with NO independent end time this is an error, not a silent pad."""
    with pytest.raises(ft.HarnessError) as e:
        ct.sample_grid([100, 110, 120], 5)
    assert "Refusing to guess" in str(e.value)
    assert "No independent end time" in str(e.value)


# --- the FSDB's own end time proves the span of a QUIET TAIL ------------------
# `fsdb2vcd` stops the VCD at the last transition, so a hardware trace whose tail
# carries no value change at all -- a stalled or wedged capture, the shape this
# whole harness exists to look at -- loses its trailing timestamps and used to be
# refused as an unestablishable grid (exit 2, naming the symptom).  The missing
# information is the file's true end time, which IS in the FSDB header
# (`fsdb2vcd -summary`, min/max xtag) and is now asked for separately.
#
# TWO end-time conventions were measured on this install and they disagree by one
# sample period, so only `L <= end <= L + period` may be assumed:
#   * Identify `write fsdb`:            last sample 20470, max xtag 20480
#   * this harness write_vcd+vcd2fsdb:  last sample == max xtag (63 of depth 64)

def test_sample_grid_pins_a_quiet_tail_from_the_end_time_harness_convention():
    """depth-64 trace that stopped changing at sample 39; end time == last sample."""
    grid = ct.sample_grid(list(range(40)), 64, end_time=63, min_time=0)
    assert grid == list(range(64))


def test_sample_grid_pins_a_quiet_tail_from_the_end_time_identify_convention():
    """depth-1024 trace that stopped changing at sample 39; end time == L + period.

    The numbers are the measured Identify capture's: #10240 step 10, max xtag
    20480 (one period past the last sample 20470).
    """
    observed = list(range(10240, 10240 + 40 * 10, 10))
    grid = ct.sample_grid(observed, 1024, end_time=20480, min_time=10240)
    assert len(grid) == 1024
    assert grid[0] == 10240 and grid[1] - grid[0] == 10 and grid[-1] == 20470


def test_sample_grid_pins_a_wholly_frozen_trace_with_one_timestamp():
    """Frozen from sample 1: only the $dumpvars timestamp survives the round trip."""
    grid = ct.sample_grid([10240], 1024, end_time=20480, min_time=10240)
    assert len(grid) == 1024 and grid[0] == 10240 and grid[-1] == 20470


def test_sample_grid_STILL_REFUSES_the_every_other_sample_trap_with_an_end_time():
    """The load-bearing anti-weakening test.

    True period 10 with only every other sample present.  The gap GCD says 20;
    padding to depth 5 on that would compare the wrong cycles and report green.
    The end time REFUTES period 20 rather than confirming it: 5 samples of period
    20 would need the file to reach 180, and it ends at 140/150.  Both end-time
    conventions must refuse.
    """
    for end in (140, 150):
        with pytest.raises(ft.HarnessError) as e:
            ct.sample_grid([100, 120, 140], 5, end_time=end, min_time=100)
        assert "Refusing to guess" in str(e.value)
        assert "does NOT fit" in str(e.value)


def test_sample_grid_STILL_REFUSES_a_truncated_capture():
    """A capture that stopped being WRITTEN has a short end time, and a short end
    time fails the lower bound.  This is the case that must stay exit 2: the
    values after 120 were never recorded, so holding them forward would invent
    data."""
    with pytest.raises(ft.HarnessError) as e:
        ct.sample_grid([100, 110, 120], 5, end_time=120, min_time=100)
    assert "does NOT fit" in str(e.value)


def test_sample_grid_accepts_a_trailing_gap_once_the_end_time_proves_it():
    """Same input as test_sample_grid_rejects_a_trailing_only_gap_it_cannot_pin,
    plus the one piece of evidence that was being thrown away."""
    for end in (140, 150):
        assert ct.sample_grid([100, 110, 120], 5, end_time=end, min_time=100) == [
            100, 110, 120, 130, 140]


def test_sample_grid_will_not_anchor_on_a_timestamp_that_may_not_be_sample_zero():
    """`ts[0]` is only sample 0 if nothing precedes it in the file.  When the
    FSDB's min time is earlier, the end time must not be used to anchor a grid on
    it -- the strict rule applies instead."""
    with pytest.raises(ft.HarnessError) as e:
        ct.sample_grid([100, 110, 120], 5, end_time=140, min_time=90)
    assert "cannot be assumed to be sample 0" in str(e.value)


def test_sample_grid_reports_how_the_grid_was_established():
    """The report must be able to say "proved", never "guessed"."""
    info = {}
    ct.sample_grid(list(range(40)), 64, end_time=63, min_time=0, info=info)
    assert info["grid_source"] == "gcd_pinned_by_end_time"
    assert info["sample_period"] == 1
    info = {}
    ct.sample_grid([10240, 10250, 10260], 3, info=info)
    assert info["grid_source"] == "timestamps"
    info = {}
    ct.sample_grid([10240, 10250, 10280], 5, info=info)
    assert info["grid_source"] == "gcd_span"


def test_samples_from_ordinals_names_a_quiet_tail_in_the_meta():
    """The loader must hand the reader the finding, not just the values."""
    variables = [ft.VcdVar("!", "HADDR", 4, ("rm_led",))]
    changes = [(t, [("!", "%04d" % (t % 2))]) for t in range(0, 40)]
    trace = ct.samples_from_timestamp_ordinals(
        variables, changes, {"HADDR": "haddr"}, 64, by_leaf=True,
        end_time=63, min_time=0)
    assert trace.n_samples == 64
    assert trace.meta["last_change_sample"] == 39
    assert trace.meta["quiet_tail_samples"] == 24
    assert trace.meta["grid_source"] == "gcd_pinned_by_end_time"
    # the tail holds the sample-39 value, it is not padded with X
    assert trace.values["haddr"][39] == trace.values["haddr"][63]


def test_describe_hw_stall_names_the_stall_and_stays_quiet_otherwise():
    assert tc.describe_hw_stall({}) is None
    assert tc.describe_hw_stall({
        "source": "timestamp_ordinals", "n_samples": 64,
        "quiet_tail_samples": 0, "last_change_sample": 63}) is None
    said = tc.describe_hw_stall({
        "source": "timestamp_ordinals", "n_samples": 64,
        "quiet_tail_samples": 24, "last_change_sample": 39})
    assert said is not None
    assert "holds its last value from sample 40 of 64" in said["headline"]
    assert "stalled or wedged" in said["headline"]


def test_samples_from_ordinals_holds_values_over_missing_timestamps():
    variables = [ft.VcdVar("!", "HADDR", 4, ("rm_led",))]
    changes = [(10240, [("!", "0001")]), (10250, [("!", "0010")]),
               (10280, [("!", "0100")])]
    trace = ct.samples_from_timestamp_ordinals(
        variables, changes, {"HADDR": "haddr"}, 5, by_leaf=True)
    assert trace.values["haddr"] == ["0001", "0010", "0010", "0010", "0100"]
    assert trace.meta["first_time"] == 10240
    assert trace.meta["sample_period"] == 10


def test_samples_from_ordinals_handles_identifier_aliasing():
    """Identify declares identify_sampleclock and dut_clk against the SAME VCD
    identifier.  Collapsing that to one name would silently drop the other."""
    variables = [
        ft.VcdVar("!", "identify_sampleclock", 1, ("rm_led",)),
        ft.VcdVar("!", "dut_clk", 1, ("rm_led",)),
        ft.VcdVar("#", "blink_counter", 4, ("rm_led",)),
    ]
    changes = [(0, [("!", "1"), ("#", "0001")]), (10, [("!", "0")])]
    trace = ct.samples_from_timestamp_ordinals(
        variables, changes,
        {"dut_clk": "sample_clk", "blink_counter": "ctr"}, 2, by_leaf=True)
    assert trace.values["sample_clk"] == ["1", "0"]
    assert trace.values["ctr"] == ["0001", "0001"]


# ==========================================================================
# 3. signal_map.tsv
# ==========================================================================

def test_read_signal_map_ok(fixture_dir):
    rows = ct.read_signal_map(str(fixture_dir / "signal_map.tsv"))
    assert [r.name for r in rows] == ["haddr", "flag", "trigger_marker", "sample_clk"]
    assert rows[0].width == 4 and rows[0].radix == "hex"
    assert rows[2].hw_derived is True
    assert rows[3].hw_derived is False
    assert ct.data_signal_names(rows) == ["haddr", "flag"]


def test_read_signal_map_rejects_bad_header(tmp_path):
    p = tmp_path / "m.tsv"
    p.write_text("name\twidth\thw_path\nx\t1\t/a\n")
    with pytest.raises(ft.HarnessError) as e:
        ct.read_signal_map(str(p))
    assert "header" in str(e.value)


def test_read_signal_map_rejects_duplicate_names(tmp_path):
    p = tmp_path / "m.tsv"
    p.write_text(SIGNAL_MAP_TSV + "haddr\t4\thex\t/other\ttb.other\n")
    with pytest.raises(ft.HarnessError) as e:
        ct.read_signal_map(str(p))
    assert "duplicate" in str(e.value)


def test_read_signal_map_requires_the_reserved_signals(tmp_path):
    p = tmp_path / "m.tsv"
    p.write_text("\n".join([
        "\t".join(["name", "width", "radix", "hw_path", "sim_path"]),
        "\t".join(["haddr", "4", "hex", "/a", "tb.a"]),
    ]) + "\n")
    with pytest.raises(ft.HarnessError) as e:
        ct.read_signal_map(str(p))
    assert "trigger_marker" in str(e.value)


def test_read_signal_map_missing_file_is_a_harness_error(tmp_path):
    with pytest.raises(ft.HarnessError):
        ct.read_signal_map(str(tmp_path / "nope.tsv"))


# ==========================================================================
# 4. The manifest's iice block
# ==========================================================================

def test_read_manifest_iice(fixture_dir):
    m = ct.read_manifest_iice(str(fixture_dir / "signals_unittest.yaml"))
    assert m["depth"] == DEPTH
    assert m["trigger_time"] == TRIGGER_TIME
    assert m["edge"] == "positive"
    assert m["name"] == "IICE_UNITTEST"


@pytest.mark.parametrize("bad,needle", [
    ("depth: 4", "depth"),
    ("trigger_time: whenever", "trigger_time"),
])
def test_read_manifest_iice_rejects_bad_values(tmp_path, bad, needle):
    key = bad.split(":")[0]
    body = MANIFEST_YAML
    body = "\n".join(
        ("  " + bad) if l.strip().startswith(key + ":") else l
        for l in body.splitlines()
    )
    p = tmp_path / "bad.yaml"
    p.write_text(body)
    with pytest.raises(ft.HarnessError) as e:
        ct.read_manifest_iice(str(p))
    assert needle in str(e.value)


# ==========================================================================
# 5. THE ONE-SAMPLE TRIGGER OFFSET  (INTERFACES.md §3.2)
# ==========================================================================

def test_find_trigger_sample_applies_the_one_sample_offset():
    # marker high at sample 5 => the condition held at sample 4.
    marker = ["x"] + ["0"] * 4 + ["1"] + ["0"] * 4
    assert ct.find_trigger_sample(marker) == 4


def test_find_trigger_sample_uses_the_FIRST_high_sample():
    marker = ["0", "0", "0", "1", "1", "1", "0"]
    assert ct.find_trigger_sample(marker) == 2


def test_find_trigger_sample_ignores_x_as_high():
    """X must not be read as 'maybe fired' -- that would position the window
    arbitrarily, which the contract forbids."""
    marker = ["x", "x", "0", "1", "0"]
    assert ct.find_trigger_sample(marker) == 2


def test_find_trigger_sample_errors_when_the_trigger_never_fires():
    with pytest.raises(ft.HarnessError) as e:
        ct.find_trigger_sample(["0"] * 50)
    assert "never fired" in str(e.value)


def test_find_trigger_sample_errors_when_marker_is_high_at_sample_zero():
    with pytest.raises(ft.HarnessError) as e:
        ct.find_trigger_sample(["1", "0", "0"])
    assert "sample 0" in str(e.value)


# ==========================================================================
# 6. Window maths -- all three trigger_time values
# ==========================================================================

@pytest.mark.parametrize("depth,tt,want_offset", [
    (8, "early", 0),
    (8, "middle", 4),
    (8, "late", 7),
    (1024, "middle", 512),      # plan §5.1: depth/2
    (9, "middle", 4),           # odd depth: extra sample goes post-trigger
    (9, "early", 0),
    (9, "late", 8),
])
def test_trigger_offset(depth, tt, want_offset):
    assert ct.trigger_offset(depth, tt) == want_offset


def test_trigger_offset_rejects_unknown_trigger_time():
    with pytest.raises(ft.HarnessError):
        ct.trigger_offset(8, "midle")


@pytest.mark.parametrize("tt,want", [
    ("early", (9, 16)),     # trigger first: [T .. T+depth-1]
    ("middle", (5, 12)),    # depth//2 before, trigger at window index 4
    ("late", (2, 9)),       # depth-1 before, trigger last
])
def test_window_bounds_for_each_trigger_time(tt, want):
    assert ct.window_bounds(20, 9, 8, tt) == want


def test_window_bounds_middle_has_exactly_depth_over_2_pre_trigger_samples():
    start, end = ct.window_bounds(2000, 1000, 1024, "middle")
    assert end - start + 1 == 1024
    assert 1000 - start == 512          # 512 samples strictly before the trigger
    assert end - 1000 == 511            # trigger + 511 after


def test_window_bounds_errors_when_the_window_runs_off_the_start():
    with pytest.raises(ft.HarnessError) as e:
        ct.window_bounds(20, 2, 8, "middle")
    assert "does not fit" in str(e.value)


def test_window_bounds_errors_when_the_window_runs_off_the_end():
    with pytest.raises(ft.HarnessError) as e:
        ct.window_bounds(12, 9, 8, "early")
    assert "does not fit" in str(e.value)


# ==========================================================================
# 7. Sampling on clock edges, and the crop end to end
# ==========================================================================

def test_samples_from_clock_edges_reads_state_after_the_edge(fixture_dir):
    rows, trace = load_fixture_sim_trace(fixture_dir)
    assert trace.n_samples == N_EDGES
    for i in range(N_EDGES):
        haddr, flag = sim_sample_values(i)
        assert trace.values["haddr"][i] == haddr, i
        assert trace.values["flag"][i] == flag, i
        assert trace.values["trigger_marker"][i] == sim_marker(i), i
    # sample_clk is 1 at every sample by construction -- which is exactly why
    # comparing it would be vacuous.
    assert set(trace.values["sample_clk"]) == {"1"}


def test_samples_from_clock_edges_rejects_a_missing_signal(fixture_dir):
    rows, _ = load_fixture_sim_trace(fixture_dir)
    variables, changes = ft.parse_vcd(str(fixture_dir / "sim_iice.vcd"))
    mapping = {"iice/%s" % r.name: r.name for r in rows}
    mapping["iice/not_there"] = "not_there"
    with pytest.raises(ft.HarnessError) as e:
        ct.samples_from_clock_edges(variables, changes, mapping, ct.SAMPLE_CLK)
    assert "not_there" in str(e.value)
    assert "never a skipped row" in str(e.value)


def test_samples_from_clock_edges_negative_edge():
    variables = [
        ft.VcdVar("!", "d", 2, ("iice",)),
        ft.VcdVar("#", "sample_clk", 1, ("iice",)),
    ]
    changes = [
        (0, [("!", "00"), ("#", "1")]),
        (5, [("#", "0"), ("!", "01")]),     # negedge -> sample "01"
        (10, [("#", "1"), ("!", "10")]),
        (15, [("#", "0"), ("!", "11")]),    # negedge -> sample "11"
    ]
    mapping = {"iice/d": "d", "iice/sample_clk": "sample_clk"}
    trace = ct.samples_from_clock_edges(variables, changes, mapping, "sample_clk", "negative")
    assert trace.values["d"] == ["01", "11"]


def test_crop_sim_window_positions_the_trigger_correctly(fixture_dir):
    rows, trace = load_fixture_sim_trace(fixture_dir)
    window, info = ct.crop_sim_window(trace, DEPTH, TRIGGER_TIME)
    assert info["trigger_sample"] == TRIGGER_SAMPLE
    assert info["marker_first_high_sample"] == TRIGGER_SAMPLE + 1
    assert info["trigger_window_index"] == DEPTH // 2
    assert (info["window_start_sample"], info["window_end_sample"]) == (5, 12)
    assert window.n_samples == DEPTH
    # window index 4 must be the trigger sample's data
    want_haddr, _ = sim_sample_values(TRIGGER_SAMPLE)
    assert window.values["haddr"][DEPTH // 2] == want_haddr
    # and window index 0 must be absolute sample 5
    want0, _ = sim_sample_values(5)
    assert window.values["haddr"][0] == want0


@pytest.mark.parametrize("tt,expect_start", [("early", 9), ("middle", 5), ("late", 2)])
def test_crop_sim_window_honours_each_trigger_time(fixture_dir, tt, expect_start):
    rows, trace = load_fixture_sim_trace(fixture_dir)
    window, info = ct.crop_sim_window(trace, DEPTH, tt)
    assert info["window_start_sample"] == expect_start
    assert window.n_samples == DEPTH
    want, _ = sim_sample_values(expect_start)
    assert window.values["haddr"][0] == want
    # the trigger sample's data always lands at trigger_offset
    want_t, _ = sim_sample_values(TRIGGER_SAMPLE)
    assert window.values["haddr"][ct.trigger_offset(DEPTH, tt)] == want_t


def test_crop_sim_window_errors_without_a_marker():
    trace = ct.SampleTrace({"haddr": 4}, {"haddr": ["0000"] * 10})
    with pytest.raises(ft.HarnessError) as e:
        ct.crop_sim_window(trace, 8, "middle")
    assert "trigger_marker" in str(e.value)


# ==========================================================================
# 8. X/Z masking -- ONE WAY ONLY.  Hardware can never produce X.
# ==========================================================================

@pytest.mark.parametrize("g,s,want_bad,want_masked", [
    ("1010", "1010", [], []),
    ("1x10", "1110", [], [2]),          # sim X vs hw 1 -> masked
    ("1x10", "1010", [], [2]),          # sim X vs hw 0 -> masked
    ("1z10", "1110", [], [2]),          # sim Z likewise
    ("1x1x", "1111", [], [2, 0]),
    ("1010", "1110", [2], []),          # a real difference
    ("1010", "1x10", [2], []),          # hw X vs sim 0 -> MISMATCH
    ("1x10", "1x10", [], [2]),          # both X -> masked (sim X wins)
    ("0000", "zzzz", [3, 2, 1, 0], []), # hw Z everywhere -> all mismatch
])
def test_compare_bits_one_way_x_rule(g, s, want_bad, want_masked):
    bad, masked, hw_x = tc.compare_bits(g, s)
    assert sorted(bad) == sorted(want_bad)
    assert sorted(masked) == sorted(want_masked)
    assert hw_x == any(s[len(s) - 1 - b] in "xz" for b in want_bad)


def test_compare_bits_rejects_width_mismatch():
    with pytest.raises(ft.HarnessError):
        tc.compare_bits("101", "1010")


def test_hardware_x_is_never_silently_tolerated():
    """The dangerous direction: if this ever masks, a broken capture reads green."""
    bad, masked, hw_x = tc.compare_bits("0", "x")
    assert bad == [0] and masked == [] and hw_x is True


# ==========================================================================
# 9. compare_traces: the happy path, then the negative controls
# ==========================================================================

def _trace(values, widths=None):
    widths = widths or {k: len(v[0]) for k, v in values.items()}
    return ct.SampleTrace(widths, values)


def test_compare_traces_clean_match_exits_zero():
    g = _trace({"a": ["0000", "0001", "0010"], "b": ["0", "1", "0"]})
    s = _trace({"a": ["0000", "0001", "0010"], "b": ["0", "1", "0"]})
    res = tc.compare_traces(g, s, ["a", "b"])
    assert res.matched is True
    assert res.exit_code == ft.EXIT_MATCH == 0
    assert res.mismatch_samples == 0
    assert res.first_diverging_sample is None
    assert res.sample_comparisons == 6
    assert res.masked_samples == 0


def test_compare_traces_detects_a_ONE_BIT_perturbation():
    """THE deliverable: a single flipped bit must produce exit 1."""
    g = _trace({"a": ["0000", "0001", "0010", "0011"]})
    s = _trace({"a": ["0000", "0001", "0110", "0011"]})   # sample 2, bit 2
    res = tc.compare_traces(g, s, ["a"])
    assert res.matched is False
    assert res.exit_code == ft.EXIT_MISMATCH == 1
    assert res.mismatch_samples == 1
    assert res.mismatch_bits == 1
    assert res.first_diverging_sample == 2
    assert res.first_diverging_signals == ["a"]
    assert res.per_signal_mismatch == {"a": 1}
    assert res.records[0].bad_bits == [2]


def test_compare_traces_reports_the_FIRST_diverging_sample_not_the_last():
    g = _trace({"a": ["0", "0", "0", "0", "0"]})
    s = _trace({"a": ["0", "0", "1", "0", "1"]})
    res = tc.compare_traces(g, s, ["a"])
    assert res.first_diverging_sample == 2
    assert res.mismatch_samples == 2


def test_compare_traces_counts_masked_samples_separately_from_matches():
    g = _trace({"a": ["xxxx", "0001", "xx10"]})
    s = _trace({"a": ["1010", "0001", "0110"]})
    res = tc.compare_traces(g, s, ["a"])
    assert res.matched is True
    assert res.masked_samples == 2
    assert res.fully_masked_samples == 1
    assert res.masked_bits == 4 + 2
    assert res.per_signal_masked == {"a": 2}
    assert abs(res.masked_fraction - 2.0 / 3.0) < 1e-9


def test_compare_traces_flags_hardware_x_as_mismatch_and_counts_it():
    g = _trace({"a": ["0000", "0001"]})
    s = _trace({"a": ["0000", "000x"]})
    res = tc.compare_traces(g, s, ["a"])
    assert res.matched is False
    assert res.hw_x_samples == 1
    assert res.records[0].hw_x is True


def test_compare_traces_masking_does_not_hide_a_difference_in_a_known_bit():
    """A partially-X sample must still catch a bad bit elsewhere in the word."""
    g = _trace({"a": ["1x01"]})
    s = _trace({"a": ["0101"]})           # bit 3 differs, bit 2 masked
    res = tc.compare_traces(g, s, ["a"])
    assert res.matched is False
    assert res.mismatch_bits == 1
    assert res.masked_bits == 1
    assert res.records[0].bad_bits == [3]


def test_compare_traces_errors_on_differing_sample_counts():
    g = _trace({"a": ["0", "0", "0"]})
    s = _trace({"a": ["0", "0"]})
    with pytest.raises(ft.HarnessError) as e:
        tc.compare_traces(g, s, ["a"])
    assert "sample-count mismatch" in str(e.value)


def test_compare_traces_errors_on_differing_widths():
    g = ct.SampleTrace({"a": 4}, {"a": ["0000"]})
    s = ct.SampleTrace({"a": 2}, {"a": ["00"]})
    with pytest.raises(ft.HarnessError) as e:
        tc.compare_traces(g, s, ["a"])
    assert "width mismatch" in str(e.value)


def test_compare_traces_errors_on_a_signal_missing_from_one_side():
    g = _trace({"a": ["0"], "b": ["0"]})
    s = _trace({"a": ["0"]})
    with pytest.raises(ft.HarnessError) as e:
        tc.compare_traces(g, s, ["a", "b"])
    assert "b" in str(e.value)


def test_compare_traces_errors_when_there_is_nothing_to_compare():
    g = _trace({"a": ["0"]})
    with pytest.raises(ft.HarnessError):
        tc.compare_traces(g, g, [])


# ==========================================================================
# 10. Set-equality precheck (INTERFACES.md §4)
# ==========================================================================

def _rows():
    return ct.read_signal_map(_write_tmp(SIGNAL_MAP_TSV))


_TMP_FILES = []


def _write_tmp(text, suffix=".tsv"):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    _TMP_FILES.append(path)
    return path


def test_check_signal_sets_accepts_the_expected_sets():
    rows = _rows()
    sim = {"iice/haddr", "iice/flag", "iice/trigger_marker", "iice/sample_clk"}
    hw = {"/rp_top/u_dut/HADDR", "/rp_top/u_dut/FLAG"}
    info = tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert info["sim_signals"] == 4 and info["hw_signals"] == 2


def test_check_signal_sets_rejects_a_missing_sim_signal():
    rows = _rows()
    sim = {"iice/haddr", "iice/trigger_marker", "iice/sample_clk"}
    hw = {"/rp_top/u_dut/HADDR", "/rp_top/u_dut/FLAG"}
    with pytest.raises(ft.HarnessError) as e:
        tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    msg = str(e.value)
    assert "no scope" in msg and "missing there: ['flag']" in msg


def test_check_signal_sets_rejects_a_missing_hw_signal():
    """A missing signal is exit 2, never a skipped row."""
    rows = _rows()
    sim = {"iice/haddr", "iice/flag", "iice/trigger_marker", "iice/sample_clk"}
    hw = {"/rp_top/u_dut/HADDR"}
    with pytest.raises(ft.HarnessError) as e:
        tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert "missing:    ['FLAG']" in str(e.value)


# --- hardware side is matched on LEAF names (real captures are flat) ---------

def test_check_signal_sets_matches_hw_on_leaf_names_ignoring_hierarchy():
    """A real Identify capture flattens the hierarchy away, and puts everything
    in one scope named after the instrumented module."""
    rows = _rows()
    sim = {"iice/%s" % r.name for r in rows}
    for hw in (
        {"rp_top/u_dut/HADDR", "rp_top/u_dut/FLAG"},   # synthetic, full path
        {"rm_led/HADDR", "rm_led/FLAG"},               # Identify-shaped
        {"HADDR", "FLAG"},                             # no scope at all
    ):
        tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)


def test_check_signal_sets_whitelists_the_two_identify_injected_signals():
    rows = _rows()
    sim = {"iice/%s" % r.name for r in rows}
    hw = {"rm_led/HADDR", "rm_led/FLAG",
          "rm_led/identify_sampleclock", "rm_led/identify_cycle"}
    info = tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert info["identify_injected_seen"] == ["identify_cycle", "identify_sampleclock"]


def test_check_signal_sets_still_rejects_an_UNRECOGNISED_extra():
    """The whitelist must not become 'ignore anything unexpected'."""
    rows = _rows()
    sim = {"iice/%s" % r.name for r in rows}
    hw = {"rm_led/HADDR", "rm_led/FLAG", "rm_led/identify_cycle", "rm_led/mystery"}
    with pytest.raises(ft.HarnessError) as e:
        tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert "unexpected: ['mystery']" in str(e.value)


def test_check_signal_sets_accepts_an_optional_signal_that_IS_present():
    """hw_optional means MAY be absent, not MUST be absent -- a real capture
    does contain the sample clock (as `dut_clk`)."""
    rows = _rows()   # sample_clk hw_path is /rp_top/dut_clk -> leaf dut_clk
    sim = {"iice/%s" % r.name for r in rows}
    hw = {"rm_led/HADDR", "rm_led/FLAG", "rm_led/dut_clk"}
    info = tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert info["hw_optional_present"] == ["sample_clk"]


def test_check_signal_sets_rejects_hw_leaf_name_collisions():
    rows = _rows()
    sim = {"iice/%s" % r.name for r in rows}
    hw = {"a/HADDR", "b/HADDR", "rm_led/FLAG"}
    with pytest.raises(ft.HarnessError) as e:
        tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert "sharing a leaf name" in str(e.value)


def test_hw_leaf_map_rejects_manifest_leaf_collisions():
    """Two probes reducing to the same leaf cannot be told apart in a flat
    capture, so the manifest must rename one."""
    rows = ct.read_signal_map(_write_tmp("\n".join([
        "\t".join(["name", "width", "radix", "hw_path", "sim_path"]),
        "\t".join(["a", "1", "bin", "/top/u_x/HADDR", "tb.a"]),
        "\t".join(["b", "1", "bin", "/top/u_y/HADDR", "tb.b"]),
        "\t".join(["trigger_marker", "1", "bin", "__DERIVED__", "__DERIVED__"]),
        "\t".join(["sample_clk", "1", "bin", "/top/clk", "tb.clk"]),
    ]) + "\n"))
    with pytest.raises(ft.HarnessError) as e:
        ct.hw_leaf_map(rows)
    assert "leaf name 'HADDR'" in str(e.value)


def test_hw_leaf_map_includes_optional_rows():
    rows = _rows()
    m = ct.hw_leaf_map(rows)
    assert m == {"HADDR": "haddr", "FLAG": "flag", "dut_clk": "sample_clk"}
    assert "trigger_marker" not in m.values()   # __DERIVED__ excluded


def test_check_signal_sets_tolerates_leading_slash_differences():
    """signal_map.tsv writes '/a/b' (Identify) while an FSDB scope chain is
    'a/b'.  If these were compared literally, EVERY hardware signal would look
    missing -- a harness that can only ever report exit 2."""
    rows = _rows()
    sim = {"iice/haddr", "iice/flag", "iice/trigger_marker", "iice/sample_clk"}
    for hw in ({"/rp_top/u_dut/HADDR", "/rp_top/u_dut/FLAG"},
               {"rp_top/u_dut/HADDR", "rp_top/u_dut/FLAG"}):
        tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)


def test_check_signal_sets_rejects_unexpected_extra_signals():
    rows = _rows()
    sim = {"iice/haddr", "iice/flag", "iice/trigger_marker", "iice/sample_clk", "iice/surprise"}
    hw = {"/rp_top/u_dut/HADDR", "/rp_top/u_dut/FLAG"}
    with pytest.raises(ft.HarnessError) as e:
        tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert "extra there: ['surprise']" in str(e.value)


# --- scope resolution: INTERFACES.md §1 identifies the scope by its LEAF SET ---
# The scope name differs between the two sides by design, so nothing here may
# privilege any particular name -- only the leaf set decides.

@pytest.mark.parametrize("scope_name", [
    "iice",                 # the conventional name
    "tb/u_iice_shadow",     # what $fsdbDumpvars actually records (instance path)
    "some/deeply/nested/instance",
])
def test_resolve_sim_scope_accepts_any_scope_name_with_the_right_leaf_set(scope_name):
    rows = _rows()
    sim = {"%s/%s" % (scope_name, r.name) for r in rows}
    scope, note = ct.resolve_sim_scope(sim, rows)
    assert scope == scope_name
    # the note is informational diagnostics, not a warning about a defect
    assert note and scope_name in note
    for banned in ("DEVIATION", "should be fixed", "does not meet"):
        assert banned not in note, note


def test_resolve_sim_scope_rejects_an_ambiguous_fsdb():
    """Two candidate scopes must be a hard error: comparing the wrong haddr
    against the wrong haddr is the precise failure this harness prevents."""
    rows = _rows()
    sim = set()
    for pfx in ("tb/u_a", "tb/u_b"):
        sim |= {"%s/%s" % (pfx, r.name) for r in rows}
    with pytest.raises(ft.HarnessError) as e:
        ct.resolve_sim_scope(sim, rows)
    assert "ambiguous" in str(e.value)


def test_resolve_sim_scope_rejects_a_partial_scope():
    rows = _rows()
    sim = {"tb/u_iice_shadow/haddr", "tb/u_iice_shadow/sample_clk"}
    with pytest.raises(ft.HarnessError) as e:
        ct.resolve_sim_scope(sim, rows)
    assert "no scope" in str(e.value)


def test_check_signal_sets_resolves_an_instance_scope_and_reports_it():
    rows = _rows()
    sim = {"tb/u_iice_shadow/%s" % r.name for r in rows}
    hw = {"/rp_top/u_dut/HADDR", "/rp_top/u_dut/FLAG"}
    info = tc.check_signal_sets(sim, hw, rows, hw_optional=tc.STRUCTURAL_SIGNALS)
    assert info["sim_scope"] == "tb/u_iice_shadow"
    assert info["sim_scope_note"]


def test_report_shows_the_resolved_scope_without_implying_a_defect():
    """The scope name differing between sides is contract-conformant per the
    amended §1.  The report must say which scope was used -- useful diagnostics --
    but must not send a reader chasing a non-existent generator bug."""
    rows = _rows()
    g = _trace({"haddr": ["0000"], "flag": ["0"]})
    res = tc.compare_traces(g, g, ["haddr", "flag"])
    text = tc.format_report(res, rows, {
        "sim_scope": "tb/u_iice_shadow",
        "sim_scope_note": ct.resolve_sim_scope(
            {"tb/u_iice_shadow/%s" % r.name for r in rows}, rows)[1],
    })
    assert "tb/u_iice_shadow" in text
    assert "scope resolution" in text
    for banned in ("DEVIATION", "should be fixed", "does not meet",
                   "mandates a flat scope"):
        assert banned not in text, banned


def test_full_hw_path_matching_is_gone_not_merely_unused():
    """A real Identify capture is flat, so full-hw_path matching can never match
    one.  Leaving a plausible-looking alternative matcher around invites wiring
    up the wrong one, so it was removed rather than deprecated."""
    assert not hasattr(ct, "hw_path_map")


# ==========================================================================
# 11. Perturbation and X quantisation (synth_hw_fsdb, pure parts)
# ==========================================================================

@pytest.mark.parametrize("bits,bit,want", [
    ("0000", 0, "0001"),
    ("0000", 3, "1000"),
    ("1111", 2, "1011"),
    ("0", 0, "1"),
])
def test_flip_bit(bits, bit, want):
    assert sh.flip_bit(bits, bit) == want


def test_flip_bit_refuses_an_x_bit():
    with pytest.raises(ft.HarnessError):
        sh.flip_bit("00x0", 1)


def test_quantise_trace_removes_every_x_and_z():
    t = _trace({"a": ["xxxx", "1z01", "0011"]})
    q, n = sh.quantise_trace(t, "prng", "seed")
    assert n == 5
    for col in q.values["a"]:
        assert not (set(col) & set("xz"))
    assert q.values["a"][2] == "0011"       # known bits untouched


def test_quantise_trace_is_deterministic_and_seed_sensitive():
    t = _trace({"a": ["xxxxxxxx"] * 4})
    a, _ = sh.quantise_trace(t, "prng", "s1")
    b, _ = sh.quantise_trace(t, "prng", "s1")
    c, _ = sh.quantise_trace(t, "prng", "s2")
    assert a.values == b.values
    assert a.values != c.values


def test_quantise_trace_prng_is_not_a_constant_fill():
    """A constant fill could let a broken X-mask pass by luck; a mixed fill cannot."""
    t = _trace({"a": ["x" * 16] * 8})
    q, _ = sh.quantise_trace(t, "prng", "iice")
    allbits = "".join(q.values["a"])
    assert "0" in allbits and "1" in allbits


def test_apply_perturbation_refuses_a_masked_target():
    """The trap this guards: flipping a bit the sim had as X would be masked by
    the one-way don't-care rule, so the 'negative control' would pass while
    proving absolutely nothing."""
    sim = _trace({"a": ["1x01"]})
    hw = _trace({"a": ["1101"]})
    with pytest.raises(ft.HarnessError) as e:
        sh.apply_perturbation(hw, sim, "a", 0, 2)
    assert "proving nothing" in str(e.value)


def test_apply_perturbation_flips_exactly_one_bit():
    sim = _trace({"a": ["1001"], "b": ["0"]})
    hw = _trace({"a": ["1001"], "b": ["0"]})
    note = sh.apply_perturbation(hw, sim, "a", 0, 3)
    assert hw.values["a"] == ["0001"]
    assert hw.values["b"] == ["0"]
    assert "1001 -> 0001" in note
    res = tc.compare_traces(sim, hw, ["a", "b"])
    assert res.exit_code == 1 and res.mismatch_bits == 1


def test_apply_perturbation_rejects_out_of_range():
    sim = _trace({"a": ["1001"]})
    hw = _trace({"a": ["1001"]})
    with pytest.raises(ft.HarnessError):
        sh.apply_perturbation(hw, sim, "a", 99, 0)
    with pytest.raises(ft.HarnessError):
        sh.apply_perturbation(hw, sim, "a", 0, 99)
    with pytest.raises(ft.HarnessError):
        sh.apply_perturbation(hw, sim, "nope", 0, 0)


def test_choose_perturb_target_never_picks_an_x_bit():
    sim = _trace({"a": ["xxxx", "xxxx", "0101", "xxxx"]})
    name, sample, bit = sh.choose_perturb_target(sim, ["a"], prefer_from=0)
    assert (name, sample) == ("a", 2)
    assert sim.values["a"][sample][len("0101") - 1 - bit] in "01"


def test_choose_perturb_target_prefers_at_or_after_the_trigger():
    sim = _trace({"a": ["0101", "0101", "0101", "0101"]})
    name, sample, bit = sh.choose_perturb_target(sim, ["a"], prefer_from=2)
    assert sample == 2


def test_choose_perturb_target_falls_back_before_the_trigger():
    sim = _trace({"a": ["0101", "xxxx", "xxxx", "xxxx"]})
    name, sample, bit = sh.choose_perturb_target(sim, ["a"], prefer_from=2)
    assert sample == 0


def test_choose_perturb_target_errors_when_everything_is_x():
    sim = _trace({"a": ["xxxx"] * 4})
    with pytest.raises(ft.HarnessError) as e:
        sh.choose_perturb_target(sim, ["a"], prefer_from=0)
    assert "negative control cannot run" in str(e.value)


def test_inject_hw_x_is_detected_as_a_mismatch():
    """The dangerous direction: hardware X must never be masked."""
    sim = _trace({"a": ["1001"]})
    hw = _trace({"a": ["1001"]})
    note = sh.inject_hw_x(hw, sim, "a", 0, 3)
    assert hw.values["a"] == ["x001"]
    assert "1001 -> x001" in note
    res = tc.compare_traces(sim, hw, ["a"])
    assert res.exit_code == 1
    assert res.hw_x_samples == 1


def test_inject_hw_x_refuses_a_masked_target():
    sim = _trace({"a": ["1x01"]})
    hw = _trace({"a": ["1101"]})
    with pytest.raises(ft.HarnessError) as e:
        sh.inject_hw_x(hw, sim, "a", 0, 2)
    assert "nothing is proven" in str(e.value)


def test_inject_hw_x_rejects_out_of_range():
    sim = _trace({"a": ["1001"]})
    hw = _trace({"a": ["1001"]})
    for args in (("a", 99, 0), ("a", 0, 99), ("nope", 0, 0)):
        with pytest.raises(ft.HarnessError):
            sh.inject_hw_x(hw, sim, *args)


def test_synth_hw_cli_refuses_two_negative_controls_at_once(tmp_path):
    """A negative control that flips two things at once proves less, not more."""
    rc = sh.main([
        "--sim-fsdb", str(tmp_path / "a.fsdb"), "--signal-map", str(tmp_path / "m.tsv"),
        "--manifest", str(tmp_path / "m.yaml"), "--out", str(tmp_path / "o.fsdb"),
        "--logdir", str(tmp_path / "l"), "--perturb-auto", "--inject-hw-x-auto",
    ])
    assert rc == ft.EXIT_HARNESS


def test_parse_perturb():
    assert sh.parse_perturb("haddr:12:3") == ("haddr", 12, 3)
    with pytest.raises(ft.HarnessError):
        sh.parse_perturb("haddr:12")
    with pytest.raises(ft.HarnessError):
        sh.parse_perturb("haddr:x:3")


# ==========================================================================
# 12. Exit-code mapping (FROZEN)
# ==========================================================================

def test_frozen_exit_code_values():
    assert (ft.EXIT_MATCH, ft.EXIT_MISMATCH, ft.EXIT_HARNESS) == (0, 1, 2)


def test_compare_result_exit_code_mapping():
    r = tc.CompareResult()
    r.mismatch_samples = 0
    assert r.exit_code == 0
    r.mismatch_samples = 1
    assert r.exit_code == 1


@needs_verdi
def test_find_tool_does_not_resolve_the_verdi_wrapper_symlink():
    """Regression: every Verdi utility is a symlink to one `.wrapper` that
    dispatches on argv[0].  Resolving the link collapses them all to `.wrapper`,
    which then exits 127 for every tool.  The basename must survive."""
    for tool in ("fsdb2vcd", "vcd2fsdb"):
        p = ft.find_tool(tool)
        assert os.path.basename(p) == tool, p
        assert not p.endswith(".wrapper"), p


def test_missing_tool_raises_a_harness_error_not_a_pass():
    with pytest.raises(ft.ToolMissing) as e:
        ft.find_tool("definitely_not_a_real_verdi_tool_xyz")
    assert isinstance(e.value, ft.HarnessError)
    assert "module load" in str(e.value)


def test_trace_compare_cli_maps_a_harness_error_to_exit_2(tmp_path):
    """The CLI must return 2 -- and write a report saying so -- when the inputs
    are unusable.  A harness error is never a match."""
    report = tmp_path / "r.txt"
    rc = tc.main([
        "--sim-fsdb", str(tmp_path / "nope.fsdb"),
        "--hw-fsdb", str(tmp_path / "nope2.fsdb"),
        "--signal-map", str(tmp_path / "nope.tsv"),
        "--manifest", str(tmp_path / "nope.yaml"),
        "--report", str(report),
        "--logdir", str(tmp_path / "logs"),
    ])
    assert rc == ft.EXIT_HARNESS == 2
    assert "HARNESS ERROR" in report.read_text()
    assert "MATCH" not in report.read_text().split("\n")[0]


# ==========================================================================
# 13. nCompare rule-file generation and .nce parsing (no tool needed)
# ==========================================================================

def test_build_ncr_pairs_sim_names_against_hardware_paths(tmp_path):
    rows = _rows()
    out = str(tmp_path / "r.ncr")
    tc.build_ncr("/g.fsdb", "/s.fsdb", rows, ["haddr", "flag"], out)
    text = open(out).read()
    assert "cmpOpenFsdb /g.fsdb /s.fsdb" in text
    assert "cmpSetDelimiter ." in text
    # the one-way X rule, expressed for the vendor tool
    assert "-asym (x,0,T) (x,1,T)" in text
    assert "(0,x,F) (1,x,F)" in text
    # width>1 gets a bit range, width==1 does not
    assert "cmpSetSignalPair {iice.haddr[3:0]} {rp_top.u_dut.HADDR[3:0]}" in text
    assert "cmpSetSignalPair {iice.flag} {rp_top.u_dut.FLAG}" in text
    assert text.rstrip().endswith("cmpCompare")


def test_build_ncr_refuses_a_derived_signal(tmp_path):
    rows = _rows()
    with pytest.raises(ft.HarnessError) as e:
        tc.build_ncr("/g", "/s", rows, ["trigger_marker"], str(tmp_path / "r.ncr"))
    assert "__DERIVED__" in str(e.value)


_NCE_CLEAN = """; comment
100 "/tmp/r.ncr" 0 0
010 3 F F 0 0 T T 0ns 0ns 0ns 1 0ns 30ns 2 2
500 "/tmp/g.fsdb" "/tmp/s.fsdb" 1n 1n 2 0 0
; end
"""

_NCE_MISMATCH = """100 "/tmp/r.ncr" 0 0
500 "/tmp/g.fsdb" "/tmp/s.fsdb" 1n 1n 2 1 3
510 30ns 30ns 0ns 0ns `/iice/haddr[3:0]` `/rp_top/u_dut/HADDR[3:0]` 3 1 2 1X01 0101
510 40ns 40ns 0ns 0ns `/iice/haddr[3:0]` `/rp_top/u_dut/HADDR[3:0]` 3 1 2 1101 0101
"""

_NCE_RULE_ERROR = """100 "/tmp/r.ncr" 1 0
101 7 "Cannot find specified signal in golden FSDB"
500 "/tmp/g.fsdb" "/tmp/s.fsdb" 1n 1n 1 0 0
"""

_NCE_NOTHING_COMPARED = """100 "/tmp/r.ncr" 0 0
500 "/tmp/g.fsdb" "/tmp/s.fsdb" 1n 1n 0 0 0
"""


def test_parse_nce_clean():
    info = tc.parse_nce(_write_tmp(_NCE_CLEAN, ".nce"))
    assert info["compared"] == 2
    assert info["mismatched_signals"] == 0
    assert info["errors"] == 0


def test_parse_nce_mismatch():
    info = tc.parse_nce(_write_tmp(_NCE_MISMATCH, ".nce"))
    assert info["compared"] == 2
    assert info["mismatched_signals"] == 1
    assert info["errors"] == 3
    assert info["mismatch_lines"] == 2


def test_parse_nce_rule_error_is_a_harness_error():
    with pytest.raises(ft.HarnessError) as e:
        tc.parse_nce(_write_tmp(_NCE_RULE_ERROR, ".nce"))
    assert "rule-file error" in str(e.value)


def test_parse_nce_zero_pairs_compared_is_a_harness_error():
    """nCompare happily reports 0 errors over 0 pairs; that must not read as a
    pass -- it is the classic gate-that-cannot-fail."""
    with pytest.raises(ft.HarnessError) as e:
        tc.parse_nce(_write_tmp(_NCE_NOTHING_COMPARED, ".nce"))
    assert "0 signal pairs" in str(e.value)


def test_parse_nce_without_a_summary_record_is_a_harness_error():
    with pytest.raises(ft.HarnessError):
        tc.parse_nce(_write_tmp("100 \"/tmp/r.ncr\" 0 0\n", ".nce"))


def test_parse_nce_missing_file_is_a_harness_error(tmp_path):
    with pytest.raises(ft.HarnessError):
        tc.parse_nce(str(tmp_path / "nope.nce"))


# ==========================================================================
# 14. The report must state everything INTERFACES.md §4 demands
# ==========================================================================

def test_report_states_every_mandatory_field():
    rows = _rows()
    g = _trace({"haddr": ["0000", "0001", "xx10"], "flag": ["0", "1", "0"]})
    s = _trace({"haddr": ["0000", "0101", "0110"], "flag": ["0", "1", "0"]})
    res = tc.compare_traces(g, s, ["haddr", "flag"])
    text = tc.format_report(res, rows, {
        "depth": 3, "trigger_time": "middle", "trigger_window_index": 1,
        "marker_high_samples_in_trace": 1,
    })
    import re
    assert "total samples compared" in text
    assert "MISMATCH COUNT" in text
    assert re.search(r"FIRST DIVERGING SAMPLE\s+1\b", text), text
    assert "X-masked samples" in text
    assert "per-signal" in text
    for name in ("haddr", "flag"):
        assert name in text
    assert "VERDICT: MISMATCH" in text
    assert "exit code 1" in text
    # values rendered in the manifest radix (plan §5.5)
    assert "'h5" in text or "'h05" in text


def test_report_warns_loudly_when_mostly_masked():
    rows = _rows()
    g = _trace({"haddr": ["xxxx"] * 9 + ["0000"], "flag": ["x"] * 9 + ["0"]})
    s = _trace({"haddr": ["1010"] * 9 + ["0000"], "flag": ["1"] * 9 + ["0"]})
    res = tc.compare_traces(g, s, ["haddr", "flag"])
    assert res.matched is True
    assert res.masked_fraction == 0.9
    text = tc.format_report(res, rows, {})
    assert "WARNING" in text
    assert "worthless" in text


def test_report_notes_a_multi_sample_trigger_condition():
    rows = _rows()
    g = _trace({"haddr": ["0000"], "flag": ["0"]})
    res = tc.compare_traces(g, g, ["haddr", "flag"])
    text = tc.format_report(res, rows, {"marker_high_samples_in_trace": 4})
    assert "more than one sample" in text


def test_report_names_the_excluded_structural_signals():
    rows = _rows()
    g = _trace({"haddr": ["0000"], "flag": ["0"]})
    res = tc.compare_traces(g, g, ["haddr", "flag"])
    text = tc.format_report(res, rows, {})
    assert "excluded from the value diff" in text
    assert "sample_clk" in text and "trigger_marker" in text


def test_render_value():
    assert ct.render_value("1010", "hex") == "'hA"
    assert ct.render_value("00001010", "hex") == "'h0A"
    assert ct.render_value("1010", "bin") == "'b1010"
    assert ct.render_value("1010", "dec") == "10"
    assert ct.render_value("1x10", "hex") == "'b1x10"   # X-safe: never lies


# ==========================================================================
# 15. Verdi-dependent: the real FSDB pipeline, end to end
# ==========================================================================

@needs_verdi
def test_end_to_end_with_real_fsdbs_match_then_negative_control(fixture_dir):
    """Full pipeline on genuine FSDBs: clean compare exits 0, a one-bit
    perturbation exits 1, and a dropped signal exits 2.

    No VCS is involved: the sim FSDB is built from the hand-written VCD via
    vcd2fsdb, which is exactly the shape the generated shadow module dumps.
    """
    logdir = str(fixture_dir / "logs")
    sim_fsdb = ft.vcd2fsdb(str(fixture_dir / "sim_iice.vcd"),
                           str(fixture_dir / "sim_iice.fsdb"), logdir)
    smap = str(fixture_dir / "signal_map.tsv")
    manifest = str(fixture_dir / "signals_unittest.yaml")

    # the §4 precheck sees exactly the flat iice/* scope
    assert ft.list_signals(sim_fsdb, logdir) == {
        "iice/haddr", "iice/flag", "iice/trigger_marker", "iice/sample_clk"}

    # --- clean synthetic hardware -> MATCH -------------------------------
    hw_trace, desc = sh.build_hw_trace(sim_fsdb, smap, manifest, logdir)
    hw_fsdb = sh.write_hw_fsdb(hw_trace, desc["rows"], desc["names"],
                               str(fixture_dir / "hw_iice.fsdb"), logdir)
    # the rename really happened: hardware paths, not iice/*.  If this were an
    # identity no-op the "mangle back" in the compare step would never be
    # exercised, which is the whole point of synthesising at hardware paths.
    hw_sigs = ft.list_signals(hw_fsdb, logdir)
    assert hw_sigs == {"rp_top/u_dut/HADDR", "rp_top/u_dut/FLAG"}
    assert not any(s.startswith("iice/") for s in hw_sigs)
    rc, text = tc.run_compare(sim_fsdb, hw_fsdb, smap, manifest,
                              str(fixture_dir / "report.txt"), logdir)
    assert rc == 0, text
    assert "VERDICT: MATCH" in text
    # the X quantisation really was masked, so the mask is load-bearing here
    assert desc["x_quantised_bits"] > 0
    assert "X-masked samples" in text

    # --- NEGATIVE CONTROL: one flipped bit -> MISMATCH -------------------
    hw2, desc2 = sh.build_hw_trace(sim_fsdb, smap, manifest, logdir, perturb_auto=True)
    assert desc2["perturbation"]
    hw2_fsdb = sh.write_hw_fsdb(hw2, desc2["rows"], desc2["names"],
                                str(fixture_dir / "hw_perturbed.fsdb"), logdir)
    rc2, text2 = tc.run_compare(sim_fsdb, hw2_fsdb, smap, manifest,
                                str(fixture_dir / "report_perturbed.txt"), logdir)
    assert rc2 == 1, text2
    assert "VERDICT: MISMATCH" in text2
    assert "FIRST DIVERGING SAMPLE" in text2

    # --- a missing signal -> HARNESS ERROR -------------------------------
    hw3, desc3 = sh.build_hw_trace(sim_fsdb, smap, manifest, logdir, drop_signal="flag")
    hw3_fsdb = sh.write_hw_fsdb(hw3, desc3["rows"], desc3["names"],
                                str(fixture_dir / "hw_missing.fsdb"), logdir)
    with pytest.raises(ft.HarnessError) as e:
        tc.run_compare(sim_fsdb, hw3_fsdb, smap, manifest,
                       str(fixture_dir / "report_missing.txt"), logdir)
    assert "FLAG" in str(e.value)


@needs_verdi
def test_end_to_end_a_frozen_tail_is_a_NAMED_MISMATCH_not_a_harness_error(fixture_dir):
    """The pytest twin of `compare.sh` negctl NC5, through `run_compare`.

    NC5 only runs in root `make check` stage 9, which is guarded on VCS *and*
    nCompare and therefore skipped in most environments; this needs only the FSDB
    utilities, so the fix stays gated wherever Verdi exists.

    Two assertions, because exit 1 alone would also be satisfied by a report that
    sends the reader hunting for a value bug: the frozen tail must be reported as
    a MISMATCH **and** the text must say the hardware stopped moving.
    """
    logdir = str(fixture_dir / "logs")
    sim_fsdb = ft.vcd2fsdb(str(fixture_dir / "sim_iice.vcd"),
                           str(fixture_dir / "sim_iice.fsdb"), logdir)
    smap = str(fixture_dir / "signal_map.tsv")
    manifest = str(fixture_dir / "signals_unittest.yaml")
    freeze_from = DEPTH // 2
    hw, desc = sh.build_hw_trace(sim_fsdb, smap, manifest, logdir,
                                 freeze_tail_from=freeze_from)
    assert "FROZEN TAIL" in str(desc["perturbation"])
    hw_fsdb = sh.write_hw_fsdb(hw, desc["rows"], desc["names"],
                               str(fixture_dir / "hw_frozen.fsdb"), logdir)
    rc, text = tc.run_compare(sim_fsdb, hw_fsdb, smap, manifest,
                              str(fixture_dir / "report_frozen.txt"), logdir)
    assert rc == 1, text                              # NOT 2
    assert "VERDICT: MISMATCH" in text
    assert "holds its last value from sample %d of %d" % (freeze_from, DEPTH) in text
    assert "stalled or wedged" in text
    assert "QUIET TAIL" in text
    # and the report must say the span was proved, not guessed
    assert "PROVED by the FSDB's own end time" in text


@needs_verdi
def test_ncompare_crosscheck_agrees_on_both_verdicts(fixture_dir):
    """nCompare must independently agree: clean -> 0 errors, perturbed -> >0."""
    logdir = str(fixture_dir / "logs")
    sim_fsdb = ft.vcd2fsdb(str(fixture_dir / "sim_iice.vcd"),
                           str(fixture_dir / "sim_iice.fsdb"), logdir)
    smap = str(fixture_dir / "signal_map.tsv")
    manifest = str(fixture_dir / "signals_unittest.yaml")
    rows = ct.read_signal_map(smap)
    names = ct.data_signal_names(rows)
    trace = ct.load_sim_trace(sim_fsdb, rows, logdir, "positive")
    window, _ = ct.crop_sim_window(trace, DEPTH, TRIGGER_TIME)
    clean, _ = sh.quantise_trace(window.subset(names))

    if not ft.have_tool("nCompare"):
        pytest.skip("nCompare not on PATH")

    info = tc.ncompare_crosscheck(window, clean, rows, names, os.path.join(logdir, "nc_clean"))
    assert info["available"] is True
    assert info["compared"] == len(names)
    assert info["errors"] == 0, "nCompare found errors in an unperturbed pair"

    bad = ct.SampleTrace(clean.widths, clean.values, clean.meta)
    sh.apply_perturbation(bad, window.subset(names), *sh.choose_perturb_target(
        window.subset(names), names, prefer_from=ct.trigger_offset(DEPTH, TRIGGER_TIME)))
    info2 = tc.ncompare_crosscheck(window, bad, rows, names, os.path.join(logdir, "nc_bad"))
    assert info2["errors"] and int(info2["errors"]) > 0, \
        "nCompare did NOT see the one-bit perturbation"


# ==========================================================================
# 15b. Identify-shaped hardware captures
# ==========================================================================
# Fixture reproducing the layout MEASURED from a real `write fsdb` capture:
#   * time axis starts at a non-zero time and is trigger-relative
#   * flat names inside one scope named after the instrumented module
#   * identify_sampleclock ALIASED onto the same VCD identifier as dut_clk
#   * identify_cycle declared but with no value changes at all
# Synthesised rather than committing the 14 KB binary FSDB.

HW_FIXTURE_T0 = 10240
HW_FIXTURE_STEP = 10
HW_FIXTURE_DEPTH = 16

HW_SIGNAL_MAP_TSV = "\n".join([
    "\t".join(["name", "width", "radix", "hw_path", "sim_path"]),
    # deep hw_path on purpose: proves the hierarchy really is reduced to a leaf
    "\t".join(["blink_counter", "8", "hex", "/rm_led/u_blink/blink_counter",
               "tb.u_dut.blink_counter"]),
    "\t".join(["dut_resetn", "1", "bin", "/rm_led/dut_resetn", "tb.u_dut.dut_resetn"]),
    "\t".join(["trigger_marker", "1", "bin", "__DERIVED__", "__DERIVED__"]),
    "\t".join(["sample_clk", "1", "bin", "/rm_led/dut_clk", "tb.u_dut.dut_clk"]),
]) + "\n"


def make_identify_vcd_text(depth=HW_FIXTURE_DEPTH, t0=HW_FIXTURE_T0,
                           step=HW_FIXTURE_STEP, freeze_from=None):
    """Identify-shaped VCD.  *freeze_from* holds EVERY signal from that sample on.

    A frozen tail still emits its ``#t`` time tags (that is what a real capture
    of a wedged DUT would contain), so the FSDB keeps its true end time even
    though `fsdb2vcd` will later drop those timestamps from the VCD it emits.
    """
    L = [
        "$date\n\t\n$end",
        "$version\n\tHAPS_PRODUCT_HAPS-KEY-70\n$end",
        "$timescale\n\t1ns\n$end",
        "$scope module rm_led $end",
        '$var wire 1 ! identify_sampleclock $end',
        '$var wire 1 " identify_cycle $end',
        "$var reg 1 ! dut_clk $end",          # SAME ident as identify_sampleclock
        "$var reg 8 # blink_counter [7:0] $end",
        "$var reg 1 $ dut_resetn $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    prev = {}
    for i in range(depth):
        t = t0 + i * step
        j = i if freeze_from is None else min(i, freeze_from - 1)
        vals = {"!": "1" if j % 2 == 0 else "0",
                "#": format(j, "08b"),
                "$": "1" if j >= 2 else "0"}
        L.append("#%d" % t)
        if i == 0:
            L.append("$dumpvars")
        for ident in ("!", "#", "$"):
            if vals[ident] != prev.get(ident):
                L.append(("b%s %s" % (vals[ident], ident)) if ident == "#"
                         else "%s%s" % (vals[ident], ident))
                prev[ident] = vals[ident]
        if i == 0:
            L.append("$end")
    return "\n".join(L) + "\n"


@pytest.fixture
def hw_fixture(tmp_path):
    d = tmp_path / "hw_fixture"
    d.mkdir()
    (d / "signal_map.tsv").write_text(HW_SIGNAL_MAP_TSV)
    (d / "cap.vcd").write_text(make_identify_vcd_text())
    (d / "logs").mkdir()
    return d


@needs_verdi
def test_list_signals_works_when_the_trace_does_not_start_at_time_zero(hw_fixture):
    """REGRESSION: list_signals() used `fsdb2vcd -et 0`, which dies with exit 255
    ('End time specified is smaller than FSDB file's minimum time') on any
    trigger-relative trace -- i.e. on exactly the class of file this harness
    exists to read.  It must now work regardless of the minimum time."""
    logdir = str(hw_fixture / "logs")
    fsdb = ft.vcd2fsdb(str(hw_fixture / "cap.vcd"), str(hw_fixture / "cap.fsdb"), logdir)
    t_min, t_max = ft.fsdb_time_range(fsdb, logdir)
    assert t_min == HW_FIXTURE_T0 and t_min > 0, (t_min, t_max)
    sigs = ft.list_signals(fsdb, logdir)
    assert sigs == {
        "rm_led/identify_sampleclock", "rm_led/identify_cycle",
        "rm_led/dut_clk", "rm_led/blink_counter", "rm_led/dut_resetn",
    }


@needs_verdi
def test_hw_capture_loads_by_leaf_name_and_timestamp_ordinal(hw_fixture):
    logdir = str(hw_fixture / "logs")
    fsdb = ft.vcd2fsdb(str(hw_fixture / "cap.vcd"), str(hw_fixture / "cap.fsdb"), logdir)
    rows = ct.read_signal_map(str(hw_fixture / "signal_map.tsv"))
    trace = ct.load_hw_trace(fsdb, rows, logdir, HW_FIXTURE_DEPTH,
                             optional=tc.STRUCTURAL_SIGNALS)
    assert trace.n_samples == HW_FIXTURE_DEPTH
    assert sorted(trace.values) == ["blink_counter", "dut_resetn", "sample_clk"]
    # sample index == ordinal, NOT absolute time
    assert trace.values["blink_counter"] == [format(i, "08b") for i in range(HW_FIXTURE_DEPTH)]
    # aliasing: sample_clk comes from dut_clk, which shares an ident with
    # identify_sampleclock -- neither may shadow the other
    assert trace.values["sample_clk"][:4] == ["1", "0", "1", "0"]
    assert trace.meta["first_time"] == HW_FIXTURE_T0
    assert trace.meta["sample_period"] == HW_FIXTURE_STEP


@needs_verdi
def test_normalise_hw_capture_round_trips(hw_fixture):
    logdir = str(hw_fixture / "logs")
    fsdb = ft.vcd2fsdb(str(hw_fixture / "cap.vcd"), str(hw_fixture / "cap.fsdb"), logdir)
    rows = ct.read_signal_map(str(hw_fixture / "signal_map.tsv"))
    before = ct.load_hw_trace(fsdb, rows, logdir, HW_FIXTURE_DEPTH,
                              optional=tc.STRUCTURAL_SIGNALS)
    desc = ct.normalise_hw_capture(fsdb, str(hw_fixture / "hw_iice.fsdb"), rows,
                                   logdir, HW_FIXTURE_DEPTH,
                                   optional=tc.STRUCTURAL_SIGNALS)
    assert desc["n_samples"] == HW_FIXTURE_DEPTH
    assert desc["raw_time_base"] == (HW_FIXTURE_T0,
                                     HW_FIXTURE_T0 + (HW_FIXTURE_DEPTH - 1) * HW_FIXTURE_STEP)
    after = ct.load_hw_trace(str(hw_fixture / "hw_iice.fsdb"), rows, logdir,
                             HW_FIXTURE_DEPTH, optional=tc.STRUCTURAL_SIGNALS)
    assert after.values == before.values
    # emitted at the manifest hw_path names, hierarchy restored
    assert ft.list_signals(str(hw_fixture / "hw_iice.fsdb"), logdir) == {
        "rm_led/u_blink/blink_counter", "rm_led/dut_resetn", "rm_led/dut_clk"}


#: The real capture the integrator took over a demo cable.  Gitignored, so the
#: test skips when it is absent -- it must never be a required input.
_REAL_CAP = os.path.join(
    _HERE, "..", "..", "fpga", "rp", "nanosoc_iice", "build", "probe_softtap",
    "demo_cap.fsdb")


@needs_verdi
@pytest.mark.skipif(not os.path.isfile(_REAL_CAP),
                    reason="real Identify capture not present (gitignored)")
def test_against_the_real_identify_capture(tmp_path):
    """Confirms the fixture above matches reality, on the actual captured FSDB."""
    logdir = str(tmp_path / "logs")
    os.makedirs(logdir, exist_ok=True)
    t_min, t_max = ft.fsdb_time_range(_REAL_CAP, logdir)
    assert t_min == 10240 and t_max == 20480
    sigs = ft.list_signals(_REAL_CAP, logdir)
    assert sigs == {
        "rm_led/blink_counter", "rm_led/dut_clk", "rm_led/dut_resetn",
        "rm_led/identify_cycle", "rm_led/identify_sampleclock"}
    smap = tmp_path / "signal_map.tsv"
    smap.write_text("\n".join([
        "\t".join(["name", "width", "radix", "hw_path", "sim_path"]),
        "\t".join(["blink_counter", "26", "hex", "/rm_led/u_blink/blink_counter",
                   "tb.u_dut.blink_counter"]),
        "\t".join(["dut_resetn", "1", "bin", "/rm_led/dut_resetn", "tb.u_dut.dut_resetn"]),
        "\t".join(["trigger_marker", "1", "bin", "__DERIVED__", "__DERIVED__"]),
        "\t".join(["sample_clk", "1", "bin", "/rm_led/dut_clk", "tb.u_dut.dut_clk"]),
    ]) + "\n")
    rows = ct.read_signal_map(str(smap))
    info = tc.check_signal_sets({"iice/%s" % r.name for r in rows}, sigs, rows,
                                hw_optional=tc.STRUCTURAL_SIGNALS)
    assert info["identify_injected_seen"] == ["identify_cycle", "identify_sampleclock"]
    trace = ct.load_hw_trace(_REAL_CAP, rows, logdir, 1024,
                             optional=tc.STRUCTURAL_SIGNALS)
    assert trace.n_samples == 1024          # == iice sampler -depth
    assert trace.meta["sample_period"] == 10
    assert trace.widths["blink_counter"] == 26
    # no X anywhere: hardware cannot produce it
    for name, col in trace.values.items():
        assert not any(set(v) & set("xz") for v in col), name


@needs_verdi
def test_a_frozen_tail_survives_the_real_fsdb_round_trip(hw_fixture):
    """END-TO-END NEGATIVE CONTROL for the quiet-tail fix, through the real tools.

    This is the case the harness exists for -- a hardware capture that stops
    moving -- and it FAILED before 2026-07-30 with
    ``HarnessError: cannot establish the hardware sample grid``.  `fsdb2vcd`
    "stops the VCD at the last transition", so the frozen tail's timestamps are
    genuinely absent from the VCD; the FSDB's own end time is what proves the
    span.  Nothing here is mocked: real `vcd2fsdb`, real `fsdb2vcd`, real
    `fsdb2vcd -summary`.
    """
    freeze_from = 6
    d = hw_fixture
    (d / "frozen.vcd").write_text(make_identify_vcd_text(freeze_from=freeze_from))
    logdir = str(d / "logs")
    fsdb = ft.vcd2fsdb(str(d / "frozen.vcd"), str(d / "frozen.fsdb"), logdir)

    # the tool really does drop the quiet tail from the VCD it emits ...
    vcd = ft.fsdb2vcd(fsdb, str(d / "reread.vcd"), logdir)
    _, changes = ft.parse_vcd(vcd)
    assert len(changes) == freeze_from, \
        "expected fsdb2vcd to stop at the last transition; got %d timestamps" % len(changes)
    # ... but the FSDB still knows where the file ends.
    t_min, t_max = ft.fsdb_time_range(fsdb, logdir)
    assert (t_min, t_max) == (
        HW_FIXTURE_T0, HW_FIXTURE_T0 + (HW_FIXTURE_DEPTH - 1) * HW_FIXTURE_STEP)

    rows = ct.read_signal_map(str(d / "signal_map.tsv"))
    trace = ct.load_hw_trace(fsdb, rows, logdir, HW_FIXTURE_DEPTH,
                             optional=tc.STRUCTURAL_SIGNALS)
    assert trace.n_samples == HW_FIXTURE_DEPTH
    assert trace.meta["sample_period"] == HW_FIXTURE_STEP
    assert trace.meta["grid_source"] == "gcd_pinned_by_end_time"
    assert trace.meta["last_change_sample"] == freeze_from - 1
    assert trace.meta["quiet_tail_samples"] == HW_FIXTURE_DEPTH - freeze_from
    # the tail is HELD FORWARD, not padded with X and not invented
    held = format(freeze_from - 1, "08b")
    assert trace.values["blink_counter"][freeze_from - 1:] == \
        [held] * (HW_FIXTURE_DEPTH - freeze_from + 1)
    # and the report names it
    said = tc.describe_hw_stall(trace.meta)
    assert said and "stalled or wedged" in said["headline"]


@needs_verdi
def test_a_truncated_fsdb_is_STILL_a_harness_error(hw_fixture):
    """The other half of the fix: "fewer timestamps because something is broken"
    must stay exit 2.  Here the file itself ends early -- the samples after it
    were never written -- so its end time cannot prove a full-depth span and the
    loader must refuse rather than hold values forward over data it never saw."""
    d = hw_fixture
    short = make_identify_vcd_text(depth=6)          # only 6 of HW_FIXTURE_DEPTH
    (d / "short.vcd").write_text(short)
    logdir = str(d / "logs")
    fsdb = ft.vcd2fsdb(str(d / "short.vcd"), str(d / "short.fsdb"), logdir)
    rows = ct.read_signal_map(str(d / "signal_map.tsv"))
    with pytest.raises(ft.HarnessError) as e:
        ct.load_hw_trace(fsdb, rows, logdir, HW_FIXTURE_DEPTH,
                         optional=tc.STRUCTURAL_SIGNALS)
    assert "cannot establish the hardware sample grid" in str(e.value)
    assert "does NOT fit" in str(e.value)


@needs_verdi
@pytest.mark.skipif(not os.path.isfile(_REAL_CAP),
                    reason="real Identify capture not present (gitignored)")
def test_identify_sampleclock_changes_at_EVERY_sample_of_the_real_capture():
    """MEASURED 2026-07-30, previously speculation: a genuine Identify capture
    cannot present a quiet tail.

    ``identify_sampleclock`` (aliased onto the same VCD identifier as the probed
    ``dut_clk``) toggles once per sample, so every one of the ``depth`` sample
    timestamps carries at least that one value change and `fsdb2vcd` always
    returns exactly ``depth`` timestamps.  Consequence for reading a report: a
    quiet tail points at a synthetic or re-emitted FSDB, or at a capture whose
    sample clock is missing -- NOT, on this evidence, at a real `write fsdb` of a
    wedged DUT.  The quiet-tail path is still required, because every FSDB this
    harness writes itself omits the injected clock.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as logdir:
        vcd = ft.fsdb2vcd(_REAL_CAP, os.path.join(logdir, "real.vcd"), logdir)
        variables, changes = ft.parse_vcd(vcd)
        assert len(changes) == 1024                  # == iice sampler -depth
        clk_idents = {v.ident for v in variables
                      if v.name in ("identify_sampleclock", "dut_clk")}
        assert len(clk_idents) == 1, "the alias of INTERFACES.md §7 fact 5 is gone"
        ident = clk_idents.pop()
        quiet = [t for t, evs in changes
                 if not any(i == ident for i, _ in evs)]
        assert quiet == [], \
            "%d sample timestamps carry no sample-clock change" % len(quiet)


@needs_verdi
def test_unreadable_fsdb_is_a_harness_error(fixture_dir):
    logdir = str(fixture_dir / "logs")
    junk = fixture_dir / "junk.fsdb"
    junk.write_text("not an fsdb at all\n")
    with pytest.raises(ft.HarnessError):
        ft.list_signals(str(junk), logdir)


# ==========================================================================
# 16. compare.sh wiring (no Verdi needed: we only check the gate and usage)
# ==========================================================================

def test_compare_sh_rejects_an_unknown_subcommand():
    p = subprocess.run(["bash", os.path.join(_HERE, "compare.sh"), "wat"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert p.returncode == 2
    assert b"usage" in p.stdout


def test_compare_sh_hw_fsdb_is_gated():
    """The board target must refuse to run without an explicit opt-in."""
    env = dict(os.environ)
    env.pop("IICE_ALLOW_HW", None)
    p = subprocess.run(["bash", os.path.join(_HERE, "compare.sh"), "hw-fsdb"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    assert p.returncode == 2
    assert b"IICE_ALLOW_HW=1" in p.stdout


def test_compare_sh_reports_missing_inputs_as_harness_error(tmp_path):
    env = dict(os.environ)
    env["BUILD"] = str(tmp_path / "empty_build")
    env["HERE"] = _HERE
    env["MANIFEST"] = str(tmp_path / "no_manifest.yaml")
    for sub in ("fake-hw", "compare", "negctl"):
        p = subprocess.run(["bash", os.path.join(_HERE, "compare.sh"), sub],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        assert p.returncode == 2, (sub, p.stdout)
        assert b"HARNESS ERROR" in p.stdout, (sub, p.stdout)


def test_makefile_compare_does_not_define_clean():
    """The parent Makefile owns `clean`; a second definition would silently
    override one of them (INTERFACES.md §5)."""
    text = open(os.path.join(_HERE, "Makefile.compare")).read()
    for line in text.splitlines():
        assert not line.startswith("clean:"), line
    assert "clean-compare:" in text


def test_makefile_compare_targets_are_not_error_suppressed():
    """A `-` prefix or a trailing `;` on the compare recipe is exactly how a
    failing gate ends up reporting success."""
    text = open(os.path.join(_HERE, "Makefile.compare")).read()
    for target in ("fake-hw:", "compare:", "negctl:", "hw-fsdb:"):
        i = text.index(target)
        recipe = text[i:].split("\n")[1]
        assert not recipe.lstrip("\t").startswith("-"), target


# ===========================================================================
# The SILICON capture shape (measured 2026-07-30 on mps3_01)
# ===========================================================================
# The harness was built on "one timestamp per sample", which is what the
# demo-cable capture really does.  A capture off the REAL BOARD does not: the
# sample clock TOGGLES, so a 923-sample capture arrived as 1846 timestamps and
# the ordinal loader refused it (exit 2) on its `len(ts) > n_samples` guard.
# These tests pin the toggling shape, and pin that the guard still fires when
# there is no clock to measure -- the hole this fix must not open.

def make_toggling_identify_vcd_text(n_samples=8, t0=HW_FIXTURE_T0,
                                    step=HW_FIXTURE_STEP):
    """Identify capture with a TOGGLING sample clock: 2 timestamps per sample.

    Rising edge carries the sample (data changes there); the falling edge in
    between carries nothing but the clock.  That is the measured silicon shape.
    """
    L = [
        "$date\n\t\n$end",
        "$version\n\tHAPS_PRODUCT_HAPS-KEY-70\n$end",
        "$timescale\n\t1ns\n$end",
        "$scope module rm_led $end",
        '$var wire 1 ! identify_sampleclock $end',
        '$var wire 1 " identify_cycle $end',
        "$var reg 1 ! dut_clk $end",          # SAME ident, as on silicon
        "$var reg 8 # blink_counter [7:0] $end",
        "$var reg 1 $ dut_resetn $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    prev = {}
    for i in range(n_samples):
        for phase in (1, 0):                  # rising sample, then falling
            t = t0 + (2 * i + (0 if phase else 1)) * step
            vals = {"!": "1" if phase else "0"}
            if phase:                          # data moves only on the sample
                vals["#"] = format(i, "08b")
                vals["$"] = "1" if i >= 2 else "0"
            L.append("#%d" % t)
            if i == 0 and phase:
                L.append("$dumpvars")
            for ident in ("!", "#", "$"):
                if ident in vals and vals[ident] != prev.get(ident):
                    L.append(("b%s %s" % (vals[ident], ident)) if ident == "#"
                             else "%s%s" % (vals[ident], ident))
                    prev[ident] = vals[ident]
            if i == 0 and phase:
                L.append("$end")
    return "\n".join(L) + "\n"


HW_LEAF_TO_NAME = {
    "blink_counter": "blink_counter",
    "dut_resetn": "dut_resetn",
    "dut_clk": "sample_clk",
}


def _parse_text(tmp_path, text, name="cap.vcd"):
    p = tmp_path / name
    p.write_text(text)
    return ft.parse_vcd(str(p))


def test_toggling_sample_clock_capture_loads_via_MEASURED_clock_edges(tmp_path):
    """The silicon shape: 8 samples arrive as 16 timestamps and must still load.

    FAILS BEFORE THE FIX: the ordinal loader is the only hardware loader, and
    16 timestamps against depth 8 trips its `len(ts) > n_samples` guard.
    """
    variables, changes = _parse_text(tmp_path, make_toggling_identify_vcd_text(8))
    assert len({t for t, _ in changes}) == 16, "fixture must be 2 tags per sample"
    clk = ct._find_clock_ident(variables)
    assert clk is not None and clk[1] == "identify_sampleclock"
    trace = ct.samples_from_sample_clock(
        variables, changes, HW_LEAF_TO_NAME, clk[0], clk[1], edge="positive")
    assert trace.n_samples == 8
    assert trace.meta["source"] == "hw_sample_clock_edges"
    assert "measured" in trace.meta["grid_source"]
    # values sampled on the rising edge, in order
    assert trace.values["blink_counter"] == [format(i, "08b") for i in range(8)]
    assert trace.values["dut_resetn"] == ["0", "0"] + ["1"] * 6
    # one sample per RISING edge only -- the falling edges are not samples
    assert trace.meta["sample_period"] == 2 * HW_FIXTURE_STEP


def test_more_timestamps_than_samples_with_NO_clock_is_STILL_a_harness_error(tmp_path):
    """ANTI-WEAKENING: the ordinal guard must still fire when there is no clock.

    The new path is licensed by MEASURING sample-clock edges.  With no clock in
    the trace there is nothing to measure, so an over-long trace must remain a
    harness error rather than fall back to guessing.
    """
    text = make_toggling_identify_vcd_text(8).replace(
        '$var wire 1 ! identify_sampleclock $end', ""
    ).replace("$var reg 1 ! dut_clk $end", "")
    variables, changes = _parse_text(tmp_path, text, "noclk.vcd")
    assert ct._find_clock_ident(variables) is None
    with pytest.raises(ct.HarnessError) as e:
        ct.samples_from_timestamp_ordinals(
            variables, changes, {"blink_counter": "blink_counter"}, 8, by_leaf=True)
    assert "timestamps but the manifest depth" in str(e.value)


def test_one_tag_per_sample_captures_STILL_use_the_ordinal_loader(tmp_path):
    """NO REGRESSION: the demo-cable shape keeps the old, measured-correct path."""
    variables, changes = _parse_text(
        tmp_path, make_identify_vcd_text(), "onetag.vcd")
    assert len({t for t, _ in changes}) == HW_FIXTURE_DEPTH
    trace = ct.samples_from_timestamp_ordinals(
        variables, changes, HW_LEAF_TO_NAME, HW_FIXTURE_DEPTH, by_leaf=True)
    assert trace.meta["source"] == "timestamp_ordinals"
    assert trace.n_samples == HW_FIXTURE_DEPTH


def test_sample_count_mismatch_TRIAGE_names_the_capture_shortfall():
    """A short hardware capture is a routine hardware fact -- say so.

    Measured: a depth-1024 IICE returned 923 samples and the debugger printed
    'Range 0 1023 exceeds maximum 922'.  The old message gave only the two
    numbers, which reads like a harness bug.
    """
    sim = ct.SampleTrace({"a": 1}, {"a": ["0"] * 1024}, {"source": "clock_edges"})
    hw = ct.SampleTrace({"a": 1}, {"a": ["0"] * 923},
                        {"source": "hw_sample_clock_edges",
                         "grid_source": "sample-clock edges (measured, not inferred)"})
    with pytest.raises(tc.HarnessError) as e:
        tc.compare_traces(sim, hw, names=["a"])
    msg = str(e.value)
    assert "exceeds maximum" in msg           # the debugger's own wording
    assert "HARDWARE side is SHORTER" in msg
    assert "MISALIGNS" in msg                 # warns against naive truncation
    assert "sample-clock edges" in msg        # echoes how the grid was made


@needs_verdi
def test_load_hw_trace_SWITCHES_to_clock_edges_on_a_toggling_capture(tmp_path):
    """End-to-end through the real FSDB tools: the selector does its job.

    FAILS BEFORE THE FIX (HarnessError: 16 timestamps vs depth 8).
    """
    d = tmp_path / "tog"
    d.mkdir()
    (d / "signal_map.tsv").write_text(HW_SIGNAL_MAP_TSV)
    (d / "cap.vcd").write_text(make_toggling_identify_vcd_text(8))
    logdir = str(d / "logs")
    fsdb = ft.vcd2fsdb(str(d / "cap.vcd"), str(d / "cap.fsdb"), logdir)
    rows = ct.read_signal_map(str(d / "signal_map.tsv"))
    trace = ct.load_hw_trace(fsdb, rows, logdir, 8, optional=tc.STRUCTURAL_SIGNALS)
    assert trace.meta["source"] == "hw_sample_clock_edges"
    assert trace.n_samples == 8
    assert trace.values["blink_counter"] == [format(i, "08b") for i in range(8)]


def teardown_module(module):
    for p in _TMP_FILES:
        try:
            os.remove(p)
        except OSError:
            pass
