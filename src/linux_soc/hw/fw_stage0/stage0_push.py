#!/usr/bin/env python3
"""stage0_push.py -- push a boot image to a board sitting in stage0 RESCUE
(docs/planning/linux_lanes/STAGE0_CONTRACT.md §7), from the hub or from any PC
on the board's link. Stdlib only, Python 3.6+.

    stage0_push.py 192.168.10.101 boot.img        push it; stage0 boots it
    stage0_push.py 192.168.10.101 --status        what stage0 is doing, and why

What it does, in order -- each step refuses rather than guesses:
  1. check the image locally, exactly as stage0 will (stage0_pack.check_image)
  2. ping the board (ICMP: stage0 answers echo)            [--no-ping skips]
  3. TFTP-read "stage0.status": only stage0 answers it. The running harness's
     config_agent listens on the same port 69 and would take a WRQ as an RM
     push, so a board that does not answer as stage0 is NOT pushed to.
  4. TFTP WRQ with blksize=1468 + tsize, lock-step with retransmission, progress
  5. the verdict is in-band: stage0 ACKs the final block only after verifying
     the whole image in DDR, or answers ERROR "image rejected: <why>"

Exit: 0 accepted (stage0 is booting it), 1 the local image is bad, 2 stage0
rejected it (or cannot take one: DDR not calibrated), 3 not stage0 /
unreachable, 4 the transfer failed.
"""
import argparse
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stage0_pack import check_image  # noqa: E402
import stage0_status  # noqa: E402

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR, OP_OACK = 1, 2, 3, 4, 5, 6
STATUS_FILE = "stage0.status"
EXIT_OK, EXIT_IMAGE, EXIT_REJECTED, EXIT_NOT_STAGE0, EXIT_TRANSFER = 0, 1, 2, 3, 4


class Fail(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


def log(quiet, msg):
    if not quiet:
        print(msg, file=sys.stderr)


def tftp_error(pkt):
    code = struct.unpack_from("!H", pkt, 2)[0] if len(pkt) >= 4 else -1
    msg = pkt[4:].split(b"\0", 1)[0].decode("ascii", "replace")
    return code, msg


def ping(host, quiet):
    exe = shutil.which("ping")
    if not exe:
        log(quiet, "stage0_push: no ping binary here; skipping the ICMP check")
        return
    r = subprocess.run([exe, "-c", "1", "-W", "2", host], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        raise Fail(EXIT_NOT_STAGE0, "%s does not answer ping: the board is not up (or not "
                                    "on this link)" % host)
    log(quiet, "stage0_push: %s answers ping" % host)


def read_status(sock, addr, timeout, retries):
    """RRQ stage0.status -> the decoded block. Fail(3) if not stage0."""
    req = struct.pack("!H", OP_RRQ) + STATUS_FILE.encode() + b"\0octet\0"
    for _ in range(retries):
        sock.sendto(req, addr)
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock.settimeout(max(0.01, deadline - time.time()))
            try:
                pkt, src = sock.recvfrom(2048)
            except socket.timeout:
                break
            if src[0] != addr[0] or len(pkt) < 4:
                continue
            op = struct.unpack_from("!H", pkt)[0]
            if op == OP_ERROR:
                code, msg = tftp_error(pkt)
                raise Fail(EXIT_NOT_STAGE0,
                           "%s:%d is not stage0 rescue (TFTP ERROR %d: %s) -- a running harness "
                           "answers here too, and a push would be taken as an RM" %
                           (addr[0], addr[1], code, msg))
            if op == OP_DATA and struct.unpack_from("!H", pkt, 2)[0] == 1:
                sock.sendto(struct.pack("!HH", OP_ACK, 1), src)
                d = stage0_status.decode(pkt[4:].ljust(256, b"\0")) if len(pkt) >= 4 + 256 else None
                if not d or not d["valid"]:
                    raise Fail(EXIT_NOT_STAGE0, "%s answered %s with something that is not a "
                                                "stage0 status block" % (addr[0], STATUS_FILE))
                return d
    raise Fail(EXIT_NOT_STAGE0, "no TFTP answer from %s:%d -- not in stage0 rescue" % addr)


def push(sock, addr, name, img, blksize, timeout, retries, final_timeout, quiet):
    opts = b"blksize\0%d\0tsize\0%d\0" % (blksize, len(img))
    wrq = struct.pack("!H", OP_WRQ) + name.encode() + b"\0octet\0" + opts
    tid = None
    for _ in range(retries):
        sock.sendto(wrq, addr)
        sock.settimeout(timeout)
        try:
            pkt, src = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src[0] != addr[0] or len(pkt) < 2:
            continue
        op = struct.unpack_from("!H", pkt)[0]
        if op == OP_ERROR:
            code, msg = tftp_error(pkt)
            raise Fail(EXIT_REJECTED, "stage0 refused the push: ERROR %d: %s" % (code, msg))
        if op == OP_OACK:
            f = pkt[2:].split(b"\0")
            o = dict(zip([x.decode().lower() for x in f[0::2]], f[1::2]))
            if "blksize" in o:
                got = int(o["blksize"])
                if not 8 <= got <= blksize:
                    raise Fail(EXIT_TRANSFER, "stage0 answered blksize %d (asked %d)" % (got, blksize))
                blksize = got
            else:
                blksize = 512
            tid = src
            break
        if op == OP_ACK and struct.unpack_from("!H", pkt, 2)[0] == 0:
            blksize, tid = 512, src
            break
    if tid is None:
        raise Fail(EXIT_TRANSFER, "no answer to the write request")

    nblocks = len(img) // blksize + 1
    t0 = last = time.time()
    for b in range(1, nblocks + 1):
        chunk = img[(b - 1) * blksize: b * blksize]
        data = struct.pack("!HH", OP_DATA, b & 0xFFFF) + chunk
        final = b == nblocks
        # the final ACK comes only after stage0 has verified the whole image
        per = timeout * 2 if final else timeout
        budget = final_timeout if final else timeout * retries
        deadline = time.time() + budget
        acked = False
        while not acked and time.time() < deadline:
            sock.sendto(data, tid)
            resend = time.time() + per
            while time.time() < min(resend, deadline):
                sock.settimeout(max(0.01, min(resend, deadline) - time.time()))
                try:
                    pkt, src = sock.recvfrom(2048)
                except socket.timeout:
                    break
                if src != tid or len(pkt) < 4:
                    continue
                op, n = struct.unpack_from("!HH", pkt)
                if op == OP_ERROR:
                    code, msg = tftp_error(pkt)
                    raise Fail(EXIT_REJECTED if final or code in (0, 3) else EXIT_TRANSFER,
                               "stage0: ERROR %d: %s" % (code, msg))
                if op == OP_ACK and n == (b & 0xFFFF):
                    acked = True
                    break
        if not acked:
            raise Fail(EXIT_TRANSFER, "block %d of %d never acknowledged%s" %
                       (b, nblocks, " (stage0 verifies before it ACKs the last block)" if final else ""))
        now = time.time()
        if not quiet and (now - last > 0.5 or final):
            done = min(b * blksize, len(img))
            rate = done / max(now - t0, 1e-6) / 1e6
            sys.stderr.write("\r  %6.1f / %.1f MiB  %3d%%  %.2f MB/s   " %
                             (done / 1048576.0, len(img) / 1048576.0, 100 * done // max(len(img), 1), rate))
            sys.stderr.flush()
            last = now
    if not quiet:
        sys.stderr.write("\n")
    return blksize, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("image", nargs="?")
    ap.add_argument("--status", action="store_true", help="only read and print stage0's status")
    ap.add_argument("--json", action="store_true", help="with --status: JSON")
    ap.add_argument("--port", type=int, default=69)
    ap.add_argument("--blksize", type=int, default=1468)
    ap.add_argument("--timeout", type=float, default=1.0, help="per-packet timeout, s")
    ap.add_argument("--retries", type=int, default=8)
    ap.add_argument("--final-timeout", type=float, default=90.0,
                    help="wait for the final ACK (stage0 verifies first), s")
    ap.add_argument("--no-ping", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="push an image that fails the local check (stage0 will still verify)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if not a.status and not a.image:
        ap.error("an image to push, or --status")

    try:
        img = None
        if not a.status:
            img = open(a.image, "rb").read()
            problems, info = check_image(img)
            if problems and not a.force:
                raise Fail(EXIT_IMAGE, "%s is not a bootable stage0 image: %s" % (a.image, "; ".join(problems)))
            if not problems:
                log(a.quiet, "stage0_push: %s: %d B, %d region(s), hdr_crc 0x%08X" %
                    (a.image, len(img), info["entries"], info["header_crc32"]))
        if not a.no_ping:
            ping(a.host, a.quiet)
        addr = (socket.gethostbyname(a.host), a.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", 0))
        st = read_status(sock, addr, a.timeout, max(2, a.retries // 2))
        if a.status:
            print(__import__("json").dumps(st, indent=1, sort_keys=True) if a.json
                  else stage0_status.render(st))
            return EXIT_OK
        log(a.quiet, "stage0_push: %s is stage0 (build 0x%08X, fabric 0x%08X) in rescue: %s" %
            (a.host, st["build_id"], st["fabric_static_id"],
             stage0_status.RR.get(st["rescue_reason"], "?")))
        if st["ddr_calib"] == 2:
            raise Fail(EXIT_REJECTED, "stage0 reports ddr calib fail: it cannot stage an image "
                                      "(reseat the SODIMM, power-cycle)")
        blk, secs = push(sock, addr, os.path.basename(a.image), img, a.blksize, a.timeout,
                         a.retries, a.final_timeout, a.quiet)
        print("accepted: stage0 verified %s (%d B, blksize %d, %.1f s) and is booting it"
              % (a.image, len(img), blk, secs))
        return EXIT_OK
    except Fail as e:
        print("stage0_push: %s" % e, file=sys.stderr)
        return e.code
    except OSError as e:
        print("stage0_push: %s" % e, file=sys.stderr)
        return EXIT_NOT_STAGE0


if __name__ == "__main__":
    sys.exit(main())
