# `fpga/dfx/rms/` — RM authoring contract + build/manifest pipeline

**Owner:** A2 (dfx-flow). This directory holds the Reconfigurable Modules
(RMs) that are *this agent's own* to author — the DFX-flow test fixtures
(`rm_greybox`, `rm_led`) that exist purely to prove the swap machinery works,
independent of any real DUT. `rm_nanosoc` and `rm_eth_ss` are **real DUTs**
and live under `fpga/rp/<name>/` instead (owned by other agents — A1/W1 for
nanosoc); `fpga/dfx/rm_list.tcl` is the one place that maps every RM's
`rm_name`, regardless of which directory its source actually lives in, into
the DFX build.

## Contents

```
fpga/dfx/rms/
├── README.md                       (this file)
├── rm_greybox/rm_greybox.sv        inert tie-off RM, rm_id=0x00000000
├── rm_led/rm_led.sv                blinking-counter RM, rm_id=0x0000001E
├── rm_regdemo_a, rm_regdemo_b      register-difference demo pair
├── rm_uart_echo/rm_uart_echo.sv    AXI-Stream console echo, rm_id=0x01000004
└── rm_socscope/                    SoCScope trace plane, rm_id=0x01000006
```

(The tree above listed only two entries for a long time while six directories
existed; `rm_list.tcl`'s `RM_ORDER` is the registry that is actually authoritative,
and `pin_check.py` derives its target set from it rather than from this file.)

## RM authoring contract

Every RM — wherever its source lives (`fpga/dfx/rms/<rm>/` or
`fpga/rp/<rm>/`) — must satisfy all of the following, because `build_dfx.tcl`
links every RM into the **same** black-boxed RP cell in the **same** locked
static checkpoint (`static_routed_locked.dcp`) and then `pr_verify`s the
result against the reference config. Any RM whose boundary doesn't match
byte-for-byte fails that link/verify, not gracefully — see the "known
mismatch" note below for what that looks like today.

1. **Module name == file name.** `rm_list.tcl`'s `top` field must equal both
   the `.sv` file's base name and the `module` keyword inside it (e.g.
   `rm_led/rm_led.sv` declares `module rm_led`). `ensure_rm_synth_dcp` in
   `build_dfx.tcl` derives the synthesis source path as
   `<repo_root>/<wrapper_dir>/<top>.sv` directly from these two rm_list.tcl
   fields — get them out of sync and the build looks for a file that isn't
   there.
2. **Ports are EXACTLY the RP side of `docs/contracts/partition-pins.md`**
   (currently v0.1), mirrored (every direction flipped, since the contract
   states directions from the shell's view — see
   `fpga/rp/nanosoc/README.md` "Directions — mirror image of the shell's
   view" for the line-by-line derivation this README's port list was cross-
   checked against). As of v0.1 that is, in table order:
   - Clocks & resets: `dut_clk`, `dut_resetn`, `rp_resetn`, `dbg_resetn` (all
     inputs — no clock generation inside an RM, ever).
   - Processor debug (SWD): `swd_clk`, `swd_dio_o`, `swd_dio_oe` in,
     `swd_dio_i` out.
   - Ethernet (RMII+MDIO): `phy_rmii_ref_clk`, `phy_rmii_crs_dv`,
     `phy_rmii_rxd[1:0]`, `mdio_i` in; `phy_rmii_txd[1:0]`,
     `phy_rmii_tx_en`, `mdc`, `mdio_o`, `mdio_oe` out.
   - Console/trace (AXI-Stream byte): `uart_tx_tdata[7:0]`,
     `uart_tx_tvalid` out, `uart_tx_tready` in; `uart_rx_tdata[7:0]`,
     `uart_rx_tvalid` in, `uart_rx_tready` out; `swo` out.
   - Status/misc: `rm_id[31:0]`, `dut_lockup`, `irq_out` — all outputs.
   - **Board-port / GPIO passthrough (I4)**: `dut_gpio_o[NGPIO-1:0]`,
     `dut_gpio_oe[NGPIO-1:0]` out; `dut_gpio_i[NGPIO-1:0]` in. `NGPIO` is a
     build parameter, v0 default **16** (`partition-pins.md` "Board-port /
     GPIO passthrough" table + `shell-regmap.md` GPIO block width) — expose
     it as a Verilog `parameter int NGPIO = 16` so a future width bump is a
     one-line override, not a file edit.
3. **No shell↔DUT AXI, no pin-facing (`IOB`) registers, no clock
   generation** inside an RM — all three are static-shell-only concerns
   (`partition-pins.md` "IOB packing note", "Clock/reset domain rule").
4. **`rm_id` is a real, permanently-driven 32-bit constant**, assigned in
   `rm_list.tcl` at RM-design time (not computed from the bitstream) and
   `assign`ed as a `localparam` inside the module — this is the shell
   coordinator's RM-load-verify ground truth
   (`shell-regmap.md` DFXCTL.RM_ID, `partition-pins.md` "Status/misc: rm_id").
   Never let two RMs share an `rm_id`.
5. **Everything the RM doesn't actually implement must be tied to a safe,
   legally-driven, inert constant** — not left floating, and not asserted
   without a real driving reason (see both `.sv` files here: every port not
   involved in the RM's "real" behaviour gets a plain `assign ... = <const>`
   at the bottom of the module).

### Known mismatch — RESOLVED 2026-07-04 (integrator pass, Phase-0.5 wave)

`fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` originally predated the I4 board-GPIO
group and lacked the `dut_gpio_*` ports; the integrator pass conformed it to
`partition-pins.md` v0.1, so all three wrappers (`rm_greybox`, `rm_led`,
`rp_nanosoc_wrapper`) now share an identical RP boundary and `rm_nanosoc`
passes `build_dfx.tcl`'s `dut_gpio_o` readiness filter. The filter stays in
place as the guard against *future* boundary drift (any new/edited wrapper
missing the current contract's ports is auto-excluded rather than fed as a
doomed config into `link_design`).

**Superseded same day (b3e8e99):** both caveats are now closed — the wrapper
instantiates the **real single-core nanosoc** (read-only `nanosoc_m0_soc`
checkout, via `uart_axis_shim` for the console) and drives the real
`rm_id = 0x0000_0001`, matching `rm_list.tcl`. OOC synth is clean (7,935
LUTs, the D7 datapoint) and DFX config-3 (nanosoc vs greybox static) passed
`pr_verify` with timing met — see `../proof/build_rm_nanosoc.tcl` and
`../proof/proof_results_2026-07-04/`.

## The RMs here

| RM | `rm_id` | Purpose |
|---|---|---|
| `rm_greybox` | `0x00000000` | Inert tie-off default (spec D14 "greybox + NVM"); ALSO the DFX *reference config* `static_routed_locked.dcp` is extracted from (`rm_list.tcl` RM_ORDER — must stay first). |
| `rm_socscope` | `0x01000006` | The **SoCScope trace plane**: `socscope_trace_top` (probe → record former → ring → domain FIFO → framer → egress, plus an AHB-Lite CSR) driven by `socscope_selftest`. It is the first RM to drive the `swo` partition pin, which has been fully plumbed — decoupler-clamped, FIFO'd, relayed on TCP 6932 — since the shell was minted while **every** RM tied it to zero. Because `swo` is already in the frozen 35-port boundary this costs a **partial build and no re-key**. Sources are NOT vendored: `filelist.tcl` reads them from `$SOCSCOPE_HOME`, as `rm_nanosoc`/`rm_eth_ss` do for their DUTs. Build: `make -C fpga/dfx add-rm-socscope BUILD=fpga/dfx/build_qspi_kvm_jtag`. Since 2026-09-14 it also carries the Stage-C parameters: `TRACE_CLK_FREE` (1 = egress on the free-running `phy_rmii_ref_clk`, so the link survives a stopped DUT clock; 0 = the B1 tie, which is C2's negative control) and `FREEZE_IN_RM` (0 = the state-plane engine is static-side, the replan's decision; 1 = the HW-013 partition-pin option, benched and NOT recommended -- it cannot deliver exactly N when `dut_clk` is retuned). Benched in `tests/rm_socscope` (cocotb + Icarus, five elaborations). |
| `rm_led` | `0x0000001E` | Free-running counter blinking a few `dut_gpio_o` bits; no SoC. The whole point is a **human-visible, on-hardware** proof that partial reconfig + `pr_verify` + the clearing/partial pipeline work, before `rm_nanosoc`'s much bigger real DUT is attempted. IMPLEMENTATION_PLAN.md Phase 1.1 acceptance: `pr_verify` clean, greybox↔`rm_led`; Phase 1.2: two tender-JTAG/XVC swaps, shell (LEDs/clocks) undisturbed. |

`rm_id` note: the task brief that originated `rm_led` spelled its id
`"0x0000_00LE"` as a mnemonic — but `L` is not a legal hex digit (0-9/A-F),
so a 32-bit register genuinely cannot hold it. Read as leetspeak (`L`→`1`)
it resolves to the real constant used above, `0x0000001E` ("1E"). Flagged
for A6 in case a different literal id was actually intended — trivial to
change in exactly two places (`rm_list.tcl` + the module's `RM_ID_LED`
`localparam`) since `rm_id` is a single source of truth per RM.

See `rm_greybox.sv`'s module header for the full **I19 greybox-generation
decision** writeup (hand-authored tie-off wrapper, not Vivado
`update_design -buffer_ports` — short version: `buffer_ports` is a *post*-
extraction operation that presupposes a *different* RM already served as the
reference config, which conflicts with greybox's OTHER required role here as
*the* reference config `static_routed_locked.dcp` is extracted from; a hand
wrapper also lets `rm_id` drive a real, checkable `0x00000000` instead of a
floating buffer).

## Build recipe

`build_dfx.tcl`'s `ensure_rm_synth_dcp` synthesizes either of these directly
(`read_verilog -sv` + `synth_design -mode out_of_context -top <rm>`, single
file, no dependencies, seconds to run) if no pre-built
`<out_dir>/<rm_key>_synth.dcp` already exists — no separate synth step is
needed to exercise the Phase 1.1/1.2 greybox↔led path. This is a convenience
appropriate for RMs this tiny; `rm_nanosoc`/`rm_eth_ss` (large, multi-file,
externally-sourced) are expected to use the pre-built-checkpoint path instead
once their real wrappers exist, to keep re-synth off `build_dfx.tcl`'s
per-invocation critical path.

Default invocation (empty/absent 4th arg) auto-selects every RM whose source
exists *and* passes the port-freshness heuristic above, plus any RM with a
pre-staged `<out_dir>/<rm_key>_synth.dcp` — with `make -C fpga/dfx
rm-nanosoc-dcp` run first that resolves to `{rm_greybox, rm_led,
rm_nanosoc}`; without it, `{rm_greybox, rm_led}` (see the Makefile /
`../README.md` "Build + overlay quickstart"):
```
vivado -mode batch -source fpga/dfx/build_dfx.tcl \
       -journal <out>/build.jou -log <out>/build.log \
       -tclargs <repo_root> <out_dir> <static_shell_dcp>
```
Or force the set explicitly (same result today, but future-proof once more
RMs are ready):
```
       -tclargs <repo_root> <out_dir> <static_shell_dcp> "rm_greybox,rm_led"
```

## Verifying without Vivado

Both `.sv` files here lint clean under plain `verilator --lint-only`
(no `-Wall`, exit 0, no warnings at all). Under `-Wall` they show only the
same class of `UNUSED`-port warnings the existing `fpga/rp/nanosoc/
rp_nanosoc_wrapper.sv` Phase-0 stub already shows (expected for a
mostly-tie-off wrapper whose real DUT logic — or, here, entire purpose — is
deliberately minimal):
```
verilator --lint-only fpga/dfx/rms/rm_greybox/rm_greybox.sv   # exit 0, silent
verilator --lint-only fpga/dfx/rms/rm_led/rm_led.sv            # exit 0, silent
```

`fpga/dfx/rm_list.tcl` and `fpga/dfx/build_dfx.tcl` are plain Tcl (no Vivado
built-ins reached until the `create_project`/`synth_design`/... calls
themselves) and can be sanity-checked with `tclsh` — e.g.
`source fpga/dfx/rm_list.tcl; rm_all_names` — or exercised end-to-end against
stubbed-out Vivado commands to validate the RM-selection/error-handling
control flow with zero Vivado license/hardware dependency.

## How `gen_manifest.py` turns built `.bin`s into the overlay triple

`gen_manifest.py` (this directory, real working Python, no Vivado needed) is
the last step after `build_dfx.tcl` emits, per RM,
`config_<rm>_<pblock>_partial.bit`/`.bin` and
`config_<rm>_<pblock>_partial_clear.bit`/`.bin` (see `fpga/dfx/README.md`
"Artefact set → overlay-manifest.md mapping"). It computes each file's
`crc32`/`len`, validates both halves of the `{clearing, partial}` pair exist
(refuses to emit a manifest for a lone one — the UltraScale-mandatory rule,
`overlay-manifest.md`), and writes `overlay/<rm_name>/manifest.json` in the
exact schema the host pusher (A4) and firmware (A3) expect. Worked example
for `rm_led` (renaming build_dfx.tcl's Vivado-emitted names to the contract's
bare `<rm>.bin`/`<rm>_clear.bin` is exactly what `--copy` does here):
```
python3 fpga/dfx/gen_manifest.py build \
    --rm-name led --rm-id 0x0000001E \
    --static-id-file <out_dir>/static_id.txt \
    --partial   <out_dir>/config_rm_led_pblock_rp_dut_partial.bin \
    --clearing  <out_dir>/config_rm_led_pblock_rp_dut_partial_clear.bin \
    --vivado 2024.1 --copy --out-root overlay

python3 fpga/dfx/gen_manifest.py verify overlay/led/manifest.json
```
This was smoke-tested (fake random `.bin` payloads, same sizes/CRCs
round-tripped correctly) while building this RM pair — `gen_manifest.py`
itself needed no changes.
