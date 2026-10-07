"""Tests for :mod:`pyverify.sd` — the ONE SD writer, with the one-write-wait
discipline encoded rather than written down.

THE DISCIPLINE, AND WHAT IT COST TO LEARN
    ``fpgahub target program … --method sd`` writes ~12 MB over USB-MSC and
    **always times the client out**. That timeout is NOT a failure — the write
    is still running on the hub. A retry issued mid-write corrupts the SD and
    darkens the board (it has). The rule is: **one write, wait ~5 minutes, then
    reset.** Encoded here as: a second write is refused while one is in flight;
    the client timeout is an expected outcome, not an exception; the wait is
    performed, not suggested; and the result is verified by md5 read-back where
    the hub exposes one.

    ``docs/FIELDED_SHELL.md`` also says the write itself is not evidence of
    anything — only the shell reporting its own id is. So this writer never
    claims a shell was fielded; it reports what it did.

Every test drives the in-process :class:`tests.fakehub.FakeHub`. No hub, no
board, no sleeping, no SD.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fakehub import FakeHub
from pyverify import lease as lease_mod
from pyverify import sd as sd_mod


class _Clock:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    p = tmp_path / "nanosoc.bit"
    p.write_bytes(b"\x00\x09\x0f\xf0" + b"x" * (2 * 1024 * 1024))
    return p


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    d = tmp_path / "backup" / "20260101T000000Z"
    d.mkdir(parents=True)
    (d / "nanosoc.bit").write_bytes(b"\x00\x09\x0f\xf0" + b"y" * (2 * 1024 * 1024))
    return d


def _writer(hub: FakeHub, tmp_path: Path, **kw) -> sd_mod.SdWriter:
    lease = lease_mod.LeaseClient(runner=hub, chassis=hub.chassis, target=hub.target)
    kw.setdefault("sleep", _Clock())
    kw.setdefault("state_dir", tmp_path / "state")
    return sd_mod.SdWriter(runner=hub, lease=lease, target=hub.target, **kw)


def _held(hub: FakeHub, holder="claude-ops") -> lease_mod.Lease:
    return lease_mod.LeaseClient(
        runner=hub, chassis=hub.chassis, target=hub.target).acquire(holder)


# --------------------------------------------------------------------------- #
# the timeout is EXPECTED
# --------------------------------------------------------------------------- #


def test_client_timeout_is_an_expected_outcome_not_a_failure(bundle, backup, tmp_path) -> None:
    hub = FakeHub(sd_program_times_out=True)
    lease = _held(hub)
    res = _writer(hub, tmp_path).write(bundle, lease=lease, backup_dir=backup)
    assert res.timed_out is True
    assert res.ok is True, "a client timeout on --method sd is the NORMAL outcome"
    assert any("timed out" in line for line in res.log)


def test_a_hub_that_returns_promptly_is_also_fine(bundle, backup, tmp_path) -> None:
    hub = FakeHub(sd_program_times_out=False)
    res = _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup)
    assert res.timed_out is False
    assert res.ok is True


# --------------------------------------------------------------------------- #
# one write at a time
# --------------------------------------------------------------------------- #


def test_second_write_is_refused_while_one_is_in_flight(bundle, backup, tmp_path) -> None:
    """The corruption case: a retry issued mid-write. The guard must fire even
    though the first call RETURNED (it returned on a timeout; the hub is still
    writing)."""
    hub = FakeHub()
    lease = _held(hub)
    state = tmp_path / "state"
    w1 = _writer(hub, tmp_path, state_dir=state)
    marker = w1.marker_path
    # Simulate "still in flight": the marker is left behind by an interrupted
    # run (the writer clears it on a clean return, so plant it directly).
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("pid=1 started=0 bundle=%s\n" % bundle)
    w2 = _writer(hub, tmp_path, state_dir=state)
    with pytest.raises(sd_mod.SdWriteError) as exc:
        w2.write(bundle, lease=lease, backup_dir=backup)
    assert "in flight" in str(exc.value)
    assert "corrupt" in str(exc.value).lower()
    assert not any(c[:2] == ["fpgahub", "target"] for c in hub.calls), \
        "the refused write must not have touched the hub"


def test_the_marker_is_cleared_after_a_completed_write(bundle, backup, tmp_path) -> None:
    hub = FakeHub()
    w = _writer(hub, tmp_path)
    w.write(bundle, lease=_held(hub), backup_dir=backup)
    assert not w.marker_path.exists()
    # ... so a legitimate second write is allowed
    w.write(bundle, lease=_held(hub), backup_dir=backup)


def test_a_stale_marker_does_not_wedge_the_tool_forever(bundle, backup, tmp_path) -> None:
    hub = FakeHub()
    w = _writer(hub, tmp_path, stale_after_s=0.0)
    w.marker_path.parent.mkdir(parents=True, exist_ok=True)
    w.marker_path.write_text("pid=1 started=0 bundle=x\n")
    res = w.write(bundle, lease=_held(hub), backup_dir=backup)
    assert res.ok is True


# --------------------------------------------------------------------------- #
# the wait
# --------------------------------------------------------------------------- #


def test_the_documented_interval_is_waited_not_suggested(bundle, backup, tmp_path) -> None:
    hub = FakeHub()
    clock = _Clock()
    res = _writer(hub, tmp_path, sleep=clock, wait_s=300.0).write(
        bundle, lease=_held(hub), backup_dir=backup)
    assert sum(clock.slept) == 300.0
    assert res.waited_s == 300.0


# --------------------------------------------------------------------------- #
# gates: lease, backup
# --------------------------------------------------------------------------- #


def test_refuses_when_the_board_is_not_held_by_us(bundle, backup, tmp_path) -> None:
    hub = FakeHub()
    _held(hub, "somebody-else")
    lease = lease_mod.Lease(token="TOK-0001", holder="claude-ops", target=hub.target)
    with pytest.raises(sd_mod.SdWriteError) as exc:
        _writer(hub, tmp_path).write(bundle, lease=lease, backup_dir=backup)
    assert "not held by" in str(exc.value)


def test_refuses_without_a_verified_backup(bundle, tmp_path) -> None:
    """sd_install OVERWRITES in place. On 2026-07-16 the previous nanosoc.bit
    was overwritten with no backup and is gone. The backup is a GATE."""
    hub = FakeHub()
    with pytest.raises(sd_mod.SdWriteError) as exc:
        _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=None)
    assert "backup" in str(exc.value)


def test_refuses_an_implausible_backup(bundle, tmp_path) -> None:
    """An empty or tiny .bit is a FAILED backup wearing a backup's clothes."""
    hub = FakeHub()
    empty = tmp_path / "backup-empty"
    empty.mkdir()
    (empty / "nanosoc.bit").write_bytes(b"tiny")
    with pytest.raises(sd_mod.SdWriteError) as exc:
        _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=empty)
    assert "implausibly small" in str(exc.value)


def test_backup_gate_can_be_waived_explicitly_and_says_so(bundle, tmp_path) -> None:
    hub = FakeHub()
    res = _writer(hub, tmp_path).write(
        bundle, lease=_held(hub), backup_dir=None, require_backup=False)
    assert res.ok is True
    assert any("NO BACKUP" in line for line in res.log)


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


def test_verifies_by_md5_read_back_when_a_path_is_given(bundle, backup, tmp_path) -> None:
    import hashlib
    hub = FakeHub()
    res = _writer(hub, tmp_path).write(
        bundle, lease=_held(hub), backup_dir=backup,
        verify_path="/mnt/mps3sd/MB/HBI0309C/Nanosoc/nanosoc.bit")
    assert res.verified is True
    assert res.md5 == hashlib.md5(bundle.read_bytes()).hexdigest()


def test_a_verify_mismatch_is_loud(bundle, backup, tmp_path) -> None:
    """A silent bad write is the worst outcome available here: the board comes
    up on garbage and the next hour goes on 'why is the shell dark'."""
    hub = FakeHub(sd_corrupts=True)
    with pytest.raises(sd_mod.SdWriteError) as exc:
        _writer(hub, tmp_path).write(
            bundle, lease=_held(hub), backup_dir=backup,
            verify_path="/mnt/mps3sd/MB/HBI0309C/Nanosoc/nanosoc.bit")
    assert "md5" in str(exc.value).lower()


def test_no_verify_path_reports_unverified_honestly(bundle, backup, tmp_path) -> None:
    """The hub API exposes no SD read-back of its own. Say so; do not report a
    verified write that was never verified."""
    hub = FakeHub()
    res = _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup)
    assert res.verified is None
    assert res.verify_reason and "read-back" in res.verify_reason


def test_never_claims_the_shell_was_fielded(bundle, backup, tmp_path) -> None:
    """FIELDED_SHELL.md: an sd_install timing out is not evidence of anything;
    only the shell reporting its own id is. Nothing in the result may say
    'fielded'."""
    hub = FakeHub()
    res = _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup)
    joined = " ".join(res.log).lower()
    assert "fielded" not in joined or "not " in joined
    assert res.fielded is False


# --------------------------------------------------------------------------- #
# logging + command shape
# --------------------------------------------------------------------------- #


def test_every_step_logs(bundle, backup, tmp_path) -> None:
    hub = FakeHub()
    res = _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup)
    joined = "\n".join(res.log)
    for step in ("lease", "backup", "program", "wait", "verify"):
        assert step in joined.lower(), "step %r never logged" % step


def test_program_uses_the_sd_method_on_the_lease_target(bundle, backup, tmp_path) -> None:
    hub = FakeHub()
    _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup)
    prog = [c for c in hub.calls if c[:2] == ["fpgahub", "target"]]
    assert prog, "no target program call"
    assert prog[0][:4] == ["fpgahub", "target", "program", "mps3_pl"]
    assert "--method" in prog[0] and prog[0][prog[0].index("--method") + 1] == "sd"


def test_missing_bundle_fails_before_touching_the_hub(tmp_path) -> None:
    hub = FakeHub()
    with pytest.raises(sd_mod.SdWriteError) as exc:
        _writer(hub, tmp_path).write(tmp_path / "nope.bit", lease=_held(hub))
    assert "nope.bit" in str(exc.value)


# --------------------------------------------------------------------------- #
# 2026-09-24 (ILA handoff defect 6 follow-up): the REAL client's timeout shape,
# --yes not --force, and the pointer to `sd field`
# --------------------------------------------------------------------------- #


def test_the_real_clients_rc1_timed_out_is_expected_and_never_retried(bundle, backup,
                                                                      tmp_path) -> None:
    """The fpgahub client gives up after ~30 s on its own and exits 1 with
    `POST /targets/mps3_pl/program: timed out` (W1). That used to raise
    "sd_install failed" -- the message an operator retries on."""
    hub = FakeHub(sd_program_times_out=False, sd_client_gives_up=True)
    res = _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup)
    assert res.ok is True and res.timed_out is True
    joined = "\n".join(res.log)
    assert "EXPECTED" in joined and "NOT retrying" in joined
    progs = [c for c in hub.calls if c[:3] == ["fpgahub", "target", "program"]]
    assert len(progs) == 1, "the write was issued more than once"


def test_a_real_refusal_is_still_an_error_and_says_not_retried(bundle, backup,
                                                               tmp_path) -> None:
    hub = FakeHub(sd_program_times_out=False, sd_requires_confirm=True)
    w = _writer(hub, tmp_path)

    def runner(argv, timeout=None):
        argv = [a for a in argv if a != "--yes"]           # a caller that forgot --yes
        return hub(argv, timeout)
    w._runner = runner
    with pytest.raises(sd_mod.SdWriteError) as exc:
        w.write(bundle, lease=_held(hub), backup_dir=backup)
    assert "confirm_required" in str(exc.value) and "NOT retried" in str(exc.value)
    assert not w.marker_path.exists(), "a refused write left its in-flight marker"


def test_the_write_passes_yes_and_no_skip_if_loaded_not_force(bundle, backup,
                                                              tmp_path) -> None:
    hub = FakeHub(sd_requires_confirm=True)
    res = _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup)
    assert res.ok
    (prog,) = [c for c in hub.calls if c[:3] == ["fpgahub", "target", "program"]]
    assert "--yes" in prog and "--no-skip-if-loaded" in prog
    assert "--force" not in prog, "--force also drops the part check"


def test_the_log_points_at_sd_field_for_the_witness_and_the_reboot(bundle, backup,
                                                                    tmp_path) -> None:
    hub = FakeHub()
    res = _writer(hub, tmp_path).write(bundle, lease=_held(hub), backup_dir=backup,
                                       holder="claude-ops")
    nxt = [line for line in res.log if line.startswith("next:")]
    assert nxt and "pyverify sd field" in nxt[0] and "--already-written" in nxt[0]
    assert "--holder claude-ops" in nxt[0] and "program dispatched" in nxt[0]
