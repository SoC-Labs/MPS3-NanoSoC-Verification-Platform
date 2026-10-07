/*
 * test_smsc911x.c — host-gcc tests for the LAN9220 driver port (W-SMSC;
 * Apache-2.0-derived sequences, see smsc911x.h's provenance block) against
 * fake_lan9220.c's chip model: reset/init ordering, MAC-address + MAC_CR
 * programming through the CSR indirection, PHY reset/link via MII, TX
 * command-word framing, RX status/data draining and the drop paths.
 *
 * Links: smsc911x.c, mock_regs.c, fake_lan9220.c. -DMPS3_HAL_MOCK.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../smsc911x/smsc911x.h"
#include "mock_regs.h"
#include "fake_lan9220.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The LAN9220 is a static-memory-bus peripheral; its base is a BD detail
 * (init argument, not a platform_regs.h block) — any page works here. */
#define LAN_BASE 0x60000000u

static const uint8_t MAC[6] = { 0x02, 0x11, 0x22, 0x33, 0x44, 0x55 };

static void fresh(void)
{
    mock_regs_reset();
    fake_lan9220_reset(LAN_BASE);
    /* THE FAKE CLOCK MUST MOVE, or a bounded wait cannot finish.
     *
     * smsc911x.c's busy/ready waits are mps3_spin_until() now, not
     * `for (i = 0; i < SMSC911X_POLL_BOUND; i++)`. An iteration count expires
     * on its own; a DEADLINE expires only when time passes, and on the host
     * time passes only when a register is read (mock_regs_set_us_per_read()).
     * With the default 0 the two stuck-register cases below did not fail --
     * they HUNG, which is the worse diagnostic of the two. Charging every read
     * 1 us here makes them time out cleanly instead.
     *
     * It is set in fresh() rather than in those two cases so that a FUTURE case
     * that pins a register cannot reintroduce the hang. No case in this file
     * asserts an absolute duration; the ones that do live in
     * test_smsc911x_spin.c, which sets its own rate per case. */
    mock_regs_set_us_per_read(1u);
}

static void test_init_happy_sequence(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    /* soft reset actually issued (datasheet bring-up order) */
    CHECK(fake_lan9220_soft_resets() == 1);

    /* MAC address landed via the CSR indirection, little-endian packing */
    CHECK(fake_lan9220_mac_csr(SMSC911X_MAC_ADDRL) == 0x33221102u);
    CHECK(fake_lan9220_mac_csr(SMSC911X_MAC_ADDRH) == 0x00005544u);

    /* MAC_CR: TX+RX enabled AND promiscuous (§8.3 — two MACs, one port) */
    uint32_t mac_cr = fake_lan9220_mac_csr(SMSC911X_MAC_CR);
    CHECK((mac_cr & SMSC911X_MAC_CR_TXEN) != 0);
    CHECK((mac_cr & SMSC911X_MAC_CR_RXEN) != 0);
    CHECK((mac_cr & SMSC911X_MAC_CR_PRMS) != 0);

    /* TX datapath on; interrupts masked (poll model) */
    CHECK(mock_regs_peek(LAN_BASE, 0) == 0); /* no stray slot writes: hook consumed all */
    CHECK((fake_lan9220_phy_reg(SMSC911X_PHY_BMCR) & SMSC911X_PHY_BMCR_ANEN) != 0);
    CHECK((fake_lan9220_phy_reg(SMSC911X_PHY_BMCR) & SMSC911X_PHY_BMCR_RESET) == 0);
}

static void test_init_rejects_wrong_chip(void)
{
    fresh();
    fake_lan9220_set_id_rev(0x01180001u); /* a LAN9118, not our part */
    CHECK(smsc911x_init(LAN_BASE, MAC) == SMSC911X_ERR_ID);

    fresh();
    fake_lan9220_set_byte_test(0x12345678u); /* byte lanes swapped/wrong bus */
    CHECK(smsc911x_init(LAN_BASE, MAC) == SMSC911X_ERR_BYTE_TEST);
}

static void test_tx_frame_reassembled_by_chip(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    /* 15 bytes: exercises the trailing partial word */
    uint8_t frame[15];
    for (int i = 0; i < 15; i++) frame[i] = (uint8_t)(0xC0 + i);
    CHECK(smsc911x_tx_frame(frame, sizeof(frame)) == 0);
    CHECK(fake_lan9220_tx_count() == 1);

    uint8_t got[64];
    CHECK(fake_lan9220_take_tx(got, sizeof(got)) == 15); /* CMDA/CMDB agreed */
    CHECK(memcmp(got, frame, 15) == 0);

    /* oversize refused before any FIFO write */
    CHECK(smsc911x_tx_frame(frame, SMSC911X_MAX_FRAME + 1) == SMSC911X_ERR_TOO_BIG);
    CHECK(fake_lan9220_tx_count() == 0);
}

static void test_rx_frames_in_order_then_empty(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    uint8_t f1[9], f2[12];
    for (int i = 0; i < 9; i++)  f1[i] = (uint8_t)(0x10 + i);
    for (int i = 0; i < 12; i++) f2[i] = (uint8_t)(0x50 + i);
    CHECK(fake_lan9220_inject_rx(f1, sizeof(f1), 0) == 0);
    CHECK(fake_lan9220_inject_rx(f2, sizeof(f2), 0) == 0);

    uint8_t buf[64];
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 9);
    CHECK(memcmp(buf, f1, 9) == 0);
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 12);
    CHECK(memcmp(buf, f2, 12) == 0);
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 0); /* drained */
}

static void test_rx_drop_paths_keep_fifo_aligned(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    uint8_t bad[20], good[8];
    memset(bad, 0xAA, sizeof(bad));
    for (int i = 0; i < 8; i++) good[i] = (uint8_t)(0x70 + i);

    /* error-summary frame: consumed + dropped, next frame still clean */
    CHECK(fake_lan9220_inject_rx(bad, sizeof(bad), /*error=*/1) == 0);
    CHECK(fake_lan9220_inject_rx(good, sizeof(good), 0) == 0);
    uint8_t buf[64];
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == SMSC911X_ERR_RX_ERROR);
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 8);
    CHECK(memcmp(buf, good, 8) == 0);

    /* frame bigger than the caller's buffer: consumed + dropped (never a
     * silent truncation), following frame unaffected */
    CHECK(fake_lan9220_inject_rx(bad, sizeof(bad), 0) == 0);
    CHECK(fake_lan9220_inject_rx(good, sizeof(good), 0) == 0);
    uint8_t tiny[8];
    CHECK(smsc911x_rx_frame(tiny, sizeof(tiny)) == SMSC911X_ERR_TOO_BIG);
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 8);
    CHECK(memcmp(buf, good, 8) == 0);
}

static void test_rx_overflow_recovery_keeps_mac_alive(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    uint8_t f1[16], f2[24], buf[64];
    for (int i = 0; i < 16; i++) f1[i] = (uint8_t)(0x30 + i);
    for (int i = 0; i < 24; i++) f2[i] = (uint8_t)(0x80 + i);

    /* 1) A clean dropped-frame overrun (RXDF_INT): the FIFO is still aligned, so
     *    a frame already queued must survive — recovery just acks the sticky
     *    status + drains the drop counter, it does NOT flush the data FIFO. */
    CHECK(fake_lan9220_inject_rx(f1, sizeof(f1), 0) == 0);
    fake_lan9220_inject_rx_overrun(SMSC911X_INT_STS_RXDF_INT);
    CHECK(smsc911x_rx_recover() == 1);            /* detected + recovered */
    CHECK(fake_lan9220_int_sts() == 0);           /* sticky bits acked */
    CHECK(smsc911x_rx_recover() == 0);            /* idempotent: nothing left */
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 16); /* queued frame survived */
    CHECK(memcmp(buf, f1, 16) == 0);

    /* 2) MAC RX still alive after recovery: a fresh frame reads cleanly. */
    CHECK(fake_lan9220_inject_rx(f2, sizeof(f2), 0) == 0);
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 24);
    CHECK(memcmp(buf, f2, 24) == 0);

    /* 3) A hard RX error (RXE) flushes the RX data FIFO to resync it: a stale
     *    frame is dumped, and smsc911x_rx_frame()'s internal recovery leaves the
     *    FIFO empty (return 0) with the status cleared — MAC stays enabled. */
    CHECK(fake_lan9220_inject_rx(f1, sizeof(f1), 0) == 0);
    fake_lan9220_inject_rx_overrun(SMSC911X_INT_STS_RXE);
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 0); /* recovery dumped the FIFO */
    CHECK(fake_lan9220_int_sts() == 0);
    /* And reception continues: a new frame after the flush is delivered. */
    CHECK(fake_lan9220_inject_rx(f2, sizeof(f2), 0) == 0);
    CHECK(smsc911x_rx_frame(buf, sizeof(buf)) == 24);
    CHECK(memcmp(buf, f2, 24) == 0);

    /* Free-running diagnostics: two recovery events total (the RXDF_INT one +
     * the RXE one via rx_frame), of which exactly one took the FIFO-dump path,
     * and both drained a dropped-frame count (the fake bumps RX_DROP per inject).
     * These are exactly the counters the shell surfaces on the diag mailbox. */
    uint32_t ev = 0, du = 0, dr = 0;
    smsc911x_get_diag(&ev, &du, &dr);
    CHECK(ev == 2);
    CHECK(du == 1);
    CHECK(dr == 2);
}

static void test_init_reset_never_completes_times_out(void)
{
    /* Chip never comes out of soft reset: init's reset wait_clear() must hit
     * its bounded poll and fail SMSC911X_ERR_TIMEOUT rather than spin forever. */
    fresh();
    fake_lan9220_set_reset_stuck(1);
    CHECK(smsc911x_init(LAN_BASE, MAC) == SMSC911X_ERR_TIMEOUT);
    CHECK(fake_lan9220_soft_resets() == 1); /* it DID issue the reset */
}

static void test_init_mac_csr_stuck_busy_times_out(void)
{
    /* The MAC-CSR indirection hangs BUSY: every mac_csr_*() wait_clear() must
     * time out, so init fails closed at the first MAC-CSR access (PHY bring-up). */
    fresh();
    fake_lan9220_set_csr_busy(1);
    CHECK(smsc911x_init(LAN_BASE, MAC) == SMSC911X_ERR_TIMEOUT);
}

static void test_tx_no_fifo_space_backpressure(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    uint8_t frame[15];
    for (int i = 0; i < 15; i++) frame[i] = (uint8_t)(0xC0 + i);

    /* TX FIFO reports too little TDFREE for this frame (needs
     * roundup(15)+8 = 24 bytes) -> non-blocking SMSC911X_ERR_TX_SPACE, and
     * NOT a single command word must reach the FIFO (no torn half-frame). The
     * driver REPORTS the refusal (a distinct error code) rather than silently
     * dropping — lan_linkoutput turns this into ERR_MEM ("retry"), never a
     * silent as-sent drop that would cost a full RTO. */
    fake_lan9220_set_tx_free(16);
    CHECK(smsc911x_tx_frame(frame, sizeof(frame)) == SMSC911X_ERR_TX_SPACE);
    CHECK(fake_lan9220_tx_count() == 0);
    CHECK(fake_lan9220_tx_status_used() == 0); /* nothing transmitted => no status */

    /* Space frees up on a later poll: the same frame now goes out intact. */
    fake_lan9220_set_tx_free(4096);
    CHECK(smsc911x_tx_frame(frame, sizeof(frame)) == 0);
    CHECK(fake_lan9220_tx_count() == 1);
    uint8_t got[64];
    CHECK(fake_lan9220_take_tx(got, sizeof(got)) == 15);
    CHECK(memcmp(got, frame, 15) == 0);
}

/* THE FIX: the TX STATUS FIFO must be drained or the MAC stalls once it fills.
 * Prove (1) init enables TXSAO as the belt, (2) a burst LARGER than the fake's
 * status-FIFO depth stays fully drained (TXSUSED returns to 0 — never pinned),
 * and (3) the tx diagnostics advance in step (status_drained == frames_sent). */
static void test_tx_status_fifo_drained(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    /* TX datapath on AND status-allow-overrun set (belt to the always-drain). */
    CHECK((fake_lan9220_tx_cfg() & SMSC911X_TX_CFG_TX_ON) != 0);
    CHECK((fake_lan9220_tx_cfg() & SMSC911X_TX_CFG_TXSAO) != 0);

    uint8_t frame[60];
    for (int i = 0; i < 60; i++) frame[i] = (uint8_t)i;

    /* The driver's tx diagnostics are monotonic (never reset after init), so
     * assert on DELTAS against a baseline — prior TX tests have already bumped
     * them. */
    uint32_t f0 = 0, d0 = 0, e0 = 0;
    smsc911x_get_tx_diag(&f0, &d0, &e0);

    /* 40 frames >> the fake's 16-deep status FIFO: if the driver did not reap
     * the status FIFO it would pin at full and (on real silicon) halt TX. Each
     * smsc911x_tx_frame() drains at entry, so TXSUSED never climbs. */
    for (int k = 0; k < 40; k++) {
        CHECK(smsc911x_tx_frame(frame, sizeof(frame)) == 0);
        CHECK(fake_lan9220_tx_status_used() <= 1); /* at most the just-posted one */
    }
    /* Poll-time sweep reaps the final frame's status (posts after its tx_frame). */
    CHECK(smsc911x_tx_status_drain() == 1);
    CHECK(fake_lan9220_tx_status_used() == 0); /* fully drained, never pinned */

    uint32_t f1 = 0, d1 = 0, e1 = 0;
    smsc911x_get_tx_diag(&f1, &d1, &e1);
    CHECK(f1 - f0 == 40);
    CHECK(d1 - d0 == 40); /* every posted completion status was reaped */
    CHECK(e1 - e0 == 0);
}

/* A frame the MAC could not cleanly transmit posts a status word with an error
 * bit set; the drain must count it into tx_errors (TCP then retransmits). */
static void test_tx_status_error_counted(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    uint32_t f0 = 0, d0 = 0, e0 = 0;
    smsc911x_get_tx_diag(&f0, &d0, &e0);

    /* A real failure = ES + a non-carrier cause. NOTE: on silicon every HEALTHY
     * frame completes ES|NO_CARR (0x8400) because carrier sense is not driven and
     * the link is full duplex -- counting that flagged 822/825 good frames. The
     * carrier-only case is asserted benign below. */
    fake_lan9220_set_tx_status(SMSC911X_TX_STS_ES | SMSC911X_TX_STS_EXCESS_COLL);
    uint8_t f[64];
    memset(f, 0x5A, sizeof(f));
    CHECK(smsc911x_tx_frame(f, sizeof(f)) == 0);
    CHECK(smsc911x_tx_status_drain() == 1); /* reap the one completion status */

    uint32_t f1 = 0, d1 = 0, e1 = 0;
    smsc911x_get_tx_diag(&f1, &d1, &e1);
    CHECK(f1 - f0 == 1);
    CHECK(d1 - d0 == 1);
    CHECK(e1 - e0 == 1); /* the error-flagged status word was counted */
}

static void test_phy_link_via_mii(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);
    CHECK(smsc911x_link_up() == 1);
    fake_lan9220_set_link(0);
    CHECK(smsc911x_link_up() == 0);
    fake_lan9220_set_link(1);
    CHECK(smsc911x_link_up() == 1);

    /* raw MII read/write path */
    uint16_t v = 0;
    CHECK(smsc911x_mii_write(17, 0xBEEF) == 0);
    CHECK(smsc911x_mii_read(17, &v) == 0);
    CHECK(v == 0xBEEF);
}

/* ES|NO_CARR is what EVERY healthy frame reports on this board (measured 0x8400:
 * carrier sense undriven, link full-duplex). It must NOT be counted as an error --
 * that mis-count made tx_errors read 822/825 on a flawless 1.31 MB transfer. */
static void test_tx_status_carrier_only_not_an_error(void)
{
    fresh();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    uint32_t f0 = 0, d0 = 0, e0 = 0;
    smsc911x_get_tx_diag(&f0, &d0, &e0);

    fake_lan9220_set_tx_status(SMSC911X_TX_STS_ES | SMSC911X_TX_STS_NO_CARR);
    uint8_t frame[64];
    memset(frame, 0xA5, sizeof(frame));
    CHECK(smsc911x_tx_frame(frame, sizeof(frame)) == 0);
    (void)smsc911x_tx_status_drain();

    uint32_t f1 = 0, d1 = 0, e1 = 0;
    smsc911x_get_tx_diag(&f1, &d1, &e1);
    CHECK(d1 > d0);            /* the status word WAS drained ... */
    CHECK(e1 == e0);           /* ... and correctly NOT counted as an error */
    CHECK(smsc911x_tx_last_status() == (SMSC911X_TX_STS_ES | SMSC911X_TX_STS_NO_CARR));
}

int main(void)
{
    test_init_happy_sequence();
    test_init_rejects_wrong_chip();
    test_tx_frame_reassembled_by_chip();
    test_rx_frames_in_order_then_empty();
    test_rx_drop_paths_keep_fifo_aligned();
    test_rx_overflow_recovery_keeps_mac_alive();
    test_init_reset_never_completes_times_out();
    test_init_mac_csr_stuck_busy_times_out();
    test_tx_no_fifo_space_backpressure();
    test_tx_status_fifo_drained();
    test_tx_status_error_counted();
    test_tx_status_carrier_only_not_an_error();
    test_phy_link_via_mii();

    printf("test_smsc911x: %d checks passed\n", s_checks);
    return 0;
}
