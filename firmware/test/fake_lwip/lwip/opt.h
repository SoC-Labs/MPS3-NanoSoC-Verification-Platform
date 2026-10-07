/*
 * fake_lwip/lwip/opt.h — the lwipopts-equivalent for the host shim.
 *
 * These are the compile-time options net_if_lwip.c actually reads. They are
 * set to the values the REAL BSP is configured with (create_platform.tcl:
 * NO_SYS=1, NO_SYS_NO_TIMERS=1, RAW API, TCP keepalive ON, MEMP stats ON),
 * because two of them change the code that gets compiled:
 *
 *   LWIP_TCP_KEEPALIVE — 0 would compile out the per-pcb keep_idle/keep_intvl/
 *   keep_cnt arming in listener_accept_cb AND fire net_if_lwip.c's #warning,
 *   which is an ERROR under the harness's -Werror. Testing the arming at all
 *   requires this to be 1, exactly as the target BSP has it.
 *
 *   MEMP_STATS — 0 would compile mps3_net_lwip_pbuf_free() down to the
 *   "unknown" 0xFFFFFFFF branch.
 */
#ifndef FAKE_LWIP_OPT_H
#define FAKE_LWIP_OPT_H

#define NO_SYS                  1
#define NO_SYS_NO_TIMERS        1
#define LWIP_TCP                1
#define LWIP_UDP                1
#define LWIP_TCP_KEEPALIVE      1
#define MEMP_STATS              1
#define LWIP_STATS              1
#define LWIP_IPV4               1
#define LWIP_IPV6               0

/* The advertised receive window. net_if_lwip.c only cites it in comments
 * (mps3_net_recved's "nbytes is bounded by the app window == TCP_WND"), but
 * the constant belongs here so the shim states the same number the BSP does. */
#define TCP_WND             16384

#endif /* FAKE_LWIP_OPT_H */
