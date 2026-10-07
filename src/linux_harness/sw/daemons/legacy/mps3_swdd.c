/*
 * RETIRED 2026-09-11 ([DEV-10]) — NOT BUILT INTO THE IMAGE, NOT INSTALLED,
 * NOT STARTED BY S91mps3aux. Kept as the byte-exact record of the OpenOCD
 * remote_bitbang encoding, which its host tests still pin and which a future
 * mps3-jtagd will inherit. Same disposition as firmware/swd_server/ in the
 * main tree (firmware/platform/Makefile, LEGACY_SWD).
 *
 * WHY IT CANNOT SHIP: the SWD->JTAG cutover replaced swd_bb with jtag_bb at
 * the SAME page with the SAME bit positions. This daemon would map and drive
 * it without error -- DRIVE bit0 swclk lands on tck, bit1 swdio_o on tms,
 * bit2 swdio_oe on tdi -- feeding a JTAG TAP a stream of SWD turnarounds and
 * looking healthy the whole time. It also asks for UIO name "swd-bb", which
 * shell_linux.dts no longer declares, so even if reinstated it fails closed.
 *
 * mps3-swdd — OpenOCD remote_bitbang server (TCP 6920) -> SWDBB @0x44A7
 * (+ CLKRST dbg_resetn for srst), Linux userspace port of
 * firmware/swd_server/swd_server.c.
 *
 * FROZEN wire behaviour (SERVICE_DISPOSITION §2/§3.7, byte mappings
 * CONFIRMED against OpenOCD src/jtag/drivers/remote_bitbang.c — fixed by
 * the HOST driver, not our wiring; I22 SWD half):
 *   'O'/'o'      SWDIO drive/release (SWDBB_DRIVE.SWDIO_OE)
 *   'c'          sample SWDIO -> reply is EXACTLY one ASCII '0'/'1' byte
 *                (any other byte makes OpenOCD log an error and close)
 *   'd'..'g'     'd' + (swclk<<1 | swdio)  — CLK = bit1, DIO = bit0
 *   'r'..'u'     'r' + (trst<<1  | srst)   — srst = bit0; this shell has no
 *                trst (SWD not JTAG), trst correctly ignored. srst drives
 *                CLKRST dbg_resetn (1=released, so assert = CLEAR the bit).
 *                Sense inversion is still a confirm-at-bring-up item.
 *   'B'/'b'      LED on/off — accepted-and-ignored (no LED register)
 *   'Q'          quit — server closes the connection
 *   anything else: drop the connection (fail closed — OpenOCD never sends
 *                these; never silently desync on a corrupt stream)
 *
 * Single client (OpenOCD holds one remote_bitbang connection per session);
 * extras are accept-then-immediate-close, zero bytes. While the swap gate
 * is active the daemon stops CONSUMING bytes entirely (they queue in the
 * kernel socket buffer, serviced once ungated) — the bare-metal
 * swd_gated behaviour.
 *
 * Backends: UIO nodes "swd-bb" + "clkrst" (board) or -m mocks (host/QEMU:
 * SAMPLE reads back the driven level while OE=1, else pull-up '1').
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

#define SWD_PORT_DEFAULT     6920
#define SWD_BYTES_PER_PASS   256u

static auxhw_t     g_swdbb;
static auxhw_t     g_clkrst;
static const char *g_gate = MPS3_SWAP_GATE_DEFAULT;

static int g_listen_fd = -1;
static int g_conn_fd   = -1;

/* mirrored pin state — DRIVE is write-through; writing all three bits every
 * time keeps the mirror authoritative (bare-metal swdbb_drive()). */
static struct {
    uint8_t swclk, swdio_o, swdio_oe;
} g_pins;

static void swdbb_drive(void)
{
    uint32_t v = 0;
    if (g_pins.swclk)    v |= SWDBB_DRIVE_SWCLK;
    if (g_pins.swdio_o)  v |= SWDBB_DRIVE_SWDIO_O;
    if (g_pins.swdio_oe) v |= SWDBB_DRIVE_SWDIO_OE;
    auxhw_write32(&g_swdbb, SWDBB_DRIVE, v);
}

/* Returns 0 = ok, 1 = quit ('Q'), -1 = protocol violation (drop).
 * *reply_out nonzero = one reply byte to send. Verbatim port of
 * swd_server_handle_byte(). */
static int handle_byte(uint8_t c, uint8_t *reply_out)
{
    *reply_out = 0;

    switch (c) {
    case 'O': /* SWDIO drive */
        g_pins.swdio_oe = 1;
        swdbb_drive();
        return 0;
    case 'o': /* SWDIO release */
        g_pins.swdio_oe = 0;
        swdbb_drive();
        return 0;
    case 'c': /* sample — reply MUST be ASCII '0'/'1' exactly */
        *reply_out = (auxhw_read32(&g_swdbb, SWDBB_SAMPLE) & SWDBB_SAMPLE_SWDIO_I)
                         ? '1' : '0';
        return 0;
    case 'd': case 'e': case 'f': case 'g':
        /* CONFIRMED vs OpenOCD: c = 'd' + ((swclk?2:0)|(swdio?1:0)) */
        g_pins.swclk   = (uint8_t)(((c - 'd') >> 1) & 1);
        g_pins.swdio_o = (uint8_t)((c - 'd') & 1);
        swdbb_drive();
        return 0;
    case 'r': case 's': case 't': case 'u': {
        /* CONFIRMED vs OpenOCD: c = 'r' + ((trst?2:0)|(srst?1:0));
         * only srst is meaningful here (SWD, no trst). CLKRST convention
         * is 1=released, so srst asserted = CLEAR dbg_resetn. */
        uint8_t srst = (uint8_t)((c - 'r') & 1);
        if (srst)
            auxhw_clr_bits32(&g_clkrst, CLKRST_RESET_CTRL,
                             CLKRST_RESET_CTRL_DBG_RESETN);
        else
            auxhw_set_bits32(&g_clkrst, CLKRST_RESET_CTRL,
                             CLKRST_RESET_CTRL_DBG_RESETN);
        return 0;
    }
    case 'B': case 'b': /* LED — accepted-but-ignored */
        return 0;
    case 'Q':
        return 1;
    default:
        return -1; /* fail closed on a corrupt stream */
    }
}

static void drop_client(void)
{
    if (g_conn_fd >= 0)
        close(g_conn_fd);
    g_conn_fd = -1;
}

static void usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s [-p port] [-g gate_file] [-m]\n"
        "  -p port   listen port (default %d)\n"
        "  -g file   swap-gate file (default %s; empty = never gated)\n"
        "  -m        mock SWDBB/CLKRST backends (host/QEMU testing)\n",
        argv0, SWD_PORT_DEFAULT, MPS3_SWAP_GATE_DEFAULT);
}

int main(int argc, char **argv)
{
    int port = SWD_PORT_DEFAULT;
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

    if (auxhw_open(&g_swdbb, AUXHW_BLK_SWDBB, mock ? NULL : "swd-bb") != 0) {
        fprintf(stderr, "mps3-swdd: UIO node 'swd-bb' absent — refusing\n");
        return 1;
    }
    if (auxhw_open(&g_clkrst, AUXHW_BLK_CLKRST, mock ? NULL : "clkrst") != 0) {
        fprintf(stderr, "mps3-swdd: UIO node 'clkrst' absent — refusing\n");
        return 1;
    }

    /* Match the RTL's reset drive state (3'b000: SWCLK low, SWDIO released). */
    g_pins.swclk = g_pins.swdio_o = g_pins.swdio_oe = 0;
    swdbb_drive();

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

    fprintf(stderr, "mps3-swdd: listening on :%d (%s backend)\n",
            port, mock ? "mock" : "uio:swd-bb+clkrst");

    for (;;) {
        int gated = (g_conn_fd >= 0) && mps3_gate_active(g_gate);

        struct pollfd pfds[2];
        int nf = 0;
        pfds[nf].fd = g_listen_fd;
        pfds[nf].events = POLLIN;
        nf++;
        if (g_conn_fd >= 0 && !gated) {
            /* while gated we stop consuming entirely — bytes queue in the
             * kernel socket buffer, serviced once ungated */
            pfds[nf].fd = g_conn_fd;
            pfds[nf].events = POLLIN;
            nf++;
        }

        int pr = poll(pfds, (nfds_t)nf, gated ? 50 : -1);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            return 1;
        }

        /* Re-check the gate AFTER the poll wake: it may have been raised
         * while we were blocked with the conn armed — servicing then would
         * wiggle pins mid-swap (the exact hazard swd_gated exists for). */
        if (nf > 1 && (pfds[1].revents & (POLLIN | POLLHUP | POLLERR)) &&
            !mps3_gate_active(g_gate)) {
            uint8_t buf[SWD_BYTES_PER_PASS];
            ssize_t n = recv(g_conn_fd, buf, sizeof(buf), 0);
            if (n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN)) {
                drop_client();
            } else if (n > 0) {
                for (ssize_t i = 0; i < n; i++) {
                    uint8_t reply;
                    int rc = handle_byte(buf[i], &reply);
                    if (rc < 0) {
                        drop_client(); /* protocol violation: fail closed */
                        break;
                    }
                    if (reply != 0) {
                        /* synchronous request/response: the reply byte goes
                         * out before the next command byte is consumed */
                        ssize_t w;
                        do {
                            w = send(g_conn_fd, &reply, 1, MSG_NOSIGNAL);
                        } while (w < 0 && errno == EINTR);
                        if (w != 1) {
                            drop_client();
                            break;
                        }
                    }
                    if (rc == 1) {
                        drop_client(); /* 'Q' quit */
                        break;
                    }
                }
            }
        }

        if (pfds[0].revents & POLLIN) {
            int cfd = accept(g_listen_fd, NULL, NULL);
            if (cfd >= 0) {
                if (g_conn_fd < 0) {
                    g_conn_fd = cfd;
                    setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
                } else {
                    close(cfd); /* single client: refuse extras, zero bytes */
                }
            }
        }
    }
}
