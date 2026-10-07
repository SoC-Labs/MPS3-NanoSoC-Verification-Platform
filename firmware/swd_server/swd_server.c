/*
 * swd_server.c — remote_bitbang byte dispatch + TCP glue, real bodies
 * (W-NET-SEAM): the byte protocol drains through the common/net_if.h seam
 * and the pin pokes hit the real SWDBB register block (shell-regmap.md
 * v0.1 I5 RESOLVED — fpga/shell/ip/swd_bb is landed RTL: DRIVE @0x00
 * write-through {swclk, swdio_o, swdio_oe}, SAMPLE @0x04 RO 2-FF-synced
 * swdio_i). The firmware IS the SWD engine — swd_bb has deliberately zero
 * sequencing logic (its README), so every SWCLK edge originates here.
 *
 * BIT ORDER CONFIRMED (2026-07-09, SWD half of I22): the d/e/f/g <-> {CLK,DIO}
 * and r/s/t/u <-> {trst,srst} mappings are fixed by the HOST driver, not by our
 * wiring, so they were verifiable by reading it — no bring-up needed. Checked
 * against OpenOCD src/jtag/drivers/remote_bitbang.c ('d' + (swclk<<1|swdio);
 * 'r' + (trst<<1|srst); sample reply must be ASCII '0'/'1'). Both mappings are
 * pinned by test_openocd_remote_bitbang_conformance(), which re-derives every
 * byte from those formulas rather than hardcoding letters.
 */
#include "swd_server.h"
#include "../common/net_proto.h"
#include "../common/net_if.h"
#include "../common/platform_regs.h"
#include "../coordinator/coordinator.h" /* g_shell_state.swd_gated */

swd_pin_state_t g_swd_pins;

static mps3_net_listener_t *s_listener;
static mps3_net_conn_t     *s_conn;
/* A reply byte ('0'/'1' for the 'c' sample) deferred because the TCP send buffer
 * was full on the poll that generated it; re-sent (before any new byte) next poll.
 * 0 = none. (Mirrors jtag_server — the silicon-proven OpenOCD-autoprobe fix.) */
static uint8_t              s_reply_pending;

/* Bounded byte budget per poll (each byte can cost 1-2 register accesses
 * plus a possible reply send). */
#define SWD_SERVER_BYTES_PER_POLL 256u

/* Compose + write the full DRIVE register from the mirrored pin state
 * (DRIVE is a plain write-through register, so writing all three bits
 * every time is exact and keeps the mirror authoritative). */
static void swdbb_drive(void)
{
    uint32_t v = 0;
    if (g_swd_pins.swclk)    v |= SWDBB_DRIVE_SWCLK;
    if (g_swd_pins.swdio_o)  v |= SWDBB_DRIVE_SWDIO_O;
    if (g_swd_pins.swdio_oe) v |= SWDBB_DRIVE_SWDIO_OE;
    mps3_reg_write32(MPS3_SWDBB_BASE, SWDBB_DRIVE, v);
}

void swd_server_init(void)
{
    g_swd_pins.swclk = 0;
    g_swd_pins.swdio_o = 0;
    g_swd_pins.swdio_oe = 0;
    g_swd_pins.swdio_i = 0;
    g_swd_pins.srst = 0;

    s_listener = mps3_net_listen(MPS3_PORT_SWD);
    s_conn = 0;
    s_reply_pending = 0;

    /* Match the RTL's reset drive state explicitly (3'b000: SWCLK low,
     * SWDIO released — swd_bb README flag #3). */
    swdbb_drive();
}

int swd_server_handle_byte(uint8_t c, uint8_t *reply_out)
{
    *reply_out = 0;

    switch ((mps3_bitbang_char_t)c) {
    case MPS3_BITBANG_SWDIO_DRIVE:
        g_swd_pins.swdio_oe = 1;
        swdbb_drive();
        return 0;

    case MPS3_BITBANG_SWDIO_RELEASE:
        g_swd_pins.swdio_oe = 0;
        swdbb_drive();
        return 0;

    case MPS3_BITBANG_SWDIO_SAMPLE:
        /* SAMPLE[0] is the live, 2-FF-synchronized swd_dio_i level; at
         * remote_bitbang pace the synchronizer latency is invisible
         * (swd_bb README's note for A3). */
        g_swd_pins.swdio_i =
            (uint8_t)(mps3_reg_read32(MPS3_SWDBB_BASE, SWDBB_SAMPLE) & SWDBB_SAMPLE_SWDIO_I);
        *reply_out = g_swd_pins.swdio_i ? '1' : '0';
        return 0;

    case MPS3_BITBANG_CLK0_DIO0:
    case MPS3_BITBANG_CLK0_DIO1:
    case MPS3_BITBANG_CLK1_DIO0:
    case MPS3_BITBANG_CLK1_DIO1:
        /* CONFIRMED vs OpenOCD remote_bitbang.c:
         *     char c = 'd' + ((swclk ? 0x2 : 0x0) | (swdio ? 0x1 : 0x0));
         * so CLK = bit1 of (c - 'd'), DIO = bit0.
         * Pinned by test_openocd_remote_bitbang_conformance(). */
        g_swd_pins.swclk   = (uint8_t)(((c - 'd') >> 1) & 1);
        g_swd_pins.swdio_o = (uint8_t)((c - 'd') & 1);
        swdbb_drive();
        return 0;

    case MPS3_BITBANG_RESET_00:
    case MPS3_BITBANG_RESET_01:
    case MPS3_BITBANG_RESET_10:
    case MPS3_BITBANG_RESET_11: {
        /* CONFIRMED vs OpenOCD remote_bitbang.c:
         *     char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0));
         * so trst = bit1, srst = bit0 of (c - 'r'). This shell has no separate
         * trst (SWD, not JTAG) so only srst -> dbg_resetn is meaningful, and
         * trst is correctly ignored. srst is CLKRST's dbg_resetn, NOT an SWDBB
         * bit (swd_bb README register note). */
        uint8_t srst = (uint8_t)((c - 'r') & 1);
        g_swd_pins.srst = srst;
        if (srst) {
            /* srst asserted -> hold DUT in debug reset. CLKRST convention
             * is "1 = released" so asserting means CLEARING dbg_resetn. */
            mps3_reg_clr_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DBG_RESETN);
        } else {
            mps3_reg_set_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DBG_RESETN);
        }
        return 0;
    }

    case MPS3_BITBANG_LED_ON:
    case MPS3_BITBANG_LED_OFF:
        /* No LED register anywhere in shell-regmap.md; accepted-but-ignored
         * (MPS3 has board LEDs but no contract wires one here). */
        return 0;

    case MPS3_BITBANG_QUIT:
        return 1; /* caller closes the connection */

    default:
        return -1; /* unrecognized byte -- OpenOCD never sends these; the
                    * poll loop drops the connection (fail closed rather
                    * than silently desync on a corrupt stream). */
    }
}

static void drop_client(void)
{
    mps3_net_close(s_conn);
    s_conn = 0;
    s_reply_pending = 0;
}

void swd_server_poll(void)
{
    /* Accept even while gated so a connect isn't left dangling; byte
     * service below is what gates. Single client v1 (OpenOCD holds one
     * remote_bitbang connection for the session); extras refused -- but reap
     * before refuse (net_if.h mps3_net_peer_closed): a current client whose
     * peer is already gone is dropped and the newcomer adopted. */
    mps3_net_conn_t *incoming = mps3_net_accept(s_listener);
    if (incoming) {
        if (s_conn != 0 && mps3_net_peer_closed(s_conn)) {
            drop_client();
        }
        if (s_conn == 0) {
            s_conn = incoming;
        } else {
            mps3_net_close(incoming);
        }
    }
    if (!s_conn) {
        return;
    }

    if (g_shell_state.swd_gated) {
        return; /* RP isolated/held in reset during a swap -- see README.
                 * Bytes queue in the transport; serviced once ungated. */
    }

    /* Flush a reply deferred by send back-pressure on a prior poll BEFORE
     * consuming any new byte — the remote_bitbang stream is strictly
     * request/response, so ordering must hold. */
    if (s_reply_pending) {
        int sn = mps3_net_send(s_conn, &s_reply_pending, 1);
        if (sn < 0)  { drop_client(); return; }
        if (sn == 0) { mps3_net_flush(s_conn); return; }  /* still full: yield */
        s_reply_pending = 0;                              /* sent */
    }

    for (uint32_t budget = SWD_SERVER_BYTES_PER_POLL; budget > 0; budget--) {
        uint8_t c;
        int n = mps3_net_recv(s_conn, &c, 1);
        if (n == 0) {
            return;
        }
        if (n < 0) {
            drop_client();
            return;
        }
        uint8_t reply;
        int rc = swd_server_handle_byte(c, &reply);
        if (rc < 0) {
            drop_client(); /* protocol violation: fail closed */
            return;
        }
        if (reply != 0) {
            /* remote_bitbang reads are synchronous request/response: the reply
             * byte must go out before the next command is consumed. Under a large
             * batched-read burst (OpenOCD's scan-chain autoprobe) the board's
             * small lwIP send buffer fills, so mps3_net_send returns 0 (would-
             * block, per net_if.h: returned count = min(len, tcp_sndbuf)). NOT an
             * error: stash the reply, flush, and yield to the superloop so lwIP
             * drains; re-send it next poll before consuming more bytes. Only a
             * hard error (<0) fails closed. (Same silicon-proven fix as
             * jtag_server — the old drop-on-non-1 reset OpenOCD mid-scan.) */
            int sn = mps3_net_send(s_conn, &reply, 1);
            if (sn < 0)  { drop_client(); return; }
            if (sn == 0) { s_reply_pending = reply; mps3_net_flush(s_conn); return; }
            /* sn == 1: sent */
        }
        if (rc == 1) {
            drop_client(); /* 'Q' quit */
            return;
        }
    }
}
