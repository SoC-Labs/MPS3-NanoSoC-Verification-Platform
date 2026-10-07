# Verification & Robustness Assessment — MPS3 nanoSoC platform

> **Status: HISTORICAL** — a record of a board-free verification & robustness assessment at repo HEAD `5387a8a` as of 2026-07-10.
> Superseded by / current state in [docs/HARNESS_REGRESSION.md](HARNESS_REGRESSION.md) and [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `QSPI_CLEARING_CACHE_HW_FINDINGS.md` — internal notes, not in the public tree.

**Date:** 2026-07-10 · **Repo HEAD:** `5387a8a` (master) · **Author:** board-free
review (read / reason / run host + Tier-0/1 only; no board, no Vivado impl).

This assessment judges the current suite against **one failure mode**, the one this
platform keeps re-learning:

> **the thing under test was not the thing that ships** — a bench elaborated an IP
> at width 12 while the BD shipped 32; `package_csr_ip` skipped stale IP; a diag
> field was published but never snapshotted; a gate compared two *generated*
> artefacts and proved only that the generator is deterministic.

Line coverage is not the question. The question is: **where is the suite GREEN and
the shipped design still wrong, and how would a real failure escape today?**

---

## 0. Headlines (answers to the three asked questions)

**Top 3 verification gaps by escape-risk:**

1. **The bus-level hang has no watchdog, no test, and no self-recovery.** The
   platform's signature silicon symptom — "goes dark, 100 % ICMP loss, JTAG
   reflash to recover" (`QSPI_CLEARING_CACHE_HW_FINDINGS.md`) — is, per the
   handover's own analysis (§3d), *"a bus access that never completes, or memory
   corruption."* Every firmware loop is bounded, but a bound only helps if the AXI
   access **returns**. An `HREADY` that never asserts hangs the superloop below the
   FSM, and there is **no MicroBlaze watchdog** (grep: WDT lives only in the
   LAN9220 driver). Nothing tests it; nothing recovers from it. This is the
   least-covered load-bearing behaviour and the one that actually bit silicon.

2. **The RM_ID-clamp assumption — the entire basis of the R1 "verify-after-release"
   ordering — is unverified board-free.** `swap_fsm` reads `DFXCTL.RM_ID` *after*
   releasing DECOUPLE precisely because "the decoupler CLAMPS rm_id to 0" while
   asserted (`swap_fsm_transitions.c:76-81`). But `tests/dfx_ctl` says `rm_id_i` is
   wired **"port-level only"** (test docstring item 4), and `tests/integration/
   test_swap_sequence.py` runs against an **"idealized-vendor-IP regs helper"** — it
   *models* the clamp, it does not test it. If the real Xilinx `dfx_decoupler` is
   configured to pass `rm_id` through when decoupled, `SWAP_VERIFY` "verifies"
   against passthrough bits and the whole confirmation is theatre — green in every
   host + sim test, wrong on silicon. This is bug #1's exact shape at the
   firmware↔vendor-IP seam.

3. **The Tier-0 parameter-parity gate covers only 6 hardcoded soclabs CSR blocks —
   not "every BD-instantiated IP."** `check_bench_param_parity.py` hardcodes a
   6-entry `CSR_BLOCKS` dict; it does **not** auto-discover soclabs cells from the
   BD, and it covers **no vendor IP**. The `C_SPI_MODE` mismatch that plausibly
   *caused* the QSPI silicon failure was caught by a bespoke literal-string lint
   (`check_bd_config_lint.py`), not by the parity gate. A 7th soclabs CSR block, or
   any vendor-IP parameter the BD overrides away from its bench value, escapes with
   no general guard. The gate that names itself "the generalisation the post-mortem
   asked for" is not yet general.

**Is the QSPI bounded-promote actually wired into `swap_fsm`?**
**YES — it is now wired** (contrary to the note the task quotes). The stale caveat
is `QSPI_CLEARING_CACHE_HW_FINDINGS.md:12-15` ("plumbed, not yet wired"), written
against `6db5f43` (the API). Commit **`ad74795`** ("drive the STAGE→CACHE promote
one sector per poll", newer than `6db5f43`) wired it: `swap_fsm.c:1096-1136`
(`step_cache_clearing()`, the `in_qspi` branch) calls `overlay_store_promote_begin()`
then exactly one `overlay_store_promote_step()` per poll, bounded by
`MPS3_SWAP_CONFIRM_POLL_MAX` with a fail-closed `overlay_store_promote_abort()`. It
is dispatched at `swap_fsm.c:1303` and host-tested in `test_swap_fsm_hw.c:425-494`
(spans-multiple-polls + the BUSY-forever wedge guard). **Caveats:** (a) it is
host-sim only, never exercised on silicon; (b) the *recommended* board build is
QSPI-free (`in_qspi==0`), so on the config you would actually flash **this whole
path is dead code** — it protects a path the recommended build does not take. The
doc note should be corrected.

**Single most valuable addition next:**
An **automated, lease-gated board-in-the-loop Tier-3 CI stage** (even nightly, one
board). It is the only backstop that converts the *entire* sim-green body — the swap
FSM, QSPI promote, keepalive, session-abort, and the RM_ID clamp — from
INFERRED-on-silicon to VERIFIED, and it directly targets this platform's signature
failure (sim-green, silicon-wrong). Today Tier 3 exists but is **run by hand**, and
gates 4-6 (swap / swap-away / console) are not even automated — the script only
*prints operator instructions* (`harness_regression.sh:176-181`).
*Highest-value **board-free** unit to add:* an assertion that proves the
`dfx_decoupler` clamps `RM_ID` while DECOUPLE is asserted (gap #2 above).

---

## 1. Method — VERIFIED-by-running vs INFERRED-by-reading

**VERIFIED (I ran these this session):**

| What | Command | Result |
|---|---|---|
| 31 firmware host-gcc binaries | `make -C firmware/test test` | **ALL PASS (31 binaries)**, 2,000+ checks |
| Tier-0 + Tier-1 harness gates | `SIM=vcs scripts/harness_regression.sh --through 1` | **OK — 8 gates passed** (exit 0) |
| `tests/csr_decode_width` @ width 32 | (within Tier 1, VCS) | 4/4 PASS |
| `tests/uart_echo_integration` (DUT↔CDC) | (within Tier 1, VCS) | 4/4 PASS |
| promote-step wiring | `git show ad74795`, read `swap_fsm.c` | wired, confirmed |
| CSR width-32 burn-down | read `param_parity_waivers.txt` + benches | **zero active waivers**, 6/6 blocks covered |
| clkrst bench clocks `dut_clk_i` | read `tests/clkrst/test_clkrst.py:67` | yes (RTL header comment is stale) |
| cocotb bench inventory | `list_benches.py --summary` | `ready=18 skip=0` |

**INFERRED (read, did NOT run — board/Vivado off-limits, or not executed here):**
full 18-bench cocotb suite (105 tests; I ran 2 benches), `pytest tests/` (141) and
`host/pyverify` (228) — these are the handover's numbers, not re-run here; **all of
Tier 2** (impl reports / DCP asserts); **all of Tier 3** (every on-silicon claim).

---

## 2. Verified-vs-unverified matrix (load-bearing behaviours)

Legend: **H** host-gcc test · **S** sim bench (cocotb/pytest) · **B** on-board gate
(Tier 3) · **—** nothing. "board" = proven on silicon per committed evidence.

### 2a. Swap-FSM states — each fail-closed path

| State | Fail-closed path | H | S | B | Notes |
|---|---|:-:|:-:|:-:|---|
| `SWAP_GATE` | none (one-shot poke, unconditional →DECOUPLE) | H | S | B* | no failure arc by design |
| `SWAP_DECOUPLE_ASSERT` | `confirm_timeout → FAILED` | **H** | S | B* | `test_swap_fsm_faults.c:136` |
| `SWAP_STREAM_CLEARING` | `!clearing_cache_valid → FAILED`; `stream_error → FAILED` | **H** | — | — | `test_swap_fsm_faults/_transitions`; **no RTL-ICAP co-sim** |
| `SWAP_AWAIT_INCOMING_CLEARING` | `await_timeout → FAILED` (f5825db) | **H** | S | **B** | Tier-3 gate 5 exercises it |
| `SWAP_AWAIT_PARTIAL` | `await_timeout → FAILED` | **H** | S | **B** | " |
| `SWAP_STREAM_PARTIAL` | `stream_error → FAILED` | **H** | — | — | pure-table only; ICAP stuck-write modelled by fake |
| `SWAP_VERIFY` | `verify_mismatch \|\| confirm_timeout → REISOLATE` | **H** | S | B* | I25 real compare; **clamp assumption unverified (gap #2)** |
| `SWAP_CACHE_CLEARING` | promote wedge → `abort()` + fail-closed | **H** | — | — | bound lives in *impure* `swap_fsm.c`, **not** the pure table → only `test_swap_fsm_hw.c` covers it; **QSPI, never on silicon** |
| `SWAP_RELEASE` | `confirm_timeout → FAILED` | **H** | S | **B** | `test_swap_fsm_faults.c:185` |
| `SWAP_REISOLATE` | `confirm_timeout → FAILED` | **H** | S | — | verify-after-release reorder |

*B\* = the state is traversed on every real swap (Tier-3 gate 4), but its **failure
arc** is not deliberately provoked on-board.*

**Adversarial read:** every fail-closed *transition* is host-tested via the pure
table (`test_swap_fsm_transitions.c`, 50 checks) — but the pure table is a **model**.
Two arcs that matter most live **outside** the table: (i) the `SWAP_CACHE_CLEARING`
promote wedge-bound is an early `return` in the impure poll loop, so only
`test_swap_fsm_hw.c` sees it; (ii) `stream_error` is fed by the fake HWICAP, never
by real ICAP silicon or an RTL model. No failure arc is provoked on-board.

### 2b. Robustness fixes (this session)

| Fix | Commit | H | S | B | Verdict |
|---|---|:-:|:-:|:-:|---|
| AWAIT_* idle timeout | `f5825db` | **H** | S | — | host+model solid; on-board **untested** (Tier-3 gate 5 would) |
| Session abort (free push socket on fail) | `304970e` | **H** | — | — | `test_config_agent_keepalive`/`_e2e_net`; **host only** |
| lwIP TCP keepalive (reap dead peer) | `6b6dca1` | **H** | — | — | `test_config_agent_keepalive.c` (26 checks) drives a **fake** `net_if`; real lwIP `keep_idle/cnt` timing **never run** (host or silicon) |
| Ungate on FAILED (not only DONE) | `15c05df` | **H** | — | — | `test_swap_fsm_hw.c`, `test_swap_fsm_faults.c` |
| verify-after-release + REISOLATE | (R1) | **H** | S | board | traversed on silicon (`769518b` run) — but see gap #2 |
| Fail-closed ICAP (stream_error) | — | **H** | — | — | modelled by fake; **no RTL ICAP** |
| QSPI bounded-promote (wired) | `6db5f43`+`ad74795` | **H** | — | — | wired + host-tested; **QSPI path dead in recommended build**; silicon **untested** |

**The keepalive is the sharpest instance of the platform's own lesson:** the fix for
"a dead peer wedges the shell" is proven only against `fake_net_if.c`. The thing that
ships is lwIP's real keepalive timer (`net_if_lwip.c:89-130`, reap = 15 s + 4×5 s =
35 s). *The thing under test is not the thing that ships.*

### 2c. Proven interfaces (the silicon-facing surface)

| Interface | Committed status | H | S | B | Board-free reproven? |
|---|---|:-:|:-:|:-:|---|
| LAN9220 + lwIP + ICMP (network) | ✅ HW-PROVEN | H | — | **board** | driver `test_smsc911x.c` (174 checks); ICMP is board-only |
| MicroBlaze superloop + control TCP 6900/6910 | ✅ HW-PROVEN | **H** | S | **board** | `config_agent`/`coordinator` host + `fakeshell` conformance |
| JTAG/MDM diag mailbox | ✅ HW-PROVEN | **H** | — | **board** | `test_diag.c`, `check_diag_*` gates; magic scan is board-only |
| SWD to DUT (first light) | ✅ HW-PROVEN (`785f2d9`) | **H** | — | **board** | `test_swd_server.c` (91) — bit-bang model only |
| Console 6930 (uart_over_eth) | 🟨 built | **H** | **S** | — | `uart_echo_integration` co-sims the CDC; **on-board is Tier-3 gate 6, manual** |

Each interface's **client/driver logic** is host-tested; each interface's **silicon
edge** (the actual LAN9220 register bring-up, the MDM mailbox, the SWD/AP handshake)
is HW-PROVEN by the project lead's live sessions but has **no automated re-proof** — a rebuild
that breaks any of them is caught only by a human re-running the board.

### 2d. CDC / reset RTL

| Element | H | S | B | Notes |
|---|:-:|:-:|:-:|---|
| UARTBR async FIFO (DUT→CSR CDC) | — | **S** | board | `cdc_gray_checker.sv` **bound** to `uartbr_async_fifo`, exercised by `uart_bridge`/`csr_decode_width`/`uart_echo_integration`. Strongest CDC coverage in the repo. |
| Reset gen (async-assert / sync-deassert) | — | **S** | board | `dut_clkrst.sv` 3-FF Cummings gens; bench **now** clocks `dut_clk_i` + waits `_SETTLE` edges (`test_clkrst.py:67`). **The RTL header comment claiming a "bench gap" is STALE** — the bench was fixed. |
| `rp_resetn` gated by `dfx_ctl.rp_resetn_gate_i` | — | **S** | board | `test_clkrst.py` + `test_dfx_ctl.py` cross-block |
| Tier-2 DCP: ASYNC_REG count > 0, reset-sync cells present | — | — | (Tier2) | **cell-presence only, not behaviour**; opt-in `--with-vivado`, **not run here** |

CDC/reset is the **best-covered** RTL area (bound SVA + a fixed reset bench). The
residual is that Tier-2's DCP check asserts cells *exist*, not that synthesis kept
the CDC *intent* — acceptable given the SVA, but note it is `--with-vivado` and was
not exercised in this review.

---

## 3. Gaps that matter most (prioritised by escape risk)

Ordered by "how a real failure escapes today."

### G1 — The bus-hang / no-watchdog / no-recovery path (escape: CRITICAL)
The one failure that actually took the board dark on silicon is the one with the
**least** coverage. Bounded loops assume the AXI access returns; a stalled `HREADY`
or corrupted state does not, and there is no watchdog to recover. See §5.1.

### G2 — RM_ID clamp assumption unverified board-free (escape: HIGH)
Detailed in §0. The firmware's central safety invariant ("a released, verified
`rm_id` proves the right RM loaded") rests on vendor-IP behaviour that **no board-free
test exercises**. `test_swap_sequence.py` idealises it; `test_dfx_ctl.py` calls it
port-level-only. If the decoupler config is wrong, verify is green and hollow.

### G3 — Param-parity gate is narrow (escape: HIGH, latent)
Detailed in §0. Covers 6 hardcoded soclabs blocks; no auto-discovery; no vendor IP.
The gate that catches bug #1 would not catch bug #1's sibling on a 7th block or on
`axi_quad_spi`/`axi_hwicap`/`axi_emc`.

### G4 — Every robustness fix is host-sim only; no board-in-the-loop CI (escape: HIGH)
keepalive, session-abort, AWAIT-timeout, QSPI promote — **none proven on silicon**,
and Tier 3 is manual (and its swap/console steps are un-automated print statements).
The platform whose thesis is "reachable over the wire" has no automated wire test.

### G5 — QSPI promote is wired but dead-on-recommended-build and silicon-untested (escape: MEDIUM)
It is correct to have bounded it, but note the honest scope: the recommended build is
QSPI-free, so the bound protects a path that build never takes; and if a future build
*does* turn QSPI staging on, it hits **both** this untested-on-silicon promote **and**
the D16 shared-flash hazard (`overlay_store` and nanoSoC boot map share one physical
SST26VF064B). The bound is a seatbelt on a car that is currently parked.

### G6 — No systematic mutation / fault injection (escape: MEDIUM, meta)
Mutation checking this session was **by hand** (`param_parity_waivers.txt`: "each was
mutation-verified — revert `LOCAL_ADDR_W` → the tests go red"). The only encoded
fault injection is `test_qspi_fault_inject.c` (one fake, SPI faults). There is no
framework that proves the *suite as a whole* has teeth — i.e. that a deliberately
broken shipping artefact is actually caught. Given this platform's entire bug history
is "green suite, broken artefact," a mutation harness is unusually well-motivated
here (see §4, B6).

---

## 4. What to IMPLEMENT — concrete backlog

Each item = gap · test/gate to add · tier · the failure it catches. Items are
accepted only if they catch a **real** failure mode; rejected ideas are in §6.

| # | Add this | Tier | Catches (historical or hypothetical) |
|---|---|:-:|---|
| **B1** | **MicroBlaze hardware watchdog** (or `axi_timer` heartbeat) + firmware kick in the superloop; **RTL/firmware feature**, then a Tier-3 hazard probe: force a wedge (e.g. read an unmapped AXI window) and assert the board **recovers** (ICMP returns) without JTAG. | 3 (feature) | G1 — the actual silicon freeze. Converts "needs JTAG reflash" → self-recovery. Nothing else addresses the root symptom. |
| **B2** | **RM_ID-clamp assertion.** Board-free: a Tier-2 DCP assert that `DFXCTL.RM_ID`'s source net is inside the `dfx_decoupler`'s clamped set (not just pin-count > 2); *and* a cocotb bench that drives a decoupler model with DECOUPLE=1 and asserts `rm_id_o == 0`. On-board: promote Tier-3 gate 4 to explicitly read `RM_ID` **while decoupled** and assert it is 0/invalid *before* release. | 2 + 3 | G2 — a hollow `SWAP_VERIFY`. This is bug #1's shape at the vendor-IP seam. |
| **B3** | **Generalise `check_bench_param_parity.py`:** auto-discover every `create_bd_cell -vlnv soclabs.org:user:*` from the BD (drop the hardcoded 6-entry dict); and add a **second gate** that lists every *vendor* IP CONFIG override against a tracked allow-list, so a `C_SPI_MODE`-class change must be acknowledged. | 0 | G3 — a 7th soclabs block, or the next `C_SPI_MODE`, shipping unbenched. |
| **B4** | **Board-in-the-loop CI stage:** wire `harness_regression.sh --through 3 --allow-board` into a nightly lease-gated runner, and **automate gates 4-6** (swap / swap-away / console) instead of printing operator text (`:176-181`). | 3 (CI) | G4 — every robustness fix currently unproven on silicon; the platform's signature "sim-green, silicon-dark". |
| **B5** | **Real-lwIP keepalive test:** a loopback/QEMU or `netif`-backed test that runs the actual `net_if_lwip.c` keepalive timer (not `fake_net_if.c`) and asserts the 35 s reap fires and frees the session. | 1 | G4/keepalive — the fix is proven against a fake, not the shipping timer. |
| **B6** | **Mutation harness:** a script that applies a catalogue of source mutations (revert `LOCAL_ADDR_W`; drop a `publish()` line; unbound an `AWAIT_*`; skip `promote_abort()`) and asserts the suite goes **red**. Encodes the by-hand mutation checks as a repeatable gate; reports any mutation the suite survives. | 0/1 | G6 — a test that lost its teeth in a refactor (exactly `0d86c1c`: the bench runner was dead for 10 h and nothing noticed). |
| **B7** | **ICAP-behaviour bench:** a cocotb bench with an `axi_hwicap` model (or a faithful SR_DONE/CR-self-clear stub in RTL) so `stream_error` fail-open→closed is exercised against a **timing model**, not a C fake. | 1 | `SWAP_STREAM_CLEARING/PARTIAL` — a stuck ICAP silently "verifying" a load that never happened, on real handshake timing. |
| **B8** | **Correct the stale docs** (not a test, but a verification-integrity fix): `QSPI_CLEARING_CACHE_HW_FINDINGS.md:12-15` ("plumbed, not wired") is false since `ad74795`; `dut_clkrst.sv:33-44` ("bench gap") is false since the bench was fixed. Stale "known-gap" notes are how a closed gap gets re-opened by a well-meaning revert. | — | meta — a doc that lies about coverage is a diag that lied (bug #6, in prose). |

**Priority order for effort:** B4 (systemic backstop) → B2 (highest board-free
escape) → B3 (cheap, closes the "narrow gate" class) → B1 (root-cause of the signature
failure, but largest effort) → B6/B5/B7 → B8.

---

## 5. Robustness — remaining "no self-recovery / needs JTAG reflash" paths

### 5.1 Bus access that never completes (superloop hang) — NOT covered
`main.c:202` superloop does bounded work per pass, but a single AXI transaction with
no `HREADY` (or memory corruption) hangs it below the FSM. Handover §3d names this as
the actual freeze mechanism and rules out the firmware spin. **No watchdog exists.**
*Fix:* B1 (watchdog + recovery). *Test that would prove it:* B1's Tier-3 hazard probe
— provoke a wedge, assert ICMP returns without JTAG.

### 5.2 Boot-time dead-ends — partial coverage
- `main.c:173` — LAN9220 bring-up failure → `for(;;)` with a fast LED blink, **no
  recovery** (defensible: nothing network-facing can run without the port, but it is
  a JTAG-reflash state). *Test:* a host test asserting `mps3_net_lwip_init` failure
  reaches the park with the right rc (the rc plumbing exists; the park is untested).
- `harness_app/main.c:475,489` — `for(;;) { /* nothing to recover into without a
  timer/INTC */ }`. Dead-ends by admission. B1's timer/INTC would give these a
  recovery target.

### 5.3 QSPI clearing-cache on a QSPI build — bounded, but silicon-untested + D16 hazard
The promote is now bounded and fail-closed (§0), so it no longer *parks* the FSM. But
it has **never run on silicon**, and turning QSPI staging on re-enters the D16
shared-flash collision (`overlay_store` vs nanoSoC boot map on one part). *Fix:* keep
the QSPI-free default (raise `SWAP_CLEARING_ARENA_BYTES` from 69,632 — handover §6.1 —
so QSPI-free is the *default*, not merely *available*); settle D16 before any board
QSPI run. *Test:* B4's on-board swap-away with an ICMP-liveness watchdog around the
clearing-cache write (already specified as Tier-3's #8 hazard probe).

### 5.4 config_agent single-session — now self-recovering (COVERED)
AWAIT timeout (`f5825db`) + session abort (`304970e`) + keepalive (`6b6dca1`)
together make a dead client self-heal. This is the one former JTAG-reflash path that
is genuinely closed — **in host sim**. It needs B4/B5 to be closed *on silicon*.

---

## 6. Rejected tests (adversarial — these do NOT catch a real failure)

- **A cocotb bench per CSR block at width 32.** Redundant with the Tier-0 parity gate
  + one shared `csr_decode_width` template. Already correctly rejected in
  `HARNESS_REGRESSION.md`; I concur.
- **Re-running `validate_bd_design` as a gate.** Bug #3 is the proof it is
  insufficient (a bad `DECOUPLED_VALUE` passes it). Keep it in the build, not as a
  gate.
- **Golden-bitstream binary diff.** A legitimate rebuild mints a new `static_id`; a
  byte-diff is all false positives. The source→artefact freshness check is the right
  #2 guard.
- **A new R9-style "manifests agree with `static_id.c`" check.** Handover §2c already
  showed this is self-referential: `make overlays` regenerates *both* from one source,
  so they always agree and disagree only with the board. *A gate comparing two
  generated artefacts proves only that the generator is deterministic.* Any new gate
  must compare artefact-to-**source** or assert-on-**silicon**.
- **More host tests of the swap FSM model.** The model is thoroughly tested (50 + 105
  + 24 checks). Adding model tests does not close the model↔silicon gap — B2/B4 do.
  The suite is already "strong at testing models; blind to the model/artefact gap"
  (HARNESS_REGRESSION.md's own words). Do not add more of the covered shape.

---

## 7. Where the suite is GREEN but the design may be WRONG (summary)

1. **`SWAP_VERIFY` verifies against a value the vendor IP is *assumed* to clamp**
   (§0 gap #2) — green everywhere, unproven at the seam.
2. **keepalive/session-abort proven against `fake_net_if`, not lwIP** (§2b) — the fix
   for "the thing that ships wedges" tested on a thing that does not ship.
3. **Parity gate green, but scoped to 6 blocks** (§0 gap #3) — the general failure it
   claims to catch can still ship on the 7th block or any vendor IP.
4. **QSPI promote green + bounded, but dead on the recommended build and never on
   silicon** (§5.3) — a well-tested seatbelt on a parked path.
5. **`stream_error` fail-closed green against a C fake ICAP** (§2a) — no real
   handshake timing has ever exercised it.
6. **Two docs assert coverage that no longer matches code** (B8) — the QSPI "not
   wired" note and the clkrst "bench gap" note both describe closed gaps as open; a
   revert trusting them would silently regress.

The suite is genuinely strong at the layer it targets (models, host logic, and — for
CDC — bound SVA). Its blind spot is uniform and namable: **the firmware↔silicon and
firmware↔vendor-IP seams.** Every top gap lives there, and every top backlog item
(B1, B2, B4) is an assertion that crosses that seam.
