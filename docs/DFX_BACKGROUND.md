# DFX "PYNQ-like" plan — persistent Linux + hot-swappable nanosoc partition

> **Status: HISTORICAL** — a record of a Zynq-7 / PYNQ-Z2 DFX feasibility probe carried out in a *different* repo, before this platform's KU115 flow existed as of 2026-07-04.
> Superseded by / current state in [fpga/dfx/README.md](../fpga/dfx/README.md) and [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

Status: **FEASIBILITY PROVEN 2026-07-02** — probe routed both configs with
timing met (WNS +1.92 ns, 0 violations), `pr_verify` compatible, partial
bitstreams emitted. Evidence: `future-work/dfx-pynq/probe_results_2026-07-02/`.
Owner: the project lead.
Companion scripts: `future-work/dfx-pynq/` (probe flow, new — not wired into the main Makefile yet).

## 1. Goal

Give the Zynq targets (PYNQ-Z2 now, ZC702 later, same XC7Z020 die) a DFX
(Dynamic Function eXchange, ex-Partial Reconfiguration) split so that:

- **Static region** — PS7 + `clk_wiz` + `proc_sys_reset` + DFX Decoupler(s)
  — is programmed once per boot. PetaLinux/PYNQ on the PS **stays up** while
  the SoC underneath is swapped.
- **Reconfigurable partition (RP)** — the entire `nanosoc_multicore_ip`
  instance — is hot-swapped in seconds via the Linux `fpga_manager` partial
  path (or PYNQ `overlay.pr_download`).

This is the well-supported analogue of "PYNQ on MPS3" (which is impossible —
no PS; see the 2026-07-02 feasibility report / memory `mps3-pynq-feasibility`).
It also permanently retires the "PL clocks dead after full reprogram" failure
class (z2_04 DAP-AHB FCLK fault), because full reconfiguration stops happening
in normal operation.

## 2. Why the current design is DFX-friendly

From the routed 2026-06-15 Z2 build (`imp/fpga/project/pynq-z2/...impl_1`):

- The BD is only 4 cells: PS7, clk_wiz (1 MMCM), proc_sys_reset, nanosoc IP.
  **There is no AXI between PS and nanosoc** — the boundary is clock, reset,
  and slow scalar I/O (UART/RMII/MDIO/SWD/QSPI/SPI/LED/debug). Ideal partition
  pins; no bus decoupling protocol needed (a plain signal decoupler suffices).
- Utilization: **54% LUT / 31% BRAM / 19% FF / 3% DSP**. Static side is tiny
  (PS7 is hard silicon; clk_wiz + proc_sys_reset + decoupler ≈ a few hundred
  LUTs). An RP covering ~2/3 of the die fits both.
- IMEM/BRAM init (firmware) is carried inside each RM's partial bitstream, so
  different RMs can ship different baked firmware.

### Known boundary hazards (to be confirmed by the probe)

1. **RM-internal generated clocks on global routing.** The routed design shows
   3 global clock nets sourced *inside* nanosoc (QSPI `QSPI_SCLK_i` ripple
   clock via LUT, `rmii_to_mii` mii rx/tx clocks via FDCE). The IP's OOC synth
   contains **0 BUFG** — the buffers were inserted at top-level opt. 7-series
   DFX forbids global buffers inside an RP. Expected fix if the probe trips:
   `set_property CLOCK_BUFFER_TYPE NONE` on those nets (they are ≤25 MHz;
   fabric routing is fine) or accept placer-chosen local routing.
2. **Static MMCM placement.** The non-DFX build placed the MMCM at
   `MMCME2_ADV_X1Y0`, inside the intended RP rows. In the DFX flow the placer
   must move it to a Y2-row CMT (static area) — automatic, but watch clock
   skew to the RP.
3. **Pblock packing.** RP = clock-region rows Y0+Y1 gives ≈ 2/3 × 53,200 ≈
   35K LUTs for a 28.9K-LUT RM (~82% packing). Tight; core clock is only
   25 MHz so routing should close, but if placement fails the fallback is a
   5-region RP (all but X0Y2, where the PS sits) at ~65% packing.
4. **RMII TX IOB packing** *(CONFIRMED by probe run 1, HDPR-29)*: the pin XDC
   deliberately packs `rmii_txd[1:0]`/`rmii_tx_en` output FFs into OLOGIC
   (`IOB TRUE`, sound source-synchronous practice). OLOGIC pad sites are
   static-only — RM logic may not IOB-pack. Probe workaround: `IOB FALSE`
   (fabric FFs, fine at 50 MHz for feasibility). **Production requirement:
   move the RMII TX output stage (re-registering flops) into the static
   region** so the IOB packing survives; cleanest as a small static-side
   `rmii_tx_oreg` block between the decoupler and the pads.
5. **IP clocks don't survive checkpoint linking** *(confirmed by probe run 1)*:
   OOC dcps carry no XDCs, so `clk_fpga_0` (PS7 xci) must be re-created
   manually before the generated timing XDC is read, or `qspi_sclk_i` /
   `set_clock_groups` fail to resolve. Probe does this; the production flow
   (project mode with real xci IP) is unaffected.

## 3. Feasibility probe (this week's step — `future-work/dfx-pynq/dfx_impl_probe.tcl`)

No re-synthesis: link the **existing** project synthesis checkpoints
(`synth_1` top + 4 OOC IP dcps, 2026-06-15 vintage — structurally current
enough for a floorplan probe even though master has moved since), then:

1. `link_design`, read the pin XDC + generated timing XDC.
2. Mark `nanosoc_multicore_design_i/nanosoc_multicore_ip_0` `HD.RECONFIGURABLE`.
3. Pblock = clock regions X0Y0..X1Y1 (SLICE/RAMB/DSP ranges computed at
   runtime; IOB sites excluded), `RESET_AFTER_RECONFIG` + `SNAPPING_MODE ON`.
4. opt/place/phys_opt/route → utilization, timing, DRC reports.
5. Extract static (`update_design -black_box` + `lock_design -level routing`),
   build a **blank RM** config (`update_design -buffer_ports`), `pr_verify`.
6. `write_bitstream` both configs → full bit + `*_partial.bit` each.

Success criteria: route + timing met + pr_verify clean + partial bitfiles
emitted. Any HDPR DRC failures are the actionable output (esp. hazard #1).

### Probe RESULT (2026-07-02, Vivado 2024.1) — PASS on 2nd run

- Run 1 failed usefully: HDPR-29 (RMII TX IOB packing, hazard #4) + missing
  `clk_fpga_0` (hazard #5). Both fixed in the probe tcl.
- Run 2: **opt/place/phys_opt/route clean at 82% RP LUT packing** (hazard #3
  did not bite); **no BUFG-in-RM DRCs** (hazard #1 did not bite — the RM's
  generated clocks routed legally); **timing MET both configs**
  (config_nanosoc WNS +1.920 ns / WHS +0.053 ns, 0 of 63,683 endpoints
  failing; blank config clean); **pr_verify: configurations compatible**;
  partial bitstreams emitted (~4.2 MB each ⇒ ~sub-second fpga_manager load).
- Artifacts: reports in `future-work/dfx-pynq/probe_results_2026-07-02/`; dcp/bit in the
  session scratchpad (ephemeral — rebuild via the README recipe, ~35 min).

## 4. Production flow (after the probe passes)

1. **BD changes** (in `pynq/targets/pynq-z2/nanosoc_multicore_design.tcl`,
   parameterized like everything else so ZC702 inherits):
   - Insert a **DFX Decoupler** (PG375) between the nanosoc instance and
     (a) `uart_txd` → PS UART EMIO, (b) `eth_irq`/`cpu1_wdog_reset`/debug
     outputs, (c) optionally the pin-facing outputs (RMII TX, QSPI, SPI) so
     external devices see quiescent lines during a swap.
   - Add **PS EMIO GPIO (2 bits)**: bit0 = decouple enable, bit1 = RP
     `sys_resetn` gate (hold the incoming RM in reset until released).
   - Wrap the nanosoc instance in a **Block Design Container** with DFX
     enabled (Vivado 2024.1 BDC-DFX flow), or keep the classic instance flow
     scripted from the probe tcl — decide based on which is less disruptive
     to `build_nanosoc_multicore_design.tcl`.
2. **RM variants** (each = one partial bitstream against the same static):
   - `rm_nanosoc_eth` — current multicore SoC (network_core + chip_core).
   - `rm_blank` — greybox (power/bring-up state, also the safety fallback).
   - later: `rm_compute_m4` — the M0+/M4 compute-system variant (feature
     branch); this is the headline demo: swap eth-SoC ↔ M4-Zephyr-SoC live.
3. **Deploy path** (matches existing fpgahub model):
   - Boot: full `config_*.bit` via `fpga_manager` as today.
   - Swap: `gpio decouple=1` → `echo 1 > /sys/class/fpga_manager/fpga0/flags`
     → write partial `.bin` (bit2bin/bootgen) to `/lib/firmware`, echo name to
     `firmware` → `gpio decouple=0`, release RP reset. Wrap as an fpgahub
     `pr_deploy` action; PYNQ images ≥v2.4 also support
     `Bitstream(partial=True)` / `pr_download` if we want the Python route.
4. **Caveats**:
   - Partials are only valid against the **exact static image** — version-tag
     static and partials together (extend the bitstream-freshness gate).
   - 7-series needs no clearing bitstreams (that's UltraScale-only) but does
     need `RESET_AFTER_RECONFIG` (probe sets it).
   - SWD debug of the SoC during a swap will drop — expected; the GUI/openocd
     layers already tolerate reconnects.

## 5. Relationship to MPS3

MPS3 (pure KU115, no PS) cannot host PYNQ in any mode. Its "PYNQ-like" options
are (a) what we already have — MCC/SD orchestration via fpgahub (full reconfig
only), or (b) a research-grade MicroBlaze-PetaLinux static shell self-
reconfiguring via AXI HWICAP (EOL tooling, UltraScale clearing-bitstream +
no-Abstract-Shell taxes, no in-tree fpga-mgr ICAP driver). Decision 2026-07-02:
build the DFX experience on Zynq; keep MPS3 as an MCC-orchestrated bare
target. Full analysis in the session report + memory `mps3-pynq-feasibility`.
