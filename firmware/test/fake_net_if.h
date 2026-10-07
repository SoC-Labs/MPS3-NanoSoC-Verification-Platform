/*
 * fake_net_if.h — the host-gcc harness backend for common/net_if.h
 * (W-NET-SEAM). Implements the firmware-facing seam functions
 * (mps3_net_listen/accept/recv/send/close + the UDP surface) over
 * in-memory queues, and exposes this CLIENT-side API so a test can play
 * the host: connect to a listening port, push bytes at the firmware,
 * read back what the firmware sent, inject/capture UDP datagrams (TFTP).
 *
 * Deliberately mirrors what pyverify's real clients do over real sockets
 * against host/pyverify/pyverify/testing/fakeshell.py — same seam, roles
 * reversed: there Python fakes the SHELL for the host stack; here C tests
 * fake the HOST for the shell firmware.
 */
#ifndef MPS3_FAKE_NET_IF_H
#define MPS3_FAKE_NET_IF_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Drop every listener/connection/socket/datagram. Call at the start of
 * each test case (module _init()s re-listen idempotently afterwards). */
void fake_net_reset(void);

/* ---- TCP client side ----------------------------------------------------- */

/* Connect to a port the firmware is listening on. Returns a client handle
 * (>= 0) or -1 if nothing listens there / table full. The connection shows
 * up on the firmware side at its next mps3_net_accept(). */
int fake_net_connect(uint16_t port);

/* Client -> firmware bytes. Returns bytes queued (short only if the
 * in-memory ring fills), -1 on a bad/closed handle. */
int fake_net_send(int client, const void *buf, int len);

/* Firmware -> client bytes. Returns bytes copied (0 = none pending),
 * -1 on a bad handle. Keeps draining even after either side closed. */
int fake_net_recv(int client, void *buf, int cap);

/* 1 once the FIRMWARE has closed its end, else 0. */
int fake_net_fw_closed(int client);

/* Close the CLIENT end (firmware's next recv on it drains remaining bytes
 * then sees MPS3_NET_CLOSED). */
void fake_net_close(int client);

/* Abortive close: model a peer that died WITHOUT a FIN and is then reaped by
 * lwIP TCP keepalive. The firmware's next drained recv/send on this handle sees
 * MPS3_NET_ERR (not MPS3_NET_CLOSED) — the transport-error surface the real lwIP
 * backend produces when tcp_slowtmr aborts the pcb after its keepalive probes go
 * unanswered. Lets a host test prove the shell RELEASES the wedged session (so a
 * new client is accepted) without needing real sockets/timers. */
void fake_net_kill_peer(int client);

/* Cap how many bytes each mps3_net_send() may accept on this connection
 * (-1 = unlimited, the default) — lets a test exercise the modules'
 * send-backpressure/retry paths. */
void fake_net_set_send_limit(int client, int limit);

/* Number of not-yet-accepted pending connects on `port` (diagnostics). */
int fake_net_pending_accepts(uint16_t port);

/* Window-as-grant observability (config_agent MPS3_CFG_AGENT_WINDOWED): what the
 * firmware has handed to mps3_net_recved() on this connection — the cumulative
 * reopened byte count and the number of reopen calls (one per fully-drained
 * window). Lets a test prove the firmware WITHHELD the window until a full window
 * drained and then reopened it exactly one window at a time. */
uint32_t fake_net_recved_total(int client);
int      fake_net_recved_calls(int client);

/* ---- UDP side ------------------------------------------------------------ */

/* Queue one datagram to whatever firmware socket is bound to `dst_port`.
 * Returns 0, or -1 if no socket is bound / queue full. `src_port` is the
 * client's TID the firmware will see (and reply to). */
int fake_net_udp_inject(uint16_t dst_port, uint16_t src_port,
                        const void *buf, int len);

/* Pop the oldest datagram the FIRMWARE sent. Returns its length (truncated
 * to cap), or -1 if none. Outputs which firmware port it left from and
 * which client port it was addressed to (either out-pointer may be NULL). */
int fake_net_udp_take_sent(uint16_t *from_port, uint16_t *to_port,
                           void *buf, int cap);

/* Number of firmware-sent datagrams still queued (diagnostics). */
int fake_net_udp_sent_count(void);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_FAKE_NET_IF_H */
