/*
 * jtag_server.c — remote_bitbang JTAG byte dispatch + TCP glue.
 *
 * Host-tested: firmware/test/test_jtag_server.c (104 checks, in `make test`).
 * Compiled into the shell fw (firmware/platform, JTAG cutover); the byte-decode
 * + JTAGBB char->register bit remap are proven board-free. The RTL/on-silicon
 * path still needs the Vivado mint (jtag_bb IP-packaged, validate_bd_design).
 *
 * A one-for-one mirror of firmware/swd_server/swd_server.c, retargeted from the
 * SWD remote_bitbang protocol to the JTAG one and from the SWDBB register block
 * to the JTAGBB block (fpga/shell/ip/jtag_bb) at 0x44A7_0000 (the cutover slot
 * jtag_bb takes over from swd_bb — see MPS3_JTAGBB_BASE in jtag_server.h).
 *
 * The firmware IS the JTAG engine — jtag_bb has deliberately zero sequencing
 * logic (its README), so every TCK edge originates here, exactly as swd_server
 * drives every SWCLK edge (swd_server.c header).
 *
 * PROTOCOL MAPPING — verifiable by reading the HOST driver, no board needed
 * (same method that closed the SWD half of I22, swd_server.c:11-16).
 * OpenOCD src/jtag/drivers/remote_bitbang.c, JTAG side:
 *
 *   static int remote_bitbang_write(int tck, int tms, int tdi)
 *   {   char c = '0' + ((tck ? 0x4 : 0x0) | (tms ? 0x2 : 0x0) | (tdi ? 0x1 : 0x0)); }
 *   static int remote_bitbang_reset(int trst, int srst)
 *   {   char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0)); }
 *   static int remote_bitbang_blink(int on) { char c = on ? 'B' : 'b'; }
 *   // read: queue 'R'; reply MUST be ASCII '0'/'1' (char_to_int), else quit.
 *
 * So for JTAG writes: TCK = bit2, TMS = bit1, TDI = bit0 of (c - '0'), giving
 * the eight chars '0'..'7'. The reset chars 'r'/'s'/'t'/'u', the blink chars
 * 'B'/'b', and 'Q' quit are BYTE-IDENTICAL to the SWD protocol (swd_server.c),
 * so that half is copied verbatim. Only the drive/sample half differs
 * ('0'..'7'+'R' here vs 'd'..'g'+'O'/'o'/'c' in SWD).
 *
 * A5 DONE: firmware/test/test_jtag_server.c has
 * test_openocd_remote_bitbang_jtag_conformance() which RE-DERIVES every byte
 * from the formulas above (never hardcodes letters), mirroring
 * test_swd_server.c's test_openocd_remote_bitbang_conformance().
 */
#include "jtag_server.h"
#include "../common/net_if.h"
#include "../common/platform_regs.h"        /* mps3_reg_*, CLKRST_* (READ-ONLY include) */
#include "../coordinator/coordinator.h"     /* g_shell_state.swd_gated */

jtag_pin_state_t g_jtag_pins;

static mps3_net_listener_t *s_listener;
static mps3_net_conn_t     *s_conn;
/* A reply byte ('0'/'1' for 'R') deferred because the TCP send buffer was full
 * on the poll that generated it; re-sent (before any new byte) next poll. 0 = none. */
static uint8_t              s_reply_pending;

/* Bounded byte budget per poll (each byte can cost 1-2 register accesses plus
 * a possible reply send) — same value as swd_server (SWD_SERVER_BYTES_PER_POLL). */
#define JTAG_SERVER_BYTES_PER_POLL 256u

/* --------------------------------------------------------------------------
 * remote_bitbang JTAG command bytes (mirror of net_proto.h's SWD
 * mps3_bitbang_char_t). TODO(A6): fold this enum into common/net_proto.h.
 * -------------------------------------------------------------------------- */
enum {
    /* '0'..'7' — write {tck,tms,tdi} = bits [2:1:0] of (c - '0'). Handled as a
     * range below rather than 8 named cases. */
    MPS3_JTAG_WRITE_LO   = '0',
    MPS3_JTAG_WRITE_HI   = '7',
    MPS3_JTAG_SAMPLE     = 'R',   /* reply: one ASCII '0'/'1' byte */
    MPS3_JTAG_RESET_00   = 'r',   /* {trst,srst} = {0,0} */
    MPS3_JTAG_RESET_01   = 's',   /* {trst,srst} = {0,1} */
    MPS3_JTAG_RESET_10   = 't',   /* {trst,srst} = {1,0} */
    MPS3_JTAG_RESET_11   = 'u',   /* {trst,srst} = {1,1} */
    MPS3_JTAG_LED_ON     = 'B',
    MPS3_JTAG_LED_OFF    = 'b',
    MPS3_JTAG_QUIT       = 'Q',
};

/* Compose + write the full DRIVE register from the mirrored pin state (DRIVE
 * is a plain write-through register, so writing all three bits every time is
 * exact and keeps the mirror authoritative). Mirror of swdbb_drive(). */
static void jtagbb_drive(void)
{
    uint32_t v = 0;
    if (g_jtag_pins.tck) v |= JTAGBB_DRIVE_TCK;
    if (g_jtag_pins.tms) v |= JTAGBB_DRIVE_TMS;
    if (g_jtag_pins.tdi) v |= JTAGBB_DRIVE_TDI;
    mps3_reg_write32(MPS3_JTAGBB_BASE, JTAGBB_DRIVE, v);
}

void jtag_server_init(void)
{
    g_jtag_pins.tck = 0;
    g_jtag_pins.tms = 0;
    g_jtag_pins.tdi = 0;
    g_jtag_pins.tdo = 0;
    g_jtag_pins.srst = 0;

    s_listener = mps3_net_listen(MPS3_PORT_JTAG);
    s_conn = 0;
    s_reply_pending = 0;

    /* Match the RTL's reset drive state explicitly (3'b000: TCK low, TMS/TDI
     * low — jtag_bb README / swd_bb flag #3 analogue). */
    jtagbb_drive();
}

int jtag_server_handle_byte(uint8_t c, uint8_t *reply_out)
{
    *reply_out = 0;

    /* '0'..'7' write range first (the JTAG hot path). CONFIRMED vs OpenOCD
     * remote_bitbang.c: char = '0' + (tck<<2 | tms<<1 | tdi), so
     * TCK = bit2, TMS = bit1, TDI = bit0 of (c - '0'). */
    if (c >= MPS3_JTAG_WRITE_LO && c <= MPS3_JTAG_WRITE_HI) {
        uint8_t v = (uint8_t)(c - '0');
        g_jtag_pins.tck = (uint8_t)((v >> 2) & 1);
        g_jtag_pins.tms = (uint8_t)((v >> 1) & 1);
        g_jtag_pins.tdi = (uint8_t)( v       & 1);
        jtagbb_drive();
        return 0;
    }

    switch (c) {
    case MPS3_JTAG_SAMPLE:
        /* SAMPLE[0] is the live, 2-FF-synchronized jtag_tdo level; at
         * remote_bitbang pace the synchronizer latency is invisible (mirror of
         * swd_server's 'c' sample). Reply MUST be ASCII '0'/'1' or OpenOCD
         * logs an error and drops the link (char_to_int). */
        g_jtag_pins.tdo =
            (uint8_t)(mps3_reg_read32(MPS3_JTAGBB_BASE, JTAGBB_SAMPLE) & JTAGBB_SAMPLE_TDO);
        *reply_out = g_jtag_pins.tdo ? '1' : '0';
        return 0;

    case MPS3_JTAG_RESET_00:
    case MPS3_JTAG_RESET_01:
    case MPS3_JTAG_RESET_10:
    case MPS3_JTAG_RESET_11: {
        /* CONFIRMED vs OpenOCD remote_bitbang.c:
         *     char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0));
         * so trst = bit1, srst = bit0 of (c - 'r'). IDENTICAL to swd_server.
         * This design carries NO trst wire across the RP boundary (the RM
         * straps ntrst=1 — design note §1.3), so trst is correctly IGNORED and
         * only srst -> CLKRST.dbg_resetn is meaningful. Host cfg should declare
         * `reset_config srst_only` so OpenOCD never emits 't'/'u'. */
        uint8_t srst = (uint8_t)((c - 'r') & 1);
        g_jtag_pins.srst = srst;
        if (srst) {
            /* srst asserted -> hold DUT in debug reset. CLKRST convention is
             * "1 = released", so asserting means CLEARING dbg_resetn. Same pin
             * and same mapping the SWD path uses today (swd_server.c:110-116). */
            mps3_reg_clr_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DBG_RESETN);
        } else {
            mps3_reg_set_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DBG_RESETN);
        }
        return 0;
    }

    case MPS3_JTAG_LED_ON:
    case MPS3_JTAG_LED_OFF:
        /* No LED register in shell-regmap.md; accepted-but-ignored, exactly as
         * swd_server handles 'B'/'b' (swd_server.c:120-124). */
        return 0;

    case MPS3_JTAG_QUIT:
        return 1; /* caller closes the connection */

    default:
        return -1; /* unrecognized byte -- OpenOCD (in JTAG transport) never
                    * sends these; drop the connection (fail closed rather than
                    * silently desync on a corrupt stream). NOTE: the SWD-only
                    * chars 'd'..'g'/'O'/'o'/'c' fall here — a jtag_server must
                    * be driven by a JTAG-transport OpenOCD session, not an SWD
                    * one. */
    }
}

static void drop_client(void)
{
    mps3_net_close(s_conn);
    s_conn = 0;
    s_reply_pending = 0;
}

/* WEAK "never refuse" for the claim lock (jtag_server.h): bare metal unchanged. */
__attribute__((weak)) int mps3_jtag_refuse_peer(struct mps3_net_conn *conn)
{
    (void)conn;
    return 0;
}

void jtag_server_poll(void)
{
    /* Accept even while gated so a connect isn't left dangling; byte service
     * below is what gates. Single client v1 (OpenOCD holds one remote_bitbang
     * connection for the session); extras refused. Mirror of swd_server_poll.
     * A claimed Linux harness refuses a non-local peer first, with a line.
     * Reap before refuse (net_if.h mps3_net_peer_closed): a current client
     * whose peer is already gone -- its EOF not yet read, e.g. because service
     * is gated for a swap -- is dropped and the newcomer adopted; only a LIVE
     * client makes the newcomer a refused extra. */
    mps3_net_conn_t *incoming = mps3_net_accept(s_listener);
    if (incoming) {
        if (mps3_jtag_refuse_peer(incoming)) {
            mps3_net_refuse_with_line(incoming, MPS3_JTAG_LOCKED_LINE);
        } else {
            if (s_conn != 0 && mps3_net_peer_closed(s_conn)) {
                drop_client();
            }
            if (s_conn == 0) {
                s_conn = incoming;
            } else {
                mps3_net_close(incoming);
            }
        }
    }
    if (!s_conn) {
        return;
    }

    /* TODO(A6): reuse g_shell_state.swd_gated (the DUT-debug transport is
     * gated as one — SWD and JTAG are mutually-exclusive so a single gate is
     * correct), OR add a parallel g_shell_state.jtag_gated if the two are ever
     * present at once. Either way: stop toggling pins while the RP is
     * decoupled / held in reset during a swap (design note §6; swd_server
     * README "Gating during a swap"). */
    if (g_shell_state.swd_gated) {
        return; /* RP isolated/held in reset during a swap. Bytes queue in the
                 * transport; serviced once ungated. */
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

    for (uint32_t budget = JTAG_SERVER_BYTES_PER_POLL; budget > 0; budget--) {
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
        int rc = jtag_server_handle_byte(c, &reply);
        if (rc < 0) {
            drop_client(); /* protocol violation: fail closed */
            return;
        }
        if (reply != 0) {
            /* remote_bitbang reads are synchronous request/response: the reply
             * byte must go out before the next command is consumed. Under a large
             * batched-read burst (e.g. OpenOCD's scan-chain autoprobe) the board's
             * small lwIP send buffer fills, so mps3_net_send returns 0 (would-
             * block, per net_if.h: returned count = min(len, tcp_sndbuf)). That is
             * NOT a protocol error: stash the reply, yield to the superloop so
             * lwIP drains, and re-send it next poll (above) before consuming more
             * bytes. Only a hard error (<0) fails closed. (The old "1-byte send
             * can't-block -> drop" assumption reset the link mid-scan against
             * OpenOCD's autoprobe — proven on silicon 2026-07-30 at ~400 batched
             * reads.) */
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
