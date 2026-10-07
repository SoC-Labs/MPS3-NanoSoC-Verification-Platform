/*
 * slot_card.h — the ONE card layer for stage0's user-microSD layout
 * (docs/planning/linux_lanes/STAGE0_CONTRACT.md §5–§6), linked into BOTH writers
 * of that card:
 *
 *   mps3-harnessd   slot_linux.c   the `slot` verb + the 6910 slot-image push
 *   mps3-slot       br2_external/package/mps3-slot/src/mps3-slot.c   the admin CLI
 *
 * Two copies of these rules had already drifted once (2026-09-24: one read its
 * verify back through the page cache, and one overwrote stage0's boot-select copy
 * on a seq tie). So every rule that decides what stage0 will do, or what reaches
 * the card, lives HERE and nowhere else:
 *
 *   - the card as stage0 reads it: MBR slots, both boot-select copies, the copy
 *     stage0 USES (s0_mbr_parse / s0_bootcfg_pick from stage0_core.c, linked in);
 *   - the boot-select WRITER: never the copy stage0 uses; seq + 1; read back off
 *     the card and held to stage0's own pick;
 *   - the WRITE GUARD every card write goes through;
 *   - the OVERLAP GUARD: a slot that shares a block with the 0xDA store, the
 *     other slot, any other typed entry or LBA 0-2 is BAD and never written;
 *   - flush + uncache, so a read-back comes off the card, not the page cache;
 *   - the image check: stage0's own loader (s0_load) over memory or a card slot.
 *
 * Pure policy + I/O: no logging, no process state. Callers print. Portable C99 +
 * POSIX (the CLI builds with -std=c99, harnessd with gnu11), both with -Werror.
 *
 * TEST SEAM (br2_external/tests/run.sh): the four calls that touch the card are
 * macros, so a caching mock device can stand in for the card:
 *   -DSLOT_PREAD=mock_pread -DSLOT_PWRITE=mock_pwrite -DSLOT_FSYNC=mock_fsync
 *   -DSLOT_FADVISE=mock_posix_fadvise -include mock_blockdev.h
 * and -DMPS3_SLOT_NO_UNCACHE builds the pre-2026-09-24 behaviour (the read-back
 * served from the cache) as the negative control.
 */
#ifndef MPS3_SLOT_CARD_H
#define MPS3_SLOT_CARD_H

#include <stddef.h>
#include <stdint.h>

#include "stage0_boot.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SLOT_CARD_BLK       S0_BLOCK_BYTES
#define SLOT_CARD_DDR_BASE  0x80000000u   /* stage0's destination window (STAGE0 §5) */
#define SLOT_CARD_DDR_SIZE  0x30000000u   /* [0x8000_0000, 0xB000_0000)                */

/* ---- card I/O ---------------------------------------------------------------- */
int      slot_card_rd(int fd, uint64_t off, void *buf, size_t n);          /* 0 / -1 */
/* THE WRITE GUARD: [lo, hi) is the only byte range this write may touch. Outside
 * it: -2 (errno EPERM) and nothing is issued. An I/O error: -1. */
int      slot_card_wr(int fd, uint64_t off, const void *buf, size_t n, uint64_t lo, uint64_t hi);
/* Flush what this fd wrote, then drop the (now clean) cached pages of the range
 * -- BLKFLSBUF on a block device, POSIX_FADV_DONTNEED everywhere -- so the next
 * read of [off, off+len) comes OFF THE CARD. 0 / -1 (the flush failed). */
int      slot_card_flush_uncache(int fd, uint64_t off, uint64_t len);
uint64_t slot_card_size(int fd);                                           /* 0 = unknown */

/* ---- the card as stage0 sees it -------------------------------------------------- */
/* THE OVERLAP GUARD (lane HARDEN, 2026-09-24). Stage0 accepts any in-bounds 0x7F
 * entry as a slot, and every slot write is bounded to that slot's own LBA range --
 * so a malformed MBR whose slot overlaps D13's 0xDA store, the other slot, any
 * other typed entry, or LBA 0-2 (the MBR and the boot-select copies) would turn a
 * correctly-bounded write into a write over someone else's data. Such a slot is
 * BAD: slot_card_read() names it in `bad[i]` and leaves `slot[i]` ZEROED, so every
 * writer that sizes its write-guard range from `slot[i]` (slot_linux.c and
 * mps3-slot alike) gets an empty range and writes nothing; `mbr_slot[i]` keeps
 * what stage0 itself will read. D13's classifier applies the same rule to the 0xDA
 * entry (ovlstore_sd.c, OVLSD_MBR_DA_BAD), so neither writer ever trusts a layout
 * the other would write into. The boot-select writer is refused likewise when any
 * typed entry covers LBA 1-2 (`bootsel_bad`). */
#define SLOT_CARD_FIRST_LBA 3u            /* LBA 0 MBR, 1-2 boot-select: no slot below */
#define SLOT_CARD_WHY_MAX   24            /* == mps3_slot_info_t.err                    */

typedef struct {
    struct s0_slot slot[S0_NUM_SLOTS];   /* MBR entries 1/2 a writer may use (first_lba
                                          * 0 = absent, or BAD: see bad[])              */
    struct s0_slot mbr_slot[S0_NUM_SLOTS]; /* the same entries exactly as stage0 reads them */
    char     bad[S0_NUM_SLOTS][SLOT_CARD_WHY_MAX];   /* "" or why the slot is BAD       */
    char     bootsel_bad[40];             /* "" or why LBA 1-2 may not be written      */
    uint8_t  bc[2][SLOT_CARD_BLK];        /* the boot-select copies, LBA 1 and 2       */
    uint32_t deflt, seq;                  /* s0_bootcfg_pick(): stage0's default + seq */
    uint32_t seqs[2];                     /* each copy's seq (0 when invalid)          */
    int      valid[2];
    int      winner;                      /* the copy stage0 USES: 0/1, -1 = neither   */
    uint64_t bytes;                       /* card size                                  */
} slot_card_t;

/* 0 ok; -1 I/O (or the winner rule disagrees with s0_bootcfg_pick); -2 no MBR
 * signature. On -2 the struct is still filled: both slots absent, the boot-select
 * pick as stage0 would make it -- a blank or foreign card, never an error to show. */
int slot_card_read(int fd, slot_card_t *c);

static inline uint64_t slot_card_off(const slot_card_t *c, int i)
{
    return (uint64_t)c->slot[i].first_lba * SLOT_CARD_BLK;
}
static inline uint64_t slot_card_bytes(const slot_card_t *c, int i)
{
    return (uint64_t)c->slot[i].nblocks * SLOT_CARD_BLK;
}

/* THE BOOT-SELECT WRITER (STAGE0 §6). Rewrites the copy stage0 does NOT use (the
 * lower seq; on a tie the one it did not pick; neither valid -> LBA 1) with
 * default = slot `idx` (0 = A, 1 = B) and seq = c->seq + 1, flushes, drops the
 * cache, re-reads BOTH copies off the card and requires stage0's pick to be the
 * new one IN THE COPY THAT WAS WRITTEN. A torn write leaves the old pick valid.
 * `c` must be a fresh slot_card_read() of this fd; on success it is refreshed.
 * Returns 0; -1 the write/flush failed (errno); -2 refused, nothing written (errno
 * EPERM): a typed MBR entry covers LBA 1-2 (c->bootsel_bad says which), or slot
 * `idx` is BAD (c->bad[idx]) -- the overlap guard; -3 the
 * card did not keep it (*got_deflt / *got_seq say what it holds now). *lba = the
 * sector written (or that would have been). */
int slot_card_bootsel_write(int fd, slot_card_t *c, int idx, unsigned *lba,
                            uint32_t *got_deflt, uint32_t *got_seq);

/* ---- stage0's loader as the image checker ------------------------------------------ */
typedef struct {
    int            fd;      /* the card (when mem is NULL)                      */
    uint64_t       base;    /* the slot's first byte on the card                */
    uint64_t       size;    /* reads past this fail: stage0's truncation rule   */
    const uint8_t *mem;     /* or: an image in memory                           */
} slot_card_src_t;

/* s0_load() over `src`, every region into a scratch buffer bounded by the DDR
 * window. NULL + *hdr_crc (the table CRC) when stage0 would boot it; else stage0's
 * reason (s0_strerror). Does NOT drop the cache: call slot_card_flush_uncache()
 * first when the answer must come off the card. */
const char *slot_card_s0_check(const slot_card_src_t *src, uint32_t *hdr_crc);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_SLOT_CARD_H */
