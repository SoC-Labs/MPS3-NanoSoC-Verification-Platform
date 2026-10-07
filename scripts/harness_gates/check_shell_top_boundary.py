#!/usr/bin/env python3
"""check_shell_top_boundary.py -- the STATIC side of the RP boundary vs the contract.

fpga/shell/boundary.yaml is the one declaration of the partition boundary.
tools/gen_boundary.py generates the RM-side views from it (pin_check's table,
the stub, the wrapper skeleton) and pin_check.py holds every RM wrapper to it.
Two hand-written copies were NOT gated until 2026-09-23, and they are exactly the
two a boundary change must edit by hand:

  * fpga/shell/shell_top.sv   -- the `rp_dut u_rp_dut ( .port(...), ... );` map
  * fpga/shell/bd/shell_bd.tcl -- the `create_bd_port -dir I|O [-from M -to N] rp_<port>`
                                  list the BD exposes to shell_top

This fails on any port missing from, extra in, or mis-directioned / mis-sized in
either copy. Directions are the SHELL's view in both boundary.yaml (`O` = shell
drives the RP) and the BD (`-dir O` = out of the BD towards the RP), so they
compare directly. Widths: `NGPIO` resolves through boundary.yaml's `ngpio`.

The YAML is read through tools/gen_boundary.py's own loader (which also
re-validates totals), never re-parsed here. Board-free, tool-free, sub-second.

    check_shell_top_boundary.py [--repo <root>] [--shell-top F] [--bd F]

Exit 0 pass, 1 mismatch, 2 cannot parse / load.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[2]
BD_PREFIX = "rp_"

#: Boundary signals that are driven in shell_top.sv itself and never cross the
#: BD, each with its reason. Held both ways: if one of these ever appears as a
#: BD port, the gate fails until it is removed from this list.
BD_BYPASS = {
    "qspi_io_i": "sampled by shell_top's own QSPI IOBUFs (shell_top.sv, the "
                 "u_qspi_d*_iobuf block) and passed straight to the RP -- "
                 "partition-pins.md v0.2 'Flash / QSPI XiP boundary'",
}

_INST = re.compile(r"\brp_dut\s+(?:#\s*\(.*?\)\s*)?u_rp_dut\s*\((.*?)\)\s*;", re.S)
_CONN = re.compile(r"\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_BD_PORT = re.compile(
    r"^\s*create_bd_port\s+-dir\s+(I|O|IO)\s+(?:-from\s+(\d+)\s+-to\s+(\d+)\s+)?"
    r"(?:-type\s+\S+\s+)?(" + BD_PREFIX + r"[A-Za-z0-9_]+)\s*$", re.M)


def strip_comments_sv(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def contract(repo):
    sys.path.insert(0, str(repo / "tools"))
    import gen_boundary  # noqa: E402  (the ONE loader; do not re-parse YAML here)
    bnd = gen_boundary.load(repo)
    out = {}
    for s in gen_boundary.signals(bnd):
        out[s["name"]] = (s["dir"], gen_boundary.bits_of(s, bnd["ngpio"]))
    return out


def shell_top_ports(path):
    text = strip_comments_sv(path.read_text())
    m = _INST.search(text)
    if not m:
        return None
    return _CONN.findall(m.group(1))


def bd_ports(path):
    """-> {port: (dir, width)} for every create_bd_port rp_*; comments skipped."""
    lines = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]
    out = {}
    dups = []
    for m in _BD_PORT.finditer("\n".join(lines)):
        d, hi, lo, name = m.group(1), m.group(2), m.group(3), m.group(4)
        width = abs(int(hi) - int(lo)) + 1 if hi is not None else 1
        port = name[len(BD_PREFIX):]
        if port in out:
            dups.append(port)
        out[port] = (d, width)
    return out, dups


def check(repo, shell_top, bd):
    errors = []
    want = contract(repo)

    names = shell_top_ports(shell_top)
    if names is None:
        return want, ["%s: no `rp_dut u_rp_dut ( ... );` instance found" % shell_top]
    seen = set()
    for n in names:
        if n in seen:
            errors.append("%s: u_rp_dut connects .%s twice" % (shell_top, n))
        seen.add(n)
    for n in sorted(set(want) - seen):
        errors.append("%s: u_rp_dut is MISSING .%s (boundary.yaml declares it)" % (shell_top, n))
    for n in sorted(seen - set(want)):
        errors.append("%s: u_rp_dut connects .%s, which boundary.yaml does NOT declare" % (shell_top, n))

    have, dups = bd_ports(bd)
    for n in dups:
        errors.append("%s: create_bd_port %s%s appears twice" % (bd, BD_PREFIX, n))
    for n in sorted(set(BD_BYPASS) & set(have)):
        errors.append("%s: %s%s is now a BD port, but BD_BYPASS in this gate says it is "
                      "driven in shell_top -- drop it from BD_BYPASS" % (bd, BD_PREFIX, n))
    for n in sorted(set(want) - set(have) - set(BD_BYPASS)):
        errors.append("%s: no `create_bd_port ... %s%s` (boundary.yaml declares %s)"
                      % (bd, BD_PREFIX, n, n))
    for n in sorted(set(have) - set(want)):
        errors.append("%s: create_bd_port %s%s has no boundary.yaml signal %s"
                      % (bd, BD_PREFIX, n, n))
    for n in sorted(set(have) & set(want)):
        wd, ww = want[n]
        hd, hw = have[n]
        if hd != wd:
            errors.append("%s: %s%s is -dir %s, boundary.yaml says %s (shell view)"
                          % (bd, BD_PREFIX, n, hd, wd))
        if hw != ww:
            errors.append("%s: %s%s is %d bit(s) wide, boundary.yaml says %d"
                          % (bd, BD_PREFIX, n, hw, ww))
    return want, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=REPO_DEFAULT)
    ap.add_argument("--shell-top", type=Path, help="default <repo>/fpga/shell/shell_top.sv")
    ap.add_argument("--bd", type=Path, help="default <repo>/fpga/shell/bd/shell_bd.tcl")
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    shell_top = args.shell_top or repo / "fpga" / "shell" / "shell_top.sv"
    bd = args.bd or repo / "fpga" / "shell" / "bd" / "shell_bd.tcl"
    try:
        want, errors = check(repo, shell_top, bd)
    except Exception as exc:  # GenError, OSError, ImportError: say so, exit 2
        print("check_shell_top_boundary: cannot run: %s: %s" % (type(exc).__name__, exc))
        return 2
    if errors:
        for e in errors:
            print("SHELL_TOP_BOUNDARY_FAIL %s" % e)
        print("check_shell_top_boundary: %d mismatch(es) against fpga/shell/boundary.yaml "
              "(%d ports)" % (len(errors), len(want)))
        return 1
    bits = sum(w for _, w in want.values())
    print("check_shell_top_boundary: OK -- shell_top.sv u_rp_dut and shell_bd.tcl rp_* "
          "both match boundary.yaml (%d ports / %d bits)" % (len(want), bits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
