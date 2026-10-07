/*
 * stage0_core.c -- portable first-stage loader core. NO MMIO, NO libc, NO
 * physical addresses hard-coded. Compiles BOTH freestanding for the rv32 target
 * and hosted for the unit test, from one source, so the pack/parse/CRC/load
 * contract is proven on the host and the exact same bytes run on silicon.
 */
#include "stage0_boot.h"

/* CRC32 (reflected 0xEDB88320). Table-driven, with the 1 KiB table built on
 * first use into .bss rather than stored in .rodata.
 *
 * WHY A TABLE NOW: the loader used to be table-less to save 1 KiB, when it
 * was a 1.7 KiB image. It now CRCs a 22 MB slot at every boot and again after
 * every TFTP push, and the bitwise form costs ~45 cycles/byte on the MBV --
 * ~10 s of boot time at 100 MHz. The table form is ~8 cycles/byte. The LMB has
 * 48+ KiB to spare, the table costs nothing in the baked image (.bss), and it
 * is rebuilt after every reset because crt0 zeroes .bss (s_crc_ready too), so
 * a warm restart can never run with a half-built table. */
static uint32_t s_crc_tab[256];
static uint32_t s_crc_ready;

static void crc_build(void)
{
    for (uint32_t n = 0; n < 256u; ++n) {
        uint32_t c = n;
        for (int b = 0; b < 8; ++b)
            c = (c >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(c & 1u)));
        s_crc_tab[n] = c;
    }
    s_crc_ready = 1u;
}

uint32_t s0_crc32_update(uint32_t crc, const void *buf, uint32_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    if (!s_crc_ready)
        crc_build();
    crc ^= 0xFFFFFFFFu;
    for (uint32_t i = 0; i < len; ++i)
        crc = s_crc_tab[(crc ^ p[i]) & 0xFFu] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

uint32_t s0_crc32(const void *buf, uint32_t len)
{
    return s0_crc32_update(0u, buf, len);
}

/* Read exactly `len` bytes; fold a short/failed read into S0_EREAD. A positive
 * return is the backend's own S0_* verdict (a guard) and is passed through. */
static int read_all(const struct s0_backend *be, uint32_t off, void *dst, uint32_t len)
{
    int r = be->sto_read(off, dst, len, be->ctx);
    return r == 0 ? S0_OK : r > 0 ? r : S0_EREAD;
}

/* CRC a region where it landed, S0_CRC_CHUNK at a time, asking the backend
 * before each chunk (watchdog kick + DDR re-check on the target). */
static int crc_in_place(const struct s0_backend *be, s0_chunk_fn chunk, uint32_t region,
                        const uint8_t *p, uint32_t len, uint32_t *crc)
{
    uint32_t c = 0u;
    while (len != 0u) {
        uint32_t n = len < S0_CRC_CHUNK ? len : S0_CRC_CHUNK;
        if (chunk) {
            int g = chunk(be->ctx, region);
            if (g != 0)
                return g;
        }
        c = s0_crc32_update(c, p, n);
        p += n;
        len -= n;
    }
    *crc = c;
    return S0_OK;
}

int s0_load(const struct s0_backend *be, struct s0_result *out)
{
    return s0_load_guarded(be, 0, out);
}

int s0_load_guarded(const struct s0_backend *be, s0_chunk_fn chunk, struct s0_result *out)
{
    struct s0_header hdr;
    struct s0_entry ent[S0_MAX_ENTRIES];
    int rc = read_all(be, 0u, &hdr, (uint32_t)sizeof hdr);
    if (rc != S0_OK)
        return rc;

    if (hdr.magic != S0_MAGIC)
        return S0_EMAGIC;
    if (hdr.version != S0_VERSION)
        return S0_EVERSION;
    /* the range first: it sets how many entries the table CRC covers */
    if (hdr.num_entries == 0u || hdr.num_entries > S0_MAX_ENTRIES)
        return S0_ENENT;

    uint32_t tbl = hdr.num_entries * (uint32_t)sizeof ent[0];
    rc = read_all(be, S0_ENTRIES_OFFSET, ent, tbl);
    if (rc != S0_OK)
        return rc;

    /* table CRC: the header (header_crc32 zeroed) then every entry, so each
     * region's dst/len/payload-CRC is bound before a byte of it is read */
    uint32_t want = hdr.header_crc32;
    hdr.header_crc32 = 0u;
    uint32_t got = s0_crc32_update(s0_crc32(&hdr, (uint32_t)sizeof hdr), ent, tbl);
    if (got != want)
        return S0_EHDRCRC;

    for (uint32_t i = 0; i < hdr.num_entries; ++i) {
        void *dst = be->addr_to_ptr(ent[i].dst_addr, ent[i].len, be->ctx);
        if (dst == 0)
            return S0_EREAD;

        rc = read_all(be, ent[i].src_offset, dst, ent[i].len);
        if (rc != S0_OK)
            return rc;

        /* verify from the destination itself: on the target this is a real DDR
         * read-back, so it also exercises the path the CPU will fetch from. */
        uint32_t c;
        rc = crc_in_place(be, chunk, i, (const uint8_t *)dst, ent[i].len, &c);
        if (rc != S0_OK)
            return rc;
        if (c != ent[i].crc32)
            return S0_ECRC;
    }

    out->entry_pc = hdr.entry_pc;
    out->entry_a0 = hdr.entry_a0;
    out->entry_a1 = hdr.entry_a1;
    out->header_crc32 = want;
    return S0_OK;
}

/* In-memory source. Byte loop, not memcpy: this file links no libc, and the
 * target's copy loop is not the bottleneck (the CRC after it is). */
int s0_mem_read(uint32_t src_off, void *dst, uint32_t len, void *ctx)
{
    const struct s0_mem_src *m = (const struct s0_mem_src *)ctx;
    if (src_off > m->len || len > m->len - src_off)   /* overflow-safe */
        return -1;
    const uint8_t *s = m->img + src_off;
    uint8_t *d = (uint8_t *)dst;
    for (uint32_t i = 0; i < len; ++i)
        d[i] = s[i];
    return 0;
}

const char *s0_strerror(int rc)
{
    switch (rc) {
    case S0_OK:        return "ok";
    case S0_EMAGIC:    return "no boot table (magic)";
    case S0_EVERSION:  return "bad version";
    case S0_EHDRCRC:   return "table CRC";
    case S0_ENENT:     return "bad num_entries";
    case S0_EREAD:     return "read/truncated";
    case S0_ECRC:      return "payload CRC";
    case S0_ENOSLOT:   return "no slot";
    case S0_ENOTTRIED: return "not tried";
    case S0_ELIMIT:    return "boot limit";
    case S0_ETOOBIG:   return "image too large";
    case S0_EPROTO:    return "TFTP protocol error";
    case S0_ETIMEOUT:  return "push timed out";
    case S0_EDDR:      return "DDR calib lost";
    case S0_ESLOW:     return "slot load time limit";
    default:           return "unknown";
    }
}

static uint32_t le32_at(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

int s0_mbr_parse(const uint8_t sector[S0_BLOCK_BYTES], uint32_t card_blocks,
                 struct s0_slot out[S0_NUM_SLOTS])
{
    for (uint32_t i = 0; i < S0_NUM_SLOTS; ++i) {
        out[i].first_lba = 0u;
        out[i].nblocks = 0u;
    }
    if (sector[S0_MBR_SIG_OFF] != 0x55u || sector[S0_MBR_SIG_OFF + 1u] != 0xAAu)
        return -1;

    for (uint32_t i = 0; i < S0_NUM_SLOTS; ++i) {
        const uint8_t *e = sector + S0_MBR_PART_OFF + 16u * i;
        uint32_t lba = le32_at(e + 8);
        uint32_t n   = le32_at(e + 12);
        if (e[4] != S0_PART_TYPE_SLOT || lba == 0u || n == 0u)
            continue;
        if (card_blocks != 0u && !s0_region_in_bounds(0u, card_blocks, lba, n))
            continue;                    /* partition runs past the card */
        out[i].first_lba = lba;
        out[i].nblocks = n;
    }
    return 0;
}

/* One boot-select copy: 0 = invalid, else its default slot (1/2). */
static uint32_t bootcfg_one(const uint8_t *c, uint32_t *seq)
{
    if (le32_at(c) != S0_BOOTCFG_MAGIC || le32_at(c + 4) != S0_BOOTCFG_VERSION)
        return 0u;
    if (s0_crc32(c, S0_BOOTCFG_CRC_OFF) != le32_at(c + S0_BOOTCFG_CRC_OFF))
        return 0u;
    uint32_t d = le32_at(c + 12);
    if (d != 1u && d != 2u)
        return 0u;
    *seq = le32_at(c + 8);
    return d;
}

uint32_t s0_bootcfg_pick(const uint8_t c0[S0_BLOCK_BYTES],
                         const uint8_t c1[S0_BLOCK_BYTES], uint32_t *seq)
{
    uint32_t s0 = 0u, s1 = 0u;
    uint32_t d0 = bootcfg_one(c0, &s0);
    uint32_t d1 = bootcfg_one(c1, &s1);
    if (d0 && (!d1 || s0 >= s1)) {
        *seq = s0;
        return d0;
    }
    if (d1) {
        *seq = s1;
        return d1;
    }
    *seq = 0u;
    return 1u;
}
