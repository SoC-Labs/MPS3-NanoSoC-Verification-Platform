"""Static defence of the STREAM B <-> STREAM A contract. No simulator needed.

OWNED BY STREAM B (INTERFACES.md §5).

Stream A's ``signals_selftest.yaml`` addresses the DUT by the hierarchical paths
``tb.u_dut.<signal>``, and ``gen_shadow.py`` emits those verbatim as absolute
hierarchical references inside a portless module. So the TB top name, the DUT
instance name and the eight probe signal names are a *cross-file, cross-stream*
contract with no compiler enforcing it from this side -- renaming any of them
here produces an elaboration error in a GENERATED file, which is a confusing
place to discover it, or (worse, if the generator is regenerated at the same
time) a silently different probe set.

These tests are deliberately parse-only so they run in CI without a VCS licence,
next to Stream A's ``test_generators.py``. The real behavioural gate is
``make -C tests/identify_iice sim-selftest`` / ``sim-fsdb``.
"""

import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
DUT_SV = HERE / "selftest_dut.sv"
TB_SV = HERE / "tb_selftest.sv"
MAKEFILE_SIM = HERE / "Makefile.sim"
RUN_SH = HERE / "run_selftest.sh"

# INTERFACES.md-frozen: name -> width. Stream A references tb.u_dut.<name>.
PROBE_SIGNALS = {
    "clk": 1,
    "haddr": 32,
    "htrans": 2,
    "hwrite": 1,
    "hrdata": 32,
    "hready": 1,
    "state": 3,
    "lockup": 1,
}

SHADOW_MODULE = "iice_shadow_IICE_SELFTEST"


def _strip_comments(text):
    """Remove // and /* */ comments so prose cannot satisfy a test."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


@pytest.fixture(scope="module")
def dut_code():
    return _strip_comments(DUT_SV.read_text())


@pytest.fixture(scope="module")
def tb_code():
    return _strip_comments(TB_SV.read_text())


# --------------------------------------------------------------------------
# files exist
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", [DUT_SV, TB_SV, MAKEFILE_SIM, RUN_SH])
def test_stream_b_files_exist(path):
    assert path.is_file(), "missing Stream B deliverable: %s" % path
    assert path.stat().st_size > 0, "empty: %s" % path


def test_run_selftest_is_executable():
    assert RUN_SH.stat().st_mode & 0o111, "%s must be executable" % RUN_SH


# --------------------------------------------------------------------------
# the frozen names
# --------------------------------------------------------------------------
def test_tb_top_module_is_named_tb(tb_code):
    """Stream A hardcodes paths rooted at `tb`."""
    tops = re.findall(r"^\s*module\s+(\w+)", tb_code, flags=re.M)
    assert tops, "no module declaration found in %s" % TB_SV
    assert "tb" in tops, (
        "the TB top must be named exactly `tb` (found %r). Stream A's manifest "
        "uses absolute paths tb.u_dut.* -- renaming this breaks the generated "
        "shadow module, not this file." % (tops,)
    )


def test_dut_instance_is_named_u_dut(tb_code):
    """Stream A hardcodes tb.u_dut.<signal>."""
    m = re.search(r"\bselftest_dut\s+(\w+)\s*\(", tb_code)
    assert m, "no selftest_dut instantiation found in %s" % TB_SV
    assert m.group(1) == "u_dut", (
        "the DUT instance must be named exactly `u_dut` (found %r). Stream A's "
        "manifest uses tb.u_dut.* absolute paths." % m.group(1)
    )


def test_dut_module_is_named_selftest_dut(dut_code):
    tops = re.findall(r"^\s*module\s+(\w+)", dut_code, flags=re.M)
    assert "selftest_dut" in tops, "expected `module selftest_dut`, found %r" % (tops,)


@pytest.mark.parametrize("name,width", sorted(PROBE_SIGNALS.items()))
def test_probe_signal_declared_at_instance_level(dut_code, name, width):
    """Each probe must exist in selftest_dut's own scope at the frozen width.

    A probe declared one level down (inside a sub-instance) would make
    tb.u_dut.<name> an invalid reference even though the name is "present".
    """
    if width == 1:
        pat = r"\b(?:input|output|inout)?\s*(?:wire|reg|logic|bit)\b(?!\s*\[)[^;,)]*?\b%s\b" % re.escape(name)
    else:
        pat = r"\b(?:input|output|inout)?\s*(?:wire|reg|logic|bit)\s*\[\s*%d\s*:\s*0\s*\][^;,)]*?\b%s\b" % (
            width - 1,
            re.escape(name),
        )
    assert re.search(pat, dut_code), (
        "probe signal `%s` (width %d) is not declared at the top level of "
        "selftest_dut. This list is FROZEN in INTERFACES.md; Stream A "
        "references tb.u_dut.%s." % (name, width, name)
    )


def test_no_probe_signal_uses_a_reserved_name():
    """INTERFACES.md §1 reserves three leaf names for the harness."""
    reserved = {"trigger_marker", "sample_clk", "sample_index"}
    clash = reserved & set(PROBE_SIGNALS)
    assert not clash, "probe set collides with reserved names: %r" % (clash,)


# --------------------------------------------------------------------------
# the trigger condition has to be reachable
# --------------------------------------------------------------------------
def test_dut_can_reach_the_selftest_trigger_address(dut_code):
    """0x40 must be inside the address plan, or the trigger can never fire."""
    assert re.search(r"32'h0000_0000|PH1_BASE", dut_code), (
        "the DUT needs a phase based at 0x0 for haddr==0x40 to be reachable"
    )


def test_tb_checks_the_trigger_fires_exactly_once(tb_code):
    """The cropper keys the window on the marker; several markers are ambiguous."""
    assert "32'h0000_0040" in tb_code, (
        "tb_selftest.sv must watch for haddr==32'h0000_0040 (the selftest trigger)"
    )
    assert re.search(r"trig_hits\s*==\s*1", tb_code), (
        "the TB must assert the trigger condition occurs EXACTLY once"
    )


# --------------------------------------------------------------------------
# the X-masking rule must be exercised INSIDE the cropped window
# --------------------------------------------------------------------------
def test_dut_injects_a_mid_run_x_region(dut_code):
    """A reset-phase-only X region is cropped away and proves nothing.

    The first version of this bench injected X only before the first read
    response (cycles ~0-12). The cropper keeps `depth` samples centred on the
    trigger -- [117..180] for depth 64 with the trigger at cycle 149 -- so the
    live compare report read "X-masked samples 0 (0.00%)" and the one-way
    sim-X-vs-hardware-0/1 rule of INTERFACES.md §4 never executed on real FSDB
    data. A second X region inside the window is what fixes that.
    """
    assert "beat_no_response" in dut_code, (
        "selftest_dut.sv must inject a mid-run X region (beat_no_response()), "
        "not only the reset-phase X on hrdata -- the reset X is cropped away"
    )
    assert re.search(r"hrdata\s*<=\s*32'hxxxx_xxxx", dut_code, flags=re.I), (
        "the no-response beats must actually drive hrdata to X"
    )


def test_tb_asserts_an_x_sample_lands_in_the_cropped_window(tb_code):
    """Arranging it is not enough; the TB has to check it, or it rots again."""
    assert "IICE_DEPTH_HINT" in tb_code, (
        "the TB needs the manifest depth to reconstruct the cropped window"
    )
    assert re.search(r"ok_x_in_window", tb_code), (
        "the TB must compute whether a sim-X sample falls inside the cropped window"
    )
    # and it must be part of the verdict, not merely printed
    verdict_expr = re.search(
        r'VERDICT=%s"\s*,\s*\((?P<expr>.*?)\)\s*\?\s*"PASS"', tb_code, flags=re.S
    )
    assert verdict_expr, "could not locate the VERDICT conjunction in tb_selftest.sv"
    assert "ok_x_in_window" in verdict_expr.group("expr"), (
        "ok_x_in_window must be part of the VERDICT conjunction, not just printed"
    )


def test_tb_prints_the_house_verdict_line(tb_code):
    """`make` gates on `grep -q VERDICT=PASS`, as every other bench in tests/ does."""
    assert "VERDICT=PASS" in tb_code or 'VERDICT=%s' in tb_code, (
        "tb_selftest.sv must print a VERDICT=PASS/FAIL line"
    )
    assert "VERDICT=FAIL" in tb_code, "tb_selftest.sv must be able to print VERDICT=FAIL"


def test_tb_is_bounded_and_cannot_hang(tb_code):
    assert re.search(r"MAX_CYCLES", tb_code), "the TB needs a cycle bound"
    assert "$finish" in tb_code, "the TB must $finish"


# --------------------------------------------------------------------------
# the shadow instantiation must be compile-time guarded
# --------------------------------------------------------------------------
def test_shadow_instantiation_is_ifdef_guarded(tb_code):
    """The default build must not reference Stream A's generated module at all.

    Verilog cannot conditionally instantiate on a plusarg, so the guard has to
    be `ifdef IICE_SHADOW`. If it were unguarded, `sim-selftest` would fail to
    compile whenever build/ has not been generated -- coupling this stream to
    Stream A and, worse, dragging FSDB cost into a default gate.
    """
    assert SHADOW_MODULE in tb_code, (
        "tb_selftest.sv must instantiate %s" % SHADOW_MODULE
    )

    guarded = re.search(
        r"`ifdef\s+IICE_SHADOW\b(?P<body>.*?)`endif",
        tb_code,
        flags=re.S,
    )
    assert guarded, "no `ifdef IICE_SHADOW ... `endif block found in tb_selftest.sv"

    # every occurrence of the instantiation must sit inside such a block
    guarded_spans = [
        m.span() for m in re.finditer(r"`ifdef\s+IICE_SHADOW\b.*?`endif", tb_code, flags=re.S)
    ]
    for m in re.finditer(r"\b%s\s+\w+\s*\(" % SHADOW_MODULE, tb_code):
        assert any(lo <= m.start() < hi for lo, hi in guarded_spans), (
            "the %s instantiation at offset %d is NOT inside an "
            "`ifdef IICE_SHADOW block" % (SHADOW_MODULE, m.start())
        )


def test_shadow_is_instantiated_portless(tb_code):
    """INTERFACES.md §3.2: the generated module is PORTLESS -- `u();`, no wiring."""
    m = re.search(r"\b%s\s+(\w+)\s*\(\s*\)\s*;" % SHADOW_MODULE, tb_code)
    assert m, (
        "%s must be instantiated portless, e.g. `%s u_iice_shadow();`"
        % (SHADOW_MODULE, SHADOW_MODULE)
    )


# --------------------------------------------------------------------------
# Makefile.sim
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def makefile_sim():
    return MAKEFILE_SIM.read_text()


@pytest.mark.parametrize("target", ["sim-fsdb", "sim-selftest"])
def test_makefile_sim_defines_target(makefile_sim, target):
    """The integrator-owned Makefile calls these by name."""
    assert re.search(r"^%s\s*:" % re.escape(target), makefile_sim, flags=re.M), (
        "Makefile.sim must define a `%s` target -- tests/identify_iice/Makefile "
        "calls it" % target
    )


def test_makefile_sim_does_not_redefine_clean(makefile_sim):
    """The parent Makefile owns `clean`; a second definition would conflict."""
    assert not re.search(r"^clean\s*:", makefile_sim, flags=re.M), (
        "Makefile.sim must not define `clean` (the parent Makefile owns it); "
        "use `clean-sim`"
    )
    assert re.search(r"^clean-sim\s*:", makefile_sim, flags=re.M)


def test_sim_fsdb_asserts_the_fsdb_was_really_written(makefile_sim):
    """The measured failure mode is exit 0 + VERDICT=PASS + no FSDB.

    Both known causes (deprecated -P against a >=2024.09 Verdi PLI, and
    VERDI_HOME unset at run time) leave the simulation green. So an existence
    and non-empty check is load bearing, and so is deleting any stale FSDB
    before the run -- otherwise the previous run's trace passes the check.
    """
    body = makefile_sim.split("sim-fsdb:", 1)[1]
    body = body.split("\nclean-sim:", 1)[0]
    assert "test -f $(SIM_FSDB)" in body, "sim-fsdb must assert the FSDB exists"
    assert "test -s $(SIM_FSDB)" in body, "sim-fsdb must assert the FSDB is non-empty"
    assert "rm -f $(SIM_FSDB)" in body, (
        "sim-fsdb must delete a stale FSDB before the run, or a failed run "
        "passes on the previous run's trace"
    )


def test_sim_fsdb_passes_the_contract_plusargs(makefile_sim):
    assert "+define+IICE_SHADOW" in makefile_sim
    assert "+iice_shadow" in makefile_sim
    assert "+iice_fsdb=$(SIM_FSDB)" in makefile_sim


def test_sim_fsdb_uses_the_generated_shadow_from_build(makefile_sim):
    assert "SHADOW_SV := $(BUILD)/iice_shadow_IICE_SELFTEST.sv" in makefile_sim, (
        "sim-fsdb must compile the GENERATED shadow from $(BUILD), not a local copy"
    )


def test_sim_fsdb_uses_debug_access_not_deprecated_dash_P(makefile_sim):
    """Measured: `-P novas.tab pli.a` is refused by Verdi >= 2024.09, silently.

    It compiles, runs, exits 0, prints VERDICT=PASS and writes no FSDB. Only
    `-debug_access` + VERDI_HOME works against both Verdi vintages installed
    here.
    """
    assert "-debug_access" in makefile_sim, (
        "the portable FSDB recipe is -debug_access + VERDI_HOME"
    )
    # Only recipe lines matter -- novas.tab is discussed at length in the header
    # prose, which is where that knowledge belongs.
    recipe_lines = [
        ln for ln in makefile_sim.splitlines()
        if ln.startswith("\t") and "novas.tab" in ln
    ]
    assert not recipe_lines, (
        "Makefile.sim recipes must not use the deprecated -P novas.tab route: %r"
        % (recipe_lines,)
    )
