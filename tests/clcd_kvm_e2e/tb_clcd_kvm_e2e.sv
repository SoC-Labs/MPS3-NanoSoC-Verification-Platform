// -----------------------------------------------------------------------------
// tb_clcd_kvm_e2e.sv — the CLCD-KVM END-TO-END integration harness (W3-B,
// docs/CLCD_KVM_WAVE_PLAN.md Wave 3).
//
// This is the FEATURE PROOF. It structurally assembles the REAL delivered
// blocks, in the SAME topology the silicon has, and drives them so the panel
// model can watch ownership actually hand over:
//
//   AHB-Lite (BFM, hclk 50 MHz) ─► nanosoc_exp_socket ─► ahb_clcd ─► clcd_core
//                                        │  (ACTIVE-HIGH display pins, lcd_*)
//                                        ▼
//                          [ the tunnel bit-mapping ]   <- byte-identical mirror
//                          dut_gpio_o/oe[15:8]              of rp_nanosoc_wrapper's
//                                        │                  W3-A high-byte mux
//                                        ▼
//   HarnessSource (model, s_axi_aclk 100 MHz) ─► h_* ─► clcd_kvm ─► panel pads
//   AXI4-Lite CSR (BFM) ──────────────────────────────►          (clcd_*_o)
//   USER_nPB1 / decouple_status / rp_resetn ──────────►               │
//                                                                     ▼
//                                              PanelBusModel (HX8347-D on 8080)
//
// The DUT side is REAL RTL (ahb_clcd + clcd_core), NOT a model — that is the
// whole point of an e2e bench: the bytes the panel sees on the DUT side are the
// reference accelerator's genuine 8080 output, crossing the genuine tunnel CDC.
// The harness side is modelled (kvm_models.HarnessSource), exactly as W2-C did.
//
// The high-byte tunnel mux below is the SAME expression as
// rp_nanosoc_wrapper.sv's W3-A mux; the bit map is FROZEN in
// docs/contracts/dut-display-tunnel.md §2 (strobes ACTIVE-HIGH; reserved
// spare/pd_oe/rd driven 0). Do not restate the encoding without citing that
// file. The low byte [7:0] carries a recognisable LED pattern (there is no M0
// GPIO here): a KVM that mis-decoded the low half would show up as garbage
// bytes at the panel.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_clcd_kvm_e2e (
  // ==== Two clock domains, both bench-driven ================================
  input  logic        s_axi_aclk,     // shell / KVM domain, 100 MHz shipped
  input  logic        s_axi_aresetn,
  input  logic        hclk,           // DUT / socket domain, 50 MHz shipped
  input  logic        hresetn,

  // ==== nanosoc_exp_socket AHB-Lite slave (driven by ahb_lite.AhbLiteMaster) =
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
  output logic        hreadyout,      // the BFM samples this as the global HREADY
  output logic        hresp,

  // ==== clcd_kvm AXI4-Lite CSR (driven by regmap.AxiLiteMaster) =============
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

  // ==== SOURCE A — the harness clcd_0 (driven by kvm_models.HarnessSource) ===
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

  // ==== KVM controls ========================================================
  input  logic        decouple_status,
  input  logic        rp_resetn,
  input  logic        user_npb1,      // ACTIVE-LOW pad (MPS3 AT32)

  // ==== Panel pads — watched by tests/clcd/clcd_panel_model.py ==============
  output logic [7:0]  clcd_pd_o,
  output logic        clcd_pd_oe,
  output logic        clcd_cs_n_o,
  output logic        clcd_wr_n_o,
  output logic        clcd_rd_n_o,
  output logic        clcd_rs_o,
  output logic        clcd_bl_o,
  output logic        clcd_rst_n_o,

  // ==== Observability =======================================================
  output logic        owner_o,        // 0 = HARNESS, 1 = DUT
  output logic        lcd_en_o,       // the socket's tunnel-drive enable
  output logic [15:0] tun_o_o,        // the tunnelled dut_gpio_o  (for TUNNEL dbg)
  output logic [15:0] tun_oe_o        // the tunnelled dut_gpio_oe
);

  // ==== The DUT-side socket (REAL RTL) ======================================
  // Single-slave AHB-Lite: the global HREADY is, by definition, this slave's
  // own HREADYOUT (ahb_lite.py's "preferred harness wiring").
  logic        s_hready;
  assign s_hready = hreadyout;

  logic        lcd_en, lcd_cs, lcd_wr, lcd_rs, lcd_busy, lcd_req;
  logic [7:0]  lcd_pd;
  logic [3:0]  exp_irq_w;
  logic [1:0]  exp_drq_w;

  nanosoc_exp_socket #(
    .ADDR_W (32),
    .DATA_W (32)
  ) u_socket (
    .hclk      (hclk),
    .hresetn   (hresetn),
    .hsel      (hsel),
    .haddr     (haddr),
    .htrans    (htrans),
    .hwrite    (hwrite),
    .hsize     (hsize),
    .hburst    (hburst),
    .hprot     (hprot),
    .hmastlock (hmastlock),
    .hwdata    (hwdata),
    .hready    (s_hready),
    .hrdata    (hrdata),
    .hreadyout (hreadyout),
    .hresp     (hresp),
    .lcd_en    (lcd_en),
    .lcd_pd    (lcd_pd),
    .lcd_cs    (lcd_cs),
    .lcd_wr    (lcd_wr),
    .lcd_rs    (lcd_rs),
    .lcd_busy  (lcd_busy),
    .lcd_req   (lcd_req),
    .irq       (exp_irq_w),
    .drq       (exp_drq_w)
  );

  assign lcd_en_o = lcd_en;

  // ==== The tunnel — the W3-A high-byte mux, mirrored exactly ================
  // dut_gpio_o[15:8]  = PD[7:0]
  // dut_gpio_oe[15:8] = {spare, req, busy, pd_oe, rd, rs, wr, cs}  (MSB..LSB),
  //                     strobes ACTIVE-HIGH, reserved (spare/pd_oe/rd) = 0.
  // Low byte [7:0]: a recognisable LED pattern stands in for the M0's GPIO.
  logic [15:0] tun_o, tun_oe;
  assign tun_o  = { lcd_en ? lcd_pd : 8'h00,
                    8'hA5 };
  assign tun_oe = { lcd_en ? {1'b0,      // [15] spare  RESERVED = 0
                              lcd_req,   // [14] req
                              lcd_busy,  // [13] busy
                              1'b0,      // [12] pd_oe  RESERVED = 0
                              1'b0,      // [11] rd     RESERVED = 0
                              lcd_rs,    // [10] rs
                              lcd_wr,    // [9]  wr
                              lcd_cs}    // [8]  cs
                           : 8'h00,
                    8'h5A };
  assign tun_o_o  = tun_o;
  assign tun_oe_o = tun_oe;

  // ==== The shell KVM (REAL RTL) ============================================
  clcd_kvm u_kvm (
    .s_axi_aclk     (s_axi_aclk),
    .s_axi_aresetn  (s_axi_aresetn),
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
    .h_pd_i         (),                 // panel read-back unused (READ_PATH=0)

    .dut_gpio_o_i   (tun_o),
    .dut_gpio_oe_i  (tun_oe),

    .decouple_status(decouple_status),
    .rp_resetn      (rp_resetn),
    .user_npb1      (user_npb1),

    .clcd_pd_o      (clcd_pd_o),
    .clcd_pd_i      (8'h00),            // write-only panel: no read data in
    .clcd_pd_oe     (clcd_pd_oe),
    .clcd_cs_n_o    (clcd_cs_n_o),
    .clcd_wr_n_o    (clcd_wr_n_o),
    .clcd_rd_n_o    (clcd_rd_n_o),
    .clcd_rs_o      (clcd_rs_o),
    .clcd_bl_o      (clcd_bl_o),
    .clcd_rst_n_o   (clcd_rst_n_o),

    .owner_o        (owner_o)
  );

  // -Wall sink for the deliberately-unused socket extension hooks.
  // verilator lint_off UNUSED
  wire _unused_ok = &{1'b0, exp_irq_w, exp_drq_w, 1'b0};
  // verilator lint_on UNUSED

endmodule
