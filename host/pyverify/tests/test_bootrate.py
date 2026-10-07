"""Tests for :mod:`pyverify.bootrate` — MEASURING the boot lottery.

WHY THIS EXISTS
    The MCC configures the KU115 from the config SD at power-on, and the SAME
    image comes up lit on some power-ups and dark on others. ``LOG.TXT`` is
    byte-identical on a good boot and a dark one, so the failure is at the
    unlogged release-to-RUN step. Every plan in the tree quotes "about 1 time in
    4" from a note recording FOUR attempts. Nobody has a rate.

    A rate is a number with a denominator, an interval, and a recorded
    configuration — and a before/after for one variable at a time. That is what
    ``pyverify boot-rate`` produces and what these tests pin.

WHAT IS FAKED, AND WHY THAT IS ENOUGH
    Everything board-facing is a seam: the hub (``tests.fakehub.FakeHub``, the
    same CLI double the lease/sd tests drive), the shell probe, the clock, and
    the optional JTAG witness. No socket, no hub, no board, and **no real
    sleeps** — a 10-iteration campaign with a 90 s deadline runs in
    milliseconds, which is the only way a deadline can be tested at all.

    What is NOT faked: the outcome classification, the summary arithmetic
    (including the Wilson interval), the run-record schema, ``compare``, and the
    SD-bundle variant machinery — that last one shells out to the REAL
    ``fpga/mps3_sd/assemble_sd.sh``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time as _time
from pathlib import Path

import pytest

from fakehub import FakeHub
from pyverify import bootrate as br
from pyverify import cli as cli_mod
from pyverify import lease as lease_mod
from pyverify.fielded import repo_root
from pyverify.mailbox import (STAGE0_CONFIRM_MAGIC, STAGE0_STATUS_ADDR,
                              STAGE0_STATUS_BYTES, STAGE0_STATUS_FIELDS,
                              STAGE0_STATUS_MAGIC, STAGE0_STATUS_VERSION,
                              FakeMemory, read_stage0_status)
from pyverify.testing.fakeshell import FakeShell


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class FakeClock:
    """A monotonic clock that only ever moves when someone sleeps.

    The point of the whole seam: a 90 s deadline over 10 iterations is 15
    minutes of wall time, and a test that waited it out would be deleted within
    a week. ``slept`` is the record the deadline assertions read.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.slept: "list[float]" = []

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        # An epoch-shaped number so the record's timestamps are parseable.
        return 1_800_000_000.0 + (self.t - 1000.0)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class ScriptedProbe:
    """A shell that answers on a schedule.

    ``answers_after`` is a per-iteration list: seconds after the reset at which
    ping starts answering, or ``None`` for a boot that never comes up.
    ``connect_after`` models the other real shape — the TCP connect succeeding
    while the verb never returns a shell_id (a configured FPGA whose firmware
    hung), which must NOT be recorded as a dark boot.
    """

    def __init__(self, clock: FakeClock, answers_after, *,
                 connect_after=None, shell_id="0x1234abcd",
                 version=("1.2.3", "0x0102030a"), up_before_reset=True) -> None:
        self.clock = clock
        self.answers_after = list(answers_after)
        self.connect_after = list(connect_after or [])
        self.shell_id = shell_id
        self.version = version
        self.up_before_reset = up_before_reset
        self.iteration = -1
        self.t_reset = 0.0
        self.calls = 0

    def arm(self, iteration: int, t_reset: float) -> None:
        self.iteration = iteration
        self.t_reset = t_reset

    def __call__(self) -> br.Probe:
        self.calls += 1
        if self.iteration < 0:
            # Before the first reset: the pre-reset liveness check.
            return self._up() if self.up_before_reset else br.Probe(up=False)
        elapsed = self.clock.now() - self.t_reset
        when = self.answers_after[self.iteration]
        if when is not None and elapsed >= when:
            return self._up()
        conn = (self.connect_after[self.iteration]
                if self.iteration < len(self.connect_after) else None)
        if conn is not None and elapsed >= conn:
            return br.Probe(up=False, connected=True, error="no shell_id in reply")
        return br.Probe(up=False, connected=False, error="connection refused")

    def _up(self) -> br.Probe:
        return br.Probe(up=True, shell_id=self.shell_id, connected=True,
                        version={"harness": self.version[0], "ver32": self.version[1]})


def _hub(**kw) -> FakeHub:
    hub = FakeHub(**kw)
    return hub


def _held(hub: FakeHub, holder: str = "claude-bootrate") -> FakeHub:
    """Take the lease on the fake hub, the way a real campaign must."""
    lease_mod.LeaseClient(hub, chassis=hub.chassis, target=hub.target).acquire(
        holder, poll_s=0, sleep=lambda _s: None)
    return hub


def _runner(hub: FakeHub, clock: FakeClock, probe, **kw) -> br.BootRateRunner:
    kw.setdefault("holder", "claude-bootrate")
    kw.setdefault("lease", lease_mod.LeaseClient(
        hub, chassis=hub.chassis, target=hub.target))
    return br.BootRateRunner(
        probe=probe,
        resetter=br.HubResetter(hub, target=hub.target),
        runner=hub, sleep=clock.sleep, now=clock.now, wall=clock.wall, **kw)


def _campaign(runner: br.BootRateRunner, probe: ScriptedProbe, **kw) -> dict:
    """Drive one campaign, arming the scripted probe per iteration."""
    kw.setdefault("n", 3)
    kw.setdefault("method", "msd")
    kw.setdefault("deadline_s", 90.0)
    kw.setdefault("poll_s", 5.0)
    kw.setdefault("gap_s", 0.0)
    runner.on_iteration_start = probe.arm       # the harness tells the double where it is
    return runner.run(**kw)


# --------------------------------------------------------------------------- #
# the gates: a campaign that must not touch the board
# --------------------------------------------------------------------------- #


def test_refuses_a_run_with_no_lease_held() -> None:
    """An unheld board is NOT ours. `preflight` says True for an unheld board;
    a campaign that reboots it ten times needs a stronger answer than that."""
    hub = _hub()                                  # nobody holds it
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    runner = _runner(hub, clock, probe)
    with pytest.raises(br.BootRateError) as exc:
        _campaign(runner, probe, n=1)
    assert "lease" in str(exc.value).lower()
    assert not [c for c in hub.calls if c[:3] == ["fpgahub", "target", "reset"]], \
        "refused run still issued a reset"


def test_refuses_a_lease_held_by_someone_else() -> None:
    hub = _held(_hub(), holder="someone-else")
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    runner = _runner(hub, clock, probe, holder="claude-bootrate")
    with pytest.raises(br.BootRateError) as exc:
        _campaign(runner, probe, n=1)
    assert "someone-else" in str(exc.value)


def test_fpgahub_mcc_that_writes_to_tty_01_is_the_old_no_op_and_stops_the_run() -> None:
    """The "mcc is a proven no-op" refusal is SUPERSEDED (every recorded no-op
    was REBOOT sent to tty_01, FPGA UART lane 0). What survives is the check:
    fpgahub's reply names the tty, and anything but tty_00 is not a reboot."""
    hub = _held(_hub())
    hub.run = _mcc_reply(hub, "/dev/mps3_pl/tty_01")
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0, 10.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=2, method="mcc")
    assert len(rec["iterations"]) == 1, "a wrong-tty reset must stop the campaign"
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_HUB_ERROR
    assert "tty_01" in it["reason"] and "not the MCC console" in it["reason"]
    assert it["reset"]["fatal"] is True and it["reset"]["sent"] is False
    assert rec["campaign"]["aborted"]
    assert rec["summary"]["rate"] is None


def _mcc_reply(hub: FakeHub, tty: str):
    """Make a FakeHub's `target reset --method mcc` answer the way fpgahub's
    mps3_mcc_reboot plugin does: it names the tty it wrote to."""
    plain = hub.run

    def run(argv):
        if argv[:3] == ["fpgahub", "target", "reset"] and "mcc" in argv:
            hub.resets.append("mcc")
            return lease_mod.RunResult(
                0, "ok %s reset method=mcc plugin=mps3_mcc_reboot\n  REBOOT sent "
                   "via tty_share broker on %s\n" % (hub.target, tty), "")
        return plain(argv)
    return run


# --------------------------------------------------------------------------- #
# the outcomes
# --------------------------------------------------------------------------- #


def test_an_up_boot_records_time_to_ping_static_id_and_version() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [20.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_UP
    assert it["t_first_ping"] is not None
    assert it["time_to_ping_s"] == pytest.approx(20.0, abs=5.0)
    assert it["static_id"] == "0x1234abcd"
    assert it["version"]["harness"] == "1.2.3"
    assert rec["summary"]["rate"] == 1.0


def test_a_dark_boot_is_recorded_as_dark_not_as_an_error() -> None:
    """The whole point of the harness: a board that does not come up is DATA."""
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [None, 15.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=2)
    outcomes = [it["outcome"] for it in rec["iterations"]]
    assert outcomes == [br.OUTCOME_DARK, br.OUTCOME_UP]
    assert rec["iterations"][0]["t_first_ping"] is None
    assert rec["summary"]["dark"] == 1
    assert rec["summary"]["rate"] == 0.5
    assert rec["summary"]["hub_error"] == 0


def test_a_configured_but_inert_board_is_a_timeout_not_a_dark_boot() -> None:
    """DONE high + nothing answering is a HUNG FIRMWARE, not a failed
    configuration. Recording it as dark would poison the very rate this tool
    exists to measure."""
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [None], connect_after=[10.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1)
    assert rec["iterations"][0]["outcome"] == br.OUTCOME_TIMEOUT
    assert rec["summary"]["timeout"] == 1
    assert rec["summary"]["dark"] == 0


def test_a_hub_error_is_recorded_as_hub_error_and_left_out_of_the_rate() -> None:
    """A reset the hub refused is an experiment that never ran. Counting it as a
    dark boot would report a worse rate than the board has."""
    hub = _held(_hub(reset_fails=True))
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0, 10.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=2)
    assert [it["outcome"] for it in rec["iterations"]] == [br.OUTCOME_HUB_ERROR] * 2
    assert rec["summary"]["hub_error"] == 2
    assert rec["summary"]["rate"] is None, "no valid attempts -> no rate, not 0.0"
    assert rec["summary"]["rate_basis"] == "up / (n - hub_error)"


def test_a_reset_that_never_takes_the_board_down_is_a_hub_error() -> None:
    """The mcc-no-op signature, generalised: the reset surface reports ok and
    the shell keeps answering across it. That is not a boot -- and if
    `--method msd` ever behaves this way, this is how W0 finds out."""
    hub = _held(_hub())
    clock = FakeClock()
    # answers immediately and forever: it never went down.
    probe = ScriptedProbe(clock, [0.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1, down_deadline_s=30.0)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_HUB_ERROR
    assert "never" in " ".join(it["notes"]).lower()


def test_await_down_can_be_waived_and_says_so_in_the_record() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [0.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1, await_down=False)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_UP
    assert any("--no-await-down" in n or "pre-reboot" in n for n in it["notes"]), \
        "an unwitnessed 'up' must record that it may be the PRE-reboot shell"


def test_a_board_already_down_before_the_reset_does_not_wait_for_a_down_edge() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [12.0], up_before_reset=False)
    rec = _campaign(_runner(hub, clock, probe), probe, n=1)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_UP
    assert any("already down" in n for n in it["notes"])


# --------------------------------------------------------------------------- #
# the deadline
# --------------------------------------------------------------------------- #


def test_the_deadline_is_honoured_and_no_real_time_passes() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [None])
    import time as _t
    t0 = _t.monotonic()
    rec = _campaign(_runner(hub, clock, probe), probe, n=1,
                    deadline_s=90.0, poll_s=5.0)
    assert _t.monotonic() - t0 < 2.0, "the runner slept for real"
    assert sum(clock.slept) >= 90.0, "gave up before the deadline"
    assert rec["iterations"][0]["waited_s"] == pytest.approx(90.0, abs=5.0)
    assert rec["iterations"][0]["waited_s"] <= 90.0 + 5.0


def test_a_zero_poll_or_deadline_is_refused_rather_than_spinning() -> None:
    """A 0 poll interval is what makes the wait loops stop advancing: against a
    real clock it busy-hammers the shell for the whole deadline, against an
    injected one it never terminates. Found by mutating the deadline check."""
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    for kw in ({"poll_s": 0.0}, {"deadline_s": 0.0}, {"n": 0},
               {"down_deadline_s": 0.0}):
        with pytest.raises(br.BootRateInputError):
            _campaign(_runner(hub, clock, probe), probe, **kw)
    # ...and the zero down-window names the flag the caller actually wanted,
    # because it would otherwise record EVERY iteration as hub-error.
    with pytest.raises(br.BootRateInputError) as exc:
        _campaign(_runner(hub, clock, probe), probe, down_deadline_s=0.0)
    assert "--no-await-down" in str(exc.value)


def test_time_to_ping_is_measured_from_the_reset_not_from_the_down_edge() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [35.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1, poll_s=5.0)
    it = rec["iterations"][0]
    assert it["time_to_ping_s"] == pytest.approx(35.0, abs=5.0)
    assert it["t_first_ping"] > it["t_reset"]


# --------------------------------------------------------------------------- #
# the MCC log, through the SD seam
# --------------------------------------------------------------------------- #


def test_the_mcc_log_is_captured_through_the_same_seam_sd_py_uses(tmp_path) -> None:
    hub = _held(_hub())
    hub.sd_host_files["/mnt/V2M_MPS3/LOG.TXT"] = "MCC boot log\r\n"
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    runner = _runner(hub, clock, probe, mcc_log_path="/mnt/V2M_MPS3/LOG.TXT")
    rec = _campaign(runner, probe, n=1)
    import hashlib
    assert rec["iterations"][0]["mcc_log_sha"] == hashlib.sha256(
        b"MCC boot log\r\n").hexdigest()
    assert ["sha256sum", "/mnt/V2M_MPS3/LOG.TXT"] in hub.calls


def test_an_unreachable_mcc_log_is_null_with_a_reason_never_a_failed_run() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    runner = _runner(hub, clock, probe, mcc_log_path="/mnt/nope/LOG.TXT")
    rec = _campaign(runner, probe, n=1)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_UP
    assert it["mcc_log_sha"] is None
    assert any("LOG.TXT" in n or "sha256" in n.lower() for n in it["notes"])


def test_no_log_path_configured_says_so_rather_than_pretending() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1)
    assert rec["iterations"][0]["mcc_log_sha"] is None
    assert rec["campaign"]["mcc_log_path"] is None


# --------------------------------------------------------------------------- #
# the OPTIONAL JTAG witness
# --------------------------------------------------------------------------- #


def test_the_witness_is_off_by_default() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [None])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1)
    assert rec["campaign"]["witness"] is None
    assert rec["iterations"][0]["witness"] is None
    assert rec["iterations"][0]["outcome"] == br.OUTCOME_DARK


def test_a_witness_saying_CONFIGURED_turns_a_dark_boot_into_a_timeout() -> None:
    """DONE/USERCODE is the stronger evidence; when it is available it decides."""
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [None])
    # A synthetic usercode: a test is never a claim about what is on the board.
    runner = _runner(hub, clock, probe,
                     witness=lambda: br.Witness(configured=True,
                                                usercode="0x00C0FFEE"))
    rec = _campaign(runner, probe, n=1)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_TIMEOUT
    assert it["witness"]["configured"] is True
    assert it["witness"]["usercode"] == "0x00C0FFEE"


def test_the_record_names_the_witness_COMMAND_not_its_class() -> None:
    """"CommandWitness" is not a fact anyone can reproduce; the command is."""
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    witness = br.CommandWitness("true # my-done-probe.tcl")
    rec = _campaign(_runner(hub, clock, probe, witness=witness), probe, n=1)
    assert rec["campaign"]["witness"] == "true # my-done-probe.tcl"


def test_a_witness_that_raises_does_not_fail_the_run() -> None:
    hub = _held(_hub())
    clock = FakeClock()

    def boom():
        raise OSError("xsdb not on this host")

    probe = ScriptedProbe(clock, [None])
    rec = _campaign(_runner(hub, clock, probe, witness=boom), probe, n=1)
    assert rec["iterations"][0]["outcome"] == br.OUTCOME_DARK
    assert any("xsdb" in n for n in rec["iterations"][0]["notes"])


def test_the_command_witness_adapter_parses_json_from_a_real_subprocess() -> None:
    w = br.CommandWitness("printf '{\"configured\": false, \"usercode\": \"0x0\"}'")
    got = w()
    assert got.configured is False
    assert got.usercode == "0x0"


# --------------------------------------------------------------------------- #
# the run record + its schema
# --------------------------------------------------------------------------- #


def test_the_record_carries_a_versioned_schema_and_validates() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0, None])
    rec = _campaign(_runner(hub, clock, probe), probe, n=2)
    assert rec["schema"] == br.SCHEMA_ID
    assert br.SCHEMA_ID.endswith("/1"), "the schema id must carry its version"
    br.validate_record(rec)                     # raises on a bad record
    # It is JSON, all the way down.
    json.loads(json.dumps(rec))


def test_validate_record_refuses_a_foreign_or_unversioned_record() -> None:
    for bad in ({}, {"schema": "something-else/1"}, {"schema": "mps3.boot-rate/99"}):
        with pytest.raises(br.BootRateInputError):
            br.validate_record(bad)


def test_the_json_schema_describes_the_record_it_actually_writes() -> None:
    schema = br.RUN_RECORD_SCHEMA
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["properties"]["schema"]["const"] == br.SCHEMA_ID
    it = schema["properties"]["iterations"]["items"]["properties"]
    for field in ("iteration", "t_reset", "t_first_ping", "outcome",
                  "static_id", "version", "mcc_log_sha", "notes"):
        assert field in it, f"schema is missing the {field!r} the record carries"
    assert set(it["outcome"]["enum"]) == set(br.OUTCOMES)


def test_the_summary_arithmetic_including_the_interval() -> None:
    outcomes = ([br.OUTCOME_UP] * 7 + [br.OUTCOME_DARK] * 3)
    iterations = [{"outcome": o, "time_to_ping_s": (10.0 + i if o == br.OUTCOME_UP
                                                    else None)}
                  for i, o in enumerate(outcomes)]
    s = br.summarise(iterations)
    assert (s["n"], s["up"], s["dark"]) == (10, 7, 3)
    assert s["rate"] == pytest.approx(0.7)
    assert s["mean_time_to_ping_s"] == pytest.approx(13.0)
    assert s["max_time_to_ping_s"] == pytest.approx(16.0)
    # Wilson score interval, 95% -- 7/10 is (0.3968, 0.8922). With N=10 the
    # interval is HALF THE RANGE: that is the number that stops a 7/10 vs 9/10
    # difference being reported as a fix.
    assert s["ci95"][0] == pytest.approx(0.3968, abs=1e-3)
    assert s["ci95"][1] == pytest.approx(0.8922, abs=1e-3)


def test_the_interval_of_no_valid_attempts_is_the_whole_range() -> None:
    s = br.summarise([{"outcome": br.OUTCOME_HUB_ERROR, "time_to_ping_s": None}])
    assert s["rate"] is None
    assert s["ci95"] == [0.0, 1.0]


# --------------------------------------------------------------------------- #
# the variants (as recorded by the harness)
# --------------------------------------------------------------------------- #


def test_an_unknown_variant_is_refused_before_the_board_is_touched() -> None:
    with pytest.raises(br.BootRateInputError) as exc:
        br.variant_delta("NO_SUCH_VARIANT")
    assert "ETH_SMB0" in str(exc.value), "the refusal must list the real ones"


def test_the_variant_keys_are_read_from_the_sd_variants_dir_not_typed_in() -> None:
    delta = br.variant_delta("ETH_SMB0")
    assert delta["keys"]["nanosoc.txt"]["FPGA_SMB"] == "FALSE"
    assert delta["keys"]["nanosoc.txt"]["FPGA_LAN"] == "FALSE"
    assert delta["description"]


def test_the_campaign_records_the_exact_variant_under_test() -> None:
    hub = _held(_hub())
    clock = FakeClock()
    probe = ScriptedProbe(clock, [10.0])
    rec = _campaign(_runner(hub, clock, probe), probe, n=1, variant="ETH_SMB0")
    assert rec["campaign"]["variant"] == "ETH_SMB0"
    assert rec["campaign"]["variant_keys"]["nanosoc.txt"]["FPGA_SMB"] == "FALSE"


# --------------------------------------------------------------------------- #
# compare -- the before/after the audit asked for
# --------------------------------------------------------------------------- #


def _record(rate_up: int, n: int, **campaign) -> dict:
    iterations = [
        {"iteration": i, "t_reset": 0.0, "t_first_ping": (1.0 if i < rate_up else None),
         "time_to_ping_s": (20.0 if i < rate_up else None),
         "waited_s": 20.0, "outcome": (br.OUTCOME_UP if i < rate_up else br.OUTCOME_DARK),
         "static_id": None, "version": None, "mcc_log_sha": None, "witness": None,
         "notes": []}
        for i in range(n)
    ]
    camp = {"schema_note": "", "n": n, "method": "msd", "deadline_s": 90.0,
            "variant": None, "variant_keys": {}, "started_at": "2026-09-10T00:00:00Z",
            "finished_at": "2026-09-10T00:30:00Z", "target": "mps3_pl",
            "chassis": "mps3_01", "holder": "claude-bootrate", "host": "board",
            "control_port": 6900, "mcc_log_path": None, "witness": None,
            "poll_s": 5.0, "gap_s": 0.0, "down_deadline_s": 30.0, "await_down": True,
            "tool": "pyverify boot-rate", "notes": []}
    camp.update(campaign)
    return {"schema": br.SCHEMA_ID, "campaign": camp, "iterations": iterations,
            "summary": br.summarise(iterations)}


def test_compare_names_the_variable_that_changed_and_prints_both_rates(capsys) -> None:
    before = _record(3, 10)
    after = _record(9, 10, variant="ETH_SMB0",
                    variant_keys={"nanosoc.txt": {"FPGA_SMB": "FALSE",
                                                  "FPGA_LAN": "FALSE"}})
    text = br.format_compare(br.compare(before, after))
    assert "0.30" in text and "0.90" in text
    assert "variant" in text and "ETH_SMB0" in text
    assert "FPGA_SMB" in text, "the key that changed, not just the variant name"


def test_one_variant_is_ONE_variable_however_many_keys_it_moves() -> None:
    """`--variant ETH_SMB0` moves one variable and shows two key rows. A tool
    that nagged "more than one variable moved" on every legitimate comparison
    would be ignored by the third one."""
    cmp = br.compare(
        _record(3, 10),
        _record(9, 10, variant="ETH_SMB0",
                variant_keys={"nanosoc.txt": {"FPGA_SMB": "FALSE",
                                              "FPGA_LAN": "FALSE"}}))
    assert cmp["fields_changed"] == ["variant"]
    assert "campaign settings moved" not in br.format_compare(cmp)


def test_compare_flags_two_variables_moving_at_once() -> None:
    cmp = br.compare(_record(3, 10), _record(9, 10, variant="ETH_SMB0",
                                             method="hard"))
    assert sorted(cmp["fields_changed"]) == ["method", "variant"]
    assert "campaign settings moved" in br.format_compare(cmp)


def test_compare_catches_a_variant_that_was_EDITED_between_the_two_runs() -> None:
    """Same name, different keys: each record describes a card the other is not
    talking about, and nothing downstream could see it."""
    keys_before = {"nanosoc.txt": {"FPGA_SMB": "FALSE"}}
    keys_after = {"nanosoc.txt": {"FPGA_SMB": "FALSE", "FPGA_LAN": "FALSE"}}
    cmp = br.compare(_record(3, 10, variant="ETH_SMB0", variant_keys=keys_before),
                     _record(9, 10, variant="ETH_SMB0", variant_keys=keys_after))
    assert cmp["variant_redefined"] is True
    assert "WARNING" in br.format_compare(cmp)


def test_compare_says_plainly_when_nothing_changed() -> None:
    cmp = br.compare(_record(3, 10), _record(4, 10))
    assert cmp["variables"] == []
    text = br.format_compare(cmp)
    assert "repeatability" in text.lower()


def test_compare_refuses_to_compare_two_records_of_different_schema_versions() -> None:
    a = _record(3, 10)
    b = _record(9, 10)
    b["schema"] = "mps3.boot-rate/99"
    with pytest.raises(br.BootRateInputError):
        br.compare(a, b)


def test_compare_decides_on_the_exact_test_not_on_overlapping_intervals() -> None:
    """The trap this test exists to hold shut: at N=10 even 3/10 -> 9/10 has
    OVERLAPPING 95% intervals (0.11-0.60 vs 0.60-0.98) while the exact test puts
    it at p = 0.020. A tool that read overlap as "no difference" would tell the
    lead to throw away a genuine fix."""
    cmp = br.compare(_record(3, 10), _record(9, 10, variant="ETH_SMB0"))
    assert cmp["overlap"] is True
    assert cmp["p_value"] == pytest.approx(0.0198, abs=1e-3)
    assert cmp["significant"] is True
    assert "real difference" in br.format_compare(cmp)
    # 7/10 -> 8/10 is one boot: not a fix, and it must say so.
    cmp2 = br.compare(_record(7, 10), _record(8, 10, variant="ETH_SMB0"))
    assert cmp2["p_value"] == pytest.approx(1.0)
    assert cmp2["significant"] is False
    assert "NOT a demonstrated difference" in br.format_compare(cmp2)


def test_the_exact_test_agrees_with_scipy_on_tables_it_was_checked_against() -> None:
    """Cross-checked against ``scipy.stats.fisher_exact`` (NOT a dependency:
    pyverify is stdlib-only, so the oracle is pinned here as numbers)."""
    assert br.fisher_exact(3, 7, 9, 1) == pytest.approx(0.019766611097880443)
    assert br.fisher_exact(7, 3, 8, 2) == pytest.approx(1.0)
    assert br.fisher_exact(3, 7, 7, 3) == pytest.approx(0.1788954079975752)
    assert br.fisher_exact(0, 10, 10, 0) == pytest.approx(1.082508822446903e-05)
    assert br.fisher_exact(0, 0, 0, 0) == 1.0


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #


def test_cli_parses_the_documented_invocation() -> None:
    args = cli_mod.build_parser().parse_args(
        ["boot-rate", "--host", "board", "--n", "10", "--method", "msd",
         "--deadline", "90", "--out", "run.json", "--variant", "ETH_SMB0"])
    assert args.verb == "boot-rate"
    assert (args.n, args.method, args.deadline) == (10, "msd", 90.0)
    assert args.variant == "ETH_SMB0"


def test_cli_dry_run_prints_the_plan_and_touches_nothing(tmp_path, capsys) -> None:
    hub = _hub()                       # not even leased: a dry run asks nobody
    out = tmp_path / "run.json"
    rc = cli_mod.main(["boot-rate", "--host", "board", "--n", "10",
                       "--method", "msd", "--deadline", "90",
                       "--out", str(out), "--dry-run"], runner=hub)
    text = capsys.readouterr().out
    assert rc == 0
    assert hub.calls == [], "a dry run talked to the hub"
    assert not out.exists(), "a dry run wrote its output file"
    assert "msd" in text and "10" in text and "90" in text


def test_cli_needs_a_host_for_a_campaign_but_not_for_compare_or_schema(capsys) -> None:
    rc = cli_mod.main(["boot-rate", "--n", "1", "--holder", "h"], runner=_hub())
    err = capsys.readouterr().err
    assert rc == 2 and "--host" in err and "Traceback" not in err
    assert cli_mod.main(["boot-rate", "schema"]) == 0


def test_cli_mcc_dry_run_names_the_tty_the_pacing_and_the_linux_deadline(capsys) -> None:
    hub = _hub()
    rc = cli_mod.main(["boot-rate", "--host", "board", "--method", "mcc",
                       "--mcc-route", "tty", "--linux", "--dry-run"], runner=hub)
    out = capsys.readouterr().out
    assert rc == 0
    assert hub.calls == [], "a dry run talked to the hub"
    assert "/dev/mps3_pl/tty_00" in out and "100 ms/char" in out
    assert "180s" in out, "the Linux deadline, not 90 s"
    assert "NOT acquired or released" in out


def test_cli_refuses_a_burst_mcc_pace(capsys) -> None:
    """The MCC drops burst input: below 50 ms/char only the R lands."""
    rc = cli_mod.main(["boot-rate", "--host", "board", "--method", "mcc",
                       "--mcc-pace", "0.01", "--dry-run"], runner=_hub())
    err = capsys.readouterr().err
    assert rc == 2 and "burst" in err and "Traceback" not in err


def test_cli_refuses_without_an_explicit_holder(capsys, monkeypatch) -> None:
    """`_default_holder()` invents a per-PID name that can never match a lease
    someone else's process took -- for the other verbs that is a safe default,
    here it would silently make every campaign refuse for the wrong reason."""
    monkeypatch.delenv("MPS3_LEASE_HOLDER", raising=False)
    hub = _held(_hub())
    rc = cli_mod.main(["boot-rate", "--host", "board", "--n", "1"], runner=hub)
    err = capsys.readouterr().err
    assert rc == 1
    assert "--holder" in err


def test_cli_unknown_variant_exits_2_cleanly(capsys) -> None:
    hub = _held(_hub())
    rc = cli_mod.main(["boot-rate", "--host", "board", "--n", "1",
                       "--holder", "claude-bootrate", "--variant", "NOPE"],
                      runner=hub)
    err = capsys.readouterr().err
    assert rc == 2
    assert "NOPE" in err
    assert "Traceback" not in err


def test_cli_writes_the_run_record_and_prints_the_summary(tmp_path, capsys) -> None:
    hub = _held(_hub())
    out = tmp_path / "run.json"
    rc = cli_mod.main(
        ["boot-rate", "--host", "127.0.0.1", "--n", "2", "--holder", "claude-bootrate",
         "--deadline", "1", "--poll", "1", "--gap", "0", "--out", str(out),
         "--no-await-down"],
        runner=hub)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n"] == 2
    # The printed summary must be self-describing: a rate pasted into a report
    # without the configuration it was measured in is the "1 in 4" problem again.
    assert payload["method"] == "msd" and payload["variant"] is None
    assert payload["deadline_s"] == 1.0
    rec = json.loads(out.read_text())
    br.validate_record(rec)
    # Nothing is listening on 127.0.0.1:6900 in a test, so both boots are dark.
    assert rec["summary"]["up"] == 0
    assert rec["summary"]["rate"] == 0.0
    assert [c[:3] for c in hub.calls].count(["fpgahub", "target", "reset"]) == 2


def test_cli_compare_prints_before_and_after(tmp_path, capsys) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_record(3, 10)))
    b.write_text(json.dumps(_record(9, 10, variant="ETH_SMB0")))
    rc = cli_mod.main(["boot-rate", "compare", str(a), str(b)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0.30" in out and "0.90" in out and "ETH_SMB0" in out


def test_cli_compare_of_a_bad_file_exits_2(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rc = cli_mod.main(["boot-rate", "compare", str(bad), str(bad)])
    assert rc == 2
    assert "Traceback" not in capsys.readouterr().err


def test_cli_schema_prints_the_versioned_schema(capsys) -> None:
    rc = cli_mod.main(["boot-rate", "schema"])
    assert rc == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["schema"]["const"] == br.SCHEMA_ID


# --------------------------------------------------------------------------- #
# the SD bundle variants -- against the REAL assemble_sd.sh
# --------------------------------------------------------------------------- #


_SD = repo_root() / "fpga" / "mps3_sd"
_needs_sd = pytest.mark.skipif(
    not (_SD / "assemble_sd.sh").is_file() or shutil.which("bash") is None,
    reason="fpga/mps3_sd/assemble_sd.sh (or bash) not present in this checkout")


def _assemble(out: Path, *args: str, script: "Path | None" = None,
              env: "dict | None" = None) -> None:
    cmd = ["bash", str(script or (_SD / "assemble_sd.sh")), "C",
           "--out", str(out), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _tree(root: Path) -> "dict[str, bytes]":
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@_needs_sd
def test_the_default_bundle_is_byte_identical_to_its_templates(tmp_path) -> None:
    """THE CONTROL for the variant knob: with no --variant, every file in the
    bundle is byte-for-byte its template (board.txt modulo the documented
    @BOARD@ stamp). A knob that patched something by default fails here."""
    _assemble(tmp_path / "out")
    tree = _tree(tmp_path / "out" / "HBI0309C")
    tpl = _SD / "templates"
    assert tree["config.txt"] == (tpl / "config.txt").read_bytes()
    assert tree["MB/HBI0309C/Nanosoc/nanosoc.txt"] == (tpl / "nanosoc.txt").read_bytes()
    assert tree["MB/HBI0309C/board.txt"] == (
        tpl / "board.txt").read_bytes().replace(b"@BOARD@", b"HBI0309C")


@_needs_sd
def test_the_default_bundle_is_unchanged_by_the_variant_knob(tmp_path) -> None:
    """The stronger control, when git history is available: assemble with the
    script AS IT WAS before the knob existed, and with the current one. Skipped
    on a shallow clone (CI checks out depth 1), where the test above holds the
    line instead."""
    base = "02fb984"
    if subprocess.run(["git", "-C", str(repo_root()), "cat-file", "-e",
                       f"{base}:fpga/mps3_sd/assemble_sd.sh"],
                      capture_output=True).returncode != 0:
        pytest.skip(f"base commit {base} not in this clone (shallow?)")
    old = tmp_path / "old"
    (old / "templates").mkdir(parents=True)
    for f in (_SD / "templates").iterdir():
        shutil.copy2(f, old / "templates" / f.name)
    script = old / "assemble_sd.sh"
    script.write_bytes(subprocess.run(
        ["git", "-C", str(repo_root()), "show",
         f"{base}:fpga/mps3_sd/assemble_sd.sh"],
        capture_output=True, check=True).stdout)
    _assemble(tmp_path / "before", script=script)
    _assemble(tmp_path / "after")
    assert _tree(tmp_path / "before" / "HBI0309C") == _tree(tmp_path / "after" / "HBI0309C")


@_needs_sd
def test_a_variant_changes_exactly_the_keys_it_declares(tmp_path) -> None:
    _assemble(tmp_path / "base")
    _assemble(tmp_path / "var", "--variant", "ETH_SMB0")
    before = _tree(tmp_path / "base" / "HBI0309C")
    after = _tree(tmp_path / "var" / "HBI0309C")
    assert set(before) == set(after), "a variant added or removed a file"

    declared = br.variant_delta("ETH_SMB0")["keys"]
    changed_files = {p for p in before if before[p] != after[p]}
    assert changed_files == {"MB/HBI0309C/Nanosoc/" + f for f in declared}

    for path in changed_files:
        keys = declared[path.rsplit("/", 1)[-1]]
        b = before[path].decode().splitlines()
        a = after[path].decode().splitlines()
        # Line SETS, not a zip: the provenance banner shifts every line after it.
        removed = [ln for ln in b if ln not in a]
        added = [ln for ln in a if ln not in b]
        for ln in removed:
            key = ln.split(":", 1)[0].strip()
            assert key in keys, f"{path}: undeclared line REMOVED: {ln!r}"
        for ln in added:
            if ln.startswith(";"):
                assert "ETH_SMB0" in ln, f"{path}: stray comment added: {ln!r}"
                continue
            key = ln.split(":", 1)[0].strip()
            assert key in keys, f"{path}: undeclared key changed: {ln!r}"
            assert ln.split(":", 1)[1].split(";")[0].strip() == keys[key]
        assert any(ln.startswith(";") and "ETH_SMB0" in ln for ln in added), \
            "the bundle must say which variant it is"
        assert {ln.split(":", 1)[0].strip() for ln in removed} == set(keys), \
            "a declared key was not actually applied"


@_needs_sd
def test_the_ETH_SMB0_variant_agrees_with_the_pre_existing_env_knob(tmp_path) -> None:
    """`ETH_SMB=0 ./assemble_sd.sh` already existed. Two mechanisms for one
    experiment is how a campaign ends up reporting the wrong configuration, so
    they must produce the same keys."""
    _assemble(tmp_path / "env", env={"ETH_SMB": "0"})
    _assemble(tmp_path / "var", "--variant", "ETH_SMB0")
    env_txt = (tmp_path / "env" / "HBI0309C" / "MB/HBI0309C/Nanosoc/nanosoc.txt").read_text()
    var_txt = (tmp_path / "var" / "HBI0309C" / "MB/HBI0309C/Nanosoc/nanosoc.txt").read_text()

    def keys(text):
        return {ln.split(":")[0].strip(): ln.split(":", 1)[1].split(";")[0].strip()
                for ln in text.splitlines()
                if ln[:1].isalpha() and ":" in ln}

    assert keys(env_txt) == keys(var_txt)


@_needs_sd
def test_assemble_sd_refuses_an_unknown_variant(tmp_path) -> None:
    proc = subprocess.run(
        ["bash", str(_SD / "assemble_sd.sh"), "C", "--out", str(tmp_path / "o"),
         "--variant", "NO_SUCH"], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "NO_SUCH" in proc.stderr
    assert "ETH_SMB0" in proc.stderr, "name the variants that DO exist"


@_needs_sd
def test_a_variant_key_the_template_does_not_have_is_REFUSED(tmp_path) -> None:
    """An MPS3 config file accepts almost anything silently and fails at the I/O
    pads with no error, so a typo'd key would ship a card that claims an
    experiment it is not running -- and the campaign would report a rate for a
    configuration that never existed."""
    sd = tmp_path / "sd"
    shutil.copytree(_SD / "templates", sd / "templates")
    shutil.copy2(_SD / "assemble_sd.sh", sd / "assemble_sd.sh")
    bogus = sd / "variants" / "TYPO"
    bogus.mkdir(parents=True)
    (bogus / "variant.txt").write_text("DESCRIPTION: a variant with a typo\n")
    (bogus / "config.txt").write_text("AUTORUNDELY: 10\n")     # missing an 'A'
    proc = subprocess.run(
        ["bash", str(sd / "assemble_sd.sh"), "C", "--out", str(tmp_path / "o"),
         "--variant", "TYPO"], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "AUTORUNDELY" in proc.stderr
    assert "no key" in proc.stderr


@_needs_sd
def test_every_shipped_variant_applies_and_is_readable_by_the_harness(tmp_path) -> None:
    """A variant directory nobody can apply is a documented experiment that
    cannot be run."""
    names = br.list_variants()
    assert "ETH_SMB0" in names
    for name in names:
        delta = br.variant_delta(name)
        assert delta["description"], f"{name}: no description"
        assert delta["keys"], f"{name}: declares no key"
        _assemble(tmp_path / name, "--variant", name)
        for fname, keys in delta["keys"].items():
            sub = ("MB/HBI0309C/Nanosoc/" if fname != "config.txt" else "")
            text = (tmp_path / name / "HBI0309C" / (sub + fname)).read_text()
            for key, value in keys.items():
                assert any(ln.startswith(key + ":") and value in ln
                           for ln in text.splitlines()), \
                    f"{name}: {fname}:{key} was not stamped {value}"


# --------------------------------------------------------------------------- #
# the Linux harness, unattended: MCC REBOOT on tty_00 + boot checks
# --------------------------------------------------------------------------- #
#
# W1 (2026-09-24) fielded a static with a paced MCC REBOOT on tty_00 and no
# hands. These tests drive a whole campaign against FakeShell(profile="linux")
# over real sockets, with a simulated reboot behind a fake hub: the hub-side
# MCC writer's JSON verdict is modelled here, and the writer SCRIPT itself runs
# for real against a pseudo-terminal further down.

TTY00 = "/dev/mps3_pl/tty_00"


class RealClock:
    now = staticmethod(_time.monotonic)
    wall = staticmethod(_time.time)
    sleep = staticmethod(_time.sleep)


class SimBoard:
    """An MPS3 running the Linux harness, behind a hub.

    ``__call__`` is the HubRunner seam: fpgahub verbs go to a FakeHub (leased
    by us); ``python3 -c HUB_MCC_REBOOT_PY`` is answered in that script's own
    JSON, and a REBOOT powers the shell down. ``after`` seconds later the
    shell comes back in the planned ``mode`` with a FRESH kernel
    (``os_up_ms`` restarts) and a stage0 block describing the planned boot.
    Before the campaign the kernel has been up an hour.

    ``boots`` is one spec per REBOOT, or None for a boot that never returns:
    ``{"after": s, "mode": "run"|"rescue", "reason": ..., "stage0": {...}}``.
    """

    def __init__(self, clock, boots, *, second_reader=None, fpgahub_tty=None,
                 realtime=False, no_op=False):
        self.clock = clock
        self.boots = list(boots)
        self.second_reader = second_reader
        self.fpgahub_tty = fpgahub_tty
        self.realtime = realtime
        self.no_op = no_op
        self.hub = _held(FakeHub())
        # A powered-off board frees its ports and must get the SAME ones back.
        # OS-assigned ports come from the ephemeral range, where any outgoing
        # connection on this (shared) host can take one while it is free, so
        # the double lives below that range.
        ports = _free_low_ports(7)
        self.shell = FakeShell(
            "127.0.0.1", profile="linux", os_boot_ms=3_600_000,
            identify_rate_per_s=1000.0,
            **dict(zip(("control_port", "tftp_port", "raw_tcp_port", "uart0_port",
                        "uart1_port", "swo_port", "identify_port"), ports))).start()
        self.mem = FakeMemory()
        self._stage0()
        self.pending = None
        self.reboots = 0
        self.mcc_calls = []
        self.fpgahub_resets = 0
        self.sp = br.ShellProbe("127.0.0.1", port=self.shell.control_port,
                                identify_port=self.shell.identify_port,
                                identify_timeout=0.01)
        self.probe = _TickingProbe(self)

    def close(self):
        self.shell.stop()

    # -- the hub ------------------------------------------------------------ #

    def __call__(self, argv, timeout=None):
        argv = list(argv)
        if argv[:1] == ["python3"] or argv[:1] == [sys.executable]:
            return self._mcc(json.loads(argv[-1]))
        if argv[:3] == ["fpgahub", "target", "reset"] and "mcc" in argv \
                and self.fpgahub_tty:
            self.fpgahub_resets += 1
            if self.fpgahub_tty.endswith("/tty_00"):
                self._reboot()
            return lease_mod.RunResult(
                0, "ok mps3_pl reset method=mcc plugin=mps3_mcc_reboot\n"
                   "  REBOOT sent directly on %s\n" % self.fpgahub_tty, "")
        return self.hub(argv, timeout)

    def _mcc(self, args):
        self.mcc_calls.append(args)
        out = {"tty": args["tty"], "mode": args["mode"], "sent": False,
               "ack": False, "others": [], "prompt": "\r\n\r\nCmd> ", "echo": ""}
        if self.second_reader:
            out.update(others=[self.second_reader], rc=3,
                       reason="another process reads %s" % args["tty"])
            return lease_mod.RunResult(3, json.dumps(out) + "\n", "")
        if args["mode"] != "scan":
            self._reboot()
            out.update(sent=True, ack=True, echo="REBOOT\r\nRebooting...\r\n")
        out["rc"] = 0
        return lease_mod.RunResult(0, json.dumps(out) + "\n", "")

    # -- the board ---------------------------------------------------------- #

    def _reboot(self):
        self.reboots += 1
        if self.no_op:
            return                              # REBOOT "acknowledged", nothing happened
        self.shell.stop()
        spec = self.boots.pop(0) if self.boots else None
        self.pending = (self.clock.now(), spec)
        if self.realtime and spec is not None:
            threading.Timer(spec["after"], self._come_up, args=(spec,)).start()

    def tick(self):
        if self.pending and not self.realtime:
            t, spec = self.pending
            if spec is not None and self.clock.now() - t >= spec["after"]:
                self._come_up(spec)

    def _come_up(self, spec):
        self.pending = None
        self.shell.mode = spec.get("mode", "run")
        self.shell.rescue_reason = spec.get("reason", "no card")
        self.shell._simulate_restart()                # a fresh kernel: os_up_ms restarts
        # ...and harnessd came up this long after it (FakeShell's default is
        # 18 s; a real-time test boots in well under that)
        self.shell.os_boot_ms = spec.get("os_boot_ms", 18000)
        self.shell.start()
        if self.shell.mode == "run":
            self._stage0(**spec.get("stage0", {}))

    def _stage0(self, boot_count=1, booted_from=1, default_slot=1,
                confirmed=True, n_fallback=0):
        """Place the block by NAME through the layout pyverify decodes with."""
        offs = dict(STAGE0_STATUS_FIELDS)
        vals = {"magic": STAGE0_STATUS_MAGIC, "version": STAGE0_STATUS_VERSION,
                "size": STAGE0_STATUS_BYTES, "magic_end": STAGE0_STATUS_MAGIC,
                "boot_count": boot_count, "phase": 6, "booted_from": booted_from,
                "default_slot": default_slot, "n_fallback": n_fallback,
                "att_confirm": STAGE0_CONFIRM_MAGIC if confirmed else 0}
        words = [0] * (STAGE0_STATUS_BYTES // 4)
        for name, v in vals.items():
            words[offs[name] // 4] = v
        self.mem.poke(STAGE0_STATUS_ADDR, words)


def _free_low_ports(count: int) -> "list[int]":
    """``count`` ports, each free for TCP and UDP right now, from BELOW the
    kernel's ephemeral range (so no outgoing connection is ever given one)."""
    import random
    import socket

    try:
        lo = int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        lo = 32768
    rng = random.Random()
    found: "list[int]" = []
    while len(found) < count:
        port = rng.randrange(20000, max(20001, lo - 1))
        if port in found:
            continue
        try:
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                with socket.socket(socket.AF_INET, kind) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("127.0.0.1", port))
        except OSError:
            continue
        found.append(port)
    return found


class _TickingProbe:
    """ShellProbe with the board's clock checked first; forwards ``linux``."""

    def __init__(self, board):
        self.board = board

    @property
    def linux(self):
        return self.board.sp.linux

    @linux.setter
    def linux(self, value):
        self.board.sp.linux = value

    def __call__(self):
        self.board.tick()
        return self.board.sp()


@pytest.fixture
def boards():
    made = []

    def make(*a, **kw):
        b = SimBoard(*a, **kw)
        made.append(b)
        return b
    yield make
    for b in made:
        b.close()


def _linux_runner(board, clock, *, route="tty", stage0=True, **kw):
    return br.BootRateRunner(
        probe=board.probe,
        resetter=br.MccRebootResetter(board, target="mps3_pl", route=route),
        lease=lease_mod.LeaseClient(board, chassis="mps3_01", target="mps3_pl"),
        holder="claude-bootrate", runner=board, host="127.0.0.1",
        stage0=(lambda: read_stage0_status(board.mem)) if stage0 else None,
        stage0_via="fake devmem" if stage0 else None,
        sleep=clock.sleep, now=clock.now, wall=clock.wall, **kw)


def test_linux_success_run_counts_each_boot_from_its_witnesses(boards, tmp_path) -> None:
    clock = FakeClock()
    board = boards(clock, [{"after": 95.0}, {"after": 102.0}, {"after": 99.0}])
    rec = _linux_runner(board, clock).run(n=3, method="mcc", poll_s=10.0, gap_s=0.0)
    camp = rec["campaign"]
    assert camp["linux"] is True and camp["linux_source"] == "version.impl=linux"
    assert camp["deadline_s"] == br.LINUX_DEADLINE_S == 180.0
    assert camp["deadline_source"].startswith("default for the Linux harness")
    assert [it["outcome"] for it in rec["iterations"]] == [br.OUTCOME_UP] * 3
    for it, after in zip(rec["iterations"], (95.0, 102.0, 99.0)):
        assert it["reason"] is None
        c = it["checks"]
        assert (c["fresh"], c["mode"], c["slot"], c["confirmed"], c["boot_count"]) \
            == (True, "run", "A", True, 1)
        assert c["os_up_ms"] < 60_000, "a fresh kernel, not the hour-old one"
        assert it["time_to_ping_s"] == pytest.approx(after, abs=10.0)
        assert it["reset"]["route"] == "tty" and it["reset"]["sent"] is True
    assert board.reboots == 3
    assert all(c["tty"] == TTY00 and c["pace"] == 0.1 and c["settle"] == 1.0
               for c in board.mcc_calls), "W1's recipe: tty_00, 100 ms/char, 1 s"
    s = rec["summary"]
    assert (s["up"], s["valid"], s["rate"], s["stage0_unchecked"]) == (3, 3, 1.0, 0)
    br.validate_record(rec)

    # -- the evidence: the write-up's names, both intervals, never overwritten
    csv_path, md_path = br.write_evidence(rec, tmp_path)
    assert csv_path.name == br.evidence_stem(rec) + ".csv"
    assert re.fullmatch(r"boot_rate_\d{8}\.csv", csv_path.name)
    rows = list(__import__("csv").DictReader(csv_path.open()))
    assert [r["verdict"] for r in rows] == ["PASS"] * 3
    assert rows[0]["method"] == "mcc" and rows[0]["route"] == "tty"
    assert rows[0]["slot"] == "A" and rows[0]["confirmed"] == "yes"
    assert float(rows[1]["t_reboot_to_6900_s"]) == pytest.approx(102.0, abs=10.0)
    md = md_path.read_text()
    assert "**3 of 3**" in md and "Clopper–Pearson" in md and "Wilson" in md
    assert "[29.2%, 100.0%]" in md, "CP 3/3: 0.025**(1/3)"
    assert "## README row" in md and csv_path.name in md
    again = br.write_evidence(rec, tmp_path)
    assert again[0].name.endswith("_2.csv"), "a second campaign must not overwrite"


def test_linux_a_boot_that_never_returns_is_dark_at_the_180s_deadline(boards) -> None:
    clock = FakeClock()
    board = boards(clock, [None])
    t0 = _time.monotonic()
    rec = _linux_runner(board, clock).run(n=1, method="mcc", poll_s=15.0)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_DARK
    assert "180s deadline" in it["reason"] and "identify" in it["reason"]
    assert it["waited_s"] == pytest.approx(180.0, abs=1.0)
    assert rec["summary"]["rate"] == 0.0
    assert _time.monotonic() - t0 < 20.0, "the campaign slept for real"


def test_linux_a_rescue_mode_boot_is_a_FAIL_with_its_reason(boards) -> None:
    """Stage0's rescue server answers identify (mode "rescue") and nothing on
    6900. It is a boot that did NOT come up as Linux: in the denominator, as a
    FAIL, carrying stage0's reason -- not 'dark', not excluded."""
    clock = FakeClock()
    board = boards(clock, [
        {"after": 60.0, "mode": "rescue", "reason": "slots exhausted (unconfirmed boots)"},
        {"after": 100.0}])
    rec = _linux_runner(board, clock).run(n=2, method="mcc", poll_s=10.0, gap_s=0.0)
    first, second = rec["iterations"]
    assert first["outcome"] == br.OUTCOME_FAIL
    assert "rescue" in first["reason"] and "slots exhausted" in first["reason"]
    assert second["outcome"] == br.OUTCOME_UP, "a rescue board still takes the next REBOOT"
    s = rec["summary"]
    assert (s["fail"], s["up"], s["valid"], s["rate"]) == (1, 1, 2, 0.5)
    row = br.evidence_csv(rec).splitlines()[1]
    assert ",FAIL," in row and "slots exhausted" in row


def test_linux_wrong_slot_unconfirmed_and_retried_boots_are_FAILs(boards) -> None:
    clock = FakeClock()
    board = boards(clock, [
        {"after": 90.0, "stage0": {"booted_from": 2, "n_fallback": 1}},
        {"after": 90.0, "stage0": {"confirmed": False}},
        {"after": 90.0, "stage0": {"boot_count": 3}}])
    rec = _linux_runner(board, clock).run(n=3, method="mcc", poll_s=10.0, gap_s=0.0)
    reasons = [it["reason"] for it in rec["iterations"]]
    assert [it["outcome"] for it in rec["iterations"]] == [br.OUTCOME_FAIL] * 3
    assert "slot B, expected A" in reasons[0] and "n_fallback=1" in reasons[0]
    assert "never confirmed" in reasons[1]
    assert "boot_count=3" in reasons[2]


def test_linux_a_kernel_older_than_the_reset_is_not_a_boot(boards) -> None:
    """The REBOOT is acknowledged and nothing reboots. With the down edge
    waived, the only witness left is the kernel's age -- and it is an hour."""
    clock = FakeClock()
    board = boards(clock, [], no_op=True)
    rec = _linux_runner(board, clock).run(n=1, method="mcc", await_down=False)
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_HUB_ERROR
    assert "never restarted" in it["reason"]
    assert rec["summary"]["rate"] is None


def test_a_second_reader_on_the_mcc_tty_refuses_and_stops_the_campaign(boards) -> None:
    """W1's first REBOOT was a no-op because a leftover `cat` of tty_00 ate
    the echo. The writer refuses, sends NOTHING, and the campaign stops."""
    clock = FakeClock()
    board = boards(clock, [{"after": 90.0}] * 5,
                   second_reader=[4242, "cat %s" % TTY00])
    rec = _linux_runner(board, clock).run(n=5, method="mcc", gap_s=0.0)
    assert len(rec["iterations"]) == 1
    it = rec["iterations"][0]
    assert it["outcome"] == br.OUTCOME_HUB_ERROR
    assert "another process reads" in it["reason"] and "4242" in it["reason"]
    assert "NOTHING was sent" in it["reason"]
    assert board.reboots == 0
    assert rec["campaign"]["aborted"].startswith("stopped after iteration 0 of 5")


def test_auto_route_keeps_fpgahub_on_tty_00_and_falls_back_from_tty_01(boards) -> None:
    clock = FakeClock()
    good = boards(clock, [{"after": 90.0}], fpgahub_tty=TTY00)
    rec = _linux_runner(good, clock, route="auto").run(n=1, method="mcc", poll_s=10.0)
    assert rec["iterations"][0]["reset"]["route"] == "fpgahub"
    assert rec["summary"]["up"] == 1 and good.fpgahub_resets == 1

    clock = FakeClock()
    wrong = boards(clock, [{"after": 90.0}] * 2, fpgahub_tty="/dev/mps3_pl/tty_01")
    rec = _linux_runner(wrong, clock, route="auto").run(n=2, method="mcc", poll_s=10.0, gap_s=0.0)
    assert [it["reset"]["route"] for it in rec["iterations"]] == ["tty"] * 2
    assert rec["summary"]["up"] == 2
    assert wrong.fpgahub_resets == 1, "once fpgahub proved it writes to tty_01, stop asking"
    assert "tty_01" in rec["iterations"][0]["reset"]["detail"]


def test_the_hub_argv_survives_both_ssh_runner_dialects() -> None:
    """The ssh runner joined argv unquoted before ccc2fde and quotes it (under
    `sg fpga -c`) after: the script must arrive as ONE argument either way."""
    from pyverify.lease import SshHubRunner

    runner = SshHubRunner("hub")
    argv = br.MccTtyResetter(runner, tty_path=TTY00).argv()
    remote = shlex.split(runner.build(argv)[-1])
    if remote[0] == "sg":                       # the quoting runner: two shells
        remote = shlex.split(remote[-1])
    tail = remote[remote.index("python3"):]
    assert tail[1] == "-c" and tail[2] == br.HUB_MCC_REBOOT_PY
    assert json.loads(tail[3])["tty"] == TTY00


def test_the_intervals_clopper_pearson_as_the_writeup_quotes_it_and_wilson() -> None:
    """The write-up quotes Clopper-Pearson: 10/10 -> [69.2%, 100%]. Exact
    values against the standard tables (no SciPy)."""
    cp = br.clopper_pearson
    assert cp(10, 10) == (pytest.approx(0.6915, abs=1e-4), 1.0)
    assert cp(9, 10) == (pytest.approx(0.5550, abs=1e-4), pytest.approx(0.9975, abs=1e-4))
    assert cp(7, 10) == (pytest.approx(0.3475, abs=1e-4), pytest.approx(0.9333, abs=1e-4))
    assert cp(0, 10) == (0.0, pytest.approx(0.3085, abs=1e-4))
    assert cp(0, 0) == (0.0, 1.0)
    # Wilson is narrower at the edge: 10/10 -> 0.7225 = n / (n + z^2).
    assert br.wilson(10, 10)[0] == pytest.approx(10 / (10 + 1.96 ** 2))
    s = br.summarise([{"outcome": br.OUTCOME_UP, "time_to_ping_s": 100.0}] * 10)
    assert s["ci95_clopper_pearson"][0] == pytest.approx(0.6915, abs=1e-4)
    assert s["ci95"][0] == pytest.approx(0.7225, abs=1e-4)
    md = br.evidence_md({"schema": br.SCHEMA_ID,
                         "campaign": {"linux": True, "started_at": "2026-09-24T09:00:00Z"},
                         "iterations": [], "summary": s})
    assert "**10 of 10**" in md and "[69.2%, 100.0%]" in md and "[72.2%, 100.0%]" in md


# --------------------------------------------------------------------------- #
# the hub-side MCC writer, for real, against a pseudo-terminal
# --------------------------------------------------------------------------- #


class PtyMcc:
    """A fake MCC console on a pty: the slave is the 'tty_00' the writer opens;
    this thread plays the MCC on the master -- a bare CR gets ``Cmd>``,
    REBOOT gets ``Rebooting...``. Every byte is timestamped."""

    def __init__(self, *, prompt=b"\r\n\r\nCmd> "):
        self.master, self._slave = os.openpty()      # slave kept open: no EIO
        self.path = os.ttyname(self._slave)
        self.prompt = prompt
        self.rx = []
        self._line = b""
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        import select
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.master, 1024)
            except OSError:
                continue
            for b in data:
                self.rx.append((_time.monotonic(), bytes([b])))
                if b == 13:
                    if self._line == b"" and self.prompt:
                        os.write(self.master, self.prompt)
                    elif self._line == b"REBOOT":
                        os.write(self.master, b"REBOOT\r\nRebooting...\r\n")
                    self._line = b""
                else:
                    self._line += bytes([b])

    def sent(self) -> bytes:
        return b"".join(b for _, b in self.rx)

    def close(self):
        self._stop.set()
        self._t.join(1.0)
        os.close(self.master)
        os.close(self._slave)


def _local(argv, timeout=None):
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    return lease_mod.RunResult(proc.returncode, proc.stdout, proc.stderr)


_needs_pty = pytest.mark.skipif(not hasattr(os, "openpty") or not os.path.isdir("/proc")
                                or shutil.which("cat") is None,
                                reason="needs a pty, /proc and cat")


@pytest.fixture
def mcc_pty():
    made = []

    def make(**kw):
        m = PtyMcc(**kw)
        made.append(m)
        return m
    yield make
    for m in made:
        m.close()


def _writer(path):
    return br.MccTtyResetter(_local, tty_path=path, pace_s=0.05, settle_s=0.1,
                             ack_s=2.0, prompt_s=1.0, python=sys.executable)


@_needs_pty
def test_the_hub_writer_sends_a_bare_CR_then_a_paced_REBOOT(mcc_pty) -> None:
    mcc = mcc_pty()
    out = _writer(mcc.path)()
    assert out.ok and out.sent is True and "Rebooting" in out.detail
    assert mcc.sent() == b"\rREBOOT\r", "a bare CR first, then REBOOT and CR"
    # Receive-side timestamps can only be LATE (a loaded host delays the fake
    # MCC's reads), so judge spans, not single gaps: a burst would put all
    # seven bytes inside a few ms.
    ts = [t for t, _ in mcc.rx]
    assert ts[1] - ts[0] >= 0.05, "the settle after the bare CR"
    assert ts[-1] - ts[1] >= 6 * 0.05 * 0.7, "REBOOT + CR paced, not a burst: %r" % ts


@_needs_pty
def test_the_hub_writer_refuses_a_second_reader_and_sends_nothing(mcc_pty) -> None:
    mcc = mcc_pty()
    cat = subprocess.Popen(["cat", mcc.path], stdout=subprocess.DEVNULL)
    try:
        _time.sleep(0.2)
        out = _writer(mcc.path)()
    finally:
        cat.kill()
        cat.wait()
    assert not out.ok and out.fatal and out.sent is False
    assert "pid %d" % cat.pid in out.detail and "NOTHING was sent" in out.detail
    assert mcc.sent() == b"", "not even the bare CR"


@_needs_pty
def test_the_hub_writer_refuses_when_the_prompt_is_not_intact(mcc_pty) -> None:
    """A reader this account cannot see (a root cat, an fpgahub share) shows
    only as the MCC's prompt going missing."""
    mcc = mcc_pty(prompt=b"d> ")
    out = _writer(mcc.path)()
    assert not out.ok and out.fatal and out.sent is False
    assert "Cmd>" in out.detail and "fpgahub" in out.detail
    assert mcc.sent() == b"\r", "the bare CR only; REBOOT never went out"


# --------------------------------------------------------------------------- #
# the CLI, end to end on a real clock
# --------------------------------------------------------------------------- #


def test_cli_linux_mcc_campaign_writes_the_record_and_the_evidence(boards, tmp_path, capsys) -> None:
    board = boards(RealClock, [{"after": 0.3, "os_boot_ms": 0}], realtime=True)
    ev = tmp_path / "docs" / "evidence" / "2026-09-linux-soak"
    rc = cli_mod.main(
        ["boot-rate", "--host", "127.0.0.1", "--control-port", str(board.shell.control_port),
         "--identify-port", str(board.shell.identify_port), "--holder", "claude-bootrate",
         "--method", "mcc", "--mcc-route", "tty", "--n", "1", "--poll", "0.2",
         "--gap", "0", "--down-deadline", "5", "--stage0-ssh", "none",
         "--evidence-dir", str(ev), "--out", str(tmp_path / "run.json")],
        runner=board)
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    summary = json.loads(cap.out)
    assert summary["up"] == 1 and summary["linux"] is True and summary["deadline_s"] == 180.0
    assert summary["stage0_unchecked"] == 1, "--stage0-ssh none must SAY slot/confirm went unchecked"
    names = sorted(p.name for p in ev.iterdir())
    assert len(names) == 2 and names[0].startswith("boot_rate_") \
        and {n.rsplit(".", 1)[1] for n in names} == {"csv", "md"}
    br.validate_record(json.loads((tmp_path / "run.json").read_text()))


def test_cli_second_reader_exits_1_and_still_writes_the_record(boards, tmp_path, capsys) -> None:
    board = boards(RealClock, [], second_reader=[4242, "cat %s" % TTY00])
    rc = cli_mod.main(
        ["boot-rate", "--host", "127.0.0.1", "--control-port", str(board.shell.control_port),
         "--identify-port", str(board.shell.identify_port), "--holder", "claude-bootrate",
         "--method", "mcc", "--mcc-route", "tty", "--n", "3", "--stage0-ssh", "none",
         "--evidence-dir", str(tmp_path)], runner=board)
    err = capsys.readouterr().err
    assert rc == 1 and "another process reads" in err and "Traceback" not in err
    md = next(tmp_path.glob("boot_rate_*.md")).read_text()
    assert "Stopped early" in md and board.reboots == 0
