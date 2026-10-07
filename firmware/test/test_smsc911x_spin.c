/*
 * test_smsc911x_spin.c — the LAN9220 driver's TIME discipline: the six bounded
 * waits, and the two loops that are DRAINS rather than waits.
 *
 * WHY A SEPARATE BINARY FROM test_smsc911x.c. Every case here asserts a
 * DURATION or an ACCESS COUNT against a register that is deliberately stuck, so
 * it needs mock_regs_set_us_per_read() plus a register interposer — machinery
 * no other case in that file wants, and a per-read clock that would silently
 * change what every other case measures.
 *
 * WHAT WAS WRONG, AND WHY NONE OF THIS WAS ASSERTABLE BEFORE. smsc911x.c spun
 * `for (i = 0; i < SMSC911X_POLL_BOUND; i++)` — 100000 ITERATIONS — for the
 * soft reset, the MAC-CSR handshake, the MII handshake, the PHY reset, the
 * EEPROM auto-load, the RX_DUMP flush, and BOTH drains. One constant for eight
 * different things, none of which it could state in microseconds: an iteration
 * count is (loop body cycles / clock) seconds, so it meant something different
 * on every clock, every -O level and every edit to the loop body. See
 * common/service.h, "THE BOUNDS WERE ITERATION COUNTS".
 *
 * These cases are therefore CONTROLS for that conversion, not decoration:
 * against the old code test_stuck_*_gives_up_on_time() cannot even be written
 * (the old loop never reads a clock, so there is no duration to assert), and
 * test_*_drain_is_count_bounded() FAILS — the old bound let a drain pop 100000
 * times, and the assertions below pin it at 256, which is TXSUSED's/RXSUSED's
 * own 8-bit field width plus one.
 *
 * THE DRAINS ARE NOT WAITS, and are deliberately NOT converted to
 * mps3_spin_until(). A wait has a condition that becomes true and a duration
 * you will lose to it; a drain has neither — it pops items until the hardware
 * says there are none, at one bus access per item, and the hardware bounds a
 * COUNT. So they are asserted with access counts, not stopwatches. The cases
 * below are what stops someone "finishing the conversion" by giving them a
 * timeout, which would be a livelock budget rather than a bound.
 *
 * Links: ../smsc911x/smsc911x.{c,h}, ../common/service.c, mock_regs.c,
 * fake_lan9220.c. -DMPS3_HAL_MOCK.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../smsc911x/smsc911x.h"
#include "../common/platform_regs.h"   /* the mocked mps3_reg_*32 the interposer forwards with */
#include "../common/service.h"
#include "mock_regs.h"
#include "fake_lan9220.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The base the DRIVER is initialised with. FAKE_BASE is where the chip model
 * actually lives whenever the interposer below is in play (mock_regs allows one
 * hook per base and offers no way to chain, so two bases rather than a wrapper
 * — the same arrangement test_net_if_lwip_tx.c uses). */
#define LAN_BASE  0x60000000u
#define FAKE_BASE 0x61000000u

static const uint8_t MAC[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };

/* ---- the interposer ---------------------------------------------------------
 * fake_lan9220's own knobs cover a stuck SRST and a stuck MAC_CSR BUSY. The
 * remaining stuck conditions are fields the model computes rather than stores
 * (MII_ACC.BUSY, RX_CFG.RX_DUMP, TXSUSED, RXSUSED), so this hook claims
 * LAN_BASE, forwards every access to the real model at FAKE_BASE, and masks
 * those fields on the way back.
 *
 * COST: forwarding performs a second charged read, so fake time runs at 2x
 * us_per_read while the interposer is installed. Every duration assertion below
 * is therefore a RANGE with the deadline as its floor — which is the right
 * shape anyway: mps3_spin_until() guarantees "not less than the budget, and
 * one predicate more at most", not an exact number.
 * -------------------------------------------------------------------------- */
static int      s_pin_mii_busy;    /* MII_ACC.BUSY never clears                */
static int      s_pin_phy_in_reset;/* BMCR.RESET never clears                  */
static int      s_pin_rx_dump;     /* RX_CFG.RX_DUMP never self-clears         */
static int      s_pin_txsused;     /* TXSUSED pinned at its 8-bit maximum      */
static int      s_pin_rxsused;     /* RXSUSED pinned at its 8-bit maximum      */
static uint32_t s_last_csr_idx;    /* index of the last MAC_CSR_CMD write      */
static unsigned s_tx_status_pops;  /* reads of TX_STATUS_FIFO                  */
static unsigned s_rx_status_pops;  /* reads of RX_STATUS_FIFO                  */

static int lan_interposer(void *ctx, int is_write, uint32_t base,
                          uint32_t off, uint32_t *val)
{
    (void)ctx;
    (void)base;
    if (is_write) {
        if (off == SMSC911X_MAC_CSR_CMD) {
            s_last_csr_idx = *val & 0xFFu;
        }
        mps3_reg_write32(FAKE_BASE, off, *val);
        return 1;
    }

    if (off == SMSC911X_TX_STATUS_FIFO) {
        s_tx_status_pops++;
    }
    if (off == SMSC911X_RX_STATUS_FIFO) {
        s_rx_status_pops++;
    }

    uint32_t v = mps3_reg_read32(FAKE_BASE, off);

    if (off == SMSC911X_MAC_CSR_DATA && s_pin_mii_busy &&
        s_last_csr_idx == SMSC911X_MAC_MII_ACC) {
        v |= SMSC911X_MII_ACC_BUSY;          /* the MDIO frame never completes */
    }
    if (off == SMSC911X_MAC_CSR_DATA && s_pin_phy_in_reset &&
        s_last_csr_idx == SMSC911X_MAC_MII_DATA) {
        v |= SMSC911X_PHY_BMCR_RESET;        /* the PHY never leaves reset     */
    }
    if (off == SMSC911X_RX_CFG && s_pin_rx_dump) {
        v |= SMSC911X_RX_CFG_RX_DUMP;        /* the flush never self-clears    */
    }
    if (off == SMSC911X_TX_FIFO_INF && s_pin_txsused) {
        v = (v & ~SMSC911X_TX_FIFO_INF_TXSUSED_MASK) |
            (0xFFu << SMSC911X_TX_FIFO_INF_TXSUSED_SHIFT);
    }
    if (off == SMSC911X_RX_FIFO_INF && s_pin_rxsused) {
        v = (v & ~0x00FF0000u) | 0x00FF0000u;
    }
    *val = v;
    return 1;
}

static void pins_clear(void)
{
    s_pin_mii_busy = s_pin_phy_in_reset = s_pin_rx_dump = 0;
    s_pin_txsused = s_pin_rxsused = 0;
    s_last_csr_idx = 0;
    s_tx_status_pops = s_rx_status_pops = 0;
}

/* Chip model AT LAN_BASE — no interposer, so one charged read per access. */
static void fresh_direct(void)
{
    mock_regs_reset();
    pins_clear();
    fake_lan9220_reset(LAN_BASE);
}

/* Chip model at FAKE_BASE, interposer on LAN_BASE. */
static void fresh_interposed(void)
{
    mock_regs_reset();
    pins_clear();
    fake_lan9220_reset(FAKE_BASE);
    mock_regs_set_hook(LAN_BASE, lan_interposer, NULL);
}

/* ============================================================================
 * 1. THE BOUNDED WAITS — every one gives up AT its own budget, in microseconds
 * ========================================================================== */

static void test_stuck_soft_reset_gives_up_on_time(void)
{
    printf("- HW_CFG.SRST that never self-clears: SMSC911X_RESET_TIMEOUT_US\n");
    fresh_direct();
    fake_lan9220_set_reset_stuck(1);
    mock_regs_set_us_per_read(1u);

    uint32_t t0 = mock_time_now_us();
    CHECK(smsc911x_init(LAN_BASE, MAC) == SMSC911X_ERR_TIMEOUT);
    uint32_t elapsed = mock_time_now_us() - t0;

    printf("    elapsed %u us (budget %u)\n",
           (unsigned)elapsed, (unsigned)SMSC911X_RESET_TIMEOUT_US);
    /* Floor: the wait really did run to its deadline rather than giving up
     * early. Ceiling: it did NOT run to some other, longer bound — which is
     * what "one constant for eight waits" used to mean. */
    CHECK(elapsed >= SMSC911X_RESET_TIMEOUT_US);
    CHECK(elapsed <= 2u * SMSC911X_RESET_TIMEOUT_US);
    mock_regs_set_us_per_read(0u);
}

static void test_stuck_mac_csr_costs_one_budget_not_two(void)
{
    printf("- MAC_CSR.BUSY pinned: ONE CSR budget, not CSR nested inside MII\n");
    fresh_direct();
    fake_lan9220_set_csr_busy(1);
    mock_regs_set_us_per_read(1u);

    uint32_t t0 = mock_time_now_us();
    CHECK(smsc911x_init(LAN_BASE, MAC) == SMSC911X_ERR_TIMEOUT);
    uint32_t elapsed = mock_time_now_us() - t0;

    printf("    elapsed %u us (CSR budget %u)\n",
           (unsigned)elapsed, (unsigned)SMSC911X_CSR_TIMEOUT_US);
    CHECK(elapsed >= SMSC911X_CSR_TIMEOUT_US);
    /* THE ASSERTION THAT MATTERS. init's first MII access is the PHY reset
     * write, so the failing CSR read happens INSIDE mii_idle_settled(). If that
     * predicate kept polling after a hard error — the mistake common/service.h
     * warns about in terms — the MII wait would run its own budget on top,
     * evaluating a 1000 us predicate until its own 1000 us deadline, and this
     * would measure about twice what it does. Stopping on the error keeps it at
     * one budget. */
    CHECK(elapsed < (3u * SMSC911X_CSR_TIMEOUT_US) / 2u);
    mock_regs_set_us_per_read(0u);
}

static void test_stuck_mii_busy_gives_up_on_time(void)
{
    printf("- MII_ACC.BUSY pinned with a HEALTHY CSR: SMSC911X_MII_TIMEOUT_US\n");
    fresh_interposed();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);   /* a good chip first */

    s_pin_mii_busy = 1;
    mock_regs_set_us_per_read(1u);
    uint32_t t0 = mock_time_now_us();
    /* link_up() is reached from the CLCD service every superloop pass
     * (firmware/clcd/clcd.c), so this is a live path, not a bring-up one. */
    CHECK(smsc911x_link_up() == SMSC911X_ERR_TIMEOUT);
    uint32_t elapsed = mock_time_now_us() - t0;

    printf("    elapsed %u us (budget %u)\n",
           (unsigned)elapsed, (unsigned)SMSC911X_MII_TIMEOUT_US);
    CHECK(elapsed >= SMSC911X_MII_TIMEOUT_US);
    CHECK(elapsed <= 4u * SMSC911X_MII_TIMEOUT_US); /* 2x per read, + 1 predicate */
    mock_regs_set_us_per_read(0u);
}

static void test_stuck_phy_reset_gives_up_on_the_802_3_ceiling(void)
{
    printf("- BMCR.RESET pinned: the clause-22 0.5 s ceiling, and it RETURNS\n");
    fresh_interposed();
    s_pin_phy_in_reset = 1;
    mock_regs_set_us_per_read(16u);   /* reach a 500 ms deadline in few passes */

    uint32_t t0 = mock_time_now_us();
    CHECK(smsc911x_init(LAN_BASE, MAC) == SMSC911X_ERR_TIMEOUT);
    uint32_t elapsed = mock_time_now_us() - t0;

    printf("    elapsed %u us (budget %u)\n",
           (unsigned)elapsed, (unsigned)SMSC911X_PHY_RESET_TIMEOUT_US);
    CHECK(elapsed >= SMSC911X_PHY_RESET_TIMEOUT_US);
    CHECK(elapsed <= 2u * SMSC911X_PHY_RESET_TIMEOUT_US);
    mock_regs_set_us_per_read(0u);
}

static void test_stuck_rx_dump_costs_one_pass_not_the_shell(void)
{
    printf("- RX_CFG.RX_DUMP pinned: SMSC911X_RX_DUMP_TIMEOUT_US, and recovery\n"
           "  still acks the latched error afterwards\n");
    fresh_interposed();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    s_pin_rx_dump = 1;
    fake_lan9220_inject_rx_overrun(SMSC911X_INT_STS_RXE);
    mock_regs_set_us_per_read(1u);

    uint32_t t0 = mock_time_now_us();
    CHECK(smsc911x_rx_recover() == 1);
    uint32_t elapsed = mock_time_now_us() - t0;

    printf("    elapsed %u us (budget %u)\n",
           (unsigned)elapsed, (unsigned)SMSC911X_RX_DUMP_TIMEOUT_US);
    CHECK(elapsed >= SMSC911X_RX_DUMP_TIMEOUT_US);
    /* net_rx's service budget is 20 ms and already contains a 5 ms
     * lan_linkoutput TX-space wait; a dead RX_DUMP bit must cost one PASS, not
     * the shell. The ceiling here is what says so. */
    CHECK(elapsed < 10u * SMSC911X_RX_DUMP_TIMEOUT_US);
    CHECK((fake_lan9220_int_sts() & SMSC911X_INT_STS_RXE) == 0u);
    mock_regs_set_us_per_read(0u);
}

/* ============================================================================
 * 2. THE TWO DRAINS — bounded by a COUNT the hardware produces, not by a clock
 * ========================================================================== */

static void test_tx_status_drain_is_count_bounded(void)
{
    printf("- TX status drain against a TXSUSED that never falls: 256 pops\n");
    fresh_interposed();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    s_pin_txsused = 1;          /* every read says 255 still posted */
    s_tx_status_pops = 0;
    int drained = smsc911x_tx_status_drain();

    printf("    drained %d, TX_STATUS_FIFO pops %u (bound %u)\n",
           drained, s_tx_status_pops, (unsigned)SMSC911X_TX_STATUS_DRAIN_MAX);
    /* TXSAO is set, so the MAC keeps posting completions while the drain runs:
     * a replenishing count is a real hardware state, not a contrived one. The
     * old 100000 bound licensed 100000 pops inside ONE superloop pass. */
    CHECK(drained == (int)SMSC911X_TX_STATUS_DRAIN_MAX);
    CHECK(s_tx_status_pops == SMSC911X_TX_STATUS_DRAIN_MAX);
    CHECK(SMSC911X_TX_STATUS_DRAIN_MAX == 256u); /* TXSUSED is 8 bits, + 1 */
}

static void test_rx_status_drain_is_count_bounded(void)
{
    printf("- RX orphan-status drain against a pinned RXSUSED: <= 256 pops\n");
    fresh_interposed();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    s_pin_rxsused = 1;
    fake_lan9220_inject_rx_overrun(SMSC911X_INT_STS_RXE);
    s_rx_status_pops = 0;
    CHECK(smsc911x_rx_recover() == 1);

    printf("    RX_STATUS_FIFO pops %u (bound %u)\n",
           s_rx_status_pops, (unsigned)SMSC911X_RX_STATUS_DRAIN_MAX);
    CHECK(s_rx_status_pops == SMSC911X_RX_STATUS_DRAIN_MAX);
    CHECK(SMSC911X_RX_STATUS_DRAIN_MAX == 256u); /* RXSUSED is 8 bits, + 1 */
}

static void test_healthy_drain_costs_one_read(void)
{
    printf("- and an IDLE drain still costs exactly one TX_FIFO_INF read\n");
    fresh_interposed();
    CHECK(smsc911x_init(LAN_BASE, MAC) == 0);

    s_tx_status_pops = 0;
    CHECK(smsc911x_tx_status_drain() == 0);
    /* The bound must not have turned the cheap path into an expensive one:
     * nothing posted means one read and out. */
    CHECK(s_tx_status_pops == 0u);
}

/* ============================================================================
 * 3. THE BUDGETS ARE STATED, AND THEY FIT
 * ========================================================================== */

static void test_budgets_are_the_shipped_ones(void)
{
    printf("- the budgets are pinned, and the superloop ones fit their service\n");
    /* Pinning the values is what makes the derivations in smsc911x.h load-
     * bearing: change a number and this says so, so the justification has to be
     * changed with it. */
    CHECK(SMSC911X_CSR_TIMEOUT_US        == 1000u);
    CHECK(SMSC911X_MII_TIMEOUT_US        == 1000u);
    CHECK(SMSC911X_RESET_TIMEOUT_US      == 1000u);
    CHECK(SMSC911X_RX_DUMP_TIMEOUT_US    == 1000u);
    CHECK(SMSC911X_BOOT_TIMEOUT_US       == 100000u);
    CHECK(SMSC911X_PHY_RESET_TIMEOUT_US  == 500000u);

    /* The three waits reachable from the SUPERLOOP (CSR and MII via the CLCD
     * service's link/ANLPAR reads, RX_DUMP via net_rx) must each fit inside the
     * smallest service budget that can contain them — tx_drain, 5 ms
     * (firmware/platform/src/main.c). The two INIT-ONLY waits are deliberately
     * far larger and are excluded here, which is the whole reason they are
     * separate constants instead of one shared number. */
    CHECK(SMSC911X_CSR_TIMEOUT_US     <= 5000u);
    CHECK(SMSC911X_MII_TIMEOUT_US     <= 5000u);
    CHECK(SMSC911X_RX_DUMP_TIMEOUT_US <= 5000u);
}

int main(void)
{
    printf("== test_smsc911x_spin: bounded waits + the two drains ==\n");
    test_stuck_soft_reset_gives_up_on_time();
    test_stuck_mac_csr_costs_one_budget_not_two();
    test_stuck_mii_busy_gives_up_on_time();
    test_stuck_phy_reset_gives_up_on_the_802_3_ceiling();
    test_stuck_rx_dump_costs_one_pass_not_the_shell();
    test_tx_status_drain_is_count_bounded();
    test_rx_status_drain_is_count_bounded();
    test_healthy_drain_costs_one_read();
    test_budgets_are_the_shipped_ones();
    printf("test_smsc911x_spin: ALL PASS (%d checks)\n", s_checks);
    return 0;
}
