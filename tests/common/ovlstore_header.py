"""ovlstore_header.py — QSPI A/B-slot store header pack/unpack + the §8A.5
"interrupted commit can't brick the default" invariant check.

Mirrors `firmware/overlay_store/overlay_store.h`'s `ovlstore_header_t` /
`ovlstore_slot_desc_t` C structs byte-for-byte (that header is the
authoritative layout; keep this in lockstep with it, same as
`host/pusher/push.py` does for the bitstream framing header). No Python
model of this on-flash struct exists elsewhere in the repo as of this
writing (overlay_store.c/.h are firmware-only; `host/pyverify` covers
`manifest.json`, not the on-flash A/B header) — this is new, A5-owned
logic exercising ARCHITECTURE_SPEC.md §8A.5 at the protocol level.

Endianness: **little-endian** (OPEN_ISSUES I15, RESOLVED). overlay_store.h's
structs are read by direct pointer cast on the MicroBlaze itself (not sent
over the network), so the layout is whatever the MicroBlaze's native
endianness is — and a modern AXI MicroBlaze (Vivado 2024.1) is little-endian.
This mirrors W3's `firmware/overlay_store/ovlstore_codec.c` byte-for-byte, and
that equivalence is now *pinned* by a cross-language golden test —
`tests/firmware_logic/test_ovlstore_header_golden.py` packs an asymmetric
header through both this module and the real C codec (via
`firmware/test/ovlstore_pack.c`) and asserts the raw bytes are identical, so a
byte-swap on either side is caught immediately.
(Distinct from net-protocol.md's *wire* bitstream header, which push.py frames
big-endian = network byte order — a legitimately independent choice for an
on-wire vs on-flash struct; the earlier "both should match / MicroBlaze is
big-endian" note was wrong and is retired here.)
"""
from __future__ import annotations

import struct

MAGIC = b"OVLS"
VER = 1

_SLOT_FMT = "<IIIIIIIIB"  # LE (I15); static_id,rm_id,clear_off,clear_len,clear_crc,part_off,part_len,part_crc,valid
_SLOT_SIZE = struct.calcsize(_SLOT_FMT)
_HEADER_FMT = "<4sHBB"  # LE (I15); magic,ver,active_slot,flags (slots packed separately, see pack/unpack)
_HEADER_FIXED_SIZE = struct.calcsize(_HEADER_FMT)
HEADER_SIZE = _HEADER_FIXED_SIZE + 2 * _SLOT_SIZE


class BadOvlstoreHeader(ValueError):
    pass


def pack_slot(static_id, rm_id, clear_off, clear_len, clear_crc,
              part_off, part_len, part_crc, valid) -> bytes:
    return struct.pack(_SLOT_FMT, static_id, rm_id, clear_off, clear_len,
                        clear_crc, part_off, part_len, part_crc, int(bool(valid)))


def pack_header(active_slot: int, flags: int, slot_a: bytes, slot_b: bytes) -> bytes:
    if active_slot not in (0, 1):
        raise BadOvlstoreHeader(f"active_slot must be 0 or 1, got {active_slot}")
    if len(slot_a) != _SLOT_SIZE or len(slot_b) != _SLOT_SIZE:
        raise BadOvlstoreHeader("slot_a/slot_b must each be a packed slot descriptor")
    return struct.pack(_HEADER_FMT, MAGIC, VER, active_slot, flags) + slot_a + slot_b


def unpack_header(raw: bytes) -> dict:
    if len(raw) < HEADER_SIZE:
        raise BadOvlstoreHeader(f"short header: {len(raw)} < {HEADER_SIZE} bytes")
    magic, ver, active_slot, flags = struct.unpack(_HEADER_FMT, raw[:_HEADER_FIXED_SIZE])
    if magic != MAGIC:
        raise BadOvlstoreHeader(f"bad magic {magic!r}, expected {MAGIC!r}")
    slots = []
    off = _HEADER_FIXED_SIZE
    for _ in range(2):
        fields = struct.unpack(_SLOT_FMT, raw[off:off + _SLOT_SIZE])
        slots.append(dict(zip(
            ("static_id", "rm_id", "clear_off", "clear_len", "clear_crc",
             "part_off", "part_len", "part_crc", "valid"), fields,
        )))
        slots[-1]["valid"] = bool(slots[-1]["valid"])
        off += _SLOT_SIZE
    return dict(magic=magic, ver=ver, active_slot=active_slot, flags=flags, slots=slots)


def validate_active_slot(header: dict) -> list:
    """The §8A.5 safe-update invariant: 'An interrupted/failed write cannot
    brick the default — it falls back to the last-good slot.' That
    property reduces to one check on the *header*: whatever `active_slot`
    points at must be marked valid. `overlay_store_commit()` (overlay_
    store.c, TODO(A3)) is supposed to flip `active_slot` only *after*
    verifying the new slot's CRC — this function is the reader-side
    counterpart proving that invariant, independent of when/how the
    firmware got there.
    """
    errors = []
    if header.get("active_slot") not in (0, 1):
        errors.append(f"active_slot must be 0 or 1, got {header.get('active_slot')!r}")
        return errors
    slots = header.get("slots")
    if not slots or len(slots) != 2:
        errors.append("expected exactly 2 slot descriptors")
        return errors
    active = header["active_slot"]
    if not slots[active]["valid"]:
        errors.append(
            f"active_slot={active} is marked invalid — would brick the "
            "default on boot (overlay_store_boot_load_default() has "
            "nothing valid to stream)"
        )
    return errors
