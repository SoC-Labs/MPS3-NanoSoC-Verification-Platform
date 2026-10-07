"""fpgahub board-node plugin skeleton for the MPS3 nanoSoC platform tender.

Per IMPLEMENTATION_PLAN.md §0.2 ("Tender base on Pi 5 ... fpgahub node") the
tender is **not** a new framework — it's an fpgahub node (the same code that
already manages Pynq-Z2/ZC702 boards), extended with three actions specific
to this platform:

1. ``program_full`` — full JTAG bitstream program via **openFPGALoader**
   (not Vivado: the Pi 5 tender has no Vivado install; see
   IMPLEMENTATION_PLAN.md §0.2 "fpgahub node + openocd + openFPGALoader +
   xvcd"). Implemented below as a real :class:`fpgahub.program.ProgramPlugin`
   — this one genuinely fits fpgahub's existing dispatch shape.
2. ``sd_install`` / ``mcc_reboot`` — **do not reimplement**. These already
   exist, are hardware-proven (IMPLEMENTATION_PLAN.md §0.2), and are
   referenced (not duplicated) below:
   ``fpgahub.program_plugins.sd_install.SdInstallProgram`` and
   ``fpgahub.reset_plugins.mps3.Mps3MccReboot``
   (the fpgahub sources, reference-only, DO NOT MODIFY).
3. ``platform_deploy`` — overlay push+swap over Ethernet via
   ``pyverify`` (``host/pyverify/``), for the fast inner loop once the
   shell is networked (spec phase 3 onward). This does **not** fit
   fpgahub's ``ProgramPlugin`` ABC (see the docstring on
   :func:`platform_deploy` below for why) — it's exposed as a small CLI
   fpgahub can shell out to via a manifest ``Action``, plus a convenience
   Python wrapper.

Deployment: this module is meant to live somewhere ``fpgahub`` (and
``pydantic``) are already importable — e.g. dropped into the tender's
fpgahub checkout as an extra ``program_plugins`` module, or the tender venv
installing this repo's ``host/tender`` in editable mode alongside a normal
fpgahub install (see ``host/tender/README.md`` for the two options).
Imports below are intentionally **not** guarded with ``try/except
ImportError`` — mirroring the reference plugins
(``fpgahub/program_plugins/vivado_jtag.py`` et al.) byte-for-byte — because
guarding would just delay a real ``ModuleNotFoundError`` to a confusing
runtime point instead of failing loudly at import time, exactly as the
reference plugins do.

Naming collision to flag for A6: fpgahub already has a "bitstream header"
(``fpgahub.bitstream.BitstreamHeader`` — a Xilinx ``.bit`` ASCII-header
parse: design name/part/date/length) and an "overlay" concept
(``ProgramPlugin.accepts_overlay`` / ``pynq_overlay.py`` — a PYNQ
device-tree ``.dtbo``). Neither is the same thing as this platform's
"bitstream header" (``pusher.push.BitstreamHeader`` — the ICAP partial
framing: magic/kind/static_id/rm_id/len_words/crc32) or "overlay"
(``overlay-manifest.md`` — the clearing+partial+manifest triple). The
identical names are a real source of confusion across the two codebases;
none of the plugin code below re-imports fpgahub's versions under the same
bare name to keep the distinction visible.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from fpgahub.bitstream import BitstreamHeader as FpgahubBitstreamHeader
from fpgahub.config import Board
from fpgahub.program import ProgramError, ProgramResult, register

if TYPE_CHECKING:
    from fpgahub.daemon import DaemonContext

log = logging.getLogger("fpgahub.program.mps3_platform")


# --------------------------------------------------------------------------- #
# 1. program_full — full JTAG program via openFPGALoader
# --------------------------------------------------------------------------- #


def _is_local_dev_host(dev_host: str) -> bool:
    """Verbatim copy of the helper every fpgahub program/reset/debug plugin
    carries privately (see ``fpgahub.program_plugins.vivado_jtag``,
    ``fpgahub.debug_plugins.xvc_jtag_openocd``) — kept as a local copy here
    for the same stated reason: avoid coupling this module to another
    plugin's internals."""
    if not dev_host:
        return False
    host = dev_host.rpartition("@")[2]
    host = host.split(":", 1)[0]
    if not host or host == "localhost":
        return True
    try:
        local_short = socket.gethostname()
    except OSError:
        local_short = ""
    try:
        local_fqdn = socket.getfqdn()
    except OSError:
        local_fqdn = ""
    if host in {local_short, local_short.partition(".")[0], local_fqdn}:
        return True
    try:
        host_ips = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except (socket.gaierror, OSError):
        return False
    try:
        local_ips = {ai[4][0] for ai in socket.getaddrinfo(local_short, None)}
    except (socket.gaierror, OSError):
        local_ips = set()
    return bool(host_ips & local_ips)


class ProgramFullOpenFpgaLoaderParams(BaseModel):
    """Params for the ``program_full`` method.

    Flag for A6/A2: exact openFPGALoader flag names below (``--board``,
    ``--cable``, ``--freq``, ``--serial``) should be confirmed against the
    version pinned on the tender image once Phase 0.3 (A2's board-checks
    workstream) actually runs ``openFPGALoader --detect`` against the
    KU115 — this params shape is my best-effort mirror of the CLI as
    documented upstream, not yet exercised against real hardware.
    """

    model_config = ConfigDict(extra="forbid")
    # openFPGALoader binary on the executing host (the tender Pi, almost
    # always — see dev_host note below).
    openfpgaloader: str = "openFPGALoader"
    # `--board <name>` — openFPGALoader's built-in board profile, if MPS3
    # ever gets one upstream; usually unset in favour of an explicit cable.
    board_name: str | None = None
    # `--cable <name>` — JTAG cable/adapter driver (e.g. "ft2232" for the
    # tender's FTDI-JTAG path per IMPLEMENTATION_PLAN.md §0.2/1.2).
    cable: str | None = "ft2232"
    # `--serial <id>` — pin a specific FTDI device when the tender has more
    # than one attached (multi-board Pi hub topology, D1).
    ftdi_serial: str | None = None
    # `--freq <hz>` — JTAG clock; KU115 SSI caps bootstrap JTAG at 20 MHz
    # (IMPLEMENTATION_PLAN.md C5) — default conservative.
    freq_hz: int | None = 10_000_000
    # Extra ssh options, matching the vivado_jtag/xvc_jtag_openocd default
    # (-oBatchMode=yes keeps a password prompt from wedging the daemon).
    ssh_opts: list[str] = Field(
        default_factory=lambda: [
            "-oBatchMode=yes", "-oStrictHostKeyChecking=accept-new",
        ],
    )
    timeout_s: float = 120.0


RunnerFn = Callable[
    [list[str], Path | None, float], Awaitable[tuple[int, str, str]],
]


async def _default_runner(
    argv: list[str], cwd: Path | None, timeout: float,
) -> tuple[int, str, str]:
    """Real subprocess runner (async, mirrors vivado_jtag's shape).
    Genuinely correct as written; simply never exercised in this task
    because there is no board/openFPGALoader available to run it against.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProgramError(f"program_full: subprocess timed out after {timeout:.0f}s")
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", "replace"),
        stderr_b.decode("utf-8", "replace"),
    )


def _openfpgaloader_argv(
    params: ProgramFullOpenFpgaLoaderParams, bitstream: Path,
) -> list[str]:
    argv = [params.openfpgaloader]
    if params.board_name:
        argv += ["--board", params.board_name]
    if params.cable:
        argv += ["--cable", params.cable]
    if params.ftdi_serial:
        argv += ["--serial", params.ftdi_serial]
    if params.freq_hz:
        argv += ["--freq", str(params.freq_hz)]
    argv += [str(bitstream)]
    return argv


@dataclass
class ProgramFullOpenFpgaLoader:
    """``program_full`` — full JTAG bitstream program via openFPGALoader.

    Mirrors ``fpgahub.program_plugins.vivado_jtag.VivadoJtag`` (reference,
    do not modify) but targets openFPGALoader instead of a Vivado
    hw_server, since the Pi-5 tender doesn't run Vivado
    (IMPLEMENTATION_PLAN.md §0.2). Used for the shell `.bit` (full
    reconfiguration — Phase 0/1 bring-up and JTAG recovery, spec §16 "First
    bring-up needs the physical CoreSight probe" / IMPLEMENTATION_PLAN.md
    Phase 3.1 "retire JTAG path to fallback/recovery").
    """

    id: str = "program_full"
    description: str = "Full JTAG bitstream program via openFPGALoader"
    required_capabilities: tuple[str, ...] = ("jtag_ftdi",)
    params_schema: type[BaseModel] = ProgramFullOpenFpgaLoaderParams
    accepts_overlay: bool = False
    runner: RunnerFn = field(default=_default_runner)

    async def execute(
        self,
        *,
        board: Board,
        board_name: str,
        bitstream: Path,
        header: FpgahubBitstreamHeader,
        fingerprint: str,
        overlay: Path | None,
        params: ProgramFullOpenFpgaLoaderParams,
        ctx: "DaemonContext",
        run_id: str,
    ) -> ProgramResult:
        dev_host = board.host.dev_host
        if dev_host and _is_local_dev_host(dev_host):
            log.info(
                "program_full: dev_host %r resolves to localhost; running "
                "locally instead of SSHing into self", dev_host,
            )
            dev_host = None

        inner_argv = _openfpgaloader_argv(params, bitstream)

        if dev_host:
            remote_cmd = " ".join(shlex.quote(a) for a in inner_argv)
            argv = ["ssh", *params.ssh_opts, dev_host, remote_cmd]
            where = f"ssh {dev_host}"
            cwd = None
        else:
            argv = inner_argv
            where = "local"
            cwd = Path("/tmp")

        log.info(
            "program_full: programming %s on %s (board=%s, cable=%s)",
            bitstream.name, where, board_name, params.cable,
        )
        try:
            rc, stdout, stderr = await self.runner(argv, cwd, params.timeout_s)
        except ProgramError:
            raise
        except Exception as exc:
            raise ProgramError(f"program_full: exec failed: {exc}") from exc

        ok = rc == 0
        msg = (
            f"programmed {bitstream.name} via {where} "
            f"(part={header.part}, sha256={fingerprint[:12]}…)"
            if ok else f"openFPGALoader exited rc={rc}"
        )
        return ProgramResult(ok=ok, message=msg, stdout=stdout, stderr=stderr, exit_code=rc)


register(ProgramFullOpenFpgaLoader())


# --------------------------------------------------------------------------- #
# 2. sd_install / mcc_reboot — reference existing plugins, don't reimplement
# --------------------------------------------------------------------------- #
#
# Both already exist and are hardware-proven (IMPLEMENTATION_PLAN.md §0.2):
#
#   fpgahub.program_plugins.sd_install.SdInstallProgram   (id="sd_install")
#   fpgahub.reset_plugins.mps3.Mps3MccReboot              (id="mps3_mcc_reboot")
#
# Both self-register via `fpgahub.program_plugins`/`fpgahub.reset_plugins`
# package `__init__.py` imports already, so nothing needs registering here
# — the MPS3 board config just needs to *reference* them by method id. A
# worked example (values illustrative; confirm the real USB VID/PID and
# SD path against the actual MPS3 config-SD layout, ARCHITECTURE_SPEC.md
# §3 "images.txt under MB/HBI0309C/<AN>/"):
#
#   [boards.mps3_nanosoc_01.program.sd]
#   method = "sd_install"
#   params = { block_device_vid = "0d28", block_device_pid = "0204",
#              dest = "MB/HBI0309C/Nanosoc/nanosoc.bit" }
#
#   [boards.mps3_nanosoc_01.reset.default]
#   method  = "mps3_mcc_reboot"
#   confirm = true
#   params  = { interface_number = 0 }
#
# --------------------------------------------------------------------------- #
# 3. platform_deploy — overlay push+swap via pyverify
# --------------------------------------------------------------------------- #
#
# Does NOT fit fpgahub.program.ProgramPlugin: the dispatcher parses the
# bitstream's Xilinx `.bit` ASCII header (design name/part/date/length) and
# SHA256-fingerprints it *before* calling the plugin (fpgahub.program's
# module docstring, point 1) — appropriate for "program a literal .bit onto
# a JTAG/SD target", not for "push a {clearing.bin, partial.bin,
# manifest.json} triple over Ethernet and ask the shell to swap it in".
# Forcing platform_deploy through that ABC would mean either fpgahub grows
# a bitstream-kind switch in its dispatcher (a real fpgahub-side change,
# out of this repo's "DO NOT modify fpgahub" scope) or this plugin lying
# about the artefact it hands over. Flag for A6: is a new fpgahub dispatch
# verb (or a `program.*` opt-out of the pre-parse) worth adding upstream,
# or is the manifest-Action route below sufficient for v1?
#
# v1 answer: expose a CLI (`pyverify.cli deploy`, see host/pyverify/) that
# fpgahub's existing generic manifest-Action runner (fpgahub.actions,
# reference-only) can already shell out to, no fpgahub code change needed:
#
#   [[actions.platform_deploy]]
#   id  = "platform_deploy"
#   run = "python -m pyverify.cli deploy --host {network.board_ip} --overlay overlay/{param.rm}"
#
# `run_platform_deploy` below is the convenience Python-side equivalent of
# that manifest Action, for callers driving fpgahub programmatically
# (fpgahub_sdk) instead of via `fpgahub actions run`.


@dataclass(frozen=True)
class PlatformDeployResult:
    ok: bool
    rm_id: str = ""
    verified: bool = False
    message: str = ""


async def _default_deploy_runner(
    argv: list[str], timeout: float,
) -> tuple[int, str, str]:
    """Real asyncio subprocess runner for :func:`run_platform_deploy`
    (mirrors :func:`_default_runner` above; two-arg signature because the
    deploy CLI never needs a cwd)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProgramError(
            f"platform_deploy: `pyverify.cli deploy` timed out after {timeout:.0f}s"
        )
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", "replace"),
        stderr_b.decode("utf-8", "replace"),
    )


async def run_platform_deploy(
    *, board: Board, overlay_dir: Path, rm_slot: int = 0,
    runner: Callable[[list[str], float], Awaitable[tuple[int, str, str]]] | None = None,
) -> PlatformDeployResult:
    """Convenience wrapper: shell out to ``pyverify.cli deploy`` against
    ``board.network.board_ip`` (the shell's static/DHCP IP, ARCHITECTURE_SPEC.md
    §3 "default static 192.168.10.101").

    The default ``runner`` is a real asyncio subprocess launch
    (:func:`_default_deploy_runner`); tests inject a fake. The CLI it
    invokes (``host/pyverify/pyverify/cli.py``) does the full
    validate/push/swap sequencing internally (``pyverify.swap.
    SwapOrchestrator`` + ``pyverify.pusher.BitstreamPusher``) and reports
    failures as clean nonzero exits (see its docstring's exit-code table),
    which surface here as ``PlatformDeployResult(ok=False, ...)``.
    ``pyverify`` must be importable by the interpreter running this
    plugin (e.g. ``pip install -e host/pyverify`` in the tender venv — see
    host/tender/README.md).
    """
    # ``sys.executable``, not a bare ``"python"``: this wrapper runs inside
    # the tender daemon's interpreter (the venv where pyverify was
    # pip-installed per the docstring above), and a bare "python" would
    # resolve against PATH — on a systemd-launched daemon that can be a
    # system python without pyverify installed. The manifest-Action example
    # in the module comment above keeps "python" because it runs in the
    # operator-authored manifest's shell context, not this interpreter.
    # (Flagged by the W-PUSH agent; asserted in
    # host/pyverify/tests/test_tender_plugin.py.)
    argv = [
        sys.executable, "-m", "pyverify.cli", "deploy",
        "--host", str(board.network.board_ip),
        "--overlay", str(overlay_dir),
        "--rm-slot", str(rm_slot),
    ]
    if runner is None:
        runner = _default_deploy_runner
    rc, stdout, stderr = await runner(argv, 60.0)
    ok = rc == 0
    return PlatformDeployResult(
        ok=ok,
        message=stdout if ok else f"deploy failed rc={rc}: {stderr or stdout}",
    )
