/*
 * fake_lwip/lwip/memp.h — just the pool index net_if_lwip.c names.
 * The real enum has ~20 entries; only MEMP_PBUF_POOL is referenced, and
 * MEMP_MAX sizes the stats array in stats.h.
 */
#ifndef FAKE_LWIP_MEMP_H
#define FAKE_LWIP_MEMP_H

#include "lwip/opt.h"

typedef enum {
    MEMP_PBUF_POOL = 0,
    MEMP_TCP_PCB,
    MEMP_UDP_PCB,
    MEMP_MAX
} memp_t;

#endif /* FAKE_LWIP_MEMP_H */
