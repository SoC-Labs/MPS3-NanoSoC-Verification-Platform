"""ila_csv.py -- read the CSV Vivado's ``write_hw_ila_data -csv_file`` writes.

Shape (Vivado 2024.1)::

    Sample in Buffer,Sample in Window,TRIGGER,counter[15:0],tick
    Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,HEX
    0,0,1,00ff,0
    1,1,0,0100,1

The second row (the radix row) is optional here: when it is absent every probe
column is read as HEX, which is Vivado's default radix for a probe, and the
three bookkeeping columns as UNSIGNED. Probe names are matched the way a human
writes them -- exactly, with the bus range stripped (``counter`` for
``counter[15:0]``), or by the last hierarchy segment (``u_rp_dut/counter``).
Stdlib only: this runs on the build host's python3 and on the hub's python3.11.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_RANGE = re.compile(r"\[\d+:\d+\]$")
_BOOKKEEPING = ("Sample in Buffer", "Sample in Window", "TRIGGER")


class IlaCsvError(ValueError):
    """The file is not an ILA CSV, or a named probe is missing/ambiguous."""


def _parse_value(text: str, radix: str) -> int:
    t = text.strip()
    r = radix.strip().upper()
    if t == "":
        raise ValueError("empty cell")
    if r in ("HEX", "HEXADECIMAL"):
        return int(t, 16)
    if r in ("BINARY", "BIN"):
        return int(t, 2)
    if r in ("OCTAL", "OCT"):
        return int(t, 8)
    if r in ("SIGNED",):
        return int(t)
    return int(t, 10)


@dataclass
class IlaCapture:
    names: List[str]
    radices: List[str]
    rows: List[List[str]]
    #: 1-based line number in the file of each row (for error messages).
    lines: List[int]

    def column(self, name: str) -> int:
        """Index of the column a human calls ``name`` (see module docstring)."""
        hits = []
        for i, full in enumerate(self.names):
            bare = _RANGE.sub("", full.strip())
            leaf = bare.split("/")[-1]
            if name in (full.strip(), bare, leaf):
                hits.append(i)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise IlaCsvError(f"no column named {name!r}; columns are {self.names}")
        raise IlaCsvError(
            f"column name {name!r} is ambiguous: {[self.names[i] for i in hits]}")

    def values(self, name: str) -> List[int]:
        col = self.column(name)
        out = []
        for row, line in zip(self.rows, self.lines):
            try:
                out.append(_parse_value(row[col], self.radices[col]))
            except (ValueError, IndexError) as exc:
                raise IlaCsvError(
                    f"line {line}: cannot read {self.names[col]!r} = "
                    f"{row[col] if col < len(row) else '<missing>'!r} "
                    f"as {self.radices[col]}: {exc}") from exc
        return out

    def width(self, name: str) -> Optional[int]:
        """Bus width from the header's ``[hi:lo]``; None for a 1-bit name."""
        m = re.search(r"\[(\d+):(\d+)\]$", self.names[self.column(name)].strip())
        return (int(m.group(1)) - int(m.group(2)) + 1) if m else None


def read_ila_csv(path: "str | Path") -> IlaCapture:
    with open(path, newline="") as fh:
        raw = [(n, r) for n, r in enumerate(csv.reader(fh), start=1) if any(c.strip() for c in r)]
    if not raw:
        raise IlaCsvError(f"{path}: empty file")
    _, header = raw[0]
    names = [h.strip() for h in header]
    if not names or names[0] != "Sample in Buffer":
        raise IlaCsvError(
            f"{path}: first header cell is {names[0] if names else ''!r}, not "
            "'Sample in Buffer' -- not a write_hw_ila_data -csv_file file")
    body = raw[1:]
    if body and body[0][1] and body[0][1][0].strip().lower().startswith("radix"):
        rad = [c.strip() for c in body[0][1]]
        rad[0] = rad[0].split("-", 1)[-1].strip()
        body = body[1:]
    else:
        rad = ["UNSIGNED" if n in _BOOKKEEPING else "HEX" for n in names]
    if len(rad) < len(names):
        rad += ["HEX"] * (len(names) - len(rad))
    return IlaCapture(names=names, radices=rad,
                      rows=[r for _, r in body], lines=[n for n, _ in body])


def write_ila_csv(path: "str | Path", columns: Dict[str, Sequence[int]],
                  radices: Optional[Dict[str, str]] = None,
                  trigger_index: int = 0) -> None:
    """Write a synthetic capture in Vivado's shape (for tests and demos).

    ``columns`` maps a probe header (e.g. ``"counter[15:0]"``) to its samples.
    HEX columns are zero-padded to the header's width.
    """
    radices = radices or {}
    names = list(columns)
    n = len(next(iter(columns.values()))) if columns else 0
    widths = {}
    for name in names:
        m = re.search(r"\[(\d+):(\d+)\]$", name)
        widths[name] = (int(m.group(1)) - int(m.group(2)) + 1) if m else 1
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(list(_BOOKKEEPING) + names)
        w.writerow(["Radix - UNSIGNED", "UNSIGNED", "UNSIGNED"]
                   + [radices.get(nm, "HEX") for nm in names])
        for i in range(n):
            cells = [str(i), str(i - trigger_index), "1" if i == trigger_index else "0"]
            for nm in names:
                v = columns[nm][i]
                r = radices.get(nm, "HEX")
                if r == "HEX":
                    cells.append(format(v, "0%dx" % ((widths[nm] + 3) // 4)))
                elif r == "BINARY":
                    cells.append(format(v, "0%db" % widths[nm]))
                else:
                    cells.append(str(v))
            w.writerow(cells)
