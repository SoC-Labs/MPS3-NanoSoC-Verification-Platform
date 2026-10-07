"""tests/integration/test_rm_id_literal_gate.py

Unit-tests the harness gate ``scripts/harness_gates/check_rm_id_literals.py``:
**no hard-coded ``rm_id`` that names a design may silently go stale.**

WHY THIS GATE EXISTS
--------------------
The v2 ``rm_id`` re-encoding (2026-07-14, docs/VERSIONING_PLAN.md §3.2) packs
the design's VERSION into the top half::

    rm_id = (ver_major << 24) | (ver_minor << 16) | design_id

so an rm_id now **legitimately changes on every design version bump** — nanosoc
v1.0 is ``0x01000001``, v1.1 will be ``0x01010001``. Every hard-coded 32-bit
rm_id in the tree therefore became a maintenance landmine that re-breaks on the
next bump, forever. The cutover proved it: six ``test_e2e_deploy`` tests died on
a stale ``0x00000001``, and simply re-pinning them to ``0x01000001`` would have
left the trap armed for v1.1.

Those tests now DERIVE the expected id from the overlay manifest and pin only
the version-stable design half. This gate holds the rest of the repo to the same
standard, and — crucially — makes the pins that legitimately survive (a hardware
swap gate's expected readback; a cocotb bench asserting the constant its wrapper
drives) **self-checking against ``fpga/dfx/rm_list.tcl``**, so the next bump
fails ``make check`` at build time with an exact "line N pins X, rm_list derives
Y" instead of rotting into a mystery hardware failure.

THE TESTS BELOW ARE THE NEGATIVE CONTROL. A gate that never fires is worse than
no gate: it buys false confidence. So each case builds a synthetic repo under
``tmp_path`` (its own ``rm_list.tcl`` + one consumer file) and proves the gate
*fires on the thing it claims to catch* and *stays silent on the things it must
not* — in particular that it does NOT flag opaque test payloads, which is what
separates this gate from a naive grep.

The gate is a standalone pure-stdlib script in ``scripts/harness_gates/`` with
no package structure, so — exactly like ``test_overlay_static_id_gate.py`` — it
is loaded BY FILE PATH rather than inventing a ``sys.path`` package relationship
the script never asks for.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "scripts" / "harness_gates" / "check_rm_id_literals.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_rm_id_literals", _GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


#: A synthetic rm_list.tcl in the real one's shape — enough for the gate's
#: parser (which it shares with check_rm_id_encoding.py). nanosoc sits at v1.0
#: here, so 0x01000001 is current and 0x00000001 (the pre-v2 id) is stale.
RM_LIST = """
set RM_LIB(rm_greybox,wrapper_dir)  "fpga/dfx/rms/rm_greybox"
set RM_LIB(rm_greybox,top)          "rm_greybox"
set RM_LIB(rm_greybox,design_id)    "0x0000"
set RM_LIB(rm_greybox,version)      "0.0.0"
set RM_LIB(rm_greybox,rm_name)      "greybox"

set RM_LIB(rm_nanosoc,wrapper_dir)  "fpga/rp/nanosoc"
set RM_LIB(rm_nanosoc,top)          "rp_nanosoc_wrapper"
set RM_LIB(rm_nanosoc,design_id)    "0x0001"
set RM_LIB(rm_nanosoc,version)      "1.0.0"
set RM_LIB(rm_nanosoc,rm_name)      "nanosoc"

set RM_LIB(rm_led,wrapper_dir)      "fpga/dfx/rms/rm_led"
set RM_LIB(rm_led,top)              "rm_led"
set RM_LIB(rm_led,design_id)        "0x001E"
set RM_LIB(rm_led,version)          "1.0.0"
set RM_LIB(rm_led,rm_name)          "led"

set RM_ORDER [list rm_greybox rm_nanosoc rm_led]
"""


def _tree(tmp_path: Path, consumer: str, *, name: str = "host/thing.py") -> Path:
    """A synthetic repo: rm_list.tcl (the authority) + one consumer file."""
    (tmp_path / "fpga" / "dfx").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fpga" / "dfx" / "rm_list.tcl").write_text(RM_LIST)
    consumer_path = tmp_path / name
    consumer_path.parent.mkdir(parents=True, exist_ok=True)
    consumer_path.write_text(consumer)
    return tmp_path


def _run(gate, tree: Path, argv: list[str] | None = None) -> int:
    import sys
    saved = sys.argv
    try:
        sys.argv = ["check_rm_id_literals.py", "--repo", str(tree)] + (argv or [])
        return gate.main()
    finally:
        sys.argv = saved


# --------------------------------------------------------------------------- #
# It FIRES on the thing it exists to catch
# --------------------------------------------------------------------------- #

def test_stale_pin_that_names_its_design_fails(gate, tmp_path, capsys):
    """The headline: a literal claiming to be nanosoc's rm_id, but carrying the
    pre-v2 value. This is the exact shape of the bug that broke test_e2e_deploy.
    """
    tree = _tree(tmp_path, 'NANOSOC_RM_ID = 0x00000001  # nanosoc\n')  # rm-id-literal: allow -- deliberately-stale FIXTURE for the gate under test
    rc = _run(gate, tree)
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE rm_id pin for 'nanosoc'" in out
    assert "0x01000001" in out          # names the value it SHOULD be
    assert "host/thing.py:1" in out     # and exactly where to fix it


def test_a_version_bump_invalidates_every_stale_pin(gate, tmp_path, capsys):
    """THE REGRESSION THIS GATE IS FOR.

    A pin that is correct TODAY (nanosoc v1.0 => 0x01000001) must fail the
    moment nanosoc is re-versioned — because the rm_id has legitimately changed
    and the pin now points at an artefact that no longer exists. Bumping
    rm_list.tcl to v1.1 must break the pin, at build time, with the new value
    named. Without this, the pin rots silently until it fails on hardware.
    """
    tree = _tree(tmp_path, 'NANOSOC_RM_ID = 0x01000001  # nanosoc\n')
    assert _run(gate, tree) == 0, "v1.0 pin must be OK against a v1.0 rm_list"
    capsys.readouterr()

    # The bump: nanosoc 1.0 -> 1.1. Nothing else changes.
    rm_list = tree / "fpga" / "dfx" / "rm_list.tcl"
    rm_list.write_text(rm_list.read_text().replace(
        'set RM_LIB(rm_nanosoc,version)      "1.0.0"',
        'set RM_LIB(rm_nanosoc,version)      "1.1.0"'))

    rc = _run(gate, tree)
    out = capsys.readouterr().out
    assert rc == 1, "a version bump MUST invalidate the now-stale 0x01000001 pin"
    assert "0x01010001" in out, "the gate must name the NEW id to update the pin to"


def test_retired_echo_id_fails_anywhere(gate, tmp_path, capsys):
    """uart_echo's pre-v2 0x4543484F ('ECHO') used all 32 bits and collides with
    the version field. It is a fossil in every context — no design name needed."""
    tree = _tree(tmp_path, 'rm_id = 0x4543484F\n')  # rm-id-literal: allow -- deliberately-stale FIXTURE for the gate under test
    rc = _run(gate, tree)
    out = capsys.readouterr().out
    assert rc == 1
    assert "RETIRED" in out


# --------------------------------------------------------------------------- #
# It STAYS SILENT on legitimate uses (the "don't cry wolf" half)
# --------------------------------------------------------------------------- #

def test_opaque_test_payload_is_not_flagged(gate, tmp_path, capsys):
    """The discriminator. ``hdr.rm_id = 0x00000001`` with no design named is an
    opaque codec payload — the test writes it and reads the same value back. It
    is not a claim about nanosoc and can never go stale. A naive grep for
    ``0x00000001`` would flag this; that false positive is what makes gates get
    switched off, so it is load-bearing that this passes."""
    tree = _tree(tmp_path, "hdr.rm_id = 0x00000001;\nfake.rm_id = 0xDEADBEEF;\n")
    assert _run(gate, tree) == 0
    assert "STALE" not in capsys.readouterr().out


def test_correct_pin_passes_and_is_reported(gate, tmp_path, capsys):
    tree = _tree(tmp_path, 'swap_check --rm nanosoc --expect-rm-id 0x01000001\n',
                 name="scripts/hw_gate.sh")
    assert _run(gate, tree) == 0
    out = capsys.readouterr().out
    assert "OK  scripts/hw_gate.sh:1" in out, "a live pin should be reported, not silent"


def test_design_id_is_stable_and_never_flagged(gate, tmp_path, capsys):
    """Keying on the DESIGN half is the sanctioned pattern for identity, so a
    bare design_id literal must never be flagged — it survives version bumps by
    construction. (This is what firmware's CLCD_RM_DESIGN table does.)"""
    tree = _tree(tmp_path, 'if (CLCD_RM_DESIGN(rm_id) == 0x0001) return "nanosoc";\n',
                 name="host/names.c")
    assert _run(gate, tree) == 0
    assert "STALE" not in capsys.readouterr().out


def test_pragma_opts_a_historical_mention_out(gate, tmp_path, capsys):
    tree = _tree(tmp_path,
                 '# nanosoc rm_id was 0x00000001 pre-v2.  rm-id-literal: allow\n')
    assert _run(gate, tree) == 0
    assert "STALE" not in capsys.readouterr().out


def test_nanosoc_does_not_match_inside_nanosoc_multicore(gate, tmp_path, capsys):
    """Whole-word name matching: ``nanosoc`` (design 0x0001) and
    ``nanosoc_multicore`` (0x0003) are DIFFERENT designs. A line naming only
    nanosoc_multicore must not be read as a claim about nanosoc — conflating
    them is precisely the confusion this gate is meant to prevent."""
    tree = _tree(tmp_path, 'MULTICORE_RM_ID = 0x00000001  # nanosoc_multicore\n')
    # 0x00000001's design half is 0x0001 (nanosoc), but only nanosoc_multicore
    # is named here, so this is not a nanosoc claim and must not be flagged.
    assert _run(gate, tree) == 0
    assert "STALE" not in capsys.readouterr().out


def test_authoritative_sources_are_not_double_gated(gate, tmp_path, capsys):
    """rm_list.tcl / the RM wrappers / the overlay manifests DEFINE the ids and
    are cross-checked by check_rm_id_encoding.py. Scanning them here would only
    re-flag their own explanatory prose (which quotes retired ids on purpose)."""
    tree = _tree(tmp_path, "")
    wrapper = tree / "fpga" / "rp" / "nanosoc" / "rp_nanosoc_wrapper.sv"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    # A wrapper stating a WRONG nanosoc rm_id: check_rm_id_encoding.py's job to
    # catch (it compares against the fabric), explicitly not this gate's.
    wrapper.write_text("localparam [31:0] RM_ID_NANOSOC = 32'h00000001; // nanosoc\n")  # rm-id-literal: allow -- deliberately-stale FIXTURE for the gate under test
    assert _run(gate, tree) == 0
    assert "STALE" not in capsys.readouterr().out
