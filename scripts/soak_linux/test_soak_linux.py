"""Board-free tests for soak_linux.py: every mode's dry run against pyverify's
FakeShell(profile="linux"), every stop rule by injection, the paced MCC REBOOT
against a pty that behaves like the MCC (drops burst characters), and the remote
shell snippets under a real POSIX shell.

Run: python3 -m pytest scripts/soak_linux/test_soak_linux.py -q   (python >= 3.10 with pytest; ~2.5 min)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import soak_linux as sl  # noqa: E402

# A small tail schedule: 30 virtual minutes at 1/100 = 18 s of wall time.
TAIL_SMALL = ["tail", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "30m",
              "--poll-every", "1m", "--swap-every", "5m", "--ssh-every", "2m",
              "--sample-every", "1m"]


def run(argv, tmp_path, name="ev.jsonl"):
    out = tmp_path / name
    rc = sl.main(list(argv) + ["--out", str(out)])
    events = [json.loads(ln) for ln in out.read_text().splitlines()]
    end = [e for e in events if e["ev"] == "end"][-1]
    return rc, end, events


# --------------------------------------------------------------------------- #
# the accel profile end to end
# --------------------------------------------------------------------------- #


def test_accel_small_schedule_passes_every_criterion(tmp_path):
    rc, end, events = run([
        "accel", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "40m",
        "--phase-a", "20m", "--phase-u", "12m", "--boots-a", "2", "--mcc-every-b", "0",
        "--mps3-reboot-every", "0", "--wdog-every", "0", "--fallback-at", "5m",
        "--swap-every", "3m", "--ssh-every", "1m", "--poll-every", "1m", "--sample-every", "1m",
        "--churn-every", "2m", "--debug-every", "5m"], tmp_path)
    assert end["verdict"] == "PASS", (end.get("fail_kinds"), end.get("problems"), end.get("undecided"))
    assert rc == 0
    drills = {e["drill"]: e for e in events if e["ev"] == "drill"}
    assert drills["sweep"]["n"] == 4 and drills["regress"]["ok"]
    assert drills["regress"]["version"]["impl"] == "linux"
    assert drills["regress"]["dutrx"]["frame_hex"].startswith("0180c2000001")
    assert drills["fallback"]["fell_back_to"] == "B" and drills["fallback"]["restored_to"] == "A"
    clk = drills["set_clk"]
    assert [r["preset"] for r in clk["rows"]] == ["25mhz", "50mhz", "100mhz", "50mhz"]
    assert all(r["ok"] and r["locked"] and r["mmcm"] for r in clk["rows"])
    assert [r["dut_mhz"] for r in clk["rows"]] == [25, 50, 100, 50]      # back to the default
    assert end["power_on_boots"] == 2 and end["reboot_verbs"] == 3      # regress + 2 fallback
    assert all(v == 0.0 for v in end["leak_growth_over_window"].values())
    md = (tmp_path / "ev.summary.md").read_text()
    for section in ("## Verdict", "## Counts", "## Sweep", "## W1 subset", "## set_clk",
                    "## Slot-A fallback",
                    "## Warm resets", "## Power-on boots", "## Resources (phase U)",
                    "## Swap times", "## Criterion"):
        assert section in md, section
    assert "2/2 unattended power-on boots" in md


def test_default_drill_plan_meets_the_study_counts():
    args = sl.build_parser().parse_args(["accel", "--expect-static-id", "0x1"])
    plan = sl.drill_plan(args)
    kinds = [d for _, d in plan]
    assert kinds[:3] == ["sweep", "regress", "set_clk"]
    assert [t for t, d in plan if d == "set_clk"] == [0.0, 18 * 3600]   # start of phases A and B
    assert kinds.count("power_on") == 13          # 10 in phase A (the 10/10) + 3 in phase B
    assert kinds.count("mps3_reboot") == 6 and kinds.count("wdog_trip") == 4
    assert kinds.count("fallback") == 1
    # nothing resets the board in the 12 h continuous-uptime window
    assert not [t for t, d in plan if 6 * 3600 <= t < 18 * 3600]
    # >= 8 WDOG resets from the drills alone (plus the regress reboot and the fallback's two)
    assert kinds.count("mps3_reboot") + kinds.count("wdog_trip") >= 8


# --------------------------------------------------------------------------- #
# every stop rule, by injection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("inject,kind", [
    ("hang@3", "wedged"),
    ("kill@3", "offline"),
    ("reset@3", "unexplained_reset"),
    ("respawn@3", "harnessd_respawn"),
    ("reflash@3", "identity"),
    ("swapfail@0", "swap"),
    ("sshfail@3", "ssh"),
    ("oops@3", "kernel_oops"),
    ("rescue@3", "rescue"),
])
def test_each_stop_rule_fires_and_collects_forensics(tmp_path, inject, kind):
    rc, end, events = run(TAIL_SMALL + ["--dry-run-inject", inject], tmp_path)
    assert rc == 1 and end["verdict"] == "FAIL"
    assert end["fail_kinds"][0] == kind, end["fail_kinds"]
    assert any(e["ev"] == "forensics" for e in events)


def test_an_unlocked_mmcm_fails_the_set_clk_drill(tmp_path):
    rc, end, events = run([
        "accel", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "10m",
        "--phase-a", "5m", "--phase-u", "3m", "--boots-a", "0", "--mcc-every-b", "0",
        "--mps3-reboot-every", "0", "--wdog-every", "0", "--no-fallback",
        "--dry-run-inject", "unlock@0"], tmp_path)
    assert rc == 1 and end["fail_kinds"][0] == "drill", end["fail_kinds"]
    fail = [e for e in events if e["ev"] == "FAIL"][0]
    assert fail["detail"].startswith("set_clk 25mhz") and fail["set_clk"][0]["locked"] is False


def test_a_leak_fails_the_flat_criterion(tmp_path):
    rc, end, _ = run(TAIL_SMALL + ["--dry-run-inject", "leak@0"], tmp_path)
    assert rc == 1 and end["verdict"] == "FAIL"
    assert end["leak_growth_over_window"]["rss_kb"] > sl.LEAK_LIMITS_KB["rss_kb"]
    assert any(p.startswith("leak: rss_kb") for p in end["problems"])


def test_tail_clean_run_passes(tmp_path):
    rc, end, _ = run(TAIL_SMALL, tmp_path)
    assert (rc, end["verdict"]) == (0, "PASS"), (end.get("problems"), end.get("undecided"))
    assert end["swaps"] >= 5 and end["samples"] >= 24


def test_keep_going_records_and_still_fails(tmp_path):
    rc, end, _ = run(TAIL_SMALL + ["--keep-going", "--dry-run-inject", "respawn@3"], tmp_path)
    assert rc == 1 and end["fail_kinds"] == ["harnessd_respawn"]
    assert end["ran_s"] >= 17                      # it carried on to the end


# --------------------------------------------------------------------------- #
# power-on boots
# --------------------------------------------------------------------------- #

BOOTS = ["boots", "--dry-run", "--quiet", "--time-scale", "0.01", "--n", "3", "--gap", "30"]


def test_boots_pass_with_the_interval(tmp_path):
    rc, end, events = run(BOOTS, tmp_path)
    assert (rc, end["verdict"], end["boots_ok"]) == (0, "PASS", 3)
    rows = [e for e in events if e["ev"] == "boot_ok"]
    assert all(r["boot_count"] == 1 and r["booted_from"] == "A" and r["down_seen"] for r in rows)
    assert "95% Clopper-Pearson [29.2%, 100.0%]" in (tmp_path / "ev.summary.md").read_text()


def test_a_reboot_that_never_goes_down_is_not_a_boot(tmp_path):
    rc, end, _ = run(BOOTS + ["--dry-run-inject", "noreset@0"], tmp_path)
    assert rc == 1 and end["fail_kinds"] == ["reset_timeout"]
    ev = [json.loads(x) for x in (tmp_path / "ev.jsonl").read_text().splitlines()]
    assert "never went down" in [e for e in ev if e["ev"] == "FAIL"][0]["detail"]


def test_clopper_pearson_reference_values():
    lo, hi = sl.clopper_pearson(10, 10)
    assert round(lo * 100, 1) == 69.2 and hi == 1.0      # the write-up's §7.4 reference
    lo, hi = sl.clopper_pearson(9, 10)
    assert round(lo, 3) == 0.555 and round(hi, 4) == 0.9975


# --------------------------------------------------------------------------- #
# the paced MCC REBOOT, against a pty that drops burst input like the MCC
# --------------------------------------------------------------------------- #

BOOT_LOG = (b"\r\nRebooting...\r\nDisabling debug USB..\r\nPowering up system...\r\n"
            b"Configuring FPGA from file \\MB\\HBI0309C\\Nanosoc\\nanosoc.bit\r\n"
            b"FPGA configuration complete.\r\nOSCCLK setup: PASSED\r\nCmd> ")


class FakeMcc:
    """The master side of a pty: echoes, DROPS a character that arrives less than
    ``min_gap`` after the previous one (the MCC's burst behaviour), and answers a
    complete ``REBOOT\\r`` line with a boot log."""

    def __init__(self, min_gap=0.05, prompt=b"\r\n\r\nCmd> "):
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.min_gap = min_gap
        #: what a bare CR brings back (the real MCC: "\r\n\r\nCmd> ")
        self.prompt = prompt
        self.line = b""
        self.got = b""
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        last = 0.0
        while not self.stop:
            try:
                ch = os.read(self.master, 1)
            except OSError:
                return
            now = time.monotonic()
            burst = now - last < self.min_gap
            last = now
            self.got += ch
            if burst and ch != b"\r":
                continue                               # dropped, like the real MCC
            os.write(self.master, ch)
            if ch == b"\r":
                if self.line == b"" and self.prompt:
                    os.write(self.master, self.prompt)
                if self.line.strip() == b"REBOOT":
                    for part in BOOT_LOG.split(b"\r\n"):
                        os.write(self.master, part + b"\r\n")
                        time.sleep(0.02)
                self.line = b""
            else:
                self.line += ch

    def close(self):
        self.stop = True
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def test_paced_reboot_is_acknowledged_and_the_log_captured(tmp_path):
    mcc = FakeMcc()
    try:
        res = sl.mcc_reboot(mcc.path, tmp_path / "mcc.log", pace=0.1, settle=0.2, capture_s=5,
                            check_readers=False)
    finally:
        mcc.close()
    assert res["rc"] == 0 and res["ack"] and res["complete"], res
    assert b"FPGA configuration complete" in (tmp_path / "mcc.log").read_bytes()


def test_a_burst_reboot_is_dropped_and_reported(tmp_path):
    mcc = FakeMcc()
    try:
        res = sl.mcc_reboot(mcc.path, tmp_path / "mcc.log", pace=0.0, settle=0.2, capture_s=3,
                            ack_s=1.5, check_readers=False)
    finally:
        mcc.close()
    assert res["rc"] == 1 and not res["ack"], res


def test_a_second_reader_refuses_before_sending(tmp_path):
    mcc = FakeMcc()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)", mcc.path])
    try:
        time.sleep(0.2)
        res = sl.mcc_reboot(mcc.path, tmp_path / "mcc.log", pace=0.1, settle=0.1, capture_s=2)
    finally:
        other.kill()
        mcc.close()
    assert res["rc"] == 2 and str(other.pid) in res["why"]
    assert mcc.got == b""                              # nothing was sent


# --------------------------------------------------------------------------- #
# the remote shell snippets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cmd", [sl.LOGIN_CMD, sl.SAMPLE_CMD, sl.TRIP_CMD, sl.MPS3_REBOOT_CMD,
                                 sl.FORENSIC_CMD,
                                 sl.CORRUPT_TMPL.format(dev="/dev/x", off=8),
                                 sl.RESTORE_TMPL.format(dev="/dev/x", off=8, orig="123")])
def test_remote_snippets_parse_as_posix_sh(cmd):
    assert subprocess.run(["sh", "-n", "-c", cmd]).returncode == 0


def test_corrupt_then_restore_flips_exactly_one_byte(tmp_path):
    dev = tmp_path / "slotA.img"
    data = bytes(range(256)) * 64
    dev.write_bytes(data)
    off = 5000
    out = subprocess.run(["sh", "-c", sl.CORRUPT_TMPL.format(dev=dev, off=off)],
                         capture_output=True, text=True, check=True).stdout
    orig, new = out.split()[0].split("=")[1], out.split()[1].split("=")[1]
    got = dev.read_bytes()
    assert got[off] == data[off] ^ 0xFF and int(orig, 8) == data[off] and int(new, 8) == got[off]
    assert got[:off] == data[:off] and got[off + 1:] == data[off + 1:]
    out = subprocess.run(["sh", "-c", sl.RESTORE_TMPL.format(dev=dev, off=off, orig=orig)],
                         capture_output=True, text=True, check=True).stdout
    assert dev.read_bytes() == data and out.strip() == "now=%s" % orig


def test_ssh_argv_is_key_only_batch_with_the_target_last():
    argv = sl.ssh_base_argv("root@192.168.10.101", ["-i", "/k"])
    assert argv[0] == "ssh" and argv[-1] == "root@192.168.10.101"
    assert "BatchMode=yes" in argv and "PasswordAuthentication=no" in argv
    assert argv[argv.index("-i") + 1] == "/k"


def test_real_mode_needs_the_expected_static_id(tmp_path, capsys):
    assert sl.main(["tail", "--overlay", str(tmp_path)]) == sl.EXIT_USAGE
    assert "--expect-static-id is required" in capsys.readouterr().err


def test_dry_run_swaps_go_through_deploys_transport_auto_detect(tmp_path):
    """Finding #13: the dry run no longer forces --transport tftp, so deploy's
    auto-detect runs on every swap, as on the board. The linux profile (like
    harnessd) reports impl "linux" and no `windowed`, so it picks plain 6910 --
    the one transport that survives the busy-ICAP window (B1 v4 finding h: the
    Sim streams nanosoc's 167,308 B clearing before the swap AWAY from it can take
    its partial, and a TFTP partial is rejected in that window). The run must
    include that swap. Negative control: an explicit --transport tftp is recorded
    as explicit flags and survives only through the Linux TFTP re-push."""
    rc, end, events = run(TAIL_SMALL, tmp_path)
    swaps = [e for e in events if e["ev"] == "swap" and e.get("ok")]
    assert rc == 0 and swaps, (end.get("fail_kinds"), end.get("problems"))
    for e in swaps:
        t = e["transport"]
        assert (t["push"], t["src"], t["windowed"]) == ("tcp", "tcp", False)
        assert t["why"].startswith("version.impl = linux"), t["why"]
    names = [e["rm"] for e in swaps]
    assert "nanosoc" in names[:-1], names                     # a swap AWAY from nanosoc ran
    assert sl.Sim.__init__ and sl.SIM_CLEARING_BYTES["nanosoc"] == 167_308
    rc, end, events = run(TAIL_SMALL + ["--transport", "tftp"], tmp_path, "explicit.jsonl")
    swaps = [e for e in events if e["ev"] == "swap" and e.get("ok")]
    assert rc == 0 and swaps, (end.get("fail_kinds"), end.get("problems"))
    assert all(e["transport"]["why"].startswith("explicit flags; impl=linux over tftp")
               for e in swaps)
    assert "nanosoc" in [e["rm"] for e in swaps][:-1]


# --------------------------------------------------------------------------- #
# ILA handoff defect 6: the MCC drill is the paced tty_00 REBOOT, never fpgahub's burst
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["accel", "tail", "boots"])
def test_the_mcc_drill_defaults_to_the_paced_tty_reboot(mode):
    args = sl.build_parser().parse_args([mode])
    assert args.reset == "mcc" and args.mcc_tty == sl._board_tty(0)
    assert (args.mcc_pace, args.mcc_settle) == (0.1, 1.0)
    assert sl.reset_refusal(args.reset) is None


@pytest.mark.parametrize("cmd", [
    "fpgahub target reset mps3_pl --method mcc --yes",
    "sg fpga -c 'fpgahub target reset mps3_pl --method=mcc --yes'",
])
def test_fpgahubs_burst_mcc_reboot_is_refused_as_a_reset_command(cmd, capsys):
    assert sl.main(["boots", "--reset", cmd, "--expect-static-id", "0x72bb0a36",
                    "--overlay", "/nonexistent"]) == sl.EXIT_USAGE
    err = capsys.readouterr().err
    assert "burst" in err and "--reset mcc" in err


def test_other_reset_commands_still_pass_the_refusal():
    for cmd in ("mcc", "manual", "none", "fpgahub target reset mps3_pl --method msd --yes",
                "ssh hub power-cycle"):
        assert sl.reset_refusal(cmd) is None, cmd


@pytest.mark.parametrize("prompt", [b"", b"\r\nDebug> "], ids=["silent", "debug-menu"])
def test_no_intact_cmd_prompt_refuses_before_reboot_is_typed(tmp_path, prompt):
    """A reader this account cannot see (a root cat, an fpgahub share) shows only
    as the MCC's prompt going missing: refuse, rc 3, only the bare CR was sent."""
    mcc = FakeMcc(prompt=prompt)
    try:
        res = sl.mcc_reboot(mcc.path, tmp_path / "mcc.log", pace=0.1, settle=0.2, capture_s=3,
                            check_readers=False, prompt_s=0.5)
    finally:
        mcc.close()
    assert res["rc"] == 3 and "REBOOT NOT sent" in res["why"], res
    assert mcc.got == b"\r"


def test_the_prompt_check_can_be_waived_explicitly(tmp_path):
    mcc = FakeMcc(prompt=b"")
    try:
        res = sl.mcc_reboot(mcc.path, tmp_path / "mcc.log", pace=0.1, settle=0.2, capture_s=5,
                            check_readers=False, check_prompt=False)
    finally:
        mcc.close()
    assert res["rc"] == 0 and res["ack"], res
    assert sl.build_parser().parse_args(["mcc-reboot", "--log", "x",
                                         "--no-prompt-check"]).check_prompt is False
    assert sl.build_parser().parse_args(["mcc-reboot", "--log", "x"]).check_prompt is True


# --------------------------------------------------------------------------- #
# --netboot: the user microSD is dead, every reset lands stage0 in rescue
# --------------------------------------------------------------------------- #

def _nb(tmp_path):
    """A --netboot IMAGE that does not exist: the dry run pushes its synthetic one."""
    return ["--netboot", str(tmp_path / "linux_slot.img")]


def test_netboot_accel_small_schedule_passes(tmp_path):
    rc, end, events = run([
        "accel", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "60m",
        "--phase-a", "20m", "--phase-u", "30m", "--boots-a", "1", "--mcc-every-b", "0",
        "--mps3-reboot-every", "0", "--wdog-every", "0", "--fallback-at", "5m",
        "--swap-every", "3m", "--ssh-every", "2m", "--poll-every", "1m", "--sample-every", "2m",
        "--churn-every", "3m", "--debug-every", "5m"] + _nb(tmp_path), tmp_path)
    assert end["verdict"] == "PASS", (end.get("fail_kinds"), end.get("problems"), end.get("undecided"))
    assert rc == 0 and end["netboot"] is True
    # the fallback drill is dropped from the plan AND said to be skipped, not silently passed
    plan = [e for e in events if e["ev"] == "plan"][0]
    assert "fallback" not in [d for _, d in plan["drills"]]
    assert not [e for e in events if e["ev"] == "drill" and e["drill"] == "fallback"]
    assert any(s.startswith("fallback drill: netboot") for s in end["skipped"])
    # one push per reset (the regress reboot verb + the power-on), each claimed + unbound
    pushes = [e for e in events if e["ev"] == "netboot_push"]
    assert [p["what"] for p in pushes] == ["reboot_verb", "power-on 1"]
    assert all(p["ok"] and p["push_rc"] == 0 and p["rescue_reason"] == "card error" for p in pushes)
    assert end["netboot_pushes"] == 2 and end["claims"] == 2
    unbinds = [e for e in events if e["ev"] == "netboot_unbind"]
    assert len(unbinds) == 3 and all(u["deviation"] and u["unbound"] == "1" for u in unbinds)
    assert end["deviations"] and "mmc_spi unbound" in end["deviations"][0]
    # every boot came from the rescue push; the power-on proof is the hand-off count,
    # since a silicon cold boot reads boot_count 4 (the Sim models it)
    resets = [e for e in events if e["ev"] == "reset_ok"]
    boots = [e for e in events if e["ev"] == "boot_ok"]
    assert all(e["booted_from"] == "RESCUE" for e in resets + boots)
    assert [b["boot_count"] for b in boots] == [sl.SIM_COLD_ENTRIES]
    assert all(b["confirmed"] is False for b in boots)           # the dead card: never confirmed
    assert all(r["wrs"] for r in resets)
    assert end["unconfirmed_expected"] >= 3
    assert any(s.startswith("stage0 CONFIRM on") for s in end["skipped"])
    # the board is claimed, so every XVC/JTAG session rode the ssh tunnel
    dbg = [e for e in events if e["ev"] == "debug"]
    assert dbg and all(d["via"] == "ssh" for d in dbg) and end["debug_tunnelled"] == len(dbg)
    regress = [e for e in events if e["ev"] == "drill" and e["drill"] == "regress"][0]
    assert regress["jtag"].endswith("via ssh)")
    # the MAC test runs with eth_ss loaded (GENCHK checks what the DUT SENDS), spaced
    # rounds, and its inject round is reported skipped -- never silently passed
    mt = regress["mactest"]
    assert mt["passed"] and mt["link"] == "down,up accepted" and mt["inject"].startswith("skipped")
    assert mt["rx"][-1] > mt["rx"][0] and mt["err"][0] == mt["err"][-1]
    assert any(s.startswith("MAC test fault-inject round: skipped") for s in end["skipped"])
    # the expected unhealthy boot-health is not a warning every minute
    assert not [e for e in events if e["ev"] == "warn" and "boot-health" in e["what"]]
    md = (tmp_path / "ev.summary.md").read_text()
    assert "## Netboot" in md and "| skipped |" in md and "| deviations |" in md


def test_netboot_warm_resets_judge_the_rescue_attempt(tmp_path):
    """The reboot verb and a WDOG trip, both through rescue. `nocard` pulls the
    card at once, so the first warm entry judges the dead-card boot UNCONFIRMED
    and the RAM boots after it are healthy and CONFIRMED -- the WDOG trip's entry
    then judges a confirmed rescue attempt. Any wrong counter or verdict FAILs."""
    rc, end, events = run([
        "accel", "--dry-run", "--quiet", "--time-scale", "0.004", "--duration", "50m",
        "--phase-a", "48m", "--phase-u", "1m", "--boots-a", "0", "--mcc-every-b", "0",
        "--mps3-reboot-every", "0", "--wdog-every", "3h", "--no-fallback",
        "--swap-every", "0", "--ssh-every", "2m", "--poll-every", "2m", "--sample-every", "0",
        "--churn-every", "0", "--debug-every", "0", "--stage0-every-logins", "1",
        "--dry-run-inject", "nocard@0"] + _nb(tmp_path), tmp_path)
    assert end["fails"] == 0, [e for e in events if e["ev"] == "FAIL"]
    resets = [e for e in events if e["ev"] == "reset_ok"]
    assert [r["what"] for r in resets] == ["reboot_verb", "wdog_trip"]
    assert all(r["booted_from"] == "RESCUE" and r["wrs"] for r in resets)
    assert resets[1]["boot_count"] == resets[0]["boot_count"] + 1
    pushes = [e for e in events if e["ev"] == "netboot_push"]
    assert [p["rescue_reason"] for p in pushes] == ["no card", "no card"]
    # stage0 reads after the healthy boots saw Linux confirm
    s0 = [e["stage0"] for e in events if e["ev"] == "ssh" and "stage0" in e]
    assert s0[0]["linux_confirmed"] is False and s0[-1]["linux_confirmed"] is True
    assert s0[-1]["last_verdict"] == "CONFIRMED" and s0[-1]["verdict_from"] == "RESCUE"


def test_netboot_a_failed_push_fails_with_kind_netboot(tmp_path):
    rc, end, events = run(["boots", "--dry-run", "--quiet", "--time-scale", "0.01", "--n", "1",
                           "--dry-run-inject", "pushfail@0"] + _nb(tmp_path), tmp_path)
    assert rc == 1 and end["verdict"] == "FAIL" and end["fail_kinds"] == ["netboot"]
    fail = [e for e in events if e["ev"] == "FAIL"][0]
    assert "rejected by stage0" in fail["detail"] and fail["push"]["push_rc"] == 2
    assert [e["ok"] for e in events if e["ev"] == "netboot_push"] == [False]


@pytest.mark.parametrize("keep_going", [False, True])
def test_netboot_an_unrequested_reset_into_rescue_still_fails(tmp_path, keep_going):
    """Only polls run (no ssh step can see the reset first): the reset nobody asked
    for lands in rescue and FAILs as `rescue`. With --keep-going the soak pushes
    once more (a board with a card would have come back by itself) and carries on."""
    argv = ["tail", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "30m",
            "--poll-every", "1m", "--swap-every", "0", "--ssh-every", "0", "--sample-every", "0",
            "--dry-run-inject", "reset@3"] + _nb(tmp_path) + (["--keep-going"] if keep_going else [])
    rc, end, events = run(argv, tmp_path)
    assert rc == 1 and end["verdict"] == "FAIL"
    assert end["fail_kinds"] == ["rescue"], end["fail_kinds"]
    if keep_going:
        assert end["recoveries"] == 1 and any(e["ev"] == "recovered" for e in events)
        t_rec = [e["el_s"] for e in events if e["ev"] == "recovered"][0]
        assert [e for e in events if e["ev"] == "poll" and e["el_s"] > t_rec]  # polls resumed
        assert end["ran_s"] >= 17
    else:
        assert not any(e["ev"] == "netboot_push" for e in events)   # a stop rule, not a drill


def test_debug_sessions_on_a_claimed_board_ride_the_tunnel(tmp_path):
    """The card-backed board is claimed too: auto sends XVC/JTAG through pyverify's
    SshTunnel; forcing direct meets the claim lock's one-line refusal."""
    rc, end, events = run(TAIL_SMALL + ["--debug-every", "5m"], tmp_path)
    assert (rc, end["verdict"]) == (0, "PASS"), (end.get("fail_kinds"), end.get("undecided"))
    dbg = [e for e in events if e["ev"] == "debug"]
    assert len(dbg) >= 5 and all(d["via"] == "ssh" and d["xvc"].startswith("xvcServer_v")
                                 for d in dbg)
    rc, end, events = run(TAIL_SMALL + ["--debug-every", "5m", "--debug-via", "direct"],
                          tmp_path, "direct.jsonl")
    assert rc == 1 and end["fail_kinds"] == ["debug"]
    fail = [e for e in events if e["ev"] == "FAIL"][0]
    assert "xvc locked: board claimed" in fail["detail"] and "ssh tunnel" in fail["detail"]


def test_the_netboot_drill_plan_has_no_fallback():
    base = ["accel", "--expect-static-id", "0x1"]
    card = [d for _, d in sl.drill_plan(sl.build_parser().parse_args(base))]
    nb = [d for _, d in sl.drill_plan(sl.build_parser().parse_args(base + ["--netboot", "x.img"]))]
    assert card.count("fallback") == 1 and "fallback" not in nb
    assert [d for d in card if d != "fallback"] == nb          # everything else is unchanged


def test_the_unbind_snippet_under_a_real_shell(tmp_path):
    """UNBIND_TMPL against a fake dmesg (the re-bake's real lines) and a fake
    driver directory: it names spi0.0 from dmesg, writes it to unbind, counts."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "dmesg").write_text(
        "#!/bin/sh\ncat <<'X'\n"
        "[   18.453435] mmc_spi spi0.0: SD/MMC host mmc0, no WP, no poweroff, cd polling\n"
        "[   18.673056] mmc0: error -110 whilst initialising SD card\n"
        "[   20.936844] mmc0: error -22 whilst initialising SD card\nX\n")
    (bindir / "dmesg").chmod(0o755)
    drv = tmp_path / "mmc_spi"
    drv.mkdir()
    (drv / "spi0.0").write_text("")
    mounts = tmp_path / "mounts"
    mounts.write_text("proc /proc proc rw 0 0\n")
    cmd = sl.UNBIND_TMPL.format(drv=drv, settle=0, mounts=mounts)
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]))
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == "U dev=spi0.0 unbound=1 err0=2 err1=2 err2=2"
    assert (drv / "unbind").read_text().strip() == "spi0.0"
    (drv / "spi0.0").unlink()                               # nothing bound any more
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env, check=True)
    assert "unbound=absent" in out.stdout
    # the card answered Linux and /persist is mounted from it: never unbind (run 3)
    (drv / "spi0.0").write_text("")
    (drv / "unbind").unlink()
    mounts.write_text("/dev/mmcblk0p3 /persist ext4 rw 0 0\n")
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env, check=True)
    assert "unbound=mounted" in out.stdout and not (drv / "unbind").exists()
    assert subprocess.run(["sh", "-n", "-c", sl.UNBIND_CMD]).returncode == 0


def test_netboot_ssh_argv_survives_a_new_host_key_and_stays_key_only():
    argv = sl.ssh_base_argv("root@192.168.10.101", list(sl.NETBOOT_SSH_OPTS) + ["-i", "/k"])
    assert argv[0] == "ssh" and argv[-1] == "root@192.168.10.101"
    for opt in ("BatchMode=yes", "PasswordAuthentication=no", "StrictHostKeyChecking=no",
                "UserKnownHostsFile=/dev/null"):
        assert opt in argv, opt


def test_the_netboot_sim_refuses_what_a_real_ssh_would(tmp_path):
    """A RAM boot is unclaimed and has a new host key: the Sim's ssh seam refuses
    both until the claim and the no-known_hosts options are there (so a dry run
    catches a soak that forgot either)."""
    sl.load_pyverify()
    sim = sl.Sim(tmp_path, netboot=True)
    try:
        sim._netboot_reset()
        assert sim.run(["ssh", "true"])[0] == 255              # rescue: no sshd
        ok, _ = sim._on_push(b"img")
        t = time.monotonic()
        while sim.fake.mode != "run" and time.monotonic() - t < 10:
            time.sleep(0.05)
        assert "Permission denied" in sim.run(["ssh", "true"])[2]
        sim.fake.ssh_claimed = True
        assert "Host key verification failed" in sim.run(["ssh", "true"])[2]
        assert sim.run(["ssh", "-o", "StrictHostKeyChecking=no", "true"])[0] == 0
    finally:
        sim.close()


def test_health_and_fingerprint_helpers():
    assert sl.health_state("healthy=1 persist=tmpfs reasons=") == "healthy"
    assert sl.health_state("healthy=0 persist=tmpfs reasons=usd-driver-path") == "card_fault"
    assert sl.health_state("healthy=0 reasons=usd-driver-path,no-ipv4") == "unhealthy"
    assert sl.health_state(None) == "unknown"
    sl.load_pyverify()
    from pyverify.testing.fakeshell import _authorized_key_fingerprint
    key = sl._sim_pubkey()
    assert sl.key_fingerprints(key.decode()) == [_authorized_key_fingerprint(key)]


def _real_accel(tmp_path, *extra):
    ovl = sl.make_overlay(tmp_path / "ovl", "greybox", 0x01000000, 0x44EE76D5)
    return ["accel", "--expect-static-id", "0x44EE76D5", "--overlay", str(ovl),
            "--out", str(tmp_path / "x.jsonl")] + list(extra)


@pytest.mark.parametrize("case", ["no-claim-key", "bad-image", "key-mismatch", "jtag-cmd"])
def test_netboot_refuses_a_bad_setup_before_the_run(tmp_path, capsys, case):
    img = tmp_path / "linux_slot.img"
    img.write_bytes(sl.synth_s0lb_image())
    key = tmp_path / "keys.pub"
    key.write_bytes(sl._sim_pubkey())
    extra = ["--netboot", str(img), "--claim-key", str(key)]
    if case == "no-claim-key":
        extra, want = ["--netboot", str(img)], "needs --claim-key"
    elif case == "bad-image":
        img.write_bytes(b"not an image" * 10)
        want = "not a bootable stage0 image"
    elif case == "key-mismatch":
        ident = tmp_path / "id_soak"
        (tmp_path / "id_soak.pub").write_bytes(sl._sim_pubkey())    # a different key
        extra += ["--ssh-opt=-i", "--ssh-opt=%s" % ident]
        want = "lock the soak's own ssh key out"
    else:
        extra, want = ["--jtag-cmd", "openocd -c init -c halt"], "{jtag_host}:{jtag_port}"
    assert sl.main(_real_accel(tmp_path, *extra)) == sl.EXIT_USAGE
    assert want in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the single-client race (real netboot soak 2026-09-27, 17 min in: regress's
# `version` got ECONNRESET and the soak died with a bare traceback)
# --------------------------------------------------------------------------- #

# tail with no swaps (a deploy is pyverify's own client) and back-to-back churn
TAIL_CTL = ["tail", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "30m",
            "--poll-every", "1m", "--swap-every", "0", "--ssh-every", "2m",
            "--sample-every", "1m", "--churn-every", "1m"]


def test_a_busy_control_port_is_retried_and_the_run_passes(tmp_path):
    rc, end, events = run(TAIL_CTL + ["--dry-run-inject", "ctlreset@3"], tmp_path)
    assert (rc, end["verdict"]) == (0, "PASS"), (end.get("fail_kinds"), end.get("undecided"))
    busy = [e for e in events if e["ev"] == "busy"]
    assert len(busy) == 2 and end["busy_retries"] == 2
    assert all(b["port"] and b["attempt"] and ("Reset" in b["err"] or "closed" in b["err"])
               for b in busy)
    # who held the port: ONE `ss` capture (<= 1 per SS_EVERY_S), our own sockets beside it
    caps = [b for b in busy if "ss" in b]
    assert len(caps) == 1
    assert "ESTAB" in caps[0]["ss"] and caps[0]["ours"]
    assert all("pid=%d," % caps[0]["pid"] in ln for ln in caps[0]["ours"])


def test_the_crash_site_regress_version_is_retried(tmp_path, monkeypatch):
    """The exact crash: the connection regress opens for `version` is reset twice."""
    orig = sl.Runner.regress

    def regress(self):
        self.sim.ctl_resets = 2
        return orig(self)
    monkeypatch.setattr(sl.Runner, "regress", regress)
    rc, end, events = run([
        "accel", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "10m",
        "--phase-a", "5m", "--phase-u", "3m", "--boots-a", "0", "--mcc-every-b", "0",
        "--mps3-reboot-every", "0", "--wdog-every", "0", "--no-fallback"], tmp_path)
    assert end["fails"] == 0, [e for e in events if e["ev"] == "FAIL"]
    assert [e["what"] for e in events if e["ev"] == "busy"] == ["version", "version"]
    assert [e for e in events if e["ev"] == "drill" and e["drill"] == "regress"][0]["ok"]


def test_a_control_port_that_never_frees_fails_wedged(tmp_path):
    rc, end, events = run(TAIL_CTL + ["--dry-run-inject", "ctlreset_all@3"], tmp_path)
    assert rc == 1 and end["fail_kinds"] == ["wedged"], end["fail_kinds"]
    assert end["busy_retries"] >= 3 and any(e["ev"] == "forensics" for e in events)


def test_a_reboot_verb_whose_reply_is_lost_is_never_sent_twice(tmp_path, monkeypatch):
    """The reply to `reboot` is lost after the send: the soak must not send it
    again; the reset itself (os_up_ms restarting) proves it was taken."""
    sl.load_pyverify()                             # the repo's pyverify, not a site one
    from pyverify.client import ShellClient
    sent = []

    def lossy(self):
        sent.append(1)
        self._send_op({"op": "reboot"})
        raise ConnectionResetError(104, "reset after the send (dry run)")
    monkeypatch.setattr(ShellClient, "reboot", lossy)
    rc, end, events = run([
        "accel", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "10m",
        "--phase-a", "5m", "--phase-u", "3m", "--boots-a", "0", "--mcc-every-b", "0",
        "--mps3-reboot-every", "0", "--wdog-every", "0", "--no-fallback"], tmp_path)
    assert end["fails"] == 0, [e for e in events if e["ev"] == "FAIL"]
    assert sent == [1]
    rows = [e for e in events if e["ev"] == "reset_ok"]
    assert len(rows) == 1 and rows[0]["reply_lost"] and rows[0]["what"] == "reboot_verb"
    assert any(e["ev"] == "warn" and "NOT re-sent" in e["what"] for e in events)


def test_busy_debug_ports_are_retried(tmp_path):
    rc, end, events = run(TAIL_SMALL + ["--debug-every", "5m", "--dry-run-inject", "dbgbusy@3"],
                          tmp_path)
    assert (rc, end["verdict"]) == (0, "PASS"), (end.get("fail_kinds"), end.get("undecided"))
    assert [e["what"] for e in events if e["ev"] == "busy"] == ["XVC", "XVC"]
    assert all(d["via"] == "ssh" for d in events if d["ev"] == "debug")


def test_an_unexpected_exception_is_a_crash_fail_with_forensics(tmp_path):
    rc, end, events = run(TAIL_SMALL + ["--dry-run-inject", "crash@3"], tmp_path)
    assert rc == 1 and end["verdict"] == "FAIL" and end["fail_kinds"] == ["crash"]
    fail = [e for e in events if e["ev"] == "FAIL"][0]
    assert fail["detail"] == "RuntimeError: injected crash (dry run)"
    assert "Traceback" in fail["traceback"] and "injected crash" in fail["traceback"]
    assert any(e["ev"] == "forensics" for e in events)
    assert "- `crash`: RuntimeError" in (tmp_path / "ev.summary.md").read_text()


def test_a_crash_outside_the_schedule_still_ends_with_a_verdict(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise KeyError("judge exploded")
    monkeypatch.setattr(sl, "judge", boom)
    rc, end, events = run(TAIL_SMALL + ["--duration", "3m"], tmp_path)
    assert rc == 1 and end["verdict"] == "FAIL" and end["fail_kinds"] == ["crash"]
    assert (tmp_path / "ev.summary.md").is_file()


def test_a_mac_checker_that_sees_no_dut_frames_fails_regress(tmp_path):
    """Silicon 2026-09-27: with an RM that sends nothing, GENCHK's rx never moves --
    the soak's first real run FAILed exactly so (it ran the test before eth_ss)."""
    rc, end, events = run([
        "accel", "--dry-run", "--quiet", "--time-scale", "0.01", "--duration", "40m",
        "--phase-a", "20m", "--phase-u", "12m", "--boots-a", "0", "--mcc-every-b", "0",
        "--mps3-reboot-every", "0", "--wdog-every", "0",
        "--dry-run-inject", "nomacrx@0"] + _nb(tmp_path), tmp_path)
    assert rc == 1 and end["verdict"] == "FAIL"
    fail = [e for e in events if e["ev"] == "FAIL"][0]
    assert fail["kind"] == "regress" and "did not advance" in fail["detail"], fail
