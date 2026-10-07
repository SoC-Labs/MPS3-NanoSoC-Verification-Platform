# `fpga/rp/nanosoc_iice/` — the nanosoc RM, synthesised by Synplify with a Synopsys Identify IICE inside

Build infrastructure to produce the **same** DFX reconfigurable module as
`fpga/rp/nanosoc/`, but through **Synplify Premier 2022.09-SP2** instead of
Vivado synthesis, with an **Identify IICE** woven in, emitting an out-of-context
checkpoint that `fpga/dfx/build_dfx.tcl` consumes **with zero changes**.

Design authority: `docs/planning/IDENTIFY_IICE_DFX_PLAN.md`.
Manifest/generator contract: `tests/identify_iice/INTERFACES.md`.

**Nothing here has ever run on hardware. No bitstream has been produced.**
Section [Gate status](#gate-status) is the honest ledger; read it before
believing anything else in this file.

> **REBASED 2026-09-10 onto the then-fielded boundary (mint `0xA8C1C535`).** This RM
> was built against the 2026-07 boundary, where the Identify soft TAP rode the
> four `swd_*` partition pins. **That group no longer exists.** The A6 SWD->JTAG
> cutover replaced `fpga/shell/ip/swd_bb` with `fpga/shell/ip/jtag_bb` and the
> boundary's debug group is now `jtag_tck/tms/tdi/tdo`
> (`fpga/shell/boundary.yaml`), so the old wrappers could not load at all — and,
> more importantly, there is no longer a spare wire-set to give the soft TAP,
> because the DUT's SoC-400 SWJ-DP is on the only one there is. The two TAPs are
> now in an **IEEE 1149.1 daisy chain**. Design note:
> [`docs/planning/IICE_JTAG_CHAIN.md`](../../../docs/planning/IICE_JTAG_CHAIN.md);
> sim gate: [`tests/jtag_chain/`](../../../tests/jtag_chain/).
> Everything below that still says "SWD" is history and is marked as such.

> **BOUNDARY WIDENED 2026-09-23 (`fda3201`, the ILA mint): 47 ports / 148 bits.**
> The `dbgbscan` group added 12 legs; both tops pass them straight through, so
> the RTL, the Makefile gates and `ooc_synth_synplify.tcl` all say 47 (51 in the
> instrumented EDIF). `make lint` passes at 47 (re-run 2026-09-29: `pin_check`
> 2/2, verilator OK against the 51-port instrumented stub). **The Synplify and
> Vivado results in §5 were all measured at the old 35 / 136 and have not been
> re-run at 47** — re-run `make probe-dcp`, `make synth` and `make dcp` before
> this RM is next built. (2026-09-29: this README still said 35 throughout.)

---

## 1. How this differs from `fpga/rp/nanosoc/`

| | `fpga/rp/nanosoc/` (baseline) | `fpga/rp/nanosoc_iice/` (this) |
|---|---|---|
| Synthesis | Vivado 2024.1 `synth_design -mode out_of_context` | Synplify Premier 2022.09-SP2 -> EDIF -> Vivado `synth_design` over the EDIF |
| Source list | `source $SOC_DIR/pynq/filelist.tcl` directly inside the Vivado script | flattened by `tests/micropython_flash_boot/collect_filelist.tcl`, emitted as a **generated** `.prj` |
| Constraints | `nanosoc_ooc.xdc` (XDC) | `nanosoc_iice.fdc` for synthesis (Synplify takes no XDC) **plus** the baseline's own `nanosoc_ooc.xdc` at checkpoint time |
| RM top | `rp_nanosoc_wrapper` (47 ports) | `rp_nanosoc_iice_shim` (47 ports) over `rp_nanosoc_iice_core` (47 in RTL, 51 in the instrumented EDIF) |
| DUT debug | reachable | **still reachable** — chained, not displaced |
| Debug | none | one IICE: sampler + trigger state machine + a **soft** JTAG TAP |
| Host reach | n/a | a 1149.1 daisy chain with the DUT's SWJ-DP on the one `jtag_*` wire-set |
| Boundary | 47 ports / 148 bits | **47 ports / 148 bits — identical** (35 / 136 before `fda3201`) |
| `static_id` | unchanged | **unchanged.** Partial-bitstream-only change: no shell rebuild, no re-mint, no overlay re-key |
| IP overrides | none | two, in `vsrc_override/` (see its README) |

Same pin list, so the checkpoint fits the shipped shell's boundary. **Not the
same `rm_id` any more (2026-09-14):** the shim drives its own `0x00010008`
(registered in `fpga/dfx/rm_list.tcl` as `rm_nanosoc_iice`, design_id 0x0008 @
0.1.0), so `make stage` -- which hands this checkpoint to `build_dfx.tcl` **as**
`rm_nanosoc` -- would now fail RM-load verify on the board (manifest 0x01000001,
fabric 0x00010008). Build it through the registry entry instead
(`RM_SYNTH_REUSE_rm_nanosoc_iice` in `fpga/dfx/Makefile`); `make stage` is kept
only for the historical A/B and warns. That
"identical boundary" claim was true of the static this RM was FIRST built for
(`0x0EE58A4D`), became false when the boundary moved, and is **true again** as of
the 2026-09-10 rebase: both tops then carried the fielded 35 ports / 136 bits,
and since `fda3201` (2026-09-23) carry the 47 / 148 of the ILA mint; `pin_check`
passes 2/2 against `fpga/shell/boundary.yaml`.

---

## 2. Why there are two tops (this is the load-bearing design decision)

`device jtagport soft` does not reuse pre-declared nets. The Identify
instrumentor **adds four external ports to the synthesis top**:

```
identify_jtag_tck   identify_jtag_tms   identify_jtag_tdi   identify_jtag_tdo
```

**Measured, not assumed.** `make probe-softtap` instrumented `rm_led` (which had
exactly the 35 contract ports of the day) and the resulting EDIF's top cell had
**39 ports / 140 bits** — the 35 contract ports (136 bits) plus those four. At
today's boundary that is 47 + 4 = 51 ports / 152 bits. The
instrumentor log says it in words too:

```
Setting JTAG Style to 'soft'
Setting xilinxinsertbufg to 0
Setting skewfree to 1
```

So the Synplify top **cannot** be the DFX RM top, and there is no RTL scope
*inside* the synthesis top in which to tie the four ports to anything. Hence:

```
rp_nanosoc_iice_shim      47 ports.  THE DFX RM TOP. Vivado-side. Splices
  |                       identify_jtag_* into the one jtag_* wire-set as a
  |                       1149.1 daisy chain with the DUT's SWJ-DP.
  +-- rp_nanosoc_iice_core   47 ports in RTL, 51 in the instrumented EDIF.
        |                    THE SYNPLIFY/IDENTIFY TOP. Pure pass-through.
        +-- rp_nanosoc_wrapper   the real RM (owned by fpga/rp/nanosoc/)
              +-- nanosoc            the SoC
```

`fpga/dfx/pin_check.py` passes on **both** `.sv` files (47 ports each) — but note
it reads *source*, so it cannot see the four added ports. That is why
`ooc_synth_synplify.tcl` runs a second, **netlist-level** boundary audit derived
from `docs/contracts/partition-pins.md`, and why the `probe-dcp` gate asserts
`47 ports / 148 bits` on the finished checkpoint rather than trusting `pin_check`
alone.

### The chain (rebased 2026-09-10) — the shim splices, it no longer steals

| JTAGBB @ `0x44A7_0000` | partition pin | carries |
|---|---|---|
| `DRIVE[0]` | `jtag_tck` | TCK — **shared** by both TAPs |
| `DRIVE[1]` | `jtag_tms` | TMS — **shared** by both TAPs |
| `DRIVE[2]` | `jtag_tdi` | TDI — head of the chain |
| `SAMPLE[0]` (ro) | `jtag_tdo` | TDO — tail of the chain |

```
 shell TDI -> [Identify soft TAP, IR 5] -> [SoC-400 SWJ-DP, IR 4] -> shell TDO
```

Both TAPs are live at once; whichever is not addressed sits in BYPASS
contributing one shift-register bit. Order is `IICE_TAP_NEAREST_TDI` (default 1)
and the reasoning — OpenOCD's first-declared TAP is the one nearest TDO, so the
DAP stays at chain position 0 — is in the design note. Host config:
`host/openocd/nanosoc_iice_chain.cfg`.

**`IICE_OWNS_SWD` and `SWD_MUX_GPIO_BIT` are GONE**, along with the design they
served. There is nothing left to arbitrate: a chain reaches both TAPs, so the RM
no longer trades the M0's debug port for the IICE's, and it no longer spends a
GPIO bit on a mux. The GPIO group is once again purely the DUT's board port.

**HISTORY, kept because the traps are reusable:** the pre-rebase design gave the
soft TAP the four `swd_*` pins outright (`IICE_OWNS_SWD = 1`), which made the
DUT's SW-DP unreachable for the life of the bitstream, with a `IICE_OWNS_SWD = 0`
runtime mux on `dut_gpio_i[7]` as the escape hatch.

---

## 3. Files

| File | What it is |
|---|---|
| `rp_nanosoc_iice_shim.sv` | the DFX RM top. 47 ports. **The chain splice**: six assigns putting Identify's soft TAP in series with the DUT's SWJ-DP on the one `jtag_*` wire-set. |
| `rp_nanosoc_iice_core.sv` | the Synplify/Identify top. 47 ports. Pure pass-through; instantiates `rp_nanosoc_wrapper` as **`u_rm`** (frozen — it is the first element of every Identify probe path). |
| `gen_prj.tcl` | **generates** the Synplify project. Flattens the real filelist (by shelling out to the committed flattener), applies the `exp_h*` patch, binds `vsrc_override/`, emits `build/nanosoc_iice.prj`. `tclsh` only. |
| `ooc_synth_synplify.tcl` | EDIF -> OOC checkpoint at `build/rm_nanosoc_synth.dcp`, plus the netlist-level gates (boundary audit, no-BSCANE2, no-BUFG/MMCM, BRAM `INIT` audit). |
| `nanosoc_iice.fdc` | `fpga/rp/nanosoc/nanosoc_ooc.xdc` hand-translated to Synplify FDC. |
| `fallback_iice.idc` | a minimal self-contained `.idc` for proving the toolchain. **Not** the product manifest — that is Stream A's `gen_idc.py` output. |
| `vsrc_override/` | two local IP overrides + a README justifying each. |
| `lint/gen_lint_stubs.py` | derives three port-exact stubs from the real sources: two lint blackboxes and the `(* black_box *)` stub `ooc_synth_synplify.tcl` needs. |
| `probe_shim_gen.py` | throwaway shim + black box for the `probe-*` Phase-1 gates. |
| `Makefile` | `lint` / `prj` / `synth` / `check-no-bscan` / `dcp` / `stage` / `probe-softtap` / `probe-dcp` / `tools`. |

Everything generated lands in `build/` (gitignored).

---

## 4. Usage

```sh
cd fpga/rp/nanosoc_iice
make tools            # what is reachable
make lint             # boundary + verilator. No licence. Run first, run in CI.
make probe-softtap    # Phase-1 toolchain proof on rm_led   [ONE licence seat, ~40 s]
make probe-dcp        # Phase-1 EDIF->47-port-checkpoint proof (Vivado only)
make prj              # generate build/nanosoc_iice.prj
make synth            # Synplify + Identify on the real DUT [LICENCE SEAT]
make dcp              # -> build/rm_nanosoc_synth.dcp
make stage            # -> fpga/dfx/build/prod/rm_nanosoc_synth.dcp
make IICE=0 synth dcp # uninstrumented Synplify swap (plan Phase 3 A/B baseline)
IDC=../../../tests/identify_iice/build/IICE_NANOSOC.idc make synth   # real manifest
```

`make synth` refuses to run unless the `.idc` sets all three of
`device jtagport soft`, `device xilinxinsertbufg 0`, `device skewfree 1`.

**Tool pinning:** both tools are called by absolute path and no module is loaded,
because `module load synplify/2022.09-SP2` pulls in `vivado/2026.1` (its
modulefile does `module load vivado` when none is loaded, and the site default is
2026.1) while the DFX flow is pinned to 2024.1.

---

## 5. Gate status

> **Port counts below are as measured at the time, against the 35-port / 136-bit
> boundary.** Only `make lint` has been re-run at 47 (see the banner at the top).

### PROVEN by execution (2026-07-29, this machine)

| Gate | Evidence |
|---|---|
| **35-port boundary, both tops** | `fpga/dfx/pin_check.py` -> `ALL WRAPPERS CONFORM — 2/2 pass`, 35 ports each. `make lint` step 1. |
| **Both tops lint clean** | `verilator --lint-only -sv` against derived stubs, incl. the shim against the **39-port instrumented** boundary. `make lint` step 3. |
| **Project generation** | `make prj` -> 246 sources (240 from the live filelist + 6 repo-side), 69 include dirs, 1 define, 2 overrides bound. **All 247 `add_file` paths verified to exist.** No duplicate module names across the 240. |
| **Synplify accepts the part** | `-technology Kintex-UltraScale-FPGAs / -part XCKU115 / -package FLVB1760 / -speed_grade -1-c` all accepted, real run, `exit status=0`. |
| **Licence** | `License checkout: synplifypremierdp` granted and checked in; the Identify jobs ran under `ProductType: identify_instrumentor`. |
| **`.idc` IS the batch interface** | `identify_db_generator` + `identify_compile` both ran from plain `synplify_premier -batch` with a seeded `<rev>/identify.idc`. No instrumentor shell. |
| **`device jtagport soft` works on XCKU115** | `Setting JTAG Style to 'soft'` in the instrumentor log; `identify_jtag_tck` present in the EDIF; 848 `iice` references. |
| **INVERTED BSCANE2 gate** | **no `cellRef BSCANE2`** in the instrumented EDIF. This is the plan's P0 architectural assumption, now measured. |
| **No BUFG** | `device xilinxinsertbufg 0` + `skewfree 1` both applied (`Setting xilinxinsertbufg to 0` / `Setting skewfree to 1`); **no `cellRef BUFG`** in the EDIF, and none in the linked netlist. Matters because the RP pblock has **zero** BUFG sites. |
| **Identify adds exactly 4 ports** | instrumented `rm_led` EDIF top cell = **39 ports / 140 bits** vs 35 / 136 in the source. This is what forces the two-top split. |
| **Identify path convention** | `/dut_clk` and `/blink_counter` both resolved **with `device stop_on_signal_not_found 1` set** and exit 0. See §6. |
| **EDIF -> 35-port OOC checkpoint** | `make probe-dcp`: `read_edif` + `(* black_box *)` stub + RTL shim + `synth_design -mode out_of_context` -> **1290 cells** (1288 from the standalone-linked EDIF + the shim's 2), `IS_BLACKBOX(u_core) == 0`, **35 ports / 136 bits**, no BSCANE2, no BUFG. |
| **IICE area, depth 1024, 27 probe bits** | 707 FF + 415 LUT + 2 BRAM + 28 distributed-memory in `syn_identify_core0_0`. Against ~32,750 free LUTs and 123.5 free BRAM tiles in the RP, noise. |
| **EDIF filename trap** | Synplify writes `<top>.edf` from `project -result_file`; `link_design` fails `[Project 1-68]` otherwise. Re-asserted by `ooc_synth_synplify.tcl`. |

### PROVEN by execution on the REAL 246-source DUT (2026-07-29, later the same day)

The integrator fixed `rp_nanosoc_wrapper.sv` (SoC-400 SWJ-DP bound over the four
existing `swd_*` pins, no re-mint) and made the baseline's `exp_*` override
conditional, which unblocked the real build. Then:

| Gate | Evidence |
|---|---|
| **`make synth IICE=1` on all 240 SoC sources** | `exit status=0`, `License checkin: synplifypremierdp`. Map ~4 min. EDIF 20,675,526 bytes. |
| **`nanosoc_iice.fdc` is consumed** | `Reading constraint file: .../nanosoc_iice.fdc` in the `.srr`, and listed under `Constraint File(s)`. **Zero `@E:` errors.** The only FDC warnings are `MT447` "false path not applied, none of the paths exist" on `phy_rmii_*`/`mdio_*`/`rm_id`/`dut_lockup`/`irq_out` — exactly the tied-off ports the file's header predicts. |
| **Both `vsrc_override/` files compile** | in the 246-source run; no `@E:`. |
| **`check-no-bscan` on the real EDIF** | no `cellRef BSCANE2`, no `cellRef BUFG`, 2592 `iice` refs, `identify_jtag_tck` present. |
| **`make dcp` on the real DUT** | 21,004 cells filled from EDIF, `IS_BLACKBOX(u_core)==0`, clocks `dut_clk swd_clk`. |
| **Boundary audit, netlist level** | **35 ports / 136 bits, contract-conformant.** |
| **Zero clocking primitives** | gate PASS, and independently in `util_rm_nanosoc.rpt` §5 CLOCK: `BUFGCE 0, BUFGCE_DIV 0, BUFG_GT 0, BUFGCTRL 0, PLLE3_ADV 0, MMCME3_ADV 0`. |
| **R2 CLOSED — `$readmemh` -> BRAM `INIT` survives the Synplify EDIF** | the IMEM maps to 4 byte-lane RAMB cells; `.../u_sram/mem_3_mem_3_0_0` carries `INIT_00 = 256'hFDFBF9F7F5F3F1EFEDEBE9E7E5E3E1DFDDDB0000D700000000000000CFCD8900`, byte-identical to the Vivado baseline's `.../u_sram/mem_reg_0`. Now gated automatically (`EXPECT_IMEM_INIT_00`). |
| **R2 UPGRADED from sentinel to FULL AUDIT** | **`BRAM_INIT_AUDIT.md`**: every `INIT_*`/`INITP_*` on every BRAM cell of all three checkpoints (Vivado baseline / IICE=1 / IICE=0) compared on content, no sampling. Both Synplify builds are **bit-identical** on every payload. IMEM and bootrom reconstructed and proven **byte-exact against external ground truth** (`hello_image.hex`, `bootrom.sv`); every other BRAM is all-zero everywhere. Two traps caught: Synplify's IMEM byte-lane numbering is **reversed** vs Vivado's (a by-name diff would false-fail), and the bootrom uses genuinely **different bit layouts** — Vivado stores ROM bits 16/17 in the *parity* array — so a payload multiset diff could not settle it and the layout had to be solved. |
| **R4 — nested unpacked struct ports** | the 3 PeakRDL regblocks compiled in the 246-source run. No longer a risk. |
| Resource use, instrumented, IICE depth 1024 | 12,185 LUT / 6,817 FF / 28 RAMB in the RM. |
| Checkpoint | `build/rm_nanosoc_synth.dcp`, 5,652,172 bytes, at the path `build_dfx.tcl` expects. |

The `rm_led` rows above it remain useful as the **toolchain-only** isolation
(plan Phase 1); the rows here are the real thing.

### PROVEN by execution — THE 2026-09-10 REBASE + CHAIN

| Gate | Evidence |
|---|---|
| **Both tops carry the FIELDED 35-port boundary** | `fpga/dfx/pin_check.py` vs `fpga/shell/boundary.yaml` -> `ALL WRAPPERS CONFORM — 2/2 pass`. **Control:** the pre-rebase tops (`git show 02fb984:...`) -> `0/2 pass`, four MISSING `jtag_*` and four EXTRA `swd_*` each. |
| **Both tops lint clean against the rebased boundary** | `make lint` 3/3, incl. the shim against the **39-port instrumented** stub derived from the real core. |
| **The chain works, against the REAL Arm SWJ-DP** | `tests/jtag_chain` (VCS T-2022.06-SP2, cocotb 2.0.1): **7/7**. Both IDCODEs (`0x6BA00477` nearest TDO, `0x1063E4CD`), total IR length 9, a real Cortex-M0 halted to `DHCSR.S_HALT=1` with the Identify TAP in BYPASS, and an Identify-TAP register write+scan-back with the DAP in BYPASS. It instantiates THIS directory's `rp_nanosoc_iice_shim.sv`, not a copy. |
| **The chain order is load-bearing** | three controls, all fail as required: swapped host declaration order; `make control-order` (fabric rebuilt with `IICE_TAP_NEAREST_TDI=0` -> IDCODEs come back swapped); legacy single-TAP scans do not halt the core. |
| **The host path exists and is targeted** | `host/openocd/nanosoc_iice_chain.cfg` (both TAPs, nearest-TDO first, `adapter speed 4000`); `firmware/xvc_server` gained `-DMPS3_XVC_TARGET_JTAGBB`, host-tested by `firmware/test/test_xvc_jtagbb_chain{,_latesample}` — both IDCODEs over a real TCP socket through the real firmware engine. |
| **No site-specific paths in this Makefile** | `SOCLABS_NANOSOC_SOC_DIR` / `SOCLABS_AHB_QSPI_DIR` now have NO default and are read from `tools.env`, the same seam `fpga/dfx/Makefile` uses; `make prj` fails by name in seconds without them. |

### WRITTEN BUT NOT PROVEN

| Item | Status |
|---|---|
| ~~Full ROM equivalence~~ | **DONE — moved to the proven table above.** See `BRAM_INIT_AUDIT.md`. |
| Identify accepting `dut_clk` as a sample clock when its BUFG is **outside** the RP | still not proven *in placement*. Synthesis is clean and the instrumentor did not complain. The `[Timing 38-242] HD.CLK_SRC ... "dut_clk" is not set` half of this row is now **FIXED** in `fpga/rp/nanosoc/nanosoc_ooc.xdc` (`HD.CLK_SRC BUFGCE_X2Y24`, traced from the shipped locked static; warning gone, link into the static re-verified clean). Two caveats, both measured: the OOC **numbers did not move** (WNS 6.362 / WHS -0.252 identical before and after — at `Synthesized` state there is no placement for skew to be estimated from, and at DFX link the real static clock tree supersedes the property), and the same warning on **`swd_clk` is unfixable by design** — it has no clock buffer in the static (net `w_swd_clk` is `TYPE=SIGNAL`, driven by an `FDRE`), so there is no site to name. |
| `make IICE=0` path | never run. `impl`-less default-`rev_1` handling is untested. |
| `pr_verify`, `report_drc -checks HDPR*`, partial bitstream, clearing-size gate (262,144 B), XVC, debugger | all downstream. Untouched. No bitstream has been produced and no hardware has been touched. |
| `identdebugger` licence seat (plan R0, "KILLS THE PROJECT") | **still unverified.** `synplifypremierdp` + the instrumentor are proven held; the *debugger* is a separate feature. |
| Timing closure of the instrumented RM | `build/timing_rm_nanosoc.rpt` written, not analysed here. |
| `make IICE=0` path | never run. `impl`-less default-`rev_1` handling is untested. |

---

## 6. The `hw:` path convention — answered

**`/` is the synthesis top's own scope. The top module name is never part of the
path.**

Documentation, `identify_debug_env_reference.pdf` p.14, "Design Hierarchy
References":

> Regardless of the underlying HDL (VHDL or Verilog) the path separator
> character is always "/". **Absolute path names begin with a path separator
> character. The top-level design unit is represented by the initial "/".** Thus,
> a port on the top-level design unit would be represented: `/port_name`

Confirmed twice by execution: the reference build `hello_ident` probes `/clk` and
`/count`, both declared directly inside `haps_sx_hello`; and our own
`probe-softtap` probed `/dut_clk` and `/blink_counter` inside `rm_led` — both
with `device stop_on_signal_not_found 1`, both resolved, exit 0.

Ignore the `signals add /top/u1/reset_n` example on the `signals add` page of the
same manual. `top` there is an instance below the synthesis top, not the top
module's name; taken literally it contradicts p.14 and both measurements.

### The exact prefix, for this flow

The Synplify top is **`rp_nanosoc_iice_core`**, which instantiates
`rp_nanosoc_wrapper` as **`u_rm`** (frozen). The hierarchy to the M0, verified by
reading the RTL:

```
rp_nanosoc_iice_core            (synthesis top -> "/")
  u_rm        rp_nanosoc_wrapper
    u_nanosoc   nanosoc                 (nanosoc.sv)
      u_ss_cpu    nanosoc_ss_cpu        (nanosoc.sv:809)
        u_cpu_0     slcorem0            (nanosoc_ss_cpu.sv:189)  <- HADDR is its port
```

So for the M0's AHB address bus:

```
/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HADDR
```

and the general prefix for anything inside the DUT is **`/u_rm/`**.

Notes for Stream A:
- `signals_nanosoc.yaml`'s current `/rp_nanosoc_wrapper/...` is **wrong on both
  counts** — the top module name must not appear, and the wrapper is no longer
  the top.
- **Verilog names are case sensitive**: `HADDR`, not `haddr`. VHDL names are not.
- The sample clock is `/u_rm/dut_clk`, or equivalently `/dut_clk` (the same net,
  reached at the synthesis top's own port). Prefer `/dut_clk` — it survives any
  future rename of `u_rm`.
- `u_rm` is frozen in `rp_nanosoc_iice_core.sv`. Renaming it invalidates every
  `hw:` path in the manifest silently, because the instrumentor's own guard
  (`stop_on_signal_not_found 1`) would then fire on *all* of them at once, which
  reads like a tool failure rather than a rename.
- The Identify comm block is instantiated at the top as `comm_block_INST` and the
  IICE core as `syn_identify_core0_0`; those are inside the tool's own logic, not
  probe targets.

---

## 7. The upstream DUT moved, and it is now RESOLVED

**Historic, kept because the traps are reusable.** The upstream nanosoc tree was
mid-regeneration on 2026-07-29 (57 files, +8,241 lines, uncommitted) and the SoC
boundary changed while this directory was being written:

- `nanosoc` dropped `cpu_0_swdi` / `cpu_0_swclk` / `cpu_0_swdo` /
  `cpu_0_swdoen` for a CoreSight **SoC-400 SWJ-DP with real JTAG**
  (`dap_swclktck, dap_swditms, dap_tdi, dap_swdo, dap_swdoen, dap_tdo,
  dap_ntdoen, dap_ntrst, dap_npotrst, dap_swj_enable`; 83 top ports).
  `rp_nanosoc_wrapper.sv` still connected the four old names, so nothing could
  elaborate — the Vivado baseline included.
- The compile set went **221 -> 240 files between two flattens nine minutes
  apart**. `cortexm0_dap/verilog/*.v` gone (`EXTERNAL_DAP=1` drops the internal
  DAP); `cxdapswjdp` + `cxdapahbap` + `src/rtl/coresight_soc400/*` arrived.
- All 13 `exp_h*` directions came out **already correct**, so the
  `regsub`-must-match-1 assertions in `fpga/rp/nanosoc/ooc_synth.tcl:49-107`
  matched 0 and `error`ed. **That override is OBSOLETE, not broken** — its guard
  was firing on a good input, which is the worst failure mode a guard has. (An
  earlier version of this file said "the baseline script is broken"; that
  inverted cause and symptom.)

**Resolved by the integrator**: `rp_nanosoc_wrapper.sv` now binds the SWJ-DP's
SWD side over the same four `swd_*` partition pins (the zero-re-key alias, no
re-mint), and the baseline's `exp_*` override is conditional. Both flows build.

What this directory keeps as a result:
- `gen_prj.tcl`'s port-direction patch is **idempotent** — accepts an
  already-correct port, still fails loudly on one that is missing, duplicated,
  or unrecognisably shaped, and reports flipped-vs-already-correct counts.
  Currently `exp_flips=0`, 13 already correct.
- The `.prj` is regenerated on every `make prj`, and the generator refuses to
  emit a project with <100 sources or any unresolvable path.

**RESOLVED 2026-09-10 — and the answer was neither option.** This section used to
end "two consumers now want the same four SWD partition pins ... somebody has to
choose". The choice turned out to be a false one. Both consumers can have the
wire-set at the same time, because that is what IEEE 1149.1 daisy-chaining is
for: shared TCK/TMS, cascaded TDI/TDO, and whichever TAP is not addressed sits in
BYPASS. So `IICE_OWNS_SWD` and the `dut_gpio_i[7]` mux are deleted rather than
defaulted, and the M0's DAP is never taken away. See
`docs/planning/IICE_JTAG_CHAIN.md` and `tests/jtag_chain/`.

## 7a. Synplify infers global buffers on DESIGN nets — fixed

The residual risk the original feasibility work flagged ("Synplify's own
global-buffer inference; the RP pblock has zero BUFG sites, so any inferred
clock buffer inside the RM is fatal") **materialised on the first real run**.
`make synth` was clean; `make dcp` linked 21,094 cells and then the no-BUFG gate
fired on two `BUFGCE`:

```
u_rm/u_nanosoc/u_qspi_flash_0/u_top_ahb_qspi/u_qspi_clock_div/partial_QSPI_SCLK_i_cb
u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/u_core_prmu/u_rstctrl/u_core_hresetn_sync/rst_sync2_n_buf
```

A divided QSPI clock and a reset synchroniser — **design nets, not IICE nets**.
Vivado does not do this promotion, which is why the baseline never hit it.

**Fix, from the installed documentation** (`fpga_attribute_reference.pdf`
pp.415-418: `syn_noclockbuf`, "Turns off automatic clock buffer usage", Xilinx
technology "all", `Global Support: Yes`; `fpga_reference.pdf` p.623: "for entire
modules or nets"; FDC-legality per `fpga_reference.pdf` p.150), one global line
in `nanosoc_iice.fdc`:

```tcl
define_global_attribute syn_noclockbuf {1}
```

Global rather than per-net on purpose: the RP can never host a BUFG at all, so
"never infer one, anywhere" is the real requirement and a per-net list would miss
the next net that crosses the fanout threshold. **Result: `cellRef BUFG` 2 -> 0
in the EDIF**, and `util_rm_nanosoc.rpt` §5 CLOCK all zeros. `syn_global_buffers`
and `set_option -globalthreshold` were both considered and rejected with reasons
recorded in the FDC.

Two guard defects this exposed, both fixed:
- The netlist gate's diagnosis blamed the `.idc`. It now **classifies** by
  hierarchy — IICE-inserted (fix: `.idc`) vs Synplify-inferred on a design net
  (fix: the FDC) — and prints the right remedy for each.
- The gate only existed at *checkpoint* time, four minutes into `make dcp`. The
  EDIF text says it directly, so `check-no-bscan` now greps `cellRef BUFG` too
  and names the offending instances.

---

## 8. Traps recorded here

1. **`device jtagport builtin` is fatal** — emits `BSCANE2` -> `HDPR-16 Illegal
   logic inside reconfigurable cell`, and there is no BSCAN site in the pblock.
   `check-no-bscan` is the **inversion** of `hello_ident/run.sh:39`, which gates
   the opposite way.
2. **"No BSCANE2" alone is not a pass** — an uninstrumented build also has none.
   The gate also requires `iice` references *and* `identify_jtag_tck`.
3. **Synplify speed grade is `-1-c`, not `-1`.** `xilinx_parts.txt:1687` offers
   XCKU115 as `{-3-e, -2-e, -1-c, -2-i, -1-i, -1L-i, -1LV-i}`. There is no bare
   `-1`. The Vivado part `xcku115-flvb1760-1-c` maps to `-speed_grade -1-c`.
4. **The EDIF must be named `<top_module>.edf`**, case-sensitive.
5. **`[Project 1-68]` has two unrelated causes.** The `.edf` filename above, and
   `link_design -top <an RTL module>` — `link_design` needs a netlist for the
   top, so it can never be used to wrap the EDIF in the RTL shim.
6. **`read_edif` + `synth_design` needs a `(* black_box *)` stub.** Without it:
   `ERROR: [Synth 8-439] module 'rp_nanosoc_iice_core' not found`.
7. **`read_checkpoint -cell` is not a workaround for 6.** Linking the core
   standalone, checkpointing it, and reading it into a separately-synthesised
   shim dies with `ERROR: [Project 1-9] Cannot open structural netlist because no
   structural source files were specified.`
8. **An unfilled black box passes every other gate.** 35 ports, no BSCANE2,
   timing trivially met — and no DUT in the bitstream. Hence the explicit
   `IS_BLACKBOX` + cell-count assertions.
9. **`module load synplify` drags in `vivado/2026.1`**, against the DFX flow's
   2024.1 pin. Call both by absolute path.
10. **`sdc2fdc` does not exist in this install** — only `pdc2sdc` and `qsf2sdc`.
    The FDC is a hand translation.
11. **Vivado searches a compiled file's own directory for `` `include ``;
    Synplify does not.** Every source directory is added to `-include_path` (the
    same fix `collect_filelist.tcl` applies for VCS). CG092's
    `p_flash_cache_f0_gen_const_pkg.vh` is the file that needs it.
12. **`.v` compiled as `v2001`, `.sv` as `sysv`** — per extension, mirroring
    `read_verilog` vs `read_verilog -sv`. A scan of all 213 `.v` files found SV
    keywords only in comments and assertion strings, so global `sysv` would
    probably work today; "probably, today" is not a reason to compile 213 vendor
    files under the wrong standard.
13. **`cm0_tarmac.v` needs no override** despite being full of `$fopen`/`$fwrite`/
    `$finish`/`wait`/`#2`: Arm already guarded it, `//synthesis translate_off` at
    line 177 to `translate_on` at 435. It is the only file in the 240 that uses
    `translate_off`, and `cmsdk_fpga_rom.v` — which needs one — has none.
14. **Tcl `$var(` is an array reference.** `gen_prj.tcl`'s direction regexes need
    `${_wrong}(...)`; without the braces you get
    `variable isn't array`. Cost one build.
15. **The upstream source list is a moving target.** 221 -> 240 files in nine
    minutes. This is why the `.prj` is generated on every `make prj` and why the
    generator refuses to emit a project with fewer than 100 sources or with a
    single unresolvable path.
16. **Synplify promotes high-fanout clock AND RESET nets onto global buffers;
    Vivado does not.** Fatal in an RP with zero BUFG sites. Fix is
    `define_global_attribute syn_noclockbuf {1}`. See §7a. Nothing in the `.idc`
    affects this — a guard that blames the `.idc` for it sends the reader to the
    wrong file.
17. **Cell names differ between the two flows, so never key a gate on one.**
    Vivado calls the IMEM's first block RAM `.../u_sram/mem_reg_0`; Synplify
    calls it `.../u_sram/mem_1_mem_1_0_0` and packs the 16 KiB IMEM as **four
    byte-lane RAMBs**. My first R2 gate looked up "the" IMEM cell by name, missed,
    silently fell back to the lexicographically-first cell (a zero-filled
    high-address slice) and **reported a byte-perfect ROM as broken**. A
    false-positive gate on a silent-failure item is worse than no gate. It is now
    a set-membership assertion — the known-good `INIT_00` must be present on *some*
    IMEM RAMB — and its error message tells the reader to grep the EDIF (plain
    text) before believing a regression.
18. **An obsolete guard fires on good input.** The baseline's `exp_*`
    port-direction override asserted the ports were still WRONG; upstream fixed
    them, so the assert failed on a correct file. When writing a "fail loudly if
    upstream drifted" check, make it tolerate upstream drifting *the right way*.


19. **A boundary can be deleted out from under an RM, and every gate here will
    still pass.** This directory's own `make lint` was green for weeks against a
    boundary the shell no longer had, because `pin_check` was being handed the
    two `.sv` files explicitly and *they agreed with each other*. What it could
    not see is that `fpga/shell/boundary.yaml` had moved. The lesson is that a
    self-consistent pair of files is not a conformance check; the authority has
    to be the generated boundary, and something has to run against it.
20. **The Arm SWJ-DP's TAP is X in simulation and the RM cannot fix it.**
    `cxdapswjdp` resets its TAP state and IR from `ntrst` (3 `negedge ntrst`
    blocks) and everything else from `npotrst` (17). The RM straps `dap_ntrst`
    HIGH because no TRST wire crosses the boundary, so in RTL sim that FSM starts
    at X and never resolves — five TCK with TMS high walk an X state to an X
    state and every IDCODE reads zero. On silicon Xilinx's GSR clears it at the
    end of configuration. `tests/jtag_chain` models that one device behaviour
    with a 100 ns pulse and says so loudly; without it the bench reports a dead
    DAP the hardware does not have.
21. **The obvious hazard was the wrong hazard.** "A DUT reset pulls
    `dap_npotrst`, so the TAP returns to TLR, reloads IDCODE, and the chain's DR
    width changes by 31 bits under the host" is plausible, was written down, and
    is FALSE — the TAP is behind `ntrst`. What a DUT reset really destroys is the
    DP power-up latch (`CTRL/STAT` `0xF8000000 -> 0xA8000000`), so the chain keeps
    scanning perfectly while every AP access is gated off. The test that was
    written to confirm the wrong hazard is what found the right one.
22. **A `-D` target flag can be numerically right and semantically dead.**
    `-DMPS3_XVC_TARGET_SWDBB` still drives the correct pins on the fielded shell
    — but only because `jtag_bb` happens to reuse `swd_bb`'s page AND bit
    positions. That coincidence is now a compile-time assertion in
    `xvc_server.h`, mutation-checked. A flag named after something that no longer
    exists is a bug waiting for the next renumbering.
