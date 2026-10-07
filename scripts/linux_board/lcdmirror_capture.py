#!/usr/bin/env python3
"""lcdmirror_capture.py -- GOLDEN FIXTURES for Harness Manager's LCD-reflector
replay test: ONE LCD-mirror connection (6940), recorded byte for byte, plus the
picture lcdmirror_grab.py would write at chosen UPDATE seqs -- the key frame, the
apps page, the status page, and frames in the middle of a DFX swap.

HM's test: feed stream.bin through HM's decoder up to a scene's stream_end; its
frame must equal the scene's PNG (and .rgb565), pixel for pixel. Format:
docs/planning/linux_lanes/LCDMIRROR_GOLDEN_CAPTURE.md ("mps3-lcdmirror-capture/1").

Run it ON THE HUB against a claimed Linux board, through ONE ssh forward that
carries both loopback-only ports (6940 is 127.0.0.1-only on the board; `panel
page` on 6900 is claim-locked, so it must arrive from the board's own loopback):

    ssh -f -N -o ExitOnForwardFailure=yes -o BatchMode=yes \\
        -L 127.0.0.1:26940:127.0.0.1:6940 -L 127.0.0.1:26900:127.0.0.1:6900 root@192.168.10.102
    python3.11 lcdmirror_capture.py --port 26940 --ctrl-port 26900 --out DIR \\
        --swap-cmd "python3.11 -m pyverify.cli deploy --host 192.168.10.102 --overlay ovl/led --no-persist"

WHY THE PAGE COMMANDS: the software mirror sends only tiles that CHANGED. A KVM
flip repaints identical content (0 changed tiles), so every scene is made by a
content change: `panel page apps` / `panel page status` (net-protocol v0.17), and
the swap (the aligned panel's R5 progress on rows 3 and 10). If the board is on
the apps page at the start, it is sent to status first, so "apps" is a real
repaint.

A scene's frame is the frame composited at a snap_last UPDATE (H1: whole SNAPs
only) once the repaint burst (an UPDATE carrying >= --big tiles) has been
followed by --settle seconds with no burst. The heartbeat (4 Hz) and the uptime
(1 Hz) keep ticking, so a scene is "the page + that moment's ticks": the PNG and
the stream agree at THAT seq, which is all the replay test needs.

Mid-swap: --swap-cmd runs while the stream is recorded (its 6900 connection is
its own; this tool sends nothing on 6900 meanwhile). Every snap_last during it
whose SNAP changed a tile on text row 3 or 10 (the progress line and bar) is a
candidate; the first, middle and last are saved as midswap_1..3. With none (a
swap too fast for the frame rate, or the today theme), the snaps that changed
any row but 6/14 (uptime, heartbeat) are used instead, and the manifest says
so. Then "after_swap" once the panel settles.

Stdlib only (the hub's /usr/bin/python3.11); the decoder, the frame and the PNG
writer are lcdmirror_grab.py's own (imported from beside this file), so the PNG
is byte for byte what `lcdmirror_grab.py --no-pillow` writes for that frame.

    lcdmirror_capture.py --verify DIR   re-read DIR/stream.bin from scratch and
                                        check every scene against its .rgb565

Exit: 0 every scene asked for captured and verified; 1 failure (connect,
protocol, refused page change, no repaint, verify mismatch); 2 bad usage;
3 some scenes missing (the rest written and verified).
"""
import argparse
import hashlib
import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import time
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lcdmirror_grab as g  # noqa: E402

FORMAT = "mps3-lcdmirror-capture/1"
TOOL = "lcdmirror_capture 1.0 (2026-09-29)"
SCENES = ("key", "apps", "status", "midswap")
PROGRESS_ROWS = (3, 10)            # aligned theme: the prog line, the R5 bar
TICK_ROWS = (6, 14)                # uptime, heartbeat: tick with no page change
EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3


class CaptureError(Exception):
    pass


# --------------------------------------------------------------------------- #
# the recording connection: lcdmirror_grab.Conn, every received byte teed
# --------------------------------------------------------------------------- #
class RecordingConn(g.Conn):
    def __init__(self, host, port, timeout):
        self.rx = bytearray()          # every byte the board sent, in order
        self.tx = bytearray()          # every byte this tool sent, in order
        self.consumed = 0              # stream offset of the next unread byte
        g.Conn.__init__(self, host, port, timeout)

    def _recv(self, n):
        try:
            chunk = self.sock.recv(n)
        except (ConnectionResetError, ConnectionAbortedError) as e:
            raise EOFError("connection reset: %s" % e)
        self.rx += chunk
        return chunk

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self._recv(65536)
            if not chunk:
                raise EOFError("the mirror closed the connection")
            self.buf += chunk

    def take(self, n):
        out = g.Conn.take(self, n)
        self.consumed += n
        return out

    def recv_msg(self):
        """One WHOLE message or nothing: the header is only peeked until the body
        is here too, so a timeout mid-message (short timeouts, a big key frame)
        never loses the stream's framing."""
        self._fill(g.HDR.size)
        magic, typ, _rsvd, n = g.HDR.unpack_from(self.buf, 0)
        if magic != b"LM":
            raise g.GrabError("bad magic %r (not the 6940 wire)" % magic)
        if n + g.HDR.size > self.max_msg:
            raise g.GrabError("message of %d B > max_msg %d" % (n + g.HDR.size, self.max_msg))
        self._fill(g.HDR.size + n)
        return typ, self.take(g.HDR.size + n)[g.HDR.size:]

    def line(self):
        while b"\n" not in self.buf:
            try:
                chunk = self._recv(4096)
            except (OSError, EOFError):
                chunk = b""
            if not chunk:
                break
            self.buf += chunk
        ln, _, self.buf = self.buf.partition(b"\n")
        self.consumed += len(ln) + 1
        return ln

    def send(self, typ, body=b""):
        msg = g.HDR.pack(b"LM", typ, 0, len(body)) + body
        self.sock.sendall(msg)
        self.tx += msg


def connect(host, port, retries, timeout, log):
    """lcdmirror_grab.connect's retry rules, with a fresh recording per attempt."""
    delay, attempt = 0.25, 0
    while True:
        attempt += 1
        c = None
        try:
            c = RecordingConn(host, port, timeout)
            return c, c.hello()
        except g.Refused as e:
            if c:
                c.close()
            if not e.busy:
                raise CaptureError("refused: %s" % e)
            why = "refused: %s" % e
        except g.Retryable as e:
            if c:
                c.close()
            why = str(e)
        except g.GrabError as e:
            if c:
                c.close()
            raise CaptureError(str(e))
        if attempt > retries:
            raise CaptureError("gave up after %d attempts: %s" % (attempt, why))
        log("mirror attempt %d: %s -- retry in %.2f s" % (attempt, why, delay))
        time.sleep(delay)
        delay = min(2.0, delay * 2)


# --------------------------------------------------------------------------- #
# 6900: one request per connection (single-client; adopted with a ping first)
# --------------------------------------------------------------------------- #
def ctrl(host, port, obj, timeout=10.0):
    deadline = time.monotonic() + timeout
    last = "no attempt"
    while time.monotonic() < deadline:
        s = f = None
        try:
            s = socket.create_connection((host, port), timeout=5.0)
            f = s.makefile("rb")
            s.sendall(b'{"op":"ping"}\n')
            if f.readline():                         # adopted: this client holds 6900
                s.sendall(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
                ln = f.readline()
                if ln:
                    return json.loads(ln)
                last = "6900 closed before the reply"
            else:
                last = "6900 accepted, then closed (another client holds it)"
        except (OSError, ValueError) as e:
            last = "%s: %s" % (type(e).__name__, e)
        finally:
            for x in (f, s):
                try:
                    if x is not None:
                        x.close()
                except OSError:
                    pass
        time.sleep(0.1)
    raise CaptureError("6900 %s:%d never answered %s: %s" % (host, port, obj, last))


# --------------------------------------------------------------------------- #
# the capture
# --------------------------------------------------------------------------- #
class Capture:
    def __init__(self, a, log):
        self.a = a
        self.log = log
        self.conn = None
        self.hello = None
        self.fr = g.Frame()
        self.index = []               # one entry per UPDATE (and PONG / RATE echo)
        self.snap_rows = set()        # tile rows changed since the last snap_last
        self.scenes = []
        self.notes = []
        self.panel = {}
        self.t0 = time.monotonic()

    # -- the stream ---------------------------------------------------------- #
    def pump_one(self, timeout):
        """One message (None on a quiet timeout). Returns the index entry."""
        c = self.conn
        c.sock.settimeout(max(0.02, timeout))
        off = c.consumed
        try:
            typ, body = c.recv_msg()
        except socket.timeout:
            return None
        except EOFError as e:
            raise CaptureError("the mirror ended the stream: %s" % e)
        except g.GrabError as e:
            raise CaptureError(str(e))
        ent = {"off": off, "len": g.HDR.size + len(body), "type": typ,
               "t_host": round(time.monotonic() - self.t0, 4)}
        if typ == g.T_UPDATE:
            hdr = self.fr.apply(body)
            _hdr, recs = g.parse_update(body)
            c.send(g.T_ACK, struct.pack("<I", hdr["seq"]))   # H1: at most 2 unACKed
            rows = sorted({idx // g.TX for idx, _e, _p in recs})
            self.snap_rows.update(rows)
            st = hdr["status"]
            ent.update(seq=hdr["seq"], t_ms=hdr["t_ms"], tiles=len(recs), rows=rows,
                       key=bool(st & g.S_KEY), key_first=bool(st & g.S_KEY_FIRST),
                       key_last=bool(st & g.S_KEY_LAST), snap_last=bool(st & g.S_SNAP_LAST),
                       owner=hdr["owner"], valid=bin(hdr["valid"]).count("1"))
            if ent["snap_last"]:
                ent["snap_rows"] = sorted(self.snap_rows)
                self.snap_rows = set()
            if self.fr.gaps and not self.fr._in_key:
                c.send(g.T_KEY)                              # a gap: key again (grab's rule)
                self.fr.gaps = 0
                self.notes.append("a seq gap at %d: KEY sent" % hdr["seq"])
        self.index.append(ent)
        return ent

    def pump_until(self, pred, timeout, what):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CaptureError("%s: nothing in %.0f s" % (what, timeout))
            ent = self.pump_one(left)
            if ent is not None and pred(ent):
                return ent

    def last_snap(self):
        for ent in reversed(self.index):
            if ent.get("snap_last"):
                return ent
        return None

    def settle(self, since_t, what, need_burst=True):
        """Wait for the repaint burst after since_t (>= --big tiles in one UPDATE),
        then --settle quiet seconds with no burst. Returns the last snap_last entry."""
        a = self.a
        deadline = time.monotonic() + a.timeout
        burst_seen = not need_burst
        last_burst = time.monotonic()
        while True:
            now = time.monotonic()
            if now > deadline:
                raise CaptureError("%s: %s in %.0f s" % (
                    what, "no settle" if burst_seen else
                    "no repaint (0 changed tiles? a flip repaints identical content)", a.timeout))
            if burst_seen and now - last_burst >= a.settle:
                snap = self.last_snap()
                if snap is not None and snap["t_host"] >= since_t:
                    return snap
            ent = self.pump_one(0.05)
            if ent is None or ent["type"] != g.T_UPDATE:
                continue
            if ent["t_host"] >= since_t and ent["tiles"] >= a.big:
                burst_seen, last_burst = True, time.monotonic()

    def save(self, name, snap, frame_bytes=None, **extra):
        """A scene: the frame composited at `snap` (the current one, or a copy
        taken at the time), as PNG (lcdmirror_grab's zlib writer) and raw RGB565."""
        a = self.a
        raw = frame_bytes if frame_bytes is not None else array("H", self.fr.frame).tobytes()
        frame = array("H")
        frame.frombytes(raw)
        png = os.path.join(a.out, name + ".png")
        g.write_image(png, list(frame), "png", use_pillow=False)
        with open(os.path.join(a.out, name + ".rgb565"), "wb") as f:
            f.write(raw)
        sc = {"name": name, "seq": snap["seq"], "stream_end": snap["off"] + snap["len"],
              "t_ms": snap["t_ms"], "t_host": snap["t_host"], "owner": snap["owner"],
              "valid_tiles": snap["valid"], "png": name + ".png", "rgb565": name + ".rgb565",
              "frame_sha256": hashlib.sha256(raw).hexdigest()}
        sc.update(extra)
        self.scenes.append(sc)
        self.log("scene %-10s seq %d (stream_end %d, %d tiles valid)" % (
            name, sc["seq"], sc["stream_end"], sc["valid_tiles"]))
        return sc

    # -- the page ------------------------------------------------------------ #
    def page(self, which):
        r = ctrl(self.a.ctrl_host, self.a.ctrl_port, {"op": "panel", "page": which})
        if not r.get("ok"):
            raise CaptureError("panel page %s refused: %s%s" % (
                which, r, " -- the DUT owns the panel: give it back to the harness first"
                if r.get("code") == "held" else ""))
        return r

    # -- the scenes ---------------------------------------------------------- #
    def run(self):
        a = self.a
        self.conn, self.hello = connect(a.host, a.port, a.retries, 5.0, self.log)
        self.log("HELLO: mode %s, static_id %s, max_msg %s" % (
            self.hello.get("mode"), self.hello.get("static_id"), self.hello.get("max_msg")))
        want = [s for s in a.scenes]
        state = None
        if any(s in want for s in ("apps", "status")):
            state = ctrl(a.ctrl_host, a.ctrl_port, {"op": "panel"})
            if not state.get("ok"):
                raise CaptureError("panel state: %s (a v0.17 Linux harness is needed)" % state)
            if state.get("owner") != "harness":
                raise CaptureError("the DUT owns the panel (owner %s): the software mirror is "
                                   "blind -- give the panel back to the harness first"
                                   % state.get("owner"))
            half = ctrl(a.ctrl_host, a.ctrl_port, {"op": "panel", "frame": "a"})
            self.panel = {"theme": half.get("theme"), "page_at_start": state.get("page")}
            self.log("panel: theme %s, page %s" % (half.get("theme"), state.get("page")))
        if a.rate:
            self.conn.send(g.T_RATE, bytes([a.rate & 0xFF]))
        self.conn.send(g.T_KEY)                                  # amendment 6
        self.pump_until(lambda e: e["type"] == g.T_UPDATE and self.fr.complete(),
                        a.timeout, "the key frame")
        if "key" in want:
            self.save("key", self.last_snap(), what="the first whole key frame")
        cur = state.get("page") if state else None
        for which in ("apps", "status"):
            if which not in want:
                continue
            if cur == which:                 # already there: a page change is needed
                other = "status" if which == "apps" else "apps"
                self.log("the board is on the %s page: %s first, so %s is a repaint"
                         % (which, other, which))
                self.flip(other)
            snap = self.flip(which)
            cur = which
            self.save(which, snap, what="`panel page %s` repaint, settled" % which)
        if "midswap" in want:
            if cur != "status":              # the R5 progress is on the status page
                if cur is None:
                    st = ctrl(a.ctrl_host, a.ctrl_port, {"op": "panel"})
                    cur = st.get("page")
                if cur != "status":
                    self.flip("status")
            self.midswap()
        self.conn.close()

    def flip(self, which):
        t = time.monotonic() - self.t0
        self.page(which)
        return self.settle(t, "page %s" % which)

    def midswap(self):
        a = self.a
        if not a.swap_cmd:
            self.notes.append("midswap skipped: no --swap-cmd")
            self.log("midswap: SKIPPED (no --swap-cmd)")
            return
        self.log("midswap: running %s" % a.swap_cmd)
        t_start = time.monotonic() - self.t0
        errf = open(os.path.join(a.out, "swap_cmd.log"), "wb")
        p = subprocess.Popen(a.swap_cmd, shell=True, stdout=errf, stderr=subprocess.STDOUT)
        snaps = []                       # (entry, frame bytes) for every snap_last
        deadline = time.monotonic() + a.swap_timeout
        try:
            while p.poll() is None:
                if time.monotonic() > deadline:
                    p.kill()
                    raise CaptureError("--swap-cmd still running after %.0f s" % a.swap_timeout)
                ent = self.pump_one(0.05)
                if ent is not None and ent.get("snap_last"):
                    snaps.append((ent, array("H", self.fr.frame).tobytes()))
        finally:
            errf.close()
        t_end = time.monotonic() - self.t0
        rc = p.returncode
        self.log("midswap: --swap-cmd rc=%d after %.1f s, %d snaps meanwhile" % (
            rc, t_end - t_start, len(snaps)))
        prog = [s for s in snaps if set(s[0]["snap_rows"]) & set(PROGRESS_ROWS)]
        basis = "a SNAP that changed text row 3 or 10 (the progress line / bar)"
        if not prog:
            prog = [s for s in snaps if set(s[0]["snap_rows"]) - set(TICK_ROWS)]
            basis = "no progress-row SNAP: a SNAP that changed a row other than 6/14"
            self.notes.append("midswap: %s" % basis)
        if not prog:
            self.notes.append("midswap: no content changed during --swap-cmd (rc=%d)" % rc)
            self.log("midswap: NOTHING changed on the panel during the swap")
        else:
            picks = sorted({0, len(prog) // 2, len(prog) - 1})
            for k, i in enumerate(picks, 1):
                ent, fb = prog[i]
                self.save("midswap_%d" % k, ent, fb, what="during --swap-cmd: " + basis,
                          snap_rows=ent["snap_rows"], swap_cmd_rc=rc)
        snap = self.settle(t_end, "after the swap", need_burst=False)
        self.save("after_swap", snap, what="settled after --swap-cmd (rc=%d)" % rc, swap_cmd_rc=rc)

    # -- the files ----------------------------------------------------------- #
    def write(self):
        a = self.a
        c = self.conn
        rx = bytes(c.rx) if c else b""
        with open(os.path.join(a.out, "stream.bin"), "wb") as f:
            f.write(rx)
        with open(os.path.join(a.out, "sent.bin"), "wb") as f:
            f.write(bytes(c.tx) if c else b"")
        man = {
            "format": FORMAT, "tool": TOOL, "git": git_rev(),
            "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mirror": "%s:%d" % (a.host, a.port), "ctrl": "%s:%d" % (a.ctrl_host, a.ctrl_port),
            "argv": sys.argv[1:], "hello": self.hello, "panel": self.panel,
            "stream": {"file": "stream.bin", "bytes": len(rx),
                       "sha256": hashlib.sha256(rx).hexdigest(),
                       "what": "every byte the board sent on this 6940 connection, from the "
                               "first byte of HELLO, unmodified"},
            "sent": {"file": "sent.bin", "bytes": len(c.tx) if c else 0,
                     "what": "every byte this tool sent (RATE, KEY, one ACK per UPDATE)"},
            "frame": {"w": g.W, "h": g.H, "fmt": "rgb565le", "composited_at": "snap_last"},
            "scenes": self.scenes, "notes": self.notes, "messages": self.index,
        }
        with open(os.path.join(a.out, "manifest.json"), "w") as f:
            json.dump(man, f, indent=1)
            f.write("\n")
        return man


def git_rev():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(["git", "-C", here, "describe", "--always", "--dirty"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- #
# --verify: stream.bin from scratch, every scene against its .rgb565
# --------------------------------------------------------------------------- #
def iter_messages(stream):
    """(offset, type, body) for each whole message of a 6940 stream."""
    o = 0
    while o + g.HDR.size <= len(stream):
        magic, typ, _r, n = g.HDR.unpack_from(stream, o)
        if magic != b"LM":
            raise CaptureError("stream offset %d: bad magic %r" % (o, magic))
        if o + g.HDR.size + n > len(stream):
            return                                    # the tail the capture left unread
        yield o, typ, stream[o + g.HDR.size:o + g.HDR.size + n]
        o += g.HDR.size + n


def verify(out, log=print):
    with open(os.path.join(out, "manifest.json")) as f:
        man = json.load(f)
    if man.get("format") != FORMAT:
        raise CaptureError("%s: format %r, not %s" % (out, man.get("format"), FORMAT))
    with open(os.path.join(out, man["stream"]["file"]), "rb") as f:
        stream = f.read()
    if hashlib.sha256(stream).hexdigest() != man["stream"]["sha256"]:
        raise CaptureError("stream.bin does not match the manifest's sha256")
    ends = {sc["stream_end"]: sc for sc in man["scenes"]}
    fr = g.Frame()
    bad, seen = [], 0
    first = True
    for off, typ, body in iter_messages(stream):
        if first:
            if typ != g.T_HELLO:
                raise CaptureError("the stream does not start with HELLO")
            first = False
            continue
        if typ == g.T_UPDATE:
            hdr = fr.apply(body)
            sc = ends.get(off + g.HDR.size + len(body))
            if sc is not None:
                seen += 1
                got = array("H", fr.frame).tobytes()
                with open(os.path.join(out, sc["rgb565"]), "rb") as f:
                    want = f.read()
                ok = hdr["seq"] == sc["seq"] and got == want \
                    and hashlib.sha256(want).hexdigest() == sc["frame_sha256"]
                log("verify %-10s seq %d: %s" % (sc["name"], sc["seq"], "OK" if ok else "MISMATCH"))
                if not ok:
                    bad.append(sc["name"])
    if seen != len(man["scenes"]):
        bad.append("%d of %d scenes found in the stream" % (seen, len(man["scenes"])))
    if bad:
        raise CaptureError("verify FAILED: %s" % ", ".join(bad))
    return len(man["scenes"])


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1", help="the 6940 forward's local end [127.0.0.1]")
    ap.add_argument("--port", type=int, default=6940, help="the 6940 forward's local port [6940]")
    ap.add_argument("--ctrl-host", default="127.0.0.1", help="the 6900 forward's local end")
    ap.add_argument("--ctrl-port", type=int, default=6900, help="the 6900 forward's local port "
                    "(it must reach the board's loopback: `panel page` is claim-locked) [6900]")
    ap.add_argument("--out", help="output directory (created; refused if it exists and is not empty)")
    ap.add_argument("--scenes", default=",".join(SCENES),
                    help="comma list of %s [all]" % ",".join(SCENES))
    ap.add_argument("--swap-cmd", default="", help="a shell command that performs one DFX swap "
                    "(run while the stream is recorded; needed for midswap)")
    ap.add_argument("--swap-timeout", type=float, default=900.0, help="seconds [900]")
    ap.add_argument("--rate", type=int, default=30, help="SNAPs a second to ask for (1..30) [30]")
    ap.add_argument("--big", type=int, default=8, help="tiles in one UPDATE that make a repaint burst [8]")
    ap.add_argument("--settle", type=float, default=0.6, help="quiet seconds after a burst [0.6]")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds per scene [20]")
    ap.add_argument("--retries", type=int, default=5, help="mirror connect retries [5]")
    ap.add_argument("--verify", metavar="DIR", help="only verify an existing capture")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    def log(msg):
        if not a.quiet:
            sys.stderr.write("lcdmirror_capture: %s\n" % msg)

    if a.verify:
        try:
            n = verify(a.verify, log)
        except (CaptureError, OSError, ValueError, KeyError) as e:
            sys.stderr.write("lcdmirror_capture: FAIL: %s\n" % e)
            return EXIT_ERR
        log("verify: %d scenes OK" % n)
        return EXIT_OK
    if not a.out:
        ap.error("--out is required (or --verify DIR)")
    a.scenes = [s.strip() for s in a.scenes.split(",") if s.strip()]
    unknown = set(a.scenes) - set(SCENES)
    if unknown:
        ap.error("unknown scenes %s (known: %s)" % (sorted(unknown), ", ".join(SCENES)))
    if os.path.isdir(a.out) and os.listdir(a.out):
        ap.error("%s exists and is not empty: never overwrite a capture" % a.out)
    os.makedirs(a.out, exist_ok=True)
    cap = Capture(a, log)
    err = None
    try:
        cap.run()
    except (CaptureError, OSError) as e:
        err = e
    finally:
        if cap.conn:
            cap.conn.close()
    man = cap.write()
    with open(os.path.join(a.out, "capture.log"), "w") as f:
        f.write("%s\n%s\n" % (TOOL, " ".join(shlex.quote(x) for x in sys.argv)))
        for sc in man["scenes"]:
            f.write("scene %-10s seq %6d stream_end %9d sha256 %s\n"
                    % (sc["name"], sc["seq"], sc["stream_end"], sc["frame_sha256"]))
        for n in man["notes"]:
            f.write("note: %s\n" % n)
        if err:
            f.write("ERROR: %s\n" % err)
    if man["scenes"]:
        try:
            verify(a.out, log)
        except CaptureError as e:
            sys.stderr.write("lcdmirror_capture: FAIL: %s\n" % e)
            return EXIT_ERR
    if err:
        sys.stderr.write("lcdmirror_capture: FAIL: %s (%d scenes written)\n" % (err, len(man["scenes"])))
        return EXIT_ERR
    got = {sc["name"].split("_")[0] if sc["name"].startswith("midswap_") else sc["name"]
           for sc in man["scenes"]}
    missing = [s for s in a.scenes if s not in got]
    log("wrote %s: %d scenes, stream %d B%s" % (a.out, len(man["scenes"]), man["stream"]["bytes"],
                                                "; MISSING " + ",".join(missing) if missing else ""))
    return EXIT_PARTIAL if missing else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
