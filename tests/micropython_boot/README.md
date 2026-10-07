# tests/micropython_boot — full-SoC RTL boot of MicroPython, REPL over UART2

**What this proves:** the delivered MicroPython image actually *runs* on the
real NanoSoC. Not "links", not "the image is well-formed" — the genuine
Cortex-M0 SoC boots from its own stage-0 BOOTROM, remaps the preloaded IMEM to
`0x0`, and the MicroPython interpreter initialises and reaches its REPL,
printing its banner on the UART2 console. This is the strongest possible
demo-works evidence short of a board.

Everything on the observed path is real RTL:

```
 stage-0 BOOTROM (real firmware)         MicroPython image ($readmemh into IMEM)
        |                                          |
   Cortex-M0 DesignStart (ARM IP)  ── AHB ──  nanosoc_region_imem (RAM_PRELOAD)
        |                                          |
   nanosoc_ss_systemctrl / pin_mux ── UART2 TXD ── nanosoc_ss_hostio4 (FT1248 mux)
        |                                          |
   cmsdk_apb_uart (UART2 @ 0x4000_6000)   ──►  p1_out[5] pad  ──►  cocotb 8N1 RX
```

The DUT is the exact `nanosoc` core the FPGA build packages
(`$SOCLABS_NANOSOC_SOC_DIR/build_soc/rtl/nanosoc.sv`), compiled from the exact
same fileset (see *Filelist* below). Nothing on the boot/console path is
stubbed.

---

## Result (observed)

**GATE 1 — boot to the MicroPython banner: PASS.**

Booted the real SoC and received, verbatim, on the UART2 TXD pad (decoded 8N1,
LSB-first, one bit = 651 `dut_clk` cycles):

```
~BQqI=18010000,0000CBC1 V=1800FC00,08000189 M=00000001 W=18010000,0000CBC1 RMicroPython 532428cc6d on 2026-0...
```

Reading it out (the leading run is stage-0's own boot telemetry on UART2, and
it is *itself* a mini proof that the image is in place):

| bytes | meaning |
|-------|---------|
| `~` | UART2 came alive (stage-0 `UartStdOutInit`) |
| `B` | stage-0 banner sent |
| `Q` | `BOOT_CFG.QSPI_PRESENT=1` → stage-0 attempts 2-stage flash boot |
| `q` | no flash device on the pads → JEDEC probe = NO-CHIP → fall back to IMEM |
| `I=18010000,0000CBC1` | stage-0 reads **IMEM[0]=0x18010000 (SP)** and **IMEM[1]=0x0000CBC1 (reset vector)** — these are exactly the MicroPython image's word0/word1, i.e. the image really is loaded in IMEM |
| `V=1800FC00,08000189` | address-`0` view *before* REMAP = the BOOTROM's own vectors |
| `M=00000001` | `SYSCON.REMAP` readback = 1 (remap took) |
| `W=18010000,0000CBC1` | address-`0` view *after* REMAP = the MicroPython vectors (equals `I=`, so IMEM now aliases to 0x0) |
| `R` | stage-0 jumps to the remapped IMEM image |
| `MicroPython 532428cc6d on 2026-0...` | **the interpreter's own banner**, printed by `pyexec_friendly_repl()` after `mp_init()` — the REPL is up |

- First UART2 TX pad transition: **22.74 µs** (CPU out of reset, ran stage-0,
  drove the console).
- `"MicroPython"` first appears at **byte offset 76**, **sim-time 15.00 ms**.
- Run cost: ~208 s wall / 15.0 ms sim under VCS (cocotb forces full signal
  access, which is the dominant slowdown).

The banner text is `MicroPython <MICROPY_GIT_TAG> on <MICROPY_BUILD_DATE>; SoC
Labs NanoSoC (Cortex-M0) with cortex-m0` (from `mpconfigport.h`
`MICROPY_HW_BOARD_NAME`/`MICROPY_HW_MCU_NAME`). The GATE-1 budget stops the sim
as soon as `"MicroPython"` is matched, so the captured tail shows the banner
mid-print (`... on 2026-0`); run longer (`UPY_MAX_MS` higher) to see it finish.

**GATE 2 — REPL RX round-trip (`print(1+1)` → `2`): PASS.**

With `UPY_GATE2=1`, after the banner the bench waited for the `>>> ` prompt,
drove `print(1+1)\r\n` into UART2 RXD (`p1_in[4]`, 8N1 @ 651 cycles/bit), and
observed the interpreter **echo the command, evaluate it, and print `2`**
followed by a fresh prompt. Full verbatim UART2 capture:

```
~BQqI=18010000,0000CBC1 V=1800FC00,08000189 M=00000001 W=18010000,0000CBC1 RMicroPython 532428cc6d on 2026-07-14; SoC Labs NanoSoC (Cortex-M0) with cortex-m0
>>> print(1+1)
2
>>> 
```

(post-command bytes captured after our input: `\r\n2\r\n>>> `.) This is the
full REPL round-trip on the real RTL — the host talks Python to the DUT and the
DUT computes and answers. Cost: 27.56 ms sim / ~375 s wall.

---

## How to run

```bash
source ~/SoCLabs/mps3-nanosoc-platform/set_env.sh   # py3.10 + cocotb 2.0.1 + VCS
cd tests/micropython_boot
make                       # GATE 1 (banner). SLOW — minutes of wallclock.
make UPY_MAX_MS=1500        # give the sim a larger sim-time budget
make UPY_GATE2=1            # also attempt the RX round-trip after the banner
```

Useful knobs (all environment variables):

| var | default | meaning |
|-----|---------|---------|
| `UPY_MAX_MS` | `800` | hard sim-time budget for GATE 1, in sim-milliseconds |
| `UPY_PROGRESS_MS` | `2` | heartbeat / poll interval, in sim-milliseconds |
| `UPY_GATE2` | `0` | set `1` to drive `print(1+1)\r\n` into RX and check for `2` |
| `UPY_IMAGE` | built `micropython_word.hex` | IMEM preload image (word-hex) |
| `IMEM_RAM_ADDR_W` | `17` | IMEM size (128 KB — the image needs it) |
| `DMEM_RAM_ADDR_W` | `16` | DMEM size (64 KB — SP = 0x18010000 = DMEM top) |

**This is a slow bench by nature.** Booting a real interpreter is many
simulated milliseconds and each UART byte is 130 µs of sim (651 cycles/bit ×
10 bits @ 50 MHz). Stage-0's telemetry alone is ~10 ms sim; the banner lands
around 15 ms. Expect minutes of wallclock. The test streams every received
byte to the log with its sim-time so a long run is visibly alive, and if the
banner is not reached it reports *exactly* how far it got (pad never moved /
bytes garbled at the wrong baud / bytes fine but banner not yet) rather than a
bare pass or fail.

---

## How it is wired

- **`collect_filelist.tcl`** — the nanosoc RTL fileset is authored as a Vivado
  TCL (`$SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl`, 21 `read_verilog` groups:
  Cortex-M0 DesignStart, Corstone-101 CMSDK, socdebug, hostio4, the generated
  interconnects, PL022 SSP, the QSPI controller + CG092 cache, and the
  generated `nanosoc.sv`). Rather than hand-transcribe it and risk drift, this
  script **sources that exact filelist** with tiny stub `read_verilog` /
  `set_property` procs that record what it would compile, then emits an
  equivalent VCS `-f` file (`nanosoc.f`). So the bench always compiles exactly
  the SoC the FPGA build does. It also adds each source file's own directory as
  an `+incdir+` (Vivado does this implicitly; VCS does not — needed for CG092's
  `p_flash_cache_f0_gen_const_pkg.vh`, which sits beside its `.v` files).

- **`micropython_boot_tb.sv`** — instantiates `nanosoc` with
  `IMEM_MEM_FPGA_IMG` = the built `micropython_word.hex`, `IMEM_RAM_ADDR_W=17`,
  `DMEM_RAM_ADDR_W=16`, and ties off every non-console port using the exact
  disposition proven in `fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` (expansion AHB
  slave inert, DMA streams sunk, SPI/QSPI inputs inert with drives open,
  scan/test off). Key console wiring:
  - `p1_in[7]=1` (FT1248MODE strap) so hostio4 routes the pin mux's `uart2_txd`
    (systemctrl `P1_OUT_MUX[5]`) out to the top-level `p1_out[5]` pad.
  - UART2 TXD tap: `uart_txd = p1_outen[5] ? p1_out[5] : 1'b1` (idle-high until
    firmware sets ALTFUNC), same idiom as the HW wrapper's `uart_txd_int`.
  - UART2 RXD is `p1_in[4]`, driven from the `dut_rxd` bench port for GATE 2.
  - the FT1248/USRT self-drain loopbacks on `p1_in[0..3]` (again mirroring the
    wrapper) keep stage-0's FT1248-side banner from backing up on an absent
    host. (Stage-0 also bounds every console wait, so it cannot wedge.)

- **`test_micropython_boot.py`** — drives `dut_clk` at 50 MHz (20 ns), resets,
  runs an 8N1 UART receiver on `uart_txd` (sample mid-bit, LSB first,
  `BIT_CYCLES=651`), streams and assembles bytes, and gates on `"MicroPython"`.
  GATE 2 (optional) feeds `print(1+1)\r\n` into `dut_rxd` and looks for `2`.

- **`Makefile`** — sets the `SOCLABS_*` / `ARM_IP_LIBRARY_PATH` /
  `FPGA_BOOTROM_DIR` / `SOCLABS_AHB_QSPI_DIR` env the filelist needs, generates
  `nanosoc.f`, passes `+define+RAM_PRELOAD` (via the filelist) and the IMEM
  image/size as VCS `-pvalue`, and hands off to cocotb's `Makefile.sim`.

## Boot model (why it works without an ADP host)

`nanosoc` is built `RAM_PRELOAD` + `QSPI_FLASH_PRESENT=1`, `BOOT_MODE=0` (ADP
external load). On reset the CPU fetches the stage-0 BOOTROM at `0x0`. Stage-0
probes QSPI (no device on the bench pads → NO-CHIP), falls back to the ADP/IMEM
path, sets `SYSCON.REMAP` so the *already-preloaded* IMEM aliases to `0x0`, and
jumps there. The "ADP external load" normally means a host downloads the image;
here `RAM_PRELOAD` has already put the image in IMEM at elaboration
(`sl_fpga_rom_word` `$readmemh`), so the REMAP-and-jump lands straight on the
MicroPython vector table. No external host is needed.
