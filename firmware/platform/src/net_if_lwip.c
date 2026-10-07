/*
 * net_if_lwip.c — THE lwIP backend of common/net_if.h (the "one thin file"
 * that seam header has anticipated since W-NET-SEAM), plus the smsc911x
 * driver <-> lwIP netif glue. TARGET-ONLY: this is the single firmware
 * translation unit that includes lwIP headers; every portable module keeps
 * calling the seam. The OTHER backend is firmware/test/fake_net_if.c — the
 * executable spec this file must match (same contract: non-blocking, handle
 * lifetimes, CLOSED-after-drain, short sends on backpressure).
 *
 * lwIP build assumptions (BSP lwip220, RAW_API, NO_SYS=1,
 * NO_SYS_NO_TIMERS=1 — see firmware/platform/README.md for the full BSP
 * table): no threads, no sockets, no sys_timeout machinery. TCP/ARP timers
 * are driven manually from mps3_net_lwip_tmr() (tcp_tmr every 250 ms,
 * etharp_tmr every ARP_TMR_INTERVAL), the classic Xilinx RAW-mode pattern.
 * DHCP/AutoIP/DNS/IGMP/IPv6/IP-reassembly are configured out, so no other
 * timer exists to service.
 *
 * Design notes (mirroring net_if.h's contract line by line):
 *  - LISTENERS: one tcp_listen pcb per port, table-idempotent (listen()
 *    twice on a port returns the same listener — module _init()s re-run
 *    harmlessly). Accepted pcbs park in a small per-listener pending ring
 *    until the module's mps3_net_accept() adopts them; ring overflow
 *    refuses the connection at accept-callback time (tcp_abort) — modules
 *    are single-client v1 anyway and close extras immediately.
 *  - CONNECTIONS: rx bytes stay in the pbuf chain lwIP delivered (no copy,
 *    no static ring): mps3_net_recv() consumes off the chain head and only
 *    then calls tcp_recved(), so the TCP window (tcp_wnd) is the inbound
 *    flow control — a slow-polling module simply stalls the sender, bounding
 *    per-connection buffering by construction. A connection switched to
 *    MANUAL-window mode (mps3_net_set_manual_window) withholds that
 *    consumption-time tcp_recved and reopens the window one window at a time
 *    via mps3_net_recved() — the window-as-grant path config_agent uses to
 *    pace a bitstream push without any application ack byte.
 *  - A handle stays valid after peer close/error until mps3_net_close():
 *    buffered bytes remain drainable, then MPS3_NET_CLOSED (graceful) /
 *    MPS3_NET_ERR (lwIP err callback — pcb already freed by the stack).
 *  - SEND: min(len, tcp_sndbuf) via tcp_write(COPY) + tcp_output — the
 *    short-count return IS the backpressure signal the seam promises;
 *    ERR_MEM maps to 0 (retry next poll), other errors to MPS3_NET_ERR.
 *  - UDP: one udp pcb per socket; inbound datagrams are copied at callback
 *    time into a small fixed ring (MPS3_UDP_Q x 516 B — TFTP's bound,
 *    net_if.h sizes callers for exactly that); ring-full drops the newest
 *    datagram (TFTP retransmits). addr.ip carries the peer ip4 u32
 *    verbatim, echoed back by sendto — opaque to firmware, per the seam.
 *
 * MAC POLICY: mps3_platform_mac() (weak) returns the fixed
 * locally-administered 02:00:00:4D:50:53 — single-board bring-up value
 * (same as the harness_app spike). Real provisioning (QSPI record or
 * static_id-derived) is an A6 decision; override = define a strong copy.
 */
#include <string.h>

#include "lwip/init.h"
#include "lwip/netif.h"
#include "lwip/ip4_addr.h"
#include "lwip/ip.h"            /* ip_set_option / SOF_KEEPALIVE (per-pcb keepalive) */
#include "lwip/tcp.h"
#include "lwip/udp.h"
#include "lwip/pbuf.h"
#include "lwip/etharp.h"
#include "lwip/stats.h"         /* lwip_stats.memp[MEMP_PBUF_POOL] — pbuf-pool diag */
#include "lwip/memp.h"          /* MEMP_PBUF_POOL */
#include "lwip/priv/tcp_priv.h" /* tcp_tmr() — RAW/NO_SYS manual-timer entry */
#include "netif/ethernet.h"     /* ethernet_input */

#include "../../common/net_if.h"
#include "../../common/net_proto.h" /* MPS3_DEFAULT_IP_* */
#include "../../common/service.h"   /* mps3_spin_until — a bound in MICROSECONDS */
#include "../../smsc911x/smsc911x.h"
#include "net_if_lwip.h"

/* ---- capacity (static tables — no allocation anywhere) -------------------- */
/* Sized tight for the 256 KiB LMB (the two ~50 KiB clearing staging buffers
 * dominate — see firmware/platform/README.md "Memory honesty"). The UDP
 * tables are the notable trim: TFTP (:69) is the only UDP service and runs
 * one transfer at a time, so 3 sockets (:69 + a couple ephemeral TIDs) with
 * a 3-deep datagram ring is ample — the old 4x4x516 was ~8 KiB of .bss. */
#define MPS3_LWIP_MAX_LISTEN   8  /* 7 contract TCP ports + 1 spare          */
#define MPS3_LWIP_MAX_CONN     8  /* single-client services + refuse-in-flight */
#define MPS3_LWIP_MAX_UDP      3  /* TFTP :69 + ephemeral TIDs               */
#define MPS3_LWIP_ACCEPT_Q     4  /* per-listener not-yet-adopted pcbs       */
#define MPS3_UDP_Q             3  /* per-socket queued datagrams             */
#define MPS3_UDP_BUF         516  /* TFTP bound (RFC1350: 512 + 4 header)    */

/* Bounded wait for LAN9220 TX-FIFO space inside linkoutput, in MICROSECONDS.
 *
 * This used to be `#define MPS3_TX_SPACE_POLL_BOUND 100000` — an ITERATION
 * count, with a comment that guessed at what it was worth ("a few ms worst
 * case"). It was not a duration: 100000 iterations of this loop body means one
 * thing at -O0 on a 100 MHz AXI clock and something else entirely at -Os, or on
 * a different clock, or after anyone touches smsc911x_tx_frame(). Nothing in
 * the tree could state the actual bound, and no host test could assert it.
 *
 * 5 ms is ~40 full frames at wire speed (a 1518-byte frame drains in ~122 us
 * @100 Mbps), so a healthy FIFO never comes close and a stuck one gives up
 * promptly. On timeout linkoutput returns ERR_MEM and lwIP RETRIES — never
 * ERR_OK, which would tell TCP the frame was sent and hide the loss until a
 * full RTO (the silent-drop failure mode). */
#define MPS3_TX_SPACE_TIMEOUT_US 5000u

/* The literal above is the ONE thing a duration-based test cannot pin: the test
 * derives its expected window FROM this constant (asserting 5000 there would be
 * a second hand-written copy of it), so moving the constant moves both. Pin the
 * RANGE here instead, against the two facts that actually constrain it:
 *   lower  10 full frames at 100 Mbps = 1220 us -- below that a merely busy
 *          FIFO would be called stuck;
 *   upper  the smallest budget in main.c's service table for a service that can
 *          reach this function (net_rx, 20000 us) -- above that a single
 *          TX-space stall would trip the superloop's budget watchdog and the
 *          sick-service logic would start chasing a fault it did not cause.
 * Negative-array form rather than _Static_assert: this file is built by whatever
 * mb-gcc Vitis ships as well as by host gcc -std=c11, and diag.h makes the same
 * allowance for the same reason. */
typedef char mps3_tx_space_timeout_in_range[
    (MPS3_TX_SPACE_TIMEOUT_US >= 1220u && MPS3_TX_SPACE_TIMEOUT_US <= 20000u)
        ? 1 : -1];

/* ---- TCP keepalive: reap a dead peer's session --------------------------------
 * Closes the last "dead client wedges the shell" hole
 * (docs/QSPI_CLEARING_CACHE_HW_FINDINGS.md). Every accepted pcb (6900 control,
 * 6910 stream, 6920 swd, 6930/6931 uart, 6932 swo, 2542 xvc — they ALL funnel
 * through listener_accept_cb below) is armed with SOF_KEEPALIVE so lwIP probes an
 * idle connection and ABORTS it if the peer has vanished without a FIN (killed
 * process / yanked cable). Without this a pcb sits ESTABLISHED forever and, since
 * config_agent serves a single 6910 session, every later client is refused until
 * a JTAG bitstream reload.
 *
 * COMPLEMENTARY TO — NOT a replacement for — the swap FSM's AWAIT-idle timeout
 * (MPS3_SWAP_AWAIT_IDLE_MS, swap_fsm.h). Those cover DIFFERENT holes and neither
 * supersedes the other:
 *   - FSM idle-timeout aborts a swap that IS ARMED but starved (a client that
 *     died mid-transfer, leaving the AWAIT_* states parked on a live-but-silent
 *     socket) — it acts on swap progress, not on the socket.
 *   - keepalive reaps a connection with NO swap armed (a client that connected
 *     and died BEFORE/BETWEEN swaps) — the FSM idle-timeout never engages there
 *     because nothing is armed. It acts on the socket, not on swap progress.
 *
 * UNITS ARE MILLISECONDS. (opt.h's doxygen says "in seconds" — that comment is
 * wrong for this lwIP; TCP_KEEPIDLE_DEFAULT/INTVL_DEFAULT in priv/tcp_priv.h are
 * 7200000/75000 and are labelled "in milliseconds", and tcp_slowtmr() divides
 * keep_idle/keep_intvl by TCP_SLOW_INTERVAL (=500 ms). Confirmed against
 * lwip220_v1_0 core/tcp.c.)
 *
 * Reap time = keep_idle + keep_cnt*keep_intvl = 15000 + 4*5000 = 35 s after the
 * last byte from the peer (probes at +15,+20,+25,+30 s; abort at +35 s). Chosen
 * so a dead peer is dropped in well under a minute — the board recovers on its
 * own — yet an idle-but-ALIVE control connection is never killed: a live peer's
 * kernel ACKs each keepalive probe automatically (no app involvement), which
 * resets the timer. Only a peer whose stack is gone stays silent through all four
 * probes. 35 s sits comfortably inside the "~30-45 s" design window. */
#define MPS3_NET_KEEPALIVE_IDLE_MS   15000u  /* idle before the first probe   */
#define MPS3_NET_KEEPALIVE_INTVL_MS   5000u  /* between probes                */
#define MPS3_NET_KEEPALIVE_CNT           4u  /* unanswered probes -> abort    */

#if !LWIP_TCP_KEEPALIVE
/* Guards the landmine in create_platform.tcl's header: a lwipopts change is only
 * honored after the BSP's liblwip*.a is REBUILT (platform generate). If this
 * translation unit sees the option OFF, the BSP was NOT regenerated with
 * `bsp config lwip_tcp_keepalive true` — the per-pcb keep_intvl/keep_cnt below
 * are compiled out of struct tcp_pcb and only the fixed ~2 h TCP_MAXIDLE applies,
 * so the reap never happens fast enough to matter. */
#warning "LWIP_TCP_KEEPALIVE is OFF in this BSP — regenerate the BSP (create_platform.tcl) so the dead-peer reap actually engages; keep_intvl/keep_cnt tuning is being ignored"
#endif

struct mps3_net_conn {
    struct tcp_pcb *pcb;        /* NULL once errored/aborted by lwIP        */
    struct pbuf    *rx;         /* un-consumed inbound chain (head)         */
    uint32_t        rx_off;     /* consumed bytes of rx's first pbuf        */
    uint8_t         in_use;
    uint8_t         peer_closed;/* graceful FIN seen                        */
    uint8_t         errored;    /* err callback fired (pcb gone)            */
    uint8_t         manual_window; /* 1 => mps3_net_recv() withholds tcp_recved
                                    * (window-as-grant); caller reopens the
                                    * window via mps3_net_recved()          */
};

struct mps3_net_listener {
    struct tcp_pcb  *pcb;
    uint16_t         port;
    uint8_t          in_use;
    mps3_net_conn_t *pending[MPS3_LWIP_ACCEPT_Q];
    uint8_t          pend_r, pend_w, pend_n;
};

typedef struct {
    uint16_t len;               /* 0 = slot empty                            */
    mps3_net_addr_t from;
    uint8_t  data[MPS3_UDP_BUF];
} mps3_udp_dgram_t;

struct mps3_net_udp {
    struct udp_pcb  *pcb;
    uint16_t         port;      /* bound port (backend-assigned if opened 0) */
    uint8_t          in_use;
    mps3_udp_dgram_t q[MPS3_UDP_Q];
    uint8_t          q_r, q_w, q_n;
};

static struct mps3_net_listener s_listeners[MPS3_LWIP_MAX_LISTEN];
static struct mps3_net_conn     s_conns[MPS3_LWIP_MAX_CONN];
static struct mps3_net_udp      s_udps[MPS3_LWIP_MAX_UDP];

/* ==========================================================================
 * TCP seam
 * ========================================================================== */

static void conn_release(mps3_net_conn_t *c)
{
    if (c->rx) {
        pbuf_free(c->rx);
        c->rx = NULL;
    }
    c->rx_off = 0;
    c->pcb = NULL;
    c->peer_closed = 0;
    c->errored = 0;
    c->manual_window = 0;
    c->in_use = 0;
}

static err_t conn_recv_cb(void *arg, struct tcp_pcb *tpcb, struct pbuf *p, err_t err)
{
    mps3_net_conn_t *c = (mps3_net_conn_t *)arg;
    (void)tpcb;

    if (p == NULL) {
        c->peer_closed = 1; /* FIN: drainable bytes stay in c->rx */
        return ERR_OK;
    }
    if (err != ERR_OK) {
        pbuf_free(p);
        return ERR_OK;
    }
    if (c->rx == NULL) {
        c->rx = p;
    } else {
        pbuf_cat(c->rx, p); /* takes ownership of p */
    }
    /* tcp_recved deliberately NOT called here — consumption-time ack is
     * the flow control (file-header design note). */
    return ERR_OK;
}

static void conn_err_cb(void *arg, err_t err)
{
    mps3_net_conn_t *c = (mps3_net_conn_t *)arg;
    (void)err;
    /* lwIP has already freed the pcb when this fires. */
    c->pcb = NULL;
    c->errored = 1;
}

static err_t listener_accept_cb(void *arg, struct tcp_pcb *newpcb, err_t err)
{
    mps3_net_listener_t *l = (mps3_net_listener_t *)arg;

    if (err != ERR_OK || newpcb == NULL) {
        return ERR_VAL;
    }
    if (l->pend_n >= MPS3_LWIP_ACCEPT_Q) {
        tcp_abort(newpcb); /* pending ring full — refuse */
        return ERR_ABRT;
    }
    mps3_net_conn_t *c = NULL;
    for (int i = 0; i < MPS3_LWIP_MAX_CONN; i++) {
        if (!s_conns[i].in_use) {
            c = &s_conns[i];
            break;
        }
    }
    if (c == NULL) {
        tcp_abort(newpcb); /* conn table exhausted — refuse */
        return ERR_ABRT;
    }

    memset(c, 0, sizeof(*c));
    c->in_use = 1;
    c->pcb = newpcb;
    tcp_arg(newpcb, c);
    tcp_recv(newpcb, conn_recv_cb);
    tcp_err(newpcb, conn_err_cb);
    tcp_nagle_disable(newpcb); /* request/response services: latency > coalescing */

    /* Arm keepalive so a peer that vanishes without a FIN is reaped (see the
     * MPS3_NET_KEEPALIVE_* block). SOF_KEEPALIVE turns the probing on; the
     * per-pcb fields tune it to a ~35 s reap. When keepalive fires, lwIP
     * tcp_abort()s the pcb, which invokes conn_err_cb (errored=1, pcb=NULL) —
     * the SAME transport-error surface a RST produces, so mps3_net_recv()
     * returns MPS3_NET_ERR on the owning module's next poll and it runs its
     * normal close/cleanup (config_agent's session_reset frees the 6910
     * session; coordinator_net's drop_client frees 6900). No separate
     * keepalive-close hook is needed — the reap rides the existing err path. */
    ip_set_option(newpcb, SOF_KEEPALIVE);
#if LWIP_TCP_KEEPALIVE
    newpcb->keep_idle  = MPS3_NET_KEEPALIVE_IDLE_MS;
    newpcb->keep_intvl = MPS3_NET_KEEPALIVE_INTVL_MS;
    newpcb->keep_cnt   = MPS3_NET_KEEPALIVE_CNT;
#endif

    l->pending[l->pend_w] = c;
    l->pend_w = (uint8_t)((l->pend_w + 1) % MPS3_LWIP_ACCEPT_Q);
    l->pend_n++;
    return ERR_OK;
}

mps3_net_listener_t *mps3_net_listen(uint16_t port)
{
    /* Idempotent per port (net_if.h contract). */
    for (int i = 0; i < MPS3_LWIP_MAX_LISTEN; i++) {
        if (s_listeners[i].in_use && s_listeners[i].port == port) {
            return &s_listeners[i];
        }
    }
    mps3_net_listener_t *l = NULL;
    for (int i = 0; i < MPS3_LWIP_MAX_LISTEN; i++) {
        if (!s_listeners[i].in_use) {
            l = &s_listeners[i];
            break;
        }
    }
    if (l == NULL) {
        return NULL;
    }

    struct tcp_pcb *pcb = tcp_new();
    if (pcb == NULL) {
        return NULL;
    }
    if (tcp_bind(pcb, IP_ADDR_ANY, port) != ERR_OK) {
        tcp_abort(pcb);
        return NULL;
    }
    struct tcp_pcb *lpcb = tcp_listen(pcb); /* frees pcb, returns listen pcb */
    if (lpcb == NULL) {
        tcp_abort(pcb);
        return NULL;
    }

    memset(l, 0, sizeof(*l));
    l->in_use = 1;
    l->port = port;
    l->pcb = lpcb;
    tcp_arg(lpcb, l);
    tcp_accept(lpcb, listener_accept_cb);
    return l;
}

mps3_net_conn_t *mps3_net_accept(mps3_net_listener_t *lst)
{
    if (lst == NULL || lst->pend_n == 0) {
        return NULL;
    }
    mps3_net_conn_t *c = lst->pending[lst->pend_r];
    lst->pend_r = (uint8_t)((lst->pend_r + 1) % MPS3_LWIP_ACCEPT_Q);
    lst->pend_n--;
    return c;
}

int mps3_net_recv(mps3_net_conn_t *conn, void *buf, uint32_t cap)
{
    if (conn == NULL || !conn->in_use) {
        return MPS3_NET_ERR;
    }

    uint32_t copied = 0;
    uint8_t *dst = (uint8_t *)buf;
    while (copied < cap && conn->rx != NULL) {
        struct pbuf *p = conn->rx;
        uint32_t avail = (uint32_t)p->len - conn->rx_off;
        uint32_t take = cap - copied;
        if (take > avail) {
            take = avail;
        }
        memcpy(dst + copied, (const uint8_t *)p->payload + conn->rx_off, take);
        copied += take;
        conn->rx_off += take;
        if (conn->rx_off == p->len) {
            /* First pbuf fully consumed: detach + free it, keep the rest. */
            struct pbuf *next = p->next;
            if (next != NULL) {
                pbuf_ref(next);
            }
            pbuf_free(p); /* frees only p now that next holds an extra ref */
            conn->rx = next;
            conn->rx_off = 0;
        }
    }

    if (copied > 0) {
        /* Auto-ack (fire-hose): reopen the window at consumption time. Under
         * manual-window mode (window-as-grant) the caller withholds this and
         * reopens the window itself, one window per drained sink chunk, via
         * mps3_net_recved(). */
        if (conn->pcb != NULL && !conn->manual_window) {
            tcp_recved(conn->pcb, (u16_t)copied); /* open the window back up */
        }
        return (int)copied;
    }
    if (conn->errored) {
        return MPS3_NET_ERR;    /* transport error, buffer already drained */
    }
    if (conn->peer_closed) {
        return MPS3_NET_CLOSED; /* FIN'd and drained */
    }
    return 0;
}

int mps3_net_send(mps3_net_conn_t *conn, const void *buf, uint32_t len)
{
    if (conn == NULL || !conn->in_use || conn->pcb == NULL || conn->errored) {
        return MPS3_NET_ERR;
    }
    uint32_t room = tcp_sndbuf(conn->pcb);
    uint32_t n = (len < room) ? len : room;
    if (n == 0) {
        return 0; /* backpressure — caller retries next poll */
    }
    err_t err = tcp_write(conn->pcb, buf, (u16_t)n, TCP_WRITE_FLAG_COPY);
    if (err == ERR_MEM) {
        return 0; /* segment/heap pressure — same retry contract */
    }
    if (err != ERR_OK) {
        return MPS3_NET_ERR;
    }
    tcp_output(conn->pcb);
    return (int)n;
}

void mps3_net_flush(mps3_net_conn_t *conn)
{
    if (conn == NULL || !conn->in_use || conn->pcb == NULL || conn->errored) {
        return;
    }
    /* Re-drive output: egress anything sitting in the pcb's unsent queue (e.g. a
     * deferred window-update ACK under window-as-grant). tcp_output is a no-op
     * when nothing is pending, so this is cheap to call every poll. */
    tcp_output(conn->pcb);
}

void mps3_net_set_manual_window(mps3_net_conn_t *conn, int manual)
{
    if (conn == NULL || !conn->in_use) {
        return;
    }
    conn->manual_window = manual ? 1 : 0;
}

void mps3_net_recved(mps3_net_conn_t *conn, uint32_t nbytes)
{
    if (conn == NULL || !conn->in_use || conn->pcb == NULL || nbytes == 0) {
        return;
    }
    /* Reopen the receive window (window-as-grant). nbytes is bounded by the app
     * window (== TCP_WND == 16384) so it always fits u16_t; guard anyway. lwIP's
     * tcp_recved() emits the window-update ACK itself once the inflation crosses
     * the update threshold — a full window always does. */
    while (nbytes > 0) {
        u16_t chunk = (nbytes > 0xFFFFu) ? 0xFFFFu : (u16_t)nbytes;
        tcp_recved(conn->pcb, chunk);
        nbytes -= chunk;
    }
}

void mps3_net_close(mps3_net_conn_t *conn)
{
    if (conn == NULL || !conn->in_use) {
        return;
    }
    if (conn->pcb != NULL) {
        struct tcp_pcb *pcb = conn->pcb;
        tcp_arg(pcb, NULL);
        tcp_recv(pcb, NULL);
        tcp_err(pcb, NULL);
        tcp_sent(pcb, NULL);
        if (tcp_close(pcb) != ERR_OK) {
            tcp_abort(pcb); /* out of memory for FIN — hard release */
        }
    }
    conn_release(conn);
}

int mps3_net_peer_closed(mps3_net_conn_t *conn)
{
    /* net_if.h's liveness probe, from the flags the callbacks already keep:
     * conn_recv_cb sets peer_closed on the FIN (p == NULL), conn_err_cb sets
     * errored on a RST / keepalive abort. Undrained rx means a request is still
     * to be served, so the connection is not reapable yet — the same "bytes
     * first, then CLOSED/ERR" order mps3_net_recv() reports in. Pure read. */
    if (conn == NULL || !conn->in_use) {
        return 1;
    }
    if (conn->rx != NULL) {
        return 0;
    }
    return (conn->errored || conn->peer_closed) ? 1 : 0;
}

/* ==========================================================================
 * UDP seam (TFTP)
 * ========================================================================== */

static void udp_recv_cb(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                        const ip_addr_t *addr, u16_t port)
{
    mps3_net_udp_t *u = (mps3_net_udp_t *)arg;
    (void)pcb;

    if (p == NULL) {
        return;
    }
    if (u->q_n >= MPS3_UDP_Q) {
        pbuf_free(p); /* ring full: drop (TFTP's own retransmit recovers) */
        return;
    }
    mps3_udp_dgram_t *d = &u->q[u->q_w];
    uint16_t len = (p->tot_len > MPS3_UDP_BUF) ? MPS3_UDP_BUF : p->tot_len;
    pbuf_copy_partial(p, d->data, len, 0);
    d->len = len;
    d->from.ip = ip4_addr_get_u32(ip_2_ip4(addr)); /* opaque u32, echoed back */
    d->from.port = port;
    u->q_w = (uint8_t)((u->q_w + 1) % MPS3_UDP_Q);
    u->q_n++;
    pbuf_free(p);
}

mps3_net_udp_t *mps3_net_udp_open(uint16_t port)
{
    if (port != 0) { /* idempotent for explicit ports (net_if.h contract) */
        for (int i = 0; i < MPS3_LWIP_MAX_UDP; i++) {
            if (s_udps[i].in_use && s_udps[i].port == port) {
                return &s_udps[i];
            }
        }
    }
    mps3_net_udp_t *u = NULL;
    for (int i = 0; i < MPS3_LWIP_MAX_UDP; i++) {
        if (!s_udps[i].in_use) {
            u = &s_udps[i];
            break;
        }
    }
    if (u == NULL) {
        return NULL;
    }

    struct udp_pcb *pcb = udp_new();
    if (pcb == NULL) {
        return NULL;
    }
    if (udp_bind(pcb, IP_ADDR_ANY, port) != ERR_OK) {
        udp_remove(pcb);
        return NULL;
    }

    memset(u, 0, sizeof(*u));
    u->in_use = 1;
    u->pcb = pcb;
    u->port = pcb->local_port; /* real port when opened with 0 (TID) */
    udp_recv(pcb, udp_recv_cb, u);
    return u;
}

int mps3_net_udp_recvfrom(mps3_net_udp_t *udp, void *buf, uint32_t cap,
                          mps3_net_addr_t *from)
{
    if (udp == NULL || !udp->in_use) {
        return MPS3_NET_ERR;
    }
    if (udp->q_n == 0) {
        return 0;
    }
    mps3_udp_dgram_t *d = &udp->q[udp->q_r];
    uint32_t n = (d->len < cap) ? d->len : cap; /* truncate oversize, per contract */
    memcpy(buf, d->data, n);
    if (from != NULL) {
        *from = d->from;
    }
    udp->q_r = (uint8_t)((udp->q_r + 1) % MPS3_UDP_Q);
    udp->q_n--;
    return (int)n;
}

int mps3_net_udp_sendto(mps3_net_udp_t *udp, const void *buf, uint32_t len,
                        const mps3_net_addr_t *to)
{
    if (udp == NULL || !udp->in_use || to == NULL) {
        return MPS3_NET_ERR;
    }
    struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)len, PBUF_RAM);
    if (p == NULL) {
        return MPS3_NET_ERR;
    }
    pbuf_take(p, buf, (u16_t)len);
    ip_addr_t dst;
    ip_addr_set_ip4_u32(&dst, to->ip); /* the u32 we handed out in recvfrom */
    err_t err = udp_sendto(udp->pcb, p, &dst, to->port);
    pbuf_free(p);
    return (err == ERR_OK) ? (int)len : MPS3_NET_ERR;
}

void mps3_net_udp_close(mps3_net_udp_t *udp)
{
    if (udp == NULL || !udp->in_use) {
        return;
    }
    udp_remove(udp->pcb);
    udp->pcb = NULL;
    udp->in_use = 0;
    udp->q_n = 0;
}

/* ==========================================================================
 * smsc911x <-> lwIP netif glue (the driver's seamed pbuf boundary)
 * ========================================================================== */

static struct netif s_netif;
static uint8_t s_frame[SMSC911X_MAX_FRAME]; /* flatten/land buffer, both dirs */

/* Free-running TX-linkoutput diagnostics (surfaced at the JTAG diag mailbox next
 * to the driver's tx_frames_sent/tx_status_drained/tx_errors). These three live
 * here because they are lwIP-boundary outcomes, not chip events. */
static uint32_t s_tx_fifo_full_drops; /* frames lwIP was told to RETRY (ERR_MEM) */
static uint32_t s_tx_space_stalls;    /* linkoutput calls that spun on TX_SPACE >=1x */
static uint32_t s_tx_iface_errors;    /* ERR_IF: oversize / driver hard error */

/* One TX attempt, as an mps3_spin_until predicate. The spin STOPS on any
 * outcome that is not "no room" — success or a hard error — and the outcome
 * itself is carried out in the context, because the caller has three cases to
 * tell apart and a bool cannot say which. `attempts` is what tells a first-try
 * success from one that had to wait (the tx_space_stalls counter). */
typedef struct {
    uint32_t len;
    int      rc;
    unsigned attempts;
} tx_attempt_ctx_t;

static int tx_frame_settled(void *vctx)
{
    tx_attempt_ctx_t *c = (tx_attempt_ctx_t *)vctx;
    c->attempts++;
    /* smsc911x_tx_frame() reaps completed TX status at entry, so a momentary
     * TX_SPACE clears as egressed frames free TDFREE (and, with the status FIFO
     * drained, the MAC keeps transmitting). */
    c->rc = smsc911x_tx_frame(s_frame, c->len);
    return c->rc != SMSC911X_ERR_TX_SPACE;
}

static err_t lan_linkoutput(struct netif *netif, struct pbuf *p)
{
    (void)netif;
    if (p->tot_len > SMSC911X_MAX_FRAME) {
        s_tx_iface_errors++;
        return ERR_IF;
    }
    /* Flatten the (possibly chained) pbuf — smsc911x_tx_frame() is a
     * single-segment API by design (its README: scatter/gather into the
     * TX command framing wasn't worth it at 100 Mbit). */
    pbuf_copy_partial(p, s_frame, p->tot_len, 0);

    tx_attempt_ctx_t ctx;
    ctx.len = p->tot_len;
    ctx.rc = SMSC911X_ERR_TX_SPACE;
    ctx.attempts = 0u;

    if (!mps3_spin_until(tx_frame_settled, &ctx, MPS3_TX_SPACE_TIMEOUT_US)) {
        /* Still no room after the full MPS3_TX_SPACE_TIMEOUT_US wait. Return
         * ERR_MEM so lwIP RETRIES (the segment stays on the unsent/unacked queue
         * and is re-driven next tcp_output) — critically NOT ERR_OK, which would
         * tell TCP the frame was sent and hide the loss until a full RTO (the
         * silent-drop failure mode). */
        s_tx_fifo_full_drops++;
        return ERR_MEM;
    }
    if (ctx.rc != 0) {
        s_tx_iface_errors++;
        return ERR_IF; /* oversize/hard error — not a transient space wait */
    }
    if (ctx.attempts > 1u) {
        s_tx_space_stalls++; /* it went out, but only after waiting on TX_SPACE */
    }
    return ERR_OK;
}

void mps3_net_lwip_tx_diag(uint32_t *fifo_full_drops, uint32_t *space_stalls,
                           uint32_t *iface_errors)
{
    if (fifo_full_drops) *fifo_full_drops = s_tx_fifo_full_drops;
    if (space_stalls)    *space_stalls    = s_tx_space_stalls;
    if (iface_errors)    *iface_errors    = s_tx_iface_errors;
}

static err_t lan_netif_init_cb(struct netif *netif)
{
    netif->name[0] = 'e';
    netif->name[1] = '0';
    netif->mtu = 1500;
    netif->hwaddr_len = 6;
    /* hwaddr already copied in by mps3_net_lwip_init before netif_add. */
    netif->flags = NETIF_FLAG_BROADCAST | NETIF_FLAG_ETHARP | NETIF_FLAG_LINK_UP;
    netif->output = etharp_output;
    netif->linkoutput = lan_linkoutput;
    return ERR_OK;
}

int mps3_net_lwip_rx_poll(int budget)
{
    int delivered = 0;
    for (int i = 0; i < budget; i++) {
        int len = smsc911x_rx_frame(s_frame, sizeof(s_frame));
        if (len == 0) {
            break;
        }
        if (len < 0) {
            continue; /* errored/oversize frame: consumed + dropped by the
                       * driver (fail closed); keep draining the budget */
        }
        struct pbuf *p = pbuf_alloc(PBUF_RAW, (u16_t)len, PBUF_POOL);
        if (p == NULL) {
            break; /* pool empty — frame dropped; TCP/ARP recover */
        }
        pbuf_take(p, s_frame, (u16_t)len);
        if (s_netif.input(p, &s_netif) != ERR_OK) {
            pbuf_free(p);
        }
        delivered++;
    }
    return delivered;
}

void mps3_net_lwip_tmr(void)
{
    /* NO_SYS_NO_TIMERS=1: tcp_tmr() every TCP_TMR_INTERVAL (250 ms, calls
     * fast+slow internally) and etharp_tmr() every ARP_TMR_INTERVAL
     * (1 s) — the complete timer set for this feature mix (no DHCP/DNS/
     * AutoIP/IGMP/reassembly compiled in). */
    static uint32_t last_tcp, last_arp;
    uint32_t now = mps3_sys_now_ms();

    if ((uint32_t)(now - last_tcp) >= TCP_TMR_INTERVAL) {
        last_tcp = now;
        tcp_tmr();
    }
    if ((uint32_t)(now - last_arp) >= ARP_TMR_INTERVAL) {
        last_arp = now;
        etharp_tmr();
    }
}

void mps3_net_lwip_conn_diag(const struct mps3_net_conn *conn,
                             uint32_t *rcv_wnd, uint32_t *rcv_ann_wnd,
                             uint32_t *rx_queued, uint32_t *snd_buf,
                             uint32_t *snd_wnd)
{
    uint32_t w = 0, aw = 0, q = 0, sb = 0, sw = 0;
    if (conn != NULL) {
        if (conn->pcb != NULL) {
            w  = (uint32_t)conn->pcb->rcv_wnd;      /* window still open to sender */
            aw = (uint32_t)conn->pcb->rcv_ann_wnd;  /* window queued to announce   */
            /* The TX-BACK side (grant delivery): snd_buf==0 => tcp_sndbuf refuses
             * the grant (send-refused); snd_wnd==0 => the peer's advertised window
             * is shut so tcp_output cannot egress the accepted grant. */
            sb = (uint32_t)tcp_sndbuf(conn->pcb);
            sw = (uint32_t)conn->pcb->snd_wnd;
        }
        /* Bytes still un-consumed in the delivered chain: the head pbuf's
         * tot_len covers the whole chain; subtract what mps3_net_recv() already
         * handed out of the head. */
        if (conn->rx != NULL) {
            q = (uint32_t)conn->rx->tot_len - conn->rx_off;
        }
    }
    if (rcv_wnd)     *rcv_wnd     = w;
    if (rcv_ann_wnd) *rcv_ann_wnd = aw;
    if (rx_queued)   *rx_queued   = q;
    if (snd_buf)     *snd_buf     = sb;
    if (snd_wnd)     *snd_wnd     = sw;
}

uint32_t mps3_net_lwip_pbuf_free(void)
{
#if MEMP_STATS
    const struct stats_mem *m = lwip_stats.memp[MEMP_PBUF_POOL];
    if (m != NULL && m->avail >= m->used) {
        return (uint32_t)(m->avail - m->used);
    }
    return 0;
#else
    return 0xFFFFFFFFu; /* MEMP stats compiled out — unknown */
#endif
}

__attribute__((weak)) void mps3_platform_mac(uint8_t mac[6])
{
    /* Locally-administered, unicast ("MPS" tail) — bring-up policy, see
     * file header. Per-octet compile-time overridable so a SECOND board on the
     * same network gets a distinct MAC: override any octet with -DMPS3_MACn=0x..
     * (Makefile: MAC0..MAC5=). Default = the C-board MAC 02:00:00:4D:50:53.
     * The HBI0309B board is built MAC5=0x42 ('B') => 02:00:00:4D:50:42. */
#ifndef MPS3_MAC0
#define MPS3_MAC0 0x02
#endif
#ifndef MPS3_MAC1
#define MPS3_MAC1 0x00
#endif
#ifndef MPS3_MAC2
#define MPS3_MAC2 0x00
#endif
#ifndef MPS3_MAC3
#define MPS3_MAC3 0x4D
#endif
#ifndef MPS3_MAC4
#define MPS3_MAC4 0x50
#endif
#ifndef MPS3_MAC5
#define MPS3_MAC5 0x53
#endif
    static const uint8_t def[6] = { MPS3_MAC0, MPS3_MAC1, MPS3_MAC2,
                                    MPS3_MAC3, MPS3_MAC4, MPS3_MAC5 };
    memcpy(mac, def, 6);
}

/* lwIP's own millisecond hook (referenced by core when timestamps etc. are
 * enabled; harmless and correct to provide unconditionally — sys_arch_raw.c,
 * the RAW/NO_SYS port file, deliberately does not define it). */
u32_t sys_now(void)
{
    return mps3_sys_now_ms();
}

int mps3_net_lwip_init(uintptr_t lan9220_base, const uint8_t mac[6])
{
    int rc = smsc911x_init(lan9220_base, mac);
    if (rc != 0) {
        return rc; /* BYTE_TEST/ID/timeout detail preserved for main's log */
    }

    lwip_init();

    ip4_addr_t ip, mask, gw;
    /* Static default per net-protocol.md; DHCP is the D8 option — wire
     * dhcp_start() + the dhcp BSP knobs here if a deployment ever needs
     * it (deliberately not compiled in: memp/timer cost on a 128 KiB LMB
     * for a lab whose tender assumes the fixed address). */
    IP4_ADDR(&ip,   MPS3_DEFAULT_IP_A, MPS3_DEFAULT_IP_B,
                    MPS3_DEFAULT_IP_C, MPS3_DEFAULT_IP_D);
    IP4_ADDR(&mask, 255, 255, 255, 0);
    IP4_ADDR(&gw,   MPS3_DEFAULT_IP_A, MPS3_DEFAULT_IP_B, MPS3_DEFAULT_IP_C, 1);

    memcpy(s_netif.hwaddr, mac, 6);
    if (netif_add(&s_netif, &ip, &mask, &gw, NULL,
                  lan_netif_init_cb, ethernet_input) == NULL) {
        return -100;
    }
    netif_set_default(&s_netif);
    netif_set_up(&s_netif);
    /* ICMP echo (ping) is answered by lwIP's IP layer itself from here on
     * — no application code (LWIP_ICMP is on in the BSP config). Real
     * link state comes from the LAN9220 PHY autoneg kicked in
     * smsc911x_init(); NETIF_FLAG_LINK_UP is set optimistically in the
     * init callback — once the physical link is actually up, traffic
     * flows (same simplification the harness_app spike documented). */
    return 0;
}
