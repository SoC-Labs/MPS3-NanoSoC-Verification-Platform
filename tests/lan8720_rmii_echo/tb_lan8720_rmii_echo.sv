// =============================================================================
// tb_lan8720_rmii_echo.sv -- cocotb TB top for the DUT MAC's RMII path against a
// REAL LAN8720 behavioral model (lan8720_model.sv), NOT the repo's
// mdio_phy_model. Proves, board-free, the thing the SHELL_REALPHY shell variant
// needs: a frame driven at the RMII pads is received and echoed through
// rmii_to_mii -> MAC -> DMA and back.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
// license.  Copyright (C) 2026, SoC Labs (www.soclabs.org)
//
// DUT: ethmac_ahb_rmii (OpenCores ethmac + eth_miim MDIO master + rmii_to_mii),
//      the SAME real MAC + RMII bridge the rm_eth_ss RM instantiates. Its AHB
//      register slave is driven by cocotb (tests/common/ahb_lite.py); its AHB
//      DMA master is answered by the pure-SV slave RAM below (ported from
//      tests/lan8720_sut/tb_lan8720_dp.sv, the proven datapath scaffold);
//      cocotb reads/writes that RAM hierarchically for frame preload/readback.
//
// CLOCKING: Option A -- the TB (standing in for the FPGA shell) SOURCES the
// 50 MHz REF_CLK and feeds it to BOTH the MAC and the LAN8720 model.
//
// mode_speed: driven from a reg (default 1 = 100 Mbps), overridable with
// +MODE_SPEED=<0|1> so the bench can prove the wire is load-bearing (an
// unconnected/tied-0 mode_speed kills 100M RX -- the multicore-wrapper bug).
// =============================================================================
`timescale 1ns / 1ps

module tb_lan8720_rmii_echo;

  // ---- clocks (SV-generated so they run from t0; cocotb uses their edges) ----
  reg hclk    = 1'b0;  always #20 hclk    = ~hclk;   // 25 MHz AHB/system
  reg ref_clk = 1'b0;  always #10 ref_clk = ~ref_clk;// 50 MHz RMII reference

  // ---- cocotb-driven controls (plain regs; no SV process drives them) --------
  reg        hresetn = 1'b0;

  // mode_speed: default 100 Mbps; +MODE_SPEED=0 forces the guard case.
  reg        mode_speed = 1'b1;
  integer    ms_pa;
  initial if ($value$plusargs("MODE_SPEED=%d", ms_pa)) mode_speed = ms_pa[0];

  // LAN8720 model controls
  reg        loopback_en  = 1'b0;
  reg        rx_inj_en    = 1'b0;
  reg  [1:0] rx_inj_dibit = 2'b00;
  reg        rx_inj_crs   = 1'b0;

  // ---- AHB register-slave side of the DUT (cocotb = AhbLiteMaster) -----------
  reg        hsel_s  = 1'b0;
  reg  [31:0] haddr_s = 32'h0;
  reg  [1:0] htrans_s = 2'b00;
  reg  [2:0] hsize_s  = 3'b010;
  reg  [2:0] hburst_s = 3'b000;
  reg  [3:0] hprot_s  = 4'b0011;
  reg        hwrite_s = 1'b0;
  reg [31:0] hwdata_s = 32'h0;
  wire [31:0] hrdata_s;
  wire        hreadyout_s, hresp_s;
  wire        hready_s = hreadyout_s;    // single-slave AHB-Lite loop

  // ---- AHB DMA-master side of the DUT ---------------------------------------
  wire [31:0] m_haddr, m_hwdata;
  wire  [1:0] m_htrans;
  wire  [2:0] m_hsize, m_hburst;
  wire  [3:0] m_hprot;
  wire        m_hmastlock, m_hwrite;
  reg  [31:0] m_hrdata;
  wire        m_hready = 1'b1;           // zero wait states
  wire        m_hresp  = 1'b0;           // OKAY

  // ---- RMII / MDIO between the MAC and the LAN8720 model ----------------------
  wire [1:0] rmii_txd;  wire rmii_tx_en;
  wire [1:0] rmii_rxd;  wire rmii_crs_dv;
  wire       mdc_pad_o, md_pad_o, md_padoe_o, md_pad_i;
  wire       int_o;

  // ---- DUT: real RMII-native OpenCores MAC ----------------------------------
  ethmac_ahb_rmii dut (
    .hclk(hclk), .hresetn(hresetn),
    // AHB DMA master -> slave RAM
    .haddr(m_haddr), .htrans(m_htrans), .hsize(m_hsize), .hburst(m_hburst),
    .hprot(m_hprot), .hmastlock(m_hmastlock), .hwrite(m_hwrite), .hwdata(m_hwdata),
    .hrdata(m_hrdata), .hready(m_hready), .hresp(m_hresp),
    // AHB register slave <- cocotb
    .hsel_s(hsel_s), .haddr_s(haddr_s), .htrans_s(htrans_s), .hsize_s(hsize_s),
    .hburst_s(hburst_s), .hprot_s(hprot_s), .hwrite_s(hwrite_s), .hwdata_s(hwdata_s),
    .hready_s(hready_s), .hrdata_s(hrdata_s), .hreadyout_s(hreadyout_s), .hresp_s(hresp_s),
    // RMII <-> LAN8720 model
    .rmii_ref_clk(ref_clk),
    .rmii_txd(rmii_txd), .rmii_tx_en(rmii_tx_en),
    .rmii_rxd(rmii_rxd), .rmii_crs_dv(rmii_crs_dv),
    .mode_speed(mode_speed),
    // MDIO <-> LAN8720 model
    .md_pad_i(md_pad_i), .mdc_pad_o(mdc_pad_o), .md_pad_o(md_pad_o),
    .md_padoe_o(md_padoe_o), .int_o(int_o)
  );

  // ---- the real LAN8720 PHY model -------------------------------------------
  lan8720_model #(.PHY_ADDR(5'd1)) u_phy (
    .ref_clk(ref_clk), .resetn(hresetn),
    .mac_txd(rmii_txd), .mac_tx_en(rmii_tx_en),
    .mac_rxd(rmii_rxd), .mac_crs_dv(rmii_crs_dv),
    .mdc(mdc_pad_o), .mdio_o(md_pad_o), .mdio_oe(md_padoe_o), .mdio_i(md_pad_i),
    .loopback_en(loopback_en),
    .rx_inj_en(rx_inj_en), .rx_inj_dibit(rx_inj_dibit), .rx_inj_crs(rx_inj_crs)
  );

  // ---- AHB-lite slave RAM for the DUT's DMA master (16 KiB) ------------------
  // Word-addressable, zero-wait. cocotb preloads/reads frames here (big-endian
  // byte layout matched in Python). Same model as tb_lan8720_dp.sv.
  localparam RAM_WORDS = 4096;
  reg [31:0] ram [0:RAM_WORDS-1];
  reg        a_valid;
  reg        a_write;
  reg [11:0] a_word;
  always @(posedge hclk or negedge hresetn) begin
    if (!hresetn) begin
      a_valid <= 1'b0; a_write <= 1'b0; a_word <= '0; m_hrdata <= '0;
    end else begin
      if (m_htrans[1]) begin
        a_valid <= 1'b1; a_write <= m_hwrite; a_word <= m_haddr[13:2];
      end else begin
        a_valid <= 1'b0;
      end
      if (a_valid && a_write) ram[a_word] <= m_hwdata;
      m_hrdata <= ram[m_haddr[13:2]];
    end
  end

  // ---- RMII TX egress decoder (cocotb reads tx_cap/tx_bytes) -----------------
  // 100 Mb/s: one di-bit per ref_clk while tx_en. LSB-pair first. Independent
  // proof of TX egress alongside the DMA-based echo check.
  reg  [7:0] tx_cap [0:1599];
  integer    tx_bytes = 0;
  integer    tx_frames = 0;
  reg        tx_en_d = 1'b0;
  reg  [3:0] tx_dibcnt = 0;
  reg  [7:0] tx_acc;
  always @(posedge ref_clk) begin
    tx_en_d <= rmii_tx_en;
    if (rmii_tx_en && !tx_en_d) tx_frames <= tx_frames + 1;
    if (rmii_tx_en) begin
      tx_acc = {rmii_txd, tx_acc[7:2]};
      tx_dibcnt <= tx_dibcnt + 1;
      if (tx_dibcnt == 3) begin
        if (tx_bytes < 1600) tx_cap[tx_bytes] = tx_acc;
        tx_bytes  <= tx_bytes + 1;
        tx_dibcnt <= 0;
      end
    end else begin
      tx_dibcnt <= 0;
    end
  end

  // ---- init + safety backstop ------------------------------------------------
  integer i;
  initial for (i = 0; i < RAM_WORDS; i = i + 1) ram[i] = 32'h0;

  initial begin
    #200_000_000;   // 200 ms hard backstop (cocotb per-test timeouts are tighter)
    $display("[%0t] TB WATCHDOG: hard $finish backstop", $time);
    $finish;
  end

`ifdef WAVES
  initial begin
    $dumpfile("waves.vcd");
    $dumpvars(0, tb_lan8720_rmii_echo);
  end
`endif

endmodule
