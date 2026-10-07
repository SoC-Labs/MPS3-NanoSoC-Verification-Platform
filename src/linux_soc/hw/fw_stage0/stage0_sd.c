/*
 * stage0_sd.c -- the stage0 STORAGE BACKEND: a BLOCKING wrapper over D13's
 * poll-driven user-microSD driver (firmware/usd/usd.c, the usd_spi block at
 * 0x44A4_0000; register contract in usd_regs.h and
 * docs/planning/HANDOVER_USD_OVERLAY_STORE.md §4.1).
 *
 * THIS IS THE USER CARD, never the MCC config card (V2M_MPS3 / sd_install).
 *
 * WHY A WRAPPER AND NOT A SECOND DRIVER. usd.c is written for the shell
 * superloop: every wait is a state revisited on the next usd_poll(), never a
 * loop. Stage0 has nothing else to do while the card comes up, so it simply
 * calls usd_poll() back to back against the free-running timebase until the
 * driver reaches a verdict. The SD command sequence, card-detect gating,
 * removal handling and error codes are D13's, unmodified -- there is one SD
 * driver in the tree, compiled from $(USD_SRC) (stage0 Makefile).
 *
 * D13 RULE 1 CARRIES OVER: with the slot empty, usd.c makes zero DATA writes
 * and never sets CTRL.EN, and this wrapper gives up after S0_SD_CD_GRACE_MS
 * (one CD debounce period plus margin), so an empty slot costs stage0 ~0.1 s
 * and goes straight to rescue. test/test_stage0_sd.c asserts both.
 *
 * Every loop is bounded by the timebase (mps3_sys_now_ms) and calls
 * s0_poll_hook() (watchdog kick + heartbeat on target, nothing on host).
 */
#include <stdint.h>

#include "stage0_usd_cfg.h"     /* FIRST: the usd.h tunables stage0 builds usd.c with */
#include "usd.h"
#include "stage0_sd.h"
#include "stage0_status.h"
#include "stage0_flow.h"        /* S0_RD_CHUNK_BLOCKS: the flow guards one op at a time */

_Static_assert(S0_RD_CHUNK_BLOCKS == USD_MAX_BLOCKS_PER_OP,
               "stage0_flow.c re-checks DDR calib before every usd.c op: keep the chunk = one op");

uint32_t mps3_sys_now_ms(void);   /* common/timebase.h: stage0.c / mock_regs.c */
void     s0_poll_hook(void);      /* stage0.c / the host tests */

/* Card-detect grace: usd_spi debounces CD for >= 10 ms, and on a cold boot the
 * CPU can start before one debounce period has elapsed. 100 ms is 10x. */
#ifndef S0_SD_CD_GRACE_MS
#define S0_SD_CD_GRACE_MS      100u
#endif
/* Whole-init bound: SETTLE 250 ms + ACMD41 (USD_ACMD41_TIMEOUT_MS), and
 * usd.c's own automatic retries (USD_INIT_TRIES x USD_RETRY_MS) for a card that
 * fails the first init. A card still not READY by then is not going to boot us. */
#ifndef S0_SD_INIT_TIMEOUT_MS
#define S0_SD_INIT_TIMEOUT_MS  (USD_SETTLE_MS + USD_INIT_TRIES * (USD_ACMD41_TIMEOUT_MS + USD_RETRY_MS) + 1000u)
#endif
/* One <= 8-block read op. usd.c already fails a missing data token at
 * USD_READ_TIMEOUT_MS per block (and a CMD12 busy at USD_WRITE_TIMEOUT_MS);
 * this only guards against the op never ending. */
#ifndef S0_SD_IO_TIMEOUT_MS
#define S0_SD_IO_TIMEOUT_MS    (USD_MAX_BLOCKS_PER_OP * USD_READ_TIMEOUT_MS + USD_WRITE_TIMEOUT_MS + 1000u)
#endif
/* A failed read op is repeated this many times, each after a FULL
 * re-initialisation of the card (see recover()). */
#ifndef S0_SD_READ_RETRIES
#define S0_SD_READ_RETRIES     3u
#endif
/* read_op() results that are not a usd_io_status() errno */
#define S0_IO_REFUSED   (-250)   /* usd_read_start() refused (not READY / EINVAL) */
#define S0_IO_GUARD     (-251)   /* S0_SD_IO_TIMEOUT_MS: the op never ended */

static uint32_t s_rd_fails;      /* failed read ops this run (retries included) */
static uint32_t s_rd_last;       /* s0_usd_read_diag() of the last failure */

static uint32_t detail_now(void)
{
    return ((uint32_t)usd_state() << 16) | ((uint32_t)usd_error_code() & 0xFFFFu);
}

/* Poll until READY or a verdict. Returns S0_SD_*. usd.c re-enters INIT from
 * ERROR every USD_RETRY_MS while it has tries left; ERROR held for longer than
 * that means it has given up (sticky until re-insert), so stop waiting. */
static int wait_ready(uint32_t t0, uint32_t timeout_ms)
{
    usd_state_t last = usd_state();
    uint32_t t_last = t0;
    for (;;) {
        uint32_t now = mps3_sys_now_ms();
        s0_poll_hook();
        usd_poll(now);
        usd_state_t st = usd_state();
        uint32_t el = now - t0;
        if (st != last) {
            last = st;
            t_last = now;
        }
        if (st == USD_ERROR && now - t_last > USD_RETRY_MS + 500u)
            return S0_SD_ERROR;
        if (st == USD_READY)
            return S0_SD_READY;
        if (st == USD_UNSUPPORTED)
            return S0_SD_UNSUP;
        if (st == USD_ABSENT && usd_error_code() == USD_ERR_NO_BLOCK)
            return S0_SD_NOHW;
        if (st == USD_ABSENT && el >= S0_SD_CD_GRACE_MS)
            return S0_SD_NOCARD;
        if (el >= timeout_ms)
            return st == USD_ERROR ? S0_SD_ERROR : S0_SD_TIMEOUT;
    }
}

int s0_usd_init(uint32_t *card_blocks, uint32_t *detail)
{
    s_rd_fails = 0u;
    s_rd_last = 0u;
    usd_init();
    int r = wait_ready(mps3_sys_now_ms(), S0_SD_INIT_TIMEOUT_MS);
    *card_blocks = (r == S0_SD_READY) ? usd_card_blocks() : 0u;
    *detail = detail_now();
    return r;
}

/* One op: 0, a negative usd_io_status() errno, or S0_IO_REFUSED/S0_IO_GUARD. */
static int read_op(uint32_t lba, uint32_t n, uint8_t *dst)
{
    if (usd_read_start(lba, n, dst) != 0)
        return S0_IO_REFUSED;
    uint32_t t0 = mps3_sys_now_ms();
    while (usd_io_status() == USD_IO_BUSY) {
        uint32_t now = mps3_sys_now_ms();
        s0_poll_hook();
        usd_poll(now);
        if (now - t0 >= S0_SD_IO_TIMEOUT_MS)
            return S0_IO_GUARD;
    }
    int st = usd_io_status();
    return st == USD_IO_DONE ? 0 : (st < 0 ? st : S0_IO_GUARD);
}

static void note_fail(int rc)
{
    s_rd_fails += 1u;
    s_rd_last = ((uint32_t)(-rc) & 0xFFu) << 24 | ((uint32_t)usd_state() & 0xFFu) << 16 |
                ((uint32_t)usd_error_code() & 0xFFFFu);
}

/* After a failed op, the card may be anything but idle: still producing the
 * block (a slow first access after power-up, or after a write it was cut off
 * in), mid-way through sending it, or confused by a corrupted command. usd.c
 * only re-initialises on a TIMEOUT, only USD_INIT_TRIES times per insertion
 * (and the insertion's own init retries count against that), and never on
 * -EIO -- so retrying the op on the same driver state can send a command into
 * a card that is still talking. Instead: start the driver from scratch
 * (usd_init(): pads released, a fresh retry budget), let it SETTLE and
 * re-initialise -- 80 clocks with CS high, then up to USD_CMD0_TRIES CMD0s,
 * which clock out whatever the card still had to say -- and repeat the op. */
static int recover_and_retry(uint32_t lba, uint32_t n, uint8_t *dst, int rc)
{
    for (uint32_t i = 0; i < S0_SD_READ_RETRIES; ++i) {
        note_fail(rc);
        usd_init();
        int r = wait_ready(mps3_sys_now_ms(), S0_SD_INIT_TIMEOUT_MS);
        if (r == S0_SD_NOCARD || r == S0_SD_NOHW)
            return -1;                        /* pulled: no retry can help */
        rc = (r == S0_SD_READY) ? read_op(lba, n, dst) : S0_IO_REFUSED;
        if (rc == 0)
            return 0;
    }
    note_fail(rc);
    return -1;
}

int s0_usd_read_blocks(uint32_t lba, uint32_t n, void *dst)
{
    uint8_t *d = (uint8_t *)dst;
    while (n != 0u) {
        uint32_t k = n < USD_MAX_BLOCKS_PER_OP ? n : USD_MAX_BLOCKS_PER_OP;
        int rc = read_op(lba, k, d);
        if (rc != 0 && recover_and_retry(lba, k, d, rc) != 0)
            return -1;
        lba += k;
        n -= k;
        d += k * 512u;
    }
    return 0;
}

uint32_t s0_usd_read_fails(void) { return s_rd_fails; }
uint32_t s0_usd_read_diag(void)  { return s_rd_last; }
