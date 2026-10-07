/*
 * mps3-uartbrd — three-stream UARTBR (@0x44A9) <-> TCP byte relay
 * (:6930 UART0 boot monitor / :6931 UART1 app / :6932 SWO trace, RX-only),
 * Linux userspace port of firmware/uart_over_eth/uart_over_eth.c.
 *
 * UARTBR semantics this daemon is written against (frozen, uart_bridge RTL
 * README / shell-regmap v0.5):
 *   - Reads of U0_RX/U1_RX/SWO_RX are DESTRUCTIVE pops: {valid=1,byte} or
 *     {valid=0,0}+no pop when empty. Never "peek" — a popped byte that
 *     can't be forwarded yet is held locally (1-byte rx holdback).
 *     COROLLARY (DRIVER_MATRIX §2.9): exactly ONE reader process per
 *     system, ever — this daemon serves all three ports from one process.
 *   - Host->DUT pushes are DROPPED by hardware when the TX FIFO is full:
 *     poll FIFO_STATUS.tx_full BEFORE each push; hold the byte otherwise
 *     (this relay never knowingly drops).
 *   - SWO is RX-only; its deserialiser needs SWO_CFG {divisor,enable}
 *     programmed once (divisor 24 -> ~2 MBaud at 50 MHz dut_clk; pure
 *     firmware policy, revisit when the DUT TPIU config is pinned).
 *   - Swap gating: STOP touching the registers, KEEP the TCP client
 *     (the bridge FIFOs are not reset by a swap; buffered bytes resume
 *     once ungated). Still notice a client hangup mid-swap.
 *   - One client per stream port; extras accept-then-immediate-close.
 *
 * Backends: UIO node "uartbr" (board) or -m mock (host/QEMU: U0/U1 are
 * loopbacks — the echo-console conformance test; SWO_CFG enable queues a
 * one-shot "SWO-OK\n" banner into the SWO FIFO).
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
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

#define UARTBR_PORT_BASE_DEFAULT 6930   /* UART0; UART1=+1, SWO=+2 */
#define UARTBR_SWO_DIVISOR       24u
#define UARTBR_BYTES_PER_PASS    2048u  /* per stream per direction */
#define UARTBR_POLL_MS           5      /* HW FIFO poll cadence with clients */

typedef struct {
    uint32_t data_off;      /* U0_TXRX / U1_TXRX / SWO_RX */
    uint32_t tx_full_bit;   /* FIFO_STATUS mask; 0 = rx-only stream */
    int      rx_only;
    int      listen_fd;
    int      conn_fd;
    uint8_t  rx_hold;       /* popped-but-unsent DUT byte (destructive reads!) */
    int      rx_hold_valid;
    uint8_t  tx_hold;       /* received-but-unpushed client byte (TX full) */
    int      tx_hold_valid;
} stream_t;

#define NSTREAMS 3
static stream_t    g_str[NSTREAMS];
static auxhw_t     g_uartbr;
static const char *g_gate = MPS3_SWAP_GATE_DEFAULT;

static void stream_drop_client(stream_t *s)
{
    if (s->conn_fd >= 0)
        close(s->conn_fd);
    s->conn_fd = -1;
    /* popped DUT byte dies with its client; a half-pushed client byte
     * still goes to the DUT next ungated pass (bare-metal policy). */
    s->rx_hold_valid = 0;
}

static void relay_step(stream_t *s, int gated)
{
    if (s->conn_fd < 0)
        return;

    if (gated) {
        /* RP decoupled: do NOT touch the FIFO registers; keep the client
         * but notice a hangup (peek for EOF, leave queued bytes alone). */
        uint8_t sink;
        ssize_t n = recv(s->conn_fd, &sink, 1, MSG_PEEK | MSG_DONTWAIT);
        if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK &&
                       errno != EINTR))
            stream_drop_client(s);
        return;
    }

    /* DUT -> client (all three streams). Destructive reads: pop only when
     * the previous byte has been delivered. */
    for (uint32_t budget = UARTBR_BYTES_PER_PASS; budget > 0; budget--) {
        if (!s->rx_hold_valid) {
            uint32_t rd = auxhw_read32(&g_uartbr, s->data_off);
            if (!(rd & UARTBR_VALID))
                break; /* FIFO empty */
            s->rx_hold = (uint8_t)(rd & UARTBR_DATA_MASK);
            s->rx_hold_valid = 1;
        }
        ssize_t n = send(s->conn_fd, &s->rx_hold, 1, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                break; /* backpressure: keep the byte held */
            if (errno == EINTR)
                continue;
            stream_drop_client(s);
            return;
        }
        s->rx_hold_valid = 0;
    }

    if (!s->rx_only) {
        /* client -> DUT. tx_full BEFORE each push — hardware drops pushes
         * while full; holding back keeps the relay lossless. */
        for (uint32_t budget = UARTBR_BYTES_PER_PASS; budget > 0; budget--) {
            if (!s->tx_hold_valid) {
                uint8_t b;
                ssize_t n = recv(s->conn_fd, &b, 1, MSG_DONTWAIT);
                if (n == 0) {
                    stream_drop_client(s);
                    return;
                }
                if (n < 0) {
                    if (errno == EAGAIN || errno == EWOULDBLOCK)
                        break;
                    if (errno == EINTR)
                        continue;
                    stream_drop_client(s);
                    return;
                }
                s->tx_hold = b;
                s->tx_hold_valid = 1;
            }
            uint32_t status = auxhw_read32(&g_uartbr, UARTBR_FIFO_STATUS);
            if (status & s->tx_full_bit)
                break; /* DUT-side FIFO full: hold the byte */
            auxhw_write32(&g_uartbr, s->data_off,
                          (uint32_t)s->tx_hold & UARTBR_DATA_MASK);
            s->tx_hold_valid = 0;
        }
    } else {
        /* SWO clients have nothing to say: drain-and-drop, notice a close. */
        uint8_t sink[16];
        ssize_t n = recv(s->conn_fd, sink, sizeof(sink), MSG_DONTWAIT);
        if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK &&
                       errno != EINTR))
            stream_drop_client(s);
    }
}

static int make_listener(int port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); return -1; }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_ANY);
    sa.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
        perror("bind");
        close(fd);
        return -1;
    }
    if (listen(fd, 4) < 0) { perror("listen"); close(fd); return -1; }
    return fd;
}

static void usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s [-p base_port] [-g gate_file] [-m]\n"
        "  -p port   UART0 port (default %d); UART1=+1, SWO=+2\n"
        "  -g file   swap-gate file (default %s; empty = never gated)\n"
        "  -m        mock UARTBR backend (U0/U1 loopback, SWO banner)\n",
        argv0, UARTBR_PORT_BASE_DEFAULT, MPS3_SWAP_GATE_DEFAULT);
}

int main(int argc, char **argv)
{
    int base = UARTBR_PORT_BASE_DEFAULT;
    int mock = 0;
    int opt;
    while ((opt = getopt(argc, argv, "p:g:mh")) != -1) {
        switch (opt) {
        case 'p': base = atoi(optarg); break;
        case 'g': g_gate = optarg; break;
        case 'm': mock = 1; break;
        default: usage(argv[0]); return 2;
        }
    }
    signal(SIGPIPE, SIG_IGN);

    if (auxhw_open(&g_uartbr, AUXHW_BLK_UARTBR, mock ? NULL : "uartbr") != 0) {
        fprintf(stderr, "mps3-uartbrd: UIO node 'uartbr' absent — refusing\n");
        return 1;
    }

    g_str[0] = (stream_t){ .data_off = UARTBR_U0_TXRX,
                           .tx_full_bit = UARTBR_FIFO_STATUS_U0_TX_FULL,
                           .rx_only = 0, .conn_fd = -1 };
    g_str[1] = (stream_t){ .data_off = UARTBR_U1_TXRX,
                           .tx_full_bit = UARTBR_FIFO_STATUS_U1_TX_FULL,
                           .rx_only = 0, .conn_fd = -1 };
    g_str[2] = (stream_t){ .data_off = UARTBR_SWO_RX,
                           .tx_full_bit = 0,
                           .rx_only = 1, .conn_fd = -1 };
    for (int i = 0; i < NSTREAMS; i++) {
        g_str[i].listen_fd = make_listener(base + i);
        if (g_str[i].listen_fd < 0)
            return 1;
    }

    /* SWO deserialiser up-front: one write (RTL captures the divisor on
     * enable's rising edge — the single write IS the handshake). */
    auxhw_write32(&g_uartbr, UARTBR_SWO_CFG,
                  UARTBR_SWO_DIVISOR | UARTBR_SWO_CFG_ENABLE);

    fprintf(stderr, "mps3-uartbrd: listening on :%d/:%d/:%d (%s backend)\n",
            base, base + 1, base + 2, mock ? "mock" : "uio:uartbr");

    for (;;) {
        int gated = mps3_gate_active(g_gate);

        struct pollfd pfds[NSTREAMS * 2];
        int listen_slot[NSTREAMS];
        int nf = 0;
        int have_conn = 0;
        for (int i = 0; i < NSTREAMS; i++) {
            listen_slot[i] = nf;
            pfds[nf].fd = g_str[i].listen_fd;
            pfds[nf].events = POLLIN;
            nf++;
            if (g_str[i].conn_fd >= 0) {
                have_conn = 1;
                /* Include the conn for wakeup only when its bytes are
                 * consumable now — otherwise a pending byte would spin the
                 * poll loop (gated, or holdback while the TX FIFO is full). */
                if (!gated && !g_str[i].tx_hold_valid) {
                    pfds[nf].fd = g_str[i].conn_fd;
                    pfds[nf].events = POLLIN;
                    nf++;
                }
            }
        }

        /* With any client attached we must poll the DUT-side FIFOs on a
         * timer (the DUT can emit at any time — no fd event for that). */
        int timeout = have_conn ? (gated ? 50 : UARTBR_POLL_MS) : -1;

        int pr = poll(pfds, (nfds_t)nf, timeout);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            return 1;
        }

        /* Service ADOPTED clients before accepting extras (mps3-ctrld's
         * discipline): a client that closed and reconnected inside one
         * poll wake must have its EOF reaped first, or the reconnect is
         * refused as a "second client" it no longer is. */
        for (int i = 0; i < NSTREAMS; i++)
            relay_step(&g_str[i], gated);

        /* accepts: one client per stream; refuse extras (zero bytes). */
        for (int i = 0; i < NSTREAMS; i++) {
            if (pfds[listen_slot[i]].revents & POLLIN) {
                int cfd = accept(g_str[i].listen_fd, NULL, NULL);
                if (cfd >= 0) {
                    if (g_str[i].conn_fd < 0) {
                        int one = 1;
                        setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one,
                                   sizeof(one));
                        fcntl(cfd, F_SETFL,
                              fcntl(cfd, F_GETFL, 0) | O_NONBLOCK);
                        g_str[i].conn_fd = cfd;
                        g_str[i].rx_hold_valid = 0;
                        g_str[i].tx_hold_valid = 0;
                    } else {
                        close(cfd);
                    }
                }
            }
        }

        /* First relay pass for freshly-adopted clients (e.g. the SWO
         * banner should not wait for the next timer tick). */
        for (int i = 0; i < NSTREAMS; i++)
            relay_step(&g_str[i], gated);
    }
}
