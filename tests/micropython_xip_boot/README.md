# micropython_xip_boot — hybrid-XiP MicroPython boot proof (product config)

The proof that MicroPython runs in the **product** flash config: a small,
ASIC-realistic IMEM holds only the **hot** set; the **cold** half — parser,
compiler, REPL, builtin modules, const tables — executes **in place from a real
SST26VF064B flash VIP** through the CG092 read cache. No board.

This is the honest counterpart to `tests/micropython_boot`, which bakes the whole
65 KB interpreter into a fat 128 KB BRAM IMEM (an FPGA-only cheat silicon cannot
reproduce). Here IMEM is sized to the hot set alone.

## What proves what

- **GATE 1 — boot + XiP up**: UART2 emits `NanoSoC MicroPython`. The hot set
  booted and `nanosoc_xip_bringup()` brought up the cached flash window.
- **GATE 2 — cold execute (the real proof)**: `print(1+1)` → `2`. The lexer,
  parser, compiler and REPL are cold — they live in flash. A correct `2` means
  cold code executed in place from the SST26 through the cache. `make UPY_GATE2=1`.

## How it is wired

- Full real `nanosoc` (same `collect_filelist.tcl` fileset as `micropython_boot`,
  symlinked), IMEM = the RAM_PRELOAD ROM loaded with the **hot** word-hex, sized
  `IMEM_RAM_ADDR_W` (default 32 KB — set per the firmware manifest).
- The real Microchip/SST **SST26VF064B VIP** on the SoC's `qspi_*` pads (per-lane
  tristate resolution, same as `tests/qspi_xip`), with the **cold** segment
  backdoored into its array at `COLD_FLASH_OFF` (= `__xip_start__ - 0x70000000`).
- The flash boot-table region below the cold offset is left **erased (0xFF)**, so
  stage-0 finds a chip, reads a bad boot-table magic, and cleanly falls back to
  the ADP/REMAP-to-IMEM path — i.e. the hot set runs, then MicroPython's own
  startup brings up XiP before any cold symbol is touched. (Stage-0 hybrid-boot
  rework is a separate item; this bench does not depend on it.)

## Inputs (from the firmware build — `firmware/micropython`, `make xip`)

Set to match `firmware/micropython/build/xip_manifest.txt`:

| Make var | Meaning |
|---|---|
| `HOT_IMEM_IMG` | hot (IMEM) word-hex, linked at 0x0 |
| `COLD_FLASH_IMG` | cold segment, byte-hex `$readmemh` into the VIP |
| `COLD_FLASH_OFF` | flash byte offset of the cold segment (`__xip_start__ - 0x70000000`) |
| `IMEM_RAM_ADDR_W` | hot IMEM size (byte-addr width; 15=32 KB, 14=16 KB) |

## Run

```sh
source ../../set_env.sh
make                # GATE 1 (banner + XiP up)
make UPY_GATE2=1    # + GATE 2 (cold-execute: print(1+1) -> 2)
```

Slow — booting a real interpreter with cold code fetched from flash is many
simulated ms. The test streams every decoded byte with sim-time and reports
exactly how far it got if the banner is not reached.

## Characterisation

Sweep `IMEM_RAM_ADDR_W` down (32 KB → 16 KB) to find the smallest IMEM that still
boots — that is the input to the product IMEM-size decision (D1). If a size is too
small, the hot set overflows IMEM at link time (a firmware-build error, not a sim
failure). `XIP_DISABLE` in the firmware folds everything resident as the control.
