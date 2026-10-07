// -----------------------------------------------------------------------------
// lcd_mirror.sv -- LCDMIR: a pixel-exact, input-only mirror of the 320x240
// HX8347-D panel, as the pads see it after the CLCD KVM.
//
// Contract: docs/planning/linux_lanes/LCD_MIRROR_FPGA.md (§2 snooper, §3
// storage, §4 register map) and this directory's README.md, which records the
// deviations and the open items (O1-O6) behind CTRL switches. Golden model:
// tests/lcd_mirror/hx8347_gram_model.py. Bench: tests/lcd_mirror/.
//
// What it is:
//   * lcd_mirror_core.sv decodes every 8080 write cycle on the pad-side nets of
//     clcd_kvm_0 (input-only fan-out, same s_axi_aclk domain: NO CDC) and
//     replays it into an HX8347-D GRAM model;
//   * this file stores the result as a 320x240 RGB565 frame in VIEWER order
//     (two 38,400 x 16 simple-dual-port BRAMs, even and odd pixels), keeps
//     16x16 dirty/valid tile maps, a raw 256-register log, counters, and serves
//     all of it on an AXI4-Lite slave.
//
// Address map (LCD_MIRROR_FPGA.md §4), BANKED = 0 (default): one 256 KiB
// region, CSRs at 0x0_0000..0x0_01FF, the frame buffer at 0x1_0000..0x3_57FF
// (38,400 words, px[vy*320+vx], 2 px per word, even x in [15:0]).
// BANKED = 1 (the §4 fallback): one 64 KiB page, CSRs at 0x0000, a 32 KiB
// frame-buffer window at 0x8000 selecting bank FB_BANK (0x038, 5 banks).
//
// Reads have NO side effects (the shell's no-destructive-read rule,
// clcd_kvm.sv:860-866). Every action -- SNAP, clear sticky, clear counts -- is
// armed by a WRITE. There is no interrupt; software polls SEQ.
//
// Decode width: shell_bd.tcl instantiates CSR blocks with C_S_AXI_ADDR_WIDTH=32
// and the interconnect hands the slave the FULL system address. Only the
// block's own 18 (or, banked, 16) low address bits are decoded -- the
// csr_decode_width lesson (clcd_kvm.sv "Decode width").
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module lcd_mirror #(
  parameter int C_S_AXI_ADDR_WIDTH = 18,
  parameter int C_S_AXI_DATA_WIDTH = 32,
  // 0 = the 256 KiB map (0x44B8_0000). 1 = the 64 KiB banked fallback.
  parameter int BANKED = 0,
  // TMIN reset value: {guard, hi, lo} = 1/2/2 cycles (LCD_MIRROR_FPGA.md §4).
  parameter logic [23:0] TMIN_INIT = 24'h01_02_02,
  // 1 = build compare-on-write dirty tracking (HM LCD_MIRROR.md §4.2, H4): a
  // tile goes dirty only when a write CHANGES a pixel. Costs a read-first port
  // on the frame BRAM (true-dual-port, same RAMB36 count). CTRL[2] dirty_all
  // (reset 0) selects it at run time; with DIRTY_CMP = 0, CTRL[2] reads 1.
  parameter int DIRTY_CMP = 1,
  // Panel register defaults after CLCD_RST (O3) -- see lcd_mirror_core.sv.
  parameter logic [8:0] RST_SC  = 9'd0,
  parameter logic [8:0] RST_EC  = 9'd239,
  parameter logic [8:0] RST_SP  = 9'd0,
  parameter logic [8:0] RST_EP  = 9'd319,
  parameter logic [7:0] RST_R01 = 8'h00,
  parameter logic [7:0] RST_R16 = 8'h00,
  parameter logic [7:0] RST_R17 = 8'h06,
  parameter logic [7:0] RST_R1F = 8'h01,
  parameter logic [7:0] RST_R28 = 8'h00,
  parameter logic [7:0] RST_R36 = 8'h00
) (
  input  logic                            s_axi_aclk,     // shell clock, 100 MHz
  input  logic                            s_axi_aresetn,  // shell reset, active-low

  // ==== AXI4-Lite slave -- MicroBlaze data bus @ 0x44B8_0000 =================
  input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_awaddr,
  input  logic [2:0]                      s_axi_awprot,
  input  logic                            s_axi_awvalid,
  output logic                            s_axi_awready,
  input  logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_wdata,
  input  logic [C_S_AXI_DATA_WIDTH/8-1:0] s_axi_wstrb,
  input  logic                            s_axi_wvalid,
  output logic                            s_axi_wready,
  output logic [1:0]                      s_axi_bresp,
  output logic                            s_axi_bvalid,
  input  logic                            s_axi_bready,
  input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_araddr,
  input  logic [2:0]                      s_axi_arprot,
  input  logic                            s_axi_arvalid,
  output logic                            s_axi_arready,
  output logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_rdata,
  output logic [1:0]                      s_axi_rresp,
  output logic                            s_axi_rvalid,
  input  logic                            s_axi_rready,

  // ==== The tap -- fan-outs of clcd_kvm_0's pad-side outputs (INPUT ONLY) ===
  input  logic [7:0]                      lcd_pd_i,       // clcd_kvm_0/clcd_pd_o
  input  logic                            lcd_cs_n_i,     // clcd_kvm_0/clcd_cs_n_o
  input  logic                            lcd_wr_n_i,     // clcd_kvm_0/clcd_wr_n_o
  input  logic                            lcd_rs_i,       // clcd_kvm_0/clcd_rs_o
  input  logic                            lcd_rd_n_i,     // clcd_kvm_0/clcd_rd_n_o
  input  logic                            lcd_rst_n_i,    // clcd_kvm_0/clcd_rst_n_o
  input  logic                            lcd_bl_i,       // clcd_kvm_0/clcd_bl_o
  input  logic                            lcd_owner_i     // clcd_kvm_0/owner_o
);

  localparam int W       = 320;
  localparam int H       = 240;
  localparam int NTILES  = 300;           // 20 x 15 tiles of 16 x 16
  localparam int FBWORDS = W * H / 2;     // 38,400

  localparam logic [31:0] ID_VAL      = 32'h4C43_444D;           // "LCDM"
  localparam logic [31:0] VERSION_VAL = {8'd1, 8'd0, 8'd16, 8'd1};
  localparam logic [31:0] GEOM_VAL    = {16'(H), 16'(W)};

  // Local offset width: the block's own region, never C_S_AXI_ADDR_WIDTH.
  localparam int REGION_W = (BANKED != 0) ? 16 : 18;
  localparam int LADDR_W  = (C_S_AXI_ADDR_WIDTH < REGION_W) ? C_S_AXI_ADDR_WIDTH : REGION_W;

  // ===========================================================================
  // AXI4-Lite WRITE channel -- the Xilinx template, identical to clcd_kvm.sv /
  // clcd.sv (shell-regmap.md "AXI4-Lite slave conventions", style 1).
  // ===========================================================================
  logic [LADDR_W-1:0] axi_awaddr_q;
  logic               axi_awready_q, axi_wready_q, aw_en_q, axi_bvalid_q;

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00;
  assign s_axi_bvalid  = axi_bvalid_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awready_q <= 1'b0;
      aw_en_q       <= 1'b1;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awready_q <= 1'b1;
      aw_en_q       <= 1'b0;
    end else if (s_axi_bready && axi_bvalid_q) begin
      aw_en_q       <= 1'b1;
      axi_awready_q <= 1'b0;
    end else begin
      axi_awready_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awaddr_q <= '0;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awaddr_q <= s_axi_awaddr[LADDR_W-1:0];
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_wready_q <= 1'b0;
    end else if (~axi_wready_q && s_axi_wvalid && s_axi_awvalid && aw_en_q) begin
      axi_wready_q <= 1'b1;
    end else begin
      axi_wready_q <= 1'b0;
    end
  end

  wire slv_reg_wren = axi_wready_q && s_axi_wvalid && axi_awready_q && s_axi_awvalid;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_bvalid_q <= 1'b0;
    end else if (slv_reg_wren && ~axi_bvalid_q) begin
      axi_bvalid_q <= 1'b1;
    end else if (s_axi_bready && axi_bvalid_q) begin
      axi_bvalid_q <= 1'b0;
    end
  end

  // Write decode. Only CTRL (0x00C), TMIN (0x034) and, banked, FB_BANK (0x038)
  // are writable; every other offset accepts the write with no effect.
  wire [17:0] wla     = 18'(axi_awaddr_q);
  wire        wcsr    = (BANKED != 0) ? (wla[15:9] == 7'd0) : (wla[17:9] == 9'd0);
  wire [6:0]  widx    = wla[8:2];
  wire        ctrl_wr = slv_reg_wren && wcsr && (widx == 7'h03);
  wire        tmin_wr = slv_reg_wren && wcsr && (widx == 7'h0D);
  wire        bank_wr = slv_reg_wren && wcsr && (widx == 7'h0E) && (BANKED != 0);

  logic       ac_load_q, flip_conv_q, dirty_all_q;
  logic [7:0] tmin_lo_q, tmin_hi_q, tmin_guard_q;
  logic [2:0] fb_bank_q;

  // W1P actions (byte 1 of CTRL). Never stored; read back 0.
  wire snap_w       = ctrl_wr && s_axi_wstrb[1] && s_axi_wdata[8];
  wire clr_sticky_w = ctrl_wr && s_axi_wstrb[1] && s_axi_wdata[9];
  wire clr_counts_w = ctrl_wr && s_axi_wstrb[1] && s_axi_wdata[10];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      ac_load_q    <= 1'b0;
      flip_conv_q  <= 1'b0;
      dirty_all_q  <= (DIRTY_CMP == 0);
      tmin_lo_q    <= TMIN_INIT[7:0];
      tmin_hi_q    <= TMIN_INIT[15:8];
      tmin_guard_q <= TMIN_INIT[23:16];
      fb_bank_q    <= 3'd0;
    end else begin
      if (ctrl_wr && s_axi_wstrb[0]) begin
        ac_load_q   <= s_axi_wdata[0];
        flip_conv_q <= s_axi_wdata[1];
        dirty_all_q <= s_axi_wdata[2] | (DIRTY_CMP == 0);
      end
      if (tmin_wr) begin
        if (s_axi_wstrb[0]) tmin_lo_q    <= s_axi_wdata[7:0];
        if (s_axi_wstrb[1]) tmin_hi_q    <= s_axi_wdata[15:8];
        if (s_axi_wstrb[2]) tmin_guard_q <= s_axi_wdata[23:16];
      end
      if (bank_wr && s_axi_wstrb[0]) fb_bank_q <= s_axi_wdata[2:0];
    end
  end

  // ===========================================================================
  // The core: tap, guards, HX8347-D model, pixel pipeline.
  // ===========================================================================
  logic        ev_byte, ev_ramwr, ev_frame, ev_rst, ev_viol, ev_rds, ev_oob;
  logic        reg_we;
  logic [7:0]  reg_idx, reg_dat;
  logic        px_we, px_odd, px_valid_ok;
  logic [15:0] px_word, px_data;
  logic [8:0]  px_vx;
  logic [7:0]  px_vy;
  logic [14:0] px_ty_oh;
  logic [19:0] px_tx_oh;
  logic        st_rst_n, st_bl, st_owner, panel_rst;
  logic [7:0]  st_idx, st_r01, st_r16, st_r17, st_r1f, st_r28, st_r36;
  logic [8:0]  st_sc, st_ec, st_sp, st_ep, st_acx, st_acy;

  lcd_mirror_core #(
    .RST_SC (RST_SC),  .RST_EC (RST_EC),  .RST_SP (RST_SP),  .RST_EP (RST_EP),
    .RST_R01(RST_R01), .RST_R16(RST_R16), .RST_R17(RST_R17),
    .RST_R1F(RST_R1F), .RST_R28(RST_R28), .RST_R36(RST_R36)
  ) u_core (
    .clk            (s_axi_aclk),
    .rst_n          (s_axi_aresetn),
    .tap_pd         (lcd_pd_i),
    .tap_cs_n       (lcd_cs_n_i),
    .tap_wr_n       (lcd_wr_n_i),
    .tap_rs         (lcd_rs_i),
    .tap_rd_n       (lcd_rd_n_i),
    .tap_rst_n      (lcd_rst_n_i),
    .tap_bl         (lcd_bl_i),
    .tap_owner      (lcd_owner_i),
    .cfg_ac_load    (ac_load_q),
    .cfg_flip_conv  (flip_conv_q),
    .cfg_tmin_lo    (tmin_lo_q),
    .cfg_tmin_hi    (tmin_hi_q),
    .cfg_tmin_guard (tmin_guard_q),
    .ev_byte        (ev_byte),
    .ev_ramwr       (ev_ramwr),
    .ev_frame       (ev_frame),
    .ev_rst         (ev_rst),
    .ev_viol        (ev_viol),
    .ev_rds         (ev_rds),
    .ev_oob         (ev_oob),
    .reg_we         (reg_we),
    .reg_idx        (reg_idx),
    .reg_dat        (reg_dat),
    .px_we          (px_we),
    .px_word        (px_word),
    .px_odd         (px_odd),
    .px_data        (px_data),
    .px_vx          (px_vx),
    .px_vy          (px_vy),
    .px_ty_oh       (px_ty_oh),
    .px_tx_oh       (px_tx_oh),
    .px_valid_ok    (px_valid_ok),
    .st_rst_n       (st_rst_n),
    .st_bl          (st_bl),
    .st_owner       (st_owner),
    .panel_rst      (panel_rst),
    .st_idx         (st_idx),
    .st_sc          (st_sc),
    .st_ec          (st_ec),
    .st_sp          (st_sp),
    .st_ep          (st_ep),
    .st_acx         (st_acx),
    .st_acy         (st_acy),
    .st_r01         (st_r01),
    .st_r16         (st_r16),
    .st_r17         (st_r17),
    .st_r1f         (st_r1f),
    .st_r28         (st_r28),
    .st_r36         (st_r36)
  );

  // ===========================================================================
  // Frame buffer: even pixels and odd pixels, 38,400 x 16 each (38 RAMB36-
  // equivalents in total). Written by the core, read by the AXI slave.
  // ===========================================================================
  logic [15:0] fb_raddr;
  logic [15:0] fb_rd_even, fb_rd_odd;
  logic [15:0] fb_old_even, fb_old_odd;

  lcd_mirror_ram #(.DEPTH(FBWORDS), .WIDTH(16), .AW(16), .OLD_DATA(DIRTY_CMP)) u_fb_even (
    .clk   (s_axi_aclk),
    .we    (px_we & ~px_odd),
    .waddr (px_word),
    .wdata (px_data),
    .wold  (fb_old_even),
    .raddr (fb_raddr),
    .rdata (fb_rd_even)
  );

  lcd_mirror_ram #(.DEPTH(FBWORDS), .WIDTH(16), .AW(16), .OLD_DATA(DIRTY_CMP)) u_fb_odd (
    .clk   (s_axi_aclk),
    .we    (px_we & px_odd),
    .waddr (px_word),
    .wdata (px_data),
    .wold  (fb_old_odd),
    .raddr (fb_raddr),
    .rdata (fb_rd_odd)
  );

  // ===========================================================================
  // REGS: the raw 256-byte register log, as four 64 x 8 LUTRAMs so a 32-bit
  // read returns four consecutive indices (byte i of the word = index 4k+i).
  // Not resettable, and deliberately not reset by CLCD_RST: it is a record of
  // what was last WRITTEN; MODE/STATUS carry the effective (reset-aware) values.
  // ===========================================================================
  logic [7:0] regs_b0 [0:63];
  logic [7:0] regs_b1 [0:63];
  logic [7:0] regs_b2 [0:63];
  logic [7:0] regs_b3 [0:63];

  initial begin
    for (int i = 0; i < 64; i++) begin
      regs_b0[i] = 8'h00; regs_b1[i] = 8'h00; regs_b2[i] = 8'h00; regs_b3[i] = 8'h00;
    end
  end

  // Plain `always` (the arrays have an initial block; see lcd_mirror_ram.sv).
  always @(posedge s_axi_aclk) begin
    if (reg_we) begin
      case (reg_idx[1:0])
        2'd0: regs_b0[reg_idx[7:2]] <= reg_dat;
        2'd1: regs_b1[reg_idx[7:2]] <= reg_dat;
        2'd2: regs_b2[reg_idx[7:2]] <= reg_dat;
        default: regs_b3[reg_idx[7:2]] <= reg_dat;
      endcase
    end
  end

  // ===========================================================================
  // Tile maps, bounding box, SEQ -- applied at stage d2, two cycles after the
  // RAM write (the core's p2 outputs are written at the next edge; the frame
  // BRAM hands back the REPLACED word two cycles later -- see lcd_mirror_ram).
  //
  //   dirty_live : tiles CHANGED (dirty_all=0) / WRITTEN (dirty_all=1) since
  //                the last SNAP
  //   dirty_snap : the copy SNAP took (DIRTY[0..9])
  //   valid      : tiles WRITTEN since the last CLCD_RST (VALID[0..9]) -- a
  //                write that repeats a pixel still makes it known-good
  //   bbox       : follows dirty_live (changed / written pixels)
  //   SEQ        : every in-range pixel write (the seqlock)
  //
  // SNAP copies live -> snapshot and clears live IN THE SAME CYCLE. A pixel
  // landing in that very cycle goes into the NEW live map (and is not in the
  // snapshot, nor in SNAP_SEQ), so a tile written while software is reading it
  // is always resent on the next pass: the mirror converges, it never tears
  // permanently. There is no capture freeze (it would drop panel writes).
  //
  // Bit k of a map is tile k = ty*20 + tx. The core hands over one-hot tile
  // row/column vectors, so each bit is a single AND -- no 9-bit decode x 300.
  // ===========================================================================
  logic [NTILES-1:0] dirty_live_q, dirty_snap_q, valid_q;
  logic [8:0]        bbx_min_q, bbx_max_q, bby_min_q, bby_max_q;   // live bbox
  logic [8:0]        sbx_min_q, sbx_max_q, sby_min_q, sby_max_q;   // snapshot
  logic [31:0]       seq_q, snap_seq_q;

  // d1 / d2: the pixel, delayed to line up with the BRAM's replaced word.
  logic        d1_we, d1_ok, d1_odd, d2_we, d2_ok, d2_odd;
  logic [15:0] d1_px, d2_px;
  logic [8:0]  d1_vx, d2_vx;
  logic [7:0]  d1_vy, d2_vy;
  logic [14:0] d1_ty, d2_ty;
  logic [19:0] d1_tx, d2_tx;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      d1_we <= 1'b0; d1_ok <= 1'b0; d2_we <= 1'b0; d2_ok <= 1'b0;
      d1_odd <= 1'b0; d2_odd <= 1'b0; d1_px <= 16'h0; d2_px <= 16'h0;
      d1_vx <= 9'd0; d2_vx <= 9'd0; d1_vy <= 8'd0; d2_vy <= 8'd0;
      d1_ty <= '0; d2_ty <= '0; d1_tx <= '0; d2_tx <= '0;
    end else begin
      d1_we <= px_we;  d1_ok <= px_valid_ok & ~panel_rst;  d1_odd <= px_odd;
      d1_px <= px_data; d1_vx <= px_vx; d1_vy <= px_vy; d1_ty <= px_ty_oh; d1_tx <= px_tx_oh;
      d2_we <= d1_we;  d2_ok <= d1_ok & ~panel_rst;        d2_odd <= d1_odd;
      d2_px <= d1_px;  d2_vx <= d1_vx; d2_vy <= d1_vy; d2_ty <= d1_ty; d2_tx <= d1_tx;
    end
  end

  wire        dirty_all_eff = (DIRTY_CMP == 0) | dirty_all_q;
  wire [15:0] d2_old        = d2_odd ? fb_old_odd : fb_old_even;
  wire        d2_mark       = d2_we & (dirty_all_eff | (d2_old != d2_px));
  wire        d2_valid_set  = d2_we & d2_ok & ~panel_rst;

  genvar gk;
  generate
    for (gk = 0; gk < NTILES; gk++) begin : g_tile
      localparam int TY = gk / 20;
      localparam int TX = gk % 20;
      wire hit = d2_ty[TY] & d2_tx[TX];

      always_ff @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
          dirty_live_q[gk] <= 1'b0;
          dirty_snap_q[gk] <= 1'b0;
          valid_q[gk]      <= 1'b0;
        end else begin
          if (snap_w) begin
            dirty_snap_q[gk] <= dirty_live_q[gk];
            dirty_live_q[gk] <= d2_mark & hit;
          end else if (d2_mark & hit) begin
            dirty_live_q[gk] <= 1'b1;
          end
          if (panel_rst)                valid_q[gk] <= 1'b0;
          else if (d2_valid_set & hit)  valid_q[gk] <= 1'b1;
        end
      end
    end
  endgenerate

  localparam logic [8:0] BB_EMPTY_MIN = 9'h1FF;
  localparam logic [8:0] BB_EMPTY_MAX = 9'h000;

  wire [8:0] vx9 = d2_vx;
  wire [8:0] vy9 = {1'b0, d2_vy};

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      bbx_min_q <= BB_EMPTY_MIN; bbx_max_q <= BB_EMPTY_MAX;
      bby_min_q <= BB_EMPTY_MIN; bby_max_q <= BB_EMPTY_MAX;
      sbx_min_q <= BB_EMPTY_MIN; sbx_max_q <= BB_EMPTY_MAX;
      sby_min_q <= BB_EMPTY_MIN; sby_max_q <= BB_EMPTY_MAX;
      seq_q      <= 32'd0;
      snap_seq_q <= 32'd0;
    end else begin
      if (d2_we) seq_q <= seq_q + 32'd1;
      if (snap_w) begin
        sbx_min_q  <= bbx_min_q; sbx_max_q <= bbx_max_q;
        sby_min_q  <= bby_min_q; sby_max_q <= bby_max_q;
        snap_seq_q <= seq_q;
        bbx_min_q  <= d2_mark ? vx9 : BB_EMPTY_MIN;
        bbx_max_q  <= d2_mark ? vx9 : BB_EMPTY_MAX;
        bby_min_q  <= d2_mark ? vy9 : BB_EMPTY_MIN;
        bby_max_q  <= d2_mark ? vy9 : BB_EMPTY_MAX;
      end else if (d2_mark) begin
        if (vx9 < bbx_min_q) bbx_min_q <= vx9;
        if (vx9 > bbx_max_q) bbx_max_q <= vx9;
        if (vy9 < bby_min_q) bby_min_q <= vy9;
        if (vy9 > bby_max_q) bby_max_q <= vy9;
      end
    end
  end

  // ===========================================================================
  // Event counters (32-bit, wrap) and sticky STATUS bits.
  //   clr_counts clears FRAMES..RDS (not SEQ / SNAP_SEQ: SEQ is the seqlock
  //   software compares against, and zeroing it could fake "no change").
  //   Clear wins over a same-cycle increment. Sticky bits: set wins over a
  //   same-cycle clear (an event that lands with the acknowledgement is kept).
  // ===========================================================================
  logic [31:0] frames_q, ramwr_q, resets_q, bytes_q, viol_q, oob_q, rds_q;
  logic        viol_st_q, oob_st_q, rds_st_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn || clr_counts_w) begin
      frames_q <= 32'd0; ramwr_q <= 32'd0; resets_q <= 32'd0; bytes_q <= 32'd0;
      viol_q   <= 32'd0; oob_q   <= 32'd0; rds_q    <= 32'd0;
    end else begin
      if (ev_frame) frames_q <= frames_q + 32'd1;
      if (ev_ramwr) ramwr_q  <= ramwr_q  + 32'd1;
      if (ev_rst)   resets_q <= resets_q + 32'd1;
      if (ev_byte)  bytes_q  <= bytes_q  + 32'd1;
      if (ev_viol)  viol_q   <= viol_q   + 32'd1;
      if (ev_oob)   oob_q    <= oob_q    + 32'd1;
      if (ev_rds)   rds_q    <= rds_q    + 32'd1;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      viol_st_q <= 1'b0; oob_st_q <= 1'b0; rds_st_q <= 1'b0;
    end else begin
      viol_st_q <= ev_viol | (viol_st_q & ~clr_sticky_w);
      oob_st_q  <= ev_oob  | (oob_st_q  & ~clr_sticky_w);
      rds_st_q  <= ev_rds  | (rds_st_q  & ~clr_sticky_w);
    end
  end

  // ===========================================================================
  // AXI4-Lite READ channel. Not the bare template: a frame-buffer read has two
  // cycles of BRAM latency, so a read is ACCEPTED only when none is in flight
  // (rd_busy_q), and RVALID rises three cycles after acceptance for every
  // address (CSR or frame buffer alike). rd_busy_q also kills the template's
  // phantom re-acceptance of a still-high ARVALID (regmap.py read() note).
  // ===========================================================================
  logic               axi_arready_q, axi_rvalid_q, rd_busy_q;
  logic [LADDR_W-1:0] rd_addr_q;
  logic [2:0]         rd_pipe_q;
  logic [31:0]        axi_rdata_q, csr_q;
  logic               fb_sel_q, fb_ok_q;

  assign s_axi_arready = axi_arready_q;
  assign s_axi_rvalid  = axi_rvalid_q;
  assign s_axi_rresp   = 2'b00;
  assign s_axi_rdata   = axi_rdata_q;

  wire rd_accept = s_axi_arvalid && ~rd_busy_q && ~axi_arready_q;

  // -- read address decode (from the captured address, stable while busy) -----
  wire [17:0] rla    = 18'(rd_addr_q);
  wire        rcsr   = (BANKED != 0) ? (rla[15:9] == 7'd0) : (rla[17:9] == 9'd0);
  wire [6:0]  ridx   = rla[8:2];
  wire        rfb    = (BANKED != 0) ? rla[15] : (rla[17:16] != 2'b00);
  wire [15:0] rfb_w  = (BANKED != 0) ? {fb_bank_q, rla[14:2]}
                                     : (rla[17:2] - 16'h4000);
  wire        rfb_ok = rfb && (rfb_w < 16'(FBWORDS));

  assign fb_raddr = rfb_w;

  // -- the CSR read mux (combinational from registers; sampled one cycle after
  //    acceptance) ---------------------------------------------------------------
  wire [319:0] dirty_pad = {20'd0, dirty_snap_q};
  wire [319:0] valid_pad = {20'd0, valid_q};

  wire display_on = st_r28[5] & st_r28[4] & st_r28[3] & st_r28[2];   // GON.DTE.D=11
  wire fmt_ok     = (st_r17[2:0] == 3'd5) || (st_r17[2:0] == 3'd6);
  wire approx     = (st_r17[2:0] == 3'd6);

  wire [31:0] status_val = {16'd0,
                            (BANKED != 0),      // [15] banked map (addition, README)
                            4'd0,               // [14:11]
                            rds_st_q,           // [10] rd_seen*
                            oob_st_q,           // [9]  oob*
                            viol_st_q,          // [8]  viol*
                            approx,             // [7]
                            fmt_ok,             // [6]
                            (st_idx == 8'h22),  // [5]  in_gram
                            st_r1f[0],          // [4]  standby (STB)
                            display_on,         // [3]
                            st_owner,           // [2]
                            st_bl,              // [1]
                            st_rst_n};          // [0]

  logic [31:0] csr_rdata;
  always_comb begin
    csr_rdata = 32'd0;
    if (rcsr) begin
      if (ridx[6]) begin
        // 0x100-0x1FF REGS[0..63]
        csr_rdata = {regs_b3[ridx[5:0]], regs_b2[ridx[5:0]],
                     regs_b1[ridx[5:0]], regs_b0[ridx[5:0]]};
      end else begin
        case (ridx)
          7'h00: csr_rdata = ID_VAL;
          7'h01: csr_rdata = VERSION_VAL;
          7'h02: csr_rdata = GEOM_VAL;
          7'h03: csr_rdata = {29'd0, dirty_all_q, flip_conv_q, ac_load_q};
          7'h04: csr_rdata = status_val;
          7'h05: csr_rdata = seq_q;
          7'h06: csr_rdata = frames_q;
          7'h07: csr_rdata = ramwr_q;
          7'h08: csr_rdata = resets_q;
          7'h09: csr_rdata = bytes_q;
          7'h0A: csr_rdata = viol_q;
          7'h0B: csr_rdata = oob_q;
          7'h0C: csr_rdata = rds_q;
          7'h0D: csr_rdata = {8'd0, tmin_guard_q, tmin_hi_q, tmin_lo_q};
          7'h0E: csr_rdata = (BANKED != 0) ? {29'd0, fb_bank_q} : 32'd0;
          7'h10: csr_rdata = {7'd0, st_ec, 7'd0, st_sc};
          7'h11: csr_rdata = {7'd0, st_ep, 7'd0, st_sp};
          7'h12: csr_rdata = {7'd0, st_acy, 7'd0, st_acx};
          7'h13: csr_rdata = {st_r01, st_r36, st_r17, st_r16};
          7'h14: csr_rdata = snap_seq_q;
          7'h15: csr_rdata = {7'd0, sbx_max_q, 7'd0, sbx_min_q};
          7'h16: csr_rdata = {7'd0, sby_max_q, 7'd0, sby_min_q};
          7'h20, 7'h21, 7'h22, 7'h23, 7'h24,
          7'h25, 7'h26, 7'h27, 7'h28, 7'h29:
                 csr_rdata = dirty_pad[32*(ridx - 7'h20) +: 32];
          7'h30, 7'h31, 7'h32, 7'h33, 7'h34,
          7'h35, 7'h36, 7'h37, 7'h38, 7'h39:
                 csr_rdata = valid_pad[32*(ridx - 7'h30) +: 32];
          default: csr_rdata = 32'd0;
        endcase
      end
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_arready_q <= 1'b0;
      axi_rvalid_q  <= 1'b0;
      rd_busy_q     <= 1'b0;
      rd_addr_q     <= '0;
      rd_pipe_q     <= 3'b000;
      axi_rdata_q   <= 32'd0;
      csr_q         <= 32'd0;
      fb_sel_q      <= 1'b0;
      fb_ok_q       <= 1'b0;
    end else begin
      axi_arready_q <= rd_accept;
      if (rd_accept) begin
        rd_busy_q <= 1'b1;
        rd_addr_q <= s_axi_araddr[LADDR_W-1:0];
      end
      rd_pipe_q <= {rd_pipe_q[1:0], rd_accept};
      if (rd_pipe_q[0]) begin
        csr_q    <= csr_rdata;
        fb_sel_q <= rfb;
        fb_ok_q  <= rfb_ok;
      end
      if (rd_pipe_q[2]) begin
        axi_rvalid_q <= 1'b1;
        // {odd, even}: even x in [15:0], so a little-endian uint16_t[] read of
        // the window is px[vy*320+vx] as-is (LCD_MIRROR_FPGA.md §4).
        axi_rdata_q  <= fb_sel_q ? (fb_ok_q ? {fb_rd_odd, fb_rd_even} : 32'd0) : csr_q;
      end else if (axi_rvalid_q && s_axi_rready) begin
        axi_rvalid_q <= 1'b0;
        rd_busy_q    <= 1'b0;
      end
    end
  end

  // ===========================================================================
  // Unused-bit sink for the -Wall self-check (the clcd_kvm.sv idiom).
  // ===========================================================================
  // verilator lint_off UNUSED
  wire _unused_ok = &{1'b0,
                      s_axi_awprot, s_axi_arprot,
                      s_axi_wdata[C_S_AXI_DATA_WIDTH-1:24],
                      s_axi_wstrb[C_S_AXI_DATA_WIDTH/8-1:3],
                      s_axi_awaddr, s_axi_araddr,   // bits above the region (BD width 32)
                      wla[1:0], rla[1:0],
                      st_r1f[7:1], st_r28[7:6], st_r28[1:0],   // only STB / GON.DTE.D are decoded
                      1'b0};
  // verilator lint_on UNUSED

endmodule
