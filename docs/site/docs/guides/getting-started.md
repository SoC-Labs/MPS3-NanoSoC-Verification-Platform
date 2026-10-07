# Getting started & building

A thin orientation guide. It points you at the authoritative recipes rather than
duplicating them — the source of truth is the repository's own `README`s and
`Makefile`.

!!! tip "New to the platform?"
    Read the [Concepts](../index.md#where-to-go-next) pages first — 10 minutes,
    and the rest of the repo will make sense.

## What you can do without any hardware

Most of this repo is exercisable with **no board and no FPGA tools** — just
Python, a C compiler, and `make`. That's the whole point of the board-free gate:

```sh
make check          # the one-command repo health gate (board-free stages)
```

See [The board-free gate](board-free-gate.md) for what each stage checks and how
to reproduce CI locally.

The Python host library installs stdlib-only at runtime:

```sh
pip install -e host/pyverify        # the "brain" library used to drive the board
cd host/pyverify && python3 -m pytest -q   # 363 tests, sockets on localhost only
```

That is one of four Python suites. The full board-free gate (`make check-ci`) runs
the lot — **780+ pytest tests** across `tests/`, `host/pyverify`,
`host/socket_harness` and `host/webharness` — plus **38 firmware test binaries**
compiled and run on the host compiler.

## The three build domains

The repo has three independently-buildable layers. You only need the tools for
the layer you're touching.

| Layer | What it produces | Tool needed | Recipe lives in |
|---|---|---|---|
| **Host (Python)** | the `pyverify` library + tender plugin | Python 3 | `host/README.md` |
| **Firmware (C)** | the MicroBlaze shell coordinator ELF | Vitis 2024.1 (target) / gcc (host tests) | `firmware/README.md`, `firmware/platform/README.md` |
| **FPGA (RTL/bitstreams)** | the static shell + RM overlays | Vivado 2024.1 + lab IP | `fpga/shell/README.md`, `fpga/dfx/README.md` |

Pointers to the actual build commands (do not memorise — read the area README
before running):

```sh
# Static shell bitstream (Vivado)
vivado -mode batch -source fpga/shell/build_shell.tcl -tclargs ./build/shell_proj

# DFX build: all RM partials + clearings, then overlay triples + round-trip check
make -C fpga/dfx prod
make -C fpga/dfx overlays
make -C fpga/dfx verify

# Shell firmware ELF (see firmware/platform/README.md for the xsct platform step first)
make -C firmware/platform elf
```

!!! note "Build from a clean clone: what you get, and what you don't"
    With **only Vivado + Vitis and this repository**, you can build the whole
    platform end to end: the **static shell**, the **five vendored RMs**
    (`greybox`, `led`, `regdemo_a`, `regdemo_b`, `uart_echo`), the **shell
    firmware**, and a **bootable harness**. That is a complete, working board.

    The remaining five RMs each need something this repository deliberately does
    not carry:

    | RM | Also needs |
    |---|---|
    | `nanosoc`, `nanosoc_multicore`, `nanosoc_upy` | Arm SoC-400 confidential IP **and** the sibling nanoSoC checkouts |
    | `eth_ss` | `ETH_SS_HOME` plus the lab IP it references |
    | `socscope` | `SOCSCOPE_HOME` (the sibling SoCScope checkout) |

    **On a clean clone you must pass `RM_SUBSET` explicitly:**

    ```sh
    make -C fpga/dfx prod RM_SUBSET="rm_greybox rm_regdemo_a rm_regdemo_b rm_led rm_uart_echo"
    ```

    Without it, the default RM-readiness filter in `fpga/dfx/build_dfx.tcl` admits
    `rm_socscope` — it lives under `fpga/dfx/rms/`, so the filter treats it as a
    single-file source it can synthesize inline — but its real sources come from
    `SOCSCOPE_HOME`. The build then "succeeds" with SoCScope left as a **black
    box**, which is worse than failing. Name the subset and the question does not
    arise.

!!! warning "The golden rule of rebuilding the shell"
    A **shell rebuild re-mints its `static_id`** (its fabric fingerprint). Every
    RM overlay and the firmware carry that id, and the pusher refuses a mismatch.
    So rebuilding the shell requires a **full re-key** (`make -C fpga/dfx
    overlays`) **+ firmware rebuild + board reflash**. Do not push old overlays
    onto a freshly rebuilt shell. This is the single most common way to waste an
    afternoon.

## Environment

The simulator/FPGA flows need the lab environment sourced (VCS + the SoC source
repositories on `PATH`); the board-free gate does not:

```sh
source set_env.sh          # required for `make check SIM=vcs` and Vivado builds
```

## Read next

- [The board-free gate (`make check`)](board-free-gate.md) — validate your
  checkout with no hardware.
- [Deploying an RM over the wire](deploying-an-rm.md) — the fast edit → load →
  test loop against a live board.
- [Board bring-up](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/BOARD_BRINGUP.md) — the dark-board → live-DUT
  runbook, including the lease and network gotchas, and
  [Troubleshooting](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/TROUBLESHOOTING.md) when something misbehaves.

## Authoritative references

- Top-level [`README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/README.md) — the project overview and
  repository layout.
- [`host/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/host/README.md) · [`firmware/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/firmware/README.md)
  — the two software layers in depth.
- The [architecture spec](../reference/architecture.md) — the full design.
