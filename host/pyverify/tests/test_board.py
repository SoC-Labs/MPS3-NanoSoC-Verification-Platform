"""Tests for pyverify.board.Mps3Board — the "PYNQ experience" facade.

All collaborators are injected fakes (client / pusher / console factory),
so nothing here opens a socket: the facade's *wiring* is what's under
test. The real transports underneath are covered by test_client.py,
test_pusher_send.py and test_reattach_apply.py.
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from pyverify.board import Mps3Board
from pyverify.client import (
    CommitResponse,
    LinkResponse,
    PingResponse,
    ResetResponse,
    SetClkResponse,
    SwapResponse,
    TelemetryResponse,
)
from pyverify.console import SWO_PORT, UART0_PORT, UART1_PORT
from pyverify.overlay import Overlay, OverlayManifestError

STATIC_ID = 0xA1B2C3D4
RM_ID = 1


def _write_overlay(root: Path, name: str = "nanosoc") -> Path:
    """Valid on-disk overlay triple (same shape as test_overlay.py's)."""
    overlay_dir = root / name
    overlay_dir.mkdir(parents=True)
    clearing = b"\x00\x01\x02\x03" * 16
    partial = b"\xde\xad\xbe\xef" * 32
    (overlay_dir / f"{name}_clear.bin").write_bytes(clearing)
    (overlay_dir / f"{name}.bin").write_bytes(partial)
    (overlay_dir / "manifest.json").write_text(json.dumps({
        "schema": 1,
        "static_id": f"0x{STATIC_ID:08X}",
        "rm_id": f"0x{RM_ID:08X}",
        "rm_name": name,
        "clearing": {"file": f"{name}_clear.bin", "len": len(clearing),
                     "crc32": f"0x{zlib.crc32(clearing):08X}"},
        "partial": {"file": f"{name}.bin", "len": len(partial),
                    "crc32": f"0x{zlib.crc32(partial):08X}"},
    }))
    return overlay_dir


class FakeShellClient:
    """Duck-typed ShellClient: records calls, plays scripted responses."""

    def __init__(self, host: str = "shell.example") -> None:
        self.host = host
        self.connected = False
        self.closed = False
        self.calls: list[tuple] = []
        self._swap_in_flight: tuple | None = None

    def connect(self) -> "FakeShellClient":
        self.connected = True
        return self

    def close(self) -> None:
        self.closed = True
        self.connected = False

    def ping(self) -> PingResponse:
        self.calls.append(("ping",))
        return PingResponse(ok=True, shell_id=f"0x{STATIC_ID:08X}", rm_id="0x0")

    # The swap is a PARKED RPC (net-protocol.md): the shell holds the reply for
    # the whole reconfiguration, and the bitstream push happens in between. This
    # fake models that split so it cannot silently accept the wrong order.
    def swap_begin(self, rm: str, src: str = "tftp") -> None:
        self.calls.append(("swap", rm, src))
        self._swap_in_flight = (rm, src)

    def swap_await(self) -> SwapResponse:
        if self._swap_in_flight is None:
            raise AssertionError("swap_await() with no swap in flight")
        self._swap_in_flight = None
        self.calls.append(("swap_await",))
        return SwapResponse(ok=True, rm_id=f"0x{RM_ID:08X}", verified=True)

    def swap(self, rm: str, src: str = "tftp") -> SwapResponse:
        self.swap_begin(rm, src)
        return self.swap_await()

    def reset(self, target: str = "dut") -> ResetResponse:
        self.calls.append(("reset", target))
        return ResetResponse(ok=True)

    def set_clk(self, preset: str) -> SetClkResponse:
        self.calls.append(("set_clk", preset))
        return SetClkResponse(ok=True, locked=True)

    def link(self, event: str) -> LinkResponse:
        self.calls.append(("link", event))
        return LinkResponse(ok=True)

    def commit(self, rm: str) -> CommitResponse:
        self.calls.append(("commit", rm))
        return CommitResponse(ok=True, slot="B")

    def telemetry(self) -> TelemetryResponse:
        self.calls.append(("telemetry",))
        # v0.6: telemetry has no success shape (no power sensor exists).
        return TelemetryResponse(ok=False, err="no power sensor", lockup=False)


class FakePusher:
    def __init__(self) -> None:
        self.pushed: list[tuple] = []

    def push_pair(self, overlay, *, rm_slot: int = 0):
        self.pushed.append((overlay.manifest.rm_name, rm_slot))
        return (object(), object())


class FakeConsole:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.connected = False
        self.closed = False

    def connect(self) -> "FakeConsole":
        self.connected = True
        return self

    def close(self) -> None:
        self.closed = True
        self.connected = False


def _board(tmp_path: Path, **kwargs) -> tuple[Mps3Board, FakeShellClient, FakePusher]:
    client = FakeShellClient()
    pusher = FakePusher()
    board = Mps3Board(
        "shell.example",
        overlay_root=tmp_path,
        client=client,
        pusher=pusher,
        console_factory=FakeConsole,
        **kwargs,
    )
    return board, client, pusher


def test_context_manager_connects_and_closes() -> None:
    client = FakeShellClient()
    with Mps3Board("shell.example", client=client, pusher=FakePusher(),
                   console_factory=FakeConsole) as board:
        assert client.connected
        assert board.ping().ok
    assert client.closed


def test_deploy_by_bare_name_resolves_against_overlay_root(tmp_path: Path) -> None:
    _write_overlay(tmp_path)
    board, client, pusher = _board(tmp_path)
    board.connect()

    result = board.deploy("nanosoc")

    assert result.verified
    assert result.rm_id == f"0x{RM_ID:08X}"
    assert pusher.pushed == [("nanosoc", 0)]
    # ping (static_id learn) must precede the swap RPC.
    assert client.calls[0] == ("ping",)
    assert ("swap", "nanosoc", "tftp") in client.calls
    assert board.last_deploy is result


def test_deploy_by_explicit_path_and_overlay_instance(tmp_path: Path) -> None:
    overlay_dir = _write_overlay(tmp_path)
    board, _client, pusher = _board(tmp_path / "elsewhere-root")
    board.connect()

    board.deploy(overlay_dir)  # explicit directory path, root irrelevant
    board.deploy(Overlay.load(overlay_dir))  # pre-loaded Overlay object
    assert len(pusher.pushed) == 2


def test_deploy_kwargs_flow_through(tmp_path: Path) -> None:
    _write_overlay(tmp_path, name="eth_ss")
    board, client, pusher = _board(tmp_path)
    board.connect()

    board.deploy("eth_ss", rm_slot=1, src="tcp")

    assert pusher.pushed == [("eth_ss", 1)]
    assert ("swap", "eth_ss", "tcp") in client.calls


def test_deploy_missing_overlay_is_a_manifest_error(tmp_path: Path) -> None:
    board, _client, _pusher = _board(tmp_path)
    board.connect()
    with pytest.raises(OverlayManifestError, match="no manifest"):
        board.deploy("does-not-exist")


def test_deploy_reattach_plan_records_shell_host(tmp_path: Path) -> None:
    """The facade's deploy must yield an apply()-able plan that knows
    which host to reopen consoles against (the injected client's)."""
    _write_overlay(tmp_path)
    board, _client, _pusher = _board(tmp_path)
    board.connect()
    result = board.deploy("nanosoc")
    assert result.reattach.shell_host == "shell.example"
    assert result.reattach.console_reopen_ports == (6930, 6931, 6932)


def test_console_accessors_lazy_connect_and_cache() -> None:
    board, _client, _pusher = _board(Path("unused"))
    u0 = board.uart0
    assert isinstance(u0, FakeConsole)
    assert (u0.host, u0.port) == ("shell.example", UART0_PORT)
    assert u0.connected
    assert board.uart0 is u0  # cached, not reconnected
    assert board.uart1.port == UART1_PORT
    assert board.swo.port == SWO_PORT


def test_reopen_consoles_drops_stale_connections() -> None:
    board, _client, _pusher = _board(Path("unused"))
    stale = board.uart0
    board.reopen_consoles()
    assert stale.closed
    fresh = board.uart0
    assert fresh is not stale
    assert fresh.connected


def test_close_closes_consoles_and_client() -> None:
    board, client, _pusher = _board(Path("unused"))
    board.connect()
    u0, swo = board.uart0, board.swo
    board.close()
    assert u0.closed and swo.closed
    assert client.closed


def test_control_passthroughs_delegate_to_client() -> None:
    board, client, _pusher = _board(Path("unused"))
    board.connect()

    # The facade is a passthrough: it hands back whatever the client parsed —
    # for telemetry that is always the v0.6 failure (net-protocol.md: there is
    # no power sensor on this platform, so the verb has no success shape).
    assert board.telemetry().err == "no power sensor"
    assert board.set_clk("25mhz").locked
    assert board.reset().ok
    assert board.reset("por").ok
    assert board.link("down").ok
    assert board.commit("nanosoc").slot == "B"

    assert ("telemetry",) in client.calls
    assert ("set_clk", "25mhz") in client.calls
    assert ("reset", "dut") in client.calls
    assert ("reset", "por") in client.calls
    assert ("link", "down") in client.calls
    assert ("commit", "nanosoc") in client.calls


def test_set_clk_typo_raises_before_touching_client() -> None:
    # Client-side preset validation (W-HOST-MISC; OPEN_ISSUES.md I16):
    # a typo dies in the facade — the injected client never sees a call.
    board, client, _pusher = _board(Path("unused"))
    board.connect()
    with pytest.raises(ValueError, match="unknown preset"):
        board.set_clk("25 mhz")
    assert client.calls == []


def test_set_clk_presets_none_passes_through_facade() -> None:
    # Escape hatch reaches the wire through the facade too; the plain
    # single-arg fake set_clk(preset) shape keeps working (the facade
    # inspects the injected client's signature rather than forcing the
    # presets kwarg on duck-typed seams).
    board, client, _pusher = _board(Path("unused"))
    board.connect()
    assert board.set_clk("13mhz", presets=None).ok
    assert ("set_clk", "13mhz") in client.calls


def test_set_clk_custom_presets_via_facade() -> None:
    board, client, _pusher = _board(Path("unused"))
    board.connect()
    assert board.set_clk("200mhz", presets=("200mhz",)).ok
    assert ("set_clk", "200mhz") in client.calls
    with pytest.raises(ValueError, match="unknown preset"):
        board.set_clk("25mhz", presets=("200mhz",))


def test_set_clk_facade_calls_client_plain_no_double_validation() -> None:
    # The facade validates once, against the *caller's* set, then calls
    # the client with the plain single-argument shape — a client with the
    # real ShellClient signature must see its presets kwarg left at its
    # (wire-transparent) default, so a custom facade-validated name is
    # never re-checked against the client's default table.
    class PresetsAwareClient(FakeShellClient):
        def set_clk(self, preset, *, presets=None):
            self.calls.append(("set_clk", preset, presets))
            return SetClkResponse(ok=True, locked=True)

    client = PresetsAwareClient()
    board = Mps3Board("shell.example", client=client, pusher=FakePusher())
    board.connect()
    assert board.set_clk("200mhz", presets=("200mhz",)).ok
    assert ("set_clk", "200mhz", None) in client.calls


def test_default_collaborators_are_real_types() -> None:
    """No injections: the facade must build a real ShellClient and a real
    BitstreamPusher wired to this board's host/ports (no connect here)."""
    from pyverify.client import ShellClient
    from pyverify.pusher import BitstreamPusher

    board = Mps3Board(
        "192.168.10.101", control_port=6901, tftp_port=1069,
        tcp_push_port=16910, transport="tcp",
    )
    assert isinstance(board._client, ShellClient)
    assert board._client.port == 6901
    assert isinstance(board._pusher, BitstreamPusher)
    assert board._pusher.transport == "tcp"
    assert board._pusher.tftp_port == 1069
    assert board._pusher.tcp_port == 16910
