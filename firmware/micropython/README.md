# MicroPython for SoC Labs NanoSoC (Cortex-M0)

A bare-metal MicroPython port that boots on the NanoSoC (Arm Cortex-M0,
armv6-m) and gives an interactive Python REPL over the CMSDK **UART2** console.
No RTOS, no interrupts — the console is fully polled.

This is built against the **`nanosoc_upy` SCAFFOLD** FPGA config (fat BRAM),
not the product. The product path keeps IMEM at 16 KB and executes large code
XiP from QSPI flash; a 128 KB IMEM cannot be reproduced for free in silicon.
See `fpga/rp/nanosoc_upy/rp_nanosoc_upy_wrapper.sv`, whose `IMEM_MEM_FPGA_IMG`
already points at the image this build produces.

## What you get

* A friendly REPL (`pyexec_friendly_repl`) over UART2 at **76800 baud, 8N1**.
* `machine.mem8` / `machine.mem16` / `machine.mem32` — raw memory access
  (the real `extmod/machine_mem.c` objects), e.g.

  ```python
  import machine
  machine.mem32[0x40010004] = 0xFF                  # drive the 8 user LEDs
  (machine.mem32[0x40010000] >> 8) & 0xFF           # read the 8 DIP switches
  ```

* A small `nanosoc` convenience module:

  ```python
  import nanosoc
  nanosoc.led(0xA5)          # write P0[7:0] -> LEDs
  nanosoc.switches()         # read P0[15:8] -> DIP switches
  nanosoc.mem32(0x40010000)  # 32-bit peek; add a 2nd arg to poke
  ```

No filesystem, no floating point, no frozen modules — all trimmed to fit.

## Target hardware (verified against the SoC sources)

| Item  | Value |
|-------|-------|
| CPU   | Cortex-M0, armv6-m, soft-float (`-mcpu=cortex-m0 -mthumb -mfloat-abi=soft`) |
| Clock | 50 MHz `dut_clk` (shell MMCM) |
| IMEM  | `0x10000000`, **128 KB**; aliased at `0x00000000` after REMAP — code + rodata, and where the image executes from |
| DMEM  | `0x18000000`, **64 KB** — `.data`/`.bss`/stack + GC heap |
| SRAM0/1 | `0x80000000` / `0x90000000`, 16 KB each — spare (unused by this port) |
| UART2 | `cmsdk_apb_uart` @ `0x40006000`; DATA +0x00, STATE +0x04 (b0=TX full, b1=RX full), CTRL +0x08 (b0=TXen, b1=RXen), BAUDDIV +0x10 = **651** (50 MHz / 76800) |
| GPIO0 | `cmsdk_ahb_gpio` @ `0x40010000`; P0[7:0]→LEDs, P0[15:8]←DIP switches |
| GPIO1 | @ `0x40011000`; **ALTFUNCSET bit 5** routes UART2 TXD onto P1[5]. RXD (P1[4]) is a direct pad tap — no altfunc — so P1[4] is left as an input |

### Boot model

The stage-0 bootrom runs first, sets the sysctrl REMAP bit so IMEM appears at
`0x00000000`, then jumps to it. This image is preloaded into IMEM and executes
from `0x00000000`, so the vector table is linked there (`nanosoc.ld`). The
reset handler sets SP, copies `.data`, zeroes `.bss`, brings up UART2, then runs
the REPL. Baud (76800) is fixed by the shell's `uart_axis_shim` and must match.

## Measured sizes (`arm-none-eabi-size`)

```
   text    data     bss     dec     hex
  66820       0    8648   75468   126cc
```

* `.text` (code + rodata) = **66,820 B ≈ 65.3 KB** in IMEM — fits 128 KB with
  ~60 KB to spare.
* `.data` = 0, `.bss` = **8,648 B ≈ 8.4 KB** in DMEM.
* GC heap = end of `.bss` up to an 8 KB stack guard below the top of DMEM,
  i.e. `_heap_end (0x1800E000) - _heap_start (0x180001C4)` ≈ **56 KB**.

The product 16 KB IMEM would NOT hold this (65 KB of code); that is exactly why
this runs on the `nanosoc_upy` scaffold and why the product path is XiP.

## Hybrid-XiP build (`make xip`)

Validates the one-config **product** flash architecture: a small fixed HOT set
resident in IMEM, with the bulk of the interpreter executing **in place (XiP)**
from QSPI flash through the CG092 read cache. Same single source tree, different
linker script (`port/nanosoc_xip.ld`) plus `-DNANOSOC_XIP`.

```
cd firmware/micropython
make xip        # -> build/xip/{micropython_hot_word.hex, micropython_cold.{bin,hex}, xip_manifest.txt}
```

### Hot / cold split

| Set | Where | Contents | Size |
|-----|-------|----------|------|
| **HOT** (resident) | IMEM `0x00000000` | vector table, reset/startup, `nanosoc_xip_bringup`, UART console, VM dispatch core (`mp_execute_bytecode`), GC | `.text` = **40,020 B ≈ 39.1 KB** (+ `.bss` 8,648 B in DMEM) |
| **COLD** (XiP) | QSPI flash `0x70000000` | lexer, parser, compiler, REPL line handling, builtin modules, qstr/const tables | `__xip_size__` = **28,780 B ≈ 28.1 KB** (`.text.xip` 21,680 + `.rodata.xip` 7,100) |

The fully-resident build is 66,828 B of `.text`; the split totals 68,800 B
(+1,972 B ≈ +3 %, from ld long-branch veneers for hot↔cold calls plus section
alignment). **Headline: the hot IMEM footprint drops from ~65 KB to ~39 KB** —
the product can carry a ~48–64 KB IMEM and keep the rest in flash.

### Which knobs / how the cold set is chosen

MicroPython's vendor sources can't be attribute-tagged function by function, so
`nanosoc_xip.ld` routes whole **cold object files** (`lexer.o`, `parse.o`,
`compile.o`, `repl.o`, `readline.o`, `pyexec.o`, the `mod*.o` builtins,
`qstr.o`, …) into the `.text.xip` / `.rodata.xip` output sections at
`0x70000000` (VMA==LMA). Port-owned code may instead opt in explicitly with the
`XIP_COLD` / `XIP_COLD_RO` macros in `port/xip_cold.h`. Bring-up
(`port/xip_bringup.c`, `nanosoc_xip_bringup()`) runs first in `Reset_Handler`,
before any cold fetch: it programs `AHB_SPI_SETUP=0x800B` (Fast-Read 0x0B + 8
dummies), sets `CTRL.XIP_ACTIVE`, enables + waits the CG092 cache, with the
load-bearing APB read-back barriers. **Cortex-M0 has no VTOR — nothing relocates
the vector table.**

### Artifacts

* `build/xip/micropython_hot_word.hex` — resident IMEM image, word-hex linked at
  `0x0` (word 0 = SP `0x18010000`, word 1 = reset vector `0x000083B5`); load like
  the resident `micropython_word.hex`.
* `build/xip/micropython_cold.bin` — the cold `.xip` segment as raw flash bytes,
  **byte offset 0 == aperture `0x70000000`** (flash the blob so it maps there).
* `build/xip/micropython_cold.hex` — the same cold segment as `@00000000`
  word-hex for a `$readmemh` flash VIP (byte offset 0).
* `build/xip/xip_manifest.txt` — `__xip_*` symbols, `arm-none-eabi-size`,
  `objdump -h` section headers, cold blob size. **The key output.**

### Fold-resident control

The characterisation control (everything resident) is just the default
`make` build (`nanosoc.ld`, no `-DNANOSOC_XIP`): its `.text` is **byte-identical
(66,828 B)** to the pre-XiP baseline — `xip_bringup.c` is `--gc-sections`-dropped
when uncalled. Because the cold set is routed by the *linker script* (object
files) rather than the `XIP_COLD` macro, `-DXIP_DISABLE` alone does not fold the
MicroPython cold set back — use the resident `nanosoc.ld` (i.e. plain `make`) for
that. `XIP_DISABLE` (in `xip_cold.h`) is retained for parity with the reference
and for any future port-owned `XIP_COLD`-tagged code.

## Rebuild

Requires the GCC 10.3 arm-none-eabi toolchain at
`~/runthrough_itb/gcc-arm-none-eabi-10.3-2021.10/bin` (the
top-level Makefile puts it on `PATH` for you).

```
cd firmware/micropython
make            # -> build/micropython_word.hex
make clean
```

`make` compiles the port, links `build/firmware.elf`, then:

```
firmware.elf
  --(objcopy -O verilog --verilog-data-width 1, .isr_vector/.text/.data)-->
firmware_byte.hex
  --(nanosoc_m0_soc/pynq/scripts/hex_byte_to_word.py)-->
micropython_word.hex   # @00000000 header, one 32-bit LE word per line
```

`build/micropython_word.hex` is the file the FPGA flow `$readmemh`s into the
32-bit IMEM BRAM (word 0 = initial SP `0x18010000`, word 1 = reset vector).

## Layout

```
firmware/micropython/
  Makefile              one-command wrapper (sets PATH, delegates to port/)
  README.md             this file
  port/
    Makefile            the real build (TOP -> vendor/micropython)
    main.c              vector table, reset handler, mp_init + REPL loop, gc_collect
    nanosoc.h           UART2 / GPIO register map + boot constants
    nanosoc_uart.c      polled mp_hal_stdin_rx_chr / mp_hal_stdout_tx_strn
    modnanosoc.c        `machine` (mem8/16/32) + `nanosoc` (led/switches/mem32)
    nanosoc.ld          linker script (vectors @ 0x0, IMEM 128K / DMEM 64K)
    mpconfigport.h      trimmed feature set; machine.memX on, float/fs/frozen off
    mphalport.h         tiny HAL (ticks/delay stubs)
    qstrdefsport.h      (empty) port qstrs
  vendor/micropython/   upstream clone (ports/minimal is the base reference)
  build/                ELF, byte-hex, word-hex, map
```

## Honesty / verification status

* **Compiles and links cleanly** for cortex-m0 with `-Werror`; sizes above are
  real `arm-none-eabi-size` output. Fits 128 KB IMEM.
* **Word-hex sanity-checked**: correct `@`-header + one 32-bit LE word/line;
  word 0 = `0x18010000` (SP at top of DMEM), word 1 = `0x0000CBC1` = the
  `Reset_Handler` address (0xCBC0) with the thumb bit set — both plausible and
  cross-checked against `nm`.
* **REPL NOT observed running.** There is no `qemu-system-arm` / FVP on this
  machine, so the banner was not executed. Co-simulation against the real RTL
  is the intended proof and is done separately.
* armv6-m specifics handled: MicroPython's NLR auto-selects the thumb1
  (non-thumb2) path; `gchelper_thumb1.s` gives a sound GC register scan; the
  reset handler avoids the illegal `ldr sp, =literal` (uses `ldr r0` + `mov sp`);
  and libgcc is linked for the M0's software divide (`__aeabi_uidiv`).
```
