// -----------------------------------------------------------------------------
// rmii_phy_if.sv — RMII (DUT-facing) <-> MII (shell-facing) datapath.
//
// Real conversion RTL (W-RTL-ETH, A1/A5). This block is the datapath half of
// the shell's "virtual PHY" (spec §8.1): the shell plays the PHY role toward
// the DUT MAC, so the RP-facing RMII group carries the *PHY-side* directions
// (shell drives crs_dv/rxd toward the DUT, samples txd/tx_en from it) and the
// shell-internal MII group faces link_partner_mac (the MAC half of the MII
// pairing).
//
// Ports named/grouped per docs/contracts/partition-pins.md ("Ethernet — RMII
// + MDIO", shell's view) on the RP-facing side. Port list is UNCHANGED from
// the Phase-0 skeleton — only the bodies are new.
//
// ---------------------------------------------------------------------------
// CLOCKING (spec §8.3 + fpga/ethernet/README.md "Clocking / gotchas")
//
//   * `refclk_i` MUST BE 50 MHz. The shell clocking plan (A6/W-BD) sources a
//     board-derived fixed 50 MHz onto this input (clk_wiz output in the BD);
//     this module deliberately does NOT contain an MMCM/divider for the RMII
//     reference — a fabric-generated divided clock would need its own BUFG
//     and cross-clock constraints for zero benefit, since the BD clock wizard
//     already produces exact 50 MHz. `phy_rmii_ref_clk_o` is that same net,
//     forwarded across the partition boundary (shell sources the reference,
//     DUT MAC must be configured ref-IN — never let both sides source it).
//   * ALL internal logic is clocked on `refclk_i` (one domain). The MII
//     "clocks" this module outputs (`mii_rx_clk_o`, `mii_tx_clk_o`) are
//     divide-by-2 (25 MHz) pacing clocks generated as ordinary flops in the
//     refclk domain: MII data/dv outputs change only on their low phase and
//     are stable across every rising edge, and MII TX inputs are sampled at
//     the refclk edge in the middle of each mii_tx_clk period (half a period
//     after the MAC-side launch edge). Consumers may treat them as real MII
//     clocks OR stay in the refclk domain and use them as enables —
//     link_partner_mac (same 50 MHz shell domain) does the latter, so no CDC
//     exists inside this pairing. The partition-boundary CDC story is
//     therefore: the DUT MAC's TX outputs are launched by the DUT's own RMII
//     logic but are *timed by this same shell-sourced ref clock* (RMII is a
//     synchronous-to-REF_CLK interface by construction); the HDPR-29
//     re-register stage below is the single static-side sampling stage. If a
//     future clocking plan runs the DUT MAC's RMII logic on an unrelated
//     clock, add a real 2-FF/FIFO CDC ahead of that stage (flagged for A6;
//     not needed while REF_CLK is the shared timing reference per the RMII
//     spec).
//
// ---------------------------------------------------------------------------
// WHAT THE v1 MODEL DOES vs DOES NOT DO (be precise — bench + docs rely on it)
//
// Modelled (both directions):
//   * 2-bit@50MHz RMII dibits <-> 4-bit@25MHz MII nibbles, LSB-first within
//     each byte (dibit0 = bits[1:0]; nibble0 = bits[3:0]) per IEEE 802.3
//     "lowest-order bits first" / Annex 22B dibit ordering.
//   * Dibit/nibble phase alignment locks to the assertion edge of the
//     carrier signal (tx_en / mii_tx_en) — per RMII spec the MAC asserts
//     TX_EN aligned with the first dibit of preamble, so locking phase to
//     the assertion edge IS the preamble-alignment rule.
//   * CRS_DV / RX_DV held continuously for the exact frame duration.
//
// Simplified / NOT modelled in v1 (documented deviations from the RMII spec):
//   * CRS_DV toggling: real RMII multiplexes carrier-sense and data-valid on
//     CRS_DV (it toggles at 25 MHz on nibble boundaries when carrier ends
//     before the RX FIFO drains). v1 drives CRS_DV as a plain frame-envelope
//     valid — continuously high from first to last dibit, no CRS/DV
//     de-multiplex phase. The DUT MAC sees "carrier and data end together",
//     which is the common case on an idle link with no elasticity buffering.
//   * No preamble insertion/stripping: this is a *converter*, not a MAC —
//     preamble/SFD pass through as ordinary data in both directions (the MAC
//     on each side owns preamble), exactly like a real RMII PHY's
//     transparent datapath.
//   * 100 Mb/s full-duplex only: no 10 Mb/s dibit-repeat-10x mode, no
//     half-duplex CRS assertion during TX, no collision (COL) modelling.
//   * No RX_ER generation: `mii_rx_er_o` is tied low (no symbol-error or
//     false-carrier conditions exist in a fabric-only virtual PHY; the
//     independent frame scoring lives in gen_checker per spec §8.4).
//   * Dribble truncation: a frame whose tx_en span is not a whole number of
//     nibbles (odd dibit count) has its trailing half-nibble dropped — MII
//     cannot represent half a nibble. (gen_checker's dribble *injection* is
//     byte-granular and unaffected by this.)
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rmii_phy_if (
  // ---------------------------------------------------------------------
  // Reference clock: MUST already be 50 MHz (see CLOCKING note above).
  // ---------------------------------------------------------------------
  input  logic       refclk_i,
  input  logic       rst_i,

  // ---------------------------------------------------------------------
  // RP-facing RMII — partition-pins.md "Ethernet" group, shell's view.
  // ---------------------------------------------------------------------
  output logic       phy_rmii_ref_clk_o,   // shell sources 50 MHz ref (spec §8.3)
  output logic       phy_rmii_crs_dv_o,
  output logic [1:0] phy_rmii_rxd_o,
  input  logic [1:0] phy_rmii_txd_i,       // sampled from DUT MAC — re-registered below
  input  logic       phy_rmii_tx_en_i,     // sampled from DUT MAC — re-registered below

  // ---------------------------------------------------------------------
  // Shell-internal MII — toward fpga/ethernet/link_partner_mac/.
  // ---------------------------------------------------------------------
  output logic [3:0] mii_rxd_o,
  output logic       mii_rx_dv_o,
  output logic       mii_rx_er_o,
  output logic       mii_rx_clk_o,
  input  logic [3:0] mii_txd_i,
  input  logic       mii_tx_en_i,
  output logic       mii_tx_clk_o
);

  // ===========================================================================
  // 50 MHz REF_CLK sourcing: refclk_i is required to already be 50 MHz (BD
  // clock-wizard output — see CLOCKING header note); forward it as the
  // partition-pin reference. No divider/MMCM here by design.
  // ===========================================================================
  assign phy_rmii_ref_clk_o = refclk_i;

  // ===========================================================================
  // HDPR-29 IOB packing note (see README): phy_rmii_txd_i / phy_rmii_tx_en_i
  // are inputs to the shell, driven by the RP across the partition boundary.
  // The FIRST register stage that samples them lives here, static-side —
  // never folded into RP logic, never left combinational. Clocked on
  // refclk_i, which is the SAME net as phy_rmii_ref_clk_o (kept on the input
  // clock so the synthesis clock tree roots at the input buffer, and so
  // simulation event ordering never races the ref-clk fan-out). Mark
  // `(* IOB = "TRUE" *)` if/when a physical pad follows it (v1+, not this
  // MPS3 build).
  // ===========================================================================
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

  // ===========================================================================
  // RMII -> MII (RX direction: DUT MAC TX -> our MII RX toward
  // link_partner_mac). Dibit-pair collection, phase-locked to the tx_en
  // assertion edge (= preamble alignment, see header). Nibble = {dibit1,
  // dibit0} — LSB-first within the byte, matching MII nibble order.
  // ===========================================================================
  logic       rx_busy_q;     // inside a frame (tx_en span)
  logic       rx_phase_q;    // 0 = next dibit is the low half, 1 = high half
  logic [1:0] rx_lo_q;       // saved low dibit
  logic [3:0] rx_nib_q;      // completed nibble
  logic       rx_nib_stb_q;  // 1-cycle strobe: rx_nib_q is fresh

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
          // frame start, or an even dibit slot: capture the low dibit
          rx_busy_q  <= 1'b1;
          rx_lo_q    <= phy_rmii_txd_q;
          rx_phase_q <= 1'b1;
        end else begin
          // odd dibit slot: complete the nibble
          rx_nib_q     <= {phy_rmii_txd_q, rx_lo_q};
          rx_nib_stb_q <= 1'b1;
          rx_phase_q   <= 1'b0;
        end
      end else begin
        // tx_en low: end of frame. If rx_phase_q was 1, a trailing half
        // nibble is dropped (dribble truncation — see header).
        rx_busy_q  <= 1'b0;
        rx_phase_q <= 1'b0;
      end
    end
  end

  // MII RX presentation: each completed nibble is presented for one full
  // 25 MHz period (two refclk cycles), updated only on mii_rx_clk's LOW
  // phase so data/dv are stable across every mii_rx_clk rising edge.
  // mii_rx_clk_o free-runs at 25 MHz and RE-ALIGNS its phase at each frame
  // start (legal for a PHY: RX_CLK phase is PHY-chosen; realignment happens
  // only between frames, and forcing the low phase can only stretch a low
  // period — never a runt pulse).
  logic       mii_rx_clk_q;
  logic [3:0] mii_rxd_q;
  logic       mii_rx_dv_q;
  logic       rx_pres_q;     // mid-nibble hold flag (2nd cycle of a nibble)

  always_ff @(posedge refclk_i or posedge rst_i) begin
    if (rst_i) begin
      mii_rx_clk_q <= 1'b0;
      mii_rxd_q    <= '0;
      mii_rx_dv_q  <= 1'b0;
      rx_pres_q    <= 1'b0;
    end else begin
      // phase realign at frame start; otherwise free-run divide-by-2
      if (phy_rmii_tx_en_q && !rx_busy_q) mii_rx_clk_q <= 1'b0;
      else                                mii_rx_clk_q <= ~mii_rx_clk_q;

      if (rx_nib_stb_q) begin
        mii_rxd_q   <= rx_nib_q;
        mii_rx_dv_q <= 1'b1;
        rx_pres_q   <= 1'b1;
      end else if (rx_pres_q) begin
        rx_pres_q   <= 1'b0;        // 2nd cycle of the current nibble
      end else begin
        mii_rx_dv_q <= 1'b0;        // no new nibble at a boundary: frame over
      end
    end
  end

  assign mii_rxd_o    = mii_rxd_q;
  assign mii_rx_dv_o  = mii_rx_dv_q;
  assign mii_rx_er_o  = 1'b0;       // no RX_ER conditions modelled (see header)
  assign mii_rx_clk_o = mii_rx_clk_q;

  // ===========================================================================
  // MII -> RMII (TX direction: link_partner_mac TX -> DUT MAC RX).
  // mii_tx_clk_o free-runs at 25 MHz; the MAC side launches a nibble on each
  // rising edge, and this module samples mii_txd_i/mii_tx_en_i at the refclk
  // edge in the MIDDLE of that period (mii_tx_clk high phase) — half a
  // 25 MHz period of setup margin, no same-edge race. Each nibble is then
  // serialized low-dibit-first onto phy_rmii_rxd_o at 50 MHz with CRS_DV as
  // the frame envelope.
  // ===========================================================================
  logic       mii_tx_clk_q;
  logic [1:0] tx_hi_q;        // saved high dibit of the current nibble
  logic       tx_stream_q;    // nibble in flight (low presented, high next)
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
        // mid-period sample edge: capture one MII nibble (or end of frame)
        if (mii_tx_en_i) begin
          phy_rmii_rxd_q    <= mii_txd_i[1:0];  // low dibit first (LSB-first)
          tx_hi_q           <= mii_txd_i[3:2];
          phy_rmii_crs_dv_q <= 1'b1;
          tx_stream_q       <= 1'b1;
        end else begin
          phy_rmii_rxd_q    <= '0;
          phy_rmii_crs_dv_q <= 1'b0;
          tx_stream_q       <= 1'b0;
        end
      end else if (tx_stream_q) begin
        // in-between edge: present the high dibit of the nibble in flight
        phy_rmii_rxd_q <= tx_hi_q;
      end
    end
  end

  assign phy_rmii_rxd_o    = phy_rmii_rxd_q;
  assign phy_rmii_crs_dv_o = phy_rmii_crs_dv_q;
  assign mii_tx_clk_o      = mii_tx_clk_q;

endmodule
