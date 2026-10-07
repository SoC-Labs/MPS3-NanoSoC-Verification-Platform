// -----------------------------------------------------------------------------
// tb_dut_egress.sv — bench-only wrapper pairing the §8 virtual-PHY subsystem
// (fpga/ethernet/eth_mac_test_subsystem.sv) with the DUT-egress capture block
// (fpga/shell/ip/dut_egress/dut_egress.sv), wired EXACTLY as shell_bd.tcl
// SECTION 5 wires them.
//
// THIS WRAPPER MIRRORS THE BD. That is its whole purpose: the thing that has
// gone wrong on this platform is not per-block RTL, it is INTEGRATION (SECTION
// 5 shipped the DUT's RX tied to a constant zero; the bridge's management
// egress ships drained into a constant today). So the bench must elaborate the
// composition, not the block. Keep the four correspondences below true:
//
//   BD (fpga/shell/bd/shell_bd.tcl SECTION 5)      here
//   ------------------------------------------     -----------------------
//   eth_mac_test_subsystem_0                       u_ss
//   dut_egress_0, C_S_AXI_ADDR_WIDTH = 32          u_egress (same override)
//   mgmt_m_* -> dut_egress_0/frm_*                 the mgmt_m_* nets below
//   dut_egress_0/inj_m_* -> mgmt_s_*  (§5 DELTA)   the inj_m_* nets below
//   uplink SAFE-TIED, uplink_m_tready = 1          tied here identically
//
// THE §5 BD DELTA IS ALREADY APPLIED HERE (2026-09-23,
// docs/planning/HANDOVER_DUT_INJECT.md §5, supplied to the SHELL lane as
// fpga/shell/ip/dut_egress/INTEGRATION/01-shell_bd-section5-inject.patch):
// `mgmt` leaves the *_s_* tie loop and the bridge's port-B INGRESS is driven by
// dut_egress_0's inject port. The shipped shell_bd.tcl still ties mgmt_s_* off
// until SHELL applies that patch — this bench is the composition AFTER it.
//
// The mgmt_s_* PORTS of this wrapper are therefore VESTIGIAL. They are kept
// only so tests/eth_ss_bringup/tb_eth_ss_tx.sv (which instantiates this
// wrapper and ties them off) still elaborates. The three inputs are NOT
// connected to anything — a bench that could inject behind DUTEGR's back would
// not be the BD — and driving mgmt_s_tvalid high is a fatal error below.
// mgmt_s_tready now reports the bridge's real port-B ingress ready.
//
// The 100 MHz / 50 MHz split is genuine: dut_egress's AXI4-Lite surface runs on
// s_axi_aclk while its capture port runs on refclk_i, so every run exercises
// the real dual-clock FIFO rather than a same-clock shortcut.
//
// The bench plays: the DUT MAC (on the RMII partition pins) and the MicroBlaze
// (on the DUTEGR + GENCHK AXI-Lite surfaces). The host reaches the bridge only
// the way it will on silicon: through DUTEGR.TX_DATA / TX_CTRL.COMMIT.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_dut_egress (
  // ---- clocks / resets (driven by cocotb) --------------------------------
  input  logic        refclk_i,        // 50 MHz RMII reference / datapath
  input  logic        rst_i,           // active-HIGH datapath reset
  input  logic        s_axi_aclk,      // 100 MHz shell AXI-Lite
  input  logic        s_axi_aresetn,   // active-LOW

  // ---- RMII partition pins, shell's view (bench plays the DUT MAC) -------
  output logic        phy_rmii_crs_dv,
  output logic [1:0]  phy_rmii_rxd,
  input  logic [1:0]  phy_rmii_txd,
  input  logic        phy_rmii_tx_en,

  // ---- VESTIGIAL (see header): port-B ingress is DUTEGR's inject port now
  input  logic [7:0]  mgmt_s_tdata,
  input  logic        mgmt_s_tvalid,
  output logic        mgmt_s_tready,
  input  logic        mgmt_s_tlast,

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

  // ---- GENCHK AXI4-Lite (refclk domain — gen_checker fuses its clocks) ---
  input  logic [11:0] g_axi_awaddr,
  input  logic        g_axi_awvalid,
  output logic        g_axi_awready,
  input  logic [31:0] g_axi_wdata,
  input  logic [3:0]  g_axi_wstrb,
  input  logic        g_axi_wvalid,
  output logic        g_axi_wready,
  output logic [1:0]  g_axi_bresp,
  output logic        g_axi_bvalid,
  input  logic        g_axi_bready,
  input  logic [11:0] g_axi_araddr,
  input  logic        g_axi_arvalid,
  output logic        g_axi_arready,
  output logic [31:0] g_axi_rdata,
  output logic [1:0]  g_axi_rresp,
  output logic        g_axi_rvalid,
  input  logic        g_axi_rready
);

  // ---- the bridge's management EGRESS: the net this whole task is about ---
  logic [7:0] mgmt_m_tdata;
  logic       mgmt_m_tvalid;
  logic       mgmt_m_tready;
  logic       mgmt_m_tlast;

  // ---- the bridge's management INGRESS: dut_egress_0/inj_m_* (§5 delta) --
  logic [7:0] inj_m_tdata;
  logic       inj_m_tvalid;
  logic       inj_m_tready;
  logic       inj_m_tlast;

  assign mgmt_s_tready = inj_m_tready;

  // A bench that injects at the bridge directly would bypass the block under
  // test, and would not be the BD. Refuse it outright.
  always @(posedge refclk_i)
    if (mgmt_s_tvalid === 1'b1)
      $fatal(1, "tb_dut_egress: mgmt_s_tvalid driven high -- port-B ingress belongs to dut_egress_0/inj_m_* (HANDOVER_DUT_INJECT.md §5)");

  logic       uplink_s_tready;
  logic [7:0] uplink_m_tdata;
  logic       uplink_m_tvalid;
  logic       uplink_m_tlast;
  logic       mdio_i_o;
  logic       phy_rmii_ref_clk_o;

  eth_mac_test_subsystem u_ss (
    .refclk_i            (refclk_i),
    .rst_i               (rst_i),

    .phy_rmii_ref_clk_o  (phy_rmii_ref_clk_o),
    .phy_rmii_crs_dv_o   (phy_rmii_crs_dv),
    .phy_rmii_rxd_o      (phy_rmii_rxd),
    .phy_rmii_txd_i      (phy_rmii_txd),
    .phy_rmii_tx_en_i    (phy_rmii_tx_en),
    .mdc_i               (1'b0),
    .mdio_o_i            (1'b0),
    .mdio_oe_i           (1'b0),
    .mdio_i_o            (mdio_i_o),

    // Port A (uplink) stays SAFE-TIED exactly as the BD ties it: no injector,
    // and m_tready = 1 so a flooded copy drains instead of parking the bridge's
    // single round-robin sequencer.
    .uplink_s_tdata      (8'h00),
    .uplink_s_tvalid     (1'b0),
    .uplink_s_tready     (uplink_s_tready),
    .uplink_s_tlast      (1'b0),
    .uplink_m_tdata      (uplink_m_tdata),
    .uplink_m_tvalid     (uplink_m_tvalid),
    .uplink_m_tready     (1'b1),
    .uplink_m_tlast      (uplink_m_tlast),

    // Port B (mgmt): INGRESS from dut_egress_0's inject port, EGRESS into
    // its capture port — both halves of the block under test.
    .mgmt_s_tdata        (inj_m_tdata),
    .mgmt_s_tvalid       (inj_m_tvalid),
    .mgmt_s_tready       (inj_m_tready),
    .mgmt_s_tlast        (inj_m_tlast),
    .mgmt_m_tdata        (mgmt_m_tdata),
    .mgmt_m_tvalid       (mgmt_m_tvalid),
    .mgmt_m_tready       (mgmt_m_tready),
    .mgmt_m_tlast        (mgmt_m_tlast),

    // VPHY AXI-Lite: present but idle in this bench (MDIO bring-up is
    // tests/eth_mac_subsystem's scenario (a), not this one).
    .s_axi_vphy_aclk     (s_axi_aclk),
    .s_axi_vphy_aresetn  (s_axi_aresetn),
    .s_axi_vphy_awaddr   (12'h000),
    .s_axi_vphy_awprot   (3'b000),
    .s_axi_vphy_awvalid  (1'b0),
    .s_axi_vphy_awready  (),
    .s_axi_vphy_wdata    (32'h0),
    .s_axi_vphy_wstrb    (4'h0),
    .s_axi_vphy_wvalid   (1'b0),
    .s_axi_vphy_wready   (),
    .s_axi_vphy_bresp    (),
    .s_axi_vphy_bvalid   (),
    .s_axi_vphy_bready   (1'b1),
    .s_axi_vphy_araddr   (12'h000),
    .s_axi_vphy_arprot   (3'b000),
    .s_axi_vphy_arvalid  (1'b0),
    .s_axi_vphy_arready  (),
    .s_axi_vphy_rdata    (),
    .s_axi_vphy_rresp    (),
    .s_axi_vphy_rvalid   (),
    .s_axi_vphy_rready   (1'b1),

    // GENCHK AXI-Lite — in the BD this arrives through axi_cc_genchk; here the
    // bench drives it directly at refclk, which is the same domain.
    .s_axi_genchk_awaddr  (g_axi_awaddr),
    .s_axi_genchk_awprot  (3'b000),
    .s_axi_genchk_awvalid (g_axi_awvalid),
    .s_axi_genchk_awready (g_axi_awready),
    .s_axi_genchk_wdata   (g_axi_wdata),
    .s_axi_genchk_wstrb   (g_axi_wstrb),
    .s_axi_genchk_wvalid  (g_axi_wvalid),
    .s_axi_genchk_wready  (g_axi_wready),
    .s_axi_genchk_bresp   (g_axi_bresp),
    .s_axi_genchk_bvalid  (g_axi_bvalid),
    .s_axi_genchk_bready  (g_axi_bready),
    .s_axi_genchk_araddr  (g_axi_araddr),
    .s_axi_genchk_arprot  (3'b000),
    .s_axi_genchk_arvalid (g_axi_arvalid),
    .s_axi_genchk_arready (g_axi_arready),
    .s_axi_genchk_rdata   (g_axi_rdata),
    .s_axi_genchk_rresp   (g_axi_rresp),
    .s_axi_genchk_rvalid  (g_axi_rvalid),
    .s_axi_genchk_rready  (g_axi_rready)
  );

  // ---- the return path ---------------------------------------------------
  // C_S_AXI_ADDR_WIDTH = 32 because that is what the BD instantiates (bug #1:
  // the interconnect hands the slave the SYSTEM address, so a block benched at
  // the RTL default of 12 is not the block that ships).
  dut_egress #(
    .C_S_AXI_ADDR_WIDTH (32),
    .DATA_DEPTH         (2048),
    .FRAME_DEPTH        (16),
    .MAX_FRAME          (1536)
  ) u_egress (
    .s_axi_aclk    (s_axi_aclk),
    .s_axi_aresetn (s_axi_aresetn),
    .s_axi_awaddr  (s_axi_awaddr),
    .s_axi_awprot  (3'b000),
    .s_axi_awvalid (s_axi_awvalid),
    .s_axi_awready (s_axi_awready),
    .s_axi_wdata   (s_axi_wdata),
    .s_axi_wstrb   (s_axi_wstrb),
    .s_axi_wvalid  (s_axi_wvalid),
    .s_axi_wready  (s_axi_wready),
    .s_axi_bresp   (s_axi_bresp),
    .s_axi_bvalid  (s_axi_bvalid),
    .s_axi_bready  (s_axi_bready),
    .s_axi_araddr  (s_axi_araddr),
    .s_axi_arprot  (3'b000),
    .s_axi_arvalid (s_axi_arvalid),
    .s_axi_arready (s_axi_arready),
    .s_axi_rdata   (s_axi_rdata),
    .s_axi_rresp   (s_axi_rresp),
    .s_axi_rvalid  (s_axi_rvalid),
    .s_axi_rready  (s_axi_rready),

    .rmii_clk_i    (refclk_i),
    .frm_tdata_i   (mgmt_m_tdata),
    .frm_tvalid_i  (mgmt_m_tvalid),
    .frm_tlast_i   (mgmt_m_tlast),
    .frm_tready_o  (mgmt_m_tready),

    .inj_m_tdata   (inj_m_tdata),
    .inj_m_tvalid  (inj_m_tvalid),
    .inj_m_tlast   (inj_m_tlast),
    .inj_m_tready  (inj_m_tready)
  );

endmodule
