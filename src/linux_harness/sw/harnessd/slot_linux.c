/*
 * slot_linux.c — the user-microSD boot slots, for TOOLS (plan §10a S10;
 * net-protocol.md "Slot images"; docs/planning/linux_lanes/SLOT_VERB_DRAFT.md).
 *
 * A tool (socharness) stages a new Linux image into the INACTIVE stage0 slot
 * over Ethernet (a 6910/TFTP push of kind MPS3_BIN_KIND_SLOT_IMAGE), waits for
 * the card read-back to pass, then flips the boot default (`slot` act commit)
 * and reboots. This file is the ENGINE half of both: the strong
 * mps3_cfg_slot_sink() (config_agent.h) and the strong mps3_slot_op()
 * (coordinator.h). Bare metal keeps the weak defaults, which decline.
 *
 * THE RULES, and where each one is enforced:
 *   1. A push never writes the RUNNING slot, nor the DEFAULT slot. The target is
 *      "the slot that is neither" (slot_target()); when the two differ (after a
 *      commit, before the reboot; or after stage0 fell back) there is no target
 *      and the push is refused: roll back first. So the image stage0 tries first
 *      is always one that was verified before it was made the default.
 *   2. Verify before flip. A flip (commit / rollback) needs the destination's
 *      region CRCs proven THIS BOOT and the image bound to THIS FABRIC:
 *        - a push or a `verify` read it back off the card through stage0's own
 *          loader (s0_load() from stage0_core.c, compiled in unmodified), with
 *          the page cache dropped first so the card -- not RAM -- is checked;
 *          the static_id is the push header's (== the fabric, checked twice) or
 *          the slot record a push left behind;
 *        - or it is the slot stage0 booted this OS from (stage0 CRC-checked
 *          every region at hand-off; image_hdr_crc names it) and the running
 *          image's own claim agrees with the fabric (no identity lock).
 *      The fabric static_id is stage0's baked value from the status block,
 *      never the card's (identity.c).
 *   3. A card fault can never break the running system. Linux runs from RAM;
 *      every card write is bounded to the target slot's LBA range (slot_card's
 *      write guard); every long card operation (flush, read-back, CRC of up to 64 MiB)
 *      runs in a forked child at nice 10 that holds nothing but the card fd and
 *      a pipe, so the service loop -- and the watchdog kick -- never waits on the
 *      card. Only small synchronous reads (the MBR, the two boot-select copies,
 *      one header + one record sector per slot) and ONE-SECTOR writes happen on
 *      the loop: the flip (a boot-select copy) and, once per confirmed boot, the
 *      booted slot's record (harnessd_slot_stamp_booted(), bounded to that one
 *      sector), each flushed and read back.
 *   4. No card => a clean decline (`no card`), and `status` says card:false.
 *   5. stage0 is read-only on the card; the boot-select writer rule is
 *      STAGE0_CONTRACT §6's: rewrite the copy that is NOT stage0's current pick
 *      with seq+1, flush, drop the cache, then read both back off the card. A
 *      torn write leaves the old pick valid.
 *
 * THE CARD LAYER IS SHARED: slot_card.[ch] holds every card rule (the read as
 * stage0 sees it, the boot-select writer, the write guard, flush + uncache,
 * stage0's loader as the checker) and IMAGE's `mps3-slot` links the same file,
 * so the verb and the admin tool cannot drift. tests/test_slot_e2e.py proves
 * both binaries carry it and write the same bytes on the same card.
 *
 * THE SLOT RECORD (new, this file's): the LAST sector of the slot partition,
 * written by a push after its read-back passed, and -- for a slot written by any
 * other tool -- stamped for the slot stage0 booted once that boot is confirmed
 * with a consistent identity (harnessd_slot_stamp_booted(), HM_ANSWERS S1).
 * Stage0 never reads past the image, so it is invisible to boot; it is what lets
 * a slot pushed or booted in an EARLIER boot be bound to a static_id again
 * (`verify`). It is keyed by the image's table CRC, so a record left behind by an
 * image that `mps3-slot write` later replaced never binds the new one.
 *     0x000 magic "S0SR"   0x004 version 1   0x008 hdr_crc   0x00C static_id
 *     0x010 len (image extent)   0x014 source (1 = a 6910/TFTP push, 2 = boot)
 *     0x018..0x1FB zero    0x1FC CRC-32 of 0x000..0x1FB
 * So a pushed image may be at most (slot size - 512) bytes.
 *
 * THE LOCK (David, 2026-09-24; net-protocol.md "Slot images" "The lock"): once
 * the board's SSH is CLAIMED, the MUTATIONS -- a slot-image push (6910 / TFTP),
 * `commit`, `rollback` -- are refused from any peer that is not this host.
 * `status` and `verify` only read and stay open. The owner keeps full access over
 * SSH: `mps3-slot` on the board, or a tunnel to 127.0.0.1:6900/6910 (a local
 * peer). SSH is the authentication. An unclaimed board is open, exactly like the
 * claim's own trust-on-first-use window. "Claimed" is harnessd_ssh_claimed() --
 * the SAME call identify's ssh.claimed and the CLCD row read -- evaluated on every
 * request, so a claim locks and an unclaim unlocks at once, with no restart. A
 * refused push is refused before the provider runs: it touches no state.
 * 2026-09-26 (David: YES to HM_ANSWERS S6 / C3): the SAME lock also refuses the D13
 * store's mutations (`usd` actions, the re-push `commit`: coordinator.h's
 * mps3_claim_refuses_active_peer()) and new connections to the DUT debug ports
 * (XVC 2542, jtag_server 6921: one "locked" line, then the close).
 *
 * STATE that must survive a harnessd respawn (not an OS reboot) lives in
 * --slot-state (default /run/mps3/slot.state, tmpfs): which slots this boot
 * verified, the staged slot, the last job. It carries the kernel boot_id and is
 * ignored under any other.
 */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../../../firmware/common/net_if.h"
#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/config_agent/config_agent.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "../../../../firmware/jtag_server/jtag_server.h"
#include "../../../../firmware/xvc_server/xvc_server.h"
#include "../../../linux_soc/hw/fw_stage0/stage0_boot.h"
#include "../../../linux_soc/hw/fw_stage0/stage0_status.h"
#include "harnessd.h"
#include "slot_card.h"

#define S0_OFF(field) ((unsigned)__builtin_offsetof(struct s0_status, field))

#define BLK            512u
#define DDR_BASE       0x80000000u   /* stage0's destination window (STAGE0 §5) */
#define DDR_SIZE       0x30000000u   /* [0x8000_0000, 0xB000_0000)                */
#define REC_MAGIC      0x52533053u   /* "S0SR" */
#define REC_VERSION    1u
#define REC_SRC_PUSH   1u
#define REC_SRC_BOOT   2u            /* harnessd_slot_stamp_booted(): the confirmed boot */
#define HOLD_BYTES     BLK           /* the image's first sector: written LAST    */

/* ==========================================================================
 * Persistent (per OS boot) state
 * ========================================================================== */
static struct {
    uint32_t vcrc[2];      /* table CRC a card read-back proved this boot (0 = none) */
    uint32_t vsid[2];      /* the static_id bound to it                              */
    uint8_t  staged;       /* MPS3_SLOT_A/B: the slot the last good push wrote        */
    uint8_t  job_act, job_state, job_slot;
    uint32_t job_got, job_len;
    char     job_err[64];
    long     job_pid;      /* the verifier child (0 = none)                          */
} s_st;

static int   s_pipe = -1;  /* read end of the running child's result pipe */
static pid_t s_child;      /* OUR child; 0 when the running job is inherited/none */
static char  s_boot_id[64];

static int busy(void)
{
    return s_st.job_state == MPS3_SLOT_JS_WRITING || s_st.job_state == MPS3_SLOT_JS_VERIFYING;
}

static void job_fail(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void state_save(void);

static void job_fail(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_st.job_err, sizeof(s_st.job_err), fmt, ap);
    va_end(ap);
    s_st.job_state = MPS3_SLOT_JS_FAILED;
    s_st.job_pid = 0;
    harnessd_log("slot: %s %c FAILED: %s\n",
                 s_st.job_act == MPS3_SLOT_JOB_PUSH ? "push" : "verify",
                 s_st.job_slot == MPS3_SLOT_B ? 'B' : 'A', s_st.job_err);
    state_save();
}

static void read_boot_id(void)
{
    FILE *f = fopen("/proc/sys/kernel/random/boot_id", "r");
    s_boot_id[0] = '\0';
    if (f) {
        if (fgets(s_boot_id, sizeof(s_boot_id), f)) {
            s_boot_id[strcspn(s_boot_id, "\n")] = '\0';
        }
        fclose(f);
    }
}

static void state_save(void)
{
    if (!g_hd.slot_state) {
        return;
    }
    char tmp[600];
    snprintf(tmp, sizeof(tmp), "%s.tmp", g_hd.slot_state);
    FILE *f = fopen(tmp, "w");
    if (!f) {
        return;   /* /run missing: the state still lives in memory for this process */
    }
    fprintf(f, "format=1\nboot_id=%s\n", s_boot_id);
    fprintf(f, "vcrc_a=0x%08" PRIx32 "\nvsid_a=0x%08" PRIx32 "\n", s_st.vcrc[0], s_st.vsid[0]);
    fprintf(f, "vcrc_b=0x%08" PRIx32 "\nvsid_b=0x%08" PRIx32 "\n", s_st.vcrc[1], s_st.vsid[1]);
    fprintf(f, "staged=%u\njob_act=%u\njob_state=%u\njob_slot=%u\n", s_st.staged,
            s_st.job_act, s_st.job_state, s_st.job_slot);
    fprintf(f, "job_got=%" PRIu32 "\njob_len=%" PRIu32 "\njob_pid=%ld\njob_err=%s\n",
            s_st.job_got, s_st.job_len, s_st.job_pid, s_st.job_err);
    if (fclose(f) == 0) {
        (void)rename(tmp, g_hd.slot_state);
    }
}

/* One `key=value` LINE of the state file (values may hold spaces: job_err). */
static int state_kv(const char *buf, const char *key, char *out, size_t cap)
{
    size_t kl = strlen(key);
    for (const char *l = buf; l && *l; l = strchr(l, '\n') ? strchr(l, '\n') + 1 : 0) {
        if (strncmp(l, key, kl) == 0 && l[kl] == '=') {
            const char *v = l + kl + 1;
            size_t n = strcspn(v, "\n");
            if (n >= cap) {
                n = cap - 1u;
            }
            memcpy(out, v, n);
            out[n] = '\0';
            return 0;
        }
    }
    return -1;
}

static void state_load(void)
{
    char buf[2048], v[64];
    FILE *f = g_hd.slot_state ? fopen(g_hd.slot_state, "r") : 0;
    if (!f) {
        return;
    }
    size_t got = fread(buf, 1, sizeof(buf) - 1u, f);
    fclose(f);
    buf[got] = '\0';
    if (state_kv(buf, "boot_id", v, sizeof(v)) != 0 || strcmp(v, s_boot_id) != 0) {
        return;   /* another OS boot's: nothing THIS boot proved */
    }
#define LOAD_U(key, dst) \
    do { if (state_kv(buf, key, v, sizeof(v)) == 0) (dst) = strtoul(v, 0, 0); } while (0)
    LOAD_U("vcrc_a", s_st.vcrc[0]); LOAD_U("vsid_a", s_st.vsid[0]);
    LOAD_U("vcrc_b", s_st.vcrc[1]); LOAD_U("vsid_b", s_st.vsid[1]);
    LOAD_U("staged", s_st.staged);  LOAD_U("job_act", s_st.job_act);
    LOAD_U("job_state", s_st.job_state); LOAD_U("job_slot", s_st.job_slot);
    LOAD_U("job_got", s_st.job_got); LOAD_U("job_len", s_st.job_len);
    LOAD_U("job_pid", s_st.job_pid);
#undef LOAD_U
    if (state_kv(buf, "job_err", s_st.job_err, sizeof(s_st.job_err)) != 0) {
        s_st.job_err[0] = '\0';
    }
    if (s_st.staged > MPS3_SLOT_B || s_st.job_slot > MPS3_SLOT_B ||
        s_st.job_act > MPS3_SLOT_JOB_VERIFY || s_st.job_state > MPS3_SLOT_JS_FAILED) {
        memset(&s_st, 0, sizeof(s_st));   /* garbage: prove nothing */
    }
}

/* ==========================================================================
 * The card -- ONE layer, shared with IMAGE's mps3-slot (slot_card.[ch]): the
 * MBR/boot-select read, which copy stage0 uses, the boot-select writer, the
 * write guard, flush + uncache, stage0's loader as the checker. Only what is
 * harnessd's own stays here: which card, the log line for a guard refusal, the
 * slot record, the streamed table check of a push.
 * ========================================================================== */
typedef slot_card_t card_t;

/* <0: -2 no card, -1 an I/O fault. */
static int card_open(int flags)
{
    if (!g_hd.card) {
        return -2;
    }
    int fd = open(g_hd.card, flags | O_CLOEXEC);
    if (fd >= 0) {
        return fd;
    }
    return (errno == ENOENT || errno == ENXIO || errno == ENOMEDIUM || errno == ENODEV) ? -2 : -1;
}

#define card_rd slot_card_rd

/* Every card write in this file: slot_card's guard, plus harnessd's log line. */
static int card_wr(int fd, uint64_t off, const void *buf, size_t n, uint64_t lo, uint64_t hi)
{
    int rc = slot_card_wr(fd, off, buf, n, lo, hi);
    if (rc == -2) {
        harnessd_log("slot: WRITE GUARD refused %zu B at 0x%" PRIx64 " (allowed 0x%" PRIx64
                     "..0x%" PRIx64 ")\n", n, off, lo, hi);
    }
    return rc == 0 ? 0 : -1;
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

/* MBR + boot-select, the way stage0 reads them. 0 ok; -1 io. A card without the
 * stage0 layout (blank, foreign) is NOT an error here: both slots read "absent",
 * which tells a tool exactly that, and nothing here ever writes such a card. */
static int card_read(int fd, card_t *c)
{
    int rc = slot_card_read(fd, c);
    return (rc == 0 || rc == -2) ? 0 : -1;
}

#define slot_off   slot_card_off
#define slot_bytes slot_card_bytes

/* ---- an image's table, from its first bytes ------------------------------ */
typedef struct {
    struct s0_header h;
    struct s0_entry  e[S0_MAX_ENTRIES];
    uint32_t extent;          /* max(table end, every region's end) */
} table_t;

/* check_image's table half (stage0_pack.py / s0_load): magic, version, the
 * entry count, the table CRC, every region inside the DDR window and inside
 * `limit` bytes. NULL = valid, else the reason. `have` bytes of `img` exist. */
static const char *table_check(const uint8_t *img, uint32_t have, uint32_t limit, table_t *t)
{
    if (have < sizeof(t->h)) {
        return "shorter than a header";
    }
    memcpy(&t->h, img, sizeof(t->h));
    if (t->h.magic != S0_MAGIC) {
        return "no S0LB magic";
    }
    if (t->h.version != S0_VERSION) {
        return "bad version";
    }
    if (t->h.num_entries == 0u || t->h.num_entries > S0_MAX_ENTRIES) {
        return "bad num_entries";
    }
    uint32_t tbl = t->h.num_entries * (uint32_t)sizeof(t->e[0]);
    if (have < S0_ENTRIES_OFFSET + tbl) {
        return "table truncated";
    }
    memcpy(t->e, img + S0_ENTRIES_OFFSET, tbl);
    struct s0_header h0 = t->h;
    h0.header_crc32 = 0u;
    if (s0_crc32_update(s0_crc32(&h0, (uint32_t)sizeof(h0)), t->e, tbl) != t->h.header_crc32) {
        return "table CRC";
    }
    t->extent = S0_ENTRIES_OFFSET + tbl;
    for (uint32_t i = 0; i < t->h.num_entries; i++) {
        const struct s0_entry *e = &t->e[i];
        if (!s0_region_in_bounds(DDR_BASE, DDR_SIZE, e->dst_addr, e->len)) {
            return "region outside the DDR window";
        }
        if (e->src_offset > limit || e->len > limit - e->src_offset) {
            return "region past the end";
        }
        if (e->src_offset + e->len > t->extent) {
            t->extent = e->src_offset + e->len;
        }
    }
    return 0;
}

/* The slot record (file header). 1 = valid and for this table CRC. */
static int record_read(int fd, const card_t *c, int i, uint32_t hdr_crc, uint32_t *sid)
{
    uint8_t r[BLK];
    if (card_rd(fd, slot_off(c, i) + slot_bytes(c, i) - BLK, r, BLK) != 0) {
        return 0;
    }
    if (le32(r) != REC_MAGIC || le32(r + 4) != REC_VERSION ||
        s0_crc32(r, BLK - 4u) != le32(r + BLK - 4u) || le32(r + 8) != hdr_crc) {
        return 0;
    }
    *sid = le32(r + 12);
    return 1;
}

static void record_build(uint8_t r[BLK], uint32_t hdr_crc, uint32_t sid, uint32_t len,
                         uint32_t src)
{
    memset(r, 0, BLK);
    put32(r, REC_MAGIC);
    put32(r + 4, REC_VERSION);
    put32(r + 8, hdr_crc);
    put32(r + 12, sid);
    put32(r + 16, len);
    put32(r + 20, src);
    put32(r + BLK - 4u, s0_crc32(r, BLK - 4u));
}

/* A slot as `status` shows it: the MBR entry, the header + table, the record. */
static void slot_info(int fd, const card_t *c, int i, mps3_slot_info_t *si, table_t *t)
{
    memset(si, 0, sizeof(*si));
    if (c->bad[i][0]) {
        /* slot_card's OVERLAP GUARD: stage0 sees this 0x7F entry, but it shares
         * blocks with the 0xDA store / the other slot / another entry / LBA 0-2,
         * so no write range is handed out for it (c->slot[i] is zero). */
        si->state = MPS3_SLOT_ST_BAD;
        snprintf(si->err, sizeof(si->err), "%s", c->bad[i]);
        return;
    }
    if (c->slot[i].first_lba == 0u) {
        si->state = MPS3_SLOT_ST_ABSENT;
        return;
    }
    uint8_t first[BLK];
    if (card_rd(fd, slot_off(c, i), first, BLK) != 0) {
        si->state = MPS3_SLOT_ST_IO;
        snprintf(si->err, sizeof(si->err), "read: %s", strerror(errno));
        return;
    }
    const char *why = table_check(first, BLK, (uint32_t)slot_bytes(c, i), t);
    if (why && strcmp(why, "no S0LB magic") == 0) {
        si->state = MPS3_SLOT_ST_EMPTY;
        return;
    }
    if (why) {
        si->state = MPS3_SLOT_ST_BAD;
        snprintf(si->err, sizeof(si->err), "%s", why);
        return;
    }
    si->state = MPS3_SLOT_ST_VALID;
    si->hdr_crc = t->h.header_crc32;
    si->len = t->extent;
    uint32_t sid = 0;
    if (record_read(fd, c, i, si->hdr_crc, &sid)) {
        si->has_sid = 1;
        si->sid = sid;
    }
}

/* ==========================================================================
 * Which slot is running, which one a push may write
 * ========================================================================== */
static uint8_t running_slot(void)
{
    if (!harnessd_s0_valid()) {
        return MPS3_SLOT_RUN_UNKNOWN;
    }
    switch (harnessd_s0_read(S0_OFF(booted_from))) {
    case S0_FROM_A:      return MPS3_SLOT_RUN_A;
    case S0_FROM_B:      return MPS3_SLOT_RUN_B;
    case S0_FROM_RESCUE: return MPS3_SLOT_RUN_RESCUE;
    default:             return MPS3_SLOT_RUN_NONE;
    }
}

/* Rule 1 (file header): neither the running slot nor the default. */
static uint8_t slot_target(uint8_t running, uint32_t deflt)
{
    if (running == MPS3_SLOT_RUN_UNKNOWN) {
        return MPS3_SLOT_NONE;
    }
    if (running == MPS3_SLOT_RUN_A || running == MPS3_SLOT_RUN_B) {
        return (deflt == running) ? (uint8_t)(3u - running) : MPS3_SLOT_NONE;
    }
    return (uint8_t)(3u - deflt);   /* rescue / a JTAG boot: neither slot runs */
}

static uint8_t verified_how(uint8_t running, int i, const mps3_slot_info_t *si)
{
    if (si->state != MPS3_SLOT_ST_VALID) {
        return MPS3_SLOT_VER_NO;
    }
    if (s_st.vcrc[i] != 0u && s_st.vcrc[i] == si->hdr_crc) {
        return MPS3_SLOT_VER_READBACK;
    }
    if (running == (uint8_t)(i + 1) && harnessd_s0_read(S0_OFF(image_hdr_crc)) == si->hdr_crc) {
        return MPS3_SLOT_VER_BOOT;
    }
    return MPS3_SLOT_VER_NO;
}

/* A failed job's STABLE CODE (2026-09-26, HM_ANSWERS S3): one table from the
 * job.err texts this file (and its verifier child) writes -- net-protocol.md
 * "job.err" -- to `job.code`. `?` matches the one slot letter; a pattern
 * matches as a PREFIX. NULL = no code (the key is then absent). The
 * card/identity refusals reuse the verb's names for the same conditions. */
static const char *job_code_of(const char *err)
{
    static const struct { const char *pat; const char *code; } k[] = {
        { "image for 0x",               "wrong_static"   },
        { "no stage0 block",            "no_stage0"      },
        { "fabric static_id unknown",   "fabric_unknown" },
        { "no card",                    "no_card"        },
        { "card io",                    "card_io"        },
        { "no free slot",               "no_free_slot"   },
        { "slot mismatch:",             "slot_mismatch"  },
        { "image too large",            "too_large"      },
        { "slot ? absent",              "slot_absent"    },
        { "slot ? bad:",                "slot_bad"       },
        { "shorter than a header",      "bad_image"      },
        { "no S0LB magic",              "bad_image"      },
        { "bad version",                "bad_image"      },
        { "bad num_entries",            "bad_image"      },
        { "table truncated",            "bad_image"      },
        { "table CRC",                  "bad_image"      },
        { "region ",                    "bad_image"      },   /* outside / past / N CRC */
        { "torn",                       "torn"           },
        { "aborted",                    "aborted"        },
        { "card write:",                "card_write"     },
        { "flush:",                     "readback"       },
        { "header write:",              "readback"       },
        { "read-back:",                 "readback"       },
        { "verifier died",              "readback"       },
        { "record ",                    "record"         },   /* write: / read-back */
        { "no slot record",             "no_record"      },
        { "harnessd restarted",         "restarted"      },
        { "result lost",                "restarted"      },
    };
    for (unsigned i = 0; i < sizeof(k) / sizeof(k[0]); i++) {
        const char *p = k[i].pat, *t = err;
        for (; *p && *t && (*p == '?' || *p == *t); p++, t++) {
        }
        if (*p == '\0') {
            return k[i].code;
        }
    }
    return 0;
}

/* The whole status, from the card as it is now. 0 ok, -2 no card, -1 io. */
static int status_fill(mps3_slot_status_t *st, card_t *c, table_t t[2], int *fd_out)
{
    memset(st, 0, sizeof(*st));
    st->fabric_sid = harnessd_fabric_static_id();
    st->running = running_slot();
    st->staged = s_st.staged;
    st->job_act = s_st.job_act;
    st->job_state = s_st.job_state;
    st->job_slot = s_st.job_slot;
    st->job_got = s_st.job_got;
    st->job_len = s_st.job_len;
    snprintf(st->job_err, sizeof(st->job_err), "%s", s_st.job_err);
    if (s_st.job_state == MPS3_SLOT_JS_FAILED) {
        const char *code = job_code_of(s_st.job_err);
        snprintf(st->job_code, sizeof(st->job_code), "%s", code ? code : "");
    }
    /* HM_ANSWERS S2/S5: the SAME claim call the lock and identify read, and the
     * stage0 confirm word (0 when there is no valid block). */
    st->claimed = harnessd_ssh_claimed() ? 1 : 0;
    st->confirmed = (harnessd_s0_valid() &&
                     harnessd_s0_read(S0_OFF(att_confirm)) == S0_CONFIRM_MAGIC) ? 1 : 0;

    int fd = card_open(O_RDONLY);
    if (fd < 0) {
        return fd;
    }
    int rc = card_read(fd, c);
    if (rc != 0) {
        close(fd);
        return rc;
    }
    st->card = 1;
    st->deflt = (uint8_t)c->deflt;
    st->seq = c->seq;
    st->target = slot_target(st->running, c->deflt);
    for (int i = 0; i < 2; i++) {
        slot_info(fd, c, i, &st->s[i], &t[i]);
        st->s[i].verified = verified_how(st->running, i, &st->s[i]);
    }
    if (fd_out) {
        *fd_out = fd;
    } else {
        close(fd);
    }
    return 0;
}

static const char *card_err(int rc)
{
    return rc == -2 ? "no card" : "card io";
}

/* stage0 has a status block and a baked static_id: every write needs both. */
static const char *fabric_err(void)
{
    if (!harnessd_s0_valid()) {
        return "no stage0 block";
    }
    if (harnessd_fabric_static_id() == 0u) {
        return "fabric static_id unknown";
    }
    return 0;
}

/* ==========================================================================
 * The lock (file header "THE LOCK")
 * ========================================================================== */
#define SLOT_LOCKED_ERR "slot locked: board claimed (use ssh)"

/* This host? 127.0.0.0/8 through the seam; a MOCK test may name the ONE trusted
 * address instead (--mock-trusted-peer), so a host test can have a "remote" peer
 * without a second machine. An unknown peer is never local. */
static int peer_local(const mps3_net_addr_t *a, int known)
{
    if (!known) {
        return 0;
    }
#ifdef MPS3_HAL_MOCK
    if (g_hd.mock_trusted_peer) {
        char t[48];
        mps3_net_addr_text(a, t, sizeof(t));
        size_t n = strcspn(t, ":");
        return strlen(g_hd.mock_trusted_peer) == n && strncmp(t, g_hd.mock_trusted_peer, n) == 0;
    }
#endif
    return mps3_net_addr_is_local(a);
}

/* 1 = refuse `what` from this peer (and log it, with the peer). */
static int slot_locked(const mps3_net_addr_t *a, int known, const char *what)
{
    if (!harnessd_ssh_claimed() || peer_local(a, known)) {
        return 0;
    }
    char t[48] = "an unknown peer";
    if (known) {
        mps3_net_addr_text(a, t, sizeof(t));
    }
    harnessd_log("slot: LOCKED -- %s from %s refused: the board is claimed; use ssh "
                 "(mps3-slot on the board, or a tunnel to 127.0.0.1)\n", what, t);
    return 1;
}

/* config_agent.h "THE LOCK": the push path asks before the provider. */
int mps3_cfg_slot_refuse_peer(const mps3_net_addr_t *peer, int known)
{
    return slot_locked(peer, known, "a slot-image push");
}

/* The 6900 client being served right now, through the lock. */
static int conn_locked(struct mps3_net_conn *conn, const char *what);
static int active_peer_locked(const char *what)
{
    return conn_locked(coordinator_net_active_conn(), what);
}

/* THE CLAIM LOCK on the DUT debug ports (HM_ANSWERS C3 / change 10; David
 * 2026-09-26: YES): on a claimed board, XVC 2542 and jtag_server 6921 serve the
 * board itself only (an ssh -L tunnel's far end); anyone else gets one line with
 * code "locked", then the close (xvc_server.h / jtag_server.h). Checked at accept:
 * a session that was open before the claim keeps running. */
static int conn_locked(struct mps3_net_conn *conn, const char *what)
{
    mps3_net_addr_t peer;
    memset(&peer, 0, sizeof(peer));
    int known = conn && mps3_net_conn_peer(conn, &peer) == 0;
    return slot_locked(&peer, known, what);
}

int mps3_xvc_refuse_peer(struct mps3_net_conn *conn)
{
    return conn_locked(conn, "an xvc connection (2542)");
}

int mps3_jtag_refuse_peer(struct mps3_net_conn *conn)
{
    return conn_locked(conn, "a jtag_server connection (6921)");
}

/* coordinator.h: this engine has the debug-port lock -> version.features "xvc_lock". */
int mps3_debug_lock_supported(void)
{
    return 1;
}

/* coordinator.h's claim-lock seam for the D13 store's mutations (HM_ANSWERS S6;
 * David 2026-09-26: YES): `usd` actions and the re-push `commit` take the SAME
 * lock as the slot mutations -- a wiped or rewritten overlay store on a claimed
 * board is the owner's call, made over SSH. */
int mps3_claim_refuses_active_peer(const char *what)
{
    /* v0.16 (lane IDENT): identity_set takes the same lock (coordinator.c). */
    return active_peer_locked(strcmp(what, "usd") == 0 ? "a usd action" :
                              strcmp(what, "identity") == 0 ? "an identity_set" :
                              "a store commit");
}

/* ==========================================================================
 * The verifier child
 * ========================================================================== */
/* stage0's loader over the card slot, after a flush and a cache drop so the
 * answer comes off the card (slot_card). 0 = S0_OK and the table CRC is `want`
 * (0 = any); else a reason in `why`. */
static int verify_slot(int fd, uint64_t base, uint64_t size, uint32_t want, uint32_t *got,
                       char *why, size_t cap)
{
    slot_card_src_t src = { fd, base, size, 0 };
    uint32_t hcrc = 0;
    if (slot_card_flush_uncache(fd, base, size) != 0) {
        snprintf(why, cap, "flush: %s", strerror(errno));
        return -1;
    }
    const char *e = slot_card_s0_check(&src, &hcrc);
    if (e) {
        snprintf(why, cap, "read-back: %s", e);
        return -1;
    }
    if (want != 0u && hcrc != want) {
        snprintf(why, cap, "read-back: table CRC 0x%08" PRIx32 " != pushed 0x%08" PRIx32,
                 hcrc, want);
        return -1;
    }
    *got = hcrc;
    return 0;
}

typedef struct {
    int      mode;            /* MPS3_SLOT_JOB_PUSH / _VERIFY                    */
    int      fd;              /* the card, O_RDWR (push) / O_RDONLY (verify)     */
    uint64_t base, size;      /* the slot's byte range                          */
    uint32_t want;            /* push: the pushed table CRC                      */
    uint32_t sid;             /* push: the static_id the push was checked against */
    uint32_t extent;          /* push: the image extent (for the record)         */
    uint8_t  hold[HOLD_BYTES];/* push: the image's first sector, written LAST    */
    uint32_t hold_len;
    uint32_t fabric;          /* verify: what the record must say                */
} job_t;

/* The child's whole life. Writes one result line to `out` and exits:
 *   "ok <hdr_crc> <sid>"  |  "fail <reason>" */
static void child_run(const job_t *j, int out) __attribute__((noreturn));
static void child_run(const job_t *j, int out)
{
    char why[64] = "";
    char line[96];
    uint32_t got = 0, sid = 0;
    int ok = 0;
    uint64_t lo = j->base, hi = j->base + j->size;

    if (j->mode == MPS3_SLOT_JOB_PUSH) {
        uint8_t zero[BLK];
        memset(zero, 0, sizeof(zero));
        /* 1. the body (already in the page cache) onto the card, 2. the first
         * sector -- the one that makes the slot look valid -- LAST, 3. read the
         * whole image back through stage0's loader off the card. */
        if (slot_card_flush_uncache(j->fd, j->base, j->size) != 0) {
            snprintf(why, sizeof(why), "flush: %s", strerror(errno));
        } else if (card_wr(j->fd, j->base, j->hold, j->hold_len, lo, hi) != 0) {
            snprintf(why, sizeof(why), "header write: %s", strerror(errno));
        } else if (verify_slot(j->fd, j->base, j->size - BLK, j->want, &got, why, sizeof(why)) == 0) {
            uint8_t rec[BLK], back[BLK];
            record_build(rec, got, j->sid, j->extent, REC_SRC_PUSH);
            uint64_t roff = j->base + j->size - BLK;
            if (card_wr(j->fd, roff, rec, BLK, lo, hi) != 0 ||
                slot_card_flush_uncache(j->fd, roff, BLK) != 0) {
                snprintf(why, sizeof(why), "record write: %s", strerror(errno));
            } else {
                if (card_rd(j->fd, roff, back, BLK) != 0 || memcmp(rec, back, BLK) != 0) {
                    snprintf(why, sizeof(why), "record read-back");
                } else {
                    ok = 1;
                    sid = j->sid;
                }
            }
        }
        if (!ok) {
            /* Never leave a valid table over a body that did not read back. */
            (void)card_wr(j->fd, j->base, zero, j->hold_len < BLK ? j->hold_len : BLK, lo, hi);
            (void)slot_card_flush_uncache(j->fd, j->base, BLK);
        }
    } else {
        if (verify_slot(j->fd, j->base, j->size, 0u, &got, why, sizeof(why)) == 0) {
            uint8_t r[BLK];
            if (card_rd(j->fd, j->base + j->size - BLK, r, BLK) != 0 ||
                le32(r) != REC_MAGIC || le32(r + 4) != REC_VERSION ||
                s0_crc32(r, BLK - 4u) != le32(r + BLK - 4u) || le32(r + 8) != got) {
                snprintf(why, sizeof(why), "no slot record: static_id unknown");
            } else if (le32(r + 12) != j->fabric) {
                snprintf(why, sizeof(why), "image for 0x%08" PRIx32 " != fabric 0x%08" PRIx32,
                         le32(r + 12), j->fabric);
            } else {
                ok = 1;
                sid = le32(r + 12);
            }
        }
    }
    if (ok) {
        snprintf(line, sizeof(line), "ok 0x%08" PRIx32 " 0x%08" PRIx32 "\n", got, sid);
    } else {
        snprintf(line, sizeof(line), "fail %s\n", why);
    }
    (void)!write(out, line, strlen(line));
    _exit(ok ? 0 : 1);
}

/* Fork the verifier. The child keeps ONLY the card fd and the pipe: a respawned
 * harnessd must be able to bind its ports while an orphaned verifier finishes. */
static int job_spawn(const job_t *j)
{
    int p[2];
    if (pipe2(p, O_CLOEXEC) != 0) {
        return -1;
    }
    pid_t pid = fork();
    if (pid < 0) {
        close(p[0]);
        close(p[1]);
        return -1;
    }
    if (pid == 0) {
        long maxfd = sysconf(_SC_OPEN_MAX);
        if (maxfd < 0 || maxfd > 4096) maxfd = 4096;
        for (int fd = 3; fd < maxfd; fd++) {
            if (fd != j->fd && fd != p[1]) {
                close(fd);
            }
        }
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        (void)!nice(10);   /* the card is the bottleneck; never the service loop's CPU */
        child_run(j, p[1]);
    }
    close(p[1]);
    (void)fcntl(p[0], F_SETFL, fcntl(p[0], F_GETFL) | O_NONBLOCK);
    s_pipe = p[0];
    s_child = pid;
    s_st.job_pid = (long)pid;
    return 0;
}

/* ==========================================================================
 * The push sink (config_agent.h "SLOT-IMAGE pushes")
 * ========================================================================== */
static struct {
    job_t    j;               /* what the verifier child will need              */
    int      slot;            /* 0 = A, 1 = B                                   */
    uint32_t total;           /* payload bytes announced                        */
    uint32_t got;
    int      table_ok;
    table_t  t;
    uint32_t rcrc[S0_MAX_ENTRIES];   /* each region's CRC, streamed             */
    int      body_started;    /* the old first sector was zeroed                */
    int      active;
} s_push;

static void push_crc_span(const uint8_t *p, uint32_t pos, uint32_t n)
{
    for (uint32_t i = 0; i < s_push.t.h.num_entries; i++) {
        const struct s0_entry *e = &s_push.t.e[i];
        uint64_t a = pos > e->src_offset ? pos : e->src_offset;
        uint64_t b = (uint64_t)pos + n < (uint64_t)e->src_offset + e->len
                         ? (uint64_t)pos + n : (uint64_t)e->src_offset + e->len;
        if (a < b) {
            s_push.rcrc[i] = s0_crc32_update(s_push.rcrc[i], p + (a - pos), (uint32_t)(b - a));
        }
    }
}

static int push_begin(uint32_t total)
{
    s_push.total = total;
    s_push.got = 0;
    s_push.table_ok = 0;
    s_push.body_started = 0;
    s_push.j.hold_len = 0;
    memset(s_push.rcrc, 0, sizeof(s_push.rcrc));
    s_push.j.fd = card_open(O_RDWR);
    if (s_push.j.fd < 0) {
        s_st.job_act = MPS3_SLOT_JOB_PUSH;
        s_st.job_slot = (uint8_t)(s_push.slot + 1);
        job_fail("%s", card_err(s_push.j.fd));
        return -1;
    }
    s_push.active = 1;
    /* A push invalidates what this boot knew about its slot. */
    s_st.vcrc[s_push.slot] = 0u;
    s_st.vsid[s_push.slot] = 0u;
    if (s_st.staged == (uint8_t)(s_push.slot + 1)) {
        s_st.staged = MPS3_SLOT_NONE;
    }
    s_st.job_act = MPS3_SLOT_JOB_PUSH;
    s_st.job_state = MPS3_SLOT_JS_WRITING;
    s_st.job_slot = (uint8_t)(s_push.slot + 1);
    s_st.job_got = 0;
    s_st.job_len = total;
    s_st.job_err[0] = '\0';
    s_st.job_pid = (long)getpid();   /* "writing" belongs to this process */
    state_save();
    harnessd_log("slot: push -> slot %c (%" PRIu32 " B)\n", 'A' + s_push.slot, total);
    return 0;
}

static void push_close(void)
{
    if (s_push.j.fd >= 0) {
        close(s_push.j.fd);
    }
    s_push.j.fd = -1;
    s_push.active = 0;
}

static int push_write(const void *buf, uint32_t len)
{
    const uint8_t *p = buf;
    uint64_t lo = s_push.j.base, hi = s_push.j.base + s_push.j.size - BLK;   /* never the record */
    while (len > 0) {
        uint32_t pos = s_push.got;
        uint32_t n = len;
        if (pos < HOLD_BYTES) {
            /* The first sector is HELD, and the table is checked from it before a
             * single byte reaches the card: a refused image never touches it. */
            n = (len < HOLD_BYTES - pos) ? len : HOLD_BYTES - pos;
            memcpy(s_push.j.hold + pos, p, n);
            s_push.j.hold_len = pos + n;
            if (!s_push.table_ok && s_push.j.hold_len >= sizeof(struct s0_header)) {
                table_t t;
                uint32_t have = s_push.j.hold_len;
                const char *why = table_check(s_push.j.hold, have, s_push.total, &t);
                int need_more = why && (strcmp(why, "table truncated") == 0) &&
                                have < s_push.total && have < HOLD_BYTES;
                if (why && !need_more) {
                    job_fail("%s", why);
                    return -1;
                }
                if (!why) {
                    s_push.t = t;
                    s_push.table_ok = 1;
                    s_push.j.want = t.h.header_crc32;
                    s_push.j.extent = t.extent;
                    push_crc_span(s_push.j.hold, 0, s_push.j.hold_len);   /* bytes already held */
                    goto advanced;
                }
            }
            if (s_push.table_ok) {
                push_crc_span(p, pos, n);
            }
        } else {
            if (!s_push.table_ok) {
                job_fail("table truncated");
                return -1;
            }
            if (!s_push.body_started) {
                /* The old image's first sector goes FIRST, so from here on the slot
                 * is not a valid image until the child writes the new one last. */
                uint8_t zero[BLK];
                memset(zero, 0, sizeof(zero));
                if (card_wr(s_push.j.fd, s_push.j.base, zero, BLK, lo, hi) != 0) {
                    job_fail("card write: %s", strerror(errno));
                    return -1;
                }
                s_push.body_started = 1;
            }
            if (card_wr(s_push.j.fd, s_push.j.base + pos, p, n, lo, hi) != 0) {
                job_fail("card write: %s", strerror(errno));
                return -1;
            }
            push_crc_span(p, pos, n);
        }
advanced:
        s_push.got += n;
        s_st.job_got = s_push.got;
        p += n;
        len -= n;
    }
    return 0;
}

static int push_finish(uint32_t crc)
{
    (void)crc;   /* config_agent has already checked the transport CRC */
    if (!s_push.table_ok) {
        job_fail("table truncated");
        push_close();
        return -1;
    }
    for (uint32_t i = 0; i < s_push.t.h.num_entries; i++) {
        if (s_push.rcrc[i] != s_push.t.e[i].crc32) {
            job_fail("region %" PRIu32 " CRC", i);
            push_close();
            return -1;
        }
    }
    s_st.job_state = MPS3_SLOT_JS_VERIFYING;
    if (job_spawn(&s_push.j) != 0) {
        job_fail("fork: %s", strerror(errno));
        push_close();
        return -1;
    }
    push_close();   /* the child has its own copy of the fd */
    state_save();
    harnessd_log("slot: push to %c complete (table CRC 0x%08" PRIx32 "); card read-back running\n",
                 'A' + s_push.slot, s_push.j.want);
    return 0;
}

static void push_abort(void)
{
    if (s_st.job_state == MPS3_SLOT_JS_WRITING) {
        job_fail("%s", s_push.got < s_push.total ? "torn (the push stopped early)" : "aborted");
    }
    push_close();
}

static const mps3_cfg_agent_qspi_sink_t s_push_sink = {
    .begin = push_begin, .write = push_write, .finish = push_finish, .abort = push_abort,
};

/* The provider: policy only, and the refusal reason for `status`. */
const mps3_cfg_agent_qspi_sink_t *mps3_cfg_slot_sink(const mps3_bitstream_hdr_t *hdr)
{
    if (busy()) {
        return 0;   /* the running job keeps its status; the pusher sees the close */
    }
    mps3_slot_status_t st;
    card_t c;
    table_t t[2];
    const char *why = fabric_err();
    uint8_t slot = MPS3_SLOT_NONE;
    char msg[64] = "";
    int rc = 0;
    if (!why && hdr->static_id != harnessd_fabric_static_id()) {
        snprintf(msg, sizeof(msg), "image for 0x%08" PRIx32 " != fabric 0x%08" PRIx32,
                 hdr->static_id, harnessd_fabric_static_id());
        why = msg;
    }
    if (!why && (rc = status_fill(&st, &c, t, 0)) != 0) {
        why = card_err(rc);
    }
    if (!why) {
        slot = st.target;
        if (slot == MPS3_SLOT_NONE) {
            snprintf(msg, sizeof(msg), "no free slot: %c runs, %c is the default -- rollback first",
                     st.running == MPS3_SLOT_RUN_B ? 'B' : 'A', st.deflt == MPS3_SLOT_B ? 'B' : 'A');
            why = msg;
        } else if (hdr->rm_slot != 0u && hdr->rm_slot != slot) {
            snprintf(msg, sizeof(msg), "slot mismatch: the target is %c", slot == MPS3_SLOT_B ? 'B' : 'A');
            why = msg;
        } else if (c.bad[slot - 1][0]) {
            snprintf(msg, sizeof(msg), "slot %c bad: %s", slot == MPS3_SLOT_B ? 'B' : 'A',
                     c.bad[slot - 1]);
            why = msg;
        } else if (c.slot[slot - 1].first_lba == 0u) {
            snprintf(msg, sizeof(msg), "slot %c absent", slot == MPS3_SLOT_B ? 'B' : 'A');
            why = msg;
        } else if ((uint64_t)hdr->len_words * 4u > slot_bytes(&c, slot - 1) - BLK) {
            snprintf(msg, sizeof(msg), "image too large for slot %c", slot == MPS3_SLOT_B ? 'B' : 'A');
            why = msg;
        }
    }
    if (why) {
        s_st.job_act = MPS3_SLOT_JOB_PUSH;
        s_st.job_slot = slot;
        s_st.job_got = 0;
        s_st.job_len = hdr->len_words * 4u;
        job_fail("%s", why);
        return 0;
    }
    memset(&s_push, 0, sizeof(s_push));
    s_push.j.fd = -1;
    s_push.j.mode = MPS3_SLOT_JOB_PUSH;
    s_push.slot = slot - 1;
    s_push.j.base = slot_off(&c, slot - 1);
    s_push.j.size = slot_bytes(&c, slot - 1);
    s_push.j.sid = hdr->static_id;
    return &s_push_sink;
}

/* ==========================================================================
 * The verb (coordinator.h mps3_slot_op)
 * ========================================================================== */

/* Rule 2: may `dest` become the default? NULL = yes. */
static const char *flip_ok(const mps3_slot_status_t *st, int dest, char *msg, size_t cap)
{
    const mps3_slot_info_t *si = &st->s[dest];
    char L = (char)('A' + dest);
    if (si->state != MPS3_SLOT_ST_VALID) {
        snprintf(msg, cap, "slot %c is not a valid image", L);
        return msg;
    }
    if (si->verified == MPS3_SLOT_VER_READBACK) {
        if (s_st.vsid[dest] != st->fabric_sid) {
            snprintf(msg, cap, "slot %c is for 0x%08" PRIx32 " != fabric 0x%08" PRIx32, L,
                     s_st.vsid[dest], st->fabric_sid);
            return msg;
        }
        return 0;
    }
    if (si->verified == MPS3_SLOT_VER_BOOT) {
        const char *lock = harnessd_identity_reason();
        if (lock) {
            snprintf(msg, cap, "slot %c runs, but identity lock: %.30s", L, lock);
            return msg;
        }
        return 0;
    }
    if (s_st.vcrc[dest] != 0u) {
        snprintf(msg, cap, "slot %c changed since it was verified", L);
        return msg;
    }
    snprintf(msg, cap, "slot %c not verified", L);
    return msg;
}

/* The boot-select writer (rule 5): slot_card_bootsel_write(), the SAME code
 * `mps3-slot default` runs. 0 ok; else a reason. */
static const char *flip_to(int dest, card_t *c, char *msg, size_t cap)
{
    if ((int)c->deflt == dest + 1) {
        return 0;   /* already the default: nothing to write */
    }
    int fd = card_open(O_RDWR);
    if (fd < 0) {
        return card_err(fd);
    }
    unsigned lba = 0;
    uint32_t d = 0, seq = 0;
    int rc = slot_card_bootsel_write(fd, c, dest, &lba, &d, &seq);
    close(fd);
    if (rc == -3) {
        snprintf(msg, cap, "boot-select read-back (default %" PRIu32 " seq %" PRIu32 ")", d, seq);
        return msg;
    }
    if (rc == -2) {   /* slot_card's overlap guard: nothing was written */
        snprintf(msg, cap, "boot-select refused: %s",
                 c->bootsel_bad[0] ? c->bootsel_bad : c->bad[dest]);
        return msg;
    }
    if (rc != 0) {
        snprintf(msg, cap, "boot-select write: %s", strerror(errno));
        return msg;
    }
    harnessd_log("slot: default -> %c (seq %" PRIu32 ", LBA %u)\n", 'A' + dest, c->seq, lba);
    return 0;
}

static const char *do_verify(const mps3_slot_status_t *st, const card_t *c, int sel,
                             char *msg, size_t cap)
{
    int dest = (sel != MPS3_SLOT_NONE) ? sel - 1 : (int)(2u - st->deflt);   /* the other slot */
    char L = (char)('A' + dest);
    if (st->s[dest].state != MPS3_SLOT_ST_VALID) {
        snprintf(msg, cap, "slot %c is not a valid image", L);
        return msg;
    }
    static job_t j;
    memset(&j, 0, sizeof(j));
    j.mode = MPS3_SLOT_JOB_VERIFY;
    j.fd = card_open(O_RDONLY);
    if (j.fd < 0) {
        return card_err(j.fd);
    }
    j.base = slot_off(c, dest);
    j.size = slot_bytes(c, dest);
    j.fabric = st->fabric_sid;
    s_st.vcrc[dest] = 0u;
    s_st.vsid[dest] = 0u;
    s_st.job_act = MPS3_SLOT_JOB_VERIFY;
    s_st.job_state = MPS3_SLOT_JS_VERIFYING;
    s_st.job_slot = (uint8_t)(dest + 1);
    s_st.job_got = 0;
    s_st.job_len = st->s[dest].len;
    s_st.job_err[0] = '\0';
    int rc = job_spawn(&j);
    close(j.fd);
    if (rc != 0) {
        job_fail("fork: %s", strerror(errno));
        return s_st.job_err;
    }
    state_save();
    harnessd_log("slot: verify %c started (card read-back)\n", L);
    return 0;
}

/* coordinator.h: this engine serves the verb -> version.features "slot" (bit 14). */
int mps3_slot_supported(void)
{
    return 1;
}

const char *mps3_slot_op(int act, int sel, mps3_slot_status_t *st)
{
    static char msg[64];
    card_t c;
    table_t t[2];
    harnessd_slot_poll();   /* a finished job is reported by the reply to THIS request */
    if (act == MPS3_SLOT_ACT_COMMIT || act == MPS3_SLOT_ACT_ROLLBACK) {
        /* THE LOCK, first: a peer that may not mutate learns nothing more. */
        if (active_peer_locked(act == MPS3_SLOT_ACT_COMMIT ? "commit" : "rollback")) {
            return SLOT_LOCKED_ERR;
        }
    }
    int rc = status_fill(st, &c, t, 0);
    if (act == MPS3_SLOT_ACT_STATUS) {
        return (rc == 0 || rc == -2) ? 0 : card_err(rc);   /* no card: card:false, ok */
    }
    if (rc != 0) {
        return card_err(rc);
    }
    const char *why = fabric_err();
    if (why) {
        return why;
    }
    if (busy()) {
        return "EBUSY";
    }
    if (act == MPS3_SLOT_ACT_VERIFY) {
        why = do_verify(st, &c, sel, msg, sizeof(msg));
    } else {
        int dest;
        if (act == MPS3_SLOT_ACT_COMMIT) {
            if (s_st.staged == MPS3_SLOT_NONE) {
                return "nothing staged: push an image first";
            }
            dest = s_st.staged - 1;
        } else {
            dest = (int)(2u - c.deflt);   /* rollback: the slot that is not the default */
        }
        if (sel != MPS3_SLOT_NONE && sel != dest + 1) {
            snprintf(msg, sizeof(msg), "slot mismatch: %s would pick %c",
                     act == MPS3_SLOT_ACT_COMMIT ? "commit" : "rollback", 'A' + dest);
            return msg;
        }
        why = flip_ok(st, dest, msg, sizeof(msg));
        if (!why) {
            why = flip_to(dest, &c, msg, sizeof(msg));
        }
    }
    if (why) {
        return why;
    }
    (void)status_fill(st, &c, t, 0);   /* the reply is the state AFTER the act */
    return 0;
}

/* ==========================================================================
 * The booted slot's record (HM_ANSWERS_2026-09-26 S1, change 1)
 * ========================================================================== */
/* Stamp the slot record ("S0SR", source 2 = boot) of the slot stage0 booted this
 * OS from, so an image that booted healthy once can be `verify`'d -- and so be
 * rolled back to -- after a later reboot, whoever wrote it (stage0_mkcard.py +
 * dd, `mps3-slot write`, the factory). main_linux.c calls it once the boot is
 * CONFIRMED; it is idempotent, so a respawn calls it again. It writes ONLY when:
 *   - stage0 handed off from slot A or B;
 *   - att_confirm holds S0_CONFIRM_MAGIC (this boot was confirmed to stage0);
 *   - there is no identity lock -- only then is the fabric static_id also the
 *     running image's (flip_ok()'s `verified: boot` gate);
 *   - the card's table CRC for that slot is stage0's image_hdr_crc (the image
 *     stage0 CRC-checked at hand-off is the one on the card: `verified: boot`);
 *   - the image ends before the record sector (a push is capped there, an image
 *     written by another tool may not be -- then nothing is written);
 *   - no record already holds (hdr_crc, fabric static_id).
 * The write is ONE sector, bounded by the write guard to the record sector
 * itself (inside the slot's range), flushed, the cache dropped, and read back
 * off the card -- the same class as the boot-select flip, so it runs on the loop
 * (file header rule 3). Returns 1 stamped, 0 nothing to do (a final answer, logged
 * once), -1 try again later (a card job holds the card). */
int harnessd_slot_stamp_booted(void)
{
    uint8_t run = running_slot();
    char why[96] = "";
    int rc = 0, fd = -1;

    if (run != MPS3_SLOT_RUN_A && run != MPS3_SLOT_RUN_B) {
        snprintf(why, sizeof(why), "running %s, not a slot",
                 run == MPS3_SLOT_RUN_RESCUE ? "rescue" : run == MPS3_SLOT_RUN_NONE ? "none" : "unknown");
        goto out;
    }
    int i = run - 1;
    char L = (char)('A' + i);
    uint32_t confirm = harnessd_s0_read(S0_OFF(att_confirm));
    if (confirm != S0_CONFIRM_MAGIC) {
        snprintf(why, sizeof(why), "boot not confirmed (att_confirm 0x%08" PRIx32 ")", confirm);
        goto out;
    }
    const char *lock = harnessd_identity_reason();
    if (lock) {
        snprintf(why, sizeof(why), "identity lock: %.60s", lock);
        goto out;
    }
    uint32_t fabric = harnessd_fabric_static_id();
    if (fabric == 0u) {
        snprintf(why, sizeof(why), "fabric static_id unknown");
        goto out;
    }
    if (busy()) {
        return -1;   /* a push or verify holds the card: stamp once it is done */
    }
    fd = card_open(O_RDWR);
    if (fd < 0) {
        snprintf(why, sizeof(why), "%s", card_err(fd));
        goto out;
    }
    card_t c;
    table_t t;
    uint8_t first[BLK];
    if (card_read(fd, &c) != 0) {
        snprintf(why, sizeof(why), "card io");
        goto out;
    }
    if (c.bad[i][0] || c.slot[i].first_lba == 0u) {
        snprintf(why, sizeof(why), "slot %c %s", L, c.bad[i][0] ? c.bad[i] : "absent");
        goto out;
    }
    uint64_t base = slot_off(&c, i), size = slot_bytes(&c, i);
    if (card_rd(fd, base, first, BLK) != 0) {
        snprintf(why, sizeof(why), "slot %c read: %s", L, strerror(errno));
        goto out;
    }
    const char *bad = table_check(first, BLK, (uint32_t)size, &t);
    if (bad) {
        snprintf(why, sizeof(why), "slot %c: %s", L, bad);
        goto out;
    }
    uint32_t boot_crc = harnessd_s0_read(S0_OFF(image_hdr_crc));
    if (t.h.header_crc32 != boot_crc) {
        snprintf(why, sizeof(why), "slot %c table CRC 0x%08" PRIx32 " != booted 0x%08" PRIx32,
                 L, t.h.header_crc32, boot_crc);
        goto out;
    }
    if ((uint64_t)t.extent > size - BLK) {
        snprintf(why, sizeof(why), "slot %c image reaches the record sector", L);
        goto out;
    }
    uint32_t sid = 0;
    if (record_read(fd, &c, i, boot_crc, &sid) && sid == fabric) {
        snprintf(why, sizeof(why), "slot %c already recorded", L);
        goto out;
    }
    uint8_t rec[BLK], back[BLK];
    uint64_t roff = base + size - BLK;
    record_build(rec, boot_crc, fabric, t.extent, REC_SRC_BOOT);
    if (card_wr(fd, roff, rec, BLK, roff, roff + BLK) != 0 ||
        slot_card_flush_uncache(fd, roff, BLK) != 0) {
        snprintf(why, sizeof(why), "slot %c record write: %s", L, strerror(errno));
        goto out;
    }
    if (card_rd(fd, roff, back, BLK) != 0 || memcmp(rec, back, BLK) != 0) {
        snprintf(why, sizeof(why), "slot %c record read-back", L);
        goto out;
    }
    rc = 1;
    harnessd_log("slot: booted slot %c STAMPED (record: table CRC 0x%08" PRIx32 ", static_id 0x%08"
                 PRIx32 ", %" PRIu32 " B, source boot)\n", L, boot_crc, fabric, t.extent);
out:
    if (fd >= 0) {
        close(fd);
    }
    if (rc == 0) {
        harnessd_log("slot: booted slot record not stamped: %s\n", why);
    }
    return rc;
}

/* ==========================================================================
 * Lifecycle
 * ========================================================================== */
void harnessd_slot_init(void)
{
    read_boot_id();
    memset(&s_st, 0, sizeof(s_st));
    s_push.j.fd = -1;
    state_load();
    if (busy() && s_st.job_pid == (long)getpid()) {
        s_st.job_pid = 0;   /* impossible (a fresh process), but never trust our own pid */
    }
    harnessd_log("slot: card %s, state %s%s\n", g_hd.card ? g_hd.card : "(none)",
                 g_hd.slot_state ? g_hd.slot_state : "(memory only)",
                 busy() ? " -- a job from the previous harnessd is still marked running" : "");
}

/* The card-job half of coordinator.h's mps3_card_job_active() (the strong seam
 * is ovlstore_linux.c's: one card, two writers). A finished job is reaped first,
 * so a verifier that has just exited never holds `reboot` off by one pass. */
int harnessd_slot_busy(void)
{
    harnessd_slot_poll();
    return busy();
}

/* Service slot 2 ("persist") calls this every pass: reap the verifier, and
 * notice a job whose owner vanished (a respawn mid-push or mid-verify). */
void harnessd_slot_poll(void)
{
    if (!busy()) {
        return;
    }
    if (s_child > 0) {
        char line[128];
        int status;
        pid_t r = waitpid(s_child, &status, WNOHANG);
        if (r == 0) {
            return;   /* still running */
        }
        ssize_t n = (s_pipe >= 0) ? read(s_pipe, line, sizeof(line) - 1u) : -1;
        line[n > 0 ? n : 0] = '\0';
        if (s_pipe >= 0) {
            close(s_pipe);
        }
        s_pipe = -1;
        s_child = 0;
        s_st.job_pid = 0;
        unsigned long crc = 0, sid = 0;
        if (sscanf(line, "ok %lx %lx", &crc, &sid) == 2) {
            int i = s_st.job_slot - 1;
            s_st.vcrc[i] = (uint32_t)crc;
            s_st.vsid[i] = (uint32_t)sid;
            if (s_st.job_act == MPS3_SLOT_JOB_PUSH) {
                s_st.staged = s_st.job_slot;
            }
            s_st.job_state = MPS3_SLOT_JS_OK;
            s_st.job_err[0] = '\0';
            harnessd_log("slot: %s %c OK (table CRC 0x%08lx, static_id 0x%08lx)\n",
                         s_st.job_act == MPS3_SLOT_JOB_PUSH ? "push" : "verify",
                         'A' + i, crc, sid);
            state_save();
        } else {
            line[strcspn(line, "\n")] = '\0';
            job_fail("%s", strncmp(line, "fail ", 5) == 0 ? line + 5 : "verifier died");
        }
        return;
    }
    /* Not our child: a job the previous harnessd started. "writing" died with its
     * process; a verifier may outlive it -- wait for it, but its result is lost. */
    if (s_st.job_state == MPS3_SLOT_JS_WRITING && !s_push.active) {
        job_fail("harnessd restarted mid-push");
    } else if (s_st.job_state == MPS3_SLOT_JS_VERIFYING &&
               (s_st.job_pid <= 0 || kill((pid_t)s_st.job_pid, 0) != 0)) {
        job_fail("result lost (harnessd restarted): verify again");
    }
}
