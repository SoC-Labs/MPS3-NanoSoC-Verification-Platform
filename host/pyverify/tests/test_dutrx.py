"""The DUT's Ethernet RETURN path over the wire: ``dutrx`` (net-protocol.md
v0.10), end to end through the real :class:`pyverify.client.ShellClient` and the
reference :class:`pyverify.testing.fakeshell.FakeShell` on a loopback socket.

Reception has been silicon-proven since 2026-07-30. The return half did not
exist: every frame the DUT transmitted arrived at the bridge's management
egress and was drained into a constant, so **a DUT could be talked to and could
not answer**. ``fpga/shell/ip/dut_egress/`` is the consumer that tie-off stood
in for, and this verb is how a host reads it.

What is pinned here, and why each one is a way the read could be wrong while
looking right:

* a frame longer than one 256-byte chunk reassembles **byte for byte** -- the
  failure mode of a chunked read is a frame that is plausible and wrong;
* ``off``/``more``/``last`` describe the run exactly, and a chunk that does not
  continue the previous one is a :class:`ShellProtocolError`, not a splice;
* the **drop counters ride every reply, including empty ones** -- the block
  cannot backpressure the bridge, so it drops, and nothing else on the wire
  says a capture was lossy;
* an empty FIFO is a RESULT (``None``, ``ok=True``), and a bitstream with no
  capture block is a DECLINE (``ok=False``) -- the two must never look alike.

The firmware side of the same contract is ``firmware/test/test_dutrx_dispatch.c``
(the handler against a behavioural DUTEGR model) and the two servers' replies
are compared byte-for-byte by
``tests/firmware_logic/test_fakeshell_conformance.py``.
"""
from __future__ import annotations

import json

import pytest

from pyverify import cli as cli_mod
from pyverify.client import DUTRX_CHUNK, ShellClient, ShellProtocolError
from pyverify.testing.fakeshell import FakeShell


@pytest.fixture
def shell():
    fake = FakeShell(
        control_port=0, tftp_port=0, raw_tcp_port=0,
        uart0_port=0, uart1_port=0, swo_port=0,
    )
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


def _client(fake: FakeShell) -> ShellClient:
    return ShellClient(fake.host, port=fake.control_port)


def test_empty_fifo_is_a_result_not_a_failure(shell) -> None:
    """An idle DUT. ``ok`` stays True and the frame is ``None``: there is
    nothing wrong with a capture that has captured nothing, and a caller must
    not have to tell that apart from an error."""
    with _client(shell) as c:
        frame, chunk = c.read_dut_frame()
    assert frame is None
    assert chunk.ok is True
    assert (chunk.n, chunk.frame_len, chunk.more, chunk.frames) == (0, 0, False, 0)
    assert chunk.data == b""


def test_short_frame_round_trips_in_one_chunk(shell) -> None:
    frame = bytes(range(60))
    shell.push_dut_frame(frame)
    with _client(shell) as c:
        got, chunk = c.read_dut_frame()
    assert got == frame
    assert (chunk.off, chunk.n, chunk.more, chunk.last) == (0, 60, False, True)
    assert chunk.frames == 0          # nothing left behind it
    assert chunk.rx == 1              # one frame captured whole


def test_long_frame_reassembles_byte_for_byte_across_chunks(shell) -> None:
    """The case the 256-byte chunk exists for. 1514 bytes is a full-MTU frame
    plus its FCS -- the largest thing a DUT will ordinarily send."""
    frame = bytes((i * 37 + (i >> 5)) & 0xFF for i in range(1514))
    shell.push_dut_frame(frame)

    chunks = []
    with _client(shell) as c:
        while True:
            chunk = c.dutrx()
            chunks.append(chunk)
            if not chunk.more:
                break

    assert b"".join(ch.data for ch in chunks) == frame
    assert len(chunks) == 6                       # 5 * 256 + 234
    assert [ch.n for ch in chunks] == [DUTRX_CHUNK] * 5 + [234]
    assert [ch.off for ch in chunks] == [0, 256, 512, 768, 1024, 1280]
    assert all(ch.frame_len == 1514 for ch in chunks)
    # `last` is the block's SECOND end-of-frame record and must agree with the
    # length exactly once: on the final chunk.
    assert [ch.last for ch in chunks] == [False] * 5 + [True]
    assert [ch.more for ch in chunks] == [True] * 5 + [False]


def test_read_dut_frame_follows_more_and_returns_the_whole_frame(shell) -> None:
    frame = bytes([0xA5]) * 300 + bytes([0x5A]) * 300
    shell.push_dut_frame(frame)
    with _client(shell) as c:
        got, last = c.read_dut_frame()
    assert got == frame
    assert last.more is False and last.last is True


def test_frames_come_out_in_order_and_do_not_smear(shell) -> None:
    """Two frames queued behind each other: the boundary is a real boundary.
    A chunked read that over-popped would take the second frame's first bytes
    onto the end of the first, and BOTH frames would still look plausible."""
    a = bytes(range(200))
    b = bytes(range(255, 55, -1))
    shell.push_dut_frame(a)
    shell.push_dut_frame(b)

    with _client(shell) as c:
        got_a, chunk_a = c.read_dut_frame()
        got_b, chunk_b = c.read_dut_frame()
        got_c, chunk_c = c.read_dut_frame()

    assert got_a == a and got_b == b and got_c is None
    assert chunk_a.frames == 1        # b still waiting, counted AFTER a's read
    assert chunk_b.frames == 0
    assert chunk_c.ok is True         # drained, not broken
    assert chunk_a.rx == chunk_b.rx == 2


def test_drop_counters_ride_every_reply_including_the_empty_one(shell) -> None:
    """The invariant the block was written around: it never backpressures the
    bridge, so it DROPS, and ``rx + drop_full + drop_giant`` is the tally
    against frames presented. A host that reads frames without reading these is
    counting only what survived -- so they are on the empty reply too, where a
    polling client would otherwise never learn it had lost anything."""
    shell.dutegr_drop_full = 3
    shell.dutegr_drop_giant = 1
    shell.dutegr_ovf = True
    shell.push_dut_frame(b"\x01\x02\x03\x04")

    with _client(shell) as c:
        _, with_frame = c.read_dut_frame()
        _, empty = c.read_dut_frame()

    for chunk in (with_frame, empty):
        assert (chunk.drop_full, chunk.drop_giant, chunk.drops) == (3, 1, 4)
        assert chunk.ovf is True
        assert chunk.rx == 1
    assert empty.n == 0               # ... and the empty one really was empty


def test_sticky_desync_is_reported_raw_not_interpreted(shell) -> None:
    shell.dutegr_desync = True
    shell.push_dut_frame(b"\xff" * 16)
    with _client(shell) as c:
        _, chunk = c.read_dut_frame()
    assert chunk.desync is True
    assert chunk.ok is True           # a latched DESYNC is evidence, not a failure


def test_a_bitstream_without_the_block_declines_and_does_not_look_empty(shell) -> None:
    """The fielded bitstream was minted before the capture block existed. The
    verb decodes and the handler declines -- it must NOT answer ok:true with
    zeroes, because "this fabric cannot capture" would then be the same line as
    "the DUT sent nothing", which is the reading-shaped lie v0.6 removed from
    `telemetry`."""
    shell.has_dut_egress = False
    with _client(shell) as c:
        frame, chunk = c.read_dut_frame()
    assert frame is None
    assert chunk.ok is False
    assert chunk.err == "dut_egress not present"


def test_a_chunk_that_does_not_continue_the_frame_is_refused(shell) -> None:
    """A spliced reassembly is the failure mode that looks fine. The client
    checks each chunk's ``off`` against what it has, and raises rather than
    joining two frames into one plausible frame."""
    shell.push_dut_frame(bytes(400))
    real_op = shell._op_dutrx
    calls = {"n": 0}

    def lying_op(request):
        # The FIRST chunk is honest; the SECOND claims to start at the beginning
        # of a different frame -- which is exactly what a mid-read RM swap, or a
        # second client draining the same FIFO, would look like on the wire.
        reply = real_op(request)
        calls["n"] += 1
        if calls["n"] > 1:
            reply["off"] = 0
        return reply

    shell._op_dutrx = lying_op            # type: ignore[method-assign]
    try:
        with _client(shell) as c:
            with pytest.raises(ShellProtocolError, match="does not continue"):
                c.read_dut_frame()
    finally:
        shell._op_dutrx = real_op         # type: ignore[method-assign]


def test_more_with_no_progress_raises_instead_of_spinning(shell) -> None:
    """``more`` with ``n == 0`` makes no progress. Without this check the read
    loop would spin against a stuck shell forever -- a hang, which is the one
    failure mode a bounded protocol should never produce."""
    shell.push_dut_frame(bytes(400))
    real_op = shell._op_dutrx
    calls = {"n": 0}

    def stuck_op(request):
        reply = real_op(request)
        calls["n"] += 1
        if calls["n"] > 1:
            reply.update({"n": 0, "data": "", "more": True})
        return reply

    shell._op_dutrx = stuck_op            # type: ignore[method-assign]
    try:
        with _client(shell) as c:
            with pytest.raises(ShellProtocolError, match="no progress"):
                c.read_dut_frame()
    finally:
        shell._op_dutrx = real_op         # type: ignore[method-assign]


def test_data_that_is_not_hex_is_refused(shell) -> None:
    shell.push_dut_frame(b"\x01\x02")
    with _client(shell) as c:
        real_op = shell._op_dutrx

        def garbled_op(request):
            reply = real_op(request)
            reply["data"] = "zzzz"
            return reply

        shell._op_dutrx = garbled_op      # type: ignore[method-assign]
        try:
            with pytest.raises(ShellProtocolError, match="not hex"):
                c.dutrx()
        finally:
            shell._op_dutrx = real_op     # type: ignore[method-assign]


def test_data_shorter_than_n_is_refused(shell) -> None:
    """``n`` and ``data`` are two statements of the same fact. When they
    disagree the chunk is torn, and a short frame that reassembles is worse
    than an error."""
    shell.push_dut_frame(b"\x01\x02\x03\x04")
    with _client(shell) as c:
        real_op = shell._op_dutrx

        def torn_op(request):
            reply = real_op(request)
            reply["data"] = reply["data"][:2]     # one byte's worth of hex
            return reply

        shell._op_dutrx = torn_op         # type: ignore[method-assign]
        try:
            with pytest.raises(ShellProtocolError, match="n=4 but carried 1"):
                c.dutrx()
        finally:
            shell._op_dutrx = real_op     # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# the CLI front door
# --------------------------------------------------------------------------- #


def test_cli_dut_eth_parses() -> None:
    args = cli_mod.build_parser().parse_args(
        ["dut-eth", "--host", "127.0.0.1", "--frames", "4"])
    assert args.verb == "dut-eth"
    assert args.frames == 4
    assert args.control_port == 6900


def test_cli_dut_eth_prints_frames_and_counters(shell, capsys) -> None:
    a = bytes(range(100))
    b = bytes(range(100, 200))
    shell.push_dut_frame(a)
    shell.push_dut_frame(b)
    shell.dutegr_drop_full = 2

    rc = cli_mod.main(["dut-eth", "--host", "127.0.0.1",
                       "--control-port", str(shell.control_port),
                       "--frames", "4"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["frames_read"] == 2
    assert [f["len"] for f in payload["frames"]] == [100, 100]
    assert bytes.fromhex(payload["frames"][0]["hex"]) == a
    assert bytes.fromhex(payload["frames"][1]["hex"]) == b
    # The drop counters are printed whether or not anything was dropped.
    assert payload["drop_full"] == 2 and payload["drop_giant"] == 0
    assert payload["waiting"] == 0 and payload["rx"] == 2


def test_cli_dut_eth_on_an_idle_fifo_is_exit_0(shell, capsys) -> None:
    """Zero frames is a RESULT. Exiting non-zero here would make "the DUT is
    quiet" indistinguishable from "the read failed" in every script."""
    rc = cli_mod.main(["dut-eth", "--host", "127.0.0.1",
                       "--control-port", str(shell.control_port)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["frames_read"] == 0 and payload["ok"] is True
    assert payload["frames"] == []


def test_cli_dut_eth_declines_with_exit_1_on_a_shell_without_the_block(
        shell, capsys) -> None:
    shell.has_dut_egress = False
    rc = cli_mod.main(["dut-eth", "--host", "127.0.0.1",
                       "--control-port", str(shell.control_port)])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["err"] == "dut_egress not present"
    assert "frames" not in payload        # no frame list on a decline


def test_cli_dut_eth_on_an_unreachable_shell_exits_3(capsys) -> None:
    import socket
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    rc = cli_mod.main(["dut-eth", "--host", "127.0.0.1", "--control-port", str(port)])
    err = capsys.readouterr().err
    assert rc == 3
    assert "cannot reach shell control channel" in err
    assert "Traceback" not in err
