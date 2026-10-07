# nanosoc_upy XiP bootrom — `NANOSOC_SYS_CLK_FREQ_HZ` undeclared (diagnosed 2026-07-24)

**Status: root-caused, NOT fixed. The obvious one-line fix is a SILENT-FAILURE
TRAP — do not apply it.** Off the critical path for the virtual-PHY demo
(nanosoc_upy is the MicroPython-flash-boot RM, unrelated to VPHY/GENCHK, which
live in the static shell). This blocked the *full* 9-overlay re-mint; the demo
re-mint proceeds with the 8 other RMs.

## Symptom

`make -C fpga/dfx rm-nanosoc-upy-dcp` (STAGE 2 of the re-key) fails compiling the
stage-0 bootrom from the pinned nanosoc snapshot:

```
.../nanosoc_snapshot/firmware/bootloader/stage0/stage0_bootloader.c:141:29:
  error: 'NANOSOC_SYS_CLK_FREQ_HZ' undeclared (first use in this function)
  141 | CMSDK_UART2->BAUDDIV = (NANOSOC_SYS_CLK_FREQ_HZ / 38400);
```

## Root cause

`stage0_bootloader.c` pulls in `nanosoc_memmap.h` (via `CMSDK_CM0.h`) expecting it
to define `NANOSOC_SYS_CLK_FREQ_HZ` — "baud divisor from **generated config**".
That macro lives in the **FPGA-generated** config
`imp/fpga/fw_config/nanosoc_memmap.h`. The pinned snapshot
(`fpga/rp/nanosoc/pin_nanosoc_snapshot.sh`) materialises committed-HEAD source but
does **not** produce `imp/fpga/fw_config/` — that directory is absent from the
snapshot. The include then resolves to a copy that lacks the unprefixed macro
(the source migrated toward prefixed `NANOSOC_MULTICORE_SOC_*` names). Hence
`undeclared`, not `file not found`.

## ⚠️ Why the obvious fix is WRONG

Two `nanosoc_memmap.h` copies in the snapshot DO define the macro, so adding one
of them to `build_bootrom.sh`'s `-I` path makes it **compile** — but with the
WRONG clock, which is far worse than a build break because it fails silently on
hardware (2×/4× baud error, reads as a wiring fault — see memory
`nanosoc-micropython-soc`).

| header | `NANOSOC_SYS_CLK_FREQ_HZ` | correct for this shell? |
|---|---|---|
| `build_soc/firmware/nanosoc_memmap.h` | **100 MHz** (generic/ASIC) | ✗ 2× too fast |
| fallback `imp/fpga/fw_config/…` (nanosoc_m0_soc, Jul-6) | **25 MHz** (stale FPGA override) | ✗ 4× too slow |
| **required** | **50 MHz** | ✓ |

**Ground truth = 50 MHz**, established two independent ways:
- `fpga/shell/bd/shell_bd.tcl:246` — `clk_wiz_dut` `CLKOUT1_REQUESTED_OUT_FREQ {50.000}`.
- The last WORKING nanosoc_upy XiP build (`fpga/dfx/build_xip`, 2026-07-19, which
  booted MicroPython) synthesised with `UART_CLK_HZ bound to: 50000000` /
  `CLK_HZ bound to: 50000000`.

`build_bootrom.sh` (repo-local, `tests/micropython_flash_boot/tools/`) verified:
`-I…/build_soc/firmware` → exit 0 but bakes 100 MHz. So it must NOT be the fix.

## Correct fix (needs a decision — the project lead / nanosoc-flow owner)

Make `imp/fpga/fw_config/nanosoc_memmap.h` (or whatever `build_bootrom.sh` finds
first) carry **`NANOSOC_SYS_CLK_FREQ_HZ = 50000000UL`** for this shell. Options:

1. **Regenerate the FPGA fw_config in the snapshot flow** so the pinned snapshot
   contains `imp/fpga/fw_config/nanosoc_memmap.h` at the real dut_clk. This is the
   proper fix — it is a generated file and the 2026-07-19 build proves it CAN be
   generated correctly. Requires the nanosoc FPGA-config generator (external tree).
2. **Deliberate `-D` in `build_bootrom.sh`**: pass
   `-DNANOSOC_SYS_CLK_FREQ_HZ=50000000UL`, commented as tied to
   `clk_wiz_dut` CLKOUT1. Repo-local and immediate, BUT bakes a constant into a
   shared script and `dut_clk` is **DRP-reconfigurable** — if firmware retunes the
   DUT clock at runtime the baked baud drifts. Acceptable only as an interim,
   with the clock coupling written down.

NOT applied here because (1) is external-tree/owner territory and (2) is a design
decision about a reconfigurable clock that should be blessed, not guessed.

## Impact / scope

- The demo re-mint excludes nanosoc_upy and is unaffected.
- At the time of writing, the then-fielded `0xD84A2E7A` shell + its nanosoc_upy
  overlay were intact and still booted MicroPython from flash — no deployed
  capability was lost. (That shell has since been superseded twice; the overlay was
  re-keyed in `3c9703d`. See `docs/FIELDED_SHELL.md` for what is on the board now.)
- Re-key nanosoc_upy into the new vPHY static once the fw_config clock is
  correct, via the incremental add-RM path (`DFX_ADD_RMS` / `make add-rm-…`),
  which preserves the minted static_id (no second full re-mint).
