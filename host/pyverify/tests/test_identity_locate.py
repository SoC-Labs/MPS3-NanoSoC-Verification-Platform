"""test_identity_locate.py -- net-protocol v0.16 in pyverify: the FakeShell's
linux-profile models of `identity` / `identity_set` / `locate` and the
ShellClient methods, over a real socket (the fake's own server).

The byte-level agreement with mps3-harnessd is pinned elsewhere:
tests/firmware_logic/test_fakeshell_conformance.py (the decline rows, both
engines) and src/linux_harness/sw/harnessd/tests/test_identity_e2e.py /
test_locate_e2e.py (the real engine). Here: the model's rules.
"""
from __future__ import annotations

import pytest

from pyverify.client import ShellClient
from pyverify.testing.fakeshell import (IDENTITY_LOCKED_ERR, IDENTITY_NO_PERSIST_ERR,
                                        LINUX_PROFILE_FEATURES, FakeShell, identity_check)


@pytest.fixture
def linux():
    fake = FakeShell.ephemeral(profile="linux", identity={
        "stage0": {"label": "MPS3-02", "ip": "192.168.10.102/24", "mac": "0200000002fe"}}).start()
    yield fake
    fake.stop()


def client(fake):
    return ShellClient("127.0.0.1", port=fake.control_port, timeout=5.0)


def test_features_name_both():
    assert LINUX_PROFILE_FEATURES[-5:-2] == ("lcd_mirror", "identity", "locate")


def test_identity_read_and_set(linux):
    with client(linux) as c:
        r = c.identity()
        assert r["ok"] and r["label"] == "MPS3" and r["source"]["label"] == "default"
        assert r["stage0"]["label"] == "MPS3-02"
        assert r["pending"] == {"label": "MPS3-02", "hostname": "mps3-02",
                                "ip": "192.168.10.102/24", "mac": "0200000002fe"}
        r = c.identity_set(label="BENCH-2", mac="02:11:22:33:44:55")
        assert r == {"ok": True, "op": "identity_set", "persisted": True,
                     "pending": {"label": "BENCH-2", "hostname": "bench-2",
                                 "ip": "192.168.10.102/24", "mac": "021122334455"},
                     "applies": "reboot"}
        assert c.identity()["override"] == {"label": "BENCH-2", "mac": "021122334455"}
        r = c.identity_set(mac="01:00:00:00:00:00")
        assert r == {"ok": False, "err": "invalid mac: multicast, not unicast", "code": "invalid"}
        assert c.identity_set(clear=True)["pending"]["label"] == "MPS3-02"
        assert c.identity()["override"] is None
        with pytest.raises(ValueError):
            c.identity_set(colour="red")


def test_identity_set_locked_and_no_persist():
    fake = FakeShell(profile="linux", ssh_claimed=True, identity={"persist": False})
    assert fake.handle_control({"op": "identity_set", "label": "X"}, peer="10.1.1.1") == \
        {"ok": False, "err": IDENTITY_LOCKED_ERR, "code": "locked"}
    assert fake.handle_control({"op": "identity_set", "label": "X"}, peer="127.0.0.1") == \
        {"ok": False, "err": IDENTITY_NO_PERSIST_ERR, "code": "no_persist"}
    assert fake.handle_control({"op": "identity"})["persist"] is False


def test_bare_metal_declines_everything():
    fake = FakeShell()
    for op in ({"op": "identity"}, {"op": "identity_set", "label": "X"}):
        assert fake.handle_control(op) == {"ok": False, "err": "identity not supported",
                                           "code": "not_supported"}
    assert fake.handle_control({"op": "locate", "s": 5}) == \
        {"ok": False, "err": "locate not supported", "code": "not_supported"}


@pytest.mark.parametrize("field,value,ok", [
    ("label", "MPS3-02", True), ("label", "mps3", False), ("label", "A" * 20, False),
    ("hostname", "a.b-c", True), ("hostname", "-a", False),
    ("ip", "10.0.0.1", True), ("ip", "10.0.0.0/8", False), ("ip", "1.2.3.4/31", False),
    ("mac", "02-00-00-00-02-fe", True), ("mac", "ffffffffffff", False),
])
def test_identity_rules(field, value, ok):
    assert (identity_check(field, value)[0] is None) == ok


def test_locate(linux):
    with client(linux) as c:
        assert c.locate(10, "alice@bench") == {"ok": True, "op": "locate", "until_ms": 10000}
        assert c.locate(3, "bob")["until_ms"] == 3000          # replaces
        assert c.locate(0) == {"ok": True, "op": "locate", "until_ms": 0}
        assert linux.locates == [(10, "alice@bench"), (3, "bob"), (0, "")]
        assert linux.locate_until is None
        for s in (31, -1):
            assert c.locate(s)["code"] == "invalid"
    assert linux.handle_control({"op": "locate", "s": 5, "who": "x\ty"})["code"] == "invalid"
    # not claim-locked
    linux.ssh_claimed = True
    assert linux.handle_control({"op": "locate", "s": 1}, peer="10.9.9.9")["ok"] is True
