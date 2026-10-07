#!/usr/bin/env python3
"""
gen_version.py — the single source of truth for the HARNESS version.

The harness is the STATIC half of the platform: the shell bitstream
(fpga/shell + fpga/dfx) plus the MicroBlaze firmware baked into it. This
script turns the repo-root `VERSION` file plus git provenance into:

  1. firmware/platform/generated/mps3_version.c   strong overrides for the
  2. firmware/platform/generated/mps3_version.h   mps3_harness_*() seam
  3. `--print <field>`                            scalars for Make / Tcl

exactly mirroring firmware/platform/gen_greybox_blob.py -> generated/
greybox_blob.c (which likewise emits the strong `mps3_shell_static_id()`
override for coordinator.c's weak fallback). Both generated files are
gitignored (firmware/platform/.gitignore: `generated/`) and regenerated on
every firmware build, so provenance can never go stale.

WHY IT LIVES IN scripts/ AND NOT firmware/platform/ (where gen_greybox_blob.py
lives): this generator has TWO consumers, not one. The firmware build reads it
(via firmware/platform/Makefile) and so does the BITSTREAM build
(fpga/dfx/build_dfx.tcl, which `exec`s `--print ver32` to stamp
BITSTREAM.CONFIG.USR_ACCESS). scripts/ is the neutral home both can reach
without one area's Makefile reaching into the other's.

------------------------------------------------------------------------------
HARNESS_VER32 — the packed 32-bit encoding (docs/VERSIONING_PLAN.md §3.1)
------------------------------------------------------------------------------

     31           24 23           16 15            8 7             0
    +---------------+---------------+---------------+---------------+
    |     major     |     minor     |     patch     |     flags     |
    +---------------+---------------+---------------+---------------+

    flags bit0 = dirty working tree
    flags bits[7:1] = RESERVED, always 0

    v1.4.2 clean -> 0x01040200        v1.4.2 dirty -> 0x01040201

Chosen to be legible in hex: the version reads straight out of a JTAG
USR_ACCESS readback with no decoding.

DEVIATION FROM THE PLAN, DELIBERATE: VERSIONING_PLAN.md §3.1 also proposed
`flags bit1 = local (non-CI) build`. It is NOT implemented, and bit1 is left
reserved. Setting it would mean a clean v1.4.2 encodes as 0x01040202 on an
engineer's machine and 0x01040200 only in CI — i.e. the documented invariant
"v1.4.2 clean => 0x01040200" would silently not hold for the overwhelmingly
common case. One number, one meaning. If a CI/local distinction is ever wanted,
bit1 is still free and the seam macros already mask flags out.

------------------------------------------------------------------------------
`static_id` IS NOT REPLACED — the two axes are orthogonal
------------------------------------------------------------------------------
`static_id` (CRC-32 of static_routed_locked.dcp) answers "will this partial FIT
this fabric?" — a compatibility fingerprint, unordered. HARNESS_VER32 answers
"WHICH RELEASE is this?" — a human-ordered semantic version. One `static_id`
legitimately serves SEVERAL harness versions: a firmware-only bump changes the
.bit (firmware is baked in via updatemem) while the static routing — and hence
partial compatibility — is untouched. That is why this generator reads VERSION
and git, and never reads or writes static_id.

Usage:
  python3 scripts/gen_version.py                       # emit the .c + .h
  python3 scripts/gen_version.py --print ver32         # 0x01000000
  python3 scripts/gen_version.py --print version       # 1.0.0
  python3 scripts/gen_version.py --print sha           # a1b2c3d4 | unknown
  python3 scripts/gen_version.py --print dirty         # 0 | 1
  python3 scripts/gen_version.py --print flags         # 0x00
  python3 scripts/gen_version.py --print date          # 2026-07-14

Reproducible builds: if SOURCE_DATE_EPOCH is set, the build date is derived
from it (UTC) instead of "now".

*** KEEP THIS PYTHON 3.8 COMPATIBLE. *** fpga/dfx/build_dfx.tcl `exec`s this
script during the bitstream build, and `python3` on Vivado 2024.1's PATH is
Vivado's OWN bundled interpreter (tps/lnx64/python-3.8.3). Measured: that is the
only interpreter that runs under Vivado's environment -- forcing /usr/bin/python3
or scrubbing LD_LIBRARY_PATH both fail (see build_dfx.tcl's harness_version proc
for the full finding). So: no walrus in comprehensions, no `X | Y` type unions,
no `list[str]` builtin generics, nothing newer than 3.8.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: flags bit0 — the working tree had uncommitted changes at generation time.
FLAG_DIRTY = 0x01

#: `major.minor.patch`, each 0..255 (one byte apiece in HARNESS_VER32).
_SEMVER_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def read_version(version_file: Path) -> tuple:
    """Parse the repo-root VERSION file -> (major, minor, patch).

    Strict on purpose: a typo'd VERSION must fail the build loudly, not
    silently stamp a wrong number into a bitstream that then goes to a board.
    """
    if not version_file.is_file():
        sys.exit(f"gen_version.py: {version_file} not found "
                 "(the harness version is the repo-root VERSION file)")
    raw = version_file.read_text().strip()
    m = _SEMVER_RE.match(raw)
    if not m:
        sys.exit(f"gen_version.py: {version_file} holds {raw!r}, "
                 "expected a bare semver 'major.minor.patch' (each 0..255), e.g. 1.4.2")
    parts = tuple(int(g) for g in m.groups())
    for name, val in zip(("major", "minor", "patch"), parts):
        if val > 255:
            sys.exit(f"gen_version.py: {version_file}: {name}={val} exceeds 255 "
                     "(each field is one byte of HARNESS_VER32)")
    return parts


def _git(*args, cwd: Path):
    """Run a git command; return stripped stdout, or None if git/repo is unusable.

    Never raises: a tarball export with no .git must still build (provenance
    degrades to 'unknown', the version itself still comes from VERSION).
    """
    try:
        out = subprocess.run(("git",) + args, cwd=str(cwd), check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.decode("utf-8", "replace").strip()


def git_provenance(repo: Path) -> tuple:
    """-> (sha8, dirty). sha8 is 8 lowercase hex, or 'unknown' outside a repo."""
    sha = _git("rev-parse", "--short=8", "HEAD", cwd=repo)
    if not sha or not re.fullmatch(r"[0-9a-f]{1,8}", sha):
        # No git, no commits yet, or a shallow/exported tree.
        return "unknown", False
    sha = sha.rjust(8, "0")

    # `git status --porcelain` lists modified-tracked AND untracked-not-ignored
    # files, and respects .gitignore -- so the generated/ outputs of this very
    # script (gitignored) can never mark the tree dirty, but a genuinely
    # uncommitted source edit does. `None` (git failed) is treated as NOT dirty
    # so a repo-less export does not claim a dirty tree it cannot observe.
    status = _git("status", "--porcelain", cwd=repo)
    return sha, bool(status)


def build_date() -> str:
    """UTC YYYY-MM-DD, honouring SOURCE_DATE_EPOCH for reproducible builds."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            dt = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            pass
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def pack_ver32(major: int, minor: int, patch: int, flags: int) -> int:
    """HARNESS_VER32 = major<<24 | minor<<16 | patch<<8 | flags.

    The ONE definition of the encoding on the Python side; the C side's
    MPS3_HARNESS_PACK_VER32() macro (firmware/platform/mps3_version.h) is the
    mirror, and firmware/test/test_version.c pins the two together against the
    same worked example (v1.4.2 clean == 0x01040200). The bare name
    MPS3_HARNESS_VER32 belongs to the generated object-like constant holding
    THIS build's value (emit_header below) -- the number the USR_ACCESS stamp
    carries.
    """
    return ((major & 0xFF) << 24 | (minor & 0xFF) << 16
            | (patch & 0xFF) << 8 | (flags & 0xFF))


def collect(repo: Path) -> dict:
    major, minor, patch = read_version(repo / "VERSION")
    sha, dirty = git_provenance(repo)
    flags = FLAG_DIRTY if dirty else 0
    return {
        "major": major, "minor": minor, "patch": patch,
        "version": f"{major}.{minor}.{patch}",
        "sha": sha,
        "dirty": dirty,
        "flags": flags,
        "ver32": pack_ver32(major, minor, patch, flags),
        "date": build_date(),
    }


def emit_header(v: dict) -> str:
    return f"""/*
 * mps3_version.h -- GENERATED by scripts/gen_version.py. DO NOT EDIT.
 *
 * Compile-time constants for this harness build. The SEAM (prototypes +
 * packing macros) lives in the committed firmware/platform/mps3_version.h,
 * which this file includes -- callers include the SEAM header, not this one,
 * so that firmware compiles with or without a generated identity present.
 *
 * Regenerate: python3 scripts/gen_version.py   (the firmware build does it
 * automatically -- firmware/platform/Makefile's `version` target).
 */
#ifndef MPS3_VERSION_GENERATED_H
#define MPS3_VERSION_GENERATED_H

#include "../mps3_version.h"

#define MPS3_HARNESS_VERSION_MAJOR  {v['major']}u
#define MPS3_HARNESS_VERSION_MINOR  {v['minor']}u
#define MPS3_HARNESS_VERSION_PATCH  {v['patch']}u
#define MPS3_HARNESS_VERSION_FLAGS  0x{v['flags']:02X}u
#define MPS3_HARNESS_VERSION_STR    "{v['version']}"

/* major<<24 | minor<<16 | patch<<8 | flags -- the value stamped into the
 * bitstream's USR_ACCESS register by fpga/dfx/build_dfx.tcl, so firmware and
 * fabric report the SAME number. */
#define MPS3_HARNESS_VER32          0x{v['ver32']:08X}u

#define MPS3_HARNESS_GIT_SHA        "{v['sha']}"
#define MPS3_HARNESS_DIRTY          {1 if v['dirty'] else 0}
#define MPS3_HARNESS_BUILD_DATE     "{v['date']}"

#endif /* MPS3_VERSION_GENERATED_H */
"""


def emit_source(v: dict) -> str:
    dirty_note = ("DIRTY working tree -- this image was NOT built from a clean commit"
                  if v["dirty"] else "clean working tree")
    return f"""/*
 * mps3_version.c -- GENERATED by scripts/gen_version.py. DO NOT EDIT.
 *
 * Strong overrides for the weak mps3_harness_*() seam
 * (firmware/platform/mps3_version_weak.c), exactly as
 * generated/greybox_blob.c strongly overrides coordinator.c's weak
 * mps3_shell_static_id(). A build WITHOUT this file still links -- it just
 * reports version 0.0.0 / sha "unknown" (the "not provisioned" answer).
 *
 * harness version : {v['version']}   ({dirty_note})
 * HARNESS_VER32   : 0x{v['ver32']:08X}
 * git             : {v['sha']}
 * built           : {v['date']}
 *
 * HARNESS_VER32 is ALSO stamped into the static bitstream's USR_ACCESS
 * register (fpga/dfx/build_dfx.tcl), which is what lets a board be
 * interrogated over JTAG when this firmware is not even running. A mismatch
 * between the two is exactly the firmware/bitstream skew this platform has
 * been bitten by before (a flashable base whose updatemem was not re-run).
 *
 * This file says NOTHING about static_id. The two are orthogonal axes:
 * static_id = "will this partial fit this fabric" (compatibility fingerprint);
 * harness version = "which release is this" (human-ordered). A firmware-only
 * bump moves THIS number and leaves static_id alone -- by design.
 */
#include "mps3_version.h"

uint32_t mps3_harness_version(void)
{{
    return MPS3_HARNESS_VER32;
}}

const char *mps3_harness_version_str(void)
{{
    return MPS3_HARNESS_VERSION_STR;
}}

const char *mps3_harness_git_sha(void)
{{
    return MPS3_HARNESS_GIT_SHA;
}}

bool mps3_harness_dirty(void)
{{
    return {"true" if v["dirty"] else "false"};
}}

const char *mps3_harness_build_date(void)
{{
    return MPS3_HARNESS_BUILD_DATE;
}}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--version-file", type=Path, default=REPO / "VERSION")
    ap.add_argument("--repo", type=Path, default=REPO,
                    help="repo root for git provenance (default: this script's repo)")
    ap.add_argument("--outdir", type=Path,
                    default=REPO / "firmware/platform/generated",
                    help="where mps3_version.{c,h} are written")
    ap.add_argument("--print", dest="print_field",
                    choices=("ver32", "version", "sha", "dirty", "flags",
                             "date", "major", "minor", "patch"),
                    help="print ONE field to stdout and exit (no files written) "
                         "-- how Make and build_dfx.tcl consume this")
    args = ap.parse_args()

    # --version-file is honoured independently of --repo so tests can point at a
    # synthetic VERSION while still exercising the real git provenance path.
    major, minor, patch = read_version(args.version_file)
    sha, dirty = git_provenance(args.repo)
    flags = FLAG_DIRTY if dirty else 0
    v = {
        "major": major, "minor": minor, "patch": patch,
        "version": f"{major}.{minor}.{patch}",
        "sha": sha, "dirty": dirty, "flags": flags,
        "ver32": pack_ver32(major, minor, patch, flags),
        "date": build_date(),
    }

    if args.print_field:
        field = args.print_field
        if field == "ver32":
            print(f"0x{v['ver32']:08X}")
        elif field == "flags":
            print(f"0x{v['flags']:02X}")
        elif field == "dirty":
            print("1" if v["dirty"] else "0")
        else:
            print(v[field])
        return

    args.outdir.mkdir(parents=True, exist_ok=True)
    (args.outdir / "mps3_version.h").write_text(emit_header(v))
    (args.outdir / "mps3_version.c").write_text(emit_source(v))
    print(f"wrote {args.outdir}/mps3_version.{{c,h}}: "
          f"v{v['version']} ver32=0x{v['ver32']:08X} sha={v['sha']} "
          f"dirty={int(v['dirty'])} built={v['date']}")


if __name__ == "__main__":
    main()
