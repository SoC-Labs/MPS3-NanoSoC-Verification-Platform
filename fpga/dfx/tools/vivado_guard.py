#!/usr/bin/env python3
"""vivado_guard.py -- ONE Vivado per build dir, and the right one for the CPU.

    mix      refuse a build dir (or a checkpoint about to be reused) that was
             written by a different Vivado than the one this run will use
    cpu      refuse a SHELL_CPU / Vivado pairing that cannot build (mbv < 2026.1)
    dcp      print the Vivado version that wrote one checkpoint

Silent and exit 0 when everything agrees; one line per problem and exit 1 when
not. The Makefile calls `mix` and `cpu` at PARSE time and turns any output into
a `$(error)`, so a refused plan never reaches a recipe -- and a clean one prints
exactly what it printed before this file existed.

WHY IT EXISTS
-------------
Two Vivados now build this platform. The bare-metal static is 2024.1; the
MicroBlaze V (Linux) static needs 2026.1, whose MBV IP revisions (S-mode, SSTC)
2024.1 does not have. The two do not mix, and the ways they fail are asymmetric:

  * a 2026.1 checkpoint opened in 2024.1 fails LOUDLY ("created with Vivado
    v2026.1 ... cannot be opened") -- an hour into the run, but loudly;
  * a 2024.1 checkpoint opened in 2026.1 opens SILENTLY. build_dfx.tcl's
    ensure_rm_synth_dcp reuses any <rm_key>_synth.dcp it finds in the prod dir,
    so a re-run under the other Vivado links a netlist the new one never
    synthesised. The July 2026 Linux run lost a build to exactly this
    ("CLEAR out_dir/*_synth.dcp between version attempts").

A checkpoint says which Vivado wrote it: `dcp.xml`, the first member of the
zip, carries `<PRODUCT Name="Vivado v2026.1 (64-bit)"/>`. That is read here
with the stdlib, no Vivado -- so the question "was everything in this build dir
written by the Vivado I am about to run?" is answered in milliseconds, before
the run, from the artefacts themselves. No stamp file to forget to write.

WHAT IT SCANS (deliberately NOT recursive: a Vivado project dir holds hundreds
of IP checkpoints, all written by the tool that wrote the project)
  --build DIR   DIR/*.dcp, DIR/prod/*.dcp, DIR/*_synth/*.dcp
  --dcp FILE    one checkpoint that the run will link or copy in (the shell
                checkpoint, a REUSE_DCPS_FROM / pre-produced RM checkpoint);
                absent files are skipped -- the run will build them itself

A file that is not a readable checkpoint (a truncated file, a test fixture) is
skipped, not refused: Vivado refuses those on its own, loudly. This guard
exists for the silent case.

Stdlib only, Python 3.6+. No Vivado, no board.
"""

import argparse
import glob
import os
import re
import struct
import sys
import zlib

#: `<PRODUCT Name="Vivado v2026.1 (64-bit)"/>` -> "2026.1"
PRODUCT_RE = re.compile(rb'<PRODUCT\s+Name="Vivado\s+v(\d{4}\.\d+(?:\.\d+)?)')

#: The MicroBlaze V needs these IP revisions (plan §1 fact 3). 2026.1 is the
#: version the July Linux static was built and proven with.
MBV_MIN = "2026.1"


def vkey(version):
    """'2026.1' -> (2026, 1); '2024.1.1' -> (2024, 1). Major.minor decides
    checkpoint compatibility; a patch level does not."""
    parts = [int(p) for p in version.split(".") if p.isdigit()]
    return tuple((parts + [0, 0])[:2])


def dcp_version(path):
    """The Vivado version that wrote a checkpoint, or None if `path` is not a
    readable checkpoint. Reads only dcp.xml (the first zip member)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(30)
            if len(head) < 30 or head[:4] != b"PK\x03\x04":
                return None
            (_sig, _ver, flags, method, _t, _d, _crc, csize, _usize,
             nlen, xlen) = struct.unpack("<IHHHHHIIIHH", head)
            name = fh.read(nlen)
            fh.read(xlen)
            if name != b"dcp.xml" or flags & 0x8:
                # dcp.xml is always first and always sized in its local header
                # in every checkpoint this repo has produced (2024.1 + 2026.1).
                # Anything else is not a shape this guard claims to read.
                return None
            data = fh.read(min(csize, 1 << 20))
        if method == 8:
            data = zlib.decompress(data, -15)
        elif method != 0:
            return None
    except (OSError, zlib.error, struct.error):
        return None
    m = PRODUCT_RE.search(data)
    return m.group(1).decode() if m else None


def build_dir_dcps(build):
    """The checkpoints a mint in `build` reads back: top level, prod/, and each
    RM stage dir. Not recursive (see the module docstring)."""
    pats = ("*.dcp", os.path.join("prod", "*.dcp"), os.path.join("*_synth", "*.dcp"))
    out = []
    for pat in pats:
        out.extend(sorted(glob.glob(os.path.join(build, pat))))
    return out


def cmd_mix(args):
    want = args.want
    problems = []
    seen = set()
    candidates = []
    for build in args.build or []:
        candidates.extend(build_dir_dcps(build))
    candidates.extend(p for p in (args.dcp or []) if p)
    for path in candidates:
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(real):
            continue
        seen.add(real)
        got = dcp_version(real)
        if got is None:
            continue
        if vkey(got) != vkey(want):
            problems.append("%s was written by Vivado %s, but this run uses %s"
                            % (path, got, want))
    if problems:
        for p in problems[:5]:
            print("VIVADO VERSION MIX: " + p)
        if len(problems) > 5:
            print("VIVADO VERSION MIX: ... and %d more checkpoint(s) like these"
                  % (len(problems) - 5))
        print("VIVADO VERSION MIX: one build dir, one Vivado. An older checkpoint opens "
              "SILENTLY in a newer Vivado and gets linked as-is; a newer one fails an "
              "hour in. Use a fresh BUILD (and SHELL_PROJ) per Vivado version, or run "
              "with the Vivado that wrote these. A pre-produced RM checkpoint of the "
              "other version is disabled with RM_SYNTH_REUSE_<rm_key>= on the command line.")
        return 1
    return 0


def cmd_cpu(args):
    cpu = (args.shell_cpu or "mb").strip() or "mb"
    if cpu not in ("mb", "mbv"):
        print("SHELL_CPU=%s: want mb (MicroBlaze v11, bare-metal -- the default) or "
              "mbv (MicroBlaze V + DDR4, Linux)" % cpu)
        return 1
    if cpu == "mbv" and vkey(args.vivado_ver) < vkey(MBV_MIN):
        print("SHELL_CPU=mbv needs Vivado >= %s (the MicroBlaze V S-mode/SSTC IP "
              "revisions), but this run would use %s (%s). Pass VIVADO=%s -- a "
              "%s MBV static cannot be built, and a 2026.1 checkpoint cannot be "
              "opened by %s afterwards."
              % (MBV_MIN, args.vivado_ver, args.vivado or "VIVADO unset",
                 MBV_MIN, args.vivado_ver, args.vivado_ver))
        return 1
    return 0


def cmd_dcp(args):
    rc = 0
    for path in args.dcp:
        got = dcp_version(path)
        print("%s %s" % (got or "unknown", path))
        if got is None:
            rc = 1
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")

    mx = sub.add_parser("mix", help="refuse checkpoints from another Vivado")
    mx.add_argument("--want", required=True, help="the Vivado version this run uses")
    mx.add_argument("--build", action="append", help="a build dir to scan")
    mx.add_argument("--dcp", action="append", help="one checkpoint the run will read")
    mx.set_defaults(func=cmd_mix)

    cp = sub.add_parser("cpu", help="refuse SHELL_CPU=mbv under Vivado < %s" % MBV_MIN)
    cp.add_argument("--shell-cpu", default="")
    cp.add_argument("--vivado-ver", required=True)
    cp.add_argument("--vivado", default="")
    cp.set_defaults(func=cmd_cpu)

    dc = sub.add_parser("dcp", help="print the Vivado that wrote each checkpoint")
    dc.add_argument("dcp", nargs="+")
    dc.set_defaults(func=cmd_dcp)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
