/*
 * ovlstore_sd.c -- the overlay store on the user microSD. See ovlstore_sd.h for
 * the layout, the states, the per-poll budget and the API contract.
 *
 * SHAPE. One JOB at a time, advanced by ovlstore_sd_poll():
 *   PROBE   read LBA 0 (MBR), the two header copies, pick the newest valid one,
 *           apply the static_id gate, then hand over to VERIFY (internal)
 *   VERIFY  read the chosen slot's clearing and partial IN FULL and CRC them;
 *           on a mismatch try the other slot (which must pass the gate too)
 *   STREAM  re-read one region for a swap source, CRC it again, hand it out
 *   COMMIT  buffer fed bytes into blocks, write them to the target slot, read
 *           both regions back, then write the older header copy
 *   FORMAT  re-read LBA 0, apply rules (a)/(b)/(c), [MBR], two header copies
 *   CLEAR   one header copy with both slots invalid
 * PROBE/VERIFY are internal; the other four are "user" jobs whose result
 * ovlstore_sd_job_result() reports.
 *
 * THE ONE OP. At most one device op is in flight (the usd.c model). It carries
 * the job generation it was started under; a result that comes back after its
 * job ended (abort, card change, a failed step) is dropped, and the next job's
 * first start simply waits for it (op_start() returns BUSY while it runs).
 *
 * WRITE GUARD. Every write goes through write_allowed(): inside the 0xDA
 * partition only, and inside the region the running job owns (the target slot
 * for a commit, one header copy for commit/clear, the two copies for format;
 * LBA 0 only for format rule (b)'s MBR). A FOREIGN card never has a partition
 * recorded, so no write can start on it at all.
 */
#include <errno.h>
#include <stddef.h>
#include <string.h>

#include "ovlstore_sd.h"
#include "../common/crc32.h"

#define BLK OVLSD_BLOCK

_Static_assert(OVLSTORE_SD_OP_BLOCKS >= 1u, "OVLSTORE_SD_OP_BLOCKS must be >= 1");
/* The budget covers ALL CRC work in a poll, header copies included: the probe
 * checks both copies (2 x 78 bytes) in one poll, so that is the floor. */
_Static_assert(OVLSTORE_SD_CRC_BYTES_PER_POLL >= 2u * OVLSD_HDR_CRC_OFF,
               "OVLSTORE_SD_CRC_BYTES_PER_POLL must cover the probe's two header CRCs");
_Static_assert(OVLSD_HDR_USED_BYTES <= BLK, "header copy must fit one block");
_Static_assert(OVLSD_SLOT_A_LBA > OVLSD_HDR_COPY1_LBA &&
               OVLSD_SLOT_B_LBA >= OVLSD_SLOT_A_LBA + OVLSD_SLOT_BLOCKS, "slot layout");

enum { J_NONE = 0, J_PROBE, J_VERIFY, J_STREAM, J_COMMIT, J_FORMAT, J_CLEAR };

enum {
    PH_NONE = 0,
    PH_MBR, PH_SIG, PH_HDR0, PH_HDR1,                           /* probe  */
    PH_V_CLEAR, PH_V_PART,                                      /* verify */
    PH_STREAM,                                                  /* stream */
    PH_C_FEED, PH_C_FLUSH, PH_C_RB_CLEAR, PH_C_RB_PART, PH_C_HDR, /* commit */
    PH_F_READ, PH_F_SIG, PH_F_WIPE, PH_F_MBR, PH_F_HDR0, PH_F_HDR1, /* format */
    PH_CL_HDR                                                   /* clear  */
};

enum { B_FREE = 0, B_IO, B_CRC, B_READY, B_FILL, B_WPEND };
enum { IOK_NONE = 0, IOK_RR, IOK_WR, IOK_MISC };

#define SLOT_NONE 0xFFu

/* ---- small helpers --------------------------------------------------------- */

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint32_t blocks_of(uint32_t bytes)
{
    return (bytes / BLK) + (((bytes % BLK) != 0u) ? 1u : 0u);
}

static uint32_t slot_base(unsigned k)
{
    return (k != 0u) ? OVLSD_SLOT_B_LBA : OVLSD_SLOT_A_LBA;
}

static bool time_reached(uint32_t now, uint32_t t)
{
    return (int32_t)(now - t) >= 0;
}

static uint32_t opmax(const ovlstore_sd_t *s)
{
    uint32_t m = s->bd.max_blocks;
    if (m == 0u || m > OVLSTORE_SD_OP_BLOCKS) {
        m = OVLSTORE_SD_OP_BLOCKS;
    }
    return m;
}

static int map_io(int st)
{
    return (st == -ENODEV) ? OVLSD_EGONE : OVLSD_EIO;
}

static bool user_job(uint8_t j)
{
    return j == J_STREAM || j == J_COMMIT || j == J_FORMAT || j == J_CLEAR;
}

/* ---- descriptors ------------------------------------------------------------- */

/* A region inside an 8 MiB slot: block-aligned start, word-multiple length,
 * padded end still inside the slot. */
static bool region_ok(uint32_t off, uint32_t len)
{
    return len > 0u && (len & 3u) == 0u && (off % BLK) == 0u &&
           off <= OVLSD_SLOT_BYTES && len <= OVLSD_SLOT_BYTES - off;
}

/* The header CRC protects against corruption, not against a writer bug or a
 * hand-crafted card: check every descriptor before its offsets are used. */
static bool desc_sane(const ovlstore_slot_desc_t *d)
{
    uint32_t ce, pe;
    if (d->valid != 1u || !region_ok(d->clear_off, d->clear_len) ||
        !region_ok(d->part_off, d->part_len)) {
        return false;
    }
    ce = d->clear_off + blocks_of(d->clear_len) * BLK;
    pe = d->part_off + blocks_of(d->part_len) * BLK;
    return ce <= d->part_off || pe <= d->clear_off;
}

/* ---- MBR ---------------------------------------------------------------------- */

/* A FAT/exFAT/NTFS volume boot sector written straight onto the card (a
 * "superfloppy"): its boot code sits where an MBR has entries. Checked with or
 * without 0x55AA. The jump byte alone is not enough (GRUB's MBR starts EB 63 90). */
static bool looks_like_boot_sector(const uint8_t *b)
{
    bool jump = (b[0] == 0xEBu && b[2] == 0x90u) || b[0] == 0xE9u;
    if (!jump) {
        return false;
    }
    return memcmp(b + 3, "EXFAT   ", 8) == 0 || memcmp(b + 3, "NTFS    ", 8) == 0 ||
           memcmp(b + 0x36, "FAT", 3) == 0 || memcmp(b + 0x52, "FAT", 3) == 0;
}

/* The 0xDA entry `self` [start, start+cnt) against every OTHER typed, non-empty
 * entry. Any shared block means a store write would land in someone else's
 * partition (stage0's slots, /persist, a second 0xDA): refused as DA_BAD. The
 * compare is 64-bit, so an entry whose start+count wraps 32 bits still overlaps. */
static bool da_overlaps(const uint8_t *b, unsigned self, uint32_t start, uint32_t cnt)
{
    uint64_t lo = start, hi = (uint64_t)start + cnt;
    for (unsigned j = 0; j < 4u; j++) {
        const uint8_t *o = b + 446u + 16u * j;
        uint64_t olo = get32(o + 8), ohi = olo + get32(o + 12);
        if (j != self && o[4] != 0x00u && ohi > olo && olo < hi && lo < ohi) {
            return true;
        }
    }
    return false;
}

ovlsd_mbr_kind_t ovlstore_sd_mbr_classify(const uint8_t lba0[512], uint32_t nblocks,
                                          uint32_t *part_lba, uint32_t *part_blocks)
{
    const uint8_t *b = lba0;
    bool all00 = true, any_byte = false, any_type = false;
    unsigned i;

    if (part_lba != NULL) {
        *part_lba = 0u;
    }
    if (part_blocks != NULL) {
        *part_blocks = 0u;
    }
    for (i = 0; i < BLK; i++) {
        if (b[i] != 0x00u) {
            all00 = false;
            break;
        }
    }
    if (all00) {
        return OVLSD_MBR_BLANK;              /* only all-ZERO; all-0xFF is not blank */
    }
    if (looks_like_boot_sector(b)) {
        return OVLSD_MBR_FS;
    }
    if (b[510] != 0x55u || b[511] != 0xAAu) {
        return OVLSD_MBR_UNKNOWN;
    }
    for (i = 0; i < 4u; i++) {
        const uint8_t *e = b + 446u + 16u * i;
        if (e[0] != 0x00u && e[0] != 0x80u) {
            return OVLSD_MBR_UNKNOWN;        /* not a partition table */
        }
        if (e[4] != 0x00u) {
            any_type = true;
        }
        for (unsigned j = 0; j < 16u; j++) {
            if (e[j] != 0x00u) {
                any_byte = true;
            }
        }
    }
    if (!any_byte) {
        return OVLSD_MBR_EMPTY;              /* signed, four all-zero entries */
    }
    if (!any_type) {
        return OVLSD_MBR_UNKNOWN;            /* entry bytes without a type: not "no entries" */
    }
    for (i = 0; i < 4u; i++) {
        const uint8_t *e = b + 446u + 16u * i;
        if (e[4] == OVLSD_MBR_TYPE) {        /* the FIRST 0xDA entry is the store */
            uint32_t start = get32(e + 8), cnt = get32(e + 12);
            if (start < OVLSD_MBR_FIRST_LBA || cnt == 0u || start >= nblocks ||
                cnt > nblocks - start || da_overlaps(b, i, start, cnt)) {
                return OVLSD_MBR_DA_BAD;
            }
            if (cnt < OVLSD_MIN_PART_BLOCKS) {
                return OVLSD_MBR_DA_SMALL;
            }
            if (part_lba != NULL) {
                *part_lba = start;
            }
            if (part_blocks != NULL) {
                *part_blocks = cnt;
            }
            return OVLSD_MBR_DA;
        }
    }
    return OVLSD_MBR_OTHER;
}

/* handover §12.3. Evaluated on the probe's reading AND on a fresh read. */
static int format_rule(const ovlstore_sd_t *s, ovlsd_mbr_kind_t k)
{
    switch (k) {
    case OVLSD_MBR_DA:
        return OVLSD_OK;                                     /* (a) */
    case OVLSD_MBR_RAW:
        return (s->nblocks >= OVLSD_MIN_PART_BLOCKS) ? OVLSD_OK : OVLSD_ESMALL;
    case OVLSD_MBR_BLANK:
    case OVLSD_MBR_EMPTY:                                    /* (b) */
        return (s->nblocks >= OVLSD_FORMAT_PART_BLOCKS + OVLSD_ALIGN_BLOCKS) ? OVLSD_OK
                                                                            : OVLSD_ESMALL;
    case OVLSD_MBR_DA_SMALL:
        return OVLSD_ESMALL;
    case OVLSD_MBR_DA_BAD:
        return OVLSD_EPART;
    case OVLSD_MBR_OTHER:
        return OVLSD_EEXIST;                                 /* (c) */
    case OVLSD_MBR_FS:
    case OVLSD_MBR_GPT:
        return OVLSD_EFSSIG;                                 /* not blank */
    default:
        return OVLSD_EUNKNOWN;
    }
}

/* Rule (b): a single 0xDA entry in entry 4 over the last 32 MiB, start aligned
 * DOWN to 1 MiB so the entry reaches the last block. An existing empty MBR
 * keeps its boot code and disk signature. Caller checked format_rule(). */
static void mbr_build(uint8_t *out, const uint8_t *old, ovlsd_mbr_kind_t k, uint32_t nblocks,
                      uint32_t *plba, uint32_t *pcnt)
{
    uint32_t start = (nblocks - OVLSD_FORMAT_PART_BLOCKS) & ~(OVLSD_ALIGN_BLOCKS - 1u);
    uint32_t cnt = nblocks - start;
    uint8_t *e = out + 446u + 16u * OVLSD_MBR_FORMAT_ENTRY;

    if (k == OVLSD_MBR_EMPTY) {
        memcpy(out, old, BLK);
    } else {
        memset(out, 0, BLK);
        put32(out + 440u, OVLSD_MBR_DISK_SIGNATURE);
    }
    memset(e, 0, 16u);
    e[1] = 0xFEu; e[2] = 0xFFu; e[3] = 0xFFu;   /* CHS start: past CHS range, use LBA */
    e[4] = OVLSD_MBR_TYPE;
    e[5] = 0xFEu; e[6] = 0xFFu; e[7] = 0xFFu;   /* CHS end */
    put32(e + 8, start);
    put32(e + 12, cnt);
    out[510] = 0x55u;
    out[511] = 0xAAu;
    *plba = start;
    *pcnt = cnt;
}

/* ---- header copies --------------------------------------------------------------- */

static bool hdr_parse(const uint8_t *b, ovlstore_header_t *h, uint32_t *seq)
{
    if (get32(b + OVLSD_HDR_CRC_OFF) != mps3_crc32(b, OVLSD_HDR_CRC_OFF)) {
        return false;
    }
    if (ovlstore_header_unpack(b, OVLSTORE_HEADER_PACKED_SIZE, h) != OVLSTORE_OK) {
        return false;
    }
    if (h->active_slot > 1u) {
        return false;
    }
    *seq = get32(b + OVLSD_HDR_SEQ_OFF);
    return true;
}

static void hdr_build(ovlstore_sd_t *s, uint8_t *b, const ovlstore_header_t *h, uint32_t seq)
{
    memset(b, 0, BLK);
    (void)ovlstore_header_pack(h, b);
    put32(b + OVLSD_HDR_SEQ_OFF, seq);
    put32(b + OVLSD_HDR_CRC_OFF, mps3_crc32(b, OVLSD_HDR_CRC_OFF));
    s->poll_crc += OVLSD_HDR_CRC_OFF;
}

static void hdr_empty(ovlstore_header_t *h)
{
    memset(h, 0, sizeof *h);
    memcpy(h->magic, OVLSTORE_MAGIC, 4);
    h->ver = OVLSTORE_VER;
}

/* ---- jobs ------------------------------------------------------------------------- */

static void bufs_reset(ovlstore_sd_t *s)
{
    memset(s->b, 0, sizeof s->b);    /* B_FREE */
    s->fill = s->crcx = s->drain = 0u;
}

static void job_start(ovlstore_sd_t *s, uint8_t job, uint8_t ph)
{
    s->job = job;
    s->ph = ph;
    s->sub = 0u;
    s->fmt_all = 0u;
    s->gen++;
    if (user_job(job)) {
        s->last_job = job;
        s->job_result = OVLSD_BUSY;
    }
    bufs_reset(s);
}

static void job_end(ovlstore_sd_t *s, int rc)
{
    if (user_job(s->job)) {
        s->job_result = rc;
    }
    s->job = J_NONE;
    s->ph = PH_NONE;
    s->sub = 0u;
    s->fmt_all = 0u;
    s->gen++;     /* an op still in flight now belongs to nobody */
    bufs_reset(s);
}

static void finish_state(ovlstore_sd_t *s, ovlstore_sd_state_t st)
{
    job_end(s, OVLSD_OK);
    s->state = st;
    s->tries = 0u;
}

static void probe_start(ovlstore_sd_t *s)
{
    s->state = OVLSD_INIT;
    s->err = 0;
    s->retry_pending = 0u;
    s->nblocks = s->bd.ops->nblocks(s->bd.ctx);
    s->hdr_known = 0u;
    s->chosen = SLOT_NONE;
    s->fallback = 0u;
    if (s->cfg.raw_partition) {
        s->mbr = OVLSD_MBR_RAW;
        s->part_lba = 0u;
        s->part_blocks = s->nblocks;
        if (s->nblocks < OVLSD_MIN_PART_BLOCKS) {
            s->part_blocks = 0u;
            s->state = OVLSD_FOREIGN;
            return;
        }
        job_start(s, J_PROBE, PH_HDR0);
    } else {
        s->mbr = OVLSD_MBR_NOT_READ;
        s->part_lba = 0u;
        s->part_blocks = 0u;
        job_start(s, J_PROBE, PH_MBR);
    }
}

/* A store read failed on a READY device: ERR 30, auto-retry a few times. */
static void store_error(ovlstore_sd_t *s, int rc)
{
    job_end(s, rc);
    s->state = OVLSD_ERROR;
    s->err = OVLSD_ERRNUM_READ;
    s->tries++;
    s->retry_pending = (s->tries < OVLSTORE_SD_TRIES) ? 1u : 0u;
    s->t_retry = s->now + OVLSTORE_SD_RETRY_MS;
}

/* ---- the one device op ------------------------------------------------------------ */

static bool write_allowed(const ovlstore_sd_t *s, uint32_t lba, uint32_t n)
{
    uint32_t rel;

    if (s->job == J_FORMAT) {
        switch (s->ph) {
        case PH_F_MBR:              /* rule (b), or the wipe: LBA 0 only */
            return !s->cfg.raw_partition && lba == 0u && n == 1u;
        case PH_F_WIPE:             /* "erase-all" only: LBA 1..OVLSD_WIPE_LAST_LBA */
            return s->fmt_all && !s->cfg.raw_partition && lba >= 1u &&
                   lba <= OVLSD_WIPE_LAST_LBA && n >= 1u && n <= OVLSD_WIPE_LAST_LBA + 1u - lba;
        case PH_F_HDR0:
        case PH_F_HDR1:
            break;                  /* one header copy, checked below */
        default:
            return false;           /* reading / checking: nothing */
        }
    }
    if (s->part_blocks == 0u || lba < s->part_lba) {
        return false;
    }
    rel = lba - s->part_lba;
    if (rel >= s->part_blocks || n > s->part_blocks - rel) {
        return false;
    }
    switch (s->job) {
    case J_COMMIT:
        if (s->ph == PH_C_HDR) {
            return n == 1u && rel == (uint32_t)(s->hdr_newest ^ 1u);
        }
        return rel >= slot_base(s->ctarget) &&
               rel - slot_base(s->ctarget) + n <= OVLSD_SLOT_BLOCKS;
    case J_CLEAR:
        return n == 1u && rel == (uint32_t)(s->hdr_newest ^ 1u);
    case J_FORMAT:
        return n == 1u && rel == ((s->ph == PH_F_HDR0) ? OVLSD_HDR_COPY0_LBA : OVLSD_HDR_COPY1_LBA);
    default:
        return false;
    }
}

#ifdef OVLSTORE_SD_TEST_HOOKS
/* Host tests only: the guard is unreachable by correct code, so it is
 * exercised directly. Absent from every target build. */
bool ovlstore_sd_test_write_allowed(const ovlstore_sd_t *s, uint32_t lba, uint32_t n)
{
    return write_allowed(s, lba, n);
}
#endif

/* Start the op. OVLSD_BUSY = not now (one already in flight, one already
 * started this poll, or the device said -EBUSY); OVLSD_OK = started; negative
 * = refused. The ONLY place a device op is started. */
static int op_start(ovlstore_sd_t *s, bool wr, uint32_t lba, uint32_t n, uint8_t *buf,
                    uint8_t kind, uint8_t bi)
{
    int rc;

    /* `started` is the per-poll budget stated outright. Today io_inflight alone
     * already stops a second start (it only clears at the top of a poll); the
     * flag keeps the rule if a provider or a refactor ever changes that. */
    if (s->io_inflight || s->started) {
        return OVLSD_BUSY;
    }
    if (n == 0u || n > opmax(s)) {
        return OVLSD_EARG;
    }
    if (wr && !write_allowed(s, lba, n)) {
        return OVLSD_EARG;
    }
    rc = wr ? s->bd.ops->write_start(s->bd.ctx, lba, n, buf)
            : s->bd.ops->read_start(s->bd.ctx, lba, n, buf);
    if (rc == -EBUSY) {
        return OVLSD_BUSY;
    }
    if (rc != 0) {
        return map_io(rc);
    }
    s->io_inflight = 1u;
    s->io_kind = kind;
    s->io_buf = bi;
    s->io_gen = s->gen;
    s->started = 1u;
    s->poll_ops++;
    s->stats.ops_started++;
    if (wr) {
        s->stats.writes_started++;
        s->stats.blocks_written += n;
    } else {
        s->stats.reads_started++;
        s->stats.blocks_read += n;
    }
    if (n > s->stats.max_blocks_per_op) {
        s->stats.max_blocks_per_op = n;
    }
    return OVLSD_OK;
}

/* Write one block from mem[0], then read it back into mem[1] and compare.
 * The caller fills mem[0] only when sub == 0 and nothing is in flight. */
static int wv_step(ovlstore_sd_t *s, uint32_t lba)
{
    int rc;

    if (s->io_completed && s->io_kind == IOK_MISC) {
        s->io_completed = 0u;
        if (s->io_result != OVL_BDEV_IO_DONE) {
            return map_io(s->io_result);
        }
        if (s->sub == 1u) {
            s->sub = 2u;
        } else if (s->sub == 3u) {
            return (memcmp(s->mem[0], s->mem[1], BLK) == 0) ? OVLSD_OK : OVLSD_EVERIFY;
        }
    }
    if (s->sub == 0u || s->sub == 2u) {
        bool wr = (s->sub == 0u);
        rc = op_start(s, wr, lba, 1u, wr ? s->mem[0] : s->mem[1], IOK_MISC, wr ? 0u : 1u);
        if (rc == OVLSD_OK) {
            s->sub++;
        } else if (rc != OVLSD_BUSY) {
            return rc;
        }
    }
    return OVLSD_BUSY;
}

/* ---- the "truly blank" signature walk (probe and format) ---------------------------
 * LBA 0 alone cannot tell an empty card from a whole-disk filesystem: ext4 and
 * btrfs leave their first block(s) zero, and a GPT whose protective MBR was
 * cleared still has its header at LBA 1. Before rule (b) may write, these
 * signatures must all be absent. Sorted by LBA; each LBA is read once. A
 * false positive only ever refuses (the safe side); "erase-all" is the way past. */

typedef struct {
    uint32_t    lba;
    uint16_t    off;
    uint8_t     len;
    uint8_t     kind;        /* OVLSD_MBR_GPT or OVLSD_MBR_FS */
    const char *magic;
} fs_sig_t;

static const fs_sig_t k_sigs[] = {
    {   1u,   0u,  8u, (uint8_t)OVLSD_MBR_GPT, "EFI PART"         },  /* GPT header        */
    {   2u,   0u,  4u, (uint8_t)OVLSD_MBR_FS,  "\x10\x20\xF5\xF2" },  /* f2fs, byte 1024   */
    {   2u,  56u,  2u, (uint8_t)OVLSD_MBR_FS,  "\x53\xEF"         },  /* ext2/3/4, byte 1080 */
    {   7u, 502u, 10u, (uint8_t)OVLSD_MBR_FS,  "SWAPSPACE2"       },  /* Linux swap, 4 KiB */
    {  64u,   1u,  5u, (uint8_t)OVLSD_MBR_FS,  "CD001"            },  /* ISO 9660, 32769   */
    { 128u,  64u,  8u, (uint8_t)OVLSD_MBR_FS,  "_BHRfS_M"         },  /* btrfs, 65600      */
};
#define N_SIGS ((uint8_t)(sizeof k_sigs / sizeof k_sigs[0]))

/* One step: take a completed read and check every signature of that LBA,
 * then start the read of the next LBA. OVLSD_BUSY = going; OVLSD_OK = walk
 * over (*found = the kind that matched, or OVLSD_MBR_NOT_READ if clean);
 * negative = a read failed. At most one read started, one block compared. */
static int sig_step(ovlstore_sd_t *s, uint8_t bi, ovlsd_mbr_kind_t *found)
{
    int rc;

    *found = OVLSD_MBR_NOT_READ;
    if (s->io_completed && s->io_kind == IOK_MISC) {
        uint32_t lba = k_sigs[s->sig_i].lba;
        s->io_completed = 0u;
        if (s->io_result != OVL_BDEV_IO_DONE) {
            return map_io(s->io_result);
        }
        for (; s->sig_i < N_SIGS && k_sigs[s->sig_i].lba == lba; s->sig_i++) {
            const fs_sig_t *g = &k_sigs[s->sig_i];
            if (memcmp(s->mem[bi] + g->off, g->magic, g->len) == 0) {
                *found = (ovlsd_mbr_kind_t)g->kind;
                return OVLSD_OK;
            }
        }
    }
    if (s->sig_i < N_SIGS && k_sigs[s->sig_i].lba >= s->nblocks) {
        s->sig_i = N_SIGS;           /* sorted: nothing further fits on this card */
    }
    if (s->sig_i >= N_SIGS) {
        return OVLSD_OK;
    }
    rc = op_start(s, false, k_sigs[s->sig_i].lba, 1u, s->mem[bi], IOK_MISC, bi);
    return (rc < 0) ? rc : OVLSD_BUSY;
}

/* ---- the region reader (verify, stream, commit read-back) --------------------------
 * Two buffers. Per poll: take a completed read, CRC at most the per-poll byte
 * budget of the oldest unchecked buffer, start the next read into a free
 * buffer. In deliver mode (stream) a checked buffer becomes READY for
 * stream_next(); the region's LAST buffer only becomes READY if the running
 * CRC over the whole region matches. */

static void rr_setup(ovlstore_sd_t *s, uint32_t lba, uint32_t len, uint32_t expect,
                     uint8_t deliver)
{
    s->rr.lba = lba;
    s->rr.len = len;
    s->rr.nblk = blocks_of(len);
    s->rr.issued = 0u;
    s->rr.crc = MPS3_CRC32_INIT;
    s->rr.crcd = 0u;
    s->rr.expect = expect;
    s->rr.deliver = deliver;
    bufs_reset(s);
}

static void rr_setup_slot(ovlstore_sd_t *s, uint8_t k, ovlstore_sd_which_t w, uint8_t deliver)
{
    const ovlstore_slot_desc_t *d = &s->hdr.slot[k];
    bool c = (w == OVLSD_CLEARING);
    rr_setup(s, s->part_lba + slot_base(k) + (c ? d->clear_off : d->part_off) / BLK,
             c ? d->clear_len : d->part_len, c ? d->clear_crc : d->part_crc, deliver);
}

/* OVLSD_BUSY (going), OVLSD_OK (verify mode: whole region checked and good),
 * or a negative error (ECRC / EIO / EGONE). */
static int rr_step(ovlstore_sd_t *s)
{
    ovlsd_buf_t *b;
    int rc;

    if (s->io_completed && s->io_kind == IOK_RR) {
        s->io_completed = 0u;
        if (s->io_result != OVL_BDEV_IO_DONE) {
            return map_io(s->io_result);
        }
        b = &s->b[s->io_buf];
        b->st = B_CRC;
        b->pos = 0u;
    }

    b = &s->b[s->crcx];
    if (b->st == B_CRC) {
        uint32_t n = b->bytes - b->pos;
        uint32_t room = (s->poll_crc < OVLSTORE_SD_CRC_BYTES_PER_POLL)
                        ? OVLSTORE_SD_CRC_BYTES_PER_POLL - s->poll_crc : 0u;
        if (n > room) {
            n = room;
        }
        if (n > 0u) {
            s->rr.crc = mps3_crc32_update(s->rr.crc, s->mem[s->crcx] + b->pos, n);
            b->pos += n;
            s->rr.crcd += n;
            s->poll_crc += n;
            s->stats.crc_bytes += n;
        }
        if (b->pos == b->bytes) {
            bool last = (b->last != 0u);
            s->crcx ^= 1u;
            if (last && s->rr.crc != s->rr.expect) {
                return OVLSD_ECRC;           /* the last buffer is never handed out */
            }
            if (s->rr.deliver) {
                b->st = B_READY;
                b->pos = 0u;
            } else {
                b->st = B_FREE;
                if (last) {
                    return OVLSD_OK;
                }
            }
        }
    }

    if (s->rr.issued < s->rr.nblk && !s->io_inflight) {
        uint8_t bi = s->fill;
        b = &s->b[bi];
        if (b->st == B_FREE) {
            uint32_t n = s->rr.nblk - s->rr.issued;
            if (n > opmax(s)) {
                n = opmax(s);
            }
            rc = op_start(s, false, s->rr.lba + s->rr.issued, n, s->mem[bi], IOK_RR, bi);
            if (rc == OVLSD_OK) {
                uint32_t off = s->rr.issued * BLK;
                b->st = B_IO;
                b->nblk = n;
                b->pos = 0u;
                b->bytes = (s->rr.len - off < n * BLK) ? (s->rr.len - off) : n * BLK;
                s->rr.issued += n;
                b->last = (s->rr.issued == s->rr.nblk) ? 1u : 0u;
                s->fill ^= 1u;
            } else if (rc != OVLSD_BUSY) {
                return rc;
            }
        }
    }
    return OVLSD_BUSY;
}

/* ---- PROBE / VERIFY -------------------------------------------------------------- */

static void verify_start(ovlstore_sd_t *s, uint8_t k, uint8_t fb)
{
    job_start(s, J_VERIFY, PH_V_CLEAR);
    s->vslot = k;
    s->vfallback = fb;
    rr_setup_slot(s, k, OVLSD_CLEARING, 0u);
    s->state = OVLSD_INIT;
}

/* The other slot, if it is valid, sane and NOT stale, gets verified. */
static bool fallback_start(ovlstore_sd_t *s, uint8_t bad)
{
    uint8_t o = (uint8_t)(bad ^ 1u);
    const ovlstore_slot_desc_t *d = &s->hdr.slot[o];
    if (d->valid == 0u || !desc_sane(d) || d->static_id != s->cfg.live_static_id) {
        return false;
    }
    verify_start(s, o, 1u);
    return true;
}

static void probe_evaluate(ovlstore_sd_t *s)
{
    ovlstore_header_t h0, h1;
    uint32_t q0 = 0u, q1 = 0u;
    bool v0 = hdr_parse(s->mem[0], &h0, &q0);
    bool v1 = hdr_parse(s->mem[1], &h1, &q1);
    const ovlstore_slot_desc_t *d;
    uint8_t act;

    s->poll_crc += 2u * OVLSD_HDR_CRC_OFF;
    if (!v0 && !v1) {
        finish_state(s, OVLSD_FOREIGN);      /* a 0xDA partition nobody formatted */
        return;
    }
    if (v0 && (!v1 || (int32_t)(q0 - q1) >= 0)) {
        s->hdr = h0;
        s->hdr_seq = q0;
        s->hdr_newest = 0u;
    } else {
        s->hdr = h1;
        s->hdr_seq = q1;
        s->hdr_newest = 1u;
    }
    s->hdr_known = 1u;

    act = s->hdr.active_slot;
    d = &s->hdr.slot[act];
    if (d->valid == 0u) {
        finish_state(s, OVLSD_EMPTY);
        return;
    }
    if (desc_sane(d)) {
        if (d->static_id != s->cfg.live_static_id) {
            finish_state(s, OVLSD_STALE);    /* THE GATE: its bytes are never read */
            return;
        }
        verify_start(s, act, 0u);
        return;
    }
    if (!fallback_start(s, act)) {           /* a descriptor that cannot be right */
        finish_state(s, OVLSD_BAD);
    }
}

static void step_probe(ovlstore_sd_t *s)
{
    int rc = OVLSD_OK;

    if (s->ph == PH_SIG) {
        ovlsd_mbr_kind_t found;
        rc = sig_step(s, 1u, &found);
        if (rc == OVLSD_BUSY) {
            return;
        }
        if (rc < 0) {
            store_error(s, rc);
            return;
        }
        if (found != OVLSD_MBR_NOT_READ) {
            s->mbr = found;                  /* a filesystem / GPT: not blank */
        }
        finish_state(s, OVLSD_FOREIGN);      /* blank or not: no partition recorded */
        return;
    }
    if (s->io_completed && s->io_kind == IOK_MISC) {
        s->io_completed = 0u;
        if (s->io_result != OVL_BDEV_IO_DONE) {
            store_error(s, map_io(s->io_result));
            return;
        }
        switch (s->ph) {
        case PH_MBR: {
            uint32_t pl = 0u, pc = 0u;
            s->mbr = ovlstore_sd_mbr_classify(s->mem[0], s->nblocks, &pl, &pc);
            if (s->mbr == OVLSD_MBR_BLANK || s->mbr == OVLSD_MBR_EMPTY) {
                s->ph = PH_SIG;              /* blank only if nothing sits behind LBA 0 */
                s->sig_i = 0u;
                return;
            }
            if (s->mbr != OVLSD_MBR_DA) {
                finish_state(s, OVLSD_FOREIGN);   /* read-only: no partition recorded */
                return;
            }
            s->part_lba = pl;
            s->part_blocks = pc;
            s->ph = PH_HDR0;
            break;
        }
        case PH_HDR0:
            s->ph = PH_HDR1;
            break;
        default:
            probe_evaluate(s);
            return;
        }
    }
    switch (s->ph) {
    case PH_MBR:
        rc = op_start(s, false, 0u, 1u, s->mem[0], IOK_MISC, 0u);
        break;
    case PH_HDR0:
        rc = op_start(s, false, s->part_lba + OVLSD_HDR_COPY0_LBA, 1u, s->mem[0], IOK_MISC, 0u);
        break;
    case PH_HDR1:
        rc = op_start(s, false, s->part_lba + OVLSD_HDR_COPY1_LBA, 1u, s->mem[1], IOK_MISC, 1u);
        break;
    default:
        break;
    }
    if (rc < 0) {
        store_error(s, rc);
    }
}

static void step_verify(ovlstore_sd_t *s)
{
    int r = rr_step(s);

    if (r == OVLSD_BUSY) {
        return;
    }
    if (r == OVLSD_OK) {
        if (s->ph == PH_V_CLEAR) {
            s->ph = PH_V_PART;
            rr_setup_slot(s, s->vslot, OVLSD_PARTIAL, 0u);
            return;
        }
        finish_state(s, OVLSD_VALID);
        s->chosen = s->vslot;
        s->fallback = s->vfallback;
        return;
    }
    if (r == OVLSD_ECRC) {
        if (s->vfallback == 0u && fallback_start(s, s->vslot)) {
            return;
        }
        finish_state(s, OVLSD_BAD);
        return;
    }
    store_error(s, r);
}

/* ---- STREAM ------------------------------------------------------------------------ */

static void step_stream(ovlstore_sd_t *s)
{
    int r = rr_step(s);
    if (r == OVLSD_BUSY) {
        return;
    }
    /* deliver mode never returns OK: DONE is reached in stream_next(). */
    job_end(s, r);
    if (r != OVLSD_EGONE) {
        probe_start(s);   /* the card no longer reads back what was verified */
    }
}

/* ---- COMMIT ------------------------------------------------------------------------ */

static void commit_fail(ovlstore_sd_t *s, int rc)
{
    job_end(s, rc);
    probe_start(s);   /* the header was never touched: re-verify the old default */
}

static void pad_block(ovlstore_sd_t *s, uint8_t bi)
{
    ovlsd_buf_t *b = &s->b[bi];
    uint32_t r = b->bytes % BLK;
    if (r != 0u) {
        memset(s->mem[bi] + b->bytes, 0xFF, BLK - r);
        b->bytes += BLK - r;
    }
}

/* Queue the fill buffer for writing (its bytes are a whole number of blocks). */
static int seal(ovlstore_sd_t *s, uint8_t bi)
{
    ovlsd_buf_t *b = &s->b[bi];
    uint32_t n = b->bytes / BLK;

    if (n == 0u) {
        b->st = B_FREE;
        return OVLSD_OK;
    }
    if (n > s->cwend - s->cwlba) {
        return OVLSD_ETOOBIG;   /* unreachable after commit_begin's size check */
    }
    b->st = B_WPEND;
    b->lba = s->cwlba;
    b->nblk = n;
    s->cwlba += n;
    s->fill = (uint8_t)(bi ^ 1u);
    return OVLSD_OK;
}

static void commit_hdr_build(ovlstore_sd_t *s)
{
    ovlstore_slot_desc_t *d;

    s->hdr_pending = s->hdr;
    d = &s->hdr_pending.slot[s->ctarget];
    memset(d, 0, sizeof *d);
    d->static_id = s->cdesc.static_id;
    d->rm_id     = s->cdesc.rm_id;
    d->clear_off = 0u;
    d->clear_len = s->cdesc.clear_len;
    d->clear_crc = s->cdesc.clear_crc;
    d->part_off  = blocks_of(s->cdesc.clear_len) * BLK;
    d->part_len  = s->cdesc.part_len;
    d->part_crc  = s->cdesc.part_crc;
    d->valid     = 1u;
    s->hdr_pending.active_slot = s->ctarget;
    hdr_build(s, s->mem[0], &s->hdr_pending, s->hdr_seq + 1u);
}

static void step_commit(ovlstore_sd_t *s)
{
    int rc;

    switch (s->ph) {
    case PH_C_FEED:
    case PH_C_FLUSH: {
        ovlsd_buf_t *b;
        if (s->io_completed && s->io_kind == IOK_WR) {
            s->io_completed = 0u;
            if (s->io_result != OVL_BDEV_IO_DONE) {
                commit_fail(s, map_io(s->io_result));
                return;
            }
            s->b[s->io_buf].st = B_FREE;
            s->drain = (uint8_t)(s->io_buf ^ 1u);
        }
        b = &s->b[s->drain];
        if (!s->io_inflight && b->st == B_WPEND) {
            rc = op_start(s, true, b->lba, b->nblk, s->mem[s->drain], IOK_WR, s->drain);
            if (rc == OVLSD_OK) {
                b->st = B_IO;
            } else if (rc != OVLSD_BUSY) {
                commit_fail(s, rc);
                return;
            }
        }
        if (s->ph == PH_C_FLUSH && !s->io_inflight &&
            s->b[0].st == B_FREE && s->b[1].st == B_FREE) {
            s->ph = PH_C_RB_CLEAR;
            rr_setup(s, s->part_lba + slot_base(s->ctarget), s->cdesc.clear_len,
                     s->cdesc.clear_crc, 0u);
        }
        return;
    }
    case PH_C_RB_CLEAR:
    case PH_C_RB_PART:
        rc = rr_step(s);
        if (rc == OVLSD_BUSY) {
            return;
        }
        if (rc != OVLSD_OK) {
            commit_fail(s, (rc == OVLSD_ECRC) ? OVLSD_EVERIFY : rc);
            return;
        }
        if (s->ph == PH_C_RB_CLEAR) {
            s->ph = PH_C_RB_PART;
            rr_setup(s, s->part_lba + slot_base(s->ctarget) + blocks_of(s->cdesc.clear_len),
                     s->cdesc.part_len, s->cdesc.part_crc, 0u);
        } else {
            s->ph = PH_C_HDR;    /* the header is built next poll: one CRC job per poll */
            s->sub = 0u;
        }
        return;
    case PH_C_HDR:
        if (s->sub == 0u && !s->io_inflight) {
            commit_hdr_build(s);
        }
        rc = wv_step(s, s->part_lba + (uint32_t)(s->hdr_newest ^ 1u));
        if (rc == OVLSD_BUSY) {
            return;
        }
        if (rc != OVLSD_OK) {
            commit_fail(s, rc);
            return;
        }
        s->hdr = s->hdr_pending;
        s->hdr_seq++;
        s->hdr_newest ^= 1u;
        s->hdr_known = 1u;
        s->commit_slot = (s->ctarget != 0u) ? 'B' : 'A';
        s->skip = 0u;
        finish_state(s, OVLSD_VALID);
        s->chosen = s->ctarget;
        s->fallback = 0u;
        return;
    default:
        return;
    }
}

/* ---- FORMAT / CLEAR ---------------------------------------------------------------- */

static void format_fail(ovlstore_sd_t *s, int rc)
{
    job_end(s, rc);
    probe_start(s);   /* whatever landed, read the card again */
}

static void step_format(ovlstore_sd_t *s)
{
    int rc;

    switch (s->ph) {
    case PH_F_READ:
        if (s->cfg.raw_partition) {
            rc = format_rule(s, OVLSD_MBR_RAW);
            if (rc != OVLSD_OK) {
                format_fail(s, rc);
                return;
            }
            s->part_lba = 0u;
            s->part_blocks = s->nblocks;
            s->ph = PH_F_HDR0;
            s->sub = 0u;
            return;
        }
        if (s->io_completed && s->io_kind == IOK_MISC) {
            uint32_t pl = 0u, pc = 0u;
            ovlsd_mbr_kind_t k;
            s->io_completed = 0u;
            if (s->io_result != OVL_BDEV_IO_DONE) {
                format_fail(s, map_io(s->io_result));
                return;
            }
            /* THE SECOND CHECK: the rules against what is on the card NOW. */
            k = ovlstore_sd_mbr_classify(s->mem[1], s->nblocks, &pl, &pc);
            rc = format_rule(s, k);
            if (rc != OVLSD_OK) {
                format_fail(s, rc);          /* zero writes */
                return;
            }
            if (k == OVLSD_MBR_DA) {         /* (a): the partition stays as it is */
                s->mbr = k;
                s->part_lba = pl;
                s->part_blocks = pc;
                s->ph = PH_F_HDR0;
                s->sub = 0u;
                return;
            }
            s->fmt_kind = k;                 /* (b), once nothing sits behind LBA 0 */
            s->ph = PH_F_SIG;
            s->sig_i = 0u;
            return;
        }
        rc = op_start(s, false, 0u, 1u, s->mem[1], IOK_MISC, 1u);
        if (rc < 0) {
            format_fail(s, rc);
        }
        return;
    case PH_F_SIG: {                         /* mem[1] keeps LBA 0; reads go to mem[0] */
        ovlsd_mbr_kind_t found;
        rc = sig_step(s, 0u, &found);
        if (rc == OVLSD_BUSY) {
            return;
        }
        if (rc < 0) {
            format_fail(s, rc);
            return;
        }
        if (found != OVLSD_MBR_NOT_READ) {
            format_fail(s, OVLSD_EFSSIG);    /* zero writes */
            return;
        }
        mbr_build(s->mem[0], s->mem[1], s->fmt_kind, s->nblocks, &s->fmt_lba, &s->fmt_blocks);
        s->ph = PH_F_MBR;
        s->sub = 0u;
        return;
    }
    case PH_F_WIPE:                          /* "erase-all" only */
        if (s->io_completed && s->io_kind == IOK_WR) {
            s->io_completed = 0u;
            if (s->io_result != OVL_BDEV_IO_DONE) {
                format_fail(s, map_io(s->io_result));
                return;
            }
            s->fmt_next += s->fmt_n;
        }
        if (s->io_inflight) {
            return;
        }
        if (!s->fmt_filled) {
            /* Zero the source only now: nothing is in flight that could still
             * be reading card data into it. */
            memset(s->mem[0], 0, sizeof s->mem[0]);
            s->fmt_filled = 1u;
        }
        if (s->fmt_next > OVLSD_WIPE_LAST_LBA) {
            mbr_build(s->mem[0], s->mem[1], OVLSD_MBR_BLANK, s->nblocks,
                      &s->fmt_lba, &s->fmt_blocks);           /* fresh: 0xDA only */
            s->ph = PH_F_MBR;
            s->sub = 0u;
            return;
        }
        {
            uint32_t n = OVLSD_WIPE_LAST_LBA + 1u - s->fmt_next;
            if (n > opmax(s)) {
                n = opmax(s);
            }
            rc = op_start(s, true, s->fmt_next, n, s->mem[0], IOK_WR, 0u);
            if (rc == OVLSD_OK) {
                s->fmt_n = n;
            } else if (rc != OVLSD_BUSY) {
                format_fail(s, rc);
            }
        }
        return;
    case PH_F_MBR:
        rc = wv_step(s, 0u);
        if (rc == OVLSD_BUSY) {
            return;
        }
        if (rc != OVLSD_OK) {
            format_fail(s, rc);
            return;
        }
        s->mbr = OVLSD_MBR_DA;
        s->part_lba = s->fmt_lba;
        s->part_blocks = s->fmt_blocks;
        s->ph = PH_F_HDR0;
        s->sub = 0u;
        return;
    case PH_F_HDR0:
    case PH_F_HDR1:
        if (s->sub == 0u && !s->io_inflight) {
            ovlstore_header_t h;
            hdr_empty(&h);
            hdr_build(s, s->mem[0], &h, (s->ph == PH_F_HDR0) ? 1u : 2u);
        }
        rc = wv_step(s, s->part_lba + ((s->ph == PH_F_HDR0) ? OVLSD_HDR_COPY0_LBA
                                                             : OVLSD_HDR_COPY1_LBA));
        if (rc == OVLSD_BUSY) {
            return;
        }
        if (rc != OVLSD_OK) {
            format_fail(s, rc);
            return;
        }
        if (s->ph == PH_F_HDR0) {
            s->ph = PH_F_HDR1;
            s->sub = 0u;
            return;
        }
        s->skip = 0u;
        job_end(s, OVLSD_OK);
        probe_start(s);
        return;
    default:
        return;
    }
}

static void step_clear(ovlstore_sd_t *s)
{
    int rc;

    if (s->sub == 0u && !s->io_inflight) {
        s->hdr_pending = s->hdr;
        s->hdr_pending.slot[0].valid = 0u;
        s->hdr_pending.slot[1].valid = 0u;
        hdr_build(s, s->mem[0], &s->hdr_pending, s->hdr_seq + 1u);
    }
    rc = wv_step(s, s->part_lba + (uint32_t)(s->hdr_newest ^ 1u));
    if (rc == OVLSD_BUSY) {
        return;
    }
    if (rc != OVLSD_OK) {
        job_end(s, rc);
        probe_start(s);
        return;
    }
    s->hdr = s->hdr_pending;
    s->hdr_seq++;
    s->hdr_newest ^= 1u;
    s->skip = 0u;
    finish_state(s, OVLSD_EMPTY);
    s->chosen = SLOT_NONE;
    s->fallback = 0u;
}

/* ---- the poll ------------------------------------------------------------------------ */

static void device_not_ready(ovlstore_sd_t *s, ovl_bdev_state_t d)
{
    if (s->job != J_NONE) {
        job_end(s, OVLSD_EGONE);
    }
    if (s->dev_ready && d == OVL_BDEV_NONE) {
        s->skip = 0u;                 /* a READY card was pulled: the boot skip is over */
    }
    s->dev_ready = 0u;
    s->io_inflight = 0u;              /* usd failed it with -ENODEV; POSIX is synchronous */
    s->hdr_known = 0u;
    s->part_blocks = 0u;
    s->mbr = OVLSD_MBR_NOT_READ;
    s->chosen = SLOT_NONE;
    s->fallback = 0u;
    s->retry_pending = 0u;
    s->nblocks = 0u;
    switch (d) {
    case OVL_BDEV_NONE:        s->state = OVLSD_NO_CARD;     break;
    case OVL_BDEV_NO_HW:       s->state = OVLSD_NO_HW;       break;
    case OVL_BDEV_INIT:        s->state = OVLSD_INIT;        break;
    case OVL_BDEV_UNSUPPORTED: s->state = OVLSD_UNSUPPORTED; break;
    default:                   s->state = OVLSD_ERROR;       break;
    }
    s->err = (d == OVL_BDEV_ERROR || d == OVL_BDEV_UNSUPPORTED)
             ? s->bd.ops->error_code(s->bd.ctx) : 0;
}

void ovlstore_sd_poll(ovlstore_sd_t *s, uint32_t now_ms)
{
    ovl_bdev_state_t d;
    uint32_t cc;

    s->now = now_ms;
    s->started = 0u;
    s->io_completed = 0u;
    s->poll_crc = 0u;
    s->poll_ops = 0u;
    s->stats.polls++;

    if (s->bd.ops->poll != NULL) {
        s->bd.ops->poll(s->bd.ctx, now_ms);
    }
    d = s->bd.ops->state(s->bd.ctx);
    cc = (s->bd.ops->change_count != NULL) ? s->bd.ops->change_count(s->bd.ctx) : 0u;

    if (d != OVL_BDEV_READY) {
        device_not_ready(s, d);
    } else {
        if (!s->dev_ready || cc != s->cc) {
            /* A card became usable, or a different card is in (swapped
             * between two polls): read it from scratch. */
            if (s->job != J_NONE) {
                job_end(s, OVLSD_EGONE);
            }
            s->dev_ready = 1u;
            s->cc = cc;
            s->tries = 0u;
            probe_start(s);
        }
        if (s->io_inflight) {
            int st = s->bd.ops->io_status(s->bd.ctx);
            if (st != OVL_BDEV_IO_BUSY) {
                s->io_inflight = 0u;
                if (s->io_gen == s->gen) {
                    s->io_completed = 1u;
                    s->io_result = st;
                }                         /* else: an abandoned job's op, dropped */
            }
        }
        if (s->job == J_NONE && s->state == OVLSD_ERROR && s->retry_pending &&
            time_reached(now_ms, s->t_retry)) {
            probe_start(s);
        }
        switch (s->job) {
        case J_PROBE:  step_probe(s);  break;
        case J_VERIFY: step_verify(s); break;
        case J_STREAM: step_stream(s); break;
        case J_COMMIT: step_commit(s); break;
        case J_FORMAT: step_format(s); break;
        case J_CLEAR:  step_clear(s);  break;
        default:                       break;
        }
    }

    if (s->poll_ops > s->stats.max_ops_per_poll) {
        s->stats.max_ops_per_poll = s->poll_ops;
    }
    if (s->poll_crc > s->stats.max_crc_per_poll) {
        s->stats.max_crc_per_poll = s->poll_crc;
    }
}

/* ---- lifecycle / status ----------------------------------------------------------------- */

void ovlstore_sd_init(ovlstore_sd_t *s, const ovl_bdev_t *bd, const ovlstore_sd_cfg_t *cfg)
{
    memset(s, 0, sizeof *s);
    s->bd = *bd;
    if (cfg != NULL) {
        s->cfg = *cfg;
    }
    s->state = OVLSD_NO_CARD;
    s->chosen = SLOT_NONE;
    s->mbr = OVLSD_MBR_NOT_READ;
    s->job_result = OVLSD_OK;
}

/* Gate for the write-side calls: the card must be READY, probed, and not
 * FOREIGN. OVLSD_OK for EMPTY / VALID / STALE / BAD. */
static int state_gate(const ovlstore_sd_t *s)
{
    switch (s->state) {
    case OVLSD_NO_CARD:     return OVLSD_ENOCARD;
    case OVLSD_NO_HW:       return OVLSD_ENOHW;
    case OVLSD_INIT:        return (s->job == J_PROBE || s->job == J_VERIFY) ? OVLSD_EBUSY
                                                                            : OVLSD_ENOTREADY;
    case OVLSD_UNSUPPORTED:
    case OVLSD_ERROR:       return OVLSD_ENOTREADY;
    case OVLSD_FOREIGN:     return OVLSD_EFOREIGN;
    default:                return OVLSD_OK;
    }
}

int ovlstore_sd_rescan(ovlstore_sd_t *s)
{
    if (!s->dev_ready) {
        return (s->state == OVLSD_NO_CARD) ? OVLSD_ENOCARD
             : (s->state == OVLSD_NO_HW)   ? OVLSD_ENOHW : OVLSD_ENOTREADY;
    }
    if (s->job != J_NONE) {
        return OVLSD_EBUSY;
    }
    s->tries = 0u;
    probe_start(s);
    return OVLSD_OK;
}

void ovlstore_sd_set_skip(ovlstore_sd_t *s, bool skip)
{
    s->skip = skip ? 1u : 0u;
}

ovlstore_sd_state_t ovlstore_sd_state(const ovlstore_sd_t *s)
{
    return s->state;
}

int ovlstore_sd_error_code(const ovlstore_sd_t *s)
{
    return s->err;
}

static const char *valid_text(ovlstore_sd_t *s)
{
    static const char hex[] = "0123456789ABCDEF";
    const ovlstore_slot_desc_t *d = &s->hdr.slot[s->chosen & 1u];
    const char *name = (s->cfg.rm_name != NULL) ? s->cfg.rm_name(d->rm_id, s->cfg.rm_name_ctx)
                                                : NULL;
    char *p = s->text;
    unsigned n = 0;

    if (name != NULL && name[0] != '\0') {
        /* 12 name chars + " [A]" = 16: the whole of CLCD row 4 after "USD : ". */
        while (name[n] != '\0' && n < 12u) {
            char c = name[n];
            p[n] = (c >= 0x20 && c <= 0x7E) ? c : '?';
            n++;
        }
    } else {
        p[n++] = '0';
        p[n++] = 'x';
        for (int sh = 28; sh >= 0; sh -= 4) {
            p[n++] = hex[(d->rm_id >> sh) & 0xFu];
        }
    }
    p[n++] = ' ';
    p[n++] = '[';
    p[n++] = (s->chosen != 0u) ? 'B' : 'A';
    p[n++] = ']';
    p[n] = '\0';
    return s->text;
}

static const char *err_text(ovlstore_sd_t *s)
{
    char digits[10];
    unsigned nd = 0;
    uint32_t v = (s->err < 0) ? (uint32_t)(-(int64_t)s->err) : (uint32_t)s->err;
    char *p = s->text;

    memcpy(p, "ERR ", 4u);
    p += 4;
    do {
        digits[nd++] = (char)('0' + (v % 10u));
        v /= 10u;
    } while (v != 0u && nd < sizeof digits);
    while (nd > 0u) {
        *p++ = digits[--nd];
    }
    *p = '\0';
    return s->text;
}

const char *ovlstore_sd_state_text(ovlstore_sd_t *s)
{
    switch (s->state) {
    case OVLSD_NO_CARD: return "none";
    case OVLSD_NO_HW:   return "no hw";
    default:            break;
    }
    if (s->skip) {
        return "skipped";
    }
    switch (s->state) {
    case OVLSD_UNSUPPORTED: return "unsupported";
    case OVLSD_ERROR:       return err_text(s);
    case OVLSD_FOREIGN:     return "foreign";
    case OVLSD_EMPTY:       return "empty";
    case OVLSD_STALE:       return "stale key";
    case OVLSD_BAD:         return "bad";
    case OVLSD_VALID:       return valid_text(s);
    default:                return "init";
    }
}

const char *ovlstore_sd_state_name(ovlstore_sd_state_t st)
{
    switch (st) {
    case OVLSD_NO_CARD:     return "none";
    case OVLSD_NO_HW:       return "no_hw";
    case OVLSD_INIT:        return "init";
    case OVLSD_UNSUPPORTED: return "unsupported";
    case OVLSD_ERROR:       return "error";
    case OVLSD_FOREIGN:     return "foreign";
    case OVLSD_EMPTY:       return "empty";
    case OVLSD_VALID:       return "valid";
    case OVLSD_STALE:       return "stale";
    case OVLSD_BAD:         return "bad";
    }
    return "error";
}

const char *ovlstore_sd_rc_name(int rc)
{
    switch (rc) {
    case OVLSD_OK:         return "ok";
    case OVLSD_BUSY:       return "busy";
    case OVLSD_DONE:       return "done";
    case OVLSD_ENOCARD:    return "no sd card";
    case OVLSD_ENOHW:      return "no usd hardware";
    case OVLSD_ENOTREADY:  return "card not ready";
    case OVLSD_EFOREIGN:   return "foreign card";
    case OVLSD_ESTALE:     return "stale key";
    case OVLSD_ESTATIC:    return "static_id mismatch";
    case OVLSD_ERMID:      return "rm_id mismatch";
    case OVLSD_EBUSY:      return "store busy";
    case OVLSD_EARG:       return "bad argument";
    case OVLSD_ETOOBIG:    return "too big for slot";
    case OVLSD_EORDER:     return "out of order";
    case OVLSD_ELEN:       return "length mismatch";
    case OVLSD_EIO:        return "io error";
    case OVLSD_ECRC:       return "crc mismatch";
    case OVLSD_EVERIFY:    return "read-back mismatch";
    case OVLSD_EGONE:      return "card removed";
    case OVLSD_ENODEFAULT: return "no default";
    case OVLSD_ESKIPPED:   return "skipped";
    case OVLSD_EABORTED:   return "aborted";
    case OVLSD_ECONFIRM:   return "confirm required";
    case OVLSD_EEXIST:     return "provision p4 first";
    case OVLSD_EUNKNOWN:   return "unrecognised lba 0";
    case OVLSD_EPART:      return "bad 0xDA partition";
    case OVLSD_ESMALL:     return "partition too small";
    case OVLSD_EFSSIG:     return "filesystem present";
    case OVLSD_ENOWIPE:    return "wipe disabled";
    default:               return "error";
    }
}

int ovlstore_sd_default(const ovlstore_sd_t *s, ovlstore_sd_desc_t *out, char *slot)
{
    const ovlstore_slot_desc_t *d;

    if (s->state != OVLSD_VALID || s->chosen > 1u) {
        return OVLSD_ENODEFAULT;
    }
    d = &s->hdr.slot[s->chosen];
    if (out != NULL) {
        out->rm_id     = d->rm_id;
        out->static_id = d->static_id;
        out->clear_len = d->clear_len;
        out->clear_crc = d->clear_crc;
        out->part_len  = d->part_len;
        out->part_crc  = d->part_crc;
    }
    if (slot != NULL) {
        *slot = (s->chosen != 0u) ? 'B' : 'A';
    }
    return OVLSD_OK;
}

int ovlstore_sd_header_default(const ovlstore_sd_t *s, ovlstore_sd_desc_t *out, char *slot)
{
    const ovlstore_slot_desc_t *d;
    uint8_t k;

    if (s->state == OVLSD_VALID) {
        return ovlstore_sd_default(s, out, slot);
    }
    if (s->state != OVLSD_STALE || !s->hdr_known) {
        return OVLSD_ENODEFAULT;
    }
    k = (uint8_t)(s->hdr.active_slot & 1u);
    d = &s->hdr.slot[k];
    if (out != NULL) {
        out->rm_id     = d->rm_id;
        out->static_id = d->static_id;
        out->clear_len = d->clear_len;
        out->clear_crc = d->clear_crc;
        out->part_len  = d->part_len;
        out->part_crc  = d->part_crc;
    }
    if (slot != NULL) {
        *slot = (k != 0u) ? 'B' : 'A';
    }
    return OVLSD_OK;
}

_Static_assert((int)OVLSD_JOB_PROBE == (int)J_PROBE && (int)OVLSD_JOB_VERIFY == (int)J_VERIFY &&
               (int)OVLSD_JOB_STREAM == (int)J_STREAM && (int)OVLSD_JOB_COMMIT == (int)J_COMMIT &&
               (int)OVLSD_JOB_FORMAT == (int)J_FORMAT && (int)OVLSD_JOB_CLEAR == (int)J_CLEAR,
               "ovlstore_sd_job_t must track the private J_* values");

ovlstore_sd_job_t ovlstore_sd_job(const ovlstore_sd_t *s)
{
    return (ovlstore_sd_job_t)s->job;
}

int ovlstore_sd_job_result(const ovlstore_sd_t *s)
{
    return user_job(s->job) ? OVLSD_BUSY : s->job_result;
}

void ovlstore_sd_info(ovlstore_sd_t *s, ovlstore_sd_info_t *o)
{
    memset(o, 0, sizeof *o);
    o->state       = s->state;
    o->state_name  = ovlstore_sd_state_name(s->state);
    o->err         = s->err;
    o->present     = !(s->state == OVLSD_NO_CARD || s->state == OVLSD_NO_HW);
    o->card_mb     = s->dev_ready ? s->nblocks / 2048u : 0u;
    o->have_default = (ovlstore_sd_default(s, &o->def, &o->slot) == OVLSD_OK);
    o->fallback    = o->have_default && s->fallback != 0u;
    o->mbr         = s->mbr;
    o->part_lba    = s->part_lba;
    o->part_blocks = s->part_blocks;
    o->skip        = s->skip != 0u;
    o->busy        = s->job != J_NONE;
    o->last_result = ovlstore_sd_job_result(s);
}

const ovlstore_sd_stats_t *ovlstore_sd_stats(const ovlstore_sd_t *s)
{
    return &s->stats;
}

void ovlstore_sd_stats_reset(ovlstore_sd_t *s)
{
    memset(&s->stats, 0, sizeof s->stats);
}

/* ---- stream API --------------------------------------------------------------------- */

int ovlstore_sd_stream_begin(ovlstore_sd_t *s, ovlstore_sd_which_t which)
{
    if (s->state != OVLSD_VALID) {
        switch (s->state) {
        case OVLSD_STALE: return OVLSD_ESTALE;
        case OVLSD_EMPTY:
        case OVLSD_BAD:   return OVLSD_ENODEFAULT;
        default:          return state_gate(s);
        }
    }
    if (s->skip) {
        return OVLSD_ESKIPPED;
    }
    if (s->job != J_NONE) {
        return OVLSD_EBUSY;
    }
    if (which != OVLSD_CLEARING && which != OVLSD_PARTIAL) {
        return OVLSD_EARG;
    }
    job_start(s, J_STREAM, PH_STREAM);
    rr_setup_slot(s, s->chosen, which, 1u);
    return OVLSD_OK;
}

int ovlstore_sd_stream_next(ovlstore_sd_t *s, uint32_t max_bytes,
                            const uint8_t **data, uint32_t *len)
{
    ovlsd_buf_t *b;
    uint32_t take;

    if (data != NULL) {
        *data = NULL;
    }
    if (len != NULL) {
        *len = 0u;
    }
    if (s->job != J_STREAM) {
        return (s->last_job == J_STREAM) ? s->job_result : OVLSD_EORDER;
    }
    if (data == NULL || len == NULL || max_bytes == 0u) {
        return OVLSD_EARG;
    }
    b = &s->b[s->drain];
    if (b->st != B_READY) {
        return OVLSD_BUSY;
    }
    take = b->bytes - b->pos;
    if (take > max_bytes) {
        take = max_bytes;
    }
    *data = s->mem[s->drain] + b->pos;
    *len = take;
    b->pos += take;
    if (b->pos == b->bytes) {
        bool last = (b->last != 0u);
        b->st = B_FREE;
        s->drain ^= 1u;
        if (last) {
            job_end(s, OVLSD_DONE);   /* the bytes stay put until the next call */
        }
    }
    return OVLSD_OK;
}

void ovlstore_sd_stream_abort(ovlstore_sd_t *s)
{
    if (s->job == J_STREAM) {
        job_end(s, OVLSD_EABORTED);
    }
}

/* ---- commit API --------------------------------------------------------------------- */

/* Never the slot that holds the verified default. */
static uint8_t commit_target(const ovlstore_sd_t *s)
{
    switch (s->state) {
    case OVLSD_VALID: return (uint8_t)(s->chosen ^ 1u);
    case OVLSD_STALE: return (uint8_t)(s->hdr.active_slot ^ 1u);  /* keep the stale one */
    case OVLSD_BAD:   return s->hdr.active_slot;                 /* overwrite the corrupt one */
    default:          return 0u;                                 /* EMPTY: slot A first */
    }
}

int ovlstore_sd_commit_begin(ovlstore_sd_t *s, const ovlstore_sd_desc_t *d,
                             uint32_t live_static_id, uint32_t live_rm_id)
{
    int rc = state_gate(s);
    uint8_t t;

    if (rc != OVLSD_OK) {
        return rc;
    }
    if (s->job != J_NONE) {
        return OVLSD_EBUSY;
    }
    if (d == NULL) {
        return OVLSD_EARG;
    }
    if (d->static_id != live_static_id || live_static_id != s->cfg.live_static_id) {
        return OVLSD_ESTATIC;
    }
    if (d->rm_id != live_rm_id) {
        return OVLSD_ERMID;
    }
    if (d->clear_len == 0u || d->part_len == 0u || ((d->clear_len | d->part_len) & 3u) != 0u) {
        return OVLSD_EARG;
    }
    if (d->clear_len > OVLSD_SLOT_BYTES || d->part_len > OVLSD_SLOT_BYTES ||
        blocks_of(d->clear_len) + blocks_of(d->part_len) > OVLSD_SLOT_BLOCKS) {
        return OVLSD_ETOOBIG;
    }
    t = commit_target(s);
    job_start(s, J_COMMIT, PH_C_FEED);
    s->cdesc = *d;
    s->ctarget = t;
    s->commit_slot = 0;
    s->cfed_clear = 0u;
    s->cfed_part = 0u;
    s->cwlba = s->part_lba + slot_base(t);
    s->cwend = s->cwlba + OVLSD_SLOT_BLOCKS;
    return OVLSD_OK;
}

static int commit_gone_rc(const ovlstore_sd_t *s)
{
    return (s->last_job == J_COMMIT && s->job_result < 0) ? s->job_result : OVLSD_EORDER;
}

int ovlstore_sd_commit_feed(ovlstore_sd_t *s, ovlstore_sd_which_t which,
                            const void *data, uint32_t len, uint32_t *consumed)
{
    const uint8_t *p = (const uint8_t *)data;
    uint32_t cap = opmax(s) * BLK;
    uint32_t done = 0u;

    if (consumed != NULL) {
        *consumed = 0u;
    }
    if (s->job != J_COMMIT) {
        return commit_gone_rc(s);
    }
    if (s->ph != PH_C_FEED) {
        return OVLSD_EORDER;
    }
    if (consumed == NULL || (data == NULL && len > 0u)) {
        return OVLSD_EARG;
    }
    if (which == OVLSD_CLEARING) {
        if (len > s->cdesc.clear_len - s->cfed_clear) {
            return OVLSD_ELEN;
        }
    } else if (which == OVLSD_PARTIAL) {
        if (s->cfed_clear != s->cdesc.clear_len) {
            return OVLSD_EORDER;
        }
        if (len > s->cdesc.part_len - s->cfed_part) {
            return OVLSD_ELEN;
        }
    } else {
        return OVLSD_EARG;
    }
    if (s->io_inflight && s->io_gen != s->gen) {
        return OVLSD_BUSY;     /* an abandoned job's op may still be using a buffer */
    }
    while (done < len) {       /* bounded: at most the two buffers' worth */
        uint8_t bi = s->fill;
        ovlsd_buf_t *b = &s->b[bi];
        uint32_t take;
        if (b->st == B_FREE) {
            b->st = B_FILL;
            b->bytes = 0u;
        }
        if (b->st != B_FILL) {
            break;             /* both buffers queued or in flight: back-pressure */
        }
        take = cap - b->bytes;
        if (take > len - done) {
            take = len - done;
        }
        memcpy(s->mem[bi] + b->bytes, p + done, take);
        b->bytes += take;
        done += take;
        if (which == OVLSD_CLEARING) {
            s->cfed_clear += take;
            if (s->cfed_clear == s->cdesc.clear_len) {
                pad_block(s, bi);    /* the partial starts on the next block */
            }
        } else {
            s->cfed_part += take;
        }
        if (b->bytes == cap) {
            int rc = seal(s, bi);
            if (rc != OVLSD_OK) {
                *consumed = done;
                commit_fail(s, rc);
                return rc;
            }
        }
    }
    *consumed = done;
    return (done == 0u && len > 0u) ? OVLSD_BUSY : OVLSD_OK;
}

int ovlstore_sd_commit_end(ovlstore_sd_t *s)
{
    uint8_t bi;

    if (s->job != J_COMMIT) {
        return commit_gone_rc(s);
    }
    if (s->ph != PH_C_FEED) {
        return OVLSD_EORDER;
    }
    if (s->cfed_clear != s->cdesc.clear_len || s->cfed_part != s->cdesc.part_len) {
        return OVLSD_ELEN;
    }
    bi = s->fill;
    if (s->b[bi].st == B_FILL) {
        int rc;
        pad_block(s, bi);
        rc = seal(s, bi);
        if (rc != OVLSD_OK) {
            commit_fail(s, rc);
            return rc;
        }
    }
    s->ph = PH_C_FLUSH;
    return OVLSD_OK;
}

void ovlstore_sd_commit_abort(ovlstore_sd_t *s)
{
    /* Only while feeding. After commit_end() the rest needs no input and runs
     * to completion (a header write already in flight cannot be recalled). */
    if (s->job == J_COMMIT && s->ph == PH_C_FEED) {
        commit_fail(s, OVLSD_EABORTED);
    }
}

char ovlstore_sd_commit_slot(const ovlstore_sd_t *s)
{
    return (s->last_job == J_COMMIT && s->job_result == OVLSD_OK && s->job != J_COMMIT)
           ? s->commit_slot : (char)0;
}

/* ---- maintenance API ------------------------------------------------------------------ */

int ovlstore_sd_clear(ovlstore_sd_t *s)
{
    int rc = state_gate(s);
    if (rc != OVLSD_OK) {
        return rc;
    }
    if (s->job != J_NONE) {
        return OVLSD_EBUSY;
    }
    job_start(s, J_CLEAR, PH_CL_HDR);
    return OVLSD_OK;
}

int ovlstore_sd_format(ovlstore_sd_t *s, const char *confirm)
{
    int  rc;
    bool wipe;

    if (confirm != NULL && strcmp(confirm, OVLSD_CONFIRM_FORMAT) == 0) {
        wipe = false;
    } else if (confirm != NULL && strcmp(confirm, OVLSD_CONFIRM_WIPE) == 0) {
        wipe = true;                         /* the ONLY way to the wipe */
    } else {
        return OVLSD_ECONFIRM;
    }
    switch (s->state) {
    case OVLSD_NO_CARD:
    case OVLSD_NO_HW:
    case OVLSD_INIT:
    case OVLSD_UNSUPPORTED:
    case OVLSD_ERROR:
        return state_gate(s);
    default:
        break;              /* FOREIGN included: format is the one way out of it */
    }
    if (s->job != J_NONE) {
        return OVLSD_EBUSY;
    }
    if (wipe) {
        if (!s->cfg.allow_wipe) {
            return OVLSD_ENOWIPE;            /* e.g. Linux: the card holds the system */
        }
        if (s->cfg.raw_partition) {
            return OVLSD_EARG;               /* the device IS p4: there is no MBR */
        }
        if (s->nblocks < OVLSD_FORMAT_PART_BLOCKS + OVLSD_ALIGN_BLOCKS) {
            return OVLSD_ESMALL;
        }
        job_start(s, J_FORMAT, PH_F_WIPE);
        s->fmt_all = 1u;
        s->fmt_filled = 0u;
        s->fmt_next = 1u;
        s->fmt_n = 0u;
        return OVLSD_OK;
    }
    /* THE FIRST CHECK: the rules against the probe's reading of the card. */
    rc = format_rule(s, s->mbr);
    if (rc != OVLSD_OK) {
        return rc;          /* zero I/O */
    }
    job_start(s, J_FORMAT, PH_F_READ);
    return OVLSD_OK;
}
