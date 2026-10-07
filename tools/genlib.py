#!/usr/bin/env python3
"""genlib.py — the shared plumbing behind this repo's ``tools/gen_*.py``.

WHY THIS EXISTS
---------------
Three truths in this platform were each written out by hand in four or more
places and kept in step by comments that said "cross-checked by hand":

  * the shell register map  — ``platform_regs.h``, ``tests/common/regmap.py``,
    ``host/socket_harness/registers.py``, the pyverify client, and the table in
    ``docs/contracts/shell-regmap.md``;
  * the diag mailbox layout — the ``diag.h`` struct, the 6900 ``diag`` verb,
    ``host/socket_harness/xsdb.py``, ``scripts/mps3_diag.tcl``;
  * the RP⇄shell partition boundary — every RM wrapper, ``rp_dut_stub.sv``,
    ``fpga/dfx/pin_check.py``, ``docs/contracts/partition-pins.md``, the XDC.

A hand-copied truth is a truth that will be wrong somewhere. ``platform_regs.h``
said "CLCDKVM — RESERVED, NOT INSTANTIATED" while ``shell_bd.tcl`` instantiated
``clcd_kvm_0`` and assigned it an address; nothing in the tree could see the
contradiction. The fix is one derivation and N generated views.

WHAT A GENERATOR LOOKS LIKE
---------------------------
A ``tools/gen_*.py`` module defines ``build(repo) -> {relpath: full file text}``
and calls :func:`run`. Every output is the COMPLETE text of a tracked file, so
one uniform diff answers "is the tree fresh?" for hand-written files with a
generated region and for wholly generated files alike.

For a file that is only PARTLY generated (``platform_regs.h`` keeps hundreds of
lines of prose and bitfield ``#define``s that no BD or RTL can derive), the
generator reads the tracked file and :func:`splice`s the derived table between a
pair of fence comments. The prose stays hand-written and reviewable; the table
cannot drift.

THE CLI EVERY GENERATOR GETS
----------------------------
    gen_x.py                  # write the outputs into the working tree
    gen_x.py --list           # print the repo-relative paths it owns
    gen_x.py --check          # exit 1 + unified diff if the tree is stale
    gen_x.py --out-dir DIR    # write under DIR/<relpath> instead of the tree

``scripts/harness_gates/check_generated_fresh.py`` drives ``--list`` +
``--out-dir`` and diffs; that is the gate. ``--check`` is the same comparison
without a temp dir, for a developer at a terminal.

IDEMPOTENCE is structural, not asserted: output is a pure function of the
derivation inputs plus the tracked file's non-generated regions, and splicing
leaves the fences in place, so generate-twice is generate-once.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Comment syntax per output language. The fence is a COMMENT in the target
#: language so a generated region never breaks the file it lives in.
_COMMENT = {
    "c": ("/* ", " */"),
    "py": ("# ", ""),
    "tcl": ("### ", ""),
    "md": ("<!-- ", " -->"),
    "sv": ("// ", ""),
}


class GenError(RuntimeError):
    """A generator could not derive or splice — always fatal, never a warning."""


def markers(tag: str, style: str, generator: str) -> tuple[str, str]:
    """The exact BEGIN/END fence lines for ``tag`` in ``style``.

    The generator name is IN the fence: someone reading the file learns which
    tool to re-run without consulting a README that may not exist.
    """
    try:
        pre, post = _COMMENT[style]
    except KeyError:
        raise GenError(f"unknown comment style {style!r}") from None
    begin = f"{pre}BEGIN GENERATED[{tag}] — {generator} — DO NOT EDIT BY HAND{post}"
    end = f"{pre}END GENERATED[{tag}]{post}"
    return begin, end


def splice(text: str, tag: str, style: str, generator: str, body: str) -> str:
    """Replace the region between the ``tag`` fences in ``text`` with ``body``.

    Both fences must be present exactly once, in order. Anything else is a hard
    error: a generator that silently appends when it cannot find its fence
    produces a file that looks generated and is not, which is the failure mode
    this whole module exists to remove.
    """
    begin, end = markers(tag, style, generator)
    lines = text.splitlines(keepends=True)
    b = [i for i, ln in enumerate(lines) if ln.rstrip("\n") == begin]
    e = [i for i, ln in enumerate(lines) if ln.rstrip("\n") == end]
    if len(b) != 1 or len(e) != 1:
        raise GenError(
            f"fence [{tag}] not found exactly once "
            f"(begin×{len(b)}, end×{len(e)}); expected these two lines:\n"
            f"  {begin}\n  {end}"
        )
    if e[0] <= b[0]:
        raise GenError(f"fence [{tag}]: END appears before BEGIN")
    body_lines = body.splitlines(keepends=True)
    if body_lines and not body_lines[-1].endswith("\n"):
        body_lines[-1] += "\n"
    return "".join(lines[: b[0] + 1] + body_lines + lines[e[0]:])


def read(repo: Path, rel: str) -> str:
    p = repo / rel
    if not p.is_file():
        raise GenError(f"{rel}: not a file (generators only splice TRACKED files)")
    return p.read_text()


def _diff(rel: str, want: str, got: str) -> str:
    return "".join(
        difflib.unified_diff(
            got.splitlines(keepends=True),
            want.splitlines(keepends=True),
            fromfile=f"{rel} (tracked)",
            tofile=f"{rel} (regenerated)",
        )
    )


def run(build, generator: str, argv: list[str] | None = None) -> int:
    """Standard CLI for a generator. ``build(repo) -> {relpath: text}``."""
    ap = argparse.ArgumentParser(description=f"{generator} — generated-view writer")
    ap.add_argument("--repo", default=str(ROOT), help="repository root")
    ap.add_argument("--list", action="store_true",
                    help="print the repo-relative paths this generator owns")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 with a diff if the tracked outputs are stale")
    ap.add_argument("--out-dir", default=None,
                    help="write under DIR/<relpath> instead of into the tree")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()

    try:
        outputs = build(repo)
    except GenError as exc:
        print(f"FAIL: {generator}: {exc}", file=sys.stderr)
        return 1

    if args.list:
        for rel in sorted(outputs):
            print(rel)
        return 0

    if args.check:
        stale = []
        for rel, want in sorted(outputs.items()):
            got = (repo / rel).read_text() if (repo / rel).is_file() else ""
            if got != want:
                stale.append(rel)
                sys.stdout.write(_diff(rel, want, got))
        if stale:
            print(f"\nFAIL: {generator}: {len(stale)} stale output(s): "
                  f"{', '.join(stale)}\n      re-run: python3 tools/{generator}",
                  file=sys.stderr)
            return 1
        print(f"OK: {generator}: {len(outputs)} generated view(s) are fresh")
        return 0

    base = Path(args.out_dir).resolve() if args.out_dir else repo
    for rel, want in sorted(outputs.items()):
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(want)
        print(f"  wrote {dest if args.out_dir else rel}")
    return 0
