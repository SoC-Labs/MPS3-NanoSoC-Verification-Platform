# Shell-BD / hardware-track confidence handoff

> **Status: HISTORICAL** — a record of a confidence handoff to the shell-BD / hardware track at commit `854a9cc` as of 2026-07-08.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `PLATFORM_LIVE_STATUS.md` — internal notes, not in the public tree.

**From:** the verification-confidence review (2026-07-08, commit `854a9cc`;
full cross-layer register in [`VERIFICATION_CONFIDENCE.md`](VERIFICATION_CONFIDENCE.md)).
**To:** the shell-BD / DFX-flow / hardware-bring-up track.
**Why:** a four-part read-only audit found the highest-severity design risks
live in *your* lane (the built shell BD, the DFX flow, the two shell
lineages). This is the actionable extract — each item has evidence, why it
bites, a **board-free** fix, and an acceptance check. Nothing here needs a
board except where marked **HW**.

---

## 0. Critical context — the two shell lineages

The silicon-proven shell is **not** the feature-complete one, so the HW proofs
do not cover the newest surface:

| | Silicon-proven | Feature-complete (board-free only) |
|---|---|---|
| `static_id` | `0x394227AF` | `0xECCEDBF3` |
| clocks | ~50 MHz shell / 25 MHz LAN | 100 MHz / 50 MHz RMII |
| contents | leaner | + VPHY/RMII, MMCM-DRP clock, **decoupler boundary**, uart_bridge |
| proof | real HW: config, ping, **JTAG greybox→led→nanosoc swap** | Vivado only (pr_verify COMPATIBLE, timing met); **0% silicon** |

**Two consequences that frame every item below:**
1. The MAC subsystem, DRP clock, and decoupler are on the `0xECCEDBF3` shell,
   which has **never been on hardware**.
2. The production reconfig engine (MicroBlaze→AXI-HWICAP→ICAPE3) has **never
   run on silicon** — only JTAG has. GSR/EOS behaviour is ICAP-path-specific.

---

## 1. Action items (ranked by hardware-bug-likelihood × board-free-detectability)

### R1 — DFX decoupler clamps *nothing* — **HIGHEST value, board-free**
**Evidence.** `dfx_decoupler_0` is instantiated with **no boundary interfaces
configured** (`fpga/shell/bd/shell_bd.tcl` ~608-627); it exposes only
`decouple`/`decouple_status`, and the status is combinational. Your own
`docs/DFX_DECOUPLER_BOUNDARY.md` §3 confirms *"not one partition-pin signal
passes through the decoupler today"* — all 19 RP→static output bits (tx_en,
uart tvalid/tready, mdc, mdio, swo, gpio_oe, irq) wire straight into the shell
CSR blocks. Firmware's `decouple_confirmed`/`release_confirmed` polls read
`DFXCTL.STATUS.DECOUPLED`, which is just the decouple bit fed back — **a
vacuous confirm**.
**Why it bites.** During an ICAP swap the RP fabric is transient garbage. With
no clamp, a mid-rewrite RP can inject a runaway frame into `link_partner_mac`
(stuck tx_en), flood/drain the console FIFOs (stuck uart tvalid/tready), clock
junk into the MDIO slave, or fight a board pad (gpio_oe). `rp_resetn` protects
static→RP inputs only, not RP→static outputs. **pr_verify cannot see this**
(it compares pin interfaces, not decoupler membership), and the JTAG HW proof
used a static with *no decoupler at all*.
**Fix (M, board-free).** Author the 8 PG294 interfaces already specified in
`DFX_DECOUPLER_BOUNDARY.md` §4; re-`validate_bd_design` + synth; confirm
(post-synth) the decoupler pin list carries the §3a members and no clock/reset.
**Acceptance.** A cocotb bench that asserts DECOUPLE and drives the RP-side
pins to garbage, checking the static side sees the safe idle values. (I can
draft this bench spec — just ask.) Residual GSR interaction is HW (see R2).

### R2 — clearing→partial→GSR proven only on JTAG, never the ICAP path
**Evidence.** Clearing bitstreams are real+paired; `RESET_AFTER_RECONFIG` is
dropped (UltraScale auto-GSR *contingent on the clearing running first*,
`fpga/dfx/README.md`). The clearing-first swap ran on silicon **over JTAG**;
the production MB→HWICAP→ICAPE3 path *"has never run on hardware"*
(`PLATFORM_LIVE_STATUS.md`). `fpga/dfx/README.md` I18 residue: GSR-on-silicon
and post-DESYNC EOS/status are open.
**Fix.** Firmware half is **done** (commit `854a9cc`): the EOS/SR_DONE wait now
times out → SWAP_FAILED instead of proceeding blind, and a stalled ICAP write
fails closed. **Board-free left for you:** parse each emitted `.bin` and assert
the trailing `CMD=DESYNC` + NOOP tail and leading sync framing (shares tooling
with R3). **HW-only residue:** the physical GSR pulse itself.

### R3 — ICAP `.bin` byte-lane — **firmware bug FIXED; your validation left**
**Evidence + status.** Two of three HWICAP writers used native-endian `memcpy`,
**wrong on the little-endian MicroBlaze** → ICAP would never see `AA995566`.
**Fixed in `854a9cc`** (one shared MSB-first primitive + a byte-exact guard
test). **Board-free left for you:** a CI check that parses a real emitted
`.bin` (validate the ASCII e-record header parse — never a fixed offset; sync
word at byte 80; length %4) so a bad packer is caught pre-hardware. **HW-only
residue:** the physical AXI byte-lane hop MB↔interconnect↔HWICAP.

### R4 — zero `ASYNC_REG` in the shell + open CDC methodology warnings
**Evidence.** `post_impl_timing_summary.rpt` reports **TIMING-9 Unknown CDC
Logic (1)**, **TIMING-10 Missing property on synchronizer (1)**, **LUTAR-1 (5)**.
`ASYNC_REG` appears exactly **once** in the whole repo — in the *monolithic*
top, not the shell. Every shell synchronizer relies solely on
`set_clock_groups -asynchronous`, leaving the placer free to spread the 2 flops
(reduced MTBF). Specific suspects: `phy_id[31:0]` crossed as 32 independent
per-bit 2-FF (torn value possible); `dut_gpio_i` re-enters `dut_clk` with no
static-side sync; the RMII TX re-register is a **single** flop (metastability
margin only while DUT shares the shell REF_CLK). *Correct* crossings: the
uart_bridge gray-pointer FIFOs, dfx_ctl rm_id 2-FF + 8-cycle settle, the 3
reset syncs. **My side is done:** SVA CDC assertions on the FIFOs pass (0
fired), so the *known-good* ones are now guarded.
**Fix (S–M, board-free).** Run `report_cdc` + `report_methodology` on the
routed shell, drive TIMING-9/10 to zero; add `ASYNC_REG=TRUE` to every
synchronizer first-stage. Convert `phy_id` to a qualifier-gated capture (or
accept-with-valid); add a static-side `dut_clk` sync for `dut_gpio_i`.
**Acceptance.** `report_cdc` clean; TIMING-9/10 = 0.

### R5 — nanosoc RM never OOC timing-signed-off; `_rm.xdc` not wired
**Evidence.** `fpga/rp/nanosoc/nanosoc_ooc.xdc` exists but the standalone OOC
synth **cannot run** — missing external `soc_glue_mux2.sv` /
`soc_glue_reset_sync.sv` (`XDC_VALIDATION.txt`). So nanosoc's internal timing
is analyzed only inside the full DFX link, where WNS is static-dominated. The
per-RM `_rm.xdc` mechanism (`docs/contracts/partition-timing.md`) is authored
but its `build_dfx.tcl` consumption is *deliberately not wired* (the
coordination handshake). And `dut_clk = 50 MHz` is a D12 placeholder — a DRP
retune upward re-opens RM-internal timing that was never signed off.
**Fix (M, board-free).** Provide the soc_glue helpers + run nanosoc OOC
`report_timing_summary` (recipe in `XDC_VALIDATION.txt`); wire the guarded
`read_xdc -cell $rp_inst <rm>_rm.xdc` into `build_dfx.tcl` per
`partition-timing.md` §"How build_dfx.tcl SHOULD consume"; freeze `dut_clk`.
**Note:** `build_dfx.tcl` is your file — this is the one place my socketed-XDC
work needs to meet your DFX flow.

### R6 — eth_ss ÷2 MII clocks are fabric-routed (non-BUFG), tight hold
**Evidence.** `mtx_clk`/`mrx_clk` are plain FFs used as clocks, **0 BUFG**
(`fpga/rp/eth_ss/XDC_VALIDATION.txt`). DFX impl: `check_timing` HIGH — *"196
register/latch pins with no clock driven by root clock pin …/mrx_clk_reg/Q"*
(and 190 for mtx); `mii_tx_clk` a routed net fo=196; worst **hold slacks
+0.024 / +0.029 / +0.042 ns** on that domain. "Met", but a divided clock on
local routing is PVT-sensitive and +0.024 ns hold can flip negative on silicon.
**Fix (S–M, board-free).** Move `mtx/mrx_clk` onto a `BUFGCE`/BUFG (or use
clock-enables — `link_partner_mac` already consumes them as enables); re-run
impl; confirm the HIGH warnings clear and hold margin widens. Touches
`fpga/rp/eth_ss` (mine originally) — happy to do it, but it re-synths eth_ss
which the DFX flow stages, so let's agree who lands it.

### R7 — reset glitch hazard + dangling RP net
**Evidence.** LUTAR-1 ×5 = combinational LUTs driving async resets (glitch on
assertion). On a failed swap firmware leaves the RP "decoupled + held in
reset" as the safe state — but per R1 "decoupled" is nominal, so real isolation
is only `rp_resetn`. `rp_irq_out` is created and wired to **nothing**
(`shell_bd.tcl` ~168). **Fix (S).** Drive async resets from FF outputs not
LUTs; give `rp_irq_out` a real sink. Ships with R1's decoupler authoring.

### R8 — LAN9220 SMC bus false-pathed; datasheet AC unverified
**Evidence.** The SMC bus is entirely false-pathed (`mps3_harness_timing.xdc`)
— the timing engine never checks it; the AC budget rests on `axi_emc_0` bank
timing set to *conservative README-derived values, primary AC table
unverified* (`fpga/shell/README.md`). This lineage pinged on silicon at 25 MHz
(the `0x394227AF` build), **not** the 100 MHz `0xECCEDBF3` shell.
**Fix (S, board-free number-check).** Reconcile the EMC bank fields against the
real LAN9220 datasheet AC table and the legacy arch_tech proven values; add the
`BYTE_TEST`/address-alignment self-check to firmware bring-up. **HW-only
residue:** final AC margin at 100 MHz.

### R9 — static_id provenance drift across the two lineages
**Evidence.** Silicon `0x394227AF` vs feature-complete `0xECCEDBF3`; partials,
the firmware `mps3_shell_static_id.c` override, and manifests are keyed per
build. The pusher correctly refuses on mismatch, but the fleet has artefacts
keyed to different shells. **My side is done:** the host E2E tests are now
manifest-derived (auto-track any rebuild). **Fix (S, board-free) for you:**
one locked static per release → regenerate manifests + `mps3_shell_static_id.c`
+ rekey from one source; CI-assert override == manifest == test constant.

---

## 2. Shell-IP RTL findings (the SVA/coverage pass surfaced these)

These are in the CSR blocks I authored but which feed *your* shell build, so
flagging rather than unilaterally changing:

1. **clkrst DRP master is tied idle** — `DUT_CLK_SEL`/`DUT_CLK_DRP` writes are
   stored but **never issue a DRP transaction**, so `set_clk` does not actually
   reconfigure the DUT clock (the MMCM_DRP @0x44AB block is not driven). The new
   `tests/clkrst` DRP bench pins `drp_den_o == 0`. Needs a real DRP FSM.
2. **dfx_ctl `axi_shutdown_ack_i` accepted but unconsumed** — no STATUS
   shutdown-idle bit, so firmware cannot poll shutdown completion (relevant to
   R1's confirm being vacuous).
3. **clkrst `dutclk_toggle_q` has no reset/init** — stays X in 4-state sim
   (`dut_clk_alive` dead in sim; works on FPGA via GSR). Recommend `= 1'b0`.
4. **UARTBR/TELEM `>=0x20` decode aliases** — blocks decode only `addr[4:2]`, so
   a UARTBR read ≥0x20 pops a console byte and a TELEM write ≥0x20 clears sticky
   `i2c_err`. Pinned as xfail footguns; widen the decode to close.

---

## 3. What's already covered (so you don't re-do it)

- **Firmware fail-closed:** ICAP stall, EOS timeout, decouple/release
  never-confirm all now → SWAP_FAILED (`854a9cc`).
- **ICAP endianness bug (R3):** fixed + guarded.
- **CDC on the known-good FIFOs:** SVA-asserted, 0 fired.
- **Host↔firmware conformance:** fakeshell byte-compared to the firmware;
  regmap constants machine-checked; GENCHK semantics reconciled.
- **static_id in host tests:** manifest-derived.

## 4. Priorities if you take only three

1. **R1 — author + bench the decoupler boundary.** Highest-value board-free
   move on the whole platform; the isolation the built shell is missing.
2. **R4 — `report_cdc`→0 + `ASYNC_REG` everywhere + fix phy_id/dut_gpio/rmii.**
   Kills the classic "passes sim+pr_verify+timing, fails on silicon" class.
3. **R3/R2 `.bin` parse in CI + R5 nanosoc OOC sign-off + wire `_rm.xdc`.**
   Shrinks the ICAP + RM-timing unknowns to just the physical GSR.

**Hardware-only residue (no board-free closure):** the physical GSR pulse (R2),
the AXI byte-lane hop (R3), final LAN9220 AC margin at 100 MHz (R8), and SWD
bit-order on silicon (R10, see the full register).

Full evidence + my-lane items: [`VERIFICATION_CONFIDENCE.md`](VERIFICATION_CONFIDENCE.md).
