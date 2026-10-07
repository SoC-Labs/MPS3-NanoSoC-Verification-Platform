/*
 * coordinator_net.c — the control channel's TRANSPORT half (W-NET-SEAM):
 * TCP 6900 against common/net_if.h. coordinator.c owns dispatch + the verb
 * handlers; this file owns the pcb-shaped bookkeeping those deliberately
 * left open — line assembly, strict request->response pacing, and the
 * HELD `swap` response (net-protocol.md shows swap as one request/response
 * pair; on this single-threaded server that is a parked connection, not a
 * second ack+result exchange).
 *
 * Held-swap-response resolution (the coordinator.h "W-NET-SEAM owns the
 * pcb bookkeeping" TODO): when coordinator_dispatch_line() returns 0, the
 * CONNECTION HANDLE ITSELF is the parked token — s_conn simply stays open
 * with s_owed set, no reads are serviced until the response goes
 * out, and each poll asks coordinator_held_poll() whether the verb has
 * settled. No handle needs threading through swap_fsm at all: 6900 is
 * single-client and single-request-in-flight, so "whoever is connected
 * asked" is exact. If the client disconnects while parked, the response is
 * dropped (the swap itself still completes — same as the fake shell, where
 * a client that hangs up mid-swap just never reads its reply).
 *
 * v0.13 (D13): the same parking serves every DEFERRED verb -- an accepted
 * `swap`, a `commit` (the pair then arrives over 6910) and a `usd` format /
 * clear -- through coordinator_held_poll(). The held verb is polled to
 * completion EVERY pass, whether or not its client is still connected: a commit
 * or a format has to finish (and release the store) even if nobody will read the
 * answer. s_owed is "the CURRENT client is owed that answer".
 */
#include <string.h>
#include "coordinator.h"
#include "swap_fsm.h"
#include "../common/net_if.h"

static mps3_net_listener_t *s_listener;
static mps3_net_conn_t     *s_conn;
static mps3_net_linebuf_t   s_lb;

/* Outbound response staging: encoded lines wait here until the backend has
 * accepted every byte (mps3_net_send may take fewer under backpressure). */
static char s_out[MPS3_CTRL_RESP_MAX];
static int  s_out_len;
static int  s_out_sent;

static int s_owed;   /* the current client is owed the held (deferred) response */

void coordinator_net_init(void)
{
    s_listener = mps3_net_listen(MPS3_PORT_CONTROL);
    s_conn = 0;
    mps3_net_linebuf_reset(&s_lb);
    s_out_len = 0;
    s_out_sent = 0;
    s_owed = 0;
}

struct mps3_net_conn *coordinator_net_active_conn(void)
{
    return s_conn;
}

static void drop_client(void)
{
    mps3_net_close(s_conn);
    s_conn = 0;
    mps3_net_linebuf_reset(&s_lb);
    s_out_len = 0;
    s_out_sent = 0;
    s_owed = 0; /* the held response (if any) is dropped with the client; the
                 * held VERB still runs to completion (coordinator_held_poll) */
}

/* Queue an encoded, newline-terminated response line. */
static void queue_response(const char *line, int len)
{
    if (len > (int)sizeof(s_out)) {
        len = (int)sizeof(s_out); /* unreachable: encoder is bounded by the same max */
    }
    memcpy(s_out, line, (size_t)len);
    s_out_len = len;
    s_out_sent = 0;
}

/* Push queued bytes; returns 1 when the queue is empty. */
static int flush_out(void)
{
    while (s_out_sent < s_out_len) {
        int n = mps3_net_send(s_conn, &s_out[s_out_sent],
                              (uint32_t)(s_out_len - s_out_sent));
        if (n == MPS3_NET_ERR) {
            drop_client();
            return 0;
        }
        if (n == 0) {
            return 0; /* backpressure — retry next poll */
        }
        s_out_sent += n;
    }
    s_out_len = 0;
    s_out_sent = 0;
    return 1;
}

void coordinator_net_poll(void)
{
    /* v0.11 `reboot`: arm the watchdog once the reply has had its head start.
     * Here rather than in a table row because this function already runs every
     * pass AND after every A4 interleave point, and the table is full. */
    coordinator_reboot_poll();

    /* Accept: adopt a client when none is connected; refuse extras
     * (single-client v1 — see coordinator.h).
     *
     * REAP BEFORE REFUSE (2026-09-28). Accept runs FIRST in the pass, before the
     * current client's EOF is read below -- and a parked client is not read at
     * all -- so a client that closed and at once reconnected found its old
     * connection still registered and was refused, every time (the 6900 soak
     * RST, Harness Manager's back-to-back requests). So a newcomer first asks
     * whether the current client is already gone; if it is, it is dropped
     * exactly as a read EOF would drop it (a held response is dropped with it;
     * the held VERB still settles below) and the newcomer is adopted. A live
     * current client -- or one whose last request is still unread -- keeps the
     * slot and the newcomer is refused, as before. */
    mps3_net_conn_t *incoming = mps3_net_accept(s_listener);
    if (incoming) {
        if (s_conn != 0 && mps3_net_peer_closed(s_conn)) {
            drop_client();
        }
        if (s_conn == 0) {
            s_conn = incoming;
            mps3_net_linebuf_reset(&s_lb);
            s_out_len = 0;
            s_out_sent = 0;
            s_owed = 0;
        } else {
            mps3_net_close(incoming);
        }
    }
    /* A held verb settles whether or not anybody is still waiting for it. */
    if (coordinator_held_pending()) {
        mps3_ctrl_response_t resp;
        if (coordinator_held_poll(&resp)) {
            if (s_owed && s_conn) {
                char line[MPS3_CTRL_RESP_MAX];
                int len = mps3_ctrl_encode_response(&resp, line, (int)sizeof(line));
                if (len < 0) {
                    /* Encoder failure is a firmware bug; emit the minimal error
                     * line rather than going silent (same policy as dispatch's
                     * encode_or_fallback). */
                    len = (int)strlen("{\"ok\":false,\"err\":\"encode overflow\"}\n");
                    memcpy(line, "{\"ok\":false,\"err\":\"encode overflow\"}\n", (size_t)len);
                }
                s_owed = 0;
                queue_response(line, len);
                (void)flush_out();
                return;
            }
            s_owed = 0;
        }
    }

    if (!s_conn) {
        return;
    }

    /* Finish any half-sent response before anything else (strict
     * request->response ordering on the wire). */
    if (s_out_len > 0 && !flush_out()) {
        return;
    }
    if (!s_conn) {
        return; /* flush_out may have dropped the client */
    }

    /* Parked: the held verb has not settled yet (above). No input is serviced
     * while a response is owed (strict request->response pacing). */
    if (s_owed) {
        return;
    }

    /* Read + assemble lines. Byte-at-a-time into the line buffer keeps
     * "stop exactly at the line that parked us" trivial — control-channel
     * traffic is tiny (one ~40-byte line per exchange), so this costs
     * nothing that matters. */
    for (;;) {
        char c;
        int n = mps3_net_recv(s_conn, &c, 1);
        if (n == 0) {
            return;
        }
        if (n < 0) {
            drop_client(); /* closed or errored */
            return;
        }
        int r = mps3_net_linebuf_feed(&s_lb, c);
        if (r == 0) {
            continue;
        }
        char line[MPS3_CTRL_RESP_MAX];
        int len;
        if (r < 0) {
            /* Oversized line: fail closed with the uniform error shape --
             * feed a deliberately-invalid line through the normal dispatch
             * path so the "bad json" response comes from the one real
             * encoder. */
            len = coordinator_dispatch_line("", 0, line, (int)sizeof(line));
            (void)len;
            len = (int)strlen(line);
            queue_response(line, len);
        } else {
            int rc = coordinator_dispatch_line(s_lb.buf, s_lb.len, line, (int)sizeof(line));
            if (rc == 0) {
                /* A deferred verb (swap / commit / usd format|clear): park the
                 * connection; the held response is built once it settles. */
                s_owed = 1;
                return;
            }
            len = (int)strlen(line);
            queue_response(line, len);
        }
        if (!flush_out()) {
            return; /* backpressure or client gone — resume next poll */
        }
        if (!s_conn) {
            return;
        }
    }
}
