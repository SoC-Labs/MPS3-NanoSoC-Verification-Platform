"""``pyverify.hwserver`` -- never let an XVC session reuse a stale local hw_server.

ILA-mint finding #19 (silicon, 2026-09-24): ``connect_hw_server`` with no server
running auto-launches ``hw_server -d -I20 -s TCP:127.0.0.1:3121``, which LINGERS
20 s after its last client. A Vivado session that connects inside that window
reuses it, and after a DFX swap its view of the XVC target is stale:
``open_hw_target`` reports "No devices detected". Every capture after a >20 s gap
passed.

The cure is the one ``scripts/mps3_ila_proofs.sh`` proved on silicon
(``wait_stale_hw_server``): before a session connects, wait for this user's
auto-launched daemon to exit -- ``pgrep -u $(id -u) -f 'hw_server -d
.*TCP:127.0.0.1:3121'``, up to 20 polls 2 s apart -- then let ``connect_hw_server``
start a fresh one. If it is still there after the wait, PROCEED (it may be
another session's) and say so. :class:`pyverify.debug.XvcSession` does this before
it starts Vivado and on every :meth:`~pyverify.debug.XvcSession.reattach`.

The process table is a seam (``proc_table``): ``() -> [(pid, uid, cmdline)]``.
Tests pass a fake; the default reads ``/proc`` (Linux), else ``ps`` (macOS/BSD),
else returns nothing (Windows: no auto-launch daemon to find this way). Stdlib only.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Callable, Iterable, List, Optional, Tuple

__all__ = [
    "AUTO_HW_SERVER_PORT", "STALE_POLLS", "STALE_POLL_S", "stale_pattern",
    "proc_table", "stale_hw_servers", "wait_stale_hw_server", "hw_server_port", "ProcTable",
]

#: The port ``connect_hw_server`` auto-launches its local daemon on.
AUTO_HW_SERVER_PORT = 3121
#: mps3_ila_proofs.sh's budget: 20 polls, 2 s apart (the daemon lingers 20 s).
STALE_POLLS = 20
STALE_POLL_S = 2.0

ProcEntry = Tuple[int, int, str]
ProcTable = Callable[[], Iterable[ProcEntry]]


def stale_pattern(port: int = AUTO_HW_SERVER_PORT) -> str:
    """The pgrep ``-f`` pattern of mps3_ila_proofs.sh, for ``port``."""
    return "hw_server -d .*TCP:127.0.0.1:%d" % port


def _uid() -> Optional[int]:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else None


def proc_table() -> List[ProcEntry]:
    """``[(pid, uid, cmdline)]`` for every visible process (best effort)."""
    out: List[ProcEntry] = []
    if os.path.isdir("/proc"):
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % d, "rb") as f:
                    raw = f.read()
                uid = os.stat("/proc/%s" % d).st_uid
            except OSError:
                continue
            cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if cmd:
                out.append((int(d), uid, cmd))
        return out
    try:
        p = subprocess.run(["ps", "-A", "-o", "pid=,uid=,args="], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in p.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out.append((int(parts[0]), int(parts[1]), parts[2]))
    return out


def stale_hw_servers(port: int = AUTO_HW_SERVER_PORT, *, uid: Optional[int] = None,
                     table: Optional[ProcTable] = None) -> List[int]:
    """The pids of THIS user's auto-launched hw_server daemons on ``port``
    (``pgrep -u <uid> -f <stale_pattern(port)>``)."""
    rx = re.compile(stale_pattern(port) + r"(?!\d)")
    uid = _uid() if uid is None else uid
    return [pid for pid, puid, cmd in (table or proc_table)()
            if (uid is None or puid == uid) and rx.search(cmd)]


def wait_stale_hw_server(port: int = AUTO_HW_SERVER_PORT, *, polls: int = STALE_POLLS,
                         poll_s: float = STALE_POLL_S, uid: Optional[int] = None,
                         table: Optional[ProcTable] = None,
                         sleep: Callable[[float], None] = time.sleep,
                         log: Optional[Callable[[str], None]] = None) -> List[int]:
    """Wait for this user's lingering auto-launched hw_server on ``port`` to exit.

    Returns ``[]`` when none is left (or none was there), else the pids still
    alive after ``polls`` checks -- the caller PROCEEDS either way, exactly as
    mps3_ila_proofs.sh does; ``log`` gets one line when it waited or gave up."""
    for i in range(polls):
        pids = stale_hw_servers(port, uid=uid, table=table)
        if not pids:
            if i and log:
                log("waited %.0f s for a lingering local hw_server (:%d) to exit"
                    % (i * poll_s, port))
            return []
        sleep(poll_s)
    pids = stale_hw_servers(port, uid=uid, table=table)
    if pids and log:
        log("a local hw_server on :%d (pid %s) is still alive after %.0f s; proceeding (it "
            "may be another session's)" % (port, ",".join(map(str, pids)), polls * poll_s))
    return pids


def hw_server_port(url: str) -> Optional[int]:
    """The port of a ``host:port`` hw_server URL (``localhost:3121`` -> 3121)."""
    tail = url.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None
