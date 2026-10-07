#!/usr/bin/env python3
"""new_rm.py -- scaffold a new RM from this template, and keep the template's
port list tied to the GENERATED boundary.

TWO MODES
---------
    python3 fpga/rp/_template/new_rm.py --check
        Assert the template wrapper's port list agrees with
        fpga/shell/generated/rp_wrapper_skeleton.sv, which tools/gen_boundary.py
        writes from fpga/shell/boundary.yaml. Exit 1 on any disagreement. If the
        generated file is absent (a tree that predates the generator), say so and
        exit 0 -- a missing input is a skip with a printed reason, not a pass in
        silence.

    python3 fpga/rp/_template/new_rm.py <name> [--design-id 0xNNNN] [--dest DIR]
        Copy the template to fpga/rp/<name>/ with every rename applied:
        wrapper file + module -> rp_<name>_wrapper, `<name>_ooc.xdc`, and the two
        RENAME ME lines in ooc_synth.tcl. Runs --check first and REFUSES to
        scaffold from a template that has drifted from the generated boundary.

WHY THE --check MODE EXISTS
---------------------------
The 47-port boundary is the most-duplicated fact in this repository: it appears
in every RM wrapper, in the shell-side stub, in pin_check's tables, in the
contract, and in the XDC. Adding a TEMPLATE to that list would make it worse,
not better -- a skeleton that has quietly gone stale is a machine for
manufacturing drifted wrappers.

So the template does not own a boundary. It owns a *body* -- the rm_id block and
the safe-idle tie-offs, which the generated skeleton deliberately leaves as
commented guidance because a generator cannot know what your RM drives -- and it
borrows the port list, with this check holding the two together.

The comparison is SEMANTIC, on (name, direction, width) triples in order, so it
is immune to formatting: the generated file writes `[NGPIO-1:0]dut_gpio_o` with
no space and this one writes `[NGPIO-1:0] dut_gpio_o`, and that is not a drift.

Stdlib only. Board-free. No imports from the repo.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TEMPLATE_WRAPPER = HERE / "rp_template_wrapper.sv"
GENERATED = ROOT / "fpga/shell/generated/rp_wrapper_skeleton.sv"

#: One port declaration: direction, optional type, optional [range], name.
_PORT = re.compile(
    r"^(input|output|inout)\s+(?:wire|logic|reg)?\s*"
    r"(?:\[\s*([^\]]+?)\s*\])?\s*(\w+)$")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _match_paren(s: str, i: int) -> int:
    depth = 0
    while i < len(s):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced parentheses")


def ports_of(path: pathlib.Path):
    """-> [(name, direction, width)] in declaration order.

    Same parse shape as fpga/dfx/pin_check.py, deliberately: if this file and
    the gate ever disagree about what a port list IS, the gate wins, and the
    cheapest way to keep them agreeing is to read ports the same way.
    """
    src = _strip_comments(path.read_text())
    m = re.search(r"\bmodule\s+(\w+)", src)
    if not m:
        raise SystemExit(f"{path}: no module declaration")
    i = m.end()
    while i < len(src) and src[i] not in "(#":
        i += 1
    if i < len(src) and src[i] == "#":          # skip the parameter block
        i = src.index("(", i)
        i = _match_paren(src, i) + 1
        while i < len(src) and src[i] != "(":
            i += 1
    body = src[i + 1:_match_paren(src, i)]

    out = []
    for decl in body.split(","):
        d = " ".join(decl.split())
        pm = _PORT.match(d)
        if pm:
            width = (pm.group(2) or "1").replace(" ", "")
            out.append((pm.group(3), pm.group(1), width))
    return out


def check() -> int:
    if not GENERATED.exists():
        print(f"SKIP: {GENERATED.relative_to(ROOT)} is absent -- this tree "
              f"predates tools/gen_boundary.py, so the template's own port "
              f"list is the only source. Nothing to compare.")
        return 0

    want = ports_of(GENERATED)
    got = ports_of(TEMPLATE_WRAPPER)
    print(f"== template boundary check ==\n"
          f"   generated {GENERATED.relative_to(ROOT)}  ({len(want)} ports)\n"
          f"   template  {TEMPLATE_WRAPPER.relative_to(ROOT)}  ({len(got)} ports)")

    if want == got:
        print(f"\nOK: the template's port list matches the generated boundary "
              f"exactly ({len(want)} ports).")
        return 0

    print(f"\nFAIL: the template has drifted from the generated boundary.")
    for a, b in zip(want, got):
        if a != b:
            print(f"  generated {a}   template {b}")
    if len(want) != len(got):
        wn, gn = {p[0] for p in want}, {p[0] for p in got}
        for n in sorted(wn - gn):
            print(f"  MISSING from the template: {n}")
        for n in sorted(gn - wn):
            print(f"  EXTRA in the template:     {n}")
    print(f"\n  Re-take the port list from {GENERATED.relative_to(ROOT)} and keep "
          f"the template's\n  rm_id block and tie-offs. Do NOT 'fix' the "
          f"generated file: it is written by\n  tools/gen_boundary.py from "
          f"fpga/shell/boundary.yaml, which is the authority.")
    return 1


def scaffold(name: str, design_id: str, dest: pathlib.Path) -> int:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise SystemExit(f"error: '{name}' is not a legal RM name "
                         f"(lowercase, digits and underscores)")
    if check():
        raise SystemExit("error: refusing to scaffold from a template that has "
                         "drifted from the generated boundary (see above)")
    if dest.exists():
        raise SystemExit(f"error: {dest} already exists")

    top = f"rp_{name}_wrapper"
    shutil.copytree(HERE, dest)
    (dest / "new_rm.py").unlink()                     # the copy needs no scaffolder

    wrapper = dest / f"{top}.sv"
    (dest / "rp_template_wrapper.sv").rename(wrapper)
    wrapper.write_text(
        wrapper.read_text()
        .replace("rp_template_wrapper", top)
        .replace("RM_ID_TEMPLATE", f"RM_ID_{name.upper()}")
        .replace("16'h00FF", f"16'h{design_id.upper()}"))

    xdc = dest / f"{name}_ooc.xdc"
    (dest / "template_ooc.xdc").rename(xdc)

    tcl = dest / "ooc_synth.tcl"
    tcl.write_text(re.sub(r'^set rm_top   "rp_template_wrapper"',
                          f'set rm_top   "{top}"',
                          re.sub(r'^set rm_name  "template"',
                                 f'set rm_name  "{name}"',
                                 tcl.read_text(), flags=re.M), flags=re.M))

    rel = dest.relative_to(ROOT) if ROOT in dest.parents else dest
    print(f"\nScaffolded {rel}/")
    for p in sorted(dest.iterdir()):
        print(f"   {p.name}")
    print(f"\nNext:\n"
          f"  1. paste {rel}/rm_list_snippet.tcl into fpga/dfx/rm_list.tcl\n"
          f"     (the RM_LIB block AND the rm_key appended to RM_ORDER)\n"
          f"  2. make -C fpga/dfx pin-check\n"
          f"  3. fill in the DUT where {top}.sv says >>> YOUR DUT GOES HERE <<<\n"
          f"\nFull walk-through: docs/site/docs/guides/adding-an-rm.md")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name", nargs="?", help="the new RM's short name, e.g. myrm")
    ap.add_argument("--check", action="store_true",
                    help="only verify the template against the generated boundary")
    ap.add_argument("--design-id", default="00FF",
                    help="16-bit design_id for the new RM (default: the "
                         "unallocated placeholder 00FF)")
    ap.add_argument("--dest", default=None,
                    help="destination directory (default: fpga/rp/<name>)")
    a = ap.parse_args()

    if a.check or not a.name:
        return check()
    did = a.design_id.lower()
    if did.startswith("0x"):          # not str.removeprefix: CI pins Python 3.8
        did = did[2:]
    did = did.zfill(4)
    if not re.fullmatch(r"[0-9a-f]{4}", did):
        raise SystemExit(f"error: --design-id {a.design_id!r} is not 16-bit hex")
    dest = pathlib.Path(a.dest) if a.dest else (ROOT / "fpga/rp" / a.name)
    return scaffold(a.name, did, dest.resolve())


if __name__ == "__main__":
    sys.exit(main())
