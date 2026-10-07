# SoCScope on the platform

SoCScope is the trace plane. It lives in its own repo (the sibling `../SoCScope`
checkout, `SOCSCOPE_HOME`) with its own benches, mutation gate and contract; its plan
for this board is that repo's `docs/MPS3_BRINGUP_TESTS.md`. This page is the platform
side only: how SoCScope becomes an RM, how its bytes reach a host, how the build pins
which SoCScope it was, and where the nanoSoC-probing variant (B2) stands.

## The RM: `rm_socscope`

`fpga/dfx/rms/rm_socscope/rm_socscope.sv`, `rm_id 0x01000006` (design_id `0x0006`,
`fpga/dfx/rm_list.tcl`). It carries the frozen 35-port RP boundary unchanged, so it is a
partial build against the locked static -- no re-key, every other overlay stays valid.

- **Sources are not vendored.** `rm_socscope/filelist.tcl` reads SoCScope's own
  `hw/rtl/socscope_trace_top.f` and `socscope_selftest.f` from `SOCSCOPE_HOME`
  (default: the sibling checkout). Nothing SoCScope-shaped is copied into this repo.
- **Configuration**: `socscope_trace_top #(DEPTH=64, TICKDIV=64, CSR_BASE=0)`, with
  the two Stage-C choices exposed as RM parameters:

  | Parameter | Ships | What the other value builds |
  |---|---|---|
  | `TRACE_CLK_FREE` | `1` | `0` = the B1 single-clock tie (`trace_clk = dut_clk`). The C2 negative control, and the back-out |
  | `FREEZE_IN_RM` | `0` | `1` = the HW-013 "enable crosses on a partition pin" option: engine + control surface in the RM, `dut_clk_en` out on `dut_gpio_o[4]` |
  | `STEP_N` | `1` | cycles a switch-issued STEP advances by (a 32-bit N does not fit through three DIP switches) |
  | `FREEZE_WDOG_CYCLES` | `64_000_000` | the watchdog, in trace clocks. A parameter so the safety argument can be exercised in a bench instead of by waiting 1.3 s |
  | `EGRESS_DIVISOR` | `24` | must equal the shell's `UART_OVER_ETH_SWO_DIVISOR`; moved only by the bench |
  | `SELFTEST_GAP_CYCLES` | `1_000_000` | HW-007's pacing expressed as silence; moved only by the bench |

  Every default is the fielded value, and at `TRACE_CLK_FREE=0, FREEZE_IN_RM=0` the RM
  drives `dut_gpio_o/oe` bit-for-bit as the fielded overlay does -- a back-out is a
  parameter, not a diff.
- **Traffic**: `socscope_selftest` configures the block over its own AHB-Lite CSR port
  and drives deterministic synthetic AHB past the probe, one transfer in four outside
  the filter. The host recomputes what it should receive (SoCScope's
  `hw/bench/check_selftest.py`, the same function for bench and board).
- **Egress**: `socscope_egress_serial` at `DIVISOR=24`, which must equal the shell
  firmware's `UART_OVER_ETH_SWO_DIVISOR`. Off by one and every byte is garbage.
- **Build**: `make -C fpga/dfx add-rm-socscope BUILD=<locked tree> && make -C fpga/dfx
  overlays && make -C fpga/dfx verify`. They no longer have to be run back to back: the
  revision is recorded at synth time and `overlays` reads that record (see Provenance).

## The trace clock -- and the shell half that has not landed

`trace_clk` is `phy_rmii_ref_clk`, the shell's free-running 50 MHz RMII reference. It is
already an RP input in the frozen boundary, so the split costs **no boundary change, no
partition pin and no re-key**. The crossings `socscope_trace_top` has always
instantiated (`socscope_cdc_fifo` for records, `socscope_cdc_count` gray-coded for
`bytes_sent`) now cross a real boundary instead of degenerating to two cycles of latency.

**Why:** C2. Anything that stops `dut_clk` used to stop the transmitter, so a freeze
killed the link mid-frame. With the split, bytes captured before a freeze can still leave
during one -- measured, and measured against its own control, in `tests/rm_socscope`.

**The half that is NOT done, and it is a live regression, not a to-do.** The shell's
receiver is still in the DUT domain: `fpga/shell/ip/uart_bridge/swo_uart_rx.sv`
2-FF-synchronises `swo_i` into `clk_i` = `dut_clk` and counts its bit period in DUT
clocks, while this RM now counts its own in TRACE clocks. **They agree only while
`dut_clk` is at its 50 MHz default.** `dut_clk` is DRP-reconfigurable and this platform
does retune it -- `HW-008` records a `set_clk` to 25 MHz on this very board -- and a
retune used to move both ends together. Moving `swo_uart_rx` onto `phy_rmii_ref_clk` is
the shell half of the Stage-C batch (MPS3_BRINGUP_TESTS.md C1's `trace_clk` row). Until
it lands: **do not retune `dut_clk` while capturing.**

## Freeze: the engine is static-side, and here is the measurement that says so

`HW-013` established that no clock buffer can be placed in this RP, so the gate the
enable drives lives static-side either way; what stayed open was whether the *engine*
crosses on a partition pin or moves static-side with the shell driving it, "both costing
the same re-mint". `FREEZE_IN_RM=1` builds the pin option so the question could be
measured rather than argued, and the bench answers it:

> `socscope_freeze` counts **delivered enable cycles on `clk_free`**. In the in-RM option
> `clk_free` is `phy_rmii_ref_clk` -- *not* the clock being gated. "Step N" is then a
> window of N **trace** periods, and the number of `dut_clk` edges inside it equals N
> only while the two clocks run at the same rate. Bench: at 50 MHz a STEP of 1000
> delivers exactly 1000 cycles; with `dut_clk` retuned to 25 MHz the same STEP delivers
> **500**. C1's acceptance test ("step 1,000,000 and require exactly 1,000,000") is not
> satisfiable by the in-RM option on a board whose DUT clock can be retuned.

Static-side the engine runs on the ungated source of the clock it gates and the question
does not arise. **That is the recommendation, and `FREEZE_IN_RM=0` is the default.**

The cost, for the record, is not what decides it: OOC synthesis of both arms
(xcku115-flvb1760-1-c, Vivado 2024.1) gives 2666 LUT / 6744 FF at `FREEZE_IN_RM=0` and
2799 / 6826 at `1` -- **+133 LUT, +82 FF**, which is nothing. The in-RM option is not
rejected for area; it is rejected because it cannot deliver exactly N.

What the RM still contributes at `FREEZE_IN_RM=1`, and what to reuse if the pin option is
ever taken: the exported bit is `dut_gpio_o[4]` = **HOLD**, not enable. The DFX decoupler
clamps `dut_gpio_o` to 0 during a swap, so the clamped value must mean *run* -- exporting
the enable directly would stop the DUT's clock for the duration of every partial
reconfiguration, including the one replacing this RM. The command path is the DIP
switches (`dut_gpio_i[10:8]`), because `dut_gpio_i` is fully allocated
(`docs/contracts/dut-display-tunnel.md` §6) and there is no host-driven path in; the
commands are generated in the **trace** domain so a release still works while the DUT is
stopped (HW-009), and `socscope_freeze_ctl`'s watchdog is the safety argument.

## How `swo` leaves the RP, and what the shell does with it

`swo` (1 bit) leaves the RP through the DFX decoupler (`fpga/shell/bd/shell_bd.tcl`,
decoupled value 0) as BD port `rp_swo`, into `uart_bridge_0.swo_i`. Inside
`fpga/shell/ip/uart_bridge/`, `swo_uart_rx.sv` synchronises it (2-FF) into the
`dut_clk` domain and deserialises it as UART/NRZ, bit period `divisor+1` `dut_clk`
cycles; the bytes land in a per-stream async FIFO of `FIFO_DEPTH=16`, which
`firmware/uart_over_eth/uart_over_eth.c` polls and relays rx-only on TCP `6932`
(`MPS3_PORT_SWO`, `docs/contracts/net-protocol.md`).

**HW-007 is the shell's limit, not the link's.** The FIFO holds 16 bytes and a socscope
frame is exactly 16 bytes, so two back-to-back frames cannot fit at any baud rate. The
symptom is whole-frame loss, never corruption (0 bad CRC, many malformed), measured at
6.76 KB/s against a 250 KB/s wire. SoCScope's self-test therefore paces every record
10 ms apart. A deeper FIFO is a static change and waits for the batched Stage C mint.

## Provenance: `socscope_rev`

Because the RTL is read from a checkout at synth time, the manifest is the only record
of which SoCScope a built overlay carries. `fpga/dfx/overlay/<rm>/manifest.json` has an
optional `socscope_rev` (schema stays 1): `<sha>`, `<sha>-dirty`, or `unknown`.

- **The rev is captured at SYNTH time, by the process that read the files.**
  `rms/rm_socscope/ooc_synth.tcl` writes `rm_socscope_provenance.json` beside the
  checkpoint: the SoCScope revision, a `socscope_dirty` flag, and a **sha256 per source
  file** -- because `git status` is a statement about a tree and the digests are a
  statement about the bytes that were synthesised. `make overlays` copies that file next
  to the manifest it stamps and takes the rev from it.

  It used to be read from `git -C $SOCSCOPE_HOME rev-parse HEAD` **at `overlays` time**,
  with "run `add-rm-socscope` and `overlays` back to back" as the mitigation. That is a
  procedure, not a check, and it had already failed once -- see "What the shipped overlay
  was really built from" below.
- `rm_nanosoc` is **not** stamped, even though its B2 variant consumes SoCScope: the
  `SOCSCOPE=1` flag reaches only `fpga/rp/nanosoc/ooc_synth.tcl`, and the staged `.dcp`
  carries no marker into `overlay_inputs.txt`, so the recipe cannot tell a B2 nanosoc
  from the shipped one. A hand-recorded field on any manifest is validated all the same.
- The gate, `scripts/harness_gates/check_socscope_overlay_rev.py` (`make check` and
  `make check-ci`, stage 2): a SoCScope-consuming overlay without the field FAILS;
  `-dirty` FAILS (unreproducible, never waivable); `unknown` FAILS unless the overlay is
  in `scripts/harness_gates/socscope_rev_waivers.txt` with a reason; a sha must resolve
  in the checkout (`git cat-file -t`); a sha older than HEAD is a NOTE, not a failure --
  the overlay predates the checkout, which is the normal state before a rebuild. With no
  checkout (a hosted CI clone) the structural checks still run and the gate says what it
  skipped; `--require-checkout` makes that a failure on a build host.
- The gate also holds the manifest **to** the synth-time record when one is present:
  `socscope_dirty: true` FAILS (unlike the manifest's own `-dirty`, this one cannot be
  hidden by re-stamping later), and a rev that disagrees with the record FAILS.
  `--require-provenance` makes a missing record fatal -- the burn-down lever once every
  consumer overlay has been rebuilt through `ooc_synth.tcl` once.

### The overlay in the tree today

Rebuilt 2026-09-14 through `add-rm-socscope` against the **fielded** locked static
(`fielded/0xA8C1C535/static_routed_locked.dcp`), so `static_id` is unchanged and every
other overlay stays valid; `pr_verify` reports the routed config **compatible** with the
greybox reference. partial 1,988,968 B `crc32 0x01ef2399`, clearing 146,056 B
`crc32 0xab45c8ae`, `socscope_rev 2edd05a2b209`, and a `provenance.json` beside the
manifest.

**It has never been on silicon.** It carries the trace-clock split, so it is the first
socscope overlay whose egress is not on `dut_clk` -- see the shell half above before
capturing with a retuned DUT clock. The bits it replaced (2026-08-10, the ones that
produced the HW-007/HW-010 board captures) are still in `fpga/dfx/build_mint/prod/` as
`config_rm_socscope_pblock_rp_dut_partial{,_clear}.bin`, byte-identical to what
`overlay/socscope/` held.

### What the shipped overlay was really built from

The 2026-08-10 overlay recorded `unknown` and was waived as "unknowable". It is not
unknowable, and what it actually was is worse than unknown:

- all four copies of `rm_socscope_synth.dcp` in this repo (`rms/rm_socscope/build/`,
  `build_mint/prod/`, `build_qspi_kvm_jtag/{prod,rm_socscope_synth}/`) are **byte-identical**
  (md5 `46306b0a1a35a940087c1ce07fa88a27`), and the shipped `overlay/socscope/*.bin` are
  byte-identical to `build_mint/prod/config_rm_socscope_pblock_rp_dut_partial{,_clear}.bin`;
- the only OOC run that produced that checkpoint was `rms/rm_socscope/build/`, whose
  `ooc.log` ran 2026-08-06 23:17:49 -> 23:19:37. **A synth log for rm_socscope did
  exist**, which is what `fielded/0xA8C1C535/mint.json` says does not ("no synth.log for
  rm_socscope exists in any tree"). It showed the synthesis reading
  `hw/rtl/socscope_selftest.f` and elaborating `socscope_cfg`;
- **the artefact is dated BEFORE both commits that describe how it was built.** SoCScope's
  `9a734d5` ("one filelist for the self-test fabric"), which ADDS `socscope_selftest.f`,
  was committed at 23:28:53. This repo's `93d6815` ("rm_socscope partial rebuilt --
  TICKDIV=64, and one filelist for the fabric"), which teaches `filelist.tcl` to read that
  file *and* rewrites `overlay/socscope/manifest.json`, was committed at 23:33:45. The
  checkpoint is from 23:19:37, and `filelist.tcl` **errors out** when
  `socscope_selftest.f` is absent -- so at synth time the file existed on disk and in
  neither tree's HEAD.

So the tree was **DIRTY**: the honest record is `5d68932-dirty`, which the gate refuses
categorically and which may never be waived. The waiver's premise ("cannot be recovered")
was wrong and its conclusion (do not ship a rev) was right for a stronger reason. A
rebuild was the only fix, and the waiver is gone with it.

> The 2026-08-06 `ooc.log` itself no longer exists: `rms/rm_socscope/build/` is
> gitignored scratch and was overwritten when the reuse checkpoint was refreshed on
> 2026-09-14. The two commit timestamps above are in git and the conclusion does not
> rest on the log -- which is the other half of why the record now lives in a
> `provenance.json` that travels with the overlay instead of in a scratch log.

Which static the overlay is keyed to is said by its manifest and by
`docs/FIELDED_SHELL.md`, and by nothing else -- not this page, not the RM header.

## The bench

`tests/rm_socscope` (cocotb + Icarus, because that is what SoCScope's own benches and
mutation gate run under, and because the oracle is SoCScope's
`hw/bench/check_selftest.py::check_stream` -- the same function that reads a board
capture). Five elaborations, because both new features are parameters:

    source set_env.sh
    make -C tests/rm_socscope                 # every configuration
    make -C tests/rm_socscope CFG=freeze sim  # one

| CFG | Proves |
|---|---|
| `split` | the self-test stream still decodes through SoCScope's own oracle with the egress on the free-running clock; GPIO drive is bit-for-bit the fielded build's; `rm_id` + the inert boundary |
| `burst_split` | **C2**: stop `dut_clk` mid-capture (the bench plays the static-side gate) and whole CRC-good frames keep arriving |
| `burst_tied` | **C2's control** on `TRACE_CLK_FREE=0`: the same stop leaves **zero** `swo` transitions. Without this the split proves nothing |
| `freeze` | fail-safe HOLD polarity, freeze/run from the trace domain, STEP delivers exactly 1 measured on the gated clock, and the watchdog releases a freeze nobody released |
| `freeze_n` | STEP 1000 delivers exactly 1000 while the clocks match -- and **500** when `dut_clk` is retuned to 25 MHz, which is the finding above |

It does not run the DFX swap, does not model the decoupler, does not exercise the shell's
`swo_uart_rx`/16-byte FIFO (that is `tests/uart_bridge`, and HW-007 is a board
measurement), and asserts no timing.

**`SIM=vcs` does not work today, and the reason is in SoCScope, not here.** VCS refuses
to elaborate the instrument at all:

    Error-[ICPD] Illegal combination of drivers
    hw/rtl/socscope_framer.sv, 98: Variable "i" is driven by an invalid combination of
    procedural drivers. Variables written on left-hand of "always_comb" cannot be
    written to by any other processes.
    first driver socscope_framer.sv:147   second driver socscope_framer.sv:109

`integer i` is declared once at module scope and assigned from two separate processes;
that is legal for Icarus and illegal per the LRM's `always_comb` rule, which VCS
enforces. The fix is one line in SoCScope (declare the loop variable inside each block),
it is that repo's to make, and until it is made **this bench is Icarus-only** -- which is
also the simulator SoCScope's own benches and mutation gate run under, so nothing here is
blocked by it.

## B2: probing the nanoSoC AHB

`fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` has a `SOCSCOPE` parameter (default 0) whose
`g_socscope` generate block attaches `socscope_probe_ahb` to `exp_*`, the expansion AHB
master at `0x6000_0000` -- real CPU traffic, visible without touching the regenerated
`nanosoc.sv`. `socscope_cfg` configures the block from inside the RM, so its CSR sits on
no DUT bus. Stimulus is firmware (`fpga/rp/nanosoc/sw/socscope_exp/`) writing `ahb_clcd`
with a counter payload, paced under HW-007. Build variant:

    SOCSCOPE_HOME=<checkout> SOCSCOPE=1 IMEM_IMG=<socscope_exp.hex> \
        vivado -mode batch -source fpga/rp/nanosoc/ooc_synth.tcl

Measured 2026-08-07 (SoCScope's `build_b2`): **+2819 LUT, +7706 FF** over the plain RM
(13907/13912 against 11088/6206); the register jump is the 64 x 96-bit ring inferring
flip-flops (HW-006). **On the board: not started.**

## The blocker, plainly

`rm_nanosoc` does not build today, with or without `SOCSCOPE` (SoCScope's control run
proved the plain build fails the same way). The committed `nanosoc.sv` passes
`.CPU0_DBG_BASE` to a DAP whose YAML has moved to `NUM_AP`, and it fails on that
**parameter mismatch**. Regenerating `nanosoc.sv` is not a one-liner; it needs three
things settled first:

1. a **generator revision decision** -- regeneration also shifts the system register
   block at `0x4000D0B0` from `INIT_n_*` to `TGT_10_*` descriptors, which is
   firmware-visible and unrelated to the DAP migration;
2. `INST_PORT_PREFIXED` on the `DEBUG` and `SOC_PERIPHERAL` interfaces;
3. re-landing an **uppercase-port fix that exists in no commit** and that the committed
   RTL depends on.

This is a `nanosoc_m0_soc` / `nanosoc_arch_tech` checkout decision, not SoCScope's.
Source: SoCScope `docs/MPS3_BRINGUP_TESTS.md` lines 325-343.
