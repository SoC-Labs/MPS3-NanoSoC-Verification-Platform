"""The Tier-3 debug-channel gate builds the argv for the PROVEN path, not a dead one.

WHAT WAS WRONG. `swd_check.py` ran openocd against TCP **6920** (`swd_server`) and
asserted a `SWD DPIDR 0x0bb11477` line. On the then-fielded `0x3F1A560F` shell 6920 was
DORMANT -- the A6 SWD->JTAG cutover moved the live debug path to `jtag_server` on
**6921**, remote_bitbang, TAP IDCODE `0x6ba00477`. So the one gate that claims to
prove "swap-then-debug actually joins up" was aimed at a port nothing answers, and
would have failed for a reason that has nothing to do with the thing it tests.

WHY THIS TEST EXISTS AT ALL. The gate itself cannot run without the board, so the
retarget cannot be proven by running it. What CAN be proven board-free is the exact
command line it would issue -- which is the whole of the change. `--dry-run` prints
that argv and exits 0; these tests pin it.

THE ON-BOARD RUN IS STILL PENDING. Nothing here proves the shell answers on 6921.
It proves the gate now ASKS the right question.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_GATE = _REPO / "scripts" / "harness_gates" / "swd_check.py"
_CFG = _REPO / "host" / "openocd" / "nanosoc_mps3_jtag.cfg"

sys.path.insert(0, str(_GATE.parent))


def _argv(*extra):
    """The argv the gate would exec, via --dry-run (never launches openocd)."""
    r = subprocess.run(
        [sys.executable, str(_GATE), "--dry-run", *extra],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_dry_run_targets_jtag_6921_not_swd_6920():
    out = _argv("--board", "10.0.0.9")
    assert "nanosoc_mps3_jtag.cfg" in out, out
    assert "6921" in out, out
    assert "TRANSPORT_MODE rbb" in out, out
    assert "RBB_HOST 10.0.0.9" in out, out
    # the dead path must be gone, not merely deprioritised
    assert "swd_remote_bitbang.cfg" not in out, out
    assert "6920" not in out, out


def test_dry_run_sets_the_transport_before_sourcing_the_cfg():
    """`-c "set TRANSPORT_MODE rbb"` after `-f cfg` would be read too late.

    The cfg picks its adapter driver at source time from TRANSPORT_MODE, so an
    override that lands after the `-f` silently leaves the xvc front-end selected
    -- the same class of failure as an unoverridable placeholder.
    """
    out = _argv()
    assert out.index("TRANSPORT_MODE") < out.index("-f"), out


def test_dry_run_expects_the_jtag_tap_idcode():
    out = _argv()
    assert "0x6ba00477" in out.lower(), out


def test_via_hub_wraps_the_whole_command_in_ssh():
    out = _argv("--via-hub", "somehub")
    assert out.lstrip().splitlines()[-1].strip().startswith("ssh ") or "ssh " in out, out
    assert "somehub" in out, out


def test_dry_run_launches_nothing():
    """--dry-run must be inert: it prints and exits 0 with no openocd on PATH."""
    r = subprocess.run(
        [sys.executable, str(_GATE), "--dry-run",
         "--openocd", "/nonexistent/openocd-does-not-exist"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/nonexistent/openocd-does-not-exist" in r.stdout


def test_the_cfg_defaults_to_the_proven_transport():
    """nanosoc_mps3_jtag.cfg's own header calls rbb the proven path; the default
    must agree with it. A default that contradicts the file's own documentation is
    a trap for whoever runs it by hand."""
    text = _CFG.read_text()
    assert 'set TRANSPORT_MODE rbb' in text, (
        "nanosoc_mps3_jtag.cfg no longer defaults TRANSPORT_MODE to rbb")
    assert 'set TRANSPORT_MODE xvc }' not in text.replace("\n", " "), (
        "the xvc default is back -- the header says xvc is NOT the proven path")
