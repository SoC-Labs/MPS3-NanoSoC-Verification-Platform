/*
 * fake_lwip/lwip/netif.h — the netif, and the reason the linkoutput tests
 * can exist at all.
 *
 * lan_linkoutput() and lan_netif_init_cb() are static inside net_if_lwip.c;
 * nothing outside can name them. But netif_add() is handed the init callback
 * and hands back the netif with ->linkoutput filled in, exactly as on target.
 * The shim remembers that netif (fake_netif_last()), so a test calls the REAL
 * registered function pointer rather than a copy of the code.
 */
#ifndef FAKE_LWIP_NETIF_H
#define FAKE_LWIP_NETIF_H

#include "lwip/opt.h"
#include "lwip/arch.h"
#include "lwip/err.h"
#include "lwip/pbuf.h"
#include "lwip/ip4_addr.h"

struct netif;

typedef err_t (*netif_init_fn)(struct netif *netif);
typedef err_t (*netif_input_fn)(struct pbuf *p, struct netif *inp);
typedef err_t (*netif_output_fn)(struct netif *netif, struct pbuf *p,
                                 const ip4_addr_t *ipaddr);
typedef err_t (*netif_linkoutput_fn)(struct netif *netif, struct pbuf *p);

#define NETIF_MAX_HWADDR_LEN 6U

#define NETIF_FLAG_UP         0x01U
#define NETIF_FLAG_BROADCAST  0x02U
#define NETIF_FLAG_LINK_UP    0x04U
#define NETIF_FLAG_ETHARP     0x08U
#define NETIF_FLAG_ETHERNET   0x10U
#define NETIF_FLAG_IGMP       0x20U

struct netif {
    struct netif       *next;
    ip4_addr_t          ip_addr;
    ip4_addr_t          netmask;
    ip4_addr_t          gw;
    netif_input_fn      input;
    netif_output_fn     output;
    netif_linkoutput_fn linkoutput;
    void               *state;
    u16_t               mtu;
    char                name[2];
    u8_t                num;
    u8_t                flags;
    u8_t                hwaddr_len;
    u8_t                hwaddr[NETIF_MAX_HWADDR_LEN];
};

struct netif *netif_add(struct netif *netif, const ip4_addr_t *ipaddr,
                        const ip4_addr_t *netmask, const ip4_addr_t *gw,
                        void *state, netif_init_fn init, netif_input_fn input);
void netif_set_default(struct netif *netif);
void netif_set_up(struct netif *netif);
void netif_set_link_up(struct netif *netif);

#endif /* FAKE_LWIP_NETIF_H */
