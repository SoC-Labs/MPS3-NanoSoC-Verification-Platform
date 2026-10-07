"""Network-send tests for ``pyverify.pusher`` — real sockets, no hardware.

The TFTP PUT client and the raw-TCP push are exercised against *minimal
in-test sinks* on ``127.0.0.1`` ephemeral ports (a threaded RFC 1350
octet-mode receiver and a plain accept-drain TCP listener). These sinks
are deliberately local to this file — they are test scaffolding for the
client's wire behaviour (TID switch, retransmit, ERROR handling), not a
reference shell implementation (that's ``pyverify.testing.fakeshell``'s
job, a separate workstream).
"""
from __future__ import annotations

import socket
import struct
import threading
from pathlib import Path

import pytest

from pyverify.pusher import (
    HEADER_SIZE,
    TFTP_BLOCK_SIZE,
    BitstreamKind,
    BitstreamPusher,
    PushError,
    frame_bitstream,
    tcp_send,
    tcp_send_windowed,
    tftp_put,
    unframe_bitstream,
)

_OP_WRQ = 2
_OP_DATA = 3
_OP_ACK = 4
_OP_ERROR = 5


class TftpSink(threading.Thread):
    """Minimal RFC 1350 octet-mode *write* server for one transfer.

    Faithful in the ways that matter to the client under test:

    - ACK(0) for the WRQ is sent from a **fresh ephemeral socket** (the
      transfer TID), like every real TFTP server — so the client's
      "reply-to-the-TID-not-port-69" handling is genuinely exercised.
    - DATA blocks are ACKed from that TID; duplicates (client
      retransmits) are re-ACKed idempotently (last write wins per block).
    - Knobs: ``error_on_wrq`` answers the WRQ with an ERROR packet;
      ``drop_acks_for_block`` swallows the first N ACKs for one block to
      force client-side timeout+retransmit; ``never_ack`` goes silent
      after binding (client must give up with PushError).
    """

    def __init__(
        self,
        *,
        error_on_wrq: "tuple[int, str] | None" = None,
        drop_acks_for_block: "tuple[int, int] | None" = None,  # (block, n_drops)
        never_ack: bool = False,
    ) -> None:
        super().__init__(daemon=True)
        self._wrq_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._wrq_sock.bind(("127.0.0.1", 0))
        self._wrq_sock.settimeout(5.0)
        self.port = self._wrq_sock.getsockname()[1]
        self.error_on_wrq = error_on_wrq
        self.drop_acks_for_block = drop_acks_for_block
        self.never_ack = never_ack
        self.filename: "str | None" = None
        self.mode: "str | None" = None
        self.blocks: "dict[int, bytes]" = {}
        self.failure: "Exception | None" = None

    @property
    def payload(self) -> bytes:
        return b"".join(self.blocks[k] for k in sorted(self.blocks))

    def run(self) -> None:  # noqa: PLR0912 - linear protocol script
        try:
            pkt, client = self._wrq_sock.recvfrom(2048)
            if self.never_ack:
                return
            (op,) = struct.unpack(">H", pkt[:2])
            assert op == _OP_WRQ, f"expected WRQ, got opcode {op}"
            fields = pkt[2:].split(b"\0")
            self.filename = fields[0].decode("ascii")
            self.mode = fields[1].decode("ascii").lower()

            # Real-server behaviour: the transfer continues from a NEW
            # ephemeral TID, never from :69.
            data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            data_sock.bind(("127.0.0.1", 0))
            data_sock.settimeout(5.0)
            try:
                if self.error_on_wrq is not None:
                    errcode, msg = self.error_on_wrq
                    data_sock.sendto(
                        struct.pack(">HH", _OP_ERROR, errcode)
                        + msg.encode("ascii") + b"\0",
                        client,
                    )
                    return
                drops_left = 0
                drop_block = -1
                if self.drop_acks_for_block is not None:
                    drop_block, drops_left = self.drop_acks_for_block
                data_sock.sendto(struct.pack(">HH", _OP_ACK, 0), client)
                while True:
                    pkt, addr = data_sock.recvfrom(4 + TFTP_BLOCK_SIZE)
                    (op, block) = struct.unpack(">HH", pkt[:4])
                    assert op == _OP_DATA, f"expected DATA, got opcode {op}"
                    chunk = pkt[4:]
                    self.blocks[block] = chunk
                    if block == drop_block and drops_left > 0:
                        drops_left -= 1
                        continue  # swallow the ACK -> client must retransmit
                    data_sock.sendto(struct.pack(">HH", _OP_ACK, block), addr)
                    if len(chunk) < TFTP_BLOCK_SIZE:
                        return  # short block terminates (RFC 1350 §2)
            finally:
                data_sock.close()
        except Exception as exc:  # surfaced to the test via .failure
            self.failure = exc
        finally:
            self._wrq_sock.close()


class TcpSink(threading.Thread):
    """Accept one connection, drain it to EOF, close. That close is the
    'frame consumed' signal ``tcp_send`` waits (best-effort) for."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(5.0)
        self.port = self._listener.getsockname()[1]
        self.received = b""
        self.failure: "Exception | None" = None

    def run(self) -> None:
        try:
            conn, _addr = self._listener.accept()
            with conn:
                conn.settimeout(5.0)
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    self.received += chunk
        except Exception as exc:
            self.failure = exc
        finally:
            self._listener.close()


def _finish(sink: threading.Thread) -> None:
    sink.join(timeout=10.0)
    assert not sink.is_alive(), "sink thread did not finish"
    assert sink.failure is None, f"sink-side failure: {sink.failure!r}"


# --------------------------------------------------------------------------- #
# TFTP client
# --------------------------------------------------------------------------- #


def test_tftp_put_single_short_block() -> None:
    sink = TftpSink()
    sink.start()
    data = b"hello tftp world"  # < 512: exactly one (final) DATA block
    sent = tftp_put(data, "127.0.0.1", sink.port, filename="clearing.bin")
    _finish(sink)
    assert sent == len(data)
    assert sink.payload == data
    assert sink.filename == "clearing.bin"
    assert sink.mode == "octet"


def test_tftp_put_multi_block_reassembles() -> None:
    sink = TftpSink()
    sink.start()
    data = bytes(range(256)) * 5  # 1280 bytes -> blocks of 512/512/256
    sent = tftp_put(data, "127.0.0.1", sink.port)
    _finish(sink)
    assert sent == len(data)
    assert sink.payload == data
    assert sorted(sink.blocks) == [1, 2, 3]
    assert len(sink.blocks[3]) == 256


def test_tftp_put_exact_multiple_of_512_sends_empty_final_block() -> None:
    """RFC 1350: a transfer whose size is an exact multiple of the block
    size must be terminated by a zero-length DATA block — otherwise the
    server waits forever for more data."""
    sink = TftpSink()
    sink.start()
    data = b"\xAB" * (TFTP_BLOCK_SIZE * 2)
    tftp_put(data, "127.0.0.1", sink.port)
    _finish(sink)
    assert sink.payload == data
    assert sorted(sink.blocks) == [1, 2, 3]
    assert sink.blocks[3] == b""


def test_tftp_put_retransmits_data_on_lost_ack() -> None:
    """Sink swallows the first ACK for block 1: the client must time out
    and retransmit that DATA block (lock-step retransmit-on-timeout), and
    the transfer must still complete with the payload intact."""
    sink = TftpSink(drop_acks_for_block=(1, 1))
    sink.start()
    data = b"\x11\x22\x33\x44" * 200  # 800 bytes -> 2 blocks
    tftp_put(data, "127.0.0.1", sink.port, timeout_s=0.2, retries=4)
    _finish(sink)
    assert sink.payload == data


def test_tftp_put_raises_pusherror_on_server_error_packet() -> None:
    sink = TftpSink(error_on_wrq=(2, "access violation"))
    sink.start()
    with pytest.raises(PushError, match="TFTP ERROR 2: access violation"):
        tftp_put(b"data", "127.0.0.1", sink.port, timeout_s=0.5, retries=0)
    _finish(sink)


def test_tftp_put_times_out_cleanly_against_a_silent_server() -> None:
    sink = TftpSink(never_ack=True)
    sink.start()
    with pytest.raises(PushError, match=r"no ACK\(0\)"):
        tftp_put(b"data", "127.0.0.1", sink.port, timeout_s=0.05, retries=1)
    _finish(sink)


# --------------------------------------------------------------------------- #
# Raw-TCP client
# --------------------------------------------------------------------------- #


def test_tcp_send_delivers_all_bytes() -> None:
    sink = TcpSink()
    sink.start()
    data = b"\xDE\xAD\xBE\xEF" * 1000
    sent = tcp_send(data, "127.0.0.1", sink.port)
    _finish(sink)
    assert sent == len(data)
    assert sink.received == data


def test_tcp_send_connection_refused_raises_pusherror() -> None:
    # Grab a port the OS just released — nothing is listening on it.
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(PushError, match="raw-TCP push"):
        tcp_send(b"data", "127.0.0.1", port, timeout_s=0.5)


# --------------------------------------------------------------------------- #
# Windowed raw-TCP client (MPS3_CFG_AGENT_WINDOWED lock-step handshake)
# --------------------------------------------------------------------------- #


class WindowedSink(threading.Thread):
    """Emulates a WINDOW-AS-GRANT config_agent on 6910: read the 24-byte header,
    then drain all payload. The real shell paces the client via its TCP receive
    window (it withholds the window update until it has drained a window to the
    sink); this sink just consumes, which is enough to prove the client sends the
    whole frame with NO application grant byte.

    ``reject_after_header`` closes right after the header WITHOUT draining, to
    exercise the client's rejection path (the client's continued send then blocks
    on the closed window and surfaces a reset / send-timeout)."""

    def __init__(self, *, reject_after_header: bool = False) -> None:
        super().__init__(daemon=True)
        self._listener = socket.create_server(("127.0.0.1", 0))
        if reject_after_header:
            # Clamp the receive window so the client's sendall is GUARANTEED to
            # block against a never-reopened window, independent of kernel
            # buffer autotuning. Without this the test raced: Linux autotunes
            # the sender buffer up to tcp_wmem[max] (4 MiB on this host), which
            # can swallow the whole test payload, so sendall() returns before
            # the peer's close is ever observed and the rejection path is never
            # exercised ("DID NOT RAISE"). SO_RCVBUF on the listener is
            # inherited by the accepted conn and disables receive autotuning.
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self._listener.settimeout(5.0)
        self.port = self._listener.getsockname()[1]
        self.reject_after_header = reject_after_header
        self.received = b""
        self.failure: "Exception | None" = None

    def run(self) -> None:
        try:
            conn, _addr = self._listener.accept()
            with conn:
                conn.settimeout(5.0)
                buf = b""
                while len(buf) < HEADER_SIZE:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise RuntimeError("EOF before header")
                    buf += chunk
                if self.reject_after_header:
                    self.received = buf
                    return  # close without draining -> client push fails
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break  # client half-closed: whole frame received
                    buf += chunk
                self.received = buf
        except Exception as exc:  # noqa: BLE001 - reported via .failure
            self.failure = exc
        finally:
            self._listener.close()


def test_tcp_send_windowed_multi_window() -> None:
    win = 1024
    sink = WindowedSink()
    sink.start()
    frame = frame_bitstream(
        b"\x5A" * 3000, kind=BitstreamKind.CLEARING, rm_slot=0,
        static_id=0xA1B2C3D4, rm_id=0x11,
    )
    sent = tcp_send_windowed(frame, "127.0.0.1", sink.port, window=win)
    _finish(sink)
    assert sent == len(frame)
    assert sink.received == frame  # whole frame arrived, paced only by TCP window


def test_tcp_send_windowed_short_single_window() -> None:
    win = 1024
    sink = WindowedSink()
    sink.start()
    frame = frame_bitstream(
        b"\x77" * 400, kind=BitstreamKind.CLEARING, rm_slot=0,
        static_id=0xA1B2C3D4, rm_id=0x22,
    )
    tcp_send_windowed(frame, "127.0.0.1", sink.port, window=win)
    _finish(sink)
    assert sink.received == frame  # fits one window; still fully delivered


def test_tcp_send_windowed_raises_on_early_close(monkeypatch) -> None:
    win = 1024
    sink = WindowedSink(reject_after_header=True)
    sink.start()

    # Clamp the CLIENT's send buffer. sendall() returns as soon as the kernel has
    # copied the payload into the sender's socket buffer — it does not wait for
    # the peer. Linux autotunes that buffer up to tcp_wmem[max] (4 MiB on this
    # host), which silently swallowed the whole 400 KB payload, so sendall()
    # completed before the peer's close was ever observed and the test failed
    # with "DID NOT RAISE". An explicit SO_SNDBUF disables sender autotuning, so
    # the send genuinely blocks against the never-reopened window and the sink's
    # close surfaces as a reset / send-timeout — which is what this test claims
    # to exercise. (Clamping only the receiver's window is NOT sufficient.)
    _real_connect = socket.create_connection

    def _small_sndbuf_connect(*args, **kwargs):
        sock = _real_connect(*args, **kwargs)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        return sock

    monkeypatch.setattr(socket, "create_connection", _small_sndbuf_connect)

    frame = frame_bitstream(
        b"\x33" * 400_000, kind=BitstreamKind.CLEARING, rm_slot=0,
        static_id=0xA1B2C3D4, rm_id=0x33,
    )
    with pytest.raises(PushError):
        tcp_send_windowed(frame, "127.0.0.1", sink.port, window=win, timeout_s=1.0)
    _finish(sink)


def test_pusher_windowed_transport_end_to_end(tmp_path: Path) -> None:
    win = 1024
    payload = bytes(range(256)) * 12  # 3072 bytes -> 3 windows
    bin_path = tmp_path / "partial.bin"
    bin_path.write_bytes(payload)

    sink = WindowedSink()
    sink.start()
    pusher = BitstreamPusher(
        host="127.0.0.1", transport="tcp", tcp_port=sink.port,
        timeout_s=2.0, windowed=True, window=win,
    )
    result = pusher.push_file(
        bin_path, kind=BitstreamKind.PARTIAL, rm_slot=0,
        static_id=0xA1B2C3D4, rm_id=9,
    )
    _finish(sink)
    assert result.ok
    hdr, body = unframe_bitstream(sink.received)
    assert body == payload
    assert hdr.rm_id == 9


# --------------------------------------------------------------------------- #
# BitstreamPusher end-to-end against the sinks (frame -> wire -> unframe)
# --------------------------------------------------------------------------- #


def test_pusher_tftp_transport_end_to_end(tmp_path: Path) -> None:
    payload = b"\x0F\x1E\x2D\x3C" * 64
    bin_path = tmp_path / "nanosoc.bin"
    bin_path.write_bytes(payload)

    sink = TftpSink()
    sink.start()
    pusher = BitstreamPusher(
        host="127.0.0.1", transport="tftp", tftp_port=sink.port,
        timeout_s=1.0, retries=2,
    )
    result = pusher.push_file(
        bin_path, kind=BitstreamKind.PARTIAL, rm_slot=0,
        static_id=0xA1B2C3D4, rm_id=7,
    )
    _finish(sink)

    assert result.ok
    assert result.kind == BitstreamKind.PARTIAL
    assert result.bytes_sent == HEADER_SIZE + len(payload)
    assert sink.filename == "partial.bin"  # advisory name derives from kind
    header, recovered = unframe_bitstream(sink.payload)
    assert recovered == payload
    assert header.kind == BitstreamKind.PARTIAL
    assert header.static_id == 0xA1B2C3D4
    assert header.rm_id == 7


def test_pusher_tcp_transport_end_to_end(tmp_path: Path) -> None:
    payload = b"\x99\x88\x77\x66" * 32
    bin_path = tmp_path / "nanosoc_clear.bin"
    bin_path.write_bytes(payload)

    sink = TcpSink()
    sink.start()
    pusher = BitstreamPusher(
        host="127.0.0.1", transport="tcp", tcp_port=sink.port, timeout_s=2.0,
    )
    result = pusher.push_file(
        bin_path, kind=BitstreamKind.CLEARING, rm_slot=1,
        static_id=0xA1B2C3D4, rm_id=7,
    )
    _finish(sink)

    assert result.ok
    header, recovered = unframe_bitstream(sink.received)
    assert recovered == payload
    assert header.kind == BitstreamKind.CLEARING
    assert header.rm_slot == 1
