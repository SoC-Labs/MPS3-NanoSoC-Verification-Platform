/*
 * fake_lwip/lwip/ip.h — only what net_if_lwip.c takes from it: the socket
 * option bits and ip_set_option(). SOF_KEEPALIVE's VALUE is the real 0x08
 * so the arming assertion means the same thing here as on target.
 */
#ifndef FAKE_LWIP_IP_H
#define FAKE_LWIP_IP_H

#include "lwip/opt.h"
#include "lwip/ip4_addr.h"

#define SOF_REUSEADDR   0x04U
#define SOF_KEEPALIVE   0x08U
#define SOF_BROADCAST   0x20U

#define ip_set_option(pcb, opt) ((pcb)->so_options = (u8_t)((pcb)->so_options | (opt)))
#define ip_reset_option(pcb, opt) ((pcb)->so_options = (u8_t)((pcb)->so_options & ~(opt)))
#define ip_get_option(pcb, opt) ((pcb)->so_options & (opt))

#endif /* FAKE_LWIP_IP_H */
