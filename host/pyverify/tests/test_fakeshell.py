"""End-to-end tests for ``pyverify.testing.fakeshell`` — the stdlib-only
reference implementation of the shell's network surface.

Everything here runs over REAL sockets on 127.0.0.1 with OS-assigned
(ephemeral) ports: the production client stack (:class:`ShellClient`,
:class:`ConsoleReader`, :class:`EdgeDeviceApi`) talks to the fake shell
exactly as it would to the board, so this suite exercises both sides of
every contract clause it names (net-protocol.md verbs + bitstream framing
+ I2 ordering; overlay-manifest.md A/B commit semantics).

Framing helpers come from ``pyverify.testing._pusher`` (the tolerant
import of the pusher's real ``frame_bitstream``), so the frames pushed
here are byte-identical to what the production pusher emits.

**Ordering (2026-07-14).** Every push here happens INSIDE a parked swap
(:func:`_parked_swap`), because that is the real protocol: the shell only
listens for a bitstream once the ``swap`` RPC has driven its FSM into
``SWAP_AWAIT_INCOMING_CLEARING``/``SWAP_AWAIT_PARTIAL``, and it parks the 6900
control connection for the whole reconfiguration precisely so that can happen.
These tests used to push first and swap second; the fake let them, which is how
a real host-side ordering bug stayed green (see the fakeshell module docstring).
"""
from __future__ import annotations

import contextlib
import json
import socket

import pytest

from pyverify.client import ShellClient, SwapResponse
from pyverify.console import ConsoleReader
from pyverify.edge import EdgeDeviceApi
from pyverify.testing._pusher import BitstreamKind, frame_bitstream
from pyverify.testing.fakeshell import (
    MPS3_BITSTREAM_VER,
    FakeShell,
    TftpError,
    raw_tcp_put,
    tftp_put,
)
from pyverify.testing.swap_model import STATIC_ID

#: A swap that nothing is ever pushed into fails on the FSM's AWAIT idle
#: timeout. Tests that WANT that outcome (or that push something the shell
#: rejects, so the pair never completes) set it short to stay fast — but it
#: MUST stay above ``FakeShell.swap_arm_grace`` (default 1.0s): a push and the
#: swap that awaits it run on different threads, and the push (even one the
#: shell will reject) has to be admitted BEFORE the await times out, or the test
#: races the idle timeout and sees ERR_NOT_AWAITING instead of the real reason.
FAST_AWAIT = 2.0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _pair_frames(*, static_id: int = STATIC_ID, rm_id: int = 1,
                 clearing_payload: bytes = b"\xC1" * 64,
                 partial_payload: bytes = b"\xB2" * 128):
    """Build a {clearing, partial} frame pair with the pusher's real framing."""
    clearing = frame_bitstream(
        clearing_payload, kind=BitstreamKind.CLEARING, rm_slot=0,
        static_id=static_id, rm_id=rm_id,
    )
    partial = frame_bitstream(
        partial_payload, kind=BitstreamKind.PARTIAL, rm_slot=0,
        static_id=static_id, rm_id=rm_id,
    )
    return clearing, partial


def _push_pair(fake: FakeShell, *, transport: str, rm_id: int = 1, **kwargs) -> None:
    clearing, partial = _pair_frames(rm_id=rm_id, **kwargs)
    if transport == "tftp":
        tftp_put(fake.host, fake.tftp_port, clearing, filename="clear.bin")
        tftp_put(fake.host, fake.tftp_port, partial, filename="partial.bin")
    else:
        raw_tcp_put(fake.host, fake.raw_tcp_port, clearing)
        raw_tcp_put(fake.host, fake.raw_tcp_port, partial)


class _ParkedSwap:
    """Handle onto an in-flight (parked) swap; ``.response`` is filled in with
    the reply the shell releases once the block exits."""

    response: SwapResponse | None = None


@contextlib.contextmanager
def _parked_swap(shell: ShellClient, rm: str = "nanosoc", src: str = "tftp"):
    """THE REAL ORDER: send ``swap`` (which does not reply — it parks), push the
    pair inside the ``with`` block, then collect the parked reply on exit.

    This is what :meth:`pyverify.swap.SwapOrchestrator.deploy` does, and it is
    the only order the shell accepts a bitstream in. A single-threaded client
    can do it precisely because the shell does not answer ``swap`` until the
    push has arrived.
    """
    parked = _ParkedSwap()
    shell.swap_begin(rm, src)
    yield parked
    parked.response = shell.swap_await()


# --------------------------------------------------------------------------- #
# Control channel: the full 7-verb matrix through a real ShellClient
# --------------------------------------------------------------------------- #

def test_ping_reports_configured_identity():
    with FakeShell.ephemeral(static_id=0x12345678, boot_rm_id=0) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            resp = shell.ping()
    assert resp.ok
    assert int(resp.shell_id, 0) == 0x12345678
    assert int(resp.rm_id, 0) == 0  # greybox at boot


def test_reset_accepts_known_targets_and_rejects_unknown():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            assert shell.reset("dut").ok       # the contract's example
            assert shell.reset("rp").ok
            assert not shell.reset("warp-core").ok
        assert fake.resets == ["dut", "rp"]


def test_set_clk_reports_locked_and_validates_presets():
    with FakeShell.ephemeral(clk_presets=("25mhz", "50mhz"), clk_locked=True) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            good = shell.set_clk("25mhz")
            bad = shell.set_clk("9.999ghz")
    assert good.ok and good.locked
    assert not bad.ok and not bad.locked
    assert fake.clk_requests == ["25mhz"]


def test_link_up_down_pulse_and_unknown_event_rejected():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            assert shell.link("down").ok
            assert fake.link_up is False
            assert shell.link("up").ok
            assert fake.link_up is True
            # "pulse" (I31(b)): accepted, steady state unchanged.
            assert shell.link("pulse").ok
            assert fake.link_up is True
            assert not shell.link("sideways").ok
        assert fake.link_events == ["down", "up", "pulse"]


def test_telemetry_always_fails_and_reports_the_configured_lockup_pin():
    """net-protocol.md v0.6: the fake shell, like the firmware, has no power
    sensor to report — telemetry answers {"ok":false,"err":"no power sensor",
    "lockup":<pin>}. The `lockup` seed is all that survives (mv/ma are not
    keys of the protocol, so the fake has no seeds for them either: a fake
    that could still be handed a rail voltage would be modelling hardware that
    does not exist)."""
    with FakeShell.ephemeral(telemetry_lockup=True) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            resp = shell.telemetry()
    assert resp.ok is False
    assert resp.err == "no power sensor"
    assert resp.lockup is True
    assert not hasattr(resp, "mv") and not hasattr(resp, "ma")


def test_v011_commit_form_is_refused_bad_args():
    """net-protocol.md v0.13 REPLACED `commit` with a re-push: the v0.11 form
    (`rm` only) is refused with the contract NAME `bad args`, and nothing is
    written. The v0.13 A/B semantics (inactive slot, then the header flips)
    are pinned in tests/test_usd.py."""
    with FakeShell.ephemeral(boot_active_slot="A", usd_card="da") as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            resp = shell.commit("nanosoc")
    assert resp.ok is False and resp.err == "bad args" and resp.slot == ""
    assert fake.commits == []


def test_unknown_op_rejected():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            resp = shell._request({"op": "self_destruct"})
    assert resp["ok"] is False
    assert "unknown op" in resp["err"]


def test_malformed_json_line_gets_ok_false_not_a_crash():
    with FakeShell.ephemeral() as fake:
        with socket.create_connection((fake.host, fake.control_port), timeout=2.0) as sock:
            sock.sendall(b"this is not json\n")
            reader = sock.makefile("rb")
            resp = json.loads(reader.readline())
            assert resp["ok"] is False
            # ...and the connection must still work for the next request.
            sock.sendall(b'{"op":"ping"}\n')
            resp = json.loads(reader.readline())
            assert resp["ok"] is True


# --------------------------------------------------------------------------- #
# macgen: the MAC gen/checker verb + counter model (net-protocol.md "MAC
# gen/checker control", I10 tail)
# --------------------------------------------------------------------------- #

def test_macgen_clean_traffic_advances_counters_without_errors():
    with FakeShell.ephemeral(macgen_frames_per_call=8) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            first = shell.macgen(gen=True, chk=True, inject="none")
            second = shell.macgen(gen=True, chk=True, inject="none")
    assert first.ok and second.ok
    assert (first.tx, first.rx, first.err) == (8, 8, 0)
    assert (second.tx, second.rx, second.err) == (16, 16, 0)  # advanced, err flat
    assert fake.genchk_gen_en and fake.genchk_chk_en


def test_macgen_armed_inject_increments_err_count():
    with FakeShell.ephemeral(macgen_frames_per_call=8) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            clean = shell.macgen(gen=True, chk=True, inject="none")
            faulted = shell.macgen(gen=True, chk=True, inject="bad_fcs")
    assert clean.err == 0
    assert faulted.err == 1                 # the injected frame failed the checker
    assert faulted.tx > clean.tx            # traffic still advanced
    assert fake.genchk_inject == "bad_fcs"


def test_macgen_gen_disabled_does_not_advance_tx():
    with FakeShell.ephemeral(macgen_frames_per_call=8) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            # chk on but gen off: no frames generated, nothing checked, no error
            resp = shell.macgen(gen=False, chk=True, inject="bad_fcs")
    assert resp.ok
    assert (resp.tx, resp.rx, resp.err) == (0, 0, 0)
    assert fake.genchk_gen_en is False and fake.genchk_chk_en is True


def test_macgen_unknown_inject_rejected_fail_closed():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            resp = shell.macgen(gen=True, chk=True, inject="smash")  # wire-transparent
    assert resp.ok is False
    assert (resp.tx, resp.rx, resp.err) == (0, 0, 0)
    # a rejected macgen must not have advanced the model's counters
    assert (fake.genchk_tx, fake.genchk_rx, fake.genchk_err) == (0, 0, 0)


def test_macgen_missing_args_are_bad_args():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            r1 = shell._request({"op": "macgen", "chk": True, "inject": "none"})
            r2 = shell._request({"op": "macgen", "gen": True, "chk": True})
    assert r1["ok"] is False and r1["err"] == "bad args"
    assert r2["ok"] is False and r2["err"] == "bad args"


def test_macgen_counters_clear_on_enable_rising_edge():
    """shell-regmap.md v0.4 / net-protocol.md v0.4 "Counter semantics": the
    GENCHK RTL clears all three counters on a gen_en/chk_en 0->1 rising edge,
    so they are monotonic *within* an enabled session but restart across an
    enable toggle. The fakeshell models that; this pins it (the previously
    silent divergence flagged by the audit — net-protocol.md used to call the
    counters 'monotonic')."""
    with FakeShell.ephemeral(macgen_frames_per_call=8) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            # Enabling gen+chk (0->1) clears from 0, then this call's frames land.
            first = shell.macgen(gen=True, chk=True, inject="none")
            second = shell.macgen(gen=True, chk=True, inject="none")
            assert (first.tx, second.tx) == (8, 16)   # monotonic within the session
            # Drop both enables: no rising edge, gen off so no frames generated.
            down = shell.macgen(gen=False, chk=False, inject="none")
            assert (down.tx, down.rx) == (16, 16)      # unchanged (no clear yet)
            # Re-enable: the 0->1 rising edge CLEARS the counters, so tx restarts
            # from zero and only this call's 8 frames are counted.
            restarted = shell.macgen(gen=True, chk=True, inject="none")
    assert restarted.tx == 8
    assert restarted.tx < second.tx              # NOT monotonic across the toggle
    assert fake.genchk_gen_en and fake.genchk_chk_en


# --------------------------------------------------------------------------- #
# Swap: happy paths over both push transports
# --------------------------------------------------------------------------- #

def test_swap_with_nothing_pushed_into_it_fails_on_the_await_timeout():
    """The shell PARKS on ``swap`` and waits for the pair to be pushed INTO it.
    Push nothing and the swap fails on the FSM's AWAIT idle timeout
    (swap_fsm_transitions.c: ``await_timeout`` is the one input that fails a
    ``SWAP_AWAIT_*`` state) — it does not hang, and it does not succeed.

    ``ShellClient.swap()`` is the blocking form: it sends and waits, leaving no
    window to push in, so it is only ever correct against a shell that already
    holds the pair. Here that is the point.
    """
    with FakeShell.ephemeral(swap_await_timeout=FAST_AWAIT) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            resp = shell.swap(rm="nanosoc", src="tftp")
    assert not resp.ok
    assert not resp.verified
    assert fake.swaps[-1]["final"] == "FAILED"
    # It got as far as inviting the clearing, and no further.
    assert fake.swaps[-1]["history"][-1] == "AWAIT_INCOMING_CLEARING"
    assert fake.push_events == []


def test_happy_path_tftp_swap_then_push_pair_verified():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell, rm="nanosoc", src="tftp") as swap:
                # The swap parks and the shell starts listening for the clearing
                # — pushing here is the ONLY order that works. (`swap_begin`
                # only SENDS, so wait for the gate rather than racing it; a real
                # client doesn't need to, because a push that arrives before the
                # shell has armed just queues — see FakeShell._admit_push.)
                assert fake.wait_awaiting(BitstreamKind.CLEARING)
                _push_pair(fake, transport="tftp", rm_id=1)
            resp = swap.response
            assert resp.ok
            assert resp.verified
            assert int(resp.rm_id, 0) == 1
            # ping must now report the new RM (net-protocol.md step 7).
            assert int(shell.ping().rm_id, 0) == 1
        kinds = [e.info.kind for e in fake.accepted_pushes]
        assert kinds == [BitstreamKind.CLEARING, BitstreamKind.PARTIAL]
        assert fake.swaps[-1]["final"] == "DONE"
        # The FSM really sat in both AWAIT states waiting for those pushes.
        history = fake.swaps[-1]["history"]
        assert "AWAIT_INCOMING_CLEARING" in history and "AWAIT_PARTIAL" in history
        # ...and the gate is shut again now the swap is done.
        assert fake.awaiting is None


def test_happy_path_raw_tcp_swap_then_push_pair_verified():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell, rm="nanosoc", src="tcp") as swap:
                _push_pair(fake, transport="tcp", rm_id=7)
            resp = swap.response
    assert resp.ok and resp.verified
    assert int(resp.rm_id, 0) == 7
    assert [e.transport for e in fake.accepted_pushes] == ["tcp", "tcp"]


def test_second_swap_uses_clearing_cached_from_first():
    """The server-visible half of I2 step 5: after swapping to RM1, the
    shell must hold RM1's clearing (cached from its pushed pair) so a
    second swap to RM2 can clear RM1 without the host re-supplying it."""
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell, rm="rm_one") as first:
                _push_pair(fake, transport="tftp", rm_id=1)
            assert first.response.verified
            with _parked_swap(shell, rm="rm_two") as second:
                _push_pair(fake, transport="tftp", rm_id=2)
            resp = second.response
            assert resp.ok and resp.verified
            assert int(resp.rm_id, 0) == 2
        assert fake._current_clearing.rm_id == 2  # cached again, for swap 3


def test_swap_rejects_unknown_rm_name_before_arming_the_transports():
    """An unknown rm name is rejected at decode, BEFORE the FSM starts — so the
    shell never arms, and a host that pushed anyway would be reset.

    (Premise changed 2026-07-14: this test used to push the pair first and then
    assert ``fake.pair_ready`` still held "for a correct retry". Under the real
    order there is nothing staged to survive a NAK, because a NAK'd swap never
    invited a push in the first place. The property that actually matters — a
    rejected swap leaves the shell clean for a correct retry — is asserted
    directly below instead.)
    """
    with FakeShell.ephemeral(known_rm_ids={"nanosoc": 1},
                             swap_await_timeout=FAST_AWAIT) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            assert not shell.swap(rm="mystery_rm").ok
            assert fake.awaiting is None       # never armed
            assert fake.swaps == []            # the FSM never even started
            assert fake.push_events == []

            # ...and the shell is clean: a correct swap still works.
            with _parked_swap(shell, rm="nanosoc") as swap:
                _push_pair(fake, transport="tftp", rm_id=1)
            assert swap.response.verified


def test_swap_verify_failure_reports_not_verified_and_keeps_old_rm():
    """I25 fault injection: the 'hardware' reads back the wrong RM_ID, so
    VERIFY fails; the shell reports the failure and still claims the old
    rm_id on ping (the RP is left decoupled, nothing new verified)."""
    with FakeShell.ephemeral() as fake:
        fake.rm_id_readback_override = 0x63  # wrong RM "landed"
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell, rm="nanosoc") as swap:
                _push_pair(fake, transport="tftp", rm_id=1)
            resp = swap.response
            assert not resp.ok
            assert not resp.verified
            assert int(shell.ping().rm_id, 0) == 0  # still greybox
        assert fake.swaps[-1]["final"] == "FAILED"
        assert "RELEASE" not in fake.swaps[-1]["history"]  # safe-failure mode


def test_swap_fails_closed_when_greybox_clearing_missing_without_inviting_a_push():
    """swap_fsm.c's I2 fail-closed guard, observed through the protocol: a shell
    image without its greybox clearing seed must fail the first swap at
    STREAM_CLEARING, not stall or proceed.

    Premise sharpened (2026-07-14): STREAM_CLEARING is reached *before*
    AWAIT_INCOMING_CLEARING, so this swap dies before the shell ever arms its
    push transports. The old version of this test pushed a pair first and then
    swapped — which the fake accepted, hiding the fact that on a real shell
    those bytes would have been reset on the floor. The correct statement is
    stronger: the host is never even invited to push.
    """
    with FakeShell.ephemeral(greybox_clearing_available=False,
                             swap_await_timeout=FAST_AWAIT) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            resp = shell.swap(rm="nanosoc")
    assert not resp.ok
    assert fake.swaps[-1]["history"][-1] == "STREAM_CLEARING"
    assert "AWAIT_INCOMING_CLEARING" not in fake.swaps[-1]["history"]
    assert fake.push_events == []       # nothing was ever invited


# --------------------------------------------------------------------------- #
# THE ORDERING GATE — regression guard for the 2026-07-14 silicon bug.
#
# pyverify's deploy used to do push_pair() -> swap(). The shell only listens for
# a bitstream once `swap` has parked and driven its FSM into SWAP_AWAIT_*, so a
# push that arrives first is RESET (ECONNRESET; the shell's `diag.got` never
# leaves 0) and the swap then fails "swap failed" with no partial staged. The
# bug survived because THIS FAKE accepted a push in any state — a test double
# more permissive than the firmware cannot catch a protocol-ordering bug.
#
# These tests fail against that permissive fake, which is the point.
# --------------------------------------------------------------------------- #

def test_push_before_swap_is_rejected_tftp():
    """TFTP: the shell answers a not-awaited push with an ERROR packet, so the
    client (here the reference ``tftp_put``; the production ``BitstreamPusher``
    raises ``PushError`` on the same packet) learns the push was refused."""
    with FakeShell.ephemeral(swap_await_timeout=FAST_AWAIT) as fake:
        clearing, _partial = _pair_frames()
        assert fake.awaiting is None, "no swap in flight => not awaiting anything"
        with pytest.raises(TftpError, match="ERR_NOT_AWAITING"):
            tftp_put(fake.host, fake.tftp_port, clearing)
    assert [e.status for e in fake.push_events] == ["ERR_NOT_AWAITING"]
    assert not fake.pair_ready, "a refused push must stage NOTHING"


def test_push_before_swap_is_rejected_raw_tcp():
    """Raw TCP: the shell RESETS the connection (the real ECONNRESET). The 6910
    service defines no accept/reject response (net-protocol.md), so the reset is
    the only signal — and the byte counters stay at zero, exactly like the
    board's ``diag.got``."""
    with FakeShell.ephemeral(swap_await_timeout=FAST_AWAIT) as fake:
        clearing, _partial = _pair_frames()
        raw_tcp_put(fake.host, fake.raw_tcp_port, clearing)
        assert fake.wait_push_events(1)
    assert [e.status for e in fake.push_events] == ["ERR_NOT_AWAITING"]
    assert not fake.pair_ready


def test_the_old_wrong_order_push_pair_then_swap_now_fails():
    """The exact sequence pyverify used to run, end to end: push the pair, then
    swap. Both pushes are refused, nothing is staged, and the swap then fails
    with no partial — which is precisely what the KU115 did on 2026-07-14.
    """
    with FakeShell.ephemeral(swap_await_timeout=FAST_AWAIT) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            clearing, partial = _pair_frames(rm_id=1)
            for frame in (clearing, partial):
                raw_tcp_put(fake.host, fake.raw_tcp_port, frame)
            assert fake.wait_push_events(2)
            resp = shell.swap(rm="nanosoc", src="tcp")   # the old order

    # Both pushes bounced: the clearing on the arming gate, the partial on I2
    # ordering (the clearing before it never landed, so it cannot be next).
    assert [e.status for e in fake.push_events] == ["ERR_NOT_AWAITING", "ERR_ORDER"]
    assert not fake.pair_ready
    # ...and the swap fails, having waited in vain for a pair that was never
    # accepted. On the board this is `{"ok":false,"err":"swap failed"}`.
    assert not resp.ok and not resp.verified
    assert fake.swaps[-1]["final"] == "FAILED"
    assert fake.current_rm_id == 0, "no RM may be adopted from a swap that got nothing"


def test_a_clearing_re_push_mid_swap_is_rejected_once_the_partial_is_due():
    """The gate tracks exactly ONE kind: once the clearing has landed the FSM is
    in SWAP_AWAIT_PARTIAL, and a second clearing pushed into that window is not
    what the shell is waiting for — it is reset, and it does NOT overwrite the
    clearing the swap already took (swap_fsm.c guards the staged slot against a
    mid-swap re-push)."""
    with FakeShell.ephemeral(swap_await_timeout=FAST_AWAIT) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell, rm="nanosoc", src="tcp") as swap:
                clearing, partial = _pair_frames(rm_id=1)
                raw_tcp_put(fake.host, fake.raw_tcp_port, clearing)
                assert fake.wait_push_events(1)
                assert fake.wait_awaiting(BitstreamKind.PARTIAL)

                # A second clearing now: not what is awaited -> reset.
                raw_tcp_put(fake.host, fake.raw_tcp_port, clearing)
                assert fake.wait_push_events(2)

                raw_tcp_put(fake.host, fake.raw_tcp_port, partial)
            resp = swap.response

    assert [e.status for e in fake.push_events] == ["OK", "ERR_NOT_AWAITING", "OK"]
    # The swap still completed on the first (correct) clearing + the partial.
    assert resp.ok and resp.verified and int(resp.rm_id, 0) == 1


# --------------------------------------------------------------------------- #
# Bitstream-receive rejection matrix (config_agent.c semantics)
#
# Two classes here, and the split is load-bearing (it is exactly where the C
# puts the arming gate — begin_payload() validates the HEADER, and only then
# calls sink->begin(), which is what fails closed when no swap is awaiting):
#
#   * HEADER-time rejections (magic/version/static_id/kind/I2 ordering) are
#     reported as themselves even for an unarmed push, so those tests need no
#     swap. That precedence is asserted, not assumed.
#   * PAYLOAD-time rejections (CRC, torn/short) can only be reached by a push
#     the shell actually admitted — so those must happen inside a parked swap.
# --------------------------------------------------------------------------- #

def test_tftp_rejects_wrong_static_id_with_tftp_error():
    """overlay-manifest.md: partials are only valid against their exact
    static — the fake shell must refuse at header time (before any
    payload/'ICAP' handling) and tell the TFTP client via ERROR.

    Header-time, so it outranks the arming gate: an unarmed push with a bad
    static_id is ERR_STATIC_ID, not ERR_NOT_AWAITING (config_agent.c validates
    the header in begin_payload() before it ever calls the sink)."""
    with FakeShell.ephemeral(static_id=STATIC_ID) as fake:
        clearing, _ = _pair_frames(static_id=0xDEADBEEF)
        with pytest.raises(TftpError, match="ERR_STATIC_ID"):
            tftp_put(fake.host, fake.tftp_port, clearing)
        assert [e.status for e in fake.rejected_pushes] == ["ERR_STATIC_ID"]
        assert not fake.pair_ready


def test_tftp_rejects_bad_crc_inside_a_parked_swap():
    """The CRC is checked when the payload completes — reachable only by a push
    the shell admitted, i.e. inside a parked swap. The corrupt clearing is
    refused, so the pair never completes and the swap dies on its await timeout:
    a torn transfer cannot smuggle an RM through."""
    with FakeShell.ephemeral(swap_await_timeout=FAST_AWAIT) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell) as swap:
                clearing, _ = _pair_frames()
                corrupted = bytearray(clearing)
                corrupted[-1] ^= 0xFF  # flip a payload byte; header CRC now stale
                with pytest.raises(TftpError, match="ERR_CRC"):
                    tftp_put(fake.host, fake.tftp_port, bytes(corrupted))
            assert not swap.response.ok
        assert [e.status for e in fake.rejected_pushes] == ["ERR_CRC"]
        assert not fake.pair_ready


def test_partial_before_clearing_rejected_i2_ordering():
    """config_agent.c's I2 rule: a partial is only accepted once its
    pair's clearing has been captured — partial-first must be refused.

    Header-time (``s_ordering_seen_clearing`` is checked in
    ``config_agent_validate_header_ex()``), so it fires even on an unarmed push
    and outranks the arming gate. The recovery leg then has to run in the real
    order — inside a parked swap — because that is the only way a clearing is
    admitted at all."""
    with FakeShell.ephemeral() as fake:
        clearing, partial = _pair_frames()
        with pytest.raises(TftpError, match="ERR_ORDER"):
            tftp_put(fake.host, fake.tftp_port, partial)
        # ...and the same over raw TCP:
        raw_tcp_put(fake.host, fake.raw_tcp_port, partial)
        assert fake.wait_push_events(2)
        assert [e.status for e in fake.rejected_pushes] == ["ERR_ORDER", "ERR_ORDER"]

        # correct order still recovers — clearing then partial, into a swap that
        # is awaiting them (the ONLY order the shell accepts a bitstream in).
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell) as swap:
                tftp_put(fake.host, fake.tftp_port, clearing)
                tftp_put(fake.host, fake.tftp_port, partial)
            assert swap.response.verified


def test_raw_tcp_rejects_torn_payload_inside_a_parked_swap():
    """Header promises len_words*4 bytes; the transport dies early — the
    config agent must reject (ERR_TRUNCATED), never stage a torn payload.
    Payload-time, so (like the CRC check) it is only reachable inside a parked
    swap."""
    with FakeShell.ephemeral(swap_await_timeout=FAST_AWAIT) as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            with _parked_swap(shell, src="tcp") as swap:
                clearing, _ = _pair_frames(clearing_payload=b"\xC1" * 256)
                raw_tcp_put(fake.host, fake.raw_tcp_port, clearing[:-100])  # tear 100 bytes off
                assert fake.wait_push_events(1)
            assert not swap.response.ok
        assert [e.status for e in fake.rejected_pushes] == ["ERR_TRUNCATED"]
        assert not fake.pair_ready


def test_raw_tcp_rejects_wrong_static_id_at_header_time():
    with FakeShell.ephemeral() as fake:
        clearing, _ = _pair_frames(static_id=0xBAD57A71)
        raw_tcp_put(fake.host, fake.raw_tcp_port, clearing)
        assert fake.wait_push_events(1)
        event = fake.rejected_pushes[0]
        assert event.status == "ERR_STATIC_ID"
        assert event.info is not None and event.info.static_id == 0xBAD57A71


def test_unknown_kind_and_bad_version_rejected():
    """Byte-level negative cases of the 24-byte header: kind not in
    {0,1} -> ERR_KIND; ver != MPS3_BITSTREAM_VER -> ERR_VERSION (checked
    in config_agent.c's order: version before kind)."""
    with FakeShell.ephemeral() as fake:
        clearing, _ = _pair_frames()
        bad_kind = bytearray(clearing)
        bad_kind[6] = 0x07  # kind byte (offset per ">4sHBBIIII")
        raw_tcp_put(fake.host, fake.raw_tcp_port, bytes(bad_kind))

        bad_ver = bytearray(clearing)
        bad_ver[4:6] = (MPS3_BITSTREAM_VER + 1).to_bytes(2, "big")
        raw_tcp_put(fake.host, fake.raw_tcp_port, bytes(bad_ver))

        assert fake.wait_push_events(2)
        assert [e.status for e in fake.rejected_pushes] == ["ERR_KIND", "ERR_VERSION"]
        assert not fake.pair_ready


# --------------------------------------------------------------------------- #
# Consoles (6930/6931/6932 shapes) via the production ConsoleReader
# --------------------------------------------------------------------------- #

def test_console_banner_seen_by_console_reader():
    with FakeShell.ephemeral() as fake:  # default uart0 banner
        with ConsoleReader(fake.host, fake.uart0_port, timeout=2.0) as uart0:
            uart0.assert_contains(b"nanosoc boot\n")


def test_console_banners_configurable_per_console():
    banners = {"uart0": b"boot monitor v0\n", "uart1": b"app says hi\n", "swo": b"\x01trace"}
    with FakeShell.ephemeral(banners=banners) as fake:
        for name, port in fake.console_ports.items():
            with ConsoleReader(fake.host, port, timeout=2.0) as console:
                console.assert_contains(banners[name])


def test_console_echoes_after_banner():
    with FakeShell.ephemeral(console_echo=True) as fake:
        with socket.create_connection((fake.host, fake.uart0_port), timeout=2.0) as sock:
            sock.settimeout(2.0)
            banner = b""
            while b"\n" not in banner:
                banner += sock.recv(4096)
            sock.sendall(b"hello")
            echoed = b""
            while len(echoed) < 5:
                chunk = sock.recv(5 - len(echoed))
                assert chunk, "console closed instead of echoing"
                echoed += chunk
            assert echoed == b"hello"


# --------------------------------------------------------------------------- #
# EdgeDeviceApi.status_fpga against the fake shell (rp truth from ping)
# --------------------------------------------------------------------------- #

#: nanosoc under the v2 rm_id encoding: design 0x0001 @ v1.0
#: (docs/VERSIONING_PLAN.md §3.2). Written out rather than derived because this
#: test only needs a realistically-SHAPED id, not the live one -- but the shape
#: matters: the design half is what the name lookup keys on.
NANOSOC_RM_ID_V1_0 = 0x01000001


def test_edge_status_fpga_rp_truth_tracks_the_fake_shell():
    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            # Registry keyed by the DESIGN half -- version-independent, so it
            # keeps naming nanosoc across version bumps (see test_edge.py's
            # test_status_fpga_rm_name_survives_a_design_version_bump).
            api = EdgeDeviceApi(shell=shell, known_rm_names={"0x0001": "nanosoc"})

            # Boot: greybox (rm_id 0) -> rp not loaded; shell level stays
            # honestly unknown (stubbed JTAG probe, out of fake-shell scope).
            status = api.status_fpga()
            assert status.rp.loaded is False
            assert status.shell.loaded is False

            # Swap + push to the real nanosoc id, then the rp level must flip
            # to loaded with the name resolved via known_rm_names.
            with _parked_swap(shell, rm="nanosoc", src="tftp") as swap:
                _push_pair(fake, transport="tftp", rm_id=NANOSOC_RM_ID_V1_0)
            assert swap.response.verified
            status = api.status_fpga()
            assert status.rp.loaded is True
            assert status.rp.rm_id == "0x01000001"
            assert status.rp.rm_name == "nanosoc"
            assert status.rp.derived_by == "shell-control"
