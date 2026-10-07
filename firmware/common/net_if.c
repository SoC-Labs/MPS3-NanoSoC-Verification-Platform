/*
 * net_if.c — backend-INDEPENDENT helpers for the network seam (net_if.h).
 * The transport functions themselves (mps3_net_listen/accept/recv/send/...)
 * are defined by whichever backend is linked (firmware/test/fake_net_if.c
 * on the host; the future net_if_lwip.c on target) — only pure logic lives
 * here, so this file links into every build unchanged.
 */
#include "net_if.h"

/* The peer queries (net_if.h "who is on the other end"): WEAK "unknown / not
 * local", so every backend that does not know fails closed. */
__attribute__((weak)) int mps3_net_conn_peer(mps3_net_conn_t *conn, mps3_net_addr_t *out)
{
    (void)conn;
    (void)out;
    return -1;
}

__attribute__((weak)) int mps3_net_addr_is_local(const mps3_net_addr_t *a)
{
    (void)a;
    return 0;
}

/* The liveness probe (net_if.h, reap-before-refuse): WEAK "presumed alive", so a
 * backend that cannot tell keeps the old refuse-while-occupied behaviour. */
__attribute__((weak)) int mps3_net_peer_closed(mps3_net_conn_t *conn)
{
    (void)conn;
    return 0;
}

__attribute__((weak)) void mps3_net_addr_text(const mps3_net_addr_t *a, char *out, uint32_t cap)
{
    (void)a;
    if (out && cap >= 2u) {
        out[0] = '?';
        out[1] = '\0';
    }
}

static void refuse_drain(mps3_net_conn_t *conn)
{
    char sink[64];
    for (int i = 0; i < 64 && mps3_net_recv(conn, sink, (uint32_t)sizeof(sink)) > 0; i++) {
    }
}

void mps3_net_refuse_with_line(mps3_net_conn_t *conn, const char *line)
{
    uint32_t n = 0;
    if (!conn) {
        return;
    }
    while (line && line[n]) {
        n++;
    }
    refuse_drain(conn);
    if (n) {
        (void)mps3_net_send(conn, line, n);
    }
    refuse_drain(conn);
    mps3_net_close(conn);
}

void mps3_net_linebuf_reset(mps3_net_linebuf_t *lb)
{
    lb->len = 0;
    lb->overflow = 0;
    lb->done = 0;
}

int mps3_net_linebuf_feed(mps3_net_linebuf_t *lb, char c)
{
    /* A previous feed() returned 1 and left buf/len intact for the caller
     * to consume (single-threaded superloop — no reentrancy); this feed
     * starts the next line. */
    if (lb->done) {
        lb->len = 0;
        lb->done = 0;
    }

    if (c == '\n') {
        if (lb->overflow) {
            /* The line that just terminated had overflowed — its content
             * is gone by design (never parse a truncated prefix). Reset
             * for the next line and tell the caller to answer with an
             * error line. */
            mps3_net_linebuf_reset(lb);
            return -1;
        }
        /* Tolerate CRLF: strip one trailing '\r'. */
        if (lb->len > 0 && lb->buf[lb->len - 1] == '\r') {
            lb->len--;
        }
        lb->done = 1;
        return 1;
    }
    if (lb->overflow) {
        return 0; /* still discarding the oversized line */
    }
    if (lb->len >= MPS3_NET_LINE_MAX) {
        lb->overflow = 1;
        return 0;
    }
    lb->buf[lb->len++] = c;
    return 0;
}
