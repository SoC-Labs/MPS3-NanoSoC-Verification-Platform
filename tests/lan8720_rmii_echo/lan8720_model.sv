// -----------------------------------------------------------------------------
// lan8720_model.sv -- behavioral model of a Microchip/Waveshare LAN8720(A)
// RMII PHY, REFCLK-in mode (CLOCKING DECISION Option A: the FPGA/MAC side
// SOURCES the 50 MHz REF_CLK; the PHY consumes it and times all its RMII I/O to
// it). Built for tests/lan8720_rmii_echo -- a REAL PHY model, deliberately NOT
// fpga/ethernet/mdio_phy_model (whose MDIO framing has a documented history of
// an off-by-one turnaround; this model uses the PROVEN-interoperable framing).
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
// license.  Copyright (C) 2026, SoC Labs (www.soclabs.org)
//
// WHAT IT MODELS
//   * RMII toward the MAC: crs_dv/rxd OUT to the MAC, txd/tx_en IN from the MAC,
//     all in the ref_clk domain.
//   * Clause-22 MDIO slave with a real LAN8720 register file (PHYID1=0x0007,
//     PHYID2=0xC0F1, BMSR link-up, ANAR) and the *correct* read turnaround
//     (DATA[15] presented at the FIRST post-header bit-time -- the framing the
//     real OpenCores eth_miim master reads correctly, per
//     fpga/ethernet/mdio_phy_model/mdio_slave.sv:194-223 and
//     tests/lan8720_sut/RESULT.txt). BMCR.14 (loopback) is live: setting it (or
//     the loopback_en pin) puts the PHY in near-end RMII loopback.
//   * Near-end loopback: MAC TX -> PHY -> MAC RX (a real LAN8720 feature). This
//     is how the bench gets a full "frame transmitted, then received" round trip
//     through BOTH rmii_to_mii directions without any external link partner.
//   * External media RX: a di-bit inject port the bench drives (a frame arriving
//     from the wire). NATURAL {rxd1,rxd0} order -- NO software transpose here.
//     The rxd0<->rxd1 board-shield swap lives ONLY in the shell XDC pin map
//     (fpga/shell/constraints_realphy/mps3_realphy_pins.xdc); sim has no shield,
//     so this model must NOT re-apply it (a second swap re-creates the board's
//     0xA-preamble / no-lock failure).
//
// WHAT IT DOES NOT MODEL: the 100BASE-TX analog PMD, auto-negotiation state
// machine dynamics, or MDINT. Link is modelled as permanently up.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module lan8720_model #(
  parameter logic [4:0]  PHY_ADDR = 5'd1,     // strap-selected MDIO address
  parameter logic [15:0] PHYID1   = 16'h0007, // LAN8720A OUI high
  parameter logic [15:0] PHYID2   = 16'hC0F1, // LAN8720A OUI low / model / rev
  parameter logic [15:0] BMSR_VAL = 16'h782D, // 100/10 FD/HD, ANEG complete, link up
  parameter logic [15:0] ANAR_VAL = 16'h01E1  // 100/10 FD/HD advertised
) (
  input  wire        ref_clk,      // 50 MHz RMII reference (FPGA-sourced)
  input  wire        resetn,       // active-low

  // RMII toward the MAC (this module is the PHY)
  input  wire  [1:0] mac_txd,      // MAC -> PHY transmit di-bits
  input  wire        mac_tx_en,    // MAC -> PHY transmit enable
  output wire  [1:0] mac_rxd,      // PHY -> MAC receive di-bits
  output wire        mac_crs_dv,   // PHY -> MAC carrier / data-valid

  // Clause-22 MDIO (the MAC is the management master)
  input  wire        mdc,          // MAC MDIO clock
  input  wire        mdio_o,       // MAC MDIO drive value
  input  wire        mdio_oe,      // MAC MDIO output-enable (1 = MAC driving)
  output reg         mdio_i,       // PHY reply, sampled by the MAC

  // Bench controls
  input  wire        loopback_en,  // 1 = near-end RMII loopback (OR BMCR.14)
  input  wire        rx_inj_en,    // 1 = drive external media di-bits onto RX
  input  wire  [1:0] rx_inj_dibit, // di-bit = {rxd1,rxd0} -- NATURAL order
  input  wire        rx_inj_crs    // carrier/data-valid for the injected stream
);

  // ===========================================================================
  // Register file (only the registers a bring-up/echo bench touches)
  // ===========================================================================
  logic [15:0] bmcr;   // reg 0 -- control (bit14 = loopback, bit15 = soft reset)
  wire         phy_loopback = loopback_en | bmcr[14];

  // ===========================================================================
  // Near-end loopback datapath: register MAC TX one ref_clk to model PHY
  // latency, then present it back on RX. Combinational passthrough for the
  // external-inject path so a bench di-bit driven on negedge ref_clk reaches
  // the MAC pins exactly as tests/lan8720_sut/tb_lan8720_dp.sv's proven
  // rmii_rx_send does (change on negedge, MAC samples on posedge).
  // ===========================================================================
  logic [1:0] rxd_loop_q;
  logic       crsdv_loop_q;
  always @(posedge ref_clk or negedge resetn) begin
    if (!resetn) begin
      rxd_loop_q   <= 2'b00;
      crsdv_loop_q <= 1'b0;
    end else begin
      rxd_loop_q   <= mac_txd;
      crsdv_loop_q <= mac_tx_en;
    end
  end

  assign mac_rxd    = phy_loopback ? rxd_loop_q   : (rx_inj_en ? rx_inj_dibit : 2'b00);
  assign mac_crs_dv = phy_loopback ? crsdv_loop_q : (rx_inj_en ? rx_inj_crs   : 1'b0);

  // ===========================================================================
  // MDIO Clause-22 slave. Frame after preamble: ST(2) OP(2) PHYAD(5) REGAD(5) =
  // 14-bit header, then TA(2)+DATA(16). Sample master bits on posedge mdc;
  // drive read data on negedge mdc. Read turnaround: present DATA[15] at the
  // FIRST payload bit-time (bit_cnt==1) -- the single-turnaround framing the
  // real eth_miim reads correctly (mdio_slave.sv:194-223).
  // ===========================================================================
  localparam logic [4:0] HEADER_BITS  = 5'd14;
  localparam logic [4:0] PAYLOAD_BITS = 5'd18;

  typedef enum logic [1:0] { ST_PRE = 2'd0, ST_HDR = 2'd1, ST_PAY = 2'd2 } mdio_state_e;
  mdio_state_e state_q;
  logic [4:0]  bit_cnt_q;
  logic [12:0] hdr_shreg_q;
  logic [14:0] dat_shreg_q;
  logic        is_read_q, is_write_q, phy_match_q;
  logic [4:0]  reg_addr_q;
  logic [15:0] rd_data_q;   // register selected for the current read

  wire [13:0] hdr_shreg_d = {hdr_shreg_q, mdio_o};
  wire [1:0]  hdr_st    = hdr_shreg_d[13:12];
  wire [1:0]  hdr_op    = hdr_shreg_d[11:10];
  wire [4:0]  hdr_phyad = hdr_shreg_d[9:5];
  wire [4:0]  hdr_regad = hdr_shreg_d[4:0];
  wire [15:0] dat_shreg_d = {dat_shreg_q, mdio_o};

  // register read mux (LAN8720 defaults; unlisted regs read 0)
  function automatic logic [15:0] reg_read(input logic [4:0] a);
    case (a)
      5'd0:    reg_read = bmcr;
      5'd1:    reg_read = BMSR_VAL;
      5'd2:    reg_read = PHYID1;
      5'd3:    reg_read = PHYID2;
      5'd4:    reg_read = ANAR_VAL;
      default: reg_read = 16'h0000;
    endcase
  endfunction

  always_ff @(posedge mdc or negedge resetn) begin
    if (!resetn) begin
      state_q     <= ST_PRE;
      bit_cnt_q   <= '0;
      hdr_shreg_q <= '0;
      dat_shreg_q <= '0;
      is_read_q   <= 1'b0;
      is_write_q  <= 1'b0;
      phy_match_q <= 1'b0;
      reg_addr_q  <= '0;
      rd_data_q   <= '0;
      bmcr        <= 16'h3100;   // LAN8720 BMCR reset: ANEG en, 100M, FD
    end else begin
      case (state_q)
        ST_PRE: begin
          if (mdio_oe && !mdio_o) begin  // first ST '0' after preamble
            hdr_shreg_q <= '0;
            bit_cnt_q   <= 5'd1;
            state_q     <= ST_HDR;
          end
        end
        ST_HDR: begin
          if (!mdio_oe) begin
            state_q   <= ST_PRE;
            bit_cnt_q <= '0;
          end else begin
            hdr_shreg_q <= hdr_shreg_d[12:0];
            if (bit_cnt_q == HEADER_BITS - 5'd1) begin
              is_read_q   <= (hdr_st == 2'b01) && (hdr_op == 2'b10);
              is_write_q  <= (hdr_st == 2'b01) && (hdr_op == 2'b01);
              phy_match_q <= (hdr_phyad == PHY_ADDR);
              reg_addr_q  <= hdr_regad;
              rd_data_q   <= reg_read(hdr_regad);
              bit_cnt_q   <= '0;
              state_q     <= ST_PAY;
            end else begin
              bit_cnt_q <= bit_cnt_q + 5'd1;
            end
          end
        end
        ST_PAY: begin
          if (is_write_q && (bit_cnt_q >= 5'd2))
            dat_shreg_q <= dat_shreg_d[14:0];
          if (bit_cnt_q == PAYLOAD_BITS - 5'd1) begin
            if (is_write_q && phy_match_q && (reg_addr_q == 5'd0))
              bmcr <= dat_shreg_d;   // let firmware toggle loopback etc.
            bit_cnt_q <= '0;
            state_q   <= ST_PRE;
          end else begin
            bit_cnt_q <= bit_cnt_q + 5'd1;
          end
        end
        default: begin state_q <= ST_PRE; bit_cnt_q <= '0; end
      endcase
    end
  end

  // Drive read data on negedge mdc. bit_cnt==1 -> DATA[15] (single turnaround).
  always_ff @(negedge mdc or negedge resetn) begin
    if (!resetn) begin
      mdio_i <= 1'b1;
    end else if (state_q == ST_PAY && is_read_q && phy_match_q) begin
      case (bit_cnt_q)
        5'd0:    mdio_i <= 1'b1;                                   // turnaround
        5'd17:   mdio_i <= 1'b1;                                   // past DATA[0]
        default: mdio_i <= rd_data_q[4'(5'd16 - bit_cnt_q)];       // DATA[15:0]
      endcase
    end else begin
      mdio_i <= 1'b1;   // idle high (dedicated point-to-point reply wire)
    end
  end

endmodule
