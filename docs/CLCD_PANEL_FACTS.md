# CLCD panel facts — the configuration that is PROVEN to light the MPS3 panel

> **Status: HISTORICAL** — a record of the CLCD panel configuration proven to light the MPS3 panel as of 2026-07-15.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `CLCD_KVM_WAVE_PLAN.md`, `CLCD_PHASE_D_INTEGRATION.md` — internal notes, not in the public tree.

> **Status: BOARD-CONFIRMED.** The on-board QVGA CLCD is **lit and rendering the
> harness status screen** on the MPS3 — observed on the bench by the project lead,
> **2026-07-14**. This document is the written record of *what that working
> configuration actually is*, so that the CLCD KVM bench and the student's
> DUT-side display driver are written against the **real** panel, not the
> assumed one.
>
> Every row below carries a source. Facts are graded:
>
> | Grade | Meaning |
> |---|---|
> | **PROVEN** | The panel lighting/rendering *cannot* be true unless this is true. Board-observed 2026-07-14. |
> | **BUILT** | Mechanically verified in the tree / build reports (file:line, `post_impl_io.rpt`). Not a panel behaviour. |
> | **STILL ASSUMED** | Shipped, and the panel works — but the working panel does **not** exercise it, so it is *not* proven. Treat with the same suspicion as before bring-up. |
>
> **The grading is the point of this document.** "The panel lights" proves a
> great deal, but it does not prove anything the driver never does. Read the
> STILL-ASSUMED section (§7) before you rely on read-back or on colour.

---

## 0. The exact configuration under test

| What | Value | Source |
|---|---|---|
| Shell bitstream | `static_id` **`0xE4B1C44A`**, commit `b4afe3e` (2026-07-11) | `fpga/dfx/overlay/mps3_shell_static_id.c:18`; `fpga/dfx/build_clcd/gate_verify.log` ("all 8 overlay static_ids match the shell") |
| Build evidence | `fpga/shell/build_results_2026-07-11/` (`bd_summary.txt:11` `clcd_0`, `:51` `SEG_clcd_0_reg0 offset=0x44AC0000 range=0x00010000`) | BUILT |
| Firmware | built **`CLCD=1`** → `-DMPS3_HAS_CLCD`, links `clcd.c` + `hx8347_init.c` | `firmware/platform/Makefile:208-210`, `:223-227`, `:263` |
| Panel | Himax **HX8347-D** controller, MPS3 on-board QVGA module (MCBQVGA-TS), 320×240 | `firmware/clcd/hx8347_init.c:1-5`; board TRM 100765_0000_04_en §2.11 |
| Screen layout | 40 cols × 15 rows of 8×16 glyphs = 320×240 | `firmware/clcd/clcd.h:45-49` |

> **The `CLCD=1` flip is a build-time flag, not a tree state.** `CLCD ?=` is
> still **empty by default** (`firmware/platform/Makefile:208`), so a plain
> `make elf` produces an image with **no** CLCD driver. The working board is
> running a `make CLCD=1` build. Nothing in the tree records that; this line is
> the record. *(board-observed 2026-07-14)*

---

## 1. Q1 — Bus mode: **8080 8-bit parallel**. PROVEN.

The shell block `fpga/shell/ip/clcd/clcd.sv` is an **8080 parallel master and
nothing else**: a 3-phase strobe FSM (`SETUP → STROBE_LO → STROBE_HI`,
`clcd.sv:343-465`) driving `CS/RS/WR/PD[7:0]`. It has **no SPI shifter, no clock
divider, no serial mode, and no parameter that could select one** — the only
build-time options are `READ_PATH` and the three TIMING seeds (`clcd.sv:50-66`).

**Therefore: the panel renders ⇒ the module is strapped for the 8-bit 8080
parallel interface.** A serially-strapped HX8347-D would ignore every byte this
block emits; nothing would appear at all. This closes `CLCD_PHASE_D_INTEGRATION`
§9-Q1 — which warned "if strapped serial, the block is the wrong shape (a
different `clcd.sv`)". It is not the wrong shape.

Corroboration in the pinout: the pad is named `CLCD_WR_SCL` — the HX8347-D's
**dual WR/SCL pin** (`docs/contracts/shell-regmap.md:225`). It is being driven as
`WR`, not `SCL` (`shell_top.sv:334` `assign CLCD_WR_SCL = w_clcd_wr_n;`), and the
panel obeys.

**Nothing in the build contradicts this.** Grepped the whole tree: no SPI/serial
CLCD path exists anywhere — `clcd.sv` is the only owner RTL, and the only
`CLCD_*` XDC entries are the 14 parallel pads (`mps3_harness.xdc:235-268`).

---

## 2. Q6 — Data-bus bit order. **VERIFIED**, no longer assumed.

The mapping, as built:

| Block bit | Top-level pad | Package pin | Panel |
|---|---|---|---|
| `clcd_pd_o[0]` | `CLCD_PD[10]` | **AN17** | DB0 |
| `clcd_pd_o[1]` | `CLCD_PD[11]` | **AP16** | DB1 |
| `clcd_pd_o[2]` | `CLCD_PD[12]` | **AP18** | DB2 |
| `clcd_pd_o[3]` | `CLCD_PD[13]` | **AR18** | DB3 |
| `clcd_pd_o[4]` | `CLCD_PD[14]` | **AM16** | DB4 |
| `clcd_pd_o[5]` | `CLCD_PD[15]` | **AN16** | DB5 |
| `clcd_pd_o[6]` | `CLCD_PD[16]` | **AR17** | DB6 |
| `clcd_pd_o[7]` | `CLCD_PD[17]` | **AR16** | DB7 (MSB) |

- The generate loop: `IOBUF .IO(CLCD_PD[10 + gi]) .I(w_clcd_pd_o[gi])` —
  `fpga/shell/shell_top.sv:319-328` (`T = ~w_clcd_pd_oe`; Xilinx `T` is
  active-high Hi-Z).
- The pin constraints: `fpga/shell/constraints/mps3_harness.xdc:241-248`.
- Confirmed **as implemented**, not just as written:
  `fpga/shell/build_results_2026-07-11/post_impl_io.rpt` lists all eight as
  `BIDIR / LVCMOS18 / bank 66` at exactly those pins.

**PROVEN by the working board.** Every byte the panel receives — the register
*index* bytes as much as the data — goes over this bus. Any permutation of the
eight lines would corrupt every command index in the init table (e.g. COLMOD
`0x17` would arrive as some other register), and the panel would not initialise,
let alone render legible 8×16 glyphs. The mapping is right. `CLCD_PHASE_D
_INTEGRATION` §9-Q6's caveat ("the block-contract order, not yet a board-verified
electrical order") is **closed** — it is now board-verified.

**Byte order within a pixel** (a separate question, also proven): RGB565 is sent
**high byte first**, `(px >> 8)` then `(px & 0xFF)` — `firmware/clcd/clcd.c:648-649`,
matching Himax DS §5.1.2 / Fig 5.22.

---

## 3. Q3 — Colour depth / pixel format: **RGB565, 16 bpp**. PROVEN (depth), see §7 (order).

| Fact | Value | Source |
|---|---|---|
| `COLMOD` (reg `0x17`) | **`0x05`** = 65k colours, RGB565 16 bpp | `firmware/clcd/hx8347_init.c:131` (`0x06` = 18 bpp is *not* used) |
| Bytes per pixel on the 8-bit bus | **2**, high byte first | `firmware/clcd/clcd.c:648-649`, `:79` (`CLCD_CELL_PIXELS = 8*16*2 = 256`) |
| GRAM write opcode | `0x22` (RAMWR) | `firmware/clcd/clcd.c:74`, `hx8347_init.c:169` |
| Window registers | col `0x02..0x05`, row `0x06..0x09` (hi/lo pairs) | `firmware/clcd/clcd.c:66-73` |
| Colours used by the harness renderer | `WHITE 0xFFFF`, `BLACK 0x0000`, `RED 0xF800` | `firmware/clcd/clcd.c:61-63` |
| Cell stream | 17 preamble bytes (8 window regs ×2 + RAMWR) + 256 pixel bytes = **273** | `firmware/clcd/clcd.c:78-80` |

**PROVEN:** 16 bpp with 2 bytes/pixel MSB-first is the *only* packing under which
the byte stream this renderer emits produces legible glyphs. At 18 bpp the panel
would consume 3 bytes/pixel and the image would shear.

⚠️ **The channel order is NOT proven** — see §7.1. The harness screen is almost
entirely white-on-black, and white/black are invariant under an R↔B swap.

---

## 4. Q7 — `CLCD_BL` and `CLCD_RST` polarity. PROVEN.

Both pads are driven **straight through** from CTRL bits, with no inversion
anywhere between the register and the pin:

```
CTRL[1] backlight_q -> clcd_bl_o     (clcd.sv:281)  -> CLCD_BL  (shell_top.sv:337)
CTRL[2] reset_n_q   -> clcd_rst_n_o  (clcd.sv:282)  -> CLCD_RST (shell_top.sv:338)
```

What the working driver drives (`firmware/clcd/clcd.c:756-782`):

| Phase | CTRL written | `CLCD_RST` pad | `CLCD_BL` pad |
|---|---|---|---|
| `ST_RESET` | `ENABLE \| FIFO_RESET` | **0** (held in reset) | **0** (dark) |
| ≥ 2 ms later (`ST_RST_WAIT` → `ST_INIT`) | `ENABLE \| RESET_N \| BACKLIGHT` | **1** (released) | **1** (lit) |

- **`CLCD_BL` is ACTIVE-HIGH.** `1` = backlight ON. The driver sets
  `CLCD_CTRL_BACKLIGHT` and never clears it, and the panel is visibly lit.
  *(board-observed 2026-07-14)*
- **`CLCD_RST` is ACTIVE-LOW.** `0` = panel held in reset. The driver's only
  reset sequence is *hold low ≥ `CLCD_RESET_PULSE_MS` = 2 ms, then release high*
  (`clcd.c:56-58`, `:764`, `:770-778`), and the panel comes out of reset and
  accepts the init stream. If the polarity were inverted the panel would be held
  in reset for the entire run and never render.
- **CTRL reset value is `0x0`** (`clcd.sv:253-260`) → panel dark, held in reset,
  until firmware acts. That matches the legacy monolithic tie-off and is the safe
  power-on state.
- **Post-reset settle** is not a driver constant: it is the init table's leading
  `{HX_DLY, 5}` (`hx8347_init.c:74`), plus the `{HX_DLY,10}` / two `{HX_DLY,100}`
  entries in the display-on flow (`:128`, `:136`, `:138`). This sequence works.

> ⚠️ **`firmware/clcd/clcd.c:774-777` still carries a stale comment** — *"CLCD_BL
> polarity is UNVERIFIED (inferred active-high from the legacy tie-off only …) to
> be confirmed at bring-up."* It **has** been confirmed. The comment is wrong, not
> the code. (Not corrected here: `clcd.c` is outside this document's file
> ownership. Flagged for the integrator.)

---

## 5. Q5 — `READ_PATH`: shipped **0**. Read-back is **NOT** used and **NOT** proven.

- **Shipped value: `READ_PATH = 0`** — the RTL default (`clcd.sv:61`).
  `shell_bd.tcl` instantiates `clcd_0` and overrides **only**
  `CONFIG.C_S_AXI_ADDR_WIDTH {32}` (`fpga/shell/bd/shell_bd.tcl:494-495`);
  `package_csr_ip.tcl:67` packages the block with its RTL defaults. **No
  `CONFIG.READ_PATH` exists anywhere in the tree** (grepped). The build is
  **write-only**.
- Consequences in the shipped hardware (`clcd.sv:504-505`):
  - `clcd_rd_n_o` is **hard-tied deasserted (1)** — `CLCD_RD` (AM15) is a
    constant-high output. Confirmed as built:
    `build_results_2026-07-11/post_impl_io.rpt` shows `CLCD_RD` as **`OUTPUT`**,
    while `CLCD_PD[*]` are `BIDIR`.
  - `clcd_pd_oe` is **always 1** — the FPGA drives the data bus at all times; the
    panel is never given the bus.
  - `CTRL[4] read_start` is gated by `READ_PATH` (`clcd.sv:251`) and is therefore
    **a no-op**; `READ` (0x10) always reads `0`.
- **Panel read-back is therefore never exercised by the working board.** The
  panel lighting says *nothing* about whether the MPS3's CLCD buffers are
  bidirectional. This remains open — see §7.2.

---

## 6. 8080 bus timing actually in use

`TIMING` (0x14) is written by the driver at every panel reset
(`firmware/clcd/clcd.c:760-763`) to exactly the RTL's reset defaults
(`clcd.sv:64-66`), so there is only one value to record:

| Field | Value | Meaning |
|---|---|---|
| `wr_lo` (`TIMING[7:0]`) | **4** | `STROBE_LO` — `WR` asserted low |
| `wr_hi` (`TIMING[15:8]`) | **4** | `STROBE_HI` — `WR` back high |
| `cs_setup` (`TIMING[23:16]`) | **2** | `SETUP` — `CS` low, `RS`/`PD` stable, before `WR` falls |

Units are **`s_axi_aclk` cycles**. `s_axi_aclk` is the shell clock =
`clk_wiz_shell` **CLKOUT1 = 100 MHz** (`fpga/shell/bd/shell_bd.tcl:199`, `:258`;
the CSR fan-out at `:499` clocks `clcd_0` from it) ⇒ **10 ns/cycle**.

**The resulting 8080 write cycle (PROVEN — this is what the panel is being driven
with right now):**

```
        |<-- SETUP -->|<- STROBE_LO ->|<- STROBE_HI ->|
CS_n  ‾‾\_____________________________________________/‾‾   (low for the whole cycle)
WR_n  ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\_______________/‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾
RS/PD  <-------------- stable for the whole cycle ------->
           20 ns            40 ns           40 ns
                                   ^ panel latches the byte on WR's rising edge
```

- `CS` setup before `WR` falls: **20 ns**
- `WR` low: **40 ns**
- `WR` high: **40 ns**
- `CS` is **deasserted between bytes** (it is asserted only in the three non-IDLE
  states, `clcd.sv:470-495`) — i.e. **one `CS` pulse per byte**, not one per
  burst. *The KVM's safe-switch gate can exploit this: a quiescent point (CS
  high, FSM `ST_IDLE`) exists after every single byte.*
- The FSM returns through `ST_IDLE` for ≥1 cycle before relaunching
  (`clcd.sv:422-435`), so the minimum byte-to-byte period is **≈110 ns**
  (11 cycles) ⇒ a hardware ceiling of **≈9.1 MB/s**.
- The **actual** rate is firmware-capped far below that:
  `CLCD_BYTES_PER_PASS = 256` bytes per superloop pass
  (`firmware/clcd/clcd.h:55-56`), refresh every `CLCD_REFRESH_MS = 250` ms
  (`clcd.h:62-63`).
- `FIFO_DEPTH = 128` `{RS,byte}` entries (`clcd.sv:55`); writes to `CMD`/`DATA`
  while `STATUS.fifo_full` are **dropped, never stalled** (`clcd.sv:31-36`,
  `:303`).

---

## 7. STILL ASSUMED — what the lit panel does **not** prove

**Read this before writing the KVM bench or the DUT-side driver.** A working
panel proves only the code paths the working driver takes.

### 7.1 RGB vs BGR channel order — SETTLED on silicon (2026-09-23); white point still open
`PANEL_CTRL` (reg `0x36`) ships **`0x09` (SS=1, BGR=1)** — ST's value
(`hx8347_init.c:132`); the corroborating source (nopnop2002) uses `0x00`, and
`PANEL_PROVENANCE.md:63-66` flags `0x36` as one of the two module-specific
`[MODULE]` bytes. The harness's own screen could not settle it (white and black
are invariant under an R↔B swap; its only colour was the `0xF800` fault banner).

**The clcd_demo RM settled it.** On 2026-09-23 the DUT drew SMPTE-style bars
through the KVM tunnel in the expected order — yellow, cyan, green, magenta, red,
blue, black — where an R↔B swap would have put cyan before yellow
(`docs/evidence/2026-09-w2/p5_clcd_demo_20260923.txt`, "VERDICT"). clcd_demo's
init table is **generated** from this harness's `hx8347_init.c`, so the same
`0x36` value is proven for the harness renderer too: **the channel order is RGB as
shipped.** Harness Manager's panel palette assumes exactly that
(`firmware/clcd/clcd_palette.h`, "channel order RGB").

**Still open — the white point, not the order.** White photographs as pale blue,
on the demo's white bar and on the harness screen alike. That is the backlight or
the white level, not a swap. HM's aligned palette draws text in `#e4e7ec`
(`0xE73D`, `CLCD_RGB565_TEXT_FG`), so the bench check is now **one red fill and one
`#e4e7ec` patch** side by side (HM CLCD_ALIGNMENT R6; §9 below).

### 7.2 `CLCD_RD` read-back — UNPROVEN (and un-built)
The board runs `READ_PATH=0` (§5). Nothing has ever driven `CLCD_RD` low or
tristated the data bus toward the panel. Whether the MPS3's CLCD buffers are
bidirectional at all is **exactly as unknown as it was before bring-up**. Do not
design a KVM or a student driver that depends on panel read-back (ID detection,
GRAM read, status polling) — it would require a shell rebuild (`READ_PATH=1`) and
a fresh `static_id` re-key, and it might still not work electrically.

### 7.3 The `[MODULE]` init bytes — working, but not individually validated
`PANEL_PROVENANCE.md:63-66` lists bytes where the two upstream sources disagree
(`0xE8`, `0xE9`, `0x23`, `0x18`, `0x16`, `0x36`). The shipped table
(ST's values, plus the local MADCTL deviation in §7.4) **produces a good picture**,
so it is *good enough* — but individual bytes have not been swept. If image
quality (gamma, VCOM flicker, contrast) is ever in question, these are the knobs;
they are firmware data (`hx8347_init.c`), never RTL.

### 7.4 Orientation — PROVEN, and note the local deviation
`MADCTL` (reg `0x16`) ships **`0x20`** (MV only), *not* ST's vendored `0xE0`
(MY|MX|MV): `CLCD_ROTATE_180` defaults to **1** because **the panel is physically
mounted upside-down** on the MPS3 (`firmware/clcd/hx8347_init.h:77-86`, `:41-47`).
That default was committed in `c77bf04` (2026-07-14) — i.e. it *is* a bring-up
finding, and the screen reads the right way up on the bench. **Grade: PROVEN.**
Listed here because it is a deviation from the cited upstream table and anyone
re-deriving the table from ST will get `0xE0` and a rotated screen.
Build `-DCLCD_ROTATE_180=0` for the as-vendored orientation.

### 7.5 Backlight is on/off only
`CLCD_BL` is driven by a single CTRL register bit (`clcd.sv:281`). There is **no
PWM, no brightness control, and no dimming path** in the shipped hardware. The
KVM's `BL` ownership is therefore a 1-bit mux, nothing more.

### 7.6 Touch is not in the design
The four touch pads (`CLCD_TSCL`/`TSDA`/`TINT`/`TNC`) are **deliberately
unconstrained** and have no ports (`mps3_harness.xdc:236-239`). No touch
capability exists. Do not offer it to students.

> **Superseded in part — see §11.** True as written (2026-07-14) and left as the
> record. A later mint added the AXI IIC master and the touch pins
> (`SHELL_TOUCH=1`; `firmware/platform/Makefile`'s `PRODUCT=1` includes
> `TOUCH=1`), and the STMPE811 has since answered on silicon. Touch still must
> not be offered to students: it has **never** reported a contact — §11 is the
> instrument built to find out why.

---

## 8. Address map / instantiation (BUILT — for the KVM's benefit)

| Property | Value | Source |
|---|---|---|
| Base | **`0x44AC_0000`**, 64 KiB page | `fpga/shell/bd/shell_bd.tcl:1108`; `build_results_2026-07-11/bd_summary.txt:51` |
| Interconnect | `NUM_MI` = **15**; CLCD is master index **14** | `shell_bd.tcl:1041`, `:1070` |
| Cell | `clcd_0`, `soclabs.org:user:clcd:1.0` | `shell_bd.tcl:494`; `bd_summary.txt:11` |
| **Decode width** | `C_S_AXI_ADDR_WIDTH` = **32** (the RTL default of 12 is *never* what ships) | `shell_bd.tcl:495`; `clcd.sv:201-215` |
| Register offsets | `CTRL 0x00`, `CMD 0x04`, `DATA 0x08`, `STATUS 0x0C`, `READ 0x10`, `TIMING 0x14` | `clcd.sv:218-223`; `firmware/common/platform_regs.h:527-532` |
| Read side effects | **none, at any offset** — deliberate (`csr_decode_width` sweeps the whole page; SWD/XVC dumps CSR pages) | `clcd.sv:310-341`, `:507-527` |
| DFX | **static peripheral** — never crosses the RP boundary, **no decoupler entry** | `docs/contracts/shell-regmap.md:229-230` |
| Pads | 14, all **`LVCMOS18`, bank 66**, `FIXED` | `build_results_2026-07-11/post_impl_io.rpt` (all 14 rows) |

> The KVM (`0x44AD_0000`) will re-mint `static_id` again. That is expected and
> scripted (`fpga/dfx/build_clcd/`), but it means **this document's §0 shell
> identity goes stale the moment the KVM lands** — everything else in this
> document is a *panel* fact and survives the rebuild.

---

## 9. What still needs the project lead's eyes on the bench

Short list, in priority order for the KVM work:

1. **The white point** (§7.1) — red is red (the clcd_demo bars proved the channel
   order on 2026-09-23); what is left is whether white is white. One saturated-red
   fill beside one `#e4e7ec` patch (the aligned theme's text colour) settles it —
   HM CLCD_ALIGNMENT R6, about 10 minutes in the next board window.
2. **Does read-back exist at all** (§7.2) — only answerable by building
   `READ_PATH=1` into a future shell (it rides the KVM's rebuild for free, if
   wanted). Until then, assume **no read-back**.
3. Nothing else. Q1 / Q3(depth) / Q6 / Q7 from
   `CLCD_PHASE_D_INTEGRATION.md` §9 are **closed** by the working board.

---

## 10. Provenance of this document

Written 2026-07-14 (W0-C of `CLCD_KVM_WAVE_PLAN.md`). Every file:line cite
above was re-read against the working tree at commit `be346f0`, not copied from
another doc — this repo's CLCD docs have gone stale twice already (see the header
of `CLCD_PHASE_D_INTEGRATION.md`). **Resolve state as: board > commits >
build reports > docs.**

Related: `fpga/shell/ip/clcd/README.md` (FROZEN port contract),
`firmware/clcd/PANEL_PROVENANCE.md` (where every init byte came from),
`docs/contracts/shell-regmap.md` §CLCD (the register contract),
`CLCD_PHASE_D_INTEGRATION.md` (how it landed).

---

## 11. Touch — root cause, and the plan for the next board window

> §10's provenance covers §0–§9. This section is a later addition and carries
> its own evidence. Everything below is marked MEASURED (on silicon), QUOTED
> (from a primary source, with the source named) or REASONED.
>
> **2026-09-14: §11 as written on 2026-09-10 is superseded, and one of its
> conclusions is RETRACTED.** The retraction is kept in full at §11.2 rather
> than deleted, because the way it went wrong is the most useful thing in here.

### 11.1 What silicon says

MEASURED 2026-09-09, on the fielded shell:

| Observation | Value |
|---|---|
| `CHIP_ID` | **0x0811** (the part, at the expected 7-bit address 0x41) |
| Init writes | **13 of 13 ACKed** |
| `TSC_CTRL` read back after init | **EN = 1** |
| Polls | **321k**, with **zero** I2C errors |
| Polls under a **confirmed human press** | **79k** |
| `TSC_CTRL.TSC_STA` (contact detected) in those | **never asserted** |
| `max_z` (largest pressure ever read) | **0** |

MEASURED 2026-09-14, rc4.1 over JTAG: the panel-continuity probe completed and
reported driven-high samples **X+ 1188, X− 1202, Y− 1068, Y+ 9** and
`touch_verdict` = **2 PANEL_OPEN**.

MEASURED 2026-09-14, bench (the project lead): **Arm's default MPS3 image detects touches
on this same panel.** That single fact kills the entire hardware theory. The
panel, its flex, the STMPE811 and the I2C wiring are all good; the fault was on
our side of the bus.

### 11.2 RETRACTED — the probe was reading the wrong pins

The PANEL_OPEN verdict and the "Y+ line is open" reading of it are **withdrawn**.
The probe was converting the wrong ADC channels, so all four numbers in §11.1
describe pins that are not connected to the panel.

QUOTED, STMPE811 datasheet, ST **Doc ID 14489 Rev 5** (April 2011):

* Table 2 "Pin assignments" (p.7) — the touch lines are **GPIO-4 = X+,
  GPIO-5 = Y+, GPIO-6 = X−, GPIO-7 = Y−**.
* Table 13 "ADC controller register summary table" (p.30) — the ADC channels are
  **CH0 = X+/GPIO-4, CH1 = X−/GPIO-6, CH2 = Y+/GPIO-5, CH3 = Y−/GPIO-7**, and
  **CH4..CH7 = IN0..IN3 / GPIO-0..GPIO-3**.

The four touch lines are **ADC channels 0..3**. The probe converted channels
**4..7**, which are IN0..IN3 — ordinary inputs with nothing on them. Worse, the
`GPIO_AF` value in force at the time (0x0F) had additionally muxed exactly those
four pins into GPIO mode, so they were not ADC inputs at all. Note also that the
two orderings are *different permutations* (CH1 is X− while GPIO-5 is Y+), so
the error relabels the result rather than obviously breaking it.

Two further mistakes in the same probe, both now fixed:

* **`ADC_CAPT` is not a busy flag.** QUOTED (Rev 5 p.32): *"Write '1' to initiate
  data acquisition for the corresponding channel. Writing '0' has no effect.
  Reads '1' if conversion is completed. Reads '0' if conversion is in
  progress."* Its reset value is **0xFF**. The old code waited for the bit to
  *clear*; it never can, which is exactly what the board showed on 2026-09-14.
  Completion now comes from `ADC_INT_STA` (0x0F), whose channel bit is cleared
  immediately before each capture so a stale one cannot be mistaken for this
  conversion finishing.
* **The follow test assumed an ADC reference the datasheet never gives.** The
  rule was "driven high ≥ ¾ of full scale, driven low ≤ ¼", which presumes full
  scale is the pin's supply rail. The datasheet states no internal reference
  voltage. The rule is now a **separation** between the two drives, which needs
  no reference at all.

**The verdict can no longer say PANEL_OPEN.** A line that *does* follow its
partner both ways is positive evidence of a conductive path, so `3 panel-present`
stands. A line that *does not* follow has explanations the datasheet cannot rule
out — it describes the internal "driver and switch control unit" in one
paragraph (Rev 5 §10.1) and never says what the analogue switches on
X+/X−/Y+/Y− do during an ADC capture — so that case is now `0 unknown` with the
raw samples and the `FOLLOW_BAD` status bit still exported. The encoding is
frozen at diag v8, so value **2 is retained in the header and never emitted**.

### 11.3 The reference — Arm's own working driver

Arm ships a proven STMPE811 driver for this exact board.

QUOTED source: `Keil.V2M-MPS3_IOTKit_BSP.1.0.2.pack` (a ZIP, from
`https://www.keil.com/pack/`), file
`Boards/ARM/V2M-MPS3/Common/Touch_V2M-MPS3.c`, © 2013–2017 ARM LIMITED,
BSD-3-Clause. It is byte-for-byte identical to `Touch_V2M-MPS2.c` in the MPS2
CMx BSP. Corroborating sources: ST's component driver
`github.com/STMicroelectronics/stm32-stmpe811` (`stmpe811.c`), and Linux
`drivers/input/touchscreen/stmpe-ts.c` + `drivers/mfd/stmpe.c` with the values
in `arch/arm/boot/dts/st/stm32f429-disco.dts`.

Register-by-register, ours before 2026-09-14 against Arm's:

| Reg | Name | **Arm (proven)** | ours (old) | ours (now) | why it matters |
|---|---|---|---|---|---|
| 0x03 | `SYS_CTRL1` | `0x02`, **10 ms**, no release | `0x02`, crude spin, `0x00` | `0x02`, settle, `0x00` | the release is ST's practice and harmless; the wait was an unquantified spin |
| 0x04 | `SYS_CTRL2` | `0x0C` | `0x00` | `0x00` | 1 = clock OFF, reset 0x0F. Ours is a **superset**: `TSC_OFF`/`ADC_OFF` are 0 in both; we also keep the GPIO clock, which the probe needs |
| 0x09 | `INT_CTRL` | never written | never written | never written | `GLOBAL_INT` gates the INT **pin** only |
| **0x0A** | **`INT_EN`** | **`0x07`** | **never written** | **`0x07`** | **the only functional register we never wrote.** See below |
| 0x0B | `INT_STA` | `0xFF` at init; **`0x1F` every poll** | `0xFF` at init only | `0xFF` at init; **`0x1F` every poll** | the bits are sticky and re-assert themselves |
| 0x17 | `GPIO_AF` | `0x00` | `0x0F` | `0x00` | 0 = touchscreen/ADC, 1 = GPIO — **inverted from the name** |
| 0x20 | `ADC_CTRL1` | `0x69` | `0x48` | `0x69` | 12-bit both ways; Arm samples for 124 clocks, not 80 |
| 0x21 | `ADC_CTRL2` | `0x01` | `0x01` | `0x01` | 3.25 MHz — unanimous |
| 0x40 | `TSC_CTRL` | `0x01` | `0x01` | `0x01` | XYZ acquisition + EN. ST's `0x73` is X,Y-only and would leave Z undefined |
| 0x41 | `TSC_CFG` | `0xC2` | `0x9A` | `0xC2` | Arm: 8-sample average, 10 µs detect delay, 500 µs settling |
| 0x4A | `FIFO_TH` | `0x01` | `0x01` | `0x01` | *"must not be set as zero"* |
| 0x4B | `FIFO_STA` | `0x01`→`0x00` at init | same | same, **plus a flush on backlog** | |
| 0x4C | `FIFO_SIZE` | **read every detect** | never read | **read every detect** | `TSC_STA` says the comparator fired, not that a sample exists |
| 0x56 | `TSC_FRACTION_Z` | `0x07` | `0x07` | `0x07` | Arm and Linux agree; ST writes `0x01`. **Coupled to `TOUCH_Z_MIN`** |
| 0x58 | `TSC_I_DRIVE` | `0x01` | `0x01` | `0x01` | 50 mA — unanimous |
| 0x59 | `TSC_SHIELD` | never written | never written | `0x00` before the probe | its bits deliberately ground X+/X−/Y+/Y− |

**`INT_EN` is the leading root-cause candidate.** It is the one *functional*
register Arm writes and we never did, and the datasheet ties it to whether a
touch can wake the part at all:

* QUOTED, `SYS_CTRL1[0] HIBERNATE` (Rev 5 p.23): *"If the hot-key feature is
  required, use the **default auto-hibernation mode**."* — auto-hibernation is
  the default behaviour, not something you opt into.
* QUOTED, programming sequence step (q) (Rev 5 p.48): *"During the
  auto-hibernate mode, a touch detection can cause a wake-up to the device only
  when the TSC is enabled **and the touch detect status interrupt mask is
  enabled**."*

With `INT_EN` at its reset `0x00` the touch-detect mask is disabled. That is a
documented path to precisely the symptom in §11.1: a healthy, correctly
configured, ACKing part whose `TSC_STA` never asserts under a real press. It is
REASONED, not proven — proving it needs a finger on the glass (§11.6).

Note that polling needs no interrupt routing regardless — QUOTED (Rev 5 p.27):
*"Regardless of whether the INT_EN bits are enabled, the INT_STA bits are still
updated."* `INT_EN` is written for the hibernate interaction, not for an ISR.

### 11.4 A second candidate, still open

`GPIO_AF` was **never written at all** before 2026-09-10. The press test in
§11.1 is from 2026-09-09, so **the sequence that added the `GPIO_AF` write has
never been tested with a finger on the glass.** It may already have fixed this.
The datasheet states that register's reset value twice and inconsistently
(`0x00` in Table 11, `0x0F` in the §13 detail page, in both Rev 2 and Rev 5),
which is why it must always be written and never assumed.

### 11.5 Reading the answer — before anyone opens the case

Diag mailbox **v8**, words **+0x6C..0x78** (`firmware/common/diag.h`; over JTAG
`mrd <base+0x6C> 4`, the base found by scanning for the magic with
`scripts/mps3_diag.tcl`). Over the wire: `pyverify diag`, keys `touch_regs`,
`touch_adc_x`, `touch_adc_y`, `touch_verdict`, plus the decoded
`touch_verdict_word` / `touch_*_hex`. The packing is in `firmware/touch/touch.h`,
which is the single statement of the rule.

| `touch_verdict` | meaning | what to do |
|---|---|---|
| **0** unknown | the probe did not run, could not complete, **or** completed with a line that did not follow its partner | read `touch_adc_y[31:24]`: `0x10` `FOLLOW_BAD` means it ran and measured — look at the raw samples. `0x08` `ADC_ERR` means a conversion never finished. `0x02`/`0x04` are read-back failures. 0 means no probe ran at all (no `TOUCH=1`, or no part) |
| **1** chip-misconfigured | a read-back differs, **in a bit the datasheet defines**, from what init wrote | compare `touch_regs` against `GPIO_AF 0x00`, `SYS_CTRL2 0x00`, `TSC_CFG 0xC2`, `ADC_CTRL1 0x69` and `touch_adc_x[31:24]` against `TSC_I_DRIVE 0x01`. Expect `ADC_CTRL1` to read back `0x6D`: bit 2 is reserved and resets to 1, and the comparison masks it |
| **2** panel-open | **never emitted.** Retained only because the wire encoding is frozen at v8 | if you ever see it, the image predates 2026-09-14 |
| **3** panel-present | all four lines followed both drives | the panel is wired. If touch still never fires, the fault is above the pins |

### 11.6 The board plan — what to read next

**(a) WITHOUT a finger.** This proves the new init landed and costs nothing but a
`pyverify diag`. Everything here is deterministic; any mismatch is a bug in the
image, not in the panel.

| Read | Expect | If not |
|---|---|---|
| `touch_regs` | **`0x69C20000`** — `[7:0]` GPIO_AF `0x00`, `[15:8]` SYS_CTRL2 `0x00`, `[23:16]` TSC_CFG `0xC2`, `[31:24]` ADC_CTRL1 `0x69` (or `0x6D`, reserved bit 2) | the image is not the new one, or a write is being swallowed → `touch_verdict` will say `1` |
| `touch_adc_x[31:24]` | `0x01` (TSC_I_DRIVE) | as above |
| `touch_verdict` | `3` present, or `0` with `FOLLOW_BAD` | `1` = the part is ignoring a write; investigate the bus before anything else |
| `touch_adc_x[11:0]` / `[23:12]` | X+ and X− driven-high samples, **and they must now be plausible**: the corrected probe reads CH0/CH1, not IN0/IN1 | |
| `touch_adc_y[11:0]` / `[23:12]` | Y+ and Y− driven-high samples | |
| `touch_adc_y[31:24]` | `0x01` `RAN` alone is the clean result | |
| JTAG statics (`mb-nm -S` + `xsdb mrd`) `s_dbg_init_wr_fail` | **0** | a non-zero count names how many init writes NACKed |
| `s_dbg_ctrl_after` | `0x01` (EN set, TSC_STA clear with no finger) | `0x81` with no finger would mean `TSC_STA` is not what we think |

The **first** thing to record is whether `touch_regs` shows `INT_EN`-era values
at all — if `touch_regs` is still `0x489A000F` the board is running the old
image and nothing below applies.

**(b) WITH a finger** (the project lead, next office day). Hold a press and read, in this
order. The point is to separate *four* states that all previously looked like
"no touch".

1. **`TSC_CTRL` (0x40) bit 7 `TSC_STA`** — via the JTAG static `s_dbg_last_ctrl`,
   or `s_dbg_sta_seen` over a press.
   * **asserts** → the `INT_EN`/auto-hibernate theory (§11.3) is CONFIRMED and
     the bug is fixed. Go to step 2.
   * **still never asserts** → `INT_EN` was not it. Next suspects, in order:
     `ADC_CTRL1 0x69` and `TSC_CFG 0xC2` are already Arm's, so the remaining
     divergences are `SYS_CTRL2 0x00` vs Arm's `0x0C` and our extra
     `SYS_CTRL1 = 0x00` release write. Try Arm's `0x0C` (this disables the GPIO
     clock and therefore the probe; run it as a one-off experiment build).
2. **`FIFO_SIZE` (0x4C)** — JTAG static `s_dbg_last_fifo`, and the counter
   `s_dbg_fifo_empty`.
   * `s_dbg_sta_seen` climbing **with** `s_dbg_fifo_empty` climbing and
     `s_dbg_last_fifo` == 0 → the detect comparator fires but the acquisition
     never produces a sample. That points at `TSC_CFG` settling/averaging or at
     `TSC_I_DRIVE`, not at the detect path.
   * `s_dbg_last_fifo` > 0 → samples are being produced. Go to step 3.
3. **`s_dbg_max_z`** — the largest Z ever read.
   * 0 with samples arriving → the packed-sample decode or `OP_MOD` is wrong.
   * non-zero but below `TOUCH_Z_MIN` (64) → the pressure floor is rejecting
     real presses; `TSC_FRACTION_Z` and `TOUCH_Z_MIN` are coupled (`0x07` means
     Z is a 1.7 fixed-point ratio, so 64 means "ratio ≥ 0.5") and one of the two
     needs retuning against this panel.
   * comfortably above 64 → touch works; what remains is calibration.
4. **`INT_STA` (0x0B) bit 0 `TOUCH_DET`** — only if step 1 says `TSC_STA` never
   asserts. It is the *latched* form of the same event (write 1 to clear), so if
   `TOUCH_DET` latches while `TSC_STA` stays 0, the detect circuit is working and
   `TSC_STA` is being misread. This requires a small instrumented build; it is
   the tie-breaker, not the first thing to try.
5. **Calibration**, once a press dispatches: `touch_set_calibration()` with a
   3-point recal. The defaults are the identity assumption and nothing more.

### 11.7 What this still does not prove

* The corrected probe has **never been run on hardware**. Everything in §11.2–
  §11.5 is proved only against the host fake in `firmware/test/test_touch.c`
  (227 checks, with a control for every verdict and 13 mutations that turn it
  red — including reverting the channel map, the `ADC_CAPT` polarity, the
  `FIFO_SIZE` guard and the `PANEL_OPEN` verdict).
* `INT_EN` as the root cause is **REASONED from two datasheet sentences plus
  Arm's practice**, not measured. Step (b)1 is the experiment that settles it.
* The `TOUCH_PROBE_FOLLOW_MIN` separation (¼ of full scale) is reasoned, not
  calibrated against this panel.
* A verdict of **3** does not mean touch works. It means the four lines are
  electrically continuous.
* The soft-reset settling wait is still an **iteration count, not a duration**.
  The tree has a real microsecond timebase (`mps3_sys_now_us()` /
  `mps3_spin_until()`); wiring `touch.c` to it needs `service.c` on
  `test_touch`'s link line and is recorded as a follow-up, not done.
