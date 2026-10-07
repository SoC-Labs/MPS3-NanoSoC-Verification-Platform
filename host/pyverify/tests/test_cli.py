"""Tests for pyverify.cli: argument parsing + deploy exit-code mapping.

Historically parser-only (the deploy path used to dead-end in the
pusher's ``NotImplementedError`` stub, mapped to exit 3). W-PUSH made the
network layer real, so exit 3 now means "shell unreachable / transport
failure" — asserted here by pointing ``deploy`` at guaranteed-closed
localhost ports (no external network, deterministic refusal). The intent
is unchanged: a deploy that can't reach a shell exits cleanly with a
message, never a traceback.
"""
from __future__ import annotations

import json
import socket
import zlib
from pathlib import Path

import pytest

from pyverify.cli import build_parser, main


def test_deploy_requires_host_and_overlay() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "deploy", "--host", "192.168.10.101", "--overlay", "overlay/nanosoc",
    ])
    assert args.verb == "deploy"
    assert args.host == "192.168.10.101"
    assert args.overlay == "overlay/nanosoc"
    # defaults
    assert args.rm_slot == 0
    # src / transport / windowed default to AUTO (None): chosen from the
    # running shell's version.features by cli._select_transport
    # (tests/test_cli_transport_auto.py) -- a windowed shell gets tcp + tcp +
    # windowed, anything else the historic tftp/plain.
    assert args.src is None
    assert args.pusher_transport is None
    assert args.control_port == 6900
    # the client timeout must be generous — the 5 s ShellClient default cannot
    # survive a real parked swap.
    assert args.windowed is None
    assert args.window == 16384
    assert args.client_timeout == 180.0


def test_deploy_accepts_overrides() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "deploy", "--host", "10.0.0.5", "--overlay", "overlay/eth_ss",
        "--rm-slot", "1", "--src", "tcp", "--pusher-transport", "tcp",
        "--control-port", "6901", "--windowed", "--window", "8192",
        "--client-timeout", "240",
    ])
    assert args.rm_slot == 1
    assert args.src == "tcp"
    assert args.pusher_transport == "tcp"
    assert args.control_port == 6901
    assert args.windowed is True
    assert args.window == 8192
    assert args.client_timeout == 240.0


def test_edge_channels_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["edge", "channels"])
    assert args.verb == "edge"
    assert args.edge_verb == "channels"
    assert args.board == "mps3_01"


def test_edge_channels_accepts_board_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["edge", "channels", "--board", "mps3_02"])
    assert args.board == "mps3_02"


def test_edge_status_defaults_have_no_host() -> None:
    parser = build_parser()
    args = parser.parse_args(["edge", "status"])
    assert args.verb == "edge"
    assert args.edge_verb == "status"
    assert args.board == "mps3_01"
    assert args.host is None
    assert args.control_port == 6900


def test_edge_status_accepts_host_and_port_overrides() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "edge", "status", "--board", "mps3_02", "--host", "192.168.10.101",
        "--control-port", "6901",
    ])
    assert args.board == "mps3_02"
    assert args.host == "192.168.10.101"
    assert args.control_port == 6901


def test_edge_requires_a_subverb() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["edge"])


# --------------------------------------------------------------------------- #
# deploy exit-code mapping (see cli.py's docstring table)
# --------------------------------------------------------------------------- #


def _closed_port() -> int:
    """A localhost port with nothing listening (bind, learn, release)."""
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _write_overlay(root: Path) -> Path:
    overlay_dir = root / "nanosoc"
    overlay_dir.mkdir()
    clearing = b"\x00\x01\x02\x03" * 8
    partial = b"\xde\xad\xbe\xef" * 8
    (overlay_dir / "nanosoc_clear.bin").write_bytes(clearing)
    (overlay_dir / "nanosoc.bin").write_bytes(partial)
    (overlay_dir / "manifest.json").write_text(json.dumps({
        "schema": 1,
        "static_id": "0xA1B2C3D4",
        "rm_id": "0x1",
        "rm_name": "nanosoc",
        "clearing": {"file": "nanosoc_clear.bin", "len": len(clearing),
                     "crc32": f"0x{zlib.crc32(clearing):08X}"},
        "partial": {"file": "nanosoc.bin", "len": len(partial),
                    "crc32": f"0x{zlib.crc32(partial):08X}"},
    }))
    return overlay_dir


def test_deploy_bad_manifest_exits_2(tmp_path: Path, capsys) -> None:
    rc = main([
        "deploy", "--host", "127.0.0.1", "--overlay", str(tmp_path / "nothing-here"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "bad overlay manifest" in err


def test_deploy_unreachable_shell_exits_3_with_clean_message(
    tmp_path: Path, capsys,
) -> None:
    """Connection refused on the control channel (guaranteed-closed
    localhost port) must be a clean exit 3 + one-line stderr message —
    the clean-venv acceptance path, minus the venv."""
    overlay_dir = _write_overlay(tmp_path)
    port = _closed_port()
    rc = main([
        "deploy", "--host", "127.0.0.1", "--overlay", str(overlay_dir),
        "--control-port", str(port),
    ])
    assert rc == 3
    err = capsys.readouterr().err
    assert "cannot reach shell control channel" in err
    assert f"127.0.0.1:{port}" in err
    assert "Traceback" not in err


def test_deploy_windowed_flag_reaches_the_pusher() -> None:
    """A default that can't talk to the real board, or a flag that doesn't
    reach the transport, is the exact class of bug this file guards. Prove the
    --windowed path constructs a windowed BitstreamPusher (the real KU115
    needs it; fire-and-hose resets against the FIFO-mode shell)."""
    from pyverify.cli import _build_pusher
    default = _build_pusher("192.168.10.101", "tcp")
    assert default.windowed is False        # backward-compatible default
    win = _build_pusher("192.168.10.101", "tcp", windowed=True, window=8192)
    assert win.windowed is True
    assert win.window == 8192
