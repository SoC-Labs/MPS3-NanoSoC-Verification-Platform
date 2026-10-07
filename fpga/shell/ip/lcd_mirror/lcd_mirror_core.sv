// -----------------------------------------------------------------------------
// lcd_mirror_core.sv -- the 8080 tap, the timing guards and the HX8347-D GRAM
// model of the pixel-exact LCD mirror. Instantiated once by lcd_mirror.sv.
//
// Contract: docs/planning/linux_lanes/LCD_MIRROR_FPGA.md §2 (the snooper) and
// this directory's README.md. The executable spec is the golden model
// tests/lcd_mirror/hx8347_gram_model.py (GRAM model) plus
// tests/lcd_mirror/pad_decoder.py (tap + guards): if this file and those two
// disagree, the bench says so pixel for pixel.
//
// INPUT-ONLY. Every tap_* input is a fan-out of a net clcd_kvm_0 already drives
// to the pads (or owner_o). Nothing here drives the panel, so the proven panel
// path keeps its gates unchanged.
//
// ONE CLOCK DOMAIN, NO SYNCHRONISER. clcd_kvm's pad outputs are a combinational
// mux of registers clocked by s_axi_aclk (the harness side is clcd_core's
// registered FSM; the DUT side is the KVM's 2-FF + stability-filtered tunnel),
// so this block, on the same clock, sees every pad transition cycle for cycle
// (LCD_MIRROR_FPGA.md §1.2). The tap is registered once (stage t1) and delayed
// once more (t2) for edge detection. Do NOT add ASYNC_REG / a synchroniser here
// "to be safe": it would not be wrong, but it would hide a real mistake if a
// future integration ever wired this to an asynchronous net.
//
// PIPELINE (all s_axi_aclk):
//   t1/t2     tap registers (t1 = the pads one cycle ago, t2 = two)
//   decode    byte event at a WR_n rising edge with CS_n low -> index/register/
//             pixel-byte handling, address counter (AC) advance, pixel request
//   p1        MADCTL -> viewer transform, GRAM range check
//   p2        frame-buffer word address, tile one-hots   (= the px_* outputs)
//   (top)     RAM write + dirty/valid/bbox/SEQ update, one cycle after p2
// It is fully pipelined (one pixel per cycle), so no stream the pads can carry
// -- at most one transition per cycle -- can overrun it.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module lcd_mirror_core #(
  // Panel register defaults after CLCD_RST (open item O3). The GRAM extents are
  // the controller's portrait-native 240 columns x 320 rows (certain: that IS
  // the glass); the MADCTL/COLMOD/R01/R1F/R28/R36 values are ASSUMED pending the
  // Himax datasheet, and the golden model states the same numbers
  // (hx8347_gram_model.DEFAULTS). They only matter to a DUT that draws without
  // programming them; the harness and clcd_demo program all of them.
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
  input  logic        clk,
  input  logic        rst_n,            // shell reset (s_axi_aresetn), sync, active-low

  // ==== The tap: pad-side nets after the KVM mux (input-only) ================
  input  logic [7:0]  tap_pd,           // clcd_kvm_0/clcd_pd_o
  input  logic        tap_cs_n,         // clcd_kvm_0/clcd_cs_n_o
  input  logic        tap_wr_n,         // clcd_kvm_0/clcd_wr_n_o  (panel latches on the RISING edge)
  input  logic        tap_rs,           // clcd_kvm_0/clcd_rs_o    (0 = index, 1 = data)
  input  logic        tap_rd_n,         // clcd_kvm_0/clcd_rd_n_o  (const 1 while READ_PATH=0)
  input  logic        tap_rst_n,        // clcd_kvm_0/clcd_rst_n_o (KVM pulses it on every handover)
  input  logic        tap_bl,           // clcd_kvm_0/clcd_bl_o
  input  logic        tap_owner,        // clcd_kvm_0/owner_o      (0 = harness, 1 = DUT)

  // ==== Configuration (CSRs in lcd_mirror.sv) ================================
  input  logic        cfg_ac_load,      // O1: 0 = AC loads on a start-register write, 1 = on index 0x22
  input  logic        cfg_flip_conv,    // O2: 0 = MX/MY flip the physical axes, 1 = the logical axes
  input  logic [7:0]  cfg_tmin_lo,      // minimum WR-low, cycles
  input  logic [7:0]  cfg_tmin_hi,      // minimum WR-high, cycles
  input  logic [7:0]  cfg_tmin_guard,   // minimum PD/RS stability either side of the WR pulse, cycles

  // ==== Event strobes (one cycle each; counted in lcd_mirror.sv) =============
  output logic        ev_byte,          // an 8080 write cycle was decoded
  output logic        ev_ramwr,         // index 0x22 written
  output logic        ev_frame,         // AC wrapped past EP
  output logic        ev_rst,           // CLCD_RST asserted (falling edge)
  output logic        ev_viol,          // a timing-guard violation (at most one per cycle)
  output logic        ev_rds,           // a CLCD_RD strobe (falling edge)
  output logic        ev_oob,           // a pixel fell outside GRAM (p2)

  // ==== Raw register log write (REGS[idx] <- datum) ===========================
  output logic        reg_we,
  output logic [7:0]  reg_idx,
  output logic [7:0]  reg_dat,

  // ==== Pixel write (the p2 stage; lcd_mirror.sv applies it next edge) =======
  output logic        px_we,            // an in-range pixel
  output logic [15:0] px_word,          // frame-buffer word (vy*320+vx) >> 1
  output logic        px_odd,           // vx[0]: 1 = the word's [31:16] half
  output logic [15:0] px_data,          // RGB565, as sent (high byte first on the bus)
  output logic [8:0]  px_vx,
  output logic [7:0]  px_vy,
  output logic [14:0] px_ty_oh,         // one-hot tile row    (vy >> 4)
  output logic [19:0] px_tx_oh,         // one-hot tile column (vx >> 4)
  output logic        px_valid_ok,      // 0 if CLCD_RST was seen after the byte was latched

  // ==== Live state (STATUS / WIN / AC / MODE) ================================
  output logic        st_rst_n,         // pad CLCD_RST (t1)
  output logic        st_bl,            // pad CLCD_BL  (t1)
  output logic        st_owner,         // owner_o      (t1)
  output logic        panel_rst,        // = ~st_rst_n: the decoded state is held at its defaults
  output logic [7:0]  st_idx,
  output logic [8:0]  st_sc, st_ec, st_sp, st_ep,
  output logic [8:0]  st_acx, st_acy,
  output logic [7:0]  st_r01, st_r16, st_r17, st_r1f, st_r28, st_r36
);

  localparam logic [7:0] IDX_RAMWR = 8'h22;

  // ===========================================================================
  // Tap registers. Reset value = pads idle with the panel IN RESET: the harness
  // CLCD block resets to CTRL=0 (panel held in reset) on the same shell reset,
  // so a shell reset must not itself look like a CLCD_RST falling edge.
  // ===========================================================================
  logic [7:0] pd_1, pd_2;
  logic       cs_n_1, cs_n_2, wr_n_1, wr_n_2, rs_1, rs_2, rd_n_1, rd_n_2;
  logic       rstn_1, rstn_2, bl_1, own_1;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      pd_1 <= 8'h00;  rs_1 <= 1'b0;  cs_n_1 <= 1'b1;  wr_n_1 <= 1'b1;
      rd_n_1 <= 1'b1; rstn_1 <= 1'b0; bl_1 <= 1'b0;   own_1 <= 1'b0;
      pd_2 <= 8'h00;  rs_2 <= 1'b0;  cs_n_2 <= 1'b1;  wr_n_2 <= 1'b1;
      rd_n_2 <= 1'b1; rstn_2 <= 1'b0;
    end else begin
      pd_1 <= tap_pd;   rs_1 <= tap_rs;   cs_n_1 <= tap_cs_n; wr_n_1 <= tap_wr_n;
      rd_n_1 <= tap_rd_n; rstn_1 <= tap_rst_n; bl_1 <= tap_bl; own_1 <= tap_owner;
      pd_2 <= pd_1;     rs_2 <= rs_1;     cs_n_2 <= cs_n_1;   wr_n_2 <= wr_n_1;
      rd_n_2 <= rd_n_1; rstn_2 <= rstn_1;
    end
  end

  assign st_rst_n  = rstn_1;
  assign st_bl     = bl_1;
  assign st_owner  = own_1;
  assign panel_rst = ~rstn_1;

  // The panel is out of reset for both samples an edge is judged on.
  wire live     = rstn_1 & rstn_2;
  wire same_1   = ({rs_1, pd_1} == {rs_2, pd_2});   // PD/RS unchanged since last cycle
  wire wr_fall  =  wr_n_2 & ~wr_n_1;
  wire wr_rise  = ~wr_n_2 &  wr_n_1;
  // THE byte event: WR_n rising while CS_n was low in the last WR-low cycle.
  // {rs_2, pd_2} is the value in that last low cycle -- the panel's latch point.
  wire byte_evt = wr_rise & ~cs_n_2 & live;

  // ===========================================================================
  // Timing guards (VIOL). Widths in cycles, saturating at 255.
  //   lo_w  : consecutive WR-low cycles up to the previous cycle
  //   hi_w  : consecutive WR-high cycles up to the previous cycle (reset 255:
  //           the first strobe after reset is not "too soon")
  //   stab  : consecutive cycles {RS,PD} has been unchanged, up to the previous
  //           cycle; setup at a WR fall = same_1 ? stab+1 : 0
  //   hold_rem : cycles of PD/RS hold still owed after a WR rise
  // A violation is counted at most once per cycle (ev_viol), whatever its kind:
  //   * WR-low shorter than TMIN.lo                       (at the byte event)
  //   * WR-high shorter than TMIN.hi                      (at a WR fall, CS low)
  //   * PD/RS stable for fewer than TMIN.guard cycles before WR fell (CS low)
  //   * PD/RS changed while WR stayed low                 (CS low)
  //   * PD/RS changed within TMIN.guard cycles after WR rose (hold)
  //   * CS_n rose while WR_n was still low (incl. the same cycle as the WR rise)
  // ===========================================================================
  logic [7:0] lo_w, hi_w, stab, hold_rem;

  function automatic logic [7:0] sat_inc(input logic [7:0] v);
    sat_inc = (v == 8'hFF) ? 8'hFF : (v + 8'd1);
  endfunction

  wire [7:0] setup_now = same_1 ? sat_inc(stab) : 8'd0;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      lo_w     <= 8'd0;
      hi_w     <= 8'hFF;
      stab     <= 8'hFF;
      hold_rem <= 8'd0;
    end else begin
      if (!wr_n_1) lo_w <= wr_n_2 ? 8'd1 : sat_inc(lo_w);
      if ( wr_n_1) hi_w <= wr_n_2 ? sat_inc(hi_w) : 8'd1;
      stab <= same_1 ? sat_inc(stab) : 8'd0;
      if (byte_evt)
        hold_rem <= (same_1 && (cfg_tmin_guard > 8'd1)) ? (cfg_tmin_guard - 8'd1) : 8'd0;
      else if (hold_rem != 8'd0)
        hold_rem <= same_1 ? (hold_rem - 8'd1) : 8'd0;
    end
  end

  wire v_lo    = byte_evt & (lo_w < cfg_tmin_lo);
  wire v_hi    = wr_fall & ~cs_n_1 & (hi_w < cfg_tmin_hi);
  wire v_setup = wr_fall & ~cs_n_1 & (setup_now < cfg_tmin_guard);
  wire v_chg   = ~wr_n_1 & ~wr_n_2 & ~cs_n_1 & ~same_1;
  wire v_hold0 = byte_evt & ~same_1 & (cfg_tmin_guard != 8'd0);
  wire v_hold  = ~byte_evt & (hold_rem != 8'd0) & ~same_1;
  wire v_cs    = ~cs_n_2 & cs_n_1 & ~wr_n_2;

  assign ev_viol = live & (v_lo | v_hi | v_setup | v_chg | v_hold0 | v_hold | v_cs);
  assign ev_rds  = live & rd_n_2 & ~rd_n_1;
  assign ev_rst  = rstn_2 & ~rstn_1;
  assign ev_byte = byte_evt;

  // ===========================================================================
  // The HX8347-D model (LCD_MIRROR_FPGA.md §2.3-2.6).
  //   index byte (RS=0) : idx <- B; pixel byte phase <- 0; on 0x22 count RAMWR
  //                       and, with ac_load=1, load AC from (SC,SP)
  //   data, idx != 0x22 : REGS[idx] <- D; decode 02-09 (9-bit hi/lo: hi[0] is
  //                       bit 8), 16, 17, and the recorded 01/1F/28/36; with
  //                       ac_load=0 a write to 02/03 loads AC.x, 06/07 AC.y
  //   data, idx == 0x22 : assemble a pixel (COLMOD[2:0]=6: 3 bytes, 18 bpp,
  //                       truncated to 565; anything else: 2 bytes, high first),
  //                       request its write at the current AC, then advance AC:
  //                       x SC..EC, then SC with y+1; after EP, y <- SP, FRAMES++
  // While CLCD_RST is low every decoded field is held at its default. REGS is a
  // raw log and is NOT reset (MODE/STATUS carry the effective values).
  // ===========================================================================
  logic [7:0]  idx_q;
  logic [1:0]  phase_q;
  logic [7:0]  b0_q, b1_q;
  logic [8:0]  sc_q, ec_q, sp_q, ep_q, acx_q, acy_q;
  logic [7:0]  r01_q, r16_q, r17_q, r1f_q, r28_q, r36_q;

  logic        req_q;          // pixel request (to p1)
  logic [8:0]  req_x_q, req_y_q;
  logic [15:0] req_px_q;
  logic [2:0]  req_mad_q;      // {MY, MX, MV}
  logic        ev_frame_q, ev_ramwr_q;

  wire       rs_b = rs_2;
  wire [7:0] d_b  = pd_2;
  wire       fmt18 = (r17_q[2:0] == 3'd6);
  wire       px_last = fmt18 ? (phase_q == 2'd2) : (phase_q == 2'd1);
  wire [15:0] px_asm = fmt18 ? {b0_q[7:3], b1_q[7:2], d_b[7:3]} : {b0_q, d_b};

  assign reg_we  = byte_evt & rs_b & (idx_q != IDX_RAMWR);
  assign reg_idx = idx_q;
  assign reg_dat = d_b;

  always_ff @(posedge clk) begin
    ev_frame_q <= 1'b0;
    ev_ramwr_q <= 1'b0;
    req_q      <= 1'b0;
    if (!rst_n || !rstn_1) begin
      idx_q   <= 8'h00;  phase_q <= 2'd0;  b0_q <= 8'h00;  b1_q <= 8'h00;
      sc_q    <= RST_SC; ec_q    <= RST_EC; sp_q <= RST_SP; ep_q <= RST_EP;
      acx_q   <= 9'd0;   acy_q   <= 9'd0;
      r01_q   <= RST_R01; r16_q <= RST_R16; r17_q <= RST_R17;
      r1f_q   <= RST_R1F; r28_q <= RST_R28; r36_q <= RST_R36;
      if (!rst_n) begin
        req_x_q <= 9'd0; req_y_q <= 9'd0; req_px_q <= 16'h0000; req_mad_q <= 3'd0;
      end
    end else if (byte_evt) begin
      if (!rs_b) begin
        // ---- index byte ------------------------------------------------------
        idx_q   <= d_b;
        phase_q <= 2'd0;
        if (d_b == IDX_RAMWR) begin
          ev_ramwr_q <= 1'b1;
          if (cfg_ac_load) begin
            acx_q <= sc_q;
            acy_q <= sp_q;
          end
        end
      end else if (idx_q != IDX_RAMWR) begin
        // ---- register datum --------------------------------------------------
        case (idx_q)
          8'h01: r01_q <= d_b;
          8'h02: begin sc_q[8]   <= d_b[0]; if (!cfg_ac_load) acx_q <= {d_b[0], sc_q[7:0]}; end
          8'h03: begin sc_q[7:0] <= d_b;    if (!cfg_ac_load) acx_q <= {sc_q[8], d_b};     end
          8'h04: ec_q[8]   <= d_b[0];
          8'h05: ec_q[7:0] <= d_b;
          8'h06: begin sp_q[8]   <= d_b[0]; if (!cfg_ac_load) acy_q <= {d_b[0], sp_q[7:0]}; end
          8'h07: begin sp_q[7:0] <= d_b;    if (!cfg_ac_load) acy_q <= {sp_q[8], d_b};     end
          8'h08: ep_q[8]   <= d_b[0];
          8'h09: ep_q[7:0] <= d_b;
          8'h16: r16_q <= d_b;
          8'h17: r17_q <= d_b;
          8'h1F: r1f_q <= d_b;
          8'h28: r28_q <= d_b;
          8'h36: r36_q <= d_b;
          default: ;
        endcase
      end else begin
        // ---- GRAM pixel byte ---------------------------------------------------
        if (!px_last) begin
          if (phase_q == 2'd0) b0_q <= d_b;
          else                 b1_q <= d_b;
          phase_q <= phase_q + 2'd1;
        end else begin
          phase_q   <= 2'd0;
          req_q     <= 1'b1;
          req_x_q   <= acx_q;
          req_y_q   <= acy_q;
          req_px_q  <= px_asm;
          req_mad_q <= {r16_q[7], r16_q[6], r16_q[5]};
          // Address counter advance (the controller's logical x-first order;
          // MADCTL maps it to the glass downstream, in p1).
          if (acx_q == ec_q) begin
            acx_q <= sc_q;
            if (acy_q == ep_q) begin
              acy_q      <= sp_q;
              ev_frame_q <= 1'b1;
            end else begin
              acy_q <= acy_q + 9'd1;
            end
          end else begin
            acx_q <= acx_q + 9'd1;
          end
        end
      end
    end
  end

  assign ev_frame = ev_frame_q;
  assign ev_ramwr = ev_ramwr_q;

  assign st_idx = idx_q;
  assign st_sc  = sc_q;  assign st_ec = ec_q;  assign st_sp = sp_q;  assign st_ep = ep_q;
  assign st_acx = acx_q; assign st_acy = acy_q;
  assign st_r01 = r01_q; assign st_r16 = r16_q; assign st_r17 = r17_q;
  assign st_r1f = r1f_q; assign st_r28 = r28_q; assign st_r36 = r36_q;

  // ===========================================================================
  // p1: MADCTL -> viewer order (§2.4). The frame buffer is stored as the viewer
  // sees the glass; the reference is the proven MADCTL 0x20 (MV only), for which
  // the harness's logical frame IS the viewer frame.
  //   exchange : MV ? (g0,s0) = (x,y) : (g0,s0) = (y,x)   g = 320 axis, s = 240
  //   flips    : flip_conv=0 (physical axes): MY flips g, MX flips s
  //              flip_conv=1 (logical axes) : MX flips x, MY flips y, i.e. with
  //              MV set MX flips g and MY flips s (MV clear: same as 0)
  //   range    : g0 <= 319 and s0 <= 239, else OOB (counted, not written)
  // 0x20 -> identity; 0xE0 -> 180 degrees, under either convention.
  // ===========================================================================
  logic        p1_v, p1_ok, p1_in;
  logic [8:0]  p1_g, p1_s;
  logic [15:0] p1_px;

  wire       mv = req_mad_q[0];
  wire       mx = req_mad_q[1];
  wire       my = req_mad_q[2];
  wire [8:0] g0 = mv ? req_x_q : req_y_q;
  wire [8:0] s0 = mv ? req_y_q : req_x_q;
  wire       flip_g = cfg_flip_conv ? (mv ? mx : my) : my;
  wire       flip_s = cfg_flip_conv ? (mv ? my : mx) : mx;
  wire       in_rng = (g0 <= 9'd319) && (s0 <= 9'd239);

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      p1_v <= 1'b0; p1_ok <= 1'b0; p1_in <= 1'b0;
      p1_g <= 9'd0; p1_s <= 9'd0;  p1_px <= 16'h0000;
    end else begin
      p1_v  <= req_q;
      p1_ok <= rstn_1;     // CLCD_RST seen after the latch: the pixel is not VALID
      p1_in <= in_rng;
      p1_g  <= flip_g ? (9'd319 - g0) : g0;
      p1_s  <= flip_s ? (9'd239 - s0) : s0;
      p1_px <= req_px_q;
    end
  end

  // ===========================================================================
  // p2: word address and tile one-hots. addr = s*320 + g = (s<<8) + (s<<6) + g.
  // ===========================================================================
  wire [16:0] p1_addr = {p1_s, 8'd0} + {2'b00, p1_s, 6'd0} + {8'd0, p1_g};
  logic       ev_oob_q;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      px_we <= 1'b0; ev_oob_q <= 1'b0; px_valid_ok <= 1'b0;
      px_word <= 16'd0; px_odd <= 1'b0; px_data <= 16'h0000;
      px_vx <= 9'd0; px_vy <= 8'd0; px_ty_oh <= '0; px_tx_oh <= '0;
    end else begin
      px_we       <= p1_v &  p1_in;
      ev_oob_q    <= p1_v & ~p1_in;
      px_valid_ok <= p1_ok & rstn_1;
      px_word     <= p1_addr[16:1];
      px_odd      <= p1_addr[0];
      px_data     <= p1_px;
      px_vx       <= p1_g;
      px_vy       <= p1_s[7:0];
      px_ty_oh    <= 15'd1 << p1_s[7:4];
      px_tx_oh    <= 20'd1 << p1_g[8:4];
    end
  end

  assign ev_oob = ev_oob_q;

  // verilator lint_off UNUSED
  wire _unused_ok = &{1'b0, p1_s[8], b1_q[1:0], 1'b0};   // 18 bpp keeps G[5:0] = b1[7:2]
  // verilator lint_on UNUSED

endmodule
