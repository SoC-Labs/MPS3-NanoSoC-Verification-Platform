# nanoSoC → current MPS3 harness: integration gap analysis

**Status: HISTORICAL — these gaps are largely closed.** The nanoSoC RM has since been built, swapped over the wire, booted and halted under a host debugger on silicon; read the analysis below as the record of what was open when it was written, and [docs/STATUS.md](STATUS.md) as current. **Scope as written:** what must happen before a *simple* single-core nanoSoC (Arm Cortex-M0)
can be swapped into the then-current shell (`static_id 0x3A8BBA62`, 512 KiB LMB; docs/FIELDED_SHELL.md has the fielded one)
over the wire and actually **run**. Bring-up + re-key problem, not from-scratch.

**Author's method:** every claim is tagged **[VERIFIED]** (read directly in the
code/artefacts named) or **[INFERRED]** (reasoned, needs a run/board to confirm).
No Vivado, board, or commit was touched producing this; read-only analysis.

---

## 1. Verdict

**A simple nanoSoC is close — this is a re-key + re-implement of an
already-proven RM, not new SoC integration.** The wrapper exists, was
`pr_verify`'d **COMPATIBLE** twice (2026-07-04 proof, 2026-07-06 prod, 7 partition
pins, timing met), fits the pblock at 18.5 % LUTs, and the single biggest
CPU-DUT footgun — *the swap path never releasing `dut_resetn`* — was **already
fixed** for `rm_uart_echo` (commit `82244ac`) and nanoSoC inherits that fix for
free.

**Gap count: 8 real gaps.** Ranked by what blocks "swapped in and running":

| # | Gap | Class | Blocks | Board? | Size |
|---|-----|-------|--------|--------|------|
| **G1** | nanoSoC overlay keyed to the **dead** `0xECCEDBF3` shell; live shell is `0x3A8BBA62` | re-key | swap refused before it starts | no | needs-a-build (impl only) |
| **G2** | No firmware baked into IMEM → partial loads but M0 has nothing to run | does-it-run | heartbeat | no | ½ day + synth |
| **G3** | OOC re-synth reads the **regenerated** `nanosoc.sv` that diverges from the wrapper's port list | build risk | any re-synth | no | hours (or 0 if dcp reused) |
| **G4** | UART shim clocked for 25 MHz; shell drives `dut_clk` at 50 MHz → baud 2× off | does-it-run | *legible* heartbeat | confirm on board | hours |
| **G5** | nanoSoC clearing (~118 KB) > 68 KiB RAM clearing arena → swap-**away** can't cache | swap-away | a↔b fast cycle | confirm on board | build to measure |
| **G6** | No `add-rm-nanosoc` Make target; `STATIC_DCP` default still the 256 KiB shell | build-flow | convenience/foot-gun | no | minutes |
| **G7** | UART **RX** never reaches the CPU (upstream RTL) → TX-only heartbeat, no echo | scope | interactive console only | no | n/a (accept) |
| **G8** | `dut_lockup`/`irq_out` tied off → a hung M0 is invisible to harness telemetry | observability | debuggability | n/a | n/a (accept) |

**Biggest gap: G1 (the re-key).** It is also the *cheapest* — the RM's OOC synth
checkpoint is shell-independent and already on disk; the re-key is one Vivado
**implementation** pass (route + partial/clearing) against the 512 KiB locked
static, no re-synth. Everything else is about turning "the partial loads" into
"the M0 prints".

### Smallest-first target (recommended)

**Single-core nanoSoC + `hello` UART2 banner, no Ethernet, no SWD, TX-only.**
- Ethernet is **[VERIFIED]** structurally absent from this DUT and tied off inert
  in the wrapper (`phy_rmii_*`, `mdio_*` → constants) — it is not on the path and
  needs zero work. `phy_rmii_ref_clk` is unused.
- SWD is debug-only: the heartbeat boots from a preloaded IMEM, so **no probe is
  needed for first light** (defer to the separate SWD bring-up plan).
- This is strictly less work than the full SoC and exercises the whole swap +
  console path that `rm_uart_echo` already proved on silicon.

**Can it beat the full SoC to "running"?** Yes, decisively — the "simple" target
*is* the full single-core nanoSoC RTL (there is no smaller nanoSoC RM); "simple"
means **the firmware and the scope**, not the fabric. Bake a banner app, skip
Ethernet/SWD/RX, and it is one synth + one impl away.

---

## 2. Prioritised gap list

### G1 — Re-key: nanoSoC overlay is bound to a dead shell  **[BLOCKER]**
**What.** The pusher refuses any overlay whose `static_id` ≠ the running shell's
(`net-protocol.md ping.shell_id`, `mps3_shell_static_id.c` strong-override seam).
- **[VERIFIED]** live shell `static_id = 0x3A8BBA62` (`overlay/mps3_shell_static_id.c`,
  `fpga/dfx/build512/prod/static_id.txt`).
- **[VERIFIED]** `overlay/nanosoc/manifest.json` is `"static_id": "0xECCEDBF3"` — the
  **256 KiB** shell. `overlay/eth_ss/` is also stale (`0xECCEDBF3`). The 512 KiB
  re-key run (`build512/prod/`) built only `{greybox, led, regdemo_a, regdemo_b,
  uart_echo}` — **nanoSoC and eth_ss were left behind** because their OOC DCPs
  were not staged into that run.

**Why it blocks.** Swap-in aborts at `ping`/manifest check; the partial is never
even streamed.

**Concrete work.** Re-implement `rm_nanosoc` against the **already-locked 512 KiB
static** without re-minting `static_id`, using the incremental path that
`build_dfx.tcl` already supports (`DFX_ADD_RMS`, proven by `add-rm-eth-ss`):
- inputs all present **[VERIFIED]**: `build512/prod/static_routed_locked.dcp`,
  `static_id.txt (0x3A8BBA62)`, `config_rm_greybox_routed.dcp`.
- reuse the existing OOC checkpoint `fpga/dfx/build/prod/rm_nanosoc_synth.dcp`
  **[VERIFIED present]** (netlist is shell-independent) **iff** you don't need to
  change the baked firmware (see G2). Otherwise re-synth first.
- then `make -C fpga/dfx overlays` regenerates `overlay/nanosoc/` keyed to
  `0x3A8BBA62`, and `make -C fpga/dfx verify` (which runs
  `check_overlay_static_id.py`) confirms lockstep.

**Size.** One Vivado *implementation* pass (opt/place/route + `write_bitstream`),
no synthesis, if the DCP is reused. **Board-free.**

**Alternative (heavier):** a full `make prod` including nanoSoC re-mints a **new**
`static_id` and invalidates *every* already-shipped overlay — do **not** do this
just to add nanoSoC; use the incremental add.

---

### G2 — Boot image: nothing runs on the M0 yet  **[BLOCKER for "running"]**
**How the M0 gets its first instruction (pinned down).**
- **[VERIFIED]** The reset vector lives in the **stage-0 bootrom**, a real ROM
  (`$readmemh`) baked at synth time. It exists on disk:
  `nanosoc_m0_soc/imp/fpga/firmware/stage0/{nanosoc_region_bootrom.v, bootrom.sv,
  stage0.hex}` (= `FPGA_BOOTROM_DIR`, the Makefile default). `pynq/filelist.tcl`
  hard-errors if it is missing.
- **[VERIFIED]** The **application** lives in **IMEM**, preloaded from a hex file
  at synth time. `pynq/filelist.tcl` sets `verilog_define {RAM_PRELOAD}`, which
  selects the `sl_ahb_rom`/`$readmemh` variant of `nanosoc_region_imem.v`. Its own
  header warns: *"Without this define the IMEM is empty and the CM0 HardFaults
  immediately on reset."* The image is `IMEM_MEM_FPGA_IMG` (default `"image.hex"`,
  a **word** hex for the 32-bit IMEM).
- **[VERIFIED]** `ooc_synth.tcl` passes **no** `IMEM_MEM_FPGA_IMG` override and
  stages **no** `image.hex`; `$readmemh("image.hex")` resolves against the Vivado
  launch cwd (`build/rm_nanosoc_synth/`). **[INFERRED]** the existing staged DCP
  therefore very likely has an **empty/undriven IMEM** — the partial would load,
  the M0 would boot the bootrom, and then have no valid app to run.

**Why it blocks "running".** "Swapped in + verified" (rm_id reads back) can pass
with an empty IMEM; a **heartbeat cannot**.

**Concrete work (board-free prep you can start now).**
1. Build the banner app: the pynq flow's default `APP=hello` is exactly a "UART2
   banner smoke" **[VERIFIED]** (`pynq/Makefile` line 73-75). A prebuilt word hex
   already exists: `nanosoc_m0_soc/imp/fpga/hello_word.hex`. (Rebuild fresh with
   `make -C nanosoc_m0_soc/pynq firmware APP=hello` if you want it current.)
2. Stage it as the IMEM image at the synth cwd: copy `hello_word.hex →
   fpga/dfx/build/rm_nanosoc_synth/image.hex` **before** re-running the OOC synth.
   (Because `ooc_synth.tcl` is "sourced, not edited," dropping `image.hex` in the
   cwd is the clean lever; the alternative is overriding the wrapper's
   `IMEM_MEM_FPGA_IMG`/`UART_CLK_HZ` params via a small synth wrapper.)
3. Re-synth (`make -C fpga/dfx rm-nanosoc-dcp`), **then** do G1's incremental add.

**Size.** ½ day incl. one OOC synth. **Board-free.** Note this **couples G1 to a
re-synth** (you can no longer reuse the firmware-less DCP), which pulls in G3.

---

### G3 — OOC re-synth reads the *regenerated* `nanosoc.sv` (port divergence)  **[BUILD RISK]**
**What.** There are two port-incompatible copies of module `nanosoc` in the
read-only source tree, and the two build recipes disagree on which one is `nanosoc`:

| | `nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv` (**canonical**, what `rp_nanosoc_wrapper.sv` instantiates) | `build_soc/rtl/nanosoc.sv` (**regenerated**, what `ooc_synth.tcl` reads) |
|---|---|---|
| SPI PL022 ports | **absent** | **present** (`spi_sclk/ss/mosi/miso`) **[VERIFIED lines 130-133]** |
| `exp_*` AHB dir | **master** (`exp_hsel/haddr…` out, `exp_hrdata/hresp/hreadyout` in) | **slave** — inverted (`exp_hsel/haddr…` in, `exp_hrdata/hresp/hreadyout` **out**) **[VERIFIED]** |
| Extra params | — | `SYS_CLK_FREQ_HZ`, `QSPI_FLASH_PRESENT`, `IMEM_0_RAM_PRELOAD`, `ACCELERATOR_SUBSYSTEM` **[VERIFIED]** |

- **[VERIFIED]** `ooc_synth.tcl` sources `nanosoc_m0_soc/pynq/filelist.tcl`, which
  reads `build_soc/rtl/nanosoc.sv` (the **regenerated** top). It does **not** use
  `fpga/rp/nanosoc/filelist.tcl`, whose whole job is to *substitute the canonical
  top for the regenerated one* — that substitution is bypassed on the staging path.
- **[VERIFIED]** `rp_nanosoc_wrapper.sv` ties `exp_hreadyout(1'b1)`,
  `exp_hresp(1'b0)`, `exp_hrdata(32'h0)` — driving constants *into* ports that are
  **outputs** in the regenerated top. **[INFERRED, high-confidence]** that is a
  hard elaboration error, and the unconnected `spi_miso`/undriven `exp_hsel…`
  inputs add warnings.

**Reconciling the paradox.** A working DCP exists on disk (G1), so this flow
*did* synthesize cleanly before — meaning **either** `build_soc/rtl/nanosoc.sv`
matched the wrapper at that earlier run (this tree is actively regenerated and
has since drifted), **or** the proven DCP came from the canonical-substituting
`filelist.tcl` path. Today's on-disk `build_soc/rtl/nanosoc.sv` does **not** match
the wrapper.

**Why it's a risk not a certain blocker.** If you take G1's DCP-reuse path (no
re-synth), G3 does not fire at all. It only bites when G2 forces a fresh synth.

**Concrete work (if it bites).** Before the re-synth, dry-run the resolution
(`tclsh fpga/rp/nanosoc/filelist.tcl` prints the resolved file set) and confirm
which `nanosoc.sv` lands. If it's the regenerated top, either (a) point the synth
at the canonical top (the substitution `fpga/rp/nanosoc/filelist.tcl` already
implements, or `RM_NANOSOC_USE_REGENERATED_TOP=0`), or (b) update the wrapper's
`exp_*`/`spi_*` connections to the regenerated port directions. **Do not edit the
read-only source tree** — fix on the platform side. **Board-free**, hours.

---

### G4 — Clock/baud mismatch garbles the heartbeat  **[does-it-run]**
- **[VERIFIED]** `uart_axis_shim` / wrapper default `UART_CLK_HZ = 25_000_000`
  (the PYNQ-Z2 operating point), and `ooc_synth.tcl` does **not** override it.
- **[VERIFIED]** the shell drives `dut_clk` at **50 MHz** — `nanosoc_ooc.xdc`:
  `create_clock -period 20.000` (50 MHz), described as the `clk_wiz_dut` MMCM
  output. `build_dfx.tcl`'s clock guard confirms the static's `dut_clk` propagates.

**Why it blocks.** The shim's 8N1 baud divider is `CLK_HZ/BAUD`. Built for 25 MHz
but clocked at 50 MHz, every bit is sampled at half the intended rate → the banner
comes out as garbage on TCP 6930. The **firmware's** UART2 divider must also match
the real clock (the pynq `fw_config` step patches the FPGA clock into firmware —
verify that value is 50 MHz, not the 100 MHz nanoSoC default).

**Concrete work.** Instantiate the RM with `UART_CLK_HZ = 50_000_000` (and confirm
the actual `clk_wiz_dut` output — the 50 MHz is a **D12 placeholder**, not yet an
A6-frozen decision), and confirm the firmware baud divider uses the same number.
**Board-free** to fix; final confirmation is on the board (read the banner).

---

### G5 — nanoSoC's clearing bitstream doesn't fit the RAM arena  **[swap-away]**
- **[VERIFIED]** the clearing arena is `MPS3_SWAP_CLEARING_ARENA_BYTES = 68 KiB`
  (commit `82244ac`; `QSPI_CLEARING_CACHE_HW_FINDINGS.md` (internal note, not in the public tree)). The small RMs' clearings
  fit (greybox 56 KB, uart_echo 64 KB in the 512 shell **[VERIFIED build512 sizes]**).
- **[VERIFIED]** nanoSoC's clearing on the 256 KiB shell was **117,684 B (~118 KB)**
  (`overlay/nanosoc/manifest.json`) — nearly 2× the small RMs, because clearing size
  scales with the fraction of the RP region the RM occupies, and nanoSoC is the
  biggest RM (18.5 % LUTs). **[INFERRED]** the 512-shell clearing will be similarly
  large (> 68 KiB).

**Why it matters (and why it's *not* a swap-in blocker).** Swap-**in** to nanoSoC
is fine: the host pushes greybox's small clearing then nanoSoC's ~1.6 MB partial
straight to ICAP (chunked). The arena is only used to **cache the resident RM's
clearing so a later swap-away needs no re-push** (the a↔b fast cycle). nanoSoC's
clearing won't fit, and the QSPI clearing-cache — the other place to hold it — is
**broken on silicon** (`QSPI_CLEARING_CACHE_HW_FINDINGS.md`: first light hangs the
shell, 100 % ICMP loss). So **swap-away from nanoSoC must push its ~118 KB clearing
over the wire every time**; the no-reload optimisation is unavailable for this DUT.

**Concrete work.** None required for "swap in + run". If a↔nanoSoC fast cycling is
wanted: enlarge the arena again, or fix QSPI, or accept the per-swap-away re-push.
**Confirm the real clearing size** when G1's build emits it. Board for final proof.

---

### G6 — Build-flow ergonomics: no `add-rm-nanosoc`, stale `STATIC_DCP` default  **[LOW]**
- **[VERIFIED]** the Makefile has `add-rm-eth-ss` but **no** `add-rm-nanosoc`; the
  incremental engine (`DFX_ADD_RMS`) is generic, so nanoSoC works by invoking
  `build_dfx.tcl` directly or by cloning the eth-ss target.
- **[VERIFIED]** `STATIC_DCP ?=` still points at `build/shell_proj_256k/…` (the
  **dead** `0xECCEDBF3` shell). The live 512 KiB static is
  `build/shell_proj_512k/shell_static_synth.dcp` **[VERIFIED present]**. A naïve
  `make prod` would build against the wrong shell and re-mint yet another id.
- Note the **`build/` vs `build512/` split**: `make rm-nanosoc-dcp` stages the DCP
  into `build/prod/`, but the 512 locked static is in `build512/prod/` — the
  incremental add must be pointed at `build512/prod/` and its DCP staged there.

**Concrete work.** Add an `add-rm-nanosoc` target mirroring `add-rm-eth-ss` but
sourcing `build512/prod/` inputs; or run the incremental invocation by hand.
Optionally bump the `STATIC_DCP` default. **Board-free, minutes.** (Concurrent
session edits this repo — coordinate before touching the Makefile.)

---

### G7 — UART RX never reaches the CPU (upstream RTL gap)  **[SCOPE — accept]**
**[VERIFIED]** (traced in `fpga/rp/nanosoc/README.md`, re-confirmed in the RTL):
the shim correctly shifts host→DUT bytes onto `p1_in[4]`, but nanoSoC's own
pin-mux hard-ties `uart2_rxd` to idle whenever bit 4 is an input — the external
pad is ignored. So **host→DUT console input does not work** on this DUT; only
**DUT→host (banner/heartbeat)** does. This is an upstream `nanosoc_m0_soc` gap
(read-only), not a wrapper bug. **Consequence:** the `rm_uart_echo` *echo* trick
won't work for nanoSoC — plan a **TX-only** heartbeat, not an interactive console.
No work; just set expectations. (`rm_id` verify still passes — see below.)

---

### G8 — Hung-M0 is invisible to the harness  **[OBSERVABILITY — accept]**
**[VERIFIED]** `dut_lockup` and `irq_out` are tied `1'b0` in the wrapper because
`nanosoc.sv` never routes `cpu_0_lockup`/`txev` to a top-level port (A6 gaps #5-6).
`swo` is tied `0` (M0 has no ITM). So if the baked firmware faults, **the shell's
telemetry cannot see the lockup** — you infer it from a silent 6930. Cheapest real
fix is upstream (add the output port to the generator template); accept for now.

---

### Non-gaps (confirmed already handled — do **not** redo)
- **Reset release — DONE. [VERIFIED]** `82244ac` releases `dut_resetn` at the swap
  **commit point** (in `step_verify`, after verify, not at RELEASE). This is the
  exact fix that made the first logic-bearing RM (`rm_uart_echo`) run; nanoSoC's M0
  is in the same `dut_clk/dut_resetn` domain and inherits it. Without it the M0
  would have been held in reset forever while `rm_id` still read back fine.
- **rm_id verify — DONE. [VERIFIED]** wrapper drives `rm_id = 32'h0000_0001`
  (`RM_ID_NANOSOC`); `rm_list.tcl` records the same; the shell reads it at
  `DFXCTL.RM_ID (0x44A1_0010)` after release → verify passes.
- **FT1248 boot-hang self-drain — DONE. [VERIFIED]** the wrapper replicates the
  proven PYNQ tie pattern on `p1_in[6:0]/[15:8]`, so the bootrom banner over the
  FT1248 controller does not back up and hang the boot.
- **soc_glue helpers — RESOLVED. [VERIFIED]** the two files the 2026-07-04 README
  flagged as missing (`soc_glue_mux2.sv`, `soc_glue_reset_sync.sv`) now exist at
  `nanosoc_arch_tech/nanosoc_gen/rtl/soc_glue/`, which is the Makefile's default
  `SOCLABS_NANOSOC_GEN_DIR`. OOC synth is no longer blocked on them.
- **Ethernet clock/PHY — N/A. [VERIFIED]** no MAC in this DUT; `phy_rmii_ref_clk`
  unused; nothing to wire. (A MAC-in-operation DUT is the separate `rm_eth_ss`.)
- **pin_check / readiness filter — PASSES. [VERIFIED]** the wrapper carries the
  full 30-signal boundary incl. the I4 `dut_gpio_*` group, so `build_dfx.tcl`'s
  readiness grep and `pin_check.py` accept it (keep it in `pin_check.py`'s
  `WRAPPERS` list when editing).

---

## 3. Simplest path to a running nanoSoC — runbook

Target: **M0 boots, `hello` UART2 banner visible on TCP 6930**, no Ethernet, no
SWD, TX-only. `[PREP]` = board-free, start now (you hold the board lease);
`[BOARD]` = needs the leased board.

1. `[PREP]` **Build the banner firmware.** `make -C nanosoc_m0_soc/pynq firmware
   APP=hello` (or reuse `nanosoc_m0_soc/imp/fpga/hello_word.hex`). Verifies the
   bootrom + word-hex exist. (G2)
2. `[PREP]` **Reconcile the OOC top.** `tclsh fpga/rp/nanosoc/filelist.tcl` and
   confirm which `nanosoc.sv` resolves; ensure the wrapper's `exp_*`/`spi_*`
   connections match it (or force the canonical top). (G3)
3. `[PREP]` **Stage the IMEM image + clock.** Copy the word hex to
   `fpga/dfx/build/rm_nanosoc_synth/image.hex`; set `UART_CLK_HZ = 50_000_000`
   (wrapper param or synth override); confirm the firmware baud divider matches. (G2/G4)
4. `[PREP]` **OOC synth.** `make -C fpga/dfx rm-nanosoc-dcp` → a
   `rm_nanosoc_synth.dcp` with `hello` baked into IMEM. (needs Vivado *synth*)
5. `[PREP]` **Incremental re-key against the 512 static.** Add an `add-rm-nanosoc`
   target (mirror `add-rm-eth-ss`) or invoke `build_dfx.tcl` directly with
   `DFX_ADD_RMS=rm_nanosoc`, `DFX_REUSE_LOCKED=build512/prod/static_routed_locked.dcp`,
   `DFX_STATIC_ID_FILE=build512/prod/static_id.txt`,
   `DFX_REF_ROUTED=build512/prod/config_rm_greybox_routed.dcp`, staging the DCP into
   `build512/prod/`. Emits nanoSoC partial+clearing `pr_verify`'d vs greybox,
   keyed to `0x3A8BBA62`. (needs Vivado *impl*) (G1/G6)
6. `[PREP]` **Regenerate + verify the overlay.** `make -C fpga/dfx overlays` then
   `make -C fpga/dfx verify` — `overlay/nanosoc/` now reports `0x3A8BBA62` and the
   static-id lockstep gate passes. (G1)
7. `[BOARD]` **Identity check.** `pyverify ping` on 6900 → `shell_id == 0x3A8BBA62`.
8. `[BOARD]` **Swap in.** `pyverify deploy nanosoc` → pushes greybox clearing +
   nanoSoC partial; expect `{"ok":true,"rm_id":"0x00000001","verified":true}`.
   `dut_resetn` releases at the commit point (G4-non-gap) → the M0 starts.
9. `[BOARD]` **Read the heartbeat.** `pyverify console 6930` → the `hello` banner.
   Garbled text ⇒ G4 clock/baud; silence ⇒ empty IMEM (G2) or boot hang (§4).
10. `[BOARD]` **Swap away.** `pyverify deploy greybox` — expect nanoSoC's ~118 KB
    clearing to be **pushed over the wire** (arena too small, QSPI broken — G5).

The whole `[PREP]` chain (steps 1-6) is runnable now without the board and lands
the artefact the swap needs; only steps 7-10 need the lease.

---

## 4. Risks specific to a CPU DUT (that a UART-echo RM never surfaced)

- **Boot hang / dead heartbeat.** If IMEM is empty (G2) or the bootrom waits for a
  host that isn't there, the M0 boots but never prints. The FT1248 self-drain that
  would otherwise hang the boot banner is already handled (non-gap), but an empty
  IMEM still yields a silent 6930 that *looks* like a bad swap. Bake `hello` first;
  treat "verified:true but silent console" as a firmware/clock problem, not a swap
  problem.
- **The swap is a clean reset — which is safer than an SWD load.** Lab landmine
  `swd-load-systick-wedge`: loading over SWD leaves SysTick armed and the new image
  wedges in `Default_Handler`; an ARMv6-M (M0) **cannot clear an active exception**,
  so a wedged core needs a POR. Here the **swap itself resets the RM** (`dut_resetn`
  released fresh at commit), so a swapped-in image comes up clean with SysTick *not*
  pre-armed by a stale load. Prefer **swap-in over SWD-load** for bring-up; if the
  M0 does wedge, a **re-swap (partial reload) is the POR** — no probe needed.
- **A faulted M0 is invisible (G8).** `dut_lockup` is tied off, so a HardFault loop
  won't raise anything the harness can read; you only see a silent console. Budget
  for "silent ⇒ suspect fault" during bring-up.
- **Clock/baud (G4)** is a CPU-DUT issue precisely because the M0 runs *real
  firmware* against a *real UART divider*; the constant-`rm_id` RMs never exercised
  either clock-domain assumption.
- **Swap-away cost (G5)** is a CPU-DUT issue because nanoSoC is the largest RM;
  its clearing is the first to overflow the arena. The small demo RMs never did.
- **Don't kill the push client mid-swap.** Unrelated to nanoSoC but on the same
  path: `AWAIT_*` states have a fail-closed timeout now (`82244ac`/`f5825db`), but a
  client killed mid-transfer can still strand a session — a ~1.6 MB nanoSoC partial
  takes longer to push than the tiny RMs, widening that window. Let the push finish.

---

## 5. One-line answer

*"It's a re-synth (bake `hello`, fix the 50 MHz clock, reconcile the regenerated
`nanosoc.sv`) plus one incremental implementation pass against the 512 KiB locked
static to re-key it to `0x3A8BBA62` — the RM itself already `pr_verify`s and the
reset-release footgun is already fixed."* The hidden hard parts are **the empty
IMEM (bake firmware or it won't run)** and **the OOC port divergence on a fresh
synth**; the rest is mechanical.
