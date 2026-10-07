"""THE CONTROL for :mod:`pyverify.fielded`: no owned board-facing file may
re-type the fielded shell's static_id or its prod directory.

A resolver that exists but is bypassed is worse than no resolver — it reads as
a fix. So this gate greps the files this lane owns for the two things that go
stale:

  * a **build/prod directory literal** (``fpga/dfx/build*/prod``). Every one of
    these in the tree had already gone dead: ``build_v3`` (swap_check.py),
    ``build_v2enc`` + ``build_clcd`` (mps3_swap_design.sh). None is the fielded
    shell's prod dir; none exists in a fresh clone.
  * a **static_id literal** in a live default. ``docs/FIELDED_SHELL.md`` is the
    authority and ``scripts/harness_gates/check_fielded_shell_claims.py``
    gates it — but that gate scans ``.md``/``.txt`` ONLY, so a static baked into
    a Python default or a shell variable is invisible to it. This is the code
    half of the same gate.

HISTORY IS NOT A CLAIM, exactly as in the doc gate: a comment about which mint
introduced what is true permanently. Only *live values* — assignments,
defaults, argument values — are matched, and a line may carry an explicit
``fielded-ok:`` waiver with a reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pyverify.fielded import repo_root

_ROOT = repo_root()

#: The files this lane owns and that talk to the board or name its artefacts.
OWNED = (
    "host/pyverify/pyverify/cli.py",
    "host/pyverify/pyverify/board.py",
    "host/pyverify/pyverify/lease.py",
    "host/pyverify/pyverify/sd.py",
    "host/pyverify/pyverify/pusher.py",
    "scripts/mps3_push.py",
    "scripts/harness_gates/ping_check.py",
    "scripts/harness_gates/swap_check.py",
    "scripts/mps3_board.sh",
    "scripts/mps3_lease_acquire.sh",
    "scripts/mps3_sd_update.sh",
    "scripts/mps3_swap_design.sh",
)

#: ``fpga/dfx/build<anything>/prod`` — the dead-build-dir shape.
_BUILD_PROD = re.compile(r"fpga/dfx/build[A-Za-z0-9_]*/prod")

#: A static_id claim = an 8-hex word ON A LINE that is talking about a
#: static/shell identity. Both halves are needed: the hex form alone sweeps up
#: CRC masks (``0xFFFFFFFF``) and CSR addresses (``0x44A10010``), and the word
#: alone sweeps up every mention of the concept.
_STATIC_HEX = re.compile(r"0[xX][0-9A-Fa-f]{8}")
_STATIC_CTX = re.compile(r"static[_ -]?id|shell[_ -]?id|usercode", re.I)


def _static_claim(line: str) -> bool:
    return bool(_STATIC_HEX.search(line) and _STATIC_CTX.search(line))

_WAIVER = "fielded-ok:"


def _lines(rel: str):
    path = _ROOT / rel
    if not path.is_file():
        pytest.skip("%s is not present in this tree" % rel)
    for n, line in enumerate(path.read_text().splitlines(), 1):
        yield n, line


def _prose_lines(rel: str) -> "set":
    """Line numbers that are PROSE, not code.

    History is not a claim (the same rule ``check_fielded_shell_claims.py``
    applies to the docs): a docstring that says "the old default named
    fpga/dfx/build_v3/prod, which had been dead for two mints" is a record and
    must survive. For Python that means comments AND string literals — a
    module docstring is a string, not a comment, and the naive
    startswith("#") test let every docstring example through as code.
    """
    path = _ROOT / rel
    if rel.endswith(".sh"):
        return {n for n, line in enumerate(path.read_text().splitlines(), 1)
                if line.lstrip().startswith("#")}
    import ast
    text = path.read_text()
    # FULL-LINE comments only. A TRAILING comment must not exempt the code in
    # front of it -- `STATIC_ID = 0x... # the fielded shell` is still a live
    # default with an explanation attached.
    prose = {n for n, line in enumerate(text.splitlines(), 1)
             if line.lstrip().startswith("#")}
    # DOCSTRINGS ONLY -- a bare string STATEMENT. Excluding every string literal
    # would exclude `PROD_DEFAULT = "fpga/dfx/build_v3/prod"`, i.e. exactly the
    # live default this gate exists to catch (verified: it did, once).
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover
        return prose
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(
                getattr(node, "value", None), (ast.Str, ast.Constant)):
            value = node.value
            if isinstance(value, ast.Constant) and not isinstance(value.value, str):
                continue
            prose.update(range(node.lineno, (getattr(node, "end_lineno", node.lineno) or node.lineno) + 1))
    return prose


@pytest.mark.parametrize("rel", OWNED)
def test_no_dead_build_prod_directory(rel: str) -> None:
    """A prod dir must come from pyverify.fielded (or an explicit --prod), not
    from a literal that was true for one mint."""
    prose = _prose_lines(rel)
    offenders = [
        "%s:%d: %s" % (rel, n, line.strip())
        for n, line in _lines(rel)
        if _BUILD_PROD.search(line) and _WAIVER not in line and n not in prose
    ]
    assert not offenders, (
        "hardcoded DFX build/prod directory -- use pyverify.fielded (which reads "
        "docs/FIELDED_SHELL.md + fielded/<static_id>/) or require an explicit "
        "--prod:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("rel", OWNED)
def test_no_static_id_literal_in_a_live_value(rel: str) -> None:
    prose = _prose_lines(rel)
    offenders = [
        "%s:%d: %s" % (rel, n, line.strip())
        for n, line in _lines(rel)
        if _static_claim(line) and _WAIVER not in line and n not in prose
    ]
    assert not offenders, (
        "a 32-bit static_id used as a live value -- docs/FIELDED_SHELL.md is the "
        "authority and pyverify.fielded is how code reads it. If the line is "
        "HISTORY (which mint introduced what), make it a comment; if it is a "
        "deliberate exception, add a `fielded-ok: <reason>` note on the line:\n  "
        + "\n  ".join(offenders)
    )


def test_the_gate_can_actually_fail(tmp_path: Path) -> None:
    """NEGATIVE CONTROL. A gate that structurally cannot fail is worse than no
    gate (this repo has shipped one). Prove both patterns match what they claim
    to and that the waiver is honoured."""
    assert _BUILD_PROD.search('PROD_DEFAULT = "fpga/dfx/build_v3/prod"')
    assert _BUILD_PROD.search('PROD="${MPS3_PROD_DIR:-$REPO/fpga/dfx/build_v2enc/prod}"')
    assert _static_claim('STATIC_ID = 0xA8C1C535')
    assert _static_claim('  --static-id 0xCD74B6AE \\')
    assert _static_claim('expect_shell_id="0xECCEDBF3"')
    assert _static_claim('echo "If shell_id is 0xECCEDBF3 the board rebooted"')
    # ... and does NOT fire on the things that are not static-id claims
    assert not _static_claim("mrd -value 0x44A10010")             # a CSR address
    assert not _static_claim("crc32(payload) & 0xFFFFFFFF")       # a mask
    assert not _static_claim("rm_id 0xB2")                        # not a static
    # ... and prose is excluded by LINE, so a docstring example survives while
    # the same text in a live default does not.
    assert 1 in _prose_lines("scripts/mps3_push.py")              # the docstring
    assert _prose_lines("scripts/mps3_board.sh")                  # shell comments


def test_a_live_default_is_not_mistaken_for_prose(tmp_path: Path) -> None:
    """THE CONTROL FOR THE CONTROL. The first version of this gate excluded
    every Python STRING token as prose, which excluded
    ``PROD_DEFAULT = "fpga/dfx/build_v3/prod"`` -- the exact line it exists to
    catch. Only bare string STATEMENTS (docstrings) are prose."""
    import sys as _sys
    src = tmp_path / "victim.py"
    src.write_text(
        '''"""A docstring that names fpga/dfx/build_v3/prod and 0xA8C1C535 as history."""
PROD_DEFAULT = "fpga/dfx/build_v3/prod"
STATIC_ID = 0xA8C1C535   # static_id
''')
    global _ROOT
    saved, _ROOT = _ROOT, tmp_path
    try:
        prose = _prose_lines("victim.py")
        lines = dict(_lines("victim.py"))
        assert 1 in prose, "the docstring must be prose"
        assert 2 not in prose and 3 not in prose, "a live default is NOT prose"
        assert _BUILD_PROD.search(lines[2])
        assert _static_claim(lines[3])
    finally:
        _ROOT = saved


def test_owned_files_that_talk_to_a_board_use_the_env_seams() -> None:
    """No site host anywhere in the owned set: MPS3_HUB / MPS3_HW_URL are the
    seams, and a literal that cannot be right anywhere else is a guard that
    reads as a value."""
    offenders = []
    for rel in OWNED:
        path = _ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text()
        for banned in ("mapstone", ".ecs.soton.ac.uk"):
            if banned in text:
                offenders.append("%s contains %r" % (rel, banned))
    assert not offenders, "\n  ".join(offenders)
