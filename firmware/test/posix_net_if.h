/*
 * posix_net_if.h — the THIRD backend behind common/net_if.h: real POSIX
 * sockets, host-gcc only.
 *
 * common/net_if.h's header used to say "exactly two backends exist"
 * (fake_net_if.c in-memory queues; platform/src/net_if_lwip.c on target). This
 * is the third, and it exists for one reason: to let a REAL external client
 * drive the REAL firmware protocol engines over a REAL TCP socket with no
 * board, no MicroBlaze and no lwIP.
 *
 * Its first consumer is xvc_fw_daemon.c (`make -C firmware/test tools`), which
 * links the REAL firmware/xvc_server/xvc_server.c built for the SWDBB target so
 * the actual Synopsys Identify debugger can drive it. Before this backend
 * existed, that engine was proven only by INFERENCE — hand-built XVC byte
 * streams pushed through it by unit tests. Now the bytes come off a socket that
 * a licensed vendor tool put them on.
 *
 * ===========================================================================
 * The seam contract, and where each clause is satisfied
 * ===========================================================================
 *   "Every call is NON-BLOCKING"          every socket is O_NONBLOCK and every
 *                                         recv/send additionally passes
 *                                         MSG_DONTWAIT; accept() on a
 *                                         non-blocking listener returns
 *                                         EAGAIN -> NULL.
 *   "handle stays valid until close(),    mps3_net_recv() calls recv(), which
 *    INCLUDING after the peer closes —     returns buffered bytes FIRST and only
 *    must drain, THEN see CLOSED"          returns 0 once the queue is empty.
 *                                         The kernel gives us this ordering; the
 *                                         backend must not pre-empt it by
 *                                         reacting to the FIN event instead
 *                                         (see MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN
 *                                         below, the negative control that
 *                                         proves this test can fail).
 *   "listen() twice on one port returns   a port table; a second listen on the
 *    the SAME listener"                    same LOGICAL port returns the same
 *                                         pointer, no second bind().
 *   "send() may return short"             EAGAIN -> 0, partial write -> n.
 *
 * NOT implemented (and fail-closed about it): window-as-grant receive flow
 * control. mps3_net_set_manual_window()/mps3_net_recved() are lwIP-specific
 * pacing over TCP_WND, which a userspace socket cannot express; there is a
 * hard #error in posix_net_if.c against MPS3_CFG_AGENT_WINDOWED so nothing that
 * DEPENDS on that pacing can be linked against this backend by accident. On
 * this path nothing uses it: xvc_server.c never calls either function.
 *
 * The UDP surface IS fully implemented (bind/recvfrom/sendto over real
 * datagram sockets, ephemeral TIDs via port 0), not stubbed — see the tests in
 * test_posix_net_if.c.
 */
#ifndef MPS3_POSIX_NET_IF_H
#define MPS3_POSIX_NET_IF_H

#include <stdint.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Close every socket and clear the tables. Call before the first listen and
 * between test cases (the analogue of fake_net_reset()). Also clears the port
 * map and restores loopback-only binding. */
void posix_net_reset(void);

/* ---- deployment knobs (all optional; defaults are the safe ones) ---------- */

/* Remap a LOGICAL port to an ACTUAL bind port.
 *
 * The firmware modules bind their own contract ports by name (xvc_server.c does
 * mps3_net_listen(MPS3_PORT_XVC) = 2542) and must not be edited to be testable.
 * So the mapping lives here: map 2542 -> 57015 to meet a client that insists on
 * the debugger's own default, or 2542 -> 0 to let the kernel pick a free
 * ephemeral port (which is what the loopback regression does, so two concurrent
 * runs can never collide on a fixed port). Read the port the kernel actually
 * chose back with posix_net_actual_port().
 *
 * Must be called BEFORE the module's _init() binds the listener. */
void posix_net_map_port(uint16_t logical, uint16_t actual);

/* The port a LOGICAL port is really bound to, or 0 if it is not listening. With
 * an ephemeral mapping (actual == 0) this is the only way to learn it. */
uint16_t posix_net_actual_port(uint16_t logical);

/* Bind to 0.0.0.0 instead of 127.0.0.1 (default: loopback only).
 *
 * Loopback is the default deliberately: this backend serves an unauthenticated
 * JTAG bit-bang, so exposing it on every interface has to be an explicit
 * decision, not a default. Must be called before the first listen/udp_open. */
void posix_net_set_bind_any(int any);

/* Where to log listen/accept/close/error events (NULL = silent, the default).
 * The daemon points this at stdout: the backend is the only place that SEES an
 * accept, so without it a run log cannot show that a client connected. */
void posix_net_set_log(FILE *f);

/* Monotonic activity counter: accepted connections + bytes moved in either
 * direction. A superloop can compare it across a poll to decide whether to
 * sleep instead of spinning, without the backend having to expose fds. */
uint64_t posix_net_activity(void);

/* ---- Driving a superloop (src/linux_harness/sw/harnessd) -------------------
 *
 * The firmware's superloop polls; on a bare-metal MicroBlaze spinning is free,
 * on a one-hart Linux box it starves everything else. These three let a loop
 * that did no work SLEEP until a socket has something for it. */

/* Every LOGICAL port N (N != 0) binds at N + offset unless posix_net_map_port()
 * named it explicitly. Lets a host test run the whole shell (including TFTP's
 * privileged :69) unprivileged, and two runs side by side. Ephemeral (0) ports
 * are never offset. Must be set before the first listen/udp_open. */
void posix_net_set_port_offset(int32_t offset);

/* poll() every open listener, live connection and UDP socket for POLLIN, for at
 * most timeout_ms. Returns >0 when something is ready, 0 on timeout (and on
 * EINTR, so a signal just ends the wait early), <0 on a poll() error.
 * Connections that already reported CLOSED or an error are NOT polled: a
 * drained-EOF socket reads ready forever and would turn the wait into a spin. */
int posix_net_wait(uint32_t timeout_ms);

/* Live connections accepted on the listener for LOGICAL port `logical_port`
 * (a connection that has reported CLOSED/error is not live). */
unsigned posix_net_conns_on(uint16_t logical_port);

/* The kernel fd behind a connection (for getsockopt TCP_INFO), or -1. */
struct mps3_net_conn;
int posix_net_conn_fd(const struct mps3_net_conn *conn);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_POSIX_NET_IF_H */
