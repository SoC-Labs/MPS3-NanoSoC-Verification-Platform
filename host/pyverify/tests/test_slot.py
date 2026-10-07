"""pyverify.slot + `pyverify slot` (net-protocol.md v0.14 "Slot images"),
board-free. The same client is driven against the REAL harnessd in
src/linux_harness/sw/harnessd/tests/test_slot_e2e.py; this pins what needs no
server: the framing bytes, the local image check, where the provisioned static_id
comes from, and the CLI's exit codes."""
from __future__ import annotations

import json
import socket
import struct
import threading
import zlib
from pathlib import Path

import pytest

from pyverify import cli
from pyverify import slot as pslot

SID = 0x61BC6789


def s0lb(payload: bytes, *, version: int = 2, pc: int = 0x80000000) -> bytes:
    """A 1-region S0LB image, laid out like stage0_pack.py (region at 0x200)."""
    ent = struct.pack("<4I", 0x200, 0x80000000, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    hdr0 = struct.pack("<8I", 0x424C3053, version, 1, pc, 0, 0, 0, 0)
    crc = zlib.crc32(hdr0 + ent) & 0xFFFFFFFF
    hdr = struct.pack("<8I", 0x424C3053, version, 1, pc, 0, 0, 0, crc)
    return hdr + ent + b"\xff" * (0x200 - 48) + payload


def test_frame_is_the_mps3_header_with_kind_2_and_a_word_pad():
    img = s0lb(b"abc")                               # odd length
    f = pslot.frame_slot_image(img, static_id=SID, slot="B")
    magic, ver, kind, rm_slot, sid, rm_id, words, crc = struct.unpack(">4sHBBIIII", f[:24])
    body = f[24:]
    assert (magic, ver, kind, rm_slot, sid, rm_id) == (b"MPS3", 1, 2, 2, SID, 0)
    assert len(body) % 4 == 0 and body[: len(img)] == img and set(body[len(img):]) <= {0}
    assert words * 4 == len(body) and crc == zlib.crc32(body) & 0xFFFFFFFF
    assert struct.unpack_from(">B", f, 7)[0] == 2
    assert pslot.frame_slot_image(img, static_id=SID)[7] == 0          # 0 = "the inactive one"
    with pytest.raises(pslot.SlotError):
        pslot.frame_slot_image(img, static_id=SID, slot="C")


def test_image_info_and_its_refusals():
    img = s0lb(b"payload")
    info = pslot.image_info(img)
    assert info["hdr_crc"] == struct.unpack_from("<I", img, 28)[0] and info["entries"] == 1
    bad = bytearray(img)
    bad[40] ^= 1                                     # an entry byte: the TABLE CRC catches it
    for broken, why in ((b"S0L", "shorter"), (b"\0" * 64, "magic"),
                        (s0lb(b"x", version=1), "version"), (bytes(bad), "table CRC")):
        with pytest.raises(pslot.SlotError, match=why):
            pslot.image_info(broken)


def _args(**kw):
    ns = cli.build_parser().parse_args(["slot", "push", "x.img"])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_the_provisioned_static_id_comes_from_the_image_never_the_board(tmp_path):
    img = tmp_path / "linux_slot.img"
    img.write_bytes(s0lb(b"p"))
    with pytest.raises(ValueError, match="provisioned"):
        cli._provisioned_static_id(img, _args(static_id=None))
    (tmp_path / "version").write_text("format=1\nstatic_id=0x0000ABCD\n")
    assert cli._provisioned_static_id(img, _args(static_id=None)) == (0xABCD, str(tmp_path / "version"))
    (tmp_path / "linux_bundle.json").write_text(json.dumps(
        {"targets": {"ethernet": {"provisioned": {"static_id": "0x61bc6789"}}}}))
    assert cli._provisioned_static_id(img, _args(static_id=None))[0] == SID      # FLOW's wins
    assert cli._provisioned_static_id(img, _args(static_id="0x5"))[0] == 5       # the flag wins


class _OneShot:
    """A 6900 that answers every connection with one fixed line."""

    def __init__(self, reply: dict):
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(8)
        self.port = self.s.getsockname()[1]
        self.reply = (json.dumps(reply) + "\n").encode()
        self.seen = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            with c:
                self.seen.append(c.makefile("rb").readline())
                c.sendall(self.reply)


def test_cli_exit_codes(tmp_path, capsys):
    ok = _OneShot({"ok": True, "card": False, "fabric_sid": "0x61bc6789", "running": "A",
                   "staged": None, "job": {"act": "none", "slot": None, "state": "idle",
                                           "got": 0, "len": 0, "err": ""}})
    assert cli.main(["slot", "status", "--host", "127.0.0.1", "--port", str(ok.port)]) == 0
    assert json.loads(capsys.readouterr().out)["card"] is False
    assert ok.seen[-1] == b'{"op":"slot","act":"status"}\n'

    no = _OneShot({"ok": False, "err": "slot not supported"})      # a bare-metal harness
    assert cli.main(["slot", "commit", "--slot", "B", "--host", "127.0.0.1",
                     "--port", str(no.port)]) == 1
    assert "slot not supported" in capsys.readouterr().err
    assert no.seen[-1] == b'{"op":"slot","act":"commit","slot":"B"}\n'

    img = tmp_path / "linux_slot.img"
    img.write_bytes(b"not an image")
    assert cli.main(["slot", "push", str(img), "--static-id", "0x1", "--host", "127.0.0.1",
                     "--port", str(ok.port)]) == 2                  # refused locally
    assert cli.main(["slot", "push", "--host", "127.0.0.1"]) == 2   # no image named

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()                                                       # nothing listens here
    assert cli.main(["slot", "status", "--host", "127.0.0.1", "--port", str(dead)]) == 3
