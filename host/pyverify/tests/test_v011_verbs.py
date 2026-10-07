"""net-protocol v0.11 client codec: stats / log / touch_cal / reboot, and the
appended version.features names, against scripted reply lines (the firmware
side is pinned in firmware/test/test_v011_dispatch.c and, end to end through the
real C codec, in tests/firmware_logic/test_json_golden.py)."""
from __future__ import annotations

import json

import pytest

from pyverify import cli as cli_mod
from pyverify.client import (LOG_CHUNK, ShellClient, ShellProtocolError,
                             TOUCH_CAL_KEYS)
from pyverify.testing.fakeshell import VERSION_FEATURES


class Scripted:
    """A Transport that records requests and replays canned reply lines."""

    def __init__(self, replies):
        self.sent = []
        self._replies = list(replies)

    def send_line(self, payload: bytes) -> None:
        self.sent.append(json.loads(payload))

    def recv_line(self) -> bytes:
        return self._replies.pop(0).encode()

    def close(self) -> None:
        pass


def _client(*replies):
    t = Scripted(replies)
    return ShellClient("x", transport=t), t


STATS_LINE = (
    '{"ok":true,"up_ms":123456,"sid":"0x3f1a560f","rm":"0x01000001","rm_ok":true,'
    '"lock":false,"clk_sel":1,"mmcm":true,"clk_alive":true,"dut_rst":true,'
    '"rp_rst":true,"decpl":false,"link":true,"spd":100,"fdx":true,'
    '"mac":"0002f7ef441c","swap":"idle","swap_ok":true,"swap_n":3,"icap":886432,'
    '"rxdrop":0,"txerr":0,"swap_err":"","clr_ok":true,"dut_mhz":50,'
    '"svc_max_us":812,"svc_skipped":0}'
)


def test_features_appended_in_bit_order():
    assert VERSION_FEATURES[:5] == ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")
    assert VERSION_FEATURES[5:13] == ("dut_egress", "jtag_server", "xvc_dbgbr", "xvc_jtagbb",
                                      "stats", "log", "reboot", "touch_cal")
    # net-protocol v0.13: APPENDED at bit 13; clients test the NAME.
    assert VERSION_FEATURES[13] == "usd"
    # v0.14 amendments (2026-09-26): APPENDED in landing order.
    assert VERSION_FEATURES[14:] == ("slot", "xvc_lock")


def test_stats_decodes_fpgahub_shape_in_wire_order():
    c, t = _client(STATS_LINE)
    r = c.stats()
    assert t.sent == [{"op": "stats"}]
    assert r.ok and r.up_ms == 123456 and r.sid == "0x3f1a560f" and r.rm_ok
    assert r.spd == 100 and r.mac == "0002f7ef441c" and r.swap_n == 3
    assert r.dut_mhz == 50 and r.svc_max_us == 812
    assert list(r.raw)[:2] == ["ok", "up_ms"]          # up_ms first after ok
    assert not any(k in r.raw for k in ("mv", "ma"))


def test_stats_on_a_pre_v011_shell_is_ok_false_not_raised():
    c, _ = _client('{"ok":false,"err":"unknown op"}')
    r = c.stats()
    assert r.ok is False and r.err == "unknown op"


def test_log_follows_more_and_resumes():
    a = "41" * LOG_CHUNK
    c, t = _client(
        '{"ok":true,"off":10,"n":%d,"more":true,"dropped":10,"data":"%s"}' % (LOG_CHUNK, a),
        '{"ok":true,"off":%d,"n":2,"more":false,"dropped":10,"data":"0d0a"}' % (10 + LOG_CHUNK),
    )
    text, last, nxt = c.read_log(0)
    assert t.sent == [{"op": "log", "off": 0}, {"op": "log", "off": 10 + LOG_CHUNK}]
    assert text == b"A" * LOG_CHUNK + b"\r\n"
    assert nxt == 10 + LOG_CHUNK + 2 and last.dropped == 10 and not last.more


def test_log_rejects_torn_chunk_and_negative_offset():
    c, _ = _client('{"ok":true,"off":0,"n":3,"more":false,"dropped":0,"data":"4142"}')
    with pytest.raises(ShellProtocolError):
        c.log(0)
    with pytest.raises(ValueError):
        c.log(-1)


def test_touch_cal_set_sends_all_seven_and_decodes():
    coeffs = (-5461, 37, 1310720, 12, 4096, -65536, 14)
    echo = "{" + '"ok":true,' + ",".join(f'"{k}":{v}' for k, v in zip(TOUCH_CAL_KEYS, coeffs)) + "}"
    c, t = _client(echo)
    r = c.touch_cal("set", coeffs)
    assert t.sent[0] == {"op": "touch_cal", "act": "set", **dict(zip(TOUCH_CAL_KEYS, coeffs))}
    assert r.ok and r.coeffs == coeffs
    with pytest.raises(ValueError):
        c.touch_cal("set", (1, 2, 3))
    with pytest.raises(ValueError):
        c.touch_cal("calibrate")


def test_touch_cal_raw_and_decline():
    c, _ = _client('{"ok":true,"raw_x":1234,"raw_y":3210,"raw_z":150,"seen":42,"x":223,"y":51}',
                   '{"ok":false,"err":"touch not present"}')
    r = c.touch_cal("raw")
    assert (r.raw_x, r.raw_y, r.raw_z, r.seen, r.x, r.y) == (1234, 3210, 150, 42, 223, 51)
    r = c.touch_cal("get")
    assert r.ok is False and r.err == "touch not present"


def test_reboot_ok_and_refusals():
    c, _ = _client('{"ok":true,"in_ms":2784}', '{"ok":false,"err":"EBUSY"}')
    assert c.reboot().in_ms == 2784
    r = c.reboot()
    assert r.ok is False and r.err == "EBUSY"


def test_cli_parses_the_v011_subcommands():
    p = cli_mod.build_parser() if hasattr(cli_mod, "build_parser") else None
    if p is None:
        pytest.skip("cli exposes no build_parser()")
    for argv in (["stats", "--host", "h"], ["log", "--host", "h", "--off", "5"],
                 ["touch-cal", "--host", "h", "set", "1", "2", "3", "4", "5", "6", "7"],
                 ["reboot", "--host", "h", "--wait"]):
        ns = p.parse_args(argv)
        assert callable(ns.func)
