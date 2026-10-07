/*
 * fake_lwip/netif/ethernet.h — ethernet_input(), the netif->input the RX
 * poll pushes frames into.
 *
 * MODELLED, not stubbed: it takes ownership of the pbuf on ERR_OK (frees it)
 * and does NOT on an error return, which is precisely the ownership rule
 * mps3_net_lwip_rx_poll() implements on the other side
 * (`if (input(...) != ERR_OK) pbuf_free(p);`). Get that backwards in either
 * place and the pool leaks — which fake_pbuf_live() then catches.
 */
#ifndef FAKE_LWIP_ETHERNET_H
#define FAKE_LWIP_ETHERNET_H

#include "lwip/opt.h"
#include "lwip/netif.h"
#include "lwip/pbuf.h"

err_t ethernet_input(struct pbuf *p, struct netif *netif);

#endif /* FAKE_LWIP_ETHERNET_H */
