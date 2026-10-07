"""Tests for ReattachPlan.apply() — spec §6.3's re-attach, now executable.

Defaults under test: the console step reopens real TCP connections (here
against an in-test listener on an ephemeral localhost port); the
tool-bound steps (ltx / swd / vphy) return their descriptive hint unless
the caller injects a real executor.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from pyverify.swap import ReattachPlan


def _plan(**overrides) -> ReattachPlan:
    # What SwapOrchestrator.deploy() now puts here: the manifest's name RESOLVED
    # against the overlay dir (the manifest itself says only "nanosoc.ltx").
    defaults = dict(
        ltx_path="/stage/overlay/nanosoc/nanosoc.ltx",
        swd_reconnect_hint="re-run SWD line reset + DP connect",
        console_reopen_ports=(6930, 6931, 6932),
        vphy_relink_hint="client.link(event='up') after MAC restart",
        shell_host=None,
    )
    defaults.update(overrides)
    return ReattachPlan(**defaults)


def test_apply_defaults_return_hints_when_no_host_recorded() -> None:
    plan = _plan()
    outcomes = plan.apply()

    assert set(outcomes) == {"ltx", "swd", "console", "vphy"}
    assert "nanosoc.ltx" in outcomes["ltx"]
    assert "refresh_ila_tcl" in outcomes["ltx"]
    assert outcomes["swd"] == plan.swd_reconnect_hint
    # No shell_host -> console step degrades to a hint (the port tuple).
    assert outcomes["console"] == (6930, 6931, 6932)
    assert outcomes["vphy"] == plan.vphy_relink_hint


def test_apply_ltx_default_is_none_when_overlay_has_no_ltx() -> None:
    outcomes = _plan(ltx_path=None).apply()
    assert outcomes["ltx"] is None


def test_apply_reopens_consoles_for_real_when_host_known() -> None:
    """Default console executor with a recorded shell_host: opens a real
    TCP connection per port (against an in-test accept-only listener)."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(5.0)
    port = listener.getsockname()[1]
    accepted: list[socket.socket] = []

    def _accept_two() -> None:
        for _ in range(2):
            conn, _addr = listener.accept()
            accepted.append(conn)

    acceptor = threading.Thread(target=_accept_two, daemon=True)
    acceptor.start()

    plan = _plan(shell_host="127.0.0.1", console_reopen_ports=(port, port))
    try:
        outcomes = plan.apply()
        readers = outcomes["console"]
        assert len(readers) == 2
        for reader in readers:
            assert reader.host == "127.0.0.1"
            assert reader.port == port
            reader.close()
        acceptor.join(timeout=5.0)
        assert len(accepted) == 2
    finally:
        for conn in accepted:
            conn.close()
        listener.close()


def test_apply_injected_executors_override_defaults() -> None:
    plan = _plan(shell_host="127.0.0.1")  # would try real sockets by default
    ran: list[str] = []

    outcomes = plan.apply({
        "console": lambda p: ran.append("console") or "consoles-reopened",
        "swd": lambda p: ran.append("swd") or "dpidr=0x0bc11477",
    })

    assert sorted(ran) == ["console", "swd"]
    assert outcomes["console"] == "consoles-reopened"
    assert outcomes["swd"] == "dpidr=0x0bc11477"
    # Non-overridden steps still ran their defaults (hints).
    assert outcomes["vphy"] == plan.vphy_relink_hint
    assert "nanosoc.ltx" in outcomes["ltx"]


def test_apply_executors_receive_the_plan() -> None:
    plan = _plan()
    seen: list[ReattachPlan] = []
    plan.apply({"ltx": lambda p: seen.append(p)})
    assert seen == [plan]


def test_apply_rejects_unknown_step_names() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="unknown re-attach step"):
        plan.apply({"jtag": lambda p: None})


def test_apply_console_failure_closes_partial_connections() -> None:
    """If reopening console N fails, consoles 0..N-1 opened by the same
    apply() must be closed, not leaked."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(5.0)
    good_port = listener.getsockname()[1]

    # A port with nothing listening: connect must fail fast.
    probe = socket.create_server(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    accepted: list[socket.socket] = []

    def _accept_one() -> None:
        conn, _addr = listener.accept()
        accepted.append(conn)

    acceptor = threading.Thread(target=_accept_one, daemon=True)
    acceptor.start()

    plan = _plan(shell_host="127.0.0.1", console_reopen_ports=(good_port, dead_port))
    try:
        with pytest.raises(OSError):
            plan.apply()
        acceptor.join(timeout=5.0)
        # The first console connected, then had to be closed on the
        # second's failure: its peer socket sees EOF.
        assert len(accepted) == 1
        accepted[0].settimeout(5.0)
        assert accepted[0].recv(1) == b""
    finally:
        for conn in accepted:
            conn.close()
        listener.close()


def test_deploy_resolves_the_manifest_relative_ltx_name(tmp_path) -> None:
    """The manifest's ``ltx`` is a name RELATIVE to overlay/<rm>/ ("nanosoc.ltx",
    exactly what fpga/dfx/gen_manifest.py writes). swap.py used to pass that bare
    name on as the plan's ltx_path (handover F15), which Vivado resolves against
    ITS cwd and cannot open. This goes through the real SwapOrchestrator.deploy()
    with a manifest-relative name, so it fails on the old code."""
    import json
    import zlib

    from pyverify.client import PingResponse, SwapResponse
    from pyverify.overlay import Overlay
    from pyverify.swap import SwapOrchestrator

    d = tmp_path / "overlay" / "nanosoc"
    d.mkdir(parents=True)
    clear, part = b"\x0c" * 32, b"\x0d" * 64
    (d / "nanosoc_clear.bin").write_bytes(clear)
    (d / "nanosoc.bin").write_bytes(part)
    (d / "nanosoc.ltx").write_text("{}")
    (d / "manifest.json").write_text(json.dumps({
        "schema": 1, "static_id": "0x1A2B3C4D", "rm_id": "0x01000001", "rm_name": "nanosoc",
        "clearing": {"file": "nanosoc_clear.bin", "len": 32, "crc32": "0x%08x" % zlib.crc32(clear)},
        "partial": {"file": "nanosoc.bin", "len": 64, "crc32": "0x%08x" % zlib.crc32(part)},
        "ltx": "nanosoc.ltx",
    }))

    class Client:
        host = None
        def ping(self): return PingResponse(ok=True, shell_id="0x1a2b3c4d", rm_id="0x0")
        def swap_begin(self, rm, src="tftp"): pass
        def swap_await(self): return SwapResponse(ok=True, rm_id="0x01000001", verified=True)

    class Pusher:
        def push_pair(self, overlay, *, rm_slot=0): return (None, None)

    plan = SwapOrchestrator(Client(), Pusher()).deploy(Overlay.load(d)).reattach
    assert plan.ltx_path == str(d / "nanosoc.ltx")
    assert Path(plan.ltx_path).is_file(), "the plan must name a file Vivado can open from anywhere"
