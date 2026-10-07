// -----------------------------------------------------------------------------
// tb_lcd_mirror.sv -- direct bench top for the LCDMIR snooper.
//
//   cocotb (AxiLiteMaster, s_axi_*) ----------+----> lcd_mirror (DUT, AXI slave)
//   tb_axil_dump (SV frame reader) -- AR/R ---+           ^  tap inputs
//   pad BFM (below, executes stim words) ------------------+
//
// The pad BFM executes the 32-bit stimulus words defined (and expanded to the
// identical per-cycle trace) by tests/lcd_mirror/pad_decoder.py. It drives the
// tap the way clcd_kvm_0 drives the pads: registered in s_axi_aclk, one value
// per cycle. cocotb writes a stimulus file, pulses `bfm_load`, and waits for
// `bfm_done` to count up.
//
// The DUT is elaborated at C_S_AXI_ADDR_WIDTH = 32 -- the width shell_bd.tcl
// instantiates CSR blocks with -- and the bench drives FULL system addresses
// (0x44B8_xxxx), so the block's local decode is exercised exactly as it ships.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_lcd_mirror #(
  parameter int BANKED   = 0,
  parameter int STIM_MAX = 1 << 20
) (
  // The 100 MHz shell clock is generated HERE, not by cocotb: a Python-driven
  // clock costs a VPI callback per edge and ran this bench at ~8 K cycles/s.
  output logic        s_axi_aclk,
  input  logic        s_axi_aresetn,

  // ==== cocotb AXI4-Lite master ==============================================
  input  logic [31:0] s_axi_awaddr,
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
  input  logic [31:0] s_axi_araddr,
  input  logic [2:0]  s_axi_arprot,
  input  logic        s_axi_arvalid,
  output logic        s_axi_arready,
  output logic [31:0] s_axi_rdata,
  output logic [1:0]  s_axi_rresp,
  output logic        s_axi_rvalid,
  input  logic        s_axi_rready,

  // ==== pad BFM control ======================================================
  input  logic        bfm_load,
  output logic        bfm_busy,
  output logic [15:0] bfm_done,

  // ==== frame dump control ===================================================
  input  logic        dump_go,
  input  logic [31:0] dump_base,
  input  logic [31:0] dump_count,
  output logic        dump_busy,

  // ==== observability: the tap as the DUT sees it ============================
  output logic [7:0]  tap_pd,
  output logic        tap_cs_n,
  output logic        tap_wr_n,
  output logic        tap_rs,
  output logic        tap_rd_n,
  output logic        tap_rst_n,
  output logic        tap_bl,
  output logic        tap_owner
);

  // ===========================================================================
  // The pad BFM.
  // ===========================================================================
  logic [31:0] stim [0:STIM_MAX-1];
  string       stim_path;
  logic [7:0]  t_setup, t_lo, t_hi;
  logic [3:0]  t_idle;

  initial begin
    s_axi_aclk = 1'b0;
    forever #5 s_axi_aclk = ~s_axi_aclk;
  end

  initial begin
    if (!$value$plusargs("STIM_FILE=%s", stim_path)) stim_path = "stim.hex";
    tap_pd    = 8'h00;
    tap_cs_n  = 1'b1;
    tap_wr_n  = 1'b1;
    tap_rs    = 1'b0;
    tap_rd_n  = 1'b1;
    tap_rst_n = 1'b1;
    tap_bl    = 1'b0;
    tap_owner = 1'b0;
    bfm_busy  = 1'b0;
    bfm_done  = 16'd0;
    t_setup   = 8'd2;
    t_lo      = 8'd4;
    t_hi      = 8'd4;
    t_idle    = 4'd1;
  end

  // Hold one pad state for n samples.
  task automatic drive(input logic cs_n, input logic wr_n, input logic rs,
                       input logic [7:0] pd, input logic rd_n, input logic rst_n,
                       input int n);
    for (int i = 0; i < n; i++) begin
      tap_cs_n  <= cs_n;
      tap_wr_n  <= wr_n;
      tap_rs    <= rs;
      tap_pd    <= pd;
      tap_rd_n  <= rd_n;
      tap_rst_n <= rst_n;
      @(posedge s_axi_aclk);
    end
  endtask

  always begin : bfm
    int          pc;
    logic [31:0] w;
    logic        hold_rs;
    logic [7:0]  hold_pd;
    bit          done;
    @(posedge s_axi_aclk);
    if (bfm_load) begin
      $readmemh(stim_path, stim);
      bfm_busy <= 1'b1;
      pc   = 0;
      done = 0;
      while (!done) begin
        w  = stim[pc];
        pc = pc + 1;
        hold_rs = tap_rs;
        hold_pd = tap_pd;
        case (w[31:28])
          4'h0: begin   // BYTE
            drive(1'b0, 1'b1, w[8], w[7:0], 1'b1, 1'b1, int'(t_setup));
            drive(1'b0, 1'b0, w[8], w[7:0], 1'b1, 1'b1, int'(t_lo));
            drive(1'b0, 1'b1, w[8], w[7:0], 1'b1, 1'b1, int'(t_hi));
            drive(1'b1, 1'b1, w[8], w[7:0], 1'b1, 1'b1, int'(t_idle));
          end
          4'h1: drive(1'b1, 1'b1, hold_rs, hold_pd, 1'b1, 1'b0, int'(w[23:0]));  // RST
          4'h2: drive(1'b1, 1'b1, hold_rs, hold_pd, 1'b1, 1'b1, int'(w[23:0]));  // IDLE
          4'h3: begin   // TIMING (BFM-internal: blocking, no cycle consumed)
            t_idle  = w[27:24];
            t_setup = w[23:16];
            t_lo    = w[15:8];
            t_hi    = w[7:0];
          end
          4'h4: drive(w[10], w[9], w[8], w[7:0], w[11], w[12], int'(w[27:16]));   // RAW
          4'h5: begin   // LEVEL
            tap_bl    <= w[1];
            tap_owner <= w[0];
          end
          4'hF: done = 1;
          default: begin
            $error("tb_lcd_mirror: bad stimulus word %08x at %0d", w, pc - 1);
            done = 1;
          end
        endcase
      end
      bfm_busy <= 1'b0;
      bfm_done <= bfm_done + 16'd1;
    end
  end

  // ===========================================================================
  // AXI read-channel mux: the SV frame reader while it runs, cocotb otherwise.
  // ===========================================================================
  logic [31:0] d_araddr;
  logic        d_arvalid, d_rready;
  logic [31:0] m_araddr;
  logic        m_arvalid, m_rready;
  logic        m_arready, m_rvalid;
  logic [31:0] m_rdata;
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

  assign m_araddr      = dump_busy ? d_araddr  : s_axi_araddr;
  assign m_arvalid     = dump_busy ? d_arvalid : s_axi_arvalid;
  assign m_rready      = dump_busy ? d_rready  : s_axi_rready;
  assign s_axi_arready = dump_busy ? 1'b0      : m_arready;
  assign s_axi_rvalid  = dump_busy ? 1'b0      : m_rvalid;
  assign s_axi_rdata   = m_rdata;
  assign s_axi_rresp   = m_rresp;

  // ===========================================================================
  // The DUT.
  // ===========================================================================
  lcd_mirror #(
    .C_S_AXI_ADDR_WIDTH (32),
    .BANKED             (BANKED)
  ) u_dut (
    .s_axi_aclk    (s_axi_aclk),
    .s_axi_aresetn (s_axi_aresetn),
    .s_axi_awaddr  (s_axi_awaddr),
    .s_axi_awprot  (s_axi_awprot),
    .s_axi_awvalid (s_axi_awvalid),
    .s_axi_awready (s_axi_awready),
    .s_axi_wdata   (s_axi_wdata),
    .s_axi_wstrb   (s_axi_wstrb),
    .s_axi_wvalid  (s_axi_wvalid),
    .s_axi_wready  (s_axi_wready),
    .s_axi_bresp   (s_axi_bresp),
    .s_axi_bvalid  (s_axi_bvalid),
    .s_axi_bready  (s_axi_bready),
    .s_axi_araddr  (m_araddr),
    .s_axi_arprot  (s_axi_arprot),
    .s_axi_arvalid (m_arvalid),
    .s_axi_arready (m_arready),
    .s_axi_rdata   (m_rdata),
    .s_axi_rresp   (m_rresp),
    .s_axi_rvalid  (m_rvalid),
    .s_axi_rready  (m_rready),
    .lcd_pd_i      (tap_pd),
    .lcd_cs_n_i    (tap_cs_n),
    .lcd_wr_n_i    (tap_wr_n),
    .lcd_rs_i      (tap_rs),
    .lcd_rd_n_i    (tap_rd_n),
    .lcd_rst_n_i   (tap_rst_n),
    .lcd_bl_i      (tap_bl),
    .lcd_owner_i   (tap_owner)
  );

endmodule
