/*
 * fake_lwip/lwip/udp.h — the RAW-API UDP surface (TFTP's whole transport).
 * Same principle as tcp.h: udp_recv() stores the callback and the test fires
 * it via fake_udp_deliver(), so udp_recv_cb's ring bookkeeping is exercised
 * through the registration path net_if_lwip.c really uses.
 */
#ifndef FAKE_LWIP_UDP_H
#define FAKE_LWIP_UDP_H

#include "lwip/opt.h"
#include "lwip/arch.h"
#include "lwip/err.h"
#include "lwip/pbuf.h"
#include "lwip/ip4_addr.h"

struct udp_pcb;

typedef void (*udp_recv_fn)(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                            const ip_addr_t *addr, u16_t port);

struct udp_pcb {
    ip_addr_t   local_ip;
    ip_addr_t   remote_ip;
    u8_t        so_options;
    u16_t       local_port;
    u16_t       remote_port;
    udp_recv_fn recv;
    void       *recv_arg;
    u8_t        fake_slot_live;
};

struct udp_pcb *udp_new(void);
err_t           udp_bind(struct udp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port);
void            udp_recv(struct udp_pcb *pcb, udp_recv_fn recv, void *recv_arg);
void            udp_remove(struct udp_pcb *pcb);
err_t           udp_sendto(struct udp_pcb *pcb, struct pbuf *p,
                           const ip_addr_t *dst_ip, u16_t dst_port);

#endif /* FAKE_LWIP_UDP_H */
