/*
 * test_touch_cal_dispatch.c -- the v0.11 `touch_cal` verb on a TOUCH=1 build,
 * through the REAL dispatcher, codec and touch driver (touch.c), exactly the
 * -DMPS3_HAS_TOUCH/-DMPS3_HAS_CLCD combination PRODUCT=1 compiles.
 *
 * Covered: get (the calibration in force), set (validated, round-trips through
 * get and through touch_map_raw), the rejections (shift range -- including a
 * shift that would WRAP to 0 if narrowed to uint8_t first -- coefficient range,
 * singular matrix, bad act, missing coefficient), default (back to the header
 * constant TOUCH_CALIB_DEFAULT) and raw (the JTAG-visible raw statics, mapped).
 * Also `stats` on this build: touch_ok / touch_bus_lost / touch_recoveries
 * through a latch and a recovery (2026-09-24, D1), and their worst-case width.
 * The raw sample's capture inside the driver is covered in test_touch.c, where
 * the AXI-IIC/STMPE811 model lives.
 * Fails on the pre-v0.11 tree: the verb did not decode.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../common/platform_regs.h"
#include "../touch/touch.h"
#include "mock_regs.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

uint32_t mps3_shell_static_id(void) { return 0x3F1A560Fu; }

/* clcd.c's status page links against these (the firmware/test fake pattern). */
int smsc911x_link_up(void) { return 0; }
int smsc911x_mii_read(uint32_t reg, uint16_t *v) { (void)reg; if (v) *v = 0u; return 0; }
void mps3_platform_mac(uint8_t mac[6]) { memset(mac, 0, 6); }

static void boot(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    overlay_manifest_info_t greybox = {
        .static_id = 0x3F1A560Fu, .rm_id = 0, .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_set_greybox(&greybox, 0);
    coordinator_init();
    touch_init();   /* seeds TOUCH_CALIB_DEFAULT (no part answers: tsc not ready) */
}

static int dispatch(const char *line, char *out, int out_len)
{
    return coordinator_dispatch_line(line, (int)strlen(line), out, out_len);
}

/* TOUCH_CALIB_DEFAULT = the 2026-09-24 silicon fit (touch.h; evidence
 * docs/evidence/2026-09-w3/touch_cal_20260924.txt section 4). */
static const char DEFAULT_LINE[] =
    "{\"ok\":true,\"ax\":21,\"bx\":377,\"cx\":-158863,\"ay\":-281,\"by\":-2,"
    "\"cy\":1091113,\"shift\":12}\n";

static void test_version_claims_touch_cal(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    CHECK(dispatch("{\"op\":\"version\"}", out, sizeof out) == 1);
    CHECK(strstr(out, "\"features\":[\"clcd\",\"touch\",\"jtag_server\",\"xvc_dbgbr\","
                      "\"stats\",\"log\",\"reboot\",\"touch_cal\",\"usd\"],") != NULL);
}

static void test_get_reports_the_header_default(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    CHECK(dispatch("{\"op\":\"touch_cal\",\"act\":\"get\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, DEFAULT_LINE) == 0);
}

static void test_set_round_trips_and_takes_effect(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    uint16_t x, y;
    boot();
    /* A plausible three-point fit with cross terms and NEGATIVE entries. */
    const char *set =
        "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":-5461,\"bx\":37,\"cx\":1310720,"
        "\"ay\":12,\"by\":4096,\"cy\":-65536,\"shift\":14}";
    CHECK(dispatch(set, out, sizeof out) == 1);
    const char *want =
        "{\"ok\":true,\"ax\":-5461,\"bx\":37,\"cx\":1310720,\"ay\":12,\"by\":4096,"
        "\"cy\":-65536,\"shift\":14}\n";
    CHECK(strcmp(out, want) == 0);                      /* echo = what is in force */
    CHECK(dispatch("{\"op\":\"touch_cal\",\"act\":\"get\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, want) == 0);                      /* ...and it stuck         */

    /* It is the calibration the DRIVER uses (touch_map_raw), not a copy. */
    {
        touch_calib_t c;
        touch_get_calibration(&c);
        CHECK(c.ax == -5461 && c.bx == 37 && c.cx == 1310720 &&
              c.ay == 12 && c.by == 4096 && c.cy == -65536 && c.shift == 14u);
        touch_map_raw(100u, 200u, &x, &y);
        /* x_cal = (-5461*100 + 37*200 + 1310720) >> 14 = 772020>>14 = 47 ; 319-47 = 272
         * y_cal = (12*100 + 4096*200 - 65536) >> 14 = 754864>>14 = 46 ; 239-46 = 193 */
        CHECK(x == 272u && y == 193u);
    }

    /* default: back to the header constant. */
    CHECK(dispatch("{\"op\":\"touch_cal\",\"act\":\"default\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, DEFAULT_LINE) == 0);
}

static void test_set_rejections_change_nothing(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    struct { const char *line; const char *err; } bad[] = {
        /* shift above the range */
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":320,\"bx\":0,\"cx\":0,"
          "\"ay\":0,\"by\":240,\"cy\":0,\"shift\":25}", "bad shift" },
        /* 256 would WRAP to 0 in a uint8_t -- must be rejected, not wrapped */
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":320,\"bx\":0,\"cx\":0,"
          "\"ay\":0,\"by\":240,\"cy\":0,\"shift\":256}", "bad shift" },
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":320,\"bx\":0,\"cx\":0,"
          "\"ay\":0,\"by\":240,\"cy\":0,\"shift\":-1}", "bad shift" },
        /* a coefficient that could overflow touch_map_raw's int32 math */
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":131073,\"bx\":0,\"cx\":0,"
          "\"ay\":0,\"by\":240,\"cy\":0,\"shift\":12}", "coef range" },
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":320,\"bx\":0,\"cx\":536870913,"
          "\"ay\":0,\"by\":240,\"cy\":0,\"shift\":12}", "offset range" },
        /* singular: the panel would collapse onto a line */
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":320,\"bx\":640,\"cx\":0,"
          "\"ay\":120,\"by\":240,\"cy\":0,\"shift\":12}", "singular" },
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":0,\"bx\":0,\"cx\":0,"
          "\"ay\":0,\"by\":240,\"cy\":0,\"shift\":12}", "singular" },
        /* an unknown act */
        { "{\"op\":\"touch_cal\",\"act\":\"calibrate\"}", "bad act" },
        /* set without all seven coefficients is a DECODE failure */
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":320,\"bx\":0,\"cx\":0,"
          "\"ay\":0,\"by\":240,\"cy\":0}", "bad args" },
        /* wrong-typed coefficient */
        { "{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":\"320\",\"bx\":0,\"cx\":0,"
          "\"ay\":0,\"by\":240,\"cy\":0,\"shift\":12}", "bad args" },
    };
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        char want[96];
        snprintf(want, sizeof want, "{\"ok\":false,\"err\":\"%s\"}\n", bad[i].err);
        CHECK(dispatch(bad[i].line, out, sizeof out) == 1);
        if (strcmp(out, want) != 0) {
            fprintf(stderr, "case %zu: got %s want %s", i, out, want);
        }
        CHECK(strcmp(out, want) == 0);
        /* ...and the calibration in force did NOT move. */
        CHECK(dispatch("{\"op\":\"touch_cal\",\"act\":\"get\"}", out, sizeof out) == 1);
        CHECK(strcmp(out, DEFAULT_LINE) == 0);
    }
}

static void test_raw_reports_the_statics_mapped(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    CHECK(dispatch("{\"op\":\"touch_cal\",\"act\":\"raw\"}", out, sizeof out) == 1);
    /* No sample yet: zeros, seen 0 -- and (0,0) through the default map + flip.
     * x_cal = -158863 >> 12 = -39 -> clamp 0 -> flip 319; y_cal = 1091113 >> 12 =
     * 266 -> clamp 239 -> flip 0: the TOP-right pixel (the panel's raw origin,
     * with its axes swapped and mirrored), which is what the reply must say. */
    CHECK(strcmp(out, "{\"ok\":true,\"raw_x\":0,\"raw_y\":0,\"raw_z\":0,\"seen\":0,"
                      "\"x\":319,\"y\":0}\n") == 0);
}

/* ==========================================================================
 * `stats` on a TOUCH=1 build: touch_ok / touch_bus_lost / touch_recoveries
 * (2026-09-24, D1), through the REAL dispatcher, codec and touch driver.
 *
 * The part is a minimal AXI-IIC (PG090 dynamic controller) + STMPE811
 * responder: it answers CHIP_ID 0x0811 and TSC_CTRL 0x01 (enabled, no touch)
 * while `alive`, and serves NOTHING when not (every read then times out on the
 * fake clock, as a wedged bus does). test_touch.c holds the faithful model and
 * the recovery's own proofs; this proves the wire reports them.
 * ========================================================================== */
static struct {
    int      alive;
    int      reg, have_reg, rd;
    uint8_t  q[16];
    unsigned qh, qt;
} tsc;

static int tsc_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *val)
{
    (void)c; (void)b;
    if (wr) {
        if (off == TOUCH_IIC_TX_FIFO) {
            const uint32_t v = *val;
            if (v & IIC_TX_START) {                      /* address byte */
                tsc.rd = (int)(v & 1u);
                if (!tsc.rd) tsc.have_reg = 0;
            } else if (v & IIC_TX_STOP) {                /* last byte / read count */
                if (tsc.rd && tsc.alive) {
                    for (unsigned i = 0u; i < (v & 0xFFu); i++) {
                        uint8_t d = 0u;
                        if (tsc.reg == 0x00) d = (i == 0u) ? 0x08u : 0x11u;  /* CHIP_ID  */
                        else if (tsc.reg == 0x40) d = 0x01u;                 /* TSC_CTRL */
                        tsc.q[tsc.qt++ & 15u] = d;
                    }
                }
                tsc.rd = 0; tsc.have_reg = 0;
            } else if (!tsc.have_reg) {                  /* register pointer */
                tsc.reg = (int)(v & 0xFFu); tsc.have_reg = 1;
            }
        }
        return 1;
    }
    if (off == TOUCH_IIC_SR) {
        *val = IIC_SR_TX_FIFO_EMPTY | ((tsc.qh == tsc.qt) ? IIC_SR_RX_FIFO_EMPTY : 0u);
    } else if (off == TOUCH_IIC_RX_FIFO) {
        *val = (tsc.qh == tsc.qt) ? 0u : tsc.q[tsc.qh++ & 15u];
    } else {
        *val = 0u;
    }
    return 1;
}

/* The line must END with the touch trio, in this order, after svc_skipped (no
 * os_up_ms on a bare-metal codec). */
static int stats_ends_with(const char *out, const char *tail)
{
    const size_t n = strlen(out), t = strlen(tail);
    return n >= t && strcmp(out + n - t, tail) == 0 &&
           strstr(out, "\"svc_skipped\":") != NULL &&
           strstr(out, "\"svc_skipped\":") < strstr(out, "\"touch_ok\":");
}

static void test_stats_reports_touch_liveness(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    boot();
    memset(&tsc, 0, sizeof tsc);
    tsc.alive = 1;
    mock_regs_set_hook(MPS3_TOUCH_BASE, tsc_hook, 0);
    mock_regs_set_us_per_read(1u);          /* IIC waits cost real (fake) time */
    touch_init();
    CHECK(touch_chip_id() == 0x0811u);

    /* Live. (Absolute counts: the only test in this binary that latches.) */
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof out) == 1);
    CHECK(stats_ends_with(out, ",\"touch_ok\":true,\"touch_bus_lost\":0,\"touch_recoveries\":0}\n"));
    for (int i = 0; i < 5; i++) { mock_time_advance_ms(10u); (void)touch_poll(); }
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof out) == 1);
    CHECK(stats_ends_with(out, ",\"touch_ok\":true,\"touch_bus_lost\":0,\"touch_recoveries\":0}\n"));

    /* The bus wedges: TOUCH_BUS_FAIL_LIMIT failing polls fire the latch -- and
     * `stats` now SAYS so (on 0x72BB0A36 it took a JTAG read of the statics). */
    tsc.alive = 0;
    for (unsigned i = 0u; i < TOUCH_BUS_FAIL_LIMIT; i++) { mock_time_advance_ms(10u); (void)touch_poll(); }
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof out) == 1);
    CHECK(stats_ends_with(out, ",\"touch_ok\":false,\"touch_bus_lost\":1,\"touch_recoveries\":0}\n"));

    /* The bus comes back; the periodic re-init (first slot +5 s) recovers it. */
    tsc.alive = 1;
    for (int i = 0; i < 600; i++) { mock_time_advance_ms(10u); (void)touch_poll(); }
    CHECK(dispatch("{\"op\":\"stats\"}", out, sizeof out) == 1);
    CHECK(stats_ends_with(out, ",\"touch_ok\":true,\"touch_bus_lost\":1,\"touch_recoveries\":1}\n"));

    mock_regs_set_us_per_read(0u);
    mock_regs_set_hook(MPS3_TOUCH_BASE, 0, 0);
}

/* The touch trio's worst case, on top of test_v011_dispatch's 514 B line. */
static void test_stats_worst_line_with_touch_fits(void)
{
    mps3_ctrl_response_t r;
    char out[MPS3_CTRL_RESP_MAX];
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_STATS;
    r.ok = 1;
    r.stats.up_ms = r.stats.sid = r.stats.rm = 0xFFFFFFFFu;
    r.stats.clk_sel = r.stats.spd = r.stats.swap_n = r.stats.icap = 0xFFFFFFFFu;
    r.stats.rxdrop = r.stats.txerr = r.stats.dut_mhz = 0xFFFFFFFFu;
    r.stats.svc_max_us = r.stats.svc_skipped = 0xFFFFFFFFu;
    memset(r.stats.swap, 'x', sizeof(r.stats.swap) - 1);
    memset(r.stats.swap_err, '"', sizeof(r.stats.swap_err) - 1);
    r.stats.touch_present = 1;
    r.stats.touch_ok = 0;                                        /* "false" */
    r.stats.touch_bus_lost = r.stats.touch_recoveries = 0xFFFFFFFFu;
    int n = mps3_ctrl_encode_response(&r, out, sizeof(out));
    CHECK(n > 0 && n < MPS3_CTRL_RESP_MAX);
    printf("  stats worst line with touch = %d B (RESP_MAX %d)\n", n, MPS3_CTRL_RESP_MAX);
    char small[1024];
    CHECK(mps3_ctrl_encode_response(&r, small, n) < 0 && small[0] == '\0');
}

int main(void)
{
    test_version_claims_touch_cal();
    test_get_reports_the_header_default();
    test_set_round_trips_and_takes_effect();
    test_set_rejections_change_nothing();
    test_raw_reports_the_statics_mapped();
    test_stats_reports_touch_liveness();
    test_stats_worst_line_with_touch_fits();
    printf("test_touch_cal_dispatch: ALL PASS (%d checks)\n", s_checks);
    return 0;
}
