/*
 * posix_net_if.c — see posix_net_if.h. The third common/net_if.h backend: real
 * POSIX sockets, host-gcc only, every call non-blocking.
 *
 * Linux-flavoured on purpose (MSG_NOSIGNAL, POLLRDHUP): this TU only ever
 * compiles with the host gcc in firmware/test, never for the MicroBlaze.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "../common/net_if.h"
#include "posix_net_if.h"

/* Fail closed rather than silently dropping the pacing: window-as-grant receive
 * flow control (config_agent MPS3_CFG_AGENT_WINDOWED) is expressed over lwIP's
 * TCP_WND, which userspace cannot withhold. A build that DEPENDS on the peer
 * being paced one window at a time must not link this backend and quietly get
 * fire-hose behaviour instead. */
#if defined(MPS3_CFG_AGENT_WINDOWED)
#error "posix_net_if.c does not implement window-as-grant receive flow control (mps3_net_set_manual_window/mps3_net_recved are no-ops here). Link fake_net_if.c for MPS3_CFG_AGENT_WINDOWED builds."
#endif

/* Sized for the WHOLE shell in one process (src/linux_harness/sw/harnessd):
 * nine TCP listeners (6900 6910 6921 2542 6930 6931 6932, + 6920 on a
 * LEGACY_SWD build, + headroom), one client per service plus the transient
 * accept-then-close refusals, and UDP 69 + a TFTP TID + identify 6899. The
 * earlier 4/8/4 fitted the one-service XVC daemon only. Tables are linear-
 * searched and tiny, so the size costs nothing that matters. */
#define POSIX_NET_MAX_LISTENERS 12
#define POSIX_NET_MAX_CONNS     16
#define POSIX_NET_MAX_UDP       8
#define POSIX_NET_MAX_PORTMAP   16
#define POSIX_NET_BACKLOG       4

/* ---- tables --------------------------------------------------------------- */
struct mps3_net_listener {
    int      used;
    int      fd;
    uint16_t logical;  /* the port the firmware asked for */
    uint16_t actual;   /* the port really bound (differs under a port map) */
};

struct mps3_net_conn {
    int used;
    int fd;
    int peer_closed;   /* recv() has already returned 0 (all bytes drained) */
    int errored;       /* a transport error was reported; stays dead        */
    uint16_t logical;  /* the LOGICAL port of the listener it came from     */
};

struct mps3_net_udp {
    int      used;
    int      fd;
    uint16_t logical;
    uint16_t actual;
};

typedef struct {
    int      used;
    uint16_t logical;
    uint16_t actual;
} portmap_t;

static struct mps3_net_listener s_listeners[POSIX_NET_MAX_LISTENERS];
static struct mps3_net_conn     s_conns[POSIX_NET_MAX_CONNS];
static struct mps3_net_udp      s_udp[POSIX_NET_MAX_UDP];
static portmap_t                s_portmap[POSIX_NET_MAX_PORTMAP];
static int                      s_bind_any;
static FILE                    *s_log;
static uint64_t                 s_activity;
static int32_t                  s_port_offset;   /* posix_net_set_port_offset() */

static void logf_(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

static void logf_(const char *fmt, ...)
{
    if (!s_log) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    vfprintf(s_log, fmt, ap);
    va_end(ap);
    fflush(s_log); /* per-record flush: a run log that dies in a pipe buffer is
                    * no evidence at all (host xvc_server.py's --trace-file
                    * lesson) */
}

/* ---- helpers -------------------------------------------------------------- */

static int set_nonblock(int fd)
{
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl < 0) {
        return -1;
    }
    return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

static uint16_t map_port(uint16_t logical)
{
    for (int i = 0; i < POSIX_NET_MAX_PORTMAP; i++) {
        if (s_portmap[i].used && s_portmap[i].logical == logical) {
            return s_portmap[i].actual;
        }
    }
    /* An explicit map wins; otherwise a whole-deployment offset (host tests run
     * every contract port at N+port so they need no privilege for :69 and can
     * run side by side). Port 0 = "ephemeral" and is never offset. */
    if (logical != 0 && s_port_offset != 0) {
        int32_t p = (int32_t)logical + s_port_offset;
        if (p > 0 && p < 65536) {
            return (uint16_t)p;
        }
    }
    return logical;
}

/* Bind `fd` to the (possibly mapped) port and report what was really bound. */
static int bind_and_query(int fd, uint16_t logical, uint16_t *actual_out)
{
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family      = AF_INET;
    sa.sin_addr.s_addr = htonl(s_bind_any ? INADDR_ANY : INADDR_LOOPBACK);
    sa.sin_port        = htons(map_port(logical));
    if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        return -1;
    }
    struct sockaddr_in got;
    socklen_t gl = sizeof(got);
    if (getsockname(fd, (struct sockaddr *)&got, &gl) != 0) {
        return -1;
    }
    *actual_out = ntohs(got.sin_port);
    return 0;
}

/* EAGAIN-class errno = "nothing right now", NOT a dead handle. */
static int errno_is_would_block(int e)
{
    return e == EAGAIN || e == EWOULDBLOCK || e == EINTR;
}

/* ---- lifecycle ------------------------------------------------------------ */

void posix_net_reset(void)
{
    for (int i = 0; i < POSIX_NET_MAX_LISTENERS; i++) {
        if (s_listeners[i].used) {
            close(s_listeners[i].fd);
        }
    }
    for (int i = 0; i < POSIX_NET_MAX_CONNS; i++) {
        if (s_conns[i].used) {
            close(s_conns[i].fd);
        }
    }
    for (int i = 0; i < POSIX_NET_MAX_UDP; i++) {
        if (s_udp[i].used) {
            close(s_udp[i].fd);
        }
    }
    memset(s_listeners, 0, sizeof(s_listeners));
    memset(s_conns, 0, sizeof(s_conns));
    memset(s_udp, 0, sizeof(s_udp));
    memset(s_portmap, 0, sizeof(s_portmap));
    s_bind_any = 0;
    s_activity = 0;
    s_port_offset = 0;
}

void posix_net_map_port(uint16_t logical, uint16_t actual)
{
    for (int i = 0; i < POSIX_NET_MAX_PORTMAP; i++) {
        if (s_portmap[i].used && s_portmap[i].logical == logical) {
            s_portmap[i].actual = actual;
            return;
        }
    }
    for (int i = 0; i < POSIX_NET_MAX_PORTMAP; i++) {
        if (!s_portmap[i].used) {
            s_portmap[i].used    = 1;
            s_portmap[i].logical = logical;
            s_portmap[i].actual  = actual;
            return;
        }
    }
}

uint16_t posix_net_actual_port(uint16_t logical)
{
    for (int i = 0; i < POSIX_NET_MAX_LISTENERS; i++) {
        if (s_listeners[i].used && s_listeners[i].logical == logical) {
            return s_listeners[i].actual;
        }
    }
    for (int i = 0; i < POSIX_NET_MAX_UDP; i++) {
        if (s_udp[i].used && s_udp[i].logical == logical) {
            return s_udp[i].actual;
        }
    }
    return 0;
}

void posix_net_set_bind_any(int any) { s_bind_any = any ? 1 : 0; }
void posix_net_set_log(FILE *f)      { s_log = f; }
uint64_t posix_net_activity(void)    { return s_activity; }

/* ---- TCP ------------------------------------------------------------------ */

mps3_net_listener_t *mps3_net_listen(uint16_t port)
{
    /* Idempotent per LOGICAL port: module _init()s may run more than once, and
     * a second bind() of the same port would fail (or worse, with SO_REUSEPORT,
     * succeed and split the accept queue). */
    for (int i = 0; i < POSIX_NET_MAX_LISTENERS; i++) {
        if (s_listeners[i].used && s_listeners[i].logical == port) {
            return &s_listeners[i];
        }
    }

    int slot = -1;
    for (int i = 0; i < POSIX_NET_MAX_LISTENERS; i++) {
        if (!s_listeners[i].used) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        logf_("[net] listen %u FAILED: listener table full\n", (unsigned)port);
        return 0;
    }

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        logf_("[net] listen %u FAILED: socket: %s\n", (unsigned)port, strerror(errno));
        return 0;
    }
    int on = 1;
    (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));

    uint16_t actual = 0;
    if (bind_and_query(fd, port, &actual) != 0 ||
        listen(fd, POSIX_NET_BACKLOG) != 0 ||
        set_nonblock(fd) != 0) {
        logf_("[net] listen %u FAILED: %s\n", (unsigned)port, strerror(errno));
        close(fd);
        return 0;
    }

    s_listeners[slot].used    = 1;
    s_listeners[slot].fd      = fd;
    s_listeners[slot].logical = port;
    s_listeners[slot].actual  = actual;
    logf_("[net] listening: logical port %u -> %s:%u\n", (unsigned)port,
          s_bind_any ? "0.0.0.0" : "127.0.0.1", (unsigned)actual);
    return &s_listeners[slot];
}

mps3_net_conn_t *mps3_net_accept(mps3_net_listener_t *lst)
{
    if (!lst || !lst->used) {
        return 0;
    }
    int fd = accept(lst->fd, 0, 0);
    if (fd < 0) {
        if (!errno_is_would_block(errno)) {
            logf_("[net] accept on %u: %s\n", (unsigned)lst->actual, strerror(errno));
        }
        return 0;
    }

    int slot = -1;
    for (int i = 0; i < POSIX_NET_MAX_CONNS; i++) {
        if (!s_conns[i].used) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        /* Resource exhaustion: refuse by closing immediately rather than
         * leaving the client hanging on a connection nobody owns. */
        logf_("[net] accept on %u REFUSED: conn table full\n", (unsigned)lst->actual);
        close(fd);
        return 0;
    }

    if (set_nonblock(fd) != 0) {
        close(fd);
        return 0;
    }
    int on = 1;
    /* XVC is strict request/response ping-pong: a 40 ms Nagle delay per reply
     * would dominate the whole session. Transport tuning only — no seam
     * semantics depend on it. */
    (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &on, sizeof(on));

    s_conns[slot].used        = 1;
    s_conns[slot].fd          = fd;
    s_conns[slot].peer_closed = 0;
    s_conns[slot].errored     = 0;
    s_conns[slot].logical     = lst->logical;
    s_activity++;
    logf_("[net] accepted a client on %u (conn slot %d)\n", (unsigned)lst->actual, slot);
    return &s_conns[slot];
}

int mps3_net_recv(mps3_net_conn_t *conn, void *buf, uint32_t cap)
{
    if (!conn || !conn->used || conn->errored) {
        return MPS3_NET_ERR;
    }
    if (conn->peer_closed) {
        /* Already drained AND already reported: stay CLOSED, idempotently. */
        return MPS3_NET_CLOSED;
    }
    if (cap == 0) {
        return 0;
    }

#if defined(MPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN)
    /* TEST-ONLY NEGATIVE CONTROL — never define in a real build.
     *
     * This is the classic backend defect the contract clause exists to forbid:
     * reacting to the FIN *event* (POLLRDHUP) instead of to a drained read(), so
     * bytes the peer sent immediately before closing are thrown away and the
     * caller is told CLOSED. It is invisible in a request/response protocol
     * whose client keeps the socket open, and fatal for one that writes-then-
     * closes. test_posix_net_if_nodrain asserts exactly this damage, which is
     * what gives the positive binary's drain assertion teeth. */
    {
        struct pollfd pfd = { .fd = conn->fd, .events = POLLRDHUP, .revents = 0 };
        if (poll(&pfd, 1, 0) > 0 && (pfd.revents & POLLRDHUP)) {
            conn->peer_closed = 1;
            return MPS3_NET_CLOSED;
        }
    }
#endif

    ssize_t n = recv(conn->fd, buf, (size_t)cap, MSG_DONTWAIT);
    if (n > 0) {
        s_activity += (uint64_t)n;
        return (int)n;
    }
    if (n == 0) {
        /* Orderly shutdown, and recv() only reports it once every buffered byte
         * has already been handed over. THIS is the drain-then-CLOSED clause. */
        conn->peer_closed = 1;
        logf_("[net] peer closed (drained)\n");
        return MPS3_NET_CLOSED;
    }
    if (errno_is_would_block(errno)) {
        return 0;
    }
    conn->errored = 1;
    logf_("[net] recv error: %s\n", strerror(errno));
    return MPS3_NET_ERR;
}

int mps3_net_send(mps3_net_conn_t *conn, const void *buf, uint32_t len)
{
    if (!conn || !conn->used || conn->errored) {
        return MPS3_NET_ERR;
    }
    if (len == 0) {
        return 0;
    }
    /* MSG_NOSIGNAL: a peer that vanished must surface as EPIPE at this call
     * site, not as SIGPIPE killing the whole superloop. */
    ssize_t n = send(conn->fd, buf, (size_t)len, MSG_DONTWAIT | MSG_NOSIGNAL);
    if (n >= 0) {
        s_activity += (uint64_t)n;
        return (int)n; /* may be SHORT — the caller retries the remainder */
    }
    if (errno_is_would_block(errno)) {
        return 0; /* send-buffer backpressure */
    }
    conn->errored = 1;
    logf_("[net] send error: %s\n", strerror(errno));
    return MPS3_NET_ERR;
}

void mps3_net_flush(mps3_net_conn_t *conn)
{
    /* No-op, honestly: unlike lwIP there is no "accepted by the backend but not
     * yet handed to the stack" state here. Whatever mps3_net_send() returned as
     * accepted is already in the kernel's send queue and will be transmitted
     * without further prompting. Safe on NULL, idempotent — so callers that
     * flush every poll (coordinator_net.c, config_agent) still work unmodified. */
    (void)conn;
}

void mps3_net_set_manual_window(mps3_net_conn_t *conn, int manual)
{
    /* Not implementable over a userspace socket — see posix_net_if.h. Requesting
     * manual mode is LOGGED rather than silently accepted, and the #error at the
     * top of this file stops the one build that depends on it from linking. */
    (void)conn;
    if (manual) {
        logf_("[net] WARNING mps3_net_set_manual_window(1) ignored: this backend "
              "has no window-as-grant flow control (posix_net_if.h)\n");
    }
}

void mps3_net_recved(mps3_net_conn_t *conn, uint32_t nbytes)
{
    /* The counterpart no-op: the kernel already reopened the window when
     * mps3_net_recv() consumed the bytes. */
    (void)conn;
    (void)nbytes;
}

void mps3_net_close(mps3_net_conn_t *conn)
{
    if (!conn || !conn->used) {
        return;
    }
    /* Graceful: close() lets the kernel deliver whatever is still queued
     * outbound before the FIN. */
    close(conn->fd);
    conn->used        = 0;
    conn->fd          = -1;
    conn->peer_closed = 0;
    conn->errored     = 0;
    conn->logical     = 0;
}

int mps3_net_peer_closed(mps3_net_conn_t *conn)
{
    if (!conn || !conn->used || conn->errored || conn->peer_closed) {
        return 1; /* released, or recv() already reported the end */
    }
    /* A one-byte PEEK: the kernel's own answer to "what would recv() say",
     * without taking the byte. Bytes pending = a request still to serve (alive,
     * even behind a FIN); 0 = orderly EOF with the buffer drained (sticky: the
     * caller's own recv() still sees it); EAGAIN = connected and silent;
     * anything else (ECONNRESET, ...) = dead. */
    char b;
    ssize_t n = recv(conn->fd, &b, 1, MSG_PEEK | MSG_DONTWAIT);
    if (n > 0) {
        return 0;
    }
    if (n == 0) {
        logf_("[net] probe: peer closed (drained) -- reapable\n");
        return 1;
    }
    if (errno_is_would_block(errno)) {
        return 0;
    }
    /* The kernel reports a socket error ONCE (the peek just consumed it; the
     * next recv() would see a plain EOF). Latch it, so what mps3_net_recv()
     * reports next is still MPS3_NET_ERR -- test_peer_closed_probe pins this. */
    conn->errored = 1;
    logf_("[net] probe: %s -- reapable\n", strerror(errno));
    return 1;
}

/* ---- UDP ------------------------------------------------------------------ */

mps3_net_udp_t *mps3_net_udp_open(uint16_t port)
{
    if (port != 0) {
        for (int i = 0; i < POSIX_NET_MAX_UDP; i++) {
            if (s_udp[i].used && s_udp[i].logical == port) {
                return &s_udp[i]; /* same idempotency rule as mps3_net_listen */
            }
        }
    }

    int slot = -1;
    for (int i = 0; i < POSIX_NET_MAX_UDP; i++) {
        if (!s_udp[i].used) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        return 0;
    }

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        logf_("[net] udp_open %u FAILED: socket: %s\n", (unsigned)port, strerror(errno));
        return 0;
    }
    int on = 1;
    (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));

    uint16_t actual = 0;
    if (bind_and_query(fd, port, &actual) != 0 || set_nonblock(fd) != 0) {
        logf_("[net] udp_open %u FAILED: %s\n", (unsigned)port, strerror(errno));
        close(fd);
        return 0;
    }

    s_udp[slot].used    = 1;
    s_udp[slot].fd      = fd;
    s_udp[slot].logical = port;
    s_udp[slot].actual  = actual;
    logf_("[net] udp bound: logical port %u -> %u\n", (unsigned)port, (unsigned)actual);
    return &s_udp[slot];
}

int mps3_net_udp_recvfrom(mps3_net_udp_t *udp, void *buf, uint32_t cap,
                          mps3_net_addr_t *from)
{
    if (!udp || !udp->used) {
        return MPS3_NET_ERR;
    }
    struct sockaddr_in sa;
    socklen_t sl = sizeof(sa);
    memset(&sa, 0, sizeof(sa));
    /* One WHOLE datagram per call, truncated to cap (the seam's contract; TFTP
     * sizes for 516 so it never truncates in practice). */
    ssize_t n = recvfrom(udp->fd, buf, (size_t)cap, MSG_DONTWAIT | MSG_TRUNC,
                         (struct sockaddr *)&sa, &sl);
    if (n < 0) {
        if (errno_is_would_block(errno)) {
            return 0;
        }
        logf_("[net] udp recvfrom error: %s\n", strerror(errno));
        return MPS3_NET_ERR;
    }
    if ((uint32_t)n > cap) {
        n = (ssize_t)cap; /* MSG_TRUNC reports the real length; we kept `cap` */
    }
    if (from) {
        /* `ip` is opaque at this seam and is only ever echoed back verbatim, so
         * keep the on-wire byte order and convert once in sendto(). */
        from->ip   = (uint32_t)sa.sin_addr.s_addr;
        from->port = ntohs(sa.sin_port);
    }
    s_activity += (uint64_t)n;
    return (int)n;
}

int mps3_net_udp_sendto(mps3_net_udp_t *udp, const void *buf, uint32_t len,
                        const mps3_net_addr_t *to)
{
    if (!udp || !udp->used || !to) {
        return MPS3_NET_ERR;
    }
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family      = AF_INET;
    sa.sin_addr.s_addr = (in_addr_t)to->ip; /* verbatim, as received */
    sa.sin_port        = htons(to->port);
    ssize_t n = sendto(udp->fd, buf, (size_t)len, MSG_DONTWAIT | MSG_NOSIGNAL,
                       (struct sockaddr *)&sa, sizeof(sa));
    if (n < 0 || (uint32_t)n != len) {
        /* A datagram is all-or-nothing: a short sendto is an error, never a
         * partial the caller could retry. */
        logf_("[net] udp sendto error: %s\n", strerror(errno));
        return MPS3_NET_ERR;
    }
    s_activity += (uint64_t)n;
    return (int)n;
}

void mps3_net_udp_close(mps3_net_udp_t *udp)
{
    if (!udp || !udp->used) {
        return;
    }
    close(udp->fd);
    udp->used = 0;
    udp->fd   = -1;
}

/* ---- superloop support (posix_net_if.h "Driving a superloop") -------------- */

void posix_net_set_port_offset(int32_t offset) { s_port_offset = offset; }

int posix_net_wait(uint32_t timeout_ms)
{
    struct pollfd pfd[POSIX_NET_MAX_LISTENERS + POSIX_NET_MAX_CONNS + POSIX_NET_MAX_UDP];
    nfds_t n = 0;

    for (int i = 0; i < POSIX_NET_MAX_LISTENERS; i++) {
        if (s_listeners[i].used) {
            pfd[n].fd = s_listeners[i].fd;
            pfd[n].events = POLLIN;
            pfd[n].revents = 0;
            n++;
        }
    }
    for (int i = 0; i < POSIX_NET_MAX_CONNS; i++) {
        /* A connection that has already reported CLOSED or an error has
         * nothing new to say, and a drained-EOF socket polls READABLE forever:
         * including it would turn every wait into a spin until its owner gets
         * round to closing it. */
        if (s_conns[i].used && !s_conns[i].peer_closed && !s_conns[i].errored) {
            pfd[n].fd = s_conns[i].fd;
            pfd[n].events = POLLIN;
            pfd[n].revents = 0;
            n++;
        }
    }
    for (int i = 0; i < POSIX_NET_MAX_UDP; i++) {
        if (s_udp[i].used) {
            pfd[n].fd = s_udp[i].fd;
            pfd[n].events = POLLIN;
            pfd[n].revents = 0;
            n++;
        }
    }
    int timeout = (timeout_ms > (uint32_t)INT32_MAX) ? INT32_MAX : (int)timeout_ms;
    int rc = poll(pfd, n, timeout);
    if (rc < 0) {
        return (errno == EINTR) ? 0 : -1;
    }
    return rc;
}

unsigned posix_net_conns_on(uint16_t logical_port)
{
    unsigned c = 0;
    for (int i = 0; i < POSIX_NET_MAX_CONNS; i++) {
        if (s_conns[i].used && s_conns[i].logical == logical_port &&
            !s_conns[i].peer_closed && !s_conns[i].errored) {
            c++;
        }
    }
    return c;
}

int posix_net_conn_fd(const mps3_net_conn_t *conn)
{
    return (conn && conn->used) ? conn->fd : -1;
}

/* ---- the peer queries (net_if.h "who is on the other end") -----------------
 * The seam's opaque `ip` is kept in network byte order by recvfrom() above, so
 * a TCP peer is reported the same way and one interpretation serves both. */
int mps3_net_conn_peer(mps3_net_conn_t *conn, mps3_net_addr_t *out)
{
    struct sockaddr_in sa;
    socklen_t sl = sizeof(sa);
    if (!conn || !conn->used || !out) {
        return -1;
    }
    memset(&sa, 0, sizeof(sa));
    if (getpeername(conn->fd, (struct sockaddr *)&sa, &sl) != 0 || sa.sin_family != AF_INET) {
        return -1;
    }
    out->ip   = (uint32_t)sa.sin_addr.s_addr;
    out->port = ntohs(sa.sin_port);
    return 0;
}

int mps3_net_addr_is_local(const mps3_net_addr_t *a)
{
    return a && ((ntohl(a->ip) >> 24) == 127u);   /* 127.0.0.0/8: this host */
}

void mps3_net_addr_text(const mps3_net_addr_t *a, char *out, uint32_t cap)
{
    struct in_addr in;
    if (!out || cap == 0u) {
        return;
    }
    if (!a) {
        snprintf(out, cap, "?");
        return;
    }
    in.s_addr = a->ip;
    snprintf(out, cap, "%s:%u", inet_ntoa(in), (unsigned)a->port);
}

