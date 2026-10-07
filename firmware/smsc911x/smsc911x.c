/*
 * smsc911x.c — bare-metal LAN9220 driver (W-SMSC). See smsc911x.h's
 * LICENSE PROVENANCE block: register sequences ported from Zephyr's
 * drivers/ethernet/eth_smsc911x.c (Apache-2.0), re-expressed against this
 * repo's HAL; no Zephyr code copied verbatim.
 *
 * Everything here is a plain mps3_reg_read32/write32 against the base
 * handed to smsc911x_init() — which is exactly what makes
 * firmware/test/test_smsc911x.c able to run these sequences against
 * fake_lan9220.c's chip model with -DMPS3_HAL_MOCK.
 */
#include <string.h>
#include "smsc911x.h"
#include "../common/platform_regs.h" /* the four HAL accessors only */
#include "../common/service.h"       /* mps3_spin_until — a bound in MICROSECONDS */

/* SMSC911X_POLL_BOUND (100000 iterations, for every wait in this file) is GONE.
 * The waits are now microseconds off the free-running AXI timer and the two
 * drains carry a count derived from the register field that produces it — the
 * constants, and the derivation of every one of them, are in smsc911x.h. */

static uintptr_t s_base;

/* Free-running RX-recovery diagnostics (read via smsc911x_get_diag()): how often
 * smsc911x_rx_recover() fired, how many of those took the hard RXE|RWT FIFO-dump
 * path, and the running total of frames the MAC dropped to overrun (RX_DROP).
 * Never reset after init — a monotonic history the shell surfaces on the wire +
 * at the JTAG-readable diag mailbox to show whether recovery is firing at the
 * stall point. */
static uint32_t s_diag_recover_events;
static uint32_t s_diag_recover_dumps;
static uint32_t s_diag_drop_frames;

/* Free-running TX diagnostics (read via smsc911x_get_tx_diag()): frames accepted
 * into the TX DATA FIFO, TX status words reaped from the TX STATUS FIFO, and how
 * many of those flagged a transmit error. status_drained tracking frames_sent
 * (and TXSUSED never pinned) is the JTAG-visible proof the un-drained-status TX
 * stall is cured. Never reset after init. */
static uint32_t s_diag_tx_frames;
static uint32_t s_diag_tx_status_drained;
static uint32_t s_diag_tx_errors;
static uint32_t s_diag_tx_last_status; /* raw last popped TX status word (decode ground truth) */

static uint32_t rd(uint32_t off)              { return mps3_reg_read32(s_base, off); }
static void     wr(uint32_t off, uint32_t v)  { mps3_reg_write32(s_base, off, v); }

/* The two generic register waits. Each call site now states its OWN budget
 * (smsc911x.h says where each number comes from) instead of sharing one
 * unitless iteration count: a 10 us reset and a 500 ms PHY reset are not the
 * same wait and must not be spelled the same way. */
typedef struct {
    uint32_t off;
    uint32_t mask;
} reg_wait_ctx_t;

static int reg_mask_is_clear(void *vctx)
{
    const reg_wait_ctx_t *c = (const reg_wait_ctx_t *)vctx;
    return (rd(c->off) & c->mask) == 0u;
}

static int reg_mask_is_set(void *vctx)
{
    const reg_wait_ctx_t *c = (const reg_wait_ctx_t *)vctx;
    return (rd(c->off) & c->mask) != 0u;
}

static int wait_clear(uint32_t off, uint32_t mask, uint32_t timeout_us)
{
    reg_wait_ctx_t c = { off, mask };
    return mps3_spin_until(reg_mask_is_clear, &c, timeout_us) ? 0
                                                             : SMSC911X_ERR_TIMEOUT;
}

static int wait_set(uint32_t off, uint32_t mask, uint32_t timeout_us)
{
    reg_wait_ctx_t c = { off, mask };
    return mps3_spin_until(reg_mask_is_set, &c, timeout_us) ? 0
                                                            : SMSC911X_ERR_TIMEOUT;
}

/* ---- MAC CSR indirection ----------------------------------------------------- */

static int mac_csr_write(uint32_t idx, uint32_t val)
{
    if (wait_clear(SMSC911X_MAC_CSR_CMD, SMSC911X_MAC_CSR_CMD_BUSY,
                   SMSC911X_CSR_TIMEOUT_US) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    wr(SMSC911X_MAC_CSR_DATA, val);
    wr(SMSC911X_MAC_CSR_CMD, SMSC911X_MAC_CSR_CMD_BUSY | (idx & 0xFFu));
    return wait_clear(SMSC911X_MAC_CSR_CMD, SMSC911X_MAC_CSR_CMD_BUSY,
                      SMSC911X_CSR_TIMEOUT_US);
}

static int mac_csr_read(uint32_t idx, uint32_t *out)
{
    if (wait_clear(SMSC911X_MAC_CSR_CMD, SMSC911X_MAC_CSR_CMD_BUSY,
                   SMSC911X_CSR_TIMEOUT_US) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    wr(SMSC911X_MAC_CSR_CMD,
       SMSC911X_MAC_CSR_CMD_BUSY | SMSC911X_MAC_CSR_CMD_READ | (idx & 0xFFu));
    if (wait_clear(SMSC911X_MAC_CSR_CMD, SMSC911X_MAC_CSR_CMD_BUSY,
                   SMSC911X_CSR_TIMEOUT_US) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    *out = rd(SMSC911X_MAC_CSR_DATA);
    return 0;
}

/* ---- MII (internal PHY) via the MAC's MII_ACC/MII_DATA CSRs ------------------- */

/* MII_ACC.BUSY, read through the MAC-CSR indirection. The predicate STOPS ON
 * EVERY OUTCOME, not just the good one (common/service.h): a MAC-CSR read that
 * has itself timed out will never start answering, so continuing to spin on it
 * would burn this budget ON TOP of the CSR budget already lost, inside a poll
 * the CLCD service calls every pass (firmware/clcd/clcd.c reads the PHY's link
 * and ANLPAR). The outcome is carried out in the context because the caller has
 * to tell "still busy at the deadline" from "the bus underneath died". */
typedef struct {
    int      rc;   /* last mac_csr_read() result */
    uint32_t acc;
} mii_idle_ctx_t;

static int mii_idle_settled(void *vctx)
{
    mii_idle_ctx_t *c = (mii_idle_ctx_t *)vctx;
    c->rc = mac_csr_read(SMSC911X_MAC_MII_ACC, &c->acc);
    if (c->rc != 0) {
        return 1; /* hard error — end the wait, do not keep polling a dead bus */
    }
    return (c->acc & SMSC911X_MII_ACC_BUSY) == 0u;
}

static int mii_wait_idle(void)
{
    mii_idle_ctx_t c = { 0, 0u };
    if (!mps3_spin_until(mii_idle_settled, &c, SMSC911X_MII_TIMEOUT_US)) {
        return SMSC911X_ERR_TIMEOUT; /* still BUSY at the deadline */
    }
    return (c.rc != 0) ? SMSC911X_ERR_TIMEOUT : 0;
}

int smsc911x_mii_write(uint32_t phy_reg, uint16_t val)
{
    int rc = mii_wait_idle();
    if (rc != 0) {
        return rc;
    }
    if (mac_csr_write(SMSC911X_MAC_MII_DATA, val) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    if (mac_csr_write(SMSC911X_MAC_MII_ACC,
                      (SMSC911X_PHY_ADDR << 11) | ((phy_reg & 0x1Fu) << 6) |
                      SMSC911X_MII_ACC_WRITE | SMSC911X_MII_ACC_BUSY) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    return mii_wait_idle();
}

int smsc911x_mii_read(uint32_t phy_reg, uint16_t *val_out)
{
    int rc = mii_wait_idle();
    if (rc != 0) {
        return rc;
    }
    if (mac_csr_write(SMSC911X_MAC_MII_ACC,
                      (SMSC911X_PHY_ADDR << 11) | ((phy_reg & 0x1Fu) << 6) |
                      SMSC911X_MII_ACC_BUSY) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    rc = mii_wait_idle();
    if (rc != 0) {
        return rc;
    }
    uint32_t data;
    if (mac_csr_read(SMSC911X_MAC_MII_DATA, &data) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    *val_out = (uint16_t)data;
    return 0;
}

int smsc911x_link_up(void)
{
    uint16_t bmsr;
    int rc = smsc911x_mii_read(SMSC911X_PHY_BMSR, &bmsr);
    if (rc != 0) {
        return rc;
    }
    return (bmsr & SMSC911X_PHY_BMSR_LINK_UP) ? 1 : 0;
}

uint32_t smsc911x_id_rev(void)
{
    return rd(SMSC911X_ID_REV);
}

/* ---- init (the Zephyr-derived bring-up order) ---------------------------------- */

/* BMCR.RESET self-clear, step 6 of init. Like mii_idle_settled() the predicate
 * ends the wait on a hard MII error as well as on success — otherwise a dead
 * MAC-CSR bus would hold this one for the FULL half second that IEEE 802.3
 * clause 22 allows a PHY reset, on a condition that will never change. */
typedef struct {
    int      rc;     /* last smsc911x_mii_read() result */
    uint16_t bmcr;
} phy_reset_ctx_t;

static int phy_reset_cleared(void *vctx)
{
    phy_reset_ctx_t *c = (phy_reset_ctx_t *)vctx;
    c->rc = smsc911x_mii_read(SMSC911X_PHY_BMCR, &c->bmcr);
    if (c->rc != 0) {
        return 1; /* hard error — stop, and let the caller fail closed */
    }
    return (c->bmcr & SMSC911X_PHY_BMCR_RESET) == 0u;
}

int smsc911x_init(uintptr_t base, const uint8_t mac[6])
{
    s_base = base;

    /* 1. Bus sanity: BYTE_TEST reads the fixed pattern regardless of chip
     *    state — anything else means the static-memory-bus wiring or byte
     *    lanes are wrong (fail before touching anything). */
    if (rd(SMSC911X_BYTE_TEST) != SMSC911X_BYTE_TEST_PATTERN) {
        return SMSC911X_ERR_BYTE_TEST;
    }

    /* 2. Chip identification. */
    if ((rd(SMSC911X_ID_REV) >> 16) != SMSC911X_ID_LAN9220) {
        return SMSC911X_ERR_ID;
    }

    /* 3. Soft reset + wait for completion, then re-check the bus. */
    wr(SMSC911X_HW_CFG, SMSC911X_HW_CFG_SRST);
    if (wait_clear(SMSC911X_HW_CFG, SMSC911X_HW_CFG_SRST,
                   SMSC911X_RESET_TIMEOUT_US) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    if (rd(SMSC911X_BYTE_TEST) != SMSC911X_BYTE_TEST_PATTERN) {
        return SMSC911X_ERR_BYTE_TEST;
    }

    /* 4. Power-management ready + EEPROM auto-load finished (the MAC
     *    address registers are stable only after E2P settles). */
    if (wait_set(SMSC911X_PMT_CTRL, SMSC911X_PMT_CTRL_READY,
                 SMSC911X_BOOT_TIMEOUT_US) != 0 ||
        wait_clear(SMSC911X_E2P_CMD, SMSC911X_E2P_CMD_BUSY,
                   SMSC911X_BOOT_TIMEOUT_US) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }

    /* 5. FIFO sizing (5 KB TX FIFO — the family default the Zephyr driver
     *    uses) + flow-control thresholds. */
    wr(SMSC911X_HW_CFG, SMSC911X_HW_CFG_MBO | SMSC911X_HW_CFG_TX_FIF_SZ(5));
    wr(SMSC911X_AFC_CFG, 0x006E3740u); /* family-datasheet recommended AFC */

    /* 6. PHY: reset the internal PHY, then (re)start autonegotiation. */
    if (smsc911x_mii_write(SMSC911X_PHY_BMCR, SMSC911X_PHY_BMCR_RESET) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    {
        phy_reset_ctx_t prc = { 0, 0u };
        if (!mps3_spin_until(phy_reset_cleared, &prc,
                             SMSC911X_PHY_RESET_TIMEOUT_US) || prc.rc != 0) {
            return SMSC911X_ERR_TIMEOUT;
        }
    }
    if (smsc911x_mii_write(SMSC911X_PHY_BMCR,
                           SMSC911X_PHY_BMCR_ANEN | SMSC911X_PHY_BMCR_ANRST) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }

    /* 7. MAC address (firmware-assigned — overrides any EEPROM value; the
     *    MAC-address SOURCE question in the porting notes is resolved as
     *    "caller provides", keeping provisioning host/manifest-driven). */
    if (mac_csr_write(SMSC911X_MAC_ADDRL,
                      (uint32_t)mac[0] | ((uint32_t)mac[1] << 8) |
                      ((uint32_t)mac[2] << 16) | ((uint32_t)mac[3] << 24)) != 0 ||
        mac_csr_write(SMSC911X_MAC_ADDRH,
                      (uint32_t)mac[4] | ((uint32_t)mac[5] << 8)) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }

    /* 8. Interrupts fully masked + latched status cleared (poll model —
     *    README point 5). */
    wr(SMSC911X_INT_EN, 0);
    wr(SMSC911X_INT_STS, 0xFFFFFFFFu);

    /* 9. Enables: TX datapath, then MAC TX/RX with PROMISCUOUS set —
     *    ARCHITECTURE_SPEC §8.3: the one LAN9220 port carries both the
     *    shell's and the DUT's MAC addresses, so filtering must be off
     *    (the Zephyr driver's config-optional promisc bit, forced on
     *    here). */
    /* TX datapath on, PLUS TXSAO (status allow-overrun): a full TX status FIFO
     * then discards its oldest status instead of HALTING the transmitter. This
     * is belt-and-braces to the primary fix (smsc911x_tx_frame drains the status
     * FIFO every transmit + the superloop drains it every poll, so it should
     * never approach full) — it removes the MAC-halt failure mode even if a
     * pathological burst outruns the drain. The drain still pops real status
     * words on the normal path, so tx_errors stays meaningful. */
    wr(SMSC911X_TX_CFG, SMSC911X_TX_CFG_TX_ON | SMSC911X_TX_CFG_TXSAO);
    if (mac_csr_write(SMSC911X_MAC_CR,
                      SMSC911X_MAC_CR_TXEN | SMSC911X_MAC_CR_RXEN |
                      SMSC911X_MAC_CR_PRMS) != 0) {
        return SMSC911X_ERR_TIMEOUT;
    }
    wr(SMSC911X_RX_CFG, 0);

    return 0;
}

/* ---- TX ------------------------------------------------------------------------ */

int smsc911x_tx_status_drain(void)
{
    int drained = 0;
    /* A DRAIN, NOT A WAIT — deliberately still a COUNT bound, and not converted
     * to mps3_spin_until(). There is no condition here that becomes true and no
     * duration worth losing: the loop pops items until the hardware says there
     * are none, one bus access per item. The quantity the hardware bounds is the
     * COUNT, and it is exactly TXSUSED's field width — 8 bits, TX_FIFO_INF
     * [23:16], so at most 255 can be posted-and-unread and each pop decrements
     * it. 256 is one more than the deepest legitimate drain.
     *
     * The old bound was 100000, and that was not merely imprecise. TXSAO is set
     * (init step 9), so the MAC keeps transmitting and keeps POSTING completions
     * while this runs: under sustained TX the count is replenished as fast as it
     * is popped, and 100000 licensed that livelock to hold one superloop pass.
     * At 256 the drain always returns, `drained` says what it took, and the
     * tx_drain service comes straight back round. See smsc911x.h. */
    for (unsigned i = 0; i < SMSC911X_TX_STATUS_DRAIN_MAX; i++) {
        uint32_t used = (rd(SMSC911X_TX_FIFO_INF) & SMSC911X_TX_FIFO_INF_TXSUSED_MASK)
                        >> SMSC911X_TX_FIFO_INF_TXSUSED_SHIFT;
        if (used == 0) {
            break; /* status FIFO empty — nothing posted (one TX_FIFO_INF read) */
        }
        uint32_t sts = rd(SMSC911X_TX_STATUS_FIFO); /* pop one completion status */
        s_diag_tx_status_drained++;
        if (sts != 0u) {
            s_diag_tx_last_status = sts; /* last NON-ZERO raw word: an empty-FIFO pop reads 0 and tells us nothing */
        }
        if ((sts & SMSC911X_TX_STS_ES) &&
            (sts & SMSC911X_TX_STS_REAL_ERR_MASK)) {
            /* ES alone is not enough: on this board every good frame reports
             * ES|NO_CARR (0x8400, measured). Only a non-carrier cause -- a
             * collision or deferral -- is a real transmit failure. */
            s_diag_tx_errors++;
        }
        drained++;
    }
    return drained;
}

int smsc911x_tx_frame(const void *frame, uint32_t len)
{
    if (len == 0 || len > SMSC911X_MAX_FRAME) {
        return SMSC911X_ERR_TOO_BIG;
    }
    /* Reap completed TX status FIRST: the MAC posts one status DWORD per
     * transmitted frame, and an un-drained status FIFO back-pressures / halts
     * TX (the HW stall). Draining here means every transmit keeps TXSUSED near
     * zero, so the TX DATA FIFO never backs up on stale completion records. */
    (void)smsc911x_tx_status_drain();

    /* Room check: frame words + the two command words must fit the TX DATA FIFO
     * free space (TDFREE, TX_FIFO_INF[15:0]). Never write a frame that doesn't
     * fit — a partial write would leave a torn frame the MAC can't parse. */
    uint32_t needed = ((len + 3u) & ~3u) + 8u;
    if ((rd(SMSC911X_TX_FIFO_INF) & SMSC911X_TX_FIFO_INF_TDFREE_MASK) < needed) {
        return SMSC911X_ERR_TX_SPACE;
    }

    /* Single-segment frame: first+last in one buffer, no start offset. */
    wr(SMSC911X_TX_DATA_FIFO,
       SMSC911X_TX_CMDA_FIRST_SEG | SMSC911X_TX_CMDA_LAST_SEG |
       (len & SMSC911X_TX_CMDA_BUFSZ_MASK));
    wr(SMSC911X_TX_DATA_FIFO, (len & SMSC911X_TX_CMDB_LEN_MASK)); /* tag 0 */

    const uint8_t *p = (const uint8_t *)frame;
    for (uint32_t i = 0; i < len; i += 4) {
        uint32_t w = 0;
        uint32_t n = len - i;
        if (n > 4) {
            n = 4;
        }
        memcpy(&w, p + i, n); /* little-endian byte lanes, per the bus mode
                               * BYTE_TEST verified */
        wr(SMSC911X_TX_DATA_FIFO, w);
    }
    s_diag_tx_frames++;
    return 0;
}

void smsc911x_get_tx_diag(uint32_t *frames_sent, uint32_t *status_drained,
                          uint32_t *errors)
{
    if (frames_sent)    *frames_sent    = s_diag_tx_frames;
    if (status_drained) *status_drained = s_diag_tx_status_drained;
    if (errors)         *errors         = s_diag_tx_errors;
}

/* Raw last-popped TX completion status word. Exposed purely so the bit layout can
 * be decoded from silicon instead of assumed: gating tx_errors on the 911x-family
 * cause bits, and then on bit15 (ES), BOTH flagged ~all healthy frames on this
 * LAN9220 while TX was demonstrably perfect (1.31 MB, 0 drops). Read it over JTAG. */
uint32_t smsc911x_tx_last_status(void)
{
    return s_diag_tx_last_status;
}

/* ---- RX ------------------------------------------------------------------------ */

int smsc911x_rx_recover(void)
{
    uint32_t err = rd(SMSC911X_INT_STS) & SMSC911X_INT_STS_RX_ERR_MASK;
    if (err == 0) {
        return 0; /* no RX overrun/error latched — nothing to do (one read) */
    }
    s_diag_recover_events++;

    /* A hard RX error / watchdog timeout can leave the RX DATA and STATUS FIFOs
     * out of step (a clean dropped-frame overrun does not — the MAC drops whole
     * frames). Resync by flushing the RX data FIFO (RX_DUMP self-clears when
     * done) and popping any orphaned status words back to an empty, aligned
     * state. Both waits are bounded so a wedged core can't spin here forever. */
    if (err & (SMSC911X_INT_STS_RXE | SMSC911X_INT_STS_RWT)) {
        s_diag_recover_dumps++;
        wr(SMSC911X_RX_CFG, SMSC911X_RX_CFG_RX_DUMP);
        (void)wait_clear(SMSC911X_RX_CFG, SMSC911X_RX_CFG_RX_DUMP,
                         SMSC911X_RX_DUMP_TIMEOUT_US);
        /* The SECOND drain, same treatment and the same reason (smsc911x.h):
         * popping orphaned status words is not a wait, so it keeps a COUNT
         * bound — RXSUSED is RX_FIFO_INF[23:16], 8 bits, so 256 is one more
         * than can ever be pending. Note this one runs in the net_rx service
         * path, under a 20 ms budget it shares with a 5 ms TX-space wait; the
         * old 100000 could not fit in that budget and nothing said so. */
        for (unsigned i = 0; i < SMSC911X_RX_STATUS_DRAIN_MAX; i++) {
            if (((rd(SMSC911X_RX_FIFO_INF) >> 16) & 0xFFu) == 0) {
                break; /* status FIFO drained */
            }
            (void)rd(SMSC911X_RX_STATUS_FIFO);
        }
    }

    /* Always ack the latched RX status (write-1-clear) + drain the dropped-frame
     * counter (read-to-clear, accumulated for the diagnostic) so neither masks a
     * later event; MAC RXEN is left enabled so reception continues (TCP
     * retransmits the frames lost to the overrun). */
    wr(SMSC911X_INT_STS, err);
    s_diag_drop_frames += rd(SMSC911X_RX_DROP);
    return 1;
}

void smsc911x_get_diag(uint32_t *recover_events, uint32_t *recover_dumps,
                       uint32_t *drop_frames)
{
    if (recover_events) *recover_events = s_diag_recover_events;
    if (recover_dumps)  *recover_dumps  = s_diag_recover_dumps;
    if (drop_frames)    *drop_frames    = s_diag_drop_frames;
}

int smsc911x_rx_frame(void *buf, uint32_t cap)
{
    /* Clear a latched RX-FIFO overrun before reading: a long busy period (e.g. a
     * HWICAP streaming burst) can overrun the MAC RX FIFO, and if the error is
     * never acked RX wedges and config_agent starves (HARDWARE EVIDENCE). No-op
     * (one INT_STS read) on the healthy path. */
    (void)smsc911x_rx_recover();

    uint32_t inf = rd(SMSC911X_RX_FIFO_INF);
    if (((inf >> 16) & 0xFFu) == 0) {
        return 0; /* no status words pending = no frame */
    }

    uint32_t sts = rd(SMSC911X_RX_STATUS_FIFO);
    uint32_t len = SMSC911X_RX_STS_LEN(sts);
    uint32_t words = (len + 3u) / 4u;

    /* len == 0 is only produced by a desynced/garbage status word (a valid
     * Ethernet frame is >= 64 B): treat it as an error-drop (words == 0, nothing
     * to drain) rather than returning 0, which the caller would read as
     * "FIFO empty" and stop draining — silently re-wedging RX. */
    if ((sts & SMSC911X_RX_STS_ERROR) || len == 0 || len > cap || len > SMSC911X_MAX_FRAME) {
        /* Consume + drop: pop the data words so the FIFO stays aligned.
         * (The RX_DP_CTRL fast-forward shortcut exists for frames >= 16
         * bytes; plain draining is unconditional and branch-free.) */
        for (uint32_t i = 0; i < words; i++) {
            (void)rd(SMSC911X_RX_DATA_FIFO);
        }
        return (len > cap || len > SMSC911X_MAX_FRAME) ? SMSC911X_ERR_TOO_BIG
                                                       : SMSC911X_ERR_RX_ERROR;
    }

    uint8_t *p = (uint8_t *)buf;
    for (uint32_t i = 0; i < words; i++) {
        uint32_t w = rd(SMSC911X_RX_DATA_FIFO);
        uint32_t n = len - 4u * i;
        if (n > 4) {
            n = 4;
        }
        memcpy(p + 4u * i, &w, n);
    }
    return (int)len;
}
