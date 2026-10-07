"""``pyverify version`` prints the fabric cross-check (ILA-mint finding #15).

The raw reply and ``ShellClient.version()`` always carried ``usr_access`` and
``skew``; the CLI dropped both, so anything scripted against it could not see a
firmware/bitstream skew. Linux's ``id_skew`` (the identity lock's reason) was not
modelled at all. Now: ``usr_access``, ``skew``, ``skew_verdict`` and ``id_skew``
are in the JSON (``impl`` stays last), and ``VersionResponse.id_skew`` is an
additive field (the raw key also stays in ``extra``).

Negative control: every cross-check field of VersionResponse must reach the
CLI's JSON -- a later edit that drops one fails here.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from pyverify import cli as cli_mod
from pyverify.client import ShellClient, VersionResponse
from pyverify.testing.fakeshell import FakeShell

REASON = "image 0x0badcafe != fabric 0x5a5a0001"


class _IdSkewShell(FakeShell):
    """harnessd with its identity lock engaged: id_skew after skew, before impl."""

    def _op_version(self, request):
        reply = super()._op_version(request)
        impl = reply.pop("impl", None)
        reply["id_skew"] = REASON
        if impl:
            reply["impl"] = impl
        return reply


def _shell(cls=FakeShell, **kw):
    s = cls(control_port=0, tftp_port=0, raw_tcp_port=0, uart0_port=0, uart1_port=0,
            swo_port=0, **kw)
    s.start()
    return s


def _cli_version(shell, capsys):
    rc = cli_mod.main(["version", "--host", "127.0.0.1", "--control-port", str(shell.control_port)])
    out = capsys.readouterr().out
    return rc, json.loads(out), out


@pytest.mark.parametrize("usr_access, want_skew, want_verdict", [
    (None, None, "unchecked"),          # the fabric value could not be read: NOT a pass
    (0x01000001, False, "ok"),
    (0x01000002, True, "SKEW"),
])
def test_version_json_carries_the_cross_check(capsys, usr_access, want_skew, want_verdict):
    s = _shell(harness_ver32=0x01000001, harness_usr_access=usr_access)
    try:
        rc, j, out = _cli_version(s, capsys)
    finally:
        s.stop()
    assert rc == 0
    assert j["usr_access"] == (None if usr_access is None else "0x%08x" % usr_access)
    assert j["skew"] is want_skew and j["skew_verdict"] == want_verdict
    assert j["id_skew"] is None and j["impl"] == "bare-metal"
    assert list(j)[-1] == "impl"                             # impl stays last, as on the wire
    # the pre-existing keys are unchanged
    assert {"ok", "harness", "ver32", "sha", "dirty", "lmb_kb", "features"} <= set(j)


def test_linux_id_skew_reaches_the_response_and_the_cli(capsys):
    s = _shell(_IdSkewShell, profile="linux", harness_ver32=0x01000001,
               harness_usr_access=0x01000001)
    try:
        with ShellClient("127.0.0.1", s.control_port, timeout=5) as c:
            v = c.version()
        rc, j, _ = _cli_version(s, capsys)
    finally:
        s.stop()
    assert v.id_skew == REASON and v.extra.get("id_skew") == REASON   # additive: extra kept
    assert v.is_linux and v.skew is False
    assert rc == 0 and j["id_skew"] == REASON and j["impl"] == "linux"


@pytest.mark.parametrize("raw, want", [(None, None), ("", None), ("x", "x"), (7, "7")])
def test_id_skew_parsing(raw, want):
    class T:
        def __init__(self):
            self.sent = []

        def connect(self):
            pass

        def close(self):
            pass

        def send_line(self, line):
            self.sent.append(line)

        def recv_line(self):
            r = {"ok": True, "harness": "1.0.0", "ver32": "0x01000001", "features": []}
            if raw is not None:
                r["id_skew"] = raw
            return json.dumps(r).encode()

    c = ShellClient("h", transport=T())
    assert c.version().id_skew == want


def test_negative_control_every_cross_check_field_reaches_the_cli(capsys):
    """A field VersionResponse models (other than the extras bag) that the CLI
    JSON leaves out is exactly finding #15's bug."""
    s = _shell(harness_ver32=0x01000001, harness_usr_access=0x01000001)
    try:
        _, j, _ = _cli_version(s, capsys)
    finally:
        s.stop()
    modelled = {f.name for f in dataclasses.fields(VersionResponse)} - {"extra"}
    assert modelled <= set(j), sorted(modelled - set(j))
