# Verification confidence — audit + risk register

> **Status: HISTORICAL** — a record of a four-audit verification-confidence risk register taken on 2026-07-08 as of 2026-07-08.
> Superseded by / current state in [docs/HARNESS_REGRESSION.md](HARNESS_REGRESSION.md) and [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

**Date:** 2026-07-08. **Method:** four independent read-only audits (RTL,
firmware, host/cross-layer, platform-risk) over the whole tree, cross-checked
against the built-shell reports and the frozen contracts. This is the
strategic view: *where a real hardware bug could hide behind green tests*, not
a test census.

## 0. The framing fact — two divergent shell lineages

The silicon-proven shell is **not** the feature-complete one:

| | Silicon-proven | Feature-complete (board-free) |
|---|---|---|
| `static_id` | `0x394227AF` | `0xECCEDBF3` |
| Clocks | ~50 MHz shell / 25 MHz LAN | 100 MHz shell / 50 MHz RMII |
| Contents | leaner shell | + VPHY/RMII, MMCM-DRP clock, decoupler boundary, uart_bridge |
| Proof | **real HW**: config, ping, TCP-6900, **JTAG greybox→led→nanosoc swap** | **Vivado only** (pr_verify COMPATIBLE, timing met); **0% silicon** |

Consequence: the hardware proofs (ping, JTAG partial-reconfig) **do not
transfer** to the design that actually contains the MAC-verification
subsystem, the DRP clock, and the per-signal decoupler. The richest, newest
surface is entirely unexercised on silicon, **and the production reconfig path
(MicroBlaze→AXI-HWICAP→ICAPE3) has never run on hardware** — only JTAG has.

## 1. Cross-cutting themes (all four audits agree)

1. **No assertion-based verification, no randomization, no coverage.** A
   whole-repo sweep for `assert property`/`cover`/`covergroup` and for
   constrained-random is **empty**. Verification is 100% directed vectors at
   one clock ratio, with no coverage metric to reveal the holes.
2. **Fail-OPEN / fail-STUCK branches that nothing exercises.** A stalled ICAP
   write is silently swallowed (returns as if written); the swap FSM parks
   forever if the decoupler never confirms (timeout is a TODO). These are the
   dangerous class — they pass every test and corrupt/hang on silicon.
3. **Model/mock fidelity gaps hide whole fault classes.** `fake_qspi_flash` is
   instant+atomic (torn-erase, flash-busy-timeout unmodelable); the fakeshell
   diverges from the firmware and is never byte-compared; RTL benches never
   model metastability.
4. **No cross-layer conformance automation.** Register constants exist as 4
   hand-transcriptions; the fakeshell is prose-validated; GENCHK counter
   semantics disagree 4 ways; the "3-way" swap cross-check is ~1.5-way (shared
   model). A contract can drift with every test still green.

## 2. Risk register (ranked; owner; board-free?)

Owner: **V** = my verification lane (ethernet/RP RTL, firmware, host, tests,
contracts) · **S** = shell-BD / hardware track (coordinate with the parallel
agent) · **HW** = only silicon closes it.

| # | Risk | Owner | Board-free fix | Status |
|---|---|---|---|---|
| **R1** | **DFX decoupler clamps *nothing*** — instantiated with no boundary interfaces; all 19 RP→static bits wire straight through; the decouple/release "confirms" are vacuous. A mid-swap RP can inject garbage into the MAC/console/MDIO. `pr_verify` can't see it; the JTAG proof had no decoupler at all. | **S** | Author the 8 PG294 interfaces per `DFX_DECOUPLER_BOUNDARY.md` §4 + a cocotb bench asserting the static side sees safe idle under DECOUPLE. | **flag → shell track** |
| **R2** | UltraScale clearing→partial→**GSR proven only on JTAG, never the ICAP path**. | V(partial)+HW | Add the bounded `SR_DONE`/EOS poll + timeout→FAILED (firmware); parse the emitted `.bin` DESYNC/sync framing. GSR itself = HW. | **wave B** (firmware poll) |
| **R3** | **ICAP `.bin` byte-lane bug** — 2 of 3 HWICAP writers use native-endian `memcpy`, **wrong on the little-endian MicroBlaze** → ICAP never syncs. End-to-end never confirmed. | V | Unify all writers on one MSB-first primitive; delete native-memcpy; add a byte-exact WF-order test + a `.bin` structural validator. | **wave B** |
| **R4** | **CDC: zero `ASYNC_REG` in the shell** + `report_methodology` TIMING-9/10 warnings; suspect crossings (phy_id[31:0] as 32 independent 2-FF; `dut_gpio_i` no static-side sync; RMII TX single-flop). Passes sim+pr_verify+timing, fails on silicon. | **S**+V | `report_cdc`→0 + `ASYNC_REG` on every synchronizer (shell); fix phy_id/dut_gpio/rmii crossings (RTL). SVA CDC assertions (V, board-free) catch the class. | **wave C** (SVA) + **flag** (constraints) |
| **R5** | **nanosoc RM never OOC-signed-off** (soc_glue helpers missing); async-group masks the dut↔shell relation; DRP retune re-opens un-signed RM timing; `_rm.xdc` not yet wired into `build_dfx.tcl`. | **S** | Provide soc_glue + run nanosoc OOC `report_timing`; wire `_rm.xdc` per `partition-timing.md`; freeze `dut_clk` (D12). | **flag → dfx track** |
| **R6** | **eth_ss ÷2 MII clocks fabric-routed (non-BUFG)**, fanout-196, hold margin +0.024 ns — "met" but PVT-fragile; Vivado won't treat them as clocks (HIGH warnings). | V/S | Move `mtx/mrx_clk` onto BUFG/BUFGCE (or clock-enables), re-run impl, confirm warnings clear + margin widens. | **flag** (needs eth_ss re-synth; coordinate) |
| **R7** | Reset LUTAR-1 ×5 (LUTs drive async resets — glitch hazard); fail-swap "safe state" isolation is nominal (see R1); `rp_irq_out` dangling. | **S** | Drive async resets from FF outputs; sink `rp_irq_out`; ships with R1. | **flag → shell track** |
| **R8** | LAN9220 SMC bus **false-pathed (never timed)**; EMC bank AC values README-derived, datasheet AC unverified. | V(numbers)+HW | Reconcile EMC fields vs the LAN9220 datasheet + legacy proven values; add BYTE_TEST self-check. | **flag** (number-check) |
| **R9** | **static_id provenance drift** — silicon `0x394227AF`, board-free `0xECCEDBF3`; partials/firmware-override/tests keyed to different shells. | V+S | One static_id per release: regen manifests + `mps3_shell_static_id.c` + rekey host tests from one locked static; CI-assert override==manifest==test. (Host tests already manifest-derived.) | **flag → coordinate** |
| **R10** | SWD `d/e/f/g`,`r/s/t/u` bit-order/turnaround vs host `bitbang.c`; DUT-egress architecture undecided. | HW / design | SWD: only OpenOCD-on-silicon closes it. Egress: a design decision. | **HW / design** |
| **F1** | **lwIP/MicroBlaze backend never run** — `net_if_lwip.c`+`main.c` (800 L of pbuf-lifetime code) compiled-to-ELF only; the fake it "must match" is never compared. | V | Loopback conformance harness: link `net_if_lwip.c` against a mock-lwIP + diff byte counts/CLOSED semantics vs `fake_net_if.c`. | **flag** (larger; next wave) |
| **F2** | Fail-STUCK swap FSM (R2/R7 firmware half); TFTP×large-payload untested; peripheral timeout branches all dead (fakes complete instantly). | V | Never-confirm timeouts; TFTP-large tests; fault-injection modes in the fakes. | **wave B** |
| **H1** | Fakeshell never byte-compared to firmware (already diverges: `verified`/`locked` on failure); GENCHK 4-way disagreement; regmap 4-way hand-transcription; swap cross-check shares one model. | V | Firmware↔fakeshell golden conformance; automated regmap-constant conformance; GENCHK reconciliation. | **wave A** |
| **H2** | Host pre-flight validator error branches untested (`overlay.py`, `swap.py` NAK + silent static_id-check bypass, `console.py` scraper). | V | Error-branch tests. | **wave A** |

## 3. What's being done now (board-free, my lane)

- **Wave A (host + contract):** firmware↔fakeshell golden-response conformance
  for all 8 verbs incl. failure shapes; automated regmap-constant conformance;
  GENCHK 4-way reconciliation; `overlay.py`/`swap.py`/`console.py` error
  branches. → H1, H2, part of R9.
- **Wave B (firmware safety+fidelity):** ICAP fail-open→FAILED + EOS timeout;
  byte-lane writer unification (R3); FSM never-confirm timeouts; `fake_qspi`
  busy/torn-erase fidelity; TFTP×large; peripheral fault injection. → R2(fw),
  R3, F2.
- **Wave C (RTL):** SVA (AXI-protocol + CDC) via `bind`; VCS coverage
  collection; WSTRB per-slave tests; the unverified features (clkrst DRP,
  dfx_ctl shutdown handshake, mdio write) + unbenched blocks (`uart_axis_shim`,
  `eth_ss_bringup`). → the no-assertions/no-coverage backbone + R4 (SVA half).

## 4. For the shell / hardware track (coordinate)

The **highest-severity platform risk is R1 (the decoupler clamps nothing)** —
it lives in the shell BD, which the hardware track owns. R4 (`ASYNC_REG` +
CDC constraints), R5 (nanosoc timing sign-off + `_rm.xdc` into `build_dfx`),
R6 (BUFG the eth_ss clocks), R7 (reset glitch), R8 (LAN9220 AC numbers), and
R9 (one static_id per release) are all board-free but touch the shell
build / DFX flow / hardware lineage. **The single most valuable board-free
move overall is authoring + benching the DFX decoupler boundary (R1).**

Residual **hardware-only** items (no board-free closure): the physical GSR
pulse (R2), the AXI byte-lane hop MB↔HWICAP (R3 tail), final LAN9220 AC
margin (R8), and SWD bit-order on silicon (R10).
