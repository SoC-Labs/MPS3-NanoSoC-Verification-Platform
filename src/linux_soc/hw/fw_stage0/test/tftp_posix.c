/*
 * tftp_posix.c -- stage0's REAL TFTP + identify state machines (stage0_tftp.c,
 * stage0_ident.c) served over host UDP sockets on 127.0.0.1, so the Python
 * client (stage0_push.py) is tested end to end against the code that runs on
 * the board -- not against a Python model of it.
 *
 *   tftp_posix --base PORT [--drop-every N] [--stage-max BYTES] [--timeout-s S]
 *
 * Port mapping (all on 127.0.0.1): logical 69 -> PORT, 6899 -> PORT+1 (so no
 * privileged port is ever bound). stage0 is SINGLE-PORT TFTP: a send from any
 * other logical port is a protocol bug, so the harness exits 4 on one -- the
 * hub's stateful firewall would have dropped it (B1 2026-09-24). --drop-every N
 * drops every Nth datagram the server RECEIVES (the client's retransmission and
 * the server's re-ACK paths both get exercised).
 *
 * Prints "READY <PORT>" once listening, "HANDOFF pc=0x.. hdr_crc=0x.." and
 * exits 0 when an image is accepted; exits 3 after --timeout-s with none.
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../stage0_boot.h"
#include "../stage0_status.h"
#include "../stage0_tftp.h"
#include "../stage0_ident.h"
#include "../stage0_flow.h"

#define NSOCK 32
static struct { int fd; uint16_t logical, actual; } g_s[NSOCK];
static int g_ns, g_base;
static uint8_t *g_ddr;
#define DDR_BASE 0x80000000u
#define DDR_SPAN (72u << 20)

static uint16_t actual_of(uint16_t logical)
{
    if (logical == 69)
        return (uint16_t)g_base;
    if (logical == 6899)
        return (uint16_t)(g_base + 1);
    fprintf(stderr, "tftp_posix: stage0 sent from port %u: not single-port TFTP "
            "(a stateful firewall drops this reply)\n", logical);
    exit(4);
}

static int sock_for(uint16_t logical)
{
    for (int i = 0; i < g_ns; i++)
        if (g_s[i].logical == logical)
            return g_s[i].fd;
    if (g_ns == NSOCK)
        return -1;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = htons(actual_of(logical));
    if (bind(fd, (struct sockaddr *)&a, sizeof a) != 0) {
        fprintf(stderr, "tftp_posix: bind %u: %s\n", actual_of(logical), strerror(errno));
        close(fd);
        return -1;
    }
    g_s[g_ns].fd = fd;
    g_s[g_ns].logical = logical;
    g_s[g_ns].actual = actual_of(logical);
    g_ns++;
    return fd;
}

/* the one platform seam the protocols need */
int s0_udp_send(uint32_t dst_ip, uint16_t sport, uint16_t dport, const void *p, uint32_t len)
{
    int fd = sock_for(sport);
    if (fd < 0)
        return -1;
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(dst_ip);
    a.sin_port = htons(dport);
    return sendto(fd, p, len, 0, (struct sockaddr *)&a, sizeof a) == (ssize_t)len ? 0 : -1;
}

static uint32_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static void *a2p(uint32_t a, uint32_t len, void *c)
{
    (void)c;
    return s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len) ? g_ddr + (a - DDR_BASE) : NULL;
}

static int verify(const uint8_t *img, uint32_t len, struct s0_result *out, void *ctx)
{
    (void)ctx;
    struct s0_mem_src m = { img, len };
    struct s0_backend be = { s0_mem_read, a2p, &m };
    return s0_load(&be, out);
}

static void log_line(void *c, const char *l) { (void)c; fprintf(stderr, "  [stage0] %s\n", l); }

int main(int argc, char **argv)
{
    uint32_t drop_every = 0, stage_max = S0_IMAGE_MAX, timeout_s = 60;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!strcmp(argv[i], "--base")) g_base = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--drop-every")) drop_every = (uint32_t)atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--stage-max")) stage_max = (uint32_t)strtoul(argv[i + 1], 0, 0);
        else if (!strcmp(argv[i], "--timeout-s")) timeout_s = (uint32_t)atoi(argv[i + 1]);
    }
    if (g_base <= 0 || g_base > 60000) {
        fprintf(stderr, "usage: tftp_posix --base PORT [--drop-every N] [--stage-max B] [--timeout-s S]\n");
        return 2;
    }
    static struct s0_status st;
    const struct s0_build_ids ids = { 0x5EED0001u, 0x3F1A560Fu, 0x0B0C0D0Eu };
    s0_status_open(&st, &ids, 0u, 2u);
    st.phase = S0_PH_RESCUE;
    st.rescue_reason = S0_RR_NOCARD;
    uint8_t *stage = malloc(stage_max ? stage_max : 1);
    g_ddr = calloc(1, DDR_SPAN);
    static struct s0_tftp_cfg tc;
    tc.stage = stage; tc.stage_max = stage_max; tc.ddr_ok = 1;
    tc.verify = verify; tc.status = &st; tc.status_len = S0_STATUS_BYTES;
    tc.log = log_line;
    s0_tftp_init(&tc, &st);
    struct s0_ident_cfg ic = { 0x7F000001u, { 2, 0, 0, 0x4D, 0x50, 0x53 }, 0x3F1A560Fu };
    s0_ident_init(&ic, &st);
    if (sock_for(69) < 0 || sock_for(6899) < 0)
        return 2;
    printf("READY %d\n", g_base);
    fflush(stdout);

    uint32_t t_end = now_ms() + timeout_s * 1000u, rx = 0;
    uint8_t buf[70000];
    struct s0_result res;
    for (;;) {
        uint32_t now = now_ms();
        s0_ident_poll(now);
        s0_tftp_poll(now);
        if (s0_tftp_ready(&res)) {
            printf("HANDOFF pc=0x%08X hdr_crc=0x%08X bytes=%u\n", res.entry_pc, res.header_crc32,
                   st.rescue_bytes);
            return 0;
        }
        if ((int32_t)(now - t_end) >= 0) {
            printf("TIMEOUT rescue_state=%u rejects=%u last_rc=%u\n", st.rescue_state,
                   st.rescue_rejects, st.rescue_last_rc);
            return 3;
        }
        struct pollfd pf[NSOCK];
        for (int i = 0; i < g_ns; i++) {
            pf[i].fd = g_s[i].fd;
            pf[i].events = POLLIN;
        }
        if (poll(pf, (nfds_t)g_ns, 5) <= 0)
            continue;
        for (int i = 0; i < g_ns; i++) {
            if (!(pf[i].revents & POLLIN))
                continue;
            struct sockaddr_in src;
            socklen_t sl = sizeof src;
            ssize_t n = recvfrom(pf[i].fd, buf, sizeof buf, 0, (struct sockaddr *)&src, &sl);
            if (n < 0)
                continue;
            if (drop_every && (++rx % drop_every) == 0)
                continue;                              /* simulated loss */
            uint32_t sip = ntohl(src.sin_addr.s_addr);
            uint16_t sp = ntohs(src.sin_port);
            if (g_s[i].logical == 6899)
                s0_ident_udp(sip, sp, buf, (uint32_t)n);
            else
                s0_tftp_udp(sip, sp, g_s[i].logical, buf, (uint32_t)n);
        }
    }
}
