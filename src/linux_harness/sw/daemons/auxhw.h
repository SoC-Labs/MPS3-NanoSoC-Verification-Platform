/*
 * auxhw.h — hardware-backend seam for the aux-service daemons
 * (mps3-xvcd / mps3-swdd / mps3-uartbrd).
 *
 * Two backends behind one read32/write32 pair:
 *   - UIO  : mmap of the block's /dev/uioN (shell_linux.dts binds every
 *            soclabs CSR block via uio_pdrv_genirq + the load-bearing
 *            bootarg uio_pdrv_genirq.of_id=generic-uio). Board path.
 *   - MOCK : an in-process behavioural model of the block, for host/QEMU
 *            protocol-conformance testing (same role as firmware/test/'s
 *            MPS3_HAL_MOCK register mocks — DRIVER_MATRIX §2.9 test plan:
 *            "daemon protocol logic is host-testable against the existing
 *            MPS3_HAL_MOCK register mocks — port that harness").
 *
 * Register layout constants are ported VERBATIM from the frozen contract
 * header firmware/common/platform_regs.h (shell-regmap.md v0.5) — only the
 * blocks the aux daemons touch. If a base or bitfield changes, it changes
 * there first; keep these in lockstep.
 *
 * Mock behavioural notes (what the conformance tests rely on):
 *   DBGBR : write CTRL=GO completes the <=32-bit BSCAN shift instantly:
 *           TDO = TDI masked to LENGTH bits, GO self-clears. This pins the
 *           XAPP1251 access ORDER end-to-end (LENGTH/TMS/TDI staged before
 *           the kick, TDO read after GO clears) without real BSCAN.
 *   SWDBB : DRIVE is a plain write-through mirror; SAMPLE.swdio_i reads the
 *           driven SWDIO level while SWDIO_OE=1, else 1 (bus pull-up idle).
 *   CLKRST: RESET_CTRL is plain RW, reset value 0x7 (all released — the
 *           RTL convention 1=released).
 *   UARTBR: U0/U1 are byte LOOPBACKs (a host->DUT push appears on that
 *           stream's DUT->host pop) — the echo console the DRIVER_MATRIX
 *           §2.9 test plan asks for. Reads are destructive pops
 *           ({valid,data}, no pop when empty), exactly like the RTL.
 *           SWO_CFG enable-rise queues one "SWO-OK\n" banner into the SWO
 *           RX FIFO so the RX-only path is observable.
 */
#ifndef MPS3_AUXHW_H
#define MPS3_AUXHW_H

#include <stdint.h>
#include "uio.h"

/* ---- CLKRST (0x44A0_0000; UIO node "clkrst") --------------------------- */
#define CLKRST_RESET_CTRL   0x00u
#define CLKRST_RESET_CTRL_DUT_RESETN   (1u << 0)  /* 1 = released */
#define CLKRST_RESET_CTRL_RP_RESETN    (1u << 1)
#define CLKRST_RESET_CTRL_DBG_RESETN   (1u << 2)

/* ---- SWDBB (0x44A7_0000; UIO node "swd-bb") — LEGACY ---------------------
 * [DEV-10] The fabric carries jtag_bb at this page now, and shell_linux.dts
 * declares it as "jtag-bb". These defines exist ONLY for the parked
 * daemons/legacy/mps3_swdd.c and its host-mock tests. A JTAGBB block
 * (DRIVE {tck,tms,tdi} / SAMPLE {tdo}) is added when mps3-jtagd is written --
 * docs/planning/LINUX_FORK_JTAG_MIGRATION.md.
 */
#define SWDBB_DRIVE   0x00u
#define SWDBB_SAMPLE  0x04u  /* RO */
#define SWDBB_DRIVE_SWCLK      (1u << 0)
#define SWDBB_DRIVE_SWDIO_O    (1u << 1)
#define SWDBB_DRIVE_SWDIO_OE   (1u << 2)
#define SWDBB_SAMPLE_SWDIO_I   (1u << 0)  /* RO, 2-FF synced */

/* ---- DBGBR (0x44A8_0000; UIO node "dbgbr") — XAPP1251 layout ------------ */
#define DBGBR_LENGTH  0x00u  /* RW — bits to shift this chunk (1..32)  */
#define DBGBR_TMS     0x04u  /* RW — TMS vector chunk, LSB shifts first */
#define DBGBR_TDI     0x08u  /* RW — TDI vector chunk, LSB shifts first */
#define DBGBR_TDO     0x0Cu  /* RO — captured TDO chunk                 */
#define DBGBR_CTRL    0x10u  /* bit0 GO: write 1 to start, self-clears  */
#define DBGBR_CTRL_GO (1u << 0)

/* ---- UARTBR (0x44A9_0000; UIO node "uartbr") ---------------------------- */
#define UARTBR_U0_TXRX      0x00u
#define UARTBR_U1_TXRX      0x08u
#define UARTBR_SWO_RX       0x10u  /* RO */
#define UARTBR_FIFO_STATUS  0x14u  /* RO */
#define UARTBR_SWO_CFG      0x18u
#define UARTBR_DATA_MASK       0x000000FFu
#define UARTBR_VALID           (1u << 8)
#define UARTBR_FIFO_STATUS_U0_TX_FULL   (1u << 0)
#define UARTBR_FIFO_STATUS_U0_RX_EMPTY  (1u << 1)
#define UARTBR_FIFO_STATUS_U1_TX_FULL   (1u << 2)
#define UARTBR_FIFO_STATUS_U1_RX_EMPTY  (1u << 3)
#define UARTBR_FIFO_STATUS_SWO_RX_EMPTY (1u << 4)
#define UARTBR_SWO_CFG_DIVISOR_MASK  0x0000FFFFu
#define UARTBR_SWO_CFG_ENABLE        (1u << 16)

/* ---- backend ------------------------------------------------------------ */
typedef enum {
    AUXHW_BLK_DBGBR,
    AUXHW_BLK_SWDBB,
    AUXHW_BLK_CLKRST,
    AUXHW_BLK_UARTBR,
} auxhw_block_t;

#define AUXHW_FIFO_CAP 8192u

typedef struct {
    uint8_t  buf[AUXHW_FIFO_CAP];
    uint32_t head, count;
} auxhw_fifo_t;

typedef struct {
    int           mock;      /* 1 = behavioural model, 0 = UIO mmap */
    auxhw_block_t block;
    mps3_uio_t    uio;
    union {
        struct { uint32_t length, tms, tdi, tdo; } dbgbr;
        struct { uint32_t drive; } swdbb;
        struct { uint32_t reset_ctrl; } clkrst;
        struct {
            auxhw_fifo_t u0, u1, swo;
            uint32_t swo_cfg;
            int swo_banner_queued;
        } uartbr;
    } m;
} auxhw_t;

/* uio_name == NULL selects the mock backend. Returns 0, or -1 when the UIO
 * device is absent/unmappable — the caller must treat that as "block not
 * present" and refuse to serve (NEVER probe a page blind: under the MBV a
 * DECERR is a real S-mode access fault, DRIVER_MATRIX §2.9). */
int  auxhw_open(auxhw_t *hw, auxhw_block_t block, const char *uio_name);
void auxhw_close(auxhw_t *hw);

uint32_t auxhw_read32(auxhw_t *hw, uint32_t off);
void     auxhw_write32(auxhw_t *hw, uint32_t off, uint32_t v);

static inline void auxhw_set_bits32(auxhw_t *hw, uint32_t off, uint32_t mask)
{
    auxhw_write32(hw, off, auxhw_read32(hw, off) | mask);
}

static inline void auxhw_clr_bits32(auxhw_t *hw, uint32_t off, uint32_t mask)
{
    auxhw_write32(hw, off, auxhw_read32(hw, off) & ~mask);
}

/* ---- swap gate ------------------------------------------------------------
 * Cross-service gating seam (SERVICE_DISPOSITION §0 constraint 3): the swap
 * engine owner (mps3-ctrld's real FSM / the mps3_dfx ioctl path) creates
 * this file BEFORE decouple and removes it AFTER release+verify; each aux
 * daemon polls it and self-gates (XVC stalls shift:, SWD stops consuming
 * bytes, UARTBR stops touching the FIFOs but keeps the client).
 * File-existence was chosen over a socket/ipc so a crashed swap owner
 * fails OBVIOUS (stale gate = services visibly gated) rather than subtle. */
#define MPS3_SWAP_GATE_DEFAULT "/run/mps3/swap_gate"
int mps3_gate_active(const char *path);

#endif /* MPS3_AUXHW_H */
