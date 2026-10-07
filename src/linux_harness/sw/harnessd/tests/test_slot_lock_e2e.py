"""test_slot_lock_e2e.py — THE LOCK on slot mutations (David, 2026-09-24;
net-protocol.md "Slot images" / "The lock"), end to end over real sockets against
mps3-harnessd (host build, MOCK fabric) and a file-backed card.

Once the board's SSH is CLAIMED, a slot-image push (6910 / TFTP), `commit` and
`rollback` are refused from any peer that is not the board itself; `status` and
`verify` stay open; the board's own peer (an ssh tunnel's far end) is allowed; an
unclaimed board is open. The claim state is the one identify reports.

On a host every peer is 127.x, so the harness runs with --mock-trusted-peer
127.0.0.3: that ONE address stands for "the board itself" (a MOCK-build-only
option), 127.0.0.1 is a remote peer, and the local-peer cases connect from
127.0.0.3. Negative controls sit beside every refusal: the same request from the
trusted peer, or with the board unclaimed, goes through.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_slot_e2e import (  # noqa: E402
    BIN, BLK, LBA_B, NBLK, REPO, SID, Board, _frame, hdr_crc, make_card, make_image, read_slot,
)
sys.path.insert(0, str(REPO / "host" / "pyverify"))
from pyverify import slot as pslot  # noqa: E402
from pyverify.identify import identify  # noqa: E402
from pyverify.pusher import PushError, tftp_put  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")

TRUSTED = "127.0.0.3"          # "the board itself" for this harness
REMOTE = "127.0.0.1"
LOCKED = {"ok": False, "err": "slot locked: board claimed (use ssh)", "code": "locked"}
KEY = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J me@pc\n"


def _loopback_alias_works() -> bool:
    try:
        with socket.socket() as s:
            s.bind((TRUSTED, 0))
        return True
    except OSError:
        return False


if not _loopback_alias_works():
    pytestmark = pytest.mark.skip(reason=f"cannot bind {TRUSTED} (no 127/8 loopback routing)")


# --------------------------------------------------------------------------- #
# a board, and clients that choose where they come from
# --------------------------------------------------------------------------- #

@pytest.fixture
def imgs(tmp_path):
    return {"a": make_image(tmp_path, "a", 3000, 1), "b": make_image(tmp_path, "b", 5000, 2),
            "c": make_image(tmp_path, "c", 7001, 3), "dir": tmp_path}


@pytest.fixture
def board(tmp_path, imgs):
    card = make_card(tmp_path, tmp_path / "a.img")
    b = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]),
              extra=["--mock-trusted-peer", TRUSTED])
    b.card = card
    b.ak = b.hd.dir / "ssh" / "authorized_keys"
    yield b
    b.stop()


def req_from(board, src, obj):
    """One 6900 request from `src` (retrying the single-client accept-then-EOF)."""
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", board.ctl), timeout=10,
                                          source_address=(src, 0)) as s:
                s.sendall(json.dumps(obj).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    d = s.recv(4096)
                    if not d:
                        break
                    buf += d
            if buf:
                return json.loads(buf)
        except OSError:
            pass
        time.sleep(0.05)
    raise ConnectionError("6900 kept refusing")


def slot_from(board, src, act, slot=None):
    obj = {"op": "slot", "act": act}
    if slot:
        obj["slot"] = slot
    return req_from(board, src, obj)


def push_from(board, src, img):
    """A LOCKED push is refused at the header and closed with the rest unread: the
    RST may beat the send or the half-close (test_slot_e2e.Board.push_raw). The
    verdict is the slot status the tests read next; only the connect must work."""
    with socket.create_connection(("127.0.0.1", board.hd.port(6910)), timeout=10,
                                  source_address=(src, 0)) as s:
        try:
            s.sendall(_frame(img))
            s.shutdown(socket.SHUT_WR)
            while s.recv(4096):
                pass
        except OSError:
            pass


def wait_job(board, src=REMOTE, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = slot_from(board, src, "status")
        if st["job"]["state"] not in ("writing", "verifying"):
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def claim(board):
    tftp_put(KEY, "127.0.0.1", board.hd.port(69), filename="authorized_keys")
    assert board.ak.is_file()


def claimed(board):
    return identify("127.0.0.1", board.hd.port(6899)).ssh_claimed


def slot_b_hash(board):
    return hashlib.sha256(read_slot(board.card, LBA_B, NBLK * BLK)).hexdigest()


# --------------------------------------------------------------------------- #
# the rule
# --------------------------------------------------------------------------- #

def test_unclaimed_board_is_open_to_a_remote_peer(board, imgs):
    assert claimed(board) is False
    push_from(board, REMOTE, imgs["b"])
    st = wait_job(board)
    assert st["job"]["state"] == "ok" and st["staged"] == "B"
    assert slot_from(board, REMOTE, "commit")["default"] == "B"
    assert slot_from(board, REMOTE, "rollback")["default"] == "A"
    assert "LOCKED" not in board.hd.log()


def test_claimed_board_refuses_remote_mutations_and_serves_reads(board, imgs):
    push_from(board, REMOTE, imgs["b"])                  # staged while still open
    before = wait_job(board)
    assert before["staged"] == "B"
    claim(board)                                         # the claim itself: TFTP, remote, TOFU
    assert claimed(board) is True
    b_before = slot_b_hash(board)

    assert slot_from(board, REMOTE, "commit") == LOCKED
    assert slot_from(board, REMOTE, "rollback") == LOCKED
    assert slot_from(board, REMOTE, "commit", "B") == LOCKED   # the lock comes before the guard

    push_from(board, REMOTE, imgs["c"])                  # refused before the provider:
    st = slot_from(board, REMOTE, "status")              # status stays open ...
    assert st["ok"] is True and st["job"] == before["job"]      # ... and no state moved
    assert st["staged"] == "B" and slot_b_hash(board) == b_before
    with pytest.raises(PushError, match="access violation"):   # TFTP: error 2
        tftp_put(_frame(imgs["c"]), "127.0.0.1", board.hd.port(69), filename="linux_slot.img")
    assert slot_b_hash(board) == b_before

    v = slot_from(board, REMOTE, "verify", "B")          # verify only reads: open
    assert v["ok"] is True and v["job"]["act"] == "verify"
    assert wait_job(board)["job"]["state"] == "ok"

    log = board.hd.log()
    for what in ("commit", "rollback", "a slot-image push"):
        assert f"slot: LOCKED -- {what} from 127.0.0.1:" in log, what


def test_claimed_board_lets_the_board_itself_mutate(board, imgs):
    claim(board)
    assert slot_from(board, REMOTE, "commit") == LOCKED            # the control ...
    push_from(board, TRUSTED, imgs["b"])                           # ... and the same from
    st = wait_job(board, src=TRUSTED)                              #     the local peer
    assert st["job"]["state"] == "ok" and st["staged"] == "B"
    assert slot_from(board, TRUSTED, "commit")["default"] == "B"
    assert slot_from(board, TRUSTED, "rollback")["default"] == "A"


def test_claim_and_lock_never_disagree_and_flip_without_a_restart(board, imgs):
    """identify.ssh.claimed, `slot status`'s `claimed` (HM_ANSWERS S2: what a
    client behind an ssh -L tunnel can see -- identify is UDP) and the lock read
    the SAME source; through claim, unclaim and claim again (no harnessd restart)
    they always agree."""
    pid = board.hd.proc.pid
    steps = [("start", None), ("claim", claim), ("unclaim", lambda b: b.ak.unlink()),
             ("claim again", claim)]
    for name, act in steps:
        if act:
            act(board)
        c = claimed(board)
        assert slot_from(board, REMOTE, "status")["claimed"] is c, name
        assert slot_from(board, TRUSTED, "status")["claimed"] is c, name   # from any peer
        r = slot_from(board, REMOTE, "rollback")
        assert (r == LOCKED) == bool(c), f"{name}: identify says claimed={c}, rollback said {r}"
        assert r == LOCKED or r["err"] == "slot B is not a valid image", r   # unlocked: the next rule
    assert board.hd.proc.pid == pid and board.hd.proc.poll() is None


def test_claimed_board_locks_the_store_mutations_too(board, imgs):
    """HM_ANSWERS S6 (David 2026-09-26: YES): `usd` format/clear/rescan and the D13
    re-push `commit` take the SAME lock -- a remote wipe of the persisted overlays
    was the denial of service. `usd` status stays open; the board itself goes
    through; an unclaimed board is open (the controls)."""
    commit = {"op": "commit", "rm": "led", "src": "tcp", "rm_id": "0x00000000",
              "static_id": f"0x{SID:08x}", "clear_len": 64, "clear_crc": "0x00000000",
              "part_len": 128, "part_crc": "0x00000000"}
    usd_locked = {"ok": False, "err": "usd locked: board claimed (use ssh)", "code": "locked"}
    deadline = time.time() + 10                                 # the store's power-on probe
    while time.time() < deadline and \
            req_from(board, REMOTE, {"op": "usd"}).get("state") in ("init", None):
        time.sleep(0.1)
    # unclaimed: open to the remote peer (rescan answers; the commit meets the next rule)
    assert req_from(board, REMOTE, {"op": "usd", "action": "rescan"})["ok"] is True
    r = req_from(board, REMOTE, commit)
    assert "locked" not in r.get("err", ""), r
    claim(board)
    p4_before = hashlib.sha256(read_slot(board.card, 2048, 65536 * BLK)).hexdigest()
    for act in ({"action": "format", "confirm": "erase"}, {"action": "clear"},
                {"action": "rescan"}):
        assert req_from(board, REMOTE, {"op": "usd", **act}) == usd_locked, act
    assert req_from(board, REMOTE, commit) == \
        {"ok": False, "err": "commit locked: board claimed (use ssh)", "code": "locked"}
    assert hashlib.sha256(read_slot(board.card, 2048, 65536 * BLK)).hexdigest() == p4_before
    st = req_from(board, REMOTE, {"op": "usd"})                 # status: open
    assert st["ok"] is True and st["present"] is True, st
    fmt = req_from(board, TRUSTED, {"op": "usd", "action": "format", "confirm": "erase"})
    assert fmt == {"ok": True, "state": "empty"}, fmt          # the board itself: allowed
    log = board.hd.log()
    assert "slot: LOCKED -- a usd action from 127.0.0.1:" in log
    assert "slot: LOCKED -- a store commit from 127.0.0.1:" in log


def _debug_port(board, src, port, hello: bytes, timeout=5.0) -> bytes:
    """Connect to a DUT debug port from `src`, say `hello`, and return what the
    board answers until it closes or goes quiet (retrying the single-client
    accept-then-close of a port still holding the previous client)."""
    for _ in range(40):
        with socket.create_connection(("127.0.0.1", board.hd.port(port)), timeout=timeout,
                                      source_address=(src, 0)) as s:
            try:
                s.sendall(hello)
            except OSError:
                pass
            s.settimeout(1.0)
            buf = b""
            try:
                while True:
                    d = s.recv(4096)
                    if not d:
                        break
                    buf += d
            except socket.timeout:
                pass
            except OSError:
                pass
            if buf:
                return buf
        time.sleep(0.05)
    return b""


def test_claimed_board_locks_the_dut_debug_ports(board):
    """HM_ANSWERS C3 (David 2026-09-26: YES): on a claimed board XVC 2542 and
    jtag_server 6921 serve the board itself only; a remote peer gets ONE line
    with code "locked", then the close -- even though it spoke first (hw_server's
    getinfo:, OpenOCD's first sample). The controls: the same connect unclaimed,
    and from the board itself once claimed, is served. `version` says xvc_lock."""
    assert "xvc_lock" in req_from(board, REMOTE, {"op": "version"})["features"]
    assert _debug_port(board, REMOTE, 2542, b"getinfo:").startswith(b"xvcServer_v1.0:")
    assert _debug_port(board, REMOTE, 6921, b"R") in (b"0", b"1")
    claim(board)
    for port, hello, what in ((2542, b"getinfo:", "xvc"), (6921, b"R", "jtag")):
        got = _debug_port(board, REMOTE, port, hello)
        assert got == (b'{"ok":false,"err":"%s locked: board claimed (use ssh)","code":"locked"}\n'
                       % what.encode()), (port, got)
        assert json.loads(got)["code"] == "locked"
    assert _debug_port(board, TRUSTED, 2542, b"getinfo:").startswith(b"xvcServer_v1.0:")
    assert _debug_port(board, TRUSTED, 6921, b"R") in (b"0", b"1")
    log = board.hd.log()
    assert "slot: LOCKED -- an xvc connection (2542) from 127.0.0.1:" in log
    assert "slot: LOCKED -- a jtag_server connection (6921) from 127.0.0.1:" in log


@pytest.mark.skipif(Path("/usr/sbin/mps3-reboot").exists(),
                    reason="never run mps3-unclaim where a real mps3-reboot exists")
def test_mps3_unclaim_unlocks(board):
    claim(board)
    assert slot_from(board, REMOTE, "rollback") == LOCKED
    unclaim = REPO / "src/linux_harness/sw/br2_external/rootfs_overlay/usr/sbin/mps3-unclaim"
    # the real script: it removes the claim first, then syncs and tries to reboot --
    # which, off the board, fails harmlessly after the part that matters
    subprocess.run(["sh", str(unclaim), "--yes"], env=dict(os.environ, MPS3_KEYS_CLAIM=str(board.ak)),
                   capture_output=True)
    assert not board.ak.exists()
    assert claimed(board) is False
    assert slot_from(board, REMOTE, "rollback") == {"ok": False, "err": "slot B is not a valid image", "code": "not_valid"}


# --------------------------------------------------------------------------- #
# the owner's way through: pyverify's ssh tunnel
# --------------------------------------------------------------------------- #

def _cli(board, *args, env=None):
    e = dict(os.environ, PYTHONPATH=str(REPO / "host" / "pyverify"),
             MPS3_SSH=f"{sys.executable} {HERE / 'fake_ssh.py'}")
    e.update(env or {})
    cmd = [sys.executable, "-m", "pyverify.cli", "slot", *args, "--host", "127.0.0.1",
           "--port", str(board.ctl), "--push-port", str(board.hd.port(6910)),
           "--identify-port", str(board.hd.port(6899)), "--timeout", "20"]
    return subprocess.run(cmd, capture_output=True, text=True, env=e)


def test_cli_goes_through_ssh_once_the_board_is_claimed(board, imgs, tmp_path):
    img = tmp_path / "art" / "linux_slot.img"
    img.parent.mkdir()
    img.write_bytes(imgs["b"])
    (img.parent / "version").write_text(f"static_id=0x{SID:08X}\n")
    claim(board)

    r = _cli(board, "commit", "--no-ssh")                          # the raw ports: locked
    assert r.returncode == 1 and "slot locked: board claimed (use ssh)" in r.stdout
    assert "--via-ssh" in r.stderr

    r = _cli(board, "push", str(img))                              # auto: identify says claimed
    assert r.returncode == 0, r.stderr
    assert "ssh tunnel via mps3-linux" in r.stderr and '"staged": "B"' in r.stdout
    r = _cli(board, "commit", "--via-ssh")
    assert r.returncode == 0 and '"default": "B"' in r.stdout, r.stderr
    assert board.hd.log().count("LOCKED -- commit") == 1          # only the --no-ssh one

    r = _cli(board, "rollback", "--via-ssh",                        # a tunnel that fails: exit 3
             env={"MPS3_SSH": f"{sys.executable} -c 'import sys; sys.exit(255)'"})
    assert r.returncode == 3 and "ssh tunnel" in r.stderr
    r = _cli(board, "push", str(img), "--via-ssh", "--via", "tftp")
    assert r.returncode == 2 and "cannot ride an ssh tunnel" in r.stderr


def test_ssh_tunnel_argv_is_key_only_and_forwards_to_the_board_loopback():
    argv = pslot.ssh_tunnel_argv("mps3-linux", [(40001, 6900), (40002, 6910)], ssh="ssh")
    assert argv[0] == "ssh" and argv[-1] == "mps3-linux" and "-N" in argv
    assert "PasswordAuthentication=no" in argv and "ExitOnForwardFailure=yes" in argv
    assert argv[argv.index("-L") + 1] == "127.0.0.1:40001:127.0.0.1:6900"
    assert "127.0.0.1:40002:127.0.0.1:6910" in argv
