/*
 * xvc_throughput.c — an XVC 1.0 CLIENT that measures how fast a server shifts
 * bits, in the one unit that makes the firmware and host paths comparable:
 * PAYLOAD BITS PER SECOND OF WALL CLOCK.
 *
 * ===========================================================================
 * WHY THIS EXISTS — the measurement the IICE work is missing
 * ===========================================================================
 * IICE trace readout over the HOST XVC server (host/socket_harness/
 * xvc_server.py driving SWDBB through xsdb -> a remote hw_server -> the
 * MicroBlaze MDM) was MEASURED on silicon at ~340 bit/s: 101 shifts x 2051
 * bits in ~605 s. Every one of the 3n+1 register accesses per JTAG bit is a
 * separate remote round trip, so a 69-bit x 923-sample trace takes 22 minutes
 * and a NIC-400-width trace takes hours.
 *
 * firmware/xvc_server/xvc_server.c built -DMPS3_XVC_TARGET_SWDBB does the same
 * 3n+1 accesses from the MicroBlaze itself, over its own AXI-Lite. The repo
 * claims that is worth roughly 1000x. THAT CLAIM HAS NEVER BEEN MEASURED
 * ANYWHERE — not on a board, and not board-free either.
 *
 * This client closes the board-free half. Pointed at bin/xvc_fw_daemon (the
 * REAL xvc_server.c on a real socket, firmware/test/xvc_fw_daemon.c) it
 * measures the ENGINE'S OWN CEILING: protocol parse + reply staging + the
 * 3n+1 shift loop + one TCP round trip per shift, with the register accesses
 * landing on an in-memory model instead of an AXI bus. That number is an UPPER
 * BOUND on what the MicroBlaze can achieve, and it is a real result: it says
 * whether the ~1000x is even arithmetically available before anyone spends a
 * board window finding out.
 *
 * Pointed at the board (firmware XVC on port 2542 of a running shell) the
 * identical binary produces the identical statistic, so the board number and
 * the loopback number are directly comparable — and both are directly
 * comparable to the 340 bit/s baseline.
 *
 * ===========================================================================
 * WHAT IS TIMED — read this before quoting a number from it
 * ===========================================================================
 * The headline `bits/s` is:
 *
 *     sum of num_bits over all timed shift: commands
 *     ---------------------------------------------------
 *     wall clock from the first timed shift: byte SENT
 *     to the last timed TDO byte RECEIVED
 *
 * That is EXACTLY the accounting of the 340 bit/s baseline (payload bits over
 * wall clock), so the two are comparable without adjustment. Specifically:
 *
 *   INCLUDED: TCP send + server parse + the whole 3n+1 shift loop + reply
 *             send + client receive, for every shift. One TCP round trip per
 *             shift is inside the number, because it is inside the real cost.
 *   INCLUDED: TAP-navigation bits, if you ask for them — `num_bits` is what is
 *             counted, and a real client's num_bits carries its own navigation
 *             (Identify asks for 2053 = 2048 payload + 5 navigation). Use
 *             --bits 2053 to reproduce that shape. There is no attempt to
 *             discount navigation: the baseline did not discount it either.
 *   EXCLUDED: the `getinfo:`/`settck:` handshake, and --warmup shifts. Both
 *             are one-off session costs, not per-bit costs, and the baseline
 *             did not include them.
 *   EXCLUDED: client think-time. This client has none — it pipelines nothing
 *             and computes nothing between shifts, it just sends the next
 *             pre-built vector. A real debugger inserts its own processing
 *             between scans, so a real Identify download will always be SLOWER
 *             than this. This is a ceiling, not a prediction.
 *
 * ===========================================================================
 * THE MEASUREMENT'S OWN CORRECTNESS GATE
 * ===========================================================================
 * A throughput number from a broken path is worse than no number. Two checks
 * run on every shift and abort the run on failure:
 *
 *   1. the TDO reply is EXACTLY ceil(num_bits/8) bytes. A server that
 *      truncated replies would look fast and be wrong; xvc_server.c documents
 *      that it never truncates, so a short reply is a real defect.
 *   2. the reply arrives at all, i.e. the server did not drop the connection.
 *      The pre-fix accept-ceiling defect fails exactly this way at 2053 bits,
 *      so a run at --bits 2053 that completes is also a re-proof of that fix.
 *
 * And the driver script cross-checks the total against the SERVER's own TCK
 * rising-edge counter: bits requested must equal edges clocked, or the bits
 * were accepted and not shifted. See xvc_loopback_throughput.sh.
 *
 * ===========================================================================
 * BUILD / USE
 * ===========================================================================
 *   make -C firmware/test tools          # builds bin/xvc_throughput
 *   firmware/test/xvc_loopback_throughput.sh          # board-free, one command
 *
 *   # against a board's shell firmware (needs the SWDBB-target image):
 *   bin/xvc_throughput --host 192.168.10.101 --port 2542 --bits 2053 --shifts 101
 *
 * Pure POSIX + stdlib. No firmware sources are linked: this is deliberately an
 * INDEPENDENT implementation of the client side, so a shared bug in
 * xvc_server.c cannot cancel itself out of the measurement.
 */
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

/* xvc_server.h's ACCEPT ceiling (4 x the advertised 2048). Carried as a local
 * constant rather than included: this client is deliberately independent of the
 * server's headers, and if the server's ceiling ever moves, this client should
 * report the resulting rejection as a finding rather than silently track it. */
#define CLIENT_MAX_BITS 8192u

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static int write_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    while (len) {
        ssize_t n = send(fd, p, len, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (n == 0) return -1;
        p += (size_t)n;
        len -= (size_t)n;
    }
    return 0;
}

static int read_all(int fd, void *buf, size_t len)
{
    uint8_t *p = (uint8_t *)buf;
    while (len) {
        ssize_t n = recv(fd, p, len, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (n == 0) return -1; /* server closed: fail closed, never report a rate */
        p += (size_t)n;
        len -= (size_t)n;
    }
    return 0;
}

static int connect_to(const char *host, const char *port)
{
    struct addrinfo hints, *res = 0, *ai;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    int rc = getaddrinfo(host, port, &hints, &res);
    if (rc != 0) {
        fprintf(stderr, "xvc_throughput: getaddrinfo(%s:%s): %s\n",
                host, port, gai_strerror(rc));
        return -1;
    }
    int fd = -1;
    for (ai = res; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, ai->ai_addr, ai->ai_addrlen) == 0) break;
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) {
        fprintf(stderr, "xvc_throughput: cannot connect to %s:%s: %s\n",
                host, port, strerror(errno));
        return -1;
    }
    /* Nagle would batch our request with nothing and add 40 ms of nothing to
     * every round trip. hw_server and Identify both disable it; so must we, or
     * the number measures Nagle rather than the server. */
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    return fd;
}

static void put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static int cmp_double(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return (x < y) ? -1 : (x > y) ? 1 : 0;
}

static void usage(const char *a0)
{
    printf(
"usage: %s [options]\n"
"  --host H         server host (default 127.0.0.1)\n"
"  --port P         server port (default 2542)\n"
"  --bits N         num_bits per shift: command (default 2053 = Identify's\n"
"                   real shape, 2048 payload + 5 TAP-navigation bits)\n"
"  --shifts N       timed shifts (default 101 = the silicon baseline's count)\n"
"  --warmup N       untimed shifts first (default 2; excluded from the rate)\n"
"  --settck NS      send settck:<NS> during the handshake (default: skip)\n"
"  --trace S:W      instead of --bits/--shifts, size the run like an IICE\n"
"                   download of S samples x W probe bits: total = S*W bits,\n"
"                   chunked into ceil(total/2048) shifts of <=2048+5 bits\n"
"  --tms-zero       TMS all zero (default): stay in the current TAP state, so\n"
"                   the model shifts every bit -- maximal work per bit\n"
"  --quiet          only the summary table\n"
"  --help\n",
        a0);
}

int main(int argc, char **argv)
{
    const char *host  = "127.0.0.1";
    const char *port  = "2542";
    unsigned bits     = 2053u;
    unsigned shifts   = 101u;
    unsigned warmup   = 2u;
    long     settck   = -1;
    int      quiet    = 0;
    unsigned tr_samples = 0, tr_width = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--help") || !strcmp(a, "-h")) { usage(argv[0]); return 0; }
        else if (!strcmp(a, "--quiet"))    quiet = 1;
        else if (!strcmp(a, "--tms-zero")) { /* the default; accepted for clarity */ }
        else if (!strcmp(a, "--host")   && i + 1 < argc) host   = argv[++i];
        else if (!strcmp(a, "--port")   && i + 1 < argc) port   = argv[++i];
        else if (!strcmp(a, "--bits")   && i + 1 < argc) bits   = (unsigned)strtoul(argv[++i], 0, 0);
        else if (!strcmp(a, "--shifts") && i + 1 < argc) shifts = (unsigned)strtoul(argv[++i], 0, 0);
        else if (!strcmp(a, "--warmup") && i + 1 < argc) warmup = (unsigned)strtoul(argv[++i], 0, 0);
        else if (!strcmp(a, "--settck") && i + 1 < argc) settck = strtol(argv[++i], 0, 0);
        else if (!strcmp(a, "--trace")  && i + 1 < argc) {
            if (sscanf(argv[++i], "%u:%u", &tr_samples, &tr_width) != 2 ||
                tr_samples == 0 || tr_width == 0) {
                fprintf(stderr, "xvc_throughput: --trace wants SAMPLES:WIDTH, both > 0\n");
                return 2;
            }
        } else {
            fprintf(stderr, "xvc_throughput: unknown argument '%s'\n", a);
            usage(argv[0]);
            return 2;
        }
    }

    /* --trace sizes the run the way a real IICE download is sized: the client
     * chunks the scan against the ADVERTISED 2048 and adds its 5 navigation
     * bits per chunk, which is precisely how the 101 x 2051 baseline arose. */
    uint64_t trace_bits = 0;
    if (tr_samples) {
        trace_bits = (uint64_t)tr_samples * (uint64_t)tr_width;
        shifts = (unsigned)((trace_bits + 2047u) / 2048u);
        bits   = 2048u + 5u;
        if (shifts == 0) shifts = 1;
    }

    if (bits == 0 || bits > CLIENT_MAX_BITS) {
        fprintf(stderr, "xvc_throughput: --bits %u outside 1..%u (the server's "
                        "accept ceiling); a larger value is REJECTED by design, "
                        "not slow.\n", bits, CLIENT_MAX_BITS);
        return 2;
    }
    if (shifts == 0) {
        fprintf(stderr, "xvc_throughput: --shifts must be > 0\n");
        return 2;
    }

    int fd = connect_to(host, port);
    if (fd < 0) return 1;

    /* ---- handshake (NOT timed) -------------------------------------------- */
    if (write_all(fd, "getinfo:", 8) != 0) {
        fprintf(stderr, "xvc_throughput: getinfo: send failed\n");
        return 1;
    }
    char info[64];
    memset(info, 0, sizeof(info));
    {   /* the reply is ASCII terminated by '\n', not length-prefixed */
        size_t used = 0;
        while (used + 1 < sizeof(info)) {
            ssize_t n = recv(fd, info + used, 1, 0);
            if (n <= 0) {
                fprintf(stderr, "xvc_throughput: no getinfo: reply (server "
                                "closed?) -- is something else already the one "
                                "client on that port?\n");
                return 1;
            }
            if (info[used++] == '\n') break;
        }
    }
    unsigned advertised = 0;
    {
        const char *c = strchr(info, ':');
        if (c) advertised = (unsigned)strtoul(c + 1, 0, 10);
    }
    if (!quiet) {
        printf("server getinfo   : %s", info);
        printf("advertised bits  : %u   (a CHUNK HINT; num_bits may exceed it)\n",
               advertised);
    }

    if (settck >= 0) {
        uint8_t cmd[7 + 4];
        memcpy(cmd, "settck:", 7);
        put_le32(cmd + 7, (uint32_t)settck);
        uint8_t echo[4];
        if (write_all(fd, cmd, sizeof(cmd)) != 0 ||
            read_all(fd, echo, 4) != 0) {
            fprintf(stderr, "xvc_throughput: settck: round trip failed\n");
            return 1;
        }
        if (!quiet) printf("settck           : %ld ns requested, echoed\n", settck);
    }

    /* ---- build ONE request buffer, reused for every shift ------------------
     * Built once on purpose: per-shift buffer construction is client work, and
     * client work must not land inside a number that is meant to describe the
     * server. TMS = all zero (stay in the current TAP state, so the model
     * shifts every one of the num_bits); TDI = a fixed pseudorandom pattern, so
     * the bits are not all-zero (an all-zero vector could in principle be
     * special-cased by some future fast path, and a constant pattern keeps runs
     * reproducible). */
    uint32_t vec_bytes = (bits + 7u) / 8u;
    size_t   req_len   = 6u + 4u + 2u * (size_t)vec_bytes;
    uint8_t *req = (uint8_t *)calloc(1, req_len);
    uint8_t *tdo = (uint8_t *)calloc(1, vec_bytes);
    if (!req || !tdo) { fprintf(stderr, "xvc_throughput: out of memory\n"); return 1; }
    memcpy(req, "shift:", 6);
    put_le32(req + 6, bits);
    /* req[10 .. 10+vec_bytes)          = TMS, left at zero */
    { /* TDI: xorshift32, seeded fixed */
        uint32_t s = 0x12345678u;
        uint8_t *tdi = req + 10u + vec_bytes;
        for (uint32_t i = 0; i < vec_bytes; i++) {
            s ^= s << 13; s ^= s >> 17; s ^= s << 5;
            tdi[i] = (uint8_t)(s & 0xFFu);
        }
    }

    /* ---- warmup (NOT timed) ----------------------------------------------- */
    for (unsigned i = 0; i < warmup; i++) {
        if (write_all(fd, req, req_len) != 0 || read_all(fd, tdo, vec_bytes) != 0) {
            fprintf(stderr, "xvc_throughput: warmup shift %u failed -- the "
                            "server dropped the connection. At num_bits=%u this "
                            "is what the pre-fix accept ceiling does.\n", i, bits);
            return 1;
        }
    }

    /* ---- the timed run ---------------------------------------------------- */
    double *lat = (double *)calloc(shifts, sizeof(double));
    if (!lat) { fprintf(stderr, "xvc_throughput: out of memory\n"); return 1; }

    double t0 = now_s();
    for (unsigned i = 0; i < shifts; i++) {
        double a = now_s();
        if (write_all(fd, req, req_len) != 0) {
            fprintf(stderr, "xvc_throughput: shift %u send failed\n", i);
            return 1;
        }
        if (read_all(fd, tdo, vec_bytes) != 0) {
            fprintf(stderr, "xvc_throughput: shift %u got no/short TDO -- "
                            "server closed the connection. NO RATE REPORTED.\n", i);
            return 1;
        }
        lat[i] = now_s() - a;
    }
    double t1 = now_s();

    double elapsed = t1 - t0;
    uint64_t total_bits = (uint64_t)bits * (uint64_t)shifts;
    double rate = elapsed > 0.0 ? (double)total_bits / elapsed : 0.0;

    qsort(lat, shifts, sizeof(double), cmp_double);
    double lmin = lat[0], lmax = lat[shifts - 1];
    double lmed = lat[shifts / 2];
    double lsum = 0.0;
    for (unsigned i = 0; i < shifts; i++) lsum += lat[i];

    /* ---- report ----------------------------------------------------------- */
    printf("=== xvc_throughput ================================================\n");
    printf("  endpoint            : %s:%s\n", host, port);
    printf("  num_bits per shift  : %u   (reply %u bytes, verified every shift)\n",
           bits, vec_bytes);
    printf("  timed shifts        : %u   (+%u untimed warmup)\n", shifts, warmup);
    printf("  TOTAL PAYLOAD BITS  : %llu\n", (unsigned long long)total_bits);
    printf("  wall clock          : %.3f s\n", elapsed);
    printf("  ---------------------------------------------------------------\n");
    printf("  THROUGHPUT          : %.1f bit/s\n", rate);
    printf("  vs 340 bit/s host-XVC silicon baseline : %.1fx\n", rate / 340.0);
    printf("  ---------------------------------------------------------------\n");
    printf("  per-shift latency   : min %.3f ms  median %.3f ms  max %.3f ms\n",
           lmin * 1e3, lmed * 1e3, lmax * 1e3);
    printf("  mean per bit        : %.3f us\n", (lsum / (double)shifts) / bits * 1e6);
    if (trace_bits) {
        printf("  ---------------------------------------------------------------\n");
        printf("  IICE trace modelled: %u samples x %u bits = %llu bits\n",
               tr_samples, tr_width, (unsigned long long)trace_bits);
        printf("  download time      : %.3f s   (at 340 bit/s: %.1f s = %.1f min)\n",
               elapsed, (double)trace_bits / 340.0,
               (double)trace_bits / 340.0 / 60.0);
    }
    printf("  TIMED: send->reply for each shift, handshake and warmup EXCLUDED,\n");
    printf("         one TCP round trip per shift INCLUDED, no client think-time.\n");
    printf("===================================================================\n");

    /* Machine-readable line for the driver script's cross-check.
     * The keys are deliberately distinct SUBSTRINGS: an earlier version emitted
     * `bits=` alongside `num_bits=` and a greedy `sed 's/.*bits=\(...\)/'` in the
     * driver silently matched the LAST one, so the cross-check compared 37,017
     * against the correct 842,515 and declared the (correct) server broken.
     * "total_bits" does not contain "num_bits" and vice versa. */
    printf("XVC_TP total_bits=%llu shifts=%u num_bits=%u elapsed_s=%.6f rate_bps=%.3f\n",
           (unsigned long long)total_bits, shifts, bits, elapsed, rate);

    close(fd);
    free(req); free(tdo); free(lat);
    return 0;
}
