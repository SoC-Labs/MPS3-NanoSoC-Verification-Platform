"""``pyverify usd format`` vs a single-client 6900 (IMAGE's first-install rehearsal).

6900 serves ONE client: a connection opened right after another one closed can be
accepted and closed UNANSWERED until the server notices the first has gone.
``usd format`` read the card state on one connection and formatted on a second
opened at once, with no retry, so it failed 2 of 4 runs. Now: the state read and
the format share ONE connection when there is nothing to ask, and every usd
request retries a connection closed unanswered (20 x 50 ms, like
``slot._one_line``).

:class:`_OneClientShell` answers with a real FakeShell's ``handle_control`` but
turns chosen connections away unanswered (accept, then close). NEGATIVE CONTROL:
the pre-fix single attempt (``cli._shell_read``) fails against the same fake.
"""
from __future__ import annotations

import argparse
import json
import socket
import threading

import pytest

from pyverify import cli as cli_mod
from pyverify.client import USD_CONFIRM_FORMAT
from pyverify.testing.fakeshell import FakeShell

STATIC_ID = 0x72BB0A36
RM_ID = 0x0100001E


class _OneClientShell:
    """A 6900 that closes the connections numbered in ``refuse`` (1-based)
    unanswered; every other connection is served line by line by ``fake``."""

    def __init__(self, fake: FakeShell, refuse=()):
        self.fake = fake
        self.refuse = set(refuse)
        self.conns: list[list[str]] = []       # the ops each connection carried
        self._ls = socket.create_server(("127.0.0.1", 0))
        self.port = self._ls.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self._ls.accept()
            except OSError:
                return
            self.conns.append([])
            n = len(self.conns)
            if n in self.refuse or "all" in self.refuse:
                c.close()                          # accept-then-EOF, unanswered
                continue
            threading.Thread(target=self._client, args=(c, self.conns[-1]), daemon=True).start()

    def _client(self, c, ops):
        with c, c.makefile("rb") as r:
            for line in r:
                req = json.loads(line)
                ops.append(" ".join(str(req.get(k)) for k in ("op", "action") if req.get(k)))
                c.sendall(json.dumps(self.fake.handle_control(req)).encode() + b"\n")

    def close(self):
        self._ls.close()


@pytest.fixture
def make_shell():
    made = []

    def make(refuse=(), **kw):
        fake = FakeShell(static_id=STATIC_ID, control_port=0, tftp_port=0, raw_tcp_port=0,
                         uart0_port=0, uart1_port=0, swo_port=0, **kw)
        srv = _OneClientShell(fake, refuse)
        made.append(srv)
        return fake, srv

    yield make
    for s in made:
        s.close()


def _argv(srv, *extra):
    return ["usd", "format", "--host", "127.0.0.1", "--control-port", str(srv.port),
            "--timeout", "2", *extra]


def test_no_default_checks_and_formats_on_one_connection(make_shell, capsys):
    fake, srv = make_shell(usd_card="blank")
    assert cli_mod.main(_argv(srv)) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "state": "empty"}
    assert srv.conns == [["usd", "usd format"]]          # ONE connection, both requests


def test_a_connection_turned_away_is_retried(make_shell, capsys):
    fake, srv = make_shell(refuse={1, 2}, usd_card="blank")
    assert cli_mod.main(_argv(srv)) == 0
    assert srv.conns == [[], [], ["usd", "usd format"]]


def test_the_format_after_a_typed_erase_survives_a_refused_connection(make_shell, capsys,
                                                                      monkeypatch):
    """The prompt path must close the state-read connection before a person decides,
    so the format IS a second connection -- the one the rehearsal lost."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")
    fake, srv = make_shell(refuse={2}, usd_default={"rm": "led", "rm_id": RM_ID})
    assert cli_mod.main(_argv(srv)) == 0
    assert srv.conns == [["usd"], [], ["usd format"]]
    assert not fake.usd_default_valid


def test_negative_control_one_attempt_fails_against_the_same_fake(make_shell, capsys):
    fake, srv = make_shell(refuse={1}, usd_card="blank")
    args = argparse.Namespace(host="127.0.0.1", control_port=srv.port, timeout=2.0)
    assert cli_mod._shell_read(args, lambda c: c.usd_format(USD_CONFIRM_FORMAT), "usd") is None
    err = capsys.readouterr().err                   # an EOF, or a RST if our request raced it
    assert "closed by peer" in err or "reset by peer" in err, err
    assert cli_mod._shell_call_retrying(
        args, lambda c: c.usd_format(USD_CONFIRM_FORMAT), "usd").ok


def test_a_channel_that_never_answers_gives_up_with_exit_3(make_shell, capsys):
    fake, srv = make_shell(refuse={"all"}, usd_card="blank")
    assert cli_mod.main(["usd", "status", "--host", "127.0.0.1", "--control-port",
                         str(srv.port), "--timeout", "2"]) == 3
    assert "kept closing the connection unanswered" in capsys.readouterr().err
    assert len(srv.conns) == cli_mod._REFUSED_ATTEMPTS


# --------------------------------------------------------------------------- #
# deploy: the soak's crash site (2026-09-27): a deploy (or any verb) opened right
# after the previous 6900 client closed is turned away until the shell reaps it.
# --------------------------------------------------------------------------- #


def _deploy_args(srv):
    return argparse.Namespace(host="127.0.0.1", control_port=srv.port, client_timeout=2.0)


def test_deploy_settles_its_connection_with_a_ping_retried_past_refusals(make_shell):
    fake, srv = make_shell(refuse={1, 2})
    client = cli_mod._deploy_client(_deploy_args(srv))
    try:
        assert client.ping().ok                     # the settled connection is ours
    finally:
        client.close()
    assert srv.conns == [[], [], ["ping", "ping"]]  # only the ping was ever re-sent


def test_deploy_gives_up_with_exit_3_when_6900_never_answers(make_shell, tmp_path, capsys,
                                                             monkeypatch):
    import zlib
    monkeypatch.setattr(cli_mod, "_REFUSED_GAP_S", 0.001)
    fake, srv = make_shell(refuse={"all"})
    clearing, partial = b"\x00\x01\x02\x03" * 8, b"\xde\xad\xbe\xef" * 8
    (tmp_path / "led_clear.bin").write_bytes(clearing)
    (tmp_path / "led.bin").write_bytes(partial)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema": 1, "static_id": "0x%08X" % STATIC_ID, "rm_id": "0x%08X" % RM_ID,
        "rm_name": "led",
        "clearing": {"file": "led_clear.bin", "len": len(clearing),
                     "crc32": "0x%08X" % zlib.crc32(clearing)},
        "partial": {"file": "led.bin", "len": len(partial),
                    "crc32": "0x%08X" % zlib.crc32(partial)},
    }))
    rc = cli_mod.main(["deploy", "--host", "127.0.0.1", "--overlay", str(tmp_path),
                       "--control-port", str(srv.port)])
    err = capsys.readouterr().err
    assert rc == 3, err
    assert "closed by peer" in err and "another client" in err, err
    assert "Traceback" not in err
    assert len(srv.conns) == cli_mod._REFUSED_ATTEMPTS
    assert all(c == [] for c in srv.conns)          # nothing was ever served
