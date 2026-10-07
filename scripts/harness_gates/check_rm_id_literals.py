#!/usr/bin/env python3
"""check_rm_id_literals.py -- no rm_id literal may silently go stale.

THE HAZARD THIS EXISTS FOR
--------------------------
Since the v2 encoding (2026-07-14, docs/VERSIONING_PLAN.md §3.2) an ``rm_id``
carries the design's VERSION as well as its identity::

     31           24 23           16 15                            0
    +---------------+---------------+-------------------------------+
    |   ver major   |   ver minor   |         design_id[15:0]        |
    +---------------+---------------+-------------------------------+

which means **a design's rm_id legitimately changes on every version bump**.
nanosoc v1.0 is ``0x01000001``; nanosoc v1.1 will be ``0x01010001``. Every
hard-coded 32-bit rm_id in the tree is therefore a maintenance landmine that
re-breaks on the next bump, forever -- as the v2 cutover itself demonstrated,
when six ``test_e2e_deploy`` tests died on ``0x00000001``.

``check_rm_id_encoding.py`` is the SIBLING gate: it enforces that the three
AUTHORITATIVE sources of an rm_id (the RM wrapper localparam, ``rm_list.tcl``,
and the overlay manifest) agree with each other. This gate covers everyone
ELSE -- every *consumer* that pins a number and names an RM -- and holds them
to that same authority. Between them there is no unchecked rm_id in the repo.

THE RULE
--------
A literal is only a landmine when it **claims to be some NAMED RM's id**. That
distinction is the whole design of this gate, and it is what keeps it from
crying wolf. Compare:

    hdr.rm_id = 0x00000001u;                     <- opaque codec payload. The
                                                    test writes it and reads it
                                                    back; it is not a claim
                                                    about any design and can
                                                    never go stale. NOT FLAGGED.

    #define A_RM_ID 0x00000001u /* nanosoc's rm_id */  rm-id-literal: allow
                                                 ^^^^ a CLAIM: it names the
                                                 design, so it is checkable --
                                                 and (post-v2) wrong. FLAGGED.

(the example above carries the ``rm-id-literal: allow`` opt-out pragma exactly
because it is an example of a WRONG value -- a line that deliberately shows what
failure looks like can never be "corrected", so it must opt out, or this gate
would fail on its own docstring. Lines quoting a CURRENT id stay checked, which
is what keeps the docs honest. The pragma is line-scoped: put it on the line the
literal is on, and keep real pins on their own line so they stay checked.)

So a hex literal is only judged when, on the same line, it (a) sits in an
``rm_id`` context, (b) has a design half matching an allocated ``design_id``,
AND (c) the line *names that very design* (as a whole word: ``nanosoc`` does
not match inside ``nanosoc_multicore``). Then:

  * a literal too wide for the 16-bit design field is read as a full rm_id and
    must equal that design's CURRENT derived rm_id;
  * the retired pre-v2 encodings (``0x4543484F`` = ASCII "ECHO", uart_echo's
    old all-32-bit id) fail ALWAYS, named or not -- there is no context in
    which a fossil id is still correct.

Everything else passes untouched: opaque test payloads (``rm_id=0x11``,
``0xDEADBEEF``, JSON goldens, the synthetic-fixture ids). Zero false positives,
no ever-growing allowlist -- the two failure modes a gate like this dies of.

Design_id 0x0000 (greybox) is exempt: "low half is zero" is far too common a
property of an unrelated constant to key on, and greybox's id is already pinned
hard, three ways, by check_rm_id_encoding.py.

SCOPE
-----
``host/``, ``tests/`` and ``scripts/`` are **enforced**: that is where an rm_id
literal is a live assertion whose staleness breaks a test or a hardware gate.

``firmware/`` and ``fpga/`` are scanned **advisory** (reported, never fatal).
Their surviving literals are stale *labels and doc examples*, not live pins --
the C unit tests are self-consistent (they push a value and assert the same
value back), and gen_manifest.py's ``--rm-id`` is always supplied by
build_dfx.tcl from rm_list.tcl. Nothing there breaks on a version bump. They are
printed on every run rather than suppressed, so they stay visible to the
workstreams that own them.

WHAT THIS BUYS
--------------
The legitimate hard pins -- a hardware swap gate asserting the exact id the
board must read back, a cocotb bench asserting the constant its wrapper drives
-- get to KEEP their literal, because the literal is now self-checking. On the
next version bump ``make check`` fails at build time with an exact
"line N pins X, rm_list.tcl derives Y" instead of the pin silently rotting into
a mystery hardware failure at 2am.

Usage:
    python3 scripts/harness_gates/check_rm_id_literals.py [--repo REPO] [-v]
Exit: 0 = every rm_id literal agrees with rm_list.tcl, 1 = at least one is stale.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The authoritative parser lives in the sibling gate -- import it rather than
# re-implement the encoding a third time (this gate exists to stop drift; it
# would be absurd for it to introduce some).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_rm_id_encoding import derive_rm_id, parse_rm_list, parse_semver  # noqa: E402

#: Retired encodings. A literal equal to one of these is a pre-v2 fossil no
#: matter where it appears -- there is no context in which it is still correct.
RETIRED: dict[int, str] = {
    0x4543484F: (
        "uart_echo's pre-v2 id (ASCII \"ECHO\"). It spent all 32 bits, so it "
        "collides head-on with the v2 version field (it would decode as design "
        "0x484F @ v69.67). uart_echo was RE-NUMBERED to design 0x0004 => rm_id "
        "0x01000004. This is the one and only id the v2 cutover broke."
    ),
}

#: greybox: see the module docstring. Gated hard by check_rm_id_encoding.py.
EXEMPT_DESIGN_IDS = {0x0000}

#: ENFORCED: an rm_id literal here is a live assertion (a test expectation, a
#: hardware swap gate's expected readback). Staleness here breaks something.
ENFORCE_DIRS = ("host", "tests", "scripts")

#: ADVISORY: reported, never fatal. See the module docstring's SCOPE section --
#: these trees' surviving literals are stale labels/doc examples, not live pins,
#: and they are owned by other workstreams.
ADVISORY_DIRS = ("firmware", "fpga")

#: Prose (.md) is excluded on purpose: docs legitimately quote historical ids
#: ("WAS 0x4543484F", "0x00000001 -> 0x01000001" migration tables) and gating
#: them would force weasel-wording of the very documents that explain the change.
SCAN_SUFFIXES = {".py", ".sh", ".sv", ".v", ".tcl", ".c", ".h", ".json"}

#: The three AUTHORITATIVE sources -- they define the ids rather than consume
#: them, and check_rm_id_encoding.py already cross-checks all three against each
#: other. Scanning them here would only re-flag their own explanatory prose.
def _is_authoritative(rel: str) -> bool:
    return (
        rel == "fpga/dfx/rm_list.tcl"
        or re.fullmatch(r"fpga/dfx/overlay/[^/]+/manifest\.json", rel) is not None
        or re.fullmatch(r"fpga/dfx/rms/[^/]+/[^/]+\.sv", rel) is not None
        or re.fullmatch(r"fpga/rp/[^/]+/rp_[^/]+_wrapper\.sv", rel) is not None
    )


#: Directories with no source-of-truth standing: build output, vendored//results
#: snapshots (which record ids as they were AT BUILD TIME and are meant to be
#: historical), and the paths this workstream was told to leave alone.
SKIP_DIR_PARTS = {
    "build", "build_v2", "build_v3", ".git", "__pycache__", ".pytest_cache",
    "node_modules", "sim_build", "lan8720_sut", "shield_path", ".Xil",
}
SKIP_DIR_RE = re.compile(r"(?:^|/)(?:prod_results_[^/]*|proof_results_[^/]*|build[^/]*)(?:/|$)")

#: A line only gets scanned if it mentions rm_id at all. Catches `rm_id`,
#: `RM_ID`, `--expect-rm-id`, `--rm-id`, `rm_id_i`, `boot_rm_id`, ...
_CONTEXT_RE = re.compile(r"rm[_-]?id", re.IGNORECASE)

#: Hex literals in any of the syntaxes this repo uses: 0x1E / 0x0100_001e (C,
#: Python, Tcl, shell) and 32'h0100001E (SystemVerilog).
_HEX_RE = re.compile(r"(?:32'h|0[xX])([0-9A-Fa-f_]+)")

#: Line-level opt-out for a genuinely intentional historical mention inside a
#: scanned source (e.g. a comment recording what an id USED to be).
_PRAGMA = "rm-id-literal: allow"


class Finding:
    def __init__(self, path: str, lineno: int, literal: str, message: str) -> None:
        self.path, self.lineno, self.literal, self.message = path, lineno, literal, message

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.literal} -- {self.message}"


def load_designs(repo: Path) -> dict[int, dict]:
    """{design_id: {name, rm_id, version}} from rm_list.tcl -- the authority."""
    order, lib = parse_rm_list(repo / "fpga/dfx/rm_list.tcl")
    designs: dict[int, dict] = {}
    for rm in order:
        ent = lib.get(rm, {})
        if not {"design_id", "version", "rm_name"} <= ent.keys():
            continue
        did = int(ent["design_id"], 0)
        major, minor, _patch = parse_semver(ent["version"], rm)
        designs[did] = {
            "rm": rm,
            "name": ent["rm_name"],
            "rm_id": derive_rm_id(did, major, minor),
            "version": f"v{major}.{minor}",
        }
    return designs


def iter_sources(repo: Path, tops: tuple[str, ...]):
    for top in tops:
        root = repo / top
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            rel = path.relative_to(repo).as_posix()
            if set(path.relative_to(repo).parts) & SKIP_DIR_PARTS:
                continue
            if SKIP_DIR_RE.search(rel) or _is_authoritative(rel):
                continue
            yield path, rel


def _names_design(line: str, name: str) -> bool:
    """Does ``line`` name this design as a whole word?

    Whole-word so that ``nanosoc`` does NOT match inside ``nanosoc_multicore``
    (they are different designs, 0x0001 vs 0x0003, and conflating them is
    exactly the confusion this gate is meant to prevent). Case-insensitive so a
    constant like ``RM_ID_UART_ECHO`` counts as naming ``uart_echo``.
    """
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                     line, re.IGNORECASE) is not None


def scan_line(line: str, designs: dict[int, dict], path: str, lineno: int):
    """-> (findings, ok_pins) for one line."""
    if _PRAGMA in line or not _CONTEXT_RE.search(line):
        return [], []

    findings: list[Finding] = []
    pins: list[tuple[str, int, str, str]] = []

    for m in _HEX_RE.finditer(line):
        text = m.group(0)
        raw = m.group(1).replace("_", "")
        try:
            value = int(raw, 16)
        except ValueError:
            continue

        # A fossil is a fossil in any context. (A deliberate historical mention
        # -- "was 0x4543484F" -- opts out with the pragma.)
        if value in RETIRED:
            findings.append(Finding(path, lineno, text,
                                    "RETIRED pre-v2 rm_id. " + RETIRED[value]))
            continue

        # Too wide for the 16-bit design field => it is being written as a full
        # 32-bit rm_id. Narrower literals are design_ids or opaque smalls, and a
        # design_id is version-STABLE, so there is nothing here that can rot.
        if not (value > 0xFFFF or len(raw) > 4):
            continue

        design = value & 0xFFFF
        if design in EXEMPT_DESIGN_IDS or design not in designs:
            continue  # opaque / synthetic value -- no claim about a real RM

        d = designs[design]
        # THE DISCRIMINATOR: only a literal that NAMES the design it decodes to
        # is asserting an identity. An unnamed one is an opaque payload.
        if not _names_design(line, d["name"]):
            continue

        if value == d["rm_id"]:
            pins.append((path, lineno, text, d["name"]))
        else:
            findings.append(Finding(
                path, lineno, text,
                f"STALE rm_id pin for '{d['name']}'. rm_list.tcl derives "
                f"0x{d['rm_id']:08X} (design 0x{design:04X} @ {d['version']}), "
                f"but this pins 0x{value:08X}. Under the v2 encoding an rm_id "
                "changes on every version bump -- either update this pin, or "
                "better, DERIVE it (from the overlay manifest / rm_list.tcl) and "
                "pin only the version-stable design half (rm_id & 0xFFFF).",
            ))
    return findings, pins


def scan_tree(repo: Path, tops: tuple[str, ...], designs: dict[int, dict]):
    findings: list[Finding] = []
    pins: list[tuple[str, int, str, str]] = []
    scanned = 0
    for path, rel in iter_sources(repo, tops):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            f, p = scan_line(line, designs, rel, lineno)
            findings.extend(f)
            pins.extend(p)
    return findings, pins, scanned


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=None)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also list the pins that PASS")
    ap.add_argument("--strict", action="store_true",
                    help="fail on ADVISORY findings too (firmware/, fpga/)")
    a = ap.parse_args()

    repo = Path(a.repo).resolve() if a.repo else Path(__file__).resolve().parents[2]
    designs = load_designs(repo)

    hard, hard_pins, n_hard = scan_tree(repo, ENFORCE_DIRS, designs)
    soft, soft_pins, n_soft = scan_tree(repo, ADVISORY_DIRS, designs)

    print("== rm_id literal gate: a pinned rm_id that NAMES a design must match rm_list.tcl ==")
    print(f"   {len(designs)} designs; scanned {n_hard} file(s) in {'/'.join(ENFORCE_DIRS)} "
          f"(enforced) + {n_soft} in {'/'.join(ADVISORY_DIRS)} (advisory)")
    print("   .md prose and the 3 authoritative sources (rm_list.tcl, RM wrappers,")
    print("   overlay manifests) are excluded -- check_rm_id_encoding.py gates those.")
    print()

    pins = hard_pins + soft_pins
    if pins:
        print(f"   {len(pins)} live rm_id pin(s), each agreeing with rm_list.tcl:")
        for rel, lineno, lit, name in pins:
            print(f"     OK  {rel}:{lineno}  {lit}  ({name})")
    else:
        print("   no named rm_id pins outside the authoritative sources.")

    if soft:
        print()
        print(f"   ADVISORY -- {len(soft)} stale rm_id label(s) in firmware/ or fpga/.")
        print("   Not fatal: these are stale COMMENTS and doc examples, not live pins")
        print("   (the C tests are self-consistent; gen_manifest's --rm-id always comes")
        print("   from rm_list.tcl via build_dfx.tcl). Owned by other workstreams --")
        print("   reported here rather than suppressed. Re-run with --strict to fail.")
        for f in soft:
            print(f"     .. {f}")

    if hard:
        print()
        print(f"FAIL: {len(hard)} stale/retired rm_id literal(s) in enforced trees:")
        for f in hard:
            print(f"  - {f}")
        print()
        print("  Fix by preference:")
        print("    1. DERIVE the expected id from the overlay manifest / rm_list.tcl")
        print("       (the subject is usually the round trip, not the number), and")
        print("       assert the version-stable design half `rm_id & 0xFFFF` too;")
        print("    2. if a literal genuinely must stay (a HW gate's exact readback, a")
        print("       bench asserting its wrapper's constant), correct it -- this gate")
        print("       will keep it honest from now on;")
        print(f"    3. for a deliberate historical mention, add `{_PRAGMA}` to the line.")
        return 1

    if a.strict and soft:
        print()
        print(f"FAIL (--strict): {len(soft)} advisory finding(s) treated as errors.")
        return 1

    print()
    print("OK: every rm_id literal that names a design agrees with rm_list.tcl.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
