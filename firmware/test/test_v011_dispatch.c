/*
 * test_v011_dispatch.c -- host-gcc tests for the net-protocol v0.11 verbs
 * `stats`, `log` and `reboot`, and the v0.11 `version.features` bits, driven the
 * way a client drives them: a line into coordinator_dispatch_line(), the real
 * codec's bytes out. Same link set as test_coordinator_dispatch (DISPATCH_SRCS:
 * real coordinator.c / swap_fsm.c / clkrst.c / net_proto.c / log_ring.c /
 * service.c over mock_regs), built WITHOUT -DMPS3_HAS_TOUCH, so `touch_cal`'s
 * OFF-build answer is covered here and its ON-build behaviour in
 * test_touch_cal_dispatch.c.
 *
 * Every test here fails on the pre-v0.11 firmware: the verbs did not decode
 * ("unknown op") and the feature array stopped at bit 4.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../common/platform_regs.h"
#include "../common/diag.h"
#include "../common/log_ring.h"
#include "../common/service.h"
#include "mock_regs.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0x3F1A560Fu

uint32_t mps3_shell_static_id(void) { return TEST_STATIC_ID; }

/* STRONG override of coordinator.c's weak mps3_stats_net() -- exactly how the
 * platform's main.c provides it on the target. */
static mps3_stats_net_t s_net;
void mps3_stats_net(mps3_stats_net_t *out) { *out = s_net; }

static void boot(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_reset();
    fake_overlay_store_set_greybox(&greybox, 0);
    memset(&s_net, 0, sizeof(s_net));
    mps3_log_reset();
    mps3_service_reset_stats();
    coordinator_init();
}

static int dispatch(const char *line, char *out, int out_len)
{
    return coordinator_dispatch_line(line, (int)strlen(line), out, out_len);
}

/* ---- tiny JSON readers for a FLAT one-line reply ------------------------- */

/* Pointer to the value text of "key", or NULL. Keys are matched with their
 * quotes and colon so "rm" never matches "rm_ok". */
static const char *jval(const char *line, const char *key)
{
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char *p = strstr(line, pat);
    return p ? p + strlen(pat) : NULL;
}
static long jint(const char *line, const char *key)
{
    const char *v = jval(line, key);
    assert(v != NULL);
    return strtol(v, NULL, 10);
}
static int jbool(const char *line, const char *key)
{
    const char *v = jval(line, key);
    assert(v != NULL);
    if (strncmp(v, "true", 4) == 0) return 1;
    assert(strncmp(v, "false", 5) == 0);
    return 0;
}
static void jstr(const char *line, const char *key, char *out, size_t cap)
{
    const char *v = jval(line, key);
    assert(v != NULL && *v == '"');
    v++;
    size_t n = 0;
    while (v[n] != '"' && n + 1 < cap) { out[n] = v[n]; n++; }
    out[n] = '\0';
}
/* The KEYS of a flat reply, in emitted order, joined by ','. */
static void jkeys(const char *line, char *out, size_t cap)
{
    size_t o = 0;
    const char *p = line;
    out[0] = '\0';
    int in_str = 0;
    int expect_key = 0;
    for (; *p; p++) {
        if (*p == '{' || (*p == ',' && !in_str)) { expect_key = 1; continue; }
        if (expect_key && *p == '"') {
            const char *e = strchr(p + 1, '"');
            size_t n = (size_t)(e - (p + 1));
            if (o) out[o++] = ',';
            memcpy(out + o, p + 1, n);
            o += n;
            out[o] = '\0';
            p = e;
            expect_key = 0;
            continue;
        }
        if (*p == '"') in_str = !in_str;
    }
    assert(o < cap);
}

/* ---- pump a swap to completion (same choreography as the dispatch test) -- */
static void pump_swap(uint32_t rm_id_readback)
{
    config_agent_bitstream_info_t incoming_clear = {
        .rm_id = 1, .static_id = TEST_STATIC_ID, .len_words = 8, .crc32 = 0x2222,
    };
    config_agent_bitstream_info_t incoming_partial = {
        .rm_id = 1, .static_id = TEST_STATIC_ID, .len_words = 4, .crc32 = 0x3333,
    };
    fake_config_agent_arm_clearing(&incoming_clear);
    fake_config_agent_arm_partial(&incoming_partial);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    for (int i = 0; i < 64 && !swap_fsm_idle(); i++) {
        switch (swap_fsm_state()) {
        case SWAP_DECOUPLE_ASSERT:
        case SWAP_REISOLATE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        case SWAP_VERIFY:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, rm_id_readback);
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS,
                           DFXCTL_RM_STATUS_RM_ID_VALID);
            break;
        case SWAP_RELEASE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
            break;
        default:
            break;
        }
        swap_fsm_poll();
    }
}

/* ==========================================================================
 * version.features (A2)
 * ========================================================================== */
static void test_version_features_v011_bits(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    CHECK(dispatch("{\"op\":\"version\"}", out, sizeof(out)) == 1);
    /* This binary: no -D feature flags, XVC_TARGET unset (=> dbgbr), no TOUCH.
     * So exactly the always-compiled-in services, in BIT order. */
    CHECK(strstr(out, "\"features\":[\"jtag_server\",\"xvc_dbgbr\",\"stats\","
                      "\"log\",\"reboot\",\"usd\"],") != NULL);   /* v0.13 appends usd */
    /* Not claimed without their builds: */
    CHECK(strstr(out, "\"touch_cal\"") == NULL);
    CHECK(strstr(out, "\"dut_egress\"") == NULL);
    CHECK(strstr(out, "\"xvc_jtagbb\"") == NULL);
}

/* ==========================================================================
 * stats (A1)
 * ========================================================================== */

/* fpgahub's _from_stats_verb shape, in order, then the v0.11 extras. */
static const char *const STATS_KEYS =
    "ok,up_ms,sid,rm,rm_ok,lock,clk_sel,mmcm,clk_alive,dut_rst,rp_rst,decpl,"
    "link,spd,fdx,mac,swap,swap_ok,swap_n,icap,rxdrop,txerr,"
    "swap_err,clr_ok,dut_mhz,svc_max_us,svc_skipped";

static void test_stats_key_shape_and_order(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX], keys[512];
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    jkeys(out, keys, sizeof(keys));
    CHECK(strcmp(keys, STATS_KEYS) == 0);
    /* No power keys, ever (fpgahub treats their absence as deliberate). */
    CHECK(strstr(out, "\"mv\"") == NULL && strstr(out, "\"ma\"") == NULL &&
          strstr(out, "power") == NULL);
    CHECK(out[strlen(out) - 1] == '\n');
    /* Extra keys in the request are ignored, like every verb. */
    char out2[MPS3_CTRL_RESP_MAX];
    CHECK(dispatch("{\"op\":\"stats\",\"x\":1}", out2, sizeof(out2)) == 1);
    CHECK(strstr(out2, "\"ok\":true") != NULL);
}

static void test_stats_each_key_maps_to_its_source(void)
{
    char out[MPS3_CTRL_RESP_MAX], buf[32];

    /* --- scenario A: every bit/flag CLEAR --------------------------------- */
    boot();
    mock_time_set_ms(1234u);
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jint(out, "up_ms") == 1234);
    jstr(out, "sid", buf, sizeof(buf)); CHECK(strcmp(buf, "0x3f1a560f") == 0);
    jstr(out, "rm", buf, sizeof(buf));  CHECK(strcmp(buf, "0x00000000") == 0);
    CHECK(jbool(out, "rm_ok") == 0);
    CHECK(jbool(out, "lock") == 0);
    CHECK(jint(out, "clk_sel") == 0);
    CHECK(jbool(out, "mmcm") == 0);
    CHECK(jbool(out, "clk_alive") == 0);
    CHECK(jbool(out, "dut_rst") == 0);   /* clkrst_init() holds all resets  */
    CHECK(jbool(out, "rp_rst") == 1);    /* STATUS.rp_in_reset reads 0      */
    CHECK(jbool(out, "decpl") == 0);
    CHECK(jbool(out, "link") == 0);
    CHECK(jint(out, "spd") == 0);
    CHECK(jbool(out, "fdx") == 0);
    jstr(out, "mac", buf, sizeof(buf)); CHECK(strcmp(buf, "000000000000") == 0);
    jstr(out, "swap", buf, sizeof(buf)); CHECK(strcmp(buf, "idle") == 0);
    CHECK(jbool(out, "swap_ok") == 0);   /* no swap has completed           */
    CHECK(jint(out, "swap_n") == 0);
    CHECK(jint(out, "icap") == 0);
    CHECK(jint(out, "rxdrop") == 0);
    CHECK(jint(out, "txerr") == 0);
    jstr(out, "swap_err", buf, sizeof(buf)); CHECK(strcmp(buf, "") == 0);
    CHECK(jint(out, "dut_mhz") == 50);   /* BD power-on DUT clock           */

    /* --- scenario B: every bit/flag SET, each from its own source ---------- */
    boot();
    mock_time_set_ms(98765u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS,
                   DFXCTL_RM_STATUS_RM_ID_VALID | DFXCTL_RM_STATUS_DUT_LOCKUP);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DUT_RESETN);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_DUT_CLK_SEL, 0x102u); /* masked to 8 bits */
    s_net.link = 1; s_net.spd = 100u; s_net.fdx = 1;
    {
        const uint8_t mac[6] = { 0x00, 0x02, 0xf7, 0xef, 0x44, 0x1c };
        memcpy(s_net.mac, mac, 6);
    }
    {
        mps3_diag_t d;
        memset(&d, 0, sizeof(d));
        d.rx_drop_frames = 7u;
        d.tx_errors      = 9u;
        mps3_diag_publish(&d);
    }
    g_shell_state.current_rm_id = 0x01000001u;
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jint(out, "up_ms") == 98765);
    jstr(out, "rm", buf, sizeof(buf)); CHECK(strcmp(buf, "0x01000001") == 0);
    CHECK(jbool(out, "rm_ok") == 1);
    CHECK(jbool(out, "lock") == 1);
    CHECK(jint(out, "clk_sel") == 2);
    CHECK(jbool(out, "mmcm") == 1);
    CHECK(jbool(out, "clk_alive") == 1);
    CHECK(jbool(out, "dut_rst") == 1);
    CHECK(jbool(out, "rp_rst") == 0);    /* in reset => NOT released        */
    CHECK(jbool(out, "decpl") == 1);
    CHECK(jbool(out, "link") == 1);
    CHECK(jint(out, "spd") == 100);
    CHECK(jbool(out, "fdx") == 1);
    jstr(out, "mac", buf, sizeof(buf)); CHECK(strcmp(buf, "0002f7ef441c") == 0);
    CHECK(jint(out, "rxdrop") == 7);
    CHECK(jint(out, "txerr") == 9);

    /* link DOWN forces spd 0 / fdx false even if the seam left stale values. */
    s_net.link = 0;
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jbool(out, "link") == 0 && jint(out, "spd") == 0 && jbool(out, "fdx") == 0);

    /* --- dut_mhz follows set_clk ------------------------------------------ */
    CHECK(dispatch("{\"op\":\"set_clk\",\"preset\":\"100mhz\"}", out, sizeof(out)) == 1);
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jint(out, "dut_mhz") == 100);
    CHECK(dispatch("{\"op\":\"set_clk\",\"preset\":\"25mhz\"}", out, sizeof(out)) == 1);
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jint(out, "dut_mhz") == 25);
}

static void test_stats_swap_fields_follow_the_fsm(void)
{
    char out[MPS3_CTRL_RESP_MAX], buf[32];
    boot();

    /* mid-swap: the state name is on the wire */
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}", out, sizeof(out)) == 0);
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    jstr(out, "swap", buf, sizeof(buf));
    CHECK(strcmp(buf, "gate") == 0);

    /* a successful swap: swap_ok, swap_n, icap, clr_ok */
    pump_swap(1u);
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    jstr(out, "swap", buf, sizeof(buf)); CHECK(strcmp(buf, "idle") == 0);
    CHECK(jbool(out, "swap_ok") == 1);
    CHECK(jint(out, "swap_n") == 1);
    CHECK(jint(out, "icap") == (long)swap_fsm_icap_bytes());
    CHECK(jbool(out, "clr_ok") == 1);
    jstr(out, "swap_err", buf, sizeof(buf)); CHECK(strcmp(buf, "") == 0);

    /* a verify failure: swap_ok false, swap_n counts it, swap_err names the
     * state that FOUND the failure (verify), not the reisolate that followed. */
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}", out, sizeof(out)) == 0);
    pump_swap(99u);
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jbool(out, "swap_ok") == 0);
    CHECK(jint(out, "swap_n") == 2);
    jstr(out, "swap_err", buf, sizeof(buf)); CHECK(strcmp(buf, "verify") == 0);
    CHECK(jbool(out, "decpl") == 1);     /* parked isolated -- visible now  */
    CHECK(jbool(out, "rp_rst") == 0);
}

/* svc_max_us / svc_skipped are WINDOWED: since the previous `stats`. */
static uint32_t s_slow_us;
static void svc_slow(void) { mock_time_advance_us(s_slow_us); }
static void test_stats_service_window(void)
{
    static const mps3_service_t tbl[] = {
        { "slow", svc_slow, 1000u },
    };
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    mps3_service_install(tbl, 1u);
    s_slow_us = 5000u;                    /* over budget: 3 passes => sick */
    for (int i = 0; i < 3; i++) mps3_service_run_pass();
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jint(out, "svc_max_us") >= 5000);
    CHECK(jint(out, "svc_skipped") == 1);
    /* the window restarted: nothing new happened */
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof(out)) == 1);
    CHECK(jint(out, "svc_max_us") == 0);
    CHECK(jint(out, "svc_skipped") == 0);
    mps3_service_install(NULL, 0u);
}

static void test_stats_worst_line_fits(void)
{
    /* Every numeric field at max width, every string at its buffer's max. */
    mps3_ctrl_response_t r;
    char out[MPS3_CTRL_RESP_MAX];
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_STATS;
    r.ok = 1;
    r.stats.up_ms = r.stats.sid = r.stats.rm = 0xFFFFFFFFu;
    r.stats.clk_sel = r.stats.spd = r.stats.swap_n = r.stats.icap = 0xFFFFFFFFu;
    r.stats.rxdrop = r.stats.txerr = r.stats.dut_mhz = 0xFFFFFFFFu;
    r.stats.svc_max_us = r.stats.svc_skipped = 0xFFFFFFFFu;
    r.stats.rm_ok = r.stats.lock = r.stats.mmcm = r.stats.clk_alive = 0;
    r.stats.dut_rst = r.stats.rp_rst = r.stats.decpl = r.stats.link = 0;
    r.stats.fdx = r.stats.swap_ok = r.stats.clr_ok = 0;   /* "false" is longer */
    memset(r.stats.swap, 'x', sizeof(r.stats.swap) - 1);
    memset(r.stats.swap_err, '"', sizeof(r.stats.swap_err) - 1); /* escapes double */
    int n = mps3_ctrl_encode_response(&r, out, sizeof(out));
    CHECK(n > 0);
    CHECK(n < MPS3_CTRL_RESP_MAX);
    printf("  stats worst line = %d B (RESP_MAX %d)\n", n, MPS3_CTRL_RESP_MAX);
    /* and a buffer one byte short of it fails CLOSED, never truncated */
    char small[1024];
    CHECK(mps3_ctrl_encode_response(&r, small, n) < 0);
    CHECK(small[0] == '\0');
}

/* ==========================================================================
 * log (A7)
 * ========================================================================== */
static void hex_to_bytes(const char *hex, char *out, size_t cap)
{
    size_t i = 0;
    while (hex[2 * i] && hex[2 * i] != '"' && i + 1 < cap) {
        unsigned b;
        sscanf(hex + 2 * i, "%2x", &b);
        out[i++] = (char)b;
    }
    out[i] = '\0';
}

static void test_log_serves_the_ring_in_chunks(void)
{
    char out[MPS3_CTRL_RESP_MAX], keys[128], text[300];
    boot();
    mps3_log_write("hello shell\r\n", 13u);

    CHECK(dispatch("{\"op\":\"log\"}", out, sizeof(out)) == 1);
    jkeys(out, keys, sizeof(keys));
    CHECK(strcmp(keys, "ok,off,n,more,dropped,data") == 0);
    CHECK(jint(out, "off") == 0 && jint(out, "n") == 13);
    CHECK(jbool(out, "more") == 0 && jint(out, "dropped") == 0);
    hex_to_bytes(jval(out, "data") + 1, text, sizeof(text));
    CHECK(strcmp(text, "hello shell\r\n") == 0);

    /* Explicit off: the tail of it. */
    CHECK(dispatch("{\"op\":\"log\",\"off\":6}", out, sizeof(out)) == 1);
    CHECK(jint(out, "off") == 6 && jint(out, "n") == 7);
    hex_to_bytes(jval(out, "data") + 1, text, sizeof(text));
    CHECK(strcmp(text, "shell\r\n") == 0);

    /* At the head: empty, same key set, off = head. */
    CHECK(dispatch("{\"op\":\"log\",\"off\":13}", out, sizeof(out)) == 1);
    CHECK(jint(out, "n") == 0 && jint(out, "off") == 13);
    CHECK(strstr(out, "\"data\":\"\"}") != NULL);

    /* > one chunk: 256 now, `more`, then the rest from the returned offset. */
    for (int i = 0; i < 300; i++) mps3_log_putc((char)('a' + (i % 26)));
    CHECK(dispatch("{\"op\":\"log\",\"off\":13}", out, sizeof(out)) == 1);
    CHECK(jint(out, "n") == 256 && jbool(out, "more") == 1);
    CHECK((int)strlen(out) < MPS3_CTRL_RESP_MAX);
    CHECK(dispatch("{\"op\":\"log\",\"off\":269}", out, sizeof(out)) == 1);
    CHECK(jint(out, "n") == 44 && jbool(out, "more") == 0);

    /* A bad off is bad args, not a silent 0. */
    CHECK(dispatch("{\"op\":\"log\",\"off\":-1}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);
    CHECK(dispatch("{\"op\":\"log\",\"off\":\"0\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);
}

static void test_log_overrun_reports_dropped_and_skips_forward(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    for (uint32_t i = 0; i < MPS3_LOG_RING_BYTES + 100u; i++) mps3_log_putc('x');
    CHECK(dispatch("{\"op\":\"log\",\"off\":0}", out, sizeof(out)) == 1);
    CHECK(jint(out, "dropped") == 100);
    CHECK(jint(out, "off") == 100);       /* skipped to the oldest retained  */
    CHECK(jint(out, "n") == 256 && jbool(out, "more") == 1);
}

/* ==========================================================================
 * reboot (A5)
 * ========================================================================== */
static uint32_t s_tbr;
static int wdog_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (!is_write && off == WDOG_TBR) {
        *val = s_tbr++;                   /* a live, free-running timebase   */
        return 1;
    }
    return 0;                             /* TWCSR0/1: plain slots           */
}

static void test_reboot_fsm(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    /* The WDOG bit fields, pinned to the VENDOR DRIVER (Vitis 2024.1 wdttb_v5_8
     * xwdttb_hw.h: WRS 0x8, WDS 0x4, EWDT1 0x2, EWDT2 0x1). platform_regs.h had
     * them wrong until this verb became the first code to write them: the old
     * "enable" was a KICK (bit 2 = WDS) plus a reserved bit, so `reboot` would
     * have answered ok and never rebooted. */
    CHECK(WDOG_TWCSR0_WRS   == 0x8u);
    CHECK(WDOG_TWCSR0_WDS   == 0x4u);
    CHECK(WDOG_TWCSR0_EWDT1 == 0x2u);
    CHECK(WDOG_TWCSR1_EWDT2 == 0x1u);

    /* No watchdog behind 0x44B4 (TBR does not move): refuse, never promise. */
    boot();
    CHECK(dispatch("{\"op\":\"reboot\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no watchdog\"}\n") == 0);
    CHECK(coordinator_reboot_state() == 0);

    /* Refused mid-swap. */
    boot();
    mock_regs_set_hook(MPS3_WDOG_BASE, wdog_hook, NULL);
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}", out, sizeof(out)) == 0);
    CHECK(dispatch("{\"op\":\"reboot\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"EBUSY\"}\n") == 0);
    CHECK(coordinator_reboot_state() == 0);
    pump_swap(1u);

    /* Idle: the REPLY comes first, the watchdog is untouched until the delay. */
    mock_time_set_ms(1000u);
    CHECK(dispatch("{\"op\":\"reboot\"}", out, sizeof(out)) == 1);
    {
        char want[64];
        snprintf(want, sizeof(want), "{\"ok\":true,\"in_ms\":%u}\n",
                 (unsigned)(MPS3_REBOOT_ARM_DELAY_MS + 2u * MPS3_WDT_STAGE_MS));
        CHECK(strcmp(out, want) == 0);
    }
    CHECK(coordinator_reboot_state() == 1);
    coordinator_reboot_poll();
    CHECK(mock_regs_peek(MPS3_WDOG_BASE, WDOG_TWCSR0) == 0u);
    CHECK(mock_regs_peek(MPS3_WDOG_BASE, WDOG_TWCSR1) == 0u);
    CHECK(!mps3_service_kick_inhibited());

    /* A repeat before arming is idempotent: ok, a SHORTER in_ms, one timer. */
    mock_time_set_ms(1000u + 40u);
    CHECK(dispatch("{\"op\":\"reboot\"}", out, sizeof(out)) == 1);
    CHECK(jint(out, "in_ms") == (long)(MPS3_REBOOT_ARM_DELAY_MS - 40u + 2u * MPS3_WDT_STAGE_MS));

    /* After the delay the poll ARMS: both enable halves set, WDS not written
     * (it is W1C -- writing 1 there would be a kick), kicks inhibited. */
    mock_time_set_ms(1000u + MPS3_REBOOT_ARM_DELAY_MS);
    coordinator_reboot_poll();
    CHECK(coordinator_reboot_state() == 2);
    CHECK(mock_regs_peek(MPS3_WDOG_BASE, WDOG_TWCSR0) == WDOG_TWCSR0_EWDT1);
    CHECK(mock_regs_peek(MPS3_WDOG_BASE, WDOG_TWCSR1) == WDOG_TWCSR1_EWDT2);
    CHECK((mock_regs_peek(MPS3_WDOG_BASE, WDOG_TWCSR0) & WDOG_TWCSR0_WDS) == 0u);
    CHECK(mps3_service_kick_inhibited());

    /* ...and a pass that would have kicked does NOT. */
    {
        static const mps3_service_t tbl[] = { { "noop", NULL, 0u } };
        mps3_service_install(tbl, 1u);
        mps3_service_inhibit_kick();        /* install() reset the stats   */
        uint32_t k0 = mps3_service_kicks();
        mps3_service_run_pass();
        CHECK(mps3_service_kicks() == k0);
        mps3_service_install(NULL, 0u);
    }

    /* The poll is reached through the control-channel service itself. */
    boot();
    mock_regs_set_hook(MPS3_WDOG_BASE, wdog_hook, NULL);
    mock_time_set_ms(5000u);
    CHECK(dispatch("{\"op\":\"reboot\"}", out, sizeof(out)) == 1);
    mock_time_set_ms(5000u + MPS3_REBOOT_ARM_DELAY_MS);
    coordinator_net_poll();
    CHECK(coordinator_reboot_state() == 2);
    mock_regs_set_hook(MPS3_WDOG_BASE, NULL, NULL);
}

/* ==========================================================================
 * touch_cal on a TOUCH-less build
 * ========================================================================== */
static void test_touch_cal_declines_without_touch(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    CHECK(dispatch("{\"op\":\"touch_cal\",\"act\":\"get\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"touch not present\"}\n") == 0);
    /* decode still enforces its arguments first */
    CHECK(dispatch("{\"op\":\"touch_cal\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);
}

int main(void)
{
    test_version_features_v011_bits();
    test_stats_key_shape_and_order();
    test_stats_each_key_maps_to_its_source();
    test_stats_swap_fields_follow_the_fsm();
    test_stats_service_window();
    test_stats_worst_line_fits();
    test_log_serves_the_ring_in_chunks();
    test_log_overrun_reports_dropped_and_skips_forward();
    test_reboot_fsm();
    test_touch_cal_declines_without_touch();
    printf("test_v011_dispatch: ALL PASS (%d checks)\n", s_checks);
    return 0;
}
