"""Tests for ``pyverify.mactest`` — the MAC-in-operation test driver.

Two layers:

- **Integration** — the real driver + real :class:`ShellClient` over real
  sockets against :class:`FakeShell`'s ``macgen`` counter model, end-to-end
  with no hardware (also via the :class:`Mps3Board` facade's ``mac_test``).
- **Unit** — the driver's pass/fail logic against a scripted duck-typed
  driver, so each failure branch (clean traffic making errors, an inject
  that doesn't raise err, a rejected link event, ...) is pinned without
  needing the fake shell to be coaxed into that state.
"""
from __future__ import annotations

import pytest

from pyverify import Mps3Board, run_mac_test
from pyverify.client import LinkResponse, MacGenResponse, ShellClient
from pyverify.mactest import MacTestResult
from pyverify.testing.fakeshell import FakeShell


# --------------------------------------------------------------------------- #
# Integration — driver + ShellClient + FakeShell
# --------------------------------------------------------------------------- #

def test_run_mac_test_passes_against_fakeshell():
    with FakeShell.ephemeral(macgen_frames_per_call=4) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            result = run_mac_test(shell)
    assert isinstance(result, MacTestResult)
    assert result.passed, result.detail
    assert result.baseline.err == 0
    assert result.clean.err == 0
    assert result.clean.tx > result.baseline.tx      # clean traffic advanced
    assert result.faulted.err == result.clean.err + 1  # the fault registered
    assert result.link_events == ("down", "up")
    # the fake shell saw the same link blip on its VPHY side
    assert fake.link_events == ["down", "up"]


def test_board_mac_test_passes_against_fakeshell():
    with FakeShell.ephemeral() as fake:
        board = Mps3Board(
            fake.host, control_port=fake.control_port,
            client=ShellClient(fake.host, fake.control_port),
        )
        with board:
            result = board.mac_test()
            # board.macgen passthrough also works and is validated by default
            r = board.macgen(gen=True, chk=True, inject="runt")
    assert result.passed, result.detail
    assert r.ok


def test_board_macgen_validates_inject_by_default():
    board = Mps3Board("shell.example", client=object())  # client never used
    with pytest.raises(ValueError, match="unknown inject"):
        board.macgen(inject="smash")


def test_run_mac_test_reports_link_rejection_against_fakeshell():
    # A shell that only knows "up"/"down"/"pulse" rejects "flap"; the driver
    # surfaces that as a clean failure, not an exception.
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            result = run_mac_test(shell, link_events=("flap",))
    assert not result.passed
    assert "link event 'flap' rejected" in result.detail
    # macgen phases still ran and are captured for inspection
    assert result.faulted is not None and result.faulted.err == 1


# --------------------------------------------------------------------------- #
# Unit — scripted driver exercising each verdict branch
# --------------------------------------------------------------------------- #

class ScriptedDriver:
    """Duck-typed :class:`~pyverify.mactest.MacGenDriver`: returns queued
    macgen replies in order, records links, and answers ``link`` per
    ``link_ok`` (a bool, or an ``{event: bool}`` map)."""

    def __init__(self, macgen_replies, link_ok=True):
        self._replies = list(macgen_replies)
        self._i = 0
        self.link_ok = link_ok
        self.links: list[str] = []

    def macgen(self, *, gen=True, chk=True, inject="none") -> MacGenResponse:
        reply = self._replies[self._i]
        self._i += 1
        return reply

    def link(self, event: str) -> LinkResponse:
        self.links.append(event)
        ok = self.link_ok if isinstance(self.link_ok, bool) else self.link_ok.get(event, True)
        return LinkResponse(ok=ok)


def _mg(tx, rx, err, ok=True):
    return MacGenResponse(ok=ok, tx=tx, rx=rx, err=err)


def test_happy_path_scripted():
    driver = ScriptedDriver([
        _mg(8, 8, 0),    # baseline
        _mg(16, 16, 0),  # clean round (clean_rounds=1)
        _mg(24, 24, 1),  # fault -> err up
    ])
    result = run_mac_test(driver, clean_rounds=1)
    assert result.passed and result.detail == ""
    assert driver.links == ["down", "up"]


def test_baseline_rejection_fails_closed():
    driver = ScriptedDriver([_mg(0, 0, 0, ok=False)])
    result = run_mac_test(driver, clean_rounds=1)
    assert not result.passed
    assert "baseline macgen rejected" in result.detail


def test_clean_traffic_must_advance_counters():
    driver = ScriptedDriver([_mg(8, 8, 0), _mg(8, 8, 0)])  # tx did not move
    result = run_mac_test(driver, clean_rounds=1)
    assert not result.passed
    assert "did not advance counters" in result.detail


def test_clean_traffic_must_not_manufacture_errors():
    driver = ScriptedDriver([_mg(8, 8, 0), _mg(16, 16, 3)])  # err jumped on clean
    result = run_mac_test(driver, clean_rounds=1)
    assert not result.passed
    assert "clean traffic produced errors" in result.detail


def test_inject_must_raise_err_count():
    driver = ScriptedDriver([
        _mg(8, 8, 0), _mg(16, 16, 0),  # clean advances, err flat
        _mg(24, 24, 0),                 # fault round but err did NOT rise
    ])
    result = run_mac_test(driver, clean_rounds=1, inject="giant")
    assert not result.passed
    assert "did not raise err count" in result.detail


def test_link_rejection_reported():
    driver = ScriptedDriver(
        [_mg(8, 8, 0), _mg(16, 16, 0), _mg(24, 24, 1)],
        link_ok={"down": False},
    )
    result = run_mac_test(driver, clean_rounds=1)
    assert not result.passed
    assert "link event 'down' rejected" in result.detail
    assert result.link_events == ("down",)  # stopped at the first rejection


@pytest.mark.parametrize("bad_inject", ["none", "smash", ""])
def test_invalid_inject_argument_raises(bad_inject):
    driver = ScriptedDriver([])
    with pytest.raises(ValueError):
        run_mac_test(driver, inject=bad_inject)


def test_clean_rounds_must_be_positive():
    driver = ScriptedDriver([])
    with pytest.raises(ValueError, match="clean_rounds"):
        run_mac_test(driver, clean_rounds=0)


def test_link_phase_can_be_skipped():
    driver = ScriptedDriver([_mg(8, 8, 0), _mg(16, 16, 0), _mg(24, 24, 1)])
    result = run_mac_test(driver, clean_rounds=1, link_events=())
    assert result.passed
    assert result.link_events == ()
    assert driver.links == []
