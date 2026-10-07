"""test_locate_e2e.py -- `locate` in mps3-harnessd over real sockets (host build,
MOCK fabric; net-protocol.md v0.16 "Locate", the Harness Manager's R3). The
backlight is CLCDKVM.CTRL[5] in the mock fabric file, read while harnessd runs.

  - the reply {"ok":true,"op":"locate","until_ms":N} (relative ms; 0 = stopped),
    the refusals (code "invalid"), version.features "locate";
  - the BLINK: CTRL[5] toggles at ~4 edges/s while it runs; it ends on time, lit;
  - REPLACE: a second locate replaces the first (a longer run, the log says so);
  - STOP (s 0) and the RESTORE: lit after every end. NEGATIVE CONTROL: a harnessd
    run with --mock-negctl-locate-no-restore, stopped in the OFF phase, stays dark
    -- the same check fails there;
  - RESTART: a harnessd killed mid-blink (dark) comes back lit (clcd_kvm_init);
  - NOT claim-locked: a remote peer of a claimed board may locate it.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harnessd_mock import Harnessd  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
SID = 0x5A5A0001
CLCDKVM, CTRL, BL = 0x44AD0000, 0x00, 1 << 5
TRUSTED, REMOTE = "127.0.0.3", "127.0.0.1"
KEY = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J me@pc\n"

pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")


def lit(hd: Harnessd) -> bool:
    return bool(hd.fabric.rd(CLCDKVM, CTRL) & BL)


def start(tmp: Path, extra=(), name="hd", fabric=None) -> Harnessd:
    hd = Harnessd(BIN, tmp / name, static_id=SID,
                  extra=["--lcdmirror", "none", *extra], **({"fabric_name": fabric} if fabric else {}))
    return hd.start()


def wait_for(pred, timeout=5.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def edges_over(hd: Harnessd, seconds: float) -> int:
    n, prev = 0, lit(hd)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        cur = lit(hd)
        if cur != prev:
            n, prev = n + 1, cur
        time.sleep(0.01)
    return n


def test_reply_blink_end_and_feature(tmp_path):
    hd = start(tmp_path)
    try:
        assert wait_for(lambda: lit(hd)), "clcd init lights the panel"
        assert "locate" in hd.req({"op": "version"})["features"]
        r = hd.req({"op": "locate", "s": 2, "who": "user@host"})
        assert r == {"ok": True, "op": "locate", "until_ms": 2000}
        n = edges_over(hd, 1.5)
        assert 4 <= n <= 8, f"~2 Hz: {n} edges in 1.5 s"
        assert wait_for(lambda: "its time is up" in hd.log(), 3.0)
        assert lit(hd), "ended on time, lit"
        assert edges_over(hd, 0.6) == 0, "no blink after the end"
        for bad in ({"op": "locate"}, {"op": "locate", "s": 31}, {"op": "locate", "s": 5, "who": 7}):
            r = hd.req(bad)
            assert r["ok"] is False and r["code"] == "invalid" and r["err"].startswith("invalid "), r
    finally:
        hd.stop()


def test_replace_and_stop_restore(tmp_path):
    hd = start(tmp_path)
    try:
        assert wait_for(lambda: lit(hd))
        hd.req({"op": "locate", "s": 1, "who": "a"})
        time.sleep(0.5)
        assert hd.req({"op": "locate", "s": 3, "who": "b"})["until_ms"] == 3000
        time.sleep(1.0)                        # past the first one's 1 s: still blinking
        assert edges_over(hd, 0.8) >= 2, "the replace's longer time holds"
        assert "REPLACED" in hd.log()
        assert wait_for(lambda: not lit(hd), 1.0), "catch an OFF phase"
        assert hd.req({"op": "locate", "s": 0}) == {"ok": True, "op": "locate", "until_ms": 0}
        assert wait_for(lambda: lit(hd), 0.5), "stopped in the OFF phase -> lit"
        assert edges_over(hd, 0.6) == 0
    finally:
        hd.stop()


def test_negative_control_without_the_restore_it_stays_dark(tmp_path):
    hd = start(tmp_path, extra=["--mock-negctl-locate-no-restore"])
    try:
        assert wait_for(lambda: lit(hd))
        hd.req({"op": "locate", "s": 10})
        assert wait_for(lambda: not lit(hd), 1.0)
        hd.req({"op": "locate", "s": 0})
        time.sleep(0.4)
        assert not lit(hd), "NEGATIVE CONTROL: with the restore removed the panel stays dark"
    finally:
        hd.stop()


def test_a_restart_mid_blink_comes_back_lit(tmp_path):
    hd = start(tmp_path)
    try:
        assert wait_for(lambda: lit(hd))
        hd.req({"op": "locate", "s": 20})
        assert wait_for(lambda: not lit(hd), 1.0)
    finally:
        hd.stop(signal.SIGKILL)                # died mid-blink, dark
    assert not lit(hd), "the fabric keeps the dark backlight after the kill"
    hd2 = Harnessd(BIN, hd.dir, static_id=SID, extra=["--lcdmirror", "none"], fabric_name="fabric")
    hd2.start()
    try:
        assert wait_for(lambda: lit(hd2), 3.0), "clcd init restores the light at start"
        assert edges_over(hd2, 0.6) == 0, "and no locate survives a restart"
    finally:
        hd2.stop()


def _alias_ok() -> bool:
    try:
        with socket.socket() as s:
            s.bind((TRUSTED, 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _alias_ok(), reason=f"cannot bind {TRUSTED}")
def test_not_claim_locked(tmp_path):
    hd = start(tmp_path, extra=["--mock-trusted-peer", TRUSTED])
    ak = hd.dir / "ssh" / "authorized_keys"
    ak.parent.mkdir(parents=True, exist_ok=True)
    ak.write_bytes(KEY)
    try:
        with socket.create_connection(("127.0.0.1", hd.port(6900)), timeout=10,
                                      source_address=(REMOTE, 0)) as s:
            s.sendall(b'{"op":"locate","s":1}\n')
            buf = b""
            while not buf.endswith(b"\n"):
                d = s.recv(4096)
                if not d:
                    break
                buf += d
        assert json.loads(buf) == {"ok": True, "op": "locate", "until_ms": 1000}
    finally:
        hd.stop()
