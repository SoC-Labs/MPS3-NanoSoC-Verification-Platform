`timescale 1ns / 1ps
// rm_regdemo_a — DFX register-difference demo RM (variant A).
// Identical to rm_regdemo_b except rm_id + the dut_gpio_o pattern.
// Ports EXACTLY the RP side of docs/contracts/partition-pins.md v0.1
// (mirror of fpga/dfx/proof/rp_dut.sv), so it links into u_rp_dut unchanged.
module rm_regdemo_a #(
  parameter int NGPIO = 16
) (
  input  logic dut_clk,
  input  logic dut_resetn,
  input  logic rp_resetn,
  input  logic dbg_resetn,

  input  logic jtag_tck,
  input  logic jtag_tms,
  input  logic jtag_tdi,
  output logic jtag_tdo,

  input  logic dbg_bscan_bscanid_en,
  input  logic dbg_bscan_capture,
  input  logic dbg_bscan_drck,
  input  logic dbg_bscan_reset,
  input  logic dbg_bscan_runtest,
  input  logic dbg_bscan_sel,
  input  logic dbg_bscan_shift,
  input  logic dbg_bscan_tck,
  input  logic dbg_bscan_tdi,
  input  logic dbg_bscan_tms,
  input  logic dbg_bscan_update,
  output logic dbg_bscan_tdo,

  input  logic       phy_rmii_ref_clk,
  input  logic       phy_rmii_crs_dv,
  input  logic [1:0] phy_rmii_rxd,
  output logic [1:0] phy_rmii_txd,
  output logic       phy_rmii_tx_en,
  output logic       mdc,
  output logic       mdio_o,
  output logic       mdio_oe,
  input  logic       mdio_i,

  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,
  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready,
  output logic       swo,

  output logic [31:0] rm_id,
  output logic        dut_lockup,
  output logic        irq_out,

  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // Flash / QSPI XiP (partition-pins.md v0.2, NON-CDC source-synchronous
  // exception). Tie-off RM: drive the flash to safe idle (deselected).
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i
);
  // --- the ONLY two things that differ between A and B -----------------------
  // Identity register: read back at DFXCTL.RM_ID (0x44A1_0010), shell-regmap.md.
  // Encoding v2 (VERSIONING_PLAN.md §3.2): {major[31:24], minor[23:16],
  // design_id[15:0]}. design_id 0xA1 UNCHANGED, version 1.0 => 0x010000A1.
  localparam logic [31:0] RM_ID_REGDEMO_A = 32'h0100_00A1;
  // GPIO output register -> board LEDs (proof LED[3:0] = low nibble 0x5).
  localparam logic [7:0]  GPIO_PATTERN_A  = 8'hA5;

  assign rm_id = RM_ID_REGDEMO_A;

  always_comb begin
    dut_gpio_o        = '0;
    dut_gpio_oe       = '0;
    dut_gpio_o[7:0]   = GPIO_PATTERN_A;   // driven pattern
    dut_gpio_oe[7:0]  = 8'hFF;            // own the low byte (LED bits)
  end

  // --- inert tie-offs, identical to rm_greybox.sv ----------------------------
  assign jtag_tdo       = 1'b0;
  assign dbg_bscan_tdo  = 1'b0;
  assign phy_rmii_txd   = 2'b00;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc            = 1'b0;
  assign mdio_o         = 1'b0;
  assign mdio_oe        = 1'b0;
  assign uart_tx_tdata  = 8'h00;
  assign uart_tx_tvalid = 1'b0;
  assign uart_rx_tready = 1'b0;
  assign swo            = 1'b0;
  assign dut_lockup     = 1'b0;
  assign irq_out        = 1'b0;
  // QSPI flash: safe idle (deselected, clock low, lanes tri-stated).
  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;
endmodule
