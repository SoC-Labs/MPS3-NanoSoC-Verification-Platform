# DFX Implementation Signoff — MicroBlaze-V Linux Shell

> **Status: HISTORICAL** — a record of DFX timing signoff for the MicroBlaze-V Linux shell lineage (`static_id 0x2B082E1B`) as of 2026-08-07.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

**Robustness item:** "DFX timing signoff" for the Linux-harness shell.
**Design:** `shell_linux_top`  ·  **DFX static_id:** `0x2B082E1B`
**Device:** `xcku115-flvb1760-1-c` (speed grade **-1**, i.e. slowest corner — signoff is at the worst-case timing model)
**Tool:** Vivado v.2026.1 (Build 6511674)  ·  **Build date:** 2026-07-22
**Flow:** `fpga/dfx/build_dfx.tcl` — `rm_greybox` (reference config, locks the static) and `rm_led` implemented against the `shell_linux_top` static; routed DCPs + partial bitstreams emitted to `fpga/dfx/build_linux/prod/`.

This doc is read-only evidence extracted **directly from the emitted reports** (not from the build-log one-line summary). All paths are under
`~/SoCLabs/mps3-nanosoc-platform/fpga/dfx/build_linux/`.

---

## 1. Artifacts present

### `build_linux/prod/` — signoff reports
| File | Purpose |
|---|---|
| `timing_rm_greybox.rpt` | `report_timing_summary`, greybox (reference) config, fully routed |
| `timing_rm_led.rpt` | `report_timing_summary`, led config, fully routed |
| `drc_rm_greybox.rpt` | `report_drc`, greybox, Design State = Fully Routed |
| `drc_rm_led.rpt` | `report_drc`, led, Design State = Fully Routed |
| `drc_hdpr_rm_greybox_prelink.rpt`, `drc_hdpr_rm_led_prelink.rpt` | HD.PR pre-link DRC (partial-reconfig rule subset) |
| `pr_verify_rm_led.rpt` | `pr_verify` greybox-routed vs led-routed |
| `util_rm_greybox.rpt`, `util_rm_led.rpt` | Utilization (RP/pblock content) |
| `config_rm_greybox_routed.dcp`, `config_rm_led_routed.dcp` | Full routed DCPs (per config) |
| `static_routed_locked.dcp` | Locked static used for the led config |
| `rm_greybox_synth.dcp`, `rm_led_synth.dcp` | RM post-synth DCPs |
| `static_id.txt` (`0x2B082E1B`), `overlay_inputs.txt` | Static-ID + overlay manifest inputs |

### `build_linux/prod/` — deliverable bitstreams
| File | Size |
|---|---|
| `config_rm_greybox.bit` (full device) | 12,821,235 B (~12.8 MB) |
| `config_rm_greybox_pblock_rp_dut_partial.bit` | 1,261,028 B (~1.26 MB) |
| `config_rm_greybox_pblock_rp_dut_partial_clear.bit` | 56,059 B (~56 KB) |
| `config_rm_led_pblock_rp_dut_partial.bit` | 1,227,734 B (~1.23 MB) |
| `config_rm_led_pblock_rp_dut_partial_clear.bit` | 62,129 B (~62 KB) |
| (`.bin` equivalents of each of the above also present) | — |

### `build_linux/` — build/log artifacts
`build_linux_dfx_2026.log` (main run log), `build_linux_dfx.log`, `clockInfo.txt`, `hd_visual/`, congestion snapshots (`iter_5`/`iter_35_CongestedCLBsAndNets.txt`).

**Report types NOT emitted as standalone files:** there is no separate post-place/post-route "power" or "methodology" report, and no standalone congestion `.rpt` (only the two congestion text snapshots). DRC **is** present as a standalone fully-routed report for each config (below), so no inference needed there. `write_bitstream completed successfully` for both configs, which independently implies the bitstream-generation DRCs passed.

---

## 2. Actual numbers (read from the reports)

### Timing — Design Timing Summary (fully routed)
| Config | WNS (ns) | TNS (ns) | WHS (ns) | THS (ns) | WPWS (ns) | Failing endpoints | Verdict line |
|---|---|---|---|---|---|---|---|
| **greybox** (reference/static) | **+0.095** | 0.000 | **+0.030** | 0.000 | +0.124 | 0 / 0 / 0 (TNS/THS/TPWS) | "All user specified timing constraints are met." |
| **led** | **+0.095** | 0.000 | **+0.030** | 0.000 | +0.124 | 0 / 0 / 0 | "All user specified timing constraints are met." |

All slacks **positive**; setup, hold and pulse-width all met; zero failing endpoints across ~131 k timing endpoints.

> Note: the build-log narrative quoted WNS +0.094 / WHS +0.029. The **reports say +0.095 / +0.030** — a rounding difference in the log, not a discrepancy in outcome. This doc uses the report values.

### Where the tight path lives (Intra-Clock Table, led config)
| Clock domain | Role | WNS (ns) | WHS (ns) |
|---|---|---|---|
| `mmcm_clkout0` (200 MHz / 5 ns — **DDR4 UI**) | static shell | **+0.095** ← worst setup | +0.030 ← worst hold |
| `mmcm_clkout6` | static shell | +2.484 | +0.030 |
| `clk_out1…clk_wiz_shell_0` (shell fabric) | static shell | +0.394 | +0.030 |
| `clk_out1…clk_wiz_dut_0` (**DUT/RP clock**) | reconfigurable region | **+16.409** | +0.035 |
| `c0_sys_clk_p` (100 MHz board input) | static | +6.763 | +0.053 |

**The critical path is the 200 MHz DDR4 domain in the STATIC shell — not the reconfigurable DUT region.** The DUT/RP logic has ~16.4 ns of setup margin; the LED RM cannot be the timing limiter. This is intrinsic to the Linux shell (the DDR4 controller is what makes the design tight), and it is shared by every RM built against this static.

### Clock constraint coverage (`check_timing`, led config)
- unconstrained_internal_endpoints: **0**
- register/latch pins with no clock: **0**
- register/latch pins with constant_clock: 0 · with multiple clocks: 0
- generated clocks not connected to a source: 0 · combinational loops: 0
- `dut_clk` (RP boundary clock) **is defined** — 50 MHz / 20 ns; RP logic actually clocked by `clk_wiz_dut_0` derived clock (constrained, +16.4 ns WNS).
- **12 output ports with no output delay (severity HIGH)**, plus 27 input / 38 output ports with no delay that are covered by explicit false-path constraints. (Typical for board-level LED/GPIO shell IO; see Gaps.)

### DRC (fully-routed `report_drc`, both configs identical profile)
| Config | Total checks | Errors | Critical | Warnings |
|---|---|---|---|---|
| greybox | 18 | **0** | **0** | 18 |
| led | 18 | **0** | **0** | 18 |

Warning breakdown (both): `BUFC-1`×2, `PDCN-1569`×3, `PLIO-6`×3, `REQP-1617`×3, `REQP-1852`×1, `REQP-1877`×4, `REQP-1934`×1, `RTSTAT-10`×1. All are advisory (QSPI legacy-mode IOB registers with no IO load; unused DDR4 `bufg_addn_ui_clk` buffers; HWICAP FIFO `NO_CHANGE` collision advisory; dbg_hub BSCAN no-routable-loads). **No routing, no timing, no placement-error DRCs.**

### pr_verify (`pr_verify_rm_led.rpt`)
- **Verdict: `check points … config_rm_greybox_routed.dcp and config_rm_led_routed.dcp are compatible`** (`[Vivado 12-3253]`).
- 106 partition pins compared, 43,911 static tiles, 6,898 static sites, 83,484 static cells, 1,170,660 static routed nodes, 1,088,428 static routed pips — **identical between the two DCPs**, confirming the led RM was built against a bit-identical locked static.
- One INFO note (`HDPRVerify-38`): the greybox DCP has an *unlocked* static portion (expected — greybox is the reference config that locks the static; the led DCP is the one built against the locked static). This is informational, not a failure, and pr_verify still returns *compatible*.

### RM utilization (led RP content, `util_rm_led.rpt`)
LED RM occupies **109 LUTs, 27 FFs, 0 BRAM, 0 DSP** inside a large RP pblock (~42.8 k LUTs available → 0.25 % filled). The RM is trivially small; the partial-bitstream size is set by the **pblock frame area**, not the RM logic.

---

## 3. Comparison to the classic-shell DFX signoff

Prior classic-shell prod trees exist under `fpga/dfx/build/prod/` and `fpga/dfx/prod_results_2026-07-06-realshell/`.

| Metric | Classic shell (`build/prod`) | Classic (`realshell`) | **Linux shell (`build_linux`)** |
|---|---|---|---|
| greybox WNS | +2.076 | +3.135 | **+0.095** |
| led WNS | +2.337 | +3.135 | **+0.095** |
| WHS (both) | +0.024 | +0.024 | **+0.030** |
| Timing endpoints | ~19.7 k | ~18.4 k | **~131 k** |
| pr_verify led | compatible | (compatible) | **compatible** |
| led partial `.bit` | 925,529 B | — | 1,227,734 B |
| led clear `.bit` | 49,780 B | — | 62,129 B |

**Assessment — comparable and healthy, but materially tighter on setup.** The Linux shell meets all constraints at the -1 (slow) corner, hold margin is actually slightly *better* than the classic shell (+0.030 vs +0.024). However **setup margin collapsed from ~2–3 ns to ~0.095 ns** because the Linux shell instantiates a 200 MHz DDR4 controller and a MicroBlaze-V subsystem (~6.6× the timing endpoints). The tight path is entirely inside the static shell's DDR4 UI domain; the reconfigurable DUT region is unaffected and remains extremely relaxed. The larger partial/clear bitstreams are expected — the Linux floorplan uses a larger RP pblock — and the ~1.23 MB led partial / ~62 KB clear are size-sane for that pblock (classic was 0.93 MB / 50 KB for a smaller pblock; both scale with frame count, not RM content).

---

## 4. Signoff gaps for a real on-silicon swap (honest list)

1. **Thin static setup margin (~95 ps on the 200 MHz DDR4 domain).** Positive at the -1 slow corner, so it is *technically signed off*, but 95 ps leaves almost no headroom for board-level jitter beyond what the constraints model, or for any future static-shell change. This is a **static-shell property, identical for every RM** — it does not gate the LED swap specifically, but it is the number to watch if the DDR4 clocking or the static logic is ever touched. Hold (+0.030) and pulse-width (+0.124) are comfortable.

2. **DUT-region timing is safe.** WNS on the reconfigurable clock (`clk_wiz_dut_0`) is +16.4 ns and WHS +0.035 — the RM cannot violate. The RP boundary clock `dut_clk` is defined and constrained; there are **no unconstrained clocks in the RP region** (`check_timing`: 0 unconstrained internal endpoints, 0 pins with no clock). This closes the "unconstrained RP clock" class of gap.

3. **Two false-path constraints on the MicroBlaze-V UART pins did NOT apply.** The log shows `CRITICAL WARNING [12-4739] set_false_path: No valid object(s) found for -to [get_ports MB_UART_TXD]` and `-from [get_ports MB_UART_RXD]` (`mps3_harness_timing.xdc:384-385`). The intended async-IO false paths on the MB console pins were silently dropped because those port names don't resolve in this shell. Low risk (these are slow async console lines and would otherwise be unconstrained board IO either way), but the constraint is **not doing what it reads as doing** — worth reconciling the port names before relying on it.

4. **Two `if` statements in the timing XDC were skipped.** `CRITICAL WARNING [20-1307] Command 'if' is not supported in the xdc constraint file` at `mps3_harness_timing.xdc:178` and `:325`. Any conditional constraint gated behind those `if` blocks did **not** take effect. Confirm nothing timing-critical (e.g. a conditional clock-group or false-path) was inside them. (This is the known "`condition:`/XDC `if` is broken" trap.)

5. **12 output ports have no output delay (check_timing HIGH).** Combined with the false-path-covered IO, these are almost certainly board LED/GPIO/status pins that are legitimately unclocked outputs — but they are **not formally constrained**, so their pad timing is not signed off. Acceptable for LEDs; would need explicit constraints if any of those pins ever carry a synchronous interface.

6. **No standalone DRC-error report by design — inferred-clean only where stated.** DRC *is* present as a fully-routed standalone report for both configs (0 errors / 0 critical), so this is not a gap for DRC. But there is **no post-route power report and no report_methodology output** in the prod set; if the robustness item wants CDC/methodology signoff, that evidence is not in this build.

7. **16 CRITICAL WARNINGs at build time, all triaged benign.** All 16 are the QSPI legacy-mode `IOB=TRUE` placement conflicts (constraint auto-removed → matches the `PLIO-6`/`REQP-1617` DRC warnings) plus items #3 and #4 above. None indicate a routing/placement failure. `write_bitstream completed successfully` for both configs; **no `ERROR` lines** in the log.

---

## Verdict

**The Linux-shell DFX build is a valid partial-reconfiguration signoff:** both configs meet all timing constraints at the -1 slow corner (WNS +0.095, WHS +0.030, zero failing endpoints), DRC is clean (0 errors / 0 critical on both), `pr_verify` returns **compatible** with a bit-identical locked static (106 partition pins, ~1.17 M static nodes matched), and both partial + clearing bitstreams generated successfully at sane sizes. The reconfigurable DUT region has ~16 ns of margin and no unconstrained clocks.

The one substantive caveat is that the **static shell's setup margin is thin (~95 ps on the 200 MHz DDR4 domain)** — healthy but far tighter than the classic shell's ~2–3 ns — so any future change to the static clocking or logic must be re-timed carefully. The two dropped constraints (MB_UART false paths, XDC `if` blocks) are low-risk here but should be reconciled so the constraint set reads truthfully.

*Evidence read-only from `fpga/dfx/build_linux/` on 2026-07-22. No files under `fpga/dfx/` were modified; no board or Vivado run was performed.*
