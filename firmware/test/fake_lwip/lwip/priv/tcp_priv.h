/*
 * fake_lwip/lwip/priv/tcp_priv.h — the NO_SYS_NO_TIMERS manual-timer entry.
 * TCP_TMR_INTERVAL lives here in real lwIP too, and mps3_net_lwip_tmr()
 * compares against it, so the shim keeps the real 250 ms.
 */
#ifndef FAKE_LWIP_TCP_PRIV_H
#define FAKE_LWIP_TCP_PRIV_H

#include "lwip/opt.h"
#include "lwip/tcp.h"

#define TCP_TMR_INTERVAL   250   /* ms: tcp_tmr() cadence  */
#define TCP_FAST_INTERVAL  250
#define TCP_SLOW_INTERVAL  500

void tcp_tmr(void);

#endif /* FAKE_LWIP_TCP_PRIV_H */
