"""test_identity_e2e.py -- the BOARD identity in mps3-harnessd, end to end over
real sockets (host build, MOCK fabric): net-protocol.md v0.16 "Identity".

  - `identity` answers this boot's identity (the run file mps3-identity wrote),
    each field's source, the stage0 bake read from the LMB-tail block, the
    override in force and `pending`; the SAME body `mps3-identity get --json`
    prints for the same inputs (one renderer, identity_core.c);
  - `identity_set` writes the override (validated, atomic) and says what the next
    boot changes; `clear` removes it; nothing changes live;
  - the CLAIM LOCK (checked first): a claimed board refuses a non-local peer with
    code "locked", and the board's own peer (the ssh tunnel's far end) passes --
    the negative control beside every refusal;
  - no card-backed /persist -> code "no_persist"; bad input -> code "invalid",
    naming the field, and nothing written;
  - identify carries `label` (after ssh, before ports) and the identity's MAC/IP;
    version.features ends "lcd_mirror", "identity", "locate" (+ v0.17's "presence",
    "panel");
  - the CLCD: row 0 is the label, the NET row the identity's IP, the MAC row its
    MAC -- read off the panel through the LCD mirror (6940), pixel-decoded.
"""
from __future__ import annotations

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
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "host" / "pyverify"))

from harnessd_mock import Harnessd, HOST_FEATURES  # noqa: E402
from pyverify.identify import identify  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
TOOL = os.environ.get("MPS3_IDENTITY_BIN", str(HERE.parent / "build" / "host-uio" / "mps3-identity"))
SID = 0x5A5A0001
TRUSTED, REMOTE = "127.0.0.3", "127.0.0.1"
KEY = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J me@pc\n"
LOCKED = {"ok": False, "err": "identity locked: board claimed (use ssh)", "code": "locked"}

pytestmark = pytest.mark.skipif(not (Path(BIN).exists() and Path(TOOL).exists()),
                                reason="mps3-harnessd / mps3-identity not built (make host)")

B2 = {"ip": 0xC0A80B65, "mac": bytes.fromhex("0200000002fe"), "label": b"MPS3-02"}


def seed_stage0(hd: Harnessd, ip: int, mac: bytes, label: bytes) -> None:
    """Board 2's bake in the mock fabric's stage0 block (stage0_status.h offsets)."""
    f = hd.fabric
    f.wr(0x1F000, 0xE00 + 0xB0, ip)
    f.wr(0x1F000, 0xE00 + 0xB4, int.from_bytes(mac[:4], "little"))
    f.wr(0x1F000, 0xE00 + 0xB8, int.from_bytes(mac[4:], "little"))
    lab = label.ljust(8, b"\0")
    f.wr(0x1F000, 0xE00 + 0xEC, int.from_bytes(lab[:4], "little"))
    f.wr(0x1F000, 0xE00 + 0xF0, int.from_bytes(lab[4:], "little"))


def s0_dump(hd: Harnessd, path: Path) -> Path:
    path.write_bytes(b"".join(hd.fabric.s0(4 * w).to_bytes(4, "little") for w in range(64)))
    return path


def tool(hd: Harnessd, *args, s0file=None):
    return subprocess.run([TOOL, "--override", str(hd.dir / "persist" / "etc" / "mps3" / "identity"),
                           "--run", str(hd.dir / "identity"),
                           "--persist-state", str(hd.dir / "persist.state"),
                           "--status-file", str(s0file) if s0file else "none", *args],
                          capture_output=True, text=True, timeout=20)


def board(tmp: Path, *, stage0=True, resolve=True, extra=(), persist=True) -> Harnessd:
    """A board-2 harnessd: its stage0 block carries board 2's bake, and (resolve)
    S13mps3identity has run -- mps3-identity over the SAME block wrote the run file."""
    hd = Harnessd(BIN, tmp / "hd", static_id=SID, extra=list(extra))
    if stage0:
        seed_stage0(hd, **B2)
    if not persist:
        (hd.dir / "persist.state").write_text("backing=tmpfs reason=cmdline storage=ok\n")
    if resolve:
        r = tool(hd, "resolve", s0file=s0_dump(hd, tmp / "s0.bin"))
        assert r.returncode == 0, r.stderr
    return hd.start()


def req_from(hd: Harnessd, src: str, obj) -> dict:
    """One 6900 request from `src` (retrying the single-client accept-then-EOF)."""
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", hd.port(6900)), timeout=10,
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


def _alias_ok() -> bool:
    try:
        with socket.socket() as s:
            s.bind((TRUSTED, 0))
        return True
    except OSError:
        return False


def test_identity_reports_the_boot_and_matches_the_cli(tmp_path):
    hd = board(tmp_path)
    try:
        raw = None
        with hd.ctl() as c:
            c.send({"op": "identity"})
            raw = c.line()
        r = json.loads(raw)
        assert list(r) == ["ok", "op", "label", "hostname", "ip", "mac", "source", "stage0",
                           "override", "pending", "persist"], "the wire key order"
        assert (r["label"], r["hostname"], r["ip"], r["mac"]) == \
            ("MPS3-02", "mps3-02", "192.168.11.101/24", "0200000002fe")
        assert r["source"] == {"label": "stage0", "hostname": "label", "ip": "stage0", "mac": "stage0"}
        assert r["stage0"] == {"label": "MPS3-02", "ip": "192.168.11.101/24", "mac": "0200000002fe"}
        assert r["override"] is None and r["pending"] is None and r["persist"] is True
        # one renderer: the board CLI prints exactly this body for the same inputs
        cli = tool(hd, "get", "--json", s0file=s0_dump(hd, tmp_path / "s0b.bin"))
        assert cli.returncode == 0, cli.stderr
        assert raw.decode().rstrip("\n") == '{"ok":true,"op":"identity",' + cli.stdout.strip()[1:]
        assert "identity: board MPS3-02 (mps3-02) 192.168.11.101/24 02:00:00:00:02:fe" in hd.log()
    finally:
        hd.stop()


def test_identity_set_writes_the_override_and_pending(tmp_path):
    hd = board(tmp_path)
    ovr = hd.dir / "persist" / "etc" / "mps3" / "identity"
    try:
        r = hd.req({"op": "identity_set", "label": "BENCH-2", "ip": "192.168.11.7", "hostname": "b2"})
        assert r == {"ok": True, "op": "identity_set", "persisted": True,
                     "pending": {"label": "BENCH-2", "hostname": "b2", "ip": "192.168.11.7/24"},
                     "applies": "reboot"}
        text = ovr.read_text()
        assert "MPS3_LABEL=BENCH-2\n" in text and "MPS3_IP=192.168.11.7/24\n" in text
        assert [p.name for p in ovr.parent.iterdir()] == ["identity"], "no temp file left"
        st = hd.req({"op": "identity"})
        assert st["label"] == "MPS3-02", "nothing changes live: it applies at the next boot"
        assert st["override"] == {"label": "BENCH-2", "hostname": "b2", "ip": "192.168.11.7/24"}
        assert st["pending"] == {"label": "BENCH-2", "hostname": "b2", "ip": "192.168.11.7/24"}
        # a key dropped with "", then the whole override cleared
        assert hd.req({"op": "identity_set", "hostname": ""})["pending"] == \
            {"label": "BENCH-2", "hostname": "bench-2", "ip": "192.168.11.7/24"}
        r = hd.req({"op": "identity_set", "clear": True})
        assert r["ok"] and r["pending"] is None and not ovr.exists()
        assert hd.req({"op": "identity"})["override"] is None
        # the next boot really resolves what `pending` promised
        hd.req({"op": "identity_set", "mac": "02-11-22-33-44-55"})
        rr = tool(hd, "resolve", s0file=s0_dump(hd, tmp_path / "s0c.bin"))
        assert rr.returncode == 0
        run = (hd.dir / "identity").read_text()
        assert "MPS3_MAC=02:11:22:33:44:55\n" in run and "MPS3_MAC_SRC=override\n" in run
        assert "MPS3_LABEL=MPS3-02\n" in run and "MPS3_LABEL_SRC=stage0\n" in run
    finally:
        hd.stop()


@pytest.mark.parametrize("req,field", [
    ({"label": "mps3-02"}, "label"),
    ({"label": "ABCDEFGHIJKLMNOPQRST"}, "label"),
    ({"mac": "01:00:5e:00:00:01"}, "mac"),
    ({"mac": "00:00:00:00:00:00"}, "mac"),
    ({"ip": "192.168.11.0/24"}, "ip"),
    ({"ip": "10.0.0.1/31"}, "ip"),
    ({"ip": "127.0.0.2/8"}, "ip"),
    ({"hostname": "bad_host"}, "hostname"),
    ({"hostname": "a-"}, "hostname"),
    ({"label": 7}, "label"),
    ({"label": "OK-1", "mac": "zz"}, "mac"),      # all or nothing: OK-1 is not written
])
def test_identity_set_invalid(tmp_path, req, field):
    hd = board(tmp_path)
    try:
        r = hd.req({"op": "identity_set", **req})
        assert r["ok"] is False and r["code"] == "invalid" and r["err"].startswith(f"invalid {field}:"), r
        assert set(r) == {"ok", "err", "code"}, "the uniform failure shape + code"
        assert not (hd.dir / "persist" / "etc" / "mps3" / "identity").exists()
    finally:
        hd.stop()


def test_identity_set_bad_shapes(tmp_path):
    hd = board(tmp_path)
    try:
        for req, why in (({}, "nothing to set"), ({"clear": True, "label": "X"}, "clear takes no"),
                         ({"clear": "yes"}, "invalid clear")):
            r = hd.req({"op": "identity_set", **req})
            assert r["ok"] is False and r["code"] == "invalid" and why in r["err"], r
    finally:
        hd.stop()


def test_identity_set_no_persist(tmp_path):
    hd = board(tmp_path, persist=False)
    try:
        r = hd.req({"op": "identity_set", "label": "MPS3-02"})
        assert r == {"ok": False, "err": "identity: no persistent /persist (use the card)",
                     "code": "no_persist"}
        st = hd.req({"op": "identity"})
        assert st["persist"] is False and st["override"] is None
    finally:
        hd.stop()


@pytest.mark.skipif(not _alias_ok(), reason=f"cannot bind {TRUSTED}")
def test_identity_set_is_claim_locked(tmp_path):
    hd = board(tmp_path, extra=["--mock-trusted-peer", TRUSTED])
    ak = hd.dir / "ssh" / "authorized_keys"
    try:
        # unclaimed: open to every peer
        assert req_from(hd, REMOTE, {"op": "identity_set", "label": "OPEN-1"})["ok"] is True
        ak.parent.mkdir(parents=True, exist_ok=True)
        ak.write_bytes(KEY)
        # claimed: a remote peer is refused FIRST -- even with an invalid value
        assert req_from(hd, REMOTE, {"op": "identity_set", "label": "LOCKED-1"}) == LOCKED
        assert req_from(hd, REMOTE, {"op": "identity_set", "mac": "zz"}) == LOCKED
        assert "OPEN-1" in (hd.dir / "persist" / "etc" / "mps3" / "identity").read_text()
        # ...the read stays open...
        assert req_from(hd, REMOTE, {"op": "identity"})["ok"] is True
        # NEGATIVE CONTROL: the board's own peer (the ssh tunnel) passes
        r = req_from(hd, TRUSTED, {"op": "identity_set", "label": "LOCAL-1"})
        assert r["ok"] is True and r["pending"]["label"] == "LOCAL-1"
        assert "LOCKED -- an identity_set from 127.0.0.1" in hd.log()
        ak.unlink()                                   # mps3-unclaim: unlocked at once
        assert req_from(hd, REMOTE, {"op": "identity_set", "label": "OPEN-2"})["ok"] is True
    finally:
        hd.stop()


def test_identify_and_version_carry_the_identity(tmp_path):
    hd = board(tmp_path)
    try:
        rep = identify("127.0.0.1", port=hd.port(6899))
        keys = list(rep.raw)
        assert keys.index("ssh") < keys.index("label") == keys.index("ports") - 1, keys
        assert rep.raw["label"] == "MPS3-02"
        assert rep.raw["mac"] == "0200000002fe" and rep.raw["ip"] == "192.168.11.101"
        assert rep.raw["dhcp"] is False
        ver = hd.req({"op": "version"})
        assert ver["features"] == HOST_FEATURES
        assert ver["features"][-5:] == ["lcd_mirror", "identity", "locate", "presence", "panel"]
    finally:
        hd.stop()


def test_no_run_file_means_the_image_defaults(tmp_path):
    hd = board(tmp_path, resolve=False)
    try:
        r = hd.req({"op": "identity"})
        assert (r["label"], r["ip"], r["mac"]) == ("MPS3", "192.168.10.101/24", "0200004d5053")
        assert set(r["source"].values()) <= {"default", "label"}
        # the block still shows the bake: what the next boot (with the resolver) takes
        assert r["pending"] == {"label": "MPS3-02", "hostname": "mps3-02", "ip": "192.168.11.101/24",
                                "mac": "0200000002fe"}
        assert "the IMAGE DEFAULTS are reported" in hd.log()
        assert identify("127.0.0.1", port=hd.port(6899)).raw["label"] == "MPS3"
    finally:
        hd.stop()


LCDM = Path(BIN).parent / "mps3-lcdmirror"


@pytest.mark.skipif(not LCDM.exists(), reason="mps3-lcdmirror not built")
def test_the_panel_shows_this_board(tmp_path):
    """Row 0 = the label, row 5 = the net row's IP, row 14 = the MAC -- decoded from
    the panel's pixels through the LCD mirror, in the default (aligned) look and in
    `--panel-theme today`."""
    from test_lcdmirror_e2e import keyed, screen_ok  # noqa: E402
    for theme, want in (("aligned", (" MPS3-02 ", "net    192.168.11.101 ",
                                     " mac 02:00:00:00:02:FE")),
                        ("today", ("MPS3-02".ljust(20), "NET : 192.168.11.101 ",
                                   "MAC 02:00:00:00:02:FE"))):
        extra = ["--lcdmirror-shm", str(tmp_path / f"lcdm_{theme}.shm")]
        if theme == "today":
            extra += ["--panel-theme", "today"]
        (tmp_path / theme).mkdir()
        hd = board(tmp_path / theme, extra=extra)
        try:
            c = keyed(hd.port(6940))
            c.pump_until(lambda c, u: u.snap_last and c.valid_count() == 300, 15.0)
            grid, _cells = screen_ok(c, theme=theme)
            assert grid[0].startswith(want[0]), (theme, grid[0])
            assert grid[5].startswith(want[1]), (theme, grid[5])
            assert grid[14].startswith(want[2]), (theme, grid[14])
            c.close()
        finally:
            hd.stop()
