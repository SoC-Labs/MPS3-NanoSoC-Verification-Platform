"""pyverify.debug.XvcSession + the swap orchestrator's close-before / reopen-after
XVC protocol (handover HANDOVER_RM_ILA_OVER_XVC.md §4.10).

Why the ORDER is the whole point: while a swap is gated the shell firmware
STALLS an XVC ``shift:`` and then runs it against the NEW RM (§2 F10, §8 trap
3). So the orchestrator must close the target before the swap RPC, and only the
re-attach step -- after the shell says DONE -- may reopen it and load the new
RM's ``.ltx``. Every test below asserts on one shared event log across the fake
Vivado, the fake client and the fake pusher, so it is the ORDER that is pinned.

No Vivado here: :class:`FakeVivado` is a ``TclProcess`` that answers the
session's sentinel protocol and records the Tcl it was sent.
"""
from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from typing import Optional

import pytest

from pyverify.client import PingResponse, SwapResponse
from pyverify.debug import (
    LOCAL_HW_SERVER_URL,
    XvcSession,
    XvcSessionError,
    XvcTarget,
)
from pyverify.overlay import Overlay
from pyverify.swap import SwapError, SwapOrchestrator

STATIC_ID = 0x1A2B3C4D


class FakeVivado:
    """Answers ``if {[catch {<body>} r]} {puts "\\n<TAG> ERR .."} else {puts "\\n<TAG> OK .."}``."""

    def __init__(self, argv, log: list[str], *, fail_on: str = "", results=None):
        self.argv = list(argv)
        self.log = log
        self.fail_on = fail_on
        self.results = results or {}
        self.pending: list[str] = []
        self.closed = False
        log.append("vivado:spawn " + " ".join(argv[1:]))

    def send(self, text: str) -> None:
        m = re.match(r"if \{\[catch \{(.*)\} __mps3_r\]\} \{puts \"\\n(@@MPS3XVC\d+) ERR", text, re.S)
        assert m, text
        body, tag = m.group(1), m.group(2)
        for line in body.splitlines():
            self.log.append("tcl:" + line.strip())
        if self.fail_on and self.fail_on in body:
            self.pending.append(f"{tag} ERR [Labtools 27-2269] No devices detected\n")
            return
        res = next((v for k, v in self.results.items() if k in body), "")
        self.pending += ["Vivado% \n", f"{tag} OK {res}\n"]

    def readline(self, timeout: float) -> Optional[str]:
        return self.pending.pop(0) if self.pending else None

    def close(self) -> None:
        self.closed = True
        self.log.append("vivado:exit")


def _session(log, **kw) -> XvcSession:
    results = {"llength [get_hw_ilas": "1", "current_hw_device": "xcku115_0"}  # first match wins
    return XvcSession(
        XvcTarget("localhost"),
        spawn=lambda argv: FakeVivado(argv, log, results=results, **kw),
        timeout_s=2.0,
    )


# --------------------------------------------------------------------------- #
# XvcTarget / XvcSession
# --------------------------------------------------------------------------- #


def test_target_refuses_a_remote_hw_server() -> None:
    with pytest.raises(ValueError, match="LOCAL hw_server only"):
        XvcTarget("localhost", hw_server_url="hub.example:3121")
    assert XvcTarget("localhost").hw_server_url == LOCAL_HW_SERVER_URL == "localhost:3121"


def test_refresh_ila_tcl_is_a_complete_session() -> None:
    tcl = XvcTarget("localhost", port=25420).refresh_ila_tcl("/x/overlay/dbg_demo/dbg_demo.ltx")
    lines = tcl.splitlines()
    order = ["open_hw_manager", "connect_hw_server -url localhost:3121",
             "open_hw_target -xvc_url localhost:25420",
             "set_property PROBES.FILE {/x/overlay/dbg_demo/dbg_demo.ltx} [current_hw_device]",
             "refresh_hw_device [current_hw_device]", "catch {close_hw_target}"]
    idx = [lines.index(o) for o in order]
    assert idx == sorted(idx), lines


def test_session_lifecycle_open_reattach_close() -> None:
    log: list[str] = []
    s = _session(log)
    assert s.open_target("/o/dbg_demo.ltx") == "xcku115_0"
    assert s.target_open and s.ltx_path == "/o/dbg_demo.ltx"
    assert log[0] == "vivado:spawn -mode tcl -nojournal -nolog"
    tcl = [e[4:] for e in log if e.startswith("tcl:")]
    assert tcl[:2] == ["open_hw_manager", "connect_hw_server -url localhost:3121"]
    assert "open_hw_target -xvc_url localhost:2542" in tcl
    assert tcl.index("open_hw_target -xvc_url localhost:2542") < tcl.index(
        "set_property PROBES.FILE {/o/dbg_demo.ltx} [current_hw_device]") < tcl.index(
        "refresh_hw_device [current_hw_device]")
    assert s.close_target() is True and not s.target_open
    assert s.close_target() is False  # idempotent
    out = s.reattach("/o/greybox_none.ltx")
    assert out == {"target": "localhost:2542", "device": "xcku115_0",
                   "ltx": "/o/greybox_none.ltx", "ilas": 1}
    s.close()
    assert log[-1] == "vivado:exit" and not s.started


def test_session_tcl_error_raises_not_hangs() -> None:
    log: list[str] = []
    s = _session(log, fail_on="open_hw_target")
    with pytest.raises(XvcSessionError, match="No devices detected"):
        s.open_target(None)
    assert not s.target_open


def test_session_times_out_when_vivado_goes_quiet() -> None:
    class Mute(FakeVivado):
        def readline(self, timeout):
            return None
    s = XvcSession(XvcTarget("localhost"), spawn=lambda a: Mute(a, []), timeout_s=0.2)
    with pytest.raises(XvcSessionError, match="no answer from Vivado"):
        s.start()


def test_reopening_an_open_target_closes_it_first() -> None:
    log: list[str] = []
    s = _session(log)
    s.open_target("/a.ltx")
    s.open_target("/b.ltx")
    tcl = [e[4:] for e in log if e.startswith("tcl:")]
    opens = [i for i, t in enumerate(tcl) if t.startswith("open_hw_target")]
    assert len(opens) == 2 and "close_hw_target" in tcl[opens[0]:opens[1]]


# --------------------------------------------------------------------------- #
# SwapOrchestrator(xvc=...) -- close before the swap RPC, reopen after DONE
# --------------------------------------------------------------------------- #


def _overlay(root: Path, *, ltx: Optional[str] = "dbg_demo.ltx") -> Overlay:
    d = root / "overlay" / "dbg_demo"
    d.mkdir(parents=True)
    clear, part = b"\x01" * 64, b"\x02" * 128
    (d / "dbg_demo_clear.bin").write_bytes(clear)
    (d / "dbg_demo.bin").write_bytes(part)
    m = {"schema": 1, "static_id": f"0x{STATIC_ID:08X}", "rm_id": "0x01000009", "rm_name": "dbg_demo",
         "clearing": {"file": "dbg_demo_clear.bin", "len": 64, "crc32": f"0x{zlib.crc32(clear):08x}"},
         "partial": {"file": "dbg_demo.bin", "len": 128, "crc32": f"0x{zlib.crc32(part):08x}"}}
    if ltx:
        (d / ltx).write_text("{}")
        m["ltx"] = ltx   # manifest-RELATIVE, exactly as gen_manifest.py writes it
    (d / "manifest.json").write_text(json.dumps(m))
    return Overlay.load(d)


class _Client:
    host = "fake-shell"

    def __init__(self, log, ok=True):
        self.log, self.ok = log, ok

    def ping(self):
        self.log.append("client:ping")
        return PingResponse(ok=True, shell_id=f"0x{STATIC_ID:08x}", rm_id="0x00000000")

    def swap_begin(self, rm, src="tftp"):
        self.log.append(f"client:swap_begin {rm}")

    def swap_await(self):
        self.log.append("client:swap_await")
        return SwapResponse(ok=self.ok, rm_id="0x01000009", verified=self.ok)


class _Pusher:
    def __init__(self, log):
        self.log = log

    def push_pair(self, overlay, *, rm_slot=0):
        self.log.append("pusher:push_pair")
        return (None, None)


def test_deploy_closes_xvc_before_the_swap_rpc_and_apply_reopens_after(tmp_path) -> None:
    log: list[str] = []
    xvc = _session(log)
    xvc.open_target("/old/greybox.ltx")
    ov = _overlay(tmp_path)
    result = SwapOrchestrator(_Client(log), _Pusher(log), xvc=xvc).deploy(ov)

    close_at = log.index("tcl:close_hw_target")
    assert close_at < log.index("client:swap_begin dbg_demo") < log.index("pusher:push_pair") \
        < log.index("client:swap_await")
    assert not xvc.target_open, "no XVC target may be open across a swap"
    reopen_before_apply = [e for e in log[close_at:] if e.startswith("tcl:open_hw_target")]
    assert reopen_before_apply == [], "deploy() must not reopen; that is the re-attach step"

    out = result.reattach.apply({"console": lambda p: None})
    ltx = str(ov.directory / "dbg_demo.ltx")
    assert out["ltx"]["ltx"] == ltx and out["ltx"]["ilas"] == 1
    assert xvc.target_open and xvc.ltx_path == ltx
    after = log[log.index("client:swap_await"):]
    assert any(e.startswith("tcl:open_hw_target") for e in after)
    assert "tcl:set_property PROBES.FILE {%s} [current_hw_device]" % ltx in after


def test_failed_swap_leaves_the_target_closed(tmp_path) -> None:
    log: list[str] = []
    xvc = _session(log)
    xvc.open_target(None)
    with pytest.raises(SwapError):
        SwapOrchestrator(_Client(log, ok=False), _Pusher(log), xvc=xvc).deploy(_overlay(tmp_path))
    assert not xvc.target_open
    assert not any(e.startswith("tcl:open_hw_target") for e in log[log.index("client:swap_begin dbg_demo"):])


def test_new_rm_without_an_ltx_reopens_with_no_probes(tmp_path) -> None:
    log: list[str] = []
    xvc = _session(log)
    result = SwapOrchestrator(_Client(log), _Pusher(log), xvc=xvc).deploy(_overlay(tmp_path, ltx=None))
    assert result.reattach.ltx_path is None
    out = result.reattach.apply({"console": lambda p: None})
    assert out["ltx"]["ltx"] is None and xvc.target_open
    assert not any("PROBES.FILE" in e for e in log)


def test_without_an_xvc_session_deploy_is_unchanged(tmp_path) -> None:
    log: list[str] = []
    result = SwapOrchestrator(_Client(log), _Pusher(log)).deploy(_overlay(tmp_path))
    assert not any(e.startswith(("tcl:", "vivado:")) for e in log)
    hint = result.reattach.apply({"console": lambda p: None})["ltx"]
    assert "XvcSession.reattach" in hint
