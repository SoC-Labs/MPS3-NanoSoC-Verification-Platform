# Reference

The engineering source of truth lives in the repository's own documents. This
section is a **guided index** to them, grouped by what you're looking for. The
concept and guide pages explain *ideas*; these pages point you at the *specs*.

!!! note "These pages link out to the repo"
    To avoid two copies drifting apart, the Reference pages **link** to the
    real documents in the repo's `docs/` tree rather than reproducing them. The
    links resolve when you browse the repository (e.g. on GitHub). See
    `DOCS_SITE_GAPS.md` for how a maintainer can make them render inline in a
    built site.

## Start here

- **[Interface contracts](contracts.md)** — the frozen boundaries that let many
  people build against the same shell: partition pins, register map, wire
  protocol, overlay schema. Change these only via the integrator.
- **[Architecture & status](architecture.md)** — points at
  `docs/ARCHITECTURE.md`, the one entry document, plus the DFX flow and the
  living, evidence-badged status.
- **[CI & testing](ci-and-testing.md)** — how the repo is gated and where the
  tests live.
- **[Archive index](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHIVE_INDEX.md)** — the repository
  keeps its design history, and many of those documents were true when written
  and never rewritten. This index says, for each one, what it recorded, when,
  and what supersedes it. **Check it before trusting a present-tense claim in a
  repo document**; anything listed there is history, not current state.

## The repository README map (quick links)

| Area | README |
|---|---|
| Project overview | [`README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/README.md) |
| Host / Python "brain" | [`host/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/host/README.md) |
| Shell firmware | [`firmware/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/firmware/README.md) |
| FPGA static shell | [`fpga/shell/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/shell/README.md) |
| DFX flow + overlays | [`fpga/dfx/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/dfx/README.md) |
| Ethernet MAC verification | [`fpga/ethernet/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/ethernet/README.md) |
| Tests | [`tests/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/tests/README.md) |
| nanoSoC integration notes | [`docs/nanosoc_m0_soc/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/nanosoc_m0_soc/README.md) |
