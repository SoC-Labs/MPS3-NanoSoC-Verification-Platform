#!/usr/bin/env python3
"""Gate: the shell console's FPGA UART lane, the MCC's UART mux, and the doc agree.

THE FAILURE THIS EXISTS FOR. The shell's `xil_printf` console was pinned to FPGA
UART lane 1 (`AE30`/`AE31`) on the reasoning that lane 0 was the MCC's and lane 2
was historically the DUT's -- "that leaves channel [1]"
(fpga/shell/constraints/mps3_harness.xdc, the MB_UART comment block). Nobody had
read the board doc. On the MPS3, lanes 0 and 1 reach the host ONLY through a
source multiplexer the MCC drives from `UARTMODE:` in the SD card's config.txt;
lanes 2 and 3 are hard-wired (Arm MPS3 TRM 100765_0000_04_en s2.18 Figure 2-25
p2-51; Arm V2M-MPS3 Schematics EOI-0309 rev C sheet 9 "USB UARTS"). This platform
ships `UARTMODE: 0` = MCC:FPGA0, which does not select lane 1. The console has
been wired to a switched-off mux input since the shell was first pinned, and the
symptom -- a silent serial node -- is indistinguishable from a dead processor. It
cost a year of firmware diagnosis over JTAG telemetry instead. See
docs/planning/CONSOLE_AUDIT.md.

Three files have to agree and none of them referenced the others:

  1. fpga/shell/constraints/mps3_harness.xdc  -- which package pins the console is on
  2. fpga/mps3_sd/templates/config.txt        -- which lanes the MCC muxes to the host
  3. docs/BOARD_BRINGUP.md                    -- what an operator is told to open

WHAT THIS GATE DOES NOT DO. It does not demand the console be reachable. A
platform is allowed to ship with a known-unreachable console -- that is today's
state, and hiding it behind a red gate that everyone waives would be worse than
the silence it replaced. What it demands is that the state be WRITTEN DOWN and
stay true: BOARD_BRINGUP.md carries a `CONSOLE_CHANNEL` marker whose
`reachable=` verdict must equal the one derived from the XDC and the MCC config.
Re-pin the console, or change UARTMODE, and this goes red until the operator
doc is corrected. That is the regression it exists to catch: the fix landing
without the doc, or the doc claiming a fix that never landed.

Board-free. Reads only tracked text files. Exit 0 on agreement, 1 otherwise.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

XDC_SHELL = "fpga/shell/constraints/mps3_harness.xdc"
XDC_PINMAP = "fpga/monolithic/nanosoc_mps3.xdc"
BD = "fpga/shell/bd/shell_bd.tcl"
MCC_CONFIG = "fpga/mps3_sd/templates/config.txt"
DOC = "docs/BOARD_BRINGUP.md"

# UARTMODE -> the FPGA UART lanes the MCC's mux puts on a host USB-serial port.
# Legend, identical in TRM s3.5.2 p3-64 and in our config.txt:
#     0-MCC:FPGA0, 1-MCC:FPGA1, 2-MCC/FPGA0:FPGA1
# Lanes 2 and 3 are not in the mux at all -- they are always reachable.
HARDWIRED_LANES = frozenset({2, 3})
MUXED_BY_MODE = {0: frozenset({0}), 1: frozenset({1}), 2: frozenset({0, 1})}

MARKER_RE = re.compile(
    r"<!--\s*CONSOLE_CHANNEL\s+lane=(?P<lane>\d+)\s+"
    r"uartmode=(?P<mode>\d+)\s+reachable=(?P<reach>yes|no)\s*-->"
)


def _fail(msg: str) -> int:
    sys.stderr.write("check_console_channel: FAIL: %s\n" % msg)
    return 1


def _pin_of(text: str, port: str) -> str | None:
    m = re.search(
        r"^\s*set_property\s+PACKAGE_PIN\s+(\S+)\s+\[get_ports\s+\{?%s\}?\s*\]"
        % re.escape(port),
        text,
        re.M,
    )
    return m.group(1) if m else None


def _lane_pins(text: str, bus: str) -> dict[str, int]:
    """{package pin -> lane index} for every UART_{TX,RX}_F[n] in the pinmap."""
    out: dict[str, int] = {}
    for m in re.finditer(
        r"^\s*set_property\s+PACKAGE_PIN\s+(\S+)\s+\[get_ports\s+\{%s\[(\d+)\]\}\s*\]"
        % re.escape(bus),
        text,
        re.M,
    ):
        out[m.group(1)] = int(m.group(2))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", default=".", help="repo root (default: .)")
    args = ap.parse_args(argv)
    root = Path(args.repo)

    try:
        shell_xdc = (root / XDC_SHELL).read_text()
        pinmap = (root / XDC_PINMAP).read_text()
        bd = (root / BD).read_text()
        mcc = (root / MCC_CONFIG).read_text()
        doc = (root / DOC).read_text()
    except OSError as exc:
        return _fail("cannot read a required file: %s" % exc)

    # --- 1. which lane is the console pinned to? -----------------------------
    tx_pin = _pin_of(shell_xdc, "MB_UART_TXD")
    rx_pin = _pin_of(shell_xdc, "MB_UART_RXD")
    if tx_pin is None or rx_pin is None:
        return _fail("%s does not pin MB_UART_TXD/MB_UART_RXD" % XDC_SHELL)

    tx_lanes = _lane_pins(pinmap, "UART_TX_F")
    rx_lanes = _lane_pins(pinmap, "UART_RX_F")
    if tx_pin not in tx_lanes:
        return _fail(
            "MB_UART_TXD is on %s, which %s does not list as a UART_TX_F lane"
            % (tx_pin, XDC_PINMAP)
        )
    if rx_pin not in rx_lanes:
        return _fail(
            "MB_UART_RXD is on %s, which %s does not list as a UART_RX_F lane"
            % (rx_pin, XDC_PINMAP)
        )
    lane, rx_lane = tx_lanes[tx_pin], rx_lanes[rx_pin]
    if lane != rx_lane:
        return _fail(
            "console TX is on lane %d (%s) but RX is on lane %d (%s) -- a half "
            "re-pin. Both must move together." % (lane, tx_pin, rx_lane, rx_pin)
        )

    # --- 2. what does the MCC mux select? ------------------------------------
    m = re.search(r"^\s*UARTMODE:\s*(\d+)", mcc, re.M)
    if m is None:
        return _fail("%s has no UARTMODE: key" % MCC_CONFIG)
    mode = int(m.group(1))
    if mode not in MUXED_BY_MODE:
        return _fail(
            "%s sets UARTMODE: %d, which is not one of the documented modes %s "
            "(TRM 100765 s3.5.2 p3-64)"
            % (MCC_CONFIG, mode, sorted(MUXED_BY_MODE))
        )
    reachable = lane in (HARDWIRED_LANES | MUXED_BY_MODE[mode])

    # --- 3. what is the console's baud? --------------------------------------
    m = re.search(r"CONFIG\.C_BAUDRATE\s*\{(\d+)\}", bd)
    if m is None:
        return _fail("%s does not set CONFIG.C_BAUDRATE on the console uartlite" % BD)
    baud = int(m.group(1))

    # --- 4. does the operator doc say all of that? ---------------------------
    mk = MARKER_RE.search(doc)
    if mk is None:
        return _fail(
            "%s carries no CONSOLE_CHANNEL marker. Add one to the shell-console "
            "paragraph, e.g.\n    <!-- CONSOLE_CHANNEL lane=%d uartmode=%d "
            "reachable=%s -->" % (DOC, lane, mode, "yes" if reachable else "no")
        )
    doc_lane, doc_mode = int(mk.group("lane")), int(mk.group("mode"))
    doc_reach = mk.group("reach") == "yes"

    if doc_lane != lane:
        return _fail(
            "%s's marker says the console is on FPGA UART lane %d, but %s pins it "
            "to %s = lane %d (%s). Update the doc with the pin."
            % (DOC, doc_lane, XDC_SHELL, tx_pin, lane, XDC_PINMAP)
        )
    if doc_mode != mode:
        return _fail(
            "%s's marker says uartmode=%d, but %s ships UARTMODE: %d."
            % (DOC, doc_mode, MCC_CONFIG, mode)
        )
    if doc_reach != reachable:
        why = (
            "lane %d is hard-wired to a host USB-serial port" % lane
            if lane in HARDWIRED_LANES
            else "UARTMODE: %d selects FPGA lane(s) %s onto the muxed host ports"
            % (mode, sorted(MUXED_BY_MODE[mode]))
        )
        return _fail(
            "%s's marker claims reachable=%s, but the console lane %d is actually "
            "%s: %s.\nEither the fix landed and the doc was not updated, or the "
            "doc claims a fix that did not land. See docs/planning/CONSOLE_AUDIT.md."
            % (
                DOC,
                "yes" if doc_reach else "no",
                lane,
                "REACHABLE" if reachable else "UNREACHABLE",
                why,
            )
        )
    if not re.search(r"FPGA UART lane %d\b" % lane, doc):
        return _fail(
            "%s's marker and its prose disagree: no 'FPGA UART lane %d' in the "
            "text. The marker must not be the only place the lane is stated."
            % (DOC, lane)
        )
    if not re.search(r"\b%d\b" % baud, doc):
        return _fail(
            "%s never states the console baud (%d, from %s CONFIG.C_BAUDRATE). "
            "An operator cannot open a port without it." % (DOC, baud, BD)
        )

    print(
        "OK: shell console on FPGA UART lane %d (%s/%s) @ %d baud; "
        "UARTMODE: %d -> %s; %s agrees"
        % (
            lane,
            tx_pin,
            rx_pin,
            baud,
            mode,
            "reachable" if reachable else "NOT reachable (known, documented)",
            DOC,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
