// =============================================================================
// tests/shield_path/shield_path.sv  --  SIMULATION MODEL, not synthesizable RTL.
//
// Models the MPS3 shield / PMOD signal path between the FPGA (side A) and a
// LAN8720 breakout (side B): J28/J34 expose the SH0/SH1 shield channels through
// a 5 V translator bank and SN74TVC16222 bus switches.
//
// It exists to reproduce, in simulation, the fault diagnosed on the real board
// (nanoSoC-refactor/ethernet-subsystem-ahb/docs/mps3_ethernet_debug_plan.md,
// 2026-04-22 session):
//
//   "the SH0 level shifter channel(s) between PMOD J28 and the FPGA are passing
//    FPGA->PHY bits cleanly but are dead on the PHY->FPGA return"
//
// That asymmetry breaks every FPGA input -- RXD[1:0], CRS_DV, REF_CLK, and the
// MDIO read turn-around -- while every FPGA output survives. Observed
// consequences on the board: MDIO writes land (PHY LEDs respond), MDIO reads
// return 0xFFFF at all 32 PHY addresses, RX descriptors never fill.
//
// Direction control is a runtime input, not a parameter, so one simulation can
// walk healthy -> broken -> healthy and show a write landing while reads are
// dead.
//
// FLOAT_LEVEL models what a dead inbound channel presents to the FPGA pin: a
// pull-up, hence 1'b1 -- which is exactly why a dead MDIO read reads 0xFFFF.
// =============================================================================
`timescale 1ns / 1ps

module shield_path #(
  parameter logic FLOAT_LEVEL = 1'b1   // dead inbound channel floats high
) (
  // ---- direction enables (runtime) ----
  input  logic       pass_a2b,   // FPGA -> PHY  (outbound). Proven good on the board.
  input  logic       pass_b2a,   // PHY  -> FPGA (inbound).  Suspected dead on the board.

  // ---- side A: the FPGA ----
  input  logic       a_mdc,
  input  logic       a_mdio_o,
  input  logic       a_mdio_oe,
  output logic       a_mdio_i,
  output logic       a_rmii_crs_dv,
  output logic [1:0] a_rmii_rxd,
  output logic       a_rmii_ref_clk,

  // ---- side B: the LAN8720 ----
  output logic       b_mdc,
  output logic       b_mdio_o,
  output logic       b_mdio_oe,
  input  logic       b_mdio_i,
  input  logic       b_rmii_crs_dv,
  input  logic [1:0] b_rmii_rxd,
  input  logic       b_rmii_ref_clk
);

  // ---- outbound: FPGA -> PHY -------------------------------------------------
  // On the real board this direction works. With pass_a2b=0 the PHY sees an
  // idle, un-driven bus.
  assign b_mdc     = pass_a2b ? a_mdc     : FLOAT_LEVEL;
  assign b_mdio_o  = pass_a2b ? a_mdio_o  : FLOAT_LEVEL;
  assign b_mdio_oe = pass_a2b ? a_mdio_oe : 1'b0;

  // ---- inbound: PHY -> FPGA --------------------------------------------------
  // This is the failing direction. A dead channel presents the pull-up level to
  // every FPGA input pin, so:
  //   * the MDIO read turn-around + data phases sample all-ones -> 0xFFFF
  //   * CRS_DV is stuck asserted and RXD is stuck at 2'b11 -> no valid frame
  //   * REF_CLK never toggles -> the MAC's RX clock domain is dead
  assign a_mdio_i       = pass_b2a ? b_mdio_i       : FLOAT_LEVEL;
  assign a_rmii_crs_dv  = pass_b2a ? b_rmii_crs_dv  : FLOAT_LEVEL;
  assign a_rmii_rxd     = pass_b2a ? b_rmii_rxd     : {2{FLOAT_LEVEL}};
  assign a_rmii_ref_clk = pass_b2a ? b_rmii_ref_clk : FLOAT_LEVEL;

endmodule
