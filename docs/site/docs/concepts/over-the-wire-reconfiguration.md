# Over-the-wire reconfiguration

*Prerequisite:
[Reconfigurable Modules & the DUT](reconfigurable-modules-and-dut.md).*

"Over-the-wire reconfiguration" is the headline capability: **replace the chip
design in the RP by streaming a file over Ethernet**, in seconds, without a
reboot and without any cable other than the network.

## The problem it solves

The board's factory way to change a design is its **MCC** (Motherboard
Configuration Controller): it reads a full bitstream from an SD card and reboots
the whole FPGA. That reloads *everything* and costs **minutes**. Worse, a full
reload takes the network down with it.

Over-the-wire reconfiguration reprograms **only the RP** (the swappable slot),
from *inside* the running chip, while the shell — and your network connection —
stays up the entire time.

## The key piece: ICAP

FPGAs can normally only be programmed from the outside (via a cable or the SD
card). But this device also has an **internal** door into its own configuration
memory: the **ICAP** (Internal Configuration Access Port). ICAP lets logic
*already running on the chip* write new bitstream data into the fabric.

The shell drives ICAP through a small controller called **HWICAP** (an AXI-based
front-end the MicroBlaze can write to word by word):

```
your file  ──Ethernet──▶  MicroBlaze  ──▶  HWICAP  ──▶  ICAP  ──▶  the RP reprograms
```

Because the MicroBlaze is *in* the shell and ICAP writes *only* the RP region,
the shell keeps running throughout.

## Why a swap needs *two* bitstreams (the clearing step)

On this FPGA family (UltraScale), you cannot just overwrite one RM with another.
You must first **clear** the region the old RM occupied, then write the new one.
So every swap streams two things:

1. the **clearing bitstream** of the *currently loaded* RM (returns the region to
   a known blank state), then
2. the **partial bitstream** of the *new* RM.

This is exactly why an [overlay](reconfigurable-modules-and-dut.md#what-actually-ships-for-one-rm-the-overlay)
ships both. (The precise ownership of *which* clearing bitstream is used when is
one of the platform's carefully-tracked interface decisions.)

## The swap sequence, step by step

A swap is choreographed by the shell's **coordinator** firmware so that the
multi-second write can never starve the network. From the outside it looks
atomic; inside, it is:

1. **Quiesce** — the coordinator gates the debug, console, and DUT-Ethernet
   traffic so nothing is mid-transaction across the boundary.
2. **Decouple** — the **DFX decoupler** (the "airlock") isolates the RP from the
   shell, and the RP is held in reset.
3. **Clear** — stream the outgoing RM's clearing bitstream into ICAP.
4. **Write** — stream the new RM's partial bitstream into ICAP.
5. **Verify** — read the new design's **`rm_id`** back from a register and check
   it against the pushed overlay's manifest. This is how the shell *proves* the
   intended design actually loaded.
6. **Release** — drop the decoupler and take the RP out of reset. The new DUT is
   live.

The whole thing is a **non-blocking state machine**: the firmware advances the
swap a little on each pass through its main loop, so the seconds-long ICAP write
never blocks the TCP connection that is driving the swap.

## How your file actually gets there

You push the bitstream from the Python host. Two transports exist over the one
network port:

- **TFTP** (UDP) — a simple, lock-step file-push protocol; the default for a
  local network.
- **raw TCP** — a plain streaming alternative, useful when traffic has to pass
  through a tunnel where UDP won't (for example, a remote VPN link).

The `static_id` guard runs before anything is written: the overlay's manifest
must match the shell's live fingerprint, or the push is refused. You cannot load
a design built for a different shell.

## It works on real hardware

This is not a paper design. On silicon, a Python host has pushed a **1.31 MB**
partial over Ethernet only (no debug cable), the RP reconfigured through ICAP at
**~570 KB/s**, the loaded design **verified** against its expected `rm_id`, and
the network stayed up across the swap.

The network is the bottleneck, not ICAP — the board's Ethernet is 10/100, which
is plenty for a fast edit → load → test loop but is why the project does not
over-engineer the ICAP path.

## Self-booting (the persistence idea)

The board can also **self-boot to a default DUT with no host attached**: the
shell can keep a default overlay in its own on-board flash memory (with two
"A/B" slots so an interrupted update can't brick the default) and load it via
ICAP at power-up. This mechanism is designed and partly built; consult the
[architecture spec](../reference/architecture.md) and status docs for exactly
what is proven on silicon today versus planned.

## What to remember

- The magic door is **ICAP** — it lets on-chip logic reprogram the fabric from
  the inside; the shell drives it via **HWICAP**.
- A swap streams **clearing + partial** bitstreams (UltraScale needs the clear
  first).
- The **coordinator** sequences quiesce → decouple → clear → write → **verify by
  `rm_id`** → release, all non-blocking so the network survives.
- The **`static_id`** guard makes loading an incompatible design impossible.
- Proven on silicon: full over-the-wire deploy at ~570 KB/s with the network
  intact.

!!! info "The engineering detail"
    This page explains the idea. The precise, citation-carrying account of
    the same mechanism is [`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)
    — §4, *The swap: the 6900 protocol and the firmware superloop*.

Next: [Debugging the DUT (SWD/JTAG) →](debugging-the-dut.md)
