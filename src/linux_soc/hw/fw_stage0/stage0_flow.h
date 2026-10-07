/*
 * stage0_flow.h -- the stage0 boot ORDER, portable and host-tested:
 *
 *     1. DDR4 calibrated?       no  -> reason "ddr calib fail", rescue
 *     2. user uSD up + MBR      no  -> reason no card / card error / ..., rescue
 *     3. the DEFAULT slot       (boot-select sector; A when none) CRC ok -> hand off
 *     4. the other slot         CRC ok -> hand off (n_fallback counts it)
 *     5. otherwise              -> rescue (TFTP, stage0_tftp.h)
 *
 * (docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md §3 + §10a S5.) A slot that
 * has been handed off `boot_limit` times in a row without Linux confirming it
 * (TRY-ONCE-THEN-CONFIRM, stage0_status.h) is skipped as if it were bad.
 * Every decision and its reason lands in the status block. The card is only
 * ever READ.
 *
 * The platform is an ops table so the SAME decision code runs on the MBV
 * (stage0.c: TELEM calib bit, usd.c over usd_spi, identity DDR) and on the
 * host (test/: an in-memory card, D13's fake_usd, a simulated DDR).
 *
 * COLD-BOOT HARDENING (lane S0-COLDFIX, 2026-09-28). On an MCC power-up the
 * FPGA's stage0 starts at "configuration complete" while the MCC is still
 * reprogramming OSCCLK0-5 (OSCCLK1 = the CPU/AXI clock), GTXCLK and the
 * SMSC9220 for seconds; CB_nRST is not in the shell's reset tree. A clock or
 * lock event in that window stalled the first DDR write forever (the board
 * hung silently: the watchdog was armed only at hand-off), or failed
 * calibration outright. So, in this order:
 *   0. S0_WDOG_LOOP_LIMIT watchdog restarts in a row BEFORE a hand-off
 *      (stage0 itself stalled twice): rescue "stage0 watchdog loop", with no
 *      card access (the DDR gate still runs, so a push can be staged);
 *   0b. a COLD entry (the block was just initialised, or a previous run was
 *      reset before its settle finished) waits S0_COLD_SETTLE_MS before
 *      touching the card or DDR (ops->settle); warm entries do not;
 *   1. the DDR gate requires the calib bit to HOLD 1 (stage0_hw.h
 *      S0_CALIB_HOLD_MS), not the first 1;
 *   -  before every card read op and every in-DDR CRC chunk, ops->guard
 *      re-checks the bit (and the target kicks the watchdog there): a drop
 *      fails the load S0_EDDR; if the gate then holds again the slots are
 *      retried S0_DDR_RETRIES times, else rescue "ddr calib fail" with
 *      ddr_calib = S0_DDR_LOST;
 *   -  a slot load longer than S0_SLOT_TIME_MS fails S0_ESLOW (F2): the other
 *      slot, then rescue.
 * The watchdog itself is armed at stage0 ENTRY by stage0.c (F1), so a stall
 * the guard cannot see (the hart frozen on a DDR transaction) resets the
 * board, and step 0 stops that from looping.
 */
#ifndef STAGE0_FLOW_H
#define STAGE0_FLOW_H

#include <stdint.h>
#include "stage0_boot.h"
#include "stage0_status.h"

struct s0_flow_ops {
    /* S0_DDR_OK / S0_DDR_FAIL / S0_DDR_IMPLIED. May block (bounded). */
    int   (*ddr_calib)(void *ctx);
    /* Bring the card up. Returns S0_SD_READY with *card_blocks set, or another
     * S0_SD_* code. *detail = (usd_state << 16) | usd_error_code. Bounded. */
    int   (*sd_init)(void *ctx, uint32_t *card_blocks, uint32_t *detail);
    /* Read n 512-byte blocks at lba into dst (any alignment). 0 / <0.
     * There is deliberately no write operation: stage0 is read-only. */
    int   (*sd_read_blocks)(void *ctx, uint32_t lba, uint32_t n, void *dst);
    /* DDR destination bound + mapping; NULL rejects the region. */
    void *(*addr_to_ptr)(uint32_t dst_addr, uint32_t len, void *ctx);
    /* One console line (no trailing newline). May be NULL. */
    void  (*log)(void *ctx, const char *line);
    void   *ctx;
    /* ---- cold-boot hardening (S0-COLDFIX); each OPTIONAL: NULL = off ---- */
    /* Block for `ms`, polling (watchdog kick, heartbeat, console progress). */
    void     (*settle)(void *ctx, uint32_t ms);
    /* Before every card read op and every in-DDR CRC chunk: 1 = the DDR calib
     * bit still reads 1, 0 = it dropped (the load fails S0_EDDR). */
    int      (*guard)(void *ctx);
    /* A free-running ms clock: the slot time bound (S0_SLOT_TIME_MS) and
     * ddr_ok_ms. */
    uint32_t (*now_ms)(void *ctx);
};

/* ---- cold-boot hardening knobs (-D overrides; Makefile COLD_SETTLE_MS) ------ */
/* The wait on a cold entry before the first DDR or card access. The MCC's
 * post-configuration steps (OSCCLK0-5, GTXCLK, SMSC9220 probe, CB_nRST) took
 * seconds in docs/evidence/2026-09-w3/w1_field_remote_20260924.txt. */
#ifndef S0_COLD_SETTLE_MS
#define S0_COLD_SETTLE_MS  10000u
#endif
/* Pre-hand-off watchdog restarts in a row that send stage0 straight to rescue. */
#ifndef S0_WDOG_LOOP_LIMIT
#define S0_WDOG_LOOP_LIMIT 2u
#endif
/* Slot retries after a calib drop mid-load, when the gate then holds again. */
#ifndef S0_DDR_RETRIES
#define S0_DDR_RETRIES     1u
#endif
/* DDR recoveries (rescue -> the normal path, when calib comes good) per run. */
#ifndef S0_DDR_RECOVER_MAX
#define S0_DDR_RECOVER_MAX 3u
#endif
/* F2: the whole of one slot's load, header to last CRC. A 22.4 MB image is
 * ~60 s of wire time at the 3.125 MHz data SCK; 64 MiB (S0_IMAGE_MAX) ~175 s. */
#ifndef S0_SLOT_TIME_MS
#define S0_SLOT_TIME_MS    300000u
#endif
/* Card reads are issued (and guarded) this many blocks at a time: one usd.c
 * op (USD_MAX_BLOCKS_PER_OP; stage0_sd.c asserts they agree). */
#define S0_RD_CHUNK_BLOCKS 8u

/* Identity of this stage0 build, stamped into the block on every entry. */
struct s0_build_ids {
    uint32_t build_id;          /* stage0 git sha8                          */
    uint32_t fabric_static_id;  /* MPS3_STATIC_ID (plan §10a S1)            */
    uint32_t fabric_ver32;      /* MPS3_VER32                               */
};

/* Default N for try-once-then-confirm: a slot gets two unconfirmed attempts
 * (the first boot plus one retry, which absorbs a single spurious reset)
 * before stage0 moves on. Override with -DS0_BOOT_LIMIT=n (Makefile
 * BOOT_LIMIT=n); 0 disables limiting (the counters are still kept). */
#ifndef S0_BOOT_LIMIT
#define S0_BOOT_LIMIT 2u
#endif

/* Open the status block for this run: initialise it if it is not valid
 * (reconfiguration), otherwise JUDGE THE PREVIOUS ATTEMPT -- confirmed by
 * Linux, or still pending after a warm restart (= one failed attempt for that
 * slot) -- then bump boot_count, stamp the build identity and reset every
 * per-run field. The NOINIT fields are only ever changed here, by the card's
 * boot-select seq changing (s0_boot_select) and by s0_status_note_handoff.
 *
 * It also CLASSIFIES THE ENTRY (field `entry`, S0_EK_*) from the block's
 * validity, reset_cause's WRS bit (0x8) and the previous run's phase, keeps the
 * NOINIT run of pre-hand-off watchdog restarts, and copies the previous run's
 * phase / subphase / heartbeat / uptime into prev_*. The caller must W1C
 * TWCSR0.WRS when the kind is S0_EK_WDOG (s0_hw_wdog_clear_wrs), so the next
 * entry's WRS is about the next reset only. Returns the entry kind. */
uint32_t s0_status_open(struct s0_status *st, const struct s0_build_ids *ids,
                        uint32_t reset_cause, uint32_t boot_limit);

/* Saturating counters packed into `entry` (the target's DDR gate reports the
 * calib drops it saw while waiting for the hold). */
void s0_status_note_calib_drops(struct s0_status *st, uint32_t n);

/* The cold settle's verdict on the calib bit: its drops, and s0_hw_settle's
 * S0_SETTLE_CAL_* bits -> entry [28] / [29]. */
void s0_status_note_settle(struct s0_status *st, uint32_t drops, uint32_t cal);

/* Steps 0-4. Returns S0_FROM_A or S0_FROM_B with *out filled (payload already
 * in DDR and CRC-verified), or S0_FROM_NONE: go to rescue, with
 * st->rescue_reason set. *ddr_ok tells the rescue server whether it may write
 * DDR. May be called again in the same run (rescue saw the calib bit come good
 * after a DDR failure): the per-attempt fields are reset and the recovery is
 * counted in `entry`; the cold settle never runs twice. */
int s0_boot_select(struct s0_status *st, const struct s0_flow_ops *ops,
                   struct s0_result *out, int *ddr_ok);

/* Record a hand-off: counters, entry, timing, and THE ATTEMPT (att_from =
 * from, att_confirm = 0, i.e. pending until Linux confirms). Call right before
 * the jump, for every source including a rescue push. phase = HANDOFF last. */
void s0_status_note_handoff(struct s0_status *st, int from,
                            const struct s0_result *res, uint32_t now_ms);

/* Fixed text for a rescue reason (identify `reason`, console). */
const char *s0_rescue_reason_text(uint32_t rr);

/* Byte-granular reader over one slot (the s0_backend the core drives for a
 * uSD slot), exposed for the host tests. Reads are bounded by the partition
 * AND by S0_IMAGE_MAX. Unaligned heads/tails go through a one-block cache so
 * the header + entry reads cost one card read, not nine. */
struct s0_slot_src {
    const struct s0_flow_ops *ops;
    struct s0_slot            slot;
    struct s0_status         *st;       /* subphase updates; may be NULL */
    uint32_t                  t0_ms;    /* load start (ops->now_ms)      */
};
int  s0_slot_read(uint32_t src_off, void *dst, uint32_t len, void *ctx);
void s0_slot_cache_invalidate(void);

#endif /* STAGE0_FLOW_H */
