# MPS3 CLCD live-status display — implementation plan

> **Status: HISTORICAL PLAN — LANDED AND FIELDED.** Written 2026-07-10 as "BUILT + UNIT-PROVEN, awaiting the Phase-D static batch"; that batch has since landed, the panel renders live status on the board, and the touch controller it defers is now in the fabric too. Current state: [docs/STATUS.md](STATUS.md).
> Drive the MPS3's on-board colour LCD from the harness MicroBlaze to show live
> platform status **without a laptop**.
>
> The RTL, its cocotb bench, the firmware driver/renderer and the HX8347 init
> table are written and green: `make check` OK; `make -C tests BLOCK=clcd run-one`
> 7/7; `READP=1` 3/3; `csr_decode_width BLOCK=clcd` 4/4; `firmware/test` 32/32.
> The CLCD wave changed nothing static — it touched no netlist. (The shell's
> `static_id` was then-current **`0x14E1A2D8`** (see docs/FIELDED_SHELL.md for what is on the board now), re-minted by the separate 1 MiB-LMB
> rebuild `53f0241`, *not* by CLCD; all 7 overlays are re-keyed to it. Earlier
> revisions of this doc quoted the prior `0xAAF21306`.)
> The integration is specified, deliberately **unapplied**, in
> `CLCD_PHASE_D_INTEGRATION.md` (internal note, not in the public tree).
>
> **This plan was written before any of it was built, and four of its claims were
> wrong.** They are corrected inline below and summarised in §0.1. Where this
> document and `CLCD_PHASE_D_INTEGRATION.md` disagree, the latter wins.
>
> **Ground truth (all first-hand, cited inline):** the board TRM
> `mps3_fpga_prototyping_board_trm_100765_0000_04_en.pdf` §2.11 (page 2-38) +
> §2.13 (page 2-40); `fpga/monolithic/nanosoc_mps3.xdc:491-530`;
> `fpga/monolithic/nanosoc_mps3_top.sv:445-455`; `fpga/shell/shell_top.sv`;
> `fpga/shell/constraints/mps3_harness.xdc`; the firmware status sources under
> `firmware/`; `docs/contracts/shell-regmap.md`; the shipped shell utilization
> report `fpga/shell/build_results_2026-07-10/post_impl_utilization.rpt`.
>
> **Read this first — the one expensive fact.** Every source below that names it
> agrees: any change to the *static* netlist re-mints the CRC-32 `static_id` and
> invalidates **all** keyed overlays plus the firmware ELF override
> (`docs/contracts/overlay-manifest.md:66-68`; `NEXT_PHASE_CAPABILITY.md` (internal note, not in the public tree)
> §6). Bringing the CLCD out adds a top-level port + XDC pins + a BD slave — a
> static change. **It cannot be shipped alone; it MUST batch with the one
> planned static rebuild (Phase D).** See §11. Everything that is *not* static —
> the RTL block, its bench, the firmware driver, the host-side render logic — can
> be written, unit-benched, and host-tested **today**, and only *lands* in the
> batch.

---

## 0. Bottom line (blunt)

- **The interface is settled, not guessed.** TRM §2.11 states it in words: the
  MPS3 CLCD is a **QVGA 320×240** panel driven by an **8-bit parallel bus**
  (8080-style), controller **Himax HX8347-D** (module MCBQVGA-TS), with a
  **separate 4-wire resistive touch controller on I²C**. The suspicious
  `CLCD_WR_SCL` pin name is the HX8347-D's dual-mode WR/SCL pin — **on this board
  it is the 8080 write strobe**, because the panel is strapped for parallel (TRM
  §2.11: "An 8-bit parallel bus between the FPGA and the display panel"). See §2.
- **What is verified vs what is sourced.** The *bus* is verified (8080, 8-bit,
  HX8347-D). The HX8347-D's *panel register sequence* (init, GRAM windowing,
  pixel format) is **not** in this repo and was **not** invented: it is
  transcribed from `STMicroelectronics/stm32-hx8347d` (BSD-3-Clause), corroborated
  against a second, independently-licensed driver, with semantics and timing
  taken from the Himax HX8347-D(T) datasheet v02. Provenance per entry:
  `firmware/clcd/PANEL_PROVENANCE.md`. The values remain **unproven until the
  panel lights** — those drivers target other modules. The CLCD **block** stays
  protocol-agnostic, so a corrected table is a data-only change.
- **Recommended architecture:** a small AXI4-Lite **8080 write-FIFO bus master**
  in `fpga/shell/ip/clcd/` (no framebuffer, no fabric font ROM), plus a
  **firmware software glyph renderer** with an 8×16 font table in the LMB. The
  panel's own GRAM *is* the framebuffer. Cost is ~1 BRAM and a few hundred
  LUT/FF (ESTIMATE, §10) versus **~34–38 RAMB36** for a fabric framebuffer on top
  of the **129.5** BRAM tiles the shell already uses
  (`post_impl_utilization.rpt`). Character-cell is the right shape; a framebuffer
  is unnecessary because the controller holds the pixels.
- **Touch is OUT of v1.** Output-only status display. "Tap to reset the DUT"
  duplicates the existing `reset` control verb (`coordinator.c:225`) and the
  physical PBON button, and would pull in an I²C touch driver + calibration +
  interrupt the poll-model firmware doesn't service. The touch pins stay
  reserved for a scoped v2.
- **Board name** has no source today; recommendation is **host-pushed + cached**
  with a MAC/IP-derived fallback (§9) — *not* the DIP switches, which are already
  owned by the DUT through `board_gpio` (`shell_top.sv:269-271`).

---

## 0.1 Corrections applied (2026-07-10, after building against this plan)

Five things this document asserted did not survive contact with the tree. Each
is fixed in place below; they are collected here so nobody re-derives them.

| # | What the plan said | What is true | Where it now lives |
|---|---|---|---|
| C1 | §12 lands the `main.c` `clcd_poll()` hook in the pre-batch wave | **There is no AXI slave at `0x44AC` on the shipped shell.** `shell_bd.tcl:1014` has `NUM_MI = 14` and `assign_bd_address` stops at MMCM_DRP. MicroBlaze bus exceptions are off, so the access neither traps nor works — `STATUS` reads garbage. An ungated hook would stream init bytes into a DECERR'ing void every superloop pass, on a board whose only ingress is that firmware. | Every call site is behind `#ifdef MPS3_HAS_CLCD`, **default OFF**, defined only on the `test_clcd` rule (`firmware/test/Makefile`). The Phase-D batch flips it. |
| C2 | §7: wait for "the ~120 ms **sleep-out** settle" | The **HX8347-D has no sleep-out command** (`0x11`) at all. The 120 ms figure is `tREST`, the controller's reset-complete/blank window. The real constraint is a **5 ms** floor after releasing RST, and `tRESW` ≥ 10 µs for the pulse itself. | Settles are `HX_DLY` entries **inside the table**; `clcd.c` owns exactly one panel timing (`CLCD_RESET_PULSE_MS`) and no sleep-out state. |
| C3 | §3: bring out "the 18 CLCD package pins" | **14.** The other four (`CLCD_TSCL`/`TSDA`/`TINT`/`TNC`) are the touch controller, which is out of v1 — the block has no ports for them. Constraining them would constrain nonexistent ports. | `CLCD_PHASE_D_INTEGRATION.md` §3 ports 14; the touch four stay reserved and unconstrained. |
| C4 | §12's W-CLCD-INTEG row: add a CLCD slave to the BD | Vivado 2024.1's `create_bd_cell -type module` **categorically refuses a SystemVerilog top file** — confirmed live on all six existing blocks (`shell_bd.tcl:428-443`). `clcd.sv` must be packaged as IP-XACT via `fpga/shell/ip_packaged/package_csr_ip.tcl` first, and joined to the CSR clock/reset `foreach` (`shell_bd.tcl:484`). | `CLCD_PHASE_D_INTEGRATION.md` §4.0/§4.3. |
| C5 | §12's ownership table is "file-scoped, no overlap" | It gave `platform_regs.h` **two owners** (W-CLCD-FW and W-CLCD-DOC), and omitted five files any real wave must touch: `tests/common/list_benches.py`, `tests/Makefile`, `tests/conftest.py`, root `Makefile` (`LINT_SV`), `firmware/test/Makefile`. Those are exactly where a wave produces a **silently skipped** gate. | §12's table below, rewritten. |

One design decision was also overridden during the build: an early `clcd.sv`
armed the panel read cycle on an **AXI read of `READ`**. Rejected — this
platform dumps CSR pages over SWD/XVC and `tests/csr_decode_width/` reads every
offset in the page, so a read side effect would silently drive the panel bus.
(The shell has shipped one such bug already: a read of DFXCTL `0x20` popped a
UART console byte.) A read-back is now armed by **writing** `CTRL.read_start`
(bit 4). `tests/clcd/test_clcd_readpath.py::test_read_sweep_has_no_panel_side_effect`
guards it, and that guard was **mutation-tested**: reintroducing the read-arms
logic makes it fail while its two siblings still pass.

---

## 1. Established facts — verified

Each bullet was re-checked against the file this session.

1. **CLCD is pinned only in the legacy monolithic XDC, and hard tied off in the
   monolithic top.** Confirmed: `fpga/monolithic/nanosoc_mps3.xdc:491-530` pins
   the whole group (all `IOSTANDARD LVCMOS18`); `nanosoc_mps3_top.sv:445-455`
   ties it inert — `CLCD_BL=1'b0` (backlight off), `CLCD_RST=1'b0` (held in
   reset), `CLCD_PD=8'bz`, strobes `1'b0`, `CLCD_TSCL=1'b1` (I²C idle-high).
2. **CLCD is entirely absent from the harness shell.** Confirmed by grep:
   **zero** `clcd`/`lcd` matches in `fpga/shell/shell_top.sv` or
   `fpga/shell/constraints/mps3_harness.xdc`. No port, no constraint, no RTL, no
   firmware anywhere under `firmware/`.
3. **Signals + package pins** (verbatim from `nanosoc_mps3.xdc:495-530`, all
   `LVCMOS18`): data `CLCD_PD[17:10]` (8 lines, AN17/AP16/AP18/AR18/AM16/AN16/
   AR17/AR16), `CLCD_RD`=AM15, `CLCD_RS`=AN14, `CLCD_CS`=AP15,
   `CLCD_WR_SCL`=AP14, `CLCD_BL`=AJ16, `CLCD_RST`=AK18; touch `CLCD_TSCL`=AL18,
   `CLCD_TSDA`=AJ15, `CLCD_TINT`=AJ14, `CLCD_TNC`=AL17.
4. **`static_id` re-mint / batching.** Confirmed the rule
   (`overlay-manifest.md:66-68`) and that the current shipped generated value is
   `0x14E1A2D8` (`fpga/dfx/overlay/mps3_shell_static_id.c`, the 1 MiB shell). The pending
   static batch is enumerated in `NEXT_PHASE_CAPABILITY.md` §6 (Phase D:
   D1 Option-C egress, D4 `WDOG_RREQ` watchdog, D5 decoupler clamp validation,
   D6 the CSR decode fixes, D7 re-key) plus the shield-header GPIO
   (`docs/CONNECTOR_SURVEY.md` §5-6) and the `HD.PARTPIN_LOCS` freeze. **The CLCD
   port joins this list.** Said loudly here and again in §11.
5. **Firmware shape.** Bare-metal MicroBlaze, single superloop, **no interrupts
   used** (`firmware/platform/src/main.c:26-37` "Poll model: no INTC/ISRs at all
   in v1"), every consumer a bounded non-blocking `_poll()`
   (`main.c:202-251`). Registers behind `firmware/common/platform_regs.h`; base
   list ends at **`MPS3_GPIO_BASE 0x44AA0000`** in that header, with
   **`MMCM_DRP 0x44AB`** added in `shell-regmap.md` v0.3. There is a free-running
   `axi_timer` (`main.c:104-130`).

---

## 2. The electrical interface — the decisive research (verified from the TRM)

> **This is the question the tasking flagged as make-or-break** ("Is it 8080
> parallel or serial? `CLCD_WR_SCL` is suspicious"). It is answered, not guessed.

`mps3_fpga_prototyping_board_trm_100765_0000_04_en.pdf` **§2.11 "QVGA video CLCD
display" (page 2-38)**, extracted verbatim via `pypdf`:

> "The MPS3 board provides QVGA, 320 × 240, CLCD video display. The CLCD video
> display system provides an on-board CLCD display panel that includes:
> **An 8-bit parallel bus between the FPGA and the display panel.**
> A 4-wire resistive touch screen.
> A Touch Screen Controller (TSC) that connects to the FPGA over an I²C bus …
> The interface supports a screen update rate of **20fps**."

Figure 2-16 labels the panel **"QVGA CLCD display panel with TSC Module
MCBQVGA-TS Controller HX8347-D"** and splits the signals into:

| TRM signal (fig 2-16) | Platform XDC name (`nanosoc_mps3.xdc`) | Role |
|---|---|---|
| `LCD_DAT[17:10]` | `CLCD_PD[17:10]` | 8-bit parallel data bus |
| `LCD_RD` | `CLCD_RD` (AM15) | 8080 read strobe |
| `LCD_WR` | `CLCD_WR_SCL` (AP14) | **8080 write strobe** |
| `LCD_RS` | `CLCD_RS` (AN14) | register/data select (D/C) |
| `LCD_CS` | `CLCD_CS` (AP15) | chip select |
| `LCD_TNC` | `CLCD_TNC` (AL17) | touch no-connect |
| `LCD_BLC` (→ GPIO) | `CLCD_BL` (AJ16) | backlight control |
| (panel reset) | `CLCD_RST` (AK18) | HX8347-D reset |
| I²C2/I²C3 SCL/SDA (dual I²C) | `CLCD_TSCL`/`CLCD_TSDA` (AL18/AJ15) | touch controller I²C |
| `LCD_TSINT` (→ GPIO/IRQ) | `CLCD_TINT` (AJ14) | touch interrupt |

**Resolving `CLCD_WR_SCL`.** The TRM calls this pin `LCD_WR`. The `_SCL` in the
platform XDC name is an artefact of the HX8347-D's *own* dual-mode pin (it
supports 8080/6800 parallel **and** 3/4-wire SPI, selected by the module's IM
straps; the WR pin doubles as SCL in serial mode). **On the MPS3 the panel is
strapped for 8-bit parallel** — TRM §2.11 is unambiguous — so this pin is driven
as the 8080 write strobe. The plan builds an 8080 parallel master. (The board
strap itself is a hardware fact; keep it in the open-questions list to eyeball at
bring-up, §13-Q1.)

**Controller / resolution / colour depth.**
- Controller: **Himax HX8347-D** (TRM fig 2-16). Well-known QVGA TFT driver with
  **on-chip GRAM** — i.e. the panel stores the framebuffer, the FPGA does not.
- Resolution: **320 × 240 (QVGA)** (TRM §2.11).
- Colour depth: the HX8347-D supports 16bpp (RGB565, 65K) and 18bpp (262K). Over
  an 8-bit bus you write **2 bytes/pixel** for RGB565, which is the natural fit;
  **16bpp RGB565 is the recommended v1 format** (halves the write traffic vs
  18bpp and is the standard MPS2/MPS3 CLCD mode). Exact strapped depth is a
  driver decision to confirm on the panel (§13-Q3).

**What is verified vs what must be ported (do NOT invent a register protocol).**
- **Verified from the TRM:** bus type (8080), width (8-bit), controller part,
  resolution, that touch is a separate I²C TSC, refresh ceiling (20fps).
- **NOT in this repo — now SOURCED, still board-unproven:** the HX8347-D **panel
  register sequence**. No HX8347 or CLCD driver existed anywhere on this machine
  (`grep -rli hx8347` over the workspace and the Arm IP library hit only this file), so
  it could not be "ported from the repo". It was transcribed from
  `STMicroelectronics/stm32-hx8347d` @ `9758706` (**BSD-3-Clause**, vendoring
  permitted, notice retained), corroborated byte-for-byte on the gamma blocks by
  `nopnop2002/esp-idf-parallel-tft` @ `1dee984` (MIT), with semantics/timing from
  the **Himax HX8347-D(T) datasheet, Version 02, March 2009**. Six values where
  the two drivers disagree are flagged `[MODULE]` in the table. Landed as
  `firmware/clcd/hx8347_init.c`; evidence in `firmware/clcd/PANEL_PROVENANCE.md`.
  Every value is **unproven until it renders on the board** — both drivers target
  other modules. The CLCD *block* is a protocol-agnostic byte-level 8080 master
  and invents no panel register values.

---

## 3. Pin-collision + bank/Vcco check

**No collisions.** Each of the 18 CLCD package pins (AN17, AP16, AP18, AR18,
AM16, AN16, AR17, AR16, AM15, AN14, AP15, AP14, AJ16, AK18, AL18, AJ15, AJ14,
AL17) appears **exactly once** in `fpga/monolithic/nanosoc_mps3.xdc` (grepped
this session — one `PACKAGE_PIN` each, all under the CLCD block). They are unused
elsewhere and unused by the shell (§1.2). Bringing them into the shell adds pins
that touch **nothing** already constrained.

> **CORRECTION (C3): v1 brings out 14 of those 18, not all 18.** The last four —
> `CLCD_TSCL` (AL18), `CLCD_TSDA` (AJ15), `CLCD_TINT` (AJ14), `CLCD_TNC` (AL17),
> `nanosoc_mps3.xdc:523-530` — are the **touch** controller, which is out of v1
> (§5). The `clcd` block has no ports for them, so constraining them would
> constrain nonexistent ports. They stay reserved and unconstrained.

**Bank / Vcco.** All 18 carry `IOSTANDARD LVCMOS18`, so they must sit in a
**1.8 V Vcco bank**. This is **self-consistent and already proven**: the same
legacy pinmap constrains this group `LVCMOS18` alongside other confirmed-working
LVCMOS18 signals in the same package region (e.g. `SH_ADC_*` at AM25/AL25/AP25/
AP26, `nanosoc_mps3.xdc:482-489`) and that monolithic build routed clean on
`xcku115-flvb1760-1-c` (the same part the shell targets —
`post_impl_utilization.rpt:8`). A bank holds one Vcco, so if the CLCD pins were
in a bank driven at another voltage the legacy build would have failed
`IOSTANDARD` DRC; it did not. **The exact bank *number* still wants a one-line
Vivado query** (`get_property BANK [get_package_pins AP14]` etc.) at integration
time — low risk, listed as §13-Q4. The CLCD is a **static-shell** peripheral: its
pads are static I/O like the LEDs (`CONNECTOR_SURVEY.md` §4), so — unlike the RP
pblock — there is no HDPR-6 IOB-exclusion concern.

---

## 4. Where each status field lives today (exact source)

| Field | Live source (symbol / register / file:line) | Notes |
|---|---|---|
| **`static_id`** | `mps3_shell_static_id()` → `fpga/dfx/overlay/mps3_shell_static_id.c` (= `0x14E1A2D8` today, the 1 MiB shell); cached in `g_shell_state.static_id` (`coordinator.h:24`) | The overlay key. A mismatch is the #1 swap-failure cause (`overlay-manifest.md:66-68`). |
| **`rm_id`** (which DUT) | hardware readback `DFXCTL.RM_ID` @ `0x44A1_0010` (`shell-regmap.md` DFXCTL); firmware cache `g_shell_state.current_rm_id` (`coordinator.h:28`, 0 = greybox) | Ground truth of what actually loaded. |
| **`rm_id` → human name** | `fpga/dfx/rm_list.tcl` (`rm_id`↔`rm_name` pairs) + `overlay/<rm>/manifest.json` `rm_name` (`overlay-manifest.md:47-60`) | greybox `0x0`, nanosoc `0x1`, eth_ss `0x2`, led `0x1E`, regdemo_a `0xA1`, regdemo_b `0xB2`, uart_echo `0x4543484F`. Firmware carries no name table today — see §9. |
| **uptime** | `mps3_sys_now_ms()` (`main.c:115`, decl `common/timebase.h:17`), free-running `axi_timer` (PG079, `main.c:104-130`), 100 MHz, wraps ~49.7 days | Format ddd:hh:mm:ss; must be wrap-safe (subtract, don't compare `<` — `timebase.h:13`). |
| **network state** | `smsc911x_link_up()` (`smsc911x.h:279`, 1=up); speed/duplex via `smsc911x_mii_read()` (BMSR 0x01 / ANLPAR 0x05 — `smsc911x.h:277`); IP static `192.168.10.101` (`net_proto.h:32-35`); MAC `02:00:00:4D:50:53` default (`net_if_lwip.c:732`, `mps3_platform_mac()`) | LAN9220 is poll-mode; link comes up from PHY autoneg (`net_if_lwip.c:772`). |
| **swap state** | `swap_fsm_last_result()` → `{valid, ok, verified, rm_id}` (`swap_fsm.h:198-205`); ICAP bytes `swap_fsm_icap_bytes()` (`swap_fsm.h:212`) | **There is NO swap *count* today.** A running counter is a trivial firmware-only add (§9); do it in the CLCD firmware, not the static. |
| **DUT reset/clock** | `CLKRST.RESET_CTRL` @ `0x44A0_0000` ([0] dut_resetn/[1] rp_resetn/[2] dbg_resetn); `CLKRST.STATUS` @ `0x0C` ([0] mmcm_locked, [1] dut_clk_alive) — `platform_regs.h` CLKRST block | 1 = released. |
| **diag counters** | `g_mps3_diag` mailbox, magic `0xD1A6C0DE` (`diag.h:32`); fields `rx_drop_frames`/`tx_errors`/`icap_bytes`/`icap_eos_status`… (`diag.h:77-131`) | Refreshed every superloop pass (`main.c:224-248`). **Read via the `g_mps3_diag` symbol, never a hardcoded address** — the LMB is now **1 MiB** (`shell_bd.tcl` `Write_Depth_A {262144}` ×32b, commit `71fe613`), so the mailbox is at `0x000FFF80`, not the `0x0007FF80` the plan and `diag.h:42`'s comment quote; and the decode ALIASES, so a stale hardcoded address reads the wrong window silently. `clcd.c` takes the symbol. |
| **board name** | **none — no source exists** | Recommendation in §9. |

---

## 5. Scope + non-goals

**In v1 (output-only status display):**
- An AXI4-Lite CLCD 8080 bus-master block in the static shell.
- A cooperative firmware driver + software text renderer.
- A fixed status screen (§8) updated live from the §4 sources.
- Backlight + panel-reset control from firmware.
- A cocotb bench with a panel bus model (§10).

**Explicit non-goals (v1):**
- **Touch input — OUT.** The 4-wire resistive TSC is a *separate I²C* device
  (`CLCD_TSCL`/`CLCD_TSDA`) plus an interrupt (`CLCD_TINT`, TRM §2.13 page 2-40
  "CLCD_TINT = CLCD touchscreen"). Enabling it needs an I²C master + touch
  calibration + servicing an IRQ the **poll-model firmware deliberately does not
  wire** (`main.c:26-37`). Its headline use — "tap to reset the DUT" — already
  exists two other ways: the `reset` control verb (`coordinator.c:225`,
  `{"op":"reset","target":"dut"}`) and the board's PBON button. Touch is a
  clean, self-contained **v2** (the pins stay reserved; no static churn to defer
  it). *(The tasking pointed at a "Q5" touch discussion — no `Q5` string exists
  in `docs/` or `firmware/` this session; treated on its merits above.)*
- **Graphics / logos / a framebuffer.** Text status only. The panel's GRAM holds
  the pixels; the fabric holds none (§6).
- **No new interrupt.** The renderer is a bounded `clcd_poll()` (§7), consistent
  with the whole superloop.
- **No touchscreen or display in the RP/DUT.** CLCD is a shell peripheral; it
  never crosses the partition boundary and needs **no DFX decoupler entry**
  (unlike the Ethernet RMII, `DFX_DECOUPLER_BOUNDARY.md`).

---

## 6. Architecture

### 6.1 The block: an AXI4-Lite 8080 write-FIFO bus master

`fpga/shell/ip/clcd/clcd.sv` — a real synthesizable AXI4-Lite slave in the house
style of `fpga/shell/ip/telem/telem.sv` (`C_S_AXI_ADDR_WIDTH=12` local decode,
base set in the BD Address Editor; both blessed AXI4-Lite handshake styles are
legal, `shell-regmap.md` "AXI4-Lite slave conventions"). Its job is **only** to
turn AXI-Lite register writes into correctly-timed 8080 bus cycles on the CLCD
pins. It has:

- A small **command/data byte FIFO** (writes to `CMD`/`DATA` push a byte + its
  RS value; a bus-cycle FSM pops and drives `CS`/`RS`/`WR`/data with the
  HX8347-D's setup/hold timing, parameterised in `s_axi_aclk` cycles).
- A **STATUS** register (fifo level/full/empty/busy) so firmware can pace without
  blocking (§7).
- Backlight (`CLCD_BL`) and panel-reset (`CLCD_RST`) as plain register bits.
- Optional **read path** (`CLCD_RD` cycle → `READ` register) so firmware can read
  the HX8347-D ID for panel-present detection (gated on §13-Q5 — some MPS3 CLCD
  buffers are write-only; if so, drop the read path and detect presence by a
  boot self-test pattern instead).

**No framebuffer, no fabric font ROM.** The controller's on-chip GRAM is the
framebuffer; the font lives in the LMB as firmware data (§6.3). The block is a
few hundred LUT/FF + at most one BRAM for the FIFO (§10).

### 6.2 Framebuffer vs character-cell vs FIFO-master — the resource call

Three ways to get glyphs onto the panel, with the BRAM cost each, against the
shipped shell's **129.5 BRAM tiles used (6.00% of 2160)**,
**5712 LUT (0.86%)**, **6736 FF (0.51%)** on `xcku115-flvb1760-1-c`
(`post_impl_utilization.rpt`, build_results_2026-07-10). All BRAM numbers are
**ESTIMATE**.

| Option | What lives in fabric | BRAM (ESTIMATE) | Verdict |
|---|---|---|---|
| **(A) Full framebuffer** | 320×240×16bpp = **153,600 B** mirror of the panel | 1,228,800 bits / ~32 Kib usable per RAMB36 ≈ **34–38 RAMB36** (≈ +26–29% on top of 129.5 tiles) | ❌ Reject. Pointless — the HX8347-D **already stores the pixels**. Pure waste. |
| **(B) Fabric character-cell renderer + font ROM** | 40×15 text buffer (~600 B) + attributes + 8×16 font ROM (96 glyphs × 16 B ≈ 1.5 KB) + a scan-out FSM | **1–2 RAMB36** + ~200 LUT | Viable, autonomous refresh, but moves glyph→pixel expansion into RTL for no need — the panel is slow (20fps) and the fields are formatted in firmware anyway. |
| **(C) 8080 write-FIFO master + firmware render** ★ | just the byte FIFO | **0–1 RAMB** (FIFO fits distributed RAM or 1 RAMB18) + ~300–600 LUT / ~450–700 FF | ✅ **Recommended.** Leanest fabric; keeps field formatting in firmware where it already is; the panel's GRAM is the framebuffer; dirty-cell tracking maps to "which fields changed". |

**Recommendation: (C).** A 320×240×16bpp framebuffer is ~34–38 RAMB36 — in
*absolute* terms only ~1.7% of the KU115's 2160 tiles, so it is *affordable*, but
it is **unnecessary**: the controller holds the framebuffer, so a fabric copy
buys nothing. (B) is a reasonable fallback if firmware CPU time for glyph
expansion ever proves tight, but for a slowly-changing text screen the software
renderer in (C) is far below the superloop's budget. Build (C).

### 6.3 Firmware render side (software glyph expansion)

- **Shadow text buffer** `char shadow[15][40]` in RAM + a **dirty bitmap**
  (600 bits = 75 B) marking changed cells.
- **8×16 font table** in the LMB (`firmware/clcd/font8x16.h`, ASCII 0x20–0x7E ≈
  95 glyphs × 16 B ≈ 1.5 KB — negligible against the 512 KiB LMB / ~208 KB ELF).
- Field formatters write ASCII into `shadow[][]`; a diff marks dirty cells; the
  renderer expands dirty cells to RGB565 pixel bytes and pushes them to the CLCD
  block's FIFO (§7). Glyph→pixel expansion is a table lookup + bit test — cheap.

---

## 7. Firmware design — the cooperative `clcd_poll()`

**Hard constraint (non-negotiable):** `clcd_poll()` runs in the same superloop as
`mps3_net_lwip_tmr()` and `swap_fsm_poll()` (`main.c:202-251`) and **must never
spin**. It does a *bounded* amount of work per call and returns — exactly why
`swap_fsm` chunks its ICAP writes (`swap_fsm.h:181-184` "Does a bounded amount of
work … never blocks"). A blocking panel delay or a busy-wait on FIFO space would
stall lwIP's TCP/ARP timers and drop the network. This is the whole design.

`firmware/clcd/clcd.c` — a small state machine, one bounded step per `clcd_poll()`:

```
CLCD_RESET      -> assert CLCD_RST (block reg bit=0), record t0
CLCD_RST_WAIT   -> return until mps3_sys_now_ms()-t0 >= RESET_MS  (non-blocking wait)
CLCD_INIT       -> push <= N init bytes/pass from the SOURCED HX8347 init table
                   into the CMD/DATA FIFO while STATUS.fifo_full==0; advance an
                   index; an HX_DLY entry arms a timed wait (INIT_WAIT)
CLCD_INIT_WAIT  -> return until the table's current HX_DLY deadline elapses
CLCD_READY/IDLE -> every REFRESH_MS: reformat fields, diff into shadow[][],
                   mark dirty cells
CLCD_RENDER     -> pop dirty cells, expand each glyph to RGB565, push pixel bytes
                   while STATUS.fifo_full==0 and a per-pass byte budget remains;
                   stop at the budget or a full FIFO; resume next pass
```

Rules:
- **All waits are timed poll-and-return** using `mps3_sys_now_ms()` — the reset
  pulse and each `HX_DLY` table settle are *deadlines checked each pass*, never
  `for`-loop delays. (The one blocking step the codebase tolerates is the boot
  default-overlay load, and only because no TCP client exists yet —
  `main.c:186-189`. The CLCD comes up *after* the network, so it gets no such
  license.)
  > **CORRECTION (C2): there is no sleep-out settle.** The HX8347-D has **no
  > `0x11` sleep-out command**. The "~120 ms" is `tREST` (reset-complete/blank),
  > not a sleep-out. The real floor is **5 ms after releasing RST** (datasheet
  > note 5), carried as the table's leading `{HX_DLY,5}`; the reset pulse is
  > `tRESW` ≥ 10 µs. `clcd.c` owns one panel timing (`CLCD_RESET_PULSE_MS`) and
  > no sleep-out state; every other settle is an `HX_DLY` table entry.
- **FIFO backpressure, not blocking.** Each pass pushes bytes only while
  `STATUS.fifo_full==0` and a byte budget (`CLCD_BYTES_PER_PASS`, tuned so the
  worst-case pass is well under lwIP's timer slack) is unspent; then it returns.
- **Refresh cadence.** Reformat fields every `REFRESH_MS` (≈ 250 ms; the panel
  ceiling is 20fps = 50 ms, so 250 ms is comfortable). Steady state, only
  uptime-seconds and the heartbeat cell change → a handful of dirty cells/tick.
  A full 600-cell redraw happens only on an RM swap or the error banner
  flipping, and is spread across many passes by the byte budget.
- **Dirty-region tracking** is per-cell (75-byte bitmap). A field whose text is
  unchanged marks no cells and costs nothing.
- **Init table is data, sourced.** The HX8347 init/GRAM sequence is a `const`
  `{op,val}` table in firmware (`firmware/clcd/hx8347_init.c`), transcribed from a
  cited driver (§2), where `op` is `HX_CMD`/`HX_DAT`/`HX_DLY`. The FSM is
  protocol-agnostic — it just streams the table — so the RTL and FSM need **no**
  change when the table is corrected against the datasheet. The driver handles
  `hx8347_init_len == 0` gracefully (falls straight to IDLE).
- **Gating during a swap.** Panel writes are fine during a swap (CLCD is static,
  the swap only touches the RP). But firmware SHOULD show the swap state on the
  banner (§8) so the screen is *most* useful exactly when the network channel is
  parked (`diag.h:11-13` "6900 is PARKED during a swap").

---

## 8. The screen — concrete layout (320×240, 8×16 font → 40 cols × 15 rows)

Ranked by "what it lets someone standing at the board diagnose **without a
laptop**." A 320×240 panel has ~15 text rows; the ranking decides what survives
if rows get tight.

```
+----------------------------------------+  row
| MPS3-01            nanoSoC harness      |  0   board name (§9) + platform
|----------------------------------------|  1   rule
| DUT : nanosoc        rm_id 0x00000001  |  2   [R2] what DUT is loaded
| SWAP: LOADED  VERIFIED   #007  last OK |  3   [R4/R8] load result + count
| SID : 0xXXXXXXXX                       |  4   [R3] static_id (overlay key)
| NET : 192.168.10.101  UP 100/FD        |  5   [R5] IP + link/speed/duplex
| UP  : 001:23:45:07                     |  6   [R6] uptime ddd:hh:mm:ss
| DUT : RST-REL  CLK-ALIVE  MMCM-LOCK    |  7   [R7] reset + dut_clk_alive + lock
| ICAP: 1,835,072 B   rxdrop 0  txerr 0  |  8   [R9] ICAP bytes + diag counters
|                                        |  9
| ############ ERROR BANNER ############ | 10   [R1] hidden unless a fault is live
| # STATIC_ID MISMATCH - overlay stale # | 11        (spans rows 10-12, inverted)
| ######################################## | 12
|----------------------------------------|  13  rule
| MAC 02:00:00:4D:50:53      hb .        | 14   MAC + 1 Hz heartbeat glyph
+----------------------------------------+
```

**Field ranking + why each earns its pixels:**

1. **[R1] Error banner** — *the* headline. A prominent inverted bar, hidden when
   healthy, shown for: `static_id` mismatch (stale overlay), swap FAILED,
   `!dut_clk_alive`/`!mmcm_locked`, network down, or the boot fatal-park
   (`main.c:171-184`). This is the single most valuable "walk up and know what's
   wrong" element. Rank 1.
2. **[R2] Bitstream name + `rm_id`** — "what am I even looking at?" The first
   question at any board. `nanosoc / 0x00000001` (§4).
3. **[R3] `static_id`** hex — because overlays are keyed to it and a mismatch is
   the **#1 swap-failure cause** (`overlay-manifest.md:66-68`). Seeing it on the
   glass makes the most common failure diagnosable instantly.
4. **[R4] Swap status** — LOADED / VERIFIED / FAILED from
   `swap_fsm_last_result()` (`swap_fsm.h:198-205`). Did the DUT that was
   requested actually land and pass the `DFXCTL.RM_ID` verify?
5. **[R5] IP + link state** — the platform is entirely network-driven; "can I
   reach it?" is second only to "what's loaded." Link/speed/duplex from
   `smsc911x_link_up()`/`_mii_read()` (§4).
6. **[R6] Uptime** — has it rebooted since I last looked? From
   `mps3_sys_now_ms()`.
7. **[R7] DUT reset + `dut_clk_alive` + `mmcm_locked`** — is the DUT actually
   *running*, or held in reset / clock-dead? `CLKRST.STATUS` (§4).
8. **[R8] Swap count** — churn indicator (needs a new firmware counter, §4/§9).
9. **[R9] ICAP bytes + diag counters** — `swap_fsm_icap_bytes()` +
   `g_mps3_diag.rx_drop_frames`/`tx_errors` (§4). Progress/health of the last
   config stream and the network path.
10. **Board name** (rank last for *size*, but printed top-left for prominence) —
    identity is a small string; §9.

If rows get tight, drop from the bottom of the rank (R9 diag detail first, then
R8 count), never the banner or the identity lines.

---

## 9. Board name — the decision

There is **no source for a board name today** (§4). Evaluated options:

| Option | One ELF, N boards? | Survives reboot? | Static change? | Verdict |
|---|---|---|---|---|
| **Firmware compile-time constant** | ❌ needs a distinct ELF per board | ✅ | no | Rejected — breaks the fleet goal (`NEXT_PHASE_CAPABILITY.md` E1: "parameterize board identity **out of** the hardcoded IP"). Re-bakes the ELF per unit. |
| **Host-pushed over 6900 + cached** ★ | ✅ | ⚠️ blank until first host contact | no | **Recommended.** |
| **DIP-switch strapping (`USER_SW`)** | ✅ | ✅ | **yes + conflict** | Rejected — `USER_SW[7:0]` is already consumed as the DUT's GPIO input (`shell_top.sv:269-271`: `board_gpio_pad_i = {USER_SW, led_drive}`). Reading it for board identity fights the DUT's own switch reads (`CONNECTOR_SURVEY.md` §3). Carving bits out is a static change to the `board_gpio` mux **and** a semantic collision. |
| **New CSR (build-info register)** | ✅ | ✅ | **yes** | Clean but a static change for a cosmetic field — not worth its own re-mint; if ever done, fold into the D-batch, not standalone. |

**Recommendation: host-pushed + cached, with a deterministic fallback.** Reuse
the existing identity plumbing: `ping` already reports `shell_id`
(`coordinator.h:37-45`), and E1 already wants a lease-side board registry. Add a
tiny `set_name`-style field (or fold the name into the lease's registry push)
over TCP 6900; cache it in a fixed RAM slot (same pattern as the diag mailbox,
`diag.h`). **Never blank:** until the host provisions it, derive a fallback from
the low MAC octets / IP last octet (e.g. `MPS3-101` from `.101`,
`net_proto.h:35`) so the screen always shows *an* identity. Zero static churn,
one ELF across the fleet, consistent with the productization direction.

---

## 10. Cost + risk

### 10.1 Resource estimate — **ESTIMATE** (no synthesis was run)

Against the shipped shell (`post_impl_utilization.rpt`, build_results_2026-07-10,
`xcku115-flvb1760-1-c`): **5712 LUT (0.86%)**, **6736 FF (0.51%)**,
**129.5 BRAM tiles (6.00% of 2160)**.

| Element | LUT | FF | BRAM | Basis |
|---|---|---|---|---|
| CLCD 8080 master + FIFO + AXI-Lite (option C) | ~300–600 | ~450–700 | **0–1** RAMB18 | Comparable to `telem`/`board_gpio` CSR blocks + a byte FIFO + a small bus FSM |
| (alt) fabric char-cell renderer (option B) | +~200 | +~150 | +1–2 RAMB36 | font ROM + text buffer + scan FSM |
| (rejected) full framebuffer (option A) | — | — | **+34–38 RAMB36** | 320×240×16bpp = 1.2288 Mib / ~32 Kib per tile |
| Firmware (font + renderer + driver) | — | — | — | ~2–4 KB code+data in the 512 KiB LMB / ~208 KB ELF; well within budget |

Bottom line: option C is **negligible** fabric — ~0.1% LUT, ≤1 BRAM tile —
versus a framebuffer that would push BRAM from 129.5 to ~166 tiles for no gain.

### 10.2 The `static_id` re-mint — the real cost

Adding `CLCD_*` to `shell_top.sv`, its pins to `mps3_harness.xdc`, and a CLCD
AXI-Lite slave to the BD is a **static** change. It **re-mints `static_id`**
(currently `0x14E1A2D8`) and **invalidates every stored overlay + the firmware
ELF override** (`overlay-manifest.md:66-68`). Consequence: after the CLCD lands,
**all 7 overlays and `mps3_shell_static_id.c` must be regenerated and re-proven**
(the D7 re-key, `NEXT_PHASE_CAPABILITY.md` §6).

**Therefore this MUST batch with the one planned static rebuild (Phase D)** —
alongside D1 Option-C egress, D4 `WDOG_RREQ`, D5 decoupler validation, D6 CSR
decode fixes, the shield-header GPIO (`CONNECTOR_SURVEY.md` §5-6), and the
`HD.PARTPIN_LOCS` freeze. **Do not ship the CLCD as its own rebuild.** The RTL /
bench / firmware are written and unit-proven *before* the batch and only *land*
in it. (Note: CLCD adds **no partition pins**, so it does not touch
`HD.PARTPIN_LOCS`; it is purely static-pad + static-slave — the cheapest kind of
D-batch rider.)

### 10.3 Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Panel isn't 8080 parallel as assumed** (strap wrong / `CLCD_WR_SCL` is SPI here) | **Low** | High | TRM §2.11 explicitly says "8-bit parallel"; confirm the module strap at bring-up (§13-Q1). The block is 8080; a serial fallback would be a different block. |
| **Ported HX8347 init sequence wrong** | Medium | Medium | Port from a known-good driver, not from memory; bench checks the *bus protocol* + a golden init vector (§11); iterate on the board. The FSM is protocol-agnostic, so fixes are firmware-data-only. |
| **`clcd_poll()` stalls the superloop** | Low | High | Bounded byte budget + timed poll-and-return waits (§7); a host-side unit test asserts the per-call byte bound; mirror `swap_fsm` discipline. |
| **CLCD pins in a non-1.8 V bank** | Very low | Medium | LVCMOS18 proven in the legacy build on the same part (§3); confirm bank number by Vivado query at integration (§13-Q4). |
| **Read-back path unsupported by the board buffers** | Medium | Low | Make the `READ` path optional; if unsupported, detect panel presence by a boot self-test pattern instead of an ID read (§13-Q5). |
| **Re-mint churn** if shipped alone | — (process) | High | Batch with Phase D (§10.2). Non-negotiable. |

---

## 11. Verification plan

Follow the repo's bench conventions (`tests/README.md`, the `tests/telem`
pattern): a per-block cocotb directory, a `Makefile` including
`tests/common/bench_common.mk`, the reusable AXI4-Lite protocol checker bound on
the slave (`bind_telem.sv` → `bind_clcd.sv`), `AxiLiteMaster` from
`tests/common/regmap.py` as the bus driver, and a `dut_notes.md` port list.

**New directory: `tests/clcd/`**
- `Makefile` — `TOPLEVEL=clcd`, `VERILOG_SOURCES=fpga/shell/ip/clcd/clcd.sv`,
  `-sverilog` on VCS, `SVA_BIND_FILES := bind_clcd.sv`, include `bench_common.mk`.
- `test_clcd.py` — cocotb, with a **panel bus model** (Python monitor on the
  `CS/RS/WR/RD/PD[17:10]` pins) that:
  1. **Init sequence** — decodes the command/data byte stream the FSM emits when
     firmware pushes the (golden) HX8347 init table; asserts the ordered
     `{RS, byte}` sequence matches a checked-in golden vector.
     *(Golden = the ported table; the bench proves the block streams it
     faithfully, not that the panel values are correct — that's a board fact.)*
  2. **Bus protocol** — CS asserts around a cycle, data is stable at the `WR`
     rising edge, `RS=0` for command / `RS=1` for data, timing meets the
     parameterised setup/hold.
  3. **Known glyph** — drive the renderer (or a direct FIFO poke) to write `'A'`
     at cell (0,0); the model decodes the GRAM-window set + pixel byte stream and
     checks it equals the 8×16 font ROM's `'A'` bitmap in RGB565.
  4. **FIFO backpressure** — stall the model's WR-accept; assert `STATUS.fifo_full`
     rises, firmware/driver stops pushing, and **zero bytes are lost** when it
     resumes.
  5. **Decode-width coverage** — width-32 decode with no offset aliasing, in the
     style of `tests/csr_decode_width` (the lesson from the CSR-decode
     regression: bench at the width the BD instantiates).
- `bind_clcd.sv` — bind the AXI4-Lite protocol checker.
- `dut_notes.md` — port list + the panel-model contract.

**Firmware host-tests** (`firmware/test/`, `MPS3_HAL_MOCK`): link `clcd.c`
against `mock_regs.c`; unit-test the renderer's shadow-buffer diff + dirty-cell
math + the **per-`clcd_poll()` byte-bound** (proves it can't spin); assert the
uptime/`static_id`/`rm_id` formatters against seeded mock registers.

**Provable in sim (no board):** bus timing, init-vector faithfulness, glyph
rendering to RGB565, FIFO backpressure/no-loss, register decode, the
non-stall byte bound.
**Needs the board:** that the HX8347-D actually accepts the sequence and lights
pixels; the strap is parallel (§13-Q1); real reset/settle timing; backlight
polarity (unsourced — see §13-Q7); colour fidelity; `[MODULE]` values (MADCTL
`0x16`, PANEL `0x36`); the LVCMOS18 bank; read-back support.

---

## 12. Work breakdown — parallel-agent wave, file-scoped, no overlap

Ordered so the non-static work (RTL/bench/firmware) completes and is proven
*before* the static batch.

> **CORRECTION (C5): the original table below double-owned `platform_regs.h`
> (W-CLCD-FW *and* W-CLCD-DOC) and omitted five files any real wave must edit —
> `tests/common/list_benches.py`, `tests/Makefile`, `tests/conftest.py`, the root
> `Makefile` (`LINT_SV`), and `firmware/test/Makefile`. Those registration files
> are exactly where a wave silently skips a gate. The table is rewritten below to
> match what was actually built. Genuinely shared files (`platform_regs.h`,
> `shell-regmap.md`, `main.c`, `tests/conftest.py`, `tests/Makefile`, root
> `Makefile`) have exactly ONE owner — the integrator — who froze the contract
> before fan-out and merged the shared edits after.**

**As-built ownership** (the integrator froze `platform_regs.h`,
`shell-regmap.md` and the `clcd/README.md` port contract *before* fan-out, and
owned every shared registration file):

| Task | Owns (exclusive) | Integrator-owned edits it depends on | Acceptance (falsifiable) — **all met** |
|---|---|---|---|
| **(integrator)** | contract freeze | `docs/contracts/shell-regmap.md` (CLCD v0.4, RESERVED), `firmware/common/platform_regs.h` (base+regs), `fpga/shell/ip/clcd/README.md` (frozen port contract + init-table seam), `tests/conftest.py` (collect-ignore), `NEXT_PHASE_CAPABILITY.md` (D6b row) | CLCD added no static change (the `static_id` re-mint to `0x14E1A2D8` ×7 came from the 1 MiB-LMB rebuild, not CLCD); `make check` OK. |
| **W-CLCD-RTL** | `fpga/shell/ip/clcd/clcd.sv`; root `Makefile` `LINT_SV` line | port contract | verilator `-Wall` clean; `make lint` OK; decode is the 64 KiB-page idiom (no width-32 aliasing); READ has no side effect. |
| **W-CLCD-BENCH** | `tests/clcd/` (`Makefile`, `test_clcd.py`, `test_clcd_readpath.py`, `clcd_panel_model.py`, `bind_clcd.sv`, `dut_notes.md`), `tests/csr_decode_width/test_decode_width_clcd.py`; **registers** in `tests/common/list_benches.py`, `tests/Makefile`, `tests/csr_decode_width/Makefile` | port contract | `list_benches` shows `clcd` **READY** not SKIP; VCS 7/7 + READP 3/3 + decode 4/4; the no-read-side-effect guard is **mutation-proven** to fail on the read-arms design. |
| **W-CLCD-FW** | `firmware/clcd/clcd.c`, `clcd.h`, `font8x16.h`; `firmware/test/test_clcd.c`; **registers** in `firmware/test/Makefile`; the gated hook in `firmware/platform/src/main.c` | `platform_regs.h` | `firmware/test` 32/32; per-`clcd_poll()` byte bound proven (50k-pass hammer); hook behind `#ifdef MPS3_HAS_CLCD`, default OFF; diag read via symbol. |
| **W-CLCD-PANEL** | `firmware/clcd/hx8347_init.c`, `hx8347_init.h`, `PANEL_PROVENANCE.md` | init-table seam | Every entry cited (outcome A, BSD-3-Clause); Q3/Q6/Q7 answered from source; `[MODULE]` divergences flagged. |
| **W-CLCD-DOC** *(Phase-D rider spec — doc only, UNAPPLIED)* | `CLCD_PHASE_D_INTEGRATION.md`; `NEXT_PHASE_CAPABILITY.md` D6b row | — | The `shell_top`/`xdc`/`bd` diffs written and **not applied**; static netlist untouched. |
| **W-CLCD-INTEG** *(Phase-D batch — NOT YET DONE)* | `fpga/shell/shell_top.sv` (14-pad port + IOBUFs), `fpga/shell/constraints/mps3_harness.xdc` (14 pins), `fpga/shell/bd/shell_bd.tcl` (package `clcd` as IP-XACT first — C4; `NUM_MI` 14→15; `assign_bd_address` @ `0x44AC`; CSR clock/reset foreach), flip `MPS3_HAS_CLCD` | all of the above; **the Phase-D batch owner** | Shell builds; `0` new DRC; CLCD @ `0x44AC_0000`; re-key regenerates all 7 overlays + `mps3_shell_static_id.c` clean (D7). See `CLCD_PHASE_D_INTEGRATION.md`. |
| **W-CLCD-BOARD** *(board only, post-batch — NOT YET DONE)* | — | W-CLCD-INTEG on silicon | Panel lights; layout §8 renders; uptime ticks; error banner flips on an **induced** `static_id` mismatch. |

**Register map for the new block** — the normative version is now the CLCD
section of `docs/contracts/shell-regmap.md` (v0.4, RESERVED @ `0x44AC_0000`) and
the defines in `firmware/common/platform_regs.h`. As built (note `CTRL[4]
read_start`, added when the read-arms design was rejected):

```
## CLCD (0x44AC_0000) — QVGA HX8347-D 8080 bus master (fpga/shell/ip/clcd)
| Off  | Reg     | Bits | Notes |
|------|---------|------|-------|
| 0x00 | CTRL    | [0] enable, [1] backlight (CLCD_BL), [2] reset_n (CLCD_RST, 0=held in reset), [3] fifo_reset, [4] read_start | read_start self-clears, READ_PATH=1 only |
| 0x04 | CMD     | [7:0] byte  | write pushes a COMMAND byte (RS=0); DROPPED (not stalled) if fifo_full |
| 0x08 | DATA    | [7:0] byte  | write pushes a DATA byte (RS=1); DROPPED (not stalled) if fifo_full |
| 0x0C | STATUS  | [0] fifo_full, [1] fifo_empty, [2] busy, [15:8] fifo_level | read-only; poll for backpressure |
| 0x10 | READ    | [7:0] rdata, [8] valid | read-only, **no side effect**; holds last CLCD_RD capture. Armed by CTRL.read_start; reads 0 if READ_PATH=0 |
| 0x14 | TIMING  | [7:0] wr_lo, [15:8] wr_hi, [23:16] cs_setup | 8080 strobe timing in s_axi_aclk cycles |
```

> The HX8347-D **panel** register values streamed *through* `CMD`/`DATA` are a
> **sourced firmware table** (`firmware/clcd/hx8347_init.c`, cited in
> `PANEL_PROVENANCE.md`), deliberately not enumerated here (§2). A read of `READ`
> has no side effect — a CSR page dump over SWD/XVC must never drive the panel.

---

## 13. Open questions — only the TRM/schematic/board can answer

1. **Q1 — Is the module strapped for 8080 parallel (not SPI)?** TRM §2.11 says
   parallel; the HX8347-D IM straps are a hardware fact. Eyeball at bring-up. If
   wrong, the block is a serial master instead (different `clcd.sv`).
2. **Q2 — Exact HX8347-D init + GRAM sequence.** Port from datasheet / a
   known-good HX8347 driver; not in this repo. Golden-vector it in the bench;
   prove on the board.
3. **Q3 — Colour depth actually strapped** (16bpp RGB565 vs 18bpp). Drives the
   bytes-per-pixel in the renderer. Recommend RGB565; confirm on the panel.
4. **Q4 — I/O bank number + Vcco of the CLCD pin group.** LVCMOS18 assumed from
   the legacy pinmap (proven-consistent, §3); confirm with
   `get_property BANK [get_package_pins AP14]` at integration.
5. **Q5 — Is the `CLCD_RD` read path usable?** Some MPS3 CLCD buffers are
   write-only. If read-back works, use it for panel-present ID detection; else
   drop the `READ` register and detect by a boot self-test pattern.
6. **Q6 — Data-bus bit order.** `CLCD_PD[17:10]` = 8 lines; confirm which is the
   MSB. Byte order is **sourced: high-byte-first for RGB565** (datasheet Fig 5.22
   + both drivers write `(color>>8)` then `(color&0xFF)`); the renderer packs
   that way. Confirm the `CLCD_PD[17:10]→DB[7:0]` mapping on the board.
7. **Q7 — `CLCD_BL` / `CLCD_RST` polarity + reset timing.** Sourced:
   `RST`/`NRESET` is **active-low**, `tRESW` ≥ 10 µs, and commands wait **5 ms**
   after RST release (datasheet note 5). There is **no sleep-out** (the ~120 ms
   is `tREST`, not a `0x11`). **`CLCD_BL` polarity is in no source** — inferred
   active-high from the legacy tie-off (`BL=0`=off); this is the one genuinely
   open item here, confirm on the board.
8. **Q8 — Touch (v2).** TSC part number, I²C address, and 4-wire calibration —
   deferred with the touch scope.

---

## 14. Summary

- The interface is **verified from the TRM**, not assumed: **8080 8-bit parallel,
  HX8347-D, 320×240 QVGA**, separate I²C touch. `CLCD_WR_SCL` is the 8080 write
  strobe (the HX8347-D's dual WR/SCL pin, strapped parallel here).
- Build an **AXI4-Lite 8080 write-FIFO master** in `fpga/shell/ip/clcd/` (~1 BRAM,
  ~hundreds of LUT/FF — ESTIMATE) plus a **firmware software renderer** with an
  8×16 LMB font. **No framebuffer** — the controller's GRAM holds the pixels;
  a fabric framebuffer would be ~34–38 RAMB36 for nothing.
- `clcd_poll()` is a **bounded, non-blocking** superloop step (timed poll-and-return
  waits, FIFO-backpressure byte budget) — the `swap_fsm` chunking discipline,
  because a stall would drop lwIP.
- The screen leads with an **error banner** (top diagnostic value), then what-DUT
  / `static_id` / swap result / network / uptime / DUT-clock / diag — each earning
  its pixels by what it lets an observer diagnose without a laptop.
- **Board name** = host-pushed + cached with a MAC/IP fallback; **not** the
  DUT-owned DIP switches.
- **Everything non-static is now built and proven** (RTL/bench/firmware/table,
  all green — see the top-of-file status). The port/XDC/BD change is static and
  **re-mints `static_id`**, so it **must batch with Phase D** — the CLCD is the
  cheapest possible rider (no partition pins, no decoupler entry), but it cannot
  ship on its own rebuild. That change is specified and **unapplied** in
  `CLCD_PHASE_D_INTEGRATION.md`.
- Panel register values are **sourced and cited, not invented**
  (`PANEL_PROVENANCE.md`); the bench proves the bus and the block, the board
  proves the panel.
