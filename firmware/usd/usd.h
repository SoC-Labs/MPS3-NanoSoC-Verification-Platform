/*
 * usd.h -- poll-driven SPI-mode driver for the MPS3 USER microSD slot, through
 * the shell's `usd_spi` block (usd_regs.h). Item D13, lane L2
 * (docs/planning/HANDOVER_USD_OVERLAY_STORE.md §4.5 a).
 *
 * THIS IS THE USER CARD. It is not the MCC config card (`V2M_MPS3`, written by
 * sd_install). Nothing here can reach that one.
 *
 * ============================ THE TWO HARD RULES ============================
 * 1. NO CARD, NO CHANGE. With the slot empty, usd_init() + any number of
 *    usd_poll() calls make ZERO writes to the DATA register and never set
 *    CTRL.EN: the pads stay high-Z and the board boots exactly as before. An
 *    empty-slot poll costs one STATUS read and one CTRL read.
 * 2. NO BLOCKING. The shell superloop has no ISRs and no threads. usd_poll()
 *    does a BOUNDED amount of SPI work per call (USD_POLL_BUDGET_* bytes) and
 *    returns; every wait longer than a single shift -- the 250 ms settle, the
 *    ACMD41 power-up (up to 1 s), a read's data token, a write's busy -- is a
 *    STATE that is revisited on the next call, never a loop. The only spin is
 *    on STATUS.BUSY for ONE shift (<= 80 us at 400 kHz, <= 2.6 us at 12.5 MHz),
 *    bounded by USD_BUSY_SPIN_MAX reads. Background: a held touch once pushed
 *    one superloop pass to 150 ms and starved lwIP (memory:
 *    touch-works-and-starves-the-superloop).
 *
 * ================================ STATES ====================================
 *   ABSENT --CD present--> SETTLE (250 ms) --> INIT --> READY
 *                                                  \--> UNSUPPORTED (v1 / SDSC / not CSD v2)
 *                                                  \--> ERROR <code>  (auto-retry: USD_INIT_TRIES
 *                                                                      attempts, USD_RETRY_MS apart,
 *                                                                      then sticky until re-insert)
 *   any state --removal--> ABSENT: EN=0, and an I/O op in flight completes with
 *       -ENODEV. Removal is any of: !CD_PRESENT; ABORT; CD_CHANGED (even with
 *       the card present again: swapped between two polls = a new card, so it
 *       goes ABSENT -> SETTLE and is re-initialised); and, in the STATUS read of
 *       every shift, the UNDEBOUNCED pin (CD_RAW, polarity applied) reading
 *       empty -- so a card pulled mid-transfer fails the op on the next shift
 *       instead of feeding 0xFF "data" for the 10 ms the debouncer lags.
 *       ABSENT -> SETTLE needs CD_PRESENT AND the raw pin.
 *   UNSUPPORTED and ERROR release the pads (EN=0) as well.
 *
 * ================================== I/O ====================================
 * One op in flight, 512-byte blocks, block (LBA) addressing -- the driver only
 * ever reaches READY for a CCS=1 (SDHC/SDXC) card, so the argument of every
 * data command is an LBA. CMD17/CMD24 for one block, CMD18+CMD12 / CMD25+0xFD
 * for several. The data phases use 32-bit (WIDE) shifts. SPI-mode CRC stays
 * off: integrity comes from the overlay store's slot CRC32 (handover §4.5 a).
 *
 *     if (usd_read_start(lba, n, buf) == 0)
 *         ... later, from the superloop ...
 *         int st = usd_io_status();   // USD_IO_BUSY until it is done
 *
 * usd_*_start() does no SPI work itself; usd_poll() runs the op.
 */
#ifndef MPS3_USD_H
#define MPS3_USD_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    USD_ABSENT = 0,
    USD_SETTLE,
    USD_INIT,
    USD_READY,
    USD_UNSUPPORTED,
    USD_ERROR
} usd_state_t;

/* ---------------------------------------------------------------------------
 * usd_error_code(): why the card is in ERROR or UNSUPPORTED (0 otherwise).
 * The ERROR codes 1..15 are what the CLCD shows as "ERR <n>"; keep them stable,
 * a person will read them off the panel and look them up here.
 * ------------------------------------------------------------------------- */
#define USD_ERR_NONE          0
#define USD_ERR_CMD0          1   /* no idle (0x01) answer to CMD0 after USD_CMD0_TRIES */
#define USD_ERR_CMD8          2   /* CMD8: no answer, bad R1, or check pattern not echoed */
#define USD_ERR_ACMD41        3   /* CMD55/ACMD41 answered with an error bit */
#define USD_ERR_ACMD41_TMO    4   /* ACMD41 still idle after USD_ACMD41_TIMEOUT_MS */
#define USD_ERR_CMD58         5   /* CMD58: bad R1, or power-up bit not set */
#define USD_ERR_CMD9          6   /* CMD9: bad R1 or a data-error token */
#define USD_ERR_CSD_TMO       7   /* CMD9: no CSD data token within USD_READ_TIMEOUT_MS */
#define USD_ERR_IO_TMO        8   /* READY card timed out an I/O (token or busy): re-init */
#define USD_ERR_HW_BUSY       9   /* STATUS.BUSY never cleared: a usd_spi fault */
#define USD_ERR_HW_OVR       10   /* STATUS.OVR seen: a DATA write landed while BUSY */
#define USD_ERR_NO_BLOCK     11   /* ID != USD_ID_VALUE: no usd_spi in this fabric.
                                   * State stays ABSENT (text "none") and the page is
                                   * never written; this code says why. */
/* UNSUPPORTED reasons (state USD_UNSUPPORTED, text "unsupported"). */
#define USD_UNSUP_V1         20   /* CMD8 illegal: a v1.x card */
#define USD_UNSUP_VOLTAGE    21   /* CMD8 did not accept 2.7-3.6 V */
#define USD_UNSUP_SDSC       22   /* CMD58 CCS=0: SDSC (<= 2 GB), byte-addressed */
#define USD_UNSUP_CSD        23   /* CSD_STRUCTURE is not v2.0 */

/* ---------------------------------------------------------------------------
 * usd_io_status() values. Negative = the last op failed:
 *   -ENODEV     card removed mid-op (or not READY at start)
 *   -EIO        bad R1, a data-error token, or a rejected data-response
 *   -ETIMEDOUT  no data token / card busy past the deadline (the driver then
 *               re-initialises the card, see USD_ERR_IO_TMO)
 * USD_IO_DONE is also what it returns before any op has been started.
 * ------------------------------------------------------------------------- */
#define USD_IO_DONE  0
#define USD_IO_BUSY  1

#define USD_BLOCK_SIZE          512u
/* Largest nblocks per op. 8 blocks = a 4 KiB caller buffer; RAM is tight. */
#define USD_MAX_BLOCKS_PER_OP   8u

/* ---------------------------------------------------------------------------
 * Tunables. Every one is overridable with -D for a bench build; the defaults
 * are what the README's budget numbers are computed from.
 * ------------------------------------------------------------------------- */

/* PER-CALL SPI BUDGET, in bytes shifted on the wire (a 32-bit shift = 4).
 * usd_poll() never starts a step it could not finish inside what is left of
 * the budget, so the bytes shifted by ONE call never exceed the budget. Two
 * budgets, because a byte costs 31x more wire time at 400 kHz than at 12.5:
 *   FAST (12.5 MHz, READY): 576 B = 512 data + 64 overhead -> 368.6 us of wire time
 *   SLOW (400 kHz, INIT):    32 B = one command exchange   -> 640.0 us of wire time
 * Floor for both: 21 (one command step; usd.c static-asserts it).
 * firmware/usd/README.md has the worst case including CPU overhead. */
#ifndef USD_POLL_BUDGET_FAST
#define USD_POLL_BUDGET_FAST    576u
#endif
#ifndef USD_POLL_BUDGET_SLOW
#define USD_POLL_BUDGET_SLOW    32u
#endif
/* Most 8-bit "wait" shifts (token hunt, busy poll) in one call. An 8-bit shift
 * is mostly CPU overhead at 12.5 MHz, so waiting is capped well below the
 * budget; the wait resumes on the next call. */
#ifndef USD_WAIT_BYTES_PER_POLL
#define USD_WAIT_BYTES_PER_POLL 64u
#endif

/* Reads of STATUS while waiting for ONE shift to finish. The longest shift the
 * driver issues is 32 bits at 400 kHz = 80 us; an AXI-Lite read on the shell is
 * ~0.1-0.2 us, so the real wait is <= ~800 reads. 20000 is 25x that, and a
 * stuck BUSY costs at most ~2-4 ms ONCE: the driver then goes to ERROR
 * (USD_ERR_HW_BUSY) and stops shifting. */
#ifndef USD_BUSY_SPIN_MAX
#define USD_BUSY_SPIN_MAX       20000u
#endif

#ifndef USD_SETTLE_MS
#define USD_SETTLE_MS           250u   /* after CD goes present, before touching the card */
#endif
#ifndef USD_CMD0_TRIES
#define USD_CMD0_TRIES          4u     /* CMD0 attempts inside one init */
#endif
#ifndef USD_ACMD41_TIMEOUT_MS
#define USD_ACMD41_TIMEOUT_MS   1000u  /* SD spec: power-up within 1 s */
#endif
#ifndef USD_ACMD41_RETRY_MS
#define USD_ACMD41_RETRY_MS     4u     /* re-issue ACMD41 this often while idle */
#endif
#ifndef USD_READ_TIMEOUT_MS
#define USD_READ_TIMEOUT_MS     250u   /* data token. Spec: 100 ms for SDHC; 2.5x margin */
#endif
#ifndef USD_WRITE_TIMEOUT_MS
#define USD_WRITE_TIMEOUT_MS    500u   /* write / stop busy. Spec: 250 ms for SDHC; 2x margin */
#endif
#ifndef USD_INIT_TRIES
#define USD_INIT_TRIES          3u     /* init attempts per insertion (incl. the first) */
#endif
#ifndef USD_RETRY_MS
#define USD_RETRY_MS            2000u  /* ERROR -> INIT auto-retry spacing */
#endif
/* CLKDIV once READY (usd_regs.h: SCK = aclk / (2*(DIV+1))). Default 12.5 MHz.
 * A larger DIV is a build-time, no-mint mitigation for a marginal slot (stage0
 * builds with its own, stage0_usd_cfg.h). DIV 0 is outside SD SPI timing. */
#ifndef USD_CLKDIV_DATA
#define USD_CLKDIV_DATA         USD_CLKDIV_12M5
#endif
/* Bytes of 0xFF the R1 hunt accepts after a command (NCR). The SD spec's
 * maximum is 8; Linux mmc_spi accepts 16 ("in practice, some SD cards are
 * slow"). Enters CMD_STEP_BYTES, so the budget asserts in usd.c cover it. */
#ifndef USD_NCR_MAX
#define USD_NCR_MAX             8u
#endif
/* Card-detect config bits OR-ed into CTRL at usd_init() (USD_CTRL_CD_POL /
 * USD_CTRL_CD_IGNORE). Default 0 = the assumed NCD-low-is-present, CD wired.
 * Board step B0 can instead poke CTRL over JTAG; the driver preserves it. */
#ifndef USD_CTRL_CD_DEFAULT
#define USD_CTRL_CD_DEFAULT     0u
#endif

/* ---------------------------------------------------------------------------
 * API
 * ------------------------------------------------------------------------- */

/* Once at boot. Never blocks. Probes ID, clears stale sticky STATUS bits and
 * writes CTRL with EN=0. With no usd_spi block (ID mismatch) it writes NOTHING.
 * With no card it makes ZERO DATA-register writes (rule 1). Re-callable: it
 * resets all driver state (host tests rely on that). */
void        usd_init(void);

/* The superloop hook. Bounded work per call (see the budgets). now_ms is the
 * caller's monotonic millisecond clock (mps3_sys_now_ms() on target); all
 * comparisons are wrap-safe. */
void        usd_poll(uint32_t now_ms);

usd_state_t usd_state(void);
int         usd_error_code(void);        /* USD_ERR_* / USD_UNSUP_*, 0 = none */
bool        usd_present(void);           /* the driver's view: state != ABSENT */
uint32_t    usd_card_mb(void);           /* MiB, from the CSD; 0 unless READY */
uint32_t    usd_card_blocks(void);       /* 512-byte blocks; 0 unless READY (additive) */
uint32_t    usd_change_count(void);      /* +1 on every insert and every removal */
const char *usd_state_text(void);        /* <= 16 chars: "none","init","ready",
                                          * "unsupported","ERR <n>" */

/* Async block I/O. Returns 0 (started), -ENODEV (not READY), -EBUSY (an op is
 * in flight), -EINVAL (NULL buf, nblocks 0 or > USD_MAX_BLOCKS_PER_OP, or the
 * range runs past the card). buf must stay valid until usd_io_status() is no
 * longer USD_IO_BUSY; no alignment requirement. */
int usd_read_start(uint32_t lba, uint32_t nblocks, void *buf);
int usd_write_start(uint32_t lba, uint32_t nblocks, const void *buf);
int usd_io_status(void);                 /* USD_IO_BUSY, USD_IO_DONE, or -errno */

#ifdef __cplusplus
}
#endif

#endif /* MPS3_USD_H */
