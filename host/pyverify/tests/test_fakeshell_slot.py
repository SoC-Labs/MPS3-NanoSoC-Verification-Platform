"""The FakeShell's v0.14 boot-slot model (``slots=``, ``profile="linux"``):
the ``slot`` verb, the kind-2 slot-image push on 6910 and TFTP, and THE LOCK --
driven over real sockets by the production client, ``pyverify.slot``.

Every verdict is the contract's text (net-protocol.md "Slot images") in the C's
order (slot_linux.c mps3_slot_op / mps3_cfg_slot_sink). Negative controls sit
beside the positives: the default double is unchanged (``slot`` unknown, kind 2
refused as an unknown kind), the lock opens for a local peer and for an
unclaimed board, and every refused push leaves the slot as it was.
"""
from __future__ import annotations

import json
import socket
import struct
import time
import zlib

import pytest

from pyverify import slot as pslot
from pyverify.pusher import PushError
from pyverify.testing.fakeshell import (
    SLOT_BYTES,
    SLOT_JOB_CODES,
    SLOT_LOCKED_ERR,
    FakeShell,
    slot_code,
    tftp_put,
)

SID = 0x61BC6789
LOCKED = {"ok": False, "err": SLOT_LOCKED_ERR, "code": "locked"}   # HM_ANSWERS S3/C2
A_CRC = 0x3E5E9C2C
KEYS = ["ok", "card", "fabric_sid", "running", "default", "seq", "target", "staged", "a", "b",
        "job", "claimed", "confirmed"]


def s0lb(payload: bytes, dst: int = 0x80000000, *, corrupt_region: bool = False) -> bytes:
    """A 1-region stage0 S0LB v2 image (stage0_boot.h), built here independently."""
    entry = struct.pack("<4I", 48, dst, len(payload),
                        zlib.crc32(payload) ^ (1 if corrupt_region else 0))
    hdr = struct.pack("<7I", 0x424C3053, 2, 1, dst, 0, 0, 0)
    return hdr + struct.pack("<I", zlib.crc32(hdr + b"\0\0\0\0" + entry) & 0xFFFFFFFF) + entry + payload


IMG = s0lb(bytes((i * 7 + 3) & 0xFF for i in range(5000)))
IMG_CRC = struct.unpack_from("<I", IMG, 28)[0]


def frame(img: bytes, *, sid: int = SID, rm_slot: int = 0, words: int = -1) -> bytes:
    pad = img + b"\0" * ((-len(img)) % 4)
    n = len(pad) // 4 if words < 0 else words
    return struct.pack(">4sHBBIIII", b"MPS3", 1, 2, rm_slot, sid, 0, n,
                       zlib.crc32(pad) & 0xFFFFFFFF) + pad


def raw(fake: FakeShell, data: bytes) -> None:
    with socket.create_connection((fake.host, fake.raw_tcp_port), timeout=5) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        try:
            while s.recv(4096):
                pass
        except OSError:
            pass


def ctl(fake: FakeShell, obj) -> dict:
    with socket.create_connection((fake.host, fake.control_port), timeout=5) as s:
        s.sendall(json.dumps(obj).encode() + b"\n")
        return json.loads(s.makefile("rb").readline())


def req(fake: FakeShell, act: str, slot=None) -> dict:
    obj = {"op": "slot", "act": act}
    if slot is not None:
        obj["slot"] = slot
    return ctl(fake, obj)


def settle(fake: FakeShell) -> dict:
    """The job's final status, failed or not."""
    for _ in range(50):
        st = req(fake, "status")
        if st["job"]["state"] not in ("writing", "verifying"):
            return st
    raise AssertionError(st)


def board(**slots) -> FakeShell:
    base = {"running": "A", "default": "A", "seq": 1,
            "a": {"state": "valid", "hdr_crc": A_CRC, "len": 24354312}}
    base.update(slots)
    return FakeShell.ephemeral(profile="linux", static_id=SID, slots=base)


# --------------------------------------------------------------------------- #
# opt-in: the default double is exactly what it was
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("profile", ["bare-metal", "linux"])
def test_without_slots_the_double_is_unchanged(profile):
    with FakeShell.ephemeral(profile=profile, static_id=SID) as fake:
        assert ctl(fake, {"op": "slot", "act": "status"}) == \
            {"ok": False, "err": "unknown op 'slot'"}
        raw(fake, frame(IMG))
        assert fake.rejected_pushes and fake.rejected_pushes[-1].status == "ERR_KIND"
        assert fake.slots is None


def test_slots_are_the_linux_harness_s():
    with pytest.raises(ValueError, match="profile='linux'"):
        FakeShell.ephemeral(static_id=SID, slots={})
    with pytest.raises(ValueError, match="unknown slots= keys"):
        FakeShell.ephemeral(profile="linux", static_id=SID, slots={"cards": True})


# --------------------------------------------------------------------------- #
# the golden path, through pyverify.slot
# --------------------------------------------------------------------------- #

def test_status_shape_is_the_contract_s():
    with board() as fake:
        st = pslot.slot_status(fake.host, port=fake.control_port)
        assert list(st) == KEYS
        assert (st["card"], st["fabric_sid"], st["running"], st["default"], st["seq"],
                st["target"], st["staged"]) == (True, f"0x{SID:08x}", "A", "A", 1, "B", None)
        assert st["a"] == {"state": "valid", "hdr_crc": f"0x{A_CRC:08x}", "len": 24354312,
                           "verified": "boot"}
        assert st["b"] == {"state": "empty", "verified": "no"}
        assert st["job"] == {"act": "none", "slot": None, "state": "idle", "got": 0, "len": 0,
                             "err": ""}
        assert (st["claimed"], st["confirmed"]) == (False, True)   # HM_ANSWERS S2/S5


def test_codes_beside_err_and_job_code():
    """HM_ANSWERS S3/C2: the verb's refusals carry ``code`` (coordinator.c's table)
    and a failed job carries ``job.code`` (slot_linux.c's); unlisted texts none."""
    assert slot_code("slot A runs, but identity lock: x") == "identity_lock"
    assert slot_code("boot-select read-back (default 1 seq 2)") == "bootsel"
    assert slot_code("fork: ENOMEM") is None
    assert slot_code("region 3 CRC", SLOT_JOB_CODES) == "bad_image"
    assert slot_code("record read-back", SLOT_JOB_CODES) == "record"
    assert slot_code("result lost (harnessd restarted): verify again", SLOT_JOB_CODES) == "restarted"
    with board() as fake:
        raw(fake, frame(s0lb(b"x" * 64, dst=0x10)))            # outside the DDR window
        job = settle(fake)["job"]
        assert (job["state"], job["err"], job["code"]) == \
            ("failed", "region outside the DDR window", "bad_image")
        assert list(job)[-2:] == ["err", "code"]                  # after err, like the C
        raw(fake, frame(IMG))
        assert "code" not in settle(fake)["job"]                  # ok: no code


def test_status_reports_the_claim_and_the_confirm():
    """``claimed`` follows the shell's claim on every request (the lock's input);
    ``confirmed`` is the scenario's att_confirm."""
    with board(confirmed=False) as fake:
        assert req(fake, "status")["confirmed"] is False
        fake.ssh_claimed = True
        assert req(fake, "status")["claimed"] is True
        fake.ssh_claimed = False
        assert req(fake, "status")["claimed"] is False


@pytest.mark.parametrize("via", ["tcp", "tftp"])
def test_push_verify_commit_then_rollback(via):
    with board(verify_polls=2) as fake:
        port = fake.raw_tcp_port if via == "tcp" else fake.tftp_port
        pslot.push_slot_image(IMG, fake.host, static_id=SID, via=via, port=port)
        st = req(fake, "status")
        assert st["job"]["state"] == "verifying" and st["job"]["slot"] == "B"   # read-back runs
        st = pslot.wait_job(fake.host, port=fake.control_port, timeout_s=5, poll_s=0.01)
        assert st["job"] == {"act": "push", "slot": "B", "state": "ok", "got": len(frame(IMG)) - 24,
                             "len": len(frame(IMG)) - 24, "err": ""}
        assert st["b"] == {"state": "valid", "hdr_crc": f"0x{IMG_CRC:08x}", "len": len(IMG),
                           "sid": f"0x{SID:08x}", "verified": "readback"}
        assert st["staged"] == "B"
        st = pslot.slot_request(fake.host, "commit", port=fake.control_port)
        assert (st["default"], st["seq"], st["target"]) == ("B", 2, None)
        assert pslot.slot_request(fake.host, "commit", port=fake.control_port)["seq"] == 2  # idempotent
        # after the flip, before the reboot: no free slot
        pslot.push_slot_image(IMG, fake.host, static_id=SID, via="tcp", port=fake.raw_tcp_port)
        assert settle(fake)["job"]["err"] == "no free slot: A runs, B is the default -- rollback first"
        # rollback: A still runs and stage0 CRC-checked it at this boot
        st = pslot.slot_request(fake.host, "rollback", port=fake.control_port)
        assert (st["default"], st["seq"], st["target"]) == ("A", 3, "B")


# --------------------------------------------------------------------------- #
# refused pushes: the job says why, the slot is as it was (or empty past sector 0)
# --------------------------------------------------------------------------- #

OLD_B = {"state": "valid", "hdr_crc": 0x0A4DA20B, "len": 9000}


@pytest.mark.parametrize("case,err,b_after", [
    # refused before a byte reaches the card: the OLD image is still there
    ("foreign_sid", "image for 0x0badcafe != fabric 0x61bc6789", "valid"),
    ("guard_running", "slot mismatch: the target is B", "valid"),
    ("not_s0lb", "no S0LB magic", "valid"),
    ("table_crc", "table CRC", "valid"),
    ("too_large", "image too large for slot B", "valid"),
    # past the first sector: its old first sector was zeroed, never a valid table
    ("region_crc", "region 0 CRC", "empty"),
    ("torn", "torn (the push stopped early)", "empty"),
    ("transport_crc", "aborted", "empty"),
    # the slot itself refuses
    ("absent", "slot B absent", "absent"),
    ("overlap_bad", "slot B bad: overlaps the 0xDA store", "bad"),
])
def test_a_refused_push(case, err, b_after):
    b = {"absent": {"state": "absent"},
         "overlap_bad": {"state": "bad", "err": "overlaps the 0xDA store", "overlap": True}
         }.get(case, OLD_B)
    extra = {"slot_bytes": 4096} if case == "too_large" else {}
    with board(b=b, **extra) as fake:
        img = IMG
        if case == "foreign_sid":
            raw(fake, frame(img, sid=0x0BADCAFE))
        elif case == "guard_running":
            raw(fake, frame(img, rm_slot=1))
        elif case == "not_s0lb":
            raw(fake, frame(bytes(4096)))
        elif case == "table_crc":
            bad = bytearray(img)
            bad[40] ^= 1
            raw(fake, frame(bytes(bad)))
        elif case == "region_crc":
            raw(fake, frame(s0lb(bytes(5000), corrupt_region=True)))
        elif case == "torn":
            raw(fake, frame(img)[:24 + 3000])
        elif case == "transport_crc":
            f = bytearray(frame(img))
            f[-1] ^= 0xFF
            raw(fake, bytes(f))
        else:
            raw(fake, frame(img))
        st = settle(fake)
        assert st["job"]["state"] == "failed" and st["job"]["err"] == err, st["job"]
        assert st["b"]["state"] == b_after and st["staged"] is None, st
        if b_after == "valid":
            assert st["b"]["hdr_crc"] == "0x0a4da20b"


def test_a_push_while_a_job_runs_is_closed_unread():
    with board(verify_polls=50) as fake:
        raw(fake, frame(IMG))
        assert req(fake, "status")["job"]["state"] == "verifying"
        raw(fake, frame(IMG, sid=0x0BADCAFE))                 # would fail: must not even try
        job = req(fake, "status")["job"]
        assert job["state"] == "verifying" and job["err"] == ""


# --------------------------------------------------------------------------- #
# the verb's refusals
# --------------------------------------------------------------------------- #

def test_the_verb_s_argument_and_flip_refusals():
    with board(b={"state": "valid", "hdr_crc": 0x0A4DA20B, "len": 9000}) as fake:
        assert ctl(fake, {"op": "slot"}) == {"ok": False, "err": "bad args"}
        assert ctl(fake, {"op": "slot", "act": 7}) == {"ok": False, "err": "bad args"}
        assert ctl(fake, {"op": "slot", "act": "status", "slot": 1}) == \
            {"ok": False, "err": "bad args"}
        assert ctl(fake, {"op": "slot", "act": "status", "slot": "ABCDEFGH"}) == \
            {"ok": False, "err": "bad args"}
        assert req(fake, "format") == {"ok": False, "err": "bad act", "code": "bad_act"}
        assert req(fake, "status", "C") == {"ok": False, "err": "bad slot", "code": "bad_slot"}
        assert req(fake, "status", "")["ok"] is True                # "" = no selector
        assert req(fake, "commit") == {"ok": False, "err": "nothing staged: push an image first",
                                        "code": "nothing_staged"}
        assert req(fake, "rollback") == {"ok": False, "err": "slot B not verified",
                                          "code": "not_verified"}
        assert req(fake, "rollback", "A") == \
            {"ok": False, "err": "slot mismatch: rollback would pick B", "code": "slot_mismatch"}
        # verify needs the slot record to bind a static_id
        assert req(fake, "verify")["job"]["state"] == "verifying"
        assert settle(fake)["job"]["err"] == "no slot record: static_id unknown"
        assert req(fake, "verify", "C") == {"ok": False, "err": "bad slot", "code": "bad_slot"}


@pytest.mark.parametrize("sid,err,how", [
    (SID, "", "readback"),
    (0x0BADCAFE, "image for 0x0badcafe != fabric 0x61bc6789", "no"),
])
def test_verify_binds_through_the_slot_record(sid, err, how):
    with board(b={"state": "valid", "hdr_crc": 0x0A4DA20B, "len": 9000, "sid": sid}) as fake:
        req(fake, "verify", "B")
        st = settle(fake)
        assert st["job"]["err"] == err and st["b"]["verified"] == how
        r = req(fake, "rollback")
        if how == "readback":
            assert r["ok"] is True and r["default"] == "B"
        else:
            assert r == {"ok": False, "err": "slot B not verified", "code": "not_verified"}


@pytest.mark.parametrize("cfg,err,code", [
    ({"card": False}, "no card", "no_card"),
    ({"card": "io"}, "card io", "card_io"),
    ({"running": "unknown"}, "no stage0 block", "no_stage0"),
    ({"fabric_sid": 0}, "fabric static_id unknown", "fabric_unknown"),
])
def test_refusals_common_to_the_mutations(cfg, err, code):
    with board(**cfg) as fake:
        assert req(fake, "rollback") == {"ok": False, "err": err, "code": code}
        st = req(fake, "status")
        if cfg.get("card") is False:
            assert list(st) == ["ok", "card", "fabric_sid", "running", "staged", "job",
                                "claimed", "confirmed"]
        elif cfg.get("card") == "io":
            assert st == {"ok": False, "err": "card io", "code": "card_io"}


# --------------------------------------------------------------------------- #
# THE LOCK
# --------------------------------------------------------------------------- #

def test_a_claimed_board_refuses_a_remote_peer_s_mutations():
    # 127.0.0.1 is "remote" here: the one trusted peer is another address
    with board(trusted_peer="192.0.2.7") as fake:
        fake.ssh_claimed = True
        assert req(fake, "commit") == LOCKED
        assert req(fake, "rollback") == LOCKED
        assert req(fake, "status")["ok"] is True                  # reads stay open
        assert req(fake, "verify", "A")["ok"] is True
        settle(fake)
        before = req(fake, "status")
        raw(fake, frame(IMG))                                     # closed unread
        assert req(fake, "status") == before                      # not even `job` moved
        with pytest.raises(PushError, match="access violation"):
            pslot.push_slot_image(IMG, fake.host, static_id=SID, via="tftp", port=fake.tftp_port)
        assert req(fake, "status") == before


def test_the_lock_negative_controls_a_local_peer_and_an_unclaimed_board():
    with board() as fake:                                        # 127/8 is local
        fake.ssh_claimed = True
        assert req(fake, "commit") == {"ok": False, "err": "nothing staged: push an image first",
                                        "code": "nothing_staged"}
        raw(fake, frame(IMG))
        assert settle(fake)["job"]["state"] == "ok"
    with board(trusted_peer="192.0.2.7") as fake:                 # remote, but unclaimed
        assert req(fake, "commit") == {"ok": False, "err": "nothing staged: push an image first",
                                        "code": "nothing_staged"}


def test_a_tofu_claim_locks_at_once():
    with board(trusted_peer="192.0.2.7") as fake:
        assert req(fake, "rollback") == {"ok": False, "err": "slot B is not a valid image",
                                          "code": "not_valid"}
        tftp_put(fake.host, fake.tftp_port, b"ssh-ed25519 AAAA owner@pc\n",
                 filename="authorized_keys")
        assert fake.ssh_claimed is True
        assert req(fake, "rollback") == LOCKED


def test_slot_bytes_default_is_the_standard_card():
    assert SLOT_BYTES == 64 * 1024 * 1024
    with board() as fake:
        raw(fake, frame(IMG, words=(SLOT_BYTES - 512) // 4 + 1))
        assert settle(fake)["job"]["err"] == "image too large for slot B"


# --------------------------------------------------------------------------- #
# a reboot boots the default (HM_ANSWERS S9) -- and waits for the card (change 6)
# --------------------------------------------------------------------------- #

def reboot_board(**slots) -> FakeShell:
    base = {"running": "A", "default": "A", "seq": 1,
            "a": {"state": "valid", "hdr_crc": A_CRC, "len": 24354312}}
    base.update(slots)
    return FakeShell.ephemeral(profile="linux", static_id=SID, slots=base, reboot_in_ms=50)


def reboot_and_wait(fake: FakeShell) -> dict:
    up0 = ctl(fake, {"op": "stats"})["up_ms"]
    assert ctl(fake, {"op": "reboot"})["ok"] is True
    for _ in range(100):
        time.sleep(0.02)
        if ctl(fake, {"op": "stats"})["up_ms"] < up0:
            break
    return req(fake, "status")


def test_a_reboot_boots_the_committed_slot_and_forgets_the_boot_state():
    with reboot_board() as fake:
        raw(fake, frame(IMG))
        st = settle(fake)
        assert st["staged"] == "B" and st["b"]["verified"] == "readback"
        assert req(fake, "commit")["default"] == "B"
        st = reboot_and_wait(fake)
        assert (st["running"], st["default"], st["target"], st["staged"]) == ("B", "B", "A", None)
        assert st["b"]["verified"] == "boot"                     # boot_crc from the new slot
        assert st["a"]["verified"] == "no"                       # the read-back proofs are gone
        assert st["job"] == {"act": "none", "slot": None, "state": "idle", "got": 0, "len": 0,
                             "err": ""}
        assert st["confirmed"] is True
        # boot 1 (A, confirmed) stamped A's record, so A can be verified and rolled back to
        assert st["a"]["sid"] == f"0x{SID:08x}"
        assert req(fake, "rollback") == {"ok": False, "err": "slot A not verified",
                                         "code": "not_verified"}
        req(fake, "verify", "A")
        assert settle(fake)["a"]["verified"] == "readback"
        assert req(fake, "rollback", "A")["default"] == "A"


def test_an_unhealthy_default_falls_back_and_keeps_the_default():
    """The fallback (S5): stage0 never rewrites the card, so after it gives up on
    the unhealthy default the OTHER slot runs, `default` still names the bad one,
    there is no target, and `rollback` is how a tool gets out."""
    with reboot_board(unhealthy={"B"}) as fake:
        raw(fake, frame(IMG))
        settle(fake)
        assert req(fake, "commit")["default"] == "B"
        st = reboot_and_wait(fake)
        assert (st["running"], st["default"], st["target"], st["staged"]) == ("A", "B", None, None)
        raw(fake, frame(IMG))
        assert settle(fake)["job"]["err"] == \
            "no free slot: A runs, B is the default -- rollback first"
        st = req(fake, "rollback")
        assert (st["default"], st["target"]) == ("A", "B")


@pytest.mark.parametrize("cfg,running", [
    ({"default": "B", "b": {"state": "empty"}}, "A"),            # default not valid: the other
    ({"a": {"state": "bad", "err": "table CRC"}}, "rescue"),     # nothing valid: rescue
    ({"card": False}, "rescue"),
])
def test_a_reboot_follows_stage0_s_pick(cfg, running):
    with reboot_board(**cfg) as fake:
        assert reboot_and_wait(fake)["running"] == running


def test_a_reboot_waits_for_the_card_job():
    with reboot_board(verify_polls=50) as fake:
        req(fake, "verify", "A")
        assert req(fake, "status")["job"]["state"] == "verifying"
        assert ctl(fake, {"op": "reboot"}) == {"ok": False, "err": "EBUSY"}
        settle(fake)
        assert ctl(fake, {"op": "reboot"})["ok"] is True


def test_unhealthy_names_slots():
    with pytest.raises(ValueError, match="unhealthy"):
        FakeShell.ephemeral(profile="linux", static_id=SID, slots={"unhealthy": {"C"}})
