/*
 * test_service.c — the superloop's time discipline, exercised on host gcc:
 * firmware/common/service.c's bounded wait (mps3_spin_until) and its service
 * table / budget / sick-service policy.
 *
 * WHY THIS IS TESTABLE AT ALL: service.c touches no register and includes no
 * platform header, and firmware/test/mock_regs.c supplies the one thing it
 * needs -- mps3_sys_now_us() off a fake clock the test can move. So "a service
 * that overruns its budget three passes running is skipped, and is probed
 * again a cooldown later" is an assertion here rather than a paragraph nobody
 * can check until a board wedges.
 *
 * A fake service is just a function that advances the fake clock by however
 * many microseconds it wants to claim to have taken. That is exactly what the
 * runner measures on target (two timer reads around the call), so the model is
 * the real measurement, not a stand-in for it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../common/service.h"
#include "mock_regs.h"

static int g_fail;

#define CHECK(cond)                                                            \
    do {                                                                       \
        if (!(cond)) {                                                         \
            printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);           \
            g_fail = 1;                                                        \
        }                                                                      \
    } while (0)

#define CHECK_EQ(a, b)                                                         \
    do {                                                                       \
        unsigned long a_ = (unsigned long)(a), b_ = (unsigned long)(b);        \
        if (a_ != b_) {                                                        \
            printf("  FAIL %s:%d: %s == %s (%lu != %lu)\n", __FILE__,          \
                   __LINE__, #a, #b, a_, b_);                                  \
            g_fail = 1;                                                        \
        }                                                                      \
    } while (0)

/* ==========================================================================
 * mps3_spin_until
 * ========================================================================== */

/* A predicate that becomes true once the fake clock has passed `at_us`, and
 * counts how many times it was asked. Every evaluation costs 1 us of fake
 * time -- the stand-in for the register read a real predicate performs (see
 * mock_regs_set_us_per_read, which does the same for real register reads). */
typedef struct {
    uint32_t at_us;
    unsigned calls;
    int      always;   /* 1 = true immediately, -1 = never true */
} pred_ctx_t;

static int pred_after(void *vctx)
{
    pred_ctx_t *c = (pred_ctx_t *)vctx;
    c->calls++;
    mock_time_advance_us(1u);
    if (c->always > 0) {
        return 1;
    }
    if (c->always < 0) {
        return 0;
    }
    return (int32_t)(mock_time_now_us() - c->at_us) >= 0;
}

static void test_spin_true_immediately(void)
{
    pred_ctx_t c;
    uint32_t t0;

    printf("- spin_until: an already-true predicate is never a timeout\n");
    mock_regs_reset();
    memset(&c, 0, sizeof(c));
    c.always = 1;

    /* timeout_us == 0 is the sharp case. A deadline-first implementation
     * ("if now >= t0 + timeout return 0") reports a timeout WITHOUT ever
     * asking, which silently turns every zero/short wait into a failure. */
    t0 = mock_time_now_us();
    CHECK_EQ(mps3_spin_until(pred_after, &c, 0u), 1);
    CHECK_EQ(c.calls, 1u);
    CHECK(mock_time_now_us() - t0 <= 2u);
}

static void test_spin_true_before_deadline(void)
{
    pred_ctx_t c;

    printf("- spin_until: satisfied before the deadline returns 1\n");
    mock_regs_reset();
    memset(&c, 0, sizeof(c));
    c.at_us = 250u;   /* becomes true a quarter of the way through */

    CHECK_EQ(mps3_spin_until(pred_after, &c, 1000u), 1);
    CHECK(mock_time_now_us() >= 250u);
    /* It must NOT have burned the whole budget once the answer arrived. */
    CHECK(mock_time_now_us() < 400u);
}

static void test_spin_times_out_on_a_stuck_predicate(void)
{
    pred_ctx_t c;
    uint32_t elapsed;

    printf("- spin_until: a stuck predicate gives up AT the microsecond bound\n");
    mock_regs_reset();
    memset(&c, 0, sizeof(c));
    c.always = -1;   /* never true: the stuck-register shape */

    CHECK_EQ(mps3_spin_until(pred_after, &c, 3000u), 0);
    elapsed = mock_time_now_us();
    /* The bound is a DURATION, so both sides of it are assertable: it may not
     * give up early, and it may not overrun by more than one iteration. An
     * iteration-count bound could satisfy neither -- it has no idea what a
     * microsecond is. */
    CHECK(elapsed >= 3000u);
    CHECK(elapsed <= 3000u + 8u);
}

static void test_spin_null_predicate(void)
{
    printf("- spin_until: a NULL predicate fails closed\n");
    mock_regs_reset();
    CHECK_EQ(mps3_spin_until(NULL, NULL, 1000u), 0);
}

static void test_spin_is_wrap_safe(void)
{
    pred_ctx_t c;

    printf("- spin_until: the deadline survives the 32-bit microsecond wrap\n");
    mock_regs_reset();
    /* ~71.6 minutes of uptime lands here; the counter wraps mid-wait. An
     * unsigned `now < t0 + timeout` comparison returns immediately here. */
    mock_time_set_us(0xFFFFFF00u);
    memset(&c, 0, sizeof(c));
    c.always = -1;

    CHECK_EQ(mps3_spin_until(pred_after, &c, 2000u), 0);
    /* 0xFFFFFF00 + ~2000 wraps past zero. */
    CHECK(mock_time_now_us() < 0x1000u);
    CHECK(c.calls >= 1000u);
}

/* ==========================================================================
 * The service table
 * ========================================================================== */

static uint32_t g_cost_us[MPS3_SVC_MAX];   /* what each fake service "takes" */
static unsigned g_ran[MPS3_SVC_MAX];       /* how many times it was called   */

#define SVC_BODY(ix)                                                           \
    static void svc_##ix(void)                                                 \
    {                                                                          \
        g_ran[ix]++;                                                           \
        mock_time_advance_us(g_cost_us[ix]);                                   \
    }
SVC_BODY(0)
SVC_BODY(1)
SVC_BODY(2)

static void svc_reset(void)
{
    mock_regs_reset();
    memset(g_cost_us, 0, sizeof(g_cost_us));
    memset(g_ran, 0, sizeof(g_ran));
}

static void test_table_times_every_service(void)
{
    static const mps3_service_t tbl[] = {
        { "a", svc_0, 1000u },
        { "b", svc_1, 1000u },
    };

    printf("- table: every service is timed and the pass is timed\n");
    svc_reset();
    CHECK_EQ(mps3_service_install(tbl, 2u), 2u);
    CHECK_EQ(mps3_service_count(), 2u);

    g_cost_us[0] = 100u;
    g_cost_us[1] = 300u;
    mps3_service_run_pass();

    CHECK_EQ(g_ran[0], 1u);
    CHECK_EQ(g_ran[1], 1u);
    CHECK_EQ(mps3_service_max_us(0), 100u);
    CHECK_EQ(mps3_service_max_us(1), 300u);
    CHECK_EQ(mps3_service_worst_us(), 300u);
    CHECK_EQ(mps3_service_worst_index(), 1u);
    CHECK_EQ(mps3_service_pass_max_us(), 400u);
    CHECK_EQ(mps3_service_overrun_events(), 0u);
    CHECK_EQ(mps3_service_skipped_mask(), 0u);

    /* Maxima are HIGH-WATER marks, not last-pass values: a fast pass after a
     * slow one must not erase the evidence of the slow one. */
    g_cost_us[0] = 10u;
    g_cost_us[1] = 10u;
    mps3_service_run_pass();
    CHECK_EQ(mps3_service_max_us(1), 300u);
    CHECK_EQ(mps3_service_pass_max_us(), 400u);

    CHECK(mps3_service_name(0) != NULL && strcmp(mps3_service_name(0), "a") == 0);
    CHECK(mps3_service_name(2) == NULL);
}

static void test_sick_after_exactly_k_consecutive_overruns(void)
{
    static const mps3_service_t tbl[] = {
        { "slow", svc_0, 1000u },
    };
    unsigned i;

    printf("- policy: K-1 consecutive overruns is not sick; K is\n");
    svc_reset();
    mps3_service_install(tbl, 1u);
    g_cost_us[0] = 5000u;   /* 5x its budget, every pass */

    for (i = 0u; i < MPS3_SVC_SICK_K - 1u; i++) {
        mps3_service_run_pass();
        CHECK_EQ(mps3_service_skipped_mask(), 0u);   /* still in the rotation */
    }
    CHECK_EQ(mps3_service_overrun_events(), MPS3_SVC_SICK_K - 1u);
    CHECK_EQ(g_ran[0], MPS3_SVC_SICK_K - 1u);

    mps3_service_run_pass();                          /* the Kth */
    CHECK_EQ(mps3_service_skipped_mask(), 1u);
    CHECK_EQ(mps3_service_skip_events(), 1u);
    CHECK_EQ(mps3_service_overrun_events(), MPS3_SVC_SICK_K);
}

static void test_one_good_pass_clears_the_streak(void)
{
    static const mps3_service_t tbl[] = {
        { "bursty", svc_0, 1000u },
    };
    unsigned i;

    printf("- policy: the overrun count is CONSECUTIVE, not cumulative\n");
    svc_reset();
    mps3_service_install(tbl, 1u);

    /* K-1 overruns, one healthy pass, K-1 overruns again: a service that is
     * merely bursty must never be taken out. A cumulative counter (the
     * mutation) marks this sick on the (K+1)th overrun. */
    for (i = 0u; i < MPS3_SVC_SICK_K - 1u; i++) {
        g_cost_us[0] = 5000u;
        mps3_service_run_pass();
    }
    g_cost_us[0] = 10u;
    mps3_service_run_pass();
    for (i = 0u; i < MPS3_SVC_SICK_K - 1u; i++) {
        g_cost_us[0] = 5000u;
        mps3_service_run_pass();
    }
    CHECK_EQ(mps3_service_skipped_mask(), 0u);
    CHECK_EQ(mps3_service_skip_events(), 0u);
    CHECK_EQ(mps3_service_overrun_events(), 2u * (MPS3_SVC_SICK_K - 1u));
}

static void test_sick_service_is_skipped_then_probed(void)
{
    static const mps3_service_t tbl[] = {
        { "sick",    svc_0, 1000u },
        { "healthy", svc_1, 1000u },
    };
    unsigned i, ran_at_sick;

    printf("- policy: a sick service is skipped, then probed once a cooldown\n");
    svc_reset();
    mps3_service_install(tbl, 2u);
    g_cost_us[0] = 5000u;
    g_cost_us[1] = 10u;

    for (i = 0u; i < MPS3_SVC_SICK_K; i++) {
        mps3_service_run_pass();
    }
    CHECK_EQ(mps3_service_skipped_mask(), 1u);
    ran_at_sick = g_ran[0];

    /* Passes inside the cooldown must not call it -- and must still call the
     * healthy service, which is the whole point of taking it out. */
    for (i = 0u; i < 20u; i++) {
        mps3_service_run_pass();
    }
    CHECK_EQ(g_ran[0], ran_at_sick);
    CHECK_EQ(g_ran[1], MPS3_SVC_SICK_K + 20u);

    /* Cooldown expires -> exactly one probe. It overruns again, so it goes
     * straight back to skipped for another cooldown. */
    mock_time_advance_us(MPS3_SVC_COOLDOWN_US);
    mps3_service_run_pass();
    CHECK_EQ(g_ran[0], ran_at_sick + 1u);
    CHECK_EQ(mps3_service_skipped_mask(), 1u);
    /* A re-skip is not a new EDGE: skip_events counts healthy->sick
     * transitions, so an endlessly sick service must not inflate it. */
    CHECK_EQ(mps3_service_skip_events(), 1u);

    mps3_service_run_pass();
    CHECK_EQ(g_ran[0], ran_at_sick + 1u);   /* back in cooldown */
}

static void test_a_healthy_probe_unskips(void)
{
    static const mps3_service_t tbl[] = {
        { "recovers", svc_0, 1000u },
    };
    unsigned i;

    printf("- policy: a probe that comes in under budget un-skips immediately\n");
    svc_reset();
    mps3_service_install(tbl, 1u);
    g_cost_us[0] = 5000u;
    for (i = 0u; i < MPS3_SVC_SICK_K; i++) {
        mps3_service_run_pass();
    }
    CHECK_EQ(mps3_service_skipped_mask(), 1u);

    g_cost_us[0] = 10u;                      /* whatever it was, it stopped */
    mock_time_advance_us(MPS3_SVC_COOLDOWN_US);
    mps3_service_run_pass();
    CHECK_EQ(mps3_service_skipped_mask(), 0u);

    /* And it is fully back: the NEXT pass runs it without waiting a cooldown. */
    {
        unsigned before = g_ran[0];
        mps3_service_run_pass();
        CHECK_EQ(g_ran[0], before + 1u);
    }
    /* Sick once, recovered once -- the edge count stays at one. */
    CHECK_EQ(mps3_service_skip_events(), 1u);
}

static void test_zero_budget_is_never_sick(void)
{
    static const mps3_service_t tbl[] = {
        { "unbudgeted", svc_0, 0u },
    };
    unsigned i;

    printf("- policy: budget_us == 0 is timed and reported but never sick\n");
    svc_reset();
    mps3_service_install(tbl, 1u);
    g_cost_us[0] = 500000u;   /* half a second, every pass */

    for (i = 0u; i < MPS3_SVC_SICK_K + 5u; i++) {
        mps3_service_run_pass();
    }
    CHECK_EQ(mps3_service_skipped_mask(), 0u);
    CHECK_EQ(mps3_service_overrun_events(), 0u);
    CHECK_EQ(g_ran[0], MPS3_SVC_SICK_K + 5u);
    CHECK_EQ(mps3_service_max_us(0), 500000u);   /* still measured */
}

static void test_install_refuses_an_oversized_table(void)
{
    static mps3_service_t big[MPS3_SVC_MAX + 4u];
    unsigned i;

    printf("- install: a table past MPS3_SVC_MAX is refused, not truncated silently\n");
    svc_reset();
    for (i = 0u; i < MPS3_SVC_MAX + 4u; i++) {
        big[i].name = "x";
        big[i].poll = svc_2;
        big[i].budget_us = 0u;
    }
    CHECK_EQ(mps3_service_install(big, MPS3_SVC_MAX + 4u), MPS3_SVC_MAX);
    CHECK_EQ(mps3_service_count(), MPS3_SVC_MAX);
}

static void test_pack_layout_and_saturation(void)
{
    static const mps3_service_t tbl[] = {
        { "a", svc_0, 0u },
        { "b", svc_1, 0u },
        { "c", svc_2, 0u },
    };

    printf("- pack: two 16-bit saturating maxima per word, low index first\n");
    svc_reset();
    mps3_service_install(tbl, 3u);
    g_cost_us[0] = 0x1234u;
    g_cost_us[1] = 0x0011u;
    g_cost_us[2] = 200000u;    /* way past 16 bits: must SATURATE, not wrap */
    mps3_service_run_pass();

    CHECK_EQ(mps3_service_max_us_pack(0), 0x00111234u);
    CHECK_EQ(mps3_service_max_us_pack(1), 0x0000FFFFu);
    /* A truncating cast would publish 200000 & 0xFFFF = 0xF200 here, i.e. a
     * 62 ms service reported as 62 ms only by accident and a 65540 us one
     * reported as 4 us. Saturation says "at least 65535" and never lies low. */
    CHECK_EQ(mps3_service_max_us_pack(MPS3_SVC_PACK_WORDS), 0u);
}

static void test_a_null_poll_entry_is_survivable(void)
{
    static const mps3_service_t tbl[] = {
        { "hole", NULL,  1000u },
        { "real", svc_1, 1000u },
    };

    printf("- table: a NULL poll entry is skipped, not called\n");
    svc_reset();
    mps3_service_install(tbl, 2u);
    g_cost_us[1] = 10u;
    mps3_service_run_pass();
    CHECK_EQ(g_ran[1], 1u);
}

/* ==========================================================================
 * THE HARDWARE WATCHDOG KICK
 *
 * WDOG (@0x44B4_0000, xilinx.com:ip:axi_timebase_wdt:3.0) resets the shell if
 * it is not kicked. The kick lives at the END of mps3_service_run_pass() and is
 * conditional, because a kick at the TOP of the superloop proves only that the
 * loop SPINS -- and a shell that spins without serving its network is a dark
 * board with a healthy heartbeat. docs/planning/SERVICES_PARTITION.md §5.2.
 *
 * mps3_wdt_kick() is a WEAK no-op in service.c; this file provides the STRONG
 * override so the kicks are countable. That the override is reached at all is
 * itself part of the test: if a compiler ever inlined the weak body at the call
 * site, g_kicks would stay 0 and these tests would go red rather than the
 * watchdog quietly resetting a healthy board on silicon.
 * ========================================================================== */
static unsigned g_kicks;

void mps3_wdt_kick(void)
{
    g_kicks++;
}

static void test_a_healthy_pass_kicks_the_watchdog(void)
{
    static const mps3_service_t tbl[] = {
        { "a", svc_0, 1000u },
        { "b", svc_1, 1000u },
    };

    printf("- watchdog: a pass that served earns a kick\n");
    svc_reset();
    g_kicks = 0u;
    CHECK_EQ(mps3_service_install(tbl, 2u), 2u);
    /* The strong override IS linked in place of service.c's weak no-op. */
    CHECK_EQ(mps3_service_vital_mask(), MPS3_SVC_VITAL_ALL);

    g_cost_us[0] = 100u;
    g_cost_us[1] = 100u;
    mps3_service_run_pass();
    CHECK_EQ(g_kicks, 1u);
    CHECK_EQ(mps3_service_kicks(), 1u);

    mps3_service_run_pass();
    mps3_service_run_pass();
    CHECK_EQ(g_kicks, 3u);
    CHECK_EQ(mps3_service_kicks(), 3u);
}

static void test_a_sick_vital_service_withholds_the_kick(void)
{
    static const mps3_service_t tbl[] = {
        { "a", svc_0, 1000u },
        { "b", svc_1, 1000u },
    };

    printf("- watchdog: a sick vital service stops the kick\n");
    svc_reset();
    g_kicks = 0u;
    mps3_service_install(tbl, 2u);

    /* Service 1 blows its budget. The first two overruns are a BURST, not a
     * pathology -- the sick policy needs K consecutive -- so the kick keeps
     * coming: a shell that is merely slow must not be reset. */
    g_cost_us[0] = 10u;
    g_cost_us[1] = 5000u;
    mps3_service_run_pass();
    mps3_service_run_pass();
    CHECK_EQ(mps3_service_skipped_mask(), 0u);
    CHECK_EQ(g_kicks, 2u);

    /* The third consecutive overrun marks it sick. THAT is when the kick
     * stops -- and with it the board's claim to be alive. */
    mps3_service_run_pass();
    CHECK_EQ(mps3_service_skipped_mask(), 1u << 1);
    CHECK_EQ(g_kicks, 2u);          /* not 3: the pass did NOT earn one */
    CHECK_EQ(mps3_service_kicks(), 2u);

    /* And it stays stopped while the service stays sick. On silicon this is
     * ~1.34 s to WDS and ~2.7 s to the reset. */
    mps3_service_run_pass();
    mps3_service_run_pass();
    CHECK_EQ(g_kicks, 2u);

    /* Recovery is real, not theoretical: the cooldown probe comes in under
     * budget, the service un-sicks in the same pass, and that pass kicks. */
    mock_time_advance_us(MPS3_SVC_COOLDOWN_US + 1u);
    g_cost_us[1] = 10u;
    mps3_service_run_pass();
    CHECK_EQ(mps3_service_skipped_mask(), 0u);
    CHECK_EQ(g_kicks, 3u);
}

static void test_a_sick_NON_vital_service_still_kicks(void)
{
    static const mps3_service_t tbl[] = {
        { "net", svc_0, 1000u },
        { "lcd", svc_1, 1000u },
    };

    printf("- watchdog: the vital mask narrows what can stop the kick\n");
    svc_reset();
    g_kicks = 0u;
    mps3_service_install(tbl, 2u);

    /* Only service 0 is vital -- the §5.2 configuration, in miniature: the
     * network is the only ingress, the panel is not. Note this is set AFTER
     * install() on purpose; install() must not revert it. */
    mps3_service_set_vital_mask(1u << 0);
    CHECK_EQ(mps3_service_vital_mask(), 1u << 0);

    g_cost_us[0] = 10u;
    g_cost_us[1] = 5000u;
    mps3_service_run_pass();
    mps3_service_run_pass();
    mps3_service_run_pass();
    CHECK_EQ(mps3_service_skipped_mask(), 1u << 1);  /* lcd IS sick */
    CHECK_EQ(g_kicks, 3u);                            /* ...and irrelevant */

    /* Now the network goes sick too. THAT stops the kick. */
    g_cost_us[0] = 5000u;
    mps3_service_run_pass();
    mps3_service_run_pass();
    mps3_service_run_pass();
    CHECK(mps3_service_skipped_mask() & (1u << 0));
    CHECK_EQ(g_kicks, 5u);   /* the first two overrun passes still kicked */

    mps3_service_run_pass();
    CHECK_EQ(g_kicks, 5u);

    /* Restore the default for any test that runs after this one: the mask is
     * deliberately NOT reset by install()/reset_stats(), so it is sticky. */
    mps3_service_set_vital_mask(MPS3_SVC_VITAL_ALL);
}

static void test_a_shell_with_no_services_never_kicks(void)
{
    printf("- watchdog: a loop with nothing in it is not alive\n");
    svc_reset();
    g_kicks = 0u;

    /* run_pass() returns early with no table installed, so it never reaches
     * the kick. A shell that registered no services is not a shell that is
     * running; letting it kick would be a heartbeat with no pulse behind it. */
    CHECK_EQ(mps3_service_install(NULL, 0u), 0u);
    mps3_service_run_pass();
    mps3_service_run_pass();
    CHECK_EQ(g_kicks, 0u);
    CHECK_EQ(mps3_service_kicks(), 0u);
}

/* ==========================================================================
 * A4: control-channel liveness -- the INTERLEAVE (service.h)
 * ==========================================================================
 * THE STATED BOUND: with the interleave installed after every heavy service,
 * the control channel (6900) is polled at least once every
 *     (longest SINGLE heavy-service call) + ILV_SLACK_US
 * instead of once per whole pass. The synthetic table mirrors main.c's order
 * (net_rx, ctrl, ..., xvc, uart, clcd, diag) with xvc and clcd made slow but
 * each still UNDER its budget -- the 2026-09-22 shape, where a slow service
 * was never sick and so nothing tripped. Without the interleave the gap is
 * the sum of both heavy calls (the starvation); with it, the larger one. */
#define ILV_XVC_US    45000u   /* under xvc's 50 ms budget: never sick    */
#define ILV_CLCD_US   25000u   /* under clcd's 30 ms budget: never sick   */
#define ILV_SLACK_US   1000u   /* the small services between two polls    */
#define ILV_BOUND_US  (ILV_XVC_US + ILV_SLACK_US)

static uint32_t g_ctrl_last_us, g_ctrl_max_gap_us;
static unsigned g_ctrl_polls;
static void ilv_ctrl(void)          /* stands in for coordinator_net_poll() */
{
    uint32_t now = mock_time_now_us();
    if (g_ctrl_polls > 0u && (now - g_ctrl_last_us) > g_ctrl_max_gap_us) {
        g_ctrl_max_gap_us = now - g_ctrl_last_us;
    }
    g_ctrl_last_us = now;
    g_ctrl_polls++;
    mock_time_advance_us(20u);
}
static void ilv_net_rx(void) { mock_time_advance_us(50u); }
static void ilv_xvc(void)    { mock_time_advance_us(ILV_XVC_US); }
static void ilv_uart(void)   { mock_time_advance_us(100u); }
static void ilv_clcd(void)   { mock_time_advance_us(ILV_CLCD_US); }
static void ilv_diag(void)   { mock_time_advance_us(100u); }

static const mps3_service_t g_ilv_tbl[] = {
    /* 0 */ { "net_rx", ilv_net_rx, 20000u },
    /* 1 */ { "ctrl",   ilv_ctrl,   10000u },
    /* 2 */ { "xvc",    ilv_xvc,    50000u },
    /* 3 */ { "uart",   ilv_uart,   10000u },
    /* 4 */ { "clcd",   ilv_clcd,   30000u },
    /* 5 */ { "diag",   ilv_diag,    2000u },
};
#define ILV_MASK ((1u << 2) | (1u << 4))   /* after xvc and clcd, as main.c */

static uint32_t ilv_measure(int with_interleave)
{
    svc_reset();
    g_ctrl_last_us = 0u; g_ctrl_max_gap_us = 0u; g_ctrl_polls = 0u;
    mps3_service_install(g_ilv_tbl, 6u);
    mps3_service_set_interleave(with_interleave ? ilv_ctrl : NULL, ILV_MASK);
    for (int i = 0; i < 20; i++) {
        mps3_service_run_pass();
    }
    return g_ctrl_max_gap_us;
}

static void test_interleave_bounds_control_channel_latency(void)
{
    printf("- A4: 6900 polled within (longest heavy call + %u us) = %u us\n",
           (unsigned)ILV_SLACK_US, (unsigned)ILV_BOUND_US);

    uint32_t without = ilv_measure(0);
    uint32_t with    = ilv_measure(1);
    printf("  worst 6900 gap: without interleave %u us, with %u us\n",
           (unsigned)without, (unsigned)with);

    /* The starvation, reproduced: both heavy calls back to back. */
    CHECK(without > ILV_BOUND_US);
    CHECK(without >= ILV_XVC_US + ILV_CLCD_US);
    /* The fix: bounded by the longest single call. */
    CHECK(with <= ILV_BOUND_US);
    /* Neither heavy service was ever sick (they were UNDER budget) -- so the
     * sick policy alone could never have produced this bound. */
    CHECK_EQ(mps3_service_skip_events(), 0u);
    /* The interleave ran twice per pass and is NOT charged to the service it
     * follows: clcd's own maximum is its own cost, not cost + a 6900 poll. */
    CHECK_EQ(mps3_service_interleave_runs(), 2u * 20u);
    CHECK_EQ(mps3_service_max_us(4), ILV_CLCD_US);
    CHECK(mps3_service_interleave_max_us() >= 20u);
    mps3_service_set_interleave(NULL, 0u);
}

static void test_a_skipped_service_earns_no_interleave(void)
{
    static const mps3_service_t tbl[] = {
        { "slow", svc_0, 1000u },
    };
    printf("- A4: a throttled (sick) service gets no interleave call\n");
    svc_reset();
    g_ctrl_polls = 0u;
    mps3_service_install(tbl, 1u);
    mps3_service_set_interleave(ilv_ctrl, 1u << 0);
    g_cost_us[0] = 5000u;                 /* over budget -> sick after K=3 */
    for (int i = 0; i < 3; i++) mps3_service_run_pass();
    CHECK_EQ(mps3_service_skipped_mask(), 1u);
    CHECK_EQ(mps3_service_interleave_runs(), 3u);
    mps3_service_run_pass();              /* skipped (inside the cooldown)  */
    CHECK_EQ(g_ran[0], 3u);
    CHECK_EQ(mps3_service_interleave_runs(), 3u);
    mps3_service_set_interleave(NULL, 0u);
}

static void test_window_take_and_kick_inhibit(void)
{
    static const mps3_service_t tbl[] = {
        { "a", svc_0, 1000u },
    };
    uint32_t pmax = 0u, skips = 0u;
    printf("- stats window: since-last-take maxima; reboot kick inhibit\n");
    svc_reset();
    mps3_service_install(tbl, 1u);
    g_cost_us[0] = 700u;
    mps3_service_run_pass();
    mps3_service_window_take(&pmax, &skips);
    CHECK_EQ(pmax, 700u);
    CHECK_EQ(skips, 0u);
    g_cost_us[0] = 100u;
    mps3_service_run_pass();
    mps3_service_window_take(&pmax, &skips);
    CHECK_EQ(pmax, 100u);                 /* the WINDOW restarted ...        */
    CHECK_EQ(mps3_service_pass_max_us(), 700u); /* ... the since-boot did not */

    g_kicks = 0u;
    mps3_service_run_pass();
    CHECK_EQ(g_kicks, 1u);
    mps3_service_inhibit_kick();
    mps3_service_run_pass();
    mps3_service_run_pass();
    CHECK_EQ(g_kicks, 1u);                /* never again this boot           */
    CHECK(mps3_service_kick_inhibited());
}

int main(void)
{
    printf("== test_service ==\n");
    test_spin_true_immediately();
    test_spin_true_before_deadline();
    test_spin_times_out_on_a_stuck_predicate();
    test_spin_null_predicate();
    test_spin_is_wrap_safe();

    test_table_times_every_service();
    test_sick_after_exactly_k_consecutive_overruns();
    test_one_good_pass_clears_the_streak();
    test_sick_service_is_skipped_then_probed();
    test_a_healthy_probe_unskips();
    test_zero_budget_is_never_sick();
    test_install_refuses_an_oversized_table();
    test_pack_layout_and_saturation();
    test_a_null_poll_entry_is_survivable();

    test_a_healthy_pass_kicks_the_watchdog();
    test_a_sick_vital_service_withholds_the_kick();
    test_a_sick_NON_vital_service_still_kicks();
    test_a_shell_with_no_services_never_kicks();

    test_interleave_bounds_control_channel_latency();
    test_a_skipped_service_earns_no_interleave();
    test_window_take_and_kick_inhibit();

    if (g_fail) {
        printf("FAILED\n");
        return 1;
    }
    printf("test_service: PASS\n");
    return 0;
}
