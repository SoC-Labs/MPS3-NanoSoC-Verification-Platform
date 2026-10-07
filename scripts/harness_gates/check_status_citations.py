#!/usr/bin/env python3
"""Gate: every claim in docs/STATUS.md cites something, and every citation resolves.

THE FAILURE THIS EXISTS FOR. docs/STATUS.md is the public answer to "what does this
platform actually do?", and it is the one file in the repo whose entire content is
claims. Its own header has said, for months, that "a silicon claim with no citation is
a row that has drifted" -- and nothing checked it. Rows cited files by memory; rows
claiming a hardware result cited the *plan* for that result; a row could name a path
that had been renamed two mints earlier and stay green forever, because prose has no
gate. That is the same shape as the stale static_id in nine files
(scripts/harness_gates/check_fielded_shell_claims.py) and the stale overlay beside it:
a fact restated by hand, in a file nothing parses.

WHAT THIS GATE DEMANDS. Four things, and deliberately nothing else:

  1. EVERY citation in the document resolves. A backticked repo path must exist; a
     `path:line` must exist AND have that many lines; a markdown link must point at a
     real file, relative to the document. Renaming a file that STATUS.md cites now
     fails here instead of rotting.
  2. EVERY matrix row carries a badge the legend defines, and at least one citation.
     A capability with no evidence at all is not a status, it is an assertion.
  3. A 🟢 Silicon row must cite EVIDENCE, not intention. Concretely: at least one
     citation that is not a plan document, and at least one that is a commit or an
     artifact outside docs/ (source, test, gate, fielded/). "We plan to do it on the
     board" and "we did it on the board" must not be able to look the same.
  4. A 🟣 Diagnosed row states all three of `Cause:`, `Fix:`, `Fielded:`. That badge
     means "the cause is proven, the fix is identified, nothing is on the board" --
     a shape that collapses back into hand-waving the moment one of the three is
     allowed to go missing.

WHAT IT DOES NOT DEMAND. It does not judge whether a claim is TRUE -- no gate can read
a board. It does not require a particular set of badges, or forbid a capability from
being 🔴 Broken; a maturity matrix that cannot say "this is broken" says nothing
instead, which is how the touch row spent a year reading as healthy. And it does not
verify commit hashes when the checkout cannot (a shallow CI clone has no history):
it says so, and still checks everything else.

WHAT COUNTS AS A CITATION, so the gate is predictable. Inside backticks: a token
containing `/` or ending in a source extension is a repo path (resolved from the repo
root), optionally with a `:12` / `:12,34` / `:12-20` line spec; a bare 7-40 hex token is
a commit. Markdown links resolve relative to the DOCUMENT, as a reader's click would.
Everything else -- `make check-ci`, `DFXCTL.RM_STATUS`, `0xA8C1C535` -- is prose and is
ignored, because a gate with false positives is a gate that gets waived. The one edge to
know: `regdemo_a/b` reads as a path and will fail. Write `regdemo_a`/`regdemo_b`. That is
not a bug to route around -- the first run of this gate found three dead citations in
STATUS.md, and one of them was exactly that shorthand hiding a real ambiguity.

Board-free. Reads only tracked text files. Exit 0 clean, 1 on any failure.
"""
from __future__ import annotations

import argparse
import glob as globmod
import os
import re
import subprocess
import sys
from pathlib import Path

STATUS_DEFAULT = "docs/STATUS.md"

#: A backticked token is a path citation if it contains "/" or ends in one of these.
#: Deliberately excludes `.txt`: prose says "config.txt" as a noun, and every real
#: citation of one is written with its directory anyway (so the "/" rule catches it).
PATH_EXTS = (
    ".c", ".h", ".py", ".sv", ".v", ".tcl", ".md", ".json", ".yml", ".yaml",
    ".sh", ".xdc", ".cfg", ".mk", ".patch", ".ld", ".bit", ".dcp",
)

#: A citation is "plan-grade" -- an intention, not evidence -- if it lives under
#: docs/planning/ or is named like a plan. Narrow on purpose: this list is the only
#: thing standing between "cited the proof" and "cited the proposal".
PLAN_DIRS = ("docs/planning/",)
PLAN_NAME_RE = re.compile(r"(PLAN|ROADMAP|PROPOSAL|NEXT_WAVE|_GAPS)", re.I)

COMMITISH_RE = re.compile(r"^[0-9a-f]{7,40}$")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TICK_RE = re.compile(r"`([^`\n]+)`")
LINESPEC_RE = re.compile(r"^(?P<path>.+?):(?P<lines>\d+(?:[,\-]\d+)*)$")

BADGE_HEADER = "Badge"
MATRIX_HEADERS = ("Capability", "Maturity", "Evidence")
DIAGNOSED_FIELDS = ("Cause:", "Fix:", "Fielded:")


class Citation(object):
    """One resolvable reference: a repo path (maybe with a :line spec), or a commit."""

    def __init__(self, raw, kind, target, lines, lineno, base):
        self.raw = raw          # exactly as written in the doc
        self.kind = kind        # "path" | "commit"
        self.target = target    # path relative to `base`, or the sha
        self.lines = lines      # list[int]
        self.lineno = lineno    # line of STATUS.md it was written on
        self.base = base        # Path the target resolves against

    @property
    def resolved(self):
        return self.base / self.target


def _split_row(line):
    """Cells of a markdown table row, or None if the line is not one."""
    s = line.strip()
    if not s.startswith("|"):
        return None
    body = s[1:-1] if s.endswith("|") else s[1:]
    return [c.strip() for c in body.split("|")]


def _is_separator(cells):
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c != "")


def _tables(text):
    """Yield (header_cells, [(lineno, cells), ...]) for every markdown table."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        hdr = _split_row(lines[i])
        if hdr and i + 1 < len(lines) and _is_separator(_split_row(lines[i + 1]) or []):
            rows = []
            j = i + 2
            while j < len(lines):
                cells = _split_row(lines[j])
                if not cells:
                    break
                rows.append((j + 1, cells))
                j += 1
            yield hdr, rows
            i = j
        else:
            i += 1


def _citations_in(text, lineno, repo, docdir):
    """Every citation on one line of STATUS.md."""
    out = []
    for tgt in LINK_RE.findall(text):
        if re.match(r"^(https?:|mailto:|#)", tgt):
            continue
        tgt = tgt.split("#", 1)[0]
        if not tgt:
            continue
        out.append(Citation(tgt, "path", tgt, [], lineno, docdir))
    for tok in TICK_RE.findall(text):
        tok = tok.strip()
        if COMMITISH_RE.match(tok):
            out.append(Citation(tok, "commit", tok, [], lineno, repo))
            continue
        if not tok or " " in tok or tok.startswith("$") or tok.startswith("-"):
            continue
        path, lines = tok, []
        m = LINESPEC_RE.match(tok)
        if m:
            path = m.group("path")
            for part in re.split(r"[,\-]", m.group("lines")):
                lines.append(int(part))
        if "/" not in path and not path.endswith(PATH_EXTS):
            continue
        if path.endswith(PATH_EXTS) or "/" in path:
            out.append(Citation(tok, "path", path, lines, lineno, repo))
    return out


def _is_plan(cit):
    if cit.kind != "path":
        return False
    p = cit.target.replace(os.sep, "/")
    if any(p.startswith(d) or ("/" + d) in p for d in PLAN_DIRS):
        return True
    return bool(PLAN_NAME_RE.search(Path(p).name))


def _is_artifact(cit):
    """Evidence you can run, read back, or check out -- as opposed to prose."""
    if cit.kind == "commit":
        return True
    p = cit.target.replace(os.sep, "/")
    return not p.startswith("docs/")


def _git_can_verify_commits(repo):
    try:
        inside = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if inside.returncode != 0 or inside.stdout.strip() != b"true":
            return False, "not a git checkout"
        shallow = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if shallow.stdout.strip() == b"true":
            return False, "shallow clone (hosted CI checks out depth 1)"
    except OSError as exc:
        return False, "git unavailable (%s)" % exc
    return True, ""


def _commit_exists(repo, sha):
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", sha + "^{commit}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", default=".", help="repo root (default: .)")
    ap.add_argument("--status", default=STATUS_DEFAULT,
                    help="status doc, relative to --repo (default: %s)" % STATUS_DEFAULT)
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    status = repo / args.status
    try:
        text = status.read_text()
    except OSError as exc:
        sys.stderr.write("check_status_citations: FAIL: cannot read %s: %s\n"
                         % (args.status, exc))
        return 1
    docdir = status.parent

    fails = []
    notes = []

    def fail(lineno, msg):
        fails.append("%s:%d: %s" % (args.status, lineno, msg))

    # --- 1. every citation in the WHOLE document resolves ---------------------
    all_cits = []
    for n, line in enumerate(text.splitlines(), 1):
        all_cits.extend(_citations_in(line, n, repo, docdir))

    can_verify, why = _git_can_verify_commits(repo)
    if not can_verify:
        notes.append("commit citations not verified: %s" % why)

    n_paths = n_commits = 0
    for cit in all_cits:
        if cit.kind == "commit":
            n_commits += 1
            if can_verify and not _commit_exists(repo, cit.target):
                fail(cit.lineno, "cites commit `%s`, which is not in this repository"
                                 % cit.target)
            continue
        n_paths += 1
        if "*" in cit.target or "?" in cit.target:
            if not globmod.glob(str(cit.base / cit.target)):
                fail(cit.lineno, "cites `%s`, which matches no file" % cit.raw)
            continue
        p = cit.resolved
        if not p.exists():
            fail(cit.lineno, "cites `%s`, which does not exist (looked for %s)"
                             % (cit.raw, os.path.relpath(str(p), str(repo))))
            continue
        if cit.lines:
            if not p.is_file():
                fail(cit.lineno, "cites `%s` with a line number, but that is a directory"
                                 % cit.raw)
                continue
            total = len(p.read_text(errors="replace").splitlines())
            for ln in cit.lines:
                if ln < 1 or ln > total:
                    fail(cit.lineno, "cites `%s`, but %s has only %d lines"
                                     % (cit.raw, cit.target, total))

    # --- 2. the legend defines the badge vocabulary ---------------------------
    badges = []
    matrices = []
    for hdr, rows in _tables(text):
        if hdr and hdr[0] == BADGE_HEADER:
            for _, cells in rows:
                if cells and cells[0]:
                    badges.append(cells[0])
        elif hdr and hdr[0] == MATRIX_HEADERS[0]:
            matrices.append((hdr, rows))

    if not badges:
        fails.append("%s: no badge legend -- expected a table whose first column "
                     "header is '%s'" % (args.status, BADGE_HEADER))
    if not matrices:
        fails.append("%s: no maturity matrix -- expected at least one table with "
                     "headers %s" % (args.status, " | ".join(MATRIX_HEADERS)))

    silicon = [b for b in badges if "Silicon" in b]
    diagnosed = [b for b in badges if "Diagnosed" in b]

    # --- 3. every matrix row: one known badge, and real evidence --------------
    n_rows = 0
    for hdr, rows in matrices:
        if len(hdr) < 3 or hdr[1] != MATRIX_HEADERS[1] or hdr[2] != MATRIX_HEADERS[2]:
            fails.append("%s: a matrix table's header is %r; expected %s"
                         % (args.status, hdr, " | ".join(MATRIX_HEADERS)))
            continue
        for lineno, cells in rows:
            if len(cells) < 3:
                fail(lineno, "matrix row has %d cells, expected %d"
                             % (len(cells), len(MATRIX_HEADERS)))
                continue
            n_rows += 1
            cap, maturity, evidence = cells[0], cells[1], "|".join(cells[2:])
            hit = [b for b in badges if b and b in maturity]
            if not hit:
                fail(lineno, "row %r carries no badge the legend defines "
                             "(maturity cell: %r)" % (cap[:60], maturity))
                continue
            if len(hit) > 1:
                fail(lineno, "row %r carries %d badges: %s"
                             % (cap[:60], len(hit), ", ".join(hit)))
                continue
            badge = hit[0]
            row_cits = _citations_in("|".join(cells), lineno, repo, docdir)
            if not row_cits:
                fail(lineno, "row %r cites nothing. Every row needs a citation: a "
                             "file, a test, or a commit." % cap[:60])
                continue
            if badge in silicon:
                nonplan = [c for c in row_cits if not _is_plan(c)]
                if not nonplan:
                    fail(lineno, "row %r claims %s but every citation is a PLAN "
                                 "document (%s). A plan is an intention, not a board "
                                 "result." % (cap[:60], badge,
                                              ", ".join(c.raw for c in row_cits)))
                    continue
                if not [c for c in nonplan if _is_artifact(c)]:
                    fail(lineno, "row %r claims %s but cites only prose under docs/ "
                                 "(%s). A silicon claim must name a commit, a test, "
                                 "or an artifact." % (cap[:60], badge,
                                                      ", ".join(c.raw for c in row_cits)))
            if badge in diagnosed:
                missing = [f for f in DIAGNOSED_FIELDS if f not in evidence]
                if missing:
                    fail(lineno, "row %r is %s but its evidence cell omits %s. That "
                                 "badge means cause proven + fix identified + nothing "
                                 "fielded; all three must be stated."
                                 % (cap[:60], badge, " and ".join(missing)))

    for note in notes:
        print("   note: %s" % note)
    if fails:
        sys.stderr.write("check_status_citations: FAIL\n")
        for f in fails:
            sys.stderr.write("  %s\n" % f)
        sys.stderr.write("  (%d problem(s) in %s)\n" % (len(fails), args.status))
        return 1
    print("OK: %s -- %d matrix rows, %d badges, %d path citations resolved, "
          "%d commit citations%s" % (args.status, n_rows, len(badges), n_paths,
                                     n_commits, "" if can_verify else " (unverified)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
