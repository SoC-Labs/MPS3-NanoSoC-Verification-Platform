// -----------------------------------------------------------------------------
// rmii_phy_if_negctl.sv  --  NEGATIVE CONTROL (deliberately broken).
//
// A verbatim copy of fpga/ethernet/rmii_phy_if/rmii_phy_if.sv with ONE change:
// the TX di-bit order is SWAPPED (high di-bit emitted first instead of the
// low di-bit). The module name is kept as `rmii_phy_if` so the conformance
// testbench binds to it unchanged; the Makefile's `neg` target compiles THIS
// file in place of the real RTL.
//
// Purpose: prove the conformance bench can actually FAIL. A bench that cannot
// fail is worthless. With this swap, Direction B (PHY TX -> MAC RX) must report
// per-byte mismatches on the asymmetric payload and the verdict must read
// CONFORMANCE: FAIL. Direction A is unaffected (it exercises the RX path).
//
// The ONLY functional deviation from the real module is marked "NEG-CTL" below.
// Everything else is identical to the shipping RTL.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rmii_phy_if (
  input  logic       refclk_i,
  input  logic       rst_i,

  output logic       phy_rmii_ref_clk_o,
  output logic       phy_rmii_crs_dv_o,
  output logic [1:0] phy_rmii_rxd_o,
  input  logic [1:0] phy_rmii_txd_i,
  input  logic       phy_rmii_tx_en_i,

  output logic [3:0] mii_rxd_o,
  output logic       mii_rx_dv_o,
  output logic       mii_rx_er_o,
  output logic       mii_rx_clk_o,
  input  logic [3:0] mii_txd_i,
  input  logic       mii_tx_en_i,
  output logic       mii_tx_clk_o
);

  assign phy_rmii_ref_clk_o = refclk_i;

  // ---- RMII TX input re-register (unchanged) --------------------------------
  logic [1:0] phy_rmii_txd_q;
  logic       phy_rmii_tx_en_q;

  always_ff @(posedge refclk_i or posedge rst_i) begin
    if (rst_i) begin
      phy_rmii_txd_q   <= '0;
      phy_rmii_tx_en_q <= 1'b0;
    end else begin
      phy_rmii_txd_q   <= phy_rmii_txd_i;
      phy_rmii_tx_en_q <= phy_rmii_tx_en_i;
    end
  end

  // ---- RMII -> MII (RX direction, unchanged) --------------------------------
  logic       rx_busy_q;
  logic       rx_phase_q;
  logic [1:0] rx_lo_q;
  logic [3:0] rx_nib_q;
  logic       rx_nib_stb_q;

  always_ff @(posedge refclk_i or posedge rst_i) begin
    if (rst_i) begin
      rx_busy_q    <= 1'b0;
      rx_phase_q   <= 1'b0;
      rx_lo_q      <= '0;
      rx_nib_q     <= '0;
      rx_nib_stb_q <= 1'b0;
    end else begin
      rx_nib_stb_q <= 1'b0;
      if (phy_rmii_tx_en_q) begin
        if (!rx_busy_q || !rx_phase_q) begin
          rx_busy_q  <= 1'b1;
          rx_lo_q    <= phy_rmii_txd_q;
          rx_phase_q <= 1'b1;
        end else begin
          rx_nib_q     <= {phy_rmii_txd_q, rx_lo_q};
          rx_nib_stb_q <= 1'b1;
          rx_phase_q   <= 1'b0;
        end
      end else begin
        rx_busy_q  <= 1'b0;
        rx_phase_q <= 1'b0;
      end
    end
  end

  logic       mii_rx_clk_q;
  logic [3:0] mii_rxd_q;
  logic       mii_rx_dv_q;
  logic       rx_pres_q;

  always_ff @(posedge refclk_i or posedge rst_i) begin
    if (rst_i) begin
      mii_rx_clk_q <= 1'b0;
      mii_rxd_q    <= '0;
      mii_rx_dv_q  <= 1'b0;
      rx_pres_q    <= 1'b0;
    end else begin
      if (phy_rmii_tx_en_q && !rx_busy_q) mii_rx_clk_q <= 1'b0;
      else                                mii_rx_clk_q <= ~mii_rx_clk_q;

      if (rx_nib_stb_q) begin
        mii_rxd_q   <= rx_nib_q;
        mii_rx_dv_q <= 1'b1;
        rx_pres_q   <= 1'b1;
      end else if (rx_pres_q) begin
        rx_pres_q   <= 1'b0;
      end else begin
        mii_rx_dv_q <= 1'b0;
      end
    end
  end

  assign mii_rxd_o    = mii_rxd_q;
  assign mii_rx_dv_o  = mii_rx_dv_q;
  assign mii_rx_er_o  = 1'b0;
  assign mii_rx_clk_o = mii_rx_clk_q;

  // ---- MII -> RMII (TX direction) -------------------------------------------
  logic       mii_tx_clk_q;
  logic [1:0] tx_hi_q;
  logic       tx_stream_q;
  logic [1:0] phy_rmii_rxd_q;
  logic       phy_rmii_crs_dv_q;

  always_ff @(posedge refclk_i or posedge rst_i) begin
    if (rst_i) begin
      mii_tx_clk_q      <= 1'b0;
      tx_hi_q           <= '0;
      tx_stream_q       <= 1'b0;
      phy_rmii_rxd_q    <= '0;
      phy_rmii_crs_dv_q <= 1'b0;
    end else begin
      mii_tx_clk_q <= ~mii_tx_clk_q;

      if (mii_tx_clk_q) begin
        if (mii_tx_en_i) begin
          // ================= NEG-CTL: DI-BIT ORDER SWAPPED =================
          // Real RTL sends the LOW di-bit first: rxd_q <= mii_txd_i[1:0];
          //                                      tx_hi_q <= mii_txd_i[3:2];
          // Here we send the HIGH di-bit first (WRONG on purpose) so the
          // conformance bench must catch it.
          phy_rmii_rxd_q    <= mii_txd_i[3:2];   // high di-bit first (WRONG)
          tx_hi_q           <= mii_txd_i[1:0];   // low di-bit deferred (WRONG)
          // ================================================================
          phy_rmii_crs_dv_q <= 1'b1;
          tx_stream_q       <= 1'b1;
        end else begin
          phy_rmii_rxd_q    <= '0;
          phy_rmii_crs_dv_q <= 1'b0;
          tx_stream_q       <= 1'b0;
        end
      end else if (tx_stream_q) begin
        phy_rmii_rxd_q <= tx_hi_q;
      end
    end
  end

  assign phy_rmii_rxd_o    = phy_rmii_rxd_q;
  assign phy_rmii_crs_dv_o = phy_rmii_crs_dv_q;
  assign mii_tx_clk_o      = mii_tx_clk_q;

endmodule
