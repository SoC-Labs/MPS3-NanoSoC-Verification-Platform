# MPS3 Onboard QSPI Flash → nanoSoC Ethernet-Subsystem Flash — Feasibility Report

> **Status: HISTORICAL** — a record of the MPS3 on-board QSPI flash feasibility study as of 2026-07-09.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

Status: research / design analysis only. **No RTL changed, nothing committed, no builds run.**
Date: 2026-07-07 · Author: analysis pass over `nanosoc-multicore-system`, `mps3-nanosoc-platform`, `ethernet-mac-ahb`, plus the Arm MPS3 AN543 board-config text + AN543 board application note (board facts only; no Arm processor IP read).

---

## 1. Summary / Verdict

**Verdict: FEASIBLE — with caveats.** Re-pointing the nanoSoC ethernet subsystem's QSPI flash from the PMOD-routed Diligent Pmod SF3 to the MPS3's **onboard 8 MB QSPI flash** is a clean fit at the RTL/pin/interface level and needs **no new flash controller and no change to the nanoSoC memory map**. The work is *integration + a firmware part-swap*, not a redesign.

Why it is a strong fit:

| Axis | nanoSoC eth flash | MPS3 onboard flash | Match? |
|---|---|---|---|
| Interface | Quad-SPI x4 (`qspi_sclk`,`qspi_csn`,`qspi_io[3:0]`) | Quad-SPI x4 (`QSPI_SCLK`,`QSPI_nCS`,`QSPI_D0..3`) | ✅ 1:1 |
| I/O standard | LVCMOS33 (PMODA) | LVCMOS33 (AU/AV/AT balls) | ✅ |
| Command model | Register/firmware-programmable opcodes | (n/a — driven by our controller) | ✅ |
| Flash part **already modelled** | VIP = **SST26VF064B** | **SST26VF064B** | ✅ *exact same part* |
| Size vs addressed aperture | 4 MB aperture (`ADDR_W=22`) | 8 MB device | ✅ aperture ⊂ device |
| Pins already in an XDC | PMODA (pynq) | AU24/AV24/AV21/AV22/AT25/AT24 (already present, tied-off) | ✅ |

The single most de-risking fact: **the nanoSoC `ahb_qspi` controller's own verification model *is* the SST26VF064B** (`ahb_qspi/verif/VIP/SST26VF064B.v`, used by both the UVM and cocotb benches) — i.e. the controller has already been simulated against a behavioural model of the exact flash chip fitted to the MPS3.

The caveats that make it "with caveats" rather than "trivially yes":

1. **DUT gap** — the platform's *monolithic* MPS3 top currently instantiates the **single-core `nanosoc`, which has no QSPI port at all**; the QSPI-bearing SoC is the **multicore** SoC (`u_qspi_flash_0`). Mapping the *ethernet subsystem's* flash requires the multicore SoC to be the MPS3 DUT (or the QSPI ports brought out of whatever DUT is used).
2. **Firmware part-swap** — SST26VF064B powers up with **all blocks write-protected**; the program/erase driver must issue a Global Block-Protection Unlock (`0x98`) and match the SST26 JEDEC ID / dummy-cycle counts. Read/XiP opcodes (`0x0B` fast-read, quad reads) are already SST26-compatible and cocotb-exercised.
3. **Single-flash contention** — the DFX **shell already claims that same onboard flash** for its bitstream "OVLSTORE" (Xilinx AXI Quad SPI). There is only one 8 MB device: in the DFX-shell flow it is *either* the shell's overlay store *or* the nanoSoC firmware flash, not both. Clean only in the **monolithic** flow.
4. **Board-TRM unknowns** — exact FPGA-pin↔flash-pin quad wiring and any MCC/QSPI arbitration/handoff need the **MPS3 board TRM (HBI0309)**; the AN543 config + the platform XDC are consistent but not the primary authority.

---

## 2. nanoSoC Ethernet-Subsystem Flash — What It Is Today

### 2.1 Controller (SoC Labs' own IP, not Arm/CMSDK)

- Controller top `top_ahb_qspi` — `nanosoc-multicore-system/ahb_qspi/logical/top_ahb_qspi/logical/top_ahb_qspi.v:24`
  Composes: `qspi_controller` (SPI/Dual/Quad FSM, `:448`), `qspi_controller_mux` (`:321`), `apb_qspi_regs` (`:366`), `ahb_qspi_interface` (`:407`), `qspi_clock_div` (`:440`), `cmsdk_apb_slave_mux`, and a `cache_subsystem` (CG092-derived XiP read cache, `:205`).
- SoC wrapper `qspi_flash_ahb` — `nanosoc-multicore-system/src/rtl/wrappers/qspi_flash_ahb.v:23` (instantiates `top_ahb_qspi` at `:75`).
- SoC-top instance `u_qspi_flash_0` — declared `nanosoc-multicore-system/sys_desc/nanosoc_multicore_soc.yaml:717`; generated RTL `imp/fpga/nanosoc_multicore_ip/src/nanosoc_multicore_soc.sv:1020,1027`. A companion `u_qspi_apb_bridge` (`cmsdk_ahb_to_apb`) exposes its register block (`yaml:753`).

### 2.2 Interface = Quad-SPI x4, tristate per lane

- Chip-boundary ports — `sys_desc/nanosoc_multicore_soc.yaml:355-359`:
  `qspi_sclk` (o), `qspi_csn` (o), `qspi_io_o[3:0]` (o), `qspi_io_i[3:0]` (i), `qspi_io_e[3:0]` (o output-enable).
- Wrapper ports — `src/rtl/wrappers/qspi_flash_ahb.v:65-69`: `QSPI_SCLK`, `QSPI_nCS`, `QSPI_IO_o[3:0]`, `QSPI_IO_i[3:0]`, `QSPI_IO_e[3:0]`.
- FPGA (PYNQ) tristate convention — `imp/fpga/nanosoc_multicore_ip/src/nanosoc_multicore_vivado_wrapper.v:101-105`: `qspi_sclk`, `qspi_ncs`, `qspi_io_i/o/t[3:0]`; 4× `IOBUF` fold these into the `qspi_io[3:0]` inout bus (`nanosoc_multicore_design_wrapper.v:170-185`).
- **Command model is fully register/firmware-programmable**: the opcode (`QSPI_CMD[7:0]`), dummy-cycle count, address-enable, byte count, `QSPI_QIO_MODE`, continuous-read and mode-code all arrive from APB/AHB registers (`top_ahb_qspi.v:264-314, 366-401`). The controller is therefore **not hard-wired to any one flash part** — the driver chooses the opcodes.

### 2.3 Memory map (nanoSoC's own bus — independent of the MPS3 host map)

- APB register interface: **`0x21000000`** (16 MB window) — `nanosoc_multicore_soc.yaml:2050`; `firmware/include/nanosoc_multicore_addrmap.h:91`.
- **XiP execute-in-place data aperture: `0x24000000`** (64 MB window; backing `phys_size = 2**QSPI_FLASH_ADDR_W` = **4 MB**, `sw_access: rx`) — `nanosoc_multicore_soc.yaml:2053`; `nanosoc_multicore_addrmap.h:114`.
- Both bases sit inside CPU0's (network_core's) 256 MB passthrough window and `qspi_flash_xip` is in `eth_ss_m`'s target list — the ethernet core reaches the flash directly.

### 2.4 How the ethernet / network core uses the flash

1. **XiP hot/cold IMEM overlay** (core mechanism): cold CLI/telnet/TFTP/GDB/HTTP/TCP code tagged `XIP_COLD` executes in place from `0x24000000` through the CG092 cache; hot vectors/ISRs/MAC-datapath/PTP stay in IMEM. This is what shrank ETH IMEM 64 KB→32 KB. `firmware/include/xip_cold.h:7-24,46-47`; bring-up (fast-read `0x0B` + 8 dummies, `XIP_ACTIVE=1`, cache `CCR.EN=1`) `firmware/include/xip_bringup.h:58-79`.
2. **Boot flash→IMEM copy** (CRC-verified) — `firmware/bootloader/stage0_bootrom/main.c:94-152` reads `NANOSOC_QSPI_XIP_BASE + offset`, word-copies to IMEM `0x10000000`, CRC-checks, REMAP+jump.
3. **App-library ('ALIB') overlay** — `firmware/bootloader/stage0_applib/main.c:5-16,202-207` copies `image[index]` flash→IMEM.
4. **TFTP stream-to-flash** (network firmware update) — `firmware/apps/eth_netapp/tftp_flash.h:96-116,328-482` streams a CPU1 image over UDP into the inactive A/B slot and rewrites the boot table. Note constraint `tftp_flash.h:22-30`: **DMA cannot read the XiP aperture**, so bulk sources must be SRAM.

Flash layout (host `flash_pack.py`): 64 KB-sector map — TABLE0 `0x00000`, TABLE1 `0x10000`, COUNTER `0x20000`, CPU1 SLOT_A `0x30000`, SLOT_B `0x40000`, GOLDEN `0x50000`; boot magic `'BOOT'`. `firmware/include/nanosoc_multicore_addrmap.h:311-427`.

### 2.5 Assumed part & size

- **Addressed aperture = 4 MB** (`QSPI_FLASH_ADDR_W=22`, `nanosoc_multicore_soc.yaml:165`; wrapper `FLASH_ADDR_W` `sys_desc/qspi_flash_ahb.yaml:79`). Internal flash address bus is 22-bit (`top_ahb_qspi.v:92`) ⇒ **3-byte addressing suffices**; no 4-byte-address mode needed.
- **Physical parts referenced**: Micron **N25Q256A** (32 MB) on the bench (Pmod SF3); **SST26VF064B** (8 MB) as the simulation VIP. Both exceed the 4 MB aperture. `firmware/apps/qspi_flasher/main.c:129-133`, `cocotb/soc_boot_flash/tb_top.sv`.
- Geometry: 64 KB sector erase (`0xD8`), 256 B page — `qspi_flasher/main.c:129-133`.

### 2.6 How it is pinned today (PMOD, not the board-TRM belief)

- **Current multicore build → PMODA (JA)** on PYNQ-Z2, LVCMOS33: `qspi_ncs`=Y18, `qspi_io[0]`=Y19, `qspi_io[1]`=Y16, `qspi_sclk`=Y17, `qspi_io[2]`=U18, `qspi_io[3]`=U19 — `pynq/targets/pynq-z2/nanosoc_multicore.xdc:51-56`; identical in `ahb_qspi/fpga/targets/pynq-z2/pynq_z2_qspi.xdc:24-29`.
- The "**QSPI on PMOD Bank-34/35**" recollection traces to the **legacy standalone** ahb_qspi target `ahb_qspi/fpga/pynq_z2/PYNQ-Z2 v1.0.xdc:1-7`, which put QSPI on **PMODB (JB)** balls W14/Y14/T11/T10/V12/W13 — the PYNQ-Z2 bank-34/35 pins. In the *current* design bank-34/35 carries the **Ethernet PHY**, and QSPI moved to PMODA. Either way, today's flash is **PMOD-routed to an off-board Pmod module** — exactly the arrangement this report proposes to replace.

---

## 3. MPS3 Onboard QSPI Flash — Board Facts (AN543 / HBI0309C)

Sourced only from board-config text and the AN543 board application note (board/FPGA-infrastructure facts; no Arm processor IP read).

- **FPGA_QSPI: TRUE** — `MPS3/Corstone-700/Boardfiles/MB/HBI0309C/AN543/an543_v3.txt:54`.
- **QSPIBASE: 0x04000000** (QSPI controller / write config; → Corstone target AHB `0x0A000000`) — `an543_v3.txt:55`.
- **QSPIDATA: 0x02000000** (QSPI data / XiP read; → target AHB `0x08000000`) — `an543_v3.txt:56`.
- **QSPISCC: 0x08** — `an543_v3.txt:57`.
- **Device: "8MB of QSPI flash … connected to the SSE-700 XNVM port … The QSPI Xilinx controller is utilized to support QSPI flash"** — AN543 §3.3.7 (DAI0543C, p3-17). §3.3.3 "Xilinx QSPI Controller … connect[s] the QSPI flash interface to the NIC-400 implemented in the FPGA Subsystem."
  ⇒ The flash chip's SPI pins are **on FPGA package I/O**, driven by a **soft QSPI controller instantiated in the FPGA fabric**. In the Corstone image that is a Xilinx controller; in *our* image it can be the nanoSoC `ahb_qspi` controller instead — the flash does not require any specific vendor controller, only the right pins + opcodes.
- **Part = SST26VF064B** (Microchip/SST, 64 Mbit = 8 MB, SPI/Dual/Quad/QPI): asserted by the platform's own XDC — `fpga/monolithic/nanosoc_mps3.xdc:251` ("Quad SPI boot/overlay flash (SST26VF064B)") and `nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/fpga_pinmap.xdc:546-557`. Consistent with AN543's "8 MB QSPI flash". (Board TRM is the ultimate authority — see §6.)
- **FPGA package pins (from the arm_mps3 pinmap, LVCMOS33 + PULLUP):**
  `QSPI_D0`=AU24, `QSPI_D1`=AV24, `QSPI_D2`=AV21, `QSPI_D3`=AV22, `QSPI_SCLK`=AT25, `QSPI_nCS`=AT24 — `nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/fpga_pinmap.xdc:546-557,970-973`; mirrored in `mps3-nanosoc-platform/fpga/monolithic/nanosoc_mps3.xdc:254-269`.
- **MCC can preload the QSPI flash from the SD card at boot** — AN543 §8.8 (DAI0543C p8-66) and `images.txt` `IMAGE2ADDRESS: 0x02000000` = XNVM (QSPI Flash), `IMAGE2UPDATE: FORCEQSPI/AUTOQSPI`. This is a **firmware-loading convenience**: the flash image can be dropped on the SD card instead of programmed by an external tool or the on-chip flasher.
- **REMAP boot-device selector (BRAM/QSPI)** — `config.txt:9` `REMAP: BRAM`; `board.txt` `FPGA_REMAP: FALSE`, `REMAPVAL: 0`. This is a Corstone-boot concept; the nanoSoC image owns its own bus and does not depend on the MCC REMAP.
- Adjacent board facts (context, not flash): MPS3 Ethernet is an **SMSC LAN9220** memory-mapped MAC at `LANBASE 0x06100000` (AN543 §3.3.9) — *not* an RMII PHY — with `OSC0 25.0 MHz` eth reference. That is a separate porting concern from flash and out of scope here.

---

## 4. Mapping Analysis — Can the eth flash be re-pointed to the onboard QSPI?

**Interface: 1:1 compatible.** Both sides are 6-wire Quad-SPI (SCLK, nCS, IO[3:0]) at LVCMOS33. The nanoSoC controller drives `qspi_io_o/i/e[3:0]` through tristate lanes; those fold through 4× `IOBUF` into a bidirectional `QSPI_D[3:0]` bus — the exact IOBUF pattern already used on PYNQ. Direct wire map:

```
nanoSoC (SoC top)     via IOBUF        MPS3 pin      package
qspi_io_o/i/e[0]  <->  QSPI_D0     ->  AU24  (LVCMOS33, PULLUP)
qspi_io_o/i/e[1]  <->  QSPI_D1     ->  AV24
qspi_io_o/i/e[2]  <->  QSPI_D2     ->  AV21
qspi_io_o/i/e[3]  <->  QSPI_D3     ->  AV22
qspi_sclk          ->  QSPI_SCLK   ->  AT25
qspi_csn           ->  QSPI_nCS    ->  AT24
```

**Controller ↔ part: already proven in sim.** The command set is firmware-programmed, and the controller's regression VIP is `sst26vf064b` (`ahb_qspi/uvm/ahb_qspi/tb/top.sv:111`, `ahb_qspi/verif/cocotb/ahb_qspi_cocotb.v:155`, flist `ahb_qspi/flist/VIP/ahb_QSPI_VIP.flist:3`; JEDEC ID + opcode table for SST26VF064B in `ahb_qspi/uvm/ahb_qspi/env/ahb_qspi_pkg.sv:103,124`). So the read/XiP/erase/program flows the design relies on have already been exercised against a model of the MPS3's exact chip.

**Memory map: no change.** The flash lives at `0x21000000`/`0x24000000` in *nanoSoC's own* address space. The AN543 `QSPIBASE`/`QSPIDATA` (0x04/0x02_000000) are the **Corstone SoC's** view and are irrelevant to a nanoSoC bitstream, which owns the QSPI pins directly and decodes them however its own interconnect says. This removes what would otherwise be the hardest part of a re-map.

**Size: fits.** The controller addresses 4 MB (22-bit); the SST26VF064B is 8 MB. The nanoSoC boot layout uses only the first ~0x60000 (`addrmap.h:376-382`), comfortably inside 4 MB, and all reads are 3-byte-address — no 4-byte-address mode is needed (unlike the >16 MB reach of the 32 MB N25Q).

**The catch — which DUT owns the pins:**

- The platform's **monolithic** top (`fpga/monolithic/nanosoc_mps3_top.sv`) instantiates the **single-core `nanosoc`** (`:499`), whose port list has **GPIO/exp/PL022-SPI but no QSPI controller port** (`:499-591`), and the board top **ties the QSPI pins off** (`QSPI_SCLK=0`, `QSPI_nCS=1`, `QSPI_D*=z`, `:465-470`). So the *ethernet-subsystem* flash cannot be reached through today's monolithic DUT — the **multicore** SoC (the one with `u_qspi_flash_0`) must be the DUT.
- The DFX **shell** already instantiates a **Xilinx AXI Quad SPI ("OVLSTORE", `0x44A40000`, standard SPI mode)** onto those *same* AU24… pins for the bitstream-overlay store — `fpga/shell/bd/shell_bd.tcl:643-659`, `fpga/shell/shell_top.sv:73-85,196-202`, currently "wired but idle pending D15". There is **one** physical 8 MB flash: it can serve the shell's overlay store **or** the nanoSoC DUT firmware, not both simultaneously.

---

## 5. Required Changes (scoped, no work done here)

**A. FPGA top-level integration (the real work).**
1. Make the **multicore** nanoSoC SoC the MPS3 DUT (the QSPI-bearing SoC), or bring `qspi_sclk/qspi_csn/qspi_io_o/i/e[3:0]` out of whichever DUT is used.
2. Replace the QSPI tie-offs (`nanosoc_mps3_top.sv:465-470`) with **4× IOBUF** connecting `qspi_io_o/i/e[3:0]`↔`QSPI_D0..3` and direct drives `qspi_sclk→QSPI_SCLK`, `qspi_csn→QSPI_nCS`. Reuse the existing pattern from `nanosoc_multicore_design_wrapper.v:170-185`.

**B. XDC.**
- Pins already exist (`nanosoc_mps3.xdc:254-269` / `arm_mps3/fpga_pinmap.xdc:546-557`, LVCMOS33 + PULLUP). Just ensure the top-level port names line up (`QSPI_D0..3/SCLK/nCS`) and drop the tie-offs.
- Add QSPI timing: a generated clock for SCLK + `set_input/output_delay` on `QSPI_D*` (crib from `ahb_qspi/fpga/targets/pynq-z2/pynq_z2_qspi_timing.xdc:19-37`, where SCLK=HCLK/10). The MPS3 harness currently `set_false_path`s the QSPI ports (firmware-paced) — `fpga/shell/constraints/mps3_harness_timing.xdc:76-82`; acceptable to start, tighten later.

**C. Memory map / controller config: none.** Keep `0x21000000`/`0x24000000`. `QSPI_FLASH_ADDR_W=22` is fine (4 MB ⊂ 8 MB).

**D. Firmware / driver part-swap (N25Q256A → SST26VF064B).**
- JEDEC-ID check → SST26VF064B ID (already in `ahb_qspi_pkg.sv:124`).
- **Add SST26 Global Block-Protection Unlock (`0x98`)** after WREN, before first erase/program — SST26 boots fully write-protected. This is the highest-risk software item and connects to the existing WEL/WREN handoff lineage.
- Confirm dummy-cycle counts for fast-read `0x0B` (8) and any quad read; 64 KB sector erase `0xD8` is SST26-valid. Read/XiP path is already SST26-safe (cocotb-proven).

**E. Firmware loading workflow (optional upgrade).**
- Use MCC SD-card preload (`images.txt` IMAGE2 `FORCEQSPI`) to stage the flash image at boot — removes the need for the SWD `qspi_flasher` or an external programmer. The on-chip TFTP stream-to-flash update path still works unchanged.

**Effort estimate: MODERATE.** No new controller RTL. Bulk of effort = (A) DUT/IOBUF integration + (D) the SST26 unlock/JEDEC firmware delta + board bring-up. Estimate ~1–2 focused days to a "flash reads/XiP works on MPS3" gate, plus board validation.

---

## 6. Risks & Unknowns

**Risks (design-level):**
- **Single-flash contention (architectural).** In the DFX-shell flow the onboard flash is already spoken for by OVLSTORE (`shell_bd.tcl:643`). Decide ownership: monolithic nanoSoC-on-MPS3 (flash = nanoSoC firmware, clean) vs shell+RP (flash = bitstream store; nanoSoC firmware must then come from BRAM/DDR, or a second flash). **Recommendation: prove this in the monolithic flow first.**
- **SST26 block-protection.** Forgetting the `0x98` unlock ⇒ silent erase/program failures (reads still work). Must be in the driver before any write path is trusted.
- **QSPI SCLK timing/electrical on MPS3 balls.** Pull-ups are set in XDC; SCLK is slow/firmware-paced. Low risk but board-validate (the Pmod-SF3-on-PMODA flow itself was never fully HW-demonstrated, so this is first-silicon either way).
- **DMA-vs-XiP restriction carries over.** The "DMA can't read the flash aperture" rule (`tftp_flash.h:22-30`) is a controller property, unchanged by the board move — bulk copies must still stage through SRAM.

**Unknowns — need the MPS3 board TRM (HBI0309), NOT Arm processor IP:**
1. **Confirm the physical part = SST26VF064B** and its exact size/geometry. (Platform XDC + AN543 "8 MB" agree, but the board TRM is authoritative.)
2. **Confirm the FPGA-pin↔flash-pin quad wiring** (AU24/AV24/AV21/AV22/AT25/AT24) and that all four IO lanes are board-routed for x4 (vs x1 only). The XDC provides the map; the board TRM confirms it.
3. **MCC ↔ QSPI arbitration / handoff.** AN543 §8.8 says the MCC preloads the flash at boot then releases (SSE-700/EXTSYS0 held in reset during preload), implying the FPGA owns the flash at runtime — but the platform tracks this as open item **"D15" (MCC config-flash arbitration)**. Confirm there is no runtime contention and that our bitstream, not the MCC, drives the pins after configuration.
4. **REMAP independence.** Confirm a nanoSoC bitstream does not depend on the MCC REMAP register (expected: it does not; nanoSoC decodes its own bus).

**Facts that reduce these unknowns:** the platform's own XDC/BD already commit to SST26VF064B on those exact pins for OVLSTORE, and the nanoSoC controller is already SST26-verified — so items 1–2 are low-risk confirmations rather than open questions.

---

## 7. Recommendation

**Proceed — feasible with caveats — starting in the monolithic (non-DFX) nanoSoC-on-MPS3 flow.**

1. Make the **multicore** SoC the monolithic MPS3 DUT and wire its QSPI (`qspi_io_o/i/e`, `qspi_sclk`, `qspi_csn`) through 4× IOBUF to the already-pinned `QSPI_D0..3/SCLK/nCS` (drop the tie-offs). No memory-map change.
2. Add the **SST26VF064B block-protection unlock (`0x98`)** + JEDEC-ID swap in the flash driver; keep the read/XiP path as-is (already SST26-proven in cocotb).
3. Bring firmware in via **MCC SD-card preload** (`images.txt` IMAGE2 `FORCEQSPI`) for the first bring-up — no external programmer needed.
4. Validate on board: JEDEC read → sector erase/program (post-unlock) → XiP fast-read → CPU0 boot flash→IMEM.
5. **Defer** the DFX-shell case until the monolithic path is proven, and there explicitly resolve the **single-flash ownership** question (nanoSoC firmware vs OVLSTORE bitstream store) — likely by giving the shell a *different* store (DDR/second device) if both are needed at once.
6. Before hardware, obtain the **MPS3 board TRM (HBI0309)** to close unknowns 1–4 (part/geometry, quad wiring, MCC arbitration, REMAP independence).

Net: this replaces an off-board Pmod-SF3 dependency with the board's own 8 MB flash at essentially **zero controller-RTL cost** and **zero memory-map churn**, trading it for a bounded firmware part-swap and a DUT-integration step — a favourable trade that also unlocks SD-card firmware staging via the MCC.
