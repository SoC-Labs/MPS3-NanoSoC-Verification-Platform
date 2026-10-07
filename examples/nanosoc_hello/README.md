# nanosoc_hello

A minimal C program for the `nanosoc` design on the MPS3 nanoSoC platform.
It prints a message and a counter on the DUT console, about once a second.

It is written for the `nanosoc` design's processor, a Cortex-M0
(`-mcpu=cortex-m0`). Other nanoSoC members use other Cortex-M cores.

Everything here is our own: the vector table, the reset handler, the linker
script and a register-level console driver. It needs no C library and no
vendor headers.

## Build

You need the Arm GNU Toolchain (`arm-none-eabi-gcc`) on your PATH, and `make`.

```
make
```

```
   text    data     bss     dec     hex filename
    432       0       4     436     1b4 nanosoc_hello.elf
```

Change the message in `main.c` (`MESSAGE`) and run `make` again.

## Run it

Getting Started, chapter 13 ("Your own program"), loads it into the running
`nanosoc` design with GDB and shows its output on the console. In short, with
a debug session up (`harness-manager debug up <board>`):

```
arm-none-eabi-gdb nanosoc_hello.elf -ex "set remotetimeout 60" -ex "target extended-remote 127.0.0.1:<port>"
(gdb) monitor halt
(gdb) load
(gdb) set $sp = *(unsigned int *) 0x10000000
(gdb) set $pc = *(unsigned int *) 0x10000004 & ~1
(gdb) continue
```

## Files

| File | What it is |
|---|---|
| `main.c` | The program: console set-up, a character writer, the loop |
| `startup.S` | The vector table and the reset handler (copies data, clears bss, calls `main`) |
| `nanosoc_hello.ld` | The memory map: code in IMEM, data and stack in DMEM |
| `Makefile` | One compile-and-link command, `-nostdlib` |

## The hardware facts it uses

| What | Value |
|---|---|
| IMEM (code) | 0x1000_0000, 16 KB; the boot ROM also maps it at 0 before it starts a program |
| DMEM (data, stack) | 0x1800_0000, 16 KB; the stack starts at 0x1800_4000 |
| Console UART (UART2) | 0x4000_6000: DATA +0x00, STATE +0x04 (bit 0 TX full), CTRL +0x08 (bit 0 TX enable), BAUDDIV +0x10 |
| UART2 TX pin | GPIO1 (0x4001_1000) pin 5, as an alternate function: ALTFUNCSET +0x18 |
| DUT clock | 50 MHz, fixed by the harness |
| Console rate | 76,800 baud: BAUDDIV = 50,000,000 / 76,800 = 651 |

The harness reads the console every few milliseconds and holds 16 bytes in
between, so the program sends about one character every 2 ms.

## Licence

Apache-2.0, as the rest of this repository.
