/*
 * fake_lwip.c — the implementation behind fake_lwip/ (see fake_lwip_ctl.h).
 *
 * Scope discipline: this models ONLY what firmware/platform/src/net_if_lwip.c
 * touches, and it models the parts the tests DEPEND on for real rather than
 * stubbing them:
 *
 *   - pbuf: a table-backed pool with lwIP's exact refcount/chain-walk rules,
 *     so a missing pbuf_ref(), a leak or a double free is a number a test can
 *     assert on instead of a crash that may or may not happen.
 *   - tcp_recved: every call logged (pcb, len) — the window-as-grant path.
 *   - tcp_write/tcp_sndbuf/tcp_output: a settable send buffer and an ERR_MEM
 *     injection, because "short send IS the backpressure signal" is a
 *     contract clause and not a detail.
 *   - the callback registry: tcp_recv/tcp_err/tcp_accept/udp_recv/netif_add
 *     keep the pointers so tests fire the REAL static callbacks.
 *
 * Deliberately inert (and listed as such wherever it matters):
 *   - etharp_output(): assigned to netif->output by lan_netif_init_cb and
 *     never called by anything under test.
 *   - lwip_init()/tcp_tmr()/etharp_tmr(): counted only.
 *   - there is no TCP state machine, no retransmission, no ARP table and no
 *     IP layer. Nothing under test has one either: net_if_lwip.c is glue.
 */
#include <stdlib.h>
#include <string.h>

#include "lwip/init.h"
#include "lwip/netif.h"
#include "lwip/ip4_addr.h"
#include "lwip/ip.h"
#include "lwip/tcp.h"
#include "lwip/udp.h"
#include "lwip/pbuf.h"
#include "lwip/etharp.h"
#include "lwip/stats.h"
#include "lwip/memp.h"
#include "lwip/priv/tcp_priv.h"
#include "netif/ethernet.h"

#include "fake_lwip_ctl.h"

const ip_addr_t ip_addr_any = { 0 };

/* ==========================================================================
 * pbuf pool
 * ========================================================================== */

#define FAKE_PBUF_MAX       256
#define FAKE_POOL_LIMIT_DEF  32   /* stand-in for PBUF_POOL_SIZE */

typedef struct {
    struct pbuf *p;
    int          live;
    int          pooled;   /* allocated with PBUF_POOL (counts against the cap) */
} fake_pbuf_slot_t;

static fake_pbuf_slot_t s_pbufs[FAKE_PBUF_MAX];
static int s_pbuf_faults;
static int s_pool_limit = FAKE_POOL_LIMIT_DEF;

/* pbuf_copy_partial() calls. Counted because ONE production guard is only
 * observable through it: lan_linkoutput()'s oversize check rejects a frame
 * BEFORE flattening it into a fixed SMSC911X_MAX_FRAME buffer, and its return
 * code is indistinguishable from the driver's own oversize rejection. "Was the
 * copy attempted" is the only evidence that the bounds check bounds anything. */
static uint32_t s_copy_partial_calls;

static struct stats_mem s_pbuf_pool_stats;
struct stats_ lwip_stats;

static int pool_live(void)
{
    int n = 0;
    for (int i = 0; i < FAKE_PBUF_MAX; i++) {
        if (s_pbufs[i].live && s_pbufs[i].pooled) n++;
    }
    return n;
}

static void pool_stats_sync(void)
{
    int lim = (s_pool_limit < 0) ? FAKE_PBUF_MAX : s_pool_limit;
    int used = pool_live();
    s_pbuf_pool_stats.name  = "PBUF_POOL";
    s_pbuf_pool_stats.avail = (u16_t)lim;
    s_pbuf_pool_stats.used  = (u16_t)used;
    if (used > s_pbuf_pool_stats.max) {
        s_pbuf_pool_stats.max = (u16_t)used;
    }
    lwip_stats.memp[MEMP_PBUF_POOL] = &s_pbuf_pool_stats;
}

static fake_pbuf_slot_t *slot_of(const struct pbuf *p)
{
    for (int i = 0; i < FAKE_PBUF_MAX; i++) {
        if (s_pbufs[i].p == p) {
            return &s_pbufs[i];
        }
    }
    return NULL;
}

int fake_pbuf_live(void)
{
    int n = 0;
    for (int i = 0; i < FAKE_PBUF_MAX; i++) {
        if (s_pbufs[i].live) n++;
    }
    return n;
}

int fake_pbuf_faults(void) { return s_pbuf_faults; }

uint32_t fake_pbuf_copy_partial_calls(void) { return s_copy_partial_calls; }

void fake_pbuf_set_pool_limit(int limit)
{
    s_pool_limit = limit;
    pool_stats_sync();
}

struct pbuf *pbuf_alloc(pbuf_layer l, u16_t length, pbuf_type type)
{
    (void)l;
    if (type == PBUF_POOL && s_pool_limit >= 0 && pool_live() >= s_pool_limit) {
        pool_stats_sync();
        return NULL; /* pool empty — the caller must handle NULL */
    }
    for (int i = 0; i < FAKE_PBUF_MAX; i++) {
        if (s_pbufs[i].p != NULL) {
            continue;
        }
        /* One block: header then payload. The block is NEVER handed back to
         * free() before fake_lwip_reset_all(), so a stale pointer stays
         * identifiable and a double free is detected instead of corrupting
         * the heap. */
        unsigned char *blk = (unsigned char *)calloc(1, sizeof(struct pbuf) + (size_t)length + 1u);
        if (blk == NULL) {
            return NULL;
        }
        struct pbuf *p = (struct pbuf *)blk;
        p->next    = NULL;
        p->payload = blk + sizeof(struct pbuf);
        p->len     = length;
        p->tot_len = length;
        p->ref     = 1;
        p->type_internal = (u8_t)type;
        s_pbufs[i].p      = p;
        s_pbufs[i].live   = 1;
        s_pbufs[i].pooled = (type == PBUF_POOL);
        pool_stats_sync();
        return p;
    }
    return NULL; /* shim table full — raise FAKE_PBUF_MAX */
}

void pbuf_ref(struct pbuf *p)
{
    if (p == NULL) {
        return;
    }
    fake_pbuf_slot_t *s = slot_of(p);
    if (s == NULL || !s->live) {
        s_pbuf_faults++;
        return;
    }
    p->ref++;
}

u8_t pbuf_free(struct pbuf *p)
{
    u8_t count = 0;
    while (p != NULL) {
        fake_pbuf_slot_t *s = slot_of(p);
        if (s == NULL || !s->live || p->ref == 0) {
            s_pbuf_faults++;      /* double free, or a pbuf we never issued */
            break;
        }
        p->ref--;
        if (p->ref != 0) {
            break;                /* someone else still holds this pbuf */
        }
        struct pbuf *q = p->next;
        s->live = 0;              /* block deliberately NOT free()d — see above */
        count++;
        p = q;
    }
    pool_stats_sync();
    return count;
}

void pbuf_cat(struct pbuf *head, struct pbuf *tail)
{
    struct pbuf *q = head;
    if (head == NULL || tail == NULL) {
        s_pbuf_faults++;
        return;
    }
    /* lwIP: tot_len of every pbuf in `head` grows by the tail's total, and
     * the tail's reference is TAKEN OVER (no pbuf_ref). */
    while (q->next != NULL) {
        q->tot_len = (u16_t)(q->tot_len + tail->tot_len);
        q = q->next;
    }
    q->tot_len = (u16_t)(q->tot_len + tail->tot_len);
    q->next = tail;
}

void pbuf_chain(struct pbuf *head, struct pbuf *tail)
{
    pbuf_cat(head, tail);
    pbuf_ref(tail);
}

u16_t pbuf_copy_partial(const struct pbuf *p, void *dataptr, u16_t len, u16_t offset)
{
    u16_t copied = 0;
    s_copy_partial_calls++;
    u16_t left = offset;
    unsigned char *dst = (unsigned char *)dataptr;

    for (const struct pbuf *q = p; q != NULL && copied < len; q = q->next) {
        if (left >= q->len) {
            left = (u16_t)(left - q->len);
            continue;
        }
        u16_t avail = (u16_t)(q->len - left);
        u16_t take  = (u16_t)(len - copied);
        if (take > avail) {
            take = avail;
        }
        memcpy(dst + copied, (const unsigned char *)q->payload + left, take);
        copied = (u16_t)(copied + take);
        left = 0;
    }
    return copied;
}

err_t pbuf_take(struct pbuf *buf, const void *dataptr, u16_t len)
{
    u16_t done = 0;
    const unsigned char *src = (const unsigned char *)dataptr;

    if (buf == NULL || (dataptr == NULL && len > 0)) {
        return ERR_ARG;
    }
    if (buf->tot_len < len) {
        return ERR_MEM;
    }
    for (struct pbuf *q = buf; q != NULL && done < len; q = q->next) {
        u16_t take = (u16_t)(len - done);
        if (take > q->len) {
            take = q->len;
        }
        memcpy(q->payload, src + done, take);
        done = (u16_t)(done + take);
    }
    return ERR_OK;
}

struct pbuf *fake_pbuf_make(const void *data, int len)
{
    struct pbuf *p = pbuf_alloc(PBUF_RAW, (u16_t)len, PBUF_RAM);
    if (p == NULL) {
        return NULL;
    }
    if (data != NULL && len > 0) {
        memcpy(p->payload, data, (size_t)len);
    }
    return p;
}

/* ==========================================================================
 * TCP
 * ========================================================================== */

#define FAKE_TCP_MAX     64
#define FAKE_RECVED_MAX 128
#define FAKE_TXCAP     8192

static struct tcp_pcb s_tcps[FAKE_TCP_MAX];

typedef struct { struct tcp_pcb *pcb; u16_t len; } recved_rec_t;
static recved_rec_t s_recved[FAKE_RECVED_MAX];
static int      s_recved_n;
static uint32_t s_recved_total;
static u16_t    s_recved_max;

static int      s_tcp_write_calls, s_tcp_output_calls, s_tcp_aborts, s_tcp_closes;
static uint32_t s_tcp_written;
static err_t    s_tcp_write_err = ERR_OK;
static err_t    s_tcp_close_err = ERR_OK;
static u16_t    s_ephemeral = 49152;
static int      s_tcp_tmr_calls;

static struct tcp_pcb *tcp_slot_alloc(void)
{
    for (int i = 0; i < FAKE_TCP_MAX; i++) {
        if (!s_tcps[i].fake_slot_live) {
            struct tcp_pcb *p = &s_tcps[i];
            memset(p, 0, sizeof(*p));
            p->fake_slot_live = 1;
            p->snd_buf = 2048;      /* stand-in for TCP_SND_BUF */
            p->snd_wnd = 8192;
            p->rcv_wnd = TCP_WND;
            p->rcv_ann_wnd = TCP_WND;
            return p;
        }
    }
    return NULL;
}

struct tcp_pcb *tcp_new(void) { return tcp_slot_alloc(); }

err_t tcp_bind(struct tcp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port)
{
    if (pcb == NULL) {
        return ERR_ARG;
    }
    if (ipaddr != NULL) {
        pcb->local_ip = *ipaddr;
    }
    pcb->local_port = port ? port : s_ephemeral++;
    return ERR_OK;
}

struct tcp_pcb *tcp_listen_with_backlog(struct tcp_pcb *pcb, u8_t backlog)
{
    (void)backlog;
    if (pcb == NULL) {
        return NULL;
    }
    /* lwIP frees the bound pcb and returns a DIFFERENT (listen) pcb. Modelled
     * exactly, so code that kept using the pre-listen pointer would break here
     * the way it breaks on target. */
    u16_t port = pcb->local_port;
    ip_addr_t ip = pcb->local_ip;
    pcb->fake_slot_live = 0;
    struct tcp_pcb *l = tcp_slot_alloc();
    if (l == NULL) {
        return NULL;
    }
    l->local_port = port;
    l->local_ip = ip;
    l->fake_is_listen = 1;
    return l;
}

void tcp_arg(struct tcp_pcb *pcb, void *arg)          { if (pcb) pcb->callback_arg = arg; }
void tcp_accept(struct tcp_pcb *pcb, tcp_accept_fn f) { if (pcb) pcb->accept = f; }
void tcp_recv(struct tcp_pcb *pcb, tcp_recv_fn f)     { if (pcb) pcb->recv = f; }
void tcp_sent(struct tcp_pcb *pcb, tcp_sent_fn f)     { if (pcb) pcb->sent = f; }
void tcp_err(struct tcp_pcb *pcb, tcp_err_fn f)       { if (pcb) pcb->errf = f; }

void tcp_abort(struct tcp_pcb *pcb)
{
    if (pcb == NULL) {
        return;
    }
    s_tcp_aborts++;
    pcb->fake_aborted = 1;
    pcb->fake_slot_live = 0;
    /* lwIP calls the err callback from tcp_abort. Every call site in
     * net_if_lwip.c has already cleared it (or never set it), so this is
     * belt-and-braces fidelity rather than a path under test. */
    if (pcb->errf != NULL) {
        pcb->errf(pcb->callback_arg, ERR_ABRT);
    }
}

err_t tcp_close(struct tcp_pcb *pcb)
{
    if (pcb == NULL) {
        return ERR_ARG;
    }
    if (s_tcp_close_err != ERR_OK) {
        return s_tcp_close_err; /* out of memory for the FIN */
    }
    s_tcp_closes++;
    pcb->fake_closed = 1;
    pcb->fake_slot_live = 0;
    return ERR_OK;
}

void tcp_recved(struct tcp_pcb *pcb, u16_t len)
{
    if (s_recved_n < FAKE_RECVED_MAX) {
        s_recved[s_recved_n].pcb = pcb;
        s_recved[s_recved_n].len = len;
    }
    s_recved_n++;
    s_recved_total += len;
    if (len > s_recved_max) {
        s_recved_max = len;
    }
    if (pcb != NULL) {
        uint32_t w = (uint32_t)pcb->rcv_wnd + len;
        pcb->rcv_wnd = (w > 0xFFFFu) ? 0xFFFFu : (u16_t)w;
    }
}

static unsigned char s_tx[FAKE_TXCAP];
static uint32_t      s_tx_used;

err_t tcp_write(struct tcp_pcb *pcb, const void *dataptr, u16_t len, u8_t apiflags)
{
    (void)apiflags;
    if (pcb == NULL) {
        return ERR_ARG;
    }
    if (s_tcp_write_err != ERR_OK) {
        return s_tcp_write_err;
    }
    s_tcp_write_calls++;
    s_tcp_written += len;
    if (s_tx_used + len <= FAKE_TXCAP) {
        memcpy(s_tx + s_tx_used, dataptr, len);
        s_tx_used += len;
    }
    pcb->snd_buf = (u16_t)((pcb->snd_buf > len) ? (pcb->snd_buf - len) : 0);
    return ERR_OK;
}

err_t tcp_output(struct tcp_pcb *pcb)
{
    (void)pcb;
    s_tcp_output_calls++;
    return ERR_OK;
}

void tcp_tmr(void) { s_tcp_tmr_calls++; }

struct tcp_pcb *fake_tcp_incoming(struct tcp_pcb *lpcb, err_t *accept_rc)
{
    struct tcp_pcb *n = tcp_slot_alloc();
    if (n == NULL || lpcb == NULL || lpcb->accept == NULL) {
        if (accept_rc) *accept_rc = ERR_VAL;
        return n;
    }
    n->local_port = lpcb->local_port;
    n->remote_port = s_ephemeral++;
    err_t rc = lpcb->accept(lpcb->callback_arg, n, ERR_OK);
    if (accept_rc) {
        *accept_rc = rc;
    }
    return n;
}

err_t fake_tcp_deliver(struct tcp_pcb *pcb, struct pbuf *p, err_t err)
{
    if (pcb == NULL || pcb->recv == NULL) {
        return ERR_VAL;
    }
    return pcb->recv(pcb->callback_arg, pcb, p, err);
}

err_t fake_tcp_deliver_fin(struct tcp_pcb *pcb)
{
    if (pcb == NULL || pcb->recv == NULL) {
        return ERR_VAL;
    }
    return pcb->recv(pcb->callback_arg, pcb, NULL, ERR_OK);
}

void fake_tcp_fire_err(struct tcp_pcb *pcb, err_t err)
{
    if (pcb == NULL || pcb->errf == NULL) {
        return;
    }
    /* lwIP has ALREADY freed the pcb when err fires (net_if_lwip.c's comment
     * says so and conn_err_cb relies on it), so release the slot first. */
    void *arg = pcb->callback_arg;
    tcp_err_fn fn = pcb->errf;
    pcb->fake_slot_live = 0;
    fn(arg, err);
}

int      fake_tcp_recved_calls(void)  { return s_recved_n; }
uint32_t fake_tcp_recved_total(void)  { return s_recved_total; }
uint16_t fake_tcp_recved_max_chunk(void) { return s_recved_max; }

int fake_tcp_recved_at(int idx, struct tcp_pcb **pcb, uint16_t *len)
{
    if (idx < 0 || idx >= s_recved_n || idx >= FAKE_RECVED_MAX) {
        return 0;
    }
    if (pcb) *pcb = s_recved[idx].pcb;
    if (len) *len = s_recved[idx].len;
    return 1;
}

void fake_tcp_set_sndbuf(struct tcp_pcb *pcb, uint16_t bytes) { if (pcb) pcb->snd_buf = bytes; }
void fake_tcp_set_write_err(err_t e) { s_tcp_write_err = e; }
void fake_tcp_set_close_err(err_t e) { s_tcp_close_err = e; }
int      fake_tcp_write_calls(void)  { return s_tcp_write_calls; }
uint32_t fake_tcp_written_bytes(void){ return s_tcp_written; }
int      fake_tcp_output_calls(void) { return s_tcp_output_calls; }
int      fake_tcp_aborts(void)       { return s_tcp_aborts; }
int      fake_tcp_closes(void)       { return s_tcp_closes; }
int      fake_tcp_tmr_calls(void)    { return s_tcp_tmr_calls; }
int      fake_tcp_was_aborted(const struct tcp_pcb *pcb) { return pcb ? pcb->fake_aborted : 0; }

int fake_tcp_live_pcbs(void)
{
    int n = 0;
    for (int i = 0; i < FAKE_TCP_MAX; i++) {
        if (s_tcps[i].fake_slot_live) n++;
    }
    return n;
}

/* ==========================================================================
 * UDP
 * ========================================================================== */

#define FAKE_UDP_MAX 16

static struct udp_pcb s_udps_pcb[FAKE_UDP_MAX];
static int      s_udp_sendto_calls;
static err_t    s_udp_sendto_err = ERR_OK;
static uint32_t s_udp_last_ip;
static uint16_t s_udp_last_port;
static int      s_udp_last_len;
static unsigned char s_udp_last[2048];

struct udp_pcb *udp_new(void)
{
    for (int i = 0; i < FAKE_UDP_MAX; i++) {
        if (!s_udps_pcb[i].fake_slot_live) {
            memset(&s_udps_pcb[i], 0, sizeof(s_udps_pcb[i]));
            s_udps_pcb[i].fake_slot_live = 1;
            return &s_udps_pcb[i];
        }
    }
    return NULL;
}

err_t udp_bind(struct udp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port)
{
    if (pcb == NULL) {
        return ERR_ARG;
    }
    if (ipaddr != NULL) {
        pcb->local_ip = *ipaddr;
    }
    /* port 0 = ephemeral, which is what mps3_net_udp_open() reads back out of
     * pcb->local_port for a TFTP transfer TID. */
    pcb->local_port = port ? port : s_ephemeral++;
    return ERR_OK;
}

void udp_recv(struct udp_pcb *pcb, udp_recv_fn recv, void *recv_arg)
{
    if (pcb == NULL) {
        return;
    }
    pcb->recv = recv;
    pcb->recv_arg = recv_arg;
}

void udp_remove(struct udp_pcb *pcb)
{
    if (pcb == NULL) {
        return;
    }
    pcb->fake_slot_live = 0;
    pcb->recv = NULL;
}

err_t udp_sendto(struct udp_pcb *pcb, struct pbuf *p, const ip_addr_t *dst_ip, u16_t dst_port)
{
    (void)pcb;
    s_udp_sendto_calls++;
    s_udp_last_ip = dst_ip ? ip4_addr_get_u32(dst_ip) : 0u;
    s_udp_last_port = dst_port;
    s_udp_last_len = p ? (int)pbuf_copy_partial(p, s_udp_last,
                                                (u16_t)((p->tot_len > sizeof(s_udp_last))
                                                        ? sizeof(s_udp_last) : p->tot_len), 0)
                       : 0;
    /* lwIP does NOT take the pbuf here (udp_sendto copies/refs as needed);
     * mps3_net_udp_sendto() frees its own pbuf right after. */
    return s_udp_sendto_err;
}

void fake_udp_deliver(struct udp_pcb *pcb, struct pbuf *p, uint32_t ip, uint16_t port)
{
    ip_addr_t a;
    if (pcb == NULL || pcb->recv == NULL) {
        return;
    }
    ip_addr_set_ip4_u32(&a, ip);
    pcb->recv(pcb->recv_arg, pcb, p, &a, port);
}

void     fake_udp_set_sendto_err(err_t e) { s_udp_sendto_err = e; }
int      fake_udp_sendto_calls(void)      { return s_udp_sendto_calls; }
uint32_t fake_udp_last_dst_ip(void)       { return s_udp_last_ip; }
uint16_t fake_udp_last_dst_port(void)     { return s_udp_last_port; }
int      fake_udp_last_len(void)          { return s_udp_last_len; }

int fake_udp_last_data(void *buf, int cap)
{
    int n = (s_udp_last_len < cap) ? s_udp_last_len : cap;
    if (n > 0) {
        memcpy(buf, s_udp_last, (size_t)n);
    }
    return n;
}

/* ==========================================================================
 * netif / ethernet_input / init / timers
 * ========================================================================== */

#define FAKE_ETH_Q      8
#define FAKE_ETH_FRAME  1600

static struct netif *s_netif_last;
static int  s_netif_add_fails;
static int  s_eth_calls;
static err_t s_eth_result = ERR_OK;
static int  s_lwip_inits;
static int  s_etharp_tmr_calls;

static struct { int len; unsigned char b[FAKE_ETH_FRAME]; } s_ethq[FAKE_ETH_Q];
static int s_ethq_r, s_ethq_w, s_ethq_n;

void lwip_init(void) { s_lwip_inits++; }

struct netif *netif_add(struct netif *netif, const ip4_addr_t *ipaddr,
                        const ip4_addr_t *netmask, const ip4_addr_t *gw,
                        void *state, netif_init_fn init, netif_input_fn input)
{
    if (netif == NULL || s_netif_add_fails) {
        return NULL;
    }
    if (ipaddr)  netif->ip_addr = *ipaddr;
    if (netmask) netif->netmask = *netmask;
    if (gw)      netif->gw = *gw;
    netif->state = state;
    netif->input = input;   /* what mps3_net_lwip_rx_poll() calls */
    if (init != NULL && init(netif) != ERR_OK) {
        return NULL;
    }
    s_netif_last = netif;
    return netif;
}

void netif_set_default(struct netif *netif) { (void)netif; }
void netif_set_up(struct netif *netif)      { if (netif) netif->flags |= NETIF_FLAG_UP; }
void netif_set_link_up(struct netif *netif) { if (netif) netif->flags |= NETIF_FLAG_LINK_UP; }

err_t etharp_output(struct netif *netif, struct pbuf *q, const ip4_addr_t *ipaddr)
{
    /* STUB — see the header. Assigned, never called by anything under test. */
    (void)netif; (void)q; (void)ipaddr;
    return ERR_OK;
}

void etharp_tmr(void) { s_etharp_tmr_calls++; }

err_t ethernet_input(struct pbuf *p, struct netif *netif)
{
    (void)netif;
    s_eth_calls++;
    if (p != NULL && s_ethq_n < FAKE_ETH_Q) {
        u16_t take = (p->tot_len > FAKE_ETH_FRAME) ? FAKE_ETH_FRAME : p->tot_len;
        s_ethq[s_ethq_w].len = (int)pbuf_copy_partial(p, s_ethq[s_ethq_w].b, take, 0);
        s_ethq_w = (s_ethq_w + 1) % FAKE_ETH_Q;
        s_ethq_n++;
    }
    if (s_eth_result != ERR_OK) {
        return s_eth_result; /* refused: the pbuf stays the CALLER's to free */
    }
    pbuf_free(p);            /* accepted: lwIP owns and frees it */
    return ERR_OK;
}

struct netif *fake_netif_last(void)            { return s_netif_last; }
void  fake_netif_set_add_fails(int fails)      { s_netif_add_fails = fails; }
void  fake_eth_set_result(err_t e)             { s_eth_result = e; }
int   fake_eth_input_calls(void)               { return s_eth_calls; }
int   fake_lwip_init_calls(void)               { return s_lwip_inits; }
int   fake_etharp_tmr_calls(void)              { return s_etharp_tmr_calls; }

int fake_eth_take(void *buf, int cap)
{
    if (s_ethq_n == 0) {
        return -1;
    }
    int n = s_ethq[s_ethq_r].len;
    if (n > cap) {
        n = cap;
    }
    memcpy(buf, s_ethq[s_ethq_r].b, (size_t)n);
    s_ethq_r = (s_ethq_r + 1) % FAKE_ETH_Q;
    s_ethq_n--;
    return n;
}

/* ==========================================================================
 * reset
 * ========================================================================== */

void fake_lwip_reset_all(void)
{
    for (int i = 0; i < FAKE_PBUF_MAX; i++) {
        if (s_pbufs[i].p != NULL) {
            free(s_pbufs[i].p);
        }
        s_pbufs[i].p = NULL;
        s_pbufs[i].live = 0;
        s_pbufs[i].pooled = 0;
    }
    s_pbuf_faults = 0;
    s_copy_partial_calls = 0;
    s_pool_limit = FAKE_POOL_LIMIT_DEF;
    memset(&s_pbuf_pool_stats, 0, sizeof(s_pbuf_pool_stats));
    pool_stats_sync();

    memset(s_tcps, 0, sizeof(s_tcps));
    memset(s_recved, 0, sizeof(s_recved));
    s_recved_n = 0;
    s_recved_total = 0;
    s_recved_max = 0;
    s_tcp_write_calls = s_tcp_output_calls = s_tcp_aborts = s_tcp_closes = 0;
    s_tcp_written = 0;
    s_tcp_write_err = ERR_OK;
    s_tcp_close_err = ERR_OK;
    s_tcp_tmr_calls = 0;
    s_tx_used = 0;
    s_ephemeral = 49152;

    memset(s_udps_pcb, 0, sizeof(s_udps_pcb));
    s_udp_sendto_calls = 0;
    s_udp_sendto_err = ERR_OK;
    s_udp_last_ip = 0;
    s_udp_last_port = 0;
    s_udp_last_len = 0;

    s_netif_last = NULL;
    s_netif_add_fails = 0;
    s_eth_calls = 0;
    s_eth_result = ERR_OK;
    s_lwip_inits = 0;
    s_etharp_tmr_calls = 0;
    s_ethq_r = s_ethq_w = s_ethq_n = 0;
}
