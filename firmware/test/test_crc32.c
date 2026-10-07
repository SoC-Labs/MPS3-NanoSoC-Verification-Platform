/*
 * test_crc32.c — host-gcc unit tests for common/crc32.c.
 * Links ONLY crc32.c -- zero other firmware dependency, see Makefile.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../common/crc32.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static void test_empty_buffer_is_zero(void)
{
    CHECK(mps3_crc32(NULL, 0) == 0u);
    CHECK(mps3_crc32_update(MPS3_CRC32_INIT, NULL, 0) == 0u);
}

static void test_standard_check_value(void)
{
    /* The canonical CRC-32/ISO-HDLC ("zlib") check value: crc32("123456789")
     * == 0xCBF43926. Every implementation claiming zlib-compatibility is
     * checked against this. */
    const char *s = "123456789";
    uint32_t crc = mps3_crc32(s, (uint32_t)strlen(s));
    CHECK(crc == 0xCBF43926u);
}

static void test_known_python_zlib_values(void)
{
    /* zlib.crc32(b"a") == 0xE8B7BE43; zlib.crc32(b"abc") == 0x352441C2 --
     * cross-checked against CPython's zlib module (net-protocol.md I13
     * pins the wire crc32 to exactly this algorithm). */
    CHECK(mps3_crc32("a", 1) == 0xE8B7BE43u);
    CHECK(mps3_crc32("abc", 3) == 0x352441C2u);
}

static void test_chained_update_matches_one_shot(void)
{
    const char *s = "the quick brown fox jumps over the lazy dog";
    uint32_t len = (uint32_t)strlen(s);
    uint32_t whole = mps3_crc32(s, len);

    /* Split into two chunks, chained the way config_agent.c's "running
     * crc32 as bytes arrive" TODO describes. */
    uint32_t split = 17;
    uint32_t running = mps3_crc32_update(MPS3_CRC32_INIT, s, split);
    running = mps3_crc32_update(running, s + split, len - split);
    CHECK(running == whole);

    /* And split into many 1-byte chunks, for good measure. */
    uint32_t byte_by_byte = MPS3_CRC32_INIT;
    for (uint32_t i = 0; i < len; i++) {
        byte_by_byte = mps3_crc32_update(byte_by_byte, s + i, 1);
    }
    CHECK(byte_by_byte == whole);
}

static void test_single_bit_change_changes_crc(void)
{
    uint8_t a[4] = { 0x00, 0x01, 0x02, 0x03 };
    uint8_t b[4] = { 0x00, 0x01, 0x02, 0x04 }; /* last byte differs */
    CHECK(mps3_crc32(a, sizeof(a)) != mps3_crc32(b, sizeof(b)));
}

int main(void)
{
    test_empty_buffer_is_zero();
    test_standard_check_value();
    test_known_python_zlib_values();
    test_chained_update_matches_one_shot();
    test_single_bit_change_changes_crc();

    printf("test_crc32: %d checks passed\n", s_checks);
    return 0;
}
