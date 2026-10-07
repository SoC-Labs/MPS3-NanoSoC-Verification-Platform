"""test_boundary_generated.py — the gate on the RP ⇄ shell partition boundary.

The partition boundary (47 ports / 148 bits since the 2026-10 ILA mint's
``dbgbscan`` group; 35 / 136 at the fielded 0x3F1A560F) used to be asserted independently in ~13 places.
Only the RM-wrapper side was gated (``fpga/dfx/pin_check.py`` parsed
``docs/contracts/partition-pins.md`` with a regex and diffed each wrapper); the
SHELL side (``fpga/shell/rp_dut_stub.sv``) was gated by nothing at all. It had
already drifted once — ``src/linux_harness/impl/rp_dut_stub.sv`` carried the
pre-re-mint SWD group for a whole cutover. That 14th copy was DELETED on
2026-09-11 (its one reader, ``build_transplant_phaseB.tcl``, reads the generated
stub now, and that build's own hand-typed ``expected_rp_pins`` conformance list
— the 15th copy, pre-cutover too — became a generated view, and at the 2026-10
widening a PINNED fork-only list, decision 5). What stops a 16th
is ``gen_boundary.check_rp_dut_copies``: every tracked ``module rp_dut`` is
parsed and diffed against the YAML, so a new copy must AGREE. Tested below.

``fpga/shell/boundary.yaml`` is now the one declaration and ``tools/gen_boundary.py``
writes every view. These tests are what makes that binding rather than
aspirational:

  * the YAML really does describe 47 ports / 148 bits — COMPUTED, then compared
    against the ``totals:`` the file asserts and against the two literals the
    2026-10 ILA mint is routed with;
  * every generated view in the tree is byte-identical to a fresh regeneration
    (the round-trip control);
  * a MUTATION control — flip one direction, widen one signal — makes the
    generated stub and the pin_check table CHANGE. A gate that cannot fail is
    not a gate, and the round-trip test alone would still pass if the generator
    ignored the YAML entirely;
  * ``pin_check.py`` still passes on every RM wrapper it discovers, and on the
    generated wrapper skeleton;
  * the decoupler safe-idle clamps in the YAML match ``shell_bd.tcl``'s
    ``dfx_decoupler_0`` — the BD is the authority for what the silicon does.
"""
import copy
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "tools/gen_boundary.py"
BOUNDARY = ROOT / "fpga/shell/boundary.yaml"
SHELL_BD = ROOT / "fpga/shell/bd/shell_bd.tcl"
PIN_CHECK = ROOT / "fpga/dfx/pin_check.py"
SKELETON = ROOT / "fpga/shell/generated/rp_wrapper_skeleton.sv"
STUB = "fpga/shell/rp_dut_stub.sv"
PINCHK = "fpga/dfx/pin_check.py"
PHASEB = "src/linux_harness/impl/build_transplant_phaseB.tcl"

# The minted numbers. The 2026-10 ILA mint (0x72BB0A36, fielded 2026-09-24) is
# placed and routed against exactly this boundary (the previous shell 0x3F1A560F's
# 35/136 + the 12-bit dbgbscan group); every overlay in fpga/dfx/prod/ links
# into it. These literals are deliberately duplicated here rather than read from
# the YAML — the whole point is to catch the YAML changing.
FIELDED_PORTS = 47
FIELDED_BITS = 148
FIELDED_NGPIO = 16


def _load_gen():
    """Import tools/gen_boundary.py by path (tools/ has no package structure)."""
    if not GEN.is_file():
        pytest.fail(f"{GEN} does not exist")
    sys.path.insert(0, str(GEN.parent))
    try:
        spec = importlib.util.spec_from_file_location("gen_boundary", GEN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(GEN.parent))
    return mod


@pytest.fixture(scope="module")
def gen():
    return _load_gen()


@pytest.fixture(scope="module")
def bnd(gen):
    return gen.load(ROOT)


# ── 1. the count, computed ───────────────────────────────────────────────────
def test_yaml_declares_47_ports_148_bits(gen, bnd):
    """COMPUTE the port and bit counts; do not take the YAML's word for them."""
    sigs = list(gen.signals(bnd))
    assert len(sigs) == FIELDED_PORTS, (
        f"{len(sigs)} ports declared, the fielded boundary has {FIELDED_PORTS}")

    assert bnd["ngpio"] == FIELDED_NGPIO
    bits = sum(gen.bits_of(s, bnd["ngpio"]) for s in sigs)
    assert bits == FIELDED_BITS, (
        f"{bits} bits declared, the fielded boundary has {FIELDED_BITS}")

    # ...and the YAML's own asserted totals must agree with the computation.
    assert bnd["totals"]["ports"] == len(sigs)
    assert bnd["totals"]["bits"] == bits


def test_per_group_counts_are_stable(gen, bnd):
    """Per-group shape — a signal moving group is as much a re-key as a new one."""
    got = {g["id"]: (len(g["signals"]),
                     sum(gen.bits_of(s, bnd["ngpio"]) for s in g["signals"]))
           for g in bnd["groups"]}
    assert got == {
        "clkrst": (4, 4),
        "jtag":   (4, 4),
        "dbgbscan": (12, 12),
        "eth":    (9, 11),
        "uart":   (7, 21),
        "status": (3, 34),
        "gpio":   (3, 48),
        "qspi":   (5, 14),
    }


def test_boundary_yaml_is_plain_safe_loadable_yaml():
    """No custom tags, no anchors needing a full loader: `yaml.safe_load` alone."""
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(BOUNDARY.read_text())
    assert isinstance(doc, dict)
    assert set(doc) >= {"version", "ngpio", "stub_rm_id_ascii", "totals",
                        "rm_direction_map", "groups"}


def test_no_duplicate_signal_names(gen, bnd):
    names = [s["name"] for s in gen.signals(bnd)]
    assert len(names) == len(set(names)), "duplicate signal name in boundary.yaml"


# ── 2. the round trip ────────────────────────────────────────────────────────
def test_generated_views_are_fresh(gen):
    """Regenerating every view produces zero diff against the tracked tree."""
    outputs = gen.build(ROOT)
    stale = []
    for rel, want in sorted(outputs.items()):
        p = ROOT / rel
        got = p.read_text() if p.is_file() else ""
        if got != want:
            stale.append(rel)
    assert not stale, (
        f"stale generated view(s): {stale} — re-run python3 tools/gen_boundary.py")


def test_generator_cli_check_exits_zero():
    """The exact invocation check_generated_fresh.py drives."""
    r = subprocess.run([sys.executable, str(GEN), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_generator_lists_every_view():
    r = subprocess.run([sys.executable, str(GEN), "--list"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    listed = set(r.stdout.split())
    assert listed == {
        STUB,
        "fpga/shell/generated/rp_wrapper_skeleton.sv",
        PINCHK,
        "docs/contracts/partition-pins.md",
    }, listed
    # the Linux fork stopped tracking at the 2026-10 widening (decision 5)
    assert PHASEB not in listed


def test_generation_is_idempotent(gen):
    """Generate-twice is generate-once (splicing leaves the fences in place)."""
    once = gen.build(ROOT)
    twice = gen.build(ROOT)
    assert once == twice


# ── 3. the mutation control — a gate that cannot fail is not a gate ──────────
def test_mutating_a_direction_changes_the_generated_views(gen, bnd):
    """Flip `mdc` from RP-drive to shell-drive: the stub and the pin_check
    table MUST both change. Bit count is unaffected, so only a direction-aware
    generator notices — which is exactly the drift that silently bricks an
    overlay."""
    m = copy.deepcopy(bnd)
    sig = next(s for s in gen.signals(m) if s["name"] == "mdc")
    assert sig["dir"] == "I"
    sig["dir"] = "O"
    sig["clamp"] = "none"

    out = gen.build_outputs(ROOT, m)
    assert out[STUB] != (ROOT / STUB).read_text(), \
        "flipping a direction did not change rp_dut_stub.sv"
    assert out[PINCHK] != PIN_CHECK.read_text(), \
        "flipping a direction did not change pin_check.py's table"

    # and concretely: mdc becomes a stub INPUT and loses its tie-off
    assert "input  logic        mdc," in out[STUB]
    assert "assign mdc " not in out[STUB]


def test_mutating_a_width_changes_the_generated_views(gen, bnd):
    """Widen `phy_rmii_rxd` 2 -> 4. This is a re-key: the generated views must
    move, and the recomputed total must stop matching the asserted total."""
    m = copy.deepcopy(bnd)
    sig = next(s for s in gen.signals(m) if s["name"] == "phy_rmii_rxd")
    assert sig["width"] == 2
    sig["width"] = 4

    out = gen.build_outputs(ROOT, m)
    assert out[STUB] != (ROOT / STUB).read_text(), \
        "widening a signal did not change rp_dut_stub.sv"
    assert out[PINCHK] != PIN_CHECK.read_text(), \
        "widening a signal did not change pin_check.py's table"
    assert "input  logic [3:0]  phy_rmii_rxd," in out[STUB]

    # the derived-total assertion in boundary.yaml must reject it outright
    with pytest.raises(gen.genlib.GenError) as e:
        gen.validate_totals(m)
    assert "148" in str(e.value)


def test_mutating_a_clamp_changes_the_stub(gen, bnd):
    """The stub's tie-offs ARE the safe-idle values. Changing qspi_csn's clamp
    to 0 would leave the flash selected during a swap; it must not be silent."""
    m = copy.deepcopy(bnd)
    sig = next(s for s in gen.signals(m) if s["name"] == "qspi_csn")
    assert sig["clamp"] == 1
    sig["clamp"] = 0

    out = gen.build_outputs(ROOT, m)
    assert "assign qspi_csn      = 1'b0;" in out[STUB]
    assert out[STUB] != (ROOT / STUB).read_text()


def test_mutating_the_stub_rm_id_changes_the_stub(gen, bnd):
    m = copy.deepcopy(bnd)
    m["stub_rm_id_ascii"] = "DEAD"
    out = gen.build_outputs(ROOT, m)
    assert "32'h44_45_41_44" in out[STUB]
    assert "32'h53_54_55_42" not in out[STUB]


# ── 4. pin_check still works, and the skeleton conforms ─────────────────────
def test_pin_check_passes_every_discovered_wrapper():
    r = subprocess.run([sys.executable, str(PIN_CHECK)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL WRAPPERS CONFORM" in r.stdout
    assert f"({FIELDED_PORTS} signals, NGPIO={FIELDED_NGPIO})" in r.stdout


def test_pin_check_no_longer_parses_markdown():
    """pin_check must have no RUNTIME dependency on the contract doc, and none
    on a YAML library (the CI image has neither guaranteed).

    The old gate scraped partition-pins.md with a regex on every run: reformat
    the tables and the signal set silently empties, at which point every wrapper
    "conforms" to nothing. The table is now a literal spliced in from
    boundary.yaml, so the check is proven by the module's own state.
    """
    src = PIN_CHECK.read_text()
    assert "BEGIN GENERATED[boundary]" in src
    assert "parse_contract" not in src, "the markdown scraper is still there"
    assert "import yaml" not in src

    sys.path.insert(0, str(PIN_CHECK.parent))
    try:
        spec = importlib.util.spec_from_file_location("pin_check_mod", PIN_CHECK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(PIN_CHECK.parent))

    # importing it populated the table with no file read at all
    assert not hasattr(mod, "parse_contract")
    assert isinstance(mod.CONTRACT, dict) and len(mod.CONTRACT) == FIELDED_PORTS
    assert mod.NGPIO == FIELDED_NGPIO
    assert mod.RM_DIR == {"O": "input", "I": "output"}
    assert mod.BOUNDARY_SOURCE == "fpga/shell/boundary.yaml"


def test_generated_skeleton_conforms_to_the_contract():
    """The skeleton is the thing an RM author starts from. If it does not itself
    pass pin_check, it is a trap."""
    assert SKELETON.is_file(), f"{SKELETON} not generated"
    r = subprocess.run([sys.executable, str(PIN_CHECK), str(SKELETON)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_skeleton_directions_are_inverted(gen, bnd):
    """contract O (shell drives) -> RM input; contract I -> RM output."""
    text = SKELETON.read_text()
    for s in gen.signals(bnd):
        want = bnd["rm_direction_map"][s["dir"]]
        m = re.search(rf"^\s*(input|output)\s+logic\s*(?:\[[^\]]*\])?\s*{s['name']},?\s*$",
                      text, re.M)
        assert m, f"{s['name']} missing from the skeleton"
        assert m.group(1) == want, (
            f"{s['name']}: skeleton says {m.group(1)}, contract "
            f"{s['dir']} requires {want}")


# ── 5. the clamps match the BD, which is what the silicon actually does ─────
def test_clamps_match_shell_bd_decoupler(gen, bnd):
    """fpga/shell/bd/shell_bd.tcl is the authority for the safe-idle values."""
    tcl = SHELL_BD.read_text()
    bd = {}
    for m in re.finditer(
            r"^\s*(\w+)\s*\{\s*ID\s+\d+.*?WIDTH\s+(\d+).*?"
            r"DECOUPLED_VALUE\s+0x([0-9a-fA-F]+)", tcl, re.M):
        bd[m.group(1)] = (int(m.group(2)), int(m.group(3), 16))

    yaml_members = {s["name"]: s for s in gen.signals(bnd)
                    if s["clamp"] != "none"}
    assert set(bd) == set(yaml_members), (
        f"decoupler membership drift: only in BD "
        f"{sorted(set(bd) - set(yaml_members))}, only in YAML "
        f"{sorted(set(yaml_members) - set(bd))}")

    for name, (bd_w, bd_v) in sorted(bd.items()):
        s = yaml_members[name]
        assert gen.bits_of(s, bnd["ngpio"]) == bd_w, (
            f"{name}: BD width {bd_w}, boundary.yaml {s['width']}")
        assert s["clamp"] == bd_v, (
            f"{name}: BD clamps to {bd_v}, boundary.yaml says {s['clamp']}")

    # the one intentional non-zero safe idle, stated positively
    assert yaml_members["qspi_csn"]["clamp"] == 1
    assert [n for n, s in yaml_members.items() if s["clamp"] != 0] == ["qspi_csn"]


def test_shell_drive_legs_are_not_decoupler_members(gen, bnd):
    """Every `O` leg routes straight through — rp_resetn is the isolation."""
    for s in gen.signals(bnd):
        if s["dir"] == "O":
            assert s["clamp"] == "none", \
                f"{s['name']} is shell-driven but declares a decoupler clamp"


# ── 6. the contract doc's prose survived ────────────────────────────────────
@pytest.mark.parametrize("phrase", [
    "**Every crossing is CDC'd on the static side.**",
    "THIS GROUP IS A DELIBERATE, DOCUMENTED EXCEPTION",
    "⚠️ **CORRECTION (2026-07-16).**",
    "## IOB packing note (Z2 lesson, HDPR-29)",
    "TODO — DEFERRED VALIDATION",
    "**Why not a `dut_gpio` tunnel:**",
])
def test_contract_prose_survives_generation(phrase):
    """The generator owns the TABLES only. Everything that carries the reasoning
    is hand-written and must be untouched by a regeneration."""
    assert phrase in (ROOT / "docs/contracts/partition-pins.md").read_text()


# ── 4. no SECOND statement of the boundary may disagree with the YAML ────────
def test_check_rp_dut_copies_finds_the_copies_that_exist(gen, bnd):
    """Not vacuous: it must actually be looking at the two real declarations."""
    gen.check_rp_dut_copies(ROOT, bnd)            # the tree as it stands: passes
    decls = [p for p in ROOT.rglob("*.sv")
             if "build" not in p.parts
             and gen._ports_of_rp_dut(p.read_text(errors="replace")) is not None]
    rels = {p.relative_to(ROOT).as_posix() for p in decls}
    assert {STUB, "fpga/dfx/proof/rp_dut.sv"} <= rels, rels


def test_a_returning_pre_cutover_copy_fails(gen, bnd, tmp_path):
    """MUTATION: restore the deleted 14th copy verbatim and the generator dies.

    ``src/linux_harness/impl/rp_dut_stub.sv`` really did carry
    ``swd_clk``/``swd_dio_o``/``swd_dio_oe``/``swd_dio_i`` after the boundary
    moved to ``jtag_*``, and nothing in the tree could see it. This is that exact
    file, planted in a scratch copy of the repo root.
    """
    (tmp_path / "fpga/shell").mkdir(parents=True)
    shutil.copy(ROOT / STUB, tmp_path / STUB)
    (tmp_path / "revenant").mkdir()
    text = (ROOT / STUB).read_text()
    for a, b in (("jtag_tck", "swd_clk"), ("jtag_tms", "swd_dio_o"),
                 ("jtag_tdi", "swd_dio_oe"), ("jtag_tdo", "swd_dio_i")):
        text = text.replace(a, b)
    (tmp_path / "revenant/rp_dut_stub.sv").write_text(text)
    with pytest.raises(gen.genlib.GenError) as e:
        gen.check_rp_dut_copies(tmp_path, bnd)
    assert "revenant/rp_dut_stub.sv" in str(e.value)
    assert "swd_clk" in str(e.value)


def test_the_phaseb_pin_list_is_pinned_to_the_fork(gen):
    """The 15th copy: the conformance list inside the transplant build itself.

    It was a generated view until the 2026-10 ILA widening. The fork (static
    0x2B082E1B, no XVC, no debug bridge) STOPPED TRACKING the main boundary
    (ILA_MINT_PLAN_2026-09-23.md decision 5), so the list is now pinned by
    hand to the fork's fielded 35 pins: the main boundary minus ``dbgbscan``,
    in contract order. If the main boundary changes any OTHER group this fails,
    and someone has to decide whether the fork follows.
    """
    text = (ROOT / PHASEB).read_text()
    assert "BEGIN GENERATED[boundary-rp-pins]" not in text, \
        "the fork's pin list is fenced as generated again, but nothing owns it"
    pins = re.search(r"set expected_rp_pins \{(.*?)\}", text, re.S).group(1).split()
    bnd = gen.load(ROOT)
    fork = [s["name"] for g in bnd["groups"] if g["id"] != "dbgbscan"
            for s in g["signals"]]
    assert len(pins) == 35
    assert pins == fork
    assert "swd_clk" not in pins, "the phase-B gate is back on the retired group"
    assert not [p for p in pins if p.startswith("dbg_bscan_")]
