"""Tests for the CLCD-KVM remote panel flip (net-protocol.md ``display``):
the ``ShellClient.display``/``display_owner`` verbs, the ``Mps3Board`` facade
methods, the ``pyverify display`` CLI subcommand, the ``FakeShell`` reference
model, and the ``pyverify.display_http`` HTTP shim.

No hardware, no board. The client layer uses an in-memory fake ``Transport``
(like ``test_client.py``); the facade uses an injected fake client (like
``test_board.py``); the CLI and the HTTP shim run against a real, ephemeral
``FakeShell`` on localhost.
"""
from __future__ import annotations

import json
import urllib.request
from collections import deque

import pytest

from pyverify.board import Mps3Board
from pyverify.client import (
    DISPLAY_OWNERS,
    DisplayResponse,
    ShellClient,
    validate_display_owner,
)
from pyverify.testing.fakeshell import FakeShell


# --------------------------------------------------------------------------- #
# ShellClient.display / display_owner — wire shape + parsing (fake transport)
# --------------------------------------------------------------------------- #


class FakeTransport:
    """Scripted transport (mirrors test_client.py): replies from a queue,
    records every request line sent."""

    def __init__(self, responses):
        self._responses = deque(responses)
        self.sent = []
        self.closed = False

    def send_line(self, payload):
        self.sent.append(payload)

    def recv_line(self):
        if not self._responses:
            raise AssertionError("FakeTransport: no more scripted responses")
        return json.dumps(self._responses.popleft()).encode("ascii")

    def close(self):
        self.closed = True


def _client(responses):
    transport = FakeTransport(responses)
    client = ShellClient("shell.example", transport=transport)
    client.connect()
    return client, transport


def test_display_dut_round_trip():
    client, transport = _client([{"ok": True, "owner": "dut"}])
    resp = client.display("dut")
    assert resp == DisplayResponse(ok=True, owner="dut", err="")
    assert json.loads(transport.sent[0]) == {"op": "display", "owner": "dut"}


@pytest.mark.parametrize("owner", DISPLAY_OWNERS)
def test_display_sends_each_owner_verbatim(owner):
    client, transport = _client([{"ok": True, "owner": "harness"}])
    client.display(owner)
    assert json.loads(transport.sent[0]) == {"op": "display", "owner": owner}


def test_display_owner_query_is_read_only_wire():
    client, transport = _client([{"ok": True, "owner": "harness"}])
    resp = client.display_owner()
    assert resp.ok and resp.owner == "harness"
    # The query is the read-only "query" owner value on the wire.
    assert json.loads(transport.sent[0]) == {"op": "display", "owner": "query"}


def test_display_off_build_failure_is_parsed():
    """A board with no KVM slave answers the uniform failure line; the client
    surfaces it as ok=False + err, owner empty (keys on ok)."""
    client, _ = _client([{"ok": False, "err": "clcd_kvm not present"}])
    resp = client.display("dut")
    assert resp.ok is False
    assert resp.err == "clcd_kvm not present"
    assert resp.owner == ""


def test_display_validation_opt_in_default_off():
    """ShellClient.display is wire-transparent by default (sends anything, so
    conformance suites can prove the server rejects it), but validates when
    asked."""
    client, transport = _client([{"ok": False, "err": "bad owner"}])
    # default: no client-side check — the bad owner reaches the wire
    client.display("banana", owners=None)
    assert json.loads(transport.sent[0]) == {"op": "display", "owner": "banana"}
    # opt-in: raises before any traffic
    with pytest.raises(ValueError):
        client.display("banana", owners=DISPLAY_OWNERS)


def test_validate_display_owner_rejects_query():
    """The read-only "query" value is NOT a valid flip target (display_owner
    sends it on its own path)."""
    validate_display_owner("dut")
    with pytest.raises(ValueError):
        validate_display_owner("query")


# --------------------------------------------------------------------------- #
# Mps3Board facade — validates by default, passes through to the client
# --------------------------------------------------------------------------- #


class FakeDisplayClient:
    """Duck-typed ShellClient recording display calls."""

    def __init__(self):
        self.connected = False
        self.closed = False
        self.calls = []
        self._owner = "harness"

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.closed = True

    def display(self, owner):
        self.calls.append(("display", owner))
        if owner == "toggle":
            self._owner = "dut" if self._owner == "harness" else "harness"
        else:
            self._owner = owner
        return DisplayResponse(ok=True, owner=self._owner)

    def display_owner(self):
        self.calls.append(("display_owner",))
        return DisplayResponse(ok=True, owner=self._owner)


def _board():
    client = FakeDisplayClient()
    board = Mps3Board("shell.example", client=client)
    board.connect()
    return board, client


def test_board_set_display_owner_passthrough():
    board, client = _board()
    assert board.set_display_owner("dut").owner == "dut"
    assert board.display_owner().owner == "dut"
    assert ("display", "dut") in client.calls
    assert ("display_owner",) in client.calls


def test_board_set_display_owner_validates_by_default():
    board, _ = _board()
    with pytest.raises(ValueError):
        board.set_display_owner("banana")
    # query is not a valid *flip* target through set_display_owner either
    with pytest.raises(ValueError):
        board.set_display_owner("query")


# --------------------------------------------------------------------------- #
# FakeShell reference model — the executable spec of the display verb
# --------------------------------------------------------------------------- #


def test_fakeshell_display_flip_and_query():
    fake = FakeShell(boot_display_owner="harness")
    assert fake.handle_control({"op": "display", "owner": "dut"}) == {
        "ok": True, "owner": "dut",
    }
    # query reports committed owner, moves nothing
    assert fake.handle_control({"op": "display", "owner": "query"}) == {
        "ok": True, "owner": "dut",
    }
    assert fake.display_requests == ["dut"]  # query is not recorded as a request


def test_fakeshell_display_toggle():
    fake = FakeShell(boot_display_owner="harness")
    assert fake.handle_control({"op": "display", "owner": "toggle"})["owner"] == "dut"
    assert fake.handle_control({"op": "display", "owner": "toggle"})["owner"] == "harness"


def test_fakeshell_display_bad_owner():
    fake = FakeShell()
    assert fake.handle_control({"op": "display", "owner": "banana"}) == {
        "ok": False, "err": "bad owner",
    }


def test_fakeshell_display_off_build():
    """has_clcd_kvm=False models today's board (no 0x44AD slave): the verb
    declines exactly like coordinator_handle_display()'s OFF-build path."""
    fake = FakeShell(has_clcd_kvm=False)
    assert fake.handle_control({"op": "display", "owner": "dut"}) == {
        "ok": False, "err": "clcd_kvm not present",
    }


# --------------------------------------------------------------------------- #
# CLI `pyverify display` — parser + end-to-end against an ephemeral FakeShell
# --------------------------------------------------------------------------- #


def test_cli_display_parser_defaults():
    from pyverify.cli import build_parser

    args = build_parser().parse_args(["display", "--host", "192.168.10.101", "dut"])
    assert args.verb == "display"
    assert args.host == "192.168.10.101"
    assert args.owner == "dut"
    assert args.control_port == 6900


def test_cli_display_parser_query_omits_owner():
    from pyverify.cli import build_parser

    args = build_parser().parse_args(["display", "--host", "10.0.0.5"])
    assert args.owner is None


def test_cli_display_flip_and_query_against_fakeshell(capsys):
    from pyverify.cli import main

    with FakeShell.ephemeral(boot_display_owner="harness") as fake:
        port = fake.control_port
        rc = main(["display", "--host", "127.0.0.1", "--control-port", str(port), "dut"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == {
            "ok": True, "owner": "dut", "err": "",
        }
        # query (owner omitted)
        rc = main(["display", "--host", "127.0.0.1", "--control-port", str(port)])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["owner"] == "dut"


def test_cli_display_off_build_is_exit_1(capsys):
    from pyverify.cli import main

    with FakeShell.ephemeral(has_clcd_kvm=False) as fake:
        rc = main(["display", "--host", "127.0.0.1",
                   "--control-port", str(fake.control_port), "dut"])
    assert rc == 1  # shell declined the verb (ok:false), clean exit
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["err"] == "clcd_kvm not present"


def test_cli_display_unreachable_is_exit_3(capsys):
    from pyverify.cli import main

    # A closed localhost port => connection refused => exit 3, clean message.
    rc = main(["display", "--host", "127.0.0.1", "--control-port", "9", "dut"])
    assert rc == 3
    assert "cannot reach shell control channel" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# display_http — the HTTP shim (pure routing core + a real ephemeral server)
# --------------------------------------------------------------------------- #


class _FakeCtxClient:
    """Context-manager client for the routing-core seam."""

    def __init__(self, owner="harness"):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def display(self, owner):
        self._owner = "dut" if owner == "toggle" and self._owner == "harness" else (
            "harness" if owner == "toggle" else owner)
        return DisplayResponse(ok=True, owner=self._owner)

    def display_owner(self):
        return DisplayResponse(ok=True, owner=self._owner)


def test_http_route_get_is_query():
    from pyverify.display_http import handle_display

    status, body = handle_display("GET", "/display", lambda: _FakeCtxClient("dut"))
    assert status == 200
    assert body == {"ok": True, "owner": "dut", "err": ""}


def test_http_route_post_flip():
    from pyverify.display_http import handle_display

    status, body = handle_display("POST", "/display/dut", lambda: _FakeCtxClient("harness"))
    assert status == 200
    assert body["owner"] == "dut"


def test_http_route_bad_owner_is_400():
    from pyverify.display_http import handle_display

    status, body = handle_display("POST", "/display/banana", lambda: _FakeCtxClient())
    assert status == 400
    assert body["ok"] is False


def test_http_route_unknown_path_is_404():
    from pyverify.display_http import handle_display

    status, body = handle_display("GET", "/nope", lambda: _FakeCtxClient())
    assert status == 404
    assert body["ok"] is False


def test_http_route_unreachable_is_502():
    from pyverify.display_http import handle_display

    def boom():
        raise ConnectionRefusedError("connection refused")

    status, body = handle_display("GET", "/display", boom)
    assert status == 502
    assert body["ok"] is False and "unreachable" in body["err"]


def test_http_shim_end_to_end_against_fakeshell():
    """serve() builds a real ShellClient factory; point it at an ephemeral
    FakeShell and drive the shim over real HTTP."""
    import threading

    from pyverify.display_http import serve

    with FakeShell.ephemeral(boot_display_owner="harness") as fake:
        server = serve("127.0.0.1", shell_port=fake.control_port,
                       listen_host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            # flip to DUT
            req = urllib.request.Request(base + "/display/dut", method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                assert json.loads(r.read())["owner"] == "dut"
            # query it back
            with urllib.request.urlopen(base + "/display", timeout=5) as r:
                assert json.loads(r.read()) == {"ok": True, "owner": "dut", "err": ""}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
