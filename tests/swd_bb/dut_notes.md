# DUT notes — `swd_bb`

RTL: `fpga/shell/ip/swd_bb/swd_bb.sv`, module `swd_bb` (params
`C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`). Single file, no
includes. **Real RTL from day one** (W-RTL-NEWIP, never a Phase-0 stub),
so `list_benches.py` reports READY on landing. Proven green under VCS
2022.06-SP2 + cocotb 2.0.1, 2026-07-06 (4/4).

Real port list (confirmed by reading the RTL, 2026-07-06):

- `s_axi_*` — standard AXI4-Lite slave, Xilinx-template FSM identical to
  dfx_ctl/dut_clkrst/board_gpio. `AxiLiteMaster.from_dut(dut)` binds
  directly.
- `swd_clk_o`, `swd_dio_o_o`, `swd_dio_oe_o` (O, from `DRIVE`) /
  `swd_dio_i_i` (I, to `SAMPLE`) — partition-pins.md "Processor debug"
  group, shell's view, split-tristate convention.

Registers (shell-regmap.md v0.1 SWDBB table — both offsets match the
contract exactly; constants in `tests/common/regmap.py`):

| Off | Reg | Bits |
|---|---|---|
| 0x00 | `DRIVE` (rw) | [0] swclk, [1] swdio_o, [2] swdio_oe — write-through |
| 0x04 | `SAMPLE` (ro) | [0] swdio_i, 2-FF synchronized live level |

## Behaviour pinned by this bench (incl. documented quirks)

- **Reset drive state `3'b000`** (SWCLK low, SWDIO released) — README
  flag #3's choice; the contract is silent. OpenOCD re-initialises
  outputs on connect, so only the pre-connect window depends on this.
- **2-FF SAMPLE latency** pinned exactly (via the internal
  `dio_i_sync_q` chain — VCS is compiled with `-debug_access+all` by the
  cocotb makefile, so internal peeks work).
- **Aliasing at offsets ≥ 0x08** (README flag #1): only address bit [2]
  is decoded, so 0x08→DRIVE, 0x0C→SAMPLE, 0x10→DRIVE, … for reads AND
  writes. `test_offsets_at_and_above_0x08_alias` asserts this CURRENT
  behaviour deliberately — the README documents it as a known ambiguity
  ("two-line change if firmware ever probes this page"). Note the other
  shell blocks are not actually alias-free either: dfx_ctl/dut_clkrst/
  board_gpio decode 2-3 index bits and alias above *their* windows too
  (mod 0x10/0x20) — swd_bb just aliases sooner. No RTL change made:
  RTL matches its own documented contract; decision stays with A6.

## Discrepancies found

None — the RTL matches shell-regmap.md v0.1 and its own README on every
point this bench checks. No RTL fixes were needed.
