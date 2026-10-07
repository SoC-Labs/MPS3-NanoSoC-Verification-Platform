// =============================================================================
// tests/shield_path/shield_path_tb.sv
//
// Reproduces the MPS3 LAN8720 failure signature in simulation, using this
// repo's own Clause-22 virtual PHY (fpga/ethernet/mdio_phy_model) behind a
// model of the MPS3 shield/PMOD path (shield_path.sv).
//
// The board findings this bench encodes (mps3_ethernet_debug_plan.md, §
// "Diagnostic findings (2026-04-22 session)"):
//
//   #2  MDIO *write* works   -- BMCR.RESET makes the PHY's LEDs blink, so
//                               FPGA->PHY signalling is electrically fine.
//   #3  MDIO *read* is dead  -- BMSR returns 0xFFFF at every PHY address 0..31.
//   #5  The PHY itself is fine -- it links and auto-negotiates on the line side.
//
// Three phases:
//   P1  healthy shield        -> PHYID reads back 0x0007 / 0xC0F1
//   P2  inbound channel dead  -> EVERY read at EVERY PHY address is 0xFFFF,
//                                yet a write to ANAR still reaches the PHY
//   P3  inbound restored      -> the ANAR written during P2 reads back
//
// P3 is the crux: it proves the write landed while the reads were dead. That is
// exactly the asymmetry observed on the board, and it is what distinguishes a
// dead inbound channel from a dead PHY, a wrong PHY address, or a dead MAC.
// =============================================================================
`timescale 1ns / 1ps

module shield_path_tb;

  localparam int  MDC_T       = 400;          // 400 ns -> 2.5 MHz MDC
  localparam logic [4:0] PHYAD = 5'd1;        // mdio_phy_model C_PHY_ADDR default
  localparam logic [15:0] EXP_PHYID1 = 16'h0007;
  localparam logic [15:0] EXP_PHYID2 = 16'hC0F1;
  localparam logic [15:0] ANAR_DEFAULT = 16'h01E1;
  localparam logic [15:0] ANAR_100_ONLY = 16'h0181;  // what the real firmware writes

  // ---- AXI-Lite side of the PHY model: clocked, reset released, idle --------
  logic aclk = 1'b0;
  always #5 aclk = ~aclk;                     // 100 MHz
  logic aresetn = 1'b0;

  // ---- FPGA (A) side signals -------------------------------------------------
  logic a_mdc = 1'b0, a_mdio_o = 1'b0, a_mdio_oe = 1'b0;
  wire  a_mdio_i;
  wire  a_rmii_crs_dv, a_rmii_ref_clk;
  wire [1:0] a_rmii_rxd;

  // ---- shield direction control ---------------------------------------------
  logic pass_a2b = 1'b1;
  logic pass_b2a = 1'b1;

  // ---- PHY (B) side ----------------------------------------------------------
  wire b_mdc, b_mdio_o, b_mdio_oe, b_mdio_i;

  shield_path u_shield (
    .pass_a2b      (pass_a2b),
    .pass_b2a      (pass_b2a),
    .a_mdc         (a_mdc),
    .a_mdio_o      (a_mdio_o),
    .a_mdio_oe     (a_mdio_oe),
    .a_mdio_i      (a_mdio_i),
    .a_rmii_crs_dv (a_rmii_crs_dv),
    .a_rmii_rxd    (a_rmii_rxd),
    .a_rmii_ref_clk(a_rmii_ref_clk),
    .b_mdc         (b_mdc),
    .b_mdio_o      (b_mdio_o),
    .b_mdio_oe     (b_mdio_oe),
    .b_mdio_i      (b_mdio_i),
    .b_rmii_crs_dv (1'b0),          // PHY idle: no carrier
    .b_rmii_rxd    (2'b00),
    .b_rmii_ref_clk(1'b0)
  );

  // ---- the virtual PHY (this repo's own Clause-22 model) ---------------------
  mdio_phy_model u_phy (
    .s_axi_aclk    (aclk),
    .s_axi_aresetn (aresetn),
    .s_axi_awaddr  ('0),
    .s_axi_awprot  ('0),
    .s_axi_awvalid (1'b0),
    .s_axi_awready (),
    .s_axi_wdata   ('0),
    .s_axi_wstrb   ('0),
    .s_axi_wvalid  (1'b0),
    .s_axi_wready  (),
    .s_axi_bresp   (),
    .s_axi_bvalid  (),
    .s_axi_bready  (1'b1),
    .s_axi_araddr  ('0),
    .s_axi_arprot  ('0),
    .s_axi_arvalid (1'b0),
    .s_axi_arready (),
    .s_axi_rdata   (),
    .s_axi_rresp   (),
    .s_axi_rvalid  (),
    .s_axi_rready  (1'b1),
    .mdc_i         (b_mdc),
    .mdio_o_i      (b_mdio_o),
    .mdio_oe_i     (b_mdio_oe),
    .mdio_i_o      (b_mdio_i)
  );

  // ===========================================================================
  // Clause-22 MDIO master (bench-only; the shell has no MDIO master today)
  // ===========================================================================
  task automatic mdc_drive(input logic b);
    a_mdio_oe = 1'b1;
    a_mdio_o  = b;
    #(MDC_T/2) a_mdc = 1'b1;      // slave samples on the rising edge
    #(MDC_T/2) a_mdc = 1'b0;
  endtask

  task automatic mdc_sample(output logic b);
    a_mdio_oe = 1'b0;             // master releases the bus
    a_mdio_o  = 1'b0;
    #(MDC_T/2) a_mdc = 1'b1;
    #(MDC_T/4) b = a_mdio_i;      // sample mid-high, data is stable
    #(MDC_T/4) a_mdc = 1'b0;
  endtask

  task automatic mdio_preamble();
    int i;
    for (i = 0; i < 32; i++) mdc_drive(1'b1);
  endtask

  task automatic mdio_read(input logic [4:0] pa, input logic [4:0] ra,
                           output logic [15:0] data);
    int i; logic b;
    mdio_preamble();
    mdc_drive(1'b0); mdc_drive(1'b1);          // ST = 01
    mdc_drive(1'b1); mdc_drive(1'b0);          // OP = 10 (read)
    for (i = 4; i >= 0; i--) mdc_drive(pa[i]);
    for (i = 4; i >= 0; i--) mdc_drive(ra[i]);
    mdc_sample(b); mdc_sample(b);              // TA: master releases
    for (i = 15; i >= 0; i--) begin mdc_sample(b); data[i] = b; end
    a_mdio_oe = 1'b0;
    repeat (2) mdc_drive(1'b1);                // idle
    a_mdio_oe = 1'b0;
  endtask

  task automatic mdio_write(input logic [4:0] pa, input logic [4:0] ra,
                            input logic [15:0] data);
    int i;
    mdio_preamble();
    mdc_drive(1'b0); mdc_drive(1'b1);          // ST = 01
    mdc_drive(1'b0); mdc_drive(1'b1);          // OP = 01 (write)
    for (i = 4; i >= 0; i--) mdc_drive(pa[i]);
    for (i = 4; i >= 0; i--) mdc_drive(ra[i]);
    mdc_drive(1'b1); mdc_drive(1'b0);          // TA = 10 (master drives)
    for (i = 15; i >= 0; i--) mdc_drive(data[i]);
    a_mdio_oe = 1'b0;
  endtask

  // ===========================================================================
  int errors = 0;
  int addr;
  logic [15:0] id1, id2, anar, dummy;
  int all_ff;

  task automatic chk(input string what, input logic [15:0] got,
                     input logic [15:0] exp);
    if (got !== exp) begin
      errors++;
      $display("SHIELD_PATH:   FAIL  %-28s got=0x%04h expected=0x%04h", what, got, exp);
    end else begin
      $display("SHIELD_PATH:   ok    %-28s     0x%04h", what, got);
    end
  endtask

  initial begin
    repeat (8) @(posedge aclk);
    aresetn = 1'b1;
    repeat (8) @(posedge aclk);

    // ---------------------------------------------------------------- P1 -----
    $display("");
    $display("SHIELD_PATH: PHASE 1 -- healthy shield (both directions pass)");
    pass_a2b = 1'b1; pass_b2a = 1'b1;
    mdio_read(PHYAD, 5'd2, id1);   chk("PHYID1 (reg 2)", id1, EXP_PHYID1);
    mdio_read(PHYAD, 5'd3, id2);   chk("PHYID2 (reg 3)", id2, EXP_PHYID2);
    mdio_read(PHYAD, 5'd4, anar);  chk("ANAR   (reg 4)", anar, ANAR_DEFAULT);

    // ---------------------------------------------------------------- P2 -----
    $display("");
    $display("SHIELD_PATH: PHASE 2 -- inbound (PHY->FPGA) channel DEAD");
    $display("SHIELD_PATH:   board finding #3: BMSR reads 0xFFFF at every PHY address");
    pass_b2a = 1'b0;

    all_ff = 1;
    for (addr = 0; addr < 32; addr++) begin
      mdio_read(addr[4:0], 5'd1, dummy);   // BMSR
      if (dummy !== 16'hFFFF) all_ff = 0;
    end
    if (all_ff) $display("SHIELD_PATH:   ok    BMSR = 0xFFFF at all 32 PHY addresses");
    else begin errors++; $display("SHIELD_PATH:   FAIL  some BMSR read was not 0xFFFF"); end

    $display("SHIELD_PATH:   board finding #2: a WRITE still reaches the PHY");
    mdio_write(PHYAD, 5'd4, ANAR_100_ONLY);   // the real firmware's ANAR=0x0181

    mdio_read(PHYAD, 5'd4, anar);
    chk("ANAR readback while dead", anar, 16'hFFFF);

    // ---------------------------------------------------------------- P3 -----
    $display("");
    $display("SHIELD_PATH: PHASE 3 -- inbound restored; did the P2 write land?");
    pass_b2a = 1'b1;
    mdio_read(PHYAD, 5'd4, anar);
    chk("ANAR after restore", anar, ANAR_100_ONLY);
    mdio_read(PHYAD, 5'd2, id1);  chk("PHYID1 again", id1, EXP_PHYID1);

    // ---------------------------------------------------------------- P4 -----
    // NEGATIVE CONTROL. A bench that only ever confirms its own hypothesis is
    // worthless. Cut the OUTBOUND path too: now a write must NOT reach the PHY.
    // If ANAR changes here, the model is passing writes through a channel it
    // claims is dead, and nothing else this bench says can be believed.
    $display("");
    $display("SHIELD_PATH: PHASE 4 -- negative control: outbound ALSO dead, write must not land");
    pass_a2b = 1'b0; pass_b2a = 1'b0;
    mdio_write(PHYAD, 5'd4, 16'h0000);          // would be visible if it landed
    pass_a2b = 1'b1; pass_b2a = 1'b1;
    mdio_read(PHYAD, 5'd4, anar);
    chk("ANAR unchanged by dead write", anar, ANAR_100_ONLY);

    $display("");
    $display("SHIELD_PATH: errors=%0d  VERDICT=%s", errors, (errors == 0) ? "PASS" : "FAIL");
    $display("SHIELD_PATH: a dead inbound channel is indistinguishable from a dead PHY,");
    $display("SHIELD_PATH: a wrong PHY address, or a dead MAC -- by reads alone.");
    $display("SHIELD_PATH: Only write-then-verify separates them. On the real board the");
    $display("SHIELD_PATH: 'verify' is the PHY's LEDs responding to a BMCR write (finding #2).");
    $display("");
    $finish;
  end

  initial begin
    #20_000_000;
    $display("SHIELD_PATH: TIMEOUT  VERDICT=FAIL");
    $finish;
  end

endmodule
