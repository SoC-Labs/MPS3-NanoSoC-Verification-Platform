"""The fielded-shell claim gate: does it catch a stale claim, and leave history alone?

`0xCD74B6AE` was asserted as "the currently shipped shell" in nine files. Every one was
true when written; all became false at once when `0xA8C1C535` was fielded on 2026-08-10,
and none changed, because prose has no gate. Running the new gate found ELEVEN, naming
THREE different dead shells (`0xCD74B6AE`, `0xE4B1C44A`, `0xD84A2E7A`) -- so the drift
was worse than a grep for one id could show.

The two properties that matter are in tension, and both are tested here:

    it must CATCH a present-tense claim that disagrees with the authority
    it must LEAVE HISTORY ALONE -- "the A6 cutover landed as 0xCD74B6AE" is true
    permanently, and a gate that forced it to change would erase the record of which
    mint introduced what

A gate with only the first property would be a rewriting machine. A gate with only the
second is decoration.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_GATE = _REPO / "scripts" / "harness_gates" / "check_fielded_shell_claims.py"
_AUTHORITY = _REPO / "docs" / "FIELDED_SHELL.md"

sys.path.insert(0, str(_GATE.parent))


def _run(repo):
    return subprocess.run([sys.executable, str(_GATE), "--repo", str(repo)],
                          capture_output=True, text=True)


def test_the_authority_exists_and_parses():
    import check_fielded_shell_claims as g
    ids = g.authority_ids(_REPO)
    assert re.fullmatch(r"0X[0-9A-F]{8}", ids["minted"])
    assert re.fullmatch(r"0X[0-9A-F]{8}", ids["fielded"])


def test_the_repo_is_clean_right_now():
    r = _run(_REPO)
    assert r.returncode == 0, r.stdout + r.stderr


def test_minted_agrees_with_the_generated_static_id():
    """The authority's `minted` row must match what `make overlays` generated.

    They are separate facts with separate update paths, so they can disagree -- and if
    they do, the authority is describing a mint that does not exist.
    """
    import check_fielded_shell_claims as g
    ids = g.authority_ids(_REPO)
    c = (_REPO / "fpga" / "dfx" / "overlay" / "mps3_shell_static_id.c").read_text()
    m = re.search(r"return\s+(0x[0-9A-Fa-f]{8})UL", c)
    assert m, "mps3_shell_static_id.c has no return literal"
    assert m.group(1).upper() == ids["minted"], (
        "docs/FIELDED_SHELL.md says minted=%s but mps3_shell_static_id.c returns %s"
        % (ids["minted"], m.group(1).upper()))


# --------------------------------------------------------------------------- #
# the controls -- run against a scratch tree, never the real repo
# --------------------------------------------------------------------------- #

def _scratch_repo(tmp_path, extra_file=None, extra_text=""):
    """A minimal git repo carrying the authority, plus one file under test."""
    repo = tmp_path / "r"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "FIELDED_SHELL.md").write_text(
        _AUTHORITY.read_text())
    if extra_file:
        p = repo / extra_file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(extra_text)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    return repo


def test_it_CATCHES_a_stale_present_tense_claim(tmp_path):
    """THE CONTROL. Without this, the gate could pass by matching nothing at all."""
    repo = _scratch_repo(
        tmp_path, "docs/somewhere.md",
        "The currently shipped shell is 0xDEADBEEF and it does things.\n")
    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "0XDEADBEEF" in r.stdout
    assert "docs/somewhere.md" in r.stdout


def test_it_LEAVES_HISTORY_ALONE(tmp_path):
    """A statement about a past mint is true permanently and must not be flagged."""
    repo = _scratch_repo(
        tmp_path, "docs/history.md",
        "The A6 SWD->JTAG cutover landed as 0xDEADBEEF.\n"
        "This RM can no longer be dropped in against the then-fielded 0xFEEDFACE static.\n"
        "At the time, the shipped 0xBAADF00D shell ran jtag_server on 6921.\n")
    r = _run(repo)
    assert r.returncode == 0, r.stdout


def test_a_claim_matching_the_authority_passes(tmp_path):
    import check_fielded_shell_claims as g
    fielded = g.authority_ids(_REPO)["fielded"]
    repo = _scratch_repo(
        tmp_path, "docs/ok.md",
        "The currently shipped shell is %s.\n" % fielded.lower())
    r = _run(repo)
    assert r.returncode == 0, r.stdout


def test_it_REFUSES_rather_than_passing_when_the_authority_is_unreadable(tmp_path):
    """A gate that cannot find its authority must fail, not pass vacuously.

    This is the failure mode that made the mutation campaign report twelve fake kills:
    scoring 'could not run' as 'nothing wrong'.
    """
    repo = tmp_path / "r"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "FIELDED_SHELL.md").write_text("no table here\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    r = _run(repo)
    assert r.returncode != 0
    assert "markers missing" in (r.stdout + r.stderr)


def test_it_SAYS_SO_when_minted_and_fielded_differ(tmp_path):
    """The normal pre-deployment state, and it must be visible rather than silent.

    Overlays keyed to a shell no board runs will be refused by the pusher. Finding that
    out on the bench with a lease running is the expensive way.
    """
    repo = tmp_path / "r"
    (repo / "docs").mkdir(parents=True)
    text = _AUTHORITY.read_text()
    text = re.sub(r"(\|\s*`fielded`\s*\|\s*)`0x[0-9A-Fa-f]{8}`", r"\1`0xFEEDFACE`", text)
    (repo / "docs" / "FIELDED_SHELL.md").write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    r = _run(repo)
    assert "minted != fielded" in r.stdout
