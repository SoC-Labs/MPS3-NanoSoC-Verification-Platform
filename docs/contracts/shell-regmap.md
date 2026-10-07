# Contract: shell register map (MicroBlaze AXI4-Lite view)

**Version:** v0.9 (2026-09-24). Base addresses are the AXI-Lite decode the
MicroBlaze coordinator firmware (A3) targets and the shell RTL (A1) decodes.
Widen registers freely within a block; moving a block base is an A6 change.
v0.1 adds the blocks the Phase-0 spike found missing (OPEN_ISSUES I5–I11) and
the GPIO passthrough block (I4) with concrete bases + fields.

> **Doc fix 2026-09-29 (no register change; version unchanged):** CLKRST and
> TELEM now say what the built shell does. CLKRST: the DUT clock IS retuned on
> silicon, through MMCM_DRP (firmware `8a12a08`; `set_clk` 25/100/50 MHz proven
> 2026-09-24, `docs/evidence/2026-09-w3/ila_proofs_20260924.txt` B5), and
> `DUT_CLK_SEL`/`DUT_CLK_DRP` are inert scratch — I16 closed. TELEM: there is no
> power sensor; `STATUS[0]` is the DDR4 calibration flag on the MicroBlaze V
> shell and 0 on the classic one.

> **Changelog v0.9 (2026-09-23, mint 3 — ADDITIVE, DUTEGR inject side):**
> - **DUTEGR** gains an **inject side** (host → DUT) on its **existing** page
>   `0x44B2_0000`: seven registers at `0x20`–`0x38` (`TX_CTRL`, `TX_STATUS`,
>   `TX_SPACE`, `TX_DATA`, `TX_FRAMES`, `TX_REJECT`, `TX_FLUSHED`). `0x00`–`0x1C`
>   are **unchanged**; the RX `DATA` pop still decodes at `0x10` only, and
>   `0x3C`–`0xFFFC` stay unmapped (read 0, pop nothing). No new page, no new
>   interconnect port, no RP-boundary change; a static change, so it lands in a
>   mint (mint 3). Design: `docs/planning/HANDOVER_DUT_INJECT.md` with
>   **amendments A1/A2, decided by the project lead 2026-09-23** (A1: RAW = 0 frames are
>   capped at 1514 staged bytes = 1518 on the wire; A2: `TX_FLUSHED` @`0x38`,
>   flushed frames no longer counted in `TX_REJECT`). RTL/README:
>   `fpga/shell/ip/dut_egress/`. (The header went v0.6 → v0.7 with D13's USD
>   entry below; v0.7 = DUTEGR RX and v0.8 = USRACC/WDOG were recorded in their
>   own sections only. This line bumps the header past all of them.)
>
> **Changelog v0.7 (2026-09-23, D13 — USD replaces OVLSTORE on `0x44A4_0000`):**
> - **USD** (`usd_spi_0`, `fpga/shell/ip/usd_spi/`) takes the `0x44A4_0000` page
>   and interconnect master **M03** from **OVLSTORE** (`axi_quad_spi_0`, pad-less
>   since D16). Same base, same 64 KiB range, `NUM_MI` unchanged (21, or 22 with
>   `SHELL_TOUCH=1`). Offsets are now DERIVED from the RTL (section below); the
>   eleven vendor `OVLSTORE_SPI_*` offsets and `MPS3_OVLSTORE_BASE` are gone.
> - Seven new static-only pads (`USD_CLK/CMD/DAT[3:0]/NCD`); **no** RP partition
>   pin. A static change: it re-mints `static_id` and re-keys every overlay, so it
>   lands only in a mint. Shared by both `SHELL_CPU` variants; under `mbv` the
>   block is **kernel**-owned (`spi-usd` → `mmc_spi`).

> **Changelog v0.6 (2026-08-06, gated — TOUCH):**
> - **TOUCH** block @ **`0x44AE_0000`** (next free 64 KiB page after CLCDKVM
>   `0x44AD`): a Xilinx **AXI IIC** master (`axi_iic:2.1`) for the on-board
>   resistive touch-screen controller (I2C TSC on `CLCD_TSCL`/`CLCD_TSDA`), with
>   the pen-down IRQ (`CLCD_TINT`) routed to the INTC concat **In4** as a polled
>   pending bit (INTC ISR `0x4120_0000` bit 4). Interconnect `NUM_MI` 18 → **19**
>   (master **M18**). GATED behind env `SHELL_TOUCH=1`
>   (`fpga/shell/bd/touch_iic_add.tcl`, sourced from `shell_bd.tcl`;
>   `MPS3_SHELL_TOUCH` verilog-define un-gates `shell_top.sv`; pins in
>   `constraints/optional/mps3_harness_touch.xdc`, added only under the flag so
>   the DEFAULT build stays byte-identical). Firmware base + offsets are
>   `MPS3_TOUCH_BASE` / `TOUCH_IIC_*`, **generated** into `platform_regs.h` by
>   `tools/gen_regmap.py` (2026-09-11; they were hand-written in
>   `firmware/touch/touch.h` as `MPS3_TOUCH_IIC_BASE` + fourteen `IIC_*`
>   offsets, with the generator suppressing its own TOUCH stanza to avoid the
>   redefinition — a second spelling of a truth this map owns).
>   Adds **no** RP partition pin (pure shell peripheral); it is a **static**
>   change that re-mints `static_id` and re-keys all overlays, so it lands only
>   in a mint.
>
> **Changelog v0.5 (2026-07-14, W1 — ADDITIVE, RESERVATION):**
> - **CLCDKVM** block reserved @ **`0x44AD_0000`** (next free 64 KiB page after
>   CLCD `0x44AC`): the **CLCD KVM**, which arbitrates the single physical
>   HX8347-D 8080 bus between the harness's `clcd_0` and a **DUT-side** display
>   accelerator, switched by the **`USER_nPB[1]`** push-button in *hardware*.
>   Design: `docs/CLCD_KVM_PLAN.md`; wave plan: `CLCD_KVM_WAVE_PLAN.md` (internal note, not in the public tree);
>   **frozen port + CSR contract: `fpga/shell/ip/clcd_kvm/README.md`**.
>   **NOT INSTANTIATED.** Like the VPHY (`0x44A3`) / GENCHK (`0x44A6`) rows, and
>   like CLCD itself was at v0.4, this is an address-map **reservation**:
>   `shell_bd.tcl` has `NUM_MI = 15` and `assign_bd_address` stops at CLCD
>   (`0x44AC`), so **any access to `0x44AD_xxxx` today is unmapped**. The RTL,
>   its bench and the firmware are written and unit-proven against this contract
>   *now*; the BD slave + the `USER_nPB1` pad + `clcd.sv`'s two new pins are a
>   **static** change that re-mints `static_id` (`0xE4B1C44A` → new) and re-keys
>   all 8 overlays, so they land only in the **Wave-4 batch**. See the CLCDKVM
>   section's hazard note.
> - **CLCD**: no register change, but a **forward note** — once the KVM lands,
>   `CLCD.CTRL[1]` (backlight) and `CLCD.CTRL[2]` (reset_n) no longer reach the
>   pads *directly*; they reach them **through** the KVM, which by default
>   (`CLCDKVM.CTRL.bl_rst_src = 0`) passes them straight through. Existing
>   firmware therefore keeps working unchanged. `clcd.sv` also gains two
>   **append-only** output pins (`busy_o`, `fifo_empty_o`) for the KVM's
>   safe-switch gate. Neither is a register-map change.
> - **New contract**: `docs/contracts/dut-display-tunnel.md` — the DUT→shell
>   display encoding over the spare upper 8 bits of `dut_gpio_o`/`dut_gpio_oe`.
>   **Zero** partition-pin / decoupler / RM-wrapper change; `partition-pins.md`
>   is **unmodified** and `pin_check` stays green.
>
> **Changelog v0.4.1 (2026-07-14, W0-C — CORRECTION, no contract change):**
> - **CLCD is INSTANTIATED and the panel is LIT.** The v0.4 rows below described
>   `0x44AC` as a *reservation*; that stopped being true on **2026-07-11** in
>   commit **`b4afe3e`** and nobody updated this file. `shell_bd.tcl:494`
>   instantiates `clcd_0`; `:1041` sets `NUM_MI 15`; `:1108`
>   `assign_bd_address -offset 0x44AC0000 -range 64K`. `static_id` was re-minted
>   `0x14E1A2D8` → **`0xE4B1C44A`** and all 8 overlays re-keyed.
>   The panel is rendering the harness status screen on the bench
>   (board-observed 2026-07-14). **The old HAZARD note and the "RESERVED, not
>   instantiated" row are struck through below.** Register offsets and semantics
>   are **unchanged** — this is a status correction only, hence the point release.
> - The proven panel configuration (8080 parallel, RGB565, `BL` active-high,
>   `RST` active-low, bit order, `READ_PATH=0`, 8080 timings) is recorded in
>   **`docs/CLCD_PANEL_FACTS.md`**. Write drivers and benches against that.
>
> **Changelog v0.4 (2026-07-10, A6 — additive, RESERVATION ONLY) — SUPERSEDED by v0.4.1:**
> - ~~**CLCD** block reserved @ `0x44AC_0000` (next free 64 KiB page after
>   MMCM_DRP): the on-board QVGA HX8347-D status display's 8080 bus master,
>   `docs/CLCD_STATUS_DISPLAY_PLAN.md`. **Not instantiated.** Like the VPHY
>   (`0x44A3`) / GENCHK (`0x44A6`) rows before it, this is an address-map
>   *reservation* — `shell_bd.tcl` has `NUM_MI = 14` and no `0x44AC` slave, so
>   **any access to this page today is unmapped**. The RTL, bench and firmware
>   driver are written and unit-proven against this contract now; the port +
>   XDC + BD change is static, re-mints `static_id`, and lands only in the
>   Phase-D batch (`NEXT_PHASE_CAPABILITY.md` (internal note, not in the public tree) §6). Firmware must keep
>   `clcd_poll()` compiled out until then — see the CLCD section's hazard note.~~
>   **The Phase-D batch ran (`b4afe3e`); the block is live. See v0.4.1.**
>
> **Changelog v0.3 (2026-07-07, A6 — additive):**
> - **MMCM_DRP** block added @ `0x44AB_0000`: the built shell BD's Clocking
>   Wizard (DUT clock) exposes its MMCM DRP as its **own AXI4-Lite slave**
>   (Vivado 2024.1 clk_wiz only offers AXI-Lite DRP, not a raw DRP port), so
>   arbitrary DUT-clock reprogramming goes through this block, not through
>   `CLKRST.DUT_CLK_DRP`. See the new block + the CLKRST note. Source:
>   `fpga/shell/build_results_2026-07-07-256k/` BD address map (W-BD).
>
> **Changelog v0.2 (2026-07-06, A6 reconcile — additive):**
> - **UARTBR**: codified `SWO_CFG` @ 0x18 (divisor/enable/overflow/frame_err)
>   and the `FIFO_STATUS` bit positions ([0]..[4]) from the landed
>   `uart_bridge` RTL; noted the reserved gaps and the UART1 tie-off seam.
> - **AXI4-Lite handshake styles**: both existing slave styles are blessed
>   (Xilinx-template ready-pulse AND accept-on-valid); bus masters/BFMs must
>   handle both. See "AXI4-Lite slave conventions" below.
> - **CLKRST**: recorded the honest I16 state — the firmware preset *lookup*
>   is real, the preset table contents + DRP FSM remain placeholders; I16
>   stays open.

## AXI4-Lite slave conventions (v0.2)
Two response-handshake styles exist in the landed shell RTL, and **both are
contract-legal**:
1. **Xilinx-template FSM** (`dfx_ctl`, `dut_clkrst`, `board_gpio`,
   `uart_bridge`, `jtag_bb`, `telem`): `awready`/`arready` pulse one cycle,
   then `bvalid`/`rvalid` assert on a later cycle.
2. **Accept-on-valid** (`mdio_phy_model`'s VPHY port): `awready`+`bvalid`
   (resp. `arready`+`rvalid`) are registered high at the **same** edge.

Consequently any master/BFM must observe ready and capture the response
**concurrently** (a sequential ready-then-response wait hangs on style 2 —
found under VCS; `tests/common/regmap.py`'s `AxiLiteMaster` is the reference
implementation). Unifying on one style is deliberately NOT required.
Shared write conventions (all blocks): unmapped/RO writes are accepted at
the protocol level (`BRESP=OKAY`, no effect); unmapped reads return 0.

All blocks are AXI4-Lite slaves on the MicroBlaze data bus, one 64 KiB page
each (VPHY and GENCHK are 4 KiB — their AXI ports are a hardcoded `[11:0]` that
the block range cannot scale past; see `shell_bd.tcl` at those two lines).

This was a **suggested** map — "adjust to the final BD address editor". It is
now the OTHER way round: the BD's address editor *is* the map, and the table
below is rendered from `shell_bd.tcl`'s `assign_bd_address` lines rather than
maintained beside them. Moving a block is still an integrator (A6) change and
still re-keys `static_id`; what changed is that this page can no longer
disagree with the design about which blocks exist.

<!-- The table below is GENERATED from fpga/shell/bd/shell_bd.tcl's
     assign_bd_address lines by tools/gen_regmap.py. A block that is not in
     the BD cannot appear here, and one that is cannot be described as
     absent -- which is how this table came to say CLCDKVM was "RESERVED,
     not instantiated" for two mints after shell_bd.tcl instantiated it. -->
<!-- BEGIN GENERATED[regmap-map] — gen_regmap.py — DO NOT EDIT BY HAND -->
| Base | Block | Owner | BD cell | Notes |
|---|---|---|---|---|
| `0x44A0_0000` | CLKRST | `fpga/shell/ip/clkrst/dut_clkrst.sv` | `dut_clkrst_0` | instantiated by the shell BD |
| `0x44A1_0000` | DFXCTL | `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` | `dfx_ctl_0` | instantiated by the shell BD |
| `0x44A2_0000` | HWICAP | `Xilinx AXI HWICAP (PG134)` | `axi_hwicap_0` | instantiated by the shell BD; offsets quoted from the vendor PG, not derived |
| `0x44A3_0000` | VPHY | `fpga/ethernet/mdio_phy_model/mdio_phy_model.sv` | `eth_mac_test_subsystem_0` | instantiated by the shell BD |
| `0x44A4_0000` | USD | `fpga/shell/ip/usd_spi/usd_spi.sv` | `usd_spi_0` | instantiated by the shell BD |
| `0x44A5_0000` | TELEM | `fpga/shell/ip/telem/telem.sv` | `telem_0` | instantiated by the shell BD |
| `0x44A6_0000` | GENCHK | `fpga/ethernet/gen_checker/gen_checker.sv` | `eth_mac_test_subsystem_0` | instantiated by the shell BD |
| `0x44A7_0000` | JTAGBB | `fpga/shell/ip/jtag_bb/jtag_bb.sv` | `jtag_bb_0` | instantiated by the shell BD |
| `0x44A7_0000` | SWDBB | `fpga/shell/ip/swd_bb/swd_bb.sv` | `jtag_bb_0` | **legacy** — retired at the A6 SWD->JTAG cutover; shares the JTAGBB page |
| `0x44A8_0000` | DBGBR | `Xilinx Debug Bridge, AXI->BSCAN (PG245)` | `debug_bridge_0` | instantiated by the shell BD; offsets quoted from the vendor PG, not derived |
| `0x44A9_0000` | UARTBR | `fpga/shell/ip/uart_bridge/uart_bridge.sv` | `uart_bridge_0` | instantiated by the shell BD |
| `0x44AA_0000` | GPIO | `fpga/shell/ip/board_gpio/board_gpio.sv` | `board_gpio_0` | instantiated by the shell BD |
| `0x44AB_0000` | MMCM_DRP | `Xilinx Clocking Wizard AXI4-Lite DRP (PG065)` | `clk_wiz_dut` | instantiated by the shell BD; offsets quoted from the vendor PG, not derived |
| `0x44AC_0000` | CLCD | `fpga/shell/ip/clcd/clcd.sv` | `clcd_0` | instantiated by the shell BD |
| `0x44AD_0000` | CLCDKVM | `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` | `clcd_kvm_0` | instantiated by the shell BD |
| `0x44AE_0000` | TOUCH | `Xilinx AXI IIC (PG090)` | `touch_iic_0` | **gated** — instantiated only when built with `SHELL_TOUCH=1`; offsets quoted from the vendor PG, not derived |
| `0x44B2_0000` | DUTEGR | `fpga/shell/ip/dut_egress/dut_egress.sv` | `dut_egress_0` | instantiated by the shell BD |
| `0x44B3_0000` | USRACC | `fpga/shell/ip/usr_access_rd/usr_access_rd.sv` | `usr_access_rd_0` | instantiated by the shell BD |
| `0x44B4_0000` | WDOG | `Xilinx AXI Timebase Watchdog Timer (PG128)` | `axi_timebase_wdt_0` | instantiated by the shell BD; offsets quoted from the vendor PG, not derived |
<!-- END GENERATED[regmap-map] -->

### Reserved pages (designed, not in any block design yet)

A page here has an owner but no hardware, so it gets **no base constant** in
firmware or in the bench — a base for a slave that is not in the fabric is
precisely how one page comes to be claimed twice. `0x44AE_0000` was: the touch
AXI IIC (`fpga/shell/bd/touch_iic_add.tcl`) and the staged `axi_jtag`
(`fpga/shell/bd/axijtag_uart_carry.tcl`, `host/socket_harness/carry_across.py`)
each took it from a file that could not see the other. **Resolved 2026-09-11 in
favour of TOUCH** — this contract and `docs/ARCHITECTURE.md` already awarded it
the page, a firmware driver and an XDC are written against it, and `shell_bd.tcl`
already sources its BD addition under `SHELL_TOUCH=1`, while the carry's claim
lived only in scaffolding no build sources. The carry's three pages moved up one,
keeping their relative order. `tools/gen_regmap.py`'s `RESERVATIONS` is now the
authority and refuses to generate on a collision.

<!-- The table below is GENERATED from tools/gen_regmap.py's RESERVATIONS.
     Do not add a page by editing this table -- add it there, where the
     collision check can see it. -->
<!-- BEGIN GENERATED[regmap-reservations] — gen_regmap.py — DO NOT EDIT BY HAND -->
| Page | Reserved for | Range | Pinned in | Why |
|---|---|---|---|---|
| `0x44AF_0000` | **AXIJTAG** | 64K | `fpga/shell/bd/axijtag_uart_carry.tcl` `::MPS3_AXIJTAG_BASE`<br>`host/socket_harness/carry_across.py` `AXIJTAG_BASE` | axi_jtag:1.0 AXI->JTAG shifter into the DUT SWJ-DP (staged carry-across; MUTUALLY EXCLUSIVE with the fielded JTAGBB @0x44A7) |
| `0x44B0_0000` | **MAGICID** | 64K | `fpga/shell/bd/axijtag_uart_carry.tcl` `::MPS3_MAGICID_BASE`<br>`host/socket_harness/carry_across.py` `MAGIC_BASE` | axi_gpio:2.0 all-inputs magic word 0x4A544147 ('JTAG') -- aperture-alive preflight (staged carry-across) |
| `0x44B1_0000` | **UART16550** | 64K | `fpga/shell/bd/axijtag_uart_carry.tcl` `::MPS3_UART16550_BASE`<br>`host/socket_harness/carry_across.py` `UART16550_BASE` | axi_uart16550:2.0 ns16550a console, regfile at +0x1000 (staged carry-across) |

Region `0x44A00000`..`0x44B50000` — **fully allocated**: a new block must extend `REGION_HI` in `tools/gen_regmap.py` and say so here.
<!-- END GENERATED[regmap-reservations] -->

## CLKRST (0x44A0_0000)
| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `RESET_CTRL` | [0] dut_resetn, [1] rp_resetn, [2] dbg_resetn | 1=released; async-assert/sync-deassert in RTL |
| 0x04 | `DUT_CLK_SEL` | [7:0] preset id | **scratch** on the built shell: stores the last preset id `set_clk` wrote (read back as `stats.clk_sel`) and drives nothing. The retune is the **MMCM_DRP** block @ `0x44AB_0000` |
| 0x08 | `DUT_CLK_DRP` | [15:0] drp | **inert** legacy window: `dut_clkrst.sv`'s DRP master port is tied idle in `shell_bd.tcl`; no firmware caller. Real DRP transactions go through **MMCM_DRP** (v0.3) |
| 0x0C | `STATUS` | [0] mmcm_locked, [1] dut_clk_alive | read-only |

> **DUT-clock DRP path (v0.3):** the built shell BD's Clocking Wizard exposes
> its MMCM DRP as a dedicated AXI4-Lite slave at `0x44AB_0000` (Vivado 2024.1
> clk_wiz offers only AXI-Lite DRP). Firmware reprogramming the DUT clock
> drives **that** block's standard clk_wiz DRP registers — every retune,
> preset or not. `CLKRST.DUT_CLK_SEL` (0x04) only records the preset id and
> `DUT_CLK_DRP` (0x08) is inert (2026-09-29: this note used to call them the
> coordinator-facing knobs). `CLKRST.STATUS.mmcm_locked` still reflects the
> MMCM lock.

> **I16 — CLOSED (firmware `8a12a08`, 2026-07-10; silicon 2026-09-24).** The
> DRP retune is implemented and proven. `firmware/clkrst/clkrst.c`
> (`clkrst_mmcm_drp_apply`) writes MMCM_DRP's clock configuration registers 0
> and 2 (`+0x200` D/M, `+0x208` O), pulses `LOAD`/`SEN` (`+0x25C`), then polls
> `STATUS.mmcm_locked` (bounded); `set_clk` reports that as `locked`. The
> preset table is the contract (50 MHz in, VCO fixed at 1000 MHz):
>
> | preset | `DUT_CLK_SEL` id | D | M | O | DUT clock |
> |---|---|---|---|---|---|
> | `"25mhz"`  | 0 | 1 | 20 | 40 | 25 MHz |
> | `"50mhz"`  | 1 | 1 | 20 | 20 | 50 MHz (clk_wiz default after configuration) |
> | `"100mhz"` | 2 | 1 | 20 | 10 | 100 MHz |
>
> Lookup is an exact string match and fails closed on an unknown name. Silicon:
> `set_clk` 25, 100, then 50 MHz, each relocked, with an RM-internal ILA
> capturing on every new `dut_clk` (`docs/evidence/2026-09-w3/ila_proofs_20260924.txt`
> B5, on `0x72BB0A36`). Use `stats.dut_mhz`, not `clk_sel`, to read the clock.

## DFXCTL (0x44A1_0000)
| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `DECOUPLE` | [0] decouple_en | 1 = RP isolated (assert before load) |
| 0x04 | `SHUTDOWN` | [0] axi_shutdown | AXI Shutdown Manager quiesce |
| 0x08 | `STATUS` | [0] decoupled, [1] rp_in_reset | read-only |
| 0x10 | `RM_ID` | [31:0] rm_id | read-only; the RP's `rm_id` partition pin (I8) — RM-load verify ground truth |
| 0x14 | `RM_STATUS` | [0] rm_id_valid, [1] dut_lockup, [2] dut_eth_irq | read-only; RM-load-verify status (I8) + DUT eth irq_out observability (2026-07-24, 2-FF synced) |

> **`DECOUPLE` is NOT cleared by a peripheral reset (changed 2026-09-14 with the
> shell watchdog).** It used to sit on `s_axi_aresetn`, which is
> `proc_sys_reset_shell/peripheral_aresetn` — exactly what the watchdog's
> `aux_reset_in` pulses. That was harmless only while nothing drove
> `aux_reset_in`; the moment WDOG (`0x44B4_0000`) was wired there, a watchdog
> fire during an ICAP write would have **un-clamped the RP boundary** while the
> partition was transient garbage (`docs/planning/SERVICES_PARTITION.md` §5.4).
> Three rules now hold, and firmware may rely on all three:
>
> * the **board POR** (`sys_rst_n`) is the only reset that clears it — a POR
>   reconfigures the whole device, so there is no RP state left to protect and
>   the power-on value is unchanged from what it has always been;
> * a **watchdog reset SETS it** (set-dominant), so a supervisor that has just
>   died leaves the partition isolated rather than open — and `DECOUPLE` reads
>   back 1 afterwards, so firmware is never told the opposite of the pin;
> * **a write of 0 still clears it**, so `swap_fsm.c`'s `step_release()`
>   read-modify-write recovers normally with no new firmware step.
>
> A shell whose BD has no watchdog ties `wdt_reset_i` low and behaves exactly as
> before, except that a peripheral reset no longer clears the clamp.

## HWICAP (0x44A2_0000)
Standard Xilinx AXI HWICAP register layout (WF/RF/SZ/CR/SR/…). Config agent
(A3) writes bitstream words; see PG134. **Delivery order per swap:** clearing
bitstream of the *currently-loaded* RM, then the new partial — enforced by the
coordinator, not the hardware (see `net-protocol.md` + `overlay-manifest.md`).

## VPHY (0x44A3_0000)
| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `PHY_STATE` | [0] link_up, [1] speed100, [2] full_duplex | drives BMSR/ANLPAR the model returns |
| 0x04 | `PHY_ID` | [31:0] | PHYID1/2 value the model presents |
| 0x08 | `LINK_EVENT` | [0] force_down, [1] pulse | host-injected link events for DUT testing |

## USD (0x44A4_0000) — D13 — the USER microSD slot, SPI mode (was OVLSTORE)

`fpga/shell/ip/usd_spi/` (README there). Replaces the pad-less AXI Quad SPI that
held this page as OVLSTORE: the A/B overlay store (`overlay-manifest.md`) now
lives on the **user** microSD card (raw `0xDA` partition,
`docs/planning/HANDOVER_USD_OVERLAY_STORE.md`). **Not the MCC config card**
(`V2M_MPS3`), which is not FPGA-wired.

| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `ID` | [31:0] = `0x55534431` (`"USD1"`) | read-only constant — the block-present witness |
| 0x04 | `CTRL` | [0] EN, [1] CS, [2] WIDE, [3] CD_POL, [4] CD_IGNORE | reset 0. EN enables the pads (subject to the gate below); CS=1 drives `USD_DAT[3]` low; WIDE: 0 = 8-bit, 1 = 32-bit shift; CD_POL=0: `USD_NCD` low = present; CD_IGNORE=1: card always present |
| 0x08 | `CLKDIV` | [15:0] DIV | reset 124. SCK = shell_clk / (2·(DIV+1)): 124 = 400 kHz (init), 3 = 12.5 MHz, 1 = 25 MHz (fastest supported). Change only while BUSY=0 |
| 0x0C | `DATA` | [31:0] | **write** starts a shift (SPI mode 0, MSB first; 8-bit mode sends [7:0]); **read** = last completed RX word (zero-extended in 8-bit mode). A write while BUSY is ignored and sets OVR |
| 0x10 | `STATUS` | [0] BUSY, [1] CD_PRESENT, [2] CD_RAW, [3] CD_CHANGED, [4] OVR, [5] ABORT | [3:5] sticky, write-1-to-clear. CD_PRESENT = synchronised + debounced (10 ms) + polarity, or forced by CD_IGNORE; CD_RAW = synchronised pin, no polarity; CD_CHANGED = any change of CD_PRESENT; ABORT = pads gated off mid-shift, or a DATA write while gated |

> **Pad gate (hardware):** `pads_en = EN && (CD_PRESENT || CD_IGNORE)`. Every
> output-enable, SCK included, is `pads_en`: with no card the socket's pads are
> high-Z whatever firmware writes. If `pads_en` drops mid-shift the shift aborts
> (BUSY clears, ABORT sets). A DATA write while gated starts nothing and sets
> ABORT. BUSY is already 1 on the first STATUS read after a DATA write's
> B-response. `USD_DAT[1:2]` are never driven.

## TELEM (0x44A5_0000) — I9
**There is no power sensor.** The MPS3 has no INA-class part (an AD7490 voltage
ADC only, and no FPGA pin reaches a power-monitor bus: `fpga/shell/shell_top.sv`
TELEM I2C note), the `ina228_i2c_master` engine was never written, and
`shell_bd.tcl` grounds every `ina228_*` input. The readings are therefore always
0 and the `telemetry` verb never reads them (`net-protocol.md` "Telemetry").
What the block still carries is `STATUS[0]`, re-purposed on the MicroBlaze V
shell as the DDR4 calibration flag.

| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `CTRL` | [0] enable, [1] alarm_en | [0] drives the unbuilt engine's seam (no effect). **[1] gates `STATUS[0]`**: set it before reading the calibration flag |
| 0x04 | `BUS_MV` | [31:0] | always 0 (input grounded; no sensor) |
| 0x08 | `CURR_UA` | [31:0] | always 0 (input grounded; no sensor) |
| 0x0C | `POWER_MW` | [31:0] | always 0 (input grounded; no sensor) |
| 0x10 | `STATUS` | [0] alarm, [1] i2c_err | read-only. **[0]** = `alarm_i`, 2-FF synchronised, AND `CTRL[1]`, live (not sticky). `SHELL_CPU=mbv`: `alarm_i` is the MIG's `c0_init_calib_complete`, so `STATUS[0]`=1 means DDR4 calibrated (`fpga/shell/bd/cpu_mbv.tcl` [SEAM-3]; stage0 reads it first). `SHELL_CPU=mb`: `alarm_i` is tied 0. **[1]** always 0 (no I2C engine) |

## GENCHK (0x44A6_0000) — I10
| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `CTRL` | [0] gen_en, [1] chk_en | error-inject generator/checker enable |
| 0x04 | `INJECT` | [0] bad_fcs, [1] runt, [2] giant, [3] ifg, [4] dribble | fault to inject on next frame |
| 0x08 | `TX_CNT` | [31:0] | frames generated (read-only) |
| 0x0C | `RX_CNT` | [31:0] | DUT frames checked (read-only) |
| 0x10 | `ERR_CNT` | [31:0] | frames failing the checker (read-only) |
| 0x14 | `DUT_IP` | [31:0] sniffed DUT IPv4, network order (first octet in [31:24]) | read-only |
| 0x18 | `DUT_STATUS` | [0] ip_seen, [31:16] last-seen ethertype | read-only |
> **DUT-IP sniffer (Phase-3, `DUT_IP`/`DUT_STATUS`):** an ALWAYS-ON, passive
> snoop of the same DUT-TX tap the checker reads (non-perturbing —
> `chk_s_tready`≡1), independent of `CTRL.chk_en`. It latches the DUT's own
> IPv4 out of the frames it transmits — IPv4 source address (bytes 26..29 for
> ethertype 0x0800) or ARP sender-protocol-address (bytes 28..31 for 0x0806) —
> so firmware can learn the DUT address without arming the generator/checker.
> `DUT_IP` is network order (first dotted-quad octet in bits [31:24]);
> `DUT_STATUS[0]` sets once any address is latched and stays set;
> `DUT_STATUS[31:16]` mirrors the last-seen ethertype (debug aid). ARP and IPv4
> are mutually exclusive per frame, last frame wins. **Limitation (v1):**
> UNTAGGED frames only — an 802.1Q VLAN tag (0x8100) shifts the offsets, so a
> tagged frame is ignored (its ethertype matches neither 0x0800 nor 0x0806).
> Runs entirely in the refclk domain like the counters; the 50→100 MHz AXI-Lite
> crossing is `axi_cc_genchk`'s job (no new CDC).
> **Counter semantics (v0.4, from the landed `gen_checker` RTL + the §8
> subsystem bench):** `TX_CNT` advances while `gen_en`, `RX_CNT` while
> `chk_en`, and `ERR_CNT` advances **only** on a frame that fails the checker
> (a clean run leaves `ERR_CNT` unchanged; an armed `INJECT` faults exactly the
> next generated frame, one-shot self-clearing). The RTL also **clears the
> three counters on a `gen_en`/`chk_en` 0→1 rising edge** (a v1 convenience —
> so counters are monotonic *within* an enabled session but not across an
> enable toggle). Hosts therefore **snapshot after enabling and compare
> deltas** (`pyverify.mactest` does this); the reference `fakeshell` models the
> simpler strictly-monotonic counters, valid for the enable-once driver flow.
> There is no counter-clear register.
> The `{"op":"macgen",…}` control verb drives GENCHK as of **net-protocol.md
> v0.4** (I10 tail RESOLVED): it writes `CTRL.gen_en`/`chk_en` + the `INJECT`
> one-hot and reads back `TX_CNT`/`RX_CNT`/`ERR_CNT` (see net-protocol.md "MAC
> gen/checker control"). BRIDGE (I11) stays register-less in v0.1: fixed 3-port
> forwarding, no MicroBlaze visibility — confirmed intentional.

## JTAGBB (0x44A7_0000) — I5 (JTAG cutover — was SWDBB)
Pin-wiggler backing the OpenOCD `remote_bitbang` server (net-proto **6921**).
`jtag_bb` replaces `swd_bb` in the same 0x44A7 slot (shell_bd.tcl); identical
AXI4-Lite CSR, JTAG partition pins instead of SWD.
| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `DRIVE` | [0] tck, [1] tms, [2] tdi | write to set the jtag_* partition pins |
| 0x04 | `SAMPLE` | [0] tdo | read-only; sampled from DUT |
> The `jtag_server` firmware translates `remote_bitbang` chars to DRIVE/SAMPLE
> pokes; drives `jtag_tck`/`jtag_tms`/`jtag_tdi`, reads `jtag_tdo`
> (partition-pins.md JTAG group). srst char → CLKRST `dbg_resetn`. The former
> `swd_bb`/`swd_server` (SWD, port 6920) is retired on this shell; `swd_server.c`
> stays compiled-but-dormant for revert.

## DBGBR (0x44A8_0000) — I6
Xilinx Debug Bridge (AXI→BSCAN mode) register interface; the `xvc_server`
firmware bridges XVC-over-TCP (net-proto 2542) to it. Standard Debug Bridge
layout — no custom fields. **Must be in static** (survives DFX swaps).

## UARTBR (0x44A9_0000) — I7 (fields codified v0.2 from the landed RTL)
AXIS ↔ MicroBlaze FIFO bridge for the three console streams; `uart_over_eth`
firmware relays each to its TCP port (6930/6931/6932). RTL:
`fpga/shell/ip/uart_bridge/`.
| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `U0_TX` / `U0_RX` | [7:0] data, [8] valid | DUT UART0 (boot monitor). W: push byte host→DUT (dropped if tx_full — poll `FIFO_STATUS` first; [8] ignored on write). R: pop one byte DUT→host, `{valid,byte}` or `{0,0x00}` when empty. **Reads are destructive.** |
| 0x04 | — | — | reserved; reads 0 |
| 0x08 | `U1_TX` / `U1_RX` | [7:0] data, [8] valid | DUT UART1 (application) — identical semantics to 0x00. Register/FIFO path fully implemented; the DUT-side `uart1_*` seam is **not a partition pin in this version** (single-core I1) and is tied off in the BD until the multicore RM widens `partition-pins.md`. |
| 0x0C | — | — | reserved; reads 0 |
| 0x10 | `SWO_RX` | [7:0] data, [8] valid | SWO/ITM trace, destructive read; writes accepted-no-effect |
| 0x14 | `FIFO_STATUS` | [0] u0_tx_full, [1] u0_rx_empty, [2] u1_tx_full, [3] u1_rx_empty, [4] swo_rx_empty | read-only, live flags in the `s_axi_aclk` domain |
| 0x18 | `SWO_CFG` | [15:0] divisor (rw), [16] enable (rw), [30] overflow (ro, sticky), [31] frame_err (ro, sticky) | SWO deserialiser config: bit period = `divisor+1` `dut_clk` cycles (`divisor ≥ 7` recommended for ≥8× oversampling, ≥ 3 absolute min). Divisor is captured on enable's rising edge — **changing it requires toggling enable off/on**. Sticky bits clear while `enable=0`. |

UARTBR behaviour notes (normative, from the landed RTL): all five streams
cross `dut_clk` ⇄ `s_axi_aclk` through async FIFOs inside this block (the
console group's partition-boundary CDC lives here); DUT→host directions are
lossless (AXIS `tready` backpressure), host→DUT writes while full are
dropped (poll `FIFO_STATUS`), SWO drops-newest on overrun with the sticky
overflow flag. The bridge resets with the shell only — console FIFOs are
**not** flushed by `dut_resetn`/`rp_resetn`/swaps (drain policy belongs to
the coordinator firmware). SWO capture is **UART/NRZ (8N1) mode only** in
this version — see OPEN_ISSUES I29 (Manchester pending TPIU confirmation).

## GPIO (0x44AA_0000) — I4 (board-port passthrough)
Board GPIO/PMOD passthrough (`partition-pins.md` board-port group). Default =
the DUT's `dut_gpio_*` drives the pads; the host can override per-bit when no
DUT owns them.
| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `IN` | [NGPIO-1:0] | board pad → sampled value (read-only) |
| 0x04 | `OUT` | [NGPIO-1:0] | host drive value (used where `OWN`=host) |
| 0x08 | `OE` | [NGPIO-1:0] | host output-enable |
| 0x0C | `OWN` | [NGPIO-1:0] | 0 = DUT owns the bit (default), 1 = host mux overrides |

## MMCM_DRP (0x44AB_0000) — v0.3 (DUT-clock reconfig)
Standard **Xilinx Clocking Wizard AXI4-Lite DRP** register interface (no
custom fields) — the built shell's DUT-clock MMCM. Vivado 2024.1 clk_wiz
exposes reconfiguration only over AXI-Lite, so arbitrary DUT-clock frequencies
are set here (clk_wiz's SW-reset / config / status / DRP registers per PG065),
not through `CLKRST.DUT_CLK_DRP`. The `set_clk` presets are firmware (the
CLKRST I16 table above); `CLKRST.DUT_CLK_SEL` only records the preset id, and
`CLKRST.STATUS.mmcm_locked` reports lock. Must be in static (survives DFX swaps).

## CLCD (0x44AC_0000) — v0.4.1 — ✅ **LIVE: INSTANTIATED, PANEL LIT**

On-board QVGA (320×240) colour LCD, controller **Himax HX8347-D**, driven over
an **8-bit 8080 parallel bus** (board TRM `100765_0000_04_en` §2.11; the
`CLCD_WR_SCL` pad is the HX8347-D's dual WR/SCL pin, strapped parallel here).
This block is *only* an AXI4-Lite→8080 byte-streaming bus master: firmware
pushes `{RS, byte}` pairs through `CMD`/`DATA` and a bus FSM times the strobes.
It holds **no framebuffer and no font** — the HX8347-D's on-chip GRAM is the
framebuffer. Static-side peripheral: it never crosses the RP boundary and takes
**no DFX decoupler entry**.

> ## ✅ LIVE (v0.4.1, 2026-07-14) — the block is instantiated and the panel is lit
>
> `clcd_0` is in the shell: `fpga/shell/bd/shell_bd.tcl:494` (cell,
> `soclabs.org:user:clcd:1.0`), `:495` (`C_S_AXI_ADDR_WIDTH {32}` — decode at 32,
> **never** the RTL default 12), `:499` (clock/reset fan-out), `:1041`
> (`NUM_MI 15`), `:1070` (master index **14**), `:1108`
> (`assign_bd_address -offset 0x44AC0000 -range 64K`). Confirmed in the build:
> `fpga/shell/build_results_2026-07-11/bd_summary.txt:11`, `:51`. Landed in
> `b4afe3e` (2026-07-11); `static_id` = **`0xE4B1C44A`**.
> **The panel renders the harness status screen** (board-observed 2026-07-14).
>
> **Shipped build parameters:** `READ_PATH = 0` (write-only — `CLCD_RD` is a
> constant-high output and `READ` always reads 0), `FIFO_DEPTH = 128`,
> `TIMING` = `wr_lo 4 / wr_hi 4 / cs_setup 2` @ 100 MHz `s_axi_aclk`.
> **Firmware:** built `CLCD=1` (`-DMPS3_HAS_CLCD`) — but the tree default is
> still `CLCD ?=` (empty), so a plain `make elf` has no CLCD driver.
> **Full proven panel configuration: `docs/CLCD_PANEL_FACTS.md`.**
>
> <details><summary>~~HAZARD (v0.4, now FALSE) — there is no slave at this page today~~</summary>
>
> ~~`shell_bd.tcl` sets `NUM_MI = 14` and `assign_bd_address` stops at MMCM_DRP
> (`0x44AB`). A MicroBlaze access to `0x44AC_xxxx` on the shipped shell is
> unmapped: the `axi_interconnect` DECERRs and MicroBlaze bus exceptions are
> **not** enabled, so it neither traps nor works — `STATUS` reads back garbage.
> Firmware therefore gates the driver behind `MPS3_HAS_CLCD` (default **off**);
> the define is turned on in the same Phase-D batch that instantiates the
> slave.~~ — **That batch ran. The slave exists. Kept only as the record of why
> the `MPS3_HAS_CLCD` gate exists.**
> </details>

| Off  | Reg      | Bits | Notes |
|------|----------|------|-------|
| 0x00 | `CTRL`   | [0] enable, [1] backlight (`CLCD_BL`), [2] `reset_n` (`CLCD_RST`, 0 = panel held in reset), [3] fifo_reset (self-clearing), [4] read_start (self-clearing; `READ_PATH=1` only) | reset value `0x0` — panel dark and held in reset, matching the legacy tie-off |
| 0x04 | `CMD`    | [7:0] byte | write pushes a **command** byte (RS=0) into the bus FIFO; W1 |
| 0x08 | `DATA`   | [7:0] byte | write pushes a **data** byte (RS=1) into the bus FIFO; W1 |
| 0x0C | `STATUS` | [0] fifo_full, [1] fifo_empty, [2] busy, [15:8] fifo_level | read-only; the backpressure poll |
| 0x10 | `READ`   | [7:0] rdata, [8] valid | read-only, **no side effect**; holds the last `CLCD_RD` capture. Reads `0` when the block is built write-only (`READ_PATH=0`) |
| 0x14 | `TIMING` | [7:0] wr_lo, [15:8] wr_hi, [23:16] cs_setup | 8080 strobe timing in `s_axi_aclk` cycles |

**No register in this block has a read side effect.** A panel read-back is
armed by writing `CTRL.read_start` (self-clearing); firmware then polls
`STATUS.busy` and reads `READ`, which merely holds the captured `{valid,rdata}`.
Arming on a *read* of `READ` was rejected: this platform dumps CSR pages over
SWD/XVC, and `tests/csr_decode_width/` reads every offset in the page — either
would silently launch 8080 bus cycles at the panel. The shell has already been
bitten by a read side effect once (a read of DFXCTL `0x20` popped a console
byte; see the decode-alias note in `dfx_ctl.sv`).

Writes to `CMD`/`DATA` when `STATUS.fifo_full` is set are **dropped**, not
stalled (an AXI-Lite slave that back-pressures `awready` on a full FIFO would
let a firmware bug wedge the MicroBlaze data bus, and `clcd_poll()` shares its
superloop with the lwIP timers). Firmware polls `STATUS` and never pushes into
a full FIFO; the bench proves zero bytes are lost when the panel side stalls.

**The panel register values streamed through `CMD`/`DATA` are NOT part of this
contract.** The HX8347-D init / GRAM-window / pixel-format sequence is a ported
firmware data table with its own provenance record — see
`fpga/shell/ip/clcd/README.md` and `firmware/clcd/`. This block is protocol-
agnostic by construction, so correcting that table never touches the RTL.

> **Forward note (v0.5) — the KVM sits between this block and the pads.** Once
> CLCDKVM lands (Wave 4), `clcd_0` no longer reaches `shell_top`'s IOBUFs: its
> pad outputs feed `clcd_kvm_0`, which arbitrates them against the DUT's. **No
> register in this block changes.** `CTRL[1]` (backlight) and `CTRL[2]` (reset_n)
> still work, because `CLCDKVM.CTRL.bl_rst_src` **resets to 0 = "follow
> `clcd_0`"** — deliberately, so the shipped firmware lights the panel unchanged
> on the new bitstream. `clcd.sv` additionally gains two **append-only** output
> pins (`busy_o`, `fifo_empty_o`, already internal wires at `:359`/`:299` and
> already published in `STATUS[2:1]`) that the KVM's safe-switch gate reads
> combinationally. Firmware **should** move to `CLCDKVM.CTRL[5]`/`[6]` and set
> `bl_rst_src = 1`, but nothing breaks if it does not.

## CLCDKVM (0x44AD_0000) — v0.5 — ✅ **LIVE: INSTANTIATED**

The **CLCD KVM**. One physical HX8347-D panel; two independent 8080 bus masters
wanting it — the harness's `clcd_0` (status screen, MicroBlaze-driven) and a
**student accelerator inside the nanosoc RM** (reaching the shell over the
`dut_gpio` **tunnel**, `docs/contracts/dut-display-tunnel.md`). This block
arbitrates them, drives the pads, and **owns `CLCD_BL`/`CLCD_RST`**.

It is **not a 2:1 mux**: the bus is strobed (so a cutover must not truncate a
cycle or leave `CS` asserted across the seam), the panel *is* the framebuffer
(so every handover must hard-reset it and make the new owner re-init), and the
DUT lives in a **reconfigurable partition** (so the KVM must force-revert to the
harness whenever the decoupler engages or the RP is in reset). That third
requirement is why the KVM is in the **static shell** and nowhere else.
Static-side peripheral: it never crosses the RP boundary and takes **no DFX
decoupler entry**.

> ### CORRECTION — the Wave-4 batch ran; the slave is live
>
> <details><summary>~~HAZARD (v0.5, now FALSE) — there is no slave at this page today~~</summary>
>
> ~~`shell_bd.tcl` sets `NUM_MI = 15` and `assign_bd_address` stops at CLCD
> (`0x44AC`). A MicroBlaze access to `0x44AD_xxxx` on the shipped shell is
> **unmapped**: the `axi_interconnect` DECERRs, MicroBlaze bus exceptions are
> **not** enabled, so it neither traps nor works — `STATUS` reads back garbage.
> Firmware must gate the driver behind a build flag (as `MPS3_HAS_CLCD` does)
> until the **Wave-4** batch instantiates the slave, adds the `USER_nPB1` pad,
> re-mints `static_id` and re-keys all 8 overlays.~~
>
> </details>
>
> `shell_bd.tcl:578` creates `clcd_kvm_0`, `:1328` sets `NUM_MI 18` and `:1407`
> assigns `0x44AD_0000` range 64K. The fielded shell is built with
> `CLCD_KVM=1` (`docs/FIELDED_SHELL.md`, `fielded_fw_flags`). The claim above
> outlived its truth by two mints in FOUR files at once, which is why the map
> table and the offsets on this page are now GENERATED from that BD rather than
> restated here — see `tools/gen_regmap.py`. It is left struck through, not
> deleted: it records what the v0.5 reservation was and why the block was gated.
>
> **Frozen port + CSR contract, with the full ownership-FSM spec:
> `fpga/shell/ip/clcd_kvm/README.md`.** The table below is the register contract;
> that file is normative for everything else (timing, quiescence, CDC, debounce).

**Decode width: 32.** `shell_bd.tcl` instantiates every CSR block with
`CONFIG.C_S_AXI_ADDR_WIDTH {32}`; the RTL default of 12 is **never** what ships.
Decode the block's own **64 KiB page** (the `dfx_ctl.sv:229-258` idiom). A
`clcd_kvm` entry in `tests/csr_decode_width/` is a **required** deliverable.

**All times are in MICROSECONDS**, counted against a 1 µs tick derived from
`s_axi_aclk` (`TICK_DIV = CLK_HZ / 1_000_000` = 100 at the shipped 100 MHz). A
time field written as `0` is floored to 1 µs.

| Off  | Reg | Bits | Notes |
|------|-----|------|-------|
| 0x00 | `CTRL` | [0] `src_sel` (0=HARNESS, 1=DUT — **written only when [16] is also 1**), [1] `force_harness`, [2] `timeout_en` (**reset 1**), [3] `pb_en` (**reset 1**), [4] `dut_req_en` (**reset 0**), [5] `backlight` (`CLCD_BL`), [6] `panel_rst_n` (`CLCD_RST`), [7] `bl_rst_src` (**reset 0** = BL/RST follow `clcd_0`; 1 = follow [5]/[6]), [8] `panel_rst_pulse` (**W1P**), [9] `force_switch` (**W1P**), [16] `src_sel_we` (**W1P**) | reset `0x0` (panel dark + held in reset, matching CLCD's own `CTRL` reset and the legacy tie-off). `src_sel_we` exists so a read-modify-write of `CTRL` can never clobber an ownership change made by a concurrent button press. Byte strobes honoured. |
| 0x04 | `STATUS` | [0] `owner` (0=HARNESS, 1=DUT), [1] `switch_pending`, [2] `tgt_owner`, [3] `panel_rst_active`, [4] `panel_settling`, [5] `granting`, [6] `draining`, [7] `kvm_drives_pads`, [8] `harness_quiet`, [9] `dut_quiet`, [10] `dut_req`, [11] `pb_level` (debounced, 1=pressed), [12] `pb_raw`, [13] `decoupled`, [14] `rp_in_reset`, [15] `interlock`, [18:16] `state` | read-only, **no side effect**. `state`: 0=`S_OWN`, 1=`S_DRAIN`, 2=`S_RST`, 3=`S_SETTLE`, 4=`S_GRANT`. |
| 0x08 | `EVENT` | [0] `harness_gained`, [1] `harness_lost`, [2] `dut_gained`, [3] `dut_lost`, [4] `timeout_fired`, [5] `forced_revert`, [6] `pb_toggle`, [7] `panel_reset_done` | **RW1C** — write 1 to clear, write 0 to leave. **Reads do NOT clear.** Reset `0x0`. |
| 0x0C | `PANEL_TMR` | [15:0] `rst_us` (reset **2000**), [31:16] `settle_us` (reset **5000**) | `CLCD_RST` held low for `rst_us`; pads then held idle *by the KVM* for `settle_us` before any source may drive. Matches the working driver's 2 ms reset pulse (`clcd.c:56-58`) and the init table's leading `{HX_DLY,5}` (`hx8347_init.c:74`). |
| 0x10 | `TIMEOUT` | [31:0] `timeout_us` (reset **1000**) | Hung-owner timeout, applied separately to the outgoing (`S_DRAIN`) and incoming (`S_GRANT`) quiescence waits. Armed by `CTRL.timeout_en`. 1 ms ≈ 9,000 byte-times at the panel's ~110 ns/byte — 35× the worst legitimate drain, 250× shorter than the 250 ms refresh period. |
| 0x14 | `DEBOUNCE` | [15:0] `debounce_us` (reset **10000**) | `USER_nPB[1]` debounce integration time. |
| 0x18 | `TUNNEL` | [15:0] `gpio_o`, [31:16] `gpio_oe` | read-only, **no side effect**. The synchronised + stability-filtered raw tunnel — the host's window onto what the DUT is actually driving. |

**No register in this block has a read side effect.** `EVENT` is **W1C, not
read-to-clear**; `TUNNEL` and `STATUS` are pure captures; unmapped offsets in the
page read `0` and accept writes with no effect (`BRESP=OKAY`). Every action is
armed by a **write** (`CTRL.panel_rst_pulse`, `CTRL.force_switch`,
`CTRL.src_sel_we`). This is not stylistic: the platform dumps whole CSR pages over
SWD/XVC, and `tests/csr_decode_width/` reads every offset in the page — a
read-to-clear `EVENT` would silently destroy handover events on every debugger
attach. The shell has been bitten by a read side effect once already (a read of
`DFXCTL 0x20` popped a UART console byte, `dfx_ctl.sv:229-258`).

**The firmware handover contract**, in one line: *re-initialise the panel and
repaint whenever `EVENT.harness_gained | EVENT.panel_reset_done` is set while you
own it* — clear the bits **before** repainting, so a handover racing the repaint
is not lost. `harness_gained` alone is **not** sufficient: a DFX interlock firing
mid-handover resets the panel and hands it back with **no** owner change.
To warn the user before losing the panel, poll `STATUS.switch_pending` — the
safe-switch gate waits for the harness's FIFO to drain, so a banner pushed at that
point reaches the panel before the handover commits.

**The button is sampled in HARDWARE** (3-FF synchroniser + 10 ms debounce +
press-edge → toggle, all in the KVM), so the switch still works when the harness
firmware is wedged. `CTRL.pb_en` (reset **1**) is the only thing that can disable
it. `USER_nPB0` (`AT30`) is the **system POR** (`shell_top.sv:117`) — untouched.
The KVM's button is `USER_nPB1` (`AT32`), a new shell pad in Wave 4.

**The DUT reaches this block over a tunnel, not over partition pins.** See
`docs/contracts/dut-display-tunnel.md`: the DUT's 8080 bus is carried on the free
upper 8 bits of `dut_gpio_o`/`dut_gpio_oe` (bits `[7:0]` stay with the LEDs), with
**strobes encoded ACTIVE-HIGH** so the existing `DECOUPLED_VALUE 0x0` clamp
already means "all strobes idle, nothing requested, nothing in flight" **by
construction** during every partial reconfiguration. `partition-pins.md` is
**unchanged**, no RM wrapper gains a port, no RM needs OOC re-synthesis, and
`pin_check` stays green — that green is the proof the tunnel cost nothing.

## DUTEGR (0x44B2_0000) — v0.7 (RX) + v0.9 (inject) — ✅ **LIVE: INSTANTIATED** (the DUT's return path; the inject side lands with mint 3)

| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `CTRL` | [0] EN, [1] FLUSH (W1, self-clearing), [2] CLR_CNT (W1, self-clearing) | capture enable / bounded drain / counter clear |
| 0x04 | `STATUS` | [0] FRAME_RDY, [1] EMPTY, [2] DATA_FULL, [3] DESC_FULL, [4] OVF (sticky), [5] FLUSH_BUSY, [6] DESYNC (sticky), [8] EN (capture-side read-back) | read-only |
| 0x08 | `LEVEL` | [15:0] committed bytes waiting, [31:16] committed frames waiting | read-only |
| 0x0C | `FRAME_LEN` | [15:0] bytes left in the head frame, [31:16] the head frame's total length | read-only; both 0 when `FRAME_RDY` is 0 |
| 0x10 | `DATA` | [7:0] byte, [8] VALID, [9] LAST | read-only and **DESTRUCTIVE** — a read pops one byte |
| 0x14 | `RX_FRAMES` | [31:0] | frames captured whole (read-only) |
| 0x18 | `DROP_FULL` | [31:0] | frames dropped for want of room (read-only) |
| 0x1C | `DROP_GIANT` | [31:0] | frames dropped for exceeding `MAX_FRAME` (read-only) |
| 0x20 | `TX_CTRL` | [0] COMMIT (W1, self-clearing), [1] ABORT (W1, self-clearing; wins over COMMIT), [2] CLR_CNT (W1, self-clearing: `TX_FRAMES`, `TX_REJECT`, `TX_FLUSHED` and the TX sticky bits only), [3] FLUSH (W1, self-clearing, bounded), [8] RAW (rw, reset 0) | inject control. Actions in byte lane 0, RAW in lane 1; reads return RAW only |
| 0x24 | `TX_STATUS` | [0] ROOM, [1] EMPTY, [2] DATA_FULL, [3] DESC_FULL, [4] REJ (sticky), [5] FLUSH_BUSY, [6] STAGING, [7] DESYNC (sticky) | read-only; the sticky bits clear on `TX_CTRL.CLR_CNT` |
| 0x28 | `TX_SPACE` | [15:0] free bytes, [31:16] free frame slots | read-only |
| 0x2C | `TX_DATA` | [7:0] | **write-only** (lane 0): stages one byte. A read returns 0 and has no side effect |
| 0x30 | `TX_FRAMES` | [31:0] | frames handed to the bridge whole (read-only) |
| 0x34 | `TX_REJECT` | [31:0] | COMMITs refused: empty, too short, too long, or no room (read-only) |
| 0x38 | `TX_FLUSHED` | [31:0] | committed-but-unsent frames discarded by `TX_CTRL.FLUSH` (read-only) |

> **What this block is.** DUT *reception* has been silicon-proven since
> 2026-07-30 (`DFXCTL.RM_STATUS[2]` 0 → 1 under `gen_checker` traffic). The
> RETURN half of Option C (`docs/DUT_ETHERNET_EGRESS.md`) was designed and never
> built: `shell_bd.tcl` SECTION 5 tied `eth_bridge_3port`'s management egress to
> `mgmt_m_tready = 1` with nothing behind it, so every frame the DUT transmitted
> was drained into a constant. A DUT could be talked to and could not answer.
> DUTEGR is the consumer that tie-off stood in for: the bridge's mgmt egress
> lands in a dual-clock frame FIFO (`fpga/shell/ip/dut_egress/`) that the
> MicroBlaze reads a byte at a time over this surface.

> **It never backpressures, and that is deliberate.** `frm_tready` is a constant
> 1. `eth_bridge_3port` is store-and-forward with ONE shared round-robin
> sequencer and head-of-line blocking by design, so a sink that stalls this port
> parks the whole bridge — killing the DUT's ingress scoring and the LAN9220
> uplink along with the egress it was trying to protect. The block therefore
> drops what it cannot hold, and the entire status surface exists so that it
> never drops **silently**. Exactly one of three counters moves for every frame
> the block accepts, so
> **`RX_FRAMES + DROP_FULL + DROP_GIANT` = frames seen while `CTRL.EN`** — an
> invariant `tests/dut_egress` asserts directly against a monitor on the
> capture port.

> **`CTRL.EN` resets to 1.** The failure this platform has actually suffered is
> silence-by-construction (SECTION 5 shipped the DUT's RX tied to zero *by
> design*, and nothing on the board could say so). A capture block that defaults
> OFF is one more way for a live DUT to look dead, so `RX_FRAMES` moves the
> moment a DUT transmits, with no setup at all. With nobody draining,
> `DROP_FULL` then climbs — honest telemetry, not a fault. `CTRL.EN = 0` is the
> explicit opt-out. `EN` is sampled only at a frame's FIRST beat, so toggling it
> mid-frame can never produce a half-captured frame.

> **Reading a frame.** Poll `STATUS.FRAME_RDY`, read `FRAME_LEN[31:16]` for the
> whole frame's length, then read `DATA` that many times. Frames are
> store-and-forward: a frame is visible only once **all** of its bytes are
> committed, so the length cannot change underneath the read and a torn frame
> can never be observed. `DATA[9]` (LAST) marks the frame's final byte
> independently of the length; if the two ever disagree `STATUS.DESYNC` latches
> and stays latched until `CTRL.CLR_CNT`. Frames **include their 4-byte FCS**
> (`link_partner_mac.sv`'s AXIS convention). `DATA` on an empty FIFO returns
> `VALID = 0` and pops nothing; every other offset in the page is unmapped,
> reads 0, and — critically — pops nothing either (the UARTBR destructive-alias
> hazard).

> **`CTRL.FLUSH`** is a BOUNDED drain in the AXI clock domain only: it pops for
> at most `DATA_DEPTH + FRAME_DEPTH` cycles, then stops whether or not the DUT
> is still transmitting. No second reset and no reset crossing. Its purpose is
> discarding the previous RM's frames after a DFX swap.

> **Sizing and clocks.** `DATA_DEPTH = 2048` bytes, `FRAME_DEPTH = 16` frames,
> `MAX_FRAME = 1536` bytes (a longer frame is rolled back whole and counted in
> `DROP_GIANT`, never truncated onto the read port). The AXI-Lite surface runs
> at the shell's 100 MHz; the capture port runs at the 50 MHz RMII reference the
> bridge lives in, and the crossing is this block's (`dutegr_cfifo.sv`,
> gray-pointer with a separate COMMIT pointer). This page is **0x44B2_0000**,
> not the next page after CLCDKVM: the region below was full, so this is the
> block that extended `REGION_HI` — see the reservation note in the generated
> appendix.

### DUTEGR inject side (v0.9, mint 3) — host → DUT

> **What it is.** The mirror of the capture side: the MicroBlaze STAGES a frame
> one byte at a time into `TX_DATA`, COMMITs it, and the block streams it into
> `eth_bridge_3port`'s port-B **ingress** (`shell_bd.tcl` SECTION 5 wires
> `dut_egress_0/inj_m_*` to `eth_mac_test_subsystem.mgmt_s_*`). It is the
> first way for anything on the shell side to put a frame onto the DUT's
> Ethernet. Status: RTL + simulation (`tests/dut_egress`, both arms); on
> silicon only after mint 3 and the handover's §8 acceptance.

> **It MAY wait — the capture side may not.** A stalled SINK parks the
> bridge's one shared sequencer, which is why `frm_tready` is a constant 1. The
> inject port is a SOURCE into a per-port ingress buffer: waiting on its
> `tready` holds up only its own frame. What it never does is START a frame it
> does not wholly have: it starts only when the frame is committed and every
> byte of it is visible, then streams it back to back.

> **Sending a frame.**
> 1. Check `TX_STATUS.ROOM` (a `MAX_FRAME` frame can be staged and a slot is
>    free) or `TX_SPACE` (exact free bytes and slots).
> 2. Write each byte to `TX_DATA` (`[7:0]`, byte lane 0).
> 3. Write `TX_CTRL = (RAW << 8) | COMMIT`. `RAW` is an ordinary rw bit in the
>    same word as the W1 actions: a full-word write sets it, and the COMMIT in
>    that write uses the RAW it carries.
>
> Staged bytes are invisible to the transmitter until COMMIT. `ABORT`
> discards them. `TX_STATUS.STAGING` says bytes are staged.

> **RAW = 0 (the default) is a normal NIC.** Stage `dst + src + type +
> payload` with NO FCS; the block zero-pads to 60 bytes and appends the IEEE
> CRC-32 FCS (LSB first — the same `crc32_byte` as `link_partner_mac.sv`,
> which neither inserts nor strips an FCS, so without this every frame would
> reach the DUT with a bad CRC). Staged length must be **14 … 1514**, so every
> RAW = 0 frame on the wire is a legal 802.3 frame of **64 … 1518** bytes
> (amendment A1). **RAW = 1** sends the staged bytes exactly as written,
> **1 … `MAX_FRAME` (1536)**, no pad and no FCS — fault injection (bad FCS,
> runts, and oversize frames on purpose).

> **Rejects are whole.** A COMMIT that is empty, too short, too long for its
> mode, finds no frame slot, or follows a staged byte the FIFO had no room for
> is REFUSED: the staged bytes are rolled back whole (none of them ever
> leaves), `TX_REJECT` counts it and `REJ` latches. `ABORT`, and `COMMIT|ABORT`
> in one write, count nothing — they are not COMMITs.

> **Every COMMIT is accounted for** (amendment A2):
> **`TX_FRAMES + TX_REJECT + TX_FLUSHED + frames_queued` = COMMITs since
> `TX_CTRL.CLR_CNT`**, where `frames_queued = FRAME_DEPTH − TX_SPACE[31:16]` (a
> frame counts as queued until its last byte is handed over;
> `TX_STATUS.EMPTY` = none queued). Each COMMIT lands in exactly one of
> `TX_REJECT` (refused), `TX_FRAMES` (sent whole) or `TX_FLUSHED` (accepted,
> then discarded by FLUSH). Exact at quiescent points; issue `CLR_CNT` while
> `EMPTY`, or frames already queued land in the new count. `tests/dut_egress`
> asserts it against a monitor on the inject port, across FLUSH and under a
> seeded random sequence that includes FLUSH.

> **`TX_CTRL.FLUSH`** discards every frame **committed before the FLUSH write
> (or in the same write)** that has not started when the request reaches the
> transmitter, and counts each in `TX_FLUSHED` (not `TX_REJECT`; `REJ` does not
> latch). Frames committed after the FLUSH write are not affected — they are
> sent once it completes. A frame already on the port is never cut; it
> finishes and counts in `TX_FRAMES`. Use it after a DFX swap, with injection
> stopped at the swap FSM's GATE and FLUSH written after DONE. Bounded: at most
> `FRAME_DEPTH` frames per FLUSH; `FLUSH_BUSY` until done, and a FLUSH written
> while `FLUSH_BUSY` is **ignored** — poll it. Staged bytes are untouched (that
> is `ABORT`).

> **Routing reality (fixed table, no learning).** The DUT's real MAC is not in
> the bridge's table, so a frame to it FLOODS to the DUT and to the tied-off
> uplink (drained); broadcast floods the same way; the table's `DUT_MAC`
> (`02:00:00:00:00:02`) routes to the DUT only. A frame to `MGMT_MAC`
> (`02:00:00:00:00:01`) resolves only to its own ingress port and the bridge
> DROPS it silently — it still counts in `TX_FRAMES`, which counts what DUTEGR
> handed the bridge. Expected, not a bug.

> **Independent of the capture side.** No TX access moves any RX state and no
> RX access moves any TX state: `CTRL.CLR_CNT` and `TX_CTRL.CLR_CNT` each
> clear their own side only, and `TX_DATA` is not the RX `DATA` window.


## USRACC (0x44B3_0000) — v0.8 — ✅ **LIVE: INSTANTIATED** (the fabric's own build identity)

| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `MAGIC` | [31:0] = `0x55535241` (`"USRA"`) | read-only constant — the block-present witness |
| 0x04 | `VALUE` | [31:0] | read-only — the AXSS `USR_ACCESS` word this bitstream carries, i.e. `HARNESS_VER32` |
| 0x08 | `STATUS` | [0] VALID | read-only — the `USR_ACCESSE2` primitive has presented `DATAVALID` and the word is captured |

> **What this block is, and what it closes.** `docs/VERSIONING_PLAN.md` §3.4
> deferred a fabric readback of `USR_ACCESS` to "the next re-key"; this is it.
> `fpga/shell/ip/usr_access_rd/` wraps the **one** `USR_ACCESSE2` BEL on this
> device (`CONFIG_SITE_X0Y0/USR_ACCESS`, clock region X5Y1 — enumerated against
> the real part in §2.1 of that plan, and **outside** the RP pblock, which is why
> no RM can ever carry its own stamp). One generator emits `HARNESS_VER32`;
> `build_dfx.tcl` stamps it into `BITSTREAM.CONFIG.USR_ACCESS` and
> `gen_version.py` compiles the same number into the image. They agree by
> construction unless the `.bit` and the image baked into it came from different
> builds — the "flashable base whose `updatemem` was never re-run" hazard. Until
> this block existed nothing on the board could see that, and the `version`
> verb's `usr_access`/`skew` pair (shipped in `ba2f4be`) read `null`/`null`
> everywhere.

> **Read `MAGIC` first. This is a contract, not a suggestion.** Every unmapped
> page in this window reads back 0, so on a shell WITHOUT this block a bare
> `VALUE` read returns 0 — and 0 is a perfectly plausible build identity.
> Without the witness, "the fabric says `0x00000000`" and "there is no sensor
> here" are the same read. `firmware/platform/mps3_usr_access.c` checks `MAGIC`,
> then `STATUS.VALID`, and reports **no answer** for either miss, which the
> control-channel codec renders as `"usr_access":null,"skew":null` — never as a
> zero and never as a `false` verdict. A check that did not happen must not read
> as "checked, fine".

> **It is purely observational.** Nothing else in the shell reads it, nothing
> depends on it, and a build without it behaves identically except that the
> cross-check goes back to saying no comparison was made. `VALUE`/`VALID` are
> cleared by `s_axi_aresetn` and RE-DERIVED from the primitive within three
> clocks — `DATAVALID` is configuration state and a fabric reset (including the
> watchdog's) does not disturb it — so the block can never report a word it has
> stopped reading, and the answer is back long before firmware finishes booting.
> It is **not** at §3.4's proposed `DFXCTL.SHELL_USR_ACCESS @ 0x44A1_001C`: the
> mechanism was taken, the address was not, and
> `fpga/shell/ip/usr_access_rd/README.md` says why.


## WDOG (0x44B4_0000) — v0.8 — ✅ **LIVE: INSTANTIATED** (the shell watchdog)

Vendor block — `xilinx.com:ip:axi_timebase_wdt:3.0`. Offsets in the generated
appendix are **quoted from PG128**, not derived from RTL in this tree.

| Off | Reg | Bits | Notes |
|---|---|---|---|
| 0x00 | `TWCSR0` | [3] WRS ("the last reset was mine", W1C), [2] WDS (**W1C — THE KICK**, set on the 1st expiry), [1] EWDT1 (enable, half 1) | control/status 0 |
| 0x04 | `TWCSR1` | [0] EWDT2 (enable, half 2) | both enables must be set for the watchdog to run |
| 0x08 | `TBR` | [31:0] | read-only free-running timebase |

> **Why it exists.** Until 2026-09-14 there was no hardware watchdog in the
> shell at all. If the superloop STOPPED — not "ran slowly", *stopped* — nothing
> on the board noticed, and the only ingress is the network that superloop
> serves; recovery was a physical power-cycle of the chassis
> (`docs/planning/SERVICES_PARTITION.md` §5.1). 107 LUT, 1.3% of the static.

> **Two stages, and the kick is conditional.** The first expiry sets `WDS`, a
> bit firmware can read and report; the second asserts `Timebase_WDT_Reset`,
> which `shell_bd.tcl` routes to `proc_sys_reset_shell/aux_reset_in` —
> unconnected in every shell up to `0xA8C1C535`. That gives the standard warm
> restart (`mb_reset` → MicroBlaze + both LMBs, `peripheral_aresetn` → the AXI
> slaves, `interconnect_aresetn` → the interconnect) and the diagnostic mailbox
> **survives**, because it is `local_ram` BRAM and a processor reset does not
> clear BRAM. Firmware kicks by W1C-ing `TWCSR0.WDS` at the **end** of
> `mps3_service_run_pass()` and only when no vital service was skipped — a kick
> at the top of the loop would prove the loop spins, not that it serves.
> `C_WDT_INTERVAL` is 27, so the first stage is 2^27/100 MHz ≈ 1.34 s and the
> reset ≈ 2.7 s, against a summed service budget of 268 ms.

> **It resets itself, deliberately.** `s_axi_aresetn` is `peripheral_aresetn`,
> which its own reset pulses, so it comes out of a watchdog reset DISABLED and
> firmware must re-arm it in `coordinator_init()`. That is what prevents a reset
> LOOP: one reset, then either the shell comes back and re-arms or it does not
> and is left reachable for JTAG, instead of being reset every 2.7 s forever.

> **The hazard it made live, fixed in the same change.** `dfx_ctl_0`'s
> `s_axi_aresetn` IS `peripheral_aresetn`, so wiring `aux_reset_in` means a
> watchdog fire during an ICAP write would have cleared `DECOUPLE` and
> un-clamped the RP boundary while the partition was transient garbage
> (§5.4). `dfx_ctl.sv` now takes the watchdog reset as a **set-dominant** input
> that asserts isolation, and the board POR (`sys_rst_n`) as the only reset that
> releases it — so `DECOUPLE` is unchanged by a peripheral reset. See the DFXCTL
> section and `tests/dfx_ctl` (`make control-wdt-unclamp` shows the pre-fix RTL
> failing).

## The MicroBlaze V view (`SHELL_CPU=mbv`, Linux)

The same shell BD with the CPU seam set to `mbv` (`fpga/shell/bd/cpu_mbv.tcl`):
every block above at the same address, plus the CPU's own map -- the LMB,
the DDR4 window, and the housekeeping peripherals outside the contract window.
Under Linux each block has ONE owner: the **kernel** (a driver binds it, no
UIO node) or **harnessd** (a `generic-uio` node; `mps3-harnessd` maps it by
physical base), never both (`LINUX_HARNESS_PLAN_2026-09-23.md` §3). Owners are
declared in `tools/gen_regmap.py` `MBV_OWNERS`; the generator refuses a block
without one. Everything else below is derived. The lane contract is
`docs/planning/linux_lanes/SHELL_CONTRACT.md`.

<!-- BEGIN GENERATED[regmap-mbv] — gen_regmap.py — DO NOT EDIT BY HAND -->
| Base | Size | Block | BD cell | Owner under Linux | INTC In | Consumer |
|---|---|---|---|---|---|---|
| 0x00000000 | 0x20000 | LMB | `(memory)` | **stage0+harnessd** |  | the MBV's own ILMB/DLMB (TDP BRAM local_ram); reset vector |
| 0x0001F000 | 0x1000 | LMB_TAIL | `local_ram` | **harnessd** |  | diag mailbox v8 at +0xF00; stage0 status block at +0xE00 (harnessd reads it, never writes) |
| 0x40600000 | 0x10000 | UARTLITE | `axi_uartlite_0` | **kernel** | 2 | console ttyUL0 -- xlnx,xps-uartlite-1.00.a |
| 0x41200000 | 0x10000 | INTC | `axi_intc_0` | **kernel** |  | root irqchip -- xlnx,xps-intc-1.00.a, kind-of-intr 0x0 |
| 0x41C00000 | 0x10000 | TIMER | `axi_timer_0` | **kernel** | 1 | clockevent -- soclabs,mbv-timer (errata E1/E2) + xlnx,xps-timer-1.00.a |
| 0x44A00000 | 0x10000 | CLKRST | `dut_clkrst_0` | **harnessd** |  | clkrst service |
| 0x44A10000 | 0x10000 | DFXCTL | `dfx_ctl_0` | **harnessd** |  | swap_fsm (decoupler clamp, RM verify) |
| 0x44A20000 | 0x10000 | HWICAP | `axi_hwicap_0` | **harnessd** | 0 | swap_fsm / config_agent (ICAP writes) |
| 0x44A30000 | 0x1000 | VPHY | `eth_mac_test_subsystem_0` | **harnessd** |  | coordinator link/macgen |
| 0x44A40000 | 0x10000 | USD | `usd_spi_0` | **kernel** |  | uSD -- spi-usd controller -> mmc_spi -> mmcblk0 (D13's usd_spi_0 at 0x44A4) |
| 0x44A50000 | 0x10000 | TELEM | `telem_0` | **harnessd** |  | coordinator; STATUS[0] = DDR4 calib in mbv (stage0 reads it first) |
| 0x44A60000 | 0x1000 | GENCHK | `eth_mac_test_subsystem_0` | **harnessd** |  | coordinator macgen |
| 0x44A70000 | 0x10000 | JTAGBB | `jtag_bb_0` | **harnessd** |  | jtag_server :6921 / xvc_server |
| 0x44A80000 | 0x10000 | DBGBR | `debug_bridge_0` | **harnessd** |  | xvc_server :2542 (debug_bridge) |
| 0x44A90000 | 0x10000 | UARTBR | `uart_bridge_0` | **harnessd** |  | uart_over_eth :6930-6932 |
| 0x44AA0000 | 0x10000 | GPIO | `board_gpio_0` | **harnessd** |  | heartbeat LED, switches |
| 0x44AB0000 | 0x10000 | MMCM_DRP | `clk_wiz_dut` | **harnessd** |  | clkrst set_clk (DUT MMCM DRP) |
| 0x44AC0000 | 0x10000 | CLCD | `clcd_0` | **harnessd** |  | clcd status/apps pages |
| 0x44AD0000 | 0x10000 | CLCDKVM | `clcd_kvm_0` | **harnessd** |  | clcd_kvm |
| 0x44AE0000 | 0x10000 | TOUCH | `touch_iic_0` | **harnessd** | 4 | touch (polls the STMPE811 over I2C; never the INTC) -- GATED: SHELL_TOUCH=1 |
| 0x44B20000 | 0x10000 | DUTEGR | `dut_egress_0` | **harnessd** |  | coordinator dutrx |
| 0x44B30000 | 0x10000 | USRACC | `usr_access_rd_0` | **harnessd** |  | coordinator version (usr_access) |
| 0x44B40000 | 0x10000 | WDOG | `axi_timebase_wdt_0` | **harnessd** |  | kicked from the harnessd service loop |
| 0x80000000 | 0x40000000 | DDR4 | `(memory)` | **kernel** |  | MIG ddr4_0 via smartconnect_ddr; == the I/D-cache aperture |
| 0xC0000000 | 0x1000000 | LAN9220 | `axi_emc_0` | **kernel** | 3 | eth0 -- smsc,lan9220 (native active-low IRQ, inverted in shell_top) |

CPU `microblaze_riscv_0`: rv32imac_zba_zbb_zbs, sv32, 100000000 Hz, reset vector 0x00000000. LMB layout: stage0 0x00000+0x1FE00 (STAGE0), stage0_status 0x1FE00+0x100 (STAGE0), diag_mailbox 0x1FF00+0x100 (HARNESSD). DDR4 calibration: TELEM CTRL (+0x00) bit 1 = 1, then STATUS (+0x10) bit 0. Machine-readable: `fpga/shell/generated/regmap_mbv.json`.
<!-- END GENERATED[regmap-mbv] -->

## Register offsets — the whole map, generated

Every register in every block, in one place. The per-block sections above
carry the *semantics* (bit fields, side effects, hazards) and are written by
hand; the offsets below are derived — from each block's own CSR RTL where
this tree owns it, and quoted from the Xilinx product guide where it does
not (the `Derived from` column says which).
`tests/firmware_logic/test_regmap_conformance.py` asserts that the
hand-written tables above agree with this one, so a semantic table cannot
drift away from the hardware while keeping its bit-field prose.

<!-- BEGIN GENERATED[regmap-offsets] — gen_regmap.py — DO NOT EDIT BY HAND -->
| Block | Register | Off | Address | Derived from |
|---|---|---|---|---|
| CLKRST | `RESET_CTRL` | 0x00 | `0x44A00000` | RTL `fpga/shell/ip/clkrst/dut_clkrst.sv` |
| CLKRST | `DUT_CLK_SEL` | 0x04 | `0x44A00004` | RTL `fpga/shell/ip/clkrst/dut_clkrst.sv` |
| CLKRST | `DUT_CLK_DRP` | 0x08 | `0x44A00008` | RTL `fpga/shell/ip/clkrst/dut_clkrst.sv` |
| CLKRST | `STATUS` | 0x0C | `0x44A0000C` | RTL `fpga/shell/ip/clkrst/dut_clkrst.sv` |
| DFXCTL | `DECOUPLE` | 0x00 | `0x44A10000` | RTL `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` |
| DFXCTL | `SHUTDOWN` | 0x04 | `0x44A10004` | RTL `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` |
| DFXCTL | `STATUS` | 0x08 | `0x44A10008` | RTL `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` |
| DFXCTL | `RM_ID` | 0x10 | `0x44A10010` | RTL `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` |
| DFXCTL | `RM_STATUS` | 0x14 | `0x44A10014` | RTL `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` |
| HWICAP | `GIER` | 0x1C | `0x44A2001C` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `ISR` | 0x20 | `0x44A20020` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `IER` | 0x28 | `0x44A20028` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `WF` | 0x100 | `0x44A20100` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `RF` | 0x104 | `0x44A20104` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `SZ` | 0x108 | `0x44A20108` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `CR` | 0x10C | `0x44A2010C` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `SR` | 0x110 | `0x44A20110` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `WFV` | 0x114 | `0x44A20114` | PG `Xilinx AXI HWICAP (PG134)` |
| HWICAP | `RFO` | 0x118 | `0x44A20118` | PG `Xilinx AXI HWICAP (PG134)` |
| VPHY | `PHY_STATE` | 0x00 | `0x44A30000` | RTL `fpga/ethernet/mdio_phy_model/mdio_phy_model.sv` |
| VPHY | `PHY_ID` | 0x04 | `0x44A30004` | RTL `fpga/ethernet/mdio_phy_model/mdio_phy_model.sv` |
| VPHY | `LINK_EVENT` | 0x08 | `0x44A30008` | RTL `fpga/ethernet/mdio_phy_model/mdio_phy_model.sv` |
| USD | `ID` | 0x00 | `0x44A40000` | RTL `fpga/shell/ip/usd_spi/usd_spi.sv` |
| USD | `CTRL` | 0x04 | `0x44A40004` | RTL `fpga/shell/ip/usd_spi/usd_spi.sv` |
| USD | `CLKDIV` | 0x08 | `0x44A40008` | RTL `fpga/shell/ip/usd_spi/usd_spi.sv` |
| USD | `DATA` | 0x0C | `0x44A4000C` | RTL `fpga/shell/ip/usd_spi/usd_spi.sv` |
| USD | `STATUS` | 0x10 | `0x44A40010` | RTL `fpga/shell/ip/usd_spi/usd_spi.sv` |
| TELEM | `CTRL` | 0x00 | `0x44A50000` | RTL `fpga/shell/ip/telem/telem.sv` |
| TELEM | `BUS_MV` | 0x04 | `0x44A50004` | RTL `fpga/shell/ip/telem/telem.sv` |
| TELEM | `CURR_UA` | 0x08 | `0x44A50008` | RTL `fpga/shell/ip/telem/telem.sv` |
| TELEM | `POWER_MW` | 0x0C | `0x44A5000C` | RTL `fpga/shell/ip/telem/telem.sv` |
| TELEM | `STATUS` | 0x10 | `0x44A50010` | RTL `fpga/shell/ip/telem/telem.sv` |
| GENCHK | `CTRL` | 0x00 | `0x44A60000` | RTL `fpga/ethernet/gen_checker/gen_checker.sv` |
| GENCHK | `INJECT` | 0x04 | `0x44A60004` | RTL `fpga/ethernet/gen_checker/gen_checker.sv` |
| GENCHK | `TX_CNT` | 0x08 | `0x44A60008` | RTL `fpga/ethernet/gen_checker/gen_checker.sv` |
| GENCHK | `RX_CNT` | 0x0C | `0x44A6000C` | RTL `fpga/ethernet/gen_checker/gen_checker.sv` |
| GENCHK | `ERR_CNT` | 0x10 | `0x44A60010` | RTL `fpga/ethernet/gen_checker/gen_checker.sv` |
| GENCHK | `DUT_IP` | 0x14 | `0x44A60014` | RTL `fpga/ethernet/gen_checker/gen_checker.sv` |
| GENCHK | `DUT_STATUS` | 0x18 | `0x44A60018` | RTL `fpga/ethernet/gen_checker/gen_checker.sv` |
| JTAGBB | `DRIVE` | 0x00 | `0x44A70000` | RTL `fpga/shell/ip/jtag_bb/jtag_bb.sv` |
| JTAGBB | `SAMPLE` | 0x04 | `0x44A70004` | RTL `fpga/shell/ip/jtag_bb/jtag_bb.sv` |
| SWDBB | `DRIVE` | 0x00 | `0x44A70000` | RTL `fpga/shell/ip/swd_bb/swd_bb.sv` |
| SWDBB | `SAMPLE` | 0x04 | `0x44A70004` | RTL `fpga/shell/ip/swd_bb/swd_bb.sv` |
| DBGBR | `LENGTH` | 0x00 | `0x44A80000` | PG `Xilinx Debug Bridge, AXI->BSCAN (PG245)` |
| DBGBR | `TMS` | 0x04 | `0x44A80004` | PG `Xilinx Debug Bridge, AXI->BSCAN (PG245)` |
| DBGBR | `TDI` | 0x08 | `0x44A80008` | PG `Xilinx Debug Bridge, AXI->BSCAN (PG245)` |
| DBGBR | `TDO` | 0x0C | `0x44A8000C` | PG `Xilinx Debug Bridge, AXI->BSCAN (PG245)` |
| DBGBR | `CTRL` | 0x10 | `0x44A80010` | PG `Xilinx Debug Bridge, AXI->BSCAN (PG245)` |
| UARTBR | `U0_TX` | 0x00 | `0x44A90000` | RTL `fpga/shell/ip/uart_bridge/uart_bridge.sv` |
| UARTBR | `U1_TX` | 0x08 | `0x44A90008` | RTL `fpga/shell/ip/uart_bridge/uart_bridge.sv` |
| UARTBR | `SWO_RX` | 0x10 | `0x44A90010` | RTL `fpga/shell/ip/uart_bridge/uart_bridge.sv` |
| UARTBR | `FIFO_STATUS` | 0x14 | `0x44A90014` | RTL `fpga/shell/ip/uart_bridge/uart_bridge.sv` |
| UARTBR | `SWO_CFG` | 0x18 | `0x44A90018` | RTL `fpga/shell/ip/uart_bridge/uart_bridge.sv` |
| GPIO | `IN` | 0x00 | `0x44AA0000` | RTL `fpga/shell/ip/board_gpio/board_gpio.sv` |
| GPIO | `OUT` | 0x04 | `0x44AA0004` | RTL `fpga/shell/ip/board_gpio/board_gpio.sv` |
| GPIO | `OE` | 0x08 | `0x44AA0008` | RTL `fpga/shell/ip/board_gpio/board_gpio.sv` |
| GPIO | `OWN` | 0x0C | `0x44AA000C` | RTL `fpga/shell/ip/board_gpio/board_gpio.sv` |
| MMCM_DRP | `SW_RESET` | 0x00 | `0x44AB0000` | PG `Xilinx Clocking Wizard AXI4-Lite DRP (PG065)` |
| MMCM_DRP | `STATUS` | 0x04 | `0x44AB0004` | PG `Xilinx Clocking Wizard AXI4-Lite DRP (PG065)` |
| MMCM_DRP | `CFG_REG0` | 0x200 | `0x44AB0200` | PG `Xilinx Clocking Wizard AXI4-Lite DRP (PG065)` |
| MMCM_DRP | `CFG_REG2` | 0x208 | `0x44AB0208` | PG `Xilinx Clocking Wizard AXI4-Lite DRP (PG065)` |
| MMCM_DRP | `LOAD` | 0x25C | `0x44AB025C` | PG `Xilinx Clocking Wizard AXI4-Lite DRP (PG065)` |
| CLCD | `CTRL` | 0x00 | `0x44AC0000` | RTL `fpga/shell/ip/clcd/clcd.sv` |
| CLCD | `CMD` | 0x04 | `0x44AC0004` | RTL `fpga/shell/ip/clcd/clcd.sv` |
| CLCD | `DATA` | 0x08 | `0x44AC0008` | RTL `fpga/shell/ip/clcd/clcd.sv` |
| CLCD | `STATUS` | 0x0C | `0x44AC000C` | RTL `fpga/shell/ip/clcd/clcd.sv` |
| CLCD | `READ` | 0x10 | `0x44AC0010` | RTL `fpga/shell/ip/clcd/clcd.sv` |
| CLCD | `TIMING` | 0x14 | `0x44AC0014` | RTL `fpga/shell/ip/clcd/clcd.sv` |
| CLCDKVM | `CTRL` | 0x00 | `0x44AD0000` | RTL `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` |
| CLCDKVM | `STATUS` | 0x04 | `0x44AD0004` | RTL `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` |
| CLCDKVM | `EVENT` | 0x08 | `0x44AD0008` | RTL `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` |
| CLCDKVM | `PANEL_TMR` | 0x0C | `0x44AD000C` | RTL `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` |
| CLCDKVM | `TIMEOUT` | 0x10 | `0x44AD0010` | RTL `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` |
| CLCDKVM | `DEBOUNCE` | 0x14 | `0x44AD0014` | RTL `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` |
| CLCDKVM | `TUNNEL` | 0x18 | `0x44AD0018` | RTL `fpga/shell/ip/clcd_kvm/clcd_kvm.sv` |
| TOUCH | `IIC_GIE` | 0x1C | `0x44AE001C` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_ISR` | 0x20 | `0x44AE0020` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_IER` | 0x28 | `0x44AE0028` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_SOFTR` | 0x40 | `0x44AE0040` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_CR` | 0x100 | `0x44AE0100` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_SR` | 0x104 | `0x44AE0104` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_TX_FIFO` | 0x108 | `0x44AE0108` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_RX_FIFO` | 0x10C | `0x44AE010C` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_ADR` | 0x110 | `0x44AE0110` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_TX_FIFO_OCY` | 0x114 | `0x44AE0114` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_RX_FIFO_OCY` | 0x118 | `0x44AE0118` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_TEN_ADR` | 0x11C | `0x44AE011C` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_RX_FIFO_PIRQ` | 0x120 | `0x44AE0120` | PG `Xilinx AXI IIC (PG090)` |
| TOUCH | `IIC_GPO` | 0x124 | `0x44AE0124` | PG `Xilinx AXI IIC (PG090)` |
| DUTEGR | `CTRL` | 0x00 | `0x44B20000` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `STATUS` | 0x04 | `0x44B20004` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `LEVEL` | 0x08 | `0x44B20008` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `FRAME_LEN` | 0x0C | `0x44B2000C` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `DATA` | 0x10 | `0x44B20010` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `RX_FRAMES` | 0x14 | `0x44B20014` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `DROP_FULL` | 0x18 | `0x44B20018` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `DROP_GIANT` | 0x1C | `0x44B2001C` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `TX_CTRL` | 0x20 | `0x44B20020` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `TX_STATUS` | 0x24 | `0x44B20024` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `TX_SPACE` | 0x28 | `0x44B20028` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `TX_DATA` | 0x2C | `0x44B2002C` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `TX_FRAMES` | 0x30 | `0x44B20030` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `TX_REJECT` | 0x34 | `0x44B20034` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| DUTEGR | `TX_FLUSHED` | 0x38 | `0x44B20038` | RTL `fpga/shell/ip/dut_egress/dut_egress.sv` |
| USRACC | `MAGIC` | 0x00 | `0x44B30000` | RTL `fpga/shell/ip/usr_access_rd/usr_access_rd.sv` |
| USRACC | `VALUE` | 0x04 | `0x44B30004` | RTL `fpga/shell/ip/usr_access_rd/usr_access_rd.sv` |
| USRACC | `STATUS` | 0x08 | `0x44B30008` | RTL `fpga/shell/ip/usr_access_rd/usr_access_rd.sv` |
| WDOG | `TWCSR0` | 0x00 | `0x44B40000` | PG `Xilinx AXI Timebase Watchdog Timer (PG128)` |
| WDOG | `TWCSR1` | 0x04 | `0x44B40004` | PG `Xilinx AXI Timebase Watchdog Timer (PG128)` |
| WDOG | `TBR` | 0x08 | `0x44B40008` | PG `Xilinx AXI Timebase Watchdog Timer (PG128)` |

19 blocks, 110 registers. 14 blocks' offsets are parsed out of RTL in this tree; the rest are quoted from Xilinx product guides and are marked `PG` above.
<!-- END GENERATED[regmap-offsets] -->

## RM-load verify
After each partial load the coordinator reads `DFXCTL.RM_ID` (0x44A1_0010, the
`rm_id` partition pin) + `RM_STATUS.rm_id_valid` + optional DFX Bitstream
Monitor + config CRC, and reports the confirmed id to the host
(`net-protocol.md` control channel; also the `rp` truth for the Hub's
two-level `FpgaStatus`, `HARDWARE_HUB_INTEGRATION.md` (internal note, not in the public tree) §3).
