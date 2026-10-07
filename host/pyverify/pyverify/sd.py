"""``pyverify.sd`` — the ONE writer for the MPS3 config SD, with the
one-write-wait discipline encoded instead of written down.

WHAT THIS ENCODES, AND WHAT IT COST TO LEARN
    ``fpgahub target program <target> <bit> --method sd`` (``sd_install``) puts
    ~12 MB across USB mass storage and **always times the client out**. That
    timeout is not a failure: the write is still running on the hub. A retry
    issued mid-write corrupts the SD and darkens the board — it has. The rule,
    from the runbook and from the scar tissue:

        one write, wait ~5 minutes, then reset.

    So :meth:`SdWriter.write`

      * refuses a second write while one is **in flight** (a marker file the
        writer plants and clears; a timed-out first write has RETURNED but the
        hub has not finished, which is exactly when the fatal retry happens);
      * treats the client timeout as an **expected outcome**, recorded, not
        raised;
      * **waits** the documented interval rather than telling the operator to;
      * verifies by **md5 read-back** where a path on the mounted SD is known,
        and says plainly that it did not when there is none — the fpgahub API
        exposes no read-back of its own;
      * gates on a **verified backup**: ``sd_install`` overwrites in place, and
        on 2026-07-16 the previous ``nanosoc.bit`` was overwritten with no
        backup and is gone;
      * gates on the **lease** (``docs/internal/MPS3_BOARD_LEASE.md``); and
      * logs every step, because the operator's only other instrument during a
        five-minute silent write is guesswork.

    It never reports that a shell was FIELDED. ``docs/FIELDED_SHELL.md`` is
    explicit: an ``sd_install`` timing out is not evidence of anything, and the
    only evidence that qualifies is the shell reporting its own id.

    THE CLIENT TIMEOUT, BOTH SHAPES (2026-09-24). The fpgahub client gives up
    after ~30 s by itself and exits 1 with ``POST /targets/<t>/program: timed
    out``; only a hub that hangs longer trips this runner's own timeout. Both
    are the expected outcome. Before this the rc=1 shape raised "sd_install
    failed" -- the message an operator retries on, which is the one thing that
    must never happen. The write passes ``--yes`` (a method with ``confirm =
    true`` needs it) and ``--no-skip-if-loaded`` (the daemon would otherwise
    skip a write it believes already loaded), not ``--force``, which would also
    drop the part check.

    For the WHOLE fielding (the write, fpgahubd's journal witness ``program
    dispatched ... ok=True sha256=<prefix>``, then the paced MCC REBOOT on
    tty_00) use ``pyverify sd field`` (:mod:`pyverify.fielding`). This writer
    only writes and waits; it neither reads the journal nor reboots.

This module runs **on the hub** in the normal case (the config SD is a USB
mass-storage device there and ``sd_install`` runs there): ``bundle`` and
``backup_dir`` are paths on the executing host, while ``verify_path`` is read
back through the same :class:`~pyverify.lease.HubRunner` seam.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Union

from .lease import Lease, LeaseClient, LeaseError, RunResult, hub_runner_from_env

__all__ = ["SdWriteError", "SdWriteResult", "SdWriter", "DEFAULT_WAIT_S",
           "SdFileDigest", "sd_file_sha256", "sd_field_hint"]

#: The documented settle interval after an sd_install. The write takes ~5 min
#: over USB-MSC (docs/ runbook); nothing may touch the board before it ends.
DEFAULT_WAIT_S = 300.0

#: How long the client waits for `target program` before accepting that it has
#: timed out. Generous, but bounded: the point is to stop WAITING, not to stop
#: the write.
DEFAULT_PROGRAM_TIMEOUT_S = 180.0

#: A .bit smaller than this is a failed backup wearing a backup's clothes.
MIN_PLAUSIBLE_BIT = 1_000_000

#: An in-flight marker older than this is assumed abandoned (a killed run),
#: rather than wedging the tool forever.
DEFAULT_STALE_AFTER_S = 1800.0

#: The fpgahub client's own give-up: exit 1, ``POST /targets/<t>/program: timed out``.
_CLIENT_TIMED_OUT = re.compile(r"timed out", re.I)


def sd_field_hint(bundle: "Union[str, Path]", holder: "Optional[str]" = None) -> str:
    """What to run next: the witness + the paced REBOOT (``pyverify sd field``)."""
    return ("the SD write is not a reload: `pyverify sd field %s --already-written "
            "--holder %s` waits for fpgahubd's `program dispatched ... ok=True "
            "sha256=<prefix>` and then sends the paced MCC REBOOT on tty_00. "
            "`pyverify sd field` without --already-written is the whole procedure "
            "(write + witness + REBOOT) in one run."
            % (shlex.quote(str(bundle)), shlex.quote(holder or "<holder>")))


class SdWriteError(Exception):
    """A gate refused, or the write is provably wrong. Never raised for the
    expected client timeout."""


@dataclass(frozen=True)
class SdFileDigest:
    """A read-only digest of one file on the mounted config SD, or the reason
    there isn't one."""

    path: str
    sha256: "Optional[str]"
    reason: "Optional[str]" = None

    @property
    def ok(self) -> bool:
        return self.sha256 is not None


_SHA256_HEX = 64


def sd_file_sha256(
    runner: "Callable[..., RunResult]",
    path: str,
    *,
    timeout: float = 60.0,
) -> SdFileDigest:
    """``sha256sum <path>`` through the hub-command seam — a READ of the config
    SD, the same way :meth:`SdWriter.write` reads back an md5.

    NEVER RAISES. It exists for callers that record evidence rather than gate on
    it (the MCC ``LOG.TXT`` capture in a boot-rate campaign): a mount that came
    back on a different path, or an SD not mounted at all, must cost that
    campaign one null field and a note, not the whole measurement.

    ``path`` is deliberately a caller-supplied path on the EXECUTING host: the
    config SD's mount point is site-specific (fpgahub mounts it transiently for
    ``sd_install``), and a default here would be a site fact wearing a default's
    clothes — the same reason ``write()`` takes ``verify_path``.
    """
    try:
        res = runner(["sha256sum", path], timeout=timeout)
    except TimeoutError as exc:
        return SdFileDigest(path, None, "sha256sum timed out: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - a read of evidence cannot fail a run
        return SdFileDigest(path, None, "cannot run sha256sum: %s" % exc)
    if res.returncode != 0:
        return SdFileDigest(path, None, res.text.strip() or "(no output)")
    token = res.stdout.split()[0] if res.stdout.split() else ""
    if len(token) != _SHA256_HEX or any(c not in "0123456789abcdefABCDEF" for c in token):
        return SdFileDigest(path, None,
                            "sha256sum printed something that is not a digest: %r"
                            % res.stdout.strip())
    return SdFileDigest(path, token.lower())


@dataclass
class SdWriteResult:
    """What the writer did. Deliberately not "what is now on the board"."""

    bundle: Path
    target: str
    timed_out: bool
    waited_s: float
    verified: "Optional[bool]"
    verify_reason: "Optional[str]"
    md5: "Optional[str]"
    backup_dir: "Optional[Path]"
    log: "List[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """The write ran and no gate refused. A client timeout is still ok."""
        return True

    @property
    def fielded(self) -> bool:
        """Always False. Writing the SD is not evidence that the board came up
        on the new shell (docs/FIELDED_SHELL.md, "Updating this file")."""
        return False


class SdWriter:
    """Writes one bundle to the config SD, under the discipline above."""

    def __init__(
        self,
        runner: "Optional[Callable[..., RunResult]]" = None,
        *,
        lease: "Optional[LeaseClient]" = None,
        target: "Optional[str]" = None,
        wait_s: float = DEFAULT_WAIT_S,
        program_timeout_s: float = DEFAULT_PROGRAM_TIMEOUT_S,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        state_dir: "Optional[Union[str, Path]]" = None,
        sleep: "Callable[[float], None]" = time.sleep,
        log: "Optional[Callable[[str], None]]" = None,
    ) -> None:
        self._runner = runner if runner is not None else hub_runner_from_env()
        self._lease = lease if lease is not None else LeaseClient(self._runner, target=target)
        self.target = target or self._lease.target
        self.wait_s = wait_s
        self.program_timeout_s = program_timeout_s
        self.stale_after_s = stale_after_s
        self.state_dir = Path(state_dir) if state_dir else Path(
            os.environ.get("MPS3_STATE_DIR") or tempfile.gettempdir())
        self._sleep = sleep
        self._log_to = log

    # -- the in-flight marker ------------------------------------------------ #

    @property
    def marker_path(self) -> Path:
        return self.state_dir / ("sd_write.%s.inflight" % self.target)

    def _claim(self, bundle: Path, lines: "List[str]") -> None:
        marker = self.marker_path
        if marker.exists():
            age = time.time() - marker.stat().st_mtime
            if age < self.stale_after_s:
                raise SdWriteError(
                    "an SD write is already in flight for %s (%s, %.0fs old). "
                    "sd_install ALWAYS times the client out while the hub keeps "
                    "writing; a second write issued now CORRUPTS the SD and "
                    "darkens the board. Wait for it, then remove %s if you are "
                    "certain it is dead."
                    % (self.target, marker, age, marker)
                )
            lines.append("marker %s is %.0fs old (> %.0fs) -- treating as abandoned"
                         % (marker, age, self.stale_after_s))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pid=%d started=%d bundle=%s\n"
                          % (os.getpid(), int(time.time()), bundle))

    def _clear(self) -> None:
        try:
            self.marker_path.unlink()
        except OSError:
            pass

    # -- the write ----------------------------------------------------------- #

    def write(
        self,
        bundle: "Union[str, Path]",
        *,
        lease: "Optional[Lease]" = None,
        holder: "Optional[str]" = None,
        backup_dir: "Optional[Union[str, Path]]" = None,
        require_backup: bool = True,
        verify_path: "Optional[str]" = None,
        force: bool = False,
    ) -> SdWriteResult:
        """Program ``bundle`` onto the config SD and wait it out."""
        lines: "List[str]" = []

        def say(msg: str) -> None:
            lines.append(msg)
            if self._log_to:
                self._log_to(msg)

        bundle = Path(bundle)
        if not bundle.is_file():
            raise SdWriteError("no such bundle: %s" % bundle)
        src_md5 = hashlib.md5(bundle.read_bytes()).hexdigest()
        say("bundle: %s (%d bytes, md5 %s)" % (bundle, bundle.stat().st_size, src_md5))

        # -- gate: the lease ------------------------------------------------- #
        holder = holder or (lease.holder if lease is not None else None)
        try:
            status = self._lease.status()
        except LeaseError as exc:
            raise SdWriteError("cannot read the board lease: %s" % exc) from exc
        if not status.held or (holder and status.holder != holder):
            raise SdWriteError(
                "lease gate: %s is not held by %r (hub says: %s). This write is "
                "destructive; refusing to touch a board we do not own."
                % (self._lease.chassis, holder, status.raw or "free")
            )
        say("lease: %s held by %s -- ok" % (self._lease.chassis, status.holder))

        # -- gate: a real backup --------------------------------------------- #
        backup = Path(backup_dir) if backup_dir else None
        if backup is None:
            if require_backup:
                raise SdWriteError(
                    "backup gate: sd_install OVERWRITES the config SD in place "
                    "and the previous bitstream is then GONE (2026-07-16). Pass "
                    "a captured backup directory, or waive the gate explicitly."
                )
            say("backup: NO BACKUP -- gate waived by the caller; the current SD "
                "contents will be unrecoverable")
        else:
            found = sorted(p for p in backup.rglob("*") if p.suffix.lower() == ".bit")
            if not found:
                raise SdWriteError(
                    "backup gate: no .bit under %s -- the SD may not have mounted "
                    "as expected; treating as a FAILED backup" % backup
                )
            size = found[0].stat().st_size
            if size < MIN_PLAUSIBLE_BIT:
                raise SdWriteError(
                    "backup gate: %s is only %d B -- implausibly small for a "
                    "bitstream, treating as a failed backup" % (found[0], size)
                )
            say("backup: %s (%d B) + %d file(s) -- ok"
                % (found[0], size, sum(1 for _ in backup.rglob("*"))))

        # -- gate: one write at a time --------------------------------------- #
        if force:
            say("marker: --force, clearing any in-flight marker")
            self._clear()
        self._claim(bundle, lines)

        try:
            # -- program ------------------------------------------------------ #
            from .fielding import program_argv      # ONE spelling of the write
            argv = program_argv(self.target, str(bundle))
            say("program: $ " + " ".join(argv) + "   (ONCE; never retried)")
            timed_out = False
            try:
                res = self._runner(argv, timeout=self.program_timeout_s)
            except TimeoutError as exc:
                timed_out = True
                say("program: client timed out after %.0fs (%s) -- EXPECTED: "
                    "~12 MB over USB-MSC. The hub is still writing; NOT retrying."
                    % (self.program_timeout_s, exc))
            else:
                text = res.text.strip()
                if res.returncode != 0 and _CLIENT_TIMED_OUT.search(text):
                    timed_out = True
                    say("program: the fpgahub client gave up (%s) -- EXPECTED: ~12 MB "
                        "over USB-MSC outlasts its ~30 s window. The hub is still "
                        "writing; NOT retrying." % text.splitlines()[-1])
                elif res.returncode != 0:
                    raise SdWriteError(
                        "program: fpgahub refused or failed the write: %s. NOT "
                        "retried. Before any second write, read fpgahubd's journal "
                        "for an `sd_install: writing` line: a write that started is "
                        "still running (`pyverify sd field` checks this for you)."
                        % (text or "(no output)")
                    )
                else:
                    say("program: returned promptly: %s" % text)

            # -- wait ---------------------------------------------------------- #
            say("wait: settling %.0fs before anything else touches the board "
                "(the write outlives the client)" % self.wait_s)
            self._sleep(self.wait_s)

            # -- verify -------------------------------------------------------- #
            verified: "Optional[bool]" = None
            reason: "Optional[str]" = None
            read_md5: "Optional[str]" = None
            if verify_path:
                res = self._runner(["md5sum", verify_path], timeout=60.0)
                if res.returncode != 0:
                    raise SdWriteError(
                        "verify: cannot read back %s: %s"
                        % (verify_path, res.text.strip())
                    )
                read_md5 = res.stdout.split()[0]
                verified = read_md5 == src_md5
                say("verify: %s md5 %s (source %s)" % (verify_path, read_md5, src_md5))
                if not verified:
                    raise SdWriteError(
                        "verify: md5 MISMATCH -- %s reads %s, the bundle is %s. "
                        "The SD content is NOT what was written; do not reset the "
                        "board onto it." % (verify_path, read_md5, src_md5)
                    )
            else:
                reason = ("no --verify-path: the fpgahub API exposes no SD "
                          "read-back of its own, so this write is UNVERIFIED")
                say("verify: skipped -- " + reason)
        finally:
            self._clear()

        say("done: SD written. This is NOT evidence the board is on the new "
            "shell -- only ping.shell_id (or the CLCD status line) is.")
        say("next: " + sd_field_hint(bundle, holder))
        return SdWriteResult(
            bundle=bundle, target=self.target, timed_out=timed_out,
            waited_s=self.wait_s, verified=verified, verify_reason=reason,
            md5=read_md5, backup_dir=backup, log=lines,
        )
