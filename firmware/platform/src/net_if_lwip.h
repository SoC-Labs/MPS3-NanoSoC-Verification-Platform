/*
 * net_if_lwip.h — TARGET-ONLY companion header for net_if_lwip.c (the lwIP
 * RAW-API backend of common/net_if.h, plus the smsc911x <-> lwIP netif
 * glue). Only firmware/platform/src/main.c includes this; no portable
 * firmware module may (seam discipline: firmware/README.md "no firmware
 * module includes an lwIP or Xilinx header" — this header is on the
 * platform side of that line, alongside main.c).
 */
#ifndef MPS3_NET_IF_LWIP_H
#define MPS3_NET_IF_LWIP_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct mps3_net_conn; /* opaque — defined in net_if_lwip.c */

/* lwIP diagnostics for one TCP connection (the config-agent's 6910 push socket).
 * RX side: rcv_wnd / rcv_ann_wnd and the bytes queued (un-consumed) in conn->rx.
 * TX-BACK side (the grant-delivery path): snd_buf = tcp_sndbuf() (0 => a grant
 * send is refused) and snd_wnd = the peer's advertised window (0 => tcp_output
 * cannot egress an accepted grant). Any out-pointer may be NULL; a NULL conn
 * yields zeros. Feeds the diag mailbox so a HW run can localise the windowed
 * grant deadlock. */
void mps3_net_lwip_conn_diag(const struct mps3_net_conn *conn,
                             uint32_t *rcv_wnd, uint32_t *rcv_ann_wnd,
                             uint32_t *rx_queued, uint32_t *snd_buf,
                             uint32_t *snd_wnd);

/* Free buffers left in lwIP's PBUF_POOL (MEMP stats). ~0u if stats are
 * compiled out. A pool at 0 = inbound frames being dropped for want of a pbuf,
 * the other candidate RX-stall cause. */
uint32_t mps3_net_lwip_pbuf_free(void);

/* TX-linkoutput diagnostics (the lwIP-boundary counterparts of the driver's
 * smsc911x_get_tx_diag): fifo_full_drops = frames lwIP was told to RETRY (ERR_MEM
 * after the full TX-space spin, never a silent as-sent drop); space_stalls =
 * linkoutput calls that had to spin on a momentarily-full TX FIFO before
 * succeeding; iface_errors = ERR_IF rejects (oversize / driver hard error). Any
 * out-pointer may be NULL. Feeds the diag mailbox so a HW run shows whether TX
 * back-pressure ever forces a retry. */
void mps3_net_lwip_tx_diag(uint32_t *fifo_full_drops, uint32_t *space_stalls,
                           uint32_t *iface_errors);

/* Bring up the whole network stack, in order: smsc911x_init(lan9220_base,
 * mac) -> lwip_init() -> netif_add(static IP 192.168.10.101/24 per
 * net-protocol.md; DHCP = D8 option, deliberately not compiled in — see
 * README) -> netif up. Returns 0, or a negative smsc911x error /
 * -100-range lwIP setup error (main prints and parks — bring-up checks
 * BYTE_TEST/ID_REV are inside smsc911x_init). MUST run before any module
 * _init() that calls mps3_net_listen()/mps3_net_udp_open(). */
int mps3_net_lwip_init(uintptr_t lan9220_base, const uint8_t mac[6]);

/* Drain up to `budget` received frames out of the LAN9220 into lwIP
 * (netif->input) — this build's equivalent of the classic Xilinx
 * xemacif_input(). Returns frames delivered. Call every superloop pass. */
int mps3_net_lwip_rx_poll(int budget);

/* Drive lwIP's TCP/ARP timers off sys_now() deltas (NO_SYS=1,
 * NO_SYS_NO_TIMERS=1 build: tcp_fasttmr/tcp_slowtmr/etharp_tmr are called
 * manually, the classic Xilinx RAW-mode pattern — nothing else in this
 * feature set needs a timer: DHCP/AutoIP/DNS/IP-reassembly are compiled
 * out). Call every superloop pass; cheap when no interval has elapsed. */
void mps3_net_lwip_tmr(void);

/* Default MAC: locally-administered 02:00:00:4D:50:53 ("MPS" tail — same
 * single-board bring-up policy as the harness_app spike documented).
 * WEAK — a provisioning source (QSPI record / static_id-derived, A6
 * decision pending) overrides it by defining a strong copy. Fills mac[6]. */
void mps3_platform_mac(uint8_t mac[6]);

/* lwIP's monotonic millisecond clock — REQUIRED BY the lwIP port headers
 * (sys_now); provided by main.c from the free-running AXI timer. Declared
 * here so both files agree on the one signature without including lwIP
 * headers in main.c. */
uint32_t mps3_sys_now_ms(void);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_NET_IF_LWIP_H */
