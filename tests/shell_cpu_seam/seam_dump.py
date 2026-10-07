#!/usr/bin/env python3
"""tests/shell_cpu_seam/seam_dump.py -- read, compare and extract from the BD
dumps fpga/shell/tools/bd_dump.tcl writes.

    seam_dump.py compare A B [--expect same|differ]
        The identity gate. `#` lines (tool version, label) are skipped; every
        other line must match. A dump too small to be a shell BD is refused
        before comparing: two EMPTY dumps are identical, and a gate that
        passes on two empties is the shape of gate this repo has been bitten
        by (the SystemRDL equivalence gate that could not fail).
        --expect differ is the negative control's mode: it FAILS when the two
        dumps are identical, and prints what differs when they are not.

    seam_dump.py compare A B --expect-delta FILE [--expect-delta FILE2 ...]
        The identity gate for DELIBERATE changes to the bare-metal BD (D13's
        usd_spi_0 replacing axi_quad_spi_0 at 0x44A4; INJ's DUTEGR inject
        path): what differs between A and B -- as `delta` prints it -- must be
        EXACTLY the union of the pinned lines of the FILEs
        (tests/shell_cpu_seam/golden/*_delta_mb.txt): nothing missing, nothing
        extra. So "the only change is this cell" is a measured
        fact, and a second, unintended change fails here by name.

    seam_dump.py delta A B
        Print the A -> B change in the golden format: every dump line with its
        ancestry (`CELL /x > PIN y > CONFIG.k=v`), each NET/INET exploded to one
        line per member (so a wire added to a 400-member clock net is one line,
        not the whole net), then `- ` for lines only in A and `+ ` for lines
        only in B, sorted. Deterministic: the dump is sorted, and so is this.

    seam_dump.py regmap DUMP JSON
        The generated MBV view (fpga/shell/generated/regmap_mbv.json, rendered
        by tools/gen_regmap.py from the Tcl SOURCES) against the address map
        Vivado actually built (the dump's microblaze_riscv_0/Data segments):
        every (base, size) must match, both ways. The regmap is derived by
        parsing Tcl, so this is the check that the parse means what Vivado did.
        Entries of kind "window" (LMB_TAIL, a UIO page INSIDE the LMB) are not
        BD segments and are skipped.

    seam_dump.py clamp DUMP
        Print the load-bearing wires (the POR/WDOG -> dfx_ctl decoupler clamp,
        the CPU reset domain) as `pin <- driver-set` lines, or `pin <- UNDRIVEN`.
        tests/shell_cpu_seam/golden/clamp_wiring_<cpu>.txt are this output;
        test_shell_cpu_seam.py feeds them to the clamp bench.

Stdlib only.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

#: A real shell BD dump has ~80 cells and ~6600 CONFIG values (2024.1, mb,
#: SHELL_TOUCH=1: 80 / 6661). Well under that is not a shell.
MIN_CELLS = 60
MIN_CONFIG = 3000

#: The wires whose absence validate_bd_design does not report (an unconnected
#: input is auto-tied, which is legal). Mirrors tools/shell_bd_guards.tcl.
COMMON_WIRES = [
    ("/dfx_ctl_0/ext_por_n_i", "/sys_rst_n"),
    ("/dut_clkrst_0/ext_por_n_i", "/sys_rst_n"),
    ("/dfx_ctl_0/wdt_reset_i", "/axi_timebase_wdt_0/wdt_reset"),
    ("/proc_sys_reset_shell/aux_reset_in", "/axi_timebase_wdt_0/wdt_reset"),
    ("/dfx_decoupler_0/decouple", "/dfx_ctl_0/decouple_en_o"),
    ("/dfx_ctl_0/decoupled_i", "/dfx_decoupler_0/decouple_status"),
]
MB_WIRES = [("/microblaze_0/Reset", "/proc_sys_reset_shell/mb_reset")]
MBV_WIRES = [
    ("/microblaze_riscv_0/Reset", "/proc_sys_reset_cpu/mb_reset"),
    ("/proc_sys_reset_cpu/aux_reset_in", "/axi_timebase_wdt_0/wdt_reset"),
    ("/proc_sys_reset_ddr/aux_reset_in", "/axi_timebase_wdt_0/wdt_reset"),
    ("/smartconnect_ddr/aresetn", "/proc_sys_reset_cpu/interconnect_aresetn"),
    ("/ddr4_0/c0_ddr4_aresetn", "/proc_sys_reset_ddr/peripheral_aresetn"),
    ("/telem_0/alarm_i", "/ddr4_0/c0_init_calib_complete"),
]


class DumpError(Exception):
    pass


def body(path: Path) -> list[str]:
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    cells = sum(1 for ln in lines if ln.startswith("CELL "))
    cfg = sum(1 for ln in lines if ln.startswith("  CONFIG."))
    if not any(ln.startswith("DESIGN ") for ln in lines) or cells < MIN_CELLS \
            or cfg < MIN_CONFIG:
        raise DumpError(f"{path}: not a shell BD dump (cells={cells}, "
                        f"config values={cfg}; need >= {MIN_CELLS}/{MIN_CONFIG})")
    return lines


def cpu_of(lines: list[str]) -> str:
    cells = {ln.split()[1] for ln in lines if ln.startswith("CELL ")}
    mb, mbv = "/microblaze_0" in cells, "/microblaze_riscv_0" in cells
    if mb == mbv:
        raise DumpError(f"expected exactly one CPU cell (mb={mb} mbv={mbv})")
    return "mbv" if mbv else "mb"


def nets(lines: list[str]) -> dict[str, list[str]]:
    """pin/port path -> the other members of its net ([] if on no net)."""
    out: dict[str, list[str]] = {}
    for ln in lines:
        if not ln.startswith("NET "):
            continue
        _, rest = ln.split(" ", 1)
        _name, _, members = rest.partition(" : ")
        m = members.split()
        for p in m:
            out[p] = [q for q in m if q != p]
    return out


def clamp_lines(lines: list[str]) -> list[str]:
    cpu = cpu_of(lines)
    n = nets(lines)
    wires = COMMON_WIRES + (MBV_WIRES if cpu == "mbv" else MB_WIRES)
    out = [f"cpu {cpu}"]
    for pin, _want in wires:
        others = n.get(pin, [])
        out.append(f"{pin} <- {' '.join(sorted(others)) if others else 'UNDRIVEN'}")
    return out


def check_clamp_lines(lines: list[str]) -> list[str]:
    """Problems with a clamp extract (empty list = every wire is present)."""
    cpu = lines[0].split()[1]
    wires = COMMON_WIRES + (MBV_WIRES if cpu == "mbv" else MB_WIRES)
    got = {}
    for ln in lines[1:]:
        pin, _, drv = ln.partition(" <- ")
        got[pin] = drv.split()
    bad = []
    for pin, want in wires:
        if pin not in got:
            bad.append(f"{pin}: absent from the extract")
        elif want not in got[pin]:
            bad.append(f"{pin}: driven by {got[pin]}, expected {want}")
    return bad


_CTX_KINDS = ("CELL", "PORT", "IPORT", "PIN", "IPIN")


def records(lines: list[str]) -> list[str]:
    """Each dump line with its ancestry, so an indented `CONFIG.k=v` names the
    cell/pin it belongs to; NET/INET lines exploded to one record per member."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for ln in lines:
        if ln.startswith(("NET ", "INET ")):
            kind, rest = ln.split(" ", 1)
            name, _, members = rest.partition(" : ")
            ms = members.split()
            out += [f"{kind} {name} : {m}" for m in ms] or [f"{kind} {name} : (no members)"]
            stack = []
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        text = ln.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        ctx = " > ".join(k for _, k in stack)
        out.append(f"{ctx} > {text}" if ctx else text)
        toks = text.split()
        stack.append((indent, " ".join(toks[:2]) if toks and toks[0] in _CTX_KINDS else text))
    return out


def delta(a: list[str], b: list[str]) -> list[str]:
    """The A -> B change as sorted `- rec` / `+ rec` lines (see `records`)."""
    ca, cb = Counter(records(a)), Counter(records(b))
    return ([f"- {r}" for r in sorted((ca - cb).elements())]
            + [f"+ {r}" for r in sorted((cb - ca).elements())])


def read_delta(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]


def regmap_problems(lines: list[str], view: dict) -> list[str]:
    segs = set()
    for ln in lines:
        m = re.match(r"ASEG /microblaze_riscv_0/Data/\S+ offset=(0x[0-9A-Fa-f]+) "
                     r"range=(0x[0-9A-Fa-f]+)", ln)
        if m:
            segs.add((int(m.group(1), 16), int(m.group(2), 16)))
    if not segs:
        return ["the dump has no microblaze_riscv_0/Data segments (not an mbv dump?)"]
    want = {(int(e["base"], 16), int(e["size"], 16)): e["name"]
            for e in view["blocks"] + view["memories"] if e.get("kind") != "window"}
    bad = [f"{n} 0x{b:08X}/0x{r:X} is in the regmap view but not in the BD"
           for (b, r), n in sorted(want.items()) if (b, r) not in segs]
    bad += [f"BD segment 0x{b:08X}/0x{r:X} is missing from the regmap view"
            for (b, r) in sorted(segs) if (b, r) not in want]
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--expect", choices=("same", "differ"), default="same")
    c.add_argument("--expect-delta", type=Path, action="append", default=None,
                   help="the A->B delta must equal this golden file exactly; repeat "
                        "it for several deliberate changes (their union is expected)")
    d = sub.add_parser("delta")
    d.add_argument("a", type=Path)
    d.add_argument("b", type=Path)
    r = sub.add_parser("regmap")
    r.add_argument("dump", type=Path)
    r.add_argument("json", type=Path)
    k = sub.add_parser("clamp")
    k.add_argument("dump", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "delta":
            print("\n".join(delta(body(args.a), body(args.b))))
            return 0
        if args.cmd == "compare" and args.expect_delta is not None:
            got = delta(body(args.a), body(args.b))
            want = []
            for f in args.expect_delta:
                part = read_delta(f)
                if not part:
                    print(f"SEAM_DUMP ERROR {f}: empty golden delta -- use "
                          "--expect same for an identity check", file=sys.stderr)
                    return 2
                want += part
            names = "+".join(f.name for f in args.expect_delta)
            extra = sorted((Counter(got) - Counter(want)).elements())
            missing = sorted((Counter(want) - Counter(got)).elements())
            for x in extra[:100]:
                print(f"UNEXPECTED {x}")
            for x in missing[:100]:
                print(f"MISSING    {x}")
            if extra or missing:
                print(f"SEAM_DUMP DELTA MISMATCH ({len(extra)} unexpected, {len(missing)} "
                      f"missing, vs {len(want)} pinned in {names})")
                return 1
            print(f"SEAM_DUMP DELTA AS PINNED ({len(want)} lines, "
                  f"{names}; cpu={cpu_of(body(args.b))})")
            return 0
        if args.cmd == "compare":
            a, b = body(args.a), body(args.b)
            diff = list(difflib.unified_diff(a, b, str(args.a), str(args.b), lineterm="", n=1))
            if args.expect == "same":
                if diff:
                    print("\n".join(diff[:200]))
                    print(f"SEAM_DUMP DIFFER ({sum(1 for d in diff if d[:1] in '+-')} changed lines)")
                    return 1
                print(f"SEAM_DUMP IDENTICAL ({len(a)} lines, cpu={cpu_of(a)})")
                return 0
            if not diff:
                print("SEAM_DUMP IDENTICAL -- but this comparison was expected to DIFFER "
                      "(negative control): the gate cannot see the change it was given")
                return 1
            print("\n".join(diff[:40]))
            print("SEAM_DUMP DIFFER as expected (negative control)")
            return 0
        if args.cmd == "regmap":
            bad = regmap_problems(body(args.dump), json.loads(args.json.read_text()))
            for b_ in bad:
                print(f"REGMAP MISMATCH: {b_}")
            print("SEAM_REGMAP " + ("FAIL" if bad else
                                    "OK -- the generated MBV view is the BD's address map"))
            return 1 if bad else 0
        lines = clamp_lines(body(args.dump))
        print("\n".join(lines))
        bad = check_clamp_lines(lines)
        for b_ in bad:
            print(f"# MISSING WIRE: {b_}", file=sys.stderr)
        return 1 if bad else 0
    except DumpError as e:
        print(f"SEAM_DUMP ERROR {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
