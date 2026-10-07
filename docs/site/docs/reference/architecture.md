# Architecture & status

## The one entry document

If you read one repository document after the concept pages, read this one:

- **[`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)**
  — the shell and the reconfigurable partition, the frozen 35-port boundary, DFX
  and `static_id`, the 6900 protocol and the firmware superloop, the host tools,
  the board, and the gates. Every fact in it names the file that is the authority
  for it.

The [Concepts](../index.md#where-to-go-next) pages on this site are the
plain-language on-ramp to it; `ARCHITECTURE.md` is where the same mechanisms are
stated precisely, with citations. Where the two ever differ, `ARCHITECTURE.md`
is canonical.

!!! note "Looking for `docs/ARCHITECTURE_SPEC.md`?"
    That is the **v2.1 design spec from July 2026** — what was *planned*. Parts
    of it were built differently and parts were not built at all, so it now
    carries a `Status: HISTORICAL` banner and a row in the
    [archive index](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHIVE_INDEX.md).
    Read it for rationale — it is where many design decisions were first recorded
    and it is cited from RTL headers — never for current state.

## DFX background

Why partial reconfiguration, how the flow is driven here, and the boundary
hazards that get checked:

- [`fpga/dfx/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/dfx/README.md) — the DFX build
  flow as it actually runs on this board: the RM library, the reference config,
  re-keying, and the gates that catch a mismatched overlay.
- [Adding an RM](../guides/adding-an-rm.md) — the practical path through it, from
  the copyable skeleton to a deployed overlay.

!!! note "Looking for `docs/DFX_BACKGROUND.md`?"
    It is still in the repo, but it records a **Zynq-7 / PYNQ-Z2** feasibility
    probe carried out in a different repository before this platform's KU115
    flow existed. It is useful as rationale and misleading as documentation of
    this board, so it is indexed as historical — see the
    [archive index](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHIVE_INDEX.md).

## Living status — what is actually proven on silicon

The platform tracks maturity **honestly, badge by badge** — hardware-proven
claims require silicon evidence, and status documents are treated as *less*
authoritative than the git history:

- [`docs/STATUS.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/STATUS.md) — the maturity matrix (proven on
  silicon vs in simulation vs designed) and the known gaps.
- [`docs/FIELDED_SHELL.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/FIELDED_SHELL.md) — the **single
  authority** for which shell the board is actually running, and what firmware
  was built beside it. Nothing else in the tree may restate it; a gate enforces
  that. Note that *fielded* and *minted* are different questions with routinely
  different answers.

!!! tip "How to read status here"
    When two sources disagree, the project's convention is: **git commits >
    build reports > status docs**. A "green tree" is not the same as "HEAD
    builds". Prefer evidence (a commit, a report) over a headline.

## Repository layout

The top-level [`README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/README.md) has the authoritative
repository map, and
[`ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)
§8 explains why the tree is shaped this way. In brief:

| Path | Contents |
|---|---|
| `fpga/shell/` | The static shell: block design + custom AXI IP |
| `fpga/rp/` | RM (DUT) partition-pin wrappers — and `_template/`, the copyable skeleton |
| `fpga/dfx/` | The DFX flow: floorplan, N-config build, RM library, overlays |
| `fpga/ethernet/` | MAC-in-operation verification blocks (virtual PHY, gen/checker) |
| `fpga/monolithic/` | Non-DFX whole-FPGA nanosoc baseline |
| `firmware/` | MicroBlaze bare-metal C (coordinator, config agent, servers) |
| `host/` | The Python side: `pyverify`, tender plugin, console/OpenOCD tooling |
| `scripts/` | CI gates, versioning, board tooling |
| `tests/` | cocotb benches per block + pure-Python integration tests |
| `docs/` | `ARCHITECTURE.md`, status, runbooks, and `docs/contracts/` (frozen interfaces) |
| `poc/` | Proofs-of-concept (SystemRDL register generation, DDR4 + MicroBlaze V) |

## Versioning

Two orthogonal axes — a **compatibility** fingerprint (`static_id`) and a
**release** semver (in the root `VERSION` file, stamped into firmware and the
bitstream) with a per-DUT `rm_id` encoding `{major, minor, design_id}`:

- [`docs/VERSIONING_PLAN.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/VERSIONING_PLAN.md) — the two-axis scheme
  and how `make check` gates it.
