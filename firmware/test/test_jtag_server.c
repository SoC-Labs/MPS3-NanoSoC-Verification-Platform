/*
 * test_jtag_server.c — host-gcc tests for jtag_server.c's REAL byte drain +
 * JTAGBB register pokes. The JTAG analogue of test_swd_server.c (closes
 * jtag_server.c's TODO(A5)). remote_bitbang JTAG bytes go in through a
 * fake_net_if client exactly as OpenOCD would send them; pin effects are
 * observed in mock_regs' JTAGBB/CLKRST state.
 *
 * The load-bearing check is test_openocd_remote_bitbang_jtag_conformance():
 * it RE-DERIVES every byte from OpenOCD src/jtag/drivers/remote_bitbang.c
 *     char c = '0' + (tck<<2 | tms<<1 | tdi);   // JTAG write
 *     char c = 'r' + (trst<<1 | srst);          // reset
 * and verifies jtag_server.c's char->register bit REMAP (the char packs TCK at
 * bit2 / TDI at bit0, but JTAGBB_DRIVE has TCK at bit0 / TDI at bit2 — an
 * inverted mapping would fail HERE, not at first silicon bring-up).
 *
 * Links: jtag_server.c, common/net_if.c, mock_regs.c, fake_net_if.c.
 * g_shell_state defined here (jtag_server.c reads swd_gated only).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../jtag_server/jtag_server.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
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
    jtag_server_init();
}

static void polls(int n)
{
    for (int i = 0; i < n; i++) {
        jtag_server_poll();
    }
}

static uint32_t drive_reg(void)
{
    return mock_regs_peek(MPS3_JTAGBB_BASE, JTAGBB_DRIVE);
}

/* '0'..'7' write: char = '0' + (tck<<2 | tms<<1 | tdi). JTAG TCK/TMS/TDI are
 * always driven (no tristate/OE like SWD), so DRIVE directly reflects them. */
static void test_write_range_drives_pins(void)
{
    fresh();
    CHECK(drive_reg() == 0);                 /* init drive state 3'b000 */

    int cli = fake_net_connect(MPS3_PORT_JTAG);
    CHECK(cli >= 0);

    CHECK(fake_net_send(cli, "7", 1) == 1);  /* tck=1 tms=1 tdi=1 */
    polls(2);
    CHECK(drive_reg() == (JTAGBB_DRIVE_TCK | JTAGBB_DRIVE_TMS | JTAGBB_DRIVE_TDI));
    CHECK(g_jtag_pins.tck == 1 && g_jtag_pins.tms == 1 && g_jtag_pins.tdi == 1);

    CHECK(fake_net_send(cli, "0", 1) == 1);  /* all low */
    polls(2);
    CHECK(drive_reg() == 0);

    /* '4' = tck only. Proves the char-bit2 -> DRIVE-bit0 remap (NOT a passthrough). */
    CHECK(fake_net_send(cli, "4", 1) == 1);  /* tck=1 tms=0 tdi=0 */
    polls(2);
    CHECK(drive_reg() == JTAGBB_DRIVE_TCK);
    CHECK(g_jtag_pins.tck == 1 && g_jtag_pins.tms == 0 && g_jtag_pins.tdi == 0);

    /* '1' = tdi only. Proves char-bit0 -> DRIVE-bit2 remap. */
    CHECK(fake_net_send(cli, "1", 1) == 1);  /* tck=0 tms=0 tdi=1 */
    polls(2);
    CHECK(drive_reg() == JTAGBB_DRIVE_TDI);
    CHECK(g_jtag_pins.tdi == 1 && g_jtag_pins.tck == 0);
}

static void test_sample_reads_jtagbb_and_replies(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    char rsp[4];

    mock_regs_poke(MPS3_JTAGBB_BASE, JTAGBB_SAMPLE, JTAGBB_SAMPLE_TDO);
    CHECK(fake_net_send(cli, "R", 1) == 1);
    polls(2);
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1);
    CHECK(rsp[0] == '1');
    CHECK(g_jtag_pins.tdo == 1);

    mock_regs_poke(MPS3_JTAGBB_BASE, JTAGBB_SAMPLE, 0);
    CHECK(fake_net_send(cli, "R", 1) == 1);
    polls(2);
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1);
    CHECK(rsp[0] == '0');
}

static void test_srst_maps_to_clkrst_dbg_resetn(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    /* Seed dbg_resetn released. */
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DBG_RESETN);

    CHECK(fake_net_send(cli, "s", 1) == 1);  /* {trst,srst}={0,1}: assert srst */
    polls(2);
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_DBG_RESETN) == 0); /* 1=released -> cleared = held */
    CHECK(g_jtag_pins.srst == 1);

    CHECK(fake_net_send(cli, "r", 1) == 1);  /* {0,0}: deassert */
    polls(2);
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_DBG_RESETN) != 0);
    CHECK(g_jtag_pins.srst == 0);
}

/* ---------------------------------------------------------------------------
 * OpenOCD remote_bitbang JTAG CONFORMANCE (jtag_server.c TODO(A5)).
 * Re-derives every byte from the host driver's formulas so an inverted bit
 * mapping fails here rather than at silicon bring-up.
 * ------------------------------------------------------------------------- */
static void test_openocd_remote_bitbang_jtag_conformance(void)
{
    /* --- write: c = '0' + (tck<<2 | tms<<1 | tdi) ------------------------- */
    for (int tck = 0; tck <= 1; tck++) {
        for (int tms = 0; tms <= 1; tms++) {
            for (int tdi = 0; tdi <= 1; tdi++) {
                fresh();
                int cli = fake_net_connect(MPS3_PORT_JTAG);

                char c = (char)('0' + ((tck ? 0x4 : 0x0) |
                                       (tms ? 0x2 : 0x0) |
                                       (tdi ? 0x1 : 0x0)));
                CHECK(fake_net_send(cli, &c, 1) == 1);
                polls(2);

                uint32_t want = (tck ? JTAGBB_DRIVE_TCK : 0u)
                              | (tms ? JTAGBB_DRIVE_TMS : 0u)
                              | (tdi ? JTAGBB_DRIVE_TDI : 0u);
                CHECK(drive_reg() == want);
                CHECK(g_jtag_pins.tck == (uint8_t)tck);
                CHECK(g_jtag_pins.tms == (uint8_t)tms);
                CHECK(g_jtag_pins.tdi == (uint8_t)tdi);
            }
        }
    }

    /* --- reset: c = 'r' + (trst<<1 | srst); only srst is meaningful ------- */
    for (int trst = 0; trst <= 1; trst++) {
        for (int srst = 0; srst <= 1; srst++) {
            fresh();
            int cli = fake_net_connect(MPS3_PORT_JTAG);
            mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL,
                           CLKRST_RESET_CTRL_DBG_RESETN);  /* seed: released */

            char c = (char)('r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0)));
            CHECK(fake_net_send(cli, &c, 1) == 1);
            polls(2);

            uint32_t rc = mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL);
            /* srst asserted => dbg_resetn HELD (bit clear). trst must not matter. */
            CHECK(((rc & CLKRST_RESET_CTRL_DBG_RESETN) == 0) == (srst == 1));
            CHECK(g_jtag_pins.srst == (uint8_t)srst);
        }
    }

    /* --- sample reply MUST be ASCII '0'/'1' (char_to_int errors otherwise) -- */
    for (int level = 0; level <= 1; level++) {
        fresh();
        int cli = fake_net_connect(MPS3_PORT_JTAG);
        mock_regs_poke(MPS3_JTAGBB_BASE, JTAGBB_SAMPLE,
                       level ? JTAGBB_SAMPLE_TDO : 0u);
        CHECK(fake_net_send(cli, "R", 1) == 1);
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
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    CHECK(fake_net_send(cli, "Bb", 2) == 2); /* LED on/off: accepted-ignored */
    polls(2);
    CHECK(!fake_net_fw_closed(cli));

    CHECK(fake_net_send(cli, "Q", 1) == 1);
    polls(2);
    CHECK(fake_net_fw_closed(cli)); /* quit closes */
    fake_net_close(cli);

    /* An SWD-only char ('d') is invalid on a JTAG session: fail closed. This is
     * the guardrail that a JTAG server must be driven by a JTAG-transport
     * OpenOCD session, not an SWD one (jtag_server.c default case). */
    int cli2 = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);
    CHECK(fake_net_send(cli2, "d", 1) == 1);
    polls(2);
    CHECK(fake_net_fw_closed(cli2));

    /* Generic garbage also fails closed. */
    int cli3 = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);
    CHECK(fake_net_send(cli3, "z", 1) == 1);
    polls(2);
    CHECK(fake_net_fw_closed(cli3));
}

static void test_gated_defers_bytes_until_ungated(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    polls(1); /* accept */

    g_shell_state.swd_gated = true;
    CHECK(fake_net_send(cli, "7", 1) == 1);  /* would drive all pins high */
    polls(3);
    CHECK(drive_reg() == 0); /* byte queued, not serviced (RP decoupled) */

    g_shell_state.swd_gated = false;
    polls(2);
    CHECK(drive_reg() == (JTAGBB_DRIVE_TCK | JTAGBB_DRIVE_TMS | JTAGBB_DRIVE_TDI));
}

static void test_second_client_refused(void)
{
    fresh();
    int first = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);
    int second = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);
    CHECK(fake_net_fw_closed(second));
    CHECK(!fake_net_fw_closed(first));
}

static void test_client_hangup_drops_connection(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    polls(1); /* accept */
    fake_net_close(cli);
    polls(2); /* recv -> CLOSED -> drop_client */
    CHECK(fake_net_fw_closed(cli));

    int cli2 = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);
    CHECK(fake_net_send(cli2, "7", 1) == 1);
    polls(2);
    CHECK(drive_reg() == (JTAGBB_DRIVE_TCK | JTAGBB_DRIVE_TMS | JTAGBB_DRIVE_TDI));
}

static void test_send_backpressure_defers_not_drops(void)
{
    /* THE OpenOCD-autoprobe silicon bug (2026-07-30): a large batched-read burst
     * fills the board's small lwIP send buffer, so mps3_net_send returns 0
     * (would-block). The 'R' reply must be DEFERRED and re-sent — NOT dropped.
     * (The old code dropped on any non-1 send, which reset OpenOCD mid-scan at
     * ~400 batched reads on real silicon.) */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);                                   /* accept */
    fake_net_set_send_limit(cli, 0);            /* send buffer "full" -> send returns 0 */
    CHECK(fake_net_send(cli, "R", 1) == 1);
    polls(3);
    CHECK(!fake_net_fw_closed(cli));            /* connection ALIVE (reply deferred, not dropped) */
    char rsp[2] = {0, 0};
    CHECK(fake_net_recv(cli, rsp, 1) == 0);     /* nothing delivered yet — still pending */

    fake_net_set_send_limit(cli, -1);           /* buffer drains */
    polls(2);
    CHECK(!fake_net_fw_closed(cli));
    CHECK(fake_net_recv(cli, rsp, 1) == 1);     /* the deferred reply now arrives, in order */
    CHECK(rsp[0] == '0' || rsp[0] == '1');
}

static void test_backpressure_preserves_order(void)
{
    /* While a reply is deferred, NO further byte is consumed — so a write queued
     * behind the stuck 'R' is not applied until the reply is flushed (ordering). */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);
    fake_net_set_send_limit(cli, 0);
    CHECK(fake_net_send(cli, "R7", 2) == 2);    /* 'R' (owes a reply) then '7' (drive all high) */
    polls(3);
    CHECK(drive_reg() == 0);                    /* '7' NOT applied — parked behind the pending reply */
    fake_net_set_send_limit(cli, -1);
    polls(3);
    CHECK(drive_reg() == (JTAGBB_DRIVE_TCK | JTAGBB_DRIVE_TMS | JTAGBB_DRIVE_TDI)); /* now '7' applied */
}

/* REAP BEFORE REFUSE (2026-09-28, net_if.h mps3_net_peer_closed): accept runs
 * before the current client's EOF is read, so a client that closed and at once
 * reconnected used to be refused. Now: close, reconnect with NO poll between,
 * sample -- answered every time; a second LIVE client is still refused
 * (test_second_client_refused); and a client that closed while service was
 * GATED (never read, so its EOF could not be seen) is reaped for a newcomer. */
static void test_close_then_reconnect_is_adopted(void)
{
    fresh();
    char rsp[4];
    mock_regs_poke(MPS3_JTAGBB_BASE, JTAGBB_SAMPLE, JTAGBB_SAMPLE_TDO);
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    for (int i = 0; i < 50; i++) {
        CHECK(fake_net_send(cli, "R", 1) == 1);
        polls(2);
        CHECK(!fake_net_fw_closed(cli));
        CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1 && rsp[0] == '1');
        fake_net_close(cli);
        cli = fake_net_connect(MPS3_PORT_JTAG);   /* immediately: no poll between */
        CHECK(cli >= 0);
    }
    fake_net_close(cli);
    polls(2);

    /* Gated: the current client is never read, so only the probe can see it is
     * gone. The newcomer's byte queues and is served once ungated. */
    int old = fake_net_connect(MPS3_PORT_JTAG);
    polls(1);
    g_shell_state.swd_gated = true;
    fake_net_close(old);
    int next = fake_net_connect(MPS3_PORT_JTAG);
    polls(2);
    CHECK(!fake_net_fw_closed(next));             /* adopted, not refused */
    CHECK(fake_net_send(next, "R", 1) == 1);
    polls(2);
    CHECK(fake_net_recv(next, rsp, sizeof(rsp)) == 0);   /* gated: queued */
    g_shell_state.swd_gated = false;
    polls(2);
    CHECK(fake_net_recv(next, rsp, sizeof(rsp)) == 1 && rsp[0] == '1');
    fake_net_close(next);
    polls(2);
}

/* THE CLAIM LOCK (2026-09-26, HM_ANSWERS C3; the strong seam is harnessd's): a
 * refused peer gets ONE line with code "locked" and the close -- even when it
 * spoke first -- and touches nothing; with the seam at 0 (the weak default, every
 * other test here) the same connect is adopted and served. */
static int s_refuse;
int mps3_jtag_refuse_peer(struct mps3_net_conn *conn)
{
    (void)conn;
    return s_refuse;
}

static void test_claim_lock_refuses_with_one_line(void)
{
    char rsp[160];
    fresh();
    s_refuse = 1;
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    CHECK(fake_net_send(cli, "getinfo:", 8) == 8);         /* it spoke before the accept */
    polls(2);
    CHECK(fake_net_fw_closed(cli));
    int n = fake_net_recv(cli, rsp, sizeof(rsp) - 1);
    CHECK(n == (int)strlen(MPS3_JTAG_LOCKED_LINE));
    rsp[n > 0 ? n : 0] = '\0';
    CHECK(strcmp(rsp, MPS3_JTAG_LOCKED_LINE) == 0);
    CHECK(drive_reg() == 0);                              /* ...and it moved no pin */
    s_refuse = 0;                                         /* the control: adopted */
    int ok = fake_net_connect(MPS3_PORT_JTAG);
    polls(2);
    CHECK(!fake_net_fw_closed(ok));
}

int main(void)
{
    test_write_range_drives_pins();
    test_sample_reads_jtagbb_and_replies();
    test_srst_maps_to_clkrst_dbg_resetn();
    test_openocd_remote_bitbang_jtag_conformance();
    test_led_ignored_quit_closes_junk_drops();
    test_gated_defers_bytes_until_ungated();
    test_second_client_refused();
    test_client_hangup_drops_connection();
    test_close_then_reconnect_is_adopted();
    test_send_backpressure_defers_not_drops();
    test_backpressure_preserves_order();

    test_claim_lock_refuses_with_one_line();

    printf("test_jtag_server: %d checks passed\n", s_checks);
    return 0;
}
