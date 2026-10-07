// -----------------------------------------------------------------------------
// tb_eth_ss_tx.sv — bench-only composition for the ARM=tx arm: the WHOLE
// rm_eth_ss (rp_eth_ss_wrapper — bring-up FSM + real OpenCores MAC + PTP +
// rmii_to_mii + DMA SRAM, exactly as the OOC synth reads it) plugged into the
// shell's return path as tests/dut_egress/tb_dut_egress.sv builds it
// (eth_mac_test_subsystem + dut_egress, wired as shell_bd.tcl SECTION 5).
//
// Nothing plays the DUT here: the RM is the DUT, unmodified. The bench plays
// only the MicroBlaze (DUTEGR AXI4-Lite). GENCHK and the host ingress are idle.
//
// REARM_CYCLES is shortened by defparam (RTL default 50,000,000 = 1 s at
// 50 MHz; a simulated second of 100 Mb/s MAC is not affordable). Under the
// control build (an FSM without the parameter) the defparam is compiled out.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_eth_ss_tx (
  input  logic        dut_clk,         // 50 MHz DUT clock (shell clk_wiz_dut)
  input  logic        dut_resetn,
  input  logic        refclk_i,        // 50 MHz RMII reference / datapath
  input  logic        rst_i,           // active-HIGH shell datapath reset
  input  logic        s_axi_aclk,      // 100 MHz shell AXI-Lite
  input  logic        s_axi_aresetn,

  // ---- DUTEGR AXI4-Lite (bench plays the MicroBlaze) ---------------------
  input  logic [31:0] s_axi_awaddr,
  input  logic        s_axi_awvalid,
  output logic        s_axi_awready,
  input  logic [31:0] s_axi_wdata,
  input  logic [3:0]  s_axi_wstrb,
  input  logic        s_axi_wvalid,
  output logic        s_axi_wready,
  output logic [1:0]  s_axi_bresp,
  output logic        s_axi_bvalid,
  input  logic        s_axi_bready,
  input  logic [31:0] s_axi_araddr,
  input  logic        s_axi_arvalid,
  output logic        s_axi_arready,
  output logic [31:0] s_axi_rdata,
  output logic [1:0]  s_axi_rresp,
  output logic        s_axi_rvalid,
  input  logic        s_axi_rready,

  // ---- observation ---------------------------------------------------------
  output logic [1:0]  phy_rmii_txd,    // the RM's TX partition pins
  output logic        phy_rmii_tx_en,
  output logic        irq_out
);

`ifndef CONTROL_OLD_FSM
  defparam u_rm.u_bringup.REARM_CYCLES       = 4000;
`endif
  defparam u_rm.u_bringup.START_DELAY_CYCLES = 16;

  logic       phy_rmii_crs_dv;
  logic [1:0] phy_rmii_rxd;
  logic       mdc, mdio_o, mdio_oe;
  logic [31:0] rm_id;

  rp_eth_ss_wrapper u_rm (
    .dut_clk              (dut_clk),
    .dut_resetn           (dut_resetn),
    .rp_resetn            (1'b1),
    .dbg_resetn           (1'b1),
    .jtag_tck             (1'b0),
    .jtag_tms             (1'b0),
    .jtag_tdi             (1'b0),
    .jtag_tdo             (),
    .dbg_bscan_bscanid_en (1'b0),
    .dbg_bscan_capture    (1'b0),
    .dbg_bscan_drck       (1'b0),
    .dbg_bscan_reset      (1'b0),
    .dbg_bscan_runtest    (1'b0),
    .dbg_bscan_sel        (1'b0),
    .dbg_bscan_shift      (1'b0),
    .dbg_bscan_tck        (1'b0),
    .dbg_bscan_tdi        (1'b0),
    .dbg_bscan_tms        (1'b0),
    .dbg_bscan_update     (1'b0),
    .dbg_bscan_tdo        (),
    .phy_rmii_ref_clk     (refclk_i),
    .phy_rmii_crs_dv      (phy_rmii_crs_dv),
    .phy_rmii_rxd         (phy_rmii_rxd),
    .phy_rmii_txd         (phy_rmii_txd),
    .phy_rmii_tx_en       (phy_rmii_tx_en),
    .mdc                  (mdc),
    .mdio_o               (mdio_o),
    .mdio_oe              (mdio_oe),
    .mdio_i               (1'b1),
    .uart_tx_tdata        (),
    .uart_tx_tvalid       (),
    .uart_tx_tready       (1'b1),
    .uart_rx_tdata        (8'h00),
    .uart_rx_tvalid       (1'b0),
    .uart_rx_tready       (),
    .swo                  (),
    .rm_id                (rm_id),
    .dut_lockup           (),
    .irq_out              (irq_out),
    .dut_gpio_o           (),
    .dut_gpio_oe          (),
    .dut_gpio_i           (16'h0000),
    .qspi_sclk            (),
    .qspi_csn             (),
    .qspi_io_o            (),
    .qspi_io_oe           (),
    .qspi_io_i            (4'h0)
  );

  // The shell side, reused verbatim from tests/dut_egress (it mirrors the BD).
  logic mgmt_s_tready;
  logic g_awready, g_wready, g_bvalid, g_arready, g_rvalid;
  logic [1:0] g_bresp, g_rresp;
  logic [31:0] g_rdata;

  tb_dut_egress u_shell (
    .refclk_i        (refclk_i),
    .rst_i           (rst_i),
    .s_axi_aclk      (s_axi_aclk),
    .s_axi_aresetn   (s_axi_aresetn),
    .phy_rmii_crs_dv (phy_rmii_crs_dv),
    .phy_rmii_rxd    (phy_rmii_rxd),
    .phy_rmii_txd    (phy_rmii_txd),
    .phy_rmii_tx_en  (phy_rmii_tx_en),
    .mgmt_s_tdata    (8'h00),
    .mgmt_s_tvalid   (1'b0),
    .mgmt_s_tready   (mgmt_s_tready),
    .mgmt_s_tlast    (1'b0),
    .s_axi_awaddr    (s_axi_awaddr),
    .s_axi_awvalid   (s_axi_awvalid),
    .s_axi_awready   (s_axi_awready),
    .s_axi_wdata     (s_axi_wdata),
    .s_axi_wstrb     (s_axi_wstrb),
    .s_axi_wvalid    (s_axi_wvalid),
    .s_axi_wready    (s_axi_wready),
    .s_axi_bresp     (s_axi_bresp),
    .s_axi_bvalid    (s_axi_bvalid),
    .s_axi_bready    (s_axi_bready),
    .s_axi_araddr    (s_axi_araddr),
    .s_axi_arvalid   (s_axi_arvalid),
    .s_axi_arready   (s_axi_arready),
    .s_axi_rdata     (s_axi_rdata),
    .s_axi_rresp     (s_axi_rresp),
    .s_axi_rvalid    (s_axi_rvalid),
    .s_axi_rready    (s_axi_rready),
    .g_axi_awaddr    (12'h000),
    .g_axi_awvalid   (1'b0),
    .g_axi_awready   (g_awready),
    .g_axi_wdata     (32'h0),
    .g_axi_wstrb     (4'h0),
    .g_axi_wvalid    (1'b0),
    .g_axi_wready    (g_wready),
    .g_axi_bresp     (g_bresp),
    .g_axi_bvalid    (g_bvalid),
    .g_axi_bready    (1'b1),
    .g_axi_araddr    (12'h000),
    .g_axi_arvalid   (1'b0),
    .g_axi_arready   (g_arready),
    .g_axi_rdata     (g_rdata),
    .g_axi_rresp     (g_rresp),
    .g_axi_rvalid    (g_rvalid),
    .g_axi_rready    (1'b1)
  );

endmodule
