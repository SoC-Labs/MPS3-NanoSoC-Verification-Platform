# `fpga/monolithic/` — nanosoc-on-MPS3, whole-FPGA baseline (Phase 0.4)

**Owner:** A1 (this workstream). **Status:** buildable end-to-end from real,
current RTL. The board-top wrapper is filled in, the build scripts read the
real single-core `nanosoc` SoC (not the legacy, RTL-less `nanosoc_chip`
target this repo started from), and the constraints remain the same
Vivado-2024.1-verified `nanosoc_mps3.xdc` from the original port (see §6).
What remains before a real `.bit` exists is firmware (a real build, not run
by this agent — see §4) and a real Vivado synth/impl run (also not run by
this agent — see §7 for exactly what *was* checked here).

## Buildability status (read this first)

| Deliverable | Status |
|---|---|
| `nanosoc_mps3.xdc` (pin + timing constraints) | **Done, verified.** Unchanged since the original port — every one of 272 `PACKAGE_PIN` assignments checked against the real `xcku115-flvb1760-1-c` package database in Vivado 2024.1, 272/272 pass (§6). |
| `nanosoc_mps3_top.sv` (board-top wrapper) | **Done.** Instantiates the real, current, single-core `nanosoc` core (`nanosoc_m0_soc/build_soc/rtl/nanosoc.sv`) directly — the same "wrap `nanosoc`, not `nanosoc_system`/`nanosoc_chip`" pattern already HW-validated on a PYNQ-Z2 build. Clock (OSCCLK[1]→BUFG, 50 MHz, no MMCM), reset (CB_nPOR & CB_nRST → 3-FF sync), console UART (CMSDK UART2 via the P1 GPIO mux → UART_TX_F[2]/UART_RX_F[2], with the mandatory FT1248/ADP self-drain ties), and GPIO (p0 ↔ USER_nLED/USER_SW) are wired; every other board-I/O group the XDC declares is tied off safely. `verilator --lint-only` clean against a port-accurate black-box stub of `nanosoc` (§7). |
| `filelist.tcl` (RTL source list) | **Rewritten.** No longer an IP-repo/VLNV pre-check for a Block Design — this is now a flat list of `read_verilog`/`read_verilog -sv` calls pulling in the real RTL (Arm Cortex-M0 + Corstone-101 CMSDK + PL022 SSP from `ARM_IP_LIBRARY_PATH`, plus all of `nanosoc_m0_soc`'s SoC RTL), mirroring `nanosoc_m0_soc/pynq/filelist.tcl`'s own already-Vivado-proven transcription of the same file set. All 198 referenced files confirmed to exist on disk (§7); requires `NANOSOC_BOOTROM_DIR` (a real firmware/bootrom build, see §4) with no default. |
| `build_monolithic.tcl` (full build driver) | **Rewritten.** Plain flat-RTL Vivado project flow: `create_project` → `filelist.tcl` → add `nanosoc_mps3_top.sv` as top → add the XDC → override `IMEM_MEM_FPGA_IMG` via the project `GENERIC` property → synth → (optional) impl → `write_bitstream`. No Block Design, no packaged IP, no PS7 — MPS3/KU115 has none of those and needs none for this baseline. Dry-run-checked under plain `tclsh` with a Vivado-command stub harness (§7); real `synth_design`/`launch_runs` behaviour has **not** been run under real Vivado by this agent (out of scope, see §7). |
| `synth_check.tcl` (fast smoke) | **Rewritten.** Stage 1 (PACKAGE_PIN sanity) unchanged, still real, still passes (§6). Stage 2 now runs a best-effort out-of-context synth of `nanosoc_mps3_top.sv` against the real RTL instead of the old (permanently blocked) legacy BD recreate; self-skips cleanly if `NANOSOC_BOOTROM_DIR` isn't set. |

**Bottom line:** the earlier blocker in this directory — the legacy
`arm_mps3` target's `nanosoc_chip` module having no RTL anywhere in
`nanosoc-multicore-system`, confirmed absent even from git history — is
resolved by building against a **different, real, current source tree**:
`~/SoCLabs/nanosoc_m0_soc` (the single-core nanoSoC repo; see
`docs/nanosoc_m0_soc/README.md` for how that source was identified and
`docs/nanosoc_m0_soc/PLATFORM_MAPPING.md` §2 for the full signal-by-signal
wiring table this port implements). This directory now has everything
needed for the integrator to run a real Vivado build — see §7 for the exact
command and what is/isn't already checked.

---

## 1. What this baseline is

Per `docs/IMPLEMENTATION_PLAN.md` WP 0.4 and spec §14 step 1, this is the
**Phase-0.4 head start**: a single-core nanosoc occupying the **whole**
XCKU115 device, loaded by the MCC from SD like any other MPS3 application
note — no MicroBlaze, no DFX, no partial reconfiguration. It is **not** the
DFX shell (`fpga/shell/` + `fpga/dfx/` + `fpga/rp/`, owned by other
agents/tracks). Its target acceptance gate (spec §14 step 1):

> nanosoc boots on MPS3, UART prints (via FT4232)

This baseline exists so the DFX shell work (Phase 1+) has a **known-good,
whole-device reference point**, and so D7 ("measure nanosoc first" for RP
Pblock sizing) has a real utilisation number before `fpga/dfx/`'s Pblock is
drawn.

---

## 2. Where the SoC RTL actually comes from, and what changed

### 2.1 The original plan (legacy `arm_mps3` target) was a dead end

The first pass at this baseline ported `nanosoc_mps3.xdc` from the *legacy*
single-core nanoSoC `arm_mps3` FPGA target under
`nanosoc-multicore-system/nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/`
(Vivado 2021.1, Block-Design-based, `soclabs.org:user:nanosoc_chip:1.0` as
the one SoC cell). That target's `nanosoc_chip` module turned out to have
**no packaged IP and no underlying RTL anywhere in that source tree** —
confirmed absent from the current checkout and from `git log --all` on the
`system/`/`flist/project/top_FPGA.flist` sources its own packaging flow
depends on. That finding still stands and is a genuine fact about
`nanosoc-multicore-system`'s history — it just turned out not to be the only
available single-core nanoSoC source in this lab.

### 2.2 The real source: `nanosoc_m0_soc`

`~/SoCLabs/nanosoc_m0_soc` is a **separate, current, buildable**
single-core nanoSoC repo (one Cortex-M0/SLCore-M0, no Ethernet, optional
SPI/QSPI/DMA) with its own already-HW-validated FPGA flow (a PYNQ-Z2 build,
`doc/reports/pynq_z2_build.md`, dated 2026-07-03). Full survey detail lives
in this repo's own `docs/nanosoc_m0_soc/` (`README.md`, `RTL_ANATOMY.md`,
`FPGA_FLOW.md`, `PLATFORM_MAPPING.md`) — the headline facts this port relies
on:

- **Integration module = `nanosoc`** (`nanosoc_m0_soc/build_soc/rtl/nanosoc.sv`,
  ~76 ports incl. 4 SPI pins). Wrap **this** directly, not `nanosoc_system`/
  `nanosoc_chip` — the generated `nanosoc_system` has a confirmed expansion-
  port wiring bug (undriven/mis-directed `exp_h*` nets under real synthesis),
  and the PYNQ-Z2 reference deliberately routes around it the same way.
- **IMPORTANT, found while wiring this port:** `nanosoc_m0_soc` has **two**
  checked-in copies of this module that are **not interchangeable**:
  `nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv` (the arch_tech submodule's
  own separately-generated copy) has a **reversed** `exp_h*` AHB port-
  direction convention and **no `spi_*` ports at all**, relative to
  `build_soc/rtl/nanosoc.sv` (the project-regenerated snapshot the real,
  proven synth flow — and this port's own `filelist.tcl` — actually reads).
  `nanosoc_mps3_top.sv` is written against `build_soc/rtl/nanosoc.sv`;
  `filelist.tcl` reads that same file. Do not "fix" the wrapper by
  cross-checking it against the arch_tech copy — that copy is the stale one
  for this purpose.
- **Console = CMSDK UART2 @ 0x4000_6000, raw serial**, muxed onto GPIO port
  P1 (`p1_out[5]`=TXD, `p1_in[4]`=RXD), **not** AXI-Stream — nanosoc has no
  dedicated UART pins. TX is HW-proven (PYNQ-Z2); RX is wired but does not
  yet reach the CPU in the currently generated RTL (internal pin-mux
  feedback, not the physical pad — an upstream limitation, not something
  this port introduces).
- **No Ethernet MAC.** Confirmed absent from `nanosoc`'s full port list and
  sub-hierarchy — the MPS3's LAN9220 pins (§3) stay tied off in this
  baseline exactly as they were in the original port.
- **FPGA RTL selection**: `nanosoc_arch_tech/rtl/flist/nanosoc_FPGA.flist` is
  the correct upstream flist (full detail + why the others aren't:
  `docs/nanosoc_m0_soc/RTL_ANATOMY.md` §5). This repo's `filelist.tcl` is a
  hand-transcription of that exact file set into Vivado `read_verilog` calls
  (Vivado's own `-f` reader does not understand that flist's VCS-style
  `$(VAR)`/`+libext+` syntax) — see that script's own header for the full
  rationale and the proven precedent (`nanosoc_m0_soc/pynq/filelist.tcl`) it
  mirrors.

### 2.3 Wiring summary (full detail in `nanosoc_mps3_top.sv`'s own header)

| Function | MPS3 board pin(s) | `nanosoc` port(s) | Notes |
|---|---|---|---|
| Clock | `OSCCLK[1]` (50 MHz, xdc `create_clock -period 20.000`) | `sys_clk` | Through a `BUFG`. **No MMCM needed** — 50 MHz is a legal operating point and `nanosoc`'s RTL has no clocking-critical dependency on a specific `sys_clk` frequency (see the wrapper header for a note on the `SYS_CLK_FREQ_HZ` parameter, which is discovery/build-info metadata, not a clock generator control). |
| Reset | `CB_nRST` & `CB_nPOR` | `sys_sysresetn` | 3-FF synchroniser (assert async, release sync) in the `sys_clk` domain — nanosoc has exactly one reset input. |
| Console UART (TX proven, RX wired but not yet CPU-reachable upstream) | `UART_TX_F[2]` / `UART_RX_F[2]` | `p1_out[5]`/`p1_in[4]` (CMSDK UART2 via P1 GPIO mux) | Includes the mandatory FT1248/ADP self-drain ties on `p1[3:0]`/`p1[7:6]` — omitting these reproduces a real stage-0 BOOTROM boot hang, not a cosmetic gap. |
| GPIO | `USER_nLED[7:0]` / `USER_SW[7:0]` | `p0_out[7:0]` / `p0_in[7:0]` | LEDs are active-low (inverted in the wrapper). |
| SWD | *(tied off)* | `cpu_0_swdi=0`, `cpu_0_swclk=0` | Optional for the "boots, UART prints" gate (spec §14 step 1) — not brought up in this baseline. |
| Everything else (LAN9220/SMC, USB debug FIFO, HDMI/MMB, Audio, eMMC, CLCD, CoreSight JTAG/trace, QSPI, USD, SCC, MCC SMB, shield header) | — | — | Tied off safely (outputs to a defined idle level, inouts to high-Z) — declared only because Vivado requires every top-level port on this board-wrapper-style target to have a legal site before `write_bitstream`. |
| Expansion region / SPI | — | `exp_*`, `spi_*` | `exp_*` tied off benign exactly per the HW-validated PYNQ reference pattern; `spi_*` left unconnected (no SPI pin group exists anywhere in `nanosoc_mps3.xdc` — future work, not this baseline's scope). |

---

## 3. LAN9220 SMC pin inventory

The MPS3's Ethernet is a **memory-mapped SMSC LAN9220** (10/100, integrated
MAC+PHY) on the FPGA's static-memory-controller bus — not RMII/MII to
fabric (`docs/ARCHITECTURE_SPEC.md` §3). This baseline's wrapper ties these
pins off (no LAN9220 driver logic in the monolithic bitstream — that comes
later, in `fpga/shell/`'s LAN9220 host I/F, spec §4.1/§12; also consistent
with `nanosoc` itself having no Ethernet MAC at all, §2.2); they are
constrained only because Vivado requires every top-level port on this
board-wrapper-style target to have a legal site before `write_bitstream`.

| Signal | Width | KU115 pins | IOSTANDARD | Function |
|---|---|---|---|---|
| `ETH_nCS` | 1 | AL24 | LVCMOS18 | LAN9220 chip-select |
| `ETH_nOE` | 1 | AJ23 | LVCMOS18 | LAN9220 output-enable |
| `ETH_INT` | 1 | AK23 | LVCMOS18 | LAN9220 interrupt (input) |
| `SMBF_ADDR[6:0]` | 7 | AH19/AJ18/AJ19/AH21/AJ21/AK20/AK21 | LVCMOS18 | Static-memory address bus (shared: LAN9220 + USB debug FIFO) |
| `SMBF_DATA[15:0]` | 16 | AK22/AL19-22/AH18/AM19-22/AN19-22/AP19-21/AR22 | LVCMOS18 | Static-memory data bus |
| `SMBF_FIFOSEL` | 1 | AJ20 | LVCMOS18 | FIFO-select (mux between LAN9220 and USB debug FIFO on the same bus) |
| `SMBF_nOE` / `SMBF_nWE` | 1/1 | AN23 / AP23 | LVCMOS18 | Bus output-enable / write-enable |
| `SMBF_nRST` | 1 | AL23 | LVCMOS18 | Static-memory-bus-side reset |

Full pin-to-ball table with every property (all Vivado-verified, §6):
`nanosoc_mps3.xdc`, sections "LAN9220 Ethernet SMC interface" + "SMBF_\*
static memory bus". Provenance: legacy `fpga_pinmap.xdc` lines ~37-39 and
~319-321 (`ETH_*`), ~10-39 and ~639-665 (`SMBF_*`).

---

## 4. Firmware — building the "UART prints" half (NOT run by this agent)

Nothing in this directory runs `cmake`/`gcc`/the nanoSoC generator. The
integrator needs to build firmware **before** a real `build_monolithic.tcl`
run, following `docs/nanosoc_m0_soc/FPGA_FLOW.md` §4 (the PYNQ-Z2 flow this
mirrors), adapted for MPS3's clock:

```bash
cd ~/SoCLabs/nanosoc_m0_soc   # read-only source repo — never write here

# 0. (Only if build_soc/ is stale or missing) regenerate RTL from YAML.
#    Needs python3.10 + nanosoc_gen deps (jinja2, pyyaml,
#    systemrdl-compiler>=1.29, peakrdl-regblock>=1.2) importable.
make soc_model

# 1. FPGA-clock-patched copy of build_soc/firmware. MPS3's OSCCLK[1] is
#    50 MHz (nanosoc_mps3.xdc `create_clock`), NOT the PYNQ-Z2 25 MHz
#    clk_wiz value — the generated nanosoc_memmap.h's NANOSOC_SYS_CLK_FREQ_HZ
#    #define is unguarded (a -D override is silently ineffective), so this
#    step makes a patched COPY, never editing build_soc/ itself.
FW_CONFIG_DIR=~/SoCLabs/mps3-nanosoc-platform/fpga/monolithic/build/fw_config_mps3
mkdir -p "$FW_CONFIG_DIR"
cp -f build_soc/firmware/* "$FW_CONFIG_DIR/"
sed -i 's/#define NANOSOC_SYS_CLK_FREQ_HZ.*/#define NANOSOC_SYS_CLK_FREQ_HZ                  (50000000UL)  \/* FPGA override: MPS3 OSCCLK[1] *\//' \
    "$FW_CONFIG_DIR/nanosoc_memmap.h"

# 2. CMake firmware build (stage-0 bootrom + the "hello" testcode), building
#    OUTSIDE the read-only nanosoc_m0_soc tree. pynq/firmware/CMakeLists.txt
#    is a thin, board-agnostic superproject (no PYNQ-specific paths) — reusing
#    it for MPS3 is legitimate, confirmed by FPGA_FLOW.md: "None of this is
#    MPS3-specific yet."
FW_BUILD_DIR=~/SoCLabs/mps3-nanosoc-platform/fpga/monolithic/build/firmware_mps3
cmake -S pynq/firmware -B "$FW_BUILD_DIR" \
    -DCMAKE_TOOLCHAIN_FILE=nanosoc_arch_tech/firmware/cmake/toolchains/arm-gcc.cmake \
    -DNanoSoC_FIRMWARE_CONFIG_DIR="$FW_CONFIG_DIR" \
    -DNANOSOC_BUILD_TESTS=ON
cmake --build "$FW_BUILD_DIR" --target nanosoc_bootrom hello -j 4

# 3. Byte-hex -> word-hex. Vivado's $readmemh (via sl_ahb_rom/sl_fpga_rom_word)
#    expects a WORD-oriented hex file; objcopy's raw output is byte-oriented
#    and will NOT work directly — this conversion step is mandatory, not
#    optional.
python3 pynq/scripts/hex_byte_to_word.py \
    "$FW_BUILD_DIR/fw/testcodes/hello/hello.hex" \
    ~/SoCLabs/mps3-nanosoc-platform/fpga/monolithic/build/hello_word.hex

# 4. Point the Vivado build at the generated bootrom + word-hex.
export NANOSOC_BOOTROM_DIR="$FW_BUILD_DIR/stage0"
export IMEM_MEM_FPGA_IMG=~/SoCLabs/mps3-nanosoc-platform/fpga/monolithic/build/hello_word.hex
```

`nanosoc_region_bootrom.v`/`bootrom.sv` (from `NANOSOC_BOOTROM_DIR`) are read
directly into the RTL fileset by `filelist.tcl` — **required** for every
build (there is no default; reusing the PYNQ-Z2 build's bootrom would bake
in the wrong UART baud divisor, since it was compiled against a 25 MHz
firmware-clock constant, not MPS3's 50 MHz). `IMEM_MEM_FPGA_IMG` is preloaded
into IMEM as `$readmemh` BRAM-initial-content at synthesis time (via the
project `GENERIC` property `nanosoc_mps3_top`'s own parameter) — swapping
firmware images means re-running steps 2-4 above plus `synth_1`/`impl_1`,
not a full RTL re-synthesis from scratch (only the IMEM's init string
changes).

---

## 5. Env vars / how this is consumed

```bash
export NANOSOC_M0_SOC_SRC=~/SoCLabs/nanosoc_m0_soc   # default; read-only source
export ARM_IP_LIBRARY_PATH=/path/to/ip_library                  # REQUIRED; read-only Arm IP

# REQUIRED, no default — see §4:
export NANOSOC_BOOTROM_DIR=/path/to/mps3-clock-patched/firmware/stage0
export IMEM_MEM_FPGA_IMG=/path/to/hello_word.hex

# Vivado 2024.1 (license already resolved in this lab's environment)
source /apps/Xilinx/Vivado/2024.1/settings64.sh
```

`filelist.tcl` and `build_monolithic.tcl` read these (with the defaults
shown) via `$env(...)`, so `vivado -mode batch -source build_monolithic.tcl`
works for anyone with the same lab layout once firmware (§4) is built. See
each script's header for the full variable list, including `FPGA_PART`,
`FPGA_PROJECT_DIR`, `FPGA_OUTPUT_DIR`, `FPGA_NUM_JOBS`, and `FPGA_SYNTH_ONLY`
(stop after `synth_1`; also auto-generates a placeholder `IMEM_MEM_FPGA_IMG`
if one isn't supplied, for a CI smoke build with no real firmware).

---

## 6. Files in this directory

```
fpga/monolithic/
├── README.md              (this file)
├── nanosoc_mps3.xdc        Ported pin + timing constraints — Vivado-2024.1-verified (§7).
│                           Unchanged from the original port.
├── nanosoc_mps3_top.sv     Board-top wrapper: instantiates the real `nanosoc` core, wires
│                           clock/reset/UART/GPIO per §2.3, ties off everything else.
├── filelist.tcl            Flat RTL source list (Arm IP + all nanosoc_m0_soc SoC RTL +
│                           stage-0 bootrom) -- read_verilog calls, no BD/IP-repo machinery.
├── build_monolithic.tcl    create_project -> filelist.tcl -> add board top + XDC ->
│                           GENERIC override -> synth -> (optional) impl -> write_bitstream
└── synth_check.tcl         Fast 2-stage smoke: Stage 1 PACKAGE_PIN-vs-part sanity (always
                            runs, no other input needed) + Stage 2 best-effort real-RTL OOC
                            synth of nanosoc_mps3_top (self-skips with an exact repro if
                            NANOSOC_BOOTROM_DIR isn't set)
```

---

## 7. What was actually verified in this pass, and what remains

**Carried over unchanged from the original port (still valid, not re-run
this pass):** the Stage 1 Vivado smoke (`nanosoc_mps3.xdc` PACKAGE_PIN
sanity) — **272/272 `PACKAGE_PIN` tokens checked and valid** against the
real `xcku115-flvb1760-1-c` package database, Vivado 2024.1. Repro:

```bash
cd fpga/monolithic
export SYNTH_CHECK_DIR=/tmp/synth_check_smoke   # or any scratch dir
vivado -mode batch -source synth_check.tcl -log smoke.log -journal smoke.jou
```

**Newly checked in this pass (no Vivado license used — see constraints
below):**

1. `nanosoc_mps3_top.sv` lints clean under `verilator --lint-only -Wall -sv`
   against a black-box stub of `nanosoc` whose port list/directions were
   extracted **verbatim** from the real `build_soc/rtl/nanosoc.sv` (not the
   arch_tech copy, see §2.2's warning about the two diverging). Result:
   0 real errors; only the expected `PINCONNECTEMPTY` (intentionally
   unconnected outputs, matching the proven PYNQ reference tie-off pattern)
   and `UNUSED`/`UNDRIVEN` warnings inherent to linting a board wrapper with
   many genuinely-inert legacy I/O groups against an empty black box.
2. `filelist.tcl` and `build_monolithic.tcl` are both syntactically complete
   Tcl (`info complete`, no unbalanced braces/brackets/quotes) and were
   dry-run under plain `tclsh` with a stub harness standing in for the
   Vivado-only commands (`create_project`, `read_verilog`, `set_property`,
   `launch_runs`, etc.) — this confirmed **all 198 RTL files `filelist.tcl`
   references resolve on disk** (0 missing), and that `build_monolithic.tcl`
   takes the correct path both when `IMEM_MEM_FPGA_IMG`/`FPGA_SYNTH_ONLY` are
   set (proceeds through a stubbed synth-only smoke) and when neither is set
   (fails fast with a clear, actionable error instead of a confusing
   downstream one).

**Deliberately NOT run by this agent** (per this task's own constraints —
no Vivado, no cmake, no git): a real `synth_design`/`launch_runs synth_1`
under actual Vivado 2024.1, and a real firmware build (§4). The integrator's
exact next command, once firmware (§4) is built:

```bash
cd fpga/monolithic
source /apps/Xilinx/Vivado/2024.1/settings64.sh
export NANOSOC_BOOTROM_DIR=~/SoCLabs/mps3-nanosoc-platform/fpga/monolithic/build/firmware_mps3/stage0
export IMEM_MEM_FPGA_IMG=~/SoCLabs/mps3-nanosoc-platform/fpga/monolithic/build/hello_word.hex
vivado -mode batch -source build_monolithic.tcl \
    -log build/build_monolithic.log -journal build/build_monolithic.jou
```

A quicker sanity gate before committing to a full impl run:

```bash
cd fpga/monolithic
export FPGA_SYNTH_ONLY=1   # stop after synth_1; auto-stubs IMEM_MEM_FPGA_IMG if unset
vivado -mode batch -source build_monolithic.tcl
```

One item flagged for the integrator to double-check against a real Vivado
run (documented, not silently assumed, in both `nanosoc_mps3_top.sv` and
`build_monolithic.tcl`'s headers): the exact Tcl idiom used to override the
board top's `IMEM_MEM_FPGA_IMG` string parameter (`set_property generic
"IMEM_MEM_FPGA_IMG=$IMEM_MEM_FPGA_IMG" [current_fileset]`) is the documented/
common one for Vivado 2024.1 but was not exercised against a real
`synth_design` call by this agent — if the firmware image doesn't appear to
take effect, the reliable fallback is hand-editing the parameter's default
in `nanosoc_mps3_top.sv` before synthesis.
