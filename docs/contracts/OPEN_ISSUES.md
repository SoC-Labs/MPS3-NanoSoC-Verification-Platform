# Contract open issues — Phase 0 integrator punch-list

**Compiled 2026-07-04 (A6)** from the five parallel workstream reports
(A1 shell-RTL, A2 dfx-flow, A3 firmware, A4 host-python, A5 verify). These are
the questions the frozen v0 contracts could not answer and that the parallel
build surfaced. Nothing here blocks the Phase-0 skeleton; each item is tagged
with a **disposition** and **owner**.

> **RESOLVED 2026-07-04 (user decisions):** **I1** single-core DUT first
> (multicore deferred to the ethernet effort); **I2** the shell owns
> clearing-bitstream sequencing; **I3** integrate with the Hardware Hub —
> tender = Edge Device API supervisor (`HARDWARE_HUB_INTEGRATION.md` (internal note, not in the public tree));
> **I4** RP is a hier cell alongside the shell, board GPIO/ports brokered to the
> DUT through the shell. Contracts reconciled to **v0.1** accordingly
> (partition-pins, net-protocol, overlay-manifest); the cheap **I12** (len
> units) and **I13** (crc32 = zlib) folded in at the same time.
>
> **RESOLVED 2026-07-04 (wave 2 — build + integrator):** **I5–I11** register
> blocks assigned concrete addresses in `shell-regmap.md` v0.1 (SWDBB/DBGBR/
> UARTBR/GPIO/TELEM/GENCHK fields + DFXCTL.RM_ID; BRIDGE stays register-less by
> design). **I15** endianness = little-endian (modern AXI MicroBlaze), firmware
> + `tests/common/ovlstore_header.py` now agree. **I25** fixed (`step_verify`
> reads DFXCTL.RM_ID). **I19** greybox = hand-authored tie-off (W2). Hub-doc
> §2/§4 reconciled: DUT console split into `console` (shell) + `dut-uart` (rp),
> `swo` is rp-gated and now in the `dfx-swap` invalidation set (was the one
> inconsistency two agents independently caught); `rm_name` host-resolved from
> manifests; `xvc` handle scheme left to the Hub registry (conflicts capture the
> behaviour). Remaining open = bring-up validations (I17–I22) + RTL catch-up
> (dfx_ctl AXI decode, board_gpio IP) + Phase-0.4 RTL sourcing (see below).
>
> **Phase-0.4 finding (W1):** the legacy arch_tech `arm_mps3` target's actual
> single-core SoC RTL (`nanosoc_chip`) was never checked into the source repo —
> orphaned pre-generator infra. The MPS3 *board port* (pins/timing/flow) is
> done + pin-verified in `fpga/monolithic/`, but the SoC RTL must come from the
> nanosoc **generator**, not the legacy target. New decision **D2b**.

> **RESOLVED/UPDATED 2026-07-06 (contracts v0.2 reconcile, A6 — after the
> seven-workstream wave W-SIM/W-FAKESHELL/W-PUSH/W-PKG/W-JSON/W-DFX-ART+PROD/
> W-RTL-NEWIP landed):** **I24** RESOLVED (repo-root `set_env.sh` exists;
> 5 benches green under VCS + cocotb 2.0.1 — `tests/README.md`). **I18**
> software half RESOLVED (`.bin` = the `.bit`'s `e`-record payload
> byte-for-byte, big-endian config words, sync at word 20, **no bit-swap**;
> stream MSB-first 4-bytes-per-word into HWICAP `WF` — full analysis in
> `fpga/dfx/README.md` "I18"; the hardware residue stays open, see I18
> below). The wave's ambiguity flags are folded into `net-protocol.md` /
> `shell-regmap.md` / `overlay-manifest.md` **v0.2** (raw-6910 semantics,
> advisory filenames, link events, pair rm_id coherence, id rendering,
> required `swap.src`, held swap response, pair staging, UARTBR SWO_CFG +
> FIFO_STATUS fields, dual AXI handshake styles, static_id scheme). New
> items **I27–I31** added below (section F).
>
> **RESOLVED 2026-07-07 (gate-fix + polish pass):** the two "documented gate"
> caveats are closed — the shell rebuilt to **256 KiB LMB** (ELF fits, 53.9 KiB
> headroom, real greybox blob baked) and **QSPI staging** now moves MiB-scale
> partials *and* large clearings (the v2 clearing-cache keyed by rm_id,
> `overlay-manifest.md` map updated), so **every RM including nanosoc
> network-swaps**. **rm_eth_ss** (2nd DUT) pr_verified against the 256 KiB
> shell (`static_id 0xECCEDBF3`) — four RMs now. **I31 RESOLVED**: failure key
> pinned to **`err`** across firmware + fakeshell + `net-protocol.md`;
> `link.event` accepts `up`/`down`/`pulse` both sides. **regmap v0.3** adds the
> **MMCM_DRP** block @ `0x44AB_0000` (the built shell's clk_wiz AXI-Lite DRP).

Legend — disposition: **DECIDE** (needs user/architecture call) ·
**FIX** (clear resolution, safe to apply next) · **BRINGUP** (only a board can
resolve) · **NOTE** (recorded, no action yet).

## A. Architectural decisions (DECIDE — user/lead)

**I1 — v0 DUT: single-core nanosoc vs multicore. [RESOLVED → single-core]** Spec §3 names the DUT as
single-core Cortex-M0 nanosoc, but the `partition-pins.md` names were taken
from the dual-core `nanosoc-multicore-system` wrapper (which has a 2nd
UART/IPC with no partition-pin equivalent). Ties into unfilled spec **D2**.
*Surfaced by A1(#5), A2.* → Pick the v0 RM; drop/keep the 2nd-UART pins
accordingly. Recommendation: single-core nanosoc as `rm_nanosoc` v0 (matches
spec + smallest RP), multicore deferred to a later RM.

**I2 — Clearing-bitstream sourcing during a swap. [RESOLVED → shell owns it]** `net-protocol.md` says the
coordinator "supplies the right clearing bitstream"; `overlay-manifest.md`'s
QSPI A/B store holds only *one* RM's pair; the config-agent tasking reads as if
both arrive over the network per swap. A3 implemented a RAM cache of the
previously-loaded RM's clearing as a placeholder. *Surfaced by A3(#1), A4(#1).*
→ Decide the source of the *current* RM's clearing bitstream at swap time:
(a) host always pushes the outgoing RM's clearing + incoming partial as a unit;
(b) shell caches the running RM's clearing in RAM/QSPI; (c) QSPI library keyed
by rm_id. Relates to **D16** (clearing-state ownership). This is the single
biggest UltraScale-specific correctness decision — resolve before Phase 2.3.

**I3 — Host/tender role split (in-band vs out-of-band). [RESOLVED → Edge Device API supervisor; see HARDWARE_HUB_INTEGRATION.md]** Design discussion
2026-07-04: the tender is an out-of-band management + network on-ramp role
(MCC provisioning, JTAG, power, first-bring-up SWD), NOT the compute brain
(pyverify floats on the network) and NOT merely a VPN. fpgahub today is
USB/IP+mTLS, not WireGuard. A deployed board self-boots headless (§8A); a
dedicated Pi is needed only for remote/standalone pods, else OOB-USB tunnels
from the existing hub. *Surfaced in design review.* → Fold into the plan's
host section + A4 tender README once confirmed (pending user).

**I4 — Shell BD topology. [RESOLVED → RP is a hier cell alongside the shell; board ports brokered to DUT]** Is the RP a hierarchical cell *inside* the shell
BD (A1 modelled it so, via `create_bd_pin`) or a separately-stitched
checkpoint? This sets whether partition pins are BD pins or top ports, and
fixes A2's RP instance path (`u_shell_top/u_rp_dut`, placeholder). *A1(#1),
A2(#3).* → A1+A2 to agree one hierarchy; then freeze the instance path.

**D16 — QSPI single-part sharing: map collision + MCC arbitration. [RESOLVED 2026-07-19 → the DUT owns `0x0`-`0x50000`]**

> **2026-09-23 (D13):** the shell's overlay store has LEFT the QSPI for good: it now lives on the
> user microSD (`usd_spi` at `0x44A4`, `0xDA` partition). The SST26 store code is deleted, so the
> "rebase out of `0x0`-`0x50000` first" consequence below no longer applies. The DUT owns the
> whole SST26.
`QSPI_CLEARING_CACHE_HANDBACK.md` (internal note, not in the public tree) asked for this row and never got one, which
is how the clearing-cache came to be written against an interface the XDC said to
leave idle. Two questions were tangled together; both are now answered.

**(a) The map collision — the DUT wins the low region.** There is ONE physical
8 MiB SST26VF064B. The nanoSoC boot map wants its boot table at flash `0x0`
(`flash_pack.py`: table `0x0`, HOT `0x1000`, COLD `0x20000`) and stage-0 reads it
from there; the shell's overlay store claims `0x0` too (`OVLSTORE_HEADER`).
**Decision: the DUT owns `0x0`-`0x50000`.** Rationale, on evidence rather than
preference:
* The shell's default firmware keeps every clearing **in RAM and never touches
  the flash store**, so the overlay store is unused in practice.
* Read on silicon 2026-07-17: the low region held only an address-ramp **test
  pattern**, and `0x40000`/`0x50000` were blank — no live overlay store, no
  GOLDEN image. Nothing is being displaced.
* The alternative (rebase the DUT) costs a stage-0 recompile **and** an RM
  rebuild for no benefit today.
**Consequence:** if the shell's flash-backed overlay store is ever enabled, it
MUST be rebased out of `0x0`-`0x50000` first. `overlay_store_commit()` and A/B
slot staging remain off-limits until that rebase exists.

**(b) QSPI ↔ MCC arbitration — not a hazard on this board.**
`mps3_harness.xdc:207-212` warns "this flash is also the MCC's FPGA-config source
… keep the interface idle until D15 is resolved", pinned to a D15 row that asks a
different question and so could never answer it. Evidence now says the premise
does not hold here: the MCC configures the FPGA from the **SD card**
(`sd_install` writes `MB/HBI0309C/Nanosoc/nanosoc.bit`, and MCC reboot loads it —
exercised repeatedly 2026-07-17/18), and the DUT has since erased/programmed this
QSPI part many times (256 B, 4 KB, and the full 160 KB image) with **no effect on
the board's ability to configure**. Treat the XDC comment as stale and re-point it
at this row when that file is next touched — do not rebuild the shell just for a
comment.

**D17 — nanosoc_upy: XiP flash-boot vs BRAM scaffold. [RESOLVED 2026-07-22 → XiP is the shipped default; scaffold kept as a no-flash build variant]**
DECISION (lead): ship the FLASH-BOOT XiP config as the default `nanosoc_upy` overlay
(the one-config doctrine's product path), and keep the baked-BRAM scaffold as an
explicit no-flash bring-up variant (`make rm-nanosoc-upy-scaffold-dcp`). Same
fabric, same rm_id 0x01000005 — no second id, no wrapper fork. Implemented:
`rm-nanosoc-upy-dcp`/`add-rm-nanosoc-upy` now build XiP, the shipped manifest
carries the XiP partial, rm_list.tcl + README_XIP.md updated, all rm_id/overlay
gates green. Footgun noted in-tree: deploying nanosoc_upy needs flash programmed
at 0x0 first (the M0 loader does it in ~3 min).

---
Original analysis follows.
CORRECTION: an earlier take here suggested the two could COEXIST as separate
overlays via a distinct rm_id (design_id 0x0006). Digging into the rm_id gate
(scripts/harness_gates/check_rm_id_encoding.py) shows that is NOT a cheap additive
change and in fact fights the architecture:
  * design_ids MUST be unique across the library (the gate rejects a collision), and
  * each overlay binds to a wrapper whose STATIC `localparam` must equal its rm_id
    — parsed from the .sv, NOT overridable by a synth generic (unlike the IMEM/
    bootrom knobs). So a coexisting nanosoc_upy_xip would need its own wrapper file
    driving 0x0006 — a wrapper FORK.
That is semantically wrong anyway: rm_id is the FABRIC-DESIGN identity, and the
scaffold and XiP are the SAME fabric (same rp_nanosoc_upy_wrapper, same boundary,
same routed logic). They differ ONLY in baked content — the IMEM image (baked
MicroPython vs zeros) and the bootrom (QSPI-enabled vs not). Baked content is not
fabric identity, so they correctly share rm_id 0x01000005 and the machinery
rightly refuses to mint a second id for one fabric.

**So the real decision is narrow:** which build config does the single
`nanosoc_upy` overlay SHIP with — scaffold (BRAM) or XiP (flash)? The other stays
available via the existing make targets (`rm-nanosoc-upy-dcp` /
`rm-nanosoc-upy-xip-dcp`); it just isn't the shipped overlay.
  * **scaffold default (today):** always boots, no flash dependency — safe for
    bring-up/CI; but flash-boot is build-on-demand, not the deployable default.
  * **XiP default:** matches the one-config doctrine (flash boot = product,
    RAM_PRELOAD = scaffold); but deploying it without flash programmed at 0x0
    boots to silence — a footgun, mitigated only by docs.
No new rm_id, no wrapper fork either way. Recommendation deferred to lead: the
doctrine points to XiP-as-default, safety points to scaffold-as-default. Whoever
decides, keep the loser as the documented build variant, not deleted.

ORIGINAL FRAMING (superseded, kept for provenance):
does the XiP flash-boot variant REPLACE the BRAM scaffold? [OPEN — user/lead]
As of 2026-07-21 there are two builds of `rm_nanosoc_upy` from one wrapper,
differing only by two synth-time env overrides (`FPGA_BOOTROM_DIR`,
`UPY_IMEM_IMG` — see `fpga/rp/nanosoc_upy/README_XIP.md`):
* **scaffold** — MicroPython baked into IMEM BRAM; boots with no flash. Self-
  describes as "NOT THE PRODUCT". This is the committed `overlay/nanosoc_upy`.
* **XiP** — empty IMEM; boots MicroPython from flash. PROVEN on silicon (the
  "PRODUCT GATE PASSED" run). Built by `make rm-nanosoc-upy-xip-dcp` +
  `add-rm-nanosoc-upy-xip`.
They share `rm_name=nanosoc_upy` and `rm_id=0x01000005`, so the overlay system
cannot hold both — a `swap nanosoc_upy` is ambiguous between two bitstreams with
the same identity.
**Decision needed:** does XiP *replace* the scaffold as the shipped `nanosoc_upy`?
* For: the scaffold was always a stepping-stone; the product path is flash boot.
* Against / cost: a stock `deploy nanosoc_upy` then REQUIRES the boot image
  programmed at flash `0x0` first, or the board boots to silence — a new
  precondition for every consumer of this RM.
* If they must coexist, the XiP variant needs its own `rm_id` (a new design id,
  e.g. 0x0006), which is a boundary/identity change to weigh.
Not baking this in unilaterally (same class of call as D16). Until decided, the
XiP overlay is built to an out-of-tree `OVERLAY_ROOT`, leaving the committed
scaffold overlay untouched.

## B. Missing register blocks (FIX — A6+A3+A1 assign offsets)

`shell-regmap.md` v0 defined 7 blocks; the build found these needed and
unlisted. Add each a page in the map + fields:

- **I5 — SWD pin-wiggler block** (only `dbg_resetn` was covered). *A3(#3), A5.*
- **I6 — Debug Bridge (XVC/BSCAN) block** — no base assigned. *A3(#4).*
- **I7 — UART0/UART1/SWO AXIS-bridge block** — absent. *A3(#5).*
- **I8 — `rm_id` readback / DFX Bitstream Monitor** — "DFXCTL-adjacent" but no
  offset. *A3(#2), A5(#4).*
- **I9 — TELEM (0x44A5)** — base only, zero fields. *A3(#6).*
- **I10 — GENCHK** — has a base but no `net-protocol.md` verb drives it; A1
  drafted a layout needing sign-off. *A3(#7), A1(#4), A5(#6).*
- **I11 — BRIDGE** — no AXI-Lite surface (fixed forwarding only); confirm the
  MicroBlaze needs no visibility/override. *A1(#3), A5.*

## C. Contract inconsistencies / underspecs (FIX — clear resolution)

- **I12 — `len` unit mismatch.** `overlay-manifest.md` `len` = bytes;
  `net-protocol.md` wire header `len_words` = 32-bit words. A4's pusher derives
  words from the actual payload. *A4(#2).* → Keep both, name explicitly, document
  the ×4 relation; treat payload as source of truth.
- **I13 — CRC-32 variant unspecified.** A3/A4 assumed zlib/IEEE CRC-32.
  *A3(#9).* → Standardise on zlib `crc32`; state it in both contracts.
- **I14 — RM name ↔ numeric rm_id owner.** `swap.rm:"nanosoc"` vs the header's
  numeric `rm_id`. *A3(#10), A5.* → `fpga/dfx/rm_list.tcl` (A2) is the canonical
  map; the manifest carries both; host + firmware read from it.
- ~~**I15 — OVLSTORE header endianness** unspecified~~ → **little-endian**
  (MicroBlaze config default). **CLOSED + PINNED 2026-07-09.** Both independent
  implementations were verified against their actual packing code (not their
  comments) and agree on *every* field — the dangerous "partial agreement" state
  does not exist here:
  - C: `firmware/overlay_store/ovlstore_codec.c` — `put_u16le`/`put_u32le`
    serialize LSB-first.
  - Python: `tests/common/ovlstore_header.py` — `_SLOT_FMT = "<IIIIIIIIB"`,
    `_HEADER_FMT = "<4sHBB"` (`<` = little-endian).

  `ovlstore_codec.h`'s long "NOTE FOR A6: this firmware-side choice currently
  DISAGREES with tests/common/ovlstore_header.py … which packs big-endian" was
  simply **stale and false**; the Python side is (and per git history was)
  little-endian. Comments corrected; **no behavioural code changed**.

  Pinned by a cross-language golden test (the `ctrl_echo` pattern):
  `firmware/test/ovlstore_pack.c` emits an asymmetric golden header through the
  real codec; `tests/firmware_logic/test_ovlstore_header_golden.py` asserts the
  two byte strings are identical and that each side round-trips the other's
  bytes. Negative-controlled both ways (flip either side to big-endian → the
  golden test fails). *A5 closed.*
- ~~**I16 — `set_clk` presets not enumerated.**~~ **CLOSED — the DRP retune
  landed in firmware `8a12a08` (2026-07-10) and was proven on silicon 2026-09-24**
  (`set_clk` 25/100/50 MHz, each relocked, `docs/evidence/2026-09-w3/ila_proofs_20260924.txt`
  B5). The preset table (ids 0/1/2, D/M/O, 50 MHz after configuration) is now in
  `shell-regmap.md` under CLKRST. *History, as recorded 2026-07-06:*
  *A4(#3).* Current state, precisely: `firmware/clkrst/clkrst.c`
  has a **real** `strcmp` table lookup that fails closed on unknown preset
  names (the lookup logic is final), but the table *contents*
  (`"25mhz"`/`"50mhz"`/`"100mhz"` → ids 0/1/2) are an explicit placeholder,
  and the DRP sequencer + MMCM-relock polling behind `DUT_CLK_SEL` are
  unimplemented. `shell-regmap.md` v0.2 records this under CLKRST. → A1/A3
  to publish the real DRP preset table (25 MHz default); host-side preset
  validation (W-HOST-MISC) waits on it.

## D. Bring-up / hardware validations (BRINGUP — flag, cannot close now)

- ~~**I17 — SLR geometry + Pblock sizing (D7).**~~ **RESOLVED 2026-07-09** —
  the evidence was already committed; this was documentation lag, not open work.
  Both halves are measured, not assumed:
  - *Geometry:* `fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt` —
    `SLR0 = X0Y0..X5Y4`, `SLR1 = X0Y5..X5Y9`; all four RP regions
    (X2Y0/X3Y0/X2Y1/X3Y1) and the primary ICAP site `CONFIG_SITE_X0Y0` report
    `-> SLR SLR0`. The single-SLR rule holds by measurement.
  - *Sizing:* `prod_results_2026-07-07-256k/util_rm_nanosoc.rpt` (scoped to
    `pblock_rp_dut`, Design State: Routed) — **7902 / 42824 CLB LUTs (18.45 %)**
    and **16.5 / 144 BRAM tiles (11.46 %)** for the largest real DUT. ~5× LUT /
    ~9× BRAM headroom. *A2(#1,2) closed.* Revisit only when a larger DUT lands.
- **I18 — ICAP `.bin` word order vs PG134. [SOFTWARE HALF RESOLVED
  2026-07-06]** The emitted artefacts are fully pinned by byte-level
  analysis (`fpga/dfx/README.md` "I18"): the `.bin` is exactly the `.bit`'s
  `e`-record payload (variable-length ASCII header — never strip a fixed
  offset), config words stored **big-endian** (sync `AA 99 55 66` literal at
  word 20), **no bit-swap**, length always a multiple of 4; the config agent
  assembles each 4 file bytes MSB-first into one `WF` word. **Hardware
  residue — updated 2026-07-09 after commit `2f8813d` (OTW reconfig on silicon):**

  **RE-CONFIRMED 2026-07-22 (regression found + fixed).** OTW reconfig had
  SILENTLY REGRESSED between `2f8813d` and now: the re-keying scripts built the
  shell firmware LITE against a FIFO-mode `axi_hwicap`, so every re-keyed shell
  could ping/pass-the-gate but load NO RM (fixed `d28c292`, fw
  `HWICAP_FIFO=1 WINDOWED=1`). With that fix, over-the-wire reconfiguration is
  PROVEN AGAIN on the shell current *at that date* (`static_id 0xD84A2E7A`; the
  fielded shell has since been re-minted — see `docs/FIELDED_SHELL.md`): a swap read back
  `rm_id=0x01000005` with `verified=True`. This re-establishes (1) and (2) at
  the shipped static_id; it does NOT close (3) (EOS still needs the mailbox read
  described below). Evidence: `d28c292`; `BOARD_HANDOFF_NOTES.md` (internal note, not in the public tree)
  ("SD / POWER-ON BOOT — CURRENT STATE"), `QSPI_RP_BOARD_BRINGUP.md` (internal note, not in the public tree)
  ("CURRENT STATE (TL;DR)").
  - ~~(1) end-to-end MicroBlaze→AXI→HWICAP byte-lane confirmation~~ —
    **RESOLVED on silicon.** `icap_bytes = 1312792 = 1312536 + 256`: every byte
    of the partial (plus the 256-byte clearing tail) reached `HWICAP.WF`, and the
    RP came up as the intended module. A byte-lane error would not produce a
    working configuration.
  - ~~(2) clearing-then-partial actually clearing/GSR-ing the RP on silicon~~ —
    **RESOLVED on silicon.** `DFXCTL.RM_ID` read back `0x000000b2` after the swap
    (JTAG-verified independently), and `regdemo_a` independently yields
    `0x000000a1`. Four successful over-the-wire swaps.
  - **(3) post-`DESYNC` EOS/status behaviour — STILL OPEN, but now INSTRUMENTED
    (2026-07-09).** Do not infer this from (1)/(2): `swap_fsm.c`'s post-DESYNC
    EOS wait is explicitly **best-effort**, so a successful swap does *not* prove
    `HWICAP_SR_EOS` ever asserted on this `axi_hwicap` build.

    The **next real stream-direct swap now answers it by one JTAG read** — two
    capture-only fields were added to the diag mailbox (following the
    `tx_last_status` pattern that root-caused the LAN9220 stall in `22ea891`):

    | field | offset | abs (256 KiB build) | meaning |
    |---|---|---|---|
    | `icap_sr_last` | `+0x5C` | `0x3FFDC` | raw last **non-zero** `HWICAP_SR` at finish (an all-zero read tells you nothing) |
    | `icap_eos_status` | `+0x60` | `0x3FFE0` | `0`=no finish yet, `1`=EOS seen, `2`=bounded wait expired without EOS |

    Capture-only: it reads the *same* `SR` the gate already reads, changing no
    timing and no transition. Both outcomes are host-tested
    (`test_swap_icap_direct`: EOS-seen and EOS-timeout).

    **This does not close I18(3).** It closes only when someone reads the
    mailbox after a real swap. *A2(#5).*
- ~~**I19 — Greybox RM generation**~~ — **RESOLVED (decision taken + written up,
  not a hardware question).** Hand-authored tie-off wrapper, *not* Vivado
  `update_design -buffer_ports`; full rationale in `rm_greybox.sv`'s module
  header and `fpga/dfx/rms/README.md` (`buffer_ports` is a *post*-synthesis
  operation and cannot express the contract's tie-off semantics). The wrapper is
  pr_verify-COMPATIBLE across every config. *A2(#6) closed.*
- **I20 — Two coexisting SWD paths** — physical CoreSight header (first
  bring-up) + shell internal SWD probe; confirm both wanted. *A1(#2).*
  **NOTE (2026-07-09): this is a DESIGN DECISION for the lead, not a bring-up
  measurement** — no board time will resolve it. Mis-filed under section D.
- **I21 — SMBF_DATA tristate** — A1 picked split o/i/t (vs legacy `inout`) for
  consistency with swd_dio/mdio; confirm. *A1(#6).*
  **NOTE (2026-07-09): also a DESIGN DECISION, not a measurement.** The chosen
  split-o/i/t form is already synthesis-proven (it rides into both the
  timing-met monolithic bitstream and the implemented static shell). What is
  "open" is only whether the lead prefers the legacy `inout` style.
- **I22 — SWD / RMII bit-order** — **SPLIT 2026-07-09.**
  - ~~`d/e/f/g`, `r/s/t/u` vs host `bitbang.c`~~ — **RESOLVED, no board needed.**
    The encoding is fixed by the *host driver*, so it was always verifiable by
    reading it rather than by bring-up. OpenOCD
    `src/jtag/drivers/remote_bitbang.c`:
    `char c = 'd' + ((swclk ? 0x2 : 0x0) | (swdio ? 0x1 : 0x0));` and
    `char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0));`; the `'c'`
    sample reply must be ASCII `'0'`/`'1'` (`char_to_int()` errors and calls
    `remote_bitbang_quit()` on anything else). The firmware already matched on
    all three counts. Now pinned by `firmware/test/test_swd_server.c`'s
    `test_openocd_remote_bitbang_conformance()`, which **re-derives** every byte
    from those formulas — an inverted mapping fails the test, not the bring-up.
    *A3(#8) closed.*
  - ~~`rmii_phy_if` RXD/TXD nibble order~~ — **RESOLVED 2026-07-09, no board
    needed.** It looked like a wiring question, but the tree already contained an
    **independent implementation** of the same RMII↔MII conversion: the DUT's
    MAC-side `rmii_to_mii.v` (`ethernet-mac-ahb/amba_wb_bridges`, instantiated by
    `rp_eth_ss_wrapper.sv`). `tests/rmii_conformance/` faces our shell-side
    virtual PHY against it and passes a realistic frame with an asymmetric
    payload: **byte-exact in both directions** (VCS; `RESULT.txt`).

    The result has force because the two have **different ancestry**:
    `rmii_to_mii.v`'s header credits WangXuan95's upstream, while our
    `rmii_phy_if.sv` carries no attribution and its README states "Reuse
    outcome: hand-rolled, not LiteEth"; they share zero identifiers. (Beware the
    coincidence that WangXuan95's upstream module is *also* named
    `rmii_phy_if` — that is a name collision, not shared lineage.)

    Both directions are independently falsifiable: swapping the TX di-bit order
    breaks Direction B only (18 byte errors); swapping the RX nibble halves
    breaks Direction A only (18). Neither control touches the third-party RTL.

    **Not proven** (and out of scope for this bench): pad-level timing, CRS_DV
    de-multiplexing, preamble insert/strip, 10 Mb/s di-bit repetition, RX_ER —
    these remain the `rmii_phy_if` README's documented v1 simplifications. *A5
    closed.* **I22 is now fully resolved.**

## E. Tooling / integration notes (NOTE)

- **I23 — fpgahub fit.** `platform_deploy` doesn't match fpgahub's
  `ProgramPlugin` ABC (its dispatcher pre-parses a Xilinx `.bit` header); A4
  implemented it as a CLI an `Action` shells out to. Name collisions:
  fpgahub's "overlay" (PYNQ `.dtbo`) and `BitstreamHeader` (Xilinx `.bit`) vs
  this platform's overlay-triple and ICAP header. *A4(#4,#5).*
- **I24 — No repo-root `set_env.sh`. [RESOLVED 2026-07-06 — W-SIM]**
  `set_env.sh` exists at the repo root and pins the validated combo
  (miniconda python 3.10 + cocotb 2.0.1 + VCS 2022.06-SP2 + license env;
  Questa documented as fallback). Five benches proven green under VCS:
  `sim_smoke` 2/2, `clkrst` 5/5, `dfx_ctl` 4/4, `board_gpio` 4/4,
  `mdio_phy_model` 3/3 (`tests/README.md` "Sim target"). Note: pinning
  cocotb 1.7.2 would NOT have avoided the driver port (1.7.2 has the same
  ReadOnly-phase restriction); the shared drivers were ported to 2.x
  instead. *A5.*
- **I25 — `swap_fsm.c::step_verify()` always reports `verified=true`** — known
  Phase-0 stub gap; A5 left a "canary" test documenting it. *A5.*
- **I26 — ser2net pty accepter syntax** unverified against a real install;
  `socat` is the confident fallback. *A4(#6).*

## F. New items from the 2026-07-06 wave (v0.2 reconcile)

- **I27 — `firmware/test/bin/*` binaries are git-tracked (FIX).** Six
  compiled host-gcc test binaries are in the index (slipped in with an
  earlier `git add -A`; the two newest binaries are untracked, so the set is
  also inconsistent). → `git rm --cached firmware/test/bin/*` + gitignore at
  the next commit; the harness rebuilds them anyway.
- **I28 — UART1 partition pins reserved-but-outside-contract (NOTE).** The
  `uart_bridge` RTL implements the full U1 register/FIFO path, but the
  DUT-side `uart1_tx_*`/`uart1_rx_*` seam is NOT in `partition-pins.md`
  (single-core I1 boundary); the BD must tie it off (`uart1_tx_tvalid_i=0`,
  `uart1_rx_tready_i=0`). The pin contract widens only with the multicore
  RM. *Source: `fpga/shell/ip/uart_bridge/README.md` flag #5.*
- **I29 — SWO capture is UART/NRZ-only (BRINGUP/DECIDE).** `swo_uart_rx.sv`
  deserialises 8N1 NRZ only; confirm the nanosoc/CMSDK TPIU trace path is
  configured for NRZ (Manchester would need a sibling decoder behind the
  same byte+valid interface — do not build until confirmed needed). *Source:
  uart_bridge README flag #1.*
- **I30 — Two AXI4-Lite slave handshake styles coexist (NOTE — decision
  recorded).** Xilinx-template ready-pulse (most shell IP) vs
  `mdio_phy_model`'s accept-on-valid. **Decision (v0.2): bless both;**
  masters/BFMs must observe ready and capture responses concurrently
  (`shell-regmap.md` "AXI4-Lite slave conventions";
  `tests/common/regmap.py` is the reference BFM). Unification is
  deliberately not required.
- **I31 — Reference-server divergences vs firmware. [RESOLVED 2026-07-07]**
  (a) failure key pinned to **`err`** (fakeshell aligned to the firmware +
  net-protocol.md v0.2); (b) `link.event` accepts `up`/`down`/`pulse` in both
  the fakeshell and firmware; golden-vector coverage added. Original: The two
  server implementations of `net-protocol.md` disagree on details the v0.2
  contract deliberately leaves loose: (a) failure-diagnostic key — firmware
  `mps3_ctrl_encode_response()` emits `"err"`, `pyverify.testing.fakeshell`
  emits `"error"` (plus extra fields like `"verified":false`); clients must
  key only on `ok` until this is pinned. (b) fakeshell accepts only
  `up`/`down` for `link.event` — firmware (and v0.2) also accept `"pulse"`.
  → align fakeshell with firmware (or vice versa) next time either is
  touched; add a golden-vector case for both.
