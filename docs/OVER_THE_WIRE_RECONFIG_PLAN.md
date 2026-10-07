# Over-the-Wire Partial Reconfiguration — Unblock Plan

> **Status: HISTORICAL** — a record of the plan to unblock over-the-wire partial reconfiguration, written before it was proven on silicon as of 2026-07-09.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `PLATFORM_LIVE_STATUS.md` — internal notes, not in the public tree.

> **Status:** design / analysis only. **No Vivado, no builds, no hardware, no commits.**
> **Date:** 2026-07-07 · **Author:** analysis pass over `mps3-nanosoc-platform`
> (fpga/dfx, firmware/, docs/contracts). Cites `file:line` throughout.
> **Scope:** how to get a *full* over-the-wire partial reconfiguration
> (host pushes a partial over Ethernet → HWICAP writes it → the RP
> reconfigures → `rm_id` changes) working on the physical MPS3 KU115.

---

## 1. Ground truth (proven this session, the starting line)

On the physical MPS3 KU115, the DFX static shell `fpga/dfx/build/prod/config_rm_greybox_fw.bit`
(static_id **`0x394227AF`**) is proven to:

- **(a)** answer ICMP ping (192.168.10.101, `PLATFORM_LIVE_STATUS.md:110`);
- **(b)** serve the JSON control channel on TCP 6900 (`ping`/`telemetry`,
  `"shell_id":"0x394227af"` — `PLATFORM_LIVE_STATUS.md:113`);
- **(c)** receive + header-validate + RAM-stage a real **46 KB clearing**
  bitstream over TCP 6910 end-to-end (`PLATFORM_LIVE_STATUS.md:114`);
- **(d)** **survive a JTAG partial RP swap** — the network stays up
  (`PLATFORM_LIVE_STATUS.md:112`). This is the single most important
  de-risking fact (see §6).

**Blocked:**
- **(e)** the ~886 KB **partial** was rejected on SIZE — it exceeds the
  256 KiB MicroBlaze LMB and the config_agent's overflow sink is QSPI, which
  is not operational on this board;
- **(f)** `{"op":"swap"}` drove the firmware into the swap FSM and stalled with
  no valid partial staged; **the `swap_fsm → HWICAP → ICAP` write path has
  never been exercised on hardware** (`PLATFORM_LIVE_STATUS.md:80-82`).

Both `swap_fsm`, `config_agent`, `overlay_store` and the QSPI SST26 driver are
**fully written and pytest/host-gcc green** — the gap is HW bring-up, not code.

---

## 2. The blocker, and the partial-size-vs-RAM math

### 2.1 Measured artefact sizes (current `fpga/dfx/build/prod/`, one 4-clock-region RP pblock)

| RM (`rm_id`) | Partial `.bin` | Clearing `.bin` | RAM-stageable partial? |
|---|---:|---:|---|
| `rm_regdemo_a` (0xA1) — **the demo RM** | **906,732 B (885.5 KiB)** | 46,984 B | ❌ |
| `rm_regdemo_b` (0xB2) | 908,312 B | 46,984 B | ❌ |
| `rm_greybox` (0x00) | 955,232 B | 47,808 B | ❌ |
| `rm_led` (0x1E) | 925,388 B | 49,628 B | ❌ |
| `rm_nanosoc` (real M0 SoC) | 1,648,200 B | 117,684 B | ❌ |
| `rm_eth_ss` (MAC+PTP DUT) | 1,429,640 B | 98,964 B | ❌ |

Every **clearing** fits RAM; **no partial does.**

### 2.2 The RAM budget (hard numbers)

- LMB = `0x0..0x3FFFF` = **262,144 B** (256 KiB), confirmed in `xparameters.h`
  — `firmware/platform/BUILD_RESULT.txt:6`.
- Current firmware footprint: text 140,516 + data 1,832 + bss 65,888 =
  **208,236 B → headroom 53,908 B** (`firmware/platform/BUILD_RESULT.txt:14-15`).
- The bss already contains config_agent's two staging slots
  (`s_clearing_ram` + `s_partial_ram`, `config_agent.c:42-46`) and swap_fsm's
  clearing arena (`swap_fsm.c:98`). Reclaiming both partial buffers and running
  a single max-size staging buffer gets you to **~116 KiB, best case.**

**886 KiB partial ≫ 116 KiB best-case staging.** RAM-staging a partial is
flatly impossible for *any* RM at the current pblock size — this is gate (e).

### 2.3 Why the partial is ~886 KiB — the pblock is the lever

The RP pblock (`fpga/dfx/dfx_floorplan.xdc:73`) is
`{X2Y0 X3Y0 X2Y1 X3Y1}` — **2 clock-region columns × 2 clock-region rows** of
SLR0, sized for **nanosoc's ~7,903 LUTs** (`dfx_floorplan.xdc:69-72`). An
UltraScale partial covers every configuration frame in that rectangle, so its
size is set by the *pblock geometry*, not the RM. The demo RM actually needs
almost nothing: **72 LUTs / 2 FF / 0 BRAM / 0 DSP**
(`fpga/dfx/build/prod/util_rm_regdemo_a.rpt`, CLB Logic table). The pblock is
~100× oversized for regdemo — which is exactly the knob Path 1 turns.

---

## 3. Ranked recommendation

| Rank | Path | Removes size gate for… | New HW bring-up needed | Touches proven static (`0x394227AF`)? | Verdict |
|---|---|---|---|---|---|
| **1** | **Stream-direct config_agent → HWICAP** (§5, Path 3) | **every RM** (regdemo, nanosoc, eth_ss) | **none** (HWICAP only, which we must prove anyway) | **no** — reuses it as-is | **DO FIRST** |
| 2 | Onboard QSPI staging (§5, Path 2) | every RM | QSPI/SST26 first-light | no | Do next — the *production* staging path |
| 3 | Small RP pblock (§5, Path 1) | **regdemo only** (nanosoc/eth_ss don't fit) | none | **yes — new static_id, full re-validation** | Last resort / demo-only |

**Recommendation: do Path 3 (stream-direct), gated behind a standalone HWICAP
bring-up (§6).** It is the only path that (a) removes the size limit for the
*real* DUT RMs, not just the toy demo; (b) needs **no new peripheral bring-up**
(QSPI stays out of the critical path); (c) runs on the **existing proven static**
with **no `static_id` churn**; and (d) directly exercises the one link that is
untested and blocks everything downstream — the HWICAP write path. Paths 1 and 2
both *still depend on HWICAP working*, so proving HWICAP first (§6) is on the
critical path regardless of which staging strategy ultimately ships.

**Why not Path 1 first:** it rebuilds the whole DFX static → **new static_id** →
invalidates the board's proven `0x394227AF` bitstream and forces re-proof of
ping/JSON/config_agent on a fresh static — throwing away this session's hardest-
won asset — and it only shrinks the *trivial* regdemo partial; nanosoc/eth_ss
(BRAM, 7,900 LUTs, the real DUT) can never fit a tiny pblock, so it does not
advance the north star.

**Why not Path 2 first:** the SST26/AXI-Quad-SPI path has *never come up on this
board* ("QSPI flash that isn't working on this board yet",
`PLATFORM_LIVE_STATUS.md:73,128`). It is the right *production* staging store
(preserves reject-before-ICAP, resumable, decouples receive from reconfig), but
it is a second HW bring-up stacked on top of the HWICAP one — not the fastest
route to first light.

---

## 4. Path evaluations

### Path 1 — Small RP pblock → RAM-stageable partials

**What it is.** Shrink `dfx_floorplan.xdc`'s pblock so the demo RM's partial
fits the ~116 KiB RAM budget, then RAM-stage as today.

**Feasibility / sizing.** The pblock is built from SLICE/DSP/RAMB site ranges
derived from the four clock regions `{X2Y0 X3Y0 X2Y1 X3Y1}`
(`dfx_floorplan.xdc:73-95`). Partial size scales ≈ linearly with
(frame-columns × clock-region-rows) inside the pblock. Two independent levers:

- **Vertical:** UltraScale config frames span a *full clock region* (≈60 CLBs)
  in height — you cannot make the RP shorter than one CR row, but going
  **2 CR rows → 1 CR row halves the frame count** (≈886 KiB → ≈450 KiB).
- **Horizontal:** the pblock currently spans two whole CR columns (X2–X3).
  Restricting it to a **narrow band of a few CLB columns inside one clock
  region** — ample for 72 LUTs + routing + RP-boundary partition-pin reach —
  cuts the horizontal frame count several-fold.

Combined (1 CR row × a narrow column band) plausibly lands the regdemo partial
in the **~100–200 KiB** range. **This is marginal against the ~116 KiB budget**
— it *might* clear it with headroom, it might not, and that can only be known by
a build. The ICAP co-location constraint is satisfied either way (ICAP is
`CONFIG_SITE_X0Y0` / CR X5Y1, in SLR0; the RP only needs to be *in* SLR0, not
adjacent — `dfx_floorplan.xdc:48-56`).

**Risks.**
- **New `static_id` → invalidates the proven board bitstream.** The `static_id`
  is the CRC-32 of `static_routed_locked.dcp` (`build_dfx.tcl:563`); any static
  rebuild re-mints it and invalidates `0x394227AF` and every shipped partial.
  Full re-validation of ping/JSON/config_agent on the new static required.
- **Only the toy RM benefits.** nanosoc (7,903 LUTs + BRAM) and eth_ss cannot
  fit a sub-CR pblock — Path 1 abandons the actual DUT goal.
- **Routing / DRC risk on a tight pblock:** `CONTAIN_ROUTING` is on
  (`util_rm_regdemo_a.rpt` Pblock Summary), so a too-narrow region can fail to
  route the RP boundary or trip HDPR DRCs — iterative Vivado work.
- **Decoupler boundary** must still be re-checked against the new geometry.

**Effort/risk: MEDIUM-HIGH effort, HIGH opportunity cost, demo-only payoff.**

### Path 2 — Onboard QSPI staging

**What it is.** Make the config_agent's existing QSPI overflow sink real on HW:
a large partial streams into the inactive A/B slot of the onboard SST26VF064B,
is CRC-verified in flash, then `swap_fsm` streams it flash→HWICAP.

**How ready is it?** Very — in *firmware*. The plumbing is complete and wired:

- config_agent routes any payload `> s_dst->cap` to the QSPI sink
  (`config_agent.c:257-269`); the partial sink and the symmetric clearing sink
  are registered by `coordinator_init()` (`coordinator.c:70,76`).
- `overlay_store.c` has **real** SST26 sequences — READ 0x03, WREN 0x06,
  RDSR 0x05, PP 0x02, 4K-erase 0x20, **ULBPR 0x98** global unlock, WBPR 0x42
  re-lock (`overlay_store.c:26-33,211-235`), page-program, chunked read-back
  CRC, and flash→HWICAP streaming (`overlay_store.c:346-365,676-682`).
- The AXI Quad SPI (OVLSTORE) core **is in the proven static**:
  `axi_quad_spi:3.2` @ `0x44A40000`, `SPI_0` → `QSPI_D0..3/SCLK/nCS`
  (`fpga/shell/bd/shell_bd.tcl:643-659,827`; `fpga/shell/shell_top.sv:73-85,196-202`).

**What is needed to make it work on HW** (all first-silicon for this peripheral):

1. **Register-offset confirmation.** `platform_regs.h:192-229` is "PG153-shaped,
   confirm against the Vitis-generated `xspi_l.h`" (`platform_regs.h:22-23`).
   Cross-check SRR/CR/SR/DTR/DRR/SSR offsets + the FIFO depth
   (`OVLSTORE_SPI_FIFO_DEPTH 16u`, `platform_regs.h:229`) before trusting a write.
2. **SST26 first-light:** JEDEC-ID read, then the **Global Block-Protection
   Unlock (0x98)** — the SST26 powers up fully write-protected, so without the
   unlock every erase/program silently no-ops while reads still work
   (`docs/MPS3_ONBOARD_FLASH_MAPPING.md:28,139,153`). `overlay_store_bp_unlock()`
   already issues it globally (`overlay_store.c:211-223`).
3. **Pins/XDC + timing.** Pins already exist (AU24/AV24/AV21/AV22/AT25/AT24,
   LVCMOS33+PULLUP — `MPS3_ONBOARD_FLASH_MAPPING.md:90-91`); the MPS3 harness
   currently `set_false_path`s the QSPI ports (firmware-paced, OK to start —
   `MPS3_ONBOARD_FLASH_MAPPING.md:133`).
4. **MCC ↔ QSPI arbitration (open item D15).** Confirm the FPGA (not the MCC)
   owns the flash pins at runtime (`MPS3_ONBOARD_FLASH_MAPPING.md:160`).

**Risks / cost.**
- A brand-new peripheral bring-up (SPI mode/CS, block-protect, offset
  confirmation) that has never worked on this board.
- Adds flash erase+program+read-back-CRC latency to every large swap.
- **Still depends on HWICAP** — QSPI only fixes *staging*; the flash→HWICAP
  stream (`overlay_store_stream_staged_partial`, `overlay_store.c:676`) is the
  same untested ICAP write. So Path 2 does not shorten the critical path to
  first HWICAP light; it is the *production hardening* of staging.
- **Single-flash contention:** the SST26 is shared between OVLSTORE and the
  proposed nanoSoC DUT firmware (`MPS3_ONBOARD_FLASH_MAPPING.md:29,121,159`) —
  fine while the shell owns it for staging, but a live nanoSoC-firmware-in-flash
  demo collides.

**Effort/risk: MEDIUM effort (real code, needs peripheral first-light + offset
audit). Right long-term staging store; wrong thing to prove *first*.**

### Path 3 — Stream-direct config_agent → HWICAP (no full staging)  ★ RECOMMENDED

**What it is.** As each word of the 6910 payload arrives, write it straight into
`HWICAP.WF` (paced by `WFV` vacancy), computing the running CRC on the fly.
Never buffer the whole partial — no RAM slot, no QSPI. Removes the size limit
for *every* RM.

**Why it fits the machinery.** All the pieces exist:
- config_agent already streams the payload chunk-by-chunk in `session_feed()`'s
  `RECV_PAYLOAD` state and can dispatch each chunk to a sink instead of a RAM
  `memcpy` — it already does exactly this for the QSPI sink
  (`config_agent.c:298-313`). A **direct-to-ICAP sink is a third delivery
  target** alongside RAM and QSPI, reusing the same `mps3_cfg_agent_qspi_sink_t`
  begin/write/finish/abort shape (`config_agent.h:128-133`).
- The HWICAP write primitive already exists twice: `hwicap_push_chunk()`
  (`swap_fsm.c:211-230`) and `hwicap_write_words()` (`overlay_store.c:315-324`)
  — WFV-vacancy-paced `WF`/`SZ`/`CR_WRITE`.

**What breaks, and the answer:**

1. **Reject-before-ICAP is lost.** The contract verifies CRC *before* any ICAP
   write (`net-protocol.md:196-199`; `config_agent.h:46-49`). Stream-direct
   writes to ICAP before the trailing header CRC is known. **This is acceptable
   and standard**, because two independent integrity checks remain:
   - the **partial bitstream carries embedded CRC words that ICAPE3 validates in
     hardware** as it loads — a corrupt stream makes ICAP flag a config error;
   - the **post-load verify** (`SWAP_VERIFY`) reads `DFXCTL.RM_ID` +
     `RM_STATUS.rm_id_valid` and compares to the target (`swap_fsm.c:428-448`);
     a bad load → `SWAP_FAILED` → **RP parked decoupled + held in reset (safe
     state)**, never released live (`swap_fsm.c:555-569`).
   You still compute the header CRC as you stream and, on mismatch, force
   `SWAP_FAILED` and *do not release* — so "reject before ICAP" becomes "detect
   after ICAP + park safe." Most production ICAP loaders work exactly this way.
2. **Receive-then-verify-then-stream design is collapsed.** Today config_agent
   accumulates and hands a validated slot to `swap_fsm`
   (`config_agent.c:607-637`); stream-direct fuses receipt and ICAP write. The
   swap must be *armed first* (gate + decouple, `swap_fsm.c:232-277`), then the
   payload streamed. Cleanest shape: `swap` arms the FSM, the FSM asserts
   DECOUPLE + RP-reset, then config_agent's direct sink pushes to `WF`.
3. **Clearing-first ordering.** config_agent already enforces clearing-then-
   partial within a pair (`config_agent.c:199-204`). Stream-direct streams the
   incoming **clearing** to ICAP first (it clears the same RP frames regardless
   of which RM is resident — the clearings are near-identical, 46–118 KB), then
   the **partial**. For bring-up this *replaces* the cached-outgoing-clearing
   step (`swap_fsm.c:279-320`) with the pair's own clearing, sidestepping the
   whole clearing-cache/QSPI machinery.
4. **swap_fsm's readback/verify is unaffected** — it still reads `DFXCTL.RM_ID`
   after the stream.

**Minimal firmware change (est. ~80–150 lines, no new RTL):**
- Add a `hwicap` sink (begin: nothing / assert decouple already done by FSM;
  write: pack 4 bytes MSB-first → one `WF` push, WFV-paced, poll `SR_DONE`;
  finish: compare running CRC, poll `SR_EOS`; abort: force `SWAP_FAILED`).
- Register it as a third config_agent sink, selected when a swap is armed and
  the static exposes HWICAP.
- Sequence: `coordinator_handle_swap` (`coordinator.c:242-258`) → FSM gate +
  decouple → config_agent streams the pushed pair direct → FSM verify → release.

**Byte order — the one must-check (I18).** The `.bin` is **big-endian**; the
config agent must assemble **each 4 file bytes MSB-first**:
`word = (b0<<24)|(b1<<16)|(b2<<8)|b3`, no bit-swap
(`fpga/dfx/README.md:245-258`). **Warning:** both existing HWICAP writers use a
*native-endian* `memcpy(&w, &bytes[..], 4)` (`swap_fsm.c:222-224`;
`overlay_store.c:319-321`) — correct only if MicroBlaze is big-endian. This is
the I18 "hardware-verifiable residue" (`fpga/dfx/README.md:259-262`) and the
**single most likely first-write failure**; the direct sink must pack MSB-first
explicitly (or the memcpy must be proven against the actual MB endianness).

**Effort/risk: LOW-MEDIUM effort, LOW opportunity cost. Best first move.**

---

## 5. Concrete steps for the top path (Path 3, after §6 HWICAP bring-up)

1. **Prove HWICAP in isolation first — do §6 before any network work.**
2. Add a `hwicap_direct` sink (config_agent.h sink shape) that packs bytes
   MSB-first (I18) and WFV-paces `WF`, polling `SR_DONE` between chunks.
3. Restructure `coordinator_handle_swap` so `swap` **arms** the FSM (gate +
   DECOUPLE + RP-reset) and *then* config_agent streams the pushed pair's
   clearing, then partial, straight to the sink; keep the running CRC; on
   mismatch → `SWAP_FAILED`, do not release.
4. Keep `SWAP_VERIFY` (`DFXCTL.RM_ID` compare) and the release sequence exactly
   as written (`swap_fsm.c:428-536`).
5. **First over-the-wire target = the register-difference demo:** push
   `regdemo_b`'s pair over 6910, stream-direct, confirm `DFXCTL.RM_ID` flips
   `0xA1→0xB2` and the LED bar changes (`0xA5→0xBA`, `fpga/dfx/rm_list.tcl`
   regdemo entries). This is the JTAG-proven demo, now fully over Ethernet.
6. Then `rm_nanosoc` / `rm_eth_ss` — same path, no size change (the whole point).

Path 2 (QSPI) is then added *behind* this as the production staging store to
restore reject-before-ICAP and resumability; Path 1 is not pursued unless a
RAM-only, no-static-rebuild demo is specifically wanted (and even then only for
regdemo).

---

## 6. HWICAP write bring-up + debug plan (prove the untested link)

The `swap_fsm → HWICAP → ICAP` write has **never run on HW**
(`PLATFORM_LIVE_STATUS.md:80-82`). Prove it **in isolation**, before layering
the network on top. The big de-risker: **the JTAG partial swap already works and
the static survives** (`PLATFORM_LIVE_STATUS.md:112`) — so the *frames*
reconfigure the RP correctly and the decoupler holds. HWICAP only changes the
*delivery* of identical frames from JTAG pins to fabric. The only genuinely new
variables are: register offsets, byte-lane order, and FIFO pacing.

**Step 0 — confirm register offsets.** `platform_regs.h:157-175` HWICAP offsets
(WF 0x100, RF 0x104, SZ 0x108, CR 0x10C, SR 0x110, WFV 0x114) are
"PG134-shaped … confirm against the Vitis-generated `xhwicap_l.h`"
(`platform_regs.h:20-23`). Verify against the BSP header first. Core is
`axi_hwicap:3.0`, `C_ICAP_DWIDTH=32`, ICAPE3 (`shell_bd.tcl:580-593`), @0x44A20000.

**Step 1 — ICAP *read* smoke (zero reconfig risk).** Use the HWICAP read path
to issue the ICAP "read IDCODE" sequence and confirm you read back the KU115
IDCODE **`0x1390d093`** (the exact value JTAG already reported,
`PLATFORM_LIVE_STATUS.md:61`). This proves the AXI-HWICAP↔ICAPE3 datapath and
your offsets with **no** frame writes. Safest possible first contact.

**Step 2 — first *write*, JTAG-staged (network removed).** JTAG-load a small
known-good partial into a MicroBlaze buffer / DDR, then have firmware stream it
via HWICAP with real WFV pacing + `SR_DONE` polling. Confirm `DFXCTL.RM_ID` →
expected + `RM_STATUS.rm_id_valid`. This isolates "does the ICAP *write* path
work" from "does the network deliver bytes." (regdemo *clearing* = 46,984 B fits
RAM if you want a RAM-resident payload; a full partial needs DDR or the
stream-direct sink.)

**Step 3 — over the wire (Path 3).** Push `regdemo_b` over 6910, stream-direct,
confirm the same `rm_id` flip. Now end-to-end over Ethernet.

**Registers / status to watch** (`platform_regs.h:161-175`):
- `HWICAP.WFV` (0x114) — write-FIFO vacancy; **never push more words than WFV**.
- `HWICAP.SR` (0x110) — `SR_DONE` (bit0) between chunks, `SR_EOS` (bit2) at end.
- `HWICAP.CR` (0x10C) — `CR_WRITE` (bit1) kick; `CR_READ` (bit0) for Step 1.
- `HWICAP.ISR`/`IER` (0x20/0x28) — abort/error flags after a bad load.
- Post-load: `DFXCTL.RM_ID` (0x44A1_0010) + `RM_STATUS.rm_id_valid` (0x14 bit0);
  `RM_STATUS.dut_lockup` (bit1) (`platform_regs.h:133-142`).

**Decoupler / rp_resetn sequencing** (net-protocol steps 1–7,
`net-protocol.md:163-171`; implemented `swap_fsm.c`):
1. Gate XVC/SWD/UART/VPHY; assert `DFXCTL.DECOUPLE` (bit0) + `SHUTDOWN_AXI`,
   **clear** `CLKRST.RESET_CTRL.RP_RESETN` (reset = bit low) — `swap_fsm.c:251-258`.
   Confirm `DFXCTL.STATUS` = `DECOUPLED | RP_IN_RESET` (`swap_fsm.c:261-264`).
2–3. Stream clearing → partial to HWICAP.
4. Verify `DFXCTL.RM_ID == target` (`swap_fsm.c:437-441`).
6. Clear DECOUPLE + SHUTDOWN, **set** `RP_RESETN`; confirm `STATUS == 0` before
   ungating (`swap_fsm.c:510-535`).
- **Failure:** stay decoupled + in reset (safe), do **not** release
  (`swap_fsm.c:555-569`).

**Failure modes to watch (ranked by likelihood):**
1. **Byte-lane order (I18)** — native `memcpy` word packing vs required
   MSB-first (`fpga/dfx/README.md:245-262`; `swap_fsm.c:222-224`). Wrong order →
   ICAP never sees the `0xAA995566` sync → silent no-op / abort. *If Step 1's
   IDCODE read works but a JTAG-fine partial fails via HWICAP, suspect this
   first.*
2. **Register-offset mismatch** (illustrative offsets) — Step 0 + Step 1 catch it.
3. **Missing/short `SR_DONE` poll between chunks** — the current pump leaves the
   inter-chunk `SR` poll seamed as a bring-up TODO (`swap_fsm.c:205-210`);
   without it the WF overflows and words drop → ICAP CRC error. Add the bounded
   poll + poll-count timeout → `SWAP_FAILED`.
4. **Decoupler per-signal boundary incomplete (PG294)** — flagged 🟧 PARTIAL
   (`PLATFORM_LIVE_STATUS.md:81,124`). The JTAG swap surviving is strong
   evidence the boundary is *functional enough* for bring-up; a proper
   per-signal boundary is needed for a glitch-free production swap.
5. **Throughput over 100 Mb** — partials are 0.9–1.6 MB; keep lwIP timers pumped
   (`sys_check_timeouts`) via the non-blocking bounded-per-poll design —
   stream-direct must still cap words/poll (`swap_fsm.c:34` chunk bound).
6. **Clearing-first correctness on silicon** — designed + pytest'd, never run on
   HW; the biggest single correctness risk (`PLATFORM_LIVE_STATUS.md:155`).

---

## 7. Risk register (top path)

| Risk | Likelihood | Mitigation |
|---|---|---|
| Word byte-lane order wrong (I18) | **High** | Step 1 IDCODE read; pack MSB-first explicitly; A/B against the JTAG-proven frames |
| HWICAP offsets differ from BSP | Medium | Confirm vs `xhwicap_l.h` (Step 0) before any write |
| WF overflow (no SR_DONE poll) | Medium | Add bounded `SR_DONE` poll + timeout → SWAP_FAILED |
| Lost reject-before-ICAP (stream-direct) | Low (accepted) | ICAP HW CRC + post-load `RM_ID` verify + park-safe on mismatch |
| Decoupler boundary incomplete (PG294) | Low for bring-up | JTAG swap already survived; author per-signal boundary before production |
| New `static_id` (only if Path 1 chosen) | N/A for Path 3 | Path 3 reuses `0x394227AF` untouched |

---

## 8. One-line bottom line

**Prove HWICAP in isolation (IDCODE read → JTAG-staged write), then wire a
stream-direct config_agent→HWICAP sink (Path 3).** It reuses the proven
`0x394227AF` static, needs no QSPI or pblock rebuild, removes the size limit for
*every* RM, and exercises the exact untested link that blocks the north star.
Add QSPI staging (Path 2) afterwards as the production hardening; keep the small
pblock (Path 1) on the shelf as a demo-only fallback.
