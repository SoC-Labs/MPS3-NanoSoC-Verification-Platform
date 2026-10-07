# ICAP DFX swap — debug plan (synthesis of 4 parallel analyses)

> **Status: HISTORICAL** — a record of the ICAP DFX swap debug campaign, while the failure was still open as of 2026-07-23.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `ICAP_SWAP_FAILURE_ANALYSIS.md` — internal notes, not in the public tree.

Synthesised from four independent debug-plan passes: **software-instrumentation**, **JTAG/hardware**,
**DFX/bitstream forensics**, and **campaign-sequencing/risk**. Companion to `ICAP_SWAP_FAILURE_ANALYSIS.md`
(the failure record + cleared static suspects).

## Converged root-cause model

The first real `mps3_dfx.ko mock=0` swap on `shell_linux_top` hung ~150 s and lost the **whole** FPGA config
(MBV Hart vanished; recoverable by reprogram; lease held → swap-caused).

Four lenses independently point to the **same** conclusion:

1. **The bitstream is exonerated by forensics.** A raw FAR (frame-address) decode of both partials shows the
   linux `led` partial writes **6556 frames confined to the SLR0 RP rows {0,1,31}, CLB cols 94-200, BRAM
   cols 6-11 (FAR 0x2f00-0x03be0000)** — the *identical* footprint to the silicon-proven classic partial
   (6679 frames, same ranges). **It touches zero DDR4/MBV static frames.** Command grammar
   (RCRC·SHUTDOWN·WCFG/MFW·GRESTORE·START·DESYNC×2), compression (`COMPRESS=TRUE`), CRC handling (RCRC,
   no PERFRAMECRC), and header/sync are all identical to the proven classic recipe. The only real deltas
   are the **Vivado tool version (2026.1 vs 2024.1)** and **MFWR/compression timing-sensitivity under a real
   HWICAP FIFO** (untested — the classic proof used the same compression but a different write path).
2. **`pr_verify COMPATIBLE` is passed-but-irrelevant.** It is a DCP-vs-DCP routed-boundary comparison of
   two artifacts from the *same* 2026.1 run against the *same* locked static — it cannot model the ICAP
   delivery path or a tool-version/silicon mismatch. Do not spend more cycles on it.
3. **The partial leads with `SHUTDOWN`.** So *any* correct partial whose stream is interrupted after
   SHUTDOWN reaches the config engine but before START/DESYNC completes leaves the device shut-down/desynced
   → total config loss, reprogram-only. **This is exactly the observed symptom** — and it is caused by a
   mid-stream **writer stall**, not a malformed frameset.
4. **The `mps3_dfx.ko` write path was never run on real silicon** (only the QEMU mock, which accepts writes
   instantly). Its waits are bounded in **iterations, not wall-time**: `done_poll_max=1,000,000` bare
   `readl()` spins ≈ the observed 150 s at slow in-fabric AXI latency; and `eos_capture` with the default
   `eos_strict=0` **returns success on timeout**, so it can silently burn 150 s *after* the partial is
   written, immediately before `finish()` releases the RP. The 30 s idle-reaper **cannot** rescue this — it
   needs `d->lock`, which the spinning swap thread holds.

**Leading hypothesis: a mid-stream stall in the `mps3_dfx.ko` HWICAP FIFO write (WFV-drain / CR-clear /
EOS spin) leaves the device SHUTDOWN → whole-config loss.** The bitstream and floorplan are sound.

## Priority order — cheapest & least-destructive first

### PHASE 0 — off-board, no board, do these FIRST (they may localize it with zero board risk)

- **P0.1 — Writer-equivalence audit (highest value).** Diff `mps3_icap_engine.c`'s word-packing + FIFO loop
  against the bare-metal firmware that proved the classic swap: `MPS3_ICAP_PACK_WORD` byte order, the
  `wait_wfv_nonzero` batch size vs vacancy, and the `lite_write_word`/`fifo_drain` StartConfig(CR.WRITE)+
  refill sequence. Confirm the `.ko` presents byte-for-byte the same HWICAP word stream the firmware did. A
  wrong pack or a CR/WFV-ordering divergence corrupts from the first frame → early stall → SHUTDOWN left
  hanging. This is the one path never exercised on silicon and is fully auditable off-board.
- **P0.2 — Vivado frame-subset check.** `open_checkpoint config_rm_led_routed.dcp` (or the locked static),
  enumerate `pblock_rp_dut`'s legal frame list, and confirm the 6556 stream FARs are a **subset**. Closes
  the loop the raw parser can only infer (no frame lies outside the RP for *this* locked static). One
  `open_checkpoint` + query, no board.
- **P0.3 — Independent bitstream parser cross-check.** Run both `.bin`s through a non-Vivado UltraScale FAR
  decoder to confirm no type-1/2 packet's declared word-count overruns a register boundary (a truncated
  packet = config engine waits forever = a hang class). Removes single-tool risk.

### PHASE 1 — one supervised board window: the DECISIVE isolation test

- **P1.1 — Load the SAME partial via the Vivado hardware manager / JTAG, bypassing `mps3_dfx` entirely.**
  With `shell_linux_top.bit` healthy, program `config_rm_led…partial.bit` through the JTAG config port
  (`program_hw_devices`), NOT the AXI-HWICAP. This is the single most decisive test:
  - **Partial loads over JTAG, static survives, `rm_id` reads 0x0100001E** → the partial is silicon-loadable
    on this static; **the fault is 100% the `mps3_dfx.ko` HWICAP write path** → fix off-board (P0.1), no
    more risky ICAP swaps needed to prove the diagnosis.
  - **Partial over JTAG ALSO kills the static** → the partial or the DFX floorplan is bad (a build/DCP
    problem `pr_verify` doesn't cover) → back to Phase 0 / regenerate.
  - Cross-check: also load `led_clear.bin` (62 KB) this way; and, if available, via the JTAG-ICAP path
    (exercises the ICAP frame engine but still bypasses the `.ko`) to narrow "ICAP" → "the `.ko` FIFO
    protocol."

  P1.1 is far less destructive than an instrumented `mock=0` swap (JTAG DFX is the vendor path) and can
  resolve SW-path-vs-artifact in one non-`.ko` operation. **Run it before any instrumented `mock=0` retry.**

### PHASE 2 — instrumented `mock=0` retry (only if P1.1 says "SW path is the suspect")

Prereqs (build once, off-board) BEFORE the board window:
- **Instrument the engine:** `dev_warn`/`dev_err` at every seam (swap_begin decouple, `wait_wfv_nonzero`,
  `wait_cr_write_clear`, the stream walkers, `eos_capture`, `engine_fail`, `finish` release/commit),
  logging `icap_bytes`, the last register value, `spins`, and state. Throttle in-spin (`1<<16`).
- **Add a read-only ICAP probe** ioctl/debugfs: `readl(HWICAP_SR/WFV/CR)`, `DFXCTL_STATUS`, `CLKRST_STATUS`
  — no write, cannot corrupt fabric (this is Rung 0; it does not exist today).
- **Time-bound the spins + shrink via module param:** reload with `done_poll_max=2000 confirm_poll_max=2000`
  so a stuck poll fails in ms with a logged errno instead of 150 s of silence. Size the floor from a
  healthy WFV latency measured on the clearing rung.
- **A `shell_linux_dbg` diagnostic bitstream** (model on `linux_soc` `build_dbg.tcl`/`read_diag.md`): source
  `shell_linux_bd.tcl` verbatim, add a VIO on the decouple/reset handshake (`decouple_en`, `decoupled`,
  `rp_resetn`, `rm_id`), a free-running System-ILA on the HWICAP `S_AXI` (trigger on `AWVALID` into
  0x44A2xxxx), and a **`jtag_axi` master** so xsdb can `run_hw_axi`-read HWICAP/DFXCTL status **while the
  Hart is stalled** (closes the "System Bus Access absent" gap).

Then run the least-destructive ladder, each rung gated (STOP-and-review before the next):

| Rung | Op | Proves | Destructive? |
|---|---|---|---|
| R0 | read-only ICAP SR/WFV/IDCODE probe | ICAP plumbing alive, WFV≠0 at rest, clock toggling | no |
| R1 | SWAP_BEGIN → ABORT (decouple only, no write) | decouple-confirm on silicon; does decouple glitch static | low |
| R2 | clearing-only push (`led_clear.bin`) → ABORT | is the ICAP **write path** itself fatal, independent of RM content | **yes** |
| R3 | truncated / small partial (shrunk bounds) | **where** it dies (byte/frame offset via `icap_bytes`) | **yes** |
| R4 | full `led` partial, production bounds, full capture | reproduce with a complete trace / confirm the timeout artifact | **yes** |

Capture on EVERY destructive rung, all streams live + one synchronized wall-clock marker:
- **Serial ttyUSB6 (PRIMARY — survives the network/MBV death)**, `printk` time on, console loglevel 8.
- **sysfs poller** (zero-code, lock-free): `state/icap_bytes/eos_status/rm_id` every 0.2 s → live progress
  without taking the swap mutex. (Do NOT poll `GET_STATUS` — it blocks behind the swap.)
- **JTAG PC sampler** in parallel (passive `stop; rrd pc`): PC frozen on a store into 0x44A2xxxx with a
  sluggish `stop` = hard AXI bus stall; PC spinning in `fifo_drain` = live WFV poll. Also the authoritative
  death-moment detector (Hart drops from scan).
- **`dmesg -w`** over SSH with `ServerAliveInterval=2` (secondary — dies with the MBV).
- **pyverify `-u`** unbuffered (defeats the block-buffering that lost the first trace).

Post-hang, BEFORE the recovery reprogram (the config engine + JTAG TAP outlive the fabric):
- Read config **STAT/BOOTSTS** over JTAG (Vivado hw-manager): a CRC/frame-address/watchdog error bit = the
  engine aborted on a bad frame; a clean-but-unconfigured device = SHUTDOWN-left-hanging (matches the model).
- **Masked partial readback vs the golden `.rbd`/`.msk`**: if delta frames stay inside the RP pblock, the
  write did NOT escape the RP (consistent with the FAR forensics) → the static died by engine abort/SHUTDOWN,
  not frame-escape.

## Unified discriminator table (what proves which hypothesis)

Triage rule: **`icap_bytes` at death + which function logged last.**

| Hypothesis | Signature |
|---|---|
| H3 decouple never confirms | swap_begin `ECONFIRM`, `icap_bytes==0`, board **survives** (fail-closed) |
| H4 dies at decouple | last line = "asserting DECOUPLE", `icap_bytes==0`, drop before any WFV/CR log |
| H1 WFV never drains | `wait_wfv_nonzero` timeout `spins==max v=0`; `icap_bytes` frozen at a chunk boundary; JTAG PC spinning in `fifo_drain` |
| H2 CR never clears | `wait_cr_write_clear` timeout `cr=0x1`; frozen at a **batch** boundary |
| H5 dies mid-write | WFV/CR healthy, `icap_bytes` **advancing**, then abrupt drop with **no** timeout (frame-specific; but FAR forensics say frames are RP-confined, so this kills the static only via the SHUTDOWN-hanging mechanism) |
| H6 EOS spin | `icap_bytes==full` **before** a ~150 s silent gap; `eos_capture` `EOS_TIMEOUT` (the `eos_strict=0` success-on-timeout trap) |
| H7 dies at release/verify | `icap_bytes` full, EOS ok, death right after finish() "releasing dut/dbg reset" or garbage `rm_id` at verify |

## Decisions before touching the board again

- **Measure the real recovery time on the first recover** (the docs disagree: 12 s vs "several minutes") —
  it sizes the attempt budget.
- **Lease-TTL-vs-swap false-death is the linchpin of valid attribution.** `fpgahubd` resets the board on TTL
  expiry, which looks identical to a swap-death. Verify TTL headroom ≥ (op + recovery) before each attempt;
  tag any TTL-coincident death **VOID**, not diagnostic. Don't fight the 403 release — hold one window for a
  pre-decided rung batch, pre-stage all payloads, abandon by TTL.
- **Prereqs gate the acquire:** `/dev/mps3dfx` actually in the image (S3 packaging), the Phase-2
  instrumentation hooks built, and the image built with `MPS3_STATIC_ID=0x2B082E1B` so `rm_id`/`shell_id`
  are meaningful (not 0x0).
- **First firing of every destructive rung is supervised** (the project lead reviews the trace, decides the next rung).
  The harness automates boot/provision/capture/detect/recover/archive; it never auto-advances rungs and
  never blind-retries a destructive op. Hard cap ~4-6 destructive attempts; hitting it = rethink, not more
  board-downs.
- **Escalate to AMD/Xilinx** only if the ICAP accepts a correctly-formatted, pr_verify-passing, FAR-in-RP
  partial (WFV drains, sync/CRC/frames valid) yet silently drops config — an on-silicon primitive behaviour
  no static artifact explains. Given the forensics, that is unlikely until P0.1/P1.1 are done.

## Fastest route to root cause (recommended)
1. **P0.1 writer-equivalence audit** + **P0.2 frame-subset check** — off-board, no risk, may pinpoint the bug.
2. **P1.1 JTAG partial load** — one supervised, low-destruction op that decisively splits SW-path vs artifact.
3. Only if P1.1 says "SW path": **build the Phase-2 instrumentation + `shell_linux_dbg`, run R0→R4** with
   full capture, gated and supervised.

Most likely outcome per the combined evidence: P1.1 loads the partial cleanly over JTAG, and P0.1 finds the
`.ko` HWICAP FIFO/pack divergence — an **off-board code fix**, no fuzzing of a shared board required.
