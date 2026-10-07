/*
 * test_stage0_core.c -- host unit test for the portable loader core.
 *
 * Board-free proof of the pack -> parse -> CRC -> region-copy contract: it links
 * the SAME stage0_core.c the target uses, drives it through an in-memory backend
 * that stands in for storage + DDR, and checks the loader copies exactly the
 * right bytes to the right (simulated) DDR addresses and validates every CRC.
 *
 * The boot image under test is produced by stage0_pack.py in the test Makefile,
 * so this also proves the packer and the C CRC32 agree by construction.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "../stage0_boot.h"

/* ---- in-memory backend -------------------------------------------------- */

/* storage = the packed boot image, read whole into RAM */
static uint8_t *g_img;
static long     g_img_len;

/* simulated DDR: a flat window [DDR_BASE, DDR_BASE+DDR_SPAN) */
#define DDR_BASE 0x80000000u
#define DDR_SPAN (256u * 1024u * 1024u)   /* 256 MiB is plenty for the test map */
static uint8_t *g_ddr;

static int sto_read(uint32_t off, void *dst, uint32_t len, void *ctx)
{
    (void)ctx;
    if ((long)off + (long)len > g_img_len) return -1;   /* short read */
    memcpy(dst, g_img + off, len);
    return 0;
}

static void *addr_to_ptr(uint32_t a, uint32_t len, void *ctx)
{
    (void)ctx;
    if (a < DDR_BASE || (uint64_t)a + len > (uint64_t)DDR_BASE + DDR_SPAN)
        return NULL;                    /* reject out-of-range like the target */
    return g_ddr + (a - DDR_BASE);
}

/* ---- helpers ------------------------------------------------------------ */

static uint8_t *slurp(const char *path, long *out_len)
{
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); exit(2); }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    uint8_t *b = malloc(n);
    if (fread(b, 1, n, f) != (size_t)n) { fprintf(stderr, "short read %s\n", path); exit(2); }
    fclose(f);
    *out_len = n;
    return b;
}

static int fails;
#define CHECK(cond, ...) do { if (!(cond)) { \
    printf("  FAIL: "); printf(__VA_ARGS__); printf("\n"); fails++; } } while (0)

/* Independent CRC32 (zlib params) to cross-check s0_crc32 against a second impl */
static uint32_t ref_crc32(const uint8_t *p, uint32_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < n; i++) {
        c ^= p[i];
        for (int k = 0; k < 8; k++)
            c = (c & 1) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
    }
    return c ^ 0xFFFFFFFFu;
}

/* ---- tests -------------------------------------------------------------- */

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: %s boot.img [expect_file@0xADDR ...]\n", argv[0]); return 2; }

    g_img = slurp(argv[1], &g_img_len);
    g_ddr = calloc(1, DDR_SPAN);
    if (!g_ddr) { fprintf(stderr, "OOM ddr\n"); return 2; }

    struct s0_backend be = { sto_read, addr_to_ptr, NULL };
    struct s0_result  res;

    printf("test: s0_crc32 matches a reference impl\n");
    const char *v = "The quick brown fox";
    CHECK(s0_crc32(v, (uint32_t)strlen(v)) == ref_crc32((const uint8_t *)v, (uint32_t)strlen(v)),
          "s0_crc32 disagrees with reference");
    CHECK(s0_crc32("", 0) == 0u, "crc32 of empty != 0");

    printf("test: happy-path load of %s\n", argv[1]);
    int rc = s0_load(&be, &res);
    CHECK(rc == S0_OK, "s0_load rc=%d (expected 0)", rc);

    /* Compare each region the packer was told about against simulated DDR. */
    for (int i = 2; i < argc; i++) {
        char *at = strrchr(argv[i], '@');
        CHECK(at != NULL, "bad expect spec %s", argv[i]);
        if (!at) continue;
        *at = 0;
        uint32_t addr = (uint32_t)strtoul(at + 1, NULL, 0);
        long elen; uint8_t *exp = slurp(argv[i], &elen);
        uint8_t *got = addr_to_ptr(addr, (uint32_t)elen, NULL);
        CHECK(got != NULL, "addr 0x%08X out of simulated DDR", addr);
        CHECK(got && memcmp(got, exp, elen) == 0,
              "region %s @0x%08X: %ld bytes differ in DDR", argv[i], addr, elen);
        printf("    region %-20s @0x%08X  %ld B  OK\n", argv[i], addr, elen);
        free(exp);
    }

    printf("test: header hand-off fields are sane\n");
    CHECK(res.entry_pc != 0u, "entry_pc is 0");

    /* Negative tests: corrupt the image copy and confirm the loader refuses. */
    printf("test: corrupt magic -> S0_EMAGIC\n");
    { uint8_t save = g_img[0]; g_img[0] ^= 0xFF;
      CHECK(s0_load(&be, &res) == S0_EMAGIC, "did not reject bad magic");
      g_img[0] = save; }

    printf("test: flipped payload byte -> S0_ECRC\n");
    {   /* flip a byte inside the first region's payload (well past the entries) */
        long pos = g_img_len - 1;               /* last byte is inside a payload */
        uint8_t save = g_img[pos]; g_img[pos] ^= 0xFF;
        int r = s0_load(&be, &res);
        CHECK(r == S0_ECRC, "flipped payload byte not caught (rc=%d)", r);
        g_img[pos] = save;
    }

    printf("test: truncated image -> S0_EREAD\n");
    { long save = g_img_len; g_img_len = (long)sizeof(struct s0_header) - 1;
      CHECK(s0_load(&be, &res) == S0_EREAD, "did not reject truncated image");
      g_img_len = save; }

    if (fails) { printf("\nRESULT: %d CHECK(s) FAILED\n", fails); return 1; }
    printf("\nRESULT: all stage0-core checks PASSED\n");
    return 0;
}
