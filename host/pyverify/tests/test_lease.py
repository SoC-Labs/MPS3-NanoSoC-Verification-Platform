"""Tests for :mod:`pyverify.lease` — ONE fpgahub lease dialect.

This replaces two scripts that disagreed with each other and with the hub:

  * ``scripts/mps3_board.sh acquire`` passed ``--json`` and assumed the QUEUED
    response carries a token. It does not (only ``position``), so a contended
    acquire errored out AND left a stale queue entry — documented in
    ``scripts/mps3_lease_acquire.sh``'s header, verified 2026-07-21.
  * ``scripts/mps3_lease_acquire.sh`` polls correctly but knows only the
    ``mps3_pl`` target name, and has no ``status``/``release`` at all.

The name authority is the part most easily got wrong, so it is pinned here:
``lease show`` is **chassis**-scoped (``mps3``) and 404s on the member board,
while acquire/release/cancel/heartbeat address the **lease target**
(``mps3_pl``). Both names are real; using either for the other verb wastes a
board window.

Every test drives the in-process :class:`tests.fakehub.FakeHub` through the
``HubRunner`` seam. No ssh, no hub, no board, no sleeping.
"""
from __future__ import annotations

import pytest

from fakehub import FakeHub
from pyverify import lease as lease_mod


class _Clock:
    """A sleep that records instead of sleeping, so the poller's timing is
    asserted rather than waited out."""

    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


def _client(hub: FakeHub, **kw) -> lease_mod.LeaseClient:
    return lease_mod.LeaseClient(runner=hub, chassis=hub.chassis, target=hub.target, **kw)


# --------------------------------------------------------------------------- #
# acquire
# --------------------------------------------------------------------------- #


def test_acquire_free_board_returns_the_bare_token() -> None:
    hub = FakeHub()
    got = _client(hub).acquire("claude-ops")
    assert got.token == "TOK-0001"
    assert got.holder == "claude-ops"
    assert got.target == "mps3_pl"


def test_acquire_addresses_the_lease_TARGET_not_the_chassis() -> None:
    hub = FakeHub()
    _client(hub).acquire("claude-ops")
    assert hub.calls[0][:3] == ["fpgahub", "lease", "acquire"]
    assert hub.calls[0][3] == "mps3_pl"


def test_acquire_polls_through_a_queued_response_and_wins() -> None:
    """The regression that mattered: a queued response has NO token. It must be
    polled, not treated as a failure and not fed to `lease wait --token`."""
    hub = FakeHub(queue_before_granting=2)
    clock = _Clock()
    got = _client(hub).acquire("claude-ops", poll_s=20.0, sleep=clock)
    assert got.token == "TOK-0001"
    assert clock.slept == [20.0, 20.0], "one sleep per queued response"


def test_acquire_never_passes_json() -> None:
    """--json is what broke mps3_board.sh against this fpgahub."""
    hub = FakeHub(queue_before_granting=1)
    _client(hub).acquire("claude-ops", poll_s=0.0, sleep=_Clock())
    for call in hub.calls:
        assert "--json" not in call


def test_acquire_giving_up_cancels_its_own_queue_entry() -> None:
    """A killed/abandoned acquire strands a queue entry that can later grant
    the board to a dead token and block it. Giving up must clean up."""
    hub = FakeHub(queue_before_granting=99)
    clock = _Clock()
    with pytest.raises(lease_mod.LeaseError) as exc:
        _client(hub).acquire("claude-ops", poll_s=1.0, timeout_s=2.0, sleep=clock,
                             now=iter([0.0, 1.0, 2.0, 3.0, 4.0]).__next__)
    assert "gave up" in str(exc.value)
    assert ["fpgahub", "lease", "cancel", "mps3_pl", "--holder", "claude-ops"] in hub.calls
    assert "claude-ops" not in hub.queued_holders


def test_acquire_passes_ttl_and_tier() -> None:
    hub = FakeHub()
    _client(hub).acquire("claude-ops", ttl=900, tier="interactive")
    call = hub.calls[0]
    assert "--ttl" in call and call[call.index("--ttl") + 1] == "900"
    assert "--tier" in call and call[call.index("--tier") + 1] == "interactive"


def test_background_tier_is_refused_by_default() -> None:
    """A background lease is REVOCABLE, and a revoke mid-swap is the exact
    corruption the lease exists to prevent. Ask for it explicitly."""
    hub = FakeHub()
    with pytest.raises(lease_mod.LeaseError) as exc:
        _client(hub).acquire("claude-ops", tier="background")
    assert "revocable" in str(exc.value)
    _client(hub).acquire("claude-ops", tier="background", allow_preemptible=True)


# --------------------------------------------------------------------------- #
# release / cancel / heartbeat
# --------------------------------------------------------------------------- #


def test_release_needs_the_holder_too() -> None:
    """Token alone replies "no lease to release" and leaves the board HELD.
    That silence cost an hour once; it must be an exception now."""
    hub = FakeHub()
    cli = _client(hub)
    got = cli.acquire("claude-ops")
    with pytest.raises(lease_mod.LeaseError) as exc:
        cli.release(got.token, holder="someone-else")
    assert "no lease to release" in str(exc.value)
    assert hub.held is not None, "the board is still held -- that is the trap"
    cli.release(got.token, holder="claude-ops")
    assert hub.held is None


def test_release_accepts_the_lease_object() -> None:
    hub = FakeHub()
    cli = _client(hub)
    cli.release(cli.acquire("claude-ops"))
    assert hub.held is None


def test_cancel_needs_no_token() -> None:
    """The one holder-addressable recovery path: a stray QUEUE entry."""
    hub = FakeHub(queue_before_granting=1)
    hub.queued_holders.append("claude-stray")
    assert _client(hub).cancel("claude-stray") is True
    assert _client(hub).cancel("claude-stray") is False   # nothing to cancel


def test_heartbeat_extends() -> None:
    hub = FakeHub()
    cli = _client(hub)
    got = cli.acquire("claude-ops")
    cli.heartbeat(got)
    with pytest.raises(lease_mod.LeaseError):
        cli.heartbeat(lease_mod.Lease(token="WRONG", holder="claude-ops",
                                      target=hub.target))


# --------------------------------------------------------------------------- #
# status / preflight
# --------------------------------------------------------------------------- #


def test_status_addresses_the_TARGET_first() -> None:
    """MEASURED against the live hub 2026-09-11: `lease show mps3_pl` answers
    and `lease show mps3` is a 404 naming the configured boards. This test
    asserted the exact opposite until then, and passed, because the fake hub
    modelled the same wrong belief."""
    hub = FakeHub()
    st = _client(hub).status()
    assert hub.calls[0] == ["fpgahub", "lease", "show", "mps3_pl"]
    assert st.held is False
    assert st.holder is None


def test_status_falls_back_to_the_chassis_only_on_no_such_board() -> None:
    """Which name `show` takes is deployment-dependent, so an unknown-name 404
    falls back. NOTHING ELSE does: any other failure must raise."""
    hub = FakeHub()
    cli = _client(hub)                      # client keeps the real names
    hub.target = "some_other_name"          # ...then the daemon stops knowing them
    hub.calls.clear()
    with pytest.raises(lease_mod.LeaseError) as exc:
        cli.status()
    # it tried the target, saw "no such board", then tried the chassis
    assert [c[3] for c in hub.calls] == ["mps3_pl", "mps3"]
    assert "no such board" in str(exc.value) or "404" in str(exc.value)


def test_status_reports_the_holder() -> None:
    hub = FakeHub()
    cli = _client(hub)
    cli.acquire("claude-ops")
    st = cli.status()
    assert st.held is True
    assert st.holder == "claude-ops"


def test_status_on_the_wrong_name_raises_rather_than_reading_free() -> None:
    """A 404 must never be mistaken for "the board is free" — that is how a
    second agent drives a board somebody else is mid-swap on."""
    hub = FakeHub()
    hub.calls.clear()
    # Both names wrong: the fallback is exhausted and the error must surface.
    cli = lease_mod.LeaseClient(runner=hub, chassis="nope_01", target="nope_01_pl")
    with pytest.raises(lease_mod.LeaseError) as exc:
        cli.status()
    assert "404" in str(exc.value)


def test_preflight_ok_when_unheld_or_held_by_us() -> None:
    hub = FakeHub()
    cli = _client(hub)
    assert cli.preflight("claude-ops") is True
    cli.acquire("claude-ops")
    assert cli.preflight("claude-ops") is True
    assert cli.preflight("claude-other") is False


def test_preflight_without_a_holder_only_asserts_unheld() -> None:
    """Guessing a prefix match would let one agent mistake another's lease for
    its own — the exact failure the lease exists to prevent."""
    hub = FakeHub()
    cli = _client(hub)
    assert cli.preflight(None) is True
    cli.acquire("claude-ops")
    assert cli.preflight(None) is False


# --------------------------------------------------------------------------- #
# the runner seam: no site host baked in
# --------------------------------------------------------------------------- #


def test_ssh_runner_refuses_without_MPS3_HUB(monkeypatch) -> None:
    monkeypatch.delenv("MPS3_HUB", raising=False)
    monkeypatch.delenv("FPGAHUB_HOST", raising=False)
    monkeypatch.delenv("MPS3_ON_HUB", raising=False)
    with pytest.raises(lease_mod.LeaseError) as exc:
        lease_mod.hub_runner_from_env()
    assert "MPS3_HUB" in str(exc.value)


def _remote_layers(remote: str):
    """Parse a remote command the way the hub does: its login shell first,
    then the ``sh -c`` that ``sg`` starts. Returns (sg argv, inner argv)."""
    import shlex
    outer = shlex.split(remote)
    assert outer[:3] == ["sg", "fpga", "-c"] and len(outer) == 4, outer
    return outer, shlex.split(outer[3])


def test_ssh_runner_builds_a_batchmode_command(monkeypatch) -> None:
    monkeypatch.setenv("MPS3_HUB", "hub.example")
    monkeypatch.delenv("MPS3_HUB_GROUP", raising=False)
    runner = lease_mod.hub_runner_from_env()
    assert isinstance(runner, lease_mod.SshHubRunner)
    argv = runner.build(["fpgahub", "lease", "show", "mps3_01"])
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    # A LocalForward in the caller's ssh config must not be re-bound per call.
    assert "ClearAllForwardings=yes" in argv
    assert argv[-2] == "hub.example"
    # The fpgahub argv is the tail of the remote command, and the renderer
    # settings ride IN FRONT OF IT rather than on the ssh process: ssh does not
    # carry the local environment, so setting them here would leave the hub's
    # own renderer at its 80-column default. See _RENDER_ENV.
    _, inner = _remote_layers(argv[-1])
    assert inner[-4:] == ["fpgahub", "lease", "show", "mps3_01"]
    assert "COLUMNS=400" in inner and "NO_COLOR=1" in inner


def test_ssh_runner_takes_the_socket_group_via_sg(monkeypatch) -> None:
    """fpgahub 0.3.0's socket is group ``fpga``. A non-interactive ssh session
    does not carry that group, so a bare ``fpgahub`` got Errno 13 on every lease
    verb (2026-09-23). ``sg fpga -c`` takes it from /etc/group; proven on the hub
    the same day. The remote command must be exactly that wrapper around the
    render settings and the fpgahub argv."""
    monkeypatch.setenv("MPS3_HUB", "hub.example")
    monkeypatch.delenv("MPS3_HUB_GROUP", raising=False)
    remote = lease_mod.hub_runner_from_env().build(
        ["fpgahub", "lease", "acquire", "mps3_pl", "--holder", "h"])[-1]
    outer, inner = _remote_layers(remote)
    assert inner == ["COLUMNS=400", "NO_COLOR=1", "TERM=dumb",
                     "fpgahub", "lease", "acquire", "mps3_pl", "--holder", "h"]


def test_hub_group_is_overridable_and_can_be_switched_off(monkeypatch) -> None:
    import shlex
    monkeypatch.setenv("MPS3_HUB", "hub.example")
    monkeypatch.setenv("MPS3_HUB_GROUP", "labusers")
    remote = lease_mod.hub_runner_from_env().build(["fpgahub", "lease", "show", "x"])[-1]
    assert shlex.split(remote)[:3] == ["sg", "labusers", "-c"]
    # Empty = no wrapper: the old bare form, for a hub whose socket is not
    # group-protected.
    monkeypatch.setenv("MPS3_HUB_GROUP", "")
    remote = lease_mod.hub_runner_from_env().build(["fpgahub", "lease", "show", "x"])[-1]
    assert shlex.split(remote)[0] != "sg"
    assert shlex.split(remote)[-4:] == ["fpgahub", "lease", "show", "x"]


def test_ssh_runner_quotes_each_argument_through_both_shells(monkeypatch) -> None:
    """A holder with a space must arrive at fpgahub as ONE argument, not two.
    The pre-sg runner joined the argv with bare spaces; this pins the quoting."""
    monkeypatch.setenv("MPS3_HUB", "hub.example")
    monkeypatch.delenv("MPS3_HUB_GROUP", raising=False)
    remote = lease_mod.hub_runner_from_env().build(
        ["fpgahub", "lease", "cancel", "mps3_pl", "--holder", "two words; true"])[-1]
    _, inner = _remote_layers(remote)
    assert inner[-2:] == ["--holder", "two words; true"]


def test_a_wrapped_token_is_refused_instead_of_used(monkeypatch) -> None:
    """fpgahub 0.3.0 prints the lease through `rich`, which wraps at 80 columns.
    A token split by that wrap would be captured as its first half: the board
    would be held under a token the caller cannot release, and nothing would say
    so until the TTL expired. The dialect refuses it instead.

    The control is the same reply unwrapped, immediately below."""
    wrapped = ("granted token=tok-0123456789abcdef0123456789abc\n"
               "def expires=2099-01-01T00:00:00Z tier=interactive")

    class _Wrapping(lease_mod.HubRunner):
        def build(self, argv):
            return list(argv)

        def __call__(self, argv, timeout=None):
            return lease_mod.RunResult(0, wrapped, "")

    cli = lease_mod.LeaseClient(runner=_Wrapping(), chassis="mps3_01",
                                target="mps3_pl")
    with pytest.raises(lease_mod.LeaseError) as exc:
        cli.acquire("someone")
    assert "HALF token" in str(exc.value)


def test_the_same_reply_unwrapped_is_accepted(monkeypatch) -> None:
    """The control for the test above: one line, one whole token."""
    whole = ("granted token=tok-0123456789abcdef0123456789abcdef "
             "expires=2099-01-01T00:00:00Z tier=interactive")

    class _Whole(lease_mod.HubRunner):
        def build(self, argv):
            return list(argv)

        def __call__(self, argv, timeout=None):
            return lease_mod.RunResult(0, whole, "")

    cli = lease_mod.LeaseClient(runner=_Whole(), chassis="mps3_01",
                                target="mps3_pl")
    assert cli.acquire("someone").token == "tok-0123456789abcdef0123456789abcdef"


def test_on_hub_runner_runs_fpgahub_directly(monkeypatch) -> None:
    monkeypatch.delenv("MPS3_HUB", raising=False)
    monkeypatch.setenv("MPS3_ON_HUB", "1")
    runner = lease_mod.hub_runner_from_env()
    assert isinstance(runner, lease_mod.LocalHubRunner)
    assert runner.build(["fpgahub", "lease", "show", "x"]) == ["fpgahub", "lease", "show", "x"]


def test_no_module_level_default_hub_host() -> None:
    """A public tree with a literal site hostname is a guard that reads as a
    value. There must be none."""
    import inspect
    src = inspect.getsource(lease_mod)
    for banned in ("mapstone", ".ecs.soton.ac.uk", "srv0"):
        assert banned not in src, "site host %r is baked into lease.py" % banned


# --------------------------------------------------------------------------- #
# defaults come from the env seams, not from literals in the caller
# --------------------------------------------------------------------------- #


def test_names_default_from_the_env(monkeypatch) -> None:
    monkeypatch.setenv("MPS3_CHASSIS", "mps3_09")
    monkeypatch.setenv("MPS3_LEASE_TARGET", "mps3_09_pl")
    cli = lease_mod.LeaseClient(runner=FakeHub(chassis="mps3_09", target="mps3_09_pl"))
    assert cli.chassis == "mps3_09"
    assert cli.target == "mps3_09_pl"


def test_names_default_to_the_neutral_names() -> None:
    import os
    for key in ("MPS3_CHASSIS", "MPS3_LEASE_TARGET"):
        os.environ.pop(key, None)
    cli = lease_mod.LeaseClient(runner=FakeHub())
    assert (cli.chassis, cli.target) == ("mps3", "mps3_pl")
    assert lease_mod.board_tty(2) == "/dev/mps3_pl/tty_02"


def test_board_tty_follows_the_target_and_MPS3_TTY_DIR(monkeypatch) -> None:
    """A site that sets only MPS3_LEASE_TARGET gets that target's TTY directory."""
    monkeypatch.delenv("MPS3_TTY_DIR", raising=False)
    monkeypatch.setenv("MPS3_LEASE_TARGET", "lab_board_pl")
    assert lease_mod.board_tty(0) == "/dev/lab_board_pl/tty_00"
    assert lease_mod.board_tty(2, "other_pl") == "/dev/other_pl/tty_02"
    monkeypatch.setenv("MPS3_TTY_DIR", "/run/ttys/")
    assert lease_mod.board_tty(1) == "/run/ttys/tty_01"
