/*
 * s0_testutil.h -- shared helpers for the stage0 host tests: a CHECK macro, and
 * builders for the three on-card structures (boot image, MBR, boot-select
 * sector) written INDEPENDENTLY of the code under test (plain byte pokes + the
 * test's own CRC), so a builder bug and a loader bug cannot cancel out.
 * stage0_pack.py / stage0_mkcard.py are cross-checked against the same loader
 * by test_stage0_card + the Python tests.
 */
#ifndef S0_TESTUTIL_H
#define S0_TESTUTIL_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_fails, g_checks;
#define CHECK(cond, ...) do { g_checks++; if (!(cond)) { g_fails++; \
    printf("  FAIL %s:%d: %s -- ", __FILE__, __LINE__, #cond); \
    printf(__VA_ARGS__); printf("\n"); } } while (0)

static inline uint32_t tu_crc32(const uint8_t *p, uint32_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < n; i++) {
        c ^= p[i];
        for (int k = 0; k < 8; k++)
            c = (c & 1u) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
    }
    return c ^ 0xFFFFFFFFu;
}

static inline void tu_put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

struct tu_region {
    const uint8_t *data;
    uint32_t       len;
    uint32_t       dst;
};

/* The stage0_pack.py layout: header, entries, payloads at 4 KiB alignment.
 * Returns the image length (0 if it does not fit `cap`). */
static inline uint32_t tu_build_image(uint8_t *out, uint32_t cap, const struct tu_region *r,
                                      uint32_t n, uint32_t pc, uint32_t a0, uint32_t a1)
{
    uint32_t off = (32u + 16u * n + 0xFFFu) & ~0xFFFu, end = 0;
    memset(out, 0xFF, cap);
    for (uint32_t i = 0; i < n; i++) {
        if (off + r[i].len > cap)
            return 0;
        uint8_t *e = out + 32u + 16u * i;
        tu_put32(e + 0, off);
        tu_put32(e + 4, r[i].dst);
        tu_put32(e + 8, r[i].len);
        tu_put32(e + 12, tu_crc32(r[i].data, r[i].len));
        memcpy(out + off, r[i].data, r[i].len);
        end = off + r[i].len;
        off = (end + 0xFFFu) & ~0xFFFu;
    }
    uint8_t *h = out;
    tu_put32(h + 0, 0x424C3053u);
    tu_put32(h + 4, 2u);                             /* S0_VERSION 2 */
    tu_put32(h + 8, n);
    tu_put32(h + 12, pc);
    tu_put32(h + 16, a0);
    tu_put32(h + 20, a1);
    tu_put32(h + 24, 0u);
    tu_put32(h + 28, 0u);
    tu_put32(h + 28, tu_crc32(h, 32u + 16u * n));    /* table CRC: header + entries */
    return end;
}

/* MBR with up to four entries (type 0 = unused). */
struct tu_part { uint8_t type; uint32_t lba, n; };
static inline void tu_build_mbr(uint8_t sec[512], const struct tu_part p[4])
{
    memset(sec, 0, 512);
    for (int i = 0; i < 4; i++) {
        uint8_t *e = sec + 446 + 16 * i;
        e[4] = p[i].type;
        tu_put32(e + 8, p[i].lba);
        tu_put32(e + 12, p[i].n);
    }
    sec[510] = 0x55;
    sec[511] = 0xAA;
}

static inline void tu_build_bootsel(uint8_t sec[512], uint32_t seq, uint32_t def)
{
    memset(sec, 0, 512);
    tu_put32(sec + 0, 0x43423053u);
    tu_put32(sec + 4, 1u);
    tu_put32(sec + 8, seq);
    tu_put32(sec + 12, def);
    tu_put32(sec + 0x1FC, tu_crc32(sec, 0x1FC));
}

/* deterministic payload bytes */
static inline void tu_fill(uint8_t *p, uint32_t n, uint32_t seed)
{
    uint32_t x = seed * 2654435761u + 1u;
    for (uint32_t i = 0; i < n; i++) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        p[i] = (uint8_t)x;
    }
}

#endif /* S0_TESTUTIL_H */
