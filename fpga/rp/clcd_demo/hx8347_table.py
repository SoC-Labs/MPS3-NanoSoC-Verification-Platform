#!/usr/bin/env python3
"""hx8347_table.py -- read the HX8347-D panel init table out of the FIRMWARE,
so the demo RM and its bench never own a second copy of it.

WHY THIS FILE EXISTS
--------------------
`firmware/clcd/hx8347_init.c` is the ONE transcription of the panel's power-on
sequence in this repository. Its provenance -- the exact upstream driver, commit
SHA, licence and datasheet cross-checks for every byte -- is recorded in
`firmware/clcd/PANEL_PROVENANCE.md`, and the values in it are the ones the board
is known to light with (`docs/CLCD_PANEL_FACTS.md`).

`fpga/rp/clcd_demo/` needs the SAME sequence in RTL, because every CLCD-KVM
handover HARD-RESETS the panel (`fpga/shell/ip/clcd_kvm/README.md` §10) and the
new owner must re-initialise it (`docs/contracts/dut-display-tunnel.md` §6). A
hand-retyped ROM would be a second transcription of a table whose whole value is
that it was transcribed once, carefully, with citations -- and it would drift the
first time a bring-up tweak landed in the firmware (MADCTL 0x16 and PANEL 0x36
are flagged in `hx8347_init.c` as the likeliest ones to move).

So the ROM is GENERATED from the firmware table by `gen_init_rom.py`, which uses
this parser; `tests/clcd_demo/` uses the SAME parser to compute what the RM must
emit; and `tests/clcd_demo/test_init_rom_fresh.py` fails if the checked-in ROM
is not byte-identical to a fresh render. Firmware table moves -> the gate goes
red -> you re-run the generator. That is the whole mechanism.

Stdlib only. Board-free. No imports from the repo, and nothing here reads RTL.
"""
from __future__ import annotations

import pathlib
import re
from typing import Dict, List, NamedTuple, Tuple

#: Opcode names, as spelled in firmware/clcd/hx8347_init.h. Their VALUES are
#: read from that header (never assumed here) -- only the spelling is fixed.
OP_NAMES = ("HX_CMD", "HX_DAT", "HX_DLY")


class Entry(NamedTuple):
    """One firmware table row, resolved to numbers."""
    op: int          # HX_CMD / HX_DAT / HX_DLY, as valued by the header
    val: int         # register index, datum, or millisecond count
    op_name: str     # the spelling, for readable diffs
    raw_val: str     # the token as written in the C, e.g. "HX_MADCTL_VALUE"


def _strip_c_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _clean_number_token(tok: str) -> str:
    """C integer-literal syntax -> Python. `0x2Fu` -> `0x2F`, `(uint8_t)x` -> `x`."""
    tok = re.sub(r"\(\s*uint\d+_t\s*\)", "", tok)
    tok = re.sub(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]+", r"\1", tok)
    return tok.strip()


def parse_header_defines(header: pathlib.Path, rotate_180: int = None
                         ) -> Dict[str, int]:
    """Resolve the `#define`s in hx8347_init.h to integers.

    The header carries ONE conditional -- `#if CLCD_ROTATE_180 / #else /
    #endif` around `HX_MADCTL_VALUE` -- and that switch decides which of the two
    landscape orientations ships. It is honoured here rather than hard-coded:
    pass `rotate_180=` to override, else the header's own
    `#ifndef CLCD_ROTATE_180 / #define CLCD_ROTATE_180 1` default is used.

    Raises ValueError if a name this module needs cannot be resolved -- a header
    restructure must fail loudly, not silently fall back to a stale constant.
    """
    src = _strip_c_comments(header.read_text())

    if rotate_180 is None:
        m = re.search(r"#\s*define\s+CLCD_ROTATE_180\s+(\d+)", src)
        if not m:
            raise ValueError(f"{header}: no `#define CLCD_ROTATE_180` default")
        rotate_180 = int(m.group(1))

    # Walk the file honouring only the ONE conditional we know about. Any other
    # #if would be mis-handled, so assert there is only the one.
    out: Dict[str, int] = {}
    skipping = False
    seen_rotate_if = False
    for line in src.splitlines():
        s = line.strip()
        cond = re.match(r"#\s*if\s+CLCD_ROTATE_180\s*$", s)
        if cond:
            seen_rotate_if = True
            skipping = not bool(rotate_180)
            continue
        if seen_rotate_if and re.match(r"#\s*else\s*$", s):
            skipping = not skipping
            continue
        if seen_rotate_if and re.match(r"#\s*endif\s*$", s):
            seen_rotate_if, skipping = False, False
            continue
        if skipping:
            continue
        m = re.match(r"#\s*define\s+(\w+)\s+(.+?)\s*\\?$", s)
        if not m:
            continue
        name, expr = m.group(1), _clean_number_token(m.group(2))
        if not expr or "(" in expr and name in ("HX_MADCTL_VALUE",) and "\\" in line:
            continue
        try:
            out[name] = int(eval(expr, {"__builtins__": {}}, dict(out)))  # noqa: S307
        except Exception:
            # Not an integer constant (a function-like macro, a string, a name
            # we have not seen yet). Skipped deliberately -- `require()` below
            # is what turns a MISSING name into an error.
            continue
    return out


def _multiline_defines(header: pathlib.Path, defines: Dict[str, int],
                       rotate_180: int = None) -> Dict[str, int]:
    """Second pass for `#define`s whose body is continued with a trailing `\\`.

    `HX_MADCTL_VALUE` is written across two lines in the shipped header, which
    the line-oriented pass above cannot see. Joining continuations first and
    re-running the same evaluation keeps ONE evaluator rather than two.
    """
    src = _strip_c_comments(header.read_text())
    src = re.sub(r"\\\s*\n", " ", src)          # join line continuations
    if rotate_180 is None:
        rotate_180 = defines.get("CLCD_ROTATE_180", 1)

    # Re-apply the single conditional to the joined text.
    def _branch(m):
        return m.group(1) if rotate_180 else m.group(2)
    src = re.sub(r"#\s*if\s+CLCD_ROTATE_180\s*\n(.*?)#\s*else\s*\n(.*?)#\s*endif",
                 _branch, src, flags=re.S)

    out = dict(defines)
    for m in re.finditer(r"#\s*define\s+(\w+)\s+([^\n]+)", src):
        name, expr = m.group(1), _clean_number_token(m.group(2))
        if name in out:
            continue
        try:
            out[name] = int(eval(expr, {"__builtins__": {}}, dict(out)))  # noqa: S307
        except Exception:
            continue
    return out


def header_constants(header: pathlib.Path, rotate_180: int = None) -> Dict[str, int]:
    d = parse_header_defines(header, rotate_180)
    return _multiline_defines(header, d, rotate_180)


def require(defines: Dict[str, int], *names: str) -> None:
    missing = [n for n in names if n not in defines]
    if missing:
        raise ValueError(
            "hx8347_init.h no longer defines " + ", ".join(missing) +
            " -- fpga/rp/clcd_demo/ reads the panel init table OUT of the "
            "firmware on purpose (see this module's header). Fix the parser, "
            "do not retype the table.")


def parse_table(csrc: pathlib.Path, defines: Dict[str, int]) -> List[Entry]:
    """`const hx8347_entry_t hx8347_init[] = { ... };` -> [Entry].

    Every row must match `{ <op>, <value> }` with `op` one of OP_NAMES; anything
    else in the initialiser is an error, not a silently-skipped row.
    """
    src = _strip_c_comments(csrc.read_text())
    m = re.search(r"hx8347_init\s*\[\s*\]\s*=\s*\{", src)
    if not m:
        raise ValueError(f"{csrc}: no `hx8347_init[] = {{` initialiser")
    i = m.end()
    depth = 1
    j = i
    while j < len(src) and depth:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
        j += 1
    body = src[i:j - 1]

    entries: List[Entry] = []
    for row in re.finditer(r"\{([^{}]*)\}", body):
        parts = [p.strip() for p in row.group(1).split(",") if p.strip()]
        if len(parts) != 2:
            raise ValueError(f"{csrc}: cannot parse table row {{{row.group(1)}}}")
        op_name, val_tok = parts
        if op_name not in OP_NAMES:
            raise ValueError(f"{csrc}: unknown opcode {op_name!r} in the table")
        require(defines, op_name)
        tok = _clean_number_token(val_tok)
        try:
            val = int(eval(tok, {"__builtins__": {}}, dict(defines)))  # noqa: S307
        except Exception as exc:
            raise ValueError(
                f"{csrc}: cannot resolve table value {val_tok!r} ({exc})") from exc
        entries.append(Entry(defines[op_name], val, op_name, val_tok.strip()))
    if not entries:
        raise ValueError(f"{csrc}: the init table parsed as EMPTY")
    return entries


def load(repo_root: pathlib.Path, rotate_180: int = None
         ) -> Tuple[List[Entry], Dict[str, int]]:
    """The one entry point: (table, header constants) for a repo checkout."""
    fw = pathlib.Path(repo_root) / "firmware" / "clcd"
    defines = header_constants(fw / "hx8347_init.h", rotate_180)
    require(defines, *OP_NAMES, "HX_REG_MADCTL", "HX_MADCTL_VALUE")
    return parse_table(fw / "hx8347_init.c", defines), defines


# --- the derived facts the RM needs, all taken FROM the table ----------------
#
# The demo RM re-issues the GRAM address window and the write-GRAM command
# before every repaint (the panel has been hard-reset under it, and a repaint
# that trusted a window it did not set would be trusting state it does not own).
# Those register INDICES and VALUES are not retyped here either: they are pulled
# back out of the parsed firmware table by matching the CMD byte.

#: HX8347-D address-window register indices, in the order the firmware writes
#: them. Datasheet-fixed (hx8347d_reg.h); the VALUES come from the table.
WINDOW_REGS = (0x08, 0x09, 0x04, 0x05, 0x06, 0x07, 0x02, 0x03)
GRAM_WRITE_CMD = 0x22


def window_writes(table: List[Entry], defines: Dict[str, int]
                  ) -> List[Tuple[int, int]]:
    """-> [(reg, datum)] for WINDOW_REGS, in WINDOW_REGS order, as the firmware
    table sets them. Raises if the firmware stops writing one of them."""
    cmd, dat = defines["HX_CMD"], defines["HX_DAT"]
    found: Dict[int, int] = {}
    for k, e in enumerate(table):
        if e.op == cmd and e.val in WINDOW_REGS and k + 1 < len(table):
            nxt = table[k + 1]
            if nxt.op == dat:
                found[e.val] = nxt.val
    missing = [f"0x{r:02X}" for r in WINDOW_REGS if r not in found]
    if missing:
        raise ValueError(
            "firmware/clcd/hx8347_init.c no longer writes window register(s) " +
            ", ".join(missing) + " -- fpga/rp/clcd_demo takes the window from "
            "the firmware table rather than retyping it.")
    return [(r, found[r]) for r in WINDOW_REGS]


def frame_geometry(table: List[Entry], defines: Dict[str, int]) -> Tuple[int, int]:
    """(width, height) implied by the firmware table's window END registers.

    col_end = {0x04 hi, 0x05 lo}, row_end = {0x08 hi, 0x09 lo}; the panel counts
    from zero, so width = col_end + 1. This is how the RM's default geometry is
    kept equal to the firmware's without either side stating "320x240".
    """
    w = dict(window_writes(table, defines))
    return ((w[0x04] << 8 | w[0x05]) + 1, (w[0x08] << 8 | w[0x09]) + 1)
