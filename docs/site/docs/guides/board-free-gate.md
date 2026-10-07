# The board-free gate (`make check`)

`make check` is the repository's one-command health gate. Most of it needs **no
board, no FPGA tools, and no simulator** — so you (and CI) can catch regressions
in the firmware logic, the host library, the interface contracts, and the RTL
lint before anything touches hardware.

This guide is a thin map. The authoritative description is
[`docs/CI.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/CI.md) in the repo.

## The two commands you'll actually run

```sh
make check          # the local superset: board-free stages, + lint;
                    #   simulator/overlay stages skip loudly if unavailable
make check-ci       # exactly the hosted CI logic gate (stages 1-5)
make lint           # exactly the hosted verilator-lint job
```

`make check` is deliberately **not** silent about what it skipped: if a stage
can't run (no simulator, no built overlays), it says so by name at the end. A
green gate that hid an empty one has bitten this project before — hence the
loud-skip discipline.

## What the stages check

`make check` runs up to 8 stages; `make check-ci` is stages 1–5 (the fully
board-free half):

| Stage | Checks | Board-free? |
|---|---|---|
| 1 | Interface **contracts** exist (`docs/contracts/*.md`) | yes |
| 2 | **RM boundary** conformance + **`rm_id`** three-way agreement | yes |
| 3 | Cross-workstream **pytest** (`tests/`) | yes |
| 4 | **Host pytest** (`pyverify`, socket harness, web harness) | yes |
| 5 | **Firmware** + stage0 + daemon wire-contract host-gcc harnesses | yes |
| 6 | **Verilator lint** of the RTL blocks | yes (needs verilator) |
| 7 | **cocotb benches** under a simulator | needs `SIM=vcs` + `set_env.sh` |
| 8 | **Overlay round-trip** (manifest CRC) | needs built overlays |

Stages 2 and 5 are worth knowing about as a newcomer:

- **Stage 2** mechanically enforces that a design's `rm_id` matches across the
  RTL wrapper, the RM library list, and the shipped manifest. This is what turns
  "loaded the wrong design" from a silent hardware failure into a named CI
  failure.
- **Stage 5** compiles and *runs the firmware logic natively* on the host (via
  fakes for the network and hardware), so the coordinator/swap/codec logic is
  tested with no MicroBlaze in sight.

## Reproducing CI locally

The hosted CI is two jobs — a logic gate (`make check-ci`) and a separate lint
job (`make lint`) — so a Verilator version difference shows up as a lint signal,
not noise on the logic gate. To mirror CI exactly:

```sh
make check-ci      # the hosted logic-gate job
make lint          # the hosted verilator-lint job
```

The full gate (stages 1–8, including the simulator benches) is a **self-hosted,
manual-trigger** job because it needs a licensed simulator and the SoC source
repos:

```sh
source set_env.sh && make check SIM=vcs
```

## What the gate does NOT cover

Anything **on-board** — programming, swapping, booting — is not in `make check`.
Board work is leased, hub-local, and human-supervised. For the on-board smoke
tier see [Board bring-up](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/BOARD_BRINGUP.md) and
[Deploying an RM over the wire](deploying-an-rm.md).

## Authoritative reference

- [`docs/CI.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/CI.md) — the full CI design: the two gates, the exact
  pinned environment, and the drift guard that keeps `check-ci` from quietly
  diverging from `make check`.
