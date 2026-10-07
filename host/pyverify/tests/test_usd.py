"""net-protocol.md v0.13 (D13): the user-microSD overlay store, host side.

- the client: ``ShellClient.usd()`` / ``usd_format`` / ``usd_clear`` /
  ``usd_rescan`` and the parked ``commit_begin`` / ``commit_await``, against
  scripted reply lines (the exact bytes on the wire);
- FakeShell, the executable spec, on BOTH engine profiles: every ``usd`` state,
  the format rules, the wipe, every ``commit`` refusal, the park and the pushes;
- ``SwapOrchestrator.deploy(persist=...)``: commit after a verified swap, skip
  silently with no card / no ``usd`` feature, a commit failure is a warning;
- the CLI: ``pyverify usd status|format [--wipe]|clear|rescan`` and
  ``deploy --no-persist``.
"""
from __future__ import annotations

import json
import logging
import socket
import zlib
from pathlib import Path

import pytest

from pyverify.cli import main
from pyverify.client import (
    USD_ERRORS,
    CommitResponse,
    PingResponse,
    ShellClient,
    SwapResponse,
    UsdResponse,
    VersionResponse,
    is_usd_error_name,
)
from pyverify.mailbox import DIAG_FIELDS, DIAG_MAGIC, decode_diag
from pyverify.overlay import Overlay
from pyverify.pusher import BitstreamPusher, PushError, tcp_send
from pyverify.swap import (
    PERSIST_COMMITTED,
    PERSIST_FAILED,
    PERSIST_OFF,
    PERSIST_SKIPPED,
    SwapOrchestrator,
)
from pyverify.testing._pusher import BitstreamKind, frame_bitstream
from pyverify.testing.fakeshell import (
    LINUX_PROFILE_FEATURES,
    USD_ERRORS as FAKE_USD_ERRORS,
    USD_STATES,
    FakeShell,
    tftp_put,
    TftpError,
)

STATIC_ID = 0x72BB0A36
RM_ID = 0x0100001E
PROFILES = ("bare-metal", "linux")
CLEARING = b"\xC1\x0E\xA2\x00" * 32
PARTIAL = b"\xB1\x75\x00\x0D" * 96


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class _Scripted:
    """A Transport that records every sent line and replays scripted replies."""

    def __init__(self, *replies: str):
        self.sent: list[dict] = []
        self.raw_sent: list[bytes] = []
        self._replies = [r.encode() for r in replies]

    def send_line(self, payload: bytes) -> None:
        self.raw_sent.append(payload)
        self.sent.append(json.loads(payload))

    def recv_line(self) -> bytes:
        if not self._replies:
            raise AssertionError("no scripted reply left")
        return self._replies.pop(0)

    def close(self) -> None:
        pass


def _client(*replies: str):
    t = _Scripted(*replies)
    return ShellClient("scripted", transport=t), t


def _make_overlay(root: Path, *, rm_id: int = RM_ID, static_id: int = STATIC_ID,
                  rm_name: str = "led") -> Overlay:
    d = root / rm_name
    d.mkdir(parents=True)
    (d / f"{rm_name}_clear.bin").write_bytes(CLEARING)
    (d / f"{rm_name}.bin").write_bytes(PARTIAL)
    (d / "manifest.json").write_text(json.dumps({
        "schema": 1, "static_id": f"0x{static_id:08x}", "rm_id": f"0x{rm_id:08x}",
        "rm_name": rm_name,
        "clearing": {"file": f"{rm_name}_clear.bin", "len": len(CLEARING),
                     "crc32": f"0x{zlib.crc32(CLEARING) & 0xFFFFFFFF:08x}"},
        "partial": {"file": f"{rm_name}.bin", "len": len(PARTIAL),
                    "crc32": f"0x{zlib.crc32(PARTIAL) & 0xFFFFFFFF:08x}"},
    }))
    return Overlay.load(d)


def _crc(b: bytes) -> str:
    return "0x%08x" % (zlib.crc32(b) & 0xFFFFFFFF)


def _commit_req(**over) -> dict:
    req = {"op": "commit", "rm": "led", "src": "tcp",
           "rm_id": "0x%08x" % RM_ID, "static_id": "0x%08x" % STATIC_ID,
           "clear_len": len(CLEARING), "clear_crc": _crc(CLEARING),
           "part_len": len(PARTIAL), "part_crc": _crc(PARTIAL)}
    req.update(over)
    return {k: v for k, v in req.items() if v is not None}


def _fake(profile: str = "bare-metal", **kw) -> FakeShell:
    kw.setdefault("static_id", STATIC_ID)
    return FakeShell(profile=profile, **kw)


def _running(profile: str = "bare-metal", **kw) -> FakeShell:
    """A FakeShell with RM_ID live in the RP (as after a verified swap)."""
    kw.setdefault("boot_rm_id", RM_ID)
    return _fake(profile, **kw)


def _push_pair_tcp(fake: FakeShell, *, clearing: bytes = CLEARING, partial: bytes = PARTIAL,
                   rm_id: int = RM_ID) -> None:
    for payload, kind in ((clearing, BitstreamKind.CLEARING), (partial, BitstreamKind.PARTIAL)):
        tcp_send(frame_bitstream(payload, kind=kind, rm_slot=0, static_id=fake.static_id,
                                 rm_id=rm_id), fake.host, fake.raw_tcp_port, timeout_s=5.0)


def _commit_over_sockets(fake: FakeShell, *, rm: str = "led", push=_push_pair_tcp,
                         **over) -> CommitResponse:
    fields = dict(rm_id=RM_ID, static_id=STATIC_ID, clear_len=len(CLEARING),
                  clear_crc=zlib.crc32(CLEARING), part_len=len(PARTIAL),
                  part_crc=zlib.crc32(PARTIAL))
    fields.update(over)
    with ShellClient(fake.host, fake.control_port, timeout=10.0) as shell:
        shell.commit_begin(rm, **fields)
        try:
            push(fake)
        except PushError:
            pass
        return shell.commit_await()


# --------------------------------------------------------------------------- #
# 1. the client, against the bytes on the wire
# --------------------------------------------------------------------------- #

NO_CARD = '{"ok":true,"present":false,"state":"none","text":"none","boot":"none"}'
VALID = ('{"ok":true,"present":true,"state":"valid","text":"led [A]","card_mb":15193,'
         '"default":{"rm_id":"0x0100001e","static_id":"0x72bb0a36","slot":"A"},"boot":"loaded"}')


def test_usd_status_no_card_is_ok_not_an_error():
    c, t = _client(NO_CARD)
    r = c.usd()
    assert t.sent == [{"op": "usd"}]
    assert r.ok and r.present is False and r.state == "none" and r.text == "none"
    assert r.boot == "none" and r.card_mb is None and r.default is None
    assert not r.ready and not r.committable
    assert list(r.raw) == ["ok", "present", "state", "text", "boot"]


def test_usd_status_valid_parses_default_and_card():
    c, _ = _client(VALID)
    r = c.usd()
    assert r.ok and r.present and r.state == "valid" and r.text == "led [A]"
    assert r.card_mb == 15193 and r.ready and r.committable
    assert r.default.rm_id == "0x0100001e" and r.default.static_id == "0x72bb0a36"
    assert r.default.slot == "A" and r.boot == "loaded"


def test_usd_actions_send_the_contract_lines():
    c, t = _client('{"ok":true,"state":"empty"}', '{"ok":false,"err":"wipe disabled"}',
                   '{"ok":true,"state":"empty"}', '{"ok":false,"err":"no card"}')
    assert c.usd_format("erase").state == "empty"
    r = c.usd_format("erase-all")
    assert r.ok is False and r.err == "wipe disabled"
    assert c.usd_clear().ok
    assert c.usd_rescan().err == "no card"
    assert t.sent == [
        {"op": "usd", "action": "format", "confirm": "erase"},
        {"op": "usd", "action": "format", "confirm": "erase-all"},
        {"op": "usd", "action": "clear"},
        {"op": "usd", "action": "rescan"},
    ]


def test_usd_on_a_pre_v013_shell_is_ok_false_not_raised():
    c, _ = _client('{"ok":false,"err":"unknown op"}')
    r = c.usd()
    assert r.ok is False and r.err == "unknown op"


def test_commit_begin_sends_the_v013_line_exactly():
    c, t = _client('{"ok":true,"slot":"B"}')
    c.commit_begin("led", rm_id=RM_ID, static_id="0x72BB0A36", clear_len=68332,
                   clear_crc=0xDEADBEEF, part_len=1251884, part_crc="0x1")
    assert t.raw_sent == [(
        b'{"op":"commit","rm":"led","src":"tcp","rm_id":"0x0100001e",'
        b'"static_id":"0x72bb0a36","clear_len":68332,"clear_crc":"0xdeadbeef",'
        b'"part_len":1251884,"part_crc":"0x00000001"}')]
    r = c.commit_await()
    assert r == CommitResponse(ok=True, slot="B", err="")


def test_commit_await_carries_the_error_name():
    c, _ = _client('{"ok":false,"err":"rm mismatch"}')
    c.commit_begin("led", rm_id=1, static_id=2, clear_len=4, clear_crc=0,
                   part_len=4, part_crc=0)
    r = c.commit_await()
    assert r.ok is False and r.err == "rm mismatch" and is_usd_error_name(r.err)


@pytest.mark.parametrize("bad", [dict(rm_id="nope"), dict(static_id=-1),
                                 dict(clear_crc=1 << 32), dict(clear_len="4"),
                                 dict(part_len=True)])
def test_commit_begin_rejects_malformed_fields_before_sending(bad):
    c, t = _client()
    fields = dict(rm_id=1, static_id=2, clear_len=4, clear_crc=0, part_len=4, part_crc=0)
    fields.update(bad)
    with pytest.raises(ValueError):
        c.commit_begin("led", **fields)
    assert t.sent == []


def test_retired_commit_form_still_sends_rm_only_and_reports_the_refusal():
    c, t = _client('{"ok":false,"err":"bad args"}')
    r = c.commit("nanosoc")
    assert t.sent == [{"op": "commit", "rm": "nanosoc"}]
    assert r.ok is False and r.err == "bad args"


def test_error_name_set_is_the_contract_table():
    assert USD_ERRORS == FAKE_USD_ERRORS
    assert is_usd_error_name("identity lock: fabric static_id unknown")
    assert not is_usd_error_name("no sd card")        # the store's C name, not the wire's
    assert not is_usd_error_name("110")               # never an errno number


# --------------------------------------------------------------------------- #
# 2. FakeShell: `usd` status, on both profiles
# --------------------------------------------------------------------------- #

def _status(fake):
    return fake.handle_control({"op": "usd"})


@pytest.mark.parametrize("profile", PROFILES)
def test_status_no_card_is_the_contract_line(profile):
    f = _fake(profile)
    assert json.dumps(_status(f), separators=(",", ":")) == NO_CARD


@pytest.mark.parametrize("profile", PROFILES)
def test_status_no_hw(profile):
    r = _status(_fake(profile, usd_hw=False, usd_card="da"))
    assert r == {"ok": True, "present": False, "state": "no_hw", "text": "no hw", "boot": "none"}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("fault,text,boot", [("init", "init", "pending"),
                                              ("unsupported", "unsupported", "none"),
                                              ("error", "ERR 13", "none")])
def test_status_card_not_ready_has_no_card_mb(profile, fault, text, boot):
    r = _status(_fake(profile, usd_card="da", usd_fault=fault))
    assert r == {"ok": True, "present": True, "state": fault, "text": text, "boot": boot}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("layout,formatted", [("blank", False), ("empty_mbr", False),
                                              ("fs", False), ("gpt", False), ("other", False),
                                              ("unknown", False), ("da_small", False),
                                              ("da", False)])
def test_status_foreign_cards(profile, layout, formatted):
    r = _status(_fake(profile, usd_card=layout, usd_formatted=formatted, usd_card_mb=7580))
    assert r == {"ok": True, "present": True, "state": "foreign", "text": "foreign",
                 "card_mb": 7580, "boot": "none"}


@pytest.mark.parametrize("profile", PROFILES)
def test_status_empty(profile):
    r = _status(_fake(profile, usd_card="da"))
    assert list(r) == ["ok", "present", "state", "text", "card_mb", "boot"]
    assert r["state"] == "empty" and r["text"] == "empty" and r["boot"] == "none"


@pytest.mark.parametrize("profile", PROFILES)
def test_status_valid_loads_at_power_on(profile):
    f = _fake(profile, usd_default={"rm": "led", "rm_id": RM_ID, "slot": "B"})
    r = _status(f)
    assert list(r) == ["ok", "present", "state", "text", "card_mb", "default", "boot"]
    assert r["state"] == "valid" and r["text"] == "led [B]" and r["boot"] == "loaded"
    assert r["default"] == {"rm_id": "0x0100001e", "static_id": "0x72bb0a36", "slot": "B"}
    # the power-on load went through the swap FSM: the RM is live, a swap counted
    assert f.current_rm_id == RM_ID
    assert f.swaps[-1]["src"] == "usd" and f.swaps[-1]["final"] == "DONE"
    assert f.handle_control({"op": "ping"})["rm_id"] == "0x0100001e"


def test_status_valid_text_clips_the_name_to_eleven_and_falls_back_to_the_id():
    f = _fake(usd_default={"rm": "a_very_long_rm_name", "rm_id": RM_ID})
    assert _status(f)["text"] == "a_very_long [A]"
    assert len(_status(f)["text"]) <= 16
    f = _fake(usd_default={"rm": "", "rm_id": RM_ID})
    assert _status(f)["text"] == "0x0100001e [A]"


@pytest.mark.parametrize("profile", PROFILES)
def test_status_stale_never_loads(profile):
    f = _fake(profile, usd_default={"rm": "led", "rm_id": RM_ID, "static_id": 0x11111111})
    r = _status(f)
    assert r["state"] == "stale" and r["text"] == "stale key"
    assert r["default"]["static_id"] == "0x11111111"
    assert r["boot"] == "none"                          # the state says why
    assert f.current_rm_id == 0 and f.swaps == []       # zero ICAP words


@pytest.mark.parametrize("profile", PROFILES)
def test_status_bad_when_both_slots_fail_crc(profile):
    f = _fake(profile, usd_default={"rm": "led", "rm_id": RM_ID, "corrupt": True})
    r = _status(f)
    assert r["state"] == "bad" and r["text"] == "bad" and "default" not in r
    assert r["boot"] == "none" and f.swaps == []


def test_status_falls_back_to_the_other_slot_when_the_active_one_is_corrupt():
    from pyverify.testing.fakeshell import UsdSlot
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID, "slot": "A", "corrupt": True})
    f.usd_slots["B"] = UsdSlot(rm="old", rm_id=0x01000001, static_id=STATIC_ID)
    r = _status(f)
    assert r["state"] == "valid" and r["default"]["slot"] == "B" and r["text"] == "old [B]"


@pytest.mark.parametrize("profile", PROFILES)
def test_pb1_held_skips_the_load_and_shows_skipped(profile):
    f = _fake(profile, usd_default={"rm": "led", "rm_id": RM_ID}, usd_pb1_held=True)
    r = _status(f)
    assert r["state"] == "valid" and r["text"] == "skipped" and r["boot"] == "skipped"
    assert f.current_rm_id == 0
    # PB1 is sampled first, card or not; the CLCD text says "skipped" only
    # while a card is in
    r = _status(_fake(profile, usd_pb1_held=True))
    assert r["boot"] == "skipped" and r["text"] == "none"


def test_power_on_load_only_over_the_greybox():
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID}, boot_rm_id=0x01000001)
    assert _status(f)["boot"] == "none" and f.current_rm_id == 0x01000001


def test_skipped_clears_on_removal_and_on_a_successful_clear():
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID}, usd_pb1_held=True)
    assert f.handle_control({"op": "usd", "action": "clear"}) == {"ok": True, "state": "empty"}
    assert _status(f)["text"] == "empty"
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID}, usd_pb1_held=True)
    f.usd_remove()
    f.usd_insert("da")
    assert _status(f)["text"] != "skipped"


def test_power_on_decision_is_made_once():
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID})
    assert _status(f)["boot"] == "loaded"
    f.handle_control({"op": "usd", "action": "clear"})
    f._simulate_restart()                  # a WDOG reset / harnessd respawn
    assert _status(f)["boot"] == "loaded"  # the decision stands


def test_pending_power_on_load_refuses_a_swap_until_it_completes():
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID}, usd_boot="pending")
    assert _status(f)["boot"] == "pending"
    assert f.handle_control({"op": "swap", "rm": "x", "src": "tcp"}) == {
        "ok": False, "err": "a swap is already in flight"}
    assert f.handle_control(_commit_req())["err"] == "store busy"
    f.complete_usd_boot("loaded")
    assert _status(f)["boot"] == "loaded" and f.current_rm_id == RM_ID


@pytest.mark.parametrize("profile", PROFILES)
def test_every_state_is_reachable_and_in_the_contract_list(profile):
    seen = {
        _status(_fake(profile, usd_hw=False))["state"],
        _status(_fake(profile))["state"],
        _status(_fake(profile, usd_card="da", usd_fault="init"))["state"],
        _status(_fake(profile, usd_card="da", usd_fault="unsupported"))["state"],
        _status(_fake(profile, usd_card="da", usd_fault="error"))["state"],
        _status(_fake(profile, usd_card="fs"))["state"],
        _status(_fake(profile, usd_card="da"))["state"],
        _status(_fake(profile, usd_default={"rm": "l", "rm_id": 1}))["state"],
        _status(_fake(profile, usd_default={"rm": "l", "rm_id": 1, "static_id": 9}))["state"],
        _status(_fake(profile, usd_default={"rm": "l", "rm_id": 1, "corrupt": True}))["state"],
    }
    assert seen == set(USD_STATES)


@pytest.mark.parametrize("profile", PROFILES)
def test_usd_is_in_the_profile_features(profile):
    f = _fake(profile, features=("stats", "usd"))
    assert "usd" in f.handle_control({"op": "version"})["features"]
    assert "usd" in LINUX_PROFILE_FEATURES
    assert "usd" in _fake("linux").handle_control({"op": "version"})["features"]


def test_an_engine_without_the_store_answers_unavailable():
    f = _running(has_overlay_store=False, usd_card="da")
    assert f.handle_control({"op": "usd"}) == {"ok": False, "err": "unavailable"}
    assert f.handle_control(_commit_req())["err"] == "unavailable"


@pytest.mark.parametrize("req", [
    {"op": "usd", "action": "explode"},
    {"op": "usd", "action": 3},
    {"op": "usd", "action": "format", "confirm": 1},
])
def test_usd_malformed_action_is_bad_args(req):
    assert _fake(usd_card="da").handle_control(req) == {"ok": False, "err": "bad args"}


def test_usd_empty_action_is_status():
    f = _fake()
    assert f.handle_control({"op": "usd", "action": ""}) == _status(f)


# --------------------------------------------------------------------------- #
# 3. FakeShell: format / wipe / clear / rescan
# --------------------------------------------------------------------------- #

def _act(f, action, **kw):
    return f.handle_control({"op": "usd", "action": action, **kw})


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("confirm", [None, "", "yes", "ERASE", "erase_all"])
def test_format_needs_the_confirm(profile, confirm):
    f = _fake(profile, usd_card="blank")
    kw = {} if confirm is None else {"confirm": confirm}
    assert _act(f, "format", **kw) == {"ok": False, "err": "confirm required"}
    assert f.usd_card == "blank"                          # nothing written


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("layout", ["blank", "empty_mbr"])
def test_format_rule_b_truly_blank_card(profile, layout):
    f = _fake(profile, usd_card=layout)
    assert _act(f, "format", confirm="erase") == {"ok": True, "state": "empty"}
    assert _status(f)["state"] == "empty"


@pytest.mark.parametrize("profile", PROFILES)
def test_format_rule_a_reinitialises_an_existing_store(profile):
    f = _fake(profile, usd_default={"rm": "led", "rm_id": RM_ID})
    assert _act(f, "format", confirm="erase") == {"ok": True, "state": "empty"}
    f = _fake(profile, usd_card="da", usd_formatted=False)    # an unformatted p4
    assert _status(f)["state"] == "foreign"
    assert _act(f, "format", confirm="erase") == {"ok": True, "state": "empty"}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("layout,err", [("other", "exists"), ("fs", "filesystem present"),
                                        ("gpt", "filesystem present"),
                                        ("unknown", "filesystem present"),
                                        ("da_small", "partition too small")])
def test_format_refuses_and_writes_nothing(profile, layout, err):
    f = _fake(profile, usd_card=layout)
    assert _act(f, "format", confirm="erase") == {"ok": False, "err": err}
    assert f.usd_card == layout and _status(f)["state"] == "foreign"


def test_format_rule_b_needs_room_for_the_partition():
    f = _fake(usd_card="blank", usd_card_mb=16)
    assert _act(f, "format", confirm="erase") == {"ok": False, "err": "partition too small"}


def test_wipe_takes_over_any_card_on_bare_metal():
    for layout in ("fs", "gpt", "other", "unknown"):
        f = _fake(usd_card=layout)
        assert _act(f, "format", confirm="erase-all") == {"ok": True, "state": "empty"}
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID})    # a harness card too
    assert _act(f, "format", confirm="erase-all") == {"ok": True, "state": "empty"}


def test_wipe_is_disabled_under_linux():
    f = _fake("linux", usd_card="fs")
    assert _act(f, "format", confirm="erase-all") == {"ok": False, "err": "wipe disabled"}
    assert f.usd_card == "fs"
    # ... and a plain format is still the contract's rules
    assert _act(f, "format", confirm="erase") == {"ok": False, "err": "filesystem present"}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("action,kw", [("format", {"confirm": "erase"}),
                                       ("format", {"confirm": "erase-all"}),
                                       ("clear", {}), ("rescan", {})])
def test_actions_with_no_card_and_no_hw(profile, action, kw):
    assert _act(_fake(profile), action, **kw) == {"ok": False, "err": "no card"}
    assert _act(_fake(profile, usd_hw=False), action, **kw) == {"ok": False, "err": "no hw"}


@pytest.mark.parametrize("profile", PROFILES)
def test_clear_invalidates_the_default(profile):
    f = _fake(profile, usd_default={"rm": "led", "rm_id": RM_ID})
    assert _act(f, "clear") == {"ok": True, "state": "empty"}
    r = _status(f)
    assert r["state"] == "empty" and "default" not in r


@pytest.mark.parametrize("profile", PROFILES)
def test_clear_never_writes_a_foreign_card(profile):
    f = _fake(profile, usd_card="fs")
    assert _act(f, "clear") == {"ok": False, "err": "foreign"}


def test_rescan_restarts_the_probe_and_the_card_not_ready_errors():
    f = _fake(usd_card="da")
    assert _act(f, "rescan") == {"ok": True, "state": "init"}    # the probe restarted
    assert _status(f)["state"] == "empty"
    assert _act(_fake(usd_card="da", usd_fault="error"), "rescan") == {
        "ok": False, "err": "unavailable"}
    assert _act(_fake(usd_card="da", usd_fault="init"), "rescan") == {
        "ok": False, "err": "store busy"}


# --------------------------------------------------------------------------- #
# 4. FakeShell: commit refusals (answered at once, before any park)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("req", [
    {"op": "commit", "rm": "nanosoc"},                         # the v0.11 form
    _commit_req(src="tftp"),
    _commit_req(src=None),
    _commit_req(rm=""),
    _commit_req(rm_id=RM_ID),                                  # an int, not "0x..."
    _commit_req(static_id="72bb0a36"),                         # no 0x
    _commit_req(clear_crc="0x123456789"),                      # > 8 digits
    _commit_req(part_crc=None),
    _commit_req(clear_len=0),
    _commit_req(part_len=6),                                   # not a multiple of 4
    _commit_req(clear_len="512"),
    _commit_req(part_len=True),
    _commit_req(clear_len=4 * 1024 * 1024, part_len=4 * 1024 * 1024 + 4),   # > one slot
], ids=["v011", "src_tftp", "src_missing", "rm_empty", "rm_id_int", "static_no_0x",
        "crc_9_digits", "part_crc_missing", "clear_len_0", "part_len_6", "len_str",
        "len_bool", "too_big"])
def test_commit_bad_args(profile, req):
    f = _running(profile, usd_card="da")
    assert f.handle_control(req) == {"ok": False, "err": "bad args"}
    assert f.awaiting is None and f.commits == []


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("kw,err", [
    (dict(), "no card"),
    (dict(usd_hw=False, usd_card="da"), "no hw"),
    (dict(usd_card="fs"), "foreign"),
    (dict(usd_card="da", usd_formatted=False), "foreign"),
    (dict(usd_card="da", usd_fault="unsupported"), "unavailable"),
    (dict(usd_card="da", usd_fault="init"), "store busy"),
], ids=["no_card", "no_hw", "foreign_fs", "foreign_unformatted", "unsupported", "init"])
def test_commit_refused_by_the_card_state(profile, kw, err):
    f = _running(profile, **kw)
    assert f.handle_control(_commit_req()) == {"ok": False, "err": err}


@pytest.mark.parametrize("profile", PROFILES)
def test_commit_refused_unless_it_is_what_is_running(profile):
    f = _running(profile, usd_card="da")
    assert f.handle_control(_commit_req(static_id="0x11111111")) == {
        "ok": False, "err": "stale key"}
    assert f.handle_control(_commit_req(rm_id="0x01000001")) == {
        "ok": False, "err": "rm mismatch"}
    g = _fake(profile, usd_card="da")                    # greybox running
    assert g.handle_control(_commit_req()) == {"ok": False, "err": "rm mismatch"}


def test_every_fakeshell_usd_and_commit_error_is_a_contract_name():
    """Walk every refusal this double can produce and check the NAME."""
    replies = []
    for profile in PROFILES:
        for kw in (dict(), dict(usd_hw=False), dict(usd_card="fs"), dict(usd_card="other"),
                   dict(usd_card="da_small"), dict(usd_card="da", usd_fault="error"),
                   dict(usd_card="blank", usd_card_mb=8)):
            # a wipe can turn the card into an empty store, and the commit then
            # PARKS: a short idle timeout makes that the `timeout` name, fast
            f = _running(profile, swap_await_timeout=0.05, **kw)
            for req in ({"op": "usd", "action": "format", "confirm": "erase"},
                        {"op": "usd", "action": "format", "confirm": "erase-all"},
                        {"op": "usd", "action": "format"},
                        {"op": "usd", "action": "clear"},
                        {"op": "usd", "action": "rescan"},
                        {"op": "usd", "action": "?"},
                        _commit_req(), _commit_req(rm_id="0x1"), {"op": "commit", "rm": "x"}):
                replies.append(f.handle_control(req))
    errs = {r["err"] for r in replies if not r["ok"]}
    assert errs and errs <= USD_ERRORS, errs - USD_ERRORS
    assert "timeout" in errs and "wipe disabled" in errs
    assert all(set(r) == {"ok", "err"} for r in replies if not r["ok"])


# --------------------------------------------------------------------------- #
# 5. FakeShell: the parked commit over real sockets
# --------------------------------------------------------------------------- #

@pytest.fixture(params=PROFILES)
def live(request):
    f = FakeShell.ephemeral(profile=request.param, static_id=STATIC_ID, boot_rm_id=RM_ID,
                            usd_card="da", swap_await_timeout=2.0, swap_arm_grace=0.5)
    f.start()
    try:
        yield f
    finally:
        f.stop()


def test_commit_parks_takes_the_pair_and_lands_in_the_inactive_slot(live):
    r = _commit_over_sockets(live)
    assert r == CommitResponse(ok=True, slot="A", err="")        # empty store: A first
    st = _status(live)
    assert st["state"] == "valid" and st["text"] == "led [A]"
    assert st["default"] == {"rm_id": "0x0100001e", "static_id": "0x72bb0a36", "slot": "A"}
    slot = live.usd_slots["A"]
    assert slot.clearing == CLEARING and slot.partial == PARTIAL
    assert [e.transport for e in live.accepted_pushes] == ["tcp", "tcp"]
    # a second commit of the same RM goes to the OTHER slot, then the header flips
    r2 = _commit_over_sockets(live)
    assert r2.slot == "B" and _status(live)["default"]["slot"] == "B"
    assert live.usd_slots["A"] is not None                       # the old one survives
    assert live.commits == [("led", "A"), ("led", "B")]
    # the store's power-on decision is not redone by a commit
    assert _status(live)["boot"] == "none"


def test_commit_over_a_stale_card_is_how_a_rekeyed_board_recovers(live):
    from pyverify.testing.fakeshell import UsdSlot
    live.usd_slots["A"] = UsdSlot(rm="old", rm_id=RM_ID, static_id=0x11111111)
    live.usd_default_valid = True
    assert _status(live)["state"] == "stale"
    r = _commit_over_sockets(live)
    assert r.ok and r.slot == "B"                                # keep the stale one
    assert _status(live)["state"] == "valid"


def test_commit_over_a_bad_store_overwrites_the_corrupt_active_slot(live):
    from pyverify.testing.fakeshell import UsdSlot
    live.usd_slots["B"] = UsdSlot(rm="old", rm_id=RM_ID, static_id=STATIC_ID, corrupt=True)
    live.active_slot = "B"
    live.usd_default_valid = True
    assert _status(live)["state"] == "bad"
    r = _commit_over_sockets(live)
    assert r.ok and r.slot == "B" and _status(live)["state"] == "valid"


def test_commit_admits_6910_only(live):
    def tftp_push(fake):
        frame = frame_bitstream(CLEARING, kind=BitstreamKind.CLEARING, rm_slot=0,
                                static_id=STATIC_ID, rm_id=RM_ID)
        with pytest.raises(TftpError):
            tftp_put(fake.host, fake.tftp_port, frame, timeout=2.0)

    r = _commit_over_sockets(live, push=tftp_push)
    assert r == CommitResponse(ok=False, err="timeout")
    assert [e.status for e in live.push_events] == ["ERR_NOT_AWAITING"]
    assert live.commits == [] and _status(live)["state"] == "empty"


def test_commit_pushes_in_order_partial_first_is_refused(live):
    def partial_first(fake):
        tcp_send(frame_bitstream(PARTIAL, kind=BitstreamKind.PARTIAL, rm_slot=0,
                                 static_id=STATIC_ID, rm_id=RM_ID),
                 fake.host, fake.raw_tcp_port, timeout_s=5.0)

    r = _commit_over_sockets(live, push=partial_first)
    assert r.ok is False and r.err == "timeout"
    assert [e.status for e in live.push_events] == ["ERR_ORDER"]


def test_commit_crc_that_is_not_the_request_leaves_the_old_default(live):
    first = _commit_over_sockets(live)
    assert first.ok
    other = b"\x00\x11\x22\x33" * 32
    r = _commit_over_sockets(live, push=lambda f: _push_pair_tcp(f, clearing=other))
    assert r == CommitResponse(ok=False, err="crc")
    st = _status(live)
    assert st["state"] == "valid" and st["default"]["slot"] == "A"   # intact


def test_commit_idle_timeout_when_nothing_is_pushed(live):
    r = _commit_over_sockets(live, push=lambda f: None)
    assert r == CommitResponse(ok=False, err="timeout")
    assert live.awaiting is None and _status(live)["state"] == "empty"


def test_commit_card_pulled_mid_commit(live):
    def pull_then_push(fake):
        fake.usd_remove()
        _push_pair_tcp(fake)

    r = _commit_over_sockets(live, push=pull_then_push)
    assert r == CommitResponse(ok=False, err="no card")


def test_commit_write_failure_leaves_the_previous_default(live):
    assert _commit_over_sockets(live).slot == "A"
    live.usd_commit_fail = "io"
    r = _commit_over_sockets(live)
    assert r == CommitResponse(ok=False, err="io")
    assert _status(live)["default"]["slot"] == "A" and live.commits == [("led", "A")]


def test_a_push_with_no_commit_or_swap_parked_is_reset(live):
    try:
        _push_pair_tcp(live)
    except PushError:
        pass           # a small frame is often fully sent before the RST lands
    assert live.wait_push_events(2, timeout=5.0)
    # the clearing is reset (nothing awaits it), so the partial is out of order
    assert [e.status for e in live.push_events] == ["ERR_NOT_AWAITING", "ERR_ORDER"]
    assert live.accepted_pushes == [] and live.commits == []


def test_a_swap_during_a_parked_commit_is_refused(live):
    with ShellClient(live.host, live.control_port, timeout=10.0) as shell:
        shell.commit_begin("led", rm_id=RM_ID, static_id=STATIC_ID, clear_len=len(CLEARING),
                           clear_crc=zlib.crc32(CLEARING), part_len=len(PARTIAL),
                           part_crc=zlib.crc32(PARTIAL))
        assert live.wait_awaiting(BitstreamKind.CLEARING, timeout=5.0)
        other = live.handle_control({"op": "swap", "rm": "x", "src": "tcp"})
        assert other == {"ok": False, "err": "store busy"}
        busy = live.handle_control(_commit_req())
        assert busy == {"ok": False, "err": "store busy"}
        # `usd` status is never held
        assert live.handle_control({"op": "usd"})["ok"] is True
        _push_pair_tcp(live)
        assert shell.commit_await().ok


# --------------------------------------------------------------------------- #
# 6. SwapOrchestrator.deploy(persist=...)
# --------------------------------------------------------------------------- #

def _deploy(fake: FakeShell, overlay: Overlay, *, transport: str = "tcp", **kw):
    pusher = BitstreamPusher(host=fake.host, transport=transport, tftp_port=fake.tftp_port,
                             tcp_port=fake.raw_tcp_port, timeout_s=5.0)
    with ShellClient(fake.host, fake.control_port, timeout=20.0) as shell:
        return SwapOrchestrator(shell, pusher).deploy(
            overlay, src="tcp" if transport == "tcp" else "tftp", **kw)


@pytest.fixture
def overlay(tmp_path):
    return _make_overlay(tmp_path)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("transport", ["tcp", "tftp"])
def test_deploy_persists_by_default(profile, transport, overlay):
    features = ("usd",) if profile == "bare-metal" else ()
    with FakeShell.ephemeral(profile=profile, static_id=STATIC_ID, usd_card="da",
                             features=features) as fake:
        res = _deploy(fake, overlay, transport=transport)
        assert res.verified and res.persist.status == PERSIST_COMMITTED
        assert res.persist.slot == "A" and res.persist.committed
        assert fake.commits == [("led", "A")]
        # the swap pair over the chosen transport, then the commit pair over 6910
        assert [e.transport for e in fake.accepted_pushes] == [transport, transport, "tcp", "tcp"]
        # rm_id / static_id: the verified swap's readback and the manifest's
        slot = fake.usd_slots["A"]
        assert slot.rm_id == RM_ID == int(res.rm_id, 0)
        assert slot.static_id == STATIC_ID == overlay.manifest.static_id
        assert (slot.clear_len, slot.part_len) == (len(CLEARING), len(PARTIAL))
        assert slot.clear_crc == zlib.crc32(CLEARING) and slot.part_crc == zlib.crc32(PARTIAL)


def test_deploy_persist_false_opts_out(overlay):
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="da", features=("usd",)) as fake:
        res = _deploy(fake, overlay, persist=False)
        assert res.persist.status == PERSIST_OFF and fake.commits == []
        assert len(fake.accepted_pushes) == 2


def test_deploy_with_no_card_skips_silently(overlay, caplog):
    caplog.set_level(logging.DEBUG, logger="pyverify.swap")
    with FakeShell.ephemeral(static_id=STATIC_ID, features=("usd",)) as fake:
        res = _deploy(fake, overlay)
    assert res.persist.status == PERSIST_SKIPPED and "no card" in res.persist.reason
    assert fake.commits == [] and len(fake.accepted_pushes) == 2
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_deploy_on_a_shell_without_the_usd_feature_skips_silently(overlay, caplog):
    caplog.set_level(logging.DEBUG, logger="pyverify.swap")
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="da") as fake:   # features=()
        res = _deploy(fake, overlay)
    assert res.persist.status == PERSIST_SKIPPED and "'usd'" in res.persist.reason
    assert fake.commits == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_deploy_commit_failure_is_a_warning_not_a_failure(overlay, caplog):
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="da", features=("usd",),
                             usd_commit_fail="io") as fake:
        res = _deploy(fake, overlay)
        assert res.verified and fake.current_rm_id == RM_ID          # the swap stands
    assert res.persist.status == PERSIST_FAILED and res.persist.err == "io"
    warn = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warn and "FAILED" in warn[0].getMessage()


def test_deploy_foreign_card_is_not_committed_and_warns(overlay, caplog):
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="fs", features=("usd",)) as fake:
        res = _deploy(fake, overlay)
    assert res.persist.status == PERSIST_SKIPPED and "foreign" in res.persist.reason
    assert "usd format" in res.persist.reason
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


class _ClientStub:
    """Duck-typed client for the orchestrator's persist seams."""

    def __init__(self, *, swap: SwapResponse, usd: UsdResponse,
                 commit: "CommitResponse | Exception" = CommitResponse(ok=True, slot="B"),
                 features=("usd",)):
        self.host = "stub"
        self.log: list = []
        self._swap, self._usd, self._commit, self._features = swap, usd, commit, features

    def ping(self):
        return PingResponse(ok=True, shell_id=f"0x{STATIC_ID:08x}", rm_id="0x0")

    def swap_begin(self, rm, src="tftp"):
        self.log.append("swap_begin")

    def swap_await(self):
        self.log.append("swap_await")
        return self._swap

    def version(self):
        self.log.append("version")
        return VersionResponse(ok=True, features=tuple(self._features))

    def usd(self):
        self.log.append("usd")
        return self._usd

    def commit_begin(self, rm, **kw):
        self.log.append(("commit_begin", rm, kw))

    def commit_await(self):
        self.log.append("commit_await")
        if isinstance(self._commit, Exception):
            raise self._commit
        return self._commit


class _Pushes:
    def __init__(self, fail_on: int = -1):
        self.n = 0
        self.fail_on = fail_on

    def push_pair(self, overlay, *, rm_slot=0):
        self.n += 1
        if self.n == self.fail_on:
            raise PushError("connection reset")
        return (object(), object())


_READY = UsdResponse(ok=True, present=True, state="empty", text="empty", card_mb=100)
_OK_SWAP = SwapResponse(ok=True, rm_id=f"0x{RM_ID:08x}", verified=True)


def test_persist_sends_the_verified_ids_and_exact_lengths(overlay):
    c = _ClientStub(swap=_OK_SWAP, usd=_READY)
    res = SwapOrchestrator(c, _Pushes()).deploy(overlay)
    assert res.persist.status == PERSIST_COMMITTED and res.persist.slot == "B"
    begin = [x for x in c.log if isinstance(x, tuple)][0]
    assert begin[1] == "led"
    assert begin[2] == dict(rm_id=RM_ID, static_id=STATIC_ID, clear_len=len(CLEARING),
                            clear_crc=zlib.crc32(CLEARING), part_len=len(PARTIAL),
                            part_crc=zlib.crc32(PARTIAL), src="tcp")
    assert c.log[:2] == ["swap_begin", "swap_await"]
    assert c.log[-1] == "commit_await"


@pytest.mark.parametrize("swap,why", [
    (SwapResponse(ok=True, rm_id=f"0x{RM_ID:08x}", verified=False), "verified"),
    (SwapResponse(ok=True, rm_id="", verified=True), "rm_id"),
    (SwapResponse(ok=True, rm_id="0x01000001", verified=True), "manifest"),
])
def test_persist_only_after_a_verified_swap_of_this_overlay(overlay, swap, why):
    c = _ClientStub(swap=swap, usd=_READY)
    res = SwapOrchestrator(c, _Pushes()).deploy(overlay)
    assert res.persist.status == PERSIST_SKIPPED and why in res.persist.reason
    assert "usd" not in c.log


def test_persist_push_failure_still_reads_the_reply(overlay):
    c = _ClientStub(swap=_OK_SWAP, usd=_READY,
                    commit=CommitResponse(ok=False, err="rm mismatch"))
    res = SwapOrchestrator(c, _Pushes(fail_on=2)).deploy(overlay)
    assert res.persist.status == PERSIST_FAILED and res.persist.err == "rm mismatch"
    assert c.log[-1] == "commit_await"


def test_persist_push_failure_with_an_ok_reply_is_not_believed(overlay):
    c = _ClientStub(swap=_OK_SWAP, usd=_READY)
    res = SwapOrchestrator(c, _Pushes(fail_on=2)).deploy(overlay)
    assert res.persist.status == PERSIST_FAILED and "push failed" in res.persist.err


def test_persist_reply_timeout_is_a_warning(overlay):
    c = _ClientStub(swap=_OK_SWAP, usd=_READY, commit=socket.timeout("timed out"))
    res = SwapOrchestrator(c, _Pushes()).deploy(overlay)
    assert res.persist.status == PERSIST_FAILED and "out of step" in res.persist.err


def test_persist_usd_refusal_is_a_warning_skip(overlay):
    c = _ClientStub(swap=_OK_SWAP, usd=UsdResponse(ok=False, err="unavailable"))
    res = SwapOrchestrator(c, _Pushes()).deploy(overlay)
    assert res.persist.status == PERSIST_SKIPPED and "unavailable" in res.persist.reason


def test_commit_pusher_is_6910_and_windowed_iff_the_shell_is():
    swap_pusher = BitstreamPusher(host="h", transport="tftp", tcp_port=16910)
    orch = SwapOrchestrator(object(), swap_pusher)   # type: ignore[arg-type]
    plain = orch._commit_pusher_for(("usd",))
    assert plain.transport == "tcp" and plain.tcp_port == 16910 and not plain.windowed
    assert orch._commit_pusher_for(("usd", "windowed")).windowed
    tcp_pusher = BitstreamPusher(host="h", transport="tcp", windowed=True)
    assert SwapOrchestrator(object(), tcp_pusher)._commit_pusher_for(()) is tcp_pusher
    explicit = _Pushes()
    assert SwapOrchestrator(object(), swap_pusher,
                            commit_pusher=explicit)._commit_pusher_for(()) is explicit


# --------------------------------------------------------------------------- #
# 7. the CLI
# --------------------------------------------------------------------------- #

def _usd_argv(fake, verb, *extra):
    return ["usd", verb, "--host", fake.host, "--control-port", str(fake.control_port),
            *extra]


def test_cli_usd_status(capsys):
    with FakeShell.ephemeral(static_id=STATIC_ID,
                             usd_default={"rm": "led", "rm_id": RM_ID}) as fake:
        rc = main(_usd_argv(fake, "status"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["state"] == "valid" and out["text"] == "led [A]"
    assert list(out) == ["ok", "present", "state", "text", "card_mb", "default", "boot"]


def test_cli_usd_status_no_card_is_exit_0(capsys):
    with FakeShell.ephemeral() as fake:
        assert main(_usd_argv(fake, "status")) == 0
    assert json.loads(capsys.readouterr().out)["present"] is False


def test_cli_usd_unreachable_is_exit_3(capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    rc = main(["usd", "status", "--host", "127.0.0.1", "--control-port", str(port),
               "--timeout", "1"])
    assert rc == 3 and "cannot reach" in capsys.readouterr().err


def test_cli_usd_format_clear_rescan(capsys):
    with FakeShell.ephemeral(usd_card="blank") as fake:
        assert main(_usd_argv(fake, "format")) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True, "state": "empty"}
        assert main(_usd_argv(fake, "clear")) == 0
        assert main(_usd_argv(fake, "rescan")) == 0
        capsys.readouterr()
    with FakeShell.ephemeral(usd_card="fs") as fake:
        assert main(_usd_argv(fake, "format")) == 1
        io = capsys.readouterr()
        assert json.loads(io.out) == {"ok": False, "err": "filesystem present"}
        assert "filesystem present" in io.err


def test_cli_usd_wipe_needs_typed_erase(capsys, monkeypatch):
    with FakeShell.ephemeral(usd_card="fs") as fake:
        monkeypatch.setattr("builtins.input", lambda prompt="": "erase")
        assert main(_usd_argv(fake, "format", "--wipe")) == 2
        assert fake.usd_card == "fs"                              # nothing sent
        assert "NOT confirmed" in capsys.readouterr().err
        monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")
        assert main(_usd_argv(fake, "format", "--wipe")) == 0
        assert fake.usd_card == "da" and json.loads(capsys.readouterr().out)["state"] == "empty"


def test_cli_usd_wipe_with_no_terminal_needs_yes(capsys, monkeypatch):
    def no_tty(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_tty)
    with FakeShell.ephemeral(usd_card="other") as fake:
        assert main(_usd_argv(fake, "format", "--wipe")) == 2
        assert fake.usd_card == "other"
        assert main(_usd_argv(fake, "format", "--wipe", "--yes")) == 0
        assert fake.usd_card == "da"


@pytest.mark.parametrize("state_kw", [
    dict(usd_default={"rm": "led", "rm_id": RM_ID}),                        # valid
    dict(usd_default={"rm": "led", "rm_id": RM_ID, "static_id": 0x11111111}),  # stale
])
def test_cli_plain_format_over_a_default_needs_typed_erase(capsys, monkeypatch, state_kw):
    """Rule (a) re-initialises the store and DISCARDS the default: a plain
    format of a card holding one (valid or stale) asks like the wipe does."""
    with FakeShell.ephemeral(static_id=STATIC_ID, **state_kw) as fake:
        monkeypatch.setattr("builtins.input", lambda prompt="": "no")
        assert main(_usd_argv(fake, "format")) == 2
        io = capsys.readouterr()
        assert io.out == "" and "DISCARDS" in io.err and "NOT confirmed" in io.err
        assert fake.usd_default_valid                               # nothing sent
        monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")
        assert main(_usd_argv(fake, "format")) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True, "state": "empty"}
        assert not fake.usd_default_valid


def test_cli_plain_format_over_a_default_with_yes_and_no_tty(capsys, monkeypatch):
    def no_tty(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_tty)
    with FakeShell.ephemeral(static_id=STATIC_ID,
                             usd_default={"rm": "led", "rm_id": RM_ID}) as fake:
        assert main(_usd_argv(fake, "format")) == 2                 # no terminal: a NO
        assert main(_usd_argv(fake, "format", "--yes")) == 0


@pytest.mark.parametrize("state_kw", [dict(usd_card="da"), dict(usd_card="blank"),
                                      dict(usd_card="fs"), dict()])
def test_cli_plain_format_without_a_default_does_not_prompt(capsys, monkeypatch, state_kw):
    def must_not_ask(prompt=""):
        raise AssertionError("prompted with no default on the card")

    monkeypatch.setattr("builtins.input", must_not_ask)
    with FakeShell.ephemeral(static_id=STATIC_ID, **state_kw) as fake:
        rc = main(_usd_argv(fake, "format"))
    assert rc in (0, 1)                        # sent; the shell's verdict decides
    assert "ERASE" not in capsys.readouterr().err


def test_cli_usd_wipe_on_linux_is_refused(capsys):
    with FakeShell.ephemeral(profile="linux", usd_card="fs") as fake:
        assert main(_usd_argv(fake, "format", "--wipe", "--yes")) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "err": "wipe disabled"}


def _deploy_argv(fake, overlay_dir, *extra):
    return ["deploy", "--host", fake.host, "--overlay", str(overlay_dir),
            "--control-port", str(fake.control_port), "--pusher-transport", "tcp",
            "--src", "tcp", "--tcp-push-port", str(fake.raw_tcp_port),
            "--tftp-port", str(fake.tftp_port), "--client-timeout", "20", *extra]


def test_cli_deploy_persists_and_reports_it(overlay, capsys):
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="da", features=("usd",)) as fake:
        rc = main(_deploy_argv(fake, overlay.directory))
        assert fake.commits == [("led", "A")]
    io = capsys.readouterr()
    out = json.loads(io.out)
    assert rc == 0 and out["persist"] == {"status": "committed", "slot": "A",
                                          "reason": "", "err": ""}
    assert "WARNING" not in io.err


def test_cli_deploy_with_no_card_is_silent(overlay, capsys):
    with FakeShell.ephemeral(static_id=STATIC_ID, features=("usd",)) as fake:
        assert main(_deploy_argv(fake, overlay.directory)) == 0
    io = capsys.readouterr()
    assert json.loads(io.out)["persist"]["status"] == "skipped"
    assert "persist" not in io.err.lower() and "WARNING" not in io.err


def test_cli_deploy_foreign_card_warns(overlay, capsys):
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="fs", features=("usd",)) as fake:
        assert main(_deploy_argv(fake, overlay.directory)) == 0
    assert "WARNING: not persisted" in capsys.readouterr().err


def test_cli_deploy_no_persist(overlay, capsys):
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="da", features=("usd",)) as fake:
        rc = main(_deploy_argv(fake, overlay.directory, "--no-persist"))
        assert fake.commits == []
    assert rc == 0 and json.loads(capsys.readouterr().out)["persist"]["status"] == "off"


def test_cli_deploy_commit_failure_is_exit_0_with_a_warning(overlay, capsys):
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="da", features=("usd",),
                             usd_commit_fail="crc") as fake:
        rc = main(_deploy_argv(fake, overlay.directory))
    io = capsys.readouterr()
    assert rc == 0 and json.loads(io.out)["persist"]["status"] == "failed"
    assert "WARNING" in io.err and "crc" in io.err
    assert io.err.count("crc") == 1            # printed once, not echoed by logging


def test_board_deploy_persists_by_default_and_opts_out(tmp_path):
    from pyverify.board import Mps3Board
    _make_overlay(tmp_path)
    with FakeShell.ephemeral(static_id=STATIC_ID, usd_card="da", features=("usd",)) as fake:
        board = Mps3Board(fake.host, control_port=fake.control_port,
                          tftp_port=fake.tftp_port, tcp_push_port=fake.raw_tcp_port,
                          timeout=20.0, overlay_root=tmp_path)
        with board:
            first = board.deploy("led")
            assert first.persist.committed and first.persist.slot == "A"
            assert board.usd().state == "valid"
            second = board.deploy("led", persist=False)
            assert second.persist.status == PERSIST_OFF
        assert fake.commits == [("led", "A")]


# --------------------------------------------------------------------------- #
# 8. diag.h v9: the two D13 words, decoded only on a v9 image; the boot latch
# --------------------------------------------------------------------------- #

from pyverify.client import (  # noqa: E402
    USD_BOOT_DECISIONS,
    USD_BOOT_LATCH_MAGIC,
    USD_BOOT_WHY,
    USD_STORE_RC_NAMES,
    USD_SWAP_STATE_NAMES,
    usd_boot_text,
    usd_boot_word,
)

_FW = Path(__file__).resolve().parents[3] / "firmware"


def test_usd_boot_latch_words_render_like_the_firmware():
    assert usd_boot_text(0) == "pending"                   # not decided yet
    assert usd_boot_text(0xB0070001) == "pending"          # deciding (latched PENDING)
    assert usd_boot_text(0xB0070002) == "loaded"
    assert usd_boot_text(0xB0070003) == "skipped"
    assert usd_boot_text(0xB0070004) == "none"
    assert usd_boot_text(0xB0070105) == "failed:timeout"
    assert usd_boot_text(0xB0070205) == "failed:identity lock"
    assert usd_boot_text(0xB0074E05) == "failed:crc"       # store: -OVLSD_ECRC (14)
    assert usd_boot_text(0xB0077F05) == "failed:io"        # store: unknown rc -> "io"
    assert usd_boot_text(0xB0078705) == "failed:verify"    # swap: SWAP_VERIFY (7)
    with pytest.raises(ValueError):
        usd_boot_text(0x12340004)                          # no magic: not a latch
    for text in ("pending", "loaded", "skipped", "none", "failed:timeout",
                 "failed:too big", "failed:aborted", "failed:store busy", "failed:crc",
                 "failed:no card", "failed:stale key", "failed:verify", "failed:reisolate"):
        assert usd_boot_text(usd_boot_word(text)) == text
    with pytest.raises(ValueError):
        usd_boot_word("failed:bogus")


def _c_enum(text: str, prefix: str) -> "dict[str, int]":
    import re
    return {m.group(1): int(m.group(2), 0)
            for m in re.finditer(r"\b(%s\w*)\s*=\s*(-?(?:0x)?[0-9A-Fa-f]+)u?\b" % prefix, text)}


def test_latch_tables_match_the_firmware_headers():
    """The codec above is a copy of three C tables. Hold it to them."""
    import re
    osh = _FW / "overlay_store" / "overlay_store.h"
    sdh = _FW / "overlay_store" / "ovlstore_sd.h"
    fsm = _FW / "coordinator" / "swap_fsm.c"
    if not (osh.is_file() and sdh.is_file() and fsm.is_file()):
        pytest.skip("firmware headers not present")
    oh, sh, fc = osh.read_text(), sdh.read_text(), fsm.read_text()
    magic = re.search(r"#define\s+OVL_BOOT_LATCH_MAGIC\s+(0x[0-9A-Fa-f]+)", oh)
    assert magic and int(magic.group(1), 16) == USD_BOOT_LATCH_MAGIC
    dec = _c_enum(oh, "OVL_BOOT_(?:PENDING|LOADED|SKIPPED|NONE|FAILED)")
    assert {v: k.split("_")[-1].lower() for k, v in dec.items()} == USD_BOOT_DECISIONS
    why = _c_enum(oh, "OVL_BOOT_WHY_")
    assert why.pop("OVL_BOOT_WHY_SWAP") == 0x80 and why.pop("OVL_BOOT_WHY_STORE") == 0x40
    assert set(why.values()) == set(USD_BOOT_WHY)
    names = re.search(r"swap_fsm_state_name.*?names\[\]\s*=\s*\{(.*?)\};", fc, re.S)
    assert names and tuple(re.findall(r'"(\w+)"', names.group(1))) == USD_SWAP_STATE_NAMES
    rcs = {k: -v for k, v in _c_enum(sh, "OVLSD_E").items() if v < 0}
    body = re.search(r"overlay_store_err_name\(int rc\)\s*\{(.*?)\n\}", oh, re.S).group(1)
    wire, pending = {}, []
    for line in body.splitlines():
        pending += re.findall(r"case\s+(OVLSD_E\w+)\s*:", line)
        ret = re.search(r'return\s+"([^"]+)"', line)
        if ret:
            for label in pending:
                wire[rcs[label]] = ret.group(1)
            pending = []
    assert wire == USD_STORE_RC_NAMES, (wire, USD_STORE_RC_NAMES)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("kw,text", [
    (dict(), "none"),
    (dict(usd_hw=False), "none"),
    (dict(usd_default={"rm": "led", "rm_id": RM_ID}), "loaded"),
    (dict(usd_default={"rm": "led", "rm_id": RM_ID}, usd_pb1_held=True), "skipped"),
    (dict(usd_card="da", usd_fault="init"), "pending"),
    (dict(usd_card="da", usd_boot="failed:timeout"), "failed:timeout"),
])
def test_fakeshell_diag_usd_boot_is_the_latch(profile, kw, text):
    f = _fake(profile, **kw)
    word = f.handle_control({"op": "diag"})["usd_boot"]
    assert word >> 16 == USD_BOOT_LATCH_MAGIC
    assert usd_boot_text(word) == text == _status(f)["boot"]
    d = ShellClient("x", transport=_Scripted(json.dumps(f.handle_control({"op": "diag"})))).diag()
    assert d.usd_boot_text == text


def test_fakeshell_latch_follows_a_pending_load_to_its_decision():
    f = _fake(usd_default={"rm": "led", "rm_id": RM_ID}, usd_boot="pending")
    assert f.handle_control({"op": "diag"})["usd_boot"] == 0xB0070001
    f.complete_usd_boot("loaded")
    assert f.handle_control({"op": "diag"})["usd_boot"] == 0xB0070002


def test_fakeshell_latch_cannot_be_seeded_as_a_counter():
    with pytest.raises(ValueError, match="latch"):
        FakeShell(diag_counters={"usd_boot": 0})
    with pytest.raises(ValueError):
        FakeShell(usd_boot="failed:bogus")
    assert FakeShell(has_overlay_store=False).handle_control({"op": "diag"})["usd_boot"] == 0

def test_mailbox_v9_words_are_pad_on_a_v8_image():
    names = [n for n, _ in DIAG_FIELDS]
    assert names[-2:] == ["svc_max_us_6", "usd_boot"]
    words = [0] * 64
    words[0] = DIAG_MAGIC
    words[names.index("usd_boot")] = 0xB0070002
    words[1] = 8
    assert decode_diag(0x1FF00, words).get("usd_boot") is None
    words[1] = 9
    mb = decode_diag(0x1FF00, words)
    assert mb.get("usd_boot") == 0xB0070002 and mb.as_wire()["usd_boot"] == 0xB0070002
