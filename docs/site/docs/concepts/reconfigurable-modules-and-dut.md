# Reconfigurable Modules & the DUT

*Prerequisite: [The shell & the reconfigurable partition](shell-and-partition.md).*

## RP vs RM — the socket vs the chip you plug in

Two acronyms that are easy to confuse:

- **RP — Reconfigurable Partition.** The *region* of fabric reserved for
  swapping. It is fixed: same size, same location, same boundary signals. Think
  **the socket**.
- **RM — Reconfigurable Module.** One *design* you load into that region. Many
  different RMs can be loaded into the one RP, one at a time. Think **a chip you
  plug into the socket**.

So "swapping the DUT" means: reprogram the RP with a different RM.

## What is "the DUT"?

**DUT = Device Under Test** — the thing you are actually testing. On this
platform the DUT is whatever RM is currently loaded into the RP. Most of the time
that is **nanoSoC**.

**nanoSoC** is a small, real **Arm Cortex-M0 microcontroller SoC** from SoC Labs,
built on Arm's **CMSDK** reference components. In software terms it is a tiny
computer: a Cortex-M0 CPU core, on-chip memory, a serial port (UART), a DMA
controller, and (in the richer variants) an Ethernet MAC. It runs bare-metal
firmware or MicroPython. It is consumed **read-only** from its own source
repository — this platform hosts and tests it, it does not own or modify it.

> **Why "under test"?** The whole point of the platform is to exercise a chip
> design on real hardware: boot its firmware, poke its registers, drive its
> Ethernet, halt and inspect its CPU. The RP is the test socket; the RM is the
> silicon-equivalent design in the socket; the DUT is that design *while you are
> testing it*.

## The RM library — what you can load today

Each RM is a self-contained design that fits the frozen boundary. They range from
trivial (to prove the swap machinery works) to a full multicore SoC:

| RM | What it is / proves |
|---|---|
| `greybox` | Inert tied-off default and DFX reference; the safe fallback (see [previous page](shell-and-partition.md)) |
| `led` | Human-visible proof the reconfiguration pipeline works — a GPIO blinker |
| `uart_echo` | First data-moving DUT: prints a banner and echoes bytes across the boundary |
| `regdemo_a` / `regdemo_b` | A register-difference pair on a bit-identical shell — used to prove a swap really changed the logic |
| `nanosoc` | The real single-core Cortex-M0 nanoSoC |
| `eth_ss` | A standalone Ethernet MAC + PTP-timestamping subsystem |
| `nanosoc_multicore` | Dual-core Ethernet nanoSoC (network core + chip core, MAC, PTP, inter-core comms) — the richest DUT |

## Every RM carries an identity you can read back

A subtle but important design point: each RM bakes a **32-bit hardware ID**
(`rm_id`) into its fabric. After a swap, the shell reads that ID from a register
and confirms the *right* design actually loaded — it does not trust a filename or
a cache. This is why the board's status display can name the loaded design
correctly even if someone loaded it "behind the firmware's back" over the debug
cable.

The 32-bit ID is not arbitrary: it encodes `{major version, minor version,
design identity}`, so the *design* keeps a stable identity across version bumps
while the *version* is readable from a live register. Automated checks
(`make check`) enforce that the ID agreed upon in three places — the RTL wrapper,
the RM library list, and the shipped overlay manifest — always match. A mismatch
fails in CI, naming the stale line, instead of silently loading the wrong thing
on hardware.

## What actually ships for one RM: the "overlay"

You don't load a single file. Each RM ships as an **overlay** — a small set of
artefacts built together and locked to one specific shell:

- a **partial bitstream** — the fabric definition for the RM (what gets written
  into the RP);
- a **clearing bitstream** — a companion file needed on this FPGA family to
  cleanly wipe the *previous* RM before writing the new one;
- a **manifest** — a small JSON file describing the overlay: its `rm_id`, sizes,
  checksums, and the `static_id` of the shell it was built against.

That `static_id` is a **fingerprint of the exact shell** the overlay was built
for. The pusher refuses to load an overlay whose `static_id` doesn't match the
shell on the board — so an incompatible design **physically cannot** be loaded
into the wrong socket. (More on why the clearing bitstream exists, and how a
push works end to end, in
[Over-the-wire reconfiguration](over-the-wire-reconfiguration.md).)

## What to remember

- **RP** = the fixed socket (a region of fabric); **RM** = one design you load
  into it. **DUT** = the RM you're currently testing.
- The flagship DUT is **nanoSoC**, a real Arm Cortex-M0 SoC (single-core, and a
  richer dual-core Ethernet variant).
- An RM ships as an **overlay** = partial + clearing bitstream + manifest, locked
  to one shell by its `static_id`.
- Every RM carries a readable **`rm_id`**, so the platform can *prove* which
  design is loaded rather than trust a name.

!!! info "The engineering detail"
    This page explains the idea. The precise, citation-carrying account of
    the same mechanism is [`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)
    — §3, *DFX, `static_id`, and why a mint is expensive*.

Next: [Over-the-wire reconfiguration →](over-the-wire-reconfiguration.md)
