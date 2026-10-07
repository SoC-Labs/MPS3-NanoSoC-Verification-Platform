/*
 * test_clcd_usd.c -- the D13 user-microSD field on the CLCD status page
 * (firmware/clcd/clcd.c), row 4, cols 18..39:
 *
 *        0         1         2         3
 *        0123456789012345678901234567890123456789
 *        SID : 0xAAF21306  USD : nanosoc_mult [B]
 *
 * Built exactly like test_clcd (-DMPS3_HAL_MOCK -DMPS3_HAS_CLCD
 * -DMPS3_CLCD_TEST_HOOKS, the REAL clcd.c + hx8347_init.c + mock_regs.c). The
 * one difference: this file defines STRONG overlay_store_usd_text() /
 * overlay_store_usd_change_count(), standing in for the overlay store (lane
 * I-FW), so it can pose every state string. The weak "no hw" fallback -- a build
 * with no store -- is covered by test_clcd.c, which defines neither.
 *
 * What these prove (HANDOVER_USD_OVERLAY_STORE.md sections 1, 4.5 g, 8):
 *   1. EVERY state string the store can emit renders IN FULL after "USD : " --
 *      put_at() clips silently at col 40, so "it fits" is asserted on the glass,
 *      not on strlen() -- and nothing follows it.
 *   2. Row 4 cols 0..15 ("SID : 0x........") and the two separator blanks are
 *      untouched by every state.
 *   3. No USD state ever raises the fault banner (rows 10-12): a missing card is
 *      not a harness fault.
 *   4. A text longer than the field is clipped by the formatter at col 39; a
 *      NULL or non-printable text renders as '?', never as a plausible state.
 *   5. A change-count move refreshes the page AT ONCE (inside one 250 ms refresh
 *      window), a text change without a count move does not, and the refresh is
 *      a diff: only the USD cells (+ uptime/heartbeat) are re-pushed.
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

/* ==== the overlay store's two exports -- STRONG, so they beat clcd.c's weak
 * fallbacks. The test poses the text and moves the count. ================== */
static const char *s_usd_text  = "none";
static uint32_t    s_usd_count = 0u;
static unsigned    s_usd_text_calls;
const char *overlay_store_usd_text(void)         { s_usd_text_calls++; return s_usd_text; }
uint32_t    overlay_store_usd_change_count(void) { return s_usd_count; }

/* ==== symbols clcd.c links against (the firmware/test/ fake pattern) ======== */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
uint32_t swap_fsm_icap_bytes(void) { return 0u; }
static int s_link_up;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t reg, uint16_t *v) { if (v) *v = (reg == 0x05u) ? (1u << 8) : 0u; return 0; }
static uint8_t s_mac[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
void mps3_platform_mac(uint8_t mac[6]) { memcpy(mac, s_mac, 6); }

/* ==== CLCD FIFO: an infinite sink that counts bytes (test_clcd.c owns the
 * byte-bound proofs; here we only need the render FSM to make progress). ==== */
static unsigned s_fifo_bytes;
static int clcd_fifo_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *val)
{
    (void)c; (void)b;
    if (!wr) { *val = (off == CLCD_STATUS) ? CLCD_STATUS_FIFO_EMPTY : 0u; return 1; }
    if (off == CLCD_CMD || off == CLCD_DATA) s_fifo_bytes++;
    return 1;
}

/* Every reformat -- on ANY page -- reads the live RM out of DFXCTL (RM_ID twice,
 * the clcd_rm_live() sandwich); an idle pass that does not reformat reads none.
 * So DFXCTL.RM_ID reads are a page-independent "did it reformat?" detector. The
 * hook only counts, then falls through to the poked slots (return 0). */
static unsigned s_dfx_rmid_reads;
static int dfx_count_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *val)
{
    (void)c; (void)b; (void)val;
    if (!wr && off == DFXCTL_RM_ID) s_dfx_rmid_reads++;
    return 0;
}

/* Healthy sources, and a NON-ethernet RM (nanosoc) so row 10 carries no DIP line:
 * rows 10-12 are then blank unless a banner is raised, which makes "no banner"
 * a strict assertion. */
static void seed_healthy(void)
{
    mock_regs_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id = 0xAAF21306u;
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     0x01000001u);   /* nanosoc v1.0 */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DUT_RESETN);
    s_link_up = 1;
    s_fifo_bytes = 0;
    mock_regs_set_hook(MPS3_CLCD_BASE, clcd_fifo_hook, 0);
    mock_regs_set_hook(MPS3_DFXCTL_BASE, dfx_count_hook, 0);
}

static void poll_step(uint32_t dt) { mock_time_advance_ms(dt); clcd_poll(); }

static void row_str(const char *grid, unsigned r, char out[CLCD_COLS + 1])
{
    memcpy(out, grid + r * CLCD_COLS, CLCD_COLS);
    out[CLCD_COLS] = '\0';
}

/* Rows 10-12 blank and un-inverted: no banner, no DIP. */
static void check_no_banner(const char *grid, const uint8_t *inv)
{
    for (unsigned r = 10; r <= 12; r++) {
        CHECK(inv[r] == 0);
        for (unsigned c = 0; c < CLCD_COLS; c++)
            CHECK(grid[r * CLCD_COLS + c] == ' ');
    }
}

/* Row 4 = "SID : 0xAAF21306" + 2 blanks + "USD : " + `expect` + blanks to col 39. */
static void check_row4(const char *grid, const char *expect)
{
    const char *row4 = grid + CLCD_USD_ROW * CLCD_COLS;
    size_t n = strlen(expect);

    CHECK(n <= CLCD_USD_TEXT_MAX);
    CHECK(strncmp(row4, "SID : 0xAAF21306", 16) == 0);            /* cols 0..15  */
    for (unsigned c = 16; c < CLCD_USD_COL; c++) CHECK(row4[c] == ' ');
    CHECK(strncmp(row4 + CLCD_USD_COL, "USD : ", 6) == 0);         /* cols 18..23 */
    CHECK(strncmp(row4 + CLCD_USD_TEXT_COL, expect, n) == 0);      /* IN FULL     */
    for (unsigned c = CLCD_USD_TEXT_COL + (unsigned)n; c < CLCD_COLS; c++)
        CHECK(row4[c] == ' ');                                     /* nothing after */
}

/* ==========================================================================
 * 0. The field geometry itself.
 * ========================================================================== */
static void test_geometry(void)
{
    CHECK(CLCD_USD_TEXT_COL == CLCD_USD_COL + 6u);                /* "USD : "    */
    CHECK(CLCD_USD_TEXT_COL + CLCD_USD_TEXT_MAX <= CLCD_COLS);    /* fits row 4  */
    CHECK(CLCD_USD_TEXT_COL + CLCD_USD_TEXT_MAX == CLCD_COLS);    /* ...exactly  */
    CHECK(CLCD_USD_COL >= 16u + 2u);      /* clear of "SID : 0x........" + a gap */
    CHECK(CLCD_USD_ROW == 4u);
}

/* ==========================================================================
 * 1-3. Every state string, rendered.
 * ========================================================================== */
static const char *const STATES[] = {
    "none",             /* no card                                        */
    "no hw",            /* no usd_spi block (older shell)                 */
    "init",             /* probing the card / verifying the store         */
    "unsupported",      /* SDSC <= 2 GB                                   */
    "ERR 30",           /* card init or read error                        */
    "foreign",          /* not harness-formatted -- never written         */
    "empty",            /* harness card, no default saved                 */
    "led [A]",          /* a valid default in slot A                      */
    "nanosoc_mult [B]", /* a 12-char name + " [B]" = 16: the widest case  */
    "stale key",        /* default minted for another shell               */
    "bad",              /* both slots fail CRC                            */
    "skipped",          /* PB1 held during power-up                       */
};
#define NSTATES (sizeof(STATES) / sizeof(STATES[0]))

static void test_every_state_renders(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    char    row[CLCD_COLS + 1];

    seed_healthy();
    clcd_init();

    for (unsigned i = 0; i < NSTATES; i++) {
        s_usd_text = STATES[i];
        CHECK(strlen(STATES[i]) <= CLCD_USD_TEXT_MAX);   /* the contract (<= 16) */
        clcd_test_render(grid, inv);
        check_row4(grid, STATES[i]);
        check_no_banner(grid, inv);
        CHECK(inv[CLCD_USD_ROW] == 0);                   /* row 4 never inverted */
        /* The neighbours are intact: row 3's swap line and row 5's NET line. */
        row_str(grid, 3, row); CHECK(strncmp(row, "SWAP: ", 6) == 0);
        row_str(grid, 5, row); CHECK(strncmp(row, "NET : 192.168.10.101", 20) == 0);
    }

    /* The widest case, pinned as the literal glass row. */
    s_usd_text = "nanosoc_mult [B]";
    clcd_test_render(grid, inv);
    row_str(grid, CLCD_USD_ROW, row);
    CHECK(strcmp(row, "SID : 0xAAF21306  USD : nanosoc_mult [B]") == 0);
    CHECK(row[CLCD_COLS - 1u] == ']');                   /* col 39 used, not lost */

    /* And the no-card row, the one david sees most. */
    s_usd_text = "none";
    clcd_test_render(grid, inv);
    row_str(grid, CLCD_USD_ROW, row);
    CHECK(strcmp(row, "SID : 0xAAF21306  USD : none            ") == 0);
}

/* A USD state never raises the banner -- and never SUPPRESSES a real one either:
 * with the link down the banner is still NETWORK LINK DOWN, and row 4 still shows
 * the card. (The two are independent inputs.) */
static void test_usd_neither_raises_nor_hides_banner(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    char    row[CLCD_COLS + 1];

    seed_healthy();
    clcd_init();
    s_link_up = 0;
    for (unsigned i = 0; i < NSTATES; i++) {
        s_usd_text = STATES[i];
        clcd_test_render(grid, inv);
        CHECK(inv[10] == 1 && inv[11] == 1 && inv[12] == 1);
        row_str(grid, 11, row);
        CHECK(strstr(row, "NETWORK LINK DOWN") != NULL);
        check_row4(grid, STATES[i]);
    }
    s_link_up = 1;
}

/* ==========================================================================
 * 4. Out-of-contract provider text: clipped / sanitised, never overflowing.
 * ========================================================================== */
static void test_fmt_usd_and_clipping(void)
{
    char b[CLCD_USD_TEXT_MAX + 8];
    char grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];

    clcd_fmt_usd(b, "led [A]");               CHECK(strcmp(b, "led [A]") == 0);
    clcd_fmt_usd(b, "");                      CHECK(strcmp(b, "") == 0);
    clcd_fmt_usd(b, NULL);                    CHECK(strcmp(b, "?") == 0);
    clcd_fmt_usd(b, "0123456789abcdef");      CHECK(strcmp(b, "0123456789abcdef") == 0);
    clcd_fmt_usd(b, "0123456789abcdefXYZ");   CHECK(strcmp(b, "0123456789abcdef") == 0);
    clcd_fmt_usd(b, "ERR\t3\n");              CHECK(strcmp(b, "ERR?3?") == 0);

    seed_healthy();
    clcd_init();

    /* 20 chars from a buggy provider: exactly the first 16 land, cols 24..39. */
    s_usd_text = "abcdefghijklmnopqrst";
    clcd_test_render(grid, inv);
    check_row4(grid, "abcdefghijklmnop");
    check_no_banner(grid, inv);
    /* Row 5 is untouched (put_at cannot wrap, and the formatter clipped first). */
    CHECK(strncmp(grid + 5u * CLCD_COLS, "NET : ", 6) == 0);

    /* NULL: an honest "?", not a plausible state. */
    s_usd_text = NULL;
    clcd_test_render(grid, inv);
    check_row4(grid, "?");
    s_usd_text = "none";
}

/* ==========================================================================
 * 5. Refresh on a change-count move -- inside one CLCD_REFRESH_MS window.
 * ========================================================================== */
static void settle(void)
{
    /* Force one reformat, then drive with ZERO elapsed time: the forced refresh
     * restarts the 250 ms window (s_last_refresh = now), and with the clock
     * frozen no periodic refresh can follow. 3000 passes drain even a full-page
     * repaint (600 cells, ~1 cell per 256-byte pass). Ends idle and clean, at
     * the START of a refresh window. */
    clcd_test_force_reformat();
    for (int i = 0; i < 3000; i++) poll_step(0);
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcd_test_dirty_count() == 0);
}

static void test_refresh_on_change_count(void)
{
    char    before[CLCD_NCELLS], after[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    unsigned full = 0;

    for (unsigned i = 0; i < hx8347_init_len; i++)
        if (hx8347_init[i].op != HX_DLY) full++;
    full += CLCD_NCELLS * (17u + 8u * 16u * 2u);

    seed_healthy();
    s_usd_text  = "none";
    s_usd_count = 7u;                  /* arbitrary start: clcd_init() latches it */
    clcd_init();

    /* Boot, init, first full paint. */
    for (int i = 0; i < 40000; i++) {
        poll_step(5);
        if (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0 &&
            s_fifo_bytes >= full)
            break;
    }
    CHECK(s_fifo_bytes >= full);
    settle();
    clcd_test_shadow(before, inv);
    check_row4(before, "none");

    /* (a) Quiet window: 20 ms, no count move -> no reformat at all. The provider
     *     text is not even read (it is read only by a reformat). */
    s_usd_text_calls = 0;
    s_dfx_rmid_reads = 0;
    for (int i = 0; i < 20; i++) poll_step(1);
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcd_test_dirty_count() == 0);
    CHECK(s_usd_text_calls == 0);
    CHECK(s_dfx_rmid_reads == 0);                    /* no reformat happened */

    /* (b) The text changes WITHOUT a count move: still inside the window, so the
     *     glass keeps the old text. The count -- not a text poll -- is the
     *     trigger; this is what keeps the idle path to one O(1) getter. */
    s_usd_text = "init";
    for (int i = 0; i < 20; i++) poll_step(1);
    clcd_test_shadow(after, inv);
    check_row4(after, "none");
    CHECK(s_usd_text_calls == 0);

    /* (c) The count moves (a card went in): the very next idle pass reformats,
     *     ~40 ms into a 250 ms window. */
    s_usd_count++;
    poll_step(1);
    CHECK(s_usd_text_calls == 1);
    CHECK(s_dfx_rmid_reads == 2);                    /* exactly ONE reformat */
    clcd_test_shadow(after, inv);
    check_row4(after, "init");
    check_no_banner(after, inv);
    CHECK(clcd_test_dirty_count() > 0);
    CHECK(clcd_test_state() == CLCD_ST_RENDER);

    /* It is a DIFF: outside the USD text field, the only cells that may differ
     * are the uptime (row 6) and the heartbeat spinner (row 14). */
    for (unsigned i = 0; i < CLCD_NCELLS; i++) {
        unsigned r = i / CLCD_COLS, c = i % CLCD_COLS;
        if (r == CLCD_USD_ROW && c >= CLCD_USD_TEXT_COL) continue;
        if (r == 6u || r == 14u) continue;
        CHECK(before[i] == after[i]);
    }

    /* The render drains back to idle; the latched count now matches, so the next
     * quiet window is quiet again (no refresh storm on a stable count). */
    settle();
    s_usd_text_calls = 0;
    for (int i = 0; i < 20; i++) poll_step(1);
    CHECK(clcd_test_dirty_count() == 0);
    CHECK(s_usd_text_calls == 0);

    /* (d) Several moves, one per state: each one lands on the glass without
     *     waiting for the periodic tick. */
    for (unsigned k = 0; k < NSTATES; k++) {
        settle();
        s_usd_text = STATES[k];
        s_usd_count++;
        poll_step(1);
        clcd_test_shadow(after, inv);
        check_row4(after, STATES[k]);
        check_no_banner(after, inv);
    }

    /* (e) A text change with no count move is still picked up by the periodic
     *     refresh -- the count only makes it immediate, it is not the only path. */
    settle();
    s_usd_text = "stale key";
    poll_step(CLCD_REFRESH_MS);
    clcd_test_shadow(after, inv);
    check_row4(after, "stale key");

    /* (f) A count move while the Apps page is up must not reformat forever: the
     *     latch is taken on every page, so one move = one refresh. (The Apps page
     *     draws no USD field, and it has no heartbeat, so a storm would change no
     *     cell -- the DFXCTL read counter is what sees it.) */
    settle();
    clcd_test_set_page(CLCD_PAGE_APPS);
    settle();
    s_dfx_rmid_reads = 0;
    s_usd_count++;
    poll_step(1);
    CHECK(s_dfx_rmid_reads == 2);                    /* one refresh for the move */
    s_dfx_rmid_reads = 0;
    s_usd_text_calls = 0;
    for (int i = 0; i < 20; i++) poll_step(1);
    CHECK(s_dfx_rmid_reads == 0);                    /* ...and then quiet        */
    CHECK(s_usd_text_calls == 0);                    /* Apps never reads the text */
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcd_test_dirty_count() == 0);
    clcd_test_set_page(CLCD_PAGE_STATUS);
    s_usd_text = "none";
}

int main(void)
{
    test_geometry();
    test_every_state_renders();
    test_usd_neither_raises_nor_hides_banner();
    test_fmt_usd_and_clipping();
    test_refresh_on_change_count();
    printf("test_clcd_usd: %d checks passed\n", s_checks);
    return 0;
}
