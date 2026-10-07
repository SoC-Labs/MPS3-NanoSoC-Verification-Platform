// -----------------------------------------------------------------------------
// tb_link_partner_pair.sv — bench-only wrapper pairing link_partner_mac (the
// MAC half) with rmii_phy_if (the PHY half) exactly as fpga/ethernet/README's
// datapath wires them. Lives in the bench dir, not fpga/ethernet/ (same
// precedent as tests/sim_smoke/counter.sv): it is test scaffolding, not a
// shipped block — the real composition happens in the shell BD.
//
// The bench therefore drives/observes only the OUTER interfaces:
//   * RMII partition pins (bench plays the DUT MAC's role), and
//   * the MAC's AXI-Stream ports (bench plays the bridge's role),
// so every test crosses BOTH modules' conversion logic + the MII pairing
// contract between them (pacing-clock phases, nibble order, preamble/SFD).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_link_partner_pair (
  input  logic       refclk_i,      // 50 MHz (rmii_phy_if requirement)
  input  logic       rst_i,

  // RMII partition pins, shell's view (bench = DUT MAC side)
  output logic       phy_rmii_ref_clk_o,
  output logic       phy_rmii_crs_dv_o,
  output logic [1:0] phy_rmii_rxd_o,
  input  logic [1:0] phy_rmii_txd_i,
  input  logic       phy_rmii_tx_en_i,

  // AXI-Stream toward the bridge (bench = bridge side)
  output logic [7:0] m_axis_rx_tdata,
  output logic       m_axis_rx_tvalid,
  input  logic       m_axis_rx_tready,
  output logic       m_axis_rx_tlast,
  output logic       m_axis_rx_tuser,
  input  logic [7:0] s_axis_tx_tdata,
  input  logic       s_axis_tx_tvalid,
  output logic       s_axis_tx_tready,
  input  logic       s_axis_tx_tlast
);

  // shell-internal MII pairing (rmii_phy_if <-> link_partner_mac)
  logic [3:0] mii_rxd, mii_txd;
  logic       mii_rx_dv, mii_rx_er, mii_rx_clk, mii_tx_en, mii_tx_clk;

  rmii_phy_if u_phy (
    .refclk_i           (refclk_i),
    .rst_i              (rst_i),
    .phy_rmii_ref_clk_o (phy_rmii_ref_clk_o),
    .phy_rmii_crs_dv_o  (phy_rmii_crs_dv_o),
    .phy_rmii_rxd_o     (phy_rmii_rxd_o),
    .phy_rmii_txd_i     (phy_rmii_txd_i),
    .phy_rmii_tx_en_i   (phy_rmii_tx_en_i),
    .mii_rxd_o          (mii_rxd),
    .mii_rx_dv_o        (mii_rx_dv),
    .mii_rx_er_o        (mii_rx_er),
    .mii_rx_clk_o       (mii_rx_clk),
    .mii_txd_i          (mii_txd),
    .mii_tx_en_i        (mii_tx_en),
    .mii_tx_clk_o       (mii_tx_clk)
  );

  link_partner_mac u_mac (
    .clk_i            (refclk_i),   // single 50 MHz domain (see MAC header)
    .rst_i            (rst_i),
    .mii_rxd_i        (mii_rxd),
    .mii_rx_dv_i      (mii_rx_dv),
    .mii_rx_er_i      (mii_rx_er),
    .mii_rx_clk_i     (mii_rx_clk),
    .mii_txd_o        (mii_txd),
    .mii_tx_en_o      (mii_tx_en),
    .mii_tx_clk_i     (mii_tx_clk),
    .m_axis_rx_tdata  (m_axis_rx_tdata),
    .m_axis_rx_tvalid (m_axis_rx_tvalid),
    .m_axis_rx_tready (m_axis_rx_tready),
    .m_axis_rx_tlast  (m_axis_rx_tlast),
    .m_axis_rx_tuser  (m_axis_rx_tuser),
    .s_axis_tx_tdata  (s_axis_tx_tdata),
    .s_axis_tx_tvalid (s_axis_tx_tvalid),
    .s_axis_tx_tready (s_axis_tx_tready),
    .s_axis_tx_tlast  (s_axis_tx_tlast)
  );

endmodule
