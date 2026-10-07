# `usd_spi` — shell integration (D13)

**Status: APPLIED (2026-09-23, Wave 2 lane D13 I-SHELL).** The five patches lane
D13-L1 wrote against `7b2be6d` were re-derived on the ONE-BD tree (the CPU seam,
`6e5c546`) and applied in the working tree of `feat/usd-overlay-store`. The
patch files are gone: they no longer applied (the BD moved and the regmap was
regenerated), and git history keeps them (`0a825a0`).

`usd_spi_0` replaces the pad-less `axi_quad_spi_0` (OVLSTORE) on `0x44A4_0000`,
64 KiB, interconnect port **M03**. It is in the shared BD, so **both**
`SHELL_CPU` variants carry it (under `mbv` it is **kernel**-owned: `spi-usd` →
`mmc_spi`). Seven static-only pads; no RP boundary change.

## 1. Where each patch's intent landed

| Was | Now (files) | Notes |
|---|---|---|
| 01 BD / top / XDC | `fpga/shell/bd/shell_bd.tcl`, `fpga/shell/shell_top.sv`, `fpga/shell/constraints/mps3_harness.xdc`, `fpga/shell/constraints/mps3_harness_timing.xdc`, `fpga/shell/ip_packaged/package_csr_ip.tcl` | BD, top, pins and packaging as written, at the new line numbers. **Timing changed** after a routed pad test (§5): the outputs' `-max 8 / -min -2` (copied from the CLCD block) is a zero-wide window and failed by 5.6 ns — now `-max 0 / -min 0` (the [0, 10] ns window the comment meant); MISO `-hold 2` failed by 2.4 ns on a race the RTL does not run — now `-hold 3` (the replaced bit was captured one cycle before the launch); MISO `-max` 21 → 24 (measured SCK clock-to-pad). **Plus** an explicit `set_property IOB TRUE` on `*usd_spi_0*/miso_q_reg` in the (implementation-only) timing XDC, with a WARNING when it does not find exactly one cell: the BD synthesises the block out of context, where the RTL `IOB` attribute has no buffer to pack into. |
| 02 gates | `scripts/harness_gates/check_bd_config_lint.py`, `check_bench_param_parity.py`, `check_packaged_ip_fresh.py` | As written, **plus**: check 3 now scans every `fpga/shell/bd/*.tcl`, not only `shell_bd.tcl` — since the seam, a cell created in `cpu_mb.tcl`/`cpu_mbv.tcl` is as much the static as one in `shell_bd.tcl`. Controls: the pre-D13 BD fails; an `axi_quad_spi` added to `cpu_mbv.tcl` fails. |
| 03 regmap | `tools/gen_regmap.py` (+ every generated view), `docs/contracts/shell-regmap.md` (v0.7, USD section), `tests/common/test_regmap.py`, `tests/firmware_logic/test_regmap_conformance.py` | As written, **plus** the MBV view the SHELL lane added since: `USD` moves from `MBV_PENDING` into `MBV_OWNERS` (kernel), `OVLSTORE` (owner `none`) is deleted, `pending_owners` is now `{}`. `DECL_ALIASES` keeps `OVL → USD`: the parked fork `src/linux_harness/shell_linux_bd.tcl` still declares `MAP_OVL_BASE` and `tools/gen_dts.py` still reads that file (`FORK_BD`), so the fork is not retired. |
| — DTS | `src/linux_harness/shell_linux.dts` (via `tools/gen_dts.py`) | The `OVLSTORE` "no node" line is gone; `usd: spi@44a40000` (`soclabs,usd-spi-1.0` + `mmc-spi-slot`) and alias `spi0 = &usd` appear. `tools/dts_gates.py` 21/21, G7 now against the derived USD offsets. |
| 04 socket harness | `host/socket_harness/{registers,endpoints}.py`, `tests/test_endpoints.py`, `README.md` | As written. |
| 05 bench + lint | root `Makefile` (`LINT_SV`), `tests/Makefile` (`BENCH_DIRS`, `COVERAGE_BENCHES`), `tests/common/list_benches.py` | As written, **plus** the bench split below. |

**The bench split.** `tests/usd_spi`'s default goal (what `make check` stage 7 and
`make -C tests` run) is now `REGRESS_CFGS = bdfast w12` (~1.5 min).
The full `bd` configuration — width 32 **and** the shipped 10 ms
`DEBOUNCE_CYCLES` — is `make -C tests/usd_spi premint` (~5 min), and it is wired
into the **pre-mint gate**: `fpga/dfx/Makefile` `mint-preflight` (mint stage
1/8) runs it before any static is built, sourcing `set_env.sh` if `vcs` is not on
`PATH`, and FAILS without a simulator. `USD_PREMINT=skip` is the explicit, loud
escape hatch; the stamp records `usd_premint=PASS|SKIPPED`. The recipe uses
`$(MK)`, not `$(MAKE)`, so `make -n mint` stays a plan and does not run the sim.

**The CPU-seam gate** (`tests/shell_cpu_seam`). The bare-metal BD is no longer
"identical to eafe787": it differs by exactly
`tests/shell_cpu_seam/golden/d13_usd_delta_mb.txt` (247 lines: the
`axi_quad_spi_0` cell out, the `usd_spi_0` cell + 8 ports + 8 nets in, and the
M03 path's propagated `arprot/awprot` width and `NUM_*_OUTSTANDING` 2 → 1).
`seam_dump.py compare --expect-delta` enforces it; the pytest holds every line of
the golden to the cell swap.

## 2. The pads and their timing (design rationale)

**`shell_top.sv`**: `USD_CLK` (out, `OBUFT`), `USD_CMD` (inout), `USD_DAT[3:0]`
(inout), `USD_NCD` (in). `T = ~oe` (the `board_gpio` convention); `DAT[1]`/`DAT[2]`
are permanently `T=1`; `USD_NCD` goes into the BD raw (`usd_spi` synchronises and
debounces it). They sit after the MCC tie-offs and before the
`ifdef MPS3_SHELL_TOUCH` block, so every define combination keeps a legal port list.

**`mps3_harness.xdc`**: `USD_CLK` AU15, `USD_CMD` AU16, `USD_DAT[0..3]`
AV14 / AV13 / AT13 / AT12, `USD_NCD` AT15, all `LVCMOS33`; `PULLUP` on `CMD`,
`DAT[0..3]`, `NCD` (these hold `CS#` deasserted and card-detect at "no card" while
`usd_spi` floats the pads); `USD_CLK` unpulled.

**`mps3_harness_timing.xdc`** (the CLCD 8080 model: the RTL counts the margin in
`shell_clk` cycles, the constraints stop the tool eroding it):

- `USD_CLK`, `USD_CMD`, `USD_DAT[3]`: `set_output_delay -max 0.0 / -min 0.0`
  against `$clk_shell`, i.e. every clock-to-pad in [0, 10] ns — pairwise skew
  ≤ 10 ns, so ≥ 10 ns setup and hold at the card at DIV=1 (25 MHz) against SD
  tISU = tIH = 5 ns. (Setup checks `arrival ≤ T − max`, hold `arrival ≥ −min`:
  the CLCD block's `-max 8 / -min -2` is the window [2, 2] ns.)
- `USD_DAT[0]` (MISO): a real multicycle input — `set_input_delay -max 24.0 /
  -min 0.0`, `set_multicycle_path 3 -setup` / `3 -hold` `-from` the port (the RTL
  samples `2(DIV+1)−1` = 3 cycles after the SCK-falling launch at DIV=1, and the
  bit that launch replaces was captured one cycle before it), and the sample
  register `miso_q` forced into the IOB (above).
- `USD_NCD`: false path (mechanical switch, 2-FF `ASYNC_REG` + 10 ms debounce).
  `USD_DAT[1:2]`: false path (structurally dead).
- Every `get_ports` is `-quiet` and `llength`-guarded, with a WARNING.
- The numbers are first-pass estimates; the caveats block in the XDC lists them.

## 3. Firmware (lanes I-FW / L3a, not this lane)

The regen removes `MPS3_OVLSTORE_BASE` and the generated `OVLSTORE_SPI_*`
offsets. Their users are SST26 code handover §4.5b says to **delete**, not to
alias: **no compat `#define`** (it would send SST26 opcodes to an SD card). Until
I-FW lands, `firmware/test` and the host-gcc pytest are red on exactly those
names. The hand-written `OVLSTORE_SPI_*` **bit** defines outside the generated
blocks of `platform_regs.h` are firmware's to replace.

## 4. Other consumers

| Consumer | Status |
|---|---|
| `tests/services_rp/device_facts.py` | Measured on the fielded checkpoint (74 bonded IOB). The next mint adds 7; the `== 74` literal follows when the facts are regenerated from it. |
| `boundary.yaml`, `pin_check`, `check_shell_top_boundary` | Unaffected (no `rp_*` change); green. |
| `docs/planning/linux_lanes/SHELL_CONTRACT.md` row `0x44A4`, item L-3; `docs/BUILD_AND_MINT.md` stage-1 row | Stale prose (SHELL / FLOW own them): the row should read USD / `usd_spi_0` / kernel, L-3 is done, the seam gate is "identical modulo the pinned D13 delta", and preflight now runs `tests/usd_spi premint`. |
| `fpga/shell/README.md` inventory, `docs/MPS3_ONBOARD_FLASH_MAPPING.md`, `docs/ARCHITECTURE.md`, `firmware/README.md` | Prose that still says OVLSTORE / Quad SPI. Follow-up only. |

## 5. Verification (2026-09-23, build host, single-thread Vivado, nice 19)

| Check | Result |
|---|---|
| `tools/gen_regmap.py`, `tools/gen_dts.py`, `check_generated_fresh.py` | 4 generators, 13 views fresh |
| Tier-0 gates (`check_bench_param_parity`, `check_bd_config_lint` + 2 controls, `check_packaged_ip_fresh`, `check_shell_top_boundary`, the rest of `make check` stage 1–2) | all OK |
| `tools/dts_gates.py` / `make check-linux-dts` / `make check-linux-seam` | 21/21 · 44 PASS · 41 passed |
| `make lint` | OK, including `usd_spi.sv` and `clcd_kvm.sv` |
| `tests/usd_spi` default (`bdfast`, `w12`) / `premint` (`bd`) | 27/27, 27/27 / 27/27 |
| Vivado 2024.1 package `usd_spi` | `s_axi` + `reg0`, eight scalar pad ports, no stray clock interface |
| `SHELL_CPU=mb` validate (2024.1, `shell_bd_dump_run.tcl`) | `validate_bd_design` clean, `SHELL_BD_GUARDS_OK cpu=mb`, delta == golden, clamp wiring unchanged; the eafe787 baseline still hashes to the recorded golden |
| `SHELL_CPU=mbv` validate (2026.1) | clean, `SHELL_BD_GUARDS_OK cpu=mbv`, clamp wiring unchanged, `seam_dump.py regmap` OK (USD in the Data space at `0x44A40000`/64K) |
| Pad test, Vivado 2024.1 (the real `usd_spi.sv`, `shell_top`'s pad glue verbatim, the XDC pin + timing sections extracted verbatim, a 100 MHz MMCM clock; synth → place → route) | `miso_q` packed: `IOB=TRUE`, `BITSLICE_COMPONENT_RX_TX.IN_FF`. All seven pads in **bank 84**, `LVCMOS33`, `PULLUP` as specified. DRC: 0 errors / 0 critical (warnings: `BUFC-1` ×4 = the unused IOBUF `O` pins, by design; `CFGBVS-1` = pad-test artefact). Timing with the patch's constraints: outputs −5.6 ns, MISO hold −2.4 ns → fixed (§1); after: outputs +2.0 ns setup / +2.3 ns hold, MISO setup +5.8 ns, design WNS +2.0 / WHS +0.03 (an internal path). |

## 6. Still open

- The board facts in handover §7: `USD_NCD` polarity and wiring on HBI0309C,
  external pull-ups, the real SCK ceiling. That is why `CD_POL` and `CD_IGNORE`
  are runtime bits and DIV=1 is only tried at B2.
- Full static implementation timing on the USD pads comes with the mint.
