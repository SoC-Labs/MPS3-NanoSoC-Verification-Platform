# HX8347-D panel init-table provenance

**Outcome: (A) — a cited table.** Network fetches succeeded in this sandbox.
The init sequence in `hx8347_init.c` is a faithful transcription of a permissively
licensed open-source HX8347-D driver, corroborated by a second independent driver
and cross-checked against the Himax datasheet. **No register value was written
from memory.**

> **Every value here is UNPROVEN until the panel physically lights.** A datasheet
> and two good drivers make the bytes *plausible and traceable*; they do not make
> them *correct for the MCBQVGA-TS as wired on this MPS3*. The sibling's bench
> deliberately does not consume this table (it streams a synthetic vector), so a
> wrong byte will surface only at bring-up (plan `docs/CLCD_STATUS_DISPLAY_PLAN.md`
> §13-Q2). Treat this as "assumed-correct, board-unconfirmed."

Controller confirmed by the board TRM: Himax **HX8347-D**, module MCBQVGA-TS,
320x240, 8-bit 8080 parallel (TRM §2.11). This matches the datasheet part below.

---

## Sources

### PRIMARY — values + register map (vendored)
- **Repo:** `STMicroelectronics/stm32-hx8347d`, branch `main`, commit `9758706`
  (latest touching `hx8347d.c`, dated 2026-02-16).
- **Files used:**
  - `hx8347d.c` — `HX8347D_Init()`, `HX8347D_SetOrientation()`,
    `HX8347D_SetCursor()`: the ordered register writes and the 10/100/100 ms
    delays. Raw:
    `https://raw.githubusercontent.com/STMicroelectronics/stm32-hx8347d/main/hx8347d.c`
  - `hx8347d_reg.h` — the register-symbol → address map (0x00…0xED). Raw:
    `https://raw.githubusercontent.com/STMicroelectronics/stm32-hx8347d/main/hx8347d_reg.h`
  - `hx8347d.h` — `HX8347D_FORMAT_RBG565 = 0x05U` (16bpp), `_RBG666 = 0x06U`
    (18bpp), `HX8347D_ORIENTATION_LANDSCAPE = 0x02U`, `HX8347D_ID = 0x0047`,
    `WIDTH=320 HEIGHT=240`.
- **Licence: BSD-3-Clause** (`LICENSE.md`, "Copyright 2018 STMicroelectronics.
  All rights reserved."). **Vendoring permitted** — redistribution in source form
  is allowed provided the copyright notice, the condition list, and the disclaimer
  are retained. That notice is reproduced at the end of this file, and
  `hx8347_init.c` names ST + the licence + the commit. (Numeric register
  addresses and init constants are functional facts; the BSD-3-Clause attribution
  is retained regardless, out of caution and courtesy.)

### CORROBORATION — independent second source
- **Repo:** `nopnop2002/esp-idf-parallel-tft`, commit `1dee984`,
  `components/tft_library/hx8347.c`. **Licence: MIT** (`LICENSE`, "Copyright (c)
  2021 nopnop2002") — also vendorable.
- **Agreement with ST (panel-generic values):** the two gamma blocks
  (`0x40..0x4C`, `0x50..0x5D`) are **byte-identical**; both use `0x17=0x05`
  (65k/RGB565), the `0x28`=`0x38`→`0x3C` display-on pair, window registers
  `0x02-0x09`, and GRAM-write `0x22`. Both send the colour word **high byte
  first** (`(color>>8)` then `(color&0xFF)`).
- **Disagreement with ST (module-specific analog/orientation trim):** these are
  where the two drivers differ, i.e. values that depend on the glass module, not
  the silicon — flagged `[MODULE]` in `hx8347_init.c`:

  | Reg  | Meaning              | ST (vendored) | nopnop2002 |
  |------|----------------------|---------------|------------|
  | 0xE8 | SOURCE_OP_CTRL1      | 0x40          | 0x38       |
  | 0xE9 | SOURCE_OP_CTRL2      | 0x38          | 0x10       |
  | 0x23 | VCOM_CTRL1 (flicker) | 0x86          | 0x88       |
  | 0x18 | OSC_CTRL1 (framerate)| 0x36          | 0x34       |
  | 0x36 | PANEL_CTRL (SS/BGR)  | 0x09          | 0x00       |
  | 0x16 | MADCTL (orientation) | 0xE0 (landsc.)| 0x98       |

  `0x16` and `0x36` are the likeliest bring-up tweaks (mirroring / RGB-vs-BGR /
  scan direction). nopnop2002 also runs an explicit `0x1F` PON ramp
  (0x88→0x80→0x90→0xD0 with 5/3/5/5 ms delays); ST reaches the same PON state via
  `0x1C/0x1D` then `0x1F=0x90`. Both are legitimate; ST's is vendored.

### LOCAL DEVIATIONS from the vendored table

The table is otherwise a byte-faithful transcription. **One value is deliberately
ours, not ST's**, and it is generated from a build switch rather than typed in:

| Reg | ST (vendored) | Shipped | Switch | Why |
|-----|---------------|---------|--------|-----|
| `0x16` MADCTL | `0xE0` (MY\|MX\|MV) | **`0x20`** (MV) | `CLCD_ROTATE_180` (default **1**) | The panel is mounted upside-down relative to the reading position; rotate the image 180°. |

`0x20 = 0xE0 ^ (MY|MX)` — a 180° rotation of a landscape image is exactly "flip
both scan axes", so `MY` (bit7) and `MX` (bit6) toggle **together** and every
other bit — `MV` (bit5, row/column exchange), `ML`, `BGR`, `MH` — is preserved.
`0x20` and `0xE0` are the two landscape orientations of this controller family
(the same pair mainstream drivers use for their two landscape rotations); the
image stays 320×240 and the table's `col-end 0x013F` / `row-end 0x00EF` window
remains correct. Build with `-DCLCD_ROTATE_180=0` for ST's as-vendored
orientation. Definitions and the full argument live in `hx8347_init.h`.

**The renderer's GRAM window math is unchanged, on purpose.** `MY/MX/MV` map the
*command* coordinate space (the `0x02`–`0x09` window + the `0x22` auto-increment
that fills it) onto the glass as **one** transform applied to both, so
`clcd.c:build_cell()` keeps emitting logical top-left-origin coordinates and the
composed image simply lands rotated. Mirroring the coordinates
(`col → 319-col`) to "compensate" would un-rotate cell *positions* while leaving
glyph *content* rotated — garbage. Two checks that this is the controller's
model: ST drives all four orientations through **one** `SetCursor()`/window path,
changing only `OrientationTab`; and its LANDSCAPE branch writes col-end `0x013F`
(319) / row-end `0x00EF` (239), i.e. with `MV` set the COLUMN registers already
span the 320 axis — so the address registers sit *downstream* of the MADCTL
transform. The GRAM is exactly panel-sized (240×320), so no origin-corner offset
(the ST7735 `colstart` trap) applies either.

> **Still board-open.** Which physical corner is "up" is a mounting fact. If the
> glass turns out to have been the right way up, set `CLCD_ROTATE_180=0`. If it
> comes out *mirrored* rather than rotated, that is the opposite failure and
> means this reasoning is wrong for this silicon — then, and only then, mirror
> the renderer's window coordinates instead. `firmware/test/test_clcd.c`
> (`test_madctl_orientation`) pins the shipped byte and its bit invariants; it
> cannot pin what the glass does.

### DATASHEET — semantics + timing (not vendored, cited)
- **Himax "HX8347-D(T)" Data Sheet**, DOC No. `HX8347-D(T)-DS`, **Version 02,
  March 2009**. Fetched from
  `https://www.displayfuture.com/Display/datasheet/controller/HX8347-D.pdf`
  (176 pages; extracted locally with pypdf). Himax-confidential document — used
  only to confirm register meanings and timing, **no text reproduced beyond short
  factual figures**.

### NOT USED — proprietary
- `mentatpsi/Microchip .../HX8347.c` carries a **Microchip-only licence**
  ("...only when embedded on a Microchip microcontroller..."). **No value from it
  was vendored.** It was read solely to sanity-check register meanings that are
  independently present in the datasheet and the two open drivers above.

---

## Answers to the open questions (plan §13)

Each is answered *from a source*, then flagged for the one thing only the board
can settle.

### Q2 — GRAM column/row window registers + the GRAM-write command
**Sourced (ST `hx8347d_reg.h` + datasheet 5.x):**
- Column address: start `0x02` (hi) / `0x03` (lo); end `0x04` (hi) / `0x05` (lo).
- Row address: start `0x06` (hi) / `0x07` (lo); end `0x08` (hi) / `0x09` (lo).
- **GRAM write command = `0x22`** (index it with RS=0, then stream pixel data
  with RS=1; the controller auto-increments within the window).
- Access model: index byte with RS=0 (`HX_CMD`), datum with RS=1 (`HX_DAT`).
- In the table the window is baked to landscape 320x240 (col-end `0x013F`,
  row-end `0x00EF`) with cursor at (0,0). **The renderer must set its own window +
  `0x22` before each glyph** — do not rely on the init window.
- *Board-open:* none on the register identities (two drivers + datasheet agree);
  the correct end-address orientation follows from Q's MADCTL choice.

### Q3 — Colour depth available and the register that selects it
**Sourced (ST `hx8347d.h` + datasheet 8.16 / Fig captions R17H=05h/06h):**
- Register **`0x17` (COLMOD)**. **`0x05` = RGB565, 16bpp (65,536 colours)**;
  `0x06` = RGB666, 18bpp (262,144); `0x03` = RGB444, 12bpp. ST's default is
  RGB565. **Table uses `0x05` (RGB565)** — the plan's recommended v1 format,
  2 bytes/pixel over the 8-bit bus. Both drivers agree on `0x05`.
- *Board-open:* that the module is actually strapped/wired to honour 16bpp (vs a
  fixed 18bpp panel build) — confirm at bring-up.

### Q6 — Data-bus byte order for RGB565
**Sourced (datasheet 5.1.2, Fig 5.22 "8-bit bus / 16-bit-data input, R17H=05h";
and both drivers' pixel writes):**
- **HIGH byte first.** For an RGB565 word, the first 8-bit transfer carries
  `{R4..R0, G5..G3}` (bits 15:8), the second carries `{G2..G0, B4..B0}`
  (bits 7:0). ST writes `(Color>>8)&0xFF` then `Color&0xFF`; nopnop2002 does the
  same. So the sibling packs **`(pixel>>8)` first, then `(pixel&0xFF)`**.
- *Board-open:* the physical mapping `CLCD_PD[17:10] → DB[7:0]` (which pad is the
  MSB) is a wiring fact (plan §13-Q6). The datasheet says DB7..DB0; confirm the
  XDC bit order at bring-up. Also RGB-vs-BGR is set by MADCTL/PANEL, not byte
  order (see Q7/`[MODULE]`).

### Q7 — RST/BL polarity, reset pulse width, sleep-out settle
**Sourced (datasheet 11.5.4 "Reset input timing", Fig 11.5 + notes):**
- **RST (NRESET) is ACTIVE-LOW** (the "N"/"!" prefix; the RTL seam already models
  `clcd_rst_n_o` active-low). Assert low to reset.
- **Minimum reset low pulse `tRESW` = 10 µs** (a pulse <5 µs is rejected;
  5–10 µs "reset start"; >10 µs is a valid reset). During STB-OUT the figure is
  5 ms.
- **Reset-complete / blank window `tREST` ≈ 120 ms** (typ/max, when reset is
  applied during STB mode). *This is the true origin of the "~120 ms" figure the
  plan quotes* — it is the HX8347's reset/blanking time, **not** a MIPI sleep-out
  (the HX8347-D has no `0x11` sleep-out; power-up is the `0x1C/0x1D/0x1F` +
  `0x28` GON/DTE flow instead).
- **`tPRES` ≥ 1 ms:** NRESET must go high at least 1 ms after power-on.
- Datasheet **note (5): "wait 5 msec after releasing !RES before sending
  commands."** → the table opens with `{HX_DLY, 5}`; the physical reset pulse and
  its settle are owned by `clcd.c`'s reset FSM.
- **Init-flow settles (ST):** 10 ms after the power-on block, then 100 ms + 100 ms
  across the two-step display-on (`0x28`=`0x38`→`0x3C`). Encoded as `HX_DLY`
  entries, never busy-waits.
- **CLCD_BL polarity: NOT in any datasheet/driver — a board fact.** The legacy
  tie-off (`nanosoc_mps3_top.sv:445-455`) drives `CLCD_BL=0` while the panel is
  dark, which *implies* **active-high (1 = backlight on)**, but that is an
  inference from the tie-off, not a sourced fact. **Confirm at bring-up.**

---

## What someone must still do (the one-line board asks)

All of the above is sourced but **board-unconfirmed**. At bring-up, in order:
1. Confirm **CLCD_BL** active level (assumed active-high) and that **RST** low
   actually resets (assumed active-low, `tRESW ≥ 10 µs`, then ≥ 5 ms before
   commands).
2. Confirm **MADCTL `0x16`** (now **`0x20`** — ST's `0xE0` rotated 180°, see
   "Local deviations"; `-DCLCD_ROTATE_180=0` reverts to `0xE0`) and **PANEL
   `0x36`** (`0x09` assumed) give upright, correctly-mirrored, RGB-correct
   output — these are the `[MODULE]` values where ST and nopnop2002 disagree.
   Flip MADCTL scan bits / BGR bit if the image is mirrored or colour-swapped.
   *Upside-down ⇒ flip `CLCD_ROTATE_180`. **Mirrored** (not rotated) ⇒ a
   different bug: the renderer's window coordinates, not this byte.*
3. Confirm **`CLCD_PD[17:10] → DB[7:0]`** bit order so the high-byte-first RGB565
   packing lands right (Q6).
4. Optionally read register `0x00` — HX8347-D returns ID **`0x0047`** — as a
   panel-present self-test (plan §13-Q5; only if the board's CLCD buffers allow
   read-back).

If the panel stays dark or garbled after these, the module-specific analog trim
(`[MODULE]` rows above) is the next suspect: try nopnop2002's alternative values,
which target a different HX8347-D glass.

---

## Retained licence notice (BSD-3-Clause, STMicroelectronics/stm32-hx8347d)

```
Copyright 2018 STMicroelectronics.
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this
   list of conditions and the following disclaimer in the documentation and/or
   other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may
   be used to endorse or promote products derived from this software without
   specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" ...
(full disclaimer in the upstream LICENSE.md).
```

nopnop2002/esp-idf-parallel-tft is MIT ("Copyright (c) 2021 nopnop2002").
