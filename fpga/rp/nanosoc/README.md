# `rp/nanosoc` — the Reconfigurable Module (RM) wrapper

**Status: FILLED (2026-07-04, A1/W1).** The real single-core nanoSoC (module
`nanosoc`, from the read-only `~/SoCLabs/nanosoc_m0_soc`
checkout) is now instantiated and wired to the partition-pin boundary.
`rm_id` is the real, permanent constant `32'h0000_0001` (matches
`fpga/dfx/rm_list.tcl`'s `RM_LIB(rm_nanosoc,rm_id)` entry), not a placeholder.

This is the DUT side of the DFX partition (spec §4.2): the implementation
that gets swapped into the RP over Ethernet via `AXI HWICAP`. Its ports are
**exactly** the RP side of `docs/contracts/partition-pins.md` v0.1 — that
contract is written to mirror "the existing nanosoc FPGA wrapper ports
(proven on Z2)... so the RM wrapper (`fpga/rp/nanosoc/`) is a re-plumb, not a
redesign" (contract line 10-11), and that's what this fill is: a re-plumb of
the proven `nanosoc_vivado_wrapper.v` (PYNQ-Z2) pattern onto this contract's
boundary, not new SoC integration work.

## Layout

```
fpga/rp/nanosoc/
├── README.md               (this file)
├── rp_nanosoc_wrapper.sv    RM top: instantiates `nanosoc`, wires the partition-pin boundary
├── uart_axis_shim.sv        UART serial <-> AXI-Stream byte shim (console bridge)
├── nanosoc_ooc.xdc          socketed-XDC OOC timing constraints (partition-timing.md)
├── ooc_synth.tcl            OOC synth driver (reads nanosoc_ooc.xdc if present)
└── filelist.tcl             OOC-synth file list: this dir's sources + nanosoc_FPGA.flist
```

## Directions — mirror image of the shell's view

`partition-pins.md` (v0.1) states directions **from the shell's view**. This
wrapper is on the other side of the boundary, so every direction is
inverted. Concretely, by contract signal group:

**Inputs to this wrapper (shell drives, 15 signals):** `dut_clk`,
`dut_resetn`, `rp_resetn`, `dbg_resetn` (clocks & resets); `swd_clk`,
`swd_dio_o`, `swd_dio_oe` (SWD); `phy_rmii_ref_clk`, `phy_rmii_crs_dv`,
`phy_rmii_rxd[1:0]`, `mdio_i` (Ethernet); `uart_rx_tdata[7:0]`,
`uart_rx_tvalid`, `uart_tx_tready` (console); `dut_gpio_i[15:0]`
(board-port passthrough, I4).

**Outputs from this wrapper (RM drives, 15 signals):** `swd_dio_i` (SWD);
`phy_rmii_txd[1:0]`, `phy_rmii_tx_en`, `mdc`, `mdio_o`, `mdio_oe`
(Ethernet); `uart_tx_tdata[7:0]`, `uart_tx_tvalid`, `uart_rx_tready`, `swo`
(console/trace); `rm_id[31:0]`, `dut_lockup`, `irq_out` (status);
`dut_gpio_o[15:0]`, `dut_gpio_oe[15:0]` (board-port passthrough, I4).

## v0 DUT = single-core nanosoc (I1, resolved)

`docs/ARCHITECTURE_SPEC.md` §3 describes the DUT as a **single-core**
"SoC Labs nanosoc... Arm Cortex-M0 microcontroller SoC" with one UART
(`cmsdk_apb_usrt`) and one MAC-shaped group. This is now confirmed against
the real RTL (`docs/nanosoc_m0_soc/*.md`, N1-N3 survey + this fill): one
Cortex-M0 (SoC Labs `slcorem0` wrapper around Arm's real Cortex-M0 RTL,
resolved via the read-only `$ARM_IP_LIBRARY_PATH`), **no Ethernet MAC
anywhere** in the port list or sub-hierarchy, one console UART (CMSDK
UART2, raw serial, not AXI-Stream — see "Console" below), one SW-DP. The
dual-core / IPC / 2nd-UART `nanosoc-multicore-system` variant this
contract's signal-naming lineage traces back to remains **deferred** to a
later, larger `rm_nanosoc_multicore` RM (ethernet effort) — not built here.

## Wiring decisions

### Clock
`dut_clk` connects directly to `nanosoc.sys_clk` — no clock generation
inside the RM (contract "Clock/reset domain rule"). nanosoc has no
`SYS_CLK_FREQ_HZ`-shaped parameter on this particular module (confirmed by
reading the full parameter list), so there's nothing to override at the
nanosoc-instance level for a given `dut_clk` frequency — but see "Open:
clock frequency" below for why the *shim's* baud generator still needs to
know it.

### Reset combination
nanosoc has **exactly one** reset input, `sys_sysresetn` (confirmed by
reading the full port list — no `dbg_resetn`/`rp_resetn`-shaped port exists
anywhere on this module). The three contract resets are ANDed together:
```
sys_sysresetn_i = dut_resetn & rp_resetn & dbg_resetn;
```
Any of the three asserting resets the whole core. This means `rp_resetn`'s
"held through swap" semantics and `dbg_resetn`'s "just the debug logic"
semantics both collapse onto a single whole-core reset for this DUT — see
"A6 gaps" below, items 1-2.

### SWD
`swd_clk` -> `cpu_0_swclk`, `swd_dio_o` -> `cpu_0_swdi` (shell's driven value
into the DUT). `swd_dio_i` (back to the shell) is a mux:
```
swd_dio_i = cpu_0_swdoen ? cpu_0_swdo : swd_dio_o;
```
i.e. reflect nanosoc's own driven value when the DUT is driving, and
reflect the shell's own driven value back otherwise — matching a real
shared SWDIO wire's electrical behaviour. `swd_dio_oe` (input from the
shell, "host is driving") is not consumed anywhere — nanosoc's SW-DP has no
such input; 2-pin SW-DP turnaround is timed by the protocol itself. See "A6
gaps" item 3.

### Console — UART <-> AXI-Stream shim
nanosoc's console is CMSDK UART2, a **raw serial** peripheral muxed onto
GPIO port P1 (`p1_out[5]`=TXD, `p1_in[4]`=RXD), not an AXI-Stream interface.
`uart_axis_shim.sv` (deliverable 2) bridges it to the contract's
`uart_tx_*`/`uart_rx_*` AXIS pins:
- **TX (DUT -> host):** deserializes `p1_out[5]` (gated by `p1_outen[5]`,
  idle-high — same `uart_txd_int = p1_outen[5] ? p1_out[5] : 1'b1` idiom as
  the proven `nanosoc_vivado_wrapper.v`) into bytes on `uart_tx_tdata/tvalid`,
  handshaking with `uart_tx_tready`.
- **RX (host -> DUT):** serializes `uart_rx_tdata` (on `uart_rx_tvalid` /
  `uart_rx_tready`) onto `p1_in[4]`.

**RX is wired for contract conformance, but CONFIRMED NOT TO REACH THE CPU
in the current RTL** — this is not a limitation of the shim, it's an
existing gap in nanosoc's own pin-mux, independently re-traced for this task
(not just carried over from `docs/nanosoc_m0_soc/PLATFORM_MAPPING.md`,
though it corroborates that doc exactly):
- `nanosoc.sv` -> `nanosoc_ss_hostio4` (`P1_IN`) -> `SYS_P1_IN` is a genuine
  passthrough of the real pad value (`nanosoc_ss_hostio4.v:214`:
  `assign SYS_P1_IN[4] = (FT1248MODE) ? P1_IN[4] : SYS_P1_OUT_MUX[5];`).
- Inside `nanosoc_ss_systemctrl.v`, that real value is wired only to the
  plain-GPIO readback register — **`nanosoc_pin_mux`'s own `p1_in` port is
  left unconnected** (`.p1_in ( )`), and UART2's actual `uart2_rxd` net is
  produced entirely from `nanosoc_pin_mux.v`'s internal
  `p1_out`/`p1_outen` feedback (`nanosoc_pin_mux.v:95,156`):
  `assign uart2_rxd = p1_in[4];` where that local `p1_in[4]` is itself
  `assign p1_in[4] = p1_out_en_mux[4] ? p1_out_mux[4] : 1'b1;` — i.e.
  `uart2_rxd` is hard-tied to `1'b1` (idle) whenever bit 4 is configured as
  an input (the correct RX configuration), **completely independent of the
  external pad**.
- Net effect: today, `uart_rx_*` bytes sent by the shell/host are shifted
  correctly onto `p1_in[4]` by the shim, but nanosoc's UART2 peripheral
  never sees them. This is an upstream RTL gap in `nanosoc_m0_soc`
  (read-only to this task) — wiring it here is still correct/required for
  contract conformance and is what any future upstream fix needs connected.

**Also required, not optional — FT1248/ADP self-drain on the rest of P1.**
P1 is entirely consumed by CMSDK UART2 + the hostio4/FT1248 controller; it
is NOT available for the `dut_gpio_*` board-port group (see below). Without
draining the FT1248 handshake lines, the stage-0 BOOTROM's boot banner
(printed over SoCDebug USRT2 via the FT1248 controller) backs up waiting for
a host that isn't there and **hangs the boot**. `rp_nanosoc_wrapper.sv`
replicates the proven `nanosoc_vivado_wrapper.v` (PYNQ-Z2) tie pattern
bit-for-bit on `p1_in[6:0]`/`p1_in[15:8]`, substituting the shim's
serializer output for P1[4] and the raw-pin loopback for the other bits.

### Board-port / GPIO passthrough (I4)
`dut_gpio_o/oe/i` map 1:1 onto nanosoc's GPIO **port 0** (`p0_*`, 16 bits —
exact width match with `NGPIO=16`, the cleanest group in the whole contract).
**P1 is NOT available here** — see above, it's fully consumed by UART2/
FT1248. `NGPIO` is exposed as a Verilog `parameter int NGPIO = 16` per
`fpga/dfx/rms/README.md`'s RM authoring contract item 2 (matching
`rm_greybox`/`rm_led`).

### Ethernet — RMII + MDIO
Tied off inert exactly as `rm_greybox`/`rm_led` do. Single-core nanosoc has
**no MAC anywhere** in its port list or sub-hierarchy (confirmed by a full
read of `nanosoc.sv`: zero `rmii`/`mdio`/`eth`/`mac` tokens). A MAC-in-
operation DUT needs the separate eth-ss subsystem (`rm_eth_ss`), out of
scope here.

### rm_id
`32'h0000_0001`, matching `fpga/dfx/rm_list.tcl`'s
`RM_LIB(rm_nanosoc,rm_id)` entry.

## Known nanosoc.sv divergence (found while wiring this RM, 2026-07-04)

`nanosoc_m0_soc` currently contains **two, port-incompatible copies** of the
`nanosoc` module:

| | `nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv` (used by this wrapper) | `build_soc/rtl/nanosoc.sv` (regenerated snapshot) |
|---|---|---|
| SPI (PL022) top ports | **None** — no `spi_sclk/ss/mosi/miso` at all | Present, 4 extra output/input ports |
| `exp_*` AHB port direction | Ordinary AHB-master (`exp_hsel`/`haddr`/... are **outputs**, `exp_hreadyout`/`hresp`/`hrdata` are **inputs**) | **Inverted** (`exp_hsel`/... are **inputs**) |
| Extra parameters | — | `SYS_CLK_FREQ_HZ`, `QSPI_FLASH_PRESENT`, `IMEM_0_RAM_PRELOAD`, `ACCELERATOR_SUBSYSTEM` |
| Regeneration timestamp | earlier | later (2026-07-03 18:09:04 — same batch as the rest of `build_soc/`) |

This task explicitly pointed at the first file, and `rp_nanosoc_wrapper.sv`
is written against it. The problem: `nanosoc_FPGA.flist` (the flist this
task also explicitly named for `filelist.tcl`) transitively resolves the
`nanosoc` top from the **second** file
(`nanosoc_FPGA.flist` -> `nanosoc_ip.flist` -> `-f build_soc/flist/
nanosoc_toplevel.flist` -> `build_soc/rtl/nanosoc.sv`) — i.e. the flist and
this wrapper would NOT naturally agree on what `nanosoc` even looks like.

This is the same *class* of drift `docs/nanosoc_m0_soc/RTL_ANATOMY.md`
already flagged one level up (`chip/chip/verilog/nanosoc_chip.v` vs.
`build_soc/rtl/nanosoc_chip.v`), rediscovered independently on `nanosoc.sv`
itself, and — like that one — is **live upstream drift within a read-only,
actively-regenerated tree**, not a one-off. `filelist.tcl` handles it by
substituting the canonical file for the regenerated one by default (see its
"KNOWN nanosoc.sv DIVERGENCE" comment block for the exact mechanism and the
`RM_NANOSOC_USE_REGENERATED_TOP` escape hatch), so the fileset it assembles
is actually consistent with the wrapper shipped alongside it. **If
`nanosoc_m0_soc` regenerates again and the two copies reconverge or diverge
differently, re-check this.**

## Known gap: `nanosoc_gen`'s soc_glue helpers are incomplete in both available worktrees

`filelist.tcl` was dry-run-tested (`tclsh`, no Vivado — see that file's own
header) against both `nanosoc_gen` worktrees present in this lab
(`~/SoCLabs/temp/nanosoc-gen-constraints`,
`~/SoCLabs/temp/nanosoc-gen-tests`). **Neither has**
`rtl/soc_glue/soc_glue_mux2.sv` or `rtl/soc_glue/soc_glue_reset_sync.sv`,
both of which the current `build_soc/flist/nanosoc_toplevel.flist` lists as
required. This is a real, concrete blocker for an actual OOC synth run
(`filelist.tcl` fails loudly on it rather than silently proceeding) — not
something introduced by this RM, and not fixable from here (`nanosoc_gen` is
a sibling tool repo, out of this task's write scope). The integrator needs a
`nanosoc_gen` checkout with both files present before running Vivado against
this filelist.

## `A6` gaps — 6 top-level partition pins with no clean nanosoc port

Per the task brief's own count (matching
`docs/nanosoc_m0_soc/PLATFORM_MAPPING.md`'s "Top-port gaps" bucket, 6
signals):

1. **`rp_resetn`** — no separate nanosoc reset input; folded into the
   combined `sys_sysresetn` AND-tree (see "Reset combination" above).
2. **`dbg_resetn`** — same; folded into the same AND-tree, so "reset just
   the debug logic" is not distinguishable from a full core reset on this
   DUT.
3. **`swd_dio_oe`** — not consumed anywhere; nanosoc's SW-DP has no "host is
   driving" input. Informational-only for the shell's own pad-tristate
   arbitration; safe to leave unconnected.
4. **`swo`** — tied `1'b0`. Structurally N/A: nanosoc's CPU is Cortex-M0
   (no ITM/SWO — that's an M3/M4/M33 feature), not just unimplemented.
5. **`dut_lockup`** — tied `1'b0`. `cpu_0_lockup` is a REAL output of
   `nanosoc_ss_cpu` one level down, wired internally into
   `nanosoc_ss_systemctrl`'s auto-reset-on-lockup logic, but never routed to
   a `nanosoc.sv` top-level port. Cheapest fix is upstream (add one output
   port to the generator template), not something this wrapper can route
   without modifying read-only RTL.
6. **`irq_out`** — tied `1'b0`. Same non-exposure pattern:
   `cpu_0_sleeping`/`cpu_0_sleepdeep`/`cpu_0_txev` exist as real
   `nanosoc_ss_cpu` outputs but none reaches a `nanosoc.sv` top port either.

## Open: clock frequency

Neither `dut_clk`'s actual frequency (a static-shell DRP MMCM decision) nor
a firmware-side UART2 baud divider setting has been fixed by A6 yet
(`docs/nanosoc_m0_soc/PLATFORM_MAPPING.md` "Gaps/decisions for A6" item 8).
`uart_axis_shim.sv` exposes both as parameters (`CLK_HZ` default 25 MHz —
the one FPGA-proven nanosoc operating point in this codebase, PYNQ-Z2;
`BAUD` default 115200) precisely so this can be overridden at
RM-instantiation time (`rp_nanosoc_wrapper`'s own `UART_CLK_HZ`/`UART_BAUD`
parameters pass through) without touching either .sv file once the shell's
real clock is decided.

## Floorplan / DFX notes (spec §16, §7)

- Keep this RM within a **single SLR** to start — XCKU115 is a 2-SLR SSI
  device (D7, "measure nanosoc first" for the actual Pblock size).
- This wrapper is the *real* RM. The **greybox** variant (tied-off stub,
  legal fabric, inert) required per §8A.3 for the initial/default boot path
  now **exists** — `fpga/dfx/rms/rm_greybox/rm_greybox.sv` (hand-authored
  tie-off, I19 resolved), proven against `rm_led` in the
  `fpga/dfx/proof/` Vivado 2024.1 `pr_verify` run.
- `RESET_AFTER_RECONFIG` (spec §7) does **not** apply here — it is a
  7-series-only property (IMPLEMENTATION_PLAN.md correction **C1**). On
  UltraScale the equivalent robustness comes from `SNAPPING_MODE ON` + the
  decoupler + shell-held `rp_resetn` + the **clearing-bitstream** sequence
  (C2); `fpga/dfx/dfx_floorplan.xdc` deliberately omits
  `RESET_AFTER_RECONFIG` for this reason.
- No shell↔DUT AXI in v0 (`partition-pins.md` line 8) — this wrapper
  deliberately has no AXI slave/master ports at all. If nanosoc's own
  internal buses need visibility from the host later (`mmio_*`, v1+), that's
  an A6-gated contract change, not something to add unilaterally here.

## OOC timing constraints (socketed XDC — `partition-timing.md`)

Per `docs/contracts/partition-timing.md` v0.1 (the "socketed-XDC" clock
contract, sibling to `partition-pins.md`), this RM ships **`nanosoc_ooc.xdc`**
— the `<rm>_ooc.xdc` half of the two-file delivery — read additively by
`ooc_synth.tcl` (guarded on file existence) so the staged `rm_nanosoc_synth.dcp`
carries real timing constraints and a standalone `report_timing_summary`
analyzes real register-to-register paths instead of *"no user specified timing
constraints"*.

**Clocks it defines** (periods copied byte-for-byte from `partition-timing.md`,
= the built shell's driven periods):

- **`dut_clk`** — `create_clock -period 20.000 -waveform {0.000 10.000}`
  (50 MHz, the static `clk_wiz_dut` MMCM output; D12 placeholder default). The
  whole nanosoc core **and** the UART shim run on this one clock.
- **`swd_clk`** — a deliberately-slow **OOC-only** `create_clock` (100 ns /
  10 MHz), async-grouped from `dut_clk`, purely so the SW-DP registers get
  analyzed standalone. `swd_clk` carries **no clock in the static** (it is a
  firmware bit-banged `SWDBB` CSR pin — `partition-pins.md` SWD group /
  `shell-regmap.md`), so this clock is OOC-only and is NOT reapplied at link.

**Internal exceptions:** *none needed.* This RM is effectively **single-clock**
at implementation — the UART shim's baud divider is a synchronous `dut_clk`
counter and its 2-FF "synchronizer" samples an internal `dut_clk`-domain
loopback (single domain, no CDC), and the SW-DP↔core crossing is owned by the
CoreSight DAP's own synchronizers (async-grouped OOC only). Consequently
**rm_nanosoc ships no `<rm>_rm.xdc`** — there is nothing internal to reapply
scoped to the RP cell at DFX link (`partition-timing.md`). The rest of
`nanosoc_ooc.xdc` is boundary false-paths on the async partition-pin **data**
ports (CDC'd static-side per the domain rule) — OOC-only, superseded by the
static at link.

**Validation status (honest degrade):** `nanosoc_ooc.xdc` is authored and
wired, but a full standalone OOC synth **cannot complete in this environment** —
`filelist.tcl` fails on two missing external `nanosoc_gen` soc_glue helpers
(`soc_glue_mux2.sv`, `soc_glue_reset_sync.sv`, absent in both lab worktrees —
see "Known gap" above) plus the not-yet-built stage-0 bootrom. The XDC's
clock/group syntax and clock-definition behaviour were smoke-tested in Vivado
against a port-matched stub; see `XDC_VALIDATION.txt`. When the soc_glue
helpers land, re-run `ooc_synth.tcl` for the real before/after timing.

## Verifying without Vivado

`uart_axis_shim.sv` lints clean under `verilator --lint-only -Wall`:
```
verilator --lint-only -Wall -sv fpga/rp/nanosoc/uart_axis_shim.sv       # exit 0, silent
```
`rp_nanosoc_wrapper.sv` can't be lint-checked fully standalone (it
instantiates the real `nanosoc`, whose source isn't vendored here — by
design, see `filelist.tcl`). It was checked against a **black-box stub**
carrying nanosoc's exact current port header (copied verbatim from
`nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv`, kept only in the scratch
verification area, not part of this deliverable): zero port-count/width/
direction mismatches, only the expected `PINCONNECTEMPTY` (for the tied-off
`exp_*`/scan ports, matching this file's own documented intent) and
`UNUSED` (for `swd_dio_oe`/`phy_rmii_*`/`mdio_i`, matching "A6 gaps" #3 and
the RMII/MDIO tie-off) warning classes — the same classes
`fpga/dfx/rms/README.md` already documents as expected for a mostly-tie-off
wrapper.

`filelist.tcl`'s flist-parsing logic is pure Tcl and was dry-run-tested
standalone with plain `tclsh` (see its own header) — this is how the
nanosoc.sv-divergence substitution and the soc_glue gap above were actually
confirmed, not just reasoned about.
