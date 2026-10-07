// -----------------------------------------------------------------------------
// link_partner_mac.sv — on-chip link-partner MAC (recovers/generates frames
// toward the DUT via rmii_phy_if's MII bus; AXI-Stream toward bridge/).
//
// Real RTL (W-RTL-ETH, A1/A5). Hand-rolled minimal MAC framing glue — NOT the
// verilog-ethernet eth_mac_mii composition the Phase-0 notes suggested: no
// copy of verilog-ethernet exists on this machine and the repo consumes
// external IP read-only (same vendoring decision as fpga/ethernet/bridge/,
// see that file's header + the block READMEs). What a MAC *needs* to be here
// is small: preamble/SFD framing both directions + an independent FCS-check
// tap (tuser) — CRC generation/stripping stays with the AXIS frames.
//
// ---------------------------------------------------------------------------
// CLOCKING: single-domain by design. clk_i MUST be the same 50 MHz net that
// drives rmii_phy_if's refclk_i (shell fixed clock). The mii_rx_clk_i /
// mii_tx_clk_i inputs are rmii_phy_if's divide-by-2 pacing clocks — clean
// flop outputs in this same domain — and are used here as cycle ENABLES
// (edge-detected at clk_i), not as sampling clocks, so there is no CDC
// inside this pairing (fpga/ethernet/README.md's "first crossing" lives at
// the partition boundary inside rmii_phy_if). If a future clocking plan
// breaks this assumption, real MII clock domains + async FIFOs go here.
//
// ---------------------------------------------------------------------------
// AXIS FRAME CONVENTION (both directions): frames INCLUDE the 4-byte FCS —
// same convention as gen_checker and tests/common/frames.py. This MAC does
// not insert or strip FCS; it independently *checks* it on RX and reports
// via m_axis_rx_tuser.
//
// WHAT THE v1 MODEL DOES vs DOES NOT DO
//
// TX (s_axis_tx -> MII): store-and-forward of ONE frame (2 KB buffer, same
//   single-frame policy as the bridge ingress); emits 7x0x55 preamble +
//   0xD5 SFD + frame nibbles (LSB-first), then enforces a 96-bit IFG before
//   accepting the next frame. NOT modelled: FCS insertion (frames carry
//   it), padding to 64 B (generator's job), half-duplex deferral/collision.
//   Oversize frames (> buffer) are swallowed and dropped, never truncated
//   onto the wire.
// RX (MII -> m_axis_rx): preamble hunt + SFD strip; byte assembly
//   (low-nibble-first); one-byte lookahead so tlast lands on the true final
//   byte; streaming CRC-32 with residue check -> m_axis_rx_tuser (1 = bad
//   FCS, or a non-whole-byte frame, or an overrun drop occurred). NOT
//   modelled: length policing (gen_checker owns the 64..1518 envelope),
//   RX_ER handling (rmii_phy_if ties it low — input accepted and folded
//   into tuser for forward-compatibility), promiscuity/address filtering
//   (deliberately promiscuous: everything goes to the bridge).
//   Backpressure: the wire cannot be backpressured, and v1 adds no RX FIFO —
//   a byte not taken by the time the next one completes (4 clk cycles) is
//   DROPPED and the frame's tuser set. The in-tree sink (bridge ingress) is
//   always-ready while receiving; if it parks (frame pending forward), the
//   loss is per-frame-flagged. A6 flag: v2 wants a small RX FIFO here.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module link_partner_mac (
  input  logic       clk_i,   // shell fixed 50 MHz — same net as rmii_phy_if
                              // refclk_i (see CLOCKING header note)
  input  logic       rst_i,

  // ---------------------------------------------------------------------
  // MII side — toward fpga/ethernet/rmii_phy_if/rmii_phy_if.sv
  // (this module is the MAC half of the MII pairing).
  // ---------------------------------------------------------------------
  input  logic [3:0] mii_rxd_i,
  input  logic       mii_rx_dv_i,
  input  logic       mii_rx_er_i,
  input  logic       mii_rx_clk_i,
  output logic [3:0] mii_txd_o,
  output logic       mii_tx_en_o,
  input  logic       mii_tx_clk_i,

  // ---------------------------------------------------------------------
  // AXI-Stream RX — frames recovered from the DUT (DUT TX -> here -> bridge)
  // ---------------------------------------------------------------------
  output logic [7:0] m_axis_rx_tdata,
  output logic       m_axis_rx_tvalid,
  input  logic       m_axis_rx_tready,
  output logic       m_axis_rx_tlast,
  output logic       m_axis_rx_tuser,   // frame error (bad FCS/framing) — see
                                        // gen_checker/ for independent scoring

  // ---------------------------------------------------------------------
  // AXI-Stream TX — frames destined for the DUT (bridge -> here -> DUT RX)
  // ---------------------------------------------------------------------
  input  logic [7:0] s_axis_tx_tdata,
  input  logic       s_axis_tx_tvalid,
  output logic       s_axis_tx_tready,
  input  logic       s_axis_tx_tlast
);

  // ===========================================================================
  // Shared CRC-32 (IEEE 802.3 / zlib variant) — same function/constants as
  // gen_checker.sv (kept module-local: both files stay single-file builds).
  // ===========================================================================
  function automatic logic [31:0] crc32_byte(input logic [31:0] c,
                                             input logic [7:0]  b);
    logic [31:0] x;
    x = c ^ {24'h0, b};
    for (int i = 0; i < 8; i++)
      x = x[0] ? ((x >> 1) ^ 32'hEDB8_8320) : (x >> 1);
    return x;
  endfunction

  localparam logic [31:0] CRC_INIT    = 32'hFFFF_FFFF;
  localparam logic [31:0] CRC_RESIDUE = 32'hDEBB_20E3;

  localparam logic [3:0] NIB_PRE = 4'h5;  // preamble nibble (0x55 bytes)
  localparam logic [3:0] NIB_SFD = 4'hD;  // SFD high nibble (0xD5, low-first)

  // ===========================================================================
  // MII pacing enables (see CLOCKING note): the divided clocks are sampled
  // pre-edge inside always_ff blocks.
  //   * TX advance edges ("A-edges"): clk edges where mii_tx_clk_i == 0
  //     (the divided clock rises there) — outputs updated only then, so
  //     rmii_phy_if's mid-period sample edge always sees stable values.
  //   * RX capture edges: rising edge of mii_rx_clk_i detected via a 1-flop
  //     history — data/dv sampled at the last stable moment of their
  //     2-cycle presentation window.
  // ===========================================================================
  logic tx_ce;
  assign tx_ce = ~mii_tx_clk_i;

  logic rx_clk_prev_q;
  logic rx_ce;
  assign rx_ce = mii_rx_clk_i & ~rx_clk_prev_q;

  always_ff @(posedge clk_i or posedge rst_i) begin
    if (rst_i) rx_clk_prev_q <= 1'b0;
    else       rx_clk_prev_q <= mii_rx_clk_i;
  end

  // ===========================================================================
  // TX: AXIS ingress — single-frame store-and-forward buffer (same pattern
  // as eth_bridge_3port's ingress; see that file for the rationale).
  // ===========================================================================
  localparam int unsigned TX_AW = 11;   // 2 KB frame buffer

  logic              tx_recv_q, tx_pend_q, tx_drop_q;
  logic [TX_AW-1:0]  tx_wr_ptr_q;
  logic [TX_AW:0]    tx_len_q;
  logic              tx_rel;            // TX engine done with the frame

  logic [7:0]        tx_mem [0:(1 << TX_AW)-1];
  logic [TX_AW-1:0]  tx_rd_ptr_q;
  logic [7:0]        tx_byte_q;         // registered prefetch read

  assign s_axis_tx_tready = tx_recv_q;

  always_ff @(posedge clk_i) begin
    if (tx_recv_q && s_axis_tx_tvalid && !tx_drop_q)
      tx_mem[tx_wr_ptr_q] <= s_axis_tx_tdata;
    tx_byte_q <= tx_mem[tx_rd_ptr_q];
  end

  always_ff @(posedge clk_i or posedge rst_i) begin
    if (rst_i) begin
      tx_recv_q   <= 1'b1;
      tx_pend_q   <= 1'b0;
      tx_drop_q   <= 1'b0;
      tx_wr_ptr_q <= '0;
      tx_len_q    <= '0;
    end else if (tx_recv_q) begin
      if (s_axis_tx_tvalid) begin
        if (s_axis_tx_tlast) begin
          if (tx_drop_q) begin          // oversize: swallowed, not sent
            tx_recv_q   <= 1'b1;
            tx_drop_q   <= 1'b0;
            tx_wr_ptr_q <= '0;
          end else begin
            tx_recv_q <= 1'b0;
            tx_pend_q <= 1'b1;
            tx_len_q  <= {1'b0, tx_wr_ptr_q} + 1'b1;
          end
        end else begin
          if (tx_wr_ptr_q == {TX_AW{1'b1}})
            tx_drop_q <= 1'b1;
          else
            tx_wr_ptr_q <= tx_wr_ptr_q + 1'b1;
        end
      end
    end else if (tx_rel) begin
      tx_recv_q   <= 1'b1;
      tx_pend_q   <= 1'b0;
      tx_wr_ptr_q <= '0;
    end
  end

  // ---------------------------------------------------------------------
  // TX: MII emit engine. All state advances only on A-edges (tx_ce), so
  // mii_txd_o/mii_tx_en_o change exactly once per 25 MHz period.
  // Nibble schedule: 15 x 0x5, 1 x 0xD (preamble+SFD, LSB-first within
  // bytes), then 2 nibbles per data byte (low first), then >= 24 idle
  // nibble-times (96-bit IFG).
  // ---------------------------------------------------------------------
  typedef enum logic [1:0] {TX_IDLE, TX_PRE, TX_DATA, TX_IFG} tx_state_e;

  tx_state_e         txs_q;
  logic [3:0]        tx_pre_cnt_q;      // 0..15 preamble/SFD nibbles
  logic [TX_AW:0]    tx_bidx_q;         // data byte index
  logic              tx_nib_hi_q;       // 0 = low nibble next, 1 = high
  logic [4:0]        tx_ifg_cnt_q;
  logic [3:0]        mii_txd_q;
  logic              mii_tx_en_q;

  assign mii_txd_o   = mii_txd_q;
  assign mii_tx_en_o = mii_tx_en_q;

  always_ff @(posedge clk_i or posedge rst_i) begin
    if (rst_i) begin
      txs_q        <= TX_IDLE;
      tx_pre_cnt_q <= '0;
      tx_bidx_q    <= '0;
      tx_nib_hi_q  <= 1'b0;
      tx_ifg_cnt_q <= '0;
      tx_rd_ptr_q  <= '0;
      mii_txd_q    <= '0;
      mii_tx_en_q  <= 1'b0;
      tx_rel       <= 1'b0;
    end else begin
      tx_rel <= 1'b0;
      if (tx_ce) begin
        unique case (txs_q)
          TX_IDLE: begin
            mii_tx_en_q <= 1'b0;
            mii_txd_q   <= '0;
            if (tx_pend_q) begin
              tx_pre_cnt_q <= '0;
              tx_rd_ptr_q  <= '0;       // prefetch byte 0 during preamble
              txs_q        <= TX_PRE;
            end
          end

          TX_PRE: begin
            mii_tx_en_q  <= 1'b1;
            mii_txd_q    <= (tx_pre_cnt_q == 4'd15) ? NIB_SFD : NIB_PRE;
            tx_pre_cnt_q <= tx_pre_cnt_q + 4'd1;
            if (tx_pre_cnt_q == 4'd15) begin
              tx_bidx_q   <= '0;
              tx_nib_hi_q <= 1'b0;
              txs_q       <= TX_DATA;
            end
          end

          TX_DATA: begin
            mii_tx_en_q <= 1'b1;
            mii_txd_q   <= tx_nib_hi_q ? tx_byte_q[7:4] : tx_byte_q[3:0];
            if (tx_nib_hi_q) begin
              // high nibble out: advance to the next byte; prefetch it
              // (2 clk cycles until the next low-nibble A-edge — the
              // registered read lands in time, see rd path above)
              if (tx_bidx_q == tx_len_q - 1'b1) begin
                tx_ifg_cnt_q <= 5'd23;  // 24 nibble-times = 96 bits
                txs_q        <= TX_IFG;
              end else begin
                tx_bidx_q   <= tx_bidx_q + 1'b1;
                tx_rd_ptr_q <= tx_rd_ptr_q + 1'b1;
              end
            end
            tx_nib_hi_q <= ~tx_nib_hi_q;
          end

          TX_IFG: begin
            mii_tx_en_q <= 1'b0;
            mii_txd_q   <= '0;
            if (tx_ifg_cnt_q == '0) begin
              tx_rel <= 1'b1;           // 1-clk pulse (default-cleared above)
              txs_q  <= TX_IDLE;
            end else begin
              tx_ifg_cnt_q <= tx_ifg_cnt_q - 5'd1;
            end
          end

          default: txs_q <= TX_IDLE;
        endcase
      end
    end
  end

  // ===========================================================================
  // RX: preamble hunt, SFD strip, byte assembly, FCS residue check.
  // One-byte lookahead (pend_*) so tlast is asserted on the true final byte.
  // ===========================================================================
  typedef enum logic [1:0] {RX_IDLE, RX_HUNT, RX_DATA, RX_JUNK} rx_state_e;

  rx_state_e   rxs_q;
  logic        rx_nib_hi_q;     // 0 = next nibble is a byte's low half
  logic [3:0]  rx_lo_q;
  logic [31:0] rx_crc_q;
  logic        rx_err_q;        // sticky: overrun/mii_rx_er within frame

  logic        pend_vld_q;
  logic [7:0]  pend_byte_q;

  // AXIS output holding register
  logic [7:0]  out_data_q;
  logic        out_vld_q, out_last_q, out_user_q;

  assign m_axis_rx_tdata  = out_data_q;
  assign m_axis_rx_tvalid = out_vld_q;
  assign m_axis_rx_tlast  = out_last_q;
  assign m_axis_rx_tuser  = out_user_q;

  logic        out_taken;
  assign out_taken = out_vld_q && m_axis_rx_tready;

  logic [7:0]  rx_byte;
  assign rx_byte = {mii_rxd_i, rx_lo_q};

  logic        rx_frame_end;
  assign rx_frame_end = rx_ce && !mii_rx_dv_i
                        && ((rxs_q == RX_DATA) || (rxs_q == RX_HUNT)
                            || (rxs_q == RX_JUNK));

  logic [31:0] rx_crc_next;
  assign rx_crc_next = crc32_byte(rx_crc_q, rx_byte);

  always_ff @(posedge clk_i or posedge rst_i) begin
    if (rst_i) begin
      rxs_q       <= RX_IDLE;
      rx_nib_hi_q <= 1'b0;
      rx_lo_q     <= '0;
      rx_crc_q    <= CRC_INIT;
      rx_err_q    <= 1'b0;
      pend_vld_q  <= 1'b0;
      pend_byte_q <= '0;
      out_data_q  <= '0;
      out_vld_q   <= 1'b0;
      out_last_q  <= 1'b0;
      out_user_q  <= 1'b0;
    end else begin
      // AXIS commit clears the holding register
      if (out_taken) begin
        out_vld_q  <= 1'b0;
        out_last_q <= 1'b0;
      end

      if (rx_ce) begin
        unique case (rxs_q)
          RX_IDLE: begin
            if (mii_rx_dv_i) begin
              // first nibble of carrier — expect preamble
              rx_crc_q <= CRC_INIT;
              rx_err_q <= 1'b0;
              if (mii_rxd_i == NIB_PRE)      rxs_q <= RX_HUNT;
              else                           rxs_q <= RX_JUNK;
            end
          end

          RX_HUNT: begin
            if (mii_rx_dv_i) begin
              if      (mii_rxd_i == NIB_SFD) begin
                rxs_q       <= RX_DATA;      // data starts next nibble
                rx_nib_hi_q <= 1'b0;
              end
              else if (mii_rxd_i != NIB_PRE) rxs_q <= RX_JUNK;
            end
            // dv fall handled by rx_frame_end below (no SFD -> no frame)
          end

          RX_DATA: begin
            if (mii_rx_dv_i) begin
              if (mii_rx_er_i) rx_err_q <= 1'b1;
              if (!rx_nib_hi_q) begin
                rx_lo_q     <= mii_rxd_i;
                rx_nib_hi_q <= 1'b1;
              end else begin
                rx_nib_hi_q <= 1'b0;
                rx_crc_q    <= rx_crc_next;
                // byte complete: emit the previous one, park this one
                if (pend_vld_q) begin
                  if (out_vld_q && !out_taken) rx_err_q <= 1'b1; // overrun drop
                  out_data_q <= pend_byte_q;
                  out_vld_q  <= 1'b1;
                  out_last_q <= 1'b0;
                  out_user_q <= 1'b0;
                end
                pend_byte_q <= rx_byte;
                pend_vld_q  <= 1'b1;
              end
            end
          end

          RX_JUNK: ;   // non-preamble carrier: swallow until dv falls

          default: rxs_q <= RX_IDLE;
        endcase

        if (rx_frame_end) begin
          if (pend_vld_q) begin
            // final byte: tlast + independent scoring
            if (out_vld_q && !out_taken) rx_err_q <= 1'b1;       // overrun drop
            out_data_q <= pend_byte_q;
            out_vld_q  <= 1'b1;
            out_last_q <= 1'b1;
            out_user_q <= (rx_crc_q != CRC_RESIDUE)  // residue over frame+FCS
                          || rx_nib_hi_q             // non-whole-byte frame
                          || rx_err_q;
          end
          pend_vld_q  <= 1'b0;
          rx_nib_hi_q <= 1'b0;
          rxs_q       <= RX_IDLE;
        end
      end
    end
  end

endmodule
