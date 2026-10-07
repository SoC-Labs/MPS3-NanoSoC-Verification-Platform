# Debugging the DUT (SWD/JTAG)

*Prerequisite:
[Over-the-wire reconfiguration](over-the-wire-reconfiguration.md).*

Once a DUT is loaded, you often want to do what you'd do with any
microcontroller: **load firmware, halt the CPU, single-step, set breakpoints,
read memory and registers**. On a normal dev board you'd plug a debug probe into
a header with a cable. Here, that entire debug link runs **over the network**.

This page explains the debug vocabulary — SWD, JTAG, DAP, OpenOCD, ILA, XVC — in
plain terms, then how the platform tunnels them over Ethernet.

## Two very different "debug" things

People say "debug" for two unrelated activities on an FPGA. Keep them separate:

1. **Processor / software debug** — halt and inspect the **CPU inside the DUT**
   (the Cortex-M0). This is what a software engineer means by debugging: GDB,
   breakpoints, `printf`. The transport is **SWD**.
2. **Fabric / hardware debug** — watch the actual digital **signals** in the
   FPGA logic, like a logic analyzer built into the chip. The instrument is an
   **ILA**; the transport is **XVC**.

You will mostly use #1. #2 is for hardware bring-up.

## The software-debug vocabulary

- **JTAG** — an old, universal 4-5 wire standard for talking to debug logic
  inside chips. It's the granddaddy protocol; many things speak a JTAG-like
  interface.
- **SWD (Serial Wire Debug)** — Arm's compact **2-wire** replacement for JTAG on
  Cortex-M chips (a clock and a bidirectional data line). Same job as JTAG, fewer
  pins. This is how you reach a Cortex-M0's debug unit.
- **DAP (Debug Access Port) / SW-DP** — the debug "front desk" *inside* the Arm
  CPU that SWD talks to. Through it you reach the CPU's registers, memory, and
  halt/step controls. The DUT provides this (it's part of the Arm CoreSight debug
  system); the platform provides the wire to reach it.
- **OpenOCD** — a widely-used open-source program that runs on your computer and
  *speaks* SWD/JTAG on one side and GDB on the other. It is the bridge between
  your debugger and the chip's DAP.
- **CMSIS-DAP** — Arm's standard for debug-probe *firmware* (the host side of the
  link). Referenced by the platform's design as the model for its SWD engine.

> **Putting it together:** GDB ⇄ **OpenOCD** ⇄ **SWD** ⇄ the DUT's **DAP** ⇄ the
> Cortex-M0's registers/memory. Normally OpenOCD drives SWD through a USB probe
> and a cable. Here, there is no cable.

## How SWD gets onto the network

The shell contains a small **SWD server**. OpenOCD (running on the host) uses its
`remote_bitbang` mode: instead of wiggling physical pins, it sends the individual
SWD line movements as tiny text commands over a **TCP socket**. The shell's SWD
server receives them and drives the *internal* SWD wires that cross the boundary
into the DUT's DAP.

```
 host: GDB ⇄ OpenOCD (remote_bitbang) ──TCP──▶ shell SWD server ──▶ DUT DAP ⇄ Cortex-M0
```

So from your laptop you run OpenOCD pointed at the board's IP, and you get real
halt/step/breakpoint/memory access to the Cortex-M0 — with the only physical link
being Ethernet. This is **proven on silicon**: a bare connect reads the expected
Arm SW-DP identity, and a halt/step reports the CPU core and program counter.

!!! warning "Debug channels are gated during a swap"
    SWD (and the consoles and ILA) only work **after a swap that fully
    completed**. During a reconfiguration the boundary is decoupled, so debug is
    deliberately cut. After a swap, the debug port must be **re-attached** — the
    DUT's debug port is rebuilt, so the host re-runs the SWD line reset and DAP
    connect. The Python API returns a "reattach plan" that scripts this for you.

## The DUT's serial console, too

Alongside SWD, the DUT's **UART** (serial console — where its firmware's
`printf` / boot banner appears) is relayed over its own TCP ports. So "watch the
boot log" and "attach a debugger" are both just TCP connections to the board.
There are separate ports for the boot-monitor UART, an application UART, and
optional trace output (**SWO** — a single-wire trace stream from Cortex-M).

## Fabric debug: ILA and XVC (briefly)

For hardware bring-up you sometimes need to see the *raw signals*:

- **ILA (Integrated Logic Analyzer)** — a debug block placed *in the fabric* that
  captures selected signals into on-chip memory, like an oscilloscope built into
  the design.
- **XVC (Xilinx Virtual Cable)** — a protocol that carries JTAG **over TCP**, so
  Vivado's hardware manager can reach an ILA across the network instead of
  through a USB-JTAG cable. The shell runs an XVC server; Vivado connects to it as
  a "virtual cable".

You generally won't touch these unless you're debugging the hardware itself
rather than the software on the DUT.

## First bring-up: the one time you need a cable

There's a bootstrapping catch: the networked debug stack lives in the shell, so
before the shell exists on a brand-new board, there's nothing to tunnel through.
The very first bring-up (and recovery from a wedged shell) uses a **physical
JTAG/SWD link** through the board's debug header to load the shell. After that,
everything is over the network.

## What to remember

- **SWD** = the 2-wire link to a Cortex-M CPU's **DAP**; **OpenOCD** bridges your
  GDB to it. Here it's tunnelled over **TCP**, no cable.
- The DUT's **UART console** is likewise just TCP ports.
- **ILA + XVC** = fabric-signal debugging (a logic analyzer over the network) —
  for hardware bring-up, not everyday use.
- Debug is **gated during a swap**; you **re-attach** afterwards (the Python API
  automates this).
- The **only** time you need a physical cable is first bring-up / recovery of the
  shell itself.

!!! info "The engineering detail"
    This page explains the idea. The precise, citation-carrying account of
    the same mechanism is [`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)
    — §4.1, the port map and the control protocol.

Back to [Concepts overview](../index.md#where-to-go-next) · or start the
[Getting started guide →](../guides/getting-started.md)
