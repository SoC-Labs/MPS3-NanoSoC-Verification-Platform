"""test_slot_e2e.py — the v0.14 `slot` verb and the 6910 slot-image push, end to
end: mps3-harnessd (host build, MOCK fabric) over REAL sockets, against a
FILE-BACKED user microSD made by STAGE0's own stage0_mkcard.py (net-protocol.md
"Slot images"; docs/planning/linux_lanes/SLOT_VERB_DRAFT.md).

The card is then judged by three independent readers, none of them this lane's:
  - stage0_mkcard.py check  (STAGE0's normative "what would stage0 boot")
  - stage0_pack.py --check  (the image checker, on the bytes read off the card)
  - IMAGE's mps3-slot status (the admin tool, compiled for the host here)
and by byte comparison: nothing outside the target slot and the boot-select
sectors may change.

The client is the real one: pyverify.slot (push_slot_image / slot_request /
wait_job). Negative controls sit beside every positive: a foreign static_id, a
guard naming the running slot, a corrupt table, a corrupt region, a torn push,
no card, a foreign card, no stage0 block, an unwritable card.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import socket
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

from harnessd_mock import Harnessd  # noqa: E402
from pyverify import slot as pslot  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")

S0 = REPO / "src" / "linux_soc" / "hw" / "fw_stage0"
MPS3_SLOT_PKG = (REPO / "src" / "linux_harness" / "sw" / "br2_external" / "package" /
                 "mps3-slot" / "src")
MPS3_SLOT_SRC = MPS3_SLOT_PKG / "mps3-slot.c"
HD = HERE.parent
SID = 0x5A5A0001
BLK = 512
LBA_A, LBA_B, NBLK = 67584, 198656, 131072     # STAGE0_CONTRACT §6
S0_BOOTED_FROM, S0_IMAGE_HDR_CRC = 0x20, 0x90


# --------------------------------------------------------------------------- #
# fixtures: images, cards, a harnessd that boots "from" a slot
# --------------------------------------------------------------------------- #

def make_image(d: Path, name: str, nbytes: int, seed: int) -> bytes:
    """A real S0LB v2 image, packed by STAGE0's stage0_pack.py (1 region)."""
    rnd = bytes((seed * 131 + i * 7 + (i >> 8)) & 0xFF for i in range(nbytes))
    (d / f"{name}.bin").write_bytes(rnd)
    subprocess.run([sys.executable, str(S0 / "stage0_pack.py"), "--out", str(d / f"{name}.img"),
                    "--pc", "0x80000000", "--a1", "0", "--align", "0x200",
                    f"{d / (name + '.bin')}@0x80000000"], check=True, capture_output=True)
    return (d / f"{name}.img").read_bytes()


def hdr_crc(img: bytes) -> int:
    return struct.unpack_from("<I", img, 28)[0]


def make_card(d: Path, a: Path, b="none", default="A") -> Path:
    card = d / "card.img"
    subprocess.run([sys.executable, str(S0 / "stage0_mkcard.py"), "card", "--slot-a", str(a),
                    "--slot-b", str(b), "--default", default, "--out-dir", str(d / "mk"),
                    "--card-img", str(card), "--card-mib", "200"], check=True, capture_output=True)
    return card


def mkcard_check(card: Path) -> str:
    return subprocess.run([sys.executable, str(S0 / "stage0_mkcard.py"), "check", str(card)],
                          capture_output=True, text=True).stdout


def read_slot(card: Path, lba: int, n: int) -> bytes:
    with open(card, "rb") as f:
        f.seek(lba * BLK)
        return f.read(n)


def bootsel_copies(card: Path) -> set:
    """{(seq, default)} of the VALID boot-select copies at LBA 1 and 2."""
    out = set()
    with open(card, "rb") as f:
        for lba in (1, 2):
            f.seek(lba * BLK)
            c = f.read(BLK)
            magic, ver, seq, dflt = struct.unpack_from("<4I", c)
            if magic == 0x43423053 and ver == 1 and dflt in (1, 2) and \
                    zlib.crc32(c[:0x1FC]) & 0xFFFFFFFF == struct.unpack_from("<I", c, 0x1FC)[0]:
                out.add((seq, "AB"[dflt - 1]))
    return out


def region_hashes(card: Path) -> dict:
    """Everything a push/flip must NOT touch, hashed."""
    with open(card, "rb") as f:
        def h(off, n):
            f.seek(off)
            return hashlib.sha256(f.read(n)).hexdigest()
        return {"mbr": h(0, BLK), "gap": h(3 * BLK, (2048 - 3) * BLK),
                "p4": h(2048 * BLK, 65536 * BLK), "slotA": h(LBA_A * BLK, NBLK * BLK),
                "p3": h(329728 * BLK, 4 * 1024 * 1024)}


@pytest.fixture(scope="session")
def mps3_slot_tool(tmp_path_factory):
    """IMAGE's admin tool, built for the host by ITS OWN package Makefile from a
    copy of package/mps3-slot/src -- exactly as Buildroot does (rsync + make
    S0=... HD=...). It reads a card file as happily as /dev/mmcblk0."""
    if shutil.which("gcc") is None or shutil.which("make") is None:
        pytest.skip("no gcc/make for IMAGE's mps3-slot")
    d = tmp_path_factory.mktemp("mps3slot") / "src"
    shutil.copytree(MPS3_SLOT_PKG, d)
    subprocess.run(["make", "-s", "-C", str(d), f"S0={S0}", f"HD={HD}", "all"], check=True)
    return d / "mps3-slot"


def mps3_slot_status(tool: Path, card: Path) -> str:
    return subprocess.run([str(tool), "--disk", str(card), "status"],
                          capture_output=True, text=True, check=True).stdout


class Board:
    """A harnessd whose stage0 block says it booted from `booted_from` with
    `boot_crc`, over `card`, with its slot state in `state`.

    `healthy` is IMAGE's boot-health verdict. It defaults to False here: a boot
    harnessd never confirms is a boot whose slot record it never stamps
    (harnessd_slot_stamp_booted), so the region hashes the push/flip tests take
    around their own writes cannot race a stamp of the running slot. The stamp
    tests pass healthy=True."""

    def __init__(self, tmp: Path, card, *, booted_from=1, boot_crc=0, state=None,
                 s0_garbage=False, name="hd", extra=(), healthy=False, claim=None):
        self.dir = tmp / name
        self.state = state or (tmp / "slot.state")
        self.hd = Harnessd(BIN, self.dir, static_id=SID, s0_garbage=s0_garbage, healthy=healthy,
                           claim=claim, s0=SID if claim is not None else None, extra=[
            "--slot-disk", str(card) if card is not None else "none",
            "--slot-state", str(self.state), *extra])
        if not s0_garbage:
            self.hd.fabric.wr(0x1F000, 0xE00 + S0_BOOTED_FROM, booted_from)
            self.hd.fabric.wr(0x1F000, 0xE00 + S0_IMAGE_HDR_CRC, boot_crc)
        self.hd.start()
        self.host = "127.0.0.1"
        self.ctl = self.hd.port(6900)

    def stop(self):
        self.hd.stop()

    def req(self, act, slot=None):
        obj = {"op": "slot", "act": act}
        if slot:
            obj["slot"] = slot
        return self.hd.req(obj)

    def status(self):
        return pslot.slot_status(self.host, port=self.ctl)

    def push(self, img: bytes, *, sid=SID, slot=None, via="tcp"):
        port = self.hd.port(6910 if via == "tcp" else 69)
        return pslot.push_slot_image(img, self.host, static_id=sid, slot=slot, via=via, port=port)

    def push_raw(self, frame: bytes):
        """A frame pyverify would refuse to build (corrupt on purpose). The harness
        refuses it at the header and closes with the rest unread, so the RST may land
        before the send or the half-close is done (ENOTCONN / ECONNRESET / EPIPE --
        a full `make check` under load, 2026-09-24): the refusal is the point, and the
        verdict is the job status the test reads next. Only the connect must work."""
        with socket.create_connection((self.host, self.hd.port(6910)), timeout=5) as s:
            try:
                s.sendall(frame)
                s.shutdown(socket.SHUT_WR)
                while s.recv(4096):
                    pass
            except OSError:
                pass

    def wait(self, timeout=20.0):
        """The job's final status, failed or not (pslot.wait_job raises on failed)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status()
            if st["job"]["state"] not in ("writing", "verifying"):
                return st
            time.sleep(0.05)
        raise AssertionError(f"job did not finish: {self.status()}")


@pytest.fixture
def imgs(tmp_path):
    a = make_image(tmp_path, "a", 3000, 1)
    b = make_image(tmp_path, "b", 5000, 2)
    c = make_image(tmp_path, "c", 7001, 3)     # odd length: the push pads it
    return {"a": a, "b": b, "c": c, "dir": tmp_path}


@pytest.fixture
def board(tmp_path, imgs):
    card = make_card(tmp_path, tmp_path / "a.img")
    b = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]))
    b.card = card
    yield b
    b.stop()


# --------------------------------------------------------------------------- #
# the golden path
# --------------------------------------------------------------------------- #

def test_status_of_a_fresh_card(board, imgs):
    st = board.status()
    assert list(st) == ["ok", "card", "fabric_sid", "running", "default", "seq", "target",
                        "staged", "a", "b", "job", "claimed", "confirmed"]
    assert st["claimed"] is False                      # nobody claimed this board's SSH
    assert st["confirmed"] is False                    # Board's default: never confirmed
    assert st["card"] is True and st["fabric_sid"] == f"0x{SID:08x}"
    assert (st["running"], st["default"], st["seq"], st["target"], st["staged"]) == \
        ("A", "A", 1, "B", None)
    assert st["a"] == {"state": "valid", "hdr_crc": f"0x{hdr_crc(imgs['a']):08x}",
                       "len": len(imgs["a"]), "verified": "boot"}      # no record: mkcard's
    assert st["b"] == {"state": "empty", "verified": "no"}
    assert st["job"] == {"act": "none", "slot": None, "state": "idle", "got": 0, "len": 0,
                         "err": ""}


def test_push_verify_commit_rollback(board, imgs, mps3_slot_tool):
    before = region_hashes(board.card)
    board.push(imgs["c"])
    st = pslot.wait_job(board.host, port=board.ctl, timeout_s=20)
    c_crc = f"0x{hdr_crc(imgs['c']):08x}"
    assert st["job"]["act"] == "push" and st["job"]["state"] == "ok" and st["job"]["slot"] == "B"
    assert st["b"] == {"state": "valid", "hdr_crc": c_crc, "len": len(imgs["c"]),
                       "sid": f"0x{SID:08x}", "verified": "readback"}
    assert st["staged"] == "B" and st["default"] == "A"

    # the card, judged by the tools that are not this lane's
    raw = read_slot(board.card, LBA_B, len(imgs["c"]))
    assert raw == imgs["c"]                                  # byte for byte (the pad is past it)
    (imgs["dir"] / "readback.img").write_bytes(raw)
    chk = subprocess.run([sys.executable, str(S0 / "stage0_pack.py"), "--check",
                          str(imgs["dir"] / "readback.img")], capture_output=True, text=True)
    assert chk.returncode == 0, chk.stdout
    out = mkcard_check(board.card)
    assert "default slot A" in out and re.search(r"slot B @LBA 198656: OK", out), out
    assert f"header_crc {c_crc}" in mps3_slot_status(mps3_slot_tool, board.card)
    rec = read_slot(board.card, LBA_B + NBLK - 1, BLK)       # the slot record
    assert rec[:4] == b"S0SR" and struct.unpack_from("<III", rec, 8)[:2] == (hdr_crc(imgs["c"]), SID)
    assert zlib.crc32(rec[:508]) & 0xFFFFFFFF == struct.unpack_from("<I", rec, 508)[0]
    after = region_hashes(board.card)
    assert after == before, "a push touched something outside slot B"

    # commit: the default becomes the pushed slot -- stage0 and IMAGE agree.
    # The optional guard names the slot the tool expects; a wrong one is refused.
    assert board.req("commit", "A") == {"ok": False, "err": "slot mismatch: commit would pick B", "code": "slot_mismatch"}
    st = pslot.slot_request(board.host, "commit", port=board.ctl)
    assert (st["default"], st["seq"], st["target"]) == ("B", 2, None)
    assert "stage0 would boot slot B" in mkcard_check(board.card)
    assert "-> default B (seq 2, in use: LBA2)" in mps3_slot_status(mps3_slot_tool, board.card)
    st = pslot.slot_request(board.host, "commit", "B", port=board.ctl)      # idempotent
    assert (st["default"], st["seq"]) == ("B", 2)
    # POWER-SAFE: the copy that was not rewritten still holds the previous pick,
    # so a write torn at any byte falls back to it (STAGE0_CONTRACT §6).
    assert bootsel_copies(board.card) == {(2, "B"), (1, "A")}

    # rollback: back to the running (boot-verified) slot
    st = pslot.slot_request(board.host, "rollback", port=board.ctl)
    assert (st["default"], st["seq"], st["target"]) == ("A", 3, "B")
    assert bootsel_copies(board.card) == {(3, "A"), (2, "B")}     # never the current pick
    assert "stage0 would boot slot A" in mkcard_check(board.card)
    assert region_hashes(board.card) == before               # only LBA 1-2 and slot B moved


def _card_api() -> list:
    """The shared layer's public functions, from its header."""
    return re.findall(r"^\S[^(]*\b(slot_card_\w+)\(", (HD / "slot_card.h").read_text(), re.M)


def test_harnessd_and_mps3_slot_share_one_card_layer(tmp_path, imgs, mps3_slot_tool):
    """ONE card layer (harnessd/slot_card.[ch]) in BOTH writers of the card:
    (1) both binaries define every public slot_card_* function; (2) neither
    front end carries card I/O, a boot-select rule or a loader of its own;
    (3) on the SAME tie-case card (both boot-select copies seq 1: stage0 uses
    LBA 1), the verb's push + commit and the CLI's write + default leave the
    same bytes where stage0 looks."""
    api = [f for f in _card_api() if f not in ("slot_card_off", "slot_card_bytes")]  # inline
    assert {"slot_card_read", "slot_card_bootsel_write", "slot_card_wr",
            "slot_card_flush_uncache", "slot_card_s0_check"} <= set(api)
    for binary in (BIN, str(mps3_slot_tool)):
        defined = set(subprocess.run(["nm", "--defined-only", binary], capture_output=True,
                                     text=True, check=True).stdout.split())
        missing = [f for f in api if f not in defined]
        assert not missing, f"{binary} does not carry the shared layer: {missing}"
    forbidden = re.compile(r"\b(pread|pwrite|fsync|posix_fadvise|s0_bootcfg_pick|s0_mbr_parse|"
                           r"s0_load|BLKFLSBUF)\b")
    for front in (HD / "slot_linux.c", MPS3_SLOT_SRC):
        code = re.sub(r"/\*.*?\*/", "", front.read_text(), flags=re.S)
        hits = sorted(set(forbidden.findall(code)))
        assert not hits, f"{front.name} carries card rules of its own: {hits}"

    d1, d2 = tmp_path / "verb", tmp_path / "cli"
    d1.mkdir()
    d2.mkdir()
    c1, c2 = make_card(d1, imgs["dir"] / "a.img"), make_card(d2, imgs["dir"] / "a.img")
    assert read_slot(c1, 1, 2 * BLK) == read_slot(c2, 1, 2 * BLK)          # the same tie
    assert bootsel_copies(c1) == {(1, "A")}
    b = Board(tmp_path, c1, booted_from=1, boot_crc=hdr_crc(imgs["a"]))
    try:
        b.push(imgs["b"])
        pslot.wait_job(b.host, port=b.ctl, timeout_s=20)
        assert pslot.slot_request(b.host, "commit", port=b.ctl)["default"] == "B"
    finally:
        b.stop()
    cli = [str(mps3_slot_tool), "--disk", str(c2)]
    subprocess.run(cli + ["write", "B", str(imgs["dir"] / "b.img")], check=True, capture_output=True)
    out = subprocess.run(cli + ["default", "B"], check=True, capture_output=True, text=True).stdout
    assert "LBA 2;" in out                                   # the tie: LBA 1 (in use) untouched
    for lba, n in ((0, BLK), (1, 2 * BLK), (LBA_B, len(imgs["b"]))):
        assert read_slot(c1, lba, n) == read_slot(c2, lba, n), f"LBA {lba} differs"
    assert bootsel_copies(c1) == {(2, "B"), (1, "A")}


def test_tftp_push_and_an_odd_length_image(board, imgs):
    board.push(imgs["c"], via="tftp")
    st = pslot.wait_job(board.host, port=board.ctl, timeout_s=20)
    assert st["b"]["hdr_crc"] == f"0x{hdr_crc(imgs['c']):08x}" and st["staged"] == "B"


def test_cli_push_reads_the_provenance_beside_the_image(board, imgs):
    d = imgs["dir"] / "art"
    d.mkdir()
    (d / "linux_slot.img").write_bytes(imgs["b"])
    (d / "version").write_text(f"format=1\nimpl=linux\nstatic_id=0x{SID:08X}\n")
    env = dict(os.environ, PYTHONPATH=str(REPO / "host" / "pyverify"))
    base = [sys.executable, "-m", "pyverify.cli", "slot"]
    ports = ["--host", board.host, "--port", str(board.ctl),
             "--push-port", str(board.hd.port(6910)), "--timeout", "20"]
    r = subprocess.run(base + ["push", str(d / "linux_slot.img")] + ports,
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert '"staged": "B"' in r.stdout and "per " + str(d / "version") in r.stderr
    r = subprocess.run(base + ["commit", "--slot", "B"] + ports, capture_output=True, text=True, env=env)
    assert r.returncode == 0 and '"default": "B"' in r.stdout
    # negative control: an image provisioned for ANOTHER static is refused by the board
    (d / "version").write_text("static_id=0x0BADCAFE\n")
    r = subprocess.run(base + ["rollback"] + ports, capture_output=True, text=True, env=env)
    assert r.returncode == 0
    r = subprocess.run(base + ["push", str(d / "linux_slot.img")] + ports,
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "0x0badcafe != fabric 0x5a5a0001" in r.stderr
    # ...and one with no provenance at all is refused locally, before the network
    (d / "version").unlink()
    r = subprocess.run(base + ["push", str(d / "linux_slot.img")] + ports,
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "provisioned" in r.stderr


# --------------------------------------------------------------------------- #
# refusals: the card is untouched (or, past the table, the slot never valid)
# --------------------------------------------------------------------------- #

def _frame(img: bytes, sid=SID, rm_slot=0) -> bytes:
    pad = (-len(img)) % 4
    p = img + b"\0" * pad
    return struct.pack(">4sHBBIIII", b"MPS3", 1, 2, rm_slot, sid, 0, len(p) // 4,
                       zlib.crc32(p) & 0xFFFFFFFF) + p


@pytest.mark.parametrize("case", ["foreign_sid", "guard_running", "bad_table", "not_s0lb",
                                  "v1", "too_large"])
def test_refused_before_a_byte_reaches_the_card(board, imgs, case):
    slot_b_before = hashlib.sha256(read_slot(board.card, LBA_B, NBLK * BLK)).hexdigest()
    img = bytearray(imgs["b"])
    want = {"foreign_sid": "image for 0x0badcafe != fabric 0x5a5a0001",
            "guard_running": "slot mismatch: the target is B",
            "bad_table": "table CRC", "not_s0lb": "no S0LB magic", "v1": "bad version",
            "too_large": "image too large for slot B"}[case]
    if case == "foreign_sid":
        board.push_raw(_frame(bytes(img), sid=0x0BADCAFE))
    elif case == "guard_running":
        board.push_raw(_frame(bytes(img), rm_slot=1))
    elif case == "bad_table":
        img[32 + 8] ^= 0x01                         # the region length in the entry table
        board.push_raw(_frame(bytes(img)))
    elif case == "not_s0lb":
        board.push_raw(_frame(os.urandom(4096)))
    elif case == "v1":
        img[4] = 1
        board.push_raw(_frame(bytes(img)))
    else:                                           # announces 64 MiB: slot minus record
        hdr = struct.pack(">4sHBBIIII", b"MPS3", 1, 2, 0, SID, 0, (64 << 20) // 4, 0)
        board.push_raw(hdr)
    st = board.wait()
    assert st["job"]["state"] == "failed" and st["job"]["err"] == want, st["job"]
    code = {"foreign_sid": "wrong_static", "guard_running": "slot_mismatch",
            "too_large": "too_large"}.get(case, "bad_image")          # HM_ANSWERS S3
    assert st["job"]["code"] == code, st["job"]
    assert st["b"] == {"state": "empty", "verified": "no"} and st["staged"] is None
    assert hashlib.sha256(read_slot(board.card, LBA_B, NBLK * BLK)).hexdigest() == slot_b_before
    assert board.hd.req({"op": "ping"})["ok"] is True


def test_a_bad_region_or_a_torn_push_never_leaves_a_valid_slot(board, imgs):
    board.push(imgs["b"])
    pslot.wait_job(board.host, port=board.ctl, timeout_s=20)     # B holds a good image
    img = bytearray(imgs["c"])
    img[-10] ^= 0xFF                                  # a payload byte: region CRC
    board.push_raw(_frame(bytes(img)))
    st = board.wait()
    assert st["job"]["err"] == "region 0 CRC" and st["b"]["state"] == "empty"
    assert st["job"]["code"] == "bad_image"
    assert st["staged"] is None                       # the old image's staging died with it
    assert "slot B @LBA 198656: NOT bootable" in mkcard_check(board.card)

    frame = _frame(imgs["c"])                          # torn: the pusher dies mid-image
    with socket.create_connection((board.host, board.hd.port(6910))) as s:
        s.sendall(frame[: len(frame) // 2])
        time.sleep(0.3)
        st = board.status()
        assert st["job"]["state"] == "writing" and 0 < st["job"]["got"] < st["job"]["len"]
        assert board.req("commit") == {"ok": False, "err": "EBUSY", "code": "busy"}
        assert board.req("verify") == {"ok": False, "err": "EBUSY", "code": "busy"}
    st = board.wait()
    assert st["job"]["err"].startswith("torn") and st["b"]["state"] == "empty"
    assert st["job"]["code"] == "torn"


def test_reboot_waits_for_the_card_job(board, imgs):
    """B2 silicon (HM_ANSWERS_2026-09-26 change 6): a `reboot` answered ok while a
    slot job ran, and the next stage0 entry found the card wedged. Now `reboot`
    answers EBUSY for as long as a push (or its read-back) holds the card, touches
    nothing, and is served once the job has ended. The control is the same
    request after the job: ok, with the watchdog bound."""
    frame = _frame(imgs["c"])
    with socket.create_connection((board.host, board.hd.port(6910))) as s:
        s.sendall(frame[: len(frame) // 2])              # the pusher stalls mid-image
        time.sleep(0.3)
        assert board.status()["job"]["state"] == "writing"
        assert board.hd.req({"op": "reboot"}) == {"ok": False, "err": "EBUSY"}
        assert board.hd.req({"op": "reboot"}) == {"ok": False, "err": "EBUSY"}
        assert "reboot: watchdog armed" not in board.hd.log()
    st = board.wait()
    assert st["job"]["state"] == "failed" and st["job"]["err"].startswith("torn")
    r = board.hd.req({"op": "reboot"})
    assert r["ok"] is True and r["in_ms"] > 0, r


# --------------------------------------------------------------------------- #
# the lifecycle rules
# --------------------------------------------------------------------------- #

def test_no_free_slot_until_rollback(board, imgs):
    board.push(imgs["b"])
    pslot.wait_job(board.host, port=board.ctl, timeout_s=20)
    pslot.slot_request(board.host, "commit", port=board.ctl)       # A runs, B is default
    board.push(imgs["c"])
    st = board.wait()
    assert st["job"]["err"] == "no free slot: A runs, B is the default -- rollback first"
    assert st["b"]["hdr_crc"] == f"0x{hdr_crc(imgs['b']):08x}"     # the committed image is intact
    pslot.slot_request(board.host, "rollback", port=board.ctl)
    board.push(imgs["c"])
    assert pslot.wait_job(board.host, port=board.ctl, timeout_s=20)["staged"] == "B"


def test_commit_needs_a_staged_verified_image(board):
    assert board.req("commit") == {"ok": False, "err": "nothing staged: push an image first", "code": "nothing_staged"}
    assert board.req("rollback") == {"ok": False, "err": "slot B is not a valid image", "code": "not_valid"}
    assert board.req("verify") == {"ok": False, "err": "slot B is not a valid image", "code": "not_valid"}
    assert board.req("commit", "C") == {"ok": False, "err": "bad slot", "code": "bad_slot"}
    assert board.req("frobnicate") == {"ok": False, "err": "bad act", "code": "bad_act"}


def test_a_respawn_keeps_what_this_boot_proved(tmp_path, board, imgs):
    board.push(imgs["b"])
    pslot.wait_job(board.host, port=board.ctl, timeout_s=20)
    board.stop()
    again = Board(tmp_path, board.card, booted_from=1, boot_crc=hdr_crc(imgs["a"]),
                  state=board.state, name="hd2")
    try:
        st = again.status()
        assert st["staged"] == "B" and st["b"]["verified"] == "readback"
        assert pslot.slot_request(again.host, "commit", port=again.ctl)["default"] == "B"
    finally:
        again.stop()


def test_a_harnessd_killed_mid_push_leaves_no_valid_slot(tmp_path, board, imgs):
    """SIGKILL mid-push (no abort() runs): the respawn reads the state file, sees a
    `writing` job whose process is gone, and fails it; the slot was never valid."""
    frame = _frame(imgs["c"])
    s = socket.create_connection((board.host, board.hd.port(6910)))
    s.sendall(frame[: len(frame) // 2])
    time.sleep(0.3)
    assert board.status()["job"]["state"] == "writing"
    board.hd.stop(sig=signal.SIGKILL)
    s.close()
    again = Board(tmp_path, board.card, booted_from=1, boot_crc=hdr_crc(imgs["a"]),
                  state=board.state, name="respawn")
    try:
        st = again.status()
        assert st["job"]["state"] == "failed" and st["job"]["err"] == "harnessd restarted mid-push"
        assert st["job"]["code"] == "restarted"
        assert st["b"] == {"state": "empty", "verified": "no"} and st["staged"] is None
        again.push(imgs["c"])                           # and the next push is served
        assert pslot.wait_job(again.host, port=again.ctl, timeout_s=20)["staged"] == "B"
    finally:
        again.stop()


def test_after_a_reboot_verify_rebinds_through_the_slot_record(tmp_path, imgs):
    """Boot 1: from A, push B, commit. Boot 2: from B (a NEW state: /run is
    tmpfs), roll back to A -- which mkcard wrote, so it has no record and cannot
    be bound (boot 1 was never confirmed, so harnessd never stamped A: see
    test_a_confirmed_boot_stamps_its_slot_so_it_can_be_rolled_back_to for the
    other case); then push A properly. Boot 3: from A again, verify B through its
    record and roll back to it."""
    card = make_card(tmp_path, tmp_path / "a.img")
    b1 = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]),
               state=tmp_path / "s1", name="boot1")
    try:
        b1.push(imgs["b"])
        pslot.wait_job(b1.host, port=b1.ctl, timeout_s=20)
        pslot.slot_request(b1.host, "commit", port=b1.ctl)
    finally:
        b1.stop()

    b2 = Board(tmp_path, card, booted_from=2, boot_crc=hdr_crc(imgs["b"]),
               state=tmp_path / "s2", name="boot2")
    try:
        st = b2.status()
        assert (st["running"], st["default"], st["target"], st["staged"]) == ("B", "B", "A", None)
        assert st["b"]["verified"] == "boot" and st["b"]["sid"] == f"0x{SID:08x}"
        assert b2.req("rollback") == {"ok": False, "err": "slot A not verified", "code": "not_verified"}
        assert b2.req("verify")["job"]["state"] == "verifying"
        st = b2.wait()
        assert st["job"] == {"act": "verify", "slot": "A", "state": "failed", "got": 0,
                             "len": len(imgs["a"]), "err": "no slot record: static_id unknown",
                             "code": "no_record"}
        b2.push(imgs["c"])                                   # the target is A now
        st = pslot.wait_job(b2.host, port=b2.ctl, timeout_s=20)
        assert st["staged"] == "A" and st["a"]["sid"] == f"0x{SID:08x}"
    finally:
        b2.stop()

    b3 = Board(tmp_path, card, booted_from=2, boot_crc=hdr_crc(imgs["b"]),
               state=tmp_path / "s3", name="boot3")
    try:
        assert b3.req("rollback") == {"ok": False, "err": "slot A not verified", "code": "not_verified"}
        b3.req("verify", "A")
        st = b3.wait()
        assert st["job"]["state"] == "ok" and st["a"]["verified"] == "readback"
        st = pslot.slot_request(b3.host, "rollback", "A", port=b3.ctl)
        assert st["default"] == "A"
        assert "stage0 would boot slot A" in mkcard_check(card)
    finally:
        b3.stop()


# --------------------------------------------------------------------------- #
# the booted slot's record (HM_ANSWERS_2026-09-26 S1, change 1)
# --------------------------------------------------------------------------- #

S0_ATT_CONFIRM = 0x48
S0_CONFIRM_MAGIC = 0x4B4F3053


def _stamp_verdict(board, timeout=15.0) -> str:
    """harnessd's ONE log line about the booted slot's record: it is written once
    the boot is confirmed (~2-3 s after start), stamped or not. '' = no verdict
    (the boot was never confirmed)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = re.search(r"slot: booted slot (?:record not stamped: .*|[AB] STAMPED .*)", board.hd.log())
        if m:
            return m.group(0)
        if "does not say healthy=1" in board.hd.log():
            return ""
        time.sleep(0.1)
    raise AssertionError("no stamp verdict:\n" + board.hd.log())


def _record(card: Path, lba: int) -> bytes:
    return read_slot(card, lba + NBLK - 1, BLK)


def _slot_a_image_hash(card: Path) -> str:
    """Slot A minus its record sector: what a stamp must never touch."""
    return hashlib.sha256(read_slot(card, LBA_A, (NBLK - 1) * BLK)).hexdigest()


def test_a_confirmed_boot_stamps_its_slot_so_it_can_be_rolled_back_to(tmp_path, imgs,
                                                                     mps3_slot_tool):
    """Boot 1 from A (written by mkcard: no record), healthy: once the boot is
    confirmed to stage0, harnessd stamps A's record -- source 2 "boot", bound to
    the fabric static_id -- touching nothing but that one sector. Boot 1 then
    pushes B and commits. Boot 2 from B: A was not booted this time, yet `verify`
    binds it through the stamped record and `rollback` to it is allowed -- the
    flow that failed before (`no slot record: static_id unknown`)."""
    card = make_card(tmp_path, tmp_path / "a.img")
    before = region_hashes(card)
    a_img = _slot_a_image_hash(card)
    b1 = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]), healthy=True,
               state=tmp_path / "s1", name="boot1")
    try:
        verdict = _stamp_verdict(b1)
        assert "booted slot A STAMPED" in verdict, verdict
        assert b1.hd.fabric.s0(S0_ATT_CONFIRM) == S0_CONFIRM_MAGIC   # the confirm came first
        assert b1.status()["confirmed"] is True                     # ...and the wire says so
        rec = _record(card, LBA_A)
        assert rec[:4] == b"S0SR"
        ver, crc, sid, ln, src = struct.unpack_from("<5I", rec, 4)
        assert (ver, crc, sid, ln, src) == (1, hdr_crc(imgs["a"]), SID, len(imgs["a"]), 2)
        assert rec[24:508] == bytes(484)
        assert zlib.crc32(rec[:508]) & 0xFFFFFFFF == struct.unpack_from("<I", rec, 508)[0]
        after = region_hashes(card)
        assert {k for k in before if before[k] != after[k]} == {"slotA"}   # the record only
        assert _slot_a_image_hash(card) == a_img                         # never image bytes
        assert "slot A @LBA 67584: OK" in mkcard_check(card)             # stage0 still boots it
        st = b1.status()
        assert st["a"] == {"state": "valid", "hdr_crc": f"0x{hdr_crc(imgs['a']):08x}",
                           "len": len(imgs["a"]), "sid": f"0x{SID:08x}", "verified": "boot"}
        b1.push(imgs["b"])
        pslot.wait_job(b1.host, port=b1.ctl, timeout_s=20)
        assert pslot.slot_request(b1.host, "commit", port=b1.ctl)["default"] == "B"
    finally:
        b1.stop()

    b2 = Board(tmp_path, card, booted_from=2, boot_crc=hdr_crc(imgs["b"]),
               state=tmp_path / "s2", name="boot2")
    try:
        assert b2.req("rollback") == {"ok": False, "err": "slot A not verified", "code": "not_verified"}
        b2.req("verify", "A")
        st = b2.wait()
        assert st["job"]["state"] == "ok", st["job"]
        assert st["a"]["verified"] == "readback" and st["a"]["sid"] == f"0x{SID:08x}"
        st = pslot.slot_request(b2.host, "rollback", "A", port=b2.ctl)
        assert st["default"] == "A"
        assert "stage0 would boot slot A" in mkcard_check(card)
    finally:
        b2.stop()


def test_the_stamp_is_idempotent_across_respawns(tmp_path, imgs):
    """A respawn reaches the confirm again (already confirmed) and calls the stamp
    again: the record holds (hdr_crc, fabric static_id), so nothing is written."""
    card = make_card(tmp_path, tmp_path / "a.img")
    b = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]), healthy=True)
    try:
        assert "STAMPED" in _stamp_verdict(b)
    finally:
        b.stop()
    rec, mtime = _record(card, LBA_A), card.stat().st_mtime_ns
    again = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]), healthy=True,
                  state=b.state, name="respawn")
    try:
        assert _stamp_verdict(again) == "slot: booted slot record not stamped: slot A already recorded"
        assert _record(card, LBA_A) == rec and card.stat().st_mtime_ns == mtime
    finally:
        again.stop()


def test_a_stale_record_for_another_static_is_restamped(tmp_path, imgs):
    """A record for the SAME image but ANOTHER static_id (the fabric changed under
    an image whose claim now agrees with it) does not "already hold": the stamp
    replaces it with the fabric's."""
    card = make_card(tmp_path, tmp_path / "a.img")
    stale = bytearray(512)
    struct.pack_into("<4s5I", stale, 0, b"S0SR", 1, hdr_crc(imgs["a"]), 0x0BADCAFE, len(imgs["a"]), 1)
    struct.pack_into("<I", stale, 508, zlib.crc32(bytes(stale[:508])) & 0xFFFFFFFF)
    with open(card, "r+b") as f:
        f.seek((LBA_A + NBLK - 1) * BLK)
        f.write(stale)
    b = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]), healthy=True)
    try:
        assert b.status()["a"]["sid"] == "0x0badcafe"                  # the control
        assert "STAMPED" in _stamp_verdict(b)
        assert struct.unpack_from("<3I", _record(card, LBA_A), 8) == (hdr_crc(imgs["a"]), SID,
                                                                       len(imgs["a"]))
        assert b.status()["a"]["sid"] == f"0x{SID:08x}"
    finally:
        b.stop()


@pytest.mark.parametrize("case", ["not_confirmed", "identity_lock", "boot_crc", "rescue"])
def test_the_stamp_gates(tmp_path, imgs, case):
    """Each gate alone keeps the record sector untouched: a boot never confirmed,
    an identity lock (the image's claim != the fabric), a card image that is not
    the one stage0 booted, a rescue boot. The positive twin is the test above."""
    card = make_card(tmp_path, tmp_path / "a.img")
    rec0 = _record(card, LBA_A)
    kw = {"booted_from": 1, "boot_crc": hdr_crc(imgs["a"]), "healthy": True}
    want = {"not_confirmed": "",
            "identity_lock": "slot: booted slot record not stamped: identity lock: image 0x0badcafe "
                             "!= fabric 0x5a5a0001",
            "boot_crc": "slot: booted slot record not stamped: slot A table CRC "
                        f"0x{hdr_crc(imgs['a']):08x} != booted 0x12345678",
            "rescue": "slot: booted slot record not stamped: running rescue, not a slot"}[case]
    if case == "not_confirmed":
        kw["healthy"] = False
    elif case == "identity_lock":
        kw["claim"] = 0x0BADCAFE
    elif case == "boot_crc":
        kw["boot_crc"] = 0x12345678
    else:
        kw["booted_from"] = 3
    b = Board(tmp_path, card, **kw)
    try:
        assert _stamp_verdict(b) == want, b.hd.log()
        assert _record(card, LBA_A) == rec0
        assert "sid" not in b.status()["a"]
    finally:
        b.stop()


def test_a_rescue_boot_targets_the_slot_that_is_not_the_default(tmp_path, imgs):
    card = make_card(tmp_path, tmp_path / "a.img", default="A")
    b = Board(tmp_path, card, booted_from=3, boot_crc=0x12345678)
    try:
        st = b.status()
        assert (st["running"], st["target"]) == ("rescue", "B")
        assert st["a"]["verified"] == "no"                     # nothing booted from it
        b.push(imgs["b"])
        pslot.wait_job(b.host, port=b.ctl, timeout_s=20)
        assert pslot.slot_request(b.host, "commit", port=b.ctl)["default"] == "B"
    finally:
        b.stop()


# --------------------------------------------------------------------------- #
# no card, a foreign card, no stage0 block, a card fault
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("disk", ["none", "/nonexistent/mmcblk0"])
def test_no_card_is_a_clean_decline(tmp_path, imgs, disk):
    b = Board(tmp_path, disk, booted_from=3)
    try:
        st = b.status()
        assert st == {"ok": True, "card": False, "fabric_sid": f"0x{SID:08x}",
                      "running": "rescue", "staged": None,
                      "job": {"act": "none", "slot": None, "state": "idle", "got": 0,
                              "len": 0, "err": ""}, "claimed": False, "confirmed": False}
        for act in ("commit", "rollback", "verify"):
            assert b.req(act) == {"ok": False, "err": "no card", "code": "no_card"}
        b.push(imgs["b"])
        st = b.wait()
        assert st["job"]["state"] == "failed" and st["job"]["err"] == "no card"
        assert b.hd.req({"op": "ping"})["ok"] is True
    finally:
        b.stop()


def test_a_foreign_card_is_reported_and_never_written(tmp_path, imgs):
    card = tmp_path / "foreign.img"
    with open(card, "wb") as f:
        f.truncate(64 << 20)
        f.seek(510)
        f.write(b"\x55\xaa")                      # an MBR with no stage0 slots (a PC's card)
        f.seek(3 << 20)
        f.write(b"somebody else's filesystem")
    before = hashlib.sha256(card.read_bytes()).hexdigest()
    b = Board(tmp_path, card, booted_from=3)
    try:
        st = b.status()
        assert st["card"] is True and st["a"] == {"state": "absent", "verified": "no"}
        assert st["b"] == {"state": "absent", "verified": "no"}
        b.push(imgs["b"])
        job = b.wait()["job"]
        assert (job["err"], job["code"]) == ("slot B absent", "slot_absent")
        assert b.req("commit") == {"ok": False, "err": "nothing staged: push an image first", "code": "nothing_staged"}
        assert b.req("rollback") == {"ok": False, "err": "slot B is not a valid image", "code": "not_valid"}
    finally:
        b.stop()
    assert hashlib.sha256(card.read_bytes()).hexdigest() == before


def test_no_stage0_block_refuses_every_write(tmp_path, imgs):
    card = make_card(tmp_path, tmp_path / "a.img")
    b = Board(tmp_path, card, s0_garbage=True)
    try:
        st = b.status()
        assert (st["running"], st["target"], st["fabric_sid"]) == ("unknown", None, "0x00000000")
        assert b.req("rollback") == {"ok": False, "err": "no stage0 block", "code": "no_stage0"}
        b.push(imgs["b"], sid=0)
        assert b.wait()["job"]["err"] == "no stage0 block"
        assert "NOT bootable" in mkcard_check(card).split("slot B")[1]
    finally:
        b.stop()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores the read-only mode this fault is modelled with")
def test_a_card_fault_never_breaks_the_running_system(tmp_path, imgs):
    card = make_card(tmp_path, tmp_path / "a.img")
    os.chmod(card, 0o444)                         # the card refuses writes (a locked/failing card)
    b = Board(tmp_path, card, booted_from=1, boot_crc=hdr_crc(imgs["a"]))
    try:
        b.push(imgs["b"])
        st = b.wait()
        assert st["job"]["state"] == "failed" and st["job"]["err"] == "card io"
        assert st["job"]["code"] == "card_io"
        assert b.req("rollback") == {"ok": False, "err": "slot B is not a valid image", "code": "not_valid"}
        # the running system: every other service answers, the loop still kicks
        assert b.hd.req({"op": "ping"})["ok"] is True
        assert b.hd.req({"op": "stats"})["ok"] is True
        k0 = b.hd.fabric.hdr(0x20)
        time.sleep(0.6)
        assert b.hd.fabric.hdr(0x20) > k0, "the watchdog kicks stopped"
    finally:
        b.stop()
        os.chmod(card, 0o644)
