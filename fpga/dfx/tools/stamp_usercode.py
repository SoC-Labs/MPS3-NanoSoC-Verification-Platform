#!/usr/bin/env python3
"""stamp_usercode.py -- bind every overlay to the BITSTREAM BUILD it was keyed to.

    stamp   read USERID out of the full static .bit and write `static_usercode`
            into every overlay/<rm>/manifest.json
    check   the same comparison, writing nothing (a gate)

WHY
---
`static_id` is a CRC of `static_routed_locked.dcp`, and the shell compares it
against a value COMPILED INTO ITS OWN FIRMWARE. So it identifies the DESIGN, not
the IMPLEMENTATION, and it reads "correct" no matter which implementation of that
design is actually flown. One synthesis implemented twice yields two
bitstreams with the SAME static_id and incompatible routing; loading a partial
from one onto the other destroys the whole FPGA configuration -- which is what
happened, twice, on 2026-07-24 (scripts/harness_gates/check_image_overlay_match.py).

`BITSTREAM.CONFIG.USERID` does discriminate: build_dfx.tcl stamps the 8-hex build
commit into the FULL bitstream, and the device reports it back over JTAG as
`REGISTER.USERCODE`. `gen_manifest.py` has carried a `static_usercode` field for
it since c0784ed, `pyverify`'s Overlay reads it, and check_image_overlay_match.py
gates it -- but `make -C fpga/dfx overlays` never passed it, so NOT ONE overlay in
fpga/dfx/overlay/ carries the field. A guard with no data in it is not a guard.
This is the stage that puts the data there.

USR_ACCESS is deliberately NOT used for this. It carries HARNESS_VER32 -- the
harness *version*, identical across different implementation runs of the same
version -- so it cannot tell two impls apart. USERID can. (USR_ACCESS answers a
different question, firmware/bitstream skew: docs/VERSIONING_PLAN.md §3.4.)

TWO INDEPENDENT DERIVATIONS, AND THEY MUST AGREE
------------------------------------------------
The value is read from the **artefact** -- the ASCII `UserID=` token in the full
.bit's header -- using gen_manifest.py's own extractor, so there is exactly one
implementation of that parse in the tree. If build_dfx.tcl also left a
`static_stamp.json` beside the bitstream (what it INTENDED to stamp), that is
cross-checked against the artefact and a disagreement is fatal. A value that two
independent paths agree on is evidence; a value only one path knows is a claim.

UNSTAMPED IS REFUSED. An unstamped bitstream reads 0xFFFFFFFF, which is a
legitimate hardware value and a useless identity -- it is shared by every
unstamped design in the world, so recording it would make the guard pass on
exactly the builds it exists to catch. `--allow-unstamped` if you really mean it.

Stdlib only (plus gen_manifest.py, its sibling). No Vivado, no board.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DFX_DIR = os.path.dirname(HERE)
sys.path.insert(0, DFX_DIR)

# ONE parser for the .bit header, not a third copy of it.
from gen_manifest import usercode_from_bitstream, _norm_hex32  # noqa: E402
from pathlib import Path  # noqa: E402

UNSTAMPED = "0xFFFFFFFF"


def read_stamp_json(path):
    """build_dfx.tcl's record of what it INTENDED to stamp, or None."""
    if not path or not os.path.isfile(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def resolve_usercode(static_bit, stamp_json_path, allow_unstamped):
    """The usercode, from the artefact, cross-checked against the build's own
    record of it. Returns (usercode, provenance)."""
    from_bit = usercode_from_bitstream(Path(static_bit))
    provenance = "%s header UserID=" % os.path.basename(static_bit)

    stamp = read_stamp_json(stamp_json_path)
    if stamp is not None:
        declared = stamp.get("usercode")
        if declared is None:
            raise SystemExit(
                "stamp_usercode: %s exists but records no `usercode` -- it was "
                "written by a build that stamped nothing (DFX_NO_VERSION_STAMP), "
                "so there is no identity to bind the overlays to."
                % stamp_json_path)
        declared = _norm_hex32(declared)
        if declared != from_bit:
            raise SystemExit(
                "stamp_usercode: the build and the bitstream DISAGREE about the "
                "usercode.\n  %s says %s\n  %s says %s\n"
                "  These are the same stamp read two ways; they cannot differ "
                "unless the .bit beside the record is not the one the record "
                "describes." % (stamp_json_path, declared, provenance, from_bit))
        provenance += " (agrees with %s)" % os.path.basename(stamp_json_path)

    if from_bit == UNSTAMPED and not allow_unstamped:
        raise SystemExit(
            "stamp_usercode: %s carries no USERID stamp (%s). Every unstamped "
            "design in the world reads that value, so it cannot bind an overlay "
            "to an implementation -- recording it would make "
            "check_image_overlay_match.py pass on exactly the builds it exists "
            "to catch. Build without DFX_NO_VERSION_STAMP, or pass "
            "--allow-unstamped if you know what you are giving up."
            % (static_bit, UNSTAMPED))
    return from_bit, provenance


def overlay_manifests(overlay_root):
    root = Path(overlay_root)
    if not root.is_dir():
        raise SystemExit("stamp_usercode: no such overlay root: %s" % overlay_root)
    found = sorted(root.glob("*/manifest.json"))
    if not found:
        raise SystemExit(
            "stamp_usercode: no overlay/*/manifest.json under %s -- run "
            "`make -C fpga/dfx overlays` first (this stage stamps what that "
            "one wrote; it never creates a manifest)." % overlay_root)
    return found


def run(args):
    usercode, provenance = resolve_usercode(args.static_bit, args.stamp_json,
                                            args.allow_unstamped)
    print("stamp_usercode: static_usercode = %s  (from %s)" % (usercode, provenance))

    changed, already, wrong = [], [], []
    for path in overlay_manifests(args.overlay_root):
        data = json.loads(path.read_text())
        current = data.get("static_usercode")
        rel = os.path.relpath(str(path), args.repo) if args.repo else str(path)
        if current is not None and _norm_hex32(current) == usercode:
            already.append(rel)
            continue
        if args.check:
            wrong.append((rel, current))
            continue
        data["static_usercode"] = usercode
        # Same shape gen_manifest.py's write_manifest() emits, so a stamped
        # manifest is byte-for-byte what a `--static-bit` build would have
        # written and nothing downstream can tell the two apart.
        path.write_text(json.dumps(data, indent=2) + "\n")
        changed.append(rel)

    for rel in changed:
        print("  STAMPED %s" % rel)
    for rel in already:
        print("  ok      %s" % rel)

    if wrong:
        sys.stderr.write(
            "stamp_usercode: %d overlay(s) are NOT bound to this bitstream "
            "build (%s):\n" % (len(wrong), usercode))
        for rel, current in wrong:
            sys.stderr.write(
                "  - %s: static_usercode=%s\n"
                % (rel, "<absent>" if current is None else current))
        sys.stderr.write(
            "  An overlay with no static_usercode cannot be refused when the "
            "wrong static is flown -- and a WRONG one is worse: it was keyed "
            "to a different implementation of this same design.\n"
            "  Fix: make -C fpga/dfx overlay-usercode BUILD=<the build dir>\n")
        return 1

    print("stamp_usercode: %d stamped, %d already correct" % (len(changed), len(already)))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--overlay-root", required=True,
                   help="the directory holding overlay/<rm>/manifest.json")
    p.add_argument("--static-bit", required=True,
                   help="the FULL static bitstream this overlay set was routed "
                        "against (<prod>/config_rm_greybox.bit)")
    p.add_argument("--stamp-json", default=None,
                   help="build_dfx.tcl's static_stamp.json, cross-checked "
                        "against the bitstream header when present")
    p.add_argument("--repo", default=None, help="print paths relative to this")
    p.add_argument("--check", action="store_true",
                   help="compare only; write nothing; non-zero on a mismatch")
    p.add_argument("--allow-unstamped", action="store_true",
                   help="accept 0xFFFFFFFF (an identity shared by every "
                        "unstamped design) -- see the module docstring")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
