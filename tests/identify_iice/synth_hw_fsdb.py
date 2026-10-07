#!/usr/bin/env python3
"""PHASE 0 ONLY: derive a synthetic "hardware" FSDB from a simulation FSDB.

Stream C (compare pipeline).  Plan §4 Phase 0 step 4; INTERFACES.md §4.

This exists so the entire comparison methodology can be proven with **no board,
no bitstream and no Identify licence**.  It stands in for silicon by being
*deliberately* nearly identical to the simulation, then the negative control
(``--perturb``) proves the comparator can still fail.

What it does, and why each step matters:

1. **Crop to the IICE window** using the same trigger-relative maths as the
   compare step, so the synthetic capture is ``depth`` samples positioned like a
   real IICE buffer.
2. **Keep only the hardware-visible signal set** -- the manifest data signals.
   A real Identify capture has no ``sample_clk`` (the sample clock *defines* the
   sample domain, it is not sampled) and no ``trigger_marker`` (marked
   ``__DERIVED__`` in ``signal_map.tsv``).  Dropping them here is what makes the
   phase-0 set-equality precheck a real check rather than a formality.
3. **Quantise X/Z to 0/1.**  Hardware cannot produce X.  The default mode is a
   deterministic per-(signal,sample,bit) pseudo-random choice, *not* a constant:
   with a constant fill an accidentally-broken X-mask could still pass by luck,
   whereas a pseudo-random fill makes the one-way X don't-care rule genuinely
   load-bearing.
4. **Rename into the hardware paths** from ``signal_map.tsv``, so the compare
   step's "mangle back" is genuinely exercised instead of being an identity
   no-op.

``--perturb``/``--perturb-auto`` flip exactly one bit.  That is the negative
control, and it is the most important thing in this directory: a comparator that
cannot fail is worse than no comparator.  A perturbation is refused if the
targeted simulation bit is X or Z, because the one-way don't-care rule would
legitimately mask it and the "negative control" would silently prove nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fsdb_tools  # noqa: E402
import provenance  # noqa: E402
from crop_trace import (  # noqa: E402
    SampleTrace,
    SignalMapRow,
    crop_sim_window,
    data_signal_names,
    load_sim_trace,
    read_manifest_iice,
    read_signal_map,
    resolve_sim_scope,
    trigger_offset,
)
from fsdb_tools import EXIT_HARNESS, EXIT_MATCH, HarnessError  # noqa: E402

X_MODES = ("prng", "zero", "one")


def quantise_bit(mode: str, seed: str, signal: str, sample: int, bit: int) -> str:
    """Choose the 0/1 a "hardware" bit takes where the simulation had X or Z."""
    if mode == "zero":
        return "0"
    if mode == "one":
        return "1"
    if mode != "prng":
        raise HarnessError("--x-mode=%r, expected one of %s" % (mode, list(X_MODES)))
    key = "%s|%s|%d|%d" % (seed, signal, sample, bit)
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return "1" if digest[0] & 1 else "0"


def quantise_trace(
    trace: SampleTrace, mode: str = "prng", seed: str = "iice"
) -> Tuple[SampleTrace, int]:
    """Replace every X/Z with 0/1.  Returns ``(new_trace, n_bits_quantised)``."""
    out: Dict[str, List[str]] = {}
    count = 0
    for name in sorted(trace.values):
        width = trace.widths[name]
        col: List[str] = []
        for i, bits in enumerate(trace.values[name]):
            if set(bits) & set("xz"):
                chars = []
                for pos, ch in enumerate(bits):
                    if ch in "xz":
                        bit = width - 1 - pos  # LSB-first bit index
                        chars.append(quantise_bit(mode, seed, name, i, bit))
                        count += 1
                    else:
                        chars.append(ch)
                col.append("".join(chars))
            else:
                col.append(bits)
        out[name] = col
    meta = dict(trace.meta)
    meta["x_quantised_bits"] = count
    meta["x_mode"] = mode
    return SampleTrace(trace.widths, out, meta), count


def flip_bit(bits: str, bit: int) -> str:
    """Flip LSB-first bit index *bit* of a bitstring."""
    width = len(bits)
    if bit < 0 or bit >= width:
        raise HarnessError("bit %d out of range for width %d" % (bit, width))
    pos = width - 1 - bit
    ch = bits[pos]
    if ch not in "01":
        raise HarnessError("cannot flip a %r bit" % ch)
    return bits[:pos] + ("0" if ch == "1" else "1") + bits[pos + 1 :]


def parse_perturb(spec: str) -> Tuple[str, int, int]:
    parts = spec.split(":")
    if len(parts) != 3:
        raise HarnessError("--perturb expects <signal>:<sample>:<bit>, got %r" % spec)
    name, sample, bit = parts
    try:
        return name, int(sample), int(bit)
    except ValueError:
        raise HarnessError("--perturb sample/bit must be integers, got %r" % spec)


def choose_perturb_target(
    sim_window: SampleTrace, names: Sequence[str], prefer_from: int = 0
) -> Tuple[str, int, int]:
    """Deterministically pick a bit that is 0/1 in the simulation.

    Scans from *prefer_from* (the trigger's window index by default) so the
    negative control lands on real post-trigger data rather than on reset X,
    then falls back to scanning the whole window.  A target whose simulation bit
    is X/Z is never chosen: the one-way don't-care rule would mask the flip and
    the negative control would pass while proving nothing.
    """
    n = sim_window.n_samples
    order = list(range(prefer_from, n)) + list(range(0, min(prefer_from, n)))
    for i in order:
        for name in sorted(names):
            bits = sim_window.values[name][i]
            width = len(bits)
            for pos, ch in enumerate(bits):
                if ch in "01":
                    return name, i, width - 1 - pos
    raise HarnessError(
        "no 0/1 bit anywhere in the %d-sample window: every bit is X/Z, so no "
        "meaningful perturbation exists and the negative control cannot run.\n"
        "  This means the simulation trace is unusable, not that it matched." % n
    )


def inject_hw_x(
    hw_trace: SampleTrace, sim_window: SampleTrace, name: str, sample: int, bit: int
) -> str:
    """Put an X on the HARDWARE side -- the dangerous direction of the X rule.

    The don't-care is one-way: sim-X is ignored, hardware-X must be a mismatch,
    because real hardware cannot produce X and an X there means the capture or
    the loader is broken.  If that direction ever silently masked, a broken
    capture would read green.  Used by ``negctl`` NC4 so the rule is proven in
    the real FSDB pipeline even when the simulation trace happens to contain no
    X at all (the selftest DUT does not).
    """
    if name not in hw_trace.values:
        raise HarnessError("--inject-hw-x signal %r is not in the hardware trace" % name)
    if sample < 0 or sample >= hw_trace.n_samples:
        raise HarnessError(
            "--inject-hw-x sample %d out of range 0..%d" % (sample, hw_trace.n_samples - 1)
        )
    bits = hw_trace.values[name][sample]
    width = len(bits)
    if bit < 0 or bit >= width:
        raise HarnessError("--inject-hw-x bit %d out of range 0..%d" % (bit, width - 1))
    sim_ch = sim_window.values[name][sample][width - 1 - bit]
    if sim_ch not in "01":
        raise HarnessError(
            "refusing to inject a hardware X at %s[%d] bit %d: the SIMULATION "
            "value there is %r, so the one-way rule masks it and nothing is "
            "proven." % (name, sample, bit, sim_ch)
        )
    pos = width - 1 - bit
    after = bits[:pos] + "x" + bits[pos + 1 :]
    hw_trace.values[name][sample] = after
    hw_trace.meta["hw_x_injected"] = "%s:%d:%d" % (name, sample, bit)
    return "%s sample %d bit %d: %s -> %s (sim had %s)" % (
        name, sample, bit, bits, after, sim_ch)


def freeze_tail(hw_trace: SampleTrace, sim_window: SampleTrace, from_sample: int) -> str:
    """Hold EVERY hardware signal at its sample ``from_sample - 1`` value to the end.

    The wedged-DUT shape, and a negative control in its own right (``negctl``
    NC5).  A tail with no value change at all is the case ``fsdb2vcd`` cannot
    round-trip -- it "stops the VCD at the last transition", so those timestamps
    never come back and the loader sees fewer timestamps than the manifest depth.
    That used to be reported as a HARNESS ERROR (exit 2) naming the symptom.  The
    grid is now pinned from the FSDB's own end time, so the tail is held forward
    and the verdict is an ordinary MISMATCH (exit 1) whose text names the stall.

    Refused unless the frozen value actually differs from the simulation
    somewhere in the tail: a freeze that the simulation happens to match would
    make the control pass while proving nothing (same reasoning as
    ``apply_perturbation``'s X refusal).
    """
    n = hw_trace.n_samples
    if from_sample < 1 or from_sample >= n:
        raise HarnessError(
            "--freeze-tail-from %d out of range 1..%d (sample 0 has no preceding "
            "value to hold)" % (from_sample, n - 1)
        )
    held = {name: hw_trace.values[name][from_sample - 1] for name in hw_trace.values}
    differs = 0
    for name, value in held.items():
        for i in range(from_sample, n):
            hw_trace.values[name][i] = value
            sim_bits = sim_window.values[name][i]
            if any(
                s in "01" and s != h
                for s, h in zip(sim_bits, value)
            ):
                differs += 1
    if differs == 0:
        raise HarnessError(
            "refusing to freeze the tail from sample %d: the simulation matches "
            "the frozen value on every signal for every remaining sample, so the "
            "comparison would legitimately report MATCH and the negative control "
            "would prove nothing.  Pick an earlier --freeze-tail-from." % from_sample
        )
    hw_trace.meta["frozen_tail_from"] = from_sample
    return ("FROZEN TAIL all %d signals held at their sample-%d value from sample "
            "%d to %d (%d signal-samples differ from the simulation)"
            % (len(held), from_sample - 1, from_sample, n - 1, differs))


def apply_perturbation(
    hw_trace: SampleTrace,
    sim_window: SampleTrace,
    name: str,
    sample: int,
    bit: int,
) -> str:
    """Flip one bit of the hardware trace, refusing masked targets."""
    if name not in hw_trace.values:
        raise HarnessError(
            "--perturb signal %r is not in the hardware trace; available: %s"
            % (name, sorted(hw_trace.values))
        )
    if sample < 0 or sample >= hw_trace.n_samples:
        raise HarnessError(
            "--perturb sample %d out of range 0..%d" % (sample, hw_trace.n_samples - 1)
        )
    sim_bits = sim_window.values[name][sample]
    width = len(sim_bits)
    if bit < 0 or bit >= width:
        raise HarnessError("--perturb bit %d out of range 0..%d for %s" % (bit, width - 1, name))
    sim_ch = sim_bits[width - 1 - bit]
    if sim_ch not in "01":
        raise HarnessError(
            "refusing to perturb %s[%d] bit %d: the SIMULATION value there is %r, "
            "so the one-way X don't-care rule would mask the flip and the "
            "negative control would pass while proving nothing.\n"
            "  Pick a different bit, or use --perturb-auto." % (name, sample, bit, sim_ch)
        )
    before = hw_trace.values[name][sample]
    after = flip_bit(before, bit)
    hw_trace.values[name][sample] = after
    hw_trace.meta["perturbed"] = "%s:%d:%d" % (name, sample, bit)
    return "%s sample %d bit %d: %s -> %s (sim had %s)" % (
        name, sample, bit, before, after, sim_bits)


def build_hw_trace(
    sim_fsdb: str,
    signal_map: str,
    manifest: str,
    logdir: str,
    x_mode: str = "prng",
    x_seed: str = "iice",
    perturb: Optional[str] = None,
    perturb_auto: bool = False,
    drop_signal: Optional[str] = None,
    inject_hw_x_spec: Optional[str] = None,
    inject_hw_x_auto: bool = False,
    freeze_tail_from: Optional[int] = None,
    freeze_tail_auto: bool = False,
) -> Tuple[SampleTrace, Dict[str, object]]:
    """Produce the synthetic hardware sample trace plus a description of what was done."""
    rows = read_signal_map(signal_map)
    iice = read_manifest_iice(manifest)
    depth = int(iice["depth"])
    names = data_signal_names(rows)
    if not names:
        raise HarnessError("signal_map.tsv has no data signals to synthesise")
    if drop_signal:
        # Used by `make negctl` to prove the set-equality precheck reports a
        # HARNESS ERROR (exit 2) rather than quietly comparing a smaller set.
        if drop_signal not in names:
            raise HarnessError(
                "--drop-signal %r is not a data signal; available: %s"
                % (drop_signal, names)
            )
        if len(names) < 2:
            raise HarnessError(
                "--drop-signal needs at least 2 data signals in the manifest"
            )
        names = [n for n in names if n != drop_signal]

    scope, scope_note = resolve_sim_scope(
        fsdb_tools.list_signals(sim_fsdb, logdir), rows
    )
    sim_trace = load_sim_trace(sim_fsdb, rows, logdir, str(iice["edge"]), scope=scope)
    window, info = crop_sim_window(sim_trace, depth, str(iice["trigger_time"]))
    sim_data = window.subset(names)
    hw_trace, n_quant = quantise_trace(sim_data, x_mode, x_seed)

    note = None
    prefer = trigger_offset(depth, str(iice["trigger_time"]))
    if perturb_auto:
        name, sample, bit = choose_perturb_target(sim_data, names, prefer_from=prefer)
        note = apply_perturbation(hw_trace, sim_data, name, sample, bit)
    elif perturb:
        name, sample, bit = parse_perturb(perturb)
        note = apply_perturbation(hw_trace, sim_data, name, sample, bit)
    elif inject_hw_x_auto:
        name, sample, bit = choose_perturb_target(sim_data, names, prefer_from=prefer)
        note = "HW-X " + inject_hw_x(hw_trace, sim_data, name, sample, bit)
    elif inject_hw_x_spec:
        name, sample, bit = parse_perturb(inject_hw_x_spec)
        note = "HW-X " + inject_hw_x(hw_trace, sim_data, name, sample, bit)
    elif freeze_tail_auto:
        # A little after the trigger, matching inject_divergence's
        # late-divergence default: clean agreement through the trigger, then a
        # tail that never moves again.
        start = min(max(prefer + max(1, depth // 8), 1), depth - 1)
        note = freeze_tail(hw_trace, sim_data, start)
    elif freeze_tail_from is not None:
        note = freeze_tail(hw_trace, sim_data, int(freeze_tail_from))

    desc: Dict[str, object] = {
        "rows": rows,
        "names": names,
        "depth": depth,
        "window_info": info,
        "x_quantised_bits": n_quant,
        "x_mode": x_mode,
        "perturbation": note,
        "dropped_signal": drop_signal,
        "sim_scope": scope,
        "sim_scope_note": scope_note,
    }
    return hw_trace, desc


def write_hw_fsdb(
    hw_trace: SampleTrace,
    rows: Sequence[SignalMapRow],
    names: Sequence[str],
    out_fsdb: str,
    logdir: str,
) -> str:
    """Emit the trace as an FSDB at the *hardware* signal paths.

    One timestamp per sample: an IICE has no time base, so the sample index *is*
    the time.  ``load_hw_trace`` reads it back with the same convention.
    """
    by_name = {r.name: r for r in rows}
    signals: List[Tuple[str, int]] = []
    for n in sorted(names):
        r = by_name[n]
        if r.hw_derived:
            raise HarnessError(
                "cannot emit %r on the hardware side: its hw_path is __DERIVED__" % n
            )
        signals.append((r.hw_path, r.width))
    frames = [
        {by_name[n].hw_path: hw_trace.values[n][i] for n in sorted(names)}
        for i in range(hw_trace.n_samples)
    ]
    os.makedirs(logdir, exist_ok=True)
    vcd = os.path.join(logdir, "hw_iice_synth.vcd")
    fsdb_tools.write_vcd(vcd, signals, frames, version="identify_iice synthetic hw")
    return fsdb_tools.vcd2fsdb(vcd, out_fsdb, logdir)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="PHASE 0: synthesise a 'hardware' FSDB from a sim FSDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--sim-fsdb", required=True)
    ap.add_argument("--signal-map", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True, help="output hardware FSDB")
    ap.add_argument("--logdir", required=True)
    ap.add_argument("--x-mode", default="prng", choices=list(X_MODES),
                    help="how to quantise sim X/Z (default prng: makes the "
                         "one-way X mask load-bearing)")
    ap.add_argument("--x-seed", default="iice")
    ap.add_argument("--perturb", metavar="SIGNAL:SAMPLE:BIT",
                    help="NEGATIVE CONTROL: flip exactly one bit")
    ap.add_argument("--perturb-auto", action="store_true",
                    help="NEGATIVE CONTROL: flip the first deterministically "
                         "chosen non-X bit at/after the trigger")
    ap.add_argument("--inject-hw-x", metavar="SIGNAL:SAMPLE:BIT",
                    help="NEGATIVE CONTROL: put an X on the HARDWARE side, which "
                         "must be a mismatch (hardware can never produce X)")
    ap.add_argument("--inject-hw-x-auto", action="store_true",
                    help="NEGATIVE CONTROL: as --inject-hw-x, target chosen "
                         "deterministically at/after the trigger")
    ap.add_argument("--drop-signal", metavar="NAME",
                    help="omit one data signal, to prove the set-equality "
                         "precheck reports a harness error (exit 2)")
    ap.add_argument("--freeze-tail-auto", action="store_true",
                    help="NEGATIVE CONTROL: as --freeze-tail-from, start chosen "
                         "deterministically a little after the trigger")
    ap.add_argument("--freeze-tail-from", metavar="SAMPLE", type=int,
                    help="NEGATIVE CONTROL: hold EVERY signal at its "
                         "SAMPLE-1 value to the end, so the tail carries no "
                         "value change at all (the wedged-DUT shape). Must be "
                         "reported as a MISMATCH naming the stall, never as a "
                         "harness error")
    ap.add_argument("--json", help="also write the trace as JSON (debugging)")
    ap.add_argument("--no-stamp", action="store_true",
                    help="do NOT write <out>.prov.json. Use it to build an "
                         "artifact with no provenance on purpose -- an unstamped "
                         "artifact must stay comparable (absence is not a "
                         "mismatch), and `make negctl` NC7 asserts exactly that")
    ap.add_argument("--stamp-origin", default=provenance.ORIGIN_SYNTHETIC,
                    help="what the stamp records as this artifact's origin "
                         "(default %s -- this is NOT a board capture)"
                         % provenance.ORIGIN_SYNTHETIC)
    args = ap.parse_args(argv)

    chosen = [bool(args.perturb), args.perturb_auto,
              bool(args.inject_hw_x), args.inject_hw_x_auto,
              args.freeze_tail_from is not None, args.freeze_tail_auto]
    if sum(1 for c in chosen if c) > 1:
        print("HARNESS ERROR: pick at most ONE of --perturb / --perturb-auto / "
              "--inject-hw-x / --inject-hw-x-auto / --freeze-tail-from / "
              "--freeze-tail-auto (a "
              "negative control must flip exactly one thing)", file=sys.stderr)
        return EXIT_HARNESS
    try:
        hw_trace, desc = build_hw_trace(
            args.sim_fsdb, args.signal_map, args.manifest, args.logdir,
            x_mode=args.x_mode, x_seed=args.x_seed,
            perturb=args.perturb, perturb_auto=args.perturb_auto,
            drop_signal=args.drop_signal,
            inject_hw_x_spec=args.inject_hw_x,
            inject_hw_x_auto=args.inject_hw_x_auto,
            freeze_tail_from=args.freeze_tail_from,
            freeze_tail_auto=args.freeze_tail_auto,
        )
        out = write_hw_fsdb(hw_trace, desc["rows"], desc["names"], args.out, args.logdir)
        if args.json:
            hw_trace.save(args.json)
        # PROVENANCE: bind the artifact to the manifest it was derived from, so
        # `compare` can REFUSE a pairing rather than produce a confident wrong
        # answer. Origin says `synthetic` -- this trace never came off a board, so
        # it must not claim a static_id/rm_id it does not have.
        stamp_note = None
        stamp_digest = None
        if not args.no_stamp:
            identity = provenance.manifest_identity(args.manifest)
            provenance.check_signal_map(identity, args.signal_map)
            block = provenance.build_stamp(
                identity, out,
                origin=args.stamp_origin,
                produced_by="synth_hw_fsdb.py",
                capture={
                    "n_samples": hw_trace.n_samples,
                    "signals": sorted(desc["names"]),
                    "grid_source": "one timestamp per sample (written by this tool)",
                    "x_mode": desc["x_mode"],
                    "x_quantised_bits": desc["x_quantised_bits"],
                    "perturbation": desc["perturbation"],
                    "dropped_signal": desc["dropped_signal"],
                },
                ids=provenance.ids_from_env(),
            )
            stamp_note = provenance.write_stamp(out, block)
            stamp_digest = provenance.short_digest(identity["signal_set_sha256"])
        info = desc["window_info"]
        print("synth_hw_fsdb: sim scope %r (unique leaf-set match, "
              "INTERFACES.md §1)" % desc["sim_scope"])
        print("synth_hw_fsdb: %d samples x %d signals at hardware paths" % (
            hw_trace.n_samples, len(desc["names"])))
        print("synth_hw_fsdb: window [%d..%d], trigger at window index %d" % (
            info["window_start_sample"], info["window_end_sample"],
            info["trigger_window_index"]))
        print("synth_hw_fsdb: quantised %d X/Z bits to 0/1 (mode=%s)" % (
            desc["x_quantised_bits"], desc["x_mode"]))
        if args.freeze_tail_from is not None or args.freeze_tail_auto:
            # Not a bit flip, so it must not print as one: negctl NC1 greps for
            # "PERTURBED" and NC5 greps for "FROZEN TAIL".
            print("synth_hw_fsdb: *** %s" % desc["perturbation"])
        elif desc["perturbation"]:
            print("synth_hw_fsdb: *** PERTURBED *** %s" % desc["perturbation"])
        else:
            print("synth_hw_fsdb: no perturbation (clean synthetic capture)")
        if desc["dropped_signal"]:
            print("synth_hw_fsdb: *** DROPPED SIGNAL *** %s (expect exit 2 from compare)"
                  % desc["dropped_signal"])
        if stamp_note:
            print("synth_hw_fsdb: provenance stamp %s (%s, probe set %s)"
                  % (stamp_note, args.stamp_origin, stamp_digest))
        else:
            print("synth_hw_fsdb: *** NO PROVENANCE STAMP *** (--no-stamp); "
                  "compare must still accept this artifact")
        print("synth_hw_fsdb: wrote %s" % out)
    except HarnessError as exc:
        print("HARNESS ERROR (synth_hw_fsdb): %s" % exc, file=sys.stderr)
        return EXIT_HARNESS
    return EXIT_MATCH


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
