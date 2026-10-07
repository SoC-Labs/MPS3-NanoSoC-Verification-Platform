/*
 * test_locate.c — `locate` (net-protocol.md v0.16; the Harness Manager's R3), the
 * REAL locate_linux.c over a fake clock, the REAL clcd_kvm.c over the mock
 * registers (the backlight is CLCDKVM.CTRL[5]) and the REAL clcd.c renderer:
 *
 *   1. THE TIMER: one backlight edge per 250 ms (a 2 Hz blink), ONE CTRL write per
 *      edge, the first edge on the first tick; it ends on time, lit;
 *   2. REPLACE: a new locate replaces the old -- its time and who -- and the blink
 *      carries on (no reset, no extra edge);
 *   3. STOP (s 0) and the RESTORE: the backlight is ON after every end, whatever
 *      phase it ended in. NEGATIVE CONTROL: with the restore switched off
 *      (g_locate_negctl_no_restore) an end in the OFF phase leaves it OFF -- the
 *      same check then FAILS, so it can see a missing restore;
 *   4. THE TAP: a tap during a locate is "found it" (it ends it, lit, and is not
 *      also a nav action); with no locate the tap is the nav bar's again;
 *   5. THE BANNER: "IDENTIFY: <who>" inverted on rows 10-12 of the status page when
 *      no fault banner is up; a fault banner OUTRANKS it; the apps page gets it
 *      over its rows 10-12; gone when the locate ends;
 *   6. the verb's refusals (s out of range / missing, a bad who).
 */
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../../../../firmware/clcd/clcd.h"
#include "../../../../../firmware/common/diag.h"
#include "../../../../../firmware/common/net_proto.h"
#include "../../../../../firmware/common/platform_regs.h"
#include "../../../../../firmware/coordinator/coordinator.h"
#include "../../../../../firmware/coordinator/swap_fsm.h"
#include "../../../../../firmware/test/mock_regs.h"
#include "../harnessd.h"

static int g_fail, g_n;
#define CHECK(c, ...) do { g_n++; if (!(c)) { g_fail++; printf("  FAIL %s:%d: %s -- ", \
    __FILE__, __LINE__, #c); printf(__VA_ARGS__); printf("\n"); } } while (0)

/* ---- what locate_linux.c / clcd.c need from harnessd and the firmware ------ */
static uint64_t g_now_us = 1000000u;
uint64_t harnessd_now_us64(void) { return g_now_us; }
static char g_log[8192];
void harnessd_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    size_t l = strlen(g_log);
    vsnprintf(g_log + l, sizeof(g_log) - l, fmt, ap);
    va_end(ap);
}
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
uint32_t swap_fsm_icap_bytes(void) { return 0u; }
static int s_link_up = 1;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t r, uint16_t *v) { if (v) *v = (r == 5u) ? 0x01E1u : 0u; return 0; }
void mps3_platform_mac(uint8_t mac[6]) { static const uint8_t m[6] = { 2, 0, 0, 0x4D, 0x50, 0x53 }; memcpy(mac, m, 6); }

/* ---- the backlight: CLCDKVM.CTRL[5], and every write to CTRL counted -------- */
static unsigned g_ctrl_writes;
static int ctrl_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base; (void)val;
    if (is_write && off == CLCDKVM_CTRL) {
        g_ctrl_writes++;
    }
    return 0;   /* 0 = let the mock perform the access */
}
static int bl(void) { return (mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) & CLCDKVM_CTRL_BACKLIGHT) != 0; }

static const char *locate(const char *json, char *code)
{
    mps3_ctrl_request_t req;
    memset(&req, 0, sizeof(req));
    req.line = json;
    req.line_len = (int)strlen(json);
    const char *body = 0;
    code[0] = '\0';
    const char *err = mps3_locate_op(&req, &body, code, 24);
    return err ? err : body;
}

static void run_for(unsigned ms)   /* the clcd row's tick, every 10 ms (harnessd's idle cadence) */
{
    for (unsigned t = 0; t < ms; t += 10u) {
        harnessd_locate_tick();
        g_now_us += 10000u;
    }
}

static void t_timer_and_restore(void)
{
    char code[24];
    printf("test: the 2 Hz blink, one CTRL write per edge, the end restores the light\n");
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL, CLCDKVM_CTRL_BACKLIGHT | CLCDKVM_CTRL_BL_RST_SRC);
    g_ctrl_writes = 0;
    CHECK(strcmp(locate("{\"op\":\"locate\",\"s\":2,\"who\":\"user@host\"}", code),
                  "\"until_ms\":2000") == 0, "reply body");
    CHECK(g_ctrl_writes == 0 && bl(), "nothing is written by the verb itself");
    harnessd_locate_tick();
    CHECK(!bl() && g_ctrl_writes == 1, "the first edge on the first tick: OFF (%u writes)", g_ctrl_writes);
    unsigned edges = 1, prev = bl();
    for (unsigned t = 0; t < 1990u; t += 10u) {
        g_now_us += 10000u;
        harnessd_locate_tick();
        if ((unsigned)bl() != prev) { edges++; prev = (unsigned)bl(); }
    }
    CHECK(edges == 8 && g_ctrl_writes == 8, "8 edges in 2 s (every 250 ms), one write each: %u / %u",
          edges, g_ctrl_writes);
    CHECK(harnessd_locate_active(), "still running just before 2 s");
    g_now_us += 10000u;
    harnessd_locate_tick();
    CHECK(!harnessd_locate_active() && bl(), "ended on time, lit");
    CHECK(strstr(g_log, "its time is up") != 0, "logged");
}

static void t_replace(void)
{
    char code[24];
    printf("test: a new locate replaces the old (time + who), the blink carries on\n");
    locate("{\"op\":\"locate\",\"s\":5,\"who\":\"alice\"}", code);
    run_for(1000);                              /* 4 edges: OFF ON OFF ON */
    unsigned w = g_ctrl_writes;
    char who[48];
    CHECK(mps3_clcd_locate(who, sizeof(who)) == 1 && strcmp(who, "alice") == 0, "who %s", who);
    CHECK(strcmp(locate("{\"op\":\"locate\",\"s\":3,\"who\":\"bob\"}", code), "\"until_ms\":3000") == 0,
          "replace reply");
    CHECK(g_ctrl_writes == w, "a replace writes nothing itself");
    CHECK(mps3_clcd_locate(who, sizeof(who)) == 1 && strcmp(who, "bob") == 0, "who now %s", who);
    run_for(2990);
    CHECK(harnessd_locate_active(), "the NEW time (3 s from the replace), not the old 5 s from its start");
    run_for(20);
    CHECK(!harnessd_locate_active() && bl(), "ended at the replace's time, lit");
    CHECK(strstr(g_log, "REPLACED") != 0, "the replace is logged");
}

static int stop_in_off_phase_leaves_light(void)
{
    char code[24];
    locate("{\"op\":\"locate\",\"s\":10}", code);
    run_for(100);                               /* first edge: OFF */
    int off = !bl();
    CHECK(strcmp(locate("{\"op\":\"locate\",\"s\":0}", code), "\"until_ms\":0") == 0, "stop reply");
    return off && !harnessd_locate_active() && bl();
}

static void t_stop_and_negative_control(void)
{
    printf("test: s 0 stops it in the OFF phase and the light comes back; NEGATIVE CONTROL\n");
    CHECK(stop_in_off_phase_leaves_light(), "stopped while OFF -> lit");
    g_locate_negctl_no_restore = 1;
    CHECK(!stop_in_off_phase_leaves_light(),
          "NEGATIVE CONTROL: with the restore removed the check must FAIL (the light stays off)");
    g_locate_negctl_no_restore = 0;
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL, CLCDKVM_CTRL_BACKLIGHT | CLCDKVM_CTRL_BL_RST_SRC);
    char code[24];
    CHECK(strcmp(locate("{\"op\":\"locate\",\"s\":0}", code), "\"until_ms\":0") == 0 && bl(),
          "s 0 with nothing running: a no-op");
}

static void t_tap(void)
{
    char code[24];
    printf("test: a tap during a locate is \"found it\"; otherwise the nav bar's\n");
    locate("{\"op\":\"locate\",\"s\":20,\"who\":\"lab\"}", code);
    run_for(300);
    /* the bottom row (14) is the nav button: a tap there is a page change -- but
     * not while a locate runs */
    CHECK(clcd_hittest(10u, 14u * CLCD_GLYPH_H + 4u) == CLCD_ACT_NONE, "consumed, not a page change");
    CHECK(!harnessd_locate_active() && bl(), "ended, lit");
    CHECK(strstr(g_log, "FOUND") != 0 && strstr(g_log, "by lab") != 0, "found is logged with who");
    CHECK(clcd_hittest(10u, 14u * CLCD_GLYPH_H + 4u) == CLCD_ACT_NEXT_PAGE, "no locate: the nav bar again");
}

static void render(char *grid, uint8_t *inv) { clcd_test_render(grid, inv); }

static int row_has(const char *grid, unsigned r, const char *s)
{
    char row[CLCD_COLS + 1];
    memcpy(row, grid + r * CLCD_COLS, CLCD_COLS);
    row[CLCD_COLS] = '\0';
    return strstr(row, s) != 0;
}

static void t_banner(void)
{
    char code[24], grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    printf("test: the IDENTIFY banner (rows 10-12), below the fault banners\n");
    clcd_init();
    g_shell_state.static_id = 0x5A5A0001u;
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    s_link_up = 1;
    clcd_test_set_page(CLCD_PAGE_STATUS);
    render(grid, inv);
    CHECK(!inv[10] && !inv[11] && !inv[12], "healthy + no locate: rows 10-12 not inverted");
    locate("{\"op\":\"locate\",\"s\":10,\"who\":\"user@host\"}", code);
    render(grid, inv);
    CHECK(inv[10] && inv[11] && inv[12] && row_has(grid, 11, "IDENTIFY: user@host"),
          "the identify banner");
    s_link_up = 0;
    render(grid, inv);
    CHECK(row_has(grid, 11, "NETWORK LINK DOWN") && !row_has(grid, 11, "IDENTIFY"),
          "a FAULT banner outranks it");
    s_link_up = 1;
    clcd_test_set_page(CLCD_PAGE_APPS);
    render(grid, inv);
    CHECK(inv[11] && row_has(grid, 11, "IDENTIFY: user@host") && row_has(grid, 0, "APPS"),
          "over the apps page's rows 10-12");
    locate("{\"op\":\"locate\",\"s\":0}", code);
    render(grid, inv);
    CHECK(!row_has(grid, 11, "IDENTIFY") && !inv[11], "gone when it ends");
    clcd_test_set_page(CLCD_PAGE_STATUS);
}

static void t_refusals(void)
{
    char code[24];
    printf("test: refusals\n");
    static const char *const bad[] = {
        "{\"op\":\"locate\"}", "{\"op\":\"locate\",\"s\":31}", "{\"op\":\"locate\",\"s\":-1}",
        "{\"op\":\"locate\",\"s\":\"10\"}", "{\"op\":\"locate\",\"s\":5,\"who\":7}",
        "{\"op\":\"locate\",\"s\":5,\"who\":\"abcdefghijabcdefghijabcdefghijabc\"}",
        "{\"op\":\"locate\",\"s\":5,\"who\":\"tab\\there\"}",
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        const char *r = locate(bad[i], code);
        CHECK(strncmp(r, "invalid ", 8) == 0 && strcmp(code, "invalid") == 0, "%s -> %s (%s)",
              bad[i], r, code);
    }
    CHECK(!harnessd_locate_active(), "a refused locate starts nothing");
}

int main(void)
{
    mock_regs_reset();
    mock_regs_set_hook(MPS3_CLCDKVM_BASE, ctrl_hook, 0);
    t_timer_and_restore();
    t_replace();
    t_stop_and_negative_control();
    t_tap();
    t_banner();
    t_refusals();
    printf("locate: %d checks, %d failures\n", g_n, g_fail);
    return g_fail ? 1 : 0;
}
