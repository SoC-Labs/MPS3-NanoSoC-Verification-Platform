"""``pyverify.lease`` — ONE fpgahub lease dialect for the shared MPS3.

The MPS3 is driven over two independent channels at once (Ethernet 6900/6910 and
JTAG via ``hw_server``), and they do not know about each other. A live 1.31 MB
partial-bitstream transfer has already been destroyed by an ``xsdb stop`` issued
from another context. The fpgahub lease is the coordination primitive for that —
**advisory** (``docs/internal/MPS3_BOARD_LEASE.md`` documents exactly which
channels are and are not gated: today, none), so it is honoured by convention and
the convention has to be cheap.

Before this module the dialect lived in two shell scripts that disagreed:

``scripts/mps3_board.sh acquire``
    passed ``--json`` and assumed the QUEUED response carries a token. This
    fpgahub's queued response carries only ``position``, so a **contended**
    acquire errored out with "no token in response" *and stranded a queue
    entry* — and ``lease wait`` could not rescue it, because it needs the token
    the queued response never gives (verified 2026-07-21).
``scripts/mps3_lease_acquire.sh``
    polls correctly (the proven path) but knows only ``acquire``, hardcodes the
    target name, and has no ``release``/``status``.

THE NAME AUTHORITY — the part most easily got wrong
    Both names are real and each verb takes exactly one of them:

    * ``<board>_pl`` (the LEASE TARGET) is the name the daemon knows, for
      EVERY verb including ``show``. MEASURED against a live hub 2026-09-11:
      ``fpgahub lease show <board>`` -> ``HTTP 404: no such board: '<board>';
      configured: ... <board>_pl ...``, and ``show <board>_pl`` -> ``not
      leased``. An earlier note here asserted the opposite (chassis for
      ``show``, member board a 404); it was INFERRED from a stale CLI record
      and was wrong, so ``status()`` would have 404'd on every call.
    * ``<board>`` is still accepted where a CHASSIS is genuinely wanted, and
      ``status()`` falls back to it, because a 404 read as "free" is how two
      holders end up mid-swap on one board.

    A 404 is therefore never read as "the board is free": that is how a second
    agent drives a board somebody else is mid-swap on.

NO SITE HOST IS BAKED IN. ``MPS3_HUB`` names the hub to ssh into (or
``MPS3_ON_HUB=1`` when already there); unset, this refuses rather than guessing.

THE SOCKET GROUP. fpgahub 0.3.0 puts its unix socket in the ``fpga`` group
(``srw-rw----``). A non-interactive ssh session does not pick up that group even
when ``getent group fpga`` lists the account, so a bare ``ssh hub fpgahub …``
fails with ``[Errno 13] Permission denied``. It did, for every lease verb, on
2026-09-23. The ssh runner therefore runs the command under ``sg <group> -c``,
which does take the group from ``/etc/group``. ``MPS3_HUB_GROUP`` overrides the
group; set it empty to run without ``sg``.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Union

__all__ = [
    "LeaseError",
    "RunResult",
    "HubRunner",
    "SshHubRunner",
    "LocalHubRunner",
    "hub_runner_from_env",
    "Lease",
    "LeaseStatus",
    "LeaseClient",
    "DEFAULT_CHASSIS",
    "DEFAULT_TARGET",
    "DEFAULT_HUB_GROUP",
    "default_chassis",
    "default_target",
    "board_tty",
]

#: A daemon that does not know the name at all, as opposed to any other
#: failure. Only THIS shape is allowed to fall back to the other name.
_NO_SUCH_BOARD = re.compile(r"no such board", re.I)

#: Neutral built-in names. A site names its own boards via ``MPS3_CHASSIS`` /
#: ``MPS3_LEASE_TARGET`` (read at call time by :func:`default_chassis` /
#: :func:`default_target`); the fpgahub TTY directory follows the target
#: (``/dev/<target>/tty_NN``, see :func:`board_tty`).
DEFAULT_CHASSIS = "mps3"
DEFAULT_TARGET = "mps3_pl"


def default_chassis() -> str:
    """``MPS3_CHASSIS`` if set, else :data:`DEFAULT_CHASSIS`."""
    return os.environ.get("MPS3_CHASSIS") or DEFAULT_CHASSIS


def default_target() -> str:
    """``MPS3_LEASE_TARGET`` if set, else :data:`DEFAULT_TARGET`."""
    return os.environ.get("MPS3_LEASE_TARGET") or DEFAULT_TARGET


def board_tty(lane: int, target: Optional[str] = None) -> str:
    """fpgahub's host port for FPGA UART lane ``lane``: ``/dev/<target>/tty_NN``.

    ``MPS3_TTY_DIR`` overrides the directory outright (a site whose hub names
    its device nodes differently)."""
    root = os.environ.get("MPS3_TTY_DIR") or "/dev/" + (target or default_target())
    return "%s/tty_%02d" % (root.rstrip("/"), lane)

DEFAULT_TTL = 3600
DEFAULT_TIER = "interactive"
DEFAULT_POLL_S = 20.0
DEFAULT_TIMEOUT_S = 3600.0

#: ``ClearAllForwardings`` drops any ``LocalForward`` in the caller's ssh config.
#: Without it, every lease call tried to re-bind those ports, and the resulting
#: "bind … Address already in use" lines landed in front of the real error.
SSH_OPTS = ("-o", "ControlPath=none", "-o", "BatchMode=yes",
            "-o", "ClearAllForwardings=yes")

#: The group fpgahub's installer creates and gives its socket. An fpgahub
#: convention, not a site name. See THE SOCKET GROUP above.
DEFAULT_HUB_GROUP = "fpga"

#: What the hub CLI's output renderer must be told before it prints a lease.
#: fpgahub 0.3.0 prints through ``rich``, which WRAPS to 80 columns and colours
#: unless told otherwise, and neither is cosmetic here: a long token wrapped at
#: column 80 makes ``_TOKEN`` below capture the first half, and a half token is
#: silently wrong -- every later command runs, the release at the end fails, and
#: the board stays leased until its TTL expires. ``TERM=dumb`` keeps a renderer
#: that probes the terminal from deciding it has one.
_RENDER_ENV = {"COLUMNS": "400", "NO_COLOR": "1", "TERM": "dumb"}

#: A token is opaque, so its CONTENT cannot be validated -- and the dangerous
#: case proves why a charset check is not enough: half of
#: ``tok-0123456789abcdef…`` is still made entirely of legal characters. What a
#: wrapped reply breaks is STRUCTURE. The CLI prints ``token=`` and ``expires=``
#: as one line, always; a wrap is what puts them on different ones. So the
#: invariant to check is that the line carrying the token also carries the
#: expiry. The charset is checked too, which catches escape sequences a
#: colouring renderer would leave behind.
_TOKEN_OK = re.compile(r"\A[A-Za-z0-9._:~-]{4,128}\Z")

_TOKEN = re.compile(r"token=(\S+)")
_POSITION = re.compile(r"position=(\d+)")
_HELD_BY = re.compile(r"held by (?P<holder>\S+)(?: \(user (?P<user>[^,)]+))?")


class LeaseError(Exception):
    """The hub refused, or answered something this dialect will not guess at."""


@dataclass(frozen=True)
class RunResult:
    """One hub command's outcome."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        return self.stdout + self.stderr


class HubRunner:
    """Runs an ``fpgahub …`` argv on the hub. The single test seam: every
    board-facing call in this module goes through ``__call__``."""

    def build(self, argv: "Sequence[str]") -> "List[str]":  # pragma: no cover
        raise NotImplementedError

    def __call__(self, argv: "Sequence[str]", timeout: "Optional[float]" = None) -> RunResult:
        cmd = self.build(argv)
        env = dict(os.environ)
        env.update(_RENDER_ENV)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                "hub command timed out after %ss: %s" % (timeout, " ".join(cmd))
            ) from exc
        except OSError as exc:
            raise LeaseError("cannot run %s: %s" % (cmd[0], exc)) from exc
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclass
class SshHubRunner(HubRunner):
    """``ssh <hub> "sg fpga -c 'fpgahub …'"``. BatchMode so a missing key fails
    fast instead of hanging on a password prompt. ``group=None`` drops the
    ``sg`` wrapper."""

    hub: str
    group: Optional[str] = DEFAULT_HUB_GROUP

    def build(self, argv):
        # ssh does not carry the local environment, so the render settings have
        # to travel INSIDE the remote command string -- setting them on the ssh
        # process would leave the hub's renderer at its 80-column default.
        prefix = " ".join("%s=%s" % kv for kv in sorted(_RENDER_ENV.items()))
        remote = prefix + " " + " ".join(shlex.quote(a) for a in argv)
        if self.group:
            # Two shells parse this: the hub's login shell, then the `sh -c`
            # that sg starts. Quote once per shell.
            remote = "sg %s -c %s" % (shlex.quote(self.group), shlex.quote(remote))
        return ["ssh", *SSH_OPTS, self.hub, remote]


class LocalHubRunner(HubRunner):
    """``fpgahub …`` directly — for code already running ON the hub (the board
    dataplane is reachable only from there)."""

    def build(self, argv):
        return list(argv)


def hub_runner_from_env() -> HubRunner:
    """Build the runner from the environment, or refuse.

    ``MPS3_HUB`` (or the legacy ``FPGAHUB_HOST``) => ssh; ``MPS3_ON_HUB=1`` =>
    local. Neither set is an error: a default host in a public tree is a guard
    that reads as a value."""
    hub = os.environ.get("MPS3_HUB") or os.environ.get("FPGAHUB_HOST")
    if hub:
        group = os.environ.get("MPS3_HUB_GROUP", DEFAULT_HUB_GROUP)
        return SshHubRunner(hub, group=group or None)
    if os.environ.get("MPS3_ON_HUB"):
        return LocalHubRunner()
    raise LeaseError(
        "no hub configured: set MPS3_HUB=<hub-host> to reach fpgahub over ssh, "
        "or MPS3_ON_HUB=1 when running ON the hub. There is deliberately no "
        "default host."
    )


@dataclass(frozen=True)
class Lease:
    """A held lease. ``token`` AND ``holder`` are both needed to release it."""

    token: str
    holder: str
    target: str


@dataclass(frozen=True)
class LeaseStatus:
    """What ``fpgahub lease show <chassis>`` reported."""

    held: bool
    holder: "Optional[str]"
    user: "Optional[str]"
    raw: str


class LeaseClient:
    """The lease verbs, one dialect, both names.

    ``runner`` is any callable ``(argv, timeout=None) -> RunResult``; omitted,
    it is built from the environment (:func:`hub_runner_from_env`).
    """

    def __init__(
        self,
        runner: "Optional[Callable[..., RunResult]]" = None,
        *,
        chassis: "Optional[str]" = None,
        target: "Optional[str]" = None,
        timeout: float = 60.0,
    ) -> None:
        self._runner = runner if runner is not None else hub_runner_from_env()
        self.chassis = chassis or default_chassis()
        self.target = target or default_target()
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------- #

    def _run(self, argv: "Sequence[str]") -> RunResult:
        return self._runner(list(argv), timeout=self.timeout)

    @staticmethod
    def _check(res: RunResult, what: str) -> RunResult:
        if res.returncode != 0:
            raise LeaseError("%s failed: %s" % (what, res.text.strip() or "(no output)"))
        return res

    # -- acquire ------------------------------------------------------------ #

    def acquire(
        self,
        holder: str,
        *,
        ttl: int = DEFAULT_TTL,
        tier: str = DEFAULT_TIER,
        poll_s: float = DEFAULT_POLL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        allow_preemptible: bool = False,
        sleep: "Callable[[float], None]" = time.sleep,
        now: "Callable[[], float]" = time.monotonic,
        log: "Optional[Callable[[str], None]]" = None,
    ) -> Lease:
        """Take the lease, polling through queued responses until granted.

        Blocks. Returns a :class:`Lease` whose ``token`` is what the callers
        used to scrape off ``mps3_board.sh acquire``'s bare stdout.

        Re-acquiring as the SAME holder keeps FCFS position (the server keys the
        queue by holder), which is why the poll re-issues ``acquire`` rather
        than calling ``lease wait`` — ``wait`` needs a token the queued response
        never gives.
        """
        if tier != "interactive" and not allow_preemptible:
            raise LeaseError(
                "tier %r is revocable and a revoke mid-swap is the exact "
                "corruption the lease exists to prevent; pass "
                "allow_preemptible=True if you really mean it" % tier
            )
        say = log or (lambda _msg: None)
        deadline = now() + timeout_s
        while True:
            res = self._run([
                "fpgahub", "lease", "acquire", self.target,
                "--holder", holder, "--tier", tier, "--ttl", str(ttl),
            ])
            self._check(res, "lease acquire %s" % self.target)
            match = _TOKEN.search(res.text)
            if match:
                token = match.group(1)
                line = next((ln for ln in res.text.splitlines() if "token=" in ln), "")
                if not _TOKEN_OK.match(token) or "expires=" not in line:
                    raise LeaseError(
                        "the hub's reply carried a token this dialect will not "
                        "use: %r. The granted line is printed as one line "
                        "carrying both token= and expires=, so this one was "
                        "wrapped or coloured in transit and %r is a HALF "
                        "token -- the board would be held under an id the "
                        "caller cannot release, and nothing would say so until "
                        "the TTL expired. Full reply:\n%s"
                        % (token, token, res.text.strip())
                    )
                say("lease granted: %s holds %s (token %s)" % (holder, self.target, token))
                return Lease(token=token, holder=holder, target=self.target)
            pos = _POSITION.search(res.text)
            say("queued%s for %s as %s; re-acquiring in %ss"
                % (" at position " + pos.group(1) if pos else "", self.target, holder, poll_s))
            if now() >= deadline:
                # Clean up OUR queue entry: an abandoned one can later grant the
                # board to a dead token and block it for everyone.
                self.cancel(holder)
                raise LeaseError(
                    "gave up waiting for %s after %ss as %s (queue entry "
                    "cancelled)" % (self.target, timeout_s, holder)
                )
            sleep(poll_s)

    # -- release / cancel / heartbeat ---------------------------------------- #

    @staticmethod
    def _token_holder(lease: "Union[Lease, str]", holder: "Optional[str]"):
        if isinstance(lease, Lease):
            return lease.token, holder or lease.holder
        if not holder:
            raise LeaseError(
                "release needs the HOLDER as well as the token: the hub replies "
                "'no lease to release' and leaves the board HELD when the holder "
                "does not match"
            )
        return lease, holder

    def release(self, lease: "Union[Lease, str]", holder: "Optional[str]" = None) -> None:
        token, holder = self._token_holder(lease, holder)
        res = self._run([
            "fpgahub", "lease", "release", self.target,
            "--token", token, "--holder", holder,
        ])
        self._check(res, "lease release %s" % self.target)
        if "no lease to release" in res.text:
            # Exit 0 + this line is the hub's way of saying it did NOT release.
            # Swallowing it is how a board stays held until its TTL expires.
            raise LeaseError(
                "no lease to release on %s as holder %r with that token -- the "
                "board is STILL HELD (a mismatched --holder is the usual cause)"
                % (self.target, holder)
            )

    def cancel(self, holder: str) -> bool:
        """Cancel a stray QUEUE entry by holder. No token needed — this is the
        only holder-addressable recovery path there is. Returns whether
        anything was removed."""
        res = self._run(["fpgahub", "lease", "cancel", self.target, "--holder", holder])
        self._check(res, "lease cancel %s" % self.target)
        return "no matching wait" not in res.text

    def heartbeat(self, lease: "Union[Lease, str]", holder: "Optional[str]" = None,
                  ttl: "Optional[int]" = None) -> None:
        token, holder = self._token_holder(lease, holder)
        argv = ["fpgahub", "lease", "heartbeat", self.target,
                "--token", token, "--holder", holder]
        if ttl is not None:
            argv += ["--ttl", str(ttl)]
        self._check(self._run(argv), "lease heartbeat %s" % self.target)

    # -- status / preflight --------------------------------------------------- #

    def status(self) -> LeaseStatus:
        """``fpgahub lease show`` — the TARGET name first, the chassis as a
        fallback.

        Which name ``show`` takes is deployment-dependent and this code got it
        backwards once already. Measured on the live hub 2026-09-11: the target
        answers and the chassis is a 404. So try the target, and fall back to
        the chassis only on a *no such board* 404 -- never on any other failure,
        because a 404 silently read as "free" is how two holders end up mid-swap
        on one board.
        """
        res = self._run(["fpgahub", "lease", "show", self.target])
        if res.returncode != 0 and _NO_SUCH_BOARD.search(res.text):
            res = self._run(["fpgahub", "lease", "show", self.chassis])
            self._check(res, "lease show %s (target %s was not a known board)"
                        % (self.chassis, self.target))
        else:
            self._check(res, "lease show %s" % self.target)
        m = _HELD_BY.search(res.text)
        if not m:
            return LeaseStatus(held=False, holder=None, user=None, raw=res.text.strip())
        return LeaseStatus(
            held=True, holder=m.group("holder"),
            user=(m.group("user") or None), raw=res.text.strip(),
        )

    def held_by(self, holder: "Optional[str]") -> bool:
        """Is the board held by ``holder`` EXACTLY, right now?

        Not :meth:`preflight`: that answers True for an UNHELD board, which is
        the right answer for "may I read from it" and the wrong one for "may I
        power-cycle it ten times in a row". A campaign needs to own the board,
        and *nobody owns it* is not ownership — the next agent to acquire would
        walk into a rebooting board with a clean lease.
        """
        if not holder:
            return False
        st = self.status()
        return bool(st.held) and st.holder == holder

    def preflight(self, holder: "Optional[str]") -> bool:
        """Safe for US to touch the board? True iff unheld, or held by
        ``holder`` EXACTLY.

        Deliberately does not probe the board: the only ways to ask "is a swap
        running?" over JTAG involve halting the MicroBlaze, which is the very
        corruption this guards. And it does not prefix-match the holder —
        guessing would let one agent mistake another agent's lease for its own.
        """
        st = self.status()
        if not st.held:
            return True
        return bool(holder) and st.holder == holder


# --------------------------------------------------------------------------- #
# The xsdb halt ban
# --------------------------------------------------------------------------- #
#
# The lease coordinates two channels that cannot see each other (module
# docstring): Ethernet 6900/6910 and JTAG via hw_server. The one JTAG action
# that has already destroyed a transfer is HALTING the harness CPU -- an
# `xsdb stop` from another context killed a live 1.31 MB partial push on the
# bare-metal MicroBlaze, whose TCP stack stops with the core.
#
# On the MicroBlaze V it is worse, and it is why this ban is code rather than a
# sentence. There the harness CPU runs LINUX: `stop` halts the whole kernel --
# timers, eth0, SSH, mps3-harnessd, the watchdog kick -- and if the halt
# outlives the watchdog period, the WDOG resets the CPU and the board reboots
# under whoever is looking at it. Reading memory never needs a halt: `mrd`
# works on a running core (bare metal: through the MDM; MicroBlaze V: through
# the debug module's system-bus access, a B1 item) and the Linux harness can
# also be read over ssh (pyverify.mailbox.SshDevmemReader).
#
# So every xsdb script this tree runs against a LIVE harness is checked by
# assert_xsdb_script_safe() -- host/pyverify/tests/test_xsdb_halt_ban.py runs
# it over scripts/mps3_diag.tcl and every tier-3 harness gate, and fails the
# day one of them grows a halting command.

__all__ += ["XSDB_HALTING_COMMANDS", "XsdbHaltError", "xsdb_halting_commands",
            "assert_xsdb_script_safe"]

#: xsdb commands that halt (or reset, or reload) the processor they target, or
#: only make sense on a halted one. `con` is here too: a script that resumes a
#: core is a script that expects to have stopped one. `bpadd` halts the core
#: when the breakpoint hits; `fpga` reconfigures the device under the harness.
XSDB_HALTING_COMMANDS = frozenset({
    "stop", "con", "rst", "dow", "stp", "stpi", "nxt", "nxti", "stpout",
    "bpadd", "fpga",
})


class XsdbHaltError(LeaseError):
    """An xsdb script for a LIVE harness contains a halting command."""


_TCL_CMD = re.compile(r"(?:^|[;\[{])\s*([A-Za-z_][\w:]*)")


def xsdb_halting_commands(script: str) -> "List[Tuple[int, str]]":
    """Every ``(line_no, command)`` in ``script`` that invokes a halting xsdb
    command. A Tcl command word is the first word of a line, or the first word
    after ``;``, ``[`` or ``{``. Comment lines (``#`` first) are ignored, and
    so is text inside double quotes, so ``puts "NEVER stop here"`` is fine."""
    hits = []
    for n, line in enumerate(script.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Blank out double-quoted strings (no escapes needed for this purpose).
        code = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
        for m in _TCL_CMD.finditer(code):
            word = m.group(1)
            if word in XSDB_HALTING_COMMANDS:
                hits.append((n, word))
    return hits


def assert_xsdb_script_safe(script: str, *, name: str = "<script>") -> None:
    """Raise :class:`XsdbHaltError` if ``script`` would halt the harness CPU.

    Call it before running ANY generated xsdb script against a live board.
    """
    hits = xsdb_halting_commands(script)
    if hits:
        where = ", ".join("line %d: %s" % h for h in hits)
        raise XsdbHaltError(
            "%s would halt the harness CPU (%s). On the MicroBlaze V that stops "
            "the whole of Linux -- SSH, mps3-harnessd, the watchdog kick -- and "
            "on bare metal it has already destroyed a live partial push. Read "
            "with `mrd` on the running core, or over ssh "
            "(pyverify.mailbox.SshDevmemReader)." % (name, where))
