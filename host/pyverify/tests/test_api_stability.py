"""The pyverify surface other repos depend on, pinned: it may GROW, never break.

The Harness Manager (socharness, a separate repo) imports pyverify and its
``FakeShell`` directly, and its whole suite is written against the bare-metal
shell. The Linux harness work (docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md
§10) is ADDITIVE ONLY on this surface. This file imports and exercises EXACTLY
the names socharness's lead listed on 2026-09-23 -- a renamed class, a dropped
keyword, a removed attribute or a changed default behaviour fails HERE, in this
repo's own gate, instead of in someone else's CI a week later.

Everything not named here may change freely. Adding a name to this file is a
promise; removing one needs the socharness lead's agreement
(docs/planning/linux_lanes/HOST_CONTRACT.md §2).
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import socket
import threading

import pytest

import pyverify.client as client_mod
from pyverify.client import (
    CONTROL_PORT,
    DEFAULT_CLK_PRESETS,
    MACGEN_INJECTS,
    DiagResponse,
    PingResponse,
    ShellClient,
    ShellProtocolError,
    VersionResponse,
)
from pyverify.console import SWO_PORT, UART0_PORT, UART1_PORT, ConsoleReader
from pyverify.overlay import (
    Overlay,
    OverlayManifest,
    OverlayManifestError,
    OverlayValidationError,
)
from pyverify.pusher import (
    DEFAULT_ACK_WINDOW,
    HEADER_SIZE,
    TFTP_PORT,
    BitstreamKind,
    BitstreamPusher,
    PushError,
    PushResult,
)
from pyverify.rm_id import design_id, format_rm_id, parse_rm_id
from pyverify.swap import SwapError, SwapOrchestrator
from pyverify.testing.fakeshell import FakeShell


# --------------------------------------------------------------------------- #
# Constants: names AND values (a changed port number is a break too)
# --------------------------------------------------------------------------- #

def test_constants_keep_their_values():
    assert CONTROL_PORT == 6900
    assert (UART0_PORT, UART1_PORT, SWO_PORT) == (6930, 6931, 6932)
    assert TFTP_PORT == 69
    assert HEADER_SIZE == 24
    assert isinstance(DEFAULT_ACK_WINDOW, int) and DEFAULT_ACK_WINDOW > 0
    assert "25mhz" in DEFAULT_CLK_PRESETS
    assert MACGEN_INJECTS[0] == "none" and "bad_fcs" in MACGEN_INJECTS


def test_exception_types_keep_their_bases():
    assert issubclass(ShellProtocolError, Exception)
    assert issubclass(OverlayManifestError, Exception)
    assert issubclass(OverlayValidationError, Exception)
    assert issubclass(PushError, Exception)
    assert issubclass(SwapError, Exception)


# --------------------------------------------------------------------------- #
# ShellClient against the (default, bare-metal) FakeShell over real sockets
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake():
    f = FakeShell.ephemeral(static_id=0xA1B2C3D4, boot_rm_id=0, reset_targets=("dut",),
                            harness_version="1.2.3", harness_sha="deadbeef",
                            harness_usr_access=0x01020300, features=("clcd", "windowed"))
    f.start()
    try:
        yield f
    finally:
        f.stop()


def test_shellclient_surface(fake):
    with ShellClient(fake.host, fake.control_port, timeout=5.0) as shell:
        p = shell.ping()
        assert isinstance(p, PingResponse)
        assert p.shell_id == "0xa1b2c3d4" and p.rm_id == "0x00000000"

        v = shell.version()
        assert isinstance(v, VersionResponse)
        assert v.harness == "1.2.3" and v.sha == "deadbeef" and v.dirty is False
        assert v.features == ("clcd", "windowed")
        assert v.skew_verdict in ("ok", "SKEW", "unchecked")
        # ADDITIVE: the default FakeShell is bare metal and says nothing.
        assert v.impl == "bare-metal"

        d = shell.diag()
        assert isinstance(d, DiagResponse) and d.ok
        assert shell.reset("dut").ok
        assert shell.set_clk("25mhz", presets=DEFAULT_CLK_PRESETS).ok
        assert shell.link("down").ok
        shell.display_owner()
        shell.display_settled("harness", timeout=0.5)
        assert shell.macgen(gen=True, chk=True, inject="none").ok
        frame, resp = shell.read_dut_frame()
        assert frame is None or isinstance(frame, bytes)


def test_signatures_socharness_calls():
    sig = inspect.signature(ShellClient.__init__)
    assert list(sig.parameters)[:3] == ["self", "host", "port"]
    assert "timeout" in sig.parameters
    assert "presets" in inspect.signature(ShellClient.set_clk).parameters
    assert "timeout" in inspect.signature(ShellClient.display_settled).parameters
    mg = inspect.signature(ShellClient.macgen).parameters
    assert {"gen", "chk", "inject"} <= set(mg)


def test_versionresponse_fields():
    names = {f.name for f in dataclasses.fields(VersionResponse)}
    assert {"ok", "harness", "ver32", "sha", "dirty", "lmb_kb", "features",
            "usr_access", "skew"} <= names
    assert "impl" in names                         # the additive one
    assert isinstance(VersionResponse.skew_verdict, property)
    # positional construction of the pre-impl fields still works
    v = VersionResponse(True, "1.0.0", "0x01000000", "abc", False, 1024, ("clcd",), "", None)
    assert v.impl == "bare-metal" and v.skew_verdict == "unchecked"


DIAG_FIELDS_PINNED = (
    "ok", "rx_recover", "rx_dumps", "rx_drops", "icap_bytes", "got", "expect",
    "rcv_wnd", "rcv_ann_wnd", "rx_queued", "pbuf_free", "grants_sent",
    "grant_fails", "sndbuf", "snd_wnd", "tx_frames_sent", "tx_status_drained",
    "tx_fifo_full_drops", "tx_errors", "tx_space_stalls", "tx_iface_errors",
    "tx_last_status", "icap_sr_last", "icap_eos_status", "ovlstore_phase",
    "ovlstore_detail", "touch_regs", "touch_adc_x", "touch_adc_y", "touch_verdict",
    "svc_count", "pass_max_us", "svc_max_us", "svc_max_ix", "svc_overruns",
    "svc_skips", "svc_skipped",
)


def test_diagresponse_fields():
    names = {f.name for f in dataclasses.fields(DiagResponse)}
    missing = set(DIAG_FIELDS_PINNED) - names
    assert not missing, missing
    # compatibility: an absent key still reads 0 through the field ...
    d = DiagResponse(ok=True)
    assert d.pbuf_free == 0
    # ... and the ADDITIVE `present` is how a caller tells absent from zero
    assert d.present == frozenset() and not d.has("pbuf_free")


# --------------------------------------------------------------------------- #
# "closed by peer": LOAD-BEARING for socharness ("board held")
# --------------------------------------------------------------------------- #

def _accept_then_eof_server():
    """A server that closes the control channel with NO reply (what a parked or
    single-client-refusing 6900 looks like). It drains the request first, so
    the close is a clean FIN -- closing with the request still unread would
    make the kernel send a RST instead, which is a different wire event."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.settimeout(5)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(256)
            if not chunk:
                break
            buf += chunk
        conn.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], t


def test_closed_by_peer_text_and_subclass_both_work():
    port, t = _accept_then_eof_server()
    shell = ShellClient("127.0.0.1", port, timeout=5.0)
    with pytest.raises(ConnectionError) as ei:
        with shell:
            shell.ping()
    t.join(5)
    exc = ei.value
    # 1. the string match socharness does today
    assert "closed by peer" in str(exc)
    assert str(exc) == "shell control channel closed by peer"
    # 2. the dedicated subclass, for new code
    assert isinstance(exc, client_mod.ShellChannelClosed)
    assert isinstance(exc, ConnectionError)
    # 3. and it is NOT a ConnectionResetError (which callers treat separately)
    assert not isinstance(exc, ConnectionResetError)


def test_connection_reset_is_not_wrapped():
    """A RST mid-read stays a ConnectionResetError -- socharness treats it as
    "held" on its own terms; pyverify must not turn it into something else."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        import struct
        conn, _ = srv.accept()
        conn.recv(64)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    shell = ShellClient("127.0.0.1", port, timeout=5.0)
    with pytest.raises(ConnectionResetError):
        with shell:
            shell.ping()
    t.join(5)


# --------------------------------------------------------------------------- #
# console / rm_id / overlay / pusher / swap
# --------------------------------------------------------------------------- #

def test_console_reader_surface():
    r = ConsoleReader("127.0.0.1", UART0_PORT)
    assert r.host == "127.0.0.1" and r.port == UART0_PORT
    for m in ("connect", "close", "read", "read_until", "assert_contains"):
        assert callable(getattr(r, m))


def test_rm_id_helpers():
    assert format_rm_id(parse_rm_id("0x01000001")) == "0x01000001"
    assert isinstance(design_id(0x01000001), int)


def test_overlay_names_are_classes():
    assert inspect.isclass(Overlay) and inspect.isclass(OverlayManifest)


def test_pusher_send_seam_is_subclassable():
    sig = inspect.signature(BitstreamPusher._send)
    assert list(sig.parameters) == ["self", "frame", "kind"]
    assert sig.parameters["kind"].kind is inspect.Parameter.KEYWORD_ONLY

    sent = []

    class Capture(BitstreamPusher):
        def _send(self, frame, *, kind):
            sent.append((len(frame), kind))
            return PushResult(ok=True, kind=kind, bytes_sent=len(frame))

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.bin"
        p.write_bytes(b"\x00" * 16)
        res = Capture("127.0.0.1").push_file(p, kind=BitstreamKind.PARTIAL, rm_slot=0,
                                            static_id=1, rm_id=2)
    assert res.ok and sent == [(HEADER_SIZE + 16, BitstreamKind.PARTIAL)]


def test_swap_orchestrator_is_importable_class():
    assert inspect.isclass(SwapOrchestrator)


# --------------------------------------------------------------------------- #
# FakeShell: kwargs, attributes, restart on the SAME ports, default unchanged
# --------------------------------------------------------------------------- #

def test_fakeshell_kwargs_and_attributes(fake):
    for attr in ("control_port", "raw_tcp_port", "tftp_port", "console_ports",
                 "current_rm_id", "swaps", "push_events", "link_events"):
        assert hasattr(fake, attr), attr
    assert set(fake.console_ports) == {"uart0", "uart1", "swo"}
    params = inspect.signature(FakeShell.__init__).parameters
    for kw in ("static_id", "boot_rm_id", "reset_targets", "harness_version",
               "harness_sha", "harness_usr_access", "features"):
        assert kw in params, kw


def test_fakeshell_restart_on_same_ports():
    f = FakeShell.ephemeral().start()
    ports = (f.control_port, f.raw_tcp_port, f.tftp_port, dict(f.console_ports))
    f.stop()
    f.start()
    try:
        assert (f.control_port, f.raw_tcp_port, f.tftp_port, dict(f.console_ports)) == ports
        with ShellClient(f.host, f.control_port, timeout=5.0) as shell:
            assert shell.ping().ok
    finally:
        f.stop()


def test_fakeshell_default_is_still_bare_metal():
    """The default double answers exactly as before the Linux profile existed:
    no impl key, lmb_kb 1024, no v0.11 verbs, every diag key present, no
    identify port bound."""
    f = FakeShell(static_id=0xA1B2C3D4)
    ver = f.handle_control({"op": "version"})
    assert "impl" not in ver and ver["lmb_kb"] == 1024 and ver["features"] == []
    assert list(ver)[-1] == "skew"
    assert f.handle_control({"op": "stats"}) == {"ok": False, "err": "unknown op 'stats'"}
    # It may GROW (diag.h v9 appended svc_us_6 + usd_boot for D13), never shrink.
    assert len(f.handle_control({"op": "diag"})) == 1 + 44
    assert f.profile == "bare-metal" and not f.identify_enabled and not f.tofu_enabled
    assert json.dumps(ver, separators=(",", ":")).endswith('"usr_access":null,"skew":null}')


# --------------------------------------------------------------------------- #
# net-protocol v0.13 (D13, the user microSD): pinned 2026-09-23 at the
# socharness lead's request -- socharness reads `usd` for its status view and
# drives deploy(persist=...).
# --------------------------------------------------------------------------- #

def test_v013_usd_surface():
    from pyverify.client import CommitResponse, UsdDefault, UsdResponse
    assert callable(ShellClient.usd)
    names = {f.name for f in dataclasses.fields(UsdResponse)}
    assert {"ok", "present", "state", "text", "card_mb", "default", "boot", "err",
            "raw"} <= names
    assert isinstance(UsdResponse.ready, property)
    assert isinstance(UsdResponse.committable, property)
    assert {f.name for f in dataclasses.fields(UsdDefault)} >= {"rm_id", "static_id", "slot"}
    assert {f.name for f in dataclasses.fields(CommitResponse)} >= {"ok", "slot", "err"}
    # positional construction of the pre-v0.13 CommitResponse still works
    assert CommitResponse(True, "B").err == ""


def test_v013_commit_begin_await_signatures():
    sig = inspect.signature(ShellClient.commit_begin).parameters
    assert list(sig)[:2] == ["self", "rm"]
    for kw in ("rm_id", "static_id", "clear_len", "clear_crc", "part_len", "part_crc", "src"):
        assert kw in sig and sig[kw].kind is inspect.Parameter.KEYWORD_ONLY, kw
    assert sig["src"].default == "tcp"
    assert list(inspect.signature(ShellClient.commit_await).parameters) == ["self"]


def test_v013_deploy_persist_and_persist_result():
    from pyverify.swap import PersistResult, SwapDeployResult
    p = inspect.signature(SwapOrchestrator.deploy).parameters["persist"]
    assert p.default is True and p.kind is inspect.Parameter.KEYWORD_ONLY
    from pyverify.board import Mps3Board
    assert inspect.signature(Mps3Board.deploy).parameters["persist"].default is True
    names = {f.name for f in dataclasses.fields(PersistResult)}
    assert {"status", "slot", "reason", "err", "warning"} <= names
    assert isinstance(PersistResult.committed, property)
    assert "persist" in {f.name for f in dataclasses.fields(SwapDeployResult)}
    import pyverify
    assert pyverify.UsdResponse is not None and pyverify.PersistResult is PersistResult


def test_v013_usd_against_the_default_fakeshell(fake):
    """The default (bare-metal) double answers `usd` with the contract's
    no-card line: absence is not an error."""
    with ShellClient(fake.host, fake.control_port, timeout=5.0) as shell:
        r = shell.usd()
    assert r.ok and r.present is False and r.state == "none" and r.boot == "none"
