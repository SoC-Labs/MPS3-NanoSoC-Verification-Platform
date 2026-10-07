"""Board-free proofs for :mod:`socket_harness.harness` — the unified facade.

Two things are proven here with ZERO real sockets to a board:

1. The pyverify interop KEYSTONE: ``SocketHarness.shell()`` returns a real
   :class:`pyverify.client.ShellClient` whose transport is a reconnecting
   ``LineSession``. Because ``LineChannel`` is byte-identical to
   ``pyverify.client.Transport``, the harness gets ``ping``/``swap`` "for free"
   over the session — driven here through an in-process JSON-line responder on
   one end of a ``socket.socketpair`` (promoting pyverify's FakeTransport idea
   to the socket layer).

2. The HONEST probe: ``probe`` reports ``ok``/``declined``/``unreachable`` and
   NEVER fabricates a plausible zero (same discipline as
   ``pyverify.edge.status_fpga`` / ``TelemetryResponse``): ``ok=True`` -> ok
   with rm_id; ``ok=False`` -> declined; a peer that closes -> unreachable; a
   CSR ``TargetNotFound`` -> unreachable. Probe never raises for an unreachable
   endpoint.
"""
from __future__ import annotations

import json
import socket
import threading

import pytest

from pyverify.client import ShellClient

from socket_harness.harness import ProbeResult, SocketHarness
from socket_harness.loopback import FakeXsdbRunner
from socket_harness.xsdb import TargetNotFound

HOST = "192.168.10.101"


# --------------------------------------------------------------------------- #
# An in-process JSON-line shell over a socketpair. The client end is handed to
# the harness via its ``opener`` seam; the server end is driven by a scripted
# handler in a daemon thread. Records every request dict it received.
# --------------------------------------------------------------------------- #


class _LineShell:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.received: list[dict] = []
        self._srv, self._cli = socket.socketpair()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def opener(self, address=None, timeout=None):
        # The harness calls opener((host, port), timeout=...) — signature-
        # compatible, and always hands back the pre-wired client socket.
        return self._cli

    def _serve(self) -> None:
        self._srv.settimeout(0.2)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._srv.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line:
                    continue
                req = json.loads(line.decode())
                self.received.append(req)
                resp = self._handler(req)
                if resp is None:
                    # simulate a peer half-close (unreachable path)
                    try:
                        self._srv.close()
                    except OSError:
                        pass
                    return
                try:
                    self._srv.sendall(json.dumps(resp).encode() + b"\n")
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        for s in (self._srv, self._cli):
            try:
                s.close()
            except OSError:
                pass
        self._t.join(timeout=1.0)


def _ops(shell: _LineShell) -> list:
    return [r.get("op") for r in shell.received]


# --------------------------------------------------------------------------- #
# The interop keystone: shell() rides a LineSession, pyverify verbs just work.
# --------------------------------------------------------------------------- #


def test_shell_returns_shellclient_and_drives_ping_and_swap() -> None:
    def handler(req):
        if req.get("op") == "ping":
            return {"ok": True, "shell_id": "0xD84A2E7A", "rm_id": "0x1"}
        if req.get("op") == "swap":
            return {"ok": True, "rm_id": "0x2", "verified": True}
        return {"ok": False, "err": "unknown op"}

    shell = _LineShell(handler)
    try:
        harness = SocketHarness(HOST, opener=shell.opener)
        client = harness.shell()
        # It really is a pyverify ShellClient — no adapter (Transport == LineChannel).
        assert isinstance(client, ShellClient)

        ping = client.ping()
        assert ping.ok is True
        assert ping.rm_id == "0x1"
        assert ping.shell_id == "0xD84A2E7A"

        swap = client.swap(rm="nanosoc", src="tftp")
        assert swap.ok is True
        assert swap.rm_id == "0x2"
        assert swap.verified is True

        # The requests really crossed the LineSession, in order.
        assert _ops(shell) == ["ping", "swap"]
        assert shell.received[1] == {"op": "swap", "rm": "nanosoc", "src": "tftp"}
    finally:
        try:
            client.close()
        except Exception:
            pass
        shell.close()


# --------------------------------------------------------------------------- #
# probe(): honest liveness, never a fabricated zero.
# --------------------------------------------------------------------------- #


def test_probe_control_ok_carries_rm_id() -> None:
    shell = _LineShell(lambda req: {"ok": True, "shell_id": "0x1", "rm_id": "0x1"})
    try:
        harness = SocketHarness(HOST, opener=shell.opener)
        res = harness.probe("control")
        assert isinstance(res, ProbeResult)
        assert res.status == "ok"
        # honest: the real rm_id is surfaced somewhere (detail or value)
        assert "0x1" in (res.detail or "") or "0x1" in str(res.value)
    finally:
        shell.close()


def test_probe_control_declined_when_ok_false() -> None:
    shell = _LineShell(lambda req: {"ok": False})
    try:
        harness = SocketHarness(HOST, opener=shell.opener)
        res = harness.probe("control")
        assert res.status == "declined"
    finally:
        shell.close()


def test_probe_control_unreachable_on_peer_close_not_a_fake_zero() -> None:
    """NEGATIVE CONTROL: a fake that closes immediately yields 'unreachable',
    NOT 'ok' — the probe never invents a plausible healthy answer."""
    shell = _LineShell(lambda req: None)  # close on first request
    try:
        harness = SocketHarness(HOST, opener=shell.opener)
        res = harness.probe("control")
        assert res.status == "unreachable"
        assert res.status != "ok"
    finally:
        shell.close()


# --------------------------------------------------------------------------- #
# probe() over the CSR/xsdb backend.
# --------------------------------------------------------------------------- #


def test_probe_csr_ok_reads_rm_id_over_fake_xsdb_runner() -> None:
    # DFXCTL.RM_ID lives at 0x44A10000 + 0x10 == 0x44A10010.
    runner = FakeXsdbRunner({0x44A10010: 0x1})
    harness = SocketHarness(HOST, runner=runner)
    res = harness.probe("dfxctl")
    assert res.status == "ok"
    assert res.value == 0x1 or "1" in str(res.value)


def test_probe_csr_target_not_found_is_unreachable(monkeypatch) -> None:
    """A wrong-magic scan (no MicroBlaze answers with D1A6C0DE) surfaces as
    TargetNotFound, which the probe maps to 'unreachable' — never a silent 0."""
    harness = SocketHarness(HOST)

    class _Regs:
        def rm_id(self):
            raise TargetNotFound("no MicroBlaze had the magic at any candidate")

    monkeypatch.setattr(harness, "registers", lambda: _Regs())
    res = harness.probe("dfxctl")
    assert res.status == "unreachable"
