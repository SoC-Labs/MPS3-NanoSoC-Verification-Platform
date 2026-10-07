/*
 * usd_regs.h -- register contract of the shell's `usd_spi` block (user microSD,
 * SPI mode), as the firmware driver (usd.c) and the host fake
 * (firmware/test/fake_usd.c) see it.
 *
 * SOURCE: docs/planning/HANDOVER_USD_OVERLAY_STORE.md §4.1, plus the D13-L1/L2
 * lane contract of 2026-09-23 (which adds STATUS.OVR, STATUS.ABORT and the
 * "a DATA write while BUSY is ignored" rule). The RTL is lane L1's
 * fpga/shell/ip/usd_spi/usd_spi.sv.
 *
 * ======================= TO BE REPLACED AT INTEGRATION =======================
 * The BASE and the five OFFSETS below are PROVISIONAL. At Wave 2 the block
 * lands in shell_bd.tcl at 0x44A4_0000 (reusing the OVLSTORE page) and
 * tools/gen_regmap.py generates them into firmware/common/platform_regs.h,
 * from the RTL's `localparam ... IDX_<NAME>` decode. The names used here are
 * the ones the generator will emit for a block called USD with RTL registers
 * ID/CTRL/CLKDIV/DATA/STATUS: MPS3_USD_BASE, USD_ID, USD_CTRL, USD_CLKDIV,
 * USD_DATA, USD_STATUS. Each fallback is #ifndef-guarded, so once the
 * generated names exist they win and the fallbacks go dead. Then DELETE the
 * fallback block below (one hand-written spelling of a generated truth is the
 * duplication the generator exists to remove -- see touch.h's note on the same
 * fold). If the generator picks different names, rename the uses in usd.c and
 * fake_usd.c; do not keep both spellings.
 *
 * The BIT FIELDS are NOT generated (platform_regs.h's rule: fields live
 * outside the fences, beside the prose that explains them), so they stay here
 * permanently.
 */
#ifndef MPS3_USD_REGS_H
#define MPS3_USD_REGS_H

#include "../common/platform_regs.h"

/* ---- PROVISIONAL: replaced by generated names at integration ------------- */
#ifndef MPS3_USD_BASE
#define MPS3_USD_BASE   0x44A40000u   /* 64K; the OVLSTORE page, axi_quad_spi_0 today */
#endif
#ifndef USD_ID
#define USD_ID          0x00u         /* (ro) constant USD_ID_VALUE */
#endif
#ifndef USD_CTRL
#define USD_CTRL        0x04u
#endif
#ifndef USD_CLKDIV
#define USD_CLKDIV      0x08u
#endif
#ifndef USD_DATA
#define USD_DATA        0x0Cu
#endif
#ifndef USD_STATUS
#define USD_STATUS      0x10u
#endif
/* ---- end PROVISIONAL ------------------------------------------------------ */

/* ID: "USD1". The driver reads it once at usd_init(); anything else means the
 * block is not in this fabric (e.g. an older shell that still has the pin-less
 * axi_quad_spi_0 on this page) and the driver then never writes the page. */
#define USD_ID_VALUE        0x55534431u

/* CTRL @ 0x04, reset 0. */
#define USD_CTRL_EN         (1u << 0)  /* enable pad drivers. The HARDWARE drives the
                                        * pads only when EN && (CD_PRESENT || CD_IGNORE) */
#define USD_CTRL_CS         (1u << 1)  /* 1 = assert chip select (USD_DAT[3] driven low) */
#define USD_CTRL_WIDE       (1u << 2)  /* 0 = 8-bit shift, 1 = 32-bit shift */
#define USD_CTRL_CD_POL     (1u << 3)  /* 0 = USD_NCD low means present (assumed default) */
#define USD_CTRL_CD_IGNORE  (1u << 4)  /* 1 = treat the slot as always present */
/* The two card-detect configuration bits. The driver PRESERVES them on every
 * CTRL write (read-modify-write), so board step B0 can set them over JTAG and
 * the driver will not undo it. */
#define USD_CTRL_CD_MASK    (USD_CTRL_CD_POL | USD_CTRL_CD_IGNORE)

/* CLKDIV @ 0x08, [15:0]. SCK = aclk / (2 * (DIV + 1)); aclk = 100 MHz. */
#define USD_CLKDIV_MASK     0x0000FFFFu
#define USD_CLKDIV_400K     124u       /* 400 kHz: reset value, and SD identification mode */
#define USD_CLKDIV_12M5     3u         /* 12.5 MHz: data transfer after init */
#define USD_CLKDIV_25M      1u         /* 25 MHz: board step B2 only, not used by default */

/* DATA @ 0x0C. Write = start an SPI mode-0, MSB-first shift: [7:0] when
 * CTRL.WIDE=0, [31:0] when WIDE=1 (bit 31 leaves first; the first byte
 * received lands in [31:24]). Read = the last received word, 8-bit results
 * zero-extended. A write while BUSY is IGNORED and sets STATUS.OVR. */

/* STATUS @ 0x10. */
#define USD_STATUS_BUSY       (1u << 0)  /* a shift is in progress */
#define USD_STATUS_CD_PRESENT (1u << 1)  /* debounced (>= 10 ms), polarity applied */
#define USD_STATUS_CD_RAW     (1u << 2)  /* synchronised pin level (for board step B0) */
#define USD_STATUS_CD_CHANGED (1u << 3)  /* sticky, W1C: CD_PRESENT changed */
#define USD_STATUS_OVR        (1u << 4)  /* sticky, W1C: DATA written while BUSY */
#define USD_STATUS_ABORT      (1u << 5)  /* sticky, W1C: pads were disabled mid-transfer */
#define USD_STATUS_W1C_MASK   (USD_STATUS_CD_CHANGED | USD_STATUS_OVR | USD_STATUS_ABORT)

#endif /* MPS3_USD_REGS_H */
