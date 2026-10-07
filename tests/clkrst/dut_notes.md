# DUT notes — `clkrst`

RTL: `fpga/shell/ip/clkrst/dut_clkrst.sv`, module `dut_clkrst` (params
`C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`, `C_DRP_ADDR_WIDTH=7`,
`C_DRP_DATA_WIDTH=16`). Real port list (confirmed by reading the RTL,
2026-07-04):

- `s_axi_*` — AXI4-Lite slave, CLKRST regmap (`RESET_CTRL`@0x00,
  `DUT_CLK_SEL`@0x04, `DUT_CLK_DRP`@0x08, `STATUS`@0x0C ro — all
  real/confirmed offsets).
- DRP master toward the `clk_wiz_dut` BD cell: `drp_den_o`/`drp_dwe_o`/
  `drp_daddr_o`/`drp_di_o` (O) / `drp_do_i`/`drp_drdy_i`/`mmcm_locked_i` (I).
- `dut_clk_i` (I) — DUT-domain clock tap, for the `dut_clk_alive` heartbeat.
- `ext_por_n_i` (I) — board-level power-on reset (async-assert source).
- `rp_resetn_gate_i` (I) — **from `dfx_ctl.sv`**, ANDed into `rp_resetn_o`
  (the cross-block composition this bench's `dfx_ctl` sibling exercises
  from the other side).
- `dbg_reset_req_i` (I) — from the not-yet-stubbed `swd_probe` block
  (OpenOCD srst).
- `dut_resetn_o`/`rp_resetn_o`/`dbg_resetn_o` (O) — the three partition-pin
  resets (partition-pins.md lines 28-31).

Status (refreshed 2026-07-08, A5 verification pass): **REAL, synthesizable
AXI4-Lite slave — graduated from the A1 Phase-0 stub.** The whole block is
now real logic: the standard Xilinx-template AXI-Lite write/read FSM, the
register file (`RESET_CTRL`/`DUT_CLK_SEL`/`DUT_CLK_DRP`), and — the part the
old note said was still `// TODO(A1)` — the three reset generators are now
full async-assert / sync-deassert 3-FF Cummings synchronizers clocked by
`dut_clk_i` (async source = `ext_por_n_i & reset_ctrl_q[n] & <gate>`). The
bench (`test_clkrst.py`) clocks `dut_clk_i` and waits settle cycles on every
release path accordingly. `rtl_ready()` reports READY.

Covered by the bench: RESET_CTRL per-reset assert/release, `ext_por_n_i`
async override, the `rp_resetn_gate_i` cross-block composition,
`dbg_reset_req_i`, STATUS.mmcm_locked, WSTRB per-lane decode on DUT_CLK_DRP,
DUT_CLK_SEL/DRP register readback, and the STATUS.dut_clk_alive heartbeat.

### Findings for the RTL owner (A1/A6) — from the A5 verification pass

1. **DRP master is a documented tied-idle placeholder.** `drp_den_o`/
   `drp_dwe_o`/`drp_daddr_o`/`drp_di_o` are hard-tied idle (RTL header says so
   explicitly), so DUT_CLK_SEL/DUT_CLK_DRP writes are stored in registers but
   NEVER issue a DRP transaction — the DUT clock is not actually
   reconfigured. `test_dut_clk_sel_drp_writes_and_drp_master_is_idle` attaches
   a DRP-responder model and PINS that `drp_den_o` stays 0 / the responder is
   never triggered; it will need updating the day A1 wires a real preset-ROM +
   DRP FSM.
2. **`dutclk_toggle_q` (the dut_clk_alive heartbeat generator flop) has no
   reset / no initial value**, so in 4-state VCS simulation it powers up to X
   and stays X forever (`~x == x`) — the whole dut_clk_alive feature is dead in
   sim. On a Xilinx FPGA global-set-reset inits it to 0 (works in hardware);
   `test_dut_clk_alive_heartbeat_tracks_dut_clk` mirrors that GSR init with a
   one-time bench deposit so the real liveness logic can be exercised.
   Recommend an explicit reset (or `= 1'b0` init) so sim matches silicon.
