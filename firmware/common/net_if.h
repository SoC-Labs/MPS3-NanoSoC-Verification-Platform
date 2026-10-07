/*
 * net_if.h — the byte-stream SEAM between every network-facing firmware
 * module (coordinator control listener, config_agent, swd_server,
 * uart_over_eth, xvc_server) and whatever actually moves the bytes.
 *
 * W-NET-SEAM (docs/NEXT_WAVE_PLAN.md): the modules' _poll() bodies are
 * written against THIS interface only — no lwIP header is included
 * anywhere outside firmware/platform/ (seam discipline). Three backends
 * exist:
 *
 *   - firmware/test/fake_net_if.c — host-gcc harness backend: in-memory
 *     queues, test-injectable clients/datagrams. This is what most
 *     firmware/test binaries link.
 *   - firmware/test/posix_net_if.c — host-gcc, REAL POSIX sockets. Same
 *     contract, no in-memory shortcut, so a REAL EXTERNAL CLIENT can drive
 *     real firmware protocol code board-free: it is what lets the licensed
 *     Synopsys Identify debugger speak to the real xvc_server.c
 *     (firmware/test/xvc_fw_daemon.c, host/identify/fw_com_check.sh) and
 *     what test_posix_net_if.c / test_xvc_posix_loopback.c regress. It does
 *     NOT implement the manual-window pacing below, and says so with a
 *     compile-time #error rather than a silent no-op.
 *   - firmware/platform/src/net_if_lwip.c — REAL (W-VITIS, 2026-07-07):
 *     the one thin file mapping these calls onto lwIP RAW-API pcbs
 *     (tcp_new/tcp_bind/tcp_listen + accept/recv/err callbacks queueing
 *     delivered pbuf chains per connection — consumption-time tcp_recved
 *     makes TCP_WND the inbound flow control; udp_new/udp_bind + udp_recv
 *     feeding a small per-socket datagram ring; mps3_net_send ->
 *     tcp_write(COPY) + tcp_output, returned count = min(len,
 *     tcp_sndbuf)). Target-only: compiled by firmware/platform/Makefile
 *     against the W-BD XSA's BSP — which is why the seam exists:
 *     everything above this line was finished and tested before lwIP
 *     ever entered the build.
 *
 * Contract (EVERY backend must honor; the fake is the executable spec, and
 * firmware/test/test_posix_net_if.c is where the clauses are asserted against
 * a real kernel socket — clause by clause, with a negative control on the
 * drain-then-CLOSED rule below, which is the one a socket backend is most
 * likely to get subtly wrong):
 *   - Every call is NON-BLOCKING and returns immediately — this is what
 *     lets coordinator_main_loop() stay a strict superloop (see
 *     firmware/README.md "RAW API, single superloop").
 *   - Handles are opaque pointers owned by the backend. NULL = invalid.
 *   - A connection handle stays valid until mps3_net_close() — including
 *     after the peer closes (the local side must still be able to drain
 *     buffered bytes and then observe MPS3_NET_CLOSED).
 */
#ifndef MPS3_NET_IF_H
#define MPS3_NET_IF_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Remote endpoint of a datagram (TFTP needs the peer's TID). `ip` is an
 * opaque 32-bit host identifier as far as this seam is concerned — the
 * lwIP backend will carry an ip4_addr there; the fake carries a small
 * integer. Firmware only ever echoes it back verbatim. */
typedef struct {
    uint32_t ip;
    uint16_t port;
} mps3_net_addr_t;

typedef struct mps3_net_listener mps3_net_listener_t; /* TCP listen socket */
typedef struct mps3_net_conn     mps3_net_conn_t;     /* TCP connection    */
typedef struct mps3_net_udp      mps3_net_udp_t;      /* UDP socket        */

/* mps3_net_recv()/mps3_net_udp_recvfrom() return values < 0: */
enum {
    MPS3_NET_CLOSED = -1, /* peer closed; all buffered bytes already drained */
    MPS3_NET_ERR    = -2, /* transport error — treat the handle as dead, close it */
};

/* ---- TCP-shaped stream surface ------------------------------------------ */

/* Listen on `port`. Returns NULL on resource exhaustion. Calling twice for
 * the same port returns the SAME listener (module _init()s may run more
 * than once in the host harness; the lwIP backend gets this for free by
 * keeping one pcb per port). */
mps3_net_listener_t *mps3_net_listen(uint16_t port);

/* One pending connection, or NULL. The caller owns the returned handle
 * (must eventually mps3_net_close() it — including when refusing a second
 * client on a single-client service, which is close-immediately). */
mps3_net_conn_t *mps3_net_accept(mps3_net_listener_t *lst);

/* Up to `cap` buffered bytes into `buf`. Returns the byte count (0 = no
 * data right now), MPS3_NET_CLOSED once the peer has closed AND every
 * buffered byte has been consumed, or MPS3_NET_ERR. */
int mps3_net_recv(mps3_net_conn_t *conn, void *buf, uint32_t cap);

/* Queue up to `len` bytes for transmission. Returns how many bytes the
 * backend accepted (0..len — short on send-buffer backpressure; the caller
 * must retry the remainder on a later poll), or MPS3_NET_ERR. */
int mps3_net_send(mps3_net_conn_t *conn, const void *buf, uint32_t len);

/* Best-effort re-drive of the connection's output: flush any bytes already
 * accepted by mps3_net_send() but still queued in the backend (lwIP unsent).
 * A no-op when nothing is pending, and safe on NULL. Idempotent — call it every
 * poll while output may be pending (e.g. a deferred TCP window-update ACK under
 * window-as-grant flow control). */
void mps3_net_flush(mps3_net_conn_t *conn);

/* ---- window-as-grant receive flow control (config_agent MPS3_CFG_AGENT_WINDOWED)
 *
 * By DEFAULT mps3_net_recv() reopens the TCP receive window at consumption time
 * (fire-hose flow control: the window tracks the consumer, bounding buffering by
 * construction). A caller that wants to pace the PEER by exactly one window at a
 * time (window-as-grant) instead switches a connection to MANUAL-window mode:
 * mps3_net_recv() then consumes bytes off the chain WITHOUT reopening the window,
 * and the caller reopens it explicitly, in window-sized batches, once it has
 * DRAINED those bytes to its sink — via mps3_net_recved(). Per-connection,
 * default off, so every other connection (and the whole non-windowed build) is
 * unaffected. */

/* Put `conn` into manual receive-window mode (manual != 0) or back to the
 * default consumption-time-ack mode (manual == 0). Safe on NULL / not-in-use. */
void mps3_net_set_manual_window(mps3_net_conn_t *conn, int manual);

/* Reopen the receive window by `nbytes` (announce that this many
 * previously-consumed bytes have drained, so the peer may send that many more).
 * The manual-window counterpart to the auto-ack mps3_net_recv() does by default.
 * Safe on NULL / closed; a no-op when nbytes == 0. */
void mps3_net_recved(mps3_net_conn_t *conn, uint32_t nbytes);

/* Release the handle (graceful close; buffered outbound bytes are still
 * delivered by the backend where possible). Safe on NULL. */
void mps3_net_close(mps3_net_conn_t *conn);

/* Is this connection's peer already GONE? (2026-09-28, reap-before-refuse.)
 *
 * 1 = the peer has closed (FIN) or the transport has errored, AND no unread
 *     input is buffered -- exactly "the next mps3_net_recv() would return
 *     MPS3_NET_CLOSED or MPS3_NET_ERR". A NULL / released handle is also 1.
 * 0 = alive; OR the peer closed but bytes it sent are still unread (they are a
 *     request the server has yet to serve, so it is not dead yet); OR the
 *     backend cannot tell.
 * A query: it consumes no byte and leaves what the next mps3_net_recv() reports
 * unchanged, so a caller may ask and then carry on with the connection exactly
 * as before.
 *
 * WHY. Every single-client service (6900, 6921, 2542, the consoles) accepts
 * FIRST in its poll and only reads the current client later in the same pass,
 * so a client that closed and at once reconnected found its OLD connection still
 * registered and was refused -- every time (the 6900 soak RST, Harness Manager's
 * back-to-back requests). The services now ask this before refusing: a dead
 * current client is dropped and the newcomer adopted; a live one still wins.
 *
 * net_if.c carries a WEAK default returning 0 ("presumed alive" = the old
 * refuse-while-occupied behaviour) for any backend that cannot tell; all three
 * backends in the tree answer for real. */
int mps3_net_peer_closed(mps3_net_conn_t *conn);

/* ---- UDP-shaped datagram surface (TFTP) ---------------------------------- */

/* Bind a UDP socket. port==0 = backend-assigned ephemeral port (a TFTP
 * transfer TID, RFC1350). Same idempotency rule as mps3_net_listen() for
 * a nonzero port. NULL on exhaustion. */
mps3_net_udp_t *mps3_net_udp_open(uint16_t port);

/* One whole queued datagram (truncated to `cap` if oversized — TFTP's are
 * bounded at 516 so callers just size for that). Returns its length, 0 if
 * none pending, or MPS3_NET_ERR. *from (may be NULL) receives the sender. */
int mps3_net_udp_recvfrom(mps3_net_udp_t *udp, void *buf, uint32_t cap,
                          mps3_net_addr_t *from);

/* Send one datagram. Returns len, or MPS3_NET_ERR. */
int mps3_net_udp_sendto(mps3_net_udp_t *udp, const void *buf, uint32_t len,
                        const mps3_net_addr_t *to);

void mps3_net_udp_close(mps3_net_udp_t *udp);

/* ---- who is on the other end (v0.14, the Linux harness's slot lock) ---------
 *
 * The Linux engine refuses slot MUTATIONS from a non-local peer once its SSH is
 * claimed (net-protocol.md "Slot images", "The lock"), so it must ask where a
 * connection or datagram came from. Three queries, all in the seam's own opaque
 * address form (the one mps3_net_udp_recvfrom() fills):
 *   mps3_net_conn_peer()      the remote end of a TCP connection: 0, or <0 unknown
 *   mps3_net_addr_is_local()  1 = this host (loopback), 0 = anything else OR unknown
 *   mps3_net_addr_text()      dotted text for a log line ("?" when unknown)
 * net_if.c carries WEAK defaults (unknown / not local / "?"), so a backend that
 * never answers -- lwIP, the fake -- fails CLOSED, and no service module calls
 * them: bare metal declines every slot mutation before a peer matters.
 * posix_net_if.c (the Linux engine's backend) answers for real. */
int  mps3_net_conn_peer(mps3_net_conn_t *conn, mps3_net_addr_t *out);
int  mps3_net_addr_is_local(const mps3_net_addr_t *a);
void mps3_net_addr_text(const mps3_net_addr_t *a, char *out, uint32_t cap);

/* Refuse a just-accepted connection WITH A REASON (2026-09-26, the Linux
 * harness's claim lock on 2542 / 6921): drain what the peer already sent, send
 * `line` once (best effort, one attempt), drain again, close. The drains matter:
 * a close() over unread input is an RST, which can discard the line before the
 * peer reads it. Backend-independent (net_if.c); nothing on bare metal calls it. */
void mps3_net_refuse_with_line(mps3_net_conn_t *conn, const char *line);

/* ==========================================================================
 * Backend-independent helpers (net_if.c — pure logic, host-tested in
 * firmware/test/test_net_linebuf.c). Used by the coordinator's 6900
 * listener for JSON-lines framing.
 * ========================================================================== */

/* Max control line the firmware will assemble. Sized comfortably above the
 * largest contract request (`swap` ~= 45 bytes) — a longer line is a
 * hostile/broken client and is rejected as one oversized line, never
 * silently split into two. Firmware-side bound, not a protocol limit. */
#define MPS3_NET_LINE_MAX 256

typedef struct {
    char buf[MPS3_NET_LINE_MAX];
    int  len;       /* bytes currently assembled (excludes the '\n')      */
    int  overflow;  /* mid-line overflow: discarding until the next '\n' */
    int  done;      /* internal: last feed() completed a line; the next
                     * feed() starts a fresh one (buf/len stay readable
                     * until then — single-threaded superloop) */
} mps3_net_linebuf_t;

void mps3_net_linebuf_reset(mps3_net_linebuf_t *lb);

/* Feed ONE byte. Returns:
 *   0  — no complete line yet
 *   1  — a complete line is ready in lb->buf/lb->len (trailing '\r'
 *        stripped, NOT NUL-terminated; stays readable until the next
 *        feed(), which then starts a fresh line)
 *  -1  — a line terminated that had overflowed MPS3_NET_LINE_MAX (its
 *        content was discarded; caller should answer with an error line).
 * Fail-closed by construction: an overlong line can never be parsed as
 * two shorter ones. */
int mps3_net_linebuf_feed(mps3_net_linebuf_t *lb, char c);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_NET_IF_H */
