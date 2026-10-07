# nanosoc_m0_soc — RTL Anatomy Report

> **Status: HISTORICAL** — a record of a read-only RTL anatomy survey of `nanosoc_m0_soc` as of 2026-08-07.
> Superseded by / current state in [docs/STATUS.md](../STATUS.md).
> Kept for provenance; do not update.

Source investigated: `~/SoCLabs/nanosoc_m0_soc` (read-only checkout).
Purpose: ground truth for (a) a monolithic nanoSoC build on Arm MPS3 (Xilinx
Kintex UltraScale KU115, `xcku115-flvb1760-1-c`) and (b) a DFX reconfigurable-module
wrapper for `mps3-nanosoc-platform`. No files in `nanosoc_m0_soc` were modified.

---

## 0. TL;DR

- **This repo builds a genuinely different SoC from the multicore project** — one
  Cortex-M0 (SLCore-M0 wrapper around Arm's real Cortex-M0 RTL), **no ethernet**,
  optional SPI/QSPI, optional single-channel DMA. It is not "the multicore SoC with
  one core disabled"; it is a separate, smaller design tree.
- **Three nested top-level modules exist, in order of increasing "chip-ness":**
  `nanosoc` (SoC core, 72 ports) → `nanosoc_system` (adds an expansion-region hook)
  → `nanosoc_chip` (adds ASIC scan/bist/diag pins + UART/SWD pad muxing). None of
  the three is unambiguously "the" FPGA top by convention — the correct pick
  depends on which one is actually known to build, see §1.4.
- **A same-day build report already exists in this repo**
  (`doc/reports/pynq_z2_build.md`, dated 2026-07-03, uncommitted working-tree
  state) documenting a **successful bitstream build to a PYNQ-Z2** that
  deliberately bypasses both `nanosoc_system` and `nanosoc_chip` and wraps
  `nanosoc` directly, because the generated `nanosoc_system`/expansion-region
  RTL has real, confirmed-in-this-RTL bugs (see §1.3, §1.4). This is the single
  most load-bearing fact for picking an MPS3/DFX integration point.
- **MPS3 (`FPGA=mps3`) is already a declared target**: Xilinx part
  `xcku115-flvb1760-1-c`, board name `arm_mps3`, `PLATFORM := bare` (no hard
  processor system — a genuine bare-metal Kintex US build, matching the task).
  A board-level wrapper skeleton exists
  (`nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/`) but it is a generic,
  mostly-stubbed MPS3 daughterboard template (SMB/HDMI/EMMC/CLCD/Audio pins tied
  off) that has **not** been proven to build — see §1.4 and §5.
- CPU IP is Arm's real Cortex-M0 RTL (`cm0_*.v`, `CORTEXM0.v`, etc.) resolved
  through `$(ARM_IP_LIBRARY_PATH)` (the site Arm IP library; read-only,
  per user policy) — not obfuscated, plain Verilog, but licensed Arm IP.
- Ethernet: **confirmed absent.** No MAC, no RMII, nothing eth-shaped anywhere
  in this tree. The DFX platform's RMII/MDIO partition-pin group will simply be
  unused/tied-off by this DUT.
- Recommended FPGA flist: `nanosoc_arch_tech/rtl/flist/nanosoc_FPGA.flist` (see §5).

---

## 1. Top module

### 1.1 The three-level hierarchy

```
nanosoc_chip            (chip/chip/verilog/nanosoc_chip.v)       — ASIC/pad-mux level
   └─ nanosoc_system     (nanosoc_arch_tech/rtl/src/system/nanosoc_system.v)  — + expansion-region hook
        └─ nanosoc       (nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv)      — SoC core, all subsystems
             ├─ nanosoc_ss_cpu       (CPU subsystem: CM0 core + bootrom + imem + dmem)
             ├─ nanosoc_ss_dma       (optional PL230/DMA350 DMA controller)
             ├─ nanosoc_ss_debug     (SoCDebug ADP controller)
             ├─ nanosoc_ss_systemctrl (GPIO0/1, sysctrl, APB peripheral subsystem)
             ├─ nanosoc_ss_hostio4   (FT1248/host-IO byte-stream bridge)
             ├─ nanosoc_interconnect (main AHB bus matrix, 8 masters × 9 targets)
             └─ Ssp (PL022 SPI master, Arm IP)
```

Every one of these three levels is auto-generated from the same Jinja templates
(`soc_toplevel.sv.j2`, `soc_system.sv.j2`, `soc_chip.v.j2`) by nanosoc_gen, driven
by `sys_desc/nanosoc_m0_soc.yaml`. `build_soc/rtl/` holds the last-regenerated
snapshot; the canonical/checked-in copies live under `nanosoc_arch_tech/rtl/src/`
and `chip/chip/verilog/` (see §1.3 for why these two copies are **not always
identical**).

### 1.2 `nanosoc` — the SoC-core level (recommended integration point)

- **Module**: `nanosoc`
- **File** (canonical/current): `~/SoCLabs/nanosoc_m0_soc/nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv`
- **File** (last regenerated snapshot): `~/SoCLabs/nanosoc_m0_soc/build_soc/rtl/nanosoc.sv`
- **72 top-level ports.** Full list:

| # | Port | Dir | Width | Function |
|---|---|---|---|---|
| 1 | `sys_clk` | in | 1 | System input clock |
| 2 | `sys_sysresetn` | in | 1 | System reset, active low |
| 3 | `sys_xtalclk_out` | out | 1 | Crystal clock output |
| 4 | `sys_scanenable` | in | 1 | Scan mode enable |
| 5 | `sys_testmode` | in | 1 | Test mode enable (override synchronisers) |
| 6 | `sys_scaninhclk` | in | 1 | HCLK scan input |
| 7 | `sys_scanouthclk` | out | 1 | Scan chain output |
| 8 | `cpu_0_swdi` | in | 1 | CPU SWD data in |
| 9 | `cpu_0_swclk` | in | 1 | CPU SWD clock |
| 10 | `cpu_0_swdo` | out | 1 | CPU SWD data out |
| 11 | `cpu_0_swdoen` | out | 1 | CPU SWD data out-enable |
| 12 | `p0_in` | in | [15:0] | GPIO port 0 in |
| 13 | `p0_out` | out | [15:0] | GPIO port 0 out |
| 14 | `p0_outen` | out | [15:0] | GPIO port 0 out-enable |
| 15 | `p1_in` | in | [15:0] | GPIO port 1 in (bit 7 = FT1248MODE strap) |
| 16 | `p1_out` | out | [15:0] | GPIO port 1 out |
| 17 | `p1_outen` | out | [15:0] | GPIO port 1 out-enable |
| 18 | `sys_hclk` | out | 1 | AHB clock out (for expansion) |
| 19 | `sys_hresetn` | out | 1 | AHB reset out (for expansion) |
| 20 | `exp_hsel` | in* | 1 | Expansion AHB port, hsel (see §1.3 bug) |
| 21 | `exp_haddr` | in* | 32 | Expansion AHB port, haddr |
| 22 | `exp_htrans` | in* | 2 | Expansion AHB port, htrans |
| 23 | `exp_hwrite` | in* | 1 | Expansion AHB port, hwrite |
| 24 | `exp_hsize` | in* | 3 | Expansion AHB port, hsize |
| 25 | `exp_hburst` | in* | 3 | Expansion AHB port, hburst |
| 26 | `exp_hprot` | in* | 4 | Expansion AHB port, hprot |
| 27 | `exp_hwdata` | in* | 32 | Expansion AHB port, hwdata |
| 28 | `exp_hmastlock` | in* | 1 | Expansion AHB port, hmastlock |
| 29 | `exp_hready` | in* | 1 | Expansion AHB port, hready |
| 30 | `exp_hrdata` | out* | 32 | Expansion AHB port, hrdata |
| 31 | `exp_hresp` | out* | 1 | Expansion AHB port, hresp |
| 32 | `exp_hreadyout` | out* | 1 | Expansion AHB port, hreadyout |
| 33-47 | `exp_str_in_{0,1,2}_{tvalid,tready,tdata,tstrb,tlast}` | mixed | 1/32/4 | 3× AXI-Stream DMA channel, SoC→expansion |
| 48-63 | `exp_str_out_{0,1,2}_{tvalid,tready,tdata,tstrb,tlast,flush}` | mixed | 1/32/4 | 3× AXI-Stream DMA channel, expansion→SoC |
| 64 | `exp_irq` | in | [3:0] | Expansion interrupt requests |
| 65 | `exp_drq` | in | [1:0] | Expansion DMA requests |
| 66 | `exp_dlast` | out | [1:0] | DMA last signals to expansion |
| 67 | `spi_sclk` | out | 1 | SPI serial clock (PL022 SSPCLKOUT) |
| 68 | `spi_ss` | out | 1 | SPI chip select, active low (PL022 SSPFSSOUT) |
| 69 | `spi_mosi` | out | 1 | SPI MOSI (PL022 SSPTXD) |
| 70 | `spi_miso` | in | 1 | SPI MISO (PL022 SSPRXD) |

(Table rows 33-63 are 31 individual signals collapsed for brevity; count above
includes them individually — 72 ports total, verified against the RTL.)

\* **`exp_h*` port directions are as-declared in the source but are backwards for
their stated role** — see §1.3. This matters directly for the DFX RM boundary if
`exp` is chosen as the black-box attachment point.

Key module parameters (defaults as generated): `SYS_CLK_FREQ_HZ=100000000`,
`DMAC_0_TYPE=0` (no DMA), `DMAC_1_TYPE=0`, `BOOTROM_ADDR_W=11` (2KB),
`IMEM_RAM_ADDR_W=14` (64KB), `DMEM_RAM_ADDR_W=14` (64KB), `SRAM_0/1_RAM_ADDR_W=14`
(64KB each), `QSPI_FLASH_PRESENT=0`, `NUMIRQ=32`.

### 1.3 `nanosoc_system` — adds the expansion-region hook (confirmed broken for FPGA)

- **Module**: `nanosoc_system`
- **File**: `nanosoc_arch_tech/rtl/src/system/nanosoc_system.v` (also
  `build_soc/rtl/nanosoc_system.sv`, functionally equivalent regenerated copy)
- Instantiates `nanosoc` plus a default expansion-region slave
  (`nanosoc_region_exp_default`, a `cmsdk_ahb_default_slave` that AHB-errors
  all accesses) wired onto `nanosoc`'s `exp_*` port.
- **I independently confirmed the wiring bug** the PYNQ build report (§1.4)
  describes: `nanosoc.exp_hsel` is declared `input`, and
  `nanosoc_region_exp_default.HSEL` is *also* declared `input`. `nanosoc_system`
  connects both to the same internal wire `EXP_HSEL` — **neither end drives it**.
  Same pattern on `exp_haddr/htrans/hwrite/hsize/hburst/hprot/hwdata/hmastlock`
  (all inputs on both sides) and `exp_hrdata/hresp/hreadyout` (outputs on both
  sides, i.e. two drivers if anything were ever hooked up the "normal" way).
  Verilog/SystemVerilog simulators typically don't flag this loudly (undriven
  nets read as X/Z, not an elaboration error), so it passes sim but is a real
  problem for synthesis/PnR, where Vivado will not silently paper over it.
- Also drops `sys_hclk`/`sys_hresetn` re-export relative to `nanosoc` (needed by
  the PYNQ board wrapper's LED-activity stretcher, per the build report).
- **Net effect: do not build on top of `nanosoc_system` as-is for an FPGA/DFX
  target without first fixing the exp-port direction bug** (either at the
  nanosoc_gen AHB-target port-emission level, or with a hand-written local
  override, the same pattern already used elsewhere in this repo for generator
  gaps — see `src/rtl/local_overrides/`).

### 1.4 `nanosoc_chip` — the ASIC/pad-mux level

- **Module**: `nanosoc_chip`
- **File (canonical, hand-maintained, current)**: `chip/chip/verilog/nanosoc_chip.v`
  — 34 functional ports + optional `VDD`/`VSS`/`VDDACC` under `` `ifdef POWER_PINS ``
  (37 total when defined; FPGA builds leave `POWER_PINS` undefined and the module
  ties off `VDD`/`VDDACC`/`VSS` internally via `supply1`/`supply0`).
- **File (last auto-regenerated snapshot)**: `build_soc/rtl/nanosoc_chip.v` —
  **stale relative to the canonical copy.** `chip/chip/verilog/nanosoc_chip.v`
  was hand-edited (mtime 22:29 same day) *after* the last `build_soc/rtl`
  regeneration (mtime 18:09) to bond out the SPI master pins
  (`spi_sclk_o/spi_ss_o/spi_mosi_o/spi_miso_i`) and select DMA type via
  `` `ifdef DMAC_DMA350`` / `` `ifdef DMAC_0_PL230`` compile-time macros. The
  regenerated `build_soc/rtl/nanosoc_chip.v` has **no SPI ports at all** and
  selects DMA type via a `nanosoc_soc_config_pkg::DMAC_0_TYPE` SystemVerilog
  package parameter instead. **These are two different generations of the same
  file with two different DMA-config mechanisms; the flist-driven build (§5)
  deliberately uses the hand-maintained `chip/chip/verilog/` copy, not the
  `build_soc/rtl/` snapshot, for this one file.** Anyone regenerating
  `build_soc/` (`make soc_model`) should diff against `chip/chip/verilog/`
  before trusting it.
- Port list of the canonical (`chip/chip/verilog/`) copy: `diag_mode`,
  `diag_ctrl`, `scan_mode`, `scan_enable`, `scan_in[3:0]`, `scan_out[3:0]`,
  `bist_mode`, `bist_enable`, `bist_in[3:0]`, `bist_out[3:0]`, `alt_mode`,
  `uart_rxd_i`, `uart_txd_o`, `swd_mode`, `clk_i`, `test_i`, `nrst_i`,
  `p0_i/o/e/z[15:0]`, `p1_i/o/e/z[15:0]`, `swdio_i/o/e/z`, `swdclk_i`,
  `spi_sclk_o`, `spi_ss_o`, `spi_mosi_o`, `spi_miso_i`. It muxes UART and SWD
  onto shared pads via `alt_mode`/`swd_mode` straps, ties off scan/bist for
  normal operation, and instantiates `nanosoc_system` underneath — **so it
  inherits the §1.3 expansion-port bug unless the expansion region is simply
  never used** (true for a plain UART-prints bring-up).
- Packaged as Vivado IP `nanosoc_vivado_wrapper` via
  `nanosoc_arch_tech/fpga/fpga/vivado_ip/nanosoc_chip_vivado_wrapper.v`
  (`set_property top nanosoc_chip_vivado_wrapper` in
  `nanosoc_arch_tech/fpga/fpga/vivado_ip/package_nanosoc_ip.tcl`), which ties
  off scan/bist/diag, hardcodes `alt_mode=1`/`swd_mode=1`, and exposes a
  clean GPIO-tristate + SWD-tristate + UART IP-Integrator interface. Five board
  targets exist under `nanosoc_arch_tech/fpga/fpga/targets/`:
  **`arm_mps3`**, `pynq_kr260`, `pynq_kv260`, `pynq_z2`, `pynq_zcu104`.

### 1.5 Ground truth from the same-day working PYNQ-Z2 build — read this before picking a top module

`doc/reports/pynq_z2_build.md` (dated 2026-07-03, in `nanosoc_m0_soc`'s working
tree, not committed) documents a **complete, timing-closed bitstream build**
(WNS +16.3 ns @ 25 MHz) for a PYNQ-Z2 that was built through this exact repo.
Critically, it does **not** use `nanosoc_chip` or `nanosoc_chip_vivado_wrapper`
at all — it uses a bespoke, hand-written `pynq/vivado_ip/nanosoc_vivado_wrapper.v`
that **wraps `nanosoc` directly**, explicitly to route around the
`nanosoc_system`/`nanosoc_chip` problems above:

> "Wrapper wraps `nanosoc` (core) instead of `nanosoc_system`. `nanosoc_system`
> does not re-export `sys_hclk`/`sys_hresetn` ... and instantiates a module that
> does not exist" / "Expansion region tied off, not error-responding ... Wiring
> the default slave through this boundary would produce undriven / multiply-driven
> nets" / "`nanosoc_region_exp_default` RTL does not exist upstream (yaml is
> `gen: True` but no backend emits it)".

It also confirms (independently corroborating everything found by RTL
inspection in §2-4 below): CM0-only, no Ethernet, PL022 SPI at
`0x2800C000`, `ARM_IP_LIBRARY_PATH=<Arm IP library>` supplies
Cortex-M0 + CMSDK + PL022, and the generator's own FPGA backends
(`--fpga-wrapper`, `--emit-vivado-bd`, `--emit-constraints`) were evaluated and
**rejected** as not yet covering this board's needs — a hand-written wrapper was
used instead. This is the same generalized generator machinery that produced the
`nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/` skeleton (§5), which is
consistent with that skeleton being unproven scaffolding rather than a working
build.

**Recommendation for the MPS3 monolithic baseline / DFX RM boundary: treat
`nanosoc` (§1.2, 72 ports) as the synthesizable SoC-core top, and write a
bespoke MPS3/DFX board-and-partition wrapper around it** the same way the
PYNQ-Z2 flow wrote a bespoke wrapper around it, rather than trying to make
`nanosoc_system`/`nanosoc_chip` work first. If the DFX flow specifically wants
the ASIC-style pad-mux/scan/bist boundary (`nanosoc_chip`'s 34-port interface),
that's usable too **provided the expansion region is left unconnected** (true
for a UART-only bring-up) — but the `exp_*` bug should be fixed upstream (or
locally overridden, matching the existing `src/rtl/local_overrides/` pattern)
before anything tries to route real transactions through it, e.g. a DFX
accelerator partition hung off `exp`.

---

## 2. Single-core confirmation

Confirmed: **exactly one Cortex-M0**, no second core, no multicore interconnect,
no IPC mailbox, no cross-core reset controller — all of that
(`network_core`/`chip_core`, IPC, `evt_route_ctrl`, per-core reset controller
etc., per the multicore project's own memory) is simply absent from this tree.

- CPU subsystem: `nanosoc_ss_cpu` (`nanosoc_arch_tech/rtl/src/subsystems/cpu/nanosoc_ss_cpu.v`,
  regenerated at `build_soc/rtl/nanosoc_ss_cpu.sv`) instantiates exactly one
  `slcorem0` (`u_cpu_0`) plus one bootrom region, one IMEM region, one DMEM
  region, wired through `nanosoc_cpu_ss_interconnect` (a small dedicated AHB
  matrix: cpu_0 + cpu_ss master ports → bootrom_0/imem_0/dmem_0/system targets).
- `slcorem0` (module `slcorem0`,
  `nanosoc_arch_tech/rtl/slcorem0_tech/src/verilog/slcorem0.v`) is **SoC Labs'
  own wrapper** ("SoCLabs SLCore-M0 — Basic Cortex-M0 CPU Subsystem") around the
  real Arm Cortex-M0 core + DAP, not a from-scratch reimplementation and not
  obfuscated — plain, readable Verilog throughout (`cm0_core.v`, `cm0_core_alu.v`,
  `cm0_nvic.v`, `CORTEXM0.v`, `CORTEXM0DAP.v`, etc.).
- **IP source path / env var**: `slcorem0.flist` →
  `-f $(SOCLABS_SLCOREM0_TECH_DIR)/flist/cortexm0_ip.flist`, and
  `cortexm0_ip.flist` resolves every Cortex-M0 file under
  `$(ARM_IP_LIBRARY_PATH)/latest/Cortex-M0/logical/{cortexm0,cortexm0_dap,
  cortexm0_integration,models/cells,models/wrappers}/verilog/…`.
  `$(ARM_IP_LIBRARY_PATH)` comes from tools.env (no default)
  (confirmed both by the makefile chain
  `nanosoc_arch_tech/makefile` → `nanosoc.config`/`autoconfig`, and explicitly
  stated in `doc/reports/pynq_z2_build.md:42`:
  `` `ARM_IP_LIBRARY_PATH=<Arm IP library>` (read-only; supplies the
  Cortex-M0, CMSDK and the PL022 SSP) ``). This path was only **read** during
  this investigation, per the read-only policy on the Arm IP library.
  `$(SOCLABS_SLCOREM0_TECH_DIR)` itself resolves locally to
  `nanosoc_arch_tech/rtl/slcorem0_tech` (a git submodule inside
  `nanosoc_arch_tech/rtl/`), per `nanosoc_arch_tech/makefile:20`.
- Note: `imp/fpga/nanosoc_ip/src/{cm0_*, CORTEXM0*, Ssp*}` is a **stale 2022
  Vivado-project snapshot** (Vivado 2021.1 project files, `component.xml` dated
  2022) with a different, older module-naming convention (`cm0_top`, not
  `slcorem0`) — it is not part of the current build flow; ignore it for
  anatomy purposes, it is a leftover artifact from an earlier packaging attempt.

---

## 3. Peripheral / interconnect inventory

### 3.1 Main AHB interconnect (`nanosoc_interconnect`, inside `nanosoc`)

9 AHB targets off the main matrix (masters: `cpu_0`(via cpu_ss passthrough),
`dmac_0`, `dmac_1`, `debug`): `cpu_ss` (the CPU subsystem itself, at address 0),
`dmac_ctrl`, `soc_peripheral`, `sram_0`, `sram_1`, `systable`, `spi`, plus the
`exp` expansion port. Base addresses (from
`imp/fpga/fw_config/nanosoc_memmap.h`, generated by `gen_firmware_config.py`
from the same `sys_desc/nanosoc_m0_soc.yaml`):

| Region | Base | Notes |
|---|---|---|
| `CPU_SS` / `BOOTROM_0` | `0x0000_0000` | 8KB physical (`BOOTROM_ADDR_W=11`), aliased/remapped to `0x0800_0000` after boot (`sys_remap_ctrl[0]`) |
| `IMEM_0` | `0x1000_0000` | 64KB (`IMEM_RAM_ADDR_W=14`); remaps to `0x0000_0000` post-boot |
| `DMEM_0` | `0x1800_0000` | 64KB (`DMEM_RAM_ADDR_W=14`) |
| `SPI` (PL022) | `0x2800_C000` | Dedicated AHB→APB bridge + Arm PL022 SSP master, own top-level pins (`spi_sclk/ss/mosi/miso`) — **not** part of the `SOC_PERIPHERAL` APB subsystem below |
| `SOC_PERIPHERAL` | `0x4000_0000` | size `0x1000_0000`; APB subsystem + GPIO0/1 + sysctrl (breakdown below) |
| `DMAC_CTRL` | `0x5000_0000` | Only populated if `DMAC_0_TYPE`/`DMAC_1_TYPE` ≠ 0 (default 0 = none) |
| `EXP` | `0x6000_0000` | Expansion region; default = AHB-error slave, currently mis-wired for FPGA (§1.3) |
| `SRAM_0` | `0x8000_0000` | 64KB |
| `SRAM_1` | `0x9000_0000` | 64KB |
| `SYSTABLE` | `0xF000_0000` | CoreSight ROM table |

`SOC_PERIPHERAL` (0x4000_0000) sub-map, from
`nanosoc_arch_tech/rtl/src/regions/soc_peripheral/nanosoc_soc_peripheral_decode.v`
+ `nanosoc_soc_peripheral_apb_ss.v` (an Arm CMSDK APB subsystem instance,
`cmsdk_apb_slave_mux` PORT0-PORT15, one 4KB window each):

| Offset | Peripheral | IP module |
|---|---|---|
| `+0x0000` | Timer 0 | `cmsdk_apb_timer` |
| `+0x1000` | Timer 1 | `cmsdk_apb_timer` |
| `+0x2000` | Dual-timer | `cmsdk_apb_dualtimers` |
| `+0x4000` | "UART0" — **SoCDebug USRT0**, an AXI-byte-stream ADP debug channel, not a pinned serial port | `socdebug_usrt_control` |
| `+0x5000` | "UART1" — SoCDebug USRT1, same as above | `socdebug_usrt_control` |
| `+0x6000` | **UART2 — the real, pinned, console UART** (RXD/TXD/TXEN) | `cmsdk_apb_uart` |
| `+0x8000` | Watchdog | `cmsdk_apb_watchdog` |
| `+0xB000` | Test slave (validation only) | `cmsdk_apb_test_slave` |
| `+0xC000..0xF000` | 4× extension slots (`ext12-15`); SoCDebug USRT2 (ADP host-debug channel) lives on one of these | — |
| `0x4001_0000` | GPIO0 (16-bit AHB GPIO) | `cmsdk_ahb_gpio` |
| `0x4001_1000` | GPIO1 (16-bit AHB GPIO) | `cmsdk_ahb_gpio` |
| `0x4001_F000` | Sysctrl (reset/lockup/NMI/remap control) | `nanosoc_sysctrl` |

**For a "UART prints" bring-up, the console is CMSDK UART2** (`0x4000_6000`,
top-level `nanosoc.p1_*` GPIO pins via the pin-mux, `uart2_txd`→P1[5]/`uart2_rxd`←P1[4]
when firmware sets `ALTFUNCSET`, per both the RTL pinmux and
`doc/reports/pynq_z2_build.md` §4). UART0/UART1 in the address map are **not**
general-purpose serial UARTs — don't wire firmware `printf` to them.

### 3.2 Full inventory summary

- **UART**: 1× real pinned UART (CMSDK UART2) + 2× SoCDebug USRT debug-channel
  pseudo-UARTs (USRT0/1, byte-stream to the ADP/FT1248 debug controller, not pins).
- **Timers**: 2× `cmsdk_apb_timer` + 1× `cmsdk_apb_dualtimers`.
- **Watchdog**: 1× `cmsdk_apb_watchdog` (drives NMI + system reset).
- **DMA**: optional/configurable — `nanosoc_ss_dma` subsystem exists structurally
  (`nanosoc_region_dmac_ctrl`, `nanosoc_arbiter_DMAC_CTRL`) but `DMAC_0_TYPE`/
  `DMAC_1_TYPE` default to `0` (none) in both `nanosoc.sv`'s parameter default
  and `nanosoc_chip.v`'s `` `ifdef`` fallback. Populate with PL230
  (`DMAC_0_PL230`/`DMAC_1_PL230`) or DMA-350 (`DMAC_DMA350`, dual-port, DMAC_0
  only) via compile-time macro, resolved through `SOCLABS_SLDMA230_TECH_DIR`/
  `SOCLABS_SLDMA350_TECH_DIR` (both under `nanosoc_arch_tech/rtl/`).
- **QSPI**: **not present by default.** `QSPI_FLASH_PRESENT=0`. Opt-in
  extension (`flist/nanosoc_qspi.flist`, wrapper `src/rtl/wrappers/qspi_flash_ahb.v`,
  IP from a separate `ahb_qspi` repo + Arm CG092 flash-cache IP) — legacy stage-0
  bootrom probes a dead QSPI-shaped register slot at `0x4000_C000` and always
  falls through to 1-stage boot when the extension isn't built in.
- **SPI**: Arm PL022 SSP, master-mode-only bond-out, dedicated AHB target at
  `0x2800_C000`, own top-level pins. This is the SoC's actual/default "SPI"
  (QSPI is the opt-in alternative, mutually exclusive in practice per
  `doc/reports/pynq_z2_build.md`'s rework note).
- **GPIO**: 2× 16-bit ports (P0, P1), AHB `cmsdk_ahb_gpio`, `0x4001_0000`/`0x4001_1000`.
  P1 has muxed alternate functions (UART2, FT1248/hostio4).
- **Debug**: per-core SW-DP (`CORTEXM0DAP`, no SoC-400 SWJ-DP/AP mux — single
  `cortex_m` target, DPIDR `0x0BB11477`), plus a separate SoCDebug ADP
  controller (`nanosoc_ss_debug`) for USRT/FT1248 host-debug byte streams
  (unrelated to the CPU's SWD).

---

## 4. Ethernet

**Confirmed absent.** No MAC, no RMII/MII, no MDIO, nothing eth-shaped anywhere
under `nanosoc_m0_soc` — no `eth_*`/`rmii_*`/`mdio_*` module, no such AHB target
in the interconnect, no such IRQ line. `doc/reports/pynq_z2_build.md` states this
explicitly ("this SoC has no ethernet, so PMODB + RPi header are free"). This
matches the task's expectation and means: for the `mps3-nanosoc-platform`
partition-pin boundary, this DUT will simply leave the RMII/MDIO pin group
unconnected/tied-off — there is nothing in this RTL to drive it.

---

## 5. Flists — which one is right for FPGA synthesis, and what it pulls in

### 5.1 The candidates, what each actually is

- **`flist/nanosoc_spi.flist`** — NOT a whole-SoC flist. An **add-on** flist for
  the Arm PL022 SSP RTL only (resolves through
  `$(ARM_IP_LIBRARY_PATH)/PL022/PL022-BU-00000-r1p4-00rel0/…`). Its own header
  says to combine it with `build_soc/flist/nanosoc_toplevel.flist`.
- **`flist/nanosoc_qspi.flist`** — also an add-on, **opt-in and disabled by
  default** (`QSPI_FLASH_PRESENT=0`). Pulls `src/rtl/wrappers/qspi_flash_ahb.v`
  + an external `ahb_qspi` repo (`$(SOCLABS_AHB_QSPI_DIR)`) + Arm CG092 flash-cache
  IP. Not needed unless QSPI is explicitly re-enabled.
- **`nanosoc_arch_tech/rtl/flist/nanosoc_FPGA.flist`** — **this is the one.**
  A thin wrapper that pulls in hostio4 RTL directly, then
  `-f nanosoc_ip.flist` (the full chip→system→core→subsystem→region hierarchy,
  see §5.2), `-f corstone101_ip.flist` (non-ASIC/FPGA-flavoured Arm CMSDK
  peripheral IP), `-f slcorem0.flist` (CM0 core, non-ASIC variant), `-f
  socdebug.flist`. Compare to `nanosoc_arch_tech/rtl/flist/nanosoc.flist`
  (same, plus an extra ASIC/glib pad-ring file, `chip/pads/glib/verilog/
  nanosoc_chip_pads.v` — **do not use that one for FPGA**, it adds real
  pad-cell models that Xilinx synthesis doesn't need and doesn't know how to
  handle) and `nanosoc_ASIC.flist` (swaps in `corstone101_ip_ASIC.flist` +
  `slcorem0_ASIC.flist`, technology-mapped variants — also not for FPGA).
- **`build_soc/flist/*.flist`** — these are **leaf/building-block flists**
  consumed *by* `nanosoc_ip.flist`, not top-level flists in their own right:
  - `nanosoc_toplevel.flist` → `build_soc/rtl/nanosoc.sv` + the `soc_glue_*.sv`
    structural helper modules (from `nanosoc_gen/rtl/soc_glue/`).
  - `nanosoc_interconnect.flist` → main AHB matrix RTL + discovery + config pkg.
  - `nanosoc_cpu_ss_interconnect.flist` → CPU-subsystem AHB matrix RTL.
  - `nanosoc_soc_config_pkg.flist`, `nanosoc_system_system.flist`,
    `nanosoc_ss_cpu_toplevel.flist` exist as same-shaped leaf flists but are
    **not** referenced by `nanosoc_ip.flist` (which pulls `nanosoc_system.v`
    and `nanosoc_ss_cpu.sv` as bare file paths instead) — they appear to be
    for alternative/simulation-only compile scripts (e.g. `nanosoc_qs.flist`,
    the Arm QuickStart-flavoured build) rather than the FPGA path.

### 5.2 What `nanosoc_ip.flist` (pulled in by `nanosoc_FPGA.flist`) actually contains

In elaboration order:
1. `chip/chip/verilog/nanosoc_chip.v` + `nanosoc_chip_cfg.v` — **the canonical,
   hand-maintained chip top** (§1.4), not the `build_soc/rtl/` snapshot.
2. `-f build_soc/flist/nanosoc_toplevel.flist` → `build_soc/rtl/nanosoc.sv` (the
   freshly-regenerated SoC-core top, §1.2) + soc_glue helpers.
3. `nanosoc_arch_tech/rtl/src/system/nanosoc_system.v` (§1.3).
4. `build_soc/rtl/nanosoc_ss_cpu.sv` (regenerated CPU-subsystem top) +
   `nanosoc_ss_debug.v`, `nanosoc_ss_systemctrl.v`, `nanosoc_dma_wrapper.v`,
   `nanosoc_ss_dma.sv`, `nanosoc_ss_hostio4.v` (all from
   `nanosoc_arch_tech/rtl/src/subsystems/`).
5. `-f build_soc/flist/nanosoc_interconnect.flist` (main matrix) +
   `nanosoc_ahb_interconnect_discovery_apb_wrapper.sv` (hand-written,
   peakrdl-regblock bridge) + `-f build_soc/flist/nanosoc_cpu_ss_interconnect.flist`.
6. Bootrom: `$(SOCLABS_PROJECT_DIR)/build/firmware/bootloader/stage0/out/
   nanosoc_region_bootrom.v` — **generated by the firmware/bootrom build flow,
   at a path outside `nanosoc_m0_soc` entirely** (a sibling `build/` at the
   project root). This confirms `build_soc/rtl/` is not fully self-contained:
   the bootrom RTL specifically comes from the firmware toolchain, not from
   `nanosoc_gen`.
7. Regions: `imem`, `dmem`, `exp`, `sram`, `dmac_ctrl`, `soc_peripheral` (+
   `sysctrl`/`apb_ss`/`decode`), `systable` (+ `coresight_systable`) — all from
   `nanosoc_arch_tech/rtl/src/regions/`.
8. Control: `nanosoc_clkctrl.v`, `nanosoc_pin_mux.v`.

Plus, from the two sibling `-f` includes in `nanosoc_FPGA.flist` itself:
`corstone101_ip.flist` (Arm CMSDK: APB timer/dualtimer/UART/watchdog, AHB
GPIO/default-slave/slave-mux/to-APB/to-SRAM, clock-gate + memory models — all
resolved via `$(ARM_IP_LIBRARY_PATH)/latest/Corstone-101/…`) and
`slcorem0.flist` → `cortexm0_ip.flist` (the real Arm Cortex-M0, §2) +
`slcorem0_ip.flist` (SoC Labs' wrapper RTL). `socdebug.flist` pulls the
SoCDebug ADP/FT1248 controller RTL from `nanosoc_arch_tech/rtl/socdebug_tech/`.

### 5.3 Is `build_soc/rtl/` ready-to-synth, or does something regenerate it?

**It's regenerated, not hand-authored, and is not always in sync with the
hand-maintained copies** (§1.4's `nanosoc_chip.v` divergence is direct proof).
`build_soc/` is nanosoc_gen's output directory: `make soc_model` (or, in the
PYNQ flow, `make -C pynq soc_model`) regenerates it from
`sys_desc/nanosoc_m0_soc.yaml` via the Jinja templates named in every
`build_soc/rtl/*.sv` file's own header comment (e.g. `soc_toplevel.sv.j2`,
`soc_system.sv.j2`, `soc_chip.v.j2`). Every file in `build_soc/rtl/` and
`build_soc/flist/` carries an explicit `AUTO-GENERATED — DO NOT EDIT DIRECTLY`
banner and a `Generated: <timestamp>` line — the same SoC-wide generation run
produced most of them within seconds of each other on 2026-07-03, **except**
`chip/chip/verilog/nanosoc_chip.v`'s hand-maintained twin, which was edited
~4 hours later and is now ahead of what a fresh `make soc_model` would produce
for that one file. Anything picking up this build should either (a) regenerate
`build_soc/` and then re-apply the SPI bond-out / verify DMA-type selection
still matches the canonical chip file, or (b) keep using the `chip/chip/verilog/`
copy directly (as the real `nanosoc_ip.flist` already does) and not regenerate
over it.

---

## 6. Files referenced (all read-only)

- Top modules: `chip/chip/verilog/nanosoc_chip.v`, `chip/chip/verilog/nanosoc_chip_cfg.v`,
  `nanosoc_arch_tech/rtl/src/system/nanosoc_system.v`,
  `nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv`, `build_soc/rtl/nanosoc.sv`,
  `build_soc/rtl/nanosoc_chip.v`, `build_soc/rtl/nanosoc_system.sv`,
  `build_soc/rtl/nanosoc_ss_cpu.sv`.
- CPU: `nanosoc_arch_tech/rtl/slcorem0_tech/src/verilog/slcorem0.v`,
  `nanosoc_arch_tech/rtl/slcorem0_tech/flist/{slcorem0.flist,cortexm0_ip.flist}`.
- Peripherals: `nanosoc_arch_tech/rtl/src/regions/soc_peripheral/
  {nanosoc_soc_peripheral_decode.v,nanosoc_soc_peripheral_apb_ss.v,nanosoc_sysctrl.v}`.
- Flists: `flist/nanosoc_{spi,qspi}.flist`,
  `nanosoc_arch_tech/rtl/flist/{nanosoc_FPGA,nanosoc,nanosoc_ASIC,nanosoc_ip,
  corstone101_ip}.flist`, `build_soc/flist/*.flist`.
- FPGA/MPS3 target scaffolding: `nanosoc_arch_tech/fpga/fpga/makefile.targets`,
  `nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/{nanosoc_design_wrapper.v,
  fpga_pinmap.xdc,fpga_timing.xdc}`,
  `nanosoc_arch_tech/fpga/fpga/vivado_ip/{nanosoc_chip_vivado_wrapper.v,
  package_nanosoc_ip.tcl}`.
- Ground-truth build evidence: `doc/reports/pynq_z2_build.md` (uncommitted
  working-tree file, dated 2026-07-03).
- Memory map: `imp/fpga/fw_config/nanosoc_memmap.h` (generated).
