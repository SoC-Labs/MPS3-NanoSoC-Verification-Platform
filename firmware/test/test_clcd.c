/*
 * test_clcd.c -- host-gcc unit/integration tests for firmware/clcd/clcd.c, the
 * cooperative HX8347-D status-display driver + software text renderer.
 *
 * Built with -DMPS3_HAL_MOCK (so clcd.c's register accesses hit mock_regs.c's
 * in-memory file), -DMPS3_HAS_CLCD (exercise the gated code) and
 * -DMPS3_CLCD_TEST_HOOKS (introspection). Links the REAL panel init table
 * (../clcd/hx8347_init.c) so the no-loss test proves the driver streams the
 * actual landed table faithfully.
 *
 * What these prove (the four required properties + a bounded-progress check):
 *   1. THE PER-clcd_poll() BYTE BOUND, against a FIFO that NEVER drains: every
 *      pass returns having pushed <= CLCD_BYTES_PER_PASS and the loop never
 *      hangs (a busy-wait on FIFO space would never return here).
 *   2. bounded pushes WITH room + forward progress to completion + exact byte
 *      accounting (a full-screen redraw completes in a bounded number of passes,
 *      each within the budget, losing no bytes).
 *   3. forward progress + ZERO byte loss when the FIFO drains SLOWLY: the driver
 *      only ever writes when !fifo_full, so the drop-on-full FIFO drops nothing
 *      and the captured byte stream equals the table exactly.
 *   4. shadow-buffer diff / dirty-cell math.
 *   5. the formatters (uptime ddd:hh:mm:ss incl. wrap, static_id hex, rm_id,
 *      link state) against seeded mock registers, and the board name (the
 *      MPS3_BOARD_NAME default + the clcd_set_board_name() override) as it is
 *      RENDERED into row 0.
 *   6. the MADCTL (0x16) datum the landed table ships -- the CLCD_ROTATE_180
 *      panel flip. That rotation is a change to the PANEL's scan direction, so
 *      it is invisible to the shadow buffer and CANNOT be proven off-target;
 *      the table byte + its bit invariants are all that is checkable here.
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

/* ==========================================================================
 * Symbols clcd.c links against -- defined here (the firmware/test/ pattern),
 * so the test controls every field source without pulling in coordinator.c /
 * swap_fsm.c / smsc911x.c / diag.c.
 * ========================================================================== */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;

static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }

static uint32_t s_icap_bytes;
uint32_t swap_fsm_icap_bytes(void) { return s_icap_bytes; }

static int      s_link_up;      /* smsc911x_link_up() return */
static uint16_t s_anlpar;       /* value smsc911x_mii_read(0x05) yields */
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t reg, uint16_t *val_out)
{
    if (val_out) *val_out = (reg == 0x05u) ? s_anlpar : 0u;
    return 0;
}

static uint8_t s_mac[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
void mps3_platform_mac(uint8_t mac[6]) { memcpy(mac, s_mac, 6); }

#ifdef TEST_CLCD_ENGINE
/* The engine-row provider, as mps3-harnessd supplies it (tofu_linux.c). The
 * default build of this file links clcd.c's WEAK one: the bare-metal image. */
static int                s_eng_on;
static mps3_clcd_engine_t s_eng;
int mps3_clcd_engine(mps3_clcd_engine_t *out)
{
    if (!s_eng_on) return 0;
    *out = s_eng;
    return 1;
}
#endif

/* ==========================================================================
 * Behavioural CLCD FIFO model (mock_regs hook on MPS3_CLCD_BASE).
 *   - NEVER_FULL : infinite sink (fifo_full always 0)         -> bound test
 *   - ALWAYS_FULL: fifo_full always 1                          -> never-drain
 *   - CAP        : real capacity; drained ONLY by fifo_drain() -> slow-drain
 * Writes to CMD/DATA arriving while full are DROPPED (the block's contract);
 * the driver must never trigger that, so `dropped` must stay 0.
 * ========================================================================== */
enum { FIFO_NEVER_FULL, FIFO_ALWAYS_FULL, FIFO_CAP };

static struct {
    int      mode;
    unsigned cap;
    unsigned occ;
    unsigned dropped;
    int      logging;
    unsigned logn;
    struct { uint8_t rs; uint8_t val; } log[512];
} fifo;

static int fifo_full_now(void)
{
    if (fifo.mode == FIFO_ALWAYS_FULL) return 1;
    if (fifo.mode == FIFO_NEVER_FULL)  return 0;
    return fifo.occ >= fifo.cap;
}

static int clcd_fifo_hook(void *ctx, int is_write,
                          uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (!is_write) {
        if (off == CLCD_STATUS) {
            uint32_t st = 0;
            if (fifo_full_now())          st |= CLCD_STATUS_FIFO_FULL;
            if (fifo.occ == 0)            st |= CLCD_STATUS_FIFO_EMPTY;
            st |= (fifo.occ << CLCD_STATUS_LEVEL_SHIFT) & CLCD_STATUS_LEVEL_MASK;
            *val = st;
            return 1;
        }
        *val = 0;                 /* READ etc. -- inert on this build */
        return 1;
    }
    /* write */
    if (off == CLCD_CMD || off == CLCD_DATA) {
        if (fifo_full_now()) {
            fifo.dropped++;       /* contract: dropped, not stalled */
            return 1;
        }
        if (fifo.logging && fifo.logn < (unsigned)(sizeof(fifo.log) / sizeof(fifo.log[0]))) {
            fifo.log[fifo.logn].rs  = (off == CLCD_DATA) ? 1u : 0u;
            fifo.log[fifo.logn].val = (uint8_t)(*val & 0xFFu);
            fifo.logn++;
        }
        if (fifo.mode == FIFO_CAP) fifo.occ++;
        return 1;
    }
    return 1;                     /* CTRL / TIMING -- consume, ignore */
}

static void fifo_drain(unsigned n)
{
    if (fifo.mode != FIFO_CAP) return;
    fifo.occ = (fifo.occ > n) ? fifo.occ - n : 0u;
}

static void fifo_setup(int mode, unsigned cap, int logging)
{
    memset(&fifo, 0, sizeof(fifo));
    fifo.mode = mode;
    fifo.cap = cap;
    fifo.logging = logging;
    mock_regs_set_hook(MPS3_CLCD_BASE, clcd_fifo_hook, 0);
}

/* ==========================================================================
 * DFXCTL seeding -- the LIVE resident-RM source (clcd.h). The panel reads
 * DFXCTL.RM_ID @0x10 qualified by RM_STATUS.rm_id_valid @0x14 and STATUS @0x08;
 * these helpers fabricate the three registers a real dfx_ctl would present.
 * ========================================================================== */

/* Coupled, out of reset, id settled: rm_id_valid set. The ONLY state in which
 * the glass may name a design. */
static void seed_rm_live(uint32_t rm_id)
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     rm_id);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
}

/* RP isolated. dfx_ctl's rm_id_gate is low, so rm_id_valid CANNOT be set, and
 * the rm_id bus reads the decoupler's DECOUPLED_VALUE (0x0) -- the clamp that
 * "0 == greybox" would misread as a real design. */
static void seed_rm_decoupled(void)
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    DFXCTL_STATUS_DECOUPLED);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     0u);   /* the clamp */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u);   /* !rm_id_valid */
}

/* Coupled and released, but the settle detector has not (yet) qualified the id:
 * mid-reconfiguration, or a shell that never asserts the bit at all. */
static void seed_rm_not_valid(uint32_t rm_id_on_the_bus)
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     rm_id_on_the_bus);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u);
}

/* Healthy field sources (no error banner). */
static void seed_healthy(void)
{
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id     = 0xAAF21306u;
    /* DELIBERATE TRAP: the firmware cache is left at 0 (= "greybox" under the
     * software convention) while the CSR below says nanosoc. That is EXACTLY
     * the silicon symptom -- a JTAG-loaded RM the firmware never saw. Every
     * render assertion in this file therefore fails loudly if anyone ever
     * re-points the panel back at g_shell_state.current_rm_id: the glass would
     * say "greybox" again. Do not "fix" this by seeding the cache. */
    g_shell_state.current_rm_id = 0u;
    seed_rm_live(0x00000001u);    /* the RP really holds nanosoc v0.0 */
    s_icap_bytes = 0;
    s_link_up = 1;
    s_anlpar  = (1u << 8);        /* 100BASE-TX full duplex */
    /* CLKRST.STATUS: mmcm_locked + dut_clk_alive; RESET_CTRL: dut released */
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL,
                   CLKRST_RESET_CTRL_DUT_RESETN);
}

/* Poll once, advancing the mock timebase so timed waits (reset pulse, HX_DLY
 * settles, refresh cadence) all elapse. */
static void poll_step(uint32_t dt_ms)
{
    mock_time_advance_ms(dt_ms);
    clcd_poll();
}

/* ==========================================================================
 * 1. Per-pass byte bound: FIFO NEVER drains (permanently full).
 * ========================================================================== */
static void test_byte_bound_never_drains(void)
{
    /* (a) Permanently-full FIFO: the harshest no-spin case. */
    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_ALWAYS_FULL, 0, 0);
    clcd_init();

    /* Get past the reset pulse into a PUSHING state (INIT), where the driver
     * would spin if it busy-waited on FIFO space. */
    for (int i = 0; i < 8; i++) poll_step(5);
    CHECK(clcd_test_state() == CLCD_ST_INIT);

    /* Hammer it: every pass must return (loop completes = no hang) and push
     * nothing (the FIFO is full), never exceeding the budget. */
    for (int i = 0; i < 50000; i++) {
        clcd_poll();
        CHECK(clcd_test_bytes_last_pass() <= CLCD_BYTES_PER_PASS);
    }
    CHECK(clcd_test_state() == CLCD_ST_INIT);   /* stuck but never spinning */
    CHECK(clcd_test_bytes_last_pass() == 0);
    CHECK(fifo.dropped == 0);

    /* (b) A real FIFO that FILLS then never drains: pushes are bounded by the
     * capacity, every pass stays within the budget, and once full it plateaus
     * -- it never spins trying to make room and never over-pushes. */
    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_CAP, /*cap=*/50, /*logging=*/0);   /* no fifo_drain() calls */
    clcd_init();
    for (int i = 0; i < 8; i++) poll_step(5);
    CHECK(clcd_test_state() == CLCD_ST_INIT);
    for (int i = 0; i < 50000; i++) {
        clcd_poll();
        CHECK(clcd_test_bytes_last_pass() <= CLCD_BYTES_PER_PASS);
    }
    CHECK(clcd_test_bytes_total() == 50);       /* exactly the capacity, no more */
    CHECK(clcd_test_bytes_last_pass() == 0);    /* plateaued */
    CHECK(fifo.dropped == 0);                   /* never wrote into a full FIFO */
}

/* ==========================================================================
 * 2. Bounded pushes WITH room + forward progress + exact accounting.
 *    Infinite sink; a full-screen redraw completes in a bounded pass count,
 *    each pass within the budget, and the total byte count is exact.
 * ========================================================================== */
static unsigned init_push_count(void)   /* non-DLY entries of the real table */
{
    unsigned n = 0;
    for (unsigned i = 0; i < hx8347_init_len; i++)
        if (hx8347_init[i].op != HX_DLY) n++;
    return n;
}

static void test_byte_bound_with_room(void)
{
    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();

    /* Per-cell byte cost: 8 window-reg writes (cmd+data) + RAMWR + 8*16*2 px. */
    const unsigned cell_bytes = 17u + (8u * 16u * 2u);   /* 273 */
    const unsigned expect_total = init_push_count() + CLCD_NCELLS * cell_bytes;

    int reached_idle_after_render = 0;
    for (int i = 0; i < 20000; i++) {
        poll_step(5);
        CHECK(clcd_test_bytes_last_pass() <= CLCD_BYTES_PER_PASS);
        /* Done when we are back in IDLE with nothing left dirty AND we have
         * pushed the whole screen (i.e. the render actually ran). */
        if (clcd_test_state() == CLCD_ST_IDLE &&
            clcd_test_dirty_count() == 0 &&
            clcd_test_bytes_total() >= expect_total) {
            reached_idle_after_render = 1;
            break;
        }
    }
    CHECK(reached_idle_after_render);
    CHECK(clcd_test_bytes_total() == expect_total);   /* no loss, no duplication */
    CHECK(fifo.dropped == 0);
}

/* ==========================================================================
 * 3. Forward progress + ZERO byte loss with a slowly-draining FIFO.
 *    Drive the INIT stream (the real landed table) through a small FIFO drained
 *    a few bytes per pass; the captured {RS,byte} stream must equal the table.
 * ========================================================================== */
static void test_slow_drain_no_loss(void)
{
    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_CAP, /*cap=*/16, /*logging=*/1);
    clcd_init();

    /* Expected stream = every non-DLY table entry, in order. */
    static struct { uint8_t rs, val; } expect[512];
    unsigned en = 0;
    for (unsigned i = 0; i < hx8347_init_len; i++) {
        if (hx8347_init[i].op == HX_DLY) continue;
        expect[en].rs  = (hx8347_init[i].op == HX_DAT) ? 1u : 0u;
        expect[en].val = hx8347_init[i].val;
        en++;
    }

    /* Poll until INIT completes (state advances to IDLE). Break BEFORE the
     * first reformat/render runs, so the capture is purely the init stream.
     * Advance time each pass (clears the 5/10/100/100 ms HX_DLY deadlines) and
     * drain a few FIFO slots per pass (the "slow panel"). */
    int reached_idle = 0;
    for (int i = 0; i < 20000; i++) {
        poll_step(3);
        fifo_drain(4);
        CHECK(clcd_test_bytes_last_pass() <= CLCD_BYTES_PER_PASS);
        if (clcd_test_state() == CLCD_ST_IDLE) { reached_idle = 1; break; }
    }
    CHECK(reached_idle);
    CHECK(fifo.dropped == 0);            /* never wrote into a full FIFO       */
    CHECK(fifo.logn == en);              /* every byte arrived, none extra     */
    for (unsigned i = 0; i < en; i++) {  /* in order, unchanged                */
        CHECK(fifo.log[i].rs  == expect[i].rs);
        CHECK(fifo.log[i].val == expect[i].val);
    }
}

/* Empty-table graceful path: hx8347_init_len==0 must not wedge INIT. We cannot
 * null the real table, so this asserts the driver's own guard: when the cursor
 * has consumed the whole table it enters IDLE (never loops in INIT). Covered
 * implicitly by tests 2/3 reaching IDLE; here we additionally confirm a driver
 * with a fully-consumed table transitions cleanly. */

/* ==========================================================================
 * 4. Shadow-buffer diff / dirty-cell math.
 * ========================================================================== */
static int dirty_bit(const uint8_t *d, unsigned i) { return (d[i >> 3] >> (i & 7u)) & 1; }

static void test_diff_cells(void)
{
    char cur[CLCD_NCELLS], next[CLCD_NCELLS];
    uint8_t dirty[CLCD_DIRTY_BYTES];
    memset(cur, ' ', sizeof(cur));
    memset(next, ' ', sizeof(next));
    memset(dirty, 0, sizeof(dirty));

    /* No change -> nothing dirty. */
    CHECK(clcd_diff_cells(cur, next, 0, 0, dirty) == 0);

    /* Change three specific cells. */
    next[0] = 'A';
    next[41] = 'Z';               /* row 1, col 1 */
    next[CLCD_NCELLS - 1] = '!';  /* last cell    */
    unsigned n = clcd_diff_cells(cur, next, 0, 0, dirty);
    CHECK(n == 3);
    CHECK(dirty_bit(dirty, 0));
    CHECK(dirty_bit(dirty, 41));
    CHECK(dirty_bit(dirty, CLCD_NCELLS - 1));
    CHECK(!dirty_bit(dirty, 1));

    /* Idempotent: same next again marks nothing NEW (bits already set). */
    CHECK(clcd_diff_cells(cur, next, 0, 0, dirty) == 0);

    /* Row-inversion change alone dirties every cell in that row (and only NEW
     * ones are counted). Reset dirty first. */
    memset(dirty, 0, sizeof(dirty));
    uint8_t cur_inv[CLCD_ROWS] = {0};
    uint8_t next_inv[CLCD_ROWS] = {0};
    next_inv[10] = 1;             /* banner row flips to inverted */
    memcpy(cur, next, sizeof(cur));   /* identical text so only inv differs */
    unsigned m = clcd_diff_cells(cur, next, cur_inv, next_inv, dirty);
    CHECK(m == CLCD_COLS);        /* exactly one row's worth */
    for (unsigned c = 0; c < CLCD_COLS; c++)
        CHECK(dirty_bit(dirty, 10u * CLCD_COLS + c));
    CHECK(!dirty_bit(dirty, 9u * CLCD_COLS));   /* neighbouring row untouched */
}

/* ==========================================================================
 * 5. Formatters against seeded values.
 * ========================================================================== */
static void test_fmt_uptime(void)
{
    char b[16];
    clcd_fmt_uptime(b, 0);                 CHECK(strcmp(b, "000:00:00:00") == 0);
    clcd_fmt_uptime(b, 1000);              CHECK(strcmp(b, "000:00:00:01") == 0);
    clcd_fmt_uptime(b, 59u * 1000u);       CHECK(strcmp(b, "000:00:00:59") == 0);
    clcd_fmt_uptime(b, 60u * 1000u);       CHECK(strcmp(b, "000:00:01:00") == 0);
    clcd_fmt_uptime(b, 3600u * 1000u);     CHECK(strcmp(b, "000:01:00:00") == 0);
    /* 1 day 23:45:07 */
    uint32_t t = ((1u * 86400u) + (23u * 3600u) + (45u * 60u) + 7u) * 1000u;
    clcd_fmt_uptime(b, t);                 CHECK(strcmp(b, "001:23:45:07") == 0);
    /* Counter wrap: the raw 32-bit ms max (~49.7 days). Hand-computed. */
    clcd_fmt_uptime(b, 0xFFFFFFFFu);       CHECK(strcmp(b, "049:17:02:47") == 0);
}

static void test_fmt_hex32(void)
{
    char b[16];
    clcd_fmt_hex32(b, 0xAAF21306u);   CHECK(strcmp(b, "0xAAF21306") == 0);
    clcd_fmt_hex32(b, 0);             CHECK(strcmp(b, "0x00000000") == 0);
    clcd_fmt_hex32(b, 0xDEADBEEFu);   CHECK(strcmp(b, "0xDEADBEEF") == 0);
}

static void test_rm_name(void)
{
    /* Keyed on the DESIGN half (rm_id v2: {major[31:24],minor[23:16],
     * design[15:0]}), so the SAME design resolves to the same name at every
     * version -- that is the property that stops a re-versioned RM from
     * silently falling back to "rm?<hex>" on the glass. */
    CHECK(strcmp(clcd_rm_name(0x00000000u), "greybox") == 0);
    CHECK(strcmp(clcd_rm_name(0x00000001u), "nanosoc") == 0);
    CHECK(strcmp(clcd_rm_name(0x00000002u), "eth_ss") == 0);
    CHECK(strcmp(clcd_rm_name(0x00000003u), "nanosoc_multicore") == 0);
    CHECK(strcmp(clcd_rm_name(0x0000001Eu), "led") == 0);
    CHECK(strcmp(clcd_rm_name(0x000000A1u), "regdemo_a") == 0);
    CHECK(strcmp(clcd_rm_name(0x000000B2u), "regdemo_b") == 0);

    /* ...and the v1.0 forms of the very same designs must name identically. */
    CHECK(strcmp(clcd_rm_name(0x01000001u), "nanosoc") == 0);
    CHECK(strcmp(clcd_rm_name(0x01000002u), "eth_ss") == 0);
    CHECK(strcmp(clcd_rm_name(0x01000003u), "nanosoc_multicore") == 0);
    CHECK(strcmp(clcd_rm_name(0x01000004u), "uart_echo") == 0);
    CHECK(strcmp(clcd_rm_name(0x0100001Eu), "led") == 0);
    /* ...at an absurd version too: the high half must never reach the lookup. */
    CHECK(strcmp(clcd_rm_name(0xFFFF0003u), "nanosoc_multicore") == 0);

    /* greybox is the carve-out: design 0 stays greybox, and 0x00000000 exactly
     * (VERSIONING_PLAN §3.2 -- an inert tie-off is not a versioned design). */
    CHECK(strcmp(clcd_rm_name(0x00000000u), "greybox") == 0);

    /* The old 32-bit uart_echo id (ASCII "ECHO") is design 0x484F under v2 --
     * NOT the new 0x0004 -- so a stale partial must render raw, not claim to be
     * the re-numbered design. Cosmetic-only, exactly as VERSIONING_PLAN §6
     * predicts; the swap still verifies (step_verify() is value-agnostic). */
    CHECK(strcmp(clcd_rm_name(0x4543484Fu), "rm?4543484F") == 0);

    /* Unknown ids render raw, never a lie -- and show the WHOLE 32-bit word. */
    CHECK(strcmp(clcd_rm_name(0x00001234u), "rm?00001234") == 0);
    CHECK(strcmp(clcd_rm_name(0x01001234u), "rm?01001234") == 0);
}

static void test_fmt_rm_version(void)
{
    char b[16];
    clcd_fmt_rm_version(b, 0x01000003u);  CHECK(strcmp(b, "v1.0") == 0);
    clcd_fmt_rm_version(b, 0x0102001Eu);  CHECK(strcmp(b, "v1.2") == 0);
    clcd_fmt_rm_version(b, 0x0A0F0001u);  CHECK(strcmp(b, "v10.15") == 0);
    /* greybox / any pre-v2 partial: v0.0 is RENDERED, not blanked -- it is what
     * the bits say, and suppressing it would be the shell editorialising. */
    clcd_fmt_rm_version(b, 0x00000000u);  CHECK(strcmp(b, "v0.0") == 0);
    clcd_fmt_rm_version(b, 0x00000003u);  CHECK(strcmp(b, "v0.0") == 0);
    /* The widest possible string still fits the declared field. */
    clcd_fmt_rm_version(b, 0xFFFFFFFFu);  CHECK(strcmp(b, "v255.255") == 0);
    CHECK(strlen(b) == CLCD_RM_VER_MAX);
    /* The version must come from the HIGH half only -- never the design bits. */
    clcd_fmt_rm_version(b, 0x0100FFFFu);  CHECK(strcmp(b, "v1.0") == 0);
}

/* ==========================================================================
 * THE REGRESSION GUARD (the whole point of this batch).
 *
 * Row 2 silently overflowed for real: the name went in at col 6 and a second
 * field at col 21, giving the name 14 columns -- while clcd_rm_name() already
 * returned "nanosoc_multicore", 17 chars. It had simply never been LOOKED at,
 * because the glass had only ever shown "greybox" (7). put_at() clips at the
 * row edge, so nothing crashed; the name just ate its neighbour's columns.
 *
 * A test that checked only today's seven names would have caught today's bug
 * and none of the next one. So sweep the WHOLE design-id space -- all 65536
 * values the mask can produce -- and assert the field bounds for every one.
 * Any name added to the table lands inside this sweep by construction: there is
 * no way to add a case clcd_rm_name() can return that this does not test.
 * ========================================================================== */
static void test_row2_fields_cannot_overflow(void)
{
    /* The layout invariant itself: the two fields cannot touch, and the version
     * cannot run off the row. Everything below rests on these. */
    CHECK(CLCD_RM_NAME_COL + CLCD_RM_NAME_MAX <= CLCD_RM_VER_COL);
    CHECK(CLCD_RM_VER_COL + CLCD_RM_VER_MAX <= CLCD_COLS);
    CHECK(CLCD_RM_NAME_COL >= 6u);            /* clear of the "DUT : " label */

    /* Exhaustive over every design id, at several versions (incl. the widest
     * one, which is where a version string would overrun if it ever grew). */
    static const uint32_t VER_HI[] = {
        0x00000000u,  /* v0.0   -- pre-v2 / greybox   */
        0x01000000u,  /* v1.0   -- the first release  */
        0x0A0F0000u,  /* v10.15                       */
        0xFFFF0000u,  /* v255.255 -- widest possible  */
    };
    char ver[16];

    for (unsigned d = 0; d <= 0xFFFFu; d++) {
        for (unsigned v = 0; v < sizeof(VER_HI) / sizeof(VER_HI[0]); v++) {
            uint32_t rm_id = VER_HI[v] | d;

            const char *name = clcd_rm_name(rm_id);
            CHECK(name != NULL);
            size_t nlen = strlen(name);
            CHECK(nlen > 0);                       /* never a blank DUT field */
            CHECK(nlen <= CLCD_RM_NAME_MAX);       /* <-- THE ASSERTION       */

            clcd_fmt_rm_version(ver, rm_id);
            CHECK(strlen(ver) <= CLCD_RM_VER_MAX);
            CHECK(ver[0] == 'v');

            /* The two rendered fields cannot collide, at any id, any version. */
            CHECK(CLCD_RM_NAME_COL + nlen <= CLCD_RM_VER_COL);
            CHECK(CLCD_RM_VER_COL + strlen(ver) <= CLCD_COLS);

            /* Row 9's makeup line shares the same discipline: "CFG : " + caps
             * must fit the 40-col row, or it would clip at the row edge. */
            const char *caps = clcd_rm_caps(rm_id);
            CHECK(caps != NULL);
            CHECK(6u + strlen(caps) <= CLCD_COLS);
        }
    }
}

/* And prove it where it actually matters -- in the RENDERED frame, not just in
 * strlen(). Renders row 2 for the longest name in the table alongside its
 * version and reads the glass back cell by cell. */
static void test_row2_rendered_long_name_and_version(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];

    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();

    /* nanosoc_multicore v1.0 -- the 17-char name that was overflowing. Seeded
     * into the DFXCTL CSR (the live source), NOT g_shell_state. */
    seed_rm_live(0x01000003u);
    clcd_test_render(grid, inv);

    const char *row2 = grid + 2u * CLCD_COLS;
    CHECK(strncmp(row2, "DUT : ", 6) == 0);
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "nanosoc_multicore", 17) == 0);
    /* The cell right after the name is BLANK -- i.e. the name did not run into
     * anything, which is precisely what it used to do. */
    CHECK(row2[CLCD_RM_NAME_COL + 17u] == ' ');
    /* The version renders in its own field, intact. */
    CHECK(strncmp(row2 + CLCD_RM_VER_COL, "v1.0", 4) == 0);
    /* Nothing at all between the two fields. */
    for (unsigned c = CLCD_RM_NAME_COL + 17u; c < CLCD_RM_VER_COL; c++)
        CHECK(row2[c] == ' ');
    /* The raw hex id is gone from row 2 (it is what the name overflowed into). */
    CHECK(strstr(row2, "rm_id") == NULL);

    /* Same design, DIFFERENT version -> same name, new version. Proves the
     * lookup really is masked in the rendered path, not just in the unit call. */
    seed_rm_live(0x020A0003u);
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "nanosoc_multicore", 17) == 0);
    CHECK(strncmp(row2 + CLCD_RM_VER_COL, "v2.10", 5) == 0);

    /* An unrecognised id still shows its full raw hex, and still fits. */
    seed_rm_live(0x0100DEADu);
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "rm?0100DEAD", 11) == 0);
    CHECK(row2[CLCD_RM_NAME_COL + 11u] == ' ');
    CHECK(strncmp(row2 + CLCD_RM_VER_COL, "v1.0", 4) == 0);

    /* greybox: design 0, v0.0 -- the version is shown, not hidden. Note this is
     * a QUALIFIED zero (rm_id_valid set), i.e. a real resident greybox, which is
     * a different fact from the CLAMPED zero of a decoupled RP -- see
     * test_rm_live_source(), where that one must NOT render as greybox. */
    seed_rm_live(0x00000000u);
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "greybox", 7) == 0);
    CHECK(strncmp(row2 + CLCD_RM_VER_COL, "v0.0", 4) == 0);

    /* Rows 10-12 stay banner-reserved: nothing above may have written there.
     * (Healthy state -> no banner -> those rows are blank and un-inverted.) */
    for (unsigned r = 10; r <= 12; r++) {
        CHECK(inv[r] == 0);
        for (unsigned c = 0; c < CLCD_COLS; c++)
            CHECK(grid[r * CLCD_COLS + c] == ' ');
    }
}

/* ==========================================================================
 * THE LIVE-RM REGRESSION (this batch).
 *
 * Observed on silicon: the multicore RM was JTAG-loaded into the RP.
 * DFXCTL.RM_ID read 0x01000003 (nanosoc_multicore v1.0) -- and the LCD said
 *
 *     |DUT : greybox              v0.0         |
 *
 * The panel was rendering g_shell_state.current_rm_id, the last id the SWAP FSM
 * verified. A JTAG/ICAP load never goes through the swap FSM, so the cache was
 * stale and the glass named a design that was not in the part. Confidently.
 *
 * The fix: rows 2 and 9 read the DFXCTL CSR, qualified by RM_STATUS.rm_id_valid.
 * These tests pin every branch of that, including the ones where the honest
 * answer is "I don't know".
 * ========================================================================== */

/* Pure classifier: isolation beats the valid bit, and only a qualified,
 * un-isolated id is ever LIVE. */
static void test_rm_classify(void)
{
    const uint32_t VALID = DFXCTL_RM_STATUS_RM_ID_VALID;
    const uint32_t DEC   = DFXCTL_STATUS_DECOUPLED;
    const uint32_t RST   = DFXCTL_STATUS_RP_IN_RESET;

    CHECK(clcd_rm_classify(0u,   VALID) == CLCD_RM_LIVE);
    CHECK(clcd_rm_classify(0u,   0u)    == CLCD_RM_SETTLING);
    CHECK(clcd_rm_classify(DEC,  0u)    == CLCD_RM_DECOUPLED);
    CHECK(clcd_rm_classify(RST,  0u)    == CLCD_RM_RP_RESET);
    CHECK(clcd_rm_classify(DEC | RST, 0u) == CLCD_RM_DECOUPLED);   /* mid-swap */

    /* dfx_ctl.sv makes these pairs impossible (rm_id_gate forces rm_id_valid low
     * whenever the RP is decoupled or in reset). We check them anyway, and we
     * degrade to the ISOLATED verdict rather than to LIVE: if a shell ever did
     * report this, the id on the bus is the decoupler's clamped 0x0, and calling
     * that "greybox" is precisely the lie this whole change removes. */
    CHECK(clcd_rm_classify(DEC, VALID) == CLCD_RM_DECOUPLED);
    CHECK(clcd_rm_classify(RST, VALID) == CLCD_RM_RP_RESET);

    /* Unrelated bits (e.g. RM_STATUS.dut_lockup) must not disturb the verdict. */
    CHECK(clcd_rm_classify(0u, VALID | DFXCTL_RM_STATUS_DUT_LOCKUP) == CLCD_RM_LIVE);
    CHECK(clcd_rm_classify(0u, DFXCTL_RM_STATUS_DUT_LOCKUP) == CLCD_RM_SETTLING);
}

/* Every transient label must fit the declared name field -- the same bound
 * clcd_rm_name() is held to, for the same reason (row 2 has a neighbour). */
static void test_rm_src_text_bounds(void)
{
    static const clcd_rm_src_t ALL[] = {
        CLCD_RM_LIVE, CLCD_RM_DECOUPLED, CLCD_RM_RP_RESET, CLCD_RM_SETTLING,
    };
    for (unsigned i = 0; i < sizeof(ALL) / sizeof(ALL[0]); i++) {
        const char *t = clcd_rm_src_text(ALL[i]);
        CHECK(t != NULL);
        CHECK(strlen(t) <= CLCD_RM_NAME_MAX);
        /* LIVE is the only one with no text (a real name is rendered instead);
         * every other state MUST say something -- a blank DUT field would be
         * just another way of not knowing, quietly. */
        if (ALL[i] == CLCD_RM_LIVE) CHECK(strlen(t) == 0);
        else                        CHECK(strlen(t) > 0);
    }
    /* Row 9's unknown text fits after "CFG : " on the 40-col row. */
    CHECK(6u + strlen(CLCD_RM_CAPS_UNKNOWN) <= CLCD_COLS);
}

/* A DFXCTL hook that changes RM_ID between the two reads clcd_rm_live() makes,
 * i.e. a reconfiguration landing exactly inside the read sandwich. */
static struct { uint32_t ids[4]; unsigned n; uint32_t status, rm_status; } dfxrace;
static int dfx_race_hook(void *ctx, int is_write, uint32_t base, uint32_t off,
                         uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write) return 1;
    if (off == DFXCTL_RM_ID) {
        unsigned i = dfxrace.n < 4u ? dfxrace.n : 3u;
        *val = dfxrace.ids[i];
        dfxrace.n++;
        return 1;
    }
    if (off == DFXCTL_STATUS)    { *val = dfxrace.status;    return 1; }
    if (off == DFXCTL_RM_STATUS) { *val = dfxrace.rm_status; return 1; }
    *val = 0;
    return 1;
}

static void test_rm_live_source(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    const char *row2, *row9;
    uint32_t id;

    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();

    /* --- (a) THE SILICON CASE: the live id is displayed ------------------
     * Firmware cache = 0 (greybox); the RP actually holds the multicore RM,
     * JTAG-loaded behind the firmware's back. The glass must show the RM. */
    g_shell_state.current_rm_id = 0x00000000u;      /* the stale cache */
    seed_rm_live(0x01000003u);                      /* the truth, in hardware */
    CHECK(clcd_rm_live(&id) == CLCD_RM_LIVE);
    CHECK(id == 0x01000003u);
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    row9 = grid + 9u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "nanosoc_multicore", 17) == 0);
    CHECK(strncmp(row2 + CLCD_RM_VER_COL,  "v1.0", 4) == 0);
    CHECK(strncmp(row9 + 6, "2x CPU  ETH MAC+PTP  2x UART", 27) == 0);
    /* and the stale name is NOWHERE on the glass */
    CHECK(strstr(row2, "greybox") == NULL);
    CHECK(strstr(row9, "greybox") == NULL);

    /* The cache is now IRRELEVANT to the panel: poison it with a different
     * design and the glass must not budge. */
    g_shell_state.current_rm_id = 0x0100001Eu;      /* "led" -- a lie */
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "nanosoc_multicore", 17) == 0);
    CHECK(strstr(row2, "led") == NULL);

    /* A second JTAG load, again with NO firmware swap: the next refresh tracks
     * it. This is the property the cache structurally cannot have. */
    seed_rm_live(0x01000002u);                      /* eth_ss v1.0 */
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    row9 = grid + 9u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "eth_ss", 6) == 0);
    CHECK(strncmp(row2 + CLCD_RM_VER_COL,  "v1.0", 4) == 0);
    CHECK(strncmp(row9 + 6, "no CPU  ETH MAC+PTP  no UART", 27) == 0);

    /* --- (b) rm_id_valid CLEAR -> transient, never a stale name ---------- */
    seed_rm_live(0x01000003u);                      /* glass shows multicore  */
    clcd_test_render(grid, inv);
    CHECK(strncmp(grid + 2u * CLCD_COLS + CLCD_RM_NAME_COL, "nanosoc_multicore", 17) == 0);

    seed_rm_not_valid(0x01000003u);   /* same bus value -- but NOT qualified */
    CHECK(clcd_rm_live(&id) == CLCD_RM_SETTLING);
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    row9 = grid + 9u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "RM ID NOT VALID", 15) == 0);
    /* NOT the name that was on the glass a moment ago, and NOT a name at all --
     * even though the value on the bus happens to be a nameable one. The bit
     * says we may not trust it; so we do not. */
    CHECK(strstr(row2, "nanosoc") == NULL);
    CHECK(strstr(row2, "greybox") == NULL);
    /* No version either: the version field is blank. */
    for (unsigned c = CLCD_RM_VER_COL; c < CLCD_COLS; c++)
        CHECK(row2[c] == ' ');
    CHECK(strstr(row2, "v1.0") == NULL);
    /* Row 9 says it does not know, rather than describing the unqualified id. */
    CHECK(strncmp(row9 + 6, CLCD_RM_CAPS_UNKNOWN, strlen(CLCD_RM_CAPS_UNKNOWN)) == 0);
    CHECK(strstr(row9, "2x CPU") == NULL);

    /* --- (c) DECOUPLED -> NOT rendered as greybox ------------------------
     * THE trap: while decoupled, RM_ID reads the decoupler's DECOUPLED_VALUE
     * (0x0). Under the "0 == greybox" software convention that is a real design.
     * It is not: it is a clamp. */
    seed_rm_decoupled();
    CHECK(clcd_rm_live(&id) == CLCD_RM_DECOUPLED);
    CHECK(id == 0u);                                /* the clamp, as read */
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    row9 = grid + 9u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "RP DECOUPLED", 12) == 0);
    CHECK(strstr(row2, "greybox") == NULL);         /* <-- THE ASSERTION */
    CHECK(strstr(row9, "greybox") == NULL);
    CHECK(strstr(row2, "v0.0")    == NULL);         /* no version for a clamp */
    CHECK(strncmp(row9 + 6, CLCD_RM_CAPS_UNKNOWN, strlen(CLCD_RM_CAPS_UNKNOWN)) == 0);

    /* --- (e) RP IN RESET -> its own honest state ------------------------- */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    DFXCTL_STATUS_RP_IN_RESET);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     0x01000003u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u);
    CHECK(clcd_rm_live(&id) == CLCD_RM_RP_RESET);
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "RP IN RESET", 11) == 0);
    CHECK(strstr(row2, "nanosoc") == NULL);

    /* --- (d) an UNKNOWN design id still renders RAW, from the live CSR ---- */
    seed_rm_live(0x0100BEEFu);
    clcd_test_render(grid, inv);
    row2 = grid + 2u * CLCD_COLS;
    row9 = grid + 9u * CLCD_COLS;
    CHECK(strncmp(row2 + CLCD_RM_NAME_COL, "rm?0100BEEF", 11) == 0);
    CHECK(strncmp(row2 + CLCD_RM_VER_COL,  "v1.0", 4) == 0);   /* qualified: real */
    CHECK(strncmp(row9 + 6, "design unknown", 14) == 0);
    /* A qualified id we cannot NAME is still an id we can SHOW: raw hex, not a
     * transient state, and not the nearest plausible name. */
    CHECK(strstr(row2, "RM ID NOT VALID") == NULL);

    /* --- (f) a reconfiguration landing inside the read sandwich ----------
     * RM_ID moves between clcd_rm_live()'s two reads while rm_id_valid still
     * reads 1 (a stale qualifier). Displaying either id as settled would be a
     * guess, so we report SETTLING and let the next refresh resolve it. */
    dfxrace.status    = 0u;
    dfxrace.rm_status = DFXCTL_RM_STATUS_RM_ID_VALID;
    dfxrace.ids[0] = 0x01000001u;   /* first  RM_ID read: nanosoc   */
    dfxrace.ids[1] = 0x01000003u;   /* second RM_ID read: multicore */
    dfxrace.ids[2] = dfxrace.ids[3] = 0x01000003u;
    dfxrace.n = 0;
    mock_regs_set_hook(MPS3_DFXCTL_BASE, dfx_race_hook, 0);
    CHECK(clcd_rm_live(&id) == CLCD_RM_SETTLING);
    CHECK(dfxrace.n == 2);          /* RM_ID really was read on both sides */

    /* Once it holds still, the very same hook yields LIVE. */
    dfxrace.ids[0] = dfxrace.ids[1] = 0x01000003u;
    dfxrace.n = 0;
    CHECK(clcd_rm_live(&id) == CLCD_RM_LIVE);
    CHECK(id == 0x01000003u);
    mock_regs_set_hook(MPS3_DFXCTL_BASE, 0, 0);
}

static void test_link_and_net(void)
{
    int s100, fd;
    clcd_link_speed_duplex((1u << 8), &s100, &fd); CHECK(s100 == 1 && fd == 1);
    clcd_link_speed_duplex((1u << 7), &s100, &fd); CHECK(s100 == 1 && fd == 0);
    clcd_link_speed_duplex((1u << 6), &s100, &fd); CHECK(s100 == 0 && fd == 1);
    clcd_link_speed_duplex((1u << 5), &s100, &fd); CHECK(s100 == 0 && fd == 0);
    clcd_link_speed_duplex(0x0000,    &s100, &fd); CHECK(s100 == 0 && fd == 0);

    char b[48];
    s_link_up = 1; s_anlpar = (1u << 8);
    clcd_fmt_net(b, sizeof(b));
    CHECK(strcmp(b, "192.168.10.101  UP 100/FD") == 0);

    s_link_up = 1; s_anlpar = (1u << 5);   /* 10BASE-T half duplex */
    clcd_fmt_net(b, sizeof(b));
    CHECK(strcmp(b, "192.168.10.101  UP 10/HD") == 0);

    s_link_up = 0;
    clcd_fmt_net(b, sizeof(b));
    CHECK(strcmp(b, "192.168.10.101  DOWN") == 0);
}

/* Board name: clcd_init() seeds the MPS3_BOARD_NAME build-time default ("MPS3-01",
 * this board's fpgahub node), and the host-push seam still overrides it. Both are
 * observed where they matter -- rendered into row 0 of the frame. */
static void test_board_name_default_and_hook(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    const unsigned dlen = (unsigned)strlen(MPS3_BOARD_NAME);

    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();

    /* The default lands top-left on row 0, and nothing bleeds past it (row 0's
     * other field, "nanoSoC harness", starts at col 20). */
    CHECK(dlen > 0 && dlen < 20u);
    clcd_test_render(grid, inv);
    CHECK(strncmp(grid, MPS3_BOARD_NAME, dlen) == 0);
    CHECK(grid[dlen] == ' ');
    CHECK(strncmp(grid + 20, "nanoSoC harness", 15) == 0);
    /* The rename is the point: the old IP-derived fallback must be gone. */
    CHECK(strncmp(grid, "MPS3-101", 8) != 0);

    /* The host-push seam still wins over the build-time default. */
    clcd_set_board_name("LAB-BENCH-2");
    clcd_test_render(grid, inv);
    CHECK(strncmp(grid, "LAB-BENCH-2", 11) == 0);
    CHECK(grid[11] == ' ');
    /* The setter forces a refresh; drive one reformat+render and confirm no
     * loss/hang (behavioural: the name change dirties row 0). */
    for (int i = 0; i < 4000; i++) {
        poll_step(5);
        CHECK(clcd_test_bytes_last_pass() <= CLCD_BYTES_PER_PASS);
        if (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0 &&
            clcd_test_bytes_total() > 0)
            break;
    }
    CHECK(fifo.dropped == 0);
}

/* ==========================================================================
 * 6. Panel orientation: the MADCTL (0x16) datum the table actually ships.
 *    The 180-degree rotation is a PANEL scan-direction change -- it is invisible
 *    to the shadow buffer and to every render assertion above, so the byte in
 *    the table is the only thing testable off-target. Pin it, and pin the
 *    invariants that make the driver's un-mirrored GRAM window math correct:
 *    MV must be preserved (still landscape, so the col-end 0x013F / row-end
 *    0x00EF window in the table stays right) and only MY|MX may differ from the
 *    vendored ST value.
 * ========================================================================== */
static void test_madctl_orientation(void)
{
    int found = 0;
    uint8_t madctl = 0;

    /* Scan for the 0x16 register index (an HX_CMD entry -- data bytes carry
     * HX_DAT, so there is no aliasing) and take the HX_DAT that follows it. */
    for (unsigned i = 0; i + 1u < hx8347_init_len; i++) {
        if (hx8347_init[i].op == HX_CMD && hx8347_init[i].val == HX_REG_MADCTL) {
            CHECK(hx8347_init[i + 1u].op == HX_DAT);
            madctl = hx8347_init[i + 1u].val;
            found++;
        }
    }
    CHECK(found == 1);                       /* written exactly once */
    CHECK(madctl == HX_MADCTL_VALUE);        /* the table ships what the seam says */

    /* Only the two flip bits may differ from ST's landscape value: MV, ML, BGR,
     * MH are untouched, so orientation cannot silently change colour order. */
    CHECK(((madctl ^ HX_MADCTL_LANDSCAPE) & (uint8_t)~(HX_MADCTL_MY | HX_MADCTL_MX)) == 0);
    CHECK((madctl & HX_MADCTL_MV) == HX_MADCTL_MV);   /* still landscape */

#if CLCD_ROTATE_180
    CHECK(madctl == 0x20u);                  /* 0xE0 ^ (MY|MX): rotated landscape */
    CHECK((madctl & HX_MADCTL_MY) == 0);
    CHECK((madctl & HX_MADCTL_MX) == 0);
#else
    CHECK(madctl == 0xE0u);                  /* as vendored from ST */
#endif
}

/* Swap-count edge detection: valid 0->1 with ok increments once and only once. */
static void test_swap_count_edges(void)
{
    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();

    /* Bring it to IDLE (init + first render done). */
    for (int i = 0; i < 20000; i++) {
        poll_step(5);
        if (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0)
            break;
    }
    CHECK(clcd_test_swap_count() == 0);      /* no completed swap yet */

    /* A successful swap completes (latched valid+ok). One refresh sees the
     * 0->1 edge and counts it exactly once. */
    s_swap_res.valid = true; s_swap_res.ok = true; s_swap_res.verified = true;
    s_swap_res.rm_id = 0x2u;
    for (int i = 0; i < 400; i++) poll_step(5);   /* several refresh windows */
    CHECK(clcd_test_swap_count() == 1);

    /* Result stays latched -> no further increments. */
    for (int i = 0; i < 400; i++) poll_step(5);
    CHECK(clcd_test_swap_count() == 1);

    /* A new swap starts (valid clears) then completes ok -> a second edge. */
    s_swap_res.valid = false;
    for (int i = 0; i < 200; i++) poll_step(5);
    s_swap_res.valid = true; s_swap_res.ok = true;
    for (int i = 0; i < 400; i++) poll_step(5);
    CHECK(clcd_test_swap_count() == 2);
}

/* Copy one grid row into a NUL-terminated buffer (grid rows are not terminated,
 * so strstr across a row would run into the next). */
static void row_str(const char *grid, unsigned r, char out[CLCD_COLS + 1])
{
    memcpy(out, grid + r * CLCD_COLS, CLCD_COLS);
    out[CLCD_COLS] = '\0';
}

/* ==========================================================================
 * Apps/Ports page (Phase 1): the per-RM service set is a FACT table, keyed on
 * the design half like clcd_rm_name/_caps.
 * ========================================================================== */
static void test_rm_services(void)
{
    const uint32_t SHELL = CLCD_SVC_CTRL | CLCD_SVC_TFTP | CLCD_SVC_PUSH | CLCD_SVC_XVC;

    /* Shell-side services exist for EVERY design (they are the static shell's,
     * not the DUT's) -- sweep the whole 16-bit design space to prove it. */
    for (unsigned d = 0; d <= 0xFFFFu; d++)
        CHECK((clcd_rm_services(0x01000000u | d) & SHELL) == SHELL);

    /* greybox / eth_ss: shell-only (no CPU, no console). */
    CHECK(clcd_rm_services(0x00000000u) == SHELL);
    CHECK(clcd_rm_services(0x01000002u) == SHELL);

    /* nanosoc: + SWD/JTAG/UART0/SWO, but NOT UART1 (one UART). */
    uint32_t n = clcd_rm_services(0x01000001u);
    CHECK((n & (CLCD_SVC_SWD | CLCD_SVC_JTAG | CLCD_SVC_UART0 | CLCD_SVC_SWO)) ==
          (CLCD_SVC_SWD | CLCD_SVC_JTAG | CLCD_SVC_UART0 | CLCD_SVC_SWO));
    CHECK(!(n & CLCD_SVC_UART1));

    /* multicore: everything, incl. UART1 (two UARTs). */
    uint32_t m = clcd_rm_services(0x01000003u);
    const uint32_t ALL = SHELL | CLCD_SVC_SWD | CLCD_SVC_JTAG | CLCD_SVC_UART0 |
                         CLCD_SVC_UART1 | CLCD_SVC_SWO;
    CHECK((m & ALL) == ALL);

    /* uart_echo: a console UART, but no CPU (no SWD/JTAG). */
    uint32_t e = clcd_rm_services(0x01000004u);
    CHECK(e & CLCD_SVC_UART0);
    CHECK(!(e & (CLCD_SVC_SWD | CLCD_SVC_JTAG)));

    /* Keyed on the DESIGN half: the version bits must not change the set. */
    CHECK(clcd_rm_services(0xFFFF0003u) == clcd_rm_services(0x00000003u));
}

static void test_apps_page_rendered(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    char    row[CLCD_COLS + 1];

    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();

    clcd_test_set_page(CLCD_PAGE_APPS);
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);

    /* --- nanosoc: 8 services, no UART1 --------------------------------- */
    seed_rm_live(0x01000001u);
    clcd_test_render(grid, inv);
    row_str(grid, 0, row);  CHECK(strncmp(row, "APPS & PORTS", 12) == 0);
                            CHECK(strncmp(row + 13, "nanosoc", 7) == 0);
    row_str(grid, 2, row);  CHECK(strcmp(row, "CTRL  nc 192.168.10.101 6900            ") == 0);
    row_str(grid, 7, row);  CHECK(strncmp(row, "JTAG  ocd rbb 192.168.10.101:6921", 33) == 0);
    row_str(grid, 8, row);  CHECK(strncmp(row, "UART0 nc 192.168.10.101 6930", 28) == 0);
    row_str(grid, 9, row);  CHECK(strncmp(row, "SWO   nc 192.168.10.101 6932", 28) == 0);
    /* nav bar: whole-row inverse "button". */
    CHECK(inv[14] == 1);
    row_str(grid, 14, row); CHECK(strncmp(row, "PB1 tap:next page", 17) == 0);
    /* UART1 (6931) must NOT appear for single-UART nanosoc. */
    for (unsigned r = 2; r <= 12; r++) { row_str(grid, r, row); CHECK(strstr(row, "6931") == NULL); }

    /* --- multicore: UART1 present ------------------------------------- */
    seed_rm_live(0x01000003u);
    clcd_test_render(grid, inv);
    row_str(grid, 0, row);  CHECK(strncmp(row + 13, "nanosoc_multicore", 17) == 0);
    int saw_uart1 = 0;
    for (unsigned r = 2; r <= 12; r++) {
        row_str(grid, r, row);
        if (strstr(row, "nc 192.168.10.101 6931")) saw_uart1 = 1;
    }
    CHECK(saw_uart1);

    /* --- eth_ss: shell-only, no console/debug ------------------------- */
    seed_rm_live(0x01000002u);
    clcd_test_render(grid, inv);
    row_str(grid, 0, row);  CHECK(strncmp(row + 13, "eth_ss", 6) == 0);
    row_str(grid, 2, row);  CHECK(strstr(row, "6900") != NULL);   /* control still shown */
    for (unsigned r = 2; r <= 12; r++) {
        row_str(grid, r, row);
        CHECK(strstr(row, "6930") == NULL);   /* no UART0 */
        CHECK(strstr(row, "6921") == NULL);   /* no JTAG  */
    }

    /* --- unknown/transient RM still shows the shell services ---------- */
    seed_rm_not_valid(0x01000003u);   /* rm_id_valid clear */
    clcd_test_render(grid, inv);
    row_str(grid, 0, row);  CHECK(strncmp(row + 13, "RM ID NOT VALID", 15) == 0);
    row_str(grid, 2, row);  CHECK(strstr(row, "6900") != NULL);   /* CTRL present */
    for (unsigned r = 2; r <= 12; r++) {  /* but no DUT console/debug */
        row_str(grid, r, row);
        CHECK(strstr(row, "6930") == NULL);
        CHECK(strstr(row, "6921") == NULL);
    }

    /* --- no rendered row overruns the 40-col frame (widest cmd ends at col 33;
     *     rule rows 1/13 are full '-') ---------------------------------- */
    seed_rm_live(0x01000003u);
    clcd_test_render(grid, inv);
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        if (r == 1u || r == 13u) continue;             /* rules are full-width */
        CHECK(grid[r * CLCD_COLS + (CLCD_COLS - 1u)] == ' ');
    }

    clcd_test_set_page(CLCD_PAGE_STATUS);              /* restore */
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);
}

/* clcd_hittest(): the bottom nav row is a "next page" button; elsewhere misses. */
static void test_hittest(void)
{
    /* Bottom row (y in [14*16 .. 15*16-1]) anywhere across the width -> NEXT. */
    CHECK(clcd_hittest(0, 14u * CLCD_GLYPH_H) == CLCD_ACT_NEXT_PAGE);
    CHECK(clcd_hittest(300u, 14u * CLCD_GLYPH_H + 8u) == CLCD_ACT_NEXT_PAGE);
    /* Any other row misses. */
    CHECK(clcd_hittest(0, 0) == CLCD_ACT_NONE);
    CHECK(clcd_hittest(100u, 5u * CLCD_GLYPH_H) == CLCD_ACT_NONE);
}

/* ==========================================================================
 * Phase-3 DUT-IP display: clcd_rm_has_eth() (which RMs carry a MAC) and
 * clcd_fmt_dut_ip() (the GENCHK-sourced dotted quad), both keyed/read exactly
 * like the surrounding rm_id-v2 lookups and the clcd_fmt_net() cap idiom.
 * ========================================================================== */
static void test_rm_has_eth(void)
{
    /* Only the two ethernet RMs, eth_ss (0x0002) + nanosoc_multicore (0x0003). */
    CHECK(clcd_rm_has_eth(0x00000002u) == 1);
    CHECK(clcd_rm_has_eth(0x00000003u) == 1);

    /* Everything else -- incl. single-core nanosoc (has a CPU but NO MAC). */
    CHECK(clcd_rm_has_eth(0x00000000u) == 0);   /* greybox   */
    CHECK(clcd_rm_has_eth(0x00000001u) == 0);   /* nanosoc   */
    CHECK(clcd_rm_has_eth(0x00000004u) == 0);   /* uart_echo */
    CHECK(clcd_rm_has_eth(0x0000001Eu) == 0);   /* led       */
    CHECK(clcd_rm_has_eth(0x00001234u) == 0);   /* unknown   */

    /* Keyed on the DESIGN half: the version bits must not change the verdict. */
    CHECK(clcd_rm_has_eth(0xFFFF0003u) == clcd_rm_has_eth(0x00000003u));
    CHECK(clcd_rm_has_eth(0x01000002u) == 1);
    CHECK(clcd_rm_has_eth(0x0A0F0001u) == 0);
}

static void test_fmt_dut_ip(void)
{
    char b[32];

    mock_regs_reset();

    /* IP_SEEN clear -> "--", whatever is left in DUT_IP. */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, 0u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP,     0xC0A80A6Eu);
    clcd_fmt_dut_ip(b, sizeof(b));
    CHECK(strcmp(b, "--") == 0);

    /* IP_SEEN set -> the dotted quad, MSB octet first (bits[31:24]). Pins the
     * network byte order: 0xC0A80A6E -> 192.168.10.110. */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, GENCHK_DUT_STATUS_IP_SEEN);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP,     0xC0A80A6Eu);
    clcd_fmt_dut_ip(b, sizeof(b));
    CHECK(strcmp(b, "192.168.10.110") == 0);

    /* A different value + the ethertype packed into the high half of STATUS: the
     * ethertype bits must not disturb IP_SEEN or the quad. */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS,
                   GENCHK_DUT_STATUS_IP_SEEN |
                   (0x0800u << GENCHK_DUT_STATUS_ETYPE_SHIFT));
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP, 0x0A000001u);
    clcd_fmt_dut_ip(b, sizeof(b));
    CHECK(strcmp(b, "10.0.0.1") == 0);
}

/* Rendered: row 10 shows "DIP : <ip>" ONLY for a known, healthy, ethernet RM,
 * and yields those rows to the error banner. */
static void test_dut_ip_row_rendered(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    char    row[CLCD_COLS + 1];

    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();

    /* --- multicore (an ethernet RM) + a seen IP: row 10 shows DIP -------- */
    seed_rm_live(0x01000003u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, GENCHK_DUT_STATUS_IP_SEEN);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP,     0xC0A80A6Eu);
    clcd_test_render(grid, inv);
    row_str(grid, 10, row);
    CHECK(strncmp(row, "DIP : 192.168.10.110", 20) == 0);
    CHECK(inv[10] == 0);              /* NOT inverted -- inversion is the banner */

    /* DISTINCT from row 5's NET (the harness's OWN management IP). */
    row_str(grid, 5, row);
    CHECK(strncmp(row, "NET : 192.168.10.101", 20) == 0);

    /* --- eth_ss HAS ethernet but no IP stack: the row shows, reading "--" -- */
    seed_rm_live(0x01000002u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, 0u);   /* nothing seen */
    clcd_test_render(grid, inv);
    row_str(grid, 10, row);
    CHECK(strncmp(row, "DIP : --", 8) == 0);
    CHECK(inv[10] == 0);

    /* --- single-core nanosoc has NO MAC: row 10 has no DIP at all -------- */
    seed_rm_live(0x01000001u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, GENCHK_DUT_STATUS_IP_SEEN);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP,     0xC0A80A6Eu);
    clcd_test_render(grid, inv);
    row_str(grid, 10, row);
    CHECK(strstr(row, "DIP") == NULL);
    CHECK(inv[10] == 0);              /* healthy non-eth -> row 10 blank + plain */
    for (unsigned c = 0; c < CLCD_COLS; c++)
        CHECK(grid[10u * CLCD_COLS + c] == ' ');

    /* --- an UNKNOWN/transient RM: no DIP (rm_known is false) even with an IP
     *     latched -- we must not describe an id we could not qualify. -------- */
    seed_rm_not_valid(0x01000003u);   /* multicore on the bus, but not qualified */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, GENCHK_DUT_STATUS_IP_SEEN);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP,     0xC0A80A6Eu);
    clcd_test_render(grid, inv);
    row_str(grid, 10, row);
    CHECK(strstr(row, "DIP") == NULL);

    /* --- the banner WINS: force link down on an ethernet RM with an IP ----- */
    seed_rm_live(0x01000003u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, GENCHK_DUT_STATUS_IP_SEEN);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP,     0xC0A80A6Eu);
    s_link_up = 0;                    /* -> "NETWORK LINK DOWN" banner */
    clcd_test_render(grid, inv);
    CHECK(inv[10] == 1 && inv[11] == 1 && inv[12] == 1);   /* banner owns 10-12 */
    row_str(grid, 10, row);
    CHECK(strstr(row, "DIP") == NULL);                     /* DIP yielded */
    row_str(grid, 11, row);
    CHECK(strstr(row, "NETWORK LINK DOWN") != NULL);
    s_link_up = 1;                    /* restore */
}

/* ==========================================================================
 * D13: a build WITHOUT the overlay store. This binary defines neither
 * overlay_store_usd_text() nor overlay_store_usd_change_count(), so clcd.c's
 * WEAK fallbacks are what link -- exactly the image that has no store. The glass
 * must say "USD : no hw" on row 4 right, leave "SID : ..." alone, and raise no
 * banner. (Every other USD state string is render-tested in test_clcd_usd.c,
 * which supplies a strong provider.) Row 12 is the v0.14 engine row: blank in
 * the bare-metal build, "SYS : ..." in the TEST_CLCD_ENGINE build -- either
 * way never inverted, so the no-banner check is on the inversion + rows 10-11.
 * ========================================================================== */
static void test_usd_weak_fallback_no_hw(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    char    row[CLCD_COLS + 1];

    CHECK(strcmp(overlay_store_usd_text(), "no hw") == 0);   /* the weak one */
    CHECK(overlay_store_usd_change_count() == 0u);

    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();
    clcd_test_render(grid, inv);

    row_str(grid, CLCD_USD_ROW, row);
    CHECK(strcmp(row, "SID : 0xAAF21306  USD : no hw           ") == 0);
    for (unsigned r = 10; r <= 12; r++) {                     /* no banner */
        CHECK(inv[r] == 0);
    }
    for (unsigned r = 10; r <= 11; r++) {
        for (unsigned c = 0; c < CLCD_COLS; c++)
            CHECK(grid[r * CLCD_COLS + c] == ' ');
    }
}

/* ---- the engine row (row 12; clcd.h "THE ENGINE ROW") ---------------------- */
static void test_fmt_engine(void)
{
    char b[64];
    mps3_clcd_engine_t e;
    memset(&e, 0, sizeof(e));
    e.impl = "linux";
    clcd_fmt_engine(b, sizeof(b), &e);
    CHECK(strcmp(b, "linux  ssh unclaimed") == 0);
    e.claimed = 1;
    clcd_fmt_engine(b, sizeof(b), &e);
    CHECK(strcmp(b, "linux  ssh claimed") == 0);          /* no fingerprint known */
    snprintf(e.fpr, sizeof(e.fpr), "AbCdEfGhIjK");         /* clipped to 8 */
    clcd_fmt_engine(b, sizeof(b), &e);
    CHECK(strcmp(b, "linux  ssh claimed SHA256:AbCdEfGh") == 0);
    CHECK(strlen("SYS : ") + strlen(b) == CLCD_COLS);      /* exactly the row */
    e.impl = "baremetal";                                  /* clipped to 6 */
    clcd_fmt_engine(b, sizeof(b), &e);
    CHECK(strcmp(b, "bareme ssh claimed SHA256:AbCdEfGh") == 0);
    CHECK(strlen("SYS : ") + strlen(b) <= CLCD_COLS);
    clcd_fmt_engine(b, 8, &e);                             /* cap-safe */
    CHECK(strlen(b) == 7);
}

static void test_engine_row_rendered(void)
{
    char    grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    char    row[CLCD_COLS + 1];

    mock_regs_reset();
    seed_healthy();
    fifo_setup(FIFO_NEVER_FULL, 0, 0);
    clcd_init();
    seed_rm_live(0x01000001u);        /* a healthy non-ethernet RM: rows 10-12 free */
#ifndef TEST_CLCD_ENGINE
    /* THE BARE-METAL FRAME: no provider, so row 12 is exactly what it always was. */
    clcd_test_render(grid, inv);
    row_str(grid, CLCD_ENGINE_ROW, row);
    CHECK(strspn(row, " ") == CLCD_COLS);
    CHECK(inv[CLCD_ENGINE_ROW] == 0);
#else
    s_eng_on = 1;
    memset(&s_eng, 0, sizeof(s_eng));
    s_eng.impl = "linux";
    clcd_test_render(grid, inv);
    row_str(grid, CLCD_ENGINE_ROW, row);
    CHECK(strncmp(row, "SYS : linux  ssh unclaimed", 26) == 0);
    CHECK(inv[CLCD_ENGINE_ROW] == 0);

    s_eng.claimed = 1;
    snprintf(s_eng.fpr, sizeof(s_eng.fpr), "s6n1vtZI");
    clcd_test_render(grid, inv);
    row_str(grid, CLCD_ENGINE_ROW, row);
    CHECK(strcmp(row, "SYS : linux  ssh claimed SHA256:s6n1vtZI") == 0);   /* all 40 */

    /* the rows it shares with: D13's row 4 right half and row 10's DIP are
     * untouched by it, and it YIELDS to the banner */
    seed_rm_live(0x01000003u);        /* an ethernet RM: DIP on row 10 */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS, GENCHK_DUT_STATUS_IP_SEEN);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_DUT_IP, 0xC0A80A6Eu);
    clcd_test_render(grid, inv);
    row_str(grid, 10, row);
    CHECK(strncmp(row, "DIP : 192.168.10.110", 20) == 0);
    row_str(grid, CLCD_USD_ROW, row);                    /* D13 row 4, cols 18-39 */
    CHECK(strcmp(row + 18, "USD : no hw           ") == 0);
    row_str(grid, CLCD_ENGINE_ROW, row);
    CHECK(strncmp(row, "SYS : linux", 11) == 0);
    s_link_up = 0;                    /* the banner */
    clcd_test_render(grid, inv);
    CHECK(inv[10] == 1 && inv[11] == 1 && inv[12] == 1);
    row_str(grid, CLCD_ENGINE_ROW, row);
    CHECK(strstr(row, "SYS") == NULL);
    s_link_up = 1;
    s_eng_on = 0;
#endif
}

int main(void)
{
    test_fmt_engine();
    test_engine_row_rendered();
    test_byte_bound_never_drains();
    test_byte_bound_with_room();
    test_slow_drain_no_loss();
    test_diff_cells();
    test_fmt_uptime();
    test_fmt_hex32();
    test_rm_name();
    test_fmt_rm_version();
    test_row2_fields_cannot_overflow();
    test_row2_rendered_long_name_and_version();
    test_rm_classify();
    test_rm_src_text_bounds();
    test_rm_live_source();
    test_link_and_net();
    test_board_name_default_and_hook();
    test_madctl_orientation();
    test_swap_count_edges();
    test_rm_services();
    test_apps_page_rendered();
    test_hittest();
    test_rm_has_eth();
    test_fmt_dut_ip();
    test_dut_ip_row_rendered();
    test_usd_weak_fallback_no_hw();
#ifdef TEST_CLCD_ENGINE
    printf("test_clcd_engine: %d checks passed\n", s_checks);
#else
    printf("test_clcd: %d checks passed\n", s_checks);
#endif
    return 0;
}
