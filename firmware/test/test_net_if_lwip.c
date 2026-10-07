/*
 * test_net_if_lwip.c — host-gcc tests for firmware/platform/src/net_if_lwip.c,
 * the lwIP backend of common/net_if.h.
 *
 * WHY THIS FILE EXISTS. net_if_lwip.c is ~800 lines, it is the ONLY firmware
 * translation unit that includes an lwIP header, and it is where the 6910
 * receive-path incident lived — and it had no host test at all, because
 * "target-only" was taken to mean "untestable". It is not: the lwIP surface it
 * uses is small and mechanical, so firmware/test/fake_lwip/ provides a shim
 * lwIP (real pbuf refcounting, a tcp_recved() call log, a settable send
 * buffer) and THIS TEST COMPILES THE REAL FILE against it — #include of the
 * production source, never a copy, so the code under test cannot drift away
 * from the code that ships.
 *
 * HOW IT REACHES THE INTERESTING CODE. conn_recv_cb / conn_err_cb /
 * listener_accept_cb / udp_recv_cb are static; nothing may name them. They are
 * driven the way lwIP drives them: net_if_lwip.c registers them via
 * tcp_recv()/tcp_err()/tcp_accept()/udp_recv(), the shim remembers the
 * pointers, and fake_tcp_* / fake_udp_* (fake_lwip_ctl.h) fire them. So the
 * registration path is under test too — a callback wired to the wrong pcb
 * would fail here.
 *
 * Links: net_if_lwip.c (INCLUDED, not linked), fake_lwip.c, mock_regs.c,
 * fake_lan9220.c, ../smsc911x/smsc911x.c. -DMPS3_HAL_MOCK -I fake_lwip.
 * The linkoutput/TX-backpressure half lives in test_net_if_lwip_tx.c.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "mock_regs.h"
#include "fake_lan9220.h"
#include "fake_lwip_ctl.h"

/* THE FILE UNDER TEST. Included rather than linked for two reasons: its
 * per-connection state (s_conns/s_listeners/s_udps) is static with no reset
 * entry point, so a test binary with more than one case must be able to zero
 * it between cases; and the rx-ring assertions are about conn->rx_off, which
 * is the field the incident was about. */
#include "../platform/src/net_if_lwip.c"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* Any page works: the LAN9220 base is an init argument, not a platform_regs.h
 * block (same convention as test_smsc911x.c). */
#define LAN_BASE 0x60000000u

static const uint8_t MAC[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };

/* Zero net_if_lwip.c's own static tables. There is no production reset entry
 * point (on target the shell boots once), so the test owns this. */
static void backend_reset(void)
{
    memset(s_listeners, 0, sizeof(s_listeners));
    memset(s_conns, 0, sizeof(s_conns));
    memset(s_udps, 0, sizeof(s_udps));
    memset(&s_netif, 0, sizeof(s_netif));
    s_tx_fifo_full_drops = 0;
    s_tx_space_stalls = 0;
    s_tx_iface_errors = 0;
}

static void fresh(void)
{
    mock_regs_reset();
    fake_lan9220_reset(LAN_BASE);
    fake_lwip_reset_all();
    backend_reset();
    CHECK(mps3_net_lwip_init(LAN_BASE, MAC) == 0);
    CHECK(fake_lwip_init_calls() == 1);
    CHECK(fake_pbuf_live() == 0);   /* every case starts from a clean pool */
}

static mps3_net_conn_t *connect_one(mps3_net_listener_t *l, struct tcp_pcb **out)
{
    err_t rc = ERR_VAL;
    struct tcp_pcb *pcb = fake_tcp_incoming(l->pcb, &rc);
    CHECK(rc == ERR_OK);
    mps3_net_conn_t *c = mps3_net_accept(l);
    CHECK(c != NULL);
    CHECK(c->pcb == pcb);
    if (out) {
        *out = pcb;
    }
    return c;
}

/* Three pbufs cat'd into one chain — the shape lwIP delivers a segment burst
 * in, and the shape mps3_net_recv() must walk without losing a byte or a ref. */
static struct pbuf *chain3(const char *a, const char *b, const char *c)
{
    struct pbuf *p1 = fake_pbuf_make(a, (int)strlen(a));
    struct pbuf *p2 = fake_pbuf_make(b, (int)strlen(b));
    struct pbuf *p3 = fake_pbuf_make(c, (int)strlen(c));
    CHECK(p1 != NULL && p2 != NULL && p3 != NULL);
    pbuf_cat(p1, p2);
    pbuf_cat(p1, p3);
    CHECK(p1->tot_len == (u16_t)(strlen(a) + strlen(b) + strlen(c)));
    return p1;
}

/* ==========================================================================
 * 1. rx ring / recv path
 * ========================================================================== */

static void test_rx_chain_consumed_across_segments(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    CHECK(l != NULL);
    mps3_net_conn_t *c = connect_one(l, NULL);

    CHECK(fake_tcp_deliver(c->pcb, chain3("ABCD", "EFG", "HIJKL"), ERR_OK) == ERR_OK);
    CHECK(fake_pbuf_live() == 3);
    CHECK(c->rx != NULL && c->rx->tot_len == 12);

    char buf[32];
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 12);
    CHECK(memcmp(buf, "ABCDEFGHIJKL", 12) == 0);
    CHECK(c->rx == NULL);
    CHECK(c->rx_off == 0);

    /* The head pbuf is freed EXACTLY once: the pool is back to baseline (no
     * leak) AND the shim logged no refcount fault (no double free). Both
     * halves matter — the detach dance in mps3_net_recv() (pbuf_ref(next)
     * before pbuf_free(p)) gets one or the other wrong if it is edited. */
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);

    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 0); /* nothing left, still open */

    /* Dead handles are errors, not crashes: net_if.h's "NULL = invalid" and
     * "the handle is the backend's until close" both land here. */
    CHECK(mps3_net_recv(NULL, buf, sizeof(buf)) == MPS3_NET_ERR);
    mps3_net_close(c);
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == MPS3_NET_ERR);
    mps3_net_close(NULL);
    mps3_net_close(c);          /* double close is a no-op, not a double free */
    CHECK(fake_pbuf_faults() == 0);
}

static void test_rx_partial_reads_leave_remainder_drainable(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    mps3_net_conn_t *c = connect_one(l, NULL);
    CHECK(fake_tcp_deliver(c->pcb, chain3("ABCD", "EFG", "HIJKL"), ERR_OK) == ERR_OK);

    char buf[32];
    uint32_t queued = 0;

    /* Short of the head pbuf: nothing is freed, rx_off carries the position. */
    CHECK(mps3_net_recv(c, buf, 2) == 2);
    CHECK(memcmp(buf, "AB", 2) == 0);
    CHECK(c->rx_off == 2);
    CHECK(fake_pbuf_live() == 3);
    mps3_net_lwip_conn_diag(c, NULL, NULL, &queued, NULL, NULL);
    CHECK(queued == 10); /* tot_len 12 minus the 2 already handed out */

    /* Straddles a segment boundary: finishes pbuf 1, consumes all of pbuf 2,
     * and stops exactly on the cap with rx_off back at 0. */
    CHECK(mps3_net_recv(c, buf, 5) == 5);
    CHECK(memcmp(buf, "CDEFG", 5) == 0);
    CHECK(c->rx_off == 0);
    CHECK(fake_pbuf_live() == 1);

    CHECK(mps3_net_recv(c, buf, 3) == 3);
    CHECK(memcmp(buf, "HIJ", 3) == 0);
    CHECK(c->rx_off == 3);

    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 2);
    CHECK(memcmp(buf, "KL", 2) == 0);
    CHECK(c->rx == NULL);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
    mps3_net_close(c);
}

static void test_rx_second_delivery_appends_to_the_chain(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    mps3_net_conn_t *c = connect_one(l, NULL);

    CHECK(fake_tcp_deliver(c->pcb, fake_pbuf_make("ABCD", 4), ERR_OK) == ERR_OK);
    char buf[32];
    CHECK(mps3_net_recv(c, buf, 2) == 2);   /* read cursor now sits at 2 */
    CHECK(c->rx_off == 2);

    /* A second segment arrives while the first is only PARTLY consumed. It is
     * cat'd onto the live chain, and the append must not disturb rx_off — a
     * backend that reset the cursor here would re-deliver "AB", which is the
     * shape of a stream corruption that only shows up under a busy sender. */
    CHECK(fake_tcp_deliver(c->pcb, fake_pbuf_make("EFG", 3), ERR_OK) == ERR_OK);
    CHECK(c->rx_off == 2);
    CHECK(c->rx->tot_len == 7);
    uint32_t queued = 0;
    mps3_net_lwip_conn_diag(c, NULL, NULL, &queued, NULL, NULL);
    CHECK(queued == 5);

    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 5);
    CHECK(memcmp(buf, "CDEFG", 5) == 0);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
    mps3_net_close(c);
}

static void test_rx_fin_mid_buffer_drains_before_closed(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    mps3_net_conn_t *c = connect_one(l, NULL);

    CHECK(fake_tcp_deliver(c->pcb, fake_pbuf_make("WXYZ", 4), ERR_OK) == ERR_OK);
    CHECK(fake_tcp_deliver_fin(c->pcb) == ERR_OK);   /* FIN arrives mid-buffer */
    CHECK(c->peer_closed == 1);

    /* net_if.h's clause: buffered bytes come out FIRST, and only a drained
     * buffer may report CLOSED. A backend that checked peer_closed before the
     * copy loop would eat these four bytes. */
    char buf[8];
    CHECK(mps3_net_recv(c, buf, 2) == 2);
    CHECK(memcmp(buf, "WX", 2) == 0);
    CHECK(mps3_net_recv(c, buf, 2) == 2);
    CHECK(memcmp(buf, "YZ", 2) == 0);
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == MPS3_NET_CLOSED);
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == MPS3_NET_CLOSED); /* and stays closed */
    CHECK(fake_pbuf_live() == 0);
    mps3_net_close(c);
}

static void test_rx_error_reported_only_after_drain(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    struct tcp_pcb *pcb = NULL;
    mps3_net_conn_t *c = connect_one(l, &pcb);

    CHECK(fake_tcp_deliver(pcb, fake_pbuf_make("WXYZ", 4), ERR_OK) == ERR_OK);
    fake_tcp_fire_err(pcb, ERR_RST);
    CHECK(c->errored == 1);
    CHECK(c->pcb == NULL);   /* lwIP already freed it */

    char buf[8];
    CHECK(mps3_net_recv(c, buf, 2) == 2);
    CHECK(mps3_net_recv(c, buf, 2) == 2);
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == MPS3_NET_ERR);

    /* The auto-ack must NOT be attempted on a freed pcb: conn->pcb is NULL and
     * tcp_recved() would be a use-after-free on target. */
    CHECK(fake_tcp_recved_calls() == 0);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
    mps3_net_close(c);
}

/* mps3_net_peer_closed() (net_if.h, reap-before-refuse) over the lwIP flags:
 * "gone" exactly when mps3_net_recv() would now say CLOSED/ERR -- never while
 * the chain still holds bytes (FIN or RST behind them), and asking consumes
 * nothing and acks nothing. */
static void test_peer_closed_probe(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6900);
    struct tcp_pcb *pcb = NULL;
    mps3_net_conn_t *c = connect_one(l, &pcb);
    char buf[8];

    CHECK(mps3_net_peer_closed(c) == 0);                  /* alive, silent */
    CHECK(fake_tcp_deliver(pcb, fake_pbuf_make("AB", 2), ERR_OK) == ERR_OK);
    CHECK(mps3_net_peer_closed(c) == 0);                  /* alive, bytes unread */
    CHECK(fake_tcp_deliver_fin(pcb) == ERR_OK);
    CHECK(mps3_net_peer_closed(c) == 0);                  /* FIN behind bytes */
    CHECK(c->rx != NULL && c->rx->tot_len == 2);          /* nothing consumed... */
    CHECK(fake_tcp_recved_calls() == 0);                  /* ...nothing acked */
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 2);
    CHECK(mps3_net_peer_closed(c) == 1);                  /* drained + FIN: gone */
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == MPS3_NET_CLOSED); /* recv unchanged */
    mps3_net_close(c);
    CHECK(mps3_net_peer_closed(c) == 1);                  /* released */
    CHECK(mps3_net_peer_closed(NULL) == 1);

    /* RST / keepalive abort: the same rule, via conn_err_cb. */
    c = connect_one(l, &pcb);
    CHECK(fake_tcp_deliver(pcb, fake_pbuf_make("Z", 1), ERR_OK) == ERR_OK);
    fake_tcp_fire_err(pcb, ERR_RST);
    CHECK(mps3_net_peer_closed(c) == 0);                  /* a byte still unread */
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 1);
    CHECK(mps3_net_peer_closed(c) == 1);
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == MPS3_NET_ERR);
    mps3_net_close(c);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
}

static void test_rx_callback_error_frees_without_queueing(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    mps3_net_conn_t *c = connect_one(l, NULL);

    /* lwIP hands a non-OK err with a pbuf: the pbuf is still OURS to free and
     * must not enter the rx chain. */
    CHECK(fake_tcp_deliver(c->pcb, fake_pbuf_make("junk", 4), ERR_MEM) == ERR_OK);
    CHECK(c->rx == NULL);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
    mps3_net_close(c);
}

static void test_conn_release_frees_undrained_chain(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    mps3_net_conn_t *c = connect_one(l, NULL);
    CHECK(fake_tcp_deliver(c->pcb, chain3("ABCD", "EFG", "HIJKL"), ERR_OK) == ERR_OK);
    CHECK(fake_pbuf_live() == 3);

    mps3_net_close(c);           /* closing with bytes still queued */
    CHECK(fake_tcp_closes() == 1);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
    CHECK(c->in_use == 0);
    CHECK(c->rx == NULL);

    /* tcp_close() failing for want of memory must fall back to tcp_abort() —
     * otherwise the pcb leaks on target with no way to reclaim it. */
    mps3_net_conn_t *c2 = connect_one(l, NULL);
    fake_tcp_set_close_err(ERR_MEM);
    int aborts = fake_tcp_aborts();
    mps3_net_close(c2);
    CHECK(fake_tcp_aborts() == aborts + 1);
    CHECK(c2->in_use == 0);
    fake_tcp_set_close_err(ERR_OK);
}

/* ==========================================================================
 * 2. window logic (the 6910 window-as-grant path)
 * ========================================================================== */

static void test_window_auto_acks_exactly_what_was_consumed(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    struct tcp_pcb *pcb = NULL;
    mps3_net_conn_t *c = connect_one(l, &pcb);

    CHECK(fake_tcp_deliver(pcb, fake_pbuf_make("0123456789", 10), ERR_OK) == ERR_OK);
    CHECK(fake_tcp_recved_calls() == 0); /* delivery alone opens nothing */

    char buf[32];
    struct tcp_pcb *got = NULL;
    uint16_t len = 0;

    CHECK(mps3_net_recv(c, buf, 6) == 6);
    CHECK(fake_tcp_recved_calls() == 1);
    CHECK(fake_tcp_recved_at(0, &got, &len) == 1);
    CHECK(got == pcb);
    CHECK(len == 6);            /* CONSUMED count, not the delivered count */

    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 4);
    CHECK(fake_tcp_recved_calls() == 2);
    CHECK(fake_tcp_recved_at(1, NULL, &len) == 1);
    CHECK(len == 4);
    CHECK(fake_tcp_recved_total() == 10);

    /* A read that consumes nothing must not ack anything. */
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 0);
    CHECK(fake_tcp_recved_calls() == 2);
    mps3_net_close(c);
}

static void test_window_manual_withholds_the_ack(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    struct tcp_pcb *pcb = NULL;
    mps3_net_conn_t *c = connect_one(l, &pcb);

    /* THE INCIDENT. Under window-as-grant the consumer must NOT reopen the
     * window at consumption time — the whole point is that the peer is paced
     * by explicit grants. An auto-ack leaking through here turns the grant
     * into a fire-hose and the pacing silently stops working. */
    mps3_net_set_manual_window(c, 1);
    CHECK(c->manual_window == 1);
    CHECK(fake_tcp_deliver(pcb, fake_pbuf_make("0123456789", 10), ERR_OK) == ERR_OK);

    char buf[32];
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 10);
    CHECK(fake_tcp_recved_calls() == 0);   /* withheld ENTIRELY */
    CHECK(fake_tcp_recved_total() == 0);

    /* ... and the caller reopens it explicitly, once its sink has drained. */
    mps3_net_recved(c, 10);
    CHECK(fake_tcp_recved_calls() == 1);
    CHECK(fake_tcp_recved_total() == 10);

    /* Switching back restores the default consumption-time ack, so the mode is
     * per-connection state and not a one-way latch. */
    mps3_net_set_manual_window(c, 0);
    CHECK(fake_tcp_deliver(pcb, fake_pbuf_make("abcde", 5), ERR_OK) == ERR_OK);
    CHECK(mps3_net_recv(c, buf, sizeof(buf)) == 5);
    CHECK(fake_tcp_recved_calls() == 2);
    CHECK(fake_tcp_recved_total() == 15);
    mps3_net_close(c);
}

static void test_window_recved_splits_above_u16(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    mps3_net_conn_t *c = connect_one(l, NULL);

    /* tcp_recved() takes a u16_t. A grant larger than 0xFFFF must be SPLIT,
     * not cast: a single truncating cast of 0x20000 announces 0 bytes and the
     * peer is never released — a silent stall, not a crash. */
    mps3_net_recved(c, 0x20000u);
    CHECK(fake_tcp_recved_calls() == 3);
    CHECK(fake_tcp_recved_total() == 0x20000u);
    CHECK(fake_tcp_recved_max_chunk() <= 0xFFFFu);

    uint32_t sum = 0;
    for (int i = 0; i < fake_tcp_recved_calls(); i++) {
        uint16_t len = 0;
        CHECK(fake_tcp_recved_at(i, NULL, &len) == 1);
        CHECK(len > 0);              /* a zero-length announce is progress-free */
        sum += len;
    }
    CHECK(sum == 0x20000u);

    /* Degenerate inputs are no-ops, not one-chunk-of-zero calls. */
    mps3_net_recved(c, 0);
    mps3_net_recved(NULL, 100);
    CHECK(fake_tcp_recved_calls() == 3);
    mps3_net_close(c);
}

/* ==========================================================================
 * 3. send / backpressure (the seam's short-count contract)
 * ========================================================================== */

static void test_send_short_on_backpressure(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    struct tcp_pcb *pcb = NULL;
    mps3_net_conn_t *c = connect_one(l, &pcb);

    fake_tcp_set_sndbuf(pcb, 4);
    CHECK(mps3_net_send(c, "ABCDEFGH", 8) == 4); /* min(len, tcp_sndbuf) */
    CHECK(fake_tcp_output_calls() == 1);

    fake_tcp_set_sndbuf(pcb, 0);
    CHECK(mps3_net_send(c, "X", 1) == 0);        /* 0 = retry next poll */

    fake_tcp_set_sndbuf(pcb, 100);
    fake_tcp_set_write_err(ERR_MEM);
    CHECK(mps3_net_send(c, "X", 1) == 0);        /* ERR_MEM shares that contract */
    fake_tcp_set_write_err(ERR_VAL);
    CHECK(mps3_net_send(c, "X", 1) == MPS3_NET_ERR); /* anything else is fatal */
    fake_tcp_set_write_err(ERR_OK);
    mps3_net_close(c);
}

/* ==========================================================================
 * 4. rx_poll budget
 * ========================================================================== */

static void inject(int tag, int len, int error)
{
    uint8_t f[64];
    for (int i = 0; i < len; i++) {
        f[i] = (uint8_t)(tag + i);
    }
    CHECK(fake_lan9220_inject_rx(f, len, error) == 0);
}

static void test_rx_poll_stops_at_first_empty_read(void)
{
    fresh();
    inject(0x10, 20, 0);
    inject(0x40, 24, 0);
    inject(0x70, 28, 0);

    /* Budget 10, three frames available: the loop must stop on the first
     * zero-length read rather than spending the rest of the budget on empty
     * FIFO reads. */
    CHECK(mps3_net_lwip_rx_poll(10) == 3);
    CHECK(fake_eth_input_calls() == 3);

    uint8_t got[64];
    CHECK(fake_eth_take(got, sizeof(got)) == 20);
    CHECK(got[0] == 0x10);
    CHECK(fake_eth_take(got, sizeof(got)) == 24);
    CHECK(got[0] == 0x40);
    CHECK(fake_eth_take(got, sizeof(got)) == 28);
    CHECK(got[0] == 0x70);
    CHECK(fake_eth_take(got, sizeof(got)) == -1);

    CHECK(fake_pbuf_live() == 0);   /* ethernet_input took and freed each one */
    CHECK(fake_pbuf_faults() == 0);
}

static void test_rx_poll_never_exceeds_budget(void)
{
    fresh();
    for (int i = 0; i < 5; i++) {
        inject(0x10 * (i + 1), 16, 0);
    }
    CHECK(mps3_net_lwip_rx_poll(2) == 2);   /* budget is a hard cap */
    CHECK(fake_eth_input_calls() == 2);
    CHECK(mps3_net_lwip_rx_poll(10) == 3);  /* the rest survive for next pass */
    CHECK(fake_pbuf_live() == 0);
}

static void test_rx_poll_keeps_draining_past_a_dropped_frame(void)
{
    fresh();
    inject(0x10, 20, 0);
    inject(0xA0, 20, /*error=*/1);   /* driver consumes + drops, returns < 0 */
    inject(0x70, 20, 0);
    CHECK(mps3_net_lwip_rx_poll(10) == 2);
    CHECK(fake_eth_input_calls() == 2);

    /* A dropped frame still COSTS a budget slot — the loop `continue`s, it does
     * not retry for free. Budget 1 against [bad, good] therefore delivers
     * nothing, and the good frame is still there on the next pass. */
    fresh();
    inject(0xA0, 20, /*error=*/1);
    inject(0x70, 20, 0);
    CHECK(mps3_net_lwip_rx_poll(1) == 0);
    CHECK(mps3_net_lwip_rx_poll(1) == 1);
}

static void test_rx_poll_stops_when_pbuf_pool_is_empty(void)
{
    fresh();
    inject(0x10, 20, 0);
    inject(0x40, 20, 0);
    inject(0x70, 20, 0);

    fake_pbuf_set_pool_limit(0);
    CHECK(mps3_net_lwip_pbuf_free() == 0);  /* MEMP stats agree with the pool */
    CHECK(mps3_net_lwip_rx_poll(10) == 0);  /* break, not spin */
    CHECK(fake_eth_input_calls() == 0);

    /* The frame that was already popped off the chip when the pool ran dry is
     * GONE (the driver consumed it) — two of the three survive. That is the
     * documented "frame dropped; TCP/ARP recover" behaviour, asserted so a
     * future rewrite cannot quietly turn it into a torn FIFO instead. */
    fake_pbuf_set_pool_limit(32);
    CHECK(mps3_net_lwip_rx_poll(10) == 2);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
}

static void test_rx_poll_frees_a_frame_the_stack_refuses(void)
{
    fresh();
    fake_eth_set_result(ERR_MEM);   /* netif->input refuses ownership */
    inject(0x10, 20, 0);
    CHECK(mps3_net_lwip_rx_poll(4) == 1);
    CHECK(fake_pbuf_live() == 0);   /* rx_poll freed it — no pool leak */
    CHECK(fake_pbuf_faults() == 0);
    fake_eth_set_result(ERR_OK);
}

/* ==========================================================================
 * 5. udp ring
 * ========================================================================== */

static void udp_send_in(mps3_net_udp_t *u, const void *data, int len,
                        uint32_t ip, uint16_t port)
{
    struct pbuf *p = fake_pbuf_make(data, len);
    CHECK(p != NULL);
    fake_udp_deliver(u->pcb, p, ip, port);
}

static void test_udp_ring_wraps(void)
{
    fresh();
    mps3_net_udp_t *u = mps3_net_udp_open(69);
    CHECK(u != NULL);
    CHECK(u->port == 69);
    CHECK(mps3_net_udp_open(69) == u);   /* idempotent per port, per net_if.h */

    uint8_t buf[600];
    /* Two in, two out, then three more: q_w/q_r must wrap the 3-slot ring
     * (0,1 then 2,0,1) without aliasing a slot. */
    udp_send_in(u, "aa", 2, 1, 1001);
    udp_send_in(u, "bb", 2, 2, 1002);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), NULL) == 2);
    CHECK(memcmp(buf, "aa", 2) == 0);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), NULL) == 2);
    CHECK(memcmp(buf, "bb", 2) == 0);

    udp_send_in(u, "cc", 2, 3, 1003);
    udp_send_in(u, "dd", 2, 4, 1004);
    udp_send_in(u, "ee", 2, 5, 1005);
    mps3_net_addr_t from;
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 2);
    CHECK(memcmp(buf, "cc", 2) == 0 && from.port == 1003);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 2);
    CHECK(memcmp(buf, "dd", 2) == 0 && from.port == 1004);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 2);
    CHECK(memcmp(buf, "ee", 2) == 0 && from.port == 1005);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), NULL) == 0);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
    mps3_net_udp_close(u);
}

static void test_udp_ring_full_drops_the_newest(void)
{
    fresh();
    mps3_net_udp_t *u = mps3_net_udp_open(69);
    udp_send_in(u, "AA", 2, 1, 1001);
    udp_send_in(u, "BB", 2, 2, 1002);
    udp_send_in(u, "CC", 2, 3, 1003);
    CHECK(u->q_n == MPS3_UDP_Q);

    /* Ring full: the ARRIVING datagram is dropped, not the oldest. TFTP's own
     * retransmit recovers it; evicting the oldest would instead reorder a
     * transfer, which TFTP cannot recover from. */
    udp_send_in(u, "DD", 2, 4, 1004);
    CHECK(u->q_n == MPS3_UDP_Q);
    CHECK(fake_pbuf_live() == 0);   /* the dropped datagram's pbuf was freed */

    uint8_t buf[16];
    mps3_net_addr_t from;
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 2);
    CHECK(memcmp(buf, "AA", 2) == 0);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 2);
    CHECK(memcmp(buf, "BB", 2) == 0);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 2);
    CHECK(memcmp(buf, "CC", 2) == 0);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 0); /* "DD" never arrived */
    CHECK(fake_pbuf_faults() == 0);
    mps3_net_udp_close(u);
}

static void test_udp_oversize_truncated_to_ring_slot(void)
{
    fresh();
    mps3_net_udp_t *u = mps3_net_udp_open(69);

    uint8_t big[600];
    for (int i = 0; i < (int)sizeof(big); i++) {
        big[i] = (uint8_t)(i & 0xFF);
    }
    udp_send_in(u, big, (int)sizeof(big), 7, 1007);

    uint8_t buf[1024];
    memset(buf, 0xEE, sizeof(buf));
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), NULL) == MPS3_UDP_BUF);
    CHECK(memcmp(buf, big, MPS3_UDP_BUF) == 0);
    CHECK(buf[MPS3_UDP_BUF] == 0xEE);   /* nothing written past the slot */

    /* Caller-side cap truncates too, and the REST of that datagram is gone:
     * recvfrom hands out one whole datagram per call, never a fragment queue. */
    udp_send_in(u, "0123456789", 10, 8, 1008);
    CHECK(mps3_net_udp_recvfrom(u, buf, 4, NULL) == 4);
    CHECK(memcmp(buf, "0123", 4) == 0);
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), NULL) == 0);
    CHECK(fake_pbuf_live() == 0);
    mps3_net_udp_close(u);
}

static void test_udp_from_echoed_back_verbatim(void)
{
    fresh();
    mps3_net_udp_t *u = mps3_net_udp_open(69);
    udp_send_in(u, "REQ", 3, 0xC0A80A05u, 12345);

    uint8_t buf[16];
    mps3_net_addr_t from;
    memset(&from, 0, sizeof(from));
    CHECK(mps3_net_udp_recvfrom(u, buf, sizeof(buf), &from) == 3);
    /* net_if.h calls addr.ip "an opaque 32-bit host identifier ... firmware
     * only ever echoes it back verbatim". Assert both halves of that: what
     * comes out of recvfrom, and what sendto puts back on the wire. */
    CHECK(from.ip == 0xC0A80A05u);
    CHECK(from.port == 12345);

    CHECK(mps3_net_udp_sendto(u, "ACK", 3, &from) == 3);
    CHECK(fake_udp_sendto_calls() == 1);
    CHECK(fake_udp_last_dst_ip() == 0xC0A80A05u);
    CHECK(fake_udp_last_dst_port() == 12345);
    CHECK(fake_udp_last_len() == 3);

    /* A NULL pbuf (lwIP's "socket closing" delivery) must not touch the ring. */
    fake_udp_deliver(u->pcb, NULL, 1, 1);
    CHECK(u->q_n == 0);
    CHECK(fake_pbuf_live() == 0);
    CHECK(fake_pbuf_faults() == 0);
    mps3_net_udp_close(u);
}

static void test_udp_open_close_and_send_errors(void)
{
    fresh();

    /* port 0 = backend-assigned TID (RFC1350). The socket must report the REAL
     * bound port, not the 0 it was opened with — TFTP puts it in the reply. */
    mps3_net_udp_t *tid = mps3_net_udp_open(0);
    CHECK(tid != NULL);
    CHECK(tid->port != 0);
    CHECK(tid->port == tid->pcb->local_port);

    mps3_net_addr_t to = { 0x0A000001u, 4242 };
    CHECK(mps3_net_udp_sendto(tid, "hi", 2, &to) == 2);
    CHECK(fake_pbuf_live() == 0);   /* sendto frees the pbuf it allocated */

    /* A refusing stack is MPS3_NET_ERR, not a silent success ... */
    fake_udp_set_sendto_err(ERR_VAL);
    CHECK(mps3_net_udp_sendto(tid, "hi", 2, &to) == MPS3_NET_ERR);
    CHECK(fake_pbuf_live() == 0);   /* ... and still no pbuf left behind */
    fake_udp_set_sendto_err(ERR_OK);

    CHECK(mps3_net_udp_sendto(tid, "hi", 2, NULL) == MPS3_NET_ERR);

    mps3_net_udp_close(tid);
    CHECK(tid->in_use == 0);
    CHECK(mps3_net_udp_recvfrom(tid, NULL, 0, NULL) == MPS3_NET_ERR);
    CHECK(mps3_net_udp_sendto(tid, "hi", 2, &to) == MPS3_NET_ERR);
    mps3_net_udp_close(NULL);
    CHECK(fake_pbuf_faults() == 0);
}

/* ==========================================================================
 * 6. accept ring
 * ========================================================================== */

static void test_accept_is_fifo(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6900);
    CHECK(l != NULL);
    CHECK(mps3_net_listen(6900) == l);   /* idempotent per port */

    struct tcp_pcb *p[3];
    for (int i = 0; i < 3; i++) {
        err_t rc = ERR_VAL;
        p[i] = fake_tcp_incoming(l->pcb, &rc);
        CHECK(rc == ERR_OK);
    }
    CHECK(l->pend_n == 3);
    for (int i = 0; i < 3; i++) {
        mps3_net_conn_t *c = mps3_net_accept(l);
        CHECK(c != NULL);
        CHECK(c->pcb == p[i]);   /* oldest first */
    }
    CHECK(mps3_net_accept(l) == NULL);
    CHECK(l->pend_n == 0);
}

static void test_accept_arms_the_connection(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6900);
    struct tcp_pcb *pcb = NULL;
    mps3_net_conn_t *c = connect_one(l, &pcb);

    /* Every accepted pcb is armed for the dead-peer reap before the owning
     * module ever sees it (docs: a peer that vanishes without a FIN otherwise
     * wedges a single-client service until a bitstream reload). */
    CHECK((pcb->so_options & SOF_KEEPALIVE) != 0);
    CHECK(pcb->keep_idle  == MPS3_NET_KEEPALIVE_IDLE_MS);
    CHECK(pcb->keep_intvl == MPS3_NET_KEEPALIVE_INTVL_MS);
    CHECK(pcb->keep_cnt   == MPS3_NET_KEEPALIVE_CNT);
    CHECK(pcb->fake_nagle_off == 1);  /* request/response: latency > coalescing */
    CHECK(pcb->callback_arg == c);    /* callbacks land on THIS conn */
    mps3_net_close(c);
}

static void test_accept_ring_full_refuses(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6900);
    for (int i = 0; i < MPS3_LWIP_ACCEPT_Q; i++) {
        err_t rc = ERR_VAL;
        (void)fake_tcp_incoming(l->pcb, &rc);
        CHECK(rc == ERR_OK);
    }
    CHECK(l->pend_n == MPS3_LWIP_ACCEPT_Q);
    CHECK(fake_tcp_aborts() == 0);

    err_t rc = ERR_OK;
    struct tcp_pcb *extra = fake_tcp_incoming(l->pcb, &rc);
    CHECK(rc == ERR_ABRT);                    /* refused at callback time ... */
    CHECK(fake_tcp_was_aborted(extra) == 1);  /* ... by aborting the pcb */
    CHECK(fake_tcp_aborts() == 1);
    CHECK(l->pend_n == MPS3_LWIP_ACCEPT_Q);   /* ring untouched */
}

static void test_accept_conn_table_exhaustion_refuses(void)
{
    fresh();
    mps3_net_listener_t *a = mps3_net_listen(6900);
    mps3_net_listener_t *b = mps3_net_listen(6901);

    /* Fill the CONN table (8) through two listeners, adopting each connection
     * so neither pending ring is full. The next incoming can then only be
     * refused for the other reason — no free conn slot. */
    for (int i = 0; i < MPS3_LWIP_ACCEPT_Q; i++) {
        err_t rc = ERR_VAL;
        (void)fake_tcp_incoming(a->pcb, &rc);
        CHECK(rc == ERR_OK);
        CHECK(mps3_net_accept(a) != NULL);
    }
    for (int i = 0; i < MPS3_LWIP_ACCEPT_Q; i++) {
        err_t rc = ERR_VAL;
        (void)fake_tcp_incoming(b->pcb, &rc);
        CHECK(rc == ERR_OK);
        CHECK(mps3_net_accept(b) != NULL);
    }
    CHECK(a->pend_n == 0);
    CHECK(b->pend_n == 0);
    CHECK(fake_tcp_aborts() == 0);

    err_t rc = ERR_OK;
    struct tcp_pcb *extra = fake_tcp_incoming(a->pcb, &rc);
    CHECK(rc == ERR_ABRT);
    CHECK(fake_tcp_was_aborted(extra) == 1);
    CHECK(a->pend_n == 0);   /* not the ring-full branch: the ring is EMPTY */

    /* Freeing one conn makes room again — the refusal is capacity, not a latch. */
    mps3_net_close(&s_conns[0]);
    rc = ERR_VAL;
    (void)fake_tcp_incoming(a->pcb, &rc);
    CHECK(rc == ERR_OK);
    CHECK(a->pend_n == 1);
}

static void test_accept_callback_rejects_bad_arguments(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6900);
    /* lwIP may report an accept error with no pcb; the callback must not
     * consume a conn slot for it. */
    CHECK(listener_accept_cb(l, NULL, ERR_OK) == ERR_VAL);
    CHECK(l->pend_n == 0);
}

/* ==========================================================================
 * 7. flush + the manual NO_SYS timers
 * ========================================================================== */

static void test_flush_and_timers(void)
{
    fresh();
    mps3_net_listener_t *l = mps3_net_listen(6910);
    mps3_net_conn_t *c = connect_one(l, NULL);

    int out = fake_tcp_output_calls();
    mps3_net_flush(c);
    CHECK(fake_tcp_output_calls() == out + 1); /* re-drives a deferred ACK */
    mps3_net_flush(NULL);                      /* safe on NULL, per the seam  */
    CHECK(fake_tcp_output_calls() == out + 1);

    /* NO_SYS_NO_TIMERS: tcp_tmr() every TCP_TMR_INTERVAL and etharp_tmr()
     * every ARP_TMR_INTERVAL, off mps3_sys_now_ms() deltas and nothing else.
     * The fake clock starts at 0 here, and this is the only case that drives
     * mps3_net_lwip_tmr() -- its last_tcp/last_arp are function statics with
     * no reset, so a second case would inherit them. */
    mps3_net_lwip_tmr();
    CHECK(fake_tcp_tmr_calls() == 0);          /* nothing is due at t=0 */
    CHECK(fake_etharp_tmr_calls() == 0);

    mock_time_advance_ms(TCP_TMR_INTERVAL);
    mps3_net_lwip_tmr();
    CHECK(fake_tcp_tmr_calls() == 1);
    CHECK(fake_etharp_tmr_calls() == 0);       /* ARP runs 4x slower */

    mock_time_advance_ms(ARP_TMR_INTERVAL - TCP_TMR_INTERVAL);
    mps3_net_lwip_tmr();
    CHECK(fake_tcp_tmr_calls() == 2);
    CHECK(fake_etharp_tmr_calls() == 1);
    mps3_net_close(c);
}

int main(void)
{
    /* Unbuffered: an assert() aborts the process, and a buffered stdout would
     * take the progress lines -- and, in the stuck-TX case below, the measured
     * number the failure is ABOUT -- down with it. */
    setvbuf(stdout, NULL, _IONBF, 0);

    test_rx_chain_consumed_across_segments();
    test_rx_partial_reads_leave_remainder_drainable();
    test_rx_second_delivery_appends_to_the_chain();
    test_rx_fin_mid_buffer_drains_before_closed();
    test_rx_error_reported_only_after_drain();
    test_peer_closed_probe();
    test_rx_callback_error_frees_without_queueing();
    test_conn_release_frees_undrained_chain();
    printf("test_net_if_lwip: rx ring OK\n");

    test_window_auto_acks_exactly_what_was_consumed();
    test_window_manual_withholds_the_ack();
    test_window_recved_splits_above_u16();
    test_send_short_on_backpressure();
    printf("test_net_if_lwip: window + send OK\n");

    test_rx_poll_stops_at_first_empty_read();
    test_rx_poll_never_exceeds_budget();
    test_rx_poll_keeps_draining_past_a_dropped_frame();
    test_rx_poll_stops_when_pbuf_pool_is_empty();
    test_rx_poll_frees_a_frame_the_stack_refuses();
    printf("test_net_if_lwip: rx_poll budget OK\n");

    test_udp_ring_wraps();
    test_udp_ring_full_drops_the_newest();
    test_udp_oversize_truncated_to_ring_slot();
    test_udp_from_echoed_back_verbatim();
    test_udp_open_close_and_send_errors();
    printf("test_net_if_lwip: udp ring OK\n");

    test_accept_is_fifo();
    test_accept_arms_the_connection();
    test_accept_ring_full_refuses();
    test_accept_conn_table_exhaustion_refuses();
    test_accept_callback_rejects_bad_arguments();
    printf("test_net_if_lwip: accept ring OK\n");

    test_flush_and_timers();
    printf("test_net_if_lwip: flush + timers OK\n");

    printf("test_net_if_lwip: %d checks passed\n", s_checks);
    return 0;
}
