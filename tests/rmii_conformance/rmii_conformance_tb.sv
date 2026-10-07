// -----------------------------------------------------------------------------
// rmii_conformance_tb.sv
//
// CROSS-IMPLEMENTATION RMII conformance bench (closes the RMII half of I22).
//
// Wires our shell-side virtual PHY (fpga/ethernet/rmii_phy_if/rmii_phy_if.sv)
// BACK-TO-BACK across the RMII partition wires with an INDEPENDENT, third-party
// MAC-side bridge (ethernet-mac-ahb/amba_wb_bridges/src/rtl/rmii_to_mii.v — the
// exact module rp_eth_ss_wrapper.sv instantiates inside the DUT). On real
// hardware these two blocks face each other across the shell<->DUT partition
// boundary. If their RMII nibble / di-bit orders disagreed, every frame would
// scramble. Passing a known ASYMMETRIC byte pattern byte-exact through both
// directions is therefore a genuine conformance check, NOT a self-check
// (tests/rmii_phy_if/ only ever drives our own RTL against our own RTL).
//
//   Direction A (MAC -> PHY): drive an MII nibble stream into rmii_to_mii's
//     MII TX side (mtxd/mtxen); it emits RMII (rmii_txd/rmii_tx_en); that feeds
//     rmii_phy_if's RMII RX pins (phy_rmii_txd_i/phy_rmii_tx_en_i); we capture
//     the MII bytes rmii_phy_if produces (mii_rxd_o/mii_rx_dv_o). This path
//     uses the dedicated TX_EN envelope.
//
//   Direction B (PHY -> MAC): drive an MII nibble stream into rmii_phy_if's
//     MII TX side (mii_txd_i/mii_tx_en_i); it emits RMII (phy_rmii_rxd_o/
//     phy_rmii_crs_dv_o); that feeds rmii_to_mii's RMII RX (rmii_rxd/
//     rmii_crs_dv); we capture the MII bytes rmii_to_mii produces (mrxd/mrxdv).
//     This path exercises rmii_to_mii's IEEE-802.3u CRS_DV decode FSM, which
//     locks di-bit alignment on the first pair of non-zero di-bits -- so the
//     stream MUST begin with a real preamble (0x55...), exactly what a MAC
//     transmits. rmii_phy_if does no preamble insertion, so the preamble is
//     supplied here as ordinary MII data (link_partner_mac's role).
//
// Clocking: one 50 MHz shell REF_CLK (20 ns). rmii_phy_if sources it on
// phy_rmii_ref_clk_o and it is forwarded to rmii_to_mii.rmii_ref_clk (both
// sides share the reference by construction -- RMII is synchronous to REF_CLK).
// Each block divides by 2 internally for its 25 MHz MII nibble clocks. Drivers
// pace one nibble per MII-clock period; monitors sample one nibble per period.
//
// Self-checking: prints the driven vs. recovered byte stream for each
// direction and a final "CONFORMANCE: PASS/FAIL" verdict line.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rmii_conformance_tb;

  // ---------------------------------------------------------------------------
  // 50 MHz shell reference clock + reset.
  // ---------------------------------------------------------------------------
  logic refclk = 1'b0;
  always #10 refclk = ~refclk;          // 20 ns period = 50 MHz

  logic rst = 1'b1;                      // rmii_phy_if active-high reset
  wire  resetn = ~rst;                   // rmii_to_mii active-low reset

  // ---------------------------------------------------------------------------
  // RMII wires across the (virtual) partition boundary.
  //   phy_ref_clk : shell sources REF_CLK (rmii_phy_if -> rmii_to_mii)
  //   MAC transmit (TX_EN dedicated): rmii_to_mii.rmii_txd/tx_en -> rmii_phy_if
  //   PHY drive   (CRS_DV muxed)     : rmii_phy_if.rxd/crs_dv    -> rmii_to_mii
  // ---------------------------------------------------------------------------
  logic       phy_ref_clk;
  logic       phy_crs_dv;
  logic [1:0] phy_rxd;
  logic [1:0] phy_txd;
  logic       phy_tx_en;

  // ---------------------------------------------------------------------------
  // Shell-internal MII of rmii_phy_if (the link_partner_mac side).
  // ---------------------------------------------------------------------------
  logic [3:0] pif_mii_rxd;
  logic       pif_mii_rx_dv;
  logic       pif_mii_rx_er;
  logic       pif_mii_rx_clk;
  logic [3:0] pif_mii_txd   = 4'h0;
  logic       pif_mii_tx_en = 1'b0;
  logic       pif_mii_tx_clk;

  // ---------------------------------------------------------------------------
  // MII of rmii_to_mii (the MAC side).
  // ---------------------------------------------------------------------------
  logic       mac_mtx_clk;
  logic [3:0] mac_mtxd   = 4'h0;
  logic       mac_mtxen  = 1'b0;
  logic       mac_mtxerr = 1'b0;
  logic       mac_mrx_clk;
  logic [3:0] mac_mrxd;
  logic       mac_mrxdv;
  logic       mac_mrxerr;
  logic       mac_mcoll;
  logic       mac_mcrs;

  // ===========================================================================
  // DUT #1 -- our shell-side virtual PHY (RMII <-> MII datapath).
  // ===========================================================================
  rmii_phy_if u_phy (
    .refclk_i           (refclk),
    .rst_i              (rst),
    .phy_rmii_ref_clk_o (phy_ref_clk),
    .phy_rmii_crs_dv_o  (phy_crs_dv),
    .phy_rmii_rxd_o     (phy_rxd),
    .phy_rmii_txd_i     (phy_txd),
    .phy_rmii_tx_en_i   (phy_tx_en),
    .mii_rxd_o          (pif_mii_rxd),
    .mii_rx_dv_o        (pif_mii_rx_dv),
    .mii_rx_er_o        (pif_mii_rx_er),
    .mii_rx_clk_o       (pif_mii_rx_clk),
    .mii_txd_i          (pif_mii_txd),
    .mii_tx_en_i        (pif_mii_tx_en),
    .mii_tx_clk_o       (pif_mii_tx_clk)
  );

  // ===========================================================================
  // DUT #2 -- third-party MAC-side RMII<->MII bridge (independent RTL).
  // Shares the shell-sourced REF_CLK. RMII wires cross-connected as on HW.
  // ===========================================================================
  rmii_to_mii u_mac (
    .RESETn       (resetn),
    .rmii_ref_clk (phy_ref_clk),
    .rmii_txd     (phy_txd),
    .rmii_tx_en   (phy_tx_en),
    .rmii_rxd     (phy_rxd),
    .rmii_crs_dv  (phy_crs_dv),
    .mtx_clk      (mac_mtx_clk),
    .mtxd         (mac_mtxd),
    .mtxen        (mac_mtxen),
    .mtxerr       (mac_mtxerr),
    .mrx_clk      (mac_mrx_clk),
    .mrxd         (mac_mrxd),
    .mrxdv        (mac_mrxdv),
    .mrxerr       (mac_mrxerr),
    .mcoll        (mac_mcoll),
    .mcrs         (mac_mcrs)
  );

  // ---------------------------------------------------------------------------
  // Test vector: a realistic frame prefix with an intentionally ASYMMETRIC,
  // non-palindromic, not-all-same payload so a nibble swap or di-bit reversal
  // scrambles it detectably. Preamble(7x55)+SFD(D5) is required for direction
  // B's CRS_DV-decode alignment (and is what a real MAC sends).
  // ---------------------------------------------------------------------------
  localparam int N = 30;
  logic [7:0] vec [0:N-1];

  // recovered-nibble queues (first nibble = low nibble of the byte)
  logic [3:0] recA [$];
  logic [3:0] recB [$];

  int errors_A = 0;
  int errors_B = 0;

  // ---------------------------------------------------------------------------
  // Driver A: MAC-side MII TX into rmii_to_mii (one nibble per mtx_clk period,
  // low nibble first).
  // ---------------------------------------------------------------------------
  task automatic drive_A;
    @(posedge mac_mtx_clk);
    for (int i = 0; i < N; i++) begin
      mac_mtxen <= 1'b1; mac_mtxd <= vec[i][3:0];   // low nibble
      @(posedge mac_mtx_clk);
      mac_mtxen <= 1'b1; mac_mtxd <= vec[i][7:4];   // high nibble
      @(posedge mac_mtx_clk);
    end
    mac_mtxen <= 1'b0; mac_mtxd <= 4'h0;
  endtask

  // Monitor A: rmii_phy_if MII RX out (sample one nibble per mii_rx_clk period).
  task automatic mon_A;
    recA = {};
    do @(posedge pif_mii_rx_clk); while (pif_mii_rx_dv !== 1'b1);
    while (pif_mii_rx_dv === 1'b1) begin
      recA.push_back(pif_mii_rxd);
      @(posedge pif_mii_rx_clk);
    end
  endtask

  // ---------------------------------------------------------------------------
  // Driver B: PHY-side MII TX into rmii_phy_if (one nibble per tx_clk period).
  // ---------------------------------------------------------------------------
  task automatic drive_B;
    @(posedge pif_mii_tx_clk);
    for (int i = 0; i < N; i++) begin
      pif_mii_tx_en <= 1'b1; pif_mii_txd <= vec[i][3:0];   // low nibble
      @(posedge pif_mii_tx_clk);
      pif_mii_tx_en <= 1'b1; pif_mii_txd <= vec[i][7:4];   // high nibble
      @(posedge pif_mii_tx_clk);
    end
    pif_mii_tx_en <= 1'b0; pif_mii_txd <= 4'h0;
  endtask

  // Monitor B: rmii_to_mii MII RX out (sample one nibble per mrx_clk period).
  task automatic mon_B;
    recB = {};
    do @(posedge mac_mrx_clk); while (mac_mrxdv !== 1'b1);
    while (mac_mrxdv === 1'b1) begin
      recB.push_back(mac_mrxd);
      @(posedge mac_mrx_clk);
    end
  endtask

  // ---------------------------------------------------------------------------
  // Pack recovered nibbles into bytes and compare against the driven vector.
  // Returns the error count; prints driven vs. recovered streams.
  // ---------------------------------------------------------------------------
  function automatic int check_dir(string label, ref logic [3:0] nibs [$]);
    int      errs;
    logic [7:0] got;
    errs = 0;

    $write("[%s] driven    (%0d bytes):", label, N);
    for (int i = 0; i < N; i++) $write(" %02x", vec[i]);
    $write("\n");

    $write("[%s] recovered (%0d nibbles):", label, nibs.size());
    for (int i = 0; i < nibs.size(); i++) $write(" %01x", nibs[i]);
    $write("\n");

    if (nibs.size() != 2*N) begin
      $display("[%s] NIBBLE-COUNT MISMATCH: expected %0d, got %0d",
               label, 2*N, nibs.size());
      errs++;
    end

    $write("[%s] recovered (bytes):        ", label);
    for (int i = 0; i < N; i++) begin
      if (2*i+1 < nibs.size()) begin
        got = {nibs[2*i+1], nibs[2*i]};   // first nibble is the low nibble
        $write(" %02x", got);
        if (got !== vec[i]) errs++;
      end else begin
        $write(" --");
        errs++;
      end
    end
    $write("\n");

    for (int i = 0; i < N; i++) begin
      if (2*i+1 < nibs.size()) begin
        got = {nibs[2*i+1], nibs[2*i]};
        if (got !== vec[i])
          $display("[%s]   byte %0d MISMATCH: driven %02x recovered %02x",
                   label, i, vec[i], got);
      end
    end

    if (errs == 0) $display("[%s] RESULT: byte-exact (0 errors)", label);
    else           $display("[%s] RESULT: %0d error(s)", label, errs);
    return errs;
  endfunction

  // ---------------------------------------------------------------------------
  // Watchdog -- only fires on a genuine hang (never on a data mismatch).
  // ---------------------------------------------------------------------------
  initial begin
    #500000;
    $display("CONFORMANCE: FAIL (watchdog timeout -- a monitor never saw its data-valid; check clocking/reset)");
    $fatal(1, "watchdog timeout");
  end

  // ---------------------------------------------------------------------------
  // Stimulus.
  // ---------------------------------------------------------------------------
  initial begin
    // preamble x7
    vec[0]=8'h55; vec[1]=8'h55; vec[2]=8'h55; vec[3]=8'h55;
    vec[4]=8'h55; vec[5]=8'h55; vec[6]=8'h55;
    // SFD
    vec[7]=8'hD5;
    // DA
    vec[8]=8'h00; vec[9]=8'h11; vec[10]=8'h22; vec[11]=8'h33; vec[12]=8'h44; vec[13]=8'h55;
    // SA
    vec[14]=8'hAA; vec[15]=8'hBB; vec[16]=8'hCC; vec[17]=8'hDD; vec[18]=8'hEE; vec[19]=8'hFF;
    // EtherType (IPv4)
    vec[20]=8'h08; vec[21]=8'h00;
    // asymmetric payload -- the tell-tale pattern
    vec[22]=8'h01; vec[23]=8'h23; vec[24]=8'h45; vec[25]=8'h67;
    vec[26]=8'h89; vec[27]=8'hAB; vec[28]=8'hCD; vec[29]=8'hEF;

    $display("=====================================================================");
    $display("RMII cross-implementation conformance:  rmii_phy_if  <-> rmii_to_mii");
    $display("  DUT#1 (shell PHY): fpga/ethernet/rmii_phy_if/rmii_phy_if.sv");
    $display("  DUT#2 (MAC bridge): ethernet-mac-ahb/amba_wb_bridges/src/rtl/rmii_to_mii.v");
    $display("=====================================================================");

    // reset both blocks
    rst = 1'b1;
    mac_mtxen = 1'b0; mac_mtxd = 4'h0; mac_mtxerr = 1'b0;
    pif_mii_tx_en = 1'b0; pif_mii_txd = 4'h0;
    repeat (10) @(posedge refclk);
    rst = 1'b0;
    repeat (24) @(posedge refclk);      // let reset synchronisers + /2 clocks settle

    // ---- Direction A: MAC -> PHY -----------------------------------------
    $display("--- Direction A: MAC(rmii_to_mii TX) -> RMII -> PHY(rmii_phy_if RX) ---");
    fork
      drive_A();
      mon_A();
    join
    repeat (20) @(posedge refclk);
    errors_A = check_dir("A: MAC->PHY", recA);

    repeat (40) @(posedge refclk);      // inter-frame idle

    // ---- Direction B: PHY -> MAC -----------------------------------------
    $display("--- Direction B: PHY(rmii_phy_if TX) -> RMII -> MAC(rmii_to_mii RX) ---");
    fork
      drive_B();
      mon_B();
    join
    repeat (20) @(posedge refclk);
    errors_B = check_dir("B: PHY->MAC", recB);

    // ---- Verdict ----------------------------------------------------------
    $display("=====================================================================");
    $display("Direction A errors: %0d   Direction B errors: %0d", errors_A, errors_B);
    if (errors_A == 0 && errors_B == 0)
      $display("CONFORMANCE: PASS  (byte-exact both directions, cross-implementation)");
    else
      $display("CONFORMANCE: FAIL  (see per-byte mismatches above)");
    $display("=====================================================================");
    $finish;
  end

endmodule
