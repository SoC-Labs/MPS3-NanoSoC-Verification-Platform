# Contract: partition pins (RP ⇄ static shell boundary)

**Version:** v0.2 (2026-07-15) — v0.1 (2026-07-04) reconciled after the
Phase-0 spike per resolved OPEN_ISSUES I1/I4; v0.2 adds the +14-bit Flash/QSPI
XiP group (integrator A6), the one documented non-CDC source-synchronous
crossing. **2026-09-23 (the 2026-10 ILA mint):** adds the 12-bit `dbgbscan`
group, 35 → 47 ports / 136 → 148 bits, 19 → 20 decoupler INTFs; mint
0x3F1A560F (fielded) is the last static on the 35-port boundary. Widen only via
the integrator (A6), re-reviewing both sides. Widening
this boundary re-keys `static_id` and forces a rebuild of every RM partial.

The reconfigurable partition (the DUT / nanosoc RM) connects to the static
shell **only** through the signals below. Everything crossing this boundary is
a slow scalar or a low-rate stream — no shell↔DUT AXI in v0 (this is what made
the Z2 DFX split clean; keep it). Directions are **from the shell's view**.

**Decisions folded in:**
- **I1 — v0 DUT = single-core nanosoc (Cortex-M0).** These pins are the
  single-core boundary: one console UART, one JTAG SWJ-DP, one RMII MAC. The
  dual-core / IPC / 2nd-UART variant is **deferred to the ethernet effort**
  (later RM `rm_nanosoc_multicore`, larger Pblock). Port names still track the
  proven nanosoc FPGA wrapper so the RM wrapper is a re-plumb, not a redesign.
- **I4 — the RP is a hierarchical cell *alongside* the shell** in one top
  design (`u_top` = `u_shell` + `u_rp_dut`, the RP marked `HD.RECONFIGURABLE`);
  partition pins are BD pins between the two, not top-level ports. This settles
  the A1/A2 topology question and fixes A2's RP instance path
  (`u_top/u_rp_dut`). The shell **brokers all board I/O to the DUT** — the DUT
  reaches board GPIO / PMOD / other ports only through the shell via the
  board-port group below, never directly (keeps pads + IOB packing static).

## Clock/reset domain rule

- **All DUT clocks are generated in the static shell** (DRP MMCM, see
  `shell-regmap.md` CLKRST block) and enter the RP as partition pins. No clock
  generation inside the RM.
- **Every crossing is CDC'd on the static side.** The RM sees already-safe
  signals; shell owns the synchronizers/async FIFOs (`clkrst` + per-block).
- Resets: asserted asynchronously, deasserted synchronized to the destination
  domain. Three resets, all shell-driven (see below).

## Signal groups

### Clocks & resets (shell → RP)
<!-- BEGIN GENERATED[boundary-clkrst] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Domain | Notes |
|---|---|---|---|---|
| `dut_clk` | O | 1 | dut | DRP-reconfigurable DUT system clock |
| `dut_resetn` | O | 1 | dut | reset #1 — DUT system reset (host register) |
| `rp_resetn` | O | 1 | dut | reset #2 — reconfig reset, held through swap |
| `dbg_resetn` | O | 1 | dut | reset #3 — debug/SRST from OpenOCD path |
<!-- END GENERATED[boundary-clkrst] -->

### Processor debug — internal JTAG (SWJ-DP)
The DUT's SoC-400 SWJ-DP is driven as a 4-wire JTAG TAP (Phase-4 P1 re-mint,
`JTAG_UART_REMINT_PLAN.md` (internal note, not in the public tree)). The shell drives TCK/TMS/TDI out and samples TDO
back — a 3-out / 1-in shape identical to the old SWD group, so the partition
width is unchanged (no `static_id` re-key from this signal count). The SWJ
straps (`swj_enable`, `ntrst`, `npotrst`) are tied inside each RM wrapper, not
carried across the boundary.
<!-- BEGIN GENERATED[boundary-jtag] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Notes |
|---|---|---|---|
| `jtag_tck` | O | 1 | TCK → DUT dap_swclktck |
| `jtag_tms` | O | 1 | TMS → DUT dap_swditms |
| `jtag_tdi` | O | 1 | TDI → DUT dap_tdi |
| `jtag_tdo` | I | 1 | TDO ← DUT dap_tdo |
<!-- END GENERATED[boundary-jtag] -->

### RM debug — BSCAN to an RM debug hub (shell → RP, one leg back)
The static `debug_bridge_0` (mode 2, AXI-to-BSCAN, the XVC `dbgbr` target) is
built with `C_NUM_BS_MASTER=1` and its `m0_bscan` master crosses the boundary
as 12 discrete scalars. An RM that carries ILAs puts a `debug_bridge` in
**mode 1** ("From BSCAN to Debug Hub") on these legs: its hub, and every ILA in
the same RM, are then reachable over XVC with no `connect_debug_cores` and no
BSCANE2 or clock buffer in the RP (none may live there, HDPR-16/-18). Rules:

- **ILA ⇒ bridge.** An ILA in an RM with no mode-1 bridge makes Vivado look for
  a BSCANE2 hub, which is illegal in an RP.
- **Hub clock = `phy_rmii_ref_clk`** (50 MHz, 4× TCK, the only always-on shell
  clock in the RP). Never `dut_clk`: the DRP stops it and the hub dies with it.
- **The 11 shell-driven legs are NOT decoupler members** (same rule as `jtag_*`):
  XVC gating in the firmware keeps TCK still during a swap — do not "fix" them
  by clamping. `dbg_bscan_tdo` is decoupler INTF ID 19, clamped to 0.
- An RM with no hub ignores the inputs and ties `dbg_bscan_tdo = 1'b0`.
- `dbg_bscan_tck` (80 ns, from the static BUFGCE) and `dbg_bscan_drck` are
  declared as 80 ns clocks in every RM OOC XDC, asynchronous to `dut_clk` and
  `phy_rmii_ref_clk` (`partition-timing.md`).
<!-- BEGIN GENERATED[boundary-dbgbscan] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Notes |
|---|---|---|---|
| `dbg_bscan_bscanid_en` | O | 1 | BSCAN id-enable from debug_bridge_0 m0_bscan (ILA-over-XVC) |
| `dbg_bscan_capture` | O | 1 | BSCAN CAPTURE from debug_bridge_0 m0_bscan |
| `dbg_bscan_drck` | O | 1 | BSCAN DRCK (gated TCK) from debug_bridge_0 m0_bscan; 80 ns clock in RM OOC XDCs |
| `dbg_bscan_reset` | O | 1 | BSCAN RESET (TAP test-logic-reset) from debug_bridge_0 m0_bscan |
| `dbg_bscan_runtest` | O | 1 | BSCAN RUNTEST from debug_bridge_0 m0_bscan |
| `dbg_bscan_sel` | O | 1 | BSCAN SEL (user instruction selected) from debug_bridge_0 m0_bscan |
| `dbg_bscan_shift` | O | 1 | BSCAN SHIFT from debug_bridge_0 m0_bscan |
| `dbg_bscan_tck` | O | 1 | BSCAN TCK from debug_bridge_0 m0_bscan, 80 ns (12.5 MHz) off a static BUFGCE |
| `dbg_bscan_tdi` | O | 1 | BSCAN TDI from debug_bridge_0 m0_bscan |
| `dbg_bscan_tms` | O | 1 | BSCAN TMS from debug_bridge_0 m0_bscan |
| `dbg_bscan_update` | O | 1 | BSCAN UPDATE from debug_bridge_0 m0_bscan |
| `dbg_bscan_tdo` | I | 1 | BSCAN TDO from the RM's debug_bridge (mode 1); 0 when no RM hub |
<!-- END GENERATED[boundary-dbgbscan] -->

### Ethernet — RMII + MDIO (shell = virtual PHY)
<!-- BEGIN GENERATED[boundary-eth] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Notes |
|---|---|---|---|
| `phy_rmii_ref_clk` | O | 1 | 50 MHz REF_CLK sourced by shell (DUT = ref-in) |
| `phy_rmii_crs_dv` | O | 1 | carrier-sense / RX-valid to DUT MAC |
| `phy_rmii_rxd` | O | 2 | RX data to DUT MAC |
| `phy_rmii_txd` | I | 2 | TX data from DUT MAC |
| `phy_rmii_tx_en` | I | 1 | TX enable from DUT MAC |
| `mdc` | I | 1 | MDIO clock from DUT (DUT = MDIO master) |
| `mdio_o` | I | 1 | MDIO out from DUT |
| `mdio_oe` | I | 1 | MDIO output-enable from DUT |
| `mdio_i` | O | 1 | MDIO in to DUT (virtual-PHY register model reply) |
<!-- END GENERATED[boundary-eth] -->

### Console / trace — AXI-Stream byte (nanosoc `cmsdk_apb_usrt`)
<!-- BEGIN GENERATED[boundary-uart] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Notes |
|---|---|---|---|
| `uart_tx_tdata` | I | 8 | DUT → host console bytes |
| `uart_tx_tvalid` | I | 1 | |
| `uart_tx_tready` | O | 1 | |
| `uart_rx_tdata` | O | 8 | host → DUT console bytes |
| `uart_rx_tvalid` | O | 1 | |
| `uart_rx_tready` | I | 1 | |
| `swo` | I | 1 | Cortex-M SWO/ITM single-wire trace (relayed like UART) |
<!-- END GENERATED[boundary-uart] -->

### Status / misc (RP → shell)
<!-- BEGIN GENERATED[boundary-status] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Notes |
|---|---|---|---|
| `rm_id` | I | 32 | RM identity register — read back for RM-load verify |
| `dut_lockup` | I | 1 | core lockup indicator (telemetry/watchdog) |
| `irq_out` | I | 1 | spare DUT IRQ/event to shell (optional) |
<!-- END GENERATED[boundary-status] -->

### Board-port / GPIO passthrough (shell ⇄ RP) — I4
The DUT reaches MPS3 board I/O (LEDs, buttons, PMOD/Arduino header, spare FMC
single-ended) **through the shell**, which owns the actual pads and routes a
generic GPIO bus across the partition boundary. This keeps board pins + their
IOB packing in static (survives DFX swaps) while giving the DUT real board
access. Width `NGPIO` is a build parameter (v0: 16).
<!-- BEGIN GENERATED[boundary-gpio] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Notes |
|---|---|---|---|
| `dut_gpio_o` | I | NGPIO | DUT drive value → shell → board pad |
| `dut_gpio_oe` | I | NGPIO | DUT output-enable (per-bit tristate) |
| `dut_gpio_i` | O | NGPIO | board pad → shell → DUT sample |
<!-- END GENERATED[boundary-gpio] -->

Which physical board pin each `dut_gpio_*` bit maps to is fixed in the **shell
constraints** (`fpga/shell/constraints/`), not here — the RP boundary is
pin-agnostic so a new RM never re-pins the board. A shell CSR (regmap, new
GPIO block) can also mux/override bits for host-driven board I/O when no DUT
needs them.

### Flash / QSPI XiP — external SST26VF064B (RP ⇄ shell) — the NON-CDC exception

**Added v0.2 (2026-07-15), integrator A6.** The DUT's own QSPI controller
(`qspi_flash_ahb.v`, inside the nanosoc / multicore SoC — ports `QSPI_SCLK`,
`QSPI_nCS`, `QSPI_IO_o[3:0]`, `QSPI_IO_i[3:0]`, `QSPI_IO_e[3:0]`) reaches the
board's external quad flash (**SST26VF064B**, pads D0-3 = AU24/AV24/AV21/AV22,
SCLK = AT25, nCS = AT24) across the partition boundary. This is the product
path for code that does not fit in 16 KB IMEM (XiP), replacing the FPGA-only
fat-BRAM scaffold (`rm_nanosoc_upy`).

<!-- BEGIN GENERATED[boundary-qspi] — gen_boundary.py — DO NOT EDIT BY HAND -->
| Signal | Dir | Width | Notes |
|---|---|---|---|
| `qspi_sclk` | I | 1 | SPI clock, RP → shell → SCLK pad (AT25) |
| `qspi_csn` | I | 1 | chip-select (active-low), RP → shell → nCS pad (AT24) |
| `qspi_io_o` | I | 4 | IO0-3 drive value, RP → shell → data pads |
| `qspi_io_oe` | I | 4 | IO0-3 per-lane output-enable, RP → shell (SoC `QSPI_IO_e`) |
| `qspi_io_i` | O | 4 | IO0-3 sampled at the pad, shell → RP (SoC `QSPI_IO_i`) |
<!-- END GENERATED[boundary-qspi] -->

Ten RP→shell bits (`qspi_sclk`+`qspi_csn`+`qspi_io_o[4]`+`qspi_io_oe[4]`) +
four shell→RP bits (`qspi_io_i[4]`) = **+14 partition bits**.

**THIS GROUP IS A DELIBERATE, DOCUMENTED EXCEPTION to the "every crossing is
CDC'd on the static side" rule above.** QSPI read capture is
*source-synchronous*: IO is sampled relative to SCLK, and the flash's data-hold
budget (`tHD`) is tiny. Passing these bits through the shell's normal CDC
synchronizers would jitter the SCLK↔IO phase relationship and destroy the read
capture. So this group MUST be a **matched, NON-CDC, source-synchronous
passthrough**: equal register depth in both directions, OR raw combinational
nets with shell-side pad registers only. The RP-drive legs still route through
the DFX decoupler (a purely combinational clamp — NOT a synchronizer) so the
flash is forced deselected during a swap; the decoupler's clamp values are the
one intentional non-zero safe-idle in the design:
`qspi_csn`→1 (deselected), `qspi_sclk`/`qspi_io_o`/`qspi_io_oe`→0.

**Timing rule (settled by the prior study):** XiP read timing closes for quad
mode only at **SCLK ≤ HCLK/8**. The DUT's `qspi_flash_ahb` clock divider must
be programmed accordingly.

> ⚠️ **CORRECTION (2026-07-16).** This paragraph previously claimed *"the RTL
> already forbids CLK_DIV=0"*. **It does not.** `qspi_clock_div.v:10` makes
> CLK_DIV=0 an explicit, deliberate HCLK **bypass** —
> `QSPI_SCLK_i = (QSPI_CLK_DIV==5'h00) ? HCLK : QSPI_SCLK_reg` — i.e. raw HCLK
> combinationally onto the pad, documented in the RDL as a feature. There is no
> guard. Worse, **CLK_DIV resets to 1 (HCLK/2)**, not to a ÷8 value. So the
> ≤HCLK/8 rule is a **firmware obligation, not an RTL guarantee**: firmware MUST
> program CLK_DIV ≥ 4 before any XiP fetch. This is not theoretical —
> `ahb_qspi/docs/cocotb-silicon-findings.md` reports CLK_DIV=1 garbling RDID on
> real silicon. The shell's QSPI pad constraints
> (`fpga/shell/constraints/mps3_harness_timing.xdc`) declare the generated SCLK
> at `-divide_by 8`, so a firmware CLK_DIV < 4 also invalidates timing signoff.

**Why not a `dut_gpio` tunnel:** rejected — a `dut_gpio` bit is a true CDC
synchroniser (it jitters the source-synchronous capture) and the tunnel bits
are already fully consumed. Dedicated matched pins are mandatory.

> **TODO — DEFERRED VALIDATION (not blocking this boundary freeze).** This
> contract entry lands the *plumbing* only. Still owed, tracked separately:
> (1) full I/O timing signoff of the qspi passthrough against the SST26VF064B
> datasheet (`set_input/output_delay`, the matched-depth constraint);
> (2) fixing the cold-XiP backward-branch livelock in the SoC;
> (3) silicon bring-up of XiP execution. Until those close, an RM that carries
> this group still boots from IMEM — the controller is present and
> register-accessible but XiP-EXECUTE is unproven. Do not present XiP as
> working on silicon on the strength of this boundary alone.

## IOB packing note (Z2 lesson, HDPR-29)

Pin-facing output registers that want `IOB TRUE` packing (e.g. an RMII TX
re-register stage) **must live in the static shell**, not the RM — OLOGIC pad
sites are static-only in a DFX design. The RMII TX signals above therefore
enter the shell as plain fabric signals and are re-registered shell-side before
the pads.

## Optional / deferred (v1+, not in the v0 boundary)
`mmio_*` (host AXI/AHB into DUT), `adp_*` / FT1248 stream. Add via A6 only.

RM-internal ILAs are **no longer deferred**: the 2026-10 ILA mint reverses spec
D4 (`docs/planning/ILA_MINT_PLAN_2026-09-23.md` decision 1) and carries them on
the `dbgbscan` group above. Each debug RM ships its own `.ltx`
(`write_debug_probes -cell u_rp`), which must be re-staged with its partial.
