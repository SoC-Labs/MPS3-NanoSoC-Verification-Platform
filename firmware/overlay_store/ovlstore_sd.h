/*
 * ovlstore_sd.h -- the overlay store on the USER microSD: the last DUT overlay
 * (clearing + partial) kept in an A/B pair of slots inside an MBR partition of
 * type 0xDA, reloaded at power-on. Item D13, lane L3-core
 * (docs/planning/HANDOVER_USD_OVERLAY_STORE.md §1, §4.5 b-f, §8, §12).
 *
 * THIS IS THE USER CARD, never the MCC config card (V2M_MPS3 / sd_install).
 *
 * It runs over an abstract block device (ovl_bdev.h), so the same engine runs
 * bare metal over firmware/usd/usd.c and under Linux over pread/pwrite. It
 * reuses the SST26 store's header codec (ovlstore_codec.h) unchanged and does
 * not touch overlay_store.c.
 *
 * ============================== CARD LAYOUT ================================
 * LBA 0: MBR. The store's partition is the first entry of TYPE 0xDA (no fixed
 * geometry), bounds-checked against the card size. Inside that partition
 * (partition-relative LBAs, 512-byte blocks):
 *
 *   LBA 0      header copy 0  \  ping-pong: [0..73] the codec's packed header,
 *   LBA 1      header copy 1  /  [74..77] u32le seq, [78..81] u32le CRC-32 of
 *                                [0..77], rest of the block 0. Readers take the
 *                                newest valid copy (wrap-safe seq compare);
 *                                writers overwrite the OTHER one, so a torn
 *                                header write leaves the previous copy intact.
 *   +1 MiB     slot A (8 MiB)    clearing at slot offset 0, then the partial at
 *   +9 MiB     slot B (8 MiB)    the next 512-byte boundary.
 *   >= 17 MiB  (end of layout)   a smaller partition is refused.
 *
 * ================================ STATES ===================================
 * From the device (passthrough):  NO_CARD, NO_HW, INIT, UNSUPPORTED, ERROR.
 * From the store (device READY):
 *   INIT      the MBR / headers are being read, or the default is being verified
 *   FOREIGN   no usable 0xDA partition, or no valid header in it: READ-ONLY
 *             FOREVER. No write of any kind is ever started (format rules below
 *             are the only way out, and they refuse to touch other partitions).
 *   EMPTY     harness card, no default
 *   VALID     the active slot (or, if it failed CRC, the other slot) was read
 *             IN FULL and its clearing and partial CRCs match; its static_id is
 *             the live shell's. Only VALID offers anything for streaming.
 *   STALE     the active slot was minted for another shell (static_id gate).
 *             Its bytes are never even read.
 *   BAD       the active slot fails CRC and the other slot is not usable.
 *   ERROR     also: a store read failed (ERR 30); auto-retried
 *             OVLSTORE_SD_TRIES times, OVLSTORE_SD_RETRY_MS apart.
 *
 * ============================ PER-POLL BUDGET ==============================
 * ovlstore_sd_poll() is the ONLY entry point that touches the device, and one
 * call does at most:
 *   - the provider's own poll (usd: usd_poll(), its budget in firmware/usd/README);
 *   - 1 io_status() + the state/nblocks/change_count queries;
 *   - START 1 block op of at most OVLSTORE_SD_OP_BLOCKS blocks (enforced: a
 *     second start in the same call is refused);
 *   - CRC at most OVLSTORE_SD_CRC_BYTES_PER_POLL bytes in total, header copies
 *     included (512 B: ~0.4 ms on the 100 MHz MicroBlaze -- the mb-gcc -Os
 *     inner loop of the bitwise crc32.c is 8 instructions per bit, ~75
 *     cycles/byte; an estimate, not a board measurement);
 *   - at most one 512-byte parse / pack / compare (MBR, header, read-back).
 * No loop waits on the device: every wait is a state revisited next call.
 * The other entry points do no device I/O: stream_next() is a pointer handoff,
 * commit_feed() is a memcpy of at most 2 x OVLSTORE_SD_OP_BLOCKS blocks.
 * ovlstore_sd_stats() records the maxima; the host tests assert them.
 *
 * ============================== THREADING ==================================
 * None. Single superloop (or one Linux thread). A pointer returned by
 * stream_next() is valid until the next call into the store.
 */
#ifndef MPS3_OVLSTORE_SD_H
#define MPS3_OVLSTORE_SD_H

#include <stdbool.h>
#include <stdint.h>

#include "ovl_bdev.h"
#include "ovlstore_codec.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- layout (blocks unless named _BYTES) ---------------------------------- */
#define OVLSD_BLOCK                 512u
#define OVLSD_MBR_TYPE              0xDAu
#define OVLSD_MBR_FORMAT_ENTRY      3u        /* rule (b) writes entry 4 (index 3) */
#define OVLSD_HDR_COPY0_LBA         0u        /* partition-relative */
#define OVLSD_HDR_COPY1_LBA         1u
#define OVLSD_SLOT_A_LBA            2048u     /* +1 MiB */
#define OVLSD_SLOT_B_LBA            18432u    /* +9 MiB */
#define OVLSD_SLOT_BLOCKS           16384u    /* 8 MiB each */
#define OVLSD_SLOT_BYTES            (OVLSD_SLOT_BLOCKS * OVLSD_BLOCK)
#define OVLSD_MIN_PART_BLOCKS       (OVLSD_SLOT_B_LBA + OVLSD_SLOT_BLOCKS)   /* 17 MiB */
#define OVLSD_FORMAT_PART_BLOCKS    65536u    /* rule (b): the last 32 MiB */
#define OVLSD_ALIGN_BLOCKS          2048u     /* rule (b): 1 MiB-aligned start */
#define OVLSD_MBR_DISK_SIGNATURE    0x4D505333u /* "MPS3", written on a blank card only */
/* The lowest LBA a 0xDA entry may start at: LBA 0 is the MBR and LBA 1-2 are
 * stage0's boot-select copies (STAGE0_CONTRACT §6), which a store header write
 * at partition LBA 0/1 would overwrite. A lower start is DA_BAD. */
#define OVLSD_MBR_FIRST_LBA         3u

/* Header copy block. */
#define OVLSD_HDR_SEQ_OFF           OVLSTORE_HEADER_PACKED_SIZE       /* 74 */
#define OVLSD_HDR_CRC_OFF           (OVLSD_HDR_SEQ_OFF + 4u)          /* 78 */
#define OVLSD_HDR_USED_BYTES        (OVLSD_HDR_CRC_OFF + 4u)          /* 82 */

/* ---- tunables (-D for a bench build) -------------------------------------- */
/* Blocks per device op, and the size of each of the store's TWO buffers
 * (2 x 8 x 512 = 8 KiB of RAM). Capped by ovl_bdev_t.max_blocks (usd: 8).
 * Multi-block ops matter on real cards: CMD18/CMD25 pay the card's access
 * latency once per op, not once per block. */
#ifndef OVLSTORE_SD_OP_BLOCKS
#define OVLSTORE_SD_OP_BLOCKS          8u
#endif
/* CRC bytes per poll, ALL CRC work included (region data and header copies;
 * floor 156 = the probe's two 78-byte header CRCs, static-asserted). crc32.c is
 * bitwise (~75 cycles/byte on MicroBlaze): 512 B is ~0.4 ms at 100 MHz. One
 * block per pass keeps pace with usd.c's one-block-per-poll read rate. */
#ifndef OVLSTORE_SD_CRC_BYTES_PER_POLL
#define OVLSTORE_SD_CRC_BYTES_PER_POLL 512u
#endif
#ifndef OVLSTORE_SD_RETRY_MS
#define OVLSTORE_SD_RETRY_MS           2000u   /* store read error -> re-probe */
#endif
#ifndef OVLSTORE_SD_TRIES
#define OVLSTORE_SD_TRIES              3u      /* probes per card before ERR 30 sticks */
#endif

/* ERR <n> numbers the STORE adds (the device's own are usd.h's 1..23). */
#define OVLSD_ERRNUM_READ              30      /* a store read failed (-EIO etc.) */

/* ---- states ---------------------------------------------------------------- */
typedef enum {
    OVLSD_NO_CARD = 0,
    OVLSD_NO_HW,
    OVLSD_INIT,
    OVLSD_UNSUPPORTED,
    OVLSD_ERROR,
    OVLSD_FOREIGN,
    OVLSD_EMPTY,
    OVLSD_VALID,
    OVLSD_STALE,
    OVLSD_BAD
} ovlstore_sd_state_t;

/* ---- return codes -------------------------------------------------------- *
 * Negative = refused / failed. ovlstore_sd_rc_name() gives the wire NAME;
 * the `usd` / `commit` verbs send names, never numbers (handover §8). */
enum {
    OVLSD_OK         =  0,
    OVLSD_BUSY       =  1,   /* nothing yet: poll, then call again         */
    OVLSD_DONE       =  2,   /* stream: the region has been fully handed out */
    OVLSD_ENOCARD    = -1,   /* "no sd card"                                */
    OVLSD_ENOHW      = -2,   /* "no usd hardware"                           */
    OVLSD_ENOTREADY  = -3,   /* "card not ready" (init / unsupported / error) */
    OVLSD_EFOREIGN   = -4,   /* "foreign card"                              */
    OVLSD_ESTALE     = -5,   /* "stale key"                                 */
    OVLSD_ESTATIC    = -6,   /* "static_id mismatch"                        */
    OVLSD_ERMID      = -7,   /* "rm_id mismatch"                            */
    OVLSD_EBUSY      = -8,   /* "store busy" (probe / verify / stream / write) */
    OVLSD_EARG       = -9,   /* "bad argument"                              */
    OVLSD_ETOOBIG    = -10,  /* "too big for slot"                          */
    OVLSD_EORDER     = -11,  /* "out of order"                              */
    OVLSD_ELEN       = -12,  /* "length mismatch"                           */
    OVLSD_EIO        = -13,  /* "io error"                                  */
    OVLSD_ECRC       = -14,  /* "crc mismatch"                              */
    OVLSD_EVERIFY    = -15,  /* "read-back mismatch"                        */
    OVLSD_EGONE      = -16,  /* "card removed"                              */
    OVLSD_ENODEFAULT = -17,  /* "no default"                                */
    OVLSD_ESKIPPED   = -18,  /* "skipped"                                   */
    OVLSD_EABORTED   = -19,  /* "aborted"                                   */
    OVLSD_ECONFIRM   = -20,  /* "confirm required"                          */
    OVLSD_EEXIST     = -21,  /* "provision p4 first" (format rule c)        */
    OVLSD_EUNKNOWN   = -22,  /* "unrecognised lba 0"                        */
    OVLSD_EPART      = -23,  /* "bad 0xDA partition"                        */
    OVLSD_ESMALL     = -24,  /* "partition too small"                       */
    OVLSD_EFSSIG     = -25,  /* "filesystem present" (rule b: not blank)    */
    OVLSD_ENOWIPE    = -26   /* "wipe disabled" (cfg.allow_wipe is false)   */
};

/* ---- MBR classification (pure; exported for the Linux provider selection) -- */
/* "Blank" (format rule b) means TRULY blank: LBA 0 all ZERO (all-0xFF is not
 * blank), or a signed MBR whose four 16-byte entries are all zero -- AND no
 * filesystem or GPT signature in the blocks LBA 0 alone cannot show (the
 * signature table in ovlstore_sd.c: GPT header at LBA 1, ext2/3/4 and f2fs
 * superblocks at LBA 2, Linux swap at LBA 7, ISO 9660 at LBA 64, btrfs at
 * LBA 128). A signature turns BLANK/EMPTY into GPT/FS: FOREIGN, refused. */
typedef enum {
    OVLSD_MBR_NOT_READ = 0,
    OVLSD_MBR_BLANK,      /* all 0x00, no signature behind it              -> format rule (b) */
    OVLSD_MBR_EMPTY,      /* 0x55AA, four all-zero entries, no signature   -> format rule (b) */
    OVLSD_MBR_DA,         /* a 0xDA entry, in bounds, >= OVLSD_MIN_PART_BLOCKS -> the store */
    OVLSD_MBR_DA_SMALL,   /* a 0xDA entry smaller than the layout          -> refused */
    OVLSD_MBR_DA_BAD,     /* a 0xDA entry out of the card's bounds, starting below
                           * OVLSD_MBR_FIRST_LBA, or sharing a block with any other
                           * typed entry (stage0's slots, /persist, ...) -> refused */
    OVLSD_MBR_OTHER,      /* partitions, none 0xDA (incl. GPT's 0xEE)      -> rule (c): refused */
    OVLSD_MBR_FS,         /* a filesystem: FAT/exFAT/NTFS boot sector at LBA 0 (with or
                           * without 0x55AA), or a table signature further in -> refused */
    OVLSD_MBR_UNKNOWN,    /* not zero and not a clean MBR (incl. all 0xFF, no 0x55AA,
                           * a bad boot flag, entry bytes without a type)  -> refused */
    OVLSD_MBR_RAW,        /* raw-partition mode: the device IS the partition */
    OVLSD_MBR_GPT         /* a GPT header ("EFI PART") at LBA 1            -> refused */
} ovlsd_mbr_kind_t;

/* LBA 0 alone. BLANK / EMPTY here still need the signature walk (the store
 * does it in the probe and again in format) before rule (b) may write. */
ovlsd_mbr_kind_t ovlstore_sd_mbr_classify(const uint8_t lba0[512], uint32_t nblocks,
                                          uint32_t *part_lba, uint32_t *part_blocks);

/* The format confirmations. They are distinct on purpose: "erase" only ever
 * applies rules (a)/(b)/(c); "erase-all" is the explicit wipe. */
#define OVLSD_CONFIRM_FORMAT        "erase"
#define OVLSD_CONFIRM_WIPE          "erase-all"
/* The wipe zeroes LBA 1..OVLSD_WIPE_LAST_LBA before it writes the new MBR: the
 * primary GPT header + entry array (LBA 1-33), which also covers the ext
 * superblock (LBA 2) and the FAT32/exFAT backup boot sectors (LBA 6, 12). */
#define OVLSD_WIPE_LAST_LBA         33u

/* ---- configuration --------------------------------------------------------- */
/* rm_id -> short name for the CLCD (e.g. the overlay manifest table). NULL or
 * "" = show the rm_id as 0xXXXXXXXX. */
typedef const char *(*ovlstore_sd_name_fn)(uint32_t rm_id, void *ctx);

typedef struct {
    uint32_t            live_static_id;  /* mps3_shell_static_id(): the STALE gate */
    bool                raw_partition;   /* the bdev IS the 0xDA partition (Linux
                                          * /dev/mmcblk0p4): no MBR read, format =
                                          * rule (a) only */
    bool                allow_wipe;      /* "erase-all" permitted. TRUE on the bare-
                                          * metal shell only. The Linux daemon
                                          * leaves it FALSE: there the card also
                                          * holds the running system (p1-p3) */
    ovlstore_sd_name_fn rm_name;
    void               *rm_name_ctx;
} ovlstore_sd_cfg_t;

/* What a slot holds / what a commit writes. */
typedef struct {
    uint32_t rm_id;
    uint32_t static_id;
    uint32_t clear_len;    /* bytes, > 0, multiple of 4 */
    uint32_t clear_crc;    /* zlib CRC-32 of the clearing */
    uint32_t part_len;     /* bytes, > 0, multiple of 4 */
    uint32_t part_crc;
} ovlstore_sd_desc_t;

typedef enum { OVLSD_CLEARING = 0, OVLSD_PARTIAL = 1 } ovlstore_sd_which_t;

typedef struct {
    uint32_t polls;
    uint32_t ops_started, reads_started, writes_started;
    uint32_t blocks_read, blocks_written;
    uint32_t crc_bytes;
    uint32_t max_ops_per_poll;     /* <= 1 by construction */
    uint32_t max_crc_per_poll;     /* <= OVLSTORE_SD_CRC_BYTES_PER_POLL */
    uint32_t max_blocks_per_op;    /* <= OVLSTORE_SD_OP_BLOCKS */
} ovlstore_sd_stats_t;

/* For the `usd` verb (L4). */
typedef struct {
    ovlstore_sd_state_t state;
    const char *state_name;        /* "none","no_hw","init","unsupported","error",
                                    * "foreign","empty","valid","stale","bad" */
    int         err;               /* the ERR <n> number, 0 = none */
    bool        present;           /* a card is in the slot (any state but NO_CARD/NO_HW) */
    uint32_t    card_mb;           /* 0 unless the device is READY */
    bool        have_default;      /* state == VALID */
    char        slot;              /* 'A' / 'B' when have_default */
    bool        fallback;          /* the default is the NON-active slot (active failed CRC) */
    ovlstore_sd_desc_t def;        /* verified descriptor when have_default */
    ovlsd_mbr_kind_t mbr;          /* how LBA 0 was classified */
    uint32_t    part_lba, part_blocks;
    bool        skip;
    bool        busy;              /* a job is running */
    int         last_result;       /* result of the last finished job */
} ovlstore_sd_info_t;

/* ---- the store object (fields are PRIVATE; sized here so callers own it) --- */
typedef struct {
    uint8_t  st;          /* buffer state */
    uint8_t  last;        /* holds the last block of the region */
    uint32_t lba;         /* absolute LBA of its first block */
    uint32_t nblk;        /* blocks in its op */
    uint32_t bytes;       /* region bytes it holds (read) / has been fed (write) */
    uint32_t pos;         /* CRC / hand-out position */
} ovlsd_buf_t;

typedef struct {
    uint32_t lba, len, nblk, issued, crc, crcd, expect;
    uint8_t  deliver;
} ovlsd_rr_t;

typedef struct ovlstore_sd {
    ovl_bdev_t          bd;
    ovlstore_sd_cfg_t   cfg;
    ovlstore_sd_state_t state;
    int                 err;
    uint32_t            now;
    /* card */
    uint8_t             dev_ready, skip;
    uint32_t            cc, nblocks;
    ovlsd_mbr_kind_t    mbr;
    uint32_t            part_lba, part_blocks;
    /* header */
    uint8_t             hdr_known, hdr_newest;
    uint32_t            hdr_seq;
    ovlstore_header_t   hdr;          /* the newest valid copy, as on the card */
    ovlstore_header_t   hdr_pending;  /* being written; adopted once read back */
    uint32_t            fmt_lba, fmt_blocks;
    uint32_t            fmt_next, fmt_n;       /* wipe cursor */
    uint8_t             fmt_all, fmt_filled;   /* "erase-all"; mem[0] zeroed */
    ovlsd_mbr_kind_t    fmt_kind;              /* rule (b): BLANK or EMPTY */
    uint8_t             sig_i;                 /* signature walk position */
    /* default */
    uint8_t             chosen, fallback;
    /* retry */
    uint8_t             tries, retry_pending;
    uint32_t            t_retry;
    /* job */
    uint8_t             job, last_job, ph, sub;
    uint32_t            gen;
    int                 job_result;
    /* the one device op */
    uint8_t             io_inflight, io_kind, io_buf, io_completed, started;
    uint32_t            io_gen;
    int                 io_result;
    /* verify */
    uint8_t             vslot, vfallback;
    /* buffers / region reader */
    ovlsd_rr_t          rr;
    uint8_t             fill, crcx, drain;
    ovlsd_buf_t         b[2];
    /* commit */
    ovlstore_sd_desc_t  cdesc;
    uint8_t             ctarget;
    char                commit_slot;
    uint32_t            cfed_clear, cfed_part, cwlba, cwend;
    /* text + stats */
    char                text[17];
    uint32_t            poll_crc, poll_ops;
    ovlstore_sd_stats_t stats;
    uint8_t             mem[2][OVLSTORE_SD_OP_BLOCKS * OVLSD_BLOCK];
} ovlstore_sd_t;

/* ---- lifecycle --------------------------------------------------------------- */
/* Bind to a device. Does no I/O: the first ovlstore_sd_poll() looks. */
void ovlstore_sd_init(ovlstore_sd_t *s, const ovl_bdev_t *bd, const ovlstore_sd_cfg_t *cfg);
/* The superloop hook. Bounded work (see PER-POLL BUDGET). */
void ovlstore_sd_poll(ovlstore_sd_t *s, uint32_t now_ms);
/* Re-read the card from scratch (MBR, headers, verify). OVLSD_EBUSY while a
 * job runs, OVLSD_ENOCARD etc. when the device is not READY. */
int  ovlstore_sd_rescan(ovlstore_sd_t *s);
/* PB1 held at boot: text "skipped" while a card is in, stream refused.
 * Cleared by a removal of a READY card, a successful format/clear/commit, or
 * set_skip(false). */
void ovlstore_sd_set_skip(ovlstore_sd_t *s, bool skip);

/* ---- status ------------------------------------------------------------------ */
ovlstore_sd_state_t ovlstore_sd_state(const ovlstore_sd_t *s);
int         ovlstore_sd_error_code(const ovlstore_sd_t *s);   /* the ERR <n> number */
/* CLCD row 4 text, <= 16 ASCII chars: "none", "no hw", "init", "unsupported",
 * "ERR <n>", "foreign", "empty", "<rm> [A]", "stale key", "bad", "skipped".
 * Valid until the next call. */
const char *ovlstore_sd_state_text(ovlstore_sd_t *s);
const char *ovlstore_sd_state_name(ovlstore_sd_state_t st);
const char *ovlstore_sd_rc_name(int rc);
/* The verified default (state VALID only): 0 and *out / *slot filled, else
 * OVLSD_ENODEFAULT. slot = 'A' or 'B'. */
int  ovlstore_sd_default(const ovlstore_sd_t *s, ovlstore_sd_desc_t *out, char *slot);
/* What the header NAMES as the default, for reporting only (the `usd` verb's
 * "default" key): VALID -> the verified default (as ovlstore_sd_default());
 * STALE -> the active slot's descriptor as the header records it (its bytes are
 * never read, and nothing may stream it); else OVLSD_ENODEFAULT. */
int  ovlstore_sd_header_default(const ovlstore_sd_t *s, ovlstore_sd_desc_t *out, char *slot);
void ovlstore_sd_info(ovlstore_sd_t *s, ovlstore_sd_info_t *out);
/* OVLSD_BUSY while a job runs, else the last finished job's result. */
int  ovlstore_sd_job_result(const ovlstore_sd_t *s);
/* Which job is running (the glue's diag phase and its "wait for the store"
 * checks). Values track ovlstore_sd.c's private J_* one for one. */
typedef enum {
    OVLSD_JOB_NONE = 0, OVLSD_JOB_PROBE, OVLSD_JOB_VERIFY, OVLSD_JOB_STREAM,
    OVLSD_JOB_COMMIT, OVLSD_JOB_FORMAT, OVLSD_JOB_CLEAR
} ovlstore_sd_job_t;
ovlstore_sd_job_t ovlstore_sd_job(const ovlstore_sd_t *s);
const ovlstore_sd_stats_t *ovlstore_sd_stats(const ovlstore_sd_t *s);
void ovlstore_sd_stats_reset(ovlstore_sd_t *s);

/* ---- stream (a swap source: src "usd") ---------------------------------------
 * begin: state VALID, not skipped, no job running. Re-reads the region and
 * CRCs it again as it goes; the region's LAST buffer is withheld unless the
 * running CRC matches the descriptor, so a re-read that went bad never
 * completes the bitstream.
 * next: OVLSD_OK with *data and *len (len <= max_bytes; a multiple of 4 whenever
 * max_bytes is), OVLSD_BUSY (poll and ask again), OVLSD_DONE (all handed out),
 * or a negative error (the stream is then over). */
int  ovlstore_sd_stream_begin(ovlstore_sd_t *s, ovlstore_sd_which_t which);
int  ovlstore_sd_stream_next(ovlstore_sd_t *s, uint32_t max_bytes,
                             const uint8_t **data, uint32_t *len);
void ovlstore_sd_stream_abort(ovlstore_sd_t *s);

/* ---- commit (D1 = re-push) ---------------------------------------------------
 * begin: refused unless the state is EMPTY, VALID, BAD or STALE (a stale card
 * MAY be committed over: that is how a re-keyed board recovers), no job runs,
 * desc->static_id == live_static_id == the configured live static_id, and
 * desc->rm_id == live_rm_id (only what is running can be persisted). The
 * target is never the slot holding the verified default.
 * feed: the clearing, then the partial, in order, any chunking. Takes what fits
 * (*consumed); OVLSD_BUSY with *consumed 0 = a write is in flight, poll and
 * retry (back-pressure). A failed commit reports its error here.
 * end: all bytes fed. Flushes, READS BACK both regions and checks their CRCs,
 * then writes the older header copy with the new slot active. Completion via
 * ovlstore_sd_job_result() (OVLSD_OK -> ovlstore_sd_commit_slot()).
 * abort: stop; the header is never touched, so the old default survives.
 * Any failure re-probes the card (the old default is re-verified). */
int  ovlstore_sd_commit_begin(ovlstore_sd_t *s, const ovlstore_sd_desc_t *desc,
                              uint32_t live_static_id, uint32_t live_rm_id);
int  ovlstore_sd_commit_feed(ovlstore_sd_t *s, ovlstore_sd_which_t which,
                             const void *data, uint32_t len, uint32_t *consumed);
int  ovlstore_sd_commit_end(ovlstore_sd_t *s);
void ovlstore_sd_commit_abort(ovlstore_sd_t *s);
char ovlstore_sd_commit_slot(const ovlstore_sd_t *s);   /* 'A'/'B' after a good commit, else 0 */

/* ---- maintenance ---------------------------------------------------------------
 * clear: invalidate the default with one header write (the next boot is
 * greybox). States EMPTY, VALID, BAD, STALE.
 *
 * format(confirm = "erase"): rules (handover §12.3), checked against the
 * probe's reading of the card AND again against a fresh read before any write:
 *   (a) a 0xDA entry exists -> (re)initialise both header copies inside it only;
 *   (b) TRULY blank (see ovlsd_mbr_kind_t: LBA 0 all zero or a signed MBR with
 *       four zero entries, and no filesystem/GPT signature) -> write an MBR
 *       with a single 0xDA entry in entry 4 covering the last 32 MiB (1 MiB-
 *       aligned start), then the headers;
 *   (c) other partitions and no 0xDA -> OVLSD_EEXIST, zero writes.
 * A signature -> OVLSD_EFSSIG; anything else unrecognised -> OVLSD_EUNKNOWN;
 * both with zero writes.
 *
 * format(confirm = "erase-all"): THE EXPLICIT WIPE, for a card nothing else
 * can reach (Ethernet-only users; the user microSD is not on USB). On ANY
 * card, FOREIGN and harness cards included: zero LBA 1..OVLSD_WIPE_LAST_LBA,
 * write a fresh MBR (no boot code, disk signature "MPS3") whose ONLY entry is
 * 0xDA in entry 4 over the last 32 MiB, then the headers. Everything the old
 * partition table described is gone. Never reached from "erase" or any other
 * path. Refused unless cfg.allow_wipe (OVLSD_ENOWIPE), in raw-partition mode
 * (OVLSD_EARG: the device IS p4), and on a card under 33 MiB (OVLSD_ESMALL).
 *
 * Any other confirm string -> OVLSD_ECONFIRM. Completion via
 * ovlstore_sd_job_result(). */
int  ovlstore_sd_clear(ovlstore_sd_t *s);
int  ovlstore_sd_format(ovlstore_sd_t *s, const char *confirm);

#ifdef OVLSTORE_SD_TEST_HOOKS
/* Host tests only (firmware/test/Makefile's test_ovlstore_sd* define it): the
 * write guard, directly. */
bool ovlstore_sd_test_write_allowed(const ovlstore_sd_t *s, uint32_t lba, uint32_t n);
#endif

#ifdef __cplusplus
}
#endif

#endif /* MPS3_OVLSTORE_SD_H */
