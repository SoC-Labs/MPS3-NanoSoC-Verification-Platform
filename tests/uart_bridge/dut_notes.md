# DUT notes — `uart_bridge`

RTL: `fpga/shell/ip/uart_bridge/`, module `uart_bridge` (params
`C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`, `FIFO_DEPTH=16`).
**Three files, single-file build**: the top `include`s
`uartbr_async_fifo.sv` (gray-pointer dual-clock FIFO, 5 instances) and
`swo_uart_rx.sv` (8N1 NRZ deserialiser), so the Makefile lists only
`uart_bridge.sv` plus `+incdir+$(RTL_DIR)` — same pattern as
`tests/mdio_phy_model/Makefile`. `list_benches.py` gates readiness on all
three files. **Real RTL from day one** (W-RTL-NEWIP, never a Phase-0
stub). Proven green under VCS 2022.06-SP2 + cocotb 2.0.1, 2026-07-06
(5/5) — first-ever simulation of this block.

Real port list (confirmed by reading the RTL, 2026-07-06):

- `s_axi_*` — standard AXI4-Lite slave (Xilinx-template FSM).
- `dut_clk_i` — DUT clock-domain tap; the bench runs it at 7 ns against a
  10 ns `s_axi_aclk` so every byte genuinely crosses the CDC.
- UART0 partition pins: `uart_tx_tdata_i[7:0]`/`uart_tx_tvalid_i`/
  `uart_tx_tready_o` (AXIS in, DUT→host), `uart_rx_tdata_o[7:0]`/
  `uart_rx_tvalid_o`/`uart_rx_tready_i` (AXIS out, host→DUT), `swo_i`.
- UART1 reserved seam: `uart1_tx_*`/`uart1_rx_*` — NOT partition pins in
  v0.1; the bench drives them directly (nothing else will).

Registers (constants in `tests/common/regmap.py`): `U0_TX/U0_RX`@0x00,
`U1_TX/U1_RX`@0x08, `SWO_RX`@0x10(ro), `FIFO_STATUS`@0x14(ro),
`SWO_CFG`@0x18. **Data-window reads are DESTRUCTIVE** (each read pops one
byte; `{valid=0, 0}` + no pop when empty) — the bench never re-reads a
data window to "confirm" a value.

## Behaviour pinned by this bench (incl. documented quirks)

- **FIFO_STATUS bit layout** (RTL ambiguity #4, this file's choice —
  needs codifying into regmap v0.2): [0] u0_tx_full, [1] u0_rx_empty,
  [2] u1_tx_full, [3] u1_rx_empty, [4] swo_rx_empty. Reset value 0x1A.
- **Loss policies**: DUT→host lossless (tready backpressure, tvalid/tdata
  hold proven); host→DUT push-while-full silently dropped (two dropped
  bytes proven absent from the drained stream); SWO drop-newest + sticky
  overflow.
- **SWO_CFG @0x18 is NOT in shell-regmap.md v0.1** (RTL ambiguity #2 —
  RTL-added per the deliverable brief; regmap.py carries the same
  caveat). Layout pinned: [31] frame_err (sticky ro), [30] overflow
  (sticky ro), [16] enable, [15:0] divisor; bit period =
  (divisor+1)·dut_clk; stickies clear while enable=0; divisor changes
  need the documented enable off/on toggle (proven at div 7 → 9).
- **TX write bit [8] ignored** (ambiguity #3): writes push
  unconditionally; the regmap's "[8] valid" is read-side only. The bench
  writes bare bytes.

## Discrepancies found (documented, resolved bench-side — no RTL change)

1. **README "Unmapped offsets ≥ 0x1C read 0" is only true inside the
   32-byte window.** Only address bits [4:2] decode (`waddr_idx`/
   `raddr_idx`), so offsets ≥ 0x20 alias mod 0x20 — e.g. a READ of 0x20
   aliases U0_RX and would **destructively pop a console byte**. This is
   the house decode convention (dfx_ctl/dut_clkrst/board_gpio/telem all
   alias above their own windows the same way), so the bench pins the
   in-window behaviour (0x04/0x0C/0x1C read 0) and does NOT demand
   read-0 above 0x20; but the destructive-read aliasing is worth an A6
   look (either widen the decode or fix the README wording in v0.2 —
   firmware must not probe this page). No RTL change made: convention-
   consistent, and the block's own ambiguity list already routes decode
   questions to A6.

## Bench-side infrastructure findings (shared code, fixed)

- `tests/common/regmap.py` `AxiLiteMaster.read()`: zero-gap back-to-back
  reads returned the PREVIOUS address's data against the Xilinx-template
  FSM (phantom re-armed ARREADY captures the old ARADDR at the retire
  edge). Fixed in the shared BFM (one trailing idle edge); see the
  comment in `read()`. Found via tests/swd_bb, affects every bench.
- AXIS monitor sampling: a beat must be sampled in the ReadOnly window
  BEFORE the committing edge — post-edge sampling sees the FWFT FIFO's
  post-pop state and drops the first byte. This bench's `_axis_collect`
  does it right; `tests/common/axis.py`'s `AxisByteMonitor` still uses
  post-edge sampling and its own header already flags its beat timing as
  unproven — revalidate before gen_checker/bridge unskip.
