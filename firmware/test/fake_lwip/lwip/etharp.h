/*
 * fake_lwip/lwip/etharp.h — etharp_output() and etharp_tmr().
 *
 * HONEST LABEL: etharp_output is a pure STUB here. net_if_lwip.c only ever
 * ASSIGNS it (netif->output = etharp_output) and never calls it; nothing
 * below the seam does either, because the tests drive linkoutput directly.
 * etharp_tmr() is counted so mps3_net_lwip_tmr()'s cadence is observable.
 * ARP_TMR_INTERVAL keeps lwIP's real 1 s.
 */
#ifndef FAKE_LWIP_ETHARP_H
#define FAKE_LWIP_ETHARP_H

#include "lwip/opt.h"
#include "lwip/netif.h"

#define ARP_TMR_INTERVAL 1000  /* ms */

err_t etharp_output(struct netif *netif, struct pbuf *q, const ip4_addr_t *ipaddr);
void  etharp_tmr(void);

#endif /* FAKE_LWIP_ETHARP_H */
