# FPGA & the fabric in 5 minutes

*Audience: you write software and have never used an FPGA. No prior hardware
knowledge assumed.*

## The one idea

A normal processor (the CPU in your laptop) has **fixed circuitry**. You change
what it *does* by feeding it different software; you cannot change the circuits
themselves.

An **FPGA** (Field-Programmable Gate Array) is a chip whose **circuitry itself
is programmable**. Instead of running instructions, you *become the chip
designer*: you describe digital logic — gates, registers, memories, buses — and
the FPGA rearranges its internal wiring to *become* that logic. "Field-
programmable" means you can do this after the chip is manufactured, in the
"field", as many times as you like.

> **Software analogy that mostly works:** compiling source code produces a
> binary the CPU executes. "Compiling" a hardware design produces a
> **bitstream** the FPGA *turns into*. The big difference: a CPU binary runs on
> unchanged hardware; an FPGA bitstream *changes the hardware*.

## What's inside — "the fabric"

Picture a huge grid of tiny, identical, configurable building blocks with a sea
of programmable wires between them. That grid is **the fabric**. Its main
ingredients:

- **Look-up tables (LUTs)** — tiny configurable truth tables that can be set to
  compute any small logic function. These are the "logic" atoms.
- **Flip-flops (registers)** — one-bit memories that hold a value until the next
  clock tick. These give the design state and timing.
- **Block RAM (BRAM)** — small on-chip memories for buffers, FIFOs, and program
  memory.
- **DSP blocks, clock generators, I/O pins** — specialised blocks for maths,
  timing, and talking to the outside world.
- **The interconnect** — the programmable wiring that stitches all of the above
  into your design.

When you "program the FPGA", you are setting millions of these little switches.
The file that holds those switch settings is the **bitstream**.

The specific FPGA on this platform is a **Xilinx Kintex UltraScale XCKU115** — a
large device. You do not need to know its details; just that it is big enough to
hold both the always-on "shell" and a real Cortex-M0 SoC at the same time.

## How a hardware design gets made (the toolchain, from a software lens)

| Software world | FPGA world |
|---|---|
| Source code (`.c`) | **HDL** — Hardware Description Language (Verilog/VHDL/SystemVerilog), plus block diagrams |
| Compiler | **Synthesis** — turns HDL into a netlist of gates/registers |
| Linker + optimiser | **Place & route** — decides *which physical* LUTs/wires implement it, and whether it can run fast enough (**timing closure**) |
| Executable binary | **Bitstream** (`.bit` / `.bin`) — the switch settings loaded into the fabric |
| `./run` | **Configuration** — loading the bitstream so the fabric *becomes* the design |

The tool that does all this here is **Vivado** (AMD/Xilinx's design suite). You
do not need Vivado to *use* the platform — only to *rebuild* the hardware. Day to
day you push already-built bitstreams over the network.

## Two facts that make this platform possible

1. **You can reprogram just *part* of the fabric while the rest keeps running.**
   That's called **partial reconfiguration** (Xilinx brands it **DFX** —
   Dynamic Function eXchange). It is the whole trick behind swapping the DUT
   without disturbing the network. See
   [The shell & the reconfigurable partition](shell-and-partition.md).

2. **A design can carry an on-chip identity you can read back.** The platform
   bakes a 32-bit **hardware ID** into each swappable design, so after loading
   one, the shell can read a register and *prove* the right design is in the
   slot — the on-board status display "never lies" because it reads this live,
   not from a cached name.

## What to remember

- An FPGA is **reprogrammable hardware**; a **bitstream** is the "binary" that
  defines the hardware.
- **The fabric** = the grid of LUTs, flip-flops, BRAM, and programmable wiring.
- You can reprogram **part** of the fabric live — that is DFX / partial
  reconfiguration, and it is the foundation of everything else here.

!!! info "The engineering detail"
    This page explains the idea. The precise, citation-carrying account of
    the same mechanism is [`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)
    — §2, *The two halves of the fabric*.

Next: [The shell & the reconfigurable partition →](shell-and-partition.md)
