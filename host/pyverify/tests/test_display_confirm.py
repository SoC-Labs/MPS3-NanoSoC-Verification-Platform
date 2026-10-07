"""test_display_confirm.py -- the CONFIRMED panel flip:
``ShellClient.display_settled`` and ``pyverify display --confirm``.

Board-free: the client layer against an in-memory transport, the CLI against a
real ephemeral ``FakeShell``.

WHAT THIS IS ACTUALLY TESTING, AND WHY IT NEEDED A NEW FAKESHELL KNOB
---------------------------------------------------------------------
`display` is send-now and its reply carries ``CLCDKVM.STATUS.owner`` -- the
COMMITTED owner. A flip takes a hardware handover (drain -> panel hard-reset ->
settle -> grant, ~7-9 ms), so the reply to the flip itself routinely names the
owner that is going AWAY. net-protocol.md "Display" says this in words and ends
"a client confirms the landing with a follow-up query".

Until now ``FakeShell`` modelled an idealized INSTANT handover, so that lag --
the single most confusing thing about this verb, and the reason a human running
the board proof mis-reads their own output -- was not representable, and no
test could tell a confirmed flip from an unconfirmed one. `display_commit_polls`
adds it, defaulting to 0 so every existing test is unchanged.

THE CONTROL is `test_without_confirm_the_reply_names_the_outgoing_owner`: the
same FakeShell, the same flip, WITHOUT --confirm, reporting `"harness"` after a
flip to `"dut"`. That is the failure mode --confirm exists to remove, and it is
green here only because it is asserted to happen.
"""
from __future__ import annotations

import json

import pytest

from pyverify.client import DisplaySettleResponse, ShellClient
from pyverify.testing.fakeshell import FakeShell


# --------------------------------------------------------------------------- #
# FakeShell: the handover lag itself
# --------------------------------------------------------------------------- #
def test_fakeshell_default_is_still_an_instant_handover():
    """The knob defaults OFF, so nothing that existed before this change moves."""
    fake = FakeShell(boot_display_owner="harness")
    assert fake.handle_control({"op": "display", "owner": "dut"}) == {
        "ok": True, "owner": "dut"}


def test_fakeshell_lagging_handover_reports_the_outgoing_owner_first():
    fake = FakeShell(boot_display_owner="harness", display_commit_polls=2)
    # The flip is ACCEPTED, but the committed owner has not moved yet.
    assert fake.handle_control({"op": "display", "owner": "dut"})["owner"] == "harness"
    assert fake.handle_control({"op": "display", "owner": "query"})["owner"] == "harness"
    assert fake.handle_control({"op": "display", "owner": "query"})["owner"] == "dut"
    assert fake.display_target == "dut"


def test_fakeshell_query_does_not_count_as_a_request():
    fake = FakeShell(boot_display_owner="harness", display_commit_polls=1)
    fake.handle_control({"op": "display", "owner": "dut"})
    fake.handle_control({"op": "display", "owner": "query"})
    assert fake.display_requests == ["dut"]     # query moves nothing


def test_fakeshell_rejects_a_negative_lag():
    with pytest.raises(ValueError):
        FakeShell(display_commit_polls=-1)


# --------------------------------------------------------------------------- #
# ShellClient.display_settled -- against a real (ephemeral) FakeShell
# --------------------------------------------------------------------------- #
def _settled(fake, owner, **kw):
    with ShellClient("127.0.0.1", port=fake.control_port) as c:
        return c.display_settled(owner, _sleep=lambda _s: None, **kw)


def test_settled_flip_lands_after_polling():
    with FakeShell.ephemeral(boot_display_owner="harness",
                             display_commit_polls=3) as fake:
        r = _settled(fake, "dut")
    assert isinstance(r, DisplaySettleResponse)
    assert (r.ok, r.landed, r.owner, r.requested) == (True, True, "dut", "dut")
    assert r.polls == 3, "one poll per pending handover step"


def test_settled_flip_lands_with_no_polls_when_the_handover_is_instant():
    with FakeShell.ephemeral(boot_display_owner="harness") as fake:
        r = _settled(fake, "dut")
    assert (r.landed, r.polls) == (True, 0)


def test_settled_toggle_resolves_a_concrete_target_first():
    """"Did the toggle land?" is unanswerable without resolving it: the
    send-now reply and the eventual state can be the same string."""
    with FakeShell.ephemeral(boot_display_owner="harness",
                             display_commit_polls=2) as fake:
        r = _settled(fake, "toggle")
    assert (r.requested, r.owner, r.landed) == ("dut", "dut", True)


def test_settled_reports_inconclusive_rather_than_success_on_timeout():
    """A handover that never commits is INCONCLUSIVE: ok (the shell took the
    request) but not landed. Reporting it as a landed flip is how a bring-up
    log ends up claiming a proof it does not have."""
    ticks = iter([0.0] + [i * 0.5 for i in range(1, 40)])
    with FakeShell.ephemeral(boot_display_owner="harness",
                             display_commit_polls=1000) as fake:
        with ShellClient("127.0.0.1", port=fake.control_port) as c:
            r = c.display_settled("dut", timeout=1.0, _sleep=lambda _s: None,
                                  _clock=lambda: next(ticks))
    assert r.ok is True and r.landed is False
    assert r.owner == "harness"
    assert "did not commit" in r.err


def test_settled_on_a_kvm_less_bitstream_declines_without_polling():
    with FakeShell.ephemeral(has_clcd_kvm=False) as fake:
        r = _settled(fake, "dut")
    assert (r.ok, r.landed, r.polls) == (False, False, 0)
    assert r.err == "clcd_kvm not present"


def test_settled_toggle_on_a_kvm_less_bitstream_fails_on_the_first_query():
    """The toggle path reads BEFORE it writes; a declining shell must surface
    there too, not as a confusing "requested ''"."""
    with FakeShell.ephemeral(has_clcd_kvm=False) as fake:
        r = _settled(fake, "toggle")
    assert r.ok is False and r.err == "clcd_kvm not present"


def test_settled_rejects_a_typo_before_any_round_trip():
    with FakeShell.ephemeral() as fake:
        with ShellClient("127.0.0.1", port=fake.control_port) as c:
            with pytest.raises(ValueError):
                c.display_settled("banana")
        assert fake.display_requests == []


# --------------------------------------------------------------------------- #
# CLI `pyverify display --confirm`
# --------------------------------------------------------------------------- #
def test_cli_confirm_parser_defaults():
    from pyverify.cli import build_parser

    a = build_parser().parse_args(
        ["display", "--host", "10.0.0.5", "dut", "--confirm"])
    assert a.confirm is True and a.timeout == 2.0 and a.poll_interval == 0.05
    b = build_parser().parse_args(["display", "--host", "10.0.0.5", "dut"])
    assert b.confirm is False


def test_cli_confirm_is_exit_0_and_says_it_landed(capsys):
    from pyverify.cli import main

    with FakeShell.ephemeral(boot_display_owner="harness",
                             display_commit_polls=2) as fake:
        rc = main(["display", "--host", "127.0.0.1",
                   "--control-port", str(fake.control_port),
                   "dut", "--confirm", "--poll-interval", "0"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["landed"] is True
    assert out["owner"] == "dut" and out["requested"] == "dut"
    assert out["polls"] == 2


def test_without_confirm_the_reply_names_the_outgoing_owner(capsys):
    """THE CONTROL. Same shell, same flip, no --confirm: exit 0 and
    `"owner": "harness"` -- a wire-truthful reply that reads, to a human
    pasting it into a bring-up log, as "the flip did not work" or, worse, gets
    recorded as evidence for the wrong side."""
    from pyverify.cli import main

    with FakeShell.ephemeral(boot_display_owner="harness",
                             display_commit_polls=2) as fake:
        rc = main(["display", "--host", "127.0.0.1",
                   "--control-port", str(fake.control_port), "dut"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0                      # "success"...
    assert out["owner"] == "harness"    # ...reporting the owner going AWAY
    assert "landed" not in out          # and no way to tell


def test_cli_confirm_timeout_is_exit_2_not_0_or_1(capsys):
    """Inconclusive has its own exit code on purpose: a script that treats it
    as success records a flip that may never have happened, and one that treats
    it as failure hides a shell that accepted the request."""
    from pyverify.cli import main

    with FakeShell.ephemeral(boot_display_owner="harness",
                             display_commit_polls=1000) as fake:
        rc = main(["display", "--host", "127.0.0.1",
                   "--control-port", str(fake.control_port),
                   "dut", "--confirm", "--timeout", "0.2",
                   "--poll-interval", "0.01"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ok"] is True and out["landed"] is False
    assert out["owner"] == "harness"


def test_cli_confirm_off_build_is_still_exit_1(capsys):
    from pyverify.cli import main

    with FakeShell.ephemeral(has_clcd_kvm=False) as fake:
        rc = main(["display", "--host", "127.0.0.1",
                   "--control-port", str(fake.control_port),
                   "dut", "--confirm"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["err"] == "clcd_kvm not present"


def test_cli_query_is_unaffected_by_the_new_flag(capsys):
    from pyverify.cli import main

    with FakeShell.ephemeral(boot_display_owner="dut") as fake:
        rc = main(["display", "--host", "127.0.0.1",
                   "--control-port", str(fake.control_port), "--confirm"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "owner": "dut", "err": ""}
