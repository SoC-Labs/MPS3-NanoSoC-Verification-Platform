"""``pyverify sd field`` / :mod:`pyverify.fielding`, and the paced MCC route as
the default everywhere (ILA handoff 2026-09-24 defect 6).

The rules under test, each learned by a failure on the board:
  * ONE SD write, never retried: the client ALWAYS times out while the hub keeps
    writing, and a second write mid-install darkened the board (2026-07-18);
  * no REBOOT before fpgahubd's ``program dispatched ... ok=True sha256=<prefix>``:
    a REBOOT sent while sd_install was writing was echoed and ignored (09-24);
  * exactly ONE reader on tty_00, a bare CR, R-E-B-O-O-T at 100 ms/char: a
    leftover ``cat`` made W1's first REBOOT a no-op (09-24);
  * fpgahub's own ``reset --method mcc`` is not the default route: the handoff
    found it sends a burst the MCC drops.

The hub is :class:`SimHub`: fpgahub (lease verbs from :class:`FakeHub`), the
fpgahubd journal on a simulated clock, ``date``/``sha256sum``/``cat``, and the
hub-side MCC writer's JSON verdict. The writer SCRIPT runs for real against a
pseudo-terminal at the end of the file.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import select
import subprocess
import sys
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import pytest

from fakehub import FakeHub
from pyverify import bootrate as br
from pyverify import cli as cli_mod
from pyverify import fielding as fl
from pyverify import lease as lease_mod
from pyverify.lease import RunResult

TARGET = "mps3_pl"
TTY00 = "/dev/mps3_pl/tty_00"
HOLDER = "b2-linux"
BST = timezone(timedelta(hours=1))
#: W1's SD write started at 08:35:45 BST on 2026-09-24.
BASE = datetime(2026, 9, 24, 8, 35, 45, tzinfo=BST).timestamp()


class Clock:
    """Monotonic ``now`` and the hub's wall clock, advanced only by ``sleep``."""

    def __init__(self, base: float = BASE) -> None:
        self.t = 0.0
        self.base = base

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.base + self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)


class RealClock:
    now = staticmethod(_time.monotonic)
    wall = staticmethod(_time.time)
    sleep = staticmethod(_time.sleep)


def iso(epoch: float, text: str) -> str:
    stamp = datetime.fromtimestamp(epoch, tz=BST).strftime("%Y-%m-%dT%H:%M:%S%z")
    return "%s hub fpgahubd[4242]: %s" % (stamp, text)


def short(epoch: float, text: str) -> str:
    """journalctl's default format: no year, no zone -- never trusted as a time."""
    stamp = datetime.fromtimestamp(epoch, tz=BST).strftime("%b %d %H:%M:%S")
    return "%s hub fpgahubd[4242]: %s" % (stamp, text)


def dispatched(sha: str, ok: bool = True, dur: float = 68.44, board: str = TARGET) -> str:
    return ("INFO fpgahub.program: program dispatched: board=%s method=sd plugin=sd_install "
            "ok=%s part=xcku115-flvb1760-1-c sha256=%s dur=%.2fs" % (board, ok, sha[:12], dur))


def writing(bit: str, board: str = TARGET) -> str:
    return ("INFO fpgahub.program_plugins.sd_install: sd_install: writing %s -> "
            "/run/fpgahub/sd/%s/MB/HBI0309C/Nanosoc/nanosoc.bit" % (bit, board))


class SimHub:
    """The HubRunner seam for a fielding run (see the module docstring)."""

    def __init__(self, clock, *, holder=HOLDER, dispatch_ok=True, dispatch_sha=None,
                 dispatch_after=68.0, no_dispatch=False, client="rc1-timeout",
                 journal_denied=False, second_reader=False, reader_after_scan=False,
                 ack=True, complete=True, capture_file=None, capture_short=True,
                 client_wait=30.0, boot_wait=40.0):
        self.clock = clock
        self.hub = FakeHub()
        if holder:
            lease_mod.LeaseClient(self.hub, chassis=self.hub.chassis, target=TARGET).acquire(
                holder, poll_s=0, sleep=lambda _s: None)
        self.dispatch_ok = dispatch_ok
        self.dispatch_sha = dispatch_sha
        self.dispatch_after = dispatch_after
        self.no_dispatch = no_dispatch
        self.client = client
        self.journal_denied = journal_denied
        self.second_reader = second_reader
        self.reader_after_scan = reader_after_scan
        self.ack = ack
        self.complete = complete
        self.capture_file = capture_file
        self.capture_short = capture_short
        #: the fpgahub client gives up after ~30 s; the MCC boot log takes ~40 s
        self.client_wait = client_wait
        self.boot_wait = boot_wait
        #: (epoch, text) -- fpgahubd has been running for an hour
        self.journal = [(clock.wall() - 3600, "INFO fpgahub.daemon: fpgahubd listening on "
                                               "/run/fpgahub/fpgahub.sock")]
        self.calls = []
        self.program_calls = []
        self.fpgahub_resets = []
        self.mcc_calls = []
        self.reboot_at = None
        self.dispatch_at = None

    def add(self, epoch: float, text: str) -> None:
        self.journal.append((epoch, text))
        self.journal.sort(key=lambda e: e[0])

    def visible(self):
        now = self.clock.wall()
        return [(e, t) for e, t in self.journal if e <= now]

    # -- the seam ----------------------------------------------------------- #

    def __call__(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:1] == ["date"]:
            return RunResult(0, "%d\n" % int(self.clock.wall()), "")
        if argv[:1] == ["sha256sum"]:
            path = argv[-1]
            if not os.path.isfile(path):
                return RunResult(1, "", "sha256sum: %s: No such file or directory\n" % path)
            with open(path, "rb") as fh:
                return RunResult(0, "%s  %s\n" % (hashlib.sha256(fh.read()).hexdigest(), path), "")
        if argv[:1] == ["journalctl"] or argv[:3] == ["sudo", "-n", "journalctl"]:
            return self._journalctl(argv)
        if argv[:1] == ["cat"]:
            if argv[1] != self.capture_file:
                return RunResult(1, "", "cat: %s: No such file or directory\n" % argv[1])
            fmt = short if self.capture_short else iso
            return RunResult(0, "".join(fmt(e, t) + "\n" for e, t in self.visible()), "")
        if argv[:1] in (["python3"], [sys.executable]):
            return self._mcc(json.loads(argv[-1]))
        if argv[:3] == ["fpgahub", "target", "program"]:
            return self._program(argv)
        if argv[:3] == ["fpgahub", "target", "reset"]:
            self.fpgahub_resets.append(argv)
            return RunResult(0, "ok %s reset method=mcc plugin=mps3_mcc_reboot\n  REBOOT "
                                "sent directly on %s\n" % (TARGET, TTY00), "")
        return self.hub(argv, timeout)

    def _journalctl(self, argv):
        argv = argv[argv.index("journalctl"):]          # drop a `sudo -n` prefix
        if self.journal_denied:
            return RunResult(0, "-- No entries --\n",
                             "Hint: You are currently not seeing messages from other users "
                             "and the system.\n      Users in groups 'adm', "
                             "'systemd-journal', 'wheel' can see all messages.\n")
        since = None
        if "--since" in argv:
            since = float(argv[argv.index("--since") + 1].lstrip("@"))
        rows = [(e, t) for e, t in self.visible() if since is None or e >= since]
        if "-n" in argv:
            rows = rows[-int(argv[argv.index("-n") + 1]):]
        assert "-o" in argv and argv[argv.index("-o") + 1] == "short-iso"
        text = "".join(iso(e, t) + "\n" for e, t in rows) or "-- No entries --\n"
        return RunResult(0, text, "")

    def _program(self, argv):
        self.program_calls.append(argv)
        t = self.clock.wall()
        bit = argv[4]
        with open(bit, "rb") as fh:
            sha = self.dispatch_sha or hashlib.sha256(fh.read()).hexdigest()
        self.add(t + 4.0, writing(bit))
        if not self.no_dispatch:
            self.dispatch_at = t + self.dispatch_after
            self.add(self.dispatch_at, dispatched(sha, ok=self.dispatch_ok,
                                                  dur=self.dispatch_after - 4.0))
        self.clock.sleep(self.client_wait)      # the fpgahub client gives up at ~30 s
        if self.client == "raise":
            raise TimeoutError("hub command timed out after 120s")
        if self.client == "refused":
            return RunResult(1, "", "HTTP 400: sd_install: no USB mass-storage device "
                                    "c251:4003 found under 1-2.3.4.3.4\n")
        return RunResult(1, "", "POST /targets/%s/program: timed out\n" % TARGET)

    def _mcc(self, args):
        self.mcc_calls.append((self.clock.wall(), args))
        out = {"tty": args["tty"], "mode": args["mode"], "sent": False, "ack": False,
               "others": [], "prompt": "\r\n\r\nCmd> ", "echo": ""}
        if self.second_reader or (self.reader_after_scan and args["mode"] != "scan"):
            out.update(others=[[577303, "cat %s" % args["tty"]]], rc=3,
                       reason="another process reads %s" % args["tty"])
            return RunResult(3, json.dumps(out) + "\n", "")
        if args["mode"] == "scan":
            out["rc"] = 0
            return RunResult(0, json.dumps(out) + "\n", "")
        self.reboot_at = self.clock.wall()
        out["sent"] = True
        if not self.ack:
            out.update(rc=5, reason="no Rebooting echo", echo="REBOOT\r\n")
            return RunResult(5, json.dumps(out) + "\n", "")
        out.update(ack=True, echo="REBOOT\r\nRebooting...\r\n", rc=0)
        if args.get("capture_s"):
            tail = ("Configuring FPGA from file \\MB\\HBI0309C\\Nanosoc\\nanosoc.bit\r\n"
                    + ("FPGA configuration complete.\r\nCmd> " if self.complete else ""))
            out.update(configuring=True, complete=self.complete, failed=False,
                       log=args.get("log"), tail=tail)
            self.clock.sleep(self.boot_wait)
        return RunResult(0, json.dumps(out) + "\n", "")


@pytest.fixture
def bit(tmp_path):
    p = tmp_path / "config_rm_greybox_stage0.bit"
    p.write_bytes(b"\x00\x09\x0f\xf0" + b"MPS3 stage0 static" * 4096)
    return str(p)


def sha_of(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def fielder(hub, clock, *, journal=None, mcc=True, capture=150.0, **kw):
    kw.setdefault("witness_deadline_s", 300.0)
    kw.setdefault("poll_s", 10.0)
    return fl.SdFielder(
        hub, target=TARGET,
        lease=lease_mod.LeaseClient(hub, chassis="mps3_01", target=TARGET),
        holder=HOLDER, journal=journal or fl.CommandJournal(hub),
        mcc=(br.MccTtyResetter(hub, tty_path=TTY00, capture_s=capture,
                               log_path="/home/hubuser/pv_b2/mcc.log") if mcc else None),
        now=clock.now, sleep=clock.sleep, **kw)


def reboots(hub):
    return [a for _t, a in hub.mcc_calls if a["mode"] == "reboot"]


# --------------------------------------------------------------------------- #
# the sequence
# --------------------------------------------------------------------------- #


def test_one_write_then_the_ok_true_witness_then_the_paced_reboot(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_OK and res.ok, res.reason
    assert hub.program_calls == [["fpgahub", "target", "program", TARGET, bit, "--method",
                                  "sd", "--yes", "--no-skip-if-loaded"]]
    assert hub.reboot_at is not None and hub.reboot_at >= hub.dispatch_at, \
        "the REBOOT went out before the daemon's `program dispatched ... ok=True`"
    assert hub.fpgahub_resets == [], "fpgahub's burst REBOOT was used"
    (reboot,) = reboots(hub)
    assert (reboot["tty"], reboot["pace"], reboot["settle"]) == (TTY00, 0.1, 1.0)
    assert reboot["capture_s"] == 150.0 and reboot["log"] == "/home/hubuser/pv_b2/mcc.log"
    rec = res.record
    assert rec["sha256"] == sha_of(bit) and rec["witness"]["sha12"] == sha_of(bit)[:12]
    assert rec["witness"]["status"] == "ok" and rec["witness"]["dur_s"] == 64.0
    assert rec["write"]["outcome"] == "client-timeout"
    assert rec["reboot"]["complete"] is True and rec["fielded"] is False
    assert json.loads(json.dumps(rec)) == rec


def test_the_scan_for_a_second_reader_runs_before_the_write(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    fielder(hub, clock).field(bit)
    first_program = next(i for i, c in enumerate(hub.calls) if c[:3] == ["fpgahub", "target", "program"])
    scans = [i for i, c in enumerate(hub.calls)
             if c[:1] == ["python3"] and json.loads(c[-1])["mode"] == "scan"]
    assert scans and scans[0] < first_program


@pytest.mark.parametrize("client", ["rc1-timeout", "raise"])
def test_the_client_timeout_is_expected_and_the_write_is_never_retried(bit, client) -> None:
    clock = Clock()
    hub = SimHub(clock, client=client)
    f = fielder(hub, clock)
    res = f.field(bit)
    assert res.ok, res.reason
    assert len(hub.program_calls) == 1 and f.writes_issued == 1


def test_ok_false_refuses_the_reboot(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, dispatch_ok=False)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_NO_WITNESS and res.stage == "witness"
    assert res.record["witness"]["status"] == "failed" and "half-written" in res.reason
    assert reboots(hub) == [] and len(hub.program_calls) == 1


def test_another_sha256_refuses_the_reboot(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, dispatch_sha="bbcd7f45" + "0" * 56)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_NO_WITNESS
    assert res.record["witness"]["status"] == "mismatch" and "bbcd7f450000" in res.reason
    assert reboots(hub) == []


def test_no_witness_by_the_deadline_refuses_and_never_retries(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, no_dispatch=True)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_NO_WITNESS
    w = res.record["witness"]
    assert w["status"] == "timeout" and "may still be running" in w["detail"]
    assert w["waited_s"] == pytest.approx(300.0, abs=10.0)
    assert "do NOT retry" in res.record["next"] and "--already-written" in res.record["next"]
    assert len(hub.program_calls) == 1 and reboots(hub) == []


def test_a_late_witness_inside_the_deadline_is_waited_for(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, dispatch_after=250.0)
    res = fielder(hub, clock).field(bit)
    assert res.ok and hub.reboot_at >= hub.dispatch_at


def test_a_skipped_write_refuses_the_reboot(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, no_dispatch=True)
    orig = hub._program

    def program(argv):
        r = orig(argv)
        hub.add(clock.wall() + 1, "INFO fpgahub.program: program skipped: skip: "
                "bitstream_loaded already at sha256=%s… (use --force to re-flash) "
                "board=%s" % (sha_of(bit)[:12], TARGET))
        return r
    hub._program = program
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_NO_WITNESS and res.record["witness"]["status"] == "skipped"
    assert reboots(hub) == []


def test_a_second_writer_racing_ours_is_ambiguous(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, dispatch_after=68.0)
    orig = hub._program

    def program(argv):
        t = clock.wall()
        r = orig(argv)
        hub.add(t + 65.0, dispatched("3f1a560f" + "0" * 56))
        return r
    hub._program = program
    res = fielder(hub, clock, poll_s=100.0).field(bit)
    assert res.exit_code == fl.EXIT_NO_WITNESS and res.record["witness"]["status"] == "ambiguous"
    assert reboots(hub) == []


def test_a_refused_write_is_not_retried_and_says_nothing_was_written(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, client="refused", no_dispatch=True)
    orig = hub._program

    def program(argv):
        r = orig(argv)
        hub.journal = [j for j in hub.journal if "sd_install: writing" not in j[1]]
        return r
    hub._program = program
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_NO_WITNESS and res.stage == "write"
    assert "no USB mass-storage device" in res.reason and "nothing was written" in res.reason
    assert len(hub.program_calls) == 1 and reboots(hub) == []


# --------------------------------------------------------------------------- #
# the gates: refused before the card is touched
# --------------------------------------------------------------------------- #


def test_a_second_reader_on_tty_00_refuses_before_the_write(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, second_reader=True)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REFUSED and res.stage == "preflight"
    assert "577303" in res.reason and "Nothing was written" in res.reason
    assert hub.program_calls == [] and reboots(hub) == []


def test_a_write_in_flight_refuses_before_writing(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    hub.add(clock.wall() - 60, writing("/home/hubuser/other.bit"))
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REFUSED and "IN FLIGHT" in res.reason
    assert hub.program_calls == []


def test_a_finished_earlier_write_is_not_in_flight(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    hub.add(clock.wall() - 600, writing("/home/hubuser/old.bit"))
    hub.add(clock.wall() - 530, dispatched("68f70da13dbe" + "0" * 52))
    assert fielder(hub, clock).field(bit).ok


def test_an_unreadable_journal_refuses_before_writing_and_names_the_ways_out(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, journal_denied=True)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REFUSED and hub.program_calls == []
    assert "not seeing messages from other users" in res.reason
    nxt = res.record["next"]
    assert "systemd-journal" in nxt and "--journal-cmd" in nxt and "--journal-file" in nxt


def test_the_journal_command_is_configurable_for_sudo(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    j = fl.CommandJournal(hub, cmd="sudo -n journalctl")
    assert fielder(hub, clock, journal=j).field(bit).ok
    assert any(c[:3] == ["sudo", "-n", "journalctl"] for c in hub.calls)


def test_a_lease_held_by_someone_else_refuses_before_writing(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, holder="someone-else")
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REFUSED and "someone-else" in res.reason
    assert hub.program_calls == []


def test_a_0_3_0_principal_holder_is_named_in_the_refusal(bit) -> None:
    """fpgahub 0.3.0 records user@host whatever --holder the acquire passed; the
    gate still refuses (no guessing), but the next step names the holder to pass."""
    clock = Clock()
    hub = SimHub(clock, holder="operator@hub")
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REFUSED and hub.program_calls == []
    assert "--holder operator@hub" in res.record["next"]


def test_an_unexpected_sha256_refuses_before_writing(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    res = fielder(hub, clock).field(bit, expect_sha256="deadbeefdeadbeef")
    assert res.exit_code == fl.EXIT_REFUSED and "wrong file" in res.reason
    assert hub.program_calls == []
    assert fielder(SimHub(clock), clock).field(bit, expect_sha256=sha_of(bit)[:16]).ok


def test_an_unreadable_bit_refuses_before_writing(tmp_path) -> None:
    clock = Clock()
    hub = SimHub(clock)
    res = fielder(hub, clock).field(str(tmp_path / "missing.bit"))
    assert res.exit_code == fl.EXIT_REFUSED and hub.program_calls == []


# --------------------------------------------------------------------------- #
# the REBOOT after the witness
# --------------------------------------------------------------------------- #


def test_a_reader_that_appears_after_the_write_blocks_the_reboot_exit_5(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, reader_after_scan=True)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REBOOT and res.stage == "reboot"
    assert res.record["witness"]["status"] == "ok" and res.record["reboot"]["sent"] is False
    assert "--already-written" in res.record["next"]
    assert len(hub.program_calls) == 1


def test_an_unacknowledged_reboot_is_exit_5(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, ack=False)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REBOOT and "not acknowledged" in res.reason


def test_no_fpga_configuration_complete_is_exit_5_and_points_at_rollback(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, complete=False)
    res = fielder(hub, clock).field(bit)
    assert res.exit_code == fl.EXIT_REBOOT and "FPGA configuration" in res.reason
    assert "ROLLBACK" in res.record["next"]


def test_no_reboot_writes_and_witnesses_only(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    res = fielder(hub, clock, mcc=False).field(bit, reboot=False)
    assert res.ok and hub.mcc_calls == [] and len(hub.program_calls) == 1


# --------------------------------------------------------------------------- #
# --already-written: the recovery path
# --------------------------------------------------------------------------- #


def test_already_written_finds_the_witness_and_reboots_without_writing(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    hub.add(clock.wall() - 400, writing(bit))
    hub.add(clock.wall() - 332, dispatched(sha_of(bit)))
    res = fielder(hub, clock).field(bit, already_written=True)
    assert res.ok, res.reason
    assert hub.program_calls == [] and len(reboots(hub)) == 1


def test_already_written_refuses_a_superseded_witness(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    hub.add(clock.wall() - 400, dispatched(sha_of(bit)))
    hub.add(clock.wall() - 100, dispatched("2a457f7e" + "0" * 56))
    res = fielder(hub, clock).field(bit, already_written=True)
    assert res.exit_code == fl.EXIT_NO_WITNESS and res.record["witness"]["status"] == "mismatch"
    assert reboots(hub) == []


def test_already_written_ignores_a_witness_older_than_the_window(bit) -> None:
    clock = Clock()
    hub = SimHub(clock)
    hub.add(clock.wall() - 3000, dispatched(sha_of(bit)))
    res = fielder(hub, clock, witness_deadline_s=30.0).field(bit, already_written=True,
                                                             since_s=1200.0)
    assert res.exit_code == fl.EXIT_NO_WITNESS and res.record["witness"]["status"] == "timeout"


# --------------------------------------------------------------------------- #
# journal sources that need no journal read access of their own
# --------------------------------------------------------------------------- #


def test_a_capture_file_ignores_its_history(bit) -> None:
    """An ok=True for the SAME bit already in the capture file (an earlier
    write) must not stand in for this write's failed one."""
    clock = Clock()
    path = "/home/hubuser/fpgahubd_capture.log"
    hub = SimHub(clock, dispatch_ok=False, capture_file=path, capture_short=True)
    hub.add(clock.wall() - 900, writing(bit))
    hub.add(clock.wall() - 832, dispatched(sha_of(bit), ok=True))
    res = fielder(hub, clock, journal=fl.FileJournal(hub, path)).field(bit)
    assert res.exit_code == fl.EXIT_NO_WITNESS and res.record["witness"]["status"] == "failed"
    assert not any(c[:1] == ["journalctl"] for c in hub.calls)


def test_a_capture_file_witness_is_taken(bit) -> None:
    clock = Clock()
    path = "/home/hubuser/fpgahubd_capture.log"
    hub = SimHub(clock, capture_file=path)
    assert fielder(hub, clock, journal=fl.FileJournal(hub, path)).field(bit).ok


def test_a_missing_capture_file_refuses_before_writing(bit) -> None:
    clock = Clock()
    hub = SimHub(clock, capture_file="/elsewhere")
    res = fielder(hub, clock, journal=fl.FileJournal(hub, "/home/hubuser/none.log")).field(bit)
    assert res.exit_code == fl.EXIT_REFUSED and hub.program_calls == []


def test_stdin_takes_only_exactly_timestamped_lines_after_the_start() -> None:
    sha = "6a7a2755886a" + "0" * 52
    t0 = BASE
    stream = io.StringIO(
        iso(t0 - 600, dispatched(sha)) + "\n"            # history, timestamped: dropped
        + short(t0 + 60, dispatched(sha)) + "\n"         # no exact time: dropped
        + "%.6f hub fpgahubd[1]: %s\n" % (t0 + 68, dispatched(sha, ok=False)))
    j = fl.StreamJournal(stream)
    for _ in range(100):
        if j.eof:
            break
        _time.sleep(0.01)
    lines = j.lines_since(t0)
    assert len(lines) == 1 and "ok=False" in lines[0] and j.untimed == 1
    assert fl.judge(fl.parse_events(lines, TARGET), sha).status == "failed"


# --------------------------------------------------------------------------- #
# the parser, on the lines W1 actually logged
# --------------------------------------------------------------------------- #


def test_the_w1_journal_lines_parse() -> None:
    lines = [
        "2026-09-24T08:35:49+0100 hub fpgahubd[912]: sd_install: writing "
        "/home/hubuser/mint_72BB0A36/prod/config_rm_greybox_fw.bit -> "
        "/run/fpgahub/sd/mps3_pl/MB/HBI0309C/Nanosoc/nanosoc.bit",
        "2026-09-24T08:36:57+0100 hub fpgahubd[912]: program dispatched: "
        "board=mps3_pl method=sd plugin=sd_install ok=True part=xcku115-flvb1760-1-c "
        "sha256=6a7a2755886a dur=68.44s",
        "2026-09-24T08:37:01+0100 hub fpgahubd[912]: program dispatched: "
        "board=kr260_01 method=sd plugin=sd_install ok=True part=xck26 sha256=aaaaaaaaaaaa "
        "dur=10.00s",
        "2026-09-24T08:37:02+0100 hub fpgahubd[912]: program dispatched: "
        "board=mps3_pl method=jtag plugin=vivado_jtag ok=True part=x sha256=bbbbbbbbbbbb "
        "dur=10.00s",
    ]
    ev = fl.parse_events(lines, TARGET)
    assert [e.kind for e in ev] == ["writing", "dispatched"], "other boards/methods dropped"
    assert ev[1].sha12 == "6a7a2755886a" and ev[1].ok and ev[1].dur_s == 68.44
    assert ev[1].ts == datetime(2026, 9, 24, 8, 36, 57, tzinfo=BST).timestamp()
    assert fl.judge(ev, "6a7a2755886a" + "f" * 52).ok
    assert fl.judge(ev[:1], "6a7a2755886a" + "f" * 52).status == "in-flight"
    assert fl.line_epoch("1790000000.250000 host x: y") == 1790000000.25
    assert fl.line_epoch("Sep 24 08:36:57 host x: y") is None


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #


def test_cli_sd_field_dry_run_contacts_nothing_and_names_the_sequence(capsys) -> None:
    hub = FakeHub()
    rc = cli_mod.main(["sd", "field", "/home/hubuser/mints/0xS3/config_rm_greybox_stage0.bit",
                       "--holder", HOLDER, "--dry-run"], runner=hub)
    out = capsys.readouterr().out
    assert rc == 0 and hub.calls == []
    assert ("fpgahub target program mps3_pl /home/hubuser/mints/0xS3/"
            "config_rm_greybox_stage0.bit --method sd --yes --no-skip-if-loaded") in out
    assert "ONCE, never retried" in out and "ok=True" in out
    assert "journalctl -u fpgahubd" in out and TTY00 in out and "100 ms/char" in out


def test_cli_sd_field_needs_a_holder(capsys, monkeypatch) -> None:
    monkeypatch.delenv("MPS3_LEASE_HOLDER", raising=False)
    rc = cli_mod.main(["sd", "field", "/x.bit", "--dry-run"], runner=FakeHub())
    assert rc == fl.EXIT_USAGE and "--holder" in capsys.readouterr().err


def test_cli_sd_field_refuses_a_burst_pace(capsys) -> None:
    rc = cli_mod.main(["sd", "field", "/x.bit", "--holder", HOLDER, "--mcc-pace", "0.01",
                       "--dry-run"], runner=FakeHub())
    assert rc == fl.EXIT_USAGE and "burst" in capsys.readouterr().err


def test_cli_sd_field_end_to_end_on_a_real_clock(bit, tmp_path, capsys) -> None:
    hub = SimHub(RealClock, dispatch_after=1.2, client_wait=0.0, boot_wait=0.0)
    out_file = tmp_path / "field.json"
    rc = cli_mod.main(["sd", "field", bit, "--holder", HOLDER, "--poll", "0.2",
                       "--witness-deadline", "20", "--out", str(out_file)], runner=hub)
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    rec = json.loads(cap.out)
    assert rec["ok"] and rec["witness"]["status"] == "ok" and rec["reboot"]["complete"]
    assert json.loads(out_file.read_text()) == rec
    assert len(hub.program_calls) == 1 and hub.reboot_at >= hub.dispatch_at
    assert "EXPECTED" in cap.err and "NOT retrying" in cap.err


# --------------------------------------------------------------------------- #
# defect 6 part 1: the paced tty route is the default everywhere
# --------------------------------------------------------------------------- #


def test_boot_rate_defaults_to_the_paced_tty_route() -> None:
    args = cli_mod.build_parser().parse_args(["boot-rate", "--host", "b", "--method", "mcc"])
    assert args.mcc_route == "tty"
    assert br.DEFAULT_MCC_ROUTE == "tty"
    calls = []

    def runner(argv, timeout=None):
        calls.append(list(argv))
        return RunResult(0, json.dumps({"rc": 0, "ack": True, "sent": True,
                                        "echo": "Rebooting..."}) + "\n", "")
    r = br.MccRebootResetter(runner, target=TARGET)
    assert r.route == "tty" and "fpgahub" not in r.describe()
    out = r()
    assert out.ok and out.route == "tty"
    assert not any(c[:1] == ["fpgahub"] for c in calls), "fpgahub's reset.mcc was asked"


def test_boot_rate_dry_run_without_a_route_plans_the_paced_write(capsys) -> None:
    hub = FakeHub()
    rc = cli_mod.main(["boot-rate", "--host", "board", "--method", "mcc", "--dry-run"],
                      runner=hub)
    out = capsys.readouterr().out
    assert rc == 0 and hub.calls == []
    assert "paced REBOOT on /dev/mps3_pl/tty_00" in out
    assert "fpgahub target reset" not in out


def test_the_writer_argv_is_unchanged_without_a_capture() -> None:
    """boot-rate's hub command is byte-identical to before the capture option."""
    w = br.MccTtyResetter(lambda *a, **k: None, tty_path=TTY00)
    args = json.loads(w.argv()[-1])
    assert sorted(args) == ["ack_s", "baud", "mode", "pace", "prompt_s", "settle", "tty"]
    assert w.timeout == 60.0
    c = br.MccTtyResetter(lambda *a, **k: None, tty_path=TTY00, capture_s=150.0, log_path="/l")
    assert json.loads(c.argv()[-1])["capture_s"] == 150.0 and c.timeout >= 150.0 + 30.0
    assert "capture_s" not in json.loads(c.argv("scan")[-1])


# --------------------------------------------------------------------------- #
# the hub-side writer's boot-log capture, for real, against a pseudo-terminal
# --------------------------------------------------------------------------- #

BOOT_LOG = (b"Rebooting...\r\nDisabling debug USB..\r\nBoard rebooting...\r\n"
            b"ARM V2M-MPS3 Firmware v1.3.2\r\nPowering up system...\r\n"
            b"Configuring FPGA from file \\MB\\HBI0309C\\Nanosoc\\nanosoc.bit\r\n"
            b"Address: 0x00BC0000\r\n")


class PtyMcc:
    """The MCC on a pty: a bare CR gets ``Cmd>``; REBOOT gets the boot log, then
    (``complete``) ``FPGA configuration complete.`` and a fresh ``Cmd>``."""

    def __init__(self, *, complete=True):
        self.master, self._slave = os.openpty()
        self.path = os.ttyname(self._slave)
        self.complete = complete
        self.rx = b""
        self._line = b""
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.master, 1024)
            except OSError:
                continue
            for b in data:
                self.rx += bytes([b])
                if b != 13:
                    self._line += bytes([b])
                    continue
                if self._line == b"":
                    os.write(self.master, b"\r\n\r\nCmd> ")
                elif self._line == b"REBOOT":
                    os.write(self.master, b"REBOOT\r\n")
                    for part in BOOT_LOG.split(b"\r\n"):
                        os.write(self.master, part + b"\r\n")
                        _time.sleep(0.02)
                    if self.complete:
                        os.write(self.master, b"FPGA configuration complete.\r\n"
                                              b"OSCCLK setup: PASSED\r\nCmd> ")
                self._line = b""

    def close(self):
        self._stop.set()
        self._t.join(1.0)
        os.close(self.master)
        os.close(self._slave)


def _local(argv, timeout=None):
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


_needs_pty = pytest.mark.skipif(not hasattr(os, "openpty") or not os.path.isdir("/proc"),
                                reason="needs a pty and /proc")


@_needs_pty
@pytest.mark.parametrize("complete", [True, False])
def test_the_hub_writer_captures_the_boot_log_to_configuration_complete(tmp_path, complete):
    mcc = PtyMcc(complete=complete)
    log = tmp_path / "mcc.log"
    try:
        w = br.MccTtyResetter(_local, tty_path=mcc.path, pace_s=0.05, settle_s=0.1,
                              ack_s=2.0, prompt_s=1.0, python=sys.executable,
                              capture_s=(5.0 if complete else 1.5), log_path=str(log))
        t = _time.monotonic()
        out = w()
        took = _time.monotonic() - t
    finally:
        mcc.close()
    assert out.ok and out.sent is True
    info = w.last_info
    assert info["ack"] and info["configuring"] and info["complete"] is complete
    assert info["log"] == str(log)
    text = log.read_bytes()
    assert b"Configuring FPGA from file" in text and b"Cmd>" in text
    assert (b"FPGA configuration complete" in text) is complete
    if complete:
        assert took < 4.5, "the capture stops at the Cmd> after 'complete', not at capture_s"
    assert mcc.rx == b"\rREBOOT\r"
