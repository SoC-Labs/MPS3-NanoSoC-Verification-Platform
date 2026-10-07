/*
 * test_swd_server.c — host-gcc tests for swd_server.c's REAL byte drain +
 * SWDBB register pokes (W-NET-SEAM; the DRIVE/SAMPLE offsets are real per
 * shell-regmap.md v0.1 / fpga/shell/ip/swd_bb). remote_bitbang bytes go in
 * through a fake_net_if client exactly as OpenOCD would send them; pin
 * effects are observed in mock_regs' SWDBB/CLKRST state.
 *
 * Links: swd_server.c, common/net_if.c, mock_regs.c, fake_net_if.c.
 * g_shell_state defined here (swd_server.c reads swd_gated only).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../swd_server/swd_server.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "fake_net_if.h"

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static void fresh(void)
{
    mock_regs_reset();
    fake_net_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    swd_server_init();
}

static void polls(int n)
{
    for (int i = 0; i < n; i++) {
        swd_server_poll();
    }
}

static uint32_t drive_reg(void)
{
    return mock_regs_peek(MPS3_SWDBB_BASE, SWDBB_DRIVE);
}

static void test_drive_release_and_clock_wiggles(void)
{
    fresh();
    CHECK(drive_reg() == 0); /* reset state 3'b000 (swd_bb README flag #3) */

    int cli = fake_net_connect(MPS3_PORT_SWD);
    CHECK(cli >= 0);

    CHECK(fake_net_send(cli, "O", 1) == 1); /* drive SWDIO */
    polls(2);
    CHECK(drive_reg() == SWDBB_DRIVE_SWDIO_OE);
    CHECK(g_swd_pins.swdio_oe == 1);

    /* d/e/f/g: CLK=bit1, DIO=bit0 of (c-'d') — CONFIRMED vs OpenOCD
     * remote_bitbang.c; exhaustively re-derived in
     * test_openocd_remote_bitbang_conformance(). OE must be preserved across
     * clock wiggles (full write-through composition). */
    CHECK(fake_net_send(cli, "g", 1) == 1); /* CLK=1 DIO=1 */
    polls(2);
    CHECK(drive_reg() == (SWDBB_DRIVE_SWCLK | SWDBB_DRIVE_SWDIO_O | SWDBB_DRIVE_SWDIO_OE));
    CHECK(fake_net_send(cli, "d", 1) == 1); /* CLK=0 DIO=0 */
    polls(2);
    CHECK(drive_reg() == SWDBB_DRIVE_SWDIO_OE);
    CHECK(fake_net_send(cli, "e", 1) == 1); /* CLK=0 DIO=1 */
    polls(2);
    CHECK(drive_reg() == (SWDBB_DRIVE_SWDIO_O | SWDBB_DRIVE_SWDIO_OE));
    CHECK(fake_net_send(cli, "f", 1) == 1); /* CLK=1 DIO=0 */
    polls(2);
    CHECK(drive_reg() == (SWDBB_DRIVE_SWCLK | SWDBB_DRIVE_SWDIO_OE));

    CHECK(fake_net_send(cli, "o", 1) == 1); /* release SWDIO */
    polls(2);
    CHECK((drive_reg() & SWDBB_DRIVE_SWDIO_OE) == 0);
}

static void test_sample_reads_swdbb_and_replies(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_SWD);
    char rsp[4];

    mock_regs_poke(MPS3_SWDBB_BASE, SWDBB_SAMPLE, SWDBB_SAMPLE_SWDIO_I);
    CHECK(fake_net_send(cli, "c", 1) == 1);
    polls(2);
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1);
    CHECK(rsp[0] == '1');

    mock_regs_poke(MPS3_SWDBB_BASE, SWDBB_SAMPLE, 0);
    CHECK(fake_net_send(cli, "c", 1) == 1);
    polls(2);
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1);
    CHECK(rsp[0] == '0');
}

static void test_srst_maps_to_clkrst_dbg_resetn(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_SWD);
    /* Seed dbg_resetn released. */
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DBG_RESETN);

    CHECK(fake_net_send(cli, "s", 1) == 1); /* {trst,srst}={0,1}: assert srst */
    polls(2);
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_DBG_RESETN) == 0); /* 1=released -> cleared = held */
    CHECK(g_swd_pins.srst == 1);

    CHECK(fake_net_send(cli, "r", 1) == 1); /* {0,0}: deassert */
    polls(2);
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_DBG_RESETN) != 0);
    CHECK(g_swd_pins.srst == 0);
}

/* ---------------------------------------------------------------------------
 * OpenOCD remote_bitbang CONFORMANCE (closes the SWD half of I22).
 *
 * The two char->bit mappings used to be flagged "ASSUMED, confirm at bring-up".
 * They are now confirmed against the authoritative implementation, OpenOCD
 * src/jtag/drivers/remote_bitbang.c:
 *
 *     static int remote_bitbang_swd_write(int swclk, int swdio)
 *     {   char c = 'd' + ((swclk ? 0x2 : 0x0) | (swdio ? 0x1 : 0x0));
 *
 *     static int remote_bitbang_reset(int trst, int srst)
 *     {   char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0));
 *
 *     static enum bb_value char_to_int(int c)   // reply to 'c' (swdio sample)
 *     {   case '0': return BB_LOW;  case '1': return BB_HIGH;  default: error
 *
 * This test does NOT hardcode "g means clk=1,dio=1". It RE-DERIVES the byte
 * from OpenOCD's formula for every (swclk,swdio) and (trst,srst) pair, so an
 * inverted mapping introduced later in swd_server.c or net_proto.h fails here
 * rather than at first silicon bring-up.
 *
 * srst -> CLKRST.dbg_resetn (1 = released, so asserting srst CLEARS the bit).
 * trst is meaningless for SWD (no TRST wire) and must be ignored.
 * ------------------------------------------------------------------------- */
static void test_openocd_remote_bitbang_conformance(void)
{
    /* --- swd_write: c = 'd' + (swclk<<1 | swdio) --------------------------- */
    for (int swclk = 0; swclk <= 1; swclk++) {
        for (int swdio = 0; swdio <= 1; swdio++) {
            fresh();
            int cli = fake_net_connect(MPS3_PORT_SWD);
            CHECK(fake_net_send(cli, "O", 1) == 1);   /* drive, so OE is set */
            polls(2);

            char c = (char)('d' + ((swclk ? 0x2 : 0x0) | (swdio ? 0x1 : 0x0)));
            CHECK(fake_net_send(cli, &c, 1) == 1);
            polls(2);

            uint32_t want = SWDBB_DRIVE_SWDIO_OE
                          | (swclk ? SWDBB_DRIVE_SWCLK : 0u)
                          | (swdio ? SWDBB_DRIVE_SWDIO_O : 0u);
            CHECK(drive_reg() == want);
            CHECK(g_swd_pins.swclk == (uint8_t)swclk);
            CHECK(g_swd_pins.swdio_o == (uint8_t)swdio);
        }
    }

    /* Cross-check the enum names still line up with the derived bytes. */
    CHECK(MPS3_BITBANG_CLK0_DIO0 == 'd' + ((0 ? 2 : 0) | (0 ? 1 : 0)));
    CHECK(MPS3_BITBANG_CLK0_DIO1 == 'd' + ((0 ? 2 : 0) | (1 ? 1 : 0)));
    CHECK(MPS3_BITBANG_CLK1_DIO0 == 'd' + ((1 ? 2 : 0) | (0 ? 1 : 0)));
    CHECK(MPS3_BITBANG_CLK1_DIO1 == 'd' + ((1 ? 2 : 0) | (1 ? 1 : 0)));

    /* --- reset: c = 'r' + (trst<<1 | srst); only srst is meaningful -------- */
    for (int trst = 0; trst <= 1; trst++) {
        for (int srst = 0; srst <= 1; srst++) {
            fresh();
            int cli = fake_net_connect(MPS3_PORT_SWD);
            mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL,
                           CLKRST_RESET_CTRL_DBG_RESETN);  /* seed: released */

            char c = (char)('r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0)));
            CHECK(fake_net_send(cli, &c, 1) == 1);
            polls(2);

            uint32_t rc = mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL);
            /* srst asserted => dbg_resetn HELD (bit clear). trst must not matter. */
            CHECK(((rc & CLKRST_RESET_CTRL_DBG_RESETN) == 0) == (srst == 1));
            CHECK(g_swd_pins.srst == (uint8_t)srst);
        }
    }

    CHECK(MPS3_BITBANG_RESET_00 == 'r' + 0);
    CHECK(MPS3_BITBANG_RESET_01 == 'r' + 1);   /* srst */
    CHECK(MPS3_BITBANG_RESET_10 == 'r' + 2);   /* trst */
    CHECK(MPS3_BITBANG_RESET_11 == 'r' + 3);

    /* --- sample reply MUST be ASCII '0'/'1' (char_to_int errors otherwise) -- */
    for (int level = 0; level <= 1; level++) {
        fresh();
        int cli = fake_net_connect(MPS3_PORT_SWD);
        mock_regs_poke(MPS3_SWDBB_BASE, SWDBB_SAMPLE,
                       level ? SWDBB_SAMPLE_SWDIO_I : 0u);
        CHECK(fake_net_send(cli, "c", 1) == 1);
        polls(2);
        uint8_t rsp[2] = {0, 0};
        CHECK(fake_net_recv(cli, rsp, 1) == 1);
        CHECK(rsp[0] == (level ? '1' : '0'));
        CHECK(rsp[0] == 0x30 + level);   /* ASCII, not a raw 0/1 byte */
    }
}

static void test_led_ignored_quit_closes_junk_drops(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_SWD);
    CHECK(fake_net_send(cli, "Bb", 2) == 2); /* LED on/off: accepted-ignored */
    polls(2);
    CHECK(!fake_net_fw_closed(cli));

    CHECK(fake_net_send(cli, "Q", 1) == 1);
    polls(2);
    CHECK(fake_net_fw_closed(cli)); /* quit closes */
    fake_net_close(cli);

    /* fresh client sending garbage: fail closed */
    int cli2 = fake_net_connect(MPS3_PORT_SWD);
    polls(1);
    CHECK(fake_net_send(cli2, "z", 1) == 1);
    polls(2);
    CHECK(fake_net_fw_closed(cli2));
}

static void test_gated_defers_bytes_until_ungated(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_SWD);
    polls(1); /* accept */

    g_shell_state.swd_gated = true;
    CHECK(fake_net_send(cli, "O", 1) == 1);
    polls(3);
    CHECK(drive_reg() == 0); /* byte queued, not serviced (RP decoupled) */

    g_shell_state.swd_gated = false;
    polls(2);
    CHECK(drive_reg() == SWDBB_DRIVE_SWDIO_OE); /* serviced after ungate */
}

static void test_second_client_refused(void)
{
    fresh();
    int first = fake_net_connect(MPS3_PORT_SWD);
    polls(1);
    int second = fake_net_connect(MPS3_PORT_SWD);
    polls(1);
    CHECK(fake_net_fw_closed(second));
    CHECK(!fake_net_fw_closed(first));
}

/* REAP BEFORE REFUSE (2026-09-28, net_if.h mps3_net_peer_closed): close,
 * reconnect with NO poll between, sample -- answered every time (accept runs
 * before the old client's EOF is read, so this used to be refused); and a client
 * that closed while service was GATED is reaped for a newcomer. A second LIVE
 * client is still refused (test_second_client_refused). */
static void test_close_then_reconnect_is_adopted(void)
{
    fresh();
    char rsp[4];
    mock_regs_poke(MPS3_SWDBB_BASE, SWDBB_SAMPLE, SWDBB_SAMPLE_SWDIO_I);
    int cli = fake_net_connect(MPS3_PORT_SWD);
    for (int i = 0; i < 50; i++) {
        CHECK(fake_net_send(cli, "c", 1) == 1);
        polls(2);
        CHECK(!fake_net_fw_closed(cli));
        CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1 && rsp[0] == '1');
        fake_net_close(cli);
        cli = fake_net_connect(MPS3_PORT_SWD);    /* immediately: no poll between */
        CHECK(cli >= 0);
    }
    fake_net_close(cli);
    polls(2);

    int old = fake_net_connect(MPS3_PORT_SWD);
    polls(1);
    g_shell_state.swd_gated = true;
    fake_net_close(old);
    int next = fake_net_connect(MPS3_PORT_SWD);
    polls(2);
    CHECK(!fake_net_fw_closed(next));             /* adopted, not refused */
    g_shell_state.swd_gated = false;
    CHECK(fake_net_send(next, "c", 1) == 1);
    polls(2);
    CHECK(fake_net_recv(next, rsp, sizeof(rsp)) == 1 && rsp[0] == '1');
    fake_net_close(next);
    polls(2);
}

static void test_client_hangup_drops_connection(void)
{
    /* OpenOCD hangs up mid-session (connection drop): the next recv returns
     * CLOSED and the server tears the connection down (fail closed), then a
     * fresh client can take the freed single-client slot. */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_SWD);
    polls(1); /* accept */
    fake_net_close(cli);
    polls(2); /* recv -> CLOSED -> drop_client */
    CHECK(fake_net_fw_closed(cli));

    int cli2 = fake_net_connect(MPS3_PORT_SWD);
    polls(1);
    CHECK(fake_net_send(cli2, "O", 1) == 1);
    polls(2);
    CHECK(drive_reg() == SWDBB_DRIVE_SWDIO_OE); /* new client serviced */
}

static void test_send_backpressure_defers_not_drops(void)
{
    /* A SAMPLE ('c') owes a reply. Under a full send buffer (large batched-read
     * burst), mps3_net_send returns 0 (would-block) — the reply must be DEFERRED
     * and re-sent, NOT dropped. (Mirrors the jtag_server OpenOCD-autoprobe silicon
     * fix; the old code reset the link on any non-1 send.) */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_SWD);
    polls(1); /* accept */
    fake_net_set_send_limit(cli, 0);           /* send buffer "full" -> send returns 0 */
    CHECK(fake_net_send(cli, "c", 1) == 1);
    polls(3);
    CHECK(!fake_net_fw_closed(cli));           /* ALIVE — reply deferred, not dropped */
    char rsp[2] = {0, 0};
    CHECK(fake_net_recv(cli, rsp, 1) == 0);    /* nothing delivered yet — still pending */
    fake_net_set_send_limit(cli, -1);          /* buffer drains */
    polls(2);
    CHECK(!fake_net_fw_closed(cli));
    CHECK(fake_net_recv(cli, rsp, 1) == 1);    /* deferred reply now arrives, in order */
    CHECK(rsp[0] == '0' || rsp[0] == '1');
}

static void test_backpressure_preserves_order(void)
{
    /* While a reply is deferred, NO further byte is consumed — a drive queued
     * behind the stuck 'c' isn't applied until the reply flushes. */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_SWD);
    polls(1);
    fake_net_set_send_limit(cli, 0);
    CHECK(fake_net_send(cli, "cO", 2) == 2);   /* 'c' owes a reply, then 'O' (drive SWDIO) */
    polls(3);
    CHECK(drive_reg() == 0);                   /* 'O' NOT applied — parked behind the pending reply */
    fake_net_set_send_limit(cli, -1);
    polls(3);
    CHECK(drive_reg() == SWDBB_DRIVE_SWDIO_OE); /* now 'O' applied */
}

int main(void)
{
    test_drive_release_and_clock_wiggles();
    test_sample_reads_swdbb_and_replies();
    test_srst_maps_to_clkrst_dbg_resetn();
    test_openocd_remote_bitbang_conformance();
    test_led_ignored_quit_closes_junk_drops();
    test_gated_defers_bytes_until_ungated();
    test_second_client_refused();
    test_client_hangup_drops_connection();
    test_close_then_reconnect_is_adopted();
    test_send_backpressure_defers_not_drops();
    test_backpressure_preserves_order();

    printf("test_swd_server: %d checks passed\n", s_checks);
    return 0;
}
