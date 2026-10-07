/*
 * mps3_icap_regs.h — the three register blocks the DFX swap path touches.
 *
 * PROVENANCE (do not re-derive — every constant below is silicon-proven or
 * frozen by contract):
 *   - firmware/common/platform_regs.h in the main repo (read-only) is the
 *     shared contract header; this file is a subset port of its HWICAP /
 *     DFXCTL / CLKRST blocks with the provenance comments kept.
 *   - CR bit0 = WRITE, bit1 = READ (xhwicap_l.h XHI_CR_WRITE_MASK=0x1). These
 *     were once SWAPPED in the firmware and the swap stalled with RM_ID stuck
 *     at greybox — HW-confirmed on the KU115 shell. Never "fix" this.
 *   - The Linux-baseline BD builds axi_hwicap with C_MODE {0} = FIFO mode,
 *     C_WRITE_FIFO_DEPTH 1024 (shell_linux_bd.tcl / ADDRESS_MAP.md), while
 *     the on-silicon bare-metal shell is LITE mode (C_MODE {1}). Both write
 *     protocols are implemented in the engine; FIFO is the default here.
 *   - Byte order: the DFX .bin stores config words BIG-endian (sync word is
 *     file bytes AA 99 55 66 in order); rv32 is little-endian, so a native
 *     memcpy byte-swaps the word and ICAP never sees 0xAA995566. The ONE
 *     packing primitive is mps3_hwicap_pack_word() below — every writer must
 *     go through it (R3 unification, firmware lesson).
 */
#ifndef MPS3_ICAP_REGS_H
#define MPS3_ICAP_REGS_H

#include "mps3_compat.h"

/* Block bases (shell-regmap frozen; the Linux BD keeps the same map —
 * ADDRESS_MAP.md). The engine never uses absolute addresses — it indexes
 * blocks through the ops vtable — but the kernel driver ioremaps these. */
#define MPS3_CLKRST_PHYS   0x44A00000u
#define MPS3_DFXCTL_PHYS   0x44A10000u
#define MPS3_HWICAP_PHYS   0x44A20000u
#define MPS3_BLOCK_SPAN    0x10000u

/* Block indices for the ops vtable. */
enum mps3_blk {
	MPS3_BLK_HWICAP = 0,
	MPS3_BLK_DFXCTL = 1,
	MPS3_BLK_CLKRST = 2,
	MPS3_BLK_COUNT
};

/* ---- HWICAP (PG134 / xhwicap_l.h layout) ------------------------------- */
#define HWICAP_GIER   0x1Cu
#define HWICAP_ISR    0x20u
#define HWICAP_IER    0x28u
#define HWICAP_WF     0x100u  /* Write FIFO (config words, ICAP order)   */
#define HWICAP_RF     0x104u  /* Read FIFO                               */
#define HWICAP_SZ     0x108u  /* transfer size, words                    */
#define HWICAP_CR     0x10Cu  /* control                                 */
#define HWICAP_SR     0x110u  /* status                                  */
#define HWICAP_WFV    0x114u  /* write FIFO vacancy, words               */
#define HWICAP_RFO    0x118u  /* read FIFO occupancy, words              */

#define HWICAP_CR_WRITE   (1u << 0)  /* bit0=WRITE — silicon-found, see header */
#define HWICAP_CR_READ    (1u << 1)
#define HWICAP_SR_DONE    (1u << 0)
#define HWICAP_SR_EOS     (1u << 2)

/* ---- DFXCTL (soclabs dfx_ctl, shell-regmap v0.1, I8 resolved) ----------- */
#define DFXCTL_DECOUPLE     0x00u
#define DFXCTL_SHUTDOWN     0x04u
#define DFXCTL_STATUS       0x08u
#define DFXCTL_RM_ID        0x10u  /* RO — RP rm_id partition pin readback  */
#define DFXCTL_RM_STATUS    0x14u  /* RO                                     */

#define DFXCTL_DECOUPLE_EN        (1u << 0)
#define DFXCTL_SHUTDOWN_AXI       (1u << 0)
#define DFXCTL_STATUS_DECOUPLED   (1u << 0)
#define DFXCTL_STATUS_RP_IN_RESET (1u << 1)

#define DFXCTL_RM_STATUS_RM_ID_VALID (1u << 0)
#define DFXCTL_RM_STATUS_DUT_LOCKUP  (1u << 1)

/* ---- CLKRST (soclabs dut_clkrst; 1 = released) -------------------------- */
#define CLKRST_RESET_CTRL   0x00u
#define CLKRST_STATUS       0x0Cu

#define CLKRST_RESET_CTRL_DUT_RESETN   (1u << 0)
#define CLKRST_RESET_CTRL_RP_RESETN    (1u << 1)
#define CLKRST_RESET_CTRL_DBG_RESETN   (1u << 2)

/* THE one shared HWICAP word-packing primitive. First file byte -> MSB
 * (I18); explicit shifts, NEVER a native memcpy on a little-endian CPU. */
static inline u32 mps3_hwicap_pack_word(const u8 *p)
{
	return ((u32)p[0] << 24) | ((u32)p[1] << 16) |
	       ((u32)p[2] << 8) | (u32)p[3];
}

/* Bitstream landmarks (Xilinx UG570/UG470 config packets) — used by the
 * mock to prove the stream reached ICAP in the right byte order. */
#define MPS3_ICAP_SYNC_WORD    0xAA995566u
#define MPS3_ICAP_DESYNC_CMD   0x0000000Du

#endif /* MPS3_ICAP_REGS_H */
