/*
 * fake_lan9220.c — see fake_lan9220.h. Models exactly the datasheet
 * behavior the driver's sequences depend on; everything else reads 0 /
 * ignores writes.
 */
#include <string.h>
#include "../smsc911x/smsc911x.h"
#include "mock_regs.h"
#include "fake_lan9220.h"

#define MAX_FRAMES 8
#define MAX_FRAME_BYTES 1600

typedef struct {
    int used;
    int len;
    int error;
    uint8_t data[MAX_FRAME_BYTES];
} frame_t;

static uint32_t s_id_rev;
static uint32_t s_byte_test;
static uint32_t s_hw_cfg;
static uint32_t s_tx_cfg, s_rx_cfg, s_int_en, s_afc_cfg;
static uint32_t s_soft_resets;
static uint32_t s_int_sts;   /* INT_STS: latched status; RX-error bits injectable */
static uint32_t s_rx_drop;   /* RX_DROP: dropped-frame counter (read-to-clear) */

static uint32_t s_mac_csr[16];
static uint16_t s_phy[32];
static int s_link_up;

/* Fault-injection knobs (see fake_lan9220.h). */
static uint32_t s_tx_free;       /* TX_FIFO_INF TDFREE (bytes) */
static int      s_reset_stuck;   /* HW_CFG.SRST never self-clears */
static int      s_csr_busy;      /* MAC_CSR_CMD reads BUSY forever */

/* TX reassembly */
typedef enum { TX_CMDA = 0, TX_CMDB, TX_DATA } tx_state_t;
static tx_state_t s_tx_state;
static uint32_t s_tx_cmda, s_tx_cmdb;
static uint32_t s_tx_got; /* payload bytes received so far */
static frame_t  s_tx_frames[MAX_FRAMES];
static frame_t  s_tx_cur;

/* TX STATUS FIFO: the MAC posts one status DWORD per fully-transmitted frame.
 * TX_FIFO_INF.TXSUSED reports how many are queued; TX_STATUS_FIFO pops the
 * oldest. If the driver never drains it, TXSUSED climbs — the model of the real
 * chip's un-drained-status back-pressure (here it just caps; the point under
 * test is that the driver KEEPS it drained). s_tx_status_post is the status word
 * stamped on each completed frame (0 = clean; injectable with error bits). */
#define MAX_TX_STATUS 16
static uint32_t s_tx_status[MAX_TX_STATUS];
static int      s_tx_status_used, s_tx_status_r, s_tx_status_w;
static uint32_t s_tx_status_post;

/* RX queue */
static frame_t s_rx_frames[MAX_FRAMES];
static int s_rx_cur = -1;   /* frame whose data words are being drained */
static uint32_t s_rx_word;  /* next word index in s_rx_cur */

static void phy_defaults(void)
{
    memset(s_phy, 0, sizeof(s_phy));
    s_phy[SMSC911X_PHY_BMCR] = SMSC911X_PHY_BMCR_ANEN;
    s_phy[SMSC911X_PHY_BMSR] = (uint16_t)(s_link_up ? SMSC911X_PHY_BMSR_LINK_UP : 0) | 0x7800u;
}

static int rx_pending_count(void)
{
    int n = 0;
    for (int i = 0; i < MAX_FRAMES; i++) {
        if (s_rx_frames[i].used) n++;
    }
    return n;
}

static int rx_oldest(void)
{
    for (int i = 0; i < MAX_FRAMES; i++) {
        if (s_rx_frames[i].used) return i; /* injection order == index order (no reuse races in tests) */
    }
    return -1;
}

static void mii_execute(void)
{
    uint32_t acc = s_mac_csr[SMSC911X_MAC_MII_ACC];
    if (!(acc & SMSC911X_MII_ACC_BUSY)) {
        return;
    }
    uint32_t reg = (acc >> 6) & 0x1Fu;
    if (acc & SMSC911X_MII_ACC_WRITE) {
        uint16_t v = (uint16_t)s_mac_csr[SMSC911X_MAC_MII_DATA];
        if (reg == SMSC911X_PHY_BMCR && (v & SMSC911X_PHY_BMCR_RESET)) {
            /* PHY soft reset: self-completes, registers back to defaults */
            phy_defaults();
        } else {
            s_phy[reg] = v;
        }
    } else {
        if (reg == SMSC911X_PHY_BMSR) {
            s_phy[reg] = (uint16_t)((s_link_up ? SMSC911X_PHY_BMSR_LINK_UP : 0) | 0x7800u);
        }
        s_mac_csr[SMSC911X_MAC_MII_DATA] = s_phy[reg];
    }
    s_mac_csr[SMSC911X_MAC_MII_ACC] = acc & ~SMSC911X_MII_ACC_BUSY; /* instant */
}

static void tx_word(uint32_t w)
{
    switch (s_tx_state) {
    case TX_CMDA:
        s_tx_cmda = w;
        s_tx_state = TX_CMDB;
        return;
    case TX_CMDB: {
        s_tx_cmdb = w;
        s_tx_cur.len = (int)(s_tx_cmdb & SMSC911X_TX_CMDB_LEN_MASK);
        s_tx_got = 0;
        /* driver invariant: single segment, sizes agree */
        if ((uint32_t)s_tx_cur.len != (s_tx_cmda & SMSC911X_TX_CMDA_BUFSZ_MASK)) {
            s_tx_cur.len = -1; /* poisoned: mismatch shows up as a -1 frame */
        }
        s_tx_state = (s_tx_cur.len != 0) ? TX_DATA : TX_CMDA;
        return;
    }
    case TX_DATA: {
        uint32_t n = (uint32_t)s_tx_cur.len - s_tx_got;
        if (n > 4) n = 4;
        if (s_tx_got + n <= MAX_FRAME_BYTES) {
            memcpy(&s_tx_cur.data[s_tx_got], &w, n);
        }
        s_tx_got += n;
        if (s_tx_got >= (uint32_t)s_tx_cur.len) {
            for (int i = 0; i < MAX_FRAMES; i++) {
                if (!s_tx_frames[i].used) {
                    s_tx_frames[i] = s_tx_cur;
                    s_tx_frames[i].used = 1;
                    break;
                }
            }
            /* Frame transmitted: post its completion status (independent of the
             * capture ring above — a full capture ring must not lose the status
             * word, exactly as on real silicon). Caps at MAX_TX_STATUS. */
            if (s_tx_status_used < MAX_TX_STATUS) {
                s_tx_status[s_tx_status_w] = s_tx_status_post;
                s_tx_status_w = (s_tx_status_w + 1) % MAX_TX_STATUS;
                s_tx_status_used++;
            }
            s_tx_state = TX_CMDA;
        }
        return;
    }
    }
}

static int lan9220_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write) {
        switch (off) {
        case SMSC911X_HW_CFG:
            if (*val & SMSC911X_HW_CFG_SRST) {
                s_soft_resets++;
                if (s_reset_stuck) {
                    /* Chip never comes out of reset: SRST stays set so the
                     * driver's reset wait_clear() times out. */
                    s_hw_cfg = *val;
                } else {
                    /* reset completes instantly: SRST reads back clear, and (as
                     * on the real chip) the FIFOs come up empty. */
                    s_hw_cfg = 0;
                    s_tx_state = TX_CMDA;
                    s_tx_status_used = s_tx_status_r = s_tx_status_w = 0;
                }
            } else {
                s_hw_cfg = *val;
            }
            return 1;
        case SMSC911X_TX_DATA_FIFO:
            tx_word(*val);
            return 1;
        case SMSC911X_MAC_CSR_DATA:
            s_mac_csr[0] = *val; /* staging slot: idx 0 unused as a real CSR */
            return 1;
        case SMSC911X_MAC_CSR_CMD: {
            uint32_t idx = *val & 0xFFu;
            if (*val & SMSC911X_MAC_CSR_CMD_BUSY) {
                if (*val & SMSC911X_MAC_CSR_CMD_READ) {
                    if (idx < 16 && idx != 0) {
                        s_mac_csr[0] = s_mac_csr[idx]; /* data readback slot */
                    }
                } else {
                    if (idx < 16 && idx != 0) {
                        s_mac_csr[idx] = s_mac_csr[0];
                        if (idx == SMSC911X_MAC_MII_ACC) {
                            mii_execute();
                        }
                    }
                }
            }
            return 1; /* BUSY self-clears: CMD reads back 0 (below) */
        }
        case SMSC911X_TX_CFG:  s_tx_cfg = *val;  return 1;
        case SMSC911X_RX_CFG:
            if (*val & SMSC911X_RX_CFG_RX_DUMP) {
                /* RX_DUMP: flush the whole RX data FIFO. The bit self-clears when
                 * the dump completes, so it never reads back set. */
                memset(s_rx_frames, 0, sizeof(s_rx_frames));
                s_rx_cur = -1;
                s_rx_word = 0;
                s_rx_cfg = *val & ~SMSC911X_RX_CFG_RX_DUMP;
            } else {
                s_rx_cfg = *val;
            }
            return 1;
        case SMSC911X_INT_EN:  s_int_en = *val;  return 1;
        case SMSC911X_INT_STS: s_int_sts &= ~*val; return 1; /* write-1-clear */
        case SMSC911X_AFC_CFG: s_afc_cfg = *val; return 1;
        default:
            return 1; /* everything else: accepted, ignored */
        }
    }

    switch (off) {
    case SMSC911X_BYTE_TEST:
        *val = s_byte_test;
        return 1;
    case SMSC911X_ID_REV:
        *val = s_id_rev;
        return 1;
    case SMSC911X_HW_CFG:
        *val = s_hw_cfg; /* SRST already clear = "reset done" */
        return 1;
    case SMSC911X_PMT_CTRL:
        *val = SMSC911X_PMT_CTRL_READY;
        return 1;
    case SMSC911X_E2P_CMD:
        *val = 0; /* EEPROM controller idle */
        return 1;
    case SMSC911X_MAC_CSR_CMD:
        /* Normally never busy (ops complete inline); csr_busy pins BUSY so the
         * MAC-CSR indirection's wait_clear() times out. */
        *val = s_csr_busy ? SMSC911X_MAC_CSR_CMD_BUSY : 0u;
        return 1;
    case SMSC911X_MAC_CSR_DATA:
        *val = s_mac_csr[0];
        return 1;
    case SMSC911X_TX_FIFO_INF:
        /* TDFREE (bytes, [15:0], injectable low) + TXSUSED (status FIFO used
         * count, [23:16]) — the driver's tx-status drain reads this to know how
         * many completion words to pop. */
        *val = (s_tx_free & SMSC911X_TX_FIFO_INF_TDFREE_MASK)
             | (((uint32_t)s_tx_status_used << SMSC911X_TX_FIFO_INF_TXSUSED_SHIFT)
                & SMSC911X_TX_FIFO_INF_TXSUSED_MASK);
        return 1;
    case SMSC911X_TX_STATUS_FIFO:
        /* Pop the oldest completion status; 0 when empty (matches silicon: a
         * read of an empty TX status FIFO returns 0). */
        if (s_tx_status_used == 0) {
            *val = 0;
        } else {
            *val = s_tx_status[s_tx_status_r];
            s_tx_status_r = (s_tx_status_r + 1) % MAX_TX_STATUS;
            s_tx_status_used--;
        }
        return 1;
    case SMSC911X_RX_FIFO_INF: {
        int n = rx_pending_count();
        uint32_t bytes = 0;
        for (int i = 0; i < MAX_FRAMES; i++) {
            if (s_rx_frames[i].used) bytes += ((uint32_t)s_rx_frames[i].len + 3u) & ~3u;
        }
        *val = ((uint32_t)n << 16) | (bytes & 0xFFFFu);
        return 1;
    }
    case SMSC911X_RX_STATUS_FIFO: {
        int idx = rx_oldest();
        if (idx < 0) {
            *val = 0;
            return 1;
        }
        s_rx_cur = idx;
        s_rx_word = 0;
        *val = ((uint32_t)s_rx_frames[idx].len << 16)
             | (s_rx_frames[idx].error ? SMSC911X_RX_STS_ERROR : 0);
        return 1;
    }
    case SMSC911X_RX_DATA_FIFO: {
        if (s_rx_cur < 0) {
            *val = 0;
            return 1;
        }
        frame_t *f = &s_rx_frames[s_rx_cur];
        uint32_t w = 0;
        uint32_t offb = 4u * s_rx_word;
        if ((int)offb < f->len) {
            uint32_t n = (uint32_t)f->len - offb;
            if (n > 4) n = 4;
            memcpy(&w, &f->data[offb], n);
        }
        s_rx_word++;
        if (4u * s_rx_word >= (((uint32_t)f->len + 3u) & ~3u)) {
            f->used = 0; /* fully drained */
            s_rx_cur = -1;
        }
        *val = w;
        return 1;
    }
    case SMSC911X_TX_CFG:  *val = s_tx_cfg;  return 1;
    case SMSC911X_RX_CFG:  *val = s_rx_cfg;  return 1;
    case SMSC911X_INT_EN:  *val = s_int_en;  return 1;
    case SMSC911X_INT_STS: *val = s_int_sts; return 1;
    case SMSC911X_RX_DROP:
        *val = s_rx_drop; /* dropped-frame counter is read-to-clear */
        s_rx_drop = 0;
        return 1;
    case SMSC911X_AFC_CFG: *val = s_afc_cfg; return 1;
    default:
        *val = 0;
        return 1;
    }
}

void fake_lan9220_reset(uintptr_t base)
{
    s_id_rev = 0x92200002u;
    s_byte_test = SMSC911X_BYTE_TEST_PATTERN;
    s_hw_cfg = 0;
    s_tx_cfg = s_rx_cfg = s_int_en = s_afc_cfg = 0;
    s_soft_resets = 0;
    s_int_sts = 0;
    s_rx_drop = 0;
    s_tx_free = 4096u; /* plenty of TDFREE by default */
    s_reset_stuck = 0;
    s_csr_busy = 0;
    memset(s_mac_csr, 0, sizeof(s_mac_csr));
    s_link_up = 1;
    phy_defaults();
    s_tx_state = TX_CMDA;
    s_tx_got = 0;
    memset(s_tx_frames, 0, sizeof(s_tx_frames));
    memset(&s_tx_cur, 0, sizeof(s_tx_cur));
    memset(s_tx_status, 0, sizeof(s_tx_status));
    s_tx_status_used = s_tx_status_r = s_tx_status_w = 0;
    s_tx_status_post = 0; /* clean completion status by default */
    memset(s_rx_frames, 0, sizeof(s_rx_frames));
    s_rx_cur = -1;
    s_rx_word = 0;
    mock_regs_set_hook((uint32_t)base, lan9220_hook, 0);
}

void fake_lan9220_set_id_rev(uint32_t id_rev)  { s_id_rev = id_rev; }
void fake_lan9220_set_byte_test(uint32_t val)  { s_byte_test = val; }

void fake_lan9220_set_link(int up)
{
    s_link_up = up;
    s_phy[SMSC911X_PHY_BMSR] = (uint16_t)((up ? SMSC911X_PHY_BMSR_LINK_UP : 0) | 0x7800u);
}

void fake_lan9220_set_tx_free(uint32_t free_bytes) { s_tx_free = free_bytes; }
void fake_lan9220_set_reset_stuck(int stuck)       { s_reset_stuck = stuck; }
void fake_lan9220_set_csr_busy(int stuck)          { s_csr_busy = stuck; }

void fake_lan9220_set_tx_status(uint32_t status)   { s_tx_status_post = status; }
int  fake_lan9220_tx_status_used(void)             { return s_tx_status_used; }
uint32_t fake_lan9220_tx_cfg(void)                 { return s_tx_cfg; }

void fake_lan9220_inject_rx_overrun(uint32_t int_sts_bits)
{
    s_int_sts |= int_sts_bits;      /* latch the RX error/overrun status */
    s_rx_drop++;                    /* and bump the dropped-frame counter */
}

uint32_t fake_lan9220_int_sts(void) { return s_int_sts; }

uint32_t fake_lan9220_soft_resets(void) { return s_soft_resets; }
uint32_t fake_lan9220_mac_csr(uint32_t idx) { return (idx < 16) ? s_mac_csr[idx] : 0; }
uint16_t fake_lan9220_phy_reg(uint32_t reg) { return (reg < 32) ? s_phy[reg] : 0; }

int fake_lan9220_take_tx(void *buf, int cap)
{
    for (int i = 0; i < MAX_FRAMES; i++) {
        if (s_tx_frames[i].used) {
            int n = s_tx_frames[i].len;
            int c = (n < cap) ? n : cap;
            if (c > 0) {
                memcpy(buf, s_tx_frames[i].data, (size_t)c);
            }
            s_tx_frames[i].used = 0;
            return n;
        }
    }
    return -1;
}

int fake_lan9220_tx_count(void)
{
    int n = 0;
    for (int i = 0; i < MAX_FRAMES; i++) {
        if (s_tx_frames[i].used) n++;
    }
    return n;
}

int fake_lan9220_inject_rx(const void *frame, int len, int error)
{
    if (len > MAX_FRAME_BYTES) {
        return -1;
    }
    for (int i = 0; i < MAX_FRAMES; i++) {
        if (!s_rx_frames[i].used) {
            s_rx_frames[i].used = 1;
            s_rx_frames[i].len = len;
            s_rx_frames[i].error = error;
            memcpy(s_rx_frames[i].data, frame, (size_t)len);
            return 0;
        }
    }
    return -1;
}
