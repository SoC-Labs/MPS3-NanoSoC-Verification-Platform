# The shell & the reconfigurable partition

*Prerequisite: [FPGA & the fabric in 5 minutes](fpga-in-5-minutes.md).*

## The core split

You now know an FPGA's fabric can be reprogrammed **in part** while the rest
keeps running ([DFX / partial reconfiguration](fpga-in-5-minutes.md)). This
platform divides the fabric into exactly two pieces:

- **The static shell** — the part that is programmed once and stays put.
- **The Reconfigurable Partition (RP)** — a reserved region of fabric that can
  be reprogrammed on its own, over and over, without touching the shell.

> **Analogy:** the shell is the **motherboard + operating system**; the RP is a
> **CPU socket**. You can pull one chip out of the socket and drop another in
> while the rest of the machine keeps running. The socket's shape (pin count,
> voltage, size) is fixed; what you plug into it can change.

```
              THE FPGA FABRIC (one XCKU115 chip)
  ┌──────────────────────────────────────────────────────────┐
  │  STATIC SHELL  (programmed once, always running)           │
  │                                                            │
  │   network stack   helper CPU   config engine   debug      │
  │   status display  Ethernet MAC  reset/clock control        │
  │                                                            │
  │      ┌────────────────────────────────────────────┐       │
  │      │  RECONFIGURABLE PARTITION (the RP)          │       │
  │      │  a reserved region of fabric --             │       │
  │      │  holds ONE swappable design (the DUT)       │       │
  │      └────────────────────────────────────────────┘       │
  └──────────────────────────────────────────────────────────┘
```

## Why split it this way?

Because the shell holds **everything you rely on to reach the board**: the
Ethernet connection, the little on-board helper processor that receives your
commands, the engine that reprograms the RP, and the debug channels. If any of
those lived in the swappable region, then swapping the DUT — or a DUT that hangs
— could take the whole board off the network. By keeping them in the **static**
shell, a broken or wedged DUT can **always be swapped out from the outside**.
The board's only way in is Ethernet, and Ethernet is entirely in static logic.

## What's in the shell

The shell is a small system-on-chip in its own right, built as a block diagram
in Vivado. Its main parts, in plain terms:

- **A helper CPU** — a soft **MicroBlaze** processor (a small CPU *built out of
  fabric*, not a separate chip) running a tiny bare-metal C program. It is the
  board's "receptionist": it accepts your network commands and orchestrates
  everything. It is **not** running Linux — no operating system, one loop, one
  job at a time.
- **A network stack** — the MicroBlaze runs **lwIP** (a lightweight TCP/IP
  stack) and drives the board's **LAN9220** Ethernet chip. This is the single
  network port that carries *everything*: your control commands, the bitstreams,
  the debug traffic, and the DUT's console.
- **The configuration engine** — **HWICAP → ICAP**, the hardware that can write
  new bitstreams into the RP from *inside* the chip (see
  [Over-the-wire reconfiguration](over-the-wire-reconfiguration.md)).
- **The DFX decoupler** — an electrical "airlock" that cleanly disconnects the
  RP from the shell during a swap, so half-formed logic can't glitch the shell
  while the slot is being rewritten.
- **Debug bridges** — servers that expose the DUT's processor debug (**SWD**)
  and the FPGA's own logic-analyzer (**ILA over XVC**) over the network. See
  [Debugging the DUT](debugging-the-dut.md).
- **Clock & reset control** — the shell generates the DUT's clock and can reset
  it on command.
- **The status display** — the board's small LCD is driven by the shell to show,
  at a glance, what's loaded and whether it's healthy.

## The boundary between shell and RP

The shell and the DUT meet at a **deliberately narrow, frozen boundary** — 35
slow, simple signals: clocks and resets, the JTAG debug wires, the DUT's Ethernet
(RMII + MDIO) pins, its serial console streams, a 32-bit hardware ID, some
general-purpose I/O, and the flash pins. Crucially, **there is no shared
high-speed bus** crossing the boundary — that keeps a misbehaving DUT from
corrupting shell memory or hanging a shared bus.

This boundary is a **contract**. Because many people build different DUTs against
the *same* shell, the exact list of boundary signals is frozen and version-
controlled in [`partition-pins.md`](../reference/contracts.md), with a companion
timing contract. A DUT that doesn't match the boundary simply won't fit the
socket — and the build's automated checks catch that before it ever reaches the
board.

## The safety default: the greybox

When the shell first powers up, the RP has to contain *something* legal — an
empty socket is not a valid FPGA design. So the RP ships holding a **greybox**: a
tied-off, do-nothing stub that occupies the region legally until a real design is
loaded. It is the inert default and the safe fallback.

## What to remember

- **Static shell** = always-on infrastructure (network, helper CPU, config
  engine, debug). **RP** = the swappable slot.
- Everything you need to *reach* the board lives in the shell, so a broken DUT
  can always be swapped out.
- The shell↔DUT boundary is a small, frozen set of signals — a versioned
  **contract**, not an ad-hoc wiring.
- The RP is never empty: it holds a **greybox** stub until a real design lands.

!!! info "The engineering detail"
    This page explains the idea. The precise, citation-carrying account of
    the same mechanism is [`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)
    — §2, *The two halves of the fabric*, and §2.3 for the boundary signal by signal.

Next: [Reconfigurable Modules & the DUT →](reconfigurable-modules-and-dut.md)
