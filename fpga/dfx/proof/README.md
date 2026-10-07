# KU115 DFX proof — greybox ↔ LED (PROVEN 2026-07-04)

Proves the MPS3 platform's DFX (partial reconfiguration) machinery on the real
**xcku115-flvb1760-1-c**, end to end in Vivado 2024.1 — the KU115 analog of the
Z2 DFX proof. **No hardware, no MicroBlaze shell, no nanosoc DUT** needed: a
minimal real static shell + two trivial RMs exercise the full RP boundary.

## Result — PASS

- **`pr_verify`: greybox and LED configs are COMPATIBLE** — both RMs implement
  against a bit-identical static; partials are interchangeable.
- **Timing met**, WNS **+19.0 ns** (0 of 9 failing endpoints; 50 MHz OSCCLK).
- **HDPR DFX DRC clean** after the floorplan fix (see below).
- **Bitstreams generated** (in the run's out dir; sizes in
  `proof_results_2026-07-04/ARTIFACTS.txt`):
  - `config_greybox.bit/.bin` — 48.25 MB full KU115 boot image
  - `config_{greybox,led}_..._partial.bit/.bin` — 3.62 MB each (swap payloads)
  - `config_{greybox,led}_..._partial_clear.bit/.bin` — 240 KB each
    (**the UltraScale-mandatory clearing bitstreams** — the artefact the
    shell's clearing-cache firmware + overlay-manifest triple were designed for)

Evidence reports: `proof_results_2026-07-04/` (pr_verify, timing, util, DRC).
The `.bit/.bin` themselves are too large to commit — reproduce below (~15 min).

## The REAL SoC as a 3rd RM (PROVEN 2026-07-04) — `build_rm_nanosoc.tcl`

The greybox/LED proof de-risked the *mechanism*; this de-risks the *DUT*. The
real single-core Cortex-M0 nanoSoC (`fpga/rp/nanosoc/`, instantiating the
upstream `nanosoc` via the proven file set through `ooc_synth.tcl`) was dropped
into the SAME locked static as a third RM:

- `rm_nanosoc` OOC synth: **0 errors**, 7935 LUT / 4050 FF / 16.5 BRAM / 3 DSP
  (1.2% of the KU115 — tiny vs the RP Pblock). One critical warning = missing
  IMEM `image.hex` (empty BRAM; a *structural* DFX proof needs no firmware).
- config-3 (nanosoc) placed + routed against the locked static;
  **`pr_verify`: greybox vs nanosoc COMPATIBLE**; timing met **WNS +6.05 ns**
  (0 of 10,360 endpoints failing); **nanosoc partial (3.6 MB) + clearing
  (240 KB)** bitstreams emitted.

So a real Cortex-M0 SoC and a blinking LED are interchangeable partial
bitstreams on one bit-identical KU115 static shell. Reproduce:
`ooc_synth.tcl` (RM synth, needs the SOCLABS_NANOSOC_*/ARM_IP_LIBRARY_PATH/
FPGA_BOOTROM_DIR env) → `build_rm_nanosoc.tcl <proof_out> <rm_nanosoc.dcp> <out>`.

## What it is

- `rp_dut.sv` — the RP cell (port-only black box; partition-pins.md v0.1).
- `rp_shell_top.sv` — minimal real static shell: BUFG off OSCCLK[1], reset
  synchronizer, the RP instance, and observation logic folding every RP output
  onto the LEDs (so nothing is trimmed; LED[3:0] mirror the RM blink GPIO).
- `proof.xdc` — real verified KU115 pins (OSCCLK[1]=AK16, USER_nPB[0]=AT30,
  USER_nLED[7:0]); 50 MHz clock.
- `build_proof.tcl` — self-contained flow: synth static (RP black-box) + both
  RMs OOC → config1=greybox → lock static → config2=led → pr_verify →
  full+partial+clearing bitstreams. UltraScale deltas: clearing bitstreams,
  single-SLR SLICE-range Pblock, no RESET_AFTER_RECONFIG, PERSIST=NO.

## Reproduce

```sh
cd <repo_root>
OUT=/path/to/scratch; mkdir -p $OUT
vivado -mode batch -source fpga/dfx/proof/build_proof.tcl \
       -journal $OUT/proof.jou -log $OUT/proof.log \
       -tclargs $(pwd) $OUT
# success = "DFX_PROOF_COMPLETE" + "are compatible" in the log
```

## Two DRC iterations it took (recorded for the real DUT build)

1. **`BITSTREAM.CONFIG.PERSIST FALSE` → `NO`** — UltraScale expects `NO`/`YES`,
   not `FALSE` (Netlist 29-154). Fixed here and in `../build_dfx.tcl`.
2. **HDPR-6: static I/O inside the RP Pblock** — a whole-CLOCKREGION Pblock
   includes that region's IOB sites, so static LED/button buffers landed
   "inside" the partition. Fix (in `../dfx_floorplan.xdc`, benefits the real
   flow too): build the Pblock from **SLICE/DSP/BRAM site ranges only**
   (excludes IOB), over interior SLR0 columns X2-X3 (away from the config and
   I/O columns). This is exactly the Z2 probe's approach.

Both are now baked into the scripts, so the real `rm_nanosoc` DFX build inherits
the fixes.
