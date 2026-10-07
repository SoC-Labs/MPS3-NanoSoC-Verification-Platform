/*
 * hx8347_init.c -- HX8347-D power-on / init / GRAM-window / pixel-format table
 *                  for the MPS3 QVGA CLCD (module MCBQVGA-TS, controller
 *                  Himax HX8347-D), streamed byte-by-byte through the AXI4-Lite
 *                  8080 master in fpga/shell/ip/clcd/clcd.sv.
 *
 * ============================ PROVENANCE (outcome A) =========================
 * Every register index and datum below is TRANSCRIBED from a cited, permissively
 * licensed open-source HX8347-D driver -- NOT written from memory. Full record,
 * with URLs, commit SHAs, licences, and datasheet page cites, is in
 * firmware/clcd/PANEL_PROVENANCE.md. Summary:
 *
 *   PRIMARY  (values + register map):
 *     STMicroelectronics/stm32-hx8347d, branch main, commit 9758706
 *       - hx8347d.c  : HX8347D_Init() / SetOrientation() / SetCursor() -- the
 *                      ordered register writes and the 10/100/100 ms delays.
 *       - hx8347d_reg.h : the register-symbol -> address map (0x00..0xED).
 *       - hx8347d.h  : HX8347D_FORMAT_RBG565 = 0x05 (16bpp); LANDSCAPE = 0x02.
 *     Licence: BSD-3-Clause (LICENSE.md, "Copyright 2018 STMicroelectronics").
 *              Redistribution of these values is permitted; attribution retained
 *              here and in PANEL_PROVENANCE.md. See that file for the notice.
 *
 *   CORROBORATION (independent, second source):
 *     nopnop2002/esp-idf-parallel-tft, commit 1dee984,
 *       components/tft_library/hx8347.c -- MIT licence.
 *       Its gamma blocks (0x40.., 0x50..) are BYTE-IDENTICAL to ST's; it also
 *       uses 0x17=0x05 (65k), 0x28=0x38->0x3C display-on, window regs 0x02-0x09,
 *       and GRAM-write 0x22 -- see PANEL_PROVENANCE.md for the diff.
 *
 *   DATASHEET (semantics + timing, Q2/Q3/Q6/Q7):
 *     Himax "HX8347-D(T)" Data Sheet, DOC No. HX8347-D(T)-DS, Version 02,
 *     March 2009. 5.1.2 (8-bit-bus/16-bit-data mapping, Fig 5.22 -> MSByte
 *     first), 8.16 COLMOD (17h), 11.5.4 reset timing.
 *
 *   NOT USED: Microchip Graphics HX8347.c -- its licence forbids use except when
 *     embedded on a Microchip MCU, so none of its values were vendored. It only
 *     served to cross-check register meanings that are also in the datasheet.
 *
 * ============================ UNPROVEN ON SILICON ============================
 * These bytes have been proven to COMPILE and to be a faithful copy of a good
 * source. They have NOT been proven to light THIS panel. Nothing downstream
 * consumes this table in sim (the bench streams its own synthetic vector on
 * purpose), so a wrong value here surfaces only at the board. The entries most
 * likely to need a bring-up tweak are the module-specific ones flagged
 * [MODULE] below -- MADCTL (0x16) and PANEL (0x36) -- where ST and nopnop2002
 * DISAGREE, i.e. they depend on how the glass is mounted, not on the silicon.
 * ============================================================================
 */

#include "hx8347_init.h"

/*
 * HX8347-D 8080 register access: write the register index with RS=0 (HX_CMD),
 * then its datum with RS=1 (HX_DAT). Multi-parameter registers are written as
 * an index followed by several HX_DAT bytes. This is the exact model ST's
 * hx8347d_write_reg() and nopnop2002's lcd_write_comm_byte()/_data_byte() use.
 *
 * Orientation baked in = LANDSCAPE (ST HX8347D_ORIENTATION_LANDSCAPE, 0x02),
 * giving X=320 / Y=240 to match the 40x15 text layout in the plan (section 8).
 * Pixel format baked in = RGB565 16bpp (COLMOD 0x17 = 0x05). See Q3/Q6 in
 * PANEL_PROVENANCE.md: over the 8-bit bus a pixel is two writes, HIGH byte
 * (Color>>8) first, LOW byte (Color&0xFF) second.
 *
 * Comment on each line = the ST symbol name (its address is fixed in
 * hx8347d_reg.h) -- that IS the per-entry citation.
 */
const hx8347_entry_t hx8347_init[] = {
    /* Datasheet 11.5.4 note (5): "wait 5 msec after releasing !RES before
     * sending commands." (RST/NRESET is active-low; tRESW min = 10 us; the
     * reset-complete/blank window tREST is up to 120 ms -- that is the origin of
     * the commonly-quoted "120 ms", it is NOT a MIPI sleep-out.) The physical
     * reset pulse + settle is driven by clcd.c's reset FSM; this is a belt-and-
     * braces floor honoured as a poll-and-return deadline. */
    { HX_DLY, 5 },

    /* --- Driving-ability setting (ST hx8347d.c) --------------------------- */
    { HX_CMD, 0xEA }, { HX_DAT, 0x00 },  /* POWER_CTRL_INTERNAL_USED1  */
    { HX_CMD, 0xEB }, { HX_DAT, 0x20 },  /* POWER_CTRL_INTERNAL_USED2  */
    { HX_CMD, 0xEC }, { HX_DAT, 0x0C },  /* SOURCE_CTRL_INTERNAL_USED1 */
    { HX_CMD, 0xED }, { HX_DAT, 0xC4 },  /* SOURCE_CTRL_INTERNAL_USED2 */
    { HX_CMD, 0xE8 }, { HX_DAT, 0x40 },  /* SOURCE_OP_CTRL1  [MODULE: nopnop=0x38] */
    { HX_CMD, 0xE9 }, { HX_DAT, 0x38 },  /* SOURCE_OP_CTRL2  [MODULE: nopnop=0x10] */
    { HX_CMD, 0x27 }, { HX_DAT, 0xA3 },  /* DISPLAY_CTRL2 (driving)    */

    /* --- Gamma curve (ST GAMMA_CTRL1..27; byte-identical in nopnop2002) --- */
    { HX_CMD, 0x40 }, { HX_DAT, 0x01 },
    { HX_CMD, 0x41 }, { HX_DAT, 0x00 },
    { HX_CMD, 0x42 }, { HX_DAT, 0x00 },
    { HX_CMD, 0x43 }, { HX_DAT, 0x10 },
    { HX_CMD, 0x44 }, { HX_DAT, 0x0E },
    { HX_CMD, 0x45 }, { HX_DAT, 0x24 },
    { HX_CMD, 0x46 }, { HX_DAT, 0x04 },
    { HX_CMD, 0x47 }, { HX_DAT, 0x50 },
    { HX_CMD, 0x48 }, { HX_DAT, 0x02 },
    { HX_CMD, 0x49 }, { HX_DAT, 0x13 },
    { HX_CMD, 0x4A }, { HX_DAT, 0x19 },
    { HX_CMD, 0x4B }, { HX_DAT, 0x19 },
    { HX_CMD, 0x4C }, { HX_DAT, 0x16 },
    { HX_CMD, 0x50 }, { HX_DAT, 0x1B },
    { HX_CMD, 0x51 }, { HX_DAT, 0x31 },
    { HX_CMD, 0x52 }, { HX_DAT, 0x2F },
    { HX_CMD, 0x53 }, { HX_DAT, 0x3F },
    { HX_CMD, 0x54 }, { HX_DAT, 0x3F },
    { HX_CMD, 0x55 }, { HX_DAT, 0x3E },
    { HX_CMD, 0x56 }, { HX_DAT, 0x2F },
    { HX_CMD, 0x57 }, { HX_DAT, 0x7B },
    { HX_CMD, 0x58 }, { HX_DAT, 0x09 },
    { HX_CMD, 0x59 }, { HX_DAT, 0x06 },
    { HX_CMD, 0x5A }, { HX_DAT, 0x06 },
    { HX_CMD, 0x5B }, { HX_DAT, 0x0C },
    { HX_CMD, 0x5C }, { HX_DAT, 0x1D },
    { HX_CMD, 0x5D }, { HX_DAT, 0xCC },

    /* --- Power-voltage setting (ST hx8347d.c) ---------------------------- */
    { HX_CMD, 0x1B }, { HX_DAT, 0x1B },  /* POWER_CTRL2 (VRH ~4.65V)   */
    { HX_CMD, 0x1A }, { HX_DAT, 0x01 },  /* POWER_CTRL1 (BT)           */
    { HX_CMD, 0x24 }, { HX_DAT, 0x2F },  /* VCOM_CTRL2 (VMH)           */
    { HX_CMD, 0x25 }, { HX_DAT, 0x57 },  /* VCOM_CTRL3 (VML)           */
    { HX_CMD, 0x23 }, { HX_DAT, 0x86 },  /* VCOM_CTRL1  [MODULE: nopnop=0x88; reloadable from OTP] */

    /* --- Power-on setting-up flow (ST hx8347d.c) ------------------------- */
    { HX_CMD, 0x18 }, { HX_DAT, 0x36 },  /* OSC_CTRL1 (~70Hz, RADJ=0110) [MODULE: nopnop=0x34] */
    { HX_CMD, 0x19 }, { HX_DAT, 0x01 },  /* OSC_CTRL2 (OSC_EN=1)       */
    { HX_CMD, 0x1C }, { HX_DAT, 0x06 },  /* POWER_CTRL3 (AP=111)       */
    { HX_CMD, 0x1D }, { HX_DAT, 0x06 },  /* POWER_CTRL4 (AP=111)       */
    { HX_CMD, 0x1F }, { HX_DAT, 0x90 },  /* POWER_CTRL6 (PON=1,STB=0)  */
    { HX_CMD, 0x26 }, { HX_DAT, 0x01 },  /* DISPLAY_CTRL1 (REF=1)      */
    { HX_DLY, 10 },                      /* ST HX8347D_IO_Delay(10)    */

    /* --- Colour depth + panel (ST hx8347d.c) ----------------------------- */
    { HX_CMD, 0x17 }, { HX_DAT, 0x05 },  /* COLMOD -> RGB565 16bpp (0x06 would be 262k/18bpp) -- Q3 */
    { HX_CMD, 0x36 }, { HX_DAT, 0x09 },  /* PANEL_CTRL (SS=1,BGR=1)  [MODULE: nopnop=0x00] */

    /* --- Display-ON flow (ST hx8347d.c) ---------------------------------- */
    { HX_CMD, 0x28 }, { HX_DAT, 0x38 },  /* DISPLAY_CTRL3 (GON=1,DTE=1,D=10) */
    { HX_DLY, 100 },                     /* ST HX8347D_IO_Delay(100)   */
    { HX_CMD, 0x28 }, { HX_DAT, 0x3C },  /* DISPLAY_CTRL3 (GON=1,DTE=1,D=11) */
    { HX_DLY, 100 },                     /* ST HX8347D_IO_Delay(100)   */

    /* --- Partial-display / GRAM mode (ST hx8347d.c) ---------------------- */
    { HX_CMD, 0x01 }, { HX_DAT, 0x00 },  /* DISPLAY_MODE_CTRL (no scroll/partial) */

    /* --- Address window, LANDSCAPE branch (ST SetOrientation) ------------ */
    { HX_CMD, 0x08 }, { HX_DAT, 0x00 },  /* ROW_ADDRESS_END2   } row end  = 0x00EF (239 -> 240 rows) -- Q2 */
    { HX_CMD, 0x09 }, { HX_DAT, 0xEF },  /* ROW_ADDRESS_END1   }                                          */
    { HX_CMD, 0x04 }, { HX_DAT, 0x01 },  /* COLUMN_ADDRESS_END2} col end  = 0x013F (319 -> 320 cols) -- Q2 */
    { HX_CMD, 0x05 }, { HX_DAT, 0x3F },  /* COLUMN_ADDRESS_END1}                                          */
    /* MEMORY_ACCESS_CTRL (MADCTL). Vendored value = OrientationTab[LANDSCAPE][0]
     * = 0xE0 (MY|MX|MV)  [MODULE: nopnop=0x98]. This is the ONE deliberate
     * deviation from the ST table (see PANEL_PROVENANCE.md "Local deviations"):
     * with CLCD_ROTATE_180 (default ON) we ship 0xE0 ^ (MY|MX) = 0x20, the other
     * landscape orientation, because the panel is mounted upside-down relative to
     * the reading position. MV and every non-geometry bit are preserved, so the
     * col-end 0x013F / row-end 0x00EF window above stays correct and the
     * renderer's per-cell window math needs no mirroring -- the full reasoning
     * (and why mirroring the coordinates would be WRONG) is in hx8347_init.h. */
    { HX_CMD, HX_REG_MADCTL }, { HX_DAT, HX_MADCTL_VALUE },

    /* --- Cursor origin (0,0) (ST SetCursor) ------------------------------ */
    { HX_CMD, 0x06 }, { HX_DAT, 0x00 },  /* ROW_ADDRESS_START2    } row start = 0 -- Q2 */
    { HX_CMD, 0x07 }, { HX_DAT, 0x00 },  /* ROW_ADDRESS_START1    }                     */
    { HX_CMD, 0x02 }, { HX_DAT, 0x00 },  /* COLUMN_ADDRESS_START2 } col start = 0 -- Q2 */
    { HX_CMD, 0x03 }, { HX_DAT, 0x00 },  /* COLUMN_ADDRESS_START1 }                     */

    /* --- Leave the index register at GRAM-write (ST writes 0x22 + 1 byte) -
     * The renderer re-issues its own window + 0x22 before each glyph, so the
     * trailing datum is a harmless single black pixel at (0,0). GRAM-write
     * command = 0x22 (READ_DATA / SRAM write). -- Q2 */
    { HX_CMD, 0x22 }, { HX_DAT, 0x00 },
};

const unsigned hx8347_init_len =
    (unsigned)(sizeof(hx8347_init) / sizeof(hx8347_init[0]));

const char hx8347_init_provenance[] =
    "HX8347-D init: TRANSCRIBED from STMicroelectronics/stm32-hx8347d "
    "(hx8347d.c HX8347D_Init, hx8347d_reg.h, hx8347d.h; branch main, "
    "commit 9758706; BSD-3-Clause). Corroborated by nopnop2002/"
    "esp-idf-parallel-tft hx8347.c (commit 1dee984, MIT). Semantics/timing: "
    "Himax HX8347-D(T) DS ver.02 Mar-2009 (5.1.2/Fig5.22, 8.16, 11.5.4). "
    "LANDSCAPE 320x240, RGB565 (COLMOD 0x17=0x05). UNPROVEN until the panel "
    "lights; see firmware/clcd/PANEL_PROVENANCE.md. MADCTL 0x16 / PANEL 0x36 "
    "are module-specific and the likeliest bring-up tweaks. LOCAL DEVIATION: "
    "MADCTL 0x16 ships 0x20 (ST 0xE0 with MY|MX toggled) under CLCD_ROTATE_180=1 "
    "-- the 180-degree-rotated landscape; build with -DCLCD_ROTATE_180=0 for ST's "
    "as-vendored orientation.";
