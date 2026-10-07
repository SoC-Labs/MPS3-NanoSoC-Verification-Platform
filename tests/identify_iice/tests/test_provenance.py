#!/usr/bin/env python3
"""Provenance unit tests -- artifact identity, and the REFUSALS built on it.

Contract: INTERFACES.md §4 (frozen exit codes) and its 2026-07-31 amendment.
Design and the three incidents that motivated it: ``provenance.py``'s docstring.

WHAT THESE TESTS ARE FOR
------------------------
The failure class this file gates is **mismatched artifacts producing confident
wrong output**.  Every test below is therefore one of exactly three shapes, and
the file is worthless without all three:

* a REFUSAL that fires -- and provably does not fire before the fix.  Each is
  marked ``# PRE-FIX:`` with what the old code did instead.
* an ANTI-WEAKENING assertion -- a *matching* pair, or an artifact with NO
  provenance at all, must still compare cleanly.  Absence of a stamp is not a
  mismatch; get that backwards and the whole existing corpus stops working.
* a MESSAGE assertion -- exit 2 alone is satisfied by any refusal for any
  reason.  The message has to name the discrepancy, or the reader goes hunting
  for a value bug that is not there (the 2026-07-30 lesson recorded in
  INTERFACES.md §7 fact 9).

Everything except the handful of ``@needs_verdi`` tests runs with no simulator,
no Verdi and no licence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_HERE = os.path.dirname(_TESTS)
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_HERE, os.path.join(_HERE, "ncompare")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import crop_trace as ct  # noqa: E402
import fsdb_tools as ft  # noqa: E402
import gen_mangle as gm  # noqa: E402
import manifest as mf  # noqa: E402
import provenance as P  # noqa: E402
import synth_hw_fsdb as sh  # noqa: E402
import trace_compare as tc  # noqa: E402


def _verdi_usable():
    """Same probe as tests/test_compare.py: presence is not enough.

    The Verdi on the bare login PATH has a broken ``.wrapper`` and every utility
    exits 127, so these tests skip rather than fail against a toolchain that
    cannot run.
    """
    if not ft.verdi_available():
        return False
    import tempfile
    d = tempfile.mkdtemp(prefix="iice_prov_probe_")
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
    not VERDI, reason="Verdi FSDB utilities unusable here")

SHIPPED = ("signals_selftest.yaml", "signals_nanosoc.yaml",
           "signals_nanosoc_asbuilt.yaml")

#: The Identify instrumentation log this tree's RM was built with.  Gitignored
#: build output, so every test that reads it skips when it is absent -- it must
#: never be a required input (a fresh clone has no build).
REAL_LOG = os.path.join(_ROOT, P.IDENTIFY_LOG_DEFAULT)
has_real_log = pytest.mark.skipif(
    not os.path.isfile(REAL_LOG),
    reason="no instrumented build (fpga/rp/nanosoc_iice/build/rev_1_identify)")


# ==========================================================================
# Fixtures: a tiny manifest + its signal map, built by hand
# ==========================================================================

DEPTH = 8
IICE_NAME = "IICE_PROVTEST"       # deliberately NOT IICE_CPU: the in-tree
                                  # identify.log instruments IICE_CPU, and a
                                  # collision would make these tests depend on
                                  # whether a build happens to be present.

MANIFEST_YAML = textwrap.dedent(
    """
    iice:
      name: %s
      depth: %d
      trigger_time: middle
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
    """ % (IICE_NAME, DEPTH)
)

SIGNAL_MAP_TSV = "\n".join([
    "\t".join(["name", "width", "radix", "hw_path", "sim_path"]),
    "\t".join(["haddr", "4", "hex", "/rp_top/u_dut/HADDR", "tb.u_dut.haddr"]),
    "\t".join(["flag", "1", "bin", "/rp_top/u_dut/FLAG", "tb.u_dut.flag"]),
    "\t".join(["trigger_marker", "1", "bin", "__DERIVED__", "__DERIVED__"]),
    "\t".join(["sample_clk", "1", "bin", "/rp_top/dut_clk", "tb.u_dut.clk"]),
]) + "\n"

N_EDGES = 20
TRIGGER_SAMPLE = 9


def sim_sample_values(i):
    if i <= 2:
        return "xxxx", "x"
    return format(i % 16, "04b"), ("1" if i % 3 == 0 else "0")


def make_sim_vcd_text():
    """A continuous clocked VCD in the shape the generated shadow module dumps."""
    L = [
        "$date\n\tprovenance test\n$end",
        "$version\n\thandmade\n$end",
        "$timescale\n\t1ns\n$end",
        "$scope module iice $end",
        "$var wire 4 ! haddr [3:0] $end",
        '$var wire 1 " flag $end',
        "$var wire 1 # trigger_marker $end",
        "$var wire 1 $ sample_clk $end",
        "$upscope $end",
        "$enddefinitions $end",
        "#0", "$dumpvars", "bxxxx !", 'x"', "x#", "0$", "$end",
    ]
    prev = {"!": "xxxx", '"': "x", "#": "x"}
    for k in range(N_EDGES):
        t = 10 * (k + 1)
        haddr, flag = sim_sample_values(k)
        marker = "1" if k == TRIGGER_SAMPLE + 1 else ("x" if k == 0 else "0")
        L.append("#%d" % t)
        L.append("1$")
        for ident, val, width in (("!", haddr, 4), ('"', flag, 1), ("#", marker, 1)):
            if val != prev[ident]:
                L.append(("b%s %s" % (val, ident)) if width > 1
                         else ("%s%s" % (val, ident)))
                prev[ident] = val
        L.append("#%d" % (t + 5))
        L.append("0$")
    return "\n".join(L) + "\n"


@pytest.fixture
def fx(tmp_path):
    d = tmp_path / "prov"
    d.mkdir()
    (d / "signal_map.tsv").write_text(SIGNAL_MAP_TSV)
    (d / "signals_provtest.yaml").write_text(MANIFEST_YAML)
    (d / "sim_iice.vcd").write_text(make_sim_vcd_text())
    (d / "logs").mkdir()
    return d


@pytest.fixture
def identity(fx):
    return P.manifest_identity(str(fx / "signals_provtest.yaml"))


def write_identify_log(path, signals, depth, width, clock, iice,
                       gen_depth=None):
    """A synthetic Identify instrumentor log, in the real one's exact shape.

    Shape taken verbatim from fpga/rp/nanosoc_iice/build/rev_1_identify/
    identify.log -- `test_parse_the_REAL_identify_log` asserts the parser reads
    that file, so this fixture cannot drift into a shape only it produces.
    """
    L = ["Tool: Identify (R) Instrumentor",
         "Build: T-2022.09-SP2",
         "Setting JTAG Style to 'soft'",
         "Setting IICE sample clock to '%s' for IICE named '%s'" % (clock, iice),
         "Setting IICE sampler (sampledepth) to %d for IICE named '%s'"
         % (depth, iice)]
    for s in signals:
        if s["sample"] and s["trigger"]:
            mode = "trigger and sample"
        elif s["sample"]:
            mode = "sample"
        else:
            mode = "trigger"
        L.append("Instrument Signal %s for %s in %s" % (s["hw"], mode, iice))
        L.append("\t\tTotal instrumentation in bits: Sample Only 0")
    L += [" Generating IICE '%s' for the following settings:" % iice,
          "      Sample Buffer:",
          "          Type                   behavioral",
          "          Depth                  %d" % (depth if gen_depth is None
                                                   else gen_depth),
          "          Width                  %d bits" % width,
          "          RAM type               BRAM",
          "exit status=0"]
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")
    return path


def agreeing_log(tmp_path, identity, **kw):
    return write_identify_log(
        str(tmp_path / "identify_agree.log"), identity["signals"],
        identity["depth"], identity["sampled_bits"],
        identity["clock"]["hw"], identity["iice_name"], **kw)


# ==========================================================================
# 1. Identity -- the digest changes exactly when the probe set changes
# ==========================================================================

def test_manifest_identity_of_the_shipped_manifests():
    """The two nanosoc manifests are the incident: 13/113 vs 6/69."""
    retargeted = P.manifest_identity(os.path.join(_HERE, "signals_nanosoc.yaml"))
    asbuilt = P.manifest_identity(
        os.path.join(_HERE, "signals_nanosoc_asbuilt.yaml"))
    assert (retargeted["n_signals"], retargeted["sampled_bits"]) == (13, 113)
    assert (asbuilt["n_signals"], asbuilt["sampled_bits"]) == (6, 69)
    # same IICE name and same depth -- which is exactly why nothing caught it
    assert retargeted["iice_name"] == asbuilt["iice_name"] == "IICE_CPU"
    assert retargeted["depth"] == asbuilt["depth"] == 1024
    # ...and yet the identity differs, which is the whole point
    assert retargeted["signal_set_sha256"] != asbuilt["signal_set_sha256"]


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_manifest_has_a_stable_identity(name):
    path = os.path.join(_HERE, name)
    a = P.manifest_identity(path)
    b = P.manifest_identity(path)
    assert a["signal_set_sha256"] == b["signal_set_sha256"]
    assert len(a["signal_set_sha256"]) == 64
    assert a["read_by"] == "manifest.py"       # the validating reader, not YAML


def test_digest_text_is_domain_separated(identity):
    text = P.digest_text(str(identity["iice_name"]), int(identity["depth"]),
                         identity["clock"], identity["signals"])
    assert text.startswith(P.DIGEST_DOMAIN + "\n")
    assert "\niice\t%s\n" % IICE_NAME in text
    assert "\ndepth\t%d\n" % DEPTH in text


def test_digest_IGNORES_radix_because_radix_is_a_display_choice(identity):
    """Deliberately permissive: re-rendering haddr in binary must not make an
    existing capture unusable."""
    sigs = json.loads(json.dumps(identity["signals"]))
    sigs[0]["radix"] = "bin"
    assert P.signal_set_digest(
        identity["iice_name"], identity["depth"], identity["clock"], sigs
    ) == identity["signal_set_sha256"]


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda s, c: s[0].__setitem__("width", 8), id="width"),
    pytest.param(lambda s, c: s[0].__setitem__("hw", "/other/HADDR"), id="hw_path"),
    pytest.param(lambda s, c: s[0].__setitem__("sim", "tb.other.haddr"), id="sim_path"),
    pytest.param(lambda s, c: s[0].__setitem__("name", "haddr2"), id="name"),
    pytest.param(lambda s, c: s[0].__setitem__("sample", False), id="sample_flag"),
    pytest.param(lambda s, c: s[0].__setitem__("trigger", False), id="trigger_flag"),
    pytest.param(lambda s, c: s.reverse(), id="signal_order"),
    pytest.param(lambda s, c: s.pop(), id="signal_removed"),
    pytest.param(lambda s, c: c.__setitem__("hw", "/rp_top/other_clk"), id="clock_hw"),
    pytest.param(lambda s, c: c.__setitem__("edge", "negative"), id="clock_edge"),
])
def test_digest_changes_when_the_probe_set_changes(identity, mutate):
    sigs = json.loads(json.dumps(identity["signals"]))
    clock = json.loads(json.dumps(identity["clock"]))
    mutate(sigs, clock)
    assert P.signal_set_digest(
        identity["iice_name"], identity["depth"], clock, sigs
    ) != identity["signal_set_sha256"]


def test_digest_changes_with_depth_and_iice_name(identity):
    for name, depth in ((IICE_NAME, DEPTH * 2), ("IICE_OTHER", DEPTH)):
        assert P.signal_set_digest(
            name, depth, identity["clock"], identity["signals"]
        ) != identity["signal_set_sha256"]


def test_lenient_yaml_reader_agrees_with_the_validating_one(tmp_path):
    """The fallback path must produce the SAME identity, or a manifest that
    happens to fail validation would silently get a different digest and every
    artifact stamped with it would read as mismatched."""
    path = tmp_path / "m.yaml"
    path.write_text(MANIFEST_YAML)
    strict = P.manifest_identity(str(path))

    real_load = mf.load

    def boom(_p):
        raise mf.ManifestError("forced", ["forced fallback"])

    mf.load = boom
    try:
        lenient = P.manifest_identity(str(path))
    finally:
        mf.load = real_load
    assert lenient["read_by"] == "yaml"
    assert lenient["signal_set_sha256"] == strict["signal_set_sha256"]
    # and the documented defaults really are applied on the lenient path
    assert [s["sample"] for s in lenient["signals"]] == [True, True]
    assert [s["trigger"] for s in lenient["signals"]] == [True, False]


# ==========================================================================
# 2. manifest <-> signal_map.tsv  (incident 1, generalised)
# ==========================================================================

@pytest.mark.parametrize("name", SHIPPED)
def test_expected_rows_AGREE_with_stream_As_generator(name):
    """ANTI-DRIFT. ``expected_signal_map_rows`` reimplements gen_mangle's row
    order (it must work on the lenient path, which has no validated Manifest).
    If the two ever disagree, this harness would refuse every correctly
    generated map -- so the agreement is gated, not assumed."""
    path = os.path.join(_HERE, name)
    assert P.expected_signal_map_rows(P.manifest_identity(path)) == \
        [tuple(r) for r in gm.rows(mf.load(path))]


def test_the_committed_golden_signal_map_matches_its_manifest():
    """The shipped golden/ pair must satisfy the binding, or `make check` would
    refuse its own gate input."""
    identity = P.manifest_identity(os.path.join(_HERE, "signals_selftest.yaml"))
    info = P.check_signal_map(
        identity, os.path.join(_HERE, "golden", "signal_map.tsv"))
    assert info["verified"] is True and info["rows"] == 9


def test_check_signal_map_accepts_the_matching_pair(fx, identity):
    # ANTI-WEAKENING: the ordinary case must stay silent.
    assert P.check_signal_map(identity, str(fx / "signal_map.tsv"))["verified"]


def test_check_signal_map_accepts_ROWS_passed_in_memory(fx, identity):
    """normalise_hw_capture holds parsed rows, not a path."""
    rows = ct.read_signal_map(str(fx / "signal_map.tsv"))
    assert P.check_signal_map(identity, rows)["verified"]


def test_check_signal_map_REFUSES_a_map_from_another_manifest(fx):
    """PRE-FIX: nothing compared the two at all, so `make compare` happily used
    the selftest rename table for a nanosoc capture (2026-07-30)."""
    identity = P.manifest_identity(os.path.join(_HERE, "signals_nanosoc.yaml"))
    with pytest.raises(ft.HarnessError) as e:
        P.check_signal_map(identity, str(fx / "signal_map.tsv"))
    msg = str(e.value)
    assert "WAS NOT GENERATED FROM THIS MANIFEST" in msg
    assert "row count" in msg
    assert "signals_nanosoc.yaml" in msg


@pytest.mark.parametrize("field,col,bad", [
    ("sim_path", 4, "tb.u_dut.SOMETHING_ELSE"),
    ("hw_path", 3, "/rp_top/u_other/HADDR"),
    ("width", 1, "8"),
])
def test_check_signal_map_names_the_offending_row_and_field(fx, identity,
                                                            field, col, bad):
    """A refusal that does not say WHICH field differs sends the reader hunting.

    ``sim_path`` is the load-bearing case: it is invisible to every other check
    in the pipeline (sim-side set equality matches on ``<scope>/<name>``, the
    hardware leaf map does not use it, and the values are unaffected), so before
    this check a foreign map compared cleanly and lied.
    """
    lines = SIGNAL_MAP_TSV.rstrip("\n").split("\n")
    f = lines[1].split("\t")
    f[col] = bad
    lines[1] = "\t".join(f)
    bad_map = fx / "bad_map.tsv"
    bad_map.write_text("\n".join(lines) + "\n")
    with pytest.raises(ft.HarnessError) as e:
        P.check_signal_map(identity, str(bad_map))
    msg = str(e.value)
    assert "row 1 field %r" % field in msg
    assert bad in msg
    assert "make -C tests/identify_iice signal-map" in msg   # the fix, named


def test_check_signal_map_reports_extra_and_missing_rows(fx, identity):
    lines = SIGNAL_MAP_TSV.rstrip("\n").split("\n")
    extra = fx / "extra.tsv"
    extra.write_text("\n".join(lines + ["\t".join(
        ["ghost", "1", "bin", "/rp_top/GHOST", "tb.ghost"])]) + "\n")
    with pytest.raises(ft.HarnessError) as e:
        P.check_signal_map(identity, str(extra))
    assert "in signal_map.tsv but not in the manifest" in str(e.value)

    short = fx / "short.tsv"
    short.write_text("\n".join(lines[:-1]) + "\n")
    with pytest.raises(ft.HarnessError) as e:
        P.check_signal_map(identity, str(short))
    assert "in the manifest but not in signal_map.tsv" in str(e.value)


# ==========================================================================
# 3. manifest <-> the instrumentation that was BUILT  (incident 2)
# ==========================================================================

@has_real_log
def test_parse_the_REAL_identify_log():
    """Ground truth, on the actual file that proved the as-built set was 69 bits.

    Hard-codes the measured numbers from
    fpga/rp/nanosoc_iice/build/rev_1_identify/identify.log so a parser
    regression cannot pass by agreeing with itself.
    """
    log = P.parse_identify_log(REAL_LOG)
    assert log is not None
    assert sorted(log["iices"]) == ["IICE_CPU"]
    built = log["iices"]["IICE_CPU"]
    assert built["depth"] == 1024
    assert built["gen_depth"] == 1024
    assert built["width_bits"] == 69           # NOT 113
    assert built["clock"] == "/dut_clk"
    assert len(built["signals"]) == 6
    paths = {s["path"]: (s["sample"], s["trigger"]) for s in built["signals"]}
    assert paths["/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HADDR"] == (True, True)
    assert paths["/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HRDATA"] == (True, False)
    assert paths["/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/CORE_LOCKUP"] == (True, False)


@has_real_log
def test_the_ASBUILT_manifest_matches_the_real_built_instrumentation():
    """signals_nanosoc_asbuilt.yaml exists to record what is in silicon. If this
    fails, that record has drifted from the log it was derived from."""
    identity = P.manifest_identity(
        os.path.join(_HERE, "signals_nanosoc_asbuilt.yaml"))
    result = P.check_identify_log(identity, P.parse_identify_log(REAL_LOG))
    assert result["status"] == "ok", result["discrepancies"]
    assert result["facts"]["width_bits"] == identity["sampled_bits"] == 69


@has_real_log
def test_the_RETARGETED_manifest_is_REFUSED_against_the_real_log():
    """THE INCIDENT, on real data.

    PRE-FIX: nothing compared a manifest with the built instrumentation, so
    `MANIFEST=signals_nanosoc.yaml make hw-fsdb` would normalise a capture off
    the 69-bit bitstream against a 113-bit manifest. It surfaced only later, as a
    set-equality failure, and only because the signal NAMES happened to differ.
    """
    identity = P.manifest_identity(os.path.join(_HERE, "signals_nanosoc.yaml"))
    result = P.check_identify_log(identity, P.parse_identify_log(REAL_LOG))
    assert result["status"] == "mismatch"
    text = P.identify_log_error(identity, result)
    assert "113" in text and "69" in text            # both numbers, named
    assert "sample-buffer width" in text
    assert "HPROT" in text                           # the missing probes, listed
    assert "signals_nanosoc_asbuilt.yaml" in text    # the fix, named


def test_an_ABSENT_log_DEGRADES_and_is_not_a_mismatch(tmp_path, identity):
    """ANTI-WEAKENING: `build/` is gitignored, so a fresh clone has no log."""
    result = P.check_identify_log(
        identity, P.parse_identify_log(str(tmp_path / "nope.log")))
    assert result["status"] == "absent"
    assert result["discrepancies"] == []
    assert "fresh clone" in result["note"]


def test_a_log_for_a_DIFFERENT_iice_is_not_applicable_unless_required(
        tmp_path, identity):
    """The selftest IICE is a simulation-only fixture and is never instrumented,
    so pairing it with the nanosoc log is a non-pairing, not a finding. It is
    still REPORTED -- an explicit non-result, never a silent pass."""
    log = write_identify_log(str(tmp_path / "other.log"), identity["signals"],
                             identity["depth"], identity["sampled_bits"],
                             identity["clock"]["hw"], "IICE_SOMETHING_ELSE")
    parsed = P.parse_identify_log(log)
    lax = P.check_identify_log(identity, parsed)
    assert lax["status"] == "not_applicable"
    assert "IICE_SOMETHING_ELSE" in lax["note"]
    strict = P.check_identify_log(identity, parsed, require_iice=True)
    assert strict["status"] == "mismatch"


def test_an_AGREEING_log_is_ok(tmp_path, identity):
    result = P.check_identify_log(identity,
                                 P.parse_identify_log(agreeing_log(tmp_path, identity)))
    assert result["status"] == "ok" and result["discrepancies"] == []


def test_a_log_naming_no_iice_at_all_is_a_mismatch(tmp_path, identity):
    junk = tmp_path / "junk.log"
    junk.write_text("this is not an Identify log\n")
    result = P.check_identify_log(identity, P.parse_identify_log(str(junk)))
    assert result["status"] == "mismatch"
    assert "names no IICE" in result["discrepancies"][0]


def test_log_mismatch_names_a_DEPTH_disagreement(tmp_path, identity):
    log = write_identify_log(str(tmp_path / "d.log"), identity["signals"],
                             identity["depth"] * 2, identity["sampled_bits"],
                             identity["clock"]["hw"], identity["iice_name"])
    result = P.check_identify_log(identity, P.parse_identify_log(log))
    assert result["status"] == "mismatch"
    assert any(d.startswith("depth:") for d in result["discrepancies"])


def test_log_mismatch_names_a_WIDTH_disagreement(tmp_path, identity):
    """The 69-vs-113 shape, in miniature: same names, fewer sampled bits."""
    log = write_identify_log(str(tmp_path / "w.log"), identity["signals"],
                             identity["depth"], identity["sampled_bits"] - 1,
                             identity["clock"]["hw"], identity["iice_name"])
    result = P.check_identify_log(identity, P.parse_identify_log(log))
    assert result["status"] == "mismatch"
    assert any("sample-buffer width" in d for d in result["discrepancies"])


def test_log_mismatch_names_a_CLOCK_disagreement(tmp_path, identity):
    log = write_identify_log(str(tmp_path / "c.log"), identity["signals"],
                             identity["depth"], identity["sampled_bits"],
                             "/rp_top/some_other_clk", identity["iice_name"])
    result = P.check_identify_log(identity, P.parse_identify_log(log))
    assert result["status"] == "mismatch"
    assert any("sample clock" in d for d in result["discrepancies"])


def test_log_mismatch_names_a_SAMPLE_TRIGGER_FLAG_disagreement(tmp_path, identity):
    """A flag flip changes the instrumentation without changing the signal list,
    so a name-set check alone would miss it."""
    sigs = json.loads(json.dumps(identity["signals"]))
    sigs[0]["trigger"] = False
    log = write_identify_log(str(tmp_path / "f.log"), sigs, identity["depth"],
                             identity["sampled_bits"], identity["clock"]["hw"],
                             identity["iice_name"])
    result = P.check_identify_log(identity, P.parse_identify_log(log))
    assert result["status"] == "mismatch"
    assert any("trigger=" in d and "HADDR" in d for d in result["discrepancies"])


def test_log_mismatch_names_an_EXTRA_instrumented_signal(tmp_path, identity):
    sigs = json.loads(json.dumps(identity["signals"]))
    sigs.append({"name": "ghost", "width": 1, "hw": "/rp_top/u_dut/GHOST",
                 "sim": "tb.ghost", "sample": True, "trigger": False,
                 "radix": "bin"})
    log = write_identify_log(str(tmp_path / "x.log"), sigs, identity["depth"],
                             identity["sampled_bits"] + 1,
                             identity["clock"]["hw"], identity["iice_name"])
    result = P.check_identify_log(identity, P.parse_identify_log(log))
    assert result["status"] == "mismatch"
    assert any("NOT in the manifest" in d and "GHOST" in d
               for d in result["discrepancies"])


def test_a_log_that_disagrees_with_ITSELF_on_depth_is_a_mismatch(tmp_path, identity):
    log = write_identify_log(str(tmp_path / "s.log"), identity["signals"],
                             identity["depth"], identity["sampled_bits"],
                             identity["clock"]["hw"], identity["iice_name"],
                             gen_depth=identity["depth"] * 2)
    result = P.check_identify_log(identity, P.parse_identify_log(log))
    assert result["status"] == "mismatch"
    assert any("disagrees with ITSELF" in d for d in result["discrepancies"])


# ==========================================================================
# 4. ids -- same spelling as the overlay manifests
# ==========================================================================

@pytest.mark.parametrize("raw,want", [
    ("0x0EE58A4D", "0x0EE58A4D"),
    ("0ee58a4d", "0x0EE58A4D"),
    ("0x0000_0001", "0x00000001"),      # the overlay-manifest grouping
    ("0x1", "0x00000001"),
    ("  0xCD74B6AE  ", "0xCD74B6AE"),
    ("", None),
    (None, None),
])
def test_norm_id(raw, want):
    assert P.norm_id(raw) == want


@pytest.mark.parametrize("bad", ["0xZZZZ", "nanosoc", "0x1234567890", "0x"])
def test_norm_id_REFUSES_junk_rather_than_disabling_the_check(bad):
    """A typo in IICE_EXPECT_RM_ID must stop the run. Returning None would
    silently switch off the check the caller was trying to turn on."""
    with pytest.raises(ft.HarnessError) as e:
        P.norm_id(bad, "IICE_EXPECT_RM_ID")
    assert "IICE_EXPECT_RM_ID" in str(e.value)


def test_ids_and_expectations_come_from_disjoint_env_vars():
    env = {"IICE_STATIC_ID": "0x0EE58A4D", "IICE_RM_ID": "0x01000001",
           "IICE_EXPECT_RM_ID": "0x0100001E"}
    assert P.ids_from_env(env) == {"static_id": "0x0EE58A4D",
                                  "rm_id": "0x01000001"}
    assert P.expectations_from_env(env) == {"rm_id": "0x0100001E"}


# ==========================================================================
# 5. Stamps -- absence is permissive, conflict is fatal
# ==========================================================================

def _artifact(tmp_path, content=b"not really an fsdb"):
    p = tmp_path / "hw.fsdb"
    p.write_bytes(content)
    return str(p)


def test_stamp_round_trip(tmp_path, identity):
    art = _artifact(tmp_path)
    stamp = P.build_stamp(identity, art, origin=P.ORIGIN_CAPTURE,
                          produced_by="a test",
                          capture={"n_samples": 923},
                          ids={"static_id": "0x0EE58A4D",
                               "rm_id": "0x01000001"})
    path = P.write_stamp(art, stamp)
    assert path == art + ".prov.json"
    back = P.read_stamp(art)
    assert back == stamp
    assert back["manifest"]["signal_set_sha256"] == identity["signal_set_sha256"]
    assert back["artifact"]["bytes"] == os.path.getsize(art)
    assert back["schema"] == P.SCHEMA
    # overlay-manifest spelling, not a parallel convention
    assert back["static_id"] == "0x0EE58A4D" and back["rm_id"] == "0x01000001"
    assert P.check_stamp(back, identity, artifact=art) == []


def test_NO_stamp_is_NOT_a_mismatch(tmp_path, identity):
    """ANTI-WEAKENING, the single most important assertion in this file.

    Every capture taken before 2026-07-31 and every hand-made phase-0 FSDB is
    unstamped. If absence were treated as a conflict the entire existing corpus
    would stop being comparable.
    """
    art = _artifact(tmp_path)
    assert P.read_stamp(art) is None
    assert P.check_stamp(None, identity, artifact=art) == []


def test_a_stamp_naming_a_DIFFERENT_probe_set_is_a_conflict(tmp_path, identity):
    """PRE-FIX: no stamp existed and no check existed, so this pairing compared
    cleanly and produced a confident wrong verdict."""
    art = _artifact(tmp_path)
    other = P.manifest_identity(os.path.join(_HERE, "signals_nanosoc.yaml"))
    P.write_stamp(art, P.build_stamp(other, art, origin=P.ORIGIN_CAPTURE,
                                     produced_by="a test"))
    disc = P.check_stamp(P.read_stamp(art), identity, artifact=art)
    assert len(disc) == 1
    assert "DIFFERENT probe set" in disc[0]
    assert P.short_digest(other["signal_set_sha256"]) in disc[0]
    assert P.short_digest(identity["signal_set_sha256"]) in disc[0]


def test_a_stamp_naming_a_different_depth_is_a_conflict(tmp_path, identity):
    art = _artifact(tmp_path)
    stamp = P.build_stamp(identity, art, origin=P.ORIGIN_CAPTURE,
                          produced_by="a test")
    # digest left alone so the depth line is what has to fire
    stamp["manifest"]["depth"] = DEPTH * 4
    disc = P.check_stamp(stamp, identity, artifact=art)
    assert any("depth" in d for d in disc)


def test_a_STALE_stamp_beside_a_replaced_artifact_is_a_conflict(tmp_path, identity):
    """Incident 3's shape: the sidecar must describe THIS file, not a previous
    one that happened to have the same name."""
    art = _artifact(tmp_path)
    P.write_stamp(art, P.build_stamp(identity, art, origin=P.ORIGIN_CAPTURE,
                                     produced_by="a test"))
    assert P.check_stamp(P.read_stamp(art), identity, artifact=art) == []
    with open(art, "ab") as fh:
        fh.write(b"a different capture entirely")
    disc = P.check_stamp(P.read_stamp(art), identity, artifact=art)
    assert len(disc) == 1 and "STALE STAMP" in disc[0]


def test_a_stamp_from_a_FUTURE_schema_is_refused_not_ignored(tmp_path, identity):
    art = _artifact(tmp_path)
    stamp = P.build_stamp(identity, art, origin=P.ORIGIN_CAPTURE,
                          produced_by="a test")
    stamp["schema"] = P.SCHEMA + 1
    disc = P.check_stamp(stamp, identity, artifact=art)
    assert len(disc) == 1 and "schema" in disc[0]
    assert "must never read as 'verified'" in disc[0]


def test_a_CORRUPT_stamp_is_a_harness_error_not_an_absent_one(tmp_path, identity):
    art = _artifact(tmp_path)
    with open(P.stamp_path(art), "w") as fh:
        fh.write("{ this is not json")
    with pytest.raises(ft.HarnessError) as e:
        P.read_stamp(art)
    assert "unreadable" in str(e.value)


def test_a_stamp_that_is_not_an_object_is_a_harness_error(tmp_path, identity):
    art = _artifact(tmp_path)
    with open(P.stamp_path(art), "w") as fh:
        json.dump([1, 2, 3], fh)
    with pytest.raises(ft.HarnessError):
        P.read_stamp(art)


def test_id_checks_are_PER_FIELD_permissive(tmp_path, identity):
    """A stamp that records no static_id is silent about static_id. Only a
    recorded-AND-different value is a conflict."""
    art = _artifact(tmp_path)
    stamp = P.build_stamp(identity, art, origin=P.ORIGIN_CAPTURE,
                          produced_by="a test",
                          ids={"rm_id": "0x01000001"})
    expect = {"rm_id": "0x01000001", "static_id": "0x0EE58A4D"}
    # static_id is expected but not recorded -> not a conflict
    assert P.check_stamp(stamp, identity, artifact=art, expect=expect) == []
    # rm_id recorded and DIFFERENT -> conflict, both values named
    disc = P.check_stamp(stamp, identity, artifact=art,
                         expect={"rm_id": "0x0100001E"})
    assert len(disc) == 1
    assert "0x01000001" in disc[0] and "0x0100001E" in disc[0]


def test_expectations_are_normalised_before_comparison(tmp_path, identity):
    """0x0100_0001 and 0x01000001 are the same id; a formatting difference must
    not read as a mismatched RM."""
    art = _artifact(tmp_path)
    stamp = P.build_stamp(identity, art, origin=P.ORIGIN_CAPTURE,
                          produced_by="a test", ids={"rm_id": "0x0100_0001"})
    assert P.check_stamp(stamp, identity, artifact=art,
                         expect={"rm_id": "0x01000001"}) == []


def test_stamp_report_lines_say_UNVERIFIED_when_there_is_no_stamp():
    """A report that silently omits provenance reads as if provenance had been
    checked -- which is how three mismatched pairs got believed."""
    lines = "\n".join(P.stamp_report_lines(None, label="hardware"))
    assert "carries no provenance stamp" in lines
    assert "Absence is NOT a" in lines
    assert "not as verified" in lines


def test_stamp_report_lines_say_when_no_ids_were_recorded(tmp_path, identity):
    art = _artifact(tmp_path)
    stamp = P.build_stamp(identity, art, origin=P.ORIGIN_SYNTHETIC,
                          produced_by="a test")
    lines = "\n".join(P.stamp_report_lines(stamp))
    assert "VERIFIED against the manifest above" in lines
    assert "not recorded" in lines
    assert "no board, no Identify" in lines      # a synthetic trace says so


def test_refuse_message_names_the_artifacts(tmp_path, identity):
    text = P.refuse(["something disagreed"], "/x/hw.fsdb", "/x/m.yaml")
    assert "PROVENANCE MISMATCH" in text
    assert "/x/hw.fsdb.prov.json" in text
    assert "INTERFACES.md §4" in text


# ==========================================================================
# 6. The CLI -- exit codes are INTERFACES.md §4's
# ==========================================================================

def _cli(*args):
    return subprocess.run(
        [sys.executable, os.path.join(_HERE, "provenance.py")] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)


def test_cli_identity_and_signal_map_check_exit_zero(fx):
    r = _cli("identity", "--manifest", str(fx / "signals_provtest.yaml"),
             "--signal-map", str(fx / "signal_map.tsv"))
    assert r.returncode == 0, r.stdout
    assert "signal_set_sha256" in r.stdout
    r = _cli("check-signal-map", "--manifest", str(fx / "signals_provtest.yaml"),
             "--signal-map", str(fx / "signal_map.tsv"))
    assert r.returncode == 0, r.stdout


def test_cli_refuses_a_foreign_signal_map_with_exit_2(fx):
    r = _cli("check-signal-map",
             "--manifest", os.path.join(_HERE, "signals_nanosoc.yaml"),
             "--signal-map", str(fx / "signal_map.tsv"))
    assert r.returncode == ft.EXIT_HARNESS, r.stdout
    assert "WAS NOT GENERATED FROM THIS MANIFEST" in r.stdout


def test_cli_identity_json_is_machine_readable(fx):
    r = _cli("identity", "--manifest", str(fx / "signals_provtest.yaml"), "--json")
    assert r.returncode == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["iice_name"] == IICE_NAME and doc["depth"] == DEPTH


@has_real_log
def test_cli_check_identify_log_exit_codes_on_real_data():
    ok = _cli("check-identify-log", "--manifest",
              os.path.join(_HERE, "signals_nanosoc_asbuilt.yaml"))
    assert ok.returncode == 0, ok.stdout
    bad = _cli("check-identify-log", "--manifest",
               os.path.join(_HERE, "signals_nanosoc.yaml"))
    assert bad.returncode == ft.EXIT_HARNESS, bad.stdout
    assert "INSTRUMENTATION MISMATCH" in bad.stdout


def test_cli_check_identify_log_skips_cleanly_with_no_log(fx, tmp_path):
    r = _cli("check-identify-log", "--manifest", str(fx / "signals_provtest.yaml"),
             "--identify-log", str(tmp_path / "nope.log"))
    assert r.returncode == 0, r.stdout
    assert "SKIP" in r.stdout
    r = _cli("check-identify-log", "--manifest", str(fx / "signals_provtest.yaml"),
             "--identify-log", str(tmp_path / "nope.log"), "--require-log")
    assert r.returncode == ft.EXIT_HARNESS, r.stdout


def test_cli_show_and_check_stamp(fx, tmp_path, identity):
    art = _artifact(tmp_path)
    manifest = str(fx / "signals_provtest.yaml")
    r = _cli("show-stamp", "--artifact", art)
    assert r.returncode == 0 and "carries NO stamp" in r.stdout
    r = _cli("check-stamp", "--artifact", art, "--manifest", manifest)
    assert r.returncode == 0 and "carries no stamp" in r.stdout
    P.write_stamp(art, P.build_stamp(identity, art, origin=P.ORIGIN_CAPTURE,
                                     produced_by="a test",
                                     ids={"rm_id": "0x01000001"}))
    r = _cli("check-stamp", "--artifact", art, "--manifest", manifest)
    assert r.returncode == 0 and "AGREES" in r.stdout
    r = _cli("check-stamp", "--artifact", art, "--manifest", manifest,
             "--expect-rm-id", "0x0100001E")
    assert r.returncode == ft.EXIT_HARNESS, r.stdout
    assert "PROVENANCE MISMATCH" in r.stdout


# ==========================================================================
# 7. End to end, through the real FSDB tools
# ==========================================================================

def _sim_fsdb(fx):
    logdir = str(fx / "logs")
    return ft.vcd2fsdb(str(fx / "sim_iice.vcd"), str(fx / "sim_iice.fsdb"), logdir)


CAP_DEPTH, CAP_T0, CAP_STEP = 8, 1230, 10

CAP_MANIFEST_YAML = textwrap.dedent(
    """
    iice:
      name: IICE_PROVCAP
      depth: 8
      trigger_time: middle
      controller: statemachine
      trigger_conditions: 2
      trigger_states: 2
      clock:
        hw:   /rp_top/dut_clk
        sim:  tb.u_dut.clk
        edge: positive
    signals:
      - {name: haddr, width: 4, hw: /rp_top/HADDR, sim: tb.u_dut.haddr,
         sample: true, trigger: true, radix: hex}
      - {name: flag,  width: 1, hw: /rp_top/FLAG,  sim: tb.u_dut.flag,
         sample: true, radix: bin}
    trigger:
      expr: "haddr == 4'h9"
    """)

CAP_SIGNAL_MAP_TSV = "\n".join([
    "\t".join(["name", "width", "radix", "hw_path", "sim_path"]),
    "\t".join(["haddr", "4", "hex", "/rp_top/HADDR", "tb.u_dut.haddr"]),
    "\t".join(["flag", "1", "bin", "/rp_top/FLAG", "tb.u_dut.flag"]),
    "\t".join(["trigger_marker", "1", "bin", "__DERIVED__", "__DERIVED__"]),
    "\t".join(["sample_clk", "1", "bin", "/rp_top/dut_clk", "tb.u_dut.clk"]),
]) + "\n"


def make_identify_capture(fx):
    """An Identify-shaped capture: flat leaves in ONE scope, a non-zero time base,
    Identify's two injected signals, and ``identify_sampleclock`` aliased onto the
    same VCD identifier as the probed clock (INTERFACES.md §7 facts 2-5).

    Returns ``(raw_fsdb, manifest, signal_map)``.
    """
    logdir = str(fx / "logs")
    L = ["$date\n\t\n$end", "$version\n\tHAPS\n$end", "$timescale\n\t1ns\n$end",
         "$scope module rp_top $end",
         "$var wire 1 ! identify_sampleclock $end",
         '$var wire 1 " identify_cycle $end',
         "$var reg 1 ! dut_clk $end",
         "$var reg 4 # HADDR [3:0] $end",
         "$var reg 1 $ FLAG $end",
         "$upscope $end", "$enddefinitions $end"]
    prev = {}
    for i in range(CAP_DEPTH):
        L.append("#%d" % (CAP_T0 + i * CAP_STEP))
        if i == 0:
            L.append("$dumpvars")
        vals = {"!": "1" if i % 2 == 0 else "0", "#": format(i, "04b"),
                "$": "1" if i >= 2 else "0"}
        for ident in ("!", "#", "$"):
            if vals[ident] != prev.get(ident):
                L.append(("b%s %s" % (vals[ident], ident)) if ident == "#"
                         else "%s%s" % (vals[ident], ident))
                prev[ident] = vals[ident]
        if i == 0:
            L.append("$end")
    (fx / "cap.vcd").write_text("\n".join(L) + "\n")
    (fx / "signals_cap.yaml").write_text(CAP_MANIFEST_YAML)
    (fx / "signal_map_cap.tsv").write_text(CAP_SIGNAL_MAP_TSV)
    raw = ft.vcd2fsdb(str(fx / "cap.vcd"), str(fx / "cap.fsdb"), logdir)
    return raw, str(fx / "signals_cap.yaml"), str(fx / "signal_map_cap.tsv")


@needs_verdi
def test_synth_hw_fsdb_STAMPS_its_output_and_compare_accepts_it(fx):
    """The gate's own path: fake-hw stamps, compare verifies, verdict unchanged."""
    logdir = str(fx / "logs")
    sim = _sim_fsdb(fx)
    smap, manifest = str(fx / "signal_map.tsv"), str(fx / "signals_provtest.yaml")
    hw = str(fx / "hw_iice.fsdb")
    rc = sh.main(["--sim-fsdb", sim, "--signal-map", smap, "--manifest", manifest,
                  "--out", hw, "--logdir", logdir])
    assert rc == 0
    stamp = P.read_stamp(hw)
    assert stamp is not None
    assert stamp["origin"] == P.ORIGIN_SYNTHETIC
    assert stamp["produced_by"] == "synth_hw_fsdb.py"
    assert stamp["manifest"]["signal_set_sha256"] == \
        P.manifest_identity(manifest)["signal_set_sha256"]
    assert stamp["capture"]["n_samples"] == DEPTH
    # a synthetic trace must NOT claim to have come off a shell
    assert "static_id" not in stamp and "rm_id" not in stamp

    code, text = tc.run_compare(sim, hw, smap, manifest,
                                str(fx / "report.txt"), logdir,
                                use_ncompare=False)
    assert code == 0, text
    assert "VERDICT: MATCH" in text
    assert "-- provenance" in text
    assert "VERIFIED row-for-row against this manifest" in text
    assert "VERIFIED against the manifest above" in text


@needs_verdi
def test_run_compare_REFUSES_a_trace_stamped_against_another_probe_set(fx):
    """The pytest twin of `compare.sh` negctl NC6.

    PRE-FIX: exit 0, a confident MATCH. Every value, name, width and sample count
    is correct here -- only the provenance disagrees -- so no other check in the
    pipeline can see the problem. That is the 2026-07-30 failure class exactly.
    """
    logdir = str(fx / "logs")
    sim = _sim_fsdb(fx)
    smap, manifest = str(fx / "signal_map.tsv"), str(fx / "signals_provtest.yaml")
    hw = str(fx / "hw_iice.fsdb")
    assert sh.main(["--sim-fsdb", sim, "--signal-map", smap, "--manifest", manifest,
                    "--out", hw, "--logdir", logdir]) == 0
    # sanity: it compares cleanly BEFORE the stamp is tampered with
    assert tc.run_compare(sim, hw, smap, manifest, str(fx / "r0.txt"), logdir,
                          use_ncompare=False)[0] == 0

    stamp = P.read_stamp(hw)
    stamp["manifest"]["signal_set_sha256"] = "f" * 64
    P.write_stamp(hw, stamp)          # rewrites artifact.bytes, so ONLY the
                                      # digest differs
    with pytest.raises(ft.HarnessError) as e:
        tc.run_compare(sim, hw, smap, manifest, str(fx / "r1.txt"), logdir,
                       use_ncompare=False)
    assert "PROVENANCE MISMATCH" in str(e.value)
    assert "DIFFERENT probe set" in str(e.value)

    # and through the CLI, so the FROZEN exit code is what is asserted
    rc = tc.main(["--sim-fsdb", sim, "--hw-fsdb", hw, "--signal-map", smap,
                  "--manifest", manifest, "--report", str(fx / "r2.txt"),
                  "--logdir", logdir, "--no-ncompare"])
    assert rc == ft.EXIT_HARNESS
    assert "HARNESS ERROR" in open(str(fx / "r2.txt")).read()


@needs_verdi
def test_run_compare_STILL_COMPARES_an_unstamped_trace(fx):
    """ANTI-WEAKENING through the whole pipeline: negctl NC7's twin."""
    logdir = str(fx / "logs")
    sim = _sim_fsdb(fx)
    smap, manifest = str(fx / "signal_map.tsv"), str(fx / "signals_provtest.yaml")
    hw = str(fx / "hw_iice.fsdb")
    assert sh.main(["--sim-fsdb", sim, "--signal-map", smap, "--manifest", manifest,
                    "--out", hw, "--logdir", logdir, "--no-stamp"]) == 0
    assert P.read_stamp(hw) is None
    code, text = tc.run_compare(sim, hw, smap, manifest, str(fx / "r.txt"),
                                logdir, use_ncompare=False)
    assert code == 0, text
    assert "carries no provenance stamp" in text     # visibly unverified


@needs_verdi
def test_run_compare_REFUSES_a_signal_map_from_another_manifest(fx):
    """negctl NC8's twin. The perturbation (`sim_path`) is invisible to every
    pre-existing check, so PRE-FIX this exits 0."""
    logdir = str(fx / "logs")
    sim = _sim_fsdb(fx)
    smap, manifest = str(fx / "signal_map.tsv"), str(fx / "signals_provtest.yaml")
    hw = str(fx / "hw_iice.fsdb")
    assert sh.main(["--sim-fsdb", sim, "--signal-map", smap, "--manifest", manifest,
                    "--out", hw, "--logdir", logdir]) == 0
    foreign = fx / "foreign_map.tsv"
    foreign.write_text(SIGNAL_MAP_TSV.replace(
        "tb.u_dut.haddr", "tb.u_dut.haddr_from_another_manifest"))
    with pytest.raises(ft.HarnessError) as e:
        tc.run_compare(sim, hw, str(foreign), manifest, str(fx / "r.txt"),
                       logdir, use_ncompare=False)
    assert "WAS NOT GENERATED FROM THIS MANIFEST" in str(e.value)
    assert "sim_path" in str(e.value)


@needs_verdi
def test_run_compare_ENFORCES_a_caller_supplied_rm_id(fx):
    """Opt-in, but binding once given: which RM was resident is a fact only the
    caller has (scripts/mps3_state.sh --quiet prints RM_ID=)."""
    logdir = str(fx / "logs")
    sim = _sim_fsdb(fx)
    smap, manifest = str(fx / "signal_map.tsv"), str(fx / "signals_provtest.yaml")
    hw = str(fx / "hw_iice.fsdb")
    assert sh.main(["--sim-fsdb", sim, "--signal-map", smap, "--manifest", manifest,
                    "--out", hw, "--logdir", logdir]) == 0
    stamp = P.read_stamp(hw)
    stamp["rm_id"] = "0x00000000"          # greybox: no RM resident
    P.write_stamp(hw, stamp)
    with pytest.raises(ft.HarnessError) as e:
        tc.run_compare(sim, hw, smap, manifest, str(fx / "r.txt"), logdir,
                       use_ncompare=False, expect={"rm_id": "0x01000001"})
    assert "rm_id" in str(e.value)
    # ...and the matching expectation still compares cleanly
    code, text = tc.run_compare(sim, hw, smap, manifest, str(fx / "r2.txt"),
                                logdir, use_ncompare=False,
                                expect={"rm_id": "0x00000000"})
    assert code == 0, text
    assert "caller expectations ENFORCED" in text


@needs_verdi
def test_run_compare_REFUSES_when_the_BUILT_instrumentation_disagrees(fx, tmp_path):
    """The 69-vs-113 refusal, wired all the way through `run_compare`."""
    logdir = str(fx / "logs")
    sim = _sim_fsdb(fx)
    smap, manifest = str(fx / "signal_map.tsv"), str(fx / "signals_provtest.yaml")
    hw = str(fx / "hw_iice.fsdb")
    assert sh.main(["--sim-fsdb", sim, "--signal-map", smap, "--manifest", manifest,
                    "--out", hw, "--logdir", logdir]) == 0
    identity = P.manifest_identity(manifest)
    bad = write_identify_log(str(tmp_path / "bad.log"), identity["signals"],
                             identity["depth"], identity["sampled_bits"] - 1,
                             identity["clock"]["hw"], identity["iice_name"])
    with pytest.raises(ft.HarnessError) as e:
        tc.run_compare(sim, hw, smap, manifest, str(fx / "r.txt"), logdir,
                       use_ncompare=False, identify_log=bad)
    assert "INSTRUMENTATION MISMATCH" in str(e.value)
    # the AGREEING log compares cleanly and the report says so
    good = agreeing_log(tmp_path, identity)
    code, text = tc.run_compare(sim, hw, smap, manifest, str(fx / "r2.txt"),
                                logdir, use_ncompare=False, identify_log=good)
    assert code == 0, text
    assert "built instrumentation     MATCHES the manifest" in text


@needs_verdi
def test_normalise_hw_capture_STAMPS_a_real_capture(fx):
    """The `make hw-fsdb` path: the stamp records the raw capture, the sample
    count actually recovered, how the grid was established, and the caller's ids.
    """
    logdir = str(fx / "logs")
    raw, manifest, smap = make_identify_capture(fx)
    rows = ct.read_signal_map(smap)
    out = str(fx / "hw_norm.fsdb")
    desc = ct.normalise_hw_capture(raw, out, rows, logdir, CAP_DEPTH,
                                   optional=tc.STRUCTURAL_SIGNALS,
                                   manifest=manifest,
                                   ids={"static_id": "0x0EE58A4D",
                                        "rm_id": "0x01000001"})
    assert desc["n_samples"] == CAP_DEPTH
    stamp = P.read_stamp(out)
    assert stamp is not None and desc["provenance"] == stamp
    assert stamp["origin"] == P.ORIGIN_CAPTURE
    assert stamp["produced_by"] == "crop_trace.normalise_hw_capture"
    assert stamp["raw_capture"]["path"] == os.path.abspath(raw)
    assert stamp["raw_capture"]["bytes"] == os.path.getsize(raw)
    assert stamp["capture"]["n_samples"] == CAP_DEPTH
    assert stamp["capture"]["manifest_depth"] == CAP_DEPTH
    assert stamp["capture"]["grid_source"]
    assert stamp["capture"]["raw_time_base"] == [
        CAP_T0, CAP_T0 + (CAP_DEPTH - 1) * CAP_STEP]
    assert stamp["capture"]["sample_period"] == CAP_STEP
    assert stamp["static_id"] == "0x0EE58A4D"
    assert stamp["rm_id"] == "0x01000001"
    assert stamp["manifest"]["signal_set_sha256"] == \
        P.manifest_identity(manifest)["signal_set_sha256"]
    assert P.check_stamp(stamp, P.manifest_identity(manifest), artifact=out) == []
    # ...and the stamp names a DIFFERENT manifest as a conflict
    assert P.check_stamp(stamp, P.manifest_identity(
        str(fx / "signals_provtest.yaml")), artifact=out)


@needs_verdi
def test_normalise_hw_capture_REFUSES_rows_from_another_manifest(fx):
    """The 2026-07-30 incident at its origin: normalising against the wrong map.

    PRE-FIX this produced an FSDB -- either a confidently wrong one, or a
    downstream set-equality error naming selftest signals that were never in the
    capture. Either way the refusal came too late and named the wrong thing.
    The refusal now happens on the PAIRING, before the capture is even read.
    """
    logdir = str(fx / "logs")
    raw, manifest, smap = make_identify_capture(fx)
    rows = ct.read_signal_map(smap)
    with pytest.raises(ft.HarnessError) as e:
        ct.normalise_hw_capture(
            raw, str(fx / "out.fsdb"), rows, logdir, CAP_DEPTH,
            optional=tc.STRUCTURAL_SIGNALS,
            manifest=str(fx / "signals_provtest.yaml"))
    assert "WAS NOT GENERATED FROM THIS MANIFEST" in str(e.value)
    assert not os.path.exists(str(fx / "out.fsdb"))     # refused BEFORE any work


@needs_verdi
def test_normalise_hw_capture_without_a_manifest_stamps_NOTHING(fx):
    """Backward compatibility: every pre-existing caller keeps its behaviour."""
    logdir = str(fx / "logs")
    raw, _manifest, smap = make_identify_capture(fx)
    rows = ct.read_signal_map(smap)
    out = str(fx / "unstamped.fsdb")
    desc = ct.normalise_hw_capture(raw, out, rows, logdir, CAP_DEPTH,
                                   optional=tc.STRUCTURAL_SIGNALS)
    assert desc["provenance"] is None and desc["stamp_file"] is None
    assert P.read_stamp(out) is None
    assert desc["n_samples"] == CAP_DEPTH          # and it still works
