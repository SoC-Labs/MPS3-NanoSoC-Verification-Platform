// -----------------------------------------------------------------------------
// rp_dbg_hub.sv -- the RM side of ILA-over-XVC: the debug hub every RM that
// carries ILAs must hold. Handover: docs/planning/HANDOVER_RM_ILA_OVER_XVC.md
// §1, §4.6.
//
// It wraps ONE debug_bridge in mode 1 (From_BSCAN_to_DebugHub, C_DESIGN_TYPE 1)
// created by fpga/rp/common/dbg_ip.tcl under the FIXED module name
// `rp_dbg_bridge`. The bridge's BSCAN slave port is fed from the 12
// `dbg_bscan_*` partition pins (the static debug_bridge_0's m0_bscan master,
// mode 2, TCK 80 ns from a STATIC BUFGCE) and holds the RM's own debug hub
// (xsdbm). Every ILA in the same RM is auto-connected to that hub at
// opt_design (F4) -- no connect_debug_cores.
//
// clk = phy_rmii_ref_clk (50 MHz): the only always-on shell clock in the RP
// (F12). NOT dut_clk -- the DRP can stop or retune it. The ILAs themselves are
// clocked by whatever they sample (dut_clk).
//
// RULES (trap 1/2): no clock buffer and no BSCANE2 in the RP; an ILA with no
// hub here makes Vivado look for a BSCANE2 (HDPR-16). One hub per RM.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_dbg_hub (
  input  logic clk,                   // = phy_rmii_ref_clk, 50 MHz, always on
  input  logic dbg_bscan_bscanid_en,
  input  logic dbg_bscan_capture,
  input  logic dbg_bscan_drck,
  input  logic dbg_bscan_reset,
  input  logic dbg_bscan_runtest,
  input  logic dbg_bscan_sel,
  input  logic dbg_bscan_shift,
  input  logic dbg_bscan_tck,
  input  logic dbg_bscan_tdi,
  input  logic dbg_bscan_tms,
  input  logic dbg_bscan_update,
  output logic dbg_bscan_tdo
);

  rp_dbg_bridge u_bridge (
    .clk                (clk),
    .S_BSCAN_bscanid_en (dbg_bscan_bscanid_en),
    .S_BSCAN_capture    (dbg_bscan_capture),
    .S_BSCAN_drck       (dbg_bscan_drck),
    .S_BSCAN_reset      (dbg_bscan_reset),
    .S_BSCAN_runtest    (dbg_bscan_runtest),
    .S_BSCAN_sel        (dbg_bscan_sel),
    .S_BSCAN_shift      (dbg_bscan_shift),
    .S_BSCAN_tck        (dbg_bscan_tck),
    .S_BSCAN_tdi        (dbg_bscan_tdi),
    .S_BSCAN_tdo        (dbg_bscan_tdo),
    .S_BSCAN_tms        (dbg_bscan_tms),
    .S_BSCAN_update     (dbg_bscan_update)
  );

endmodule
