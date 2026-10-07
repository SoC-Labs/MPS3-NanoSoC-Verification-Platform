# MPS3 nanoSoC Verification Platform

**A networked verification and development platform for Arm Cortex-M SoCs on the
Arm MPS3 board (Xilinx Kintex UltraScale XCKU115).**

Think of it as a **disaggregated PYNQ**: a persistent **static shell** (network
stack, configuration engine, debug plumbing, status display) stays resident on
the FPGA, while the SoC under test lives in a **DFX reconfigurable partition**
that is swapped at runtime **over Ethernet** — in seconds, with no reboot and no
cables. The Python "brain" runs on an external tender (a Raspberry Pi 5 or a
shared server) and drives the board through a small JSON control protocol and
the `pyverify` library.

Deploying a new SoC to live hardware looks like this:

```python
from pyverify import Mps3Board

with Mps3Board("192.168.10.101") as board:
    result = board.deploy("nanosoc")               # push overlay + swap the partition
    result.reattach.apply()                        # reopen SWD / console / ILA
    board.uart0.assert_contains(b"nanosoc boot")   # verdict comes from the console
```

Over-the-wire reconfiguration is **proven on silicon**: a Python host pushed a
1.31 MB partial bitstream over Ethernet only (no JTAG), the partition
reconfigured through the shell's ICAP at ~570 KB/s, the loaded design was
verified against its live hardware ID, and the network survived the swap.

This repository is board-support + build flow + firmware + host harness. It
consumes SoC IP and firmware **read-only** from sibling checkouts (nanosoc,
ethernet-subsystem, Arm Academic Access IP) via environment variables — it never
vendors or modifies DUT sources.

## Documentation

Start with the **Getting Started guide**: it takes you from an unboxed board to
your own program running on the nanoSoC.

| Guide | For |
|---|---|
| [`docs/MPS3_Getting_Started.pdf`](docs/MPS3_Getting_Started.pdf) | First setup, loading designs, your first program |
| [`docs/MPS3_User_Guide.pdf`](docs/MPS3_User_Guide.pdf) | Day-to-day use: designs, consoles, debug, updates |
| [`docs/MPS3_Technical_Guide.pdf`](docs/MPS3_Technical_Guide.pdf) | Technical reference and maintainers' guide |

The same PDFs are attached to each [release](../../releases). The worked example
from the Getting Started guide is in [`examples/nanosoc_hello/`](examples/nanosoc_hello/).

---

## How it works

```
   tender (Pi 5 / server)                MPS3 board (XCKU115)
  ┌─────────────────────┐             ┌──────────────────────────────────────┐
  │  pyverify / Jupyter │   Ethernet  │  STATIC SHELL (always resident)      │
  │  OpenOCD / xvcd     ├─────────────┤   MicroBlaze V: Linux + harnessd     │
  │  overlay store      │             │   config agent → HWICAP → ICAPE3     │
  └─────────────────────┘             │   DFX decoupler + AXI shutdown mgr   │
        TCP 6900  control (JSON)      │   XVC + JTAG debug servers, SSH      │
        UDP 69 / TCP 6910  bitstreams │   UART-over-Ethernet, telemetry      │
        TCP 2542  XVC (ILA)           │   CLCD status panel driver           │
        TCP 6921  JTAG (OpenOCD)      │  ┌────────────────────────────────┐  │
        TCP 6930-32  UART0/1/SWO      │  │ RECONFIGURABLE PARTITION       │  │
                                      │  │ one DUT SoC at a time,         │  │
                                      │  │ swapped live over the network  │  │
                                      │  └────────────────────────────────┘  │
                                      └──────────────────────────────────────┘
```

The shell and the DUT meet at a deliberately narrow, **frozen boundary** — 35
slow scalar signals (clocks/resets, JTAG, RMII + MDIO, UART/SWO streams, a
32-bit hardware `rm_id`, GPIO passthrough, QSPI flash), no shared AXI. Everything the host
relies on (network, configuration, debug) lives entirely in static logic, so a
broken or hung DUT can always be swapped out from the outside.

**The shell CPU runs Linux.** Since 2026-10-01 the static shell's CPU is a MicroBlaze V
running Linux (static `0x44EE76D5`, [docs/FIELDED_SHELL.md](docs/FIELDED_SHELL.md)), and the shell
services are the same C modules as before, run as one process, `mps3-harnessd`. The wire is
unchanged: every client sees the same ports and verbs, plus `version.impl == "linux"`. You can
SSH into the board, a watchdog restarts a hung harness, and stage0 boots the image from the user
µSD with an A/B fallback and a network rescue. How to get a shell, a console and a board back:
[docs/LINUX_HARNESS.md](docs/LINUX_HARNESS.md). The bare-metal MicroBlaze image (`0x72BB0A36`) is
the rollback, one SD write away ([docs/planning/ROLLBACK_RUNBOOK_LINUX.md](docs/planning/ROLLBACK_RUNBOOK_LINUX.md)).

A swap is sequenced by the shell's coordinator firmware: gate debug/console
traffic, decouple the partition, stream the *clearing* bitstream of the
currently-loaded design (mandatory on UltraScale), stream the new partial,
verify the design's live hardware ID against the pushed image's header, then
release the partition. The whole state machine is non-blocking so the
multi-second ICAP write never starves the network stack.

Four frozen interface contracts in [docs/contracts/](docs/contracts/) coordinate
all parallel work: `partition-pins.md` (RP ⇄ shell boundary),
`shell-regmap.md` (register map), `net-protocol.md` (host ⇄ shell wire
protocol), `overlay-manifest.md` (overlay artefact schema). Change these only
via the integrator.

## Repository layout

```
fpga/shell/       static shell: BD + custom AXI IP (clkrst, dfx_ctl, board_gpio,
                  swd_bb, telem, uart_bridge, clcd), LAN9220 I/F, HWICAP, decoupler
fpga/rp/          reconfigurable-module (DUT) wrappers: nanosoc, eth_ss,
                  nanosoc_multicore — partition-pin wrappers around real SoCs
fpga/dfx/         DFX flow: floorplan, N-config build (build_dfx.tcl), RM library,
                  manifest generation, pr_verify proofs, overlay/ artefacts
fpga/ethernet/    MAC-in-operation verification blocks (virtual PHY, RMII, MDIO
                  model, link-partner MAC, error-injecting generator/checker)
fpga/monolithic/  non-DFX whole-FPGA nanosoc baseline build
firmware/         the shell services in C (coordinator, config agent, overlay store,
                  JTAG/XVC servers, CLCD): one codebase, run as mps3-harnessd on
                  Linux (src/linux_harness/) and bare metal on the rollback image
src/              the Linux harness image and daemons (src/linux_harness/) and stage0,
                  the MicroBlaze V boot loader (src/linux_soc/hw/fw_stage0/)
host/             the Python side: pyverify library, fpgahub tender plugin,
                  console/OpenOCD tooling, notebooks
scripts/          CI gates (scripts/harness_gates/), versioning, board tooling
tests/            cocotb benches per block + pure-Python integration tests
docs/             specs, plans, live status, and docs/contracts/ (frozen interfaces)
poc/              proofs-of-concept (SystemRDL register generation, DDR4)
```

## The on-board status display

The MPS3's own QVGA LCD (320×240, Himax HX8347-D) is driven by the shell as a
live status panel, so the board answers "what is loaded, is it healthy, can I
reach it?" **without a laptop attached**:

```
┌────────────────────────────────────────┐
│ MPS3-01             nanoSoC harness    │
│ ---------------------------------------│
│ DUT : nanosoc_multicore   v1.0         │
│ SWAP: LOADED VERIFIED    #012  last OK │
│ SID : 0x44EE76D5                       │
│ NET : 192.168.10.101  UP 100/FD        │
│ UP  : 000:02:41:07                     │
│ DUT : RST-REL  CLK-ALIVE  MMCM-LOCK    │
│ ICAP: 1312792 B  rxdrop 0 txerr 0      │
│ CFG : 2 cores  ethernet  serial        │
│              (error banner rows)       │
│ MAC 02:xx:xx:xx:xx:xx           hb /   │
└────────────────────────────────────────┘
```

Design points worth knowing:

- **The glass never lies.** The resident-design row is read live from the
  partition's hardware ID register, never from a firmware cache — a design
  loaded behind the firmware's back (e.g. via JTAG) is still named correctly,
  and an unreadable partition shows `RP DECOUPLED` / `RM ID NOT VALID` rather
  than a stale name.
- **It can never hurt the network.** The driver is a cooperative, never-blocking
  renderer (bounded bytes per superloop pass, FIFO-backpressured, overflow
  drops pixels rather than stalling the bus). The panel is cosmetic; Ethernet
  is the board's only ingress.
- **Every init byte is cited.** [firmware/clcd/PANEL_PROVENANCE.md](firmware/clcd/PANEL_PROVENANCE.md)
  traces the HX8347-D init sequence to vendored upstream sources
  (BSD-3/MIT) plus the datasheet — no register value was written from memory.
- **The layout is regression-tested off-target.**
  [clcd_preview](firmware/clcd/tools/clcd_preview.c) renders the real
  formatter against 14 seeded scenarios on the host, and the test suite sweeps
  all 65,536 design IDs to prove no name can ever overflow its field.

The RTL block is a small AXI4-Lite → 8080-bus byte streamer
([fpga/shell/ip/clcd/](fpga/shell/ip/clcd/)); the panel's own GRAM is the
framebuffer. Firmware support is gated behind `MPS3_HAS_CLCD` (build with
`CLCD=1`), which must be flipped in lockstep with a CLCD-bearing shell
bitstream.

## Available DUT overlays

Each overlay is a `{clearing, partial, manifest}` triple built against one
locked static shell, keyed by that shell's `static_id` so an incompatible
partial can never be loaded.

| Overlay | What it proves |
|---|---|
| `greybox` | Inert tie-off default and DFX reference config; the safety fallback |
| `led` | Human-visible proof of the reconfiguration pipeline (GPIO blinker) |
| `uart_echo` | First data-moving DUT: banner + byte echo across the boundary |
| `regdemo_a` / `regdemo_b` | Register-difference pair on a bit-identical static |
| `nanosoc` | The real single-core Cortex-M0 nanoSoC |
| `eth_ss` | Standalone AHB Ethernet MAC + PTP timestamping subsystem |
| `nanosoc_multicore` | Dual-core Ethernet nanoSoC (network core + chip core, MAC, PTP, IPC) — the richest DUT |

## Versioning

Two orthogonal axes (see [docs/VERSIONING_PLAN.md](docs/VERSIONING_PLAN.md)):

- **Compatibility** — `static_id`, a fingerprint of the locked static fabric.
  Answers "will this partial physically fit this shell?"
- **Release** — the harness carries a semver in the root `VERSION` file,
  stamped into firmware and the bitstream's `USR_ACCESS` register; each DUT's
  32-bit hardware `rm_id` is encoded as `{major, minor, design_id}`, so the
  design version is readable from a live register while the design's identity
  survives re-versioning.

`rm_id` values are derived from `(design_id, version)` in one place and
mechanically gated: `make check` asserts three-way agreement between RTL
wrapper, RM library, and overlay manifest, and that every hard-coded consumer
matches the authority — a version bump fails in CI naming the stale line, not
on hardware.

## Building and testing

```sh
make check                          # the one-command repo gate (8 stages, board-free)
source set_env.sh && make check SIM=vcs    # + the cocotb bench stage

make -C fpga/dfx prod               # N-config DFX build of all RMs (Vivado 2024.1)
make -C fpga/dfx overlays           # emit overlay/<rm>/ triples + manifests
make -C fpga/dfx verify             # round-trip every manifest

pip install -e host/pyverify        # the host library (stdlib-only at runtime)
python -m pyverify.cli deploy --host 192.168.10.101 --overlay overlay/nanosoc
```

`make check` runs: contract presence → RM boundary + rm_id gates →
cross-workstream pytest → pyverify pytest → firmware host-gcc harness (the
firmware logic compiles and runs natively) → Verilator lint → cocotb benches
(when a simulator is available) → overlay round-trip. Everything that cannot
run is named, never silently skipped.

Bitstream payloads are deliberately not committed (large, regenerated);
manifests and build reports are committed as evidence.

## Where this is going

Current maturity is tracked honestly, badge by badge, in
[docs/STATUS.md](docs/STATUS.md) — hardware-proven claims require silicon
evidence. Highlights already proven on the board:
the Linux harness on a MicroBlaze V (the fielded shell since 2026-10-01),
shell-on-network (Linux, and lwIP over the LAN9220 on the bare-metal rollback image),
JTAG-verified partial reconfiguration,
full over-the-wire deploy of a working DUT (`uart_echo` echoing over the
swapped partition), and root-cause fixes committed back at every layer.

That matrix also says what does **not** work, which is the harder half. It has
badges for *intermittent*, *broken* and *not built*, and one for the shape three
of this platform's failures currently have: **diagnosed** — the cause is proven,
the fix is identified and written down, and nothing has changed on the board yet.
The shell's own serial console, the standalone ethernet-subsystem image's Cortex-M0 (the `rm_eth_ss` overlay has no CPU) and the
touchscreen are all in that state as of 2026-09-10, each with its root cause
committed.

Near term is tracked where it cannot go stale: **[docs/STATUS.md](docs/STATUS.md)**
is the single maturity matrix; every silicon claim there names the commit, test or
dated log that proved it, and `scripts/harness_gates/check_status_citations.py`
(stage 2 of `make check` and `make check-ci`) refuses a citation that does not
resolve, and a silicon claim backed only by a plan document. (This list used to
duplicate the matrix, and duplicated it wrongly — it was still asking for three
things that had already shipped.) The two worth naming here, because they are
*capability* gaps rather than tasks:

- **Prove the DFX decoupler's per-signal clamps on silicon** (authored and
  built; clamp values not yet exercised on the board).
- **Bring up the QSPI overlay store** (A/B default-DUT slots + clearing cache)
  so the board self-boots its DUT with no host attached.

Medium term:

- **DUT Ethernet egress** — terminate the DUT's RMII in fabric and bridge it
  onto the shared board MAC ("one port, two MACs"), so a loaded SoC gets real
  network reachability. DUT *reception* is already proven on silicon; the
  DUT→host return path is the half that does not exist yet.
- **A fleet stats service and dashboard** —
  provenance-tagged board state across the whole fpgahub fleet (MPS3, PYNQ-Z2,
  ZC702, KR260), with per-board history and alerting.
- **A "DUT SDK"** — package the locked static, timing context, wrapper
  template, and conformance gates so external SoC designers can build an
  overlay for the platform without cloning the whole flow.

Longer term:

- **The cold-start fix in fabric** — the Linux harness's stage0 works around the
  MCC re-programming clocks while DDR4 calibrates; mint 4 gates the MIG's reset on
  the MCC's `CB_nRST` so a cold start needs no workaround and no PB0.
- **Faster reconfiguration paths** (DDR4-staged bitstreams into ICAP) once the
  10/100 network is no longer the bottleneck.

## References

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — **the one entry document**: what
  the platform is made of, why, and which file is the authority for each fact
- [docs/ARCHITECTURE_SPEC.md](docs/ARCHITECTURE_SPEC.md) — the v2.1 platform spec
  (**historical**: the design as agreed in July 2026, kept for rationale)
- [docs/STATUS.md](docs/STATUS.md) — the maturity matrix (silicon vs sim vs designed)
- [docs/BOARD_BRINGUP.md](docs/BOARD_BRINGUP.md) — hardware runbook: dark board → live networked DUT
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — symptom-indexed fixes for the common failure modes
- [docs/DFX_BACKGROUND.md](docs/DFX_BACKGROUND.md) — DFX background and prior proof
- [docs/contracts/](docs/contracts/) — the frozen interfaces (start here before changing a boundary)
- [host/README.md](host/README.md) and [host/notebooks/demo.md](host/notebooks/demo.md) — driving the board from Python

### Where to read next

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — start here if you are new:
  the single entry document for how the platform works, and a map to every
  authority behind it. New DUT? [docs/site/docs/guides/adding-an-rm.md](docs/site/docs/guides/adding-an-rm.md)
  takes you from the skeleton in [fpga/rp/_template/](fpga/rp/_template/) to a
  deployed overlay.
- **[docs/STATUS.md](docs/STATUS.md)** — what is proven on silicon
  vs simulation vs designed, with the evidence for each.
- **[docs/FIELDED_SHELL.md](docs/FIELDED_SHELL.md)** — the single authority for
  which static shell is on the board. Nothing else in the tree may restate it.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — the conventions that keep the gates
  honest (and why `git add -A` is banned here).
- **[docs/CI.md](docs/CI.md)** — what runs in CI, what needs a board, and the
  parity test that stops the two drifting apart.
- **[docs/site/](docs/site/)** — the MkDocs sources for the rendered doc site.

A SoC Labs ([soclabs.org](https://soclabs.org)) project, developed under Arm
Academic Access. DUT IP (nanoSoC, Ethernet subsystem, Arm CMSDK) is consumed
read-only from its source repositories.

## License

This project's own source is licensed under the [Apache License 2.0](LICENSE).

**It does not include, and does not license, third-party confidential IP.** The
Arm CoreSight SoC-400, Cortex-M0, CMSDK and vendor memory/physical IP are used
under Arm Academic Access, are referenced only via git-ignored local paths, and
are excluded from this repository — see [NOTICE](NOTICE). Obtain that IP under
your own license. Security or IP-leak reports: see [SECURITY.md](SECURITY.md).
