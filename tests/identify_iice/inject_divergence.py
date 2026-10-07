#!/usr/bin/env python3
"""Inject a *realistic* hardware divergence, and interpret what the compare found.

This is the evaluation half of the harness (``make evaluate``).  It exists to
answer a different question from ``make check``:

    ``make check``     "is the comparator correct?"      -> pass/fail
    ``make evaluate``  "if I had a real hardware bug,
                        would this tooling find it, and
                        would the output tell me where
                        to look?"                        -> artifacts + a reading

``negctl`` already flips one bit, which proves the comparator is not blind.  It
does **not** tell you whether the *output* is useful, because a single random bit
flip is not a bug shape any silicon ever produced.  The three modes here are
shaped like bugs this repo has actually had:

``stuck``            one bit of one bus held at a constant level from some sample
                     onward.  The LAN8720/RMII class: a level shifter that cannot
                     drive, a pad stuck, a net tied off.
                     (docs: ``lan8720-rx-root-cause-xdc-swap``, the SH0 shifter
                     audit -- 11 RMII/MDIO pins behind one 40 Mbps bank.)

``skew``             hardware reproduces the simulation exactly but one sample
                     late, on one signal.  The CDC / sampling-phase class: a
                     missing synchroniser stage, a capture on the wrong edge, an
                     off-by-one in a bridge's ready/valid handshake.

``late-divergence``  hardware tracks the simulation and then stops advancing and
                     never recovers.  The "boots then wedges" class, and the
                     closest analogue of the live M0 question: the core is alive
                     and fetching, but something stops making progress partway
                     in.

``clean``            no injection at all -- the positive control.  A run whose
                     verdict is MATCH with a low X-masked fraction is what
                     "hardware agrees with the model" actually looks like.

Everything below the FSDB boundary is a pure string operation on
:class:`crop_trace.SampleTrace`, so ``tests/test_evaluate.py`` exercises all of
it with no VCS, no Verdi and no licence.

TWO RULES THAT ARE NOT NEGOTIABLE HERE
--------------------------------------
1. **An injected value is never X.**  Real hardware cannot produce X, and the
   X don't-care is one-way (INTERFACES.md §4).  An injector that wrote an X
   would be exercising ``negctl`` NC4's path, not a hardware bug, and the
   "divergence" it produced would be an artefact of the harness.  Every mode
   therefore derives its injected values from the already-quantised hardware
   trace, never from the simulation's raw 4-state values.
2. **The interpretation may not overclaim.**  The tooling localises to a signal
   and a sample index.  It cannot tell you which side is wrong, nor why.  Every
   classification printed by :func:`interpretation_text` carries the alternative
   explanation it cannot rule out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "ncompare")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fsdb_tools  # noqa: E402
import provenance  # noqa: E402
import synth_hw_fsdb  # noqa: E402
from crop_trace import (  # noqa: E402
    SampleTrace,
    crop_sim_window,
    data_signal_names,
    load_sim_trace,
    read_manifest_iice,
    read_signal_map,
    resolve_sim_scope,
    trigger_offset,
)
# NOTE: the mismatch TABLE's radix rendering lives in trace_compare.format_report
# (via crop_trace.render_value) and is not duplicated here.  The reading below
# quotes sample indices and bit numbers only, so a radix bug can never make this
# file and the report disagree about a value.
from fsdb_tools import EXIT_HARNESS, EXIT_MATCH, HarnessError  # noqa: E402
from trace_compare import compare_bits, compare_traces  # noqa: E402

#: Selectable failure modes.  ``clean`` first: it is the positive control.
MODES = ("clean", "stuck", "skew", "late-divergence")

#: One line each, for `make evaluate-help` and the argparse epilogue.
MODE_BLURB = {
    "clean": "no injection - the positive control (expect MATCH)",
    "stuck": "one bus bit held at a constant level from a sample onward "
             "(LAN8720/RMII class)",
    "skew": "hardware lags the simulation by one sample on one signal "
            "(CDC / sampling-phase class)",
    "late-divergence": "hardware tracks the model, then stops advancing and "
                       "never recovers (boots-then-wedges class)",
}


# ==========================================================================
# Baseline: the same synthetic hardware trace `make fake-hw` produces
# ==========================================================================

def build_baseline(
    sim_fsdb: str,
    signal_map: str,
    manifest: str,
    logdir: str,
    x_mode: str = "prng",
    x_seed: str = "iice",
) -> Dict[str, object]:
    """Load the sim window and the clean synthetic hardware trace.

    Deliberately built from ``synth_hw_fsdb.quantise_trace`` with the same
    default seed as ``make fake-hw``, so ``MODE=clean`` reproduces the CI gate's
    hardware trace exactly rather than a look-alike.  The sim window is returned
    as well, which ``synth_hw_fsdb.build_hw_trace`` does not expose -- the
    injectors need it to pick targets that are not X-masked.
    """
    rows = read_signal_map(signal_map)
    iice = read_manifest_iice(manifest)
    depth = int(iice["depth"])
    names = data_signal_names(rows)
    if not names:
        raise HarnessError("signal_map.tsv has no data signals to inject into")
    scope, scope_note = resolve_sim_scope(
        fsdb_tools.list_signals(sim_fsdb, logdir), rows
    )
    sim_trace = load_sim_trace(sim_fsdb, rows, logdir, str(iice["edge"]), scope=scope)
    window, info = crop_sim_window(sim_trace, depth, str(iice["trigger_time"]))
    sim_data = window.subset(names)
    hw_trace, n_quant = synth_hw_fsdb.quantise_trace(sim_data, x_mode, x_seed)
    return {
        "rows": rows,
        "iice": iice,
        "names": names,
        "sim_window_full": window,   # includes trigger_marker + sample_clk
        "sim_data": sim_data,        # data signals only == what is compared
        "hw_trace": hw_trace,
        "window_info": info,
        "x_quantised_bits": n_quant,
        "sim_scope": scope,
        "sim_scope_note": scope_note,
        "trigger_index": trigger_offset(depth, str(iice["trigger_time"])),
    }


# ==========================================================================
# Pure helpers over a sample column
# ==========================================================================

def _bit(bits: str, index: int) -> str:
    """LSB-first bit *index* of a bitstring (bit 0 is the rightmost char)."""
    width = len(bits)
    if index < 0 or index >= width:
        raise HarnessError("bit %d out of range for width %d" % (index, width))
    return bits[width - 1 - index]


def _set_bit(bits: str, index: int, value: str) -> str:
    if value not in "01":
        raise HarnessError(
            "refusing to inject %r: an injected hardware value must be 0 or 1. "
            "Hardware cannot produce X, and the X don't-care is one-way "
            "(INTERFACES.md §4) -- injecting an X would exercise negctl NC4, "
            "not a hardware bug." % value
        )
    width = len(bits)
    pos = width - 1 - index
    return bits[:pos] + value + bits[pos + 1 :]


def _known_bit_values(col: Sequence[str], index: int, lo: int, hi: int) -> set:
    """The set of 0/1 values a column takes on bit *index* over samples [lo, hi]."""
    return {_bit(col[i], index) for i in range(lo, hi + 1)} & {"0", "1"}


def _has_x(col: Sequence[str], lo: int, hi: int, index: Optional[int] = None) -> bool:
    for i in range(lo, hi + 1):
        chunk = col[i] if index is None else _bit(col[i], index)
        if set(chunk) & set("xz"):
            return True
    return False


def _n_changes(col: Sequence[str]) -> int:
    return sum(1 for i in range(1, len(col)) if col[i] != col[i - 1])


# ==========================================================================
# The injectors.  Each mutates `hw` and returns a description dict.
# ==========================================================================

def choose_stuck_target(
    sim: SampleTrace,
    names: Sequence[str],
    from_sample: int,
    level: Optional[str] = None,
) -> Tuple[str, int, str]:
    """Pick a (signal, bit, level) whose stuck-at would actually be visible.

    A stuck-at on a bit the simulation never drives to the opposite value
    produces **no mismatch at all** -- the injection would silently prove
    nothing, which is the failure mode this whole directory exists to avoid.  So
    the target must be a bit that the simulation genuinely toggles inside the
    affected range, with no X anywhere in it (an X-masked sample would hide the
    stuck value, one-way).

    Deterministic: signals in sorted order, bits LSB-first, first viable hit.
    """
    n = sim.n_samples
    hi = n - 1
    if from_sample < 0 or from_sample > hi:
        raise HarnessError(
            "--from-sample %d is outside the %d-sample window" % (from_sample, n)
        )
    for name in sorted(names):
        col = sim.values[name]
        for index in range(sim.widths[name]):
            if _has_x(col, from_sample, hi, index):
                continue
            seen = _known_bit_values(col, index, from_sample, hi)
            if seen != {"0", "1"}:
                continue
            if level is None:
                # Stuck LOW is the commoner silicon failure (a driver that
                # cannot pull up, a net tied off), so it is the default.
                return name, index, "0"
            return name, index, level
    raise HarnessError(
        "no bit toggles in samples [%d..%d] without an X in it, so a stuck-at "
        "injection would be invisible and would prove nothing.\n"
        "  Widen the window (later trigger / bigger depth), or pick the target "
        "explicitly with --signal/--bit." % (from_sample, hi)
    )


def inject_stuck(
    hw: SampleTrace,
    name: str,
    bit: int,
    level: str,
    from_sample: int,
) -> Dict[str, object]:
    """Hold one bit of one signal at *level* from *from_sample* to the end."""
    if name not in hw.values:
        raise HarnessError(
            "stuck target %r is not a data signal; available: %s"
            % (name, sorted(hw.values))
        )
    n = hw.n_samples
    if from_sample < 0 or from_sample >= n:
        raise HarnessError("--from-sample %d out of range 0..%d" % (from_sample, n - 1))
    if bit < 0 or bit >= hw.widths[name]:
        raise HarnessError(
            "--bit %d out of range 0..%d for %s" % (bit, hw.widths[name] - 1, name)
        )
    touched = 0
    for i in range(from_sample, n):
        before = hw.values[name][i]
        after = _set_bit(before, bit, level)
        if after != before:
            touched += 1
        hw.values[name][i] = after
    return {
        "mode": "stuck",
        "signal": name,
        "bit": bit,
        "level": level,
        "from_sample": from_sample,
        "samples_altered": touched,
        "summary": "%s bit %d held at %s from sample %d to the end (%d samples "
                   "actually changed)" % (name, bit, level, from_sample, touched),
    }


def choose_skew_signal(sim: SampleTrace, names: Sequence[str]) -> str:
    """The signal a one-sample skew shows up on most clearly.

    A skew is only observable at a *transition*, so pick the signal that
    transitions most.  Ties break alphabetically, so the choice is stable across
    runs (and across machines) rather than "whichever dict order gave us".
    """
    ranked = sorted((-_n_changes(sim.values[n]), n) for n in names)
    if not ranked or -ranked[0][0] == 0:
        raise HarnessError(
            "no signal changes anywhere in the window, so a one-sample skew "
            "would be invisible and would prove nothing."
        )
    return ranked[0][1]


def inject_skew(
    hw: SampleTrace,
    name: str,
    lag: int = 1,
    from_sample: int = 1,
) -> Dict[str, object]:
    """Delay one signal by *lag* samples: ``hw[i] = hw_before[i-lag]``.

    Taken from the **already-quantised hardware** column, not from the raw
    simulation column, so no X is ever introduced (rule 1 in the module
    docstring).  Samples before ``from_sample + lag`` are left alone: the value
    the hardware would have held there comes from outside the window, and this
    harness must never invent data it does not have.
    """
    if name not in hw.values:
        raise HarnessError(
            "skew target %r is not a data signal; available: %s"
            % (name, sorted(hw.values))
        )
    if lag < 1:
        raise HarnessError("--lag must be >= 1, got %d" % lag)
    n = hw.n_samples
    if from_sample < 1 or from_sample >= n:
        raise HarnessError(
            "--from-sample %d out of range 1..%d for a skew" % (from_sample, n - 1)
        )
    before = list(hw.values[name])
    start = max(from_sample, lag)
    touched = 0
    for i in range(start, n):
        new = before[i - lag]
        if new != before[i]:
            touched += 1
        hw.values[name][i] = new
    return {
        "mode": "skew",
        "signal": name,
        "lag": lag,
        "from_sample": start,
        "samples_altered": touched,
        "summary": "%s delayed by %d sample(s) from sample %d onward "
                   "(hw[i] = hw[i-%d]; %d samples actually changed)"
                   % (name, lag, start, lag, touched),
    }


#: How the hardware misbehaves after it stops following the model.
STALL_SHAPES = ("loop", "freeze")

#: Default spin-loop length, in samples, for ``late-divergence``.
LOOP_PERIOD = 4


def inject_late_divergence(
    hw: SampleTrace,
    from_sample: int,
    names: Optional[Sequence[str]] = None,
    shape: str = "loop",
    period: int = LOOP_PERIOD,
) -> Dict[str, object]:
    """Hardware tracks the model up to *from_sample*, then departs for good.

    Two shapes, and which one is the *default* matters:

    ``loop`` (default)
        the hardware repeats the ``period`` samples that preceded the departure,
        for ever.  This is the closer model of this repo's live M0 question: the
        witness tap PROVED the core keeps fetching (``fc`` saturated), so the bus
        is not dead -- it is going round.  ``signals_nanosoc.yaml`` says the same
        thing about what a capture should show ("a loop repeating every N < 1024
        samples appears as a repeating address pattern").

    ``freeze``
        the hardware holds its last value on every signal, for ever -- a total
        stall.  A tail with no value changes at all does not survive the FSDB
        round trip intact (``fsdb2vcd`` stops at the last transition), so
        ``load_hw_trace`` sees fewer timestamps than ``depth``.

        **FIXED 2026-07-30 -- this shape used to make the compare step exit 2.**
        ``crop_trace.sample_grid`` now pins the grid from the FSDB's own end time
        (``fsdb2vcd -summary``, ``max xtag``) instead of from the last value
        change, so the span is *proved* rather than guessed, the tail is held
        forward, and the verdict is an ordinary MISMATCH whose report names the
        stall.  A span the end time cannot prove -- a genuinely truncated file --
        is still exit 2.  Gated by ``compare.sh`` negctl NC5.
    """
    n = hw.n_samples
    if shape not in STALL_SHAPES:
        raise HarnessError(
            "stall shape %r; expected one of %s" % (shape, list(STALL_SHAPES))
        )
    if from_sample < 1 or from_sample >= n:
        raise HarnessError(
            "--from-sample %d out of range 1..%d for late-divergence "
            "(sample 0 has no preceding value to hold)" % (from_sample, n - 1)
        )
    want = sorted(names) if names else sorted(hw.values)
    missing = [x for x in want if x not in hw.values]
    if missing:
        raise HarnessError("late-divergence: unknown signal(s) %s" % missing)

    if shape == "freeze":
        period = 1
    else:
        period = max(1, min(int(period), from_sample))
        if period > 1:
            body_changes = sum(
                _n_changes(hw.values[name][from_sample - period : from_sample])
                for name in want
            )
            if body_changes == 0:
                raise HarnessError(
                    "the %d samples before sample %d do not change on any signal, "
                    "so a spin loop over them is indistinguishable from a freeze "
                    "and would prove nothing.  Move --from-sample, or raise the "
                    "loop period." % (period, from_sample)
                )

    before = {name: list(hw.values[name]) for name in want}
    touched = 0
    for name in want:
        base = from_sample - period
        for i in range(from_sample, n):
            new = before[name][base + ((i - from_sample) % period)]
            if hw.values[name][i] != new:
                touched += 1
            hw.values[name][i] = new
    if shape == "freeze":
        summary = ("all %d data signals frozen at their sample-%d value from "
                   "sample %d to the end (%d signal-samples changed)"
                   % (len(want), from_sample - 1, from_sample, touched))
    else:
        summary = ("all %d data signals repeat samples [%d..%d] cyclically from "
                   "sample %d to the end -- a %d-sample spin loop (%d "
                   "signal-samples changed)"
                   % (len(want), from_sample - period, from_sample - 1,
                      from_sample, period, touched))
    return {
        "mode": "late-divergence",
        "shape": shape,
        "period": period,
        "signals": want,
        "from_sample": from_sample,
        "samples_altered": touched,
        "summary": summary,
    }


def apply_mode(
    mode: str,
    base: Dict[str, object],
    signal: Optional[str] = None,
    bit: Optional[int] = None,
    level: Optional[str] = None,
    lag: int = 1,
    from_sample: Optional[int] = None,
    stall_shape: str = "loop",
    period: int = LOOP_PERIOD,
) -> Dict[str, object]:
    """Dispatch to the mode's injector, filling in the defaults it wants."""
    if mode not in MODES:
        raise HarnessError("unknown mode %r; expected one of %s" % (mode, list(MODES)))
    hw: SampleTrace = base["hw_trace"]        # type: ignore[assignment]
    sim: SampleTrace = base["sim_data"]       # type: ignore[assignment]
    names: List[str] = list(base["names"])    # type: ignore[arg-type]
    trig = int(base["trigger_index"])         # type: ignore[arg-type]
    depth = hw.n_samples

    if mode == "clean":
        return {
            "mode": "clean",
            "summary": "no injection (positive control)",
            "from_sample": None,
            "samples_altered": 0,
        }

    if mode == "stuck":
        # Default: start at the trigger, so the report's "N samples after the
        # trigger" reading is exercised.
        start = trig if from_sample is None else from_sample
        start = min(max(start, 0), depth - 1)
        if signal is None or bit is None:
            signal, bit, chosen = choose_stuck_target(sim, names, start, level)
            level = chosen
        return inject_stuck(hw, signal, int(bit), str(level or "0"), start)

    if mode == "skew":
        # Whole window by default: a sampling-phase error is present from the
        # first cycle, not "from some event onward".
        start = 1 if from_sample is None else from_sample
        if signal is None:
            signal = choose_skew_signal(sim, names)
        return inject_skew(hw, signal, lag=lag, from_sample=start)

    # late-divergence.  A little AFTER the trigger by default, so the report
    # shows clean agreement through the trigger and then a persistent departure
    # -- which is the shape the reading is meant to teach.
    start = trig + max(1, depth // 8) if from_sample is None else from_sample
    start = min(max(start, 1), depth - 1)
    return inject_late_divergence(
        hw, start, names if signal is None else [signal],
        shape=stall_shape, period=period,
    )


# ==========================================================================
# Prediction: what the compare step SHOULD now report
# ==========================================================================

def predict(sim: SampleTrace, hw: SampleTrace, names: Sequence[str]) -> Dict[str, object]:
    """Expected report numbers, computed on the in-memory traces.

    This is the comparator core applied to the data *before* it goes through
    ``write_vcd -> vcd2fsdb -> fsdb2vcd -> parse``.  ``evaluate.sh`` then checks
    the real report against it, which proves the injected divergence survived
    the FSDB round trip, the hardware-path rename and the leaf matching intact.

    It does **not** independently validate the comparison rule itself -- the same
    ``compare_traces`` decides both -- and evaluate.sh says so.  The rule is
    covered by ``tests/test_compare.py`` and by ``make negctl``.
    """
    res = compare_traces(sim, hw, names)
    return {
        "mismatch_samples": res.mismatch_samples,
        "mismatch_bits": res.mismatch_bits,
        "first_diverging_sample": res.first_diverging_sample,
        "first_diverging_signals": list(res.first_diverging_signals),
        "per_signal_mismatch": dict(res.per_signal_mismatch),
        "masked_samples": res.masked_samples,
        "hw_x_samples": res.hw_x_samples,
    }


# ==========================================================================
# Classification: read the SHAPE of a divergence off the two traces
# ==========================================================================

def _lag_score(sim_col: Sequence[str], hw_col: Sequence[str], shift: int) -> Optional[float]:
    """Fraction of samples where ``hw[i] == sim[i-shift]``, sim-X as wildcard.

    ``None`` when there is nothing to score.  A score of 1.0 with a plain
    (shift-0) score below 1.0 is the signature of a sampling-phase error.
    """
    n = len(hw_col)
    lo = max(0, shift)
    hi = n + min(0, shift)
    total = ok = 0
    for i in range(lo, hi):
        bad, _masked, _hw_x = compare_bits(sim_col[i - shift], hw_col[i])
        total += 1
        if not bad:
            ok += 1
    if not total:
        return None
    return float(ok) / float(total)


def classify(
    sim: SampleTrace, hw: SampleTrace, names: Sequence[str]
) -> Dict[str, object]:
    """Describe the divergence's shape, from the compared data alone.

    No knowledge of what was injected: the same function runs unchanged on a real
    capture, where nothing was injected and the shape is all there is.
    """
    res = compare_traces(sim, hw, names)
    n = hw.n_samples
    out: Dict[str, object] = {
        "n_samples": n,
        "signals": sorted(names),
        "mismatch_samples": res.mismatch_samples,
        "first_diverging_sample": res.first_diverging_sample,
        "first_diverging_signals": list(res.first_diverging_signals),
        "per_signal_mismatch": dict(res.per_signal_mismatch),
        "masked_samples": res.masked_samples,
        "masked_fraction": res.masked_fraction,
        "hw_x_samples": res.hw_x_samples,
        "diverging_signals": sorted(k for k, v in res.per_signal_mismatch.items() if v),
        "persistent": False,
        "persist_fraction": 0.0,
        "lag": {},
        "stuck_bits": {},
        "frozen_from": None,
        "frozen_samples": 0,
        "hw_period": None,
        "hw_period_from": None,
    }
    first = res.first_diverging_sample
    if first is None:
        return out

    # --- persistence: does it recover, or does it stay broken? -------------
    diverging_at = []
    for i in range(n):
        hit = False
        for name in names:
            bad, _m, _x = compare_bits(sim.values[name][i], hw.values[name][i])
            if bad:
                hit = True
                break
        diverging_at.append(hit)
    tail = diverging_at[first:]
    out["persist_fraction"] = float(sum(1 for h in tail if h)) / float(len(tail))
    out["persistent"] = bool(tail and all(tail))

    # --- one-sample lag/lead, per diverging signal ------------------------
    for name in list(out["diverging_signals"]):  # type: ignore[arg-type]
        sim_col, hw_col = sim.values[name], hw.values[name]
        scores = {shift: _lag_score(sim_col, hw_col, shift) for shift in (-1, 0, 1)}
        out["lag"][name] = {  # type: ignore[index]
            "aligned": scores[0],
            "hw_lags_sim_by_1": scores[1],
            "hw_leads_sim_by_1": scores[-1],
        }

    # --- stuck bits: an hw bit constant from the divergence to the end ----
    for name in list(out["diverging_signals"]):  # type: ignore[arg-type]
        found = []
        for index in range(hw.widths[name]):
            hw_vals = {_bit(hw.values[name][i], index) for i in range(first, n)}
            sim_vals = _known_bit_values(sim.values[name], index, first, n - 1)
            if len(hw_vals) == 1 and sim_vals == {"0", "1"}:
                found.append({"bit": index, "level": sorted(hw_vals)[0]})
        if found:
            out["stuck_bits"][name] = found  # type: ignore[index]

    # --- freeze: the whole hardware side constant to the end --------------
    last_change = None
    for i in range(1, n):
        if any(hw.values[name][i] != hw.values[name][i - 1] for name in names):
            last_change = i
    if last_change is not None and last_change < n - 1:
        start = last_change + 1
        sim_moves = any(
            sim.values[name][i] != sim.values[name][start - 1]
            for name in names
            for i in range(start, n)
        )
        if sim_moves:
            out["frozen_from"] = start
            out["frozen_samples"] = n - start

    # --- periodicity: is the hardware going ROUND rather than forward? -----
    # The finding signals_nanosoc.yaml is actually hunting: "a loop repeating
    # every N < depth samples appears as a repeating pattern".  Only reported
    # when the SIMULATION does not share the period, otherwise it says nothing
    # about the hardware (an idle bus is periodic on both sides).
    period = _repeat_period(hw, names, first, n)
    if period and period > 1 and not _repeats_with(sim, names, first, n, period):
        out["hw_period"] = period
        out["hw_period_from"] = first
    return out


def _repeats_with(
    trace: SampleTrace, names: Sequence[str], start: int, end: int, period: int
) -> bool:
    """True if every signal satisfies ``v[i] == v[i-period]`` over [start+period, end)."""
    for i in range(start + period, end):
        for name in names:
            if trace.values[name][i] != trace.values[name][i - period]:
                return False
    return True


def _repeat_period(
    trace: SampleTrace, names: Sequence[str], start: int, end: int,
    max_period: int = 32,
) -> Optional[int]:
    """Smallest period the tail [start, end) repeats with, or None.

    Needs at least two whole repetitions before it will claim a period -- one
    repetition is not evidence of anything, and a "loop" claim that rests on a
    single pass is the kind of overclaim this file is written to avoid.
    """
    span = end - start
    for period in range(1, min(max_period, span // 2) + 1):
        if _repeats_with(trace, names, start, end, period):
            return period
    return None


# ==========================================================================
# The reading: honest prose over the classification
# ==========================================================================

def _pct(x: float) -> str:
    return "%.1f%%" % (100.0 * x)


def _wrap_body(text: str, width: int = 72, indent: str = "    ") -> str:
    import textwrap

    lines = textwrap.wrap(" ".join(text.split()), width=width)
    if not lines:
        return ""
    return ("\n" + indent).join(lines)


def interpretation_text(
    shape: Dict[str, object],
    context: Dict[str, object],
    injection: Optional[Dict[str, object]] = None,
) -> str:
    """The short reading printed at the end of `make evaluate`.

    Structure is fixed on purpose: what diverged, where, how far from the
    trigger, what class of bug that pattern is consistent with, and -- always --
    what it cannot distinguish.
    """
    L: List[str] = []
    A = L.append
    trig = context.get("trigger_window_index")
    depth = int(shape.get("n_samples") or 0)
    n_sig = len(shape.get("signals") or [])
    mode = str(context.get("mode", "?"))

    A("=" * 78)
    A("INTERPRETATION  (mode=%s)" % mode)
    A("=" * 78)
    if injection and injection.get("mode") != "clean":
        A("")
        A("  injected:  %s" % _wrap_body(str(injection.get("summary")), 68, "             "))
    first = shape.get("first_diverging_sample")

    A("")
    if first is None:
        A("  NO DIVERGENCE. All %d compared signals agree over all %d samples,"
          % (n_sig, depth))
        A("  under the one-way X rule.")
    else:
        sigs = ", ".join(shape.get("first_diverging_signals") or [])
        A("  FIRST DIVERGENCE    sample %s   on %s" % (first, sigs))
        if isinstance(trig, int) and isinstance(first, int):
            A("  RELATIVE TO TRIGGER %+d samples = %+d sample-clock cycles from the"
              % (first - trig, first - trig))
            A("                      trigger event (trigger at window index %d)" % trig)
        A("  MISMATCHING         %s of %d signal-samples; %d of %d signals affected"
          % (shape.get("mismatch_samples"), depth * n_sig,
             len(shape.get("diverging_signals") or []), n_sig))
        A("  PERSISTENCE         %s of the samples after it also diverge%s"
          % (_pct(float(shape.get("persist_fraction") or 0.0)),
             " -- it never recovers" if shape.get("persistent") else ""))
        per = shape.get("per_signal_mismatch") or {}
        worst = sorted(((-v, k) for k, v in per.items() if v))[:4]
        if worst:
            A("  WORST SIGNALS       %s"
              % ", ".join("%s (%d)" % (k, -v) for v, k in worst))

    # --- what the pattern is consistent with ------------------------------
    A("")
    A("-- consistent with -----------------------------------------------------")
    claims: List[str] = []
    caveats: List[str] = []

    if first is None:
        claims.append(
            "the hardware doing what the model did, for THESE signals over THESE "
            "samples. That is the whole claim."
        )
        caveats.append(
            "a MATCH says nothing about any cycle outside the window. Depth is the "
            "hard limit: %d samples is %d sample-clock cycles and no more" % (depth, depth)
        )
        caveats.append(
            "it also says nothing about a signal that is not in the manifest. "
            "Hardware has ONLY the probed set"
        )
    else:
        stuck = shape.get("stuck_bits") or {}
        frozen_from = shape.get("frozen_from")
        lag = shape.get("lag") or {}
        lagging = [
            k for k, s in lag.items()
            if s.get("hw_lags_sim_by_1") == 1.0 and (s.get("aligned") or 0) < 1.0
        ]
        leading = [
            k for k, s in lag.items()
            if s.get("hw_leads_sim_by_1") == 1.0 and (s.get("aligned") or 0) < 1.0
        ]

        if frozen_from is not None:
            claims.append(
                "A STALL. Every hardware signal is constant from sample %s to the "
                "end (%s samples) while the model keeps changing."
                % (frozen_from, shape.get("frozen_samples"))
            )
            caveats.append(
                "a frozen capture is indistinguishable from a capture that stopped "
                "being written -- a clock that died at the IICE, a sampler that lost "
                "its enable, a truncated read-back. Check the capture's own sample "
                "count and sample_clk before concluding the DUT wedged"
            )
            caveats.append(
                "it localises WHEN progress stopped, never WHY. The signal that "
                "stopped first is a lead, not a cause"
            )
        if lagging:
            claims.append(
                "A ONE-SAMPLE SAMPLING-PHASE ERROR on %s: hw[i] == sim[i-1] on "
                "EVERY sample, while hw[i] == sim[i] does not hold. The values are "
                "right; the cycle they land on is not."
                % ", ".join(sorted(lagging))
            )
            caveats.append(
                "which side is off by one is NOT determined. A missing synchroniser "
                "stage in the DUT, an IICE capture on the wrong clock edge, and a "
                "shadow mirror registered one stage too deep all produce exactly "
                "this. Rule the harness out first -- the mirror's own one-sample "
                "offset is INTERFACES.md §3.2"
            )
        if leading:
            claims.append(
                "the hardware LEADING the model by one sample on %s "
                "(hw[i] == sim[i+1]) -- the same class, opposite sign"
                % ", ".join(sorted(leading))
            )
        period = shape.get("hw_period")
        if period:
            claims.append(
                "THE HARDWARE GOING ROUND, NOT FORWARD. From sample %s the "
                "hardware side repeats every %s samples on every signal, and the "
                "model does not. That is what a spin loop shorter than the window "
                "looks like -- the address pattern inside those %s samples is the "
                "loop body."
                % (shape.get("hw_period_from"), period, period)
            )
            caveats.append(
                "a %s-sample repeat ON THE PROBED SET is not a %s-cycle "
                "instruction loop: a longer loop whose probed signals happen to "
                "repeat looks identical, and a loop LONGER than the window shows "
                "up as no period at all, just a ramp" % (period, period)
            )
        if stuck and not lagging and frozen_from is None and not period:
            bits = "; ".join(
                "%s bit%s %s stuck at %s" % (
                    k, "" if len(v) == 1 else "s",
                    ",".join(str(b["bit"]) for b in v),
                    ",".join(sorted({b["level"] for b in v})),
                )
                for k, v in sorted(stuck.items())
            )
            claims.append(
                "A STUCK NET: %s, from sample %s to the end, while the model drives "
                "both values there." % (bits, first)
            )
            caveats.append(
                "stuck AT THE PROBE. A stuck DUT net, a stuck pad, a level shifter "
                "that cannot drive (this repo's SH0 bank) and a broken IICE probe "
                "route on that one bit all read identically here"
            )
        if not claims:
            claims.append(
                "a value divergence with no clean signature: not a whole-signal "
                "one-cycle lag, not a bit stuck to the end, not a stall."
            )
            caveats.append(
                "with no signature the tooling gives you a signal and a cycle and "
                "stops. That is still two things you did not have, but do not read "
                "a cause into it"
            )
        if len(shape.get("diverging_signals") or []) > 1 and frozen_from is None:
            caveats.append(
                "%d signals diverge, and the FIRST is not necessarily the cause -- "
                "on a bus they are usually all downstream of one event"
                % len(shape.get("diverging_signals") or [])
            )

    for c in claims:
        A("  * %s" % _wrap_body(c))
    A("")
    A("-- what this CANNOT tell you -------------------------------------------")
    for c in caveats:
        A("  * %s" % _wrap_body(c))
    masked = float(shape.get("masked_fraction") or 0.0)
    A("  * %s" % _wrap_body(
        "%s of compared samples were X-masked (sim X vs hardware 0/1, don't-care "
        "one way). %s" % (
            _pct(masked),
            "Low enough to ignore." if masked < 0.10 else
            "HIGH -- those samples were not really compared, so this run is weak "
            "whatever the verdict says.")))
    if shape.get("hw_x_samples"):
        A("  * %s" % _wrap_body(
            "%d mismatches were caused by X ON THE HARDWARE SIDE. Silicon cannot "
            "produce X: suspect the capture or the FSDB loader before the DUT."
            % shape.get("hw_x_samples")))
    A("")
    A("-- the question to ask yourself ----------------------------------------")
    if first is None:
        A("  Was the window pointed at the failure at all? A MATCH over the wrong")
        A("  %d cycles is the cheapest way to waste a board slot." % depth)
    else:
        A("  Given a signal and a cycle offset from a trigger you chose, can you now")
        A("  name the next thing to probe? If yes, the tooling did its job. If the")
        A("  answer needs a signal that is not in the manifest, then the finding is")
        A("  'my probe set is wrong' -- edit signals_*.yaml and capture again.")
    A("=" * 78)
    return "\n".join(L) + "\n"


# ==========================================================================
# nWave artifacts: two aligned FSDBs, a mismatch overlay, and the joined file
#
# This lives here rather than in evaluate.sh for one reason: a heredoc of Python
# inside a shell script is unreachable from pytest.  Everything in this file is
# the *Python half of the evaluate workflow*, injection included.
# ==========================================================================

#: Scope names inside the viewing artifacts.  Short on purpose -- they are what
#: you read in nWave's signal pane, next to each other.
SCOPE_SIM = "sim"
SCOPE_HW = "hw"
SCOPE_DIFF = "diff"


def _emit_side(
    trace: SampleTrace, scope: str, out_fsdb: str, logdir: str, tag: str
) -> str:
    """Write one SampleTrace as a sample-indexed FSDB under a single scope.

    Time stamp == sample index, on BOTH sides.  That is the whole point: the sim
    FSDB's real time base (ns, and minutes long) and a hardware capture's
    arbitrary trigger-relative base cannot be laid over each other, but their
    sample indices can (INTERFACES.md §0/§4, plan §5.1).  A cursor at t=N in
    nWave is therefore sample N on both sides.
    """
    names = sorted(trace.values)
    signals = [("%s/%s" % (scope, n), trace.widths[n]) for n in names]
    frames = [
        {"%s/%s" % (scope, n): trace.values[n][i] for n in names}
        for i in range(trace.n_samples)
    ]
    vcd = os.path.join(logdir, "%s.vcd" % tag)
    fsdb_tools.write_vcd(vcd, signals, frames, version="identify_iice evaluate %s" % tag)
    return fsdb_tools.vcd2fsdb(vcd, out_fsdb, logdir)


def mismatch_overlay(
    sim: SampleTrace, hw: SampleTrace, names: Sequence[str]
) -> SampleTrace:
    """A 1-bit-per-signal "diverges here" trace, plus ``any_neq``.

    Uses ``compare_bits``, so it carries the one-way X rule rather than a second
    opinion about it: a sample the comparator masked reads 0 here too.  This is
    the signal you actually put a cursor on in nWave -- scrolling 1024 samples of
    a 32-bit bus by eye is how you miss the divergence you came to find.
    """
    want = sorted(names)
    n = hw.n_samples
    widths: Dict[str, int] = {}
    values: Dict[str, List[str]] = {}
    for name in want:
        widths["%s_neq" % name] = 1
        values["%s_neq" % name] = []
    widths["any_neq"] = 1
    values["any_neq"] = []
    for i in range(n):
        any_bad = False
        for name in want:
            bad, _m, _x = compare_bits(sim.values[name][i], hw.values[name][i])
            values["%s_neq" % name].append("1" if bad else "0")
            any_bad = any_bad or bool(bad)
        values["any_neq"].append("1" if any_bad else "0")
    return SampleTrace(widths, values, {"source": "mismatch_overlay"})


def write_virtual_fsdb(path: str, members: Sequence[str]) -> str:
    """Write a Verdi *virtual* FSDB (``.vf``) naming *members*.

    Format copied from the vendor's own
    ``$VERDI_HOME/demo/hwsw_debug/multi_core/VirtualFile.vf``, which stitches an
    RTL FSDB and a software-trace FSDB into one nWave view -- exactly the shape
    of a sim-vs-hardware comparison.  Absolute member paths, so the file works
    from any CWD.
    """
    if len(members) < 2:
        raise HarnessError("a virtual FSDB wants at least 2 member files")
    lines = [
        "@FSDB rc file Version 1.0",
        "[VRTL_FILE_HEADER]",
        "# !! DO NOT EDIT [VRTL_FILE_HEADER] SESSION !!",
        "Version = 1",
        "[VRTL_FILE_SOURCE]",
        "FileType = stitch",
    ]
    for i, member in enumerate(members, start=1):
        lines.append("File%d = %s" % (i, os.path.abspath(member)))
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def join_fsdb(joined: str, vf: str, logdir: str) -> Dict[str, object]:
    """``fsdbjoin -c`` the virtual file into one self-contained joined FSDB.

    Three measured facts drive this shape:

    * ``fsdbjoin -a <new file>`` FAILS (exit 255, "Failed to execute command")
      under BOTH Verdi vintages here -- ``-a`` appends to an *existing* joined
      file.  The creation path is ``-c`` from a virtual file, which is why
      :func:`write_virtual_fsdb` exists at all.
    * the result is self-contained: ``fsdbjoin -f`` shows the ``.vf`` plus a full
      copy of every member, so the ``.jf`` can be copied to another machine.
    * ``fsdb2vcd`` REFUSES a joined file ("is a virtual file and is not supported
      by fsdb2vcd"), so the joined file is a *viewing* artifact only.  Everything
      machine-checkable reads the real member FSDBs.

    Verified structurally, not visually: launching nWave needs a GUI licence seat
    and a DISPLAY, which this harness deliberately never takes.  So the members
    are extracted again and re-read, which is the strongest board-free evidence
    available that the container holds what it claims.
    """
    if os.path.exists(joined):
        os.remove(joined)
    # fsdb_tools._run is used deliberately: it locates the tool without
    # realpath()ing the wrapper symlink, confines <tool>Log/ to logdir, and
    # raises on a non-zero exit.  Re-implementing that here would re-introduce
    # two traps INTERFACES.md §6 already records.
    fsdb_tools._run("fsdbjoin", ["-c", os.path.abspath(joined), "-i",
                                 os.path.abspath(vf)], logdir)
    if not os.path.isfile(joined):
        raise HarnessError("fsdbjoin -c wrote no joined FSDB at %s" % joined)
    info = fsdb_tools._run("fsdbjoin", ["-f", os.path.abspath(joined)], logdir)
    contained = None
    for line in info.splitlines():
        if "Number of contained files" in line:
            try:
                contained = int(line.split(":")[-1].strip())
            except ValueError:
                pass
    return {"joined": joined, "contained_files": contained}


def verify_joined(joined: str, index: int, logdir: str) -> List[str]:
    """Extract member *index* of a joined FSDB and list its signals again.

    The board-free integrity check for the viewing artifact: if the extracted
    member reads back with the leaf set we put in, the container is sound.
    """
    out = os.path.join(logdir, "extracted_%d.fsdb" % index)
    if os.path.exists(out):
        os.remove(out)
    fsdb_tools._run(
        "fsdbjoin",
        ["-e", os.path.abspath(joined), "-n", str(index), "-o", os.path.abspath(out)],
        logdir,
    )
    if not os.path.isfile(out):
        raise HarnessError(
            "fsdbjoin -e could not extract member %d of %s" % (index, joined)
        )
    return sorted(fsdb_tools.list_signals(out, logdir))


# ==========================================================================
# Report cross-check (the numbers evaluate.sh asserts on)
# ==========================================================================

_REPORT_KEYS = (
    ("verdict", r"^VERDICT:\s+(\S+)"),
    ("mismatch_samples", r"^\s*MISMATCH COUNT \(signal-samples\)\s+(\d+)"),
    ("first_diverging_sample", r"^\s*FIRST DIVERGING SAMPLE\s+(\S+)"),
    # NOTE the escaped literal: a lazy `[^\d]*` here would stop at the `0` in
    # "(sim X vs hw 0/1)" and report a masked count of 0 on every report.
    ("masked_samples", r"^\s*X-masked samples \(sim X vs hw 0/1\)\s+(\d+)"),
    ("hw_x_samples", r"^\s*mismatches caused by hardware X/Z\s+(\d+)"),
)


def parse_report(path: str) -> Dict[str, object]:
    """Pull the numbers INTERFACES.md §4 mandates out of ``compare_report.txt``.

    Parsed rather than recomputed on purpose: the point of the cross-check is
    that the *report the user reads* says what the injection put in.  A report
    that is right in memory and wrong on disk is still a broken report.
    """
    import re

    if not os.path.isfile(path):
        raise HarnessError("compare report not found: %s" % path)
    with open(path) as fh:
        text = fh.read()
    out: Dict[str, object] = {}
    for key, pattern in _REPORT_KEYS:
        m = re.search(pattern, text, re.M)
        if m is None:
            continue
        raw = m.group(1)
        if key == "verdict":
            out[key] = raw
        elif raw == "(none)":
            out[key] = None
        else:
            out[key] = int(raw)
    if "verdict" not in out:
        raise HarnessError(
            "no 'VERDICT:' line in %s -- that is not a compare report" % path
        )
    return out


def check_report_against_injection(
    record: Dict[str, object], report: Dict[str, object]
) -> List[str]:
    """Problems found comparing a written report against an injection record."""
    expect = dict(record["expect"])  # type: ignore[arg-type]
    problems: List[str] = []
    want_verdict = "MATCH" if not expect["mismatch_samples"] else "MISMATCH"
    if report.get("verdict") != want_verdict:
        problems.append(
            "verdict is %r, expected %r" % (report.get("verdict"), want_verdict)
        )
    for key in ("mismatch_samples", "first_diverging_sample"):
        if key in report and report[key] != expect[key]:
            problems.append(
                "%s: the report says %r, the injection predicts %r"
                % (key, report[key], expect[key])
            )
    return problems


# ==========================================================================
# CLI
# ==========================================================================

def _cmd_inject(args: argparse.Namespace) -> int:
    base = build_baseline(
        args.sim_fsdb, args.signal_map, args.manifest, args.logdir,
        x_mode=args.x_mode, x_seed=args.x_seed,
    )
    injection = apply_mode(
        args.mode, base,
        signal=args.signal, bit=args.bit, level=args.level,
        lag=args.lag, from_sample=args.from_sample,
        stall_shape=args.stall_shape, period=args.loop_period,
    )
    hw: SampleTrace = base["hw_trace"]     # type: ignore[assignment]
    sim: SampleTrace = base["sim_data"]    # type: ignore[assignment]
    names = list(base["names"])            # type: ignore[arg-type]

    out = synth_hw_fsdb.write_hw_fsdb(
        hw, base["rows"], names, args.out, args.logdir  # type: ignore[arg-type]
    )
    # PROVENANCE: this is the third producer of a "hardware" FSDB in this
    # directory (with synth_hw_fsdb.py and crop_trace.normalise_hw_capture), and
    # the claim "every artifact this harness writes is bound to a manifest" is
    # only true if all three stamp. Origin says `synthetic` and no static_id/rm_id
    # is claimed: an injected trace never went near a board.
    identity = provenance.manifest_identity(args.manifest)
    provenance.check_signal_map(identity, args.signal_map)
    stamp_file = provenance.write_stamp(out, provenance.build_stamp(
        identity, out, origin=provenance.ORIGIN_SYNTHETIC,
        produced_by="inject_divergence.py",
        capture={"n_samples": hw.n_samples, "signals": sorted(names),
                 "grid_source": "one timestamp per sample (written by this tool)",
                 "injected_mode": args.mode},
    ))
    expect = predict(sim, hw, names)
    shape = classify(sim, hw, names)
    info = dict(base["window_info"])       # type: ignore[arg-type]
    record = {
        "mode": args.mode,
        "injection": injection,
        "expect": expect,
        "shape": shape,
        "window_info": info,
        "trigger_window_index": base["trigger_index"],
        "x_quantised_bits": base["x_quantised_bits"],
        "sim_scope": base["sim_scope"],
        "hw_fsdb": out,
        "depth": hw.n_samples,
        "signals": names,
    }
    if args.json:
        directory = os.path.dirname(os.path.abspath(args.json))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.json, "w") as fh:
            json.dump(record, fh, indent=1, sort_keys=True)
    if args.sim_window_json:
        base["sim_window_full"].save(args.sim_window_json)  # type: ignore[union-attr]

    print("inject_divergence: mode=%s" % args.mode)
    print("inject_divergence: %s" % injection.get("summary"))
    print("inject_divergence: window [%d..%d], trigger at window index %d, depth %d"
          % (info["window_start_sample"], info["window_end_sample"],
             base["trigger_index"], hw.n_samples))
    print("inject_divergence: expect MISMATCH COUNT %d, FIRST DIVERGING SAMPLE %s"
          % (expect["mismatch_samples"], expect["first_diverging_sample"]))
    print("inject_divergence: wrote %s" % out)
    print("inject_divergence: provenance stamp %s (probe set %s)"
          % (stamp_file, provenance.short_digest(identity["signal_set_sha256"])))
    return EXIT_MATCH


def _cmd_interpret(args: argparse.Namespace) -> int:
    sim_full = SampleTrace.load(args.sim_window)
    hw = SampleTrace.load(args.hw_window)
    names = sorted(set(sim_full.values) & set(hw.values))
    if not names:
        raise HarnessError(
            "the two window JSONs share no signal names: %s vs %s"
            % (sorted(sim_full.values), sorted(hw.values))
        )
    sim = sim_full.subset(names)
    shape = classify(sim, hw, names)

    injection = None
    context: Dict[str, object] = {"mode": args.mode or "?"}
    if args.injection and os.path.isfile(args.injection):
        with open(args.injection) as fh:
            record = json.load(fh)
        injection = record.get("injection")
        context["mode"] = record.get("mode", context["mode"])
        context["trigger_window_index"] = record.get("trigger_window_index")
    if args.trigger_index is not None:
        context["trigger_window_index"] = args.trigger_index

    text = interpretation_text(shape, context, injection)
    if args.out:
        directory = os.path.dirname(os.path.abspath(args.out))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w") as fh:
            fh.write(text)
    sys.stdout.write(text)
    return EXIT_MATCH


def _cmd_waves(args: argparse.Namespace) -> int:
    sim_full = SampleTrace.load(args.sim_window)
    hw = SampleTrace.load(args.hw_window)
    names = sorted(set(sim_full.values) & set(hw.values))
    if not names:
        raise HarnessError(
            "the two window JSONs share no signal names: %s vs %s"
            % (sorted(sim_full.values), sorted(hw.values))
        )
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.logdir, exist_ok=True)

    sim_fsdb = _emit_side(
        sim_full, SCOPE_SIM, os.path.join(args.outdir, "sim_window.fsdb"),
        args.logdir, "wave_sim")
    hw_fsdb = _emit_side(
        hw, SCOPE_HW, os.path.join(args.outdir, "hw_window.fsdb"),
        args.logdir, "wave_hw")
    diff = mismatch_overlay(sim_full.subset(names), hw, names)
    diff_fsdb = _emit_side(
        diff, SCOPE_DIFF, os.path.join(args.outdir, "mismatch.fsdb"),
        args.logdir, "wave_diff")

    members = [sim_fsdb, hw_fsdb, diff_fsdb]
    vf = write_virtual_fsdb(
        os.path.join(args.outdir, "iice_%s.vf" % args.tag), members)
    print("waves: sim  %s   (scope %s/, %d signals, %d samples)"
          % (sim_fsdb, SCOPE_SIM, len(sim_full.values), sim_full.n_samples))
    print("waves: hw   %s   (scope %s/, %d signals, %d samples)"
          % (hw_fsdb, SCOPE_HW, len(hw.values), hw.n_samples))
    print("waves: diff %s   (scope %s/, one _neq bit per signal + any_neq)"
          % (diff_fsdb, SCOPE_DIFF))
    print("waves: virtual FSDB %s" % vf)

    if args.no_join:
        print("waves: fsdbjoin SKIPPED (--no-join)")
        return EXIT_MATCH
    joined = os.path.join(args.outdir, "iice_%s.jf" % args.tag)
    info = join_fsdb(joined, vf, args.logdir)
    print("waves: joined FSDB  %s  (%s contained files: the .vf + %d members)"
          % (joined, info["contained_files"], len(members)))
    # Integrity, board-free and GUI-free: pull a member back out and re-read it.
    leaves = verify_joined(joined, 1, args.logdir)
    want = sorted("%s/%s" % (SCOPE_SIM, n) for n in sim_full.values)
    if leaves != want:
        raise HarnessError(
            "the joined FSDB's member 1 read back with the wrong signal set.\n"
            "  got:      %s\n  expected: %s" % (leaves, want)
        )
    print("waves: joined-file integrity OK -- member 1 extracts and re-reads "
          "with all %d sim signals" % len(want))
    return EXIT_MATCH


def _cmd_check(args: argparse.Namespace) -> int:
    """Assert the written report says what the injection put in.

    Exit 1 -- not 2 -- when they disagree: the harness ran fine, it is the
    *usefulness* claim that failed, and `make evaluate` reserves 2 for "could
    not run".
    """
    with open(args.injection) as fh:
        record = json.load(fh)
    got = parse_report(args.report)
    expect = record["expect"]
    problems = check_report_against_injection(record, got)

    print("evaluate-check: mode=%s" % record.get("mode"))
    print("evaluate-check: report verdict           %s (want %s)"
          % (got.get("verdict"),
             "MATCH" if not expect["mismatch_samples"] else "MISMATCH"))
    print("evaluate-check: report mismatch count    %s (injection predicts %s)"
          % (got.get("mismatch_samples"), expect["mismatch_samples"]))
    print("evaluate-check: report first divergence  %s (injection predicts %s)"
          % (got.get("first_diverging_sample"), expect["first_diverging_sample"]))
    if problems:
        print("")
        print("EVALUATE CHECK FAILED:")
        for p in problems:
            print("  - %s" % p)
        print("")
        print("  The injected divergence did not reach the report intact. That is a")
        print("  defect BETWEEN the injector and the report -- the FSDB round trip,")
        print("  the hardware-path rename, leaf matching, or the window crop -- and")
        print("  NOT in the comparison rule, which decided both numbers. Do not")
        print("  trust a compare report from this tree until it is fixed.")
        return 1
    print("evaluate-check: the injected divergence survived the FSDB round trip, "
          "the rename and the crop intact.")
    return EXIT_MATCH


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="inject a realistic hardware divergence / read the result",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="modes:\n" + "\n".join(
            "  %-17s %s" % (m, MODE_BLURB[m]) for m in MODES
        ),
    )
    sub = ap.add_subparsers(dest="cmd")

    inj = sub.add_parser("inject", help="write a perturbed 'hardware' FSDB")
    inj.add_argument("--mode", required=True, choices=list(MODES))
    inj.add_argument("--sim-fsdb", required=True)
    inj.add_argument("--signal-map", required=True)
    inj.add_argument("--manifest", required=True)
    inj.add_argument("--out", required=True, help="output hardware FSDB")
    inj.add_argument("--logdir", required=True)
    inj.add_argument("--json", help="write the injection record here")
    inj.add_argument("--sim-window-json", help="also save the cropped sim window")
    inj.add_argument("--signal", help="override the target signal")
    inj.add_argument("--bit", type=int, help="override the target bit (stuck)")
    inj.add_argument("--level", choices=["0", "1"], help="stuck level")
    inj.add_argument("--lag", type=int, default=1, help="skew, in samples")
    inj.add_argument("--from-sample", type=int, help="window index to start at")
    inj.add_argument("--stall-shape", default="loop", choices=list(STALL_SHAPES),
                     help="late-divergence: spin LOOP (default, the better model "
                          "of this repo's live M0 question) or total FREEZE")
    inj.add_argument("--loop-period", type=int, default=LOOP_PERIOD,
                     help="late-divergence loop length, in samples")
    inj.add_argument("--x-mode", default="prng", choices=list(synth_hw_fsdb.X_MODES))
    inj.add_argument("--x-seed", default="iice")
    inj.set_defaults(func=_cmd_inject)

    itp = sub.add_parser("interpret", help="read a compare result out loud")
    itp.add_argument("--sim-window", required=True, help="sim_window.json")
    itp.add_argument("--hw-window", required=True, help="hw_window.json")
    itp.add_argument("--injection", help="the injection record, if there was one")
    itp.add_argument("--mode", help="label for the header")
    itp.add_argument("--trigger-index", type=int)
    itp.add_argument("--out", help="also write the text here")
    itp.set_defaults(func=_cmd_interpret)

    wav = sub.add_parser("waves", help="emit the nWave viewing artifacts")
    wav.add_argument("--sim-window", required=True, help="sim_window.json")
    wav.add_argument("--hw-window", required=True, help="hw_window.json")
    wav.add_argument("--outdir", required=True)
    wav.add_argument("--logdir", required=True)
    wav.add_argument("--tag", default="evaluate", help="artifact name stem")
    wav.add_argument("--no-join", action="store_true",
                     help="skip fsdbjoin; leave the two real FSDBs + the .vf")
    wav.set_defaults(func=_cmd_waves)

    chk = sub.add_parser("check", help="assert the report matches the injection")
    chk.add_argument("--injection", required=True)
    chk.add_argument("--report", required=True)
    chk.set_defaults(func=_cmd_check)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return EXIT_HARNESS
    try:
        return args.func(args)
    except HarnessError as exc:
        print("HARNESS ERROR (inject_divergence): %s" % exc, file=sys.stderr)
        return EXIT_HARNESS


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
