"""test_fakeshell_parity.py — FakeShell(profile="linux") says what the PRODUCT
harnessd says, taken from the real binary rather than restated.

The conformance suite (HOST_CONTRACT §3) runs harnessd's ctrl_echo twin
(host-echo), whose feature set is ctrl_echo's -- so it could not see the linux
profile claim `windowed`, which harnessd deliberately builds without (Makefile
"THE FLAG SET", HARNESSD_CONTRACT §9.5). A client picks windowed-vs-plain pushes
from that list, so the drift sent the windowed TCP push to a fake of a board
that takes plain ones (lane BOARD-RUNNER, 2026-09-24). Here the host PRODUCT
build (`build/host`, the rv32 build's flag set on x86) answers `version`, and
the fake's linux profile must answer the same `features`, `lmb_kb` and `impl`.
The ctrl_echo twin is the negative control: it reports a DIFFERENT set, so the
comparison is not vacuous.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "host" / "pyverify"))

from harnessd_mock import Harnessd  # noqa: E402
from pyverify.testing.fakeshell import LINUX_PROFILE_FEATURES, PROFILES, FakeShell  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
ECHO = str(HERE.parent / "build" / "host-echo" / "mps3-harnessd")
pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")


def _version(binary: str, tmp: Path) -> dict:
    h = Harnessd(binary, tmp).start()
    try:
        return h.req({"op": "version"})
    finally:
        h.stop()


def test_the_linux_profile_reports_the_product_harnessd_s_features(tmp_path):
    real = _version(BIN, tmp_path / "hd")
    assert real["ok"] is True and real["impl"] == "linux", real
    assert "windowed" not in real["features"]                    # harnessd's choice
    assert tuple(real["features"]) == LINUX_PROFILE_FEATURES
    assert real["lmb_kb"] == PROFILES["linux"]["lmb_kb"]
    with FakeShell.ephemeral(profile="linux") as fake:
        mine = fake.handle_control({"op": "version"})
    assert (mine["features"], mine["lmb_kb"], mine["impl"]) == \
        (real["features"], real["lmb_kb"], real["impl"])


@pytest.mark.skipif(not Path(ECHO).exists(), reason="host-echo not built (make host-echo)")
def test_negative_control_the_echo_twin_reports_another_set(tmp_path):
    echo = _version(ECHO, tmp_path / "echo")
    assert tuple(echo["features"]) != LINUX_PROFILE_FEATURES, echo["features"]
