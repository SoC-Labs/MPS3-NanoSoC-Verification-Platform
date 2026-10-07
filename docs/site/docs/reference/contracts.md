# Interface contracts

The platform is built by many parallel workstreams (shell RTL, DFX flow,
firmware, host Python, verification) against a small set of **frozen
interfaces**. These contracts are what let a DUT built by one person load into a
shell built by another. They are version-controlled, and `make check` stage 1
fails if any is missing.

!!! warning "Change these only via the integrator"
    A boundary change ripples across every workstream. Read the contract, and the
    process notes in each, before proposing an edit.

## The contracts

| Contract | What it fixes | Document |
|---|---|---|
| **Partition pins** | The ~30-signal RP ⇄ shell boundary (clocks, resets, SWD, RMII+MDIO, UART/SWO, `rm_id`, GPIO) | [`docs/contracts/partition-pins.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/partition-pins.md) |
| **Partition timing** | The socketed-XDC clock timing contract for DFX RMs | [`docs/contracts/partition-timing.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/partition-timing.md) |
| **Shell register map** | The MicroBlaze AXI4-Lite view — every control/status register block | [`docs/contracts/shell-regmap.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/shell-regmap.md) |
| **Net protocol** | The host ⇄ shell wire protocol: control JSON, bitstream push framing, ports | [`docs/contracts/net-protocol.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/net-protocol.md) |
| **Overlay manifest** | The overlay artefact-set schema + the A/B slot store | [`docs/contracts/overlay-manifest.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/overlay-manifest.md) |
| **DUT display tunnel** | RP → shell display path over spare `dut_gpio` bits (the CLCD KVM feature) | [`docs/contracts/dut-display-tunnel.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/dut-display-tunnel.md) |

## Open issues / integrator punch-list

The questions the frozen contracts could not answer, with dispositions and
owners, are tracked in one place:

- [`docs/contracts/OPEN_ISSUES.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/OPEN_ISSUES.md)

Most historical items are resolved; the file records *how* they were resolved
(and which few genuinely need a board to close), which is valuable context before
you touch a boundary.

## How the contracts connect to the concepts

- The **partition pins** contract *is* the shell↔DUT boundary from
  [The shell & the RP](../concepts/shell-and-partition.md).
- The **net protocol** contract is the wire behind
  [Over-the-wire reconfiguration](../concepts/over-the-wire-reconfiguration.md)
  and the debug tunnels in [Debugging the DUT](../concepts/debugging-the-dut.md).
- The **overlay manifest** contract defines the `static_id` / `rm_id` / checksum
  fields that make swaps verifiable, from
  [Reconfigurable Modules & the DUT](../concepts/reconfigurable-modules-and-dut.md).
