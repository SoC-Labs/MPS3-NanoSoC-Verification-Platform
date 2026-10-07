"""pyverify.slot.SshTunnel vs an ssh ControlMaster (ILA-mint finding #20, SECURITY).

An ssh ControlMaster keeps a ``-L`` forward bound after the ``ssh -N`` that asked
for it has exited. A forward left bound to the Linux board's 127.0.0.1:6900/6910
passes the S12 claim lock for any process on this host. So the tunnel (1) never
uses a master (``-o ControlMaster=no -o ControlPath=none``), (2) refuses to start
when a local port is already bound, and (3) checks every local port is released
after ssh exits.

``fake_ssh_forward.py`` is the ssh: with ``$FAKE_SSH_MASTER_DIR`` set it behaves
like a user config with a ControlMaster -- unless the command line turns the
master off, the forwards go to a detached "master" that outlives the client.
The NEGATIVE CONTROL runs the pre-fix argv through that fake and shows the leak
is real and is caught.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

from pyverify import slot as pslot
from pyverify.localport import port_free
from pyverify.slot import SSH_FORWARD_OPTS, SshTunnel, SshTunnelError, ssh_tunnel_argv

HERE = Path(__file__).resolve().parent
FAKE_SSH = f"{sys.executable} {HERE / 'fake_ssh_forward.py'}"


class _Echo(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            d = self.request.recv(4096)
            if not d:
                return
            self.request.sendall(d)


@pytest.fixture
def board():
    """Two echo servers standing in for the board's 127.0.0.1:6900 / 6910."""
    srvs = [_Echo(("127.0.0.1", 0), _EchoHandler) for _ in range(2)]
    for s in srvs:
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield [s.server_address[1] for s in srvs]
    for s in srvs:
        s.shutdown()
        s.server_close()


@pytest.fixture
def ssh_env(tmp_path, monkeypatch):
    log = tmp_path / "ssh.log"
    master = tmp_path / "master"
    master.mkdir()
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FAKE_SSH_MASTER_DIR", str(master))     # the user's config HAS a master
    yield {"log": log, "master": master}
    pidfile = master / "master.pid"
    if pidfile.exists():                                        # never leave a "master" behind
        try:
            os.kill(int(pidfile.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def _calls(log: Path):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def _echo_through(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"ping\n")
        return s.recv(64)


def test_the_argv_never_uses_a_control_master():
    argv = ssh_tunnel_argv("mps3-linux", [(40001, 6900), (40002, 6910)], ssh="ssh")
    opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    for want in ("ControlMaster=no", "ControlPath=none", "ExitOnForwardFailure=yes",
                 "BatchMode=yes", "PasswordAuthentication=no"):
        assert want in opts, (want, argv)
    assert argv[-1] == "mps3-linux" and "-N" in argv
    assert argv.index("-N") < argv.index("-L")            # every option before the target
    assert SSH_FORWARD_OPTS == ("-o", "ControlMaster=no", "-o", "ControlPath=none",
                                "-o", "ExitOnForwardFailure=yes")


def test_a_tunnel_carries_traffic_and_releases_its_ports(board, ssh_env):
    t = SshTunnel("mps3-linux", board, ssh=FAKE_SSH, timeout_s=10)
    with t:
        lports = [t.local(p) for p in board]
        assert all(_echo_through(lp) == b"ping\n" for lp in lports)
    assert t.leaked == [] and all(port_free(p) for p in lports)
    (argv,) = _calls(ssh_env["log"])
    assert "ControlPath=none" in argv and "ControlMaster=no" in argv
    assert not (ssh_env["master"] / "master.pid").exists()    # the master was never used


def test_the_tunnel_is_up_only_once_every_forward_listens(board, ssh_env, monkeypatch):
    """The slot-lock e2e flake (2026-09-24): readiness probed the control forward
    alone, and under load ``slot push`` hit the 6910 forward before its listener
    existed (ECONNREFUSED). Here the fake binds the second forward 1.5 s late,
    so the pre-fix tunnel returns early -- and this echo is refused."""
    monkeypatch.setenv("FAKE_SSH_LATE", "%d=1.5" % board[1])
    t = SshTunnel("mps3-linux", board, ssh=FAKE_SSH, timeout_s=10)
    with t:
        assert _echo_through(t.local(board[1])) == b"ping\n"     # the late one, first
        assert _echo_through(t.local(board[0])) == b"ping\n"
    (argv,) = _calls(ssh_env["log"])
    fwd = [argv[i + 1] for i, a in enumerate(argv) if a == "-L"]
    assert fwd[-1].endswith(":127.0.0.1:%d" % board[0])        # the probed forward LAST


def test_a_forward_that_never_listens_fails_the_tunnel(board, ssh_env, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_LATE", "%d=30" % board[1])
    t = SshTunnel("mps3-linux", board, ssh=FAKE_SSH, timeout_s=1.5)
    with pytest.raises(SshTunnelError, match=r"not up after .*not listening"):
        t.__enter__()
    assert t.leaked == []                                      # ssh stopped, ports released


def test_negative_control_a_master_keeps_the_forward_and_close_catches_it(board, ssh_env,
                                                                          monkeypatch):
    """The pre-fix argv (no ControlMaster/ControlPath options) through a user
    config with a master: the forward OUTLIVES the ssh, exactly as on a dev box.
    The release check must see it."""
    monkeypatch.setattr(pslot, "SSH_FORWARD_OPTS", ("-o", "ExitOnForwardFailure=yes"))
    t = SshTunnel("mps3-linux", board, ssh=FAKE_SSH, timeout_s=10)
    t.RELEASE_S = 0.5
    with pytest.raises(SshTunnelError, match="still bound after the ssh tunnel"):
        with t:
            lport = t.local(board[0])
    assert lport in t.leaked
    assert _echo_through(lport) == b"ping\n"                   # still reaches the "board"
    assert (ssh_env["master"] / "master.pid").exists()


def test_a_port_already_bound_is_refused_before_ssh_runs(board, ssh_env):
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]
    try:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        other = free.getsockname()[1]
        free.close()
        t = SshTunnel("mps3-linux", board, ssh=FAKE_SSH, local_ports=(taken, other))
        with pytest.raises(SshTunnelError, match=r"local port\(s\) %d already bound" % taken):
            t.__enter__()
        assert _calls(ssh_env["log"]) == []                    # ssh never started
    finally:
        squatter.close()
    # and with the port released, the same pinned ports work
    with SshTunnel("mps3-linux", board, ssh=FAKE_SSH, local_ports=(taken, other),
                   timeout_s=10) as t:
        assert t.local(board[0]) == taken and _echo_through(taken) == b"ping\n"
    assert port_free(taken) and port_free(other)


def test_a_leak_does_not_mask_the_error_already_in_flight(board, ssh_env, monkeypatch):
    monkeypatch.setattr(pslot, "SSH_FORWARD_OPTS", ())
    t = SshTunnel("mps3-linux", board, ssh=FAKE_SSH, timeout_s=10)
    t.RELEASE_S = 0.3
    with pytest.raises(KeyError):
        with t:
            raise KeyError("the caller's own failure")
    assert t.leaked                                           # recorded, not raised


def test_local_ports_must_pair_with_remote_ports():
    with pytest.raises(ValueError):
        SshTunnel("mps3-linux", (6900, 6910), local_ports=(40001,))
