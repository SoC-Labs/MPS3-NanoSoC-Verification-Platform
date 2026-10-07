"""``FakeHub`` — an in-process double for the ``fpgahub`` CLI on the hub host.

WHY A CLI DOUBLE AND NOT AN HTTP ONE
    ``fpgahub`` exposes an mTLS HTTP API (``/targets/…``, ``/boards/…``) *and* a
    CLI that speaks it. Every proven interaction this repo has with the hub goes
    through ``ssh <hub> fpgahub …`` — the board's dataplane is reachable only
    from the hub, and the client there is the CLI. Faking the HTTP layer would
    fake a surface we do not drive, and would not catch the failures that have
    actually bitten us, all of which are CLI-response-shape failures:

      * a QUEUED acquire that carries a ``position`` and **no token**
        (``scripts/mps3_lease_acquire.sh``'s header, verified 2026-07-21) — the
        response ``mps3_board.sh`` assumed always had a token, so a contended
        acquire errored out AND stranded a queue entry;
      * ``release`` replying ``no lease to release`` and leaving the board HELD
        when ``--holder`` does not match;
      * ``lease show <member-board>`` answering 404 while
        ``lease show <chassis>`` answers — two real names, different verbs;
      * ``target program --method sd`` ALWAYS timing the client out (12 MB over
        USB-MSC), which is NOT a failure.

    So this double models the CLI's stdout/stderr/exit code per verb, and the
    tests drive it through the same ``HubRunner`` seam the real code uses.

Nothing here opens a socket, an ssh connection, or a file.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pyverify.lease import RunResult


def _md5_of(path: str) -> str:
    """md5 of a real file, so a read-back check that should pass, passes."""
    import hashlib
    from pathlib import Path as _P
    p = _P(path)
    if not p.is_file():
        return "f" * 32
    return hashlib.md5(p.read_bytes()).hexdigest()


@dataclass
class _Held:
    holder: str
    token: str
    user: str = "tester"
    expires_at: str = "2026-01-01T00:00:00Z"


@dataclass
class FakeHub:
    """A hub with ONE board: a chassis (``chassis``) whose single member is the
    lease target (``target``).

    Scripted behaviours (all observed on the real hub):

    ``queue_before_granting``
        how many ``lease acquire`` calls answer ``queued position=N`` before
        one is granted. 0 => granted immediately.
    ``sd_program_times_out``
        ``target program --method sd`` raises the client timeout instead of
        returning (the documented, expected outcome).
    ``reset_fails``
        ``target reset`` exits non-zero (an unwired method, a busy board) —
        what a boot-rate campaign must record as ``hub-error`` rather than as a
        dark boot.
    """

    chassis: str = "mps3"
    target: str = "mps3_pl"
    queue_before_granting: int = 0
    sd_program_times_out: bool = True
    #: the REAL client's shape of the same thing: it gives up after ~30 s by
    #: itself and exits 1 with ``POST /targets/<t>/program: timed out``
    #: (W1 2026-09-24) -- still NOT a failure; the hub keeps writing
    sd_client_gives_up: bool = False
    #: the sd method has ``confirm = true``: without ``--yes`` the daemon
    #: answers HTTP 409 confirm_required and writes nothing
    sd_requires_confirm: bool = False
    #: the SD comes back with DIFFERENT content than was written
    sd_corrupts: bool = False
    held: Optional[_Held] = None
    queued_holders: List[str] = field(default_factory=list)
    #: every argv the code under test asked us to run, in order
    calls: List[List[str]] = field(default_factory=list)
    #: files "on the SD", by basename -> md5 the hub would report
    sd_files: Dict[str, str] = field(default_factory=dict)
    #: files readable ON THE HUB by FULL PATH -> contents. This is the config
    #: SD as a mounted filesystem (`sha256sum /mnt/…/LOG.TXT`), which is a
    #: different surface from `sd_files` above (what `sd_install` believes it
    #: wrote, by basename).
    sd_host_files: Dict[str, str] = field(default_factory=dict)
    #: `target reset` exits non-zero
    reset_fails: bool = False
    #: every `target reset --method M` this hub was asked for, in order
    resets: List[str] = field(default_factory=list)
    next_token: str = "TOK-0001"
    _acquires: int = 0

    # -- the HubRunner seam ------------------------------------------------- #

    def __call__(self, argv, timeout=None):  # noqa: D401 - it IS the runner
        self.calls.append(list(argv))
        return self.run(list(argv))

    # -- dispatch ----------------------------------------------------------- #

    def run(self, argv: "List[str]") -> RunResult:
        # Plain hub-side shell tools (the SD read-back) are not fpgahub verbs.
        if argv[:1] == ["md5sum"]:
            return self._md5(argv[1:])
        if argv[:1] == ["sha256sum"]:
            return self._sha256(argv[1:])
        if argv[:1] != ["fpgahub"]:
            return RunResult(127, "", "fpgahub: command not found")
        rest = argv[1:]
        if rest[:2] == ["lease", "acquire"]:
            return self._acquire(rest[2:])
        if rest[:2] == ["lease", "release"]:
            return self._release(rest[2:])
        if rest[:2] == ["lease", "cancel"]:
            return self._cancel(rest[2:])
        if rest[:2] == ["lease", "heartbeat"]:
            return self._heartbeat(rest[2:])
        if rest[:2] == ["lease", "show"]:
            return self._show(rest[2:])
        if rest[:2] == ["target", "program"]:
            return self._program(rest[2:])
        if rest[:2] == ["target", "reset"]:
            return self._reset(rest[2:])
        return RunResult(2, "", "Usage: fpgahub ... (no such command: %s)" % " ".join(rest))

    # -- verbs -------------------------------------------------------------- #

    @staticmethod
    def _opt(args, name, default=None):
        if name in args:
            return args[args.index(name) + 1]
        return default

    def _acquire(self, args) -> RunResult:
        name = args[0]
        holder = self._opt(args, "--holder", "unknown")
        if name != self.target:
            return RunResult(1, "", "Error: HTTP 404: no such board %s" % name)
        self._acquires += 1
        if self._acquires <= self.queue_before_granting:
            if holder not in self.queued_holders:
                self.queued_holders.append(holder)
            return RunResult(
                0,
                "queued position=%d queue=interactive\n"
                % (self.queued_holders.index(holder) + 1),
                "",
            )
        if self.held is not None and self.held.holder != holder:
            if holder not in self.queued_holders:
                self.queued_holders.append(holder)
            return RunResult(0, "queued position=1 queue=interactive\n", "")
        if holder in self.queued_holders:
            self.queued_holders.remove(holder)
        self.held = _Held(holder=holder, token=self.next_token)
        return RunResult(
            0,
            "granted token=%s expires=%s tier=interactive\n"
            % (self.held.token, self.held.expires_at),
            "",
        )

    def _release(self, args) -> RunResult:
        name = args[0]
        holder = self._opt(args, "--holder")
        token = self._opt(args, "--token")
        if name != self.target:
            return RunResult(1, "", "Error: HTTP 404: no such board %s" % name)
        # The real hub declines a release whose holder does not match, and
        # leaves the board HELD (memory: "token alone replies 'no lease to
        # release'").
        if self.held is None or self.held.token != token or self.held.holder != holder:
            return RunResult(0, "no lease to release\n", "")
        self.held = None
        return RunResult(0, "released %s\n" % name, "")

    def _cancel(self, args) -> RunResult:
        name = args[0]
        holder = self._opt(args, "--holder")
        if name != self.target:
            return RunResult(1, "", "Error: HTTP 404: no such board %s" % name)
        if holder in self.queued_holders:
            self.queued_holders.remove(holder)
            return RunResult(0, "cancelled board=%s\n" % name, "")
        return RunResult(0, "no matching wait to cancel board=%s\n" % name, "")

    def _heartbeat(self, args) -> RunResult:
        name = args[0]
        token = self._opt(args, "--token")
        holder = self._opt(args, "--holder")
        if self.held is None or self.held.token != token or self.held.holder != holder:
            return RunResult(1, "", "Error: HTTP 409: not the lease holder")
        return RunResult(0, "extended expires=%s\n" % self.held.expires_at, "")

    def _show(self, args) -> RunResult:
        name = args[0]
        # THE NAME TRAP, as MEASURED against the live hub 2026-09-11: `show`
        # takes the TARGET name (mps3_pl). The chassis is the 404, and the
        # daemon says WHICH names it knows. This fake used to model the exact
        # opposite, which is worse than not modelling it at all: lease.py and
        # this file agreed with each other and both disagreed with the daemon,
        # so every test passed while `status()` would have 404'd on the board.
        # A caller that treats a 404 as "free" drives an already-held board.
        if name != self.target:
            return RunResult(
                1, "",
                "Error: HTTP 404: no such board: '%s'; configured: %s"
                % (name, self.target),
            )
        if self.held is None:
            return RunResult(0, "not leased\n", "")
        return RunResult(
            0,
            "held by %s (user %s, expires %s)\n"
            % (self.held.holder, self.held.user, self.held.expires_at),
            "",
        )

    def _program(self, args) -> RunResult:
        name = args[0]
        path = args[1]
        method = self._opt(args, "--method", "default")
        if name != self.target:
            return RunResult(1, "", "Error: HTTP 404: no such board %s" % name)
        if self.held is None:
            return RunResult(1, "", "Error: HTTP 409: board is not leased by you")
        if method == "sd":
            if self.sd_requires_confirm and "--yes" not in args:
                return RunResult(1, "", "HTTP 409: confirm_required: program method "
                                        "'sd' on %s needs --yes\n" % name)
            self.sd_files[path.rsplit("/", 1)[-1]] = (
                "0" * 32 if self.sd_corrupts else _md5_of(path)
            )
            if self.sd_client_gives_up:
                return RunResult(1, "", "POST /targets/%s/program: timed out\n" % name)
            if self.sd_program_times_out:
                raise TimeoutError("client timed out after 120s")
            return RunResult(0, "ok programmed %s via sd\n" % path, "")
        return RunResult(0, "ok programmed %s via %s\n" % (path, method), "")

    def _reset(self, args) -> RunResult:
        """``fpgahub target reset <name> --method <m> --yes``.

        The real CLI prints ``ok <name> reset method=… plugin=…`` and, for a
        method that dispatched and failed, ``failed …`` — with exit 0 either
        way. So a caller that reads only the exit code cannot tell them apart,
        which is why the resetter reads the line too.
        """
        name = args[0]
        method = self._opt(args, "--method", "default")
        if name != self.target:
            return RunResult(1, "", "Error: HTTP 404: no such board %s" % name)
        self.resets.append(method)
        if self.reset_fails:
            return RunResult(1, "", "Error: HTTP 409: reset method %r not "
                                    "available on %s" % (method, name))
        return RunResult(0, "ok %s reset method=%s plugin=mps3_msd_reboot\n"
                            % (name, method), "")

    def _sha256(self, args) -> RunResult:
        """A read of the MOUNTED config SD by full path (the MCC LOG.TXT
        capture), as opposed to `_md5`'s by-basename view of what sd_install
        believes it wrote."""
        import hashlib

        path = args[-1]
        if path not in self.sd_host_files:
            return RunResult(1, "", "sha256sum: %s: No such file or directory" % path)
        digest = hashlib.sha256(self.sd_host_files[path].encode()).hexdigest()
        return RunResult(0, "%s  %s\n" % (digest, path), "")

    def _md5(self, args) -> RunResult:
        """The SD read-back. Only files the hub believes it wrote are there."""
        target = args[-1].rsplit("/", 1)[-1]
        if target not in self.sd_files:
            return RunResult(1, "", "md5sum: %s: No such file or directory" % args[-1])
        return RunResult(0, "%s  %s\n" % (self.sd_files[target], args[-1]), "")

    # -- assertions helpers -------------------------------------------------- #

    def command_lines(self) -> "List[str]":
        return [" ".join(shlex.quote(a) for a in c) for c in self.calls]
