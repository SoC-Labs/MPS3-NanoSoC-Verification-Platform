/*
 * fake_lwip/lwip/tcp.h — the RAW-API TCP surface net_if_lwip.c binds to.
 *
 * The pcb carries the same FIELDS the file reads (rcv_wnd / rcv_ann_wnd /
 * snd_wnd / snd_buf feed mps3_net_lwip_conn_diag; keep_* are the keepalive
 * arming), and the callback typedefs are lwIP's, because the whole point of
 * this shim is that the tests drive the code under test through the callbacks
 * it really registers — tcp_recv()/tcp_err()/tcp_accept() store the function
 * pointers and fake_tcp_* (fake_lwip_ctl.h) fires them.
 */
#ifndef FAKE_LWIP_TCP_H
#define FAKE_LWIP_TCP_H

#include "lwip/opt.h"
#include "lwip/arch.h"
#include "lwip/err.h"
#include "lwip/pbuf.h"
#include "lwip/ip4_addr.h"

struct tcp_pcb;

typedef err_t (*tcp_accept_fn)(void *arg, struct tcp_pcb *newpcb, err_t err);
typedef err_t (*tcp_recv_fn)(void *arg, struct tcp_pcb *tpcb, struct pbuf *p, err_t err);
typedef err_t (*tcp_sent_fn)(void *arg, struct tcp_pcb *tpcb, u16_t len);
typedef err_t (*tcp_poll_fn)(void *arg, struct tcp_pcb *tpcb);
typedef void  (*tcp_err_fn)(void *arg, err_t err);

struct tcp_pcb {
    /* --- ip_pcb head: ip_set_option() writes so_options through the cast --- */
    ip_addr_t     local_ip;
    ip_addr_t     remote_ip;
    u8_t          so_options;
    u8_t          ttl;

    u16_t         local_port;
    u16_t         remote_port;

    tcpwnd_size_t rcv_wnd;      /* window still open to the sender     */
    tcpwnd_size_t rcv_ann_wnd;  /* window queued to be announced       */
    tcpwnd_size_t snd_wnd;      /* peer's advertised window            */
    tcpwnd_size_t snd_buf;      /* tcp_sndbuf() reads this             */

    u32_t         keep_idle;
    u32_t         keep_intvl;
    u32_t         keep_cnt;

    void         *callback_arg;
    tcp_accept_fn accept;
    tcp_recv_fn   recv;
    tcp_sent_fn   sent;
    tcp_err_fn    errf;

    /* ---- shim bookkeeping (no lwIP counterpart) ---- */
    u8_t          fake_slot_live;   /* 0 once the shim released it        */
    u8_t          fake_is_listen;
    u8_t          fake_nagle_off;
    u8_t          fake_aborted;
    u8_t          fake_closed;
};

#define TCP_WRITE_FLAG_COPY 0x01
#define TCP_WRITE_FLAG_MORE 0x02

#define tcp_sndbuf(pcb)         ((pcb)->snd_buf)
#define tcp_nagle_disable(pcb)  ((pcb)->fake_nagle_off = 1)
#define tcp_listen(pcb)         tcp_listen_with_backlog((pcb), 0xff)

struct tcp_pcb *tcp_new(void);
err_t           tcp_bind(struct tcp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port);
struct tcp_pcb *tcp_listen_with_backlog(struct tcp_pcb *pcb, u8_t backlog);
void            tcp_abort(struct tcp_pcb *pcb);
err_t           tcp_close(struct tcp_pcb *pcb);
void            tcp_arg(struct tcp_pcb *pcb, void *arg);
void            tcp_accept(struct tcp_pcb *pcb, tcp_accept_fn accept);
void            tcp_recv(struct tcp_pcb *pcb, tcp_recv_fn recv);
void            tcp_sent(struct tcp_pcb *pcb, tcp_sent_fn sent);
void            tcp_err(struct tcp_pcb *pcb, tcp_err_fn err);
void            tcp_recved(struct tcp_pcb *pcb, u16_t len);
err_t           tcp_write(struct tcp_pcb *pcb, const void *dataptr, u16_t len, u8_t apiflags);
err_t           tcp_output(struct tcp_pcb *pcb);

#endif /* FAKE_LWIP_TCP_H */
