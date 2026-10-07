# shell_linux_bd — ADDRESS MAP + DEVIATION REGISTER

**Design:** `src/linux_harness/shell_linux_bd.tcl` — THE TRANSPLANT BD (the
static shell with the classic MicroBlaze replaced by MicroBlaze V + DDR4).
**Status:** VALIDATED 2026-07-15; **RE-VALIDATED 2026-07-16 after the
judge-fix wave** (see CHANGELOG + §5), Vivado **2026.1** (v2026.1 SW Build
6511674), part `xcku115-flvb1760-1-c`. `validate_bd_design PASSED` with
**zero ERRORs and zero CRITICAL WARNINGs on the deliverable BD**; all
post-propagation asserts, the frozen-base assert, the EMC-window assert and
the undriven-reset guard passed; the decoupler ALL_PARAMS generation gate
passed (`DECOUPLER_GEN_OK`); driver exit 0 with `SHELL_LINUX_BD_VALIDATE_OK`.
Every table below was cross-checked against `build/address_map.txt` — the
map READ BACK off the built BD, not this file's intent.
**Gate command:** `cd build && $XILINX_VIVADO/Vivado/bin/vivado -mode batch
-source ../validate_shell_linux_bd.tcl`. Design-only: no synth, no impl,
no board — no timing or pblock claim is made by this document.

## 1. Baseline declaration

Forks the **working-tree** `shell_bd.tcl` (branch `feat/clcd-kvm-display`,
41 cells): WITH `clcd_kvm_0` (Wave 4) and WITH the QSPI v0.2 boundary
(19-interface decoupler, `qspi_pad_*` ports, OVLSTORE `SPI_0` removed).
That baseline is AHEAD of the board-proven shell (2026-07-11,
`static_id 0xE4B1C44A`). Nothing here inherits board-proven status; a CPU
transplant re-mints `static_id` and forces re-implementation of **all 8 RM
overlays** regardless (contract §7 law).

## 2. Address map (as instantiated — `assign_bd_address` explicit, never auto)

### 2.1 `microblaze_riscv_0/Data` space (peripherals via M_AXI_DP, DDR via M_AXI_DC)

| Base | Range | Block | Segment | Notes |
|---|---|---|---|---|
| `0x0000_0000` | 128 KiB | LMB boot BRAM (data port) | `dlmb_bram_if_cntlr/SLMB/Mem` | [DEV-2] was 1 MiB firmware RAM |
| `0x4060_0000` | 64 KiB | UARTLITE (console fallback) | `axi_uartlite_0/S_AXI/Reg` | frozen |
| `0x4120_0000` | 64 KiB | INTC | `axi_intc_0/s_axi/Reg` | frozen; lowercase `s_axi` at 2026.1 [DEV-8] |
| `0x41C0_0000` | 64 KiB | TIMER | `axi_timer_0/S_AXI/Reg` | frozen; 2 counters pinned [DEV-6] |
| `0x44A0_0000` | 64 KiB | CLKRST | `dut_clkrst_0/s_axi/reg0` | frozen, width-32 law |
| `0x44A1_0000` | 64 KiB | DFXCTL | `dfx_ctl_0/s_axi/reg0` | frozen |
| `0x44A2_0000` | 64 KiB | HWICAP (FIFO mode) | `axi_hwicap_0/S_AXI_LITE/Reg` | frozen; no in-tree Linux driver — custom kernel work, flagged not solved |
| `0x44A3_0000` | 64 KiB | VPHY | — | **RESERVED, not instantiated** (unchanged) |
| `0x44A4_0000` | 64 KiB | OVLSTORE (vestigial reg window) | `axi_quad_spi_0/AXI_LITE/Reg` | SPI master drives nothing (QSPI v0.2 / D16) |
| `0x44A5_0000` | 64 KiB | TELEM | `telem_0/s_axi/reg0` | frozen |
| `0x44A6_0000` | 64 KiB | GENCHK | — | **RESERVED, not instantiated** (unchanged) |
| `0x44A7_0000` | 64 KiB | JTAGBB | `jtag_bb_0/s_axi/reg0` | frozen (page unchanged; block swapped [DEV-10]) |
| `0x44A8_0000` | 64 KiB | DBGBR (XVC) | `debug_bridge_0/S_AXI/Reg0` | frozen; survives swaps |
| `0x44A9_0000` | 64 KiB | UARTBR | `uart_bridge_0/s_axi/reg0` | frozen; reads destructive (regmap v0.5) |
| `0x44AA_0000` | 64 KiB | GPIO | `board_gpio_0/s_axi/reg0` | frozen |
| `0x44AB_0000` | 64 KiB | MMCM_DUT_DRP | `clk_wiz_dut/s_axi_lite/Reg` | frozen; the ONLY DUT-clock reconfig path |
| `0x44AC_0000` | 64 KiB | CLCD | `clcd_0/s_axi/reg0` | frozen; live on silicon in donor |
| `0x44AD_0000` | 64 KiB | CLCDKVM | `clcd_kvm_0/s_axi/reg0` | Wave-4 (unproven on board) |
| `0x8000_0000` | 1 GiB | **DDR4 (Linux system RAM)** | `ddr4_0/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK` | NEW; via M_AXI_DC (cached) |
| `0xC000_0000` | 16 MiB | LAN9220 via AXI EMC (16-bit) | `axi_emc_0/S_AXI_MEM/*` | frozen; asserted post-validate (no silent `-quiet` miss) |

1 GiB not 2 GiB: rv32 Linux has no highmem — lowmem linear map caps at 1 GiB
(mbv_soc.tcl §0 rationale, carried verbatim).

### 2.2 `microblaze_riscv_0/Instruction` space

| Base | Range | Block | Segment |
|---|---|---|---|
| `0x0000_0000` | 128 KiB | LMB boot BRAM (instr port) | `ilmb_bram_if_cntlr/SLMB/Mem` |
| `0x8000_0000` | 1 GiB | DDR4 | via M_AXI_IC (cached) |

Reset vector / trap base (`C_BASE_VECTORS`) = `0x0000_0000` → boots from BRAM,
never from uncalibrated DDR. Cache apertures (I+D) = exactly
`0x8000_0000..0xBFFF_FFFF`, derived from the same constants (never re-typed).

### 2.3 IRQ map (xlconcat index == INTC input == DTS interrupt number)

| INTC bit | Source | Level rationale |
|---|---|---|
| 0 | `axi_hwicap_0/ip2intc_irpt` | IER/ISR interrupt block, level |
| 1 | `axi_timer_0/interrupt` | held until T0INT W1C — Linux clockevent **on SEIP** via `soclabs,mbv-timer` (errata E1/E2, see §2.3a); the level typing is load-bearing for the driver's ISR ack |
| 2 | `axi_uartlite_0/interrupt` | FIFO-state level |
| 3 | BD port `eth_irq` (LAN9220) | **software obligation:** LAN9220 `IRQ_CFG` must be programmed push-pull ACTIVE-HIGH before `smsc911x` unmasks it (donor firmware polled and never enabled the pin; the pad IS wired — contract red-flag "ETH_INT not wired" refers to the *donor poll model*, the `eth_irq` port + In3 wiring exists in both BDs) |

INTC pinned `C_KIND_OF_INTR 0x00000000` (all level), `C_IRQ_IS_LEVEL 1`,
`C_IRQ_ACTIVE 0x1`, `C_HAS_FAST 0` [DEV-5]. `C_NUM_INTR_INPUTS` is read-only
and asserted `== 4` post-validate.

**kind-of-intr manual-vs-computed — RESOLVED [FIX-C].** Vivado propagation
warns that the manual `0x00000000` differs from its computed sensitivity word
`0x00000004` (the uartlite `interrupt` pin is metadata'd EDGE_RISING; observed
in this design's validate log, matching linux_soc `build_dbg_bd.log:1379`).
Expected and benign — do not adopt the computed value: the linux_soc IRQ audit
(`../linux_soc/hw/build_dbg/IRQ_WIRING_AUDIT.md` §2, source-verified against
`axi_intc_v4_1_rfs.vhd` ~1964) settled that axi_intc's LEVEL-mode `LVL_P`
detector **latches** the 1-cycle pulse and holds it until IAR ack (and
same-clock inputs have no synchroniser to swallow it), so level-forced is
hardware-safe and keeps one irq_chip flow for all four lines. Guarded by a
POST-validate `_assert_cfg` re-read (the manual value must survive
propagation — it does: `intc_kind_of_intr = 0x00000000` in
`build/address_map.txt` is now a read-back, not intent) and mirrored by the
DTS `xlnx,kind-of-intr = <0x0>`. **`rp_irq_out` is still NOT an interrupt**
— terminated at the decoupler clamp with `s_irq_out_DATA` a documented
dangling tie-off (dut_clk-domain CDC hazard + data_rtl type + no consumer;
contract §4). Future recipe unchanged: 2-FF ASYNC_REG sync + regmap entry +
spare INTC line.

### 2.3a Kernel timebase — SILICON ERRATA E1/E2/E3 (transplant-fold 2026-07-17)

The MicroBlaze V's architected S-mode timer is UNUSABLE on this silicon (proven
on the linux_soc donor, MPS3 KU115, board windows 2026-07-16/17). Two coupled
errata:

- **E1 "Sstc dead"** — writing `stimecmp` never raises a deliverable S-mode
  timer interrupt (the `rdtime`/Zicntr counter runs correctly at 100 MHz, so it
  stays the clocksource; only the clockevent is broken).
- **E2 "STIP stuck"** — `mip.STIP`, once set from M-mode, can never be cleared
  again, so an SBI/plan-C-injected STIP livelocks the kernel in its timer
  handler.

External-interrupt delivery is healthy (MEIP+SEIP taken, `mideleg=0x222`), so
the fix (silicon-proven: 2500+ ticks, zero spurious) moves the tick onto the
**AXI timer delivered as a normal S-mode external interrupt (SEIP)** through
`axi_intc` In1. Two coupled changes, both in `shell_linux.dts`, neither a BD
hardware change:

1. `axi_timer` compatible LEADS with `"soclabs,mbv-timer"` → binds the errata
   clockevent driver (`linux_soc/linux/patches/linux/0003-clocksource-soclabs-
   mbv-*.patch`; needs `CONFIG_SOCLABS_MBV_TIMER=y` in the kernel fragment). The
   driver uses timer channel 0; its ISR ack sequence (T0INT W1C + read-back,
   then own-bit INTC IAR scrub + read-back) depends on the In1 LEVEL typing
   (§2.3) — do not edge-type it.
2. `"sstc"` is DROPPED from the cpu `riscv,isa-extensions` so the kernel never
   programs the dead comparator; `"zicntr"` stays (rdtime clocksource).

The BD keeps `C_USE_SSTC=1` as a LATENT capability — flipping the DTS back on
and dropping the `soclabs,mbv-timer` prefix (same commit) is the re-enable path
once AMD ships fixed IP; one kernel binary serves both.

**E3 "wfi never wakes"** is a separate HARDWARE erratum, fixed in the BD by
`C_INTERRUPT_WAKEUP=1` (DEV/FIX-A, re-verified this fold; IP default 0 = a core
that `wfi`s at first idle never wakes). No DT representation. The off-board
fallback (`Image_nowfi`, wfi patched out) is retained as a diagnosis aid only.

Console stays the STOCK IRQ-driven uartlite (In2) — the SEIP path is proven
healthy alongside the errata tick, so no `xlnx,rx-poll-ms` belt-and-braces.

### 2.4 Clock/reset summary

| Domain | Source | Consumers |
|---|---|---|
| `shell_clk` 100 MHz | `clk_wiz_shell/clk_out1` (OSCCLK1 50 MHz in) | **MBV core + caches SI-side**, LMB, all 16 interconnect masters, every CSR/vendor `s_axi_aclk`, ICAP clk, EMC rdclk, smartconnect_ddr `aclk` [DEV-3] |
| `rmii_ref_clk` 50 MHz fixed | `clk_wiz_shell/clk_out2` | `rp_phy_rmii_ref_clk` (unchanged) |
| `dut_clk` 50 MHz DRP | `clk_wiz_dut/clk_out1` | RP, dut_clkrst heartbeat, uart_bridge CDC (unchanged) |
| `ui_clk` 200 MHz | `ddr4_0/c0_ddr4_ui_clk` | smartconnect_ddr `aclk1`, MIG AXI shim, `ddr_axi_rst_inv` (NEW) |

| Reset | Generator | Notes |
|---|---|---|
| `proc_sys_reset_shell` | sys_rst_n + clk_wiz_shell lock | peripheral/interconnect resets UNCHANGED — shell fabric does NOT wait on DDR calibration [DEV-4] |
| `proc_sys_reset_cpu` (NEW) | sys_rst_n + lock + **aux_reset_in = c0_init_calib_complete** | `mb_reset` → MBV + LMB (active-HIGH pairing); `interconnect_aresetn` → smartconnect_ddr. CPU cannot execute before DRAM calibrates ([MBV-LESSON]) |
| `ddr4_0/c0_ddr4_aresetn` | `NOT(c0_ddr4_ui_clk_sync_rst)` | **INPUT, MUST be driven** — the silent-1'b0 wedge class; guarded by the undriven-reset check |
| `ddr4_0/sys_rst` (active-HIGH) | `NOT(sys_rst_n)` | the polarity trap, explicit inverter |
| dut_clkrst 3 RP resets | unchanged | rp_resetn-held-low = input-side isolation, unchanged |

### 2.5 Frozen half (verified unchanged)

19-interface decoupler `ALL_PARAMS` verbatim (hex strings; `qspi_csn=0x1`
sole non-zero clamp); dfx_ctl/dut_clkrst/shutdown-mgr/inv_rp_in_reset nets
verbatim; all partition-pin ports names+widths verbatim (partition-pins v0.2);
`clk_wiz_dut` AXI-Lite-only DRP; EMC slow LAN9220 AC timing verbatim;
debug_bridge `C_DEBUG_MODE 2`; CLCD/KVM pad chain + interlocks verbatim;
VPHY/GENCHK pages reserved; deferred RMII group idle ties + pre-authored
clamps verbatim. RP pblock/ICAP/SLR facts are constraint-side
(`dfx_floorplan.xdc`) and untouched by this BD; DDR4 pins are SLR1 banks
49-51 (`ddr4_pins.xdc`) — separable from the SLR0 RP pblock as required.

## 3. Deviation register (complete, [DEV-n] tags match the BD script)

| # | Deviation | Rationale / consequence |
|---|---|---|
| DEV-1 | Baseline = working tree (Wave 4 + QSPI v0.2), not the board-proven 0xE4B1C44A build | Declared per contract baseline warning; the fork target is the frozen boundary the successor must keep |
| DEV-2 | `microblaze_0`/`mdm_1`/`ilmb`/`dlmb`/ctlrs/`local_ram`(1 MiB) removed; `microblaze_riscv_0` (rv32imac+Zba/Zbb/Zbs, Sv32) + `mdm_riscv_0` + 128 KiB `lmb_bram` boot BRAM added | Contract §9.1/§9.2. **LMB JTAG diag mailbox (0xFFF80, magic 0xD1A6C0DE) RETIRED** — `mps3_diag.tcl`, tier3 gates, `check_diag_mailbox_parity.py` lose their substrate; successor mid-swap diagnostics must be re-homed (ramoops in DDR / kernel-driver mailbox) — explicitly owed, not designed here. The 1 MiB "core + 2× clearing" invariant moves to the Linux service design (`check_clearing_fits.py` equivalent in userspace) |
| DEV-3 | CPU/AXI clock = `shell_clk` (OSCCLK1-derived 100 MHz), not ui_clk-derived as in `mbv_soc.tcl` | Contract §9.3 option (a), recommended: zero disturbance to the 16-slave fabric and the shell clock stays independent of DDR calibration; smartconnect_ddr (NUM_CLKS 2) does the 100↔200 crossing exactly as the proven recipe's dual-clock config |
| DEV-4 | Second reset controller `proc_sys_reset_cpu` (calib-gated) instead of gating the single shell PSR | mbv_soc gated its whole SoC on calib; doing that here would make HWICAP/DFXCTL/XVC dead if DDR4 never calibrates. Shell fabric keeps donor reset semantics; only CPU+LMB+DDR path waits for calib. Release order stays slave-live-first |
| DEV-5 | `axi_intc_0` pinned to the Linux irq contract (all-level, active-high, no fast) | Donor left defaults (poll-model firmware). Values must equal DTS `xlnx,kind-of-intr` |
| DEV-6 | `axi_timer_0` `enable_timer2` pinned {1} + asserted | Channel 0 is THE kernel clockevent on SEIP via `soclabs,mbv-timer` (errata E1/E2, §2.3a); rdtime/Zicntr is the clocksource (NOT counter 1). Same `enable_timer2` value the donor inherited — kept loud; channel 1 spare |
| DEV-7 | New BD ports: `c0_sys_clk_p/n`, external intf `c0_ddr4` (renamed from auto `C0_DDR4_0` — XDC name match is load-bearing), `calib_complete_led_n` | Successor `shell_top` must add these pads (OSC6 + DDR4 + one LED); the board-proven `shell_top.sv` is not touched. Boundary change law: static change anyway |
| DEV-8 | Interface-name case resolved per 2026.1 catalogue (`axi_intc` lowercase `s_axi` etc.) via `_find_intf`/`_find_seg` candidates | 2024.1→2026.1 drift; resolved names visible in `build/address_map.txt` |
| DEV-9 | Authored/validated at Vivado 2026.1 (donor: 2024.1); donor VLNVs requested exactly, any substitution is loud (`::vlnv_substitutions`, echoed into address_map.txt) | The 2024.1-confirmed facts (decoupler ALL_PARAMS schema, clk_wiz AXI-only DRP, EMC property names) are re-verified by this validate run at 2026.1 |

Also deliberate (not BD deviations, but stated): `save_bd_design` + validate +
decoupler-only `generate_target synthesis` is the gate — **no synth/impl, no
pblock/timing claims are made here** (DFX timing must gate on dut_clk, known
signoff gap inherited). Wire-compat (:6900/:6910 et al.) is untouched by this
BD; it binds the Linux userland, not the fabric.

## 3A. Boot-BRAM provisioning — ownership decided [FIX-D]

The 128 KiB LMB BRAM at the 0x0 reset vector needs CONTENTS; deciding whose
problem that is was a judge-flagged gap. Decision (normative; the BD carries a
summary at the `lmb_bram` cell):

1. **Baked stub — owned by the HW track.** The BRAM image shipped inside the
   bitstream is a diagnostic **park stub**, ported from the proven
   `../linux_soc/hw/fw_dbg/bootstub.*` (rv32, `-nostdlib`, banner + DDR-calib
   report on the UARTLITE @0x4060_0000, then a spin loop). It must never jump
   into DDR of its own accord and never write DDR above the first probe word.
   Bake path: `blk_mem_gen` COE at BD build time, or `updatemem` + MMI against
   the routed DCP (both proven in linux_soc). An all-zero BRAM is *safe*
   (0x00000000 = illegal instruction → trap loop at the 0x0 trap base) but not
   acceptable as the deliverable: the stub is what makes "CPU alive, DDR dead"
   remotely distinguishable from "CPU dead" — the same reason
   `calib_complete_led_n` exists, but readable off-bench.
2. **Linux payloads — owned by the boot track, XSDB-loaded, NEVER baked.**
   `fw_jump.bin` / `Image` / DTB / initrd are `dow -data`'d into DDR and
   started with `rwr a0 0; rwr a1 <dtb>; rwr pc 0x80000000; con` (the
   `mk_dtb.sh` recipe, silicon-proven 2026-07-16). Constraint restated:
   **never `elf load fw_jump.elf`** — it is linked at vaddr 0x0 (THIS BRAM)
   and is 276 KiB > 128 KiB.
3. **A self-booting first stage** (SREC/TFTP/QSPI loader in BRAM, removing the
   XSDB dependency) is a future work item with its own owner — deliberately
   NOT implied by this BD or this document.

Interaction with [DEV-4]: the CPU (and hence the stub) is held in reset until
DDR4 calibrates, so the stub's calib print is normally trivially `1`; if
calibration never completes the CPU never runs and the ONLY external signals
are the dark calib LED + the shell fabric still being alive (deliberate DEV-4
property). That boundary is accepted and documented rather than "fixed".

## 4. Inherited open items (not created here)

D7 RP pblock sizing; D12 real DUT-clock target; zero CLCD 8080 pad timing;
QSPI XiP I/O timing signoff / cold-XiP livelock / silicon bring-up;
LAN9220 AC table unverified (A6 — the conservative EMC timings stand);
ethernet MAC-verification subsystem deferred (decoupler IDs 1–5 sinks
dangling by design); no in-tree Linux fpga-manager driver for AXI HWICAP
(custom kernel driver spec'd in SERVICE_DISPOSITION.md §4, not solved here).

## 5. Validation evidence (run of 2026-07-16, `build/run_stdout_fixwave.log`;
the 2026-07-15 pre-fix run is preserved in `build/run_stdout.log`)

| Gate | Result |
|---|---|
| Every `_assert_cfg` read-back (MBV **30** params — now incl. `C_USE_COUNTERS`/`C_USE_SSTC`/`C_INTERRUPT_WAKEUP` [FIX-A], INTC, TIMER, UARTLITE, HWICAP, EMC 9 params, smartconnect_ddr, 3 inverters, 8× width-32 law) | all "read back as requested" |
| POST-validate re-asserts [FIX-A]/[FIX-C]: MBV SSTC/COUNTERS/INTERRUPT_WAKEUP ==1 and INTC `C_KIND_OF_INTR`==0x0 surviving propagation | all "read back as requested" (log lines 955, 961) |
| kind-of-intr manual-vs-computed warning (`axi_intc:4.1-7`, manual 0x0 vs computed 0x4) | present, EXPECTED+BENIGN (§2.3 [FIX-C]; LVL_P latch) — the manual value shipped |
| `ddr4_ip.tcl` snapped-parameter counter | 0 (driver refuses on non-zero). NB at 2026.1 `SUPPORTS_NARROW_BURST` on `C0_DDR4_S_AXI` is read-only ([BD 41-737]) and already reads back `1` — the BFM byte-enable fix is now the IP default; the read-back check still guards it |
| `validate_bd_design` | **PASSED** — zero ERRORs, zero CRITICAL WARNINGs on the deliverable BD |
| `C_NUM_INTR_INPUTS` post-propagation | 4 (read-only, derived from xlconcat) |
| LAN9220 EMC window | confirmed `0xC000_0000/16M` (real segment `SEG_axi_emc_0_MEM0` — the `-quiet` wildcard landed, then asserted) |
| Frozen bases | all 16 present in `microblaze_riscv_0/Data` at contract offsets |
| Undriven-reset guard | PASSED (whitelist: 2× `mb_debug_sys_rst`, donor-inherited `proc_sys_reset_shell/aux_reset_in`) |
| VLNV drift | **none** — every donor IP version (incl. `dfx_decoupler:1.0`, `axi_emc:3.0`, `axi_hwicap:3.0`, `clk_wiz:6.0`) exists in the 2026.1 catalogue. 2026.1 does deprecation-warn that `util_vector_logic`/`xlconcat`/`xlconstant` "will not be supported from release 2025.2" in favour of inline-HDL variants — informational, kept as donor IP for wire-compat with the donor BD |
| Decoupler ALL_PARAMS generation gate | `DECOUPLER_GEN_OK` — generated via a scratch sibling BD (`decoupler_gen_gate`) holding one decoupler with the byte-identical dict (a BD-nested XCI cannot be generated directly: [Vivado 12-3563], found live; the gate BD's own dangling `decouple` pin is the only 41-759 in the log and is not part of the deliverable) |
| Driver | exit 0, `SHELL_LINUX_BD_VALIDATE_OK` |

Read-back highlights from `build/address_map.txt`: MMU=3 (Sv32 SUPERVISOR),
atomics=1, compression=1, `counters_zicntr=1` (clocksource), `sstc=1`
(**latent HW capability only — the deployed DTS drops sstc per errata E1/E2;
clockevent is the AXI timer on SEIP via `soclabs,mbv-timer`, §2.3a**),
`interrupt_wakeup=1` (E3 wfi-coma fix) [FIX-A], `intc_kind_of_intr=0x00000000`
(post-validate read-back [FIX-C]),
`riscv_isa=rv32imac_zba_zbb_zbs` **computed from the CONFIG read-backs, no
longer hardcoded** [FIX-B], cache aperture == DDR aperture, LMB 0x20000
(128 KiB) in BOTH I/D spaces, DDR `0x8000_0000/0x4000_0000` in BOTH cache
spaces, `vlnv_substitutions=none`.

What this run does NOT prove (unchanged honesty list): timing (incl. the
dut_clk DFX gate), pblock fit (D7), IP generation of the FULL BD (only the
decoupler was generation-gated; the DDR4/MBV halves generate-cleanly per the
linux_soc build but not re-proven here), synthesis of the packaged CSR RTL
(validate elaborates structure, not RTL — donor lesson), and anything on a
board. The two CRITICAL WARNINGs in the log are both pre-existing and
documented: [BD 41-737] `SUPPORTS_NARROW_BURST` read-only (ddr4_ip.tcl,
pre-deliverable, reads back 1 = the desired value) and [BD 41-759] the scratch
gate BD's own dangling `decouple` pin (not part of the deliverable).

## CHANGELOG

- **2026-07-17 (M7b transplant-fold):** silicon errata E1/E2/E3 folded in from
  the linux_soc board windows. New §2.3a (kernel timebase errata): the tick
  moves off the dead architected S-mode timer onto the AXI timer on SEIP via the
  `soclabs,mbv-timer` errata driver, "sstc" dropped from the deployed DTS,
  rdtime stays the clocksource. §2.3 In1 row + DEV-6 + the read-back highlights
  updated to say sstc is a latent capability (NOT the clockevent). No BD
  hardware change: `C_USE_SSTC=1` kept latent, `C_INTERRUPT_WAKEUP=1` (E3)
  re-verified, In1 unchanged. DTS edits in `shell_linux.dts`; BD comments in
  `shell_linux_bd.tcl`. Re-validated (`build/`, `SHELL_LINUX_BD_VALIDATE_OK`).
- **2026-07-16 (judge-fix wave):** [FIX-A] `mbv_cfg` pins + asserts
  `C_USE_COUNTERS`/`C_USE_SSTC`/`C_INTERRUPT_WAKEUP` (=1, the silicon-proven
  wfi-coma fix), re-asserted post-validate. [FIX-B] `riscv_isa` in
  `address_map.txt` computed from read-backs. [FIX-C] kind-of-intr
  manual-vs-computed resolved per the linux_soc IRQ audit (LVL_P latch) —
  documented in §2.3 + post-validate assert. [FIX-D] new §3A: boot-BRAM
  provisioning ownership (baked park stub = HW track; XSDB-loaded Linux
  payload chain = boot track; self-booting first stage = future item).
  §5 refreshed against the 2026-07-16 re-validate
  (`build/run_stdout_fixwave.log`, `SHELL_LINUX_BD_VALIDATE_OK`, exit 0).
- **2026-07-15:** initial authoring + validate.
