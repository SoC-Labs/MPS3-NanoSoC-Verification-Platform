"""``pyverify`` as the FRONT DOOR: the lease / sd / ping / version / diag verbs.

These verbs replace four shell scripts that each carried their own dialect of
the same conversation (``scripts/mps3_board.sh``,
``scripts/mps3_lease_acquire.sh``, ``scripts/mps3_sd_update.sh``,
``scripts/harness_gates/ping_check.py``). The contract the shell callers depend
on is pinned here:

  * ``lease acquire`` prints the **bare token on stdout** and nothing else, so
    ``TOKEN=$(… lease acquire …)`` keeps working. Progress goes to stderr.
  * every failure is a clean one-line stderr message and a documented exit
    code, never a traceback.

The hub is the in-process :class:`tests.fakehub.FakeHub` behind the
``--runner``-less injection seam; the shell is
:class:`pyverify.testing.fakeshell.FakeShell` on a loopback port. No hub, no
board.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakehub import FakeHub
from pyverify import cli as cli_mod
from pyverify.testing.fakeshell import FakeShell


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def test_lease_verbs_parse() -> None:
    p = cli_mod.build_parser()
    for sub in ("acquire", "release", "cancel", "status", "heartbeat", "preflight"):
        args = p.parse_args(["lease", sub] + (
            ["--token", "T"] if sub in ("release", "heartbeat") else []))
        assert args.verb == "lease"
        assert args.lease_verb == sub


def test_lease_acquire_defaults_are_the_safe_ones() -> None:
    args = cli_mod.build_parser().parse_args(["lease", "acquire"])
    assert args.tier == "interactive"       # background is revocable mid-swap
    assert args.ttl == 3600


def test_sd_write_parses() -> None:
    args = cli_mod.build_parser().parse_args(
        ["sd", "write", "some.bit", "--token", "T", "--holder", "h",
         "--backup", "/b", "--verify-path", "/mnt/x.bit"])
    assert (args.verb, args.sd_verb) == ("sd", "write")
    assert args.bundle == "some.bit"
    assert args.wait == 300.0


def test_shell_verbs_parse() -> None:
    p = cli_mod.build_parser()
    for verb in ("ping", "version", "diag"):
        args = p.parse_args([verb, "--host", "127.0.0.1"])
        assert args.verb == verb
        assert args.control_port == 6900


def test_deploy_accepts_a_prod_dir_and_rm_instead_of_an_overlay() -> None:
    args = cli_mod.build_parser().parse_args(
        ["deploy", "--host", "h", "--prod", "fielded/0xA8C1C535", "--rm", "regdemo_b"])
    assert args.prod == "fielded/0xA8C1C535"
    assert args.rm == "regdemo_b"
    assert args.overlay is None


# --------------------------------------------------------------------------- #
# lease
# --------------------------------------------------------------------------- #


def test_lease_acquire_prints_ONLY_the_token_on_stdout(capsys) -> None:
    hub = FakeHub()
    rc = cli_mod.main(["lease", "acquire", "--holder", "claude-ops"], runner=hub)
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "TOK-0001\n", "TOKEN=$(...) must capture the token and nothing else"
    assert "granted" in out.err


def test_lease_acquire_on_a_contended_board_polls(capsys) -> None:
    hub = FakeHub(queue_before_granting=1)
    rc = cli_mod.main(
        ["lease", "acquire", "--holder", "claude-ops", "--poll", "0"], runner=hub)
    assert rc == 0
    assert capsys.readouterr().out == "TOK-0001\n"


def test_lease_acquire_giving_up_exits_1_cleanly(capsys) -> None:
    hub = FakeHub(queue_before_granting=99)
    rc = cli_mod.main(
        ["lease", "acquire", "--holder", "claude-ops", "--poll", "0",
         "--acquire-timeout", "0"], runner=hub)
    err = capsys.readouterr()
    assert rc == 1
    assert err.out == "", "no half-token on stdout when we did not get the board"
    assert "Traceback" not in err.err
    assert "gave up" in err.err


def test_lease_status_prints_the_holder(capsys) -> None:
    hub = FakeHub()
    cli_mod.main(["lease", "acquire", "--holder", "claude-ops"], runner=hub)
    capsys.readouterr()
    rc = cli_mod.main(["lease", "status"], runner=hub)
    assert rc == 0
    assert "claude-ops" in capsys.readouterr().out


def test_lease_release_needs_holder_and_token(capsys) -> None:
    hub = FakeHub()
    cli_mod.main(["lease", "acquire", "--holder", "claude-ops"], runner=hub)
    capsys.readouterr()
    rc = cli_mod.main(
        ["lease", "release", "--token", "TOK-0001", "--holder", "wrong"], runner=hub)
    assert rc == 1
    assert "STILL HELD" in capsys.readouterr().err
    assert cli_mod.main(
        ["lease", "release", "--token", "TOK-0001", "--holder", "claude-ops"],
        runner=hub) == 0


def test_lease_cancel_recovers_a_stray_queue_entry(capsys) -> None:
    hub = FakeHub(queue_before_granting=1)
    hub.queued_holders.append("claude-stray")
    rc = cli_mod.main(["lease", "cancel", "--holder", "claude-stray"], runner=hub)
    assert rc == 0
    assert "cancelled" in capsys.readouterr().out


def test_lease_preflight_refuses_a_board_held_by_someone_else(capsys) -> None:
    hub = FakeHub()
    cli_mod.main(["lease", "acquire", "--holder", "other"], runner=hub)
    capsys.readouterr()
    rc = cli_mod.main(["lease", "preflight", "--holder", "claude-ops"], runner=hub)
    assert rc == 1
    assert "REFUSE" in capsys.readouterr().err


def test_lease_without_a_hub_exits_3_and_names_the_env_var(capsys, monkeypatch) -> None:
    for key in ("MPS3_HUB", "FPGAHUB_HOST", "MPS3_ON_HUB"):
        monkeypatch.delenv(key, raising=False)
    rc = cli_mod.main(["lease", "status"])
    err = capsys.readouterr().err
    assert rc == 3
    assert "MPS3_HUB" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# sd write
# --------------------------------------------------------------------------- #


def _bundle(tmp_path: Path) -> Path:
    p = tmp_path / "nanosoc.bit"
    p.write_bytes(b"\x00\x09\x0f\xf0" + b"x" * (2 * 1024 * 1024))
    return p


def _backup(tmp_path: Path) -> Path:
    d = tmp_path / "bk"
    d.mkdir()
    (d / "old.bit").write_bytes(b"y" * (2 * 1024 * 1024))
    return d


def test_sd_write_runs_the_discipline_and_logs_it(tmp_path, capsys) -> None:
    hub = FakeHub()
    cli_mod.main(["lease", "acquire", "--holder", "claude-ops"], runner=hub)
    capsys.readouterr()
    rc = cli_mod.main([
        "sd", "write", str(_bundle(tmp_path)),
        "--token", "TOK-0001", "--holder", "claude-ops",
        "--backup", str(_backup(tmp_path)),
        "--wait", "0", "--state-dir", str(tmp_path / "state"),
    ], runner=hub)
    out = capsys.readouterr()
    assert rc == 0
    for step in ("lease", "backup", "program", "wait", "verify"):
        assert step in out.err.lower(), "step %r not logged" % step
    payload = json.loads(out.out)
    assert payload["timed_out"] is True          # the expected outcome
    assert payload["verified"] is None           # honestly unverified
    assert payload["fielded"] is False
    # the write is not a reload: the JSON says what finishes the job
    assert "pyverify sd field" in payload["next"] and "--already-written" in payload["next"]


def test_sd_write_refused_by_a_gate_exits_1_cleanly(tmp_path, capsys) -> None:
    hub = FakeHub()
    cli_mod.main(["lease", "acquire", "--holder", "claude-ops"], runner=hub)
    capsys.readouterr()
    rc = cli_mod.main([
        "sd", "write", str(_bundle(tmp_path)),
        "--token", "TOK-0001", "--holder", "claude-ops",
        "--wait", "0", "--state-dir", str(tmp_path / "state"),
    ], runner=hub)
    err = capsys.readouterr().err
    assert rc == 1
    assert "backup gate" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# ping / version / diag over the conformance-pinned client
# --------------------------------------------------------------------------- #


@pytest.fixture
def shell():
    fake = FakeShell(
        static_id=0xA1B2C3D4, boot_rm_id=0x010000B2,
        control_port=0, tftp_port=0, raw_tcp_port=0,
        uart0_port=0, uart1_port=0, swo_port=0,
    )
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


def test_ping_prints_the_identity(shell, capsys) -> None:
    rc = cli_mod.main(["ping", "--host", "127.0.0.1",
                       "--control-port", str(shell.control_port)])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["shell_id"] == "0xa1b2c3d4"
    assert payload["ok"] is True


def test_ping_line_mode_matches_the_old_gate_output(shell, capsys) -> None:
    """``ping_check.py`` printed `shell_id=… rm_id=…`; harness_regression greps
    it. Keep that shape available."""
    rc = cli_mod.main(["ping", "--host", "127.0.0.1", "--line",
                       "--control-port", str(shell.control_port)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "shell_id=0xa1b2c3d4 rm_id=0x010000b2"


def test_version_prints_the_firmware_identity(shell, capsys) -> None:
    rc = cli_mod.main(["version", "--host", "127.0.0.1",
                       "--control-port", str(shell.control_port)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "features" in payload and isinstance(payload["features"], list)


def test_diag_prints_the_counters(shell, capsys) -> None:
    rc = cli_mod.main(["diag", "--host", "127.0.0.1",
                       "--control-port", str(shell.control_port)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "icap_bytes" in payload


def test_unreachable_shell_exits_3_for_every_read_verb(capsys) -> None:
    import socket
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    for verb in ("ping", "version", "diag"):
        rc = cli_mod.main([verb, "--host", "127.0.0.1", "--control-port", str(port)])
        err = capsys.readouterr().err
        assert rc == 3, verb
        assert "cannot reach shell control channel" in err
        assert "Traceback" not in err
