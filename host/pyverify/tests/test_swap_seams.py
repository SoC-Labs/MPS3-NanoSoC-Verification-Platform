"""Failure-seam tests for :mod:`pyverify.swap` (the deploy orchestrator's
error paths) — previously only the happy path and the host-side static_id
*match* rejection were covered.

Two seams the audit flagged:

1. **Push accepted, swap NAK'd server-side.** The overlay validates and both
   bitstreams push fine, then the shell rejects the ``swap`` (VERIFY fails).
   ``SwapOrchestrator.deploy`` must raise :class:`SwapError`, and the CLI must
   turn that into a clean **exit 1** (not a traceback). Covered here at the
   orchestrator level (fake client) and end-to-end through ``main()`` against a
   FakeShell fault-injected to fail VERIFY.

2. **Non-hex ``shell_id`` silently disables the static_id cross-check**
   (``swap.py`` ``deploy()``: a ``ValueError`` from ``int(ping.shell_id, 0)``
   sets ``expected_static_id = None``, which makes ``overlay.validate()`` skip
   the mismatch guard entirely). This test **pins the current fail-soft
   behaviour** and is flagged for follow-up — see the test docstring.
"""
from __future__ import annotations

import json
import socket
import zlib
from pathlib import Path

import pytest

from pyverify.cli import main
from pyverify.client import PingResponse, SwapResponse
from pyverify.overlay import Overlay
from pyverify.swap import SwapError, SwapOrchestrator
from pyverify.testing.fakeshell import FakeShell

SYNTHETIC_STATIC_ID = 0x2F458F06


def _make_overlay(root: Path, *, static_id: int = SYNTHETIC_STATIC_ID,
                  rm_name: str = "nanosoc", rm_id: int = 1) -> Overlay:
    """A minimal valid overlay triple on disk (manifest + two .bins whose
    lengths/CRCs match), so ``overlay.validate()`` passes on everything except
    whatever the test is probing."""
    directory = root / rm_name
    directory.mkdir(parents=True)
    clearing = b"\xC1\x0E\xA2\x00" * 32
    partial = b"\xB1\x75\x00\x0D" * 96
    (directory / f"{rm_name}_clear.bin").write_bytes(clearing)
    (directory / f"{rm_name}.bin").write_bytes(partial)
    manifest = {
        "schema": 1,
        "static_id": f"0x{static_id:08X}",
        "rm_id": f"0x{rm_id:08x}",
        "rm_name": rm_name,
        "clearing": {"file": f"{rm_name}_clear.bin", "len": len(clearing),
                     "crc32": f"0x{zlib.crc32(clearing) & 0xFFFFFFFF:08x}"},
        "partial": {"file": f"{rm_name}.bin", "len": len(partial),
                    "crc32": f"0x{zlib.crc32(partial) & 0xFFFFFFFF:08x}"},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return Overlay.load(directory)


class _FakeClient:
    """The slice of ShellClient SwapOrchestrator uses: ping + the PARKED swap
    (``swap_begin``/``swap_await``) + host.

    The split is the protocol, not an implementation detail: the shell does not
    answer ``swap`` until the bitstream has been pushed INTO it, so a client fake
    that answered immediately (this one, until 2026-07-14) lets a
    push-then-swap orchestrator look correct. Every call is appended to a shared
    ``log`` so a test can assert the ORDER, not just the calls.
    """

    def __init__(self, *, shell_id: str, swap_ok: bool = True,
                 log: list[str] | None = None):
        self.host = "fake-shell"
        self._shell_id = shell_id
        self._swap_ok = swap_ok
        self.pinged = 0
        self.swapped = 0          # swap RPCs SENT (swap_begin)
        self.awaited = 0          # parked replies COLLECTED (swap_await)
        self.log: list[str] = log if log is not None else []
        self._in_flight: tuple | None = None

    def ping(self) -> PingResponse:
        self.pinged += 1
        self.log.append("ping")
        return PingResponse(ok=True, shell_id=self._shell_id, rm_id="0x00000000")

    def swap_begin(self, rm: str, src: str = "tftp") -> None:
        self.swapped += 1
        self._in_flight = (rm, src)
        self.log.append("swap_begin")

    def swap_await(self) -> SwapResponse:
        if self._in_flight is None:
            raise AssertionError(
                "swap_await() with no swap in flight — the reply is PARKED by "
                "the swap RPC; you cannot collect one you never sent"
            )
        self._in_flight = None
        self.awaited += 1
        self.log.append("swap_await")
        return SwapResponse(ok=self._swap_ok, rm_id="0x00000009", verified=self._swap_ok)

    def swap(self, rm: str, src: str = "tftp") -> SwapResponse:
        self.swap_begin(rm, src)
        return self.swap_await()


class _RecordingPusher:
    def __init__(self, log: list[str] | None = None) -> None:
        self.pushes: list = []
        self.log: list[str] = log if log is not None else []

    def push_pair(self, overlay, *, rm_slot: int = 0):
        self.pushes.append((overlay, rm_slot))
        self.log.append("push_pair")
        return (object(), object())


# --------------------------------------------------------------------------- #
# Seam 0: THE ORDER ITSELF (regression guard, 2026-07-14)
# --------------------------------------------------------------------------- #

def test_deploy_pushes_between_swap_begin_and_swap_await(tmp_path: Path) -> None:
    """The load-bearing order, asserted at the orchestrator seam.

    The shell only listens for the bitstream once the ``swap`` RPC has driven its
    FSM into AWAIT_INCOMING_CLEARING/AWAIT_PARTIAL — which is exactly why it
    PARKS the control connection. deploy() must therefore send the swap, push
    into it, and only then collect the reply. The old order (push, then swap)
    hit a shell that was not expecting data: it reset the push connection and the
    swap failed with nothing staged (KU115, 2026-07-14).
    """
    overlay = _make_overlay(tmp_path)
    log: list[str] = []
    client = _FakeClient(shell_id=f"0x{SYNTHETIC_STATIC_ID:08x}", log=log)
    pusher = _RecordingPusher(log=log)
    orch = SwapOrchestrator(client, pusher)  # type: ignore[arg-type]

    orch.deploy(overlay)

    assert log == ["ping", "swap_begin", "push_pair", "swap_await"]


# --------------------------------------------------------------------------- #
# Seam 1: push accepted, swap NAK'd
# --------------------------------------------------------------------------- #

def test_swap_nak_after_accepted_push_raises_swaperror(tmp_path: Path) -> None:
    """The overlay validates and the push happens INTO the parked swap, and THEN
    the shell NAKs it — deploy() must raise SwapError, and the push must have
    already been attempted (proving the NAK is the server-side swap step, not
    validation, and that the push really did land inside the swap window)."""
    overlay = _make_overlay(tmp_path)
    log: list[str] = []
    client = _FakeClient(shell_id=f"0x{SYNTHETIC_STATIC_ID:08x}", swap_ok=False, log=log)
    pusher = _RecordingPusher(log=log)
    orch = SwapOrchestrator(client, pusher)  # type: ignore[arg-type]

    with pytest.raises(SwapError, match="swap RPC rejected"):
        orch.deploy(overlay)
    assert pusher.pushes, "push must have been attempted before the swap NAK"
    assert client.swapped == 1
    # The NAK is the reply to a swap the push already went into.
    assert log == ["ping", "swap_begin", "push_pair", "swap_await"]


# --------------------------------------------------------------------------- #
# Seam 1b: swap RPC times out (control channel reached, swap did not finish)
# --------------------------------------------------------------------------- #

class _TimeoutOnSwapClient(_FakeClient):
    """ping() succeeds (control channel IS reachable); the PARKED REPLY never
    arrives — the real failure mode when ShellClient's timeout is shorter than a
    swap (the shell parks the connection for the whole reconfiguration, and that
    window now spans the bitstream push too)."""

    def swap_await(self) -> SwapResponse:
        self.awaited += 1
        self.log.append("swap_await")
        raise socket.timeout("timed out")


def test_swap_rpc_timeout_is_a_precise_swaperror_not_unreachable(tmp_path: Path) -> None:
    """A timeout DURING the swap RPC must surface as a SwapError that names the
    real cause (client timeout too short for a real swap), NOT propagate as a bare
    OSError — which cli.py would then mislabel as "cannot reach the control
    channel", the misdiagnosis this guard exists to prevent."""
    overlay = _make_overlay(tmp_path)
    client = _TimeoutOnSwapClient(shell_id=f"0x{SYNTHETIC_STATIC_ID:08x}")
    orch = SwapOrchestrator(client, _RecordingPusher())  # type: ignore[arg-type]

    with pytest.raises(SwapError) as ei:
        orch.deploy(overlay)
    msg = str(ei.value)
    assert "did not complete" in msg and "timeout" in msg.lower()
    assert "NOT an unreachable control channel" in msg
    # The bare OSError must NOT escape (cli.py's OSError branch mislabels it).
    assert not isinstance(ei.value, OSError)
    assert client.swapped == 1


def test_cli_deploy_swap_nak_exits_1(tmp_path: Path, capsys) -> None:
    """End-to-end: a FakeShell that ACCEPTS the pushed pair but fails the swap
    VERIFY step (fault-injected wrong RM_ID readback). ``python -m pyverify.cli
    deploy`` must exit 1 with a clean one-line stderr, no stdout, no traceback —
    and the server side must show the push accepted but the swap FAILED."""
    overlay_dir = _make_overlay(tmp_path).directory
    with FakeShell.ephemeral(static_id=SYNTHETIC_STATIC_ID) as fake:
        fake.rm_id_readback_override = 0x63  # VERIFY reads back the wrong RM
        argv = [
            "deploy", "--host", fake.host, "--overlay", str(overlay_dir),
            "--control-port", str(fake.control_port),
            "--tftp-port", str(fake.tftp_port),
        ]
        rc = main(argv)
        captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "swap RPC rejected" in captured.err
    assert "Traceback" not in captured.err
    # server-side truth: the pair WAS accepted, the swap FSM ran to FAILED.
    assert [e.status for e in fake.push_events] == ["OK", "OK"]
    assert fake.swaps[-1]["final"] == "FAILED"


# --------------------------------------------------------------------------- #
# Seam 2: non-hex shell_id silently skips the static_id cross-check
# --------------------------------------------------------------------------- #

def test_non_hex_shell_id_fails_loud_and_pushes_nothing(tmp_path: Path) -> None:
    """A non-empty but unparseable ``shell_id`` now FAILS LOUD (2026-07-08).

    ``SwapOrchestrator.deploy`` derives ``expected_static_id`` from
    ``int(ping.shell_id, 0)``. Previously a non-hex ``shell_id`` swallowed the
    ValueError and set it to ``None``, silently skipping the static_id mismatch
    guard — so an overlay built for a DIFFERENT static would push+swap with no
    cross-check. That is exactly what overlay-manifest.md's static_id rule
    exists to prevent, so the shell reporting a garbage id now raises
    :class:`SwapError` before any bytes cross the wire.
    """
    overlay = _make_overlay(tmp_path, static_id=0xAAAA0000)
    client = _FakeClient(shell_id="greybox-unprogrammed", swap_ok=True)
    pusher = _RecordingPusher()
    orch = SwapOrchestrator(client, pusher)  # type: ignore[arg-type]

    with pytest.raises(SwapError, match="unparseable shell_id"):
        orch.deploy(overlay)
    assert not pusher.pushes, "deploy must not push to an unverifiable static"
    assert client.swapped == 0


def test_hex_shell_id_mismatch_still_raises(tmp_path: Path) -> None:
    """Contrast to the above: when the shell reports a well-formed hex
    ``shell_id`` that DISAGREES with the overlay's static_id, the guard fires —
    SwapError before any push. This is what makes the non-hex skip a real hole,
    not a design choice."""
    overlay = _make_overlay(tmp_path, static_id=0xAAAA0000)
    client = _FakeClient(shell_id="0x11111111", swap_ok=True)  # valid hex, mismatched
    pusher = _RecordingPusher()
    orch = SwapOrchestrator(client, pusher)  # type: ignore[arg-type]

    with pytest.raises(SwapError, match="static_id mismatch"):
        orch.deploy(overlay)
    assert not pusher.pushes, "nothing must be pushed when the static check rejects"
    assert client.swapped == 0