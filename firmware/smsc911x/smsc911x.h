/*
 * smsc911x.h — bare-metal LAN9220 (SMSC/Microchip smsc911x family) driver
 * for the MPS3 shell's management Ethernet port.
 *
 * LICENSE PROVENANCE: the register sequences (init/reset ordering, MAC-CSR
 * and MII indirection, TX command word framing, RX status/data FIFO
 * draining) are PORTED FROM Zephyr's drivers/ethernet/eth_smsc911x.c
 * (Apache-2.0) per firmware/smsc911x/README.md's §12 licensing rationale —
 * re-expressed against this repo's HAL (common/platform_regs.h accessors),
 * no Zephyr code copied verbatim, no Zephyr device-model/net_pkt/IRQ
 * dependency retained. Record in the A6 licensing audit: Apache-2.0
 * derivative notice applies to smsc911x.{h,c}.
 *
 * Seams (deliberate, per the porting notes):
 *   - REGISTER BASE: the LAN9220 sits behind the static-memory bus (AXI
 *     EMC), NOT an AXI4-Lite shell block — its base is an A1 block-design
 *     detail surfaced via xparameters.h, so it is a runtime init argument
 *     here rather than a platform_regs.h constant.
 *   - PBUF GLUE: this driver moves flat byte buffers only. The lwIP
 *     ethernetif wrapper (pbuf chain <-> these tx/rx calls, netif->
 *     linkoutput, xemacif_input shape) is the same one-thin-file TODO as
 *     the net_if.h lwIP backend — not written this pass.
 *   - POLL MODEL: no interrupts; the main loop polls rx (README point 5).
 */
#ifndef MPS3_SMSC911X_H
#define MPS3_SMSC911X_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- register map (LAN9220 datasheet ch. 5; byte offsets from base) ------ */
#define SMSC911X_RX_DATA_FIFO   0x00u
#define SMSC911X_TX_DATA_FIFO   0x20u
#define SMSC911X_RX_STATUS_FIFO 0x40u
#define SMSC911X_RX_STATUS_PEEK 0x44u
#define SMSC911X_TX_STATUS_FIFO 0x48u
#define SMSC911X_TX_STATUS_PEEK 0x4Cu
#define SMSC911X_ID_REV         0x50u
#define SMSC911X_IRQ_CFG        0x54u
#define SMSC911X_INT_STS        0x58u
#define SMSC911X_INT_EN         0x5Cu
#define SMSC911X_BYTE_TEST      0x64u
#define SMSC911X_FIFO_INT       0x68u
#define SMSC911X_RX_CFG         0x6Cu
#define SMSC911X_TX_CFG         0x70u
#define SMSC911X_HW_CFG         0x74u
#define SMSC911X_RX_DP_CTRL     0x78u
#define SMSC911X_RX_FIFO_INF    0x7Cu
#define SMSC911X_TX_FIFO_INF    0x80u
#define SMSC911X_PMT_CTRL       0x84u
#define SMSC911X_GPIO_CFG       0x88u
#define SMSC911X_GPT_CFG        0x8Cu
#define SMSC911X_GPT_CNT        0x90u
#define SMSC911X_WORD_SWAP      0x98u
#define SMSC911X_FREE_RUN       0x9Cu
#define SMSC911X_RX_DROP        0xA0u
#define SMSC911X_MAC_CSR_CMD    0xA4u
#define SMSC911X_MAC_CSR_DATA   0xA8u
#define SMSC911X_AFC_CFG        0xACu
#define SMSC911X_E2P_CMD        0xB0u
#define SMSC911X_E2P_DATA       0xB4u

#define SMSC911X_BYTE_TEST_PATTERN 0x87654321u
#define SMSC911X_ID_LAN9220        0x9220u  /* ID_REV[31:16] */

#define SMSC911X_HW_CFG_SRST       (1u << 0)
#define SMSC911X_HW_CFG_MBO        (1u << 20) /* must-be-one */
#define SMSC911X_HW_CFG_TX_FIF_SZ(k) ((uint32_t)(k) << 16) /* KB */

#define SMSC911X_PMT_CTRL_READY    (1u << 0)
#define SMSC911X_E2P_CMD_BUSY      (1u << 31)
#define SMSC911X_TX_CFG_STOP_TX    (1u << 0)
#define SMSC911X_TX_CFG_TX_ON      (1u << 1)
#define SMSC911X_TX_CFG_TXSAO      (1u << 2)  /* TX Status Allow Overrun: on a FULL
                                               * TX status FIFO discard the oldest
                                               * status word instead of HALTING the
                                               * transmitter — belt to the always-
                                               * drain in smsc911x_tx_frame(). */
#define SMSC911X_RX_DP_CTRL_FFWD   (1u << 31) /* RX data fast-forward (discard) */

#define SMSC911X_MAC_CSR_CMD_BUSY  (1u << 31)
#define SMSC911X_MAC_CSR_CMD_READ  (1u << 30)

/* MAC CSR indices (indirect via MAC_CSR_CMD/DATA) */
#define SMSC911X_MAC_CR    1u
#define SMSC911X_MAC_ADDRH 2u
#define SMSC911X_MAC_ADDRL 3u
#define SMSC911X_MAC_HASHH 4u
#define SMSC911X_MAC_HASHL 5u
#define SMSC911X_MAC_MII_ACC  6u
#define SMSC911X_MAC_MII_DATA 7u
#define SMSC911X_MAC_FLOW  8u

#define SMSC911X_MAC_CR_RXEN  (1u << 2)
#define SMSC911X_MAC_CR_TXEN  (1u << 3)
#define SMSC911X_MAC_CR_PRMS  (1u << 18) /* promiscuous — §8.3: two MACs share the port */

#define SMSC911X_MII_ACC_BUSY  (1u << 0)
#define SMSC911X_MII_ACC_WRITE (1u << 1)

/* Internal PHY (address 1 on the internal MII) */
#define SMSC911X_PHY_ADDR      1u
#define SMSC911X_PHY_BMCR      0u
#define SMSC911X_PHY_BMSR      1u
#define SMSC911X_PHY_BMCR_RESET   (1u << 15)
#define SMSC911X_PHY_BMCR_ANEN    (1u << 12)
#define SMSC911X_PHY_BMCR_ANRST   (1u << 9)
#define SMSC911X_PHY_BMSR_LINK_UP (1u << 2)

/* TX command words (datasheet "TX command format") */
#define SMSC911X_TX_CMDA_FIRST_SEG (1u << 13)
#define SMSC911X_TX_CMDA_LAST_SEG  (1u << 12)
#define SMSC911X_TX_CMDA_BUFSZ_MASK 0x000007FFu
#define SMSC911X_TX_CMDB_LEN_MASK   0x000007FFu

/* TX_FIFO_INF (0x80): TX DATA FIFO free space (bytes) in [15:0] (TDFREE) and TX
 * STATUS FIFO used count in [23:16] (TXSUSED). The MAC pushes ONE status DWORD
 * into the TX STATUS FIFO for every frame it transmits. With TXSAO clear, a FULL
 * status FIFO makes the MAC stop STARTING new frames (it has nowhere to post
 * their completion) — the TX DATA FIFO then backs up until TDFREE < a frame and
 * TX wedges. This is the sustained-TX stall the HW showed (RX + ICAP busy, TX
 * egress only after a TCP RTO). The cure is to POP the status FIFO — TXSUSED
 * entries via TX_STATUS_FIFO — every transmit and every poll so it never fills. */
#define SMSC911X_TX_FIFO_INF_TDFREE_MASK   0x0000FFFFu
#define SMSC911X_TX_FIFO_INF_TXSUSED_MASK  0x00FF0000u
#define SMSC911X_TX_FIFO_INF_TXSUSED_SHIFT 16

/* TX status word (popped from TX_STATUS_FIFO, 0x48): [31:16] echoes the packet
 * tag; the low bits are the per-frame transmit-error flags (LAN9220 datasheet
 * "TX Status Format"). Any set = a frame the MAC could not cleanly put on the
 * wire (counted into tx_errors; TCP retransmits the lost data). */
/* TX completion status word: [31:16] packet tag, [15] ERROR STATUS summary,
 * [11:8] + [2] error causes, [7:3] collision count, [0] deferred.
 *
 * ES (bit 15) is the AUTHORITATIVE "this frame failed" flag; the cause bits are
 * only meaningful when it is set. Gating on the causes alone mis-counts good
 * frames (HW: 822 of 825 healthy frames flagged, while TX was demonstrably
 * perfect -- 1.31 MB at ~570 KB/s, zero drops). The Linux smsc911x driver tests
 * ES first for the same reason. */
#define SMSC911X_TX_STS_ES           (1u << 15) /* error status (summary)       */
#define SMSC911X_TX_STS_LOST_CARR    (1u << 11) /* carrier lost mid-frame       */
#define SMSC911X_TX_STS_NO_CARR      (1u << 10) /* no carrier                   */
#define SMSC911X_TX_STS_LATE_COLL    (1u << 9)  /* late collision               */
#define SMSC911X_TX_STS_EXCESS_COLL  (1u << 8)  /* excessive collisions -> drop */
#define SMSC911X_TX_STS_EXCESS_DEFER (1u << 2)  /* excessive deferral           */
/* Cause bits, valid only when ES is set. */
#define SMSC911X_TX_STS_CAUSE_MASK \
    (SMSC911X_TX_STS_LOST_CARR | SMSC911X_TX_STS_NO_CARR | \
     SMSC911X_TX_STS_LATE_COLL | SMSC911X_TX_STS_EXCESS_COLL | \
     SMSC911X_TX_STS_EXCESS_DEFER)

/* Carrier-sense causes. MEASURED ON THIS BOARD: every healthy frame completes with
 * status 0x00008400 == ES | NO_CARR (read from the JTAG diag mailbox's raw
 * tx_last_status). The MAC's carrier-sense input is not meaningfully driven here,
 * and on a FULL-DUPLEX link carrier sense is irrelevant -- the frame transmits
 * perfectly (1.31 MB at ~570 KB/s, zero drops). Counting these as errors made
 * tx_errors read 822/825 on a flawless link. Excluded from the error count. */
#define SMSC911X_TX_STS_CARRIER_MASK \
    (SMSC911X_TX_STS_LOST_CARR | SMSC911X_TX_STS_NO_CARR)

/* A REAL transmit failure: ES set AND a non-carrier cause (collision/deferral).
 * In full duplex these should never occur, so tx_errors should read 0. */
#define SMSC911X_TX_STS_REAL_ERR_MASK \
    (SMSC911X_TX_STS_LATE_COLL | SMSC911X_TX_STS_EXCESS_COLL | \
     SMSC911X_TX_STS_EXCESS_DEFER)

/* RX status word */
#define SMSC911X_RX_STS_LEN(sts)   (((sts) >> 16) & 0x3FFFu)
#define SMSC911X_RX_STS_ERROR      (1u << 15) /* error summary */

/* INT_STS (0x58) RX-side error/overrun status — write-1-to-clear. Even in the
 * poll model (INT_EN = 0, so no ETH_INT line is raised) these still LATCH when
 * the RX FIFO overruns because the single-threaded main loop could not drain RX
 * often enough during a busy period (e.g. a long HWICAP streaming burst). If
 * they are never cleared the RX path can wedge and deliver no further frames; the
 * driver detects + acks them (smsc911x_rx_recover) so a transient overrun
 * self-heals and the lost bytes are simply retransmitted by TCP. Bit positions
 * per the LAN9220 datasheet (ch.5 INT_STS) / Linux smsc911x.h. */
#define SMSC911X_INT_STS_RXDF_INT  (1u << 22) /* RX dropped frame (FIFO overrun) */
#define SMSC911X_INT_STS_RWT       (1u << 15) /* RX watchdog timeout             */
#define SMSC911X_INT_STS_RXE       (1u << 14) /* RX error                        */
#define SMSC911X_INT_STS_RSFF      (1u << 4)  /* RX status FIFO full             */
#define SMSC911X_INT_STS_RX_ERR_MASK \
    (SMSC911X_INT_STS_RXDF_INT | SMSC911X_INT_STS_RWT | \
     SMSC911X_INT_STS_RXE | SMSC911X_INT_STS_RSFF)

/* RX_CFG (0x6C): RX_DUMP flushes the whole RX DATA FIFO (bit self-clears when
 * the dump completes) — the recovery sledgehammer that resyncs the data/status
 * FIFOs after a hard RX error left them out of step. */
#define SMSC911X_RX_CFG_RX_DUMP    (1u << 15)

/* Frame bounds */
#define SMSC911X_MAX_FRAME 1522u

/* ---- THE BOUNDED WAITS, IN MICROSECONDS -------------------------------------
 * Every busy/ready wait in smsc911x.c used to spin SMSC911X_POLL_BOUND (100000)
 * ITERATIONS. An iteration count is not a duration: the same constant is worth a
 * different number of microseconds on every clock, every -O level and every edit
 * to the loop body, so nothing in this tree could say what any of these waits was
 * worth -- see common/service.h, "THE BOUNDS WERE ITERATION COUNTS". These are
 * microseconds off the free-running AXI timer, so they mean the same thing in
 * every build and firmware/test/test_smsc911x_spin.c can assert them.
 *
 * They are declared HERE, not in the .c, for the same reason
 * MPS3_TX_SPACE_TIMEOUT_US is: the host test asserts the same symbol the
 * firmware spins on, so the two cannot drift.
 *
 * WHERE EACH NUMBER COMES FROM. All four are UPPER BOUNDS derived from a stated
 * fact and then given headroom -- none is a silicon measurement, and the tree
 * should not pretend otherwise:
 *
 *   CSR     the MAC-CSR / E2P indirect handshake (MAC_CSR_CMD.BUSY,
 *           E2P_CMD.BUSY). An on-chip register handshake between the host
 *           interface and the MAC's own clock domain: microseconds at worst.
 *           1 ms is ~3 orders above that and still 5x below the smallest
 *           superloop budget that can contain one (tx_drain, 5 ms --
 *           firmware/platform/src/main.c).
 *   MII     one IEEE 802.3 clause-22 MDIO frame is 64 bit times (32 preamble +
 *           32 frame); at the 2.5 MHz MDC ceiling that is 25.6 us. 1 ms is ~39x.
 *   RESET   HW_CFG.SRST self-clear. The driver's own long-standing note put this
 *           at ~10 us on silicon; 1 ms is 100x.
 *   RX_DUMP RX_CFG.RX_DUMP self-clear -- an internal FIFO pointer reset, same
 *           class as RESET. 1 ms, and it sits inside net_rx's 20 ms budget
 *           alongside the 5 ms lan_linkoutput TX-space wait, so a dead RX_DUMP
 *           bit costs ONE superloop pass rather than the shell.
 *
 * The two INIT-ONLY waits are deliberately far larger, because they are not in
 * the superloop at all -- smsc911x_init() runs once, before the service table
 * exists, so their cost is boot latency and never a missed service:
 *   BOOT    PMT_CTRL.READY + the EEPROM auto-load (E2P_CMD.BUSY) after reset.
 *           This is the one step whose duration is set by an external serial
 *           part rather than by on-chip logic, and no number for it is recorded
 *           anywhere in this tree. 100 ms is an unashamed upper bound, chosen so
 *           that a board that never finishes the auto-load still reaches the
 *           FATAL branch in main() in a tenth of a second instead of hanging.
 *   PHY     BMCR.RESET self-clear. IEEE 802.3 clause 22 (22.2.4.1.1) REQUIRES a
 *           PHY to complete reset within 0.5 s of bit 0.15 being set, so 500 ms
 *           is the standard's own ceiling -- not a guess, and not to be
 *           tightened without a measurement.
 * -------------------------------------------------------------------------- */
#define SMSC911X_CSR_TIMEOUT_US        1000u
#define SMSC911X_MII_TIMEOUT_US        1000u
#define SMSC911X_RESET_TIMEOUT_US      1000u
#define SMSC911X_RX_DUMP_TIMEOUT_US    1000u
#define SMSC911X_BOOT_TIMEOUT_US     100000u
#define SMSC911X_PHY_RESET_TIMEOUT_US 500000u

/* ---- THE TWO DRAINS ARE NOT WAITS -------------------------------------------
 * smsc911x_tx_status_drain() and the orphaned-RX-status sweep inside
 * smsc911x_rx_recover() also spun SMSC911X_POLL_BOUND, and converting them to
 * mps3_spin_until() would have been the WRONG mechanical substitution. A wait
 * has a condition that becomes true and a duration you are willing to lose to
 * it; a drain has neither. It pops items until the hardware says there are no
 * more, its cost is one bus access per item, and the quantity the hardware
 * bounds is a COUNT, not a time.
 *
 * So they keep a count bound -- but the count is now derived from the register
 * field that produces it instead of being 100000. TXSUSED is TX_FIFO_INF[23:16]
 * and RXSUSED is RX_FIFO_INF[23:16]: both are 8 bits, so at most 255 status
 * words can be posted and unread, and each pop decrements the count. 256 is
 * therefore one more than the deepest legitimate drain.
 *
 * WHAT THE OLD BOUND ACTUALLY COST. TXSAO is set (TX_CFG), so the MAC keeps
 * transmitting and keeps posting completions while the drain runs: under
 * sustained TX the count can be REPLENISHED as fast as it is popped, and the
 * old bound let that livelock spin 100,000 times inside one superloop pass.
 * With 256 the drain always returns, the superloop's tx_drain service comes
 * straight back round, and `drained` reports what was taken.
 * -------------------------------------------------------------------------- */
#define SMSC911X_TX_STATUS_DRAIN_MAX 256u
#define SMSC911X_RX_STATUS_DRAIN_MAX 256u

/* ---- driver API ------------------------------------------------------------ */

/* Full bring-up: byte-order sanity (BYTE_TEST), chip-ID check, soft reset,
 * power-management ready, EEPROM-load wait, FIFO sizing, PHY reset +
 * autoneg restart, MAC address programming, PROMISCUOUS + TX/RX enable,
 * interrupts masked (poll model). Returns 0, or a negative
 * SMSC911X_ERR_* on any step failing its bounded poll/check. */
int smsc911x_init(uintptr_t base, const uint8_t mac[6]);

enum {
    SMSC911X_ERR_BYTE_TEST = -1, /* bus wiring/endianness wrong */
    SMSC911X_ERR_ID        = -2, /* not a LAN9220 */
    SMSC911X_ERR_TIMEOUT   = -3, /* a bounded poll (reset/ready/busy) expired */
    SMSC911X_ERR_TX_SPACE  = -4, /* TX data FIFO can't hold the frame right now */
    SMSC911X_ERR_TOO_BIG   = -5, /* frame exceeds SMSC911X_MAX_FRAME (or rx cap) */
    SMSC911X_ERR_RX_ERROR  = -6, /* RX status error summary: frame consumed + dropped */
};

/* Transmit one whole frame (single segment, FCS appended by the MAC).
 * Reaps completed TX status words (smsc911x_tx_status_drain) BEFORE the room
 * check so the status FIFO can never fill and stall the MAC, then checks TDFREE
 * and — only if the whole frame + its two command words fit — writes it.
 * Non-blocking: returns SMSC911X_ERR_TX_SPACE if the TX DATA FIFO lacks room now
 * (nothing written — caller retries a later poll), SMSC911X_ERR_TOO_BIG on an
 * oversize frame, else 0. */
int smsc911x_tx_frame(const void *frame, uint32_t len);

/* Reap completed TX status words: pop every entry the MAC has posted into the TX
 * STATUS FIFO (TX_FIFO_INF.TXSUSED count) so it can never fill and back-pressure
 * / halt the transmitter — the fix for the sustained-TX stall. Called at the
 * head of every smsc911x_tx_frame() AND once per superloop poll (a completion
 * status posts slightly after its frame's tx_frame() returned, so the poll-time
 * sweep reaps the last frame's status too). Any error-flagged word is counted
 * into the TX diagnostics' `errors`. Cheap when idle (one TX_FIFO_INF read).
 * Returns the number of status words drained. */
int smsc911x_tx_status_drain(void);

/* Read the free-running TX diagnostics (any out-pointer may be NULL):
 *   frames_sent    — frames accepted into the TX DATA FIFO by smsc911x_tx_frame;
 *   status_drained — TX status words reaped from the TX STATUS FIFO;
 *   errors         — of those, words with a transmit-error bit set.
 * Surfaced at the JTAG diag mailbox so a HW run SHOWS the status FIFO draining
 * (status_drained tracking frames_sent, TXSUSED never pinned) — the proof the
 * TX stall is cured. Never reset after init (monotonic). */
uint32_t smsc911x_tx_last_status(void);
void smsc911x_get_tx_diag(uint32_t *frames_sent, uint32_t *status_drained,
                          uint32_t *errors);

/* Receive one whole pending frame into buf. Returns its length in bytes
 * (as reported by the RX status word, FCS included), 0 if none pending,
 * SMSC911X_ERR_TOO_BIG if it exceeds cap (frame is consumed + dropped —
 * fail closed, never a truncated frame handed upward), or < 0 on an
 * error-summary frame (also consumed + dropped). Calls smsc911x_rx_recover()
 * internally at entry so a latched RX-FIFO overrun is cleared before the read. */
int smsc911x_rx_frame(void *buf, uint32_t cap);

/* Detect + recover a latched RX-FIFO overrun/error (INT_STS RX bits). A busy
 * period where the poll-model main loop can't drain RX fast enough overruns the
 * LAN9220 RX FIFO; if the latched error is never cleared, RX wedges and no
 * further frames are delivered. This acks the sticky status (write-1-clear) +
 * drains the dropped-frame counter, and on a hard RX error/watchdog also flushes
 * the RX data FIFO (RX_DUMP) and drains stale status words so both FIFOs return
 * to an aligned, empty state — leaving MAC RX enabled throughout. Cheap when
 * idle (one INT_STS read). smsc911x_rx_frame() calls it every poll; it is
 * exposed for a caller that wants to force a sweep / count events. Returns 1 if
 * an overrun was detected and recovered, 0 if there was nothing to do. */
int smsc911x_rx_recover(void);

/* Read the free-running RX-recovery diagnostics (any out-pointer may be NULL):
 *   recover_events — smsc911x_rx_recover() firings (a latched overrun found);
 *   recover_dumps  — of those, how many took the hard RXE|RWT RX-FIFO-dump path;
 *   drop_frames    — running total of frames the MAC dropped to overrun (summed
 *                    RX_DROP). These surface on the control channel (diag verb)
 *                    and at the JTAG-readable diag mailbox so a HW run SHOWS
 *                    whether recovery is firing at a stall point. */
void smsc911x_get_diag(uint32_t *recover_events, uint32_t *recover_dumps,
                       uint32_t *drop_frames);

/* PHY management (internal PHY via the MAC's MII indirection). */
int  smsc911x_mii_read(uint32_t phy_reg, uint16_t *val_out);
int  smsc911x_mii_write(uint32_t phy_reg, uint16_t val);
int  smsc911x_link_up(void); /* 1 = BMSR reports link, 0 = down, <0 = MII error */

/* Read back ID_REV (diagnostics). */
uint32_t smsc911x_id_rev(void);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_SMSC911X_H */
