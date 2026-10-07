"""tests/realphy_timing/realphy_budget.py

The RMII I/O timing budget of the SHELL_REALPHY variant, re-done in Python from
the numbers the XDC actually declares.

WHY A SEPARATE MODULE
---------------------
The arithmetic and the tests that exercise it are split so the checker can be
fed HAND-BUILT parameter dictionaries. A checker that can only ever read the
committed file can only ever pass -- there is no way to see it fail, so there is
no evidence it checks anything. `evaluate()` takes a plain dict; the tests feed
it both the committed values and deliberately broken ones.

WHAT IS BEING CHECKED, AND WHY IT IS NOT OBVIOUS
-----------------------------------------------
Clocking Option A has the FPGA SOURCE the 50 MHz RMII reference and drive it out
to a LAN8720 strapped REF_CLK-in. That makes the RX path a ROUND TRIP: the
clock the PHY launches its RX data against is the one this FPGA sent, so the
board flight time is paid TWICE -- once on the way out with the clock, once on
the way back with the data -- plus the PHY's own clock-to-out, all inside ONE
20 ns REF_CLK period.

The first-pass constraint file charged that flight time ONCE (a single
`brd_trace_max`). That is the defect this module exists to keep fixed: with the
declared numbers the one-trace model claims 3.5 ns of budget and the round-trip
model claims 1.5 ns, and 2 ns of imaginary margin on a path with 1.5 ns of real
margin is exactly the kind of error that gets found on a board instead of at a
desk.

THE RATE IS NOT IN THIS ARITHMETIC, ON PURPOSE
----------------------------------------------
RMII clocks REF_CLK at 50 MHz at BOTH 10 and 100 Mb/s -- the line rate is
carried by di-bit repetition, not by clock frequency. So none of these numbers
change with REALPHY_RATE, and `evaluate()` takes no rate argument. Dropping the
link to 10 Mb/s buys inbound ANALOG margin (25 MHz -> 2.5 MHz fundamental on
RXD/CRS_DV) and exactly zero setup margin. See
docs/planning/REALPHY_RATE_DECISION.md.
"""
from __future__ import annotations

import pathlib
import re
from typing import Dict, List

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TIMING_XDC = REPO_ROOT / "fpga" / "shell" / "constraints_realphy" / "mps3_realphy_timing.xdc"
PINS_XDC = REPO_ROOT / "fpga" / "shell" / "constraints_realphy" / "mps3_realphy_pins.xdc"

# Every parameter the budget needs. Missing one is an error, not a default --
# a silently-defaulted timing term is how a model stops describing the board.
REQUIRED_PARAMS = (
    "lan_tco_max",
    "lan_tco_min",
    "lan_tsu",
    "lan_th",
    "brd_clk_out_max",
    "brd_clk_out_min",
    "brd_data_in_max",
    "brd_data_in_min",
    "brd_data_out_max",
    "brd_data_out_min",
    "brd_skew",
    "fpga_capture_reserve",
    "rmii_period",
)

_SET_NUM = re.compile(r"^\s*set\s+([a-z_][a-z0-9_]*)\s+(-?\d+(?:\.\d*)?)\s*(?:;.*)?$", re.M | re.I)


def parse_params(text: str) -> Dict[str, float]:
    """Numeric `set <name> <value>` bindings from an XDC, as a dict.

    Deliberately literal: it reads the same lines a human edits. Anything
    computed with `expr` is NOT picked up here, which is why the budget below
    recomputes the sums rather than trusting a derived value in the file.
    """
    return {m.group(1): float(m.group(2)) for m in _SET_NUM.finditer(text)}


def parse_committed_params() -> Dict[str, float]:
    return parse_params(TIMING_XDC.read_text())


def evaluate(p: Dict[str, float]) -> Dict[str, object]:
    """The RMII I/O budget. Returns every intermediate, plus `failures`.

    `failures` is empty iff the declared model is internally consistent and
    leaves the FPGA side at least its declared capture reserve.
    """
    missing = [k for k in REQUIRED_PARAMS if k not in p]
    if missing:
        return {"failures": [f"missing timing parameter(s): {', '.join(missing)}"]}

    period = p["rmii_period"]

    # RX, the round trip. Note brd_clk_out AND brd_data_in -- see the docstring.
    rx_in_max = p["brd_clk_out_max"] + p["lan_tco_max"] + p["brd_data_in_max"] + p["brd_skew"]
    rx_in_min = p["brd_clk_out_min"] + p["lan_tco_min"] + p["brd_data_in_min"] - p["brd_skew"]
    rx_capture_budget = period - rx_in_max

    # TX, deliberately conservative: the relief from the forwarded clock
    # reaching the PHY late is not claimed.
    tx_out_max = p["lan_tsu"] + p["brd_data_out_max"] + p["brd_skew"]
    tx_out_min = -p["lan_th"] - p["brd_data_out_min"] - p["brd_skew"]

    failures: List[str] = []

    if period <= 0:
        failures.append(f"rmii_period must be positive, got {period}")
    if rx_capture_budget < p["fpga_capture_reserve"]:
        failures.append(
            f"RX setup: period {period} ns - input_delay_max {rx_in_max} ns "
            f"= {rx_capture_budget:.3f} ns left for FPGA capture, below the declared "
            f"fpga_capture_reserve of {p['fpga_capture_reserve']} ns. The RMII RX pads "
            f"cannot be signed off at 50 MHz with these numbers. Dropping the LINK RATE "
            f"does NOT help (REF_CLK is 50 MHz at both rates); the levers are a real "
            f"lan_tco_max from the datasheet, shorter board flight, or clocking Option B "
            f"(PHY sources REF_CLK, flight times cancel)."
        )
    if rx_in_min <= 0:
        failures.append(
            f"RX hold: input_delay_min {rx_in_min:.3f} ns is not positive -- the model says "
            f"PHY data can arrive at or before its own launch edge, which is not a board, "
            f"it is a sign error."
        )
    if rx_in_min >= rx_in_max:
        failures.append(f"RX: input_delay_min {rx_in_min} >= input_delay_max {rx_in_max}")
    if tx_out_max >= period:
        failures.append(
            f"TX setup: output_delay_max {tx_out_max} ns >= period {period} ns -- "
            f"no launch-to-capture window left for the FPGA at all."
        )
    if tx_out_min >= 0:
        failures.append(
            f"TX hold: output_delay_min {tx_out_min} ns should be negative "
            f"(it encodes the PHY's hold requirement)."
        )

    return {
        "period": period,
        "rx_in_max": rx_in_max,
        "rx_in_min": rx_in_min,
        "rx_capture_budget": rx_capture_budget,
        "rx_budget_fraction_used": rx_in_max / period if period else float("inf"),
        "tx_out_max": tx_out_max,
        "tx_out_min": tx_out_min,
        "failures": failures,
    }
