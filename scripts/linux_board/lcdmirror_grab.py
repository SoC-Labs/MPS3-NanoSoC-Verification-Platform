#!/usr/bin/env python3
"""lcdmirror_grab.py -- grab ONE picture of the MPS3 harness's CLCD panel from
the Linux harness's LCD mirror (mps3-lcdmirror, TCP 127.0.0.1:6940 on the
board) and write it as a PNG (or PPM).

    # 1. forward the board's loopback-only port (the SSH claim, S12)
    ssh -N -L 6940:127.0.0.1:6940 root@<board> &
    # 2. grab
    python3 scripts/linux_board/lcdmirror_grab.py --port 6940 -o lcd.png

The wire is net-protocol.md "LCD mirror (TCP 6940)" (LCD_MIRROR_FPGA.md §6.2 +
its six amendments): every message is 'L' 'M' u8 type, u8 rsvd, u32 len (LE),
then len bytes. The board opens with HELLO (JSON); this tool sends KEY and
collects UPDATEs -- ACKing each one, the board keeps at most 2 unACKed -- until
a keyframe has arrived whole AND all 300 16x16 tiles are valid, or --timeout
(counted from HELLO).
Tiles come FILL / PAL1 / PAL2 / RLE16 / RAW, RGB565 little-endian; they are
decoded into a 320x240 frame composited at each snap_last (whole SNAPs only).

A refused or reset connect is retried (--retries, backoff 0.25 s doubling to
2 s): nothing listening yet (the mirror respawning), a reset, EOF before HELLO,
or the board's "busy (2 clients)" line. "not loopback" is NOT retried: it means
the connection did not come through an SSH forward.

Exit status: 0 a full frame written; 3 a partial frame written (--timeout hit
with tiles still invalid -- e.g. the DUT owns the panel, `blind`); 1 no frame
(connect failed, refused, protocol error, no keyframe in time); 2 bad usage.

Stdlib only. Pillow is used for the PNG when importable (--no-pillow forces the
built-in zlib writer, which writes the same pixels).
"""
import argparse
import json
import socket
import struct
import sys
import time
import zlib

W, H, TILE, TX, TY, NTILES = 320, 240, 16, 20, 15, 300
HDR = struct.Struct("<2sBBI")            # 'L','M', type, rsvd, len
UPD = struct.Struct("<IIIIIB38s")        # seq t_ms frames resets status owner valid[38]
TREC = struct.Struct("<HBH")             # idx enc len
T_HELLO, T_UPDATE, T_KEY, T_RATE, T_PING, T_PONG, T_ACK = 0x01, 0x02, 0x10, 0x11, 0x12, 0x13, 0x14
ENC_FILL, ENC_PAL1, ENC_PAL2, ENC_RLE16, ENC_RAW = 0, 1, 2, 3, 4
ST_OWNER = 1 << 2
S_EXACT, S_TEXT_ONLY, S_BLIND = 1 << 16, 1 << 17, 1 << 18
S_KEY, S_KEY_FIRST, S_KEY_LAST, S_SNAP_LAST = 1 << 24, 1 << 25, 1 << 26, 1 << 27
ALL_VALID = (1 << NTILES) - 1
OWNERS = {0: "harness", 1: "dut", 3: "unknown"}

EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3


class GrabError(Exception):
    """A failure that ends the grab (exit 1)."""


class Retryable(GrabError):
    """A connect-time failure worth another attempt."""


class Refused(GrabError):
    """The board's one-line refusal instead of HELLO."""

    def __init__(self, line):
        self.line = line
        try:
            self.reply = json.loads(line)
        except ValueError:
            self.reply = None
        err = self.reply.get("err", "") if isinstance(self.reply, dict) else ""
        self.busy = "busy" in err
        GrabError.__init__(self, line.decode("utf-8", "replace").strip())


# --------------------------------------------------------------------------- #
# the tile codec (decode side)
# --------------------------------------------------------------------------- #
def decode_tile(enc, p):
    """One tile payload -> 256 RGB565 values, row-major (x fastest). Raises
    GrabError on anything the wire does not allow."""
    p = bytes(p)
    n = len(p)
    if enc == ENC_FILL:
        if n != 2:
            raise GrabError("FILL payload of %d B (want 2)" % n)
        return [p[0] | p[1] << 8] * 256
    if enc == ENC_PAL1:
        if n != 36:
            raise GrabError("PAL1 payload of %d B (want 36)" % n)
        pal = struct.unpack_from("<2H", p, 0)
        rows = struct.unpack_from("<16H", p, 4)
        return [pal[(row >> x) & 1] for row in rows for x in range(16)]
    if enc == ENC_PAL2:
        if n != 72:
            raise GrabError("PAL2 payload of %d B (want 72)" % n)
        pal = struct.unpack_from("<4H", p, 0)
        rows = struct.unpack_from("<16I", p, 8)
        return [pal[(row >> (2 * x)) & 3] for row in rows for x in range(16)]
    if enc == ENC_RLE16:
        out, i = [], 0
        while i < n:
            t = p[i]
            i += 1
            if t & 0x80:                                 # a run of (t & 0x7F) + 1
                if i + 2 > n:
                    raise GrabError("RLE16 run truncated")
                out.extend([p[i] | p[i + 1] << 8] * ((t & 0x7F) + 1))
                i += 2
            else:                                        # t + 1 literals
                k = t + 1
                if i + 2 * k > n:
                    raise GrabError("RLE16 literals truncated")
                out.extend(struct.unpack_from("<%dH" % k, p, i))
                i += 2 * k
            if len(out) > 256:
                break
        if len(out) != 256:
            raise GrabError("RLE16 covers %d pixels (want 256)" % len(out))
        return out
    if enc == ENC_RAW:
        if n != 512:
            raise GrabError("RAW payload of %d B (want 512)" % n)
        return list(struct.unpack("<256H", p))
    raise GrabError("unknown tile encoding %d" % enc)


def parse_update(b):
    """An UPDATE body -> (header dict, [(idx, enc, payload), ...])."""
    if len(b) < UPD.size + 6:
        raise GrabError("UPDATE of %d B is too short" % len(b))
    seq, t_ms, frames, resets, status, owner, valid = UPD.unpack_from(b, 0)
    o = UPD.size
    regs = mode = None
    if status & S_KEY_FIRST:
        regs = bytes(b[o:o + 256])
        o += 256
    else:
        (mode,) = struct.unpack_from("<I", b, o)
        o += 4
    (ntiles,) = struct.unpack_from("<H", b, o)
    o += 2
    recs = []
    for _ in range(ntiles):
        if o + TREC.size > len(b):
            raise GrabError("UPDATE seq %d: tile record truncated" % seq)
        idx, enc, ln = TREC.unpack_from(b, o)
        o += TREC.size
        if idx >= NTILES:
            raise GrabError("UPDATE seq %d: tile index %d" % (seq, idx))
        if o + ln > len(b):
            raise GrabError("UPDATE seq %d: tile %d payload truncated" % (seq, idx))
        recs.append((idx, enc, b[o:o + ln]))
        o += ln
    if o != len(b):
        raise GrabError("UPDATE seq %d: %d trailing bytes" % (seq, len(b) - o))
    hdr = {"seq": seq, "t_ms": t_ms, "frames": frames, "resets": resets, "status": status,
           "owner": owner, "valid": int.from_bytes(valid, "little") & ALL_VALID,
           "regs": regs, "mode": mode}
    return hdr, recs


class Frame:
    """The 320x240 RGB565 frame: `live` takes every tile as it arrives, `frame`
    is composited at each snap_last (H1: a viewer shows whole SNAPs only)."""

    def __init__(self):
        self.live = [0] * (W * H)
        self.frame = [0] * (W * H)
        self.valid = 0
        self.last = None
        self.updates = 0
        self.tiles = 0
        self.keyframe_done = False
        self._in_key = False
        self.gaps = 0

    def apply(self, body):
        hdr, recs = parse_update(body)
        for idx, enc, payload in recs:
            px = decode_tile(enc, payload)
            x0, y0 = (idx % TX) * TILE, (idx // TX) * TILE
            for yy in range(TILE):
                base = (y0 + yy) * W + x0
                self.live[base:base + TILE] = px[yy * TILE:(yy + 1) * TILE]
        if self.last is not None and hdr["seq"] != (self.last["seq"] + 1) & 0xFFFFFFFF:
            self.gaps += 1
        st = hdr["status"]
        if st & S_KEY_FIRST:
            self._in_key = True
        if st & S_KEY_LAST and self._in_key:
            self.keyframe_done = True
            self._in_key = False
        if st & S_SNAP_LAST:
            self.frame[:] = self.live
            self.valid = hdr["valid"]
        self.last = hdr
        self.updates += 1
        self.tiles += len(recs)
        return hdr

    def complete(self):
        return self.keyframe_done and self.valid == ALL_VALID

    def valid_count(self):
        return bin(self.valid).count("1")


# --------------------------------------------------------------------------- #
# the connection
# --------------------------------------------------------------------------- #
class Conn:
    def __init__(self, host, port, timeout):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError,
                socket.timeout) as e:
            raise Retryable("connect %s:%d: %s" % (host, port, e))
        except OSError as e:
            raise GrabError("connect %s:%d: %s" % (host, port, e))
        self.sock.settimeout(timeout)
        self.buf = b""
        self.max_msg = 65536

    def _fill(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except (ConnectionResetError, ConnectionAbortedError) as e:
                raise EOFError("connection reset: %s" % e)
            if not chunk:
                raise EOFError("the mirror closed the connection")
            self.buf += chunk

    def take(self, n):
        self._fill(n)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def line(self):
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            self.buf += chunk
        ln, _, self.buf = self.buf.partition(b"\n")
        return ln

    def recv_msg(self):
        magic, typ, _rsvd, n = HDR.unpack(self.take(HDR.size))
        if magic != b"LM":
            raise GrabError("bad magic %r (not the 6940 wire)" % magic)
        if n + HDR.size > self.max_msg:
            raise GrabError("message of %d B > max_msg %d" % (n + HDR.size, self.max_msg))
        return typ, self.take(n)

    def send(self, typ, body=b""):
        self.sock.sendall(HDR.pack(b"LM", typ, 0, len(body)) + body)

    def hello(self):
        """HELLO, or Refused / Retryable (EOF or reset before it)."""
        try:
            self._fill(1)
        except (EOFError, socket.timeout) as e:
            raise Retryable("no HELLO: %s" % e)
        if self.buf[:1] == b"{":
            raise Refused(self.line())
        typ, body = self.recv_msg()
        if typ != T_HELLO:
            raise GrabError("first message type %#x, not HELLO" % typ)
        h = json.loads(body.decode("utf-8"))
        if (h.get("w"), h.get("h"), h.get("tile")) != (W, H, TILE) or h.get("fmt") != "rgb565le":
            raise GrabError("HELLO describes a %sx%s %s panel, tile %s: this tool knows 320x240 "
                            "rgb565le, tile 16" % (h.get("w"), h.get("h"), h.get("fmt"), h.get("tile")))
        self.max_msg = int(h.get("max_msg", 65536))
        return h

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def connect(host, port, retries, timeout, log):
    """Connect and read HELLO, retrying what is worth retrying. Returns
    (Conn, hello, attempts)."""
    delay = 0.25
    attempt = 0
    while True:
        attempt += 1
        c = None
        try:
            c = Conn(host, port, timeout)
            return c, c.hello(), attempt
        except Refused as e:
            if c:
                c.close()
            if not e.busy:
                raise
            why = "refused: %s" % e
        except Retryable as e:
            if c:
                c.close()
            why = str(e)
        if attempt > retries:
            raise GrabError("gave up after %d attempts: %s" % (attempt, why))
        log("attempt %d: %s -- retry in %.2f s" % (attempt, why, delay))
        time.sleep(delay)
        delay = min(2.0, delay * 2)


def grab(host, port, *, timeout=10.0, retries=5, rate=0, log=lambda s: None):
    """Returns (Frame, hello, attempts). Raises GrabError when no keyframe
    arrived at all."""
    conn, hello, attempts = connect(host, port, retries, min(timeout, 5.0), log)
    t_end = time.monotonic() + timeout
    fr = Frame()
    try:
        if rate:
            conn.send(T_RATE, bytes([rate & 0xFF]))
        conn.send(T_KEY)                                 # amendment 6: KEY on start
        while not fr.complete():
            left = t_end - time.monotonic()
            if left <= 0:
                break
            conn.sock.settimeout(max(0.05, left))
            try:
                typ, body = conn.recv_msg()
            except socket.timeout:
                break
            except EOFError as e:
                if fr.keyframe_done:
                    log("connection ended early: %s" % e)
                    break
                raise GrabError("no keyframe: %s" % e)
            if typ in (T_PONG, T_RATE):
                continue
            if typ != T_UPDATE:
                raise GrabError("unexpected message type %#x" % typ)
            hdr = fr.apply(body)
            conn.send(T_ACK, struct.pack("<I", hdr["seq"]))   # H1: at most 2 unACKed
            if fr.gaps and not fr._in_key:
                conn.send(T_KEY)                         # a gap: ask for a keyframe again
                fr.gaps = 0
    finally:
        conn.close()
    if not fr.keyframe_done:
        raise GrabError("no whole keyframe within %.1f s (%d UPDATEs)" % (timeout, fr.updates))
    return fr, hello, attempts


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def rgb888(frame):
    """RGB565 values -> packed RGB888 bytes (5/6-bit channels widened by bit
    replication, so 0x1F -> 0xFF and 0 -> 0)."""
    out = bytearray(len(frame) * 3)
    lut5 = [(v << 3) | (v >> 2) for v in range(32)]
    lut6 = [(v << 2) | (v >> 4) for v in range(64)]
    o = 0
    for v in frame:
        out[o] = lut5[(v >> 11) & 0x1F]
        out[o + 1] = lut6[(v >> 5) & 0x3F]
        out[o + 2] = lut5[v & 0x1F]
        o += 3
    return bytes(out)


def png_bytes(rgb, w=W, h=H):
    """A minimal 8-bit RGB PNG (filter 0 on every row), stdlib zlib only."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ppm_bytes(rgb, w=W, h=H):
    return b"P6\n%d %d\n255\n" % (w, h) + rgb


def write_image(path, frame, fmt, use_pillow=True):
    rgb = rgb888(frame)
    if fmt == "ppm":
        data = ppm_bytes(rgb)
        with open(path, "wb") as f:
            f.write(data)
        return "ppm"
    if use_pillow:
        try:
            from PIL import Image
        except ImportError:
            use_pillow = False
    if use_pillow:
        Image.frombytes("RGB", (W, H), rgb).save(path, format="PNG")
        return "png (Pillow)"
    with open(path, "wb") as f:
        f.write(png_bytes(rgb))
    return "png (zlib)"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Grab one picture of the MPS3 harness CLCD from the LCD mirror "
                    "(6940, reached through `ssh -L 6940:127.0.0.1:6940`).")
    ap.add_argument("--host", default="127.0.0.1", help="the forward's local end [127.0.0.1]")
    ap.add_argument("--port", type=int, default=6940, help="the forward's local port [6940]")
    ap.add_argument("-o", "--out", default="lcd_mirror.png",
                    help="output file; .ppm writes a PPM, anything else a PNG [lcd_mirror.png]")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="seconds to wait for a whole frame once connected [10]")
    ap.add_argument("--retries", type=int, default=5,
                    help="extra connect attempts after a refused/reset/busy connect [5]")
    ap.add_argument("--rate", type=int, default=0,
                    help="ask for this many SNAPs a second (1..30; 0 = the board's default)")
    ap.add_argument("--raw", metavar="FILE",
                    help="also write the frame as raw RGB565 little-endian (153,600 B)")
    ap.add_argument("--no-pillow", action="store_true",
                    help="write the PNG with the built-in zlib writer even if Pillow is there")
    ap.add_argument("--quiet", action="store_true", help="only errors on stderr")
    a = ap.parse_args(argv)

    def log(msg):
        if not a.quiet:
            sys.stderr.write("lcdmirror_grab: %s\n" % msg)

    try:
        fr, hello, attempts = grab(a.host, a.port, timeout=a.timeout, retries=max(0, a.retries),
                                   rate=a.rate, log=log)
    except GrabError as e:
        sys.stderr.write("lcdmirror_grab: FAIL: %s\n" % e)
        return EXIT_ERR
    fmt = "ppm" if a.out.lower().endswith(".ppm") else "png"
    how = write_image(a.out, fr.frame, fmt, use_pillow=not a.no_pillow)
    if a.raw:
        with open(a.raw, "wb") as f:
            f.write(struct.pack("<%dH" % (W * H), *fr.frame))
    st = fr.last["status"] if fr.last else 0
    flags = [n for b, n in ((S_EXACT, "exact"), (S_TEXT_ONLY, "text_only"), (S_BLIND, "blind"))
             if st & b]
    owner = OWNERS.get(fr.last["owner"], "?") if fr.last else "?"
    summary = ("%s: %dx%d %s, %d/%d tiles valid, owner %s, %s, mode %s, static_id %s, "
               "%d UPDATEs / %d tiles, board t_ms %d, %d connect attempt%s"
               % (a.out, W, H, how, fr.valid_count(), NTILES, owner, ",".join(flags) or "-",
                  hello.get("mode"), hello.get("static_id"), fr.updates, fr.tiles,
                  fr.last["t_ms"] if fr.last else 0, attempts, "" if attempts == 1 else "s"))
    if fr.complete():
        log("wrote " + summary)
        return EXIT_OK
    sys.stderr.write("lcdmirror_grab: PARTIAL (timeout before every tile was valid%s): wrote %s\n"
                     % ("; the DUT owns the panel, the sw mirror is blind" if st & S_BLIND else "",
                        summary))
    return EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
