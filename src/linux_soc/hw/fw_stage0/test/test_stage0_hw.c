/*
 * test_stage0_hw.c -- stage0's DDR gate and watchdog control (stage0_hw.c)
 * against behavioural fakes of TELEM and the AXI Timebase WDT installed as
 * mock_regs hooks (-DMPS3_HAL_MOCK), so every register write is pinned.
 *
 *   1 calibrated: CTRL is written exactly 0x2 (alarm_en; `enable` 0), STATUS[0] read
 *   2 WATCHDOG WARM RESTART: TELEM was reset (alarm_en cleared); the next stage0
 *     entry must write CTRL again or it would read "not calibrated" on good DDR
 *   3 never calibrates: gives up after the timeout, bounded, polling meanwhile
 *   4 arm FRESH: stop (TWCSR0 = WDS, TWCSR1 = 0), then TWCSR1 = EWDT2 and
 *     TWCSR0 = EWDT1|WDS, nothing else; the WDT is then running
 *   5 kick against the HDL-faithful WDT (s0_models.h: TWCSR1 READS 0, TWCSR0[0]
 *     mirrors EWDT2): a stopped WDT is never written; a running one gets
 *     (EWDT1 as read)|WDS and never WRS; the PRE-FIX kick (tested TWCSR1 &
 *     EWDT2) is the negative control -- it never writes
 *   6 S0_WDOG_ARM defaults to 1 (SHELL_CONTRACT v0.4: C_WDT_INTERVAL=31)
 *  10 calib HOLD (S0-COLDFIX): a drop inside the hold window restarts it
 *  11 a bit that flaps faster than the hold never passes: bounded, 0
 *  12 hold 0 = the old first-1 rule
 *  13 cold settle: waits its ms, polls, counts calib drops, TELEM only
 *  14 WDOG armed at entry + the poll hook's kick through a 60 s wait: no
 *     reset; with the pre-fix kick the model resets it at 42.95 s
 *  15 the hand-off re-arm gives a FRESH 42.95 s even on a running WDT; the
 *     pre-fix arm (no stop) leaves the old window (negative control)
 *  16 WRS clear: one TWCSR0 write of WRS alone
 *   7 usd_spi hand-over (D13 handover §12.2): ONE CTRL write leaving only the
 *     card-detect bits -- EN=0, CS deasserted, WIDE=0, CD_POL / CD_IGNORE kept
 *   8 no usd_spi (ID != "USD1": an older shell's page): read the ID, NEVER write
 *   9 the card-detect knobs default to the MPS3's wiring (active low, pin used)
 */
#include "s0_testutil.h"
#include "../stage0_hw.h"
#include "platform_regs.h"
#include "usd_regs.h"
#include "mock_regs.h"
#include "s0_models.h"

_Static_assert(S0_WDOG_ARM == 1, "the MBV stage0 must arm the watchdog at hand-off by default");
_Static_assert(MPS3_USD_CD_POL == 0 && MPS3_USD_CD_IGNORE == 0 && S0_USD_CD_CTRL == 0u,
               "the uSD card detect defaults to active low (AT15's pull-up), pin used");

static unsigned g_hooks;
static int g_kick_mode;     /* 0 none, 1 s0_hw_wdog_kick (stage0.c's hook), 2 pre-fix kick */

/* The kick as it was before S0-COLDFIX: it tested TWCSR1 & EWDT2, which the
 * vendor core reads as 0 -- so it never wrote. Kept as the negative control. */
static void prefix_kick(void)
{
    uint32_t c0 = mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);
    if ((c0 & WDOG_TWCSR0_EWDT1) &&
        (mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR1) & WDOG_TWCSR1_EWDT2))
        mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_EWDT1 | WDOG_TWCSR0_WDS);
}

/* The arm as it was: no stop first. Negative control for test 15. */
static void prefix_arm(void)
{
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR1, WDOG_TWCSR1_EWDT2);
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_EWDT1 | WDOG_TWCSR0_WDS);
}

void s0_poll_hook(void)
{
    g_hooks++;
    if (g_kick_mode == 1)
        s0_hw_wdog_kick();
    else if (g_kick_mode == 2)
        prefix_kick();
}

/* TELEM and the WDT: s0_models.h (tm, wdt). */
#define t_ctrl        tm.ctrl
#define t_ctrl_writes tm.ctrl_writes

/* every WDT write, in order, on top of the model */
static struct { uint32_t off, val; } w_log[16];
static unsigned w_n;
static int wdog_log_hook(void *c, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    if (is_write) {
        if (w_n < 16) {
            w_log[w_n].off = off;
            w_log[w_n].val = *val;
        }
        w_n++;
    }
    return wdt_hook(c, is_write, base, off, val);
}

/* ---- usd_spi fake: ID + CTRL, every access logged ---------------------------------- */
static uint32_t u_id, u_ctrl, u_reads, u_writes;
static struct { uint32_t off, val; } u_log[8];
static int usd_hook(void *c, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)c; (void)base;
    if (is_write) {
        if (u_writes < 8) {
            u_log[u_writes].off = off;
            u_log[u_writes].val = *val;
        }
        u_writes++;
        if (off == USD_CTRL)
            u_ctrl = *val & 0x1Fu;
        return 1;
    }
    u_reads++;
    *val = off == USD_ID ? u_id : off == USD_CTRL ? u_ctrl : 0u;
    return 1;
}

static void fresh(void)
{
    mock_regs_reset();
    mock_regs_set_hook(MPS3_TELEM_BASE, telem_hook, NULL);
    mock_regs_set_hook(MPS3_WDOG_BASE, wdog_log_hook, NULL);
    mock_regs_set_hook(MPS3_USD_BASE, usd_hook, NULL);
    mock_regs_set_us_per_read(50u);
    u_id = USD_ID_VALUE;
    u_ctrl = u_reads = u_writes = 0;
    telem_model_reset();                         /* calibrated from t=0 */
    memset(&wdt, 0, sizeof wdt);
    wdt_model_reset();
    w_n = 0;
    g_hooks = 0;
    g_kick_mode = 0;
}

/* spin the clock (register reads) for ms, calling the poll hook like a loop */
static void spin_ms(uint32_t ms)
{
    uint32_t t0 = mock_time_now_ms();
    while (mock_time_now_ms() - t0 < ms && !wdt.reset_pending) {
        (void)mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TBR);
        s0_poll_hook();
    }
}

int main(void)
{
    printf("test 1: calibrated DDR: CTRL = 0x2, STATUS[0] read\n");
    fresh();
    CHECK(s0_hw_ddr_calib(2000u, 0u, NULL) == 1, "calibrated");
    CHECK(t_ctrl_writes == 1u && t_ctrl == 0x2u, "CTRL written 0x%X x%u", t_ctrl, t_ctrl_writes);

    printf("test 2: watchdog warm restart cleared TELEM CTRL: stage0 writes it again\n");
    telem_model_warm_reset();                    /* peripheral_aresetn */
    CHECK(s0_hw_ddr_calib(2000u, S0_CALIB_HOLD_MS, NULL) == 1, "still sees calibrated DDR on the next entry");
    CHECK(t_ctrl_writes == 2u && t_ctrl == 0x2u, "CTRL rewritten on every entry (%u)", t_ctrl_writes);

    printf("test 3: DDR never calibrates: bounded failure (timeout + hold)\n");
    fresh();
    tm.never = 1u;
    uint32_t t0 = mock_time_now_ms(), drops = 99u;
    CHECK(s0_hw_ddr_calib(2000u, 1000u, &drops) == 0, "not calibrated");
    uint32_t el = mock_time_now_ms() - t0;
    CHECK(el >= 3000u && el < 3100u, "gave up after %u ms", el);
    CHECK(g_hooks > 0u && drops == 0u, "kept polling (watchdog/heartbeat hook) while waiting; drops %u", drops);

    printf("test 4: arm FRESH = TWCSR0 WDS, TWCSR1 0, TWCSR1 EWDT2, TWCSR0 EWDT1|WDS\n");
    fresh();
    s0_hw_wdog_arm();
    CHECK(w_n == 4u, "%u writes", w_n);
    CHECK(w_log[0].off == WDOG_TWCSR0 && w_log[0].val == WDOG_TWCSR0_WDS, "stop half 1: TWCSR0=0x%X", w_log[0].val);
    CHECK(w_log[1].off == WDOG_TWCSR1 && w_log[1].val == 0u, "stop half 2: TWCSR1=0x%X", w_log[1].val);
    CHECK(w_log[2].off == WDOG_TWCSR1 && w_log[2].val == WDOG_TWCSR1_EWDT2, "TWCSR1=0x%X", w_log[2].val);
    CHECK(w_log[3].off == WDOG_TWCSR0 && w_log[3].val == (WDOG_TWCSR0_EWDT1 | WDOG_TWCSR0_WDS),
          "then TWCSR0=0x%X", w_log[3].val);
    CHECK(wdt.ewdt1 && wdt.ewdt2 && wdt_running(), "running");

    printf("test 5: kick vs the vendor core: TWCSR1 reads 0, TWCSR0[0] mirrors EWDT2\n");
    fresh();
    wdt.wrs = 1u;                                /* stopped, after a watchdog reset */
    s0_hw_wdog_kick();
    CHECK(w_n == 0u, "a stopped watchdog is not written (%u)", w_n);
    CHECK(s0_hw_wdog_status() & WDOG_TWCSR0_WRS, "reset cause readable");
    CHECK(mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR1) == 0u, "the model: TWCSR1 reads 0");
    s0_hw_wdog_arm();
    w_n = 0;
    wdt.wds = 1u;                                /* a first expiry pending */
    prefix_kick();
    CHECK(w_n == 0u && wdt.wds == 1u, "NEGATIVE CONTROL: the pre-fix kick never writes (%u)", w_n);
    s0_hw_wdog_kick();
    CHECK(w_n == 1u && w_log[0].off == WDOG_TWCSR0 &&
          w_log[0].val == (WDOG_TWCSR0_EWDT1 | WDOG_TWCSR0_WDS), "kick 0x%X", w_log[0].val);
    CHECK(wdt.wrs && !wdt.wds, "WDS cleared, WRS kept");
    w_n = 0;
    wdt.ewdt1 = 0u;                              /* EWDT2 alone still RUNS the core */
    wdt.wds = 1u;
    s0_hw_wdog_kick();
    CHECK(w_n == 1u && w_log[0].val == WDOG_TWCSR0_WDS && !wdt.ewdt1 && !wdt.wds,
          "EWDT2 alone: kicked, EWDT1 not set by the kick (0x%X)", w_log[0].val);

    printf("test 7: usd_spi hand-over: one CTRL write, EN/CS/WIDE off, CD bits kept\n");
    static const uint32_t cds[] = { 0u, USD_CTRL_CD_POL, USD_CTRL_CD_IGNORE,
                                    USD_CTRL_CD_POL | USD_CTRL_CD_IGNORE };
    for (unsigned i = 0; i < 4u; i++) {
        fresh();
        u_ctrl = USD_CTRL_EN | USD_CTRL_CS | USD_CTRL_WIDE | cds[i];   /* mid-use, as left */
        s0_hw_usd_release();
        CHECK(u_writes == 1u && u_log[0].off == USD_CTRL && u_log[0].val == cds[i],
              "CD 0x%X: %u writes, first CTRL=0x%X", cds[i], u_writes, u_log[0].val);
        CHECK(!(u_ctrl & (USD_CTRL_EN | USD_CTRL_CS | USD_CTRL_WIDE)) &&
              (u_ctrl & USD_CTRL_CD_MASK) == cds[i], "CTRL now 0x%X", u_ctrl);
    }
    fresh();
    u_ctrl = 0;                                   /* already released: still one clean write */
    s0_hw_usd_release();
    CHECK(u_writes == 1u && u_ctrl == 0u, "idle block: CTRL 0x%X (%u writes)", u_ctrl, u_writes);

    printf("test 8: no usd_spi block: the page is read, never written\n");
    fresh();
    u_id = 0x12345678u;                            /* e.g. axi_quad_spi_0 on an older shell */
    u_ctrl = USD_CTRL_EN;
    s0_hw_usd_release();
    CHECK(u_writes == 0u && u_reads >= 1u, "%u writes, %u reads", u_writes, u_reads);

    printf("test 10: calib hold: a drop inside the hold window restarts it\n");
    fresh();
    tm.up_ms = 200u;                             /* MIG calibrates at 200 ms */
    telem_glitch(700u, 750u);                    /* then drops for 50 ms, inside the hold */
    t0 = mock_time_now_ms();
    drops = 0u;
    CHECK(s0_hw_ddr_calib(2000u, 1000u, &drops) == 1, "holds in the end");
    el = mock_time_now_ms() - t0;
    CHECK(el >= 1750u && el < 1800u, "passed at %u ms: 750 + a full 1000 ms hold", el);
    CHECK(drops == 1u, "the drop was counted (%u)", drops);
    fresh();
    tm.up_ms = 200u;                             /* the same bit, first-1 rule: 200 ms */
    t0 = mock_time_now_ms();
    CHECK(s0_hw_ddr_calib(2000u, 0u, NULL) == 1 && mock_time_now_ms() - t0 < 250u,
          "test 12: hold 0 is the old first-1 gate (%u ms)", mock_time_now_ms() - t0);

    printf("test 11: a bit flapping faster than the hold never passes (bounded)\n");
    fresh();
    for (uint32_t k = 0; k < 8u; k++)
        telem_glitch(400u + k * 600u, 450u + k * 600u);   /* up 550 ms, down 50 ms */
    t0 = mock_time_now_ms();
    drops = 0u;
    CHECK(s0_hw_ddr_calib(2000u, 1000u, &drops) == 0, "never held 1000 ms");
    el = mock_time_now_ms() - t0;
    CHECK(el >= 3000u && el < 3100u && drops >= 4u, "gave up at %u ms after %u drops", el, drops);

    printf("test 13: cold settle: waits, polls, counts calib drops, TELEM only\n");
    fresh();
    tm.up_ms = 800u;
    telem_glitch(3000u, 3050u);
    telem_glitch(6000u, 6200u);
    t0 = mock_time_now_ms();
    uint32_t cal = 99u;
    drops = s0_hw_settle(10000u, &cal);
    el = mock_time_now_ms() - t0;
    CHECK(el >= 10000u && el < 10010u, "settled %u ms", el);
    CHECK(drops == 2u, "two drops during the settle (%u)", drops);
    CHECK(cal == S0_SETTLE_CAL_ROSE, "calib down at the start, rose inside (cal %u)", cal);
    fresh();                                     /* calibrated before the settle began */
    drops = s0_hw_settle(3000u, &cal);
    CHECK(drops == 0u && cal == S0_SETTLE_CAL_AT_START, "up at the start, never moved (cal %u)", cal);
    CHECK(g_hooks > 1000u && tm.ctrl == TELEM_CTRL_ALARM_EN, "polled (%u), alarm_en written", g_hooks);
    CHECK(u_reads == 0u && u_writes == 0u, "no uSD access while settling");

    printf("test 14: WDOG armed at entry, kicked by the poll hook through a 60 s wait\n");
    fresh();
    tm.never = 1u;
    g_kick_mode = 1;
    s0_hw_wdog_arm();
    CHECK(s0_hw_ddr_calib(58000u, 1000u, NULL) == 0, "a 59 s calib wait");
    spin_ms(2000u);
    CHECK(wdt.resets == 0u && wdt.kicks > 1000u, "no reset in %u ms, %u kicks",
          mock_time_now_ms(), wdt.kicks);
    fresh();
    tm.never = 1u;
    g_kick_mode = 2;                             /* the pre-fix kick */
    s0_hw_wdog_arm();
    uint32_t t_arm = mock_time_now_us();
    (void)s0_hw_ddr_calib(58000u, 1000u, NULL);
    CHECK(wdt.resets == 1u, "NEGATIVE CONTROL: the pre-fix kick lets it fire (%u)", wdt.resets);
    CHECK(wdt.reset_at_us - t_arm >= 2u * WDT_PERIOD_US - 1000u &&
          wdt.reset_at_us - t_arm <= 2u * WDT_PERIOD_US + 1000u,
          "reset %u us after the arm (42.95 s)", wdt.reset_at_us - t_arm);

    printf("test 15: the hand-off re-arm restarts the window on a RUNNING watchdog\n");
    fresh();
    g_kick_mode = 1;
    s0_hw_wdog_arm();                            /* entry */
    spin_ms(30000u);                             /* 30 s of stage0, kicking */
    g_kick_mode = 0;                             /* Linux: nothing kicks yet */
    s0_hw_wdog_arm();                            /* the hand-off */
    uint32_t t_ho = mock_time_now_us();
    spin_ms(60000u);
    CHECK(wdt.resets == 1u && wdt.reset_at_us - t_ho >= 2u * WDT_PERIOD_US - 1000u,
          "Linux got %u us (>= 42.95 s)", wdt.reset_at_us - t_ho);
    fresh();
    g_kick_mode = 1;
    s0_hw_wdog_arm();
    spin_ms(30000u);
    g_kick_mode = 0;
    prefix_arm();                                /* no stop: the timebase keeps running */
    t_ho = mock_time_now_us();
    spin_ms(60000u);
    CHECK(wdt.resets == 1u && wdt.reset_at_us - t_ho < 2u * WDT_PERIOD_US - 5000000u,
          "NEGATIVE CONTROL: without the stop Linux gets only %u us", wdt.reset_at_us - t_ho);

    printf("test 16: WRS clear = one TWCSR0 write of WRS alone\n");
    fresh();
    wdt.wrs = 1u;
    s0_hw_wdog_clear_wrs();
    CHECK(w_n == 1u && w_log[0].off == WDOG_TWCSR0 && w_log[0].val == WDOG_TWCSR0_WRS && !wdt.wrs,
          "%u writes, 0x%X", w_n, w_log[0].val);

    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 DDR gate (hold) + settle + watchdog + usd_spi hand-over %s\n",
           g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
