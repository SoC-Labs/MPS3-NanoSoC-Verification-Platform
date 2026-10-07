"""tests/firmware_logic/test_json_golden.py — E2E-2, the cross-language
golden test for the control-channel JSON-line codec (W-JSON).

Both halves are the REAL artifacts, not re-implementations:

- **Host half:** ``pyverify.client.ShellClient`` — the exact client that
  must interoperate with the shell (``json.dumps(op, separators=(",",":"))``
  requests, ``json.loads`` + response-dataclass parsing). It is driven
  through its own ``Transport`` seam (the same seam
  ``host/pyverify/tests/test_client.py`` uses), so every request line on
  the wire below is produced by client.py's own code path.
- **Firmware half:** ``firmware/test/bin/ctrl_echo`` — a stdin/stdout
  harness (built here via the firmware Makefile's own recipe) that runs
  each line through the REAL firmware path: ``net_proto.c`` decode ->
  ``coordinator.c`` dispatch/handlers -> ``clkrst.c``/``swap_fsm.c``
  against mock registers + module fakes -> ``net_proto.c`` per-op encode.
  Its fixed scenario (static_id 0xa1b2c3d4, MMCM locked, swaps land+verify
  rm_id 1, NO CARD in the user microSD -- so every v0.13 ``commit`` and ``usd``
  action is refused ``no card``) is documented in ``firmware/test/ctrl_echo.c``
  and mirrored by ``firmware/test/test_coordinator_dispatch.c``. It no longer
  seeds TELEM: as of net-protocol.md v0.6 ``telemetry`` does not read it (see
  ``test_telemetry_always_fails_no_power_sensor`` below).

The assertion, per verb: the C-emitted response line parses as JSON, is
accepted by the pyverify response dataclass for that verb, and carries the
per-op field values net-protocol.md's table promises.

Import bootstrap: same ``sys.path`` pattern as
``tests/integration/test_swap_sequence.py`` (pyverify is not required to be
pip-installed for the repo test suite to run).
"""
from __future__ import annotations

import json
import select
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FW_TEST_DIR = _REPO_ROOT / "firmware" / "test"
_CTRL_ECHO = _FW_TEST_DIR / "bin" / "ctrl_echo"

_PYVERIFY_DIR = str(_REPO_ROOT / "host" / "pyverify")
if _PYVERIFY_DIR not in sys.path:
    sys.path.insert(0, _PYVERIFY_DIR)

from pyverify.client import (  # noqa: E402  (path bootstrap must come first)
    CommitResponse,
    LinkResponse,
    MacGenResponse,
    PingResponse,
    ResetResponse,
    SetClkResponse,
    ShellClient,
    SwapResponse,
    TelemetryResponse,
    UsdResponse,
    VersionResponse,
)

_MAKE = shutil.which("make")
_CC = shutil.which("gcc") or shutil.which("cc")

pytestmark = [
    pytest.mark.skipif(not _FW_TEST_DIR.is_dir(), reason="firmware/test/ not present"),
    pytest.mark.skipif(_MAKE is None, reason="no `make` on PATH -- cannot build ctrl_echo"),
    pytest.mark.skipif(_CC is None, reason="no gcc/cc on PATH -- firmware/test/Makefile needs one"),
]

# What ctrl_echo.c's fixed scenario promises (see its header comment) --
# duplicated here as the *expected* half of the golden pairing.
SHELL_ID = "0xa1b2c3d4"
BOOT_RM_ID = "0x00000000"
SWAPPED_RM_ID = "0x00000001"
# telemetry's one and only reply (net-protocol.md v0.6 — there is no power
# sensor on this platform, so the verb has no success shape at all). The
# diagnostic is contract-fixed, not a free-form message: it is what
# coordinator.c's set_err(resp, "no power sensor") emits and what the
# fakeshell must emit byte-for-byte.
TELEM_ERR = "no power sensor"
# GENCHK counters ctrl_echo.c seeds (distinct so a tx/rx/err mixup can't pass).
MACGEN_TX = 1234
MACGEN_RX = 1230
MACGEN_ERR = 4

# What ctrl_echo reports for `version` (net-protocol.md v0.8). It links ONLY
# ../platform/mps3_version_weak.c -- the committed WEAK fallback -- and NOT the
# generated strong override, so it reports the honest "not provisioned"
# identity. That is deliberate on both sides: it makes this golden a FIXED
# expectation rather than one that moves with every commit, and it exercises the
# fallback path a fresh clone is in. It is also built with none of the feature
# -D flags, so the feature array is EMPTY -- the single most load-bearing row
# here, because "no flags" is what a plain `make elf` produces and what a
# mis-built board would report.
VERSION_HARNESS = "0.0.0"
VERSION_VER32 = "0x00000000"
VERSION_SHA = "unknown"
VERSION_LMB_KB = 1024      # firmware/platform/Makefile's LMB_KB default

# The user microSD (net-protocol.md v0.13). ctrl_echo has NO CARD in the slot,
# and the contract quotes the no-card status line verbatim, so it is pinned
# byte-for-byte. Every commit / usd action then refuses with the NAME "no card".
USD_NO_CARD_LINE = b'{"ok":true,"present":false,"state":"none","text":"none","boot":"none"}'
USD_NO_CARD_ERR = "no card"
#: A well-formed v0.13 commit of what ctrl_echo is running (its static_id, the
#: greybox rm_id 0), as pyverify's commit_begin() encodes it.
COMMIT_FIELDS = dict(rm_id=0, static_id=0xA1B2C3D4, clear_len=64, clear_crc=0x11111111,
                     part_len=128, part_crc=0x22222222)


class CtrlEchoTransport:
    """``pyverify.client.Transport`` over the ctrl_echo subprocess's pipes.

    One line each way, exactly like ``SocketTransport`` minus the socket.
    ``recv_line`` is select()-guarded so a wedged/crashed C process fails
    the test in bounded time instead of hanging pytest.
    """

    def __init__(self, proc: subprocess.Popen, timeout: float = 10.0):
        self._proc = proc
        self._timeout = timeout

    def send_line(self, payload: bytes) -> None:
        self._proc.stdin.write(payload + b"\n")
        self._proc.stdin.flush()

    def recv_line(self) -> bytes:
        ready, _, _ = select.select([self._proc.stdout], [], [], self._timeout)
        if not ready:
            raise TimeoutError(
                f"no response line from ctrl_echo within {self._timeout}s "
                f"(rc={self._proc.poll()!r})"
            )
        line = self._proc.stdout.readline()
        if not line:
            raise ConnectionError("ctrl_echo closed stdout (crashed?)")
        return line.rstrip(b"\n")

    def close(self) -> None:  # pragma: no cover - ShellClient.close() path
        pass


@pytest.fixture(scope="session")
def ctrl_echo_bin() -> Path:
    """Builds bin/ctrl_echo via the firmware Makefile's own recipe (the
    single source of truth for how firmware test binaries are built --
    same rationale as test_firmware_host_gcc_harness.py)."""
    result = subprocess.run(
        ["make", "bin/ctrl_echo"],
        cwd=str(_FW_TEST_DIR),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"building ctrl_echo failed:\n{result.stdout}\n{result.stderr}"
    )
    assert _CTRL_ECHO.exists()
    return _CTRL_ECHO


@pytest.fixture
def shell(ctrl_echo_bin: Path):
    """A connected ShellClient talking to a FRESH ctrl_echo process (fresh
    per test so verb ordering inside one test is meaningful and tests stay
    independent -- e.g. the swap test moves the reported rm_id)."""
    proc = subprocess.Popen(
        [str(ctrl_echo_bin)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=str(_FW_TEST_DIR),
    )
    client = ShellClient("ctrl-echo", transport=CtrlEchoTransport(proc))
    try:
        yield client
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait()


@pytest.fixture
def transport(ctrl_echo_bin: Path):
    """Raw transport (no ShellClient) for below-the-client wire tests."""
    proc = subprocess.Popen(
        [str(ctrl_echo_bin)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=str(_FW_TEST_DIR),
    )
    try:
        yield CtrlEchoTransport(proc)
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait()


# --------------------------------------------------------------------------- #
# Per-verb golden pairs (client generates, C parses+responds, client parses)
# --------------------------------------------------------------------------- #


def test_ping(shell: ShellClient):
    r = shell.ping()
    assert isinstance(r, PingResponse)
    assert r.ok is True
    assert r.shell_id == SHELL_ID
    assert r.rm_id == BOOT_RM_ID


def test_reset(shell: ShellClient):
    r = shell.reset("dut")
    assert isinstance(r, ResetResponse)
    assert r.ok is True


def test_reset_bad_target_is_clean_failure(shell: ShellClient):
    r = shell.reset("mainframe")
    assert isinstance(r, ResetResponse)
    assert r.ok is False


def test_set_clk(shell: ShellClient):
    r = shell.set_clk("25mhz")
    assert isinstance(r, SetClkResponse)
    assert r.ok is True
    assert r.locked is True


def test_set_clk_unknown_preset_is_clean_failure(shell: ShellClient):
    r = shell.set_clk("13mhz")
    assert r.ok is False
    assert r.locked is False


def test_swap_reports_verified_rm_id(shell: ShellClient):
    """The contract's inner loop: swap responds (held response) with the
    confirmed rm_id + verified, and a subsequent ping agrees the shell's
    world moved."""
    r = shell.swap(rm="nanosoc", src="tftp")
    assert isinstance(r, SwapResponse)
    assert r.ok is True
    assert r.verified is True
    assert r.rm_id == SWAPPED_RM_ID

    after = shell.ping()
    assert after.rm_id == SWAPPED_RM_ID
    assert after.shell_id == SHELL_ID  # static didn't change, only the RM


def test_link(shell: ShellClient):
    for event in ("down", "up", "pulse"):
        r = shell.link(event)
        assert isinstance(r, LinkResponse)
        assert r.ok is True, f"link event {event!r} rejected"
    assert shell.link("sideways").ok is False


def test_commit_v013_no_card_is_refused_before_the_park(shell: ShellClient):
    """v0.13 `commit` is a RE-PUSH that parks like `swap` -- but a refusal comes
    back at once, before any park. With no card that refusal is the NAME
    "no card", and commit_await() collects it without a push."""
    shell.commit_begin("nanosoc", **COMMIT_FIELDS)
    r = shell.commit_await()
    assert isinstance(r, CommitResponse)
    assert (r.ok, r.slot, r.err) == (False, "", USD_NO_CARD_ERR)
    assert shell.ping().ok                        # the session stayed in step


def test_commit_v011_form_is_bad_args(shell: ShellClient):
    """v0.13 REPLACED `commit`: the old rm-only line is refused "bad args"."""
    r = shell.commit("nanosoc")
    assert isinstance(r, CommitResponse)
    assert (r.ok, r.slot, r.err) == (False, "", "bad args")


def test_usd_status_no_card(shell: ShellClient):
    r = shell.usd()
    assert isinstance(r, UsdResponse)
    assert (r.ok, r.present, r.state, r.text, r.boot) == (True, False, "none", "none", "none")
    assert r.card_mb is None and r.default is None and not r.committable


def test_diag_carries_the_v9_words(shell: ShellClient):
    """diag.h v9 (D13) appends svc_us_6 and usd_boot; the codec must carry both
    to DiagResponse. (What usd_boot HOLDS -- the power-on latch, agreeing with
    usd.boot -- is test_fakeshell_conformance.py's
    test_usd_boot_latch_agrees_with_usd_boot.)"""
    d = shell.diag()
    assert d.ok and d.has("svc_us_6") and d.has("usd_boot")
    assert len(d.present) == 44


def test_usd_actions_with_no_card(shell: ShellClient):
    for r in (shell.usd_format("erase"), shell.usd_clear(), shell.usd_rescan()):
        assert isinstance(r, UsdResponse)
        assert (r.ok, r.err) == (False, USD_NO_CARD_ERR)


def test_telemetry_always_fails_no_power_sensor(shell: ShellClient):
    """telemetry has NO SUCCESS SHAPE (net-protocol.md v0.6).

    Replaces ``test_telemetry_floats_parse_from_integer_wire_values``, which
    pinned the client's ``float()`` parsing of the integer ``mv``/``ma`` wire
    values. That behaviour no longer exists to pin: there is no power sensor
    reachable from this design by any path, so ``mv``/``ma`` were removed from
    the protocol rather than left reporting a permanent, plausible zero. The
    verb is still one of the eight and still needs a golden pair here — so the
    case is REPURPOSED to pin the new truth, not deleted.

    The negative assertions are the point: they are what fails if anyone
    "helpfully" re-adds a zero-defaulted ``mv`` to either half."""
    r = shell.telemetry()
    assert isinstance(r, TelemetryResponse)
    assert r.ok is False                    # always — there is nothing to succeed at
    assert r.err == TELEM_ERR
    # lockup is real, is carried on the failure line (the one declared
    # carve-out to the uniform failure shape), and parses regardless of ok.
    assert r.lockup is False

    # mv/ma are ABSENT, not zero -- on the wire AND on the dataclass.
    assert not hasattr(r, "mv") and not hasattr(r, "ma"), (
        "TelemetryResponse grew mv/ma back: a zero-defaulted power field is "
        "exactly the reading-shaped lie v0.6 removed"
    )


def test_version_reports_build_identity(shell: ShellClient):
    """`version` (v0.8) — the verb that makes "which firmware is on this board"
    answerable over the wire.

    `ping` cannot answer it: it reports the FABRIC's static_id and the resident
    rm_id, and one static_id serves many harness releases (a firmware-only bump
    re-bakes the .bit via updatemem without moving the static routing). So a
    board can report the expected static_id, pass every gate, and be running an
    image built with the wrong flags — which is exactly what a LITE-vs-FIFO
    HWICAP mismatch looks like from outside.

    The pairing here is the real one: pyverify's ShellClient generates the
    request line, the REAL firmware codec + handler answer it, and the response
    parses back into VersionResponse."""
    r = shell.version()
    assert isinstance(r, VersionResponse)
    assert r.ok is True
    assert r.harness == VERSION_HARNESS
    assert r.ver32 == VERSION_VER32
    assert r.sha == VERSION_SHA
    assert r.dirty is False
    assert r.lmb_kb == VERSION_LMB_KB
    # ctrl_echo is built with -DMPS3_HAS_DUT_EGRESS and no other feature -D
    # flag. v0.11 appends the always-compiled-in services (jtag_server, the XVC
    # target, stats/log/reboot), so the array is no longer empty -- but it is
    # still FIXED and in bit order, which is what this pins. A tuple.
    assert r.features == ("dut_egress", "jtag_server", "xvc_dbgbr",
                          "stats", "log", "reboot",
                          "usd")      # v0.13: compiled into every image (bit 13)

    # And it is orthogonal to ping: the same session's identity axes do not
    # bleed into each other (no static_id in `version`, no harness in `ping`).
    assert shell.ping().shell_id == SHELL_ID


def test_macgen_drives_genchk_and_reports_counters(shell: ShellClient):
    """The MAC-in-operation verb (I10 tail): the C drives GENCHK.CTRL/INJECT
    and reports the three counters back. ctrl_echo seeds distinct TX/RX/ERR,
    so this pins both the wire encoding and the tx/rx/err field mapping."""
    r = shell.macgen(gen=True, chk=True, inject="bad_fcs")
    assert isinstance(r, MacGenResponse)
    assert r.ok is True
    assert (r.tx, r.rx, r.err) == (MACGEN_TX, MACGEN_RX, MACGEN_ERR)


def test_macgen_unknown_inject_is_clean_failure(shell: ShellClient):
    r = shell.macgen(gen=True, chk=True, inject="smash")  # wire-transparent
    assert r.ok is False
    assert (r.tx, r.rx, r.err) == (0, 0, 0)  # counters not parsed on failure


def test_full_session_over_one_connection(shell: ShellClient):
    """The core verbs back-to-back on one 'connection', in the deploy
    loop's natural order -- the framing must stay in sync line for line."""
    assert shell.ping().ok
    assert shell.set_clk("25mhz").ok
    assert shell.reset("dut").ok
    swap = shell.swap(rm="nanosoc", src="tftp")
    assert swap.ok and swap.verified
    # v0.13: persist what is now running (rm_id 1) -- refused at once, no card
    shell.commit_begin("nanosoc", **dict(COMMIT_FIELDS, rm_id=int(SWAPPED_RM_ID, 16)))
    assert shell.commit_await().err == USD_NO_CARD_ERR
    assert shell.usd().present is False
    assert shell.link("up").ok
    # telemetry ALWAYS fails (v0.6: no power sensor) -- what this test cares
    # about is that its reply is still exactly one line, so the session stays
    # in sync and the verbs after it still land.
    telem = shell.telemetry()
    assert telem.ok is False and telem.err == TELEM_ERR
    assert shell.macgen(gen=True, chk=True, inject="none").ok
    assert shell.ping().rm_id == SWAPPED_RM_ID


# --------------------------------------------------------------------------- #
# Below-the-client wire checks
# --------------------------------------------------------------------------- #


def test_request_lines_are_exactly_pyverify_encoding(transport: CtrlEchoTransport):
    """Belt and braces: hand-build each request with the same encoder call
    client.py uses (json.dumps + compact separators) and check the firmware
    ACCEPTS every one -- so if ShellClient's internals ever change, the
    wire contract itself is still pinned here.

    "Accepted" != "succeeded". Seven verbs accept-and-succeed; ``telemetry``
    accepts-and-fails, by design and always (v0.6: no power sensor). So its
    row asserts the *exact* v0.6 diagnostic rather than just ``ok is False``
    -- that is what distinguishes "the line reached
    coordinator_handle_telemetry() and it did its job" from "the decoder threw
    the line out as garbage", which a bare ok:false could not."""
    ops = [
        {"op": "ping"},
        {"op": "reset", "target": "dut"},
        {"op": "set_clk", "preset": "25mhz"},
        {"op": "swap", "rm": "nanosoc", "src": "tftp"},
        {"op": "link", "event": "down"},
        {"op": "macgen", "gen": True, "chk": True, "inject": "bad_fcs"},
        {"op": "usd"},
    ]
    for op in ops:
        line = json.dumps(op, separators=(",", ":")).encode("ascii")
        transport.send_line(line)
        resp = json.loads(transport.recv_line())
        assert isinstance(resp, dict) and resp.get("ok") is True, (
            f"firmware rejected canonical request {line!r}: {resp!r}"
        )

    # telemetry: the canonical request line is accepted and answered by its
    # handler -- with the verb's only reply, a failure.
    line = json.dumps({"op": "telemetry"}, separators=(",", ":")).encode("ascii")
    transport.send_line(line)
    resp = json.loads(transport.recv_line())
    assert isinstance(resp, dict), resp
    assert resp.get("ok") is False and resp.get("err") == TELEM_ERR, (
        f"firmware did not answer canonical request {line!r} with the v0.6 "
        f"telemetry failure: {resp!r}"
    )
    assert "lockup" in resp                       # the carve-out field survives
    assert "mv" not in resp and "ma" not in resp  # ...and the dead ones are gone

    # commit (v0.13): the canonical line is DECODED and reaches the handler,
    # which refuses it for the scenario's reason ("no card") -- not "bad args",
    # which is what a line the decoder threw out would get. The retired v0.11
    # line is the opposite: "bad args".
    canonical = {"op": "commit", "rm": "nanosoc", "src": "tcp", "rm_id": "0x00000000",
                 "static_id": "0xa1b2c3d4", "clear_len": 64, "clear_crc": "0x11111111",
                 "part_len": 128, "part_crc": "0x22222222"}
    for op, err in ((canonical, USD_NO_CARD_ERR),
                    ({"op": "commit", "rm": "nanosoc"}, "bad args")):
        line = json.dumps(op, separators=(",", ":")).encode("ascii")
        transport.send_line(line)
        assert json.loads(transport.recv_line()) == {"ok": False, "err": err}, line


def test_malformed_lines_get_parseable_error_responses(transport: CtrlEchoTransport):
    """Garbage in must still produce one syntactically valid JSON error
    line out (never silence, never a broken line) -- the client-side
    ShellProtocolError path is for transport corruption, not for firmware
    rejections."""
    for bad in (b"not json", b"{\"op\":\"ping\"", b"{}", b"{\"op\":\"selfdestruct\"}",
                b"{\"op\":\"reset\"}", b"[1,2,3]"):
        transport.send_line(bad)
        resp = json.loads(transport.recv_line())  # must parse
        assert resp["ok"] is False
        assert isinstance(resp["err"], str) and resp["err"]


def test_responses_are_compact_single_lines(transport: CtrlEchoTransport):
    """Shape pinning: one '\\n'-terminated object per request, no embedded
    newlines, byte-identical to what test_coordinator_dispatch.c asserts
    (the same numbers appear there as C literals)."""
    transport.send_line(b'{"op":"ping"}')
    raw = transport.recv_line()
    assert raw == b'{"ok":true,"shell_id":"0xa1b2c3d4","rm_id":"0x00000000"}'
    transport.send_line(b'{"op":"telemetry"}')
    assert transport.recv_line() == b'{"ok":false,"err":"no power sensor","lockup":false}'
    transport.send_line(b'{"op":"usd"}')
    assert transport.recv_line() == USD_NO_CARD_LINE


def test_commit_begin_line_is_what_the_firmware_decodes(ctrl_echo_bin: Path):
    """The exact bytes commit_begin() puts on the wire go through the REAL
    decoder: hex ids/CRCs as "0x" + 8 lowercase digits, lengths as integers."""
    sent = []

    class _Tap(CtrlEchoTransport):
        def send_line(self, payload: bytes) -> None:
            sent.append(payload)
            super().send_line(payload)

    proc = subprocess.Popen([str(ctrl_echo_bin)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, cwd=str(_FW_TEST_DIR))
    try:
        c = ShellClient("ctrl-echo", transport=_Tap(proc))
        c.commit_begin("nanosoc", **COMMIT_FIELDS)
        assert c.commit_await().err == USD_NO_CARD_ERR
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
    assert sent == [
        b'{"op":"commit","rm":"nanosoc","src":"tcp","rm_id":"0x00000000",'
        b'"static_id":"0xa1b2c3d4","clear_len":64,"clear_crc":"0x11111111",'
        b'"part_len":128,"part_crc":"0x22222222"}']


# --------------------------------------------------------------------------- #
# net-protocol v0.11 -- stats / log / touch_cal / reboot through the REAL codec
# --------------------------------------------------------------------------- #

#: fpgahub's _from_stats_verb key shape, in order, then the v0.11 extras.
STATS_KEYS = (
    "ok", "up_ms", "sid", "rm", "rm_ok", "lock", "clk_sel", "mmcm", "clk_alive",
    "dut_rst", "rp_rst", "decpl", "link", "spd", "fdx", "mac", "swap", "swap_ok",
    "swap_n", "icap", "rxdrop", "txerr",
    "swap_err", "clr_ok", "dut_mhz", "svc_max_us", "svc_skipped",
)


def test_v011_stats_key_shape(shell: ShellClient):
    r = shell.stats()
    assert r.ok is True
    assert tuple(r.raw.keys()) == STATS_KEYS      # exact shape AND order
    assert r.sid == SHELL_ID
    assert r.swap == "idle"
    assert not any(k in r.raw for k in ("mv", "ma", "power_mw"))


def test_v011_log_empty_ring_is_a_result(shell: ShellClient):
    r = shell.log()
    assert r.ok is True
    assert (r.off, r.n, r.more, r.dropped, r.data) == (0, 0, False, 0, b"")


def test_v011_touch_cal_declines_without_touch(shell: ShellClient):
    r = shell.touch_cal("get")
    assert r.ok is False and r.err == "touch not present"


def test_v011_reboot_refuses_without_a_watchdog(shell: ShellClient):
    # ctrl_echo's mock register file has no live WDOG timebase at 0x44B4.
    r = shell.reboot()
    assert r.ok is False and r.err == "no watchdog"
