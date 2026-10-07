"""Board-free proofs for the ``python -m socket_harness`` CLI surface and its
exit-code discipline (copied from ``pyverify.cli``: clean one-line stderr, never
a traceback).

    0  ok
    1  endpoint declined / selftest failure
    2  usage error
    3  unreachable

The board-touching verbs (``reg``, ``probe``) are exercised WITHOUT a board:
``reg --dry-run`` prints the xsdb argv/tcl with no subprocess (a monkeypatched
``default_runner`` FAILS if invoked), and ``probe`` runs against a monkeypatched
``SocketHarness`` whose ``probe`` returns a scripted :class:`ProbeResult`.
"""
from __future__ import annotations

import json

import pytest

from socket_harness import cli
from socket_harness.harness import ProbeResult


def _run(argv: list) -> int:
    """Run the CLI, normalising argparse's ``SystemExit`` (usage -> code 2) into
    a returned int so every case is asserted uniformly. A returned int passes
    through unchanged."""
    try:
        rc = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0 if rc is None else rc


def _no_traceback(captured) -> None:
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


def test_endpoints_json_lists_control_and_dfxctl(capsys) -> None:
    rc = _run(["endpoints", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    # It is valid JSON (rc 0 + parses).
    parsed = json.loads(out)
    assert parsed is not None
    low = out.lower()
    # control endpoint on TCP 6900
    assert "control" in low
    assert "6900" in out
    # dfxctl CSR block at base 0x44A10000 (hex or int rendering both accepted)
    assert "dfxctl" in low
    assert "44a10000" in low or str(0x44A10000) in out


# --------------------------------------------------------------------------- #
# emit — regenerate host/console/* from the one registry
# --------------------------------------------------------------------------- #


def test_emit_ser2net_contains_all_three_console_ports(capsys) -> None:
    rc = _run(["emit", "ser2net"])
    assert rc == 0
    out = capsys.readouterr().out
    for port in ("6930", "6931", "6932"):
        assert port in out
    assert "/tmp/mps3-console/uart0" in out


def test_emit_socat_prints_pty_link_and_tcp_argv(capsys) -> None:
    rc = _run(["emit", "socat"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "socat" in out
    assert "PTY,link=/tmp/mps3-console/uart0,raw,echo=0" in out
    assert "TCP:" in out
    assert "6930" in out


# --------------------------------------------------------------------------- #
# reg --dry-run — xsdb preview, NO subprocess
# --------------------------------------------------------------------------- #


def _forbid_runner(monkeypatch) -> None:
    """Make any real xsdb subprocess a hard failure, so --dry-run is proven to
    be board-free (it must never reach the runner)."""

    def _boom(*args, **kwargs):
        raise AssertionError("default_runner was invoked during a --dry-run")

    import socket_harness.xsdb as xsdb_mod

    monkeypatch.setattr(xsdb_mod, "default_runner", _boom, raising=False)
    import pyverify.debug as debug_mod

    monkeypatch.setattr(debug_mod, "default_runner", _boom, raising=False)


def test_reg_read_dry_run_prints_mrd_tcl_without_subprocess(capsys, monkeypatch) -> None:
    _forbid_runner(monkeypatch)
    rc = _run(["reg", "dfxctl.RM_STATUS", "read", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    low = captured.out.lower()
    assert "mrd" in low
    # DFXCTL.RM_STATUS is 0x44A10000 + 0x14 == 0x44A10014.
    assert "44a10014" in low
    _no_traceback(captured)


def test_reg_write_dry_run_prints_mwr_with_encoded_value(capsys, monkeypatch) -> None:
    _forbid_runner(monkeypatch)
    rc = _run(["reg", "genchk.INJECT", "write", "giant=1", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    low = captured.out.lower()
    assert "mwr" in low
    # GENCHK.INJECT is 0x44A60000 + 0x04 == 0x44A60004; giant is bit 2 -> 0x4.
    assert "44a60004" in low
    assert "0x00000004" in low or "0x4" in low or "0x04" in low
    _no_traceback(captured)


# --------------------------------------------------------------------------- #
# probe — honest exit-code mapping (monkeypatched harness, no board)
# --------------------------------------------------------------------------- #


def _patch_probe(monkeypatch, status: str) -> None:
    class _FakeHarness:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def probe(self, name):
            return ProbeResult(name=name, status=status, detail="scripted")

    monkeypatch.setattr(cli, "SocketHarness", _FakeHarness)


def test_probe_unreachable_exits_3(capsys, monkeypatch) -> None:
    _patch_probe(monkeypatch, "unreachable")
    rc = _run(["probe", "control", "--host", "192.168.10.101"])
    assert rc == 3
    _no_traceback(capsys.readouterr())


def test_probe_declined_exits_1(capsys, monkeypatch) -> None:
    _patch_probe(monkeypatch, "declined")
    rc = _run(["probe", "control", "--host", "192.168.10.101"])
    assert rc == 1
    _no_traceback(capsys.readouterr())


def test_probe_ok_exits_0(capsys, monkeypatch) -> None:
    _patch_probe(monkeypatch, "ok")
    rc = _run(["probe", "control", "--host", "192.168.10.101"])
    assert rc == 0
    _no_traceback(capsys.readouterr())


# --------------------------------------------------------------------------- #
# usage error + selftest
# --------------------------------------------------------------------------- #


def test_no_subcommand_is_a_usage_error_exit_2(capsys) -> None:
    rc = _run([])
    assert rc == 2
    # argparse prints usage, not a Python traceback.
    _no_traceback(capsys.readouterr())


def test_selftest_runs_the_board_free_proofs_and_exits_0(capsys) -> None:
    rc = _run(["selftest"])
    captured = capsys.readouterr()
    assert rc == 0
    _no_traceback(captured)
