/*
 * test_ovlstore.c — host tests for the READ-ONLY overlay-store tooling.
 *
 * Builds a synthetic 8 MiB-layout store image (sparse: only header + payload
 * regions written) using the verbatim-ported codec, then proves:
 *   1. codec golden bytes: 74-byte header, little-endian fields at the frozen
 *      offsets (I15-resolved layout — a byte-swap regression fails here);
 *   2. header read + slot CRC verify pass on a good image;
 *   3. one flipped payload byte -> OVLSTORE_ERR_CRC with the right computed
 *      value reported;
 *   4. bad magic / bad version / short file -> the right structured errors;
 *   5. an out-of-range descriptor (payload pointing past its region) is
 *      rejected, not read;
 *   6. an invalid slot reports NOT_VALID and is never CRC'd;
 *   7. the whole flow works on a read-only (0400) file — no write access is
 *      ever needed (the D16 embargo made structural).
 * CRC32 is cross-checked against Python binascii by run_tests.sh.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "../crc32.h"
#include "../ovlstore_read.h"

static int g_checks;
#define CHECK(cond, ...) do { \
    g_checks++; \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
        fprintf(stderr, __VA_ARGS__); \
        fprintf(stderr, "\n"); \
        return 1; \
    } \
} while (0)

static const char *g_img;

static uint8_t pat(unsigned i, unsigned salt)
{
    return (uint8_t)(i * 31u + salt);
}

static int write_image(const ovlstore_header_t *hdr,
                       unsigned a_clear_len, unsigned a_part_len,
                       int corrupt_a_partial)
{
    FILE *f = fopen(g_img, "wb");
    if (!f)
        return -1;

    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    ovlstore_header_pack(hdr, raw);
    fwrite(raw, 1, sizeof(raw), f);

    /* slot A payloads at their descriptor offsets */
    fseek(f, (long)(OVLSTORE_SLOT_A_PAYLOAD_OFFSET + hdr->slot[0].clear_off),
          SEEK_SET);
    for (unsigned i = 0; i < a_clear_len; i++)
        fputc(pat(i, 1), f);

    fseek(f, (long)(OVLSTORE_SLOT_A_PAYLOAD_OFFSET + hdr->slot[0].part_off),
          SEEK_SET);
    for (unsigned i = 0; i < a_part_len; i++) {
        uint8_t b = pat(i, 2);
        if (corrupt_a_partial && i == a_part_len / 2)
            b ^= 0xFFu;
        fputc(b, f);
    }
    fclose(f);
    return 0;
}

int main(int argc, char **argv)
{
    g_img = (argc > 1) ? argv[1] : "ovlstore_test.img";

    /* ---- build descriptor + reference CRCs -------------------------------- */
    enum { CLEAR_LEN = 5000, PART_LEN = 12345 };
    uint8_t *cb = malloc(CLEAR_LEN), *pb = malloc(PART_LEN);
    for (unsigned i = 0; i < CLEAR_LEN; i++) cb[i] = pat(i, 1);
    for (unsigned i = 0; i < PART_LEN; i++)  pb[i] = pat(i, 2);
    uint32_t ccrc = mps3_crc32(0, cb, CLEAR_LEN);
    uint32_t pcrc = mps3_crc32(0, pb, PART_LEN);

    ovlstore_header_t hdr;
    memset(&hdr, 0, sizeof(hdr));
    memcpy(hdr.magic, "OVLS", 4);
    hdr.ver = OVLSTORE_VER;
    hdr.active_slot = 0;
    hdr.flags = 0;
    hdr.slot[0].static_id = 0x14E1A2D8u;
    hdr.slot[0].rm_id     = 0x010000A1u;
    hdr.slot[0].clear_off = 0x100;
    hdr.slot[0].clear_len = CLEAR_LEN;
    hdr.slot[0].clear_crc = ccrc;
    hdr.slot[0].part_off  = 0x2000;
    hdr.slot[0].part_len  = PART_LEN;
    hdr.slot[0].part_crc  = pcrc;
    hdr.slot[0].valid     = 1;
    /* slot B left invalid */

    /* ---- 1. codec golden bytes ------------------------------------------- */
    {
        uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
        uint32_t n = ovlstore_header_pack(&hdr, raw);
        CHECK(n == 74, "packed size %u != 74", n);
        CHECK(memcmp(raw, "OVLS", 4) == 0, "magic bytes");
        CHECK(raw[4] == 0x01 && raw[5] == 0x00, "ver not LE u16");
        CHECK(raw[6] == 0x00, "active_slot byte");
        /* slot A static_id at offset 8, little-endian 0x14E1A2D8 */
        CHECK(raw[8] == 0xD8 && raw[9] == 0xA2 && raw[10] == 0xE1 &&
              raw[11] == 0x14, "static_id not LE at frozen offset");
        /* slot A rm_id at offset 12 */
        CHECK(raw[12] == 0xA1 && raw[13] == 0x00 && raw[14] == 0x00 &&
              raw[15] == 0x01, "rm_id not LE at frozen offset");
        /* slot A valid flag at 8+32=40; slot B block at 41..73 */
        CHECK(raw[40] == 1, "slot A valid byte offset");
        CHECK(raw[73] == 0, "slot B valid byte offset");

        ovlstore_header_t back;
        CHECK(ovlstore_header_unpack(raw, n, &back) == OVLSTORE_OK, "unpack");
        CHECK(back.slot[0].part_crc == pcrc, "roundtrip part_crc");
        CHECK(ovlstore_header_unpack(raw, 73, &back) == OVLSTORE_ERR_SHORT,
              "short unpack");
        raw[0] = 'X';
        CHECK(ovlstore_header_unpack(raw, n, &back) == OVLSTORE_ERR_MAGIC,
              "bad magic unpack");
        raw[0] = 'O'; raw[4] = 9;
        CHECK(ovlstore_header_unpack(raw, n, &back) == OVLSTORE_ERR_VERSION,
              "bad version unpack");
    }

    /* ---- 2. good image: header + verify ----------------------------------- */
    CHECK(write_image(&hdr, CLEAR_LEN, PART_LEN, 0) == 0, "write image");
    /* read-only from here on — proves no write access is needed (7) */
    chmod(g_img, 0400);
    {
        ovlstore_src_t src;
        CHECK(ovlstore_src_open(g_img, &src) == 0, "open image");
        ovlstore_header_t h;
        CHECK(ovlstore_read_header(&src, &h) == OVLSTORE_OK, "read header");
        CHECK(h.active_slot == 0, "active slot");
        CHECK(h.slot[0].rm_id == 0x010000A1u, "slot A rm_id");

        uint32_t cc = 0, pc = 0;
        CHECK(ovlstore_verify_slot(&src, &h, 0, &cc, &pc) == OVLSTORE_OK,
              "slot A verify");
        CHECK(cc == ccrc && pc == pcrc, "computed CRCs");

        /* 6. invalid slot: declared not-valid, never CRC'd */
        CHECK(ovlstore_verify_slot(&src, &h, 1, &cc, &pc) ==
              OVLSTORE_ERR_NOT_VALID, "slot B must be NOT_VALID");
        ovlstore_src_close(&src);
    }

    /* ---- 3. corrupt payload -> ERR_CRC ------------------------------------- */
    chmod(g_img, 0600);
    CHECK(write_image(&hdr, CLEAR_LEN, PART_LEN, 1) == 0, "write corrupt");
    chmod(g_img, 0400);
    {
        ovlstore_src_t src;
        CHECK(ovlstore_src_open(g_img, &src) == 0, "open corrupt");
        ovlstore_header_t h;
        CHECK(ovlstore_read_header(&src, &h) == OVLSTORE_OK, "hdr corrupt img");
        uint32_t cc = 0, pc = 0;
        CHECK(ovlstore_verify_slot(&src, &h, 0, &cc, &pc) == OVLSTORE_ERR_CRC,
              "corrupt partial must fail CRC");
        CHECK(cc == ccrc, "clearing CRC still good");
        CHECK(pc != pcrc, "partial CRC must differ");
        ovlstore_src_close(&src);
    }

    /* ---- 5. out-of-range descriptor is rejected, not read ------------------ */
    {
        ovlstore_header_t bad = hdr;
        bad.slot[0].part_off = 0x3F0000u;              /* A region is 4 MiB.. */
        bad.slot[0].part_len = 0x200000u;              /* ..this runs past B  */
        chmod(g_img, 0600);
        CHECK(write_image(&bad, CLEAR_LEN, 16, 0) == 0, "write bad-desc");
        chmod(g_img, 0400);
        ovlstore_src_t src;
        CHECK(ovlstore_src_open(g_img, &src) == 0, "open bad-desc");
        ovlstore_header_t h;
        CHECK(ovlstore_read_header(&src, &h) == OVLSTORE_OK, "hdr bad-desc");
        uint32_t cc, pc;
        CHECK(ovlstore_verify_slot(&src, &h, 0, &cc, &pc) == OVLSTORE_ERR_SPI,
              "out-of-range descriptor must be rejected");
        ovlstore_src_close(&src);
    }

    /* ---- 4. bad magic image ------------------------------------------------ */
    {
        chmod(g_img, 0600);
        FILE *f = fopen(g_img, "r+b");
        fputc('!', f);
        fclose(f);
        chmod(g_img, 0400);
        ovlstore_src_t src;
        CHECK(ovlstore_src_open(g_img, &src) == 0, "open bad-magic");
        ovlstore_header_t h;
        CHECK(ovlstore_read_header(&src, &h) == OVLSTORE_ERR_MAGIC,
              "bad magic image");
        ovlstore_src_close(&src);
    }

    /* CRC32 reference vector: "123456789" -> 0xCBF43926 (IEEE/zlib) */
    CHECK(mps3_crc32(0, "123456789", 9) == 0xCBF43926u, "crc32 check vector");

    free(cb);
    free(pb);
    printf("test_ovlstore: PASS (%d checks)\n", g_checks);
    return 0;
}
