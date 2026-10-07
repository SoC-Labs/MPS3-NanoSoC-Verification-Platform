"""test_card_sharing_e2e.py — ONE user microSD, TWO writers in one mps3-harnessd:
D13's overlay store (v0.13 `usd` / `commit`, ovlstore_sd over ovl_bdev_posix)
and the slot lane's boot slots (v0.14 `slot` + the kind-2 push, slot_card.c), on
the ONE device `--card` names (aliases --usd-dev / --slot-disk, which must agree).
Written at the D13 x linux-harness merge (lane INTEG); the flag and the overlap
guard below are lane HARDEN's (2026-09-24).

The card is the Linux-provisioned layout, made by STAGE0's own stage0_mkcard.py
(STAGE0_CONTRACT §6): LBA 1-2 boot-select, p4 0xDA @2048 (32 MiB, D13's),
p1/p2 0x7F slots A/B, p3 /persist. Each writer is held to its own regions by
hashing every other one around each of its writes:

    the store (format, commit)   -> only p4 moves
    the slots (push, commit)     -> only slot B and LBA 1-2 move; p4 never does

Both paths open the same whole-disk node, exactly as the rv32 build does
(/dev/mmcblk0 for both). Negative control: the store's own state survives the
slot writes (still `valid`, same default), i.e. the hashes are not vacuous.

THE OVERLAP GUARD: each writer bounds its writes to its own partition, which is
only safe if the partitions do not overlap. A malformed MBR -- the 0xDA entry
starting in LBA 0-2 or overlapping a slot, a 0x7F slot overlapping the 0xDA
store, the other slot, /persist or LBA 0-2 -- is refused by BOTH: D13's
classifier (DA_BAD: `foreign`; format refused, on the wire `exists` -- the
contract's name for OVLSD_EPART, overlay_store_err_name()) and slot_card (the
slot `bad`, never pushed, never the default; IMAGE's mps3-slot, which links the
same slot_card.c, refuses it too). The valid STAGE0 card above is the control.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "host" / "pyverify"))

from harnessd_mock import Harnessd, clearing_payload, frame, partial_payload  # noqa: E402
from pyverify import pusher  # noqa: E402
from pyverify import slot as pslot  # noqa: E402
from test_slot_e2e import (  # noqa: E402, F401  (helpers + the mps3-slot build fixture; no tests)
    BLK, LBA_A, LBA_B, NBLK, S0_BOOTED_FROM, S0_IMAGE_HDR_CRC, hdr_crc, make_card, make_image,
    mps3_slot_tool,
)

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")

SID = 0x5A5A0001
RM_LED = 0x0100001E
P4_LBA, P4_NBLK = 2048, 65536          # STAGE0_CONTRACT §6: D13's 0xDA partition
P3_LBA = 329728


def regions(card: Path) -> dict:
    with open(card, "rb") as f:
        def h(lba, n):
            f.seek(lba * BLK)
            return hashlib.sha256(f.read(n * BLK)).hexdigest()
        return {"mbr": h(0, 1), "bootsel": h(1, 2), "gap": h(3, P4_LBA - 3),
                "p4": h(P4_LBA, P4_NBLK), "slotA": h(LBA_A, NBLK), "slotB": h(LBA_B, NBLK),
                "p3": h(P3_LBA, 8192)}


def moved(a: dict, b: dict) -> set:
    return {k for k in a if a[k] != b[k]}


def _usd(h):
    return h.req({"op": "usd"})


def _wait_usd(h, timeout=10.0):
    deadline = time.time() + timeout
    st = _usd(h)
    while time.time() < deadline and (st["boot"] == "pending" or st["state"] == "init"):
        time.sleep(0.1)
        st = _usd(h)
    return st


def _swap(h, rm_id):
    clr = frame(clearing_payload(), kind=0, static_id=SID, rm_id=rm_id)
    par = frame(partial_payload(rm_id), kind=1, static_id=SID, rm_id=rm_id)
    with h.ctl() as c:
        c.send({"op": "swap", "rm": "led", "src": "tcp"})
        time.sleep(0.2)
        pusher.tcp_send(clr, "127.0.0.1", h.port(6910))
        time.sleep(0.2)
        pusher.tcp_send(par, "127.0.0.1", h.port(6910))
        return json.loads(c.line())


def _commit(h, rm_id):
    clr_p, par_p = clearing_payload(), partial_payload(rm_id)
    req = {"op": "commit", "rm": "led", "src": "tcp", "rm_id": f"0x{rm_id:08x}",
           "static_id": f"0x{SID:08x}", "clear_len": len(clr_p),
           "clear_crc": f"0x{zlib.crc32(clr_p) & 0xFFFFFFFF:08x}", "part_len": len(par_p),
           "part_crc": f"0x{zlib.crc32(par_p) & 0xFFFFFFFF:08x}"}
    with h.ctl() as c:
        c.send(req)
        time.sleep(0.2)                                   # parked
        pusher.tcp_send(frame(clr_p, kind=0, static_id=SID, rm_id=rm_id), "127.0.0.1",
                        h.port(6910))
        pusher.tcp_send_windowed(frame(par_p, kind=1, static_id=SID, rm_id=rm_id),
                                 "127.0.0.1", h.port(6910))
        return json.loads(c.line())


def test_store_and_slots_share_one_card_and_never_write_each_other(tmp_path):
    a = make_image(tmp_path, "a", 3000, 1)
    b = make_image(tmp_path, "b", 5000, 2)
    card = make_card(tmp_path, tmp_path / "a.img")
    # healthy=False: never confirmed, so harnessd never stamps slot A's record --
    # the region hashes below are about the two writers' own writes.
    h = Harnessd(BIN, tmp_path / "hd", static_id=SID, healthy=False,
                 extra=["--card", str(card), "--slot-state", str(tmp_path / "slot.state")])
    h.fabric.wr(0x1F000, 0xE00 + S0_BOOTED_FROM, 1)       # stage0 booted slot A
    h.fabric.wr(0x1F000, 0xE00 + S0_IMAGE_HDR_CRC, hdr_crc(a))
    h.start()
    try:
        # D13 finds ITS partition by type on a Linux card: p4, never formatted yet
        st = _wait_usd(h)
        assert st["present"] is True and st["state"] == "foreign", (st, h.log())
        r0 = regions(card)

        # the store: format (rule (a), inside the existing 0xDA) -- only p4 moves
        assert h.req({"op": "usd", "action": "format", "confirm": "erase-all"}) == \
            {"ok": False, "err": "wipe disabled"}                 # never under Linux
        assert h.req({"op": "usd", "action": "format", "confirm": "erase"}) == \
            {"ok": True, "state": "empty"}, h.log()
        r1 = regions(card)
        assert moved(r0, r1) == {"p4"}

        # the store: persist a running RM -- only p4 moves
        assert _swap(h, RM_LED)["verified"] is True
        assert _commit(h, RM_LED) == {"ok": True, "slot": "A"}, h.log()
        r2 = regions(card)
        assert moved(r1, r2) == {"p4"}
        usd_before = _usd(h)
        assert usd_before["state"] == "valid" and usd_before["default"]["slot"] == "A"

        # the slots: push into B, verify, commit -- only slot B and LBA 1-2 move
        pslot.push_slot_image(b, "127.0.0.1", static_id=SID, via="tcp", port=h.port(6910))
        st = pslot.wait_job("127.0.0.1", port=h.port(6900), timeout_s=20)
        assert st["job"]["state"] == "ok" and st["b"]["state"] == "valid", st
        r3 = regions(card)
        assert moved(r2, r3) == {"slotB"}
        st = pslot.slot_request("127.0.0.1", "commit", port=h.port(6900))
        assert st["default"] == "B"
        r4 = regions(card)
        assert moved(r3, r4) == {"bootsel"}

        # ...and the store never noticed: same state, same default, same text
        usd_after = _usd(h)
        assert {k: usd_after[k] for k in ("state", "text", "default")} == \
            {k: usd_before[k] for k in ("state", "text", "default")}
        # the store's clear rewrites one header copy inside p4, nothing else
        assert h.req({"op": "usd", "action": "clear", "confirm": "erase"})["ok"] is True
        assert moved(r4, regions(card)) == {"p4"}
    finally:
        h.stop()


# --------------------------------------------------------------------------- #
# ONE card flag (lane HARDEN): --card, and the aliases that must agree
# --------------------------------------------------------------------------- #

def _slot(h, act="status"):
    return h.req({"op": "slot", "act": act})


@pytest.mark.parametrize("flag", ["--card", "--usd-dev", "--slot-disk"])
def test_one_flag_gives_the_store_and_the_slots_the_same_card(tmp_path, flag):
    make_image(tmp_path, "a", 3000, 1)
    card = make_card(tmp_path, tmp_path / "a.img")
    h = Harnessd(BIN, tmp_path / "hd", static_id=SID,
                 extra=[flag, str(card), "--slot-state", str(tmp_path / "slot.state")])
    h.start()
    try:
        st = _wait_usd(h)
        assert st["present"] is True and st["state"] == "foreign", (st, h.log())   # p4 unformatted
        sl = _slot(h)
        assert sl["card"] is True and sl["a"]["state"] == "valid", sl
        assert f"slot: card {card}," in h.log()
    finally:
        h.stop()


def test_the_host_default_is_no_card_for_both(tmp_path):
    h = Harnessd(BIN, tmp_path / "hd", static_id=SID,
                 extra=["--slot-state", str(tmp_path / "slot.state")]).start()
    try:
        assert _usd(h)["present"] is False
        assert _slot(h)["card"] is False
    finally:
        h.stop()


def test_agreeing_spellings_of_one_card_start(tmp_path):
    make_image(tmp_path, "a", 3000, 1)
    card = make_card(tmp_path, tmp_path / "a.img")
    link = tmp_path / "by-id-link"
    link.symlink_to(card)
    h = Harnessd(BIN, tmp_path / "hd", static_id=SID,
                 extra=["--card", str(card), "--usd-dev", str(link), "--slot-disk", str(card),
                        "--slot-state", str(tmp_path / "slot.state")]).start()
    try:
        assert _wait_usd(h)["present"] is True and _slot(h)["card"] is True
    finally:
        h.stop()


@pytest.mark.parametrize("case", ["usd_vs_slot", "card_vs_none", "disk_vs_partition"])
def test_card_flags_naming_different_devices_are_refused(tmp_path, case):
    """Negative control of the above: the store and the slots on two devices would
    each keep to 'their' partitions of a DIFFERENT disk. harnessd must not start."""
    one, two = tmp_path / "one.img", tmp_path / "two.img"
    for f in (one, two):
        with open(f, "wb") as fh:
            fh.truncate(1 << 20)
    argv = {"usd_vs_slot": ["--usd-dev", str(one), "--slot-disk", str(two)],
            "card_vs_none": ["--card", str(one), "--usd-dev", "none"],
            "disk_vs_partition": ["--card", "/dev/mmcblk0", "--slot-disk", "/dev/mmcblk0p4"]}[case]
    r = subprocess.run([BIN, "--loopback", "--port-offset", "40000", *argv],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "name different devices" in r.stderr and "pass --card once" in r.stderr, r.stderr


# --------------------------------------------------------------------------- #
# THE OVERLAP GUARD: malformed MBRs, refused by BOTH writers
# --------------------------------------------------------------------------- #

P3_END = 409600                        # make_card's 200 MiB card, in blocks


def set_entry(card: Path, idx: int, typ: int, start: int, cnt: int) -> None:
    """MBR entry `idx` (0-based) := type/start/count, CHS left as the card had it."""
    with open(card, "r+b") as f:
        f.seek(446 + 16 * idx + 4)
        f.write(bytes([typ]))
        f.seek(446 + 16 * idx + 8)
        f.write(struct.pack("<II", start, cnt))


def card_hash(card: Path) -> str:
    return hashlib.sha256(card.read_bytes()).hexdigest()


#: kind -> (MBR edits, the store's verdict on `format`, slot A / slot B as `status`
#: shows them). A store verdict of "ok" = its 0xDA is fine: this kind is the SLOT
#: side's to refuse, and the store formats inside p4 only.
MALFORMED = {
    "da_from_lba1": ([(3, 0xDA, 1, 67583)], "exists",
                     {"state": "valid"}, {"state": "empty"}),
    "slot_over_da": ([(1, 0x7F, 3000, 50000)], "exists",
                     {"state": "valid"}, {"state": "bad", "err": "overlaps the 0xDA store"}),
    "slot_over_slot": ([(1, 0x7F, 100000, 131072)], "ok",
                       {"state": "bad", "err": "overlaps slot B"},
                       {"state": "bad", "err": "overlaps slot A"}),
    "slot_over_lba2": ([(1, 0x7F, 2, 2000)], "ok",
                       {"state": "valid"}, {"state": "bad", "err": "overlaps LBA 0-2"}),
    "slot_over_persist": ([(1, 0x7F, 300000, 60000)], "ok",
                          {"state": "valid"}, {"state": "bad", "err": "overlaps entry 3 (0x83)"}),
}


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_a_malformed_mbr_is_refused_by_both_writers(tmp_path, kind, mps3_slot_tool):
    edits, store_verdict, want_a, want_b = MALFORMED[kind]
    a = make_image(tmp_path, "a", 3000, 1)
    b = make_image(tmp_path, "b", 5000, 2)
    card = make_card(tmp_path, tmp_path / "a.img")
    for e in edits:
        set_entry(card, *e)
    # healthy=False: never confirmed, so harnessd never stamps slot A's record --
    # the region hashes below are about the two writers' own writes.
    h = Harnessd(BIN, tmp_path / "hd", static_id=SID, healthy=False,
                 extra=["--card", str(card), "--slot-state", str(tmp_path / "slot.state")])
    h.fabric.wr(0x1F000, 0xE00 + S0_BOOTED_FROM, 1)       # stage0 booted slot A
    h.fabric.wr(0x1F000, 0xE00 + S0_IMAGE_HDR_CRC, hdr_crc(a))
    h.start()
    try:
        # ---- the store (D13's classifier) ----
        before = regions(card)
        st = _wait_usd(h)
        assert st["present"] is True and st["state"] == "foreign", (st, h.log())
        fmt = h.req({"op": "usd", "action": "format", "confirm": "erase"})
        if store_verdict == "ok":
            assert fmt == {"ok": True, "state": "empty"}, (fmt, h.log())
            assert moved(before, regions(card)) == {"p4"}     # its own partition only
        else:
            assert fmt == {"ok": False, "err": store_verdict}, (fmt, h.log())
            assert moved(before, regions(card)) == set()

        # ---- the slots (slot_card), through harnessd ----
        whole = card_hash(card)
        sl = _slot(h)
        assert {k: v for k, v in sl["a"].items() if k in ("state", "err")} == want_a, sl
        assert {k: v for k, v in sl["b"].items() if k in ("state", "err")} == want_b, sl
        if want_b["state"] == "bad":
            pslot.push_slot_image(b, "127.0.0.1", static_id=SID, via="tcp", port=h.port(6910))
            deadline = time.time() + 10
            job = _slot(h)["job"]
            while job["state"] in ("writing", "verifying") and time.time() < deadline:
                time.sleep(0.05)
                job = _slot(h)["job"]
            assert job["state"] == "failed" and job["err"] == f"slot B bad: {want_b['err']}", job
            assert _slot(h)["staged"] is None
            assert h.req({"op": "slot", "act": "rollback"}) == \
                {"ok": False, "err": "slot B is not a valid image", "code": "not_valid"}
            assert card_hash(card) == whole                   # not one byte, anywhere
        else:
            # slot B is sound, so the push lands -- but LBA 1-2 sit inside the
            # malformed 0xDA entry, so the boot-select writer refuses the flip
            pslot.push_slot_image(b, "127.0.0.1", static_id=SID, via="tcp", port=h.port(6910))
            job = pslot.wait_job("127.0.0.1", port=h.port(6900), timeout_s=20)
            assert job["job"]["state"] == "ok", job
            before = regions(card)
            r = h.req({"op": "slot", "act": "commit"})
            assert r == {"ok": False, "err": "boot-select refused: LBA 1-2 inside MBR entry 4 "
                                              "(0xDA)", "code": "bootsel"}, r
            assert moved(before, regions(card)) == set()
            assert "slot: WRITE GUARD" not in h.log()        # refused before any write

        # ---- the slots (slot_card), through IMAGE's mps3-slot on the same card ----
        whole = card_hash(card)
        for i, want in (("A", want_a), ("B", want_b)):
            if want["state"] != "bad":
                continue
            w = subprocess.run([str(mps3_slot_tool), "--disk", str(card), "write", i,
                                str(tmp_path / "b.img"), "--force"], capture_output=True, text=True)
            d = subprocess.run([str(mps3_slot_tool), "--disk", str(card), "default", i],
                               capture_output=True, text=True)
            assert w.returncode == 1 and d.returncode == 1, (w.stderr, d.stderr)
        assert card_hash(card) == whole
    finally:
        h.stop()
