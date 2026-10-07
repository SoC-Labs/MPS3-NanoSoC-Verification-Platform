"""Tests for ``host/tender/fpgahub_mps3_plugin.py`` — first CI coverage
for the tender plugin (W-HOST-MISC, docs/NEXT_WAVE_PLAN.md).

The plugin is *not* part of the pyverify package: it lives in
``host/tender/`` and imports the real fpgahub package (reference checkout,
read-only, located by the ``FPGAHUB_SRC`` env var; unset or absent =>
every test here skips). Both are put on ``sys.path`` below, mirroring
the plugin's own deployment story ("live somewhere fpgahub is already
importable").

Skip policy (module-level, precise reasons): fpgahub requires Python
>= 3.11 (``pyproject.toml`` ``requires-python``; ``import tomllib``) and
``pydantic>=2.6``. When the interpreter running pytest can't import
them, every test here skips with the real underlying error instead of
failing — the rest of the pyverify suite is stdlib-only and unaffected.
To actually run these, use any Python 3.11+ interpreter with pydantic
and pytest installed, e.g.::

    python3.11 -m pytest -q host/pyverify/tests/test_tender_plugin.py

No test below shells out: ``execute()`` / ``run_platform_deploy`` are
driven through their injectable *runner* seams with fakes that record
the argv/cwd/timeout they were handed (same pattern as
``tests/test_debug_launch.py``).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Import plumbing: host/tender + the fpgahub checkout's src/ onto sys.path
# --------------------------------------------------------------------------- #

_HOST_DIR = Path(__file__).resolve().parents[2]        # .../host
_TENDER_DIR = _HOST_DIR / "tender"
_FPGAHUB_SRC = Path(
    os.environ.get("FPGAHUB_SRC", "fpgahub-src-not-configured")
)

for _p in (str(_TENDER_DIR), str(_FPGAHUB_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if sys.version_info < (3, 11):
    pytest.skip(
        "fpgahub requires Python >= 3.11 (pyproject requires-python; "
        "fpgahub.config imports the 3.11+ stdlib 'tomllib'); this "
        f"interpreter is {sys.version.split()[0]}. Run e.g. "
        "'python3.11 -m pytest host/pyverify/tests/test_tender_plugin.py' "
        "(needs pydantic>=2.6 + pytest installed for that interpreter).",
        allow_module_level=True,
    )

try:
    import pydantic  # noqa: F401  (fpgahub + the plugin both need it)
except ImportError as exc:
    pytest.skip(
        f"pydantic not importable in this interpreter ({exc}); fpgahub "
        "and the tender plugin require pydantic>=2.6",
        allow_module_level=True,
    )

if not _FPGAHUB_SRC.is_dir():
    pytest.skip(
        f"fpgahub checkout not found at {_FPGAHUB_SRC} (set FPGAHUB_SRC "
        "to the checkout's src/ directory)",
        allow_module_level=True,
    )

try:
    import fpgahub.program as fpgahub_program
    from fpgahub.bitstream import BitstreamHeader as FpgahubBitstreamHeader
    from fpgahub.config import Board, BoardHost, BoardNaming, BoardNetwork
    from fpgahub.program import ProgramError, ProgramPlugin, ProgramResult
except Exception as exc:  # pragma: no cover - env-dependent
    pytest.skip(
        f"fpgahub not importable from {_FPGAHUB_SRC}: {exc!r}",
        allow_module_level=True,
    )

import fpgahub_mps3_plugin as plugin  # noqa: E402  (host/tender/)


# --------------------------------------------------------------------------- #
# Shared fixtures/helpers
# --------------------------------------------------------------------------- #


def make_board(dev_host: str | None = None) -> Board:
    """Minimal valid fpgahub Board (required fields only, per
    fpgahub.config's pydantic models) with the platform's documented
    static IP (ARCHITECTURE_SPEC.md §3 / net-protocol.md)."""
    return Board(
        server="tender01",
        hub_path="1-1.1",
        # tty_symlink_dir is a bare subdirectory name under /dev/ (fpgahub
        # BoardNaming validator rejects '/'-containing values).
        naming=BoardNaming(tty_symlink_dir="mps3_01", net_name="mps3-01"),
        network=BoardNetwork(
            host_ip="192.168.10.2/24",
            board_ip="192.168.10.101",
            board_mac="02:00:00:00:00:01",
            hostname="mps3-shell",
        ),
        host=BoardHost(dev_host=dev_host),
    )


HEADER = FpgahubBitstreamHeader(
    design="shell_top", part="xcku115-flva1517-2-e",
    build_date="2026/07/06", build_time="12:00:00",
)


class FakeRunner:
    """Records every (argv, cwd, timeout) execute() hands the runner and
    replies with a scripted (rc, stdout, stderr)."""

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "",
                 exc: BaseException | None = None) -> None:
        self.rc, self.stdout, self.stderr, self.exc = rc, stdout, stderr, exc
        self.calls: list[tuple[list[str], Path | None, float]] = []

    async def __call__(self, argv: list[str], cwd: Path | None,
                       timeout: float) -> tuple[int, str, str]:
        self.calls.append((list(argv), cwd, timeout))
        if self.exc is not None:
            raise self.exc
        return (self.rc, self.stdout, self.stderr)


class FakeDeployRunner:
    """Two-arg runner shape for run_platform_deploy (argv, timeout)."""

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.calls: list[tuple[list[str], float]] = []

    async def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append((list(argv), timeout))
        return (self.rc, self.stdout, self.stderr)


def run(coro):
    return asyncio.run(coro)


def execute_kwargs(**overrides):
    """Full keyword set the fpgahub dispatcher passes to plugin.execute()."""
    kwargs = dict(
        board=make_board(),
        board_name="mps3_nanosoc_01",
        bitstream=Path("/srv/bitstreams/shell_top.bit"),
        header=HEADER,
        fingerprint="a" * 64,
        overlay=None,
        params=plugin.ProgramFullOpenFpgaLoaderParams(),
        ctx=object(),   # execute() never touches ctx; any object will do
        run_id="program-test0001",
    )
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------- #
# 1. ProgramPlugin ABC conformance — compared against the *real* fpgahub ABC
# --------------------------------------------------------------------------- #


def test_conforms_to_runtime_checkable_program_plugin_protocol() -> None:
    # fpgahub.program.ProgramPlugin is a @runtime_checkable Protocol; the
    # instance must satisfy it structurally, not by assumption.
    assert isinstance(plugin.ProgramFullOpenFpgaLoader(), ProgramPlugin)


def test_plugin_attribute_surface_matches_abc() -> None:
    p = plugin.ProgramFullOpenFpgaLoader()
    assert p.id == "program_full"
    assert isinstance(p.description, str) and p.description
    assert p.required_capabilities == ("jtag_ftdi",)
    # params_schema must be a pydantic model type (the dispatcher calls
    # .model_validate on it — fpgahub.program._validate_params).
    assert isinstance(p.params_schema, type)
    assert issubclass(p.params_schema, pydantic.BaseModel)
    # Our partials are not fpgahub "overlays" (.dtbo) — must refuse them
    # so dispatch_program errors on a stray --overlay.
    assert p.accepts_overlay is False


def test_execute_signature_matches_abc_exactly() -> None:
    abc_sig = inspect.signature(ProgramPlugin.execute)
    impl_sig = inspect.signature(plugin.ProgramFullOpenFpgaLoader.execute)

    def kwonly(sig: inspect.Signature) -> set[str]:
        return {
            n for n, prm in sig.parameters.items()
            if prm.kind is inspect.Parameter.KEYWORD_ONLY
        }

    # The dispatcher calls execute(board=..., ..., run_id=...) with every
    # argument by keyword — the keyword-only surface must match 1:1.
    assert kwonly(impl_sig) == kwonly(abc_sig)
    assert kwonly(impl_sig) == {
        "board", "board_name", "bitstream", "header", "fingerprint",
        "overlay", "params", "ctx", "run_id",
    }
    assert inspect.iscoroutinefunction(plugin.ProgramFullOpenFpgaLoader.execute)


def test_import_registers_plugin_in_fpgahub_registry() -> None:
    # The module calls register(...) at import time, like the reference
    # plugins; fpgahub.program.get() must resolve it.
    registered = fpgahub_program.get("program_full")
    assert isinstance(registered, plugin.ProgramFullOpenFpgaLoader)
    assert "program_full" in [p.id for p in fpgahub_program.list_all()]


# --------------------------------------------------------------------------- #
# 2. _openfpgaloader_argv construction matrix
# --------------------------------------------------------------------------- #

BIT = Path("/srv/bitstreams/shell_top.bit")


def _argv(**params) -> list[str]:
    return plugin._openfpgaloader_argv(
        plugin.ProgramFullOpenFpgaLoaderParams(**params), BIT,
    )


def test_argv_defaults() -> None:
    # Defaults: ft2232 cable, 10 MHz (KU115 bootstrap JTAG cap, plan C5),
    # no --board/--serial, bitstream last.
    assert _argv() == [
        "openFPGALoader", "--cable", "ft2232",
        "--freq", "10000000", str(BIT),
    ]


def test_argv_board_name_adds_board_flag() -> None:
    argv = _argv(board_name="mps3")
    assert argv[:3] == ["openFPGALoader", "--board", "mps3"]
    assert argv[-1] == str(BIT)


def test_argv_ftdi_serial_adds_serial_flag() -> None:
    argv = _argv(ftdi_serial="FT12345")
    i = argv.index("--serial")
    assert argv[i + 1] == "FT12345"


def test_argv_none_flags_are_omitted() -> None:
    argv = _argv(board_name=None, cable=None, ftdi_serial=None, freq_hz=None)
    assert argv == ["openFPGALoader", str(BIT)]


def test_argv_custom_binary_and_all_flags() -> None:
    argv = _argv(
        openfpgaloader="/opt/ofl/bin/openFPGALoader",
        board_name="mps3", cable="digilent", ftdi_serial="FTAAA1",
        freq_hz=20_000_000,
    )
    assert argv == [
        "/opt/ofl/bin/openFPGALoader",
        "--board", "mps3", "--cable", "digilent",
        "--serial", "FTAAA1", "--freq", "20000000", str(BIT),
    ]


def test_params_schema_forbids_unknown_keys() -> None:
    # model_config extra="forbid": a typo'd param must fail validation
    # (the dispatcher surfaces this as ProgramError before execute()).
    with pytest.raises(pydantic.ValidationError):
        plugin.ProgramFullOpenFpgaLoaderParams(cabel="ft2232")


# --------------------------------------------------------------------------- #
# 3. _is_local_dev_host
# --------------------------------------------------------------------------- #


def test_is_local_dev_host_empty_is_false() -> None:
    assert plugin._is_local_dev_host("") is False


@pytest.mark.parametrize("dev_host", [
    "localhost",
    "user@localhost",
    "user@localhost:22",
])
def test_is_local_dev_host_localhost_forms(dev_host: str) -> None:
    assert plugin._is_local_dev_host(dev_host) is True


def test_is_local_dev_host_own_hostname_is_true() -> None:
    import socket as socket_mod
    me = socket_mod.gethostname()
    assert plugin._is_local_dev_host(me) is True
    assert plugin._is_local_dev_host(f"user@{me}") is True


def test_is_local_dev_host_remote_ip_is_false() -> None:
    # TEST-NET-1 (RFC 5737): resolvable as a literal, never a local addr.
    assert plugin._is_local_dev_host("user@192.0.2.1") is False


def test_is_local_dev_host_unresolvable_is_false() -> None:
    # .invalid TLD (RFC 6761) never resolves -> getaddrinfo fails -> False.
    assert plugin._is_local_dev_host("user@no-such-host.invalid") is False


# --------------------------------------------------------------------------- #
# 4. execute() happy/fail paths (injected fake runner)
# --------------------------------------------------------------------------- #


def _make_plugin(runner: FakeRunner) -> plugin.ProgramFullOpenFpgaLoader:
    return plugin.ProgramFullOpenFpgaLoader(runner=runner)


def test_execute_local_happy_path() -> None:
    runner = FakeRunner(rc=0, stdout="Parse file OK", stderr="")
    p = _make_plugin(runner)
    result = run(p.execute(**execute_kwargs()))

    assert isinstance(result, ProgramResult)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout == "Parse file OK"
    assert "shell_top.bit" in result.message
    assert "local" in result.message          # no dev_host -> ran locally
    assert HEADER.part in result.message

    (argv, cwd, timeout), = runner.calls
    assert argv == plugin._openfpgaloader_argv(
        plugin.ProgramFullOpenFpgaLoaderParams(), BIT,
    )
    assert cwd == Path("/tmp")
    assert timeout == pytest.approx(120.0)


def test_execute_nonzero_rc_is_ok_false_not_exception() -> None:
    # Plugin-side failure convention (fpgahub.program.ProgramResult):
    # rc!=0 -> ok=False result, no raise; dispatcher emits program_failed.
    runner = FakeRunner(rc=2, stdout="", stderr="unable to open ftdi device")
    p = _make_plugin(runner)
    result = run(p.execute(**execute_kwargs()))
    assert result.ok is False
    assert result.exit_code == 2
    assert "rc=2" in result.message
    assert result.stderr == "unable to open ftdi device"


def test_execute_remote_dev_host_wraps_in_ssh() -> None:
    runner = FakeRunner(rc=0)
    p = _make_plugin(runner)
    result = run(p.execute(**execute_kwargs(
        board=make_board(dev_host="dev@192.0.2.1"),
    )))
    assert result.ok is True
    assert "ssh dev@192.0.2.1" in result.message

    (argv, cwd, _), = runner.calls
    assert argv[0] == "ssh"
    # ssh_opts defaults (BatchMode blocks password prompts wedging daemon)
    assert "-oBatchMode=yes" in argv
    assert "-oStrictHostKeyChecking=accept-new" in argv
    assert argv[argv.index("-oStrictHostKeyChecking=accept-new") + 1] == "dev@192.0.2.1"
    # Remote command is a single shlex-quoted string, and no local cwd.
    assert argv[-1] == "openFPGALoader --cable ft2232 --freq 10000000 " + str(BIT)
    assert cwd is None


def test_execute_localhost_dev_host_runs_locally() -> None:
    # ssh-into-self avoidance: dev_host resolving to this machine drops
    # the ssh wrap entirely.
    runner = FakeRunner(rc=0)
    p = _make_plugin(runner)
    run(p.execute(**execute_kwargs(board=make_board(dev_host="user@localhost"))))
    (argv, cwd, _), = runner.calls
    assert argv[0] == "openFPGALoader"
    assert cwd == Path("/tmp")


def test_execute_runner_generic_exception_wrapped_as_program_error() -> None:
    runner = FakeRunner(exc=FileNotFoundError("openFPGALoader: not found"))
    p = _make_plugin(runner)
    with pytest.raises(ProgramError, match="exec failed"):
        run(p.execute(**execute_kwargs()))


def test_execute_runner_program_error_passes_through_unwrapped() -> None:
    runner = FakeRunner(exc=ProgramError("program_full: subprocess timed out after 120s"))
    p = _make_plugin(runner)
    with pytest.raises(ProgramError, match="timed out"):
        run(p.execute(**execute_kwargs()))


# --------------------------------------------------------------------------- #
# 5. run_platform_deploy (injected runner; W-PUSH agent's two flags)
# --------------------------------------------------------------------------- #


def test_platform_deploy_argv_shape_uses_sys_executable() -> None:
    # W-PUSH flag #1: the argv must launch *this* interpreter
    # (sys.executable — the tender venv where pyverify is installed),
    # never a bare "python" resolved from PATH.
    runner = FakeDeployRunner(rc=0, stdout='{"ok": true}')
    result = run(plugin.run_platform_deploy(
        board=make_board(), overlay_dir=Path("/srv/overlay/nanosoc"),
        rm_slot=3, runner=runner,
    ))
    assert result.ok is True

    (argv, timeout), = runner.calls
    assert argv[0] == sys.executable
    assert argv[0] != "python"
    assert argv[1:4] == ["-m", "pyverify.cli", "deploy"]
    assert argv[argv.index("--host") + 1] == "192.168.10.101"
    assert argv[argv.index("--overlay") + 1] == "/srv/overlay/nanosoc"
    assert argv[argv.index("--rm-slot") + 1] == "3"
    assert timeout == pytest.approx(60.0)


def test_platform_deploy_happy_message_is_stdout() -> None:
    runner = FakeDeployRunner(rc=0, stdout='{"ok": true, "rm_id": "0x2"}')
    result = run(plugin.run_platform_deploy(
        board=make_board(), overlay_dir=Path("/srv/overlay/nanosoc"),
        runner=runner,
    ))
    assert result.ok is True
    assert result.message == '{"ok": true, "rm_id": "0x2"}'


def test_platform_deploy_failure_prefers_stderr() -> None:
    # W-PUSH flag #2: pyverify.cli's exit-code contract puts the one-line
    # failure reason on *stderr* — the failure message must prefer it
    # over whatever landed on stdout.
    runner = FakeDeployRunner(
        rc=3, stdout="partial progress noise",
        stderr="deploy: cannot reach shell control channel at 192.168.10.101:6900",
    )
    result = run(plugin.run_platform_deploy(
        board=make_board(), overlay_dir=Path("/srv/overlay/nanosoc"),
        runner=runner,
    ))
    assert result.ok is False
    assert "rc=3" in result.message
    assert "cannot reach shell control channel" in result.message
    assert "partial progress noise" not in result.message


def test_platform_deploy_failure_falls_back_to_stdout_when_stderr_empty() -> None:
    runner = FakeDeployRunner(rc=1, stdout="swap NAK from shell", stderr="")
    result = run(plugin.run_platform_deploy(
        board=make_board(), overlay_dir=Path("/srv/overlay/nanosoc"),
        runner=runner,
    ))
    assert result.ok is False
    assert "swap NAK from shell" in result.message


def test_platform_deploy_default_rm_slot_zero() -> None:
    runner = FakeDeployRunner(rc=0, stdout="{}")
    run(plugin.run_platform_deploy(
        board=make_board(), overlay_dir=Path("/srv/overlay/nanosoc"),
        runner=runner,
    ))
    (argv, _), = runner.calls
    assert argv[argv.index("--rm-slot") + 1] == "0"
