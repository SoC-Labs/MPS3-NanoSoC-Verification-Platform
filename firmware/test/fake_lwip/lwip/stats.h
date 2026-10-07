/*
 * fake_lwip/lwip/stats.h — the MEMP slice of lwIP's stats block, which is
 * all mps3_net_lwip_pbuf_free() reads. The shim keeps avail/used honest
 * against the fake pbuf pool (fake_lwip.c), so "pool free" is a real
 * number here and the pool-exhaustion rx_poll test can assert on it.
 */
#ifndef FAKE_LWIP_STATS_H
#define FAKE_LWIP_STATS_H

#include "lwip/opt.h"
#include "lwip/arch.h"
#include "lwip/memp.h"

struct stats_mem {
    const char *name;
    u16_t avail;
    u16_t used;
    u16_t max;
    u32_t err;
    u32_t illegal;
};

struct stats_ {
    struct stats_mem *memp[MEMP_MAX];
};

extern struct stats_ lwip_stats;

#endif /* FAKE_LWIP_STATS_H */
