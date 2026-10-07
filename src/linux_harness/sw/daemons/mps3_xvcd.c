/*
 * mps3-xvcd — XVC v1.0 server (TCP 2542) -> Debug Bridge (DBGBR @0x44A8),
 * Linux userspace port of firmware/xvc_server/xvc_server.c.
 *
 * FROZEN wire behaviour (SERVICE_DISPOSITION §2/§3.8, DRIVER_MATRIX §2.9),
 * each clause traceable to the bare-metal source:
 *   - "getinfo:"                     -> "xvcServer_v1.0:2048\n" BYTE-EXACT
 *     (max vector 2048 bits — hw_server sizes its shifts off this line).
 *   - "settck:" + u32 LE period      -> the 4 period bytes echoed. Advisory
 *     only: the Debug Bridge has no TCK divider register (same behaviour
 *     as the Xilinx reference server on this IP).
 *   - "shift:" + u32 LE num_bits + tms[] + tdi[] -> tdo[ceil(bits/8)].
 *     Commands are NOT newline-framed — incremental accumulate-and-
 *     reevaluate parsing, never a line assembler.
 *   - Hardware access is the XAPP1251-conformant ORDER per <=32-bit chunk:
 *     LENGTH <- bits, TMS <- word, TDI <- word, CTRL <- GO, poll GO
 *     self-clear (BOUNDED, fail-closed), THEN read TDO.
 *   - Mid-swap gating is STALL-NOT-ERROR for a completed shift: command
 *     stays queued, no DBGBR access, no reply, serviced on first ungated
 *     pass. getinfo/settck still answered while gated. hw_server treats a
 *     short/failed reply as a dead cable; a stalled read just looks slow.
 *   - Single client; extras get accept-then-immediate-close (zero bytes).
 *   - Protocol violation (non-XVC prefix, num_bits 0 or >2048) or a dead
 *     bridge (GO poll bound) drops the client — fail closed, never wedge,
 *     never resync-guess.
 *
 * Gate source: the swap-gate file (auxhw.h) — created by the swap engine
 * owner before decouple, removed after release. Stubbed cross-service
 * seam until the DFX driver/daemon FSM owns it on the board.
 *
 * Backends: UIO node "dbgbr" (board) or -m mock (host/QEMU conformance:
 * TDO = TDI masked to LENGTH — pins chunking + byte packing end-to-end).
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "auxhw.h"

#define XVC_PORT_DEFAULT      2542
#define XVC_MAX_VECTOR_BITS   2048u
#define XVC_MAX_VECTOR_BYTES  (XVC_MAX_VECTOR_BITS / 8u)   /* 256 */
#define XVC_DBGBR_POLL_BOUND  100000
#define XVC_CMD_BUF_MAX       (6u + 4u + 2u * XVC_MAX_VECTOR_BYTES)

#define XVC_PREFIX_GETINFO "getinfo:"
#define XVC_PREFIX_SETTCK  "settck:"
#define XVC_PREFIX_SHIFT   "shift:"

static auxhw_t     g_dbgbr;
static const char *g_gate = MPS3_SWAP_GATE_DEFAULT;

static int      g_listen_fd = -1;
static int      g_conn_fd   = -1;
static uint8_t  g_cmd[XVC_CMD_BUF_MAX];
static uint32_t g_cmd_len;
static uint32_t g_tck_period_ns;   /* advisory, stored + echoed only */
static uint8_t  g_tdo[XVC_MAX_VECTOR_BYTES];

/* ---- DBGBR shift engine (verbatim port) ---------------------------------- */

static uint32_t vector_word(const uint8_t *vec, uint32_t byte_off, uint32_t nbytes)
{
    uint32_t w = 0;
    for (uint32_t i = 0; i < nbytes; i++)
        w |= (uint32_t)vec[byte_off + i] << (8u * i);
    return w;
}

static int do_shift(uint32_t num_bits, const uint8_t *tms, const uint8_t *tdi,
                    uint8_t *tdo)
{
    if (num_bits == 0 || num_bits > XVC_MAX_VECTOR_BITS)
        return -1;

    for (uint32_t done = 0; done < num_bits; done += 32u) {
        uint32_t chunk_bits = num_bits - done;
        if (chunk_bits > 32u)
            chunk_bits = 32u;
        uint32_t byte_off    = done / 8u;   /* 32-bit chunks are byte-aligned */
        uint32_t chunk_bytes = (chunk_bits + 7u) / 8u;

        /* XAPP1251 ORDER — conformance-bound (firmware
         * test_dbgbr_xapp1251_conformance): stage LENGTH/TMS/TDI, kick GO,
         * poll GO self-clear, only then read TDO. */
        auxhw_write32(&g_dbgbr, DBGBR_LENGTH, chunk_bits);
        auxhw_write32(&g_dbgbr, DBGBR_TMS, vector_word(tms, byte_off, chunk_bytes));
        auxhw_write32(&g_dbgbr, DBGBR_TDI, vector_word(tdi, byte_off, chunk_bytes));
        auxhw_write32(&g_dbgbr, DBGBR_CTRL, DBGBR_CTRL_GO);

        int settled = 0;
        for (int i = 0; i < XVC_DBGBR_POLL_BOUND; i++) {
            if (!(auxhw_read32(&g_dbgbr, DBGBR_CTRL) & DBGBR_CTRL_GO)) {
                settled = 1;
                break;
            }
        }
        if (!settled)
            return -1; /* dead/absent bridge — caller fails closed */

        uint32_t w = auxhw_read32(&g_dbgbr, DBGBR_TDO);
        for (uint32_t i = 0; i < chunk_bytes; i++)
            tdo[byte_off + i] = (uint8_t)(w >> (8u * i));
    }
    return 0;
}

/* ---- transport helpers ------------------------------------------------------ */

static void drop_client(void)
{
    if (g_conn_fd >= 0)
        close(g_conn_fd);
    g_conn_fd = -1;
    g_cmd_len = 0;
}

static int send_all(const void *buf, uint32_t len)
{
    const uint8_t *p = buf;
    uint32_t off = 0;
    while (off < len) {
        ssize_t w = send(g_conn_fd, p + off, len - off, MSG_NOSIGNAL);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        off += (uint32_t)w;
    }
    return 0;
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void cmd_consume(uint32_t n)
{
    memmove(g_cmd, g_cmd + n, g_cmd_len - n);
    g_cmd_len -= n;
}

static int prefix_possible(const char *pfx)
{
    uint32_t plen = (uint32_t)strlen(pfx);
    uint32_t n = (g_cmd_len < plen) ? g_cmd_len : plen;
    return memcmp(g_cmd, pfx, n) == 0;
}

static int prefix_complete(const char *pfx)
{
    uint32_t plen = (uint32_t)strlen(pfx);
    return g_cmd_len >= plen && memcmp(g_cmd, pfx, plen) == 0;
}

/* Evaluate accumulated bytes. 1 = one command consumed + replied;
 * 0 = need more bytes (or a shift is stalled while gated); -1 = drop. */
static int try_dispatch(void)
{
    if (g_cmd_len == 0)
        return 0;
    if (!prefix_possible(XVC_PREFIX_GETINFO) &&
        !prefix_possible(XVC_PREFIX_SETTCK) &&
        !prefix_possible(XVC_PREFIX_SHIFT))
        return -1; /* not an XVC command — fail closed */

    if (prefix_complete(XVC_PREFIX_GETINFO)) {
        char info[32];
        int n = snprintf(info, sizeof(info), "xvcServer_v1.0:%u\n",
                         (unsigned)XVC_MAX_VECTOR_BITS);
        cmd_consume((uint32_t)strlen(XVC_PREFIX_GETINFO));
        return send_all(info, (uint32_t)n) < 0 ? -1 : 1;
    }

    if (prefix_complete(XVC_PREFIX_SETTCK)) {
        if (g_cmd_len < 7u + 4u)
            return 0;
        g_tck_period_ns = le32(&g_cmd[7]);
        (void)g_tck_period_ns;
        uint8_t echo[4] = { g_cmd[7], g_cmd[8], g_cmd[9], g_cmd[10] };
        cmd_consume(7u + 4u);
        return send_all(echo, 4u) < 0 ? -1 : 1;
    }

    if (prefix_complete(XVC_PREFIX_SHIFT)) {
        if (g_cmd_len < 6u + 4u)
            return 0;
        uint32_t num_bits = le32(&g_cmd[6]);
        if (num_bits == 0 || num_bits > XVC_MAX_VECTOR_BITS)
            return -1; /* hostile/desynced client */
        uint32_t vec_bytes = (num_bits + 7u) / 8u;
        uint32_t total = 6u + 4u + 2u * vec_bytes;
        if (g_cmd_len < total)
            return 0;

        if (mps3_gate_active(g_gate))
            return 0; /* STALL: leave queued, touch nothing (swap gating) */

        const uint8_t *tms = &g_cmd[10];
        const uint8_t *tdi = &g_cmd[10 + vec_bytes];
        if (do_shift(num_bits, tms, tdi, g_tdo) != 0)
            return -1; /* bridge poll timeout — fail closed */
        cmd_consume(total);
        return send_all(g_tdo, vec_bytes) < 0 ? -1 : 1;
    }

    return 0; /* prefix still ambiguous/incomplete */
}

static void service_buffer(void)
{
    for (;;) {
        int rc = try_dispatch();
        if (rc < 0) {
            drop_client();
            return;
        }
        if (rc == 0)
            return;
    }
}

static void usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s [-p port] [-g gate_file] [-m]\n"
        "  -p port   listen port (default %d)\n"
        "  -g file   swap-gate file (default %s; empty = never gated)\n"
        "  -m        mock DBGBR backend (host/QEMU conformance testing)\n",
        argv0, XVC_PORT_DEFAULT, MPS3_SWAP_GATE_DEFAULT);
}

int main(int argc, char **argv)
{
    int port = XVC_PORT_DEFAULT;
    int mock = 0;
    int opt;
    while ((opt = getopt(argc, argv, "p:g:mh")) != -1) {
        switch (opt) {
        case 'p': port = atoi(optarg); break;
        case 'g': g_gate = optarg; break;
        case 'm': mock = 1; break;
        default: usage(argv[0]); return 2;
        }
    }
    signal(SIGPIPE, SIG_IGN);

    if (auxhw_open(&g_dbgbr, AUXHW_BLK_DBGBR, mock ? NULL : "dbgbr") != 0) {
        fprintf(stderr, "mps3-xvcd: UIO node 'dbgbr' absent — refusing to "
                        "serve a bridge that is not there\n");
        return 1;
    }

    g_listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (g_listen_fd < 0) { perror("socket"); return 1; }
    int one = 1;
    setsockopt(g_listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_ANY);
    sa.sin_port = htons((uint16_t)port);
    if (bind(g_listen_fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
        perror("bind");
        return 1;
    }
    if (listen(g_listen_fd, 4) < 0) { perror("listen"); return 1; }

    fprintf(stderr, "mps3-xvcd: listening on :%d (%s backend)\n",
            port, mock ? "mock" : "uio:dbgbr");

    for (;;) {
        struct pollfd pfds[2];
        int nf = 0;
        pfds[nf].fd = g_listen_fd;
        pfds[nf].events = POLLIN;
        nf++;
        if (g_conn_fd >= 0 && g_cmd_len < XVC_CMD_BUF_MAX) {
            pfds[nf].fd = g_conn_fd;
            pfds[nf].events = POLLIN;
            nf++;
        }

        /* A queued-but-unserviced command (a gated shift, or a full buffer
         * behind one) needs a timer to notice the gate lifting. */
        int timeout = (g_conn_fd >= 0 && g_cmd_len > 0) ? 50 : -1;

        int pr = poll(pfds, (nfds_t)nf, timeout);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            return 1;
        }

        if (nf > 1 && (pfds[1].revents & (POLLIN | POLLHUP | POLLERR))) {
            uint32_t want = XVC_CMD_BUF_MAX - g_cmd_len;
            ssize_t n = recv(g_conn_fd, &g_cmd[g_cmd_len], want, 0);
            if (n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN)) {
                drop_client();
            } else if (n > 0) {
                g_cmd_len += (uint32_t)n;
            }
        }

        if (g_conn_fd >= 0)
            service_buffer(); /* also the gate-lift retry path */

        if (pfds[0].revents & POLLIN) {
            int cfd = accept(g_listen_fd, NULL, NULL);
            if (cfd >= 0) {
                if (g_conn_fd < 0) {
                    g_conn_fd = cfd;
                    g_cmd_len = 0;
                    setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
                } else {
                    close(cfd); /* single client: refuse extras, zero bytes */
                }
            }
        }
    }
}
