/*
 * ahb_clcd.h -- the register map for the reference accelerator `ahb_clcd`, the
 * block that fills the nanosoc expansion socket (fpga/rp/nanosoc_exp/) and draws
 * on the on-board panel from the DUT side.
 *
 * This is the DUT-side counterpart of the shell's firmware/clcd/ driver: it runs
 * on nanosoc's Cortex-M0, not on the shell's MicroBlaze. The block it talks to is
 * `ahb_clcd.sv` (built by W2-D), an AHB-Lite slave at 0x6000_0000 whose registers
 * deliberately MIRROR the shell CLCD block so the two drivers look the same.
 *
 * Contract: fpga/rp/nanosoc_exp/README.md §5 (the address space) and §7 (this
 * register table). Do not open-code these addresses elsewhere.
 */
#ifndef AHB_CLCD_H
#define AHB_CLCD_H

#include <stdint.h>

/* The socket -- 256 MB at 0x6000_0000 (nanosoc's `exp_*` region). Your block
 * decodes only the low bits it needs; everything else in the region reads 0.
 * (README §5: NEVER respond above 0x6FFF_FFFF -- 0x7000_0000+ is the QSPI flash.) */
#define EXP_BASE            0x60000000u

/* Registers, mirroring the shell CLCD block (README §7). */
#define AHB_CLCD_CTRL       0x00u   /* RW  */
#define AHB_CLCD_CMD        0x04u   /* W   : [7:0] byte -> push {RS=0, byte} */
#define AHB_CLCD_DATA       0x08u   /* W   : [7:0] byte -> push {RS=1, byte} */
#define AHB_CLCD_STATUS     0x0Cu   /* RO  */
#define AHB_CLCD_TIMING     0x10u   /* RW  : hclk cycles */

/* CTRL bits. */
#define AHB_CLCD_CTRL_ENABLE      (1u << 0)
#define AHB_CLCD_CTRL_FIFO_RESET  (1u << 1)   /* self-clearing */
#define AHB_CLCD_CTRL_REQ         (1u << 2)   /* drives the tunnel's lcd_req */

/* STATUS bits. Writes into a FULL FIFO are DROPPED, never stalled (README §4) --
 * so the driver MUST poll fifo_full and never assume a write landed. */
#define AHB_CLCD_STATUS_FIFO_FULL   (1u << 0)
#define AHB_CLCD_STATUS_FIFO_EMPTY  (1u << 1)
#define AHB_CLCD_STATUS_BUSY        (1u << 2)
#define AHB_CLCD_STATUS_LEVEL_SHIFT 8
#define AHB_CLCD_STATUS_LEVEL_MASK  0x0000FF00u

/* TIMING fields, in hclk (DUT clock, 50 MHz shipped) cycles. The tunnel is a
 * clock-domain crossing with a stability filter, so the DUT must respect a floor:
 * every 8080 phase >= 8 hclk cycles (README §6 / dut-display-tunnel.md §5). The
 * reset default 8/8/8 satisfies it with margin -- do not go faster. */
#define AHB_CLCD_TIMING_WR_LO_SHIFT     0
#define AHB_CLCD_TIMING_WR_HI_SHIFT     8
#define AHB_CLCD_TIMING_CS_SETUP_SHIFT  16
#define AHB_CLCD_TIMING_DEFAULT \
    ((8u << AHB_CLCD_TIMING_WR_LO_SHIFT) | \
     (8u << AHB_CLCD_TIMING_WR_HI_SHIFT) | \
     (8u << AHB_CLCD_TIMING_CS_SETUP_SHIFT))

/* Panel geometry (Himax HX8347-D, QVGA landscape -- CLCD_PANEL_FACTS.md). */
#define AHB_CLCD_WIDTH  320u
#define AHB_CLCD_HEIGHT 240u

/* RGB565 helper: pack 8-bit r/g/b into the panel's 16-bit pixel. */
#define AHB_CLCD_RGB565(r, g, b) \
    ((uint16_t)((((r) & 0xF8u) << 8) | (((g) & 0xFCu) << 3) | ((b) >> 3)))

/* Run the reference demo forever: (re-)init the panel and repaint, on a loop.
 * Never returns. See ahb_clcd.c for WHY it re-inits every pass. */
void ahb_clcd_demo(void);

#endif /* AHB_CLCD_H */
