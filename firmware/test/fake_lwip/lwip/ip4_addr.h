/*
 * fake_lwip/lwip/ip4_addr.h — the ip4 address type plus the four accessors
 * net_if_lwip.c uses to move a peer address across the net_if.h seam
 * (mps3_net_addr_t.ip is "an opaque u32 echoed back verbatim", and THIS is
 * where the opacity is implemented).
 */
#ifndef FAKE_LWIP_IP4_ADDR_H
#define FAKE_LWIP_IP4_ADDR_H

#include "lwip/opt.h"
#include "lwip/arch.h"

struct ip4_addr {
    u32_t addr;     /* network byte order on target; host order here — the
                     * seam never interprets it, so the shim does not
                     * byte-swap and no test may depend on the order. */
};
typedef struct ip4_addr ip4_addr_t;

/* LWIP_IPV6 is off in this BSP, so ip_addr_t IS ip4_addr_t and ip_2_ip4() is
 * the identity — same collapse the target build gets. */
typedef ip4_addr_t ip_addr_t;

#define ip_2_ip4(ipaddr)                 (ipaddr)
#define ip4_addr_get_u32(src_ipaddr)     ((src_ipaddr)->addr)
#define ip4_addr_set_u32(dst, u32)       ((dst)->addr = (u32))
#define ip_addr_set_ip4_u32(ipaddr, val) ((ipaddr)->addr = (val))

#define IP4_ADDR(ipaddr, a, b, c, d) \
    (ipaddr)->addr = (((u32_t)((a) & 0xff) << 24) | ((u32_t)((b) & 0xff) << 16) | \
                      ((u32_t)((c) & 0xff) <<  8) |  (u32_t)((d) & 0xff))

extern const ip_addr_t ip_addr_any;
#define IP_ADDR_ANY (&ip_addr_any)
#define IP4_ADDR_ANY (&ip_addr_any)

#endif /* FAKE_LWIP_IP4_ADDR_H */
