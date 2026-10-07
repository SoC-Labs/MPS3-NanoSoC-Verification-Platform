/*
 * ovlstore_codec.h — QSPI A/B-slot store header pack/unpack (pure logic).
 *
 * Split out of overlay_store.h/.c specifically so this codec (the on-flash
 * byte layout + its (un)pack functions) has ZERO dependency on
 * platform_regs.h / SPI register access -- overlay_store.c (the impure
 * module that actually talks to the AXI Quad SPI core) includes this file
 * and calls into it; firmware/test/test_ovlstore_header.c links THIS file
 * alone to unit-test the on-flash format with plain host gcc.
 *
 * On-flash layout mirrors docs/contracts/overlay-manifest.md's "A/B slot
 * store" byte for byte:
 *   Header @ 0x000000:
 *     magic "OVLS" | u16 ver | u8 active_slot(0/1) | u8 flags
 *     per slot: { u32 static_id, u32 rm_id, u32 clear_off, u32 clear_len,
 *                 u32 clear_crc, u32 part_off, u32 part_len, u32 part_crc,
 *                 u8 valid }
 *
 * Endianness (I15, RESOLVED): docs/contracts/OPEN_ISSUES.md resolves I15 to
 * "little-endian" (a modern AXI MicroBlaze / Vivado 2024.1 is little-endian,
 * and this struct is read by direct pointer cast on the MicroBlaze, never
 * sent over the network) -- that is what ovlstore_pack_slot()/
 * ovlstore_header_pack() below implement (see put_u16le/put_u32le in the .c).
 *
 * tests/common/ovlstore_header.py (the independent Python model of this same
 * on-flash struct) now packs the SAME little-endian layout -- its struct
 * format strings are "<...". The two implementations are pinned byte-for-byte
 * by a cross-language golden test so they can never silently diverge again:
 *   - firmware/test/ovlstore_pack.c        (the C half -- packs a fixed,
 *                                           deliberately asymmetric header
 *                                           via THIS codec, emits raw bytes)
 *   - tests/firmware_logic/test_ovlstore_header_golden.py
 *                                          (packs the identical header via the
 *                                           Python model, asserts the byte
 *                                           strings are identical + that each
 *                                           side round-trips the other's bytes)
 * A byte-swap on either side changes the golden bytes and fails that test.
 * (The earlier note here claiming a live big-endian disagreement with the
 * Python model was stale: both sides are, and were verified to be, LE.)
 */
#ifndef MPS3_OVLSTORE_CODEC_H
#define MPS3_OVLSTORE_CODEC_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OVLSTORE_MAGIC "OVLS"  /* 4 raw bytes, not NUL-terminated on flash */
#define OVLSTORE_VER   1u

typedef struct {
    uint32_t static_id;
    uint32_t rm_id;
    uint32_t clear_off;   /* offset of clearing.bin within this slot's payload region */
    uint32_t clear_len;   /* bytes */
    uint32_t clear_crc;
    uint32_t part_off;    /* offset of partial.bin within this slot's payload region */
    uint32_t part_len;    /* bytes */
    uint32_t part_crc;
    uint8_t  valid;
} ovlstore_slot_desc_t;

typedef struct {
    char     magic[4];       /* "OVLS" */
    uint16_t ver;
    uint8_t  active_slot;    /* 0 = A, 1 = B */
    uint8_t  flags;          /* reserved; 0 in v1 */
    ovlstore_slot_desc_t slot[2]; /* slot[0] = A, slot[1] = B */
} ovlstore_header_t;

typedef enum {
    OVLSTORE_OK = 0,
    OVLSTORE_ERR_MAGIC,
    OVLSTORE_ERR_VERSION,
    OVLSTORE_ERR_CRC,
    OVLSTORE_ERR_NOT_VALID,   /* slot.valid == 0 */
    OVLSTORE_ERR_SPI,         /* transport-level failure */
    OVLSTORE_ERR_SHORT,       /* fewer than OVLSTORE_HEADER_PACKED_SIZE bytes given to unpack */
} overlay_store_status_t;

/* On-flash sizes -- fixed regardless of host struct padding, since the pack/
 * unpack functions below serialize field-by-field rather than relying on
 * sizeof(ovlstore_header_t)/memcpy (that was the previous, endian- and
 * padding-fragile approach; see overlay_store.c's history). */
#define OVLSTORE_SLOT_PACKED_SIZE          33u  /* 8*u32 + 1*u8 */
#define OVLSTORE_HEADER_FIXED_PACKED_SIZE  8u   /* magic(4) + ver(2) + active_slot(1) + flags(1) */
#define OVLSTORE_HEADER_PACKED_SIZE \
    (OVLSTORE_HEADER_FIXED_PACKED_SIZE + 2u * OVLSTORE_SLOT_PACKED_SIZE) /* 74 */

void ovlstore_pack_slot(const ovlstore_slot_desc_t *d,
                         uint8_t out[OVLSTORE_SLOT_PACKED_SIZE]);
void ovlstore_unpack_slot(const uint8_t in[OVLSTORE_SLOT_PACKED_SIZE],
                           ovlstore_slot_desc_t *out);

/* Packs the full 74-byte on-flash header. `out` must have at least
 * OVLSTORE_HEADER_PACKED_SIZE bytes; returns that size. */
uint32_t ovlstore_header_pack(const ovlstore_header_t *hdr,
                               uint8_t out[OVLSTORE_HEADER_PACKED_SIZE]);

/* Unpacks + structurally validates (magic, ver) a raw header buffer of at
 * least `in_len` bytes. Does NOT check slot CRCs -- that's
 * overlay_store_verify_slot()'s job, one layer up, once the slot payload
 * bytes are also available. */
overlay_store_status_t ovlstore_header_unpack(const uint8_t *in, uint32_t in_len,
                                                ovlstore_header_t *out);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_OVLSTORE_CODEC_H */
