#!/usr/bin/env python3
"""Crop a continuous simulation FSDB down to an IICE-shaped, trigger-relative window.

Stream C (compare pipeline).  Contract: INTERFACES.md §0, §3.2, §3.3, §4.

WHY THIS EXISTS
---------------
A hardware IICE stores exactly ``depth`` samples, positioned relative to the
trigger, and has no notion of time.  The simulation dumps a *continuous* trace
of registered mirrors.  This module converts the latter into the former, in the
sample domain, so that the comparison is a same-shape diff (plan §5.1: "anchor
on the trigger, not on time").

THE ONE-SAMPLE TRIGGER OFFSET  (INTERFACES.md §3.2 -- the cropper owns it)
-------------------------------------------------------------------------
The generated shadow module registers ``trigger_marker`` off the *already
registered* mirrors::

    always_ff @(posedge sample_clk) haddr          <= <design>.haddr;
    always_ff @(posedge sample_clk) trigger_marker <= (haddr == ...);

so at sample edge N, ``trigger_marker`` takes the value of the condition
evaluated on the mirrors as they stood *before* edge N -- i.e. on sample N-1's
data.  Therefore:

    trigger_marker high at sample N  <=>  the trigger condition held at sample N-1

and the trigger sample index is ``(first N where marker == '1') - 1``.  This is
deliberate; it is NOT to be "fixed" in the generator.  It is unit tested in
tests/test_compare.py.

WINDOW CONVENTION (documented, exact)
-------------------------------------
Let ``T`` be the trigger sample index and ``D`` the manifest ``depth``.  The
window is ``D`` samples wide and places the trigger sample at window index
``trigger_offset(D, trigger_time)``:

    trigger_time   trigger at window index   pre-trigger samples   post-trigger
    ------------   -----------------------   -------------------   ------------
    early          0                         0                     D-1
    middle         D // 2                    D//2                  D-1 - D//2
    late           D - 1                     D-1                   0

so ``middle`` gives ``D//2`` samples strictly before the trigger and the
trigger sample plus ``D - 1 - D//2`` after it -- matching the plan's
"sample 0 is ``-triggertime middle`` => index ``depth/2``" (§5.1).  For odd
``D``, ``middle`` biases the extra sample to the post-trigger side.

Absolute window bounds are ``[T - offset, T - offset + D - 1]`` inclusive.  If
those bounds fall outside the recorded trace the crop is a **hard error**
(exit 2): emitting a short or re-positioned window would make every downstream
comparison meaningless while still looking green.  Real hardware always has a
full buffer, so this only bites a sim that is too short for the chosen depth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fsdb_tools import (  # noqa: E402
    EXIT_HARNESS,
    EXIT_MATCH,
    HarnessError,
    VcdVar,
    canon_path,
    fsdb2vcd,
    fsdb_time_range,
    normalise_vcd_value,
    parse_vcd,
)

# Reserved manifest names -- INTERFACES.md §1.
TRIGGER_MARKER = "trigger_marker"
SAMPLE_CLK = "sample_clk"
SAMPLE_INDEX = "sample_index"
RESERVED_NAMES = (TRIGGER_MARKER, SAMPLE_CLK, SAMPLE_INDEX)

#: signal_map.tsv marker for a signal the harness synthesises on that side.
DERIVED = "__DERIVED__"

#: Signals Identify injects into every ``write fsdb`` capture of its own accord.
#: They are NOT manifest signals.  Measured against a real capture:
#:   * ``identify_sampleclock`` -- aliased onto the SAME VCD identifier as the
#:     probed sample clock (``dut_clk``), i.e. the same waveform under two names.
#:   * ``identify_cycle``       -- declared but carrying ZERO value changes; it
#:     is not even present in ``$dumpvars``, so there is no data in it to keep.
#: Whitelisted so the set-equality precheck does not reject a good capture, and
#: reported so their presence is visible.  An *unrecognised* extra signal is
#: still exit 2 -- this is a named whitelist, not "ignore anything unexpected".
IDENTIFY_INJECTED = ("identify_sampleclock", "identify_cycle")

TRIGGER_TIMES = ("early", "middle", "late")


# --------------------------------------------------------------------------
# signal_map.tsv  (INTERFACES.md §3.3) and the manifest's iice block
# --------------------------------------------------------------------------

class SignalMapRow(object):
    __slots__ = ("name", "width", "radix", "hw_path", "sim_path")

    def __init__(self, name: str, width: int, radix: str, hw_path: str, sim_path: str):
        self.name = name
        self.width = width
        self.radix = radix
        self.hw_path = hw_path
        self.sim_path = sim_path

    @property
    def hw_derived(self) -> bool:
        return self.hw_path == DERIVED

    @property
    def sim_derived(self) -> bool:
        return self.sim_path == DERIVED

    def __repr__(self) -> str:  # pragma: no cover
        return "SignalMapRow(%r, w=%d)" % (self.name, self.width)


_MAP_HEADER = ["name", "width", "radix", "hw_path", "sim_path"]


def read_signal_map(path: str) -> List[SignalMapRow]:
    """Parse ``build/signal_map.tsv`` -- the frozen generator/compare interface.

    Strict on purpose: a malformed or truncated map must be a harness error, not
    a smaller comparison.
    """
    if not os.path.isfile(path):
        raise HarnessError(
            "signal_map.tsv not found: %s\n"
            "  Run `make gen` first (stream A generates it from the manifest)." % path
        )
    rows: List[SignalMapRow] = []
    with open(path) as fh:
        lines = [l.rstrip("\n") for l in fh if l.strip()]
    if not lines:
        raise HarnessError("signal_map.tsv is empty: %s" % path)
    header = lines[0].split("\t")
    if header != _MAP_HEADER:
        raise HarnessError(
            "signal_map.tsv header is %r, expected %r (INTERFACES.md §3.3)"
            % (header, _MAP_HEADER)
        )
    seen: Set[str] = set()
    for lineno, line in enumerate(lines[1:], start=2):
        parts = line.split("\t")
        if len(parts) != 5:
            raise HarnessError(
                "signal_map.tsv:%d has %d fields, expected 5: %r"
                % (lineno, len(parts), line)
            )
        name, width, radix, hw_path, sim_path = parts
        if name in seen:
            raise HarnessError("signal_map.tsv:%d duplicate signal name %r" % (lineno, name))
        seen.add(name)
        try:
            w = int(width)
        except ValueError:
            raise HarnessError("signal_map.tsv:%d width %r is not an integer" % (lineno, width))
        if w < 1:
            raise HarnessError("signal_map.tsv:%d width %d < 1" % (lineno, w))
        rows.append(SignalMapRow(name, w, radix, hw_path, sim_path))
    for required in (TRIGGER_MARKER, SAMPLE_CLK):
        if required not in seen:
            raise HarnessError(
                "signal_map.tsv is missing the reserved signal %r "
                "(INTERFACES.md §1 says it is ALWAYS present)" % required
            )
    return rows


def data_signal_names(rows: Sequence[SignalMapRow]) -> List[str]:
    """Manifest signals that carry DUT data, i.e. everything but the reserved two.

    ``sample_clk`` is excluded because after sampling it is 1 at every sample by
    construction -- comparing it is vacuous.  ``trigger_marker`` is excluded
    because it is harness alignment metadata, synthesised on the hardware side
    (``__DERIVED__``); comparing a derived signal against a derived signal proves
    nothing.  Both are still *presence*-checked and both are reported.
    """
    return [r.name for r in rows if r.name not in RESERVED_NAMES]


def read_manifest_iice(path: str) -> Dict[str, object]:
    """Read the ``iice:`` block of a manifest: name, depth, trigger_time, edge.

    Prefers stream A's ``manifest.py`` if it is importable (single source of
    truth); otherwise parses the YAML directly.  Only the handful of keys the
    crop needs are extracted, so this stays robust to schema growth.
    """
    if not os.path.isfile(path):
        raise HarnessError("manifest not found: %s" % path)
    doc = None
    try:
        import manifest as _stream_a  # type: ignore

        for fn in ("load", "load_manifest", "read_manifest", "parse"):
            f = getattr(_stream_a, fn, None)
            if callable(f):
                cand = f(path)
                # Only accept a result that is shaped like the frozen schema;
                # anything else falls through to plain YAML rather than being
                # trusted because the import happened to succeed.
                if isinstance(cand, dict) and isinstance(cand.get("iice"), dict):
                    doc = cand
                break
    except Exception:
        doc = None
    if doc is None:
        try:
            import yaml
        except ImportError:
            raise HarnessError(
                "PyYAML is not installed and stream A's manifest.py is not "
                "importable; cannot read %s" % path
            )
        with open(path) as fh:
            doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or "iice" not in doc:
        raise HarnessError("manifest %s has no top-level 'iice:' block" % path)
    iice = doc["iice"]
    if not isinstance(iice, dict):
        raise HarnessError("manifest %s: 'iice:' is not a mapping" % path)

    try:
        depth = int(iice["depth"])
    except (KeyError, TypeError, ValueError):
        raise HarnessError("manifest %s: iice.depth missing or not an integer" % path)
    if depth < 8:
        raise HarnessError("manifest %s: iice.depth=%d, schema requires >= 8" % (path, depth))
    trigger_time = str(iice.get("trigger_time", "middle")).strip().lower()
    if trigger_time not in TRIGGER_TIMES:
        raise HarnessError(
            "manifest %s: iice.trigger_time=%r, expected one of %s"
            % (path, trigger_time, list(TRIGGER_TIMES))
        )
    clock = iice.get("clock") or {}
    edge = str(clock.get("edge", "positive")).strip().lower() if isinstance(clock, dict) else "positive"
    if edge not in ("positive", "negative"):
        raise HarnessError("manifest %s: iice.clock.edge=%r" % (path, edge))
    return {
        "name": str(iice.get("name", "IICE")),
        "depth": depth,
        "trigger_time": trigger_time,
        "edge": edge,
    }


# --------------------------------------------------------------------------
# The sample-domain trace -- plain data, no tool, no FSDB
# --------------------------------------------------------------------------

class SampleTrace(object):
    """``depth``-independent sample-indexed trace: one bitstring per signal per sample.

    ``values[name][i]`` is a lowercase bitstring of exactly ``widths[name]``
    characters drawn from ``01xz``, MSB first.  Deliberately dumb and explicit:
    every downstream operation (comparison, X-masking, perturbation) is a
    string operation that a unit test can construct by hand.
    """

    __slots__ = ("widths", "values", "meta")

    def __init__(
        self,
        widths: Dict[str, int],
        values: Dict[str, List[str]],
        meta: Optional[Dict[str, object]] = None,
    ):
        self.widths = dict(widths)
        self.values = {k: list(v) for k, v in values.items()}
        self.meta = dict(meta or {})
        if set(self.widths) != set(self.values):
            raise HarnessError(
                "SampleTrace widths/values key mismatch: %s vs %s"
                % (sorted(self.widths), sorted(self.values))
            )
        lengths = {len(v) for v in self.values.values()}
        if len(lengths) > 1:
            raise HarnessError("SampleTrace signals have differing sample counts: %s" % sorted(lengths))
        for name, seq in self.values.items():
            w = self.widths[name]
            for i, bits in enumerate(seq):
                if len(bits) != w or set(bits) - set("01xz"):
                    raise HarnessError(
                        "SampleTrace[%s][%d]=%r is not %d chars of 01xz" % (name, i, bits, w)
                    )

    @property
    def names(self) -> List[str]:
        return sorted(self.values)

    @property
    def n_samples(self) -> int:
        for seq in self.values.values():
            return len(seq)
        return 0

    def slice(self, start: int, count: int) -> "SampleTrace":
        n = self.n_samples
        if start < 0 or count < 1 or start + count > n:
            raise HarnessError(
                "SampleTrace.slice(%d, %d) out of range for %d samples" % (start, count, n)
            )
        return SampleTrace(
            self.widths,
            {k: v[start : start + count] for k, v in self.values.items()},
            dict(self.meta),
        )

    def subset(self, names: Iterable[str]) -> "SampleTrace":
        want = list(names)
        missing = [n for n in want if n not in self.values]
        if missing:
            raise HarnessError("SampleTrace.subset: unknown signals %s" % missing)
        return SampleTrace(
            {k: self.widths[k] for k in want},
            {k: self.values[k] for k in want},
            dict(self.meta),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "n_samples": self.n_samples,
            "widths": self.widths,
            "values": self.values,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "SampleTrace":
        return cls(d["widths"], d["values"], d.get("meta"))  # type: ignore[index]

    def save(self, path: str) -> str:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=1, sort_keys=True)
        return path

    @classmethod
    def load(cls, path: str) -> "SampleTrace":
        if not os.path.isfile(path):
            raise HarnessError("sample trace JSON not found: %s" % path)
        with open(path) as fh:
            return cls.from_dict(json.load(fh))


# --------------------------------------------------------------------------
# Window maths -- pure, and the part most worth unit testing
# --------------------------------------------------------------------------

def trigger_offset(depth: int, trigger_time: str) -> int:
    """Window index at which the trigger sample sits.  See module docstring."""
    if depth < 1:
        raise HarnessError("depth must be >= 1, got %d" % depth)
    tt = str(trigger_time).strip().lower()
    if tt == "early":
        return 0
    if tt == "middle":
        return depth // 2
    if tt == "late":
        return depth - 1
    raise HarnessError("trigger_time=%r, expected one of %s" % (trigger_time, list(TRIGGER_TIMES)))


def find_trigger_sample(marker: Sequence[str]) -> int:
    """Trigger sample index from the ``trigger_marker`` samples.

    ``marker`` is the per-sample bitstring sequence for ``trigger_marker``
    (width 1).  Returns ``first_high_index - 1`` per INTERFACES.md §3.2.

    Only an exact ``'1'`` counts as high: the marker register is X before the
    first edge, and treating X as "maybe fired" would position the window
    arbitrarily -- exactly what the contract forbids.
    """
    first_high = None
    for i, bits in enumerate(marker):
        if bits == "1":
            first_high = i
            break
    if first_high is None:
        raise HarnessError(
            "trigger never fired: trigger_marker is never '1' in %d samples.\n"
            "  The window position would be arbitrary, so this is a hard error.\n"
            "  Check trigger.expr in the manifest and that the sim actually "
            "reaches the trigger condition." % len(marker)
        )
    if first_high == 0:
        raise HarnessError(
            "trigger_marker is already '1' at sample 0, so the trigger condition "
            "held at sample -1, which was never recorded.\n"
            "  INTERFACES.md §3.2: marker high at N means the condition held at "
            "N-1.  Start the FSDB dump earlier or move the trigger later."
        )
    return first_high - 1


def window_bounds(
    n_samples: int, trigger_sample: int, depth: int, trigger_time: str
) -> Tuple[int, int]:
    """Absolute inclusive ``(start, end)`` of the window, or a hard error."""
    offset = trigger_offset(depth, trigger_time)
    start = trigger_sample - offset
    end = start + depth - 1
    if start < 0 or end > n_samples - 1:
        raise HarnessError(
            "window does not fit the recorded trace: depth=%d trigger_time=%s "
            "puts the trigger at window index %d, so samples [%d..%d] are needed "
            "but only [0..%d] were recorded (trigger at sample %d).\n"
            "  Refusing to emit a short or re-positioned window -- it would look "
            "green while comparing the wrong cycles.\n"
            "  Fix: lower iice.depth, or run the sim longer / trigger later."
            % (depth, trigger_time, offset, start, end, n_samples - 1, trigger_sample)
        )
    return start, end


# --------------------------------------------------------------------------
# VCD events -> SampleTrace  (pure; FSDB I/O stays at the edges)
# --------------------------------------------------------------------------

def _ident_map(
    variables: Sequence[VcdVar],
    path_to_name: Dict[str, str],
    by_leaf: bool = False,
) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
    """Map VCD identifiers to manifest names.  Returns ``(ident_to_names, widths)``.

    *path_to_name* keys are full '/'-joined paths when ``by_leaf`` is false (the
    simulation side), or bare leaf names when it is true (the hardware side --
    see :func:`hw_leaf_map`).

    An identifier maps to a **list** of names, not one name, because a real
    Identify FSDB aliases signals onto a shared identifier: the measured capture
    declares both ``identify_sampleclock`` and ``dut_clk`` against identifier
    ``!``.  Collapsing that to one name would silently drop whichever lost, so
    every aliased name is updated on each change.
    """
    ident_to_names: Dict[str, List[str]] = {}
    widths: Dict[str, int] = {}
    by_name: Dict[str, List[VcdVar]] = {}
    if by_leaf:
        wanted = dict(path_to_name)
        key_of = lambda v: v.name  # noqa: E731
    else:
        # Canonicalise both sides: signal_map.tsv hardware paths carry a leading
        # slash, VCD scope chains do not.
        wanted = {canon_path(p): n for p, n in path_to_name.items()}
        key_of = lambda v: canon_path(v.path)  # noqa: E731
    for v in variables:
        name = wanted.get(key_of(v))
        if name is None:
            continue
        by_name.setdefault(name, []).append(v)
    dupes = {n: [v.path for v in vs] for n, vs in by_name.items() if len(vs) > 1}
    if dupes:
        raise HarnessError(
            "signal(s) map to more than one FSDB variable, so the comparison "
            "would be against an arbitrary one of them: %r" % dupes
        )
    missing = sorted(set(path_to_name.values()) - set(by_name))
    if missing:
        want = {v: k for k, v in path_to_name.items()}
        raise HarnessError(
            "signal(s) named in signal_map.tsv are absent from the trace: %s\n"
            "  expected at %s: %s\n"
            "  INTERFACES.md §4: a missing signal is exit 2, never a skipped row."
            % (missing, "leaf name(s)" if by_leaf else "path(s)",
               [want[m] for m in missing])
        )
    for name, vs in by_name.items():
        ident_to_names.setdefault(vs[0].ident, []).append(name)
        widths[name] = vs[0].width
    return ident_to_names, widths


def _apply(
    evs: Sequence[Tuple[str, str]],
    ident_to_names: Dict[str, List[str]],
    widths: Dict[str, int],
    cur: Dict[str, str],
) -> None:
    """Apply one timestamp's value changes to *cur*, honouring ident aliasing."""
    for ident, raw in evs:
        for name in ident_to_names.get(ident, ()):
            cur[name] = normalise_vcd_value(raw, widths[name])


def samples_from_clock_edges(
    variables: Sequence[VcdVar],
    changes: Sequence[Tuple[int, Sequence[Tuple[str, str]]]],
    path_to_name: Dict[str, str],
    clk_name: str,
    edge: str = "positive",
) -> SampleTrace:
    """Sample every mapped signal once per sample-clock edge.

    The mirrors are already registered in the shadow module, so the correct
    sample value is the state *after* all changes at the edge timestamp have
    been applied: at posedge T the mirror takes the design value from just
    before T, which is precisely what the IICE stores as that sample.  One
    sample per edge -- no interpolation, no combinational glitches.
    """
    if edge not in ("positive", "negative"):
        raise HarnessError("edge=%r, expected positive|negative" % edge)
    ident_to_names, widths = _ident_map(variables, path_to_name)
    if clk_name not in widths:
        raise HarnessError("sample clock %r is not in the mapped signal set" % clk_name)
    if widths[clk_name] != 1:
        raise HarnessError("sample clock %r has width %d, expected 1" % (clk_name, widths[clk_name]))

    cur: Dict[str, str] = {n: "x" * w for n, w in widths.items()}
    out: Dict[str, List[str]] = {n: [] for n in widths}
    want_prev, want_now = ("0", "1") if edge == "positive" else ("1", "0")
    prev_clk = None  # unknown before the first dump
    edge_times: List[int] = []

    for t, evs in changes:
        _apply(evs, ident_to_names, widths, cur)
        clk = cur[clk_name]
        if prev_clk == want_prev and clk == want_now:
            edge_times.append(t)
            for n in widths:
                out[n].append(cur[n])
        prev_clk = clk

    if not out[clk_name]:
        raise HarnessError(
            "no %s edge of the sample clock %r was found in the trace; "
            "nothing to sample" % (edge, clk_name)
        )
    meta = {
        "source": "clock_edges",
        "edge": edge,
        "clk_name": clk_name,
        "first_edge_time": edge_times[0],
        "last_edge_time": edge_times[-1],
    }
    return SampleTrace(widths, out, meta)


def sample_grid(
    timestamps: Sequence[int],
    n_samples: int,
    end_time: Optional[int] = None,
    min_time: Optional[int] = None,
    info: Optional[Dict[str, object]] = None,
) -> List[int]:
    """Times at which the *n_samples* hardware samples sit.

    **Sample index is the timestamp ORDINAL, not the absolute time.**  An IICE
    trace is trigger-relative, so its axis starts wherever the capture happened
    to land -- the measured Identify capture runs ``#10240 .. #20470`` step 10.
    Nothing may key off absolute time.

    Cases, in order:

    1. ``len(timestamps) == n_samples`` -- the normal, measured case (a
       ``-depth 1024`` IICE gives exactly 1024 timestamps).  The timestamps *are*
       the grid; no inference at all.
    2. Fewer timestamps than samples -- some samples carried no value change, and
       an FSDB legitimately stores nothing for those.  The period is recovered as
       the GCD ``g`` of the observed gaps and the grid is ``ts[0] + i*g``, but
       that grid is only accepted if its **span is proved**, never guessed:

       a. *end_time given* (:func:`fsdb_tools.fsdb_time_range`, the FSDB's own
          ``max xtag``) -- accept iff ``(n-1)*g <= end_time - ts[0] <= n*g``.
       b. *end_time not given* -- accept iff the LAST OBSERVED timestamp is the
          last sample, i.e. ``(ts[-1] - ts[0]) // g + 1 == n_samples``.  This is
          the historical rule and stays exactly as strict as it was.

    (2b) is deliberately ``==`` and not ``<=``.  With ``<=`` an every-other-sample
    trace (true period 10, only 100/120/140 present) infers a period of 20, spans
    3, pads the tail, and silently compares the wrong cycles against each other
    -- green and wrong.  Nothing *in the value changes alone* tells that apart
    from a genuine 3-sample trace, so with no end time the only safe answer is to
    refuse.

    (2a) is the fix for the one case (2b) gets wrong: a hardware trace whose
    **tail carries no value changes at all** -- a stalled or wedged capture, the
    exact thing this harness exists to look at.  ``fsdb2vcd`` "stops the VCD at
    the last transition", so those trailing timestamps never come back and (2b)
    refuses a grid that is in fact fully determined.  The missing information is
    the file's true end time, which is *not* in the VCD but *is* in the FSDB
    header, and asking for it separately turns the guess into a measurement.

    Why the two-sided bound in (2a), and why it is still safe:

    * Two producers were measured on this install, and they disagree by one
      sample period on where a file "ends":

      - Identify's own ``write fsdb``: last sample ``20470``, ``max xtag``
        ``20480`` -- one period beyond (INTERFACES.md §7 fact 6).
      - this harness's ``write_vcd`` + ``vcd2fsdb``: last sample == ``max xtag``
        (measured 63 for a depth-64 trace).

      So all that can be assumed is ``L <= end_time <= L + g`` where ``L`` is the
      last sample's time.  Substituting ``L = ts[0] + (n-1)*g`` gives exactly the
      bound above; it is satisfied by both conventions and by nothing wider.
    * It still refuses the every-other-sample trap: there the true period is
      ``g/2``, so the file really ends around ``ts[0] + 2g`` while the rule
      demands at least ``ts[0] + (n-1)*g`` -- the end time *proves* the file is
      too short for ``n`` samples of period ``g``, and we refuse.
    * It still refuses a genuinely truncated capture, for the same reason: a file
      that stopped early has a short ``max xtag``, and a short ``max xtag`` fails
      the lower bound.
    * The period is never sub-divided.  The grid step is always the GCD of the
      *observed* gaps, so no sample is ever invented *between* two observed
      timestamps -- only held forward across timestamps the FSDB legitimately
      omitted.  That is the property (2b) was written to protect.

    ``min_time`` (the FSDB's ``min xtag``), when given, must equal ``ts[0]``
    before (2a) is used: ``ts[0]`` is only the first sample if nothing precedes
    it in the file.  A mismatch falls back to (2b) rather than anchoring the grid
    on a timestamp that might not be sample 0.

    *info*, if given, is filled with diagnostics: ``grid_source``,
    ``sample_period``, ``n_timestamps``, ``end_time``.
    """
    if info is None:
        info = {}
    if n_samples < 1:
        raise HarnessError("n_samples must be >= 1, got %d" % n_samples)
    ts = sorted(set(timestamps))
    if not ts:
        raise HarnessError("hardware trace has no timestamps at all")
    info["n_timestamps"] = len(ts)
    info["end_time"] = end_time
    if len(ts) > n_samples:
        raise HarnessError(
            "hardware trace has %d timestamps but the manifest depth is %d.\n"
            "  One timestamp per sample is expected (measured: a -depth 1024 "
            "IICE yields exactly 1024).\n"
            "  If this came from `fsdb2vcd`, check that -keep_last_time was NOT "
            "passed: it appends a trailing time tag that is not a sample."
            % (len(ts), n_samples)
        )
    if len(ts) == n_samples:
        info["grid_source"] = "timestamps"
        info["sample_period"] = (ts[1] - ts[0]) if len(ts) > 1 else 0
        return ts

    # --- the end time can only anchor a grid on ts[0] if ts[0] starts the file.
    anchored = end_time is not None and (min_time is None or min_time == ts[0])
    span_to_end = (end_time - ts[0]) if end_time is not None else None

    if len(ts) == 1:
        # No gaps, so there is no GCD and no period evidence in the data at all.
        # Every sample holds the same value whatever the period is, so the values
        # are unambiguous -- only the reported time base depends on the step.
        # With an end time the step is recoverable exactly when it is unique.
        if anchored and span_to_end is not None and span_to_end > 0:
            lo = -(-span_to_end // n_samples)              # ceil
            hi = span_to_end // (n_samples - 1) if n_samples > 1 else span_to_end
            if lo == hi and lo > 0:
                info["grid_source"] = "single_timestamp_pinned_by_end_time"
                info["sample_period"] = lo
                return [ts[0] + i * lo for i in range(n_samples)]
        info["grid_source"] = "single_timestamp"
        info["sample_period"] = 1
        return [ts[0] + i for i in range(n_samples)]

    import functools

    step = functools.reduce(_gcd, (b - a for a, b in zip(ts, ts[1:])))
    if step <= 0:
        raise HarnessError("could not infer a sample period from %d timestamps" % len(ts))
    # No separate "is every timestamp on the grid?" test is needed: the GCD of
    # the gaps divides (t - ts[0]) for every t by construction, so they always
    # are.  The span check below is what actually constrains the result.
    span = (ts[-1] - ts[0]) // step + 1

    # (2a) the end time PROVES the span: n samples of period `step` fit the file,
    # and n+1 do not.  Both measured end-time conventions satisfy this.
    if anchored and span_to_end is not None:
        if (n_samples - 1) * step <= span_to_end <= n_samples * step:
            info["grid_source"] = "gcd_pinned_by_end_time"
            info["sample_period"] = step
            return [ts[0] + i * step for i in range(n_samples)]

    # (2b) no end time, or an end time that does NOT fit: fall back to the strict
    # historical rule.  It either accepts (last observed timestamp IS the last
    # sample) or refuses -- it never pads on inference alone.
    if span != n_samples:
        if end_time is None:
            why = (
                "  No independent end time was available for this trace, so the "
                "only evidence about the span is the last value change.\n"
            )
        elif not anchored:
            why = (
                "  The FSDB reports min time %s but the first value change is at "
                "%d, so %d cannot be assumed to be sample 0 and its end time "
                "(%d) cannot anchor the grid.\n" % (min_time, ts[0], ts[0], end_time)
            )
        else:
            why = (
                "  The FSDB's own end time (%d, i.e. %d after the first sample) "
                "does NOT fit %d samples of period %d -- that needs an end time "
                "in %d..%d.  A trace whose samples merely stopped CHANGING has "
                "an end time in that range; one that stopped being WRITTEN, or "
                "whose true period is smaller than %d, does not.\n"
                % (end_time, span_to_end, n_samples, step,
                   ts[0] + (n_samples - 1) * step, ts[0] + n_samples * step, step)
            )
        raise HarnessError(
            "cannot establish the hardware sample grid: %d timestamps over "
            "%d..%d imply a period of %d and a span of %d sample(s), but the "
            "manifest depth is %d.\n"
            "%s"
            "  Refusing to guess: if the true period were smaller, padding the "
            "difference would compare the wrong cycles against each other and "
            "still report green.\n"
            "  Expected one timestamp per sample (a -depth %d IICE gives exactly "
            "%d), or a span the file's own end time can prove."
            % (len(ts), ts[0], ts[-1], step, span, n_samples, why,
               n_samples, n_samples)
        )
    info["grid_source"] = "gcd_span"
    info["sample_period"] = step
    return [ts[0] + i * step for i in range(n_samples)]


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return abs(a)


def samples_from_timestamp_ordinals(
    variables: Sequence[VcdVar],
    changes: Sequence[Tuple[int, Sequence[Tuple[str, str]]]],
    path_to_name: Dict[str, str],
    n_samples: int,
    by_leaf: bool = False,
    end_time: Optional[int] = None,
    min_time: Optional[int] = None,
) -> SampleTrace:
    """Hardware-side loader: one sample per timestamp, indexed by ordinal.

    The hardware side needs no cropping -- it arrives already windowed, exactly
    ``depth`` samples wide, which is what makes the offline-crop model in
    INTERFACES.md §0 work.  Values are held forward across a timestamp that
    carries no change.

    *end_time* / *min_time* are the FSDB's own time bounds
    (:func:`fsdb_tools.fsdb_time_range`).  They are what lets :func:`sample_grid`
    accept a trace whose TAIL carries no value changes -- see its docstring.
    Without them the strict historical rule applies and such a trace is refused.

    The returned ``meta`` records how the grid was established and how much of
    the tail is held forward, so ``compare_report.txt`` can name a quiet tail as
    a finding instead of leaving the reader to notice it:

    ``grid_source``            how the span was established (never "guessed")
    ``n_timestamps``           value-change timestamps actually in the trace
    ``quiet_samples``          samples carrying no value change on any signal
    ``last_change_sample``     ordinal of the last sample that changed anything
    ``quiet_tail_samples``     ``n_samples - 1 - last_change_sample``
    """
    ident_to_names, widths = _ident_map(variables, path_to_name, by_leaf=by_leaf)
    by_time: Dict[int, List[Tuple[str, str]]] = {}
    for t, evs in changes:
        by_time.setdefault(t, []).extend(evs)
    grid_info: Dict[str, object] = {}
    grid = sample_grid(sorted(by_time), n_samples, end_time=end_time,
                       min_time=min_time, info=grid_info)
    grid_set = set(grid)
    stray = sorted(t for t in by_time if t not in grid_set)
    if stray:
        raise HarnessError(
            "hardware trace has value changes at times not on the sample grid: "
            "%s (grid %d..%d)" % (stray[:5], grid[0], grid[-1])
        )
    cur: Dict[str, str] = {n: "x" * w for n, w in widths.items()}
    out: Dict[str, List[str]] = {n: [] for n in widths}
    for t in grid:
        _apply(by_time.get(t, []), ident_to_names, widths, cur)
        for n in widths:
            out[n].append(cur[n])
    # A timestamp is "quiet" when the FSDB stored no value change for it.  The
    # index of the LAST non-quiet one is what distinguishes "the capture is
    # short" (already refused above) from "the DUT stopped moving" (a finding).
    changed = sorted(t for t in by_time if by_time[t])
    last_change_sample = grid.index(changed[-1]) if changed else -1
    return SampleTrace(widths, out, {
        "source": "timestamp_ordinals",
        "n_samples": n_samples,
        "first_time": grid[0],
        "last_time": grid[-1],
        "sample_period": (grid[1] - grid[0]) if len(grid) > 1 else 0,
        "grid_source": grid_info.get("grid_source"),
        "fsdb_end_time": end_time,
        "n_timestamps": len(changed),
        "quiet_samples": n_samples - len(changed),
        "last_change_sample": last_change_sample,
        "quiet_tail_samples": n_samples - 1 - last_change_sample,
    })


# Leaf names that carry the IICE sample clock in a REAL capture, best first.
# MEASURED 2026-07-30 on silicon: Identify emits `identify_sampleclock` in every
# capture, and aliases the probed clock (`dut_clk`) onto the SAME VCD identifier,
# so either name resolves to the same waveform. See INTERFACES.md §7 fact 8.
HW_SAMPLE_CLOCK_LEAVES = ("identify_sampleclock", "dut_clk")


def _find_clock_ident(variables: Sequence[VcdVar],
                      candidates: Sequence[str] = HW_SAMPLE_CLOCK_LEAVES,
                      ) -> Optional[Tuple[str, str]]:
    """``(ident, leaf)`` of the first candidate sample-clock leaf present, or None."""
    by_leaf: Dict[str, str] = {}
    for v in variables:
        leaf = v.name.rsplit("/", 1)[-1]
        if v.width == 1:
            by_leaf.setdefault(leaf, v.ident)
    for leaf in candidates:
        if leaf in by_leaf:
            return by_leaf[leaf], leaf
    return None


def samples_from_sample_clock(
    variables: Sequence[VcdVar],
    changes: Sequence[Tuple[int, Sequence[Tuple[str, str]]]],
    leaf_to_name: Dict[str, str],
    clk_ident: str,
    clk_leaf: str,
    edge: str = "positive",
) -> SampleTrace:
    """Hardware-side loader for a capture whose SAMPLE CLOCK TOGGLES.

    **Why this exists — measured on silicon 2026-07-30.** The harness was built
    on "one timestamp per sample", which is what the *demo-cable* capture really
    does. A capture off the real board does not: ``identify_sampleclock``
    toggles, so each sample contributes a rising **and** a falling timestamp and a
    923-sample capture arrives as **1846** timestamps. Fed to
    :func:`samples_from_timestamp_ordinals` that trips its
    ``len(ts) > n_samples`` guard and the capture is refused (exit 2) — correct,
    but useless.

    The fix is not to relax that guard. It is to stop inferring the sample count
    from the timestamp count at all: the sample clock IS the sample grid, so
    **counting its edges is a measurement**. One sample per qualifying edge, no
    inference, and a toggling clock and a one-tag-per-sample clock both come out
    right.

    Values are taken *after* all changes at the edge timestamp are applied, the
    same convention as :func:`samples_from_clock_edges`, so both sides of the
    comparison sample identically.

    The sample count here is whatever the hardware actually delivered — it is
    deliberately NOT forced to the manifest depth, because those differ in
    practice (INTERFACES.md §7 fact 9: a depth-1024 IICE returned 923 samples,
    with the debugger itself reporting ``Range 0 1023 exceeds maximum 922``).
    The caller is responsible for reconciling that, loudly.
    """
    if edge not in ("positive", "negative"):
        raise HarnessError("edge=%r, expected positive|negative" % edge)
    ident_to_names, widths = _ident_map(variables, leaf_to_name, by_leaf=True)

    cur: Dict[str, str] = {n: "x" * w for n, w in widths.items()}
    out: Dict[str, List[str]] = {n: [] for n in widths}
    want_prev, want_now = ("0", "1") if edge == "positive" else ("1", "0")
    clk_val: Optional[str] = None
    # Seeded to the OPPOSITE phase, unlike samples_from_clock_edges which starts
    # from None.  That difference is deliberate and is the hardware/simulation
    # asymmetry: a simulation dump has arbitrary history before its first
    # timestamp, so the first edge there is genuinely unknown.  An Identify
    # capture does not -- the buffer is written out from index 0, so its FIRST
    # timestamp IS sample 0, and dropping that edge loses a real sample (caught
    # by test_..._loads_via_MEASURED_clock_edges: 8 samples came back as 7).
    prev_clk: Optional[str] = want_prev
    edge_times: List[int] = []
    changed_at_edge: List[bool] = []

    for t, evs in changes:
        # The clock may or may not be in the mapped signal set, so track it from
        # the raw event stream by identifier rather than through `cur`.
        for ident, val in evs:
            if ident == clk_ident:
                clk_val = val[0].lower() if val else clk_val
        _apply(evs, ident_to_names, widths, cur)
        if prev_clk == want_prev and clk_val == want_now:
            edge_times.append(t)
            # "did anything other than the clock change at this edge?"
            changed_at_edge.append(
                any(ident != clk_ident for ident, _ in evs)
            )
            for n in widths:
                out[n].append(cur[n])
        prev_clk = clk_val

    if not edge_times:
        raise HarnessError(
            "no %s edge of the hardware sample clock %r was found in the trace.\n"
            "  Without sample-clock edges there is no measured sample grid, and "
            "inferring one from timestamps is what INTERFACES.md §7 fact 8 "
            "exists to prevent." % (edge, clk_leaf)
        )
    n = len(edge_times)
    last_change = max((i for i, c in enumerate(changed_at_edge) if c), default=-1)
    return SampleTrace(widths, out, {
        "source": "hw_sample_clock_edges",
        "n_samples": n,
        "clock_leaf": clk_leaf,
        "edge": edge,
        "first_time": edge_times[0],
        "last_time": edge_times[-1],
        "sample_period": (edge_times[1] - edge_times[0]) if n > 1 else 0,
        "grid_source": "sample-clock edges (measured, not inferred)",
        "n_timestamps": len(changes),
        "quiet_samples": n - sum(1 for c in changed_at_edge if c),
        "last_change_sample": last_change,
        "quiet_tail_samples": n - 1 - last_change,
    })


# --------------------------------------------------------------------------
# FSDB I/O at the edges
# --------------------------------------------------------------------------

def _vcd_for(fsdb: str, logdir: str, tag: str) -> str:
    vcd = os.path.join(logdir, "%s.vcd" % tag)
    return fsdb2vcd(fsdb, vcd, logdir)


#: Conventional scope name.  NOT privileged: INTERFACES.md §1 (amended
#: 2026-07-29) identifies the scope by its leaf set, not by its name, because
#: ``$fsdbDumpvars(0, <module>)`` records the *instance* path.  Kept only as the
#: default for callers that already know the scope.
SIM_SCOPE = "iice"


def resolve_sim_scope(
    sim_signals: Iterable[str], rows: Sequence[SignalMapRow]
) -> Tuple[str, Optional[str]]:
    """Find the scope holding the manifest signals.  Returns ``(scope, note)``.

    INTERFACES.md §1 identifies the scope by its **leaf set**, not by its name:
    the scope name legitimately differs between the two sides
    (``tb/u_iice_shadow`` in sim, whatever the hardware normalisation produces),
    because ``$fsdbDumpvars(0, iice_shadow_<NAME>)`` records the instance path.

    Enforcement, per §1 and non-negotiable:

    * exactly one scope whose leaf-name set equals the manifest set -> proceed
    * partial match -> :class:`HarnessError` (exit 2)
    * more than one matching scope -> :class:`HarnessError` (exit 2)

    Never guess a scope and never compare a partially-matched leaf set:
    comparing the wrong ``haddr`` against the wrong ``haddr`` is the exact
    failure this harness exists to prevent.  *note* is an informational string
    recording how the scope was resolved, for the report.
    """
    expect = {r.name for r in rows}
    by_scope: Dict[str, Set[str]] = {}
    for path in sim_signals:
        parts = [p for p in str(path).split("/") if p]
        if len(parts) < 2:
            continue
        by_scope.setdefault("/".join(parts[:-1]), set()).add(parts[-1])

    exact = sorted(s for s, leaves in by_scope.items() if leaves == expect)
    if len(exact) == 1:
        n_scopes = len(by_scope)
        return exact[0], (
            "sim scope %r resolved as the unique scope whose leaf set is exactly "
            "the manifest signal set (%d leaves; %d %s in the FSDB). Scope names "
            "differ between sides by design -- INTERFACES.md §1 identifies the "
            "scope by its leaf set, not its name."
            % (exact[0], len(expect), n_scopes,
               "scope" if n_scopes == 1 else "scopes")
        )
    if len(exact) > 1:
        raise HarnessError(
            "ambiguous sim FSDB: %d scopes contain exactly the manifest signal "
            "set (%s). Refusing to guess which one the IICE mirrors are in."
            % (len(exact), exact)
        )
    best = sorted(by_scope.items(), key=lambda kv: -len(kv[1] & expect))
    detail = ""
    if best:
        scope, leaves = best[0]
        detail = "\n  closest scope %r has %s\n  missing there: %s\n  extra there: %s" % (
            scope, sorted(leaves), sorted(expect - leaves), sorted(leaves - expect)
        )
    raise HarnessError(
        "no scope in the sim FSDB contains exactly the manifest signal set.\n"
        "  expected leaves: %s%s\n"
        "  INTERFACES.md §1: the trace must expose exactly one scope whose leaf "
        "set is exactly the manifest signal set; a partial match is exit 2."
        % (sorted(expect), detail)
    )


def load_sim_trace(
    fsdb: str,
    rows: Sequence[SignalMapRow],
    logdir: str,
    edge: str = "positive",
    scope: str = SIM_SCOPE,
) -> SampleTrace:
    """Continuous sim FSDB -> sample-domain trace (one sample per clock edge).

    *scope* is the FSDB scope holding the mirrors; use
    :func:`resolve_sim_scope` to determine it.
    """
    path_to_name = {"%s/%s" % (scope, r.name): r.name for r in rows}
    variables, changes = parse_vcd(_vcd_for(fsdb, logdir, "sim_iice"))
    trace = samples_from_clock_edges(variables, changes, path_to_name, SAMPLE_CLK, edge)
    trace.meta["sim_scope"] = scope
    return trace


# NOTE: there was a `hw_path_map()` here that keyed the hardware side on the
# FULL manifest hw_path.  It is gone, not deprecated: a real Identify capture
# flattens the design hierarchy away, so full-path matching can never match one,
# and leaving a plausible-looking alternative matcher in place is an invitation
# to wire up the wrong one.  Use `hw_leaf_map()`.


def hw_leaf(hw_path: str) -> str:
    """Last component of a ``signal_map.tsv`` hardware path.

    A real Identify capture **flattens the hierarchy away**: measured against
    ``write fsdb``, a probe on a deep path is declared simply as
    ``blink_counter``, inside one scope named after the instrumented module
    (``rm_led``) -- no design hierarchy at all.  So the hardware side is matched
    on the leaf name, not on the manifest's full ``hw_path``.
    """
    parts = [p for p in str(hw_path).split("/") if p]
    if not parts:
        raise HarnessError("empty hardware path in signal_map.tsv")
    return parts[-1]


def hw_leaf_map(rows: Sequence[SignalMapRow]) -> Dict[str, str]:
    """``{hw_leaf_name: canonical_name}`` for every non-``__DERIVED__`` row.

    Optional rows are **included**: "optional" means may-be-absent, not
    must-be-absent, and a real capture does contain the sample clock.

    A leaf collision is a hard error: if two probes share a leaf name, a flat
    hardware trace cannot tell them apart, and comparing the wrong one is worse
    than not comparing at all.  Fix it by renaming in the manifest.
    """
    out: Dict[str, str] = {}
    for r in rows:
        if r.hw_derived:
            continue
        leaf = hw_leaf(r.hw_path)
        if leaf in out:
            raise HarnessError(
                "signals %r and %r both reduce to the hardware leaf name %r.\n"
                "  A real Identify capture is flat, so they cannot be told apart.\n"
                "  Rename one of them in the manifest."
                % (out[leaf], r.name, leaf)
            )
        out[leaf] = r.name
    if not out:
        raise HarnessError("no hardware-side signals in signal_map.tsv (all __DERIVED__?)")
    return out


def load_hw_trace(
    fsdb: str,
    rows: Sequence[SignalMapRow],
    logdir: str,
    depth: int,
    optional: Sequence[str] = (),
) -> SampleTrace:
    """Hardware-side FSDB -> canonical sample trace, indexed by timestamp ordinal.

    Matched on **leaf names** (:func:`hw_leaf_map`), because a real Identify
    capture flattens the design hierarchy away.  That also keeps the synthetic
    phase-0 FSDB working, since the leaf of ``/rp_top/u_dut/HADDR`` is ``HADDR``
    either way.

    *optional* names are dropped from the requirement only if they are genuinely
    absent from the trace; when present they are loaded and compared like any
    other signal.

    The FSDB's own time bounds are read separately, via ``fsdb2vcd -summary``,
    and handed to :func:`sample_grid`.  They are NOT redundant with the VCD:
    ``fsdb2vcd`` stops the VCD at the last transition, so a trace whose tail
    carries no value change loses its trailing timestamps, and the end time is
    the only remaining evidence that the file really is ``depth`` samples long.
    Reading it lets a stalled capture be held forward and reported, instead of
    refused as an unestablishable grid.  If the summary cannot be parsed the
    bounds are simply not supplied and the strict rule applies -- an unprovable
    span must still be a harness error.
    """
    leafmap = hw_leaf_map(rows)
    variables, changes = parse_vcd(_vcd_for(fsdb, logdir, "hw_iice"))
    present = {v.name for v in variables}
    skip = set(optional)
    wanted = {
        leaf: name
        for leaf, name in leafmap.items()
        if leaf in present or name not in skip
    }
    if not wanted:
        raise HarnessError(
            "no manifest signal is present in the hardware trace (saw %s)"
            % sorted(present)[:10]
        )
    try:
        t_min, t_max = fsdb_time_range(fsdb, logdir)
    except HarnessError:
        t_min, t_max = None, None

    # --- pick the grid source ------------------------------------------------
    # The ordinal loader assumes ONE timestamp per sample. A real board capture
    # breaks that: the sample clock toggles, so each sample contributes two
    # timestamps (measured 2026-07-30: 923 samples arrived as 1846 timestamps --
    # INTERFACES.md §7 fact 8). Rather than relax the ordinal loader's
    # `len(ts) > n_samples` guard -- which exists to catch a genuinely
    # over-long trace -- switch to counting SAMPLE-CLOCK EDGES, which is a
    # measurement of the sample grid rather than an inference from it.
    #
    # Deliberately narrow: only when the ordinal path CANNOT work (more
    # timestamps than samples) and a sample clock is actually present. Every
    # existing one-tag-per-sample trace keeps the old path bit-for-bit, so this
    # cannot regress the synthetic phase-0 corpus.
    n_timestamps = len({t for t, _ in changes})
    clk = _find_clock_ident(variables)
    if n_timestamps > depth and clk is not None:
        clk_ident, clk_leaf = clk
        return samples_from_sample_clock(
            variables, changes, wanted, clk_ident, clk_leaf, edge="positive",
        )

    return samples_from_timestamp_ordinals(
        variables, changes, wanted, depth, by_leaf=True,
        end_time=t_max, min_time=t_min,
    )


def normalise_hw_capture(
    raw_fsdb: str,
    out_fsdb: str,
    rows: Sequence[SignalMapRow],
    logdir: str,
    depth: int,
    optional: Sequence[str] = (),
    manifest: Optional[str] = None,
    ids: Optional[Dict[str, str]] = None,
    origin: str = "identify_capture",
    stamp: bool = True,
) -> Dict[str, object]:
    """Turn a real Identify ``write fsdb`` capture into the compare step's input.

    Measured against an actual capture (``write fsdb -iice <n> -range {0 1023}``
    over a demo cable, so no board was involved):

    * exactly ``depth`` timestamps -- 1024 for ``-depth 1024``.  The hardware side
      arrives already windowed, so it is never cropped.
    * the time axis is **arbitrary and non-zero** (``#10240..#20470`` step 10),
      because the trace is trigger-relative.  Sample index therefore comes from
      the timestamp ordinal.
    * signal names are **flat** -- the design hierarchy is gone.  Everything sits
      in one scope named after the instrumented module (``rm_led``), so matching
      is by leaf name.
    * Identify adds ``identify_sampleclock`` (aliased onto the same VCD
      identifier as the probed clock) and ``identify_cycle`` (declared, but with
      no value changes at all).  Both are whitelisted, neither is compared.

    Re-emits the samples at the manifest ``hw_path`` names with one timestamp per
    sample so that ``load_hw_trace`` -- and hence ``make compare`` -- reads it
    exactly like the synthetic phase-0 FSDB.  Returns a description dict.

    PROVENANCE (added 2026-07-31).  When *manifest* is given, a stamp is written
    beside *out_fsdb* recording the manifest identity this capture was normalised
    against, the raw capture's path and size, the sample count actually
    recovered, how the sample grid was established, and any ``rm_id`` /
    ``static_id`` / ``static_usercode`` the caller supplies in *ids*.  ``compare``
    then REFUSES (exit 2) if that identity disagrees with the manifest it is
    given -- see ``provenance.py``.  Without *manifest* nothing is stamped, so
    every existing caller keeps its exact behaviour and an unstamped artifact
    stays comparable (absence is not a mismatch).

    The stamp is returned in ``desc["provenance"]`` and its path in
    ``desc["stamp_file"]``; both are ``None`` when nothing was stamped.
    """
    import fsdb_tools as _ft
    import provenance as _prov

    # The manifest <-> signal-map binding is checked FIRST, before any work: on
    # 2026-07-30 this normalisation ran against the SELFTEST rename table and the
    # only complaint came later, from the set-equality check, naming selftest
    # signals that were never in the capture. Refuse at the pairing, not
    # downstream of it.
    identity = None
    if stamp and manifest:
        identity = _prov.manifest_identity(manifest)
        _prov.check_signal_map(identity, rows)

    trace = load_hw_trace(raw_fsdb, rows, logdir, depth, optional=optional)
    by_name = {r.name: r for r in rows}
    names = sorted(trace.values)
    signals = [(by_name[n].hw_path, trace.widths[n]) for n in names]
    frames = [
        {by_name[n].hw_path: trace.values[n][i] for n in names}
        for i in range(trace.n_samples)
    ]
    vcd = os.path.join(logdir, "hw_iice_normalised.vcd")
    _ft.write_vcd(vcd, signals, frames, version="identify_iice normalised hw")
    _ft.vcd2fsdb(vcd, out_fsdb, logdir)
    desc: Dict[str, object] = {
        "out_fsdb": out_fsdb,
        "signals": names,
        "n_samples": trace.n_samples,
        "raw_capture": os.path.abspath(raw_fsdb),
        "raw_capture_bytes": (os.path.getsize(raw_fsdb)
                              if os.path.isfile(raw_fsdb) else None),
        "raw_time_base": (trace.meta.get("first_time"), trace.meta.get("last_time")),
        "sample_period": trace.meta.get("sample_period"),
        "grid_source": trace.meta.get("grid_source"),
        "quiet_tail_samples": trace.meta.get("quiet_tail_samples"),
        "last_change_sample": trace.meta.get("last_change_sample"),
        "provenance": None,
        "stamp_file": None,
    }
    if identity is not None:
        block = _prov.build_stamp(
            identity, out_fsdb,
            origin=origin,
            produced_by="crop_trace.normalise_hw_capture",
            raw_capture=raw_fsdb,
            capture={
                "n_samples": trace.n_samples,
                "signals": names,
                "grid_source": trace.meta.get("grid_source"),
                "sample_period": trace.meta.get("sample_period"),
                "raw_time_base": [trace.meta.get("first_time"),
                                  trace.meta.get("last_time")],
                "fsdb_end_time": trace.meta.get("fsdb_end_time"),
                "n_timestamps": trace.meta.get("n_timestamps"),
                "quiet_tail_samples": trace.meta.get("quiet_tail_samples"),
                "last_change_sample": trace.meta.get("last_change_sample"),
                "loader": trace.meta.get("source"),
                "manifest_depth": depth,
            },
            ids=ids,
        )
        desc["provenance"] = block
        desc["stamp_file"] = _prov.write_stamp(out_fsdb, block)
        desc["manifest_identity"] = identity
    return desc


def crop_sim_window(
    trace: SampleTrace, depth: int, trigger_time: str
) -> Tuple[SampleTrace, Dict[str, object]]:
    """Crop *trace* to the IICE window.  Returns ``(window, info)``.

    ``info`` records the trigger sample, the absolute bounds and the marker
    high-sample list, so ``compare_report.txt`` can show *where* the window came
    from rather than asserting it was right.
    """
    if TRIGGER_MARKER not in trace.values:
        raise HarnessError(
            "sim trace has no %r signal; cannot position the window "
            "(INTERFACES.md §1 requires it)" % TRIGGER_MARKER
        )
    marker = trace.values[TRIGGER_MARKER]
    trig = find_trigger_sample(marker)
    start, end = window_bounds(trace.n_samples, trig, depth, trigger_time)
    offset = trigger_offset(depth, trigger_time)
    highs = [i for i, b in enumerate(marker) if b == "1"]
    window = trace.slice(start, depth)
    info = {
        "trace_samples": trace.n_samples,
        "trigger_sample": trig,
        "marker_first_high_sample": trig + 1,
        "marker_high_samples_in_trace": len(highs),
        "marker_high_samples_in_window": len([i for i in highs if start <= i <= end]),
        "window_start_sample": start,
        "window_end_sample": end,
        "trigger_window_index": offset,
        "depth": depth,
        "trigger_time": trigger_time,
    }
    window.meta = dict(trace.meta)
    window.meta.update(info)
    return window, info


# --------------------------------------------------------------------------
# Value rendering (radix from the manifest -- plan §5.5)
# --------------------------------------------------------------------------

def render_value(bits: str, radix: str) -> str:
    """Render a 4-state bitstring in the manifest radix, X/Z-safe."""
    r = (radix or "bin").strip().lower()
    if r in ("bin", "b", "binary") or set(bits) & set("xz"):
        return "'b" + bits
    val = int(bits, 2)
    if r in ("hex", "h", "hexadecimal"):
        return "'h%0*X" % ((len(bits) + 3) // 4, val)
    if r in ("oct", "o", "octal"):
        return "'o%o" % val
    if r in ("dec", "d", "decimal", "udec"):
        return "%d" % val
    return "'b" + bits


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Crop a continuous sim FSDB into an IICE-shaped window",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--fsdb", required=True, help="continuous sim FSDB (flat iice/* scope)")
    ap.add_argument("--signal-map", required=True, help="build/signal_map.tsv")
    ap.add_argument("--manifest", required=True, help="signals_*.yaml (depth / trigger_time)")
    ap.add_argument("--out", required=True, help="output window JSON")
    ap.add_argument("--logdir", default=None, help="scratch + tool log directory")
    args = ap.parse_args(argv)

    logdir = args.logdir or os.path.join(os.path.dirname(os.path.abspath(args.out)), "croplog")
    try:
        rows = read_signal_map(args.signal_map)
        iice = read_manifest_iice(args.manifest)
        trace = load_sim_trace(args.fsdb, rows, logdir, str(iice["edge"]))
        window, info = crop_sim_window(trace, int(iice["depth"]), str(iice["trigger_time"]))
        window.save(args.out)
        print("crop_trace: %d recorded samples -> window [%d..%d] (%d samples)" % (
            info["trace_samples"], info["window_start_sample"],
            info["window_end_sample"], window.n_samples))
        print("crop_trace: trigger condition held at sample %d "
              "(trigger_marker first high at %d -- the documented one-sample offset)"
              % (info["trigger_sample"], info["marker_first_high_sample"]))
        print("crop_trace: trigger sits at window index %d (trigger_time=%s, depth=%d)" % (
            info["trigger_window_index"], info["trigger_time"], info["depth"]))
        print("crop_trace: wrote %s" % args.out)
    except HarnessError as exc:
        print("HARNESS ERROR (crop_trace): %s" % exc, file=sys.stderr)
        return EXIT_HARNESS
    return EXIT_MATCH


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
