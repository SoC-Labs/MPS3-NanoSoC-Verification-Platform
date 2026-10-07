/*
 * test_bitstream_header.c — host-gcc unit tests for common/net_proto.c's
 * mps3_bitstream_hdr_pack()/_unpack() (net-protocol.md "Bitstream framing").
 * Links net_proto.c ONLY -- see Makefile.
 *
 * The expected byte layout below is hand-derived from net-protocol.md's
 * header diagram and cross-checked against host/pusher/push.py's real
 * `struct.Struct(">4sHBBIIII")` framing (big-endian) -- if this test and
 * push.py's HEADER_SIZE/byte order ever disagree, that is the bug to chase.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../common/net_proto.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static void test_pack_matches_hand_computed_bytes(void)
{
    mps3_bitstream_hdr_t hdr;
    memset(&hdr, 0, sizeof(hdr));
    memcpy(hdr.magic, "MPS3", 4);
    hdr.ver = 1;
    hdr.kind = MPS3_BIN_KIND_PARTIAL; /* 1 */
    hdr.rm_slot = 0;
    hdr.static_id = 0xA1B2C3D4u;
    hdr.rm_id = 0x00000001u;
    hdr.len_words = 100u; /* 0x64 */
    hdr.crc32 = 0xDEADBEEFu;

    uint8_t out[MPS3_BITSTREAM_HDR_WIRE_SIZE];
    mps3_bitstream_hdr_pack(&hdr, out);

    static const uint8_t expected[MPS3_BITSTREAM_HDR_WIRE_SIZE] = {
        'M', 'P', 'S', '3',        /* magic */
        0x00, 0x01,                /* ver = 1, big-endian u16 */
        0x01,                      /* kind = partial */
        0x00,                      /* rm_slot = 0 */
        0xA1, 0xB2, 0xC3, 0xD4,    /* static_id, big-endian u32 */
        0x00, 0x00, 0x00, 0x01,    /* rm_id = 1 */
        0x00, 0x00, 0x00, 0x64,    /* len_words = 100 */
        0xDE, 0xAD, 0xBE, 0xEF,    /* crc32 */
    };
    CHECK(sizeof(expected) == MPS3_BITSTREAM_HDR_WIRE_SIZE);
    CHECK(memcmp(out, expected, sizeof(expected)) == 0);
}

static void test_round_trip(void)
{
    mps3_bitstream_hdr_t hdr, hdr2;
    memset(&hdr, 0, sizeof(hdr));
    memcpy(hdr.magic, "MPS3", 4);
    hdr.ver = 1;
    hdr.kind = MPS3_BIN_KIND_CLEARING;
    hdr.rm_slot = 1;
    hdr.static_id = 0x12345678u;
    hdr.rm_id = 0x00000002u;
    hdr.len_words = 65536u;
    hdr.crc32 = 0x00000000u;

    uint8_t wire[MPS3_BITSTREAM_HDR_WIRE_SIZE];
    mps3_bitstream_hdr_pack(&hdr, wire);

    memset(&hdr2, 0xAA, sizeof(hdr2)); /* poison, to prove unpack fills everything */
    int rc = mps3_bitstream_hdr_unpack(wire, sizeof(wire), &hdr2);
    CHECK(rc == 0);
    CHECK(memcmp(hdr2.magic, "MPS3", 4) == 0);
    CHECK(hdr2.ver == hdr.ver);
    CHECK(hdr2.kind == hdr.kind);
    CHECK(hdr2.rm_slot == hdr.rm_slot);
    CHECK(hdr2.static_id == hdr.static_id);
    CHECK(hdr2.rm_id == hdr.rm_id);
    CHECK(hdr2.len_words == hdr.len_words);
    CHECK(hdr2.crc32 == hdr.crc32);
}

static void test_rejects_short_buffer(void)
{
    uint8_t wire[MPS3_BITSTREAM_HDR_WIRE_SIZE] = { 'M', 'P', 'S', '3' };
    mps3_bitstream_hdr_t out;
    int rc = mps3_bitstream_hdr_unpack(wire, MPS3_BITSTREAM_HDR_WIRE_SIZE - 1, &out);
    CHECK(rc != 0);
}

static void test_rejects_bad_magic(void)
{
    uint8_t wire[MPS3_BITSTREAM_HDR_WIRE_SIZE];
    memset(wire, 0, sizeof(wire));
    memcpy(wire, "XXXX", 4);
    mps3_bitstream_hdr_t out;
    int rc = mps3_bitstream_hdr_unpack(wire, sizeof(wire), &out);
    CHECK(rc != 0);
}

static void test_payload_bytes_helper(void)
{
    mps3_bitstream_hdr_t hdr;
    memset(&hdr, 0, sizeof(hdr));
    hdr.len_words = 524288u; /* overlay-manifest.md's ~2 MB partial example / 4 */
    CHECK(mps3_bitstream_payload_bytes(&hdr) == 524288u * 4u);
    CHECK(mps3_bitstream_payload_bytes(&hdr) == 2097152u);
}

int main(void)
{
    test_pack_matches_hand_computed_bytes();
    test_round_trip();
    test_rejects_short_buffer();
    test_rejects_bad_magic();
    test_payload_bytes_helper();

    printf("test_bitstream_header: %d checks passed\n", s_checks);
    return 0;
}
