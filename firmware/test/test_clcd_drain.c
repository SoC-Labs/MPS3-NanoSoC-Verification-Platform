/*
 * test_clcd_drain.c -- lane CLCD-SLOW: clcd_poll_drain() (clcd.h THE DRAIN),
 * the harnessd-side fix for "the panel repaints glyph by glyph under Linux".
 *
 * A MODEL of one mps3-harnessd pass, on mock_regs' fake clock:
 *   - every register read costs US_PER_READ (clcd.c reads CLCD_STATUS once per
 *     byte, so this is the per-byte cost of an MMIO read + write on the MBV);
 *   - every pass costs PASS_OVERHEAD_US besides the CLCD row (the ~37
 *     clock_gettime + ~12 socket syscalls of the other 12 rows: rv32 has no
 *     vDSO, so each is a trap).
 * Then it counts passes and model time for a FULL page (the first draw, all
 * 600 cells) and for a page change (clcd_page_next()), with:
 *   A. one clcd_poll() a pass     -- today (the negative control: must be SLOW)
 *   B. clcd_poll_drain(4000) a pass -- the fix (must be FAST, bounded per pass)
 * plus the no-spin properties: FIFO full, idle, a timed init wait.
 *
 * Build (the test_clcd link set):
 *   gcc -std=gnu11 -O2 -Wall -Wextra -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD \
 *       -DMPS3_CLCD_TEST_HOOKS -o test_clcd_drain test_clcd_drain.c \
 *       ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../clcd/clcd.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "../common/diag.h"
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../smsc911x/smsc911x.h"
#include "../clcd/hx8347_init.h"
#include "mock_regs.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- the symbols clcd.c links against (test_clcd.c's pattern) ------------ */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
uint32_t swap_fsm_icap_bytes(void) { return 0; }
int smsc911x_link_up(void) { return 1; }
int smsc911x_mii_read(uint32_t reg, uint16_t *v) { if (v) *v = (reg == 5u) ? (1u << 8) : 0u; return 0; }
void mps3_platform_mac(uint8_t mac[6]) { static const uint8_t m[6] = { 2, 0, 0, 0x4D, 0x50, 0x53 }; memcpy(mac, m, 6); }

/* ---- the CLCD FIFO: never full, or always full (the no-spin case) -------- */
static int s_fifo_full;
static uint32_t s_bytes;
static int clcd_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (!is_write) {
        *val = (off == CLCD_STATUS) ? (s_fifo_full ? CLCD_STATUS_FIFO_FULL : CLCD_STATUS_FIFO_EMPTY) : 0u;
        return 1;
    }
    if (off == CLCD_CMD || off == CLCD_DATA) {
        assert(!s_fifo_full);   /* the driver never writes a full FIFO */
        s_bytes++;
    }
    return 1;
}

static void setup(void)
{
    mock_regs_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id = 0xAAF21306u;
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 1u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    s_fifo_full = 0;
    s_bytes = 0;
    mock_regs_set_hook(MPS3_CLCD_BASE, clcd_hook, 0);
    clcd_init();
}

/* ---- the harnessd pass model --------------------------------------------- */
#ifndef US_PER_READ
#define US_PER_READ      1u
#endif
#define US_PER_READ_DOC      /* ~1 us per CLCD byte (STATUS read + DATA write)  */
#ifndef PASS_OVERHEAD_US
#define PASS_OVERHEAD_US 1500u
#endif
#define PASS_OVERHEAD_DOC   /* ~50 syscalls in the other rows of a pass         */
#define DRAIN_US         4000u   /* main_linux.c HARNESSD_CLCD_DRAIN_US              */

typedef struct {
    unsigned passes;          /* every pass (a first draw includes the init waits) */
    uint32_t model_us, worst_row_us, bytes;
    unsigned rpasses;         /* from the pass the render started to idle          */
    uint32_t r_us;            /* ... and its model time: what the eye sees          */
} run_t;

/* One pass: the other rows, then the CLCD row. Returns the CLCD row's time. */
static uint32_t pass(int drained)
{
    mock_time_advance_us(PASS_OVERHEAD_US);
    uint32_t t0 = mock_time_now_us();
    if (drained) (void)clcd_poll_drain(DRAIN_US);
    else         clcd_poll();
    return mock_time_now_us() - t0;
}

/* Run passes until a render has run and the renderer is idle again with
 * nothing dirty (max_passes cap). A render pass pushes glyph cells: the init
 * table's bytes alone (first draw) do not count. */
static run_t run_until_idle(int drained, unsigned max_passes)
{
    run_t r = { 0, 0, 0, 0, 0, 0 };
    uint32_t t0 = mock_time_now_us(), b0 = s_bytes, rs_us = 0;
    unsigned rs_pass = 0;
    int rendered = 0;
    while (r.passes < max_passes) {
        uint32_t ps = mock_time_now_us();
        uint32_t row = pass(drained);
        r.passes++;
        if (row > r.worst_row_us) r.worst_row_us = row;
        if (!rendered && clcd_test_state() == CLCD_ST_RENDER) {
            rendered = 1;
            rs_us = ps;
            rs_pass = r.passes;
        }
        if (rendered && clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0)
            break;
    }
    r.model_us = mock_time_now_us() - t0;
    r.rpasses = r.passes - rs_pass + 1u;
    r.r_us = mock_time_now_us() - rs_us;
    r.bytes = s_bytes - b0;
    return r;
}

/* Bring the panel through reset + init to its first full draw. */
static run_t first_draw(int drained)
{
    setup();
    mock_regs_set_us_per_read(US_PER_READ);
    return run_until_idle(drained, 100000u);
}

static run_t page_change(int drained)
{
    (void)first_draw(drained);
    clcd_page_next();
    return run_until_idle(drained, 100000u);
}

static void report(const char *what, run_t r)
{
    printf("  %-36s render %4u passes %7.1f ms | all %4u passes %7.1f ms | worst clcd row %4u us | %6u B\n",
           what, r.rpasses, r.r_us / 1000.0, r.passes, r.model_us / 1000.0, r.worst_row_us, r.bytes);
}

int main(void)
{
    const uint32_t cell = 17u + 8u * 16u * 2u;   /* 273 B a glyph cell */
    setvbuf(stdout, NULL, _IONBF, 0);

    /* A/B: the full first draw (init table + all 600 cells). */
    run_t a = first_draw(0), b = first_draw(1);
    report("first draw, 1 clcd_poll a pass", a);
    report("first draw, clcd_poll_drain(4 ms)", b);
    CHECK(a.bytes == b.bytes);                        /* same stream, nothing lost */
    CHECK(a.bytes > 100u * cell);                    /* a real screenful */
    CHECK(a.rpasses >= (a.bytes - 200u) / CLCD_BYTES_PER_PASS);  /* today: a glyph a pass */
    CHECK(b.rpasses * 5u <= a.rpasses);               /* the fix: >= 5x fewer passes  */
    CHECK(b.r_us * 3u < a.r_us);                      /* ... and >= 3x less time      */
    CHECK(b.worst_row_us <= DRAIN_US + CLCD_BYTES_PER_PASS * US_PER_READ + 64u);

    /* A/B: a page change (status -> apps). */
    run_t pa = page_change(0), pb = page_change(1);
    report("page change, 1 clcd_poll a pass", pa);
    report("page change, clcd_poll_drain(4 ms)", pb);
    CHECK(pa.bytes == pb.bytes && pa.bytes > 0);
    CHECK(pa.r_us > 500000u);                         /* today: > 0.5 s on this model */
    CHECK(pb.rpasses * 5u <= pa.rpasses);
    CHECK(pb.r_us * 3u < pa.r_us);                    /* the fix: > 3x faster        */
    CHECK(pb.worst_row_us <= DRAIN_US + CLCD_BYTES_PER_PASS * US_PER_READ + 64u);

    /* NO SPIN 1: idle panel -> one clcd_poll, 0 bytes. (Drain first whatever
     * the 250 ms refresh found -- the uptime cells -- with the clock held.) */
    (void)first_draw(1);
    mock_regs_set_us_per_read(0u);
    for (int i = 0; i < 8; i++) (void)clcd_poll_drain(DRAIN_US);
    mock_regs_set_us_per_read(US_PER_READ);
    uint32_t t0 = mock_time_now_us();
    CHECK(clcd_poll_drain(DRAIN_US) == 0);
    CHECK(mock_time_now_us() - t0 < 64u);

    /* NO SPIN 2: FIFO full mid-render -> returns at once, 0 bytes, no write. */
    (void)first_draw(1);
    clcd_page_next();
    (void)clcd_poll_drain(DRAIN_US);                 /* start the render */
    s_fifo_full = 1;
    t0 = mock_time_now_us();
    CHECK(clcd_poll_drain(DRAIN_US) == 0);
    CHECK(mock_time_now_us() - t0 < 64u);
    CHECK(clcd_test_state() == CLCD_ST_RENDER);      /* resumes next pass */
    s_fifo_full = 0;
    run_t rest = run_until_idle(1, 1000u);
    CHECK(clcd_test_state() == CLCD_ST_IDLE && rest.passes < 1000u);

    /* NO SPIN 3: a timed init wait (HX_DLY) returns without spinning on it. */
    setup();
    mock_regs_set_us_per_read(US_PER_READ);
    int saw_wait = 0;
    for (int i = 0; i < 20000 && !saw_wait; i++) {
        mock_time_advance_us(100u);
        t0 = mock_time_now_us();
        (void)clcd_poll_drain(DRAIN_US);
        if (clcd_test_state() == CLCD_ST_INIT_WAIT) {
            saw_wait = 1;
            t0 = mock_time_now_us();
            CHECK(clcd_poll_drain(DRAIN_US) <= CLCD_BYTES_PER_PASS);
            CHECK(mock_time_now_us() - t0 < 64u);
        }
    }
    CHECK(saw_wait);

    printf("test_clcd_drain: %d checks PASS\n", s_checks);
    return 0;
}
