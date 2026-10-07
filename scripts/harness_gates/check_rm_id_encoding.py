#!/usr/bin/env python3
"""check_rm_id_encoding.py -- gate the v2 ``rm_id`` encoding across its THREE sources.

``rm_id`` is stated in three independent places, and a partial whose sources
disagree is a partial that lands on silicon and reports the wrong identity:

    1. the RM wrapper's ``localparam`` -- what the FABRIC actually drives out on
       the ``rm_id`` partition pin (the only one that is physically true);
    2. ``fpga/dfx/rm_list.tcl`` -- the build-flow source of truth, which
       build_dfx.tcl reads to write ``overlay_inputs.txt``;
    3. ``fpga/dfx/overlay/<rm_name>/manifest.json`` -- what the HOST pushes, and
       hence the value ``step_verify()`` compares DFXCTL.RM_ID against.

Nothing in hardware or firmware cross-checks these against each other. Firmware's
RM-load verify compares the CSR against the value carried in the pushed partial's
own 24-byte header (generated from the manifest) -- so if the manifest and the
wrapper disagree, **every swap of that RM fails verification on the board**, and
the failure looks like a swap/ICAP bug rather than a bookkeeping one. If instead
rm_list.tcl and the wrapper disagree, the manifest silently records an id the
fabric never drives. This repo has been bitten by exactly this class of
three-way drift before; the v2 encoding makes it cheap to catch mechanically.

ENCODING v2 (docs/VERSIONING_PLAN.md §3.2):

     31           24 23           16 15                            0
    +---------------+---------------+-------------------------------+
    |   ver major   |   ver minor   |         design_id[15:0]        |
    +---------------+---------------+-------------------------------+

    rm_id = (major << 24) | (minor << 16) | design_id

Checks performed:
  * rm_list.tcl parses, and every RM declares ``design_id`` + ``version``;
  * major/minor fit 8 bits, design_id fits 16 bits;
  * design_ids are UNIQUE across the library (a collision makes RM-load verify
    ambiguous and makes the firmware's masked name lookup return the wrong DUT);
  * the derived rm_id satisfies the invariant ``rm_id & 0xFFFF == design_id``;
  * rm_greybox is EXACTLY 0x00000000 (the carve-out: the DFX decoupler clamps
    rm_id to DECOUPLED_VALUE 0x0, and firmware/pyverify read an all-zero id as
    "no RM loaded" -- a versioned greybox would break both);
  * the wrapper localparam actually assigned to ``rm_id`` == the derived value
    (flat ``32'h...`` or the template's composed ``{MAJOR, MINOR, DESIGN_ID}``
    -- both are read, neither is trusted: the gate composes and compares);
  * the overlay manifest's ``rm_id`` == the derived value (skipped, with a
    notice, for any RM whose manifest has not been built yet -- payloads are
    gitignored, so a fresh clone legitimately has none).

Optionally cross-checks the pure-Python derivation against ``tclsh`` sourcing
rm_list.tcl for real, so the gate cannot drift from the Tcl it is gating.

Usage:
    python3 scripts/harness_gates/check_rm_id_encoding.py [--repo REPO_ROOT] [-v]
Exit: 0 = all sources agree, 1 = drift (prints every disagreement, not just the first).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GREYBOX_KEY = "rm_greybox"
GREYBOX_RM_ID = 0x00000000


# --- rm_list.tcl ------------------------------------------------------------
# Matches:  set RM_LIB(rm_led,design_id)  "0x001E"
_SET_RE = re.compile(
    r"^\s*set\s+RM_LIB\(\s*(?P<rm>\w+)\s*,\s*(?P<field>\w+)\s*\)\s+"
    r'"?(?P<val>[^";\n]*?)"?\s*(?:;#.*)?$',
    re.MULTILINE,
)
_ORDER_RE = re.compile(r"^\s*set\s+RM_ORDER\s+\[\s*list\s+(?P<rms>[^\]]+)\]", re.MULTILINE)


def parse_rm_list(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    text = path.read_text()
    m = _ORDER_RE.search(text)
    if not m:
        raise SystemExit(f"FAIL: could not find RM_ORDER in {path}")
    order = m.group("rms").split()

    lib: dict[str, dict[str, str]] = {}
    for hit in _SET_RE.finditer(text):
        lib.setdefault(hit.group("rm"), {})[hit.group("field")] = hit.group("val").strip()
    return order, lib


def parse_semver(ver: str, rm: str) -> tuple[int, int, int]:
    parts = ver.split(".")
    if len(parts) not in (2, 3):
        raise SystemExit(f"FAIL: {rm}: version '{ver}' is not M.m or M.m.p")
    try:
        nums = [int(p, 10) for p in parts]
    except ValueError:
        raise SystemExit(f"FAIL: {rm}: version '{ver}' has a non-integer component")
    major, minor = nums[0], nums[1]
    patch = nums[2] if len(nums) == 3 else 0
    return major, minor, patch


def derive_rm_id(design_id: int, major: int, minor: int) -> int:
    return ((major & 0xFF) << 24) | ((minor & 0xFF) << 16) | (design_id & 0xFFFF)


# --- wrapper .sv ------------------------------------------------------------
_ASSIGN_RE = re.compile(r"^\s*assign\s+rm_id\s*=\s*(?P<name>\w+)\s*;", re.MULTILINE)
# A sized Verilog literal: 8'd1, 8'h01, 16'h0007, 32'h0001_0007 (underscores allowed).
_SIZED_LIT = r"(?P<w>\d+)'(?P<b>[hHdDbB])(?P<v>[0-9A-Fa-f_]+)"


class WrapperError(ValueError):
    """A wrapper whose rm_id cannot be read. Collected, never fatal: the gate
    promises to print EVERY disagreement, and one unparseable wrapper must not
    hide a design_id collision or a manifest drift in another RM."""


def _lit(m: "re.Match[str]") -> tuple[int, int]:
    """(value, width) of a sized literal match from _SIZED_LIT."""
    base = {"h": 16, "d": 10, "b": 2}[m.group("b").lower()]
    return int(m.group("v").replace("_", ""), base), int(m.group("w"))


def _localparam_literal(text: str, name: str) -> tuple[int, int] | None:
    """Find `localparam [logic] [[N:0]] NAME = <sized literal>;` -> (value, width)."""
    lp = re.search(
        r"localparam\s+(?:logic\s*)?(?:\[\d+:0\]\s*)?" + re.escape(name)
        + r"\s*=\s*" + _SIZED_LIT + r"\s*;",
        text,
    )
    return _lit(lp) if lp else None


def wrapper_rm_id(sv: Path) -> tuple[int, str]:
    """Return (value, localparam_name) for the constant actually assigned to rm_id.

    Deliberately follows `assign rm_id = <NAME>;` rather than grepping for any
    RM_ID-ish localparam: a wrapper that declares the right constant but drives
    a different one is exactly the bug this gate exists to catch.

    Two spellings of NAME are accepted, and only two:

      1. the flat form   `localparam [31:0] NAME = 32'h0100001E;`
      2. the COMPOSED form the RM template (fpga/rp/_template) emits:
             localparam logic [7:0]  RM_VER_MAJOR = 8'd1;
             localparam logic [7:0]  RM_VER_MINOR = 8'd0;
             localparam logic [15:0] RM_DESIGN_ID = 16'h00FF;
             localparam logic [31:0] NAME = {RM_VER_MAJOR, RM_VER_MINOR, RM_DESIGN_ID};
         where each member must itself be a localparam with a sized literal of
         exactly the field's width (8, 8, 16). The composition is the v2 encoding
         written out, so the gate composes it the same way and compares -- it
         does not trust the wrapper's arithmetic any more than the flat form's.

    Raises WrapperError (collected by main, not fatal) on anything else.
    """
    text = sv.read_text()
    m = _ASSIGN_RE.search(text)
    if not m:
        raise WrapperError(f"{sv}: no `assign rm_id = <localparam>;` found")
    name = m.group("name")

    flat = _localparam_literal(text, name)
    if flat is not None:
        val, width = flat
        if width != 32:
            raise WrapperError(f"{sv}: `{name}` is a {width}-bit literal, rm_id is 32 bits")
        return val, name

    comp = re.search(
        r"localparam\s+(?:logic\s*)?(?:\[31:0\]\s*)?" + re.escape(name)
        + r"\s*=\s*\{\s*(?P<a>\w+)\s*,\s*(?P<b>\w+)\s*,\s*(?P<c>\w+)\s*\}\s*;",
        text,
    )
    if not comp:
        raise WrapperError(
            f"{sv}: rm_id is driven by '{name}', but no `localparam [31:0] {name} = 32'h...;` "
            f"and no `localparam [31:0] {name} = {{MAJOR, MINOR, DESIGN_ID}};` defines it"
        )
    fields = (("ver major", comp.group("a"), 8), ("ver minor", comp.group("b"), 8),
              ("design_id", comp.group("c"), 16))
    parts: list[int] = []
    for label, member, want_w in fields:
        lit = _localparam_literal(text, member)
        if lit is None:
            raise WrapperError(
                f"{sv}: `{name}` composes `{member}` ({label}), which is not a localparam "
                "with a sized literal"
            )
        val, width = lit
        if width != want_w:
            raise WrapperError(
                f"{sv}: `{member}` ({label}) is {width} bits wide; the v2 encoding field is "
                f"{want_w} bits, so the concatenation would not be the encoding"
            )
        if val >= (1 << want_w):
            raise WrapperError(f"{sv}: `{member}` = {val} does not fit {want_w} bits")
        parts.append(val)
    return derive_rm_id(parts[2], parts[0], parts[1]), name


# --- optional tclsh cross-check --------------------------------------------
def tcl_rm_ids(rm_list: Path) -> tuple[dict[str, int] | None, str | None]:
    """Source rm_list.tcl with a REAL tclsh and read back its rm_ids.

    Guards against this gate's Python re-implementation of the encoding drifting
    from the Tcl that the build flow actually executes.

    Returns (ids, error). NEVER raises: a Tcl failure is reported as an error
    string and appended alongside the Python findings, so that this cross-check
    can never preempt the precise per-RM diagnostics below (and so the gate
    still does useful work on a host with no tclsh).
    """
    tclsh = shutil.which("tclsh")
    if not tclsh:
        return None, None
    script = (
        f"source {rm_list.as_posix()}\n"
        'foreach rm [rm_all_names] { puts "$rm [rm_field $rm rm_id]" }\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as fh:
        fh.write(script)
        tmp = fh.name
    try:
        out = subprocess.run([tclsh, tmp], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        Path(tmp).unlink(missing_ok=True)
        return None, f"tclsh could not run rm_list.tcl: {exc}"
    finally:
        Path(tmp).unlink(missing_ok=True)
    if out.returncode != 0:
        first = next((ln for ln in out.stderr.strip().splitlines() if ln.strip()), "")
        return None, (
            "rm_list.tcl does not SOURCE cleanly under tclsh -- the build flow "
            f"itself would fail here: {first}"
        )
    ids: dict[str, int] = {}
    for line in out.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            ids[parts[0]] = int(parts[1], 16)
    return ids, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=None, help="repo root (default: infer from this file)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    repo = Path(a.repo).resolve() if a.repo else Path(__file__).resolve().parents[2]
    rm_list = repo / "fpga/dfx/rm_list.tcl"
    overlay_root = repo / "fpga/dfx/overlay"

    if not rm_list.is_file():
        print(f"FAIL: {rm_list} not found")
        return 1

    order, lib = parse_rm_list(rm_list)
    errors: list[str] = []
    notices: list[str] = []
    rows: list[tuple[str, str, str, str, str]] = []
    seen_design: dict[int, str] = {}

    tcl_ids, tcl_err = tcl_rm_ids(rm_list)

    for rm in order:
        ent = lib.get(rm, {})
        for field in ("design_id", "version", "rm_name", "wrapper_dir", "top"):
            if field not in ent:
                errors.append(f"{rm}: rm_list.tcl declares no '{field}'")
        if not {"design_id", "version", "rm_name", "wrapper_dir", "top"} <= ent.keys():
            continue

        try:
            design_id = int(ent["design_id"], 0)
        except ValueError:
            errors.append(f"{rm}: design_id '{ent['design_id']}' is not an integer")
            continue
        major, minor, _patch = parse_semver(ent["version"], rm)

        # --- well-formedness ------------------------------------------------
        if not 0 <= design_id <= 0xFFFF:
            errors.append(f"{rm}: design_id 0x{design_id:X} does not fit 16 bits")
        for label, v in (("major", major), ("minor", minor)):
            if not 0 <= v <= 0xFF:
                errors.append(f"{rm}: version {label}={v} does not fit its 8-bit field")
        if design_id in seen_design:
            errors.append(
                f"{rm}: design_id 0x{design_id:04X} COLLIDES with {seen_design[design_id]} "
                "-- design_ids must be unique"
            )
        else:
            seen_design[design_id] = rm

        expect = derive_rm_id(design_id, major, minor)

        # the encoding invariant that makes old ids survive as (rm_id & 0xFFFF)
        if expect & 0xFFFF != design_id:
            errors.append(f"{rm}: rm_id 0x{expect:08X} & 0xFFFF != design_id 0x{design_id:04X}")

        # the greybox carve-out
        if rm == GREYBOX_KEY and expect != GREYBOX_RM_ID:
            errors.append(
                f"{rm}: rm_id is 0x{expect:08X}, MUST be 0x{GREYBOX_RM_ID:08X}. The greybox is "
                "held at design 0x0000 / v0.0 on purpose: the DFX decoupler clamps rm_id to "
                "DECOUPLED_VALUE 0x0, and firmware + pyverify read an all-zero rm_id as "
                "'no RM loaded'. Do not version the greybox."
            )

        # --- source 1 vs the Tcl the flow really runs ------------------------
        if tcl_ids is not None and rm in tcl_ids and tcl_ids[rm] != expect:
            errors.append(
                f"{rm}: this gate derives 0x{expect:08X} but tclsh-sourced rm_list.tcl "
                f"yields 0x{tcl_ids[rm]:08X} -- the gate and the build flow disagree"
            )

        # --- source 2: the RM wrapper localparam (what the fabric drives) ----
        sv = repo / ent["wrapper_dir"] / f"{ent['top']}.sv"
        if not sv.is_file():
            errors.append(f"{rm}: wrapper not found: {sv}")
            wrap_s = "MISSING"
        else:
            try:
                got, name = wrapper_rm_id(sv)
            except WrapperError as exc:
                errors.append(str(exc))
                got, name, wrap_s = None, "?", "UNREADABLE"
            else:
                wrap_s = f"0x{got:08X}"
            if got is not None and got != expect:
                errors.append(
                    f"{rm}: WRAPPER DRIFT -- {sv.relative_to(repo)} drives {name} = "
                    f"0x{got:08X}, but rm_list.tcl derives 0x{expect:08X} "
                    f"(design_id 0x{design_id:04X} @ v{major}.{minor}). The fabric is the "
                    "only source that is physically true; fix whichever is wrong."
                )

        # --- source 3: the overlay manifest (what the host pushes) -----------
        man = overlay_root / ent["rm_name"] / "manifest.json"
        if not man.is_file():
            man_s = "-"
            notices.append(f"{rm}: no {man.relative_to(repo)} yet (not built) -- manifest check skipped")
        else:
            try:
                got_m = int(json.loads(man.read_text())["rm_id"], 0)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                errors.append(f"{rm}: cannot read rm_id from {man.relative_to(repo)}: {exc}")
                man_s = "BAD"
            else:
                man_s = f"0x{got_m:08X}"
                if got_m != expect:
                    errors.append(
                        f"{rm}: MANIFEST DRIFT -- {man.relative_to(repo)} says rm_id "
                        f"0x{got_m:08X}, but rm_list.tcl derives 0x{expect:08X}. The manifest "
                        "feeds the pushed partial's 24-byte header, which step_verify() "
                        "compares against DFXCTL.RM_ID -- so EVERY swap of this RM would fail "
                        "verification on the board. Re-run `make -C fpga/dfx overlays`."
                    )

        rows.append((rm, f"0x{design_id:04X}", f"v{major}.{minor}", f"0x{expect:08X}",
                     f"{wrap_s} / {man_s}"))

    # The tclsh cross-check is appended LAST, so it can never preempt the precise
    # per-RM diagnostics above (e.g. a design_id collision makes rm_list.tcl
    # refuse to source at all -- we still want the "COLLIDES with" line).
    if tcl_err:
        errors.append(tcl_err)

    # --- report -------------------------------------------------------------
    print("== rm_id encoding v2 gate: {ver_major[31:24], ver_minor[23:16], design_id[15:0]} ==")
    if tcl_ids is not None:
        cross = "   (cross-checked against tclsh)"
    elif tcl_err:
        cross = "   (tclsh cross-check FAILED -- see below)"
    else:
        cross = "   (tclsh absent: Python derivation only)"
    print(f"   rm_list.tcl: {rm_list.relative_to(repo)}" + cross)
    print()
    print(f"   {'RM':<22} {'design':<8} {'ver':<6} {'rm_id':<12} wrapper / manifest")
    for rm, did, ver, rid, srcs in rows:
        print(f"   {rm:<22} {did:<8} {ver:<6} {rid:<12} {srcs}")
    if a.verbose or notices:
        print()
        for n in notices:
            print(f"   NOTE: {n}")

    if errors:
        print()
        print(f"FAIL: {len(errors)} rm_id encoding error(s):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print()
    print(f"OK: {len(rows)} RMs -- wrapper localparam, rm_list.tcl and manifest all agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
