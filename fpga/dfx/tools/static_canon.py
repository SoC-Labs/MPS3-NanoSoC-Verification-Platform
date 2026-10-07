#!/usr/bin/env python3
"""static_canon.py -- the CONTENT identity of the static shell.

    check    the declaration in static_inputs.txt (three rules, below)
    list     the tracked files that reach the static netlist, one per line
    explain  print the canonical manifest the digest is taken over
    compute  write static_canon.json
    verify   recompute and compare against a static_canon.json

WHY IT EXISTS
-------------
`static_id` is the CRC-32 of `static_routed_locked.dcp`. A .dcp is a zip with
embedded timestamps, so the SAME sources built twice mint two different ids.
build_dfx.tcl says so in as many words: "changes whenever the static changes --
INCLUDING on a mere rebuild of an unchanged shell". That is the right property
for a COMPATIBILITY key (be conservative; strand the overlays rather than load a
partial onto a fabric it was not routed against), and it leaves two questions
with no answer anywhere in the repo:

    "is this the same shell?"        -- the id says no even when nothing moved
    "did anything actually change?"  -- nothing on disk can say

`static_canon` answers both, and answers them WITHOUT Vivado. It is a SHA-256
over a canonical text manifest of the sources that decide the static, the part,
the tool version and the build flags -- no timestamps, no artefacts, nothing a
rebuild perturbs.

    static_id    = CRC-32(one built artefact)   "will this partial FIT?"
    static_canon = SHA-256(the inputs)          "are these the same INPUTS?"
    HARNESS_VER32= semantic release             "WHICH RELEASE is this?"

Three axes, three questions. `static_id` is untouched by this file -- it is
baked into every fielded overlay, and changing it is a re-mint.

THE DIGEST, EXACTLY
-------------------
The manifest is UTF-8 text, LF line endings, every line terminated:

    mps3-static-canon/1
    part\t<part>
    tool\tvivado\t<version>
    flag\t<NAME>=<VALUE>                  (sorted by NAME, bytewise)
    src\t<sha256-hex>\t<repo-relative path>   (sorted by path, bytewise)

    digest = sha256(that text), lowercase hex, 64 chars

Normalisation, stated so two sites can agree:
  - paths are repo-relative, '/'-separated, no './' prefix;
  - sorting is BYTEWISE on the UTF-8 path (never locale collation);
  - file bytes are hashed RAW. No line-ending or whitespace normalisation:
    a CRLF that appears in a .tcl or .xdc is a change to what Vivado reads, so
    it must be a change to the digest;
  - the file list comes from `git ls-files` (TRACKED files), the CONTENT from
    the working tree. So an uncommitted edit moves the digest -- which is
    correct, that edit is what Vivado would read;
  - flag values are recorded verbatim, including the empty string. An ABSENT
    flag and a flag set to "" are different states and hash differently.

WHAT IS DELIBERATELY *NOT* IN THE DIGEST
----------------------------------------
`linked_static_dcp` -- the shell checkpoint a DFX run consumed -- is recorded
BESIDE the digest and hashed into nothing. It is a build artefact with embedded
timestamps; feeding it in would reintroduce the exact defect this file exists to
remove. Same for anything else produced by a tool rather than written by a
human.

CONSERVATIVE, AND SAYING SO
---------------------------
A flag-gated input (the touch XDC, the real-PHY XDC) is hashed whether or not
the flag that would pull it in is set. So the digest can move when a change
could not have reached the netlist. That direction is safe -- it says "look
again" -- and it is the same direction static_id already errs in. The digest
never fails to move when a declared input changes.

WHAT IT CANNOT PROMISE
----------------------
An identical digest means the INPUTS were identical. It does NOT promise Vivado
produced a bit-identical implementation from them -- place-and-route is not
guaranteed reproducible across hosts or tool patch levels. The claim is exactly
"same inputs", which is the claim static_id cannot make at all.

Stdlib only. No Vivado, no board, no network (git is shelled out to for the
file list, and its absence is reported, never guessed around).
"""

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys

SCHEMA = "mps3-static-canon"
SCHEMA_VERSION = "1"
MANIFEST_HEADER = "%s/%s" % (SCHEMA, SCHEMA_VERSION)
GENERATED_BY = "fpga/dfx/tools/static_canon.py"

DEFAULT_DECL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static_inputs.txt")
BUILD_DFX_TCL = os.path.join("fpga", "dfx", "build_dfx.tcl")

#: rule 2's scope -- what counts as a design source under a declared root.
DESIGN_SOURCE_EXT = (".sv", ".v", ".svh", ".vh", ".xdc", ".tcl", ".xml",
                     ".yaml", ".yml")


class DeclarationError(SystemExit):
    pass


# ---------------------------------------------------------------------------
# the declaration
# ---------------------------------------------------------------------------
def parse_declaration(path):
    """static_inputs.txt -> {'roots': [...], 'includes': [...],
                             'excludes': [(glob, reason)]}"""
    roots, includes, excludes = [], [], []
    with open(path) as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise DeclarationError(
                    "%s:%d: expected '<directive> <glob>', got %r"
                    % (path, lineno, line))
            directive, rest = parts[0], parts[1].strip()
            if directive == "root":
                roots.append(rest)
            elif directive == "include":
                if "#" in rest:
                    rest = rest.split("#", 1)[0].strip()
                includes.append(rest)
            elif directive == "exclude":
                glob, sep, reason = rest.partition("#")
                glob = glob.strip()
                reason = reason.strip()
                if not sep or not reason:
                    # RULE 3. An exclusion is a claim about the build ("this
                    # file cannot reach the netlist"). Unexplained, it is
                    # indistinguishable from someone silencing a failing gate.
                    raise DeclarationError(
                        "%s:%d: exclude %r carries no '# reason'. Every "
                        "exclusion must say WHY that file does not reach the "
                        "static netlist." % (path, lineno, glob))
                excludes.append((glob, reason))
            else:
                raise DeclarationError(
                    "%s:%d: unknown directive %r (want root|include|exclude)"
                    % (path, lineno, directive))
    if not includes:
        raise DeclarationError("%s declares no `include` -- nothing would be "
                               "hashed" % path)
    return {"roots": roots, "includes": includes, "excludes": excludes}


def tracked_files(repo):
    """Every file git tracks, repo-relative, '/'-separated."""
    try:
        out = subprocess.check_output(["git", "-C", repo, "ls-files", "-z"],
                                      stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(
            "static_canon: `git ls-files` failed in %s (%s). The canonical "
            "hash is defined over TRACKED sources; outside a git checkout "
            "there is no such set and this tool will not invent one." % (repo, exc))
    return sorted(p.decode("utf-8") for p in out.split(b"\0") if p)


def _matches(path, globs):
    for glob in globs:
        if fnmatch.fnmatchcase(path, glob):
            return glob
    return None


def classify(decl, files):
    """-> (hashed, undeclared, stale_includes)

    include beats exclude (so one broad exclude can carry the rationale for a
    subtree while a single file inside it is still hashed).
    """
    include_globs = decl["includes"]
    exclude_globs = [g for g, _ in decl["excludes"]]

    hashed, hit_include = [], set()
    for path in files:
        glob = _matches(path, include_globs)
        if glob:
            hashed.append(path)
            hit_include.add(glob)

    stale = [g for g in include_globs if g not in hit_include]

    hashed_set = set(hashed)
    roots = [r.rstrip("/") for r in decl["roots"]]
    undeclared = []
    for path in files:
        if path in hashed_set:
            continue
        if not path.lower().endswith(DESIGN_SOURCE_EXT):
            continue
        if not any(path == r or path.startswith(r + "/") for r in roots):
            continue
        if _matches(path, exclude_globs):
            continue
        undeclared.append(path)

    return hashed, undeclared, stale


def check_declaration(decl, files):
    """The three rules. Returns a list of problems (empty == OK)."""
    hashed, undeclared, stale = classify(decl, files)
    problems = []
    for glob in stale:
        # RULE 1. A glob that matches nothing looks EXACTLY like a healthy one
        # from the outside: the digest is computed, the tool exits 0, and the
        # thing it was written to cover is no longer covered by anything.
        problems.append(
            "include %r matches no tracked file -- a declaration that has "
            "silently stopped covering anything" % glob)
    for path in sorted(undeclared):
        # RULE 2. This is what stops the hash from rotting: a new design source
        # under a declared root is a hard failure naming the file, not a silent
        # omission from the digest.
        problems.append(
            "%s is a design source under a declared root and is neither "
            "`include`d nor `exclude`d -- declare it (it reaches the static) "
            "or exclude it WITH the reason it does not" % path)
    if not hashed:
        problems.append("no tracked file is hashed at all")
    return problems


# ---------------------------------------------------------------------------
# the digest
# ---------------------------------------------------------------------------
def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_digests(repo, hashed):
    """[(repo-relative path, sha256)], sorted BYTEWISE by path."""
    out = []
    for rel in sorted(hashed):
        abs_path = os.path.join(repo, rel)
        if not os.path.isfile(abs_path):
            raise SystemExit(
                "static_canon: %s is tracked but missing from the working "
                "tree. The digest is over what Vivado would READ; it will not "
                "be computed over a file that is not there." % rel)
        out.append((rel, sha256_of(abs_path)))
    return out


def canonical_manifest(digests, part, vivado, flags):
    """The exact bytes the digest is taken over. See the module docstring."""
    lines = ["%s\n" % MANIFEST_HEADER,
             "part\t%s\n" % part,
             "tool\tvivado\t%s\n" % vivado]
    for name in sorted(flags):                      # bytewise, not locale
        lines.append("flag\t%s=%s\n" % (name, flags[name]))
    for rel, digest in digests:
        lines.append("src\t%s\t%s\n" % (digest, rel))
    return "".join(lines)


def part_from_build_dfx(repo):
    """The part, read from the ONE place that sets it (build_dfx.tcl), so the
    digest cannot be taken against a part nobody builds for."""
    path = os.path.join(repo, BUILD_DFX_TCL)
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("set part "):
                    return line.split(None, 2)[2].strip()
    except OSError:
        return None
    return None


def parse_flags(pairs):
    flags = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit("static_canon: --flag wants NAME=VALUE, got %r" % item)
        name, _, value = item.partition("=")
        flags[name.strip()] = value.strip()
    return flags


def compute(repo, decl_path, part, vivado, flags, linked_dcp=None):
    decl = parse_declaration(decl_path)
    files = tracked_files(repo)
    problems = check_declaration(decl, files)
    if problems:
        raise SystemExit(_problem_report(decl_path, problems))
    hashed, _, _ = classify(decl, files)

    digests = source_digests(repo, hashed)
    manifest = canonical_manifest(digests, part, vivado, flags)
    digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()

    record = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        "digest": digest,
        "short": digest[:12],
        "part": part,
        "vivado": vivado,
        "flags": dict(flags),
        "input_count": len(digests),
        "manifest_bytes": len(manifest.encode("utf-8")),
        "inputs": [{"path": rel, "sha256": d} for rel, d in digests],
        # NOT hashed. See "WHAT IS DELIBERATELY *NOT* IN THE DIGEST".
        "linked_static_dcp": None,
    }
    if linked_dcp:
        if not os.path.isfile(linked_dcp):
            raise SystemExit("static_canon: --linked-dcp not found: %s" % linked_dcp)
        record["linked_static_dcp"] = {
            "name": os.path.basename(linked_dcp),
            "sha256": sha256_of(linked_dcp),
            "bytes": os.path.getsize(linked_dcp),
            "note": "the shell checkpoint this DFX run linked against; RECORDED, "
                    "NOT HASHED -- it is a timestamped artefact and hashing it "
                    "would reintroduce the defect static_canon removes",
        }
    return record, manifest


def _problem_report(decl_path, problems):
    out = ["static_canon: %s FAILED %d declaration check(s):"
           % (decl_path, len(problems))]
    out.extend("  - %s" % p for p in problems)
    out.append("  (see the header of %s: the three rules, and why each one is "
               "a hard failure)" % decl_path)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _resolve_part(args, repo):
    if args.part:
        return args.part
    part = part_from_build_dfx(repo)
    if not part:
        raise SystemExit(
            "static_canon: no `set part ` line in %s and no --part given. The "
            "digest will not be taken against a guessed part." % BUILD_DFX_TCL)
    return part


def cmd_check(args):
    repo = os.path.abspath(args.repo)
    decl = parse_declaration(args.decl)
    files = tracked_files(repo)
    problems = check_declaration(decl, files)
    if problems:
        sys.stderr.write(_problem_report(args.decl, problems) + "\n")
        return 1
    hashed, _, _ = classify(decl, files)
    print("static_canon: declaration OK (%d tracked file(s) reach the static "
          "netlist; %d root(s), %d exclusion(s) each with a reason)"
          % (len(hashed), len(decl["roots"]), len(decl["excludes"])))
    return 0


def cmd_list(args):
    repo = os.path.abspath(args.repo)
    decl = parse_declaration(args.decl)
    files = tracked_files(repo)
    problems = check_declaration(decl, files)
    if problems:
        sys.stderr.write(_problem_report(args.decl, problems) + "\n")
        return 1
    hashed, _, _ = classify(decl, files)
    for rel in sorted(hashed):
        print(os.path.join(repo, rel) if args.absolute else rel)
    return 0


def cmd_explain(args):
    repo = os.path.abspath(args.repo)
    _, manifest = compute(repo, args.decl, _resolve_part(args, repo),
                          args.vivado, parse_flags(args.flag))
    sys.stdout.write(manifest)
    return 0


def cmd_compute(args):
    repo = os.path.abspath(args.repo)
    record, _ = compute(repo, args.decl, _resolve_part(args, repo),
                        args.vivado, parse_flags(args.flag), args.linked_dcp)
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print("static_canon: wrote %s (digest %s, %d inputs)"
              % (args.out, record["short"], record["input_count"]))
    else:
        sys.stdout.write(text)
    return 0


def cmd_verify(args):
    repo = os.path.abspath(args.repo)
    with open(args.record) as fh:
        recorded = json.load(fh)
    if recorded.get("schema") != SCHEMA:
        sys.stderr.write("static_canon: %s is not a %s record\n"
                         % (args.record, SCHEMA))
        return 1
    fresh, _ = compute(repo, args.decl, recorded.get("part"),
                       recorded.get("vivado"), recorded.get("flags") or {})
    if fresh["digest"] != recorded.get("digest"):
        sys.stderr.write(
            "static_canon: %s DIGEST MISMATCH\n  recorded %s\n  sources  %s\n"
            "  -> the declared inputs are not the ones this record was taken "
            "over. Something in the static's sources moved since.\n"
            % (args.record, recorded.get("digest"), fresh["digest"]))
        return 1
    print("static_canon: %s OK (digest %s, %d inputs, sources unchanged)"
          % (args.record, fresh["short"], fresh["input_count"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")

    def common(p, want_build_args=False):
        p.add_argument("--repo", default=os.getcwd())
        p.add_argument("--decl", default=DEFAULT_DECL,
                       help="the input declaration (default: %s)" % DEFAULT_DECL)
        if want_build_args:
            p.add_argument("--part", default=None,
                           help="default: the `set part` line in " + BUILD_DFX_TCL)
            p.add_argument("--vivado", required=True,
                           help="the toolchain version; REQUIRED -- two builds "
                                "under different Vivados are not the same build")
            p.add_argument("--flag", action="append", default=[],
                           metavar="NAME=VALUE",
                           help="a build flag that changes the netlist "
                                "(SHELL_TOUCH, SHELL_REALPHY, ...)")

    ck = sub.add_parser("check", help="check static_inputs.txt (the three rules)")
    common(ck)
    ck.set_defaults(func=cmd_check)

    ls = sub.add_parser("list", help="print the files that reach the static")
    common(ls)
    ls.add_argument("--absolute", action="store_true")
    ls.set_defaults(func=cmd_list)

    ex = sub.add_parser("explain", help="print the canonical manifest text")
    common(ex, want_build_args=True)
    ex.set_defaults(func=cmd_explain)

    co = sub.add_parser("compute", help="write static_canon.json")
    common(co, want_build_args=True)
    co.add_argument("--linked-dcp", default=None,
                    help="the shell checkpoint this DFX run linked against "
                         "(RECORDED beside the digest, never hashed into it)")
    co.add_argument("-o", "--out", default=None)
    co.set_defaults(func=cmd_compute)

    vf = sub.add_parser("verify", help="recompute and compare a static_canon.json")
    common(vf)
    vf.add_argument("record")
    vf.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
