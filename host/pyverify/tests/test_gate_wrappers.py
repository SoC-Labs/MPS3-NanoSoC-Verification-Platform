"""The board gates are wrappers, and a wrapper's contract is its CLI.

``scripts/harness_gates/swap_check.py`` / ``ping_check.py`` /
``scripts/mps3_push.py`` are driven by ``scripts/harness_regression.sh`` and by
the silicon workflows, which pin their **flags, their stdout shapes and their
exit codes** — not their internals. This file holds those three things while
the internals move onto pyverify.

Board-free: ``--dry-run`` and ``--help`` only, plus one end-to-end swap against
:class:`~pyverify.testing.fakeshell.FakeShell` on loopback.
"""
from __future__ import annotations

import json
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from pyverify.fielded import repo_root

_ROOT = repo_root()
_SWAP_CHECK = _ROOT / "scripts" / "harness_gates" / "swap_check.py"
_PING_CHECK = _ROOT / "scripts" / "harness_gates" / "ping_check.py"
_PUSH = _ROOT / "scripts" / "mps3_push.py"


def _run(argv, **kw):
    env = dict(kw.pop("env", {}))
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("PYVERIFY_DIR", str(_ROOT / "host" / "pyverify"))
    env.setdefault("HOME", str(Path.home()))
    return subprocess.run([sys.executable] + [str(a) for a in argv],
                          capture_output=True, text=True, timeout=120, env=env, **kw)


# --------------------------------------------------------------------------- #
# swap_check.py — flags, exit codes, and the new --dry-run
# --------------------------------------------------------------------------- #


def test_swap_check_keeps_its_flags() -> None:
    out = _run([_SWAP_CHECK, "--help"]).stdout
    for flag in ("--rm", "--expect-rm-id", "--prod", "--repo", "--board",
                 "--via-hub", "--timeout"):
        assert flag in out, "swap_check.py lost %s -- harness_regression pins it" % flag


def test_swap_check_dry_run_prints_the_exact_pyverify_command(tmp_path) -> None:
    """A gate you cannot inspect before a board window is a gate you run blind.
    --dry-run must print the command it WOULD run, exactly."""
    res = _run([_SWAP_CHECK, "--rm", "regdemo_b", "--expect-rm-id", "0x010000B2",
                "--prod", str(tmp_path), "--dry-run"])
    assert res.returncode == 0, res.stderr
    line = [l for l in res.stdout.splitlines() if l.strip().startswith("$ ")]
    assert line, "no `$ <command>` line in the dry run:\n" + res.stdout
    cmd = line[-1]
    assert "pyverify.cli" in cmd and "deploy" in cmd
    assert "--rm regdemo_b" in cmd
    assert str(tmp_path) in cmd
    # a regression gate must never rewrite the board's boot default (v0.13 persist)
    assert "--no-persist" in cmd
    # ... and it must not have touched a board
    assert "FAIL" not in res.stdout


def test_swap_check_dry_run_via_hub_shows_the_ssh_form(tmp_path) -> None:
    res = _run([_SWAP_CHECK, "--rm", "regdemo_b", "--expect-rm-id", "0x010000B2",
                "--prod", "/tmp/staged", "--via-hub", "hub.example", "--dry-run"])
    assert res.returncode == 0
    cmd = [l for l in res.stdout.splitlines() if l.strip().startswith("$ ")][-1]
    assert cmd.startswith("$ ssh") or " ssh " in cmd
    assert "hub.example" in cmd
    # the staged package dir must be named, or the hub-side python cannot import it
    assert "PYTHONPATH" in cmd
    assert "--no-persist" in cmd


def test_swap_check_has_no_dead_prod_default() -> None:
    """The old default named fpga/dfx/build_v3/prod -- a build dir that had
    been dead for two mints and is absent from a fresh clone."""
    body = _SWAP_CHECK.read_text()
    # naming it in prose is HISTORY and stays; a live default is the bug
    live = [l for l in body.splitlines()
            if "build_v3" in l and not l.lstrip().startswith(("#", "*"))
            and "``" not in l]
    assert not live, "a live fpga/dfx/build_v3 default survives:\n  " + "\n  ".join(live)
    assert "PROD_DEFAULT" not in body
    res = _run([_SWAP_CHECK, "--rm", "x", "--expect-rm-id", "0x1", "--dry-run"])
    assert res.returncode == 0
    assert "fielded" in res.stdout.lower(), \
        "the resolved default --prod must be the fielded artefact dir"


def test_swap_check_still_fails_loud_on_a_missing_pusher(tmp_path) -> None:
    """Exit code contract: anything that is not a verified swap is non-zero."""
    res = _run([_SWAP_CHECK, "--rm", "nope", "--expect-rm-id", "0x010000B2",
                "--prod", str(tmp_path), "--via-hub", "", "--timeout", "20"])
    assert res.returncode != 0
    assert "Traceback" not in res.stderr


@pytest.mark.parametrize("verdict,expected_rc", [
    ({"ok": True, "verified": True, "rm_id": "0x010000b2"}, 0),
    ({"ok": False, "verified": False, "rm_id": "0x0"}, 1),
    ({"ok": True, "verified": False, "rm_id": "0x010000b2"}, 1),
    ({"ok": True, "verified": True, "rm_id": "0x01000001"}, 1),
])
def test_swap_check_verdict_mapping(verdict, expected_rc) -> None:
    """ok alone does not prove the RP took the config (bug #5 ordering), and a
    swap that lands on the WRONG RM is the escape this gate exists for."""
    sys.path.insert(0, str(_SWAP_CHECK.parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location("swap_check_mod", _SWAP_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.judge(verdict, 0x010000B2)[0] == expected_rc


def test_swap_check_parses_both_result_shapes() -> None:
    """`pyverify deploy` prints a JSON object; the legacy staged pusher prints
    `swap response: {...}`. A hub that has only the old staged copy must keep
    working, so both are accepted."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("swap_check_mod2", _SWAP_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a = mod.parse_verdict('{"ok": true, "rm_id": "0x010000b2", "verified": true}\n')
    b = mod.parse_verdict('noise\nswap response: {"ok": true, "rm_id": "0x010000b2", '
                          '"verified": true}\nmore noise\n')
    assert a == b
    assert mod.parse_verdict("nothing parseable here") is None


# --------------------------------------------------------------------------- #
# ping_check.py — the two paths emit the same line
# --------------------------------------------------------------------------- #


def test_ping_check_keeps_the_dependency_free_fallback() -> None:
    """harness_regression pipes this FILE into the hub's python over ssh, where
    there is no repo to import from. The fallback must survive."""
    body = _PING_CHECK.read_text()
    assert "import socket" in body
    assert "<stdin>" in body, "the piped path must be detected, not assumed"


def test_ping_check_line_shape_is_unchanged() -> None:
    body = _PING_CHECK.read_text()
    assert 'shell_id=%s rm_id=%s' in body


def test_ping_check_against_a_fake_shell(tmp_path) -> None:
    from pyverify.testing.fakeshell import FakeShell
    fake = FakeShell(static_id=0xA1B2C3D4, boot_rm_id=0x010000B2,
                     control_port=0, tftp_port=0, raw_tcp_port=0,
                     uart0_port=0, uart1_port=0, swo_port=0)
    fake.start()
    try:
        res = _run([_PING_CHECK, "127.0.0.1", str(fake.control_port)])
    finally:
        fake.stop()
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "shell_id=0xa1b2c3d4 rm_id=0x010000b2"


# --------------------------------------------------------------------------- #
# mps3_push.py — flags kept, stdout lines kept
# --------------------------------------------------------------------------- #


def test_push_keeps_its_flags() -> None:
    out = _run([_PUSH, "--help"]).stdout
    for flag in ("--board", "--timeout", "--ping", "--prod", "--rm",
                 "--static-id", "--rm-id", "--rm-name", "--clearing", "--partial",
                 "--no-persist"):
        assert flag in out, "mps3_push.py lost %s" % flag


def test_push_end_to_end_against_a_fake_shell(tmp_path) -> None:
    """The whole wrapper, over real loopback sockets: prod dir -> pyverify
    pusher -> FakeShell -> the `swap response:` line swap_check reads."""
    from pyverify.testing.fakeshell import FakeShell

    clearing = b"\xc1" * 64
    partial = b"\xb2" * 128
    (tmp_path / "rm_clear.bin").write_bytes(clearing)
    (tmp_path / "rm.bin").write_bytes(partial)
    (tmp_path / "static_id.txt").write_text("0xA1B2C3D4\n")
    (tmp_path / "overlay_inputs.txt").write_text(
        "rm_regdemo_b regdemo_b 0x010000B2 %s %s\n"
        % (tmp_path / "rm.bin", tmp_path / "rm_clear.bin"))

    fake = FakeShell(static_id=0xA1B2C3D4, boot_rm_id=0,
                     known_rm_ids={"regdemo_b": 0x010000B2},
                     control_port=0, tftp_port=0, raw_tcp_port=0,
                     uart0_port=0, uart1_port=0, swo_port=0)
    fake.start()
    try:
        env = {"MPS3_PUSH_CTRL_PORT": str(fake.control_port),
               "MPS3_PUSH_STREAM_PORT": str(fake.raw_tcp_port)}
        res = _run([_PUSH, "--board", "127.0.0.1", "--prod", str(tmp_path),
                    "--rm", "regdemo_b", "--timeout", "30"], env=env)
    finally:
        fake.stop()
    assert res.returncode == 0, res.stdout + res.stderr
    resp = [l for l in res.stdout.splitlines() if l.startswith("swap response:")]
    assert resp, res.stdout
    payload = json.loads(resp[0].split(":", 1)[1])
    assert payload["ok"] is True and payload["verified"] is True
    assert int(payload["rm_id"], 16) == 0x010000B2
    assert res.stdout.startswith("before:")
    assert "after:" in res.stdout


@pytest.mark.parametrize("how,persisted", [
    ((), True),                                        # a human push: the D1 default
    (("--no-persist",), False),                        # the flag
    ("env", False),                                    # the automation form
])
def test_push_persists_by_default_and_opts_out(tmp_path, how, persisted) -> None:
    """net-protocol.md v0.13: mps3_push.py is a HUMAN tool and keeps the library
    default (commit the pair to the user microSD after the swap); --no-persist
    and MPS3_PUSH_PERSIST=0 opt out, and stdout's lines are unchanged either way."""
    from pyverify.testing.fakeshell import FakeShell

    clearing = b"\xc1" * 64
    partial = b"\xb2" * 128
    (tmp_path / "rm_clear.bin").write_bytes(clearing)
    (tmp_path / "rm.bin").write_bytes(partial)
    (tmp_path / "static_id.txt").write_text("0xA1B2C3D4\n")
    (tmp_path / "overlay_inputs.txt").write_text(
        "rm_regdemo_b regdemo_b 0x010000B2 %s %s\n"
        % (tmp_path / "rm.bin", tmp_path / "rm_clear.bin"))
    fake = FakeShell(static_id=0xA1B2C3D4, usd_card="da", features=("usd",),
                     control_port=0, tftp_port=0, raw_tcp_port=0,
                     uart0_port=0, uart1_port=0, swo_port=0)
    fake.start()
    try:
        env = {"MPS3_PUSH_CTRL_PORT": str(fake.control_port),
               "MPS3_PUSH_STREAM_PORT": str(fake.raw_tcp_port)}
        extra = []
        if how == "env":
            env["MPS3_PUSH_PERSIST"] = "0"
        else:
            extra = list(how)
        res = _run([_PUSH, "--board", "127.0.0.1", "--prod", str(tmp_path),
                    "--rm", "regdemo_b", "--timeout", "30", *extra], env=env)
    finally:
        fake.stop()
    assert res.returncode == 0, res.stdout + res.stderr
    assert fake.commits == ([("regdemo_b", "A")] if persisted else [])
    lines = res.stdout.splitlines()
    assert len(lines) == 4, lines                       # stdout's shape is unchanged
    for line, prefix in zip(lines, ("before:", "swap -> ", "swap response:", "after:")):
        assert line.startswith(prefix), lines
    assert ('"status": "committed"' in res.stderr) is persisted
    assert ("persist: off" in res.stderr) is (not persisted)


# --------------------------------------------------------------------------- #
# the shell shims — they must actually reach pyverify
# --------------------------------------------------------------------------- #

_BOARD_SH = _ROOT / "scripts" / "mps3_board.sh"
_ACQUIRE_SH = _ROOT / "scripts" / "mps3_lease_acquire.sh"


def _sh(argv):
    """Run a shim with NO hub configured, so it stops at the env seam instead
    of reaching for a board."""
    import os
    env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin",
        "HOME": str(Path.home()),
        # the shims honour $PYTHON; pin it to the interpreter running the tests
        # so a stale /usr/bin/python3 cannot make this look like a shim bug
        "PYTHON": sys.executable,
    }
    return subprocess.run(["bash"] + [str(a) for a in argv],
                          capture_output=True, text=True, timeout=60, env=env)


@pytest.mark.parametrize("verb", ["status", "preflight"])
def test_board_shim_reaches_pyverify(verb) -> None:
    """The first version of this shim wrote `exec pv lease ...` where `pv` was a
    shell FUNCTION -- bash cannot exec a function, so every verb died with
    `exec: pv: not found` and rc=127 without ever reaching pyverify. Pin it:
    the no-hub message is proof the CLI ran."""
    res = _sh([_BOARD_SH, verb])
    assert res.returncode == 3, res.stdout + res.stderr
    assert "MPS3_HUB" in res.stderr
    assert "not found" not in res.stderr


def test_board_shim_still_requires_a_token_to_release() -> None:
    res = _sh([_BOARD_SH, "release"])
    assert res.returncode != 0
    assert "token" in res.stderr


def test_acquire_shim_reaches_pyverify() -> None:
    res = _sh([_ACQUIRE_SH, "some-holder"])
    assert res.returncode == 3
    assert "MPS3_HUB" in res.stderr
    assert res.stdout == "", "no half-token on stdout when there is no hub"
