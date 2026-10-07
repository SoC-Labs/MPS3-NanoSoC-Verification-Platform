#!/usr/bin/env python3
"""The comparison engine: sample-domain diff, report writer, and nCompare cross-check.

Stream C (compare pipeline).  Contract: INTERFACES.md §4 (frozen exit codes and
mandatory report contents), plan §5 (comparison design decisions).

WHY THE ENGINE IS OURS AND nCompare IS THE CROSS-CHECK
-----------------------------------------------------
``nCompare``'s batch interface *was* pinned down (see ncompare/README.md -- it is
``-rule <f>.ncr -report <f>.nce``, rule files are Tcl using ``cmp*`` commands),
and it is invoked here for real.  It is nonetheless the *second* opinion, not the
primary comparator, for three reasons that are properties of the task rather
than of the tool:

1. INTERFACES.md §4 mandates a report stating the **index of the first diverging
   sample** and the **count of X-masked samples**.  nCompare is time-based: its
   ``.nce`` reports mismatch *times*, and it never counts don't-cares at all.
   Neither number is obtainable from it.
2. Plan §5.1 mandates trigger-relative alignment.  A real Identify capture has
   its own unrelated time base, so alignment is a sample-index operation that
   this harness must perform regardless.
3. INTERFACES.md §4 requires the comparator to be trustworthy, and the negative
   control has to be runnable.  A pure-Python core over plain data is unit
   testable with no Verdi and no licence; an opaque vendor invocation is not.

So: this module compares, and then -- when ``nCompare`` is available -- re-emits
both sides as sample-indexed FSDBs and asks nCompare for an independent verdict
on the same data.  **If the two verdicts disagree, that is exit 2**, not a
match: a disagreement means we do not know the answer.  What the cross-check
does and does not cover is spelled out in ncompare/README.md.

X/Z RULE (INTERFACES.md §4, plan §5.2) -- one way only
------------------------------------------------------
Golden is the simulation, secondary is the hardware.

* sim bit is x or z  -> don't care, whatever the hardware says (masked, counted)
* hardware bit is x or z -> **MISMATCH**.  Hardware cannot produce X; if it
  appears, the capture or the loader is broken and must not read as a pass.
* otherwise -> mismatch iff the bits differ

The same asymmetry is expressed to nCompare as
``cmpSetStateMap -asym (x,0,T) (x,1,T) (0,x,F) (1,x,F) ...``, which was verified
against the tool: sim-X vs hw-1 is silent, sim-0 vs hw-X is reported.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_PARENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fsdb_tools  # noqa: E402
import provenance  # noqa: E402
from crop_trace import (  # noqa: E402
    SAMPLE_CLK,
    TRIGGER_MARKER,
    SampleTrace,
    SignalMapRow,
    crop_sim_window,
    data_signal_names,
    load_hw_trace,
    load_sim_trace,
    read_manifest_iice,
    read_signal_map,
    render_value,
)
from crop_trace import IDENTIFY_INJECTED  # noqa: E402
from crop_trace import hw_leaf_map as ct_hw_leaf_map  # noqa: E402
from crop_trace import resolve_sim_scope as ct_resolve_sim_scope  # noqa: E402
from fsdb_tools import (  # noqa: E402
    EXIT_HARNESS,
    EXIT_MATCH,
    EXIT_MISMATCH,
    HarnessError,
)

#: Signals present in both traces but excluded from the value diff by
#: construction.  See crop_trace.data_signal_names for the reasoning.
STRUCTURAL_SIGNALS = (SAMPLE_CLK, TRIGGER_MARKER)

MAX_RECORDS_DEFAULT = 200


# --------------------------------------------------------------------------
# Pure comparator core -- plain data in, plain data out
# --------------------------------------------------------------------------

class Mismatch(object):
    __slots__ = ("signal", "sample", "golden", "secondary", "bad_bits", "hw_x")

    def __init__(self, signal, sample, golden, secondary, bad_bits, hw_x):
        self.signal = signal
        self.sample = sample
        self.golden = golden
        self.secondary = secondary
        self.bad_bits = bad_bits  # bit indices, 0 == LSB
        self.hw_x = hw_x          # True if caused by X/Z on the hardware side

    def __repr__(self) -> str:  # pragma: no cover
        return "Mismatch(%s[%d] %s vs %s)" % (
            self.signal, self.sample, self.golden, self.secondary)


class CompareResult(object):
    """Everything INTERFACES.md §4 requires the report to state, as data."""

    def __init__(self):
        self.signals: List[str] = []
        self.n_samples: int = 0
        self.sample_comparisons: int = 0
        self.bit_comparisons: int = 0
        self.masked_bits: int = 0
        self.mismatch_bits: int = 0
        self.mismatch_samples: int = 0
        self.masked_samples: int = 0
        self.fully_masked_samples: int = 0
        self.hw_x_samples: int = 0
        self.per_signal_mismatch: Dict[str, int] = {}
        self.per_signal_masked: Dict[str, int] = {}
        self.first_diverging_sample: Optional[int] = None
        self.first_diverging_signals: List[str] = []
        self.records: List[Mismatch] = []
        self.records_truncated: bool = False

    @property
    def matched(self) -> bool:
        return self.mismatch_samples == 0

    @property
    def exit_code(self) -> int:
        """FROZEN mapping: 0 match / 1 mismatch.  Harness errors raise instead."""
        return EXIT_MATCH if self.matched else EXIT_MISMATCH

    @property
    def masked_fraction(self) -> float:
        if not self.sample_comparisons:
            return 0.0
        return float(self.masked_samples) / float(self.sample_comparisons)


def compare_bits(golden: str, secondary: str) -> Tuple[List[int], List[int], bool]:
    """Per-bit comparison of one sample.  Returns ``(bad_bits, masked_bits, hw_x)``.

    Bit indices are LSB-first (bit 0 is the rightmost character), matching how a
    ``--perturb sig:sample:bit`` argument reads.
    """
    if len(golden) != len(secondary):
        raise HarnessError(
            "width mismatch in compare_bits: %d vs %d (%r vs %r)"
            % (len(golden), len(secondary), golden, secondary)
        )
    width = len(golden)
    bad: List[int] = []
    masked: List[int] = []
    hw_x = False
    for i in range(width):
        bit = width - 1 - i  # LSB-first index
        g = golden[i]
        s = secondary[i]
        if g in "xz":
            # ONE-WAY don't-care: the simulation is allowed to be unknown.
            masked.append(bit)
            continue
        if s in "xz":
            # Hardware can never produce X.  Never silently tolerated.
            bad.append(bit)
            hw_x = True
            continue
        if g != s:
            bad.append(bit)
    return bad, masked, hw_x


def _sample_count_triage(golden: SampleTrace, secondary: SampleTrace) -> str:
    """Name the LIKELY CAUSE of a sample-count mismatch, not just the numbers.

    MEASURED 2026-07-30 on silicon: a depth-1024 IICE returned **923** samples,
    with the debugger itself printing ``Warning: Range 0 1023 exceeds maximum
    922``. So "hardware is short" is a routine hardware fact, not a harness bug,
    and the report has to say which of the two it is or the reader guesses.
    """
    hw = secondary.meta or {}
    sim = golden.meta or {}
    lines = []
    src = hw.get("source")
    if src:
        lines.append("  hardware grid source: %s" % src)
    if hw.get("grid_source"):
        lines.append("  hardware grid established by: %s" % hw["grid_source"])
    if secondary.n_samples < golden.n_samples:
        lines.append(
            "  The HARDWARE side is SHORTER. This is usually the capture itself,\n"
            "  not the harness: a `write fsdb -range {0 N-1}` can exceed what the\n"
            "  buffer actually holds, and the debugger says so as\n"
            "    'Warning: Range 0 <N-1> exceeds maximum <M>'\n"
            "  Check the capture log for that line. If it is there, the manifest\n"
            "  depth (%d) is larger than the real capture (%d): either lower the\n"
            "  manifest depth to match, or re-capture a full buffer. Do NOT simply\n"
            "  truncate the simulation side -- with trigger_time middle/late the\n"
            "  window is anchored at the trigger, so dropping the tail MISALIGNS\n"
            "  the two traces and the comparison silently compares wrong samples."
            % (golden.n_samples, secondary.n_samples)
        )
    else:
        lines.append(
            "  The SIMULATION side is shorter -- that is a crop/manifest problem\n"
            "  on our side, since the sim window length is ours to choose."
        )
    if sim.get("source"):
        lines.append("  sim grid source: %s" % sim["source"])
    return "\n".join(lines)


def compare_traces(
    golden: SampleTrace,
    secondary: SampleTrace,
    names: Optional[Sequence[str]] = None,
    max_records: int = MAX_RECORDS_DEFAULT,
) -> CompareResult:
    """Compare two sample-domain traces.  *golden* is sim, *secondary* is hardware.

    Raises :class:`HarnessError` (-> exit 2) on any structural problem: differing
    sample counts, differing widths, or a requested signal missing from either
    side.  It never "skips" a signal.
    """
    want = sorted(names) if names is not None else sorted(set(golden.values) & set(secondary.values))
    if not want:
        raise HarnessError("no signals to compare")
    for side, trace in (("sim/golden", golden), ("hw/secondary", secondary)):
        missing = [n for n in want if n not in trace.values]
        if missing:
            raise HarnessError("signals missing from the %s trace: %s" % (side, missing))
    if golden.n_samples != secondary.n_samples:
        raise HarnessError(
            "sample-count mismatch: sim window has %d samples, hardware trace has "
            "%d.  Both must be exactly `depth` samples (INTERFACES.md §0).\n"
            "%s" % (golden.n_samples, secondary.n_samples,
                    _sample_count_triage(golden, secondary))
        )
    bad_widths = {
        n: (golden.widths[n], secondary.widths[n])
        for n in want
        if golden.widths[n] != secondary.widths[n]
    }
    if bad_widths:
        raise HarnessError("width mismatch between the two traces: %r" % bad_widths)

    res = CompareResult()
    res.signals = list(want)
    res.n_samples = golden.n_samples
    res.per_signal_mismatch = {n: 0 for n in want}
    res.per_signal_masked = {n: 0 for n in want}

    for i in range(golden.n_samples):
        diverged_here: List[str] = []
        for n in want:
            g = golden.values[n][i]
            s = secondary.values[n][i]
            bad, masked, hw_x = compare_bits(g, s)
            res.sample_comparisons += 1
            res.bit_comparisons += len(g)
            res.masked_bits += len(masked)
            if masked:
                res.masked_samples += 1
                res.per_signal_masked[n] += 1
                if len(masked) == len(g):
                    res.fully_masked_samples += 1
            if bad:
                res.mismatch_bits += len(bad)
                res.mismatch_samples += 1
                res.per_signal_mismatch[n] += 1
                if hw_x:
                    res.hw_x_samples += 1
                diverged_here.append(n)
                if len(res.records) < max_records:
                    res.records.append(Mismatch(n, i, g, s, bad, hw_x))
                else:
                    res.records_truncated = True
        if diverged_here and res.first_diverging_sample is None:
            res.first_diverging_sample = i
            res.first_diverging_signals = diverged_here
    return res


# --------------------------------------------------------------------------
# Set-equality precheck (INTERFACES.md §4)
# --------------------------------------------------------------------------

def check_signal_sets(
    sim_signals: Set[str],
    hw_signals: Set[str],
    rows: Sequence[SignalMapRow],
    hw_optional: Sequence[str] = (),
    sim_scope: Optional[str] = None,
) -> Dict[str, object]:
    """Assert both FSDBs carry exactly the signal set ``signal_map.tsv`` describes.

    Sim side: strict set equality against ``iice/<name>`` for every row -- the
    flat scope of INTERFACES.md §1, including both reserved signals.

    Hardware side: strict set equality against the ``hw_path`` of every row that
    is not ``__DERIVED__`` and not listed in *hw_optional*.  ``hw_optional``
    exists because a real Identify capture contains only the sampled design
    signals: it has no ``trigger_marker`` (already ``__DERIVED__``) and no
    ``sample_clk`` (the sample clock defines the sample domain, it is not a
    sample).  Any *data* signal missing is exit 2, never a skipped row.
    """
    canon = fsdb_tools.canon_path
    sim_signals = {canon(s) for s in sim_signals}
    hw_signals = {canon(s) for s in hw_signals}

    # Which scope the sim mirrors live in.  Per INTERFACES.md §1 the scope is
    # identified by its leaf set, not its name.  resolve_sim_scope() raises
    # (-> exit 2) on a partial or ambiguous match, which IS the §4 set-equality
    # check for the sim side.
    note = None
    if sim_scope is None:
        sim_scope, note = ct_resolve_sim_scope(sim_signals, rows)
    expect_sim = {"%s/%s" % (sim_scope, r.name) for r in rows}
    extra_sim = sorted(sim_signals - expect_sim)
    missing_sim = sorted(expect_sim - sim_signals)
    if missing_sim or extra_sim:
        raise HarnessError(
            "sim FSDB signal set does not match signal_map.tsv.\n"
            "  missing: %s\n  unexpected: %s\n"
            "  INTERFACES.md §1: the sim trace must be a flat scope with exactly "
            "the manifest signals." % (missing_sim, extra_sim)
        )

    # --- hardware side: matched on LEAF names -----------------------------
    # A real Identify capture flattens the design hierarchy away: a probe on a
    # deep path is declared simply as `blink_counter`, inside a single scope
    # named after the instrumented module.  So the manifest's hw_path is reduced
    # to its leaf for matching.
    leafmap = ct_hw_leaf_map(rows)
    observed: Dict[str, List[str]] = {}
    for path in hw_signals:
        observed.setdefault(path.split("/")[-1], []).append(path)
    clash = {leaf: sorted(p) for leaf, p in observed.items() if len(p) > 1}
    if clash:
        raise HarnessError(
            "hardware FSDB has several signals sharing a leaf name, so they "
            "cannot be told apart: %r" % clash
        )
    seen = set(observed)

    # "optional" means may-be-absent, NOT must-be-absent: a real capture does
    # contain the sample clock, so requiring its absence would reject it.
    optional_names = set(hw_optional)
    required = {l for l, n in leafmap.items() if n not in optional_names}
    allowed = set(leafmap) | set(IDENTIFY_INJECTED)
    missing_hw = sorted(required - seen)
    extra_hw = sorted(seen - allowed)
    if missing_hw or extra_hw:
        raise HarnessError(
            "hardware FSDB signal set does not match signal_map.tsv "
            "(matched on leaf names).\n"
            "  missing:    %s\n  unexpected: %s\n"
            "  hw-optional (may be absent): %s\n"
            "  Identify-injected (whitelisted): %s\n"
            "  An unrecognised extra signal is exit 2 by design -- the check is "
            "not widened to ignore anything unexpected."
            % (missing_hw, extra_hw, sorted(optional_names),
               list(IDENTIFY_INJECTED))
        )
    injected_seen = sorted(seen & set(IDENTIFY_INJECTED))
    optional_present = sorted(
        leafmap[l] for l in seen & set(leafmap) if leafmap[l] in optional_names
    )
    return {
        "sim_signals": len(sim_signals),
        "hw_signals": len(hw_signals),
        "hw_optional": list(hw_optional),
        "sim_scope": sim_scope,
        "sim_scope_note": note,
        "identify_injected_seen": injected_seen,
        "hw_optional_present": optional_present,
    }


# --------------------------------------------------------------------------
# nCompare rule-file generation + .nce parsing  (the cross-check)
# --------------------------------------------------------------------------

def _dotted(hw_path: str) -> str:
    return hw_path.strip("/").replace("/", ".")


def _ranged(base: str, width: int) -> str:
    return base if width == 1 else "%s[%d:0]" % (base, width - 1)


def build_ncr(
    golden_fsdb: str,
    secondary_fsdb: str,
    rows: Sequence[SignalMapRow],
    names: Sequence[str],
    out_path: str,
) -> str:
    """Write an nCompare rule file pairing ``iice.<name>`` against the HW path.

    This is where the hardware-side rename actually lives for the vendor tool:
    ``fsdbmangle`` cannot apply a rename map (it is an obfuscator), so the map
    from ``signal_map.tsv`` becomes explicit ``cmpSetSignalPair`` pairs.  The
    generators stay tool-neutral, exactly as INTERFACES.md §3.3 intends.
    """
    by_name = {r.name: r for r in rows}
    lines = [
        "# GENERATED by ncompare/trace_compare.py -- do not edit.",
        "# nCompare batch rule file (Tcl).  Interface confirmed against",
        "# verdi/X-2025.06-SP2; see ncompare/README.md for the evidence.",
        "cmpOpenFsdb %s %s" % (golden_fsdb, secondary_fsdb),
        "cmpSetDelimiter .",
        "# 0 == unlimited, so the reported totals are the real totals.",
        "cmpSetCmpOption -maxerror 0 -maxerrorpersignal 0 -x T -z T",
        "# ONE-WAY don't-care: sim(golden) X/Z vs hardware(secondary) 0/1 is a",
        "# match; hardware X/Z against a known sim value is a MISMATCH.",
        "cmpSetStateMap -asym (x,0,T) (x,1,T) (x,z,T) (0,x,F) (1,x,F) \\",
        "                     (z,0,T) (z,1,T) (z,x,T) (0,z,F) (1,z,F)",
    ]
    for n in names:
        r = by_name.get(n)
        if r is None:
            raise HarnessError("signal %r is not in signal_map.tsv" % n)
        if r.hw_derived:
            raise HarnessError(
                "cannot pair %r for nCompare: its hw_path is __DERIVED__" % n
            )
        lines.append(
            "cmpSetSignalPair {%s} {%s}"
            % (_ranged("iice.%s" % n, r.width), _ranged(_dotted(r.hw_path), r.width))
        )
    lines.append("cmpCompare")
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return out_path


_NCE_500 = re.compile(r"^500\s+(.*)$")
_NCE_100 = re.compile(r"^100\s+(.*)$")


def parse_nce(path: str) -> Dict[str, object]:
    """Parse an nCompare ``.nce`` report into ``{compared, mismatched_signals, errors}``.

    Record layouts are from the Verdi docs (Appendix D, "Error File"):
      ``100 [rule file] [rule errors] [rule warnings]``
      ``500 [golden] [secondary] [G:scale] [S:scale] [compared] [mismatched] [errors]``
      ``510``/``511``-``513`` one line per mismatch.
    A rule-file error is a harness error: it means nCompare did not compare what
    we asked it to.
    """
    if not os.path.isfile(path):
        raise HarnessError("nCompare report not found: %s" % path)
    info: Dict[str, object] = {
        "compared": None,
        "mismatched_signals": None,
        "errors": None,
        "rule_errors": 0,
        "rule_warnings": 0,
        "mismatch_lines": 0,
        "messages": [],
    }
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            m = _NCE_100.match(line)
            if m:
                tail = m.group(1).split()
                if len(tail) >= 2:
                    try:
                        info["rule_errors"] = int(tail[-2])
                        info["rule_warnings"] = int(tail[-1])
                    except ValueError:
                        pass
                continue
            m = _NCE_500.match(line)
            if m:
                tail = m.group(1).split()
                try:
                    info["compared"] = int(tail[-3])
                    info["mismatched_signals"] = int(tail[-2])
                    info["errors"] = int(tail[-1])
                except (ValueError, IndexError):
                    raise HarnessError(
                        "could not parse the nCompare summary record: %r" % line
                    )
                continue
            if line[:3] in ("510", "511", "512", "513"):
                info["mismatch_lines"] = int(info["mismatch_lines"]) + 1  # type: ignore[arg-type]
            elif line[:3] in ("101", "201"):
                info["messages"].append(line)  # type: ignore[union-attr]
    if info["compared"] is None:
        raise HarnessError(
            "nCompare report %s has no '500' summary record; the comparison did "
            "not run" % path
        )
    if info["rule_errors"]:
        raise HarnessError(
            "nCompare reported %s rule-file error(s):\n  %s"
            % (info["rule_errors"], "\n  ".join(info["messages"]) or "(no detail)")
        )
    if int(info["compared"]) == 0:
        raise HarnessError(
            "nCompare compared 0 signal pairs -- the rule file matched nothing, "
            "so its verdict is worthless.  (report: %s)" % path
        )
    return info


def _write_side_fsdb(
    trace: SampleTrace,
    path_of: Dict[str, str],
    fsdb_out: str,
    logdir: str,
    tag: str,
) -> str:
    """Re-emit a sample trace as a sample-indexed FSDB at the given signal paths."""
    signals = [(path_of[n], trace.widths[n]) for n in sorted(path_of)]
    frames = [
        {path_of[n]: trace.values[n][i] for n in sorted(path_of)}
        for i in range(trace.n_samples)
    ]
    vcd = os.path.join(logdir, "%s.vcd" % tag)
    fsdb_tools.write_vcd(vcd, signals, frames, version="identify_iice %s" % tag)
    return fsdb_tools.vcd2fsdb(vcd, fsdb_out, logdir)


def ncompare_crosscheck(
    golden: SampleTrace,
    secondary: SampleTrace,
    rows: Sequence[SignalMapRow],
    names: Sequence[str],
    logdir: str,
) -> Dict[str, object]:
    """Ask nCompare for an independent verdict on the same sample data.

    Returns a dict with ``available``/``skipped_reason`` or the parsed report.
    Raises :class:`HarnessError` only if nCompare was present but could not be
    made to run a meaningful comparison.
    """
    if not fsdb_tools.have_tool("nCompare") or not fsdb_tools.have_tool("vcd2fsdb"):
        return {"available": False, "skipped_reason": "nCompare/vcd2fsdb not on PATH"}
    by_name = {r.name: r for r in rows}
    os.makedirs(logdir, exist_ok=True)
    g_paths = {n: "iice/%s" % n for n in names}
    s_paths = {n: by_name[n].hw_path for n in names}
    g_fsdb = _write_side_fsdb(golden, g_paths, os.path.join(logdir, "ncmp_golden.fsdb"), logdir, "ncmp_golden")
    s_fsdb = _write_side_fsdb(secondary, s_paths, os.path.join(logdir, "ncmp_secondary.fsdb"), logdir, "ncmp_secondary")
    ncr = build_ncr(g_fsdb, s_fsdb, rows, names, os.path.join(logdir, "iice_compare.ncr"))
    nce = fsdb_tools.ncompare(ncr, os.path.join(logdir, "iice_compare.nce"), logdir)
    info = parse_nce(nce)
    info["available"] = True
    info["rule_file"] = ncr
    info["report_file"] = nce
    info["golden_fsdb"] = g_fsdb
    info["secondary_fsdb"] = s_fsdb
    return info


# --------------------------------------------------------------------------
# Report (INTERFACES.md §4: the required contents)
# --------------------------------------------------------------------------

def _wrap(text: str, indent: str = "    ", width: int = 74) -> str:
    """Soft-wrap a diagnostic line so the report stays readable in a terminal."""
    import textwrap

    return "\n".join(
        textwrap.wrap(" ".join(text.split()), width=width,
                      initial_indent=indent, subsequent_indent=indent)
    )


#: How the sample grid was established, in words, for the report.  A grid is
#: never "guessed" -- see crop_trace.sample_grid.
_GRID_SOURCE_TEXT = {
    "timestamps": "one timestamp per sample, straight from the FSDB (no inference)",
    "gcd_span": "period from the gap GCD; the LAST VALUE CHANGE is the last sample",
    "gcd_pinned_by_end_time":
        "period from the gap GCD; the span PROVED by the FSDB's own end time",
    "single_timestamp_pinned_by_end_time":
        "one value-change timestamp only; period pinned by the FSDB's own end time",
    "single_timestamp":
        "one value-change timestamp only; the period is not recoverable from it "
        "and does not affect any compared value",
}


def describe_hw_stall(hwm: Dict[str, object]) -> Optional[Dict[str, object]]:
    """Name a quiet hardware tail as a finding, or return ``None``.

    A hardware trace whose tail carries no value change is exactly the shape a
    wedged DUT produces, and it is the case the harness exists to look at.  It
    used to be a harness error (exit 2, "cannot establish the hardware sample
    grid"), which named the symptom -- fewer timestamps than the manifest depth
    -- rather than the finding.  Now the FSDB's own end time proves the span, the
    tail is held forward, and the verdict comes from the value diff like any
    other trace.  This function turns the loader's metadata into the sentence the
    reader needs.

    Deliberately says what it CANNOT distinguish.  A frozen capture, a stopped
    sample clock and a capture that stopped being written all look identical from
    one trace (EVALUATING.md, "shape classification is ambiguous").
    """
    if hwm.get("source") != "timestamp_ordinals":
        return None
    try:
        tail = int(hwm.get("quiet_tail_samples") or 0)
        n = int(hwm.get("n_samples") or 0)
        last = int(hwm.get("last_change_sample", -1))
    except (TypeError, ValueError):
        return None
    if tail <= 0 or n <= 0:
        return None
    if last < 0:
        headline = (
            "the hardware trace carries NO value change on any signal, at any of "
            "its %d samples." % n
        )
    else:
        headline = (
            "the hardware trace holds its last value from sample %d of %d; the "
            "tail (%d sample%s) carries no value changes, consistent with a "
            "stalled or wedged capture."
            % (last + 1, n, tail, "" if tail == 1 else "s")
        )
    return {"headline": headline, "tail": tail, "n": n, "last_change": last}


def provenance_report_lines(context: Dict[str, object]) -> List[str]:
    """The ``-- provenance --`` block: what bound these artifacts together.

    Every line here is either a verified fact or an explicit statement that
    nothing was verified.  There is no third state: a report that quietly omits
    provenance reads as if provenance had been checked, which is how a mismatched
    pair got believed three times on 2026-07-30.
    """
    identity = context.get("manifest_identity")
    if not isinstance(identity, dict):
        return []
    L: List[str] = ["", "-- provenance (INTERFACES.md §4, 2026-07-31) ------------------------"]
    A = L.append
    A("  manifest identity         %s" % provenance.identity_headline(identity))
    A("    probe set sha256        %s" % identity.get("signal_set_sha256"))
    A("    read by                 %s" % identity.get("read_by"))
    map_info = context.get("signal_map_verified")
    if isinstance(map_info, dict):
        A("  signal_map.tsv            VERIFIED row-for-row against this manifest")
        A("                            (%s rows)" % map_info.get("rows"))
    log = context.get("identify_log_result")
    if isinstance(log, dict):
        status = str(log.get("status"))
        if status == "ok":
            facts = log.get("facts") or {}
            A("  built instrumentation     MATCHES the manifest")
            A("    identify.log            %s" % log.get("log"))
            A("    built                   IICE %s, depth %s, width %s bits, "
              "%s signal(s)" % (facts.get("iice_name"), facts.get("depth"),
                                facts.get("width_bits"), facts.get("n_signals")))
        elif status == "absent":
            A("  built instrumentation     NOT CHECKED -- no Identify log at")
            A("                            %s" % provenance.default_identify_log())
            A("                            (`build/` is gitignored; a fresh clone")
            A("                            has none. Nothing is asserted about")
            A("                            what is in the bitstream.)")
        elif status == "not_applicable":
            A("  built instrumentation     NOT APPLICABLE")
            A(_wrap(str(log.get("note")), "                            "))
    for label, key in (("hardware", "hw_provenance"), ("sim", "sim_provenance")):
        L.extend(provenance.stamp_report_lines(
            context.get(key) if isinstance(context.get(key), dict) else None,
            label=label))
    expect = context.get("provenance_expectations")
    if isinstance(expect, dict) and expect:
        A("  caller expectations ENFORCED:")
        for key in sorted(expect):
            A("    %-25s %s" % (key, expect[key]))
    else:
        A("  caller expectations       (none given -- set IICE_EXPECT_RM_ID /")
        A("                            IICE_EXPECT_STATIC_ID to have the stamp's")
        A("                            ids enforced too; scripts/mps3_state.sh")
        A("                            --quiet prints RM_ID=)")
    return L


def format_report(
    res: CompareResult,
    rows: Sequence[SignalMapRow],
    context: Dict[str, object],
    crosscheck: Optional[Dict[str, object]] = None,
    max_shown: int = 20,
) -> str:
    by_name = {r.name: r for r in rows}
    L: List[str] = []
    A = L.append
    A("=" * 78)
    A("IICE sim-vs-hardware trace comparison report")
    A("=" * 78)
    A("")
    A("VERDICT: %s   (exit code %d)" % (
        "MATCH" if res.matched else "MISMATCH", res.exit_code))
    stall = describe_hw_stall(context.get("hw_trace_meta") or {})
    if stall:
        A("")
        A("FINDING: %s" % stall["headline"])
    A("")
    A("-- inputs ------------------------------------------------------------")
    for key in ("manifest", "iice_name", "depth", "trigger_time", "clock_edge",
                "signal_map", "sim_fsdb", "hw_fsdb", "sim_scope"):
        if key in context:
            A("  %-22s %s" % (key, context[key]))
    if context.get("sim_scope_note"):
        A("")
        A("  scope resolution (INTERFACES.md §1 -- by leaf set, not by name):")
        A(_wrap(str(context["sim_scope_note"]), "    "))
    L.extend(provenance_report_lines(context))
    hwm = context.get("hw_trace_meta") or {}
    if hwm.get("source") == "timestamp_ordinals":
        A("")
        A("  hardware trace time base (sample index == timestamp ORDINAL):")
        A("    %d samples over t=%s..%s, sample period %s" % (
            hwm.get("n_samples"), hwm.get("first_time"),
            hwm.get("last_time"), hwm.get("sample_period")))
        A("    An IICE trace is trigger-relative, so absolute time is arbitrary")
        A("    and is never used for alignment.")
        A("    value-change timestamps in the FSDB   %s of %s" % (
            hwm.get("n_timestamps"), hwm.get("n_samples")))
        A("    FSDB end time (fsdb2vcd -summary)      %s" % hwm.get("fsdb_end_time"))
        A("    sample grid established by            %s" % _GRID_SOURCE_TEXT.get(
            str(hwm.get("grid_source")), hwm.get("grid_source")))
    if stall:
        A("")
        A("-- hardware trace: QUIET TAIL ----------------------------------------")
        A(_wrap(stall["headline"].capitalize(), "  "))
        A("")
        if stall["last_change"] >= 0:
            A("  last sample carrying a value change    %d" % stall["last_change"])
        else:
            A("  last sample carrying a value change    (none)")
        A("  samples held forward to the end        %d of %d"
          % (stall["tail"], stall["n"]))
        A("")
        A(_wrap(
            "This is NOT padding and NOT a guess: fsdb2vcd stops the VCD at the "
            "last transition, so those timestamps are absent from the VCD, but "
            "the FSDB's own end time (%s) proves the file really spans %d samples "
            "of period %s. A span the end time could not prove is still a harness "
            "error (exit 2) -- see crop_trace.sample_grid."
            % (hwm.get("fsdb_end_time"), hwm.get("n_samples"),
               hwm.get("sample_period")), "  "))
        A("")
        A("  Consistent with, and NOT distinguishable from one another here:")
        A("    * the DUT stalled or wedged (the shape this harness exists for)")
        A("    * the sample clock stopped, so the IICE stored the same sample")
        A("    * the capture stopped being written part-way through")
        A(_wrap(
            "A genuine Identify `write fsdb` should not produce this shape: "
            "identify_sampleclock was MEASURED to change at every one of the "
            "1024 sample timestamps of the real capture, so a real capture "
            "always yields depth timestamps. A quiet tail therefore points at a "
            "synthetic or re-emitted FSDB, or at a capture missing its sample "
            "clock -- check where this file came from before reading it as a "
            "DUT stall.", "  "))
    if context.get("identify_injected_seen"):
        A("")
        A("  Identify-injected signals present in the capture (whitelisted, not")
        A("  compared -- they are not manifest signals):")
        for s in context["identify_injected_seen"]:
            extra = {
                "identify_sampleclock":
                    "aliased onto the same VCD identifier as the probed clock",
                "identify_cycle":
                    "declared but carries no value changes at all",
            }.get(s, "")
            A("    %-24s %s" % (s, extra))
        A("    An UNRECOGNISED extra signal is still exit 2.")
    if context.get("hw_optional_present"):
        A("")
        A("  optional signals that the capture did contain: %s"
          % ", ".join(context["hw_optional_present"]))
    A("")
    A("-- window alignment --------------------------------------------------")
    for key, label in (
        ("trace_samples", "sim samples recorded"),
        ("trigger_sample", "trigger condition at sim sample"),
        ("marker_first_high_sample", "trigger_marker first high at"),
        ("marker_high_samples_in_trace", "trigger_marker high samples (trace)"),
        ("marker_high_samples_in_window", "trigger_marker high samples (window)"),
        ("window_start_sample", "window start (absolute sim sample)"),
        ("window_end_sample", "window end   (absolute sim sample)"),
        ("trigger_window_index", "trigger at window index"),
    ):
        if key in context:
            A("  %-38s %s" % (label, context[key]))
    A("  (trigger_marker high at N means the condition held at N-1 --")
    A("   INTERFACES.md §3.2, one-sample offset owned by crop_trace.py)")
    if int(context.get("marker_high_samples_in_trace", 1) or 1) > 1:
        A("  NOTE: the trigger condition held on more than one sample; the FIRST")
        A("        one positions the window, as a real IICE would.")
    A("")
    A("-- totals ------------------------------------------------------------")
    A("  signals compared                       %d" % len(res.signals))
    A("  samples compared (per signal)          %d" % res.n_samples)
    A("  total samples compared                 %d   (%d signals x %d samples)"
      % (res.sample_comparisons, len(res.signals), res.n_samples))
    A("  total bits compared                    %d" % res.bit_comparisons)
    A("  MISMATCH COUNT (signal-samples)        %d" % res.mismatch_samples)
    A("  mismatching bits                       %d" % res.mismatch_bits)
    A("  X-masked samples (sim X vs hw 0/1)     %d   (%.2f%% of samples compared)"
      % (res.masked_samples, 100.0 * res.masked_fraction))
    A("  ... of which fully masked              %d" % res.fully_masked_samples)
    A("  X-masked bits                          %d" % res.masked_bits)
    A("  mismatches caused by hardware X/Z      %d   (hardware must never emit X)"
      % res.hw_x_samples)
    A("  FIRST DIVERGING SAMPLE                 %s"
      % ("(none)" if res.first_diverging_sample is None else str(res.first_diverging_sample)))
    if res.first_diverging_sample is not None:
        A("    diverging signal(s) there            %s" % ", ".join(res.first_diverging_signals))
        trig_idx = context.get("trigger_window_index")
        if isinstance(trig_idx, int):
            A("    relative to trigger                  %+d samples"
              % (res.first_diverging_sample - trig_idx))
    if res.masked_fraction >= 0.5:
        A("")
        A("  *** WARNING: %.1f%% of all compared samples were X-masked. This run" % (100.0 * res.masked_fraction))
        A("      proves very little -- a mostly-masked comparison is close to")
        A("      worthless (plan §5.2). Trigger later, or after reset.")
    A("")
    A("-- per-signal --------------------------------------------------------")
    A("  %-24s %6s %8s %8s %8s" % ("signal", "width", "radix", "mismatch", "masked"))
    for n in res.signals:
        r = by_name.get(n)
        A("  %-24s %6d %8s %8d %8d" % (
            n,
            r.width if r else -1,
            r.radix if r else "?",
            res.per_signal_mismatch.get(n, 0),
            res.per_signal_masked.get(n, 0),
        ))
    excluded = [s for s in STRUCTURAL_SIGNALS if s in {r.name for r in rows}]
    if excluded:
        A("")
        A("  excluded from the value diff by construction: %s" % ", ".join(excluded))
        A("    sample_clk is 1 at every sample once sampled (vacuous);")
        A("    trigger_marker is harness metadata, __DERIVED__ on the hw side.")
        A("    Both are still presence-checked in the set-equality precheck.")
    A("")
    if res.records:
        A("-- first %d mismatches ------------------------------------------------"
          % min(max_shown, len(res.records)))
        A("  %8s %-20s %-22s %-22s %s" % ("sample", "signal", "sim (golden)", "hw (secondary)", "bad bits"))
        for m in res.records[:max_shown]:
            r = by_name.get(m.signal)
            radix = r.radix if r else "bin"
            A("  %8d %-20s %-22s %-22s %s%s" % (
                m.sample, m.signal,
                render_value(m.golden, radix), render_value(m.secondary, radix),
                ",".join(str(b) for b in m.bad_bits),
                "  <- hardware X/Z" if m.hw_x else "",
            ))
        if len(res.records) > max_shown:
            A("  ... %d more recorded" % (len(res.records) - max_shown))
        if res.records_truncated:
            A("  ... recording capped; totals above are complete.")
        A("")
    A("-- nCompare cross-check ----------------------------------------------")
    if not crosscheck:
        A("  not run")
    elif not crosscheck.get("available"):
        A("  SKIPPED: %s" % crosscheck.get("skipped_reason", "unknown"))
        A("  (the verdict above comes from this harness's own comparator)")
    else:
        A("  rule file      %s" % crosscheck.get("rule_file"))
        A("  report         %s" % crosscheck.get("report_file"))
        A("  signal pairs   %s" % crosscheck.get("compared"))
        A("  mismatching signals %s" % crosscheck.get("mismatched_signals"))
        A("  mismatch records    %s" % crosscheck.get("errors"))
        agree = bool(crosscheck.get("errors")) == (not res.matched)
        A("  verdict        %s" % ("AGREES with this harness" if agree else "DISAGREES"))
        A("  scope: cross-checks the value/X-masking comparison on the same")
        A("         sample data. It does NOT independently check FSDB loading,")
        A("         sample-edge extraction or window cropping -- those are")
        A("         covered by tests/test_compare.py.")
    A("")
    A("=" * 78)
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------

def run_compare(
    sim_fsdb: str,
    hw_fsdb: str,
    signal_map: str,
    manifest: str,
    report_path: str,
    logdir: str,
    hw_optional: Sequence[str] = STRUCTURAL_SIGNALS,
    use_ncompare: bool = True,
    save_windows: bool = True,
    expect: Optional[Dict[str, str]] = None,
    identify_log: Optional[str] = None,
    require_identify_log: bool = False,
) -> Tuple[int, str]:
    """Full compare step.  Returns ``(exit_code, report_text)``.

    Any structural problem raises :class:`HarnessError`; the caller maps that to
    exit 2.  This function never returns a match verdict on an error path.

    PROVENANCE (added 2026-07-31).  Three refusals run BEFORE any value is
    compared, because comparing mismatched artifacts is worse than not comparing
    at all -- it produces a confident wrong answer:

    1. ``signal_map.tsv`` must have been generated from *manifest*.  Nothing
       enforced this before, and on 2026-07-30 a real nanosoc capture was
       normalised against the selftest map because ``build/signal_map.tsv`` is
       written by whichever manifest last ran ``make gen``.
    2. A provenance stamp beside either FSDB must agree with *manifest*.  Absence
       is not a mismatch -- older captures and synthetic phase-0 FSDBs have
       none -- but a conflicting stamp is exit 2.
    3. If an Identify instrumentation log describes this manifest's IICE, the
       manifest must match what was actually BUILT (the 69-vs-113 check).  An
       absent log, or one for a different IICE, is reported and not fatal.
    """
    rows = read_signal_map(signal_map)
    iice = read_manifest_iice(manifest)
    identity = provenance.manifest_identity(manifest)
    # (1) the manifest <-> signal_map binding, for EVERY entry point
    map_info = provenance.check_signal_map(identity, signal_map)
    depth = int(iice["depth"])
    names = data_signal_names(rows)
    if not names:
        raise HarnessError(
            "signal_map.tsv describes no data signals (only the reserved ones); "
            "there is nothing to compare"
        )
    os.makedirs(logdir, exist_ok=True)

    # --- (2) provenance: refuse a pairing, do not compare it --------------
    hw_stamp = provenance.read_stamp(hw_fsdb)
    sim_stamp = provenance.read_stamp(sim_fsdb)
    disc = provenance.check_stamp(hw_stamp, identity, artifact=hw_fsdb,
                                  expect=expect, label="hardware")
    # The sim side is checked with the same machinery even though nothing writes
    # a sim stamp yet (Makefile.sim is stream B's): a stale sim FSDB is the same
    # failure class, and this way the door is shut the moment one appears.
    disc += provenance.check_stamp(sim_stamp, identity, artifact=sim_fsdb,
                                   expect=None, label="sim")
    if disc:
        raise HarnessError(provenance.refuse(disc, hw_fsdb, manifest))

    # --- (3) provenance: the manifest vs what was BUILT -------------------
    log_path = (identify_log or os.environ.get("IICE_IDENTIFY_LOG")
                or provenance.default_identify_log())
    log_result = provenance.check_identify_log(
        identity, provenance.parse_identify_log(log_path),
        require_iice=require_identify_log)
    if log_result["status"] == "mismatch":
        raise HarnessError(provenance.identify_log_error(identity, log_result))
    if require_identify_log and log_result["status"] == "absent":
        raise HarnessError(
            "require_identify_log was set but there is no Identify "
            "instrumentation log at %s, so nothing proves the manifest "
            "describes what is in the bitstream." % log_path)

    # --- INTERFACES.md §4 precheck: set equality BEFORE comparing ---------
    sim_sigs = fsdb_tools.list_signals(sim_fsdb, logdir)
    hw_sigs = fsdb_tools.list_signals(hw_fsdb, logdir)
    sets = check_signal_sets(sim_sigs, hw_sigs, rows, hw_optional=hw_optional)
    sim_scope = str(sets["sim_scope"])

    sim_trace = load_sim_trace(sim_fsdb, rows, logdir, str(iice["edge"]), scope=sim_scope)
    window, info = crop_sim_window(sim_trace, depth, str(iice["trigger_time"]))
    hw_trace = load_hw_trace(hw_fsdb, rows, logdir, depth, optional=hw_optional)

    if save_windows:
        window.save(os.path.join(logdir, "sim_window.json"))
        hw_trace.save(os.path.join(logdir, "hw_window.json"))

    res = compare_traces(window, hw_trace, names)

    crosscheck = None
    if use_ncompare:
        crosscheck = ncompare_crosscheck(window, hw_trace, rows, names, logdir)
        if crosscheck.get("available"):
            nc_mismatch = bool(crosscheck.get("errors"))
            if nc_mismatch != (not res.matched):
                raise HarnessError(
                    "COMPARATOR DISAGREEMENT -- refusing to report a verdict.\n"
                    "  this harness: %s (%d mismatching signal-samples)\n"
                    "  nCompare:     %s (%s mismatch records over %s pairs)\n"
                    "  A disagreement means we do not know the answer, so this is\n"
                    "  a harness error (exit 2), not a match.  See %s"
                    % (
                        "MATCH" if res.matched else "MISMATCH",
                        res.mismatch_samples,
                        "MATCH" if not nc_mismatch else "MISMATCH",
                        crosscheck.get("errors"),
                        crosscheck.get("compared"),
                        crosscheck.get("report_file"),
                    )
                )

    context: Dict[str, object] = {
        "manifest": manifest,
        "iice_name": iice["name"],
        "depth": depth,
        "trigger_time": iice["trigger_time"],
        "clock_edge": iice["edge"],
        "signal_map": signal_map,
        "sim_fsdb": sim_fsdb,
        "hw_fsdb": hw_fsdb,
        "sim_scope": sim_scope,
        "sim_scope_note": sets.get("sim_scope_note"),
        "identify_injected_seen": sets.get("identify_injected_seen"),
        "hw_optional_present": sets.get("hw_optional_present"),
        "hw_trace_meta": hw_trace.meta,
        "manifest_identity": identity,
        "signal_map_verified": map_info,
        "hw_provenance": hw_stamp,
        "sim_provenance": sim_stamp,
        "identify_log_result": log_result,
        "provenance_expectations": dict(expect or {}),
    }
    context.update(info)
    text = format_report(res, rows, context, crosscheck)
    directory = os.path.dirname(os.path.abspath(report_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(report_path, "w") as fh:
        fh.write(text)
    return res.exit_code, text


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="IICE sim-vs-hardware trace comparison")
    ap.add_argument("--sim-fsdb", required=True)
    ap.add_argument("--hw-fsdb", required=True)
    ap.add_argument("--signal-map", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--logdir", required=True)
    ap.add_argument(
        "--hw-optional",
        default=",".join(STRUCTURAL_SIGNALS),
        help="comma-separated names allowed to be absent from the hardware FSDB",
    )
    ap.add_argument(
        "--no-ncompare",
        action="store_true",
        help="skip the nCompare cross-check (our comparator still decides)",
    )
    # --- provenance (INTERFACES.md §4, 2026-07-31) ------------------------
    # Expectations are OPT-IN: what the harness can prove unaided is that the
    # capture was produced against this manifest. Which RM was resident is a
    # runtime fact only the caller has -- `scripts/mps3_state.sh --quiet` prints
    # RM_ID=. Supplying it here turns that fact into an enforced one.
    ap.add_argument("--expect-rm-id", default=None,
                    help="refuse unless the hardware stamp records this RM_ID")
    ap.add_argument("--expect-static-id", default=None,
                    help="refuse unless the hardware stamp records this static_id")
    ap.add_argument("--expect-static-usercode", default=None)
    ap.add_argument("--identify-log", default=None,
                    help="Identify instrumentation log to check the manifest "
                         "against (default: the in-tree rev_1_identify one)")
    ap.add_argument("--require-identify-log", action="store_true",
                    help="an absent log, or one that does not instrument this "
                         "manifest's IICE, is a harness error")
    args = ap.parse_args(argv)
    optional = [s for s in args.hw_optional.split(",") if s]
    try:
        expect = {
            "rm_id": provenance.norm_id(args.expect_rm_id, "--expect-rm-id"),
            "static_id": provenance.norm_id(args.expect_static_id,
                                            "--expect-static-id"),
            "static_usercode": provenance.norm_id(args.expect_static_usercode,
                                                  "--expect-static-usercode"),
        }
        expect = {k: v for k, v in expect.items() if v is not None}
        if not expect:
            expect = provenance.expectations_from_env()
        code, text = run_compare(
            args.sim_fsdb,
            args.hw_fsdb,
            args.signal_map,
            args.manifest,
            args.report,
            args.logdir,
            hw_optional=optional,
            use_ncompare=not args.no_ncompare,
            expect=expect,
            identify_log=args.identify_log,
            require_identify_log=args.require_identify_log,
        )
    except HarnessError as exc:
        msg = "HARNESS ERROR (compare): %s" % exc
        print(msg, file=sys.stderr)
        try:
            directory = os.path.dirname(os.path.abspath(args.report))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.report, "w") as fh:
                fh.write(
                    "VERDICT: HARNESS ERROR   (exit code %d)\n\n%s\n\n"
                    "No comparison was performed.  A harness error is never a "
                    "match (INTERFACES.md §4).\n" % (EXIT_HARNESS, msg)
                )
        except OSError:
            pass
        return EXIT_HARNESS
    sys.stdout.write(text)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
