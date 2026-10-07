# Glossary

Every acronym and platform-specific term you're likely to meet in this repo, in
one plain line each. Where a concept has its own page, it's linked.

## Core platform terms

- <a id="dut"></a>**DUT — Device Under Test.** The chip design currently loaded
  into the swappable slot and being tested. Usually [nanoSoC](#nanosoc). See
  [Reconfigurable Modules & the DUT](concepts/reconfigurable-modules-and-dut.md).
- **Shell (static shell).** The always-on part of the FPGA design: network, helper
  CPU, config engine, debug bridges, status display. Programmed once; survives DUT
  swaps. See [The shell & the RP](concepts/shell-and-partition.md).
- **RP — Reconfigurable Partition.** The fixed *region* of fabric reserved for
  swapping — "the socket".
- **RM — Reconfigurable Module.** One *design* loaded into the RP — "a chip you
  plug into the socket". Many RMs, one RP.
- **Overlay.** What ships for one RM: a partial bitstream + a clearing bitstream +
  a JSON manifest, locked to one shell.
- **Greybox.** An inert, tied-off RM that legally occupies the RP as the default /
  safe fallback until a real design is loaded.
- **Fabric.** The FPGA's grid of configurable logic (LUTs, flip-flops, block RAM,
  wiring) that a design is built from. See
  [FPGA in 5 minutes](concepts/fpga-in-5-minutes.md).
- **Tender.** The external computer (Raspberry Pi 5 or shared lab server) that runs
  the Python "brain" and does out-of-band board management (JTAG, MCC, power).

## FPGA & reconfiguration

- <a id="fpga"></a>**FPGA — Field-Programmable Gate Array.** A chip whose internal
  digital circuitry is reprogrammable after manufacture.
- **DFX — Dynamic Function eXchange.** Xilinx's name for **partial
  reconfiguration**: reprogramming part of the fabric (the RP) while the rest keeps
  running. (Older name: PR / Partial Reconfiguration.)
- **Bitstream.** The binary file that configures the FPGA fabric. `.bit` (Xilinx
  format) / `.bin` (raw, for ICAP delivery).
- **Partial bitstream.** A bitstream that configures only the RP, not the whole
  chip.
- **Clearing bitstream.** A companion bitstream that returns an RP region to a
  blank state before a new RM is written (required on UltraScale).
- **Static / dynamic region.** Static = the shell (unchanging); dynamic = the RP
  (reprogrammable).
- **ICAP — Internal Configuration Access Port.** The on-chip door that lets logic
  already running on the FPGA write new bitstreams into the fabric.
- **HWICAP.** The AXI controller the shell's CPU uses to feed data to ICAP.
  **HBICAP** is a higher-bandwidth variant (not used in v1).
- **DFX Decoupler.** The "airlock" IP that electrically isolates the RP from the
  shell during a swap.
- **AXI Shutdown Manager.** IP that safely terminates bus transactions to the RP so
  a swap can't leave a shared bus hung.
- **Greybox / blank config / `pr_verify`.** Vivado DFX build artefacts and the
  compatibility check that proves two configs share the same static region.
- **DCP — Design Checkpoint.** Vivado's saved-design file (`.dcp`): a netlist plus
  whatever placement, routing and constraints it has so far. The one that matters
  here is `static_routed_locked.dcp` — the routed shell with the partition
  black-boxed and its routing locked — because [`static_id`](#static_id) is the
  CRC-32 of exactly that file.
- **OOC — out-of-context synthesis.** Synthesising one module on its own, with no
  surrounding design and no I/O buffers inserted. Every RM is synthesised OOC into
  a DCP that the DFX flow later links into the locked static.
- **Pblock.** The physical rectangle of fabric a region is constrained to. The RP's
  Pblock is what an RM has to fit inside; the utilization report from an OOC synth
  is how you find out whether it does.
- **Partition pins.** The signals crossing the RP ⇄ shell boundary — 35 ports,
  136 bits, frozen in `partition-pins.md`. Directions there are stated from the
  *shell's* view and invert on the RM side.
- **`pin_check`.** The gate (`make -C fpga/dfx pin-check`) that mechanically diffs
  every registered RM wrapper against the boundary. It catches in a second what
  `pr_verify` would otherwise catch after a full place and route.
- **Half-mint.** A shipped image whose two halves disagree — a bitstream built with
  one feature set and the firmware beside it built with another. It is invisible to
  a `static_id` check, because a `static_id` identifies only the half that has a
  CRC; `FIELDED_SHELL.md` records the firmware's build flags for exactly this
  reason.
- <a id="static_id"></a>**static_id.** A 32-bit fingerprint of one exact locked shell. An overlay must
  match it or it is refused — "will this design physically fit this shell?".
- **rm_id.** A 32-bit hardware ID baked into an RM and readable from a register,
  encoding `{major, minor, design_id}` — "which design (and version) is actually
  loaded?".
- **Mint.** A full rebuild of the static shell that produces a *new* shell — and
  therefore a new `static_id`, which is the CRC-32 of the routed
  and locked static checkpoint (the DCP). Minting is the expensive operation on this
  platform: it is a full place-and-route, and everything keyed to the old shell stops
  fitting the moment it completes. Most work is deliberately arranged to avoid one.
- **Re-key.** The work that follows a mint: rebuilding *every* overlay's partial
  bitstream against the new static and rewriting the manifests so they carry the new
  `static_id`. An overlay that is not re-keyed is refused by the pusher rather than
  loaded — the mismatch is caught, not tolerated. "One mint, one re-key" is why
  unrelated static changes get batched together.
- **Fielded vs minted.** Two different questions with routinely different answers.
  *Fielded* = the shell the board actually boots from its config SD right now.
  *Minted* = the shell the current overlay set is keyed to. They drift apart whenever
  a new shell is built but not yet written to the board's SD, and a swap will fail
  confusingly if you assume they match. Always resolve which is which before
  debugging a refused overlay — see
  [`docs/FIELDED_SHELL.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/FIELDED_SHELL.md).
- **USERCODE / `static_usercode`.** The 8-hex-digit git SHA stamped into the
  bitstream at build time as `BITSTREAM.CONFIG.USERID`, and readable back over JTAG
  as `REGISTER.USERCODE` without disturbing the running design. It identifies **which
  mint** is loaded in the fabric — not which firmware is baked into it, which is a
  separate `updatemem` step. It is the first thing to read on a board that is
  configured but behaving unexpectedly.

## The board & configuration

- <a id="mps3"></a>**MPS3.** The Arm development board this runs on (an FPGA
  prototyping board carrying the Xilinx chip).
- **XCKU115.** The specific Xilinx **Kintex UltraScale** FPGA on the MPS3. A large,
  2-SLR device.
- **SLR — Super Logic Region.** A physical slice of a large FPGA die; the XCKU115
  has two. The RP is kept within one SLR.
- **MCC — Motherboard Configuration Controller.** The board's built-in controller
  that loads a *full* FPGA image from SD and reboots — the slow path this platform
  avoids in the inner loop.
- **config-SD.** The MCC's own microSD card (it enumerates over USB with the volume
  label `V2M-MPS3`), holding the board configuration and the full FPGA image the MCC
  loads at power-on — `MB/HBI0309C/Nanosoc/nanosoc.bit` plus the text files that
  point at it. This is the only non-volatile home of the fielded shell: a bitstream
  pushed over JTAG is *volatile* and a power-cycle reverts the board to whatever the
  config-SD holds.
- **updatemem.** The Vitis tool that bakes a compiled MicroBlaze ELF into an existing
  bitstream's block RAM, producing a new bitstream **without re-implementing the
  design**. It is how shell firmware is changed without paying for a mint: same
  fabric, same `static_id`, new firmware.
- **LAN9220 / SMSC9220 / smsc911x.** The board's 10/100 Ethernet chip (MAC+PHY) and
  the names of its software driver. The single network port for everything.
- **QSPI / eMMC / µSD.** On-board non-volatile memories reachable by the FPGA
  design; can hold a self-boot default overlay. QSPI = the 8 MB flash.
- **MicroBlaze.** A small soft CPU (built from fabric) that runs the shell's
  bare-metal coordinator firmware. Not running Linux.
- **lwIP.** The lightweight TCP/IP stack the MicroBlaze runs.
- **MMCM / clocking wizard / DRP.** FPGA clock-generation blocks; DRP is the
  interface that lets firmware retune the DUT clock at runtime.
- **XiP — eXecute in Place.** Running code directly from flash memory rather than
  copying it to RAM first (a nanoSoC boot mode).
- **LMB — Local Memory Bus.** The MicroBlaze's fast local-memory bus (its program
  RAM lives here).
- **USR_ACCESS.** A bitstream register the release version is stamped into.

## Debug & trace

- **SWD — Serial Wire Debug.** Arm's 2-wire debug link to a Cortex-M CPU.
- **JTAG.** The universal, older multi-wire debug/boundary-scan standard SWD
  descends from.
- **DAP — Debug Access Port / SW-DP — Serial Wire Debug Port.** The debug
  front-end *inside* the Arm CPU that SWD talks to (target side).
- **SWJ-DP.** A combined debug port that supports both SWD and JTAG; the DUT's
  canonical debug port.
- **CoreSight.** Arm's on-chip debug/trace architecture (the DAP, trace units,
  etc.).
- **CMSIS-DAP.** Arm's standard for debug-probe *firmware* (host side).
- **OpenOCD.** Host program that speaks SWD/JTAG on one side and GDB on the other.
- **remote_bitbang.** An OpenOCD mode that sends individual SWD/JTAG pin movements
  over a TCP socket — how SWD is tunnelled here.
- **SoCScope.** The platform's trace plane: an on-chip trace/observation design kept
  in its own repository (a sibling checkout found via `SOCSCOPE_HOME`) and delivered
  here as an RM, `rm_socscope`. Because it conforms to the frozen RP boundary it is a
  partial build against the locked static — loading it costs no mint and invalidates
  no other overlay. Its sources are never vendored into this repo; the build records
  which SoCScope revision it was made from.
- **ILA — Integrated Logic Analyzer.** A logic-analyzer block placed in the fabric
  to capture signals.
- **VIO — Virtual Input/Output.** A companion debug block to drive/observe signals
  from the tool.
- **XVC — Xilinx Virtual Cable.** JTAG-over-TCP, so Vivado can reach an ILA over
  the network.
- **Debug Bridge.** Xilinx IP bridging AXI/JTAG/BSCAN to on-chip debug cores;
  paired with the XVC server.
- **BSCAN.** The FPGA's internal boundary-scan/JTAG access used by debug cores.
- **SWO / ITM.** Cortex-M single-wire trace output / the trace unit that produces
  it. Relayed like a UART.
- **ADP / SoCDebug / FT1248.** nanoSoC's built-in debug/test-injection command path
  (used by the SoC Labs cocotb bench and RP2040 test board).
- **DPIDR / CPUID.** Identity registers read to confirm the DAP and CPU on connect.
- **diag mailbox.** A struct the shell firmware refreshes at a fixed address on
  every pass of its main loop, readable over JTAG. It is the **only** telemetry
  available *during* a swap, when the control channel is parked. Anchored to the
  top of the MicroBlaze local RAM (`lmb_kb*1024 - 0x80`), and the LMB decode
  aliases — so a wrong belief about the RAM size returns a plausible wrong answer
  rather than an error.
- **xsdb.** Xilinx's system-debugger command shell. Used here to read the diag
  mailbox and shell CSRs over JTAG when the network path is unavailable.

## Ethernet (MAC-in-operation testing)

- **MAC — Media Access Controller.** The digital block that frames/deframes
  Ethernet; the DUT has one under test.
- **PHY.** The analog/physical Ethernet layer a MAC talks to. The shell provides a
  **virtual PHY** in fabric toward the DUT.
- **MII / RMII.** (Reduced) Media Independent Interface — the wires between a MAC
  and a PHY. RMII is the 2-bit-wide variant crossing the RP boundary.
- **MDIO / MDC.** The 2-wire management bus for configuring a PHY; the shell answers
  it with a synthesized MDIO slave so the DUT's link bring-up doesn't hang.
- **Virtual PHY.** A fabric block that emulates a PHY (RMII datapath + MDIO register
  model) so the DUT MAC can be tested as a black box.
- **Link-partner MAC.** A fabric MAC that receives the DUT's frames and feeds the
  bridge.
- **gen/checker.** An error-injecting traffic generator/checker (bad FCS, runts,
  giants) that makes it MAC *verification*, not just "it pings".
- **PTP.** Precision Time Protocol — hardware timestamping in the richer Ethernet
  DUTs.

## Firmware, host & process

- **Coordinator.** The shell firmware module that sequences a swap (quiesce →
  decouple → clear → write → verify → release).
- **config agent.** The firmware module that receives bitstreams (TFTP/raw-TCP) and
  feeds HWICAP.
- **overlay store.** The firmware module that manages a self-boot default overlay in
  QSPI (with A/B slots).
- **superloop.** The shell firmware's single `for(;;)` loop (there is exactly one,
  in `firmware/platform/src/main.c`). No RTOS, no threads: lwIP and every port
  server get one bounded turn per pass. This is why a swap is a non-blocking
  *stepper* — a multi-second bitstream write inside the loop would starve the very
  TCP connection driving it.
- **pyverify.** The host-side Python library — the "PYNQ experience" facade
  (`Mps3Board`), pushers, clients, console/debug wrappers. It is the **front door**: the CLI
  and the API you drive the board with.
- **socket_harness.** The host-side *library* pyverify imports — endpoint registry,
  `xsdb` CSR access, console bridge. Not a second command-line tool.
- **FakeShell.** A byte-conformant test double for the shell, so the host half is
  tested with no board. Its conformance test matters because a fake once turned out
  to be *more permissive* than the firmware, and kept a whole end-to-end suite green
  against the wrong swap ordering.
- **`make check` / `make check-ci` / `make lint`.** The gate, in three pieces:
  `check-ci` is the board-free logic stages that hosted CI runs on every branch,
  `lint` is the Verilator job (separate on purpose, so a tool-version difference is
  not noise on the logic gate), and `check` is the local superset that adds the
  simulator benches and the overlay round-trip. Stages that cannot run **skip
  loudly** — a green gate hiding an empty one is a failure mode this project has
  been bitten by.
- **`harness_regression.sh`.** The tiered go/no-go run for a newly built harness
  image (bitstream + firmware). Tiers 0–2 are board-free; tier 3 touches the board
  and refuses to run without a lease and an explicit flag.
- **Board lease.** The claim you take on a shared board before touching it, and
  release with the same holder id. An unreleased lease leaves the board held
  against everyone else.
- **fpgahub / the hub.** The lab's board-management service, and by extension the
  host that runs it (`<hub-host>`). It owns board leases, power and reconfiguration,
  and — importantly — it is the only host from which the board's data IP is reachable,
  so console, push and debug commands are run *through* the hub rather than from a
  workstation. Take a lease before touching a board and release it with the same
  holder id, or the board stays HELD.
- **overlay manifest.** The JSON schema describing an overlay (see the
  [contracts](reference/contracts.md)).
- **CMSDK.** Arm's Cortex-M System Design Kit — the reference components nanoSoC is
  built from.
- **SMM — Soft Macro Model.** Arm's CMSDK-based FPGA reference system on MPS boards.
- **ser2net / socat.** Host tools that present a TCP stream as a local serial port.
- **cocotb.** A Python-based hardware simulation/testbench framework used for the
  RTL benches.
- **Verilator.** An open-source RTL simulator/linter used for the lint gate.
- **RTL — Register Transfer Level.** The style of HDL that describes hardware as
  registers and the logic between them.
- **HDL — Hardware Description Language.** Verilog / VHDL / SystemVerilog.
- **AAA — Arm Academic Access.** The programme this project runs under; source of the
  licensed Cortex-M0/CMSDK IP.
- **PYNQ.** A framework pairing Python/Jupyter with FPGA overlays on a hard
  processor — the inspiration this platform "disaggregates".

## nanoSoC family

- <a id="nanosoc"></a>**nanoSoC.** The flagship DUT: a small Arm Cortex-M0 SoC (from
  SoC Labs, on CMSDK). Single-core, plus a dual-core Ethernet **multicore** variant.
- **nanosoc_upy.** A nanoSoC build that boots MicroPython (from flash via XiP, or
  from baked-in RAM in the scaffold variant).
- **AHB-Lite / APB.** The Arm on-chip buses inside nanoSoC (fast interconnect /
  peripheral bus).
- **DMAC.** The DMA controller inside nanoSoC.
- **UART.** The serial console peripheral whose output you read over TCP.
