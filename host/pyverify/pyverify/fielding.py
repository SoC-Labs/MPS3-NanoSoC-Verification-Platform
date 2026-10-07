"""``pyverify.fielding`` -- put a new MCC config-SD image on the MPS3 with nobody
at the board: ONE SD write, fpgahubd's journal witness, then the paced MCC
REBOOT on ``tty_00``. ``pyverify sd field`` is the CLI.

THE SEQUENCE, AND WHAT EACH RULE COST (ILA handoff 2026-09-24 §3/§4.6,
FINDINGS_FOR_LINUX_HARNESS #1, FLOW_CONTRACT §5, ROLLBACK R2/R3)
    1. **ONE write**: ``fpgahub target program <target> <bit> --method sd --yes
       --no-skip-if-loaded``. The client ALWAYS times out (~30 s; 12 MB over
       USB mass storage) while the daemon keeps writing (~68 s). That timeout is
       recorded as expected and the write is NEVER re-issued: a second write
       mid-install corrupted the card and darkened the board (2026-07-18).
       ``--no-skip-if-loaded`` stops the daemon from skipping the write when it
       believes the image is already loaded; unlike ``--force`` it keeps the
       part check.
    2. **The witness**: fpgahubd's journal line
       ``program dispatched: board=<target> method=sd plugin=sd_install ok=True
       part=... sha256=<first 12 hex of the bit's sha256> dur=...s``, polled
       with a deadline (default 300 s). No REBOOT on ``ok=False`` (the card may
       be half-written), on another sha256, on ``program skipped`` (nothing was
       written), on a second SD write for the board in the window, or when no
       line arrives by the deadline. A REBOOT sent while ``sd_install`` was
       still writing was echoed and ignored (``v011_card_boot_20260924.txt``).
    3. **The paced REBOOT** with exactly ONE reader on ``tty_00``: the hub-side
       writer :data:`pyverify.bootrate.HUB_MCC_REBOOT_PY` refuses when another
       process names or holds the tty, refuses when a bare CR does not bring
       back an intact ``Cmd>``, then waits 1 s, sends ``R E B O O T`` at 100
       ms/char and a CR, waits for ``Rebooting``, and (by default) captures the
       MCC boot log up to ``FPGA configuration complete``. fpgahub's own
       ``reset --method mcc`` is never used: the handoff found it sends a burst
       the MCC drops.

    Every gate that can be checked before the card is touched is checked first:
    the lease is held by ``holder``, the bit is readable on the hub (its sha256
    is the witness's key), the journal is readable, no SD write is already in
    flight, and nothing else reads ``tty_00``. A refusal there has written
    nothing and sent nothing.

READING THE JOURNAL FROM A NON-ROOT LOGIN
    Default: ``journalctl -u fpgahubd --since @<hub epoch> --no-pager -o
    short-iso``, run through the same hub-command seam as every fpgahub call
    (``ssh <hub> "sg fpga -c '...'"`` from a dev box; direct when
    ``MPS3_ON_HUB=1``). On 2026-09-08 a non-root operator login read fpgahubd's
    journal without sudo (EL8 grants system-journal read to the ``wheel``,
    ``adm`` and ``systemd-journal`` groups), and every runbook since has used
    the same command. The run PROBES it before the write (``-n 1``): if no
    fpgahubd entry is visible it refuses, having written nothing, and names the
    three ways out:

    * ``--journal-cmd 'sudo -n journalctl'`` -- a NOPASSWD sudo rule;
    * ``--journal-file PATH`` -- a file on the hub that a privileged
      ``journalctl -u fpgahubd -f -o short-iso > PATH`` is appending to, started
      before this run. Lines already in the file are history; later ones (and,
      when timestamped, only ones at or after the run's start) count;
    * ``--journal-file -`` -- stdin, e.g. ``sudo journalctl -u fpgahubd -f -o
      short-iso | pyverify sd field ... --journal-file -``. Only lines with an
      exact timestamp (``short-iso``, ``short-iso-precise``, ``short-unix``) at
      or after the start count: ``-f`` replays old lines first, and an old
      ``ok=True`` for the same sha256 must never be taken for this write's.

AFTER A REFUSAL
    ``--already-written`` skips step 1 and looks for the witness in the last
    ``since_s`` (default 20 min): the recovery when the REBOOT was refused (a
    second reader, fixed since), when the write was done by hand (``sudo
    fpgahub target program ...``, W1's form), or when the witness arrived after
    the deadline. It still refuses unless the LAST SD event for the board is an
    ``ok=True`` dispatch of this bit's sha256.

It never reports that a shell was FIELDED: the journal proves the card holds the
bytes and the MCC log proves the FPGA configured from it. Only the shell (or
stage0) reporting its own ``static_id`` proves what runs.
"""
from __future__ import annotations

import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Any, Callable, Dict, List, Optional, Sequence

from .lease import LeaseClient, LeaseError, RunResult
from .sd import sd_file_sha256

__all__ = [
    "DEFAULT_WITNESS_DEADLINE_S",
    "DEFAULT_POLL_S",
    "DEFAULT_PROGRAM_TIMEOUT_S",
    "DEFAULT_INFLIGHT_WINDOW_S",
    "DEFAULT_SINCE_S",
    "DEFAULT_CAPTURE_S",
    "JOURNAL_UNIT",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "EXIT_HUB",
    "EXIT_NO_WITNESS",
    "EXIT_REBOOT",
    "JournalError",
    "JournalEvent",
    "Witness",
    "CommandJournal",
    "FileJournal",
    "StreamJournal",
    "FieldResult",
    "SdFielder",
    "line_epoch",
    "parse_events",
    "judge",
    "program_argv",
]

#: The write takes ~68 s (W1: 68.4 s and 69.9 s); five minutes is the
#: runbooks' "wait 5 min" with room for a slow card.
DEFAULT_WITNESS_DEADLINE_S = 300.0
DEFAULT_POLL_S = 10.0
#: How long the hub command may run before the runner gives up waiting. The
#: fpgahub client gives up on its own after ~30 s; this is only a backstop.
DEFAULT_PROGRAM_TIMEOUT_S = 120.0
#: Before writing: an ``sd_install: writing`` for the board this recent with no
#: dispatch line after it is a write in flight.
DEFAULT_INFLIGHT_WINDOW_S = 900.0
#: ``--already-written``: how far back the witness may be.
DEFAULT_SINCE_S = 1200.0
#: The MCC boot log after the REBOOT (the soak's ``--mcc-capture``).
DEFAULT_CAPTURE_S = 150.0
JOURNAL_UNIT = "fpgahubd"

EXIT_OK = 0            # written, witnessed, REBOOT acknowledged (+ configuration complete)
EXIT_REFUSED = 1       # a gate refused BEFORE the write: nothing written, nothing sent
EXIT_USAGE = 2         # bad input
EXIT_HUB = 3           # the hub is unreachable or unconfigured
EXIT_NO_WITNESS = 4    # write issued (or refused by the daemon) but no ok=True witness: NO REBOOT
EXIT_REBOOT = 5        # witnessed; the REBOOT was refused, unacknowledged, or did not configure


class JournalError(Exception):
    """The journal could not be read (a command that failed, a missing file)."""


# --------------------------------------------------------------------------- #
# journal lines
# --------------------------------------------------------------------------- #

_TS_UNIX = re.compile(r"^(\d{9,11}(?:\.\d+)?)\s")
_TS_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})\s")

_DISPATCH = re.compile(
    r"program dispatched: board=(?P<board>\S+) method=(?P<method>\S+) "
    r"plugin=(?P<plugin>\S+) ok=(?P<ok>\w+)(?P<rest>.*)$")
_SKIPPED = re.compile(r"program skipped: (?P<rest>.*)$")
_WRITING = re.compile(r"sd_install: writing (?P<src>\S+) -> (?P<dst>\S+)")
_SHA = re.compile(r"\bsha256=(?P<sha>[0-9a-fA-F]+)")
_DUR = re.compile(r"\bdur=(?P<dur>[0-9.]+)s")
_BOARD = re.compile(r"\bboard=(?P<board>\S+)")

#: What journalctl says when this account may not read the system journal.
_PERM_HINTS = ("not seeing messages from other users", "insufficient permissions",
               "No journal files were opened", "Permission denied")

#: The fpgahub client's own give-up (``POST /targets/<t>/program: timed out``).
_CLIENT_TIMEOUT = re.compile(r"timed out", re.I)


def line_epoch(line: str) -> Optional[float]:
    """The exact time of a journal line, or None: ``short-unix``
    (``1727163417.123456 host ...``) and ``short-iso``/``short-iso-precise``
    (``2026-09-24T08:36:57[.123456]+0100 host ...``). The default ``short``
    format has no year or zone and is deliberately NOT parsed."""
    m = _TS_UNIX.match(line)
    if m:
        return float(m.group(1))
    m = _TS_ISO.match(line)
    if not m:
        return None
    tz = m.group(3)
    tz = "+0000" if tz == "Z" else tz.replace(":", "")
    try:
        dt = datetime.strptime(m.group(1) + tz, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return dt.timestamp() + (float(m.group(2)) if m.group(2) else 0.0)


@dataclass(frozen=True)
class JournalEvent:
    """One SD-relevant fpgahubd line for the board."""

    kind: str                       # "writing" | "dispatched" | "skipped"
    line: str
    ok: Optional[bool] = None
    sha12: Optional[str] = None
    dur_s: Optional[float] = None
    ts: Optional[float] = None


def parse_events(lines: Sequence[str], target: str) -> List[JournalEvent]:
    """The SD events for ``target``, in journal order: ``sd_install: writing``
    into ``/sd/<target>/``, ``program dispatched: board=<target> method=sd``,
    and ``program skipped ... board=<target>``. Everything else is dropped --
    another board's writes, a JTAG program of this one."""
    out: List[JournalEvent] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        ts = line_epoch(line)
        m = _DISPATCH.search(line)
        if m:
            if m.group("board") != target or m.group("method") != "sd":
                continue
            sha = _SHA.search(m.group("rest"))
            dur = _DUR.search(m.group("rest"))
            out.append(JournalEvent(
                "dispatched", line, ok=(m.group("ok") == "True"),
                sha12=(sha.group("sha").lower() if sha else None),
                dur_s=(float(dur.group("dur")) if dur else None), ts=ts))
            continue
        m = _SKIPPED.search(line)
        if m:
            board = _BOARD.search(m.group("rest"))
            if not board or board.group("board") != target:
                continue
            sha = _SHA.search(m.group("rest"))
            out.append(JournalEvent("skipped", line,
                                    sha12=(sha.group("sha").lower() if sha else None), ts=ts))
            continue
        m = _WRITING.search(line)
        if m and ("/%s/" % target) in m.group("dst"):
            out.append(JournalEvent("writing", line, ts=ts))
    return out


def _sha_matches(logged: Optional[str], full: str) -> bool:
    """fpgahub logs ``fingerprint[:12]`` (the whole file's sha256)."""
    return bool(logged) and len(logged) >= 8 and full.lower().startswith(logged.lower())


@dataclass(frozen=True)
class Witness:
    """The journal's verdict on the SD write."""

    status: str
    detail: str
    event: Optional[JournalEvent] = None

    #: states that end the wait
    TERMINAL = ("ok", "failed", "mismatch", "skipped", "ambiguous", "timeout", "unreadable")

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def terminal(self) -> bool:
        return self.status in self.TERMINAL


def judge(events: Sequence[JournalEvent], sha256: str, *,
          single_write: bool = True) -> Witness:
    """What the SD events say about a write of the bit with ``sha256``.

    The LAST event decides: a ``writing`` with nothing after it is a write in
    flight; a dispatch of this sha256 with ``ok=True`` is the witness. With
    ``single_write`` (this run issued the write), a second completed SD event
    for the board in the window is somebody else's write racing ours, and
    nothing about the card can be trusted."""
    if not events:
        return Witness("pending", "no sd_install line for the board yet")
    done = [e for e in events if e.kind != "writing"]
    if single_write and len(done) > 1:
        return Witness("ambiguous",
                       "%d SD writes for the board finished in the window; this run "
                       "issued ONE. Another writer raced it: the card holds whichever "
                       "finished last. Lines: %s" % (len(done), " | ".join(e.line for e in done)),
                       done[-1])
    last = events[-1]
    if last.kind == "writing":
        return Witness("in-flight", "sd_install is writing: %s" % last.line, last)
    if last.kind == "skipped":
        if _sha_matches(last.sha12, sha256):
            return Witness("skipped", "the daemon SKIPPED the write (it believed this "
                           "image already loaded): nothing was written to the card. %s"
                           % last.line, last)
        return Witness("mismatch", "the last SD event is a skip of ANOTHER image "
                       "(sha256=%s, expected %s): %s" % (last.sha12, sha256[:12], last.line), last)
    if not _sha_matches(last.sha12, sha256):
        return Witness("mismatch", "the card was last written with ANOTHER image: "
                       "sha256=%s, this bit is %s. %s" % (last.sha12, sha256[:12], last.line), last)
    if not last.ok:
        return Witness("failed", "ok=False: the write failed and the card may be "
                       "half-written. %s" % last.line, last)
    return Witness("ok", last.line, last)


def program_argv(target: str, bit: str) -> List[str]:
    """The ONE write (ILA handoff §3 + ``--no-skip-if-loaded``, B2 step 1)."""
    return ["fpgahub", "target", "program", target, bit, "--method", "sd", "--yes",
            "--no-skip-if-loaded"]


# --------------------------------------------------------------------------- #
# journal sources
# --------------------------------------------------------------------------- #


def _entry_lines(text: str) -> List[str]:
    """journalctl's entries, without its ``-- Logs begin ... --`` /
    ``-- No entries --`` banners."""
    return [ln for ln in (text or "").splitlines()
            if ln.strip() and not ln.startswith("-- ")]


class CommandJournal:
    """``journalctl -u <unit> ... -o short-iso`` through the hub-command seam."""

    def __init__(self, runner: Callable[..., RunResult], *, cmd: str = "journalctl",
                 unit: str = JOURNAL_UNIT, timeout: float = 60.0) -> None:
        self._runner = runner
        self.prefix = shlex.split(cmd) or ["journalctl"]
        self.unit = unit
        self.timeout = timeout

    def argv(self, since: Optional[float] = None, n: Optional[int] = None) -> List[str]:
        argv = self.prefix + ["-u", self.unit, "--no-pager", "-o", "short-iso"]
        if since is not None:
            argv += ["--since", "@%d" % int(since)]
        if n is not None:
            argv += ["-n", str(n)]
        return argv

    def describe(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv(since=0)).replace("@0", "@<start>")

    def _run(self, argv: List[str]) -> RunResult:
        try:
            return self._runner(argv, timeout=self.timeout)
        except TimeoutError as exc:
            raise JournalError("`%s` timed out: %s" % (" ".join(argv), exc)) from exc
        except (LeaseError, OSError) as exc:
            raise JournalError("cannot run `%s`: %s" % (" ".join(argv), exc)) from exc

    def probe(self) -> Optional[str]:
        """None when at least one ``unit`` entry is visible to this account."""
        argv = self.argv(n=1)
        try:
            res = self._run(argv)
        except JournalError as exc:
            return str(exc)
        if res.returncode != 0:
            return "`%s` exited %d: %s" % (" ".join(argv), res.returncode,
                                           res.text.strip()[-300:] or "(no output)")
        if _entry_lines(res.stdout):
            return None
        hint = [ln.strip() for ln in res.text.splitlines()
                if any(h in ln for h in _PERM_HINTS)]
        return ("no %s entries are visible to this account (%s)"
                % (self.unit, "journalctl: " + " ".join(hint) if hint else
                   "a wrong --journal-unit, or no read access to the system journal"))

    def mark(self) -> None:
        """Nothing to remember: ``--since`` separates history from this run."""

    def lines_since(self, since: float) -> List[str]:
        argv = self.argv(since=since)
        res = self._run(argv)
        if res.returncode != 0:
            raise JournalError("`%s` exited %d: %s" % (" ".join(argv), res.returncode,
                                                       res.text.strip()[-300:]))
        return _entry_lines(res.stdout)


class FileJournal:
    """A journal capture file ON THE HUB, read through the seam (``cat``)."""

    def __init__(self, runner: Callable[..., RunResult], path: str, *,
                 timeout: float = 60.0) -> None:
        self._runner = runner
        self.path = path
        self.timeout = timeout
        self._mark = 0

    def describe(self) -> str:
        return "the capture file %s (lines after the start, or timestamped at/after it)" \
            % self.path

    def _read(self) -> List[str]:
        try:
            res = self._runner(["cat", self.path], timeout=self.timeout)
        except (TimeoutError, LeaseError, OSError) as exc:
            raise JournalError("cannot read %s: %s" % (self.path, exc)) from exc
        if res.returncode != 0:
            raise JournalError("cannot read %s: %s" % (self.path, res.text.strip()))
        return _entry_lines(res.stdout)

    def probe(self) -> Optional[str]:
        try:
            self._read()
        except JournalError as exc:
            return str(exc)
        return None

    def mark(self) -> None:
        self._mark = len(self._read())

    def lines_since(self, since: float) -> List[str]:
        lines = self._read()
        start = self._mark if len(lines) >= self._mark else 0   # truncated: all new
        out = []
        for i, line in enumerate(lines):
            ts = line_epoch(line)
            if (ts is not None and ts >= since - 1.0) or (ts is None and i >= start):
                out.append(line)
        return out


class StreamJournal:
    """Journal lines arriving on a stream (stdin). Only lines with an EXACT
    timestamp at or after the start count (see the module docstring)."""

    def __init__(self, stream: IO[str]) -> None:
        self._lines: List[str] = []
        self._lock = threading.Lock()
        self.untimed = 0
        self.eof = False
        self._t = threading.Thread(target=self._pump, args=(stream,), daemon=True,
                                   name="journal-stdin")
        self._t.start()

    def _pump(self, stream: IO[str]) -> None:
        for line in stream:
            with self._lock:
                self._lines.append(line.rstrip("\r\n"))
        self.eof = True

    def describe(self) -> str:
        return "stdin (exactly-timestamped lines only: journalctl -o short-iso)"

    def probe(self) -> Optional[str]:
        return None

    def mark(self) -> None:
        """Nothing to remember: the timestamp decides."""

    def lines_since(self, since: float) -> List[str]:
        with self._lock:
            lines = list(self._lines)
        out, untimed = [], 0
        for line in lines:
            ts = line_epoch(line)
            if ts is None:
                untimed += 1
            elif ts >= since - 1.0:
                out.append(line)
        self.untimed = untimed
        return out


# --------------------------------------------------------------------------- #
# the fielding
# --------------------------------------------------------------------------- #


@dataclass
class FieldResult:
    """Where the run stopped, why, and everything it saw."""

    exit_code: int
    stage: str
    reason: str
    record: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


class SdFielder:
    """ONE SD write -> the journal witness -> the paced MCC REBOOT.

    ``runner`` is the hub-command seam (:class:`~pyverify.lease.HubRunner`);
    ``journal`` one of the sources above; ``mcc`` a
    :class:`~pyverify.bootrate.MccTtyResetter` (None: write and witness only);
    ``now``/``sleep`` the clock (a five-minute wait is not a unit test)."""

    def __init__(self, runner: Callable[..., RunResult], *, target: str,
                 lease: LeaseClient, holder: str, journal: Any,
                 mcc: Any = None,
                 witness_deadline_s: float = DEFAULT_WITNESS_DEADLINE_S,
                 poll_s: float = DEFAULT_POLL_S,
                 program_timeout_s: float = DEFAULT_PROGRAM_TIMEOUT_S,
                 inflight_window_s: float = DEFAULT_INFLIGHT_WINDOW_S,
                 now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._runner = runner
        self.target = target
        self.lease = lease
        self.holder = holder
        self.journal = journal
        self.mcc = mcc
        self.witness_deadline_s = witness_deadline_s
        self.poll_s = poll_s
        self.program_timeout_s = program_timeout_s
        self.inflight_window_s = inflight_window_s
        self._now = now
        self._sleep = sleep
        self._log = log
        #: how many times this object issued ``target program`` (never > 1)
        self.writes_issued = 0

    def _say(self, msg: str) -> None:
        if self._log:
            self._log("sd field: " + msg)

    def _hub_epoch(self) -> int:
        try:
            res = self._runner(["date", "+%s"], timeout=30.0)
        except (TimeoutError, LeaseError, OSError) as exc:
            raise LeaseError("cannot read the hub's clock: %s" % exc) from exc
        text = res.stdout.strip()
        if res.returncode != 0 or not text.isdigit():
            raise LeaseError("cannot read the hub's clock (`date +%%s` -> rc %d: %r)"
                             % (res.returncode, res.text.strip()))
        return int(text)

    def field(self, bit: str, *, expect_sha256: Optional[str] = None,
              already_written: bool = False, since_s: float = DEFAULT_SINCE_S,
              reboot: bool = True) -> FieldResult:
        rec: Dict[str, Any] = {
            "ok": False, "target": self.target, "bit": bit, "holder": self.holder,
            "mode": "already-written" if already_written else "write",
            "journal": self.journal.describe(), "sha256": None, "hub_t0": None,
            "write": None, "witness": None, "reboot": None, "fielded": False,
        }

        def stop(code: int, stage: str, reason: str, nxt: str) -> FieldResult:
            rec.update(ok=(code == EXIT_OK), exit=code, stage=stage, reason=reason, next=nxt)
            self._say(("DONE: " if code == EXIT_OK else "STOP: ") + reason)
            if nxt:
                self._say("next: " + nxt)
            return FieldResult(code, stage, reason, rec)

        rerun = "pyverify sd field %s --already-written --holder %s" % (
            shlex.quote(bit), shlex.quote(self.holder))

        # -- gate: the hub's clock (every journal window is on it) ------------ #
        try:
            t0 = self._hub_epoch()
        except LeaseError as exc:
            return stop(EXIT_HUB, "preflight", str(exc), "check MPS3_HUB / --hub")
        rec["hub_t0"] = t0

        # -- gate: the lease ----------------------------------------------------- #
        try:
            st = self.lease.status()
        except LeaseError as exc:
            return stop(EXIT_HUB, "preflight", "cannot read the board lease: %s" % exc, "")
        if not st.held or st.holder != self.holder:
            nxt = "acquire the lease as %s, or pass the holder that has it" % self.holder
            if st.held and st.holder and "@" in st.holder:
                # fpgahub >= 0.3.0 records the caller's principal (user@host) and
                # ignores --holder at acquire, so the name to pass is the one shown.
                nxt = ("fpgahub 0.3.0 records the lease as %s and ignores the acquire's "
                       "--holder: if that lease is yours, re-run with --holder %s"
                       % (st.holder, shlex.quote(st.holder)))
            return stop(EXIT_REFUSED, "preflight",
                        "lease gate: %s is not held by %r (hub says: %s). Writing the "
                        "config SD and rebooting a board we do not own is refused; "
                        "nothing was written." % (self.target, self.holder, st.raw or "free"),
                        nxt)
        self._say("lease: %s held by %s -- ok" % (self.target, st.holder))

        # -- gate: the bit, and the sha256 the witness is keyed on --------------- #
        digest = sd_file_sha256(self._runner, bit)
        if not digest.ok:
            return stop(EXIT_REFUSED, "preflight",
                        "cannot read %s on the hub (%s); fpgahubd needs it and the "
                        "witness is keyed on its sha256. Nothing was written."
                        % (bit, digest.reason), "stage the bit on the hub first")
        sha = digest.sha256 or ""
        rec["sha256"] = sha
        if expect_sha256 and not sha.startswith(expect_sha256.lower()):
            return stop(EXIT_REFUSED, "preflight",
                        "%s has sha256 %s, not the expected %s: the wrong file is "
                        "staged. Nothing was written." % (bit, sha, expect_sha256.lower()),
                        "re-stage the bit and compare with linux_bundle.json")
        self._say("bit: %s sha256 %s (the journal will log %s)" % (bit, sha, sha[:12]))

        # -- gate: the journal is readable (before the card is touched) --------- #
        why = self.journal.probe()
        if why:
            return stop(EXIT_REFUSED, "preflight",
                        "cannot read fpgahubd's journal: %s. Without it the write "
                        "cannot be witnessed, so nothing was written." % why,
                        "give the account journal read (an admin: `sudo usermod -aG "
                        "systemd-journal <user>`, then log in again), or pass "
                        "--journal-cmd 'sudo -n journalctl', or --journal-file with a "
                        "privileged `journalctl -u fpgahubd -f -o short-iso` capture")

        # -- gate: no SD write in flight ------------------------------------------ #
        if not already_written:
            try:
                before = parse_events(self.journal.lines_since(t0 - self.inflight_window_s),
                                      self.target)
            except JournalError as exc:
                return stop(EXIT_REFUSED, "preflight", "cannot read fpgahubd's journal: "
                            "%s. Nothing was written." % exc, "")
            if before and before[-1].kind == "writing":
                return stop(EXIT_REFUSED, "preflight",
                            "an SD write for %s is IN FLIGHT (%s) with no `program "
                            "dispatched` line after it. A second write now corrupts the "
                            "card; nothing was written." % (self.target, before[-1].line),
                            "wait for its dispatch line; if it is this bit's, run `%s`"
                            % rerun)

        # -- gate: exactly one reader on tty_00 (checked before the write) ------ #
        if reboot and self.mcc is not None:
            scan = self.mcc.scan()
            if not scan.ok or scan.fatal:
                return stop(EXIT_REFUSED, "preflight",
                            "the MCC REBOOT could not follow the write: %s. Nothing was "
                            "written." % scan.detail,
                            "stop the other tty_00 reader (`ps -eo pid,user,args | grep "
                            "-F tty_00`; `fpgahub share stop` for a share), then re-run")
            self._say("mcc: %s" % scan.detail)

        # -- the ONE write --------------------------------------------------------- #
        if already_written:
            since = t0 - since_s
            rec["write"] = {"issued": False, "why": "--already-written: the witness is "
                            "looked for in the last %.0f s" % since_s}
            self._say("write: skipped (--already-written); witness window %.0f s" % since_s)
        else:
            since = t0
            self.journal.mark()          # a capture file's history ends here
            argv = program_argv(self.target, bit)
            self._say("write: $ %s   (ONCE; the client times out, the hub keeps "
                      "writing; NEVER retried)" % " ".join(argv))
            self.writes_issued += 1
            wrec: Dict[str, Any] = {"issued": True, "argv": argv}
            rec["write"] = wrec
            try:
                res = self._runner(argv, timeout=self.program_timeout_s)
            except TimeoutError as exc:
                wrec.update(outcome="client-timeout", text=str(exc))
            except (LeaseError, OSError) as exc:
                wrec.update(outcome="not-run", text=str(exc))
                return stop(EXIT_NO_WITNESS, "write",
                            "could not run the write command (%s). NOT retried; NO "
                            "REBOOT." % exc,
                            "read the journal for an sd_install line before anything "
                            "else: %s" % self.journal.describe())
            else:
                text = res.text.strip()
                wrec.update(rc=res.returncode, text=text[-500:])
                if res.returncode == 0:
                    wrec["outcome"] = "returned"
                elif _CLIENT_TIMEOUT.search(text):
                    wrec["outcome"] = "client-timeout"
                else:
                    wrec["outcome"] = "refused"
            if wrec["outcome"] == "client-timeout":
                self._say("write: the client timed out -- EXPECTED (12 MB over USB-MSC). "
                          "The hub is still writing. NOT retrying.")
            elif wrec["outcome"] == "refused":
                started = []
                try:
                    started = [e for e in parse_events(self.journal.lines_since(since),
                                                       self.target) if e.kind == "writing"]
                except JournalError:
                    pass
                return stop(EXIT_NO_WITNESS, "write",
                            "fpgahub refused or failed the write: %s. %s NOT retried; NO "
                            "REBOOT." % (wrec["text"] or "(no output)",
                                         "An sd_install DID start (%s): the card is being "
                                         "written." % started[-1].line if started else
                                         "No sd_install line since the start: nothing "
                                         "was written."),
                            "wait for the dispatch line, then `%s`" % rerun if started else
                            "fix the cause (HTTP 400 no USB mass-storage device = the "
                            "hub-path pin; HTTP 409 = the lease), then re-run")
            else:
                self._say("write: the client returned: %s" % (wrec.get("text") or "")[:200])

        # -- the witness ------------------------------------------------------------ #
        self._say("witness: polling %s for `program dispatched: board=%s method=sd ... "
                  "ok=True ... sha256=%s` (deadline %.0f s)"
                  % (self.journal.describe(), self.target, sha[:12], self.witness_deadline_s))
        t_start = self._now()
        deadline = t_start + self.witness_deadline_s
        verdict = Witness("pending", "not polled yet")
        polls = 0
        while True:
            polls += 1
            try:
                events = parse_events(self.journal.lines_since(since), self.target)
            except JournalError as exc:
                verdict = Witness("unreadable", str(exc))
                break
            verdict = judge(events, sha, single_write=not already_written)
            if verdict.terminal:
                break
            if self._now() >= deadline:
                if verdict.status == "in-flight":
                    why = ("sd_install started (%s) and there is still no `program "
                           "dispatched` line after %.0f s: the write may still be "
                           "running" % (verdict.event.line if verdict.event else "?",
                                        self.witness_deadline_s))
                else:
                    why = ("no sd_install or `program dispatched` line for %s in %.0f s"
                           % (self.target, self.witness_deadline_s))
                untimed = getattr(self.journal, "untimed", 0)
                if untimed:
                    why += ("; %d stdin line(s) had no exact timestamp and were "
                            "ignored (use journalctl -o short-iso)" % untimed)
                verdict = Witness("timeout", why, verdict.event)
                break
            self._sleep(max(0.0, min(self.poll_s, deadline - self._now())))
        waited = self._now() - t_start
        ev = verdict.event
        rec["witness"] = {"status": verdict.status, "detail": verdict.detail,
                          "line": ev.line if ev else None,
                          "sha12": ev.sha12 if ev else None,
                          "dur_s": ev.dur_s if ev else None,
                          "waited_s": round(waited, 1), "polls": polls}
        if not verdict.ok:
            nxt = {
                "failed": "do NOT reboot; the daemon has finished, so ONE new write is "
                          "safe (ROLLBACK R2 writes 0x72BB0A36 back): re-run without "
                          "--already-written",
                "timeout": "do NOT retry the write and do NOT reboot. Re-read the "
                           "journal (%s); when it shows ok=True sha256=%s, run `%s`"
                           % (self.journal.describe(), sha[:12], rerun),
                "mismatch": "do NOT reboot: find out which image the card holds "
                            "(the line above) before anything else",
                "ambiguous": "do NOT reboot: another writer raced this one; find it",
                "skipped": "nothing was written; re-run (the write passes "
                           "--no-skip-if-loaded, so a skip means another program call)",
                "unreadable": "the journal became unreadable mid-run; read it by hand "
                              "before anything else",
            }.get(verdict.status, "")
            return stop(EXIT_NO_WITNESS, "witness",
                        "no ok=True witness (%s): %s. The REBOOT was NOT sent."
                        % (verdict.status, verdict.detail), nxt)
        self._say("witness: %s" % verdict.detail)

        # -- the paced REBOOT --------------------------------------------------------- #
        if not reboot or self.mcc is None:
            rec["reboot"] = {"sent": False, "why": "--no-reboot"}
            rec["ok"] = True
            return stop(EXIT_OK, "witness",
                        "SD written and witnessed (%s); no REBOOT (--no-reboot). The "
                        "board runs the OLD image until the next MCC reload."
                        % verdict.detail,
                        "the paced REBOOT: `%s`" % rerun)
        self._say("reboot: %s" % self.mcc.describe())
        out = self.mcc()
        info = getattr(self.mcc, "last_info", None) or {}
        capture = bool(getattr(self.mcc, "capture_s", 0))
        rrec = {"ok": out.ok, "sent": out.sent, "fatal": out.fatal, "detail": out.detail,
                "ack": info.get("ack"), "configuring": info.get("configuring"),
                "complete": info.get("complete"), "failed": info.get("failed"),
                "log": info.get("log"), "tail": info.get("tail")}
        rec["reboot"] = rrec
        if out.sent is False or (not out.ok and out.fatal):
            return stop(EXIT_REBOOT, "reboot",
                        "the card is written and witnessed, but the REBOOT was NOT "
                        "sent: %s" % out.detail,
                        "fix it (one tty_00 reader, an intact Cmd>), then `%s`" % rerun)
        if not out.ok or out.sent is None:
            return stop(EXIT_REBOOT, "reboot",
                        "the REBOOT was not acknowledged: %s" % out.detail,
                        "an unacknowledged REBOOT did not happen (B2 step 2): check "
                        "`pyverify version`/identify in 2 min; if the old shell still "
                        "answers, `%s` ONCE; twice -> a person power-cycles" % rerun)
        if capture and not info.get("complete"):
            return stop(EXIT_REBOOT, "reboot",
                        "REBOOT acknowledged, but the MCC log has no `FPGA configuration "
                        "complete`%s: the SD image may not configure. Log tail: %r"
                        % (" (it reports a configuration FAILURE)" if info.get("failed")
                           else " within the capture", (info.get("tail") or "")[-200:]),
                        "ROLLBACK_RUNBOOK_LINUX.md R2, or a person at the board")
        rec["ok"] = True
        return stop(EXIT_OK, "done",
                    "SD written (%s), REBOOT acknowledged on %s%s. NOT yet evidence of "
                    "what runs: only the shell's/stage0's static_id is."
                    % (verdict.detail, self.mcc.tty_path,
                       ", FPGA configuration complete" if capture else ""),
                    "check the identity: `pyverify version`/`ping` (bare metal) or "
                    "`pyverify identify` (Linux/stage0)")
