# Next wave — agent-parallel work plan (in-repo scope, no hardware)

> **Status: HISTORICAL** — a record of one agent-parallel work wave and its launch state as of 2026-08-07.
> Superseded by / current state in [docs/planning/PLATFORM_COMPLETION_PLAN.md](planning/PLATFORM_COMPLETION_PLAN.md) and [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

**Version:** 1.2 (2026-07-06 — the seven-workstream wave marked LANDED;
batch-2 launch recorded). **Baseline:** the Phase-0.5 wave, since advanced
by commits `fbc9025`/`b3e8e99`/`3514315`/`ef22443` plus the uncommitted
wave in the working tree (`docs/STATUS.md` is the current picture).
**Purpose:** the concrete, parallelisable tranche of work that can be built
and *verified* entirely inside this repo on this machine — no MPS3 board, no
tender Pi, no fpgahub changes.

**What makes this tranche possible:** Vivado 2024.1 (+ Vitis) and VCS/Questa
are installed locally, the DFX mechanism is already proven on the KU115 part
(`fpga/dfx/proof/`), and every workstream below closes with a self-check that
runs headless. The two *enabler* workstreams (W-SIM, W-FAKESHELL) unblock
most of the rest — start them first.

**Hard scope exclusions** (unchanged from IMPLEMENTATION_PLAN.md):
bring-up items I17/I20–I22, Phase-0.2 tender-Pi install, fpgahub and
the Arm IP library are read-only, no real `edge-wrapperd` (Hub-side).
~~The real nanosoc SoC RTL (D2b)~~ — **no longer excluded: D2b is resolved**
(real single-core RTL at `~/SoCLabs/nanosoc_m0_soc`, consumed
read-only; see Progress below).

---

## Progress (updated 2026-07-06)

Status of this plan after the three post-baseline commits (`fbc9025`,
`b3e8e99`, `3514315`), the 2026-07-04 wave (**now LANDED** — see below),
and the batch-2 launch of 2026-07-06.

### Done — landed by `b3e8e99`/`3514315`, before the wave's agents report

| Item | What landed | Evidence |
|---|---|---|
| **W-RTL-CORE ≈ DONE** (via G3) | Real `dut_clkrst.sv` + `dfx_ctl.sv` AXI4-Lite bodies (resets, decouple/shutdown, RM_ID readback, `rp_resetn` gate); markers dropped, benches gate READY. Remaining sliver: I16 preset-table publication + actually running the benches (needs W-SIM). | `fpga/shell/ip/{clkrst,dfx_ctl}/`, `list_benches` READY |
| **board_gpio** (first quarter of W-RTL-NEWIP) | Real IN/OUT/OE/OWN-mux AXI4-Lite block. Quirk: its bench still reports SKIP — false-negative, the RTL's own comment contains the literal `"Phase 0 stub"` string that `dut_presence.rtl_ready()` greps for (one-line detector fix, W-SIM/A5). | `fpga/shell/ip/board_gpio/` |
| **W-MDIO RTL DONE** (via G4) | Real Clause-22 `mdio_slave` + `phy_reg_model` + AXI-Lite link-event injection; compiles/elaborates/**runs under VCS**. Bench NOT green — cocotb 2.0.1 ReadOnly-phase strictness breaks the 1.x-era shared drivers (now W-SIM's harness-port job). | `fpga/ethernet/mdio_phy_model/`, `tests/mdio_phy_model/results.xml` |
| **D7 datapoint DONE** (part of W-DFX-PROD) | Real (not stub) `rm_nanosoc` OOC utilisation measured: **7,935 LUT / 4,050 FF = 1.2 % of the KU115** — Pblock headroom is now a fact. | `fpga/dfx/proof/proof_results_2026-07-04/util_rm_nanosoc.rpt` |
| **Half of W-SIM's risk retired** | VCS 2022.06-SP2 license + full compile/elab/run pipeline proven (`+incdir` Makefile fix committed). Remaining = `set_env.sh`, cocotb python pinning (1.7.2 `~/.local` vs 2.0.1 miniconda), driver port, benches green. | `tests/mdio_phy_model/sim_build/`, `results.xml` |
| **Beyond this plan's scope** | The REAL nanosoc proven as a DFX partial on the locked proof static (config-3 `pr_verify` COMPATIBLE, WNS +6.05 ns, partial+clearing emitted); the monolithic nanosoc-on-MPS3 **full bitstream BUILT, timing MET** (WNS +4.13 ns, 7.87 MB, `hello_word.hex` in IMEM); D2b resolved + source survey; proof flow extended (`build_rm_nanosoc.tcl`, `ooc_synth.tcl`). | `fpga/dfx/proof/`, `fpga/monolithic/build_results_2026-07-04/`, `docs/nanosoc_m0_soc/` |

### LANDED — the 2026-07-04 wave, all seven workstreams (working tree, 2026-07-06)

Suite totals at landing (all re-run 2026-07-06, root `make check` green):
**135** host pytest · **70** cross-workstream pytest · **357** firmware C
checks / 8 binaries · benches: the 5 wave-baseline READY green under VCS.

| Agent | Workstream(s) | Outcome / evidence |
|---|---|---|
| **W-SIM** | Sim env + benches green | **DONE, I24 closed**: `set_env.sh` (miniconda py3.10 + cocotb 2.0.1 + VCS pinned; drivers ported to 2.x — 1.7.2 pinning was a mirage), `tests/sim_smoke/`, board_gpio marker fix. 5 benches green under VCS: sim_smoke 2/2, clkrst 5/5, dfx_ctl 4/4, board_gpio 4/4, mdio_phy_model 3/3 (`tests/README.md`) |
| **W-FAKESHELL** | Reference shell server | **DONE**: `pyverify.testing.{fakeshell,swap_model}` — all 7 verbs w/ per-op fields, TFTP:69 + raw:6910 receive, I2 ordering + **pair staging**, consoles; swap model importable + cross-checked vs the C. Its ambiguity flags drove contracts **v0.2** |
| **W-PUSH + W-PKG** | Pusher + packaging + facade | **DONE**: real RFC1350 TFTP PUT + raw-TCP send (`pyverify.pusher`; `host/pusher/push.py` = shim), debug/deploy subprocess leaves, `Mps3Board` facade (`board.py`); 135 pytest green |
| **W-JSON** | Firmware control-line codec | **DONE**: real fail-closed tokenizer + per-op encoders + held-swap-response plumbing; `test_net_proto_json` (156 checks) + `test_coordinator_dispatch` (63) + `ctrl_echo` golden-vector binary |
| **W-DFX-ART + W-DFX-PROD** | static_id + real triples + production flow | **DONE**: `build_dfx.tcl` ran end-to-end on the proof static (`fpga/dfx/prod_results_2026-07-06/`), **real `static_id 0x2F458F06`** (scheme normative in overlay-manifest v0.2), verified overlay triples greybox/led/nanosoc (`make -C fpga/dfx verify` green); I18 software half closed (`fpga/dfx/README.md`) |
| **W-RTL-NEWIP** | `uart_bridge`, `swd_bb`, `telem` | **RTL DONE** (all verilator-lint-clean, in root `make lint`; UARTBR `SWO_CFG`/`FIFO_STATUS` codified in regmap v0.2). Benches = batch-2 (below) |
| **W-CI** (this pass) | One-command check + contracts v0.2 reconcile | **DONE**: root `make check` (contracts → pytest ×2 → firmware harness → lint → `SIM=`-guarded benches → overlay-guarded verify), contracts bumped to v0.2, OPEN_ISSUES I24/I18(sw) closed + I27–I31 added |

### LANDED — batch 2 (2026-07-06)

| Agent | Outcome |
|---|---|
| **W-E2E** | **E2E-1 PASSED**: real nanosoc triple deployed CLI+facade through the fakeshell, both transports, `verified:true`; notebook sequence as pytest; raw-TCP RST race root-caused + fixed (10× suite + 50× stress green) |
| **W-NET-SEAM / W-SPI / W-SMSC** | All three done: `net_if` seam + real TFTP server in C + **two-slot pair staging** (resolves the push-vs-swap gap) + held-response transport; QSPI overlay store vs register-level SST26 model (commit/boot/fallback proven); full LAN9220 driver port (Apache-2.0 provenance). **756 checks / 15 binaries** |
| **New-IP benches** | uart_bridge 5/5, swd_bb 4/4, telem 6/6(+2 fake-mode) — green under VCS **and Questa** (Questa fallback proven); found+fixed a latent stale-data bug in the shared AXI BFM |
| **W-CI reconcile** | Contracts **v0.2** (13 flags folded, I27–I31 opened), root `make check` |

### LANDED — batch 3 (2026-07-06, late)

| Agent | Outcome |
|---|---|
| **W-BD** | **Static shell BD fully implemented**: validate 0 err/0 crit → synth DCP → **impl timing MET (WNS +3.32 ns, 0 failing)** → bitstream (8.6 MB) + XSA. Real bugs fixed en route: C3 PERSIST DRC, axi_emc bank timing faster than LAN9220 spec, clk_wiz FVCO. Evidence: `fpga/shell/build_results_2026-07-06/`. Handoffs recorded for A2 (DCP) + A3 (XSA) |
| **W-RTL-ETH** | rmii_phy_if / eth_bridge_3port / gen_checker / link_partner_mac real (hand-rolled; vendoring documented as fallback); `lan9220_if` deliberately stays stub (EMC route supersedes). Bench gate **ready=12 skip=0**, all green |
| **W-HOST-MISC** | 28 tender-plugin tests vs the real fpgahub checkout (py3.11 venv; precise skip under 3.8), openocd cfgs (no binary on box — tcl-validated), client-side preset validation (facade-on / client-opt-in) |

### LANDED — batch 4 (2026-07-07)

| Agent | Outcome |
|---|---|
| **W-VITIS** | **Firmware cross-compiles to a real MicroBlaze ELF** (239,868 B, 0 warnings) from `shell_harness.xsa`: Vitis platform + lwIP220 RAW BSP, `net_if_lwip.c` seam backend + smsc911x pbuf glue, `main.c` superloop, **xvc_server implemented** (72 host checks — last stub gone), greybox blob generated from the real manifest. **828 firmware checks / 16 binaries.** Findings: 128 KiB LMB too small (ELF built against 256 KiB — shell rebuild needed before it runs); push staging 8 KiB/slot (MiB RMs need QSPI/DDR staging) |
| **DFX on real shell** | Production flow re-run against A1's `shell_static_synth.dcp` (rp_inst `u_rp_dut`): **`static_id 0x628E2D0D`**, `pr_verify` COMPATIBLE led+nanosoc, timing met, compressed partials, triples reverified; generated `mps3_shell_static_id.c` firmware override. `fpga/dfx/prod_results_2026-07-06-realshell/` |
| **rm_eth_ss v1** | Second DUT RM: OOC synth clean, **1,769 LUTs**, real RMII/MDIO + internal AHB bring-up FSM + DMA SRAM (MAC IRQ fires — functional MAC-in-operation, no CPU). `fpga/rp/eth_ss/` |

Integrator: host E2E rekeyed to read `static_id`/sizes from the manifest
(auto-tracks rebuilds); root `make check` green (16 firmware binaries).

### LANDED — the gate-fix pass (2026-07-07, commit `0f93fe3`)

- **256 KiB shell** (gate 1): `local_ram` doubled, impl closed timing
  (WNS +2.472 ns), DCP+XSA; DFX re-run → `static_id 0xECCEDBF3`,
  pr_verify COMPATIBLE ×3; **firmware ELF fits 256 KiB with 8.6 KB
  headroom**.
- **QSPI MiB partial staging** (gate 2): partials > RAM buffer stream to a
  QSPI scratch slot with read-back CRC, then QSPI→HWICAP bounded per-poll.
  +53 checks (881/17 binaries).

### Remaining — in-repo, board-free (the next tranche)

1. **QSPI clearing-cache keyed by `rm_id`** (the overlay-manifest.md v2
   optimization). Unblocks **nanosoc network-swap** (its 117 KB clearing
   won't fit two RAM buffers) AND frees LMB to **bake the real greybox
   blob** (currently a QSPI-served placeholder). Needs a QSPI region for
   the cache — the 8 MB A/B map is full, so this carries a contract call.
2. **rm_eth_ss pr_verify** against the real shell (stage its OOC DCP in
   `build_dfx.tcl` — the RM synth is proven; only the DFX config is unrun).
3. **Contracts v0.2 tail**: I31 (firmware `"err"` vs fakeshell `"error"`
   response-key divergence — pick one), the QSPI-staging wording for
   overlay-manifest.md/net-protocol.md, regmap **v0.3** (MMCM-DRP @0x44AB,
   SWO_CFG @0x18), MAC provisioning policy, DHCP (D8) hook.
4. **Shell polish**: per-signal decoupler boundary (PG294); retire the
   superseded `firmware/harness_app/`; DBGBR offset cross-check vs the
   generated `x*_l.h`.

### Remaining — board-gated (needs the physical MPS3 + tender Pi)

**Phase 0.2** tender-Pi bring-up (fpgahub + openocd + openFPGALoader +
xvcd, validated on a Pynq-Z2 first) → MCC-load the shell `.bit` +
`updatemem` the ELF → **the six bring-up validations I17/I20–I22** (SLR
geometry, ICAP `.bin` byte-lane on real HWICAP, SWD/RMII bit-order vs host
`bitbang.c`, LAN9220 BYTE_TEST/ID, DBGBR offsets) → the live "select DUT →
load → run test → check" loop.

### Remaining — later phases / optional

Multicore nanosoc RM (bigger Pblock); **DDR4 staging fast path** (MIG —
the v2 alternative to QSPI staging); MMIO bridge; eMMC overlay library;
full MAC-in-operation error-inject suite (Phase 5); real `edge-wrapperd`
Hub daemon; port ADP/cocotb vectors + nightly regression.

---

## 1. Workstream catalogue

Effort: S (< half day of agent work) · M (about a day) · L (multi-day).
"Gate" = the headless self-check that closes the workstream.

### Enablers — start first

| WS | Owner | Effort | Work | Gate |
|---|---|---|---|---|
| **W-SIM** — simulator enablement (closes **I24**) *(IN FLIGHT; partly overtaken — VCS+cocotb end-to-end is already proven via `tests/mdio_phy_model/`, `b3e8e99`)* | A5/A6 | S | Repo-root `set_env.sh` (VCS 2022.06-SP2 + Questa + license env; **resolve the cocotb split**: miniconda `cocotb-config` = 2.0.1 vs `~/.local` python3.8 = 1.7.2 — either pin 1.x or port the shared drivers (`mdio_master`/`axis`/`regmap`/`rmii`) past 2.0.1's ReadOnly-phase strictness). Add `tests/sim_smoke/` (~10-line counter). Fix the `board_gpio` marker false-negative in `dut_presence.py`. Document Questa fallback (`SIM=questa`). | `source set_env.sh && make -C tests BLOCK=sim_smoke run-one` green **and the 4 READY benches green** |
| **W-FAKESHELL** — reference shell server | A4+A5 | M | `pyverify.testing.fakeshell`: asyncio server implementing `net-protocol.md` — TCP 6900 JSON-lines (all 7 verbs, per-op response fields), TFTP:69 WRQ + raw:6910 receive with 24-byte header validation, CRC check, **I2 clearing→partial ordering enforcement**, consoles 6930–32 (scripted banner playback). Behaviour backed by the swap-FSM Python port — refactor it out of `tests/integration/test_swap_sequence.py` into an importable module (`pyverify.testing.swap_model` or `tests/common/`), tests keep passing. This doubles as the **executable spec for A3's firmware**. | Own pytest suite green: full verb matrix + torn-transfer/wrong-order/wrong-static rejection cases |

### Host Python (A4)

| WS | Owner | Effort | Work | Gate |
|---|---|---|---|---|
| **W-PUSH** — pusher + subprocess leaves | A4 | S/M | Implement `BitstreamPusher._send`: hand-rolled TFTP PUT (RFC1350, zero-dep, mirrors lwIP tftp semantics) + raw-TCP `sendall`. Implement `debug.py` launch stubs and `run_platform_deploy`'s default runner (asyncio, mirror the tender plugin's `_default_runner`). | Unit tests vs local socket sink green; then E2E-1 (below) |
| **W-PKG** — packaging + facade (same agent as W-PUSH; same files) | A4 | M | Fold `host/pusher/` into the package as `pyverify.pusher` (keep a thin `host/pusher/push.py` re-export shim); delete the `sys.path` hack in `cli.py` + notebook. Add the **`Mps3Board` session facade** (connect / `deploy(rm)` / `uart0` / `telemetry` / `reattach.apply()` with injectable executors — the "PYNQ experience" object). Make `ReattachPlan` executable. Regenerate `host/notebooks/demo.md` (+ a real `.ipynb`) on the facade. | Clean-venv `pip install -e host/pyverify` then `python -m pyverify.cli deploy` from an arbitrary CWD reaches the network layer; 73+ pytest green |
| **W-HOST-MISC** — small closures | A4 | S | Tender-plugin pytest (import via `PYTHONPATH` to the fpgahub checkout, injected fake runner — first CI coverage for `fpgahub_mps3_plugin.py`); `host/openocd/` configs (remote_bitbang :6920, XVC :2542 per contract); client-side `set_clk` preset validation once I16's table lands. | New tests green |

### Firmware (A3) — all host-gcc-harness verifiable

| WS | Owner | Effort | Work | Gate |
|---|---|---|---|---|
| **W-JSON** — control-line codec (the "hollow" spot) | A3 | M | Real tokenizer for the contract's flat JSON-line subset (`find_key`/`extract_string_value` + int/bool), bounded/fail-closed on malformed input; per-op response **encoders** with real fields (`ping`→`shell_id`/`rm_id`, `swap`→`rm_id`/`verified`, `telemetry`→`mv`/`ma`/`lockup`, …). Golden-vector file shared with host: pyverify generates request/response lines, the C harness must parse/emit byte-identical ones. | New harness binary green; cross-language golden test green (E2E-2) |
| **W-SPI** — overlay-store leaves | A3 | M | Implement `spi_flash_read/program` + SST26 block-protect via AXI Quad SPI register sequences (PG153) behind the existing HAL; add a mock QSPI flash to the harness (XSP-register-level fake). Chunked CRC verify loops. | Harness: commit → simulated power-cycle → boot-load happy path **and** interrupted-commit fallback green |
| **W-SMSC** — LAN9220 driver | A3(+A6 lic.) | L | Port the Zephyr `eth_smsc911x.c`-derived driver (Apache-2.0 — record in the licensing audit) to bare-metal behind the HAL seam; pbuf glue left seamed (no lwIP dependency yet). Mock-reg tests for reset/init/MAC-addr/TX-FIFO/RX-FIFO sequences per datasheet. | New harness tests green |
| **W-NET-SEAM** — poll-body seam | A3 | S/M | Define the byte-stream seam (`mps3_net_if`: accept/recv/send callbacks) and write `config_agent_poll`/`uart_over_eth`/`swd_server_poll`/coordinator listener bodies **against the seam** with harness fakes; the real lwIP RAW-API glue becomes one thin later file. Decide + document the staging-buffer question (DDR vs stream-to-HWICAP). | Harness: full swap driven through the seam (header→payload→FSM→verify) green |

### Shell / ethernet RTL (A1) — each de-stub auto-unskips its bench

| WS | Owner | Effort | Work | Gate (needs W-SIM) |
|---|---|---|---|---|
| **W-RTL-CORE** — `dut_clkrst` + `dfx_ctl` bodies ***(≈ DONE — landed in `b3e8e99` as G3; see Progress)*** | A1 | M | ~~AXI4-Lite R/W FSMs + register files~~ done: real bodies, markers dropped, benches gate READY, verilator lint clean. **Remaining sliver:** publish the CLKRST preset table → closes **I16** (with A6); run the benches once W-SIM lands. | `make -C tests BLOCK=clkrst run-one` + `BLOCK=dfx_ctl` green (blocked on W-SIM only) |
| **W-RTL-NEWIP** — the ~~four~~ **three** missing IP dirs *(IN FLIGHT; `board_gpio` already landed real in `b3e8e99`)* | A1(+A5 benches) | L | Create `fpga/shell/ip/{uart_bridge, swd_bb, telem}` per regmap v0.1 (UARTBR AXIS↔FIFO + status; SWDBB DRIVE/SAMPLE; TELEM register surface with the I2C master behind a seam). A5 writes the three missing benches. | All benches green |
| **W-MDIO** — Clause-22 PHY model engine ***(RTL DONE — landed in `b3e8e99` as G4; see Progress)*** | A1+A5 | M | ~~MDIO frame engine + register file + AXI-Lite side~~ done and VCS-validated (compile/elab/run). **Remaining:** the bench itself green — blocked on W-SIM's cocotb-2.x driver port, not on this RTL. | `BLOCK=mdio_phy_model` green incl. host-injected link-down (blocked on W-SIM only) |
| **W-RTL-ETH** — datapath blocks | A1 | L | `rmii_phy_if` RMII↔MII conversion + 50 MHz ref gen (TX re-register stage already real); `eth_bridge_3port` forwarding (vendor or hand-roll the crossbar — decide vendoring policy for Forencich cores with A6); `gen_checker` inject/score paths. `link_partner_mac`/`lan9220_if` may stay stubs this wave (bench coverage first). | `BLOCK=rmii_phy_if`, `BLOCK=bridge`, `BLOCK=gen_checker` green |

### DFX flow (A2) — Vivado runs locally

| WS | Owner | Effort | Work | Gate |
|---|---|---|---|---|
| **W-DFX-ART** — real overlay artefacts | A2 | M | Extend the proof flow to emit `.bin` (ICAP ordering) alongside `.bit`; run `gen_manifest.py` against the **real** proof bitstreams → generated `overlay/{greybox,led}/` triples (gitignore payloads; commit manifests + a `make overlays` target). Software half of **I18**: parse emitted `.bin` headers, document word order vs PG134. Implement the **`static_id` scheme** (replace `build_dfx.tcl`'s sentinel; e.g. hash of the routed static; A6 signs off, contracts updated). | `gen_manifest.py verify` green on real triples; `pyverify` `Overlay.load()` accepts them |
| **W-DFX-PROD** — production flow dry run *(IN FLIGHT; D7 half already DONE — the REAL `rm_nanosoc`'s OOC utilisation is measured: 7,935 LUT = 1.2 %, `util_rm_nanosoc.rpt`, far better than the planned stub datapoint)* | A2 | M | Run `build_dfx.tcl` end-to-end using the proof's minimal static as the stand-in `static_shell_dcp`: proves the N-config loop, readiness filter, clearing-pair emission and manifest hand-off without waiting for the real shell. | `build_dfx.tcl` completes on this machine; reports committed like the proof's |

### Integration + CI (A6)

| WS | Owner | Effort | Work | Gate |
|---|---|---|---|---|
| **W-CI** — one-command check ***(DONE 2026-07-06 — root `make check` landed; overlay verify is guarded on triple presence rather than `VIVADO`, since `gen_manifest.py verify` needs no Vivado)*** | A6 | S | Root `make check`: contracts → host pytest → tests pytest → firmware harness → verilator lint → (if `SIM` set) ready benches → (if overlay triples exist) manifest/artefact verify. Wire as CI skeleton. | Single command green locally ✔ |
| **W-E2E** — the wave's convergence gates | A6/A5 | M | **E2E-1:** `pyverify.cli deploy --host 127.0.0.1` against W-FAKESHELL using W-DFX-ART's real triples — the full host stack exercised with genuine UltraScale artefacts. **E2E-2:** W-JSON golden vectors both directions. **E2E-3:** headless run of the facade notebook against the fake shell. **E2E-4:** three-way swap-FSM equivalence (C harness ↔ Python port ↔ fake-shell observable behaviour). | All four green under `make check` |

### Stretch / capstone

| WS | Owner | Effort | Work | Gate |
|---|---|---|---|---|
| **W-BD** — shell BD v0 assembly | A1 | L | Complete `shell_bd.tcl`: rename RP cell to the I4-resolved `u_top/u_rp_dut`, package the custom IP (`soclabs.org:user:*` VLNVs) with packaging tcl, fill the `connect_bd_net` TODOs, address map per regmap v0.1, `validate_bd_design`, synth to a real static DCP + **XSA**. Unblocks: W-DFX-PROD on the *real* shell; a Vitis BSP compile of the firmware (the first non-mock firmware build target). | `validate_bd_design` clean + synth DCP written; DRC/timing reports committed |

---

## 2. Dependency graph & suggested parallel launch

```
Wave A (all parallel):
  W-SIM          W-FAKESHELL     W-PUSH→W-PKG     W-JSON     W-SPI
  W-DFX-ART      W-DFX-PROD*     W-RTL-CORE†      W-MDIO†    W-RTL-NEWIP†
  W-HOST-MISC    W-CI            W-SMSC           W-NET-SEAM
      († bench gates need W-SIM done first — RTL writing can start immediately)
      (* shares fpga/dfx/ files with W-DFX-ART — same agent, sequential)

Wave B (integration): W-E2E (needs W-FAKESHELL + W-PUSH + W-DFX-ART + W-JSON)
Stretch:              W-BD (any time; unblocks real-shell DFX + Vitis build)
```

**File-collision rules for parallel agents** (avoid merge pain):
- One agent per directory subtree wherever possible. Known shared files:
  `firmware/test/Makefile` (W-JSON/W-SPI/W-SMSC/W-NET-SEAM all add binaries
  — each adds its *own* test files; run these as one A3 agent sequentially,
  or let the integrator merge the four small Makefile hunks).
- `host/pyverify/` is touched by W-FAKESHELL, W-PUSH/W-PKG, W-HOST-MISC —
  W-PUSH/W-PKG must be one agent; W-FAKESHELL adds only new files under
  `pyverify/testing/` + a refactor of `tests/integration` (coordinate the
  swap-model move with the integrator).
- `tests/` bench additions (W-RTL-NEWIP) are per-block directories — safe.

**Priority if running fewer agents:** W-SIM → W-FAKESHELL → W-PUSH/W-PKG →
W-JSON → W-DFX-ART → W-RTL-CORE → everything else. The first five give the
platform its first true end-to-end loop (real artefacts, real protocol, real
sequencing — fake hardware).

## 3. What this wave deliberately leaves on the table

Board bring-up (I17/I20–I22, tender-Pi Phase 0.2 — including booting the
now-built monolithic bitstream and loading the now-proven nanosoc partial
over HWICAP), lwIP running on a MicroBlaze (needs W-BD's XSA first — the
seam from W-NET-SEAM makes that a thin final step), the `rm_eth_ss` wrapper
(the MAC-in-operation DUT — single-core nanosoc has no MAC), and any
fpgahub-side or Hub-side code. ~~The real nanosoc RM (D2b)~~ — no longer on
the table: **done and proven** (`b3e8e99`, see Progress). Every remaining
item becomes *smaller* after this wave because its software half will
already be tested.
