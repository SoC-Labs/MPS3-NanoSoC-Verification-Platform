// -----------------------------------------------------------------------------
// tb_lcd_mirror_e2e.sv -- LCDMIR bolted onto the CLCD-KVM end-to-end harness.
//
// tests/clcd_kvm_e2e/tb_clcd_kvm_e2e.sv is instantiated AS-IS (read-only
// reuse): REAL clcd_kvm + the W3-A tunnel mux + nanosoc_exp_socket + ahb_clcd +
// clcd_core, harness side driven by kvm_models.HarnessSource. Every port of it
// is forwarded under its own name, so the existing BFMs bind unchanged. The
// snooper taps the KVM's pad-side outputs exactly as the mint-4 BD will
// (LCD_MIRROR_FPGA.md §2.1: an input-only fan-out of the nine pad nets plus
// owner_o), and gets its own AXI-Lite port (lm_axi_*) plus the SV frame reader.
//
// Both clocks are generated here (a cocotb clock costs a VPI callback per edge).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_lcd_mirror_e2e (
  output logic        s_axi_aclk,     // 100 MHz shell domain
  input  logic        s_axi_aresetn,
  output logic        hclk,           // 50 MHz DUT domain
  input  logic        hresetn,

  // ==== nanosoc_exp_socket AHB-Lite slave ====================================
  input  logic        hsel,
  input  logic [31:0] haddr,
  input  logic [1:0]  htrans,
  input  logic        hwrite,
  input  logic [2:0]  hsize,
  input  logic [2:0]  hburst,
  input  logic [3:0]  hprot,
  input  logic        hmastlock,
  input  logic [31:0] hwdata,
  output logic [31:0] hrdata,
  output logic        hreadyout,
  output logic        hresp,

  // ==== clcd_kvm AXI4-Lite CSR ===============================================
  input  logic [11:0] s_axi_awaddr,
  input  logic [2:0]  s_axi_awprot,
  input  logic        s_axi_awvalid,
  output logic        s_axi_awready,
  input  logic [31:0] s_axi_wdata,
  input  logic [3:0]  s_axi_wstrb,
  input  logic        s_axi_wvalid,
  output logic        s_axi_wready,
  output logic [1:0]  s_axi_bresp,
  output logic        s_axi_bvalid,
  input  logic        s_axi_bready,
  input  logic [11:0] s_axi_araddr,
  input  logic [2:0]  s_axi_arprot,
  input  logic        s_axi_arvalid,
  output logic        s_axi_arready,
  output logic [31:0] s_axi_rdata,
  output logic [1:0]  s_axi_rresp,
  output logic        s_axi_rvalid,
  input  logic        s_axi_rready,

  // ==== SOURCE A -- the harness clcd_0 (kvm_models.HarnessSource) ============
  input  logic [7:0]  h_pd_o,
  input  logic        h_pd_oe,
  input  logic        h_cs_n,
  input  logic        h_wr_n,
  input  logic        h_rd_n,
  input  logic        h_rs,
  input  logic        h_bl,
  input  logic        h_rst_n,
  input  logic        h_busy,
  input  logic        h_fifo_empty,

  // ==== KVM controls =========================================================
  input  logic        decouple_status,
  input  logic        rp_resetn,
  input  logic        user_npb1,

  // ==== Panel pads (tests/clcd/clcd_panel_model.py watches these) ============
  output logic [7:0]  clcd_pd_o,
  output logic        clcd_pd_oe,
  output logic        clcd_cs_n_o,
  output logic        clcd_wr_n_o,
  output logic        clcd_rd_n_o,
  output logic        clcd_rs_o,
  output logic        clcd_bl_o,
  output logic        clcd_rst_n_o,
  output logic        owner_o,
  output logic        lcd_en_o,
  output logic [15:0] tun_o_o,
  output logic [15:0] tun_oe_o,

  // ==== LCDMIR AXI4-Lite (cocotb AxiLiteMaster, prefix "lm_axi_") ============
  output logic        lm_axi_aclk,
  input  logic [31:0] lm_axi_awaddr,
  input  logic [2:0]  lm_axi_awprot,
  input  logic        lm_axi_awvalid,
  output logic        lm_axi_awready,
  input  logic [31:0] lm_axi_wdata,
  input  logic [3:0]  lm_axi_wstrb,
  input  logic        lm_axi_wvalid,
  output logic        lm_axi_wready,
  output logic [1:0]  lm_axi_bresp,
  output logic        lm_axi_bvalid,
  input  logic        lm_axi_bready,
  input  logic [31:0] lm_axi_araddr,
  input  logic [2:0]  lm_axi_arprot,
  input  logic        lm_axi_arvalid,
  output logic        lm_axi_arready,
  output logic [31:0] lm_axi_rdata,
  output logic [1:0]  lm_axi_rresp,
  output logic        lm_axi_rvalid,
  input  logic        lm_axi_rready,

  // ==== frame dump control ===================================================
  input  logic        dump_go,
  input  logic [31:0] dump_base,
  input  logic [31:0] dump_count,
  output logic        dump_busy
);

  initial begin
    s_axi_aclk = 1'b0;
    forever #5 s_axi_aclk = ~s_axi_aclk;
  end
  initial begin
    hclk = 1'b0;
    #3;                               // not phase-aligned with the shell clock
    forever #10 hclk = ~hclk;
  end
  assign lm_axi_aclk = s_axi_aclk;

  // ==== the CLCD-KVM e2e harness, unchanged ==================================
  tb_clcd_kvm_e2e u_e2e (
    .s_axi_aclk     (s_axi_aclk),
    .s_axi_aresetn  (s_axi_aresetn),
    .hclk           (hclk),
    .hresetn        (hresetn),
    .hsel           (hsel),
    .haddr          (haddr),
    .htrans         (htrans),
    .hwrite         (hwrite),
    .hsize          (hsize),
    .hburst         (hburst),
    .hprot          (hprot),
    .hmastlock      (hmastlock),
    .hwdata         (hwdata),
    .hrdata         (hrdata),
    .hreadyout      (hreadyout),
    .hresp          (hresp),
    .s_axi_awaddr   (s_axi_awaddr),
    .s_axi_awprot   (s_axi_awprot),
    .s_axi_awvalid  (s_axi_awvalid),
    .s_axi_awready  (s_axi_awready),
    .s_axi_wdata    (s_axi_wdata),
    .s_axi_wstrb    (s_axi_wstrb),
    .s_axi_wvalid   (s_axi_wvalid),
    .s_axi_wready   (s_axi_wready),
    .s_axi_bresp    (s_axi_bresp),
    .s_axi_bvalid   (s_axi_bvalid),
    .s_axi_bready   (s_axi_bready),
    .s_axi_araddr   (s_axi_araddr),
    .s_axi_arprot   (s_axi_arprot),
    .s_axi_arvalid  (s_axi_arvalid),
    .s_axi_arready  (s_axi_arready),
    .s_axi_rdata    (s_axi_rdata),
    .s_axi_rresp    (s_axi_rresp),
    .s_axi_rvalid   (s_axi_rvalid),
    .s_axi_rready   (s_axi_rready),
    .h_pd_o         (h_pd_o),
    .h_pd_oe        (h_pd_oe),
    .h_cs_n         (h_cs_n),
    .h_wr_n         (h_wr_n),
    .h_rd_n         (h_rd_n),
    .h_rs           (h_rs),
    .h_bl           (h_bl),
    .h_rst_n        (h_rst_n),
    .h_busy         (h_busy),
    .h_fifo_empty   (h_fifo_empty),
    .decouple_status(decouple_status),
    .rp_resetn      (rp_resetn),
    .user_npb1      (user_npb1),
    .clcd_pd_o      (clcd_pd_o),
    .clcd_pd_oe     (clcd_pd_oe),
    .clcd_cs_n_o    (clcd_cs_n_o),
    .clcd_wr_n_o    (clcd_wr_n_o),
    .clcd_rd_n_o    (clcd_rd_n_o),
    .clcd_rs_o      (clcd_rs_o),
    .clcd_bl_o      (clcd_bl_o),
    .clcd_rst_n_o   (clcd_rst_n_o),
    .owner_o        (owner_o),
    .lcd_en_o       (lcd_en_o),
    .tun_o_o        (tun_o_o),
    .tun_oe_o       (tun_oe_o)
  );

  // ==== LCDMIR read-channel mux (SV frame reader while it runs) ==============
  logic [31:0] d_araddr, m_araddr, m_rdata;
  logic        d_arvalid, d_rready, m_arvalid, m_rready, m_arready, m_rvalid;
  logic [1:0]  m_rresp;

  tb_axil_dump #(.AW(32)) u_dump (
    .clk     (s_axi_aclk),
    .go      (dump_go),
    .base    (dump_base),
    .count   (dump_count),
    .busy    (dump_busy),
    .araddr  (d_araddr),
    .arvalid (d_arvalid),
    .arready (m_arready),
    .rdata   (m_rdata),
    .rvalid  (m_rvalid),
    .rready  (d_rready)
  );

  assign m_araddr       = dump_busy ? d_araddr  : lm_axi_araddr;
  assign m_arvalid      = dump_busy ? d_arvalid : lm_axi_arvalid;
  assign m_rready       = dump_busy ? d_rready  : lm_axi_rready;
  assign lm_axi_arready = dump_busy ? 1'b0      : m_arready;
  assign lm_axi_rvalid  = dump_busy ? 1'b0      : m_rvalid;
  assign lm_axi_rdata   = m_rdata;
  assign lm_axi_rresp   = m_rresp;

  // ==== the snooper: an input-only tap on the KVM's pad-side nets =============
  lcd_mirror #(
    .C_S_AXI_ADDR_WIDTH (32),
    .BANKED             (0)
  ) u_lcd_mirror (
    .s_axi_aclk    (s_axi_aclk),
    .s_axi_aresetn (s_axi_aresetn),
    .s_axi_awaddr  (lm_axi_awaddr),
    .s_axi_awprot  (lm_axi_awprot),
    .s_axi_awvalid (lm_axi_awvalid),
    .s_axi_awready (lm_axi_awready),
    .s_axi_wdata   (lm_axi_wdata),
    .s_axi_wstrb   (lm_axi_wstrb),
    .s_axi_wvalid  (lm_axi_wvalid),
    .s_axi_wready  (lm_axi_wready),
    .s_axi_bresp   (lm_axi_bresp),
    .s_axi_bvalid  (lm_axi_bvalid),
    .s_axi_bready  (lm_axi_bready),
    .s_axi_araddr  (m_araddr),
    .s_axi_arprot  (lm_axi_arprot),
    .s_axi_arvalid (m_arvalid),
    .s_axi_arready (m_arready),
    .s_axi_rdata   (m_rdata),
    .s_axi_rresp   (m_rresp),
    .s_axi_rvalid  (m_rvalid),
    .s_axi_rready  (m_rready),
    .lcd_pd_i      (clcd_pd_o),
    .lcd_cs_n_i    (clcd_cs_n_o),
    .lcd_wr_n_i    (clcd_wr_n_o),
    .lcd_rs_i      (clcd_rs_o),
    .lcd_rd_n_i    (clcd_rd_n_o),
    .lcd_rst_n_i   (clcd_rst_n_o),
    .lcd_bl_i      (clcd_bl_o),
    .lcd_owner_i   (owner_o)
  );

endmodule
