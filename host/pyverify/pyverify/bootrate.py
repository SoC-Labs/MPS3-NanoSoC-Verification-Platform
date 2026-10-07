"""``pyverify.bootrate`` — MEASURE the MPS3 boot lottery instead of quoting it.

THE FACT THIS TOOL EXISTS TO REPLACE
    Every plan in this tree says MCC-config-from-SD works "about 1 time in 4",
    and every one of them is quoting the same internal note recording **four**
    power-ups: one clean, two dark, one partial. That is not a rate. It has no
    denominator worth the name, no interval, and no record of which config the
    board was running — so no change to that config can ever be shown to have
    helped, which is why nothing has been tried.

    The same image comes up lit on some power-ups and dark on others, and the
    MCC's ``LOG.TXT`` is byte-identical on a good boot and a dark one: the
    failure is at the **unlogged release-to-RUN step**. Nothing in the log will
    ever tell us. Only counting will.

WHAT A MEASUREMENT HAS TO CARRY TO BE ONE
    * a **denominator** — how many reboots were actually performed, which is not
      the same as how many were asked for (a hub error is an experiment that did
      not happen, not a dark boot);
    * an **interval** — with N=10 the 95% Wilson interval on 7/10 is
      (0.40, 0.89). Half the range. Anyone comparing 7/10 against 9/10 without
      it is reading noise, and this tool prints it beside every rate so that
      reading is available to be refused;
    * the **configuration under test**, read from the SD-bundle variant it names
      rather than typed into the record by hand (``fpga/mps3_sd/variants/``);
    * a **before and an after** for ONE variable — :func:`compare`.

THE OUTCOMES, AND WHY DARK IS NOT AN ERROR
    ``up``          ping answered with a ``shell_id`` inside the deadline (and,
                    on the Linux harness, every boot check below passed).
    ``fail``        (Linux harness) the board came back, but not as the boot
                    under test: stage0's rescue server instead of Linux, the
                    wrong slot, an unconfirmed boot, or stage0 needing more
                    than one entry. Counted in the denominator, with its reason.
    ``dark``        the deadline passed with nothing answering at all. THE
                    MEASUREMENT — the thing being counted, recorded as data and
                    never as a failed run.
    ``timeout``     something was there and never converged: the control channel
                    accepted a connection without answering, or the optional
                    JTAG witness said the FPGA IS configured. Configured+inert
                    is a **hung firmware**, a different defect from a failed
                    configuration, and folding it into ``dark`` would poison the
                    rate this tool exists to produce.
    ``hub-error``   the reboot never happened: the reset surface refused, or it
                    reported ok and the shell went on answering across it (the
                    signature of a REBOOT sent to the wrong tty), or the kernel
                    that answered is older than the reset. Excluded from the
                    rate's denominator and reported separately.

THE MCC REBOOT (``--method mcc``), AND WHY IT IS NO LONGER REFUSED
    This tool used to refuse ``mcc`` as a "proven no-op". That theory is
    SUPERSEDED: every recorded no-op (07-28 x3, 07-30, 08-04) was sent to
    ``tty_01``, which is FPGA UART lane 0; the MCC console is ``tty_00`` (the
    MCC's own boot log says ``UART0: MCC, UART1: FPGA0``). W1 on 2026-09-24
    was the hands-free fielding of ``0x72BB0A36``: exactly ONE reader on ``tty_00``, a
    bare CR, then R-E-B-O-O-T one character per 100 ms (the MCC drops burst
    input; fpgahub's ``mcc.py`` paces at 50 ms) and a CR. Its first attempt
    failed only because a leftover ``cat`` of ``tty_00`` was a second reader.
    :class:`MccRebootResetter` issues it three ways (``route``):

    * ``tty`` (DEFAULT since 2026-09-24, ILA handoff defect 6) —
      :data:`HUB_MCC_REBOOT_PY`, run ON THE HUB through the runner:
      refuses when another process names or holds the tty, refuses when a bare
      CR does not bring back an intact ``Cmd>`` (the effect of a reader this
      account cannot see), then the paced REBOOT, then waits for ``Rebooting``.
      The one route the fielded procedure has proven.
    * ``fpgahub`` — ``fpgahub target reset <t> --method mcc``, accepted ONLY
      when its reply names ``…/tty_00``: the live ``reset.mcc`` config was once
      pinned to ``tty_01``, and the reply is the one place that says which.
      The ILA lead found that path sends a burst the MCC drops (handoff §4.6);
      the fpgahub source paces at 50 ms, but nobody has checked the build on
      the hub. Opt-in only.
    * ``auto`` — fpgahub if its reply names ``tty_00``; if fpgahub refused, or
      wrote to another ``tty_NN``, the paced write instead (for the rest of the
      campaign). A reply that names no recognisable tty stops the campaign
      rather than risk a second REBOOT on top of a first. It was the default
      until 2026-09-24; it TRUSTS fpgahub's reply, which names the tty but
      cannot say whether the characters were paced, so it is opt-in now.

    Writing the SD and then rebooting onto it is :mod:`pyverify.fielding`
    (``pyverify sd field``): the REBOOT there waits for fpgahubd's journal
    witness first.

THE LINUX HARNESS (``version.impl == "linux"``, or ``linux=True``)
    * the default deadline is :data:`LINUX_DEADLINE_S` (180 s): MCC reload +
      ~24 s to load 24 MB from the µSD + ~20 s to 6900 is about 100 s;
    * "down" means 6900 refused AND identify (UDP 6899) silent — stage0's
      rescue server answers identify with no 6900 at all;
    * each boot is checked: ``stats.os_up_ms`` is younger than the reset
      (else hub-error: the kernel never restarted), identify says
      ``mode:"run"`` (``rescue`` = fail, with stage0's reason), and — through
      the ``stage0`` seam (``pyverify mailbox``'s ssh devmem read by default in
      the CLI) — the status block is valid, ``boot_count == 1`` (a
      reconfiguration zeroes it, so 1 = first try), ``booted_from`` is the
      expected slot, and Linux confirmed (``att_confirm == "S0OK"``).

WHAT IT REFUSES
    * a run without a lease **held by us**. :meth:`~pyverify.lease.LeaseClient.preflight`
      answers True for an UNHELD board, which is right for a read-only probe and
      wrong for a campaign that power-cycles a shared board ten times. It never
      acquires or releases a lease: the token and the hub are parameters.
    * a paced MCC write with a second reader on the tty (see above). The
      campaign stops there and says which process; nothing is sent.

SEAMS (nothing site-specific, nothing that needs a board to test)
    ``probe``     ``() -> Probe`` — liveness. Default: :class:`ShellProbe` over
                  the conformance-pinned :class:`~pyverify.client.ShellClient`.
    ``resetter``  ``(method) -> ResetOutcome``. Default: :class:`HubResetter`,
                  ``fpgahub target reset <target> --method <m> --yes`` through
                  the same :class:`~pyverify.lease.HubRunner` seam every other
                  hub call in this package uses; :class:`MccRebootResetter`
                  for ``--method mcc``.
    ``stage0``    OPTIONAL ``() -> mailbox.Stage0Status`` (Linux only).
    ``witness``   OPTIONAL ``() -> Witness`` — DONE/USERCODE over JTAG is the
                  stronger evidence, but it needs ``xsdb`` on the hub, so it is
                  a hook that is **off by default** and never a requirement.
                  :class:`CommandWitness` adapts any command that prints
                  ``{"configured": bool, "usercode": "0x…"}``.
    ``now``/``sleep``/``wall``  the clock. A 10×90 s campaign is 15 minutes; a
                  test that waited it out would be deleted within the week.
    ``mcc_log_path``  where ``LOG.TXT`` is readable on the executing host — the
                  same seam ``sd.py``'s ``--verify-path`` uses, because there is
                  no site-independent mount path and guessing one would be a
                  hardcoded site fact wearing a default's clothes.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import math
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .lease import LeaseClient, LeaseError, RunResult, SshHubRunner
from .sd import sd_file_sha256

__all__ = [
    "SCHEMA_ID",
    "OUTCOMES",
    "OUTCOME_UP",
    "OUTCOME_DARK",
    "OUTCOME_TIMEOUT",
    "OUTCOME_HUB_ERROR",
    "OUTCOME_FAIL",
    "RUN_RECORD_SCHEMA",
    "BootRateError",
    "BootRateInputError",
    "Probe",
    "Witness",
    "ResetOutcome",
    "ShellProbe",
    "HubResetter",
    "MccTtyResetter",
    "MccRebootResetter",
    "HUB_MCC_REBOOT_PY",
    "MCC_ROUTES",
    "DEFAULT_MCC_ROUTE",
    "LINUX_DEADLINE_S",
    "CommandWitness",
    "BootRateRunner",
    "check_method",
    "summarise",
    "wilson",
    "clopper_pearson",
    "validate_record",
    "compare",
    "format_compare",
    "variant_delta",
    "list_variants",
    "evidence_stem",
    "evidence_csv",
    "evidence_md",
    "write_evidence",
]

#: The run-record schema id. VERSIONED: a record written by a future, different
#: shape must be rejected rather than half-read, because the whole value of
#: these files is that a run from months ago can still be compared.
SCHEMA_ID = "mps3.boot-rate/1"

OUTCOME_UP = "up"
OUTCOME_DARK = "dark"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_HUB_ERROR = "hub-error"
#: ADDITIVE (Linux harness): the board came back, but not as the boot under
#: test -- rescue, the wrong slot, unconfirmed, or a retried boot. Last in the
#: tuple so every older outcome keeps its position.
OUTCOME_FAIL = "fail"
OUTCOMES = (OUTCOME_UP, OUTCOME_DARK, OUTCOME_TIMEOUT, OUTCOME_HUB_ERROR,
            OUTCOME_FAIL)

#: Reset methods this tool will not drive, and why. EMPTY since 2026-09-24:
#: `mcc` sat here as a "proven no-op", and every one of those no-ops had been
#: sent to tty_01 (FPGA UART lane 0), not the MCC console tty_00. W1 fielded a
#: static with a paced tty_00 REBOOT and no hands. The table stays so a method
#: that really is a no-op has somewhere to go.
REFUSED_METHODS: Dict[str, str] = {}

#: the method name that means "MCC REBOOT" (fpgahub's reset.mcc, or the paced
#: tty_00 write -- :class:`MccRebootResetter`)
MCC_METHOD = "mcc"

DEFAULT_DEADLINE_S = 90.0
#: A Linux boot from the µSD: MCC reload + ~24 s to load 24 MB + ~20 s to 6900
#: is about 100 s (LINUX_SPEEDUP_2026-09-24 §5). 90 s would count most of them
#: dark, so a Linux campaign defaults to this.
LINUX_DEADLINE_S = 180.0
#: A kernel uptime up to this much OLDER than the reset still counts as fresh:
#: the poll interval and the ssh/TCP round trip separate the two clocks.
FRESH_SLACK_MS = 2000
#: How long after 6900 first answers to keep re-reading stage0 for Linux's
#: confirm word: harnessd writes it once its service table runs, which can land
#: a moment after the first ping.
DEFAULT_CONFIRM_GRACE_S = 20.0

#: The paced MCC REBOOT (W1, 2026-09-24): 100 ms per character, 1 s after the
#: bare CR. The MCC drops burst input; fpgahub's mcc.py found 50 ms the floor.
DEFAULT_MCC_PACE_S = 0.1
MIN_MCC_PACE_S = 0.05
DEFAULT_MCC_SETTLE_S = 1.0
DEFAULT_MCC_ACK_S = 10.0
DEFAULT_MCC_PROMPT_S = 3.0
MCC_ROUTES = ("auto", "fpgahub", "tty")
#: The route every tool uses unless told otherwise: the paced tty_00 write
#: (ILA handoff defect 6). ``auto`` trusted fpgahub's reset.mcc, which the
#: handoff found sends a burst the MCC drops.
DEFAULT_MCC_ROUTE = "tty"
DEFAULT_POLL_S = 5.0
DEFAULT_GAP_S = 10.0
DEFAULT_DOWN_DEADLINE_S = 30.0
DEFAULT_PROBE_TIMEOUT_S = 3.0
DEFAULT_RESET_TIMEOUT_S = 120.0

#: Where the SD-bundle experiment variants live, relative to the repo root.
VARIANTS_REL = os.path.join("fpga", "mps3_sd", "variants")
VARIANT_META = "variant.txt"


class BootRateError(Exception):
    """A gate refused: no lease, the wrong lease, a refused method."""


class BootRateInputError(Exception):
    """Bad input: an unknown variant, an unreadable or foreign run record."""


# --------------------------------------------------------------------------- #
# the seams
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Probe:
    """One liveness observation.

    ``connected`` is deliberately separate from ``up``: a TCP connect that
    succeeds while the verb never returns a ``shell_id`` is a **configured
    board with hung firmware**, which is the difference between ``timeout`` and
    ``dark``.
    """

    up: bool
    shell_id: str = ""
    version: Optional[Dict[str, Any]] = None
    connected: bool = False
    error: str = ""
    #: ADDITIVE (Linux): ``{"up_ms": …, "os_up_ms": …}`` from ``stats`` when
    #: the shell answered it -- the kernel's age is the reboot witness.
    stats: Optional[Dict[str, Any]] = None
    #: ADDITIVE (Linux): identify's ``mode`` ("run" / "rescue"); None when
    #: identify was silent or not asked. ``identify`` is the whole reply.
    mode: Optional[str] = None
    identify: Optional[Dict[str, Any]] = None

    @property
    def alive(self) -> bool:
        """Anything answering at all: the shell, or identify (stage0's rescue
        server answers identify with nothing on 6900)."""
        return self.up or self.mode is not None

    @property
    def impl(self) -> Optional[str]:
        return (self.version or {}).get("impl")


@dataclass(frozen=True)
class Witness:
    """What a JTAG DONE/USERCODE probe saw. ``configured`` is tri-state:
    ``None`` means the witness could not tell, which must never be read as
    "not configured"."""

    configured: Optional[bool]
    usercode: Optional[str] = None
    raw: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"configured": self.configured, "usercode": self.usercode,
                "raw": self.raw}


@dataclass(frozen=True)
class ResetOutcome:
    ok: bool
    detail: str = ""
    #: ADDITIVE: stop the campaign -- every further reset would fail the same
    #: way (a second reader on the MCC tty, fpgahub writing to the wrong tty).
    fatal: bool = False
    #: ADDITIVE: which path issued it ("fpgahub", "tty"); None for plain
    #: ``fpgahub target reset --method <m>``.
    route: Optional[str] = None
    #: ADDITIVE: did the REBOOT reach the MCC? True, False (known NOT to: the
    #: hub refused, or wrote to another tty), None (cannot tell).
    sent: Optional[bool] = None


class ShellProbe:
    """Liveness over the 6900 control channel: ``ping`` for the fabric identity,
    then ``version`` for the firmware identity (one static_id serves many
    firmware builds, so the pair is the honest answer to "what came up").

    On the Linux harness (``version.impl == "linux"`` seen once, or
    ``linux=True``) it also reads ``stats`` (the kernel's ``os_up_ms``) and asks
    identify on UDP ``identify_port`` for ``mode`` -- which is how a board in
    stage0's rescue server (identify, no 6900) is told apart from a dark one.

    A short timeout on purpose — during a campaign this is called every few
    seconds against a board that is expected to be down half the time.
    """

    def __init__(self, host: str, port: int = 6900,
                 timeout: float = DEFAULT_PROBE_TIMEOUT_S, *,
                 identify_port: Optional[int] = 6899,
                 identify_timeout: float = 1.0,
                 linux: bool = False) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.identify_port = identify_port
        self.identify_timeout = identify_timeout
        #: latched True the first time the shell reports impl=linux
        self.linux = bool(linux)

    def __call__(self) -> Probe:
        probe = self._shell()
        if probe.impl == "linux":
            self.linux = True
        if self.linux and self.identify_port:
            probe = self._with_identify(probe)
        return probe

    def _with_identify(self, probe: Probe) -> Probe:
        from .identify import IdentifyError, identify

        try:
            reply = identify(self.host, self.identify_port,
                             timeout=self.identify_timeout, retries=0)
        except (IdentifyError, OSError):
            return probe
        raw = dict(reply.raw)
        mode = raw.get("mode")
        if not isinstance(mode, str) or not mode:
            # harnessd's run-mode key position is still PROVISIONAL; stage0's
            # rescue reply always carries mode, so an answer without one is
            # the harness engine.
            mode = "run"
        return dataclasses.replace(probe, mode=mode, identify=raw)

    def _shell(self) -> Probe:
        from .client import ShellClient, ShellProtocolError

        client = ShellClient(self.host, port=self.port, timeout=self.timeout)
        try:
            with client:
                ping = client.ping()
                if not ping.shell_id:
                    # Connected, answered, said nothing identifying: alive but
                    # not converged.
                    return Probe(up=False, connected=True,
                                 error="ping carried no shell_id")
                version: Optional[Dict[str, Any]] = None
                try:
                    v = client.version()
                    version = {"harness": v.harness, "ver32": v.ver32,
                               "sha": v.sha, "dirty": v.dirty,
                               "lmb_kb": v.lmb_kb, "features": list(v.features),
                               "impl": getattr(v, "impl", None)}
                except (OSError, ShellProtocolError) as exc:
                    # An older image without `version` is still UP.
                    version = {"error": str(exc)}
                stats: Optional[Dict[str, Any]] = None
                if self.linux or version.get("impl") == "linux":
                    try:
                        s = client.stats()
                        stats = ({"up_ms": s.up_ms, "os_up_ms": s.os_up_ms}
                                 if s.ok else {"error": s.err})
                    except (OSError, ShellProtocolError) as exc:
                        stats = {"error": str(exc)}
                return Probe(up=True, shell_id=ping.shell_id, version=version,
                             connected=True, stats=stats)
        except ShellProtocolError as exc:
            return Probe(up=False, connected=True, error=str(exc))
        except OSError as exc:
            return Probe(up=False, connected=False, error=str(exc))


class HubResetter:
    """``fpgahub target reset <target> --method <m> --yes`` over the HubRunner
    seam — the dialect ``scripts/mps3_shell_update.sh`` already uses."""

    def __init__(self, runner: Callable[..., RunResult], *, target: str,
                 timeout: float = DEFAULT_RESET_TIMEOUT_S) -> None:
        self._runner = runner
        self.target = target
        self.timeout = timeout

    def argv(self, method: str) -> List[str]:
        return ["fpgahub", "target", "reset", self.target,
                "--method", method, "--yes"]

    def __call__(self, method: str) -> ResetOutcome:
        mcc = method == MCC_METHOD
        route = "fpgahub" if mcc else None
        try:
            res = self._runner(self.argv(method), timeout=self.timeout)
        except TimeoutError as exc:
            return ResetOutcome(False, "reset command timed out: %s" % exc,
                                route=route)
        except (LeaseError, OSError) as exc:
            return ResetOutcome(False, "cannot run the reset command: %s" % exc,
                                route=route, sent=(False if mcc else None))
        text = res.text.strip()
        if res.returncode != 0:
            return ResetOutcome(False, text or "(no output)", route=route,
                                sent=(False if mcc else None))
        # The CLI prints `ok <name> reset ...` / `failed ...` and exits 0 either
        # way for a dispatched-but-failed reset.
        if "failed" in text.lower() and "ok" not in text.split("\n")[0].lower():
            return ResetOutcome(False, text, route=route,
                                sent=(False if mcc else None))
        if not mcc:
            return ResetOutcome(True, text)
        # reset.mcc: the reply is the ONLY place that says which tty the
        # REBOOT went to, and the live config was once pinned to tty_01.
        path = mcc_reply_tty(text)
        kind = _tty_kind(path)
        if kind == "mcc":
            return ResetOutcome(True, text, route=route, sent=True)
        if kind == "other":
            return ResetOutcome(
                False,
                "fpgahub's reset.mcc wrote REBOOT to %s, which is an FPGA UART, "
                "not the MCC console (tty_00): the no-op recorded 07-28..08-04. "
                "Drop reset.mcc's tty_path from the hub config, or use the "
                "paced write (--mcc-route tty). Reply: %s" % (path, text),
                fatal=True, route=route, sent=False)
        return ResetOutcome(
            False,
            "cannot tell which tty fpgahub's reset.mcc wrote REBOOT to (reply: "
            "%r). Stopping rather than risk a second REBOOT on top of a first; "
            "use --mcc-route tty." % text,
            fatal=True, route=route, sent=None)


# --------------------------------------------------------------------------- #
# the MCC REBOOT (tty_00, paced) -- W1 2026-09-24
# --------------------------------------------------------------------------- #

_MCC_SENT = re.compile(r"REBOOT sent\b[^\n]*?\bon\s+(\S+)")
_TTY_NODE = re.compile(r"(?:^|/)tty_(\d+)$")


def mcc_reply_tty(text: str) -> Optional[str]:
    """The tty fpgahub's ``mps3_mcc_reboot`` plugin says it wrote to: its reply
    is ``REBOOT sent directly on <path>`` or ``REBOOT sent via tty_share broker
    on <path>`` (fpgahub ``reset_plugins/mps3.py``)."""
    m = _MCC_SENT.search(text or "")
    return m.group(1).rstrip(".,;") if m else None


def _tty_kind(path: Optional[str]) -> str:
    """``mcc`` (…/tty_00), ``other`` (another …/tty_NN: an FPGA UART), or
    ``unknown`` (no path, or a raw /dev/ttyACMn / -mcc alias we cannot map)."""
    if not path:
        return "unknown"
    m = _TTY_NODE.search(path)
    if not m:
        return "unknown"
    return "mcc" if int(m.group(1)) == 0 else "other"


def _hub_argv(runner: Any, argv: Sequence[str]) -> List[str]:
    """An argv with multi-word elements, made safe for whichever runner runs it.

    A local runner execs the argv as-is. :class:`~pyverify.lease.SshHubRunner`
    joins it into ONE remote command string -- quoted per element since
    ccc2fde (``sg fpga -c``), but by a bare ``" ".join`` before it. Ask the
    runner which it does (``build`` is pure) and pre-quote only for the
    unquoting kind, so the script arrives intact either way.
    """
    if isinstance(runner, SshHubRunner):
        try:
            if runner.build(["a b"])[-1].endswith(" a b"):
                return [shlex.quote(a) for a in argv]
        except Exception:  # noqa: BLE001 - a probe of the runner must never fail a reset
            pass
    return list(argv)


#: Runs ON THE HUB (``python3 -c HUB_MCC_REBOOT_PY '<json args>'``): stdlib
#: only, Python 3.6+. Prints ONE JSON line; the exit code is the verdict:
#: 0 done (``mode`` scan: no other reader; reboot: ``Rebooting`` echoed),
#: 2 the tty is missing or will not open, 3 another process names or holds the
#: tty (NOTHING sent), 4 a bare CR did not bring back an intact ``Cmd>``
#: (NOTHING sent), 5 REBOOT sent but no ``Rebooting`` within ``ack_s``.
#: The second-reader scan is soak_linux.py's rule: /proc/*/cmdline is readable
#: for every user (a root ``cat /dev/…/tty_00`` shows), /proc/*/fd only for our
#: own; the prompt check catches a reader neither can see (a root broker).
#: ADDITIVE (``pyverify sd field``): with ``capture_s`` > 0 an acknowledged
#: REBOOT is followed by a capture of the MCC's boot log (appended to ``log``
#: on the hub when given) until ``FPGA configuration complete`` and the next
#: ``Cmd>``, a configuration failure, or ``capture_s``; the verdict gains
#: ``configuring``/``complete``/``failed``/``tail``. The exit code is unchanged
#: (0 = acknowledged): the caller judges ``complete``. Absent (boot-rate), the
#: script behaves exactly as before.
HUB_MCC_REBOOT_PY = r'''
import json, os, re, select, sys, time
A = json.loads(sys.argv[1])
TTY = A["tty"]
OUT = {"tty": TTY, "mode": A["mode"], "sent": False, "ack": False,
       "others": [], "prompt": "", "echo": ""}

def done(rc, reason=None):
    OUT["rc"] = rc
    OUT["reason"] = reason
    sys.stdout.write(json.dumps(OUT) + "\n")
    sys.stdout.flush()
    sys.exit(rc)

def ancestors():
    pids, pid = set(), os.getpid()
    for _ in range(64):
        pids.add(pid)
        try:
            with open("/proc/%d/stat" % pid) as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        pid = ppid
    return pids

def others():
    real = os.path.realpath(TTY)
    names = set([TTY, real])
    mine = ancestors()
    hits = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in mine:
            continue
        pid = int(d)
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                argv = [x.decode("utf-8", "replace") for x in fh.read().split(b"\0") if x]
        except OSError:
            continue
        cmd = " ".join(argv)[:160]
        # `cat TTY`, `--tty=TTY`, socat's `TTY,raw,echo=0`
        if any(c in names or c.split(",", 1)[0] in names
               for x in argv[1:] for c in (x, x.split("=", 1)[-1])):
            hits.append([pid, cmd])
            continue
        try:
            fds = os.listdir("/proc/%d/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath("/proc/%d/fd/%s" % (pid, fd)) == real:
                    hits.append([pid, "(holds it open) " + cmd])
                    break
            except OSError:
                pass
    return hits

def open_raw():
    import termios, tty
    fd = os.open(TTY, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    at = termios.tcgetattr(fd)
    speed = getattr(termios, "B%d" % A["baud"])
    at[2] |= termios.CLOCAL | termios.CREAD
    at[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG)
    at[4] = at[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, at)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd

def drain(fd, secs, until):
    buf = b""
    end = time.monotonic() + secs
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        try:
            r, _, _ = select.select([fd], [], [], min(0.1, left))
            if r:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buf += chunk
                if until in buf:
                    break
        except OSError:
            break
    return buf

if not os.path.exists(TTY):
    done(2, "no such tty %s on this host" % TTY)
OUT["others"] = others()
if OUT["others"]:
    done(3, "another process reads %s" % TTY)
if A["mode"] == "scan":
    done(0)
try:
    fd = open_raw()
except OSError as exc:
    done(2, "cannot open %s: %s" % (TTY, exc))
t0 = time.monotonic()
os.write(fd, b"\r")
p = drain(fd, A["prompt_s"], b"Cmd>")
OUT["prompt"] = p.decode("utf-8", "replace")[-120:]
if b"Cmd>" not in p:
    done(4, "Debug> submenu, not Cmd>" if b"Debug>" in p else
         "no intact Cmd> after a bare CR")
time.sleep(max(0.0, A["settle"] - (time.monotonic() - t0)))
for ch in b"REBOOT":
    os.write(fd, bytes([ch]))
    time.sleep(A["pace"])
os.write(fd, b"\r")
OUT["sent"] = True
e = drain(fd, A["ack_s"], b"Rebooting")
OUT["echo"] = e.decode("utf-8", "replace")[-200:]
OUT["ack"] = b"Rebooting" in e

def reopen(until):
    # A USB re-enumeration of the MCC's CDC port: the fd dies; the node returns.
    while time.monotonic() < until:
        time.sleep(0.25)
        try:
            return open_raw()
        except OSError:
            continue
    return None

def capture(fd, first, secs, log):
    buf = bytearray(first)
    fh = None
    if log:
        try:
            fh = open(log, "ab")
        except OSError as exc:
            OUT["log_error"] = str(exc)
    if fh:
        fh.write(first)
        fh.flush()
    end = time.monotonic() + secs
    fail = re.compile(br"(?i)configuration failed|fpga configuration error")
    settled = None
    while time.monotonic() < end:
        if settled is not None and (bytes(buf).rstrip().endswith(b"Cmd>")
                                    or time.monotonic() - settled >= 15):
            break
        chunk = b""
        try:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                chunk = os.read(fd, 4096)
                if not chunk:
                    raise OSError("hangup")
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = reopen(min(end, time.monotonic() + 10))
            if fd is None:
                break
            continue
        if chunk:
            buf += chunk
            if fh:
                fh.write(chunk)
                fh.flush()
        if settled is None and (b"FPGA configuration complete" in buf or fail.search(buf)):
            settled = time.monotonic()
    if fh:
        fh.close()
    b = bytes(buf)
    OUT["configuring"] = b"Configuring FPGA from file" in b
    OUT["complete"] = b"FPGA configuration complete" in b
    OUT["failed"] = bool(fail.search(b))
    OUT["log"] = log
    OUT["tail"] = b.decode("utf-8", "replace")[-400:]

if OUT["ack"] and float(A.get("capture_s") or 0) > 0:
    capture(fd, p + e, float(A["capture_s"]), A.get("log"))
done(0 if OUT["ack"] else 5, None if OUT["ack"] else "no Rebooting echo")
'''


def _last_json_line(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


class MccTtyResetter:
    """The paced MCC REBOOT on ``tty_path`` (the MCC console, ``tty_00``),
    executed ON THE HUB through the runner seam as ``python3 -c``
    :data:`HUB_MCC_REBOOT_PY`. W1's recipe: exactly one reader, a bare CR, then
    R-E-B-O-O-T ``pace_s`` apart and a CR."""

    def __init__(self, runner: Callable[..., RunResult], *, tty_path: str,
                 pace_s: float = DEFAULT_MCC_PACE_S,
                 settle_s: float = DEFAULT_MCC_SETTLE_S,
                 ack_s: float = DEFAULT_MCC_ACK_S,
                 prompt_s: float = DEFAULT_MCC_PROMPT_S,
                 baud: int = 115200, python: str = "python3",
                 timeout: float = 60.0,
                 capture_s: float = 0.0,
                 log_path: Optional[str] = None) -> None:
        if pace_s < MIN_MCC_PACE_S:
            raise BootRateInputError(
                "--mcc-pace %.3fs is below %.2fs: the MCC drops burst input, so "
                "only the first characters of REBOOT would land" % (pace_s, MIN_MCC_PACE_S))
        self._runner = runner
        self.tty_path = tty_path
        self.pace_s = pace_s
        self.settle_s = settle_s
        self.ack_s = ack_s
        self.prompt_s = prompt_s
        self.baud = baud
        self.python = python
        #: ADDITIVE: capture the MCC boot log after the acknowledgement for up
        #: to this long (0 = no capture: boot-rate's behaviour, argv unchanged)
        self.capture_s = capture_s
        #: where the capture is appended ON THE HUB (None = verdict tail only)
        self.log_path = log_path
        # the hub command must outlive the capture it was asked for
        self.timeout = max(timeout, capture_s + prompt_s + settle_s + ack_s + 30.0)
        #: the hub writer's last JSON verdict (``complete``/``tail`` when captured)
        self.last_info: Optional[Dict[str, Any]] = None

    def argv(self, mode: str = "reboot") -> List[str]:
        args = {"mode": mode, "tty": self.tty_path, "pace": self.pace_s,
                "settle": self.settle_s, "ack_s": self.ack_s,
                "prompt_s": self.prompt_s, "baud": self.baud}
        if self.capture_s > 0 and mode != "scan":
            args["capture_s"] = self.capture_s
            if self.log_path:
                args["log"] = self.log_path
        return _hub_argv(self._runner, [self.python, "-c", HUB_MCC_REBOOT_PY,
                                        json.dumps(args, sort_keys=True)])

    def describe(self) -> str:
        return ("paced REBOOT on %s (%d ms/char, %.1fs after a bare CR, one "
                "reader only)" % (self.tty_path, round(self.pace_s * 1000), self.settle_s))

    def scan(self) -> ResetOutcome:
        """The second-reader check alone. Sends nothing."""
        return self._run("scan")

    def __call__(self, method: str = MCC_METHOD) -> ResetOutcome:
        return self._run("reboot")

    def _run(self, mode: str) -> ResetOutcome:
        tty = self.tty_path
        self.last_info = None
        try:
            res = self._runner(self.argv(mode), timeout=self.timeout)
        except TimeoutError as exc:
            # The REBOOT may or may not have gone out: never fatal, never
            # retried here -- the down edge decides.
            return ResetOutcome(False, "MCC writer timed out: %s" % exc, route="tty")
        except (LeaseError, OSError) as exc:
            return ResetOutcome(False, "cannot run the MCC writer on the hub: %s" % exc,
                                route="tty", sent=False)
        info = _last_json_line(res.stdout)
        self.last_info = info
        rc = res.returncode
        if info is None:
            return ResetOutcome(
                False, "the hub-side MCC writer gave no verdict (rc %d): %s"
                % (rc, res.text.strip()[-300:] or "(no output)"),
                route="tty", sent=(False if rc == 127 else None))
        if rc == 0:
            return ResetOutcome(
                True, ("no other reader on %s" % tty) if mode == "scan" else
                "REBOOT acknowledged on %s: %r" % (tty, info.get("echo", "")[-60:]),
                route="tty", sent=(None if mode == "scan" else True))
        if rc == 5:
            return ResetOutcome(
                True, "REBOOT sent on %s but no 'Rebooting' echo within %.0fs "
                "(got %r); the down edge decides" % (tty, self.ack_s, info.get("echo", "")),
                route="tty", sent=None)
        if rc == 3:
            others = "; ".join("pid %s: %s" % (p, c) for p, c in info.get("others") or [])
            return ResetOutcome(
                False, "refusing the MCC REBOOT: another process reads %s (%s). "
                "NOTHING was sent. A second reader eats the MCC's echo and made "
                "W1's first REBOOT a no-op (a leftover `cat` of tty_00); stop it "
                "and rerun." % (tty, others or "?"),
                fatal=True, route="tty", sent=False)
        if rc == 4:
            return ResetOutcome(
                False, "refusing the MCC REBOOT: %s on %s (got %r). NOTHING was "
                "sent. Either a reader this account cannot see holds the tty (a "
                "root process, or an fpgahub tty share -- then use --mcc-route "
                "fpgahub) or the MCC is not at its Cmd> prompt."
                % (info.get("reason"), tty, info.get("prompt", "")),
                fatal=True, route="tty", sent=False)
        if rc == 2:
            return ResetOutcome(
                False, "%s. The MCC console is root:fpga -- the hub account "
                "needs group fpga (the ssh runner's `sg fpga`, MPS3_HUB_GROUP)."
                % info.get("reason"), fatal=True, route="tty", sent=False)
        return ResetOutcome(False, "MCC writer rc %d: %s" % (rc, info), route="tty")


class MccRebootResetter:
    """``--method mcc``: the MCC REBOOT, by ``route`` (see the module docstring).

    ``auto`` tries fpgahub's ``reset.mcc`` and keeps it only when its reply
    names ``tty_00``; when fpgahub refused (nothing sent) or wrote to another
    ``tty_NN`` (a known no-op), it falls back to the paced write -- for this
    reset and every later one. Before fpgahub is asked, the second-reader scan
    runs anyway (sends nothing): fpgahub's direct path has no such check.
    """

    def __init__(self, runner: Callable[..., RunResult], *, target: str,
                 tty_path: Optional[str] = None, route: str = DEFAULT_MCC_ROUTE,
                 pace_s: float = DEFAULT_MCC_PACE_S,
                 settle_s: float = DEFAULT_MCC_SETTLE_S,
                 ack_s: float = DEFAULT_MCC_ACK_S,
                 python: str = "python3",
                 timeout: float = DEFAULT_RESET_TIMEOUT_S) -> None:
        if route not in MCC_ROUTES:
            raise BootRateInputError("--mcc-route must be one of %s, not %r"
                                     % ("/".join(MCC_ROUTES), route))
        self.route = route
        self.tty = MccTtyResetter(
            runner, tty_path=tty_path or "/dev/%s/tty_00" % target,
            pace_s=pace_s, settle_s=settle_s, ack_s=ack_s, python=python)
        self.hub = HubResetter(runner, target=target, timeout=timeout)
        #: why fpgahub is no longer asked (auto, sticky), or None
        self.fpgahub_skipped: Optional[str] = None

    def describe(self) -> str:
        if self.route == "tty":
            return self.tty.describe()
        fp = "fpgahub target reset %s --method mcc (accepted only if it names tty_00)" \
            % self.hub.target
        if self.route == "fpgahub":
            return fp
        return "%s, else %s" % (fp, self.tty.describe())

    def __call__(self, method: str = MCC_METHOD) -> ResetOutcome:
        if self.route == "tty" or self.fpgahub_skipped:
            out = self.tty(method)
            if self.fpgahub_skipped:
                out = dataclasses.replace(out, detail="%s [fpgahub skipped: %s]"
                                          % (out.detail, self.fpgahub_skipped))
            return out
        scan = self.tty.scan()
        if scan.fatal:
            return scan
        via = self.hub(MCC_METHOD)
        if not scan.ok:
            via = dataclasses.replace(via, detail="%s [second-reader scan could "
                                      "not run: %s]" % (via.detail, scan.detail))
        if via.ok or self.route == "fpgahub" or via.sent is not False:
            return via
        # auto, and fpgahub KNOWN not to have reached the MCC: the paced write.
        self.fpgahub_skipped = via.detail.split("\n")[0][:200]
        out = self.tty(method)
        return dataclasses.replace(out, detail="%s [fpgahub first: %s]"
                                   % (out.detail, self.fpgahub_skipped))


class CommandWitness:
    """Adapt any command that prints ``{"configured": …, "usercode": "0x…"}``
    into the witness seam.

    Deliberately NOT an xsdb recipe: DONE/USERCODE needs ``xsdb`` on the hub and
    a JTAG cable that the swap path also wants, so the readback stays a
    site-supplied command instead of a hardcoded one this repo cannot test.
    """

    def __init__(self, cmd: str, *, timeout: float = 60.0) -> None:
        self.cmd = cmd
        self.timeout = timeout

    def __call__(self) -> Witness:
        proc = subprocess.run(["/bin/sh", "-c", self.cmd], capture_output=True,
                              text=True, timeout=self.timeout)
        raw = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return Witness(configured=None, raw=raw.strip())
        try:
            obj = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            return Witness(configured=None, raw=raw.strip())
        configured = obj.get("configured")
        return Witness(
            configured=(bool(configured) if configured is not None else None),
            usercode=(str(obj["usercode"]) if obj.get("usercode") is not None else None),
            raw=raw.strip(),
        )


# --------------------------------------------------------------------------- #
# the campaign
# --------------------------------------------------------------------------- #


def check_method(method: str) -> None:
    """Raise :class:`BootRateError` for a method that cannot measure anything."""
    reason = REFUSED_METHODS.get(method)
    if reason:
        raise BootRateError("refusing --method %s: %s" % (method, reason))


def _iso(epoch: float) -> str:
    return datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


class BootRateRunner:
    """Reboot N times, count what came back, and write down the configuration."""

    #: Test seam: called as ``(iteration, t_reset_monotonic)`` immediately after
    #: each reset is issued. Nothing in production sets it.
    on_iteration_start: Optional[Callable[[int, float], None]] = None

    def __init__(
        self,
        *,
        probe: Callable[[], Probe],
        resetter: Callable[[str], ResetOutcome],
        lease: Optional[LeaseClient] = None,
        holder: Optional[str] = None,
        runner: Optional[Callable[..., RunResult]] = None,
        mcc_log_path: Optional[str] = None,
        witness: Optional[Callable[[], Witness]] = None,
        host: str = "",
        control_port: int = 6900,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        log: Optional[Callable[[str], None]] = None,
        stage0: Optional[Callable[[], Any]] = None,
        stage0_via: Optional[str] = None,
    ) -> None:
        self._probe = probe
        self._reset = resetter
        self._lease = lease
        self.holder = holder
        self._runner = runner
        self.mcc_log_path = mcc_log_path
        self._witness = witness
        self.host = host
        self.control_port = control_port
        self._sleep = sleep
        self._now = now
        self._wall = wall
        self._log_to = log
        #: ``() -> pyverify.mailbox.Stage0Status`` (Linux only), and a
        #: human-readable name for the record ("ssh devmem via mps3-linux")
        self._stage0 = stage0
        self.stage0_via = stage0_via
        self.on_iteration_start = None

    # -- plumbing ----------------------------------------------------------- #

    def _say(self, msg: str) -> None:
        if self._log_to:
            self._log_to(msg)

    def _require_lease(self) -> str:
        """The board is SHARED. A campaign that reboots it N times must own it,
        and "nobody holds it" is not ownership."""
        if self._lease is None:
            raise BootRateError(
                "no lease client: a boot-rate campaign reboots a SHARED board "
                "N times and must hold the lease")
        if not self.holder:
            raise BootRateError(
                "boot-rate needs an explicit --holder (or MPS3_LEASE_HOLDER): "
                "it has to prove the lease on the board is OURS, and the "
                "per-PID default holder every other verb falls back to can "
                "never match a lease another process took")
        if not self._lease.held_by(self.holder):
            st = self._lease.status()
            raise BootRateError(
                "lease gate: %s is not held by %r (hub says: %s). Take it first "
                "-- `pyverify lease acquire --holder %s` -- this campaign "
                "reboots the board %s times."
                % (self._lease.chassis, self.holder, st.raw or "free",
                   self.holder, "N"))
        return self.holder

    def _heartbeat(self, token: Optional[str], notes: List[str]) -> None:
        if not token or self._lease is None:
            return
        try:
            self._lease.heartbeat(token, holder=self.holder)
        except LeaseError as exc:
            # Never fatal: a campaign that dies at iteration 7 of 10 because a
            # heartbeat 404'd has thrown away the measurement it was making.
            notes.append("lease heartbeat failed (continuing): %s" % exc)

    def _mcc_log_sha(self, notes: List[str]) -> Optional[str]:
        if not self.mcc_log_path:
            return None
        if self._runner is None:
            notes.append("no hub runner: cannot read %s" % self.mcc_log_path)
            return None
        digest = sd_file_sha256(self._runner, self.mcc_log_path)
        if digest.sha256 is None:
            notes.append("MCC log unreadable (%s): %s"
                         % (self.mcc_log_path, digest.reason))
        return digest.sha256

    def _ask_witness(self, notes: List[str]) -> Optional[Witness]:
        if self._witness is None:
            return None
        try:
            return self._witness()
        except Exception as exc:  # noqa: BLE001 - a witness must never fail a run
            notes.append("witness hook failed (ignored): %s" % exc)
            return None

    # -- one iteration ------------------------------------------------------- #

    def _iteration(
        self,
        index: int,
        *,
        method: str,
        deadline_s: float,
        poll_s: float,
        down_deadline_s: float,
        await_down: bool,
        token: Optional[str],
        linux: bool = False,
        expect_slot: Optional[str] = None,
        confirm_grace_s: float = DEFAULT_CONFIRM_GRACE_S,
    ) -> Dict[str, Any]:
        notes: List[str] = []
        self._heartbeat(token, notes)

        pre = self._probe()
        t_reset_wall = self._wall()
        t0 = self._now()

        reset = self._reset(method)
        reset_rec = {"ok": reset.ok, "route": reset.route, "sent": reset.sent,
                     "fatal": reset.fatal, "detail": reset.detail[-600:],
                     "s": round(self._now() - t0, 3)}
        if self.on_iteration_start is not None:
            self.on_iteration_start(index, t0)
        if not reset.ok:
            notes.append("reset refused: %s" % reset.detail)
            return self._record_iteration(
                index, t_reset_wall, None, 0.0, OUTCOME_HUB_ERROR,
                None, None, notes, reason="reset refused: %s" % reset.detail,
                reset=reset_rec)
        self._say("iteration %d: reset issued (%s%s)"
                  % (index, method, ", " + reset.route if reset.route else ""))

        # -- phase 1: the board must actually go DOWN ------------------------ #
        # "Down" is NOTHING answering: on Linux, identify too (stage0's rescue
        # server answers it with no 6900). Bare metal: Probe.alive == up.
        down_seen = False
        if not pre.alive:
            notes.append("already down before the reset: nothing was answering "
                         "when this iteration began, so there is no down edge "
                         "to wait for")
        elif not await_down:
            notes.append(
                "--no-await-down: the first answer is TRUSTED as the new boot, "
                "but it may be the PRE-reboot shell still answering")
        else:
            down_budget = min(down_deadline_s, deadline_s)
            while True:
                if not self._probe().alive:
                    down_seen = True
                    notes.append("down edge seen after %.1fs" % (self._now() - t0))
                    break
                waited = self._now() - t0
                if waited >= down_budget:
                    notes.append(
                        "the shell NEVER stopped answering %.0fs after the "
                        "reset: the reset surface reported ok and rebooted "
                        "nothing (the signature of a REBOOT sent to the wrong "
                        "tty). Recorded as hub-error, NOT as a boot." % waited)
                    return self._record_iteration(
                        index, t_reset_wall, None, waited, OUTCOME_HUB_ERROR,
                        None, self._ask_witness(notes), notes,
                        reason="never went down %.0fs after the reset" % waited,
                        reset=reset_rec)
                self._sleep(max(0.0, min(poll_s, down_budget - waited)))

        # -- phase 2: wait for it to come back ------------------------------- #
        saw_connect = False
        probe: Optional[Probe] = None
        while True:
            probe = self._probe()
            if probe.up:
                waited = self._now() - t0
                t_ping = self._wall()
                outcome, reason, checks, s0 = OUTCOME_UP, None, None, None
                if linux:
                    outcome, reason, checks, s0 = self._linux_verdict(
                        probe, waited, expect_slot=expect_slot,
                        confirm_grace_s=confirm_grace_s, poll_s=poll_s,
                        notes=notes)
                return self._record_iteration(
                    index, t_reset_wall, t_ping, waited, outcome,
                    probe, self._ask_witness(notes), notes, reason=reason,
                    reset=reset_rec, checks=checks, stage0=s0)
            if linux and probe.mode == "rescue":
                # stage0 found nothing to boot and listens forever: final.
                waited = self._now() - t0
                why = (probe.identify or {}).get("reason") or "?"
                if not down_seen and pre.alive:
                    notes.append("no down edge was seen: this rescue may be the "
                                 "PRE-reset state")
                return self._record_iteration(
                    index, t_reset_wall, None, waited, OUTCOME_FAIL,
                    None, None, notes,
                    reason="stage0 rescue (identify mode 'rescue', reason %r) "
                           "%.0fs after the reset: no bootable slot" % (why, waited),
                    reset=reset_rec,
                    checks={"mode": "rescue", "rescue_reason": why})
            saw_connect = saw_connect or probe.connected
            waited = self._now() - t0
            if waited >= deadline_s:
                break
            self._sleep(max(0.0, min(poll_s, deadline_s - waited)))

        witness = self._ask_witness(notes)
        waited = self._now() - t0
        if witness is not None and witness.configured is True:
            notes.append("witness says the FPGA IS configured (usercode %s) but "
                         "nothing answered: configured+inert is a hung image, "
                         "not a dark boot" % (witness.usercode or "?"))
            outcome = OUTCOME_TIMEOUT
            reason = "configured but silent for %.0fs" % waited
        elif saw_connect:
            notes.append("the control channel accepted a connection but never "
                         "reported a shell_id: alive, not converged")
            outcome = OUTCOME_TIMEOUT
            reason = "6900 connected but never answered ping within %.0fs" % waited
        else:
            outcome = OUTCOME_DARK
            reason = "nothing answered%s within the %.0fs deadline" % (
                " (6900 or identify)" if linux else "", deadline_s)
            if witness is None:
                notes.append("no witness configured: 'dark' here means 'nothing "
                             "answered within the deadline', not 'DONE low'")
        return self._record_iteration(index, t_reset_wall, None, waited, outcome,
                                      probe if probe and probe.up else None,
                                      witness, notes, reason=reason,
                                      reset=reset_rec)

    def _linux_verdict(self, probe: Probe, waited: float, *,
                       expect_slot: Optional[str], confirm_grace_s: float,
                       poll_s: float, notes: List[str]):
        """Is the Linux boot that answered the one under test? Returns
        ``(outcome, reason, checks, stage0_summary)``. A check that could not
        be made is ``None`` in ``checks`` and says why in ``notes`` -- never a
        silent pass."""
        stats = probe.stats or {}
        os_ms = stats.get("os_up_ms")
        up_ms = stats.get("up_ms")
        checks: Dict[str, Any] = {"fresh": None, "mode": probe.mode, "slot": None,
                                  "expected_slot": expect_slot, "confirmed": None,
                                  "boot_count": None, "os_up_ms": os_ms,
                                  "up_ms": up_ms}
        # -- fresh: a kernel older than the reset never rebooted ------------ #
        age, which = (os_ms, "os_up_ms") if os_ms is not None else (up_ms, "up_ms")
        if age is None:
            notes.append("stats gave no uptime (%s): freshness unchecked"
                         % (stats.get("error") or "no stats"))
        else:
            checks["fresh"] = age <= waited * 1000.0 + FRESH_SLACK_MS
            if not checks["fresh"]:
                return (OUTCOME_HUB_ERROR,
                        "%s=%d ms but the reset was %.0fs ago: the kernel never "
                        "restarted, so this is not a boot" % (which, age, waited),
                        checks, None)
            if which == "up_ms":
                notes.append("no os_up_ms: freshness judged on harnessd's up_ms")
        # -- identify: run, not rescue ------------------------------------- #
        if probe.mode is None:
            notes.append("identify was silent: mode unchecked")
        elif probe.mode != "run":
            return (OUTCOME_FAIL, "identify mode %r, not 'run'" % probe.mode,
                    checks, None)
        # -- stage0: first try, expected slot, confirmed ------------------- #
        if self._stage0 is None:
            notes.append("no stage0 reader: slot / boot_count / confirm unchecked")
            return OUTCOME_UP, None, checks, None
        grace_end = self._now() + confirm_grace_s
        while True:
            try:
                st = self._stage0()
            except Exception as exc:  # noqa: BLE001 - an unreadable block is unchecked, not a boot failure
                notes.append("stage0 block unreadable (%s): slot / boot_count / "
                             "confirm unchecked" % exc)
                return OUTCOME_UP, None, checks, None
            summ = st.summary()
            if not st.valid:
                return (OUTCOME_FAIL, "stage0 status block invalid (no magic/"
                        "version/size): stage0 did not run this boot", checks, summ)
            f = st.fields
            checks["boot_count"] = f.get("boot_count")
            slot = summ.get("booted_from")
            checks["slot"] = slot
            want = (expect_slot or summ.get("default_slot") or "A").upper()
            checks["expected_slot"] = want
            if checks["boot_count"] != 1:
                return (OUTCOME_FAIL,
                        "stage0 boot_count=%s: it needed more than one entry "
                        "since the reconfiguration (an unconfirmed attempt or a "
                        "watchdog reset before this boot)" % checks["boot_count"],
                        checks, summ)
            if slot not in ("A", "B"):
                return (OUTCOME_FAIL, "stage0 booted_from=%s, not slot A or B"
                        % slot, checks, summ)
            if slot != want:
                return (OUTCOME_FAIL,
                        "booted from slot %s, expected %s (n_fallback=%s, "
                        "fails_a=%s, fails_b=%s)" % (slot, want, f.get("n_fallback"),
                                                     f.get("fails_a"), f.get("fails_b")),
                        checks, summ)
            checks["confirmed"] = st.linux_confirmed
            if st.linux_confirmed:
                return OUTCOME_UP, None, checks, summ
            left = grace_end - self._now()
            if left <= 0:
                return (OUTCOME_FAIL,
                        "Linux never confirmed the boot (att_confirm != 'S0OK') "
                        "%.0fs after 6900 answered" % confirm_grace_s, checks, summ)
            self._sleep(max(0.0, min(poll_s, left)))

    def _record_iteration(self, index, t_reset_wall, t_ping_wall, waited,
                          outcome, probe, witness, notes, *, reason=None,
                          reset=None, checks=None, stage0=None) -> Dict[str, Any]:
        sha = self._mcc_log_sha(notes)
        return {
            "iteration": index,
            "t_reset": _iso(t_reset_wall),
            "t_first_ping": (_iso(t_ping_wall) if t_ping_wall is not None else None),
            "time_to_ping_s": (round(waited, 3) if outcome == OUTCOME_UP else None),
            "waited_s": round(waited, 3),
            "outcome": outcome,
            "reason": reason,
            "static_id": (probe.shell_id if probe is not None and probe.shell_id
                          else None),
            "version": (probe.version if probe is not None else None),
            "mcc_log_sha": sha,
            "witness": (witness.to_json() if witness is not None else None),
            "reset": reset,
            "checks": checks,
            "stage0": stage0,
            "notes": notes,
        }

    # -- the campaign -------------------------------------------------------- #

    def run(
        self,
        *,
        n: int,
        method: str,
        deadline_s: Optional[float] = None,
        poll_s: float = DEFAULT_POLL_S,
        gap_s: float = DEFAULT_GAP_S,
        down_deadline_s: float = DEFAULT_DOWN_DEADLINE_S,
        await_down: bool = True,
        variant: Optional[str] = None,
        token: Optional[str] = None,
        notes: Optional[Sequence[str]] = None,
        linux: Optional[bool] = None,
        expect_slot: Optional[str] = None,
        confirm_grace_s: float = DEFAULT_CONFIRM_GRACE_S,
    ) -> Dict[str, Any]:
        """``deadline_s=None`` = :data:`DEFAULT_DEADLINE_S`, or
        :data:`LINUX_DEADLINE_S` on the Linux harness. ``linux=None`` = decide
        from the board: one probe before the first reset, ``version.impl``."""
        check_method(method)
        if n < 1:
            raise BootRateInputError("--n must be at least 1")
        if poll_s <= 0:
            # The poll interval is what makes the wait loops advance. At 0 they
            # busy-spin on the shell for the whole deadline, and against an
            # injected clock they never advance at all.
            raise BootRateInputError("--poll must be greater than 0")
        if deadline_s is not None and deadline_s <= 0:
            raise BootRateInputError("--deadline must be greater than 0")
        if expect_slot is not None and expect_slot.upper() not in ("A", "B"):
            raise BootRateInputError("--expect-slot must be A or B")
        if await_down and down_deadline_s <= 0:
            # A zero down-window would record EVERY iteration as hub-error
            # ("never stopped answering" at t=0) for anyone who meant "do not
            # wait for a down edge". Say which flag they wanted.
            raise BootRateInputError(
                "--down-deadline must be greater than 0; to skip the down-edge "
                "wait entirely pass --no-await-down (the record then says the "
                "first answer may be the pre-reboot shell)")
        variant_keys: Dict[str, Dict[str, str]] = {}
        variant_desc = None
        if variant:
            delta = variant_delta(variant)      # refuses an unknown name
            variant_keys = delta["keys"]
            variant_desc = delta["description"]
        holder = self._require_lease()

        started = self._wall()
        camp_notes = list(notes or [])
        # -- which engine: it sets the deadline and the boot checks ---------- #
        impl = None
        if linux is None:
            first = self._probe()
            impl = first.impl
            linux = impl == "linux"
            linux_source = "version.impl=%s" % (impl or "?")
            if not first.up:
                camp_notes.append(
                    "the board was not answering when the campaign began, so "
                    "the engine is unknown and treated as bare metal; pass "
                    "--linux for the Linux harness")
        else:
            linux_source = "flag"
        if linux and hasattr(self._probe, "linux"):
            self._probe.linux = True            # identify + stats from the first probe
        if deadline_s is None:
            deadline_s = LINUX_DEADLINE_S if linux else DEFAULT_DEADLINE_S
            deadline_source = ("default for the Linux harness (MCC reload + "
                               "~24 s uSD load + ~20 s to 6900 ~ 100 s)" if linux
                               else "default")
        else:
            deadline_source = "flag"

        iterations: List[Dict[str, Any]] = []
        aborted: Optional[str] = None
        for i in range(n):
            iterations.append(self._iteration(
                i, method=method, deadline_s=deadline_s, poll_s=poll_s,
                down_deadline_s=down_deadline_s, await_down=await_down,
                token=token, linux=bool(linux),
                expect_slot=(expect_slot.upper() if expect_slot else None),
                confirm_grace_s=confirm_grace_s))
            self._say("iteration %d: %s%s" % (
                i, iterations[-1]["outcome"],
                " (%s)" % iterations[-1]["reason"] if iterations[-1].get("reason") else ""))
            reset = iterations[-1].get("reset") or {}
            if reset.get("fatal"):
                # Every further reset would fail the same way (a second reader,
                # the wrong tty): stop, and say so in the record.
                aborted = "stopped after iteration %d of %d: %s" % (
                    i, n, reset.get("detail") or "fatal reset refusal")
                self._say("boot-rate: " + aborted)
                break
            if gap_s and i < n - 1:
                self._sleep(gap_s)

        campaign = {
            "n": n,
            "method": method,
            "deadline_s": deadline_s,
            "poll_s": poll_s,
            "gap_s": gap_s,
            "down_deadline_s": down_deadline_s,
            "await_down": await_down,
            "variant": variant,
            "variant_keys": variant_keys,
            "variant_description": variant_desc,
            "started_at": _iso(started),
            "finished_at": _iso(self._wall()),
            "chassis": (self._lease.chassis if self._lease else None),
            "target": (self._lease.target if self._lease else None),
            "holder": holder,
            "host": self.host,
            "control_port": self.control_port,
            "mcc_log_path": self.mcc_log_path,
            # The COMMAND, when there is one: "CommandWitness" is not a fact
            # anyone can reproduce six weeks later, and which xsdb script read
            # DONE is exactly the sort of thing that goes missing.
            "witness": (getattr(self._witness, "cmd", None)
                        or type(self._witness).__name__) if self._witness else None,
            "tool": "pyverify boot-rate",
            "notes": camp_notes,
            # ADDITIVE (Linux harness / MCC REBOOT)
            "linux": bool(linux),
            "linux_source": linux_source,
            "deadline_source": deadline_source,
            "reset_via": (self._reset.describe() if hasattr(self._reset, "describe")
                          else "fpgahub target reset --method %s" % method),
            "expect_slot": (expect_slot.upper() if expect_slot else None),
            "confirm_grace_s": confirm_grace_s,
            "stage0_via": (self.stage0_via or ("custom" if self._stage0 else None)),
            "aborted": aborted,
        }
        summary = summarise(iterations)
        # The summary is what gets printed, pasted into a report and read six
        # weeks later. A rate without the configuration it was measured in is
        # the "about 1 time in 4" problem all over again, so the three fields
        # that identify the experiment ride along with it.
        summary["variant"] = variant
        summary["method"] = method
        summary["deadline_s"] = deadline_s
        summary["linux"] = bool(linux)
        if aborted:
            summary["aborted"] = aborted
        return {"schema": SCHEMA_ID, "campaign": campaign,
                "iterations": iterations, "summary": summary}


# --------------------------------------------------------------------------- #
# arithmetic
# --------------------------------------------------------------------------- #


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson score interval for ``k`` successes in ``n`` trials.

    Wilson rather than the textbook normal interval because at N=10 and p near
    0 or 1 the normal one runs off the end of the scale and reports impossible
    bounds — precisely the regime this board is in. No SciPy: pyverify has no
    runtime dependencies by design.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / float(n)
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), exact sum (n is tens)."""
    from math import comb

    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(comb(n, i) * p ** i * (1.0 - p) ** (n - i) for i in range(k + 1))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> Tuple[float, float]:
    """The exact (Clopper–Pearson) two-sided ``1 - alpha`` interval for ``k``
    successes in ``n`` trials -- the interval the Linux write-up quotes
    (``docs/writeup/LINUX_HARNESS_2026-09.md`` §7.4: 10/10 gives [69.2%, 100%]).

    Conservative where Wilson is not: it never covers less than 95%, which is
    the honest reading of "k of 10". Both are reported; no SciPy (the beta
    quantiles are found by bisection on the exact binomial tail).
    """
    if n <= 0:
        return (0.0, 1.0)
    half = alpha / 2.0

    def solve(f, target: float) -> float:
        # f is monotone decreasing in p on [0, 1].
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2.0
            if f(mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    # lower: P(X >= k | p) = alpha/2  <=>  cdf(k-1; p) = 1 - alpha/2
    lower = 0.0 if k == 0 else solve(lambda p: _binom_cdf(k - 1, n, p), 1.0 - half)
    # upper: P(X <= k | p) = alpha/2
    upper = 1.0 if k >= n else solve(lambda p: _binom_cdf(k, n, p), half)
    return (lower, upper)


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for the 2x2 table ``[[a, b], [c, d]]``.

    WHY THIS AND NOT "DO THE INTERVALS OVERLAP"
        Overlapping 95% intervals are NOT a test — at N=10 even 3/10 -> 9/10
        overlaps (0.11-0.60 against 0.60-0.98), while the exact test puts that
        same pair at p = 0.020. Reading overlap as "no difference" would have
        the lead discard a genuine fix; the interval is for showing how little
        one campaign pins down, and this is for comparing two.

    Exact enumeration over the hypergeometric distribution — the counts here are
    tens, and pyverify carries no SciPy (values cross-checked against
    ``scipy.stats.fisher_exact`` in the tests).
    """
    from math import comb

    n = a + b + c + d
    if n == 0:
        return 1.0
    row1, col1 = a + b, a + c
    denom = comb(n, col1)
    if denom == 0 or row1 == 0 or row1 == n or col1 == 0 or col1 == n:
        return 1.0

    def prob(x: int) -> float:
        return comb(row1, x) * comb(n - row1, col1 - x) / denom

    lo = max(0, col1 - (n - row1))
    hi = min(row1, col1)
    p_obs = prob(a)
    total = sum(prob(x) for x in range(lo, hi + 1)
                if prob(x) <= p_obs * (1.0 + 1e-9))
    return min(1.0, total)


def summarise(iterations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts, rate, interval and timings.

    THE DENOMINATOR IS THE POINT: a ``hub-error`` iteration is an experiment
    that never ran (the hub refused, or the board never went down), so it is
    excluded from the rate and reported on its own. Counting it as a dark boot
    would report a worse board than we have; counting it as a success, a better
    one.
    """
    counts = {o: 0 for o in OUTCOMES}
    for it in iterations:
        counts[it.get("outcome", OUTCOME_DARK)] = counts.get(
            it.get("outcome", OUTCOME_DARK), 0) + 1
    n = len(iterations)
    valid = n - counts[OUTCOME_HUB_ERROR]
    up = counts[OUTCOME_UP]
    times = [it["time_to_ping_s"] for it in iterations
             if it.get("outcome") == OUTCOME_UP
             and it.get("time_to_ping_s") is not None]
    lo, hi = wilson(up, valid) if valid > 0 else (0.0, 1.0)
    cp_lo, cp_hi = clopper_pearson(up, valid) if valid > 0 else (0.0, 1.0)
    # An `up` whose stage0 checks could not be made (no reader, ssh refused)
    # is still counted -- but the count of them rides with the rate.
    unchecked = sum(1 for it in iterations
                    if it.get("outcome") == OUTCOME_UP
                    and isinstance(it.get("checks"), dict)
                    and it["checks"].get("confirmed") is None)
    return {
        "n": n,
        "valid": valid,
        "up": up,
        "dark": counts[OUTCOME_DARK],
        "timeout": counts[OUTCOME_TIMEOUT],
        "hub_error": counts[OUTCOME_HUB_ERROR],
        "fail": counts[OUTCOME_FAIL],
        "rate": (up / float(valid) if valid > 0 else None),
        "rate_basis": "up / (n - hub_error)",
        "ci95": [lo, hi],
        "ci_method": "Wilson score, 95%",
        "ci95_clopper_pearson": [cp_lo, cp_hi],
        "stage0_unchecked": unchecked,
        "mean_time_to_ping_s": (sum(times) / len(times) if times else None),
        "max_time_to_ping_s": (max(times) if times else None),
    }


# --------------------------------------------------------------------------- #
# the run record: schema, validation, comparison
# --------------------------------------------------------------------------- #

RUN_RECORD_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://soclabs.org/mps3/boot-rate/v1.schema.json",
    "title": "MPS3 boot-rate run record",
    "description": (
        "One boot-rate campaign: N reboots of one board in one recorded SD "
        "configuration. Versioned by `schema`; a record of another version is "
        "refused rather than half-read."
    ),
    "type": "object",
    "required": ["schema", "campaign", "iterations", "summary"],
    "properties": {
        "schema": {"const": SCHEMA_ID},
        "campaign": {
            "type": "object",
            "required": ["n", "method", "deadline_s", "variant", "variant_keys",
                         "started_at", "finished_at"],
            "properties": {
                "n": {"type": "integer", "minimum": 1},
                "method": {"type": "string",
                           "description": "the reset method driven: an fpgahub "
                                          "reset method, or 'mcc' = the MCC "
                                          "REBOOT on tty_00 (see reset_via)"},
                "reset_via": {"type": "string",
                              "description": "how the reset was issued, in words"},
                "linux": {"type": "boolean"},
                "linux_source": {"type": "string"},
                "deadline_source": {"type": "string"},
                "expect_slot": {"type": ["string", "null"]},
                "confirm_grace_s": {"type": "number"},
                "stage0_via": {"type": ["string", "null"]},
                "aborted": {"type": ["string", "null"],
                            "description": "why the campaign stopped early "
                                           "(a fatal reset refusal), or null"},
                "deadline_s": {"type": "number", "exclusiveMinimum": 0},
                "poll_s": {"type": "number"},
                "gap_s": {"type": "number"},
                "down_deadline_s": {"type": "number"},
                "await_down": {"type": "boolean"},
                "variant": {"type": ["string", "null"],
                            "description": "fpga/mps3_sd/variants/<name>, or "
                                           "null for the default bundle"},
                "variant_keys": {
                    "type": "object",
                    "description": "the config-file key deltas that variant "
                                   "applies, READ from the variant directory",
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "started_at": {"type": "string"},
                "finished_at": {"type": "string"},
                "chassis": {"type": ["string", "null"]},
                "target": {"type": ["string", "null"]},
                "holder": {"type": ["string", "null"]},
                "host": {"type": "string"},
                "control_port": {"type": "integer"},
                "mcc_log_path": {"type": ["string", "null"]},
                "witness": {"type": ["string", "null"]},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "iterations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["iteration", "t_reset", "t_first_ping", "outcome",
                             "static_id", "version", "mcc_log_sha", "notes"],
                "properties": {
                    "iteration": {"type": "integer", "minimum": 0},
                    "t_reset": {"type": "string",
                                "description": "UTC ISO-8601 of the reset"},
                    "t_first_ping": {"type": ["string", "null"]},
                    "time_to_ping_s": {"type": ["number", "null"]},
                    "waited_s": {"type": "number"},
                    "outcome": {"enum": list(OUTCOMES)},
                    "static_id": {"type": ["string", "null"]},
                    "version": {"type": ["object", "null"]},
                    "mcc_log_sha": {
                        "type": ["string", "null"],
                        "description": "sha256 of the MCC LOG.TXT read through "
                                       "the SD seam, or null with a note",
                    },
                    "witness": {"type": ["object", "null"]},
                    "reason": {"type": ["string", "null"],
                               "description": "why this boot is not a PASS"},
                    "reset": {"type": ["object", "null"],
                              "description": "ok, route, sent, fatal, detail, s"},
                    "checks": {"type": ["object", "null"],
                               "description": "Linux: fresh, mode, slot, "
                                              "expected_slot, confirmed, "
                                              "boot_count, os_up_ms, up_ms "
                                              "(null = could not be checked)"},
                    "stage0": {"type": ["object", "null"],
                               "description": "the stage0 status block summary"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "summary": {
            "type": "object",
            "required": ["n", "up", "rate", "ci95"],
            "properties": {
                "n": {"type": "integer"},
                "valid": {"type": "integer"},
                "up": {"type": "integer"},
                "dark": {"type": "integer"},
                "timeout": {"type": "integer"},
                "hub_error": {"type": "integer"},
                "fail": {"type": "integer"},
                "rate": {"type": ["number", "null"]},
                "rate_basis": {"type": "string"},
                "ci95": {"type": "array", "items": {"type": "number"},
                         "minItems": 2, "maxItems": 2},
                "ci_method": {"type": "string"},
                "ci95_clopper_pearson": {"type": "array",
                                         "items": {"type": "number"},
                                         "minItems": 2, "maxItems": 2},
                "stage0_unchecked": {"type": "integer"},
                "mean_time_to_ping_s": {"type": ["number", "null"]},
                "max_time_to_ping_s": {"type": ["number", "null"]},
                "variant": {"type": ["string", "null"],
                            "description": "carried here as well as in the "
                                           "campaign: a rate without its "
                                           "configuration is folklore"},
                "method": {"type": "string"},
            },
        },
    },
}


def validate_record(record: Any) -> None:
    """Structural validation against :data:`RUN_RECORD_SCHEMA`, by hand.

    Hand-rolled because pyverify is dependency-free on purpose (it gets
    vendored onto a Pi-5 tender and a Jupyter host with nothing but the
    stdlib). This checks what actually goes wrong — a foreign or future schema,
    a missing block, an outcome outside the enum — not every keyword in the
    schema document.
    """
    if not isinstance(record, dict):
        raise BootRateInputError("run record is not a JSON object")
    schema = record.get("schema")
    if schema != SCHEMA_ID:
        raise BootRateInputError(
            "run record schema %r is not %r: this file was written by a "
            "different version of the tool and its fields cannot be trusted to "
            "mean the same thing" % (schema, SCHEMA_ID))
    for key, kind in (("campaign", dict), ("iterations", list), ("summary", dict)):
        if not isinstance(record.get(key), kind):
            raise BootRateInputError("run record is missing a %s block" % key)
    for key in RUN_RECORD_SCHEMA["properties"]["campaign"]["required"]:
        if key not in record["campaign"]:
            raise BootRateInputError("run record campaign is missing %r" % key)
    required_it = RUN_RECORD_SCHEMA["properties"]["iterations"]["items"]["required"]
    for i, it in enumerate(record["iterations"]):
        if not isinstance(it, dict):
            raise BootRateInputError("iteration %d is not an object" % i)
        for key in required_it:
            if key not in it:
                raise BootRateInputError("iteration %d is missing %r" % (i, key))
        if it["outcome"] not in OUTCOMES:
            raise BootRateInputError(
                "iteration %d has outcome %r, which is not one of %s"
                % (i, it["outcome"], ", ".join(OUTCOMES)))


def load_record(path: "os.PathLike[str] | str") -> Dict[str, Any]:
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        raise BootRateInputError("cannot read %s: %s" % (path, exc)) from exc
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BootRateInputError("%s is not valid JSON: %s" % (path, exc)) from exc
    validate_record(record)
    return record


#: Campaign fields whose change makes two runs a BEFORE and an AFTER.
COMPARED_FIELDS = ("variant", "method", "deadline_s", "await_down", "n")


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Diff two campaigns: the rates, the intervals, and the variable(s) that
    changed between them.

    Names every difference rather than assuming the caller changed only one
    thing — an experiment that moved two variables at once is still worth
    printing, but it must SAY it moved two.
    """
    validate_record(before)
    validate_record(after)
    variables: List[Dict[str, Any]] = []
    for field_name in COMPARED_FIELDS:
        b = before["campaign"].get(field_name)
        a = after["campaign"].get(field_name)
        if b != a:
            variables.append({"name": field_name, "before": b, "after": a})
    b_keys = before["campaign"].get("variant_keys") or {}
    a_keys = after["campaign"].get("variant_keys") or {}
    for fname in sorted(set(b_keys) | set(a_keys)):
        bk = b_keys.get(fname, {})
        ak = a_keys.get(fname, {})
        for key in sorted(set(bk) | set(ak)):
            if bk.get(key) != ak.get(key):
                variables.append({"name": "%s:%s" % (fname, key),
                                  "before": bk.get(key, "(template default)"),
                                  "after": ak.get(key, "(template default)")})
    fields_changed = [v["name"] for v in variables if ":" not in v["name"]]
    key_rows = [v for v in variables if ":" in v["name"]]
    # The same variant NAME on both sides with different keys means the variant
    # directory was edited between the runs: each record describes a card the
    # other one is not talking about, and nothing downstream can see that.
    variant_redefined = bool(key_rows) and "variant" not in fields_changed
    bs, as_ = before["summary"], after["summary"]
    b_ci = bs.get("ci95") or [0.0, 1.0]
    a_ci = as_.get("ci95") or [0.0, 1.0]
    overlap = not (b_ci[1] < a_ci[0] or a_ci[1] < b_ci[0])
    b_up, b_valid = bs.get("up", 0), bs.get("valid", bs.get("n", 0))
    a_up, a_valid = as_.get("up", 0), as_.get("valid", as_.get("n", 0))
    p_value = fisher_exact(b_up, b_valid - b_up, a_up, a_valid - a_up)
    return {
        "schema": SCHEMA_ID,
        "before": bs,
        "after": as_,
        "before_campaign": before["campaign"],
        "after_campaign": after["campaign"],
        "variables": variables,
        "fields_changed": fields_changed,
        "variant_redefined": variant_redefined,
        "overlap": overlap,
        "p_value": p_value,
        "p_method": "Fisher exact, two-sided",
        "alpha": 0.05,
        "significant": bool(p_value < 0.05),
        "delta_rate": (None if bs.get("rate") is None or as_.get("rate") is None
                       else as_["rate"] - bs["rate"]),
    }


def _fmt_rate(summary: Dict[str, Any]) -> str:
    rate = summary.get("rate")
    ci = summary.get("ci95") or [0.0, 1.0]
    if rate is None:
        return "no valid attempts (%d hub-error of %d)" % (
            summary.get("hub_error", 0), summary.get("n", 0))
    return "%.2f  (%d/%d, 95%% CI %.2f-%.2f)" % (
        rate, summary.get("up", 0), summary.get("valid", 0), ci[0], ci[1])


def format_compare(cmp: Dict[str, Any]) -> str:
    """The before/after, in the shape a human reads in a terminal."""
    lines = ["boot rate: before -> after", ""]
    lines.append("  before : %s" % _fmt_rate(cmp["before"]))
    lines.append("  after  : %s" % _fmt_rate(cmp["after"]))
    if cmp.get("delta_rate") is not None:
        lines.append("  delta  : %+.2f" % cmp["delta_rate"])
    lines.append("")
    if not cmp["variables"]:
        lines.append("  variable: NONE recorded -- the two campaigns ran the "
                     "same configuration.")
        lines.append("  This is a REPEATABILITY pair, not a before/after.")
    else:
        lines.append("  variable(s) that changed:")
        for var in cmp["variables"]:
            lines.append("    %-24s %s -> %s"
                         % (var["name"], var["before"], var["after"]))
        # The key rows are the DETAIL of a variant change, not extra variables:
        # `--variant ETH_SMB0` moves one variable and shows two keys, and a tool
        # that nagged about that on every legitimate comparison would be ignored
        # by the third one.
        if len(cmp["fields_changed"]) > 1:
            lines.append("    NOTE: %d campaign settings moved (%s). The rule "
                         "is one variable at a time; read this as a story, not "
                         "a result." % (len(cmp["fields_changed"]),
                                        ", ".join(cmp["fields_changed"])))
        if cmp["variant_redefined"]:
            lines.append("    WARNING: both runs name the SAME variant but its "
                         "keys differ -- the variant directory was edited "
                         "between them, so neither record describes the other's "
                         "card.")
    lines.append("")
    lines.append("  Fisher exact (two-sided): p = %.4g" % cmp["p_value"])
    if cmp["significant"]:
        lines.append("  VERDICT: a real difference at this sample size "
                     "(p < %.2f)." % cmp["alpha"])
        if cmp["overlap"]:
            lines.append("  (The 95% intervals still overlap -- at this N "
                         "they almost always do. The exact test is the "
                         "comparison; the interval only shows how little ONE "
                         "campaign pins down.)")
    else:
        lines.append("  VERDICT: NOT a demonstrated difference (p >= %.2f). "
                     "More iterations, or it is noise." % cmp["alpha"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# evidence files: boot_rate_<YYYYMMDD>.csv + .md
# --------------------------------------------------------------------------- #

#: Where the write-up (docs/writeup/LINUX_HARNESS_2026-09.md §7.3/§7.4) expects
#: the Linux boot count: it runs inside the soak.
EVIDENCE_DIR_HINT = "docs/evidence/2026-09-linux-soak"

EVIDENCE_CSV_COLUMNS = (
    "iteration", "method", "route", "t_reset_utc", "t_reboot_to_6900_s",
    "outcome", "verdict", "slot", "confirmed", "boot_count", "os_up_ms",
    "mode", "reason",
)


def _verdict(outcome: str) -> str:
    if outcome == OUTCOME_UP:
        return "PASS"
    if outcome == OUTCOME_HUB_ERROR:
        return "EXCLUDED"
    return "FAIL"


def evidence_stem(record: Dict[str, Any]) -> str:
    """``boot_rate_<YYYYMMDD>``, the date the campaign STARTED (UTC)."""
    started = str(record.get("campaign", {}).get("started_at") or "")
    if re.match(r"\d{4}-\d{2}-\d{2}", started):
        return "boot_rate_" + started[:10].replace("-", "")
    return "boot_rate_" + datetime.utcnow().strftime("%Y%m%d")


def _rows(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    method = record["campaign"].get("method")
    rows = []
    for it in record["iterations"]:
        checks = it.get("checks") or {}
        reset = it.get("reset") or {}
        t6900 = it.get("time_to_ping_s")
        if t6900 is None and it.get("t_first_ping"):
            t6900 = it.get("waited_s")          # 6900 answered, the checks failed
        confirmed = checks.get("confirmed")
        rows.append({
            "iteration": it.get("iteration"),
            "method": method,
            "route": reset.get("route") or "",
            "t_reset_utc": it.get("t_reset"),
            "t_reboot_to_6900_s": ("" if t6900 is None else "%.1f" % t6900),
            "outcome": it.get("outcome"),
            "verdict": _verdict(it.get("outcome", "")),
            "slot": checks.get("slot") or "",
            "confirmed": ("" if confirmed is None else ("yes" if confirmed else "no")),
            "boot_count": ("" if checks.get("boot_count") is None
                           else checks.get("boot_count")),
            "os_up_ms": ("" if checks.get("os_up_ms") is None else checks.get("os_up_ms")),
            "mode": checks.get("mode") or "",
            "reason": it.get("reason") or "",
        })
    return rows


def evidence_csv(record: Dict[str, Any]) -> str:
    """One row per boot: method, route, reset -> 6900, slot, confirmed, and
    the reason for anything that is not a PASS."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(EVIDENCE_CSV_COLUMNS), lineterminator="\n")
    w.writeheader()
    for row in _rows(record):
        w.writerow(row)
    return buf.getvalue()


def _pct(x: float) -> str:
    return "%.1f%%" % (100.0 * x)


def evidence_md(record: Dict[str, Any], stem: Optional[str] = None) -> str:
    """The human half: the rate with BOTH intervals, the settings, a per-boot
    table, and the README row to paste."""
    stem = stem or evidence_stem(record)
    c, s = record["campaign"], record["summary"]
    up, valid = s.get("up", 0), s.get("valid", 0)
    cp = s.get("ci95_clopper_pearson") or list(clopper_pearson(up, valid))
    wl = s.get("ci95") or list(wilson(up, valid))
    engine = "Linux harness" if c.get("linux") else "bare-metal harness"
    day = stem.split("_")[2] if stem.count("_") >= 2 else ""
    date = "%s-%s-%s" % (day[:4], day[4:6], day[6:8]) if len(day) == 8 else day
    lines = ["# Boot rate: %s, %s" % (engine, date), ""]
    if valid:
        lines.append("**%d of %d** unattended resets came back as the boot under "
                     "test. 95%% Clopper–Pearson interval **[%s, %s]**; Wilson "
                     "score [%s, %s]." % (up, valid, _pct(cp[0]), _pct(cp[1]),
                                          _pct(wl[0]), _pct(wl[1])))
    else:
        lines.append("**No valid attempt** (%d of %d excluded as hub-error): "
                     "no rate." % (s.get("hub_error", 0), s.get("n", 0)))
    if c.get("aborted"):
        lines += ["", "**Stopped early:** %s" % c["aborted"]]
    checks = "ping answered with a shell_id"
    if c.get("linux"):
        checks = ("6900 answered; `stats.os_up_ms` younger than the reset; "
                  "identify `mode:\"run\"`; stage0 block (%s): valid, "
                  "`boot_count == 1`, `booted_from` = %s, `att_confirm == S0OK`"
                  % (c.get("stage0_via") or "not read",
                     c.get("expect_slot") or "the card's default slot"))
    lines += [
        "",
        "| Setting | Value |",
        "|---|---|",
        "| Engine | %s (%s) |" % (engine, c.get("linux_source", "?")),
        "| Reset | `%s`: %s |" % (c.get("method"), c.get("reset_via") or "?"),
        "| Deadline | %s s per boot (%s) |" % (_num(c.get("deadline_s")),
                                             c.get("deadline_source", "?")),
        "| A PASS means | %s |" % checks,
        "| Board | %s / %s, shell %s:%s |" % (c.get("chassis"), c.get("target"),
                                             c.get("host"), c.get("control_port")),
        "| Lease holder | %s |" % c.get("holder"),
        "| SD variant | %s |" % (c.get("variant") or "default bundle"),
        "| Started / finished (UTC) | %s / %s |" % (c.get("started_at"),
                                                   c.get("finished_at")),
        "",
        "## Per boot",
        "",
        "| # | Reset (UTC) | Route | Reset → 6900 (s) | Verdict | Outcome | Slot "
        "| Confirmed | boot_count | Reason |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in _rows(record):
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % tuple(
            (str(r[k]) if r[k] not in ("", None) else "—").replace("|", "\\|")
            for k in ("iteration", "t_reset_utc", "route", "t_reboot_to_6900_s",
                      "verdict", "outcome", "slot", "confirmed", "boot_count",
                      "reason")))
    lines += [
        "",
        "## Counts",
        "",
        "| PASS | FAIL | dark | timeout | excluded (hub-error) | n |",
        "|---|---|---|---|---|---|",
        "| %d | %d | %d | %d | %d | %d |" % (up, s.get("fail", 0), s.get("dark", 0),
                                           s.get("timeout", 0), s.get("hub_error", 0),
                                           s.get("n", 0)),
        "",
        "Rate basis: %s. A hub-error is a reboot that did not happen (the reset "
        "was refused, the board never went down, or the kernel that answered was "
        "older than the reset), so it is left out of the denominator."
        % s.get("rate_basis", "up / (n - hub_error)"),
    ]
    if s.get("stage0_unchecked"):
        lines += ["", "**%d PASS(es) had no stage0 read** (slot / confirm "
                  "unchecked): see the notes in the JSON run record."
                  % s["stage0_unchecked"]]
    if s.get("mean_time_to_ping_s") is not None:
        lines += ["", "Reset → 6900: mean %.1f s, max %.1f s."
                  % (s["mean_time_to_ping_s"], s["max_time_to_ping_s"])]
    row = "| B | boot rate (%s) | **%s** | `%s.csv`, `%s.md` |" % (
        engine, ("%d of %d, 95%% CP [%s, %s]" % (up, valid, _pct(cp[0]), _pct(cp[1]))
                 if valid else "no valid attempt"), stem, stem)
    lines += ["", "## README row", "", "    " + row, "",
              "Written by `pyverify boot-rate` (run record schema `%s`); the "
              "per-boot data is `%s.csv`." % (record.get("schema"), stem), ""]
    return "\n".join(lines)


def _num(x: Any) -> str:
    return ("%g" % x) if isinstance(x, (int, float)) else str(x)


def write_evidence(record: Dict[str, Any], out_dir: "os.PathLike[str] | str"
                   ) -> Tuple[Path, Path]:
    """Write ``boot_rate_<YYYYMMDD>.csv`` + ``.md`` into ``out_dir`` (the
    write-up's placeholder names). Never overwrites: a second campaign on the
    same day gets ``boot_rate_<YYYYMMDD>_2``."""
    validate_record(record)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = evidence_stem(record)
    stem, k = base, 1
    while (out / (stem + ".csv")).exists() or (out / (stem + ".md")).exists():
        k += 1
        stem = "%s_%d" % (base, k)
    csv_path, md_path = out / (stem + ".csv"), out / (stem + ".md")
    csv_path.write_text(evidence_csv(record))
    md_path.write_text(evidence_md(record, stem))
    return csv_path, md_path


# --------------------------------------------------------------------------- #
# the SD-bundle experiment variants
# --------------------------------------------------------------------------- #


def _variants_dir(root: Optional[Path] = None) -> Path:
    from .fielded import repo_root

    return (Path(root) if root else repo_root()) / VARIANTS_REL


def list_variants(root: Optional[Path] = None) -> List[str]:
    base = _variants_dir(root)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and (p / VARIANT_META).is_file())


def _parse_keys(text: str) -> Dict[str, str]:
    """``KEY: VALUE  ;comment`` — the MCC config dialect the templates use."""
    keys: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        keys[key.strip()] = value.split(";")[0].strip()
    return keys


def variant_delta(name: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """What ``fpga/mps3_sd/variants/<name>/`` changes, read from the directory.

    The harness never types the keys into the record: an experiment recorded as
    "ETH_SMB0" whose keys were typed by hand is one rename away from claiming a
    configuration the SD never carried.
    """
    base = _variants_dir(root)
    path = base / name
    known = list_variants(root)
    if not (path.is_dir() and (path / VARIANT_META).is_file()):
        raise BootRateInputError(
            "no such SD variant %r under %s. Known variants: %s"
            % (name, base, ", ".join(known) or "(none)"))
    meta = _parse_keys((path / VARIANT_META).read_text())
    keys: Dict[str, Dict[str, str]] = {}
    for f in sorted(path.iterdir()):
        if f.name == VARIANT_META or not f.is_file() or f.suffix.lower() != ".txt":
            continue
        keys[f.name] = _parse_keys(f.read_text())
    return {
        "name": name,
        "description": meta.get("DESCRIPTION", ""),
        "keys": keys,
        "path": str(path),
    }
