#!/usr/bin/env python3
"""test_tools.py -- the host tools against the REAL stage0 code.

  stage0_pack.py --check    good image / corrupted image / truncated image
  stage0_mkcard.py          card -> test_stage0_card (the C boot order reads it
                            block by block): slot A, default B, A corrupted ->
                            B, no slot B + A corrupted -> rescue; `check` agrees
  stage0_push.py            against tftp_posix (stage0_tftp.c + stage0_ident.c
                            over localhost UDP): a push boots; 1-in-7 loss still
                            boots; a bad-CRC image (--force) is rejected in-band
                            (exit 2); a bad local image never leaves (exit 1);
                            an oversize push is refused (exit 2); a target that
                            answers like config_agent is NOT pushed to (exit 3);
                            silence is exit 3; --status decodes the block;
                            a push and a status read both complete through a
                            stateful-firewall client (single-port TFTP)
  identify (UDP 6899)       the rescue reply parses as JSON, mode "rescue"

Run by test/Makefile with the built binaries in test/work/. Stdlib only.
"""
import json
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
S0 = os.path.dirname(HERE)
WORK = os.path.join(HERE, "work")
PY = sys.executable
PACK = os.path.join(S0, "stage0_pack.py")
MKCARD = os.path.join(S0, "stage0_mkcard.py")
PUSH = os.path.join(S0, "stage0_push.py")
CARD_T = os.path.join(WORK, "test_stage0_card")
POSIX = os.path.join(WORK, "tftp_posix")

sys.path.insert(0, S0)
import stage0_status  # noqa: E402

# A stateful host firewall (the hub, B1 2026-09-24) accepts only replies from the
# (ip, port) the client sent to. A connected UDP socket is exactly that filter:
# the kernel drops every datagram from any other source. stage0_push.py runs
# unmodified; only its sockets are connected on their first send.
FIREWALLED = r'''
import runpy, socket, sys
class Stateful(socket.socket):
    def sendto(self, data, *a):
        if self.type == socket.SOCK_DGRAM and getattr(self, "_peer", None) is None:
            self._peer = a[-1]
            self.connect(self._peer)
        return super().sendto(data, *a)
socket.socket = Stateful
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def run(*args, **kw):
    return subprocess.run(list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kw)


def free_base():
    """A base port with PORT..PORT+2 and PORT+16..PORT+16+1023 free, roughly."""
    for _ in range(50):
        base = random.randrange(20000, 28000, 16)
        socks = []
        try:
            for p in (base, base + 1, base + 2):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind(("127.0.0.1", p))
                socks.append(s)
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RuntimeError("no free UDP port base")


class Tools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="s0tools_", dir=WORK)
        rnd = random.Random(1234)
        cls.pay = os.path.join(cls.tmp, "payload.bin")
        with open(cls.pay, "wb") as fh:
            fh.write(bytes(rnd.getrandbits(8) for _ in range(700001)))
        cls.pay2 = os.path.join(cls.tmp, "payload2.bin")
        with open(cls.pay2, "wb") as fh:
            fh.write(bytes(rnd.getrandbits(8) for _ in range(50000)))
        cls.img = cls.pack("boot.img", cls.pay)
        cls.img_b = cls.pack("boot_b.img", cls.pay2)
        data = bytearray(_read(cls.img))
        data[4096 + 1000] ^= 0x01
        cls.bad = os.path.join(cls.tmp, "bad.img")
        _write(cls.bad, data)

    @classmethod
    def pack(cls, name, payload):
        out = os.path.join(cls.tmp, name)
        r = run(PY, PACK, "--out", out, "--pc", "0x80000000", "--a1", "0",
                "%s@0x80000000" % payload)
        assert r.returncode == 0, r.stdout + r.stderr
        return out

    # ---- pack --check ---------------------------------------------------------------
    def test_pack_check(self):
        self.assertEqual(run(PY, PACK, "--check", self.img).returncode, 0)
        r = run(PY, PACK, "--check", self.bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("payload CRC", r.stdout)
        trunc = os.path.join(self.tmp, "trunc.img")
        _write(trunc, _read(self.img)[:300000])
        self.assertIn("truncated", run(PY, PACK, "--check", trunc).stdout)

    # ---- mkcard <-> the C reader --------------------------------------------------------
    def card(self, name, *extra):
        d = os.path.join(self.tmp, name)
        ci = os.path.join(self.tmp, name + ".img")
        r = run(PY, MKCARD, "card", "--out-dir", d, "--card-img", ci, "--card-mib", "256", *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return ci

    def boots(self, card_img, expect, payload=None):
        args = [CARD_T, card_img, expect]
        if payload:
            args.append("%s@0x80000000" % payload)
        r = run(*args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        chk = run(PY, MKCARD, "check", card_img)
        if expect == "NONE":
            self.assertNotEqual(chk.returncode, 0, chk.stdout)
        else:
            self.assertIn("would boot slot %s" % expect, chk.stdout)

    def test_card_slot_a(self):
        self.boots(self.card("c1", "--slot-a", self.img), "A", self.pay)

    def test_card_default_b(self):
        self.boots(self.card("c2", "--slot-a", self.img, "--slot-b", self.img_b,
                             "--default", "B", "--seq", "3"), "B", self.pay2)

    def test_card_a_corrupt_falls_to_b(self):
        ci = self.card("c3", "--slot-a", self.img, "--slot-b", self.img_b)
        with open(ci, "r+b") as fh:
            fh.seek(67584 * 512 + 4096 + 77)
            b = fh.read(1)
            fh.seek(-1, 1)
            fh.write(bytes([b[0] ^ 0x80]))
        self.boots(ci, "B", self.pay2)

    def test_card_no_b_a_corrupt_rescue(self):
        ci = self.card("c4", "--slot-a", self.img, "--slot-b", "none")
        with open(ci, "r+b") as fh:
            fh.seek(67584 * 512)
            fh.write(b"\0\0\0\0")
        self.boots(ci, "NONE")

    def test_card_refuses_bad_image(self):
        r = run(PY, MKCARD, "card", "--out-dir", os.path.join(self.tmp, "c5"), "--slot-a", self.bad)
        self.assertNotEqual(r.returncode, 0)

    def test_bootsel_torn_copy(self):
        ci = self.card("c6", "--slot-a", self.img, "--slot-b", self.img_b, "--default", "B")
        bs = os.path.join(self.tmp, "bs.bin")
        self.assertEqual(run(PY, MKCARD, "bootsel", "--default", "A", "--seq", "9",
                             "--copy", "1", "--out", bs).returncode, 0)
        sec = bytearray(_read(bs))
        self.assertEqual(len(sec), 512)
        with open(ci, "r+b") as fh:          # the newer copy says A...
            fh.seek(2 * 512)
            fh.write(sec)
        self.boots(ci, "A", self.pay)
        sec[100] ^= 1                        # ...torn: back to the older copy (B)
        with open(ci, "r+b") as fh:
            fh.seek(2 * 512)
            fh.write(sec)
        self.boots(ci, "B", self.pay2)

    # ---- push against the real server ------------------------------------------------------
    def server(self, *extra, timeout_s=20):
        base = free_base()
        p = subprocess.Popen([POSIX, "--base", str(base), "--timeout-s", str(timeout_s)] + list(extra),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        line = p.stdout.readline()
        self.assertTrue(line.startswith("READY"), line)
        return p, base

    def push(self, base, *args):
        return run(PY, PUSH, "127.0.0.1", *args, "--port", str(base), "--no-ping",
                   "--timeout", "0.3", "--quiet")

    def finish(self, p):
        out, err = p.communicate(timeout=30)
        return p.returncode, out, err

    def hdr_crc(self, path):
        return struct.unpack_from("<I", _read(path), 28)[0]

    def test_push_ok(self):
        p, base = self.server()
        r = self.push(base, self.img)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("accepted", r.stdout)
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0, out)
        self.assertIn("hdr_crc=0x%08X" % self.hdr_crc(self.img), out)

    def test_push_with_loss(self):
        p, base = self.server("--drop-every", "7")
        r = self.push(base, self.img_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0, out)

    def test_push_bad_crc_rejected_in_band(self):
        p, base = self.server(timeout_s=6)
        r = self.push(base, self.bad, "--force")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("payload CRC", r.stderr)
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 3, "stage0 must stay in rescue: " + out)
        self.assertIn("rescue_state=4", out)

    def test_push_bad_local_image_never_sent(self):
        r = run(PY, PUSH, "127.0.0.1", self.bad, "--port", "9", "--no-ping")
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_push_oversize_refused(self):
        p, base = self.server("--stage-max", "100000", timeout_s=6)
        r = self.push(base, self.img)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("too large", r.stderr)
        self.finish(p)

    def test_push_status(self):
        p, base = self.server(timeout_s=5)
        r = self.push(base, "--status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d["valid"])
        self.assertEqual(d["fabric_static_id"], 0x3F1A560F)
        self.assertEqual(d["rescue_reason"], 2)
        self.finish(p)

    def test_push_through_a_stateful_firewall(self):
        p, base = self.server()
        r = run(PY, "-c", FIREWALLED, PUSH, "127.0.0.1", self.img_b, "--port", str(base),
                "--no-ping", "--timeout", "0.3", "--quiet")
        self.assertEqual(r.returncode, 0, "a firewalled client must complete a push: "
                         + r.stdout + r.stderr)
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0, out)

    def test_status_through_a_stateful_firewall(self):
        p, base = self.server(timeout_s=5)
        r = run(PY, "-c", FIREWALLED, PUSH, "127.0.0.1", "--status", "--json", "--port", str(base),
                "--no-ping", "--timeout", "0.3", "--quiet")
        self.assertEqual(r.returncode, 0, "a firewalled client must read the status: " + r.stderr)
        self.assertTrue(json.loads(r.stdout)["valid"])
        self.finish(p)

    def test_push_refuses_non_stage0(self):
        """A board running the harness answers RRQ on 69 with ERROR 4 (config_agent)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        seen = []

        def serve():
            s.settimeout(3)
            try:
                while True:
                    pkt, src = s.recvfrom(2048)
                    seen.append(struct.unpack_from("!H", pkt)[0])
                    s.sendto(b"\0\5\0\4reads not supported (push-only server)\0", src)
            except OSError:
                pass
        t = threading.Thread(target=serve)
        t.start()
        r = self.push(port, self.img)
        s.close()
        t.join()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn(2, seen, "a WRQ reached a non-stage0 server")

    def test_push_silence(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        r = self.push(s.getsockname()[1], self.img)
        s.close()
        self.assertEqual(r.returncode, 3)

    def test_identify(self):
        p, base = self.server(timeout_s=4)
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(2)
        c.sendto(json.dumps({"op": "identify", "v": 1, "nonce": "0011aabbccddeeff"}).encode(),
                 ("127.0.0.1", base + 1))
        data, _ = c.recvfrom(2048)
        d = json.loads(data.decode())
        self.assertEqual((d["ok"], d["mode"], d["board"], d["nonce"]),
                         (True, "rescue", "mps3", "0011aabbccddeeff"))
        self.assertEqual(d["ports"], {"tftp": 69})
        self.assertEqual(d["shell_id"], "0x3f1a560f")
        self.assertEqual(d["reason"], "no card")
        self.assertNotIn("ctrl", d["ports"])
        c.sendto(b'{"op":"identify","v":1,"nonce":"xyz"}', ("127.0.0.1", base + 1))
        with self.assertRaises(socket.timeout):
            c.recvfrom(2048)
        c.close()
        self.finish(p)

    def test_status_decoder(self):
        blk = bytearray(256)
        for name, off in stage0_status.FIELDS:
            struct.pack_into("<I", blk, off, off)
        struct.pack_into("<I", blk, 0, stage0_status.MAGIC)
        struct.pack_into("<I", blk, 4, 1)
        struct.pack_into("<I", blk, 8, 256)
        struct.pack_into("<I", blk, 0xFC, stage0_status.MAGIC)
        d = stage0_status.decode(bytes(blk))
        self.assertTrue(d["valid"])
        self.assertEqual(d["att_confirm"], 0x48)
        blk[0xFC] ^= 1
        self.assertFalse(stage0_status.decode(bytes(blk))["valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
