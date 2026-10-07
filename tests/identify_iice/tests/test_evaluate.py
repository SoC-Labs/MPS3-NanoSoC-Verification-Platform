#!/usr/bin/env python3
"""Unit tests for the `make evaluate` workflow -- NO VCS, NO Verdi, NO licence.

What is worth testing here is not "does it run" (that is what
``make evaluate`` itself does) but the two claims the workflow rests on:

1. **each injector produces the divergence SHAPE it advertises** -- a stuck bit
   really is held, a skew really is a one-sample delay, a late divergence really
   is clean-then-broken-for-good;
2. **the three shapes are DISTINGUISHABLE from each other** by
   :func:`inject_divergence.classify` alone, with no knowledge of what was
   injected.  If they were not, the "what class of bug is this" reading would be
   decoration -- it would be reciting the mode name back at you.

Everything is hand-built ``SampleTrace`` data, so it runs on a box with no
simulator.  The handful of tool-dependent assertions carry an explicit
``skipif``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_HERE = os.path.dirname(_TESTS)
for _p in (_HERE, os.path.join(_HERE, "ncompare")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import crop_trace as ct  # noqa: E402
import fsdb_tools as ft  # noqa: E402
import inject_divergence as idv  # noqa: E402


# ==========================================================================
# Tool probe -- presence is not enough, the login-PATH Verdi exits 127
# ==========================================================================

def _verdi_usable():
    if not ft.verdi_available():
        return False
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="iice_eval_probe_")
    try:
        vcd = os.path.join(d, "p.vcd")
        ft.write_vcd(vcd, [("s/a", 1)], [{"s/a": "0"}, {"s/a": "1"}])
        ft.vcd2fsdb(vcd, os.path.join(d, "p.fsdb"), d)
        return True
    except ft.HarnessError:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


VERDI = _verdi_usable()
needs_verdi = pytest.mark.skipif(
    not VERDI,
    reason="Verdi FSDB utilities unusable here (module load verdi/T-2022.06-SP2 "
           "or X-2025.06-SP2, and unset NOVAS_HOME)",
)
needs_fsdbjoin = pytest.mark.skipif(
    not (VERDI and ft.have_tool("fsdbjoin")),
    reason="fsdbjoin not usable here",
)


# ==========================================================================
# Fixtures: a tiny window, entirely hand-written
# ==========================================================================

# 24, not 16: the periodicity detector demands TWO full repetitions before it
# will claim a loop, and `late-divergence`'s default departure point is
# `trigger + depth/8`.  A window too short for two repetitions is a real
# limitation of the detector (documented in EVALUATING.md), not something to
# paper over -- but it must not be what this fixture is testing.
N = 24
#: sample 0/1 are X on `addr`, so the one-way X mask is exercised for real.
ADDR = ["xxxxxxxx", "xxxxxxxx"] + [format(4 * i, "08b") for i in range(2, N)]
FLAG = [("1" if i % 2 else "0") for i in range(N)]
STATE = [format(i % 4, "02b") for i in range(N)]
NAMES = ["addr", "flag", "state"]


@pytest.fixture()
def sim():
    return ct.SampleTrace(
        {"addr": 8, "flag": 1, "state": 2},
        {"addr": list(ADDR), "flag": list(FLAG), "state": list(STATE)},
    )


@pytest.fixture()
def hw(sim):
    """The clean "hardware" side: X quantised away, nothing else changed."""
    import synth_hw_fsdb as sh
    out, _n = sh.quantise_trace(sim, "prng", "iice")
    return out


def _base(sim, hw, depth=N, trigger_index=8):
    """The dict shape `apply_mode` consumes, without any FSDB in sight."""
    return {
        "hw_trace": hw,
        "sim_data": sim,
        "names": list(NAMES),
        "trigger_index": trigger_index,
        "window_info": {"window_start_sample": 0, "window_end_sample": depth - 1},
    }


def _signature(shape):
    """The coarse fingerprint a human reads off the interpretation."""
    return (
        bool(shape["stuck_bits"]),
        any(v.get("hw_lags_sim_by_1") == 1.0 and (v.get("aligned") or 0) < 1.0
            for v in shape["lag"].values()),
        shape["frozen_from"] is not None,
        shape["hw_period"],
    )


# ==========================================================================
# The X rule: an injector must never put an X on the hardware side
# ==========================================================================

def test_set_bit_refuses_to_write_an_x():
    with pytest.raises(ft.HarnessError) as exc:
        idv._set_bit("0000", 1, "x")
    assert "one-way" in str(exc.value)


@pytest.mark.parametrize("mode", [m for m in idv.MODES if m != "clean"])
def test_no_mode_ever_injects_an_x(sim, hw, mode):
    """Hardware cannot produce X; an injected X would exercise negctl NC4."""
    idv.apply_mode(mode, _base(sim, hw))
    for name in NAMES:
        for bits in hw.values[name]:
            assert not (set(bits) & set("xz")), "%s got an X: %r" % (name, bits)


def test_clean_mode_changes_nothing(sim, hw):
    before = {k: list(v) for k, v in hw.values.items()}
    record = idv.apply_mode("clean", _base(sim, hw))
    assert record["mode"] == "clean"
    assert hw.values == before
    expect = idv.predict(sim, hw, NAMES)
    assert expect["mismatch_samples"] == 0
    assert expect["first_diverging_sample"] is None


# ==========================================================================
# stuck
# ==========================================================================

def test_inject_stuck_holds_the_bit_only_from_the_given_sample(sim, hw):
    before = list(hw.values["addr"])
    rec = idv.inject_stuck(hw, "addr", 2, "0", 8)
    assert rec["mode"] == "stuck"
    for i in range(8):
        assert hw.values["addr"][i] == before[i], "sample %d must be untouched" % i
    for i in range(8, N):
        assert idv._bit(hw.values["addr"][i], 2) == "0"
        # every OTHER bit is untouched -- a stuck net is one bit, not a bus
        for other in range(8):
            if other != 2:
                assert idv._bit(hw.values["addr"][i], other) == \
                       idv._bit(before[i], other)


def test_stuck_shows_up_as_a_stuck_bit_and_nothing_else(sim, hw):
    idv.inject_stuck(hw, "addr", 2, "0", 8)
    shape = idv.classify(sim, hw, NAMES)
    assert shape["first_diverging_sample"] is not None
    assert shape["diverging_signals"] == ["addr"]
    assert shape["stuck_bits"]["addr"] == [{"bit": 2, "level": "0"}]
    assert shape["frozen_from"] is None
    assert shape["hw_period"] is None


def test_choose_stuck_target_skips_a_bit_that_never_toggles(sim, hw):
    # addr bits 0 and 1 are always 0 in the fixture (addresses are multiples of
    # 4), so a stuck-at-0 there would be invisible.  bit 2 is the first useful.
    name, bit, level = idv.choose_stuck_target(sim, NAMES, 8)
    assert (name, bit, level) == ("addr", 2, "0")


def test_choose_stuck_target_skips_x_and_is_deterministic(sim):
    # Over the WHOLE window addr[0..1] are X, so addr must be skipped entirely
    # and the choice must fall through to another signal.
    name, _bit, _level = idv.choose_stuck_target(sim, NAMES, 0)
    assert name != "addr"
    assert idv.choose_stuck_target(sim, NAMES, 0) == (name, _bit, _level)


def test_choose_stuck_target_refuses_when_nothing_would_be_visible():
    flat = ct.SampleTrace({"a": 2}, {"a": ["00"] * 8})
    with pytest.raises(ft.HarnessError) as exc:
        idv.choose_stuck_target(flat, ["a"], 0)
    assert "prove nothing" in str(exc.value)


# ==========================================================================
# skew
# ==========================================================================

def test_inject_skew_is_exactly_a_one_sample_delay(sim, hw):
    before = list(hw.values["state"])
    rec = idv.inject_skew(hw, "state", lag=1, from_sample=1)
    assert rec["mode"] == "skew"
    assert hw.values["state"][0] == before[0]
    for i in range(1, N):
        assert hw.values["state"][i] == before[i - 1]


def test_skew_shows_up_as_a_lag_and_not_as_a_stuck_bit(sim, hw):
    idv.inject_skew(hw, "state", lag=1, from_sample=1)
    shape = idv.classify(sim, hw, NAMES)
    assert shape["diverging_signals"] == ["state"]
    lag = shape["lag"]["state"]
    assert lag["hw_lags_sim_by_1"] == 1.0, "hw[i] == sim[i-1] must hold everywhere"
    assert lag["aligned"] < 1.0, "and hw[i] == sim[i] must NOT"
    assert shape["frozen_from"] is None


def test_skew_target_defaults_to_the_signal_that_moves_most(sim):
    # `flag` and `state` both change every sample in the fixture; ties break
    # alphabetically so the choice is reproducible.
    assert idv.choose_skew_signal(sim, NAMES) == "flag"


def test_skew_refuses_a_lag_of_zero(sim, hw):
    with pytest.raises(ft.HarnessError):
        idv.inject_skew(hw, "state", lag=0)


# ==========================================================================
# late-divergence
# ==========================================================================

def test_late_divergence_loop_repeats_the_preceding_samples(sim, hw):
    before = {k: list(v) for k, v in hw.values.items()}
    rec = idv.inject_late_divergence(hw, 8, NAMES, shape="loop", period=4)
    assert rec["shape"] == "loop"
    for name in NAMES:
        for i in range(8):
            assert hw.values[name][i] == before[name][i]
        for i in range(8, N):
            assert hw.values[name][i] == before[name][4 + ((i - 8) % 4)]


def test_late_divergence_loop_is_read_as_a_repeating_pattern(sim, hw):
    idv.inject_late_divergence(hw, 8, NAMES, shape="loop", period=4)
    shape = idv.classify(sim, hw, NAMES)
    assert shape["hw_period"] == 4
    assert shape["hw_period_from"] == shape["first_diverging_sample"]
    assert shape["persistent"] is True
    assert shape["frozen_from"] is None


def test_a_looping_tail_can_ALSO_look_bit_stuck_and_the_loop_wins(sim, hw):
    """A real ambiguity, resolved in the reading rather than hidden.

    Inside a repeating tail some individual bits are inevitably constant, so the
    stuck-bit detector fires too.  The evidence dict keeps both -- but the
    reading must not offer "a stuck net" as the explanation when the whole probe
    set is periodic, because a periodic tail explains those constant bits.
    """
    idv.inject_late_divergence(hw, 8, NAMES, shape="loop", period=4)
    shape = idv.classify(sim, hw, NAMES)
    assert shape["stuck_bits"], "the fixture is meant to be ambiguous"
    text = idv.interpretation_text(shape, {"mode": "late-divergence"}, None)
    assert "GOING ROUND" in text
    assert "A STUCK NET" not in text


def test_late_divergence_freeze_is_read_as_a_stall(sim, hw):
    idv.inject_late_divergence(hw, 8, NAMES, shape="freeze")
    shape = idv.classify(sim, hw, NAMES)
    assert shape["frozen_from"] == 8
    assert shape["frozen_samples"] == N - 8
    assert shape["persistent"] is True


def test_late_divergence_is_clean_before_the_departure(sim, hw):
    idv.inject_late_divergence(hw, 10, NAMES, shape="loop", period=4)
    expect = idv.predict(sim, hw, NAMES)
    assert expect["first_diverging_sample"] >= 10, \
        "nothing may diverge before the injected departure point"


def test_late_divergence_refuses_sample_zero(sim, hw):
    with pytest.raises(ft.HarnessError):
        idv.inject_late_divergence(hw, 0, NAMES)


def test_late_divergence_refuses_a_loop_over_an_unchanging_body():
    flat = ct.SampleTrace({"a": 2}, {"a": ["00"] * 4 + ["01", "10", "11", "00"]})
    with pytest.raises(ft.HarnessError) as exc:
        idv.inject_late_divergence(flat, 4, ["a"], shape="loop", period=4)
    assert "prove nothing" in str(exc.value)


# ==========================================================================
# THE POINT: the three shapes must be distinguishable from each other
# ==========================================================================

def test_the_three_modes_have_pairwise_DISTINCT_signatures(sim):
    import synth_hw_fsdb as sh

    sigs = {}
    for mode in ("stuck", "skew", "late-divergence"):
        side, _n = sh.quantise_trace(sim, "prng", "iice")
        idv.apply_mode(mode, _base(sim, side))
        sigs[mode] = _signature(idv.classify(sim, side, NAMES))
    assert len(set(sigs.values())) == 3, (
        "two modes classify identically, so the 'class of bug' reading is "
        "decoration: %r" % (sigs,)
    )


@pytest.mark.parametrize("mode,needle", [
    ("stuck", "A STUCK NET"),
    ("skew", "SAMPLING-PHASE ERROR"),
    ("late-divergence", "GOING ROUND"),
    ("clean", "NO DIVERGENCE"),
])
def test_the_reading_names_the_right_class(sim, mode, needle):
    import synth_hw_fsdb as sh
    side, _n = sh.quantise_trace(sim, "prng", "iice")
    rec = idv.apply_mode(mode, _base(sim, side))
    shape = idv.classify(sim, side, NAMES)
    text = idv.interpretation_text(
        shape, {"mode": mode, "trigger_window_index": 8}, rec)
    assert needle in text


@pytest.mark.parametrize("mode", list(idv.MODES))
def test_every_reading_states_what_it_CANNOT_tell_you(sim, mode):
    """The section is not optional: a reading with no caveats is an overclaim."""
    import synth_hw_fsdb as sh
    side, _n = sh.quantise_trace(sim, "prng", "iice")
    rec = idv.apply_mode(mode, _base(sim, side))
    shape = idv.classify(sim, side, NAMES)
    text = idv.interpretation_text(
        shape, {"mode": mode, "trigger_window_index": 8}, rec)
    assert "what this CANNOT tell you" in text
    body = text.split("what this CANNOT tell you")[1]
    assert body.count("  * ") >= 1
    assert "X-masked" in body


def test_the_reading_does_not_invent_a_cause_for_an_unshaped_divergence(sim, hw):
    # A single flipped bit at one sample: no stuck bit, no lag, no stall.
    hw.values["addr"][9] = idv._set_bit(hw.values["addr"][9], 5, "1")
    hw.values["addr"][9] = idv._set_bit(hw.values["addr"][9], 4, "1")
    shape = idv.classify(sim, hw, NAMES)
    text = idv.interpretation_text(shape, {"mode": "manual"}, None)
    assert "no clean signature" in text
    for forbidden in ("A STUCK NET", "SAMPLING-PHASE ERROR", "A STALL",
                      "GOING ROUND"):
        assert forbidden not in text


def test_the_reading_reports_a_high_masked_fraction_as_weak():
    mostly_x = ct.SampleTrace({"a": 4}, {"a": ["xxxx"] * 7 + ["0000"]})
    side = ct.SampleTrace({"a": 4}, {"a": ["0000"] * 7 + ["1111"]})
    shape = idv.classify(mostly_x, side, ["a"])
    text = idv.interpretation_text(shape, {"mode": "x"}, None)
    assert "HIGH" in text and "not really compared" in text


# ==========================================================================
# The mismatch overlay (the signal you actually put a cursor on)
# ==========================================================================

def test_mismatch_overlay_marks_exactly_the_diverging_samples(sim, hw):
    idv.inject_stuck(hw, "addr", 2, "0", 8)
    overlay = idv.mismatch_overlay(sim, hw, NAMES)
    assert overlay.n_samples == N
    expect = idv.predict(sim, hw, NAMES)
    assert overlay.values["any_neq"].count("1") > 0
    first = overlay.values["any_neq"].index("1")
    assert first == expect["first_diverging_sample"]
    assert overlay.values["flag_neq"] == ["0"] * N
    assert overlay.values["addr_neq"].count("1") == \
        expect["per_signal_mismatch"]["addr"]


def test_mismatch_overlay_honours_the_one_way_x_mask():
    golden = ct.SampleTrace({"a": 2}, {"a": ["xx", "00"]})
    side = ct.SampleTrace({"a": 2}, {"a": ["11", "11"]})
    overlay = idv.mismatch_overlay(golden, side, ["a"])
    assert overlay.values["a_neq"] == ["0", "1"], \
        "a sim-X sample must not be marked; a real difference must be"


# ==========================================================================
# Report parsing and the injection-vs-report cross-check
# ==========================================================================

REPORT_MISMATCH = """\
==============================================================================
IICE sim-vs-hardware trace comparison report
==============================================================================

VERDICT: MISMATCH   (exit code 1)

-- totals ------------------------------------------------------------
  MISMATCH COUNT (signal-samples)        19
  X-masked samples (sim X vs hw 0/1)     2   (0.45% of samples compared)
  mismatches caused by hardware X/Z      0   (hardware must never emit X)
  FIRST DIVERGING SAMPLE                 33
"""

REPORT_MATCH = REPORT_MISMATCH.replace("MISMATCH   (exit code 1)",
                                       "MATCH   (exit code 0)") \
    .replace("(signal-samples)        19", "(signal-samples)        0") \
    .replace("FIRST DIVERGING SAMPLE                 33",
             "FIRST DIVERGING SAMPLE                 (none)")


def test_parse_report_reads_the_mandated_numbers(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text(REPORT_MISMATCH)
    got = idv.parse_report(str(p))
    assert got["verdict"] == "MISMATCH"
    assert got["mismatch_samples"] == 19
    assert got["first_diverging_sample"] == 33
    assert got["masked_samples"] == 2
    assert got["hw_x_samples"] == 0


def test_parse_report_reads_none_as_none(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text(REPORT_MATCH)
    got = idv.parse_report(str(p))
    assert got["verdict"] == "MATCH"
    assert got["first_diverging_sample"] is None


def test_parse_report_rejects_something_that_is_not_a_report(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text("hello\n")
    with pytest.raises(ft.HarnessError):
        idv.parse_report(str(p))


def test_cross_check_passes_when_the_report_agrees(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text(REPORT_MISMATCH)
    record = {"expect": {"mismatch_samples": 19, "first_diverging_sample": 33}}
    assert idv.check_report_against_injection(
        record, idv.parse_report(str(p))) == []


def test_cross_check_FAILS_when_the_report_lost_the_divergence(tmp_path):
    """The load-bearing assertion: a report that dropped the injected bug."""
    p = tmp_path / "r.txt"
    p.write_text(REPORT_MATCH)
    record = {"expect": {"mismatch_samples": 19, "first_diverging_sample": 33}}
    problems = idv.check_report_against_injection(record, idv.parse_report(str(p)))
    assert problems
    assert any("verdict" in x for x in problems)


def test_cross_check_FAILS_on_a_shifted_first_divergence(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text(REPORT_MISMATCH.replace("33", "34"))
    record = {"expect": {"mismatch_samples": 19, "first_diverging_sample": 33}}
    problems = idv.check_report_against_injection(record, idv.parse_report(str(p)))
    assert any("first_diverging_sample" in x for x in problems)


# ==========================================================================
# The viewing artifacts
# ==========================================================================

def test_virtual_fsdb_has_the_vendor_documented_shape(tmp_path):
    a, b = tmp_path / "a.fsdb", tmp_path / "b.fsdb"
    a.write_text("x")
    b.write_text("y")
    vf = idv.write_virtual_fsdb(str(tmp_path / "v.vf"), [str(a), str(b)])
    text = open(vf).read()
    assert text.startswith("@FSDB rc file Version 1.0")
    assert "FileType = stitch" in text
    assert "File1 = %s" % a in text
    assert "File2 = %s" % b in text


def test_virtual_fsdb_refuses_a_single_member(tmp_path):
    with pytest.raises(ft.HarnessError):
        idv.write_virtual_fsdb(str(tmp_path / "v.vf"), ["only.fsdb"])


@needs_verdi
def test_emit_side_round_trips_through_a_real_fsdb(tmp_path, sim):
    logdir = str(tmp_path / "log")
    out = idv._emit_side(sim, "sim", str(tmp_path / "s.fsdb"), logdir, "t")
    assert os.path.getsize(out) > 0
    leaves = ft.list_signals(out, logdir)
    assert leaves == {"sim/addr", "sim/flag", "sim/state"}


@needs_fsdbjoin
def test_joined_fsdb_contains_and_returns_its_members(tmp_path, sim, hw):
    logdir = str(tmp_path / "log")
    a = idv._emit_side(sim, "sim", str(tmp_path / "a.fsdb"), logdir, "a")
    b = idv._emit_side(hw, "hw", str(tmp_path / "b.fsdb"), logdir, "b")
    vf = idv.write_virtual_fsdb(str(tmp_path / "v.vf"), [a, b])
    info = idv.join_fsdb(str(tmp_path / "j.jf"), vf, logdir)
    # the .vf itself counts as a contained file, hence members + 1
    assert info["contained_files"] == 3
    assert idv.verify_joined(str(tmp_path / "j.jf"), 1, logdir) == \
        ["sim/addr", "sim/flag", "sim/state"]


# ==========================================================================
# Anti-drift: the mode list is written in three places and they must agree
# ==========================================================================

def test_mode_list_is_the_same_in_python_makefile_and_shell():
    mk = open(os.path.join(_HERE, "Makefile.evaluate")).read()
    sh = open(os.path.join(_HERE, "evaluate.sh")).read()
    mk_modes = re.search(r"^EVAL_MODES\s*:=\s*(.+)$", mk, re.M).group(1).split()
    sh_modes = re.search(r'^MODES="([^"]+)"', sh, re.M).group(1).split()
    assert sorted(mk_modes) == sorted(idv.MODES)
    assert sorted(sh_modes) == sorted(idv.MODES)
    assert sorted(idv.MODE_BLURB) == sorted(idv.MODES)


def test_evaluate_sh_rejects_an_unknown_mode():
    """Needs no tools: the mode check happens before anything is touched."""
    proc = subprocess.run(
        ["bash", os.path.join(_HERE, "evaluate.sh"), "not-a-mode"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert proc.returncode == 2, "an unknown mode must be a harness error"
    assert b"unknown MODE" in proc.stdout


def test_evaluate_sh_with_no_argument_is_a_harness_error():
    proc = subprocess.run(
        ["bash", os.path.join(_HERE, "evaluate.sh")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert proc.returncode == 2
    assert b"usage" in proc.stdout


def test_inject_cli_rejects_an_unknown_mode():
    proc = subprocess.run(
        [sys.executable, os.path.join(_HERE, "inject_divergence.py"), "inject",
         "--mode", "nonsense", "--sim-fsdb", "x", "--signal-map", "x",
         "--manifest", "x", "--out", "x", "--logdir", "x"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert proc.returncode != 0
    assert b"invalid choice" in proc.stdout


def test_injection_record_is_json_serialisable(sim, hw, tmp_path):
    """evaluate.sh reads this back; a non-serialisable field breaks the run."""
    rec = idv.apply_mode("stuck", _base(sim, hw))
    record = {
        "mode": "stuck",
        "injection": rec,
        "expect": idv.predict(sim, hw, NAMES),
        "shape": idv.classify(sim, hw, NAMES),
    }
    p = tmp_path / "injection.json"
    p.write_text(json.dumps(record, indent=1, sort_keys=True))
    back = json.loads(p.read_text())
    assert back["injection"]["signal"] == rec["signal"]
    assert back["expect"]["mismatch_samples"] == \
        record["expect"]["mismatch_samples"]
