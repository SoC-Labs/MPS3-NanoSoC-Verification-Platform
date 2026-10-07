/*
 * test_stage0_entry.c -- the COLD-BOOT path end to end on the host (lane
 * S0-COLDFIX, 2026-09-28): the REAL stage0_flow.c + stage0_core.c +
 * stage0_hw.c over mock_regs, with the two shell blocks as behavioural models
 * (s0_models.h):
 *   - the AXI Timebase WDT exactly as the vendor HDL builds it (TWCSR1 reads 0,
 *     runs on either enable, timebase zeroed only from fully stopped, two
 *     expiries to a reset, WRS survives the reset);
 *   - TELEM whose calib bit follows a schedule after the cold entry: 0 while the
 *     MIG calibrates, then glitches where the MCC reprograms OSCCLKs.
 * The card is in memory; each 8-block read op costs 300 ms of mock time (the
 * 3.125 MHz data SCK), so a slot load outlasts the whole 42.95 s watchdog
 * window and only real kicks keep the board alive. A DDR STALL freezes the
 * "hart" (no more register accesses, no kicks) until the WDT model resets it.
 *
 * The entry sequence and the four ops below MIRROR stage0.c's main() and
 * op_ddr_calib / op_settle / op_guard / op_now_ms line for line (stage0.c is
 * the MMIO glue and is not host-built); everything they call is the real code.
 *
 *   A cold entry: the watchdog is armed first, the settle absorbs the MCC
 *     window (its calib drops are counted), the hold passes, the 45 s load is
 *     kicked throughout (no reset), the hand-off re-arms a fresh window
 *   B a glitch just after the settle restarts the hold (ddr_ok_ms moves)
 *   C negative control: the pre-fix kick (TWCSR1 & EWDT2) -> the model resets
 *     stage0 in the middle of the load
 *   D a DDR stall: the watchdog resets 21.5-42.9 s after the last kick; the
 *     next entry is S0_EK_WDOG (run 1, WRS cleared, no settle, prev_* = where
 *     it hung); the second stall -> straight to rescue, card untouched
 *   E PB0 after that: warm, run 0, boots without a settle
 *   F a Linux-era watchdog (nothing kicks after the hand-off): 42.95 s after
 *     the hand-off, S0_EK_WDOG_LINUX, not counted, WRS left for harnessd
 */
#include <setjmp.h>

#include "s0_testutil.h"
#include "../stage0_boot.h"
#include "../stage0_flow.h"
#include "../stage0_hw.h"
#include "platform_regs.h"
#include "mock_regs.h"
#include "s0_models.h"

#define DDR_BASE   0x80000000u
#define DDR_SPAN   (2u << 20)
#define LBA_A      64u
#define LBA_B      4160u
#define SLOT_N     4096u
#define CARD_N     (LBA_B + SLOT_N)
#define PAY_LEN    600000u               /* ~1172 blocks: ~147 read ops */
#define READ_MS    300u                  /* one 8-block op at 3.125 MHz, with overhead */

static uint8_t *g_card, *g_ddr;
static uint8_t  g_img[1u << 20];
static uint8_t  g_pay[PAY_LEN];
static struct s0_status ST;
static const struct s0_build_ids IDS = { 0xC01DF1C5u, 0x44EE76D5u, 0x01000000u };

static uint32_t g_entry_ms;
static int      g_prefix_kick;
static int      g_in_run;
static jmp_buf  g_reset;
static uint32_t g_reads, g_first_read_ms;
static uint32_t g_stall_lba;             /* 0 = off */
static uint32_t g_freeze_us;

/* ---- the platform, as stage0.c has it ---------------------------------------------- */

static void check_reset(void)
{
    wdt_eval();
    if (wdt.reset_pending && g_in_run)
        longjmp(g_reset, 1);
}

static void prefix_kick(void)
{
    uint32_t c0 = mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);
    if ((c0 & WDOG_TWCSR0_EWDT1) &&
        (mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR1) & WDOG_TWCSR1_EWDT2))
        mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_EWDT1 | WDOG_TWCSR0_WDS);
}

void s0_poll_hook(void)                  /* stage0.c: kick, heartbeat, uptime */
{
    if (g_prefix_kick)
        prefix_kick();
    else
        s0_hw_wdog_kick();
    ST.heartbeat += 1u;
    ST.uptime_ms = mock_time_now_ms() - g_entry_ms;
    check_reset();
}

static int op_ddr(void *c)               /* stage0.c op_ddr_calib */
{
    (void)c;
    uint32_t drops = 0u;
    int ok = s0_hw_ddr_calib(S0_CALIB_TIMEOUT_MS, S0_CALIB_HOLD_MS, &drops);
    s0_status_note_calib_drops(&ST, drops);
    return ok ? S0_DDR_OK : S0_DDR_FAIL;
}

static void op_settle(void *c, uint32_t ms)   /* stage0.c op_settle */
{
    (void)c;
    uint32_t cal = 0u;
    uint32_t drops = s0_hw_settle(ms, &cal);
    s0_status_note_settle(&ST, drops, cal);
}

static int op_guard(void *c)             /* stage0.c op_guard */
{
    (void)c;
    s0_poll_hook();
    return s0_hw_calib_bit();
}

static uint32_t op_now(void *c) { (void)c; return mock_time_now_ms() - g_entry_ms; }

static int op_sd_init(void *c, uint32_t *blocks, uint32_t *detail)
{
    (void)c;
    mock_time_advance_ms(300u);          /* SETTLE + ACMD41 */
    *blocks = CARD_N;
    *detail = 0x00030000u;
    return S0_SD_READY;
}

static int op_sd_read(void *c, uint32_t lba, uint32_t n, void *dst)
{
    (void)c;
    if (!g_reads++)
        g_first_read_ms = mock_time_now_ms() - g_entry_ms;
    if (g_stall_lba && lba >= g_stall_lba && lba < LBA_A + SLOT_N) {
        /* the hart is frozen on a DDR write: no access, no kick, until the
         * watchdog resets the board */
        g_freeze_us = mock_time_now_us();
        for (int i = 0; i < 1200 && !wdt.reset_pending; i++) {
            mock_time_advance_ms(100u);
            wdt_eval();
        }
        CHECK(wdt.reset_pending, "the watchdog reset a frozen hart");
        longjmp(g_reset, 1);
    }
    mock_time_advance_ms(READ_MS);
    check_reset();
    if (lba >= CARD_N || n > CARD_N - lba)
        return -1;
    memcpy(dst, g_card + (size_t)lba * 512u, (size_t)n * 512u);
    return 0;
}

static void *op_a2p(uint32_t a, uint32_t len, void *c)
{
    (void)c;
    return s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len) ? g_ddr + (a - DDR_BASE) : NULL;
}

static const struct s0_flow_ops OPS = { op_ddr, op_sd_init, op_sd_read, op_a2p, NULL, NULL,
                                        op_settle, op_guard, op_now };

/* ---- the board -------------------------------------------------------------------------- */

static void install(void)
{
    mock_regs_set_hook(MPS3_TELEM_BASE, telem_hook, NULL);
    mock_regs_set_hook(MPS3_WDOG_BASE, wdt_hook, NULL);
    mock_regs_set_us_per_read(50u);
}

/* MCC power-up: the FPGA configures (BRAM outside the image and every
 * register come up zero), the MIG calibrates `up_ms` later. */
static void power_cycle(uint32_t up_ms)
{
    mock_regs_reset();
    install();
    memset(&ST, 0, sizeof ST);
    memset(&wdt, 0, sizeof wdt);
    wdt_model_reset();
    telem_model_reset();
    tm.up_ms = up_ms;
}

/* PB0: the CPU, the peripherals (WDT, TELEM) reset; BRAM and WRS survive. */
static void press_pb0(void)
{
    wdt_model_reset();
    telem_model_warm_reset();
}

/* stage0.c main()'s entry sequence, then the boot order and the hand-off.
 * Returns the source, S0_FROM_NONE (rescue) or -1 (the watchdog reset it). */
static int stage0(uint32_t *kind)
{
    static volatile int from;
    struct s0_result res;
    int ok;
    g_entry_ms = mock_time_now_ms();
    g_reads = 0u;
    g_first_read_ms = 0u;
    wdt.reset_pending = 0u;
    uint32_t cause = s0_hw_wdog_status();
    *kind = s0_status_open(&ST, &IDS, cause, S0_BOOT_LIMIT);
    if (*kind == S0_EK_WDOG)
        s0_hw_wdog_clear_wrs();
    s0_hw_wdog_arm();
    g_in_run = 1;
    if (setjmp(g_reset)) {
        g_in_run = 0;
        telem_model_warm_reset();        /* the reset cleared alarm_en */
        return -1;
    }
    from = s0_boot_select(&ST, &OPS, &res, &ok);
    if (from != S0_FROM_NONE) {
        ST.subphase = S0_SUBPHASE(S0_SP_HANDOFF, 0u);
        s0_status_note_handoff(&ST, from, &res, mock_time_now_ms() - g_entry_ms);
        s0_hw_wdog_arm();
    }
    g_in_run = 0;
    return from;
}

static void make_card(void)
{
    memset(g_card, 0, (size_t)CARD_N * 512u);
    struct tu_part p[4] = { { 0x7F, LBA_A, SLOT_N }, { 0x7F, LBA_B, SLOT_N }, { 0, 0, 0 }, { 0, 0, 0 } };
    tu_build_mbr(g_card, p);
    tu_fill(g_pay, PAY_LEN, 42u);
    struct tu_region r = { g_pay, PAY_LEN, DDR_BASE };
    uint32_t n = tu_build_image(g_img, sizeof g_img, &r, 1, DDR_BASE, 0, 0);
    memcpy(g_card + (size_t)LBA_A * 512u, g_img, n);
    memcpy(g_card + (size_t)LBA_B * 512u, g_img, n);
}

/* ---- scenarios -------------------------------------------------------------------------- */

static void t_cold(void)
{
    uint32_t k;
    printf("A: cold entry: arm, settle through the MCC window, hold, a 45 s load kept alive\n");
    power_cycle(800u);
    telem_glitch(3000u, 3050u);          /* OSCCLK steps under a calibrated MIG */
    telem_glitch(6000u, 6200u);
    int from = stage0(&k);
    uint32_t t_ho = mock_time_now_ms() - g_entry_ms;
    CHECK(from == S0_FROM_A && k == S0_EK_COLD, "booted A (from %d, kind %u)", from, k);
    CHECK(wdt.resets == 0u && wdt.kicks > 100u, "no watchdog reset; %u kicks", wdt.kicks);
    CHECK(t_ho > 50000u, "entry -> hand-off %u ms: longer than the 42.95 s window", t_ho);
    CHECK(g_first_read_ms >= S0_COLD_SETTLE_MS, "first card read at %u ms: after the settle",
          g_first_read_ms);
    CHECK(S0_ENTRY_CALIB_DROPS(ST.entry) == 2u, "the settle saw the 2 MCC glitches (0x%X)", ST.entry);
    CHECK(!(ST.entry & S0_ENTRY_CAL_AT_SETTLE) && (ST.entry & S0_ENTRY_CAL_ROSE),
          "calib came up (and re-came up) inside the settle: entry [28]=0 [29]=1");
    CHECK(ST.ddr_ok_ms >= S0_COLD_SETTLE_MS + S0_CALIB_HOLD_MS &&
          ST.ddr_ok_ms < S0_COLD_SETTLE_MS + S0_CALIB_HOLD_MS + 100u, "ddr_ok_ms %u", ST.ddr_ok_ms);
    CHECK(memcmp(g_ddr, g_pay, PAY_LEN) == 0, "payload in DDR");
    CHECK(ST.phase == S0_PH_HANDOFF && (ST.subphase >> 24) == S0_SP_HANDOFF &&
          !(ST.entry & S0_ENTRY_SETTLE_PENDING) && S0_ENTRY_WDOG_RUN(ST.entry) == 0u,
          "status: handoff, settle done, run 0");
    CHECK(wdt_running() && wdt.k_done == 0u && mock_time_now_us() - wdt.tb0_us < 100000u,
          "the hand-off re-armed a FRESH window (timebase %u us)", mock_time_now_us() - wdt.tb0_us);

    printf("B: a glitch just after the settle restarts the hold\n");
    power_cycle(800u);
    telem_glitch(10200u, 10300u);
    from = stage0(&k);
    CHECK(from == S0_FROM_A && ST.ddr_ok_ms >= 10300u + S0_CALIB_HOLD_MS,
          "ddr_ok_ms %u >= glitch end + a full hold", ST.ddr_ok_ms);
    CHECK(S0_ENTRY_CALIB_DROPS(ST.entry) == 1u, "one drop (0x%X)", ST.entry);
    CHECK(wdt.resets == 0u, "no reset");

    printf("B2: calib already up at the settle and never disturbed: the stale-cal signature\n");
    power_cycle(0u);
    from = stage0(&k);
    CHECK(from == S0_FROM_A && (ST.entry & S0_ENTRY_CAL_AT_SETTLE) && !(ST.entry & S0_ENTRY_CAL_ROSE) &&
          S0_ENTRY_CALIB_DROPS(ST.entry) == 0u, "entry [28]=1 [29]=0, 0 drops (0x%X)", ST.entry);

    printf("C: NEGATIVE CONTROL: the pre-fix kick lets the armed watchdog reset the load\n");
    power_cycle(800u);
    g_prefix_kick = 1;
    from = stage0(&k);
    g_prefix_kick = 0;
    CHECK(from == -1 && wdt.resets == 1u && ST.phase == S0_PH_SLOT_A,
          "reset mid-load (from %d, resets %u, phase %u)", from, wdt.resets, ST.phase);
}

static void t_stall(void)
{
    uint32_t k;
    printf("D: DDR stall -> watchdog -> S0_EK_WDOG run 1 -> stall -> run 2 -> rescue\n");
    power_cycle(800u);
    g_stall_lba = LBA_A + 200u;
    int from = stage0(&k);
    CHECK(from == -1 && k == S0_EK_COLD && wdt.resets == 1u, "cold boot froze and was reset");
    uint32_t since_kick = wdt.reset_at_us - wdt.last_kick_us;
    CHECK(wdt.last_kick_us <= g_freeze_us && since_kick > WDT_PERIOD_US && since_kick <= 2u * WDT_PERIOD_US,
          "reset %u us after the last kick (21.5-42.9 s)", since_kick);
    uint32_t hung_at = ST.subphase;
    CHECK((hung_at >> 24) == S0_SP_SLOT_LOAD && (hung_at & 0xFFFFFFu) >= 200u,
          "the block says slot load, block %u", hung_at & 0xFFFFFFu);

    from = stage0(&k);                   /* the watchdog restart */
    CHECK(from == -1 && k == S0_EK_WDOG && S0_ENTRY_WDOG_RUN(ST.entry) == 1u,
          "entry: WATCHDOG before a hand-off, run 1 (kind %u)", k);
    CHECK(ST.reset_cause & S0_RESET_WRS, "reset_cause 0x%X has WRS", ST.reset_cause);
    CHECK(ST.prev_phase == S0_PREV(S0_PH_SLOT_A, hung_at) && ST.prev_uptime_ms > 0u,
          "prev_phase: slot A, slot-load block %u (0x%X)", S0_PREV_DETAIL(ST.prev_phase), ST.prev_phase);
    CHECK(g_first_read_ms < S0_COLD_SETTLE_MS, "no settle on the watchdog entry (%u ms)", g_first_read_ms);

    from = stage0(&k);                   /* the second restart */
    CHECK(from == S0_FROM_NONE && k == S0_EK_WDOG && S0_ENTRY_WDOG_RUN(ST.entry) == 2u,
          "run 2: straight to rescue (from %d)", from);
    CHECK(ST.rescue_reason == S0_RR_WDOG && g_reads == 0u && ST.sd_result == S0_SD_SKIPPED,
          "reason watchdog loop, card untouched (%u reads)", g_reads);
    CHECK(ST.ddr_calib == S0_DDR_OK, "the DDR gate ran: a push may be staged");
    CHECK(wdt.wrs == 0u, "stage0 cleared the WRS it counted");

    printf("E: PB0 from rescue: warm, run 0, the normal path, no settle\n");
    g_stall_lba = 0u;
    press_pb0();
    from = stage0(&k);
    CHECK(from == S0_FROM_A && k == S0_EK_WARM && S0_ENTRY_WDOG_RUN(ST.entry) == 0u,
          "booted A (from %d, kind %u)", from, k);
    CHECK(g_first_read_ms < 2000u && ST.ddr_ok_ms < 1100u, "fast: first read %u ms, ddr_ok %u ms",
          g_first_read_ms, ST.ddr_ok_ms);

    printf("F: nothing kicks after the hand-off: a Linux-era watchdog, not counted\n");
    uint32_t t0 = mock_time_now_us();
    for (int i = 0; i < 600 && !wdt.reset_pending; i++) {
        mock_time_advance_ms(100u);
        wdt_eval();
    }
    CHECK(wdt.reset_pending && wdt.reset_at_us - t0 >= 2u * WDT_PERIOD_US - 100000u,
          "reset %u us after the hand-off (42.95 s)", wdt.reset_at_us - t0);
    telem_model_warm_reset();
    from = stage0(&k);
    CHECK(k == S0_EK_WDOG_LINUX && S0_ENTRY_WDOG_RUN(ST.entry) == 0u && ST.fails_a == 1u,
          "kind %u, run 0, the attempt judged unconfirmed", k);
    CHECK(wdt.wrs == 1u, "WRS left set for harnessd");
    CHECK(from == S0_FROM_A, "and it boots again");
}

int main(void)
{
    g_card = calloc(CARD_N, 512u);
    g_ddr = calloc(1, DDR_SPAN);
    if (!g_card || !g_ddr)
        return 2;
    make_card();
    t_cold();
    t_stall();
    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 cold-boot entry path %s\n", g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
