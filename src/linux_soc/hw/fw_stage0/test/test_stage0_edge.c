/*
 * test_stage0_edge.c -- EXTENDED host edge-case unit test for the portable
 * stage0 loader core (src/linux_soc/hw/fw_stage0/stage0_core.c).
 *
 * Companion to test_stage0_core.c, which covers the happy path + bad-magic + one
 * payload-flip + header-truncation. This file builds boot images IN MEMORY (no
 * packer dependency; CRCs via the core's own s0_crc32) so it can exercise EVERY
 * return code and boundary in s0_load():
 *
 *   S0_EVERSION (incl. a legacy v1 image), S0_EHDRCRC (corrupt + silent header
 *   mutation), S0_ENENT (0 and MAX+1), the MAX-entry boundary that must PASS, the
 *   addr_to_ptr()==NULL -> S0_EREAD path (out-of-range and oversized dst), a
 *   zero-length region, a CRC failure on a LATER region (proving earlier regions
 *   already landed), entry-table truncation, the check-ORDER precedence (version
 *   before num_entries before the table CRC), and -- v2 -- that the TABLE CRC
 *   binds every entry (dst / len / src / payload CRC) and is the image's
 *   identity: two images that differ only in payload get different
 *   header_crc32 values, and one image's payload under another's table is
 *   refused.
 *
 * Wired into test/Makefile (`make -C src/linux_soc/hw/fw_stage0/test`) and thus
 * into `make check` -- previously NOTHING ran the stage0 host tests in CI, so the
 * harness boot core's one board-free proof could rot silently.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "../stage0_boot.h"

/* ---- in-memory backend (storage image + simulated DDR) ------------------ */

static uint8_t *g_img;
static long     g_img_len;

#define DDR_BASE 0x80000000u
#define DDR_SPAN (256u * 1024u * 1024u)
static uint8_t *g_ddr;

static int sto_read(uint32_t off, void *dst, uint32_t len, void *ctx)
{
    (void)ctx;
    if ((long)off + (long)len > g_img_len) return -1;   /* short read */
    memcpy(dst, g_img + off, len);
    return 0;
}

/* Rejects out-of-range like the target's host test does. NOTE: the REAL
 * target_addr_to_ptr() in stage0.c bounds-checks against the DDR aperture (it
 * used to ignore len -- hardened alongside this test); this backend mirrors that
 * so the core's NULL -> S0_EREAD propagation is what is under test. */
static void *addr_to_ptr(uint32_t a, uint32_t len, void *ctx)
{
    (void)ctx;
    /* Same predicate the real target_addr_to_ptr() uses -- so the bounds logic
     * proven here is the logic that runs on silicon. */
    if (!s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len))
        return NULL;
    return g_ddr + (a - DDR_BASE);
}

static const struct s0_backend BE = { sto_read, addr_to_ptr, NULL };

/* ---- tiny CHECK harness ------------------------------------------------- */

static int fails;
#define CHECK(cond, ...) do { if (!(cond)) { \
    printf("  FAIL: "); printf(__VA_ARGS__); printf("\n"); fails++; } } while (0)
#define OK(...) do { printf("    ok: "); printf(__VA_ARGS__); printf("\n"); } while (0)

/* ---- in-memory image builder ------------------------------------------- */

struct region { uint32_t dst_addr; uint32_t len; const uint8_t *data; };

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0]=(uint8_t)v; p[1]=(uint8_t)(v>>8); p[2]=(uint8_t)(v>>16); p[3]=(uint8_t)(v>>24);
}

/*
 * Pack a stage0 image. `num_entries_field` is written into the header
 * independently of `nregs` (so num_entries=0/9 can be tested without laying out
 * that many payloads -- s0_load() range-checks num_entries BEFORE reading any
 * entry). header_crc32 is the v2 TABLE CRC (header with that field zeroed, then
 * the nregs entries), computed correctly UNLESS `bad_hdr_crc` is non-zero. Payloads are packed tight after the entry table;
 * alignment is irrelevant (the loader follows src_offset). Returns malloc'd buf.
 */
static uint8_t *pack(uint32_t magic, uint32_t version, uint32_t num_entries_field,
                     uint32_t pc, uint32_t a0, uint32_t a1, uint32_t flags,
                     const struct region *regs, uint32_t nregs,
                     int bad_hdr_crc, long *out_len)
{
    uint32_t entries_off = (uint32_t)sizeof(struct s0_header);
    uint32_t body_off    = entries_off + nregs * (uint32_t)sizeof(struct s0_entry);
    uint32_t total = body_off;
    for (uint32_t i = 0; i < nregs; i++) total += regs[i].len;
    if (total < sizeof(struct s0_header)) total = sizeof(struct s0_header);

    uint8_t *img = calloc(1, total ? total : 1);
    put_u32(img + 0,  magic);
    put_u32(img + 4,  version);
    put_u32(img + 8,  num_entries_field);
    put_u32(img + 12, pc);
    put_u32(img + 16, a0);
    put_u32(img + 20, a1);
    put_u32(img + 24, flags);
    put_u32(img + 28, 0);

    uint32_t cur = body_off;
    for (uint32_t i = 0; i < nregs; i++) {
        uint32_t crc = s0_crc32(regs[i].data, regs[i].len);
        uint8_t *e = img + entries_off + i * sizeof(struct s0_entry);
        put_u32(e + 0,  cur);            /* src_offset */
        put_u32(e + 4,  regs[i].dst_addr);
        put_u32(e + 8,  regs[i].len);
        put_u32(e + 12, crc);
        if (regs[i].len) memcpy(img + cur, regs[i].data, regs[i].len);
        cur += regs[i].len;
    }

    uint32_t hcrc = s0_crc32(img, sizeof(struct s0_header) + nregs * (uint32_t)sizeof(struct s0_entry));
    if (bad_hdr_crc) hcrc ^= 0xA5A5A5A5u;
    put_u32(img + 28, hcrc);

    *out_len = (long)total;
    return img;
}

/* Re-seal the table CRC after a deliberate edit, so the test reaches the check
 * behind it (the edit itself would otherwise be caught as S0_EHDRCRC). */
static void reseal(uint8_t *img, uint32_t nregs)
{
    put_u32(img + 28, 0);
    put_u32(img + 28, s0_crc32(img, (uint32_t)sizeof(struct s0_header) +
                                    nregs * (uint32_t)sizeof(struct s0_entry)));
}

static void use(uint8_t *img, long len)
{
    free(g_img);
    g_img = img;
    g_img_len = len;
    memset(g_ddr, 0, DDR_SPAN);
}

static uint8_t *mkbuf(uint32_t len, uint32_t seed)
{
    uint8_t *b = malloc(len ? len : 1);
    for (uint32_t i = 0; i < len; i++) b[i] = (uint8_t)(i * 7u + seed);
    return b;
}

/* ---- tests -------------------------------------------------------------- */

int main(void)
{
    g_ddr = calloc(1, DDR_SPAN);
    if (!g_ddr) { fprintf(stderr, "OOM ddr\n"); return 2; }

    struct s0_result res;
    long len;

    /* s0_region_in_bounds -- the exact predicate target_addr_to_ptr() uses on
     * silicon to refuse writing outside DRAM. Test it directly (overflow-safe). */
    printf("test: s0_region_in_bounds predicate (the on-silicon DDR bound)\n");
    { const uint32_t B = 0x80000000u, S = 0x40000000u;   /* 1 GiB, real DDR aperture */
      CHECK(s0_region_in_bounds(B, S, B, 16), "in-range region rejected");
      CHECK(s0_region_in_bounds(B, S, B + S - 16u, 16), "exact-fit tail region rejected");
      CHECK(!s0_region_in_bounds(B, S, B - 1u, 16), "below-base accepted");
      CHECK(!s0_region_in_bounds(B, S, B + S - 8u, 16), "tail overrun accepted");
      CHECK(!s0_region_in_bounds(B, S, B, S + 1u), "len>size accepted");
      CHECK( s0_region_in_bounds(B, S, B, S), "whole-aperture region rejected");
      CHECK(!s0_region_in_bounds(B, S, 0xFFFFFFF0u, 0x20u), "addr+len wrap accepted");
      CHECK( s0_region_in_bounds(B, S, B, 0), "zero-len in-range rejected");
      OK("bounds predicate correct across base/tail/overrun/wrap/zero-len"); }

    uint8_t *p0 = mkbuf(4096, 1), *p1 = mkbuf(8192, 2), *p2 = mkbuf(1024, 3);
    struct region r3[3] = {
        { 0x80000000u, 4096, p0 },
        { 0x80400000u, 8192, p1 },
        { 0x82200000u, 1024, p2 },
    };

    printf("test: valid 3-region image -> S0_OK\n");
    { long l; uint8_t *img = pack(S0_MAGIC, S0_VERSION, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &l);
      use(img, l);
      CHECK(s0_load(&BE, &res) == S0_OK, "baseline valid image did not load");
      CHECK(memcmp(g_ddr + (0x80400000u - DDR_BASE), p1, 8192) == 0, "region1 bytes wrong in DDR");
      OK("baseline loads, region1 landed"); }

    printf("test: version mismatch -> S0_EVERSION (incl. a legacy v1 image)\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION + 1u, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EVERSION, "bad version not rejected");
      img = pack(S0_MAGIC, 1u, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EVERSION, "a v1 image (header-only CRC) must be refused"); }

    printf("test: header CRC corruption -> S0_EHDRCRC\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 1, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EHDRCRC, "corrupt header CRC not rejected"); }

    printf("test: any header field flip is caught by header CRC (flip entry_pc)\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &len);
      img[12] ^= 0xFF;
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EHDRCRC, "silent header mutation not caught"); }

    printf("test: num_entries == 0 -> S0_ENENT\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 0, 0x80000000u, 0, 0, 0, r3, 3, 0, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_ENENT, "num_entries=0 not rejected"); }

    printf("test: num_entries == S0_MAX_ENTRIES+1 -> S0_ENENT\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, S0_MAX_ENTRIES + 1u, 0x80000000u, 0, 0, 0, r3, 3, 0, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_ENENT, "num_entries=MAX+1 not rejected"); }

    printf("test: num_entries == S0_MAX_ENTRIES (8) with 8 regions -> S0_OK (boundary)\n");
    { struct region r8[8]; uint8_t *bufs[8];
      for (int i = 0; i < 8; i++) {
          bufs[i] = mkbuf(256u + (uint32_t)i * 16u, (uint32_t)(i + 10));
          r8[i].dst_addr = DDR_BASE + (uint32_t)i * 0x100000u;
          r8[i].len = 256u + (uint32_t)i * 16u;
          r8[i].data = bufs[i];
      }
      uint8_t *img = pack(S0_MAGIC, S0_VERSION, 8, 0x80000000u, 0, 0, 0, r8, 8, 0, &len);
      use(img, len);
      int rc = s0_load(&BE, &res);
      CHECK(rc == S0_OK, "8-region (MAX) image rejected (rc=%d)", rc);
      CHECK(memcmp(g_ddr + 7u * 0x100000u, bufs[7], r8[7].len) == 0, "8th region not landed");
      for (int i = 0; i < 8; i++) free(bufs[i]);
      OK("MAX-entry boundary loads all 8 regions"); }

    printf("test: dst_addr out of DDR range -> addr_to_ptr NULL -> S0_EREAD\n");
    { struct region bad = { 0x70000000u, 4096, p0 };
      uint8_t *img = pack(S0_MAGIC, S0_VERSION, 1, 0x80000000u, 0, 0, 0, &bad, 1, 0, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EREAD, "out-of-range dst_addr not rejected"); }

    printf("test: dst_addr+len overruns DDR span -> addr_to_ptr NULL -> S0_EREAD\n");
    { struct region over = { DDR_BASE + DDR_SPAN - 16u, 4096, p0 };
      uint8_t *img = pack(S0_MAGIC, S0_VERSION, 1, 0x80000000u, 0, 0, 0, &over, 1, 0, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EREAD, "oversized region (len overruns span) not rejected"); }

    printf("test: zero-length region (len=0, crc=0) -> S0_OK (documents current behaviour)\n");
    { struct region z = { 0x80000000u, 0, NULL };
      uint8_t *img = pack(S0_MAGIC, S0_VERSION, 1, 0x80000000u, 0, 0, 0, &z, 1, 0, &len);
      use(img, len);
      int rc = s0_load(&BE, &res);
      CHECK(rc == S0_OK, "zero-length region unexpectedly rejected (rc=%d)", rc); }

    printf("test: CRC failure on a LATER region (index 1 of 3) -> S0_ECRC,\n"
           "      and the earlier region still landed in DDR before the abort\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &len);
      uint32_t r1_off = (uint32_t)sizeof(struct s0_header)
                        + 3u * (uint32_t)sizeof(struct s0_entry) + 4096u;
      img[r1_off + 100] ^= 0xFF;
      use(img, len);
      int rc = s0_load(&BE, &res);
      CHECK(rc == S0_ECRC, "corrupt LATER region not caught (rc=%d)", rc);
      CHECK(memcmp(g_ddr + (0x80000000u - DDR_BASE), p0, 4096) == 0,
            "region0 should have landed before the region1 CRC abort"); }

    printf("test: entry src_offset beyond image end -> S0_EREAD\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 1, 0x80000000u, 0, 0, 0, r3, 1, 0, &len);
      put_u32(img + sizeof(struct s0_header) + 0, (uint32_t)len + 0x1000u);
      reseal(img, 1);                                /* a VALID table that points outside */
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EREAD, "out-of-image src_offset not rejected"); }

    printf("test: image too short to hold the entry table -> S0_EREAD\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &len);
      use(img, (long)sizeof(struct s0_header) + 8);
      CHECK(s0_load(&BE, &res) == S0_EREAD, "entry-table truncation not rejected"); }

    printf("test: header truncated (< 32 B) -> S0_EREAD\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 1, 0x80000000u, 0, 0, 0, r3, 1, 0, &len);
      use(img, (long)sizeof(struct s0_header) - 1);
      CHECK(s0_load(&BE, &res) == S0_EREAD, "sub-header image not rejected"); }

    printf("test: check ORDER -- bad version wins over bad header CRC\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION + 1u, 3, 0x80000000u, 0, 0, 0, r3, 3, 1, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EVERSION, "version must be checked before header CRC"); }

    printf("test: check ORDER -- bad num_entries wins over a bad table CRC\n"
           "      (the range sets how many entries the CRC covers, so it comes first)\n");
    { uint8_t *img = pack(S0_MAGIC, S0_VERSION, 99, 0x80000000u, 0, 0, 0, r3, 3, 1, &len);
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_ENENT, "num_entries must be range-checked before the table CRC"); }

    printf("test: v2 -- the table CRC binds EVERY entry field (src/dst/len/crc) and\n"
           "      num_entries itself: any flip is S0_EHDRCRC before a payload byte moves\n");
    { static const char *const fld[4] = { "src_offset", "dst_addr", "len", "crc32" };
      for (uint32_t e = 0; e < 3u; e++) {
          for (uint32_t f = 0; f < 4u; f++) {
              uint8_t *img = pack(S0_MAGIC, S0_VERSION, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &len);
              img[sizeof(struct s0_header) + e * sizeof(struct s0_entry) + f * 4u] ^= 0x01;
              use(img, len);
              int rc = s0_load(&BE, &res);
              CHECK(rc == S0_EHDRCRC, "entry %u %s flip -> rc %d", e, fld[f], rc);
              CHECK(g_ddr[0] == 0 && g_ddr[1] == 0, "nothing copied before the table was trusted");
          }
      }
      uint8_t *img = pack(S0_MAGIC, S0_VERSION, 3, 0x80000000u, 0, 0x82200000u, 0, r3, 3, 0, &len);
      put_u32(img + 8, 2u);                          /* drop the 3rd entry from the count */
      use(img, len);
      CHECK(s0_load(&BE, &res) == S0_EHDRCRC, "a changed num_entries is caught");
      OK("12 entry-field flips + a num_entries change all refused"); }

    printf("test: v2 -- header_crc32 is the image identity: same pc/a0/a1, different\n"
           "      payload -> different header_crc32 (IMAGE's 0x0a4da20b collision)\n");
    { uint8_t *qa = mkbuf(4096, 41), *qb = mkbuf(4096, 42);
      struct region ra = { 0x80000000u, 4096, qa }, rb = { 0x80000000u, 4096, qb };
      long la, lb;
      uint8_t *ia = pack(S0_MAGIC, S0_VERSION, 1, 0x80000000u, 0, 0, 0, &ra, 1, 0, &la);
      uint8_t *ib = pack(S0_MAGIC, S0_VERSION, 1, 0x80000000u, 0, 0, 0, &rb, 1, 0, &lb);
      uint32_t ha, hb;
      memcpy(&ha, ia + 28, 4);
      memcpy(&hb, ib + 28, 4);
      CHECK(ha != hb, "two payloads, one header_crc32 0x%08X", ha);
      uint8_t *ic = malloc((size_t)la);
      memcpy(ic, ia, (size_t)la);
      use(ib, lb);
      CHECK(s0_load(&BE, &res) == S0_OK && res.header_crc32 == hb, "B loads, reports B's id");
      /* A's table over B's payload: a stale table with a swapped-in payload */
      memcpy(ic + 32 + 16, qb, 4096);
      use(ic, la);
      CHECK(s0_load(&BE, &res) == S0_ECRC, "A's table + B's payload refused");
      free(ia); free(qa); free(qb);
      OK("ids 0x%08X != 0x%08X; a swapped payload is S0_ECRC", ha, hb); }

    free(p0); free(p1); free(p2);
    free(g_img); free(g_ddr);

    if (fails) { printf("\nRESULT: %d edge CHECK(s) FAILED\n", fails); return 1; }
    printf("\nRESULT: all stage0 edge-case checks PASSED\n");
    return 0;
}
