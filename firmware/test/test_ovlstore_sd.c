/*
 * test_ovlstore_sd.c -- host tests for the SD overlay store
 * (firmware/overlay_store/ovlstore_sd.c, D13 lane L3-core).
 *
 * Most cases run the store over the POSIX provider on a temp-file card image
 * (created, unlinked at once, so nothing is left behind). The last case runs it
 * end to end over the bare-metal provider (ovl_bdev_usd.c) + the real usd.c
 * driver + fake_usd.c (a register-level usd_spi and an SD card in SPI mode).
 *
 * Every store poll goes through spoll(), which asserts on EVERY call:
 *   - at most 1 device op started per poll, and none outside a poll;
 *   - CRC bytes per poll <= OVLSTORE_SD_CRC_BYTES_PER_POLL, blocks per op <=
 *     OVLSTORE_SD_OP_BLOCKS;
 *   - the CLCD text is <= 16 printable ASCII chars.
 * So the budget and the text width are checked in every state any case reaches.
 *
 * Cases (the lane's list):
 *    1  no card (and no hw / init / unsupported / ERR n): zero device ops
 *    2  foreign card (FAT MBR, GPT, superfloppy, garbage, bad/small 0xDA, and
 *       "not truly blank": whole-disk ext4 / exFAT / f2fs / btrfs / swap / ISO,
 *       a GPT behind a zeroed LBA 0, all-0xFF, entry bytes without a type):
 *       FOREIGN, format refuses, ZERO writes
 *    3  blank card: format makes 0xDA over the last 32 MiB -> EMPTY
 *    4  Linux-provisioned card (p1 raw, p2 raw, p3 83, p4 0xDA): format and a
 *       commit write only inside p4; MBR and p1-p3 bytes unchanged
 *    5  commit A -> VALID [A], commit B -> VALID [B], streamed back byte-exact
 *    6  torn commit: abort mid-feed, write failure, torn data block, silent
 *       corruption: the old default survives
 *    7  torn header write: the previous copy is used
 *    8  stale static_id: STALE, stream refused, ZERO slot reads; a stale card
 *       may be committed over (re-keyed board recovery)
 *    9  CRC fallback to the other slot; both bad -> BAD; other slot stale -> BAD
 *   10  commit refusals: wrong static_id / rm_id -> refused, zero writes
 *   11  sizes: a 2.9 MB partial + a 222 KB clearing
 *   12  per-poll budget (every poll; maxima reported)
 *   13  state text <= 16 chars in every state; skip; ERR 30 + auto-retry
 *   15  "erase-all": the explicit wipe works on FAT32 / ext4 / GPT / exFAT /
 *       harness cards; plain "erase" still refuses the same cards
 *   14  end to end over ovl_bdev_usd + usd.c + fake_usd: no card = zero DATA
 *       writes; format, commits, card pulled mid-commit, re-insert
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

#include "../overlay_store/ovlstore_sd.h"
#include "../overlay_store/ovl_bdev.h"
#include "../overlay_store/ovlstore_codec.h"
#include "../common/crc32.h"
#include "../usd/usd.h"
#include "mock_regs.h"
#include "fake_usd.h"

static unsigned s_checks;
#define CHECK(cond) do {                                                          \
        s_checks++;                                                               \
        if (!(cond)) {                                                            \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
            exit(1);                                                              \
        }                                                                         \
    } while (0)
#define CHECK_RC(expr, want) do {                                                 \
        int rc_ = (expr);                                                         \
        s_checks++;                                                               \
        if (rc_ != (want)) {                                                      \
            fprintf(stderr, "%s:%d: %s = %d (%s), want %d (%s)\n", __FILE__, __LINE__, \
                    #expr, rc_, ovlstore_sd_rc_name(rc_), (int)(want),            \
                    ovlstore_sd_rc_name(want));                                   \
            exit(1);                                                              \
        }                                                                         \
    } while (0)

#define LIVE      0x3F1A560Fu     /* the live shell */
#define OTHER     0xA8C1C535u     /* a different mint */
#define RM_LED    0x00000011u
#define RM_MC     0x0000002Au     /* long name: clipped */
#define RM_ANON   0x0000002Bu     /* no name: hex */
#define RM_WEIRD  0x0000002Cu     /* non-printable name chars */

#define MIB       2048u           /* blocks */
#define POLL_LIMIT 4000000u

/* ======================= the counting / fault wrapper ======================= */

typedef struct {
    ovl_bdev_t inner;
    int        force_state;        /* -1 = pass through */
    int        force_err;
    uint32_t   reads, writes, blocks_r, blocks_w;
    uint32_t   poll_ops, max_poll_ops, outside_ops;
    /* writes outside [allow_lo, allow_hi) (plus LBA 0 when allow_lba0) */
    uint32_t   allow_lo, allow_hi, writes_outside;
    int        allow_lba0;
    uint32_t   allow_head;                    /* writes inside [0, allow_head) are fine */
    /* reads intersecting [watch_lo, watch_hi) */
    uint32_t   watch_lo, watch_hi, watch_reads;
    /* fault injection (1-based op indices; 0 = off) */
    uint32_t   fail_read_at, fail_write_at, tear_write_at, corrupt_write_at;
    uint32_t   tear_lba_armed, tear_lba;      /* tear the next write that starts at tear_lba */
    int        override, last;
    uint32_t   cc_bump;
    uint32_t   last_write_lba;
} cw_t;

static cw_t     g_cw;
static int      g_in_poll;
static uint32_t g_now = 1000u;
static uint8_t  g_tmp[128u * 512u];

static void cw_count(cw_t *w)
{
    if (g_in_poll) {
        w->poll_ops++;
    } else {
        w->outside_ops++;
    }
}

static int cw_read_start(void *ctx, uint32_t lba, uint32_t n, void *buf)
{
    cw_t *w = ctx;
    int rc = w->inner.ops->read_start(w->inner.ctx, lba, n, buf);
    if (rc != 0) {
        return rc;
    }
    cw_count(w);
    w->reads++;
    w->blocks_r += n;
    if (lba < w->watch_hi && lba + n > w->watch_lo) {
        w->watch_reads++;
    }
    w->override = 0;
    if (w->fail_read_at != 0u && w->reads == w->fail_read_at) {
        w->override = 1;
        w->last = -EIO;
    }
    return 0;
}

static int cw_write_start(void *ctx, uint32_t lba, uint32_t n, const void *buf)
{
    cw_t *w = ctx;
    uint32_t idx = w->writes + 1u;
    const void *src = buf;
    int torn = 0;
    int rc;

    if (n * 512u > sizeof g_tmp) {
        return -EINVAL;
    }
    if (w->tear_write_at == idx || (w->tear_lba_armed && lba == w->tear_lba)) {
        /* A torn write: the first 40 bytes land, the rest of the op is garbage. */
        memset(g_tmp, 0xA5, n * 512u);
        memcpy(g_tmp, buf, 40u);
        src = g_tmp;
        torn = 1;
        w->tear_lba_armed = 0u;
    } else if (w->corrupt_write_at == idx) {
        /* Silent corruption: the card says OK and stores a flipped byte. */
        memcpy(g_tmp, buf, n * 512u);
        g_tmp[n * 256u] ^= 0x40u;
        src = g_tmp;
    }
    if (w->fail_write_at == idx) {
        /* Nothing lands, the op fails. */
        w->writes++;
        cw_count(w);
        w->override = 1;
        w->last = -EIO;
        return 0;
    }
    rc = w->inner.ops->write_start(w->inner.ctx, lba, n, src);
    if (rc != 0) {
        return rc;
    }
    cw_count(w);
    w->writes++;
    w->blocks_w += n;
    w->last_write_lba = lba;
    if (!((lba >= w->allow_lo && lba + n <= w->allow_hi) ||
          (w->allow_lba0 && lba == 0u && n == 1u) || lba + n <= w->allow_head)) {
        w->writes_outside++;
    }
    w->override = torn;
    w->last = -EIO;
    return 0;
}

static int cw_io_status(void *ctx)
{
    cw_t *w = ctx;
    return w->override ? w->last : w->inner.ops->io_status(w->inner.ctx);
}

static void cw_poll(void *ctx, uint32_t now)
{
    cw_t *w = ctx;
    if (w->inner.ops->poll != NULL) {
        w->inner.ops->poll(w->inner.ctx, now);
    }
}

static uint32_t cw_nblocks(void *ctx)
{
    cw_t *w = ctx;
    return w->inner.ops->nblocks(w->inner.ctx);
}

static bool cw_present(void *ctx)
{
    cw_t *w = ctx;
    return w->inner.ops->present(w->inner.ctx);
}

static ovl_bdev_state_t cw_state(void *ctx)
{
    cw_t *w = ctx;
    return (w->force_state >= 0) ? (ovl_bdev_state_t)w->force_state
                                 : w->inner.ops->state(w->inner.ctx);
}

static int cw_error_code(void *ctx)
{
    cw_t *w = ctx;
    return (w->force_state >= 0) ? w->force_err : w->inner.ops->error_code(w->inner.ctx);
}

static uint32_t cw_change_count(void *ctx)
{
    cw_t *w = ctx;
    uint32_t c = (w->inner.ops->change_count != NULL) ? w->inner.ops->change_count(w->inner.ctx) : 0u;
    return c + w->cc_bump;
}

static const ovl_bdev_ops_t s_cw_ops = {
    .read_start = cw_read_start, .write_start = cw_write_start, .io_status = cw_io_status,
    .poll = cw_poll, .nblocks = cw_nblocks, .present = cw_present, .state = cw_state,
    .error_code = cw_error_code, .change_count = cw_change_count,
};

static ovl_bdev_t cw_wrap(const ovl_bdev_t *inner)
{
    ovl_bdev_t bd;
    memset(&g_cw, 0, sizeof g_cw);
    g_cw.inner = *inner;
    g_cw.force_state = -1;
    g_cw.allow_hi = 0xFFFFFFFFu;
    bd.ops = &s_cw_ops;
    bd.ctx = &g_cw;
    bd.max_blocks = inner->max_blocks;
    return bd;
}

/* ============================ harness ======================================== */

static uint32_t g_max_crc, g_max_ops, g_max_blk, g_polls;
static int      g_usd_mode;                   /* 1: time is mock time, check fake_usd budget */
static uint32_t g_max_usd_bytes;

static void check_text(ovlstore_sd_t *s)
{
    const char *t = ovlstore_sd_state_text(s);
    size_t n = strlen(t);
    CHECK(n >= 1u && n <= 16u);
    for (size_t i = 0; i < n; i++) {
        CHECK(t[i] >= 0x20 && t[i] <= 0x7E);
    }
}

static void spoll(ovlstore_sd_t *s)
{
    const ovlstore_sd_stats_t *st;
    if (g_usd_mode) {
        mock_time_advance_ms(1u);
        g_now = mock_time_now_ms();
        fake_usd_poll_begin();
    } else {
        g_now += 1u;
    }
    g_in_poll = 1;
    g_cw.poll_ops = 0u;
    ovlstore_sd_poll(s, g_now);
    g_in_poll = 0;
    g_polls++;

    st = ovlstore_sd_stats(s);
    CHECK(g_cw.poll_ops <= 1u);
    CHECK(g_cw.outside_ops == 0u);
    CHECK(st->max_ops_per_poll <= 1u);
    CHECK(st->max_crc_per_poll <= OVLSTORE_SD_CRC_BYTES_PER_POLL);
    CHECK(st->max_blocks_per_op <= OVLSTORE_SD_OP_BLOCKS);
    if (st->max_crc_per_poll > g_max_crc) g_max_crc = st->max_crc_per_poll;
    if (st->max_ops_per_poll > g_max_ops) g_max_ops = st->max_ops_per_poll;
    if (st->max_blocks_per_op > g_max_blk) g_max_blk = st->max_blocks_per_op;
    if (g_usd_mode) {
        uint32_t b = fake_usd_poll_bytes();
        CHECK(b <= USD_POLL_BUDGET_FAST);
        if (b > g_max_usd_bytes) g_max_usd_bytes = b;
    }
    check_text(s);
}

static void spoll_n(ovlstore_sd_t *s, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        spoll(s);
    }
}

/* Poll until the store has settled: no job running and not INIT. */
static uint32_t settle(ovlstore_sd_t *s)
{
    ovlstore_sd_info_t in;
    for (uint32_t i = 1; i <= POLL_LIMIT; i++) {
        spoll(s);
        ovlstore_sd_info(s, &in);
        if (!in.busy && in.state != OVLSD_INIT) {
            return i;
        }
    }
    fprintf(stderr, "store did not settle: state %s\n", ovlstore_sd_state_name(ovlstore_sd_state(s)));
    CHECK(0);
    return 0;
}

static int wait_job(ovlstore_sd_t *s)
{
    for (uint32_t i = 0; i < POLL_LIMIT && ovlstore_sd_job_result(s) == OVLSD_BUSY; i++) {
        spoll(s);
    }
    return ovlstore_sd_job_result(s);
}

static const char *rm_name_cb(uint32_t rm_id, void *ctx)
{
    (void)ctx;
    switch (rm_id) {
    case RM_LED:   return "led";
    case RM_MC:    return "nanosoc_multicore";
    case RM_WEIRD: return "a\tb\x01" "c";
    default:       return NULL;
    }
}

static void store_init(ovlstore_sd_t *s, const ovl_bdev_t *bd, uint32_t live)
{
    ovlstore_sd_cfg_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.live_static_id = live;
    cfg.rm_name = rm_name_cb;
    cfg.allow_wipe = true;                    /* the bare-metal setting */
    ovlstore_sd_init(s, bd, &cfg);
}

/* ---- card images ---------------------------------------------------------- */

typedef struct {
    int              fd;
    ovl_bdev_posix_t px;
    ovl_bdev_t       inner;
    ovl_bdev_t       bd;      /* the wrapper */
} card_t;

static void card_new(card_t *c, uint32_t nblocks)
{
    char path[512];
    const char *dir = getenv("TMPDIR");
    snprintf(path, sizeof path, "%s/ovlsd_card_XXXXXX", (dir && dir[0]) ? dir : "/tmp");
    c->fd = mkstemp(path);
    CHECK(c->fd >= 0);
    CHECK(unlink(path) == 0);
    CHECK(ftruncate(c->fd, (off_t)nblocks * 512) == 0);
    CHECK(ovl_bdev_posix_attach_fd(&c->px, &c->inner, c->fd) == 0);
    CHECK(c->px.nblocks == nblocks);
    c->bd = cw_wrap(&c->inner);
}

static void card_free(card_t *c)
{
    ovl_bdev_posix_close(&c->px);
    close(c->fd);
}

static void img_write(card_t *c, uint32_t lba, const void *buf, uint32_t nbytes)
{
    CHECK(pwrite(c->fd, buf, nbytes, (off_t)lba * 512) == (ssize_t)nbytes);
}

static void img_read(card_t *c, uint32_t lba, void *buf, uint32_t nbytes)
{
    CHECK(pread(c->fd, buf, nbytes, (off_t)lba * 512) == (ssize_t)nbytes);
}

static void img_flip(card_t *c, uint64_t byte_off)
{
    uint8_t b;
    CHECK(pread(c->fd, &b, 1, (off_t)byte_off) == 1);
    b ^= 0x10u;
    CHECK(pwrite(c->fd, &b, 1, (off_t)byte_off) == 1);
}

static void put32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

static uint32_t get32le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

typedef struct { uint8_t type; uint32_t start, cnt; } pent_t;

static void mk_mbr(uint8_t mbr[512], const pent_t e[4])
{
    memset(mbr, 0, 512);
    mbr[0] = 0xFAu; mbr[1] = 0x33u; mbr[2] = 0xC0u;      /* some boot code */
    put32le(mbr + 440, 0x12345678u);
    for (unsigned i = 0; i < 4u; i++) {
        uint8_t *p = mbr + 446u + 16u * i;
        p[4] = e[i].type;
        put32le(p + 8, e[i].start);
        put32le(p + 12, e[i].cnt);
    }
    mbr[510] = 0x55u;
    mbr[511] = 0xAAu;
}

/* ---- payloads -------------------------------------------------------------- */

static uint8_t *pattern(uint32_t len, uint32_t seed)
{
    uint8_t *p = malloc(len);
    uint32_t x = seed * 2654435761u + 1u;
    CHECK(p != NULL);
    for (uint32_t i = 0; i < len; i++) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        p[i] = (uint8_t)x;
    }
    return p;
}

typedef struct {
    ovlstore_sd_desc_t d;
    uint8_t *clr, *part;
} ovl_t;

static void ovl_make(ovl_t *o, uint32_t rm, uint32_t static_id, uint32_t clen, uint32_t plen, uint32_t seed)
{
    o->clr = pattern(clen, seed);
    o->part = pattern(plen, seed + 7777u);
    o->d.rm_id = rm;
    o->d.static_id = static_id;
    o->d.clear_len = clen;
    o->d.clear_crc = mps3_crc32(o->clr, clen);
    o->d.part_len = plen;
    o->d.part_crc = mps3_crc32(o->part, plen);
}

static void ovl_free(ovl_t *o)
{
    free(o->clr);
    free(o->part);
}

/* Feed a region the way a TCP receiver would: fixed chunks, retry on BUSY. */
static int feed_region(ovlstore_sd_t *s, ovlstore_sd_which_t w, const uint8_t *buf,
                       uint32_t len, uint32_t chunk, uint32_t stop_after)
{
    uint32_t off = 0;
    while (off < len && off < stop_after) {
        uint32_t n = len - off, got = 0;
        int rc;
        if (n > chunk) n = chunk;
        rc = ovlstore_sd_commit_feed(s, w, buf + off, n, &got);
        if (rc < 0) {
            return rc;
        }
        off += got;
        if (rc == OVLSD_BUSY || got < n) {
            spoll(s);
        }
    }
    return OVLSD_OK;
}

/* A whole commit: begin, feed clearing then partial, end, wait. */
static int do_commit(ovlstore_sd_t *s, const ovl_t *o, uint32_t live, uint32_t chunk)
{
    int rc = ovlstore_sd_commit_begin(s, &o->d, live, o->d.rm_id);
    if (rc != OVLSD_OK) return rc;
    rc = feed_region(s, OVLSD_CLEARING, o->clr, o->d.clear_len, chunk, 0xFFFFFFFFu);
    if (rc != OVLSD_OK) return rc;
    rc = feed_region(s, OVLSD_PARTIAL, o->part, o->d.part_len, chunk, 0xFFFFFFFFu);
    if (rc != OVLSD_OK) return rc;
    rc = ovlstore_sd_commit_end(s);
    if (rc != OVLSD_OK) return rc;
    return wait_job(s);
}

/* Stream a region back, one chunk per poll; returns OVLSD_DONE or an error. */
static int stream_back(ovlstore_sd_t *s, ovlstore_sd_which_t w, uint8_t *out, uint32_t cap,
                       uint32_t *got, uint32_t max_chunk)
{
    int rc = ovlstore_sd_stream_begin(s, w);
    *got = 0;
    if (rc != OVLSD_OK) return rc;
    for (uint32_t i = 0; i < POLL_LIMIT; i++) {
        const uint8_t *p = NULL;
        uint32_t n = 0;
        spoll(s);
        rc = ovlstore_sd_stream_next(s, max_chunk, &p, &n);
        if (rc == OVLSD_OK) {
            CHECK(n > 0u && n <= max_chunk && *got + n <= cap);
            CHECK((n & 3u) == 0u);
            memcpy(out + *got, p, n);
            *got += n;
        } else if (rc != OVLSD_BUSY) {
            return rc;
        }
    }
    return OVLSD_EIO;
}

/* The verified default must be `o`, in `slot`, and both regions must stream
 * back byte-exact. */
static void expect_default(ovlstore_sd_t *s, const ovl_t *o, char slot)
{
    ovlstore_sd_desc_t d;
    char sl = 0;
    uint32_t got = 0;
    uint8_t *buf;

    CHECK(ovlstore_sd_state(s) == OVLSD_VALID);
    CHECK_RC(ovlstore_sd_default(s, &d, &sl), OVLSD_OK);
    CHECK(sl == slot);
    CHECK(memcmp(&d, &o->d, sizeof d) == 0);

    buf = malloc(o->d.part_len > o->d.clear_len ? o->d.part_len : o->d.clear_len);
    CHECK(buf != NULL);
    CHECK_RC(stream_back(s, OVLSD_CLEARING, buf, o->d.clear_len, &got, 1024u), OVLSD_DONE);
    CHECK(got == o->d.clear_len && memcmp(buf, o->clr, got) == 0);
    CHECK_RC(stream_back(s, OVLSD_PARTIAL, buf, o->d.part_len, &got, 1460u & ~3u), OVLSD_DONE);
    CHECK(got == o->d.part_len && memcmp(buf, o->part, got) == 0);
    free(buf);
    CHECK(ovlstore_sd_state(s) == OVLSD_VALID);
}

/* Absolute LBA of partition + slot. */
static uint32_t slot_lba(const ovlstore_sd_t *s, int k)
{
    ovlstore_sd_info_t in;
    ovlstore_sd_info((ovlstore_sd_t *)s, &in);
    return in.part_lba + (k ? OVLSD_SLOT_B_LBA : OVLSD_SLOT_A_LBA);
}

static void blank_and_format(ovlstore_sd_t *s, card_t *c, uint32_t nblocks, uint32_t live)
{
    card_new(c, nblocks);
    store_init(s, &c->bd, live);
    settle(s);
    CHECK(ovlstore_sd_state(s) == OVLSD_FOREIGN);
    CHECK_RC(ovlstore_sd_format(s, "erase"), OVLSD_OK);
    CHECK_RC(wait_job(s), OVLSD_OK);
    settle(s);
    CHECK(ovlstore_sd_state(s) == OVLSD_EMPTY);
}

/* =============================== cases ======================================= */

static void test_no_card(void)
{
    static const struct { int st; int err; ovlstore_sd_state_t want; const char *text; int rc; } rows[] = {
        { OVL_BDEV_NONE,        0,  OVLSD_NO_CARD,     "none",        OVLSD_ENOCARD },
        { OVL_BDEV_NO_HW,       11, OVLSD_NO_HW,       "no hw",       OVLSD_ENOHW },
        { OVL_BDEV_INIT,        0,  OVLSD_INIT,        "init",        OVLSD_ENOTREADY },
        { OVL_BDEV_UNSUPPORTED, 22, OVLSD_UNSUPPORTED, "unsupported", OVLSD_ENOTREADY },
        { OVL_BDEV_ERROR,       4,  OVLSD_ERROR,       "ERR 4",       OVLSD_ENOTREADY },
    };
    card_t c;
    ovlstore_sd_t s;
    ovl_t o;

    ovl_make(&o, RM_LED, LIVE, 1000u, 4000u, 1u);
    for (unsigned r = 0; r < sizeof rows / sizeof rows[0]; r++) {
        card_new(&c, 64u * MIB);
        g_cw.force_state = rows[r].st;
        g_cw.force_err = rows[r].err;
        store_init(&s, &c.bd, LIVE);
        CHECK(strcmp(ovlstore_sd_state_text(&s), "none") == 0);   /* before the first poll */
        spoll_n(&s, 3000u);
        CHECK(ovlstore_sd_state(&s) == rows[r].want);
        CHECK(strcmp(ovlstore_sd_state_text(&s), rows[r].text) == 0);
        CHECK_RC(ovlstore_sd_format(&s, "erase"), rows[r].rc);
        CHECK_RC(ovlstore_sd_commit_begin(&s, &o.d, LIVE, RM_LED), rows[r].rc);
        CHECK_RC(ovlstore_sd_clear(&s), rows[r].rc);
        CHECK_RC(ovlstore_sd_rescan(&s), rows[r].rc);
        CHECK(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL) < 0);
        spoll_n(&s, 100u);
        CHECK(g_cw.reads == 0u && g_cw.writes == 0u);        /* ZERO device I/O */
        {
            ovlstore_sd_info_t in;
            ovlstore_sd_info(&s, &in);
            CHECK(in.present == (rows[r].want != OVLSD_NO_CARD && rows[r].want != OVLSD_NO_HW));
            CHECK(!in.have_default && in.card_mb == 0u);
        }
        card_free(&c);
    }
    ovl_free(&o);
    printf("  1 no card / no hw / init / unsupported / ERR n: zero device ops\n");
}

typedef struct { uint32_t lba; const uint8_t *data; } blk_t;

/* A card holding `nb` given blocks (the rest zero): FOREIGN, every write-side
 * call refused with zero writes, and every given block still intact after. */
static void foreign_card(const char *what, const blk_t *blks, unsigned nb, int format_rc)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t o;

    ovl_make(&o, RM_LED, LIVE, 1000u, 4000u, 2u);
    card_new(&c, 64u * MIB);
    for (unsigned i = 0; i < nb; i++) {
        img_write(&c, blks[i].lba, blks[i].data, 512);
    }
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "foreign") == 0);
    {
        ovlstore_sd_info_t in;
        ovlstore_sd_info(&s, &in);
        CHECK(in.part_blocks == 0u || in.mbr == OVLSD_MBR_DA);  /* nothing writable recorded */
    }
    CHECK_RC(ovlstore_sd_format(&s, NULL), OVLSD_ECONFIRM);
    CHECK_RC(ovlstore_sd_format(&s, "yes"), OVLSD_ECONFIRM);
    CHECK_RC(ovlstore_sd_format(&s, "ERASE-ALL"), OVLSD_ECONFIRM);
    CHECK_RC(ovlstore_sd_format(&s, "erase-all "), OVLSD_ECONFIRM);
    CHECK_RC(ovlstore_sd_format(&s, "erase all"), OVLSD_ECONFIRM);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), format_rc);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &o.d, LIVE, RM_LED), OVLSD_EFOREIGN);
    CHECK_RC(ovlstore_sd_clear(&s), OVLSD_EFOREIGN);
    CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL), OVLSD_EFOREIGN);
    spoll_n(&s, 500u);
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    CHECK(g_cw.writes == 0u);                            /* ZERO writes, ever */
    for (unsigned i = 0; i < nb; i++) {
        uint8_t now[512];
        img_read(&c, blks[i].lba, now, 512);
        CHECK(memcmp(now, blks[i].data, 512) == 0);
    }
    card_free(&c);
    ovl_free(&o);
    printf("  2 foreign (%s): format -> \"%s\", zero writes\n", what, ovlstore_sd_rc_name(format_rc));
}

static void foreign_case(const char *what, const uint8_t lba0[512], int format_rc)
{
    blk_t b = { 0u, lba0 };
    foreign_card(what, &b, 1u, format_rc);
}

/* ---- the "not truly blank" images ------------------------------------------ */

static const uint8_t k_zero[512];

/* ext4 on the whole disk: bytes 0-1023 are padding (zero), the superblock
 * starts at byte 1024, s_magic 0xEF53 at byte 1080 = LBA 2 + 56. */
static void mk_ext4_sb(uint8_t b[512])
{
    memset(b, 0, 512);
    put32le(b + 0, 8192u);            /* s_inodes_count */
    put32le(b + 4, 32768u);           /* s_blocks_count_lo */
    put32le(b + 24, 2u);              /* s_log_block_size: 4 KiB */
    b[56] = 0x53u; b[57] = 0xEFu;     /* s_magic */
    b[58] = 1u;                       /* s_state: clean */
    memcpy(b + 120, "rootfs", 6);     /* s_volume_name */
}

/* exFAT on the whole disk: the main boot sector at LBA 0. */
static void mk_exfat_bs(uint8_t b[512])
{
    memset(b, 0, 512);
    b[0] = 0xEBu; b[1] = 0x76u; b[2] = 0x90u;
    memcpy(b + 3, "EXFAT   ", 8);
    put32le(b + 72, 64u * MIB);       /* VolumeLength (low word) */
    b[108] = 9u;                      /* BytesPerSectorShift */
    b[109] = 6u;                      /* SectorsPerClusterShift */
    b[510] = 0x55u; b[511] = 0xAAu;
}

/* A GPT header at LBA 1. */
static void mk_gpt_hdr(uint8_t b[512])
{
    memset(b, 0, 512);
    memcpy(b, "EFI PART", 8);
    put32le(b + 8, 0x00010000u);      /* revision 1.0 */
    put32le(b + 12, 92u);             /* header size */
    put32le(b + 24, 1u);              /* my LBA */
    put32le(b + 72, 2u);              /* entries LBA */
    put32le(b + 80, 128u);            /* number of entries */
    put32le(b + 84, 128u);            /* entry size */
}

static void mk_magic(uint8_t b[512], unsigned off, const char *m, unsigned len)
{
    memset(b, 0, 512);
    memcpy(b + off, m, len);
}

static void test_foreign(void)
{
    uint8_t b[512];
    pent_t e[4];

    /* A PC card: one FAT32 partition. */
    memset(e, 0, sizeof e);
    e[0].type = 0x0Cu; e[0].start = 8192u; e[0].cnt = 64u * MIB - 8192u;
    mk_mbr(b, e);
    foreign_case("FAT32 MBR", b, OVLSD_EEXIST);
    CHECK(strcmp(ovlstore_sd_rc_name(OVLSD_EEXIST), "provision p4 first") == 0);

    /* GPT protective MBR. */
    memset(e, 0, sizeof e);
    e[0].type = 0xEEu; e[0].start = 1u; e[0].cnt = 64u * MIB - 1u;
    mk_mbr(b, e);
    foreign_case("GPT", b, OVLSD_EEXIST);

    /* FAT32 superfloppy: a volume boot sector at LBA 0, no partition table. */
    memset(b, 0, sizeof b);
    b[0] = 0xEBu; b[1] = 0x58u; b[2] = 0x90u;
    memcpy(b + 3, "MSDOS5.0", 8);
    b[11] = 0x00u; b[12] = 0x02u; b[13] = 8u;
    memcpy(b + 0x52, "FAT32   ", 8);
    memcpy(b + 0x1B0, "Remove disks", 12);
    b[510] = 0x55u; b[511] = 0xAAu;
    foreign_case("FAT superfloppy", b, OVLSD_EFSSIG);
    b[510] = 0u; b[511] = 0u;                                  /* even without 0x55AA */
    foreign_case("FAT superfloppy, unsigned", b, OVLSD_EFSSIG);

    /* Garbage without a signature. */
    for (unsigned i = 0; i < 512u; i++) b[i] = (uint8_t)(i * 7u + 3u);
    b[510] = 0x12u;
    foreign_case("no signature", b, OVLSD_EUNKNOWN);

    /* All 0xFF is NOT blank (only all-zero is). */
    memset(b, 0xFF, sizeof b);
    foreign_case("all 0xFF", b, OVLSD_EUNKNOWN);

    /* A signed MBR whose entries hold bytes but no type: not "zero entries". */
    memset(e, 0, sizeof e);
    mk_mbr(b, e);
    put32le(b + 446u + 16u + 8u, 2048u);
    foreign_case("typeless entry bytes", b, OVLSD_EUNKNOWN);

    /* NOT TRULY BLANK: LBA 0 says blank, the blocks behind it do not. */
    {
        uint8_t sb[512], gh[512], m[512], mbr0[512];
        pent_t z[4];
        blk_t ext4[] = { { 2u, sb } };
        blk_t gpt0[] = { { 1u, gh } };
        blk_t emp_ext4[] = { { 0u, mbr0 }, { 2u, sb } };
        memset(z, 0, sizeof z);
        mk_mbr(mbr0, z);
        mk_ext4_sb(sb);
        mk_gpt_hdr(gh);
        foreign_card("whole-disk ext4", ext4, 1u, OVLSD_EFSSIG);
        foreign_card("empty MBR + ext4 behind it", emp_ext4, 2u, OVLSD_EFSSIG);
        foreign_card("GPT header, LBA 0 zeroed", gpt0, 1u, OVLSD_EFSSIG);
        mk_exfat_bs(m);
        foreign_case("whole-disk exFAT", m, OVLSD_EFSSIG);
        {
            blk_t f2[] = { { 2u, m } };
            mk_magic(m, 0u, "\x10\x20\xF5\xF2", 4u);
            foreign_card("whole-disk f2fs", f2, 1u, OVLSD_EFSSIG);
        }
        {
            blk_t bt[] = { { 128u, m } };
            mk_magic(m, 64u, "_BHRfS_M", 8u);
            foreign_card("whole-disk btrfs", bt, 1u, OVLSD_EFSSIG);
        }
        {
            blk_t sw[] = { { 7u, m } };
            mk_magic(m, 502u, "SWAPSPACE2", 10u);
            foreign_card("whole-disk swap", sw, 1u, OVLSD_EFSSIG);
        }
        {
            blk_t iso[] = { { 64u, m } };
            mk_magic(m, 1u, "CD001", 5u);
            foreign_card("ISO 9660", iso, 1u, OVLSD_EFSSIG);
        }
        {
            /* A full GPT card: protective MBR + header. Rule (c). */
            blk_t g[] = { { 0u, mbr0 }, { 1u, gh } };
            pent_t pe[4];
            memset(pe, 0, sizeof pe);
            pe[0].type = 0xEEu; pe[0].start = 1u; pe[0].cnt = 64u * MIB - 1u;
            mk_mbr(mbr0, pe);
            foreign_card("GPT card", g, 2u, OVLSD_EEXIST);
        }
    }
    CHECK(strcmp(ovlstore_sd_rc_name(OVLSD_EFSSIG), "filesystem present") == 0);

    /* A 0xDA entry past the end of the card, and one too small. */
    memset(e, 0, sizeof e);
    e[3].type = 0xDAu; e[3].start = 60u * MIB; e[3].cnt = 32u * MIB;
    mk_mbr(b, e);
    foreign_case("0xDA out of bounds", b, OVLSD_EPART);
    memset(e, 0, sizeof e);
    e[3].type = 0xDAu; e[3].start = 48u * MIB; e[3].cnt = 16u * MIB;
    mk_mbr(b, e);
    foreign_case("0xDA 16 MiB", b, OVLSD_ESMALL);

    /* THE OVERLAP GUARD (lane HARDEN, 2026-09-24). A 0xDA entry that starts in
     * LBA 0-2 (the MBR, stage0's boot-select copies) or shares a block with any
     * other typed entry would put the store's header/slot writes inside someone
     * else's data: DA_BAD -> FOREIGN, format "bad 0xDA partition", ZERO writes,
     * and every given block intact (foreign_card). */
    memset(e, 0, sizeof e);
    e[3].type = 0xDAu; e[3].start = 1u; e[3].cnt = 32u * MIB;
    mk_mbr(b, e);
    foreign_case("0xDA from LBA 1 (boot-select copy 0)", b, OVLSD_EPART);
    e[3].start = 2u;
    mk_mbr(b, e);
    foreign_case("0xDA from LBA 2 (boot-select copy 1)", b, OVLSD_EPART);
    memset(e, 0, sizeof e);                                  /* over stage0's slot A */
    e[0].type = 0x7Fu; e[0].start = 4u * MIB; e[0].cnt = 4u * MIB;
    e[3].type = 0xDAu; e[3].start = 1u * MIB; e[3].cnt = 32u * MIB;
    mk_mbr(b, e);
    foreign_case("0xDA over a 0x7F slot", b, OVLSD_EPART);
    memset(e, 0, sizeof e);                                  /* ONE shared block */
    e[2].type = 0x83u; e[2].start = 33u * MIB - 1u; e[2].cnt = 8u * MIB;
    e[3].type = 0xDAu; e[3].start = 1u * MIB; e[3].cnt = 32u * MIB;
    mk_mbr(b, e);
    foreign_case("0xDA sharing its last block with /persist", b, OVLSD_EPART);
    memset(e, 0, sizeof e);                                  /* two 0xDA, overlapping */
    e[0].type = 0xDAu; e[0].start = 1u * MIB; e[0].cnt = 20u * MIB;
    e[3].type = 0xDAu; e[3].start = 10u * MIB; e[3].cnt = 20u * MIB;
    mk_mbr(b, e);
    foreign_case("two overlapping 0xDA entries", b, OVLSD_EPART);
    memset(e, 0, sizeof e);                                  /* a hybrid GPT MBR */
    e[0].type = 0xEEu; e[0].start = 1u; e[0].cnt = 64u * MIB - 1u;
    e[3].type = 0xDAu; e[3].start = 32u * MIB; e[3].cnt = 32u * MIB;
    mk_mbr(b, e);
    foreign_case("0xDA inside a GPT protective entry", b, OVLSD_EPART);
    {
        /* ...and the controls that must STILL be the store: the STAGE0_CONTRACT
         * §6 card exactly (p4 0xDA @2048 x 65536 ends where slot A starts), the
         * lowest legal start (LBA 3), a typed-but-EMPTY entry inside the 0xDA
         * range (no blocks, no overlap), and entries that only touch it. */
        uint32_t pl = 0u, pc = 0u;
        pent_t s0[4] = {
            { 0x7Fu, 67584u, 131072u },          /* p1 stage0 slot A */
            { 0x7Fu, 198656u, 131072u },         /* p2 stage0 slot B */
            { 0x83u, 329728u, 819200u - 329728u },   /* p3 /persist (the rest) */
            { 0xDAu, 2048u, 65536u },            /* p4 D13's store */
        };
        mk_mbr(b, s0);
        CHECK(ovlstore_sd_mbr_classify(b, 819200u, &pl, &pc) == OVLSD_MBR_DA);
        CHECK(pl == 2048u && pc == 65536u);
        memset(e, 0, sizeof e);
        e[3].type = 0xDAu; e[3].start = OVLSD_MBR_FIRST_LBA; e[3].cnt = 32u * MIB;
        mk_mbr(b, e);
        CHECK(ovlstore_sd_mbr_classify(b, 64u * MIB, &pl, &pc) == OVLSD_MBR_DA);
        CHECK(pl == 3u);
        e[1].type = 0x83u; e[1].start = 4u * MIB; e[1].cnt = 0u;   /* typed, empty */
        e[0].type = 0x7Fu; e[0].start = 3u + 32u * MIB; e[0].cnt = 4u * MIB;  /* touches */
        mk_mbr(b, e);
        CHECK(ovlstore_sd_mbr_classify(b, 64u * MIB, &pl, &pc) == OVLSD_MBR_DA);
        printf("  2 0xDA overlap guard: LBA 1/2 starts, a 0x7F / 0x83 / 0xDA / 0xEE overlap "
               "refused; the STAGE0 card, LBA 3 and touching entries still the store\n");
    }

    /* The classifier directly: a GRUB-style MBR (EB 63 90 jump, entries) is an MBR. */
    memset(e, 0, sizeof e);
    e[0].type = 0x83u; e[0].start = 2048u; e[0].cnt = 1000u;
    mk_mbr(b, e);
    b[0] = 0xEBu; b[1] = 0x63u; b[2] = 0x90u;
    CHECK(ovlstore_sd_mbr_classify(b, 64u * MIB, NULL, NULL) == OVLSD_MBR_OTHER);
}

static void test_blank(void)
{
    card_t c;
    ovlstore_sd_t s;
    uint8_t mbr[512], h[1024];
    ovlstore_header_t hd;
    ovlstore_sd_info_t in;

    /* All-zero card. */
    card_new(&c, 64u * MIB);
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    ovlstore_sd_info(&s, &in);
    CHECK(in.mbr == OVLSD_MBR_BLANK && in.card_mb == 64u);
    CHECK(in.part_blocks == 0u);                               /* nothing writable recorded */
    g_cw.allow_lo = 32u * MIB;
    g_cw.allow_hi = 64u * MIB;
    g_cw.allow_lba0 = 1;
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_EBUSY);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "empty") == 0);
    CHECK(g_cw.writes == 3u && g_cw.writes_outside == 0u);   /* MBR + two header copies */
    img_read(&c, 0, mbr, 512);
    CHECK(mbr[510] == 0x55u && mbr[511] == 0xAAu);
    for (unsigned i = 0; i < 3u; i++) {
        for (unsigned j = 0; j < 16u; j++) CHECK(mbr[446u + 16u * i + j] == 0u);
    }
    CHECK(mbr[446u + 48u + 4u] == 0xDAu);
    CHECK(get32le(mbr + 446u + 48u + 8u) == 32u * MIB);        /* the last 32 MiB */
    CHECK(get32le(mbr + 446u + 48u + 12u) == 32u * MIB);
    CHECK(get32le(mbr + 440u) == OVLSD_MBR_DISK_SIGNATURE);
    ovlstore_sd_info(&s, &in);
    CHECK(in.part_lba == 32u * MIB && in.part_blocks == 32u * MIB && in.mbr == OVLSD_MBR_DA);
    img_read(&c, 32u * MIB, h, 1024);
    CHECK(ovlstore_header_unpack(h, OVLSTORE_HEADER_PACKED_SIZE, &hd) == OVLSTORE_OK);
    CHECK(get32le(h + 74) == 1u && get32le(h + 512 + 74) == 2u);
    CHECK(get32le(h + 78) == mps3_crc32(h, 78));
    CHECK(get32le(h + 512 + 78) == mps3_crc32(h + 512, 78));
    CHECK(hd.slot[0].valid == 0u && hd.slot[1].valid == 0u);
    /* (a) on the now-harness card: re-initialise, headers only. */
    g_cw.allow_lba0 = 0;
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY && g_cw.writes == 5u && g_cw.writes_outside == 0u);
    card_free(&c);

    /* An odd-sized blank card: start aligned DOWN to 1 MiB, entry to the end. */
    card_new(&c, 131000u);
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
    img_read(&c, 0, mbr, 512);
    CHECK(get32le(mbr + 446u + 48u + 8u) == 63488u);
    CHECK(get32le(mbr + 446u + 48u + 12u) == 131000u - 63488u);
    card_free(&c);

    /* An MBR with no entries but boot code: the code and signature are kept. */
    card_new(&c, 64u * MIB);
    {
        pent_t e[4];
        memset(e, 0, sizeof e);
        mk_mbr(mbr, e);
        img_write(&c, 0, mbr, 512);
    }
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
    {
        uint8_t now[512];
        img_read(&c, 0, now, 512);
        CHECK(memcmp(now, mbr, 446) == 0);                     /* boot code + signature */
        CHECK(now[446u + 48u + 4u] == 0xDAu);
    }
    card_free(&c);

    /* Too small for rule (b): 32 MiB. Refused, zero writes. */
    card_new(&c, 32u * MIB);
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_ESMALL);
    spoll_n(&s, 100u);
    CHECK(g_cw.writes == 0u);
    card_free(&c);
    printf("  3 blank card: 0xDA over the last 32 MiB (entry 4), EMPTY; (a) re-init; too small refused\n");
}

static void test_linux_provisioned(void)
{
    enum { NB = 64u * MIB, P4 = 32u * MIB };
    card_t c;
    ovlstore_sd_t s;
    pent_t e[4] = {
        { 0x7Fu, 1u * MIB,  4u * MIB },   /* p1 raw (image slot A) */
        { 0x7Fu, 5u * MIB,  4u * MIB },   /* p2 raw (image slot B) */
        { 0x83u, 9u * MIB, 23u * MIB },   /* p3 ext4 /persist       */
        { 0xDAu, P4,       32u * MIB },   /* p4 the overlay store   */
    };
    uint8_t mbr[512];
    uint8_t *before = malloc((size_t)P4 * 512u), *after = malloc((size_t)P4 * 512u);
    ovl_t o;

    CHECK(before && after);
    card_new(&c, NB);
    mk_mbr(mbr, e);
    img_write(&c, 0, mbr, 512);
    for (uint32_t lba = 1u * MIB; lba < P4; lba += 97u) {       /* p1..p3 content */
        uint8_t blk[512];
        for (unsigned i = 0; i < 512u; i++) blk[i] = (uint8_t)(lba * 31u + i);
        img_write(&c, lba, blk, 512);
    }
    img_read(&c, 0, before, P4 * 512u);

    store_init(&s, &c.bd, LIVE);
    g_cw.allow_lo = P4;
    g_cw.allow_hi = NB;
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);          /* p4 exists, never formatted */
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);     /* rule (a) */
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
    CHECK(g_cw.writes == 2u && g_cw.writes_outside == 0u);

    ovl_make(&o, RM_LED, LIVE, 3000u, 20000u, 4u);
    CHECK_RC(do_commit(&s, &o, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &o, 'A');
    CHECK(g_cw.writes_outside == 0u);

    img_read(&c, 0, after, P4 * 512u);
    CHECK(memcmp(before, after, (size_t)P4 * 512u) == 0);    /* MBR + p1..p3 untouched */
    ovl_free(&o);
    free(before);
    free(after);
    card_free(&c);
    printf("  4 Linux-provisioned card: format + commit wrote only inside p4\n");
}

static void test_commits(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t a, b, d;
    ovlstore_sd_info_t in;
    uint8_t h[1024];

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    g_cw.allow_lo = 32u * MIB;
    g_cw.allow_hi = 64u * MIB;
    ovl_make(&a, RM_LED, LIVE, 3000u, 50000u, 10u);
    ovl_make(&b, RM_MC, LIVE, 5000u, 70004u, 11u);
    ovl_make(&d, RM_LED, LIVE, 512u, 1024u, 12u);           /* block-exact sizes */

    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    CHECK(ovlstore_sd_commit_slot(&s) == 'A');
    CHECK(strcmp(ovlstore_sd_state_text(&s), "led [A]") == 0);
    expect_default(&s, &a, 'A');

    CHECK_RC(do_commit(&s, &b, LIVE, 4096u), OVLSD_OK);
    CHECK(ovlstore_sd_commit_slot(&s) == 'B');
    CHECK(strcmp(ovlstore_sd_state_text(&s), "nanosoc_mult [B]") == 0);
    expect_default(&s, &b, 'B');

    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);              /* from scratch */
    CHECK(ovlstore_sd_state(&s) == OVLSD_INIT);
    settle(&s);
    expect_default(&s, &b, 'B');
    ovlstore_sd_info(&s, &in);
    CHECK(in.have_default && in.slot == 'B' && !in.fallback && in.def.rm_id == RM_MC);
    CHECK(strcmp(in.state_name, "valid") == 0);

    CHECK_RC(do_commit(&s, &d, LIVE, 7u * 4u), OVLSD_OK);   /* tiny odd chunks */
    expect_default(&s, &d, 'A');
    CHECK(g_cw.writes_outside == 0u);
    img_read(&c, 32u * MIB, h, 1024);
    CHECK(get32le(h + 74) == 5u && get32le(h + 512 + 74) == 4u); /* format 1,2; commits 3,4,5 */

    /* clear(): one header write, the next boot is greybox. */
    {
        uint32_t w0 = g_cw.writes;
        CHECK_RC(ovlstore_sd_clear(&s), OVLSD_OK);
        CHECK_RC(wait_job(&s), OVLSD_OK);
        CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY && g_cw.writes == w0 + 1u);
        CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
        CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL), OVLSD_ENODEFAULT);
        CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);  /* EMPTY -> slot A again */
        expect_default(&s, &a, 'A');
        CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_OK);
        expect_default(&s, &b, 'B');
        CHECK_RC(ovlstore_sd_clear(&s), OVLSD_OK);            /* clear with the default in B */
        CHECK_RC(wait_job(&s), OVLSD_OK);
        CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
    }
    /* A stream aborted with a read in flight, and a new one begun at once:
     * the abandoned read's result must not land in the new stream. */
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_OK);
    for (int k = 0; k < 3; k++) {
        CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL), OVLSD_OK);
        spoll_n(&s, (uint32_t)k + 1u);
        ovlstore_sd_stream_abort(&s);
        CHECK_RC(ovlstore_sd_job_result(&s), OVLSD_EABORTED);
        expect_default(&s, &b, 'A');
    }
    ovl_free(&a);
    ovl_free(&b);
    ovl_free(&d);
    card_free(&c);
    printf("  5 commit A -> VALID [A], B -> VALID [B], byte-exact; rescan; clear\n");
}

static void test_torn_commit(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t a, b;
    uint32_t w0;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    ovl_make(&a, RM_LED, LIVE, 3000u, 60000u, 20u);
    ovl_make(&b, RM_MC, LIVE, 8000u, 90000u, 21u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &a, 'A');
    g_cw.allow_lo = slot_lba(&s, 1);                          /* the target: slot B */
    g_cw.allow_hi = slot_lba(&s, 1) + OVLSD_SLOT_BLOCKS;

    /* (a) abort mid-feed */
    CHECK_RC(ovlstore_sd_commit_begin(&s, &b.d, LIVE, RM_MC), OVLSD_OK);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &b.d, LIVE, RM_MC), OVLSD_EBUSY);
    CHECK_RC(feed_region(&s, OVLSD_CLEARING, b.clr, b.d.clear_len, 1460u, 0xFFFFFFFFu), OVLSD_OK);
    CHECK_RC(feed_region(&s, OVLSD_PARTIAL, b.part, b.d.part_len, 1460u, b.d.part_len / 2u), OVLSD_OK);
    spoll_n(&s, 5u);
    ovlstore_sd_commit_abort(&s);
    CHECK_RC(ovlstore_sd_job_result(&s), OVLSD_EABORTED);
    {
        uint32_t got = 0;
        CHECK_RC(ovlstore_sd_commit_feed(&s, OVLSD_PARTIAL, b.part, 4u, &got), OVLSD_EABORTED);
    }
    settle(&s);
    expect_default(&s, &a, 'A');
    CHECK(g_cw.writes_outside == 0u);

    /* (b) a write fails mid-slot */
    w0 = g_cw.writes;
    g_cw.fail_write_at = w0 + 5u;
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_EIO);
    g_cw.fail_write_at = 0u;
    settle(&s);
    expect_default(&s, &a, 'A');

    /* (c) a torn data block */
    g_cw.tear_write_at = g_cw.writes + 3u;
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_EIO);
    g_cw.tear_write_at = 0u;
    settle(&s);
    expect_default(&s, &a, 'A');

    /* (d) a block the card acknowledged but stored wrong: the read-back catches it */
    g_cw.corrupt_write_at = g_cw.writes + 4u;
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_EVERIFY);
    g_cw.corrupt_write_at = 0u;
    settle(&s);
    expect_default(&s, &a, 'A');
    CHECK(g_cw.writes_outside == 0u);                          /* never the old slot, never a header */

    /* and then it goes through */
    g_cw.allow_lo = 32u * MIB;
    g_cw.allow_hi = 64u * MIB;
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &b, 'B');
    ovl_free(&a);
    ovl_free(&b);
    card_free(&c);
    printf("  6 torn commit (abort, write fail, torn block, silent corruption): old default intact\n");
}

static void test_torn_header(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t a, b, d;
    uint32_t part;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    part = 32u * MIB;
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 30u);
    ovl_make(&b, RM_MC, LIVE, 4000u, 40000u, 31u);
    ovl_make(&d, RM_ANON, LIVE, 5000u, 50000u, 32u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);      /* seq 3 -> copy 0 */
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_OK);      /* seq 4 -> copy 1 */
    expect_default(&s, &b, 'B');

    /* The next header write goes to copy 0: tear it. */
    g_cw.tear_lba_armed = 1u;
    g_cw.tear_lba = part + OVLSD_HDR_COPY0_LBA;
    CHECK_RC(do_commit(&s, &d, LIVE, 1460u), OVLSD_EIO);
    CHECK(g_cw.tear_lba_armed == 0u);
    settle(&s);
    expect_default(&s, &b, 'B');                              /* copy 1, seq 4 */

    /* A good commit, then its header copy rots on the card: the previous copy
     * (seq 4, B active) is used, and B was never touched. */
    CHECK_RC(do_commit(&s, &d, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &d, 'A');
    CHECK(strcmp(ovlstore_sd_state_text(&s), "0x0000002B [A]") == 0);
    img_flip(&c, (uint64_t)(part + OVLSD_HDR_COPY0_LBA) * 512u + 7u);   /* `flags`: only the CRC sees it */
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    expect_default(&s, &b, 'B');
    ovl_free(&a);
    ovl_free(&b);
    ovl_free(&d);
    card_free(&c);
    printf("  7 torn header write: the previous copy is used\n");
}

static void test_stale(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t a, y;
    uint32_t r0;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 40u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &a, 'A');

    /* The board is re-keyed: the same card under a shell with another static_id. */
    store_init(&s, &c.bd, OTHER);
    g_cw.watch_lo = 32u * MIB + OVLSD_SLOT_A_LBA;
    g_cw.watch_hi = 32u * MIB + OVLSD_SLOT_B_LBA + OVLSD_SLOT_BLOCKS;
    g_cw.watch_reads = 0u;
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_STALE);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "stale key") == 0);
    r0 = g_cw.reads;
    CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL), OVLSD_ESTALE);
    CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_CLEARING), OVLSD_ESTALE);
    CHECK_RC(ovlstore_sd_default(&s, NULL, NULL), OVLSD_ENODEFAULT);
    spoll_n(&s, 1000u);
    CHECK(g_cw.reads == r0);
    CHECK(g_cw.watch_reads == 0u);                            /* not one slot block read */
    {
        ovlstore_sd_info_t in;
        ovlstore_sd_info(&s, &in);
        CHECK(strcmp(in.state_name, "stale") == 0 && !in.have_default);
    }

    /* The old mint's pair is refused; the new mint's is accepted (recovery). */
    CHECK_RC(ovlstore_sd_commit_begin(&s, &a.d, LIVE, RM_LED), OVLSD_ESTATIC);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &a.d, OTHER, RM_LED), OVLSD_ESTATIC);
    ovl_make(&y, RM_LED, OTHER, 3000u, 30000u, 41u);
    CHECK_RC(do_commit(&s, &y, OTHER, 1460u), OVLSD_OK);
    expect_default(&s, &y, 'B');                              /* the stale slot A is kept */

    /* Back on the old shell: B is now the stale one. No fallback to A. */
    store_init(&s, &c.bd, LIVE);
    g_cw.watch_reads = 0u;
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_STALE && g_cw.watch_reads == 0u);
    ovl_free(&a);
    ovl_free(&y);
    card_free(&c);
    printf("  8 stale static_id: STALE, stream refused, zero slot reads; stale card committed over\n");
}

/* Write a hand-made header copy (seq, crc) at partition-relative `copy`. */
static void write_hdr_copy(card_t *c, uint32_t part, unsigned copy, const ovlstore_header_t *h, uint32_t seq)
{
    uint8_t b[512];
    memset(b, 0, sizeof b);
    ovlstore_header_pack(h, b);
    put32le(b + 74, seq);
    put32le(b + 78, mps3_crc32(b, 78));
    img_write(c, part + copy, b, 512);
}

static void test_fallback(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t a, b, d;
    uint32_t part = 32u * MIB;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 50u);
    ovl_make(&b, RM_MC, LIVE, 4000u, 40000u, 51u);
    ovl_make(&d, RM_ANON, LIVE, 1000u, 9000u, 52u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &b, 'B');

    /* B's partial rots: A (valid, same static_id) is used instead. */
    img_flip(&c, (uint64_t)(part + OVLSD_SLOT_B_LBA + 8u + 30u) * 512u + 100u);
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    expect_default(&s, &a, 'A');
    {
        ovlstore_sd_info_t in;
        ovlstore_sd_info(&s, &in);
        CHECK(in.fallback && in.slot == 'A');
    }
    /* The card goes bad AFTER the verify: the re-read stream fails its CRC and
     * the region's last buffer is never handed out (the bitstream stays
     * incomplete), then the store re-probes. */
    {
        uint8_t *buf = malloc(a.d.part_len);
        uint32_t got = 0;
        CHECK(buf != NULL);
        img_flip(&c, (uint64_t)(part + OVLSD_SLOT_A_LBA + 6u + 40u) * 512u + 3u);   /* A's partial */
        CHECK_RC(stream_back(&s, OVLSD_PARTIAL, buf, a.d.part_len, &got, 1460u), OVLSD_ECRC);
        CHECK(got < a.d.part_len);
        CHECK(a.d.part_len - got <= OVLSTORE_SD_OP_BLOCKS * 512u);                 /* only the last buffer */
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_BAD);           /* A now fails too, B was already bad */
        img_flip(&c, (uint64_t)(part + OVLSD_SLOT_A_LBA + 6u + 40u) * 512u + 3u);   /* heal it */
        CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
        settle(&s);
        expect_default(&s, &a, 'A');
        free(buf);
    }
    /* A commit now overwrites the CORRUPT slot, never the good one. */
    CHECK_RC(do_commit(&s, &d, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &d, 'B');
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    expect_default(&s, &d, 'B');

    /* Both bad: BAD, nothing streams. */
    img_flip(&c, (uint64_t)(part + OVLSD_SLOT_A_LBA) * 512u + 7u);               /* A's clearing */
    img_flip(&c, (uint64_t)(part + OVLSD_SLOT_B_LBA + 2u + 3u) * 512u + 9u);      /* D's partial */
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_BAD);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "bad") == 0);
    {
        uint32_t r0 = g_cw.reads;
        CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL), OVLSD_ENODEFAULT);
        spoll_n(&s, 200u);
        CHECK(g_cw.reads == r0);
    }
    /* A BAD card may be committed over: the corrupt ACTIVE slot is the target. */
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &a, 'B');
    card_free(&c);

    /* The other slot is stale: BAD, not a fallback onto another mint's bits. */
    blank_and_format(&s, &c, 64u * MIB, LIVE);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);       /* A: LIVE */
    {
        ovl_t y;
        store_init(&s, &c.bd, OTHER);
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_STALE);
        ovl_make(&y, RM_MC, OTHER, 4000u, 40000u, 53u);
        CHECK_RC(do_commit(&s, &y, OTHER, 1460u), OVLSD_OK);  /* B: OTHER, active */
        expect_default(&s, &y, 'B');
        img_flip(&c, (uint64_t)(part + OVLSD_SLOT_B_LBA + 20u) * 512u);
        CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_BAD);            /* A is LIVE's, not OTHER's */
        ovl_free(&y);
    }
    card_free(&c);

    /* A descriptor that cannot be right (active A, part_len 3) -> fallback to B. */
    blank_and_format(&s, &c, 64u * MIB, LIVE);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_OK);
    {
        ovlstore_header_t h;
        uint8_t raw[512];
        img_read(&c, part + OVLSD_HDR_COPY1_LBA, raw, 512);   /* seq 4, active B */
        CHECK(ovlstore_header_unpack(raw, OVLSTORE_HEADER_PACKED_SIZE, &h) == OVLSTORE_OK);
        h.active_slot = 0u;
        h.slot[0].part_len = 3u;
        write_hdr_copy(&c, part, OVLSD_HDR_COPY0_LBA, &h, 9u);
    }
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    expect_default(&s, &b, 'B');
    /* Descriptors whose CRCs MATCH but that break the layout rules: each one
     * must be rejected (fallback to B), never read as a default. */
    {
        ovlstore_header_t h, base;
        uint8_t raw[512];
        img_read(&c, part + OVLSD_HDR_COPY1_LBA, raw, 512);
        CHECK(ovlstore_header_unpack(raw, OVLSTORE_HEADER_PACKED_SIZE, &base) == OVLSTORE_OK);
        for (int k = 0; k < 3; k++) {
            h = base;
            h.active_slot = 0u;
            h.slot[0] = base.slot[0];
            if (k == 0) {            /* partial not a whole number of words */
                h.slot[0].part_len = 5u;
                h.slot[0].part_crc = mps3_crc32(a.part, 5u);
            } else if (k == 1) {     /* partial offset not block-aligned */
                h.slot[0].part_off += 4u;
                h.slot[0].part_len = 16u;
                h.slot[0].part_crc = mps3_crc32(a.part, 16u);   /* what off/512 would read */
            } else {                 /* partial runs out of slot A into slot B */
                h.slot[0].part_off = OVLSD_SLOT_BYTES - 512u;
                h.slot[0].part_len = 1024u;
                {
                    uint8_t tmp[1024];
                    img_read(&c, part + OVLSD_SLOT_A_LBA + OVLSD_SLOT_BLOCKS - 1u, tmp, 1024);
                    h.slot[0].part_crc = mps3_crc32(tmp, 1024u);
                }
            }
            write_hdr_copy(&c, part, OVLSD_HDR_COPY0_LBA, &h, 20u + (uint32_t)k);
            CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
            settle(&s);
            expect_default(&s, &b, 'B');
        }
    }
    ovl_free(&a);
    ovl_free(&b);
    ovl_free(&d);
    card_free(&c);
    printf("  9 CRC fallback to the other slot; both bad -> BAD; stale other -> BAD\n");
}

static void test_refusals(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t a, big;
    ovlstore_sd_desc_t d;
    uint32_t w0, got = 0;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 60u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    w0 = g_cw.writes;

    d = a.d; d.static_id = OTHER;
    CHECK_RC(ovlstore_sd_commit_begin(&s, &d, LIVE, RM_LED), OVLSD_ESTATIC);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &d, OTHER, RM_LED), OVLSD_ESTATIC);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &a.d, LIVE, RM_MC), OVLSD_ERMID);     /* not what runs */
    d = a.d; d.rm_id = RM_MC;
    CHECK_RC(ovlstore_sd_commit_begin(&s, &d, LIVE, RM_LED), OVLSD_ERMID);
    d = a.d; d.clear_len = 0u;
    CHECK_RC(ovlstore_sd_commit_begin(&s, &d, LIVE, RM_LED), OVLSD_EARG);
    d = a.d; d.part_len = 30001u;
    CHECK_RC(ovlstore_sd_commit_begin(&s, &d, LIVE, RM_LED), OVLSD_EARG);
    d = a.d; d.clear_len = 4u * 1024u * 1024u; d.part_len = 4u * 1024u * 1024u + 512u;
    CHECK_RC(ovlstore_sd_commit_begin(&s, &d, LIVE, RM_LED), OVLSD_ETOOBIG);
    CHECK_RC(ovlstore_sd_commit_begin(&s, NULL, LIVE, RM_LED), OVLSD_EARG);
    CHECK_RC(ovlstore_sd_commit_feed(&s, OVLSD_CLEARING, a.clr, 4u, &got), OVLSD_EORDER);
    CHECK_RC(ovlstore_sd_commit_end(&s), OVLSD_EORDER);
    spoll_n(&s, 200u);
    CHECK(g_cw.writes == w0);                                   /* ZERO writes */
    expect_default(&s, &a, 'A');

    /* Out-of-order and over-long feeds, an early end: refused, then abort. */
    CHECK_RC(ovlstore_sd_commit_begin(&s, &a.d, LIVE, RM_LED), OVLSD_OK);
    CHECK_RC(ovlstore_sd_commit_feed(&s, OVLSD_PARTIAL, a.part, 4u, &got), OVLSD_EORDER);
    CHECK_RC(ovlstore_sd_commit_feed(&s, OVLSD_CLEARING, a.clr, 3004u, &got), OVLSD_ELEN);
    CHECK_RC(ovlstore_sd_commit_end(&s), OVLSD_ELEN);
    CHECK_RC(ovlstore_sd_clear(&s), OVLSD_EBUSY);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_EBUSY);
    CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL), OVLSD_EBUSY);
    ovlstore_sd_commit_abort(&s);
    CHECK(g_cw.writes == w0);
    settle(&s);
    expect_default(&s, &a, 'A');

    /* While the default is being verified, the store is busy, not wrong. */
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    spoll_n(&s, 10u);
    CHECK(ovlstore_sd_state(&s) == OVLSD_INIT);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &a.d, LIVE, RM_LED), OVLSD_EBUSY);
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_EBUSY);
    settle(&s);

    /* The largest pair that fits: exactly 8 MiB of blocks. */
    ovl_make(&big, RM_MC, LIVE, 1024u, OVLSD_SLOT_BYTES - 1024u, 61u);
    CHECK_RC(do_commit(&s, &big, LIVE, 65536u), OVLSD_OK);
    expect_default(&s, &big, 'B');
    ovl_free(&a);
    ovl_free(&big);
    card_free(&c);
    printf(" 10 commit refusals (static_id, rm_id, sizes, order, busy): zero writes\n");
}

static void test_sizes(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t o;
    uint32_t p0, polls;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    ovl_make(&o, RM_MC, LIVE, 222000u, 2900000u, 70u);
    p0 = g_polls;
    CHECK_RC(do_commit(&s, &o, LIVE, 1460u), OVLSD_OK);
    polls = g_polls - p0;
    expect_default(&s, &o, 'A');
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    p0 = g_polls;
    settle(&s);
    printf(" 11 sizes: 222000 B clearing + 2900000 B partial: commit %u polls, "
           "power-on verify %u polls\n", (unsigned)polls, (unsigned)(g_polls - p0));
    expect_default(&s, &o, 'A');
    ovl_free(&o);
    card_free(&c);
}

static void test_text_and_errors(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t w, n;
    ovlstore_sd_info_t in;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    ovl_make(&w, RM_WEIRD, LIVE, 1000u, 2000u, 80u);
    CHECK_RC(do_commit(&s, &w, LIVE, 1460u), OVLSD_OK);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "a?b?c [A]") == 0);

    /* PB1 held at boot: "skipped", nothing streams; a commit ends the skip. */
    ovlstore_sd_set_skip(&s, true);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "skipped") == 0);
    CHECK_RC(ovlstore_sd_stream_begin(&s, OVLSD_PARTIAL), OVLSD_ESKIPPED);
    ovlstore_sd_info(&s, &in);
    CHECK(in.skip && in.have_default);
    ovl_make(&n, RM_LED, LIVE, 1000u, 2000u, 81u);
    CHECK_RC(do_commit(&s, &n, LIVE, 1460u), OVLSD_OK);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "led [B]") == 0);

    /* A card pulled while READY clears the skip; "none" wins over "skipped". */
    ovlstore_sd_set_skip(&s, true);
    ovl_bdev_posix_set_present(&c.px, false);
    spoll(&s);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "none") == 0);
    ovl_bdev_posix_set_present(&c.px, true);
    settle(&s);
    expect_default(&s, &n, 'B');

    /* A store read error: ERR 30, retried OVLSTORE_SD_RETRY_MS later. */
    g_cw.fail_read_at = g_cw.reads + 2u;                      /* the first header copy */
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    spoll_n(&s, 5u);
    CHECK(ovlstore_sd_state(&s) == OVLSD_ERROR);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "ERR 30") == 0);
    CHECK(ovlstore_sd_error_code(&s) == OVLSD_ERRNUM_READ);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &n.d, LIVE, RM_LED), OVLSD_ENOTREADY);
    spoll_n(&s, OVLSTORE_SD_RETRY_MS - 10u);
    CHECK(ovlstore_sd_state(&s) == OVLSD_ERROR);
    spoll_n(&s, 20u);
    settle(&s);
    expect_default(&s, &n, 'B');

    /* Three failures in a row: sticky until rescan / card change. */
    g_cw.fail_read_at = g_cw.reads + 1u;                      /* the MBR read */
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    spoll_n(&s, 5u);
    CHECK(ovlstore_sd_state(&s) == OVLSD_ERROR);
    for (unsigned k = 1; k < OVLSTORE_SD_TRIES; k++) {        /* each retry fails too */
        g_cw.fail_read_at = g_cw.reads + 1u;
        spoll_n(&s, OVLSTORE_SD_RETRY_MS + 10u);
        CHECK(ovlstore_sd_state(&s) == OVLSD_ERROR);
    }
    {
        uint32_t r0 = g_cw.reads;
        spoll_n(&s, 3u * OVLSTORE_SD_RETRY_MS);
        CHECK(g_cw.reads == r0 && ovlstore_sd_state(&s) == OVLSD_ERROR);
    }
    g_cw.cc_bump++;                                            /* a new card */
    settle(&s);
    expect_default(&s, &n, 'B');

    /* Every rc has a name, and none is a number. */
    for (int rc = -30; rc <= 2; rc++) {
        const char *nm = ovlstore_sd_rc_name(rc);
        CHECK(nm != NULL && nm[0] != '\0' && !(nm[0] >= '0' && nm[0] <= '9'));
    }
    for (int st = OVLSD_NO_CARD; st <= OVLSD_BAD; st++) {
        CHECK(strlen(ovlstore_sd_state_name((ovlstore_sd_state_t)st)) > 0u);
    }
    ovl_free(&w);
    ovl_free(&n);
    card_free(&c);
    printf(" 13 state text: clipped/sanitised names, hex, skipped, none, ERR 30 + retry\n");
}

/* format re-reads LBA 0 before writing: a card that changed since the probe
 * (another writer under Linux) is judged on what is there NOW. */
static void test_format_recheck(void)
{
    card_t c;
    ovlstore_sd_t s;
    uint8_t mbr[512];
    pent_t e[4];

    card_new(&c, 64u * MIB);
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);            /* blank */
    memset(e, 0, sizeof e);
    e[0].type = 0x0Cu; e[0].start = 8192u; e[0].cnt = 100000u;
    mk_mbr(mbr, e);
    img_write(&c, 0, mbr, 512);                                /* behind the store's back */
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);        /* the probe said blank */
    CHECK_RC(wait_job(&s), OVLSD_EEXIST);                       /* the card says otherwise */
    settle(&s);
    CHECK(g_cw.writes == 0u && ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    card_free(&c);

    /* The same with a filesystem appearing BEHIND a still-blank LBA 0. */
    card_new(&c, 64u * MIB);
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);        /* blank at probe time... */
    CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_EBUSY);   /* no wipe while busy */
    {
        uint8_t sb[512];
        mk_ext4_sb(sb);
        img_write(&c, 2u, sb, 512);                             /* ...mkfs.ext4 since */
    }
    CHECK_RC(wait_job(&s), OVLSD_EFSSIG);
    settle(&s);
    CHECK(g_cw.writes == 0u && ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    card_free(&c);
    printf("    format re-checks the card before writing: changed card refused, zero writes\n");
}

/* The write guard, directly (it is unreachable by correct code). */
static void test_write_guard(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovl_t a;
    const uint32_t part = 32u * MIB;

    blank_and_format(&s, &c, 64u * MIB, LIVE);
    CHECK(!ovlstore_sd_test_write_allowed(&s, part, 1u));      /* no job: nothing */
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 95u);
    CHECK_RC(ovlstore_sd_commit_begin(&s, &a.d, LIVE, RM_LED), OVLSD_OK);   /* target A */
    CHECK(ovlstore_sd_test_write_allowed(&s, part + OVLSD_SLOT_A_LBA, 8u));
    CHECK(ovlstore_sd_test_write_allowed(&s, part + OVLSD_SLOT_A_LBA + OVLSD_SLOT_BLOCKS - 8u, 8u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_SLOT_A_LBA + OVLSD_SLOT_BLOCKS - 7u, 8u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_SLOT_B_LBA, 1u));   /* the other slot */
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_HDR_COPY0_LBA, 1u));/* headers: not yet */
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_HDR_COPY1_LBA, 1u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 0u, 1u));                        /* the MBR */
    CHECK(!ovlstore_sd_test_write_allowed(&s, part - 1u, 2u));                 /* straddles p4 */
    CHECK(!ovlstore_sd_test_write_allowed(&s, 64u * MIB - 1u, 2u));            /* off the card */
    ovlstore_sd_commit_abort(&s);
    settle(&s);
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_SLOT_A_LBA, 1u));
    /* Run a commit up to its header write (copy 0 is the older one: seqs 1, 2). */
    CHECK_RC(ovlstore_sd_commit_begin(&s, &a.d, LIVE, RM_LED), OVLSD_OK);
    CHECK_RC(feed_region(&s, OVLSD_CLEARING, a.clr, a.d.clear_len, 1460u, 0xFFFFFFFFu), OVLSD_OK);
    CHECK_RC(feed_region(&s, OVLSD_PARTIAL, a.part, a.d.part_len, 1460u, 0xFFFFFFFFu), OVLSD_OK);
    CHECK_RC(ovlstore_sd_commit_end(&s), OVLSD_OK);
    for (uint32_t i = 0; i < POLL_LIMIT && g_cw.last_write_lba != part + OVLSD_HDR_COPY0_LBA; i++) {
        spoll(&s);
    }
    CHECK(g_cw.last_write_lba == part + OVLSD_HDR_COPY0_LBA);
    CHECK(ovlstore_sd_test_write_allowed(&s, part + OVLSD_HDR_COPY0_LBA, 1u));   /* the older copy */
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_HDR_COPY1_LBA, 1u));  /* never the newest */
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_HDR_COPY0_LBA, 2u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, part + OVLSD_SLOT_A_LBA, 1u));     /* data is done */
    CHECK_RC(wait_job(&s), OVLSD_OK);
    expect_default(&s, &a, 'A');
    ovl_free(&a);
    card_free(&c);

    /* Raw-partition mode: LBA 0 is a header copy, never "the MBR". */
    {
        ovlstore_sd_cfg_t cfg;
        card_new(&c, 32u * MIB);
        memset(&cfg, 0, sizeof cfg);
        cfg.live_static_id = LIVE;
        cfg.raw_partition = true;
        ovlstore_sd_init(&s, &c.bd, &cfg);
        settle(&s);
        CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
        spoll(&s);                                               /* PH_F_READ -> copy 0 */
        CHECK(ovlstore_sd_test_write_allowed(&s, 0u, 1u));      /* copy 0, due now */
        CHECK(!ovlstore_sd_test_write_allowed(&s, 1u, 1u));     /* copy 1, not yet */
        CHECK(!ovlstore_sd_test_write_allowed(&s, 2u, 1u));
        CHECK(!ovlstore_sd_test_write_allowed(&s, 0u, 2u));
        CHECK_RC(wait_job(&s), OVLSD_OK);
        card_free(&c);
    }
    /* Format rule (b) on a DISK: while the MBR is being written, only LBA 0. */
    card_new(&c, 64u * MIB);
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    {
        uint32_t i;
        /* Reading LBA 0 and walking the signatures: nothing is writable. */
        for (i = 0; i < 100u && !ovlstore_sd_test_write_allowed(&s, 0u, 1u); i++) {
            CHECK(!ovlstore_sd_test_write_allowed(&s, 1u, 1u));
            CHECK(!ovlstore_sd_test_write_allowed(&s, 32u * MIB, 1u));
            spoll(&s);
        }
        CHECK(i > 6u && i < 100u);                              /* LBA 0 + five signature LBAs */
    }
    CHECK(ovlstore_sd_test_write_allowed(&s, 0u, 1u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 0u, 2u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 1u, 1u));          /* no wipe in plain format */
    CHECK(!ovlstore_sd_test_write_allowed(&s, 32u * MIB, 1u));  /* p4 not adopted yet */
    CHECK_RC(wait_job(&s), OVLSD_OK);
    card_free(&c);

    /* "erase-all": LBA 1..33 while wiping, nothing else; LBA 0 only after. */
    card_new(&c, 64u * MIB);
    {
        uint8_t mbr[512];
        pent_t e[4];
        memset(e, 0, sizeof e);
        e[0].type = 0x0Cu; e[0].start = 8192u; e[0].cnt = 100000u;
        mk_mbr(mbr, e);
        img_write(&c, 0, mbr, 512);
    }
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_OK);
    CHECK(ovlstore_sd_test_write_allowed(&s, 1u, 8u));
    CHECK(ovlstore_sd_test_write_allowed(&s, 33u, 1u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 33u, 2u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 34u, 1u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 0u, 1u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 0u, 2u));
    CHECK(!ovlstore_sd_test_write_allowed(&s, 8192u, 1u));      /* the old p1 */
    CHECK(!ovlstore_sd_test_write_allowed(&s, 32u * MIB, 1u));
    CHECK_RC(wait_job(&s), OVLSD_OK);
    card_free(&c);
    printf("    write guard: target slot only, header copy only when due, MBR only in rule (b),\n"
           "                 LBA 1..33 only while \"erase-all\" wipes\n");
}

/* "erase-all": the explicit wipe. On a card holding `nb` blocks (e.g. a FAT32
 * MBR, an ext4 superblock), plain "erase" must still refuse with `plain_rc`
 * and zero writes; "erase-all" then leaves a clean harness card: a fresh MBR
 * whose ONLY entry is 0xDA over the last 32 MiB, LBA 1..33 zeroed, nothing
 * written outside LBA 0..33 and the new partition, and a commit that works. */
static void wipe_case(const char *what, const blk_t *blks, unsigned nb, int plain_rc)
{
    enum { NB = 64u * MIB, P4 = 32u * MIB };
    card_t c;
    ovlstore_sd_t s;
    uint8_t mbr[512], z[512], marker[512];
    ovl_t a;

    card_new(&c, NB);
    for (unsigned i = 0; i < nb; i++) {
        img_write(&c, blks[i].lba, blks[i].data, 512);
    }
    memset(marker, 0x3Cu, sizeof marker);
    img_write(&c, 34u, marker, 512);                           /* just past the wiped range */
    img_write(&c, 8192u, marker, 512);                         /* old data, not in p4 */
    store_init(&s, &c.bd, LIVE);
    settle(&s);
    if (plain_rc != OVLSD_OK) {
        CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_FORMAT), plain_rc);   /* plain: refused */
        spoll_n(&s, 50u);
        CHECK(g_cw.writes == 0u);
    }
    g_cw.allow_head = OVLSD_WIPE_LAST_LBA + 1u;
    g_cw.allow_lo = P4;
    g_cw.allow_hi = NB;
    CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_OK);
    CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_EBUSY);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
    CHECK(g_cw.writes_outside == 0u);

    img_read(&c, 0, mbr, 512);
    for (unsigned i = 0; i < 446u; i++) {
        if (i < 440u || i >= 444u) CHECK(mbr[i] == 0u);         /* no boot code kept */
    }
    CHECK(get32le(mbr + 440u) == OVLSD_MBR_DISK_SIGNATURE);
    for (unsigned i = 0; i < 48u; i++) CHECK(mbr[446u + i] == 0u);   /* entries 1-3 empty */
    CHECK(mbr[446u + 48u + 4u] == 0xDAu);
    CHECK(get32le(mbr + 446u + 48u + 8u) == P4 && get32le(mbr + 446u + 48u + 12u) == P4);
    CHECK(mbr[510] == 0x55u && mbr[511] == 0xAAu);
    for (uint32_t lba = 1u; lba <= OVLSD_WIPE_LAST_LBA; lba++) {
        img_read(&c, lba, z, 512);
        CHECK(memcmp(z, k_zero, 512) == 0);                      /* GPT / ext / backup BS gone */
    }
    img_read(&c, 34u, z, 512);
    CHECK(memcmp(z, marker, 512) == 0);
    img_read(&c, 8192u, z, 512);
    CHECK(memcmp(z, marker, 512) == 0);

    /* A plain harness card now: rule (a) applies, and a commit works. */
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 120u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &a, 'A');
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    expect_default(&s, &a, 'A');
    CHECK(g_cw.writes_outside == 0u);
    ovl_free(&a);
    card_free(&c);
    printf(" 15 erase-all on %s: plain format -> \"%s\", wipe -> clean harness card\n",
           what, ovlstore_sd_rc_name(plain_rc));
}

static void test_erase_all(void)
{
    uint8_t mbr[512], sb[512], gh[512], bs[512];
    pent_t e[4];

    /* A factory card: one FAT32 partition. */
    memset(e, 0, sizeof e);
    e[0].type = 0x0Cu; e[0].start = 8192u; e[0].cnt = 64u * MIB - 8192u;
    mk_mbr(mbr, e);
    {
        blk_t b[] = { { 0u, mbr } };
        wipe_case("a FAT32 card", b, 1u, OVLSD_EEXIST);
    }
    /* Whole-disk ext4 (LBA 0 zero). */
    mk_ext4_sb(sb);
    {
        blk_t b[] = { { 2u, sb } };
        wipe_case("whole-disk ext4", b, 1u, OVLSD_EFSSIG);
    }
    /* A GPT card (protective MBR + header). */
    memset(e, 0, sizeof e);
    e[0].type = 0xEEu; e[0].start = 1u; e[0].cnt = 64u * MIB - 1u;
    mk_mbr(mbr, e);
    mk_gpt_hdr(gh);
    {
        blk_t b[] = { { 0u, mbr }, { 1u, gh } };
        wipe_case("a GPT card", b, 2u, OVLSD_EEXIST);
    }
    /* Whole-disk exFAT, with its backup boot sector at LBA 12. */
    mk_exfat_bs(bs);
    {
        blk_t b[] = { { 0u, bs }, { 12u, bs } };
        wipe_case("whole-disk exFAT", b, 2u, OVLSD_EFSSIG);
    }
    /* A harness card with a default: the wipe is allowed there too, and loses it. */
    {
        card_t c;
        ovlstore_sd_t s;
        ovl_t a;
        blank_and_format(&s, &c, 64u * MIB, LIVE);
        ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 121u);
        CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
        expect_default(&s, &a, 'A');
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_OK);
        CHECK_RC(wait_job(&s), OVLSD_OK);
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
        ovl_free(&a);
        card_free(&c);
    }
    /* Refusals, all with zero writes: too small, raw-partition mode, busy. */
    {
        card_t c;
        ovlstore_sd_t s;
        ovlstore_sd_cfg_t cfg;
        card_new(&c, 32u * MIB);
        store_init(&s, &c.bd, LIVE);
        settle(&s);
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_ESMALL);
        memset(&cfg, 0, sizeof cfg);
        cfg.live_static_id = LIVE;
        cfg.raw_partition = true;
        cfg.allow_wipe = true;
        ovlstore_sd_init(&s, &c.bd, &cfg);
        settle(&s);
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_EARG);
        spoll_n(&s, 50u);
        CHECK(g_cw.writes == 0u);
        card_free(&c);
    }
    /* allow_wipe false (the Linux daemon's setting): refused, zero writes,
     * while plain "erase" still works under the same config. */
    {
        card_t c;
        ovlstore_sd_t s;
        ovlstore_sd_cfg_t cfg;
        card_new(&c, 64u * MIB);
        memset(&cfg, 0, sizeof cfg);
        cfg.live_static_id = LIVE;
        ovlstore_sd_init(&s, &c.bd, &cfg);
        settle(&s);
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_ENOWIPE);
        spoll_n(&s, 50u);
        CHECK(g_cw.writes == 0u);
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_FORMAT), OVLSD_OK);    /* blank: rule (b) */
        CHECK_RC(wait_job(&s), OVLSD_OK);
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY && g_cw.writes == 3u);
        card_free(&c);
    }
    /* A wipe write that fails: the old MBR is still there (the MBR is written
     * only after LBA 1..33), the card stays FOREIGN, and it can be retried. */
    {
        card_t c;
        ovlstore_sd_t s;
        uint8_t now[512];
        memset(e, 0, sizeof e);
        e[0].type = 0x0Cu; e[0].start = 8192u; e[0].cnt = 64u * MIB - 8192u;
        mk_mbr(mbr, e);
        card_new(&c, 64u * MIB);
        img_write(&c, 0, mbr, 512);
        store_init(&s, &c.bd, LIVE);
        settle(&s);
        g_cw.fail_write_at = 2u;
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_OK);
        CHECK_RC(wait_job(&s), OVLSD_EIO);
        g_cw.fail_write_at = 0u;
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
        img_read(&c, 0, now, 512);
        CHECK(memcmp(now, mbr, 512) == 0);
        CHECK_RC(ovlstore_sd_format(&s, OVLSD_CONFIRM_WIPE), OVLSD_OK);
        CHECK_RC(wait_job(&s), OVLSD_OK);
        settle(&s);
        CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
        card_free(&c);
    }
    printf(" 15 erase-all refusals (too small, raw mode, allow_wipe off, busy) and a failed wipe:\n"
           "    zero MBR change\n");
}

/* Raw-partition mode: the bdev IS p4 (Linux /dev/mmcblk0p4). */
static void test_raw_partition(void)
{
    card_t c;
    ovlstore_sd_t s;
    ovlstore_sd_cfg_t cfg;
    ovl_t a;

    card_new(&c, 32u * MIB);
    memset(&cfg, 0, sizeof cfg);
    cfg.live_static_id = LIVE;
    cfg.raw_partition = true;
    ovlstore_sd_init(&s, &c.bd, &cfg);
    g_cw.allow_lo = 0u;
    g_cw.allow_hi = 32u * MIB;
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY && g_cw.writes == 2u);
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 90u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &a, 'A');
    CHECK(strcmp(ovlstore_sd_state_text(&s), "0x00000011 [A]") == 0);   /* no name callback */
    ovl_free(&a);
    card_free(&c);
    printf("    raw-partition mode (Linux /dev/mmcblk0p4): format (a), commit, stream\n");
}

/* The provider the Linux daemon opens: by path, O_DSYNC + page-cache drop. */
static void test_posix_linux_flags(void)
{
    char path[512];
    const char *dir = getenv("TMPDIR");
    ovl_bdev_posix_t px;
    ovl_bdev_t inner, bd;
    ovlstore_sd_t s;
    ovl_t a;
    int fd;

    snprintf(path, sizeof path, "%s/ovlsd_dev_XXXXXX", (dir && dir[0]) ? dir : "/tmp");
    fd = mkstemp(path);
    CHECK(fd >= 0);
    CHECK(ftruncate(fd, (off_t)64u * MIB * 512) == 0);
    close(fd);
    CHECK(ovl_bdev_posix_open(&px, &inner, path, 0u) == 0);             /* read-only */
    CHECK(px.nblocks == 64u * MIB && !px.writable);
    {
        uint8_t blk[512];
        memset(blk, 0, sizeof blk);
        CHECK(inner.ops->write_start(inner.ctx, 0u, 1u, blk) == -EROFS);
        CHECK(inner.ops->read_start(inner.ctx, 64u * MIB, 1u, blk) == -EINVAL);
        CHECK(inner.ops->read_start(inner.ctx, 64u * MIB - 1u, 2u, blk) == -EINVAL);
    }
    ovl_bdev_posix_close(&px);
    CHECK(ovl_bdev_posix_open(&px, &inner, path, OVL_BDEV_POSIX_LINUX_CARD) == 0);
    CHECK(unlink(path) == 0);
    CHECK(px.writable && px.nocache);
    bd = cw_wrap(&inner);
    store_init(&s, &bd, LIVE);
    settle(&s);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    ovl_make(&a, RM_LED, LIVE, 3000u, 30000u, 110u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &a, 'A');
    ovl_bdev_posix_close(&px);
    CHECK(ovl_bdev_posix_open(&px, &inner, "/nonexistent/ovlsd", 0u) == -ENOENT);
    ovl_free(&a);
    printf("    POSIX provider by path, O_DSYNC + page-cache drop (the Linux card flags)\n");
}

/* ======================= end to end over usd.c + fake_usd ==================== */

static void usd_setup(ovlstore_sd_t *s, ovl_bdev_t *inner, ovl_bdev_t *bd)
{
    mock_regs_reset();
    fake_usd_reset();
    usd_init();
    ovl_bdev_usd_bind(inner, true);           /* the store drives usd_poll() */
    *bd = cw_wrap(inner);
    store_init(s, bd, LIVE);
    g_usd_mode = 1;
}

static void test_usd_e2e(void)
{
    ovlstore_sd_t s;
    ovl_bdev_t inner, bd;
    ovl_t a, b, d;
    uint8_t blk[512];
    const uint32_t nb = (FAKE_USD_C_SIZE_DEFAULT + 1u) << 10;
    const uint32_t part = (nb - OVLSD_FORMAT_PART_BLOCKS) & ~(OVLSD_ALIGN_BLOCKS - 1u);

    /* No card: thousands of passes, zero DATA writes, zero store I/O. */
    usd_setup(&s, &inner, &bd);
    spoll_n(&s, 5000u);
    CHECK(ovlstore_sd_state(&s) == OVLSD_NO_CARD);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "none") == 0);
    CHECK(fake_usd_data_writes() == 0u && fake_usd_en_writes() == 0u && !fake_usd_pads_ever_driven());
    CHECK(g_cw.reads == 0u && g_cw.writes == 0u);

    /* No usd_spi in the fabric: "no hw", and the page is never written. */
    mock_regs_reset();
    fake_usd_reset();
    fake_usd_set_id(0u);
    usd_init();
    store_init(&s, &bd, LIVE);
    spoll_n(&s, 2000u);
    CHECK(ovlstore_sd_state(&s) == OVLSD_NO_HW);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "no hw") == 0);
    CHECK(fake_usd_page_writes() == 0u);

    /* A card whose LBA 0 is neither blank nor an MBR: foreign, format refused. */
    usd_setup(&s, &inner, &bd);
    fake_usd_insert();
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_EUNKNOWN);
    spoll_n(&s, 200u);
    CHECK(fake_usd_blocks_written() == 0u);

    /* Wipe LBA 0 behind the store's back (a PC zeroing it), then format. */
    memset(blk, 0, sizeof blk);
    CHECK(usd_write_start(0u, 1u, blk) == 0);
    for (int i = 0; i < 2000 && usd_io_status() == USD_IO_BUSY; i++) {
        mock_time_advance_ms(1u);
        usd_poll(mock_time_now_ms());
    }
    CHECK(usd_io_status() == USD_IO_DONE);
    CHECK_RC(ovlstore_sd_rescan(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_FOREIGN);
    g_cw.allow_lo = part;
    g_cw.allow_hi = nb;
    g_cw.allow_lba0 = 1;
    CHECK_RC(ovlstore_sd_format(&s, "erase"), OVLSD_OK);
    CHECK_RC(wait_job(&s), OVLSD_OK);
    settle(&s);
    CHECK(ovlstore_sd_state(&s) == OVLSD_EMPTY);
    fake_usd_peek_block(0u, blk);
    CHECK(blk[446u + 48u + 4u] == 0xDAu && get32le(blk + 446u + 48u + 8u) == part);
    {
        ovlstore_sd_info_t in;
        ovlstore_sd_info(&s, &in);
        CHECK(in.card_mb == (FAKE_USD_C_SIZE_DEFAULT + 1u) >> 1);
    }

    /* Two commits, streamed back through the real driver. Small, because the
     * fake card stores FAKE_USD_STORE_SLOTS written blocks. */
    ovl_make(&a, RM_LED, LIVE, 1000u, 6000u, 100u);
    ovl_make(&b, RM_MC, LIVE, 1200u, 5600u, 101u);
    ovl_make(&d, RM_ANON, LIVE, 1000u, 6000u, 102u);
    CHECK_RC(do_commit(&s, &a, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &a, 'A');
    CHECK_RC(do_commit(&s, &b, LIVE, 1460u), OVLSD_OK);
    expect_default(&s, &b, 'B');

    /* The card is pulled in the middle of the next commit's writes. */
    CHECK_RC(ovlstore_sd_commit_begin(&s, &d.d, LIVE, RM_ANON), OVLSD_OK);
    CHECK_RC(feed_region(&s, OVLSD_CLEARING, d.clr, d.d.clear_len, 1460u, 0xFFFFFFFFu), OVLSD_OK);
    fake_usd_remove_after_bytes(3000u);
    {
        int rc = feed_region(&s, OVLSD_PARTIAL, d.part, d.d.part_len, 1460u, 0xFFFFFFFFu);
        if (rc == OVLSD_OK) {
            rc = ovlstore_sd_commit_end(&s);
            if (rc == OVLSD_OK) {
                rc = wait_job(&s);
            }
        }
        CHECK_RC(rc, OVLSD_EGONE);
    }
    spoll_n(&s, 50u);
    CHECK(ovlstore_sd_state(&s) == OVLSD_NO_CARD);
    CHECK(strcmp(ovlstore_sd_state_text(&s), "none") == 0);
    fake_usd_insert();
    settle(&s);
    expect_default(&s, &b, 'B');                              /* the old default */
    CHECK(g_cw.writes_outside == 0u && fake_usd_store_overflows() == 0u);
    CHECK(fake_usd_busy_violations() == 0u && fake_usd_protocol_errors() == 0u);

    g_usd_mode = 0;
    ovl_free(&a);
    ovl_free(&b);
    ovl_free(&d);
    printf(" 14 end to end over ovl_bdev_usd + usd.c + fake_usd: no card = 0 DATA writes; "
           "format, 2 commits, pull mid-commit -> old default (max %u SPI B/poll)\n",
           (unsigned)g_max_usd_bytes);
}

int main(int argc, char **argv)
{
    printf("test_ovlstore_sd (OP_BLOCKS=%u, CRC_BYTES_PER_POLL=%u)\n",
           (unsigned)OVLSTORE_SD_OP_BLOCKS, (unsigned)OVLSTORE_SD_CRC_BYTES_PER_POLL);
    test_no_card();
    test_foreign();
    test_blank();
    test_linux_provisioned();
    test_commits();
    test_torn_commit();
    test_torn_header();
    test_stale();
    test_fallback();
    test_refusals();
    test_sizes();
    test_text_and_errors();
    test_raw_partition();
    test_format_recheck();
    test_write_guard();
    test_posix_linux_flags();
    test_erase_all();
    test_usd_e2e();
    printf(" 12 per-poll budget over %u polls: max ops started %u (cap 1), max CRC bytes %u "
           "(cap %u), max blocks/op %u (cap %u), 0 ops outside a poll\n",
           (unsigned)g_polls, (unsigned)g_max_ops, (unsigned)g_max_crc,
           (unsigned)OVLSTORE_SD_CRC_BYTES_PER_POLL, (unsigned)g_max_blk,
           (unsigned)OVLSTORE_SD_OP_BLOCKS);
    /* One source builds two binaries (default + tiny budget); the harness
     * (test_firmware_host_gcc_harness.py) wants "<binary>: N checks passed". */
    const char *self = (argc > 0 && argv[0]) ? strrchr(argv[0], '/') : NULL;
    self = self ? self + 1 : ((argc > 0 && argv[0]) ? argv[0] : "test");
    printf("%s: %u checks passed (ALL PASS)\n", self, s_checks);
    return 0;
}
