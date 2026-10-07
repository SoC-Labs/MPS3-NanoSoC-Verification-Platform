"""pyverify.linux + the Linux-harness CLI verbs: ssh / console / netboot /
mailbox / identify / csr-liveness. All board-free: argv builders are pure, the
console share is a fake hub runner plus a loopback TCP server, and the ssh
reads go through the reader's injectable ``run`` seam."""
from __future__ import annotations

import io
import json
import socket
import threading
from pathlib import Path

import pytest

from pyverify import cli
from pyverify.lease import RunResult
from pyverify.linux import (
    BOARD_IP, CONSOLE_TTY, ConsoleShare, LinuxHarnessError, _Stream,
    default_stage0_push_tool, parse_share_endpoint, read_for, send_paced,
    ssh_argv, ssh_config_stanza, stage0_push_argv,
)
from pyverify.testing.fakeshell import FakeShell


# --------------------------------------------------------------------------- #
# ssh
# --------------------------------------------------------------------------- #

def test_ssh_alias_is_key_only():
    argv = ssh_argv()
    assert argv[0] == "ssh" and argv[-1] == "mps3-linux"
    for opt in ("PreferredAuthentications=publickey", "PasswordAuthentication=no",
                "KbdInteractiveAuthentication=no"):
        assert opt in argv


def test_ssh_direct_needs_a_hub_and_bakes_none(monkeypatch):
    monkeypatch.delenv("MPS3_HUB", raising=False)
    monkeypatch.delenv("FPGAHUB_HOST", raising=False)
    with pytest.raises(LinuxHarnessError):
        ssh_argv(direct=True)
    monkeypatch.setenv("MPS3_HUB", "hub.example")
    argv = ssh_argv(direct=True, batch=True)
    assert argv[-3:] == ["-J", "hub.example", "root@%s" % BOARD_IP]
    assert "BatchMode=yes" in argv


def test_config_stanza_pins_the_host_key_apart():
    st = ssh_config_stanza("hub.example")
    assert "Host mps3-linux" in st and "ProxyJump hub.example" in st
    assert "PasswordAuthentication no" in st and "HostKeyAlias mps3-linux" in st
    assert "UserKnownHostsFile ~/.ssh/known_hosts_mps3_linux" in st


def test_cli_ssh_print_and_stanza(capsys, monkeypatch):
    assert cli.main(["ssh", "--print"]) == 0
    assert capsys.readouterr().out.strip().endswith("-t mps3-linux")
    assert cli.main(["ssh", "--print", "cat", "/etc/mps3/version"]) == 0
    out = capsys.readouterr().out
    assert "BatchMode=yes" in out and out.strip().endswith("'cat /etc/mps3/version'")
    monkeypatch.delenv("MPS3_HUB", raising=False)
    assert cli.main(["ssh", "--config-stanza"]) == 0
    assert "ProxyJump <hub-host>" in capsys.readouterr().out
    monkeypatch.delenv("FPGAHUB_HOST", raising=False)
    assert cli.main(["ssh", "--direct", "--print"]) == 3


# --------------------------------------------------------------------------- #
# console share
# --------------------------------------------------------------------------- #

def test_parse_share_endpoint_start_and_list_shapes():
    assert parse_share_endpoint("share /dev/mps3_pl/tty_02 → 127.0.0.1:7002\n") == ("127.0.0.1", 7002)
    table = ("│ /dev/mps3_pl/tty_00 │ 127.0.0.1:7000 │ - │ 0 │ yes │\n"
             "│ /dev/mps3_pl/tty_02 │ 127.0.0.1:7002 │ - │ 1 │ yes │\n")
    assert parse_share_endpoint(table) == ("127.0.0.1", 7002)


def test_parse_share_endpoint_never_takes_another_lane():
    with pytest.raises(LinuxHarnessError):
        parse_share_endpoint("share /dev/mps3_pl/tty_00 → 127.0.0.1:7000\n")


class _Hub:
    """A fake hub runner for `fpgahub share list|start`."""

    def __init__(self, listed="", started=None, rc=0):
        self.calls = []
        self.listed, self.started, self.rc = listed, started, rc

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        if argv[:3] == ["fpgahub", "share", "list"]:
            return RunResult(0, self.listed, "")
        return RunResult(self.rc, self.started or "", "")


def test_console_share_reuses_an_existing_share():
    hub = _Hub(listed="share %s → 127.0.0.1:7002\n" % CONSOLE_TTY)
    assert ConsoleShare(runner=hub).endpoint() == ("127.0.0.1", 7002)
    assert len(hub.calls) == 1


def test_console_share_starts_one_at_115200():
    hub = _Hub(listed="no active shares", started="share %s → 127.0.0.1:7010\n" % CONSOLE_TTY)
    assert ConsoleShare(runner=hub).endpoint() == ("127.0.0.1", 7010)
    assert hub.calls[1] == ["fpgahub", "share", "start", "mps3_pl", CONSOLE_TTY,
                            "--baud", "115200"]


def test_console_share_start_failure_is_loud():
    with pytest.raises(LinuxHarnessError):
        ConsoleShare(runner=_Hub(listed="", started="HTTP 403", rc=1)).endpoint()


def test_attach_goes_through_the_hub_unless_on_it(monkeypatch):
    monkeypatch.setenv("MPS3_HUB", "hub.example")
    monkeypatch.delenv("MPS3_ON_HUB", raising=False)
    argv = ConsoleShare(runner=_Hub()).attach_argv("127.0.0.1", 7002)
    assert argv[-3:] == ["-W", "127.0.0.1:7002", "hub.example"]
    monkeypatch.setenv("MPS3_ON_HUB", "1")
    assert ConsoleShare(runner=_Hub()).attach_argv("127.0.0.1", 7002) is None


def _echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = bytearray()

    def run():
        conn, _ = srv.accept()
        conn.sendall(b"login: ")
        conn.settimeout(3)
        try:
            while True:
                d = conn.recv(64)
                if not d:
                    break
                got.extend(d)
                if d.endswith(b"\r"):
                    conn.sendall(b"\r\n# ")
        except socket.timeout:
            pass
        conn.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], got, t


def test_paced_send_and_read_over_a_direct_stream():
    port, got, t = _echo_server()
    stream = _Stream("127.0.0.1", port, None)
    sleeps = []
    try:
        assert send_paced(stream, b"root\r", 0.02, sleep=sleeps.append) == 5
        out = io.BytesIO()
        text = read_for(stream, 0.5, out)
    finally:
        stream.close()
    t.join(5)
    assert bytes(got) == b"root\r"
    assert sleeps == [0.02] * 5                  # one pause per BYTE
    assert b"login: " in text and out.getvalue() == text


def test_cli_console_send(monkeypatch, capsysbinary):
    port, got, t = _echo_server()
    monkeypatch.setenv("MPS3_ON_HUB", "1")
    hub = _Hub(listed="share %s → 127.0.0.1:%d\n" % (CONSOLE_TTY, port))
    rc = cli.main(["console", "--send", "uname", "--seconds", "0.5", "--pace-ms", "1"],
                  runner=hub)
    t.join(5)
    assert rc == 0 and bytes(got) == b"uname\r"
    assert b"login: " in capsysbinary.readouterr().out


def test_cli_console_print(monkeypatch, capsys):
    monkeypatch.setenv("MPS3_HUB", "hub.example")
    monkeypatch.delenv("MPS3_ON_HUB", raising=False)
    hub = _Hub(listed="share %s → 127.0.0.1:7002\n" % CONSOLE_TTY)
    assert cli.main(["console", "--print"], runner=hub) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["share"] == "127.0.0.1:7002" and "-W 127.0.0.1:7002 hub.example" in out["attach"]


# --------------------------------------------------------------------------- #
# netboot
# --------------------------------------------------------------------------- #

def test_stage0_push_argv_local_and_via_hub(tmp_path):
    tool = tmp_path / "stage0_push.py"
    local = stage0_push_argv("blob.img", tool=tool, extra=["--blksize", "1468"])
    assert local == [["python3", str(tool), "--blksize", "1468", BOARD_IP, "blob.img"]]
    hub = stage0_push_argv(str(tmp_path / "blob.img"), tool=tool, via_hub="hub.example")
    assert [c[0] for c in hub] == ["ssh", "scp", "ssh"]
    assert hub[-1][-1].endswith("/tmp/pyverify_netboot/blob.img")


def test_default_push_tool_path_names_stage0s_file(monkeypatch):
    monkeypatch.delenv("MPS3_STAGE0_PUSH", raising=False)
    tool = default_stage0_push_tool()
    assert tool.name == "stage0_push.py" and tool.parent.name == "fw_stage0"
    monkeypatch.setenv("MPS3_STAGE0_PUSH", "/x/y.py")
    assert default_stage0_push_tool() == Path("/x/y.py")


def test_cli_netboot_dry_run_and_missing_inputs(tmp_path, capsys):
    blob = tmp_path / "b.img"
    assert cli.main(["netboot", str(blob), "--dry-run", "--tool", str(tmp_path / "t.py")]) == 0
    assert "192.168.10.101 " in capsys.readouterr().err
    # not a dry run: a missing blob / a missing tool is refused BEFORE any push
    assert cli.main(["netboot", str(blob), "--tool", str(tmp_path / "t.py")]) == 2
    blob.write_bytes(b"x")
    assert cli.main(["netboot", str(blob), "--tool", str(tmp_path / "t.py")]) == 2
    assert "push tool" in capsys.readouterr().err


def test_cli_netboot_runs_the_tool(tmp_path):
    blob = tmp_path / "b.img"
    blob.write_bytes(b"x")
    tool = tmp_path / "t.py"
    marker = tmp_path / "ran"
    tool.write_text("import sys; open(%r, 'w').write(' '.join(sys.argv[1:]))\n" % str(marker))
    import sys
    assert cli.main(["netboot", str(blob), "--tool", str(tool), "--python", sys.executable,
                     "--push-arg=--blksize", "--push-arg", "1468"]) == 0
    assert marker.read_text() == "--blksize 1468 192.168.10.101 %s" % blob


@pytest.mark.parametrize("tool_rc,cli_rc", [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)])
def test_cli_netboot_preserves_stage0_push_exit_codes(tmp_path, tool_rc, cli_rc, capsys):
    """STAGE0_CONTRACT §9 owns these codes; netboot passes them through."""
    import sys
    blob = tmp_path / "b.img"
    blob.write_bytes(b"x")
    tool = tmp_path / "t.py"
    tool.write_text("raise SystemExit(%d)\n" % tool_rc)
    assert cli.main(["netboot", str(blob), "--tool", str(tool), "--python", sys.executable]) == cli_rc
    assert "netboot:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# identify CLI
# --------------------------------------------------------------------------- #

def test_cli_identify_against_the_linux_fake(capsys):
    f = FakeShell.ephemeral(profile="linux").start()
    try:
        assert cli.main(["identify", "--host", f.host, "--port", str(f.identify_port)]) == 0
        assert json.loads(capsys.readouterr().out)["impl"] == "linux"
    finally:
        f.stop()


def test_cli_identify_silence_is_exit_3(capsys):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))            # bound, never answers
    port = s.getsockname()[1]
    try:
        assert cli.main(["identify", "--host", "127.0.0.1", "--port", str(port),
                         "--timeout", "0.2"]) == 3
    finally:
        s.close()


def test_cli_version_prints_impl(capsys):
    f = FakeShell.ephemeral(profile="linux").start()
    try:
        assert cli.main(["version", "--host", f.host, "--control-port", str(f.control_port)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["impl"] == "linux" and out["lmb_kb"] == 128
    finally:
        f.stop()


# --------------------------------------------------------------------------- #
# mailbox / csr-liveness CLI over a fake ssh+devmem
# --------------------------------------------------------------------------- #

def _fake_devmem(monkeypatch, mem, writable=True):
    import re
    from pyverify import mailbox as mbx
    calls = []

    def run(self, argv):
        calls.append(argv)
        cmd = argv[-1]
        if cmd.startswith("for a in"):
            # the batched read: every loop's words, then the end marker (this
            # fake never fires the stop guard -- it just reads everything)
            addrs = [a for grp in re.findall(r"for a in (.*?); do", cmd) for a in grp.split()]
            end = re.search(r"echo (@MBX-END \d+)", cmd)
            return 0, ("".join("0x%08X\n" % mem.read_words(int(a, 16), 1)[0] for a in addrs)
                       + (end.group(1) + "\n" if end else "")), ""
        m = re.search(r"devmem (0x[0-9A-F]+) 32 (0x[0-9A-F]+)", cmd)
        addr, pat = int(m.group(1), 16), int(m.group(2), 16)
        return 0, "0x%08X\n" % (pat if writable else 0), ""

    monkeypatch.setattr(mbx.SshDevmemReader, "_subprocess_run", run)
    return calls


def test_cli_mailbox_reads_both_blocks(monkeypatch, capsys):
    from pyverify.mailbox import (DIAG_MAGIC, STAGE0_STATUS_FIELDS, STAGE0_STATUS_MAGIC,
                                  FakeMemory)
    mem = FakeMemory(alias_kb=128)
    mem.poke(0x1FF00, [DIAG_MAGIC, 8] + [7] * 62)
    offs = dict(STAGE0_STATUS_FIELDS)
    blk = [0] * 64
    for k, v in {"magic": STAGE0_STATUS_MAGIC, "version": 1, "size": 0x100, "phase": 6,
                 "magic_end": STAGE0_STATUS_MAGIC}.items():
        blk[offs[k] // 4] = v
    mem.poke(0x1FE00, blk)
    calls = _fake_devmem(monkeypatch, mem)
    assert cli.main(["mailbox"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["diag"]["base"] == "0x1FF00" and out["diag"]["lmb_kb"] == 128
    assert out["stage0"]["valid"] and out["stage0"]["phase"] == "HANDOFF"
    assert all("BatchMode=yes" in c for c in calls)


def test_cli_mailbox_no_mailbox_is_exit_1(monkeypatch, capsys):
    from pyverify.mailbox import FakeMemory
    _fake_devmem(monkeypatch, FakeMemory())
    assert cli.main(["mailbox", "--what", "diag", "--lmb-kb", "128"]) == 1
    assert "no diag mailbox" in capsys.readouterr().err


def test_cli_csr_liveness_pass_and_dead_decode(monkeypatch, capsys):
    from pyverify.mailbox import FakeMemory
    _fake_devmem(monkeypatch, FakeMemory(), writable=True)
    assert cli.main(["csr-liveness"]) == 0
    assert "decode is live" in capsys.readouterr().out
    _fake_devmem(monkeypatch, FakeMemory(), writable=False)     # bug #1: every read 0
    assert cli.main(["csr-liveness"]) == 1
    assert "CSR decode DEAD" in capsys.readouterr().out


def test_cli_netboot_status_needs_no_blob(tmp_path, capsys):
    tool = tmp_path / "stage0_push.py"
    assert cli.main(["netboot", "--status", "--dry-run", "--tool", str(tool)]) == 0
    err = capsys.readouterr().err
    assert "--status 192.168.10.101" in err and ".img" not in err
    assert cli.main(["netboot", "--dry-run", "--tool", str(tool)]) == 2


def test_via_hub_copies_the_tools_siblings(tmp_path):
    tool = tmp_path / "stage0_push.py"
    for n in ("stage0_push.py", "stage0_pack.py", "stage0_status.py"):
        (tmp_path / n).write_text("")
    cmds = stage0_push_argv("b.img", tool=tool, via_hub="hub")
    scp = cmds[1]
    assert str(tmp_path / "stage0_pack.py") in scp and str(tmp_path / "stage0_status.py") in scp


def test_netboot_drives_the_real_stage0_push_tool_to_its_not_stage0_verdict(tmp_path):
    """End to end with STAGE0's REAL tool against a port nothing answers on:
    exit 3 (not stage0 / unreachable), passed through unchanged."""
    import sys
    tool = default_stage0_push_tool()
    assert tool.is_file(), "STAGE0's stage0_push.py is missing"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        rc = cli.main(["netboot", "--status", "--host", "127.0.0.1", "--tool", str(tool),
                       "--python", sys.executable, "--push-arg=--no-ping",
                       "--push-arg=--port", "--push-arg", str(port),
                       "--push-arg=--timeout", "--push-arg", "0.3",
                       "--push-arg=--retries", "--push-arg", "1"])
    finally:
        s.close()
    assert rc == 3


# --------------------------------------------------------------------------- #
# claim (TOFU) against the linux FakeShell
# --------------------------------------------------------------------------- #

def test_cli_claim_once_then_refused(tmp_path, capsys):
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample me@pc\n")
    f = FakeShell.ephemeral(profile="linux").start()
    try:
        assert cli.main(["claim", "--host", f.host, "--port", str(f.tftp_port),
                         "--key", str(key)]) == 0
        assert f.ssh_claimed and f.authorized_keys == key.read_bytes()
        assert cli.main(["claim", "--host", f.host, "--port", str(f.tftp_port),
                         "--key", str(key)]) == 1
        assert "already claimed" in capsys.readouterr().err
    finally:
        f.stop()


def test_cli_claim_refuses_a_private_key(tmp_path):
    key = tmp_path / "id"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    assert cli.main(["claim", "--key", str(key)]) == 2
