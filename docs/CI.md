# Continuous Integration

The repo's health gate is `make check`. Until now nothing ran it on change, so
the driver / daemon / firmware / pytest corpus could rot silently between
manual runs -- the exact failure mode this repo has been bitten by before (a
green gate hiding an empty one). CI closes that: every push and pull request
runs the board-free half of `make check` automatically.

## Two gates, split by what they need

| gate | file | runner | what it runs | when |
|------|------|--------|--------------|------|
| **board-free** | `.github/workflows/ci.yml` | GitHub-hosted `ubuntu-22.04` | `make check-ci` (stages 1-5) + `make lint` (stage 6) | every push to **any** branch, and every PR |
| **full** | `.github/workflows/ci-full.yml` | self-hosted `[self-hosted, mps3-lab]` | `make check SIM=vcs` (stages 1-8) + opt-in board smoke | manual (`workflow_dispatch`) -- **dormant** until a runner is registered |

The split is not arbitrary: a stage is in the board-free gate **iff** it needs no
simulator, no Vivado, no built overlays, and no board. Everything else needs a
licensed simulator (VCS), the lab IP library, and/or the MPS3 board, none of
which exist on a hosted runner.

## The board-free gate (`ci.yml`)

Two independent jobs:

### `logic-gate` -> `make check-ci`

`make check-ci` is stages 1-5 of `make check`, carved out as its own target so it
is runnable locally and on a hosted runner:

1. **contracts** exist (`docs/contracts/*.md`)
2. **mechanical agreement gates** — `pin-check` (RM boundary), `check_rm_id_encoding.py`,
   `check_rm_id_literals.py`, `check_overlay_static_id.py` (R9), `check_clearing_fits.py`,
   `check_fielded_shell_claims.py` (see below), `check_socscope_overlay_rev.py`,
   `check_overlay_ip_class.py` (every manifest's `ip_class` vs `rm_list.tcl`), **plus the six
   folded in on 2026-09-09** (below)
3. **cross-workstream pytest** (`tests/`)
4. **host pytest** (`host/pyverify`, `host/socket_harness`, `host/webharness`)
5. **firmware + stage0 + daemon wire-contract** host-gcc harnesses

### The six gates folded into stage 2 (2026-09-09)

These are the Tier-0/1 gates of `scripts/harness_regression.sh`. Until this change
they ran **only** under `make harness-regression` — a target nothing invokes on
change: not `make check`, not `make check-ci`, not any workflow. So the entire
family they gate had no gate that actually *runs*, and an uninvoked gate emits no
output at all, which reads as a pass.

| gate | catches |
|------|---------|
| `check_bench_param_parity.py` | **#1** — a CSR block benched at a parameter it does not ship with. The escape that made every shell CSR read 0 on silicon. |
| `check_bd_config_lint.py` | **#3** — a `DECOUPLED_VALUE` that passes `validate_bd_design` and dies 45 s into IP generation; the QSPI safe-idle clamp; `axi_quad_spi_0` standard-SPI lockstep. |
| `check_diag_mailbox_parity.py` | **#4** — a hardware change (LMB size) moving a firmware address out from under every xsdb script. |
| `check_packaged_ip_fresh.py` | **#2** — a shell synthesised from stale packaged IP: a fresh `static_id` for RTL that did not change. |
| `check_diag_field_parity.py` | **#6** — a field declared in `mps3_diag_t` that no producer ever writes reads back zero forever; asserts the whole-struct copy structure holds and every declared field has a producer. |
| `check_impl_reports.py` | timing/DRC on the **implemented artifact**: WNS > 0, 0 DRC errors, LUTAR-1 (advisory). |

Each is guarded on its own tracked input and **SKIPs with a printed reason** rather
than failing when that input is absent. `check_impl_reports.py` skips itself (it
reads `fpga/shell/build_results_*/`, untracked Vivado output absent on every hosted
runner), so a fresh clone stays green.

Wiring them up immediately paid for itself: the parity gate found that the A6
SWD→JTAG cutover left **`jtag_bb`** — the live debug block on the fielded shell,
shipped at `C_S_AXI_ADDR_WIDTH=32` — unregistered, so the gate had been checking
`swd_bb`, which the BD no longer instantiates. One dated waiver now tracks the
missing bench (`param_parity_waivers.txt`).

### What `check_fielded_shell_claims.py` now covers

`docs/FIELDED_SHELL.md` is the single authority for what is on the board, and the
gate holds the tree to it. It gained two rows on 2026-09-09/10, because a
`static_id` identifies only the half of the fielded image that has a CRC:

- **`fielded_fw_flags`** — the `make` flags the fielded *firmware* was built with.
  A half-mint (fabric with the AXI IIC, firmware without the touch driver) passed
  every gate in the repo, because no gate knew what the firmware should contain.
  Validated for shape (≥3 `NAME=VALUE` tokens; `unknown`/`TBD` refused), not for
  content — pinning the flag set would fail the next legitimate mint for being
  *different* rather than *wrong*.
- **`lmb_kb`** — the MicroBlaze LMB size (1024). Validated as a positive **power of
  two**: the decode is a bit slice, so 768 KiB cannot be built. This one is not
  bookkeeping — the JTAG diag mailbox is anchored at `lmb_kb*1024 - 0x80`, and the
  LMB decode **aliases**, so a stale size reads a plausible wrong word instead of
  erroring. That is bug #4.

The claim patterns were also widened from "shipped"/"fielded" phrasing to cover
`current`-adjacent prose, CLCD `SID : 0x…` panel mocks, and present-tense LMB/BRAM
**size** claims. They run on `.md`/`.txt` only — this gate exists because "a
sentence in a README has no gate"; code that pins an id has its own gates — and
history (`then-fielded`, `landed as`, `superseded`) is never rewritten.

`tests/integration/test_ci_gate_mirrors_check.py` also now asserts that **every**
`scripts/harness_gates/*.py` and `*.tcl` is invoked by `check-ci`, `check`, or
`scripts/harness_regression.sh`, or sits in an explicit allowlist with a reason.
That is the general form of the bug: the six were orphaned and nothing said so.

Environment (see `.github/ci-requirements.txt`):

- **Python 3.8** -- matches the deployment box. `pyverify`/`socket_harness`/
  `webharness` are imported via `PYTHONPATH`, not pip-installed, so their
  `requires-python >= 3.10` metadata never blocks the 3.8 box that actually runs
  the gate. `webharness` (the web dashboard) contributes pure-routing and
  backend-seam tests plus the drift guards that parse `firmware/clcd/clcd.{h,c}`
  and check the service/RM tables agree over all 65536 design ids -- board-free,
  the only sockets being a loopback server against an in-process fake backend.
- **pytest, pydantic** -- test-time deps.
- **cocotb 1.7.2, import-only.** `tests/common/regmap.py` imports cocotb at top
  level, so two *pure-logic* tests (`test_regmap`, `test_swap_sequence`) fail
  *collection* without it. No simulator is installed or needed -- the
  `@cocotb.test()` benches are excluded by `tests/conftest.py` and run only on
  the full gate.
- **tcl (tclsh)** -- `pin-check` cross-checks `rm_list.tcl` with `tclsh`.
- **build-essential** -- gcc/make for the firmware, stage0, and daemon harnesses.

Cross-repo tests (e.g. the QSPI regmap-parity gate, which reads
`$SOCLABS_AHB_QSPI_DIR`) **skip** cleanly when the lab sources are absent -- they
never hard-fail a hosted runner.

### Which branches it runs on

`push` is `branches: ["**"]` — **every** branch. Until 2026-09-09 it was
`[master, "feat/**"]`, so a `chore/`, `fix/`, `docs/` or `wip/` branch got no
board-free gate at all; `chore/public-release-tidy` accumulated weeks of work
without the gate running on it once. `pull_request` alone does not close that: the
PR event fires only once a PR exists, which is late, and never for a direct push.

The `concurrency` block (`group: ci-${{ github.ref }}`, `cancel-in-progress: true`)
keeps the cost flat — a newer push on the same ref cancels the older run.

### `verilator-lint` -> `make lint`

RTL lint is a **separate job on purpose**. The dev box is verilator 4.028;
`ubuntu-22.04` apt ships 4.038 (same 4.x lint engine). Isolating lint means a
verilator-version difference surfaces as a *lint-job* signal, never as noise on
the logic gate. If version drift ever produces a false lint failure, pin
verilator in that job (build 4.028 from source + `actions/cache`).

## The full gate (`ci-full.yml`) -- self-hosted, dormant

`make check SIM=vcs` is the *whole* gate: the board-free stages **plus** the
cocotb benches under VCS (stage 7) and the overlay CRC round-trip (stage 8). It
needs a simulator and the SoC source repos, so it targets a self-hosted lab
runner and is **manual-trigger only**. With no matching runner registered it
simply never runs -- it cannot go red on a hosted runner.

### Enabling the self-hosted full gate

1. On a lab box that has `source set_env.sh` working (VCS + the SoC source repos
   on `PATH`), install a GitHub Actions self-hosted runner for the repo.
2. Give the runner the labels `self-hosted` and `mps3-lab`.
3. Trigger **full gate (self-hosted)** from the Actions tab.

### On-board smoke (Phase A, opt-in `run_board`)

The on-board step is wired (no longer a stub). It runs the **board-safe** Tier-3
subset of `scripts/harness_regression.sh` -- CSR liveness + diag mailbox +
control-plane ping -- and **deliberately excludes** the 6910-push swap / console /
SWD tiers (`MPS3_RUN_SWAP/CONSOLE/SWD=0`): the **D16 flash-collision embargo**
forbids any commit / push / flash-writing swap on the shared board. Those tiers
are a later phase (and must run from a workstation over a tunnel, never on-hub).

**Board-unavailable is a SKIP, not a failure** -- a busy/dark board is not a code
regression, so the step exits 0 with a `::warning::`; only a real assertion (dead
CSR, ping mismatch) fails it. The flow is: bounded lease acquire (busy -> SKIP) ->
ensure-a-live-shell (ping; one JTAG recover; still dark -> SKIP) -> board-safe
smoke -> release (tolerating the expected HTTP 403).

Provision the host wiring on the runner **as environment, never committed**
(public repo): `MPS3_HUB` (the fpgahub host reaching the board), `MPS3_BOARD_VIA_HUB=1`
when the runner is a **dev box** like `<dev-box>` (relays dataplane/JTAG via
`ssh $MPS3_HUB`; leave 0/unset if the runner *is* the board hub), and
`MPS3_BOARD_HOST` (or the repo variable) for the board IP.

## Reproducing CI locally

```sh
make check-ci      # exactly the hosted logic-gate job (stages 1-5)
make lint          # exactly the hosted verilator-lint job (stage 6)
make check         # the local superset: 1-6 here, 7-8 skip without SIM/overlays
source set_env.sh && make check SIM=vcs   # the full gate (needs VCS)
```

`make check` is deliberately **not** refactored to call `check-ci` (it is the
repo's most important target and cannot be exercised end to end without VCS, so
it is left untouched). The two therefore duplicate stages 1-5, and that
duplication is guarded against drift by
`tests/integration/test_ci_gate_mirrors_check.py`: if a board-free gate is added
to `make check` but forgotten in `check-ci` -- which would make hosted CI quietly
weaker than a local `make check` -- that test fails.

## What CI does NOT cover

- **On-board** anything (programming, swap, boot). Board work is leased,
  hub-local, and human-supervised -- see the board-run notes in the platform docs.
- **cocotb benches / overlay round-trip / Vivado builds** on hosted runners --
  these are the full gate's job.
- `mps3_dfx.ko` ICAP host tests and the QEMU swaptest are not yet in `make
  check` (manual `make -C .../icap host-tests`); folding them in is a follow-up.
