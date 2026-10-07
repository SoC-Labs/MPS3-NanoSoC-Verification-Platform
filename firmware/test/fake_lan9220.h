/*
 * fake_lan9220.h — behavioral mock of the LAN9220 host interface (mock_regs
 * hook): BYTE_TEST/ID_REV, self-completing soft reset, PMT/E2P readiness,
 * MAC-CSR + MII indirection with a minimal internal-PHY register file,
 * TX-FIFO command-word framing (frames reassembled for assertions), and an
 * injectable RX FIFO. Backs firmware/test/test_smsc911x.c (W-SMSC).
 */
#ifndef MPS3_FAKE_LAN9220_H
#define MPS3_FAKE_LAN9220_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Reset the model and install its hook on `base` (any page not already
 * hooked; call after mock_regs_reset()). */
void fake_lan9220_reset(uintptr_t base);

/* Behavior knobs */
void fake_lan9220_set_id_rev(uint32_t id_rev);        /* default 0x92200002 */
void fake_lan9220_set_byte_test(uint32_t val);        /* default the real pattern */
void fake_lan9220_set_link(int up);                    /* PHY BMSR link bit */

/* Fault injection (W-SMSC): make the driver's bounded polls reachable.
 *   tx_free:      TX_FIFO_INF TDFREE (low 16 bits) in bytes — default large. Set
 *                 below (frame_words*4 + 8) so smsc911x_tx_frame() sees no room
 *                 and returns SMSC911X_ERR_TX_SPACE without touching the FIFO.
 *   reset_stuck:  when nonzero, HW_CFG.SRST does NOT self-clear, so
 *                 smsc911x_init()'s reset wait_clear() exhausts its poll bound
 *                 and returns SMSC911X_ERR_TIMEOUT (models a chip that never
 *                 comes out of reset).
 *   csr_busy:     when nonzero, MAC_CSR_CMD reads back BUSY forever, so the MAC
 *                 CSR indirection's wait_clear() times out — every mac_csr_*()
 *                 (MAC address, MAC_CR, MII) fails SMSC911X_ERR_TIMEOUT. */
void fake_lan9220_set_tx_free(uint32_t free_bytes);
void fake_lan9220_set_reset_stuck(int stuck);
void fake_lan9220_set_csr_busy(int stuck);

/* Model an RX-FIFO overrun: latch the given INT_STS RX-error bits (e.g.
 * SMSC911X_INT_STS_RXE | SMSC911X_INT_STS_RXDF_INT) and bump the dropped-frame
 * counter, exactly as the LAN9220 does when its RX FIFO overflows because the
 * driver didn't drain it in time. smsc911x_rx_recover() must detect + clear it. */
void fake_lan9220_inject_rx_overrun(uint32_t int_sts_bits);

/* Observability */
uint32_t fake_lan9220_soft_resets(void);               /* HW_CFG.SRST count */
uint32_t fake_lan9220_int_sts(void);                   /* INT_STS latched value */
uint32_t fake_lan9220_mac_csr(uint32_t idx);           /* MAC CSR file peek */
uint16_t fake_lan9220_phy_reg(uint32_t reg);           /* PHY reg file peek */
uint32_t fake_lan9220_tx_cfg(void);                    /* TX_CFG readback (TX_ON/TXSAO) */

/* TX STATUS FIFO model (the un-drained-status stall under test):
 *   set_tx_status  — the completion status word stamped on each subsequently
 *                    transmitted frame (0 = clean; OR in SMSC911X_TX_STS_* error
 *                    bits to make the driver count a TX error);
 *   tx_status_used — TXSUSED: status words posted but not yet popped. The driver
 *                    must keep this drained (never pinned at the FIFO depth). */
void fake_lan9220_set_tx_status(uint32_t status);
int  fake_lan9220_tx_status_used(void);

/* TX capture: pop the oldest fully-framed transmitted frame. Returns its
 * length (truncated to cap), or -1 if none. */
int fake_lan9220_take_tx(void *buf, int cap);
int fake_lan9220_tx_count(void);

/* RX injection: queue one frame for smsc911x_rx_frame() to find. `error`
 * sets the status word's error-summary bit. */
int fake_lan9220_inject_rx(const void *frame, int len, int error);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_FAKE_LAN9220_H */
