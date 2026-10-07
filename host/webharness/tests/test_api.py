"""``webharness.api`` — the routing core, driven directly. No sockets anywhere.

Every test here calls :func:`webharness.api.handle` with a
:class:`~webharness.backend.FakeBackend` (or a hostile stub) and asserts on the
returned bytes/status. That is the whole point of the pure-function split: the
HTTP surface is provable without a board, a network, or a server.
"""
from __future__ import annotations

import json

import pytest

from webharness import api
from webharness.backend import BackendInfo, FakeBackend


def call(method, path, backend, body=b"", host_label="board"):
    return api.handle(method, path, body, backend, host_label=host_label)


class ExplodingBackend:
    """Fails the test if anything touches the board. Used to prove the routes
    that must NOT open a control connection (``/`` and ``/healthz``) don't."""

    def info(self):
        return BackendInfo(mode="stub", target="nowhere", live=False)

    def _boom(self, *a, **k):
        raise AssertionError("this route must not contact the board")

    ping = diag = telemetry = reset = set_clk = _boom


# --------------------------------------------------------------------------- #
# Routing basics
# --------------------------------------------------------------------------- #

def test_unknown_route_is_404_json():
    r = call("GET", "/nope", FakeBackend())
    assert r.status == 404
    assert r.json()["ok"] is False
    assert "no route" in r.json()["err"]


def test_wrong_method_on_a_real_path_is_404():
    assert call("GET", "/api/reset", FakeBackend()).status == 404
    assert call("POST", "/api/status", FakeBackend()).status == 404


def test_query_string_is_stripped_so_cache_busters_do_not_404():
    r = call("GET", "/api/status?t=12345", FakeBackend())
    assert r.status == 200


def test_every_json_body_carries_ok():
    b = FakeBackend()
    for path in ("/healthz", "/api/status", "/api/services", "/api/clocks",
                 "/api/resets", "/api/diag", "/api/console/uart0"):
        payload = call("GET", path, b).json()
        assert "ok" in payload, path


# --------------------------------------------------------------------------- #
# healthz + page: must never touch the board
# --------------------------------------------------------------------------- #

def test_healthz_does_not_contact_the_board():
    r = call("GET", "/healthz", ExplodingBackend())
    assert r.status == 200 and r.json()["ok"] is True


def test_healthz_is_true_even_when_the_board_is_down():
    r = call("GET", "/healthz", FakeBackend(reachable=False))
    assert r.json()["ok"] is True, "healthz answers for the SERVER, not the board"


def test_page_render_does_not_contact_the_board():
    r = call("GET", "/", ExplodingBackend())
    assert r.status == 200
    assert r.content_type.startswith("text/html")
    assert b"<!doctype html>" in r.body[:32]


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def test_status_reports_identity_and_makeup():
    r = call("GET", "/api/status", FakeBackend())
    p = r.json()
    assert r.status == 200 and p["ok"] is True
    assert p["shell"]["reachable"] is True
    assert p["shell"]["static_id"] == "0xcd74b6ae"
    assert p["rm"]["loaded"] is True
    assert p["rm"]["name"] == "nanosoc"
    assert p["rm"]["design"] == "0x0001"
    assert p["rm"]["caps"] == "1x Cortex-M0  no ETH  1x UART"


def test_status_when_the_board_is_down_is_502_but_still_shaped():
    r = call("GET", "/api/status", FakeBackend(reachable=False))
    assert r.status == 502
    p = r.json()
    assert p["ok"] is False
    assert p["shell"]["reachable"] is False
    assert p["rm"]["loaded"] is False and p["rm"]["name"] is None
    assert "unreachable" in p["err"]


def test_status_never_invents_a_power_reading():
    p = call("GET", "/api/status", FakeBackend()).json()
    assert p["telemetry"]["power_sensor"] is False
    assert "mv" not in json.dumps(p["telemetry"])
    assert "ma" not in p["telemetry"]


def test_lockup_is_flagged_meaningless_for_rms_that_tie_it_off():
    # nanosoc hard-ties dut_lockup to 0: False means "cannot report".
    p = call("GET", "/api/status", FakeBackend(rm_id="0x01000001")).json()
    assert p["telemetry"]["lockup_meaningful"] is False
    assert "cannot report" in p["telemetry"]["lockup_note"]
    # multicore actually drives it.
    p = call("GET", "/api/status", FakeBackend(rm_id="0x01000003")).json()
    assert p["telemetry"]["lockup_meaningful"] is True


def test_greybox_zero_rm_id_is_not_loaded():
    p = call("GET", "/api/status", FakeBackend(rm_id="0x00000000")).json()
    assert p["rm"]["loaded"] is False
    assert p["rm"]["name"] is None


def test_rm_id_loaded_test_agrees_with_pyverify():
    """Tripwire on the transcription in ``api.rm_id_indicates_loaded``."""
    edge = pytest.importorskip("pyverify.edge")
    for rm_id in ("", "0x00000000", "0x0", "0x01000001", "0x0000001e",
                  "0xffffffff", "not-a-number"):
        assert api.rm_id_indicates_loaded(rm_id) == edge._rm_id_indicates_loaded(rm_id), rm_id


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #

def test_services_reflect_the_resident_rm():
    p = call("GET", "/api/services", FakeBackend(rm_id="0x01000003")).json()
    rows = {r["key"]: r for r in p["services"]}
    assert rows["uart1"]["present"] is True, "multicore has two UARTs"
    p = call("GET", "/api/services", FakeBackend(rm_id="0x01000001")).json()
    rows = {r["key"]: r for r in p["services"]}
    assert rows["uart1"]["present"] is False, "single-core nanosoc has one"


def test_services_survive_an_unreachable_board():
    """The shell's ports are a property of the shell, not of our ability to ask
    about them — the directory must still be useful when :6900 is down."""
    p = call("GET", "/api/services", FakeBackend(reachable=False)).json()
    assert p["ok"] is True
    rows = {r["key"]: r for r in p["services"]}
    assert rows["ctrl"]["present"] is True
    assert rows["uart0"]["present"] is False
    assert p["rm_known"] is False


def test_services_render_copy_pasteable_commands():
    p = call("GET", "/api/services", FakeBackend(), host_label="192.168.10.101").json()
    rows = {r["key"]: r for r in p["services"]}
    assert rows["uart0"]["command"] == "nc 192.168.10.101 6930"
    assert p["host"] == "192.168.10.101"


def test_services_report_the_registry_gap():
    p = call("GET", "/api/services", FakeBackend()).json()
    assert p["registry_gap"] == []


# --------------------------------------------------------------------------- #
# clocks
# --------------------------------------------------------------------------- #

def test_clocks_list_presets_with_derived_frequencies():
    p = call("GET", "/api/clocks", FakeBackend()).json()
    got = {row["name"]: row["mhz"] for row in p["presets"]}
    assert got == {"25mhz": 25.0, "50mhz": 50.0, "100mhz": 100.0}


def test_clocks_state_the_open_issue_rather_than_implying_a_frozen_interface():
    p = call("GET", "/api/clocks", FakeBackend()).json()
    assert p["contract"]["retune"] == "real"
    assert p["contract"]["open_issue"] == "I16"
    assert "not yet contract values" in p["contract"]["caveat"]


def test_set_clock_happy_path():
    b = FakeBackend()
    r = call("POST", "/api/clock", b, body=b'{"preset":"100mhz"}')
    assert r.status == 200
    p = r.json()
    assert p["ok"] is True and p["locked"] is True and p["preset"] == "100mhz"
    assert b.calls == [("set_clk", "100mhz")]


def test_set_clock_unknown_preset_is_rejected_without_touching_the_board():
    b = FakeBackend()
    r = call("POST", "/api/clock", b, body=b'{"preset":"33mhz"}')
    assert r.status == 400
    assert b.calls == [], "must not reach the board"
    assert "unknown preset" in r.json()["err"]


def test_set_clock_missing_arg_is_400():
    r = call("POST", "/api/clock", FakeBackend(), body=b"{}")
    assert r.status == 400 and "preset" in r.json()["err"]


def test_set_clock_warns_when_the_mmcm_did_not_lock():
    r = call("POST", "/api/clock", FakeBackend(locked=False), body=b'{"preset":"25mhz"}')
    p = r.json()
    assert p["ok"] is True and p["locked"] is False
    assert "did not report lock" in p["warn"]


def test_set_clock_transport_failure_is_502():
    r = call("POST", "/api/clock", FakeBackend(reachable=False), body=b'{"preset":"25mhz"}')
    assert r.status == 502 and r.json()["ok"] is False


# --------------------------------------------------------------------------- #
# resets
# --------------------------------------------------------------------------- #

def test_reset_happy_path():
    b = FakeBackend()
    r = call("POST", "/api/reset", b, body=b'{"target":"dut"}')
    assert r.status == 200 and r.json()["ok"] is True
    assert b.calls == [("reset", "dut")]


def test_reset_defaults_to_dut_with_an_empty_body():
    b = FakeBackend()
    r = call("POST", "/api/reset", b, body=b"")
    assert r.json()["ok"] is True and b.calls == [("reset", "dut")]


def test_reset_refuses_targets_the_shell_would_refuse_anyway():
    """coordinator.c:238 accepts only 'dut'. Rejecting here keeps a button that
    cannot work from ever being offered, and saves a pointless round trip."""
    b = FakeBackend()
    for target in ("rp", "dbg", "shell", ""):
        r = call("POST", "/api/reset", b, body=json.dumps({"target": target}).encode())
        assert r.status == 400, target
    assert b.calls == []


def test_reset_taxonomy_marks_exactly_one_kind_actionable():
    p = call("GET", "/api/resets", FakeBackend()).json()
    actionable = [k for k in p["kinds"] if k["actionable"]]
    assert [k["kind"] for k in actionable] == ["dut-reset"]
    assert p["targets"] == ["dut"]


def test_reset_taxonomy_carries_invalidation_sets():
    p = call("GET", "/api/resets", FakeBackend()).json()
    by_kind = {k["kind"]: k for k in p["kinds"]}
    assert by_kind["dfx-swap"]["rp_scoped"] is True
    assert "swd" in by_kind["dfx-swap"]["invalidated_channels"]
    assert by_kind["dut-reset"]["invalidated_channels"] == []


def test_reset_transport_failure_is_502():
    r = call("POST", "/api/reset", FakeBackend(reachable=False), body=b"{}")
    assert r.status == 502


# --------------------------------------------------------------------------- #
# diag + console
# --------------------------------------------------------------------------- #

def test_diag_returns_the_counters():
    p = call("GET", "/api/diag", FakeBackend()).json()
    assert p["ok"] is True
    for key in ("rx_drops", "icap_bytes", "pbuf_free", "snd_wnd"):
        assert key in p


def test_console_route_gives_an_attach_command_and_an_honest_ws_seam():
    p = call("GET", "/api/console/uart0", FakeBackend(), host_label="board").json()
    assert p["ok"] is True and p["port"] == 6930
    assert p["command"] == "nc board 6930"
    assert p["socat"].startswith("socat -,raw,echo=0 tcp:board:6930")
    assert p["websocket"]["available"] is False
    assert "not implemented" in p["websocket"]["reason"]


def test_console_route_rejects_non_console_services():
    for key in ("ctrl", "xvc", "nope"):
        r = call("GET", "/api/console/" + key, FakeBackend())
        assert r.status == 404, key


# --------------------------------------------------------------------------- #
# request hygiene
# --------------------------------------------------------------------------- #

def test_bad_json_uses_the_wire_contracts_own_error_string():
    r = call("POST", "/api/reset", FakeBackend(), body=b"{not json")
    assert r.status == 400 and r.json()["err"] == "bad json"


def test_non_object_json_body_is_rejected():
    r = call("POST", "/api/reset", FakeBackend(), body=b'["dut"]')
    assert r.status == 400 and "JSON object" in r.json()["err"]


def test_bodies_are_utf8_json_terminated_by_a_newline():
    r = call("GET", "/api/status", FakeBackend())
    assert r.body.endswith(b"\n")
    assert r.content_type == "application/json"
