# MPS3 nanoSoC Verification Platform

**A way to load a chip design onto real hardware over the network in seconds,
and drive it entirely from Python — no cables to replug, no full reboot.**

This site is written for a competent **software engineer who is new to FPGAs**.
Every hardware term is introduced in plain language the first time it appears;
if a word looks like jargon, it is either explained inline or in the
[Glossary](glossary.md).

---

## What is this, in one screen?

The platform turns an [Arm MPS3 development board](glossary.md#mps3) into a
shared, network-attached lab instrument for testing small Arm Cortex-M
**System-on-Chip (SoC)** designs.

Think of it as a **remote-controlled test socket for chip designs**:

- A **permanent "shell"** lives on the board's programmable chip. It owns the
  network connection, a small on-board helper processor, the plumbing that
  reprograms part of the chip, and the debug channels. It is always there — like
  the operating system of the board.
- A **swappable slot** holds *one chip design under test at a time* — the
  **DUT** (device under test). Today that is typically **nanoSoC**, a small Arm
  Cortex-M0 microcontroller SoC.
- You **swap the DUT over Ethernet**, in seconds, by streaming a small file to
  the board. The shell reprograms just the slot; the network never drops.
- You then **drive and observe the DUT from Python** — send data to its serial
  console, attach a debugger, read its status — all over the same network cable.

The usual analogy the project uses is a **"disaggregated PYNQ"**. PYNQ is a
popular framework where a Linux+Python "brain" sits on the same board as the
chip logic. This board has no place to run Linux, so the Python brain is moved
onto a **separate computer** (a Raspberry Pi 5 or a shared lab server, called
the *tender*) that talks to the board over the network. Same experience, split
across two machines.

```
   Your computer / lab server            The MPS3 board
  ┌──────────────────────┐            ┌───────────────────────────────────┐
  │  Python (pyverify)   │  Ethernet  │  SHELL  (always on)               │
  │  OpenOCD debugger    ├────────────┤   network + helper CPU + config   │
  │  overlay files       │            │   engine + debug bridges + display│
  └──────────────────────┘            │  ┌─────────────────────────────┐  │
                                       │  │ SWAPPABLE SLOT (the DUT)    │  │
                                       │  │ one SoC design at a time,   │  │
                                       │  │ replaced live over the wire │  │
                                       │  └─────────────────────────────┘  │
                                       └───────────────────────────────────┘
```

Here is the whole workflow, in real code from the repository:

```python
from pyverify import Mps3Board

with Mps3Board("192.168.10.101") as board:
    result = board.deploy("nanosoc")               # push the design + swap the slot
    result.reattach.apply()                        # reopen debug / console channels
    board.uart0.assert_contains(b"nanosoc boot")   # the verdict comes from the console
```

---

## Why it exists

The board's built-in way to load a design (its **MCC** configuration
controller) reloads the *entire* chip from an SD card and reboots — that costs
**minutes** per change. This platform removes that from the inner loop: load the
shell **once**, then swap only the DUT **over the network in seconds**. The
result is a fast edit → load → test cycle for chip bring-up, register and
peripheral testing, and Ethernet-MAC-in-operation testing.

It is **proven on real silicon**: a Python host has pushed a 1.31 MB design over
Ethernet only (no debug cable), the slot reconfigured through the shell at
~570 KB/s, the loaded design was checked against its expected hardware ID, and
the network survived the swap.

---

## Where to go next

New to FPGAs? Read the concept pages in order — each is 1–2 screens of plain
language:

1. [FPGA & the fabric in 5 minutes](concepts/fpga-in-5-minutes.md) — what the
   chip actually is and why it can be reprogrammed.
2. [The shell & the reconfigurable partition](concepts/shell-and-partition.md) —
   the "always-on part" vs the "swappable slot".
3. [Reconfigurable Modules & the DUT](concepts/reconfigurable-modules-and-dut.md) —
   what you actually swap in, and what nanoSoC is.
4. [Over-the-wire reconfiguration](concepts/over-the-wire-reconfiguration.md) —
   how a swap happens over Ethernet.
5. [Debugging the DUT (SWD/JTAG)](concepts/debugging-the-dut.md) — how you halt,
   step, and inspect the chip remotely.

Ready to *do* something?

- [Getting started & building](guides/getting-started.md)
- [Running the board-free gate (`make check`)](guides/board-free-gate.md)
- [Deploying an RM over the wire](guides/deploying-an-rm.md)

Ready for the engineering account? **[`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)**
is the one entry document: the same mechanisms as the concept pages, stated
precisely, with the authority file named for every fact. The
[Reference](reference/index.md) section indexes it alongside the frozen
interface contracts and the CI notes.

!!! note "This site organises; it does not replace"
    The engineering source of truth is the repository's own documents — the
    [interface contracts](reference/contracts.md), the
    [architecture spec](reference/architecture.md), and each area's `README`.
    This site is a friendly front door to them, not a copy. Where the two ever
    disagree, trust the repository docs (and, per project convention, the git
    history over any status document).
