/*
 * test_net_if_lwip_tx.c — host-gcc tests for lan_linkoutput(), the lwIP
 * transmit boundary in firmware/platform/src/net_if_lwip.c.
 *
 * Separate binary from test_net_if_lwip.c because this half needs the REAL
 * LAN9220 driver underneath it (firmware/smsc911x/smsc911x.c against
 * fake_lan9220.c) and because its central case is a TIMING case: what the
 * bounded TX-space wait does when the FIFO never drains.
 *
 * THE FAILURE MODE UNDER TEST. lan_linkoutput() has exactly three outcomes and
 * the difference between two of them is a silent-data-loss bug:
 *   - ERR_IF   — oversize/hard error. The frame is rejected, counted, gone.
 *   - ERR_OK   — the frame is in the chip's TX FIFO.
 *   - ERR_MEM  — no room after the bounded wait. lwIP RETRIES: the segment
 *                stays on the unsent/unacked queue. Returning ERR_OK here
 *                instead would tell TCP the frame was sent and hide the loss
 *                until a full RTO, which is the failure the ERR_MEM branch
 *                exists to prevent. The stuck-register case asserts NEVER-OK.
 *
 * WHY IT IS A CLOCK ASSERTION AND NOT AN ITERATION COUNT. See common/service.h
 * "THE BOUNDS WERE ITERATION COUNTS": a spin of N iterations is not a
 * duration, so nothing could state what the TX-space wait was worth in
 * microseconds. mock_regs' per-register-read clock (mock_regs_set_us_per_read)
 * makes it one: a stuck TX_FIFO_INF advances fake time by spinning on it, so
 * "this wait gives up after MPS3_TX_SPACE_TIMEOUT_US" becomes assertable.
 *
 * Links: net_if_lwip.c (INCLUDED, not linked), fake_lwip.c, mock_regs.c,
 * fake_lan9220.c, ../smsc911x/smsc911x.c. -DMPS3_HAL_MOCK -I fake_lwip.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../common/platform_regs.h"
#include "mock_regs.h"
#include "fake_lan9220.h"
#include "fake_lwip_ctl.h"

/* Included, not linked: the tx counters (s_tx_fifo_full_drops et al) are
 * static with no reset entry point, and a multi-case binary must be able to
 * zero them between cases. */
#include "../platform/src/net_if_lwip.c"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The driver's base as far as net_if_lwip.c is concerned. FAKE_BASE is where
 * the chip model actually lives when the interposer below is in play. */
#define LAN_BASE  0x60000000u
#define FAKE_BASE 0x61000000u

static const uint8_t MAC[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };

/* The bounded TX-space wait, in microseconds.
 *
 * net_if_lwip.c is mid-migration from `for (i = 0; i < MPS3_TX_SPACE_POLL_BOUND;
 * i++)` to a mps3_spin_until()-shaped wait; once it defines the constant this
 * picks it up, and until then the test states the agreed value itself. That is
 * deliberate: test_stuck_tx_space_gives_up_on_time() below is the CONTROL for
 * that migration and FAILS against the iteration-count spin, which never
 * consults the clock and therefore burns ~40x the budget in fake time. */
#ifndef MPS3_TX_SPACE_TIMEOUT_US
#define MPS3_TX_SPACE_TIMEOUT_US 5000u
#endif

static void backend_reset(void)
{
    memset(s_listeners, 0, sizeof(s_listeners));
    memset(s_conns, 0, sizeof(s_conns));
    memset(s_udps, 0, sizeof(s_udps));
    memset(&s_netif, 0, sizeof(s_netif));
    s_tx_fifo_full_drops = 0;
    s_tx_space_stalls = 0;
    s_tx_iface_errors = 0;
}

/* ---- the TX-space interposer ------------------------------------------------
 * fake_lan9220's TDFREE is a static knob: fake_lan9220_set_tx_free() pins it
 * and nothing moves it again. That is enough for a PERMANENTLY stuck FIFO, but
 * a TRANSIENT stall has to clear from INSIDE one lan_linkoutput() call, and no
 * test code runs during that spin.
 *
 * So the chip model is moved to FAKE_BASE and this hook claims LAN_BASE (the
 * base the driver is initialised with), forwarding every access on to the real
 * model and masking TDFREE to zero for the first `s_stall_reads` TX_FIFO_INF
 * reads. mock_regs allows one hook per base and offers no way to chain, hence
 * two bases rather than a wrapper. Forwarding costs a second charged read, so
 * fake time runs at 2x us_per_read while the interposer is installed — no test
 * that uses it asserts on elapsed time.
 * -------------------------------------------------------------------------- */
static int s_stall_reads;

static int tx_space_interposer(void *ctx, int is_write, uint32_t base,
                               uint32_t off, uint32_t *val)
{
    (void)ctx;
    (void)base;
    if (is_write) {
        mps3_reg_write32(FAKE_BASE, off, *val);
        return 1;
    }
    uint32_t v = mps3_reg_read32(FAKE_BASE, off);
    if (off == SMSC911X_TX_FIFO_INF && s_stall_reads > 0) {
        s_stall_reads--;
        v &= ~SMSC911X_TX_FIFO_INF_TDFREE_MASK; /* "no room right now" */
    }
    *val = v;
    return 1;
}

/* Plain wiring: the chip model IS at LAN_BASE. */
static struct netif *fresh_direct(void)
{
    mock_regs_reset();
    fake_lan9220_reset(LAN_BASE);
    fake_lwip_reset_all();
    backend_reset();
    s_stall_reads = 0;
    CHECK(mps3_net_lwip_init(LAN_BASE, MAC) == 0);
    struct netif *n = fake_netif_last();
    CHECK(n != NULL);
    CHECK(n->linkoutput == lan_linkoutput); /* the registered function, not a copy */
    return n;
}

/* Interposed wiring: model at FAKE_BASE, hook at LAN_BASE. */
static struct netif *fresh_interposed(void)
{
    mock_regs_reset();
    fake_lan9220_reset(FAKE_BASE);
    mock_regs_set_hook(LAN_BASE, tx_space_interposer, NULL);
    fake_lwip_reset_all();
    backend_reset();
    s_stall_reads = 0;
    CHECK(mps3_net_lwip_init(LAN_BASE, MAC) == 0);
    struct netif *n = fake_netif_last();
    CHECK(n != NULL);
    return n;
}

static void tx_diag(uint32_t *drops, uint32_t *stalls, uint32_t *ifaces)
{
    *drops = *stalls = *ifaces = 0;
    mps3_net_lwip_tx_diag(drops, stalls, ifaces);
}

static struct pbuf *frame_pbuf(int len, uint8_t seed)
{
    struct pbuf *p = pbuf_alloc(PBUF_RAW, (u16_t)len, PBUF_RAM);
    CHECK(p != NULL);
    for (int i = 0; i < len; i++) {
        ((uint8_t *)p->payload)[i] = (uint8_t)(seed + i);
    }
    return p;
}

/* ==========================================================================
 * happy path (the negative control for everything below)
 * ========================================================================== */

static void test_linkoutput_sends_and_moves_no_counter(void)
{
    struct netif *n = fresh_direct();
    struct pbuf *p = frame_pbuf(60, 0x30);

    CHECK(n->linkoutput(n, p) == ERR_OK);
    CHECK(fake_lan9220_tx_count() == 1);

    uint8_t got[128];
    CHECK(fake_lan9220_take_tx(got, sizeof(got)) == 60);
    CHECK(memcmp(got, p->payload, 60) == 0);

    uint32_t d, s, e;
    tx_diag(&d, &s, &e);
    CHECK(d == 0 && s == 0 && e == 0); /* a clean send moves NOTHING */
    pbuf_free(p);
    CHECK(fake_pbuf_faults() == 0);
}

static void test_linkoutput_flattens_a_chain(void)
{
    struct netif *n = fresh_direct();
    /* smsc911x_tx_frame() is single-segment by design, so linkoutput must
     * flatten the chain lwIP handed it — a copy that stopped at the first
     * pbuf would put a short, silently-truncated frame on the wire. */
    struct pbuf *a = frame_pbuf(10, 0x10);
    struct pbuf *b = frame_pbuf(20, 0x40);
    struct pbuf *c = frame_pbuf(7,  0x90);
    pbuf_cat(a, b);
    pbuf_cat(a, c);
    CHECK(a->tot_len == 37);

    CHECK(n->linkoutput(n, a) == ERR_OK);
    uint8_t got[128];
    CHECK(fake_lan9220_take_tx(got, sizeof(got)) == 37);
    CHECK(got[0] == 0x10 && got[9] == 0x19);
    CHECK(got[10] == 0x40 && got[29] == 0x53);
    CHECK(got[30] == 0x90 && got[36] == 0x96);
    pbuf_free(a);
}

/* ==========================================================================
 * ERR_IF: oversize and driver hard errors
 * ========================================================================== */

static void test_oversize_frame_is_an_iface_error(void)
{
    struct netif *n = fresh_direct();
    struct pbuf *p = frame_pbuf(SMSC911X_MAX_FRAME + 1, 0x55);

    uint32_t copies = fake_pbuf_copy_partial_calls();
    CHECK(n->linkoutput(n, p) == ERR_IF);

    uint32_t d, s, e;
    tx_diag(&d, &s, &e);
    CHECK(e == 1);                        /* counted as an interface error ... */
    CHECK(d == 0 && s == 0);
    CHECK(fake_lan9220_tx_count() == 0);  /* ... and nothing reached the chip */

    /* And it was rejected BEFORE the flatten copy. This is the only assertion
     * that can tell the guard apart from the driver's own oversize rejection —
     * smsc911x_tx_frame() would also return an error for this length, with the
     * same ERR_IF and the same counter. The difference is that s_frame is
     * exactly SMSC911X_MAX_FRAME bytes, so reaching the copy at all overruns
     * it: the guard is a BOUNDS CHECK, not a duplicate error report. */
    CHECK(fake_pbuf_copy_partial_calls() == copies);

    /* Exactly at the limit still goes out: the check is >, not >=. */
    struct pbuf *ok = frame_pbuf(SMSC911X_MAX_FRAME, 0x11);
    CHECK(n->linkoutput(n, ok) == ERR_OK);
    CHECK(fake_lan9220_tx_count() == 1);
    CHECK(fake_pbuf_copy_partial_calls() == copies + 1);
    tx_diag(&d, &s, &e);
    CHECK(e == 1);
    pbuf_free(p);
    pbuf_free(ok);
}

static void test_driver_hard_error_is_not_a_space_wait(void)
{
    struct netif *n = fresh_direct();
    /* A zero-length frame is SMSC911X_ERR_TOO_BIG from the driver, i.e. a hard
     * error and not SMSC911X_ERR_TX_SPACE. It must leave the spin IMMEDIATELY
     * as ERR_IF; treating any non-zero return as backpressure would burn the
     * whole wait on a frame that can never be sent. */
    struct pbuf *p = pbuf_alloc(PBUF_RAW, 0, PBUF_RAM);
    CHECK(p != NULL);
    mock_regs_set_us_per_read(1);
    uint32_t t0 = mock_time_now_us();
    CHECK(n->linkoutput(n, p) == ERR_IF);
    uint32_t elapsed = mock_time_now_us() - t0;

    uint32_t d, s, e;
    tx_diag(&d, &s, &e);
    CHECK(e == 1);
    CHECK(d == 0 && s == 0);
    CHECK(elapsed < MPS3_TX_SPACE_TIMEOUT_US); /* it did not spin at all */
    mock_regs_set_us_per_read(0);
    pbuf_free(p);
}

/* ==========================================================================
 * ERR_OK after a TRANSIENT stall
 * ========================================================================== */

static void test_transient_tx_space_stall_clears(void)
{
    struct netif *n = fresh_interposed();

    /* Each smsc911x_tx_frame() attempt reads TX_FIFO_INF twice: once in the
     * status drain (TXSUSED) and once for the room check (TDFREE). Masking
     * TDFREE for the first two reads therefore stalls exactly ONE attempt; the
     * next attempt finds room and transmits. */
    s_stall_reads = 2;
    struct pbuf *p = frame_pbuf(64, 0x20);
    CHECK(n->linkoutput(n, p) == ERR_OK);
    CHECK(s_stall_reads == 0);
    CHECK(fake_lan9220_tx_count() == 1);

    uint32_t d, s, e;
    tx_diag(&d, &s, &e);
    CHECK(s == 1);            /* the stall is counted ONCE per linkoutput call */
    CHECK(d == 0 && e == 0);  /* and it is NOT a drop and NOT an iface error */

    /* A second, unobstructed frame must not touch the counter again — the
     * stall count is "calls that had to wait", not "calls". */
    struct pbuf *q = frame_pbuf(64, 0x60);
    CHECK(n->linkoutput(n, q) == ERR_OK);
    tx_diag(&d, &s, &e);
    CHECK(s == 1);
    CHECK(fake_lan9220_tx_count() == 2);

    /* A longer stall is still ONE stall, not one per spin iteration. */
    s_stall_reads = 8;
    struct pbuf *r = frame_pbuf(64, 0xA0);
    CHECK(n->linkoutput(n, r) == ERR_OK);
    tx_diag(&d, &s, &e);
    CHECK(s == 2);
    CHECK(d == 0 && e == 0);

    pbuf_free(p);
    pbuf_free(q);
    pbuf_free(r);
    CHECK(fake_pbuf_faults() == 0);
}

/* ==========================================================================
 * ERR_MEM after a STUCK TX-space register  (the time-bounded wait)
 * ========================================================================== */

static void test_stuck_tx_space_gives_up_on_time(void)
{
    struct netif *n = fresh_direct();

    /* PIN THE BUDGET. The bounds below are expressed in terms of
     * MPS3_TX_SPACE_TIMEOUT_US, so on their own they would follow the constant
     * wherever it went — a wait silently retuned to 50 ms would still "pass".
     * The agreed value is part of the contract (5 ms is ~40 frame times at
     * wire speed), so changing it has to be a deliberate edit to this line. */
    CHECK(MPS3_TX_SPACE_TIMEOUT_US == 5000u);

    /* TDFREE pinned below what any frame needs: the FIFO never drains, so the
     * wait can only end by giving up. */
    fake_lan9220_set_tx_free(0);
    struct pbuf *p = frame_pbuf(64, 0x77);

    /* Every register read now costs a microsecond of fake time, which is the
     * only thing that moves the clock from inside the spin. */
    mock_regs_set_us_per_read(1);
    uint32_t t0 = mock_time_now_us();
    err_t rc = n->linkoutput(n, p);
    uint32_t elapsed = mock_time_now_us() - t0;
    mock_regs_set_us_per_read(0);

    /* NEVER ERR_OK. Telling lwIP a frame was sent when it was not is the
     * silent-drop failure mode; ERR_MEM keeps the segment on the unsent queue
     * so the next tcp_output re-drives it. */
    CHECK(rc == ERR_MEM);
    CHECK(rc != ERR_OK);
    CHECK(fake_lan9220_tx_count() == 0);

    uint32_t d, s, e;
    tx_diag(&d, &s, &e);
    CHECK(d == 1);            /* counted as a retry-forced drop ... */
    CHECK(s == 0);            /* ... and NOT as a stall that cleared */
    CHECK(e == 0);            /* ... and not as an interface error */

    printf("test_net_if_lwip_tx: stuck TX-space wait gave up after %u us of "
           "fake time (budget %u us, upper bound %u us)\n",
           (unsigned)elapsed, (unsigned)MPS3_TX_SPACE_TIMEOUT_US,
           (unsigned)(4u * MPS3_TX_SPACE_TIMEOUT_US));

    /* THE BOUND. Lower: it must actually WAIT — a wait that gives up before
     * its budget would turn every momentary FIFO burst into a retransmit.
     * Upper: 4x the budget is generous slack for the granularity of one spin
     * iteration, and still an order of magnitude under what an iteration-count
     * spin costs, so this is the assertion that distinguishes the two. */
    CHECK(elapsed >= MPS3_TX_SPACE_TIMEOUT_US);
    CHECK(elapsed <= 4u * MPS3_TX_SPACE_TIMEOUT_US);

    pbuf_free(p);
    CHECK(fake_pbuf_faults() == 0);
}

static void test_tx_diag_tolerates_null_out_pointers(void)
{
    (void)fresh_direct();
    mps3_net_lwip_tx_diag(NULL, NULL, NULL); /* must not fault */
    uint32_t only = 0xDEADBEEFu;
    mps3_net_lwip_tx_diag(&only, NULL, NULL);
    CHECK(only == 0);
    s_checks++;
}

int main(void)
{
    /* Unbuffered: an assert() aborts the process, and a buffered stdout would
     * take the progress lines -- and, in the stuck-TX case below, the measured
     * number the failure is ABOUT -- down with it. */
    setvbuf(stdout, NULL, _IONBF, 0);

    test_linkoutput_sends_and_moves_no_counter();
    test_linkoutput_flattens_a_chain();
    printf("test_net_if_lwip_tx: linkoutput happy path OK\n");

    test_oversize_frame_is_an_iface_error();
    test_driver_hard_error_is_not_a_space_wait();
    printf("test_net_if_lwip_tx: ERR_IF paths OK\n");

    test_transient_tx_space_stall_clears();
    printf("test_net_if_lwip_tx: transient TX-space stall OK\n");

    test_stuck_tx_space_gives_up_on_time();
    test_tx_diag_tolerates_null_out_pointers();
    printf("test_net_if_lwip_tx: stuck TX-space bound OK\n");

    printf("test_net_if_lwip_tx: %d checks passed\n", s_checks);
    return 0;
}
