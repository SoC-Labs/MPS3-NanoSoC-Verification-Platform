# Verification (`tests/`) — mps3-nanosoc-platform

Owner: **A5**. This tree is the cocotb bench scaffolding + test plan for the
new RTL blocks (§8/§8.4/§13 of `docs/ARCHITECTURE_SPEC.md`) and the
platform integration, per `docs/IMPLEMENTATION_PLAN.md`'s A5 rows. Every
contract this suite verifies against lives in `docs/contracts/`:
`partition-pins.md`, `shell-regmap.md`, `net-protocol.md`,
`overlay-manifest.md`.

## Important note on timing (2026-07-04)

This scaffolding was written from the contracts + `IMPLEMENTATION_PLAN.md`
*before* checking whether the RTL/firmware/host code it targets existed —
by the time it was finished, A1/A2/A3/A4 had already landed real work in
parallel:

- **All six RTL blocks this suite benches exist as real, synthesizable-
  shaped "Phase 0 stub" SystemVerilog** (`fpga/ethernet/{mdio_phy_model,
  gen_checker,rmii_phy_if,bridge}/*.sv`, `fpga/shell/ip/{dfx_ctl,clkrst}/
  *.sv}`) — real port lists, `// TODO(A1)` bodies. Every per-block bench
  below binds to those **real, confirmed port names** (verified by reading
  each `.sv` file directly), not guessed ones.
- **A4's `host/pyverify` and A3's `firmware/` are real, working code**,
  not stubs — `pyverify.overlay.Overlay`/`OverlayManifest`,
  `host/pusher/push.py`'s bitstream framing, and
  `firmware/coordinator/swap_fsm.c`'s state machine all exist. Rather than
  re-implement any of that, `tests/integration/` **imports and exercises
  the real modules directly** (see `test_manifest_roundtrip.py` and
  `test_swap_sequence.py`).

Because RTL now exists but is functionally an unimplemented stub (AXI-Lite
`awready`/`arready` never assert; the MDIO/frame-gen bodies are tied-off
constants), a bare "does the file exist" skip-gate would have flipped
every bench from a clean skip straight to a **hang** the moment A1
committed a stub. `tests/common/dut_presence.rtl_ready()` gates on the
absence of the literal `"Phase 0 stub"` marker every one of those six
files currently carries in its own header comment, instead — see that
module's docstring. Every `@cocotb.test(skip=...)` below computes this
automatically; no manual flip needed once a block is genuinely finished.

## v0.1 contract expansion (2026-07-04, W5)

Contracts moved to v0.1 mid-delivery (`partition-pins.md`/`shell-regmap.md`/
`net-protocol.md`/`overlay-manifest.md`, resolving OPEN_ISSUES I2/I4/I8/I12/
I13) while other agents landed real work concurrently with this suite's own
expansion. Three new test areas cover the v0.1 delta:

- **`tests/board_gpio/`** — the new GPIO passthrough OWN-mux block (I4,
  `shell-regmap.md` @ `0x44AA_0000`). No RTL exists for this block at all
  yet (`fpga/shell/ip/board_gpio/` doesn't exist) — the cocotb bench
  (`test_board_gpio.py`) is a forward-looking skeleton, permanently skipped
  by `dut_presence.rtl_ready()` until it lands; the OWN-mux truth table
  itself is independently derived and exhaustively tested pure-Python in
  `test_gpio_mux_logic.py` (**runs today**).
- **`tests/edge/`** — cross-checks `docs/HARDWARE_HUB_INTEGRATION.md` §2
  (channel enumeration) and §4 (`dfx-swap` invalidation) against each
  other and against `host/pyverify/pyverify/edge.py` (W4's Edge Device API
  model, which landed mid-session). Found and pinned a real doc-internal
  inconsistency (§2's `gated_by` column vs. §4's literal invalidation
  list disagree on `swo` vs. `dut-uart`) **and** a genuine self-consistency
  bug inside `edge.py` itself (`swo` is `gated_by=rp` but exempt from the
  `dfx-swap` invalidation set, contradicting `GatedBy.RP`'s own docstring)
  — see that test file's docstrings and the ambiguity list below.
- **`tests/firmware_logic/`** — shells out to `firmware/test/Makefile`
  (W3's host-gcc unit-test harness, which also landed mid-session) and
  asserts every one of its 6 binaries builds and passes. Covers
  `crc32`/bitstream-header framing (I12/I13), the swap-FSM's pure
  transition table, `config_agent`'s validation logic, and — via
  `test_swap_fsm_hw`, run unmodified as W3 wrote it — `swap_fsm.c`'s real
  impure `step_*()` functions against a mocked register file.

`tests/integration/test_swap_sequence.py` was also **rewritten** during
this same window: it originally ported an earlier `swap_fsm.c` with two
live gaps (I2's "cache the incoming RM's clearing" step had no code path
at all; `step_verify()` hardcoded `verified = true`), encoded as
deliberate canary tests. Both gaps were fixed mid-session (I2/I25 both
RESOLVED in `swap_fsm.c`/`swap_fsm_transitions.c`) — the file now ports
the current, fixed 12-state machine, and the old canaries are gone,
replaced by tests asserting the now-correct behavior (see that file's own
module docstring for the full before/after).

## Verification strategy

| Scope | What | How |
|---|---|---|
| **Unit** (per-block, isolated) | Each of the 6 new/owned RTL blocks + `board_gpio` (I4, no RTL yet) | `tests/<block>/test_*.py`, cocotb, binds directly to the block's own module as `TOPLEVEL` (no wrapper needed — all 7 are leaf modules) |
| **Protocol/regmap integration** | Swap ordering (net-protocol.md + `swap_fsm.c`/`swap_fsm_transitions.c`), manifest validation (overlay-manifest.md + `pyverify.overlay` + `gen_manifest.py`), A/B-slot invariant (§8A.5) | `tests/integration/`, pure Python (no DUT/simulator) — runs today |
| **Helper-logic unit tests** | MDIO framing, Ethernet frame/CRC/error-injection, regmap constants, A/B-slot header pack/unpack, GPIO OWN-mux truth table | `tests/common/test_*.py` + `tests/board_gpio/test_gpio_mux_logic.py`, pure Python, pytest-collectible, **run today, no simulator** |
| **Hub/edge integration** | `HARDWARE_HUB_INTEGRATION.md` §2/§4 channel enumeration + `dfx-swap` invalidation, cross-checked against `pyverify.edge` | `tests/edge/test_edge_channel_invalidation.py`, pure Python — **runs today** |
| **Firmware host-gcc harness** | `firmware/test/`'s own build+run (crc32/net_proto/ovlstore_codec/swap_fsm_transitions/config_agent/swap_fsm.c-against-mock-regs) | `tests/firmware_logic/test_firmware_host_gcc_harness.py` shells out to `make -C firmware/test clean test` — **runs today** (needs `gcc`+`make`, both present in this dev env) |
| **Unit (W-RTL-NEWIP, 2026-07-06)** | `uart_bridge` (I7, UARTBR AXIS⇄FIFO bridge + SWO deserialiser, dual-clock CDC), `swd_bb` (I5, SWD pin-wiggler), `telem` (I9, INA228 CSR seam) — real RTL from day one, benched green under VCS+Questa | `tests/{uart_bridge,swd_bb,telem}/test_*.py`, cocotb; `telem` additionally has a `make FAKE=1` fake-data-mode pass |
| Not yet in scope here | `nanosoc` RM itself (has its own cocotb suite in `nanosoc_arch_tech`), XVC/Debug Bridge (DBGBR, I6 — Xilinx IP, no custom RTL), LAN9220 host I/F, link_partner_mac | Flagged below under "gaps," not built here |

Of the six per-block benches, **`mdio_phy_model` is the most detailed**
and **`gen_checker` is second** — both per the task brief, matching
`ARCHITECTURE_SPEC.md` §12's "only two blocks need meaningful new RTL."

## Sim target — VCS + cocotb 2.0.1 (W-SIM, I24 closed 2026-07-04)

cocotb + VCS is the primary target (`SIM ?= vcs` in every per-block
`Makefile`, matching this lab's convention — see e.g.
`ethernet-mac-ahb/cocotb/*/Makefile`). **Source the repo-root
`set_env.sh` before running any cocotb target** — it now exists (the
former "flagged for A6" gap below is closed) and pins the validated
combo: **miniconda Python 3.10 + cocotb 2.0.1 + VCS 2022.06-SP2**, plus
the Synopsys license env. verilator 4.028 on this machine is lint-only
(too old for cocotb); iverilog is not installed, so the old `SIM=icarus`
idea is dead on this box.

**Questa fallback: PROVEN GREEN (2026-07-06, W-RTL-NEWIP bench pass —
closes W-SIM open question 2).** `SIM=questa make` (Questa 2022.4 via
`set_env.sh`'s license env) ran `sim_smoke` 2/2 and then all three new
benches (`swd_bb` 4/4, `uart_bridge` 5/5 incl. the dual-clock CDC tests,
`telem` seam-mode 6/6) green. Two caveats, both handled:

1. `-sverilog` is a VCS-only flag — Questa's `vlog` hard-errors on it
   (`vlog-1902`) and parses `*.sv` as SystemVerilog by extension anyway.
   The `sim_smoke`/`swd_bb`/`uart_bridge`/`telem` Makefiles now wrap it
   in `ifeq ($(SIM),vcs)`; the older per-block Makefiles (mdio_phy_model/
   clkrst/dfx_ctl/board_gpio/...) still pass it unconditionally — copy
   the guard when/if those need Questa.
2. **`make clean` (or `rm -rf sim_build`) when switching simulators** —
   a stale VCS-era `sim_build/` makes the Questa run fail confusingly.
   Also `telem`'s `FAKE=1` mode uses VCS `-pvalue+` syntax and is
   VCS-only for now (Questa would want `vsim -g`).

VCS stays the default/primary (`SIM ?= vcs` everywhere).

**The cocotb version decision** (full rationale in `set_env.sh`'s header):
this machine has cocotb 1.7.2 (`~/.local`, python3.8 — the default
`python3`) *and* cocotb 2.0.1 (miniconda, python3.10 — the `cocotb-config`
that PATH resolves, i.e. what the Makefiles actually use). Commit
b3e8e99's Wave-3 diagnosis ("cocotb 2.0.1 ReadOnly-phase strictness breaks
the 1.x-era shared drivers") turned out to be only half right: **cocotb
1.7.2 forbids ReadOnly-phase writes too** (its `scheduler.py:596` raises
the same error; the drivers were only ever `make -n` dry-run under 1.7.2,
never executed). So pinning 1.7.2 would not have avoided the driver port —
2.0.1 was adopted (already proven with VCS on this machine by b3e8e99's
own run) and the shared drivers were ported instead:
`tests/common/regmap.py` (`AxiLiteMaster`) and `tests/common/axis.py`
(`AxisByteDriver`) now do all signal writes in writable phases, public
APIs unchanged; `mdio_master.py`/`rmii.py` were audited and were already
2.x-safe (Timer/RisingEdge-context writes only). The benches keep the
`units=` kwarg (deprecated-but-working in 2.x) so they stay importable
under 1.7.2 — expect harmless `DeprecationWarning: 'units' has been
renamed to 'unit'` lines in sim logs.

RTL is SystemVerilog; every per-block `Makefile` passes `-sverilog` to VCS
accordingly.

## Running the benches

```
source set_env.sh                          # ALWAYS first (from the repo root)

make -C tests            # RTL-readiness summary, run every READY bench + the pure-Python suite
make -C tests list       # just the readiness summary (no simulator invoked)
make -C tests pytest     # only the pure-Python suite (no simulator/RTL needed)
make -C tests BLOCK=sim_smoke run-one      # harness sanity check (always READY)
make -C tests BLOCK=mdio_phy_model run-one # one block, gated on its RTL readiness

python3 -m pytest tests -q       # the pure-Python suite from the repo root
                                 # (passes with or without set_env.sh sourced)
```

If a bench fails, run `make -C tests BLOCK=sim_smoke run-one` first: its
DUT is a ~10-line counter living inside `tests/sim_smoke/` itself, so a
red sim_smoke means the simulator/cocotb/license environment is broken,
not any RTL.

Benches proven green under VCS in the W-SIM pass (2026-07-04):
`sim_smoke` (2/2), `clkrst` (5/5), `dfx_ctl` (4/4), `board_gpio` (4/4),
`mdio_phy_model` (3/3). The W-RTL-NEWIP bench pass (2026-07-06) added
three more, green under VCS **and** Questa (first-ever simulation of
those blocks): `uart_bridge` (5/5, dual-clock 10 ns/7 ns CDC),
`swd_bb` (4/4), `telem` (6/6 seam mode + 2/2 `make FAKE=1` fake mode) —
see each `tests/<block>/dut_notes.md` for what was pinned. That pass also
fixed a latent shared-BFM bug: `tests/common/regmap.py`'s
`AxiLiteMaster.read()` returned the PREVIOUS address's data when two
`read()` calls were issued with zero gap (the Xilinx-template FSM re-arms
ARREADY at the retire edge, while ARVALID is still high, and re-captures
the old ARADDR; the older benches always had an await between reads so it
never fired). `read()` now ends with one idle ARVALID-low edge — full
suite re-run green after the change. Related: `tests/common/axis.py`'s
`AxisByteMonitor` samples beats AFTER the clock edge, which against a
FWFT-FIFO source records the post-pop next-head and drops the first byte
— `tests/uart_bridge/test_uart_bridge.py::_axis_collect` shows the
correct pre-edge sampling; revalidate the shared monitor before
gen_checker/bridge unskip (its own header already flags this).

Two bench-side fixes were needed in the original W-SIM pass:

- **`board_gpio` was stuck reporting SKIP** because its (real, G3) RTL
  header *quoted the stub-marker string inside a comment saying the marker
  was absent* — `dut_presence.rtl_ready()`'s substring check tripped on
  its own negation. Fixed with a comment-only rephrase in the RTL header;
  no test-infra change needed (the mapping in `list_benches.py` was
  correct all along).
- **`tests/clkrst/test_clkrst.py` contradicted the contract**: it never
  drove `dut_clk_i` and sampled `*_resetn_o` one AXI edge after a
  RESET_CTRL write, but partition-pins.md's "Clock/reset domain rule"
  mandates async-assert/**sync-deassert** (releases only propagate after
  2-3 destination-clock edges) — exactly as `dut_clkrst.sv`'s "IMPORTANT
  NOTE FOR A6" header predicted. The bench now clocks `dut_clk_i` (20 ns,
  deliberately different from the AXI clock) and waits settle cycles on
  every *release* path; *assert* paths still check single-edge (assertion
  is async per the same rule).

`make -C tests pytest` (and the bare `pytest tests/` above) now also runs
`tests/board_gpio/` (pure-logic part only — `test_board_gpio.py` is
cocotb-only and excluded, see `conftest.py`), `tests/edge/`, and
`tests/firmware_logic/` (which itself shells out to `make -C firmware/test
clean test` — needs `gcc`+`make` on `PATH`, both present in this dev
environment; skips cleanly if either is missing or `firmware/test/` isn't
there).

## TEST PLAN — phase gate → bench(es)

Gates are `IMPLEMENTATION_PLAN.md`'s phase/WS acceptance criteria (and,
where noted, `ARCHITECTURE_SPEC.md` §14's numbered phase list).

| Phase / WS | Gate | Bench(es) that close it |
|---|---|---|
| 0.5 (A5) | "CI runs a first bench" / smoke bench for the MDIO slave stub | `tests/mdio_phy_model/` (full bench, skip-guarded) + `tests/common/test_mdio_master.py` (framing logic — **runs today**) + `tests/Makefile` as the CI entry point |
| 1.1–1.2 (A2/A4) | `pr_verify` clean; two swaps over JTAG/XVC undisturbed | `tests/integration/test_swap_sequence.py` — protocol-level model of the same ordering (decouple→clearing→partial→verify→release), independent of transport |
| 1.4 (A5) | "decoupler/reset sequencing model; artefact-set CRC checks in CI" | `tests/dfx_ctl/`, `tests/clkrst/` (regmap-driven sequencing, incl. the cross-block `rp_resetn_gate` composition) + `tests/integration/test_swap_sequence.py`'s header-validation tests (reusing `host/pusher/push.py`'s real CRC/framing) |
| 2.1–2.3 (A1/A3) | LAN9220 bridge synth-clean; swap LED RMs over Ethernet; wrong-order/wrong-static rejected | `tests/bridge/` (routing/flood, once RTL lands) + `tests/integration/test_swap_sequence.py::test_bad_static_id_rejected_before_any_regs_touched` |
| 2.4 (A5) | "Bridge + config-agent benches (frame routing, malformed input, torn transfer)" | `tests/bridge/test_bridge.py` + `tests/integration/test_swap_sequence.py` (config_agent-style header rejection) |
| 3.2 (A3) | Overlay store A/B slots; interrupted-commit fallback | `tests/integration/test_manifest_roundtrip.py::test_ab_slot_commit_cannot_brick_the_default` (via `tests/common/ovlstore_header.py`) |
| 3.3 (A4) | Host pusher + manifest tooling | `tests/integration/test_manifest_roundtrip.py` (exercises the REAL `pyverify.overlay` + `fpga/dfx/gen_manifest.py` end to end) |
| 4.1 (A2+A1) | nanosoc RM partition-pin wrapper boots | Out of this suite's scope — nanosoc has its own cocotb suite upstream (`nanosoc_arch_tech`); this suite's `clkrst`/`rmii_phy_if` benches cover the shell-side halves of the same partition pins |
| 5.1 (A1) | RMII virtual PHY loopback | `tests/rmii_phy_if/test_rmii_phy_if.py` |
| 5.2 (A1+A5, **bench-first**) | MDIO slave + PHY register model; host-injected link-down observed by DUT | `tests/mdio_phy_model/test_mdio_phy_model.py` (most detailed bench in this delivery) |
| 5.3 (A1) | link-partner MAC into the bridge | `tests/bridge/test_bridge.py` (dut_mac port) — `link_partner_mac` itself has no dedicated bench dir in this delivery (not in the per-block list given); its AXI-Stream shape is exercised indirectly via `tests/common/axis.py`'s generic driver/monitor |
| 5.4 (A5+A1) | Error-inject gen/checker: injection cases detected/scored both sides | `tests/gen_checker/test_gen_checker.py` (7 cases: good/bad-fcs/runt/giant generator + checker scoring, incl. `tuser` cross-check) + `tests/common/test_frames.py` (**runs today**) |
| 5.5 (A2) | `rm_eth_ss` RM passes the same suite | Re-run `tests/gen_checker/` + `tests/mdio_phy_model/` unmodified against the eth-ss RM once it exists — no new bench needed |
| 6.2 (A4+A5) | Regression runner; nightly platform regression green | `tests/Makefile` (discovery + skip-summary + pytest aggregation, now incl. `board_gpio`/`edge`/`firmware_logic`) |
| spec §14 phase 9 | MAC verification subsystem, full acceptance | `tests/gen_checker/` + `tests/mdio_phy_model/` + `tests/rmii_phy_if/` + `tests/bridge/` together |
| spec §14 phase 10 | Python verification layer / "port cocotb/ADP vectors" | `tests/integration/` is exactly this: the swap-sequence and manifest models are the vectors `pyverify`'s eventual orchestration layer should reuse |
| v0.1 / I4 (board-port GPIO) | GPIO passthrough OWN-mux: DUT-owns-bit default vs. host-override | `tests/board_gpio/` — `test_gpio_mux_logic.py` (independent truth table, **runs today**) + `test_board_gpio.py` (skip-guarded cocotb skeleton, no RTL yet) |
| v0.1 / I2+I25 (swap sequence rewrite) | Shell-owns-clearing full 7-step sequence incl. caching the incoming RM's clearing; real `DFXCTL.RM_ID` verify compare | `tests/integration/test_swap_sequence.py` (rewritten to the current, fixed `swap_fsm.c`/`swap_fsm_transitions.c`) + `tests/firmware_logic/` (shells out to the real `test_swap_fsm_hw`/`test_swap_fsm_transitions` C binaries) |
| I3 (Hardware Hub integration) | Edge Device API channel enumeration + `dfx-swap`-scoped invalidation | `tests/edge/test_edge_channel_invalidation.py` — doc-internal cross-check (§2 vs §4) + cross-check against `pyverify.edge` |
| I12/I13 (net-protocol framing units) | `len_words = len/4`; `crc32` = zlib/IEEE CRC-32 | `tests/firmware_logic/` (`test_crc32`/`test_bitstream_header` binaries, incl. the CRC-32/ISO-HDLC catalogue check value) — supersedes what would otherwise have been a from-scratch Python re-derivation, since W3's real C harness landed first |

## Contract ambiguities found (for A6)

Cross-referenced against `platform_regs.h`'s own `AMBIGUITY(A6) #N`
numbering where it already exists, so these don't read as a second,
disconnected list:

1. **GENCHK register layout is a draft, not contract-final**
   (`AMBIGUITY(A6) #6`, also flagged independently in
   `gen_checker/README.md` and `fpga/ethernet/README.md`). `gen_checker/
   dut_notes.md` and `tests/common/regmap.py` both carry the same
   placeholder offsets/mode-encoding this bench uses — update both if A6
   settles something different.
2. **RESOLVED mid-delivery, RTL still not caught up: DFXCTL RM-load-verify
   (rm_id) offset** (was `AMBIGUITY(A6) #4` / `OPEN_ISSUES` I8).
   `shell-regmap.md` moved to v0.1 and gave this a real, non-placeholder
   offset pair (`RM_ID`@0x10, `RM_STATUS`@0x14); `firmware/common/
   platform_regs.h` and `firmware/coordinator/swap_fsm.c` have since been
   updated to match (confirmed by re-reading both directly — `swap_fsm.c`'s
   `step_verify()` now does a real compare, see the former "known gap"
   note this replaces, below). **`dfx_ctl.sv` (RTL) has NOT been updated**:
   it still only accepts `rm_id_i`/`dut_lockup_i` as plain ports with
   `s_axi_rdata` tied to `'0` unconditionally — `tests/dfx_ctl/
   test_dfx_ctl.py::test_rm_id_and_lockup_ports_are_wired` is
   correspondingly still a weak, port-level-only smoke test, and
   `OPEN_ISSUES.md`'s own front-matter has not been updated to mark I8
   resolved even though `shell-regmap.md`'s v0.1 header claims it closes
   I5-I11 (which includes I8) — a small cross-doc-consistency gap for A6.
   See `tests/dfx_ctl/dut_notes.md` and `tests/common/regmap.py`'s module
   docstring for the full timeline.
3. **Bridge has no regmap block at all**
   (flagged in `bridge/README.md` / `fpga/ethernet/README.md`).
   `eth_bridge_3port.sv`'s forwarding table is a compile-time constant;
   `tests/bridge/test_bridge.py` uses placeholder MAC addresses standing
   in for whatever A1 picks — see `bridge/dut_notes.md`.
4. **`rmii_phy_if`'s RMII-dibit / MII-nibble bit order is unconfirmed**
   — the conversion logic is 100% unimplemented (every assign is a
   tied-off placeholder), so there's no RTL to check a specific IEEE
   802.3 Annex 22B bit order against yet. `tests/rmii_phy_if/
   test_rmii_phy_if.py` checks a self-consistent round trip instead;
   tighten once real conversion logic lands.
5. **`net-protocol.md`'s bitstream-header byte order is inherited from
   A4, not independently specified.** `host/pusher/push.py` explicitly
   chose big-endian (`">4sHBBIIII"`, documented in that file); this
   suite's `overlay_store.h`-mirroring `tests/common/ovlstore_header.py`
   (a *different*, on-flash header with no cross-endian transfer to force
   a choice) assumes big-endian too, purely for internal consistency —
   confirm the MicroBlaze BSP's actual configured endianness with A3
   before treating that second assumption as load-bearing.
6. **RESOLVED mid-delivery (was a "known gap," not an ambiguity):**
   `swap_fsm.c`'s `step_verify()` used to hardcode `verified = true`
   unconditionally regardless of the `DFXCTL_RM_ID` readback (I25).
   `tests/integration/test_swap_sequence.py`'s Python port of this state
   machine used to reproduce that gap **on purpose**
   (`test_verify_state_currently_always_reports_success_TODO`, a canary)
   so the day someone wired up a real comparison, the test would need
   updating too. That day arrived mid-session: `swap_fsm.c` now does a
   real `DFXCTL.RM_ID == target_rm_id && RM_STATUS.rm_id_valid` compare,
   `swap_fsm_transitions.c`'s `SWAP_VERIFY` case branches both ways, and
   `firmware/test/test_swap_fsm_hw.c::test_i25_verify_mismatch_fails_and_
   stays_decoupled()` (W3's own C-level test) plus this file's rewritten
   `test_verify_rejects_rm_id_mismatch`/`test_verify_rejects_when_rm_id_
   valid_bit_clear` now assert the fixed behavior directly. The canary is
   gone (correctly) — see `test_swap_sequence.py`'s module docstring for
   the full before/after.
7. **RESOLVED mid-delivery: I2's missing "cache the incoming clearing"
   step.** Also flagged by this file's earlier delivery pass (grepping
   `firmware/` at the time found `g_rm_cache` was only ever written once,
   at boot — no code path cached a newly-swapped-to RM's clearing
   bitstream for the *next* swap, so a second consecutive swap always
   failed closed). `swap_fsm.h`/`swap_fsm.c` gained two new states
   (`SWAP_AWAIT_INCOMING_CLEARING`, `SWAP_CACHE_CLEARING`) implementing
   net-protocol.md's full 7-step sequence; `tests/integration/
   test_swap_sequence.py::test_two_consecutive_swaps_succeed_now_that_
   cache_clearing_is_wired_up` is the direct positive proof.
8. **`HARDWARE_HUB_INTEGRATION.md` §2 vs §4 disagree on one channel name**
   (found independently by this suite AND by W4 while writing
   `pyverify/edge.py` — see that module's own "open questions" docstring
   section, "dut-uart vs. console"). §2's `gated_by` column marks exactly
   `swo`/`dut-net`/`swd` as `rp`-gated; §4's `dfx-swap` row instead lists
   its invalidation set in prose as `swd`/`dut-net`/`dut-uart` — `dut-uart`
   names no channel in §2's table, and §4 never mentions `swo` even though
   §2 says it's `rp`-gated too. `pyverify.edge` resolves this by modelling
   `dut-uart` as its own channel (splitting UART0/console from
   UART1/dut-uart), which makes §4 satisfiable but leaves `swo` (still
   `rp`-gated per §2) genuinely unaccounted for in the `dfx-swap`
   invalidation set — see finding #9. `tests/edge/
   test_edge_channel_invalidation.py::test_dfx_swap_rp_gated_channels_vs_
   doc_literal_invalidation_list` pins the exact mismatch.
9. **`pyverify/edge.py` self-consistency bug (new finding, not previously
   flagged in that module's own docstring):** `GatedBy.RP`'s docstring
   states "a channel gated on `rp` does not [survive dfx-swap]" — i.e.
   every `rp`-gated channel should appear in
   `reset("dfx-swap").invalidated_channels`. `swo` is declared
   `gated_by=GatedBy.RP` but is **not** in the `_RESET_SEMANTICS[DFX_SWAP]`
   tuple (`("swd", "dut-net", "dut-uart")`) — a lessee holding an `swo`
   (trace) lease across a DUT swap would keep it, contradicting the
   module's own stated rule. `tests/edge/
   test_edge_channel_invalidation.py::test_every_rp_gated_channel_is_
   invalidated_by_dfx_swap_TODO` encodes this as a canary (documents
   current behavior, expected to need updating once W4 either adds `swo`
   to the tuple or gives `GatedBy.RP` a documented carve-out).
10. **GPIO passthrough (I4) has no RTL at all yet.**
    `fpga/shell/ip/board_gpio/` doesn't exist — `tests/board_gpio/
    test_board_gpio.py` is a forward-looking cocotb skeleton (port names
    are a best guess, not confirmed against real RTL — see that
    directory's `dut_notes.md`), permanently skipped until A1 lands it.
    The OWN-mux truth table itself (`tests/board_gpio/
    test_gpio_mux_logic.py`) is independently derived from the contract
    prose and needs no RTL to run today.
11. **RESOLVED (W-SIM, 2026-07-04): repo-root `set_env.sh` now exists**
    (closes OPEN_ISSUES I24). It pins miniconda Python 3.10 + cocotb 2.0.1
    + VCS 2022.06-SP2 + the Synopsys/Mentor license env, and documents the
    cocotb 1.7.2-vs-2.0.1 decision (see "Sim target" above). The stale
    "doesn't exist yet" notes in the per-block Makefile headers predate
    this and can be cleaned up opportunistically.

## Layout

```
tests/
├── README.md                    (this file)
├── Makefile                     top-level discovery/runner
├── conftest.py                  excludes the cocotb-only per-block test_*.py from bare `pytest`
├── common/                      shared helpers (see each file's docstring)
│   ├── dut_presence.py          RTL-readiness gate (stub-marker aware, not just file-exists)
│   ├── list_benches.py          block -> RTL-file mapping, used by tests/Makefile
│   ├── mdio_master.py           Clause-22 MDIO framing (pure) + cocotb bus master
│   ├── frames.py                Ethernet frame build/CRC/error-injection (pure)
│   ├── rmii.py                  RMII/MII bit-level cocotb driver/monitor
│   ├── axis.py                  generic byte AXI-Stream cocotb driver/monitor
│   ├── regmap.py                shell-regmap.md constants (incl. v0.1 GPIO/DFXCTL RM-ID) + AXI4-Lite cocotb BFM
│   ├── ovlstore_header.py       QSPI A/B-slot header pack/unpack (mirrors overlay_store.h)
│   └── test_*.py                pure-Python unit tests for all of the above (run today)
├── sim_smoke/                     harness sanity bench (W-SIM/I24): ~10-line
│                                  counter DUT lives IN this dir; always READY;
│                                  proves cocotb+VCS+license independent of fpga/
├── mdio_phy_model/               most-detailed bench (WS 5.2, bench-first)
├── gen_checker/                  second-most-detailed bench (WS 5.4)
├── rmii_phy_if/                  RMII<->MII loopback framing (WS 5.1)
├── bridge/                       3-port routing + flood-on-miss (WS 5.3/2.4)
├── dfx_ctl/                       regmap-driven decouple/shutdown/reset-gate (WS 1.4)
├── clkrst/                        regmap-driven reset/clock-preset (WS 1.4)
│   (each of the above 6: test_*.py + Makefile + dut_notes.md)
├── board_gpio/                    GPIO passthrough OWN-mux (I4, v0.1 -- NEW)
│   ├── test_gpio_mux_logic.py    independent OWN-mux truth table, pure Python (runs today)
│   ├── test_board_gpio.py        cocotb skeleton, skip-guarded (no RTL yet)
│   ├── Makefile
│   └── dut_notes.md
├── uart_bridge/                   UARTBR console bridge (I7, W-RTL-NEWIP -- NEW):
│                                  destructive-read FIFO windows, tready
│                                  backpressure/drop policies, SWO 8N1 capture
│                                  + sticky frame_err/overflow, 10ns/7ns CDC
├── swd_bb/                        SWDBB pin-wiggler (I5, W-RTL-NEWIP -- NEW):
│                                  DRIVE write-through, SAMPLE 2-FF latency,
│                                  >=0x08 aliasing pinned as documented
├── telem/                         TELEM INA228 CSR seam (I9, W-RTL-NEWIP -- NEW):
│                                  seam-mode default + `make FAKE=1` fake mode
│   (each of the above 3: test_*.py + Makefile + dut_notes.md)
├── edge/                          Hardware Hub / Edge Device API cross-check (I3, v0.1 -- NEW)
│   └── test_edge_channel_invalidation.py   doc-vs-doc + doc-vs-pyverify.edge cross-checks
├── firmware_logic/                firmware host-gcc harness wrapper (v0.1 -- NEW)
│   └── test_firmware_host_gcc_harness.py   shells out to `make -C firmware/test clean test`
└── integration/
    ├── test_swap_sequence.py     protocol-level swap-ordering model (ports swap_fsm.c/swap_fsm_transitions.c, I2+I25-resolved)
    └── test_manifest_roundtrip.py  gen_manifest.py -> pyverify.overlay round trip + §8A.5
```
