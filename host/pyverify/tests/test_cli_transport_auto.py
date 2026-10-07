"""``pyverify deploy``'s push transport is chosen from the RUNNING shell's
``version.features`` (board-manager handover B2).

A shell built ``HWICAP_FIFO=1 WINDOWED=1`` reports ``windowed`` and deadlocks
against a plain push; every silicon sweep used tcp + tcp + windowed. So:
``windowed`` in features => ``--pusher-transport tcp --src tcp --windowed``;
anything else (including a shell too old to answer ``version``) => the historic
tftp/plain default. Each explicit flag overrides its own part.

Both feature sets are served by a real :class:`FakeShell` over loopback; the
choice is read from what ``_select_transport`` returns and from the argv
``_build_pusher`` is handed when ``main()`` runs end to end.
"""
from __future__ import annotations

import argparse

import pytest

from pyverify import cli
from pyverify.client import ShellClient
from pyverify.testing.fakeshell import FakeShell

WINDOWED_FEATURES = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")


def _ns(**kw) -> argparse.Namespace:
    base = dict(pusher_transport=None, src=None, windowed=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def shell_factory():
    started = []

    def make(features):
        fake = FakeShell.ephemeral(features=features).start()
        started.append(fake)
        return fake

    yield make
    for f in started:
        f.stop()


def _select(fake, **kw):
    with ShellClient(fake.host, port=fake.control_port, timeout=5.0) as c:
        return cli._select_transport(c, _ns(**kw))


def test_windowed_shell_selects_tcp_tcp_windowed(shell_factory, capsys):
    assert _select(shell_factory(WINDOWED_FEATURES)) == ("tcp", "tcp", True)
    assert "version.features has 'windowed'" in capsys.readouterr().err


def test_plain_shell_keeps_the_tftp_default(shell_factory, capsys):
    assert _select(shell_factory(("clcd",))) == ("tftp", "tftp", False)
    assert "no 'windowed'" in capsys.readouterr().err


@pytest.mark.parametrize("kw,want", [
    ({"pusher_transport": "tftp"}, ("tftp", "tcp", False)),   # windowed needs tcp: dropped, and said so
    ({"src": "tftp"}, ("tcp", "tftp", True)),
    ({"windowed": False}, ("tcp", "tcp", False)),
])
def test_explicit_flags_override_their_own_part(shell_factory, kw, want):
    assert _select(shell_factory(WINDOWED_FEATURES), **kw) == want


def test_all_explicit_does_not_even_ask(shell_factory):
    class NoVersion:
        def version(self):
            raise AssertionError("must not probe when every flag is explicit")
    got = cli._select_transport(NoVersion(), _ns(pusher_transport="tcp", src="tcp", windowed=True))
    assert got == ("tcp", "tcp", True)


def test_a_shell_without_version_keeps_the_default(capsys):
    class Old:
        def version(self):
            raise OSError("unknown op 'version'")
    assert cli._select_transport(Old(), _ns()) == ("tftp", "tftp", False)
    assert "version unavailable" in capsys.readouterr().err


def test_main_deploy_builds_the_pusher_from_the_features(shell_factory, monkeypatch, tmp_path):
    """End to end through main(): the pusher is built AFTER the probe, with its
    result. The push itself is stubbed out (FakeShell models no window grants)."""
    seen = {}

    class Stop(Exception):
        pass

    def fake_build(host, transport, **kw):
        seen.update(transport=transport, windowed=kw.get("windowed"))
        raise Stop()

    monkeypatch.setattr(cli, "_build_pusher", fake_build)
    monkeypatch.setattr(cli, "_resolve_deploy_overlay", lambda args: object())
    fake = shell_factory(WINDOWED_FEATURES)
    with pytest.raises(Stop):
        cli.main(["deploy", "--host", fake.host, "--control-port", str(fake.control_port),
                  "--overlay", str(tmp_path)])
    assert seen == {"transport": "tcp", "windowed": True}


@pytest.mark.parametrize("features,want", [
    (WINDOWED_FEATURES, ("tcp", True, "tcp")),
    (("clcd",), ("tftp", False, "tftp")),
])
def test_mps3board_auto_transport_follows_the_features(shell_factory, features, want):
    from pyverify.board import Mps3Board
    fake = shell_factory(features)
    with Mps3Board(fake.host, control_port=fake.control_port) as board:
        assert board._pusher is None, "auto builds the pusher on first deploy, not before"
        p = board._auto_pusher()
        assert (p.transport, p.windowed, board._auto_src) == want


def test_mps3board_explicit_transport_is_untouched():
    from pyverify.board import Mps3Board
    b = Mps3Board("127.0.0.1", transport="tcp")
    assert b._pusher.transport == "tcp" and b._pusher.windowed is False
