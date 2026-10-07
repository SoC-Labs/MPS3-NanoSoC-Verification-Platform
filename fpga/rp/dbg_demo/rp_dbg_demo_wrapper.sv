// -----------------------------------------------------------------------------
// rp_dbg_demo_wrapper.sv -- rm_dbg_demo: the smallest RM that carries an ILA
// reachable over XVC. The B2 proof vehicle of the ILA mint
// (docs/planning/ILA_MINT_PLAN_2026-09-23.md §2 decision 4; handover §4.6).
//
// WHAT IT DOES
//   * a 16-bit free-running counter on dut_clk;
//   * one ILA (`ila_dbg_demo`, clk = dut_clk, depth 1024):
//       probe0 = counter[15:0]
//       probe1 = a 1-cycle tick when counter[7:0] == 0 (every 256 cycles)
//     so a capture is self-checking: probe0 increments by exactly 1 per
//     sample, and probe1 fires exactly when probe0[7:0] wraps;
//   * a visible sign of life on the SAME legs rm_led drives: dut_gpio_o[3:0]
//     (oe[3:0] = 1) blink from the top of a 26-bit counter;
//   * rp_dbg_hub (debug_bridge mode 1 + xsdbm) on phy_rmii_ref_clk, fed by the
//     12 dbg_bscan_* partition pins; the ILA joins its hub at opt_design (F4).
//
// Every other output is tied to the decoupler's safe-idle clamp value, as
// the generated skeleton (fpga/shell/generated/rp_wrapper_skeleton.sv) says.
//
// rm_id: design_id 0x0009 @ v1.0 => 0x01000009 (fpga/dfx/rm_list.tcl derives it;
// scripts/harness_gates/check_rm_id_encoding.py holds this localparam to it).
//
// IP: the ILA and the bridge are created by fpga/rp/dbg_demo/ooc_synth.tcl through
// fpga/rp/common/dbg_ip.tcl -- this RM is synth_mode "prebuilt" (inline RMs
// cannot carry IP, handover F14).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_dbg_demo_wrapper #(
  parameter int NGPIO      = 16,
  parameter int BLINK_W    = 26,   // 50 MHz / 2^26 ~ 0.75 Hz on the top bit
  parameter int BLINK_BITS = 4     // same four legs rm_led drives
) (
  input  logic        dut_clk,
  input  logic        dut_resetn,
  input  logic        rp_resetn,
  input  logic        dbg_resetn,

  input  logic        jtag_tck,
  input  logic        jtag_tms,
  input  logic        jtag_tdi,
  output logic        jtag_tdo,

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

  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  output logic        qspi_sclk,
  output logic        qspi_csn,
  output logic [3:0]  qspi_io_o,
  output logic [3:0]  qspi_io_oe,
  input  logic [3:0]  qspi_io_i
);

  // --- identity ---------------------------------------------------------------
  localparam logic [31:0] RM_ID_DBG_DEMO = 32'h0100_0009;
  assign rm_id = RM_ID_DBG_DEMO;

  // --- the thing the ILA looks at --------------------------------------------
  // Free-running (no reset): it must count through a DUT reset so a capture
  // taken at any moment shows a clean +1 ramp.
  logic [15:0] counter = '0;
  always_ff @(posedge dut_clk) counter <= counter + 16'd1;

  logic tick;
  assign tick = (counter[7:0] == 8'h00);

  ila_dbg_demo u_ila_dbg_demo (   // distinct per debug RM: the ILA uuid follows this name (findings #6; ltx_sidecar.py gate)
    .clk    (dut_clk),
    .probe0 (counter),
    .probe1 (tick)
  );

  // --- the RM's debug hub (xsdbm) on the always-on clock ---------------------
  rp_dbg_hub u_dbg_hub (
    .clk                  (phy_rmii_ref_clk),
    .dbg_bscan_bscanid_en (dbg_bscan_bscanid_en),
    .dbg_bscan_capture    (dbg_bscan_capture),
    .dbg_bscan_drck       (dbg_bscan_drck),
    .dbg_bscan_reset      (dbg_bscan_reset),
    .dbg_bscan_runtest    (dbg_bscan_runtest),
    .dbg_bscan_sel        (dbg_bscan_sel),
    .dbg_bscan_shift      (dbg_bscan_shift),
    .dbg_bscan_tck        (dbg_bscan_tck),
    .dbg_bscan_tdi        (dbg_bscan_tdi),
    .dbg_bscan_tms        (dbg_bscan_tms),
    .dbg_bscan_update     (dbg_bscan_update),
    .dbg_bscan_tdo        (dbg_bscan_tdo)
  );

  // --- sign of life: rm_led's legs --------------------------------------------
  logic [BLINK_W-1:0] blink_counter;
  always_ff @(posedge dut_clk or negedge dut_resetn) begin
    if (!dut_resetn) blink_counter <= '0;
    else             blink_counter <= blink_counter + 1'b1;
  end

  always_comb begin
    dut_gpio_o                  = '0;
    dut_gpio_o[BLINK_BITS-1:0]  = blink_counter[BLINK_W-1 -: BLINK_BITS];
    dut_gpio_oe                 = '0;
    dut_gpio_oe[BLINK_BITS-1:0] = {BLINK_BITS{1'b1}};
  end

  // --- every other output: the decoupler's safe-idle clamp value --------------
  assign jtag_tdo       = 1'b0;
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
  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;      // flash DESELECTED -- not 0
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;

endmodule
