"""Tests for pyverify.swd — the high-level SWD debug API.

**What these cover (all board-free):** the *plumbing* — OpenOCD ``-c`` batch
construction, the ssh hub-relay wrapping, per-op cfg selection (bare vs
+cortex_m), the memory-map addressing (IMEM base, mww word stride), and the
OpenOCD-output parsers — driven either by ``dry_run`` (argv only) or by a
fake runner returning *canned* OpenOCD text.

**What still needs a board (NOT covered here):** the real SWD transaction —
whether a live shell on ``<hub-host>`` actually answers 6920, whether the
DAP is out of reset (``RESET_CTRL=0x7``), and whether an image really lands
in IMEM. Those are the on-silicon rungs in ``docs/SWD_BRINGUP_PLAN.md`` §3;
there is no way to exercise them without hardware, and this suite never
opens a socket or spawns OpenOCD against the board.
"""
from __future__ import annotations

import subprocess

import pytest

from pyverify.swd import (
    IMEM_BASE,
    SCS_CPUID,
    SwdConfig,
    SwdDebugger,
    SwdError,
    parse_downloaded_bytes,
    parse_dpidr,
    parse_mdw,
    parse_reg,
)


class FakeRunner:
    """Records the (argv, timeout_s) handed to it; returns scripted output.
    Same shape as tests/test_debug_launch.py's FakeRunner."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: "list[dict]" = []
        self._result = (returncode, stdout, stderr)

    def __call__(self, argv, *, timeout_s, **kw):
        self.calls.append({"argv": list(argv), "timeout_s": timeout_s})
        rc, out, err = self._result
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


# --------------------------------------------------------------------------- #
# Parsers (pure, the load-bearing board-free logic)
# --------------------------------------------------------------------------- #


def test_parse_dpidr_pulls_value_from_init_log() -> None:
    log = "Info : SWD DPIDR 0x0bb11477\nInfo : Cortex-M0 r0p0 detected\n"
    assert parse_dpidr(log) == 0x0BB11477


def test_parse_dpidr_missing_raises() -> None:
    with pytest.raises(SwdError):
        parse_dpidr("Error: unable to connect; Connection refused\n")


def test_parse_reg_pc() -> None:
    assert parse_reg("pc (/32): 0x00000378", "pc") == 0x378


def test_parse_reg_missing_raises() -> None:
    with pytest.raises(SwdError):
        parse_reg("r0 (/32): 0x00000001", "pc")


def test_parse_mdw_multiword_multiline() -> None:
    log = "0x10000000: 1800fc00 00000189 000001cd 000001cf\n0x10000010: 000001d1"
    assert parse_mdw(log) == [0x1800FC00, 0x189, 0x1CD, 0x1CF, 0x1D1]


def test_parse_mdw_cpuid() -> None:
    assert parse_mdw("0xe000ed00: 410cc200")[0] == 0x410CC200


def test_parse_downloaded_bytes_optional() -> None:
    assert parse_downloaded_bytes("downloaded 4096 bytes in 12.3s") == 4096
    assert parse_downloaded_bytes("no such line") is None


# --------------------------------------------------------------------------- #
# argv construction + hub relay (dry_run: no runner, no board)
# --------------------------------------------------------------------------- #


def test_dpidr_argv_uses_bare_cfg_only_wrapped_in_ssh() -> None:
    # Pass the hub explicitly (DEFAULT_HUB is now env-supplied via MPS3_HUB and
    # is None when unset -> local, no ssh wrap); this exercises the ssh-relay
    # argv shape host-agnostically.
    argv = SwdDebugger(hub="hub.example", dry_run=True).dpidr()
    assert argv[0] == "ssh"
    assert "hub.example" in argv
    joined = " ".join(argv)
    # bare cfg present, cortex_m cfg absent (DPIDR needs no target)
    assert "swd_remote_bitbang.cfg" in joined
    assert "swd_cortex_m.cfg" not in joined
    # the proven op batch shape
    assert "-c init" in joined and "-c shutdown" in joined
    assert "set SHELL_HOST 192.168.10.101" in joined


def test_target_ops_add_cortex_m_cfg_in_order() -> None:
    # hub=None gives the flat OpenOCD argv (ops are separate -c elements).
    argv = SwdDebugger(dry_run=True, hub=None).read_pc()
    joined = " ".join(argv)
    assert "swd_remote_bitbang.cfg" in joined
    assert "swd_cortex_m.cfg" in joined
    # bare cfg must precede the cortex_m cfg (order matters — USER_GUIDE §6)
    assert joined.index("swd_remote_bitbang.cfg") < joined.index("swd_cortex_m.cfg")
    assert "halt" in argv and "reg pc" in argv


def test_hub_none_runs_locally_no_ssh() -> None:
    argv = SwdDebugger(dry_run=True, hub=None).dpidr()
    assert argv[0] == "openocd"
    assert "ssh" not in argv


def test_custom_hub_and_shell_host_thread_through() -> None:
    argv = SwdDebugger(dry_run=True, hub="other-host", shell_host="10.0.0.9").dpidr()
    assert "other-host" in argv
    assert "set SHELL_HOST 10.0.0.9" in " ".join(argv)


def test_config_and_kwargs_are_mutually_exclusive() -> None:
    with pytest.raises(TypeError):
        SwdDebugger(SwdConfig(), hub=None)


def test_load_image_defaults_to_imem_base_and_halts_first() -> None:
    argv = SwdDebugger(dry_run=True, hub=None).load_image("firmware/app.bin")
    assert "halt" in argv
    assert f"load_image firmware/app.bin 0x{IMEM_BASE:08x}" in argv
    assert "load_image firmware/app.bin 0x10000000" in argv  # IMEM base, spelled out


def test_load_image_elf_mode_omits_address() -> None:
    argv = SwdDebugger(dry_run=True, hub=None).load_image("app.elf", addr=None)
    assert "load_image app.elf" in argv  # no trailing address


def test_load_image_reset_run_appended() -> None:
    argv = SwdDebugger(dry_run=True, hub=None).load_image("a.bin", reset="run")
    assert "reset run" in argv


def test_load_image_bad_reset_rejected() -> None:
    with pytest.raises(ValueError):
        SwdDebugger(dry_run=True).load_image("a.bin", reset="nope")


def test_write_mem_increments_address_per_word() -> None:
    argv = SwdDebugger(dry_run=True, hub=None).write_mem(IMEM_BASE, [0xAAAA, 0xBBBB, 0xCCCC])
    assert "mww 0x10000000 0x0000aaaa" in argv
    assert "mww 0x10000004 0x0000bbbb" in argv
    assert "mww 0x10000008 0x0000cccc" in argv


def test_read_mem_builds_mdw_with_count() -> None:
    argv = SwdDebugger(dry_run=True, hub=None).read_mem(SCS_CPUID, 1)
    assert "mdw 0xe000ed00 1" in argv


# --------------------------------------------------------------------------- #
# runner seam: parse canned OpenOCD output (still no board)
# --------------------------------------------------------------------------- #


def test_dpidr_parses_runner_output() -> None:
    runner = FakeRunner(stdout="", stderr="Info : SWD DPIDR 0x0bb11477\n")
    assert SwdDebugger(runner=runner).dpidr() == 0x0BB11477
    # DPIDR needs only the bare cfg
    assert "swd_cortex_m.cfg" not in " ".join(runner.calls[0]["argv"])


def test_read_pc_parses_runner_output() -> None:
    runner = FakeRunner(stdout="pc (/32): 0x00000378\n")
    assert SwdDebugger(runner=runner).read_pc() == 0x378


def test_cpuid_reads_scs_and_parses() -> None:
    runner = FakeRunner(stdout="0xe000ed00: 410cc200\n")
    dbg = SwdDebugger(runner=runner, hub=None)  # flat argv for the token check
    assert dbg.cpuid() == 0x410CC200
    assert "mdw 0xe000ed00 1" in runner.calls[0]["argv"]


def test_read_mem_returns_requested_count() -> None:
    runner = FakeRunner(stdout="0x10000000: 1800fc00 00000189 000001cd 000001cf\n")
    words = SwdDebugger(runner=runner).read_mem(IMEM_BASE, 2)
    assert words == [0x1800FC00, 0x189]  # truncated to count


def test_nonzero_exit_raises_swderror_with_text() -> None:
    runner = FakeRunner(returncode=1, stderr="Error: couldn't connect to 6920")
    with pytest.raises(SwdError) as ei:
        SwdDebugger(runner=runner).halt()
    assert "couldn't connect" in str(ei.value)


def test_action_op_returns_completedprocess() -> None:
    runner = FakeRunner(returncode=0, stdout="")
    result = SwdDebugger(runner=runner, hub=None).resume()
    assert isinstance(result, subprocess.CompletedProcess)
    assert "resume" in runner.calls[0]["argv"]


def test_load_image_uses_roomier_default_timeout() -> None:
    """load can take minutes over remote_bitbang (§5) — the default timeout
    for load_image must be >= 300s even though the config default is 60s."""
    runner = FakeRunner()
    SwdDebugger(runner=runner).load_image("a.bin", reset="run")
    assert runner.calls[0]["timeout_s"] >= 300.0
