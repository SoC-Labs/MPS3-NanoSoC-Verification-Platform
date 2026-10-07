/*
 * crc32.c — bitwise zlib/IEEE 802.3 CRC-32. See crc32.h.
 *
 * Deliberately the simple bit-at-a-time algorithm rather than a 256-entry
 * lookup table: this firmware has no throughput requirement stated anywhere
 * in the contracts for the *checksum* step (HWICAP streaming, not CRC
 * computation, is called out in ARCHITECTURE_SPEC §7 as the bottleneck), and
 * the bitwise form is trivially checked against the standard CRC-32 "check
 * value" (crc32("123456789") == 0xCBF43926) with no transcription risk from
 * a hand-typed table. A table-based version is a drop-in perf upgrade later
 * if profiling ever shows this mattering -- same signature, same output.
 */
#include "crc32.h"

uint32_t mps3_crc32_update(uint32_t crc, const void *buf, uint32_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    uint32_t c = crc ^ 0xFFFFFFFFu;

    for (uint32_t i = 0; i < len; i++) {
        c ^= p[i];
        for (int bit = 0; bit < 8; bit++) {
            uint32_t mask = (uint32_t)(-(int32_t)(c & 1u));
            c = (c >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return c ^ 0xFFFFFFFFu;
}
