"""tests/realphy_timing/test_realphy_timing_budget.py

Board-free gates on the SHELL_REALPHY variant's RMII I/O timing model.

Nothing here runs Vivado. What it gates is the thing Vivado cannot tell you:
whether the NUMBERS FED TO Vivado describe the board. A `set_input_delay` is
believed by the tool unconditionally -- an under-counted board flight time
produces a clean "all constraints met" report for a design that will not work,
which is the most expensive kind of green.

Four gates, each with a control that is seen to fail:

1. THE ROUND TRIP IS CHARGED TWICE. Option A (FPGA sources REF_CLK) sends the
   clock out and gets the data back, so the board flight time is paid on BOTH
   legs. The first-pass file charged it once. `evaluate()` fails if the file
   ever loses a leg.

2. THE BUDGET STILL CLOSES. period - input_delay_max must leave at least the
   declared `fpga_capture_reserve` for the ILOGIC setup and clock uncertainty.
   With today's numbers that is 1.5 ns out of 20 ns -- 92.5% of the period is
   already spent -- so this gate is live, not decorative: raising any estimate
   by 0.5 ns turns it red.

3. THE RATE IS NOT A TIMING LEVER. RMII clocks REF_CLK at 50 MHz at both 10 and
   100 Mb/s. The constraint file must not branch its I/O delays on
   REALPHY_RATE, and must say why. A file that relaxed its numbers at 10 Mb/s
   would be claiming margin that physics does not supply.

4. THE RX PADS ARE IOB-PACKED. The 1.5 ns in gate 2 covers an ILOGIC setup and
   nothing else. If the RX pads fed the reconfigurable partition directly, the
   pad-to-pblock route would have to fit in that 1.5 ns too. Both halves of the
   fix -- the `IOB = "TRUE"` capture flops in shell_top.sv and the `IOB TRUE`
   property on the RX ports -- are gated here, because either one alone silently
   does not pack.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import realphy_budget as rb  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SHELL_TOP = REPO_ROOT / "fpga" / "shell" / "shell_top.sv"


@pytest.fixture(scope="module")
def params():
    return rb.parse_committed_params()


# ---------------------------------------------------------------------------
# 1. the round trip
# ---------------------------------------------------------------------------

def test_every_timing_parameter_is_declared(params):
    missing = [k for k in rb.REQUIRED_PARAMS if k not in params]
    assert not missing, (
        f"mps3_realphy_timing.xdc no longer declares {missing}. A timing term that "
        f"vanishes does not become zero, it becomes un-modelled."
    )


def test_rx_input_delay_charges_both_legs_of_the_round_trip(params):
    """The clock's outbound flight AND the data's inbound flight, not one trace."""
    ev = rb.evaluate(params)
    expected = (
        params["brd_clk_out_max"]
        + params["lan_tco_max"]
        + params["brd_data_in_max"]
        + params["brd_skew"]
    )
    assert ev["rx_in_max"] == pytest.approx(expected)
    # The defect this replaced: a single-trace model. It must be strictly more
    # optimistic than the round trip, i.e. the two are genuinely different.
    one_trace = params["lan_tco_max"] + params["brd_data_in_max"] + params["brd_skew"]
    assert ev["rx_in_max"] > one_trace, (
        "the round-trip model must be MORE pessimistic than the one-trace model it "
        "replaced; if these are equal the outbound clock flight has gone missing again"
    )


def test_control_dropping_the_clock_flight_leg_is_visible():
    """CONTROL: a model that forgets the outbound clock leg over-claims margin."""
    p = rb.parse_committed_params()
    honest = rb.evaluate(p)["rx_capture_budget"]
    p_broken = dict(p, brd_clk_out_max=0.0)
    optimistic = rb.evaluate(p_broken)["rx_capture_budget"]
    assert optimistic > honest, "the control did not actually change the budget"
    assert optimistic - honest == pytest.approx(p["brd_clk_out_max"])


# ---------------------------------------------------------------------------
# 2. the budget closes -- and the control that shows the gate can fail
# ---------------------------------------------------------------------------

def test_committed_timing_model_is_self_consistent(params):
    ev = rb.evaluate(params)
    assert ev["failures"] == [], "\n".join(str(f) for f in ev["failures"])


def test_budget_is_reported_as_tight(params):
    """Not a pass/fail of the design -- a guard on the story told about it.

    docs/planning/REALPHY_RATE_DECISION.md and the XDC header both state that
    the model spends ~92% of the period before the FPGA sees the data. If an
    edit ever makes that comfortably untrue, those words are stale and should be
    rewritten rather than left to rot.
    """
    ev = rb.evaluate(params)
    assert ev["rx_budget_fraction_used"] > 0.75, (
        f"the RX budget now uses only {ev['rx_budget_fraction_used']:.1%} of the period "
        f"({ev['rx_capture_budget']:.2f} ns spare). Good news -- but the 'this is tight' "
        f"narrative in mps3_realphy_timing.xdc and docs/planning/REALPHY_RATE_DECISION.md "
        f"is now wrong and must be updated."
    )


@pytest.mark.parametrize(
    "mutation,why",
    [
        ({"lan_tco_max": 17.0}, "a real datasheet tco that is 3 ns worse"),
        ({"brd_data_in_max": 4.0}, "a longer inbound shield trace"),
        ({"brd_clk_out_max": 4.0}, "a longer outbound clock trace"),
        ({"brd_skew": 2.5}, "more skew across the shield bank"),
        ({"fpga_capture_reserve": 3.0}, "a stricter capture reserve"),
    ],
)
def test_control_plausible_worsenings_turn_the_gate_red(mutation, why):
    """CONTROL: each of these is a number someone might legitimately measure.

    Every one of them must fail the gate rather than pass quietly -- that is
    what makes gate 2 an instrument instead of an ornament.
    """
    p = dict(rb.parse_committed_params(), **mutation)
    ev = rb.evaluate(p)
    assert ev["failures"], f"{why}: {mutation} should have failed the budget and did not"


def test_control_sign_error_on_tx_hold_is_caught():
    p = dict(rb.parse_committed_params(), lan_th=-10.0, brd_data_out_min=0.0, brd_skew=0.0)
    ev = rb.evaluate(p)
    assert any("TX hold" in f for f in ev["failures"])


# ---------------------------------------------------------------------------
# 3. the rate is not a timing lever
# ---------------------------------------------------------------------------

def _rate_branch_lines(text: str):
    """Lines whose `if`/`elseif` CONDITION tests the rate value itself.

    Reading REALPHY_RATE out of the environment (`$::env(REALPHY_RATE)`) is not
    a branch on the rate -- that is how the value arrives. Testing
    `$realphy_rate` is.
    """
    out = []
    for m in re.finditer(r"^\s*(?:\}\s*)?(?:if|elseif)\b[^\n]*$", text, re.M):
        if re.search(r"\$realphy_rate\b", m.group(0)):
            out.append((text[: m.start()].count("\n") + 1, m.group(0).strip()))
    return out


def test_io_delays_do_not_branch_on_the_link_rate():
    """No set_input_delay / set_output_delay may sit under a rate conditional."""
    text = rb.TIMING_XDC.read_text()
    branches = _rate_branch_lines(text)
    assert not branches, (
        "mps3_realphy_timing.xdc branches on the link rate at "
        + ", ".join(f"line {n}: {s}" for n, s in branches)
        + ". RMII REF_CLK is 50 MHz at BOTH 10 and 100 Mb/s, so an I/O delay that "
        "changes with the rate is claiming margin the interface does not provide."
    )
    # ...and no delay VALUE may mention the rate either, branch or no branch.
    for ln_no, ln in enumerate(text.splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        if "set_input_delay" in ln or "set_output_delay" in ln:
            assert "realphy_rate" not in ln, (
                f"mps3_realphy_timing.xdc:{ln_no} makes an I/O delay depend on the "
                f"link rate:\n  {ln.strip()}"
            )


def test_control_a_rate_conditional_would_be_caught():
    """CONTROL: the detector fires on a rate branch and not on the env read."""
    env_read = 'if { [info exists ::env(REALPHY_RATE)] } {\n    set realphy_rate 10\n}\n'
    assert _rate_branch_lines(env_read) == []
    rate_branch = 'if { $realphy_rate == 10 } {\n    set lan_tco_max 30.0\n}\n'
    assert _rate_branch_lines(rate_branch), "the rate-branch detector never fires"


def test_xdc_states_why_the_rate_does_not_change_the_numbers():
    text = rb.TIMING_XDC.read_text()
    assert "50 MHz at BOTH" in text or "50 MHz at both" in text, (
        "the rate block must say out loud that REF_CLK is 50 MHz at both link rates -- "
        "otherwise the next reader reaches for 10 Mb/s as a timing fix, which it is not"
    )


def test_no_ungrounded_multicycle_waiver_on_the_rx_capture():
    """A 10 Mb/s MCP is arithmetically available but needs an argument first."""
    text = rb.TIMING_XDC.read_text()
    live = [
        ln for ln in text.splitlines()
        if "set_multicycle_path" in ln and not ln.lstrip().startswith("#")
    ]
    assert not live, (
        "a live set_multicycle_path appeared in the RMII constraints. At 10 Mb/s the "
        "di-bit is stable for 10 cycles so the waiver is arithmetically available, but "
        "the shell-side capture flop samples EVERY cycle -- taking it needs a written "
        "tick-alignment argument. Write the argument, then take it:\n  " + "\n  ".join(live)
    )


# ---------------------------------------------------------------------------
# 4. the RX pads are IOB-packed (both halves)
# ---------------------------------------------------------------------------

def _realphy_block(text: str) -> str:
    """The `ifdef MPS3_SHELL_REALPHY body that holds the pad glue in shell_top."""
    blocks = re.findall(
        r"^`ifdef\s+MPS3_SHELL_REALPHY\b(.*?)^`endif",
        text,
        re.S | re.M,
    )
    assert blocks, "shell_top.sv has no `ifdef MPS3_SHELL_REALPHY block at all"
    return "\n".join(blocks)


def test_shell_top_registers_the_rx_pads_with_iob_true():
    body = _realphy_block(SHELL_TOP.read_text())
    assert re.search(r'\(\*\s*IOB\s*=\s*"TRUE"\s*\*\)\s*reg\s+r_phy_crs_dv', body), (
        "CRS_DV has no IOB=TRUE capture flop in shell_top.sv"
    )
    assert re.search(r'\(\*\s*IOB\s*=\s*"TRUE"\s*\*\)\s*reg\s*\[1:0\]\s*r_phy_rxd', body), (
        "RXD has no IOB=TRUE capture flop in shell_top.sv"
    )
    assert "r_phy_crs_dv <=" in body and "r_phy_rxd    <=" in body.replace("  ", "  "), (
        "the RX capture flops are declared but never clocked"
    )


def test_rx_capture_takes_one_cycle_on_all_three_signals_together():
    """Alignment, not latency, is what rmii_to_mii's SFD nibble-align depends on.

    CRS_DV and both RXD lanes must be registered in the SAME always block on the
    SAME clock. Splitting them (or registering two of the three) skews the
    di-bit stream against its own valid flag.
    """
    body = _realphy_block(SHELL_TOP.read_text())
    blocks = re.findall(
        r"always\s*@\(posedge\s+w_phy_pad_ref_clk\)\s*begin(.*?)\n\s*end",
        body,
        re.S,
    )
    rx_blocks = [b for b in blocks if "r_phy_rxd" in b or "r_phy_crs_dv" in b]
    assert len(rx_blocks) == 1, (
        f"expected exactly ONE always block to capture the RX group, found {len(rx_blocks)}"
    )
    assert "r_phy_crs_dv" in rx_blocks[0] and "r_phy_rxd" in rx_blocks[0], (
        "CRS_DV and RXD are not captured together"
    )


def test_rx_lane_order_is_not_transposed_a_second_time():
    """The rxd0<->rxd1 board swap lives in the XDC pin map and NOWHERE else.

    A second swap here would re-create the 0xA-preamble / no-SFD-lock failure
    that the pins file's header warns about at length.
    """
    body = _realphy_block(SHELL_TOP.read_text())
    assert "{w_pad_rxd1_i, w_pad_rxd0_i}" in body, (
        "the RX capture must concatenate the lanes in natural {rxd1, rxd0} order; the "
        "board's lane transpose belongs to mps3_realphy_pins.xdc alone"
    )


def test_rx_ports_carry_iob_true_in_the_pin_constraints():
    """The RTL attribute alone does not pack -- the port property is the other half."""
    text = rb.PINS_XDC.read_text()
    for port in ("ETH_RMII_RXD[0]", "ETH_RMII_RXD[1]", "ETH_RMII_CRS_DV"):
        line = next(
            (ln for ln in text.splitlines()
             if not ln.lstrip().startswith("#") and port in ln and "PACKAGE_PIN" in ln),
            None,
        )
        assert line is not None, f"no PACKAGE_PIN constraint for {port}"
        assert "IOB TRUE" in line, (
            f"{port} is not IOB TRUE:\n  {line.strip()}\n"
            f"Without it the capture flop can be placed in fabric and the "
            f"{rb.evaluate(rb.parse_committed_params())['rx_capture_budget']:.1f} ns "
            f"external window has to absorb the pad-to-flop route as well."
        )
