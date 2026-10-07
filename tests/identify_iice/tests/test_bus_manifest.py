"""Tests for gen_bus_manifest.py -- the bus probe-manifest generator.

The load-bearing assertion in here is not any single width: it is that every
generated manifest VALIDATES THROUGH THE REAL SCHEMA and renders to a .idc. A
generator that emits something manifest.py rejects is worse than no generator,
because the failure surfaces at instrumentation time on a build you have already
paid for.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_IICE = os.path.dirname(_HERE)
if _IICE not in sys.path:
    sys.path.insert(0, _IICE)

import gen_bus_manifest as gb   # noqa: E402
import gen_idc                  # noqa: E402
import gen_mangle               # noqa: E402
import gen_shadow               # noqa: E402
import manifest as mf           # noqa: E402

ALL_PROTOCOLS = sorted(gb.PROTOCOLS)


def _render(protocol, **kw):
    kw.setdefault("data_width", 64)
    kw.setdefault("addr_width", 32)
    kw.setdefault("id_width", 4)
    prefix = kw.pop("prefix", "")
    qualify_on = kw.pop("qualify_on", None)
    rows = gb.signal_rows(protocol, kw["data_width"], kw["addr_width"],
                          kw["id_width"], prefix=prefix, qualify_on=qualify_on)
    base_expr, sufs = gb.qualifier_for(protocol, qualify_on)
    expr = gb.prefix_expr(base_expr, sufs, prefix)
    return rows, gb.render_yaml(
        protocol, "/u_rm/u_soc/m0", "tb.u_soc.m0", "/dut_clk", "tb.dut_clk",
        rows, "IICE_T", 1024, qualified_sampling=True, expr=expr)


@pytest.mark.parametrize("protocol", ALL_PROTOCOLS)
def test_generated_manifest_validates_and_renders(protocol):
    """The whole point: schema-valid and .idc-renderable, for every protocol."""
    rows, text = _render(protocol)
    m = mf.loads(text, "generated_%s.yaml" % protocol)
    assert len(m.signals) == len(rows)
    assert m.iice.qualified_sampling is True
    # all three generators must accept it, not just the loader
    assert gen_idc.render(m)
    assert gen_shadow.render(m)
    assert gen_mangle.render(m)


@pytest.mark.parametrize("protocol", ALL_PROTOCOLS)
def test_trigger_flags_match_the_qualifier_EXACTLY(protocol):
    """`trigger: true` must be exactly the signals in trigger.expr.

    This is the rule the schema enforces in both directions, and the first
    version of this generator got it wrong -- it marked every handshake signal
    trigger-capable, which the validator rejected with 12 errors. A signal the
    hardware triggers on but the sim shadow does not makes the two windows
    disagree about where they start.
    """
    rows, text = _render(protocol)
    m = mf.loads(text, "t.yaml")
    flagged = {s.name for s in m.signals if s.trigger}
    _expr, sufs = gb.qualifier_for(protocol, None)
    assert flagged == {s.lower() for s in sufs}
    # and cheap: never more than a handful of comparator bits
    assert sum(s.width for s in m.signals if s.trigger) <= 8


@pytest.mark.parametrize("protocol", ALL_PROTOCOLS)
def test_every_named_qualifier_validates(protocol):
    """Each qualifier option must produce a loadable manifest, not just default."""
    for name in gb.QUALIFIERS[protocol]:
        rows, text = _render(protocol, qualify_on=name)
        m = mf.loads(text, "q.yaml")
        _expr, sufs = gb.qualifier_for(protocol, name)
        assert {s.name for s in m.signals if s.trigger} == {s.lower() for s in sufs}


def test_prefix_rewrites_the_expression_too():
    """With --prefix the expr must reference the PREFIXED names or be rejected."""
    rows, text = _render("ahb", prefix="m0_")
    m = mf.loads(text, "p.yaml")
    assert "m0_htrans" in m.trigger_expr and "m0_hready" in m.trigger_expr
    assert all(s.name.startswith("m0_") for s in m.signals)


def test_widths_follow_data_and_id_width():
    rows, _ = _render("axi4", data_width=64, id_width=6)
    by = {r["name"]: r["width"] for r in rows}
    assert by["wdata"] == 64 and by["rdata"] == 64
    assert by["wstrb"] == 8                 # 64/8
    assert by["awid"] == 6 and by["rid"] == 6
    assert by["awlen"] == 8 and by["awsize"] == 3


def test_a_non_byte_multiple_data_width_is_REFUSED():
    """WSTRB has no whole-byte width, so this must fail loudly, not round."""
    with pytest.raises(gb.BusManifestError) as e:
        gb.signal_rows("axi4", 33, 32, 4)
    assert "not a multiple of 8" in str(e.value)


def test_axi4_requires_an_id_width():
    with pytest.raises(gb.BusManifestError) as e:
        gb.signal_rows("axi4", 32, 32, 0)
    assert "--id-width >= 1" in str(e.value)


def test_unknown_protocol_and_qualifier_are_refused():
    with pytest.raises(gb.BusManifestError):
        gb.signal_rows("wishbone", 32, 32, 4)
    with pytest.raises(gb.BusManifestError) as e:
        gb.qualifier_for("ahb", "nonesuch")
    assert "unknown qualifier" in str(e.value)


def test_budget_reports_the_numbers_that_decide_feasibility():
    """Sample bits size the BRAM; readout time decides if it is usable at all."""
    rows = gb.signal_rows("axi4", 64, 32, 4)
    b = gb.budget(rows, 1024)
    assert b["sample_bits"] == sum(r["width"] for r in rows)
    assert b["total_bits"] == b["sample_bits"] * 1024
    # 64-bit AXI4 is ~282 bits/port; ten ports must not fit in a 4.45 Mbit RP by
    # accident, so keep the per-port figure honest
    assert 250 <= b["sample_bits"] <= 320, b["sample_bits"]
    # trigger bits are the expensive resource and must stay tiny
    assert b["trigger_bits"] <= 8
    # readout at the MEASURED host rate must be reported in seconds
    assert b["readout_s_host_xvc"] > 0
    assert b["ramb36_floor"] >= 1


def test_ten_axi4_ports_fit_the_RP_bram_but_NOT_the_host_readout():
    """The headline engineering conclusion, pinned as a test.

    BRAM is not the binding constraint for NIC-400-scale tracing; the readout
    wire is. If either of these flips, the platform advice changes.
    """
    rows = gb.signal_rows("axi4", 64, 32, 4)
    b = gb.budget(rows, 1024)
    ten_ports_bits = b["total_bits"] * 10
    assert ten_ports_bits / (36 * 1024) < 123.5          # fits the free BRAM
    assert ten_ports_bits / 340.0 > 3600                 # > 1 h on host XVC
