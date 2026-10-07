/*
 * hx8347_init.h -- HX8347-D panel init-table seam (declarations).
 *
 * FROZEN encoding, per fpga/shell/ip/clcd/README.md ("The init-table seam").
 * The struct and the three opcodes below are the contract the sibling driver
 * (firmware/clcd/clcd.c) links against. Do NOT change names or values here.
 *
 * The table itself lives in hx8347_init.c. Its provenance -- the exact source,
 * revision, licence, and datasheet cross-checks for every byte -- is recorded
 * in firmware/clcd/PANEL_PROVENANCE.md. Every value is UNPROVEN until the panel
 * physically lights (plan docs/CLCD_STATUS_DISPLAY_PLAN.md 13-Q2).
 */
#ifndef HX8347_INIT_H
#define HX8347_INIT_H

#include <stdint.h>

/* Opcodes carried in hx8347_entry_t.op. FROZEN -- match the seam exactly. */
#define HX_CMD 0u  /* val -> CLCD_CMD  (RS=0): an HX8347 register index byte  */
#define HX_DAT 1u  /* val -> CLCD_DATA (RS=1): the datum for the last index   */
#define HX_DLY 2u  /* val = milliseconds to wait; the driver arms a timed,
                    * poll-and-return deadline. NEVER a busy-wait.            */

typedef struct { uint8_t op; uint8_t val; } hx8347_entry_t;

/* ==========================================================================
 * Orientation -- MADCTL (register 0x16) and the CLCD_ROTATE_180 build switch
 * ==========================================================================
 * HX8347-D reg 0x16 MEMORY_ACCESS_CTRL carries the MIPI-style scan-direction
 * bits. Only the three geometry bits matter here; the rest (ML/BGR/MH) are left
 * exactly as the vendored ST value had them, so the switch below can never
 * disturb colour order or refresh order:
 *
 *   bit7 MY  row    address order   (flip top<->bottom)
 *   bit6 MX  column address order   (flip left<->right)
 *   bit5 MV  row/column exchange    (portrait <-> landscape)
 *   bit4 ML  vertical refresh order
 *   bit3 BGR RGB/BGR order          (this panel sets BGR in PANEL_CTRL 0x36)
 *   bit2 MH  horizontal refresh order
 *
 * HX_MADCTL_LANDSCAPE (0xE0 = MY|MX|MV) is the value transcribed from ST's
 * OrientationTab[HX8347D_ORIENTATION_LANDSCAPE] -- see hx8347_init.c and
 * PANEL_PROVENANCE.md. A 180-degree rotation of a landscape image is exactly
 * "flip both scan axes": toggle MY and MX together, leave MV (and every other
 * bit) alone. 0xE0 ^ 0xC0 = 0x20 (MV only) -- the OTHER landscape orientation,
 * the same pair of values every mainstream driver for this controller family
 * uses for its two landscape rotations.
 *
 * GRAM WINDOWING IS UNAFFECTED. MY/MX/MV define the mapping between the command
 * coordinate space (the 0x02-0x09 column/row address window + the 0x22 GRAM
 * write auto-increment) and the physical glass -- they are one transform applied
 * uniformly to BOTH the window addressing and the pixel fill. The renderer in
 * clcd.c therefore keeps emitting x0..x1 / y0..y1 in the SAME 320x240 logical
 * frame with the origin at the logical top-left, and the whole composed image
 * simply lands rotated. Do NOT mirror the coordinates (col -> W-1-col etc.) to
 * "compensate": that would cancel the rotation for cell POSITIONS while the
 * glyph CONTENT stayed rotated, i.e. produce garbage. The two proofs that this
 * is the controller's model, not the "counter direction only" model:
 *   - ST's own driver uses ONE SetCursor()/window path for all four
 *     orientations, changing only OrientationTab; and its LANDSCAPE branch
 *     writes col-end 0x013F (319) / row-end 0x00EF (239) -- i.e. with MV set the
 *     COLUMN registers already span the 320 axis, so the address registers are
 *     downstream of the MADCTL transform.
 *   - The two landscape values 0x20 and 0xE0 differ only in MY|MX, and drivers
 *     use identical window math for both.
 * The GRAM is exactly panel-sized (240x320), so there is also no dead-row/column
 * offset to add when the origin corner moves (the ST7735-style "colstart" trap).
 *
 * Set CLCD_ROTATE_180=0 at build time to go back to the as-vendored landscape.
 */
#define HX_REG_MADCTL       0x16u
#define HX_MADCTL_MY        0x80u
#define HX_MADCTL_MX        0x40u
#define HX_MADCTL_MV        0x20u
#define HX_MADCTL_LANDSCAPE 0xE0u   /* ST OrientationTab[LANDSCAPE] = MY|MX|MV */

#ifndef CLCD_ROTATE_180
#define CLCD_ROTATE_180 1           /* default ON: the panel is mounted upside-down */
#endif

#if CLCD_ROTATE_180
#define HX_MADCTL_VALUE \
    ((uint8_t)(HX_MADCTL_LANDSCAPE ^ (HX_MADCTL_MY | HX_MADCTL_MX)))   /* 0x20 */
#else
#define HX_MADCTL_VALUE ((uint8_t)HX_MADCTL_LANDSCAPE)                 /* 0xE0 */
#endif

extern const hx8347_entry_t hx8347_init[];
extern const unsigned       hx8347_init_len;
extern const char           hx8347_init_provenance[];  /* source URL/doc + rev */

#endif /* HX8347_INIT_H */
