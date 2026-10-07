# Harness-image regression procedure

A **harness image** = a rebuilt static shell bitstream + its re-keyed MicroBlaze
firmware. This is the go/no-go gate that runs before one is trusted on the KU115.

The eight escapes this suite is built against all share one shape: **the thing
under test was not the thing that ships** — wrong parameter, stale artifact,
unverified diagnostic, unexercised state. The existing suite (38 host binaries —
`firmware/test/Makefile` `TESTS`, ~49 cocotb tests, SVA) is strong at testing
*models*; it is blind to the gap between a model and the artifact. So every gate
here either **compares the artifact to its source**, or **asserts on the real
silicon** — never adds another model-shaped test. Each gate names the bug it would
have caught; a gate that catches none was cut (see *Rejected gates*).

> **This table is maintained by hand, and was wrong.** Until 2026-09-09 it
> described 25 host binaries (there are 38), six Tier-3 gates driven through
> `pyverify deploy` (there are eight, driven through `swap_check` /
> `console_check` / `swd_check` and three xsdb probes), an ICMP-liveness probe
> that does not exist in the script, and a 512 KiB LMB
> (`firmware/platform/Makefile` says `LMB_KB ?= 1024`). Every row
> below has now been read back off `scripts/harness_regression.sh`. **Phase 2 will
> generate this table from the script** so it cannot drift again; until then, the
> script is the authority and this file is a description of it.

> **Tiers 0–1 no longer live only here.** All six board-free gates below now also
> run in `make check` and `make check-ci` (stage 2), so they execute on every push
> and PR. Before that they ran *only* under `make harness-regression` — a target
> nothing invokes on change — which meant the entire bug-#1 class had no gate in
> any CI job. See `docs/CI.md`.

## Run it

```
make harness-regression                      # tiers 0..2 (no board, no Vivado)
make harness-regression HR_ARGS="--only 0"   # just tier 0 (seconds)
source set_env.sh
make harness-regression HR_ARGS="--through 1" SIM=vcs      # + cocotb benches
make harness-regression HR_ARGS="--through 2 --with-vivado" # + static-DCP asserts
make harness-regression HR_ARGS="--through 3 --allow-board" # + on-board smoke
```

Or directly: `scripts/harness_regression.sh --through N`. **It stops loudly at the
first failing gate; higher tiers do not run.** Each tier is a hard precondition
for the next. Individual gates under `scripts/harness_gates/` are runnable
standalone (each prints its own PASS/FAIL and exits non-zero on failure).

---

## Tier 0 — static, seconds, no tools

Pure-Python source/artifact parity. This is the cheapest tier and the one that
catches the worst class, so it runs first and gates everything.

| Gate | Catches | Pass criterion |
|---|---|---|
| `check_bench_param_parity.py` | **#1 CSR decode width** | For every `(module, CONFIG.param)` the BD overrides on a CSR cell, some bench elaborates that module **at the shipped value**. The BD ships all six CSR blocks at `C_S_AXI_ADDR_WIDTH=32`; a bench that only elaborates the RTL default (12) is not testing what ships. Waivable via `param_parity_waivers.txt` (a tracked burn-down list, not a fix). |
| `check_bd_config_lint.py` | **#3 `validate_bd_design` insufficiency** | Every `DECOUPLED_VALUE` in `shell_bd.tcl` is a `0x`-hex string. A bare `0` passes `set_property` **and** `validate_bd_design`, then fails ~45 s into the build at IP generation. |
| `check_diag_mailbox_parity.py` | **#4 HW change moved a FW address** | The mailbox base implied by firmware `LMB_KB` (`LMB_KB*1024 − 0x80`) is in `mps3_diag.tcl`'s magic-scan `CANDIDATES`. Also **advisory**: shell BRAM (`Write_Depth_A×4`) vs firmware `LMB_KB` lockstep, and any *other* script hardcoding a mailbox literal. |
| `check_packaged_ip_fresh.py` | **#2 stale packaged IP** | `package_csr_ip.tcl` still contains the mtime staleness guard (a revert to the unconditional skip fails here). **Advisory**: any `ip_packaged/<b>/component.xml` older than its source. |

All four also run in `make check` / `make check-ci` stage 2, each guarded on its
own tracked input so a fresh clone SKIPs with a printed reason instead of failing.

**Active waiver (1).** `jtag_bb C_S_AXI_ADDR_WIDTH`, added 2026-09-09. The A6
SWD→JTAG cutover made `jtag_bb` the live debug block (`JTAGBB` @ `0x44A7_0000`,
shipped at width 32) but never registered it in the parity gate's `CSR_BLOCKS` —
so the gate was checking `swd_bb`, which the BD no longer instantiates, and saying
nothing at all about `jtag_bb`. Registering it exposed the real gap: **no bench
elaborates `jtag_bb` at 32**. Its decode carries the bug-#1 fix by construction
(`LOCAL_ADDR_W`, byte-identical to the mutation-verified `swd_bb`), so the waiver
records *unproven*, not *broken*. Burn it down with a `BLOCK=jtag_bb` arm in
`tests/csr_decode_width/` — see the TODO in `param_parity_waivers.txt`.

## Tier 1 — minutes, host-gcc + cocotb

| Gate | Catches | Pass criterion |
|---|---|---|
| `check_diag_field_parity.py` | **#6 a diagnostic that lied** | `mps3_diag_publish()`/`mps3_diag_snapshot()` copy the **whole** `mps3_diag_t` (since `86f6cfa`), so no field can be dropped in transport; the gate asserts that structure holds **and** that every field `diag.h` declares is written by some producer — a struct copy carries a never-written field faithfully, and it reads back zero forever. Rewritten 2026-09-09: the field-by-field scan it replaced had found 0 counters since `86f6cfa` and printed OK over an empty set. **Advisory**: fields only reachable over JTAG, not the 6900 `diag` verb. |
| `make -C firmware/test test` (38 binaries) | **#5 FSM ordering, #7 idle timeout** | The host binaries pass. `test_swap_fsm_transitions.c` pins `STREAM_PARTIAL → RELEASE` (RM_ID read *after* releasing DECOUPLE, #5 — line 111) and `await_timeout → SWAP_FAILED` on the two former no-bound `AWAIT_*` states (#7 — lines 288/301/306); `test_swap_fsm_faults.c` covers the confirm-timeout fail-closed paths. |
| `tests/csr_decode_width` (cocotb, `SIM=vcs`) | **#1 at the shipped width** | Elaborates `uart_bridge` at `C_S_AXI_ADDR_WIDTH=32` and drives base+offset. Fails on the un-fixed decode. |
| `tests/uart_echo_integration` (cocotb) | join/CDC gap | The DUT→uart_bridge async-FIFO→CSR path co-simulated end to end (banner crosses the CDC, echo round-trips, slow host loses nothing). |

Cocotb benches **SKIP** cleanly when `SIM` is unset, so Tier 1 still runs the
host-gcc + static gates on a box without a simulator.

## Tier 2 — build-time, assert on the artifact

| Gate | Catches | Pass criterion |
|---|---|---|
| `check_impl_reports.py` | timing / reset methodology | From `fpga/shell/build_results_*/`: **WNS > 0** (hard), **0 DRC errors** (hard; warnings reported), **`report_methodology` LUTAR-1 == 0** (advisory — a LUT driving an async reset is the R7 reset-fix smell). |
| `tier2_dcp_assert.tcl` (`--with-vivado`) | **#1, #2, #3** on the netlist | `open_checkpoint` the static synth DCP and assert: all six soclabs CSR blocks present (paired with #2's freshness gate = the RTL that ships was repackaged); reset-sync registers present; **ASYNC_REG cell count > 0** (CDC syncs kept their attribute); `dfx_decoupler` has a **real boundary** (>2 `rp_`/`s_` pins, not just the handshake — an unconfigured decoupler clamps nothing). |

The DCP-open asserts are **opt-in** (`--with-vivado`) so a busy build machine is
never disturbed by default. `tier2_dcp_assert.tcl` is written to real 2024.1 Tcl
and syntax-validated (tclsh with the Vivado commands stubbed); the semantic run
belongs in CI after a shell build. Vivado: `/apps/Xilinx/Vivado/2024.1/bin/vivado`.

## Tier 3 — on-board smoke (needs the board)

**Refuses to run** unless `scripts/mps3_board.sh preflight` says the lease is held
by us **and** `--allow-board` is given. Ordered so the cheapest, most diagnostic
check runs *before* anything streams a bitstream. All JTAG is `mrd`/`mwr` on a
running MicroBlaze — **never `stop`/`con`** (halting mid-swap resets the TCP
connection and destroys the transfer).

| # | Gate | Toggle | Catches | Pass criterion |
|---|---|---|---|---|
| 1 | `tier3_csr_liveness.tcl` (xsdb) | always | **#1 — the 5-second worst-bug catch** | Write-readback `UARTBR.SWO_CFG` (side-effect-free) and `DFXCTL.DECOUPLE` (the literal silicon symptom, save/restore) over JTAG. Value sticks ⇒ decode is live. A dead decode reads 0 ⇒ **STOP, do not swap**. |
| 2 | `scripts/mps3_diag.tcl` (xsdb) | always | **#4** | A MicroBlaze target carries magic `0xD1A6C0DE` at a scanned `CANDIDATES` base. Confirms the mailbox is where *this* firmware put it. |
| 3 | `dut_rx_check.tcl` (xsdb) | `MPS3_RUN_DUTRX` (**0**) | DUT reception | `gen_checker` drives the loaded DUT's RMII; `DFXCTL.RM_STATUS[2]` (`dut_eth_irq`) must go 0→1. The **only** witness that a frame crossed the partition boundary *into* the DUT — no host-side gate can see it, because the DUT return path does not exist. Off by default: it needs an RX-capable RM already resident. Name it with `MPS3_DUTRX_RM_ID` (default `0x01000002`, eth_ss); the id is an argument, not a constant, because the previous copy of this probe hard-coded one RM and rotted. |
| 4 | `ping_check.py` → 6900 | always | identity | `{"op":"ping"}` returns the expected `shell_id`. |
| 5 | `swap_check.py --rm regdemo_b` | `MPS3_RUN_SWAP` (1) | **#5, #7** | A full swap: `rm_id` reads back `0x010000B2` *after* DECOUPLE is released (#5 ordering), and the FSM never parks (#7). |
| 6 | `swap_check.py --rm regdemo_a` | `MPS3_RUN_SWAP` (1) | **#7** | Swap-away re-isolates cleanly (`0x010000A1`); the a↔b cycle proves both clearings fit the arena. |
| 7 | `swap_check.py --rm uart_echo` + `console_check.py` 6930 | `MPS3_RUN_CONSOLE` (1) | join | Swap in `rm_uart_echo` (`0x01000004`), then round-trip a probe on TCP 6930 — the on-silicon twin of `uart_echo_integration`. |
| 8 | `swap_check.py --rm nanosoc` + `swd_check.py` 6921 | `MPS3_RUN_SWD` (**0**) | debug join | Swap in nanoSoC (`0x01000001`), then openocd `remote_bitbang` against the firmware `jtag_server`: **TAP IDCODE `0x6ba00477`**. Runs LAST, only because it leaves nanoSoC resident. The old "nanoSoC cannot cleanly swap away (clearing overflow)" note is obsolete: the 256 KiB arena holds the largest clearing (see below), and on 2026-09-22 greybox was restored straight after nanoSoC (`docs/evidence/2026-09-w2/summary_20260922T113359Z.txt`). |

**Gate 8 was retargeted 2026-09-09 and has NOT been run on the board in this
form.** It previously ran `swd_remote_bitbang.cfg` against **6920** (`swd_server`)
asserting `SWD DPIDR 0x0bb11477`. That port is **dormant** on the fielded
`0xA8C1C535` shell — the A6 cutover moved the live, silicon-proven debug path to
`jtag_server`/**6921**, JTAG `remote_bitbang`, TAP `0x6ba00477`. So the one gate
that claims to prove "swap-then-debug joins up" was aimed at a port nothing
answers, and would have failed for a reason unrelated to what it tests. The
retarget is proven only as far as the **argv** it builds: `swd_check.py --dry-run`
prints the exact command and `tests/integration/test_swd_check_argv.py` pins it.
The on-silicon run is pending a lease.

**Both xsdb gates and `dut_rx_check.tcl` need `MPS3_HW_URL`.** They read it from
the environment and refuse loudly when it is unset. Until 2026-09-09 gates 1 and 2
hard-coded `set HUB_URL "tcp:<hub-fqdn>:3121"` — a literal placeholder with **no
override** (since `cacbb2d`, 2026-08-07) — so every Tier-3 run died inside
`connect -url` before reaching the shell, and the failure looked like a board
fault rather than a missing setting. Export it (FQDN required; a bare host name
fails inside `hw_server`):

```sh
export MPS3_HW_URL=tcp:<hub-fqdn>:3121
```

`MPS3_HUB` is needed **only** for the via-hub dataplane relay
(`MPS3_BOARD_VIA_HUB=1`). It used to be dereferenced unguarded under `set -u` at
script line 36, which aborted the *whole run* — including the six board-free
Tier-0/1 gates — with `MPS3_HUB: unbound variable` on any box that had not
exported it. Between that and nothing in CI invoking this script, the Tier-0
gates were both **uninvoked and unrunnable**.

**#8 (QSPI first light) is a known-open hazard, not a fixed bug** (see
`docs/QSPI_CLEARING_CACHE_HW_FINDINGS.md`): the clearing-cache flash write blocks
the superloop and the board goes dark (100 % ICMP loss).

**The ICMP-liveness watchdog this section used to describe does not exist.** No
such step is in `scripts/harness_regression.sh`, and none ever was — the paragraph
described an intended guard as though it were a running one, which is the same
failure the whole suite is built against. What *does* limit the exposure today is
`check_clearing_fits.py` (in `make check-ci`): every RM's clearing fits the RAM
arena, so no clearing reaches QSPI in the first place. The largest is
`nanosoc_multicore` at 221 932 B against a 262 144 B arena — **84 % full**, which
is a margin, not a fix. If a future RM overflows it, that gate goes red *before*
anything touches the board. Writing the watchdog is still open.

---

## Bug → gate traceability

| Bug | Gate | Tier |
|---|---|---|
| #1 CSR decode width | `check_bench_param_parity.py`; `tests/csr_decode_width`; `tier3_csr_liveness.tcl`; DCP CSR-present | 0 / 1 / 3 / 2 |
| #2 stale packaged IP | `check_packaged_ip_fresh.py`; DCP CSR-present | 0 / 2 |
| #3 validate_bd insufficient | `check_bd_config_lint.py` | 0 |
| #4 HW moved a FW address | `check_diag_mailbox_parity.py`; `mps3_diag.tcl` scan | 0 / 3 |
| #5 HW broke a FW invariant (RM_ID/DECOUPLE order) | `test_swap_fsm_transitions/_faults`; on-board swap | 1 / 3 |
| #6 a diagnostic that lied | `check_diag_field_parity.py` | 1 |
| #7 dead client wedged the shell | `test_swap_fsm_transitions` (await_timeout → FAILED); on-board swap-away | 1 / 3 |
| #8 QSPI first light (open) | `check_clearing_fits.py` keeps every clearing in RAM (the ICMP watchdog is unwritten) | 0 |
| DUT never received a frame | `dut_rx_check.tcl` (`RM_STATUS[2]`) | 3 |

## Findings surfaced while building this suite

1. **CLOSED (2026-07-10), then reopened by a rename.** All six original CSR
   blocks now have a `tests/csr_decode_width` arm at `C_S_AXI_ADDR_WIDTH=32`, each
   mutation-verified, and `param_parity_waivers.txt` records the burn-down. But the
   A6 SWD→JTAG cutover replaced `swd_bb` with **`jtag_bb`** as the live debug
   block and nobody registered the new name, so the gate went on checking a block
   the BD no longer instantiates. `jtag_bb` ships at 32 with **no bench at all** —
   found 2026-09-09, the moment this tier was first wired into CI. One active
   waiver; see Tier 0 above.
2. **Shell/firmware LMB is in lockstep at 1024 KiB.** Both `shell_bd.tcl:392`
   (`Write_Depth_A {262144}` × 4 B) and `firmware/platform/Makefile:44`
   (`LMB_KB ?= 1024`) say 1 MiB, and `check_diag_mailbox_parity.py` confirms it on
   every run (`mailbox base = 0x000FFF80`). This finding described a 512-vs-256
   disagreement that no longer exists; the *check* that would catch it recurring is
   live. **The prose was a separate problem**: `fpga/shell/README.md` announced
   "Current build: 2026-07-07, 256 KiB LMB" until two mints after the board had
   moved to 1 MiB, and nothing could see it, because no file recorded the fielded
   LMB size to compare against. (That quotation needs the word "until" to pass the
   gate now guarding it — which is the rule working, not a workaround: a sentence
   quoting a dead claim must say it is dead.) `docs/FIELDED_SHELL.md` now carries an `lmb_kb` row and
   `check_fielded_shell_claims.py` gates present-tense LMB/BRAM size claims against
   it.
3. **`report_methodology` LUTAR-1 = 1** on the 2026-07-09 impl (a LUT driving an
   async reset). Advisory today; make it hard once the reset path is clean.
4. **`mps3_diag_snapshot()` drops `tx_last_status`** — published every pass and
   visible over JTAG, but invisible to the 6900 `diag` verb. Latent (the verb
   does not encode it yet); flagged so it is not the next #6.

## Gates considered and REJECTED

- **Re-run `validate_bd_design` as a gate.** Rejected: bug #3 is the proof it is
  insufficient — a bad `DECOUPLED_VALUE` passes it. The static lint is cheaper
  and actually catches the failure. (We still run it in the build; it is just not
  a *sufficient* gate.)
- **A cocotb bench per CSR block at width 32.** Rejected as redundant with the
  Tier-0 parity gate + one shared-template bench (`csr_decode_width`): the gate
  proves coverage exists at the shipped width for near-zero cost; adding six near
  identical benches is more of the same shape. (The parity gate *names* the five
  uncovered blocks so the one real bench can be generalised instead.)
- **Assert a specific fixed-decode net name in the DCP.** Rejected as brittle
  (synthesis renames nets). The Tier-0 parity gate + Tier-3 liveness probe pin
  the *behaviour*; the DCP gate asserts only robust facts (cell presence, ASYNC_REG
  count, decoupler pin count).
- **A runtime diag round-trip host test.** Considered for #6, rejected in favour
  of the static field-parity check: it needs no compiler, cannot be defeated by a
  green build, and reads the same source the bug lives in.
- **Golden-bitstream binary diff.** Rejected: a shell rebuild legitimately mints a
  new `static_id`; a byte-diff gate would be all false positives. The #2 guard is
  the source→artifact freshness check, not a bitstream compare.
