# ICAP swap — P0 off-board findings (writer-equivalence audit + FIFO-proof archaeology)

> **Status: HISTORICAL** — a record of the ICAP swap P0 off-board findings, written while the root cause was still being isolated as of 2026-07-24.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `OVER_THE_WIRE_DEPLOY_STATUS.md`, `PLATFORM_LIVE_STATUS.md` — internal notes, not in the public tree.

> ## ⚠️ SUPERSEDED IN PART — read this first (2026-07-24)
> **The root cause is now CONFIRMED on silicon and it is NOT the Linux-runtime path.** It is a
> **static-implementation mismatch**: the partials were built by `build_dfx.tcl` against its OWN
> place-and-route of the static (`static_routed_locked.dcp` → `config_rm_greybox.bit`, 12,821,235 B),
> but the board was running `shell_linux_top.bit` from the **separate** `build_transplant_phaseB.tcl`
> impl run (11,883,949 B). A partial is bound to the exact static implementation it was built against,
> so loading it onto a different P&R corrupts the whole configuration — deterministically, by any path.
>
> **A/B proof (silicon, 2026-07-24):** the same `config_rm_led..._partial.bit` loaded via the *vendor
> JTAG* config port (Vivado 2025.2 hw_manager — `mps3_dfx`, the kernel and the FIFO protocol entirely
> bypassed): onto the **mismatched** static → config DIED (identical symptom to the mps3_dfx swap);
> onto the **matching** static (`config_rm_greybox.bit`) → **loaded cleanly, static SURVIVED**, Linux
> and the control plane stayed up. Same partial, opposite outcome.
>
> **Therefore the H6 hypothesis below (the `eos_capture` 1M-spin) is REFUTED as the root cause** — the
> JTAG path performs no EOS wait at all, yet failed identically. The eos fix (commit `397cec9`) remains
> **valid hardening** (a best-effort wait must not spin ~150 s and it makes failures fast + diagnostic),
> but it is **not** the fix for this failure. Everything below about the *writer* being a faithful,
> correctly-configured port of the proven firmware still stands and is corroborated — the writer was
> never the problem.
>
> **The fix:** the flown static and the partials must come from **one** impl run — either ship
> `build_dfx`'s own `config_rm_greybox.bit` as the board image (verified: boots the full Linux harness,
> control plane up at ~60 s), or rebuild the partials against phaseB's routed/locked static.
>
> **Why the static checks missed it:** `pr_verify` compared build_dfx's own greybox-vs-led (same run —
> internally consistent, never checked against the board's flown bitstream), and the FAR decode showed
> only that frames land in the RP *region*, not that the surrounding static matched the board's.


Autonomous, off-board, zero board risk. Executes P0.1 of `ICAP_SWAP_DEBUG_PLAN.md` and adds the decisive
silicon-history archaeology it depends on. **Result: the `mps3_dfx.ko` writer, the FIFO config, and the
bitstream are all the PROVEN combination — faithfully ported. The fault is the Linux-runtime path, not the
writer.**

## P0.1 — writer-equivalence audit: `mps3_dfx.ko` engine vs the silicon-proven firmware

Compared `src/linux_harness/drivers/icap/mps3_icap_engine.c` (`fifo_drain`/`write_words`) against the
bare-metal firmware `firmware/coordinator/swap_fsm.c` (`hwicap_fifo_*`, the classic proven writer).

| Aspect | Firmware (proven) | `mps3_dfx.ko` engine | Match? |
|---|---|---|---|
| Mode selected | FIFO (C_MODE 0) | `fifo_mode=1` default (I loaded with `mock=0`, no override) | ✓ same, matches C_MODE 0 |
| Drain loop | `while(done<n){ while(WFV==0); batch=min(n-done,WFV); push batch→WF; CR=WRITE; while(CR&WRITE); done+=batch; }` | identical structure (`wait_wfv_nonzero`→push≤vacancy→`CR=WRITE`→`wait_cr_write_clear`) | ✓ byte-for-byte |
| StartConfig granularity | one CR=WRITE per **batch** | one CR=WRITE per **batch** | ✓ |
| Word packing | `mps3_hwicap_pack_word` (MSB-first, shared header) | same shared primitive | ✓ |
| `HWICAP_SZ` writes | none (WFV-paced) | none | ✓ |
| Fail-closed | returns -1 on WFV-stuck / CR-never-clears | identical | ✓ |

**No divergence.** The engine is a faithful port; `fifo_mode` was correctly 1. This is **not** the LITE-vs-FIFO
mismatch class (that was `d28c292`: fw built LITE against a FIFO BD — the opposite of a match). The audit
clears the writer.

## FIFO-mode HWICAP IS silicon-proven (the archaeology the audit hinges on)

The concern was that only the LITE path was ever board-proven. It is not:
- `2f8813d` (2026-07-09) "over-the-wire DFX partial reconfiguration proven on KU115 silicon" — introduced
  `C_MODE {0}` (FIFO) **and** proved a **1.31 MB partial over Ethernet, `icap_bytes=1312792`, JTAG RM_ID
  readback, ~570 KB/s, network survived the swap** (`PLATFORM_LIVE_STATUS.md` L122/L124).
- Repeated `nanosoc_upy` swaps this program: `rm_id=0x01000005 verified=True`, then cold-boot MicroPython.
- `OVER_THE_WIRE_DEPLOY_STATUS.md` L44: the proven recipe is `HWICAP_FIFO=1` = `C_MODE {0}` depth 1024.
- `d28c292` (2026-07-17): the regression was **fw LITE vs a FIFO-mode HWICAP** — a mismatch, promptly fixed.

So the **FIFO protocol + `C_MODE {0}` depth-1024 HWICAP** is a proven-good silicon combination — driven by the
**bare-metal MB firmware**. My swap used the same protocol + config, ported to Linux.

## What that leaves — the fault is the Linux-runtime path, not the writer/config/artifact

Everything static is now cleared or proven: bitstream (FAR forensics: frames confined to the RP, identical to
the proven classic partial), `pr_verify` (compatible), FIFO config (matches proven), writer code (identical to
proven firmware), FIFO-mode-on-silicon (proven). The delta that remains is **runtime**: `mps3_dfx.ko` running
the (proven) protocol under Linux on the (bigger) linux static, for the first time on real silicon.

### Refined hypotheses (narrowed from 7 to the runtime-plausible few)

1. **H6 — EOS spin + release-into-bad-state (leading).** After the partial is fully written
   (`icap_bytes==full`), `eos_capture` polls `HWICAP_SR.EOS` up to `done_poll_max=1,000,000` and — with the
   default `eos_strict=0` — **returns success on timeout**. If `SR_EOS` doesn't assert on the linux shell (EOS
   on this axi_hwicap build is flagged unproven; the firmware's own EOS wait is "best-effort"), that is a
   ~150 s silent spin, after which `finish()` releases the RP — a coherent "150 s silent, then config lost".
   The firmware's `icap_direct_finish` treats the EOS wait as best-effort/bounded-short; the `.ko`'s 1M-spin
   is the behavioural difference even though the *write* code matches.
2. **Kernel-MMIO / AXI-path timing.** `readl/writel` ordering barriers and the MBV→AXI→HWICAP path under
   Linux differ from the bare-metal MB path, even with identical register sequences. A subtle ordering or
   back-pressure difference could stall the FIFO drain where the firmware didn't.
3. **Linux-static integration** (EOS/config-engine interaction specific to the DDR4+MBV static).

### Why P1 (JTAG partial-load) is now the single decisive test
JTAG-loading the same partial bypasses the entire Linux runtime (mps3_dfx.ko, kernel MMIO, the MBV AXI path).
Given the writer/config/bitstream are the proven combination, a clean JTAG load would pin the fault to the
Linux-runtime path with near-certainty — most likely H6 (EOS/finish) rather than the write itself.

### Autonomous follow-ups worth doing before the board
- **Reconcile `eos_capture` with the firmware.** Consider defaulting the swap to bound the EOS wait short (or
  make `eos_strict` meaningful) so a non-asserting EOS FAILS FAST and fail-closed (RP parked) instead of a
  150 s spin then a release. This is a code change to validate on the (supervised) board — but it directly
  addresses the leading hypothesis and would have made the first failure diagnostic instead of destructive.
- The Phase-2 engine instrumentation (printk at `eos_capture` entry/exit + `icap_bytes`) will confirm/deny H6
  on the first instrumented attempt.
