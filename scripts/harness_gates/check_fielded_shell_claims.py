#!/usr/bin/env python3
"""Gate: no tracked file may claim a FIELDED static_id that disagrees with the authority.

THE FAILURE THIS EXISTS FOR. `0xCD74B6AE` was asserted as "the currently shipped shell"
in nine files -- docs/STATUS.md, the webharness README/backend/catalog, two of its tests,
socket_harness/endpoints.py, pyverify/debug.py, the OpenOCD config, prog_shell_jtag.tcl.
Every one was true when written. All of them became false at once when `0xA8C1C535` was
fielded on 2026-08-10, and none of them changed, because prose has no gate.

That is the same shape as the stale overlay beside it: nanosoc_upy stayed keyed to a dead
shell across two mints because R9 never ran (3c9703d, 6b0f870). A fact asserted in nine
places is a fact that will be wrong in eight of them.

WHAT IS AND IS NOT A CLAIM. This gate matches PRESENT-TENSE FIELDED phrasing only:

    "currently shipped shell 0xDEADBEEF"      <- a claim. Gated.
    "the shipped 0xDEADBEEF shell runs ..."   <- a claim. Gated.
    "the A6 cutover landed as 0xDEADBEEF"     <- history. Left alone, permanently.

History must not be rewritten -- doing so erases which mint introduced what -- so the
patterns below are deliberately narrow. The cost of being narrow is that a novel phrasing
escapes; the cost of being broad is that the gate rewrites the record, which is worse.

WIDENED 2026-09-09, because that cost was being paid. The original patterns keyed on
"shipped"/"fielded" only, and the docstring admitted novel phrasings would escape. They
did: SIX live stale claims sat in the tree, naming FOUR dead shells (0xECCEDBF3,
0x14E1A2D8, 0x3A8BBA62, 0xE4B1C44A) -- every one phrased around the word "current"
rather than "shipped", or drawn as a CLCD panel row (`SID : 0x...`) rather than written
as a sentence. Two new prose patterns cover exactly those two shapes:

    "the **current** shell (`static_id 0xDEADBEEF`)"   <- a claim. Gated (CURRENCY-ADJ).
    "`static_id` is currently **`0xDEADBEEF`**"        <- a claim. Gated (IS-CURRENTLY).
    "| SID : 0xDEADBEEF |"                             <- a claim. Gated (SID-ROW).

THREE THINGS KEEP THE WIDENING FROM BECOMING A REWRITING MACHINE:

  1. PROSE ONLY. The new patterns run on `.md`/`.txt` alone. This gate exists because
     "a sentence in a README has no gate" -- code that PINS an id is a different
     problem with its own gates (check_rm_id_literals.py, check_overlay_static_id.py),
     and a test fixture asserting `SID : 0x...` is not a claim about a board. Running
     the new patterns over source flagged five such fixtures; none was a stale claim.
  2. The same HISTORY_MARKERS exemption, applied to every line the match spans (these
     patterns can cross a line break -- one of the six did).
  3. ADJACENCY, not proximity. `current` must sit directly against its noun (modulo
     markdown punctuation). A first draft used a plain 60-character window and flagged
     18 sites, most of them addresses and test constants that merely happened to be
     near the word "current".

WHY IT READS A MARKDOWN TABLE. The authority is docs/FIELDED_SHELL.md because `fielded`
is not derivable: nothing in the repo knows what a board booted. `minted` IS derivable
(R9 already holds mps3_shell_static_id.c and the manifests in lockstep), and the two are
routinely different -- between a mint and a deployment the repo legitimately holds
overlays keyed to a shell no board is running.

Exit 0 clean, 1 on any disagreement. No imports beyond the stdlib.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

#: The authority, and the only file exempt from the scan.
AUTHORITY = "docs/FIELDED_SHELL.md"

#: Present-tense "this is what is on the board" phrasings, each capturing a static_id.
#: `.{0,80}` keeps the association local -- a hex id three paragraphs from the word
#: "shipped" is not a claim about it.
CLAIM_PATTERNS = [
    re.compile(r"currently[- ]shipped.{0,80}?(0x[0-9A-Fa-f]{8})", re.I),
    re.compile(r"current(?:ly)? shipped.{0,80}?(0x[0-9A-Fa-f]{8})", re.I),
    re.compile(r"\bshipped\b.{0,40}?(0x[0-9A-Fa-f]{8})", re.I),
    re.compile(r"(0x[0-9A-Fa-f]{8}).{0,40}?\bis (?:the )?(?:currently )?(?:shipped|fielded)\b", re.I),
    re.compile(r"\bfielded\b.{0,40}?(0x[0-9A-Fa-f]{8})", re.I),
    re.compile(r"(0x[0-9A-Fa-f]{8}).{0,30}?shell (?:runs|is running|boots)\b", re.I),
]

#: Lines carrying one of these are statements about the PAST even if they also match a
#: claim pattern. Kept tiny and specific: every entry is a licence to be wrong.
HISTORY_MARKERS = re.compile(
    # `then-shipped` / `then-fielded` is the canonical way to keep a sentence that was
    # true when written without asserting it still is. It is the minimal edit that turns
    # a stale claim into an accurate record, so it must be recognised -- otherwise the
    # gate pushes people towards deleting the history instead of dating it.
    r"\b(then-\w+|landed|was |were |former|previous|superseded|until|historic|"
    r"re-mint(?:ed)? from|used to|no longer|prior to|since the|at the time)\b", re.I
)

#: Markdown/formatting noise permitted between a currency word and its noun, so that
#: "the **current** shell" reads the same to the gate as "the current shell".
_NOISE = r"[\s*_`]{0,4}"
#: The nouns a static_id can be predicated of.
_SUBJECT = r"(?:build|shell|static(?:_id)?|image|bitstream)"

#: PROSE-ONLY patterns (.md/.txt), matched against the file with newlines flattened to
#: spaces so a claim that wraps mid-sentence is still one claim. Each captures the id.
PROSE_CLAIM_PATTERNS = [
    # "the **current** shell (`static_id 0x3A8BBA62`)" / "current build ... 0xECCEDBF3"
    ("CURRENCY-ADJ", re.compile(
        r"\bcurrent(?:ly)?\b" + _NOISE + _SUBJECT + r".{0,60}?(0x[0-9A-Fa-f]{8})",
        re.I | re.S)),
    # "`static_id` is currently **`0x14E1A2D8`**"
    ("IS-CURRENTLY", re.compile(
        _SUBJECT + r"\b.{0,24}?\bis\s+current(?:ly)?\b" + _NOISE + r"(0x[0-9A-Fa-f]{8})",
        re.I | re.S)),
    # The CLCD status panel's own row, drawn in prose as a mock: "| SID : 0xE4B1C44A |".
    # A panel mock is a picture of what the board shows, which is a claim about the
    # board -- it was one of the six.
    ("SID-ROW", re.compile(r"\bSID\s*:\s*(0x[0-9A-Fa-f]{8})")),
]

#: An LMB/BRAM size with its unit, in either order:
#:   "256 KiB LMB"  /  "1 MiB local RAM"  /  "LMB: 512 KB"  /  "BRAM = 256 KiB"
_LMB_SIZE = (r"(?:(\d{1,5})\s*(KiB|KB|kB|MiB|MB)\s*-?\s*(?:of\s+)?(?:LMB|BRAM|local\s+RAM)"
             r"|(?:LMB|BRAM|local\s+RAM)\s*[:=]?\s*(\d{1,5})\s*(KiB|KB|kB|MiB|MB))")

#: Present-tense LMB-size claims, PROSE ONLY. A currency word, then a size within
#: 60 characters; the subject noun may sit between them ("current shell ... 256 KiB
#: LMB") or FOLLOW the size ("the current 256 KiB-LMB shell build"), or be absent.
#: The stricter subject-first form missed fpga/dfx/README.md:201 for exactly that
#: reason on 2026-09-10 -- and the unit-to-noun hyphen in "KiB-LMB" missed it a
#: second time, hence the `-?` in _LMB_SIZE. The subject stays optional rather than
#: alternated because the extraction unpacks exactly four groups from _LMB_SIZE and
#: an alternation would double them. Captures are (n1, unit1, n2, unit2).
LMB_CLAIM_PATTERN = re.compile(
    r"\bcurrent(?:ly)?\b" + _NOISE + r"(?:" + _SUBJECT + r".{0,60}?)?" + _LMB_SIZE, re.I | re.S)


def _as_kib(n: str, unit: str) -> int:
    return int(n) * 1024 if unit.lower() in ("mib", "mb") else int(n)


#: Cheap "could this file possibly hold an LMB-size claim?" test, used only to
#: skip files fast. It exists because the original early-out was `"0x" not in
#: text` -- correct for static_id claims, and silently fatal for LMB ones: a file
#: that states "Current build: 256 KiB LMB" and contains no hex at all was
#: dropped before any pattern ran. The LMB pattern was dead on arrival and the
#: gate reported OK; only its own control caught it.
_LMB_HINT = re.compile(r"\b(?:LMB|BRAM|local\s+RAM)\b", re.I)

#: Paths whose static_id mentions are structurally not fielded-claims. Each needs a
#: reason, because an unexplained exemption is how a gate quietly stops covering things.
EXEMPT_PREFIXES = {
    AUTHORITY: "the authority itself",
    "docs/planning/": "planning records describe past and proposed mints",
    "fpga/dfx/overlay_linux/": "a separate static implementation (PLATFORM_ONE_IMPL.md)",
    "scripts/harness_gates/check_fielded_shell_claims.py": "this gate's own docstring",
    "tests/integration/test_fielded_shell_claims.py": "the gate's tests, which need literals",
    "host/pyverify/tests/test_fielded.py": "the resolver's tests build a FAKE authority table with literals",
    "fielded/": "an archive directory is NAMED for the shell it preserves and describes that "
                "shell throughout, by construction -- fielded/0xA8C1C535/README.md saying "
                "'the fielded static shell 0xA8C1C535' is the archive stating its own subject, "
                "not the tree claiming what is on the board today. Exempting it is what lets "
                "the archive survive the next mint without being rewritten into a lie.",
}

#: A PATH is not a CLAIM. `fielded/0xA8C1C535/README.md` cited in prose names a
#: directory that archives a past shell; the id in it is part of a filename. When
#: 0x3F1A560F was fielded on 2026-09-22 this gate raised 58 findings and 38 of them
#: were this -- pointers to the archive, in sentences that were still true. Blanking
#: these spans (rather than deleting them) keeps every later offset valid, so the
#: line numbers this gate reports stay right.
#:
#: WIDENED 2026-09-22 (twice), because the first form covered only the prose two
#: thirds of them and 53 findings remained, 28 of them still paths:
#:
#:   * THE TRAILING `/` IS OPTIONAL. `--prod fielded/0xA8C1C535` names the directory
#:     ITSELF (scripts/mps3_push.py, host/pyverify/tests/test_cli_ops.py), and the
#:     original pattern required a file inside it. Safe to widen because `fielded/`
#:     followed IMMEDIATELY by `0x` is a path by construction -- a claim is written
#:     "the fielded 0xA8C1C535 shell", with a SPACE, and is still gated.
#:   * THE COMPONENTS FORM. `REPO / "fielded" / "0xA8C1C535"` is how Python builds
#:     that same path (tests/dfx_flow/test_mint_record.py, test_static_canon.py).
#:     Two quoted segments joined by `/`: also a path, and no sentence is written
#:     that way.
_ARCHIVE_PATH = re.compile(
    r"fielded/0[xX][0-9A-Fa-f]{8}/?"                     # fielded/0xA8C1C535[/...]
    r'|"fielded"\s*/\s*"0[xX][0-9A-Fa-f]{8}"'           # REPO / "fielded" / "0x..."
)


def _blank_archive_paths(text: str) -> str:
    """Blank every archive-directory path, preserving LENGTH.

    Spaces rather than deletion so every offset -- and therefore every line number
    this gate reports -- stays valid in the blanked copy.
    """
    return _ARCHIVE_PATH.sub(lambda m: " " * len(m.group(0)), text)


def authority_ids(repo: pathlib.Path) -> dict:
    """Parse the `minted`/`fielded` rows out of the authority's marked table."""
    text = (repo / AUTHORITY).read_text()
    block = re.search(
        r"<!-- FIELDED_SHELL_TABLE.*?-->(.*?)<!-- /FIELDED_SHELL_TABLE -->",
        text, re.S)
    if not block:
        raise SystemExit("%s: FIELDED_SHELL_TABLE markers missing -- the gate cannot "
                         "read the authority, so it refuses rather than passing "
                         "vacuously" % AUTHORITY)
    out = {}
    for key in ("minted", "fielded"):
        m = re.search(r"\|\s*`%s`\s*\|\s*`(0x[0-9A-Fa-f]{8})`\s*\|" % key, block.group(1))
        if not m:
            raise SystemExit("%s: no `%s` row in the table" % (AUTHORITY, key))
        out[key] = m.group(1).upper()
    out["fielded_fw_flags"] = _fw_flags(block.group(1))
    out["lmb_kb"] = _lmb_kb(block.group(1))
    return out


def _lmb_kb(block: str) -> int:
    """Parse and VALIDATE the `lmb_kb` row: a positive power-of-two KiB size.

    WHY IT IS HERE. The fielded image's MicroBlaze local RAM size is a THIRD fact
    with the same shape as the other two: not derivable from the repo (it is a
    property of the shell that was flashed), asserted independently in prose all
    over the tree, and silently wrong the moment a shell is re-minted at a new
    size. `fpga/shell/README.md` announced "Current build: 2026-07-07, 256 KiB
    LMB" long after the board moved to 1 MiB.

    It is not cosmetic. The JTAG diag mailbox is anchored to the TOP of the LMB
    (`LMB_KB*1024 - 0x100` since diag v8; `- 0x80` for v5..v7 images), so a
    wrong LMB belief points every xsdb read at the wrong address -- and the
    LMB decode ALIASES, so the stale address returns a
    plausible-looking value instead of an error. That is bug #4 exactly.

    POWER OF TWO is the real constraint, not a style rule: the decode is a bit
    slice, so 768 KiB cannot be built and a row saying so would be fiction.
    """
    m = re.search(r"\|\s*`lmb_kb`\s*\|\s*`?(\d+)`?\s*\|", block)
    if not m:
        raise SystemExit(
            "%s: no `lmb_kb` row in the table.\n"
            "  The mailbox is anchored to the top of the LMB, so an LMB size nobody\n"
            "  records is an address every xsdb script gets wrong -- silently, because\n"
            "  the LMB decode aliases a stale address onto a live one (bug #4).\n"
            "  Add a row, e.g.:   | `lmb_kb` | `1024` |" % AUTHORITY)
    kb = int(m.group(1))
    if kb <= 0 or (kb & (kb - 1)) != 0:
        raise SystemExit(
            "%s: `lmb_kb` is %d, which is not a positive power of two. The LMB\n"
            "  decode is a bit slice; a non-power-of-two size cannot be built, so a\n"
            "  row stating one describes a shell that does not exist."
            % (AUTHORITY, kb))
    return kb


#: A build-flag token: NAME=VALUE, the shape `make FLAG=1` takes.
_FW_FLAG_TOKEN = re.compile(r"\b([A-Z][A-Z0-9_]*)=(\S+)")
#: Words that mean "we did not record this" and must not pass for an answer.
_FW_FLAGS_NON_ANSWERS = re.compile(r"\b(unknown|tbd|todo|n/?a|none|\?+)\b", re.I)
#: Fewer than this many flags is not a build configuration, it is a gesture.
_FW_FLAGS_MIN = 3


def _fw_flags(block: str) -> str:
    """Parse and VALIDATE the `fielded_fw_flags` row.

    WHY THIS ROW IS REQUIRED. The fielded shell is two artifacts -- a bitstream and a
    firmware -- and only the bitstream had an identity here. The firmware's build flags
    were recorded NOWHERE, which is how a HALF-MINT shipped: fabric built with the AXI
    IIC block, firmware built without the touch driver, and every gate in the repo
    green, because no gate knew what the firmware was supposed to contain. A static_id
    identifies the half of the image that has a CRC.

    Validated for SHAPE, not for content: at least %d NAME=VALUE tokens, and no
    "unknown"/"TBD" placeholder. Pinning the exact flag set would freeze today's build
    configuration into a gate and make the next legitimate mint fail here.
    """ % _FW_FLAGS_MIN
    m = re.search(r"\|\s*`fielded_fw_flags`\s*\|(.+?)\|", block, re.S)
    if not m:
        raise SystemExit(
            "%s: no `fielded_fw_flags` row in the table.\n"
            "  The fielded image is a bitstream AND a firmware; recording only the\n"
            "  static_id identifies half of it. A half-mint (fabric with the AXI IIC,\n"
            "  firmware without the touch driver) passed every gate in this repo\n"
            "  because nothing recorded what the firmware was built with.\n"
            "  Add a row, e.g.:\n"
            "    | `fielded_fw_flags` | `CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 ...` |"
            % AUTHORITY)
    value = m.group(1).strip()
    if _FW_FLAGS_NON_ANSWERS.search(value):
        raise SystemExit(
            "%s: `fielded_fw_flags` is a placeholder (%r), not a build configuration.\n"
            "  If the flags for the fielded image are genuinely not known, that is a\n"
            "  finding to report, not a value to write down -- the honest recovery is\n"
            "  to read them off the build that produced the fielded firmware."
            % (AUTHORITY, value))
    flags = _FW_FLAG_TOKEN.findall(value)
    if len(flags) < _FW_FLAGS_MIN:
        raise SystemExit(
            "%s: `fielded_fw_flags` holds %d NAME=VALUE token(s) (%r); at least %d are\n"
            "  required. The row must carry the actual `make` flags the fielded\n"
            "  firmware was built with, not a prose description of them."
            % (AUTHORITY, len(flags), value, _FW_FLAGS_MIN))
    return value


def tracked_files(repo: pathlib.Path):
    out = subprocess.run(["git", "-C", str(repo), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def exempt(path: str):
    for pre, why in EXEMPT_PREFIXES.items():
        if path == pre or path.startswith(pre):
            return why
    return None


#: Suffixes the PROSE patterns apply to. See the docstring: this gate is about
#: sentences, and a `SID : 0x...` in a C test is a fixture, not a claim about a board.
PROSE_SUFFIXES = (".md", ".txt")


def _line_index(text: str):
    """offset -> 1-based line number, for a flattened copy of `text`.

    Flattening replaces each newline with a space, so offsets are IDENTICAL in both
    strings and this index is valid for matches found in the flat copy."""
    idx = []
    ln = 1
    for ch in text:
        idx.append(ln)
        if ch == "\n":
            ln += 1
    idx.append(ln)
    return idx


def scan(repo: pathlib.Path, fielded: str, lmb_kb: int):
    bad = []
    for rel in tracked_files(repo):
        if exempt(rel):
            continue
        p = repo / rel
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue                      # binary or unreadable: nothing to claim
        # Early-out. MUST admit both claim classes: static_id claims need a "0x",
        # LMB-size claims do not. See _LMB_HINT for what happened when it did not.
        if "0x" not in text and not _LMB_HINT.search(text):
            continue
        lines = text.splitlines()
        seen = set()                      # (line, id) -- one site, not one per pattern

        # --- pass 1: the original per-line patterns, over every tracked file ----
        for n, line in enumerate(lines, 1):
            if HISTORY_MARKERS.search(line):
                continue
            # A path is not a claim in CODE either -- `--prod fielded/0xA8C1C535`
            # and `REPO / "fielded" / "0xA8C1C535"` name the archive directory.
            # Blanked only for MATCHING; the text reported below is the real line.
            probe = _blank_archive_paths(line)
            for pat in CLAIM_PATTERNS:
                m = pat.search(probe)
                if not m:
                    continue
                got = m.group(1).upper()
                if got != fielded:
                    seen.add((n, got))
                    bad.append((rel, n, got, line.strip()[:110]))
                break

        # --- pass 2: the prose patterns, on documentation only, over the file
        #     with newlines flattened so a claim that wraps is still one claim ---
        if not rel.endswith(PROSE_SUFFIXES):
            continue
        flat = text.replace("\n", " ")   # same length => offsets stay valid
        # Blank out archive-directory paths, same length, so their ids cannot be
        # read as claims while every offset below stays valid (see _ARCHIVE_PATH).
        flat = _blank_archive_paths(flat)
        line_of = _line_index(text)
        for name, pat in PROSE_CLAIM_PATTERNS:
            for m in pat.finditer(flat):
                got = m.group(1).upper()
                if got == fielded:
                    continue
                first = line_of[m.start()]
                last = line_of[min(m.end(), len(line_of) - 1)]
                # history exemption over every line the match spans
                if any(HISTORY_MARKERS.search(lines[i - 1])
                       for i in range(first, min(last, len(lines)) + 1)):
                    continue
                if (first, got) in seen:
                    continue
                seen.add((first, got))
                bad.append((rel, first, "%s [%s]" % (got, name),
                            lines[first - 1].strip()[:110]))

        # --- pass 3: present-tense LMB/BRAM SIZE claims. Same shape as the
        #     static_id claims and the same failure: a size that was true when
        #     written, restated in prose, never revisited. The mailbox anchor
        #     rides on it, and a stale anchor ALIASES rather than erroring. ---
        for m in LMB_CLAIM_PATTERN.finditer(flat):
            n1, u1, n2, u2 = m.groups()
            claimed = _as_kib(n1, u1) if n1 else _as_kib(n2, u2)
            if claimed == lmb_kb:
                continue
            first = line_of[m.start()]
            last = line_of[min(m.end(), len(line_of) - 1)]
            if any(HISTORY_MARKERS.search(lines[i - 1])
                   for i in range(first, min(last, len(lines)) + 1)):
                continue
            key = (first, "LMB%d" % claimed)
            if key in seen:
                continue
            seen.add(key)
            bad.append((rel, first, "%d KiB LMB [LMB-SIZE]" % claimed,
                        lines[first - 1].strip()[:110]))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    args = ap.parse_args()
    repo = pathlib.Path(args.repo).resolve()

    ids = authority_ids(repo)
    print("== fielded-shell claim gate ==")
    print("   authority %s" % AUTHORITY)
    print("   minted    %s" % ids["minted"])
    print("   fielded   %s" % ids["fielded"])
    print("   fw flags  %s" % ids["fielded_fw_flags"])
    print("   lmb_kb    %d KiB" % ids["lmb_kb"])
    if ids["minted"] != ids["fielded"]:
        # Legitimate and worth saying: overlays are keyed to a shell no board runs.
        print("   NOTE: minted != fielded -- the overlay set is keyed to a shell that is")
        print("         NOT on the board. Pushing one will be refused by the pusher until")
        print("         the shell is deployed. This is a normal pre-deployment state.")

    bad = scan(repo, ids["fielded"], ids["lmb_kb"])
    if not bad:
        print("\nOK: no tracked file claims a fielded static_id other than %s"
              % ids["fielded"])
        return 0

    print("\nFAIL: %d present-tense claim(s) about the fielded image disagree with %s"
          % (len(bad), AUTHORITY))
    for rel, n, got, line in bad:
        print("  %s:%d  claims %s" % (rel, n, got))
        print("      %s" % line)
    print("\n  Either the board was re-flashed (update the table in %s) or the prose is"
          % AUTHORITY)
    print("  stale (point it at that file instead of restating the fact). If the line is")
    print("  about a PAST mint, phrase it in the past -- history is not gated.")
    print("  An [LMB-SIZE] hit is the mailbox-anchor class: the anchor is")
    print("  lmb_kb*1024-0x80, and the LMB decode ALIASES, so a stale size does not")
    print("  error -- it reads a plausible wrong word (bug #4).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
