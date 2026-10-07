/*
 * fake_net_if.c — see fake_net_if.h. Defines BOTH halves: the seam
 * functions common/net_if.h declares (what the firmware modules call) and
 * the client-side driver API (what tests call). Pure memory, no sockets.
 */
#include <string.h>
#include "../common/net_if.h"
#include "fake_net_if.h"

#define FAKE_NET_MAX_LISTENERS 8
#define FAKE_NET_MAX_CONNS     16
#define FAKE_NET_MAX_UDP       8
#define FAKE_NET_RING_BYTES    (32 * 1024)
#define FAKE_NET_MAX_DGRAMS    64
#define FAKE_NET_DGRAM_BYTES   600 /* > TFTP's 516 max */
#define FAKE_NET_EPHEMERAL_BASE 40000u
#define FAKE_NET_CLIENT_IP      0x0A00002Au /* arbitrary, echoed verbatim */

/* ---- byte ring ------------------------------------------------------------ */
typedef struct {
    uint8_t  buf[FAKE_NET_RING_BYTES];
    uint32_t rd, wr; /* wr-rd = occupancy; never wraps mod 2^32 issues at test scale */
} ring_t;

static uint32_t ring_used(const ring_t *r) { return r->wr - r->rd; }
static uint32_t ring_free(const ring_t *r) { return FAKE_NET_RING_BYTES - ring_used(r); }

static int ring_write(ring_t *r, const uint8_t *p, uint32_t n)
{
    uint32_t can = ring_free(r);
    if (n > can) n = can;
    for (uint32_t i = 0; i < n; i++) {
        r->buf[(r->wr + i) % FAKE_NET_RING_BYTES] = p[i];
    }
    r->wr += n;
    return (int)n;
}

static int ring_read(ring_t *r, uint8_t *p, uint32_t n)
{
    uint32_t can = ring_used(r);
    if (n > can) n = can;
    for (uint32_t i = 0; i < n; i++) {
        p[i] = r->buf[(r->rd + i) % FAKE_NET_RING_BYTES];
    }
    r->rd += n;
    return (int)n;
}

/* ---- tables ---------------------------------------------------------------- */
struct mps3_net_listener {
    int      used;
    uint16_t port;
};

struct mps3_net_conn {
    int    used;
    int    accepted;    /* firmware has accepted (until then recv/send by fw is invalid) */
    uint16_t port;      /* listener port this arrived on */
    ring_t c2f;         /* client -> firmware */
    ring_t f2c;         /* firmware -> client */
    int    client_closed;
    int    peer_reset;  /* abortive close: models a RST / keepalive-reaped dead peer.
                         * Distinct from client_closed (graceful FIN): the firmware's
                         * next drained recv sees MPS3_NET_ERR, not MPS3_NET_CLOSED —
                         * the exact surface the lwIP backend's conn_err_cb produces
                         * when tcp_slowtmr keepalive aborts the pcb. */
    int    fw_closed;
    int    send_limit;  /* -1 unlimited; else cap per mps3_net_send call */
    int      manual_window;/* window-as-grant: firmware withholds auto-recved */
    uint32_t recved_total; /* cumulative bytes passed to mps3_net_recved()    */
    int      recved_calls; /* number of mps3_net_recved() calls (window reopens) */
};

struct mps3_net_udp {
    int      used;
    uint16_t port;
};

typedef struct {
    int      used;
    uint32_t seq;       /* FIFO order (slots are reused; index order isn't arrival order) */
    uint16_t dst_port;  /* firmware socket it is queued for */
    uint16_t src_port;  /* client TID */
    uint32_t src_ip;
    int      len;
    uint8_t  data[FAKE_NET_DGRAM_BYTES];
} dgram_t;

typedef struct {
    int      used;
    uint32_t seq;
    uint16_t from_port; /* firmware port it left from */
    uint16_t to_port;
    int      len;
    uint8_t  data[FAKE_NET_DGRAM_BYTES];
} sent_dgram_t;

static struct mps3_net_listener s_listeners[FAKE_NET_MAX_LISTENERS];
static struct mps3_net_conn     s_conns[FAKE_NET_MAX_CONNS];
static struct mps3_net_udp      s_udp[FAKE_NET_MAX_UDP];
static dgram_t      s_rx_dgrams[FAKE_NET_MAX_DGRAMS];  /* client -> firmware (FIFO by scan order) */
static sent_dgram_t s_tx_dgrams[FAKE_NET_MAX_DGRAMS];  /* firmware -> client */
static uint16_t s_next_ephemeral;
static uint32_t s_next_seq;

void fake_net_reset(void)
{
    memset(s_listeners, 0, sizeof(s_listeners));
    memset(s_conns, 0, sizeof(s_conns));
    memset(s_udp, 0, sizeof(s_udp));
    memset(s_rx_dgrams, 0, sizeof(s_rx_dgrams));
    memset(s_tx_dgrams, 0, sizeof(s_tx_dgrams));
    s_next_ephemeral = 0;
    s_next_seq = 0;
}

/* ---- seam implementation: TCP -------------------------------------------- */

mps3_net_listener_t *mps3_net_listen(uint16_t port)
{
    for (int i = 0; i < FAKE_NET_MAX_LISTENERS; i++) {
        if (s_listeners[i].used && s_listeners[i].port == port) {
            return &s_listeners[i]; /* idempotent re-listen (net_if.h contract) */
        }
    }
    for (int i = 0; i < FAKE_NET_MAX_LISTENERS; i++) {
        if (!s_listeners[i].used) {
            s_listeners[i].used = 1;
            s_listeners[i].port = port;
            return &s_listeners[i];
        }
    }
    return (mps3_net_listener_t *)0;
}

mps3_net_conn_t *mps3_net_accept(mps3_net_listener_t *lst)
{
    if (!lst || !lst->used) {
        return (mps3_net_conn_t *)0;
    }
    for (int i = 0; i < FAKE_NET_MAX_CONNS; i++) {
        if (s_conns[i].used && !s_conns[i].accepted && s_conns[i].port == lst->port) {
            s_conns[i].accepted = 1;
            return &s_conns[i];
        }
    }
    return (mps3_net_conn_t *)0;
}

int mps3_net_recv(mps3_net_conn_t *conn, void *buf, uint32_t cap)
{
    if (!conn || !conn->used || !conn->accepted) {
        return MPS3_NET_ERR;
    }
    int n = ring_read(&conn->c2f, (uint8_t *)buf, cap);
    if (n == 0 && ring_used(&conn->c2f) == 0) {
        /* errored beats closed (mirrors the lwIP backend's mps3_net_recv order:
         * conn->errored is checked before conn->peer_closed): a reaped/RST peer
         * surfaces as MPS3_NET_ERR once its buffered bytes have drained. */
        if (conn->peer_reset) {
            return MPS3_NET_ERR;    /* abortive close (RST / keepalive reap) */
        }
        if (conn->client_closed) {
            return MPS3_NET_CLOSED; /* graceful FIN AND every byte drained */
        }
    }
    return n;
}

int mps3_net_send(mps3_net_conn_t *conn, const void *buf, uint32_t len)
{
    if (!conn || !conn->used || !conn->accepted) {
        return MPS3_NET_ERR;
    }
    if (conn->client_closed || conn->peer_reset) {
        /* Peer gone (graceful FIN or abortive reset): model a broken pipe —
         * the lwIP backend likewise fails a send once the pcb is gone. */
        return MPS3_NET_ERR;
    }
    uint32_t cap = len;
    if (conn->send_limit >= 0 && (uint32_t)conn->send_limit < cap) {
        cap = (uint32_t)conn->send_limit;
    }
    return ring_write(&conn->f2c, (const uint8_t *)buf, cap);
}

void mps3_net_flush(mps3_net_conn_t *conn)
{
    (void)conn; /* the fake egresses immediately in mps3_net_send — nothing queued */
}

void mps3_net_set_manual_window(mps3_net_conn_t *conn, int manual)
{
    if (conn && conn->used) {
        conn->manual_window = manual ? 1 : 0;
    }
}

void mps3_net_recved(mps3_net_conn_t *conn, uint32_t nbytes)
{
    /* No TCP window to model here — record the reopen so a test can prove the
     * firmware paced by exactly one window per drained sink chunk (window-as-grant).
     * fake_net_recved_total()/fake_net_recved_calls() surface it. */
    if (conn && conn->used && nbytes) {
        conn->recved_total += nbytes;
        conn->recved_calls++;
    }
}

void mps3_net_close(mps3_net_conn_t *conn)
{
    if (!conn || !conn->used) {
        return;
    }
    conn->fw_closed = 1;
    /* The slot stays allocated until the CLIENT side also closes, so the
     * test can still drain f2c and observe fw_closed. */
    if (conn->client_closed) {
        conn->used = 0;
    }
}

/* The liveness probe (net_if.h): the executable spec. Dead = the client closed
 * (gracefully or abortively) AND every byte it sent has been consumed -- i.e.
 * exactly when mps3_net_recv() would now return CLOSED/ERR. Pure read.
 *
 * FAKE_NET_TEST_NO_PEER_PROBE (test-only NEGATIVE CONTROL, never in a real
 * build): compile this definition out, so net_if.c's WEAK "presumed alive"
 * default links instead -- the pre-2026-09-28 refuse-while-occupied behaviour.
 * test_ctrl_reap_noprobe asserts the refusals that come back, which is what
 * gives the positive binary's "never refused" assertions their teeth. */
#if !defined(FAKE_NET_TEST_NO_PEER_PROBE)
int mps3_net_peer_closed(mps3_net_conn_t *conn)
{
    if (!conn || !conn->used || !conn->accepted || conn->fw_closed) {
        return 1;
    }
    if (ring_used(&conn->c2f) != 0) {
        return 0; /* unread request bytes: not reapable yet */
    }
    return (conn->client_closed || conn->peer_reset) ? 1 : 0;
}
#endif

/* ---- seam implementation: UDP --------------------------------------------- */

mps3_net_udp_t *mps3_net_udp_open(uint16_t port)
{
    if (port != 0) {
        for (int i = 0; i < FAKE_NET_MAX_UDP; i++) {
            if (s_udp[i].used && s_udp[i].port == port) {
                return &s_udp[i]; /* idempotent re-open */
            }
        }
    }
    for (int i = 0; i < FAKE_NET_MAX_UDP; i++) {
        if (!s_udp[i].used) {
            s_udp[i].used = 1;
            s_udp[i].port = port ? port
                                 : (uint16_t)(FAKE_NET_EPHEMERAL_BASE + (s_next_ephemeral++));
            return &s_udp[i];
        }
    }
    return (mps3_net_udp_t *)0;
}

int mps3_net_udp_recvfrom(mps3_net_udp_t *udp, void *buf, uint32_t cap,
                          mps3_net_addr_t *from)
{
    if (!udp || !udp->used) {
        return MPS3_NET_ERR;
    }
    int oldest = -1;
    for (int i = 0; i < FAKE_NET_MAX_DGRAMS; i++) {
        if (s_rx_dgrams[i].used && s_rx_dgrams[i].dst_port == udp->port &&
            (oldest < 0 || s_rx_dgrams[i].seq < s_rx_dgrams[oldest].seq)) {
            oldest = i;
        }
    }
    if (oldest >= 0) {
        int n = s_rx_dgrams[oldest].len;
        if ((uint32_t)n > cap) n = (int)cap;
        memcpy(buf, s_rx_dgrams[oldest].data, (size_t)n);
        if (from) {
            from->ip = s_rx_dgrams[oldest].src_ip;
            from->port = s_rx_dgrams[oldest].src_port;
        }
        s_rx_dgrams[oldest].used = 0;
        return n;
    }
    return 0;
}

int mps3_net_udp_sendto(mps3_net_udp_t *udp, const void *buf, uint32_t len,
                        const mps3_net_addr_t *to)
{
    if (!udp || !udp->used || !to || len > FAKE_NET_DGRAM_BYTES) {
        return MPS3_NET_ERR;
    }
    for (int i = 0; i < FAKE_NET_MAX_DGRAMS; i++) {
        if (!s_tx_dgrams[i].used) {
            s_tx_dgrams[i].used = 1;
            s_tx_dgrams[i].seq = s_next_seq++;
            s_tx_dgrams[i].from_port = udp->port;
            s_tx_dgrams[i].to_port = to->port;
            s_tx_dgrams[i].len = (int)len;
            memcpy(s_tx_dgrams[i].data, buf, (size_t)len);
            return (int)len;
        }
    }
    return MPS3_NET_ERR;
}

void mps3_net_udp_close(mps3_net_udp_t *udp)
{
    if (udp && udp->used) {
        udp->used = 0;
        /* Drop anything still queued for this port. */
        for (int i = 0; i < FAKE_NET_MAX_DGRAMS; i++) {
            if (s_rx_dgrams[i].used && s_rx_dgrams[i].dst_port == udp->port) {
                s_rx_dgrams[i].used = 0;
            }
        }
    }
}

/* ---- client-side driver API ------------------------------------------------ */

int fake_net_connect(uint16_t port)
{
    int listening = 0;
    for (int i = 0; i < FAKE_NET_MAX_LISTENERS; i++) {
        if (s_listeners[i].used && s_listeners[i].port == port) {
            listening = 1;
            break;
        }
    }
    if (!listening) {
        return -1;
    }
    for (int i = 0; i < FAKE_NET_MAX_CONNS; i++) {
        if (!s_conns[i].used) {
            memset(&s_conns[i], 0, sizeof(s_conns[i]));
            s_conns[i].used = 1;
            s_conns[i].port = port;
            s_conns[i].send_limit = -1;
            return i;
        }
    }
    return -1;
}

static struct mps3_net_conn *client_conn(int client)
{
    if (client < 0 || client >= FAKE_NET_MAX_CONNS || !s_conns[client].used) {
        return 0;
    }
    return &s_conns[client];
}

int fake_net_send(int client, const void *buf, int len)
{
    struct mps3_net_conn *c = client_conn(client);
    if (!c || c->client_closed) {
        return -1;
    }
    return ring_write(&c->c2f, (const uint8_t *)buf, (uint32_t)len);
}

int fake_net_recv(int client, void *buf, int cap)
{
    struct mps3_net_conn *c = client_conn(client);
    if (!c) {
        return -1;
    }
    return ring_read(&c->f2c, (uint8_t *)buf, (uint32_t)cap);
}

int fake_net_fw_closed(int client)
{
    struct mps3_net_conn *c = client_conn(client);
    return c ? c->fw_closed : 1;
}

void fake_net_close(int client)
{
    struct mps3_net_conn *c = client_conn(client);
    if (c) {
        c->client_closed = 1;
        if (c->fw_closed) {
            c->used = 0;
        }
    }
}

void fake_net_kill_peer(int client)
{
    /* Model a peer that vanished WITHOUT a FIN (killed process / yanked cable)
     * and is subsequently reaped by lwIP TCP keepalive: an ABORTIVE close. The
     * firmware's next drained recv/send on this handle sees MPS3_NET_ERR — the
     * same surface net_if_lwip.c's conn_err_cb delivers when tcp_slowtmr aborts
     * the pcb after keep_cnt unanswered probes. Unlike fake_net_close() this does
     * NOT set client_closed, so it is distinguishable from a graceful FIN. */
    struct mps3_net_conn *c = client_conn(client);
    if (c) {
        c->peer_reset = 1;
    }
}

void fake_net_set_send_limit(int client, int limit)
{
    struct mps3_net_conn *c = client_conn(client);
    if (c) {
        c->send_limit = limit;
    }
}

/* Index-based (NOT client_conn): the window-reopen counters stay readable AFTER
 * the connection closes — the slot is freed (used=0) once both ends close, but
 * its counters persist until the next fake_net_reset()/reuse, so a test can read
 * the final pacing tally after the shell has closed the transfer. */
uint32_t fake_net_recved_total(int client)
{
    if (client < 0 || client >= FAKE_NET_MAX_CONNS) {
        return 0;
    }
    return s_conns[client].recved_total;
}

int fake_net_recved_calls(int client)
{
    if (client < 0 || client >= FAKE_NET_MAX_CONNS) {
        return 0;
    }
    return s_conns[client].recved_calls;
}

int fake_net_pending_accepts(uint16_t port)
{
    int n = 0;
    for (int i = 0; i < FAKE_NET_MAX_CONNS; i++) {
        if (s_conns[i].used && !s_conns[i].accepted && s_conns[i].port == port) {
            n++;
        }
    }
    return n;
}

int fake_net_udp_inject(uint16_t dst_port, uint16_t src_port,
                        const void *buf, int len)
{
    if (len > FAKE_NET_DGRAM_BYTES) {
        return -1;
    }
    int bound = 0;
    for (int i = 0; i < FAKE_NET_MAX_UDP; i++) {
        if (s_udp[i].used && s_udp[i].port == dst_port) {
            bound = 1;
            break;
        }
    }
    if (!bound) {
        return -1;
    }
    for (int i = 0; i < FAKE_NET_MAX_DGRAMS; i++) {
        if (!s_rx_dgrams[i].used) {
            s_rx_dgrams[i].used = 1;
            s_rx_dgrams[i].seq = s_next_seq++;
            s_rx_dgrams[i].dst_port = dst_port;
            s_rx_dgrams[i].src_port = src_port;
            s_rx_dgrams[i].src_ip = FAKE_NET_CLIENT_IP;
            s_rx_dgrams[i].len = len;
            memcpy(s_rx_dgrams[i].data, buf, (size_t)len);
            return 0;
        }
    }
    return -1;
}

int fake_net_udp_take_sent(uint16_t *from_port, uint16_t *to_port,
                           void *buf, int cap)
{
    int oldest = -1;
    for (int i = 0; i < FAKE_NET_MAX_DGRAMS; i++) {
        if (s_tx_dgrams[i].used &&
            (oldest < 0 || s_tx_dgrams[i].seq < s_tx_dgrams[oldest].seq)) {
            oldest = i;
        }
    }
    if (oldest < 0) {
        return -1;
    }
    int n = s_tx_dgrams[oldest].len;
    if (n > cap) n = cap;
    memcpy(buf, s_tx_dgrams[oldest].data, (size_t)n);
    if (from_port) *from_port = s_tx_dgrams[oldest].from_port;
    if (to_port)   *to_port   = s_tx_dgrams[oldest].to_port;
    s_tx_dgrams[oldest].used = 0;
    return n;
}

int fake_net_udp_sent_count(void)
{
    int n = 0;
    for (int i = 0; i < FAKE_NET_MAX_DGRAMS; i++) {
        if (s_tx_dgrams[i].used) n++;
    }
    return n;
}
