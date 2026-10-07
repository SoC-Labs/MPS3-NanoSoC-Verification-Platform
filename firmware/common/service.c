/*
 * service.c — see service.h. The superloop walk, the budget watchdog and the
 * one bounded wait, with no register access and no platform header, so the
 * whole policy is exercised by firmware/test/test_service*.c on host gcc.
 */
#include <stddef.h>

#include "service.h"

/* ---------------------------------------------------------------------------
 * The bounded wait.
 * -------------------------------------------------------------------------- */
int mps3_spin_until(mps3_spin_pred_fn pred, void *ctx, uint32_t timeout_us)
{
    uint32_t t0;

    if (pred == NULL) {
        return 0;
    }
    /* Evaluate FIRST, then start counting. A predicate that is already true
     * must never be reported as a timeout, whatever timeout_us says -- this is
     * the one behaviour the old `for (i = 0; i < BOUND; i++)` form got right
     * for free and a naive deadline-first rewrite silently loses. */
    if (pred(ctx)) {
        return 1;
    }
    t0 = mps3_sys_now_us();
    for (;;) {
        if (pred(ctx)) {
            return 1;
        }
        /* Wrap-safe: the SIGNED difference, never `now < deadline`. */
        if ((int32_t)(mps3_sys_now_us() - t0) >= (int32_t)timeout_us) {
            return 0;
        }
    }
}

/* ---------------------------------------------------------------------------
 * The service table.
 * -------------------------------------------------------------------------- */
typedef struct {
    uint32_t max_us;      /* worst duration seen, since boot / reset_stats    */
    uint32_t resume_us;   /* while sick: the timestamp of the next probe pass */
    uint16_t consec;      /* CONSECUTIVE budget overruns                      */
    uint8_t  sick;        /* 1 = currently skipped (probed once a cooldown)   */
} svc_state_t;

static const mps3_service_t *s_tbl;
static unsigned              s_n;
static svc_state_t           s_st[MPS3_SVC_MAX];

static uint32_t s_pass_max_us;
static uint32_t s_worst_us;
static uint32_t s_worst_ix;
static uint32_t s_overrun_events;
static uint32_t s_skip_events;
static uint32_t s_skipped_mask;
static uint32_t s_kicks;

/* A4 interleave (service.h). Configuration, like the vital mask: not reset by
 * reset_stats()/install(). */
static mps3_service_fn s_ilv_fn;
static uint32_t        s_ilv_mask;
static uint32_t        s_ilv_runs;
static uint32_t        s_ilv_max_us;

/* `stats` window (service.h mps3_service_window_take). */
static uint32_t s_win_pass_max_us;
static uint32_t s_win_skip_base;

/* `reboot`: once set, run_pass() never kicks again this boot. */
static int s_kick_inhibited;

/* Which services must be healthy for a pass to earn its watchdog kick. NOT
 * reset by mps3_service_reset_stats() or mps3_service_install(): it is
 * configuration, not a statistic, and a caller that sets it before installing
 * the table must not have it silently reverted. Defaults to "every service is
 * vital", the strict reading -- see service.h's THE KICK block on why that is
 * the right DEFAULT and the wrong permanent CONFIGURATION. */
static uint32_t s_vital_mask = MPS3_SVC_VITAL_ALL;

/* THE SEAM. Weak, so every host-gcc binary in firmware/test/ links unchanged
 * and a shell whose BD carries no WDOG block is simply not kicking anything.
 * The platform overrides it with the real W1C of WDOG.TWCSR0.WDS. Identical
 * mechanism to coordinator.c's weak mps3_shell_static_id(), which
 * platform/generated/greybox_blob.c overrides -- already proven in this tree at
 * the target's optimisation level.
 *
 * It is a WEAK DEFINITION rather than an undefined extern on purpose: an
 * undefined symbol would make the kick a link-time obligation on every one of
 * the ~55 test binaries that pull service.c in transitively, and the first
 * response to that would be a stub in each, which is five copies of a decision
 * nobody would re-read. */
__attribute__((weak)) void mps3_wdt_kick(void)
{
    /* no watchdog in this build */
}

void mps3_service_reset_stats(void)
{
    unsigned i;
    for (i = 0u; i < MPS3_SVC_MAX; i++) {
        s_st[i].max_us    = 0u;
        s_st[i].resume_us = 0u;
        s_st[i].consec    = 0u;
        s_st[i].sick      = 0u;
    }
    s_pass_max_us    = 0u;
    s_worst_us       = 0u;
    s_worst_ix       = 0u;
    s_overrun_events = 0u;
    s_skip_events    = 0u;
    s_skipped_mask   = 0u;
    s_kicks          = 0u;
    s_ilv_runs       = 0u;
    s_ilv_max_us     = 0u;
    s_win_pass_max_us = 0u;
    s_win_skip_base  = 0u;
    s_kick_inhibited = 0;
    /* s_vital_mask and the interleave fn/mask are deliberately NOT reset here
     * -- configuration, not statistics (see their declarations). */
}

unsigned mps3_service_install(const mps3_service_t *tbl, unsigned n)
{
    if (tbl == NULL) {
        n = 0u;
    }
    /* REFUSE the overflow rather than truncating quietly: the per-service
     * telemetry is sized off MPS3_SVC_MAX, so a 13th service would be run but
     * never reported -- an invisible service is the diagnostic-that-lied bug
     * with a different hat on. The caller sees the shortfall in the return. */
    if (n > MPS3_SVC_MAX) {
        n = MPS3_SVC_MAX;
    }
    s_tbl = tbl;
    s_n   = n;
    mps3_service_reset_stats();
    return n;
}

void mps3_service_run_pass(void)
{
    uint32_t pass_t0;
    unsigned i;

    if (s_tbl == NULL || s_n == 0u) {
        return;
    }
    pass_t0 = mps3_sys_now_us();

    for (i = 0u; i < s_n; i++) {
        uint32_t t0, dt, now;

        if (s_st[i].sick) {
            /* Throttled, not executed: one probe per cooldown (service.h's
             * policy block). Wrap-safe signed compare. */
            if ((int32_t)(mps3_sys_now_us() - s_st[i].resume_us) < 0) {
                continue;
            }
        }

        t0 = mps3_sys_now_us();
        if (s_tbl[i].poll != NULL) {
            s_tbl[i].poll();
        }
        now = mps3_sys_now_us();
        dt  = now - t0;                    /* wrap-safe unsigned difference */

        if (dt > s_st[i].max_us) {
            s_st[i].max_us = dt;
        }
        if (dt > s_worst_us) {
            s_worst_us = dt;
            s_worst_ix = i;
        }

        /* A4: serve the control channel between heavy services. Timed on its
         * own clock so it is never charged to service i (service.h). */
        if (s_ilv_fn != NULL && i < 32u && (s_ilv_mask & ((uint32_t)1u << i))) {
            uint32_t it0 = mps3_sys_now_us();
            uint32_t idt;
            s_ilv_fn();
            idt = mps3_sys_now_us() - it0;
            s_ilv_runs++;
            if (idt > s_ilv_max_us) {
                s_ilv_max_us = idt;
            }
        }

        if (s_tbl[i].budget_us != 0u && dt > s_tbl[i].budget_us) {
            s_overrun_events++;
            if (s_st[i].consec < 0xFFFFu) {
                s_st[i].consec++;
            }
            if (s_st[i].consec >= MPS3_SVC_SICK_K) {
                if (!s_st[i].sick) {
                    s_skip_events++;       /* healthy -> sick EDGE, not a level */
                    s_st[i].sick = 1u;
                    s_skipped_mask |= (uint32_t)1u << i;
                }
                s_st[i].resume_us = now + MPS3_SVC_COOLDOWN_US;
            }
        } else {
            /* Under budget (or unbudgeted): healthy again, immediately. This
             * is the un-skip -- a probe pass that behaves clears both the
             * consecutive count and the sick bit in one step. */
            s_st[i].consec = 0u;
            if (s_st[i].sick) {
                s_st[i].sick = 0u;
                s_skipped_mask &= ~((uint32_t)1u << i);
            }
        }
    }

    {
        uint32_t pass = mps3_sys_now_us() - pass_t0;
        if (pass > s_pass_max_us) {
            s_pass_max_us = pass;
        }
        if (pass > s_win_pass_max_us) {
            s_win_pass_max_us = pass;
        }
    }

    /* THE KICK -- last thing in the pass, and conditional (service.h's THE
     * KICK block has the full argument).
     *
     * At the END, not the top: a kick at the top of the superloop proves the
     * loop SPINS. This pass has just measured whether it SERVED, so the kick is
     * spent on that evidence instead. A shell whose network RX has been marked
     * SICK -- throttled to one probe per 100 ms because it kept blowing its
     * budget -- stops kicking and is reset, which is correct: a board that
     * cannot be reached is a dark board however healthily the CLCD service is
     * running. A shell that is merely SLOW keeps kicking, because the sick
     * policy is a throttle and not an execution.
     *
     * Note what does NOT reach here: the early return at the top of this
     * function, for a table that was never installed or is empty. A shell with
     * no services does not kick, and the watchdog resets it. That is the
     * intended reading of "the superloop stopped" -- it never started. */
    if (!s_kick_inhibited && (s_skipped_mask & s_vital_mask) == 0u) {
        s_kicks++;
        mps3_wdt_kick();
    }
}

void mps3_service_set_interleave(mps3_service_fn fn, uint32_t after_mask)
{
    s_ilv_fn   = fn;
    s_ilv_mask = (fn != NULL) ? after_mask : 0u;
}

uint32_t mps3_service_interleave_runs(void)   { return s_ilv_runs; }
uint32_t mps3_service_interleave_max_us(void) { return s_ilv_max_us; }

void mps3_service_window_take(uint32_t *pass_max_us, uint32_t *skip_events)
{
    if (pass_max_us) {
        *pass_max_us = s_win_pass_max_us;
    }
    if (skip_events) {
        *skip_events = s_skip_events - s_win_skip_base;   /* wrap-safe delta */
    }
    s_win_pass_max_us = 0u;
    s_win_skip_base   = s_skip_events;
}

void mps3_service_inhibit_kick(void)   { s_kick_inhibited = 1; }
int  mps3_service_kick_inhibited(void) { return s_kick_inhibited; }

/* ---- telemetry ----------------------------------------------------------- */
unsigned mps3_service_count(void) { return s_n; }

const char *mps3_service_name(unsigned ix)
{
    if (s_tbl == NULL || ix >= s_n) {
        return NULL;
    }
    return s_tbl[ix].name;
}

uint32_t mps3_service_pass_max_us(void)    { return s_pass_max_us; }
uint32_t mps3_service_worst_us(void)       { return s_worst_us; }
uint32_t mps3_service_worst_index(void)    { return s_worst_ix; }
uint32_t mps3_service_overrun_events(void) { return s_overrun_events; }
uint32_t mps3_service_skip_events(void)    { return s_skip_events; }
uint32_t mps3_service_skipped_mask(void)   { return s_skipped_mask; }
uint32_t mps3_service_kicks(void)          { return s_kicks; }
uint32_t mps3_service_vital_mask(void)     { return s_vital_mask; }

void mps3_service_set_vital_mask(uint32_t mask)
{
    s_vital_mask = mask;
}

uint32_t mps3_service_max_us(unsigned ix)
{
    if (ix >= s_n) {
        return 0u;
    }
    return s_st[ix].max_us;
}

static uint32_t sat16(uint32_t us)
{
    return (us > 0xFFFFu) ? 0xFFFFu : us;
}

uint32_t mps3_service_max_us_pack(unsigned pair)
{
    unsigned lo_ix = pair * 2u;
    uint32_t lo, hi;

    if (pair >= MPS3_SVC_PACK_WORDS) {
        return 0u;
    }
    lo = sat16(mps3_service_max_us(lo_ix));
    hi = sat16(mps3_service_max_us(lo_ix + 1u));
    return (hi << 16) | lo;
}
