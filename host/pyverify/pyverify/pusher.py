"""Bitstream pusher — ``docs/contracts/net-protocol.md`` "Bitstream framing".

Implements the wire header the config agent (A3) validates *before*
touching ICAP::

    magic "MPS3" | u16 ver | u8 kind(0=clearing,1=partial) | u8 rm_slot
    u32 static_id | u32 rm_id | u32 len_words | u32 crc32(payload)

Header pack/unpack and the CRC32 are real and unit-tested
(``host/pyverify/tests/test_push_framing.py``), and so is the network
send: a hand-rolled, zero-dependency **TFTP PUT client** (RFC 1350, octet
mode, 512-byte blocks, per-packet timeout + retransmit, ephemeral-TID
handling — mirroring the lwIP tftp server semantics the shell runs) and a
**raw-TCP** ``sendall`` alternative. Both are exercised against local
socket sinks in ``tests/test_pusher_send.py`` — nothing here needs a
board to be *correct*, only to be *exercised* against real hardware.

Transport (net-protocol.md port table):
    69   UDP  TFTP        partial/clearing bitstream push (D3 default)
    6910 TCP  raw partial push (alt to TFTP; D3)

Packaging note (supersedes the old "sibling directories" arrangement):
this module used to live at ``host/pusher/push.py``, deliberately outside
the pyverify package, with the relationship documented via the ``Pusher``
Protocol in :mod:`pyverify.swap`. That packaging question is now resolved
the other way — the pusher is part of pyverify proper (this module), so a
``pip install -e host/pyverify`` carries it everywhere pyverify goes.
``host/pusher/push.py`` remains as a thin re-export shim for the
path-based consumers that predate the move (``tests/integration/
test_swap_sequence.py`` et al.); new code should import
:mod:`pyverify.pusher`. The ``Pusher`` Protocol in ``swap.py`` survives as
a *test seam* (fake pushers), not as a package boundary.
"""
from __future__ import annotations

import errno
import socket
import struct
import time
import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Literal

__all__ = [
    "MAGIC",
    "HEADER_SIZE",
    "TFTP_PORT",
    "RAW_TCP_PORT",
    "TFTP_BLOCK_SIZE",
    "BitstreamFramingError",
    "PushError",
    "BitstreamKind",
    "BitstreamHeader",
    "frame_bitstream",
    "unframe_bitstream",
    "tftp_put",
    "tcp_send",
    "TCP_SEND_CHUNK",
    "tcp_send_windowed",
    "WINDOW_GRANT_BYTE",
    "DEFAULT_ACK_WINDOW",
    "PushResult",
    "BitstreamPusher",
    "PushChoice",
    "choose_push",
    "DEFAULT_PUSH_TIMEOUT_S",
    "LINUX_PUSH_TIMEOUT_S",
    "LINUX_TFTP_PARTIAL_RETRY_S",
    "TFTP_PARTIAL_RETRY_GAP_S",
]

MAGIC = b"MPS3"

# net-protocol.md port map.
TFTP_PORT = 69
RAW_TCP_PORT = 6910

# Windowed flow control on 6910 (config_agent built with MPS3_CFG_AGENT_WINDOWED)
# is now WINDOW-AS-GRANT: the shell paces us purely via its TCP receive window (it
# withholds the window update until it has drained a full window to the sink), so
# our OS send() blocks when the shell's window is closed. There is NO application
# grant byte to read. DEFAULT_ACK_WINDOW must equal the shell's compiled
# CFG_AGENT_ACK_WINDOW_BYTES == its BSP tcp_wnd (a matched pair — see
# firmware/platform/create_platform.tcl); it only sizes our per-iteration chunk.
DEFAULT_ACK_WINDOW = 16384
# Retired: the old 1-byte shell->host grant token. Kept as a named constant for
# back-compat with any importer; window-as-grant reads no such byte.
WINDOW_GRANT_BYTE = 0x06

# RFC 1350: fixed 512-byte data blocks; a block shorter than 512 (possibly
# zero-length) terminates the transfer.
TFTP_BLOCK_SIZE = 512

# ">" = big-endian, standard sizes, no implicit padding (all fields are
# already naturally aligned at their standard sizes, so struct emits no
# padding either way — being explicit here rather than relying on that).
#   4s  magic
#   H   ver          (u16)
#   B   kind         (u8)
#   B   rm_slot      (u8)
#   I   static_id    (u32)
#   I   rm_id        (u32)
#   I   len_words    (u32)
#   I   crc32        (u32)
_STRUCT = struct.Struct(">4sHBBIIII")
HEADER_SIZE = _STRUCT.size  # 24 bytes


class BitstreamFramingError(Exception):
    """Malformed header, wrong magic, unknown kind, or length/CRC mismatch
    on unframe. Mirrors the reject-before-ICAP check net-protocol.md
    assigns to the config agent, done host-side pre-flight."""


class PushError(Exception):
    """The network transfer itself failed (timeout, TFTP ERROR packet,
    connection refused/reset, ...). Distinct from
    :class:`BitstreamFramingError`, which is about the *payload* being
    wrong before any network is touched. Always chains the underlying
    ``OSError``/protocol detail where there is one."""


class BitstreamKind(IntEnum):
    CLEARING = 0
    PARTIAL = 1


@dataclass(frozen=True)
class BitstreamHeader:
    """The 24-byte framing header, net-protocol.md "Bitstream framing"."""

    ver: int
    kind: BitstreamKind
    rm_slot: int
    static_id: int
    rm_id: int
    len_words: int
    crc32: int

    def pack(self) -> bytes:
        return _STRUCT.pack(
            MAGIC,
            self.ver,
            int(self.kind),
            self.rm_slot,
            self.static_id,
            self.rm_id,
            self.len_words,
            self.crc32,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "BitstreamHeader":
        if len(data) < HEADER_SIZE:
            raise BitstreamFramingError(
                f"short header: got {len(data)} bytes, need {HEADER_SIZE}"
            )
        magic, ver, kind_byte, rm_slot, static_id, rm_id, len_words, crc = (
            _STRUCT.unpack(data[:HEADER_SIZE])
        )
        if magic != MAGIC:
            raise BitstreamFramingError(f"bad magic {magic!r}, expected {MAGIC!r}")
        try:
            kind = BitstreamKind(kind_byte)
        except ValueError as exc:
            raise BitstreamFramingError(
                f"unknown kind byte {kind_byte} (expected 0=clearing, 1=partial)"
            ) from exc
        return cls(
            ver=ver, kind=kind, rm_slot=rm_slot, static_id=static_id,
            rm_id=rm_id, len_words=len_words, crc32=crc,
        )


def frame_bitstream(
    payload: bytes,
    *,
    kind: BitstreamKind,
    rm_slot: int,
    static_id: int,
    rm_id: int,
    ver: int = 1,
) -> bytes:
    """Build a complete header+payload frame ready to push.

    ``len_words`` is derived from the payload (net-protocol.md gives the
    length in **words**, i.e. 32-bit ICAP words, not bytes — see the
    ambiguity note in host/README.md: the overlay-manifest.md manifest
    records file ``len`` in *bytes*, so callers pushing straight from an
    :class:`~pyverify.overlay.Overlay` must not confuse the two).
    """
    if len(payload) % 4 != 0:
        raise BitstreamFramingError(
            f"payload length {len(payload)} is not a multiple of 4 bytes "
            "(bitstream words are 32-bit; a non-multiple means a truncated "
            "or wrongly-converted .bin)"
        )
    header = BitstreamHeader(
        ver=ver,
        kind=kind,
        rm_slot=rm_slot,
        static_id=static_id,
        rm_id=rm_id,
        len_words=len(payload) // 4,
        crc32=zlib.crc32(payload) & 0xFFFFFFFF,
    )
    return header.pack() + payload


def unframe_bitstream(frame: bytes) -> tuple[BitstreamHeader, bytes]:
    """Inverse of :func:`frame_bitstream`, with the same validation the
    config agent is specified to do "before any ICAP write": length and
    CRC32 must match the header. Useful for host-side self-check before
    sending, and for tests.
    """
    header = BitstreamHeader.unpack(frame)
    payload = frame[HEADER_SIZE:]
    expected_len = header.len_words * 4
    if len(payload) != expected_len:
        raise BitstreamFramingError(
            f"payload length {len(payload)} != header len_words*4 "
            f"({expected_len})"
        )
    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if actual_crc != header.crc32:
        raise BitstreamFramingError(
            f"payload crc32 0x{actual_crc:08x} != header crc32 "
            f"0x{header.crc32:08x}"
        )
    return header, payload


# --------------------------------------------------------------------------- #
# TFTP PUT client (RFC 1350, octet mode) — hand-rolled, stdlib-only
# --------------------------------------------------------------------------- #
#
# Wire shapes (RFC 1350 §5):
#   WRQ   = | 02 | filename | 0 | "octet" | 0 |
#   DATA  = | 03 | block# | up to 512 bytes |
#   ACK   = | 04 | block# |
#   ERROR = | 05 | errcode | message | 0 |
#
# TID handling: the WRQ goes to (host, 69); the server answers ACK(0) from
# a fresh *ephemeral* source port (its transfer TID), and every subsequent
# DATA must be sent to that TID, not to 69. Packets arriving from any
# other address/port mid-transfer are ignored (RFC 1350 §4's "unknown TID"
# case — we drop rather than send the optional ERROR 5 courtesy packet).

_TFTP_OP_WRQ = 2
_TFTP_OP_DATA = 3
_TFTP_OP_ACK = 4
_TFTP_OP_ERROR = 5


def _parse_tftp_error(pkt: bytes) -> str:
    """Human-readable rendering of an ERROR packet (opcode already checked)."""
    if len(pkt) >= 4:
        (errcode,) = struct.unpack(">H", pkt[2:4])
        msg = pkt[4:].split(b"\0", 1)[0].decode("ascii", "replace")
        return f"TFTP ERROR {errcode}: {msg}"
    return "TFTP ERROR (malformed error packet)"


def _tftp_send_await_ack(
    sock: socket.socket,
    pkt: bytes,
    dest: tuple[str, int],
    *,
    expected_block: int,
    timeout_s: float,
    retries: int,
    locked_tid: "tuple[str, int] | None",
    what: str,
) -> tuple[str, int]:
    """Send ``pkt`` to ``dest``; await ACK(``expected_block``).

    Retransmits ``pkt`` up to ``retries`` extra times on timeout (plain
    RFC 1350 lock-step — we only ever retransmit on *timeout*, never on a
    duplicate ACK, so the Sorcerer's-Apprentice failure mode can't start
    from this side). Returns the address the ACK came from (the server's
    transfer TID). If ``locked_tid`` is set, ACKs from any other address
    are ignored; otherwise the first well-formed ACK fixes the TID (the
    post-WRQ case, where the server answers from an ephemeral port).
    """
    for _attempt in range(retries + 1):
        try:
            sock.sendto(pkt, dest)
        except OSError as exc:
            raise PushError(f"TFTP {what}: send to {dest[0]}:{dest[1]} failed: {exc}") from exc
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # retransmit pkt
            sock.settimeout(remaining)
            try:
                resp, addr = sock.recvfrom(4 + TFTP_BLOCK_SIZE)
            except socket.timeout:
                break  # retransmit pkt
            except OSError as exc:
                # e.g. ICMP port-unreachable surfacing as ECONNREFUSED.
                raise PushError(
                    f"TFTP {what}: no server at {dest[0]}:{dest[1]}: {exc}"
                ) from exc
            if locked_tid is not None and addr != locked_tid:
                continue  # unknown TID mid-transfer: ignore (RFC 1350 §4)
            if len(resp) < 4:
                continue
            (opcode,) = struct.unpack(">H", resp[:2])
            if opcode == _TFTP_OP_ERROR:
                raise PushError(f"TFTP {what}: server aborted: {_parse_tftp_error(resp)}")
            if opcode == _TFTP_OP_ACK:
                (block,) = struct.unpack(">H", resp[2:4])
                if block == expected_block:
                    return addr
                continue  # stale/duplicate ACK: keep waiting, don't resend
            # Anything else (unexpected opcode): ignore and keep waiting.
    raise PushError(
        f"TFTP {what}: no ACK({expected_block}) from {dest[0]}:{dest[1]} "
        f"after {retries + 1} attempt(s) ({timeout_s:.1f}s each)"
    )


def tftp_put(
    data: bytes,
    host: str,
    port: int = TFTP_PORT,
    *,
    filename: str = "bitstream.bin",
    timeout_s: float = 2.0,
    retries: int = 4,
) -> int:
    """RFC 1350 TFTP PUT (octet mode) of ``data`` to ``host:port``.

    Lock-step stop-and-wait: WRQ -> ACK(0), then DATA(n) -> ACK(n) in
    512-byte blocks, retransmitting the outstanding packet on timeout. A
    final short block (zero-length when ``len(data)`` is an exact multiple
    of 512) terminates the transfer. Block numbers wrap modulo 65536 for
    payloads past the 16-bit block counter (32 MiB+ partials).

    ``filename`` is advisory — the shell's config agent routes/validates
    on the 24-byte frame *header* (kind/static_id/...), not the name.

    Returns the number of bytes sent. Raises :class:`PushError` on
    timeout-exhaustion or a server ERROR packet.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        wrq = (
            struct.pack(">H", _TFTP_OP_WRQ)
            + filename.encode("ascii") + b"\0"
            + b"octet" + b"\0"
        )
        # WRQ -> ACK(0). The ACK's source addr is the server's transfer TID
        # (an ephemeral port, NOT :69) — everything after goes there.
        server_tid = _tftp_send_await_ack(
            sock, wrq, (host, port),
            expected_block=0, timeout_s=timeout_s, retries=retries,
            locked_tid=None, what=f"WRQ {filename!r}",
        )
        offset = 0
        block = 1
        while True:
            chunk = data[offset:offset + TFTP_BLOCK_SIZE]
            pkt = struct.pack(">HH", _TFTP_OP_DATA, block & 0xFFFF) + chunk
            _tftp_send_await_ack(
                sock, pkt, server_tid,
                expected_block=block & 0xFFFF, timeout_s=timeout_s,
                retries=retries, locked_tid=server_tid,
                what=f"DATA block {block}",
            )
            offset += len(chunk)
            if len(chunk) < TFTP_BLOCK_SIZE:
                return len(data)  # short (possibly empty) block terminates
            block += 1
    finally:
        sock.close()


#: :func:`tcp_send` hands the socket this much per ``sendall``, so its timeout is
#: a per-chunk INACTIVITY limit rather than a bound on the whole transfer.
TCP_SEND_CHUNK = 64 * 1024

#: errnos a half-close can hit after a COMPLETE send when the server has already
#: closed (and reset) the connection: "the server closed", not a failed push.
_CLOSED_BY_SERVER = frozenset({errno.ENOTCONN, errno.ECONNRESET})


def _half_close(sock: socket.socket) -> bool:
    """``shutdown(SHUT_WR)`` after the whole frame is sent. Returns False when the
    server had ALREADY closed (ENOTCONN / ECONNRESET): harnessd declining a push
    it will not take (no card, a bad kind) closes early, and the verdict of a push
    comes from the protocol (the swap reply, the slot job), never from the
    half-close. Any other error propagates."""
    try:
        sock.shutdown(socket.SHUT_WR)
        return True
    except OSError as exc:
        if exc.errno in _CLOSED_BY_SERVER:
            return False
        raise


def tcp_send(
    data: bytes,
    host: str,
    port: int = RAW_TCP_PORT,
    *,
    timeout_s: float = 5.0,
) -> int:
    """Raw-TCP push (net-protocol.md "raw partial push (alt to TFTP; D3)"):
    connect to ``host:port``, send the whole frame, half-close, then best-effort
    drain until the server closes (its "I consumed the frame" signal — the 6910
    service defines no response payload).

    ``timeout_s`` bounds the connect and EACH :data:`TCP_SEND_CHUNK` of the send
    -- an inactivity limit, never the whole transfer. (Since Python 3.5 a socket
    timeout bounds a whole ``sendall``: a 24 MB slot image pushed at <0.8 MB/s
    with 30 s was cut mid-image, and harnessd logged "push B FAILED: torn".)

    A server that closes after the frame is COMPLETELY sent (ENOTCONN /
    ECONNRESET at the half-close) is "the server closed", not an error: the
    result comes from the protocol. Returns the number of bytes sent. Raises
    :class:`PushError` on any other socket-level failure, including a send that
    could not complete (chaining the ``OSError``).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            view = memoryview(data)
            for off in range(0, len(data), TCP_SEND_CHUNK):
                sock.sendall(view[off:off + TCP_SEND_CHUNK])   # each bounded by timeout_s
            if _half_close(sock):
                try:
                    while sock.recv(4096):
                        pass  # discard anything the server says; EOF = done
                except OSError:
                    pass  # timeout/reset after a complete send: data is out
        return len(data)
    except OSError as exc:
        raise PushError(f"raw-TCP push to {host}:{port} failed: {exc}") from exc


def tcp_send_windowed(
    data: bytes,
    host: str,
    port: int = RAW_TCP_PORT,
    *,
    window: int = DEFAULT_ACK_WINDOW,
    timeout_s: float = 5.0,
) -> int:
    """Windowed raw-TCP push (net-protocol.md; config_agent built with
    ``MPS3_CFG_AGENT_WINDOWED``) — WINDOW-AS-GRANT. This is the HW-pivot primary
    fix for the fire-and-hose TCP-receive stall, and it needs NO application grant
    byte: the shell paces us purely via its TCP receive window. It consumes each
    received window WITHOUT reopening the window and reopens it (by one window)
    only after it has DRAINED that window to the sink, so our OS ``send()`` blocks
    whenever the shell's window is closed. We therefore just send the frame in
    ``window``-sized chunks and let TCP flow control lock-step us to the shell's
    real consume rate; between windows the shell's receive side goes quiescent, so
    there is no TCP-window/pbuf race.

    This replaces the earlier 1-byte 0x06 grant, which egressed unreliably on this
    board's LAN9220 while the shell was busy writing ICAP (a lone shell->host TCP
    segment stalled until an RTO — the ~98304-byte deadlock). Window-update ACKs
    egress reliably, so pacing by the window carries no such risk.

    ``window`` MUST equal the shell's compiled ``CFG_AGENT_ACK_WINDOW_BYTES`` ==
    its BSP TCP_WND (a matched pair). Chunking by ``window`` here is cosmetic —
    ``sendall`` blocks on the OS window regardless — but mirrors the shell's unit
    of pacing. Returns bytes sent; raises :class:`PushError` on any socket failure
    (a shell that rejects/closes early surfaces as a reset or a send timeout).
    """
    if len(data) < HEADER_SIZE:
        raise PushError(f"frame shorter than the {HEADER_SIZE}-byte header")
    if window <= 0:
        raise PushError("window must be positive")
    header, payload = data[:HEADER_SIZE], data[HEADER_SIZE:]
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            sock.settimeout(timeout_s)
            # Header + first window together; then one window per iteration. No
            # grant is read — each sendall() blocks in the OS TCP stack until the
            # shell reopens its receive window (one window per drained window).
            sock.sendall(header + payload[:window])
            off = window
            while off < len(payload):
                sock.sendall(payload[off : off + window])
                off += window
            if _half_close(sock):   # a server that already closed: see _half_close
                try:
                    while sock.recv(4096):
                        pass  # drain until the shell closes (validated + staged)
                except OSError:
                    pass
        return len(data)
    except OSError as exc:
        raise PushError(
            f"windowed raw-TCP push to {host}:{port} failed: {exc}"
        ) from exc


@dataclass(frozen=True)
class PushResult:
    ok: bool
    kind: BitstreamKind
    bytes_sent: int
    message: str = ""


TransportName = Literal["tftp", "tcp"]


@dataclass
class BitstreamPusher:
    """Sends framed bitstreams to the shell's config agent.

    Everything is real: header/CRC framing (:func:`frame_bitstream`), file
    reads, and the network send (:func:`tftp_put` / :func:`tcp_send`,
    selected by ``transport``). :meth:`_send` remains the injectable seam
    tests override to capture frames without a socket — see
    ``tests/test_push_framing.py``.
    """

    host: str
    transport: TransportName = "tftp"
    tftp_port: int = TFTP_PORT
    tcp_port: int = RAW_TCP_PORT
    #: Per-packet (TFTP) / connect+send (TCP) timeout.
    timeout_s: float = 2.0
    #: Extra retransmit attempts per TFTP packet after the first send.
    retries: int = 4
    #: transport=="tcp" only: use the windowed lock-step handshake
    #: (:func:`tcp_send_windowed`) instead of fire-and-hose. REQUIRES a shell
    #: built with MPS3_CFG_AGENT_WINDOWED (matched pair — see that function).
    windowed: bool = False
    #: Windowed payload chunk / ack granularity; must equal the shell's
    #: compiled CFG_AGENT_ACK_WINDOW_BYTES.
    window: int = DEFAULT_ACK_WINDOW
    #: transport=="tftp" only: for how long to RE-PUSH a PARTIAL the shell
    #: refused at its header (``DATA block 1`` answered ``TFTP ERROR 0``) --
    #: the busy-ICAP race. After ``swap`` the shell first streams the OUTGOING
    #: RM's cached clearing into the ICAP (SWAP_STREAM_CLEARING); a partial whose
    #: header lands before that finishes cannot arm the ICAP-direct sink
    #: (swap_fsm.c ``icap_direct_begin`` needs SWAP_AWAIT_PARTIAL), and TFTP has
    #: no window to hold it on (config_agent.c's RECV_PENDING_ICAP_BEGIN park is
    #: TCP only), so the push is rejected while the swap itself carries on. The
    #: rejection touches nothing (the staged clearing and the FSM are intact), so
    #: re-pushing once the stream is done is exactly right. ``0`` (the default)
    #: never re-pushes -- bare metal is unchanged; :func:`choose_push` turns it
    #: on for the Linux harness (silicon B1 v4 2026-09-25: nanosoc -> dbg_demo
    #: over TFTP, "DATA block 1: server aborted: TFTP ERROR 0: rejected").
    partial_retry_s: float = 0.0
    #: The gap between those re-pushes.
    partial_retry_gap_s: float = 0.5
    #: How many re-pushes the last :meth:`push_pair` needed (observability).
    partial_retries: int = field(default=0, compare=False)

    def push_pair(
        self, overlay, *, rm_slot: int = 0,
    ) -> tuple[PushResult, PushResult]:
        """Push an overlay's clearing bitstream, then its partial.

        Order matches the "Swap sequence" step numbering in
        net-protocol.md (clearing before partial). ``overlay`` is a
        ``pyverify.overlay.Overlay`` — typed loosely so any object with
        ``.clearing_path()``/``.partial_path()``/``.manifest.{static_id,
        rm_id}`` works (handy for tests; the historical reason was the
        pre-move package boundary, see module docstring).
        """
        clearing = self.push_file(
            overlay.clearing_path(),
            kind=BitstreamKind.CLEARING,
            rm_slot=rm_slot,
            static_id=overlay.manifest.static_id,
            rm_id=overlay.manifest.rm_id,
        )
        partial = self._push_partial(
            overlay.partial_path(),
            rm_slot=rm_slot,
            static_id=overlay.manifest.static_id,
            rm_id=overlay.manifest.rm_id,
        )
        return clearing, partial

    def _push_partial(self, path: Path, *, rm_slot: int, static_id: int,
                      rm_id: int) -> PushResult:
        """The partial push, with the TFTP busy-ICAP re-push (see
        :attr:`partial_retry_s`). Only a refusal AT THE HEADER is retried
        (``DATA block 1`` + ``TFTP ERROR 0``): anything later is a real failure
        of a transfer the shell had admitted, and a timeout is not a refusal."""
        self.partial_retries = 0
        deadline = time.monotonic() + max(0.0, self.partial_retry_s)
        while True:
            try:
                res = self.push_file(path, kind=BitstreamKind.PARTIAL, rm_slot=rm_slot,
                                     static_id=static_id, rm_id=rm_id)
            except PushError as exc:
                if (self.transport != "tftp" or self.partial_retry_s <= 0
                        or not _is_header_refusal(exc) or time.monotonic() >= deadline):
                    if self.partial_retries:
                        raise PushError(
                            f"{exc} (after {self.partial_retries} re-push(es) over "
                            f"{self.partial_retry_s:.0f} s: the shell never took the "
                            "partial -- not the busy-ICAP race)") from exc
                    raise
                self.partial_retries += 1
                time.sleep(self.partial_retry_gap_s)
                continue
            if self.partial_retries:
                res = PushResult(ok=res.ok, kind=res.kind, bytes_sent=res.bytes_sent,
                                 message=f"{res.message} (after {self.partial_retries} "
                                         "re-push(es): the shell was still streaming the "
                                         "outgoing clearing into the ICAP)")
            return res

    def push_file(
        self,
        path: Path,
        *,
        kind: BitstreamKind,
        rm_slot: int,
        static_id: int,
        rm_id: int,
        ver: int = 1,
    ) -> PushResult:
        payload = Path(path).read_bytes()
        frame = frame_bitstream(
            payload, kind=kind, rm_slot=rm_slot, static_id=static_id, rm_id=rm_id, ver=ver,
        )
        return self._send(frame, kind=kind)

    def _send(self, frame: bytes, *, kind: BitstreamKind) -> PushResult:
        if self.transport == "tftp":
            sent = tftp_put(
                frame, self.host, self.tftp_port,
                filename=f"{kind.name.lower()}.bin",
                timeout_s=self.timeout_s, retries=self.retries,
            )
            return PushResult(
                ok=True, kind=kind, bytes_sent=sent,
                message=f"tftp put {sent} bytes -> {self.host}:{self.tftp_port}",
            )
        if self.transport == "tcp":
            if self.windowed:
                sent = tcp_send_windowed(
                    frame, self.host, self.tcp_port,
                    window=self.window, timeout_s=self.timeout_s,
                )
                return PushResult(
                    ok=True, kind=kind, bytes_sent=sent,
                    message=f"windowed raw-tcp sent {sent} bytes "
                            f"-> {self.host}:{self.tcp_port} (win {self.window})",
                )
            sent = tcp_send(
                frame, self.host, self.tcp_port, timeout_s=self.timeout_s,
            )
            return PushResult(
                ok=True, kind=kind, bytes_sent=sent,
                message=f"raw-tcp sent {sent} bytes -> {self.host}:{self.tcp_port}",
            )
        raise ValueError(f"unknown transport {self.transport!r}")


#: TFTP's refusal of a transfer AT ITS HEADER: the frame header is the first 24
#: bytes, so it is always in DATA block 1, and config_agent.c answers a header it
#: will not take with ``ERROR 0 "rejected"`` (tftp_put's PushError text).
_HEADER_REFUSAL = "DATA block 1: server aborted: TFTP ERROR 0"


def _is_header_refusal(exc: BaseException) -> bool:
    return _HEADER_REFUSAL in str(exc)


# --------------------------------------------------------------------------- #
# The push transport, from what the RUNNING shell says it is (one rule for
# `pyverify deploy` and pyverify.board.Mps3Board)
# --------------------------------------------------------------------------- #

#: :attr:`BitstreamPusher.timeout_s`'s default: per TFTP packet / per TCP chunk.
DEFAULT_PUSH_TIMEOUT_S = 2.0
#: The Linux harness's 6910 inactivity limit. A partial that arrives while the
#: OUTGOING clearing is still streaming into the ICAP is PARKED by config_agent
#: (RECV_PENDING_ICAP_BEGIN: the header is read, the TCP window is left closed),
#: so the host's send legitimately blocks for that whole stream -- 167,308 B for
#: nanosoc, seconds on the MicroBlaze V. 30 s = MPS3_SWAP_AWAIT_IDLE_MS, the
#: shell's own silence bound: a stall longer than that is a dead swap anyway.
LINUX_PUSH_TIMEOUT_S = 30.0
#: The Linux harness's TFTP re-push budget (:attr:`BitstreamPusher.partial_retry_s`)
#: when a caller forces TFTP there. Generous: the outgoing clearing stream has no
#: shell-side bound, and the cost of a genuinely refused partial is only a later
#: failure (host-side validation already caught the static_id/framing ones).
LINUX_TFTP_PARTIAL_RETRY_S = 60.0
#: The gap between those re-pushes: well inside the shell's 30 s AWAIT idle bound.
TFTP_PARTIAL_RETRY_GAP_S = 0.5

_IMPL_LINUX = "linux"


@dataclass(frozen=True)
class PushChoice:
    """How to push to one shell: what :func:`choose_push` decided, and why."""

    transport: str
    src: str
    windowed: bool
    why: str
    #: :attr:`BitstreamPusher.timeout_s`
    timeout_s: float = DEFAULT_PUSH_TIMEOUT_S
    #: :attr:`BitstreamPusher.partial_retry_s`
    partial_retry_s: float = 0.0
    #: ``version.impl`` when the shell was asked (``None``: not asked / no answer)
    impl: "str | None" = None
    #: windowing was asked for (or auto-picked) but the transport is tftp, so it
    #: was dropped -- the CLI says so on its own line
    windowed_dropped: bool = False

    def as_tuple(self) -> "tuple[str, str, bool]":
        return self.transport, self.src, self.windowed

    def pusher(self, host: str, *, tftp_port: int = TFTP_PORT,
               tcp_port: int = RAW_TCP_PORT, window: int = DEFAULT_ACK_WINDOW,
               timeout_s: "float | None" = None) -> "BitstreamPusher":
        return BitstreamPusher(
            host=host, transport=self.transport,  # type: ignore[arg-type]
            tftp_port=tftp_port, tcp_port=tcp_port, windowed=self.windowed, window=window,
            timeout_s=self.timeout_s if timeout_s is None else timeout_s,
            partial_retry_s=self.partial_retry_s,
        )


def choose_push(
    client,
    *,
    transport: "str | None" = None,
    src: "str | None" = None,
    windowed: "bool | None" = None,
) -> PushChoice:
    """The push transport for the RUNNING shell behind ``client`` (anything with
    ``version()``). Each explicit argument overrides its own part.

    The auto rule, first match wins:

    1. ``windowed`` in ``version.features`` -- a bare-metal PRODUCT shell built
       ``HWICAP_FIFO=1 WINDOWED=1``: **tcp + tcp + windowed** (a plain push
       deadlocks it; every silicon sweep used this triple). Unchanged.
    2. ``version.impl == "linux"`` -- ``mps3-harnessd`` on the MicroBlaze V:
       **tcp + tcp + plain**, 30 s inactivity limit. The swap FSM streams the
       OUTGOING RM's clearing into the ICAP before it can take the partial, and
       for a DAP RM (nanosoc: 167,308 B) that stream outlasts the incoming
       clearing's push, so the partial arrives early. On 6910 config_agent PARKS
       it (header read, window closed, armed when the FSM reaches
       SWAP_AWAIT_PARTIAL); on TFTP it REJECTS it -- silicon B1 v4 2026-09-25,
       nanosoc -> dbg_demo, "TFTP ERROR 0: rejected". harnessd advertises no
       ``windowed`` (its kernel sockets pace themselves), so the push is plain.
    3. anything else (no ``windowed``, not Linux, or no ``version`` at all):
       the historical **tftp + tftp + plain**. Unchanged.

    A TFTP push to a Linux harness (only by an explicit ``transport="tftp"``)
    gets the busy-ICAP re-push (:data:`LINUX_TFTP_PARTIAL_RETRY_S`) instead; the
    shell is then asked for ``version`` even when every flag is explicit, since
    only it can say whether the guard is needed. Bare metal never gets it.
    """
    need_probe = transport is None or src is None or windowed is None
    impl: "str | None" = None
    features: "tuple[str, ...]" = ()
    probe_why = ""

    def probe() -> None:
        nonlocal impl, features, probe_why
        try:
            ver = client.version()
        except Exception as exc:  # an old shell with no `version` verb
            probe_why = f"version unavailable ({exc}); keeping the tftp default"
            return
        if not bool(getattr(ver, "ok", False)):
            probe_why = f"version refused ({getattr(ver, 'err', '') or 'not ok'}); keeping the tftp default"
            return
        features = tuple(getattr(ver, "features", ()) or ())
        impl = getattr(ver, "impl", None)

    if need_probe:
        probe()
    windowed_shell = "windowed" in features
    linux = impl == _IMPL_LINUX
    if windowed_shell:
        auto, why = ("tcp", "tcp", True), "version.features has 'windowed'"
    elif linux:
        auto, why = ("tcp", "tcp", False), (
            "version.impl = linux: 6910 parks a partial that arrives while the "
            "outgoing clearing still streams; TFTP would reject it")
    elif need_probe and not probe_why:
        auto, why = ("tftp", "tftp", False), f"version.features = {list(features)} (no 'windowed')"
    else:
        auto, why = ("tftp", "tftp", False), probe_why or "explicit flags"
    if not need_probe:
        why = "explicit flags"
    t = transport if transport is not None else auto[0]
    s = src if src is not None else auto[1]
    w = windowed if windowed is not None else auto[2]
    note = ""
    dropped = bool(w and t != "tcp")
    if dropped:
        w = False
    timeout_s, retry_s = DEFAULT_PUSH_TIMEOUT_S, 0.0
    if t == "tftp" and not need_probe:
        probe()                   # explicit tftp: is this the Linux harness?
        linux = impl == _IMPL_LINUX
    if linux:
        if t == "tcp":
            timeout_s = LINUX_PUSH_TIMEOUT_S
            note += f"; push inactivity limit {timeout_s:.0f} s (a parked partial blocks)"
        else:
            retry_s = LINUX_TFTP_PARTIAL_RETRY_S
            note += (f"; impl=linux over tftp: a partial refused while the outgoing "
                     f"clearing streams is re-pushed for up to {retry_s:.0f} s")
    return PushChoice(transport=t, src=s, windowed=w, why=why + note,
                      timeout_s=timeout_s, partial_retry_s=retry_s, impl=impl,
                      windowed_dropped=dropped)
