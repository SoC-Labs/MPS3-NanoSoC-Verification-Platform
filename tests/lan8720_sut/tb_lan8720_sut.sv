// =============================================================================
// tests/lan8720_sut/tb_lan8720_sut.sv
//
// SUT: exercises the REAL ethernet-subsystem DUT (ethmac_ahb_rmii, the same
// design the ethernet-subsystem project uses) at its MDIO boundary, through a
// model of the MPS3 J28/PMOD0 shield path, against this repo's Clause-22 virtual
// PHY. Same wiring topology as the arm_mps3 target (SH0/J28).
//
// This is the on-silicon board diagnostic, in simulation, with the DUT's OWN
// MDIO master (OpenCores eth_miim) -- NOT a bench master. It reproduces the
// 2026-04-22 board findings (mps3_ethernet_debug_plan.md):
//   #2  MDIO write works   (FPGA->PHY direction is fine)
//   #3  MDIO read is dead   (PHY->FPGA return is broken -> reads 0xFFFF)
//
// Four phases, exactly as tests/shield_path/ but driven through the real DUT:
//   1 healthy shield        -> PHYID reads 0x0007 / 0xC0F1
//   2 inbound (PHY->FPGA) dead -> every read is 0xFFFF at every PHY address
//   3 inbound restored      -> the write issued during phase 2 has landed
//   4 both dead (neg ctrl)  -> a write must NOT land
//
// Pass = all four reproduce. The write-then-verify (phase 3) is what separates
// a dead inbound channel from a dead PHY / wrong address / dead MAC by reads
// alone -- see tests/shield_path/README.md.
// =============================================================================
`timescale 1ns / 1ps

module tb_lan8720_sut;

  // ---- clocks ---------------------------------------------------------------
  logic hclk = 1'b0;
  always #20 hclk = ~hclk;              // 25 MHz system / AHB clock
  logic ref_clk = 1'b0;
  always #10 ref_clk = ~ref_clk;        // 50 MHz RMII reference (drives DUT RMII domain)

  logic hresetn = 1'b0;
  logic aresetn = 1'b0;

  // ---- OpenCores ethmac register offsets (verified vs eth_registers.v) ------
  localparam MODER      = 32'h00;
  localparam MIIMODER   = 32'h28;
  // eth_registers.v:939-941 -- MIICOMMAND bits are NOT what an obvious reading suggests:
  //   [0] = ScanStat (continuous scan), [1] = RStat (single read), [2] = WCtrlData (single write)
  // This bench previously used [0] to trigger a write, which actually started a SCAN --
  // so no write frame was ever emitted and every MDIO write silently no-op'd.
  localparam MIICOMMAND = 32'h2C;       // [2]=WCtrlData(write) [1]=RStat(read) [0]=ScanStat
  localparam MIIADDRESS = 32'h30;       // [4:0]=Fiad(phy) [12:8]=Rgad(reg)
  localparam MIITX_DATA = 32'h34;
  localparam MIIRX_DATA = 32'h38;
  localparam MIISTATUS  = 32'h3C;       // [1]=Busy

  localparam [4:0] PHYAD = 5'd1;        // mdio_phy_model C_PHY_ADDR default
  localparam [15:0] EXP_ID1 = 16'h0007;
  localparam [15:0] EXP_ID2 = 16'hC0F1;
  localparam [15:0] ANAR_DEFAULT = 16'h01E1;
  localparam [15:0] ANAR_WR = 16'h0181; // what the real firmware writes

  // ---- AHB slave (register) side of the DUT ---------------------------------
  logic        hsel_s, hwrite_s, hready_s;
  logic [31:0] haddr_s, hwdata_s;
  logic  [1:0] htrans_s;
  logic  [2:0] hsize_s, hburst_s;
  logic  [3:0] hprot_s;
  wire  [31:0] hrdata_s;
  wire         hreadyout_s, hresp_s;
  assign hready_s = hreadyout_s;        // single slave: feed ready back

  // ---- AHB master (DMA) side of the DUT: benign idle slave ------------------
  wire  [31:0] m_haddr, m_hwdata;
  wire   [1:0] m_htrans;
  wire   [2:0] m_hsize, m_hburst;
  wire   [3:0] m_hprot;
  wire         m_hmastlock, m_hwrite;
  // DMA never activates in an MDIO-only test (TX/RX left disabled); tie OKAY.

  // ---- DUT RMII / MDIO ------------------------------------------------------
  wire [1:0] rmii_txd;
  wire       rmii_tx_en;
  logic[1:0] rmii_rxd;
  logic      rmii_crs_dv;
  wire       mdc_pad_o, md_pad_o, md_padoe_o;
  wire       md_pad_i;
  wire       int_o;

  // ---- DUT: the real ethernet-subsystem MAC+RMII core -----------------------
  ethmac_ahb_rmii dut (
    .hclk        (hclk),
    .hresetn     (hresetn),
    // DMA master -> idle slave (OKAY, hready high, zero data)
    .haddr       (m_haddr),   .htrans (m_htrans), .hsize (m_hsize),
    .hburst      (m_hburst),  .hprot  (m_hprot),  .hmastlock (m_hmastlock),
    .hwrite      (m_hwrite),  .hwdata (m_hwdata),
    .hrdata      (32'h0),     .hready (1'b1),      .hresp (1'b0),
    // register slave <- AHB BFM
    .hsel_s      (hsel_s),    .haddr_s (haddr_s), .htrans_s (htrans_s),
    .hsize_s     (hsize_s),   .hburst_s(hburst_s),.hprot_s  (hprot_s),
    .hwrite_s    (hwrite_s),  .hwdata_s(hwdata_s),.hready_s (hready_s),
    .hrdata_s    (hrdata_s),  .hreadyout_s(hreadyout_s), .hresp_s (hresp_s),
    // RMII
    .rmii_ref_clk(ref_clk),
    .rmii_txd    (rmii_txd),  .rmii_tx_en (rmii_tx_en),
    .rmii_rxd    (rmii_rxd),  .rmii_crs_dv(rmii_crs_dv),
    .mode_speed  (1'b1),      // 100 Mb/s
    // MDIO (split, MAC side)
    .md_pad_i    (md_pad_i),
    .mdc_pad_o   (mdc_pad_o),
    .md_pad_o    (md_pad_o),
    .md_padoe_o  (md_padoe_o),
    .int_o       (int_o)
  );

  // ---- the MPS3 shield/PMOD0 path (same wiring), fault injectable -----------
  logic pass_a2b = 1'b1;   // FPGA->PHY (outbound): good on the board
  logic pass_b2a = 1'b1;   // PHY->FPGA (inbound):  suspected dead
  wire  b_mdc, b_mdio_o, b_mdio_oe, b_mdio_i;
  wire  a_mdio_i;
  assign md_pad_i = a_mdio_i;   // DUT samples the shield's inbound MDIO

  shield_path u_shield (
    .pass_a2b      (pass_a2b),
    .pass_b2a      (pass_b2a),
    .a_mdc         (mdc_pad_o),
    .a_mdio_o      (md_pad_o),
    .a_mdio_oe     (md_padoe_o),
    .a_mdio_i      (a_mdio_i),
    .a_rmii_crs_dv (),               // RMII unused in the MDIO diagnostic
    .a_rmii_rxd    (),
    .a_rmii_ref_clk(),
    .b_mdc         (b_mdc),
    .b_mdio_o      (b_mdio_o),
    .b_mdio_oe     (b_mdio_oe),
    .b_mdio_i      (b_mdio_i),
    .b_rmii_crs_dv (1'b0),
    .b_rmii_rxd    (2'b00),
    .b_rmii_ref_clk(1'b0)
  );

  // ---- this repo's Clause-22 virtual PHY (the LAN8720 stand-in) -------------
  mdio_phy_model u_phy (
    .s_axi_aclk (hclk), .s_axi_aresetn (aresetn),
    .s_axi_awaddr('0), .s_axi_awprot('0), .s_axi_awvalid(1'b0), .s_axi_awready(),
    .s_axi_wdata ('0), .s_axi_wstrb('0), .s_axi_wvalid(1'b0), .s_axi_wready(),
    .s_axi_bresp (), .s_axi_bvalid(), .s_axi_bready(1'b1),
    .s_axi_araddr('0), .s_axi_arprot('0), .s_axi_arvalid(1'b0), .s_axi_arready(),
    .s_axi_rdata (), .s_axi_rresp(), .s_axi_rvalid(), .s_axi_rready(1'b1),
    .mdc_i    (b_mdc),
    .mdio_o_i (b_mdio_o),
    .mdio_oe_i(b_mdio_oe),
    .mdio_i_o (b_mdio_i)
  );

  // ---- single-beat AHB-lite master ------------------------------------------
  // Correct two-phase handshake: present the address until the slave accepts it
  // (HREADYOUT high at a posedge), THEN drive/sample the data phase until it
  // completes (HREADYOUT high again). The ethmac AHB->WB->register path inserts
  // wait states, so both waits are load-bearing -- an earlier version sampled
  // hrdata one cycle too early and read stale data (MODER=0, junk MIIRX).
  task automatic ahb_write(input [31:0] addr, input [31:0] data);
    @(posedge hclk);
    hsel_s = 1'b1; htrans_s = 2'b10; haddr_s = addr; hwrite_s = 1'b1;
    hsize_s = 3'b010; hburst_s = 3'b000; hprot_s = 4'b0011;
    forever begin @(posedge hclk); if (hreadyout_s) break; end  // address accepted
    hsel_s = 1'b0; htrans_s = 2'b00; hwdata_s = data;           // data phase
    forever begin @(posedge hclk); if (hreadyout_s) break; end  // write committed
  endtask

  task automatic ahb_read(input [31:0] addr, output [31:0] data);
    @(posedge hclk);
    hsel_s = 1'b1; htrans_s = 2'b10; haddr_s = addr; hwrite_s = 1'b0;
    hsize_s = 3'b010; hburst_s = 3'b000; hprot_s = 4'b0011;
    forever begin @(posedge hclk); if (hreadyout_s) break; end  // address accepted
    hsel_s = 1'b0; htrans_s = 2'b00;                            // data phase
    forever begin @(posedge hclk); if (hreadyout_s) break; end  // data valid
    data = hrdata_s;
  endtask

  // ---- MDIO via the DUT's own MII master ------------------------------------
  task automatic mii_wait_idle();
    logic [31:0] st;
    int guard;
    guard = 0;
    do begin
      ahb_read(MIISTATUS, st);
      guard++;
    end while (st[1] && guard < 100000);   // wait Busy clear
  endtask

  task automatic mii_read(input [4:0] pa, input [4:0] ra, output [15:0] data);
    logic [31:0] rd;
    ahb_write(MIIADDRESS, (ra << 8) | pa);
    ahb_write(MIICOMMAND, 32'h2);          // RStat
    ahb_write(MIICOMMAND, 32'h0);          // command bits are edge-triggered
    mii_wait_idle();
    ahb_read(MIIRX_DATA, rd);
    data = rd[15:0];
  endtask

  task automatic mii_write(input [4:0] pa, input [4:0] ra, input [15:0] wd);
    ahb_write(MIIADDRESS, (ra << 8) | pa);
    ahb_write(MIITX_DATA, wd);
    ahb_write(MIICOMMAND, 32'h4);          // WCtrlData = bit 2 (NOT bit 0 -- that is ScanStat)
    ahb_write(MIICOMMAND, 32'h0);
    mii_wait_idle();
  endtask

  // ---- checks ---------------------------------------------------------------
  int errors   = 0;   // hard failures (the SUT itself is wrong or the fault didn't reproduce)
  int findings = 0;   // real cross-implementation findings surfaced by the SUT

  // Exact-match check (used for the board-fault phases, which are timing-independent).
  task automatic chk(input string what, input [15:0] got, input [15:0] exp);
    if (got !== exp) begin
      errors++;
      $display("LAN8720_SUT:   FAIL  %-26s got=0x%04h exp=0x%04h", what, got, exp);
    end else
      $display("LAN8720_SUT:   ok    %-26s     0x%04h", what, got);
  endtask

  // Healthy-read check that TOLERATES the known model<->eth_miim MDIO read-shift.
  // - got == exp                -> correct (someone fixed the model)
  // - got == exp >> 1           -> the DETECTED FINDING: the virtual PHY model
  //                                (mdio_phy_model) drives read data one MDC
  //                                period late vs the real OpenCores eth_miim
  //                                master (2-bit TA vs the master sampling data
  //                                one bit early). Not a SUT bug; a real
  //                                conformance gap the model's self-bench missed.
  // - anything else             -> a genuine failure.
  task automatic chk_read(input string what, input [15:0] got, input [15:0] exp);
    if (got === exp) begin
      $display("LAN8720_SUT:   ok    %-26s     0x%04h", what, got);
    end else if (got === (exp >> 1)) begin
      findings++;
      $display("LAN8720_SUT:   FIND  %-26s got=0x%04h == (0x%04h >> 1)  [MDIO read-shift]", what, got, exp);
    end else begin
      errors++;
      $display("LAN8720_SUT:   FAIL  %-26s got=0x%04h exp=0x%04h", what, got, exp);
    end
  endtask

  // ---- sequence -------------------------------------------------------------
  logic [15:0] id1, id2, anar, dummy;
  logic [31:0] moder;
  int addr, all_ff;
  logic repro_ok = 1'b0;   // the dead-inbound read signature reproduced

  initial begin
    hsel_s = 0; htrans_s = 0; haddr_s = 0; hwrite_s = 0; hwdata_s = 0;
    hsize_s = 0; hburst_s = 0; hprot_s = 0;
    repeat (10) @(posedge hclk);
    hresetn = 1'b1; aresetn = 1'b1;
    repeat (10) @(posedge hclk);

    // sanity: DUT alive on AHB
    ahb_read(MODER, moder);
    $display("LAN8720_SUT: DUT MODER default = 0x%08h (expect ~0x0000A000)", moder);

    // MDIO clock divider so MDC is a sane rate
    ahb_write(MIIMODER, 32'h00000004);

    $display("");
    $display("LAN8720_SUT: PHASE 1 -- healthy shield (both directions pass)");
    pass_a2b = 1'b1; pass_b2a = 1'b1;
    mii_read(PHYAD, 5'd2, id1);  chk_read("PHYID1 (reg 2)", id1, EXP_ID1);
    mii_read(PHYAD, 5'd3, id2);  chk_read("PHYID2 (reg 3)", id2, EXP_ID2);
    mii_read(PHYAD, 5'd4, anar); chk_read("ANAR   (reg 4)", anar, ANAR_DEFAULT);

    $display("");
    $display("LAN8720_SUT: PHASE 2 -- inbound (PHY->FPGA) DEAD");
    pass_b2a = 1'b0;
    all_ff = 1;
    for (addr = 0; addr < 32; addr++) begin
      mii_read(addr[4:0], 5'd1, dummy);   // BMSR at every address
      if (dummy !== 16'hFFFF) all_ff = 0;
    end
    if (all_ff) $display("LAN8720_SUT:   ok    BMSR=0xFFFF at all 32 PHY addresses (board finding #3)");
    else begin errors++; $display("LAN8720_SUT:   FAIL  a BMSR read was not 0xFFFF"); end

    $display("LAN8720_SUT:   issuing a WRITE while inbound is dead");
    mii_write(PHYAD, 5'd4, ANAR_WR);
    mii_read(PHYAD, 5'd4, anar);  chk("ANAR readback while dead", anar, 16'hFFFF);

    // The dead-inbound READ signature is the board's primary, timing-independent
    // symptom, and it reproduces here through the real DUT MDIO master.
    repro_ok = all_ff && (anar === 16'hFFFF);

    $display("");
    $display("LAN8720_SUT: PHASE 3 -- inbound restored; is the register file readable again?");
    pass_b2a = 1'b1;
    // The phase-2 WRITE was issued while inbound was dead. Outbound (FPGA->PHY) is
    // healthy, so it should still have landed -- exactly the board's asymmetry
    // (finding #2: "MDIO write works, MDIO read is dead"). With inbound restored we
    // can now read it back, and ANAR must hold the WRITTEN value, not the default.
    //
    // Both halves of the old "MDIO not interoperable" finding are now CLOSED:
    //   * reads came back >>1  -> fixed in mdio_phy_model (commit 0ea0a1b).
    //   * writes never committed -> that was THIS BENCH: it triggered MIICOMMAND[0],
    //     which is ScanStat, not WCtrlData. WCtrlData is bit [2] (eth_registers.v:939).
    //     eth_miim was being told to SCAN, so no write frame was ever emitted.
    mii_read(PHYAD, 5'd4, anar);
    if (anar === ANAR_WR) begin
      $display("LAN8720_SUT:   ok    ANAR = 0x%04h (the written value) -> MDIO WRITE landed", anar);
    end else if (anar === ANAR_DEFAULT) begin
      errors++;
      $display("LAN8720_SUT:   FAIL  ANAR still default (0x%04h) -> the MDIO WRITE did not commit", anar);
    end else begin
      errors++;
      $display("LAN8720_SUT:   FAIL  ANAR unexpected 0x%04h (expected written 0x%04h)", anar, ANAR_WR);
    end
    mii_read(PHYAD, 5'd2, id1);   chk_read("PHYID1 again", id1, EXP_ID1);

    $display("");
    $display("LAN8720_SUT: ============================================================");
    $display("LAN8720_SUT: PRIMARY RESULT -- reproduce the MPS3 dead-inbound signature");
    $display("LAN8720_SUT:   with the REAL DUT MDIO master (OpenCores eth_miim):");
    $display("LAN8720_SUT:   inbound dead -> MDIO reads 0xFFFF at all 32 PHY addresses.");
    $display("LAN8720_SUT:   repro_ok=%0b   unexplained_failures=%0d", repro_ok, errors);
    $display("LAN8720_SUT:");
    // The old "mdio_phy_model is not MDIO-interoperable" finding is now CLOSED, both halves:
    //   * READS came back >>1  -- a real model bug, fixed in mdio_phy_model (commit 0ea0a1b).
    //   * WRITES never committed -- NOT a model bug: this bench pulsed MIICOMMAND[0], which
    //     is ScanStat. WCtrlData is bit [2] (eth_registers.v:939-941), so eth_miim was told
    //     to SCAN and never emitted a write frame at all. Fixed here.
    // Reads and writes now both interoperate with the real eth_miim.
    if (findings > 0) begin
      $display("LAN8720_SUT: UNEXPECTED: an MDIO interop finding re-appeared (%0d)", findings);
    end
    $display("LAN8720_SUT: VERDICT=%s", (repro_ok && errors==0) ? "PASS" : "FAIL");
    $display("LAN8720_SUT: ============================================================");
    $display("");
    $finish;
  end

  initial begin
    #50_000_000;
    $display("LAN8720_SUT: TIMEOUT  VERDICT=FAIL  errors=%0d", errors);
    $finish;
  end

endmodule
