"""tests/integration/test_uio_name_parity.py

Static parity gate: every UIO device name a Linux aux daemon requests
(``auxhw_open(..., "NAME")`` / ``mps3_uio_open("NAME")`` in
``src/linux_harness/sw/daemons/*.c``) MUST be declared as a
``linux,uio-name`` in ``src/linux_harness/shell_linux.dts``.

Why this gate exists: the daemon finds its device by string-matching
``/sys/class/uio/uioN/name`` (``uio.c:mps3_uio_open`` -> exact ``strcmp``)
against ``linux,uio-name``. A single-character drift between the DTS node and the
daemon literal makes the daemon report "UIO absent" and fall back to the mock (or
die) at runtime, on the board, with no build-time signal. This bit the harness
during bring-up (the aux daemons silently ran mock because the DTS lacked
``linux,uio-name`` on the aux nodes). A pure static string-set diff catches it
board-free, before it reaches silicon.

Direction that FAILS the gate: a requested name absent from the DTS (the
"UIO absent" bug). A DTS name that no daemon requests is only a soft NOTE (a
node reserved for another consumer -- gpio/telem/clcd panels -- or dead), not a
failure.

Pure stdlib text scan; reads no /sys, opens no device.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DTS = _REPO_ROOT / "src" / "linux_harness" / "shell_linux.dts"
_DAEMON_DIR = _REPO_ROOT / "src" / "linux_harness" / "sw" / "daemons"


def _dts_uio_names() -> set[str]:
    text = _DTS.read_text()
    return set(re.findall(r'linux,uio-name\s*=\s*"([a-z0-9-]+)"', text))


def _requested_names() -> dict[str, str]:
    """name -> the daemon file that requests it. Matches the literal passed to
    auxhw_open()/mps3_uio_open(); NULL (mock) selects the mock backend and carries
    no literal, so it is naturally excluded."""
    out: dict[str, str] = {}
    for c in sorted(_DAEMON_DIR.glob("*.c")):
        text = c.read_text()
        for pat in (r'auxhw_open\s*\([^;)]*?"([a-z0-9-]+)"',
                    r'mps3_uio_open\s*\(\s*"([a-z0-9-]+)"'):
            for name in re.findall(pat, text):
                out.setdefault(name, c.name)
    return out


@pytest.fixture(scope="module")
def dts_names():
    assert _DTS.is_file(), f"missing {_DTS}"
    return _dts_uio_names()


@pytest.fixture(scope="module")
def requested():
    assert _DAEMON_DIR.is_dir(), f"missing {_DAEMON_DIR}"
    return _requested_names()


def test_dts_declares_uio_names(dts_names):
    """Sanity: the DTS actually carries uio-name nodes (else the scan is vacuous
    and would hide a real regression)."""
    assert dts_names, "no linux,uio-name entries found in shell_linux.dts"


def test_daemons_request_uio_names(requested):
    """Sanity: the daemon scan found real requests (guards against a regex that
    silently matches nothing -- the empty-gate trap)."""
    assert requested, "no auxhw_open/mps3_uio_open name literals found in daemons/"


def test_every_requested_name_is_declared(dts_names, requested):
    """THE gate: a name a daemon asks for but the DTS does not provide is the
    'UIO absent' runtime failure. Fail build-time, naming the offender(s)."""
    missing = {n: f for n, f in requested.items() if n not in dts_names}
    assert not missing, (
        "aux daemon(s) request UIO name(s) absent from shell_linux.dts "
        "(would be 'UIO absent' on the board): "
        + ", ".join(f"{n!r} <- {f}" for n, f in sorted(missing.items()))
        + f"; DTS provides: {sorted(dts_names)}"
    )


def test_report_unrequested_dts_nodes(dts_names, requested, capsys):
    """Soft: a DTS uio node no daemon requests is reserved for another consumer
    or dead. Not a failure -- just surfaced so it is never silently assumed live."""
    unrequested = sorted(dts_names - set(requested))
    if unrequested:
        print(f"NOTE: DTS uio-name node(s) with no aux-daemon requester "
              f"(other consumer or dead): {unrequested}")
    # informational only
    assert True
