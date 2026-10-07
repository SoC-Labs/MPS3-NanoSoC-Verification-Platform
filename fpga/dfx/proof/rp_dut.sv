// -----------------------------------------------------------------------------
// rp_dut.sv — the reconfigurable-partition CELL as seen by the static shell.
//
// This is a PORT-ONLY module (no body): synthesizing the static shell with this
// instance leaves it a black box, which is exactly the RP cell the DFX flow
// fills with an RM checkpoint (rm_greybox / rm_led) via `read_checkpoint -cell`.
// Its port list is EXACTLY the RP side of docs/contracts/partition-pins.md v0.2
// (matches rm_greybox.sv / rm_led.sv, NGPIO=16), so those RM checkpoints link
// into `u_rp_dut` unchanged.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_dut (
  input  logic        dut_clk,
  input  logic        dut_resetn,
  input  logic        rp_resetn,
  input  logic        dbg_resetn,

  input  logic        jtag_tck,
  input  logic        jtag_tms,
  input  logic        jtag_tdi,
  output logic        jtag_tdo,

  // RM debug — BSCAN to an RM debug hub (partition-pins.md, 2026-10 ILA mint).
  input  logic        dbg_bscan_bscanid_en,
  input  logic        dbg_bscan_capture,
  input  logic        dbg_bscan_drck,
  input  logic        dbg_bscan_reset,
  input  logic        dbg_bscan_runtest,
  input  logic        dbg_bscan_sel,
  input  logic        dbg_bscan_shift,
  input  logic        dbg_bscan_tck,
  input  logic        dbg_bscan_tdi,
  input  logic        dbg_bscan_tms,
  input  logic        dbg_bscan_update,
  output logic        dbg_bscan_tdo,

  input  logic        phy_rmii_ref_clk,
  input  logic        phy_rmii_crs_dv,
  input  logic [1:0]  phy_rmii_rxd,
  output logic [1:0]  phy_rmii_txd,
  output logic        phy_rmii_tx_en,
  output logic        mdc,
  output logic        mdio_o,
  output logic        mdio_oe,
  input  logic        mdio_i,

  output logic [7:0]  uart_tx_tdata,
  output logic        uart_tx_tvalid,
  input  logic        uart_tx_tready,
  input  logic [7:0]  uart_rx_tdata,
  input  logic        uart_rx_tvalid,
  output logic        uart_rx_tready,
  output logic        swo,

  output logic [31:0] rm_id,
  output logic        dut_lockup,
  output logic        irq_out,

  output logic [15:0] dut_gpio_o,
  output logic [15:0] dut_gpio_oe,
  input  logic [15:0] dut_gpio_i,

  // Flash / QSPI XiP boundary (partition-pins.md v0.2) — port-only black box.
  output logic        qspi_sclk,
  output logic        qspi_csn,
  output logic [3:0]  qspi_io_o,
  output logic [3:0]  qspi_io_oe,
  input  logic [3:0]  qspi_io_i
);
  // Intentionally empty — synthesized as a black box; the DFX flow links an
  // RM checkpoint (rm_greybox/rm_led) into this cell.
endmodule
