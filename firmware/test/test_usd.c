/*
 * test_usd.c -- host-gcc tests for the user-microSD driver (firmware/usd/usd.c)
 * against fake_usd.c (a register-level usd_spi + an SD card in SPI mode) over
 * mock_regs. Built -DMPS3_HAL_MOCK, linking the REAL usd.c.
 *
 * Every usd_poll() goes through poll_once(), which asserts on EVERY call:
 *   - bytes shifted <= USD_POLL_BUDGET_FAST, and bytes at 400 kHz <= _SLOW;
 *   - wire time <= 1 ms;
 *   - strlen(usd_state_text()) <= 16.
 * So the budget and the text width are checked across every state every case
 * reaches, not only in the cases named after them.
 *
 * Cases:
 *   1  no card: thousands of polls, ZERO DATA writes, EN never set (david's rule)
 *   1b no usd_spi block (ID mismatch): the page is never written at all
 *   2  insertion -> SETTLE (no shifts) -> READY within a bounded number of polls
 *   2b board step B0: CD_POL set over JTAG is honoured and preserved
 *   3  SDSC -> UNSUPPORTED, v1 -> UNSUPPORTED, slow ACMD41 -> READY,
 *      ACMD41 never ready -> ERROR 4
 *   3b dead card -> ERROR 1, exactly USD_INIT_TRIES attempts USD_RETRY_MS apart,
 *      then sticky; re-insert starts over
 *   3c stuck BUSY -> ERROR 9, bounded spin, and no DATA write while BUSY
 *   4  single/multi-block read and write, data integrity, argument checks
 *   5  removal mid-read / mid-write data / mid-write busy / mid-init, with 0 ms
 *      and 10 ms debounce -> negative io status + ABSENT; re-insert -> READY
 *   5b a READY card that stops answering -> -ETIMEDOUT, ERROR 8, re-init
 *   6  per-poll budget: reports the measured maxima
 *   7  state text for every state
 *
 * The same file builds twice (firmware/usd/test.mk): once with the default
 * budgets and once with tiny ones, which forces every data phase and wait to
 * be split across many polls and so exercises every resume path.
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../usd/usd.h"
#include "../usd/usd_regs.h"
#include "mock_regs.h"
#include "fake_usd.h"

static unsigned s_checks;
#define CHECK(cond) do {                                                        \
        s_checks++;                                                             \
        if (!(cond)) {                                                          \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
            exit(1);                                                            \
        }                                                                       \
    } while (0)

#define POLL_MS          1u          /* fake superloop period */
#define WIRE_CAP_NS      1000000ull  /* 1 ms of SPI wire time per usd_poll(), absolute */
#define CARD_BLOCKS      ((FAKE_USD_C_SIZE_DEFAULT + 1u) << 10)
#define CARD_MB          ((FAKE_USD_C_SIZE_DEFAULT + 1u) >> 1)
/* Write busy long enough to span ~30 polls whatever the wait allowance is. */
#define WRITE_BUSY_LONG  (USD_WAIT_BYTES_PER_POLL * 30u)
#define IO_POLL_LIMIT    20000u
#define BUDGET_MIN_STEP  21u         /* usd.c CMD_STEP_BYTES */
/* Shifts (DATA writes) per poll: at most the 8-bit wait allowance, plus a full
 * budget of 32-bit words, plus two command steps' worth of 8-bit shifts. What
 * this bounds is CPU time: an 8-bit shift at 12.5 MHz is mostly overhead. */
#define SHIFT_CAP        (USD_WAIT_BYTES_PER_POLL + USD_POLL_BUDGET_FAST / 4u + 2u * BUDGET_MIN_STEP)

/* Measured maxima over the whole run. */
static uint32_t g_max_bytes_fast, g_max_bytes_slow, g_max_shifts;
static uint64_t g_max_wire_ns;
static uint32_t g_polls;

static void setup(void)
{
    mock_regs_reset();
    fake_usd_reset();
    usd_init();
}

static void poll_once(void)
{
    uint32_t b, bs;
    uint64_t ns;

    mock_time_advance_ms(POLL_MS);
    fake_usd_poll_begin();
    usd_poll(mock_time_now_ms());
    g_polls++;

    b  = fake_usd_poll_bytes();
    bs = fake_usd_poll_bytes_slow();
    ns = fake_usd_poll_wire_ns();
    CHECK(b <= USD_POLL_BUDGET_FAST);
    CHECK(bs <= USD_POLL_BUDGET_SLOW);
    CHECK(ns <= WIRE_CAP_NS);
    CHECK(fake_usd_poll_shifts() <= SHIFT_CAP);
    CHECK(strlen(usd_state_text()) <= 16u);
    if (bs > g_max_bytes_slow) {
        g_max_bytes_slow = bs;
    }
    if (b - bs > g_max_bytes_fast) {
        g_max_bytes_fast = b - bs;
    }
    if (fake_usd_poll_shifts() > g_max_shifts) {
        g_max_shifts = fake_usd_poll_shifts();
    }
    if (ns > g_max_wire_ns) {
        g_max_wire_ns = ns;
    }
}

static void poll_n(uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        poll_once();
    }
}

/* Poll until the state is `want`; returns the polls it took. Fails the test
 * if it is not reached within `limit`. */
static uint32_t poll_until_state(usd_state_t want, uint32_t limit)
{
    for (uint32_t i = 1; i <= limit; i++) {
        poll_once();
        if (usd_state() == want) {
            return i;
        }
    }
    fprintf(stderr, "state %d not reached in %u polls (state %d, text %s, err %d)\n",
            (int)want, (unsigned)limit, (int)usd_state(), usd_state_text(), usd_error_code());
    CHECK(0);
    return 0;
}

static int poll_io(uint32_t limit)
{
    for (uint32_t i = 0; i < limit && usd_io_status() == USD_IO_BUSY; i++) {
        poll_once();
    }
    return usd_io_status();
}

/* Faults that no case provokes on purpose. */
static void check_clean(void)
{
    CHECK(fake_usd_ovr_count() == 0u);
    CHECK(fake_usd_crc_errors() == 0u);
    CHECK(fake_usd_crc_bad_any() == 0u);                  /* usd.c CRCs every command */
    CHECK(fake_usd_frame_errors() == 0u);
    CHECK(fake_usd_busy_violations() == 0u);
    CHECK(fake_usd_protocol_errors() == 0u);
    CHECK(fake_usd_store_overflows() == 0u);
}

static void ready_card(void)
{
    setup();
    fake_usd_insert();
    poll_until_state(USD_READY, USD_SETTLE_MS + 400u);
}

static void check_pattern(const uint8_t *buf, uint32_t lba, uint32_t n)
{
    for (uint32_t b = 0; b < n; b++) {
        for (uint32_t i = 0; i < USD_BLOCK_SIZE; i++) {
            CHECK(buf[b * USD_BLOCK_SIZE + i] == fake_usd_pattern_byte(lba + b, i));
        }
    }
}

static uint32_t s_lcg = 12345u;
static void fill_random(uint8_t *buf, uint32_t len)
{
    for (uint32_t i = 0; i < len; i++) {
        s_lcg = s_lcg * 1103515245u + 12345u;
        buf[i] = (uint8_t)(s_lcg >> 16);
    }
}

/* ======================================================================== */

/* 1: no card. */
static void test_no_card(void)
{
    uint32_t w0;
    uint8_t  buf[USD_BLOCK_SIZE];

    setup();
    w0 = fake_usd_page_writes();
    CHECK(w0 <= 1u);                               /* at most usd_init's CTRL write */
    CHECK((fake_usd_ctrl() & USD_CTRL_EN) == 0u);

    poll_n(5000u);
    CHECK(fake_usd_data_writes() == 0u);           /* THE requirement */
    CHECK(fake_usd_en_writes() == 0u);
    CHECK(!fake_usd_pads_ever_driven());
    CHECK(fake_usd_page_writes() == w0);           /* polling an empty slot writes nothing */
    CHECK(usd_state() == USD_ABSENT);
    CHECK(!usd_present());
    CHECK(usd_change_count() == 0u);
    CHECK(usd_card_mb() == 0u);
    CHECK(usd_error_code() == USD_ERR_NONE);
    CHECK(strcmp(usd_state_text(), "none") == 0);
    CHECK(usd_read_start(0u, 1u, buf) == -ENODEV);
    CHECK(usd_write_start(0u, 1u, buf) == -ENODEV);
    CHECK(usd_io_status() == USD_IO_DONE);

    /* Same with a realistic debouncer. */
    setup();
    fake_usd_set_debounce_ms(10u);
    poll_n(3000u);
    CHECK(fake_usd_data_writes() == 0u);
    CHECK(fake_usd_en_writes() == 0u);
    CHECK(!fake_usd_pads_ever_driven());
    printf("PASS 1  no card: 8000 polls, 0 DATA writes, EN never set, %u page write(s) (usd_init CTRL)\n",
           (unsigned)w0);
}

/* 1b: no usd_spi in this fabric. */
static void test_no_block(void)
{
    mock_regs_reset();
    fake_usd_reset();
    fake_usd_set_id(0u);
    usd_init();
    fake_usd_insert();
    poll_n(2000u);
    CHECK(fake_usd_page_writes() == 0u);
    CHECK(fake_usd_data_writes() == 0u);
    CHECK(usd_state() == USD_ABSENT);
    CHECK(usd_error_code() == USD_ERR_NO_BLOCK);
    CHECK(strcmp(usd_state_text(), "none") == 0);
    printf("PASS 1b no block (ID mismatch): 0 writes to the page, state none, err %d\n",
           USD_ERR_NO_BLOCK);
}

/* 2: insertion -> READY. */
static void test_insert_ready(void)
{
    uint32_t n;
    const uint8_t *f0, *f8;

    setup();
    fake_usd_set_debounce_ms(10u);
    poll_n(100u);
    fake_usd_insert();

    /* Debounce (10 ms) + settle (250 ms): the card is seen but not touched. */
    poll_n(10u + USD_SETTLE_MS - 5u);
    CHECK(usd_state() == USD_SETTLE);
    CHECK(usd_present());
    CHECK(strcmp(usd_state_text(), "init") == 0);
    CHECK(usd_change_count() == 1u);
    CHECK(fake_usd_data_writes() == 0u);
    CHECK(fake_usd_en_writes() == 0u);

    n = poll_until_state(USD_READY, 200u);
    n += 10u + USD_SETTLE_MS - 5u;
    CHECK(n <= 10u + USD_SETTLE_MS + 40u);
    CHECK(strcmp(usd_state_text(), "ready") == 0);
    CHECK(usd_card_mb() == CARD_MB);
    CHECK(usd_card_blocks() == CARD_BLOCKS);
    CHECK(usd_error_code() == USD_ERR_NONE);
    CHECK(usd_change_count() == 1u);

    /* Init protocol. */
    CHECK(fake_usd_clocks_before_cmd0() >= 74u);
    f0 = fake_usd_cmd_frame(0u);
    f8 = fake_usd_cmd_frame(8u);
    CHECK(f0[0] == 0x40u && f0[5] == 0x95u);
    CHECK(f8[0] == 0x48u && f8[3] == 0x01u && f8[4] == 0xAAu && f8[5] == 0x87u);
    CHECK(fake_usd_cmd_count(41u) == 1u);
    CHECK(fake_usd_cmd_clkdiv(0u) == USD_CLKDIV_400K);
    CHECK(fake_usd_cmd_clkdiv(8u) == USD_CLKDIV_400K);
    CHECK(fake_usd_cmd_clkdiv(55u) == USD_CLKDIV_400K);
    CHECK(fake_usd_cmd_clkdiv(41u) == USD_CLKDIV_400K);
    CHECK(fake_usd_cmd_clkdiv(58u) == USD_CLKDIV_400K);
    CHECK(fake_usd_cmd_clkdiv(9u) == USD_CLKDIV_400K);
    CHECK(fake_usd_clkdiv() == USD_CLKDIV_12M5);
    CHECK((fake_usd_ctrl() & (USD_CTRL_EN | USD_CTRL_CS)) == USD_CTRL_EN);
    check_clean();
    printf("PASS 2  insertion -> READY in %u polls (10 debounce + %u settle + init), %u MiB, "
           "%u pre-CMD0 clocks\n", (unsigned)n, (unsigned)USD_SETTLE_MS, (unsigned)usd_card_mb(),
           (unsigned)fake_usd_clocks_before_cmd0());
}

/* 2b: board step B0. NCD turns out to be high-when-present: the driver sees an
 * empty slot (and stays silent) until CD_POL is set over JTAG, then initialises
 * the card, and every CTRL write it makes keeps CD_POL set. */
static void test_cd_pol_b0(void)
{
    mock_regs_reset();
    fake_usd_reset();
    fake_usd_set_pin_inverted(1);
    usd_init();
    fake_usd_insert();
    poll_n(1000u);
    CHECK(usd_state() == USD_ABSENT);
    CHECK(fake_usd_data_writes() == 0u);
    fake_usd_jtag_ctrl_set(USD_CTRL_CD_POL);
    poll_until_state(USD_READY, USD_SETTLE_MS + 400u);
    CHECK((fake_usd_ctrl() & USD_CTRL_CD_POL) != 0u);    /* survived every RMW */
    CHECK(usd_card_mb() == CARD_MB);
    fake_usd_remove();
    poll_once();
    CHECK(usd_state() == USD_ABSENT);                     /* removal still seen */
    CHECK((fake_usd_ctrl() & USD_CTRL_CD_POL) != 0u);
    check_clean();
    printf("PASS 2b B0: inverted NCD -> silent; CD_POL over JTAG -> ready, CD_POL preserved\n");
}

/* 3: unsupported cards and a slow card. */
static void test_card_types(void)
{
    uint32_t dw, t0, t_err;

    /* SDSC */
    setup();
    fake_usd_set_sdsc(1);
    fake_usd_insert();
    poll_until_state(USD_UNSUPPORTED, USD_SETTLE_MS + 400u);
    CHECK(usd_error_code() == USD_UNSUP_SDSC);
    CHECK(strcmp(usd_state_text(), "unsupported") == 0);
    CHECK((fake_usd_ctrl() & USD_CTRL_EN) == 0u);       /* pads released */
    CHECK(usd_card_mb() == 0u);
    dw = fake_usd_data_writes();
    poll_n(3000u);
    CHECK(fake_usd_data_writes() == dw);                  /* final: no more traffic */
    CHECK(usd_state() == USD_UNSUPPORTED);
    check_clean();

    /* v1 card: CMD8 illegal */
    setup();
    fake_usd_set_v1(1);
    fake_usd_insert();
    poll_until_state(USD_UNSUPPORTED, USD_SETTLE_MS + 400u);
    CHECK(usd_error_code() == USD_UNSUP_V1);
    CHECK(fake_usd_cmd_count(41u) == 0u);
    CHECK((fake_usd_ctrl() & USD_CTRL_EN) == 0u);
    check_clean();

    /* Slow ACMD41: 100 idle answers, still inside 1 s. */
    setup();
    fake_usd_set_acmd41_busy(100u);
    fake_usd_insert();
    poll_until_state(USD_READY, USD_SETTLE_MS + USD_ACMD41_TIMEOUT_MS);
    CHECK(fake_usd_cmd_count(41u) == 101u);
    CHECK(usd_card_mb() == CARD_MB);
    check_clean();

    /* ACMD41 never ready: ERROR 4 after ~1 s of retries. */
    setup();
    fake_usd_set_acmd41_busy(1000000u);
    fake_usd_insert();
    poll_until_state(USD_INIT, USD_SETTLE_MS + 10u);
    t0 = mock_time_now_ms();
    poll_until_state(USD_ERROR, USD_ACMD41_TIMEOUT_MS + 100u);
    t_err = mock_time_now_ms() - t0;
    CHECK(usd_error_code() == USD_ERR_ACMD41_TMO);
    CHECK(strcmp(usd_state_text(), "ERR 4") == 0);
    CHECK(t_err >= USD_ACMD41_TIMEOUT_MS && t_err <= USD_ACMD41_TIMEOUT_MS + 50u);
    CHECK(fake_usd_cmd_count(41u) > 100u);
    CHECK((fake_usd_ctrl() & USD_CTRL_EN) == 0u);
    check_clean();
    printf("PASS 3  SDSC -> unsupported, v1 -> unsupported, slow ACMD41 (100) -> ready, "
           "ACMD41 stuck -> ERR 4 after %u ms\n", (unsigned)t_err);
}

/* 3b: a card that never answers: bounded auto-retry, then sticky ERROR. */
static void test_error_retry(void)
{
    uint32_t inits = 0, t_init[8] = { 0 }, dw;
    usd_state_t prev;

    setup();
    fake_usd_set_unresponsive(1);
    fake_usd_insert();
    prev = usd_state();
    for (uint32_t i = 0; i < 20000u; i++) {                /* 20 s */
        poll_once();
        if (usd_state() == USD_INIT && prev != USD_INIT) {
            if (inits < 8u) {
                t_init[inits] = mock_time_now_ms();
            }
            inits++;
        }
        prev = usd_state();
    }
    CHECK(inits == USD_INIT_TRIES);
    for (uint32_t k = 1; k < inits; k++) {
        CHECK(t_init[k] - t_init[k - 1u] >= USD_RETRY_MS);
    }
    CHECK(usd_state() == USD_ERROR);
    CHECK(usd_error_code() == USD_ERR_CMD0);
    CHECK(strcmp(usd_state_text(), "ERR 1") == 0);
    CHECK((fake_usd_ctrl() & USD_CTRL_EN) == 0u);
    CHECK(fake_usd_cmd_count(0u) == USD_INIT_TRIES * USD_CMD0_TRIES);
    dw = fake_usd_data_writes();
    poll_n(5000u);
    CHECK(fake_usd_data_writes() == dw);                  /* sticky: silent */
    CHECK(usd_state() == USD_ERROR);

    /* Re-insert a good card: starts over. */
    fake_usd_remove();
    poll_once();
    CHECK(usd_state() == USD_ABSENT);
    fake_usd_set_unresponsive(0);
    fake_usd_insert();
    poll_until_state(USD_READY, USD_SETTLE_MS + 400u);
    CHECK(usd_change_count() == 3u);
    check_clean();
    printf("PASS 3b dead card: %u init attempts >= %u ms apart, then sticky ERR 1; "
           "re-insert -> ready\n", (unsigned)inits, (unsigned)USD_RETRY_MS);
}

/* 3c: STATUS.BUSY stuck. */
static void test_busy_stuck(void)
{
    uint32_t sr0, dw;

    setup();
    fake_usd_set_busy_reads(-1);
    fake_usd_insert();
    poll_n(USD_SETTLE_MS - 5u);
    CHECK(usd_state() == USD_SETTLE);
    sr0 = fake_usd_status_reads();
    /* The SETTLE->INIT poll's first shift hangs: ERROR in that same poll. */
    poll_until_state(USD_ERROR, 20u);
    sr0 += 20u;                                           /* 1 STATUS read per settle poll */
    CHECK(usd_error_code() == USD_ERR_HW_BUSY);
    CHECK(strcmp(usd_state_text(), "ERR 9") == 0);
    CHECK(fake_usd_status_reads() - sr0 <= USD_BUSY_SPIN_MAX + 4u);
    dw = fake_usd_data_writes();
    CHECK(dw == 1u);                                      /* the one shift that hung */
    poll_n(10000u);                                       /* retries due, BUSY still set */
    CHECK(fake_usd_data_writes() == dw);
    CHECK(fake_usd_ovr_count() == 0u);
    CHECK(usd_state() == USD_ERROR);
    printf("PASS 3c stuck BUSY -> ERR 9 after <= %u STATUS reads; retries skipped, 0 OVR\n",
           (unsigned)USD_BUSY_SPIN_MAX);
}

/* 4: block I/O with data integrity. */
static void test_io(void)
{
    static uint8_t buf[USD_MAX_BLOCKS_PER_OP * USD_BLOCK_SIZE];
    static uint8_t wbuf[USD_MAX_BLOCKS_PER_OP * USD_BLOCK_SIZE];
    uint8_t  peek[USD_BLOCK_SIZE];
    uint32_t dw;

    ready_card();

    /* Single-block read. */
    memset(buf, 0, sizeof buf);
    dw = fake_usd_data_writes();
    CHECK(usd_read_start(1000u, 1u, buf) == 0);
    CHECK(usd_io_status() == USD_IO_BUSY);
    CHECK(usd_read_start(1000u, 1u, buf) == -EBUSY);
    CHECK(usd_write_start(1000u, 1u, buf) == -EBUSY);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    check_pattern(buf, 1000u, 1u);
    CHECK(fake_usd_cmd_count(17u) == 1u);
    CHECK(fake_usd_cmd_clkdiv(17u) == USD_CLKDIV_12M5);
    CHECK(fake_usd_data_writes() - dw < 200u);            /* WIDE data phase: 128 + ~25, not 512+ */

    /* Multi-block read: CMD18 + CMD12. */
    memset(buf, 0, sizeof buf);
    CHECK(usd_read_start(5000u, USD_MAX_BLOCKS_PER_OP, buf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    check_pattern(buf, 5000u, USD_MAX_BLOCKS_PER_OP);
    CHECK(fake_usd_cmd_count(18u) == 1u);
    CHECK(fake_usd_cmd_count(12u) == 1u);

    /* Single-block write with write-busy, read back two ways. */
    fake_usd_set_write_busy(300u);
    fill_random(wbuf, USD_BLOCK_SIZE);
    CHECK(usd_write_start(42u, 1u, wbuf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    CHECK(fake_usd_cmd_count(24u) == 1u);
    fake_usd_peek_block(42u, peek);
    CHECK(memcmp(peek, wbuf, USD_BLOCK_SIZE) == 0);
    memset(buf, 0, sizeof buf);
    CHECK(usd_read_start(42u, 1u, buf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    CHECK(memcmp(buf, wbuf, USD_BLOCK_SIZE) == 0);

    /* Multi-block write: CMD25, 0xFC tokens, 0xFD stop; busy spans many polls. */
    fake_usd_set_write_busy(WRITE_BUSY_LONG);
    fill_random(wbuf, sizeof wbuf);
    CHECK(usd_write_start(7000u, USD_MAX_BLOCKS_PER_OP, wbuf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    CHECK(fake_usd_cmd_count(25u) == 1u);
    CHECK(fake_usd_stop_tokens() == 1u);
    CHECK(fake_usd_blocks_written() == 1u + USD_MAX_BLOCKS_PER_OP);
    for (uint32_t b = 0; b < USD_MAX_BLOCKS_PER_OP; b++) {
        fake_usd_peek_block(7000u + b, peek);
        CHECK(memcmp(peek, wbuf + b * USD_BLOCK_SIZE, USD_BLOCK_SIZE) == 0);
    }
    memset(buf, 0, sizeof buf);
    CHECK(usd_read_start(7000u, USD_MAX_BLOCKS_PER_OP, buf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    CHECK(memcmp(buf, wbuf, sizeof wbuf) == 0);
    /* Neighbours untouched. */
    fake_usd_peek_block(6999u, peek);
    check_pattern(peek, 6999u, 1u);
    fake_usd_peek_block(7000u + USD_MAX_BLOCKS_PER_OP, peek);
    check_pattern(peek, 7000u + USD_MAX_BLOCKS_PER_OP, 1u);

    /* A rejected data response: -EIO, nothing stored, the card stays READY.
     * Single block, then block 3 of a multi-block write (which must still end
     * with the 0xFD stop token and must not send blocks 4..8). */
    {
        uint32_t bw = fake_usd_blocks_written();
        uint32_t stops = fake_usd_stop_tokens();
        fake_usd_set_write_busy(0u);
        fill_random(wbuf, sizeof wbuf);
        fake_usd_reject_write_block(1u);
        CHECK(usd_write_start(900u, 1u, wbuf) == 0);
        CHECK(poll_io(IO_POLL_LIMIT) == -EIO);
        CHECK(fake_usd_blocks_written() == bw);
        CHECK(usd_state() == USD_READY);
        fake_usd_reject_write_block(3u);
        CHECK(usd_write_start(910u, USD_MAX_BLOCKS_PER_OP, wbuf) == 0);
        CHECK(poll_io(IO_POLL_LIMIT) == -EIO);
        CHECK(fake_usd_blocks_written() == bw + 2u);      /* blocks 1-2 only */
        CHECK(fake_usd_stop_tokens() == stops + 1u);
        fake_usd_peek_block(912u, peek);
        check_pattern(peek, 912u, 1u);                    /* block 3 never landed */
        CHECK(usd_state() == USD_READY);
        CHECK(usd_write_start(900u, 1u, wbuf) == 0);      /* and the next op is fine */
        CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    }

    /* Argument checks. */
    CHECK(usd_read_start(0u, 0u, buf) == -EINVAL);
    CHECK(usd_read_start(0u, USD_MAX_BLOCKS_PER_OP + 1u, buf) == -EINVAL);
    CHECK(usd_read_start(0u, 1u, NULL) == -EINVAL);
    CHECK(usd_write_start(0u, 1u, NULL) == -EINVAL);
    CHECK(usd_read_start(CARD_BLOCKS, 1u, buf) == -EINVAL);
    CHECK(usd_read_start(CARD_BLOCKS - 1u, 2u, buf) == -EINVAL);
    CHECK(usd_read_start(0xFFFFFFFFu, 2u, buf) == -EINVAL);
    CHECK(usd_io_status() == USD_IO_DONE);                /* rejected starts change nothing */
    CHECK(usd_read_start(CARD_BLOCKS - 1u, 1u, buf) == 0); /* the last block is legal */
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    check_pattern(buf, CARD_BLOCKS - 1u, 1u);

    CHECK(usd_state() == USD_READY);
    check_clean();
    printf("PASS 4  read 1 + %u blocks, write 1 + %u blocks (busy %u B), read-back identical, "
           "rejected write -> -EIO, -EBUSY / -EINVAL\n", (unsigned)USD_MAX_BLOCKS_PER_OP, (unsigned)USD_MAX_BLOCKS_PER_OP,
           (unsigned)WRITE_BUSY_LONG);
}

/* 5: removal in the middle of things. */
static void removal_case(int write, uint32_t remove_after, uint32_t write_busy, uint32_t debounce_ms,
                         const char *what)
{
    static uint8_t buf[USD_MAX_BLOCKS_PER_OP * USD_BLOCK_SIZE];
    uint32_t dw;
    int      st;

    setup();
    fake_usd_set_debounce_ms(debounce_ms);
    fake_usd_insert();
    poll_until_state(USD_READY, debounce_ms + USD_SETTLE_MS + 400u);
    fake_usd_set_write_busy(write_busy);
    fill_random(buf, sizeof buf);

    CHECK((write ? usd_write_start(300u, 4u, buf) : usd_read_start(300u, 4u, buf)) == 0);
    fake_usd_remove_after_bytes(remove_after);
    st = poll_io(IO_POLL_LIMIT);
    CHECK(st == -ENODEV);
    CHECK(usd_state() == USD_ABSENT);
    CHECK(!usd_present());
    CHECK((fake_usd_ctrl() & (USD_CTRL_EN | USD_CTRL_CS)) == 0u);
    CHECK(usd_change_count() == 2u);
    CHECK(strcmp(usd_state_text(), "none") == 0);
    CHECK(usd_read_start(300u, 1u, buf) == -ENODEV);

    /* Stays ABSENT (no flapping while the debouncer catches up), and silent. */
    dw = fake_usd_data_writes();
    poll_n(1000u);
    CHECK(usd_state() == USD_ABSENT);
    CHECK(usd_change_count() == 2u);
    CHECK(fake_usd_data_writes() == dw);

    /* Re-insert: READY again, and I/O works. */
    fake_usd_set_write_busy(0u);
    fake_usd_insert();
    poll_until_state(USD_READY, debounce_ms + USD_SETTLE_MS + 400u);
    CHECK(usd_change_count() == 3u);
    CHECK(usd_write_start(300u, 4u, buf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    {
        static uint8_t rb[4u * USD_BLOCK_SIZE];
        CHECK(usd_read_start(300u, 4u, rb) == 0);
        CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
        CHECK(memcmp(rb, buf, sizeof rb) == 0);
    }
    check_clean();
    printf("PASS 5  removed %s (debounce %u ms) -> io %d, ABSENT; re-insert -> ready, I/O ok\n",
           what, (unsigned)debounce_ms, st);
}

static void test_removal(void)
{
    /* Byte offsets into a 4-block op: ~9 command bytes, then 2 + 512 + 3 per
     * write block (token, data, CRC + response) or ~3 + 512 + 2 per read block. */
    removal_case(0, 1500u, 0u, 0u, "mid-read (block 3 data)");
    removal_case(0, 1500u, 0u, 10u, "mid-read (block 3 data)");
    removal_case(1, 700u, 0u, 0u, "mid-write (block 2 data)");
    removal_case(1, 700u, 0u, 10u, "mid-write (block 2 data)");
    removal_case(1, 600u, WRITE_BUSY_LONG, 0u, "mid-write (block 1 busy)");
    removal_case(1, 600u, WRITE_BUSY_LONG, 10u, "mid-write (block 1 busy)");

    /* The debounce window. A single-block read whose card leaves mid-data would,
     * if only the DEBOUNCED bit were watched, run to the end on a floating MISO
     * and report DONE with 0xFF data. The raw pin in every shift's STATUS read
     * must turn it into -ENODEV instead. */
    {
        static uint8_t buf[USD_BLOCK_SIZE];
        setup();
        fake_usd_set_debounce_ms(10u);
        fake_usd_insert();
        poll_until_state(USD_READY, 10u + USD_SETTLE_MS + 400u);
        CHECK(usd_read_start(77u, 1u, buf) == 0);
        fake_usd_remove_after_bytes(200u);
        CHECK(poll_io(IO_POLL_LIMIT) == -ENODEV);
        CHECK(usd_state() == USD_ABSENT);
        poll_n(100u);
        CHECK(usd_state() == USD_ABSENT);
        CHECK(usd_change_count() == 2u);
        check_clean();
    }

    /* Mid-init: the init stops, nothing is retried against an empty slot. */
    {
        uint32_t dw;
        setup();
        fake_usd_insert();
        poll_until_state(USD_INIT, USD_SETTLE_MS + 10u);
        fake_usd_remove_after_bytes(15u);
        poll_until_state(USD_ABSENT, 100u);
        CHECK((fake_usd_ctrl() & USD_CTRL_EN) == 0u);
        dw = fake_usd_data_writes();
        poll_n(5000u);
        CHECK(fake_usd_data_writes() == dw);
        CHECK(usd_change_count() == 2u);
        check_clean();
    }

    /* Removed AND re-inserted between two polls (the loop was busy for 60 ms):
     * the level reads "present" both times, only the sticky CD_CHANGED says it
     * is a different card. It must be re-initialised, not trusted. */
    {
        uint32_t cmd0;
        setup();
        fake_usd_set_debounce_ms(10u);
        fake_usd_insert();
        poll_until_state(USD_READY, 10u + USD_SETTLE_MS + 400u);
        cmd0 = fake_usd_cmd_count(0u);
        fake_usd_remove();
        mock_time_advance_ms(30u);
        fake_usd_tick();                                  /* CD_PRESENT drops */
        fake_usd_insert();
        mock_time_advance_ms(30u);
        fake_usd_tick();                                  /* ... and rises again */
        poll_once();
        CHECK(usd_state() == USD_SETTLE);
        CHECK(usd_change_count() == 3u);
        poll_until_state(USD_READY, USD_SETTLE_MS + 400u);
        CHECK(fake_usd_cmd_count(0u) > cmd0);             /* re-initialised */
        check_clean();
    }

    /* Removal while idle and READY: ABSENT on the next poll. */
    ready_card();
    fake_usd_remove();
    poll_once();
    CHECK(usd_state() == USD_ABSENT);
    CHECK((fake_usd_ctrl() & USD_CTRL_EN) == 0u);
    printf("PASS 5  removed inside the 10 ms debounce window of a 1-block read -> -ENODEV, not DONE\n");
    printf("PASS 5  removed mid-init -> ABSENT, silent; swapped between two polls -> re-init; "
           "removed while idle -> ABSENT next poll\n");
}

/* 5b: a READY card stops answering reads: -ETIMEDOUT, ERROR 8, re-init. */
static void test_io_timeout(void)
{
    static uint8_t buf[USD_BLOCK_SIZE];

    ready_card();
    fake_usd_set_read_nac(1000000u);                      /* token never comes */
    CHECK(usd_read_start(10u, 1u, buf) == 0);
    CHECK(poll_io(USD_READ_TIMEOUT_MS + 50u) == -ETIMEDOUT);
    CHECK(usd_state() == USD_ERROR);
    CHECK(usd_error_code() == USD_ERR_IO_TMO);
    CHECK(strcmp(usd_state_text(), "ERR 8") == 0);
    fake_usd_set_read_nac(2u);
    poll_until_state(USD_READY, USD_RETRY_MS + 100u);     /* auto re-init */
    CHECK(usd_read_start(10u, 1u, buf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    check_pattern(buf, 10u, 1u);
    printf("PASS 5b READY card stops answering -> -ETIMEDOUT, ERR 8, re-init -> ready\n");
}

/* 6: the budget, measured. */
static void test_budget(void)
{
    static uint8_t buf[USD_MAX_BLOCKS_PER_OP * USD_BLOCK_SIZE];

    /* One more full-speed 8-block read and write, so the maxima below come
     * from real data phases whatever order the cases ran in. */
    ready_card();
    CHECK(usd_read_start(123u, USD_MAX_BLOCKS_PER_OP, buf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);
    CHECK(usd_write_start(123u, USD_MAX_BLOCKS_PER_OP, buf) == 0);
    CHECK(poll_io(IO_POLL_LIMIT) == USD_IO_DONE);

    CHECK(g_max_bytes_fast <= USD_POLL_BUDGET_FAST);
    CHECK(g_max_bytes_slow <= USD_POLL_BUDGET_SLOW);
    CHECK(g_max_wire_ns <= WIRE_CAP_NS);
    /* The budget is what binds: data-phase polls run close to it. */
    CHECK(g_max_bytes_fast + BUDGET_MIN_STEP > USD_POLL_BUDGET_FAST);
    printf("PASS 6  budget over %u polls: max %u B/poll at 12.5 MHz (budget %u), "
           "max %u B/poll at 400 kHz (budget %u), max %u shifts/poll, max wire %.1f us/poll\n",
           (unsigned)g_polls, (unsigned)g_max_bytes_fast, (unsigned)USD_POLL_BUDGET_FAST,
           (unsigned)g_max_bytes_slow, (unsigned)USD_POLL_BUDGET_SLOW, (unsigned)g_max_shifts,
           (double)g_max_wire_ns / 1000.0);
}

/* 7: every state's text. (poll_once() has also checked <= 16 on every poll.) */
static void test_state_text(void)
{
    static const char *const seen[] = { "none", "init", "ready", "unsupported",
                                        "ERR 1", "ERR 4", "ERR 8", "ERR 9" };
    for (unsigned i = 0; i < sizeof seen / sizeof seen[0]; i++) {
        CHECK(strlen(seen[i]) <= 16u);
    }
    /* Each of those strings was asserted verbatim above, in the state that
     * produces it: none (1, 5), init (2), ready (2), unsupported (3),
     * ERR 1 (3b), ERR 4 (3), ERR 8 (5b), ERR 9 (3c). The widest code the
     * driver defines is 23, "ERR 23" (6 chars). */
    CHECK(USD_UNSUP_CSD < 100);
    printf("PASS 7  state text <= 16 chars on all %u polls; strings: none init ready "
           "unsupported ERR <n>\n", (unsigned)g_polls);
}

int main(int argc, char **argv)
{
    printf("=== test_usd (budget fast %u / slow %u, wait %u B/poll) ===\n",
           (unsigned)USD_POLL_BUDGET_FAST, (unsigned)USD_POLL_BUDGET_SLOW,
           (unsigned)USD_WAIT_BYTES_PER_POLL);
    test_no_card();
    test_no_block();
    test_insert_ready();
    test_cd_pol_b0();
    test_card_types();
    test_error_retry();
    test_busy_stuck();
    test_io();
    test_removal();
    test_io_timeout();
    test_budget();
    test_state_text();
    /* One source builds two binaries (default + tiny budget); the harness
     * (test_firmware_host_gcc_harness.py) wants "<binary>: N checks passed". */
    const char *self = (argc > 0 && argv[0]) ? strrchr(argv[0], '/') : NULL;
    self = self ? self + 1 : ((argc > 0 && argv[0]) ? argv[0] : "test");
    printf("%s: %u checks passed (ALL PASS, %u polls)\n", self, s_checks, (unsigned)g_polls);
    return 0;
}
