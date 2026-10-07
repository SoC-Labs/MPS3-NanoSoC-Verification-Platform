#!/usr/bin/env python3
"""gen_diag.py — every view of the diag mailbox, rendered from ONE list.

THE TRUTH
---------
``MPS3_DIAG_FIELDS(X)`` in ``firmware/common/diag.h``. One row per word of the
mailbox, in declaration order::

    X(icap_bytes, COUNT, "icap_bytes")  /* total bytes written to HWICAP.WF */

carrying the C field name, a class (``META`` for the two header words, ``COUNT``
for a counter, ``PAD`` for ``reserved[]``) and the JSON/wire key the 6900
``diag`` verb transports it under. Row index == word offset, which is what a
JTAG reader hard-codes.

THE VIEWS
---------
* ``firmware/common/diag.h`` — the ``mps3_diag_t`` struct itself, as literal C
  declarations spliced between fences INSIDE the typedef. It is rendered rather
  than written as ``MPS3_DIAG_FIELDS(DECL)`` for one concrete reason:
  ``scripts/harness_gates/check_diag_field_parity.py`` PARSES that struct to
  learn what a counter is. Behind a macro expansion it would parse zero fields,
  print "covers every counter" over an empty set and exit 0 — the exact
  "gate that cannot fail" that gate's own preamble was written to stop. The
  ``_Static_assert`` block under the struct is what makes the rendered form
  provably identical to the list (size + every offset), at compile time.
* ``host/socket_harness/xsdb.py`` — ``DIAG_FIELDS``, the board-free Python
  reader's ``(word_offset, name)`` table.
* ``scripts/mps3_diag.tcl`` — ``FIELDS``, the xsdb reader's flat
  ``{index name ...}`` list.

Both readers listed 20 of the 25 counters when this generator was written, the
JSON verb transported 14, and nothing in the tree could see the disagreement.
A counter nobody reads out is a counter that reads back its init value forever.

The comments live in the LIST, next to the field they describe, and are carried
through into the rendered struct — so the knowledge does not have to be
duplicated (or lost) to make the layout derivable.

    tools/gen_diag.py [--check|--list|--out-dir DIR]
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import genlib  # noqa: E402  (path shim above must run first)

GENERATOR = "gen_diag.py"

DIAG_H = "firmware/common/diag.h"
XSDB_PY = "host/socket_harness/xsdb.py"
DIAG_TCL = "scripts/mps3_diag.tcl"

#: Row classes. META = the two header words mps3_diag_init() owns; COUNT = a
#: published counter; PAD = reserved[] (MPS3_DIAG_RESERVED_WORDS words).
META = "META"
COUNT = "COUNT"
PAD = "PAD"
CLASSES = (META, COUNT, PAD)

#: Column the struct's trailing comments start at (matches the hand-written
#: file this replaced; longer declarations push their own comment right).
_COMMENT_COL = 33


@dataclass(frozen=True)
class Field:
    """One row of ``MPS3_DIAG_FIELDS(X)``."""
    name: str
    cls: str
    key: str
    comment: str            #: trailing /* ... */ text, "" if none (may be multi-line)
    pre_comments: tuple     #: standalone /* ... */ blocks that precede this row
    words: int              #: 32-bit words the row occupies (PAD > 1)
    word_offset: int        #: running word offset == declaration index


def renumber(fields) -> list:
    """Re-derive ``word_offset`` from the row order (used after a list edit)."""
    out, off = [], 0
    for f in fields:
        out.append(replace(f, word_offset=off))
        off += f.words
    return out


# --------------------------------------------------------------------------- #
# Parse the X-macro list out of diag.h — it is the source; there is no YAML.
# --------------------------------------------------------------------------- #
_ROW_RX = re.compile(r'X\(\s*(\w+)\s*,\s*(\w+)\s*,\s*"([^"]*)"\s*\)')
_COMMENT_RX = re.compile(r"/\*.*?\*/", re.S)


def _macro_body(text: str, name: str) -> str:
    """The full (line-spliced) body of a backslash-continued ``#define``.

    Returns the body with the trailing backslashes removed but the NEWLINES
    kept, because "is this comment on the same line as the row before it?" is
    how a trailing comment is told from a standalone block.
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(f"#define {name}(X)"):
            start = i
            break
    if start is None:
        raise genlib.GenError(
            f"{DIAG_H}: no `#define {name}(X)` — that list is the single source "
            "for the mailbox layout; without it nothing here can be derived")
    body: list[str] = []
    i = start
    while True:
        raw = lines[i]
        cont = raw.rstrip().endswith("\\")
        body.append(raw.rstrip()[:-1].rstrip() if cont else raw.rstrip())
        if not cont:
            break
        i += 1
        if i >= len(lines):
            raise genlib.GenError(f"{DIAG_H}: {name} runs off the end of the file")
    body[0] = body[0][len(f"#define {name}(X)"):]
    return "\n".join(body)


def _reserved_words(text: str) -> int:
    m = re.search(r"#define\s+MPS3_DIAG_RESERVED_WORDS\s+(\d+)", text)
    if not m:
        raise genlib.GenError(
            f"{DIAG_H}: MPS3_DIAG_RESERVED_WORDS is not defined — the PAD row's "
            "width has no source")
    return int(m.group(1))


def parse_fields(text: str) -> list:
    """``MPS3_DIAG_FIELDS(X)`` -> ``[Field, ...]`` in declaration order."""
    body = _macro_body(text, "MPS3_DIAG_FIELDS")
    pad_words = _reserved_words(text)

    # Tokenize rows and comments in source order; a comment that starts on the
    # SAME line a row ended on is that row's trailing comment, anything else is
    # a standalone block that precedes the next row.
    toks = []
    for m in list(_ROW_RX.finditer(body)) + list(_COMMENT_RX.finditer(body)):
        toks.append((m.start(), m.end(), m))
    toks.sort(key=lambda t: t[0])

    fields: list[Field] = []
    pending: list[str] = []
    prev_end = 0
    prev_is_row = False
    for start, end, m in toks:
        gap = body[prev_end:start]
        if _ROW_RX.fullmatch(m.group(0)):
            name, cls, key = m.group(1), m.group(2), m.group(3)
            if cls not in CLASSES:
                raise genlib.GenError(
                    f"{DIAG_H}: field {name!r} has class {cls!r}; expected one "
                    f"of {', '.join(CLASSES)}")
            if cls != PAD and not key:
                raise genlib.GenError(
                    f"{DIAG_H}: field {name!r} ({cls}) has an empty wire key — "
                    "every transported field needs one")
            fields.append(Field(name=name, cls=cls, key=key, comment="",
                                pre_comments=tuple(pending),
                                words=pad_words if cls == PAD else 1,
                                word_offset=0))
            pending = []
            prev_is_row = True
        else:
            inner = m.group(0)[2:-2].strip("\n")
            if prev_is_row and "\n" not in gap and fields:
                fields[-1] = replace(fields[-1], comment=inner)
            else:
                pending.append(inner)
            prev_is_row = False
        prev_end = end

    if not fields:
        raise genlib.GenError(f"{DIAG_H}: MPS3_DIAG_FIELDS parsed to ZERO rows")
    if pending:
        raise genlib.GenError(
            f"{DIAG_H}: trailing comment(s) after the last row of "
            "MPS3_DIAG_FIELDS would be dropped from the struct: "
            f"{pending[0][:40]!r}")
    if [f.cls for f in fields].count(PAD) != 1 or fields[-1].cls != PAD:
        raise genlib.GenError(
            f"{DIAG_H}: expected exactly one PAD row, last — the readers assume "
            "every other row is one word at its declaration index")
    dupes = {f.name for f in fields if [g.name for g in fields].count(f.name) > 1}
    if dupes:
        raise genlib.GenError(f"{DIAG_H}: duplicate field name(s): {sorted(dupes)}")
    return renumber(fields)


def readout(fields) -> list:
    """The rows both JTAG readers report: everything but the PAD."""
    return [f for f in fields if f.cls != PAD]


# --------------------------------------------------------------------------- #
# Render: firmware/common/diag.h — the struct, inside the typedef
# --------------------------------------------------------------------------- #
def _render_comment_block(text: str, indent: int) -> list:
    """A standalone ``/* ... */`` block at `indent`, continuation lines +1."""
    lines = [ln.strip() for ln in text.split("\n")]
    if len(lines) == 1:
        return [" " * indent + "/* " + lines[0] + " */"]
    out = [" " * indent + "/* " + lines[0]]
    out += [" " * (indent + 1) + ln for ln in lines[1:-1]]
    out.append(" " * (indent + 1) + lines[-1] + " */")
    return out


def render_struct(fields) -> str:
    lines: list[str] = []
    for f in fields:
        for c in f.pre_comments:
            lines.extend(_render_comment_block(c, 4))
        dim = "[MPS3_DIAG_RESERVED_WORDS]" if f.cls == PAD else ""
        decl = f"    uint32_t {f.name}{dim};"
        if not f.comment:
            lines.append(decl)
            continue
        col = max(_COMMENT_COL, len(decl) + 1)
        cmt = f.comment.split("\n")
        lines.append(decl.ljust(col) + "/* " + cmt[0].strip() +
                     (" */" if len(cmt) == 1 else ""))
        for k, ln in enumerate(cmt[1:]):
            tail = " */" if k == len(cmt) - 2 else ""
            lines.append(" " * (col + 1) + ln.strip() + tail)
    return "\n".join(lines) + "\n"


def render_diag_h(repo: Path, fields) -> str:
    text = genlib.read(repo, DIAG_H)
    return genlib.splice(text, "diag-struct", "c", GENERATOR, render_struct(fields))


# --------------------------------------------------------------------------- #
# Render: host/socket_harness/xsdb.py — DIAG_FIELDS
# --------------------------------------------------------------------------- #
def render_xsdb_body(fields) -> str:
    rows = readout(fields)
    out = [
        "#: ``(word_offset, field_name)`` for EVERY word the mailbox defines"
        " except the",
        "#: ``reserved[]`` pad — the same list ``scripts/mps3_diag.tcl``'s"
        " ``FIELDS`` gets,",
        "#: from the same source (``MPS3_DIAG_FIELDS`` in"
        " ``firmware/common/diag.h``).",
        "#: Word 0 (``magic``) is ALSO decoded separately by"
        " :meth:`DiagMailbox.read`.",
        "DIAG_FIELDS: tuple[tuple[int, str], ...] = (",
    ]
    for f in rows:
        out.append(f'    ({f.word_offset}, "{f.name}"),')
    out.append(")")
    return "\n".join(out) + "\n"


def render_xsdb(repo: Path, fields) -> str:
    text = genlib.read(repo, XSDB_PY)
    return genlib.splice(text, "diag-fields", "py", GENERATOR,
                         render_xsdb_body(fields))


# --------------------------------------------------------------------------- #
# Render: scripts/mps3_diag.tcl — FIELDS
# --------------------------------------------------------------------------- #
def render_tcl_body(fields) -> str:
    out = ["set FIELDS {"]
    for f in readout(fields):
        out.append(f"    {f.word_offset:<3d}{f.name}")
    out.append("}")
    return "\n".join(out) + "\n"


def render_tcl(repo: Path, fields) -> str:
    text = genlib.read(repo, DIAG_TCL)
    return genlib.splice(text, "diag-fields", "tcl", GENERATOR,
                         render_tcl_body(fields))


# --------------------------------------------------------------------------- #
# Generator entry points
# --------------------------------------------------------------------------- #
def render_outputs(repo: Path, fields) -> dict:
    return {
        DIAG_H: render_diag_h(repo, fields),
        XSDB_PY: render_xsdb(repo, fields),
        DIAG_TCL: render_tcl(repo, fields),
    }


def build(repo: Path) -> dict:
    fields = parse_fields(genlib.read(repo, DIAG_H))
    return render_outputs(repo, fields)


if __name__ == "__main__":
    sys.exit(genlib.run(build, GENERATOR))
