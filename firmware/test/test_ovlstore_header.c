/*
 * test_ovlstore_header.c — host-gcc unit tests for
 * overlay_store/ovlstore_codec.c (the on-flash A/B-slot header pack/unpack,
 * docs/contracts/overlay-manifest.md). Links ovlstore_codec.c ONLY -- see
 * Makefile; no SPI/register dependency at all.
 *
 * Endianness: little-endian per docs/contracts/OPEN_ISSUES.md's I15
 * resolution ("little-endian"). tests/common/ovlstore_header.py packs the
 * SAME little-endian layout; the C codec and that Python model are pinned
 * byte-for-byte by the cross-language golden test
 * (firmware/test/ovlstore_pack.c + tests/firmware_logic/
 * test_ovlstore_header_golden.py) so the two can never silently diverge.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../overlay_store/ovlstore_codec.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static void test_sizes(void)
{
    CHECK(OVLSTORE_SLOT_PACKED_SIZE == 33u);
    CHECK(OVLSTORE_HEADER_FIXED_PACKED_SIZE == 8u);
    CHECK(OVLSTORE_HEADER_PACKED_SIZE == 74u);
}

static void test_slot_pack_matches_hand_computed_bytes(void)
{
    ovlstore_slot_desc_t d = {
        .static_id = 0xA1B2C3D4u,
        .rm_id     = 0x00000001u,
        .clear_off = 0x00000000u,
        .clear_len = 393216u,      /* 0x00060000 */
        .clear_crc = 0x11111111u,
        .part_off  = 393216u,
        .part_len  = 2097152u,     /* 0x00200000 */
        .part_crc  = 0x22222222u,
        .valid     = 1,
    };
    uint8_t out[OVLSTORE_SLOT_PACKED_SIZE];
    ovlstore_pack_slot(&d, out);

    static const uint8_t expected[OVLSTORE_SLOT_PACKED_SIZE] = {
        0xD4, 0xC3, 0xB2, 0xA1,             /* static_id, little-endian */
        0x01, 0x00, 0x00, 0x00,             /* rm_id */
        0x00, 0x00, 0x00, 0x00,             /* clear_off */
        0x00, 0x00, 0x06, 0x00,             /* clear_len = 393216 */
        0x11, 0x11, 0x11, 0x11,             /* clear_crc */
        0x00, 0x00, 0x06, 0x00,             /* part_off  = 393216 */
        0x00, 0x00, 0x20, 0x00,             /* part_len  = 2097152 */
        0x22, 0x22, 0x22, 0x22,             /* part_crc */
        0x01,                                /* valid */
    };
    CHECK(sizeof(expected) == OVLSTORE_SLOT_PACKED_SIZE);
    CHECK(memcmp(out, expected, sizeof(expected)) == 0);

    ovlstore_slot_desc_t back;
    memset(&back, 0xAA, sizeof(back));
    ovlstore_unpack_slot(out, &back);
    CHECK(back.static_id == d.static_id);
    CHECK(back.rm_id == d.rm_id);
    CHECK(back.clear_off == d.clear_off);
    CHECK(back.clear_len == d.clear_len);
    CHECK(back.clear_crc == d.clear_crc);
    CHECK(back.part_off == d.part_off);
    CHECK(back.part_len == d.part_len);
    CHECK(back.part_crc == d.part_crc);
    CHECK(back.valid == d.valid);
}

static ovlstore_header_t make_sample_header(void)
{
    ovlstore_header_t hdr;
    memset(&hdr, 0, sizeof(hdr));
    memcpy(hdr.magic, OVLSTORE_MAGIC, 4);
    hdr.ver = OVLSTORE_VER;
    hdr.active_slot = 0;
    hdr.flags = 0;
    hdr.slot[0] = (ovlstore_slot_desc_t){
        .static_id = 0xA1B2C3D4u, .rm_id = 0, .clear_off = 0, .clear_len = 1024,
        .clear_crc = 0x1111, .part_off = 1024, .part_len = 2048, .part_crc = 0x2222,
        .valid = 1,
    };
    hdr.slot[1] = (ovlstore_slot_desc_t){
        .static_id = 0xA1B2C3D4u, .rm_id = 1, .clear_off = 0, .clear_len = 1024,
        .clear_crc = 0x3333, .part_off = 1024, .part_len = 4096, .part_crc = 0x4444,
        .valid = 0,
    };
    return hdr;
}

static void test_header_round_trip(void)
{
    ovlstore_header_t hdr = make_sample_header();
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    uint32_t packed_len = ovlstore_header_pack(&hdr, raw);
    CHECK(packed_len == OVLSTORE_HEADER_PACKED_SIZE);

    ovlstore_header_t back;
    memset(&back, 0xAA, sizeof(back));
    overlay_store_status_t st = ovlstore_header_unpack(raw, sizeof(raw), &back);
    CHECK(st == OVLSTORE_OK);
    CHECK(memcmp(back.magic, OVLSTORE_MAGIC, 4) == 0);
    CHECK(back.ver == hdr.ver);
    CHECK(back.active_slot == hdr.active_slot);
    CHECK(back.flags == hdr.flags);
    CHECK(back.slot[0].rm_id == hdr.slot[0].rm_id);
    CHECK(back.slot[0].valid == hdr.slot[0].valid);
    CHECK(back.slot[1].rm_id == hdr.slot[1].rm_id);
    CHECK(back.slot[1].valid == hdr.slot[1].valid);
    CHECK(back.slot[1].part_crc == hdr.slot[1].part_crc);
}

static void test_rejects_short_buffer(void)
{
    ovlstore_header_t hdr = make_sample_header();
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    ovlstore_header_pack(&hdr, raw);

    ovlstore_header_t back;
    overlay_store_status_t st = ovlstore_header_unpack(raw, OVLSTORE_HEADER_PACKED_SIZE - 1, &back);
    CHECK(st == OVLSTORE_ERR_SHORT);
}

static void test_rejects_bad_magic(void)
{
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    memset(raw, 0, sizeof(raw));
    memcpy(raw, "XXXX", 4);
    ovlstore_header_t back;
    overlay_store_status_t st = ovlstore_header_unpack(raw, sizeof(raw), &back);
    CHECK(st == OVLSTORE_ERR_MAGIC);
}

static void test_rejects_bad_version(void)
{
    ovlstore_header_t hdr = make_sample_header();
    hdr.ver = 99;
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    ovlstore_header_pack(&hdr, raw);
    ovlstore_header_t back;
    overlay_store_status_t st = ovlstore_header_unpack(raw, sizeof(raw), &back);
    CHECK(st == OVLSTORE_ERR_VERSION);
}

/* §8A.5 "an interrupted/failed write cannot brick the default" invariant --
 * mirrors tests/integration/test_manifest_roundtrip.py's
 * test_ab_slot_commit_cannot_brick_the_default(), re-expressed against the
 * real C codec instead of the Python model. */
static void test_active_slot_invariant(void)
{
    ovlstore_header_t hdr = make_sample_header();
    /* slot[1] (B) is NOT valid in make_sample_header(); pointing
     * active_slot at it models an interrupted commit. */
    hdr.active_slot = 1;
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    ovlstore_header_pack(&hdr, raw);
    ovlstore_header_t back;
    ovlstore_header_unpack(raw, sizeof(raw), &back);
    CHECK(back.slot[back.active_slot].valid == 0); /* would brick the default -- caller must reject */

    /* The safe recovery: active_slot pointing at A (still valid). */
    hdr.active_slot = 0;
    ovlstore_header_pack(&hdr, raw);
    ovlstore_header_unpack(raw, sizeof(raw), &back);
    CHECK(back.slot[back.active_slot].valid == 1);
}

int main(void)
{
    test_sizes();
    test_slot_pack_matches_hand_computed_bytes();
    test_header_round_trip();
    test_rejects_short_buffer();
    test_rejects_bad_magic();
    test_rejects_bad_version();
    test_active_slot_invariant();

    printf("test_ovlstore_header: %d checks passed\n", s_checks);
    return 0;
}
