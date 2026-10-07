#!/usr/bin/env python3
"""gen_bus_manifest.py -- emit an IICE manifest for a whole BUS interface.

WHY THIS EXISTS
    Tracing one CPU's AHB master took 6 hand-written manifest entries. Tracing an
    interconnect does not scale that way: a single AXI4 port is ~20 signals across
    5 channels, and a NIC-400 with ten ports is ~200 entries and ~2,900 bits. Hand
    listing that is where transcription errors live, and a wrong `width:` is not
    caught by anything until the capture decodes to nonsense.

    So the bus shape is described ONCE, here, per protocol, and the manifest is
    generated. `stop_on_signal_not_found 1` (emitted by gen_idc.py) then turns any
    path mistake into a hard board-free error at instrumentation time.

WHAT IT IS NOT
    It does not know your hierarchy. You give it the scope; it gives you the
    signal set, the widths, the radices and a bit budget. Names follow the AMBA
    spec's own conventions, upper-case by default because that is what the RTL
    ports are called and Identify paths are CASE-SENSITIVE (INTERFACES.md §7).

USAGE
    python3 gen_bus_manifest.py --protocol axi4 \
        --hw-scope  /u_rm/u_soc/u_nic400/m0 \
        --sim-scope tb.u_soc.u_nic400.m0 \
        --clk-hw /dut_clk --clk-sim tb.dut_clk \
        --data-width 64 --id-width 4 --name IICE_NIC_M0 --depth 1024 \
        --qualified-sampling --out signals_nic400_m0.yaml

    python3 gen_bus_manifest.py --protocol ahb --budget-only ...   # cost, no file

BIT BUDGET IS THE POINT OF `--budget-only`. Measured headroom in this repo's RP is
123.5 free BRAM tiles ~= 4.45 Mbit, so `width x depth` is the number that decides
whether a probe set fits. Readout time is the other one: at the MEASURED host-XVC
rate of ~340 bit/s a 2,900-bit x 1024 capture is ~2.4 hours, which is why the
firmware XVC path exists. Print the budget before you spend a build.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# (suffix, width_expr, radix, sample, trigger)
#   width_expr: int, or one of "DATA", "STRB", "ID", "ADDR"
#   trigger: LEAVE FALSE HERE. It is set from the chosen qualifier, never by
#     hand -- see QUALIFIERS below and the note on why.
_Sig = Tuple[str, object, str, bool, bool]

AHB: Sequence[_Sig] = (
    ("HADDR",     "ADDR", "hex", True,  False),
    ("HTRANS",    2,      "bin", True,  False),
    ("HWRITE",    1,      "bin", True,  False),
    ("HSIZE",     3,      "bin", True,  False),
    ("HBURST",    3,      "bin", True,  False),
    ("HPROT",     4,      "bin", True,  False),
    ("HMASTLOCK", 1,      "bin", True,  False),
    ("HWDATA",    "DATA", "hex", True,  False),
    ("HRDATA",    "DATA", "hex", True,  False),
    ("HREADY",    1,      "bin", True,  False),
    ("HRESP",     1,      "bin", True,  False),
)

APB: Sequence[_Sig] = (
    ("PADDR",   "ADDR", "hex", True,  False),
    ("PSEL",    1,      "bin", True,  False),
    ("PENABLE", 1,      "bin", True,  False),
    ("PWRITE",  1,      "bin", True,  False),
    ("PWDATA",  "DATA", "hex", True,  False),
    ("PRDATA",  "DATA", "hex", True,  False),
    ("PSTRB",   "STRB", "bin", True,  False),
    ("PPROT",   3,      "bin", True,  False),
    ("PREADY",  1,      "bin", True,  False),
    ("PSLVERR", 1,      "bin", True,  False),
)

AXI4LITE: Sequence[_Sig] = (
    ("AWADDR",  "ADDR", "hex", True,  False),
    ("AWPROT",  3,      "bin", True,  False),
    ("AWVALID", 1,      "bin", True,  False),
    ("AWREADY", 1,      "bin", True,  False),
    ("WDATA",   "DATA", "hex", True,  False),
    ("WSTRB",   "STRB", "bin", True,  False),
    ("WVALID",  1,      "bin", True,  False),
    ("WREADY",  1,      "bin", True,  False),
    ("BRESP",   2,      "bin", True,  False),
    ("BVALID",  1,      "bin", True,  False),
    ("BREADY",  1,      "bin", True,  False),
    ("ARADDR",  "ADDR", "hex", True,  False),
    ("ARPROT",  3,      "bin", True,  False),
    ("ARVALID", 1,      "bin", True,  False),
    ("ARREADY", 1,      "bin", True,  False),
    ("RDATA",   "DATA", "hex", True,  False),
    ("RRESP",   2,      "bin", True,  False),
    ("RVALID",  1,      "bin", True,  False),
    ("RREADY",  1,      "bin", True,  False),
)

AXI4: Sequence[_Sig] = (
    # write address
    ("AWID",    "ID",   "hex", True,  False),
    ("AWADDR",  "ADDR", "hex", True,  False),
    ("AWLEN",   8,      "dec", True,  False),
    ("AWSIZE",  3,      "bin", True,  False),
    ("AWBURST", 2,      "bin", True,  False),
    ("AWLOCK",  1,      "bin", True,  False),
    ("AWCACHE", 4,      "bin", True,  False),
    ("AWPROT",  3,      "bin", True,  False),
    ("AWQOS",   4,      "bin", True,  False),
    ("AWVALID", 1,      "bin", True,  False),
    ("AWREADY", 1,      "bin", True,  False),
    # write data
    ("WDATA",   "DATA", "hex", True,  False),
    ("WSTRB",   "STRB", "bin", True,  False),
    ("WLAST",   1,      "bin", True,  False),
    ("WVALID",  1,      "bin", True,  False),
    ("WREADY",  1,      "bin", True,  False),
    # write response
    ("BID",     "ID",   "hex", True,  False),
    ("BRESP",   2,      "bin", True,  False),
    ("BVALID",  1,      "bin", True,  False),
    ("BREADY",  1,      "bin", True,  False),
    # read address
    ("ARID",    "ID",   "hex", True,  False),
    ("ARADDR",  "ADDR", "hex", True,  False),
    ("ARLEN",   8,      "dec", True,  False),
    ("ARSIZE",  3,      "bin", True,  False),
    ("ARBURST", 2,      "bin", True,  False),
    ("ARLOCK",  1,      "bin", True,  False),
    ("ARCACHE", 4,      "bin", True,  False),
    ("ARPROT",  3,      "bin", True,  False),
    ("ARQOS",   4,      "bin", True,  False),
    ("ARVALID", 1,      "bin", True,  False),
    ("ARREADY", 1,      "bin", True,  False),
    # read data
    ("RID",     "ID",   "hex", True,  False),
    ("RDATA",   "DATA", "hex", True,  False),
    ("RRESP",   2,      "bin", True,  False),
    ("RLAST",   1,      "bin", True,  False),
    ("RVALID",  1,      "bin", True,  False),
    ("RREADY",  1,      "bin", True,  False),
)

PROTOCOLS: Dict[str, Sequence[_Sig]] = {
    "ahb": AHB,
    "apb": APB,
    "axi4lite": AXI4LITE,
    "axi4": AXI4,
}

# Qualifier expressions: "when is this bus actually doing something?"
#
# TWO HARD CONSTRAINTS, both learned from the schema rejecting a first attempt:
#
#  1. `trigger: true` means "APPEARS IN trigger.expr", not "is wired to the
#     comparator for later use". The manifest validator enforces both directions,
#     and it is right to: a signal the hardware IICE triggers on but the sim
#     shadow does not would make the two windows disagree about where they start.
#     So the qualifier OWNS the trigger flags -- hence (expr, signals) together
#     rather than a bare string. It is also cheaper: trigger comparators are the
#     expensive IICE resource, and marking 12 handshake signals trigger-capable
#     when the expression uses 2 wastes trigger RAM for nothing.
#
#  2. `controller: none` (Simple Triggering) can only express a CONJUNCTION of
#     per-signal conditions. So every expression here is an AND-chain. An `||`
#     would be honoured by the sim shadow and SILENTLY mistranslated in hardware
#     (signals_nanosoc.yaml's note on `controller`), which is the worst kind of
#     wrong. That is why there is no "any activity" qualifier for AXI: read-OR-
#     write is not expressible. Qualify on ONE channel, or use two IICEs, or
#     move to `controller: statemachine`.
#
# {protocol: {qualifier_name: (expr, (signal_suffixes_used, ...))}}
QUALIFIERS: Dict[str, Dict[str, Tuple[str, Tuple[str, ...]]]] = {
    "ahb": {
        "active": ("htrans[1] == 1'b1 && hready == 1'b1", ("HTRANS", "HREADY")),
        "write":  ("htrans[1] == 1'b1 && hwrite == 1'b1 && hready == 1'b1",
                   ("HTRANS", "HWRITE", "HREADY")),
        "error":  ("hresp == 1'b1 && hready == 1'b1", ("HRESP", "HREADY")),
    },
    "apb": {
        "access": ("psel == 1'b1 && penable == 1'b1 && pready == 1'b1",
                   ("PSEL", "PENABLE", "PREADY")),
        "error":  ("pslverr == 1'b1 && pready == 1'b1", ("PSLVERR", "PREADY")),
    },
    "axi4lite": {
        "ar": ("arvalid == 1'b1 && arready == 1'b1", ("ARVALID", "ARREADY")),
        "aw": ("awvalid == 1'b1 && awready == 1'b1", ("AWVALID", "AWREADY")),
        "r":  ("rvalid == 1'b1 && rready == 1'b1", ("RVALID", "RREADY")),
        "w":  ("wvalid == 1'b1 && wready == 1'b1", ("WVALID", "WREADY")),
    },
    "axi4": {
        "ar": ("arvalid == 1'b1 && arready == 1'b1", ("ARVALID", "ARREADY")),
        "aw": ("awvalid == 1'b1 && awready == 1'b1", ("AWVALID", "AWREADY")),
        "r":  ("rvalid == 1'b1 && rready == 1'b1", ("RVALID", "RREADY")),
        "w":  ("wvalid == 1'b1 && wready == 1'b1", ("WVALID", "WREADY")),
        # end-of-burst only: far fewer samples, one per transaction
        "rlast": ("rvalid == 1'b1 && rready == 1'b1 && rlast == 1'b1",
                  ("RVALID", "RREADY", "RLAST")),
    },
}

DEFAULT_QUALIFIER = {"ahb": "active", "apb": "access",
                     "axi4lite": "ar", "axi4": "ar"}


class BusManifestError(Exception):
    pass


def resolve_width(spec: object, data_width: int, addr_width: int,
                  id_width: int) -> int:
    if isinstance(spec, int):
        return spec
    if spec == "DATA":
        return data_width
    if spec == "ADDR":
        return addr_width
    if spec == "ID":
        return id_width
    if spec == "STRB":
        if data_width % 8:
            raise BusManifestError(
                "data-width %d is not a multiple of 8, so WSTRB/PSTRB has no "
                "whole-byte width" % data_width)
        return data_width // 8
    raise BusManifestError("unknown width spec %r" % (spec,))


def qualifier_for(protocol: str, which: Optional[str]
                  ) -> Tuple[str, Tuple[str, ...]]:
    """``(expr, signal_suffixes)`` for the named qualifier."""
    try:
        table = QUALIFIERS[protocol]
    except KeyError:
        raise BusManifestError(
            "unknown protocol %r; known: %s"
            % (protocol, ", ".join(sorted(QUALIFIERS))))
    key = which or DEFAULT_QUALIFIER[protocol]
    if key not in table:
        raise BusManifestError(
            "unknown qualifier %r for %s; known: %s"
            % (key, protocol, ", ".join(sorted(table))))
    return table[key]


def signal_rows(protocol: str, data_width: int, addr_width: int, id_width: int,
                prefix: str = "", qualify_on: Optional[str] = None,
                ) -> List[Dict[str, object]]:
    """The manifest `signals:` entries for one bus port.

    `trigger:` is set ONLY for the signals the chosen qualifier expression uses
    -- see QUALIFIERS for why that is a correctness rule and not a style choice.
    """
    try:
        table = PROTOCOLS[protocol]
    except KeyError:
        raise BusManifestError(
            "unknown protocol %r; known: %s"
            % (protocol, ", ".join(sorted(PROTOCOLS))))
    if id_width < 1 and protocol == "axi4":
        raise BusManifestError(
            "axi4 needs --id-width >= 1 (AWID/BID/ARID/RID are real signals; "
            "pass 1 if your interconnect is single-ID)")
    _expr, trig_suffixes = qualifier_for(protocol, qualify_on)
    trig = set(trig_suffixes)
    rows: List[Dict[str, object]] = []
    for suffix, wspec, radix, sample, _unused in table:
        width = resolve_width(wspec, data_width, addr_width, id_width)
        port = prefix + suffix
        # Manifest signal names must match ^[a-z][a-z0-9_]*$ (SIGNAL_NAME_RE),
        # so the canonical name is lower-cased even though the PORT is not.
        rows.append({
            "name": port.lower(), "width": width, "radix": radix,
            "port": port, "sample": sample, "trigger": suffix in trig,
        })
    missing = trig - {r["port"][len(prefix):] for r in rows}
    if missing:
        raise BusManifestError(
            "qualifier names signal(s) %s that are not in the %s table -- the "
            "generator's own tables are inconsistent" % (sorted(missing), protocol))
    return rows


def budget(rows: Sequence[Dict[str, object]], depth: int) -> Dict[str, object]:
    """Sample/trigger bit cost, and what it means for BRAM and readout time."""
    sample_bits = sum(int(r["width"]) for r in rows if r["sample"])
    trigger_bits = sum(int(r["width"]) for r in rows if r["trigger"])
    total_bits = sample_bits * depth
    # A RAMB36 is 36 Kbit. This is a floor, not a placement guarantee: Synplify
    # packs by byte lane, so the real tile count is >= this (measured on the
    # 69-bit IICE, which took 2 tiles for 69x1024 = 70,656 bit = 1.9 tiles).
    ramb36 = (total_bits + 36 * 1024 - 1) // (36 * 1024)
    return {
        "n_signals": len(rows),
        "sample_bits": sample_bits,
        "trigger_bits": trigger_bits,
        "depth": depth,
        "total_bits": total_bits,
        "ramb36_floor": ramb36,
        # MEASURED 2026-07-30 on mps3_01: 101 shifts x 2051 bit in ~605 s.
        "readout_s_host_xvc": total_bits / 340.0,
    }


def render_yaml(protocol: str, hw_scope: str, sim_scope: str, clk_hw: str,
                clk_sim: str, rows: Sequence[Dict[str, object]], name: str,
                depth: int, trigger_time: str = "early",
                controller: str = "none", edge: str = "positive",
                qualified_sampling: bool = False,
                data_compression: bool = False,
                expr: Optional[str] = None) -> str:
    hw_scope = "/" + hw_scope.strip("/")
    sim_scope = sim_scope.rstrip(".")
    b = budget(rows, depth)
    L: List[str] = []
    L.append("# GENERATED by gen_bus_manifest.py -- edit the generator, not this file.")
    L.append("#")
    L.append("#   protocol %s   hw %s   sim %s" % (protocol, hw_scope, sim_scope))
    L.append("#")
    L.append("# COST (see gen_bus_manifest.budget):")
    L.append("#   %d signals, %d sample bits, %d trigger bits"
             % (b["n_signals"], b["sample_bits"], b["trigger_bits"]))
    L.append("#   %d bits x depth %d = %d bits (>= %d RAMB36; RP has ~123.5 free)"
             % (b["sample_bits"], depth, b["total_bits"], b["ramb36_floor"]))
    L.append("#   readout at the MEASURED host-XVC rate (~340 bit/s): %.0f s = %.1f h"
             % (b["readout_s_host_xvc"], b["readout_s_host_xvc"] / 3600.0))
    L.append("#   -> if that number is hours, the firmware XVC path is not optional.")
    L.append("#")
    L.append("# TRIGGER BITS are the expensive resource, not sample bits: only the")
    L.append("# handshake/control signals are trigger-capable here, because you")
    L.append("# qualify a bus on VALID/READY or HTRANS, never on data.")
    L.append("")
    L.append("iice:")
    L.append("  name: %s" % name)
    L.append("  depth: %d" % depth)
    L.append("  trigger_time: %s" % trigger_time)
    L.append("  controller: %s" % controller)
    L.append("  trigger_conditions: 1")
    L.append("  trigger_states: 2")
    if qualified_sampling:
        L.append("  # depth samples become depth TRANSACTIONS, not idle cycles.")
        L.append("  qualified_sampling: true")
    if data_compression:
        L.append("  data_compression: true")
    L.append("  clock:")
    L.append("    hw:   %s" % clk_hw)
    L.append("    sim:  %s" % clk_sim)
    L.append("    edge: %s" % edge)
    L.append("")
    L.append("signals:")
    for r in rows:
        L.append("  - name:    %s" % r["name"])
        L.append("    width:   %d" % r["width"])
        L.append("    hw:      %s/%s" % (hw_scope, r["port"]))
        L.append("    sim:     %s.%s" % (sim_scope, r["port"]))
        L.append("    sample:  %s" % ("true" if r["sample"] else "false"))
        L.append("    trigger: %s" % ("true" if r["trigger"] else "false"))
        L.append("    radix:   %s" % r["radix"])
    L.append("")
    L.append("trigger:")
    L.append("  expr: \"%s\"" % expr)
    return "\n".join(L) + "\n"


def prefix_expr(expr: str, suffixes: Sequence[str], prefix: str) -> str:
    """Rewrite a qualifier expression for a prefixed port set.

    The expressions are written against bare AMBA names (`htrans[1]`), but with
    `--prefix m0_` the manifest signal is `m0_htrans`.  Without this the expr
    would reference names that do not exist and the manifest would be rejected --
    correctly, but confusingly.
    """
    if not prefix:
        return expr
    out = expr
    for suf in sorted(suffixes, key=len, reverse=True):
        out = re.sub(r"\b%s\b" % re.escape(suf.lower()),
                     (prefix + suf).lower(), out)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--protocol", required=True, choices=sorted(PROTOCOLS))
    p.add_argument("--hw-scope", required=True,
                   help="Identify scope holding the bus ports, e.g. /u_rm/u_soc/m0")
    p.add_argument("--sim-scope", required=True,
                   help="dotted testbench scope, e.g. tb.u_soc.m0")
    p.add_argument("--clk-hw", default="/dut_clk")
    p.add_argument("--clk-sim", default="tb.dut_clk")
    p.add_argument("--data-width", type=int, default=32)
    p.add_argument("--addr-width", type=int, default=32)
    p.add_argument("--id-width", type=int, default=4)
    p.add_argument("--prefix", default="", help="port-name prefix, e.g. m0_")
    p.add_argument("--name", default="IICE_BUS")
    p.add_argument("--depth", type=int, default=1024)
    p.add_argument("--trigger-time", default="early",
                   choices=("early", "middle", "late"))
    p.add_argument("--qualified-sampling", action="store_true")
    p.add_argument("--data-compression", action="store_true")
    p.add_argument("--qualify-on", default=None, dest="qualify_on",
                   help="which qualifier to trigger/qualify on; per-protocol, "
                        "see QUALIFIERS (ahb: active|write|error, apb: "
                        "access|error, axi4/axi4lite: ar|aw|r|w[|rlast])")
    p.add_argument("--expr", default=None, help="override the qualifier expression")
    p.add_argument("--out", default="-")
    p.add_argument("--budget-only", action="store_true",
                   help="print the bit/BRAM/readout cost and exit; write nothing")
    a = p.parse_args(argv)

    try:
        rows = signal_rows(a.protocol, a.data_width, a.addr_width, a.id_width,
                           prefix=a.prefix, qualify_on=a.qualify_on)
        base_expr, trig_suffixes = qualifier_for(a.protocol, a.qualify_on)
        expr = a.expr or prefix_expr(base_expr, trig_suffixes, a.prefix)
    except BusManifestError as e:
        print("gen_bus_manifest: %s" % e, file=sys.stderr)
        return 2

    if a.budget_only:
        b = budget(rows, a.depth)
        print("protocol            %s" % a.protocol)
        print("signals             %d" % b["n_signals"])
        print("sample bits         %d" % b["sample_bits"])
        print("trigger bits        %d   (the expensive resource)" % b["trigger_bits"])
        print("depth               %d" % b["depth"])
        print("total buffer bits   %d" % b["total_bits"])
        print("RAMB36 floor        %d   (RP has ~123.5 free = ~4.45 Mbit)"
              % b["ramb36_floor"])
        print("readout @ ~340 b/s  %.0f s = %.2f h   (MEASURED host XVC)"
              % (b["readout_s_host_xvc"], b["readout_s_host_xvc"] / 3600.0))
        return 0

    text = render_yaml(
        a.protocol, a.hw_scope, a.sim_scope, a.clk_hw, a.clk_sim, rows,
        a.name, a.depth, trigger_time=a.trigger_time,
        qualified_sampling=a.qualified_sampling,
        data_compression=a.data_compression, expr=expr)
    if a.out == "-":
        sys.stdout.write(text)
    else:
        with open(a.out, "w") as fh:
            fh.write(text)
        print("gen_bus_manifest: wrote %s (%d signals)" % (a.out, len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
