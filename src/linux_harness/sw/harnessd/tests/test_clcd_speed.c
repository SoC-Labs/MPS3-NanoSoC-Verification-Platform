/*
 * test_clcd_speed.c -- lane CLCD-SPEED: how long a panel repaint takes under
 * mps3-harnessd on the 100 MHz MicroBlaze V, and what it costs the superloop.
 *
 * WHY THIS EXISTS. rc2_v6 carried the CLCD drain (20d4ea2) on a host model that
 * charged ~1 us per CLCD byte and predicted a page change in 114 ms; on silicon
 * the panel still drew glyph by glyph (david, 2026-09-28). The model was wrong
 * about the per-byte cost, by ~5x: under Linux every byte paid an uncached
 * CLCD_STATUS read, the register HAL's call chain twice (mps3_reg_* ->
 * hal_backend_* -> find()) and the LCD mirror's per-byte tap + GRAM decode --
 * ~245 rv32 instructions and ~31 stores a byte, and the MBV's D-cache is
 * WRITE-THROUGH (src/linux_soc/fw_memtest/memtest.c), so every stack spill is a
 * DDR write. This test runs the REAL code over a backend that charges MBV costs
 * to a modelled clock:
 *
 *   clcd.c + clcd_kvm.c + the init table   the renderer, the KVM owner rule, the
 *                                           bus seam's call sites
 *   hal_front.c                             the strong clcd_bus_push (or, with
 *                                           -DHARNESSD_CLCD_PUSH_PER_BYTE, the
 *                                           weak per-byte default: the negative
 *                                           control), the work counter, the taps
 *   lcdmirror_model.c behind the taps       every byte still reaches the mirror:
 *                                           the model's frame must equal the font
 *                                           oracle of clcd.c's own shadow
 *   firmware/common/service.c               the superloop: budgets, the sick
 *                                           policy, THE KICK, the 6900 interleave
 *   harnessd.h                              the drain budgets main_linux.c uses
 *
 * THE COST TABLE (per operation, ns at 100 MHz) -- see cost_t below. The
 * instruction and store counts per byte were read off the rv32 -O2 build of
 * this tree (riscv32-amd-linux-gnu-objdump of build/rv32/mps3-harnessd):
 *   OLD  (weak default, per byte): render loop + try_push + build_cell 30 insn
 *        / 4 stores; mps3_reg_read32 + hal_backend_read32 + find + tap(read)
 *        68 / 8 + 1 AXI read; mps3_reg_write32 + hal_backend_write32 + find
 *        81 / 11 + 1 AXI write; tap() + lcdm_model_byte (+ put_pixel) 66 / 8.
 *        = 245 insn, 31 stores, 1 AXI read, 1 AXI write per byte.
 *   NEW  (strong clcd_bus_push + the bulk tap): the store loop 7 insn + 1 AXI
 *        write; memchr ~1.5; pixel_run ~16.5 insn / ~0.63 stores; build_cell 8 /
 *        2. = ~33 insn, ~2.6 stores, 1 AXI write per byte + one STATUS read (the
 *        HAL chain + 1 AXI read) per <= 128 bytes.
 *   Per clcd_poll(): 3 clock syscalls (clcd_poll, pb_service, the drain) + the
 *        KVM STATUS/EVENT reads + ~250 insn. Per pass: service.c's two clock
 *        reads a row (13 rows) + the other rows' bodies and socket syscalls.
 * Three cost sets: CENTRAL (asserted against the target), OPTIMISTIC and
 * PESSIMISTIC (printed; the pessimistic one is held to a looser bound).
 *
 * WHAT IS ASSERTED (the fast build):
 *   1. the full-screen repaint (600 glyphs, 163,800 bytes) and the status->apps
 *      page change each finish within TARGET_MS (150) in the central model;
 *   2. the superloop during a full repaint: every pass kicks, no budget
 *      overrun, no service goes sick, the clcd row stays < its 30 ms budget,
 *      the 6900 row comes round within 20 ms, clcd_poll() (the touch sampler)
 *      runs at least every CLCD_TOUCH_PERIOD_MS;
 *   3. the FIFO never overflows -- also with a panel slower than the CPU;
 *   4. one STATUS read per <= 128 bytes; the bytes land in the right register;
 *   5. the mirror is pixel-exact after every repaint; the KVM owner rule holds
 *      (no byte while the DUT owns the panel; regain = a full repaint);
 * and the NEGATIVE CONTROL (-DHARNESSD_CLCD_PUSH_PER_BYTE, rc2_v6's 4 ms drain
 * and 256-byte polls): the same model must reproduce the silicon symptom -- a
 * page change well over 300 ms, a full screen well over 500 ms.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../../../../firmware/clcd/clcd.h"
#include "../../../../../firmware/clcd/font8x16.h"
#include "../../../../../firmware/common/diag.h"
#include "../../../../../firmware/common/platform_regs.h"
#include "../../../../../firmware/common/service.h"
#include "../../../../../firmware/common/timebase.h"
#include "../../../../../firmware/coordinator/coordinator.h"
#include "../../../../../firmware/coordinator/swap_fsm.h"
#include "../../../../../firmware/smsc911x/smsc911x.h"
#include "hal.h"
#include "harnessd.h"
#include "lcdmirror.h"

#ifndef TARGET_MS
#define TARGET_MS 150u
#endif
#define CLCD_ROW_BUDGET_US 30000u        /* main_linux.c s_services[9]      */
#define CTRL_GAP_MAX_US    20000u        /* the 6900 row must come round    */

static int s_checks, s_fails;
#define CHECK(c) do { s_checks++; if (!(c)) { s_fails++; \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } } while (0)

/* ==========================================================================
 * the cost model
 * ========================================================================== */
typedef struct {
    const char *name;
    double insn, store_x, mmio_rd, mmio_wr, syscall;   /* ns                    */
    double pass_other_us;                                /* other rows' bodies    */
} cost_t;

static const cost_t COSTS[3] = {
    /* CPI 1.3, +4 cycles a DDR store, 25-cycle AXI read, 8-cycle posted write,
     * 8 us a clock_gettime (trap + 31-register save to write-through DDR + the
     * AXI-timer clocksource read + a 64-bit divide), 1.3 ms of other rows. */
    { "central",     13.0, 40.0, 250.0,  80.0,  8000.0, 1300.0 },
    { "optimistic",  11.0, 20.0, 150.0,  40.0,  5000.0,  800.0 },
    { "pessimistic", 20.0, 100.0, 400.0, 200.0, 15000.0, 2800.0 },
};
static const cost_t *C = &COSTS[0];

/* per-path instruction / store counts (file header) */
#define FRONT_RD_INSN   68.0
#define FRONT_RD_ST      8.0
#define FRONT_WR_INSN   81.0
#define FRONT_WR_ST     11.0
#define TAP1_INSN       66.0
#define TAP1_ST          8.0
#define RENDER_OLD_INSN 30.0
#define RENDER_OLD_ST    4.0
#define BULK_INSN       33.0
#define BULK_ST          2.6
#define POLL_INSN      250.0

/* `insn` counts every instruction, stores included; `st` of them are DDR stores,
 * each costing store_x on top. */
static double sw(double insn, double st) { return insn * C->insn + st * C->store_x; }

/* ---- the modelled clock -------------------------------------------------- */
static double   s_ns;
static uint64_t s_sys, s_rd, s_wr, s_status_rd, s_polls;
static void charge(double ns) { s_ns += ns; }

uint32_t mps3_sys_now_us(void)
{
    charge(C->syscall);
    s_sys++;
    return (uint32_t)(uint64_t)(s_ns / 1000.0);
}

uint32_t mps3_sys_now_ms(void)
{
    charge(C->syscall);
    s_sys++;
    return (uint32_t)(uint64_t)(s_ns / 1e6);
}

/* ---- the 8080 engine's FIFO (clcd.sv: 128 deep, 110 ns a byte) ----------- */
static double   s_bus_ns = 110.0;
static double   s_fifo_t;                 /* model time the level was last settled */
static double   s_level;
static uint32_t s_fifo_max, s_overflow;
static uint64_t s_bus_bytes;

static void fifo_settle(void)
{
    double drained = (s_ns - s_fifo_t) / s_bus_ns;
    s_level = (drained >= s_level) ? 0.0 : s_level - drained;
    s_fifo_t = s_ns;
}

static void fifo_push(void)
{
    fifo_settle();
    /* a byte that ARRIVES when the FIFO is already full is DROPPED (clcd.sv) */
    if (s_level + 0.5 >= 128.0) {
        s_overflow++;
        return;
    }
    s_level += 1.0;
    if ((uint32_t)(s_level + 0.5) > s_fifo_max) s_fifo_max = (uint32_t)(s_level + 0.5);
    s_bus_bytes++;
}

/* ---- the fabric: CLCD + CLCDKVM pages, everything else reads 0 ----------- */
static uint32_t s_clcd_page[0x10000u / 4u];
static uint32_t s_kvm_status, s_kvm_event, s_kvm_ctrl;
static int      s_window_on = 1;
static double   s_last_poll_ns, s_poll_gap_max_ns;
static int      s_in_repaint;

uint32_t hal_backend_read32(uintptr_t base, uintptr_t off)
{
    charge(C->mmio_rd + sw(FRONT_RD_INSN, FRONT_RD_ST));
    s_rd++;
    if (base == MPS3_CLCD_BASE) {
        if (off == CLCD_STATUS) {
            s_status_rd++;
            fifo_settle();
            uint32_t lvl = (uint32_t)(s_level + 0.5);
            uint32_t st = (lvl << CLCD_STATUS_LEVEL_SHIFT) & CLCD_STATUS_LEVEL_MASK;
            if (lvl >= 128u) st |= CLCD_STATUS_FIFO_FULL;
            if (lvl == 0u) st |= CLCD_STATUS_FIFO_EMPTY;
            return st;
        }
        return s_clcd_page[off / 4u];
    }
    if (base == MPS3_CLCDKVM_BASE) {
        if (off == CLCDKVM_STATUS) {
            /* once per clcd_poll() (kvm_service): its software */
            charge(sw(POLL_INSN, 20.0));
            s_polls++;
            if (s_in_repaint && s_last_poll_ns > 0.0 && s_ns - s_last_poll_ns > s_poll_gap_max_ns)
                s_poll_gap_max_ns = s_ns - s_last_poll_ns;
            s_last_poll_ns = s_ns;
            return s_kvm_status;
        }
        if (off == CLCDKVM_EVENT) return s_kvm_event;
        if (off == CLCDKVM_CTRL)  return s_kvm_ctrl;
        return 0u;
    }
    if (base == MPS3_CLKRST_BASE && off == CLKRST_STATUS)
        return CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE;
    if (base == MPS3_DFXCTL_BASE && off == DFXCTL_RM_ID) return 1u;
    if (base == MPS3_DFXCTL_BASE && off == DFXCTL_RM_STATUS) return DFXCTL_RM_STATUS_RM_ID_VALID;
    return 0u;
}

void hal_backend_write32(uintptr_t base, uintptr_t off, uint32_t val)
{
    charge(C->mmio_wr + sw(FRONT_WR_INSN, FRONT_WR_ST));
    s_wr++;
    if (base == MPS3_CLCD_BASE) {
        if (off == CLCD_CMD || off == CLCD_DATA) fifo_push();
        if (off == CLCD_CTRL && (val & CLCD_CTRL_FIFO_RESET)) { s_level = 0.0; s_fifo_t = s_ns; }
        s_clcd_page[off / 4u] = val;
        return;
    }
    if (base == MPS3_CLCDKVM_BASE) {
        if (off == CLCDKVM_EVENT) s_kvm_event &= ~val;     /* W1C */
        if (off == CLCDKVM_CTRL)  s_kvm_ctrl = val;
    }
}

volatile uint32_t *hal_backend_window(uintptr_t phys, size_t len)
{
    if (s_window_on && phys == MPS3_CLCD_BASE && len <= sizeof(s_clcd_page))
        return (volatile uint32_t *)s_clcd_page;
    return 0;
}

/* ---- the mirror behind the taps (lcdmirror_tap.c's pads rule, trimmed) --- */
static uint32_t s_ap[LCDM_APERTURE / 4u] __attribute__((aligned(64)));
static lcdm_model_t M;
static uint32_t s_tap_clcd_ctrl, s_tap_kvm_ctrl;
static int      s_blind;
static uint64_t s_tapped;

static void pads(void)
{
    int kvm = (s_tap_kvm_ctrl & CLCDKVM_CTRL_BL_RST_SRC) != 0u;
    int held = kvm ? !(s_tap_kvm_ctrl & CLCDKVM_CTRL_PANEL_RST_N) : !(s_tap_clcd_ctrl & CLCD_CTRL_RESET_N);
    if (held != (int)M.in_reset) lcdm_model_set_reset(&M, held);
}

static void t_tap(uintptr_t base, uintptr_t off, uint32_t val, int is_write)
{
    if (base == MPS3_CLCD_BASE) {
        if (!is_write) return;
        if (off == CLCD_CMD || off == CLCD_DATA) {
            /* the OLD per-byte path: the tap + decode, and the render loop's share */
            charge(sw(TAP1_INSN + RENDER_OLD_INSN, TAP1_ST + RENDER_OLD_ST));
            s_tapped++;
            if (!s_blind) lcdm_model_byte(&M, off == CLCD_DATA, (uint8_t)val);
        } else if (off == CLCD_CTRL) {
            s_tap_clcd_ctrl = val;
            pads();
        }
        return;
    }
    if (is_write) {
        if (off == CLCDKVM_CTRL) { s_tap_kvm_ctrl = val; pads(); }
        return;
    }
    if (off == CLCDKVM_STATUS)
        s_blind = (val & (CLCDKVM_STATUS_OWNER | CLCDKVM_STATUS_KVM_DRIVES_PADS)) != 0u;
    else if (off == CLCDKVM_EVENT && (val & CLCDKVM_EVENT_REINIT_MASK))
        lcdm_model_pulse_reset(&M);
}

static void t_tap_run(const uint8_t *rs, const uint8_t *v, uint32_t n)
{
    /* the NEW per-byte path: the store loop's AXI write, memchr, pixel_run and
     * build_cell -- charged byte by byte so the FIFO drains as they go */
    for (uint32_t i = 0; i < n; i++) {
        charge(C->mmio_wr + sw(BULK_INSN, BULK_ST));
        fifo_push();
    }
    s_wr += n;
    s_tapped += n;
    if (!s_blind) lcdm_model_bytes(&M, rs, v, n);
}

/* ---- the symbols clcd.c links against ------------------------------------ */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
uint32_t swap_fsm_icap_bytes(void) { return 0u; }
int smsc911x_link_up(void) { return 1; }
int smsc911x_mii_read(uint32_t r, uint16_t *v) { if (v) *v = (r == 5u) ? 0x01E1u : 0u; return 0; }
void mps3_platform_mac(uint8_t mac[6]) { static const uint8_t m[6] = { 2, 0, 0, 0x4D, 0x50, 0x53 }; memcpy(mac, m, 6); }

/* ==========================================================================
 * the superloop: service.c with harnessd's rows that matter here
 * ========================================================================== */
static int      s_uart;                 /* a 6930-6932 client is connected       */
static uint64_t s_kicks;
static double   s_last_kick_ns, s_kick_gap_max_ns;
static double   s_last_ctrl_ns, s_ctrl_gap_max_ns;
static double   s_clcd_row_max_ns, s_pass_max_ns;

void mps3_wdt_kick(void)
{
    if (s_in_repaint && s_last_kick_ns > 0.0 && s_ns - s_last_kick_ns > s_kick_gap_max_ns)
        s_kick_gap_max_ns = s_ns - s_last_kick_ns;
    s_last_kick_ns = s_ns;
    s_kicks++;
}

static void svc_ctrl(void)
{
    if (s_in_repaint && s_last_ctrl_ns > 0.0 && s_ns - s_last_ctrl_ns > s_ctrl_gap_max_ns)
        s_ctrl_gap_max_ns = s_ns - s_last_ctrl_ns;
    s_last_ctrl_ns = s_ns;
}

static void svc_others(void) { charge(C->pass_other_us * 1000.0); }

static void svc_clcd(void)          /* main_linux.c svc_clcd, minus locate */
{
    double t0 = s_ns;
    (void)clcd_poll_drain(s_uart ? HARNESSD_CLCD_DRAIN_UART_US : HARNESSD_CLCD_DRAIN_US);
    lcdm_model_publish(&M);         /* harnessd_lcdmirror_poll()'s publish */
    if (s_in_repaint && s_ns - t0 > s_clcd_row_max_ns) s_clcd_row_max_ns = s_ns - t0;
}

/* The 13 rows collapse to: ctrl (vital, 10 ms), the others' cost (unbudgeted
 * here: its time is a constant), clcd (30 ms); ctrl interleaves after clcd. */
static const mps3_service_t ROWS[] = {
    { "ctrl",   svc_ctrl,   10000u },
    { "others", svc_others,     0u },
    { "clcd",   svc_clcd,   CLCD_ROW_BUDGET_US },
};

static uint64_t s_passes;
static void one_pass(void)
{
    double t0 = s_ns;
    mps3_service_run_pass();
    s_passes++;
    if (s_in_repaint && s_ns - t0 > s_pass_max_ns) s_pass_max_ns = s_ns - t0;
}

/* ==========================================================================
 * scenarios
 * ========================================================================== */
typedef struct {
    double   render_ms;                 /* first RENDER pass -> idle, nothing dirty */
    uint64_t passes, bytes, status_rd, wr, sys, polls;
} rep_t;

static void stats_zero(void)
{
    s_kick_gap_max_ns = s_ctrl_gap_max_ns = s_clcd_row_max_ns = s_pass_max_ns = 0.0;
    s_poll_gap_max_ns = 0.0;
    s_last_kick_ns = s_last_ctrl_ns = s_last_poll_ns = 0.0;
}

/* Run passes until a glyph render has started (the renderer seen in
 * ST_RENDER at the end of a pass: every repaint measured here spans several)
 * and finished (idle, nothing dirty). The time is from the START of the pass
 * the render began in -- the reset/init stream and its datasheet waits before
 * it are not the repaint, and are not counted. */
static rep_t run_repaint(unsigned max_passes)
{
    rep_t r = { 0, 0, 0, 0, 0, 0, 0 };
    uint64_t p0 = s_passes, b0 = s_tapped, st0 = s_status_rd, w0 = s_wr, y0 = s_sys, q0 = s_polls;
    double t_start = -1.0;
    stats_zero();
    for (unsigned i = 0; i < max_passes; i++) {
        double pt = s_ns;
        s_in_repaint = 1;
        uint64_t pb = s_tapped, ps = s_status_rd, pw = s_wr, py = s_sys, pq = s_polls, pp = s_passes;
        one_pass();
        if (t_start < 0.0) {
            if (clcd_test_state() != CLCD_ST_RENDER)
                continue;
            t_start = pt;                /* count from this pass: its bytes too */
            b0 = pb; st0 = ps; w0 = pw; y0 = py; q0 = pq; p0 = pp;
        }
        if (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0u)
            break;
    }
    s_in_repaint = 0;
    r.render_ms = (t_start >= 0.0) ? (s_ns - t_start) / 1e6 : -1.0;
    r.passes = s_passes - p0;
    r.bytes = s_tapped - b0;
    r.status_rd = s_status_rd - st0;
    r.wr = s_wr - w0;
    r.sys = s_sys - y0;
    r.polls = s_polls - q0;
    return r;
}

/* Idle the model for `ms` (the 250 ms refresh ticks paint only a cell or two). */
static void idle_ms(unsigned ms)
{
    double end = s_ns + ms * 1e6;
    while (s_ns < end) {
        one_pass();
        charge(2e6);                     /* the idle policy's poll() sleep */
    }
}

static void boot(void)
{
    memset(s_clcd_page, 0, sizeof(s_clcd_page));
    memset(s_ap, 0, sizeof(s_ap));
    lcdm_model_init(&M, s_ap, 0);
    s_tap_clcd_ctrl = s_tap_kvm_ctrl = 0;
    s_blind = 0;
    s_kvm_status = s_kvm_event = s_kvm_ctrl = 0;
    s_level = 0.0;
    s_fifo_t = s_ns;
    s_fifo_max = s_overflow = 0;
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    g_shell_state.static_id = 0x44EE76D5u;
    hal_set_tap(t_tap);
    hal_set_tap_bytes(t_tap_run);
    (void)mps3_service_install(ROWS, 3u);
    mps3_service_set_interleave(svc_ctrl, 1u << 2);
    mps3_service_set_vital_mask(1u << 0);
    clcd_init();
}

/* the font oracle of clcd.c's own shadow (test_lcdmirror.c's) */
static unsigned mirror_diff(void)
{
    char cells[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    clcd_test_shadow(cells, inv);
    lcdm_model_publish(&M);
    const uint16_t *fb = (const uint16_t *)((const uint8_t *)s_ap + LCDM_FB);
    unsigned d = 0;
    for (unsigned r = 0; r < CLCD_ROWS; r++)
        for (unsigned c = 0; c < CLCD_COLS; c++) {
            unsigned ch = (unsigned char)cells[r * CLCD_COLS + c];
            if (ch < CLCD_FONT_FIRST || ch > CLCD_FONT_LAST) ch = ' ';
            const uint8_t *g = font8x16[ch - CLCD_FONT_FIRST];
            for (unsigned yy = 0; yy < 16u; yy++)
                for (unsigned xx = 0; xx < 8u; xx++) {
                    uint16_t want = (g[yy] & (0x80u >> xx)) ? 0xFFFFu : (inv[r] ? 0xF800u : 0x0000u);
                    d += fb[(r * 16u + yy) * 320u + c * 8u + xx] != want;
                }
        }
    return d;
}

static void report(const char *what, rep_t r)
{
    printf("  %-11s %-26s %7.1f ms | %4llu passes | %6llu B | %5llu STATUS rd (1 per %5.1f B) | "
           "%6llu syscalls | %4llu clcd_polls\n", C->name, what, r.render_ms,
           (unsigned long long)r.passes, (unsigned long long)r.bytes,
           (unsigned long long)r.status_rd, r.status_rd ? (double)r.bytes / (double)r.status_rd : 0.0,
           (unsigned long long)r.sys, (unsigned long long)r.polls);
}

/* One full scenario set under the current cost table. */
typedef struct { rep_t full, page, regain; } set_t;

static set_t scenarios(int assert_superloop)
{
    set_t s;
    boot();
    s.full = run_repaint(20000u);                /* reset + init + all 600 glyphs */
    report("full screen (first draw)", s.full);
    CHECK(s.full.bytes >= 163800u);
    CHECK(mirror_diff() == 0u);                  /* every byte reached the mirror */
    if (assert_superloop) {
        uint32_t overruns = mps3_service_overrun_events(), skips = mps3_service_skip_events();
        printf("  %-11s superloop during it: worst pass %.1f ms, worst clcd row %.1f ms (budget %u), "
               "6900 row every <= %.1f ms, kick every <= %.1f ms, clcd_poll every <= %.2f ms; "
               "%u overruns, %u skips; FIFO peak %u/128, %u dropped\n", C->name,
               s_pass_max_ns / 1e6, s_clcd_row_max_ns / 1e6, CLCD_ROW_BUDGET_US / 1000u,
               s_ctrl_gap_max_ns / 1e6, s_kick_gap_max_ns / 1e6, s_poll_gap_max_ns / 1e6,
               overruns, skips, s_fifo_max, s_overflow);
        CHECK(overruns == 0u && skips == 0u);
        CHECK(mps3_service_skipped_mask() == 0u);
        CHECK(s_clcd_row_max_ns < CLCD_ROW_BUDGET_US * 1000.0);
        CHECK(s_ctrl_gap_max_ns <= CTRL_GAP_MAX_US * 1000.0);
        CHECK(s_kick_gap_max_ns <= CTRL_GAP_MAX_US * 1000.0);
        CHECK(s_poll_gap_max_ns <= CLCD_TOUCH_PERIOD_MS * 1e6);
        CHECK(s_overflow == 0u);
    }
    idle_ms(600);
    clcd_page_next();                            /* status -> apps */
    s.page = run_repaint(20000u);
    report("page change status->apps", s.page);
    CHECK(s.page.bytes > 40000u);
    CHECK(mirror_diff() == 0u);
    if (assert_superloop) {
        CHECK(mps3_service_overrun_events() == 0u);
        CHECK(s_clcd_row_max_ns < CLCD_ROW_BUDGET_US * 1000.0);
        CHECK(s_ctrl_gap_max_ns <= CTRL_GAP_MAX_US * 1000.0);
    }
    idle_ms(600);
    clcd_page_next();                            /* back to status */
    (void)run_repaint(20000u);

    /* THE KVM OWNER RULE: the DUT takes the panel mid-repaint -> not one more
     * byte; the panel back (harness_gained) -> the production full repaint. */
    idle_ms(600);
    clcd_page_next();
    one_pass();                                  /* the repaint is under way ... */
    s_kvm_status = CLCDKVM_STATUS_OWNER;         /* ... and the DUT owns the pads */
    one_pass();                                  /* kvm_service sees it          */
    uint64_t b_lost = s_tapped;
    for (int i = 0; i < 50; i++) one_pass();
    CHECK(s_tapped == b_lost);                   /* nothing while the DUT owns it */
    CHECK(clcd_test_relinquished() == 1);
    s_kvm_status = 0u;
    s_kvm_event = CLCDKVM_EVENT_HARNESS_GAINED;
    s.regain = run_repaint(40000u);              /* re-init + every glyph */
    report("regain (full repaint)", s.regain);
    CHECK(s.regain.bytes >= 163800u);
    CHECK(mirror_diff() == 0u);
    return s;
}

/* The bytes land in the register their rs names; FIFO full -> nothing. */
static void t_bus_push_unit(void)
{
#ifndef HARNESSD_CLCD_PUSH_PER_BYTE
    C = &COSTS[0];
    boot();
    s_level = 0.0;
    s_fifo_t = s_ns;
    const uint8_t rs[6] = { 0, 1, 1, 0, 1, 0 };
    const uint8_t v[6]  = { 0x22, 0xA1, 0xB2, 0x16, 0xC3, 0x2A };
    for (unsigned i = 0; i < 6u; i++) {
        s_clcd_page[CLCD_CMD / 4u] = s_clcd_page[CLCD_DATA / 4u] = 0xDEADu;
        CHECK(clcd_bus_push(&rs[i], &v[i], 1u) == 1u);
        CHECK(s_clcd_page[(rs[i] ? CLCD_DATA : CLCD_CMD) / 4u] == v[i]);
        CHECK(s_clcd_page[(rs[i] ? CLCD_CMD : CLCD_DATA) / 4u] == 0xDEADu);
    }
    /* full: returns 0, writes nothing */
    s_bus_ns = 1e12;                             /* the panel stops draining */
    s_level = 128.0;
    s_fifo_t = s_ns;
    uint64_t t0 = s_tapped;
    CHECK(clcd_bus_push(rs, v, 6u) == 0u);
    CHECK(s_tapped == t0);
    /* 120 queued: exactly 8 go, then the next STATUS says full */
    s_level = 120.0;
    uint8_t big_rs[20], big_v[20];
    memset(big_rs, 1, sizeof(big_rs));
    memset(big_v, 0x55, sizeof(big_v));
    uint64_t sr = s_status_rd;
    CHECK(clcd_bus_push(big_rs, big_v, 20u) == 8u);
    CHECK(s_status_rd - sr == 2u);
    CHECK(s_overflow == 0u);
    s_bus_ns = 110.0;
    uint32_t runs = 0, bytes = 0, reads = 0;
    hal_clcd_push_counts(&runs, &bytes, &reads);
    CHECK(runs >= 8u && bytes >= 14u && reads >= 9u);
    printf("  bus seam unit: rs -> CMD/DATA exact; FULL -> 0 bytes; level 120 -> 8 bytes, 2 STATUS reads\n");
#endif
}

/* A panel SLOWER than the CPU (5 us a byte): the FIFO fills, the seam stops at
 * full, nothing is dropped, and the repaint still completes pixel-exact. */
static void t_slow_panel(void)
{
    C = &COSTS[0];
    s_bus_ns = 5000.0;
    boot();
    rep_t r = run_repaint(200000u);
    printf("  slow panel (5 us/B): %.1f ms, FIFO peak %u/128, %u dropped, %llu STATUS reads\n",
           r.render_ms, s_fifo_max, s_overflow, (unsigned long long)r.status_rd);
    CHECK(s_overflow == 0u);
#ifndef HARNESSD_CLCD_PUSH_PER_BYTE
    CHECK(s_fifo_max >= 120u);                   /* it really did fill */
#endif
    CHECK(mirror_diff() == 0u);
    s_bus_ns = 110.0;
}

int main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
#ifdef HARNESSD_CLCD_PUSH_PER_BYTE
    printf("test_clcd_speed NEGATIVE CONTROL: the weak per-byte push, drain %u us, %u B a clcd_poll "
           "(rc2_v6)\n", (unsigned)HARNESSD_CLCD_DRAIN_US, (unsigned)CLCD_BYTES_PER_PASS);
#else
    printf("test_clcd_speed: the bus seam, drain %u us (%u us with a UART client), %u B a clcd_poll, "
           "target %u ms\n", (unsigned)HARNESSD_CLCD_DRAIN_US, (unsigned)HARNESSD_CLCD_DRAIN_UART_US,
           (unsigned)CLCD_BYTES_PER_PASS, TARGET_MS);
#endif
    t_bus_push_unit();

    set_t cen, opt, pes;
    C = &COSTS[0]; cen = scenarios(1);
    C = &COSTS[1]; opt = scenarios(0);
    C = &COSTS[2]; pes = scenarios(0);
    C = &COSTS[0];
    s_uart = 1;
    set_t uart = scenarios(1);
    s_uart = 0;
    printf("  (the last set: a 6930 client connected, drain %u us)\n", (unsigned)HARNESSD_CLCD_DRAIN_UART_US);
    t_slow_panel();

#ifdef HARNESSD_CLCD_PUSH_PER_BYTE
    /* the model reproduces the silicon symptom: glyph-by-glyph */
    CHECK(cen.page.render_ms > 300.0);
    CHECK(cen.full.render_ms > 500.0);
    CHECK(opt.page.render_ms > (double)TARGET_MS);   /* slow even if the MBV is kind */
    CHECK(cen.full.status_rd >= cen.full.bytes);     /* a STATUS read per byte */
    (void)pes; (void)uart;
#else
    CHECK(cen.full.render_ms > 0.0 && cen.full.render_ms <= (double)TARGET_MS);
    CHECK(cen.page.render_ms > 0.0 && cen.page.render_ms <= (double)TARGET_MS);
    CHECK(cen.regain.render_ms > 0.0 && cen.regain.render_ms <= (double)TARGET_MS);
    CHECK(opt.full.render_ms <= (double)TARGET_MS);
    CHECK(pes.page.render_ms <= 2.0 * TARGET_MS);    /* even pessimistic: < 0.3 s */
    CHECK(uart.page.render_ms <= 2.0 * TARGET_MS);
    CHECK(cen.full.status_rd * 48u <= cen.full.bytes);  /* <= 1 STATUS read / 48 B (was 1/B) */
#endif
    printf("test_clcd_speed: %d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
