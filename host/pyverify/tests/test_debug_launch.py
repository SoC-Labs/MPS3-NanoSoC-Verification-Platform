"""Tests for pyverify.debug's launch wrappers.

The wrappers are real ``subprocess.run`` shells with an injectable runner
seam — the fake-runner tests pin down exactly what argv/stdin/timeout the
wrappers hand to the runner (no OpenOCD/Vivado needed), and two smoke
tests exercise the *real* ``default_runner`` against harmless stand-in
binaries (``echo``/``cat``) to prove the subprocess plumbing itself works.
"""
from __future__ import annotations

import subprocess

import pytest

from pyverify.debug import (
    SWD_REMOTE_BITBANG_PORT,
    OpenOcdRemoteBitbangConfig,
    XvcTarget,
    launch_openocd,
    launch_vivado_xvc_tcl,
)


class FakeRunner:
    """Records the wrapper -> runner handoff; returns a scripted result."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[dict] = []
        self._result_args = (returncode, stdout, stderr)

    def __call__(self, argv, *, timeout_s, input_text=None):
        self.calls.append(
            {"argv": list(argv), "timeout_s": timeout_s, "input_text": input_text}
        )
        rc, out, err = self._result_args
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


def test_launch_openocd_passes_config_argv_and_timeout() -> None:
    cfg = OpenOcdRemoteBitbangConfig(
        shell_host="192.168.10.101", adapter_speed_khz=500, timeout_s=12.5,
    )
    runner = FakeRunner(returncode=0, stdout="Info : DPIDR 0x0bc11477")
    result = launch_openocd(cfg, "dap info", runner=runner)

    assert result.returncode == 0
    assert "DPIDR" in result.stdout
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"] == cfg.command("dap info")
    assert call["timeout_s"] == pytest.approx(12.5)
    assert call["input_text"] is None
    # The argv itself must carry the remote_bitbang wiring (net-protocol.md 6920).
    joined = " ".join(call["argv"])
    assert "adapter driver remote_bitbang" in joined
    assert f"remote_bitbang port {SWD_REMOTE_BITBANG_PORT}" in joined
    assert "remote_bitbang host 192.168.10.101" in joined


def test_launch_openocd_reports_nonzero_exit_not_exception() -> None:
    """check=False semantics: a failed init (e.g. unreachable shell) comes
    back as a CompletedProcess for the caller to inspect, not a raise."""
    cfg = OpenOcdRemoteBitbangConfig(shell_host="192.168.10.101")
    runner = FakeRunner(returncode=1, stderr="Error: couldn't connect")
    result = launch_openocd(cfg, runner=runner)
    assert result.returncode == 1
    assert "couldn't connect" in result.stderr


def test_launch_vivado_pipes_tcl_via_stdin() -> None:
    target = XvcTarget(shell_host="192.168.10.101")
    tcl = target.refresh_ila_tcl("overlay/nanosoc/nanosoc.ltx")
    runner = FakeRunner()
    launch_vivado_xvc_tcl(target, tcl, vivado="vivado_lab", timeout_s=30.0, runner=runner)

    call = runner.calls[0]
    assert call["argv"] == [
        "vivado_lab", "-mode", "batch", "-nojournal", "-nolog", "-source", "/dev/stdin",
    ]
    assert call["timeout_s"] == pytest.approx(30.0)
    assert call["input_text"] == tcl
    assert "PROBES.FILE" in call["input_text"]


def test_default_runner_really_launches_openocd_argv() -> None:
    """Real-subprocess smoke: point the 'openocd' binary at ``echo`` so the
    default runner genuinely fork/execs and captures output."""
    cfg = OpenOcdRemoteBitbangConfig(shell_host="127.0.0.1", openocd="echo")
    result = launch_openocd(cfg, "init")
    assert result.returncode == 0
    assert "remote_bitbang" in result.stdout
    assert "transport select swd" in result.stdout


def test_default_runner_really_pipes_stdin_for_vivado() -> None:
    """Real-subprocess smoke for the stdin path: stand in ``vivado`` with
    ``true`` (which ignores the fixed ``-mode batch ...`` argv) and check
    the default runner delivers the Tcl on stdin and completes cleanly —
    the wrapper's fixed argv shape is already pinned by the fake-runner
    test above."""
    target = XvcTarget(shell_host="127.0.0.1")
    result = launch_vivado_xvc_tcl(
        target, target.open_hw_target_tcl(), vivado="true", timeout_s=10.0,
    )
    assert result.returncode == 0
