"""Real, no-hardware tests for pyverify.client's JSON-line request/response
framing. Uses a fully in-memory fake Transport (no sockets at all) so the
framing/parsing logic is verified independent of any board or network —
see client.py's module docstring for why this is the testing seam.
"""
from __future__ import annotations

import json
from collections import deque

import pytest

from pyverify.client import (
    DEFAULT_CLK_PRESETS,
    MACGEN_INJECTS,
    DiagResponse,
    MacGenResponse,
    ShellClient,
    ShellProtocolError,
    TelemetryResponse,
    validate_clk_preset,
    validate_macgen_inject,
)


class FakeTransport:
    """Scripted transport: replies come from a queue keyed by call order.
    Records every line sent so tests can assert on outgoing requests too.
    """

    def __init__(self, responses: list[dict]) -> None:
        self._responses = deque(responses)
        self.sent: list[bytes] = []
        self.closed = False

    def send_line(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv_line(self) -> bytes:
        if not self._responses:
            raise AssertionError("FakeTransport: no more scripted responses")
        return json.dumps(self._responses.popleft()).encode("ascii")

    def close(self) -> None:
        self.closed = True


def _client(responses: list[dict]) -> tuple[ShellClient, FakeTransport]:
    transport = FakeTransport(responses)
    client = ShellClient("shell.example", transport=transport)
    client.connect()
    return client, transport


def test_ping_round_trip() -> None:
    client, transport = _client([{"ok": True, "shell_id": "0xA1B2C3D4", "rm_id": "0x1"}])
    resp = client.ping()
    assert resp.ok is True
    assert resp.shell_id == "0xA1B2C3D4"
    assert resp.rm_id == "0x1"
    assert json.loads(transport.sent[0]) == {"op": "ping"}


def test_swap_round_trip_sends_rm_and_src() -> None:
    client, transport = _client([{"ok": True, "rm_id": "0x2", "verified": True}])
    resp = client.swap(rm="nanosoc", src="tftp")
    assert resp.ok is True
    assert resp.rm_id == "0x2"
    assert resp.verified is True
    assert json.loads(transport.sent[0]) == {"op": "swap", "rm": "nanosoc", "src": "tftp"}


def test_telemetry_parses_the_v06_failure_shape() -> None:
    """net-protocol.md v0.6: telemetry ALWAYS fails (no power sensor), and its
    failure line carries the real `lockup` pin — the one declared carve-out to
    the uniform failure shape."""
    client, _ = _client(
        [{"ok": False, "err": "no power sensor", "lockup": True}]
    )
    resp = client.telemetry()
    assert resp.ok is False
    assert resp.err == "no power sensor"
    assert resp.lockup is True   # parsed regardless of ok — it is the only line


def test_telemetry_response_has_no_power_fields() -> None:
    """The regression this guards: TelemetryResponse used to carry
    `mv`/`ma` floats parsed with `float(resp.get("mv", 0.0))`. Against a v0.6
    shell that silently yields `mv=0.0` alongside `ok=False` — the plausible
    zero the protocol change exists to kill, resurrected host-side. The fields
    are REMOVED, not defaulted: there must be no attribute to misread."""
    client, _ = _client([{"ok": False, "err": "no power sensor", "lockup": False}])
    resp = client.telemetry()
    assert not hasattr(resp, "mv")
    assert not hasattr(resp, "ma")
    assert "mv" not in TelemetryResponse.__dataclass_fields__
    assert "ma" not in TelemetryResponse.__dataclass_fields__


def test_missing_ok_field_raises_protocol_error() -> None:
    client, _ = _client([{"shell_id": "0xdeadbeef"}])
    with pytest.raises(ShellProtocolError, match="missing required 'ok'"):
        client.ping()


def test_malformed_json_raises_protocol_error() -> None:
    class BadTransport:
        def send_line(self, payload: bytes) -> None:
            pass

        def recv_line(self) -> bytes:
            return b"{not json"

        def close(self) -> None:
            pass

    client = ShellClient("shell.example", transport=BadTransport())
    client.connect()
    with pytest.raises(ShellProtocolError, match="malformed JSON"):
        client.ping()


def test_request_before_connect_raises() -> None:
    client = ShellClient("shell.example")
    with pytest.raises(ShellProtocolError, match="not connected"):
        client.ping()


def test_context_manager_closes_transport() -> None:
    transport = FakeTransport([{"ok": True, "shell_id": "0x1", "rm_id": "0x1"}])
    with ShellClient("shell.example", transport=transport) as client:
        client.ping()
    assert transport.closed is True


# --------------------------------------------------------------------------- #
# set_clk client-side preset validation (W-HOST-MISC; OPEN_ISSUES.md I16)
#
# Split by design: ShellClient (raw protocol driver) is wire-transparent
# by default — the conformance suites (tests/firmware_logic/
# test_json_golden.py, tests/test_fakeshell.py) deliberately round-trip
# unknown presets through it to prove *server-side* rejection. Validation
# is opt-in here via `presets=...`; the user-facing Mps3Board facade
# validates by default (tests/test_board.py).
# --------------------------------------------------------------------------- #


def test_default_presets_mirror_clkrst_placeholder_table() -> None:
    # Must track firmware/clkrst/clkrst.c's clkrst_preset_table exactly
    # (the placeholder table; I16 will replace both in lockstep).
    assert DEFAULT_CLK_PRESETS == ("25mhz", "50mhz", "100mhz")


@pytest.mark.parametrize("preset", DEFAULT_CLK_PRESETS)
def test_set_clk_valid_preset_sends_request(preset: str) -> None:
    client, transport = _client([{"ok": True, "locked": True}])
    resp = client.set_clk(preset, presets=DEFAULT_CLK_PRESETS)
    assert resp.ok is True and resp.locked is True
    assert json.loads(transport.sent[0]) == {"op": "set_clk", "preset": preset}


def test_set_clk_default_is_wire_transparent() -> None:
    # No presets kwarg -> no client-side check: the raw driver must be
    # able to send *anything*, or the golden/fakeshell suites couldn't
    # exercise the server's own fail-closed lookup.
    client, transport = _client([{"ok": False, "locked": False}])
    resp = client.set_clk("13mhz")
    assert resp.ok is False
    assert json.loads(transport.sent[0]) == {"op": "set_clk", "preset": "13mhz"}


@pytest.mark.parametrize("typo", ["25 mhz", "25MHz", "25", "", "125mhz"])
def test_set_clk_opt_in_typo_raises_before_any_traffic(typo: str) -> None:
    client, transport = _client([])
    with pytest.raises(ValueError, match="unknown preset"):
        client.set_clk(typo, presets=DEFAULT_CLK_PRESETS)
    assert transport.sent == []          # nothing hit the wire


def test_set_clk_error_message_names_source_and_escape_hatch() -> None:
    client, _ = _client([])
    with pytest.raises(ValueError) as excinfo:
        client.set_clk("999mhz", presets=DEFAULT_CLK_PRESETS)
    msg = str(excinfo.value)
    assert "clkrst.c" in msg             # cites the firmware table
    assert "I16" in msg                  # cites the open contract issue
    assert "presets=None" in msg         # names the escape hatch
    for known in DEFAULT_CLK_PRESETS:
        assert known in msg


def test_set_clk_custom_presets_sequence() -> None:
    client, transport = _client([{"ok": True}])
    client.set_clk("200mhz", presets=("200mhz", "300mhz"))
    assert json.loads(transport.sent[0]) == {"op": "set_clk", "preset": "200mhz"}
    with pytest.raises(ValueError, match="unknown preset"):
        client.set_clk("25mhz", presets=("200mhz", "300mhz"))


def test_validate_clk_preset_helper_directly() -> None:
    validate_clk_preset("50mhz")                     # default table: fine
    validate_clk_preset("anything", presets=None)    # passthrough: fine
    with pytest.raises(ValueError, match="unknown preset"):
        validate_clk_preset("50 MHz")


# --------------------------------------------------------------------------- #
# macgen (MAC-in-operation control verb; net-protocol.md "MAC gen/checker
# control", I10 tail)
# --------------------------------------------------------------------------- #


def test_macgen_round_trip_sends_gen_chk_inject_and_parses_counters() -> None:
    client, transport = _client([{"ok": True, "tx": 16, "rx": 16, "err": 1}])
    resp = client.macgen(gen=True, chk=True, inject="bad_fcs")
    assert isinstance(resp, MacGenResponse)
    assert resp.ok is True
    assert (resp.tx, resp.rx, resp.err) == (16, 16, 1)
    assert json.loads(transport.sent[0]) == {
        "op": "macgen", "gen": True, "chk": True, "inject": "bad_fcs",
    }


def test_diag_round_trip_parses_all_counters() -> None:
    # The windowed-grant deadlock shape: grant ACCEPTED (grants_sent=1) but stuck
    # at the first window; sndbuf/snd_wnd both nonzero (so it's not a send-refused).
    client, transport = _client([{
        "ok": True, "rx_recover": 0, "rx_dumps": 0, "rx_drops": 0,
        "icap_bytes": 256, "got": 4096, "expect": 68288,
        "rcv_wnd": 2048, "rcv_ann_wnd": 2048, "rx_queued": 0, "pbuf_free": 8,
        "grants_sent": 1, "grant_fails": 0, "sndbuf": 4096, "snd_wnd": 64240,
    }])
    resp = client.diag()
    assert isinstance(resp, DiagResponse)
    assert resp.ok is True
    assert (resp.rx_recover, resp.rx_dumps, resp.rx_drops) == (0, 0, 0)
    assert resp.icap_bytes == 256
    assert (resp.got, resp.expect) == (4096, 68288)
    assert (resp.rcv_wnd, resp.rcv_ann_wnd) == (2048, 2048)
    assert (resp.rx_queued, resp.pbuf_free) == (0, 8)
    assert (resp.grants_sent, resp.grant_fails) == (1, 0)
    assert (resp.sndbuf, resp.snd_wnd) == (4096, 64240)
    assert json.loads(transport.sent[0]) == {"op": "diag"}


def test_diag_defaults_when_fields_absent() -> None:
    client, _ = _client([{"ok": True}])
    resp = client.diag()
    assert resp.ok is True
    assert (resp.rx_recover, resp.icap_bytes, resp.got, resp.pbuf_free) == (0, 0, 0, 0)
    assert (resp.grants_sent, resp.grant_fails, resp.sndbuf, resp.snd_wnd) == (0, 0, 0, 0)


def test_macgen_defaults_are_gen_chk_on_inject_none() -> None:
    client, transport = _client([{"ok": True, "tx": 8, "rx": 8, "err": 0}])
    client.macgen()
    assert json.loads(transport.sent[0]) == {
        "op": "macgen", "gen": True, "chk": True, "inject": "none",
    }


def test_macgen_failure_reply_does_not_int_the_string_err() -> None:
    # On failure the wire "err" is the diagnostic *string*; the client must
    # NOT try to int() it — counters fail closed to 0 (polymorphic "err" key).
    client, _ = _client([{"ok": False, "err": "bad inject"}])
    resp = client.macgen(gen=True, chk=True, inject="smash")  # wire-transparent
    assert resp.ok is False
    assert (resp.tx, resp.rx, resp.err) == (0, 0, 0)


def test_macgen_default_is_wire_transparent() -> None:
    # No injects kwarg -> no client-side check: the raw driver sends anything
    # so the conformance suites can prove the server's own rejection.
    client, transport = _client([{"ok": False, "err": "bad inject"}])
    client.macgen(inject="smash")
    assert json.loads(transport.sent[0])["inject"] == "smash"


@pytest.mark.parametrize("typo", ["BAD_FCS", "bad-fcs", "", "fcs", "none "])
def test_macgen_opt_in_inject_typo_raises_before_any_traffic(typo: str) -> None:
    client, transport = _client([])
    with pytest.raises(ValueError, match="unknown inject"):
        client.macgen(inject=typo, injects=MACGEN_INJECTS)
    assert transport.sent == []          # nothing hit the wire


def test_validate_macgen_inject_helper_directly() -> None:
    for name in MACGEN_INJECTS:
        validate_macgen_inject(name)                 # every real fault: fine
    validate_macgen_inject("anything", injects=None)  # passthrough: fine
    with pytest.raises(ValueError, match="unknown inject"):
        validate_macgen_inject("smash")
