/*
 * fake_lwip/lwip/init.h — lwip_init(). Counted, not simulated: nothing in
 * the shim needs global init, but the count proves mps3_net_lwip_init()
 * still calls it before touching a pcb.
 */
#ifndef FAKE_LWIP_INIT_H
#define FAKE_LWIP_INIT_H

#include "lwip/opt.h"

void lwip_init(void);

#endif /* FAKE_LWIP_INIT_H */
