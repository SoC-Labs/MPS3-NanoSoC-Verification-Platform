# Modularization & Sub-Repo Refactor Plan

> **Status: HISTORICAL** — a record of the shelved modularization / sub-repo refactor plan as of 2026-07-09.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `PLATFORM_LIVE_STATUS.md` — internal notes, not in the public tree.

> ## 🗄️ SHELVED — 2026-07-09
>
> Paused by the project lead while the hardware track lands over-the-wire reconfiguration.
> **Nothing was extracted; no file was moved.** Everything the effort produced is
> additive and inert:
>
> - `docs/SUBREPO_REFACTOR_PLAN.md` (this file), `docs/PHASE1_PYVERIFY_EXTRACTION.md`
> - `poc/systemrdl/` — the Phase-0 keystone PoC (self-contained, `make` to re-run)
> - `scripts/extract_pyverify.sh` — extraction dry-run script (never executed
>   against the working repo)
> - `tests/integration/conftest.py` — import-boundary helper; the repo is green
>   with pyverify installed **and** not installed
>
> Delete `poc/` + `scripts/extract_pyverify.sh` + these two docs and the tree is
> exactly as it was, minus the bug fixes below (which are worth keeping).
>
> **The bug fixes this effort found are NOT part of the shelving** — they are
> independent, gated, and green: the `rdl2c.py` identifier-corruption fix
> (landed upstream), and the address-decode aliasing fix across all 8 CSR blocks.
> See `poc/systemrdl/README.md` "Bugs this PoC found". Note those RTL fixes are
> **not yet on silicon** — see `PLATFORM_LIVE_STATUS.md` §8.7.
>
> **To resume:** Phase 0 is done and proven. Start at Phase 1 (§8).

**Status:** SHELVED (was: PROPOSAL for evaluation) · **Date:** 2026-07-09 ·
**Scope:** how to split `mps3-nanosoc-platform` into reusable SoCLabs component
repos, what that buys us in robustness/modularity, and a phased migration that
never breaks the green build.

> This is a *plan*, not a change. No files are moved by this document. It is
> written to be evaluated and edited before any extraction begins. Companion:
> `PLATFORM_LIVE_STATUS.md` §8 (the build-evidence + design-question
> detail this plan draws on), `docs/STATUS.md` (implemented-vs-not matrix),
> `docs/contracts/` (the frozen interfaces that become inter-repo APIs).

---

## 1. Executive summary

The platform is **already de-facto componentized** — it just lives in one
tree. The shell is twelve AXI4-Lite blocks, each owned by exactly one RTL
module; `tests/` is one cocotb directory per block; the firmware sits behind
two deliberate seams; and external SoC IP is consumed **read-only via env
vars**, not vendored. The refactor is therefore mostly *lifting along seams
that already exist*, not carving new ones.

**Goal.** Extract the reusable pieces the user named — the **SWD bit-bang
accelerator**, the **serial (UART-over-Ethernet) bridge**, the **Ethernet
MAC-verify subsystem**, and the **reusable firmware/host core** — into
standalone SoCLabs component repos (`git.soton.ac.uk/soclabs/…`), pulled back
into the platform as `deps/` git submodules, matching the house layout already
used by sibling repos (`ethernet-mac-ahb`, `tidelink`, `ptp-hardware-clock-ahb`).

**Why.** Independent versioning + CI per component; reuse across other SoCLabs
boards/projects; fault isolation; contract-enforced boundaries; and a set of
concrete robustness fixes (register-map drift, DUT-onboarding friction, licence
provenance) that are easier to land once each component owns its own repo.

**The one prerequisite that unlocks the rest:** move the register map from a
hand-mirrored Markdown+C pair to a **per-IP SystemRDL source of truth** (as the
sibling repos already do via `scripts/rdl2c.py`). That single change collapses
the biggest cross-repo coupling — the `platform_regs.h` ↔ `shell-regmap.md`
lockstep — into a generator each subrepo runs itself.

---

## 2. What we have built (inventory the refactor must preserve)

Maturity legend (same axis as `PLATFORM_LIVE_STATUS.md`):
**[IMPL]** synth+impl+timing-met in Vivado 2024.1 · **[PRV]** linked into the
DFX static, `pr_verify` COMPATIBLE · **[SYNTH]** OOC-synthesized only ·
**[SIM]** cocotb/VCS-verified RTL · **[STUB]** placeholder ·
**✅HW** proven on the physical KU115 (the project lead's live-lab session).

### Hardware (FPGA)

| Thing | State | Evidence |
|---|---|---|
| **Static shell** (MicroBlaze + lwIP + HWICAP + DFX decoupler/shutdown-mgr + AXI-EMC→LAN9220 + Debug Bridge + QSPI + clk_wiz + 6 custom IP) | **[IMPL]** WNS +2.472 / +2.591 ns, 8.4 MB bit + XSA, 256 KiB LMB | `fpga/shell/build_results_2026-07-07-256k/`, `…-08/` |
| Shell config on real board, pings on the network | **✅HW** | `PLATFORM_LIVE_STATUS.md` §3 (idcode `1390d093`, ping `192.168.10.101`) |
| **Monolithic** nanosoc-on-MPS3 (non-DFX baseline) | **[IMPL]** WNS +4.128 ns, 7.87 MB bit, `hello_word.hex` in IMEM | `fpga/monolithic/build_results_2026-07-04/` |
| **DFX mechanism** — greybox/led/nanosoc/eth_ss vs one locked static | **[PRV]** all `pr_verify` COMPATIBLE (97 partition pins), WNS +2.337 ns, clearing+partial pairs emitted; `static_id 0xECCEDBF3` | `fpga/dfx/prod_results_2026-07-07-256k/` |
| DFX partial reconfig on real board (JTAG: greybox→led→nanosoc→regdemo) | **✅HW** | `PLATFORM_LIVE_STATUS.md` §4 |
| **Over-the-wire DFX reconfig on real silicon** — host→Ethernet, 1.31 MB partial, RP swapped, `RM_ID` verified, network survives, ~570 KB/s | **✅HW** | commit `2f8813d` (2026-07-09) |
| **rm_nanosoc** (Cortex-M0 SoC as a DUT partial) | **[SYNTH]+[PRV]** 7,935 LUT / 4,050 FF / 16.5 BRAM / 3 DSP | `fpga/rp/nanosoc/` |
| **rm_eth_ss** (AHB-MAC+PTP subsystem DUT) | **[SYNTH]+[PRV]** 1,769 LUT / 2,178 FF / 2 BRAM | `fpga/rp/eth_ss/` |
| 6 custom shell IP (`clkrst`, `dfx_ctl`, `board_gpio`, `swd_bb`, `telem`, `uart_bridge`) | **[RTL]**, ride into the **[IMPL]** shell; unit benches partial | `fpga/shell/ip/*` |
| Ethernet MAC-verify blocks (`rmii_phy_if`, `mdio_phy_model`/VPHY, `link_partner_mac`, `bridge`, `gen_checker`) | **[SIM]** green under VCS+cocotb; **not yet in the shell BD** | `fpga/ethernet/*` |
| `lan9220_if` fabric leaf, `telem` INA228 I2C engine, `clkrst` DRP FSM | **[STUB]** by decision | — |

### Software

| Thing | State | Evidence |
|---|---|---|
| **MicroBlaze firmware** — lwIP RAW superloop, swap FSM, 11 modules | cross-compiles to a **real 239,868 B ELF**; 357+ host-gcc checks / 24 binaries | `firmware/`, `firmware/platform/BUILD_RESULT.txt` |
| **pyverify** host library (client/overlay/swap/console/debug/edge/pusher + fakeshell) | 135–148 pytest, zero-dep package | `host/pyverify/` |
| **Contracts** (partition-pins, partition-timing, shell-regmap, net-protocol, overlay-manifest) | frozen, versioned | `docs/contracts/` |

> **Honesty line:** everything above except the **✅HW** rows is a Vivado-model
> or host-test result. As of commit `2f8813d` (2026-07-09) the **reconfiguration
> half is silicon-proven end to end** — host → Ethernet → HWICAP → RP swap →
> `RM_ID` verify, no JTAG. What remains unproven on hardware is the **DUT-boot**
> half: a real nanoSoC booting inside the RP and its own ethernet subsystem
> coming up (the VPHY/`eth_ss` path of §8.2 in `PLATFORM_LIVE_STATUS.md`).

---

## 3. Current coupling — where the seams already are, and where they snag

**Already clean (the refactor leans on these):**

- **12 AXI4-Lite blocks, 1 owner each** (`docs/contracts/shell-regmap.md`):
  `CLKRST DFXCTL HWICAP VPHY OVLSTORE TELEM GENCHK SWDBB DBGBR UARTBR GPIO
  MMCM_DRP`. This *is* the decomposition.
- **Two firmware seams.** The network seam `common/net_if.h` (non-blocking
  TCP/UDP; three backends — the host in-memory fake, the lwIP glue, and
  `firmware/test/posix_net_if.c`, real POSIX sockets so a real external client
  can drive real firmware protocol code board-free) and the
  register HAL `common/platform_regs.h` (`mps3_reg_{read,write,set,clr}32` →
  MMIO on target, mock in tests). **No firmware module outside
  `firmware/platform/` includes an lwIP or Xilinx header.**
- **Per-component tests.** `tests/<block>/` each reference their RTL by a single
  relative path (`../../fpga/…`) — a bench moves cleanly with its block.
- **External IP is env-var-resolved, never vendored** (`ARM_IP_LIBRARY_PATH`,
  `CMSDK_DIR`, `NANOSOC_*`, `ETH_SS_*`) — the `nanosoc-zc702-fpga` precedent.
- **Host↔firmware coupling is contract-mediated**, not code-shared (JSON verbs +
  manifest schema; the host never sees the register map).

**The sharp edges (what a split must fix, not inherit):**

1. **`platform_regs.h` ↔ `shell-regmap.md` are a hand-maintained mirror.** The
   header even documents a real HWICAP bit-swap bug of exactly this drift class.
   → the SystemRDL prerequisite (§6.1).
2. **Central registries that "know about" every component**, each a single file
   edited per-block: `tests/common/list_benches.py` (BENCHES dict),
   `tests/conftest.py` (ignore globs), `tests/Makefile` (BENCH_DIRS), top
   `Makefile` (`LINT_SV`), `fpga/dfx/rm_list.tcl` (RM registry).
3. **Shared cocotb spine** `tests/common/` (`regmap.py::AxiLiteMaster`, SVA
   protocol/CDC checkers) — every bench depends on it.
4. **Lockstep points** enforced by tests, not imports: `pyverify.fakeshell`
   is byte-conformant to the firmware response encoder; `DEFAULT_CLK_PRESETS`
   mirrors `firmware/clkrst/clkrst.c`; the `static_id` CRC-32 scheme is shared
   by DFX-build + firmware + host.
5. **`sys.path` reach-ins** (`tests/integration/` imports `host/pyverify` by
   relative path) — break across repo boundaries.
6. **The three port servers include `coordinator.h`** only to read one
   `g_shell_state.<x>_gated` flag — the sole non-`common` coupling in
   otherwise-liftable modules.

---

## 4. Target topology

House convention (from `ethernet-mac-ahb`, `tidelink`, `ptp-hardware-clock-ahb`):
each component is a repo at `git.soton.ac.uk/soclabs/<name>.git` with
`src/{rtl,rdl,sw} · cocotb/ · uvm/ · flist/ · lint/ · formal/ · syn/ ·
sys_desc/ · scripts/ · set_env.sh · README.md`, dependencies pulled as git
submodules under `deps/`.

```
mps3-nanosoc-platform (thin integrator: BD, floorplan, board XDC, top build)
├── deps/
│   ├── soclabs-mps3-fw-common      (net_if, crc32, diag, net_proto, HAL header)
│   ├── soclabs-swd-bitbang         (swd_bb RTL + swd_server fw + openocd cfg)
│   ├── soclabs-uart-over-eth       (uart_bridge RTL + uart_over_eth fw)
│   ├── soclabs-xvc-debug-bridge    (xvc_server + DBGBR wiring)
│   ├── soclabs-lan9220-driver      (smsc911x, Apache-2.0 provenance)
│   ├── soclabs-eth-mac-verify      (all fpga/ethernet/* + VPHY + gen_checker)
│   ├── soclabs-mps3-pyverify       (host library — already a package)
│   ├── soclabs-mps3-contracts      (the frozen interfaces + SystemRDL)
│   └── soclabs-mps3-dut-sdk        (floorplan, RM template, greybox, partition XDC)
└── (integration-only) shell BD, dfx build flow, monolithic, board bring-up
```

Consumed read-only via env var (unchanged, not submodules): Arm AAA IP,
`nanosoc_m0_soc`, `ethernet-subsystem-ahb`.

---

## 5. Proposed sub-repos

Ordered cleanest-first. "Abstraction needed" = the small shim that replaces an
in-tree coupling so the component builds standalone.

### 5.1 `soclabs-mps3-fw-common` — the reusable firmware core
- **Contents:** `firmware/common/{net_if,crc32,diag,net_proto}.{c,h}` + the HAL
  contract header (`mps3_reg_*32` prototypes). The pure-logic heart every other
  firmware module sits on.
- **Deps:** none but `<stdint.h>`/`<string.h>`. Ships its host-test fakes
  (`fake_net_if.c`, `mock_regs.c`).
- **Verdict:** lift as-is. Optionally split a generic JSON-line codec from the
  MPS3 verb table in `net_proto` so the codec is reusable beyond this protocol.
- **Effort:** S.

### 5.2 `soclabs-mps3-pyverify` — the host library
- **Contents:** `host/pyverify/` verbatim (client, overlay, swap, console,
  debug, edge, pusher, `testing/fakeshell`). Already `pyproject.toml`, zero
  runtime deps, Python ≥3.10.
- **Deps:** the contracts repo (for the wire schema it must stay conformant to).
- **Verdict:** cleanest cut in the whole tree. The only knot is the
  `tests/integration/` `sys.path` reach-in — replace with a normal package
  import once installed.
- **Effort:** S.

### 5.3 `soclabs-lan9220-driver` — the Ethernet MAC driver (`smsc911x`)
- **Contents:** `firmware/smsc911x/` + `fake_lan9220.c` chip model + its
  Apache-2.0 provenance note (Zephyr-derived — **licence travels with it**).
- **Deps:** only the 4-function HAL (`mps3_reg_*32`); base address is a
  **runtime argument**, so zero coupling to the shell register map.
- **Verdict:** the most self-contained module in the tree. pbuf glue stays
  external (in the platform's lwIP backend), by design.
- **Effort:** S.

### 5.4 `soclabs-swd-bitbang` — the SWD bit-bang accelerator (named component)
- **Contents:** RTL `fpga/shell/ip/swd_bb/swd_bb.sv` (dumb pin-wiggler:
  DRIVE write-through + 2-FF SAMPLE) · firmware `firmware/swd_server/`
  (OpenOCD `remote_bitbang` byte protocol, TCP 6920) · host
  `host/openocd/swd_remote_bitbang.cfg` · bench `tests/swd_bb/` · the SWDBB
  regmap slice (SystemRDL).
- **Deps:** fw-common; the SWDBB SystemRDL block.
- **Abstraction needed:** replace the two `mps3_reg_*32` pokes with a tiny "SWD
  pin HAL"; replace the direct `g_shell_state.swd_gated` read with an injected
  gate predicate. That is the *only* non-common coupling.
- **Why it's a great first vertical slice:** it spans host+fw+RTL+contract in
  ~430 lines total, with the bit-order open-issue (I22) already isolated behind
  one lookup — a self-contained, reusable "SWD-over-Ethernet probe" IP.
- **Effort:** M.

### 5.5 `soclabs-uart-over-eth` — the serial console/trace bridge (named component)
- **Contents:** RTL `fpga/shell/ip/uart_bridge/{uart_bridge,uartbr_async_fifo,
  swo_uart_rx}.sv` (AXIS⇄FIFO, 5 gray-pointer CDC FIFOs, NRZ SWO deserialiser)
  · firmware `firmware/uart_over_eth/` (UART0/1/SWO ↔ TCP 6930/6931/6932) ·
  host `host/console/` configs · bench `tests/uart_bridge/` · UARTBR SystemRDL
  slice.
- **Deps:** fw-common; UARTBR SystemRDL block.
- **Abstraction needed:** a "stream FIFO HAL" + injected gate flag (same recipe
  as SWD).
- **Effort:** M.

### 5.6 `soclabs-xvc-debug-bridge` — Xilinx Virtual Cable over Ethernet
- **Contents:** `firmware/xvc_server/` (XVC v1.0, TCP 2542) + the DBGBR
  (Debug Bridge) wiring/regmap slice + bench.
- **Abstraction needed:** a "JTAG shift HAL" + injected gate flag.
- **Note:** DBGBR offsets are PG245-pattern and flagged bring-up-pending —
  carry that flag with the repo.
- **Effort:** M.

### 5.7 `soclabs-eth-mac-verify` — the Ethernet MAC-in-operation subsystem (named component)
- **Contents:** all of `fpga/ethernet/` — `bridge/eth_bridge_3port` (the
  "ethernet bridge" the user named), `rmii_phy_if`, `mdio_phy_model` (the
  **virtual PHY**, VPHY), `link_partner_mac`, `gen_checker`, `lan9220_if`, and
  the integrator `eth_mac_test_subsystem.sv`; benches `tests/{bridge,
  eth_mac_subsystem,mdio_phy_model,rmii_phy_if,gen_checker,link_partner_mac}/`;
  VPHY + GENCHK SystemRDL slices.
- **Deps:** fw-common (for the driver side later); contracts; the shared cocotb
  spine (§6.3).
- **Verdict:** internally cohesive (the subsystem top instantiates 5 siblings),
  so a **single** subrepo, not six. Aligns with the existing sibling
  `ethernet-subsystem-ahb`. Currently **[SIM]** and not yet in the shell BD —
  extracting it now (while it is standalone) is *easier* than after it is wired
  in.
- **Effort:** M–L.

### 5.8 `soclabs-mps3-contracts` — the shared interface repo
- **Contents:** `docs/contracts/*` + the **SystemRDL register source** (§6.1)
  from which each block's `_regs.h`/RTL decode is generated.
- **Consumed by:** every other repo (RTL, firmware, host all validate against
  it). Co-versioned so a contract bump is one visible submodule bump.
- **Effort:** S (move) + M (SystemRDL adoption, §6.1).

### 5.9 `soclabs-mps3-dut-sdk` — the DFX/DUT onboarding kit
- **Contents:** `fpga/dfx/dfx_floorplan.xdc` (resolved + frozen envelope),
  the RM wrapper template (`rp_nanosoc_wrapper.sv` port block +
  `rms/README.md` checklist), `rm_greybox` as the known-good reference,
  `partition-pins.md` + `partition-timing.md`, and the exported **locked static
  DCP** + context/OOC constraints so a DUT designer can place/route/`pr_verify`
  **offline**. See §7.3.
- **Effort:** L (needs the two automations in §6.4).

### Not extracted (stay in the integrator)
`coordinator/` (the shell hub — inherently platform-specific; only
`swap_fsm_transitions.c` is a pure liftable), `firmware/platform/` (the porting
layer everything sits on), the shell BD / dfx build flow / monolithic / board
XDC / bring-up. `harness_app/` is legacy — salvage its `updatemem` docs, retire.

---

## 6. Cross-cutting enablers (do these first / alongside)

### 6.1 SystemRDL as the single register source of truth ★ (the keystone)
Today `shell-regmap.md` (Markdown) and `platform_regs.h` (C) are hand-kept in
sync — the highest-value robustness fix in this plan. Adopt the sibling pattern:
one `.rdl` per block, `scripts/rdl2c.py`-style generation of the C header **and**
the RTL address decode **and** a docs table. Each subrepo then generates its own
register view from the contracts submodule — the `platform_regs.h` lockstep
(coupling #1) disappears, and register drift becomes a build error, not a
silicon bug.

### 6.2 The firmware HAL contract
Publish `mps3_reg_{read,write,set,clr}32` + a per-server pin/stream/JTAG HAL as
a small header contract, and add a **gate-hook injection** (a function pointer /
weak predicate) to replace the three port servers' direct `coordinator.h`
include. This is the shim that lets swd/uart/xvc build standalone.

### 6.3 Decompose the central registries
Replace the hand-maintained global lists with per-component **manifests** the
tooling discovers: each component repo declares its bench + lint set; the
integrator aggregates. Kills coupling #2/#3 (the `list_benches.py`/`conftest`/
`LINT_SV`/`rm_list.tcl` edit-every-time files) and lets `tests/common/` ship as
a versioned `soclabs-cocotb-support` package rather than a copied spine.

### 6.4 Two DUT-onboarding automations the repo already TODO's
- **`pin_check` lint** (`rm_list.tcl` TODO): mechanically diff each RM wrapper's
  ports against `partition-pins.md` so drift fails fast in CI, not late at
  `pr_verify`.
- **`_rm.xdc` reapply** into `build_dfx.tcl` (`partition-timing.md` follow-up):
  so RM-internal async crossings (e.g. eth_ss WB↔MII) are signed-off at link.

### 6.5 Contract-conformance as inter-repo tests
The existing lockstep tests (fakeshell byte-conformance, clk-preset mirror,
`static_id` scheme) become **cross-repo contract tests** run in each repo's CI
against the contracts submodule — turning today's "remember to update both
sides" into an enforced gate.

---

## 7. Robustness & modularity assessment — what this actually buys

### 7.1 Robustness
- **Register drift → impossible.** SystemRDL makes RTL, firmware, and docs one
  generated artifact (§6.1). Removes the single most dangerous class of bug in
  the tree (the documented HWICAP bit-swap was exactly this).
- **Boundaries become enforced, not remembered.** Contract-conformance CI
  (§6.5) + `pin_check` (§6.4) convert manual lockstep into build gates.
- **Fault isolation.** A break in the ethernet subsystem can't red-bar the SWD
  probe's CI; each component has its own green/red.
- **Licence hygiene.** The Apache-2.0 provenance of `smsc911x` travels with its
  own repo instead of being a footnote in a monorepo.
- **DUT onboarding stops being tribal.** The DUT-SDK (§5.9/§7.3) gives external
  SoC designers a buildable, `pr_verify`-able target offline.

### 7.2 Modularity / reuse
- Each named component becomes a **portable IP**: "SWD-over-Ethernet probe",
  "UART/SWO-over-Ethernet bridge", "virtual-PHY MAC-verify subsystem", "MPS3
  firmware core" — reusable on other SoCLabs boards, not welded to this shell.
- **Independent versioning + CI** per component; the platform pins known-good
  submodule SHAs and bumps deliberately.
- The platform repo shrinks to what it actually is — **a board-support +
  integration harness** — matching its own README's self-description.

### 7.3 The DUT-SDK, specifically (answers "how do constraints reach designers")
Today a designer gets contracts + a wrapper template + OOC scripts, but **no
offline build target** and **no pin lint** — drift surfaces late. Package the
pieces the flow already produces into an SDK: the **locked static DCP** +
**resolved pblock/timing context** + **wrapper template** + **greybox
reference**; freeze the pblock envelope (42,824 LUT / 144 BRAM / 432 DSP in
SLR0) *and* the partition-pin locations; ship the UltraScale pitfall list
(clearing+partial pairs mandatory, no `RESET_AFTER_RECONFIG`, single-SLR,
`PERSIST NO`, no pin-facing IOB/clock-gen in an RM, `.bin` variable-length
header, `dut_clk` is a DRP placeholder). That makes "target our harness" a
repo you clone, not a conversation.

### 7.4 Costs / downsides (evaluate honestly)
- **Submodule friction.** Multi-repo checkout, SHA bumps, and cross-repo PRs are
  more ceremony than one tree. Mitigate with a top-level `make deps` + pinned
  SHAs + a meta-CI that builds the integrator against the pinned set.
- **Contract-change fan-out.** A boundary change now touches N repos. That is
  the *point* (visibility), but it is slower — reserve it for real interface
  changes and keep the contracts repo the choke point.
- **Migration risk window.** Files in motion + green build to preserve. Phasing
  (§8) keeps each step independently shippable and reversible.
- **Not everything should move.** The coordinator/platform/BD are integration,
  not components — forcing them into repos would create false modularity.

---

## 8. Phased migration (each phase ends green)

**Phase 0 — freeze & keystone (no files move).**
Freeze `docs/contracts/` at a versioned tag; stand up
`soclabs-mps3-contracts`; adopt SystemRDL (§6.1) generating today's
`platform_regs.h` byte-identically (prove the generator against the current
header). Add `pin_check` + `_rm.xdc` reapply (§6.4). *Deliverable: same build,
now generated + gated.*

**Phase 1 — leaf extractions (lowest risk).**
`fw-common`, `pyverify`, `lan9220-driver`, `contracts`. Each has near-zero
inbound coupling. Wire back as submodules; `make check` stays green.

**Phase 2 — vertical slices.**
`swd-bitbang`, `uart-over-eth`, `xvc-debug-bridge` — each with the HAL + gate-
hook shim (§6.2). These are the "named reusable components"; landing them proves
the vertical-slice pattern (host+fw+RTL+contract+bench in one repo).

**Phase 3 — the ethernet subsystem.**
`eth-mac-verify` as one repo (do it *before* it's wired into the shell BD).
Move the shared cocotb spine to a versioned support package (§6.3).

**Phase 4 — DUT-SDK.**
Export the locked static + context; publish `soclabs-mps3-dut-sdk`.

**Phase 5 — platform becomes the thin integrator.**
Decompose the central registries (§6.3); the platform repo now = BD + floorplan
+ board XDC + top build + `deps/` submodules. Retire `harness_app/`.

Rollback at any phase = un-bump the submodule / re-inline the directory; nothing
is deleted upstream until the integrator has been green against the extracted
repo for one full `make check` cycle.

---

## 9. Risks & open questions

- **SystemRDL fidelity.** Must reproduce every current offset/bitfield exactly
  (incl. the two AXI-Lite handshake styles and the HWICAP packing) — gate Phase
  0 on byte-identical output vs today's `platform_regs.h`.
- **Ownership overlap.** `fpga/mps3_sd/` + the boot/live-status docs are
  the project lead's parallel track — the extraction stays out of those paths.
- **`static_id` churn.** Any static-netlist change re-mints `static_id` and
  invalidates stored partials; keep the shell BD in the integrator so extraction
  never perturbs the static.
- **Submodule vs subtree.** Submodules match the house pattern; if cross-repo
  churn proves painful, `git subtree` is the fallback. Decide before Phase 1.
- **Repo naming / hosting.** Confirm the `git.soton.ac.uk/soclabs/…` names and
  whether the ethernet subsystem should nest under the existing
  `ethernet-subsystem-ahb` org path.

---

## 10. Appendix — component → files → target repo

| Component | RTL | Firmware | Host | Tests | Target repo |
|---|---|---|---|---|---|
| SWD bit-bang | `fpga/shell/ip/swd_bb` | `firmware/swd_server` | `host/openocd` | `tests/swd_bb` | `soclabs-swd-bitbang` |
| UART/SWO bridge | `fpga/shell/ip/uart_bridge` | `firmware/uart_over_eth` | `host/console` | `tests/uart_bridge` | `soclabs-uart-over-eth` |
| XVC bridge | (Debug Bridge cell) | `firmware/xvc_server` | — | (xvc bench) | `soclabs-xvc-debug-bridge` |
| Ethernet MAC-verify | `fpga/ethernet/*` | (driver later) | — | `tests/{bridge,eth_mac_subsystem,mdio_phy_model,rmii_phy_if,gen_checker,link_partner_mac}` | `soclabs-eth-mac-verify` |
| LAN9220 driver | — | `firmware/smsc911x` | — | (in fw-common tests) | `soclabs-lan9220-driver` |
| Firmware core | — | `firmware/common` | — | `firmware/test/*` | `soclabs-mps3-fw-common` |
| Host library | — | — | `host/pyverify` | `host/pyverify/tests` | `soclabs-mps3-pyverify` |
| Contracts + regs | (regdecode gen) | (regs gen) | (schema) | conformance | `soclabs-mps3-contracts` |
| DUT-SDK | `fpga/dfx/*`, `fpga/rp/*` templates | — | — | — | `soclabs-mps3-dut-sdk` |
| **Integrator (stays)** | shell BD, monolithic, floorplan, board XDC | `coordinator`, `platform` | tender/notebooks | `tests/integration` | `mps3-nanosoc-platform` |

---

*Evaluate, edit, then we execute Phase 0. Nothing here moves a file until the
contracts freeze + SystemRDL generator are proven byte-identical to today.*
