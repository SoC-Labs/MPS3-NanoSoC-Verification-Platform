# `lan9220_if` — AXI ⇄ LAN9220 static-memory-bus interface

Bridges the shell's internal AXI-Stream (from `fpga/ethernet/bridge/`'s
uplink port) to the SMSC/Microchip **LAN9220**'s register/FIFO host
interface, which sits on the MPS3 board's **static memory bus** — a
memory-mapped register/FIFO interface, **not** MII/RMII to fabric (spec §3:
"on the FPGA static memory interface"). This is the harness's *only* path to
the outside world: management, DFX reconfig delivery, debug tunnelling
(SWD/XVC/UART-over-Eth) and the DUT-MAC-in-operation traffic all share this
one 10/100 link (spec §9, §12).

> **W-RTL-ETH status note (2026-07-06, A1/A5 — coordination with W-BD):**
> `lan9220_if.sv` deliberately **stays a Phase-0 stub this wave** while the
> other four ethernet blocks graduated to real RTL. The AXI-EMC decision
> below means the register/FIFO access path is `axi_emc_0` (a vendor cell
> W-BD owns in the harness/shell BD) plus the ported Zephyr driver
> (firmware, W-SMSC) — there is no custom SMC master for this block to
> grow, and the AXIS⇄EMC glue shape cannot be fixed until W-BD lands the
> concrete `axi_emc_0` configuration in the BD. Rather than guess at a
> concurrent agent's interface, this wave leaves the stub + this note.
> Practical consequence for the datapath (do NOT wire yet): this stub ties
> `s_axis_tready` low, so an uplink-bound frame would park `bridge/`'s
> single forwarding sequencer indefinitely (head-of-line block by design
> there). The BD must leave the bridge↔lan9220_if edge unwired (or
> terminate uplink with an always-ready sink) until this block is real.

## Decision: Xilinx AXI EMC vs a hand-rolled SMC master

**Decision: Xilinx AXI EMC (`axi_emc_0`, LogiCORE PG113/EMC family), not a
hand-rolled SMC master.** Already the working assumption in
`fpga/shell/bd/shell_bd.tcl` (`axi_emc_0` cell, §12 reuse table) — this
document makes it a settled decision and gives its parameters rather than a
TODO.

Rationale:
- The LAN9220's host bus (see below) is exactly the async, non-multiplexed,
  16-bit "SRAM-like" peripheral bus the AXI EMC's asynchronous mode was built
  for — one bank, `Sync/Async = Async`, `Memory Type = SRAM`, `Data Bus
  Width = 16`. No custom read/write FSM to design or verify.
  address/data/nOE/nWE timing all become **IP customization GUI fields**
  (picoseconds), not hand-written RTL — far less to get wrong and re-spin.
- Reuse alignment: spec §12's reuse table already names "Xilinx AXI EMC (or
  tiny SMC master)" for this row, and the Arm MPS3 SMM reference designs
  (Corstone SSE-300 AN552, Cortex-R52 AN536 — spec §12 "Ethernet subsystem
  (board)" row) use Arm's own SMC/EMC-equivalent CMSDK block against the
  same LAN9220 part, so the async-SRAM-peripheral model is the proven fit,
  not a guess.
- A hand-rolled master would have to reimplement chip-select decode, R/W
  pulse generation, and read-data capture timing that AXI EMC already gets
  right — for a shared, promiscuous, everything-goes-through-here 10/100
  link, correctness here gates the whole platform (spec §16 "Network is the
  reconfig bottleneck").
- Cost: one AXI4 (or AXI4-Lite) memory-mapped region in the MicroBlaze
  address map, one bank of `axi_emc_0`; register-level FIFO push/pop
  sequencing is still firmware's job either way (this was already true in
  the phase-0 stub, unchanged by this decision).

### AXI EMC parameters (bank 0 — LAN9220)

Configure one `axi_emc_0` bank against the LAN9220's register/FIFO port as
an asynchronous 16-bit SRAM-style peripheral:

| Vivado `axi_emc` GUI field | Value | Why |
|---|---|---|
| `C_MEM0_TYPE` | `SRAM` | LAN9220 register/FIFO port behaves like async SRAM: address valid, `nOE`/`nWE` strobe, data valid — no NOR erase/program sequencing, no page-mode burst protocol of its own (its *own* burst behaviour is the FIFO auto-increment addressing described below, which the EMC sees as ordinary sequential SRAM reads). |
| `C_MEM0_WIDTH` | `16` | LAN9220 host data bus is 16 bits, non-multiplexed with the address bus (confirmed by the board pinout: `SMBF_DATA[15:0]` is a separate net group from `SMBF_ADDR[6:0]` — see pin table below). |
| `C_MEM0_ADDR_WIDTH` | matches `SMBF_ADDR[6:0]` (7 bits → 128 half-word locations = 256-byte register/FIFO address space) | LAN9220's CSR + FIFO-port address range is 256 bytes (0x00–0xFC, dword-aligned registers); a 7-bit *word* address at a 16-bit data width reaches exactly that with no unused high bits. |
| Bank timing (`C_MEM0_TXX_*`, ps units in the GUI) | derived below | See "AC timing" — pick conservative (slower-than-minimum) values for first bring-up, tighten only after a working link is proven on real HW (spec §16 general caution about not over-engineering the reconfig/network path prematurely). |
| `C_INCLUDE_NEGEDGE_IOOBUF` / registered I/O | leave EMC's registered-output option **off** for the control strobes that must combinationally reach the pads through `lan9220_emc_wrap.sv` (see HDPR-29 note below); this project has no RM/DFX boundary on this path (LAN9220 host I/F is 100% static-shell, spec §4.1), so the usual "must re-register at the RP boundary" rule doesn't apply here — any registering is a synthesis/timing-margin choice, not a correctness one. |

### AC timing basis (LAN9220 datasheet / Microchip application notes)

Cited from Microchip/SMSC's LAN9220 asynchronous host-bus-interface
application notes (AN 9.6 and AN 12.5 — Microchip document numbers
`en562794`/`en562725`; see also the DS00002276A hardware integration guide):

- Back-to-back register/FIFO transfers (either `nRD` or `nWR` alone):
  **minimum 80 ns** between pulses.
- Back-to-back **full-duplex** transfers (RX FIFO read interleaved with TX
  FIFO write): minimum **100 ns** between pulses.
- Minimum **2 ns** setup before `nRD`/`nWR` assertion (address/chip-select
  must be stable at least this long before the strobe goes active).
- Read data valid **≤ 30 ns** after `nRD` assertion (this is the number that
  maps most directly onto the EMC's "address/CE valid → data valid" timing
  field).

Translate these into the EMC bank-timing fields conservatively (safety
margin for first bring-up, not the tightest legal number):

| EMC field (name varies slightly by Vivado/PG113 version — verify against
  the live customization GUI before entering) | Recommended value | Basis |
|---|---|---|
| Chip-enable/address valid → data valid (`TCEDV`/`TAVDV`-style field) | **40–50 ns** | ≥ the datasheet's 30 ns read-data-valid figure, plus margin for board-level SMBF sharing with the USB debug FIFO (shared bus = extra capacitance/skew budget). |
| Write pulse width (`TWP`-style field) | **50 ns** | No explicit `nWR` pulse-width minimum was recovered from the sources reachable in this session (see "Not yet verified" below); 50 ns keeps the write cycle comfortably inside the 80 ns back-to-back minimum with margin either side of the strobe. |
| Read/write cycle time (`TRC`/`TWC`-style field) | **80–100 ns** | Directly the datasheet's back-to-back minimum (80 ns single-direction, 100 ns full-duplex) — use 100 ns for the whole bank so RX/TX interleaving never violates it. |
| Address/CE setup before strobe (`TAS`-style field) | **≥ 5 ns** | ≥ the datasheet's 2 ns minimum, rounded up for margin. |

**Not yet verified in this session** (flag for A6/A3 before finalizing the
EMC bank config, and before firmware timing-critical loops depend on it):
the exact `nWR` pulse-width minimum, the byte-enable / 8-bit-vs-16-bit mode
strapping, and whether `SMBF_FIFOSEL` genuinely maps to the LAN9220's own
`FIFO_SEL` device pin (fast-burst FIFO-port access bypassing full address
decode) as this document assumes, or is a board-level (non-LAN9220) signal.
The datasheet PDF itself could not be text-extracted in this session (only
secondary application-note search snippets were reachable); **pull the
primary LAN9220 datasheet's "AC Characteristics — Asynchronous Interface"
table before tape-out** and correct the EMC bank-timing fields above if they
disagree. Until then, the conservative (slower) values above are the safe
default — a too-slow EMC config costs bandwidth on an already-bottleneck
10/100 link (spec §16), not correctness; a too-fast one risks silent
corruption, which is the worse failure mode to ship.

## The SMC bus signals

Two groups, per the board's static-memory-bus wiring — confirmed against
**three independent, mutually-consistent sources** in this repo tree (do not
re-derive from scratch; these three already agree):
1. `nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/fpga_pinmap.xdc` (raw
   PACKAGE_PIN/IOSTANDARD list, legacy 2021.1 target)
2. `nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/nanosoc_design_wrapper.v`
   (port list on the legacy monolithic top, `SMBF_*`/`ETH_*` group — ties
   them off; that target never actually drove the LAN9220)
3. `fpga/monolithic/nanosoc_mps3.xdc` (this project's own W1, already
   reorganized with per-group provenance comments — "LAN9220 Ethernet SMC
   interface" + "SMBF_* static memory bus" groups)

**This module and `mps3_harness.xdc` copy pin/IOSTANDARD assignments from
these — do not re-derive package pins independently.**

### Group 1 — LAN9220-specific (device chip-select / interrupt)

| Signal | Dir (board pin) | Package pin | IOSTANDARD | Notes |
|---|---|---|---|---|
| `ETH_nCS` | O | AL24 | LVCMOS18 | LAN9220 chip-select, active-low. |
| `ETH_nOE` | O | AJ23 | LVCMOS18 | Device-side output-enable/read-qualifier — see open note below on how this relates to the shared `SMBF_nOE`. |
| `ETH_INT` | I | AK23 | LVCMOS18 | LAN9220 interrupt, board → FPGA. No partition-pins.md analog (board-level signal, not a DUT one) — terminates in the static shell (`dfx_ctl`/`telem`-adjacent status, or a dedicated bit; not yet assigned a regmap home, see `lan9220_if.sv`'s existing "Ambiguity for A6" note). |

### Group 2 — SMBF (shared static-memory bus: LAN9220 *and* the USB debug FIFO)

| Signal | Dir (board pin) | Package pin(s) | IOSTANDARD | Notes |
|---|---|---|---|---|
| `SMBF_ADDR[6:0]` | O | AK20,AK21,AJ18,AJ19,AH21,AJ21,AH19 | LVCMOS18 | Word address into the LAN9220's 256-byte CSR/FIFO-port space (or the USB debug FIFO's own decode, when `USB_nCS` is asserted instead of `ETH_nCS`). |
| `SMBF_DATA[15:0]` | IO (true bidirectional pad) | AK22,AL22,AL19,AL20,AH18,AM19,AN19,AP19,AP20,AM20,AN21,AP21,AR22,AM21,AM22,AN22 | LVCMOS18 | Shared 16-bit data bus. **This module's wrapper (`lan9220_emc_wrap.sv`) owns the tristate mux** — see I21 below. |
| `SMBF_FIFOSEL` | O | AJ20 | LVCMOS18 | Believed = LAN9220's own `FIFO_SEL` pin (bypasses full address decode for fast burst FIFO-port access) — **not yet independently confirmed against the primary datasheet in this session**, see the "Not yet verified" flag above. |
| `SMBF_nOE` | O | AN23 | LVCMOS18 | Shared bus read strobe. |
| `SMBF_nWE` | O | AP23 | LVCMOS18 | Shared bus write strobe. |
| `SMBF_nRST` | O | AL23 | LVCMOS18 | Shared bus **hardware reset** — almost certainly reaches the LAN9220's own `RESET#` pin (and possibly the USB debug FIFO's reset too, since it sits on the same bus group); driver must sequence this per the LAN9220's power-on-reset timing before touching any register (Zephyr `eth_smsc911x.c`'s init sequence already does this — carries over in the port). |

### Open item: `ETH_nOE` vs `SMBF_nOE`

The legacy pinmap keeps `ETH_nOE` (AJ23) and `SMBF_nOE` (AN23) as two
*physically distinct* FPGA pins/pads, not one signal under two names. The
most likely explanation (board-level glue, not re-derivable from the FPGA
pinout files alone) is that `SMBF_nOE`/`SMBF_nWE` are the bus-wide
read/write strobes shared by both peripherals on the SMBF group, while
`ETH_nOE` is a LAN9220-specific qualifier the board ANDs with the shared
strobe so only the LAN9220 (not the USB debug FIFO) drives the shared data
bus back during an Ethernet-targeted read — mirroring how `USB_DACK`/
`USB_DREQ` (DMA-style handshake) is USB's own equivalent arbitration
mechanism instead of a shared `nOE`. **This module does not attempt to fuse
the two pins into one internal signal** (`lan9220_emc_wrap.sv` drives them
from two independent EMC-bank outputs) — confirm the real relationship
against the MPS3 board schematic/TRM before relying on any particular phase
relationship between them.

## AXI-Stream ⇄ FIFO glue vs the pin-level wrapper — two files, two layers

This directory now has two RTL files at two different layers of the same
interface, plus this README covering both:

1. **`lan9220_if.sv`** (existing phase-0 stub, A1) — the *logical* layer:
   AXI-Stream (from `bridge/`'s uplink port) ⇄ `axi_emc_0`'s AXI4/AXI4-Lite
   memory-mapped side. FIFO push/pop + descriptor sequencing here is
   largely firmware's job (`firmware/smsc911x/`), per its own header
   comment — unchanged by this pass.
2. **`lan9220_emc_wrap.sv`** (new, this pass) — the *pin* layer: a thin,
   purely-combinational wrapper between `axi_emc_0`'s native external-memory
   bank ports and the true top-level board pins (`ETH_*`/`SMBF_*`).
   Resolves **I21** (`docs/contracts/OPEN_ISSUES.md`) by implementing the
   split-o/i/t tristate convention `shell_bd.tcl` already committed to for
   `SMBF_DATA`, matching how `swd_dio`/`mdio` are already split at this
   repo's BD boundary (consistent internal convention, not a new one). See
   the module header for the exact port list and the "External-bus port
   list the harness BD must expose" section below.

## Driver hand-off (firmware side, informational — owned by A3/`W-SMSC`)

Per spec §12: **Zephyr's `eth_smsc911x.c`** (Apache-2.0) is the preferred
driver source, ported to bare-metal MicroBlaze behind the existing HAL seam
— preferred over Linux's `smsc911x` (GPL) per spec §16 licensing guidance.
The Arm MPS3 SMM reference (Corstone SSE-300 AN552 / Cortex-R52 AN536) is a
secondary cross-check since it targets the same physical part against an
Arm-flavoured SMC/EMC block. This RTL directory's job stops at making the
LAN9220's registers and FIFO ports appear as ordinary memory-mapped
half-words at a known AXI address (via `axi_emc_0`); the driver then does:
reset sequencing (respecting `SMBF_nRST` timing), ID/MAC-address readback,
TX/RX FIFO push/pop with the auto-increment FIFO-port addressing, and
interrupt servicing off `ETH_INT`. That work is tracked as workstream
**W-SMSC** in `docs/NEXT_WAVE_PLAN.md`, not this pass.

## External-bus port list the harness BD must expose

This is the contract between this module and the shell BD (`shell_bd.tcl`,
owned by A1/W-BD) — the **complete** top-level port list for the LAN9220
SMC bus. `shell_bd.tcl`'s Section 4 (as of this pass) already declares most
of this; **two ports are missing there** (`smbf_nrst`, `smbf_fifosel`) —
flagged for A1/A6 to add, since the pin table above shows both are real,
required board pins, not optional:

| Top-level BD port (`shell_bd.tcl` `make_bd_port`) | Dir | Width | Status |
|---|---|---|---|
| `eth_ncs` | O | 1 | present |
| `eth_noe` | O | 1 | present |
| `eth_int` | I | 1 | present |
| `smbf_data_o` | O | 16 | present (split-tristate half) |
| `smbf_data_i` | I | 16 | present (split-tristate half) |
| `smbf_data_t` | O | 1 | present (shared output-enable for all 16 bits — matches `mdio_t`/`swd_dio_t` single-bit-OE convention already used elsewhere in this repo's board wrapper style) |
| `smbf_addr` | O | 7 | present |
| `smbf_noe` | O | 1 | present |
| `smbf_nwe` | O | 1 | present |
| `smbf_nrst` | O | 1 | **missing from `shell_bd.tcl` — add** |
| `smbf_fifosel` | O | 1 | **missing from `shell_bd.tcl` — add** |

`lan9220_emc_wrap.sv` is written against this full 11-port list (assuming
the two missing ones land) so the BD and this module don't have to be
re-reconciled twice.
