#!/usr/bin/env python3
"""provenance.py -- bind every artifact to the IDENTITY of the thing that
produced it, and REFUSE to proceed on a mismatch.

WHY THIS FILE EXISTS
====================
On 2026-07-30 the same failure class bit three times, and every time the result
was **plausible nonsense rather than an error**:

1. ``make hw-fsdb`` read whichever ``build/signal_map.tsv`` the last ``make gen``
   happened to leave behind -- normally the *selftest* one -- so a real nanosoc
   capture was normalised against the wrong signal set.  Patched by adding a
   ``signal-map`` prerequisite to that one target, which fixes the *instance*:
   nothing stops any other entry point, or a hand-run ``trace_compare.py``, from
   pairing a manifest with a ``signal_map.tsv`` generated from a different one.
2. ``signals_nanosoc.yaml`` was retargeted to 13 signals / 113 bits while the
   built bitstream carries the old 6-signal / 69-bit set.  Nothing compared the
   manifest against the instrumentation that was actually built; the mismatch
   surfaced only as a set-equality failure downstream, and only because the
   signal *names* happened to differ.  Had the retarget kept the names and
   changed a width, it would have compared cleanly and lied.
3. A readback ``.ll`` must correspond to the **loaded RM**, not the base.
   Nothing enforced that either.

Fixing the three instances leaves the class intact.  So: every artifact this
harness writes carries a **stamp** naming the identity of the manifest it was
produced against, and every consumer **refuses** (exit 2, INTERFACES.md §4) when
a stamp it can read disagrees with the manifest it is being asked to use.

WHAT "IDENTITY" MEANS HERE
==========================
``manifest_identity()`` -> ``signal_set_sha256``: a digest over the IICE name,
the depth, the sample clock, and the **ordered** signal set (name / width / hw /
sim / sample / trigger).  It changes exactly when the probe set changes.

Deliberately NOT in the digest:

* ``radix`` -- a display choice.  Re-rendering ``haddr`` in binary does not make
  an existing capture stale, so it must not invalidate a stamp.
* ``trigger.expr``, ``controller``, ``trigger_conditions``, ``trigger_states`` --
  they change the *trigger*, not the recorded probe set.  A capture is still a
  faithful capture of the same signals; the window may sit somewhere else, and
  ``compare_report.txt`` already shows where the window came from.

Deliberately IN the digest: ``sample`` / ``trigger`` per signal.  They are what
Identify's ``signals add`` flags become, and flipping ``sample: true`` to
``false`` removes bits from the sample buffer -- a different instrumentation.

THE PERMISSIVE/CONFLICTING DISTINCTION -- get this wrong and the whole existing
corpus breaks
==============================================================================
**Absence of a stamp is not a mismatch.**  Older captures, hand-made phase-0
FSDBs and every artifact produced before this file existed have no stamp, and
they must keep working.  A *conflicting* stamp is a hard refusal.  The same rule
applies field by field: a stamp that records no ``static_id`` is silent about
``static_id``; a stamp that records one which disagrees is fatal.

Conventions followed rather than reinvented (``fpga/dfx/README.md`` "Overlay
triples", ``docs/PLATFORM_ONE_IMPL.md``): the stamp is JSON with a ``schema``
integer, and ids are the same keys and the same ``0xXXXXXXXX`` upper-case form
the overlay manifests use -- ``static_id`` (shell *design* identity, minted
once), ``static_usercode`` (shell *implementation* identity, differs per run) and
``rm_id`` (which RM is resident, read from ``DFXCTL.RM_ID`` @ ``0x44A1_0010``).

CLI
===
::

    python3 provenance.py identity        --manifest M [--json]
    python3 provenance.py check-signal-map --manifest M --signal-map S
    python3 provenance.py check-identify-log --manifest M [--identify-log L]
                                             [--require-log] [--require-iice]
    python3 provenance.py show-stamp      --artifact F
    python3 provenance.py check-stamp     --artifact F --manifest M
                                          [--expect-rm-id X] [--expect-static-id Y]
                                          [--expect-static-usercode Z]

Exit codes are INTERFACES.md §4's: ``0`` agree, ``2`` harness error / refusal.
There is no ``1`` -- provenance is never a "finding about the DUT".
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fsdb_tools import EXIT_HARNESS, EXIT_MATCH, HarnessError  # noqa: E402

#: Stamp schema version.  Bumped only on an incompatible change; a stamp from a
#: FUTURE schema is a refusal, not a shrug -- we cannot verify what we cannot
#: parse, and "cannot verify" must never read as "verified".
SCHEMA = 1

#: Digest domain tag.  Included in the hashed text so a digest can never be
#: confused with one computed by some other tool over similar-looking data.
DIGEST_DOMAIN = "iice_signal_set/1"

#: Sidecar naming.  A stamp lives beside its artifact and is found by path, so
#: it travels with the file on a copy and is trivially inspectable.
STAMP_SUFFIX = ".prov.json"

#: The instrumentation log Identify writes -- ground truth for "what is in the
#: bitstream".  Relative to the repository root.
IDENTIFY_LOG_DEFAULT = "fpga/rp/nanosoc_iice/build/rev_1_identify/identify.log"

#: Origins a stamp may declare.  Not a whitelist for refusal (an unknown origin
#: is reported, not fatal) -- it exists so the report can say what it is reading.
ORIGIN_SYNTHETIC = "synthetic"
ORIGIN_CAPTURE = "identify_capture"
#: Reserved for the SIM side.  Nothing writes it yet -- `Makefile.sim` is stream
#: B's -- but the consumer side is already symmetric (`run_compare` checks a sim
#: stamp with the same machinery), so naming it here means stream B can start
#: stamping `build/sim_iice.fsdb` and have it enforced with no change on this
#: side.  See INTERFACES.md §5's 2026-07-31 note.
ORIGIN_SIM = "sim"
ORIGIN_TEXT = {
    ORIGIN_SYNTHETIC: "derived from the SIM trace (phase 0: no board, no Identify)",
    ORIGIN_CAPTURE: "normalised from a real Identify `write fsdb` capture",
    ORIGIN_SIM: "dumped by the generated shadow IICE in simulation",
}

_ID_KEYS = ("static_id", "static_usercode", "rm_id")


# ==========================================================================
# ids -- same spelling as the overlay manifests
# ==========================================================================

def norm_id(value: Optional[object], what: str = "id") -> Optional[str]:
    """Canonicalise a 32-bit id to ``0xXXXXXXXX``, or ``None`` if not supplied.

    Accepts ``0x``-prefixed, bare hex and the underscore grouping the overlay
    manifests' worked examples use (``0x0000_0001``).  Junk raises: a typo in
    ``IICE_EXPECT_RM_ID`` must stop the run, not silently disable the check it
    was meant to enable.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace("_", "")
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    if not cleaned or not re.match(r"^[0-9a-fA-F]{1,8}$", cleaned):
        raise HarnessError(
            "%s=%r is not a 32-bit hex id (want e.g. 0x01000001).\n"
            "  Refusing to continue: an unparseable id would silently disable "
            "the check it was supposed to turn on." % (what, text)
        )
    return "0x%08X" % int(cleaned, 16)


def ids_from_env(env: Optional[Dict[str, str]] = None,
                 prefix: str = "IICE_") -> Dict[str, str]:
    """Collect ``{static_id, static_usercode, rm_id}`` from the environment.

    Producer side.  ``IICE_STATIC_ID`` / ``IICE_STATIC_USERCODE`` / ``IICE_RM_ID``.
    Absent variables are simply absent from the result -- see the
    permissive/conflicting rule in this module's docstring.
    """
    env = os.environ if env is None else env
    out: Dict[str, str] = {}
    for key in _ID_KEYS:
        var = prefix + key.upper()
        val = norm_id(env.get(var), var)
        if val is not None:
            out[key] = val
    return out


def expectations_from_env(env: Optional[Dict[str, str]] = None,
                          prefix: str = "IICE_EXPECT_") -> Dict[str, str]:
    """Consumer side: ``IICE_EXPECT_{STATIC_ID,STATIC_USERCODE,RM_ID}``.

    Opt-in on purpose.  What the harness can prove on its own is that a capture
    was produced against *this* manifest; whether it came off the RM you think it
    did is a fact only the caller has (``scripts/mps3_state.sh --quiet`` prints
    ``RM_ID=``).  Supplying it turns a runtime fact into an enforced one.
    """
    env = os.environ if env is None else env
    out: Dict[str, str] = {}
    for key in _ID_KEYS:
        var = prefix + key.upper()
        val = norm_id(env.get(var), var)
        if val is not None:
            out[key] = val
    return out


# ==========================================================================
# manifest -> identity
# ==========================================================================

def read_manifest_signals(path: str) -> Dict[str, object]:
    """The manifest's own view of its probe set: signals, clock, name, depth.

    Read from the **manifest**, never from ``signal_map.tsv``.  That is the whole
    point: ``signal_map.tsv`` is the artifact whose correspondence to the
    manifest is in question (incident 1), so deriving the identity from it would
    make the check tautological.

    Prefers stream A's validating ``manifest.py`` (single source of truth); falls
    back to a lenient YAML read with the *same* documented defaults
    (``sample: true``, ``trigger: false`` -- manifest.py ``_validate_signals``)
    so a not-quite-valid hand-written manifest can still be identified rather
    than aborting a compare for an unrelated schema nit.
    """
    if not os.path.isfile(path):
        raise HarnessError("manifest not found: %s" % path)
    source = "manifest.py"
    signals: List[Dict[str, object]] = []
    try:
        import manifest as _mf  # type: ignore

        m = _mf.load(path)
        iice_name = m.iice.name
        depth = int(m.iice.depth)
        clock = {"hw": m.iice.clock.hw, "sim": m.iice.clock.sim,
                 "edge": m.iice.clock.edge}
        for s in m.signals:
            signals.append({
                "name": s.name, "width": int(s.width), "hw": s.hw, "sim": s.sim,
                "sample": bool(s.sample), "trigger": bool(s.trigger),
                "radix": s.radix,
            })
    except Exception:
        source = "yaml"
        try:
            import yaml  # type: ignore
        except ImportError:
            raise HarnessError(
                "cannot identify %s: stream A's manifest.py rejected it and "
                "PyYAML is not installed, so there is no second way to read it."
                % path
            )
        with open(path) as fh:
            doc = yaml.safe_load(fh)
        if not isinstance(doc, dict) or not isinstance(doc.get("iice"), dict):
            raise HarnessError("manifest %s has no top-level 'iice:' block" % path)
        iice = doc["iice"]
        iice_name = str(iice.get("name", "IICE"))
        try:
            depth = int(iice["depth"])
        except (KeyError, TypeError, ValueError):
            raise HarnessError("manifest %s: iice.depth missing or not an integer" % path)
        clk = iice.get("clock") or {}
        if not isinstance(clk, dict):
            clk = {}
        clock = {"hw": str(clk.get("hw", "")), "sim": str(clk.get("sim", "")),
                 "edge": str(clk.get("edge", "positive"))}
        node = doc.get("signals")
        if not isinstance(node, list) or not node:
            raise HarnessError("manifest %s: `signals:` must be a non-empty list" % path)
        for i, ent in enumerate(node):
            if not isinstance(ent, dict):
                raise HarnessError("manifest %s: signals[%d] is not a mapping" % (path, i))
            try:
                width = int(ent["width"])
            except (KeyError, TypeError, ValueError):
                raise HarnessError(
                    "manifest %s: signals[%d].width missing or not an integer" % (path, i))
            signals.append({
                "name": str(ent.get("name", "")),
                "width": width,
                "hw": str(ent.get("hw", "")),
                "sim": str(ent.get("sim", "")),
                "sample": bool(ent.get("sample", True)),
                "trigger": bool(ent.get("trigger", False)),
                "radix": str(ent.get("radix", "bin")),
            })
    return {
        "iice_name": iice_name,
        "depth": depth,
        "clock": clock,
        "signals": signals,
        "read_by": source,
    }


def digest_text(iice_name: str, depth: int, clock: Dict[str, object],
                signals: Sequence[Dict[str, object]]) -> str:
    """The exact text that gets hashed.  Kept separate so a test can read it."""
    lines = [
        DIGEST_DOMAIN,
        "iice\t%s" % iice_name,
        "depth\t%d" % int(depth),
        "clock\t%s\t%s\t%s" % (clock.get("hw", ""), clock.get("sim", ""),
                               clock.get("edge", "")),
    ]
    for i, s in enumerate(signals):
        lines.append("signal\t%d\t%s\t%d\t%s\t%s\t%s\t%s" % (
            i, s["name"], int(s["width"]), s["hw"], s["sim"],
            "S" if s.get("sample") else "-",
            "T" if s.get("trigger") else "-",
        ))
    return "\n".join(lines) + "\n"


def signal_set_digest(iice_name: str, depth: int, clock: Dict[str, object],
                      signals: Sequence[Dict[str, object]]) -> str:
    return hashlib.sha256(
        digest_text(iice_name, depth, clock, signals).encode("utf-8")
    ).hexdigest()


def manifest_identity(manifest_path: str) -> Dict[str, object]:
    """Everything that identifies a manifest's instrumentation, as plain data."""
    info = read_manifest_signals(manifest_path)
    signals = info["signals"]  # type: ignore[index]
    sampled = [s for s in signals if s.get("sample")]         # type: ignore[union-attr]
    triggered = [s for s in signals if s.get("trigger")]      # type: ignore[union-attr]
    digest = signal_set_digest(
        str(info["iice_name"]), int(info["depth"]),           # type: ignore[arg-type]
        info["clock"], signals,                               # type: ignore[arg-type]
    )
    return {
        "manifest": os.path.abspath(manifest_path),
        "iice_name": info["iice_name"],
        "depth": int(info["depth"]),                          # type: ignore[arg-type]
        "clock": info["clock"],
        "signals": signals,
        "n_signals": len(signals),                            # type: ignore[arg-type]
        "sampled_bits": sum(int(s["width"]) for s in sampled),
        "triggered_bits": sum(int(s["width"]) for s in triggered),
        "signal_set_sha256": digest,
        "read_by": info["read_by"],
    }


def short_digest(digest: Optional[str]) -> str:
    return "(none)" if not digest else str(digest)[:12]


def identity_headline(identity: Dict[str, object]) -> str:
    return "%s depth %s, %s signal(s), %s sampled bits, set %s" % (
        identity.get("iice_name"), identity.get("depth"),
        identity.get("n_signals"), identity.get("sampled_bits"),
        short_digest(identity.get("signal_set_sha256")),  # type: ignore[arg-type]
    )


# ==========================================================================
# manifest <-> signal_map.tsv  (incident 1, fixed for EVERY entry point)
# ==========================================================================

MAP_HEADER: Tuple[str, ...] = ("name", "width", "radix", "hw_path", "sim_path")
DERIVED = "__DERIVED__"
TRIGGER_MARKER = "trigger_marker"
SAMPLE_CLK = "sample_clk"


def expected_signal_map_rows(identity: Dict[str, object]) -> List[Tuple[str, ...]]:
    """The rows ``gen_mangle.py`` must emit for this manifest, header first.

    Mirrors ``gen_mangle.rows()`` (INTERFACES.md §3.3): manifest signals in
    manifest order, then ``trigger_marker`` (``__DERIVED__`` both sides), then
    ``sample_clk`` (the manifest's ``iice.clock`` paths).  Reimplemented rather
    than imported because ``gen_mangle`` needs a fully *validated* Manifest and
    this must work on the lenient path too -- ``test_provenance.py`` asserts the
    two agree on every shipped manifest and on ``golden/signal_map.tsv``, so a
    drift between them is a test failure, not a silent divergence.
    """
    out: List[Tuple[str, ...]] = [MAP_HEADER]
    for s in identity["signals"]:  # type: ignore[index]
        out.append((s["name"], str(int(s["width"])), s["radix"], s["hw"], s["sim"]))
    out.append((TRIGGER_MARKER, "1", "bin", DERIVED, DERIVED))
    clock = identity["clock"]  # type: ignore[index]
    out.append((SAMPLE_CLK, "1", "bin", clock["hw"], clock["sim"]))  # type: ignore[index]
    return out


def read_signal_map_rows(path: str) -> List[Tuple[str, ...]]:
    """``signal_map.tsv`` as raw tuples, header included, nothing interpreted."""
    if not os.path.isfile(path):
        raise HarnessError(
            "signal_map.tsv not found: %s\n"
            "  Run `make gen` (or `make signal-map MANIFEST=<m>`) first." % path
        )
    with open(path) as fh:
        lines = [l.rstrip("\n") for l in fh if l.strip()]
    return [tuple(l.split("\t")) for l in lines]


def rows_to_tuples(rows: Sequence[object]) -> List[Tuple[str, ...]]:
    """``crop_trace.SignalMapRow`` objects back to raw TSV tuples, header first.

    Lets an in-memory row list be checked against a manifest without a file --
    ``normalise_hw_capture`` holds rows, not a path.
    """
    out: List[Tuple[str, ...]] = [MAP_HEADER]
    for r in rows:
        out.append((r.name, str(int(r.width)), r.radix,      # type: ignore[attr-defined]
                    r.hw_path, r.sim_path))                  # type: ignore[attr-defined]
    return out


def check_signal_map(identity: Dict[str, object],
                     signal_map: object) -> Dict[str, object]:
    """Assert the signal map really was generated from *this* manifest.

    *signal_map* is either a path to ``signal_map.tsv`` or an already-parsed list
    of ``crop_trace.SignalMapRow``.  Raises :class:`HarnessError` (-> exit 2)
    naming the offending row and field.

    This is incident 1's general fix.  The 2026-07-30 patch added a
    ``signal-map`` prerequisite to ``make hw-fsdb``; that fixed one target.  This
    binds the two artifacts wherever they meet, including a hand-run
    ``trace_compare.py`` and ``normalise_hw_capture``'s in-memory rows.
    """
    if isinstance(signal_map, str):
        source = os.path.abspath(signal_map)
        got = read_signal_map_rows(signal_map)
    else:
        source = "<rows passed in memory>"
        got = rows_to_tuples(signal_map)  # type: ignore[arg-type]
    want = expected_signal_map_rows(identity)
    if got == want:
        return {"signal_map": source, "rows": len(got) - 1, "verified": True}

    detail: List[str] = []
    if got and got[0] != MAP_HEADER:
        detail.append("  header is %r, expected %r" % (list(got[0]), list(MAP_HEADER)))
    if len(got) != len(want):
        detail.append(
            "  row count: manifest describes %d signal row(s), signal_map.tsv has %d"
            % (len(want) - 1, max(len(got) - 1, 0)))
    for i in range(1, min(len(got), len(want))):
        g, w = got[i], want[i]
        if g == w:
            continue
        if len(g) != len(w):
            detail.append("  row %d has %d field(s), expected %d: %r"
                          % (i, len(g), len(w), list(g)))
            continue
        for field, gv, wv in zip(MAP_HEADER, g, w):
            if gv != wv:
                detail.append("  row %d field %r:\n"
                              "    manifest says    %s\n"
                              "    signal_map says  %s" % (i, field, wv, gv))
        if len(detail) > 24:
            detail.append("  ... further differences not listed")
            break
    if len(got) > len(want):
        for i in range(len(want), min(len(got), len(want) + 4)):
            detail.append("  row %d is in signal_map.tsv but not in the manifest: %r"
                          % (i, list(got[i])))
    if len(want) > len(got):
        for i in range(len(got), min(len(want), len(got) + 4)):
            detail.append("  row %d is in the manifest but not in signal_map.tsv: %r"
                          % (i, list(want[i])))

    raise HarnessError(
        "signal_map.tsv WAS NOT GENERATED FROM THIS MANIFEST -- refusing to "
        "compare.\n"
        "  manifest    %s\n"
        "              %s\n"
        "  signal_map  %s\n"
        "%s\n"
        "  Regenerate:  make -C tests/identify_iice signal-map MANIFEST=%s\n"
        "  WHY THIS IS FATAL: on 2026-07-30 a real nanosoc capture was "
        "normalised against the SELFTEST signal map, because build/signal_map.tsv "
        "is written by whichever manifest last ran `make gen`. That pairing "
        "produces a confident, wrong answer whenever the two happen to agree on "
        "signal names -- so it is exit 2 (INTERFACES.md §4), never a warning."
        % (identity.get("manifest"), identity_headline(identity),
           source, "\n".join(detail), identity.get("manifest"))
    )


# ==========================================================================
# identify.log -- what is ACTUALLY in the bitstream (incident 2)
# ==========================================================================

_RE_LOG_CLOCK = re.compile(
    r"Setting IICE sample clock to '(?P<clk>[^']*)' for IICE named '(?P<iice>[^']*)'")
_RE_LOG_DEPTH = re.compile(
    r"Setting IICE sampler \(sampledepth\) to (?P<depth>\d+) for IICE named "
    r"'(?P<iice>[^']*)'")
_RE_LOG_SIGNAL = re.compile(
    r"Instrument Signal (?P<path>\S+) for (?P<mode>trigger and sample|"
    r"sample and trigger|sample|trigger) in (?P<iice>\S+)\s*$")
_RE_LOG_GEN = re.compile(r"Generating IICE '(?P<iice>[^']*)' for the following settings")
_RE_LOG_GEN_DEPTH = re.compile(r"^\s+Depth\s+(?P<depth>\d+)\s*$")
_RE_LOG_GEN_WIDTH = re.compile(r"^\s+Width\s+(?P<width>\d+)\s+bits\s*$")


def _blank_iice() -> Dict[str, object]:
    return {"depth": None, "gen_depth": None, "width_bits": None,
            "clock": None, "signals": []}


def parse_identify_log(path: str) -> Optional[Dict[str, object]]:
    """Parse an Identify instrumentor log.  ``None`` if the file is absent.

    Absence is normal -- ``build/`` is gitignored, so a fresh clone has no
    instrumentation log at all.  The caller degrades to a documented SKIP.

    The lines this reads are the ones the instrumentor prints for every run:
    ``Setting IICE sample clock to '<path>' for IICE named '<n>'``,
    ``Setting IICE sampler (sampledepth) to <d> ...``,
    ``Instrument Signal <path> for {sample|trigger|trigger and sample} in <n>``,
    and the ``Generating IICE '<n>'`` block's ``Depth`` / ``Width <n> bits``.
    ``Width`` is the SAMPLE BUFFER width -- the sum of the sampled signals'
    widths -- which is the number that proved the as-built nanosoc set was 69
    bits and not the manifest's 113.
    """
    if not os.path.isfile(path):
        return None
    iices: Dict[str, Dict[str, object]] = {}
    cur_gen: Optional[str] = None
    with open(path, errors="replace") as fh:
        for line in fh:
            m = _RE_LOG_CLOCK.search(line)
            if m:
                iices.setdefault(m.group("iice"), _blank_iice())["clock"] = m.group("clk")
                continue
            m = _RE_LOG_DEPTH.search(line)
            if m:
                iices.setdefault(m.group("iice"), _blank_iice())["depth"] = \
                    int(m.group("depth"))
                continue
            m = _RE_LOG_SIGNAL.search(line)
            if m:
                mode = m.group("mode")
                entry = iices.setdefault(m.group("iice"), _blank_iice())
                entry["signals"].append({          # type: ignore[union-attr]
                    "path": m.group("path"),
                    "sample": "sample" in mode,
                    "trigger": "trigger" in mode,
                })
                continue
            m = _RE_LOG_GEN.search(line)
            if m:
                cur_gen = m.group("iice")
                iices.setdefault(cur_gen, _blank_iice())
                continue
            if cur_gen is not None:
                m = _RE_LOG_GEN_DEPTH.match(line.rstrip("\n"))
                if m:
                    iices[cur_gen]["gen_depth"] = int(m.group("depth"))
                    continue
                m = _RE_LOG_GEN_WIDTH.match(line.rstrip("\n"))
                if m:
                    iices[cur_gen]["width_bits"] = int(m.group("width"))
                    continue
    return {"path": os.path.abspath(path), "iices": iices}


def check_identify_log(identity: Dict[str, object],
                       log: Optional[Dict[str, object]],
                       require_iice: bool = False) -> Dict[str, object]:
    """Compare a manifest against the instrumentation that was actually built.

    Returns ``{"status": "absent"|"not_applicable"|"ok"|"mismatch", ...}``.

    * ``absent`` -- there is no log.  ``build/`` is gitignored, so a fresh clone
      has none.  Not a failure; the caller decides whether to require one.
    * ``not_applicable`` -- the log exists but instrumented a *different* IICE.
      The selftest manifest is a simulation-only fixture and is never
      instrumented into a bitstream, so pairing it with the nanosoc log is not a
      finding, it is a non-pairing.  Pass ``require_iice=True`` when you know the
      log ought to contain this IICE and its absence would be a real mismatch.
      Deliberately permissive: an IICE *rename* between manifest and build lands
      here rather than in ``mismatch``, and is reported as an explicit non-result
      rather than a silent pass.
    * ``ok`` / ``mismatch`` -- the log DOES describe this IICE, so every fact is
      checked hard.

    This is the check that would have caught the 69-vs-113 confusion in one
    second: ``Width 69 bits`` in the log against 113 sampled bits in the
    manifest -- same IICE name, so it lands in ``mismatch``.
    """
    if log is None:
        return {"status": "absent", "log": None, "discrepancies": [],
                "note": "no Identify instrumentation log found -- `build/` is "
                        "gitignored, so a fresh clone has none. Nothing is "
                        "asserted about what is in the bitstream."}
    name = str(identity["iice_name"])
    iices: Dict[str, Dict[str, object]] = log["iices"]  # type: ignore[assignment]
    disc: List[str] = []
    if not iices:
        return {"status": "mismatch", "log": log["path"], "discrepancies": [
            "the log names no IICE at all -- it is not an Identify instrumentor "
            "log, or the run failed before instrumenting anything"]}
    if name not in iices:
        note = ("the instrumentation log instrumented %s; this manifest's IICE "
                "%r is not among them, so the log does not describe this "
                "manifest and nothing is asserted about what is in the "
                "bitstream." % (sorted(iices), name))
        if require_iice:
            return {"status": "mismatch", "log": log["path"],
                    "discrepancies": [note + " --require-iice was given."]}
        return {"status": "not_applicable", "log": log["path"],
                "discrepancies": [], "note": note}
    built = iices[name]

    built_depth = built.get("depth")
    if built_depth is None:
        built_depth = built.get("gen_depth")
    if built_depth is not None and int(built_depth) != int(identity["depth"]):
        disc.append("depth: manifest %s, built %s"
                    % (identity["depth"], built_depth))
    if (built.get("depth") is not None and built.get("gen_depth") is not None
            and int(built["depth"]) != int(built["gen_depth"])):
        disc.append("the log disagrees with ITSELF on depth: `iice sampler` says "
                    "%s, the generated IICE says %s"
                    % (built["depth"], built["gen_depth"]))

    width = built.get("width_bits")
    if width is not None and int(width) != int(identity["sampled_bits"]):
        disc.append(
            "sample-buffer width: manifest %s bit(s) over %d sampled signal(s), "
            "built %s bit(s). THIS is the 69-vs-113 shape: a capture off this "
            "bitstream carries the BUILT set, not the manifest's."
            % (identity["sampled_bits"],
               len([s for s in identity["signals"] if s.get("sample")]),  # type: ignore[union-attr]
               width))

    clock = built.get("clock")
    want_clock = (identity["clock"] or {}).get("hw")  # type: ignore[union-attr]
    if clock is not None and want_clock and str(clock) != str(want_clock):
        disc.append("sample clock: manifest %r, built %r" % (want_clock, clock))

    want = {}
    for s in identity["signals"]:  # type: ignore[index]
        want[str(s["hw"])] = (bool(s.get("sample")), bool(s.get("trigger")), s["name"])
    have = {}
    for s in built["signals"]:  # type: ignore[index]
        have[str(s["path"])] = (bool(s["sample"]), bool(s["trigger"]))
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    if missing:
        disc.append("in the manifest but NOT instrumented: %s" % missing)
    if extra:
        disc.append("instrumented but NOT in the manifest: %s" % extra)
    for path in sorted(set(want) & set(have)):
        w_sample, w_trigger, w_name = want[path]
        h_sample, h_trigger = have[path]
        if (w_sample, w_trigger) != (h_sample, h_trigger):
            disc.append(
                "%s (%s): manifest sample=%s trigger=%s, built sample=%s trigger=%s"
                % (path, w_name, w_sample, w_trigger, h_sample, h_trigger))

    facts = {
        "iice_name": name,
        "depth": built_depth,
        "width_bits": width,
        "clock": clock,
        "n_signals": len(built["signals"]),  # type: ignore[arg-type]
    }
    return {"status": "mismatch" if disc else "ok", "log": log["path"],
            "discrepancies": disc, "facts": facts}


def identify_log_error(identity: Dict[str, object],
                       result: Dict[str, object]) -> str:
    """The message for a failed :func:`check_identify_log`."""
    facts = result.get("facts") or {}
    return (
        "INSTRUMENTATION MISMATCH -- the manifest does not describe what was "
        "BUILT.\n"
        "  manifest      %s\n"
        "                %s\n"
        "  identify.log  %s\n"
        "                IICE %s, depth %s, width %s bits, %s signal(s)\n"
        "%s\n"
        "  identify.log is GROUND TRUTH for what is in the bitstream. A capture "
        "off that bitstream carries the BUILT probe set, so normalising it "
        "against this manifest produces a confident, wrong trace.\n"
        "  Fix, pick one: use the as-built manifest (see "
        "signals_nanosoc_asbuilt.yaml, which exists for exactly this reason), or "
        "re-instrument -> Synplify -> place & route -> new DFX partial."
        % (identity.get("manifest"), identity_headline(identity),
           result.get("log"), facts.get("iice_name"), facts.get("depth"),
           facts.get("width_bits"), facts.get("n_signals"),
           "\n".join("    * %s" % d for d in result.get("discrepancies") or []))
    )


# ==========================================================================
# stamps
# ==========================================================================

def stamp_path(artifact: str) -> str:
    return artifact + STAMP_SUFFIX


def build_stamp(
    identity: Dict[str, object],
    artifact: str,
    origin: str,
    produced_by: str,
    raw_capture: Optional[str] = None,
    capture: Optional[Dict[str, object]] = None,
    ids: Optional[Dict[str, str]] = None,
    extra: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Assemble the provenance block for *artifact*.

    ``artifact`` must already exist -- its byte size goes into the stamp, which
    is what makes a stamp left beside a *replaced* file detectable.
    """
    stamp: Dict[str, object] = {
        "schema": SCHEMA,
        "kind": "iice_trace",
        "origin": origin,
        "produced_by": produced_by,
        "built": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "manifest": {
            "path": identity.get("manifest"),
            "iice_name": identity.get("iice_name"),
            "depth": identity.get("depth"),
            "n_signals": identity.get("n_signals"),
            "sampled_bits": identity.get("sampled_bits"),
            "signal_set_sha256": identity.get("signal_set_sha256"),
        },
        "artifact": {
            "file": os.path.basename(artifact),
            "bytes": os.path.getsize(artifact) if os.path.isfile(artifact) else None,
        },
    }
    if raw_capture:
        stamp["raw_capture"] = {
            "path": os.path.abspath(raw_capture),
            "bytes": (os.path.getsize(raw_capture)
                      if os.path.isfile(raw_capture) else None),
        }
    if capture:
        stamp["capture"] = dict(capture)
    for key, val in (ids or {}).items():
        if val is not None:
            stamp[key] = val
    for key, val in (extra or {}).items():
        stamp[key] = val
    return stamp


def write_stamp(artifact: str, stamp: Dict[str, object]) -> str:
    """Write ``<artifact>.prov.json``.  Returns the stamp path."""
    path = stamp_path(artifact)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(stamp, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return path


def read_stamp(artifact: str) -> Optional[Dict[str, object]]:
    """Read ``<artifact>.prov.json``, or ``None`` if there is none.

    ``None`` means "this artifact carries no provenance" and is NOT an error --
    every capture taken before this file existed is in that state.  A stamp that
    exists but cannot be parsed IS an error: a corrupt stamp is a conflicting
    stamp, and silently ignoring it would restore the exact failure mode this
    module exists to remove.
    """
    path = stamp_path(artifact)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            stamp = json.load(fh)
    except (ValueError, OSError) as exc:
        raise HarnessError(
            "provenance stamp %s is unreadable: %s\n"
            "  A stamp that exists but cannot be parsed is a harness error, not "
            "an absent stamp: ignoring it would silently restore the "
            "mismatched-artifact failure mode. Delete it to compare without "
            "provenance." % (path, exc)
        )
    if not isinstance(stamp, dict):
        raise HarnessError(
            "provenance stamp %s is not a JSON object (got %s)"
            % (path, type(stamp).__name__))
    return stamp


def check_stamp(
    stamp: Optional[Dict[str, object]],
    identity: Dict[str, object],
    artifact: Optional[str] = None,
    expect: Optional[Dict[str, str]] = None,
    label: str = "hardware",
) -> List[str]:
    """Discrepancies between a stamp and the manifest it is being used with.

    Empty list == agree (or nothing to check).  **Absence is not a mismatch**;
    absence of an individual field is not a mismatch either.  Everything the
    stamp *does* assert must agree.
    """
    if stamp is None:
        return []
    disc: List[str] = []
    schema = stamp.get("schema")
    if isinstance(schema, int) and schema > SCHEMA:
        return ["%s stamp declares schema %d; this harness understands %d. "
                "Refusing to interpret a stamp from the future -- 'cannot "
                "verify' must never read as 'verified'." % (label, schema, SCHEMA)]

    man = stamp.get("manifest")
    if isinstance(man, dict):
        got_digest = man.get("signal_set_sha256")
        want_digest = identity.get("signal_set_sha256")
        if got_digest and got_digest != want_digest:
            disc.append(
                "%s trace was produced against a DIFFERENT probe set.\n"
                "      produced against  %s  (%s depth %s, %s signal(s), %s "
                "sampled bits)\n"
                "      comparing against %s  (%s)\n"
                "      manifest recorded in the stamp: %s"
                % (label, short_digest(got_digest), man.get("iice_name"),
                   man.get("depth"), man.get("n_signals"),
                   man.get("sampled_bits"),
                   short_digest(want_digest),  # type: ignore[arg-type]
                   identity_headline(identity), man.get("path")))
        else:
            # Only worth naming separately when the digest agrees or is absent;
            # otherwise the digest line already says everything.
            for key, label_text in (("iice_name", "IICE name"), ("depth", "depth")):
                got = man.get(key)
                if got is not None and got != identity.get(key):
                    disc.append("%s trace %s: stamp says %r, manifest says %r"
                                % (label, label_text, got, identity.get(key)))

    if artifact is not None:
        art = stamp.get("artifact")
        if isinstance(art, dict):
            recorded = art.get("bytes")
            if isinstance(recorded, int) and os.path.isfile(artifact):
                actual = os.path.getsize(artifact)
                if recorded != actual:
                    disc.append(
                        "STALE STAMP: %s describes a %d-byte file but %s is now "
                        "%d bytes. The stamp does not describe this artifact -- "
                        "the file was replaced without its provenance."
                        % (stamp_path(artifact), recorded, artifact, actual))

    for key in _ID_KEYS:
        want = (expect or {}).get(key)
        got = stamp.get(key)
        if want is None or got is None:
            continue
        if norm_id(got, key) != norm_id(want, key):
            disc.append("%s trace %s: stamp says %s, caller expects %s"
                        % (label, key, norm_id(got, key), norm_id(want, key)))
    return disc


def stamp_report_lines(stamp: Optional[Dict[str, object]],
                       label: str = "hardware") -> List[str]:
    """Human-readable provenance block for ``compare_report.txt``."""
    head = "%s trace provenance" % label
    if stamp is None:
        return [
            "  %-26s(none)" % head,
            "    This artifact carries no provenance stamp. Absence is NOT a",
            "    mismatch (INTERFACES.md §4 amendment 2026-07-31) -- older",
            "    captures and synthetic phase-0 FSDBs have none -- but nothing",
            "    binds it to the manifest above either. Treat the pairing as",
            "    asserted by whoever ran this, not as verified.",
        ]
    man = stamp.get("manifest") or {}
    cap = stamp.get("capture") or {}
    raw = stamp.get("raw_capture") or {}
    def row(k, v):
        return "    %-24s%s" % (k, v)

    out = ["  %-26sVERIFIED against the manifest above" % head]
    origin = str(stamp.get("origin"))
    out.append(row("origin", "%s%s" % (
        stamp.get("origin"),
        "  -- %s" % ORIGIN_TEXT[origin] if origin in ORIGIN_TEXT else "")))
    out.append(row("produced by", stamp.get("produced_by")))
    out.append(row("produced on", stamp.get("built")))
    out.append(row("against manifest",
                   man.get("path") if isinstance(man, dict) else None))
    out.append(row("probe set digest", short_digest(
        man.get("signal_set_sha256") if isinstance(man, dict) else None)))
    if isinstance(raw, dict) and raw.get("path"):
        out.append(row("raw capture", "%s (%s bytes)"
                       % (raw.get("path"), raw.get("bytes"))))
    if isinstance(cap, dict) and cap:
        out.append(row("samples recovered", cap.get("n_samples")))
        if cap.get("grid_source"):
            out.append(row("sample grid established", cap.get("grid_source")))
        if cap.get("sample_period") is not None:
            out.append(row("sample period", cap.get("sample_period")))
        if cap.get("raw_time_base"):
            out.append(row("raw time base", (cap.get("raw_time_base"),)))
    known = [k for k in _ID_KEYS if stamp.get(k)]
    for key in known:
        out.append(row(key, stamp.get(key)))
    if not known:
        out.append(row("static_id / rm_id", "(not recorded -- nothing is"))
        out.append("%sasserted about which shell or RM this" % (" " * 28))
        out.append("%scapture came from)" % (" " * 28))
    return out


def refuse(disc: Sequence[str], hw_fsdb: str, manifest: str) -> str:
    """The message for a provenance refusal."""
    return (
        "PROVENANCE MISMATCH -- refusing to compare.\n"
        "%s\n"
        "  hw FSDB   %s\n"
        "  stamp     %s\n"
        "  manifest  %s\n"
        "  INTERFACES.md §4: a harness error is exit 2 and is never reported as "
        "a match. These artifacts were produced against different things, so a "
        "verdict over them would be meaningless -- and, historically, plausible."
        % ("\n".join("    * %s" % d for d in disc), hw_fsdb,
           stamp_path(hw_fsdb), manifest)
    )


# ==========================================================================
# CLI
# ==========================================================================

def _repo_root() -> str:
    # tests/identify_iice/ -> repo root
    return os.path.dirname(os.path.dirname(_HERE))


def default_identify_log() -> str:
    """Where the Identify instrumentation log lives, if a build has been run."""
    return os.path.join(_repo_root(), IDENTIFY_LOG_DEFAULT)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Bind IICE trace artifacts to the identity that produced them",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("identity", help="print a manifest's instrumentation identity")
    p.add_argument("--manifest", required=True)
    p.add_argument("--signal-map", help="also verify this signal_map.tsv against it")
    p.add_argument("--json", action="store_true")
    p.add_argument("--digest-text", action="store_true",
                   help="print the exact text that is hashed")

    p = sub.add_parser("check-signal-map",
                       help="refuse unless signal_map.tsv came from this manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--signal-map", required=True)

    p = sub.add_parser("check-identify-log",
                       help="refuse unless the manifest matches what was BUILT")
    p.add_argument("--manifest", required=True)
    p.add_argument("--identify-log", default=None)
    p.add_argument("--require-log", action="store_true",
                   help="absence of the log is a harness error (default: SKIP)")
    p.add_argument("--require-iice", action="store_true",
                   help="a log that does not instrument this manifest's IICE is "
                        "a harness error (default: reported as not-applicable)")

    p = sub.add_parser("show-stamp", help="print an artifact's provenance stamp")
    p.add_argument("--artifact", required=True)

    p = sub.add_parser("check-stamp", help="refuse unless the stamp agrees")
    p.add_argument("--artifact", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--expect-rm-id", default=None)
    p.add_argument("--expect-static-id", default=None)
    p.add_argument("--expect-static-usercode", default=None)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return EXIT_HARNESS

    try:
        if args.cmd == "identity":
            identity = manifest_identity(args.manifest)
            if args.digest_text:
                sys.stdout.write(digest_text(
                    str(identity["iice_name"]), int(identity["depth"]),  # type: ignore[arg-type]
                    identity["clock"], identity["signals"]))             # type: ignore[arg-type]
                return EXIT_MATCH
            if args.signal_map:
                check_signal_map(identity, args.signal_map)
            if args.json:
                print(json.dumps(identity, indent=1, sort_keys=True))
                return EXIT_MATCH
            print("manifest            %s" % identity["manifest"])
            print("read by             %s" % identity["read_by"])
            print("iice                %s" % identity["iice_name"])
            print("depth               %s" % identity["depth"])
            print("signals             %s" % identity["n_signals"])
            print("sampled bits        %s" % identity["sampled_bits"])
            print("triggered bits      %s" % identity["triggered_bits"])
            print("sample clock (hw)   %s" % identity["clock"]["hw"])  # type: ignore[index]
            print("signal_set_sha256   %s" % identity["signal_set_sha256"])
            if args.signal_map:
                print("signal_map.tsv      VERIFIED against this manifest (%s)"
                      % args.signal_map)
            return EXIT_MATCH

        if args.cmd == "check-signal-map":
            identity = manifest_identity(args.manifest)
            info = check_signal_map(identity, args.signal_map)
            print("provenance: signal_map.tsv (%d rows) was generated from %s"
                  % (info["rows"], identity["manifest"]))
            print("provenance: %s" % identity_headline(identity))
            return EXIT_MATCH

        if args.cmd == "check-identify-log":
            identity = manifest_identity(args.manifest)
            log_path = args.identify_log or default_identify_log()
            log = parse_identify_log(log_path)
            result = check_identify_log(identity, log,
                                        require_iice=args.require_iice)
            if result["status"] == "not_applicable":
                print("provenance: NOT APPLICABLE -- %s" % result["note"])
                print("provenance:   manifest %s" % identity_headline(identity))
                print("provenance:   log      %s" % result["log"])
                return EXIT_MATCH
            if result["status"] == "absent":
                msg = ("provenance: SKIP -- no Identify instrumentation log at %s\n"
                       "provenance: %s\n"
                       "provenance: nothing is asserted about what is in the "
                       "bitstream. `build/` is gitignored, so a fresh clone has "
                       "no log; that is not a mismatch." % (log_path, result["note"]))
                if args.require_log:
                    print("HARNESS ERROR (provenance): --require-log was given "
                          "but there is no log at %s" % log_path, file=sys.stderr)
                    return EXIT_HARNESS
                print(msg)
                return EXIT_MATCH
            if result["status"] == "mismatch":
                print("HARNESS ERROR (provenance): %s"
                      % identify_log_error(identity, result), file=sys.stderr)
                return EXIT_HARNESS
            facts = result["facts"]
            print("provenance: manifest MATCHES the built instrumentation")
            print("provenance:   manifest     %s" % identity_headline(identity))
            print("provenance:   identify.log %s" % result["log"])
            print("provenance:   built        IICE %s, depth %s, width %s bits, "
                  "%s signal(s)" % (facts["iice_name"], facts["depth"],  # type: ignore[index]
                                    facts["width_bits"], facts["n_signals"]))  # type: ignore[index]
            return EXIT_MATCH

        if args.cmd == "show-stamp":
            stamp = read_stamp(args.artifact)
            if stamp is None:
                print("provenance: %s carries NO stamp (%s absent). Absence is "
                      "not a mismatch." % (args.artifact, stamp_path(args.artifact)))
                return EXIT_MATCH
            print(json.dumps(stamp, indent=1, sort_keys=True))
            return EXIT_MATCH

        if args.cmd == "check-stamp":
            identity = manifest_identity(args.manifest)
            stamp = read_stamp(args.artifact)
            expect = {
                "rm_id": norm_id(args.expect_rm_id, "--expect-rm-id"),
                "static_id": norm_id(args.expect_static_id, "--expect-static-id"),
                "static_usercode": norm_id(args.expect_static_usercode,
                                           "--expect-static-usercode"),
            }
            expect = {k: v for k, v in expect.items() if v is not None}
            disc = check_stamp(stamp, identity, artifact=args.artifact,
                               expect=expect)
            if disc:
                print("HARNESS ERROR (provenance): %s"
                      % refuse(disc, args.artifact, args.manifest), file=sys.stderr)
                return EXIT_HARNESS
            if stamp is None:
                print("provenance: %s carries no stamp -- nothing to verify, "
                      "and absence is not a mismatch." % args.artifact)
            else:
                print("provenance: %s AGREES with %s" % (args.artifact, args.manifest))
                print("provenance: %s" % identity_headline(identity))
            return EXIT_MATCH

    except HarnessError as exc:
        print("HARNESS ERROR (provenance): %s" % exc, file=sys.stderr)
        return EXIT_HARNESS

    ap.print_help()
    return EXIT_HARNESS


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
