// -----------------------------------------------------------------------------
// eth_mac_test_subsystem.sv — the ARCHITECTURE_SPEC.md §8 integration module.
//
// Wires the five real ethernet blocks (fpga/ethernet/{rmii_phy_if,
// link_partner_mac,eth_bridge_3port,gen_checker,mdio_phy_model}) into the
// MAC-in-operation verification datapath (spec §8.2), so a DUT's Ethernet MAC
// can be exercised as a black box through its real RMII + MDIO partition pins,
// with the shell playing the link partner (spec §8, Phase 5 / roadmap step 9).
//
// GREENFIELD (W-ETH-SS, A1/A5): the individual blocks were real + unit-benched
// but nothing wired them into a working subsystem — this file is that wiring,
// plus the two pieces of glue the composition needs (a 2:1 AXIS arbiter into
// the MAC TX path, and the checker tap gate). Every interface reconciliation
// is called out inline; the reconciliation IS the integration value.
//
// =============================================================================
// DATAPATH (spec §8.2), block by block:
//
//   DUT MAC (RP) ⇄ RMII+MDIO partition pins
//     ├─ MDIO (mdc/mdio_o/mdio_oe → this, mdio_i ← this) ─ mdio_phy_model
//     │     (virtual PHY the DUT brings its MAC up over; VPHY @0x44A3)
//     └─ RMII (ref_clk/crs_dv/rxd → DUT, txd/tx_en ← DUT) ─ rmii_phy_if
//           ⇄ MII ⇄ link_partner_mac (far-end MAC, MII ⇄ AXI-Stream)
//              ├─ m_axis_rx (frames FROM the DUT)  ─┬─ eth_bridge_3port.dut_mac
//              │                                    └─ gen_checker.chk_s (tap)
//              └─ s_axis_tx (frames TO the DUT)   ←── 2:1 arbiter ←┬─ bridge.dut_mac egress
//                                                                  └─ gen_checker.gen_m
//   eth_bridge_3port: port A = uplink (LAN9220/host), port B = mgmt
//   (MicroBlaze/lwIP), port C = dut_mac (the link_partner_mac AXIS above).
//
// =============================================================================
// INTERFACE RECONCILIATIONS FOUND DURING INTEGRATION (each documented here —
// these did not exist until the blocks were wired together):
//
// R1. Checker tap must see COMMITTED beats, not raw valids.
//     gen_checker's chk_s port is a pure sink: it hardcodes chk_s_tready≡1 and
//     counts a beat whenever chk_s_tvalid is high (`chk_beat = chk_s_tvalid`).
//     link_partner_mac's m_axis_rx, however, HOLDS tvalid high until its sink
//     (the bridge) takes the byte — so a naive fan-out `chk_s_tvalid =
//     m_axis_rx_tvalid` would count the same byte every cycle the bridge
//     backpressures. Reconciled: the bridge is the real (flow-controlling)
//     consumer — m_axis_rx_tready = bridge.dut_mac_s_tready — and the checker
//     tap valid is GATED to the committed beats: chk_s_tvalid = m_axis_rx_tvalid
//     & bridge.dut_mac_s_tready. One pulse per accepted byte, tlast intact.
//
// R2. Two producers feed the single MAC TX port.
//     Both the bridge's dut_mac egress (host/mgmt→DUT traffic, spec §8.2) and
//     gen_checker's gen_m (injected malformed frames, spec §8.1) target
//     link_partner_mac.s_axis_tx. link_partner_mac/README.md calls for
//     splicing gen_checker "as a third AXI-Stream producer ... arbitrated
//     ahead of the bridge". Reconciled with a small packet-locked 2:1 AXIS
//     arbiter (u_tx_arb, below): grant held from first beat to tlast so
//     frames never interleave; bridge has priority when both request.
//
// R3. Reset polarity split.
//     rmii_phy_if / link_partner_mac / eth_bridge_3port take active-HIGH
//     rst_i; gen_checker + mdio_phy_model take active-LOW s_axi_aresetn.
//     Reconciled at the boundary: the datapath reset is active-high `rst_i`;
//     gen_checker's aresetn is derived as ~rst_i (it lives in the datapath
//     clock domain — see R4); the VPHY block keeps its own async AXI reset.
//
// R4. Clock domains (spec §5 "CDC everywhere static and DUT domains meet",
//     fpga/ethernet/README.md "Clocking / gotchas"):
//       * DATAPATH domain = refclk_i (MUST be 50 MHz — the RMII reference the
//         shell sources to the DUT, spec §8.3). rmii_phy_if, link_partner_mac,
//         AND eth_bridge_3port all run here; the MII "clocks" between phy_if
//         and the MAC are divide-by-2 pacing enables (no crossing) — one
//         single 50 MHz domain, exactly as the block README documents.
//       * gen_checker FUSES its control and datapath clocks: its only clock is
//         s_axi_aclk, which is also the clock of its gen_m/chk_s AXIS ports.
//         To keep the datapath single-domain (R1/R2 splice gen_checker's AXIS
//         straight into it) gen_checker is clocked on refclk_i here — so the
//         GENCHK AXI-Lite surface is on refclk_i too (no separate aclk port).
//         INTEGRATION NOTE for A6/W-BD: a real shell runs the MicroBlaze bus at
//         ~100 MHz, so GENCHK needs an AXI4-Lite clock-converter in front (or
//         gen_checker needs a datapath-vs-control clock split + async FIFOs on
//         gen_m/chk_s). Flagged; out of this subsystem's scope.
//       * VPHY (mdio_phy_model) has a GENUINE CDC: its s_axi_vphy_aclk domain
//         crosses into the DUT's mdc domain via 2-flop synchronizers inside
//         the block. That AXI clock is a separate port (s_axi_vphy_aclk) so it
//         can be — and in the bench is — a different period from both refclk
//         and mdc, exercising the crossing (spec §5).
//
// R5. HDPR-29 TX re-register stays static-side.
//     The single static-side sampling flop on phy_rmii_txd/tx_en lives inside
//     rmii_phy_if (its header) — this subsystem never re-registers those pins
//     itself, so that IOB-packable stage stays where the contract requires
//     (partition-pins.md "IOB packing note"). Preserved by construction.
//
// The subsystem is entirely static-shell logic behind the (future) Shutdown
// Manager (spec §8.3); only the RMII + MDIO group crosses the partition
// boundary. lan9220_if is intentionally NOT instantiated (stub by decision —
// see its README); the LAN9220 uplink is exposed as a plain AXI-Stream port
// pair for W-BD to attach the axi_emc_0 vendor cell to.
// -----------------------------------------------------------------------------
// Build note: the five instantiated blocks are separate compilation sources
// (listed in the Makefile / lint invocation, same pattern as the
// tests/link_partner_mac pairing) — NOT `include`d here. mdio_phy_model.sv
// pulls in mdio_slave.sv + phy_reg_model.sv via its own `include (resolved by
// +incdir+fpga/ethernet/mdio_phy_model), so those two are not listed
// separately (double-definition otherwise). This file therefore has no
// `include of its own.
`timescale 1ns / 1ps

module eth_mac_test_subsystem #(
  // VPHY (mdio_phy_model) straps
  parameter logic [4:0]  VPHY_PHY_ADDR          = 5'd1,
  parameter logic [31:0] VPHY_PHY_ID_DEFAULT    = 32'h0007_C0F1,
  parameter int          VPHY_PULSE_HOLD_CYCLES = 32,
  // bridge forwarding table (compile-time, register-less — I11)
  parameter logic [47:0] BRIDGE_MGMT_MAC        = 48'h02_00_00_00_00_01,
  parameter logic [47:0] BRIDGE_DUT_MAC         = 48'h02_00_00_00_00_02,
  parameter int unsigned BRIDGE_BUF_AW          = 11
) (
  // ---------------------------------------------------------------------------
  // Datapath clock/reset — 50 MHz RMII-reference domain (R4). refclk_i MUST be
  // 50 MHz; it is forwarded to the DUT as phy_rmii_ref_clk_o.
  // ---------------------------------------------------------------------------
  input  logic       refclk_i,
  input  logic       rst_i,        // active-high datapath reset

  // ---------------------------------------------------------------------------
  // RP-facing partition pins — RMII + MDIO group, shell's view
  // (docs/contracts/partition-pins.md "Ethernet — RMII + MDIO").
  // ---------------------------------------------------------------------------
  output logic       phy_rmii_ref_clk_o,   // 50 MHz REF_CLK sourced to DUT
  output logic       phy_rmii_crs_dv_o,    // carrier-sense/RX-valid to DUT
  output logic [1:0] phy_rmii_rxd_o,       // RX data to DUT
  input  logic [1:0] phy_rmii_txd_i,       // TX data from DUT
  input  logic       phy_rmii_tx_en_i,     // TX enable from DUT
  input  logic       mdc_i,                // MDIO clock from DUT (DUT=master)
  input  logic       mdio_o_i,             // MDIO out from DUT
  input  logic       mdio_oe_i,            // MDIO output-enable from DUT
  output logic       mdio_i_o,             // MDIO in to DUT (VPHY reply)

  // ---------------------------------------------------------------------------
  // Bridge port A — LAN9220 / host uplink AXI-Stream (toward lan9220_if,
  // stubbed; W-BD attaches the axi_emc_0 vendor cell here).
  // ---------------------------------------------------------------------------
  input  logic [7:0] uplink_s_tdata,
  input  logic       uplink_s_tvalid,
  output logic       uplink_s_tready,
  input  logic       uplink_s_tlast,
  output logic [7:0] uplink_m_tdata,
  output logic       uplink_m_tvalid,
  input  logic       uplink_m_tready,
  output logic       uplink_m_tlast,

  // ---------------------------------------------------------------------------
  // Bridge port B — MicroBlaze / mgmt (lwIP) AXI-Stream.
  // ---------------------------------------------------------------------------
  input  logic [7:0] mgmt_s_tdata,
  input  logic       mgmt_s_tvalid,
  output logic       mgmt_s_tready,
  input  logic       mgmt_s_tlast,
  output logic [7:0] mgmt_m_tdata,
  output logic       mgmt_m_tvalid,
  input  logic       mgmt_m_tready,
  output logic       mgmt_m_tlast,

  // ---------------------------------------------------------------------------
  // VPHY AXI4-Lite control surface (shell-regmap.md @ 0x44A3_0000) — its OWN
  // clock/reset (genuine CDC to the mdc domain inside mdio_phy_model, R4).
  // ---------------------------------------------------------------------------
  input  logic        s_axi_vphy_aclk,
  input  logic        s_axi_vphy_aresetn,
  input  logic [11:0] s_axi_vphy_awaddr,
  input  logic [2:0]  s_axi_vphy_awprot,
  input  logic        s_axi_vphy_awvalid,
  output logic        s_axi_vphy_awready,
  input  logic [31:0] s_axi_vphy_wdata,
  input  logic [3:0]  s_axi_vphy_wstrb,
  input  logic        s_axi_vphy_wvalid,
  output logic        s_axi_vphy_wready,
  output logic [1:0]  s_axi_vphy_bresp,
  output logic        s_axi_vphy_bvalid,
  input  logic        s_axi_vphy_bready,
  input  logic [11:0] s_axi_vphy_araddr,
  input  logic [2:0]  s_axi_vphy_arprot,
  input  logic        s_axi_vphy_arvalid,
  output logic        s_axi_vphy_arready,
  output logic [31:0] s_axi_vphy_rdata,
  output logic [1:0]  s_axi_vphy_rresp,
  output logic        s_axi_vphy_rvalid,
  input  logic        s_axi_vphy_rready,

  // ---------------------------------------------------------------------------
  // GENCHK AXI4-Lite control surface (shell-regmap.md @ 0x44A6_0000).
  // Clocked by refclk_i / reset by ~rst_i (R3/R4 — gen_checker fuses control
  // and datapath clocks; no separate aclk port). See the R4 note for the real
  // shell's AXI clock-converter requirement.
  // ---------------------------------------------------------------------------
  input  logic [11:0] s_axi_genchk_awaddr,
  input  logic [2:0]  s_axi_genchk_awprot,
  input  logic        s_axi_genchk_awvalid,
  output logic        s_axi_genchk_awready,
  input  logic [31:0] s_axi_genchk_wdata,
  input  logic [3:0]  s_axi_genchk_wstrb,
  input  logic        s_axi_genchk_wvalid,
  output logic        s_axi_genchk_wready,
  output logic [1:0]  s_axi_genchk_bresp,
  output logic        s_axi_genchk_bvalid,
  input  logic        s_axi_genchk_bready,
  input  logic [11:0] s_axi_genchk_araddr,
  input  logic [2:0]  s_axi_genchk_arprot,
  input  logic        s_axi_genchk_arvalid,
  output logic        s_axi_genchk_arready,
  output logic [31:0] s_axi_genchk_rdata,
  output logic [1:0]  s_axi_genchk_rresp,
  output logic        s_axi_genchk_rvalid,
  input  logic        s_axi_genchk_rready
);

  // ===========================================================================
  // Internal MII pairing (rmii_phy_if ⇄ link_partner_mac) — one 50 MHz domain.
  // ===========================================================================
  logic [3:0] mii_rxd, mii_txd;
  logic       mii_rx_dv, mii_rx_er, mii_rx_clk, mii_tx_en, mii_tx_clk;

  // link_partner_mac AXI-Stream (RX = frames from DUT, TX = frames to DUT).
  logic [7:0] lpm_rx_tdata;
  logic       lpm_rx_tvalid, lpm_rx_tready, lpm_rx_tlast, lpm_rx_tuser;
  logic [7:0] lpm_tx_tdata;
  logic       lpm_tx_tvalid, lpm_tx_tready, lpm_tx_tlast;

  // bridge dut_mac port (C): ingress from the MAC RX, egress toward the MAC TX.
  logic [7:0] br_dut_s_tdata;
  logic       br_dut_s_tvalid, br_dut_s_tready, br_dut_s_tlast, br_dut_s_tuser;
  logic [7:0] br_dut_m_tdata;
  logic       br_dut_m_tvalid, br_dut_m_tready, br_dut_m_tlast;

  // gen_checker generator AXI-Stream out.
  logic [7:0] gen_tdata;
  logic       gen_tvalid, gen_tready, gen_tlast;

  // gen_checker aresetn (R3): datapath-domain, active-low.
  logic       genchk_aresetn;
  assign genchk_aresetn = ~rst_i;

  // ===========================================================================
  // RMII virtual-PHY datapath (spec §8.1). Shell = PHY side: sources REF_CLK,
  // drives crs_dv/rxd toward the DUT, samples txd/tx_en from it (HDPR-29
  // re-register stage lives inside this block — R5).
  // ===========================================================================
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

  // ===========================================================================
  // Link-partner MAC (spec §8.1): MII ⇄ AXI-Stream, single 50 MHz domain.
  // ===========================================================================
  link_partner_mac u_mac (
    .clk_i            (refclk_i),
    .rst_i            (rst_i),
    .mii_rxd_i        (mii_rxd),
    .mii_rx_dv_i      (mii_rx_dv),
    .mii_rx_er_i      (mii_rx_er),
    .mii_rx_clk_i     (mii_rx_clk),
    .mii_txd_o        (mii_txd),
    .mii_tx_en_o      (mii_tx_en),
    .mii_tx_clk_i     (mii_tx_clk),
    .m_axis_rx_tdata  (lpm_rx_tdata),
    .m_axis_rx_tvalid (lpm_rx_tvalid),
    .m_axis_rx_tready (lpm_rx_tready),
    .m_axis_rx_tlast  (lpm_rx_tlast),
    .m_axis_rx_tuser  (lpm_rx_tuser),
    .s_axis_tx_tdata  (lpm_tx_tdata),
    .s_axis_tx_tvalid (lpm_tx_tvalid),
    .s_axis_tx_tready (lpm_tx_tready),
    .s_axis_tx_tlast  (lpm_tx_tlast)
  );

  // ===========================================================================
  // R1: MAC RX → bridge dut_mac ingress (the real, flow-controlling consumer)
  //     + gen_checker checker tap (gated to committed beats).
  // ===========================================================================
  assign br_dut_s_tdata  = lpm_rx_tdata;
  assign br_dut_s_tvalid = lpm_rx_tvalid;
  assign br_dut_s_tlast  = lpm_rx_tlast;
  assign br_dut_s_tuser  = lpm_rx_tuser;
  assign lpm_rx_tready   = br_dut_s_tready;   // bridge owns the handshake

  logic [7:0] chk_tdata;
  logic       chk_tvalid, chk_tlast, chk_tuser;
  assign chk_tdata  = lpm_rx_tdata;
  assign chk_tvalid = lpm_rx_tvalid & br_dut_s_tready;   // committed beats only
  assign chk_tlast  = lpm_rx_tlast;
  assign chk_tuser  = lpm_rx_tuser;

  // ===========================================================================
  // 3-port L2 bridge (spec §9): A = uplink, B = mgmt, C = dut_mac.
  // ===========================================================================
  eth_bridge_3port #(
    .MGMT_MAC (BRIDGE_MGMT_MAC),
    .DUT_MAC  (BRIDGE_DUT_MAC),
    .BUF_AW   (BRIDGE_BUF_AW)
  ) u_bridge (
    .clk_i            (refclk_i),
    .rst_i            (rst_i),
    .mgmt_s_tdata     (mgmt_s_tdata),
    .mgmt_s_tvalid    (mgmt_s_tvalid),
    .mgmt_s_tready    (mgmt_s_tready),
    .mgmt_s_tlast     (mgmt_s_tlast),
    .mgmt_m_tdata     (mgmt_m_tdata),
    .mgmt_m_tvalid    (mgmt_m_tvalid),
    .mgmt_m_tready    (mgmt_m_tready),
    .mgmt_m_tlast     (mgmt_m_tlast),
    .dut_mac_s_tdata  (br_dut_s_tdata),
    .dut_mac_s_tvalid (br_dut_s_tvalid),
    .dut_mac_s_tready (br_dut_s_tready),
    .dut_mac_s_tlast  (br_dut_s_tlast),
    .dut_mac_s_tuser  (br_dut_s_tuser),
    .dut_mac_m_tdata  (br_dut_m_tdata),
    .dut_mac_m_tvalid (br_dut_m_tvalid),
    .dut_mac_m_tready (br_dut_m_tready),
    .dut_mac_m_tlast  (br_dut_m_tlast),
    .uplink_s_tdata   (uplink_s_tdata),
    .uplink_s_tvalid  (uplink_s_tvalid),
    .uplink_s_tready  (uplink_s_tready),
    .uplink_s_tlast   (uplink_s_tlast),
    .uplink_m_tdata   (uplink_m_tdata),
    .uplink_m_tvalid  (uplink_m_tvalid),
    .uplink_m_tready  (uplink_m_tready),
    .uplink_m_tlast   (uplink_m_tlast)
  );

  // ===========================================================================
  // R2: 2:1 packet-locked AXIS arbiter into the MAC TX port.
  //   input A = bridge dut_mac egress (host/mgmt→DUT), priority when both;
  //   input B = gen_checker gen_m (injected frames).
  // Grant is chosen combinationally when idle (no lost first beat) and held to
  // tlast so frames never interleave.
  // ===========================================================================
  localparam logic [1:0] ARB_NONE = 2'd0, ARB_A = 2'd1, ARB_B = 2'd2;
  logic [1:0] arb_grant_q, arb_grant;

  always_comb begin
    arb_grant = arb_grant_q;
    if (arb_grant_q == ARB_NONE) begin
      if      (br_dut_m_tvalid) arb_grant = ARB_A;
      else if (gen_tvalid)      arb_grant = ARB_B;
    end
  end

  always_comb begin
    // defaults: nothing granted
    lpm_tx_tdata    = 8'h00;
    lpm_tx_tvalid   = 1'b0;
    lpm_tx_tlast    = 1'b0;
    br_dut_m_tready = 1'b0;
    gen_tready      = 1'b0;
    unique case (arb_grant)
      ARB_A: begin
        lpm_tx_tdata    = br_dut_m_tdata;
        lpm_tx_tvalid   = br_dut_m_tvalid;
        lpm_tx_tlast    = br_dut_m_tlast;
        br_dut_m_tready = lpm_tx_tready;
      end
      ARB_B: begin
        lpm_tx_tdata  = gen_tdata;
        lpm_tx_tvalid = gen_tvalid;
        lpm_tx_tlast  = gen_tlast;
        gen_tready    = lpm_tx_tready;
      end
      default: ; // ARB_NONE: defaults hold
    endcase
  end

  always_ff @(posedge refclk_i or posedge rst_i) begin
    if (rst_i) begin
      arb_grant_q <= ARB_NONE;
    end else if (arb_grant != ARB_NONE && lpm_tx_tvalid && lpm_tx_tready
                 && lpm_tx_tlast) begin
      arb_grant_q <= ARB_NONE;   // frame done: release
    end else begin
      arb_grant_q <= arb_grant;  // latch (or keep) the current grant
    end
  end

  // ===========================================================================
  // Error-inject traffic gen/checker (spec §8.1, §8.4; GENCHK @ 0x44A6).
  // Clocked on refclk_i (R4). Generator → arbiter input B; checker ← RX tap.
  // ===========================================================================
  gen_checker #(
    .C_S_AXI_ADDR_WIDTH (12),
    .C_S_AXI_DATA_WIDTH (32)
  ) u_gen_checker (
    .s_axi_aclk    (refclk_i),
    .s_axi_aresetn (genchk_aresetn),
    .s_axi_awaddr  (s_axi_genchk_awaddr),
    .s_axi_awprot  (s_axi_genchk_awprot),
    .s_axi_awvalid (s_axi_genchk_awvalid),
    .s_axi_awready (s_axi_genchk_awready),
    .s_axi_wdata   (s_axi_genchk_wdata),
    .s_axi_wstrb   (s_axi_genchk_wstrb),
    .s_axi_wvalid  (s_axi_genchk_wvalid),
    .s_axi_wready  (s_axi_genchk_wready),
    .s_axi_bresp   (s_axi_genchk_bresp),
    .s_axi_bvalid  (s_axi_genchk_bvalid),
    .s_axi_bready  (s_axi_genchk_bready),
    .s_axi_araddr  (s_axi_genchk_araddr),
    .s_axi_arprot  (s_axi_genchk_arprot),
    .s_axi_arvalid (s_axi_genchk_arvalid),
    .s_axi_arready (s_axi_genchk_arready),
    .s_axi_rdata   (s_axi_genchk_rdata),
    .s_axi_rresp   (s_axi_genchk_rresp),
    .s_axi_rvalid  (s_axi_genchk_rvalid),
    .s_axi_rready  (s_axi_genchk_rready),
    .gen_m_tdata   (gen_tdata),
    .gen_m_tvalid  (gen_tvalid),
    .gen_m_tready  (gen_tready),
    .gen_m_tlast   (gen_tlast),
    .chk_s_tdata   (chk_tdata),
    .chk_s_tvalid  (chk_tvalid),
    .chk_s_tready  (),            // pure sink (internally ≡1); left open (R1)
    .chk_s_tlast   (chk_tlast),
    .chk_s_tuser   (chk_tuser)
  );

  // ===========================================================================
  // Virtual-PHY MDIO register model (spec §8.1; VPHY @ 0x44A3). The DUT brings
  // its MAC up by polling this over MDIO; the host injects link events through
  // the AXI-Lite surface. Genuine s_axi_vphy_aclk ⇄ mdc CDC lives inside (R4).
  // ===========================================================================
  mdio_phy_model #(
    .C_S_AXI_ADDR_WIDTH  (12),
    .C_S_AXI_DATA_WIDTH  (32),
    .C_PHY_ADDR          (VPHY_PHY_ADDR),
    .C_PHY_ID_DEFAULT    (VPHY_PHY_ID_DEFAULT),
    .C_PULSE_HOLD_CYCLES (VPHY_PULSE_HOLD_CYCLES)
  ) u_mdio_phy_model (
    .s_axi_aclk    (s_axi_vphy_aclk),
    .s_axi_aresetn (s_axi_vphy_aresetn),
    .s_axi_awaddr  (s_axi_vphy_awaddr),
    .s_axi_awprot  (s_axi_vphy_awprot),
    .s_axi_awvalid (s_axi_vphy_awvalid),
    .s_axi_awready (s_axi_vphy_awready),
    .s_axi_wdata   (s_axi_vphy_wdata),
    .s_axi_wstrb   (s_axi_vphy_wstrb),
    .s_axi_wvalid  (s_axi_vphy_wvalid),
    .s_axi_wready  (s_axi_vphy_wready),
    .s_axi_bresp   (s_axi_vphy_bresp),
    .s_axi_bvalid  (s_axi_vphy_bvalid),
    .s_axi_bready  (s_axi_vphy_bready),
    .s_axi_araddr  (s_axi_vphy_araddr),
    .s_axi_arprot  (s_axi_vphy_arprot),
    .s_axi_arvalid (s_axi_vphy_arvalid),
    .s_axi_arready (s_axi_vphy_arready),
    .s_axi_rdata   (s_axi_vphy_rdata),
    .s_axi_rresp   (s_axi_vphy_rresp),
    .s_axi_rvalid  (s_axi_vphy_rvalid),
    .s_axi_rready  (s_axi_vphy_rready),
    .mdc_i         (mdc_i),
    .mdio_o_i      (mdio_o_i),
    .mdio_oe_i     (mdio_oe_i),
    .mdio_i_o      (mdio_i_o)
  );

endmodule
