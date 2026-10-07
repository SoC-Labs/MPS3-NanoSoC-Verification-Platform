/*
 * fake_lwip_ctl.h — the TEST-FACING half of the host lwIP shim.
 *
 * net_if_lwip.c's interesting code is unreachable from outside: conn_recv_cb,
 * conn_err_cb, listener_accept_cb, udp_recv_cb, lan_linkoutput and
 * lan_netif_init_cb are all static, and they are exactly the functions the
 * 6910 receive-path incident lived in. They are reachable through the
 * REGISTRATION calls, though — tcp_recv(), tcp_err(), tcp_accept(),
 * udp_recv(), netif_add() — so the shim remembers what was registered and
 * this header lets a test fire it. Nothing here reaches into the file under
 * test; every entry point below goes through a pointer net_if_lwip.c itself
 * handed to lwIP.
 *
 * The other half is observation: pbuf pool liveness (leak / double free),
 * the tcp_recved() call log (the window-as-grant assertions), and the
 * tcp_write/tcp_output/tcp_abort counts.
 */
#ifndef FAKE_LWIP_CTL_H
#define FAKE_LWIP_CTL_H

#include <stdint.h>

#include "lwip/pbuf.h"
#include "lwip/tcp.h"
#include "lwip/udp.h"
#include "lwip/netif.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Reset EVERY shim table (pbufs, pcbs, netif, counters). Call at the top of
 * each test case; it is the shim's mock_regs_reset(). */
void fake_lwip_reset_all(void);

/* ---- pbuf pool ---------------------------------------------------------- */

/* Live (allocated, not yet freed) pbufs. The baseline assertion: a receive
 * path that consumed a whole chain must bring this back to what it was. */
int  fake_pbuf_live(void);

/* Refcount crimes the shim caught: freeing a pbuf it never handed out, or
 * freeing one whose ref is already 0 (a double free). Blocks are never
 * returned to malloc before fake_lwip_reset_all(), so a freed pbuf is still
 * RECOGNISED rather than reused — which is what makes this exact. */
int  fake_pbuf_faults(void);

/* pbuf_copy_partial() call count. The witness for "lan_linkoutput() rejected
 * the frame BEFORE flattening it into s_frame" — its ERR_IF is otherwise
 * identical to the driver's own oversize rejection. */
uint32_t fake_pbuf_copy_partial_calls(void);

/* Cap on live PBUF_POOL pbufs (PBUF_RAM is not capped). Models an exhausted
 * lwIP pool: pbuf_alloc() returns NULL. Also drives lwip_stats.memp
 * avail/used, so mps3_net_lwip_pbuf_free() reports it. */
void fake_pbuf_set_pool_limit(int limit);

/* Build one PBUF_RAM pbuf holding `len` bytes of `data` (data may be NULL for
 * uninitialised). NULL if the table is full. */
struct pbuf *fake_pbuf_make(const void *data, int len);

/* ---- TCP ---------------------------------------------------------------- */

/* Deliver an inbound connection to a LISTEN pcb: allocates a pcb and calls
 * the accept callback net_if_lwip.c registered. Returns the new pcb (valid to
 * inspect even if the callback refused and aborted it); *accept_rc, when not
 * NULL, receives the callback's return code. */
struct tcp_pcb *fake_tcp_incoming(struct tcp_pcb *lpcb, err_t *accept_rc);

/* Fire the connection's recv callback with a pbuf chain (`err` is the err_t
 * lwIP would pass; ERR_OK for normal data). Ownership follows lwIP: the
 * callback takes the chain. */
err_t fake_tcp_deliver(struct tcp_pcb *pcb, struct pbuf *p, err_t err);

/* Fire the recv callback with p == NULL: the graceful FIN. */
err_t fake_tcp_deliver_fin(struct tcp_pcb *pcb);

/* Fire the err callback (RST / keepalive reap). lwIP has already freed the
 * pcb when this fires, so the shim releases the slot first — a use-after-free
 * in the callback would then read a released pcb. */
void  fake_tcp_fire_err(struct tcp_pcb *pcb, err_t err);

/* tcp_recved() call log — the window assertions read this. */
int      fake_tcp_recved_calls(void);
uint32_t fake_tcp_recved_total(void);
uint16_t fake_tcp_recved_max_chunk(void);
int      fake_tcp_recved_at(int idx, struct tcp_pcb **pcb, uint16_t *len);

/* tcp_write / tcp_output / tcp_abort / tcp_close observation + injection. */
void     fake_tcp_set_sndbuf(struct tcp_pcb *pcb, uint16_t bytes);
void     fake_tcp_set_write_err(err_t e);   /* ERR_OK = no injection */
void     fake_tcp_set_close_err(err_t e);   /* forces mps3_net_close's abort fallback */
int      fake_tcp_write_calls(void);
uint32_t fake_tcp_written_bytes(void);
int      fake_tcp_output_calls(void);
int      fake_tcp_aborts(void);
int      fake_tcp_closes(void);
int      fake_tcp_was_aborted(const struct tcp_pcb *pcb);
int      fake_tcp_live_pcbs(void);
int      fake_tcp_tmr_calls(void);

/* ---- UDP ---------------------------------------------------------------- */

/* Fire the socket's recv callback with a datagram from ip:port. Ownership
 * follows lwIP: udp_recv_cb owns (and must free) the pbuf. */
void     fake_udp_deliver(struct udp_pcb *pcb, struct pbuf *p,
                          uint32_t ip, uint16_t port);
void     fake_udp_set_sendto_err(err_t e);
int      fake_udp_sendto_calls(void);
uint32_t fake_udp_last_dst_ip(void);
uint16_t fake_udp_last_dst_port(void);
int      fake_udp_last_len(void);
int      fake_udp_last_data(void *buf, int cap);

/* ---- netif / ethernet_input / timers ------------------------------------ */

/* The netif net_if_lwip.c registered (its own static s_netif). This is how a
 * test reaches lan_linkoutput without naming it. NULL before init. */
struct netif *fake_netif_last(void);
void  fake_netif_set_add_fails(int fails);

/* What ethernet_input returns. ERR_OK (default) = it takes and frees the
 * pbuf; anything else = it refuses and leaves the pbuf to the caller, which
 * is the branch mps3_net_lwip_rx_poll() frees on. */
void  fake_eth_set_result(err_t e);
int   fake_eth_input_calls(void);
int   fake_eth_take(void *buf, int cap);  /* oldest captured frame, -1 = none */

int   fake_lwip_init_calls(void);
int   fake_etharp_tmr_calls(void);

#ifdef __cplusplus
}
#endif

#endif /* FAKE_LWIP_CTL_H */
