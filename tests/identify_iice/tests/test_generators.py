#!/usr/bin/env python3
"""pytest for the Stream-A manifest loader and the three generators.

Runs with plain `pytest`: no network, no VCS, no Verdi, no Identify, no board.

    python3 -m pytest tests/identify_iice/tests/test_generators.py -q

Scope: everything INTERFACES.md §2/§3.1/§3.2/§3.3 promises, one test per rule,
plus the shipped-manifest round trip and the `golden/` anti-drift comparison that
`make gen` also performs.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
IICE_DIR = os.path.dirname(HERE)
GOLDEN = os.path.join(IICE_DIR, "golden")
sys.path.insert(0, IICE_DIR)

import gen_idc            # noqa: E402
import gen_mangle         # noqa: E402
import gen_shadow         # noqa: E402
import manifest as mf     # noqa: E402

SELFTEST_YAML = os.path.join(IICE_DIR, "signals_selftest.yaml")
NANOSOC_YAML = os.path.join(IICE_DIR, "signals_nanosoc.yaml")


# --------------------------------------------------------------------------- #
# A parameterisable minimal manifest, so each bad case is a one-key mutation.
# --------------------------------------------------------------------------- #
BASE = """\
iice:
  name: {name}
  depth: {depth}
  trigger_time: {trigger_time}
  controller: {controller}
  trigger_conditions: {trigger_conditions}
  trigger_states: {trigger_states}
  clock:
    hw:   {clk_hw}
    sim:  {clk_sim}
    edge: {clk_edge}
{extra_iice}
signals:
  - name:    {s0_name}
    width:   {s0_width}
    hw:      {s0_hw}
    sim:     {s0_sim}
    sample:  {s0_sample}
    trigger: {s0_trigger}
    radix:   {s0_radix}
  - name:    {s1_name}
    width:   {s1_width}
    hw:      {s1_hw}
    sim:     {s1_sim}
    sample:  {s1_sample}
    trigger: {s1_trigger}
    radix:   {s1_radix}

trigger:
  expr: "{expr}"
"""

DEFAULTS = dict(
    name="IICE_T", depth=64, trigger_time="middle", controller="statemachine",
    trigger_conditions=2, trigger_states=2,
    clk_hw="/tb/u_dut/clk", clk_sim="tb.u_dut.clk", clk_edge="positive",
    extra_iice="",
    s0_name="haddr", s0_width=32, s0_hw="/tb/u_dut/haddr",
    s0_sim="tb.u_dut.haddr", s0_sample="true", s0_trigger="false", s0_radix="hex",
    s1_name="htrans", s1_width=2, s1_hw="/tb/u_dut/htrans",
    s1_sim="tb.u_dut.htrans", s1_sample="true", s1_trigger="true", s1_radix="bin",
    expr="htrans == 2'b10",
)


def mk(**kw):
    d = dict(DEFAULTS)
    d.update(kw)
    return BASE.format(**d)


def ok(**kw):
    """Load a mutated manifest that is expected to VALIDATE."""
    return mf.loads(mk(**kw), "test.yaml")


def errs(**kw):
    """Load a mutated manifest expected to FAIL; return its errors as one blob."""
    with pytest.raises(mf.ManifestError) as ei:
        mf.loads(mk(**kw), "test.yaml")
    return "\n".join(ei.value.errors)


def test_base_template_is_valid():
    """The mutation baseline must itself be clean, or every negative test lies."""
    m = ok()
    assert m.iice.name == "IICE_T"
    assert m.signal_names() == ["haddr", "htrans"]


# --------------------------------------------------------------------------- #
# INTERFACES.md §1 — reserved names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reserved", ["trigger_marker", "sample_clk", "sample_index"])
def test_reserved_signal_name_rejected(reserved):
    blob = errs(s0_name=reserved)
    assert "RESERVED" in blob
    assert reserved in blob


def test_reserved_names_tuple_matches_contract():
    assert mf.RESERVED_NAMES == ("trigger_marker", "sample_clk", "sample_index")


# --------------------------------------------------------------------------- #
# INTERFACES.md §2 — signal name rules
# --------------------------------------------------------------------------- #
def test_duplicate_signal_name_rejected():
    assert "duplicate signal name" in errs(s1_name="haddr", s1_trigger="false",
                                           expr="haddr == 32'h4")


@pytest.mark.parametrize("bad", ["HADDR", "1haddr", "_haddr", "h-addr", "haddr.x", ""])
def test_signal_name_pattern_enforced(bad):
    blob = errs(s0_name=bad)
    # An empty value parses as YAML null, so it lands on the "missing" branch.
    assert ("signal name must match" in blob
            or "non-empty string" in blob
            or "missing required key `name`" in blob)


def test_signal_name_pattern_accepts_lower_snake():
    assert ok(s0_name="h_addr0").signal_names() == ["h_addr0", "htrans"]


# --------------------------------------------------------------------------- #
# INTERFACES.md §2 — numeric bounds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [0, -1])
def test_width_must_be_at_least_one(bad):
    assert "width: must be >= 1" in errs(s0_width=bad)


def test_width_one_is_a_scalar():
    assert ok(s0_width=1).by_name("haddr").is_scalar


@pytest.mark.parametrize("bad", [7, 0, -8])
def test_depth_must_be_at_least_eight(bad):
    assert "depth: must be >= 8" in errs(depth=bad)


def test_depth_eight_accepted_and_no_power_of_two_rule():
    assert ok(depth=8).iice.depth == 8
    assert ok(depth=100).iice.depth == 100          # not a power of 2 — allowed


@pytest.mark.parametrize("bad", [1, 0, -2])
def test_trigger_states_below_range_rejected(bad):
    assert "trigger_states: must be >= 2" in errs(trigger_states=bad)


@pytest.mark.parametrize("bad", [11, 16, 100])
def test_trigger_states_above_range_rejected(bad):
    """INTERFACES.md §2/§7 pins 2..10 even though the Identify docs say 2..16."""
    assert "trigger_states: must be <= 10" in errs(trigger_states=bad)


@pytest.mark.parametrize("bad", [1, 11])
def test_trigger_states_error_explains_the_doc_disagreement(bad):
    """The 2..10 bound contradicts the vendor PDF, so the message MUST say why.

    Without this, the next reader opens identify_debug_env_reference.pdf p.51,
    sees "The range is 2 to 16", and files a bug against this validator.
    """
    blob = errs(trigger_states=bad)
    assert "BINARY enforces 2..10" in blob
    assert "identify_debug_env_reference.pdf p.51" in blob
    assert "2 to 16" in blob
    assert "NOT a bug in this validator" in blob


def test_in_range_numeric_errors_stay_terse():
    """The reason tail is only for the surprising bound, not every message."""
    assert "identify_debug_env_reference.pdf" not in errs(depth=2)
    assert "identify_debug_env_reference.pdf" not in errs(s0_width=0)
    assert "identify_debug_env_reference.pdf" not in errs(trigger_conditions=0)


@pytest.mark.parametrize("good", [2, 4, 10])
def test_trigger_states_in_range_accepted(good):
    assert ok(trigger_states=good).iice.trigger_states == good


@pytest.mark.parametrize("bad", [0, -1])
def test_trigger_conditions_must_be_at_least_one(bad):
    assert "trigger_conditions: must be >= 1" in errs(trigger_conditions=bad)


def test_non_integer_numeric_rejected():
    assert "must be an integer" in errs(depth="lots")


# --------------------------------------------------------------------------- #
# INTERFACES.md §2 — enumerations
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["mid", "MIDDLE", "later", "0"])
def test_trigger_time_enum(bad):
    assert "trigger_time: must be one of early|middle|late" in errs(trigger_time=bad)


@pytest.mark.parametrize("good", ["early", "middle", "late"])
def test_trigger_time_enum_accepts(good):
    assert ok(trigger_time=good).iice.trigger_time == good


@pytest.mark.parametrize("bad", ["sm", "state_machine", "None", "simple"])
def test_controller_enum(bad):
    assert "controller: must be one of none|counter|statemachine" in errs(controller=bad)


@pytest.mark.parametrize("good", ["none", "counter", "statemachine"])
def test_controller_enum_accepts(good):
    assert ok(controller=good).iice.controller == good


@pytest.mark.parametrize("bad", ["rising", "falling", "pos", "both"])
def test_clock_edge_enum(bad):
    assert "edge: must be one of positive|negative" in errs(clk_edge=bad)


def test_clock_edge_maps_to_systemverilog():
    assert ok(clk_edge="positive").iice.clock.sv_edge == "posedge"
    assert ok(clk_edge="negative").iice.clock.sv_edge == "negedge"


@pytest.mark.parametrize("bad", ["hexadecimal", "HEX", "binary", "float"])
def test_radix_enum(bad):
    assert "radix: must be one of hex|bin|dec|oct" in errs(s0_radix=bad)


@pytest.mark.parametrize("good", ["hex", "bin", "dec", "oct"])
def test_radix_enum_accepts(good):
    assert ok(s0_radix=good).by_name("haddr").radix == good


# --------------------------------------------------------------------------- #
# INTERFACES.md §2 — path separator discipline
# --------------------------------------------------------------------------- #
def test_sim_path_with_slash_rejected():
    blob = errs(s0_sim="tb/u_dut/haddr")
    assert "sim path must be dot-separated" in blob


def test_clock_sim_path_with_slash_rejected():
    assert "sim path must be dot-separated" in errs(clk_sim="tb/u_dut/clk")


def test_sim_path_must_be_absolute():
    assert "must be ABSOLUTE from the TB top" in errs(s0_sim="haddr")


def test_hw_path_with_dotted_component_rejected():
    blob = errs(s0_hw="/tb.u_dut.haddr")
    assert "hw path must be slash-separated" in blob


def test_hw_path_with_one_dotted_component_rejected():
    """A path that is mostly slashes but has one dotted component is still wrong."""
    assert "hw path must be slash-separated" in errs(s0_hw="/tb/u_dut.haddr")


def test_hw_path_must_be_absolute():
    assert "hw path must be absolute" in errs(s0_hw="tb/u_dut/haddr")


def test_hw_path_empty_component_rejected():
    assert "empty component" in errs(s0_hw="/tb//haddr")


def test_clock_hw_path_checked_too():
    assert "hw path must be absolute" in errs(clk_hw="rp_top/dut_clk")


# --------------------------------------------------------------------------- #
# THE literal-stripping trap — trigger.expr identifier extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("expr,expected", [
    # The canonical case. A naive scan yields b10 / h0000_0000 as identifiers.
    ("htrans == 2'b10 && haddr == 32'h0000_0000", ["htrans", "haddr"]),
    ("haddr == 32'h0000_0040", ["haddr"]),
    ("cnt == 8'd12", ["cnt"]),
    ("v == 4'o17", ["v"]),
    ("v == 2'b1x", ["v"]),
    ("v == 8'hFF", ["v"]),
    ("v == 8'hff", ["v"]),
    # signed literals
    ("v == 4'sd3", ["v"]),
    ("v == 4'Sb0011", ["v"]),
    # unsized based literals
    ("v == 'h1F", ["v"]),
    ("v == 'b1010", ["v"]),
    ("v == 'd99", ["v"]),
    # fill literals
    ("v == '0", ["v"]),
    ("v == '1", ["v"]),
    # bare decimals
    ("v == 12", ["v"]),
    ("v == 1_000", ["v"]),
    # whitespace around the tick, which the LRM permits
    ("v == 32 'h dead_beef".replace(" dead_beef", "dead_beef"), ["v"]),
    # multiple literals and operators
    ("a == 1'b1 && b != 2'b10 || c == 32'hDEAD_BEEF", ["a", "b", "c"]),
    # no literals at all
    ("lockup", ["lockup"]),
    ("a && !b", ["a", "b"]),
    # $-system calls must not leak their name as an identifier
    ("$signed(a) == 4'sd2", ["a"]),
    # string literal contents must not leak
    ('a == 8\'d1', ["a"]),
])
def test_trigger_identifiers_strip_literals(expr, expected):
    assert mf.trigger_identifiers(expr) == expected


def test_strip_literals_removes_the_base_char():
    """The specific false positive: the literal's base char must not survive."""
    stripped = mf.strip_literals("htrans == 2'b10 && haddr == 32'h0000_0040")
    for ghost in ("b10", "h0000_0040", "2'b10", "32'h"):
        assert ghost not in stripped
    assert "htrans" in stripped and "haddr" in stripped


def test_trigger_identifiers_dedup_preserves_order():
    assert mf.trigger_identifiers("b == 1'b0 && a == 1'b1 && b == 1'b1") == ["b", "a"]


def test_sized_literal_is_not_mistaken_after_decimal_pass():
    """Ordering regression: strip sized literals BEFORE bare decimals.

    If decimals went first, `32'h0000_0040` would lose its size and leave a
    stray `'h0000_0040`, and `2'b10` would leave `'b10`.
    """
    assert mf.strip_literals("32'h40").strip() == ""
    assert mf.strip_literals("2'b10").strip() == ""


def test_literals_do_not_cause_unknown_identifier_errors():
    """End-to-end: the real selftest expression must validate, not error."""
    m = ok(s0_trigger="true", expr="htrans == 2'b10 && haddr == 32'h0000_0040")
    assert set(mf.trigger_identifiers(m.trigger_expr)) == {"htrans", "haddr"}


# --------------------------------------------------------------------------- #
# INTERFACES.md §2 — trigger.expr cross-checks
# --------------------------------------------------------------------------- #
def test_unknown_identifier_in_trigger_expr_rejected():
    blob = errs(expr="htrans == 2'b10 && hburst == 3'b000")
    assert "identifier 'hburst' is not a manifest signal name" in blob


def test_trigger_true_signal_absent_from_expr_rejected():
    blob = errs(s0_trigger="true", expr="htrans == 2'b10")
    assert "has `trigger: true` but does not appear in trigger.expr" in blob
    assert "haddr" in blob


def test_trigger_false_signal_may_be_absent_from_expr():
    ok(s0_trigger="false", expr="htrans == 2'b10")


def test_empty_trigger_expr_rejected():
    assert "expr: must be a non-empty string" in errs(expr="")


def test_missing_trigger_section_rejected():
    text = mk().split("trigger:\n  expr:")[0]
    with pytest.raises(mf.ManifestError) as ei:
        mf.loads(text, "test.yaml")
    assert "missing or malformed `trigger:`" in "\n".join(ei.value.errors)


# --------------------------------------------------------------------------- #
# The Identify "omit both flags == BOTH" trap
# --------------------------------------------------------------------------- #
def test_sample_false_trigger_false_rejected():
    blob = errs(s0_sample="false", s0_trigger="false")
    assert "neither -sample nor -trigger means BOTH" in blob


def test_trigger_only_signal_is_allowed():
    m = ok(s0_sample="false", s0_trigger="true",
           expr="htrans == 2'b10 && haddr == 32'h4")
    s = m.by_name("haddr")
    assert (s.sample, s.trigger) == (False, True)


def test_non_boolean_flag_rejected():
    assert "must be a boolean" in errs(s0_sample="yep")


# --------------------------------------------------------------------------- #
# Structural / schema-closure checks
# --------------------------------------------------------------------------- #
def test_unknown_top_level_key_rejected():
    with pytest.raises(mf.ManifestError) as ei:
        mf.loads(mk() + "\nextras:\n  foo: 1\n", "test.yaml")
    assert "unknown key 'extras'" in "\n".join(ei.value.errors)


def test_unknown_iice_key_rejected():
    text = mk().replace("  depth: 64", "  depth: 64\n  compression: 1")
    with pytest.raises(mf.ManifestError) as ei:
        mf.loads(text, "test.yaml")
    assert "unknown key 'compression'" in "\n".join(ei.value.errors)


def test_missing_iice_section_rejected():
    with pytest.raises(mf.ManifestError) as ei:
        mf.loads("signals:\n  - name: a\n", "test.yaml")
    assert "missing or malformed `iice:`" in "\n".join(ei.value.errors)


def test_empty_signals_list_rejected():
    text = re.sub(r"signals:.*?\ntrigger:", "signals:\ntrigger:", mk(), flags=re.S)
    with pytest.raises(mf.ManifestError) as ei:
        mf.loads(text, "test.yaml")
    assert "`signals:` must be a non-empty list" in "\n".join(ei.value.errors)


@pytest.mark.parametrize("bad", ["iice-t", "1ICE", "IICE T"])
def test_iice_name_must_be_an_identifier(bad):
    """It becomes a SystemVerilog module name and a filename."""
    assert "iice.name: must match" in errs(name=bad)


def test_all_errors_reported_at_once():
    """Validation must not stop at the first problem."""
    blob = errs(depth=2, trigger_states=99, s0_radix="nope")
    assert "depth" in blob and "trigger_states" in blob and "radix" in blob


def test_manifest_error_message_names_the_source():
    with pytest.raises(mf.ManifestError) as ei:
        mf.loads(mk(depth=1), "my_manifest.yaml")
    assert "my_manifest.yaml" in str(ei.value)


def test_load_of_missing_file_raises_manifest_error():
    with pytest.raises(mf.ManifestError):
        mf.load(os.path.join(IICE_DIR, "does_not_exist.yaml"))


# --------------------------------------------------------------------------- #
# YAML: PyYAML and the stdlib-only fallback must agree
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not mf.HAVE_PYYAML, reason="PyYAML not installed")
@pytest.mark.parametrize("path", [SELFTEST_YAML, NANOSOC_YAML])
def test_fallback_yaml_matches_pyyaml_on_shipped_manifests(path):
    """The stdlib fallback exists so `make check` needs no pip install. If it
    disagrees with PyYAML the two code paths would validate different data."""
    import yaml
    with open(path) as fh:
        text = fh.read()
    assert yaml.safe_load(text) == mf.parse_yaml_fallback(text)


def test_fallback_yaml_handles_quoted_hash_and_trailing_comments():
    text = ('a: 1        # trailing comment\n'
            'b: "has # inside"\n'
            'c:\n'
            '  - x\n'
            '  - y\n')
    assert mf.parse_yaml_fallback(text) == {
        "a": 1, "b": "has # inside", "c": ["x", "y"]}


def test_fallback_yaml_rejects_flow_collections():
    with pytest.raises(Exception):
        mf.parse_yaml_fallback("clock: {hw: /a, sim: b.c}\n")


def test_fallback_yaml_rejects_tab_indentation():
    with pytest.raises(Exception):
        mf.parse_yaml_fallback("a:\n\tb: 1\n")


# --------------------------------------------------------------------------- #
# INTERFACES.md §3.1 — the .idc
# --------------------------------------------------------------------------- #
MANDATORY_DEVICE_LINES = [
    "device jtagport soft",
    "device xilinxinsertbufg 0",
    "device skewfree 1",
    "device stop_on_signal_not_found 1",
]


def test_idc_has_the_four_mandatory_device_lines_verbatim_and_first():
    text = gen_idc.render(ok())
    assert text.splitlines()[:4] == MANDATORY_DEVICE_LINES


@pytest.mark.parametrize("path", [SELFTEST_YAML, NANOSOC_YAML])
def test_shipped_idc_has_the_four_mandatory_device_lines(path):
    text = gen_idc.render(mf.load(path))
    for line in MANDATORY_DEVICE_LINES:
        assert line in text.splitlines()


def test_idc_iice_new_and_sampler():
    text = gen_idc.render(ok(name="IICE_X", depth=256))
    assert "iice new {IICE_X} -type regular" in text
    assert "iice sampler -iice {IICE_X} -depth 256" in text


def test_idc_clock_line_uses_hw_path_and_edge():
    text = gen_idc.render(ok(clk_edge="negative", clk_hw="/top/my_clk"))
    assert "iice clock   -iice {IICE_T} -edge negative {/top/my_clk}" in text


def test_idc_statemachine_emits_triggerconditions_and_states():
    text = gen_idc.render(ok(controller="statemachine",
                             trigger_conditions=3, trigger_states=4))
    assert "iice controller -iice {IICE_T} statemachine" in text
    assert ("iice controller -iice {IICE_T} -triggerconditions 3 "
            "-triggerstates 4") in text


@pytest.mark.parametrize("ctrl", ["none", "counter"])
def test_idc_non_statemachine_omits_triggerconditions(ctrl):
    text = gen_idc.render(ok(controller=ctrl))
    assert "iice controller -iice {IICE_T} %s" % ctrl in text
    assert "-triggerconditions" not in text
    assert "-triggerstates" not in text


def test_idc_signal_flags_follow_the_manifest():
    m = ok(s0_sample="true", s0_trigger="true",
           s1_sample="true", s1_trigger="false",
           expr="haddr == 32'h4")
    text = gen_idc.render(m)
    assert "signals add -iice {IICE_T} -sample -trigger {/tb/u_dut/haddr}" in text
    assert "signals add -iice {IICE_T} -sample {/tb/u_dut/htrans}" in text


def test_idc_trigger_only_signal_gets_trigger_flag_only():
    m = ok(s0_sample="false", s0_trigger="true",
           s1_trigger="false", expr="haddr == 32'h4")
    assert "signals add -iice {IICE_T} -trigger {/tb/u_dut/haddr}" in gen_idc.render(m)


def test_idc_never_emits_a_flagless_signals_add():
    """`signals add` with neither flag means BOTH in Identify — never emit it."""
    for path in (SELFTEST_YAML, NANOSOC_YAML):
        for line in gen_idc.render(mf.load(path)).splitlines():
            if line.startswith("signals add"):
                assert ("-sample" in line) or ("-trigger" in line), line


def test_idc_omits_triggertime_which_is_a_debugger_only_option():
    """`iice sampler -triggertime` is documented under the DEBUGGER options."""
    for path in (SELFTEST_YAML, NANOSOC_YAML):
        assert "-triggertime" not in gen_idc.render(mf.load(path))


def test_idc_does_not_add_the_sample_clock_as_a_signal():
    """Identify cannot sample the signal used as the sample clock."""
    m = mf.load(SELFTEST_YAML)
    text = gen_idc.render(m)
    assert "signals add" in text
    for line in text.splitlines():
        if line.startswith("signals add"):
            assert "{%s}" % m.iice.clock.hw not in line


def test_idc_one_signals_add_per_manifest_signal():
    m = mf.load(SELFTEST_YAML)
    adds = [l for l in gen_idc.render(m).splitlines() if l.startswith("signals add")]
    assert len(adds) == len(m.signals)


def test_idc_ends_with_a_newline():
    assert gen_idc.render(ok()).endswith("\n")


# --------------------------------------------------------------------------- #
# INTERFACES.md §3.2 — the shadow module
# --------------------------------------------------------------------------- #
def test_shadow_header_names_the_source_manifest_and_forbids_editing():
    m = mf.loads(mk(), "signals_selftest.yaml")
    first = gen_shadow.render(m).splitlines()[0]
    assert first.startswith("// GENERATED")
    assert "gen_shadow.py" in first
    assert "signals_selftest.yaml" in first
    assert "DO NOT EDIT" in first


def test_shadow_module_is_portless_and_named_from_the_manifest():
    text = gen_shadow.render(ok(name="IICE_X"))
    assert "module iice_shadow_IICE_X;" in text
    assert "endmodule" in text
    # portless: no port list, no port directions anywhere.
    assert "module iice_shadow_IICE_X(" not in text
    assert not re.search(r"^\s*(input|output|inout)\b", text, re.M)


def test_shadow_mirrors_are_registered_on_the_manifest_edge():
    text = gen_shadow.render(ok())
    assert "always_ff @(posedge sample_clk) begin" in text
    assert re.search(r"^\s+haddr\s+<= tb\.u_dut\.haddr;", text, re.M)
    assert re.search(r"^\s+htrans\s+<= tb\.u_dut\.htrans;", text, re.M)
    # Never a combinational mirror: that would capture glitches the IICE
    # cannot see (INTERFACES.md §0).
    assert "assign haddr =" not in text


def test_shadow_honours_a_negative_clock_edge():
    text = gen_shadow.render(ok(clk_edge="negative"))
    assert "always_ff @(negedge sample_clk) begin" in text
    assert "posedge" not in text


def test_shadow_declares_widths_from_the_manifest():
    text = gen_shadow.render(ok(s0_width=32, s1_width=2))
    assert re.search(r"^\s+logic \[31:0\]\s+haddr;", text, re.M)
    assert re.search(r"^\s+logic \[1:0\]\s+htrans;", text, re.M)


def test_shadow_declares_a_scalar_without_a_range():
    text = gen_shadow.render(ok(s0_width=1))
    assert re.search(r"^\s+logic\s+haddr;", text, re.M)
    assert "[0:0]" not in text


def test_shadow_taps_the_manifest_clock_sim_path():
    text = gen_shadow.render(ok(clk_sim="tb.u_dut.dut_clk"))
    assert "logic sample_clk;" in text
    assert "assign sample_clk = tb.u_dut.dut_clk;" in text


def test_shadow_trigger_marker_is_registered_off_the_mirrors():
    """The one-edge offset is intentional; the cropper owns it (§3.2)."""
    text = gen_shadow.render(ok(expr="htrans == 2'b10"))
    assert "logic trigger_marker;" in text
    assert "trigger_marker <= (htrans == 2'b10);" in text
    # registered off the MIRROR, not the design path
    assert "trigger_marker <= (tb.u_dut" not in text
    assert "cropper owns it" in text or "cropper" in text


def test_shadow_emits_the_trigger_expr_verbatim():
    expr = "htrans == 2'b10 && haddr == 32'h0000_0040"
    text = gen_shadow.render(ok(s0_trigger="true", expr=expr))
    assert "trigger_marker <= (%s);" % expr in text


def test_shadow_has_both_plusargs_and_a_default_fsdb_name():
    text = gen_shadow.render(ok())
    assert '$value$plusargs("iice_fsdb=%s", f)' in text
    assert '$test$plusargs("iice_shadow")' in text
    assert '"sim_iice.fsdb"' in text
    assert "$fsdbDumpfile(f);" in text


def test_shadow_dumpvars_targets_its_own_scope():
    text = gen_shadow.render(ok(name="IICE_X"))
    assert "$fsdbDumpvars(0, iice_shadow_IICE_X);" in text


def test_shadow_dump_is_inside_an_initial_block():
    text = gen_shadow.render(ok())
    initial = text.split("initial begin", 1)[1]
    assert "$fsdbDumpfile" in initial
    assert "$fsdbDumpvars" in initial


def test_shadow_declares_the_depth_localparam():
    assert "localparam int IICE_DEPTH = 512;" in gen_shadow.render(ok(depth=512))


def test_shadow_mirrors_every_manifest_signal_including_sample_only():
    m = mf.load(SELFTEST_YAML)
    text = gen_shadow.render(m)
    for s in m.signals:
        assert re.search(r"^\s+%s\s+<= %s;" % (s.name, re.escape(s.sim)), text, re.M)


def test_shadow_does_not_mirror_reserved_names_as_design_signals():
    text = gen_shadow.render(mf.load(SELFTEST_YAML))
    assert "sample_index" not in text
    # sample_clk and trigger_marker are synthesised, never <= a design path
    assert not re.search(r"^\s+sample_clk\s+<=", text, re.M)
    assert not re.search(r"trigger_marker\s+<= tb\.", text, re.M)


# --------------------------------------------------------------------------- #
# INTERFACES.md §3.3 — signal_map.tsv
# --------------------------------------------------------------------------- #
def test_tsv_header_is_exact():
    text = gen_mangle.render(ok())
    assert text.splitlines()[0] == "name\twidth\tradix\thw_path\tsim_path"


@pytest.mark.parametrize("path", [SELFTEST_YAML, NANOSOC_YAML])
def test_tsv_row_count_is_signals_plus_two_reserved(path):
    m = mf.load(path)
    lines = gen_mangle.render(m).splitlines()
    assert len(lines) == len(m.signals) + 3          # header + signals + 2 reserved


@pytest.mark.parametrize("path", [SELFTEST_YAML, NANOSOC_YAML])
def test_tsv_every_row_has_five_tab_separated_fields(path):
    for line in gen_mangle.render(mf.load(path)).splitlines():
        assert len(line.split("\t")) == 5, line


def test_tsv_trigger_marker_row_is_derived_on_both_sides():
    rows = {r[0]: r for r in gen_mangle.rows(ok())[1:]}
    assert rows["trigger_marker"] == ("trigger_marker", "1", "bin",
                                      "__DERIVED__", "__DERIVED__")


def test_tsv_sample_clk_row_carries_the_real_clock_paths():
    rows = {r[0]: r for r in gen_mangle.rows(ok())[1:]}
    assert rows["sample_clk"] == ("sample_clk", "1", "bin",
                                  "/tb/u_dut/clk", "tb.u_dut.clk")


def test_tsv_reserved_rows_come_last_and_in_contract_order():
    names = [r[0] for r in gen_mangle.rows(mf.load(SELFTEST_YAML))[1:]]
    assert names[-2:] == ["trigger_marker", "sample_clk"]


def test_tsv_has_no_sample_index_row():
    """`sample_index` is a reserved NAME, not a traced signal (§1/§3.3)."""
    assert "sample_index" not in gen_mangle.render(mf.load(SELFTEST_YAML))


def test_tsv_signal_rows_are_in_manifest_order():
    m = mf.load(SELFTEST_YAML)
    names = [r[0] for r in gen_mangle.rows(m)[1:1 + len(m.signals)]]
    assert names == m.signal_names()


def test_tsv_carries_width_and_radix_from_the_manifest():
    rows = {r[0]: r for r in gen_mangle.rows(ok(s0_width=32, s0_radix="hex"))[1:]}
    assert rows["haddr"][1] == "32"
    assert rows["haddr"][2] == "hex"


def test_tsv_leaf_names_match_the_shadow_mirror_names():
    """§1: the whole point is that both sides use the manifest name verbatim."""
    m = mf.load(SELFTEST_YAML)
    sv = gen_shadow.render(m)
    for row in gen_mangle.rows(m)[1:]:
        assert re.search(r"\b%s\b" % re.escape(row[0]), sv), row[0]


# --------------------------------------------------------------------------- #
# Round trip: both shipped manifests load, and all three generators run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [SELFTEST_YAML, NANOSOC_YAML])
def test_shipped_manifest_loads(path):
    m = mf.load(path)
    assert m.signals
    assert m.iice.name


@pytest.mark.parametrize("path", [SELFTEST_YAML, NANOSOC_YAML])
def test_shipped_manifest_generates_all_three_artifacts(path, tmp_path):
    out = str(tmp_path / "sub" / "dir")            # also proves --out is created
    assert gen_idc.main(["--manifest", path, "--out", out]) == 0
    assert gen_shadow.main(["--manifest", path, "--out", out]) == 0
    assert gen_mangle.main(["--manifest", path, "--out", out]) == 0
    m = mf.load(path)
    for f in ("%s.idc" % m.iice.name, "%s.sv" % m.shadow_module, "signal_map.tsv"):
        p = os.path.join(out, f)
        assert os.path.isfile(p) and os.path.getsize(p) > 0, f


def test_selftest_manifest_matches_the_stream_b_dut_convention():
    """Coordinated by convention (INTERFACES.md §5 splits the files): TB top
    `tb`, DUT instance `tb.u_dut`. If Stream B renames either, this fails here
    rather than as a mysterious hierarchical-reference error under VCS."""
    m = mf.load(SELFTEST_YAML)
    assert m.iice.clock.sim == "tb.u_dut.clk"
    assert set(m.signal_names()) == {
        "haddr", "htrans", "hwrite", "hrdata", "hready", "state", "lockup"}
    for s in m.signals:
        assert s.sim == "tb.u_dut.%s" % s.name
    widths = {s.name: s.width for s in m.signals}
    assert widths == {"haddr": 32, "htrans": 2, "hwrite": 1, "hrdata": 32,
                      "hready": 1, "state": 3, "lockup": 1}


def test_selftest_manifest_is_the_fast_ci_configuration():
    m = mf.load(SELFTEST_YAML)
    assert m.iice.depth == 64
    assert m.iice.trigger_time == "middle"
    assert m.iice.controller == "statemachine"
    assert m.iice.trigger_conditions == 2
    assert m.iice.trigger_states == 2


def test_nanosoc_manifest_probes_the_cpu_ahb_boundary_plus_status():
    m = mf.load(NANOSOC_YAML)
    assert set(m.signal_names()) == {
        "haddr", "htrans", "hprot", "hready", "hwrite", "hresp", "hrdata",
        "hwdata", "lockup", "sleeping", "sysresetreq", "hresetn", "remap_ctrl"}
    # Verified against slcorem0.v:46,53-63,70-73 — the slcorem0 ports are upper
    # case, and lockup is CORE_LOCKUP not LOCKUP.
    assert m.by_name("haddr").sim.endswith("u_cpu_0.HADDR")
    assert m.by_name("lockup").sim.endswith("u_cpu_0.CORE_LOCKUP")
    assert m.by_name("hresetn").sim.endswith("u_cpu_0.SYS_HRESETn")
    # sim hierarchy = the flash-boot bench's cocotb TOPLEVEL. Everything is a
    # port of the CORE except remap_ctrl, which is a port of the SUBSYSTEM one
    # level up (nanosoc_ss_cpu.sv:52).
    for s in m.signals:
        assert s.sim.startswith("micropython_flash_boot_tb.u_nanosoc.u_ss_cpu.")
    for s in m.signals:
        if s.name != "remap_ctrl":
            assert s.sim.startswith(
                "micropython_flash_boot_tb.u_nanosoc.u_ss_cpu.u_cpu_0."), s.sim
    assert m.by_name("remap_ctrl").sim == (
        "micropython_flash_boot_tb.u_nanosoc.u_ss_cpu.sys_remap_ctrl")


def test_nanosoc_manifest_can_separate_a_fetch_from_a_data_access():
    """The retarget's load-bearing signal (2026-07-29).

    A Cortex-M0 has ONE AHB-Lite master (slcorem0.v:53-63), so `haddr` carries
    fetch AND data addresses on the same net. HPROT[0] is the only thing that
    tells them apart — 0 = opcode fetch, 1 = data access (cm0_core.v:52
    "bus is data not instruction"; cm0_matrix.v:71,199,340). Without `hprot` a
    trace of `haddr` cannot be read as a PC history at all, which is the entire
    purpose of this manifest. `hready` must be there too or a wait-stated
    transfer reads as the same address fetched repeatedly.
    """
    m = mf.load(NANOSOC_YAML)
    hprot = m.by_name("hprot")
    assert hprot.width == 4
    assert hprot.hw.endswith("/HPROT")
    assert hprot.sample is True
    assert m.by_name("hready").sample is True
    # `_ic_` is a nanosoc_gen collision-rename ("bus matrix internal",
    # toplevel.py:485-504), NOT "instruction/code". There is no code-bus probe to
    # be had, so no path may pretend there is one.
    for s in m.signals:
        assert "_ic_" not in s.hw and "_ic_" not in s.sim


def test_nanosoc_trigger_is_arm_and_go_and_cheap():
    """The trigger must fire without knowing the loop address.

    We are hunting an unknown loop PC, so an address trigger is unwritable and a
    fault trigger (lockup / hresp / sysresetreq) would never fire on a core that
    is reported as not faulting — yielding NO capture, the worst outcome for a
    scarce board window. So: fire on the first ACTIVE transfer after arming
    (HTRANS[1] covers NONSEQ 10 and SEQ 11, excludes IDLE 00 and BUSY 01) and let
    the endlessness of the loop do the work. Every fault signal is SAMPLED
    instead, so one capture tests all of those hypotheses at once.

    `trigger_time: early` follows: an arm-and-go trigger has no pre-trigger
    history, so `middle`/`late` would present pre-arm BRAM contents as data.
    """
    m = mf.load(NANOSOC_YAML)
    assert m.iice.trigger_time == "early"
    assert m.trigger_expr == "htrans[1] == 1'b1"
    # Exactly one trigger signal, and it is the 2-bit one: trigger comparators
    # are the expensive part of an IICE (the previous set spent 35 bits on them).
    trig = [s for s in m.signals if s.trigger]
    assert [s.name for s in trig] == ["htrans"]
    assert sum(s.width for s in trig) == 2
    # No fault signal may be the trigger, or a non-faulting core gives no data.
    for name in ("lockup", "hresp", "sysresetreq"):
        assert m.by_name(name).trigger is False
        assert m.by_name(name).sample is True


def test_nanosoc_sample_buffer_fits_the_rp_bram_budget():
    """113 bits x 1024 = 4 RAMB36-equivalents against 123.5 free BRAM tiles.

    IDENTIFY_IICE_DFX_PLAN.md:26 records 123.5 free BRAM tiles and 32,750 free
    LUTs in the RP; :82 records the shipped IICE build at 16.32% BRAM. Depth is
    held at 1024 because that is the configuration every measured fact in
    INTERFACES.md §7 was taken at (the captured hardware FSDB has exactly 1024
    timestamps, and `write fsdb -range {0 1023}` is the command that works).
    """
    m = mf.load(NANOSOC_YAML)
    assert m.iice.depth == 1024
    width = sum(s.width for s in m.signals)
    assert width == 113
    bram36 = -(-(width * m.iice.depth) // 36864)      # ceil
    assert bram36 == 4
    assert bram36 < 123


def test_nanosoc_hw_paths_follow_the_settled_identify_convention():
    """Regression guard for the `hw:` path convention (Stream E, 2026-07-29).

    Two rules that both look like redundant verbosity and are not:

      * The TOP MODULE NAME NEVER APPEARS. identify_debug_env_reference.pdf p.14:
        "the top-level design unit is represented by the initial '/'". A port of
        the synthesis top is `/dut_clk`, NOT `/rp_nanosoc_iice_core/dut_clk` and
        NOT `/rp_nanosoc_wrapper/dut_clk` (the earlier wrong guess).
      * The DUT sits one level down at `u_rm`, because the Identify/Synplify top
        is `rp_nanosoc_iice_core` and `device jtagport soft` adds four TAP ports
        to it — so the 35-port DFX contract is met by a shim ABOVE the synthesis
        top, and probes resolve against the core.

    Anyone "simplifying" the /u_rm/ prefix away breaks every hardware probe with
    a `stop_on_signal_not_found` abort at instrumentation time. Fail here first.
    """
    m = mf.load(NANOSOC_YAML)

    # Sample clock: a port of the synthesis top => bare leaf, no instance prefix.
    assert m.iice.clock.hw == "/dut_clk"

    for s in m.signals:
        # Every probe is inside the CPU subsystem. Most are ports of the core;
        # remap_ctrl is a port of the subsystem itself (nanosoc_ss_cpu.sv:52).
        assert s.hw.startswith("/u_rm/u_nanosoc/u_ss_cpu/"), s.hw
        # The top module name must appear nowhere in any path.
        for top in ("rp_nanosoc_iice_core", "rp_nanosoc_iice_shim",
                    "rp_nanosoc_wrapper"):
            assert top not in s.hw, "%s must not appear in %s" % (top, s.hw)
        # `u_core` lives ABOVE the synthesis top, so it must not appear either.
        assert "u_core" not in s.hw, s.hw

    # Case sensitivity matters to Identify: the slcorem0 ports are upper case.
    assert m.by_name("haddr").hw.endswith("/HADDR")
    assert m.by_name("lockup").hw.endswith("/CORE_LOCKUP")
    assert m.by_name("hresetn").hw.endswith("/SYS_HRESETn")

    # The six paths a real Identify run has already RESOLVED with
    # `stop_on_signal_not_found 1` active, per
    # fpga/rp/nanosoc_iice/build/rev_1_identify/identify.log:36-59 + "exit
    # status=0". These must not drift: they are the only tool-confirmed evidence
    # that the /u_rm/ convention and the upper-case port names are right.
    resolved = {
        "haddr":  "/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HADDR",
        "htrans": "/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HTRANS",
        "hwrite": "/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HWRITE",
        "hrdata": "/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HRDATA",
        "hready": "/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HREADY",
        "lockup": "/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/CORE_LOCKUP",
    }
    for name, path in resolved.items():
        assert m.by_name(name).hw == path


def test_nanosoc_hw_and_sim_hierarchies_are_deliberately_different_shapes():
    """The sim bench has no RM wrapper and no Identify core, so it has no `u_rm`.

    This is why the manifest carries BOTH paths per signal rather than deriving
    one from the other by a dot/slash swap (which is what the selftest manifest
    can get away with).
    """
    m = mf.load(NANOSOC_YAML)
    for s in m.signals:
        assert "u_rm" in s.hw
        assert "u_rm" not in s.sim
        # A naive slash<->dot transform would therefore be WRONG here.
        assert s.hw.replace("/", ".").lstrip(".") != s.sim


# --------------------------------------------------------------------------- #
# golden/ anti-drift — the same comparison `make gen` performs
# --------------------------------------------------------------------------- #
# (relative path under golden/, source manifest, generator module)
# Mirrors the six generator invocations in Makefile's `gen` target: all three
# artifacts for the selftest manifest into $(BUILD), and all three for the
# nanosoc manifest into $(BUILD)/nanosoc. The nanosoc side is never simulated in
# CI, so these goldens are the ONLY thing that would catch a nanosoc-only
# generator regression before board day.
GOLDEN_FILES = [
    ("IICE_SELFTEST.idc", SELFTEST_YAML, gen_idc),
    ("iice_shadow_IICE_SELFTEST.sv", SELFTEST_YAML, gen_shadow),
    ("signal_map.tsv", SELFTEST_YAML, gen_mangle),
    (os.path.join("nanosoc", "IICE_CPU.idc"), NANOSOC_YAML, gen_idc),
    (os.path.join("nanosoc", "iice_shadow_IICE_CPU.sv"), NANOSOC_YAML, gen_shadow),
    (os.path.join("nanosoc", "signal_map.tsv"), NANOSOC_YAML, gen_mangle),
]


@pytest.mark.parametrize("rel,manifest_path,gen", GOLDEN_FILES)
def test_golden_matches_generated(rel, manifest_path, gen):
    """`make gen` fails on any difference; catch it here too, without make."""
    path = os.path.join(GOLDEN, rel)
    assert os.path.isfile(path), "missing golden: %s" % rel
    with open(path) as fh:
        committed = fh.read()
    assert gen.render(mf.load(manifest_path)) == committed, (
        "golden/%s is stale — re-run `make -C tests/identify_iice gen`" % rel)


def test_golden_dir_contains_exactly_what_make_gen_writes():
    """`make gen` diffs every file under golden/ against $(BUILD)/<same path>.

    A golden with no corresponding build output is an unconditional GOLDEN DRIFT
    failure, and a generated artifact with no golden silently loses drift
    protection. So the set must match the Makefile's SIX generator invocations
    exactly — three for signals_selftest.yaml into $(BUILD), three for
    signals_nanosoc.yaml into $(BUILD)/nanosoc.
    """
    found = set()
    for root, _dirs, files in os.walk(GOLDEN):
        for f in files:
            found.add(os.path.relpath(os.path.join(root, f), GOLDEN))
    assert found == set(rel for rel, _m, _g in GOLDEN_FILES)


def test_every_manifest_and_generator_pair_has_a_golden():
    """Both manifests x all three generators = six goldens, none missed."""
    pairs = set((m, g.__name__) for _rel, m, g in GOLDEN_FILES)
    expected = set((m, g) for m in (SELFTEST_YAML, NANOSOC_YAML)
                   for g in ("gen_idc", "gen_shadow", "gen_mangle"))
    assert pairs == expected


def test_nanosoc_goldens_are_in_their_own_subdir():
    """They must NOT collide with the selftest goldens: both manifests produce a
    file called signal_map.tsv, so the nanosoc set has to live under nanosoc/."""
    rels = [rel for rel, m, _g in GOLDEN_FILES if m == NANOSOC_YAML]
    assert len(rels) == 3
    for rel in rels:
        assert rel.startswith("nanosoc" + os.sep), rel


# =========================================================================== #
# Optional IICE sampler options (qualified sampling / compression / always-armed)
# =========================================================================== #
# Build-time only: the DEBUGGER shell rejects -depth and -buffertype (measured
# 2026-07-30), so the .idc is the only place these can be set. Default OFF so
# every pre-existing manifest keeps its exact meaning -- which is also why the
# committed goldens do not move.

def _idc_for(*opts):
    """Render the .idc for a manifest with the given optional iice: keys set."""
    extra = "".join("  %s: true\n" % o for o in opts)
    return gen_idc.render(ok(extra_iice=extra.rstrip("\n")))


def test_sampler_options_are_absent_by_default():
    """Default OFF. This is what keeps the goldens stable."""
    idc = _idc_for()
    assert "-depth 64" in idc
    for flag in ("-qualified_sampling", "-compression", "-always_armed"):
        assert flag not in idc, flag


def test_qualified_sampling_emits_on_the_sampler_line():
    idc = _idc_for("qualified_sampling")
    sampler = [l for l in idc.splitlines() if l.startswith("iice sampler")]
    assert "-qualified_sampling 1" in sampler[0]
    # ONE sampler line, not a second `iice sampler` that would reset the first
    assert len(sampler) == 1


def test_compression_and_always_armed_emit_together():
    idc = _idc_for("data_compression", "always_armed")
    line = [l for l in idc.splitlines() if l.startswith("iice sampler")][0]
    assert "-compression 1" in line and "-always_armed 1" in line


def test_qualified_sampling_WITHOUT_a_trigger_signal_is_REJECTED():
    """Qualified sampling gates the buffer on the TRIGGER condition, so with no
    signal instrumented for trigger it would store everything or nothing."""
    blob = errs(extra_iice="  qualified_sampling: true", s1_trigger="false")
    assert "qualified_sampling is on but no signal has `trigger: true`" in blob


def test_qualified_sampling_accepted_when_a_trigger_signal_exists():
    m = ok(extra_iice="  qualified_sampling: true")   # s1_trigger defaults true
    assert m.iice.qualified_sampling is True
    assert m.iice.data_compression is False


def test_a_TYPO_in_a_sampler_key_is_still_rejected():
    """The schema stays closed: adding three keys must not open it up."""
    blob = errs(extra_iice="  qualifed_sampling: true")
    assert "unknown key 'qualifed_sampling'" in blob
