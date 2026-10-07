"""Debug-tool launch wrappers — OpenOCD (``remote_bitbang``) and Vivado XVC
(TCP 2542). ARCHITECTURE_SPEC.md §10 "Debug architecture".

The live DUT-debug port is JTAG on TCP 6921 (:data:`JTAG_REMOTE_BITBANG_PORT`,
``host/openocd/nanosoc_mps3_jtag.cfg``). SWD on 6920 is RETIRED: no shell image
has served it since the SWD→JTAG cutover (``0xCD74B6AE``; ``swd_server.c`` is
built only with ``LEGACY_SWD=1``). :class:`OpenOcdRemoteBitbangConfig` below
still builds the SWD argv and still defaults to 6920 -- a legacy front-end that
reaches nothing on a current shell.

Command-string / argv construction is real and matches net-protocol.md's
port map and §10.1's protocol-char table exactly. The launch functions
are real ``subprocess.run`` wrappers with an injectable *runner* seam
(:data:`SubprocessRunner`): tests pass a fake runner and assert on the
argv/stdin/timeout it receives (``tests/test_debug_launch.py``); real use
leaves ``runner=None`` and gets :func:`default_runner` — the same
capture-output/text/timeout/no-check shape as
``fpgahub.debug_plugins.swd_openocd`` / ``xvc_jtag_openocd``
(the fpgahub sources, reference-only). What still needs a
board is only the *target*: a live shell listening on 6921/2542 plus the
tool binaries installed on the calling host.
"""
from __future__ import annotations

import queue
import time
import shlex
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

__all__ = [
    "SWD_REMOTE_BITBANG_PORT",
    "JTAG_REMOTE_BITBANG_PORT",
    "XVC_PORT",
    "OpenOcdRemoteBitbangConfig",
    "XvcTarget",
    "XvcSession",
    "XvcSessionError",
    "LOCAL_HW_SERVER_URL",
    "SubprocessRunner",
    "default_runner",
    "launch_openocd",
    "launch_vivado_xvc_tcl",
]

# net-protocol.md port map.
#: RETIRED (net-protocol.md port map): nothing listens on 6920 on any shell since
#: the SWD→JTAG cutover. Kept for the legacy SWD argv builder and its tests.
SWD_REMOTE_BITBANG_PORT = 6920
#: The JTAG bridge's OpenOCD ``remote_bitbang`` port (net-protocol.md TCP 6921,
#: ``firmware/common/net_proto.h`` ``MPS3_PORT_JTAG``, served by
#: ``firmware/jtag_server/``). On the JTAG-bridge shell introduced by ``0xCD74B6AE``
#: and every shell since (docs/FIELDED_SHELL.md), this — not
#: 6920 — is the live DUT-debug path: the SWD→JTAG migration made ``swd_server``
#: dormant and put a SoC-400 SWJ-DP behind ``jtag_bb``. ``host/openocd/
#: nanosoc_mps3_jtag.cfg`` defaults ``RBB_PORT`` to this.
JTAG_REMOTE_BITBANG_PORT = 6921
XVC_PORT = 2542
#: The ONLY kind of hw_server an XVC session may use: a local one, started by
#: Vivado itself. NEVER the hub's ``<hub-host>:3121`` -- it is shared with other
#: people's boards and is 2025.2, which a 2024.1 hw_manager refuses
#: (docs/internal/BOARD_HANDOFF_NOTES.md:9-13).
LOCAL_HW_SERVER_URL = "localhost:3121"


def _is_loopback_url(url: str) -> bool:
    return url.rsplit(":", 1)[0] in ("localhost", "127.0.0.1", "::1", "[::1]")


@dataclass(frozen=True)
class OpenOcdRemoteBitbangConfig:
    """Builds the OpenOCD argv for the v1 "dumb pin-wiggler" SWD front-end
    (ARCHITECTURE_SPEC.md §10.1, D10=v1): OpenOCD's ``remote_bitbang``
    adapter driver, talking the byte protocol over TCP to the shell's SWD
    probe endpoint (net-protocol.md port 6920 -- RETIRED; no current shell
    serves it, see the module docstring; the live path is JTAG on 6921)::

        O SWDIO drive   o SWDIO release   c sample SWDIO
        d {CLK0,DIO0}   e {CLK0,DIO1}     f {CLK1,DIO0}   g {CLK1,DIO1}
        B/b LED   r/s/t/u trst/srst   Q quit

    Requires OpenOCD >= the Jan-2021 remote_bitbang-SWD merge (spec
    §16). The exact CLK/DIO bit order should be confirmed against the
    shell firmware's ``bitbang.c``-equivalent at bring-up (spec §10.1
    parenthetical) — this wrapper only builds the OpenOCD-side command;
    it has no opinion on the shell's internal wiring.
    """

    shell_host: str
    port: int = SWD_REMOTE_BITBANG_PORT
    openocd: str = "openocd"
    adapter_speed_khz: int = 1000
    #: Path OpenOCD's `source [find ...]` resolves. The nanosoc-on-MPS3
    #: single-core target cfg doesn't exist yet in this repo (that's
    #: A2/A3 territory — DFX/shell-FW); placeholder name kept here so the
    #: shape of the invocation is reviewable. Flag for A6: where does this
    #: file eventually live (fpga/dfx/? firmware/?), and does it get
    #: shipped alongside the overlay manifest so pyverify can point at the
    #: right one per-RM instead of a single hardcoded default?
    target_cfg: str = "target/nanosoc_mps3.cfg"
    extra_search_dirs: tuple[str, ...] = ()
    timeout_s: float = 30.0

    def command(self, op_cmd: str = "init") -> list[str]:
        """Build the full ``openocd -c ... -c ...`` argv.

        Every directive goes via ``-c`` (no temp cfg file needed), mirroring
        the pattern in the fpgahub reference plugins, wrapped in ``init ...
        shutdown`` so the op runs against a live target then cleanly exits.
        """
        argv = [self.openocd]
        for d in self.extra_search_dirs:
            argv += ["-c", f"add_script_search_dir {d}"]
        argv += [
            "-c", "adapter driver remote_bitbang",
            "-c", f"remote_bitbang host {self.shell_host}",
            "-c", f"remote_bitbang port {self.port}",
            "-c", "transport select swd",
            "-c", f"adapter speed {self.adapter_speed_khz}",
            "-c", f"source [find {self.target_cfg}]",
            "-c", "init",
            "-c", op_cmd,
            "-c", "shutdown",
        ]
        return argv

    def command_str(self, op_cmd: str = "init") -> str:
        """Shell-quoted string form of :meth:`command`, for logging or
        building an ssh-wrapped remote invocation (see fpgahub's
        ``vivado_jtag``/``xvc_jtag_openocd`` for the ssh-wrap pattern)."""
        return " ".join(shlex.quote(a) for a in self.command(op_cmd))


@dataclass(frozen=True)
class XvcTarget:
    """Vivado hw_manager's view of the shell's XVC server (net-protocol.md
    port 2542, ``firmware/xvc_server``), as Tcl.

    ``shell_host``/``port`` are where the XVC server is reached FROM THE
    MACHINE RUNNING VIVADO -- for the MPS3 that is the local end of an
    ``ssh -L 2542:192.168.10.101:2542 <hub>`` tunnel, i.e. ``localhost``.
    ``hw_server_url`` must be loopback (:data:`LOCAL_HW_SERVER_URL`); a
    remote one is refused at construction.

    The Tcl here is the same command sequence ``host/xvc/xvc_common.tcl``
    runs for the board scripts (``scripts/mps3_xvc_smoke.sh``,
    ``scripts/mps3_ila_proofs.sh``).
    """

    shell_host: str
    port: int = XVC_PORT
    hw_server_url: str = LOCAL_HW_SERVER_URL

    def __post_init__(self) -> None:
        if not _is_loopback_url(self.hw_server_url):
            raise ValueError(
                f"refusing hw_server {self.hw_server_url!r}: an XVC session uses a "
                "LOCAL hw_server only (never the hub's :3121 -- it is shared; "
                "docs/internal/BOARD_HANDOFF_NOTES.md:9-13)"
            )

    @property
    def xvc_url(self) -> str:
        return f"{self.shell_host}:{self.port}"

    def open_hw_target_tcl(self) -> str:
        """The one ``open_hw_target -xvc_url <host>:<port>`` line."""
        return f"open_hw_target -xvc_url {self.xvc_url}"

    def connect_tcl(self) -> str:
        """Open hw_manager and connect the LOCAL hw_server (once per session)."""
        return (
            "open_hw_manager\n"
            f"connect_hw_server -url {self.hw_server_url}\n"
        )

    def open_target_tcl(self, ltx_path: Optional[str] = None) -> str:
        """Open the XVC target, select its first device, load ``ltx_path``
        (if any) as PROBES.FILE, and refresh so the RM's debug cores enumerate."""
        tcl = (
            f"{self.open_hw_target_tcl()}\n"
            "current_hw_device [lindex [get_hw_devices] 0]\n"
        )
        if ltx_path:
            q = _tcl_quote(ltx_path)
            tcl += (
                f"set_property PROBES.FILE {q} [current_hw_device]\n"
                f"set_property FULL_PROBES.FILE {q} [current_hw_device]\n"
            )
        tcl += "refresh_hw_device [current_hw_device]\n"
        return tcl

    def close_target_tcl(self) -> str:
        """Close the XVC target -- releases the firmware's ONE XVC client slot."""
        return "close_hw_target\n"

    def disconnect_tcl(self) -> str:
        return "catch {close_hw_target}\ncatch {disconnect_hw_server}\ncatch {close_hw_manager}\n"

    def refresh_ila_tcl(self, ltx_path: str) -> str:
        """A COMPLETE batch session that re-attaches after a swap: hw_manager,
        the LOCAL hw_server, the XVC target, the new RM's probes file, a refresh,
        then close (so a batch run never leaves the one XVC slot held). This
        used to be three lines with no ``open_hw_manager``/``connect_hw_server``/
        ``-xvc_url`` (handover §2 F15) and could not have run."""
        return self.connect_tcl() + self.open_target_tcl(ltx_path) + self.disconnect_tcl()


def _tcl_quote(text: str) -> str:
    """Brace-quote for Tcl (paths with spaces); refuses unbalanced braces."""
    if "{" in text or "}" in text:
        raise ValueError(f"cannot Tcl-quote a value containing braces: {text!r}")
    return "{" + text + "}"


# --------------------------------------------------------------------------- #
# A persistent Vivado hw_manager session, driven over stdin/stdout
# --------------------------------------------------------------------------- #


class XvcSessionError(RuntimeError):
    """A Tcl command in the XVC session raised, or Vivado stopped answering."""


class TclProcess(Protocol):
    """What :class:`XvcSession` needs from the Vivado process: write a line of
    Tcl, read a line of output (``None`` on timeout), close. The real one is
    :class:`_VivadoTclProcess`; tests inject a fake."""

    def send(self, text: str) -> None: ...

    def readline(self, timeout: float) -> Optional[str]: ...

    def close(self) -> None: ...


class _VivadoTclProcess:
    """``vivado -mode tcl`` with stdin/stdout pipes and a reader thread."""

    def __init__(self, argv: list[str]) -> None:
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._q.put(line)
        self._q.put(None)

    def send(self, text: str) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(text)
        self._proc.stdin.flush()

    def readline(self, timeout: float) -> Optional[str]:
        try:
            line = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            raise XvcSessionError(f"Vivado exited (rc={self._proc.poll()})")
        return line

    def close(self) -> None:
        try:
            self.send("exit\n")
            self._proc.wait(timeout=30)
        except Exception:
            self._proc.kill()


_SENTINEL = "@@MPS3XVC"


class XvcSession:
    """A live Vivado hw_manager session on the shell's XVC target, with the
    lifecycle a DFX swap needs (handover §4.10):

    * :meth:`start` -- ``open_hw_manager`` + ``connect_hw_server`` (LOCAL).
    * :meth:`open_target` -- ``open_hw_target -xvc_url``, select the device,
      ``PROBES.FILE`` = the RM's ``.ltx``, ``refresh_hw_device``.
    * :meth:`close_target` -- ``close_hw_target``. **Called before every swap
      RPC** by :class:`pyverify.swap.SwapOrchestrator`: while a swap is gated
      the firmware STALLS a ``shift:`` and then runs it against the NEW RM
      (handover §2 F10, §8 trap 3), so no target may be open across a swap.
    * :meth:`reattach` -- reopen + reload the new RM's ``.ltx``: the real
      executor of :meth:`pyverify.swap.ReattachPlan.apply`'s ``ltx`` step. It
      does NOT reuse the hw_server across the swap (ILA-mint finding #19):
      ``disconnect_hw_server``, wait for the auto-launched daemon to exit, then
      ``connect_hw_server`` starts a fresh one (:mod:`pyverify.hwserver`).
    * :meth:`close` -- close the target, disconnect, exit Vivado.

    ``spawn`` is the injectable seam (like the launch wrappers' ``runner``):
    ``spawn(argv) -> TclProcess``. Default: a real ``vivado -mode tcl``.
    Every Tcl command is wrapped in ``catch`` and answered with a sentinel
    line, so a Tcl error becomes :class:`XvcSessionError`, never a hang.

    **The stale hw_server (finding #19, additive).** ``connect_hw_server``
    auto-launches ``hw_server -d -I20`` on :3121, which lingers 20 s after its
    last client; a session that connects inside that window reuses it, and
    after a swap its view of the XVC target is stale ("No devices detected").
    So before Vivado starts, and inside every :meth:`reattach`, the session
    waits (``stale_polls`` x ``stale_poll_s``, default 20 x 2 s -- the budget of
    ``scripts/mps3_ila_proofs.sh``'s ``wait_stale_hw_server``) for this user's
    daemon on the target's hw_server port to exit, then proceeds either way
    (a line in :attr:`hw_server_log`). ``proc_table`` is the process-table seam
    (:data:`pyverify.hwserver.ProcTable`); the guard runs against a real
    Vivado, or whenever a table is given -- an injected Vivado double with no
    table has no daemon to wait for. A hw_server you run YOURSELF on another
    port is yours to restart across a swap.
    """

    def __init__(
        self,
        target: XvcTarget,
        *,
        vivado: str = "vivado",
        spawn: Optional[Callable[[list[str]], TclProcess]] = None,
        timeout_s: float = 300.0,
        proc_table: Optional[Callable[[], Any]] = None,
        stale_polls: Optional[int] = None,
        stale_poll_s: Optional[float] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from . import hwserver as _hw
        self.target = target
        self.vivado = vivado
        self.timeout_s = timeout_s
        self._spawn = spawn if spawn is not None else _VivadoTclProcess
        self._proc_table = proc_table
        self._stale_guard = spawn is None or proc_table is not None
        self._stale_polls = _hw.STALE_POLLS if stale_polls is None else stale_polls
        self._stale_poll_s = _hw.STALE_POLL_S if stale_poll_s is None else stale_poll_s
        self._sleep = sleep
        #: One line per stale-hw_server wait that waited or gave up (for logs).
        self.hw_server_log: list[str] = []
        self._proc: Optional[TclProcess] = None
        self._seq = 0
        #: True while this session holds the XVC target (the firmware's one slot).
        self.target_open = False
        #: The probes file loaded on the current target, if any.
        self.ltx_path: Optional[str] = None
        #: Every Tcl script sent, in order (for logs and tests).
        self.history: list[str] = []

    # -- lifecycle ----------------------------------------------------------- #
    @property
    def started(self) -> bool:
        return self._proc is not None

    def start(self) -> "XvcSession":
        if self._proc is None:
            self.wait_stale_hw_server()
            self._proc = self._spawn(
                [self.vivado, "-mode", "tcl", "-nojournal", "-nolog"])
            self.eval(self.target.connect_tcl())
        return self

    def wait_stale_hw_server(self) -> list[int]:
        """Wait out this user's lingering auto-launched hw_server on the
        target's hw_server port (finding #19). Returns the pids still alive
        after the wait (``[]`` = none); the caller proceeds either way."""
        from . import hwserver as _hw
        port = _hw.hw_server_port(self.target.hw_server_url)
        if not self._stale_guard or port is None:
            return []
        return _hw.wait_stale_hw_server(
            port, polls=self._stale_polls, poll_s=self._stale_poll_s,
            table=self._proc_table, sleep=self._sleep, log=self.hw_server_log.append)

    def _fresh_hw_server(self) -> None:
        """Drop this session's hw_server and connect a fresh one: the daemon that
        served the target before a swap must not serve it after (finding #19)."""
        self.eval("catch {disconnect_hw_server}")
        self.wait_stale_hw_server()
        self.eval(f"connect_hw_server -url {self.target.hw_server_url}")

    def open_target(self, ltx_path: Optional[str] = None) -> str:
        """Open the XVC target and load ``ltx_path``; returns the device name."""
        self.start()
        if self.target_open:
            self.close_target()
        self.eval(self.target.open_target_tcl(ltx_path))
        self.target_open = True
        self.ltx_path = ltx_path
        return self.eval("current_hw_device").strip()

    def close_target(self) -> bool:
        """Close the target if open. Returns whether one was open."""
        if not (self._proc is not None and self.target_open):
            return False
        try:
            self.eval(self.target.close_target_tcl())
        finally:
            self.target_open = False
        return True

    def reattach(self, ltx_path: Optional[str]) -> dict[str, Any]:
        """Reopen the target and load the new RM's probes (after a swap), on a
        FRESH hw_server: close the target, disconnect, wait for the old daemon
        to exit, reconnect, open (finding #19)."""
        if self._proc is None:
            self.start()
        else:
            self.close_target()
            self._fresh_hw_server()
        device = self.open_target(ltx_path)
        ilas = self.eval(
            "llength [get_hw_ilas -quiet -of_objects [current_hw_device]]").strip()
        return {"target": self.target.xvc_url, "device": device,
                "ltx": ltx_path, "ilas": int(ilas) if ilas.isdigit() else ilas}

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self.eval(self.target.disconnect_tcl())
        except XvcSessionError:
            pass
        finally:
            self.target_open = False
            self._proc.close()
            self._proc = None

    def __enter__(self) -> "XvcSession":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the wire ------------------------------------------------------------ #
    def eval(self, script: str) -> str:
        """Run ``script`` in the session; return its result, raise on error."""
        if self._proc is None:
            raise XvcSessionError("session not started")
        self._seq += 1
        tag = f"{_SENTINEL}{self._seq}"
        body = script.strip()
        if body.count("{") != body.count("}"):
            raise XvcSessionError(f"unbalanced braces in Tcl: {body!r}")
        self.history.append(body)
        self._proc.send(
            f"if {{[catch {{{body}}} __mps3_r]}} "
            f"{{puts \"\\n{tag} ERR [string map {{\\n {{ }}}} $__mps3_r]\"}} "
            f"else {{puts \"\\n{tag} OK [string map {{\\n {{ }}}} $__mps3_r]\"}}\n"
        )
        deadline = time.monotonic() + self.timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise XvcSessionError(
                    f"no answer from Vivado within {self.timeout_s}s to: {body[:80]!r}")
            line = self._proc.readline(left)
            if line is None:
                continue
            line = line.rstrip("\n")
            if line.startswith(tag + " "):
                status, _, result = line[len(tag) + 1:].partition(" ")
                if status == "OK":
                    return result
                raise XvcSessionError(f"Tcl error in {body[:80]!r}: {result}")


# --------------------------------------------------------------------------- #
# Launch wrappers (real subprocess, injectable runner seam)
# --------------------------------------------------------------------------- #

#: The runner seam: ``(argv, timeout_s=..., input_text=...) -> CompletedProcess``.
#: Tests inject a fake; ``None`` selects :func:`default_runner`.
SubprocessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def default_runner(
    argv: list[str], *, timeout_s: float, input_text: "str | None" = None,
) -> "subprocess.CompletedProcess[str]":
    """Real runner: captured/teed text output, bounded by ``timeout_s``,
    ``check=False`` (callers inspect ``returncode`` — a failed ``init``
    against an unreachable shell is an *expected* outcome to report, not
    an exception). Mirrors the fpgahub debug plugins' subprocess shape.
    """
    return subprocess.run(
        argv, input=input_text, capture_output=True, text=True,
        timeout=timeout_s, check=False,
    )


def launch_openocd(
    config: OpenOcdRemoteBitbangConfig,
    op_cmd: str = "init",
    *,
    runner: Optional[SubprocessRunner] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run OpenOCD against the shell's remote_bitbang SWD endpoint.

    RETIRED endpoint: the argv selects ``transport select swd`` and defaults
    to port 6920, which no current shell serves. The live path is JTAG on
    6921 through ``host/openocd/nanosoc_mps3_jtag.cfg``, which this wrapper
    does not build.

    Builds the argv via :meth:`OpenOcdRemoteBitbangConfig.command` and
    hands it to ``runner`` (default :func:`default_runner` — a real
    ``subprocess.run``). Needs the ``openocd`` binary on the calling host
    and a live shell at ``config.shell_host:config.port`` to do anything
    useful; the wrapper itself has no other environmental dependency.
    """
    run = runner if runner is not None else default_runner
    return run(config.command(op_cmd), timeout_s=config.timeout_s)


def launch_vivado_xvc_tcl(
    target: XvcTarget,
    tcl_script: str,
    *,
    vivado: str = "vivado",
    timeout_s: float = 60.0,
    runner: Optional[SubprocessRunner] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run a Vivado batch Tcl script against the shell's XVC endpoint.

    ``tcl_script`` is expected to start with
    :meth:`XvcTarget.open_hw_target_tcl` (or :meth:`XvcTarget.refresh_ila_tcl`
    for a post-swap re-attach) — it is piped to Vivado on stdin
    (``-source /dev/stdin``), so no temp file is written. ``target`` names
    the XVC endpoint the script is *about*; the endpoint itself appears
    inside the script (via the ``XvcTarget`` helpers), not on the argv.
    """
    run = runner if runner is not None else default_runner
    argv = [vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", "/dev/stdin"]
    return run(argv, timeout_s=timeout_s, input_text=tcl_script)
