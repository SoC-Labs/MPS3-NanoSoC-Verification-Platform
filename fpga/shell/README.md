# Static shell (`fpga/shell/`)

This directory holds the Vivado block design and the shell-owned custom
AXI-Lite IP blocks that this agent (A1) is responsible for. Status: **all six
custom `ip/` blocks are real, verilator-lint-clean AXI4-Lite RTL** —
`clkrst`, `dfx_ctl`, `board_gpio` (b3e8e99 wave) and `uart_bridge`, `swd_bb`,
`telem` (W-RTL-NEWIP wave; each of the three new blocks carries its own
README covering regmap, CDC structure, real-vs-seamed boundaries, A5 bench
notes and A6 flags). **The BD is assembled AND proven against a live Vivado
2024.1 install** (W-BD wave): `validate_bd_design` passes **0 errors / 0
critical warnings**, the six custom blocks are packaged as real IP-XACT
components (`ip_packaged/package_csr_ip.tcl` — see "IP packaging route"
below), and `build_shell.tcl` runs the FULL flow clean. **Build described here (superseded — see docs/FIELDED_SHELL.md):
2026-07-07, 256 KiB LMB** (`build_results_2026-07-07-256k/`,
`BUILD_SHELL_OK impl+bitstream+xsa`) — the MicroBlaze local RAM was doubled
128 KiB → 256 KiB so the real lwIP + firmware ELF fits (see "Local memory
sizing" below; the 2026-07-06 128 KiB build in `build_results_2026-07-06/`
is SUPERSEDED, kept for history). The flow: synthesis of the harness top
(`shell_top.sv` = `u_top`) → the **static post-synth DCP**
(`shell_static_synth.dcp`, the artifact `fpga/dfx/build_dfx.tcl` consumes,
`rp_inst=u_rp_dut`) → **timing-closed implementation** (256 KiB build:
WNS +2.472 ns / WHS +0.024 ns / WPWS +2.500 ns, all constraints met; 0 DRC
errors, 4 benign warnings) → **bitstream** (inert-RP "ping-first" harness
image) → the **XSA with bitstream** for the Vitis firmware flow. Because the
256 KiB rebuild changes the static netlist, A2's DFX re-run mints a **new
static_id**. Board pin constraints live in `constraints/`
(provenance-tracked from the proven monolithic/proof XDCs). Evidence
reports + `RESULT.txt` land in `build_results_<date>/`; see "Build flow"
for the exact repro command and "Handoff" for what A2/A3 pick up from
here.

Ground truth for everything below:
- `docs/contracts/partition-pins.md` — the RP boundary (every signal named
  here is authoritative; do not invent new partition pins without an A6
  contract change).
- `docs/contracts/shell-regmap.md` — the AXI-Lite register blocks.
- `docs/ARCHITECTURE_SPEC.md` §4.1 (static shell contents), §5 (clock/reset),
  §8 (MAC-verification subsystem), §12 (reuse inventory).

## What the static shell contains

Per spec §4.1, the static shell is everything that must survive an RP
teardown — the DUT (RM) appearing/disappearing must never take the network
agent down. Block inventory, mapped to regmap base + reuse source:

| Block | Regmap base | Owner RTL (this repo) | Source / IP (spec §12) |
|---|---|---|---|
| Clock/reset unit | `CLKRST` @ `0x44A0_0000` | `fpga/shell/ip/clkrst/dut_clkrst.sv` | Xilinx Clocking Wizard (DRP-enabled MMCM) + custom AXI-Lite/reset-sequencer |
| DFX Decoupler + AXI Shutdown Manager + RP-reset gate | `DFXCTL` @ `0x44A1_0000` | `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` | Xilinx DFX Decoupler + DFX AXI Shutdown Manager (vendor IP) + custom AXI-Lite ctl/status |
| AXI HWICAP → ICAPE3 | `HWICAP` @ `0x44A2_0000` | — (vendor IP only) | Xilinx AXI HWICAP (PG134) |
| RMII virtual PHY + MDIO slave (register model) | `VPHY` @ `0x44A3_0000` | `fpga/ethernet/mdio_phy_model/` | freecores/sgmii `mdio_slave.v` + custom PHY register model (BMCR/BMSR/PHYID/ANAR) |
| Overlay store (QSPI A/B slots) | `OVLSTORE` @ `0x44A4_0000` | — (vendor IP; A/B-slot manager is firmware, `firmware/overlay_store/`) | Xilinx AXI Quad SPI driving SST26VF064B |
| Telemetry (INA228 I2C, watchdog status) | `TELEM` @ `0x44A5_0000` | `fpga/shell/ip/telem/telem.sv` (**real** CSR surface + `SIM_FAKE_DATA` bench mode; INA228 I2C engine = documented follow-up module `ina228_i2c_master` behind the sample seam — see block README) | Custom + INA228 |
| Error-inject gen/checker | `GENCHK` @ `0x44A6_0000` | `fpga/ethernet/gen_checker/` | cocotbext-eth (sim ref) / custom HW gen-checker |
| MicroBlaze (bare-metal) + lwIP | n/a (AXI-Lite *master*, not a regmap slave) | — (BD cell only, `shell_bd.tcl`) | Xilinx MicroBlaze; lwIP |
| Debug Bridge (`From_AXI_to_BSCAN`) + XVC | `DBGBR` @ `0x44A8_0000` (regmap v0.1 I6 — standard Debug Bridge layout, no custom fields) | — (BD cell only) | Xilinx Debug Bridge + `xvcServer` on MicroBlaze |
| SWD probe (v1 pin-wiggler) | `SWDBB` @ `0x44A7_0000` (regmap v0.1 I5) | `fpga/shell/ip/swd_bb/swd_bb.sv` (**real** — DRIVE write-through + 2-FF SAMPLE; firmware times the protocol) | Custom; v2 = ported CMSIS-DAP `SW_DP.c`/`DAP.c` (whole-block replacement) |
| Board wrapper (clock/reset from MPS3 SCC/MCC, XCKU115 pinout) | n/a | `fpga/shell/shell_top.sv` + `fpga/shell/constraints/mps3_harness*.xdc` | Adapt nanosoc Xilinx target + Arm MPS3 SMM wrapper |
| LAN9220 host I/F | n/a | `fpga/ethernet/lan9220_if/` | Xilinx AXI EMC (or small SMC master) + ported driver |
| 3-port L2 bridge | n/a (see open ambiguity below) | `fpga/ethernet/bridge/` | Forencich `axis_switch` + `eth_axis_rx` (verilog-axis / verilog-ethernet) |
| UART-over-Eth + SWO relay | `UARTBR` @ `0x44A9_0000` (regmap v0.1 I7) | `fpga/shell/ip/uart_bridge/uart_bridge.sv` (**real** — AXIS ⇄ register-FIFO bridge, 5 gray-pointer async FIFOs = the console-group partition-boundary CDC, NRZ SWO deserialiser) + `firmware/uart_over_eth/` (relay side) | Custom bridge + relay on MicroBlaze + host ser2net |
| Board GPIO/PMOD passthrough + host mux | `GPIO` @ `0x44AA_0000` (regmap v0.1 I4) | `fpga/shell/ip/board_gpio/board_gpio.sv` (**real** — IN/OUT/OE/OWN mux) | Custom |
| RM-load verify / watchdog | n/a (folds into `DFXCTL`/`TELEM` status bits + optional DFX Bitstream Monitor vendor IP) | `fpga/shell/ip/dfx_ctl/`, `fpga/shell/ip/telem/` | DFX Bitstream Monitor + custom + INA228 |
| (optional) MMIO bridge | n/a — v1+ deferred, not in the v0 partition-pin boundary | `fpga/shell/ip/mmio_bridge/` (future) | Custom AXI master relayed by MicroBlaze |
| (optional) DDR4 staging buffer | n/a — v2 only | — (future) | Xilinx MIG + AXI CDMA |

All six regmap-v0.1 custom shell IP blocks now have real RTL. The remaining
"(future)" rows above (`mmio_bridge`, DDR4 staging) are deliberate v1+/v2
deferrals per the partition-pin contract — directories stay absent rather
than filled with placeholder content that could drift from what A6/A3
actually need. The one intentional seam inside a real block is TELEM's
INA228 I2C master engine (follow-up module, contract documented in
`ip/telem/README.md`).

## BD assembly (`bd/shell_bd.tcl` + `shell_top.sv` + `build_shell.tcl`)

### Topology: `u_top = u_shell + u_rp_dut` (resolves the previous skeleton's open ambiguity #1)

`partition-pins.md` v0.1's I4 resolution states the RP is "a hierarchical
cell *alongside* the shell in one top design (`u_top` = `u_shell` +
`u_rp_dut`) ... fixes A2's RP instance path (`u_top/u_rp_dut`)" — a flat,
two-level hierarchy. So:

- `bd/shell_bd.tcl`'s `create_root_design` builds **only the static shell**
  and exposes the *entire* `partition-pins.md` v0.1 signal list as ordinary
  top-level BD ports (`rp_*` prefix, SECTION 0 of that file). From the BD's
  own point of view these are ports; once Vivado's auto-generated wrapper
  (`shell_bd_wrapper`) is instantiated one level up, they become **pins** on
  that cell instance — exactly the "BD pins between the two" the contract
  describes, no contradiction.
- `shell_top.sv` **is** `u_top`: it instantiates the shell BD wrapper as
  `u_shell` and `rp_dut` as a sibling `u_rp_dut`, wiring the two together
  1:1 by partition-pin name. This mirrors
  `fpga/dfx/proof/rp_shell_top.sv`'s own structure (plain top +
  directly-instantiated `rp_dut`) — the "plain top" half is now a real
  Vivado BD instead of hand-written always-blocks. **In this harness build
  `rp_dut` binds to `fpga/shell/rp_dut_stub.sv`** (same module name + exact
  partition-pins port list as `fpga/dfx/proof/rp_dut.sv`, every output tied
  inert, `rm_id` = ASCII `"STUB"` = `0x53545542`): a plain empty black box
  trips DRC INBB-3 at `opt_design` in a non-DFX flow (confirmed live). The
  instance carries `DONT_TOUCH` so the partition-pin boundary survives
  synth/opt intact — no constant propagation across it, no hierarchy
  dissolution — keeping the written checkpoint black-box-able for the DFX
  flow.
- **The RP cell path in the written checkpoints is the top-level cell
  `u_rp_dut`** (shell_top is the netlist top, so there is no literal
  `u_top/` prefix in Vivado cell paths) — pass `rp_inst=u_rp_dut` to
  `fpga/dfx/build_dfx.tcl`, exactly like the proof stand-in static that
  script's header already documents.
- `HD.RECONFIGURABLE` is **not** set anywhere in this tree. Neither
  `shell_bd.tcl` nor `shell_top.sv` nor `build_shell.tcl` mark `u_rp_dut`
  reconfigurable or apply `fpga/dfx/dfx_floorplan.xdc`'s Pblock — that is the
  separate DFX build's job (`fpga/dfx/`, a different workstream) when it
  re-opens this same static checkpoint to implement real per-RM partials.
  `build_shell.tcl` produces an ordinary static netlist sufficient for the
  harness XSA / Vitis firmware bring-up (and a complete monolithic harness
  bitstream when impl closes — the "ping-first" H-path).

### BD cell inventory

| Cell | VLNV / reference | Role |
|---|---|---|
| `microblaze_0` | `xilinx.com:ip:microblaze:11.0` | Bare-metal coordinator (MMU-less: `C_USE_MMU=0`); runs config_agent/xvc_server/swd_server/uart_over_eth/coordinator/clkrst/overlay_store firmware |
| `ilmb_v10`, `dlmb_v10`, `ilmb_bram_if_cntlr`, `dlmb_bram_if_cntlr`, `local_ram` | `lmb_v10:3.0`, `lmb_bram_if_cntlr:4.0`, `blk_mem_gen:8.4` | 256 KiB shared local program/data memory (`local_ram` `Write_Depth_A=65536`×32b; standard MMU-less MicroBlaze BSP pattern) |
| `mdm_1` | `mdm:3.2` | MicroBlaze JTAG/BSCAN debug module |
| `axi_intc_0` + `xlconcat_intr` | `axi_intc:4.1`, `xlconcat:2.1` | Interrupt controller; concats HWICAP/Timer/UARTLite/`eth_irq` |
| `axi_timer_0` | `axi_timer:2.0` | Coordinator timebase |
| `axi_uartlite_0` | `axi_uartlite:2.0` | MicroBlaze console (physical UART fallback, `uart_tx_f`/`uart_rx_f`) |
| `axi_interconnect_0` | `axi_interconnect:2.1` | 1 slave (MicroBlaze `M_AXI_DP`) → 14 masters (regmap + housekeeping + `clk_wiz_dut`'s AXI-Lite DRP slave) |
| `clk_wiz_shell` | `clk_wiz:6.0` | Fixed 100 MHz shell/AXI/ICAP clock + fixed 50 MHz RMII reference (2nd output) |
| `clk_wiz_dut` | `clk_wiz:6.0`, `ENABLE_DYNAMIC_RECONFIG=true` | DRP-reconfigurable DUT clock (spec §5; D12 default 50 MHz placeholder) |
| `proc_sys_reset_shell` | `proc_sys_reset:5.0` | Shell-domain reset (MicroBlaze/AXI/CSR `peripheral_aresetn`) |
| `dut_clkrst_0` | `soclabs.org:user:dut_clkrst` (`ip/clkrst/dut_clkrst.sv`) | CLKRST — generates the 3 DUT-domain resets itself (no separate `proc_sys_reset_dut` needed) |
| `dfx_ctl_0` | `ip/dfx_ctl/dfx_ctl.sv` | DFXCTL — decoupler/shutdown-mgr control, RM-load verify (`rm_id`/`dut_lockup` direct from the RP boundary ports) |
| `board_gpio_0` | `ip/board_gpio/board_gpio.sv` | GPIO — RP GPIO ⇄ board pads, host OWN-mux |
| `swd_bb_0` | `ip/swd_bb/swd_bb.sv` | SWDBB — pin-wiggler straight onto the RP's SWD partition pins |
| `telem_0` | `ip/telem/telem.sv` | TELEM — INA228 engine seam tied off this wave (`SIM_FAKE_DATA=0`), I2C pads brought to top-level BD ports for later |
| `uart_bridge_0` | `ip/uart_bridge/uart_bridge.sv` | UARTBR — DUT console/SWO AXI-Stream ⇄ FIFO bridge; UART1 present-but-unused, tied idle |
| `axi_hwicap_0` | `axi_hwicap:3.0` | HWICAP — partial-bitstream delivery to ICAPE3 |
| `dfx_decoupler_0`, `dfx_axi_shutdown_manager_0` | `dfx_decoupler:1.0`, `dfx_axi_shutdown_manager:1.0` | DFX isolation — control handshake wired to `dfx_ctl_0`; **the per-signal boundary itself needs GUI re-authoring**, see uncertainties |
| `axi_quad_spi_0` | `axi_quad_spi:3.2` | OVLSTORE — QSPI A/B overlay store (SST26VF064B); `C_USE_STARTUP=0` (never shares STARTUP with MCC boot config) |
| `debug_bridge_0` | `debug_bridge:3.0`, `C_DEBUG_MODE=2`, `C_NUM_BS_MASTER=1` | DBGBR — AXI→BSCAN, backs `xvc_server`; its `m0_bscan` master leaves as the 12 `rp_dbg_bscan_*` partition pins (11 straight through, `tdo` back through decoupler INTF 19, clamp 0) so an RM can carry its own debug hub + ILAs. The static must carry **no** debug slave (ILA/VIO/`mark_debug`): with a BSCAN master Vivado will not insert a static hub and instead wires a static ILA into the RP |
| `axi_emc_0` | `axi_emc:3.0` | LAN9220 host I/F — external `EMC_INTF` bus port + `eth_irq` input exposed; SRAM-async CONFIG |

**Deliberately NOT instantiated this wave** (out of scope — `fpga/ethernet/`
is owned by a different agent/workstream, W-RTL-ETH per
`docs/NEXT_WAVE_PLAN.md`): `mdio_phy_model` (VPHY), `rmii_phy_if`,
`link_partner_mac`, `eth_bridge_3port`, `lan9220_if`, `gen_checker`
(GENCHK). The RP's RMII/MDIO partition pins are tied to safe idle in
`shell_bd.tcl` SECTION 5 (mirrors `rp_shell_top.sv`'s own tie-off pattern) so
`validate_bd_design` is clean today; VPHY/GENCHK regmap addresses are
reserved (documented, no `assign_bd_address` call) until that subsystem
lands.

### Address map (shell-regmap.md v0.2 + shell-internal housekeeping)

All regmap bases are unchanged between v0.1 and v0.2 (the v0.2 changelog is
additive: UARTBR `SWO_CFG` @ +0x18 codified from the landed RTL, AXI-Lite
handshake-style note, CLKRST I16 status). 64 KiB pages throughout.

| Base | Block | Cell |
|---|---|---|
| `0x44A0_0000` | CLKRST | `dut_clkrst_0` |
| `0x44A1_0000` | DFXCTL | `dfx_ctl_0` |
| `0x44A2_0000` | HWICAP | `axi_hwicap_0` |
| `0x44A3_0000` | VPHY | *(reserved — not instantiated, W-RTL-ETH)* |
| `0x44A4_0000` | OVLSTORE | `axi_quad_spi_0` |
| `0x44A5_0000` | TELEM | `telem_0` |
| `0x44A6_0000` | GENCHK | *(reserved — not instantiated, W-RTL-ETH)* |
| `0x44A7_0000` | SWDBB | `swd_bb_0` |
| `0x44A8_0000` | DBGBR | `debug_bridge_0` |
| `0x44A9_0000` | UARTBR | `uart_bridge_0` |
| `0x44AA_0000` | GPIO | `board_gpio_0` |
| `0x44AB_0000` | MMCM_DUT_DRP (shell-internal) | `clk_wiz_dut/s_axi_lite` — 2024.1's `USE_DYN_RECONFIG` clk_wiz exposes control/status/DRP as one AXI-Lite slave (no native DRP pins); firmware drives the DUT-clock MMCM here. Not (yet) a contract base — flagged for A6/regmap v0.3 |
| `0x4120_0000` | (shell-internal) | `axi_intc_0` |
| `0x4060_0000` | (shell-internal) | `axi_uartlite_0` |
| `0x41C0_0000` | (shell-internal) | `axi_timer_0` |
| `0xC000_0000` (16 MiB) | (shell-internal) | `axi_emc_0` LAN9220 memory aperture |
| `0x0000_0000` (256 KiB) | (MicroBlaze-local) | `ilmb_bram_if_cntlr`/`dlmb_bram_if_cntlr` — shared I/D LMB BRAM |

Local memory sizing: **256 KiB** (`local_ram` `Write_Depth_A=65536`×32b) as
of the 2026-07-07 rebuild. Doubled from 128 KiB because A3's real lwIP +
firmware ELF measures 239,868 B and overflows 128 KiB by 53,200 B
(firmware/platform/BUILD_RESULT.txt) — the W-VITIS "one-parameter doubling":
`Write_Depth_A` 32768→65536 plus the two `assign_bd_address` ranges 128K→256K
(both LMB controllers map the same True-Dual-Port BRAM, ILMB=PORTA/DLMB=PORTB,
so Instruction and Data both see one 256 KiB window at 0x0). Cost: +32 RAMB36
tiles (32.5→64.5 total, 2.99% of the XCKU115's 2160), LUT/reg noise, timing
still closes (WNS +2.472 ns). The 256 KiB ELF then fits with ~22 KiB headroom.

### IP packaging route (chosen + why)

The W-BD brief offered two routes for getting the six custom
`soclabs.org:user:*` blocks onto the BD: (a) package each as an IP-XACT
component, or (b) `create_bd_cell -type module -reference` (module
reference, no packaging). **Route (b) is not available for this RTL** —
confirmed live on 2024.1: `-type module -reference` hard-refuses a
SystemVerilog top file (`ERROR: [filemgmt 56-195] ... type is not allowed
as the top file in the reference`), and these blocks genuinely use SV
(`logic`/`always_ff`). So route (a) it is:
`ip_packaged/package_csr_ip.tcl` packages each block via
`ipx::package_project -import_files` into a local IP repo
(`fpga/shell/ip_packaged/`, per-IP output gitignored, the recipe committed).
`-import_files` alone auto-infers the AXI4-Lite bus interface from the
`s_axi_*` naming convention — with LOWERCASE inferred names (`s_axi` bus
interface, `reg0` address block), which is what `shell_bd.tcl` SECTION 3/6
reference. `build_shell.tcl`/`validate_bd.tcl` both package BEFORE sourcing
`shell_bd.tcl` and point `ip_repo_paths` at the repo.

### Build flow

```
# full build: package IP -> BD -> validate -> synth -> STATIC DCP + XSA
#             -> (best-effort) impl -> bitstream -> XSA with bit
vivado -mode batch -source fpga/shell/build_shell.tcl \
    -log build/shell_build.log -journal build/shell_build.jou \
    -tclargs build/shell_proj shell_bd

# fast BD-validate-only iteration (~2-3 min)
vivado -mode batch -source fpga/shell/validate_bd.tcl \
    -tclargs build/shell_validate_proj shell_bd
```

`build_shell.tcl`: creates the 2024.1 project for `xcku115-flvb1760-1-c` →
packages the six CSR blocks (step 1b) → adds `rp_dut_stub.sv` +
`shell_top.sv` → sources `shell_bd.tcl`, `validate_bd_design`, dumps the BD
cell/address evidence → `make_wrapper` + sets `shell_top` as top → adds
`constraints/*.xdc` (`mps3_harness_timing.xdc` marked
implementation-only) → `launch_runs synth_1` → **writes
`<proj>/shell_static_synth.dcp` + post-synth reports + post-synth XSA (no
bit)** → `launch_runs impl_1 -to_step write_bitstream` (best-effort: an impl
failure does not destroy the synth artifacts) → on success, post-impl
timing/utilization/DRC/IO reports + `write_hw_platform -fixed -include_bit`
to `<proj>/shell_harness.xsa`. Reports land in
`fpga/shell/build_results_<date>/` (small text, commit-worthy per the W-BD
gate); heavyweight outputs stay in gitignored `build/`.

### Constraint provenance (`constraints/`)

- `mps3_harness.xdc` — pins only, used in synth+impl. Every group is copied
  from / cross-checked against three already-agreeing sources:
  `fpga/monolithic/nanosoc_mps3.xdc` (this repo's W1 port of the legacy
  `fpga_pinmap.xdc`), the legacy `nanosoc_arch_tech` `fpga_pinmap.xdc`
  itself (read-only reference), and `fpga/dfx/proof/proof.xdc` (the
  HW-verified clock/reset/LED subset). Groups: OSCCLK1 (50 MHz), USER_nPB0,
  USER_nLED[7:0] + USER_SW[7:0] (board_gpio pads — see mapping below),
  LAN9220 SMC (ETH_nCS/ETH_nOE/ETH_INT + SMBF_*), MB console UART (FT4232
  channel 1; channel 0 = MCC-reserved, channel 2 = legacy DUT-console pins,
  kept reserved), QSPI overlay flash (constrained + wired, but **flagged
  D15**: shared with the MCC's config path — firmware keeps it idle until
  resolved), and the CFGBVS/CONFIG_VOLTAGE/BITSTREAM properties from the
  monolithic XDC — with one deliberate deviation: **PERSIST=NO** (platform
  invariant C3: PERSIST is mutually exclusive with HWICAP/ICAP use and
  trips DRC PRST-1 without a CONFIG_MODE — hit live at place_design
  2026-07-06; same fix as the DFX proof, fbc9025 / build_dfx.tcl).
- `mps3_harness_timing.xdc` — implementation-only. References the real
  clk_wiz-generated clocks (shell 100 MHz ↔ DUT MMCM declared
  asynchronous — the CDC structures own those crossings); all board I/O is
  false-path-excepted with per-group justification (async/quasi-static at
  the pad; see the file's "I/O timing philosophy" header). The LAN9220 AC
  budget is NOT a pad setup/hold problem — it is enforced by `axi_emc_0`'s
  programmed bank timing (`C_TCEDV/TAVDV=50ns, THZCE/THZOE=25ns, TWC=100ns,
  TWP=50ns`, set in `shell_bd.tcl` from the datasheet-derived table in
  `fpga/ethernet/lan9220_if/README.md`; the core DEFAULTS of 15 ns were
  faster than the LAN9220's 30 ns read-data-valid spec — a real HW bug
  caught and fixed in the first full build pass).

### shell_top.sv board wiring (confirmed against the generated wrapper)

- **EMC → LAN9220**: `EMC_INTF` flattened names confirmed from the real
  `shell_bd_wrapper.v` (`addr[31:0]`, `ce_n`, `oen`, `wen`, `dq_io[15:0]`
  with wrapper-internal IOBUFs, `wait` tied 0; SRAM-unused outputs
  unconnected). `SMBF_ADDR = addr[7:1]` (EMC mem_a is a byte address; the
  LAN9220 window is 16-bit-word addressed) — **flagged**: confirm on
  hardware via the LAN9220 `BYTE_TEST` register (0x64 → `0x87654321`).
  `ETH_nOE` and `SMBF_nOE` both driven from `oen` (two distinct pads in the
  legacy pinmap; no phase relationship invented). `SMBF_FIFOSEL=0` (full
  address decode), `SMBF_nRST=sys_rst_n` (firmware owns datasheet-timed
  soft-reset). `ETH_INT → eth_irq` — **flagged**: LAN9220 IRQ defaults to
  active-low open-drain; firmware must program `IRQ_CFG` (IRQ_POL=1,
  push-pull) before unmasking in the AXI INTC.
- **board_gpio pads**: `[7:0]` → `USER_nLED` (LED on = pad owned+driven
  high; readback loops the driven value), `[15:8]` ← `USER_SW`. Documented
  here as the pad-map contract for A3's board_gpio firmware.
- **Dropped ports** (do-not-invent-pins rule): TELEM I2C (MPS3 power
  monitors are MCC-owned; `i2c_sda_i` tied high, outputs unconnected — the
  BD seam stays) and the 20-pin CoreSight header (no partition-pin route in
  the contract — open A6 gap, unchanged).

### Remaining flags / deferred (ranked)

1. **`dfx_decoupler_0`'s per-signal boundary** — only the
   `decouple`/`decouple_status` handshake is wired (real 2024.1 pin names,
   confirmed live); the ~20-signal partition-pin pass-through/tie-off
   boundary still needs PG294 per-signal configuration (GUI or
   fully-specified `CONFIG.GUI_*`/`CONFIG.ALL_PARAMS` Tcl) in a follow-up.
   Isolation today comes from `dut_clkrst_0`'s reset gating +
   `dfx_ctl_0` sequencing.
2. **QSPI/MCC arbitration (D15)** and **LAN9220 AC timing against the
   primary datasheet** — headless-unverifiable board facts; QSPI is wired +
   flagged idle, and the EMC bank timing is set to the conservative
   README-derived values (primary "AC Characteristics" table still
   unverified, `lan9220_if/README.md` flag).
3. **EMC address bit alignment** (`BYTE_TEST` check above) and **ETH_INT
   polarity** — firmware-visible, one-register checks at bring-up.
4. RP Pblock sizing (D7) and DUT-clock default frequency (D12) remain open
   per `docs/ARCHITECTURE_SPEC.md` §15 — placeholders only (50 MHz).
5. ~~Local RAM 128 KiB vs lwIP appetite~~ — **RESOLVED 2026-07-07**: doubled
   to 256 KiB (see "Local memory sizing" above); the real firmware ELF now
   fits with ~22 KiB headroom.
6. VPHY/GENCHK (W-RTL-ETH) not instantiated; bases reserved.

## DFX / CDC rules this shell must obey

These are load-bearing — the real `ip/` blocks now enforce their share of
them (rules 3/4 concretely: `clkrst`'s reset generators, `uart_bridge`'s
async FIFOs, the per-block 2-FF input synchronizers); the BD/constraints
side still only documents its share:

1. **No shell↔DUT AXI in v0.** Every signal crossing the RP boundary is a
   slow scalar or low-rate AXI-Stream — see `partition-pins.md` line 8. Don't
   let a future block sneak an AXI(-Lite) master/slave across that boundary
   without an A6 contract change.
2. **All DUT clocks are generated in the static shell** (DRP MMCM in
   `clkrst/dut_clkrst.sv` driving a `clk_wiz` BD cell) and enter the RP only
   as partition pins (`dut_clk`). No clock generation inside the RM
   (`partition-pins.md` "Clock/reset domain rule"; spec §5).
3. **Every partition-pin crossing is CDC'd on the static side.** The RM sees
   already-safe signals; the shell owns synchronizers/async FIFOs. Keep the
   debug hub / ILA on an always-on static clock regardless of what the DUT
   clock is doing (spec §5, §16).
4. **Three resets, all shell-driven, asserted asynchronously / deasserted
   synchronized** to the destination domain (`dut_resetn`, `rp_resetn`,
   `dbg_resetn` — all in the `dut` clock domain per the contract's signal
   table). See `clkrst/dut_clkrst.sv`.
5. **RP-facing AXI must route through the AXI Shutdown Manager** — a hung or
   absent RM must never wedge the MicroBlaze/Ethernet/ICAP/console static
   logic (spec §16, "Static shell must survive RP teardown"). Owned by
   `dfx_ctl/dfx_ctl.sv` + the `dfx_axi_shutdown_manager` BD cell.
6. **Decoupling is mandatory before/through/after a partial load** — assert
   `DFXCTL.DECOUPLE` and hold `rp_resetn` low before streaming a partial via
   HWICAP; release only after RM-load verify passes (spec §7, §13).
7. **XCKU115 is a 2-SLR SSI device** — keep the RP Pblock within a single SLR
   to start (spec §16, D7). Floorplanning is an A6/`fpga/dfx/`-build concern,
   not this directory's — `u_rp_dut` lives in `shell_top.sv` (a sibling of
   the shell BD, per the I4 topology decision above), not inside
   `shell_bd.tcl`, and `dfx_floorplan.xdc`'s Pblock is applied by that
   separate DFX build, not by anything in `fpga/shell/`.
8. **IOB packing / HDPR-29 lesson (Z2):** pin-facing (or partition-boundary-
   facing) output registers that want `IOB TRUE` must live in the static
   shell, never in the RM — OLOGIC/pad-adjacent sites are static-only in a
   DFX design (`partition-pins.md` "IOB packing note"). **The RMII TX
   re-register stage lives in `fpga/ethernet/rmii_phy_if/rmii_phy_if.sv`**,
   immediately downstream of the `phy_rmii_txd` / `phy_rmii_tx_en` partition
   pins — see that subdirectory's README for the detail (this MPS3 build has
   no physical RMII pad since the virtual PHY is fabric-only, but the same
   DFX placement rule still applies to the first flop stage after the RP
   boundary, and matters again if a physical PHY is ever added downstream).
9. **XVC can't detect an RP swap.** The gate is **firmware**, not `dfx_ctl`:
   `g_shell_state.xvc_gated` is set in `swap_fsm.c` `step_gate()` and cleared
   only at DONE of `step_cache_clearing()` or on FAILED. While gated,
   `getinfo:`/`settck:` are answered and a `shift:` **stalls** (it is not
   dropped); the TCP connection survives the swap and the stalled shift then
   runs against the **new** RM (`firmware/coordinator/swap_fsm.c`,
   `firmware/xvc_server/xvc_server.c`). Nothing in this directory's RTL
   generates or sees that gate. With RM-side ILAs (the `dbg_bscan_*` group
   below) a swap therefore invalidates the XVC session: the host closes the
   Vivado target before the swap and reopens it + loads the new RM's `.ltx`
   after DONE (`host/pyverify`).

## Handoff

**To A2 (DFX flow, `fpga/dfx/build_dfx.tcl`):**
- Static shell input (CURRENT, 256 KiB LMB):
  **`build/shell_proj_256k/shell_static_synth.dcp`** (post-synth, written by
  `build_shell.tcl`; gitignored — regenerate with the repro command in
  "Build flow"). Supersedes the 128 KiB `build/shell_proj_a1/…` from the
  2026-07-06 build. **This is a NEW static netlist → your DFX re-run mints a
  new static_id.**
- `rp_inst` tclarg: **`u_rp_dut`** (top-level cell — `shell_top` is the
  netlist top, so no `u_top/` prefix appears in Vivado cell paths; same
  convention as the proof stand-in that `build_dfx.tcl`'s header already
  documents).
- The cell contains the DONT_TOUCH'd `rp_dut_stub` contents, boundary
  intact — black-box/carve it before linking RM checkpoints
  (`update_design -cells u_rp_dut -black_box` or the flow's clearing step).
  Port list is exactly `partition-pins.md` v0.1 == `fpga/dfx/proof/rp_dut.sv`.
- `HD.RECONFIGURABLE` + `dfx_floorplan.xdc` Pblock are yours to apply; this
  tree never sets them.

**To A3 (Vitis firmware, `firmware/`):**
- XSA (CURRENT, 256 KiB LMB): **`build/shell_proj_256k/shell_harness.xsa`**
  (`write_hw_platform -fixed -include_bit`; the 2026-07-07 impl closed so it
  INCLUDES the bitstream — see `build_results_2026-07-07-256k/RESULT.txt`).
  Supersedes the 128 KiB `build/shell_proj_a1/shell_harness.xsa`. Rebuild the
  Vitis platform/BSP from THIS xsa. `platform create -hw shell_harness.xsa`
  (bare-metal MicroBlaze `microblaze_0`, **256 KiB local BRAM at 0x0** — the
  `LMB_KB=256` ELF firmware/platform already builds now fits, ~22 KiB
  headroom).
- Addresses = shell-regmap.md v0.2 (`platform_regs.h` already matches) +
  shell-internal: UARTLITE `0x4060_0000`, INTC `0x4120_0000`, TIMER
  `0x41C0_0000`, LAN9220 window `0xC000_0000` (16 MiB), DUT-clock MMCM
  AXI-Lite DRP `0x44AB_0000`.
- INTC inputs: In0=HWICAP ip2intc_irpt, In1=axi_timer, In2=axi_uartlite,
  In3=eth_irq (LAN9220 `ETH_INT` — program `IRQ_CFG` IRQ_POL=1/push-pull
  BEFORE unmasking).
- LAN9220 bring-up checks: `BYTE_TEST` @ +0x64 == `0x87654321` (validates
  the EMC address alignment flag), then ID_REV. `SMBF_nRST` follows the
  board POR button; soft-reset timing is firmware's.
- board_gpio pad map: pads[7:0]=LEDs (active-high drive → LED on),
  pads[15:8]=DIP switches.
- OVLSTORE QSPI: wired + pinned but **keep idle until D15 (MCC config-flash
  arbitration) is resolved**.

## Layout

```
fpga/shell/
├── README.md            (this file)
├── shell_top.sv          harness top (`u_top`): instantiates the shell BD
│                         wrapper (`u_shell`) + rp_dut as a sibling
│                         (`u_rp_dut`, DONT_TOUCH), board-facing physical
│                         ports matching constraints/mps3_harness.xdc 1:1
├── rp_dut_stub.sv        synthesizable inert RP stub (rm_id="STUB") — this
│                         harness build only; the DFX flow keeps the empty
│                         black box fpga/dfx/proof/rp_dut.sv instead
├── build_shell.tcl       package IP -> BD assemble/validate -> wrapper ->
│                         synth -> static DCP + XSA -> impl/bitstream ->
│                         XSA(+bit); evidence -> build_results_<date>/
├── validate_bd.tcl       fast validate-only driver (same packaging + BD
│                         steps, stops after validate_bd_design)
├── build_results_<date>/ committed build evidence (bd_summary, reports,
│                         RESULT.txt)
├── ip_packaged/
│   ├── package_csr_ip.tcl the committed packaging recipe (ipx::package_project)
│   └── <block>/           per-IP IP-XACT output (gitignored, regenerated)
├── bd/
│   └── shell_bd.tcl      ASSEMBLED+VALIDATED Vivado BD (real connect_bd_net /
│                         connect_bd_intf_net / assign_bd_address calls) —
│                         MicroBlaze subsystem, HWICAP, DFX decoupler +
│                         shutdown mgr, AXI EMC, clk_wiz pair, and the six
│                         custom CSR cells, wired to the RP partition-pin
│                         boundary (exposed as this BD's own top-level ports)
├── constraints/
│   ├── mps3_harness.xdc         pins (synth+impl) — provenance-tracked
│   └── mps3_harness_timing.xdc  timing (impl-only)
└── ip/
    ├── board_gpio/
    │   └── board_gpio.sv        GPIO regmap: board-port passthrough + OWN mux
    ├── clkrst/
    │   └── dut_clkrst.sv        CLKRST regmap: DRP DUT clock + 3-reset scheme
    ├── dfx_ctl/
    │   └── dfx_ctl.sv           DFXCTL regmap: decouple/shutdown/RP-reset gate
    ├── swd_bb/
    │   ├── README.md
    │   └── swd_bb.sv            SWDBB regmap: SWD pin-wiggler (remote_bitbang backend)
    ├── telem/
    │   ├── README.md
    │   └── telem.sv             TELEM regmap: telemetry CSRs (INA228 engine seamed)
    └── uart_bridge/
        ├── README.md
        ├── uart_bridge.sv       UARTBR regmap: AXIS ⇄ FIFO console bridge (top)
        ├── uartbr_async_fifo.sv gray-pointer dual-clock FIFO (the console CDC)
        └── swo_uart_rx.sv       SWO 8N1/NRZ deserialiser (programmable divisor)
```
