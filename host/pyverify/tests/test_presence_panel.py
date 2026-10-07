"""test_presence_panel.py -- net-protocol v0.17 in pyverify: presence (`hello`) and
the front panel (`panel`), the Harness Manager's R1/R2.

  - :func:`hello_message` builds the Harness Manager's bytes (the one-codec rule):
    pinned against the design doc's example and HM's 251 B worst case, and, when
    the Harness Manager's source is on this machine, against its own
    ``core.panel.hello_message`` for a table of inputs;
  - the ShellClient methods over a real socket (the FakeShell's own server);
  - the FakeShell linux-profile model's rules: TTL (+ its negative control),
    eviction, order, clipping, relative-time ageing, the page lock and the DUT
    refusal, the frame halves, taps; bare metal declines.

The byte-level agreement with mps3-harnessd itself is pinned in
src/linux_harness/sw/harnessd/tests/test_panel_e2e.py (a parity test) and
tests/firmware_logic/test_fakeshell_conformance.py (the decline rows).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from pyverify.client import (HELLO_LINE_MAX, PANEL_ROLES, ShellClient, hello_line,
                             hello_message)
from pyverify.testing.fakeshell import (LINUX_PROFILE_FEATURES, PANEL_LOCKED_ERR, FakeShell,
                                        presence_clip)

DOC_HELLO = (b'{"op":"hello","v":1,"sid":"a1b2c3d4","who":"alice@lab-pc01","app":"hm/0.1.0",'
             b'"name":"mps3-01","role":"holder","lease":{"by":"alice","left":4332,"q":1,'
             b'"req":"bob","rl":103},"job":{"k":"program","p":42},"ttl":90}\n')
HM_WORST = (b'{"op":"hello","v":1,"sid":"ffffffff","who":"xxxxxxxxxxxxxxxxxxxx",'
            b'"app":"harness-mana","name":"nnnnnnnnnnnnnnnn","role":"holder","lease":'
            b'{"by":"yyyyyyyyyyyy","left":86400,"q":99,"req":"zzzzzzzzzzzz","rl":600},'
            b'"job":{"k":"program-","p":100},"ttl":300}\n')


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def client(fake):
    return ShellClient("127.0.0.1", port=fake.control_port, timeout=5.0)


# ---- the hello builder ------------------------------------------------------------


def test_the_doc_example_and_hms_worst_case_byte_for_byte():
    m = hello_message("a1b2c3d4", "alice@lab-pc01", "hm/0.1.0", name="mps3-01", role="holder",
                      lease={"by": "alice@lab-gateway1", "left": 4332, "q": 1,
                             "req": "bob@lab-pc02", "rl": 103}, job={"k": "program", "p": 42})
    assert hello_line(m) == DOC_HELLO
    w = hello_message("f" * 32, "x" * 80, "harness-manager/10.20.30", name="n" * 64,
                      role="holder", lease={"by": "y" * 80, "left": 10 ** 9, "q": 12345,
                                            "req": "z" * 80, "rl": 10 ** 6},
                      job={"k": "program-partition", "p": 1000}, ttl=100000)
    assert hello_line(w) == HM_WORST and len(HM_WORST) == 251 <= HELLO_LINE_MAX


def test_caps_ascii_and_the_line_limit():
    m = hello_message("a1", "dévid@h\x1b[2J", "hm", role="boss", lease={"q": 0})
    assert m["who"] == "d?vid@h?[2J" and m["role"] == "watch" and m["lease"] == {"q": 0}
    assert "rl" not in hello_message("a", "w", "hm", lease={"by": "d", "rl": 5})["lease"], \
        "rl only with a request"
    with pytest.raises(ValueError):
        hello_line({"op": "hello", "who": "w" * 300})


def _hm_core():
    src = os.environ.get("HARNESS_MANAGER_SRC",
                         str(Path.home() / "SoCLabs" / "harness-manager" / "src"))
    if not (Path(src) / "harness_manager" / "core" / "panel.py").exists():
        return None
    sys.path.insert(0, src)
    try:
        from harness_manager.core import panel as P  # noqa: E402
    except Exception:                               # pragma: no cover - its deps missing
        return None
    finally:
        sys.path.remove(src)
    return P


@pytest.mark.parametrize("case", [
    dict(sid="a1b2c3d4", who="alice@lab-pc01", app="hm/0.1.0"),
    dict(sid="a1b2c3d4", who="alice@lab-pc01", app="hm/0.1.0", name="mps3-01", role="holder",
         lease=dict(by="alice@lab-gateway1", left=4332, q=1, req="bob@lab-pc02", rl=103),
         job=dict(k="program", p=42)),
    dict(sid="f" * 32, who="x" * 80, app="harness-manager/10.20.30", name="n" * 64,
         role="owner", lease=dict(by="y" * 80, left=10 ** 9, q=12345, req="z" * 80, rl=10 ** 6),
         job=dict(k="program-partition", p=1000), ttl=100000),
    dict(sid="s", who="dévid@h\x1b", app="hm", role="nobody", lease=dict(q=-4), ttl=1),
    dict(sid="s", who="w", app="hm", lease=dict(by="", req="r", rl=None, left=None)),
])
def test_hello_bytes_equal_the_harness_managers(case):
    """The one-codec rule, checked against Harness Manager's own builder when its
    source is on this machine (HARNESS_MANAGER_SRC, else ~/SoCLabs/harness-manager)."""
    P = _hm_core()
    if P is None:
        pytest.skip("Harness Manager source not found")
    lease = case.get("lease")
    job = case.get("job")
    h = P.Hello(sid=case["sid"], who=case["who"], app=case["app"], name=case.get("name", ""),
                role=case.get("role", "watch"),
                lease=None if lease is None else P.HelloLease(
                    by=lease.get("by", ""), left=lease.get("left"), q=lease.get("q", 0),
                    req=lease.get("req", ""), rl=lease.get("rl")),
                job=None if job is None else P.HelloJob(kind=job["k"], percent=job["p"]),
                ttl=case.get("ttl", 90))
    mine = hello_message(case["sid"], case["who"], case["app"], name=case.get("name", ""),
                         role=case.get("role", "watch"), lease=lease, job=job,
                         ttl=case.get("ttl", 90))
    assert hello_line(mine) == P.encode_hello(h)


# ---- the client over a real socket ---------------------------------------------------


@pytest.fixture
def linux():
    fake = FakeShell.ephemeral(profile="linux").start()
    yield fake
    fake.stop()


def test_client_hello_panel_frame_page(linux):
    assert LINUX_PROFILE_FEATURES[-2:] == ("presence", "panel")
    with client(linux) as c:
        r = c.hello("a1b2c3d4", "alice@lab-pc01", "hm/0.1.0", name="mps3-01", role="holder",
                    lease={"by": "alice", "left": 4332, "q": 1})
        assert list(r) == ["ok", "op", "sessions", "panel", "events"]
        assert r["sessions"] == 1 and r["panel"]["owner"] == "harness"
        st = c.panel()
        assert st["sessions"] == [{"sid": "a1b2c3d4", "who": "alice@lab-pc01", "role": "holder",
                                   "age_s": 0}]
        f = c.panel_frame()
        assert f["ok"] and len(f["rows"]) == 15 and len(f["roles"]) == 600
        # harnessd's default look is the aligned one: the held glyph leads the badge
        assert f["theme"] == "aligned" and f["rows"][0].endswith("\x83 alice 1h12m, 1 waiting ")
        assert f["rows"][1] == "-" * 40 and f["rows"][2].startswith("design ")
        assert {PANEL_ROLES[ord(ch) - 97] for ch in f["roles"][:40]} <= {"text", "title-held"}
        assert c.panel_page("apps") == {"ok": True, "op": "panel", "page": "apps"}
        assert c.panel()["page"] == "apps"
    assert linux.panel.hellos[-1]["lease"] == {"by": "alice", "left": 4332, "q": 1}


def test_today_theme_has_no_glyphs():
    """`--panel-theme today` (FakeShell panel={"theme": "today"}): today's words, ASCII."""
    fake = FakeShell(profile="linux", panel={"theme": "today"})
    fake.handle_control(hello_message("a1b2c3d4", "alice@lab-pc01", "hm/0.1.0", role="holder",
                                      lease={"by": "alice", "left": 4332, "q": 1}))
    a = fake.handle_control({"op": "panel", "frame": "a"})
    assert a["theme"] == "today" and a["rows"][0].endswith(" alice 1h12m, 1 waiting ")
    assert "\x83" not in a["rows"][0], "glyphs only in the aligned theme"
    assert a["rows"][2].startswith("DUT : ")


def test_bare_metal_declines():
    fake = FakeShell()
    assert fake.handle_control({"op": "hello", "sid": "a", "who": "w"}) == \
        {"ok": False, "err": "hello not supported", "code": "not_supported"}
    assert fake.handle_control({"op": "panel"}) == \
        {"ok": False, "err": "panel not supported", "code": "not_supported"}
    with pytest.raises(ValueError):
        FakeShell(panel={})


# ---- the model's rules ---------------------------------------------------------------


def _ttl_expires(negctl: bool) -> bool:
    clk = Clock()
    fake = FakeShell(profile="linux", panel={"clock": clk})
    if negctl:                                  # a table that ignores the TTL
        m = fake.panel
        m.live = lambda now, shown=False: sorted(m.sessions.values(), key=lambda s: -s["seen"])
    fake.handle_control(hello_message("t1", "d@h", "hm", ttl=30))
    clk.t += 30
    at_ttl = len(fake.handle_control({"op": "panel"})["sessions"]) == 1
    clk.t += 0.001
    after = fake.handle_control({"op": "panel"})["sessions"] == []
    return at_ttl and after


def test_ttl_and_its_negative_control():
    assert _ttl_expires(False)
    assert not _ttl_expires(True), "NEGATIVE CONTROL: a table that ignores the TTL fails it"


def test_eviction_order_and_left_ago():
    clk = Clock()
    fake = FakeShell(profile="linux", panel={"clock": clk, "theme": "today"})
    for i, role in enumerate(("watch", "owner", "watch", "holder")):
        fake.handle_control(hello_message(f"s{i}", f"u{i}@h", "hm", role=role))
        clk.t += 1
    assert [s["sid"] for s in fake.handle_control({"op": "panel"})["sessions"]] == \
        ["s3", "s1", "s2", "s0"]
    fake.handle_control(hello_message("s0", "u0@h", "hm"))
    clk.t += 1
    assert fake.handle_control(hello_message("s4", "u4@h", "hm"))["sessions"] == 4
    assert sorted(fake.panel.sessions) == ["s0", "s2", "s3", "s4"], "the oldest (s1) went"
    clk.t += 400
    row11 = fake.handle_control({"op": "panel", "frame": "b"})["rows"][3]
    assert row11.startswith("hm     u4@h  left 6m ago"), row11


def test_clipping_ageing_badge_and_request():
    clk = Clock()
    fake = FakeShell(profile="linux", panel={"clock": clk, "theme": "today"})
    r = fake.handle_control({"op": "hello", "sid": "0123456789", "who": "dévid@h\t" + "z" * 30,
                             "role": "holder", "lease": {"by": "alice@x", "left": 4332, "q": 1,
                                                         "req": "bob@y", "rl": 103}, "ttl": 300})
    assert r["ok"] and r["panel"]["banner"] == "bob wants this board"
    s = fake.handle_control({"op": "panel"})["sessions"][0]
    assert s["sid"] == "01234567" and s["who"] == "d?vid@h?" + "z" * 12
    assert presence_clip("a@b", 12, "@") == "a"
    clk.t += 43
    rows = fake.handle_control({"op": "panel", "frame": "b"})["rows"]
    assert rows[3].strip() == "held by alice 1:00 to answer", rows[3]
    clk.t += 60
    assert fake.handle_control({"op": "panel"})["banner"] == "", "its time to answer is over"
    clk.t += 97                                   # 200 s after the hello: left aged 200 s
    b = fake.handle_control({"op": "panel", "frame": "a"})["rows"][0]
    assert b.endswith("alice 1h08m, 1 waiting "), b


def test_page_lock_and_the_dut():
    fake = FakeShell(profile="linux", ssh_claimed=True)
    assert fake.handle_control({"op": "panel", "page": "apps"}, peer="10.0.0.9") == \
        {"ok": False, "err": PANEL_LOCKED_ERR, "code": "locked"}
    assert fake.handle_control({"op": "panel"}, peer="10.0.0.9")["ok"] is True
    assert fake.handle_control(hello_message("a", "w", "hm"), peer="10.0.0.9")["ok"] is True
    assert fake.handle_control({"op": "panel", "page": "apps"}, peer="127.0.0.1")["ok"] is True
    fake.display_owner = fake.display_target = "dut"
    assert fake.handle_control({"op": "panel", "page": "status"}, peer="127.0.0.1") == \
        {"ok": False, "err": "dut owns the panel", "code": "held"}
    assert fake.handle_control({"op": "panel"})["banner"] == "DUT HAS THE DISPLAY"


def test_frame_shapes_and_taps():
    fake = FakeShell(profile="linux")
    a = fake.handle_control({"op": "panel", "frame": "a"})
    b = fake.handle_control({"op": "panel", "frame": "b"})
    assert (len(a["rows"]), len(a["roles"]), len(b["rows"]), len(b["roles"])) == (8, 320, 7, 280)
    assert len(json.dumps(a, separators=(",", ":"))) + 1 <= 1280
    assert fake.handle_control({"op": "panel", "frame": True})["code"] == "invalid"
    assert fake.panel.tap("request") == 1 and fake.panel.tap("nav") == 2
    r = fake.handle_control(hello_message("a", "w", "hm"))
    assert r["panel"]["seq"] == 2 and [e["on"] for e in r["events"]] == ["request", "nav"]
    for _ in range(10):
        fake.panel.tap("identify")
    ev = fake.handle_control({"op": "panel"})["events"]
    assert len(ev) == 8 and ev[0]["seq"] == 5 and ev[-1]["seq"] == 12, "a ring of 8"
    too_long = {"op": "hello", "v": 1, "sid": "a1", "who": "w" * 300, "app": "hm"}
    assert fake.handle_control(too_long) == {"ok": False, "err": "bad json"}
