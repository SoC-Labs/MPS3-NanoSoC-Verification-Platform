// -----------------------------------------------------------------------------
// lan9220_emc_wrap.sv — thin pin-level wrapper exposing the LAN9220's
// static-memory-bus (SMC) pins to the harness top wrapper.
//
// Layer: sits BETWEEN the Xilinx AXI EMC vendor cell (axi_emc_0, configured
// per fpga/ethernet/lan9220_if/README.md "AXI EMC parameters") and the true
// top-level MPS3 board pins constrained in fpga/shell/constraints/mps3_harness.xdc.
// It is purely combinational glue + the one tristate mux SMBF_DATA needs; no
// register-level LAN9220 protocol lives here (that is axi_emc_0's bank timing
// + firmware/smsc911x/, see the README's "AXI-Stream <-> FIFO glue vs the
// pin-level wrapper" section — this file is the OTHER layer from lan9220_if.sv).
//
// BD-facing port names below match fpga/shell/bd/shell_bd.tcl Section 4
// 1:1 (eth_ncs, eth_noe, eth_int, smbf_data_o/i/t, smbf_addr, smbf_noe,
// smbf_nwe) plus the two ports flagged as missing there in this module's
// README (smbf_nrst, smbf_fifosel) — add those to shell_bd.tcl to close the
// loop; this module assumes their eventual presence so it does not need a
// second pass once they land.
//
// Resolves I21 (docs/contracts/OPEN_ISSUES.md): SMBF_DATA tristate handling,
// split o/i/t at the BD boundary (matching how swd_dio/mdio are already
// split in this repo's board-wrapper convention), true `inout` only at this
// module's true top-level pin — mirrors eth_ss_design_wrapper.v's mdio/
// swd_dio tristate pattern (nanosoc-multicore-system /
// ethernet-subsystem-ahb/fpga/targets/arm_mps3/eth_ss_design_wrapper.v).
//
// Open item (see README "Open item: ETH_nOE vs SMBF_nOE"): eth_ncs/eth_noe
// (LAN9220-specific) and smbf_noe/smbf_nwe (shared-bus) are NOT fused here —
// each drives its own physical board pin independently, exactly as the
// legacy pinmap keeps them as two distinct pads. Confirm against the MPS3
// board schematic before assuming any fixed phase relationship between them.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module lan9220_emc_wrap (
  // ---------------------------------------------------------------------
  // BD-facing side — toward axi_emc_0 (via shell_bd.tcl's Section 4 ports).
  // Directions below are from THIS module's perspective, i.e. the opposite
  // of shell_bd.tcl's make_bd_port direction (which is stated from the BD's
  // own point of view, looking out toward the board).
  // ---------------------------------------------------------------------
  input  logic        eth_ncs,        // shell_bd.tcl: eth_ncs, dir O (BD->board)
  input  logic        eth_noe,        // shell_bd.tcl: eth_noe, dir O (BD->board)
  output logic        eth_int,        // shell_bd.tcl: eth_int, dir I (board->BD)

  input  logic [15:0] smbf_data_o,    // shell_bd.tcl: smbf_data_o, dir O
  output logic [15:0] smbf_data_i,    // shell_bd.tcl: smbf_data_i, dir I
  input  logic        smbf_data_t,    // shell_bd.tcl: smbf_data_t, dir O (1 = Hi-Z, Vivado tristate convention)

  input  logic [6:0]  smbf_addr,      // shell_bd.tcl: smbf_addr, dir O
  input  logic         smbf_noe,      // shell_bd.tcl: smbf_noe, dir O
  input  logic         smbf_nwe,      // shell_bd.tcl: smbf_nwe, dir O
  input  logic         smbf_nrst,     // NOT YET in shell_bd.tcl -- see README "missing" flag
  input  logic         smbf_fifosel,  // NOT YET in shell_bd.tcl -- see README "missing" flag

  // ---------------------------------------------------------------------
  // Board-facing side — true top-level pins, constrained in
  // fpga/shell/constraints/mps3_harness.xdc. Package pins / IOSTANDARD
  // are ALL LVCMOS18, per fpga/monolithic/nanosoc_mps3.xdc and the legacy
  // fpga_pinmap.xdc (see this directory's README pin table). Do not add
  // pin constraints here — this is pure RTL, XDC lives in shell/constraints/.
  // ---------------------------------------------------------------------
  output logic        ETH_nCS,
  output logic        ETH_nOE,
  input  logic        ETH_INT,

  output logic [6:0]  SMBF_ADDR,
  inout  wire  [15:0] SMBF_DATA,      // true bidirectional board pad
  output logic        SMBF_FIFOSEL,
  output logic        SMBF_nOE,
  output logic        SMBF_nWE,
  output logic        SMBF_nRST
);

  // ---------------------------------------------------------------------
  // Device-specific + shared-bus control/strobe pins: straight combinational
  // pass-through. No clock in this module -- axi_emc_0's bank-timing config
  // (README "AXI EMC parameters") is what actually times these relative to
  // its own internal clock; this wrapper only routes pins.
  // ---------------------------------------------------------------------
  assign ETH_nCS      = eth_ncs;
  assign ETH_nOE      = eth_noe;
  assign eth_int      = ETH_INT;

  assign SMBF_ADDR    = smbf_addr;
  assign SMBF_nOE     = smbf_noe;
  assign SMBF_nWE     = smbf_nwe;
  assign SMBF_nRST    = smbf_nrst;
  assign SMBF_FIFOSEL = smbf_fifosel;

  // ---------------------------------------------------------------------
  // SMBF_DATA tristate mux (I21 resolution) -- Vivado/this-repo convention:
  // *_t = 1 means Hi-Z (matches eth_ss_design_wrapper.v's mdio_t/swd_dio_t
  // and shell_bd.tcl's own smbf_data_t naming).
  // ---------------------------------------------------------------------
  assign SMBF_DATA   = smbf_data_t ? 16'bz : smbf_data_o;
  assign smbf_data_i = SMBF_DATA;

endmodule
