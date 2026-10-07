/*
 * jtag_linux.c -- TCP 6921 (OpenOCD remote_bitbang, JTAG) under mps3-harnessd, with
 * the per-byte syscalls taken out (lane JTAG-SERVER-FIX, 2026-09-30).
 *
 * WHAT THIS REPLACES. Service row 6 ("jtag") ran firmware/jtag_server/jtag_server.c's
 * jtag_server_poll() unmodified: ONE mps3_net_recv() of ONE byte per remote_bitbang
 * command and ONE mps3_net_send() per 'R' reply, at most 256 bytes a pass. On lwIP
 * that is a pointer walk; under Linux each is a recv(2)/send(2) (tens of us on the
 * 100 MHz MicroBlaze V), and each reply leaves as its own TCP segment -- over the
 * claim's SSH forward, its own encrypted SSH packet, which dropbear pays for on the
 * same hart. A DAP scan is ~120 command bytes and 35 samples: ~150 syscalls and 35
 * segments. Measured on silicon 30 Sep over the claim forward: ~12 ms a round trip,
 * ~25 B/s of image data (a 160 KB DUT flash write took 107 min).
 *
 * WHAT THIS DOES. The SAME protocol, byte for byte. Every command byte still goes
 * through jtag_server_handle_byte() (jtag_server.c: the one place the remote_bitbang
 * JTAG mapping lives, with its conformance test), in order, and every reply byte
 * goes out in order. Only the I/O is batched:
 *   - ONE recv of up to JTAG_LX_CHUNK bytes a poll (not one per byte);
 *   - the 'R' replies of that chunk collected and sent in ONE send (one segment);
 *   - a short send keeps the rest pending, and no new command byte is consumed
 *     until every earlier reply has gone (jtag_server.c's s_reply_pending rule,
 *     for a whole chunk);
 *   - 'Q' or a byte outside the protocol: the replies before it are delivered,
 *     then the connection is dropped (jtag_server.c drops at the same byte; the
 *     bytes after it are never interpreted).
 * TCP_NODELAY was already set on every accepted socket (posix_net_if.c accept).
 * tests/test_jtag_batch.c runs this and jtag_server_poll() over the same streams
 * (OpenOCD-shaped scans, random streams at random chunkings, Q and bad bytes, send
 * back-pressure, a swap's gate) and asserts the same reply bytes and the same JTAGBB
 * register writes in the same order; it also carries the cost model.
 *
 * --jtag-io per-byte (main_linux.c) keeps jtag_server.c's own loop -- one recv per
 * byte, one send per reply, 256 bytes a pass -- as the fallback and as the live
 * negative control.
 *
 * BARE METAL IS UNTOUCHED: this file is harnessd's, and jtag_server.c is not edited
 * (its jtag_server_init() still opens the listener and parks the pins: coordinator_init()
 * calls it, and mps3_net_listen() is idempotent per port). The claim lock
 * (mps3_jtag_refuse_peer, slot_linux.c) is checked at accept exactly as before.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <string.h>

#include "../../../../firmware/common/net_if.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "../../../../firmware/jtag_server/jtag_server.h"
#include "harnessd.h"

/* One recv a poll, at most this many command bytes. Each costs one JTAGBB DRIVE
 * write (~80 ns posted) or one SAMPLE read (~250 ns) plus the dispatch, so a full
 * chunk is ~2-3 ms on the MBV: inside the jtag row's 10 ms budget. The reply
 * buffer is the same size (a command byte makes at most one reply byte). */
#ifndef JTAG_LX_CHUNK
#define JTAG_LX_CHUNK 4096u
#endif

static struct {
    mps3_net_listener_t *lst;
    mps3_net_conn_t     *conn;
    uint8_t              in[JTAG_LX_CHUNK];
    uint8_t              out[JTAG_LX_CHUNK];
    uint32_t             out_off, out_len;   /* replies still to send: out[off..len) */
    int                  close_after_flush;  /* 'Q' / protocol violation seen        */
    harnessd_jtag_stats_t st;
} J;

static void drop_client(void)
{
    mps3_net_close(J.conn);
    J.conn = 0;
    J.out_off = J.out_len = 0u;
    J.close_after_flush = 0;
}

/* Send what is pending. 1 = all gone, 0 = some left (back-pressure), -1 = the
 * connection died (dropped here). */
static int flush_replies(void)
{
    while (J.out_off < J.out_len) {
        int sn = mps3_net_send(J.conn, J.out + J.out_off, J.out_len - J.out_off);
        J.st.sends++;
        if (sn < 0) {
            drop_client();
            return -1;
        }
        if (sn == 0) {
            mps3_net_flush(J.conn);
            return 0;
        }
        J.out_off += (uint32_t)sn;
        J.st.reply_bytes += (uint64_t)sn;
    }
    J.out_off = J.out_len = 0u;
    return 1;
}

static void accept_one(void)
{
    mps3_net_conn_t *incoming = mps3_net_accept(J.lst);
    if (!incoming) {
        return;
    }
    /* The claim lock (slot_linux.c: a claimed board serves itself only): one
     * line, then the close -- jtag_server.c's rule, unchanged. */
    if (mps3_jtag_refuse_peer(incoming)) {
        mps3_net_refuse_with_line(incoming, MPS3_JTAG_LOCKED_LINE);
        return;
    }
    /* Reap before refuse (net_if.h mps3_net_peer_closed), as jtag_server.c. */
    if (J.conn != 0 && mps3_net_peer_closed(J.conn)) {
        drop_client();
    }
    if (J.conn == 0) {
        J.conn = incoming;
        J.st.accepts++;
    } else {
        mps3_net_close(incoming);   /* single client: a second is closed at once */
    }
}

/* One exchange: at most `cap` command bytes in (one recv), their replies out
 * (one send). 1 = more may be waiting, 0 = nothing more this poll. */
static int exchange(uint32_t cap)
{
    int n = mps3_net_recv(J.conn, J.in, cap);
    J.st.recvs++;
    if (n == 0) {
        return 0;
    }
    if (n < 0) {
        drop_client();
        return 0;
    }
    J.st.cmd_bytes += (uint64_t)n;
    uint32_t o = 0u;
    for (int i = 0; i < n; i++) {
        uint8_t reply = 0u;
        int rc = jtag_server_handle_byte(J.in[i], &reply);
        if (reply != 0u) {
            J.out[o++] = reply;
        }
        if (rc != 0) {
            /* 'Q' (1) or a byte outside the protocol (-1): the bytes after it in
             * this chunk are never interpreted, the replies before it still go. */
            J.close_after_flush = 1;
            break;
        }
    }
    J.out_off = 0u;
    J.out_len = o;
    if (flush_replies() != 1) {
        return 0;                     /* back-pressure (kept pending) or dead */
    }
    if (J.close_after_flush) {
        drop_client();
        return 0;
    }
    return 1;
}

void harnessd_jtag_poll(void)
{
    if (!J.lst) {
        J.lst = mps3_net_listen(MPS3_PORT_JTAG);   /* the listener jtag_server_init() opened */
        if (!J.lst) {
            return;
        }
    }
    accept_one();
    if (!J.conn) {
        return;
    }
    /* A swap has the RP isolated / in reset: bytes queue in the kernel, served
     * once ungated (jtag_server.c's rule, unchanged). */
    if (g_shell_state.swd_gated) {
        return;
    }
    if (J.out_off < J.out_len) {
        if (flush_replies() != 1) {
            return;                   /* dead, or still back-pressured: yield */
        }
    }
    if (J.close_after_flush) {
        drop_client();
        return;
    }
    if (g_hd.jtag_per_byte) {
        /* --jtag-io per-byte: jtag_server.c's own loop, kept as the fallback and
         * as the cost model's live negative control -- one recv per byte, one
         * send per reply, JTAG_SERVER_BYTES_PER_POLL (256) bytes a pass. */
        for (unsigned budget = 256u; budget > 0u && J.conn; budget--) {
            if (!exchange(1u)) {
                return;
            }
        }
        return;
    }
    (void)exchange(JTAG_LX_CHUNK);
}

int harnessd_jtag_client_live(void)
{
    return J.conn != 0 && !mps3_net_peer_closed(J.conn);
}

struct mps3_net_conn *harnessd_jtag_conn(void)
{
    return J.conn;
}

void harnessd_jtag_stats(harnessd_jtag_stats_t *out)
{
    *out = J.st;
}

/* Tests: forget everything (the listener included -- fake_net_reset() drops it). */
void harnessd_jtag_reset(void)
{
    memset(&J, 0, sizeof(J));
}
