# nanosoc_m0_soc — FPGA Build-Flow Report (source: PYNQ-Z2 flow to mirror for MPS3)

> **Status: HISTORICAL** — a record of the `nanosoc_m0_soc` PYNQ-Z2 FPGA build flow, surveyed as a model for MPS3 as of 2026-08-07.
> Superseded by / current state in [docs/STATUS.md](../STATUS.md).
> Kept for provenance; do not update.

Investigated repo: `~/SoCLabs/nanosoc_m0_soc` (read-only; single-core
CM0 nanoSoC — NOT the multicore repo). Snapshot date 2026-07-04. All findings
below are read from the working tree as it stands (which per the repo's own
`doc/reports/productisation_status_2026-07-03.md` has **108 files of
uncommitted changes** — the PYNQ-Z2 flow described here is real and was built
through bitstream+XSA on 2026-07-03, but is not yet a git commit).

---

## 1. Existing FPGA target — PYNQ-Z2 (the flow to mirror for MPS3)

**Part:** `xc7z020clg400-1` (TUL PYNQ-Z2, Zynq-7000). **Not** the MPS3/KU115 —
this is the only board target that currently exists in the repo
(`pynq/targets/` has exactly one subdirectory, `pynq-z2/`). It is the proven
pattern the report is asked to mirror for MPS3, not a build that already runs
on MPS3.

**Build entry point:** `make -C pynq all` (from repo root), which chains:

```
soc_model → fw_config → firmware → package_ip → build_design
```

Layout (`pynq/`):
```
pynq/
├── Makefile                    # orchestration (see §1 targets below)
├── filelist.tcl                 # full RTL file list for IP packaging
├── build_nanosoc_design.tcl     # Vivado driver: project → BD → synth → impl → bitstream/xsa
├── fpgahub.toml                 # fpgahub manifest (build_hello/deploy/uart_tail/ci_hello actions)
├── firmware/CMakeLists.txt      # CMake superproject: arch_tech fw + stage0/stage1 bootloaders
├── harness/                     # vendored from ahb_qspi/fpga/harness (via multicore pynq/)
│   ├── fpga.mk                  # program/deploy/stress/ci_full/resolve_board targets
│   └── scripts/                 # with_lease.sh, deploy_overlay.sh, bit2bin.py, ...
├── vivado_ip/
│   ├── nanosoc_vivado_wrapper.v # IP wrapper: SoC core + straps + tristate splits
│   └── package_nanosoc_ip.tcl   # ipx:: packaging script
├── targets/pynq-z2/
│   ├── nanosoc_design.tcl       # BD: PS7 + clk_wiz(100→25MHz) + proc_sys_reset + SoC IP
│   ├── nanosoc_design_wrapper.v # board wrapper (DDR/FIXED_IO, IOBUFs, LEDs)
│   ├── nanosoc.xdc / nanosoc_timing.xdc  # pin + timing constraints
│   └── scripts/                 # enable_uart1_emio.dts, nanosoc-uart-bridge.{sh,service}
└── scripts/                     # slcr_fclk_reset.py, hex_byte_to_word.py, openocd/nanosoc.cfg
```

**Makefile targets** (`pynq/Makefile`):

| Target | Action |
|---|---|
| `soc_model` | delegates to root `Makefile`: regenerate `build_soc/` from YAML |
| `fw_config` | copy `build_soc/firmware/*` → `imp/fpga/fw_config/`, patch `NANOSOC_SYS_CLK_FREQ_HZ` to `FPGA_CLK_FREQ_HZ` (default 25 MHz) — see §4 |
| `firmware [APP=hello]` | CMake superproject build: stage0 bootrom (+ generated `nanosoc_region_bootrom.v`/`bootrom.sv` via `bootrom_gen.py`) + the chosen testcode `.hex`/`.bin` |
| `package_ip` | Vivado batch: package `nanosoc_vivado_wrapper` as an XCI IP into `imp/fpga/nanosoc_ip/` |
| `build_design` | Vivado batch: create project, source BD tcl, wire `IMEM_MEM_FPGA_IMG` firmware hex into the SoC IP, add board wrapper + XDC, synth, impl, write bitstream + `.xsa` |
| `synth_only` | stop after `synth_1` (CI smoke, `FPGA_SYNTH_ONLY=1`) |
| `program` / `program_jtag_local` | (from vendored `harness/fpga.mk`) fpgahub-leased / local JTAG program |
| `deploy` | (harness) fpgahub-leased scp + `fpga_manager` hot-load |
| `deploy-auto` | sshpass-automated scp of `.bit`/`.bin`/DT overlay + PS-side load (`fpgautil`/`xdevcfg`/`fpga_manager` fallback chain) + SLCR `FCLK_RESET0` pulse |
| `uart` | stream `/dev/ttyPS1` (38400 baud) over SSH |
| `clean` | `rm -rf imp/fpga` |

Outputs land in `imp/fpga/output/pynq-z2/`: `nanosoc_design_wrapper.bit` (4.0 MB),
`nanosoc_design_wrapper.bin`, `nanosoc_design.xsa`. `imp/` is entirely
git-ignored (only build outputs live there; nothing there is a source of truth).

**IP packaging / board wrapper:**
1. `filelist.tcl` reads all RTL (generated `build_soc/rtl/*`, arch_tech
   subsystems/regions, Arm IP, generated bootrom) into the Vivado fileset with
   `verilog_define {RAM_PRELOAD}` set (selects the `$readmemh`-preloadable ROM
   variant of IMEM instead of empty SRAM).
2. `vivado_ip/package_nanosoc_ip.tcl` packages `nanosoc_vivado_wrapper.v` (top
   of the fileset) as a Vivado XCI IP via `ipx::package_project`, defining
   explicit bus interfaces for `sys_fclk` (clock, 25 MHz), `nrst`
   (active-low reset) and `uart` (RxD/TxD); everything else (SWD, SPI, GPIO,
   status) is left as plain ports. It also re-adds the FPGA-only ROM model
   files (`sl_ahb_rom.v`, `sl_fpga_rom_word.v`) that `ipx::merge_project_changes`
   would otherwise strip, and re-propagates the `RAM_PRELOAD` define onto the
   IP's synthesis/simulation file groups.
3. `pynq/vivado_ip/nanosoc_vivado_wrapper.v` wraps the **`nanosoc` core**
   (`build_soc/rtl/nanosoc.sv`) — deliberately *not* `nanosoc_system` — adding
   FPGA-only glue: FT1248 self-drain ties (so the stage-0 UART/ADP write
   doesn't hang boot with no FTDI attached), the `FT1248MODE` strap, the
   UART2↔pin-mux (`P1[5]`/`P1[4]`) wiring, SWD/GPIO tristate splits, plain SPI
   master pins, and expansion-region tie-off (see §2 known-issue notes below).
4. `pynq/targets/pynq-z2/nanosoc_design.tcl` builds the actual Vivado block
   design: **PS7** (FCLK_CLK0=100 MHz, UART0 on MIO→CP2104 USB console,
   UART1 on EMIO→SoC firmware console) → **clk_wiz** (100→25 MHz `sys_fclk`)
   → **proc_sys_reset** (BTN0 as active-high aux reset) → the packaged SoC IP.
   `nanosoc_design_wrapper.v` is the top-level board wrapper adding DDR/FIXED_IO
   ports, bidirectional IOBUFs for 16 GPIO + SWD, and `nanosoc_debug_leds`.
   Constraints: `nanosoc.xdc` (pin/IOSTANDARD) + `nanosoc_timing.xdc`
   (false-paths for async SWD/GPIO/buttons/LEDs; no ethernet PHY so no
   async clock-group constraints, unlike the multicore build).

**Board-specific dependency:** this flow leans on the Zynq **PS7** hard block
for clocking (`clk_wiz` fed from `FCLK_CLK0`), reset sequencing
(`proc_sys_reset`), and the UART bridge itself (firmware UART is bridged
through **PS UART1 over EMIO** to `/dev/ttyPS1` — there is no dedicated FPGA
pin for the firmware console). None of this exists on MPS3/KU115 (no hard PS);
mirroring this flow for MPS3 will need a from-scratch clock/reset MMCM/PLL,
a real FPGA-pinned UART (e.g. to an on-board FTDI/USB-UART or PMOD), and likely
a different debug-probe wiring since there's no PS-side JTAG/`fpgautil` deploy
path. The **IP-packaging step (§1.2/1.3) and the generated-RTL filelist are
almost entirely board-agnostic** and should carry over close to verbatim; the
BD/wrapper/XDC/deploy layer (steps 1.4 + harness) is the part that must be
rewritten per-board.

Full known-issues/deviations catalogue (11 items — generator binder defects,
expansion-region tie-off, UART TX-only, no on-board flash in the basic build,
etc.) is in `doc/reports/pynq_z2_build.md` §8 and is worth reading before
attempting the MPS3 port, since several of the workarounds (project-yaml wire
fixes, `nanosoc_ss_debug.v`/`nanosoc_ss_systemctrl.v` local overrides) are
generic (not PYNQ-Z2-specific) and will be needed on MPS3 too.

---

## 2. Generator vs pre-generated RTL

**`build_soc/rtl/` is committed, ready-to-synth RTL** (tracked in git, not
gitignored — only `imp/` is in `.gitignore`), but it is a **generated
artifact**, not hand-written, and the repo's own README says explicitly:
"All files under `build_soc/` are generated from the YAML system description
by `nanosoc_gen`. Do not edit these directly — regenerate from YAML."

At the moment of inspection `git status` shows every file under `build_soc/`
as **modified** (uncommitted) relative to the last commit — i.e. someone ran
the generator locally after the last commit and the regenerated output now
differs from what's checked in. This confirms both halves of the story: (a)
a `build_soc/rtl/` tree *is* present and would synthesize as-is without
running anything, but (b) it is fully reproducible by rerunning the
generator, and the generator is an active, still-changing part of the flow.

**Regeneration path:** the root `Makefile` (`~/SoCLabs/nanosoc_m0_soc/Makefile`):

```make
GEN_DIR     = nanosoc_arch_tech/nanosoc_gen
SYS_DESC    = sys_desc
TOP_YAML    = $(SYS_DESC)/nanosoc_m0_soc.yaml
SYSTEM_YAML = $(SYS_DESC)/nanosoc_m0_system.yaml
LIB_DIR     = nanosoc_arch_tech/sys_desc
BUILD_DIR   = build_soc
LIB_ARGS    = --lib-dir $(LIB_DIR) --lib-dir $(SYS_DESC)
SOC_MODEL   = PYTHONPATH=$(GEN_DIR) python3.10 -m soc_model
GEN_ARGS    = $(TOP_YAML) $(LIB_ARGS) --build-dir $(BUILD_DIR) --system-yaml $(SYSTEM_YAML)
```

`make targets`:

| Target | Effect |
|---|---|
| `all` (default) | == `soc_model` |
| `soc_model` | `$(SOC_MODEL) $(GEN_ARGS)` — parse, validate, generate everything into `build_soc/` |
| `lint` | same + `--lint` (slang) |
| `validate` | `--validate-only`, no output written |
| `list-backends` | list registered generator backends in run order |
| `clean` | `rm -rf build_soc` |

Optional opt-in flags via env vars: `EMIT_CONSTRAINTS=1` → `--emit-constraints`
(SDC/XDC), `EMIT_TESTBENCH=1` → `--emit-testbench` (cocotb env),
`EMIT_VIVADO_BD=1` → `--emit-vivado-bd` (Vivado IP-XACT/BD packaging TCL —
per the productisation report this backend only covers the bare no-PS case,
explicitly out of scope for PS7/clk_wiz glue, so the PYNQ flow hand-writes
its own BD instead; this bare no-PS backend is actually more relevant to
MPS3 than to PYNQ since MPS3 has no PS7).

**Minimal command to get a synthesizable RTL tree** (from repo root):

```sh
python3.10 -m venv <TBD - see §5>            # nanosoc_gen deps (jinja2, pyyaml,
                                              # systemrdl-compiler>=1.29,
                                              # peakrdl-regblock>=1.2) must be
                                              # importable by python3.10
make soc_model
```

This regenerates `build_soc/rtl/`, `build_soc/firmware/`, `build_soc/rdl/`,
`build_soc/flist/`, `build_soc/reports/`, etc. from `sys_desc/nanosoc_m0_soc.yaml`
+ `sys_desc/nanosoc_m0_system.yaml`, using `nanosoc_arch_tech/sys_desc` as the
first (default) library and the project-local `sys_desc/` as a second,
higher-priority library so project overrides (e.g.
`sys_desc/subsystems/cpu/nanosoc_ss_cpu.yaml`, which keeps per-core SWD
instead of the upstream system-DAP `dbg_ahb` interface) shadow the arch_tech
originals. Note the project's `sys_desc/` is a **real directory** (not the
symlink the top-level README describes) containing both the top YAMLs and
project-local subsystem overrides — the README is stale on this point.

`build_soc/rtl/` generated top-level contents (present now):
`nanosoc.sv` (core top), `nanosoc_system.sv` (system wrapper — not used by the
PYNQ IP wrapper, see §1), `nanosoc_ss_cpu.sv`, `nanosoc_soc_config_pkg.sv` /
`nanosoc_soc_config.vh`, plus generated subdirectories
`nanosoc_ahb_interconnect/`, `nanosoc_ahb_interconnect_discovery/`,
`nanosoc_cpu_ss_ahb_interconnect/`, `nanosoc_cpu_ss_ahb_interconnect_discovery/`,
`nanosoc_build_info/`. Chip-level wrappers (`nanosoc_chip.v`,
`nanosoc_chip_pads.v`) also live here for the ASIC pad-ring flow (not used by
the FPGA flow).

The `ahb_interconnect_tool` at repo root is a symlink to the sibling repo
`~/SoCLabs/ahb_interconnect_tool` (Perl `BuildBusMatrix`-style bus
matrix generator the AHB-interconnect backend shells out to during
`soc_model`).

---

## 3. IP dependencies (synthesis-time)

All external IP is Arm Academic Access (AAA) licensed RTL, resolved read-only
through **`ARM_IP_LIBRARY_PATH`** (defaulted by `pynq/Makefile` to
`$ARM_IP_LIBRARY_PATH`, from tools.env or the command line). No separate
`CMSDK_DIR` variable is used anywhere in this repo (that name only appears in
`nanosoc_gen`'s own `USER_GUIDE.md` prose, not as a live env var the flists
consume) — everything routes through `ARM_IP_LIBRARY_PATH`.

Confirmed present/readable at that path (read-only checked, not modified):
`Cortex-M0/`, `Corstone-101/`, `PL022/PL022-BU-00000-r1p4-00rel0/`.

From `pynq/filelist.tcl`, the exact IP consumed:

- **Cortex-M0 core + DAP + integration** (per-core SWD, `EXTERNAL_DAP=0`):
  `${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/{cortexm0,cortexm0_dap,cortexm0_integration}/verilog/*.v`
  + `models/cells/*.v`, `models/wrappers/*.v`.
- **Corstone-101 CMSDK peripherals** (AHB/APB muxes, FPGA-friendly memories,
  timers, dual-timer, UART, watchdog, GPIO, clock gate):
  `${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/...` (18 files enumerated
  explicitly, incl. `cmsdk_fpga_sram.v`/`cmsdk_fpga_rom.v` FPGA memory models).
- **Arm PL022 SSP (SPI master)** — 20 source files under
  `${ARM_IP_LIBRARY_PATH}/PL022/PL022-BU-00000-r1p4-00rel0/ssp_pl022/verilog/rtl_source/`
  (`Ssp.v`, `SspApbif.v`, ... `SspTxRegFile.v`). This replaced an earlier QSPI
  flash controller in the "basic implementation" per direction on
  2026-07-03 — QSPI is now opt-in-only collateral.
- **Include dirs** set on the fileset also point into
  `Cortex-M0/logical/{cortexm0_dap,ualdis}/verilog`,
  `Corstone-101/logical/{cmsdk_apb_dualtimers,cmsdk_apb_watchdog}/verilog`,
  `Corstone-101/logical/models/memories`, and the PL022 rtl_source dir.

No Xilinx-vendor IP is hand-instantiated in the SoC RTL itself — the only
Xilinx IP is at the board-wrapper/BD level (PS7, `clk_wiz`, `proc_sys_reset`,
IOBUF primitives), which is expected to be replaced for MPS3/KU115 with
whatever clock/reset primitives that board needs (no PS7 equivalent on
KU115 — would need `MMCME` /`PLLE` clocking wizard IP + a manual reset
synchroniser instead of `proc_sys_reset`).

Everything else needed for synthesis is either generated (`build_soc/rtl/`)
or lives directly in this repo/its `nanosoc_arch_tech` submodule (not
external IP): `nanosoc_arch_tech/rtl/slcorem0_tech/` (SoC Labs' CM0 tech
wrapper: `slcorem0.v`, `_prmu.v`, `_stclkctrl.v`, `_rstctrl.v`,
`_integration.v`), `nanosoc_arch_tech/rtl/socdebug_tech/controller/` (ADP/
FT1248/USRT controllers), `nanosoc_arch_tech/rtl/hostio4/soc_rtl/`, the
arch_tech subsystems/regions RTL, and the FPGA-only helper RTL vendored into
this repo directly at `src/rtl/fpga_lib/{sram,rom,exp,debug}/` +
`src/rtl/local_overrides/{nanosoc_ss_debug.v,nanosoc_ss_systemctrl.v}` (two
generator-gap workarounds, see `doc/reports/pynq_z2_build.md` §8 items 3–4).

**Note per the read-only-filesystem rule:** I did not modify anything under
`$ARM_IP_LIBRARY_PATH` — only listed directory contents to confirm the
paths the flists reference actually resolve.

---

## 4. Firmware — the "UART prints" half

**Hello-world source:** `nanosoc_arch_tech/firmware/testcodes/hello/hello.c`
— a trivial CMSDK testcode:

```c
int main (void) {
  UartStdOutInit();
  printf("Hello world\n");
  printf("** TEST PASSED **\n");
  UartEndSimulation();
  return 0;
}
```

**Toolchain:** `arm-none-eabi-gcc` (doc reports pin it at 10.3, but the CMake
toolchain file just expects it on `PATH`; pin a specific prefix via
`-DNANOSOC_GCC_PREFIX=...`). Toolchain file:
`nanosoc_arch_tech/firmware/cmake/toolchains/arm-gcc.cmake`
(`CMAKE_SYSTEM_NAME Generic`, bare-metal, `-g` only by default — per-target
`-O` comes from `nanosoc_add_test(... OPT_LEVEL ...)`).

**Build command** (this is what `pynq/Makefile`'s `firmware` target actually
runs):

```sh
cmake -S pynq/firmware -B <build-dir> \
    -DCMAKE_TOOLCHAIN_FILE=nanosoc_arch_tech/firmware/cmake/toolchains/arm-gcc.cmake \
    -DNanoSoC_FIRMWARE_CONFIG_DIR=<FPGA-clock-patched build_soc/firmware copy> \
    -DNANOSOC_BUILD_TESTS=ON
cmake --build <build-dir> --target nanosoc_bootrom hello -j 4
```

`pynq/firmware/CMakeLists.txt` is a thin superproject that
`include()`s `${NanoSoC_FIRMWARE_CONFIG_DIR}/nanosoc_memmap.cmake` (fails
loudly if missing — "run `make soc_model` first") then
`add_subdirectory()`s three trees:
`nanosoc_arch_tech/firmware` (`fw` — all arch_tech testcodes incl. `hello`),
`firmware/bootloader/stage0` (`stage0` — SoC-level stage-0 BOOTROM source,
generates `nanosoc_region_bootrom.v`/`bootrom.sv` via `bootrom_gen.py`), and
`firmware/bootloader/stage1` (`stage1` — QSPI-mode stage-1, unused in the
basic PL022 build).

Outputs: `<build>/fw/testcodes/hello/hello.hex` + `.bin`.
`pynq/scripts/hex_byte_to_word.py` converts the byte-oriented `objcopy` hex
into a word-oriented hex Vivado's `$readmemh` expects
(`imp/fpga/hello_word.hex`).

**Clock-frequency gotcha (why `fw_config` exists):** the generated
`build_soc/firmware/nanosoc_memmap.h` hardcodes
`#define NANOSOC_SYS_CLK_FREQ_HZ (100000000UL)` (the ASIC value) with **no
`#ifndef` guard**, and `uart_stdout.c` includes that header after any `-D`
override, so a compile-definition override is silently ineffective. The BD's
`clk_wiz` divides the FPGA clock to 25 MHz, so UART baud-divisor math would be
wrong unless corrected. `make -C pynq fw_config` therefore makes a **patched
copy** of `build_soc/firmware` (never edits `build_soc/` itself) at
`imp/fpga/fw_config/`, `sed`-rewriting that one `#define` to
`FPGA_CLK_FREQ_HZ` (default `25000000`), and firmware is built against that
copy. This is a real landmine to expect on MPS3 too, at whatever clock
frequency KU115 ends up closing at.

**Getting into the SoC (this is what makes "UART prints" true):**
1-stage boot, the only boot path exercised in the basic PYNQ-Z2 build (no
flash present):
```
CPU reset → stage-0 BOOTROM (2 KB, baked into the IP at packaging time,
generated from firmware/bootloader/stage0/stage0_bootloader.c)
  → init UART, print banner (drains harmlessly via FT1248 self-loop ties
    in nanosoc_vivado_wrapper.v — see §1)
  → stage-0's legacy QSPI probe reads a dead APB slot (no QSPI controller in
    this basic build) → falls through to 1-stage path
  → SYSCON->REMAP = 1 (IMEM now aliases 0x00000000)
  → jump to 0x0 (now IMEM)
    → hello, PRELOADED INTO IMEM AT SYNTHESIS TIME via $readmemh in
      sl_ahb_rom (src/rtl/fpga_lib/rom/sl_ahb_rom.v), driven by the
      RAM_PRELOAD verilog define + the BD cell property
      CONFIG.IMEM_MEM_FPGA_IMG=<hello_word.hex path> set in
      build_nanosoc_design.tcl STEP 3.5
        → prints "Hello world" / "** TEST PASSED **" on CMSDK UART2 → pin
          mux P1[5] → PS UART1 EMIO → /dev/ttyPS1 @ 38400 baud
```
So firmware is **not** loaded post-configuration by JTAG/ADP/QSPI in this
flow — it is baked directly into the bitstream as BRAM initial contents
(`IMEM_MEM_FPGA_IMG` → `$readmemh`), selected at build time via
`APP=<testcode>` (default `hello`). Swapping images means re-running
`build_design APP=<other-testcode>` (which re-triggers `firmware` + repackages
the hex into the BD cell config) — it does not require a full resynthesis of
the SoC logic itself, only IP-Integrator BD regeneration + impl, since only a
BRAM INIT string changes. There is currently **no QSPI/flash-boot path
exercised** on this board target (2-stage boot is described in the root
`README.md` as the general SoC capability, but productisation status item 8
notes it's backed out of the "basic implementation" — opt-in only, see
`doc/reports/spi_integration.md` appendix A).

**UART is TX-only in the current generated RTL on the FPGA** (see
`pynq/README.md` Notes + `pynq_z2_build.md` §8 item 6): the pin mux's
`uart2_rxd` is derived from internal feedback rather than the actual pad, so
console *input* doesn't work yet, only output — sufficient for the "boots,
UART prints" milestone but worth knowing before planning any interactive MPS3
bring-up test.

---

## 5. Env setup

There is **no dedicated `set_env.sh`** to source in this repo (unlike the
sibling VCS-based repos in this lab, e.g. cocotb regressions needing
`set_env.sh`). Instead, `pynq/Makefile` itself **exports** the handful of env
vars the TCL filelist needs, with sensible defaults computed from the
Makefile's own location, so a bare `make -C pynq all` works without any prior
sourcing step:

```make
export SOCLABS_NANOSOC_SOC_DIR       ?= <repo root, via $(MAKEFILE_LIST)>
export SOCLABS_NANOSOC_ARCH_TECH_DIR ?= $(SOCLABS_NANOSOC_SOC_DIR)/nanosoc_arch_tech
export SOCLABS_NANOSOC_GEN_DIR       ?= $(SOCLABS_NANOSOC_ARCH_TECH_DIR)/nanosoc_gen
export ARM_IP_LIBRARY_PATH           ?=
```

All four are overridable on the `make` command line
(`make -C pynq all ARM_IP_LIBRARY_PATH=/some/other/mirror`).

Root `Makefile`'s generator invocation additionally requires `python3.10` on
`PATH` (hardcoded default `PYTHON ?= python3.10`) with the `nanosoc_gen`
package importable via `PYTHONPATH=nanosoc_arch_tech/nanosoc_gen` — i.e.
whatever Python environment is active needs `jinja2`, `pyyaml`,
`systemrdl-compiler>=1.29`, `peakrdl-regblock>=1.2` installed (from
`nanosoc_arch_tech/nanosoc_gen/pyproject.toml`; no `requirements.txt`, no
committed venv — install method not specified anywhere in this repo, so
whatever Python 3.10 environment is normally used lab-wide for nanosoc_gen
work should be reused). `pytest>=7`/`pytest-cov` are optional-extra
(`[project.optional-dependencies] test`), not needed for the build itself.

Also required on `PATH` for a full PYNQ build (all confirmed from the flow
above, none scripted/enforced by an env file):
- `vivado` (2024.1 used for the 2026-07-03 build per
  `doc/reports/pynq_z2_build.md` §1/§7)
- `arm-none-eabi-gcc` (10.3 used; toolchain file just needs it on `PATH`)
- `cmake` ≥ 3.21 (per `pynq/firmware/CMakeLists.txt`'s
  `cmake_minimum_required`)
- `dtc` (device-tree compiler, for the `overlay`/`deploy-auto` targets only)
- `sshpass` (for `deploy-auto`; the Makefile's `ensure-sshpass` target
  fails loudly with an install hint if missing)

None of this is MPS3-specific yet — it is the generic toolchain baseline any
board target in this repo needs. The MPS3 port will additionally need
whatever Xilinx UltraScale-family Vivado support and part files cover
`xcku115`-series parts (should already be present in a 2024.1 Vivado
install alongside the 7-series support used here).
