/*
 * test_posix_net_if.c — the SEAM CONTRACT tests for posix_net_if.c, the third
 * common/net_if.h backend (real POSIX sockets).
 *
 * fake_net_if.c is described in net_if.h as "the executable spec" for that
 * contract. A second backend is only as good as its conformance to the same
 * clauses, so each clause gets a case here, driven by a REAL client socket on
 * loopback:
 *
 *   listen() is idempotent per port      -> test_listen_is_idempotent_per_port
 *   every call is non-blocking           -> test_accept_and_recv_do_not_block
 *   send() may return SHORT              -> test_send_returns_short_under_backpressure
 *   a handle stays valid until close(),
 *   INCLUDING after the peer closes:
 *   drain the buffered bytes FIRST,
 *   THEN observe MPS3_NET_CLOSED         -> test_peer_close_drains_then_closes
 *   a dead peer surfaces as _ERR         -> test_abortive_close_surfaces_as_err
 *   the liveness probe (reap-before-
 *   refuse) answers "gone" only when recv
 *   would, and never takes a byte        -> test_peer_closed_probe
 *   UDP: whole datagrams + peer TID      -> test_udp_round_trip
 *
 * ===========================================================================
 * TWO BINARIES — the drain clause has a real negative control
 * ===========================================================================
 *   test_posix_net_if          positive
 *   test_posix_net_if_nodrain  NEGATIVE CONTROL: the same sources built with
 *                              -DMPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN, which
 *                              makes mps3_net_recv() react to the FIN EVENT
 *                              (POLLRDHUP) instead of to a drained read(). It
 *                              asserts the damage: MPS3_NET_CLOSED comes back
 *                              FIRST and the bytes the peer sent immediately
 *                              before closing are LOST.
 *
 * Without that second binary, test_peer_close_drains_then_closes would be the
 * kind of gate this repo treats as worse than none: on Linux the kernel hands
 * buffered bytes over before reporting EOF, so the assertion would pass for a
 * backend that had never thought about the clause at all. The control proves the
 * assertion can fail, and pins WHICH mistake it catches.
 *
 * The 50 ms settle before the first recv in that test is load-bearing, not
 * politeness: it guarantees the FIN has already arrived, so "bytes first" is a
 * statement about ordering under a pending close rather than about a race the
 * test happened to win.
 */
#define _GNU_SOURCE

#include <arpa/inet.h>
#include <assert.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../common/net_if.h"
#include "posix_net_if.h"

#ifndef POSIX_NET_TEST_NAME
#define POSIX_NET_TEST_NAME "test_posix_net_if"
#endif

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* Logical ports used here are arbitrary but distinct from every real contract
 * port (net_proto.h), and each is MAPPED to 0 so the kernel picks a free
 * ephemeral port: two concurrent runs of this suite can then never collide, and
 * a stale TIME_WAIT socket can never make it flaky. */
#define LPORT_A  4801u
#define LPORT_B  4802u
#define LPORT_U  4803u

static void nap_ms(long ms)
{
    struct timespec ts = { .tv_sec = ms / 1000, .tv_nsec = (ms % 1000) * 1000000L };
    nanosleep(&ts, 0);
}

/* ---- real client side ------------------------------------------------------ */

/* Blocking connect to loopback. Blocking is fine on THIS side: the test is the
 * host, and a connect to a listening loopback socket completes in the kernel
 * without the server having accepted yet (that is what the backlog is for). */
static int cli_connect(uint16_t port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    assert(fd >= 0);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family      = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sa.sin_port        = htons(port);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static void cli_send(int fd, const void *buf, size_t len)
{
    const char *p = (const char *)buf;
    size_t done = 0;
    while (done < len) {
        ssize_t n = send(fd, p + done, len - done, 0);
        assert(n > 0);
        done += (size_t)n;
    }
}

/* Accept the pending connection, allowing for loopback scheduling slop. */
static mps3_net_conn_t *accept_within(mps3_net_listener_t *lst, int tries)
{
    for (int i = 0; i < tries; i++) {
        mps3_net_conn_t *c = mps3_net_accept(lst);
        if (c) {
            return c;
        }
        nap_ms(1);
    }
    return 0;
}

/* Poll mps3_net_recv() until it reports SOMETHING (bytes, CLOSED or ERR). */
static int recv_until(mps3_net_conn_t *c, void *buf, uint32_t cap, int tries)
{
    for (int i = 0; i < tries; i++) {
        int rc = mps3_net_recv(c, buf, cap);
        if (rc != 0) {
            return rc;
        }
        nap_ms(1);
    }
    return 0;
}

/* ---- tests ----------------------------------------------------------------- */

static void test_listen_is_idempotent_per_port(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0); /* ephemeral */
    posix_net_map_port(LPORT_B, 0);

    mps3_net_listener_t *l1 = mps3_net_listen(LPORT_A);
    CHECK(l1 != 0);
    /* The contract's exact words: "Calling twice for the same port returns the
     * SAME listener". Not merely another working listener — the same handle, so
     * a module _init() that runs twice cannot double-bind. */
    mps3_net_listener_t *l2 = mps3_net_listen(LPORT_A);
    CHECK(l2 == l1);

    uint16_t pa = posix_net_actual_port(LPORT_A);
    CHECK(pa != 0);
    CHECK(pa != LPORT_A); /* proves the port MAP is really in effect */

    mps3_net_listener_t *l3 = mps3_net_listen(LPORT_B);
    CHECK(l3 != 0);
    CHECK(l3 != l1);
    uint16_t pb = posix_net_actual_port(LPORT_B);
    CHECK(pb != 0);
    CHECK(pb != pa);

    CHECK(posix_net_actual_port(9999u) == 0); /* not listening */
    posix_net_reset();
}

static void test_accept_and_recv_do_not_block(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    CHECK(lst != 0);

    /* Nothing has connected. A blocking backend would hang here and the suite
     * would never finish — that IS the assertion, and it is why every case in
     * this file is bounded-retry rather than wait-forever. */
    CHECK(mps3_net_accept(lst) == 0);
    CHECK(mps3_net_accept(lst) == 0);

    int cli = cli_connect(posix_net_actual_port(LPORT_A));
    CHECK(cli >= 0);
    mps3_net_conn_t *conn = accept_within(lst, 500);
    CHECK(conn != 0);

    /* Connected but silent: recv must report 0 ("no data right now"), NOT block
     * and NOT confuse silence with a close. */
    char buf[32];
    CHECK(mps3_net_recv(conn, buf, sizeof(buf)) == 0);
    CHECK(mps3_net_recv(conn, buf, 0) == 0); /* zero-cap is a no-op, not an error */

    mps3_net_close(conn);
    close(cli);
    posix_net_reset();
}

static void test_connect_then_recv_bytes(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    int cli = cli_connect(posix_net_actual_port(LPORT_A));
    CHECK(cli >= 0);
    mps3_net_conn_t *conn = accept_within(lst, 500);
    CHECK(conn != 0);

    cli_send(cli, "hello", 5);
    char buf[32];
    memset(buf, 0, sizeof(buf));
    CHECK(recv_until(conn, buf, sizeof(buf), 500) == 5);
    CHECK(memcmp(buf, "hello", 5) == 0);
    CHECK(mps3_net_recv(conn, buf, sizeof(buf)) == 0); /* drained, still open */

    /* And the reverse direction, including the flush the callers all make. */
    CHECK(mps3_net_send(conn, "pong", 4) == 4);
    mps3_net_flush(conn);
    char back[8];
    ssize_t got = 0;
    for (int i = 0; i < 500 && got < 4; i++) {
        ssize_t n = recv(cli, back + got, sizeof(back) - (size_t)got, MSG_DONTWAIT);
        if (n > 0) {
            got += n;
        } else {
            nap_ms(1);
        }
    }
    CHECK(got == 4);
    CHECK(memcmp(back, "pong", 4) == 0);

    mps3_net_close(conn);
    close(cli);
    posix_net_reset();
}

/* THE clause this backend is most likely to get wrong. See the file header for
 * why the settle is load-bearing and why there is a second binary. */
static void test_peer_close_drains_then_closes(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    int cli = cli_connect(posix_net_actual_port(LPORT_A));
    CHECK(cli >= 0);
    mps3_net_conn_t *conn = accept_within(lst, 500);
    CHECK(conn != 0);

    cli_send(cli, "hello", 5);
    close(cli);   /* graceful FIN, immediately behind the payload */
    nap_ms(50);   /* the FIN has certainly arrived by now (loopback) */

    char buf[32];
    memset(buf, 0, sizeof(buf));
    int rc = recv_until(conn, buf, sizeof(buf), 500);

#if defined(MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN)
    /* NEGATIVE CONTROL: the backend answered the FIN event instead of reading,
     * so the payload is GONE. Silent data loss — no error anywhere, the caller
     * simply never sees the last request a client sent before hanging up. */
    CHECK(rc == MPS3_NET_CLOSED);
    CHECK(memcmp(buf, "hello", 5) != 0);
#else
    /* Buffered bytes FIRST... */
    CHECK(rc == 5);
    CHECK(memcmp(buf, "hello", 5) == 0);
    /* ...and only once they are drained, CLOSED. */
    CHECK(recv_until(conn, buf, sizeof(buf), 500) == MPS3_NET_CLOSED);
#endif

    /* CLOSED is sticky and idempotent: a superloop polls the same handle again
     * on the very next pass, and must not see 0 ("still open, no data"). */
    CHECK(mps3_net_recv(conn, buf, sizeof(buf)) == MPS3_NET_CLOSED);
    CHECK(mps3_net_recv(conn, buf, sizeof(buf)) == MPS3_NET_CLOSED);

    /* The handle is still VALID until close() — flushing/closing it is safe. */
    mps3_net_flush(conn);
    mps3_net_close(conn);
    posix_net_reset();
}

static void test_send_returns_short_under_backpressure(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);

    /* Shrink the CLIENT's receive buffer before connecting so the advertised
     * window (and therefore the server's usable send queue) stays small; then
     * never read on the client. */
    int cli = socket(AF_INET, SOCK_STREAM, 0);
    CHECK(cli >= 0);
    int rcvbuf = 4096;
    (void)setsockopt(cli, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family      = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sa.sin_port        = htons(posix_net_actual_port(LPORT_A));
    CHECK(connect(cli, (struct sockaddr *)&sa, sizeof(sa)) == 0);
    mps3_net_conn_t *conn = accept_within(lst, 500);
    CHECK(conn != 0);

    static char chunk[65536];
    memset(chunk, 0xA5, sizeof(chunk));

    /* Bounded: 4096 x 64 KiB = 256 MiB. No kernel socket buffer is that large,
     * so falling off the end means send() blocked or lied, and the CHECK below
     * fails rather than the test hanging. */
    int  saw_short = 0;
    long total     = 0;
    for (int i = 0; i < 4096 && !saw_short; i++) {
        int n = mps3_net_send(conn, chunk, (uint32_t)sizeof(chunk));
        CHECK(n != MPS3_NET_ERR);
        CHECK(n <= (int)sizeof(chunk)); /* never claim more than was offered */
        if (n < (int)sizeof(chunk)) {
            saw_short = 1; /* includes n == 0, "retry on a later poll" */
        }
        total += n;
    }
    CHECK(saw_short);
    CHECK(total > 0); /* it really did move bytes before backpressure hit */

    mps3_net_close(conn);
    close(cli);
    posix_net_reset();
}

/* A peer that dies WITHOUT a FIN (RST) is a transport ERROR, not an orderly
 * close: the caller must treat the handle as dead and close it, rather than
 * waiting politely for bytes that will never come. This is the same surface
 * fake_net_kill_peer() models for the lwIP keepalive-reap path.
 *
 * Compiled out of the NEGATIVE-CONTROL binary: with the deliberate defect in
 * place a RST also latches POLLRDHUP, so the ERR-vs-CLOSED distinction this case
 * exists to pin is not meaningful there. Nothing is lost — that binary's whole
 * job is the drain assertion above. */
#if !defined(MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN)
static void test_abortive_close_surfaces_as_err(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    int cli = cli_connect(posix_net_actual_port(LPORT_A));
    CHECK(cli >= 0);
    mps3_net_conn_t *conn = accept_within(lst, 500);
    CHECK(conn != 0);

    struct linger lg = { .l_onoff = 1, .l_linger = 0 }; /* close() -> RST */
    (void)setsockopt(cli, SOL_SOCKET, SO_LINGER, &lg, sizeof(lg));
    close(cli);
    nap_ms(50);

    char buf[32];
    int rc = recv_until(conn, buf, sizeof(buf), 500);
    CHECK(rc == MPS3_NET_ERR);
    /* Sticky, and a send on a dead handle is an error too (never a silent 0
     * that a caller would retry forever). */
    CHECK(mps3_net_recv(conn, buf, sizeof(buf)) == MPS3_NET_ERR);
    CHECK(mps3_net_send(conn, "x", 1) == MPS3_NET_ERR);

    mps3_net_close(conn);
    posix_net_reset();
}
#endif /* !MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN */

/* mps3_net_peer_closed() (net_if.h, reap-before-refuse): what every single-client
 * service asks before refusing a newcomer. It must say "gone" exactly when the
 * next recv() would report CLOSED/ERR -- never while the peer is alive, never
 * while bytes it sent are unread (that is a request still to serve, even behind
 * a FIN) -- and it must not consume those bytes or change what recv() says.
 * The kernel is the witness: a MSG_PEEK that took the byte, or a probe that
 * answered on the FIN event, fails a CHECK here. */
#if !defined(MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN)
static void test_peer_closed_probe(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    uint16_t p = posix_net_actual_port(LPORT_A);
    char buf[32];

    /* Alive and silent: 0, repeatedly (EAGAIN is not a death). */
    int cli = cli_connect(p);
    CHECK(cli >= 0);
    mps3_net_conn_t *conn = accept_within(lst, 500);
    CHECK(conn != 0);
    CHECK(mps3_net_peer_closed(conn) == 0);
    CHECK(mps3_net_peer_closed(conn) == 0);

    /* Alive with unread bytes: 0, and the bytes are all still there. */
    cli_send(cli, "ping\n", 5);
    nap_ms(20);
    CHECK(mps3_net_peer_closed(conn) == 0);
    CHECK(mps3_net_peer_closed(conn) == 0);
    memset(buf, 0, sizeof(buf));
    CHECK(recv_until(conn, buf, sizeof(buf), 500) == 5);
    CHECK(memcmp(buf, "ping\n", 5) == 0);

    /* FIN behind unread bytes: still 0 until they are drained... */
    cli_send(cli, "bye", 3);
    close(cli);
    nap_ms(50);                                   /* the FIN has certainly arrived */
    CHECK(mps3_net_peer_closed(conn) == 0);
    memset(buf, 0, sizeof(buf));
    CHECK(recv_until(conn, buf, sizeof(buf), 500) == 3);
    CHECK(memcmp(buf, "bye", 3) == 0);
    /* ...then 1 -- BEFORE the caller's own recv has seen the EOF, which is the
     * whole point (the service meets the newcomer first) -- and the probe left
     * recv's answer alone. */
    CHECK(mps3_net_peer_closed(conn) == 1);
    CHECK(mps3_net_peer_closed(conn) == 1);
    CHECK(mps3_net_recv(conn, buf, sizeof(buf)) == MPS3_NET_CLOSED);
    CHECK(mps3_net_peer_closed(conn) == 1);       /* and after recv reported it */
    mps3_net_close(conn);
    CHECK(mps3_net_peer_closed(conn) == 1);       /* a released handle is gone */
    CHECK(mps3_net_peer_closed(0) == 1);

    /* An abortive close (RST) with nothing unread: 1, and recv still says ERR. */
    cli = cli_connect(p);
    CHECK(cli >= 0);
    conn = accept_within(lst, 500);
    CHECK(conn != 0);
    struct linger lg = { .l_onoff = 1, .l_linger = 0 };
    (void)setsockopt(cli, SOL_SOCKET, SO_LINGER, &lg, sizeof(lg));
    close(cli);
    nap_ms(50);
    CHECK(mps3_net_peer_closed(conn) == 1);
    CHECK(recv_until(conn, buf, sizeof(buf), 500) == MPS3_NET_ERR);
    mps3_net_close(conn);
    posix_net_reset();
}
#endif /* !MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN */

static void test_two_clients_get_independent_conns(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    uint16_t p = posix_net_actual_port(LPORT_A);

    int c1 = cli_connect(p);
    int c2 = cli_connect(p);
    CHECK(c1 >= 0 && c2 >= 0);
    mps3_net_conn_t *k1 = accept_within(lst, 500);
    mps3_net_conn_t *k2 = accept_within(lst, 500);
    CHECK(k1 != 0 && k2 != 0);
    CHECK(k1 != k2);

    cli_send(c1, "one", 3);
    cli_send(c2, "twotwo", 6);
    char b[16];
    memset(b, 0, sizeof(b));
    CHECK(recv_until(k1, b, sizeof(b), 500) == 3);
    CHECK(memcmp(b, "one", 3) == 0);
    memset(b, 0, sizeof(b));
    CHECK(recv_until(k2, b, sizeof(b), 500) == 6);
    CHECK(memcmp(b, "twotwo", 6) == 0);

    mps3_net_close(k1);
    mps3_net_close(k2);
    close(c1);
    close(c2);
    posix_net_reset();
}

/* Slots must be recycled: a long-lived daemon accepts far more connections than
 * the table has entries (Identify alone opens one per session). */
static void test_conn_slots_are_recycled(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    uint16_t p = posix_net_actual_port(LPORT_A);

    for (int i = 0; i < 24; i++) { /* >> the 8-entry conn table */
        int cli = cli_connect(p);
        CHECK(cli >= 0);
        mps3_net_conn_t *conn = accept_within(lst, 500);
        CHECK(conn != 0);
        mps3_net_close(conn);
        close(cli);
    }
    posix_net_reset();
}

static void test_udp_round_trip(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_U, 0);

    mps3_net_udp_t *u = mps3_net_udp_open(LPORT_U);
    CHECK(u != 0);
    CHECK(mps3_net_udp_open(LPORT_U) == u); /* same idempotency rule as listen */
    uint16_t sp = posix_net_actual_port(LPORT_U);
    CHECK(sp != 0);

    /* Nothing queued: 0, not an error, not a block. */
    char buf[600];
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), 0) == 0);

    int cli = socket(AF_INET, SOCK_DGRAM, 0);
    CHECK(cli >= 0);
    struct sockaddr_in me;
    memset(&me, 0, sizeof(me));
    me.sin_family      = AF_INET;
    me.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    me.sin_port        = 0; /* ephemeral TID, RFC1350-style */
    CHECK(bind(cli, (struct sockaddr *)&me, sizeof(me)) == 0);
    socklen_t ml = sizeof(me);
    CHECK(getsockname(cli, (struct sockaddr *)&me, &ml) == 0);
    uint16_t cli_port = ntohs(me.sin_port);
    CHECK(cli_port != 0);

    struct sockaddr_in to;
    memset(&to, 0, sizeof(to));
    to.sin_family      = AF_INET;
    to.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    to.sin_port        = htons(sp);
    CHECK(sendto(cli, "RRQ\0file", 8, 0, (struct sockaddr *)&to, sizeof(to)) == 8);

    mps3_net_addr_t from;
    memset(&from, 0, sizeof(from));
    int rc = 0;
    for (int i = 0; i < 500 && rc == 0; i++) {
        rc = mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from);
        if (rc == 0) {
            nap_ms(1);
        }
    }
    CHECK(rc == 8);
    CHECK(memcmp(buf, "RRQ\0file", 8) == 0);
    /* The peer's TID is what a TFTP reply has to go back to. */
    CHECK(from.port == cli_port);
    CHECK(from.ip != 0);

    /* Echo it back to the recorded address, verbatim. */
    CHECK(mps3_net_udp_sendto(u, "DATA", 4, &from) == 4);
    char back[64];
    ssize_t n = -1;
    for (int i = 0; i < 500 && n < 0; i++) {
        n = recv(cli, back, sizeof(back), MSG_DONTWAIT);
        if (n < 0) {
            nap_ms(1);
        }
    }
    CHECK(n == 4);
    CHECK(memcmp(back, "DATA", 4) == 0);

    mps3_net_udp_close(u);
    close(cli);
    posix_net_reset();
}

static void test_null_and_dead_handles_are_safe(void)
{
    posix_net_reset();
    char buf[8];
    /* "Handles are opaque pointers owned by the backend. NULL = invalid." */
    CHECK(mps3_net_accept(0) == 0);
    CHECK(mps3_net_recv(0, buf, sizeof(buf)) == MPS3_NET_ERR);
    CHECK(mps3_net_send(0, buf, sizeof(buf)) == MPS3_NET_ERR);
    CHECK(mps3_net_udp_recvfrom(0, buf, sizeof(buf), 0) == MPS3_NET_ERR);
    mps3_net_addr_t to = { .ip = 0, .port = 1 };
    CHECK(mps3_net_udp_sendto(0, buf, 1, &to) == MPS3_NET_ERR);
    /* "Safe on NULL" for every void-returning call. */
    mps3_net_flush(0);
    mps3_net_close(0);
    mps3_net_udp_close(0);
    mps3_net_set_manual_window(0, 0);
    mps3_net_recved(0, 0);
    s_checks += 5; /* the five no-crash calls above */
    posix_net_reset();
}

/* The v0.14 peer queries (net_if.h "who is on the other end"), which the Linux
 * harness's slot lock is built on: a real loopback client is named and local, a
 * datagram's sender is reported in the same form, and an address off this host
 * -- the one case a host test cannot connect from -- is NOT local. */
static void test_peer_queries(void)
{
    posix_net_reset();
    posix_net_map_port(LPORT_A, 0);
    mps3_net_listener_t *lst = mps3_net_listen(LPORT_A);
    int cli = cli_connect(posix_net_actual_port(LPORT_A));
    CHECK(cli >= 0);
    mps3_net_conn_t *conn = accept_within(lst, 500);
    CHECK(conn != 0);
    mps3_net_addr_t a;
    memset(&a, 0, sizeof(a));
    CHECK(mps3_net_conn_peer(conn, &a) == 0);
    CHECK(mps3_net_addr_is_local(&a) == 1);
    char t[48];
    mps3_net_addr_text(&a, t, sizeof(t));
    CHECK(strncmp(t, "127.0.0.1:", 10) == 0);
    struct sockaddr_in me;
    socklen_t ml = sizeof(me);
    CHECK(getsockname(cli, (struct sockaddr *)&me, &ml) == 0);
    CHECK(a.port == ntohs(me.sin_port));            /* the client's own port */

    mps3_net_addr_t off_host = { htonl(0x0A000001u), 1234 };   /* 10.0.0.1 */
    CHECK(mps3_net_addr_is_local(&off_host) == 0);
    mps3_net_addr_text(&off_host, t, sizeof(t));
    CHECK(strcmp(t, "10.0.0.1:1234") == 0);
    mps3_net_addr_t lo2 = { htonl(0x7F000003u), 1 };            /* 127.0.0.3: still this host */
    CHECK(mps3_net_addr_is_local(&lo2) == 1);
    CHECK(mps3_net_addr_is_local(0) == 0);
    CHECK(mps3_net_conn_peer(0, &a) < 0);           /* no connection: unknown */

    mps3_net_close(conn);
    close(cli);
    posix_net_reset();
}

int main(void)
{
    test_peer_queries();
    test_listen_is_idempotent_per_port();
    test_accept_and_recv_do_not_block();
    test_connect_then_recv_bytes();
    test_peer_close_drains_then_closes();
    test_send_returns_short_under_backpressure();
    test_two_clients_get_independent_conns();
    test_conn_slots_are_recycled();
    test_udp_round_trip();
    test_null_and_dead_handles_are_safe();
#if !defined(MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN)
    test_abortive_close_surfaces_as_err(); /* see its definition for why it is
                                            * absent from the control binary */
    test_peer_closed_probe();              /* the same RST sub-case, so the same
                                            * reason to keep it out of the control */
#endif

    printf("%s: %d checks passed\n", POSIX_NET_TEST_NAME, s_checks);
    return 0;
}
