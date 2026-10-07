# CI & testing

## The gate

The repository's health is enforced by `make check`, split into a board-free
half (that CI runs on every push and PR) and a full self-hosted half. The
newcomer-friendly walk-through is [The board-free gate](../guides/board-free-gate.md);
the authoritative description is:

- **[`docs/CI.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/CI.md)** — the two gates, the exact pinned
  environment, and the drift guard that keeps hosted CI from quietly diverging
  from a local `make check`.

## Where the tests live

| Suite | Location | Runs in |
|---|---|---|
| Cross-workstream integration (pure Python) | `tests/` | `make check` stage 3 |
| cocotb RTL benches (per block) | `tests/<block>/` | `make check` stage 7 (needs a simulator) |
| Host library unit tests | `host/pyverify/tests/` | `make check` stage 4 |
| Socket / web harness tests | `host/socket_harness/tests/`, `host/webharness/tests/` | `make check` stage 4 |
| Firmware host-gcc harness | `firmware/test/` | `make check` stage 5 |

The `tests/` tree has its own [`tests/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/tests/README.md)
describing the bench inventory and the simulator target.

## Testing philosophy worth knowing

Two conventions explain a lot of the test design:

- **Loud skips, never silent ones.** A stage that cannot run (no simulator, no
  built overlays) is *named* at the end of `make check`. A green gate that hid an
  empty one is a failure mode this repo has specifically been bitten by.
- **Test the logic where it's cheap.** The firmware's coordinator/swap/codec
  logic compiles and runs **natively on the host** against fakes for the network
  and hardware (stage 5), so most correctness is proven with no MicroBlaze and no
  board. Only genuinely hardware-dependent behaviour is deferred to the board
  tier.
- **Mechanical agreement gates.** The `rm_id` gate (stage 2) asserts three-way
  agreement between the RTL wrapper, the RM library list, and the overlay
  manifest — turning "wrong design loaded" into a named CI failure rather than a
  silent on-hardware fault.

## On-board testing

Nothing on-board runs in `make check` — board work is leased, hub-local, and
human-supervised. The on-board smoke tier and its runbook are in
[Board bring-up](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/BOARD_BRINGUP.md) and
[Troubleshooting](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/TROUBLESHOOTING.md).
