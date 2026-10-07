# MPS3 nanoSoC Verification Platform — Multi-Agent Implementation Plan

> **Status: HISTORICAL** — a record of the original multi-agent implementation plan (spec v2.1 era) as of 2026-07-04.
> Superseded by / current state in [docs/planning/PLATFORM_COMPLETION_PLAN.md](planning/PLATFORM_COMPLETION_PLAN.md) and [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

**Version:** 1.0 (2026-07-03)
**Derived from:** `mps3_nanosoc_verification_platform_architecture.md` (spec v2.1)
merged with `MPS3_PI_TENDER_TIER2_PLAN.md` (tender v2) + `DFX_PYNQ_PLAN.md`
(Z2 DFX feasibility, PROVEN 2026-07-02) + three days of fact-checked research
(memory: `mps3-pynq-feasibility`, `dfx-zynq-nanosoc-partition-proven`).
**Purpose:** the executable plan — phases, parallel agent workstreams,
interface contracts, acceptance gates, and what we reuse from existing repos.

---

## 0. Alignment verdict & deltas against the spec

The spec (v2.1) and our Tier-2 tender plan describe **the same architecture**
("disaggregated PYNQ": persistent static shell + DFX RP + external Linux
host). The spec goes further than Tier-2 in the right direction — it commits
to the **on-fabric config agent (MicroBlaze + lwIP + HWICAP over the
LAN9220)** as the production inner loop, where Tier-2 had deferred on-fabric
delivery to an optional phase. We adopt the spec as the target architecture,
with the following deltas (corrections and accelerants) folded in:

### 0.1 Technical corrections to the spec (fact-checked against UG909/UG570)

| # | Spec says | Correction |
|---|---|---|
| C1 | §7/§14 use `RESET_AFTER_RECONFIG` | That property is **7-series-only**. On UltraScale, post-reconfig GSR is automatic **but contingent on the clearing bitstream**; keep `SNAPPING_MODE ON` + decoupler + shell-held RP reset |
| C2 | Clearing bitstreams **absent from the spec** | **Mandatory on UltraScale (not US+)**: before each new partial, the *currently-loaded* RM's clearing bitstream must be sent. Every overlay artefact is a **triple** `{clearing.bin, partial.bin, meta}`; the config agent, overlay store A/B slots (§8A), and host pusher must all carry and sequence pairs. Post-power-on state = the greybox's clearing bitstream (ships with the shell) |
| C3 | — | `BITSTREAM.CONFIG.PERSIST` and ICAP are mutually exclusive — keep PERSIST off in the shell build |
| C4 | — | One-time check: MCC must not strap config mode pins JTAG-only (M[2:0]=101) — disrupts partial delivery via other ports incl. ICAP (UG909 Table 14) |
| C5 | §16 "keep RP within one SLR" | Confirmed + sharpened: KU115 = 2-SLR SSI; also keep **ICAP/HWICAP in the same SLR as the RP** (master-SLR ICAP limits), and note SSI caps bootstrap JTAG at 20 MHz |

### 0.2 Existing assets the spec doesn't know about (accelerants)

| Spec item | We already have |
|---|---|
| Host "tender" (MCC USB-MSD + serial, §4.3) | **fpgahub `sd_install` + `mps3_mcc_reboot` plugins — built and hardware-proven** on the real MPS3; plus board leases, probe management, `probe_mps3_mcc.sh`, DEPLOY.md |
| Phase-1 "nanosoc on MPS3 monolithic" | **Two head starts:** (a) legacy single-core nanosoc MPS3 target in `nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/` (pinmap+timing XDC, wrapper, 2021.1 tcl — includes the **LAN9220 SMC pin wiring**: `ETH_nCS/ETH_nOE/ETH_INT`); (b) the eth-ss MPS3 target (`ethernet-subsystem-ahb/fpga/targets/arm_mps3/` — complete BD/XDC/Make/fpgahub flow, April-2026 vintage) |
| DFX flow | **Proven on Z2 2026-07-02** (`pynq/dfx/` + `docs/DFX_PYNQ_PLAN.md`): whole-SoC RP routed, timing met (WNS +1.92 ns), pr_verify clean, partials emitted. Transferable lessons: pin-facing IOB-packed registers must move to static (HDPR-29); checkpoint-link needs manual top clock; boundary = slow scalars is ideal |
| RMII virtual PHY (§8) | `rmii_to_mii` bridge RTL + LAN8720 bring-up experience + eth-ss cocotb bench (MII/RMII models, `cocotbext-eth` usage) — §8's datapath is a hardware re-plumb of things this team has already debugged |
| DUT MAC to verify (§8) | Our **eth-ss AHB MAC + HA1588 PTP** is a second, more demanding DUT MAC beyond single-core nanosoc's; PTP timestamping makes the error-inject/checker subsystem earn its keep |
| SWD tooling | J-Link EDU Mini + openocd flows (DAP-direct scripts) = the §10 "first bring-up" physical path, already scripted |
| Python verification layer (§4.3, phase 10) | fpgahub actions/lease API + the demo-GUI HAL channels + cocotb harnesses to port vectors from |
| Bootstrap partial delivery before the shell exists | Tier-2 P1 "XVC-everything": FTDI JTAG + xvcd on the tender → remote Vivado programs full+partial+ILA; plus VIO/BSCANE2 decouple control. This lets **DFX floorplan work proceed in parallel with (not blocked by) shell networking firmware** |

### 0.3 Scope note

The spec's DUT is **single-core nanosoc (Cortex-M0)**. We treat the RP as
DUT-agnostic from day one: RM library = `rm_greybox`, `rm_nanosoc` (spec's
DUT), `rm_eth_ss` (our MAC-under-test), later `rm_nanosoc_multicore` (needs
the larger Pblock — measure first, D7). This changes floorplan sizing only.

---

## 1. Repository & ownership model

New standalone repo **`mps3-nanosoc-platform`** exactly per spec §17 layout
(pattern precedent: `nanosoc-zc702-fpga` — standalone board-support repo
consuming packaged IP/firmware from source repos via env vars). Source repos
(`nanosoc_arch_tech`, `ethernet-subsystem-ahb`, `fpgahub`, Arm AAA IP) are
consumed read-only; fpgahub gains plugins/actions upstream in its own repo.

**Interface contracts** (frozen early, versioned in `docs/contracts/`, owned
by the Integrator agent) — these are what let agents work in parallel:

| Contract | Content | Producers/consumers |
|---|---|---|
| `partition-pins.md` | RP boundary: SWD(3), RMII+MDIO(9), UART/SWO AXIS, clk(s), 3 resets, optional MMIO/ADP — names, widths, directions, clock domains, CDC ownership | shell-RTL ↔ dfx-flow ↔ RM wrappers |
| `shell-regmap.md` | MicroBlaze-visible register map: clk DRP window, reset ctrl, decoupler, ICAP status, VPHY/MDIO model ctrl, telemetry | shell-RTL ↔ shell-FW |
| `net-protocol.md` | TCP/TFTP port map + framing: config push (incl. **clearing+partial pair sequencing**, C2), commit, SWD remote_bitbang chars, XVC, UART/SWO ports, status/telemetry | shell-FW ↔ host-py |
| `overlay-manifest.md` | Artefact set schema: `{static_id, rm_id, clearing.bin, partial.bin, crc32, sizes, ltx?, fw?}` + A/B slot header | dfx-flow ↔ shell-FW ↔ host-py |

---

## 2. Agent roster

| Agent | Owns | Primary skills |
|---|---|---|
| **A1 shell-RTL** | Static shell BD/RTL: clock/reset unit, LAN9220 AXI-EMC, bridge, HWICAP, decoupler/shutdown, Debug Bridge, VPHY datapath, relays (HW side), telemetry | Vivado BD, RTL, CDC |
| **A2 dfx-flow** | DFX floorplan/scripts, RM builds (greybox/nanosoc/eth-ss), clearing+partial artefact generation, pr_verify, bootstrap JTAG/XVC delivery | Vivado DFX (ports `pynq/dfx/`), tcl |
| **A3 shell-FW** | MicroBlaze bare-metal: lwIP, smsc911x port (Zephyr-derived), config agent, XVC server, SWD remote_bitbang server, UART/SWO relays, overlay store A/B, coordinator/watchdog | Embedded C, lwIP |
| **A4 host-py** | Tender image + fpgahub plugins/actions, pusher, pyverify library, OpenOCD/pyOCD configs, ser2net, Jupyter notebooks, orchestrator | Python, fpgahub, OpenOCD |
| **A5 verify** | cocotb benches for every new RTL block (VPHY, MDIO slave, bridge, relays, ICAP-loader path), error-inject gen/checker, integration test suites, CI | cocotb, verification |
| **A6 integrator** | Contracts (§1), repo scaffold, phase gates, cross-agent review, licensing audit (Arm AAA entitlement, Zephyr-vs-GPL driver choice), docs | Review, glue |

Working style: one phase = a set of parallel workstreams with explicit
contract dependencies; every workstream lands via PR reviewed by A6 +
affected peers; **hardware acceptance is the phase gate** (sim green alone
does not close a phase). RTL blocks get cocotb benches (A5) before hardware.

---

## 3. Phases

Sequencing principle: **two independent tracks run in parallel** —
Track α (FPGA: monolithic target → DFX floorplan → shell RTL) and
Track β (tender/host + shell firmware) — meeting at Phase 3 (networked swap).
The Tier-2 bootstrap path (JTAG/XVC + VIO) keeps Track α unblocked while
Track β builds the Ethernet stack.

### Phase 0 — Foundations (all agents, ~1–2 wk, fully parallel)

| WS | Agent | Work | Acceptance |
|---|---|---|---|
| 0.1 | A6 | Repo scaffold per §17; contracts v0 drafted; licensing audit (Arm AAA pull for Cortex-M0/CMSDK; Zephyr smsc911x vendored) | Contracts merged; CI skeleton runs |
| 0.2 | A4 | **Tender base on Pi 5** (Tier-2 WP-T): fpgahub node + openocd + openFPGALoader + xvcd + udev/leases; validate against a **PYNQ-Z2 first** (deploy-over-SSH + XVC to on-board FT2232 + SWD) | Z2 managed end-to-end from the Pi; remote ILA from a workstation via tender XVC |
| 0.3 | A2 | Board checks: C4 mode-pin check (TRM/schematic); openFPGALoader `--detect` on KU115; JTAG full-bitstream program timing; D15 (user µSD FPGA-wired?) | Checklist answered; KU115 programs over tender JTAG |
| 0.4 | A1 | Resurrect **monolithic nanosoc-on-MPS3** (spec phase 1): port legacy arch_tech `arm_mps3` target to Vivado 2024.1; MCC SD boot via fpgahub | nanosoc boots on MPS3, UART prints (via FT4232) |
| 0.5 | A5 | Bench scaffolding: cocotb env for platform blocks; import cocotbext-eth; smoke bench for MDIO slave stub | CI runs a first bench |

### Phase 1 — DFX inner loop, bootstrap transport (Track α; ~2–3 wk)

| WS | Agent | Work | Acceptance |
|---|---|---|---|
| 1.1 | A2 | **KU115 DFX shell v0**: RP Pblock (single SLR, sized for nanosoc + margin, D7 measured from 0.4), greybox RM, decoupler, `SNAPPING_MODE`, **clearing+partial artefact triples** (C1/C2), static VIO for DECOUPLE/RP_RESETN | `pr_verify` clean across greybox↔LED-counter RM |
| 1.2 | A2+A4 | **Swap over tender JTAG/XVC** (Tier-2 P1): headless xsdb script = VIO decouple → clearing → partial → release | Two swaps, shell (LEDs/clocks) undisturbed — **spec phase-3 acceptance met without Ethernet** |
| 1.3 | A1 | **Clock/reset unit** (spec §5): DRP MMCM DUT clock + 3 resets + CDC pattern library (spec phase 4) | Set DUT clock + reset via VIO/JTAG from host |
| 1.4 | A5 | Bench: decoupler/reset sequencing model; artefact-set CRC checks in CI | Green |

### Phase 2 — Shell networking (Track β, parallel with Phase 1; ~3–4 wk)

| WS | Agent | Work | Acceptance |
|---|---|---|---|
| 2.1 | A1 | LAN9220 host I/F (AXI EMC / SMC master — pin wiring cribbed from legacy arch_tech target) + MicroBlaze + BRAM + minimal 3-port bridge (Forencich crossbar per D9) | Synth clean on KU115 shell |
| 2.2 | A3 | MicroBlaze BSP: lwIP up, **smsc911x port from Zephyr** (Apache-2.0, C-licensing per A6 audit); echo + DHCP/static (D8) | **Spec phase-2 acceptance: board answers ping/echo, self-boots from SD** |
| 2.3 | A3 | **Config agent v1**: TFTP/TCP receiver (D3) → HWICAP; enforces clearing→partial pair order (C2); RM-load verify (ID reg + CRC) | Swap LED RMs **over Ethernet**; wrong-order/wrong-static rejected |
| 2.4 | A5 | Bridge + config-agent benches (frame routing, malformed input, torn transfer) | Green |

### Phase 3 — Converge: networked platform core (~2 wk)

| WS | Agent | Work | Acceptance |
|---|---|---|---|
| 3.1 | A2+A3 | Ethernet swap of real RMs on the v1 shell; retire JTAG path to fallback/recovery | **Spec phase-3 acceptance over Ethernet**; JTAG documented as recovery |
| 3.2 | A3 | **Overlay store** (§8A): AXI Quad SPI, A/B slots **storing clearing+partial pairs** (C2), boot-time default load, networked `commit`, SST26 block-protect handling (D13=QSPI, D14=greybox+NVM) | Spec phase-3A acceptance incl. interrupted-commit fallback |
| 3.3 | A4 | Host pusher + manifest tooling (`overlay-manifest.md`); fpgahub action `platform_deploy`; golden-image fallback via MCC (reuses `sd_install`) | Notebook cell swaps an overlay end-to-end |

### Phase 4 — nanosoc as RM + debug/console planes (~3–4 wk, three parallel WS)

| WS | Agent | Work | Acceptance |
|---|---|---|---|
| 4.1 | A2+A1 | **nanosoc RM** (spec phase 5): partition-pin wrapper per contract; IOB-packed pin-facing regs → static (Z2 lesson) | nanosoc partial loads; boots; prints via UART-over-Eth |
| 4.2 | A3+A4 | **UART/SWO-over-Ethernet** (spec phase 6, §11): AXIS tap → TCP relay; host ser2net/pyserial (D11=raw TCP) | Console scraped by test script across swaps |
| 4.3 | A3+A4 | **SWD-over-Ethernet v1** (spec phase 7, §10.1): remote_bitbang pin-wiggler + OpenOCD ≥2021-01; internal SWD partition pins; physical J-Link header retained for bring-up (existing cable-domain rule) | GDB over Ethernet: DPIDR, fw load, breakpoint, re-attach after swap |
| 4.4 | A1+A4 | **ILA-over-Ethernet** (spec phase 8): Debug Bridge (AXI→BSCAN) + XVC server on MicroBlaze; RM-internal ILAs behind the `dbgbscan` partition group (D4 revised 2026-09-23 — static-side ILA/VIO forbidden); gate XVC during reconfig | Vivado waveform over Ethernet; across a swap the XVC target is closed first and reopened with the new RM's `.ltx` after (`pyverify.debug.XvcSession`, `SwapOrchestrator(xvc=...)`) — the session does not survive a swap. Designed for the 2026-10 ILA mint; not on silicon yet |

### Phase 5 — MAC-in-operation verification subsystem (~3–4 wk)

| WS | Agent | Work | Acceptance |
|---|---|---|---|
| 5.1 | A1 | **RMII virtual PHY** (§8): LiteEth-derived `rmii_phy_if` flipped PHY-side; shell sources 50 MHz ref (ref-in on DUT) — leverages `rmii_to_mii` experience | Loopback frames at RMII |
| 5.2 | A1+A5 | **MDIO slave + PHY register model** (BMCR/BMSR/PHYID/ANAR; MicroBlaze-writable link events) — *bench-first* (A5 cocotb) | DUT PHY bring-up completes against model; host-injected link-down observed by DUT |
| 5.3 | A1 | Link-partner MAC (`eth_mac_mii` + `eth_axis_rx/tx`) into the 3-port bridge; LAN9220 promiscuous | **Spec phase-9 acceptance:** DUT MAC exchanges frames with host over LAN9220 |
| 5.4 | A5+A1 | **Error-inject gen/checker**: bad FCS/runt/giant/IFG cases as HW + mirrored cocotb cases | Injection cases detected/scored both sides |
| 5.5 | A2 | `rm_eth_ss` RM (our AHB MAC + PTP) as second DUT | eth-ss MAC passes the same suite |

### Phase 6 — Python layer, orchestration, robustness (~2–3 wk)

| WS | Agent | Work | Acceptance |
|---|---|---|---|
| 6.1 | A4 | **pyverify** library (spec phase 10): overlay mgmt, SWD (pyOCD), consoles, MAC-test driver, clk/reset control; Jupyter front-end; fpgahub lease integration | Notebook: "select DUT → load fw → run test → check" end-to-end |
| 6.2 | A4+A5 | Port ADP/cocotb vectors; regression runner (mirrors `fpga-regression` pattern); farm topology (D1) | Nightly platform regression green |
| 6.3 | A3+A1 | Robustness (§13): watchdog + shutdown-manager recovery, telemetry (INA228-class), DFX Bitstream Monitor, golden MCC image | Hung-DUT recovery without MCC reboot |
| 6.4 | A6 | Docs: build/run guides, contract freeze v1.0, external-facing summary | Docs shipped |

### Phase 7 (stretch/optional)

`rm_nanosoc_multicore` (bigger Pblock, re-floorplan); DDR4-stage + HBICAP
fast path (only if a faster NIC ever lands — D5); CMSIS-DAP smart SWD engine
(D10 v2); MMIO bridge; eMMC overlay library (D13 v2).

---

## 4. Decision log deltas (spec §15, now resolved/updated)

| ID | Resolution |
|---|---|
| D1 | Both: Pi-5 tender per bench pod **and** shared-server farm — same fpgahub node code (proven Phase 0.2) |
| D2 | Phase 0.4 = legacy arch_tech `arm_mps3` single-core target (exists, incl. LAN9220 pins) + eth-ss target as reference |
| D5 | HWICAP v1 confirmed; **bootstrap transport = tender JTAG/XVC** (new, enables Track α/β parallelism); DDR+HBICAP deferred |
| D7 | Measure via Phase 0.4/1.1; datapoint: multicore SoC = 29K LUT/43 BRAM on Z2 → single-core nanosoc well under; KU115 RP at ~2× largest-DUT still ≪ 1 SLR |
| D10 | v1 pin-wiggler confirmed; physical J-Link stays as the bring-up/soft-domain path (cable-domain rule, 2026-07-03) |
| D13/D14 | QSPI A/B + greybox confirmed; slots hold **clearing+partial pairs** (C2) |
| D15 | Still confirm-on-board (Phase 0.3); QSPI anchors v1 regardless |
| NEW D16 | Clearing-bitstream state tracking owner = shell coordinator (persisted beside A/B header), host mirrors it | 
| NEW D17 | RM library scope v1 = greybox, LED, nanosoc, eth-ss; multicore = Phase 7 |

## 5. Risk register (top additions to spec §16)

1. **Clearing-bitstream sequencing bugs** (C2) — the one UltraScale-specific
   failure mode with no Zynq precedent here; mitigated by bench-first config
   agent (2.4), artefact triples, and JTAG recovery path.
2. **LAN9220 SMC timing on KU115** — legacy target pins help, but the AXI EMC
   timing needs bench + ILA validation early (2.1 before 2.3 depends on it).
3. **MicroBlaze toolchain drag** (Vitis bare-metal) — contain via thin BSP,
   everything else portable C; A3 owns a repeatable build container.
4. **Two-track divergence** — contracts (§1) are the control; A6 gates any
   partition-pin/regmap change through both tracks.
5. **Arm IP entitlement** (spec §3) — confirm AAA pull for Cortex-M0/CMSDK
   before Phase 0.4 synth; eth-ss RM path (5.5) is entitlement-independent.
