/*
 * slot_card.c — the ONE card layer for stage0's user-microSD layout. See
 * slot_card.h for why it exists and who links it (mps3-harnessd AND mps3-slot).
 */
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif
#ifndef _FILE_OFFSET_BITS
#define _FILE_OFFSET_BITS 64
#endif
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#ifdef __linux__
#include <linux/fs.h>               /* BLKFLSBUF, BLKGETSIZE64 */
#endif

#include "slot_card.h"

/* The test seam (slot_card.h): the four calls that touch the card. A plain
 * -Dpread= cannot work: glibc redirects pread to pread64 by asm name. */
#ifndef SLOT_PREAD
#define SLOT_PREAD   pread
#define SLOT_PWRITE  pwrite
#define SLOT_FSYNC   fsync
#define SLOT_FADVISE posix_fadvise
#endif

#define BLK SLOT_CARD_BLK

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

/* ---- card I/O ---------------------------------------------------------------- */
int slot_card_rd(int fd, uint64_t off, void *buf, size_t n)
{
    size_t got = 0;
    while (got < n) {
        ssize_t r = SLOT_PREAD(fd, (uint8_t *)buf + got, n - got, (off_t)(off + got));
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0)
            return -1;
        got += (size_t)r;
    }
    return 0;
}

int slot_card_wr(int fd, uint64_t off, const void *buf, size_t n, uint64_t lo, uint64_t hi)
{
    if (off < lo || off > hi || n > hi - off) {
        errno = EPERM;
        return -2;   /* the guard: never issued */
    }
    size_t put = 0;
    while (put < n) {
        ssize_t r = SLOT_PWRITE(fd, (const uint8_t *)buf + put, n - put, (off_t)(off + put));
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0)
            return -1;
        put += (size_t)r;
    }
    return 0;
}

int slot_card_flush_uncache(int fd, uint64_t off, uint64_t len)
{
    if (SLOT_FSYNC(fd) != 0)
        return -1;
#ifndef MPS3_SLOT_NO_UNCACHE        /* the tests build the pre-fix variant as a control */
    struct stat st;
    if (fstat(fd, &st) == 0 && S_ISBLK(st.st_mode)) {
#ifdef BLKFLSBUF
        (void)ioctl(fd, BLKFLSBUF, 0);
#endif
    }
    (void)SLOT_FADVISE(fd, (off_t)off, (off_t)len, POSIX_FADV_DONTNEED);
#else
    (void)off;
    (void)len;
#endif
    return 0;
}

uint64_t slot_card_size(int fd)
{
    struct stat st;
    if (fstat(fd, &st) != 0)
        return 0;
#ifdef BLKGETSIZE64
    if (S_ISBLK(st.st_mode)) {
        uint64_t sz = 0;
        return ioctl(fd, BLKGETSIZE64, &sz) == 0 ? sz : 0;
    }
#endif
    return (uint64_t)st.st_size;   /* a file-backed card (host tests) */
}

/* ---- the card as stage0 sees it -------------------------------------------------- */

/* One copy on its own, with stage0's rules: s0_bootcfg_pick() against an all-zero
 * partner answers for the copy alone, except that "invalid" and "valid, default
 * A, seq 0" both come back as (1, 0) -- told apart by the magic/version/CRC. */
static int bc_one(const uint8_t c[BLK], uint32_t *seq)
{
    static const uint8_t zero[BLK];
    uint32_t s = 0;
    uint32_t d = s0_bootcfg_pick(c, zero, &s);
    if (d == 1u && s == 0u &&
        (le32(c) != S0_BOOTCFG_MAGIC || le32(c + 4) != S0_BOOTCFG_VERSION ||
         le32(c + 12) != 1u || le32(c + S0_BOOTCFG_CRC_OFF) != s0_crc32(c, S0_BOOTCFG_CRC_OFF)))
        return 0;
    *seq = s;
    return 1;
}

/* THE OVERLAP GUARD (slot_card.h). `lo`/`n` is stage0's slot `self` (MBR entry
 * index `self`). "" when no other typed, non-empty entry shares a block with it
 * and it starts at or above SLOT_CARD_FIRST_LBA; else the reason, <= 23 chars
 * (the wire's slot err). 64-bit ranges: a start+count that wraps 32 bits still
 * overlaps. */
static void slot_overlap(const uint8_t mbr[BLK], int self, uint32_t lo, uint32_t n,
                         char why[SLOT_CARD_WHY_MAX])
{
    uint64_t a = lo, b = (uint64_t)lo + n;
    why[0] = '\0';
    if (lo < SLOT_CARD_FIRST_LBA) {
        snprintf(why, SLOT_CARD_WHY_MAX, "overlaps LBA 0-2");
        return;
    }
    for (int j = 0; j < 4; j++) {
        const uint8_t *e = mbr + S0_MBR_PART_OFF + 16u * (unsigned)j;
        uint64_t oa = le32(e + 8), ob = oa + le32(e + 12);
        if (j == self || e[4] == 0u || ob <= oa || !(oa < b && a < ob))
            continue;
        if (e[4] == 0xDAu)
            snprintf(why, SLOT_CARD_WHY_MAX, "overlaps the 0xDA store");
        else if (j < (int)S0_NUM_SLOTS && e[4] == S0_PART_TYPE_SLOT)
            snprintf(why, SLOT_CARD_WHY_MAX, "overlaps slot %c", 'A' + j);
        else
            snprintf(why, SLOT_CARD_WHY_MAX, "overlaps entry %d (0x%02X)", j + 1, e[4]);
        return;
    }
}

int slot_card_read(int fd, slot_card_t *c)
{
    uint8_t mbr[BLK];
    memset(c, 0, sizeof(*c));
    if (slot_card_rd(fd, 0, mbr, BLK) ||
        slot_card_rd(fd, (uint64_t)S0_BOOTCFG_LBA0 * BLK, c->bc[0], BLK) ||
        slot_card_rd(fd, (uint64_t)S0_BOOTCFG_LBA1 * BLK, c->bc[1], BLK))
        return -1;
    c->bytes = slot_card_size(fd);
    uint64_t blocks = c->bytes / BLK;
    int mbr_ok = s0_mbr_parse(mbr, blocks > 0xFFFFFFFFu ? 0u : (uint32_t)blocks,
                              c->mbr_slot) == 0;
    for (int i = 0; i < (int)S0_NUM_SLOTS; i++) {
        c->slot[i] = c->mbr_slot[i];
        if (c->mbr_slot[i].first_lba == 0u)
            continue;                                   /* absent: nothing to guard */
        slot_overlap(mbr, i, c->mbr_slot[i].first_lba, c->mbr_slot[i].nblocks, c->bad[i]);
        if (c->bad[i][0]) {
            c->slot[i].first_lba = 0u;                  /* BAD: no writer gets a range */
            c->slot[i].nblocks = 0u;
        }
    }
    if (mbr_ok) {
        for (int j = 0; j < 4 && !c->bootsel_bad[0]; j++) {
            const uint8_t *e = mbr + S0_MBR_PART_OFF + 16u * (unsigned)j;
            uint64_t oa = le32(e + 8), ob = oa + le32(e + 12);
            if (e[4] != 0u && ob > oa && oa <= S0_BOOTCFG_LBA1 && ob > S0_BOOTCFG_LBA0)
                snprintf(c->bootsel_bad, sizeof(c->bootsel_bad),
                         "LBA 1-2 inside MBR entry %d (0x%02X)", j + 1, e[4]);
        }
    }
    c->deflt = s0_bootcfg_pick(c->bc[0], c->bc[1], &c->seq);
    for (int i = 0; i < 2; i++) {
        c->seqs[i] = 0;
        c->valid[i] = bc_one(c->bc[i], &c->seqs[i]);
    }
    /* s0_bootcfg_pick(): copy 0 wins a tie */
    c->winner = (c->valid[0] && (!c->valid[1] || c->seqs[0] >= c->seqs[1])) ? 0
              : c->valid[1] ? 1 : -1;
    if (c->winner >= 0 && (le32(c->bc[c->winner] + 12) != c->deflt ||
                           c->seqs[c->winner] != c->seq))
        return -1;   /* this file's winner rule and stage0's pick disagree: refuse to act */
    return mbr_ok ? 0 : -2;
}

int slot_card_bootsel_write(int fd, slot_card_t *c, int idx, unsigned *lba,
                            uint32_t *got_deflt, uint32_t *got_seq)
{
    /* never the copy stage0 is using: the other one (lower seq; on a tie the one
     * stage0 did NOT pick; neither valid -> LBA 1) */
    unsigned victim = c->winner == 0 ? 1u : 0u;
    uint32_t nseq = c->seq + 1u;
    uint8_t b[BLK];
    memset(b, 0, sizeof(b));
    put32(b, S0_BOOTCFG_MAGIC);
    put32(b + 4, S0_BOOTCFG_VERSION);
    put32(b + 8, nseq);
    put32(b + 12, (uint32_t)idx + 1u);
    put32(b + S0_BOOTCFG_CRC_OFF, s0_crc32(b, S0_BOOTCFG_CRC_OFF));
    uint64_t lo = (uint64_t)S0_BOOTCFG_LBA0 * BLK, hi = (uint64_t)(S0_BOOTCFG_LBA1 + 1u) * BLK;
    if (lba)
        *lba = S0_BOOTCFG_LBA0 + victim;
    /* THE OVERLAP GUARD: LBA 1-2 inside a typed partition are someone else's
     * data, and a BAD slot never becomes the default. Nothing is written. */
    if (c->bootsel_bad[0] || idx < 0 || idx >= (int)S0_NUM_SLOTS || c->bad[idx][0]) {
        errno = EPERM;
        return -2;
    }
    if (slot_card_wr(fd, (uint64_t)(S0_BOOTCFG_LBA0 + victim) * BLK, b, BLK, lo, hi) != 0 ||
        slot_card_flush_uncache(fd, lo, hi - lo) != 0)
        return -1;
    slot_card_t back;
    int rc = slot_card_read(fd, &back);
    if (got_deflt)
        *got_deflt = back.deflt;
    if (got_seq)
        *got_seq = back.seq;
    if ((rc != 0 && rc != -2) || back.deflt != (uint32_t)idx + 1u || back.seq != nseq ||
        back.winner != (int)victim)
        return -3;
    *c = back;
    return 0;
}

/* ---- stage0's loader as the image checker ------------------------------------------ */
struct chk {
    const slot_card_src_t *src;
    uint8_t *scratch;
    size_t   cap;
};

static int chk_read(uint32_t off, void *dst, uint32_t len, void *ctx)
{
    struct chk *k = ctx;
    const slot_card_src_t *s = k->src;
    if ((uint64_t)off + len > s->size)
        return -1;                          /* stage0's truncation rule */
    if (s->mem) {
        memcpy(dst, s->mem + off, len);
        return 0;
    }
    return slot_card_rd(s->fd, s->base + off, dst, len);
}

static void *chk_ptr(uint32_t dst, uint32_t len, void *ctx)
{
    struct chk *k = ctx;
    if (!s0_region_in_bounds(SLOT_CARD_DDR_BASE, SLOT_CARD_DDR_SIZE, dst, len))
        return NULL;
    if (len > k->cap) {
        uint8_t *nb = realloc(k->scratch, len ? len : 1u);
        if (!nb)
            return NULL;
        k->scratch = nb;
        k->cap = len;
    }
    return k->scratch;
}

const char *slot_card_s0_check(const slot_card_src_t *src, uint32_t *hdr_crc)
{
    struct chk k = { src, NULL, 0 };
    struct s0_backend be = { chk_read, chk_ptr, &k };
    struct s0_result r;
    int rc = s0_load(&be, &r);
    free(k.scratch);
    if (rc != S0_OK)
        return s0_strerror(rc);
    *hdr_crc = r.header_crc32;
    return NULL;
}
