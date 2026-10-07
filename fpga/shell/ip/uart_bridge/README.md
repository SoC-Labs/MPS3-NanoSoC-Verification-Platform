# `uart_bridge` — UARTBR console-stream bridge (AXIS ⇄ register FIFOs)

**Real, synthesizable RTL** (new block, `W-RTL-NEWIP` — regmap v0.1 I7).
The AXIS ⇄ MicroBlaze-FIFO bridge for the three console streams the
`uart_over_eth` firmware relays to TCP 6930/6931/6932: DUT UART0 (boot
monitor), DUT UART1 (application — present but unused for single-core),
and SWO/ITM trace. **This block is where the partition-boundary CDC for
the console group lives** (`partition-pins.md` clock/reset-domain rule:
"shell owns the synchronizers/async FIFOs").

## Module list

| File | Module | Role |
|---|---|---|
| `uart_bridge.sv` | `uart_bridge` (top) | AXI4-Lite front-end for the `UARTBR` regmap, the five FIFO instantiations, the SWO enable/divisor CDC, and the partition-pin AXIS wiring. Binds to the shell's AXI-Lite fabric and (for U0/SWO) the partition pins. |
| `uartbr_async_fifo.sv` | `uartbr_async_fifo` | Dual-clock gray-pointer FIFO (canonical Cummings pattern: 2-FF-synchronized gray pointers, registered conservative full/empty, FWFT head). One per stream direction, `dut_clk` ⇄ `s_axi_aclk`. `WIDTH=8`, `DEPTH` = the top's `FIFO_DEPTH` (default 16; power of two, ≥ 4). |
| `swo_uart_rx.sv` | `swo_uart_rx` | 8N1 (UART/NRZ-mode) deserialiser for the raw 1-bit `swo` partition pin: 2-FF input sync, half-bit start-bit validation, mid-bit sampling, LSB-first, stop-bit judgment. Bit period = `divisor+1` `dut_clk` cycles. |

Build note: like `mdio_phy_model.sv`, the top pulls the two helpers in via
`` `include `` (resolved relative to its own directory), so a bench
Makefile can list just `uart_bridge.sv` as `VERILOG_SOURCES`. All three
modules stay independently lintable/instantiable.

## Register map (`UARTBR` @ `0x44A9_0000`, shell-regmap.md v0.1)

| Off | Reg | Access | Bits | Behaviour |
|---|---|---|---|---|
| 0x00 | `U0_TX` / `U0_RX` | W / R | `[7:0]` data, `[8]` valid | **W:** push `wdata[7:0]` into the U0 host→DUT FIFO (silently dropped if `tx_full` — poll `FIFO_STATUS` first; `wdata[8]` ignored, see flag #3). **R:** pop one byte from the U0 DUT→host FIFO — `{valid=1, byte}`, or `{valid=0, 0x00}` + no pop when empty. **Reads are destructive.** |
| 0x04 | — | — | — | reserved gap in the contract; reads 0 |
| 0x08 | `U1_TX` / `U1_RX` | W / R | `[7:0]` data, `[8]` valid | identical to 0x00 for UART1 (fully implemented; DUT-side seam tied off in v0 — see flag #5) |
| 0x0C | — | — | — | reserved gap; reads 0 |
| 0x10 | `SWO_RX` | R | `[7:0]` data, `[8]` valid | pop one deserialised SWO trace byte (read-only; a write is accepted-no-effect) |
| 0x14 | `FIFO_STATUS` | R | `[0]` u0_tx_full, `[1]` u0_rx_empty, `[2]` u1_tx_full, `[3]` u1_rx_empty, `[4]` swo_rx_empty | live flags, each naturally registered in the `s_axi_aclk` domain (write-side full / read-side empty of the respective FIFO). Bit positions are this file's choice — flag #4. |
| 0x18 | `SWO_CFG` | RW | `[15:0]` divisor, `[16]` enable; RO status `[30]` overflow (sticky), `[31]` frame_err (sticky) | **added at a spare offset per the deliverable brief — not in regmap v0.1** (flag #2). Bit period = `divisor+1` `dut_clk` cycles; `divisor ≥ 7` recommended (≥ 8× oversampling), ≥ 3 absolute minimum. Sticky bits clear while `enable=0`. |

Unmapped offsets ≥ 0x1C read 0 **and pop nothing**; all writes to
RO/unmapped offsets are accepted at the protocol level (`BRESP=OKAY`, no
effect) — same convention as `dfx_ctl.sv`/`dut_clkrst.sv`/`board_gpio.sv`.

> **RESOLVED 2026-07-09 — silent data-loss fix.** The address decode now
> spans the **full local word address** (`addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]`).
> *Previously only `addr[4:2]` was decoded*, so offsets ≥ 0x20 aliased onto
> the mapped windows (0x20 → `U0_TX`/`U0_RX`, …). Because `U0_RX`/`U1_RX`/
> `SWO_RX` are **destructive-read FIFO ports**, an aliased **read** of an
> "unmapped" offset such as 0x20 did **not** merely return a wrong value — it
> **popped the U0 RX FIFO**, silently losing a byte of DUT console/trace data
> on stray or speculative bus traffic. The fix gates every FIFO pop on the
> full-width decoded, mapped register select, so `raddr_idx == IDX_U0_DATA`
> is false for any unmapped offset and no FIFO `rd_en` can assert outside a
> genuinely-mapped data-window read. A **mapped** read still pops exactly as
> before (bit-for-bit unchanged); an **unmapped** read now pops nothing and
> returns 0. Flagged by the SystemRDL decode-equivalence gate
> (`poc/systemrdl`), which named this window as the destructive-read hazard.

Register-naming decode: the regmap names registers from the **MicroBlaze's**
perspective (`U0_TX` = MB transmits toward the DUT) while the partition pins
are named from the **DUT's** (`uart_tx_*` = DUT transmits). So `U0_TX`
writes emerge on the DUT's `uart_rx_*` AXIS input, and the DUT's `uart_tx_*`
output lands in the `U0_RX` window. The crossover happens once, here.

## Port groups (see `uart_bridge.sv`)

1. **AXI4-Lite slave** — `UARTBR` regmap, standard `s_axi_*` naming
   (identical FSM to the other shell IP blocks).
2. **`dut_clk_i`** — the DUT clock-domain tap (same convention as
   `dut_clkrst.sv`); every partition-pin-facing FIFO side runs on it.
3. **UART0 partition pins** (`partition-pins.md` console/trace group,
   shell's view, pin-name + direction suffix): `uart_tx_tdata_i[7:0]` /
   `uart_tx_tvalid_i` / `uart_tx_tready_o` (AXIS in), `uart_rx_tdata_o[7:0]`
   / `uart_rx_tvalid_o` / `uart_rx_tready_i` (AXIS out), `swo_i` (raw
   1-bit trace line).
4. **UART1 reserved seam** — `uart1_tx_*` / `uart1_rx_*`, symmetric with
   UART0 but **not partition pins in v0.1** (single-core I1 boundary). The
   BD ties `uart1_tx_tvalid_i = 0` and `uart1_rx_tready_i = 0` until the
   multicore RM widens the contract.

Parameter: `FIFO_DEPTH` (default 16) — per-stream FIFO depth, power of two,
≥ 4.

## Clock domains / CDC

- **Five gray-pointer async FIFOs** carry all payload across
  `dut_clk` ⇄ `s_axi_aclk`: U0 tx/rx, U1 tx/rx, SWO rx. No console byte
  ever crosses the boundary outside a FIFO.
- **DUT-domain reset** = `s_axi_aresetn` async-asserted / sync-deasserted
  into `dut_clk` (3-FF, Cummings pattern). Both FIFO sides reset from the
  same source, so pointer state can never split-brain across a reset. The
  bridge deliberately does **not** reset on `dut_resetn`/`rp_resetn` —
  console bytes survive a DUT reset/swap (drain policy is the coordinator
  firmware's; flag #6).
- **SWO_CFG → `dut_clk`**: enable level through a 2-FF synchronizer; the
  16-bit divisor is captured in the `dut_clk` domain on the synchronized
  enable's rising edge (data-with-qualifier — no tearing possible).
  Consequence: **changing the divisor requires toggling enable off/on.**
- **Sticky SWO status → `s_axi_aclk`**: frame_err/overflow are slow sticky
  levels, plain 2-FF back-synchronized for `SWO_CFG` readback.
- **Backpressure/loss policy per stream:** U0/U1 DUT→host: lossless
  (`tready` deasserts while full — the DUT's `cmsdk_apb_usrt` stalls).
  U0/U1 host→DUT: push-while-full dropped (firmware polls
  `FIFO_STATUS.tx_full`). SWO: drop-newest on overrun + sticky overflow
  bit (trace has no backpressure to give).

## What's real vs seamed

- **Real:** everything above — AXI-Lite FSM, all five CDC FIFOs, the U0
  AXIS plumbing, the full U1 register/FIFO path, the NRZ SWO deserialiser
  with programmable divisor.
- **Seamed:** (a) the **U1 DUT side** — ports exist and work, but nothing
  in the v0.1 pin contract drives them (BD tie-off until multicore);
  (b) **SWO Manchester mode** — not implemented; `swo_uart_rx.sv` is
  UART/NRZ-mode only, and a Manchester decoder would be a drop-in sibling
  module behind the same `byte + valid` interface.

## Verification

`verilator --lint-only` passes clean on all three files (standalone for
`uartbr_async_fifo.sv`/`swo_uart_rx.sv`; `uart_bridge.sv` from inside this
directory so its `` `include ``s resolve — same invocation pattern as
`mdio_phy_model`).

Bench notes for A5 (no `tests/uart_bridge/` exists yet — benches are
A5's deliverable, plan row W-RTL-NEWIP):

- Clock both `s_axi_aclk` **and** `dut_clk_i` (unrelated periods, e.g.
  10 ns / 7 ns, to make the CDC earn its keep), and hold reset ≥ 3 cycles
  of the slower clock so the dut-domain reset synchronizer releases.
- U0 loop: AXIS-drive `uart_tx_*` (dut side) → poll `FIFO_STATUS[1]` →
  read `U0_RX`, check `[8]` valid + data + order; write `U0_TX` → check
  bytes emerge on `uart_rx_*` in order with proper `tvalid/tready`
  handshakes. Fill-to-full both directions: check `tready` backpressure
  (DUT→host) and the documented drop-on-full (host→TX) — after draining, a
  read of an empty window must return `[8]=0`, data 0, and NOT disturb the
  FIFO.
- Destructive reads: every read of 0x00/0x08/0x10 pops — the bench must
  not re-read to "confirm" a value.
- SWO: write `SWO_CFG` divisor+enable in one 32-bit write; drive `swo_i`
  as an idle-high 8N1 serial line at `(divisor+1)`·`dut_clk` bit period;
  check bytes, then a broken stop bit (→ `[31]` sticky, byte discarded)
  and > FIFO_DEPTH un-drained bytes (→ `[30]` sticky); check both clear
  after enable→0.
- U1 registers behave identically to U0 with the bench driving the
  `uart1_*` seam directly (nothing else will, in v0).
- Suggested `tests/common/list_benches.py` entry:
  `"uart_bridge": (_p("fpga","shell","ip","uart_bridge"), ["uart_bridge.sv"])`
  (file carries no stub marker, so `rtl_ready()` reports READY on landing;
  the Makefile needs `+incdir+`/cwd handling for the includes — copy
  `tests/mdio_phy_model/Makefile`'s pattern).

## TODO(A1) / flags for A6

(Duplicated as the ambiguity block at the bottom of `uart_bridge.sv`.)

1. **SWO capture mode**: UART/NRZ only. Confirm the nanosoc/CMSDK TPIU
   trace path is configured for NRZ (Manchester would need a sibling
   decoder module — do not build until confirmed needed).
2. **`SWO_CFG` @ 0x18 is not in regmap v0.1** — codify
   `{[31] frame_err(ro), [30] overflow(ro), [16] enable, [15:0] divisor}`
   into v0.2 (the brief explicitly asked for the added divisor register),
   or strike it and fix the divisor at build time.
3. **`[8]` on the TX write side** read as read-side-only (writes push
   unconditionally). One-line change if A6 wants write-qualified pushes.
4. **`FIFO_STATUS` bit positions** are this file's choice — codify into
   v0.2.
5. **`uart1_*` are not partition pins in v0.1** — BD must tie them off;
   contract widens only with the multicore RM (I1 deferral).
6. **No hardware FIFO flush** across DUT resets/swaps (bridge resets with
   the shell only). If a flush is wanted, add a `FIFO_CTRL` register via a
   regmap change rather than wiring `rp_resetn` in.
