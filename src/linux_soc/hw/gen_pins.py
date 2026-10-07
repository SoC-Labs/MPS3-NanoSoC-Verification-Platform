#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Derive the MPS3 (xcku115) DDR4 pinout from the board pinmap — never transcribe it.

A single transposed DQ index would synthesise clean, simulate clean, and fail on
the bench.  So the pinout here is *parsed* from the one authoritative source and
re-emitted, with every assertion the source is expected to satisfy checked
loudly.  Re-run it and diff; do not hand-edit the artefacts.

Source of truth (read-only):
    <nanosoc_tech>/fpga/targets/arm_mps3/fpga_pinmap.xdc

In that file the DDR4 pins live as a commented-out block (~lines 786-980):

  * ACTIVE lines have exactly one '#' then a space:
        # set_property PACKAGE_PIN <PIN> [get_ports {c0_ddr4_<sig>[<i>]}]
        # set_property PACKAGE_PIN <PIN> [get_ports {c0_ddr4_<sig>}]        (scalar, braces)
        # set_property PACKAGE_PIN <PIN> [get_ports c0_ddr4_<sig>]          (scalar, bare)
    plus the system clock c0_sys_clk_p (H19) / c0_sys_clk_n (H18).

  * DISABLED lines have two '#':  "# #set_property ...".  These are the DIMM
    socket's unused 2-rank / x8 option (bg[1]=F18, cs_n[1]=F20, odt[1]=D20,
    cke[1]=E17, ck_t[1]=P18, ck_c[1]=N18) plus indexed duplicates of the
    scalar signals (cke[0]=E20, ck_t[0]=P16, ck_c[0]=N16, odt[0]=E16,
    cs_n[0]=F17).  The parser MUST reject every one of them: the first
    non-space character after the leading "# " being another '#' is the
    disable marker.

The one repair (see REPAIRS): c0_ddr4_dqs_c[7] is simply absent from the source.
It is added here explicitly, marked provenance 'inferred', never merged silently.

Besides the DDR4 SODIMM, four NON-DDR board pins are derived here too --
uart_txd, uart_rxd, sys_rst_n and calib_complete_led_n. They had no PACKAGE_PIN
and no IOSTANDARD, which is what made write_bitstream fail DRC NSTD-1 + UCIO-1.
They come from the SAME source pinmap (see BOARD_PINS), but from its LIVE
(uncommented) set_property lines rather than the commented-out DDR4 block.
Two of them cross an ACTIVE-LOW pad -- see the POLARITY CONTRACT in the emitted
board_pins.xdc, and the matching inverters in mbv_soc.tcl SECTION 5.

Outputs (written next to this script):
    pins.csv        port,signal,index,package_pin,iostandard,provenance
                    ('port' is the exact top-level port name; 'iostandard' is
                     empty for DDR4 pins, which get theirs from mig.xdc)
    ddr4_pins.xdc   DDR4 PACKAGE_PIN constraints, grouped, provenance header
    board_pins.xdc  the four board pins: PACKAGE_PIN + IOSTANDARD + polarity

Usage:
    python3 gen_pins.py            # (re)generate pins.csv + ddr4_pins.xdc
    python3 gen_pins.py --check    # verify the artefacts still match the source
    python3 gen_pins.py --source <path>   # override the source pinmap

Exit code is non-zero on any assertion failure or, under --check, any drift.
"""

import argparse
import csv
import hashlib
import io
import os
import re
import sys

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))

# The nanosoc_tech checkout comes from $SOCLABS_NANOSOC_TECH_DIR (tools.env key of
# the same name); no site default. --source overrides it outright.
DEFAULT_SOURCE = os.path.join(
    os.environ.get("SOCLABS_NANOSOC_TECH_DIR", "<SOCLABS_NANOSOC_TECH_DIR unset>"),
    "fpga/targets/arm_mps3/fpga_pinmap.xdc",
)

CSV_PATH = os.path.join(HERE, "pins.csv")
XDC_PATH = os.path.join(HERE, "ddr4_pins.xdc")

# Only ports beginning with one of these prefixes are ours.
PORT_PREFIXES = ("c0_ddr4_", "c0_sys_clk_")

# Number of ACTIVE PACKAGE_PIN assignments expected straight from the source,
# before the repair is applied.
EXPECTED_SOURCE_PIN_COUNT = 116

# Number of DISABLED (double-#) DDR lines expected — the socket's unused
# 2-rank / x8 option.  A cheap check that the reject logic actually fired.
EXPECTED_DISABLED_COUNT = 11

# Per-signal widths expected FROM THE SOURCE (dqs_c is 7 here: [7] is missing).
# Keyed by the short signal name used in pins.csv.
EXPECTED_SOURCE_WIDTHS = {
    "dq": 64,
    "dqs_t": 8,
    "dqs_c": 7,          # <-- source is short one bit; repaired below to 8
    "dm_dbi_n": 8,
    "adr": 17,
    "ba": 2,
    "bg": 1,             # bg[1]=F18 is a disabled x8 option
    "ck_t": 1,
    "ck_c": 1,
    "cke": 1,
    "cs_n": 1,
    "odt": 1,
    "act_n": 1,
    "reset_n": 1,
    "c0_sys_clk_p": 1,
    "c0_sys_clk_n": 1,
}

# Signals carrying a bus index (as opposed to true scalars).  Used to assert
# that indices are contiguous 0..width-1 with no holes after the repair.
INDEXED_SIGNALS = ("dq", "dqs_t", "dqs_c", "dm_dbi_n", "adr", "ba", "bg")

# The explicit, documented repair.  Keyed by (port_base, index).
# NOTHING here is parsed from the source — it is derived and marked 'inferred'.
REPAIRS = {
    ("c0_ddr4_dqs_c", 7): {
        "pin": "J20",
        "reason": (
            "c0_ddr4_dqs_c[7] is absent from fpga_pinmap.xdc (its true "
            "complement dqs_t[7] is present at K21). J20 is pad "
            "IO_L10N_T1U_N7_QBC_AD4N_51, the DIFF_PAIR complement (DIFF_PAIR_PIN) "
            "of K21 (IO_L10P_T1U_N6_QBC_AD4P_51) on xcku115-flvb1760-1-c, "
            "and is otherwise unassigned. Derived from the device pad table, "
            "not hand-copied. Corroborated THREE independent ways: (1) Vivado "
            "2024.1 DIFF_PAIR_PIN on the real part; (2) PIN_FUNC query; (3) BSDL "
            "boundary-register pad adjacency in xcku115_flvb1760.bsd, where "
            "K21=PAD864 and J20=PAD865 -- a rule validated against all 9 known "
            "diff pairs in this pinout. Note the seductive wrong answer: K22=PAD871 "
            "is the complement of L22=PAD870 (=dm_dbi_n[7]), NOT of K21. "
            "THIS IS THE ONLY INFERRED PIN IN THE FILE; it is the one thing a board "
            "schematic could still overturn."
        ),
    },
}

# Preferred grouping order for the emitted XDC (command/address, then data,
# then the differential clocks).  Anything unlisted falls to the end,
# alphabetically, so the output stays deterministic even if a signal appears.
SIGNAL_ORDER = [
    "reset_n", "act_n", "cs_n", "odt", "cke",
    "ck_t", "ck_c", "bg", "ba", "adr",
    "dq", "dm_dbi_n", "dqs_t", "dqs_c",
    "c0_sys_clk_p", "c0_sys_clk_n",
]

# Which non-PACKAGE_PIN properties we lift verbatim from the source (marked).
# The full IOSTANDARD/DCI set is expected from the DDR4 IP's mig.xdc; the
# source happens to also carry these two for bg[0], so we surface them.
EXTRA_PROPS = ("IOSTANDARD", "OUTPUT_IMPEDANCE")

# --------------------------------------------------------------------------- #
# Board (non-DDR4) pins                                                        #
#                                                                              #
# The four ports that made write_bitstream fail DRC NSTD-1 + UCIO-1: they had  #
# neither PACKAGE_PIN nor IOSTANDARD. They are derived from the SAME source     #
# pinmap as the DDR4 pins -- but note these board signals are LIVE (uncommented)#
# set_property lines there, unlike the DDR4 block which is commented out. Hence #
# a second parser pass (parse_board_source) over the non-comment lines.        #
# --------------------------------------------------------------------------- #

# WHICH OF THE FOUR FPGA UART LANES CARRIES THE CONSOLE.  *** NOT LANE 0. ***
#
# The board routes four FPGA UART lanes (UART_{TX,RX}_F[3:0]) and the pinmap pins
# all four, but only ONE is known to reach the host's FT4232 USB serial port, and
# that is lane 2:
#
#   * nanosoc_design_wrapper.v -- the legacy, BOARD-PROVEN MPS3 design sitting in
#     the same directory as this pinmap -- wires its console to
#     UART_RX_F[2] / UART_TX_F[2] and to no other lane.
#   * mps3-nanosoc-platform/fpga/monolithic/nanosoc_mps3.xdc states in its header
#     that, of its 547 constraints, the design "only drives/reads
#     UART_TX_F[2]/UART_RX_F[2], OSCCLK[1], and CB_nRST/CS_nSRST".
#   * That monolithic build is recorded HW-PROVEN in the platform's status doc --
#     a "hello" banner actually observed on the host, over the FT4232.
#
# There is NO comparable evidence for lanes 0/1/3, and the FT4232 has only four
# channels (at least one being the MCC's own console) -- so "the pinmap pins it"
# does not imply "a host tty sees it". Choosing lane 0 risks a console that is
# silent on the bench, which no simulation can catch and which looks identical to
# a dead SoC. Lane 2 is the lane with on-bench proof.
#
# If a board schematic or a bench session ever proves otherwise: flip this one
# constant and re-run. Nothing else needs to change.
CONSOLE_UART_LANE = 2

# BOARD-SIGNAL POLARITY.  Every 'n'-prefixed signal in the source pinmap is
# active-LOW (CB_nRST, CB_nPOR, CS_nSRST, CS_nTRST, ETH_nCS, ETH_nOE, SMBF_nOE,
# SMBF_nWE, SMBF_nRST, USB_nCS, USER_nPB, USER_nLED ...), and the platform's own
# PLATFORM_MAPPING.md states it outright for this very LED bus, showing the
# inversion the board requires:
#       assign USER_nLED[7:0] = ~p0_out[7:0];   (active-low LEDs; invert)
#
# mbv_soc.tcl compensates for BOTH of these with explicit inverters (SECTION 5).
# These flags exist so the assumption has exactly ONE place to be flipped if a
# board schematic ever overturns it. They are consumed here only to emit the
# polarity contract into the XDC header; the BD holds the matching inverters.
#
# This is the one load-bearing assumption in this file that a schematic could
# still overturn (cf. the dqs_c[7] repair). Getting it backwards on sys_rst means
# the DDR4 MIG is held in reset forever, calibration never completes, the CPU is
# never released, and the board looks *dead* on the bench.
USER_NPB_ACTIVE_LOW = True
USER_NLED_ACTIVE_LOW = True

# Top-level port name on the BD wrapper  ->  source-pinmap signal it comes from.
#
# The two renames vs the old (unpinnable) BD port names are deliberate: the port
# name must not lie about the polarity of the pad it drives.
#     sys_rst            -> sys_rst_n              (active-LOW pad: USER_nPB[0])
#     init_calib_complete-> calib_complete_led_n   (active-LOW pad: USER_nLED[0])
BOARD_PINS = [
    {
        "port": "uart_txd",
        "src": "UART_TX_F",
        "index": CONSOLE_UART_LANE,
        "dir": "output",
        "note": "AXI UARTLite TX -> host FT4232 console lane "
                "(see CONSOLE_UART_LANE: lane 2, not 0)",
    },
    {
        "port": "uart_rxd",
        "src": "UART_RX_F",
        "index": CONSOLE_UART_LANE,
        "dir": "input",
        "note": "host FT4232 console lane -> AXI UARTLite RX "
                "(see CONSOLE_UART_LANE: lane 2, not 0)",
    },
    {
        "port": "sys_rst_n",
        "src": "USER_nPB",
        "index": 0,
        "dir": "input",
        "pulltype": "PULLUP",
        "note": "ACTIVE-LOW user pushbutton -> inverted in the BD -> "
                "ddr4_0/sys_rst (which is ACTIVE-HIGH). Idle button = 1 = "
                "reset DEASSERTED. See the polarity contract below.",
    },
    {
        "port": "calib_complete_led_n",
        "src": "USER_nLED",
        "index": 0,
        "dir": "output",
        "note": "ACTIVE-LOW LED pad. Driven with ~c0_init_calib_complete, so "
                "the LED is LIT when DDR4 calibration has COMPLETED. "
                "See the polarity contract below.",
    },
]

# IOSTANDARD every board pin above is expected to carry in the source. This is a
# CLASS check (all four live in 1.8 V banks), not a transcription of pin numbers:
# the actual PACKAGE_PIN and IOSTANDARD values are read from the source, never
# hardcoded here.
EXPECTED_BOARD_IOSTANDARD = "LVCMOS18"

BOARD_XDC_PATH = os.path.join(HERE, "board_pins.xdc")

# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #

# set_property <PROP> <VALUE> [get_ports {c0_..[i]}]  — braces, quotes, or bare.
_PROP_RE = re.compile(
    r"set_property\s+(?P<prop>\w+)\s+(?P<value>\S+)\s+"
    r"\[\s*get_ports\s+[{\"]?\s*"
    r"(?P<port>c0_[A-Za-z0-9_]+?)"
    r"(?:\[(?P<idx>\d+)\])?"
    r"\s*[}\"]?\s*\]"
)


class AssertionFail(Exception):
    """Raised for any violated expectation; turns into a non-zero exit."""


def require(cond, msg):
    if not cond:
        raise AssertionFail(msg)


def classify_comment(raw_line):
    """Return ('active', payload) | ('disabled', None) | None (not a comment).

    A DDR line is a Tcl comment.  Strip exactly one leading '#'.  If the first
    non-space character after that is another '#', the constraint is disabled
    (the socket's unused option) and must be rejected.
    """
    ls = raw_line.rstrip("\n").lstrip()
    if not ls.startswith("#"):
        return None
    after = ls[1:]
    if after.lstrip().startswith("#"):
        return ("disabled", None)
    return ("active", after.strip())


# The Arm pinmap calls the data-mask bus `c0_ddr4_dm_dbi_n`, but the Xilinx
# `ddr4_rtl` bus abstraction names that signal DM_N, so the generated HDL wrapper
# exposes it as `c0_ddr4_dm_n`. A PACKAGE_PIN on a port that does not exist is
# only a WARNING ([Vivado 12-584] "No ports matched") -- the constraint is
# silently dropped and the pin ends up unattached. Rename at emit time; the CSV
# records the source name so provenance is not lost.
EMIT_RENAME = {
    "c0_ddr4_dm_dbi_n": "c0_ddr4_dm_n",
}


def emit_port(port):
    """Top-level port name as it exists on the synthesised wrapper."""
    return EMIT_RENAME.get(port, port)


def short_signal(port):
    """Map a full port name to the pins.csv 'signal' column."""
    if port.startswith("c0_ddr4_"):
        return port[len("c0_ddr4_"):]
    return port  # c0_sys_clk_p / c0_sys_clk_n keep their full name


def parse_source(path):
    """Parse the pinmap.

    Returns (pin_records, extra_records, disabled_count) where:
      pin_records   = list of dict(signal, port_base, index|None, pin, provenance)
      extra_records = list of dict(prop, value, signal, port_base, index|None, raw)
    """
    pin_records = []
    extra_records = []
    disabled_count = 0

    with io.open(path, "r", encoding="utf-8", errors="strict") as fh:
        for raw in fh:
            cls = classify_comment(raw)
            if cls is None:
                continue
            kind, payload = cls
            if kind == "disabled":
                # Only count the ones that are actually DDR/sys-clk lines so an
                # unrelated commented block elsewhere doesn't skew the sanity.
                if any(p in raw for p in PORT_PREFIXES):
                    disabled_count += 1
                continue

            m = _PROP_RE.search(payload)
            if not m:
                continue
            port = m.group("port")
            if not port.startswith(PORT_PREFIXES):
                continue

            prop = m.group("prop")
            value = m.group("value")
            idx = m.group("idx")
            index = int(idx) if idx is not None else None
            sig = short_signal(port)

            if prop == "PACKAGE_PIN":
                pin_records.append({
                    "signal": sig,
                    "port_base": port,
                    "index": index,
                    "pin": value,
                    "provenance": "source",
                })
            elif prop in EXTRA_PROPS:
                extra_records.append({
                    "prop": prop,
                    "value": value,
                    "signal": sig,
                    "port_base": port,
                    "index": index,
                    "raw": payload,
                })

    return pin_records, extra_records, disabled_count


# --------------------------------------------------------------------------- #
# Board (non-DDR4) pins: a second pass over the LIVE (uncommented) lines       #
# --------------------------------------------------------------------------- #

# Same shape as _PROP_RE but for arbitrary (non-c0_) port names.
_BOARD_PROP_RE = re.compile(
    r"set_property\s+(?P<prop>\w+)\s+(?P<value>\S+)\s+"
    r"\[\s*get_ports\s+[{\"]?\s*"
    r"(?P<port>[A-Za-z][A-Za-z0-9_]*?)"
    r"(?:\[(?P<idx>\d+)\])?"
    r"\s*[}\"]?\s*\]"
)


def parse_board_source(path):
    """Index every LIVE set_property in the pinmap by (signal, index).

    The DDR4 block is commented out; the board signals are not. So here we take
    the exact complement of parse_source(): skip anything that is a comment, and
    collect PACKAGE_PIN / IOSTANDARD / PULLTYPE per (signal, index).

    Returns {(signal, index_or_None): {prop: value}}.
    """
    table = {}
    with io.open(path, "r", encoding="utf-8", errors="strict") as fh:
        for raw in fh:
            if raw.lstrip().startswith("#"):
                continue                      # commented out -> not a live pin
            m = _BOARD_PROP_RE.search(raw)
            if not m:
                continue
            idx = m.group("idx")
            key = (m.group("port"), int(idx) if idx is not None else None)
            table.setdefault(key, {})[m.group("prop")] = m.group("value")
    return table


def build_board_records(source_path, ddr_records, log):
    """Derive the four board pins from the source pinmap. Nothing is hardcoded.

    Every value (PACKAGE_PIN, IOSTANDARD) is read out of the source; this
    function only says WHICH source signal each BD port hangs off, asserts the
    source actually carries both properties, and asserts the pins collide with
    nothing.
    """
    table = parse_board_source(source_path)
    ddr_pins = {r["pin"]: r for r in ddr_records}

    records = []
    for spec in BOARD_PINS:
        key = (spec["src"], spec["index"])
        props = table.get(key)
        src_name = "{}[{}]".format(spec["src"], spec["index"])

        require(props is not None,
                "board signal {} has no live set_property lines in the source "
                "pinmap -- refusing to invent a pin for port '{}'".format(
                    src_name, spec["port"]))
        require("PACKAGE_PIN" in props,
                "board signal {} has no PACKAGE_PIN in the source".format(src_name))
        require("IOSTANDARD" in props,
                "board signal {} has no IOSTANDARD in the source -- an unpinned "
                "IOSTANDARD is exactly the DRC NSTD-1 this file exists to "
                "fix".format(src_name))

        pin = props["PACKAGE_PIN"]
        iostd = props["IOSTANDARD"]

        require(iostd == EXPECTED_BOARD_IOSTANDARD,
                "board signal {} has IOSTANDARD {} in the source, expected {} "
                "(class check -- if the board really moved banks, update "
                "EXPECTED_BOARD_IOSTANDARD deliberately)".format(
                    src_name, iostd, EXPECTED_BOARD_IOSTANDARD))

        require(pin not in ddr_pins,
                "board signal {} wants PACKAGE_PIN {}, which is already taken by "
                "the DDR4 interface ({}[{}])".format(
                    src_name, pin, ddr_pins.get(pin, {}).get("signal"),
                    ddr_pins.get(pin, {}).get("index")))

        records.append({
            "port": spec["port"],
            "signal": spec["src"],
            "index": spec["index"],
            "pin": pin,
            "iostandard": iostd,
            "pulltype": spec.get("pulltype", ""),
            "dir": spec["dir"],
            "note": spec["note"],
            "provenance": "source",
        })
        log("BOARD {:<22} <- {:<12} = {:<5} {}".format(
            spec["port"], src_name, pin, iostd))

    # No two board ports may share a pin either.
    seen = {}
    for r in records:
        require(r["pin"] not in seen,
                "board ports '{}' and '{}' both want PACKAGE_PIN {}".format(
                    seen.get(r["pin"]), r["port"], r["pin"]))
        seen[r["pin"]] = r["port"]

    require(len(records) == len(BOARD_PINS),
            "expected {} board pins, built {}".format(
                len(BOARD_PINS), len(records)))
    log("PASS  {} board pins derived from source, zero collisions with DDR4"
        .format(len(records)))
    return records


# --------------------------------------------------------------------------- #
# Assertions + repair                                                         #
# --------------------------------------------------------------------------- #

def widths_of(records):
    w = {}
    for r in records:
        w[r["signal"]] = w.get(r["signal"], 0) + 1
    return w


def indices_of(records, signal):
    return sorted(r["index"] for r in records if r["signal"] == signal)


def check_no_dup_pins(records, where):
    seen = {}
    for r in records:
        pin = r["pin"]
        if pin in seen:
            other = seen[pin]
            raise AssertionFail(
                "duplicate PACKAGE_PIN {} ({}) : {}[{}] and {}[{}]".format(
                    pin, where, other["signal"], other["index"],
                    r["signal"], r["index"]))
        seen[pin] = r


def build_records(source_path, log):
    """Parse, assert everything, apply the repair, assert again.

    Returns (pin_records_after_repair, extra_records).  Raises AssertionFail.
    """
    pin_records, extra_records, disabled_count = parse_source(source_path)

    # --- source-level assertions -------------------------------------------
    require(
        len(pin_records) == EXPECTED_SOURCE_PIN_COUNT,
        "expected {} source PACKAGE_PIN assignments, parsed {}".format(
            EXPECTED_SOURCE_PIN_COUNT, len(pin_records)))
    log("PASS  parsed {} source PACKAGE_PIN assignments".format(len(pin_records)))

    require(
        disabled_count == EXPECTED_DISABLED_COUNT,
        "expected {} disabled (double-#) DDR lines, saw {}".format(
            EXPECTED_DISABLED_COUNT, disabled_count))
    log("PASS  rejected {} disabled (double-#) DDR lines".format(disabled_count))

    check_no_dup_pins(pin_records, "source")
    log("PASS  zero duplicate package pins in source")

    src_widths = widths_of(pin_records)
    require(
        src_widths == EXPECTED_SOURCE_WIDTHS,
        "source width mismatch:\n  expected {}\n  actual   {}".format(
            EXPECTED_SOURCE_WIDTHS, src_widths))
    log("PASS  source widths match expectation ({} signals)".format(
        len(src_widths)))

    # The specific hole we are here to repair.
    dqs_c_src = indices_of(pin_records, "dqs_c")
    require(
        dqs_c_src == [0, 1, 2, 3, 4, 5, 6],
        "expected source dqs_c indices 0..6 (7 missing), got {}".format(
            dqs_c_src))
    log("PASS  confirmed dqs_c source indices 0..6 — [7] is the missing bit")

    # --- apply the repair ---------------------------------------------------
    repaired = list(pin_records)
    for (port_base, index), info in sorted(REPAIRS.items()):
        signal = short_signal(port_base)
        repaired.append({
            "signal": signal,
            "port_base": port_base,
            "index": index,
            "pin": info["pin"],
            "provenance": "inferred",
        })
        log("REPAIR {}[{}] = {} (inferred)  {}".format(
            port_base, index, info["pin"],
            "J20 is the DIFF_PAIR complement of K21"))

    # --- post-repair assertions --------------------------------------------
    check_no_dup_pins(repaired, "after repair")
    log("PASS  zero duplicate package pins after repair "
        "(J20 collides with nothing)")

    require(
        len(repaired) == EXPECTED_SOURCE_PIN_COUNT + len(REPAIRS),
        "expected {} pins after repair, have {}".format(
            EXPECTED_SOURCE_PIN_COUNT + len(REPAIRS), len(repaired)))
    log("PASS  final pin count = {}".format(len(repaired)))

    # Every indexed signal must now be a contiguous 0..N-1 bus, no holes.
    final_widths = widths_of(repaired)
    for sig in INDEXED_SIGNALS:
        idxs = indices_of(repaired, sig)
        expect = list(range(final_widths[sig]))
        require(
            idxs == expect,
            "signal {} indices not contiguous: {} (want {})".format(
                sig, idxs, expect))
    log("PASS  all indexed buses contiguous 0..N-1 (dqs_c now width {})".format(
        final_widths["dqs_c"]))
    require(final_widths["dqs_c"] == 8, "dqs_c must be width 8 after repair")

    return repaired, extra_records


# --------------------------------------------------------------------------- #
# Provenance                                                                   #
# --------------------------------------------------------------------------- #

def git_blob_sha1(data):
    """The SHA-1 git itself would give this content (`git hash-object`)."""
    h = hashlib.sha1()
    h.update(("blob %d\0" % len(data)).encode("ascii"))
    h.update(data)
    return h.hexdigest()


def source_provenance(path):
    with io.open(path, "rb") as fh:
        data = fh.read()
    # Record the path relative to the nanosoc_tech checkout, so the generated
    # header is the same on every machine (and names no home directory).
    shown = path
    tech = os.environ.get("SOCLABS_NANOSOC_TECH_DIR", "")
    if tech:
        tech = os.path.abspath(tech).rstrip("/") + "/"
        if os.path.abspath(path).startswith(tech):
            shown = "<nanosoc_tech>/" + os.path.abspath(path)[len(tech):]
    return {
        "path": shown,
        "git_blob_sha1": git_blob_sha1(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


# --------------------------------------------------------------------------- #
# Rendering (pure functions of the records — no timestamps, so --check is      #
# meaningful and the output is byte-stable across runs)                        #
# --------------------------------------------------------------------------- #

def sort_key(r):
    idx = -1 if r["index"] is None else r["index"]
    return (r["signal"], idx)


def render_csv(records, board_records):
    """pins.csv -- the machine-readable contract build.tcl gates the build on.

    Column 'port' is the EXACT top-level port name on the synthesised wrapper.
    It exists so no consumer ever has to *reconstruct* a port name from a signal
    name: build.tcl used to rebuild it as "c0_ddr4_$sig" plus a special case for
    the dm_dbi_n -> dm_n rename, which silently could not express a port like
    'uart_txd'. Emitting the name we actually constrain removes that whole class
    of drift.

    'iostandard' is empty for the DDR4 pins: mig.xdc is authoritative for their
    electrical set. It is populated for the board pins, which nothing else pins
    -- that emptiness was DRC NSTD-1.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["port", "signal", "index", "package_pin", "iostandard",
                "provenance"])
    for r in sorted(records, key=sort_key):
        index = "" if r["index"] is None else str(r["index"])
        w.writerow([emit_port(r["port_base"]), r["signal"], index, r["pin"],
                    "", r["provenance"]])
    for r in board_records:
        w.writerow([r["port"], r["signal"], str(r["index"]), r["pin"],
                    r["iostandard"], r["provenance"]])
    return buf.getvalue()


def render_board_xdc(records, prov):
    """board_pins.xdc -- PACKAGE_PIN + IOSTANDARD for the four non-DDR4 ports."""
    lines = []
    ad = lines.append

    ad("#" + "-" * 75)
    ad("# board_pins.xdc  --  Arm MPS3 (xcku115-flvb1760-1-c) non-DDR4 board pins")
    ad("#" + "-" * 75)
    ad("#")
    ad("# GENERATED FILE -- do not hand-edit.  Regenerate and diff:")
    ad("#     python3 gen_pins.py")
    ad("#     python3 gen_pins.py --check   # verify vs source")
    ad("#")
    ad("# Source of truth (read-only):")
    ad("#   {}".format(prov["path"]))
    ad("#   git blob sha1 : {}".format(prov["git_blob_sha1"]))
    ad("#   sha256        : {}".format(prov["sha256"]))
    ad("#")
    ad("# WHY THIS FILE EXISTS: uart_txd, uart_rxd, sys_rst_n and")
    ad("# calib_complete_led_n had no PACKAGE_PIN and no IOSTANDARD, so")
    ad("# write_bitstream failed DRC NSTD-1 (unspecified I/O standard) and UCIO-1")
    ad("# (unconstrained logical port). Unlike the DDR4 pins -- whose IOSTANDARD")
    ad("# comes from the IP's mig.xdc -- nothing else constrains these four, so")
    ad("# BOTH properties are set here. Both values are read from the source")
    ad("# pinmap; neither is transcribed.")
    ad("#")
    ad("#" + "=" * 75)
    ad("# POLARITY CONTRACT -- read this before changing any wiring.")
    ad("#")
    ad("# The 'n' in USER_nPB / USER_nLED is not decoration: both pads are")
    ad("# ACTIVE-LOW, as is every other 'n'-prefixed signal on this board.")
    ad("#")
    ad("#   sys_rst_n  <- USER_nPB[0]   idle (not pressed) = 1 ; pressed = 0")
    ad("#     ddr4_0/sys_rst is ACTIVE-HIGH. Wiring the pad straight to it would")
    ad("#     mean idle button = 1 = RESET ASSERTED: the DDR4 MIG would sit in")
    ad("#     reset forever, c0_init_calib_complete would never assert, the CPU")
    ad("#     would never be released from proc_sys_reset, and the board would")
    ad("#     look DEAD on the bench (indistinguishable from a calibration")
    ad("#     failure). mbv_soc.tcl therefore inverts it explicitly:")
    ad("#         sys_rst_n --[NOT]--> ddr4_0/sys_rst")
    ad("#     PULLTYPE PULLUP is set so a floating input cannot glitch the MIG")
    ad("#     into reset if the board does not itself pull the button up.")
    ad("#")
    ad("#   calib_complete_led_n <- USER_nLED[0]   driving 0 LIGHTS the LED")
    ad("#     c0_init_calib_complete is ACTIVE-HIGH (1 = DDR4 calibrated). Wired")
    ad("#     straight through, the LED would be DARK on success and LIT on")
    ad("#     failure -- an inverted bench signal that would be misread. So the BD")
    ad("#     drives ~c0_init_calib_complete:")
    ad("#         LED LIT  = DDR4 calibration COMPLETE (good; CPU released)")
    ad("#         LED DARK = not calibrated (or FPGA not configured)")
    ad("#" + "=" * 75)
    ad("#")
    ad("# CONSOLE LANE: uart_txd/uart_rxd are on FPGA UART lane {} -- NOT lane 0.".format(
        CONSOLE_UART_LANE))
    ad("# Lane 2 is the only lane with on-bench proof of reaching the host FT4232")
    ad("# (the board-proven nanosoc_design_wrapper.v uses it, and the monolithic")
    ad("# MPS3 build's 'hello' banner was observed on it). See gen_pins.py.")
    ad("#" + "-" * 75)
    ad("")

    for r in records:
        ad("# --- {}  ({}, from {}[{}]) ---".format(
            r["port"], r["dir"], r["signal"], r["index"]))
        for seg in _wrap(r["note"], 70):
            ad("#     {}".format(seg))
        ad("set_property PACKAGE_PIN {} [get_ports {{{}}}]".format(
            r["pin"], r["port"]))
        ad("set_property IOSTANDARD {} [get_ports {{{}}}]".format(
            r["iostandard"], r["port"]))
        if r["pulltype"]:
            ad("set_property PULLTYPE {} [get_ports {{{}}}]"
               "    ;# NOT from the source: deliberate safety add (see header)"
               .format(r["pulltype"], r["port"]))
        ad("")

    return "\n".join(lines)


def _signal_rank(signal):
    if signal in SIGNAL_ORDER:
        return (0, SIGNAL_ORDER.index(signal), signal)
    return (1, 0, signal)


def render_xdc(records, extras, prov):
    lines = []
    ad = lines.append

    ad("#" + "-" * 75)
    ad("# ddr4_pins.xdc  --  Arm MPS3 (xcku115-flvb1760-1-c) DDR4 SODIMM PACKAGE_PIN map")
    ad("#" + "-" * 75)
    ad("#")
    ad("# GENERATED FILE -- do not hand-edit.  Regenerate and diff:")
    ad("#     python3 poc/ddr4_mbv/gen_pins.py")
    ad("#     python3 poc/ddr4_mbv/gen_pins.py --check   # verify vs source")
    ad("#")
    ad("# Source of truth (read-only):")
    ad("#   {}".format(prov["path"]))
    ad("#   git blob sha1 : {}".format(prov["git_blob_sha1"]))
    ad("#   sha256        : {}".format(prov["sha256"]))
    ad("#   size          : {} bytes".format(prov["bytes"]))
    ad("#")
    ad("# The DDR4 pins live there as a commented-out block. This file parses the")
    ad("# ACTIVE ('# ' single-hash) PACKAGE_PIN lines and rejects the DISABLED")
    ad("# ('# #' double-hash) 2-rank/x8 socket options.")
    ad("#")
    ad("# REPAIR (provenance: inferred, marked below):")
    for (port_base, index), info in sorted(REPAIRS.items()):
        ad("#   {}[{}] = {}".format(port_base, index, info["pin"]))
        for seg in _wrap(info["reason"], 71):
            ad("#     {}".format(seg))
    ad("#")
    ad("# SCOPE: this file supplies PACKAGE_PIN only. IOSTANDARD / DCI / slew /")
    ad("# ODT / OUTPUT_IMPEDANCE constraints are expected to come from the DDR4")
    ad("# IP's generated mig.xdc. The two non-PACKAGE_PIN properties the source")
    ad("# does carry (for bg[0]) are surfaced verbatim at the end, marked, but")
    ad("# mig.xdc remains authoritative for the electrical constraint set.")
    ad("#" + "-" * 75)
    ad("")

    by_sig = {}
    for r in records:
        by_sig.setdefault(r["signal"], []).append(r)

    for signal in sorted(by_sig, key=_signal_rank):
        recs = sorted(by_sig[signal], key=lambda r: (-1 if r["index"] is None
                                                      else r["index"]))
        n = len(recs)
        inferred = [r for r in recs if r["provenance"] == "inferred"]
        tag = "  [{} inferred]".format(len(inferred)) if inferred else ""
        ad("# --- {}  ({} pin{}){} ---".format(
            signal, n, "" if n == 1 else "s", tag))
        for r in recs:
            base = emit_port(r["port_base"])
            if r["index"] is None:
                port = "{{{}}}".format(base)
            else:
                port = "{{{}[{}]}}".format(base, r["index"])
            suffix = ""
            if base != r["port_base"]:
                suffix = "    ;# source name: {} (ddr4_rtl bus calls it DM_N)".format(
                    r["port_base"])
            if r["provenance"] == "inferred":
                suffix = "    ;# INFERRED: DIFF_PAIR complement of K21 (see header)"
            ad("set_property PACKAGE_PIN {} [get_ports {}]{}".format(
                r["pin"], port, suffix))
        ad("")

    if extras:
        ad("#" + "-" * 75)
        ad("# Non-PACKAGE_PIN properties found in the source (verbatim, MARKED).")
        ad("# Prefer mig.xdc for the full electrical set; these are surfaced only")
        ad("# because the source explicitly pins them.")
        ad("#" + "-" * 75)
        for e in sorted(extras, key=lambda e: (e["signal"],
                                               -1 if e["index"] is None
                                               else e["index"], e["prop"])):
            if e["index"] is None:
                port = "{{{}}}".format(e["port_base"])
            else:
                port = "{{{}[{}]}}".format(e["port_base"], e["index"])
            ad("set_property {} {} [get_ports {}]    ;# SOURCE (verbatim)".format(
                e["prop"], e["value"], port))
        ad("")

    return "\n".join(lines)


def _wrap(text, width):
    words = text.split()
    out, cur = [], ""
    for wd in words:
        if cur and len(cur) + 1 + len(wd) > width:
            out.append(cur)
            cur = wd
        else:
            cur = wd if not cur else cur + " " + wd
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="path to fpga_pinmap.xdc (default: %(default)s)")
    ap.add_argument("--check", action="store_true",
                    help="verify emitted artefacts still match the source; "
                         "do not write. Exit non-zero on any drift.")
    args = ap.parse_args(argv)

    log_lines = []

    def log(msg):
        log_lines.append(msg)
        print(msg)

    try:
        require(os.path.isfile(args.source),
                "source pinmap not found: {}".format(args.source))
        records, extras = build_records(args.source, log)
        board = build_board_records(args.source, records, log)
        prov = source_provenance(args.source)
        csv_text = render_csv(records, board)
        xdc_text = render_xdc(records, extras, prov)
        board_text = render_board_xdc(board, prov)
    except AssertionFail as exc:
        print("\nASSERTION FAILED: {}".format(exc), file=sys.stderr)
        print("STOP. Not writing artefacts, not loosening the assertion.",
              file=sys.stderr)
        return 2

    if args.check:
        ok = True
        for path, want in ((CSV_PATH, csv_text), (XDC_PATH, xdc_text),
                           (BOARD_XDC_PATH, board_text)):
            if not os.path.isfile(path):
                print("CHECK FAIL: missing {}".format(path), file=sys.stderr)
                ok = False
                continue
            with io.open(path, "r", encoding="utf-8") as fh:
                have = fh.read()
            if have != want:
                print("CHECK FAIL: {} is stale vs source".format(path),
                      file=sys.stderr)
                ok = False
            else:
                print("CHECK OK: {}".format(os.path.basename(path)))
        if not ok:
            print("\nArtefacts do not match the source. Re-run gen_pins.py.",
                  file=sys.stderr)
            return 1
        print("\nAll artefacts match the source.")
        return 0

    with io.open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(csv_text)
    with io.open(XDC_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(xdc_text)
    with io.open(BOARD_XDC_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(board_text)

    print("\nWrote {}".format(CSV_PATH))
    print("Wrote {}".format(XDC_PATH))
    print("Wrote {}".format(BOARD_XDC_PATH))
    print("Final pin count: {} DDR4/sys_clk + {} board = {}".format(
        len(records), len(board), len(records) + len(board)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
