#!/usr/bin/env python3
"""Gate: an overlay built from SoCScope must record WHICH SoCScope it was built from.

THE FAILURE THIS EXISTS FOR. rm_socscope's RTL is not vendored: fpga/dfx/rms/rm_socscope/
filelist.tcl reads it from a sibling checkout ($SOCSCOPE_HOME) at whatever revision that
checkout happens to be on. The overlay that ships is a partial bitstream plus a manifest,
and until now the manifest said nothing about that revision. So "which SoCScope is on the
board" was unanswerable from the repo -- the same shape as "which shell is on the board"
before docs/FIELDED_SHELL.md, and it fails the same way: a capture that disagrees with
the host decoder, and no way to tell whether the RTL or the decoder moved.

WHAT IS GATED. For every fpga/dfx/overlay/<rm>/manifest.json:

    rm consumes SoCScope   -> `socscope_rev` MUST be present
    `socscope_rev` present -> it MUST be one of
        <sha>          resolves in $SOCSCOPE_HOME (`git cat-file -t` says commit)
        unknown        ONLY if the overlay is waived in socscope_rev_waivers.txt
        <sha>-dirty    NEVER. A build from a dirty tree is unreproducible: the sha
                       names a commit the built RTL is not. Commit, rebuild, re-mint.

If the recorded sha is not the checkout's HEAD, that is a NOTE, not a failure: the
overlay predates the checkout, which is the normal state between a SoCScope change and
the next rebuild. The gate cannot know the rebuild is due; it can only say so.

WHAT THE FIELD IS CHECKED AGAINST, AND WHY THAT CHANGED. `socscope_rev` used to be
stamped by `make overlays` from `git -C $SOCSCOPE_HOME rev-parse HEAD` -- i.e. from
whatever that tree was on at MANIFEST time, not at SYNTH time. The documented
mitigation was "run add-rm-socscope and overlays back to back", which is a procedure,
not a check, and it had already failed once:

    the shipped overlay's checkpoint was synthesised 2026-08-06 23:17 (its OOC log is
    fpga/dfx/rms/rm_socscope/build/ooc.log, and all four copies of
    rm_socscope_synth.dcp in this repo are byte-identical to the one it wrote). The
    SoCScope reflog puts HEAD at 5d68932 then -- but the synthesis read
    hw/rtl/socscope_selftest.f, which 5d68932 does not contain: it arrives in 9a734d5,
    committed at 23:28, NINE MINUTES LATER. The tree was DIRTY. A stamp taken
    afterwards would have recorded a clean `9a734d5` and been wrong in the one
    direction nothing downstream can detect.

So fpga/dfx/rms/rm_socscope/ooc_synth.tcl now writes rm_socscope_provenance.json from
inside the Vivado process that read the files -- revision, dirty flag, and a sha256 per
source -- and `make overlays` copies it next to the manifest it stamped. When that file
is present this gate holds the manifest TO it:

    provenance `socscope_dirty` true  -> FAIL (a dirty synthesis may not ship, and
                                         unlike the manifest's `-dirty` it cannot be
                                         hidden by re-stamping later)
    provenance rev != manifest rev    -> FAIL (the stamp names a different commit from
                                         the one the gates were synthesised from)
    provenance absent                 -> NOTE, or FAIL under --require-provenance,
                                         which is the burn-down lever once every
                                         consumer overlay has been rebuilt once.

WHICH RMs CONSUME SOCSCOPE. rm_socscope always. rm_nanosoc only when built with
SOCSCOPE=1 (B2) -- and build_dfx.tcl cannot see that: the flag reaches only
fpga/rp/nanosoc/ooc_synth.tcl, and the staged .dcp carries no marker into
overlay_inputs.txt. So rm_nanosoc is NOT in CONSUMERS; a manifest that carries the field
anyway (hand-recorded, or a future flow that knows) is validated like any other.

WHEN THE CHECKOUT IS ABSENT (a fresh CI clone of the platform repo alone), the structural
checks -- presence, `-dirty`, `unknown`/waiver -- still run and still fail; only the
sha-resolves check is skipped, and the gate SAYS SO. Pass --require-checkout to make an
absent checkout a failure (recommended on a build host).

Exit 0 clean, 1 on any failure. Stdlib only.

    python3 check_socscope_overlay_rev.py [--repo ROOT] [--socscope-home DIR]
                                           [--waivers FILE] [--require-checkout]
                                           [--require-provenance]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

#: rm_names whose overlay MUST carry socscope_rev (see the docstring for why rm_nanosoc
#: is not here even though its B2 variant consumes SoCScope).
CONSUMERS = ("socscope",)

#: A legal recorded value: an abbreviated or full sha, optionally `-dirty`, or `unknown`.
_REV_RX = re.compile(r"^(?:unknown|[0-9a-f]{7,40}(?:-dirty)?)$")

DEFAULT_WAIVERS = os.path.join("scripts", "harness_gates", "socscope_rev_waivers.txt")


def load_waivers(path: str | None) -> dict:
    """`<rm_name>  # reason` per line. Mirrors clearing_fit_waivers.txt: a waiver
    without a reason is not a waiver."""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, reason = line.partition("#")
            name, reason = name.strip(), reason.strip()
            if name and reason:
                out[name] = reason
    return out


def default_socscope_home(repo: str) -> str:
    """$SOCSCOPE_HOME, else the sibling checkout -- the same default as
    fpga/dfx/rms/rm_socscope/filelist.tcl and fpga/dfx/Makefile."""
    env = os.environ.get("SOCSCOPE_HOME", "").strip()
    return env or os.path.normpath(os.path.join(repo, "..", "SoCScope"))


def _git(home: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", home, *args], capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def checkout_head(home: str) -> str | None:
    """Full HEAD sha if `home` is a git checkout, else None."""
    if not os.path.isdir(home):
        return None
    return _git(home, "rev-parse", "HEAD")


def resolves(home: str, rev: str) -> bool:
    return _git(home, "cat-file", "-t", rev) == "commit"


#: What ooc_synth.tcl writes beside the checkpoint and `make overlays` copies beside
#: the manifest. Named here rather than in three places, because the last thing this
#: area needed was a fourth copy of a filename.
PROVENANCE_NAME = "provenance.json"
PROVENANCE_SCHEMA = "rm-socscope-provenance"


def read_provenance(manifest_path: str):
    """Return (record, error) for the provenance file beside `manifest_path`.

    (None, None) means simply absent, which is the state of every overlay built
    before ooc_synth.tcl wrote one.
    """
    path = os.path.join(os.path.dirname(manifest_path), PROVENANCE_NAME)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "unreadable %s (%s)" % (PROVENANCE_NAME, exc)
    if rec.get("schema") != PROVENANCE_SCHEMA:
        return None, "%s has schema %r, expected %r" % (
            PROVENANCE_NAME, rec.get("schema"), PROVENANCE_SCHEMA)
    return rec, None


def check_provenance(rel: str, rev: str, prov: dict):
    """Hold a manifest's `socscope_rev` to the record written at synth time."""
    fails, notes = [], []
    prov_rev = str(prov.get("socscope_rev", "")).strip()
    if prov.get("socscope_dirty") is True:
        fails.append("%s: %s says the SoCScope tree was DIRTY when these gates were "
                     "synthesised (rev %s) -- the sha does not name the RTL that was "
                     "built. Commit in SoCScope, re-synthesise, rebuild the overlay."
                     % (rel, PROVENANCE_NAME, prov_rev or "?"))
    if not prov_rev:
        fails.append("%s: %s records no socscope_rev" % (rel, PROVENANCE_NAME))
        return fails, notes
    if not (rev.startswith(prov_rev) or prov_rev.startswith(rev)):
        fails.append("%s: socscope_rev %s does NOT match the synth-time record %s in %s "
                     "-- the manifest was stamped from a tree that had moved on since "
                     "the RM was synthesised (that is the failure this file exists for)"
                     % (rel, rev, prov_rev, PROVENANCE_NAME))
    if not prov.get("sources"):
        notes.append("%s: %s lists no source digests -- sha256sum was unavailable on "
                     "the build host, so only git's word is recorded" % (rel, PROVENANCE_NAME))
    return fails, notes


def check_manifest(path: str, rel: str, home: str, head: str | None, waivers: dict,
                   require_provenance: bool = False):
    """Return (failures, notes) for one manifest at absolute `path`, reported as
    `rel`. Empty failures == OK."""
    fails, notes = [], []
    try:
        with open(path) as fh:
            m = json.load(fh)
    except (OSError, ValueError) as exc:
        return ["%s: unreadable manifest (%s)" % (rel, exc)], notes
    rm = str(m.get("rm_name", "?"))
    rev = m.get("socscope_rev")

    prov, prov_err = read_provenance(path)
    if prov_err:
        fails.append("%s: %s" % (rel, prov_err))
    elif prov is None and rm in CONSUMERS:
        msg = ("%s: no %s beside the manifest -- this overlay was built before the "
               "synth-time record existed, so its socscope_rev is only as good as the "
               "procedure that stamped it" % (rel, PROVENANCE_NAME))
        (fails if require_provenance else notes).append(
            msg + (" (--require-provenance)" if require_provenance else ""))

    if rev is None:
        if rm in CONSUMERS:
            fails.append("%s: rm '%s' consumes SoCScope but records no socscope_rev "
                         "(rebuild with `make -C fpga/dfx overlays`, which stamps it)"
                         % (rel, rm))
        return fails, notes

    rev = str(rev).strip()
    if not _REV_RX.match(rev):
        fails.append("%s: socscope_rev %r is malformed (want <sha>, <sha>-dirty or unknown)"
                     % (rel, rev))
        return fails, notes

    if rev.endswith("-dirty"):
        fails.append("%s: socscope_rev %s -- built from a DIRTY SoCScope tree, so the "
                     "sha does not name the RTL that was synthesised. Commit in "
                     "SoCScope, rebuild the overlay, re-run `make overlays`." % (rel, rev))
        return fails, notes

    if prov is not None:
        f, n = check_provenance(rel, rev, prov)
        fails += f
        notes += n

    if rev == "unknown":
        if rm in waivers:
            notes.append("%s: socscope_rev unknown, WAIVED: %s" % (rel, waivers[rm]))
        else:
            fails.append("%s: socscope_rev is 'unknown' and rm '%s' is not waived in %s "
                         "(a waiver needs a reason; the fix is a rebuild, not a waiver)"
                         % (rel, rm, DEFAULT_WAIVERS))
        return fails, notes

    if head is None:
        notes.append("%s: socscope_rev %s NOT resolved -- no SoCScope checkout at %s"
                     % (rel, rev, home))
        return fails, notes

    if not resolves(home, rev):
        fails.append("%s: socscope_rev %s does not resolve to a commit in %s "
                     "(wrong checkout, unfetched history, or a sha that never existed)"
                     % (rel, rev, home))
        return fails, notes

    if not head.startswith(rev):
        notes.append("%s: socscope_rev %s != checkout HEAD %s -- the overlay PREDATES "
                     "the checkout; rebuild before trusting a capture against this RTL"
                     % (rel, rev, head[:12]))
    return fails, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--socscope-home", default=None,
                    help="SoCScope checkout (default: $SOCSCOPE_HOME, else <repo>/../SoCScope)")
    ap.add_argument("--waivers", default=None,
                    help="waiver file (default: <repo>/%s)" % DEFAULT_WAIVERS)
    ap.add_argument("--require-checkout", action="store_true",
                    help="FAIL (rather than NOTE) when the SoCScope checkout is absent")
    ap.add_argument("--require-provenance", action="store_true",
                    help="FAIL (rather than NOTE) when a consumer overlay carries no "
                         "%s -- turn this on once every consumer has been rebuilt "
                         "through ooc_synth.tcl" % PROVENANCE_NAME)
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    home = os.path.abspath(args.socscope_home or default_socscope_home(repo))
    waivers = load_waivers(args.waivers or os.path.join(repo, DEFAULT_WAIVERS))
    head = checkout_head(home)

    print("== socscope overlay provenance gate ==")
    print("   SOCSCOPE_HOME %s" % home)
    print("   HEAD          %s" % (head[:12] if head else "<no checkout>"))
    print("   consumers     %s" % ", ".join(CONSUMERS))

    manifests = sorted(glob.glob(os.path.join(repo, "fpga", "dfx", "overlay", "*", "manifest.json")))
    if not manifests:
        print("   SKIP: no fpga/dfx/overlay/*/manifest.json")
        return 0

    fails, notes = [], []
    if head is None:
        msg = "no SoCScope checkout at %s -- sha resolution NOT checked" % home
        if args.require_checkout:
            fails.append(msg + " (--require-checkout)")
        else:
            notes.append("NOTE: " + msg + " (structural checks still run)")

    for path in manifests:
        f, n = check_manifest(path, os.path.relpath(path, repo), home, head, waivers,
                              require_provenance=args.require_provenance)
        fails += f
        notes += n

    for n in notes:
        print("   " + n)
    if fails:
        print("\nFAIL: %d socscope provenance problem(s)" % len(fails))
        for f in fails:
            print("  " + f)
        return 1
    print("\nOK: every SoCScope-consuming overlay records a usable socscope_rev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
