/*
 * test_jtag_batch.c -- 6921 under harnessd (jtag_linux.c) against jtag_server.c's own
 * loop (jtag_server_poll), over the SAME streams: the wire protocol must not change.
 *
 * WHAT IS PROVEN (each case runs three engines on one input and compares them):
 *   REF      jtag_server_poll()                    -- the unmodified firmware loop
 *   BATCHED  harnessd_jtag_poll(), --jtag-io batched (the default)
 *   PERBYTE  harnessd_jtag_poll(), --jtag-io per-byte (the fallback)
 * For every case: the reply bytes the client reads are IDENTICAL, the JTAGBB/CLKRST
 * register accesses (reads and writes, offset and value) are IDENTICAL and in the
 * same order, and whether the server closed the connection is the same. Streams:
 * OpenOCD-shaped scans (bitbang.c's scan loop: an IR scan + a 35-bit DR scan per
 * DAP access, the IDCODE read), random valid streams at random chunkings, 'Q' and a
 * byte outside the protocol mid-chunk, reset chars (srst -> CLKRST.dbg_resetn), send
 * back-pressure (1 and 3 bytes a send), and a swap's gate (nothing consumed while
 * g_shell_state.swd_gated). The IDCODE read through the fake TAP must be 0x6BA00477.
 *
 * THE COST MODEL (printed; the structural counts are asserted, the times are an
 * estimate with named constants). mps3_net_recv/send are wrapped (-Wl,--wrap) and
 * counted: REF pays one recv per command byte and one send (= one TCP segment = one
 * SSH packet over the claim's forward) per sample; BATCHED pays one recv and one send
 * per scan. With the MBV costs below, REF's per-scan cost on the claim path is of the
 * order of the ~12 ms round trip measured on silicon (30 Sep), and BATCHED's is a small
 * fraction of it; the remainder is the network + host path, which this cannot touch.
 *
 * Links: jtag_linux.c, firmware jtag_server.c, net_if.c, fake_net_if.c, mock_regs.c,
 * fake_jtag_tap.c. harnessd's own objects are not needed: g_hd is defined here.
 */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../../../../firmware/common/net_if.h"
#include "../../../../../firmware/common/platform_regs.h"
#include "../../../../../firmware/coordinator/coordinator.h"
#include "../../../../../firmware/jtag_server/jtag_server.h"
#include "../../../../../firmware/test/fake_jtag_tap.h"
#include "../../../../../firmware/test/fake_net_if.h"
#include "../../../../../firmware/test/mock_regs.h"
#include "../harnessd.h"

harnessd_cfg_t g_hd;
mps3_shell_state_t g_shell_state;

static int s_checks;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); \
                                  exit(1); } s_checks++; } while (0)

/* ---- counting the syscall-shaped calls (-Wl,--wrap=mps3_net_recv,--wrap=mps3_net_send) */
int __real_mps3_net_recv(mps3_net_conn_t *conn, void *buf, uint32_t cap);
int __real_mps3_net_send(mps3_net_conn_t *conn, const void *buf, uint32_t len);
static unsigned long s_recvs, s_recvs_data, s_sends, s_segments;
int __wrap_mps3_net_recv(mps3_net_conn_t *conn, void *buf, uint32_t cap)
{
    s_recvs++;
    int n = __real_mps3_net_recv(conn, buf, cap);
    if (n > 0) {
        s_recvs_data++;
    }
    return n;
}
int __wrap_mps3_net_send(mps3_net_conn_t *conn, const void *buf, uint32_t len)
{
    s_sends++;
    int n = __real_mps3_net_send(conn, buf, len);
    if (n > 0) {
        s_segments++;
    }
    return n;
}

/* ---- the fabric: JTAGBB behind the fake TAP, CLKRST as a plain register, all traced */
#define TRACE_MAX 400000
typedef struct { uint8_t w; uint32_t base, off, val; } acc_t;
static acc_t *s_trace;
static unsigned s_ntrace;
static fake_swdbb_t s_bb;
static int s_gate_checked;
static uint32_t s_clkrst_reset_ctrl;

static void trace(int w, uint32_t base, uint32_t off, uint32_t val)
{
    if (s_ntrace < TRACE_MAX) {
        s_trace[s_ntrace].w = (uint8_t)w;
        s_trace[s_ntrace].base = base;
        s_trace[s_ntrace].off = off;
        s_trace[s_ntrace].val = val;
    }
    s_ntrace++;
}

static int jtagbb_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    int rc = fake_swdbb_hook(ctx, is_write, base, off, val);
    trace(is_write, base, off, *val);
    return rc;
}

static int clkrst_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx;
    uint32_t *r = off == CLKRST_RESET_CTRL ? &s_clkrst_reset_ctrl : 0;
    if (is_write) {
        if (r) *r = *val;
    } else {
        *val = r ? *r : 0u;
    }
    trace(is_write, base, off, *val);
    return 1;
}

/* ---- one engine run ---------------------------------------------------------- */
enum { REF = 0, BATCHED, PERBYTE, NENGINES };
static const char *const k_engine[] = { "REF (jtag_server_poll)", "BATCHED", "PERBYTE" };

typedef struct {
    uint8_t *reply;
    unsigned nreply;
    acc_t   *trace;
    unsigned ntrace;
    int      closed;
    unsigned long recvs, recvs_data, sends, segments, polls;
} result_t;

static void poll_engine(int e)
{
    if (e == REF) {
        jtag_server_poll();
    } else {
        g_hd.jtag_per_byte = (e == PERBYTE);
        harnessd_jtag_poll();
    }
}

/* Feed `n` bytes of `s` in chunks from `chunks` (cycled; 0 = end), polling `polls`
 * times after each chunk, then until quiet. gate_first: hold swd_gated for the first
 * 20 polls (nothing may be consumed), then release. send_limit: fake_net_set_send_limit. */
static void run(int e, const uint8_t *s, unsigned n, const unsigned *chunks, int send_limit,
                int gate_first, result_t *r)
{
    mock_regs_reset();
    fake_net_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    fake_tap_reset(&s_bb.tap, 0x6BA00477u);
    s_bb.drive = 0u;
    s_clkrst_reset_ctrl = CLKRST_RESET_CTRL_DBG_RESETN;
    mock_regs_set_hook(MPS3_JTAGBB_BASE, jtagbb_hook, &s_bb);
    mock_regs_set_hook(MPS3_CLKRST_BASE, clkrst_hook, 0);
    harnessd_jtag_reset();
    jtag_server_init();
    s_ntrace = 0;
    s_recvs = s_recvs_data = s_sends = s_segments = 0;
    int cli = fake_net_connect(MPS3_PORT_JTAG);
    CHECK(cli >= 0);
    if (send_limit > 0) {
        poll_engine(e);                       /* accept first, then cap the connection */
        fake_net_set_send_limit(cli, send_limit);
    }
    r->reply = malloc(n + 16);
    r->nreply = 0;
    r->polls = 0;
    unsigned off = 0, ci = 0;
    if (gate_first) {
        g_shell_state.swd_gated = true;
    }
    while (off < n) {
        unsigned c = chunks[ci] ? chunks[ci] : n;
        ci = chunks[ci] ? ci + 1 : 0;
        if (!chunks[ci]) {
            ci = 0;
        }
        if (c > n - off) {
            c = n - off;
        }
        int q = fake_net_send(cli, s + off, (int)c);
        CHECK(q >= 0);
        off += (unsigned)q;
        for (int k = 0; k < 3; k++) {
            poll_engine(e);
            r->polls++;
            int got = fake_net_recv(cli, r->reply + r->nreply, (int)(n + 16 - r->nreply));
            if (got > 0) {
                r->nreply += (unsigned)got;
            }
        }
        if (gate_first && r->polls >= 20) {
            if (g_shell_state.swd_gated) {
                CHECK(s_ntrace == 0);         /* the gate held: no pin was touched */
                CHECK(r->nreply == 0);        /* ...and nothing was answered */
                s_gate_checked++;
            }
            g_shell_state.swd_gated = false;
        }
    }
    g_shell_state.swd_gated = false;
    for (int quiet = 0, k = 0; quiet < 50 && k < 2000000; k++) {
        unsigned before = r->nreply, tb = s_ntrace;
        poll_engine(e);
        r->polls++;
        int got = fake_net_recv(cli, r->reply + r->nreply, (int)(n + 16 - r->nreply));
        if (got > 0) {
            r->nreply += (unsigned)got;
        }
        quiet = (r->nreply == before && s_ntrace == tb) ? quiet + 1 : 0;
    }
    r->closed = fake_net_fw_closed(cli);
    r->ntrace = s_ntrace < TRACE_MAX ? s_ntrace : TRACE_MAX;
    r->trace = malloc(sizeof(acc_t) * (r->ntrace + 1u));
    memcpy(r->trace, s_trace, sizeof(acc_t) * r->ntrace);
    CHECK(s_ntrace < TRACE_MAX);
    r->recvs = s_recvs;
    r->recvs_data = s_recvs_data;
    r->sends = s_sends;
    r->segments = s_segments;
    mock_regs_set_hook(MPS3_JTAGBB_BASE, 0, 0);
    mock_regs_set_hook(MPS3_CLKRST_BASE, 0, 0);
}

static void free_result(result_t *r)
{
    free(r->reply);
    free(r->trace);
}

/* The three engines over one input: identical replies, accesses and close. */
static void same_everywhere(const char *name, const uint8_t *s, unsigned n, const unsigned *chunks,
                            int send_limit, int gate_first, result_t out[NENGINES])
{
    for (int e = 0; e < NENGINES; e++) {
        run(e, s, n, chunks, send_limit, gate_first, &out[e]);
    }
    for (int e = 1; e < NENGINES; e++) {
        if (out[e].nreply != out[REF].nreply ||
            memcmp(out[e].reply, out[REF].reply, out[REF].nreply) != 0) {
            fprintf(stderr, "FAIL %s: %s replies differ from REF (%u vs %u bytes)\n", name,
                    k_engine[e], out[e].nreply, out[REF].nreply);
            exit(1);
        }
        if (out[e].ntrace != out[REF].ntrace ||
            memcmp(out[e].trace, out[REF].trace, sizeof(acc_t) * out[REF].ntrace) != 0) {
            unsigned i = 0;
            while (i < out[e].ntrace && i < out[REF].ntrace &&
                   memcmp(&out[e].trace[i], &out[REF].trace[i], sizeof(acc_t)) == 0) {
                i++;
            }
            fprintf(stderr, "FAIL %s: %s register accesses differ from REF at #%u (%u vs %u)\n",
                    name, k_engine[e], i, out[e].ntrace, out[REF].ntrace);
            exit(1);
        }
        CHECK(out[e].closed == out[REF].closed);
        s_checks += 3;
    }
    printf("  ok  %-46s %6u B in, %5u replies, %7u reg accesses, closed=%d\n", name, n,
           out[REF].nreply, out[REF].ntrace, out[REF].closed);
}

/* ---- OpenOCD's bitbang.c, as bytes -------------------------------------------- */
typedef struct { uint8_t *b; unsigned n, cap; unsigned samples; } stream_t;

static void put(stream_t *s, uint8_t c)
{
    if (s->n == s->cap) {
        s->cap = s->cap ? 2u * s->cap : 4096u;
        s->b = realloc(s->b, s->cap);
    }
    s->b[s->n++] = c;
}
static void w(stream_t *s, int tck, int tms, int tdi)
{
    put(s, (uint8_t)('0' + ((tck ? 4 : 0) | (tms ? 2 : 0) | (tdi ? 1 : 0))));
}
static void tms_bits(stream_t *s, unsigned bits, unsigned n)   /* bitbang_state_move */
{
    for (unsigned i = 0; i < n; i++) {
        int tms = (bits >> i) & 1;
        w(s, 0, tms, 0);
        w(s, 1, tms, 0);
    }
    w(s, 0, (bits >> (n - 1u)) & 1, 0);
}
/* bitbang_scan: from Shift-xR, n bits, TMS on the last; every bit sampled */
static void scan(stream_t *s, uint64_t out, unsigned n)
{
    for (unsigned i = 0; i < n; i++) {
        int tms = (i == n - 1u);
        int tdi = (int)((out >> i) & 1u);
        w(s, 0, tms, tdi);
        put(s, 'R');
        s->samples++;
        w(s, 1, tms, tdi);
    }
    w(s, 0, 1, 0);
}
/* One JTAG-DP access as adi_v5_jtag.c makes it: IR (4 bits) then DR (35 bits),
 * each from Run-Test/Idle and back (Select-xR, Capture, Shift ... Exit1, Update, Idle). */
static void dap_access(stream_t *s, unsigned ir, uint64_t dr35)
{
    tms_bits(s, 0x3u, 4);             /* RTI -> Select-DR -> Select-IR -> Capture-IR -> Shift-IR */
    scan(s, ir, 4);
    tms_bits(s, 0x1u, 2);             /* Exit1-IR -> Update-IR -> RTI */
    tms_bits(s, 0x1u, 3);             /* RTI -> Select-DR -> Capture-DR -> Shift-DR */
    scan(s, dr35, 35);
    tms_bits(s, 0x1u, 2);             /* Exit1-DR -> Update-DR -> RTI */
}
static void idcode_read(stream_t *s)  /* TLR (selects IDCODE), then a 32-bit DR scan */
{
    tms_bits(s, 0x1Fu, 5);            /* 5 x TMS=1: Test-Logic-Reset */
    tms_bits(s, 0x2u, 4);             /* TLR -> RTI -> Select-DR -> Capture-DR -> Shift-DR */
    scan(s, 0, 32);
    tms_bits(s, 0x1u, 2);
}
static uint32_t idcode_of(const uint8_t *rep, unsigned n)
{
    uint32_t v = 0;
    for (unsigned i = 0; i < 32 && i < n; i++) {
        v |= (uint32_t)(rep[i] == '1') << i;
    }
    return v;
}

static uint32_t s_rng = 0x2545F491u;
static uint32_t rnd(void)
{
    s_rng ^= s_rng << 13;
    s_rng ^= s_rng >> 17;
    s_rng ^= s_rng << 5;
    return s_rng;
}

/* ---- the cost model ----------------------------------------------------------- */
/* MBV costs (100 MHz MicroBlaze V, one hart), the project's own "central" model where
 * it has one (tests/test_clcd_speed.c: a trivial syscall 8 us, an AXI write 80 ns,
 * a read 250 ns) and ESTIMATES where it has not (marked E). */
#define US_RECV        20.0    /* E: recv(2) on a TCP socket, incl. the poll-driven wakeup   */
#define US_SEND        40.0    /* E: send(2), one TCP segment out (loopback or eth)          */
#define US_BYTE        0.6     /* E: jtag_server_handle_byte + one AXI access                */
#define US_SSH_SEG     120.0   /* E: dropbear per forwarded segment: read, chacha20-poly1305,
                                *    write -- the claim path's cost per reply segment       */
#define MS_HOST_RTT_MEASURED 12.0   /* silicon, 30 Sep: the claim-forward round trip */

/* per access: every recv that returned bytes, plus the one that finds the socket
 * empty and ends the poll; every send (one segment each) */
static double server_us(const result_t *r, unsigned scans)
{
    return ((double)(r->recvs_data + scans) * US_RECV + (double)r->segments * US_SEND) / scans;
}

int main(void)
{
    s_trace = malloc(sizeof(acc_t) * TRACE_MAX);
    result_t R[NENGINES];
    static const unsigned whole[] = { 0 };

    printf("== the wire: three engines, one stream each ==\n");

    /* 1. the IDCODE read, OpenOCD-shaped, in one burst */
    stream_t s = { 0 };
    idcode_read(&s);
    same_everywhere("IDCODE read (one burst)", s.b, s.n, whole, 0, 0, R);
    CHECK(R[REF].nreply == 32 && idcode_of(R[REF].reply, 32) == 0x6BA00477u);
    for (int e = 0; e < NENGINES; e++) free_result(&R[e]);

    /* 2. 50 DAP accesses, each its own burst (OpenOCD flushes per scan) */
    stream_t d = { 0 };
    unsigned bursts[64], nb = 0;
    for (int k = 0; k < 50; k++) {
        unsigned before = d.n;
        dap_access(&d, 0xA + (k & 1), ((uint64_t)rnd() << 3) | (k & 7));
        if (nb < 63) bursts[nb++] = d.n - before;
    }
    bursts[nb] = 0;
    same_everywhere("50 DAP accesses (IR 4 + DR 35), a burst each", d.b, d.n, bursts, 0, 0, R);
    unsigned per_scan_bytes = d.n / 50, per_scan_samples = d.samples / 50;
    result_t ref_scan = R[REF], bat_scan = R[BATCHED];

    /* the structural counts: REF one recv per byte + one segment per sample; BATCHED one
     * recv with data and one segment per burst */
    CHECK(R[REF].recvs_data == d.n);
    CHECK(R[REF].segments == d.samples);
    CHECK(R[BATCHED].recvs_data == 50u);
    CHECK(R[BATCHED].segments == 50u);
    CHECK(R[PERBYTE].recvs_data == d.n && R[PERBYTE].segments == d.samples);

    /* 3. random valid streams at random chunkings (1 .. 5000 B) */
    for (int round = 0; round < 12; round++) {
        stream_t rs = { 0 };
        static const uint8_t alphabet[] = "01234567R01234567R0123RRrsBb";
        unsigned n = 2000u + rnd() % 20000u;
        for (unsigned i = 0; i < n; i++) {
            uint8_t c = alphabet[rnd() % (sizeof(alphabet) - 1u)];
            if ((c == 'r' || c == 's') && rnd() % 8u) c = '5';   /* resets rarer */
            put(&rs, c);
        }
        unsigned ch[9];
        for (int i = 0; i < 8; i++) ch[i] = 1u + rnd() % (i < 4 ? 7u : 5000u);
        ch[8] = 0;
        char name[64];
        snprintf(name, sizeof(name), "random stream #%d, chunks %u..", round, ch[0]);
        same_everywhere(name, rs.b, rs.n, ch, 0, 0, R);
        for (int e = 0; e < NENGINES; e++) free_result(&R[e]);
        free(rs.b);
    }

    /* 4. 'Q' mid-chunk: the replies before it go, nothing after it is interpreted */
    {
        stream_t q = { 0 };
        idcode_read(&q);
        put(&q, 'R'); put(&q, 'Q'); put(&q, '7'); put(&q, 'R'); put(&q, 's');
        same_everywhere("Q mid-chunk (then 7 R s)", q.b, q.n, whole, 0, 0, R);
        CHECK(R[REF].closed == 1 && R[REF].nreply == 33);
        for (int e = 0; e < NENGINES; e++) free_result(&R[e]);
        free(q.b);
    }
    /* 5. a byte outside the protocol: dropped at it, replies before it delivered */
    {
        stream_t x = { 0 };
        idcode_read(&x);
        put(&x, 'R'); put(&x, 'd'); put(&x, 'R');
        same_everywhere("a byte outside the protocol ('d', an SWD char)", x.b, x.n, whole, 0, 0, R);
        CHECK(R[REF].closed == 1 && R[REF].nreply == 33);
        for (int e = 0; e < NENGINES; e++) free_result(&R[e]);
        free(x.b);
    }
    /* 6. send back-pressure: 1 and 3 bytes a send */
    for (int lim = 1; lim <= 3; lim += 2) {
        stream_t b = { 0 };
        for (int k = 0; k < 6; k++) dap_access(&b, 0xB, rnd());
        char name[64];
        snprintf(name, sizeof(name), "send back-pressure: %d B per send", lim);
        same_everywhere(name, b.b, b.n, whole, lim, 0, R);
        for (int e = 0; e < NENGINES; e++) free_result(&R[e]);
        free(b.b);
    }
    /* 7. a swap's gate: nothing consumed while gated, all served after */
    {
        stream_t g = { 0 };
        idcode_read(&g);
        static const unsigned small[] = { 10, 0 };
        same_everywhere("a swap's gate (swd_gated for the first 20 polls)", g.b, g.n, small, 0, 1, R);
        CHECK(s_gate_checked == NENGINES);
        CHECK(idcode_of(R[BATCHED].reply, 32) == 0x6BA00477u);
        for (int e = 0; e < NENGINES; e++) free_result(&R[e]);
        free(g.b);
    }
    /* 8. resets: srst reaches CLKRST.dbg_resetn the same way (asserted = cleared) */
    {
        static const uint8_t rs[] = "s0R4r0R";
        same_everywhere("srst/trst chars (s ... r)", rs, 7, whole, 0, 0, R);
        for (int e = 0; e < NENGINES; e++) free_result(&R[e]);
    }

    printf("\n== the cost model (MBV, per DAP access: %u command bytes, %u samples) ==\n",
           per_scan_bytes, per_scan_samples);
    double ref_srv = server_us(&ref_scan, 50) + per_scan_bytes * US_BYTE;
    double bat_srv = server_us(&bat_scan, 50) + per_scan_bytes * US_BYTE;
    double ref_ssh = (double)ref_scan.segments / 50.0 * US_SSH_SEG;
    double bat_ssh = (double)bat_scan.segments / 50.0 * US_SSH_SEG;
    printf("  REF     %6.1f recvs %5.1f segments  harnessd %6.2f ms  + dropbear %5.2f ms = %6.2f ms\n",
           ref_scan.recvs_data / 50.0 + 1.0, ref_scan.segments / 50.0, ref_srv / 1000, ref_ssh / 1000,
           (ref_srv + ref_ssh) / 1000);
    printf("  BATCHED %6.1f recvs %5.1f segments  harnessd %6.2f ms  + dropbear %5.2f ms = %6.2f ms\n",
           bat_scan.recvs_data / 50.0 + 1.0, bat_scan.segments / 50.0, bat_srv / 1000, bat_ssh / 1000,
           (bat_srv + bat_ssh) / 1000);
    double rest = MS_HOST_RTT_MEASURED - (ref_srv + ref_ssh) / 1000;
    if (rest < 0.5) rest = 0.5;
    double host_new = rest + (bat_srv + bat_ssh) / 1000;
    printf("  host path (claim forward): measured %.1f ms/round trip; the board's share (model) "
           "%.1f ms, the rest %.1f ms\n", MS_HOST_RTT_MEASURED, (ref_srv + ref_ssh) / 1000, rest);
    printf("  -> BATCHED %.1f ms/round trip: x%.1f, ~%.0f B/s of image data (was ~25 B/s: the "
           "160 KB write ~%.0f min, was 107)\n", host_new, MS_HOST_RTT_MEASURED / host_new,
           25.0 * MS_HOST_RTT_MEASURED / host_new, 107.0 * host_new / MS_HOST_RTT_MEASURED);
    printf("  on-board OpenOCD (no SSH, no network): REF %.2f ms, BATCHED %.2f ms per access "
           "(+ OpenOCD's own ~2 syscalls)\n", ref_srv / 1000, bat_srv / 1000);
    printf("  (E = estimate: recv %.0f us, send %.0f us, dropbear %.0f us/segment, dispatch %.1f "
           "us/byte; the board session measures them)\n", US_RECV, US_SEND, US_SSH_SEG, US_BYTE);
    CHECK((ref_srv + ref_ssh) / (bat_srv + bat_ssh) >= 10.0);   /* the board's share */
    CHECK(MS_HOST_RTT_MEASURED / host_new >= 2.0);

    printf("test_jtag_batch: %d checks, ALL PASS\n", s_checks);
    return 0;
}
