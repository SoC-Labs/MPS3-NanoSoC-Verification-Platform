/*
 * s0_models.h -- behavioural models of the two shell blocks stage0's cold-boot
 * hardening talks to, installed as mock_regs hooks (-DMPS3_HAL_MOCK). Shared by
 * test_stage0_hw.c (register-level) and test_stage0_entry.c (the whole entry
 * path). Time is mock_regs' clock, which advances per register read.
 *
 * THE AXI TIMEBASE WDT, as the vendor HDL builds it (Vivado 2026.1
 * data/ip/xilinx/axi_timebase_wdt_v3_0/hdl/axi_timebase_wdt_v3_0_rfs.vhd,
 * timebase_wdt_core, read 2026-09-28), with WDT_ENABLE_ONCE = Enable_repeatedly
 * as shell_bd.tcl sets it:
 *   - read_Mux_In = WRS & WDS & EWDT1 & EWDT2: TWCSR0 READS [3] WRS [2] WDS
 *     [1] EWDT1 [0] EWDT2, with timebase bits above; TWCSR1 is an "invalid
 *     read" and returns 0 (EWDT2 is write-only there);
 *   - wdt_Bit = edge AND (eWDT1_Reg OR eWDT2_Reg): it RUNS on either enable;
 *   - timebase_Reg_Reset on an enable write while BOTH enables are 0 (and on
 *     Reset) -- re-arming a running WDT does not restart its window;
 *   - an expiry every 2^C_WDT_INTERVAL cycles (bits N and N-1 of the counter:
 *     "10" and "00" both edge): the first sets WDS, the next with WDS still set
 *     asserts WDT_Reset;
 *   - WDS / WRS are W1C (Data(WDS/WRS) and WrCE(0)); WRS has NO reset term.
 * The watchdog reset pulses the WDT's own s_axi_aresetn (shell_bd.tcl), so a
 * fire clears the enables, WDS and the timebase, and leaves WRS set.
 *
 * TELEM: STATUS[0] = calib AND CTRL.alarm_en (telem.sv, [SEAM-3]); calib is a
 * schedule over mock time: 0 until `up_ms` after the cold entry, then 1 except
 * inside up to 8 glitch windows [a, b) (ms after the cold entry) -- the MCC
 * reprogramming OSCCLKs under a calibrated MIG.
 */
#ifndef S0_MODELS_H
#define S0_MODELS_H

#include <stdint.h>
#include "platform_regs.h"
#include "mock_regs.h"

#define WDT_PERIOD_US 21474836u        /* 2^31 cycles at 100 MHz */

static struct {
    uint32_t ewdt1, ewdt2, wds, wrs;
    uint32_t tb0_us;                   /* when the timebase was last zeroed */
    uint32_t k_done;                   /* expiries already processed        */
    uint32_t resets;                   /* WDT_Reset pulses                  */
    uint32_t reset_pending;            /* one fired; the harness must re-enter */
    uint32_t reset_at_us;
    uint32_t kicks;                    /* WDS W1Cs while running            */
    uint32_t last_kick_us;
    uint32_t writes;
} wdt;

static void wdt_model_reset(void)
{
    uint32_t wrs = wdt.wrs;
    memset(&wdt, 0, sizeof wdt);
    wdt.wrs = wrs;                     /* no reset term */
    wdt.tb0_us = mock_time_now_us();
}

static void wdt_eval(void)
{
    uint32_t now = mock_time_now_us();
    while ((now - wdt.tb0_us) / WDT_PERIOD_US > wdt.k_done) {
        wdt.k_done++;
        if (!(wdt.ewdt1 || wdt.ewdt2))
            continue;
        if (wdt.wds) {                 /* ExpiredTwice: WDT_Reset */
            wdt.resets++;
            wdt.reset_pending = 1u;
            wdt.reset_at_us = wdt.tb0_us + wdt.k_done * WDT_PERIOD_US;
            wdt.wrs = 1u;
            uint32_t r = wdt.resets, at = wdt.reset_at_us, p = wdt.reset_pending;
            uint32_t kk = wdt.kicks, lk = wdt.last_kick_us;
            wdt_model_reset();         /* its own aresetn (the counters are the test's) */
            wdt.resets = r;
            wdt.reset_at_us = at;
            wdt.reset_pending = p;
            wdt.kicks = kk;
            wdt.last_kick_us = lk;
            wdt.tb0_us = at;
            return;
        }
        wdt.wds = 1u;
    }
}

static int wdt_hook(void *c, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)c; (void)base;
    wdt_eval();
    uint32_t now = mock_time_now_us();
    uint32_t tb = (now - wdt.tb0_us) * 100u;
    if (is_write) {
        wdt.writes++;
        if (off == WDOG_TWCSR0) {
            if (!wdt.ewdt1 && !wdt.ewdt2 && (*val & WDOG_TWCSR0_EWDT1)) {
                wdt.tb0_us = now;
                wdt.k_done = 0u;
            }
            if ((*val & WDOG_TWCSR0_WDS) && (wdt.ewdt1 || wdt.ewdt2)) {
                wdt.kicks++;
                wdt.last_kick_us = now;
            }
            wdt.ewdt1 = (*val & WDOG_TWCSR0_EWDT1) ? 1u : 0u;
            if (*val & WDOG_TWCSR0_WDS)
                wdt.wds = 0u;
            if (*val & WDOG_TWCSR0_WRS)
                wdt.wrs = 0u;
        } else if (off == WDOG_TWCSR1) {
            if (!wdt.ewdt1 && !wdt.ewdt2 && (*val & WDOG_TWCSR1_EWDT2)) {
                wdt.tb0_us = now;
                wdt.k_done = 0u;
            }
            wdt.ewdt2 = *val & WDOG_TWCSR1_EWDT2;
        }
        return 1;
    }
    if (off == WDOG_TWCSR0)
        *val = (tb & ~0xFu) | (wdt.wrs << 3) | (wdt.wds << 2) | (wdt.ewdt1 << 1) | wdt.ewdt2;
    else if (off == WDOG_TBR)
        *val = tb;
    else
        *val = 0u;                     /* TWCSR1: "invalid read" */
    return 1;
}

static int wdt_running(void) { return wdt.ewdt1 || wdt.ewdt2; }

/* ---- TELEM ------------------------------------------------------------------------ */

static struct {
    uint32_t ctrl, ctrl_writes, status_reads;
    uint32_t t_cold_ms;                /* the cold entry the schedule is relative to */
    uint32_t up_ms;                    /* calib first comes up                     */
    uint32_t never;                    /* calib never comes up                     */
    uint32_t n;
    uint32_t a[8], b[8];               /* glitch windows [a, b), ms after t_cold  */
} tm;

static void telem_model_reset(void)
{
    memset(&tm, 0, sizeof tm);
    tm.t_cold_ms = mock_time_now_ms();
}

static void telem_glitch(uint32_t a, uint32_t b)
{
    if (tm.n < 8u) {
        tm.a[tm.n] = a;
        tm.b[tm.n] = b;
        tm.n++;
    }
}

static int telem_calib_now(void)
{
    uint32_t t = mock_time_now_ms() - tm.t_cold_ms;
    if (tm.never || t < tm.up_ms)
        return 0;
    for (uint32_t i = 0; i < tm.n; i++)
        if (t >= tm.a[i] && t < tm.b[i])
            return 0;
    return 1;
}

static int telem_hook(void *c, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)c; (void)base;
    if (is_write) {
        if (off == TELEM_CTRL) {
            tm.ctrl = *val;
            tm.ctrl_writes++;
        }
        return 1;
    }
    if (off == TELEM_STATUS)
        tm.status_reads++;
    *val = off == TELEM_CTRL ? tm.ctrl
         : off == TELEM_STATUS ? ((telem_calib_now() && (tm.ctrl & TELEM_CTRL_ALARM_EN)) ? 1u : 0u)
         : 0u;
    return 1;
}

/* A telem.sv reset (the watchdog's peripheral_aresetn): alarm_en cleared. */
static void telem_model_warm_reset(void) { tm.ctrl = 0u; }

#endif /* S0_MODELS_H */
