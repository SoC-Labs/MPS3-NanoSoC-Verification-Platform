"""Real, no-hardware tests for the bitstream header framing in
``pyverify.pusher`` — the ONE implementation of the net-protocol.md
24-byte header + CRC32 in this repo. (It briefly had a path-based twin at
``host/pusher/push.py``; that re-export shim and the test asserting its
drop-in equivalence were retired once every consumer imported the package
directly.) The network send itself is covered separately in
``test_pusher_send.py`` against local socket sinks.
"""
from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from pyverify.pusher import (
    HEADER_SIZE,
    MAGIC,
    BitstreamFramingError,
    BitstreamHeader,
    BitstreamKind,
    BitstreamPusher,
    PushResult,
    frame_bitstream,
    unframe_bitstream,
)


def test_header_pack_unpack_round_trip() -> None:
    header = BitstreamHeader(
        ver=1, kind=BitstreamKind.PARTIAL, rm_slot=0,
        static_id=0xA1B2C3D4, rm_id=1, len_words=100, crc32=0xDEADBEEF,
    )
    packed = header.pack()
    assert len(packed) == HEADER_SIZE == 24
    assert packed[:4] == MAGIC
    assert BitstreamHeader.unpack(packed) == header


def test_unpack_rejects_bad_magic() -> None:
    header = BitstreamHeader(
        ver=1, kind=BitstreamKind.CLEARING, rm_slot=0,
        static_id=1, rm_id=1, len_words=4, crc32=0,
    )
    bad = b"XXXX" + header.pack()[4:]
    with pytest.raises(BitstreamFramingError, match="bad magic"):
        BitstreamHeader.unpack(bad)


def test_unpack_rejects_unknown_kind() -> None:
    header = BitstreamHeader(
        ver=1, kind=BitstreamKind.CLEARING, rm_slot=0,
        static_id=1, rm_id=1, len_words=4, crc32=0,
    )
    packed = bytearray(header.pack())
    packed[6] = 0xFF  # kind byte
    with pytest.raises(BitstreamFramingError, match="unknown kind"):
        BitstreamHeader.unpack(bytes(packed))


def test_frame_and_unframe_round_trip() -> None:
    payload = b"\x01\x02\x03\x04" * 50
    frame = frame_bitstream(
        payload, kind=BitstreamKind.PARTIAL, rm_slot=1, static_id=0xA1B2C3D4, rm_id=7,
    )
    header, recovered_payload = unframe_bitstream(frame)
    assert recovered_payload == payload
    assert header.kind == BitstreamKind.PARTIAL
    assert header.rm_slot == 1
    assert header.static_id == 0xA1B2C3D4
    assert header.rm_id == 7
    assert header.len_words == len(payload) // 4
    assert header.crc32 == zlib.crc32(payload) & 0xFFFFFFFF


def test_frame_bitstream_rejects_non_word_aligned_payload() -> None:
    with pytest.raises(BitstreamFramingError, match="not a multiple of 4"):
        frame_bitstream(
            b"\x01\x02\x03", kind=BitstreamKind.CLEARING, rm_slot=0,
            static_id=1, rm_id=1,
        )


def test_unframe_rejects_crc_mismatch() -> None:
    payload = b"\x00\x00\x00\x00" * 10
    frame = frame_bitstream(
        payload, kind=BitstreamKind.CLEARING, rm_slot=0, static_id=1, rm_id=1,
    )
    corrupted = bytearray(frame)
    corrupted[-1] ^= 0xFF  # flip last payload byte, leave header's crc32 stale
    with pytest.raises(BitstreamFramingError, match="crc32"):
        unframe_bitstream(bytes(corrupted))


def test_pusher_push_file_frames_before_the_send_seam(tmp_path: Path) -> None:
    """push_file() does real work (read + frame) and then hands a single
    complete, self-consistent frame to the ``_send`` seam. This test used
    to assert the seam raised ``NotImplementedError`` (the pre-W-PUSH
    stub); the send is now real (see ``test_pusher_send.py``), so here the
    seam is overridden to capture the frame instead — same intent: verify
    everything *up to* the network is correct without a socket."""
    payload = b"\xAA\xBB\xCC\xDD" * 8
    bin_path = tmp_path / "nanosoc.bin"
    bin_path.write_bytes(payload)
    pusher = BitstreamPusher(host="192.168.10.101", transport="tftp")

    captured: list[tuple[bytes, BitstreamKind]] = []

    def fake_send(frame: bytes, *, kind: BitstreamKind) -> PushResult:
        captured.append((frame, kind))
        return PushResult(ok=True, kind=kind, bytes_sent=len(frame))

    pusher._send = fake_send  # type: ignore[method-assign]  # the documented seam
    result = pusher.push_file(
        bin_path, kind=BitstreamKind.PARTIAL, rm_slot=0,
        static_id=0xA1B2C3D4, rm_id=1,
    )

    assert result.ok
    assert len(captured) == 1
    frame, kind = captured[0]
    assert kind == BitstreamKind.PARTIAL
    header, recovered = unframe_bitstream(frame)  # validates len_words + crc32
    assert recovered == payload
    assert header.static_id == 0xA1B2C3D4
    assert header.rm_id == 1


def test_pusher_rejects_unknown_transport(tmp_path: Path) -> None:
    bin_path = tmp_path / "x.bin"
    bin_path.write_bytes(b"\x00" * 4)
    pusher = BitstreamPusher(host="192.168.10.101", transport="carrier-pigeon")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown transport"):
        pusher.push_file(
            bin_path, kind=BitstreamKind.CLEARING, rm_slot=0, static_id=1, rm_id=1,
        )
