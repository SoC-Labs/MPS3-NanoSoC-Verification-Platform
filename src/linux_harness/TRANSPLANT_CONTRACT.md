# TRANSPLANT CONTRACT — MPS3 static shell → Linux-capable CPU successor

**Status:** authoritative audit of the shell block design, 2026-07-15.
**Audited sources (ground truth, in precedence order):**

1. `~/SoCLabs/mps3-nanosoc-platform/fpga/shell/bd/shell_bd.tcl` (1313 lines, working tree on branch `feat/clcd-kvm-display`)
2. `~/SoCLabs/mps3-nanosoc-platform/fpga/shell/build_results_2026-07-11/bd_summary.txt` (last `validate_bd_design PASSED` build — the **board-proven** baseline, `static_id 0xE4B1C44A`)
3. `~/SoCLabs/mps3-nanosoc-platform/docs/contracts/shell-regmap.md` v0.5, `partition-pins.md` v0.2
4. `~/SoCLabs/mps3-nanosoc-platform/fpga/shell/{build_shell.tcl,shell_top.sv}`, `fpga/dfx/{dfx_floorplan.xdc,build_dfx.tcl}`
5. `~/SoCLabs/mps3-nanosoc-platform/firmware/README.md` (MicroBlaze superloop, lwIP 2.2.0 RAW)
6. Proven MBV+DDR4 recipe: `~/SoCLabs/mps3-nanosoc-platform-linux/src/linux_soc/hw/mbv_soc.tcl`

> **BASELINE WARNING (read first).** The working-tree `shell_bd.tcl` is **ahead of
> the board-proven shell**. The last built/validated BD (2026-07-11,
> `static_id 0xE4B1C44A`) has `NUM_MI = 15`, **no `clcd_kvm_0`**, and a
> 15-interface decoupler. The working tree adds Wave 4 (CLCD-KVM: `clcd_kvm_0`
> @ `0x44AD_0000`, `user_npb1` port, `NUM_MI = 16`) and the 2026-07-15 QSPI
> boundary freeze (partition-pins v0.2: +14 QSPI partition bits, decoupler
> interfaces 15–18, `qspi_pad_*` ports, OVLSTORE `SPI_0` external port
> removed). **Neither has been through `build_shell.tcl` nor a board.** A CPU
> transplant must state explicitly which baseline it forks; this contract
> documents the working-tree BD (41 cells) and flags every delta.

---

## 1. Complete cell inventory (41 cells in the working-tree BD)

The 2026-07-11 built baseline has 40 of these (all except `clcd_kvm_0`).
"MB-coupled" = touched by the classic-MicroBlaze subsystem and therefore in
the transplant blast radius (§9).

| # | Cell | VLNV | Role | MB-coupled |
|---|---|---|---|---|
| 1 | `microblaze_0` | `xilinx.com:ip:microblaze:11.0` | Bare-metal coordinator. `C_USE_MMU 0`, barrel+mul+div, no FPU, no caches (`C_ICACHE/DCACHE_ALWAYS_USED 0`), `C_DEBUG_ENABLED 1`, `C_D_AXI 1` (M_AXI_DP), `C_I_LMB/C_D_LMB 1` | **REPLACE** |
| 2 | `ilmb_v10` | `xilinx.com:ip:lmb_v10:3.0` | Instruction LMB | **REMOVE** |
| 3 | `dlmb_v10` | `xilinx.com:ip:lmb_v10:3.0` | Data LMB | **REMOVE** |
| 4 | `ilmb_bram_if_cntlr` | `xilinx.com:ip:lmb_bram_if_cntlr:4.0` | ILMB→BRAM, `C_ECC 0` | **REMOVE** |
| 5 | `dlmb_bram_if_cntlr` | `xilinx.com:ip:lmb_bram_if_cntlr:4.0` | DLMB→BRAM, `C_ECC 0` | **REMOVE** |
| 6 | `local_ram` | `xilinx.com:ip:blk_mem_gen:8.4` | True-dual-port 262144×32 = **1 MiB** shared I/D BRAM (~256 RAMB36E2, ~11.9% of KU115). Sized for `core + 2× clearing` (nanosoc clearing = 155,864 B) | **REMOVE/REPURPOSE** |
| 7 | `mdm_1` | `xilinx.com:ip:mdm:3.2` | MicroBlaze Debug Module, JTAG/BSCAN → `MBDEBUG_0`. No external clk/rst (bus-internal, confirmed live) | **REPLACE** (`mdm_riscv` for MBV) |
| 8 | `axi_intc_0` | `xilinx.com:ip:axi_intc:4.1` | Interrupt controller; `irq` → `microblaze_0/INTERRUPT` | **RE-WIRE irq** |
| 9 | `xlconcat_intr` | `xilinx.com:ip:xlconcat:2.1` | `NUM_PORTS 4` IRQ concat (§4) | keep |
| 10 | `axi_timer_0` | `xilinx.com:ip:axi_timer:2.0` | Housekeeping timer | keep |
| 11 | `axi_uartlite_0` | `xilinx.com:ip:axi_uartlite:2.0` | Physical console fallback, 115200; pads `uart_tx_f`/`uart_rx_f` | keep |
| 12 | `clk_wiz_shell` | `xilinx.com:ip:clk_wiz:6.0` | 50 MHz in → 100 MHz shell + 50 MHz RMII ref (§5). `USE_LOCKED`, `USE_RESET` ACTIVE_LOW | keep (see §9.3) |
| 13 | `clk_wiz_dut` | `xilinx.com:ip:clk_wiz:6.0` | DUT MMCM, `USE_DYN_RECONFIG 1` → **AXI4-Lite-only DRP** (`s_axi_lite`, no native DRP pins, no scalar reset in 2024.1 — confirmed live). Default 50 MHz | **must NOT move** |
| 14 | `proc_sys_reset_shell` | `xilinx.com:ip:proc_sys_reset:5.0` | Shell-domain reset generator (§6) | keep |
| 15 | `dut_clkrst_0` | `soclabs.org:user:dut_clkrst:1.0` | CLKRST CSR @0x44A0_0000; generates the 3 DUT-domain resets | **must NOT move** |
| 16 | `dfx_ctl_0` | `soclabs.org:user:dfx_ctl:1.0` | DFXCTL CSR @0x44A1_0000; decouple/shutdown/rm_id-verify | **must NOT move** |
| 17 | `board_gpio_0` | `soclabs.org:user:board_gpio:1.0` | GPIO CSR @0x44AA_0000; DUT↔pad passthrough + host mux | **must NOT move** |
| 18 | `jtag_bb_0` | `soclabs.org:user:jtag_bb:1.0` | JTAGBB CSR @0x44A7_0000; JTAG pin-wiggler ([DEV-10]; was `swd_bb_0`/SWDBB at the same page) | **must NOT move** |
| 19 | `telem_0` | `soclabs.org:user:telem:1.0` | TELEM CSR @0x44A5_0000; INA228 seam tied off (`SIM_FAKE_DATA 0`) | keep |
| 20 | `uart_bridge_0` | `soclabs.org:user:uart_bridge:1.0` | UARTBR CSR @0x44A9_0000; DUT console/SWO async FIFOs (owns the dut_clk⇄shell_clk CDC) | **must NOT move** |
| 21 | `clcd_0` | `soclabs.org:user:clcd:1.0` | CLCD CSR @0x44AC_0000; HX8347-D 8080 master (panel LIT on silicon 2026-07-14) | keep |
| 22 | `clcd_kvm_0` | `soclabs.org:user:clcd_kvm:1.0` | **Wave 4, NOT in the built baseline.** Panel arbiter @0x44AD_0000, `USER_nPB1` hardware toggle, DFX forced-revert | keep (unproven) |
| 23 | `dfx_decoupler_0` | `xilinx.com:ip:dfx_decoupler:1.0` | Per-signal RP boundary clamp — **19 interfaces (IDs 0–18)** in the working tree; 15 in the built baseline (§7). NB: the BD's own prose still says "15 interfaces"; the `ALL_PARAMS` dict is authoritative | **must NOT move** |
| 24 | `dfx_axi_shutdown_manager_0` | `xilinx.com:ip:dfx_axi_shutdown_manager:1.0` | Sequencing real (`request_shutdown`/`shutdown_requested` ↔ dfx_ctl); `S_AXI`/`M_AXI` intentionally unconnected (reserved v1+ MMIO-bridge slot) | **must NOT move** |
| 25 | `axi_hwicap_0` | `xilinx.com:ip:axi_hwicap:3.0` | HWICAP @0x44A2_0000 → ICAPE3. `C_ICAP_DWIDTH 32`, **`C_MODE 0` = FIFO mode**, `C_WRITE_FIFO_DEPTH 1024` (4 KiB). `icap_clk` = shell_clk. Firmware pairs with `HWICAP_FIFO=1` | **must NOT move** |
| 26 | `axi_quad_spi_0` | `xilinx.com:ip:axi_quad_spi:3.2` | OVLSTORE @0x44A4_0000. `C_USE_STARTUP 0`, `C_SPI_MODE 0` (standard), `C_SPI_MEMORY 0`, 1 SS. Working tree: **`SPI_0` external port REMOVED** (RP owns the SST26 pads, D16); register window kept for compatibility only | keep (vestigial) |
| 27 | `debug_bridge_0` | `xilinx.com:ip:debug_bridge:3.0` | DBGBR @0x44A8_0000, `C_DEBUG_MODE 2` = AXI→BSCAN (§8) | **must NOT move** |
| 28 | `axi_emc_0` | `xilinx.com:ip:axi_emc:3.0` | LAN9220 host I/F @0xC000_0000/16 MiB. `C_MEM0_TYPE 1` (async SRAM), **`C_MEM0_WIDTH 16`**, AC timing TCEDV/TAVDV 50 ns, THZCE/THZOE 25 ns, TWC 100 ns/TWP 50 ns (deliberately slow; defaults were a silent under-sample of the LAN9220's 30 ns spec). Extra `rdclk` pin = shell_clk | **must NOT move** |
| 29 | `axi_interconnect_0` | `xilinx.com:ip:axi_interconnect:2.1` | `NUM_MI 16` (working tree; 15 built). S00 = `microblaze_0/M_AXI_DP`. Per-master `M<NN>_ACLK/_ARESETN` all shell_clk/shell_aresetn | **RE-WIRE S00** |
| 30–39 | `gnd_drp_do`(w16), `gnd_drp_drdy`, `gnd_dbg_req`, `gnd_uart1_tx`(w8), `gnd_uart1_v`, `vcc_uart1_rdy`, `gnd_ina228_32`(w32), `gnd_ina228_1`, `gnd_rmii2`(w2), `gnd_rmii1` | `xilinx.com:ip:xlconstant:1.1` | Tie-offs: dut_clkrst DRP inputs, dbg_reset_req, UART1 idle, TELEM INA228 seam, deferred RMII RX group | keep |
| 40 | `inv_rp_in_reset` | `xilinx.com:ip:util_vector_logic:2.0` | `not(rp_resetn_o)` → `dfx_ctl_0/rp_in_reset_i` (STATUS[1]) | **must NOT move** |
| 41 | (top) `u_rp_dut` | `fpga/shell/rp_dut_stub.sv` / DFX black box | **Not a BD cell** — sibling of the BD wrapper in `shell_top.sv` (`u_top = u_shell + u_rp_dut`, I4 topology). DONT_TOUCH; DFX build passes `rp_inst=u_rp_dut` | **must NOT move** |

---

## 2. Address map — what the BD INSTANTIATES

**Decode-width lesson (normative):** every soclabs CSR block is instantiated
with `CONFIG.C_S_AXI_ADDR_WIDTH {32}` — the RTL default of 12 **never ships**.
Any successor BD must re-apply `C_S_AXI_ADDR_WIDTH {32}` on all eight custom
cells, and benches must drive `base + offset` at width 32
(`tests/csr_decode_width/` is the regression; the width-12 assumption killed
every CSR on silicon once already).

All confirmed against the built `bd_summary.txt` (2026-07-11) except the last
row. Interconnect master index (M) from `shell_bd.tcl`'s `mi_map`.

| Base | Range | M | Block | Cell / segment | Slave intf (real, case-sensitive) |
|---|---|---|---|---|---|
| `0x0000_0000` | 1 MiB | ILMB | MB instruction RAM | `ilmb_bram_if_cntlr/SLMB/Mem` | LMB (not AXI) |
| `0x0000_0000` | 1 MiB | DLMB | MB data RAM (same physical BRAM) | `dlmb_bram_if_cntlr/SLMB/Mem` | LMB (not AXI) |
| `0x4060_0000` | 64 KiB | M10 | UARTLITE (console fallback) | `axi_uartlite_0/S_AXI/Reg` | `S_AXI` |
| `0x4120_0000` | 64 KiB | M11 | INTC | `axi_intc_0/S_AXI/Reg` | `S_AXI` |
| `0x41C0_0000` | 64 KiB | M09 | TIMER | `axi_timer_0/S_AXI/Reg` | `S_AXI` |
| `0x44A0_0000` | 64 KiB | M00 | CLKRST | `dut_clkrst_0/s_axi/reg0` | `s_axi` (lowercase — IP-XACT auto-infer) |
| `0x44A1_0000` | 64 KiB | M01 | DFXCTL | `dfx_ctl_0/s_axi/reg0` | `s_axi` |
| `0x44A2_0000` | 64 KiB | M02 | HWICAP | `axi_hwicap_0/S_AXI_LITE/Reg` | `S_AXI_LITE` |
| `0x44A3_0000` | 64 KiB | — | VPHY — **RESERVED, not instantiated** (deferred ethernet wave) | — | — |
| `0x44A4_0000` | 64 KiB | M03 | OVLSTORE (register window only; SPI master drives nothing in working tree) | `axi_quad_spi_0/AXI_LITE/Reg` | `AXI_LITE` |
| `0x44A5_0000` | 64 KiB | M04 | TELEM | `telem_0/s_axi/reg0` | `s_axi` |
| `0x44A6_0000` | 64 KiB | — | GENCHK — **RESERVED, not instantiated** | — | — |
| `0x44A7_0000` | 64 KiB | M05 | JTAGBB | `jtag_bb_0/s_axi/reg0` | `s_axi` |
| `0x44A8_0000` | 64 KiB | M06 | DBGBR (XVC) | `debug_bridge_0/S_AXI/Reg0` | `S_AXI`, segment `Reg0` |
| `0x44A9_0000` | 64 KiB | M07 | UARTBR | `uart_bridge_0/s_axi/reg0` | `s_axi` |
| `0x44AA_0000` | 64 KiB | M08 | GPIO | `board_gpio_0/s_axi/reg0` | `s_axi` |
| `0x44AB_0000` | 64 KiB | M13 | MMCM_DUT_DRP (clk_wiz_dut AXI-Lite DRP — the ONLY arbitrary DUT-clock reconfig path) | `clk_wiz_dut/s_axi_lite/Reg` | `s_axi_lite` |
| `0x44AC_0000` | 64 KiB | M14 | CLCD (LIVE on silicon) | `clcd_0/s_axi/reg0` | `s_axi` |
| `0x44AD_0000` | 64 KiB | M15 | CLCDKVM — **working tree only; unmapped (DECERR, un-trapped) on the shipped shell** | `clcd_kvm_0/s_axi/reg0` | `s_axi` |
| `0xC000_0000` | **16 MiB** | M12 | LAN9220 via AXI EMC (16-bit data) | `axi_emc_0/S_AXI_MEM/*` (assigned with `-quiet`; segment name CONFIG-dependent) | `S_AXI_MEM` |

Register-level field maps for every block: `docs/contracts/shell-regmap.md`
v0.5 (normative; key traps: UARTBR reads are destructive; DFXCTL `0x20` had a
read-side-effect console-pop alias; CLCD/CLCDKVM are deliberately free of read
side effects; CLCDKVM `EVENT` is W1C).

For the transplant: the MBV recipe adds `0x8000_0000..0xBFFF_FFFF` (1 GiB DDR4)
— no collision with any window above. Keep every existing base identical:
`firmware/common/platform_regs.h` and all host tooling bake these addresses.

---

## 3. AXI fabric

- Single `axi_interconnect:2.1`, one slave port `S00_AXI` ← `microblaze_0/M_AXI_DP`
  (AXI4-Lite, 32-bit). 16 master ports (working tree), every `M<NN>_ACLK` =
  shell_clk, every `M<NN>_ARESETN` = `peripheral_aresetn`; the interconnect
  core `ACLK/ARESETN` = shell_clk / `interconnect_aresetn`.
- Single clock domain across the whole fabric — no CDC inside the interconnect.
- Two AXI-Lite slave handshake styles are contract-blessed (Xilinx-template
  ready-pulse AND accept-on-valid); masters must sample ready and response
  concurrently (shell-regmap.md "AXI4-Lite slave conventions").
- No shell↔DUT AXI exists in v0. The pre-wired quiesce slot for a future MMIO
  bridge is `dfx_axi_shutdown_manager_0`'s unconnected `S_AXI`/`M_AXI`.

## 4. IRQ map — complete

`xlconcat_intr` (4 ports) → `axi_intc_0/intr`; `axi_intc_0/irq` →
`microblaze_0/INTERRUPT`.

| INTC bit | Source | Note |
|---|---|---|
| In0 | `axi_hwicap_0/ip2intc_irpt` | wired `-quiet` |
| In1 | `axi_timer_0/interrupt` | |
| In2 | `axi_uartlite_0/interrupt` | |
| In3 | BD port `eth_irq` | LAN9220 IRQ pad via shell_top |

**`rp_irq_out` is deliberately NOT an interrupt.** It terminates at the
decoupler (`rp_irq_out_DATA`, clamp 0); `s_irq_out_DATA` is an explicit
documented dangling tie-off. Reasons recorded in the BD: (1) it is dut_clk
domain — raw wiring into the shell-clk INTC would be an unsynchronized CDC;
(2) decoupler `s_*_DATA` carries a data_rtl intf type that xlconcat rejects
([xlconcat-10], confirmed live); (3) no firmware consumer. Future recipe: 2-FF
ASYNC_REG sync + regmap entry + spare INTC line.

The firmware is a polling superloop; the INTC exists but nothing depends on
low interrupt latency. A Linux transplant will care: In0–In3 must be
re-plumbed to the RISC-V PLIC-equivalent (axi_intc in MBV mode) with the same
sources.

## 5. Clock tree — every domain

| Clock | Source | Freq | Consumers |
|---|---|---|---|
| `OSCCLK1` → `osc_clk_50m` | Board oscillator → `IBUF u_osc_ibuf` + `BUFG u_osc_bufg` in `shell_top.sv` (NOT in the BD; both clk_wiz are `PRIM_SOURCE No_buffer`) | 50 MHz | `clk_wiz_shell/clk_in1`, `clk_wiz_dut/clk_in1`. **BD port created `-freq_hz 50000000` — load-bearing:** a default 100 MHz port FREQ_HZ overrides `PRIM_IN_FREQ` and drives FVCO to an illegal 2000 MHz ("IO Clock Placer failed") |
| `shell_clk` | `clk_wiz_shell/clk_out1` | **100 MHz** | MicroBlaze, LMB, all 16 interconnect masters, all CSR `s_axi_aclk`, `axi_hwicap` `s_axi_aclk`+`icap_clk`, `axi_emc` `s_axi_aclk`+`rdclk`, `axi_quad_spi` `s_axi_aclk`+`ext_spi_clk`, shutdown mgr `clk`, `clk_wiz_dut/s_axi_aclk`, `proc_sys_reset_shell/slowest_sync_clk`, CLCD/KVM timing (1 µs tick `TICK_DIV=100`) |
| `rmii_ref_clk` | `clk_wiz_shell/clk_out2` | **50 MHz fixed** (never DRP'd) | BD port `rp_phy_rmii_ref_clk` → RP (DUT MAC ref-in). Shell sources RMII REF per spec §8.3 |
| `dut_clk` | `clk_wiz_dut/clk_out1` | **50 MHz default, DRP-reconfigurable** via AXI-Lite @0x44AB_0000 only (2024.1 clk_wiz exposes no native DRP pins once `USE_DYN_RECONFIG` is set — confirmed live). D12 (real target freq) still open | BD port `rp_dut_clk` → RP; `dut_clkrst_0/dut_clk_i` (heartbeat/reset sync); `uart_bridge_0/dut_clk_i` (CDC FIFO far side) |

CDC inventory (all shell-side, per the partition contract rule):
- `uart_bridge_0`: gray-pointer async FIFOs for all 5 console/SWO streams (dut_clk ⇄ shell_clk).
- `dfx_ctl_0`: 2-FF ASYNC_REG syncs on `rm_id`/`dut_lockup`.
- `clcd_kvm_0`: 3-FF synchronizer + debounce on `user_npb1`; sync+stability filter on the dut_gpio tunnel.
- **The one sanctioned NON-CDC exception:** QSPI XiP group (partition-pins v0.2) — matched source-synchronous passthrough; the decoupler clamp on it is combinational, never a synchronizer. Do not "fix" this by adding syncs.

## 6. Reset tree — every reset, polarity, driver

| Reset | Polarity | Driven by | Consumers |
|---|---|---|---|
| `USER_nPB0` → `sys_rst_n` | active-LOW | Board push-button = system POR (`shell_top.sv:117`; raw, undebounced) | `clk_wiz_shell/resetn`, `proc_sys_reset_shell/ext_reset_in`, `dut_clkrst_0/ext_por_n_i` |
| `mb_reset` | **active-HIGH** | `proc_sys_reset_shell` (waits on `dcm_locked` = clk_wiz_shell lock) | `microblaze_0/Reset`, and `SYS_Rst` + `LMB_Rst` on both `lmb_v10` and both `lmb_bram_if_cntlr` (**confirmed live:** feeding the LMBs `peripheral_aresetn` is a real [BD 41-238] polarity bug; mb_reset is the standard pairing) |
| `peripheral_aresetn` (`shell_aresetn`) | active-LOW | `proc_sys_reset_shell` | every AXI-Lite slave `s_axi_aresetn` (incl. `clk_wiz_dut` — its ONLY reset; no scalar pin exists), shutdown mgr `resetn`, all interconnect `M<NN>_ARESETN` + `S00_ARESETN` |
| `interconnect_aresetn` (`shell_bus_arstn`) | active-LOW | `proc_sys_reset_shell` | `axi_interconnect_0/ARESETN` |
| `rp_dut_resetn` | active-LOW, async-assert/sync-deassert (dut_clk) | `dut_clkrst_0/dut_resetn_o` (host reg CLKRST.RESET_CTRL[0]) | RP (DUT system reset) |
| `rp_rp_resetn` | active-LOW, async/sync | `dut_clkrst_0/rp_resetn_o`, gated by `dfx_ctl_0/rp_resetn_gate_o` → held through every swap. **This held-low reset IS the input-side isolation** (static→RP pins are not decoupled) | RP; inverted copy → `dfx_ctl_0/rp_in_reset_i` (STATUS[1]); raw copy → `clcd_kvm_0/rp_resetn` (forced-revert interlock) |
| `rp_dbg_resetn` | active-LOW, async/sync | `dut_clkrst_0/dbg_resetn_o` (CLKRST.RESET_CTRL[2], OpenOCD srst — pure software path; `dbg_reset_req_i` tied 0) | RP (debug SRST) |

There is **no** `proc_sys_reset` for the DUT domain — `dut_clkrst_0` generates
its three resets internally. The decoupler has no reset pin at all.

## 7. DFX decoupler boundary + RP partition pins

Topology (I4, settled): the BD wrapper (`u_shell`) and `u_rp_dut` are
**siblings inside `shell_top.sv`** (`u_top`). The entire partition boundary is
BD **ports** on `u_shell`, wired 1:1 by name to `u_rp_dut`'s pins.
`HD.RECONFIGURABLE` + the pblock are applied ONLY by `fpga/dfx/build_dfx.tcl`
(with `rp_inst=u_rp_dut`), never by the shell build.

`dfx_decoupler_0` — `CONFIG.ALL_PARAMS` authored directly in Tcl (schema
discovered live and recorded in the BD; every value must be a `0x` hex STRING
— a bare integer passes validate and fails at IP generation an hour into the
build). All interfaces `MODE master` (RP drives), `MANAGEMENT manual`,
single-signal `data_rtl` groups (AXIS rejected — [BD 41-1306] vs discrete
pins). Pins per interface: `rp_<intf>_DATA` (in from RP port) →
`s_<intf>_DATA` (out to static sink, clamped to `DECOUPLED_VALUE` while
`decouple=1`).

**RP→static (decoupled), 19 interfaces (IDs 0–18 working tree; 0–14 built baseline):**

| ID | Interface | W | Clamp | Static sink | Safe-idle rationale |
|---|---|---|---|---|---|
| 0 | `jtag_tdo` | 1 | 0 | `jtag_bb_0/jtag_tdo_i` | TDO read-back idle line during swap |
| 1 | `phy_rmii_txd` | 2 | 0 | *dangling* (ethernet wave) | idle nibbles |
| 2 | `phy_rmii_tx_en` | 1 | 0 | *dangling* | CRITICAL — no runaway frame |
| 3 | `mdc` | 1 | 0 | *dangling* | park MDIO clock |
| 4 | `mdio_o` | 1 | 0 | *dangling* | idle |
| 5 | `mdio_oe` | 1 | 0 | *dangling* | CRITICAL — tri-state, no bus fight |
| 6 | `uart_tx_tdata` | 8 | 0 | `uart_bridge_0/uart_tx_tdata_i` | don't-care w/ tvalid=0 |
| 7 | `uart_tx_tvalid` | 1 | 0 | `uart_bridge_0/uart_tx_tvalid_i` | CRITICAL — no FIFO flood |
| 8 | `uart_rx_tready` | 1 | **0** | `uart_bridge_0/uart_rx_tready_i` | CRITICAL — clamp to NOT-ready: pause, don't drain/lose RX |
| 9 | `swo` | 1 | 0 | `uart_bridge_0/swo_i` | trace idle |
| 10 | `rm_id` | 32 | 0 | `dfx_ctl_0/rm_id_i` | THE POINT — post-swap RM_ID read is genuine, not fed back |
| 11 | `dut_lockup` | 1 | 0 | `dfx_ctl_0/dut_lockup_i` | no false lockup mid-swap |
| 12 | `irq_out` | 1 | 0 | *documented dangling tie-off* (§4) | no IRQ storm |
| 13 | `dut_gpio_o` | 16 | 0 | `board_gpio_0/dut_gpio_o_i` **+ fan-out tap** `clcd_kvm_0/dut_gpio_o_i` (display tunnel, upper 8 bits; strobes encoded active-high so clamp-0 = all-idle by construction) | pad drive 0 |
| 14 | `dut_gpio_oe` | 16 | 0 | `board_gpio_0/dut_gpio_oe_i` + `clcd_kvm_0/dut_gpio_oe_i` | CRITICAL — pads high-Z during swap |
| 15 | `qspi_sclk` | 1 | 0 | BD port `qspi_pad_sclk` → shell_top IOBUF | v0.2 only |
| 16 | `qspi_csn` | 1 | **1** | BD port `qspi_pad_csn` | **the one non-zero clamp** — flash DESELECTED during swap |
| 17 | `qspi_io_o` | 4 | 0 | BD port `qspi_pad_io_o` | v0.2 only |
| 18 | `qspi_io_oe` | 4 | 0 | BD port `qspi_pad_io_oe` | v0.2 only |

**Static→RP (NOT decoupled — pass straight through; `rp_resetn` held low is
the isolation):** `dut_clk`, `phy_rmii_ref_clk` (clocks never go through a
decoupler); `dut_resetn/rp_resetn/dbg_resetn`; `jtag_tck/jtag_tms/jtag_tdi`
(from jtag_bb — a JTAG drive to a dead RP is harmless); `phy_rmii_crs_dv/rxd`, `mdio_i` (tied 0 via `gnd_rmii*` until
the ethernet wave); `uart_rx_tdata/tvalid`, `uart_tx_tready` (from
uart_bridge); `dut_gpio_i` (from board_gpio); `qspi_io_i` (v0.2 — not even a
BD port: straight `shell_top` pad-to-RP passthrough, never clamped, non-CDC).

Handshake: `dfx_ctl_0/decouple_en_o` → `decouple`; `decouple_status` →
`dfx_ctl_0/decoupled_i` **and** `clcd_kvm_0/decouple_status` (forced revert).
Shutdown mgr: `dfx_ctl_0/axi_shutdown_req_o` → `request_shutdown`;
`shutdown_requested` → `axi_shutdown_ack_i`.

Boundary change law: widening the partition boundary or ANY static-side BD
change **re-mints `static_id` and re-keys/rebuilds all 8 RM overlays** (proved
twice: `0x14E1A2D8` → `0xE4B1C44A` at CLCD; the Wave-4/QSPI tree will re-mint
again). A CPU transplant is a maximal static change: every RM partial must be
re-implemented against the new static routed DCP.

## 8. Debug paths (two distinct ones)

1. **XVC-over-Ethernet (soft, host-facing):** TCP :2542 (`xvc_server`
   firmware) → AXI-Lite DBGBR @0x44A8_0000 → `debug_bridge_0`
   (`C_DEBUG_MODE 2`, AXI→BSCAN, internally bonds a BSCAN primitive — no
   external ports) → ILA/VIO chains. Must stay in static (survives swaps).
2. **MDM (hard JTAG, MicroBlaze-facing):** board JTAG → BSCAN → `mdm_1` →
   `MBDEBUG_0` → `microblaze_0/DEBUG`. Also the path for the LMB-resident
   JTAG **diag mailbox** at `0xFFF80` (top of the 1 MiB LMB, magic
   `0xD1A6C0DE`, scanned by `scripts/mps3_diag.tcl` — dies with the LMB in a
   transplant; see §9.6).
3. **DUT debug is neither of these:** JTAG bit-bang via the JTAGBB CSR over the
   `rp_jtag_*` partition pins ([DEV-10]; was SWD over `rp_swd_*` on :6920).
   The bare-metal service is `firmware/jtag_server` on :6921; the Linux twin is
   NOT written yet — see docs/planning/LINUX_FORK_JTAG_MIGRATION.md §3.

MBV note from the proven recipe: `mdm_riscv` replaces `mdm_1`, and
`proc_sys_reset/mb_debug_sys_rst` is deliberately left unconnected.

## 9. THE TRANSPLANT — exact edit list

Goal shape (per the proven `mbv_soc.tcl`): MicroBlaze V (rv32imac, Sv32 MMU),
DDR4 MIG 1 GiB @ `0x8000_0000`, Linux replacing the bare-metal superloop.

### 9.1 Cells to remove
`microblaze_0`, `ilmb_v10`, `dlmb_v10`, `ilmb_bram_if_cntlr`,
`dlmb_bram_if_cntlr`, `local_ram` (or repurpose a slice as boot BRAM), `mdm_1`.
Nothing else in the BD instantiates or references them except the nets in 9.2.

### 9.2 Nets/ports to re-wire (the complete classic-MB touch list)
1. `axi_interconnect_0/S00_AXI` ← was `microblaze_0/M_AXI_DP`: re-source from
   the MBV's peripheral master (directly or via a periph smartconnect as in
   `mbv_soc.tcl`). Keep all 16 MI connections and every address untouched.
2. `axi_intc_0/irq` → was `microblaze_0/INTERRUPT`: re-target the MBV
   interrupt input. Keep `xlconcat_intr/In0..3` sources identical (§4).
3. `proc_sys_reset_shell/mb_reset` consumers: MB + the six LMB reset pins all
   disappear; feed the MBV core reset from `mb_reset` (same active-HIGH
   convention — mbv_soc.tcl: "feed mb_reset, NOT peripheral_aresetn").
4. `microblaze_0/DEBUG` ↔ `mdm_1/MBDEBUG_0`: replace with `mdm_riscv`.
5. LMB address segments (`ilmb/dlmb .../SLMB/Mem` @0x0, 1 MiB ×2): delete;
   add the DDR4 aperture with an **explicit** `assign_bd_address`
   (`0x8000_0000`, 1 GiB, high `0xBFFF_FFFF`) — never rely on auto-assign.

### 9.3 Cells/subsystems to ADD (carry the mbv_soc.tcl lessons VERBATIM)
- MBV (Sv32, rv32imac) + instruction/data cache masters over DDR.
- DDR4 MIG (`ddr4_0`): banks 49–51, **SLR1** — no collision with the SLR0 RP
  pblock, keep it that way (§9.5).
- `smartconnect_ddr` doing BOTH the 100↔200 MHz (`ui_clk`) async crossing and
  the 32b→512b upsizing.
- **`c0_ddr4_aresetn` MUST be driven** — it is an INPUT; unwired it
  synthesizes to `1'b0` and every DDR AXI channel FSM holds in reset while
  smartconnect silently swallows CPU stores. Drive it via
  `util_vector_logic not` from `c0_ddr4_ui_clk_sync_rst` (the example-design
  idiom).
- proc_sys_reset for the CPU domain gated on MMCM lock **AND**
  `aux_reset_in = c0_init_calib_complete` (slave-live-first ordering).
- The undriven-reset guard + explicit `assign_bd_address` + config-assert
  (`_assert_cfg`) discipline from `mbv_soc.tcl`.
- **Clock architecture decision (flag, must be made consciously):** the shell
  fabric is 100 MHz from OSCCLK1 (`clk_wiz_shell`); the proven MBV recipe
  derives its 100 MHz CPU/AXI clock from DDR4 `ui_clk` (200 MHz) instead.
  Options: (a) keep `clk_wiz_shell` as the single shell_clk and let
  smartconnect_ddr cross into ui_clk — minimal disturbance to the 16-slave
  fabric, recommended; (b) adopt the ui_clk-derived clock as shell_clk —
  touches EVERY `s_axi_aclk` and re-times the whole shell, plus makes the
  shell clock dependent on DDR calibration. Do not blend the two silently.

### 9.4 Firmware / wire-compat obligations
- Host tooling on **:6900 (JSON control) and :6910 (raw bitstream push) must
  not change**, nor 69/2542/6920/6930/6931/6932. The Linux userland must
  re-implement the coordinator services against the same
  `net-protocol.md` framing.
- All shell CSR bases (§2) frozen — `platform_regs.h` semantics become the
  Linux driver contract.
- HWICAP is FIFO-mode (`C_MODE 0`, depth 1024) — **no in-tree Linux
  fpga-manager driver for AXI HWICAP exists; flagged, not solved here.** The
  swap sequencing (decouple → clearing → partial → verify RM_ID) currently
  lives in `swap_fsm.c` and must be ported, preserving the
  clearing-before-partial order and the RM_ID genuine-verify property (§7).
- Shell MUST reset `hostio4_target` state on every swap (memory:
  hostio-integration) — whatever process replaces the superloop inherits this.

### 9.5 MUST NOT MOVE (the frozen half of the contract)
- **RP pblock**: SLR0 interior rows Y0–Y1 (`fpga/dfx/dfx_floorplan.xdc`,
  `pblock_rp_dut`, `SNAPPING_MODE ON`, no `RESET_AFTER_RECONFIG` on
  UltraScale, `BITSTREAM.CONFIG.PERSIST OFF` in build_dfx.tcl). SLR split
  confirmed live: SLR0 = X0Y0..X5Y4, SLR1 = X0Y5..X5Y9.
- **ICAP**: primary site CONFIG_SITE_X0Y0 (clock region X5Y1, SLR0) —
  RP/ICAP co-location holds; DDR4 (SLR1, banks 49–51) stays separable.
- `dfx_decoupler_0` + its full 19-interface ALL_PARAMS boundary, verbatim.
- `dfx_ctl_0`, `dut_clkrst_0`, `dfx_axi_shutdown_manager_0`, `inv_rp_in_reset`
  and all their nets (the swap sequencing fabric).
- `clk_wiz_dut` + its AXI-Lite DRP slave @0x44AB_0000 (only DUT-clock path).
- All partition-pin BD ports, names and widths (partition-pins.md v0.2 is the
  frozen list) and the `u_top = u_shell + u_rp_dut` sibling topology,
  `rp_inst=u_rp_dut`.
- ALL pin-facing logic: `axi_emc_0` (+ its slow LAN9220 AC timing — do not
  "optimize"), `board_gpio_0`, `clcd_0`/`clcd_kvm_0` pad chain, `jtag_bb_0`,
  `uart_bridge_0`, the QSPI pad path, and every `shell_top.sv` IOBUF/IBUF/BUFG
  (IOB packing is static-only in DFX — HDPR-29).
- `debug_bridge_0` (XVC must survive swaps).
- The two reserved regmap pages (VPHY 0x44A3, GENCHK 0x44A6) stay reserved.

### 9.6 Consequences to accept and document
- `static_id` re-mints; **all 8 RM overlays re-key and every RM partial must
  be re-implemented** against the new static routed DCP (§7 law).
- The LMB diag mailbox (0xFFF80, magic 0xD1A6C0DE) and its JTAG tooling
  (`scripts/mps3_diag.tcl`, tier3 gates, `check_diag_mailbox_parity.py`)
  lose their substrate — replace or retire explicitly.
- The 1 MiB LMB sizing rationale (`core + 2× clearing`, QSPI-free swaps) must
  be re-satisfied in DDR: trivially true for capacity, but the
  clearing-coexistence invariant (`check_clearing_fits.py`) moves to the
  Linux service design.
- `validate_bd_design` is NOT a sufficient gate for the decoupler
  (hex-string trap) — cheap check is `generate_target synthesis` on the
  decoupler alone. Nor is it sufficient for pblock/timing: gate DFX timing on
  **dut_clk**, not the static headline (known signoff gap).

## 10. Known open items inherited by the successor (not created by it)
- D7 (RP pblock sizing) and D12 (real DUT-clock target) still open.
- CLCD 8080 bus has zero pad timing constraints.
- QSPI XiP: plumbing landed, but I/O timing signoff vs SST26VF064B, the
  cold-XiP livelock fix, and silicon bring-up are all still owed — do not
  present XiP as working.
- Ethernet MAC-verification subsystem (VPHY/GENCHK, decoupler IDs 1–5 sinks)
  deferred; `s_*_DATA` outputs pre-authored and dangling by design.
- LAN9220 datasheet AC table remains unverified (A6 flag) — the conservative
  EMC timings stand until then.
