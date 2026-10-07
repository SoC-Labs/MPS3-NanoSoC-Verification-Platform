"""pyverify.hwserver + XvcSession: never reuse a lingering hw_server across a swap
(ILA-mint finding #19, silicon 2026-09-24).

``connect_hw_server`` auto-launches ``hw_server -d -I20 -s TCP:127.0.0.1:3121``,
which lingers 20 s after its last client. A session that connects inside that
window reuses it and, after a DFX swap, sees a stale target: "No devices
detected". The cure is mps3_ila_proofs.sh's ``wait_stale_hw_server``: wait for the
daemon to exit (20 x 2 s), then let ``connect_hw_server`` start a fresh one.

Everything runs against a FAKE PROCESS TABLE. :class:`World` models the daemon
(alive while connected, lingering N polls after the last disconnect) and the
swap; its fake Vivado fails ``open_hw_target`` exactly when the daemon serving
it predates the last swap. The negative control disables the fix and shows the
failure is real.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import pytest

from pyverify import hwserver as hw
from pyverify.debug import XvcSession, XvcSessionError, XvcTarget

UID = os.getuid()          # the session filters on THIS user's daemons, like pgrep -u
DAEMON = "/opt/Xilinx/bin/unwrapped/lnx64.o/hw_server -d -I20 -s TCP:127.0.0.1:3121"


# --------------------------------------------------------------------------- #
# the process-table half
# --------------------------------------------------------------------------- #

def test_stale_hw_servers_matches_the_ila_scripts_pgrep_pattern():
    assert hw.stale_pattern() == "hw_server -d .*TCP:127.0.0.1:3121"   # mps3_ila_proofs.sh
    table = lambda: [
        (100, UID, DAEMON),                                            # ours, auto-launched: YES
        (101, UID + 1, DAEMON),                                        # someone else's
        (102, UID, "hw_server -s TCP:localhost:3122"),                 # a private, not -d
        (103, UID, "hw_server -d -I20 -s TCP:127.0.0.1:31210"),        # another port
        (104, UID, "vim hw_server.log"),
    ]
    assert hw.stale_hw_servers(3121, uid=UID, table=table) == [100]
    assert hw.stale_hw_servers(31210, uid=UID, table=table) == [103]


def test_wait_returns_at_once_when_nothing_lingers():
    sleeps = []
    assert hw.wait_stale_hw_server(uid=UID, table=lambda: [], sleep=sleeps.append) == []
    assert sleeps == []


def test_wait_polls_until_the_daemon_exits():
    left = {"n": 3}

    def table():
        if left["n"]:
            left["n"] -= 1
            return [(100, UID, DAEMON)]
        return []

    sleeps, log = [], []
    assert hw.wait_stale_hw_server(uid=UID, table=table, sleep=sleeps.append, log=log.append) == []
    assert sleeps == [2.0, 2.0, 2.0]
    assert log == ["waited 6 s for a lingering local hw_server (:3121) to exit"]


def test_wait_gives_up_after_the_budget_and_proceeds():
    sleeps, log = [], []
    pids = hw.wait_stale_hw_server(uid=UID, table=lambda: [(100, UID, DAEMON)],
                                   sleep=sleeps.append, log=log.append)
    assert pids == [100] and len(sleeps) == hw.STALE_POLLS == 20
    assert "still alive after 40 s; proceeding" in log[0]


def test_the_default_table_reads_this_host():
    me = hw.proc_table()
    assert me and all(isinstance(pid, int) and isinstance(cmd, str) for pid, _, cmd in me)


# --------------------------------------------------------------------------- #
# the XvcSession half, against a modelled daemon + swap
# --------------------------------------------------------------------------- #

class World:
    """One local hw_server daemon + the board's swap. Launch n is generation n."""

    def __init__(self, linger_polls: int = 2):
        self.linger_polls = linger_polls
        self.launches = 0
        self.daemon_gen = 0          # 0 = no daemon; else which launch is alive
        self.connected = False
        self.linger = 0
        self.swap_gen = 0            # the daemon generation that saw the RM before the last swap
        self.log: list[str] = []

    def table(self):                 # the fake process table: a lingering daemon ages per poll
        if self.daemon_gen and not self.connected:
            if self.linger <= 0:
                self.daemon_gen = 0
            else:
                self.linger -= 1
        self.log.append("table:%s" % ("daemon" if self.daemon_gen else "-"))
        return [(900 + self.daemon_gen, UID, DAEMON)] if self.daemon_gen else []

    def swap(self):
        self.swap_gen = self.daemon_gen
        self.log.append("swap")

    def tcl(self, body: str) -> Optional[str]:
        """What the fake Vivado does with the Tcl; returns an error or None."""
        for line in body.splitlines():
            line = line.strip()
            self.log.append("tcl:" + line)
            if line.startswith("connect_hw_server"):
                if not self.daemon_gen:              # auto-launch a fresh daemon
                    self.launches += 1
                    self.daemon_gen = self.launches
                self.connected = True
            elif "disconnect_hw_server" in line:
                self.connected = False
                self.linger = self.linger_polls
            elif line.startswith("open_hw_target"):
                if self.swap_gen and self.daemon_gen == self.swap_gen:
                    return "[Labtools 27-2269] No devices detected on target localhost:3121"
        return None


class WorldVivado:
    def __init__(self, world: World):
        self.world = world
        self.pending: list[str] = []

    def send(self, text: str) -> None:
        m = re.match(r"if \{\[catch \{(.*)\} __mps3_r\]\} \{puts \"\\n(@@MPS3XVC\d+) ERR", text, re.S)
        body, tag = m.group(1), m.group(2)
        err = self.world.tcl(body)
        res = "xcku115_0" if "current_hw_device" == body.strip() else ("1" if "get_hw_ilas" in body else "")
        self.pending.append(f"{tag} ERR {err}\n" if err else f"{tag} OK {res}\n")

    def readline(self, timeout: float) -> Optional[str]:
        return self.pending.pop(0) if self.pending else None

    def close(self) -> None:
        self.world.connected = False
        self.world.linger = self.world.linger_polls


def _session(world: World) -> XvcSession:
    return XvcSession(XvcTarget("localhost"), spawn=lambda argv: WorldVivado(world),
                      timeout_s=2.0, proc_table=world.table, stale_polls=20, stale_poll_s=0.0,
                      sleep=lambda s: None)


def test_a_session_waits_for_a_lingering_daemon_before_vivado_starts():
    world = World()
    world.daemon_gen, world.linger = 1, 2           # left by a session that just ended
    s = _session(world)
    s.start()
    first_tcl = world.log.index("tcl:open_hw_manager")        # Vivado's first command
    assert world.log[:first_tcl] == ["table:daemon", "table:daemon", "table:-"]
    assert world.launches == 1 and world.daemon_gen == 1      # connect launched a FRESH one
    assert s.hw_server_log and "waited" in s.hw_server_log[0]


def test_reattach_after_a_swap_uses_a_fresh_hw_server():
    world = World()
    s = _session(world)
    s.open_target("/o/greybox.ltx")
    s.close_target()                     # the orchestrator's close-before-the-swap
    world.swap()
    out = s.reattach("/o/dbg_demo.ltx")  # would be "No devices detected" on the old daemon
    assert out["device"] == "xcku115_0" and s.target_open
    tail = world.log[world.log.index("swap"):]
    order = [tail.index("tcl:catch {disconnect_hw_server}"),
             tail.index("table:-"),
             tail.index("tcl:connect_hw_server -url localhost:3121"),
             next(i for i, e in enumerate(tail) if e.startswith("tcl:open_hw_target"))]
    assert order == sorted(order), tail
    assert world.daemon_gen != world.swap_gen                 # served by a post-swap daemon


def test_negative_control_reusing_the_daemon_across_a_swap_fails(monkeypatch):
    """Without the fresh hw_server, the modelled silicon failure appears."""
    world = World()
    s = _session(world)
    s.open_target("/o/greybox.ltx")
    s.close_target()
    world.swap()
    monkeypatch.setattr(XvcSession, "_fresh_hw_server", lambda self: None)
    with pytest.raises(XvcSessionError, match="No devices detected"):
        s.reattach("/o/dbg_demo.ltx")


def test_an_injected_vivado_without_a_table_skips_the_guard():
    """Existing callers that inject a Vivado double see no waits (additive)."""
    world = World()
    s = XvcSession(XvcTarget("localhost"), spawn=lambda argv: WorldVivado(world), timeout_s=2.0)
    s.start()
    assert not any(e.startswith("table:") for e in world.log) and s.hw_server_log == []
