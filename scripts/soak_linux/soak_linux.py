#!/usr/bin/env python3
"""soak_linux.py -- hub-side soak loops for the MPS3 Linux harness (mps3-harnessd on the
MicroBlaze V), with a dry run against pyverify's FakeShell(profile="linux").

Runs ON THE HUB (the board's 192.168.10.x link is reachable only from there) under the
hub's python3.11, with pyverify staged beside this file (or PYVERIFY_DIR set). Opening
the MCC console needs group `fpga`: launch it under `sg fpga -c "..."`.
Plan: docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md §4/§5; study:
docs/planning/LINUX_SPEEDUP_2026-09-24.md §3; runbook: docs/planning/B2_RUNBOOK_LINUX.md.

MODES
  accel   THE CUTOVER GATE (study §3, default 24 h). Three phases:
            A  hours 0-6    drills: the 13-RM sweep, the W1 regression subset, every
                            set_clk preset (LOCKED, then back to the default), 10
                            power-on boots (paced MCC REBOOT), mps3-reboot every 2 h,
                            a WDOG trip (kill -STOP harnessd) every 3 h, the slot-A
                            fallback drill
            U  hours 6-18   continuous uptime: no resets; the leak window
            B  hours 18-24  drills: set_clk once more, MCC REBOOT every 2 h,
                            mps3-reboot every 2 h, WDOG trip every 3 h
          The load runs in every phase: a swap every 3 min (all RMs in turn), an SSH
          login every minute, ping+stats every minute, 6900/6910 connection churn
          every minute, an XVC + a JTAG session every hour, a resource sample
          (harnessd RSS and fds, MemAvailable, Slab, SUnreclaim, file handles,
          /persist, kernel oops count) every 5 min.
  tail    the plan's 72 h soak, run after cutover: ping+stats and a sample every
          5 min, one swap and one SSH login per hour, no drills.
  boots   N power-on boots to SSH on their own (the 10/10 outside a soak).
  mcc-reboot
          ONE paced MCC REBOOT on tty_00 with the boot log captured: the method that
          was used to field 0x72BB0A36 on 2026-09-24 with no hands (exactly one reader on
          tty_00, a bare CR, then R-E-B-O-O-T one character per 100 ms and a CR). It
          refuses, sending nothing, when another process reads the tty (rc 2) or when
          the bare CR brings back no intact Cmd> (rc 3). It does NOT wait for the SD
          write's journal witness: after an SD write use `pyverify sd field`.

NETBOOT (--netboot IMAGE; accel, tail and boots): for a board whose user microSD is
  present but unreadable (2026-09-27: "card error" in stage0, mmc_spi -110 in Linux),
  so EVERY reset -- power-on, mps3-reboot, WDOG trip, the reboot verb -- lands stage0
  in rescue. After each reset the soak, not the operator, brings the board back:
    1. identify says mode "rescue" -> ONE push of IMAGE with STAGE0's stage0_push.py
       (--stage0-push; ~55 s for the 29 MB image). A failed push is FAIL "netboot";
    2. wait for 6900 (the deadline restarts at the push: --reset-deadline more s);
    3. claim the fresh, unclaimed RAM Linux: `pyverify claim --key <--claim-key>`
       (only once identify says mode "run": a claim sent to stage0 is taken for a
       boot image). /persist is tmpfs, so there are no keys and a NEW host key every
       boot: ssh runs with StrictHostKeyChecking=no + UserKnownHostsFile=/dev/null
       (still key-only, BatchMode);
    4. the first ssh login, the boot-health verdict, then a DELIBERATE DEVIATION,
       recorded as one: `echo spiX.Y > /sys/bus/spi/drivers/mmc_spi/unbind` (the
       device named by dmesg's "mmc_spi spiX.Y: SD/MMC host"), because mmc_spi
       retries the dead card every ~2.3 s forever and that would pollute the leak
       and load criteria. Only then does the load resume.
  Expectations change with it: every boot is booted_from RESCUE; a warm reset moves
  boot_count and n_boot_rescue by +1 and nothing else (n_boot_a/b, n_fallback);
  boot-health says healthy=0 persist=tmpfs reasons=usd-driver-path (the card), and
  harnessd confirms ONLY healthy boots, so the check becomes: Linux did NOT confirm,
  and the next entry judged that rescue attempt UNCONFIRMED (a card that is pulled
  out instead gives healthy=1: confirmed, as a slot boot). The slot-A fallback drill
  is dropped (slot A is on the card) and the verdict says "skipped". Unrequested
  resets are still FAILs (rescue); with --keep-going the soak pushes once more so
  the run can go on. If the board sits in rescue at the start, the soak pushes first.

DEBUG PORTS ON A CLAIMED BOARD (every mode): a claimed harnessd serves XVC 2542 and
  JTAG 6921 to the board itself only (xvc_lock). The hourly XVC/JTAG sessions, the W1
  JTAG check and --jtag-cmd therefore go through pyverify's SshTunnel to the board's
  127.0.0.1 whenever identify says claimed (--debug-via auto). --jtag-cmd gets the
  tunnel's end as {jtag_host}/{jtag_port} (and $MPS3_JTAG_HOST/$MPS3_JTAG_PORT).

STOP RULES (each is a FAIL event; forensics are collected; exit 1 unless --keep-going)
  offline / wedged    6900 refuses / accepts but never answers, after retries. 6900,
                      6910, XVC and JTAG serve ONE client and turn an extra one away
                      (accept, then EOF or RST) -- often our own previous connection
                      not yet reaped: every control/debug use retries that "busy"
                      refusal (BUSY_GAPS_S, ~21 s) and records a "busy" event with
                      `ss -tnp` of who holds the port and our own sockets (<= 1/min);
                      a reboot verb is never sent twice (its reply lost = wait_back
                      decides). Only a persistent refusal is a FAIL
  identity            ping.shell_id is not --expect-static-id
  unexplained_reset   stats.os_up_ms went backwards outside a drill
  harnessd_respawn    stats.up_ms went backwards while os_up_ms kept counting
  rm_changed          ping.rm_id is not what the last verified swap loaded
  swap / ssh          a swap did not verify / an SSH login failed, after retries
  stage0              the status block is invalid, booted from the wrong slot, was not
                      confirmed by Linux (netboot + the dead card: WAS confirmed),
                      or counted a reset nobody asked for
  rescue              identify says stage0 is in rescue (netboot: outside a reset the
                      soak asked for)
  netboot             the rescue push failed, the RAM image fell back into rescue
                      before 6900, the claim did not take (or someone else's key
                      holds the board), or the mmc_spi unbind failed
  reset_timeout       a requested reset never happened, or the board never came back
  kernel_oops         dmesg shows Oops / BUG: / Unable to handle / Kernel panic
  debug / regress / drill   an XVC or JTAG session (direct, or its ssh tunnel), a W1
                      subtest or a drill step failed
  crash               an exception the soak did not expect (a soak bug): still a
                      FAIL with forensics, the verdict and the summary files
  lease               the lease heartbeat failed (INCONCLUSIVE, not FAIL)
End-of-run criteria (accel): leaks flat over phase U, swap p99 stable across phase U,
coverage minimums met. Each is printed with its numbers.

EXIT  0 PASS · 1 FAIL · 2 usage/setup error · 3 INCONCLUSIVE (stopped early, lease
      lost, coverage or a criterion could not be evaluated)

DRY RUN
  --dry-run runs the mode against FakeShell(profile="linux") with synthetic overlays,
  an in-process fake ssh (uptime, boot-health, a resource sample, devmem of a simulated
  stage0 block, kill -STOP, mps3-reboot, the slot-A corruption), fake XVC/JTAG servers
  (claim-locked: a non-tunnelled session on the claimed board is refused, as on
  silicon) behind a fake `ssh -L`, and simulated power cycles, on a compressed clock.
  With --netboot every reset parks the Sim in stage0 rescue until the REAL
  stage0_push.py pushes it a synthetic S0LB image (IMAGE, when it exists, is only
  checked); the RAM boot comes up unclaimed with a new host key and the dead card's
  boot-health, and the REAL `pyverify claim` claims it. --dry-run-inject KIND@SECONDS
  shows each stop rule: hang kill reset respawn reflash swapfail sshfail rescue noreset
  leak oops unlock, and for netboot pushfail (stage0 rejects the push) and nocard (the
  card is pulled: later boots are healthy and confirmed); ctlreset (the next 2 control
  connections are refused with an RST, as a busy single-client 6900 does: the soak
  must retry and pass), ctlreset_all (every one: FAIL wedged), dbgbusy (the next 2
  XVC/JTAG connections close unanswered: retried), crash (an exception nobody
  expects: FAIL crash, with forensics and the summary).

EXAMPLES (on the hub; PY=/usr/bin/python3.11)
  $PY soak_linux.py accel --dry-run                        # ~7 min, compressed 1/200
  $PY soak_linux.py accel --dry-run --dry-run-inject hang@20
  $PY soak_linux.py accel --dry-run --netboot linux_slot.img   # the netboot rehearsal
  sg fpga -c "$PY soak_linux.py mcc-reboot --log mcc.log"
  sg fpga -c "$PY soak_linux.py accel --expect-static-id 0x44EE76D5 \\
      --expect-ver32 0x01000000 --overlay ovl/<rm> ... \\
      --ssh-opt=-i --ssh-opt=$HOME/.ssh/mps3_soak_ed25519 \\
      --netboot linux_slot.img --claim-key keys.pub --stage0-push stage0_push.py \\
      --heartbeat-cmd '...'"                           # the 24 h gate, dead card
"""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import select
import shlex
import signal
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent


def _board_tty(lane: int) -> str:
    """fpgahub's host port for FPGA UART lane ``lane`` (mirrors
    ``pyverify.lease.board_tty``, inlined: ``mcc-reboot`` needs no pyverify):
    ``$MPS3_TTY_DIR/tty_NN``, else ``/dev/$MPS3_LEASE_TARGET/tty_NN`` (neutral
    default target ``mps3_pl``)."""
    root = os.environ.get("MPS3_TTY_DIR") or "/dev/" + (os.environ.get("MPS3_LEASE_TARGET") or "mps3_pl")
    return "%s/tty_%02d" % (root.rstrip("/"), lane)


def _find_pyverify() -> Path:
    """The directory that CONTAINS the ``pyverify`` package: $PYVERIFY_DIR, this
    script's own directory (the hub staging layout), or the repo's host/pyverify.
    ``mcc-reboot`` never calls this: the rollback path needs no pyverify at all."""
    cands = []
    if os.environ.get("PYVERIFY_DIR"):
        cands.append(Path(os.environ["PYVERIFY_DIR"]))
    cands += [HERE, HERE.parents[1] / "host" / "pyverify"]
    for c in cands:
        if (c / "pyverify" / "__init__.py").is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            return c
    raise SystemExit("soak_linux: cannot find the pyverify package; set PYVERIFY_DIR to the "
                     "directory that contains pyverify/ (tried %s)" % ", ".join(map(str, cands)))


PV_DIR: Optional[Path] = None


class _Unloaded(Exception):
    """Placeholder until :func:`load_pyverify` binds the real pyverify names."""


ShellChannelClosed = MailboxError = IdentifyError = SshTunnelError = _Unloaded  # load_pyverify()
ShellProtocolError = _Unloaded


def load_pyverify() -> Path:
    """Import pyverify and bind the names this module uses (idempotent)."""
    global PV_DIR
    if PV_DIR is None:
        PV_DIR = _find_pyverify()
        from pyverify import client, identify as ident, linux, mactest, mailbox, slot
        globals().update(
            SshTunnel=slot.SshTunnel, SshTunnelError=slot.SshTunnelError,
            ShellChannelClosed=client.ShellChannelClosed, ShellClient=client.ShellClient,
            SocketTransport=client.SocketTransport, ShellProtocolError=client.ShellProtocolError,
            IdentifyError=ident.IdentifyError, identify=ident.identify,
            _pv_ssh_argv=linux.ssh_argv, run_mac_test=mactest.run_mac_test,
            STAGE0_CONFIRM_MAGIC=mailbox.STAGE0_CONFIRM_MAGIC,
            STAGE0_STATUS_FIELDS=mailbox.STAGE0_STATUS_FIELDS,
            STAGE0_STATUS_MAGIC=mailbox.STAGE0_STATUS_MAGIC,
            STAGE0_STATUS_VERSION=mailbox.STAGE0_STATUS_VERSION,
            MailboxError=mailbox.MailboxError, SshDevmemReader=mailbox.SshDevmemReader,
            read_stage0_status=mailbox.read_stage0_status)
    return PV_DIR

H = 3600.0

#: One SSH login: dropbear + key auth + a shell; the kernel uptime, IMAGE's
#: boot-health verdict and harnessd's pid in one round trip.
LOGIN_CMD = "cat /proc/uptime; cat /run/mps3/boot-health 2>/dev/null; echo pid=$(pidof mps3-harnessd)"
#: The 5-minute resource sample (busybox: pidof awk ls wc df dmesg grep).
SAMPLE_CMD = (
    "p=$(pidof mps3-harnessd); set -- $(cat /proc/sys/fs/file-nr); "
    "echo \"S rss_kb=$(awk '/^VmRSS/{print $2}' /proc/$p/status 2>/dev/null)"
    " fds=$(ls /proc/$p/fd 2>/dev/null | wc -l)"
    " avail_kb=$(awk '/^MemAvailable/{print $2}' /proc/meminfo)"
    " slab_kb=$(awk '/^Slab:/{print $2}' /proc/meminfo)"
    " sunreclaim_kb=$(awk '/^SUnreclaim/{print $2}' /proc/meminfo)"
    " files=$1"
    " persist_used_kb=$(df -k /persist | awk 'NR==2{print $3}')"
    " oops=$(dmesg | grep -cE 'Oops|BUG:|Unable to handle|Kernel panic|general protection')\"")
TRIP_CMD = "kill -STOP $(pidof mps3-harnessd)"
#: IMAGE §7.3: sync, /persist read-only, SIGSTOP harnessd -> the WDOG resets the board.
#: Detached, so the ssh session returns at once.
MPS3_REBOOT_CMD = "setsid mps3-reboot >/dev/null 2>&1 </dev/null &"
FORENSIC_CMD = ("cat /proc/uptime; cat /run/mps3/boot-health /run/mps3/persist.state "
                "/run/mps3/net.state 2>/dev/null; echo pid=$(pidof mps3-harnessd); "
                "dmesg | tail -n 40")
#: The slot-A drill flips ONE payload byte of slot A (so stage0's region CRC fails and
#: it boots B), then writes the original byte back. Busybox ash: octal via od/printf.
CORRUPT_TMPL = (
    "d={dev}; o={off}; set -- $(dd if=$d bs=1 skip=$o count=1 2>/dev/null | od -An -to1); "
    "b=$1; printf \"\\\\$(printf %03o $(( 0$b ^ 255 )))\" | dd of=$d bs=1 seek=$o count=1 "
    "conv=notrunc,fsync 2>/dev/null; sync; "
    "set -- $(dd if=$d bs=1 skip=$o count=1 2>/dev/null | od -An -to1); echo orig=$b new=$1")
RESTORE_TMPL = (
    "d={dev}; o={off}; printf '\\{orig}' | dd of=$d bs=1 seek=$o count=1 conv=notrunc,fsync "
    "2>/dev/null; sync; set -- $(dd if=$d bs=1 skip=$o count=1 2>/dev/null | od -An -to1); "
    "echo now=$1")

S0_FALLBACK_KEYS = ("n_fallback", "n_boot_b", "n_boot_rescue", "fails_a", "fails_b")
S0_HANDOFF_KEYS = ("n_boot_a", "n_boot_b", "n_boot_rescue")
S0_FIELDS = ("boot_count", "booted_from", "default_slot", "phase", "last_verdict", "reset_cause",
             "slot_a_rc", "slot_b_rc", "n_boot_a") + S0_FALLBACK_KEYS + (
             "verdict_from", "sd_result", "rescue_reason", "rescue_sessions")

# --------------------------------------------------------------------------- #
# netboot (the user microSD is dead: every reset lands stage0 in rescue)
# --------------------------------------------------------------------------- #

#: A RAM boot has /persist on tmpfs, so a NEW host key every boot: never let it
#: fail a login. Both halves are needed: StrictHostKeyChecking=no alone connects
#: to a CHANGED key but disables port forwarding (the XVC/JTAG tunnel) -- with no
#: known_hosts at all every key is merely new. Key-only BatchMode auth stays.
NETBOOT_SSH_OPTS = ("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "GlobalKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR")
MMC_SPI_DRV = "/sys/bus/spi/drivers/mmc_spi"
#: Unbind mmc_spi from the dead card's SPI device (dmesg names it: "mmc_spi
#: spi0.0: SD/MMC host mmc0"; spi0.0 if the line is gone), then count the
#: "whilst initialising" errors twice more, {settle} s apart, to see them stop.
#: NEVER while a card partition is mounted (unbound=mounted): on 2026-09-27 the
#: "dead" card came back for one boot, /persist mounted from it, the unbind pulled
#: it from under dropbear and the next ssh died at kex (soak run 3, 49 min in).
UNBIND_TMPL = (
    "drv={drv}; d=$(dmesg | sed -n 's/.*mmc_spi \\(spi[0-9][0-9.]*\\): SD\\/MMC host.*/\\1/p' "
    "| tail -n 1); [ -n \"$d\" ] || d=spi0.0; e0=$(dmesg | grep -c 'whilst initialising'); "
    "if grep -q '^/dev/mmcblk' {mounts}; then u=mounted; "
    "elif [ -e $drv/$d ]; then if echo $d > $drv/unbind; then u=1; else u=0; fi; else u=absent; fi; "
    "sleep {settle}; e1=$(dmesg | grep -c 'whilst initialising'); sleep {settle}; "
    "e2=$(dmesg | grep -c 'whilst initialising'); "
    "echo \"U dev=$d unbound=$u err0=$e0 err1=$e1 err2=$e2\"")
UNBIND_CMD = UNBIND_TMPL.format(drv=MMC_SPI_DRV, settle=5, mounts="/proc/mounts")
UNBIND_WHY = ("DELIBERATE DEVIATION (--netboot): the user microSD is present but unreadable (a "
              "person must swap it); Linux's mmc_spi retries it every ~2.3 s forever ('mmc0: "
              "error -110 whilst initialising SD card'), which spams the kernel log and eats the "
              "100 MHz MBV's CPU, polluting the leak and load criteria. So mmc_spi is unbound "
              "from its SPI device after every boot, before the load resumes.")
#: stage0_push.py's exit codes (STAGE0_CONTRACT §9)
PUSH_RC = {1: "bad local image", 2: "rejected by stage0", 3: "not stage0 / unreachable",
           4: "transfer failed", 124: "timed out", 127: "cannot run it"}
#: /run/mps3/boot-health, one key=value per line (S99mps3health)
HEALTH_KEYS = ("healthy", "persist", "net", "ssh", "reasons", "uptime_s")
#: an XVC/JTAG refusal from a claimed harnessd (net-protocol "The lock", xvc_lock)
#: The MAC test's fault-inject round, as this board can (not) prove it -- see Runner.mac_clean.
MAC_INJECT_SKIPPED = ("skipped: GENCHK injects faults into the generator's frames and checks the "
                      "frames the DUT sends; no RM echoes one into the other (silicon 2026-09-27: "
                      "bad_fcs left err at 0 with eth_ss)")
LOCK_HINT = (" -- the board is claimed and serves 2542/6921 to its own 127.0.0.1 only "
             "(xvc_lock): go through the ssh tunnel (--debug-via auto|ssh)")


def health_kv(text: Optional[str]) -> Dict[str, str]:
    kv: Dict[str, str] = {}
    for tok in (text or "").split():
        k, sep, v = tok.partition("=")
        if sep and k in HEALTH_KEYS:
            kv[k] = v
    return kv


def health_state(text: Optional[str]) -> str:
    """``healthy`` / ``card_fault`` (healthy=0 and the ONLY hard reason is the
    dead card: S99mps3health's usd-driver-path, /dev/mmcblk0 never appeared) /
    ``unhealthy`` (anything else) / ``unknown`` (no verdict yet)."""
    kv = health_kv(text)
    if "healthy" not in kv:
        return "unknown"
    if kv["healthy"] == "1":
        return "healthy"
    reasons = [x for x in kv.get("reasons", "").split(",") if x]
    if "usd-driver-path" in reasons and not {"no-ipv4", "no-sshd"} & set(reasons):
        return "card_fault"
    return "unhealthy"


def key_fingerprints(text: str) -> List[str]:
    """OpenSSH ``SHA256:<b64>`` of every public key in an authorized_keys-style
    text, in order (what identify's ``ssh.key_sha256`` reports for a claim's
    first key; harnessd sshfp.c's rule: the blob must start with its own type)."""
    out: List[str] = []
    for line in text.splitlines():
        tok = line.split()
        if not tok or tok[0].startswith("#"):
            continue
        for kind, b64 in zip(tok, tok[1:]):
            try:
                blob = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(blob) >= 4 and blob[4:4 + struct.unpack(">I", blob[:4])[0]] == kind.encode():
                out.append("SHA256:" + base64.b64encode(hashlib.sha256(blob).digest())
                           .decode().rstrip("="))
                break
    return out


def default_stage0_push() -> Optional[Path]:
    """stage0_push.py beside this script (the hub kit) or in the repo."""
    for c in (HERE / "stage0_push.py",
              HERE.parents[1] / "src" / "linux_soc" / "hw" / "fw_stage0" / "stage0_push.py"):
        if c.is_file():
            return c
    return None


def s0lb_problems(tool_dir: Path, image: bytes) -> List[str]:
    """STAGE0's own local check (stage0_pack.check_image), loaded from beside
    stage0_push.py: [] = the image boots as far as a host can tell."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_soak_stage0_pack", str(tool_dir / "stage0_pack.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                    # type: ignore[union-attr]
    return list(mod.check_image(image)[0])


def synth_s0lb_image(payload: bytes = b"\x13\x00\x00\x00" * 256) -> bytes:
    """A 1-region S0LB v2 image (STAGE0_CONTRACT §5) that stage0_pack.check_image
    accepts: the dry run pushes this, never the 29 MB one."""
    so = 32 + 16
    ent = struct.pack("<4I", so, 0x80000000, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    hdr = [0x424C3053, 2, 1, 0x80000000, 0, 0, 0, 0]
    hdr[7] = zlib.crc32(struct.pack("<8I", *hdr) + ent) & 0xFFFFFFFF
    return struct.pack("<8I", *hdr) + ent + payload

#: Phase-U leak limits (PROPOSED; the evidence keeps the raw slopes so the project lead can
#: judge): the least-squares growth over the whole phase must stay inside these.
LEAK_LIMITS_KB = {"rss_kb": 1024, "fds": 2, "sunreclaim_kb": 8192, "files": 64,
                  "persist_used_kb": 4096}
LEAK_DECLINE_LIMITS_KB = {"avail_kb": 16384}

EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_INCONCLUSIVE = 0, 1, 2, 3


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def parse_duration(text: str) -> float:
    """``24h`` / ``30m`` / ``90s`` / ``1.5d`` / a bare number of seconds."""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([smhd]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError("bad duration %r (use e.g. 24h, 30m, 90s)" % text)
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def hexint(text: str) -> int:
    return int(str(text), 0)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4] + "Z"


def slope_per_s(points: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Least-squares slope, units per second; None with fewer than 3 points."""
    if len(points) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def p99(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(0.99 * (len(v) - 1))))]


class Evidence:
    """JSON lines, one event per line, flushed per line; ``echo`` prints a short
    human line per event for ``tail -f``."""

    def __init__(self, path: Path, echo: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", buffering=1)
        self._t0 = time.monotonic()
        self.echo = echo
        self.events: List[Dict[str, Any]] = []

    def el(self) -> float:
        return time.monotonic() - self._t0

    def write(self, ev: str, **kw: Any) -> Dict[str, Any]:
        rec = {"t": _utc(), "el_s": round(self.el(), 2), "ev": ev}
        rec.update({k: v for k, v in kw.items() if not k.startswith("_")})
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        self.events.append(rec)
        if self.echo:
            brief = " ".join("%s=%s" % (k, v) for k, v in kw.items() if not k.startswith("_")
                             and not isinstance(v, (dict, list)) and v is not None)
            print("[%s +%8.1fs] %-11s %s" % (rec["t"][11:19], rec["el_s"], ev, brief[:200]),
                  flush=True)
        return rec

    def close(self) -> None:
        self._fh.close()


class StopLoop(Exception):
    """A stop rule fired; ``kind`` names it."""

    def __init__(self, kind: str, detail: str, **extra: Any):
        super().__init__("%s: %s" % (kind, detail))
        self.kind = kind
        self.detail = detail
        self.extra = extra


class BusyRefusal(ConnectionError):
    """A single-client port (XVC 2542, JTAG 6921) accepted and closed with no
    reply: it still holds a previous connection (possibly our own, not yet
    reaped by harnessd's superloop)."""


#: THE SINGLE-CLIENT RACE (real netboot soak 2026-09-27, lx a18ce7e, 17 min in:
#: regress's `version` got ECONNRESET). 6900 serves one client and closes any
#: extra accept at once; Linux answers the unread request with an RST. So a
#: connection made while another -- often OUR previous one, not yet reaped by
#: harnessd's superloop (passes up to ~82 ms) -- still holds the port is refused.
#: A refusal before the first reply is retried after these gaps (dry run: scaled).
BUSY_GAPS_S = (0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0)
#: at most one `ss` capture of who holds a port per this many seconds
SS_EVERY_S = 60.0


def crash_stop(exc: BaseException) -> "StopLoop":
    """An exception the soak did not expect, as a FAIL of kind ``crash``: the
    run ends with forensics, a verdict and the summary files, never a bare
    traceback (2026-09-27: an uncaught ECONNRESET killed the real soak silently)."""
    return StopLoop("crash", "%s: %s" % (type(exc).__name__, exc),
                    traceback=traceback.format_exc()[-4000:])


def classify(exc: BaseException) -> str:
    """offline (refused) / busy (accept-then-EOF or accept-then-RST: another
    client holds the single-client port) / wedged (connected, no answer) /
    unreachable."""
    if isinstance(exc, ConnectionRefusedError):
        return "offline"
    if isinstance(exc, (ShellChannelClosed, ConnectionResetError, BusyRefusal)):
        return "busy"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "wedged"
    return "unreachable"


# --------------------------------------------------------------------------- #
# the board, as the network sees it
# --------------------------------------------------------------------------- #


class _CountingTransport:
    """pyverify's SocketTransport (via ShellClient's transport seam) plus a count
    of reply lines: a drop before the first reply is the single-client refusal;
    after it, the session was ours and a drop is a real fault."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.lines = 0

    def send_line(self, payload: bytes) -> None:
        self.inner.send_line(payload)

    def recv_line(self) -> bytes:
        line = self.inner.recv_line()
        self.lines += 1
        return line

    def close(self) -> None:
        self.inner.close()


class Board:
    def __init__(self, host: str, *, control_port: int = 6900, push_port: int = 6910,
                 identify_port: int = 6899, xvc_port: int = 2542, jtag_port: int = 6921,
                 timeout: float = 8.0, ssh_base: Sequence[str] = (),
                 run: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
                 ssh_timeout: float = 60.0):
        self.host = host
        self.control_port, self.push_port = control_port, push_port
        self.identify_port, self.xvc_port, self.jtag_port = identify_port, xvc_port, jtag_port
        self.timeout = timeout
        self.ssh_base = list(ssh_base)
        self.ssh_timeout = ssh_timeout
        self._run = run or self._subprocess_run

    def client(self) -> ShellClient:
        return ShellClient(self.host, port=self.control_port, timeout=self.timeout)

    def shell(self, c: "Optional[ShellClient]" = None) -> Dict[str, Any]:
        """ping + stats, on ``c`` (a :meth:`Runner.control` session) or on a
        connection of its own (probe()/wait_back, which judge a failure themselves)."""
        t = time.monotonic()
        if c is None:
            with self.client() as c2:
                p = c2.ping()
                s = c2.stats()
        else:
            p = c.ping()
            s = c.stats()
        if not p.ok:
            raise StopLoop("wedged", "ping answered ok:false")
        out: Dict[str, Any] = {"shell_id": p.shell_id, "rm_id": p.rm_id, "_t": time.monotonic(),
                               "lat_ms": round((time.monotonic() - t) * 1000.0, 1)}
        if s.ok:
            out.update(up_ms=s.up_ms, os_up_ms=s.os_up_ms, svc_skipped=s.svc_skipped,
                       svc_max_us=s.svc_max_us, swap_n=s.swap_n, rxdrop=s.rxdrop, txerr=s.txerr,
                       dut_mhz=s.dut_mhz)
        else:
            out["stats_err"] = s.err
        return out

    def log_tail(self, c: "ShellClient", nbytes: int = 1500) -> str:
        text = b""
        off = 0
        for _ in range(32):
            r = c.log(off)
            if not r.ok:
                break
            text += getattr(r, "data", b"") or b""
            off = r.off + r.n
            if not r.more:
                break
        return text[-nbytes:].decode("utf-8", "replace")

    def ident(self, timeout: float = 2.0, retries: int = 1) -> Optional[Dict[str, Any]]:
        try:
            return dict(identify(self.host, self.identify_port, timeout=timeout,
                                 retries=retries).raw)
        except (IdentifyError, OSError):
            return None

    def xvc_session(self, host: Optional[str] = None, port: Optional[int] = None) -> str:
        """``host``/``port``: an ssh tunnel's local end (a claimed board serves
        2542 to its own 127.0.0.1 only); default the board's own address."""
        with socket.create_connection((host or self.host, port or self.xvc_port),
                                      timeout=self.timeout) as s:
            s.sendall(b"getinfo:")
            buf = b""
            while b"\n" not in buf and len(buf) < 128:
                chunk = s.recv(128)
                if not chunk:
                    break
                buf += chunk
        if not buf:
            raise BusyRefusal("XVC closed the connection with no reply")
        text = buf.decode("ascii", "replace").strip()
        if not text.startswith("xvcServer_v"):
            raise StopLoop("debug", "XVC getinfo answered %r%s" % (
                text[:90], LOCK_HINT if '"locked"' in text else ""))
        return text

    def jtag_session(self, host: Optional[str] = None, port: Optional[int] = None) -> str:
        """OpenOCD remote_bitbang: 'R' asks for one TDO sample ('0'/'1'), 'Q' quits.
        Moves no TCK edge, so it never disturbs the DUT's TAP."""
        with socket.create_connection((host or self.host, port or self.jtag_port),
                                      timeout=self.timeout) as s:
            s.sendall(b"R")
            b = s.recv(1)
            if b == b"{":                        # a claimed board's one-line refusal
                try:
                    b += s.recv(160)
                except OSError:
                    pass
            elif b:
                s.sendall(b"Q")
        if not b:
            raise BusyRefusal("JTAG closed the connection with no reply")
        if b not in (b"0", b"1"):
            raise StopLoop("debug", "JTAG remote_bitbang 'R' answered %r%s" % (
                b[:90], LOCK_HINT if b'"locked"' in b else ""))
        return b.decode()

    # -- ssh -------------------------------------------------------------- #

    def _subprocess_run(self, argv: List[str]) -> Tuple[int, str, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.ssh_timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, "", "ssh timed out after %ss" % self.ssh_timeout
        except OSError as exc:
            return 127, "", "cannot run ssh: %s" % exc
        return p.returncode, p.stdout or "", p.stderr or ""

    def ssh(self, cmd: str) -> Tuple[int, str, str, float]:
        t = time.monotonic()
        rc, out, err = self._run(self.ssh_base + [cmd])
        return rc, out, err, time.monotonic() - t

    def login(self) -> Dict[str, Any]:
        rc, out, err, lat = self.ssh(LOGIN_CMD)
        rec: Dict[str, Any] = {"rc": rc, "lat_s": round(lat, 2), "os_up_s": None}
        lines = out.splitlines()
        try:
            rec["os_up_s"] = float(lines[0].split()[0])
        except (IndexError, ValueError):
            pass
        kv: Dict[str, str] = {}
        for ln in lines[1:]:
            if ln.startswith("pid="):
                rec["pid"] = ln[4:].strip()
            else:                                # boot-health: one key=value per line
                kv.update(health_kv(ln))
        if "healthy" in kv:
            rec["health"] = " ".join("%s=%s" % (k, kv[k]) for k in HEALTH_KEYS[:-1] if k in kv)
        rec["ok"] = rc == 0 and rec["os_up_s"] is not None
        if not rec["ok"]:
            rec["err"] = (err or out).strip()[-300:]
        return rec

    def sample(self) -> Dict[str, Any]:
        rc, out, err, _ = self.ssh(SAMPLE_CMD)
        rec: Dict[str, Any] = {"rc": rc}
        for ln in out.splitlines():
            if ln.startswith("S "):
                for tok in ln[2:].split():
                    k, _, v = tok.partition("=")
                    try:
                        rec[k] = int(v)
                    except ValueError:
                        rec[k] = None
        rec["ok"] = rc == 0 and "rss_kb" in rec
        if not rec["ok"]:
            rec["err"] = (err or out).strip()[-200:]
        return rec

    def stage0(self) -> Dict[str, Any]:
        st = read_stage0_status(SshDevmemReader(ssh_argv=self.ssh_base, run=self._run))
        rec = st.summary()
        rec["fields"] = {k: st.fields.get(k) for k in S0_FIELDS}
        return rec


def ssh_base_argv(target: str, opts: Sequence[str], connect_timeout: int = 15) -> List[str]:
    """pyverify's key-only ssh argv + BatchMode, then the caller's options."""
    load_pyverify()
    base = _pv_ssh_argv(target, batch=True)
    dest = base.pop()
    return base + ["-o", "ConnectTimeout=%d" % connect_timeout, "-o", "ServerAliveInterval=5",
                   "-o", "ServerAliveCountMax=3", *opts, dest]


# --------------------------------------------------------------------------- #
# the runner: probes, actions, drills
# --------------------------------------------------------------------------- #


class Runner:
    def __init__(self, args: argparse.Namespace, board: Board, ev: Evidence,
                 overlays: List[Path], sim: "Optional[Sim]" = None):
        self.a = args
        self.board = board
        self.ev = ev
        self.overlays = overlays
        self.sim = sim
        self.scale = args.time_scale
        self.prev: Optional[Dict[str, Any]] = None
        self.expect_rm: Optional[str] = None
        self.s0_base: Optional[Dict[str, Any]] = None
        self.fails: List[Dict[str, Any]] = []
        self.netboot = bool(getattr(args, "netboot", None))
        self.counts: Dict[str, int] = {k: 0 for k in (
            "polls", "swaps", "ssh", "samples", "churn", "debug", "debug_tunnelled", "heartbeats",
            "busy_retries", "power_on_boots", "mps3_reboots", "wdog_trips", "reboot_verbs",
            "fallback_drills") + (("netboot_pushes", "claims", "card_unbinds", "recoveries",
                                   "unconfirmed_expected", "card_mounted_boots")
                                  if self.netboot else ())}
        self.lat_ssh: List[float] = []
        self.swap_times: List[Tuple[float, str, float]] = []     # (el_s, rm, seconds)
        self.samples: List[Dict[str, Any]] = []
        self.t_back: List[Tuple[str, float, float]] = []        # (what, to 6900, to ssh)
        self.boot_rows: List[Dict[str, Any]] = []
        self.reset_rows: List[Dict[str, Any]] = []
        self.netboot_rows: List[Dict[str, Any]] = []            # one per rescue bring-up
        self.drills: List[Dict[str, Any]] = []
        #: this boot's /run/mps3/boot-health, as the last login read it
        self.boot_health: Optional[str] = None
        #: whether Linux had confirmed the boot at the last stage0 read (a warm
        #: reset's last_verdict judges exactly that attempt)
        self.s0_confirmed: Optional[bool] = None
        self.claim_fps: List[str] = (key_fingerprints(Path(args.claim_key).read_text())
                                     if self.netboot and getattr(args, "claim_key", None) else [])
        self._swap_ix = 0
        self._ss_at: Optional[float] = None
        self._stop = threading.Event()

    # -- time --------------------------------------------------------------- #

    def sleep(self, seconds: float) -> None:
        self._stop.wait(max(0.0, seconds))
        if self._stop.is_set():
            raise StopLoop("stopped", "stopped by operator (signal)")

    def sc(self, seconds: float) -> float:
        return seconds * self.scale

    def real_gap(self, seconds: float) -> float:
        """A polling gap that is real time on a board and short in a dry run."""
        return max(0.05, self.sc(seconds)) if self.a.dry_run else seconds

    # -- the single-client ports: retry a refusal, record who holds the port ---- #

    def _ss(self, argv: List[str]) -> Tuple[int, str, str]:
        if self.sim:
            return self.sim.ss(argv)
        import shutil                             # the hub's ss is /usr/sbin/ss
        argv = [shutil.which(argv[0]) or "/usr/sbin/" + argv[0]] + list(argv[1:])
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", "cannot run ss: %s" % exc
        return p.returncode, p.stdout or "", p.stderr or ""

    def note_busy(self, host: str, port: int, what: str, attempt: int,
                  exc: Optional[BaseException]) -> None:
        """A single-client port refused us (accept-then-close/RST, or refused):
        a "busy" event, and -- at most once per SS_EVERY_S -- who holds the port
        (`ss -tnp dst HOST:PORT`, every process and state, TIME-WAIT included) and
        OUR sockets to the board (a self-race shows as two of our connections)."""
        self.counts["busy_retries"] += 1
        rec: Dict[str, Any] = {"port": port, "what": what, "attempt": attempt,
                               "err": "%s: %s" % (type(exc).__name__, exc) if exc else None}
        now = time.monotonic()
        if self._ss_at is None or now - self._ss_at >= self.sc(SS_EVERY_S):
            self._ss_at = now
            rc, out, err = self._ss(["ss", "-tnp", "dst", "%s:%d" % (host, port)])
            rec["ss"] = (out or err).strip()[-2000:]
            rc2, out2, err2 = self._ss(["ss", "-tnp", "dst", host])
            mine = "pid=%d," % os.getpid()
            rec["ours"] = [ln for ln in (out2 or "").splitlines() if mine in ln][:20]
            rec["pid"] = os.getpid()
        self.ev.write("busy", **rec)

    def _gap(self, attempt: int) -> None:
        self.sleep(self.real_gap(BUSY_GAPS_S[min(attempt, len(BUSY_GAPS_S)) - 1]))

    def control(self, fn: Callable[[Any], Any], what: str, *, first_ping: bool = False) -> Any:
        """``fn(client)`` on a fresh 6900 session, for EVERY control-channel use
        outside probe(). A refusal on the connect or before the first reply
        (ConnectionReset / ConnectionRefused / ShellChannelClosed / a broken pipe)
        is "busy": note it and retry after BUSY_GAPS_S. Only a persistent refusal
        becomes StopLoop offline (refused) / wedged. A drop AFTER a reply is not
        retried (the session was ours; ``fn`` may have changed state).
        ``first_ping``: exchange a ping first, so a non-idempotent verb is only
        ever sent on a connection that already holds the slot."""
        b = self.board
        busy_exc = (ConnectionResetError, ConnectionRefusedError, BrokenPipeError,
                    ShellChannelClosed)
        last: Optional[BaseException] = None
        for attempt in range(len(BUSY_GAPS_S) + 1):
            if attempt:
                self.note_busy(b.host, b.control_port, what, attempt, last)
                self._gap(attempt)
            try:
                tr = _CountingTransport(SocketTransport(b.host, b.control_port, b.timeout))
            except (ConnectionResetError, ConnectionRefusedError) as exc:
                last = exc
                continue
            except OSError as exc:
                raise StopLoop("wedged" if classify(exc) == "wedged" else "offline",
                               "%s: cannot connect to 6900: %s: %s" % (what, type(exc).__name__, exc))
            c = ShellClient(b.host, port=b.control_port, timeout=b.timeout, transport=tr)
            try:
                if first_ping:
                    c.ping()
                return fn(c)
            except busy_exc as exc:
                if tr.lines == 0:
                    last = exc
                    continue
                raise StopLoop("wedged", "%s: 6900 dropped our session after %d repl%s: %s: %s"
                               % (what, tr.lines, "y" if tr.lines == 1 else "ies",
                                  type(exc).__name__, exc))
            except (socket.timeout, TimeoutError) as exc:
                raise StopLoop("wedged", "%s: 6900 accepted but never answered: %s" % (what, exc))
            except ShellProtocolError as exc:
                raise StopLoop("wedged", "%s: 6900 protocol error: %s" % (what, exc))
            finally:
                c.close()
        refused = isinstance(last, ConnectionRefusedError)
        raise StopLoop("offline" if refused else "wedged",
                       "%s: 6900 %s on all %d tries (%s): %s" % (
                           what, "refused the connection" if refused else "turned us away",
                           len(BUSY_GAPS_S) + 1, "harnessd is not listening" if refused else
                           "another client holds the single-client control port",
                           "%s: %s" % (type(last).__name__, last)))

    def connect_retry(self, host: str, port: int, what: str) -> None:
        """A bare connect (6910 churn): refused/reset is retried like control()."""
        last: Optional[BaseException] = None
        for attempt in range(len(BUSY_GAPS_S) + 1):
            if attempt:
                self.note_busy(host, port, what, attempt, last)
                self._gap(attempt)
            try:
                with socket.create_connection((host, port), timeout=self.board.timeout):
                    return
            except (ConnectionResetError, ConnectionRefusedError) as exc:
                last = exc
            except OSError as exc:
                raise StopLoop("wedged" if classify(exc) == "wedged" else "offline",
                               "%s: %s: %s" % (what, type(exc).__name__, exc))
        raise StopLoop("offline" if isinstance(last, ConnectionRefusedError) else "wedged",
                       "%s: %d refused on all %d tries: %s" % (what, port, len(BUSY_GAPS_S) + 1, last))

    def session_retry(self, fn: Callable[[], Any], host: str, port: int, what: str) -> Any:
        """One XVC/JTAG exchange (idempotent: getinfo / a TDO sample): a port that
        closes or resets with no reply (still holding a previous connection) is
        retried; anything it DOES answer (the claim lock's line) is final."""
        last: Optional[BaseException] = None
        for attempt in range(len(BUSY_GAPS_S) + 1):
            if attempt:
                self.note_busy(host, port, what, attempt, last)
                self._gap(attempt)
            try:
                return fn()
            except (BusyRefusal, ConnectionResetError, ConnectionRefusedError,
                    BrokenPipeError) as exc:
                last = exc
        raise StopLoop("debug", "%s at %s:%d turned us away on all %d tries: %s: %s" % (
            what, host, port, len(BUSY_GAPS_S) + 1, type(last).__name__, last))

    def reboot_verb(self) -> Dict[str, Any]:
        """The reboot verb, NEVER sent twice: it goes out only after a ping on the
        same connection (the refusal race is settled by then); if the reply is
        lost after the send it is "maybe sent" and wait_back decides."""
        sent: List[bool] = []

        def go(c: Any) -> Dict[str, Any]:
            sent.append(True)
            r = c.reboot()
            return {"ok": r.ok, "in_ms": getattr(r, "in_ms", None), "err": getattr(r, "err", "")}
        try:
            return self.control(go, "reboot verb", first_ping=True)
        except StopLoop as stop:
            if not sent or stop.kind == "stopped":
                raise
            self.ev.write("warn", what="reboot verb sent but its reply was lost -- NOT re-sent; "
                          "wait_back decides (os_up_ms must restart)", why=stop.detail)
            return {"ok": True, "maybe_sent": True, "why": stop.detail}

    # -- probes ------------------------------------------------------------- #

    def probe(self) -> Dict[str, Any]:
        last_kind, last_detail, busy, i = "unreachable", "", 0, 0
        while i < self.a.retries:
            try:
                return self.board.shell()
            except StopLoop:
                raise
            except OSError as exc:                 # includes ShellChannelClosed, timeouts
                last_kind, last_detail = classify(exc), "%s: %s" % (type(exc).__name__, exc)
                if last_kind == "busy" and busy < 3:
                    busy += 1
                    self.note_busy(self.board.host, self.board.control_port, "probe", busy, exc)
                    self.sleep(self.real_gap(self.a.retry_gap) / 2)
                    continue
            i += 1
            if i < self.a.retries:
                self.sleep(self.real_gap(self.a.retry_gap))
        ident = self.board.ident()
        if ident and ident.get("mode") == "rescue":
            raise StopLoop("rescue", "stage0 is in rescue: %s" % ident.get("reason"), identify=ident)
        kind = "wedged" if last_kind in ("busy", "wedged") else "offline"
        raise StopLoop(kind, "%s after %d attempts (%s); identify %s" % (
            last_kind, self.a.retries, last_detail, "answered" if ident else "silent"),
            identify=ident)

    def check(self, rec: Dict[str, Any]) -> None:
        if int(rec["shell_id"], 16) != self.a.expect_static_id:
            raise StopLoop("identity", "shell_id %s != expected 0x%08x"
                           % (rec["shell_id"], self.a.expect_static_id))
        if self.expect_rm is not None and int(rec["rm_id"], 16) != int(self.expect_rm, 16):
            raise StopLoop("rm_changed", "rm_id %s != %s (the last verified swap)"
                           % (rec["rm_id"], self.expect_rm))
        prev = self.prev
        if prev is not None:
            if self.went_back(prev, rec, "os_up_ms"):
                raise StopLoop("unexplained_reset", "os_up_ms %s -> %s over %.0f s: the board "
                               "rebooted outside a drill" % (prev.get("os_up_ms"),
                                                             rec.get("os_up_ms"),
                                                             rec["_t"] - prev["_t"]))
            if self.went_back(prev, rec, "up_ms"):
                raise StopLoop("harnessd_respawn", "up_ms %s -> %s while os_up_ms kept "
                               "counting: mps3-harnessd died and init respawned it"
                               % (prev.get("up_ms"), rec.get("up_ms")))
            if (rec.get("svc_skipped") or 0) > (prev.get("svc_skipped") or 0):
                self.ev.write("warn", what="svc_skipped rose", svc_skipped=rec["svc_skipped"])
        self.prev = rec

    def went_back(self, prev: Dict[str, Any], now: Dict[str, Any], key: str) -> bool:
        """An uptime restarted iff it reads less than its previous value carried
        forward by the wall time between the two reads (a reset always loses the
        seconds between the request and the new kernel's start)."""
        p, n = prev.get(key), now.get(key)
        if p is None or n is None or "_t" not in prev or "_t" not in now:
            return False
        return n < p + (now["_t"] - prev["_t"]) * 1000.0 - self.a.reset_tolerance_ms

    # -- the load ------------------------------------------------------------ #

    def poll(self) -> None:
        rec = self.probe()
        self.check(rec)
        self.counts["polls"] += 1
        self.ev.write("poll", **rec)

    def swap(self, ovl: Optional[Path] = None, *, tag: str = "swap") -> Dict[str, Any]:
        if ovl is None:
            ovl = self.overlays[self._swap_ix % len(self.overlays)]
            self._swap_ix += 1
        want = json.loads((ovl / "manifest.json").read_text())["rm_id"]
        argv = [sys.executable, "-m", "pyverify.cli", "deploy", "--host", self.board.host,
                "--overlay", str(ovl), "--no-persist",
                "--control-port", str(self.board.control_port),
                "--tftp-port", str(self.a.tftp_port), "--tcp-push-port", str(self.board.push_port)]
        if self.a.transport != "auto":
            argv += ["--pusher-transport", self.a.transport, "--src", self.a.transport,
                     "--no-windowed"]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PV_DIR) + (os.pathsep + env["PYTHONPATH"]
                                           if env.get("PYTHONPATH") else "")
        t = time.monotonic()
        try:
            p = subprocess.run(argv, capture_output=True, text=True, env=env,
                               timeout=self.a.swap_timeout, stdin=subprocess.DEVNULL)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = 124, "", "deploy timed out after %ss" % self.a.swap_timeout
        dur = time.monotonic() - t
        res: Dict[str, Any] = {}
        for ln in reversed(out.splitlines()):
            try:
                res = json.loads(ln)
                break
            except ValueError:
                continue
        rec = {"rm": ovl.name, "rc": rc, "verified": res.get("verified"), "rm_id": res.get("rm_id"),
               "want": want, "dur_s": round(dur, 2)}
        m = re.search(r"deploy: transport=(\S+) src=(\S+) windowed=(\S+) \((.*)\)", err or "")
        if m:                   # what deploy chose, and why (auto-detect vs explicit flags)
            rec["transport"] = {"push": m.group(1), "src": m.group(2),
                                "windowed": m.group(3) == "True", "why": m.group(4)}
        if rc != 0 or res.get("verified") is not True or \
                int(str(res.get("rm_id") or "0"), 16) != int(want, 16):
            rec["err"] = (err or out).strip()[-400:]
            self.ev.write(tag, ok=False, **rec)
            raise StopLoop("swap", "deploy %s rc=%d verified=%s rm_id=%s (want %s): %s"
                           % (ovl.name, rc, res.get("verified"), res.get("rm_id"), want,
                              rec["err"][-160:]))
        self.expect_rm = want
        self.counts["swaps"] += 1
        self.swap_times.append((self.ev.el(), ovl.name, dur))
        self.ev.write(tag, ok=True, **rec)
        self.check(self.probe())
        return rec

    def login(self, *, stage0: bool = False) -> Dict[str, Any]:
        last: Dict[str, Any] = {}
        for i in range(self.a.retries):
            last = self.board.login()
            if last["ok"]:
                break
            if i + 1 < self.a.retries:
                self.sleep(self.real_gap(self.a.retry_gap))
        if not last.get("ok"):
            self.ev.write("ssh", **last)
            raise StopLoop("ssh", "ssh login failed %d times: rc=%s %s"
                           % (self.a.retries, last.get("rc"), last.get("err", "")))
        self.counts["ssh"] += 1
        self.lat_ssh.append(last["lat_s"])
        if last.get("health"):
            self.boot_health = last["health"]
        # netboot + the dead card: healthy=0 reasons=usd-driver-path is expected and
        # was recorded once at the bring-up (netboot_up); anything else still warns
        if last.get("health") and "healthy=1" not in last["health"] and not (
                self.netboot and health_state(last["health"]) == "card_fault"):
            self.ev.write("warn", what="boot-health not healthy", health=last["health"])
        if stage0 and self.a.stage0:
            last["stage0"] = self.stage0_verify("none")
        self.ev.write("ssh", **last)
        return last

    def sample(self) -> None:
        rec = self.board.sample()
        if not rec["ok"]:
            raise StopLoop("ssh", "resource sample over ssh failed: %s" % rec.get("err"))
        rec["el_s"] = round(self.ev.el(), 2)
        self.samples.append(rec)
        self.counts["samples"] += 1
        self.ev.write("sample", **{k: v for k, v in rec.items() if k != "el_s"})
        if rec.get("oops"):
            raise StopLoop("kernel_oops", "dmesg counts %d Oops/BUG/panic line(s)" % rec["oops"])

    def churn(self) -> None:
        """``churn_n`` short 6900 sessions (a ping each) and bare 6910 connects,
        back to back: exactly the single-client race, so each goes through the
        busy retry (control() / connect_retry())."""
        ok6900 = ok6910 = 0
        for _ in range(self.a.churn_n):
            if self.control(lambda c: c.ping().ok, "churn 6900"):
                ok6900 += 1
            self.connect_retry(self.board.host, self.board.push_port, "churn 6910")
            ok6910 += 1
        self.counts["churn"] += 1
        self.ev.write("churn", ok_6900=ok6900, ok_6910=ok6910)

    # -- the debug ports on a claimed board ----------------------------------- #

    def board_claimed(self) -> bool:
        ident = self.board.ident(timeout=1.0, retries=1)
        ssh = (ident or {}).get("ssh")
        return isinstance(ssh, dict) and ssh.get("claimed") is True

    def tunnel_ssh(self) -> str:
        """The ssh command (+ options) SshTunnel runs: the soak's own identity and,
        in netboot, the no-known_hosts options. The dry run's is a fake forwarder."""
        head = self.sim.ssh_prefix() if self.sim else ["ssh"]
        return shlex.join(head + ["-o", "ConnectTimeout=15"] + list(self.a.ssh_opts_all))

    @contextlib.contextmanager
    def debug_route(self, *ports: int):
        """Yields (host, {board port: port to connect to}, "direct"|"ssh"). A
        claimed harnessd serves XVC 2542 / JTAG 6921 to its own 127.0.0.1 only
        (xvc_lock), so then the sessions ride pyverify's SshTunnel to it (never a
        ControlMaster; the ports are checked free before and after). The tunnel's
        readiness probe goes to 6900, never to the single-client debug ports."""
        via = self.a.debug_via
        if via == "auto":
            via = "ssh" if self.board_claimed() else "direct"
        if via == "direct":
            yield self.board.host, {p: p for p in ports}, "direct"
            return
        tun = SshTunnel(self.a.ssh_dest, (self.board.control_port,) + tuple(ports),
                        ssh=self.tunnel_ssh(), timeout_s=10.0 if self.a.dry_run else 60.0)
        with tun:
            yield "127.0.0.1", {p: tun.local(p) for p in ports}, "ssh"

    def debug(self) -> None:
        xp, jp = self.board.xvc_port, self.board.jtag_port
        try:
            with self.debug_route(xp, jp) as (host, ports, via):
                xvc = self.session_retry(lambda: self.board.xvc_session(host, ports[xp]),
                                         host, ports[xp], "XVC")
                tdo = self.session_retry(lambda: self.board.jtag_session(host, ports[jp]),
                                         host, ports[jp], "JTAG")
        except SshTunnelError as exc:
            raise StopLoop("debug", "the ssh tunnel for the debug session: %s" % exc)
        except OSError as exc:
            raise StopLoop("debug", "debug session: %s: %s" % (type(exc).__name__, exc))
        self.counts["debug"] += 1
        if via == "ssh":
            self.counts["debug_tunnelled"] += 1
        self.ev.write("debug", xvc=xvc, jtag_tdo=tdo, via=via)

    def heartbeat(self) -> None:
        if not self.a.heartbeat_cmd:
            return
        p = subprocess.run(self.a.heartbeat_cmd, shell=True, capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL)
        self.ev.write("heartbeat", ok=p.returncode == 0, rc=p.returncode,
                      out=(p.stdout + p.stderr).strip()[-200:])
        if p.returncode != 0:
            raise StopLoop("lease", "lease heartbeat failed (rc=%d)" % p.returncode)
        self.counts["heartbeats"] += 1

    # -- stage0 --------------------------------------------------------------- #

    def stage0_verify(self, event: str) -> Dict[str, Any]:
        """``event``: none (no reset since the last read), warm (one WDOG reset),
        warm_fallback (one WDOG reset that MUST have fallen back to B), power_on
        (a reconfiguration: the block was zeroed, so exactly ONE hand-off since).

        Netboot (stage0_flow.c): every boot is booted_from RESCUE; a warm entry
        judges the pending rescue attempt (verdict_from RESCUE; last_verdict 1 if
        Linux had confirmed it, else 2 -- an unconfirmed rescue boot costs no slot
        anything, a confirmed one zeroes fails_a/fails_b), then the push hands off
        again: boot_count +1, n_boot_rescue +1, n_boot_a/b and n_fallback unmoved.
        harnessd confirms only a boot whose boot-health says healthy=1, and with the
        dead card it says healthy=0 reasons=usd-driver-path: then the check is that
        Linux did NOT confirm."""
        fault = self.netboot and health_state(self.boot_health) == "card_fault"
        s0: Dict[str, Any] = {}
        t_end = time.monotonic() + (5.0 if self.a.dry_run else 45.0)
        while True:
            try:
                s0 = self.board.stage0()
            except MailboxError as exc:
                raise StopLoop("stage0", "cannot read the stage0 block over ssh: %s" % exc)
            if fault or s0.get("linux_confirmed") or time.monotonic() > t_end:
                break
            self.sleep(self.real_gap(3.0))       # harnessd confirms 2 s after it is healthy
        f = s0["fields"]
        if not s0.get("valid"):
            raise StopLoop("stage0", "the stage0 block is not valid", stage0=s0)
        if s0.get("phase") != "HANDOFF":
            raise StopLoop("stage0", "phase %s, want HANDOFF" % s0.get("phase"), stage0=s0)
        if fault:
            if s0.get("linux_confirmed"):
                raise StopLoop("stage0", "Linux CONFIRMED a boot whose boot-health says %s: "
                               "harnessd must confirm only healthy=1" % self.boot_health, stage0=s0)
            self.counts["unconfirmed_expected"] += 1
        elif not s0.get("linux_confirmed"):
            raise StopLoop("stage0", "Linux never confirmed this boot (att_confirm)%s" % (
                "; boot-health: %s" % self.boot_health if self.netboot else ""), stage0=s0)
        want_from = ("RESCUE" if self.netboot else "B" if event == "warm_fallback"
                     else self.a.expect_slot)
        if want_from != "any" and s0.get("booted_from") != want_from:
            raise StopLoop("stage0", "booted_from %s, want %s%s" % (
                s0.get("booted_from"), want_from, " (netboot: every reset should land in "
                "rescue -- did the card read this time?)" if self.netboot else ""), stage0=s0)
        base = self.s0_base
        if event == "power_on":
            # boot_count counts every stage0 ENTRY and a cold boot on silicon reads 4
            # (B2 2026-09-26 and the re-bake 09-27, both after a paced MCC REBOOT);
            # the hand-off counters are NOINIT too, and only a reconfiguration zeroes
            # them: exactly one hand-off since the block was initialised is the proof
            handoffs = sum(f.get(k) or 0 for k in S0_HANDOFF_KEYS)
            if handoffs != 1:
                raise StopLoop("stage0", "%d hand-offs since the block was initialised after a "
                               "power-on (boot_count %d), want exactly 1: the FPGA was not "
                               "reconfigured" % (handoffs, f["boot_count"]), stage0=s0)
        elif base is not None:
            warm = event.startswith("warm")
            want_bc = base["boot_count"] + (1 if warm else 0)
            if f["boot_count"] != want_bc:
                raise StopLoop("stage0", "boot_count %d, want %d (%s)" % (
                    f["boot_count"], want_bc, "a reset nobody asked for"
                    if f["boot_count"] > want_bc else "the reset never re-entered stage0"),
                    stage0=s0)
            keys = S0_FALLBACK_KEYS + (("n_boot_a",) if self.netboot else ())
            want = {k: base.get(k) or 0 for k in keys}
            if event == "warm_fallback":
                want["n_fallback"] += 1
                want["n_boot_b"] += 1
            if self.netboot and warm:
                want["n_boot_rescue"] += 1
                if self.s0_confirmed:
                    want["fails_a"] = want["fails_b"] = 0
            moved = {k: (base.get(k), f.get(k)) for k in keys if (f.get(k) or 0) != want[k]}
            if moved:
                raise StopLoop("stage0", "counters %s (base, now); expected %s"
                               % (moved, {k: want[k] for k in moved}), stage0=s0)
            if warm:
                want_v = 2 if self.s0_confirmed is False else 1
                if f.get("last_verdict") != want_v:
                    raise StopLoop("stage0", "last_verdict %s, want %s: %s" % (
                        s0.get("last_verdict"), {1: "CONFIRMED", 2: "UNCONFIRMED"}[want_v],
                        "the previous boot was never confirmed" if want_v == 1 else
                        "Linux had not confirmed the previous boot"), stage0=s0)
                if self.netboot and s0.get("verdict_from") != "RESCUE":
                    raise StopLoop("stage0", "verdict_from %s, want RESCUE (the judged attempt "
                                   "was the rescue push)" % s0.get("verdict_from"), stage0=s0)
            if warm and not s0.get("watchdog_reset"):
                raise StopLoop("stage0", "a WDOG reset without reset_cause WRS", stage0=s0)
        self.s0_base = dict(f)
        self.s0_confirmed = bool(s0.get("linux_confirmed"))
        return s0

    # -- resets ---------------------------------------------------------------- #

    def wait_back(self, t_req: float, prev: Optional[Dict[str, Any]], *, require_down: bool,
                  what: str) -> Dict[str, Any]:
        """Wait for the reset asked for at ``t_req`` to happen and the board to come
        back (6900, then ssh). Netboot: stage0 lands in rescue, so push IMAGE once,
        then claim, log in, read the boot-health and unbind mmc_spi -- all before
        this returns, i.e. before the load resumes."""
        deadline = t_req + self.a.reset_deadline
        down_seen = False
        rec: Optional[Dict[str, Any]] = None
        gap = self.real_gap(2.0)
        nb: Dict[str, Any] = {}
        t_pushed: Optional[float] = None
        while time.monotonic() < deadline:
            self.sleep(gap)
            try:
                r = self.board.shell()
            except (OSError, StopLoop):
                if not down_seen:
                    self.ev.write("down", what=what, after_s=round(time.monotonic() - t_req, 1))
                down_seen = True
                ident = self.board.ident(timeout=0.5, retries=0)
                if ident and ident.get("mode") == "rescue":
                    if not self.netboot:
                        raise StopLoop("rescue", "after %s stage0 went to rescue: %s"
                                       % (what, ident.get("reason")), identify=ident)
                    if t_pushed is None:
                        nb = self.netboot_push(t_req, what, ident)
                        t_pushed = time.monotonic()
                        # the 6900 deadline restarts at the accepted push
                        deadline = max(deadline, t_pushed + self.a.reset_deadline)
                    elif time.monotonic() - t_pushed > (2.0 if self.a.dry_run else 15.0):
                        # stage0 dallies 2 s after the final ACK; rescue again later is a
                        # NEW stage0 entry: the RAM image reset before harnessd came up
                        raise StopLoop("netboot", "%s: stage0 is back in rescue %.0f s after the "
                                       "push was accepted: the RAM image never reached 6900"
                                       % (what, time.monotonic() - t_pushed), identify=ident)
                continue
            if (down_seen or not require_down) and \
                    (prev is None or self.went_back(prev, r, "os_up_ms")):
                rec = r
                break
        if rec is None:
            why = ("the board never went down (the REBOOT was a no-op?)" if require_down and
                   not down_seen else "the board went down and never came back" if down_seen
                   else "no reset happened (os_up_ms never restarted)")
            if self.netboot and down_seen:
                why += (" (the rescue push was accepted %.0f s before the deadline)"
                        % (deadline - t_pushed) if t_pushed is not None
                        else " (and stage0's rescue never answered identify: nothing to push to)")
            raise StopLoop("reset_timeout", "%s: %s within %ss" % (what, why, self.a.reset_deadline))
        t6900 = time.monotonic() - t_req
        self.prev = None
        self.expect_rm = None                     # the next swap re-establishes it
        self.check(rec)
        if self.netboot:
            nb["t_6900_s"] = round(t6900, 1)
            self.netboot_claim(t_req, nb)
        t_ssh = None
        while time.monotonic() < deadline + 120:
            if self.board.login()["ok"]:
                t_ssh = time.monotonic() - t_req
                break
            self.sleep(gap)
        if t_ssh is None:
            raise StopLoop("ssh", "%s: 6900 back after %.0fs, ssh never answered" % (what, t6900))
        self.t_back.append((what, t6900, t_ssh))
        back = {"t_6900_s": round(t6900, 1), "t_ssh_s": round(t_ssh, 1), "down_seen": down_seen,
                "rm_id": rec["rm_id"], "os_up_ms": rec.get("os_up_ms")}
        if self.netboot:
            nb["t_ssh_s"] = round(t_ssh, 1)
            self.netboot_settle(nb)
            nb["pushed"] = t_pushed is not None
            self.netboot_rows.append(dict(what=what, **nb))
            self.ev.write("netboot_up", what=what, **nb)
            back.update({k: nb.get(k) for k in ("t_rescue_s", "push_s", "t_claim_s", "unbind")})
        return back

    # -- netboot: push, claim, settle ----------------------------------------- #

    def netboot_push(self, t_req: float, what: str, ident: Dict[str, Any]) -> Dict[str, Any]:
        """ONE rescue push of the image with STAGE0's own stage0_push.py (its local
        check, the stage0.status RRQ that refuses a board that is not stage0, then
        the in-band verdict: exit 0 = stage0 verified it in DDR and is booting it)."""
        t0 = time.monotonic()
        argv = [sys.executable, self.a.stage0_push, self.board.host, self.a.netboot_push_image,
                "--quiet"]
        if self.a.tftp_port != 69:
            argv += ["--port", str(self.a.tftp_port)]
        if self.a.dry_run:
            argv.append("--no-ping")              # the Sim's 127.0.0.1 answers no ICMP of its own
        try:
            p = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               timeout=self.a.netboot_timeout)
            rc, text = p.returncode, (p.stdout + p.stderr).strip()
        except subprocess.TimeoutExpired:
            rc, text = 124, "stage0_push timed out after %ss" % self.a.netboot_timeout
        except OSError as exc:
            rc, text = 127, "cannot run %s: %s" % (self.a.stage0_push, exc)
        rec = {"t_rescue_s": round(t0 - t_req, 1), "push_s": round(time.monotonic() - t0, 1),
               "push_rc": rc, "rescue_reason": ident.get("reason")}
        self.ev.write("netboot_push", what=what, ok=rc == 0, out=text[-300:], **rec)
        if rc != 0:
            raise StopLoop("netboot", "%s: the rescue push failed: stage0_push rc=%d (%s): %s"
                           % (what, rc, PUSH_RC.get(rc, "?"), text[-200:]), push=rec)
        self.counts["netboot_pushes"] += 1
        return rec

    def _claim_cli(self) -> Tuple[int, str]:
        argv = [sys.executable, "-m", "pyverify.cli", "claim", "--host", self.board.host,
                "--port", str(self.a.tftp_port), "--key", self.a.claim_key]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PV_DIR) + (os.pathsep + env["PYTHONPATH"]
                                           if env.get("PYTHONPATH") else "")
        try:
            p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, "pyverify claim timed out"
        return p.returncode, (p.stdout + p.stderr).strip()[-200:]

    def netboot_claim(self, t_req: float, nb: Dict[str, Any]) -> None:
        """Claim the fresh RAM boot with --claim-key (`pyverify claim`: TOFU over
        TFTP, accepted only while unclaimed). Never while identify says rescue:
        stage0 takes ANY TFTP write for a boot image (HOST_CONTRACT §8.3 trap 3).
        A board already claimed by one of our keys is fine; by anyone else's, FAIL."""
        last = "identify silent"
        for i in range(6):
            if i:
                self.sleep(self.real_gap(3.0))
            ident = self.board.ident(timeout=1.0, retries=1)
            if ident is None:
                last = "identify silent"
                continue
            if ident.get("mode") == "rescue":
                raise StopLoop("netboot", "claim: identify says rescue; never claim there (stage0 "
                               "would take the key file for a boot image)", identify=ident)
            ssh = ident.get("ssh") if isinstance(ident.get("ssh"), dict) else {}
            nb["host_key_sha256"] = ssh.get("host_key_sha256")
            if ssh.get("claimed") is True:
                ks = ssh.get("key_sha256")
                if ks and ks not in self.claim_fps:
                    raise StopLoop("netboot", "the board is claimed by key %s, which is not in %s"
                                   % (ks, self.a.claim_key), identify=ident)
                nb.setdefault("claim", "already claimed by our key" if ks else "already claimed")
                break
            rc, text = self._claim_cli()
            last = "pyverify claim rc=%d: %s" % (rc, text)
            if rc == 0:
                nb["claim"] = "accepted"
                self.counts["claims"] += 1
                break
            if rc == 2:
                raise StopLoop("netboot", "claim: %s" % last)
            # rc 1: claimed meanwhile (identify says by whom next time); 3: transport
        else:
            raise StopLoop("netboot", "the claim did not take after 6 tries: %s" % last)
        nb["t_claim_s"] = round(time.monotonic() - t_req, 1)
        self.ev.write("claim", claim=nb["claim"], t_claim_s=nb["t_claim_s"],
                      host_key_sha256=nb.get("host_key_sha256"))

    def netboot_settle(self, nb: Dict[str, Any]) -> None:
        """After the first ssh login of a RAM boot: its boot-health verdict (S99
        writes it once the network settles), then the DELIBERATE DEVIATION -- the
        dead card's mmc_spi unbound -- before the load resumes."""
        health = ""
        t_end = time.monotonic() + (5.0 if self.a.dry_run else 120.0)
        while True:
            lg = self.board.login()
            if lg["ok"] and lg.get("health"):
                health = lg["health"]
                break
            if time.monotonic() > t_end:
                break
            self.sleep(self.real_gap(3.0))
        self.boot_health = health or None
        nb["health"] = health or None
        state = health_state(health)
        if state not in ("healthy", "card_fault"):
            self.ev.write("warn", what="netboot boot-health %s" % state, health=health)
        rc, out, err, _ = self.board.ssh(UNBIND_CMD)
        m = re.search(r"^U dev=(\S*) unbound=(\S+) err0=(\d+) err1=(\d+) err2=(\d+)", out, re.M)
        if rc != 0 or not m:
            raise StopLoop("netboot", "the mmc_spi unbind over ssh failed: rc=%d %s"
                           % (rc, (err or out).strip()[-200:]))
        dev, unbound = m.group(1), m.group(2)
        e0, e1, e2 = (int(m.group(i)) for i in (3, 4, 5))
        self.ev.write("netboot_unbind", deviation=True, why=UNBIND_WHY, dev=dev, unbound=unbound,
                      mmc_errors=[e0, e1, e2])
        if unbound == "0":
            raise StopLoop("netboot", "echo %s > %s/unbind failed" % (dev, MMC_SPI_DRV))
        if unbound == "absent":
            self.ev.write("warn", what="mmc_spi had no %s bound: nothing to unbind" % dev)
        elif unbound == "mounted":
            self.counts["card_mounted_boots"] += 1
            self.ev.write("warn", what="the card is MOUNTED this boot (it answered Linux): the "
                          "unbind is skipped, never pulled from under a mounted filesystem",
                          health=health)
        else:
            self.counts["card_unbinds"] += 1
        if e2 > e1:
            self.ev.write("warn", what="mmc errors still growing after the unbind",
                          mmc_errors=[e0, e1, e2])
        nb["unbind"] = dev if unbound == "1" else unbound

    def netboot_start(self) -> None:
        """Before the first probe: a board still in rescue gets its push now; a
        running RAM boot is claimed if it is not, and its mmc_spi unbound."""
        if not self.netboot:
            return
        ident = self.board.ident(timeout=1.0, retries=2)
        if ident and ident.get("mode") == "rescue":
            self.wait_back(time.monotonic(), None, require_down=False, what="initial netboot")
            return
        nb: Dict[str, Any] = {}
        self.netboot_claim(time.monotonic(), nb)
        self.netboot_settle(nb)
        self.netboot_rows.append(dict(what="start", **nb))
        self.ev.write("netboot_up", what="start", **nb)

    def recover(self) -> None:
        """--keep-going in netboot: a board that fell into rescue stays there until
        pushed (with a card it would have booted by itself), so push once more and
        carry on. The FAIL that got us here stays recorded."""
        if not self.netboot:
            return
        ident = self.board.ident(timeout=1.0, retries=1)
        if not ident or ident.get("mode") != "rescue":
            return
        try:
            back = self.wait_back(time.monotonic(), None, require_down=False, what="recovery")
        except StopLoop as stop:
            if stop.kind == "stopped":
                raise
            self.fail(stop)
            return
        self.s0_base, self.s0_confirmed = None, None
        self.counts["recoveries"] += 1
        self.ev.write("recovered", **back)

    def _before(self) -> Dict[str, Any]:
        rec = self.probe()
        self.check(rec)
        return rec

    def warm_reset(self, how: str, *, event: str = "warm") -> Dict[str, Any]:
        prev = self._before()
        t_req = time.monotonic()
        maybe = False
        if how == "reboot_verb":
            rb = self.reboot_verb()
            if not rb["ok"]:
                raise StopLoop("drill", "reboot verb refused: %s" % rb.get("err"))
            maybe = bool(rb.get("maybe_sent"))
            self.counts["reboot_verbs"] += 1
        else:
            cmd = TRIP_CMD if how == "wdog_trip" else MPS3_REBOOT_CMD
            rc, out, err, _ = self.board.ssh(cmd)
            if rc != 0:
                raise StopLoop("drill", "%s over ssh failed rc=%d %s" % (how, rc, (err or out)[-120:]))
            self.counts["wdog_trips" if how == "wdog_trip" else "mps3_reboots"] += 1
        # a reboot verb whose reply was lost is proven by the reset itself --
        # os_up_ms restarting (wait_back's went_back) -- never by sending it again
        back = self.wait_back(t_req, prev, require_down=False, what=how)
        if maybe:
            back["reply_lost"] = True
        s0 = self.stage0_verify(event)
        rec = dict(back, what=how, boot_count=s0["fields"]["boot_count"],
                   booted_from=s0.get("booted_from"), wrs=s0.get("watchdog_reset"))
        self.reset_rows.append(rec)
        self.ev.write("reset_ok", **rec)
        return rec

    def power_on(self, n: int) -> Dict[str, Any]:
        prev = self._before()
        t_req = time.monotonic()
        mcc: Optional[Dict[str, Any]] = None
        if self.a.reset == "mcc":
            log = Path(str(self.ev.path.with_suffix("")) + ".mcc%02d.log" % n)
            mcc = mcc_reboot(self.a.mcc_tty, log, pace=self.a.mcc_pace, settle=self.a.mcc_settle,
                             capture_s=self.a.mcc_capture, wait_done=False)
            self.ev.write("mcc", n=n, **mcc)
            if mcc["rc"] != 0:
                raise StopLoop("reset_timeout", "power-on %d: MCC REBOOT: %s" % (n, mcc.get("why")))
        elif self.a.reset == "manual":
            print(">>> power-on %d: power-cycle the board NOW, then press Enter" % n, flush=True)
            sys.stdin.readline()
        elif self.a.reset == "sim":
            self.sim.power_cycle()
        else:
            p = subprocess.run(self.a.reset, shell=True, capture_output=True, text=True, timeout=300)
            self.ev.write("reset_cmd", n=n, rc=p.returncode, out=(p.stdout + p.stderr)[-300:])
        back = self.wait_back(t_req, prev, require_down=True, what="power-on %d" % n)
        s0 = self.stage0_verify("power_on")
        if mcc is not None:
            done = mcc_wait_log(Path(mcc["log"]), timeout=30.0)
            if not done["complete"]:
                raise StopLoop("reset_timeout", "power-on %d: no 'FPGA configuration complete' "
                               "in the MCC log" % n, mcc=done)
        self.counts["power_on_boots"] += 1
        row = dict(n=n, **back, boot_count=s0["fields"]["boot_count"],
                   booted_from=s0.get("booted_from"), confirmed=s0.get("linux_confirmed"))
        self.boot_rows.append(row)
        self.ev.write("boot_ok", **row)
        return row

    # -- one-shot drills ----------------------------------------------------- #

    def overlay_named(self, name: str) -> Optional[Path]:
        for o in self.overlays:
            if o.name == name:
                return o
        return None

    def sweep(self) -> None:
        done = []
        for o in self.overlays:
            rec = self.swap(o, tag="sweep")
            done.append({"rm": o.name, "rm_id": rec["rm_id"], "verified": rec["verified"],
                         "dur_s": rec["dur_s"]})
        self.drills.append({"drill": "sweep", "ok": True, "rms": done})
        self.ev.write("drill", drill="sweep", ok=True, n=len(done), rms=done)

    def regress(self) -> None:
        """W1's regression subset on Linux: proof 3 (version/USR_ACCESS), the MAC test
        (VPHY/GENCHK), proof 6 (DUT egress on eth_ss), proof 8' (JTAG on nanosoc),
        R (the reboot verb)."""
        out: Dict[str, Any] = {}
        v = self.control(lambda c: c.version(), "version")
        out["version"] = {"impl": v.impl, "usr_access": v.usr_access, "skew": v.skew,
                          "ver32": v.ver32, "lmb_kb": v.lmb_kb}
        if v.impl != "linux" or v.skew is not False or not v.usr_access or \
                (self.a.expect_ver32 is not None and int(v.usr_access, 16) != self.a.expect_ver32):
            raise StopLoop("regress", "version: %s" % out["version"])
        eth = self.overlay_named("eth_ss")
        if eth is not None:
            self.swap(eth, tag="regress_swap")
            self.sleep(self.real_gap(20.0))
            out["mactest"] = self.mac_clean()
            frame, last = self.control(lambda c: c.read_dut_frame(), "dutrx")
            out["dutrx"] = {"frame_hex": frame.hex()[:32] if frame else None}
            if not frame or not frame.hex().startswith(self.a.dutrx_prefix):
                raise StopLoop("regress", "DUT egress: no frame starting %s (got %s)"
                               % (self.a.dutrx_prefix, out["dutrx"]["frame_hex"]))
        else:
            out["mactest"] = "skipped (no eth_ss overlay: GENCHK's checker needs a DUT that sends)"
            out["dutrx"] = "skipped (no eth_ss overlay)"
        nano = self.overlay_named("nanosoc")
        jp = self.board.jtag_port
        try:
            if nano is not None and self.a.jtag_cmd:
                self.swap(nano, tag="regress_swap")
                # a claimed board serves 6921 to its own 127.0.0.1 only: the command
                # reaches it through the tunnel's end, {jtag_host}:{jtag_port}
                with self.debug_route(jp) as (host, ports, via):
                    cmd = self.a.jtag_cmd.replace("{jtag_host}", host).replace(
                        "{jtag_port}", str(ports[jp]))
                    env = dict(os.environ, MPS3_JTAG_HOST=host, MPS3_JTAG_PORT=str(ports[jp]))
                    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env,
                                       timeout=120, stdin=subprocess.DEVNULL)
                text = p.stdout + p.stderr
                out["jtag"] = {"rc": p.returncode, "halted": "halted" in text, "via": via,
                               "tap": "0x6ba00477" in text, "tail": text[-300:]}
                if p.returncode != 0 or "halted" not in text:
                    raise StopLoop("regress", "JTAG on nanosoc (via %s): rc=%d halted=%s%s" % (
                        via, p.returncode, "halted" in text,
                        LOCK_HINT if "locked" in text else ""))
            else:
                with self.debug_route(jp) as (host, ports, via):
                    tdo = self.session_retry(lambda: self.board.jtag_session(host, ports[jp]),
                                             host, ports[jp], "JTAG")
                out["jtag"] = "protocol-level only (%s, via %s)" % (tdo, via)
        except SshTunnelError as exc:
            raise StopLoop("regress", "JTAG: the ssh tunnel: %s" % exc)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StopLoop("regress", "JTAG: %s: %s" % (type(exc).__name__, exc))
        out["reboot"] = self.warm_reset("reboot_verb")
        self.drills.append(dict({"drill": "regress", "ok": True}, **out))
        self.ev.write("drill", drill="regress", ok=True, **out)

    def mac_clean(self) -> Dict[str, Any]:
        """The MAC test as this board can prove it (silicon, 2026-09-27). GENCHK's checker
        counts frames the DUT TRANSMITS (RX_CNT), so rx moves only while an RM sends --
        eth_ss's PAUSE beacon, about one frame a second, so the rounds are spaced
        --mac-gap apart (run back to back, rx did not move). A fault injected into the
        generator's frames reaches the checker only through a DUT that echoes them, and
        no RM here does (bad_fcs left err at 0 with eth_ss), so that round is reported
        SKIPPED rather than passed. Checked: tx and rx advance, err does not, and the VPHY
        link down/up events are accepted."""
        gap = self.real_gap(self.a.mac_gap)

        def rnd(c: Any) -> Any:
            r = c.macgen(gen=True, chk=True, inject="none")
            if not r.ok:
                raise StopLoop("regress", "MAC test: macgen rejected: %s" % getattr(r, "err", ""))
            return r

        rows = [self.control(rnd, "MAC test")]
        for _ in range(2):
            self.sleep(gap)
            rows.append(self.control(rnd, "MAC test"))
        base, clean = rows[0], rows[-1]
        rec: Dict[str, Any] = {"tx": [r.tx for r in rows], "rx": [r.rx for r in rows],
                               "err": [r.err for r in rows], "gap_s": self.a.mac_gap}
        if not (clean.tx > base.tx and clean.rx > base.rx):
            raise StopLoop("regress", "MAC test: clean traffic did not advance counters "
                           "(tx %s, rx %s over %.0f s)" % (rec["tx"], rec["rx"], 2 * self.a.mac_gap))
        if clean.err != base.err:
            raise StopLoop("regress", "MAC test: clean traffic produced errors (err %s)" % rec["err"])
        for event in ("down", "up"):
            r = self.control(lambda c, e=event: c.link(e), "VPHY link %s" % event)
            if not r.ok:
                raise StopLoop("regress", "MAC test: VPHY link %s refused: %s"
                               % (event, getattr(r, "err", "")))
        rec.update(passed=True, link="down,up accepted", inject=MAC_INJECT_SKIPPED)
        return rec

    def set_clk(self) -> None:
        """Every DUT-clock preset through the MMCM DRP: each must answer ok + LOCKED
        and show up in stats (dut_mhz, mmcm); then back to the preset that was in
        force before (50 MHz, the BD default, unless someone changed it)."""
        before = self.probe()
        self.check(before)
        mhz0 = before.get("dut_mhz") or 50
        home = "%dmhz" % mhz0 if "%dmhz" % mhz0 in self.a.clk_presets else "50mhz"
        rows: List[Dict[str, Any]] = []

        def go(c: Any) -> None:
            del rows[:]                          # a busy retry starts the presets over
            for preset in list(self.a.clk_presets) + [home]:
                r = c.set_clk(preset)
                st = c.stats()
                row = {"preset": preset, "ok": r.ok, "locked": r.locked, "dut_mhz": st.dut_mhz,
                       "mmcm": st.mmcm}
                rows.append(row)
                want = int(preset[:-3]) if preset[:-3].isdigit() else None
                if not (r.ok and r.locked and st.mmcm) or (want is not None and st.dut_mhz != want):
                    raise StopLoop("drill", "set_clk %s: %s" % (preset, row), set_clk=list(rows))
        self.control(go, "set_clk")
        self.drills.append({"drill": "set_clk", "ok": True, "rows": rows, "home": home})
        self.ev.write("drill", drill="set_clk", ok=True, home=home, rows=rows)

    def fallback(self) -> None:
        """Flip one payload byte of slot A; a warm reset must boot slot B (stage0's
        region CRC catches it); write the byte back; the next reset boots A again."""
        cmd = CORRUPT_TMPL.format(dev=self.a.slot_a_dev, off=self.a.fallback_offset)
        rc, out, err, _ = self.board.ssh(cmd)
        m = re.search(r"orig=(\d+) new=(\d+)", out)
        if rc != 0 or not m or m.group(1) == m.group(2):
            raise StopLoop("drill", "slot-A corruption did not take: rc=%d %r" % (rc, (out or err)[-200:]))
        orig = m.group(1)
        self.ev.write("drill_step", drill="fallback", step="corrupted", orig=orig, new=m.group(2))
        try:
            fb = self.warm_reset("reboot_verb", event="warm_fallback")
        finally:
            rc2, out2, err2, _ = self.board.ssh(
                RESTORE_TMPL.format(dev=self.a.slot_a_dev, off=self.a.fallback_offset, orig=orig))
            self.ev.write("drill_step", drill="fallback", step="restored", rc=rc2, out=out2.strip())
        if rc2 != 0 or ("now=%s" % orig) not in out2:
            raise StopLoop("drill", "slot A NOT restored (rc=%d %r): restore it by hand "
                           "(mps3-slot write A <image> --force) before anything else" % (rc2, out2))
        back = self.warm_reset("reboot_verb", event="warm")
        self.counts["fallback_drills"] += 1
        self.drills.append({"drill": "fallback", "ok": True, "fell_back_to": fb["booted_from"],
                            "restored_to": back["booted_from"], "orig_byte": orig})
        self.ev.write("drill", drill="fallback", ok=True, fell_back_to=fb["booted_from"],
                      restored_to=back["booted_from"])

    def forensics(self) -> None:
        """Everything that can still be read, each part on its own: forensics never
        raise (a crash FAIL collects them too)."""
        out: Dict[str, Any] = {}

        def grab(key: str, fn: Callable[[], Any]) -> Any:
            try:
                out[key] = fn()
            except Exception as exc:              # noqa: BLE001 -- forensics never raise
                out[key] = "%s: %s" % (type(exc).__name__, exc)
            return out[key]
        grab("identify", lambda: self.board.ident(timeout=1.0, retries=1))
        grab("shell", lambda: self.control(self.board.shell, "forensics"))
        grab("log_tail", lambda: self.control(self.board.log_tail, "forensics log"))
        res = grab("ssh", lambda: self.board.ssh(FORENSIC_CMD))
        if isinstance(res, tuple):
            rc, so, se, _ = res
            out["ssh_rc"], out["ssh_out"] = rc, (so or se)[-3000:]
            del out["ssh"]
            if rc == 0 and self.a.stage0:
                grab("stage0", self.board.stage0)
        self.ev.write("forensics", **out)

    def fail(self, stop: StopLoop) -> None:
        rec = {"kind": stop.kind, "detail": stop.detail}
        rec.update(stop.extra)
        self.fails.append(rec)
        self.ev.write("FAIL", **rec)
        if stop.kind not in ("stopped", "lease"):
            self.forensics()


# --------------------------------------------------------------------------- #
# the scheduler
# --------------------------------------------------------------------------- #


def drill_plan(a: argparse.Namespace) -> List[Tuple[float, str]]:
    """(time, drill) in UNSCALED seconds for the accel profile."""
    dur, pa, pu = a.duration, a.phase_a, a.phase_u
    b0 = pa + pu
    plan: List[Tuple[float, str]] = [(0.0, "sweep"), (0.0, "regress"), (0.0, "set_clk")]
    if b0 < dur:
        plan.append((b0, "set_clk"))
    if a.boots_a:
        step = pa / (a.boots_a + 1)
        plan += [(step * (i + 1), "power_on") for i in range(a.boots_a)]
    for start, end in ((0.0, pa), (b0, dur)):
        t = start + 1.25 * H
        while a.mps3_reboot_every and t < end:
            plan.append((t, "mps3_reboot"))
            t += a.mps3_reboot_every
        t = start + 0.75 * H
        while a.wdog_every and t < end:
            plan.append((t, "wdog_trip"))
            t += a.wdog_every
    t = b0 + 0.5 * H
    while a.mcc_every_b and t < dur:
        plan.append((t, "power_on"))
        t += a.mcc_every_b
    # netboot: slot A lives on the dead card -- no fallback drill (judge says "skipped")
    if a.fallback_at is not None and a.fallback_at < pa and not getattr(a, "netboot", None):
        plan.append((a.fallback_at, "fallback"))
    order = {"sweep": 0, "regress": 1, "set_clk": 2}
    return sorted(plan, key=lambda x: (x[0], order.get(x[1], 9)))


def run_schedule(r: Runner, profile: str) -> Tuple[str, Dict[str, Any]]:
    a = r.a
    dur = r.sc(a.duration)
    recur = [("heartbeat", a.heartbeat_every), ("sample", a.sample_every), ("poll", a.poll_every),
             ("ssh", a.ssh_every), ("churn", a.churn_every), ("debug", a.debug_every),
             ("swap", a.swap_every)]
    recur = [(n, r.sc(e)) for n, e in recur if e and (n != "heartbeat" or a.heartbeat_cmd)]
    drills = [(r.sc(t), d) for t, d in drill_plan(a)] if profile == "accel" else []
    t0 = time.monotonic()
    due = {n: t0 + (e if n == "swap" and profile == "accel" else 0.0) for n, e in recur}
    boots = 0
    early: Optional[str] = None
    ssh_n = 0
    r.ev.write("plan", profile=profile, drills=[(round(t / r.scale / H, 2), d) for t, d in drills],
               recurring={n: round(e / r.scale, 1) for n, e in recur})

    def step(name: str) -> None:
        nonlocal boots, ssh_n
        if name == "heartbeat":
            r.heartbeat()
        elif name == "sample":
            r.sample()
        elif name == "poll":
            r.poll()
        elif name == "ssh":
            ssh_n += 1
            r.login(stage0=(ssh_n % a.stage0_every_logins == 1) or a.stage0_every_logins == 1)
        elif name == "churn":
            r.churn()
        elif name == "debug":
            r.debug()
        elif name == "swap":
            r.swap()
        elif name == "sweep":
            r.sweep()
        elif name == "regress":
            r.regress()
        elif name == "power_on":
            boots += 1
            if a.reset == "none":
                r.ev.write("skip", drill="power_on", why="--reset none")
            else:
                r.power_on(boots)
        elif name in ("mps3_reboot", "wdog_trip"):
            r.warm_reset(name)
        elif name == "fallback":
            r.fallback()
        elif name == "set_clk":
            r.set_clk()

    try:
        r.netboot_start()
        r.probe()
        r.poll()
        r.login(stage0=True)
        while True:
            now = time.monotonic()
            if now - t0 >= dur:
                break
            ran = False
            while drills and drills[0][0] <= now - t0:
                _, d = drills.pop(0)
                try:
                    try:
                        step(d)
                    except StopLoop:
                        raise
                    except Exception as exc:          # noqa: BLE001 -- never a bare traceback
                        raise crash_stop(exc)
                except StopLoop as stop:
                    if stop.kind in ("stopped", "lease"):
                        raise
                    r.fail(stop)
                    if not a.keep_going:
                        raise _Done()
                    r.prev = None
                    r.recover()
                ran = True
            for name, every in recur:
                if time.monotonic() >= due[name]:
                    try:
                        try:
                            step(name)
                        except StopLoop:
                            raise
                        except Exception as exc:      # noqa: BLE001 -- never a bare traceback
                            raise crash_stop(exc)
                    except StopLoop as stop:
                        if stop.kind in ("stopped", "lease"):
                            raise
                        r.fail(stop)
                        if not a.keep_going:
                            raise _Done()
                        r.prev = None
                        r.recover()
                    due[name] += every
                    while due[name] <= time.monotonic():
                        due[name] += every
                    ran = True
            if not ran:
                nxt = min([v for v in due.values()] + ([t0 + drills[0][0]] if drills else []) +
                          [t0 + dur])
                r.sleep(max(0.0, nxt - time.monotonic()))
        r.poll()
        r.login(stage0=True)
        r.sample()
    except _Done:
        pass
    except StopLoop as stop:
        r.fail(stop)
        if stop.kind in ("stopped", "lease"):
            early = stop.kind
    except Exception as exc:                      # noqa: BLE001 -- never a bare traceback
        r.fail(crash_stop(exc))
    ran_s = time.monotonic() - t0
    return judge(r, profile, dur, ran_s, early, pending=[d for _, d in drills])


class _Done(Exception):
    pass


def judge(r: Runner, profile: str, dur: float, ran_s: float, early: Optional[str],
          pending: List[str]) -> Tuple[str, Dict[str, Any]]:
    a = r.a
    s: Dict[str, Any] = dict(r.counts, profile=profile, ran_s=round(ran_s, 1),
                             duration_s=round(dur, 1), fails=len(r.fails),
                             fail_kinds=[f["kind"] for f in r.fails], drills_not_run=pending)
    s["ssh_lat_p99_s"] = p99(r.lat_ssh)
    if r.t_back:
        s["t_6900_median_s"] = round(statistics.median(x[1] for x in r.t_back), 1)
        s["t_6900_max_s"] = round(max(x[1] for x in r.t_back), 1)
        s["t_ssh_max_s"] = round(max(x[2] for x in r.t_back), 1)
    po = [x for x in r.t_back if x[0].startswith("power-on")]
    if po:
        s["power_on_t_6900_median_s"] = round(statistics.median(x[1] for x in po), 1)
    problems: List[str] = []
    undecided: List[str] = []

    # coverage minimums (80 % of the schedule, so a slow swap or two never fails a run)
    def need(key: str, want: float) -> None:
        if want >= 1 and s.get(key, 0) < int(want):
            undecided.append("%s %d < %d" % (key, s.get(key, 0), int(want)))
    need("swaps", 0.8 * dur / r.sc(a.swap_every) if a.swap_every else 0)
    need("ssh", 0.8 * dur / r.sc(a.ssh_every) if a.ssh_every else 0)
    need("samples", 0.8 * dur / r.sc(a.sample_every) if a.sample_every else 0)
    if profile == "accel":
        planned = drill_plan(a)
        need("power_on_boots", sum(1 for _, d in planned if d == "power_on") if a.reset != "none" else 0)
        need("debug", 0.8 * dur / r.sc(a.debug_every) if a.debug_every else 0)
        warm = s["mps3_reboots"] + s["wdog_trips"] + s["reboot_verbs"]
        s["wdog_resets"] = warm
        if a.duration >= 12 * H and warm < 8:
            undecided.append("wdog_resets %d < 8" % warm)
        for d in ("sweep", "regress", "set_clk"):
            if not any(x["drill"] == d for x in r.drills):
                undecided.append("drill %s did not complete" % d)
        if a.fallback_at is not None and a.fallback_at < a.phase_a and not s["fallback_drills"] \
                and not r.netboot:
            undecided.append("the fallback drill did not complete")
        if a.reset != "none" and s["power_on_boots"] < min(10, a.boots_a):
            undecided.append("fewer than %d power-on boots" % min(10, a.boots_a))

    # leaks: least-squares growth across the continuous-uptime window
    lo, hi = (r.sc(a.phase_a), r.sc(a.phase_a + a.phase_u)) if profile == "accel" else (0.0, dur)
    win = [x for x in r.samples if lo <= x["el_s"] <= hi]
    leaks: Dict[str, Any] = {}
    for key in list(LEAK_LIMITS_KB) + list(LEAK_DECLINE_LIMITS_KB):
        pts = [(x["el_s"] / r.scale, float(x[key])) for x in win if x.get(key) is not None]
        sl = slope_per_s(pts)
        if sl is None:
            leaks[key] = None
            continue
        growth = sl * ((hi - lo) / r.scale)
        leaks[key] = round(growth, 1)
        if key in LEAK_LIMITS_KB and growth > LEAK_LIMITS_KB[key]:
            problems.append("leak: %s grew %.0f over the window (limit %d)"
                            % (key, growth, LEAK_LIMITS_KB[key]))
        if key in LEAK_DECLINE_LIMITS_KB and -growth > LEAK_DECLINE_LIMITS_KB[key]:
            problems.append("leak: %s fell %.0f over the window (limit %d)"
                            % (key, -growth, LEAK_DECLINE_LIMITS_KB[key]))
    s["leak_growth_over_window"] = leaks
    if all(v is None for v in leaks.values()):
        undecided.append("leaks: fewer than 3 samples in the window")

    # swap p99 stability: the window's last third against its first third
    sw = [(t, d) for t, _, d in r.swap_times if lo <= t <= hi]
    if len(sw) >= 30:
        third = (hi - lo) / 3.0
        early_p = p99([d for t, d in sw if t < lo + third])
        late_p = p99([d for t, d in sw if t >= hi - third])
        s["swap_p99_first_third_s"] = None if early_p is None else round(early_p, 2)
        s["swap_p99_last_third_s"] = None if late_p is None else round(late_p, 2)
        if early_p is not None and late_p is not None and late_p > 1.25 * early_p + 2.0:
            problems.append("swap p99 grew %.1f s -> %.1f s" % (early_p, late_p))
    else:
        s["swap_p99_note"] = "fewer than 30 swaps in the window: not evaluated"
        if profile == "accel" and a.duration >= 12 * H:
            undecided.append("swap p99: fewer than 30 swaps in the window")
    if r.lat_ssh and p99(r.lat_ssh) > a.ssh_p99_max:
        problems.append("ssh p99 %.1f s > %.1f s" % (p99(r.lat_ssh), a.ssh_p99_max))

    s["criterion"] = CRITERION[profile]
    netboot_summary(r, s, profile)
    s["problems"], s["undecided"] = problems, undecided
    if any(f["kind"] not in ("stopped", "lease") for f in r.fails) or problems:
        return "FAIL", s
    if early or ran_s + 1e-6 < dur or undecided:
        return "INCONCLUSIVE", s
    return "PASS", s


def netboot_summary(r: Runner, s: Dict[str, Any], profile: str) -> None:
    """Netboot's additions to a verdict: what was SKIPPED (said, never a silent
    pass), the deliberate deviation, the push/claim timings, the criterion."""
    if not r.netboot:
        return
    a = r.a
    s["netboot"] = True
    skipped: List[str] = []
    if profile == "accel" and a.fallback_at is not None and a.fallback_at < a.phase_a:
        skipped.append("fallback drill: netboot (slot A lives on the dead user microSD)")
    if s.get("unconfirmed_expected"):
        skipped.append("stage0 CONFIRM on %d read(s): netboot with the dead card, boot-health "
                       "healthy=0 reasons=usd-driver-path, so harnessd must NOT confirm -- "
                       "checked that it did not, and that each warm entry judged the rescue "
                       "attempt UNCONFIRMED" % s["unconfirmed_expected"])
    if any(d.get("drill") == "regress" for d in r.drills):
        skipped.append("MAC test fault-inject round: " + MAC_INJECT_SKIPPED)
    s["skipped"] = skipped
    s["deviations"] = ["mmc_spi unbound from the dead card after every RAM boot (%d unbinds): "
                       "it retries the card every ~2.3 s forever and would pollute the leak and "
                       "load criteria" % s.get("card_unbinds", 0)]
    rows = [x for x in r.netboot_rows if x.get("push_s") is not None]
    if rows:
        s["netboot_push_max_s"] = max(x["push_s"] for x in rows)
        s["netboot_t_rescue_max_s"] = max(x["t_rescue_s"] for x in rows)
        s["netboot_t_ssh_max_s"] = max(x.get("t_ssh_s") or 0 for x in rows)
    s["criterion"] = s.get("criterion", "") + " " + NETBOOT_CRITERION


NETBOOT_CRITERION = (
    "NETBOOT (--netboot, the user microSD dead): every reset lands stage0 in rescue and gets "
    "exactly one push of the RAM image (stage0_push rc 0), then 6900, the claim, ssh and the "
    "mmc_spi unbind (a recorded deviation) before the load resumes; every boot is booted_from "
    "RESCUE; a warm reset moves boot_count and n_boot_rescue by +1 and leaves n_boot_a/b and "
    "n_fallback alone; a power-on shows exactly one hand-off since the block was initialised; "
    "Linux confirms a boot iff its boot-health says healthy=1 -- with the dead card it says "
    "healthy=0 reasons=usd-driver-path, so the check is that it did NOT confirm and the next "
    "entry judged that rescue attempt UNCONFIRMED; the slot-A fallback drill is skipped.")


CRITERION = {
    "accel": (
        "PASS = (1) zero FAIL events for the whole duration: no wedge or offline, every reset "
        "explained by a drill, no harnessd respawn, every swap verified, every SSH login OK, "
        "no kernel oops, every XVC/JTAG session answered; (2) every boot came from slot A and "
        "Linux CONFIRMED it (the fallback drill's one boot from B excepted), power-ons show "
        "exactly one hand-off since the block was initialised (boot_count counts every stage0 "
        "entry: 4 on a silicon cold boot), warm resets boot_count +1 with WRS and the previous "
        "boot CONFIRMED; "
        "(3) the 13-RM sweep 13/13, the W1 subset (version/USR_ACCESS, MAC test, DUT egress, "
        "JTAG, reboot verb), every set_clk preset LOCKED with dut_mhz following it (then back "
        "to the default), and the slot-A fallback drill all passed; (4) >= 10 power-on boots "
        "in phase A all OK (the 10/10) and >= 8 WDOG resets in total; (5) phase U flat: "
        "least-squares growth over 12 h of harnessd RSS <= 1 MiB, harnessd fds <= 2, "
        "SUnreclaim <= 8 MiB, file handles <= 64, /persist <= 4 MiB, MemAvailable decline "
        "<= 16 MiB; swap p99 in U's last third <= 1.25 x its first third + 2 s; SSH p99 <= "
        "10 s; (6) coverage >= 80 % of the schedule (swaps, logins, samples, debug)"),
    "tail": (
        "PASS = zero FAIL events for the whole duration (no wedge/offline, no reset, no "
        "respawn, every swap verified, every SSH login OK, no kernel oops, stage0 counters "
        "unmoved); flat resources over the whole run (the accel limits); coverage >= 80 %"),
}


def run_boots(r: Runner) -> Tuple[str, Dict[str, Any]]:
    a = r.a
    t0 = time.monotonic()
    try:
        r.heartbeat()
        r.netboot_start()
        r.poll()
        r.login(stage0=True)
        for i in range(1, a.n + 1):
            r.power_on(i)
            if i < a.n:
                r.sleep(r.sc(a.gap))
    except StopLoop as stop:
        r.fail(stop)
    except Exception as exc:                      # noqa: BLE001 -- never a bare traceback
        r.fail(crash_stop(exc))
    rows = r.boot_rows
    s: Dict[str, Any] = {"boots_ok": len(rows), "boots_wanted": a.n,
                         "fail_kinds": [f["kind"] for f in r.fails],
                         "ran_s": round(time.monotonic() - t0, 1),
                         "t_6900_median_s": statistics.median([x["t_6900_s"] for x in rows]) if rows else None,
                         "t_6900_max_s": max([x["t_6900_s"] for x in rows]) if rows else None,
                         "t_ssh_max_s": max([x["t_ssh_s"] for x in rows]) if rows else None,
                         "criterion": "PASS = N/N power-on boots: DOWN then back on 6900 with the "
                                      "expected shell_id and a restarted os_up_ms, SSH, stage0 "
                                      "exactly one hand-off since the block was initialised, "
                                      "HANDOFF, booted_from the expected slot, CONFIRMED, and "
                                      "'FPGA configuration complete' in the MCC log"}
    s.update({k: v for k, v in r.counts.items() if k in (
        "netboot_pushes", "claims", "card_unbinds", "unconfirmed_expected")})
    netboot_summary(r, s, "boots")
    if any(f["kind"] not in ("stopped", "lease") for f in r.fails):
        return "FAIL", s
    return ("PASS" if len(rows) == a.n else "INCONCLUSIVE"), s


# --------------------------------------------------------------------------- #
# the paced MCC REBOOT (2026-09-24 W1: 0x72BB0A36 was fielded this way, no hands)
# --------------------------------------------------------------------------- #

MCC_ACK = "Rebooting"
MCC_CFG = "Configuring FPGA from file"
MCC_DONE = "FPGA configuration complete"
MCC_FAIL = re.compile(r"(?i)configuration failed|fpga configuration error")
MCC_PROMPT = "Cmd>"
#: how long a bare CR may take to bring back the prompt (bootrate.HUB_MCC_REBOOT_PY's 3 s)
MCC_PROMPT_S = 3.0


def _ancestors() -> set:
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


def other_readers(tty: str) -> List[Tuple[int, str]]:
    """Other processes (not us, not our ancestors such as `sg fpga -c "..."`) whose
    command line names the tty or its target, or that hold it open (fds are visible
    for our own user only; root's `cat` shows through its command line). A second
    reader on tty_00 made the first W1 REBOOT a no-op."""
    real = os.path.realpath(tty)
    names = {tty, real}
    mine = _ancestors()
    hits: List[Tuple[int, str]] = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in mine:
            continue
        pid = int(d)
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if any(re.search(r"(^|[\s=:'\"])%s($|[\s,'\"])" % re.escape(n), cmd) for n in names):
            hits.append((pid, cmd[:160]))
            continue
        try:
            for fd in os.listdir("/proc/%d/fd" % pid):
                if os.path.realpath("/proc/%d/fd/%s" % (pid, fd)) == real:
                    hits.append((pid, "(holds it open) " + cmd[:120]))
                    break
        except OSError:
            pass
    return hits


def _open_raw(tty: str, baud: int) -> int:
    import termios
    import tty as ttymod
    fd = os.open(tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    ttymod.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[2] |= termios.CLOCAL | termios.CREAD
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG)
    attrs[4] = attrs[5] = getattr(termios, "B%d" % baud)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def mcc_reboot(tty: str, log: Path, *, pace: float = 0.1, settle: float = 1.0,
               capture_s: float = 150.0, ack_s: float = 10.0, baud: int = 115200,
               wait_done: bool = True, check_readers: bool = True,
               check_prompt: bool = True, prompt_s: float = MCC_PROMPT_S) -> Dict[str, Any]:
    """ONE paced REBOOT. rc 0 = acknowledged (with ``wait_done``: and the FPGA
    configuration completed), 1 = no `Rebooting` echo, 2 = another reader holds
    the tty (nothing was sent), 3 = a bare CR did not bring back an intact
    `Cmd>` within ``prompt_s`` (REBOOT NOT sent: a reader this account cannot
    see -- a root `cat`, an fpgahub share -- or the MCC in its `Debug>` menu),
    4 = acknowledged but no `FPGA configuration complete`. Without
    ``wait_done`` the capture carries on in the background for ``capture_s``
    and :func:`mcc_wait_log` reads the verdict later."""
    log = Path(log)
    res: Dict[str, Any] = {"tty": tty, "log": str(log), "rc": 1}
    if check_readers:
        others = other_readers(tty)
        if others:
            res.update(rc=2, why="other reader(s) on %s: %s -- stop them first (exactly ONE "
                               "reader, or the REBOOT is a no-op)" % (tty, others))
            return res
    fd = _open_raw(tty, baud)
    buf = bytearray()
    lock = threading.Lock()
    stop = threading.Event()
    fh = open(log, "ab", buffering=0)
    state = {"fd": fd}

    def reader() -> None:
        t_end = time.monotonic() + capture_s
        while not stop.is_set() and time.monotonic() < t_end:
            try:
                ready, _, _ = select.select([state["fd"]], [], [], 0.2)
                if ready:
                    chunk = os.read(state["fd"], 4096)
                    if chunk:
                        fh.write(chunk)
                        with lock:
                            buf.extend(chunk)
            except OSError:                        # USB re-enumeration: reopen
                try:
                    os.close(state["fd"])
                except OSError:
                    pass
                for _ in range(40):
                    time.sleep(0.25)
                    try:
                        state["fd"] = _open_raw(tty, baud)
                        break
                    except OSError:
                        continue
        fh.close()
        try:
            os.close(state["fd"])
        except OSError:
            pass

    th = threading.Thread(target=reader, name="mcc-capture", daemon=True)
    th.start()

    def text() -> str:
        with lock:
            return bytes(buf).decode("utf-8", "replace")

    t_cr = time.monotonic()
    os.write(fd, b"\r")
    if check_prompt:
        while time.monotonic() - t_cr < prompt_s and MCC_PROMPT not in text() \
                and "Debug>" not in text():
            time.sleep(0.05)
        if MCC_PROMPT not in text():
            stop.set()
            th.join(2)
            res.update(rc=3, prompt=text()[-120:],
                       why="%s after a bare CR on %s (got %r): REBOOT NOT sent. A reader "
                           "this account cannot see (a root cat, an fpgahub share) or the "
                           "MCC's Debug> menu" % ("Debug> submenu, not Cmd>" if "Debug>" in text()
                                                  else "no intact Cmd>", tty, text()[-60:]))
            return res
    time.sleep(max(0.0, settle - (time.monotonic() - t_cr)))
    for ch in b"REBOOT":
        os.write(fd, bytes([ch]))
        time.sleep(pace)
    os.write(fd, b"\r")
    t = time.monotonic()
    while time.monotonic() - t < ack_s and MCC_ACK not in text():
        time.sleep(0.1)
    res["ack"] = MCC_ACK in text()
    if not res["ack"]:
        stop.set()
        th.join(2)
        res["why"] = "no '%s' within %ss; the MCC line read %r" % (MCC_ACK, ack_s, text()[-120:])
        return res
    res["rc"] = 0
    if wait_done:
        while th.is_alive() and MCC_DONE not in text() and not MCC_FAIL.search(text()):
            time.sleep(0.2)
        t2 = time.monotonic()
        while th.is_alive() and time.monotonic() - t2 < 15 and \
                not text().rstrip().endswith(MCC_PROMPT):
            time.sleep(0.2)
        stop.set()
        th.join(3)
        res.update(configuring=MCC_CFG in text(), complete=MCC_DONE in text(),
                   failed=bool(MCC_FAIL.search(text())))
        if not res["complete"]:
            res.update(rc=4, why="acknowledged, but no '%s' in the capture" % MCC_DONE)
    return res


def mcc_wait_log(log: Path, timeout: float = 90.0) -> Dict[str, Any]:
    t = time.monotonic()
    txt = ""
    while True:
        try:
            txt = Path(log).read_bytes().decode("utf-8", "replace")
        except OSError:
            txt = ""
        if MCC_DONE in txt or MCC_FAIL.search(txt) or time.monotonic() - t >= timeout:
            break
        time.sleep(0.5)
    return {"configuring": MCC_CFG in txt, "complete": MCC_DONE in txt,
            "failed": bool(MCC_FAIL.search(txt)), "log": str(log)}


# --------------------------------------------------------------------------- #
# dry run: FakeShell(profile="linux") + fake ssh, stage0, debug servers, resets
# --------------------------------------------------------------------------- #

SIM_STATIC_ID = 0x5A5A0B02
SIM_VER32 = 0x01000001
SIM_RMS = (("greybox", 0x01000000), ("led", 0x0100001E), ("eth_ss", 0x01000002),
           ("nanosoc", 0x01000001))
SIM_PAUSE_FRAME = bytes.fromhex("0180c20000013253454c530288080001") + bytes(48)
#: THE BUSY-ICAP WINDOW (B1 v4 2026-09-25, finding h). A swap first streams the
#: OUTGOING RM's cached clearing into the ICAP, and a DAP RM's is big (nanosoc:
#: 167,308 B), so the partial of the swap AWAY from it arrives before the shell
#: can take it: harnessd PARKS it on 6910 and REJECTS it on TFTP. The dry run
#: models that (FakeShell clearing_stream_bytes_per_s), so a swap drill on a
#: transport that cannot survive it fails here, not at hour one of the soak.
SIM_CLEARING_BYTES = {"nanosoc": 167_308}
SIM_ICAP_STREAM_S = 0.4          # nanosoc's clearing stream; the 128 B ones: ~0.3 ms
SIM_ICAP_BYTES_PER_S = SIM_CLEARING_BYTES["nanosoc"] / SIM_ICAP_STREAM_S


def make_overlay(root: Path, name: str, rm_id: int, static_id: int) -> Path:
    """A synthetic overlay in gen_manifest.py's shape (as host/pyverify/tests/
    test_e2e_deploy.py's make_synthetic_overlay); the clearing is DAP-RM sized
    for the RMs in :data:`SIM_CLEARING_BYTES`."""
    import zlib
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    clr, prt = b"\xC1\x0E\xA2\x00" * 32, bytes([rm_id & 0xFF, 0x75, 0x00, 0x0D]) * 96
    if name in SIM_CLEARING_BYTES:
        clr = b"\xC1\x0E\xA2\x00" * (SIM_CLEARING_BYTES[name] // 4)
    (d / (name + "_clear.bin")).write_bytes(clr)
    (d / (name + ".bin")).write_bytes(prt)
    man = {"schema": 1, "static_id": "0x%08X" % static_id, "rm_id": "0x%08x" % rm_id,
           "rm_name": name,
           "clearing": {"file": name + "_clear.bin", "len": len(clr),
                        "crc32": "0x%08x" % (zlib.crc32(clr) & 0xFFFFFFFF)},
           "partial": {"file": name + ".bin", "len": len(prt),
                       "crc32": "0x%08x" % (zlib.crc32(prt) & 0xFFFFFFFF)},
           "built": "2026-09-24", "vivado": "2026.1"}
    (d / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return d


class _TinyServer:
    """A one-thread TCP server for the dry run's XVC (2542) / JTAG (6921) ports.
    ``refuse()`` returning a line = the claim lock: send it and close (net-protocol
    "The lock": one line, then close); returning b"" = a single-client port still
    holding a previous connection: close with no reply."""

    def __init__(self, handler: Callable[[socket.socket], None],
                 refuse: Optional[Callable[[], Optional[bytes]]] = None):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.handler = handler
        self.refuse = refuse
        self.alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while self.alive:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                c.settimeout(2.0)
                try:
                    line = self.refuse() if self.refuse else None
                    if line is None:
                        self.handler(c)
                    elif line:
                        c.sendall(line)
                except OSError:
                    pass

    def close(self) -> None:
        self.alive = False
        self.sock.close()


#: The dry run's stand-in for ``ssh -N -L ...`` (what SshTunnel runs, via its
#: ``ssh=`` command): each -L goes to the Sim's BOARD-LOCAL listener, which the
#: claim lock never refuses. It refuses what a real ssh would: a board in rescue
#: (no sshd), an unclaimed board (no authorized_keys), and a host key that is not
#: the one the soak started with unless known_hosts is out of the way --
#: StrictHostKeyChecking=no alone connects to a changed key but, like OpenSSH,
#: disables port forwarding.
SIM_SSH_SRC = r'''
import json, os, signal, socket, sys, threading, time

def ident(port):
    for _ in range(4):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        try:
            s.sendto(b'{"op":"identify","v":1,"nonce":"0123456789abcdef"}', ("127.0.0.1", port))
            return json.loads(s.recv(4096))
        except (OSError, ValueError):
            time.sleep(0.2)
        finally:
            s.close()
    return None

def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    for x in (a, b):
        try:
            x.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

def serve(ls, dst):
    while True:
        c, _ = ls.accept()
        try:
            u = socket.create_connection(("127.0.0.1", dst), timeout=5)
        except OSError:
            c.close()
            continue
        threading.Thread(target=pump, args=(c, u), daemon=True).start()
        threading.Thread(target=pump, args=(u, c), daemon=True).start()

def main():
    a = sys.argv[1:]
    def take(flag):
        i = a.index(flag)
        v = a[i + 1]
        del a[i:i + 2]
        return v
    port, known = int(take("--sim-identify")), take("--sim-known")
    fwd = json.loads(take("--sim-map"))
    r = ident(port)
    if not r or r.get("mode") != "run":
        sys.stderr.write("ssh: connect to host 192.168.10.101 port 22: Connection refused\n")
        return 255
    ssh = r.get("ssh") or {}
    if not ssh.get("claimed"):
        sys.stderr.write("root@192.168.10.101: Permission denied (publickey).\n")
        return 255
    specs = [a[i + 1] for i, x in enumerate(a) if x == "-L"]
    if ssh.get("host_key_sha256") != known:
        if "StrictHostKeyChecking=no" not in a:
            sys.stderr.write("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                             "Host key verification failed.\n")
            return 255
        if "UserKnownHostsFile=/dev/null" not in a and specs:
            sys.stderr.write("Port forwarding is disabled to avoid man-in-the-middle attacks.\n")
            return 255
    ls = []
    for spec in specs:
        lh, lp, _rh, rp = spec.split(":")
        if rp not in fwd:
            sys.stderr.write("channel open failed: 127.0.0.1:%s\n" % rp)
            return 255
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((lh, int(lp)))
            s.listen(8)
        except OSError as e:
            sys.stderr.write("bind [%s]:%s: %s\n" % (lh, lp, e))
            return 255
        ls.append((s, int(fwd[rp])))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    for s, dst in ls:
        threading.Thread(target=serve, args=(s, dst), daemon=True).start()
    threading.Event().wait()

if __name__ == "__main__":
    sys.exit(main())
'''

#: stage0 entries a paced MCC REBOOT produces on silicon before the first hand-off
#: (boot #4 after the B2 cold boot 2026-09-26 and the re-bake 09-27); the netboot
#: Sim models it, so the power-on check is exercised with the silicon value.
SIM_COLD_ENTRIES = 4
SIM_XVC_LOCKED = b'{"ok":false,"err":"xvc locked: board claimed (use ssh)","code":"locked"}\n'
SIM_JTAG_LOCKED = b'{"ok":false,"err":"jtag locked: board claimed (use ssh)","code":"locked"}\n'


def _sim_pubkey() -> bytes:
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + os.urandom(32)
    return b"ssh-ed25519 " + base64.b64encode(blob) + b" soak-dry-run\n"


class Sim:
    """The board for --dry-run. FakeShell's documented seams plus three private
    fields (``_t0``, ``os_boot_ms``, ``_simulate_restart``, ``_lock``) model a
    respawn, a WDOG reset and a power cycle; the stage0 block is synthesised from
    pyverify.mailbox's field table, so the real ssh/devmem reader and decoder parse it.

    The board starts CLAIMED (a card-backed board is; a netbooted one was claimed by
    whoever pushed it): XVC/JTAG refuse a non-local peer, as on silicon, and are
    served board-locally behind the fake ssh forwarder (:data:`SIM_SSH_SRC`).

    ``netboot=True``: the user microSD is present but dead. Every MBV reset enters
    stage0 (the entry judges the pending rescue attempt: stage0_flow.c), which
    lands in rescue (FakeShell mode="rescue", reason "card error") until something
    pushes it an image over TFTP; ``boot_s`` later the RAM Linux runs: unclaimed,
    a new host key, booted_from RESCUE, and the dead card's boot-health (healthy=0
    reasons=usd-driver-path), so harnessd never confirms it. inject ``nocard``
    pulls the card (later boots: "no card", healthy=1, confirmed); ``pushfail``
    makes stage0 reject pushes."""

    def __init__(self, workdir: Path, *, trip_s: float = 0.4, cycle_down_s: float = 0.6,
                 netboot: bool = False, boot_s: float = 0.3):
        from pyverify.testing.fakeshell import FakeShell
        self.fake = FakeShell.ephemeral(
            profile="linux", static_id=SIM_STATIC_ID, reboot_in_ms=300, os_boot_ms=18000,
            harness_ver32=SIM_VER32, harness_usr_access=SIM_VER32,
            dut_frames=[SIM_PAUSE_FRAME] * 64,
            clearing_stream_bytes_per_s=SIM_ICAP_BYTES_PER_S).start()
        self.overlays = [make_overlay(workdir / "ovl", n, i, SIM_STATIC_ID) for n, i in SIM_RMS]
        self.trip_s, self.cycle_down_s = trip_s, cycle_down_s
        self.netboot, self.boot_s = netboot, boot_s
        self.lock = threading.Lock()
        self.ssh_broken = self.power_on_is_noop = self.rescue = self.down = False
        self.slot_a_bad = False
        self.byte = 0o123
        self.rss_kb, self.leak_kb, self.oops = 3100, 0, 0
        self._reboots_seen = 0
        f = self.fake
        # the claim key the soak claims with, and the board starting claimed by it
        self.claim_key = workdir / "sim_claim.pub"
        self.claim_key.write_bytes(_sim_pubkey())
        f.ssh_claimed, f.authorized_keys = True, self.claim_key.read_bytes()
        self.host_key_gen = 0
        f.ssh_host_key_sha256 = self.known_host_key = self._host_key(0)
        self.card_dead = netboot              # present but unreadable (netboot's premise)
        self.boot_card_dead = netboot         # ... as the RUNNING boot saw it (S99 judges once)
        self.push_fail = False
        self.pushes: List[int] = []
        self.unbound = False
        self.image = workdir / "sim_linux_slot.img"
        self.image.write_bytes(synth_s0lb_image())
        self.fake_ssh = workdir / "sim_ssh.py"
        self.fake_ssh.write_text(SIM_SSH_SRC)
        if netboot:                           # the operator's first push, already done
            self._s0_entry(power_on=True)
            self._s0_handoff()
            self.c["confirmed"] = not self.card_dead
        else:
            self._power_reset_counters()
        # the claim lock (xvc_lock): the external ports refuse a non-local peer on a
        # claimed board; the *_lo twins are the board's own 127.0.0.1 (ssh -L's end)
        self.dbg_busy = 0                     # inject dbgbusy: debug connections turned away
        self.xvc = _TinyServer(self._xvc, refuse=lambda: SIM_XVC_LOCKED if f.ssh_claimed
                               else self._dbg_refuse())
        self.jtag = _TinyServer(self._jtag, refuse=lambda: SIM_JTAG_LOCKED if f.ssh_claimed
                                else self._dbg_refuse())
        self.xvc_lo = _TinyServer(self._xvc, refuse=self._dbg_refuse)
        self.jtag_lo = _TinyServer(self._jtag, refuse=self._dbg_refuse)
        # the single-client race: the next N 6900 connections are reset the way
        # harnessd refuses an extra client (request left unread -> RST); -1 = all
        self.ctl_resets = 0
        self.ctl_resets_done = 0
        self.crash_next = False                   # inject crash: the ssh seam raises once
        self.ss_calls: List[List[str]] = []
        self._fake_start = f.start

        def _start() -> Any:                      # every (re)start keeps the gate
            res = self._fake_start()
            self._gate_control()
            return res
        f.start = _start
        self._gate_control()

        def _restart() -> None:                    # a fresh kernel: os_up_ms from 0
            if self.netboot:                       # ... netboot: via stage0 rescue
                self._reboots_seen = len(f.reboots)
                self._netboot_reset()
                return
            with f._lock:
                f._t0 = time.monotonic()
                f.os_boot_ms = 0
        f._simulate_restart = _restart

    def _dbg_refuse(self) -> Optional[bytes]:
        if self.dbg_busy > 0:
            self.dbg_busy -= 1
            return b""                            # close with no reply: still busy
        return None

    def _gate_control(self) -> None:
        """Wrap the running control server's verify_request (a socketserver hook)
        so an armed gate resets new 6900 connections."""
        f = self.fake
        for srv in list(getattr(f, "_servers", [])):
            if srv.server_address[1] != f.control_port or getattr(srv, "_soak_gated", False):
                continue
            orig = srv.verify_request

            def verify(request: Any, client_address: Any, _orig: Any = orig) -> bool:
                if self.ctl_resets:
                    if self.ctl_resets > 0:
                        self.ctl_resets -= 1
                    self.ctl_resets_done += 1
                    self._rst(request)
                    return False
                return _orig(request, client_address)
            srv.verify_request = verify
            srv._soak_gated = True

    @staticmethod
    def _rst(sock: socket.socket) -> None:
        """harnessd's refusal: accept, close at once with the request unread ->
        the kernel sends an RST (the client sees ECONNRESET, or EOF if early)."""
        try:
            sock.settimeout(0.2)
            sock.recv(1, socket.MSG_PEEK)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()
        except OSError:
            pass

    def ss(self, argv: List[str]) -> Tuple[int, str, str]:
        """The stub `ss -tnp` (the dry run never runs the real one)."""
        self.ss_calls.append(list(argv))
        port = argv[-1].rpartition(":")[2] if ":" in argv[-1] else "6900"
        pid = os.getpid()
        return 0, ("State     Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                   "ESTAB     0      0      127.0.0.1:50002    127.0.0.1:%s "
                   "users:((\"python3\",pid=%d,fd=7))\n"
                   "TIME-WAIT 0      0      127.0.0.1:50001    127.0.0.1:%s\n"
                   % (port, pid, port)), ""

    @staticmethod
    def _host_key(gen: int) -> str:
        return "SHA256:simhostkey%033d" % gen

    def ssh_prefix(self) -> List[str]:
        """The fake ``ssh`` SshTunnel runs in the dry run: the soak's external
        debug ports map to their board-local twins, 6900 to itself (the probe)."""
        f = self.fake
        fwd = {str(f.control_port): f.control_port, str(self.xvc.port): self.xvc_lo.port,
               str(self.jtag.port): self.jtag_lo.port}
        return [sys.executable, str(self.fake_ssh), "--sim-identify", str(f.identify_port),
                "--sim-known", self.known_host_key, "--sim-map", json.dumps(fwd)]

    # -- stage0 model -------------------------------------------------------- #

    def _power_reset_counters(self) -> None:
        self.c = {"boot_count": 1, "booted_from": 1, "n_boot_a": 1, "n_boot_b": 0,
                  "n_boot_rescue": 0, "n_fallback": 0, "last_verdict": 0, "verdict_from": 0,
                  "wrs": 0, "slot_a_rc": 0, "confirmed": True, "sd_result": 1,
                  "rescue_reason": 0, "rescue_sessions": 0, "fails_a": 0, "fails_b": 0}

    def _warm_entry(self) -> None:
        c = self.c
        c["boot_count"] += 1
        c["wrs"] = 1
        c["last_verdict"], c["verdict_from"] = 1, c["booted_from"]
        if self.slot_a_bad:
            c["booted_from"], c["slot_a_rc"] = 2, 6
            c["n_boot_b"] += 1
            c["n_fallback"] += 1
        else:
            c["booted_from"], c["slot_a_rc"] = 1, 0
            c["n_boot_a"] += 1

    def _s0_entry(self, *, power_on: bool) -> None:
        """Netboot: one stage0 entry (s0_status_open), then the dead card -> rescue."""
        if power_on:
            self.c = {"boot_count": SIM_COLD_ENTRIES, "booted_from": 0, "n_boot_a": 0,
                      "n_boot_b": 0, "n_boot_rescue": 0, "n_fallback": 0, "last_verdict": 0,
                      "verdict_from": 0, "wrs": 0, "slot_a_rc": 8, "confirmed": False,
                      "sd_result": 5, "rescue_reason": 5, "rescue_sessions": 0,
                      "fails_a": 0, "fails_b": 0}
        else:
            c = self.c
            c["boot_count"] += 1
            c["wrs"] = 1
            c["verdict_from"] = c["booted_from"]
            c["last_verdict"] = 1 if c["confirmed"] else 2
            if c["confirmed"] and c["booted_from"] == 3:
                c["fails_a"] = c["fails_b"] = 0    # a confirmed rescue boot: fresh start
        c = self.c
        c["booted_from"], c["confirmed"], c["slot_a_rc"] = 0, False, 8
        c["sd_result"], c["rescue_reason"] = (5, 5) if self.card_dead else (2, 2)

    def _s0_handoff(self) -> None:
        c = self.c
        c["rescue_sessions"] += 1
        c["booted_from"] = 3
        c["n_boot_rescue"] += 1
        c["confirmed"] = False                     # att_confirm zeroed at every hand-off

    def _sync_reboots(self) -> None:
        n = len(self.fake.reboots)
        while self._reboots_seen < n:
            self._reboots_seen += 1
            self._warm_entry()

    def stage0_words(self) -> List[int]:
        if not self.netboot:
            self._sync_reboots()
        c = self.c
        vals = {"magic": STAGE0_STATUS_MAGIC, "version": STAGE0_STATUS_VERSION, "size": 0x100,
                "magic_end": STAGE0_STATUS_MAGIC, "fabric_static_id": SIM_STATIC_ID,
                "fabric_ver32": SIM_VER32, "boot_count": c["boot_count"], "phase": 6,
                "booted_from": c["booted_from"], "default_slot": 1,
                "att_confirm": STAGE0_CONFIRM_MAGIC if c["confirmed"] else 0,
                "att_from": c["booted_from"], "last_verdict": c["last_verdict"],
                "verdict_from": c["verdict_from"], "reset_cause": 0x8 if c["wrs"] else 0,
                "n_boot_a": c["n_boot_a"], "n_boot_b": c["n_boot_b"],
                "n_boot_rescue": c["n_boot_rescue"], "n_fallback": c["n_fallback"],
                "slot_a_rc": c["slot_a_rc"], "boot_limit": 2, "ddr_calib": 1,
                "sd_result": c["sd_result"], "rescue_reason": c["rescue_reason"],
                "rescue_sessions": c["rescue_sessions"], "fails_a": c["fails_a"],
                "fails_b": c["fails_b"]}
        w = [0] * 64
        for name, off in STAGE0_STATUS_FIELDS:
            if name in vals:
                w[off // 4] = vals[name] & 0xFFFFFFFF
        return w

    # -- netboot: rescue, the push, the RAM boot ------------------------------- #

    def _netboot_reset(self, *, power_on: bool = False) -> None:
        with self.lock:
            self._s0_entry(power_on=power_on)
            f = self.fake
            f.stop()
            f.mode, f.hung = "rescue", False
            f.rescue_reason = "card error" if self.card_dead else "no card"
            f.rescue_status = {"boot_count": self.c["boot_count"], "ddr_calib": 1,
                               "fabric_static_id": SIM_STATIC_ID,
                               "sd_result": self.c["sd_result"],
                               "rescue_reason": self.c["rescue_reason"],
                               "rescue_sessions": self.c["rescue_sessions"]}
            f._rescue_sink = self._on_push
            f.start()

    def _on_push(self, data: bytes) -> Tuple[bool, str]:
        """stage0's in-band verdict (FakeShell's rescue_sink seam, on its TFTP thread):
        the final ACK goes out only on (True, ""); the boot lands ``boot_s`` later."""
        if self.push_fail:
            return False, "image rejected: region 0 payload CRC"
        self.pushes.append(len(data))
        threading.Timer(self.boot_s, self._netboot_up).start()
        return True, ""

    def _netboot_up(self) -> None:
        with self.lock:
            f = self.fake
            f.stop()
            f.mode, f.hung = "run", False
            self._s0_handoff()
            self.host_key_gen += 1                 # /persist is tmpfs: a new key per boot
            f.ssh_host_key_sha256 = self._host_key(self.host_key_gen)
            f.ssh_claimed, f.authorized_keys = False, None
            with f._lock:
                f._t0 = time.monotonic()
                f.os_boot_ms = 0
            # S99 judges once per boot; harnessd confirms iff it says healthy=1
            self.boot_card_dead = self.card_dead
            self.c["confirmed"] = not self.boot_card_dead
            self.unbound = False
            f.start()

    def boot_health(self) -> str:
        if not self.netboot:
            return "healthy=1\npersist=card\nnet=dhcp\nssh=1\nreasons=\n"
        if self.boot_card_dead:                    # S99mps3health, 2026-09-27 on silicon
            return "healthy=0\npersist=tmpfs\nnet=static\nssh=1\nreasons=usd-driver-path\n"
        return "healthy=1\npersist=tmpfs\nnet=static\nssh=1\nreasons=\n"

    # -- the ssh seam ---------------------------------------------------------- #

    def run(self, argv: List[str]) -> Tuple[int, str, str]:
        cmd = argv[-1]
        time.sleep(0.005)
        if self.crash_next:                        # an exception nobody expects
            self.crash_next = False
            raise RuntimeError("injected crash (dry run)")
        # dropbear is not harnessd: a hung or dead harnessd leaves ssh up; only a
        # board that is down (power cycle), in rescue, or a broken sshd refuses it
        if self.ssh_broken or self.down or self.rescue or self.fake.mode == "rescue":
            return 255, "", "ssh: connect to host 192.168.10.101 port 22: Connection timed out"
        if not self.fake.ssh_claimed:              # a fresh RAM boot: no authorized_keys
            return 255, "", "root@192.168.10.101: Permission denied (publickey)."
        if self.fake.ssh_host_key_sha256 != self.known_host_key and \
                "StrictHostKeyChecking=no" not in argv:
            return 255, "", ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                             "Host key verification failed.")
        if cmd in (TRIP_CMD, MPS3_REBOOT_CMD):
            self.fake.hung = True
            threading.Timer(self.trip_s, self._wdog_reset).start()
            return 0, "", ""
        if cmd.startswith("for a in "):
            words = self.stage0_words()
            addrs = re.findall(r"0x([0-9A-Fa-f]+)", cmd.split(";")[0])
            # pyverify's batched read ends with "@MBX-END <n>" once the shell finished
            return 0, ("".join("0x%08X\n" % words[(int(a, 16) - 0x1FE00) // 4] for a in addrs)
                       + "@MBX-END %d\n" % len(addrs)), ""
        if cmd.startswith("d=") and "orig=$b" in cmd:
            orig = self.byte
            self.byte ^= 0xFF
            self.slot_a_bad = True
            return 0, "orig=%03o new=%03o\n" % (orig, self.byte), ""
        if cmd.startswith("d=") and "now=$1" in cmd:
            m = re.search(r"printf '\\(\d+)'", cmd)
            self.byte = int(m.group(1), 8) if m else self.byte
            self.slot_a_bad = False
            return 0, "now=%03o\n" % self.byte, ""
        if cmd.startswith("drv="):                 # UNBIND_CMD
            if self.unbound:
                return 0, "U dev=spi0.0 unbound=absent err0=41 err1=41 err2=41\n", ""
            self.unbound = True
            n = 40 if self.boot_card_dead else 0
            return 0, "U dev=spi0.0 unbound=1 err0=%d err1=%d err2=%d\n" % (
                n, n + bool(n), n + bool(n)), ""
        if cmd == SAMPLE_CMD:
            self.rss_kb += self.leak_kb
            return 0, ("S rss_kb=%d fds=14 avail_kb=1843200 slab_kb=21000 sunreclaim_kb=9000 "
                       "files=352 persist_used_kb=1200 oops=%d\n" % (self.rss_kb, self.oops)), ""
        with self.fake._lock:
            os_up = (self.fake.os_boot_ms + self.fake.up_ms()) / 1000.0
        out = ("%.2f %.2f\n%suptime_s=%d\npid=%d\n"
               % (os_up, os_up * 0.9, self.boot_health(), int(os_up), 100 + self.c["boot_count"]))
        if cmd == FORENSIC_CMD:
            out += "[    1.000000] sim: dmesg tail\n"
        return 0, out, ""

    def _wdog_reset(self) -> None:
        if self.netboot:
            self._netboot_reset()
            return
        with self.lock:
            self.fake._simulate_restart()
            self._warm_entry()
            self.fake.hung = False

    def respawn(self) -> None:
        with self.fake._lock:
            self.fake.os_boot_ms += self.fake.up_ms()
            self.fake._t0 = time.monotonic()

    def power_cycle(self) -> None:
        if self.power_on_is_noop:
            return
        self.down = True
        self.fake.stop()

        def back() -> None:
            if self.netboot:
                self._reboots_seen = len(self.fake.reboots)
                self._netboot_reset(power_on=True)
            else:
                self.fake._simulate_restart()
                self._power_reset_counters()
                self._reboots_seen = len(self.fake.reboots)
                self.fake.hung = False
                self.fake.start()
            self.down = False
        threading.Timer(self.cycle_down_s, back).start()

    # -- debug servers --------------------------------------------------------- #

    def _xvc(self, c: socket.socket) -> None:
        if c.recv(64).startswith(b"getinfo:"):
            c.sendall(b"xvcServer_v1.0:2048\n")

    def _jtag(self, c: socket.socket) -> None:
        if c.recv(1) == b"R":
            c.sendall(b"1")
            c.recv(1)

    def inject(self, kind: str) -> None:
        f = self.fake
        if kind == "hang":
            f.hung = True
        elif kind == "kill":
            f.stop()
        elif kind == "reset":
            f._simulate_restart()
        elif kind == "respawn":
            self.respawn()
        elif kind == "reflash":
            f.static_id = 0x0BADF00D
        elif kind == "swapfail":
            f.rm_id_readback_override = 0x01000077
        elif kind == "sshfail":
            self.ssh_broken = True
        elif kind == "rescue":
            f.stop()
            self.rescue = True
            f.mode = "rescue"
            f.start()
        elif kind == "noreset":
            self.power_on_is_noop = True
        elif kind == "leak":
            self.leak_kb = 256
        elif kind == "oops":
            self.oops = 1
        elif kind == "unlock":
            f.clk_locked = False
        elif kind == "pushfail":
            self.push_fail = True
        elif kind == "nocard":
            self.card_dead = False
        elif kind == "ctlreset":
            self.ctl_resets = 2
        elif kind == "ctlreset_all":
            self.ctl_resets = -1
        elif kind == "crash":
            self.crash_next = True
        elif kind == "dbgbusy":
            self.dbg_busy = 2
        elif kind == "nomacrx":              # an RM that sends nothing: GENCHK rx frozen
            orig = f._op_macgen

            def frozen(request: Dict[str, Any], _orig: Any = orig) -> Dict[str, Any]:
                rx0 = f.genchk_rx
                rep = _orig(request)
                f.genchk_rx = rx0
                rep["rx"] = rx0
                return rep
            f._op_macgen = frozen
        else:
            raise ValueError("unknown inject %r" % kind)

    def close(self) -> None:
        for s in (self.xvc, self.jtag, self.xvc_lo, self.jtag_lo):
            s.close()
        try:
            self.fake.stop()
        except Exception:                          # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

INJECTS = ("hang", "kill", "reset", "respawn", "reflash", "swapfail", "sshfail", "rescue",
           "noreset", "leak", "oops", "unlock", "pushfail", "nocard", "ctlreset",
           "ctlreset_all", "crash", "dbgbusy", "nomacrx")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="soak_linux.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    D = parse_duration

    def common(sp: argparse.ArgumentParser) -> None:
        g = sp.add_argument_group("board")
        g.add_argument("--host", default="192.168.10.101")
        g.add_argument("--expect-static-id", type=hexint, default=None, dest="expect_static_id",
                       help="the fabric static_id ping.shell_id must report (required)")
        g.add_argument("--expect-ver32", type=hexint, default=None, dest="expect_ver32",
                       help="USR_ACCESS the W1 subset's version check must read")
        g.add_argument("--expect-slot", default="A", choices=("A", "B", "any"), dest="expect_slot")
        g.add_argument("--overlay", action="append", default=[], dest="overlays",
                       help="an overlay directory; repeat; swapped to in this order, cycling")
        g.add_argument("--ssh-target", default=None, dest="ssh_target",
                       help="ssh destination (default root@<host>)")
        g.add_argument("--ssh-opt", action="append", default=[], dest="ssh_opts",
                       help="one extra ssh argv word, repeatable (e.g. --ssh-opt=-i "
                            "--ssh-opt=$HOME/.ssh/mps3_soak_ed25519)")
        g.add_argument("--no-stage0", action="store_false", dest="stage0",
                       help="never read stage0's block over ssh (devmem)")
        g.add_argument("--heartbeat-cmd", default=None, dest="heartbeat_cmd",
                       help="extends the lease; run every --heartbeat-every; failure = INCONCLUSIVE")
        g.add_argument("--transport", default="auto", choices=("auto", "tftp", "tcp"))
        g.add_argument("--debug-via", default="auto", choices=("auto", "ssh", "direct"),
                       dest="debug_via",
                       help="XVC/JTAG sessions and --jtag-cmd: auto (default) = through an ssh "
                            "tunnel to the board's 127.0.0.1 whenever identify says claimed (a "
                            "claimed harnessd serves 2542/6921 to local peers only: xvc_lock), "
                            "else direct")
        for name, dflt in (("control", 6900), ("push", 6910), ("tftp", 69), ("identify", 6899),
                           ("xvc", 2542), ("jtag", 6921)):
            g.add_argument("--%s-port" % name, type=int, default=dflt, dest="%s_port" % name)
        n = sp.add_argument_group("netboot (the user microSD is dead: every reset lands in "
                                  "stage0 rescue)")
        n.add_argument("--netboot", default=None, metavar="IMAGE",
                       help="after every reset push IMAGE (the S0LB linux_slot.img) to stage0's "
                            "rescue TFTP, claim the RAM boot, unbind mmc_spi, then carry on; "
                            "drops the slot-A fallback drill (see NETBOOT above)")
        n.add_argument("--claim-key", default=None, dest="claim_key", metavar="PUBFILE",
                       help="netboot: the authorized_keys file every RAM boot is claimed with "
                            "(`pyverify claim --key`); must include the key --ssh-opt=-i uses")
        n.add_argument("--stage0-push", default=None, dest="stage0_push", metavar="PATH",
                       help="netboot: STAGE0's stage0_push.py (stage0_pack.py and "
                            "stage0_status.py beside it; default: beside this script, else the "
                            "repo's src/linux_soc/hw/fw_stage0/)")
        n.add_argument("--netboot-timeout", type=float, default=300.0, dest="netboot_timeout",
                       help="netboot: s one rescue push may take (29 MB: ~55 s)")
        t = sp.add_argument_group("timing")
        t.add_argument("--retries", type=int, default=3)
        t.add_argument("--retry-gap", type=D, default=10.0, dest="retry_gap")
        t.add_argument("--probe-timeout", type=float, default=8.0, dest="probe_timeout")
        t.add_argument("--reset-deadline", type=float, default=180.0, dest="reset_deadline",
                       help="s from a reset request to 6900 (MCC reload + 24 MB card load + boot)")
        t.add_argument("--swap-timeout", type=float, default=300.0, dest="swap_timeout")
        t.add_argument("--reset-tolerance-ms", type=float, default=5000.0,
                       dest="reset_tolerance_ms",
                       help="how far an uptime may fall behind wall time before it counts as a "
                            "restart")
        t.add_argument("--heartbeat-every", type=D, default=1800.0, dest="heartbeat_every")
        t.add_argument("--time-scale", type=float, default=None, dest="time_scale",
                       help="multiply every interval and the duration (dry run: 1/200, so 24 h runs in ~7 min)")
        r = sp.add_argument_group("power-on")
        r.add_argument("--reset", default="mcc",
                       help="mcc (DEFAULT: the paced REBOOT on --mcc-tty, one reader, CR "
                            "first, 100 ms/char), manual (prompt), none (skip), or a shell "
                            "command. fpgahub's `target reset --method mcc` is refused as a "
                            "command: it sends a burst the MCC drops (ILA handoff defect 6)")
        r.add_argument("--mcc-tty", default=_board_tty(0), dest="mcc_tty")
        r.add_argument("--mcc-pace", type=float, default=0.1, dest="mcc_pace")
        r.add_argument("--mcc-settle", type=float, default=1.0, dest="mcc_settle")
        r.add_argument("--mcc-capture", type=float, default=150.0, dest="mcc_capture")
        o = sp.add_argument_group("output")
        o.add_argument("--out", default=None, help="evidence file (JSON lines, appended)")
        o.add_argument("--keep-going", action="store_true", dest="keep_going")
        o.add_argument("--quiet", action="store_true")
        o.add_argument("--dry-run", action="store_true", dest="dry_run")
        o.add_argument("--dry-run-inject", action="append", default=[], dest="injects",
                       metavar="KIND@SECONDS", help="dry run only: " + " ".join(INJECTS))

    def load(sp: argparse.ArgumentParser, **d: Any) -> None:
        g = sp.add_argument_group("load (0 = off)")
        for name, dflt, hlp in (("duration", d["duration"], "total run time"),
                                ("swap-every", d["swap"], "one swap, the next RM in turn"),
                                ("ssh-every", d["ssh"], "one SSH login"),
                                ("poll-every", d["poll"], "ping + stats"),
                                ("sample-every", d["sample"], "resource sample over ssh"),
                                ("churn-every", d["churn"], "6900/6910 connection churn"),
                                ("debug-every", d["debug"], "an XVC + a JTAG session")):
            g.add_argument("--" + name, type=D, default=D(dflt), dest=name.replace("-", "_"),
                           help=hlp + " (default %s)" % dflt)
        g.add_argument("--churn-n", type=int, default=3, dest="churn_n")
        g.add_argument("--stage0-every-logins", type=int, default=d["s0"], dest="stage0_every_logins",
                       help="read the stage0 block on every Nth login")
        g.add_argument("--ssh-p99-max", type=float, default=10.0, dest="ssh_p99_max")

    a = sub.add_parser("accel", help="the 24 h accelerated soak (the cutover gate)")
    common(a)
    load(a, duration="24h", swap="3m", ssh="1m", poll="1m", sample="5m", churn="1m",
         debug="1h", s0=15)
    g = a.add_argument_group("phases and drills")
    g.add_argument("--phase-a", type=D, default=D("6h"), dest="phase_a")
    g.add_argument("--phase-u", type=D, default=D("12h"), dest="phase_u")
    g.add_argument("--boots-a", type=int, default=10, dest="boots_a",
                   help="power-on boots spread over phase A (the 10/10)")
    g.add_argument("--mcc-every-b", type=D, default=D("2h"), dest="mcc_every_b")
    g.add_argument("--mps3-reboot-every", type=D, default=D("2h"), dest="mps3_reboot_every")
    g.add_argument("--wdog-every", type=D, default=D("3h"), dest="wdog_every")
    g.add_argument("--fallback-at", type=D, default=D("2.5h"), dest="fallback_at")
    g.add_argument("--no-fallback", action="store_const", const=None, dest="fallback_at")
    g.add_argument("--slot-a-dev", default="/dev/mmcblk0p1", dest="slot_a_dev")
    g.add_argument("--fallback-offset", type=int, default=8 << 20, dest="fallback_offset",
                   help="byte in slot A to flip (inside the image payload)")
    g.add_argument("--jtag-cmd", default=None, dest="jtag_cmd",
                   help="W1 proof 8': a command that halts the nanosoc M0 over 6921 and prints "
                        "'halted'. It must reach 6921 as {jtag_host}:{jtag_port} (or "
                        "$MPS3_JTAG_HOST/$MPS3_JTAG_PORT): on a claimed board that is the ssh "
                        "tunnel's end. E.g. openocd -c 'set RBB_HOST {jtag_host}' -c 'set "
                        "RBB_PORT {jtag_port}' -f nanosoc_mps3_jtag.cfg -c init -c halt -c "
                        "shutdown")
    g.add_argument("--mac-gap", type=float, default=3.0, dest="mac_gap",
                   help="s between the MAC test's macgen rounds (eth_ss beacons ~1 frame/s)")
    g.add_argument("--dutrx-prefix", default="0180c2000001", dest="dutrx_prefix",
                   help="W1 proof 6: eth_ss's PAUSE beacon starts with this")
    g.add_argument("--clk-preset", action="append", default=None, dest="clk_presets",
                   help="set_clk drill presets (default: pyverify's DEFAULT_CLK_PRESETS, "
                        "firmware/clkrst/clkrst.c: 25mhz 50mhz 100mhz)")

    t = sub.add_parser("tail", help="the plan's 72 h soak (after cutover)")
    common(t)
    load(t, duration="72h", swap="1h", ssh="1h", poll="5m", sample="5m", churn="0",
         debug="0", s0=1)

    b = sub.add_parser("boots", help="N power-on boots to SSH")
    common(b)
    b.add_argument("--n", type=int, default=10)
    b.add_argument("--gap", type=D, default=30.0)

    m = sub.add_parser("mcc-reboot", help="ONE paced MCC REBOOT, boot log captured")
    m.add_argument("--tty", default=_board_tty(0))
    m.add_argument("--log", required=True, help="capture file (appended)")
    m.add_argument("--pace", type=float, default=0.1, help="s between characters")
    m.add_argument("--settle", type=float, default=1.0, help="s after the bare CR")
    m.add_argument("--capture", type=float, default=150.0, help="s to capture")
    m.add_argument("--no-reader-check", action="store_false", dest="check_readers")
    m.add_argument("--no-prompt-check", action="store_false", dest="check_prompt",
                   help="send REBOOT even when a bare CR brings back no Cmd> (default: "
                        "refuse, rc 3, nothing sent)")
    return p


#: `--reset <command>` that is fpgahub's own MCC REBOOT: a burst the MCC drops
#: (ILA handoff 2026-09-24 §4.6). The paced write is `--reset mcc`.
FPGAHUB_MCC_RESET = re.compile(r"\bfpgahub\b.*\breset\b.*--method[ =]+['\"]?mcc\b")


def reset_refusal(reset: str) -> Optional[str]:
    """Why ``--reset`` is refused, or None. The soak's MCC drill is the paced
    tty_00 write (``mcc``, the default); a shell command that asks fpgahub for
    its REBOOT would measure the burst the MCC drops, not a power-on boot."""
    if reset in ("mcc", "manual", "none", "sim"):
        return None
    if FPGAHUB_MCC_RESET.search(reset):
        return ("--reset %r is fpgahub's MCC REBOOT, which sends a burst the MCC drops "
                "(ILA handoff defect 6). Use --reset mcc: the paced REBOOT on --mcc-tty "
                "with the single-reader check" % reset)
    return None


def _ssh_identity(opts: Sequence[str]) -> Optional[str]:
    """The file an ``-i`` among the --ssh-opt words names (``-i F`` or ``-iF``)."""
    for i, o in enumerate(opts):
        if o == "-i" and i + 1 < len(opts):
            return opts[i + 1]
        if o.startswith("-i") and len(o) > 2:
            return o[2:]
    return None


def prepare_netboot(args: argparse.Namespace, sim: "Optional[Sim]") -> Optional[str]:
    """Check everything a netboot needs BEFORE the run, so a 24 h soak cannot die
    at its first reset over a typo. Returns why not, or None (args completed:
    stage0_push, netboot_push_image, claim_key made absolute)."""
    push = Path(args.stage0_push).expanduser() if args.stage0_push else default_stage0_push()
    if push is None or not push.is_file():
        return "--netboot: no stage0_push.py at %s (give --stage0-push)" % (push or "the defaults")
    for dep in ("stage0_pack.py", "stage0_status.py"):
        if not (push.parent / dep).is_file():
            return "--netboot: %s needs %s beside it" % (push, dep)
    args.stage0_push = str(push.resolve())
    img = Path(args.netboot).expanduser()
    if img.is_file():
        problems = s0lb_problems(push.parent, img.read_bytes())
        if problems:
            return "--netboot %s is not a bootable stage0 image: %s" % (img, "; ".join(problems))
        args.netboot = str(img.resolve())
    elif sim is None:
        return "--netboot %s: no such file" % img
    # the dry run always pushes the Sim's small synthetic image (IMAGE is only checked)
    args.netboot_push_image = str(sim.image) if sim else args.netboot
    key = args.claim_key or (str(sim.claim_key) if sim else None)
    if not key:
        return ("--netboot needs --claim-key PUBFILE: every RAM boot comes up unclaimed and "
                "must be claimed (pyverify claim) before ssh can log in")
    try:
        text = Path(key).expanduser().read_text()
    except OSError as exc:
        return "--claim-key: %s" % exc
    fps = key_fingerprints(text)
    if not fps or len(text.encode()) > 16 * 1024:
        return "--claim-key %s is not an OpenSSH public key file (<= 16 KiB)" % key
    args.claim_key = str(Path(key).expanduser().resolve())
    ident = _ssh_identity(args.ssh_opts)
    if ident and Path(ident + ".pub").expanduser().is_file():
        own = key_fingerprints(Path(ident + ".pub").expanduser().read_text())
        if own and own[0] not in fps:
            return ("--claim-key %s does not include %s.pub: every claim would lock the soak's "
                    "own ssh key out" % (key, ident))
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    why = reset_refusal(getattr(args, "reset", "mcc"))
    if why:
        print("soak_linux: %s" % why, file=sys.stderr)
        return EXIT_USAGE
    if args.mode == "mcc-reboot":
        res = mcc_reboot(args.tty, Path(args.log), pace=args.pace, settle=args.settle,
                         capture_s=args.capture, check_readers=args.check_readers,
                         check_prompt=args.check_prompt)
        print(json.dumps(res))
        return res["rc"]

    load_pyverify()
    if getattr(args, "clk_presets", "absent") is None:
        from pyverify.client import DEFAULT_CLK_PRESETS
        args.clk_presets = list(DEFAULT_CLK_PRESETS)
    sim: Optional[Sim] = None
    if args.dry_run:
        import tempfile
        work = Path(tempfile.mkdtemp(prefix="soak_linux_dry_"))
        sim = Sim(work, netboot=bool(args.netboot))
        f = sim.fake
        args.host = f.host
        args.control_port, args.tftp_port, args.push_port = f.control_port, f.tftp_port, f.raw_tcp_port
        args.identify_port, args.xvc_port, args.jtag_port = f.identify_port, sim.xvc.port, sim.jtag.port
        if args.expect_static_id is None:
            args.expect_static_id = SIM_STATIC_ID
        if getattr(args, "expect_ver32", None) is None:
            args.expect_ver32 = SIM_VER32
        args.overlays = args.overlays or [str(o) for o in sim.overlays]
        # --transport is left as given (default auto), so every dry-run swap runs
        # deploy's version.features auto-detect exactly as a board run does (a
        # safety function defined but never called is ILA-mint finding #13's
        # failure mode). FakeShell's linux profile, like harnessd, does not
        # advertise `windowed` (HARNESSD_CONTRACT §9.5; HOST_CONTRACT v1.1) and
        # reports impl "linux", so auto picks plain 6910 (it parks a partial
        # that arrives inside the busy-ICAP window; TFTP would be rejected --
        # SIM_CLEARING_BYTES); each swap event records the choice.
        args.time_scale = args.time_scale if args.time_scale is not None else 1 / 200.0
        args.probe_timeout = min(args.probe_timeout, 1.0)
        args.reset_deadline = min(args.reset_deadline, 20.0)
        args.swap_timeout = min(args.swap_timeout, 60.0)
        args.reset_tolerance_ms = min(args.reset_tolerance_ms, 150.0)
        if args.reset == "mcc":
            args.reset = "sim"
        args.out = args.out or str(work / ("soak_%s_dry.jsonl" % args.mode))
        for spec in args.injects:
            kind, _, at = spec.partition("@")
            if kind not in INJECTS:
                print("soak_linux: unknown inject %r (%s)" % (kind, " ".join(INJECTS)), file=sys.stderr)
                sim.close()
                return EXIT_USAGE
    else:
        if args.injects:
            print("soak_linux: --dry-run-inject needs --dry-run", file=sys.stderr)
            return EXIT_USAGE
        if args.expect_static_id is None:
            print("soak_linux: --expect-static-id is required", file=sys.stderr)
            return EXIT_USAGE
        args.time_scale = 1.0 if args.time_scale is None else args.time_scale

    def usage(msg: str) -> int:
        print("soak_linux: %s" % msg, file=sys.stderr)
        if sim:
            sim.close()
        return EXIT_USAGE
    if args.mode in ("accel", "tail") and not args.overlays:
        return usage("give at least one --overlay")
    overlays = [Path(o).expanduser().resolve() for o in args.overlays]
    for o in overlays:
        if not (o / "manifest.json").is_file():
            return usage("%s has no manifest.json" % o)
    why = prepare_netboot(args, sim) if args.netboot else None
    if why:
        return usage(why)
    jcmd = getattr(args, "jtag_cmd", None)
    if jcmd and args.debug_via != "direct" and not re.search(r"\{jtag_port\}|MPS3_JTAG_PORT", jcmd):
        return usage("--jtag-cmd must reach 6921 as {jtag_host}:{jtag_port} (or $MPS3_JTAG_HOST/"
                     "$MPS3_JTAG_PORT): a claimed board serves it to its own 127.0.0.1 only, "
                     "so on one it is the ssh tunnel's end (or pass --debug-via direct)")
    args.ssh_dest = args.ssh_target or "root@%s" % args.host
    args.ssh_opts_all = list(args.ssh_opts) + (list(NETBOOT_SSH_OPTS) if args.netboot else [])
    if sim:                                   # the injects start with the run, not before
        for spec in args.injects:
            kind, _, at = spec.partition("@")
            threading.Timer(float(at or 0), sim.inject, args=(kind,)).start()
    out = Path(args.out or "soak_%s_%s.jsonl" % (args.mode, time.strftime("%Y%m%d_%H%M%S")))
    ev = Evidence(out, echo=not args.quiet)
    board = Board(args.host, control_port=args.control_port, push_port=args.push_port,
                  identify_port=args.identify_port, xvc_port=args.xvc_port,
                  jtag_port=args.jtag_port, timeout=args.probe_timeout,
                  ssh_base=ssh_base_argv(args.ssh_dest, args.ssh_opts_all),
                  run=sim.run if sim else None)
    r = Runner(args, board, ev, overlays, sim)

    def _sig(signum, frame):                      # noqa: ARG001
        r._stop.set()
    old = ({s: signal.signal(s, _sig) for s in (signal.SIGTERM, signal.SIGINT)}
           if threading.current_thread() is threading.main_thread() else {})
    ev.write("start", mode=args.mode, dry_run=bool(args.dry_run), pyverify=str(PV_DIR),
             config={k: v for k, v in vars(args).items() if k != "injects"}, injects=args.injects)
    try:
        if args.mode == "boots":
            verdict, summary = run_boots(r)
        else:
            verdict, summary = run_schedule(r, args.mode)
    except Exception as exc:                      # noqa: BLE001 -- e.g. in judge()
        r.fail(crash_stop(exc))
        verdict, summary = "FAIL", {"fails": len(r.fails), "fail_kinds": [f["kind"] for f in r.fails],
                                    "counts": dict(r.counts),
                                    "criterion": "the soak itself crashed (FAIL crash)"}
    finally:
        for s_, h in old.items():
            signal.signal(s_, h)
    ev.write("end", verdict=verdict, **summary)
    ev.close()
    try:
        write_summary_md(out, args.mode, verdict, summary, r)
    except Exception as exc:                      # noqa: BLE001 -- the verdict still stands
        print("soak_linux: could not write the summary: %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
    if sim:
        sim.close()
    print("%s %s -- %s" % (args.mode.upper(), verdict, ", ".join(
        "%s=%s" % (k, v) for k, v in summary.items() if k not in ("criterion",) and v not in ([], None))))
    for item in summary.get("skipped", []):
        print("skipped: %s" % item)
    for item in summary.get("deviations", []):
        print("deviation: %s" % item)
    print("evidence: %s (+ %s)" % (out, out.with_suffix(".summary.md")))
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL}.get(verdict, EXIT_INCONCLUSIVE)


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> Tuple[float, float]:
    """Exact binomial interval (stdlib; bisection on the binomial CDF)."""
    from math import comb
    if n <= 0:
        return 0.0, 1.0
    a = (1 - conf) / 2

    def cdf(x: int, p: float) -> float:
        return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(x + 1))

    def solve(f: Callable[[float], float]) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if f(mid) > 0:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2
    lower = 0.0 if k == 0 else solve(lambda p: a - (1 - cdf(k - 1, p)))
    upper = 1.0 if k == n else solve(lambda p: cdf(k, p) - a)
    return lower, upper


def _md(v: Any) -> str:
    return json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)


def write_summary_md(out: Path, mode: str, verdict: str, s: Dict[str, Any], r: Runner) -> Path:
    """``<stem>.summary.md``: the tables the write-up cites (§7.2-§7.4), with fixed
    section names: Verdict, Counts, Sweep, W1 subset, Slot-A fallback, Warm resets,
    Power-on boots, Netboot, Resources (phase U), Swap times, FAIL events, Criterion."""
    L = ["# soak_linux %s: %s" % (mode, verdict), "",
         "Raw events: `%s` (JSON lines). MCC boot logs: `%s.mccNN.log`." % (
             out.name, out.with_suffix("").name), "",
         "## Verdict", "", "| item | value |", "|---|---|",
         "| verdict | **%s** |" % verdict,
         "| duration run / planned (s) | %s / %s |" % (s.get("ran_s"), s.get("duration_s")),
         "| FAIL events | %s %s |" % (s.get("fails", 0), _md(s.get("fail_kinds", []))),
         "| problems | %s |" % _md(s.get("problems", [])),
         "| undecided | %s |" % _md(s.get("undecided", []))]
    if s.get("netboot"):
        L += ["| skipped | %s |" % _md(s.get("skipped", [])),
              "| deviations | %s |" % _md(s.get("deviations", []))]
    L += ["", "## Counts", "", "| item | value |", "|---|---|"]
    for k in ("swaps", "ssh", "polls", "samples", "churn", "debug", "debug_tunnelled",
              "power_on_boots", "mps3_reboots", "wdog_trips", "reboot_verbs", "wdog_resets",
              "fallback_drills", "netboot_pushes", "claims", "card_unbinds", "recoveries",
              "unconfirmed_expected", "heartbeats", "busy_retries", "ssh_lat_p99_s",
              "t_6900_median_s", "t_6900_max_s", "t_ssh_max_s", "power_on_t_6900_median_s",
              "netboot_t_rescue_max_s", "netboot_push_max_s", "netboot_t_ssh_max_s"):
        if k in s:
            L.append("| %s | %s |" % (k, s[k]))
    by = {d["drill"]: d for d in r.drills}
    if "sweep" in by:
        L += ["", "## Sweep", "", "| RM | rm_id | verified | swap s |", "|---|---|---|---|"]
        L += ["| %s | %s | %s | %s |" % (x["rm"], x["rm_id"], x["verified"], x["dur_s"])
              for x in by["sweep"]["rms"]]
    if "regress" in by:
        g = by["regress"]
        L += ["", "## W1 subset", "", "| subtest | result |", "|---|---|"]
        for k in ("version", "mactest", "dutrx", "jtag", "reboot"):
            if k in g:
                L.append("| %s | %s |" % (k, _md(g[k])))
    clk = [d for d in r.drills if d["drill"] == "set_clk"]
    if clk:
        L += ["", "## set_clk", "", "| run | preset | ok | locked | dut_mhz | mmcm |",
              "|---|---|---|---|---|---|"]
        for i, d in enumerate(clk, 1):
            L += ["| %d | %s | %s | %s | %s | %s |" % (i, x["preset"], x["ok"], x["locked"],
                                                       x["dut_mhz"], x["mmcm"]) for x in d["rows"]]
    if "fallback" in by:
        f = by["fallback"]
        L += ["", "## Slot-A fallback", "",
              "slot A payload byte flipped -> warm reset booted **%s**; byte restored -> warm "
              "reset booted **%s**." % (f["fell_back_to"], f["restored_to"])]
    if r.reset_rows:
        L += ["", "## Warm resets", "",
              "| what | to 6900 s | to ssh s | boot_count | booted_from | WRS |",
              "|---|---|---|---|---|---|"]
        L += ["| %s | %s | %s | %s | %s | %s |" % (x["what"], x["t_6900_s"], x["t_ssh_s"],
                                                   x["boot_count"], x["booted_from"], x.get("wrs"))
              for x in r.reset_rows]
    if r.boot_rows:
        k, n = len(r.boot_rows), len(r.boot_rows) + sum(
            1 for f in r.fails if f["detail"].startswith("power-on"))
        lo, hi = clopper_pearson(k, n)
        L += ["", "## Power-on boots", "",
              "%d/%d unattended power-on boots to SSH; 95%% Clopper-Pearson [%.1f%%, %.1f%%]."
              % (k, n, 100 * lo, 100 * hi), "",
              "| n | to 6900 s | to ssh s | boot_count | booted_from | confirmed |",
              "|---|---|---|---|---|---|"]
        L += ["| %s | %s | %s | %s | %s | %s |" % (b["n"], b["t_6900_s"], b["t_ssh_s"],
                                                   b["boot_count"], b["booted_from"], b["confirmed"])
              for b in r.boot_rows]
    if r.netboot_rows:
        L += ["", "## Netboot", "",
              "Each rescue bring-up: seconds from the reset request (the push itself: its "
              "duration). The mmc_spi unbind: " + UNBIND_WHY, "",
              "| what | to rescue s | push s | to 6900 s | claimed s | to ssh s | claim | "
              "boot-health | unbind |", "|---|---|---|---|---|---|---|---|---|"]
        L += ["| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            x["what"], x.get("t_rescue_s"), x.get("push_s"), x.get("t_6900_s"),
            x.get("t_claim_s"), x.get("t_ssh_s"), x.get("claim"), x.get("health"),
            x.get("unbind")) for x in r.netboot_rows]
    if "leak_growth_over_window" in s:
        L += ["", "## Resources (phase U)", "",
              "Least-squares growth over the continuous-uptime window, KiB (fds/files: count).",
              "", "| metric | growth | limit (negative = largest allowed decline) |", "|---|---|---|"]
        for key, g in s["leak_growth_over_window"].items():
            lim = LEAK_LIMITS_KB.get(key, -LEAK_DECLINE_LIMITS_KB.get(key, 0))
            L.append("| %s | %s | %s |" % (key, g, lim))
    L += ["", "## Swap times", "",
          "| item | value |", "|---|---|",
          "| p99, first third of the window (s) | %s |" % s.get("swap_p99_first_third_s"),
          "| p99, last third of the window (s) | %s |" % s.get("swap_p99_last_third_s"),
          "| note | %s |" % s.get("swap_p99_note", "")]
    if r.fails:
        L += ["", "## FAIL events", ""] + ["- `%s`: %s" % (f["kind"], f["detail"]) for f in r.fails]
    L += ["", "## Criterion", "", s.get("criterion", "")]
    path = out.with_suffix(".summary.md")
    path.write_text("\n".join(L) + "\n")
    return path


if __name__ == "__main__":
    sys.exit(main())
