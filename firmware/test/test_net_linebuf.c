/*
 * test_net_linebuf.c — host-gcc unit tests for the seam's pure helpers
 * (common/net_if.c: JSON-lines assembly). Also sanity-checks the harness's
 * own fake_net_if.c backend (ring FIFO order, idempotent listen, closed
 * semantics, UDP FIFO) — the fake is the executable spec every seam-driven
 * module test builds on, so its own behavior is pinned here first.
 *
 * Links: common/net_if.c, fake_net_if.c. No mocks, no firmware modules.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../common/net_if.h"
#include "fake_net_if.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- linebuf ---------------------------------------------------------- */

static void feed_str(mps3_net_linebuf_t *lb, const char *s, int *last_rc)
{
    for (const char *p = s; *p; p++) {
        *last_rc = mps3_net_linebuf_feed(lb, *p);
    }
}

static void test_linebuf_basic_lines(void)
{
    mps3_net_linebuf_t lb;
    mps3_net_linebuf_reset(&lb);
    int rc = 0;

    feed_str(&lb, "{\"op\":\"ping\"}", &rc);
    CHECK(rc == 0); /* no newline yet */
    rc = mps3_net_linebuf_feed(&lb, '\n');
    CHECK(rc == 1);
    CHECK(lb.len == (int)strlen("{\"op\":\"ping\"}"));
    CHECK(memcmp(lb.buf, "{\"op\":\"ping\"}", (size_t)lb.len) == 0);

    /* Next line starts fresh (buf was readable until this feed). */
    feed_str(&lb, "x\n", &rc);
    CHECK(rc == 1);
    CHECK(lb.len == 1 && lb.buf[0] == 'x');
}

static void test_linebuf_crlf_stripped(void)
{
    mps3_net_linebuf_t lb;
    mps3_net_linebuf_reset(&lb);
    int rc = 0;
    feed_str(&lb, "abc\r\n", &rc);
    CHECK(rc == 1);
    CHECK(lb.len == 3);
    CHECK(memcmp(lb.buf, "abc", 3) == 0);
}

static void test_linebuf_empty_line(void)
{
    mps3_net_linebuf_t lb;
    mps3_net_linebuf_reset(&lb);
    CHECK(mps3_net_linebuf_feed(&lb, '\n') == 1);
    CHECK(lb.len == 0);
}

static void test_linebuf_overflow_fails_closed(void)
{
    mps3_net_linebuf_t lb;
    mps3_net_linebuf_reset(&lb);
    int rc = 0;
    for (int i = 0; i < MPS3_NET_LINE_MAX + 50; i++) {
        rc = mps3_net_linebuf_feed(&lb, 'a');
        assert(rc == 0); /* never reports a line mid-overflow */
    }
    s_checks++; /* the loop above held */
    rc = mps3_net_linebuf_feed(&lb, '\n');
    CHECK(rc == -1); /* overflowed line reported as an error, content gone */
    /* And the NEXT line works normally. */
    feed_str(&lb, "ok\n", &rc);
    CHECK(rc == 1);
    CHECK(lb.len == 2 && memcmp(lb.buf, "ok", 2) == 0);
}

/* ---- fake_net_if: TCP ---------------------------------------------------- */

static void test_fake_tcp_roundtrip_and_close(void)
{
    fake_net_reset();
    mps3_net_listener_t *lst = mps3_net_listen(6900);
    CHECK(lst != NULL);
    CHECK(mps3_net_listen(6900) == lst); /* idempotent re-listen */

    CHECK(fake_net_connect(7000) == -1); /* nothing listens there */

    int cli = fake_net_connect(6900);
    CHECK(cli >= 0);
    CHECK(fake_net_pending_accepts(6900) == 1);

    mps3_net_conn_t *conn = mps3_net_accept(lst);
    CHECK(conn != NULL);
    CHECK(mps3_net_accept(lst) == NULL); /* consumed */

    /* client -> firmware, order preserved */
    CHECK(fake_net_send(cli, "hello", 5) == 5);
    char buf[8];
    CHECK(mps3_net_recv(conn, buf, 2) == 2);
    CHECK(memcmp(buf, "he", 2) == 0);
    CHECK(mps3_net_recv(conn, buf, 8) == 3);
    CHECK(memcmp(buf, "llo", 3) == 0);
    CHECK(mps3_net_recv(conn, buf, 8) == 0); /* drained, still open */

    /* firmware -> client, with a send limit exercised */
    fake_net_set_send_limit(cli, 3);
    CHECK(mps3_net_send(conn, "abcdef", 6) == 3); /* backpressure: short */
    fake_net_set_send_limit(cli, -1);
    CHECK(mps3_net_send(conn, "def", 3) == 3);
    CHECK(fake_net_recv(cli, buf, 8) == 6);
    CHECK(memcmp(buf, "abcdef", 6) == 0);

    /* client closes: firmware drains remaining bytes, THEN sees CLOSED */
    CHECK(fake_net_send(cli, "z", 1) == 1);
    fake_net_close(cli);
    CHECK(mps3_net_recv(conn, buf, 8) == 1);
    CHECK(buf[0] == 'z');
    CHECK(mps3_net_recv(conn, buf, 8) == MPS3_NET_CLOSED);
    mps3_net_close(conn);
}

static void test_fake_tcp_fw_close_visible_to_client(void)
{
    fake_net_reset();
    mps3_net_listener_t *lst = mps3_net_listen(6910);
    int cli = fake_net_connect(6910);
    mps3_net_conn_t *conn = mps3_net_accept(lst);
    CHECK(conn != NULL);
    CHECK(fake_net_fw_closed(cli) == 0);
    CHECK(mps3_net_send(conn, "ok", 2) == 2);
    mps3_net_close(conn);
    CHECK(fake_net_fw_closed(cli) == 1);
    char buf[4];
    CHECK(fake_net_recv(cli, buf, 4) == 2); /* bytes sent before close still readable */
    fake_net_close(cli);
}

/* ---- fake_net_if: UDP ---------------------------------------------------- */

static void test_fake_udp_fifo_and_addressing(void)
{
    fake_net_reset();
    mps3_net_udp_t *sock = mps3_net_udp_open(69);
    CHECK(sock != NULL);
    CHECK(mps3_net_udp_open(69) == sock); /* idempotent */

    CHECK(fake_net_udp_inject(69, 5001, "one", 3) == 0);
    CHECK(fake_net_udp_inject(69, 5002, "two", 3) == 0);
    CHECK(fake_net_udp_inject(70, 5001, "x", 1) == -1); /* nothing bound */

    char buf[8];
    mps3_net_addr_t from;
    CHECK(mps3_net_udp_recvfrom(sock, buf, sizeof(buf), &from) == 3);
    CHECK(memcmp(buf, "one", 3) == 0 && from.port == 5001); /* FIFO order */
    CHECK(mps3_net_udp_recvfrom(sock, buf, sizeof(buf), &from) == 3);
    CHECK(memcmp(buf, "two", 3) == 0 && from.port == 5002);
    CHECK(mps3_net_udp_recvfrom(sock, buf, sizeof(buf), &from) == 0);

    /* firmware replies (e.g. TFTP ACK from an ephemeral TID) */
    mps3_net_udp_t *tid = mps3_net_udp_open(0);
    CHECK(tid != NULL && tid != sock);
    from.port = 5001;
    CHECK(mps3_net_udp_sendto(tid, "ack", 3, &from) == 3);
    uint16_t fw_port = 0, to_port = 0;
    CHECK(fake_net_udp_take_sent(&fw_port, &to_port, buf, sizeof(buf)) == 3);
    CHECK(memcmp(buf, "ack", 3) == 0);
    CHECK(to_port == 5001);
    CHECK(fw_port != 69); /* a NEW TID, per RFC1350 */
    CHECK(fake_net_udp_take_sent(NULL, NULL, buf, sizeof(buf)) == -1);
    mps3_net_udp_close(tid);
    mps3_net_udp_close(sock);
}

int main(void)
{
    test_linebuf_basic_lines();
    test_linebuf_crlf_stripped();
    test_linebuf_empty_line();
    test_linebuf_overflow_fails_closed();
    test_fake_tcp_roundtrip_and_close();
    test_fake_tcp_fw_close_visible_to_client();
    test_fake_udp_fifo_and_addressing();

    printf("test_net_linebuf: %d checks passed\n", s_checks);
    return 0;
}
