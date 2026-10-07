"""test_panel_e2e.py -- presence (`hello`) and the front panel (`panel`) in
mps3-harnessd over real sockets (host build, MOCK fabric; net-protocol.md v0.17
"Presence and the panel", the Harness Manager's R1/R2):

  - version.features "presence" and "panel"; the ctrl-echo twin declines both;
  - HM's OWN messages replayed byte for byte (the design doc's hello, HM's 251 B
    worst case, the P2 test hellos, the frame halves) -- nested lease/job decode;
  - THE 256 B BUDGET: a 256-character line is read, a 257-character one is not
    ("bad json", and the table is unchanged);
  - PRINTABLE-ASCII CLIPPING: raw UTF-8 and escaped control characters arrive as
    '?', every field clipped to its cap;
  - THE TTL (on a 30x presence clock, --mock-presence-speed): a session leaves the
    list after its ttl. NEGATIVE CONTROL: --mock-negctl-presence-no-ttl, the same
    check FAILS -- so it can see a table that ignores the TTL;
  - 5TH-SESSION EVICTION: the oldest last hello goes;
  - `panel` state shape; the frame in two halves, each one reply <= 1280 B with
    40 role codes a row; frame:true refused;
  - `page`: refused while the DUT owns the panel (a `display` flip); CLAIM-LOCKED
    for a remote peer of a claimed board, the board's own peer passes, and
    `hello` and the panel reads stay open;
  - PARITY: pyverify's FakeShell(profile="linux") answers a scripted session the
    same way (shape, values, and the rows the harness draws from the table).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "host" / "pyverify"))

from harnessd_mock import Harnessd  # noqa: E402
from pyverify.client import ShellClient, hello_line, hello_message  # noqa: E402
from pyverify.testing.fakeshell import FakeShell  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
ECHO = str(HERE.parent / "build" / "host-echo" / "mps3-harnessd")
TRUSTED, REMOTE = "127.0.0.3", "127.0.0.1"
KEY = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J me@pc\n"
LOCKED = {"ok": False, "err": "panel locked: board claimed (use ssh)", "code": "locked"}
CODES = set("abcdefghijklmnopqrstu")            # 'a' + clcd_role (21 roles)

pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")

# HM's messages, byte for byte -------------------------------------------------------
#: harness-manager docs/design/CLCD_ALIGNMENT.md §2.2 (one line on the wire)
DOC_HELLO = (b'{"op":"hello","v":1,"sid":"a1b2c3d4","who":"alice@lab-pc01","app":"hm/0.1.0",'
             b'"name":"mps3-01","role":"holder","lease":{"by":"alice","left":4332,"q":1,'
             b'"req":"bob","rl":103},"job":{"k":"program","p":42},"ttl":90}')
#: harness-manager tests/unit/test_p1_presence.py WORST through core.panel.encode_hello
#: (251 B with its newline -- HM pins the figure)
HM_WORST = (b'{"op":"hello","v":1,"sid":"ffffffff","who":"xxxxxxxxxxxxxxxxxxxx",'
            b'"app":"harness-mana","name":"nnnnnnnnnnnnnnnn","role":"holder","lease":'
            b'{"by":"yyyyyyyyyyyy","left":86400,"q":99,"req":"zzzzzzzzzzzz","rl":600},'
            b'"job":{"k":"program-","p":100},"ttl":300}')
#: harness-manager tests/unit/test_p2_panel_mps3.py HELLO (hello_message of it)
HM_P2_HELLO = (b'{"op":"hello","v":1,"sid":"a1b2c3d4","who":"alice@lab-pc01","app":"hm/0.1.0",'
               b'"name":"mps3-01","role":"holder","lease":{"by":"alice","left":4332,"q":1},'
               b'"ttl":90}')


def start(tmp: Path, extra=(), name="hd", binary=BIN) -> Harnessd:
    """`--netif lo`: a link that is UP (carrier 1), so the panel shows no NETWORK LINK
    DOWN fault banner -- a fault outranks the request banner and the hm row (rows
    10-12), exactly as on the glass."""
    return Harnessd(binary, tmp / name,
                    extra=["--lcdmirror", "none", "--netif", "lo", *extra]).start()


def frame(hd: Harnessd):
    """Both halves on one connection: (rows, roles, theme)."""
    rows, roles, theme = [], "", ""
    with hd.ctl() as c:
        for part in "ab":
            c.send({"op": "panel", "frame": part})
            r = json.loads(c.line())
            rows += r["rows"]
            roles += r["roles"]
            theme = r["theme"]
    return rows, roles, theme


def settle(hd: Harnessd, pred, timeout: float = 10.0):
    """The frame is the COMMITTED one: clcd reformats every 250 ms, so what a hello
    changed shows up to one refresh later -- and the very first frame only after the
    panel's init stream, which a loaded host can stretch to seconds. Poll (rows, roles,
    theme, state) until pred holds; return the last read either way."""
    deadline = time.monotonic() + timeout
    while True:
        rows, roles, theme = frame(hd)
        st = hd.req({"op": "panel"})
        got = (rows, roles, theme, st)
        if pred(*got) or time.monotonic() > deadline:
            return got
        time.sleep(0.05)


def raw(hd: Harnessd, line: bytes, src: str = "127.0.0.1") -> bytes:
    """One raw line on a fresh connection (retrying the single-client refusal)."""
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", hd.port(6900)), timeout=10,
                                          source_address=(src, 0)) as s:
                s.sendall(line + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    d = s.recv(4096)
                    if not d:
                        break
                    buf += d
                if buf:
                    return buf
        except (ConnectionError, socket.timeout):
            pass
        time.sleep(0.05)
    raise ConnectionError("6900 never answered")


def req(hd: Harnessd, obj, src: str = "127.0.0.1") -> dict:
    return json.loads(raw(hd, json.dumps(obj, separators=(",", ":")).encode(), src))


def hello(sid: str, who: str, role: str = "watch", **kw) -> dict:
    return hello_message(sid, who, "hm/0.1.0", role=role, **kw)


def _alias_ok() -> bool:
    try:
        with socket.socket() as s:
            s.bind((TRUSTED, 0))
        return True
    except OSError:
        return False


# ---- features + HM's messages ---------------------------------------------------------


def test_features_and_hms_own_messages(tmp_path):
    hd = start(tmp_path)
    try:
        feats = hd.req({"op": "version"})["features"]
        assert feats[-2:] == ["presence", "panel"] and "locate" in feats
        line = raw(hd, DOC_HELLO)
        r = json.loads(line)
        assert list(r) == ["ok", "op", "sessions", "panel", "events"], r
        assert r["ok"] is True and r["op"] == "hello" and r["sessions"] == 1
        assert list(r["panel"]) == ["page", "owner", "pending", "banner", "card", "seq"]
        assert r["panel"]["page"] == "status" and r["panel"]["owner"] == "harness"
        assert r["panel"]["pending"] is False and r["events"] == [] and r["panel"]["seq"] == 0
        # the lease request: on the glass within a refresh (the reply is the glass NOW)
        st = settle(hd, lambda *g: g[3]["banner"] == "bob wants this board")[3]
        assert st["banner"] == "bob wants this board", st
        assert len(HM_WORST) + 1 == 251
        r = json.loads(raw(hd, HM_WORST))
        assert r["ok"] is True and r["sessions"] == 2
        r = json.loads(raw(hd, HM_P2_HELLO))
        assert r["ok"] is True and r["sessions"] == 2, "same sid a1b2c3d4: refreshed, not added"
        st = hd.req({"op": "panel"})
        assert frame(hd)[2] == "aligned", "HM's look unless --panel-theme today"
        assert list(st) == ["ok", "op", "page", "owner", "pending", "banner", "card", "touch",
                            "sessions", "seq", "events"], list(st)
        assert st["touch"] == {"present": False, "cal": True}     # the mock has no STMPE811
        assert st["sessions"] == [
            {"sid": "a1b2c3d4", "who": "alice@lab-pc01", "role": "holder", "age_s": 0},
            {"sid": "ffffffff", "who": "x" * 20, "role": "holder", "age_s": 0}]
        # pyverify's builder makes HM's bytes (the one-codec rule)
        m = hello_message("a1b2c3d4", "alice@lab-pc01", "hm/0.1.0", name="mps3-01",
                          role="holder", lease={"by": "alice@lab-gateway1", "left": 4332, "q": 1,
                                                "req": "bob@lab-pc02", "rl": 103},
                          job={"k": "program", "p": 42})
        assert hello_line(m) == DOC_HELLO + b"\n"
    finally:
        hd.stop()


def test_the_echo_twin_declines_both(tmp_path):
    if not Path(ECHO).exists():
        pytest.skip("host-echo not built")
    hd = start(tmp_path, binary=ECHO, name="echo")
    try:
        assert "presence" not in hd.req({"op": "version"})["features"]
        assert json.loads(raw(hd, DOC_HELLO)) == {"ok": False, "err": "hello not supported",
                                                  "code": "not_supported"}
        assert hd.req({"op": "panel"}) == {"ok": False, "err": "panel not supported",
                                           "code": "not_supported"}
    finally:
        hd.stop()


# ---- the 256 B budget + clipping ----------------------------------------------------


def test_the_line_budget_and_ascii_clipping(tmp_path):
    hd = start(tmp_path)
    try:
        def padded(n: int) -> bytes:
            base = b'{"op":"hello","sid":"s1","who":"w","app":"'
            tail = b'"}'
            return base + b"a" * (n - len(base) - len(tail)) + tail
        assert len(padded(256)) == 256
        assert json.loads(raw(hd, padded(256)))["ok"] is True, "256 characters: read"
        assert json.loads(raw(hd, padded(257))) == {"ok": False, "err": "bad json"}, \
            "257: refused whole at the line layer"
        assert [s["sid"] for s in hd.req({"op": "panel"})["sessions"]] == ["s1"], "unchanged"
        # raw UTF-8 bytes and escaped control characters arrive as '?'; clips per field
        line = ('{"op":"hello","sid":"0123456789ab","who":"dévid@h\\tzzzzzzzzzzzzzzzzzzzzzz",'
                '"lease":{"by":"alice@lab-gateway1","left":-1,"q":500},"ttl":9}').encode()
        assert json.loads(raw(hd, line))["ok"] is True
        s = {x["sid"]: x for x in hd.req({"op": "panel"})["sessions"]}["01234567"]
        assert s["who"] == "d??vid@h?zzzzzzzzzzz" and len(s["who"]) == 20, s
        # a \\u escape is refused by the tokenizer (fail closed), like every verb
        assert json.loads(raw(hd, b'{"op":"hello","sid":"x","who":"\\u0041"}')) == \
            {"ok": False, "err": "bad json"}
        # deeper nesting: still bad json; a nested line naming another op: still bad json
        assert json.loads(raw(hd, b'{"op":"hello","sid":"x","who":"w","lease":{"by":{"a":1}}}')) \
            == {"ok": False, "err": "bad json"}
        assert json.loads(raw(hd, b'{"op":"ping","x":{"a":1}}')) == {"ok": False, "err": "bad json"}
        for bad, why in ((b'{"op":"hello","who":"w"}', "invalid sid"),
                         (b'{"op":"hello","sid":"s","who":"w","role":"boss"}', "invalid role"),
                         (b'{"op":"hello","sid":"s","who":"w","lease":{"left":"1h"}}',
                          "invalid lease")):
            r = json.loads(raw(hd, bad))
            assert r["ok"] is False and r["code"] == "invalid" and r["err"].startswith(why), r
    finally:
        hd.stop()


# ---- TTL + negative control, eviction ---------------------------------------------


def _ttl_expires(tmp: Path, extra) -> bool:
    """30x presence clock: a 30 s ttl is 1 s. Live at once, gone after ~1.4 s."""
    hd = start(tmp, extra=["--mock-presence-speed", "30", *extra], name="ttl" + str(len(extra)))
    try:
        r = req(hd, hello("t1", "alice@lab-pc01", ttl=30))
        live_now = r["sessions"] == 1 and len(hd.req({"op": "panel"})["sessions"]) == 1
        time.sleep(1.4)
        gone = hd.req({"op": "panel"})["sessions"] == []
        return live_now and gone
    finally:
        hd.stop()


def test_ttl_expiry_and_its_negative_control(tmp_path):
    assert _ttl_expires(tmp_path, []), "a session leaves the list after its ttl"
    assert not _ttl_expires(tmp_path, ["--mock-negctl-presence-no-ttl"]), \
        "NEGATIVE CONTROL: a table that ignores the TTL fails the same check"


def test_the_fifth_session_evicts_the_oldest(tmp_path):
    hd = start(tmp_path)
    try:
        for i in range(4):
            assert req(hd, hello(f"s{i}", f"u{i}@h"))["sessions"] == i + 1
            time.sleep(0.02)
        req(hd, hello("s0", "u0@h"))                 # s0 beats again: s1 is now the oldest
        r = req(hd, hello("s4", "u4@h", role="holder"))
        assert r["sessions"] == 4
        sids = [s["sid"] for s in hd.req({"op": "panel"})["sessions"]]
        assert sids[0] == "s4", "the holder first"
        assert sorted(sids) == ["s0", "s2", "s3", "s4"], sids
        assert "the oldest session evicted" in hd.log()
    finally:
        hd.stop()


# ---- the frame ---------------------------------------------------------------------


def test_the_frame_comes_in_two_halves_within_the_reply_limit(tmp_path):
    hd = start(tmp_path)                    # the default, aligned: glyphs \u0080-\u0086
    try:
        req(hd, hello("f1", "alice@lab-pc01", role="holder",
                      lease={"by": "alice", "left": 4332, "q": 1}))
        with hd.ctl() as c:
            for part, n in (("a", 8), ("b", 7)):
                c.send({"op": "panel", "frame": part})
                line = c.line()
                assert len(line) + 1 <= 1280, f"half {part}: {len(line) + 1} B"
                r = json.loads(line)
                assert list(r) == ["ok", "op", "frame", "theme", "rows", "roles"], list(r)
                assert r["frame"] == part and r["theme"] in ("aligned", "today")
                assert len(r["rows"]) == n and all(len(x) == 40 for x in r["rows"])
                assert len(r["roles"]) == n * 40 and set(r["roles"]) <= CODES
        rows, roles, _t, _st = settle(hd, lambda rw, *_: rw[11].startswith("hm     \x84alice")
                                      and "\x83" in rw[0])
        assert len(rows) == 15 and len(roles) == 600
        assert rows[0].rstrip().endswith("\x83 alice 1h12m, 1 waiting"), repr(rows[0])
        b0 = rows[0].index("\x83")
        assert set(roles[b0:39]) == {"g"} and b0 + 24 == 39, "title-held, right-aligned, pad 1"
        assert rows[11].startswith("hm     \x84alice@lab-pc01"), repr(rows[11])
        assert roles[11 * 40:11 * 40 + 2] == "bb" and roles[11 * 40 + 7] == "c", "label, value"
        r = hd.req({"op": "panel", "frame": True})
        assert r == {"ok": False, "err": 'invalid frame: "a" (rows 0-7) then "b" (rows 8-14)',
                     "code": "invalid"}
        # pyverify's client reads both halves on one connection
        with ShellClient("127.0.0.1", port=hd.port(6900), timeout=5.0) as cl:
            f = cl.panel_frame()
        assert f["ok"] is True and len(f["rows"]) == 15 and len(f["roles"]) == 600
        assert f["rows"][11] == rows[11] and f["roles"][440:480] == roles[440:480]
    finally:
        hd.stop()


# ---- the page ---------------------------------------------------------------------


CLCDKVM, KVM_STATUS, KVM_EVENT, OWNER_DUT, PENDING = 0x44AD0000, 0x04, 0x08, 1 << 0, 1 << 1
#: EVENT (W1C): harness_gained | panel_reset_done -- the KVM handing the panel back
#: through its reset, which is what makes clcd.c regain the pads (test_lcdmirror_e2e.py)
HANDBACK = (1 << 0) | (1 << 7)


def test_page_follows_the_owner(tmp_path):
    """The KVM's STATUS is plain in the mock fabric (hal_mock.c): the test is the KVM."""
    hd = start(tmp_path)
    try:
        assert hd.req({"op": "panel", "page": "apps"}) == {"ok": True, "op": "panel", "page": "apps"}
        assert hd.req({"op": "panel"})["page"] == "apps"
        hd.fabric.wr(CLCDKVM, KVM_STATUS, OWNER_DUT)             # the DUT has the panel
        st = hd.req({"op": "panel"})
        assert (st["owner"], st["pending"]) == ("dut", False)
        assert hd.req({"op": "panel", "page": "status"}) == \
            {"ok": False, "err": "dut owns the panel", "code": "held"}
        assert hd.req({"op": "panel"})["page"] == "apps", "refused: unchanged"
        hd.fabric.wr(CLCDKVM, KVM_STATUS, OWNER_DUT | PENDING)   # handing it back
        assert hd.req({"op": "panel"})["pending"] is True
        assert hd.req({"op": "panel", "page": "status"})["code"] == "held", "mid-flip"
        hd.fabric.wr(CLCDKVM, KVM_STATUS, 0)                     # the harness again ...
        hd.fabric.wr(CLCDKVM, KVM_EVENT, HANDBACK)               # ... through the KVM's reset
        deadline = time.monotonic() + 3
        while True:                               # clcd regains the pads on its own tick
            r = hd.req({"op": "panel", "page": "status"})
            if r["ok"] or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert r == {"ok": True, "op": "panel", "page": "status"}, r
        hl = json.loads(raw(hd, DOC_HELLO))
        assert hl["panel"]["owner"] == "harness" and hl["panel"]["page"] == "status"
        for bad in ({"op": "panel", "page": "menu"}, {"op": "panel", "page": 1},
                    {"op": "panel", "page": "apps", "frame": "a"}):
            r = hd.req(bad)
            assert r["ok"] is False and r["code"] == "invalid", r
    finally:
        hd.stop()


@pytest.mark.skipif(not _alias_ok(), reason=f"cannot bind {TRUSTED}")
def test_page_is_claim_locked_and_the_reads_are_not(tmp_path):
    hd = start(tmp_path, extra=["--mock-trusted-peer", TRUSTED])
    ak = hd.dir / "ssh" / "authorized_keys"
    try:
        assert req(hd, {"op": "panel", "page": "apps"}, REMOTE)["ok"] is True, "unclaimed: open"
        ak.parent.mkdir(parents=True, exist_ok=True)
        ak.write_bytes(KEY)
        assert req(hd, {"op": "panel", "page": "status"}, REMOTE) == LOCKED
        assert req(hd, {"op": "panel", "page": "nonsense"}, REMOTE) == LOCKED, "the lock first"
        assert req(hd, {"op": "panel"}, REMOTE)["page"] == "apps", "the read stays open"
        assert req(hd, {"op": "panel", "frame": "a"}, REMOTE)["ok"] is True
        assert req(hd, hello("c1", "bob@lab-pc02"), REMOTE)["ok"] is True, "hello stays open"
        # NEGATIVE CONTROL: the board's own peer (an ssh tunnel's far end) passes
        assert req(hd, {"op": "panel", "page": "status"}, TRUSTED)["ok"] is True
        ak.unlink()
        assert req(hd, {"op": "panel", "page": "apps"}, REMOTE)["ok"] is True, "unclaimed again"
    finally:
        hd.stop()


# ---- parity with pyverify's FakeShell --------------------------------------------------


def _shape(v):
    if isinstance(v, dict):
        return {k: _shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_shape(x) for x in v]
    return type(v).__name__


def test_fakeshell_parity(tmp_path):
    """The same script on the real harnessd and on FakeShell(profile="linux"): the
    same keys, key order, types and values (the times are both 0 s into it), and the
    same rows the harness draws from the table: the badge (row 0) and the request
    banner (rows 10-12). The one modelled difference: the fake has no refresh, so a
    hello REPLY's panel.banner is compared once the real glass has caught up."""
    hd = start(tmp_path)
    fake = FakeShell(profile="linux", panel={"touch_present": False, "label": "MPS3"})
    script = [
        hello("w1", "bob@lab-pc02"),
        hello("h1", "alice@lab-pc01", role="holder",
              lease={"by": "alice", "left": 4332, "q": 1, "req": "carol", "rl": 103}),
        {"op": "panel"},
        {"op": "panel", "page": "apps"},
        {"op": "panel", "page": "status"},
        {"op": "panel", "frame": True},
        {"op": "hello", "sid": "x", "who": "w", "role": "boss"},
        hello("o1", "eve@lab", role="owner"),
        {"op": "panel"},
    ]
    try:
        for msg in script:
            mine = fake.handle_control(json.loads(json.dumps(msg)))
            if msg == {"op": "panel"}:            # the glass, once it has caught up
                settle(hd, lambda *g: g[3]["banner"] == mine["banner"])
            real = req(hd, msg)
            if msg.get("op") == "hello" and real.get("ok"):
                real["panel"].pop("banner")
                mine = json.loads(json.dumps(mine))
                mine["panel"].pop("banner")
            assert _shape(real) == _shape(mine), (msg, real, mine)
            assert list(real) == list(mine), (msg, list(real), list(mine))
            assert real == mine, (msg, real, mine)
        want = [fake.handle_control({"op": "panel", "frame": p}) for p in "ab"]
        frows = want[0]["rows"] + want[1]["rows"]
        froles = want[0]["roles"] + want[1]["roles"]
        rows, roles, theme, _st = settle(hd, lambda rw, *_: rw[10] == frows[10])
        assert rows[0][15:39] == frows[0][15:39], (rows[0], frows[0])       # the badge
        assert roles[15:39] == froles[15:39]
        for r in (10, 11, 12):                                            # the banner
            assert rows[r] == frows[r], (r, rows[r], frows[r])
            assert roles[r * 40:(r + 1) * 40] == froles[r * 40:(r + 1) * 40], r
        assert theme == want[0]["theme"] == "aligned"           # both defaults
    finally:
        hd.stop()


def test_fakeshell_parity_hm_row(tmp_path):
    """Row 11 with no banner over it: the hm row, both engines, both themes."""
    for theme in ("aligned", "today"):
        hd = start(tmp_path, extra=["--panel-theme", theme], name=theme)
        fake = FakeShell(profile="linux", panel={"theme": theme})
        try:
            for msg in (hello("w1", "bob@lab-pc02"), hello("h1", "alice@lab-pc01", role="holder")):
                req(hd, msg)
                fake.handle_control(msg)
            want = fake.handle_control({"op": "panel", "frame": "b"})
            rows, roles, got_theme, _st = settle(hd, lambda rw, *_: rw[11] == want["rows"][3])
            assert rows[11] == want["rows"][3], (theme, rows[11], want["rows"][3])
            assert roles[440:480] == want["roles"][120:160], theme
            assert got_theme == want["theme"] == theme
            glyph = "\x84" if theme == "aligned" else ""
            assert rows[11] == f"hm     {glyph}alice@lab-pc01  +1 watching".ljust(40)
        finally:
            hd.stop()
