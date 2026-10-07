// -----------------------------------------------------------------------------
// rp_dut_stub.sv — synthesizable STUB of the RP-DUT cell for the monolithic
// harness bitstream (build_shell.tcl, the "harness-first / ping-first" path).
//
// Same module name and EXACT port list as fpga/dfx/proof/rp_dut.sv (the
// port-only black box) — partition-pins.md's RP side — so shell_top.sv's
// `rp_dut u_rp_dut(...)` binds to this instead. Every output is tied to a safe
// inert value so opt_design/place/route completes: a plain black box trips
// DRC INBB-3 in a NON-DFX flow, which is what this build is.
//
// This is deliberately NOT used by the DFX flow (fpga/dfx/), which needs
// u_rp_dut to remain an empty, HD.RECONFIGURABLE black box that RM checkpoints
// (rm_greybox/rm_led/rm_nanosoc) link into. build_shell.tcl adds THIS file and
// NOT proof/rp_dut.sv (adding both would double-define module rp_dut).
//
// The inert DUT does not participate in the harness's job for H2 (MicroBlaze +
// lwIP + LAN9220 all live in the shell): the board still boots the shell,
// brings up Ethernet, and answers ping with the RP region doing nothing.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_dut (
// BEGIN GENERATED[boundary-ports] — gen_boundary.py — DO NOT EDIT BY HAND
  input  logic        dut_clk,
  input  logic        dut_resetn,
  input  logic        rp_resetn,
  input  logic        dbg_resetn,

  input  logic        jtag_tck,
  input  logic        jtag_tms,
  input  logic        jtag_tdi,
  output logic        jtag_tdo,

  // RM debug — BSCAN to an RM debug hub (partition-pins.md, 2026-10 ILA
  // mint). Inert stub: no hub, the legs are ignored and TDO reads 0.
  input  logic        dbg_bscan_bscanid_en,
  input  logic        dbg_bscan_capture,
  input  logic        dbg_bscan_drck,
  input  logic        dbg_bscan_reset,
  input  logic        dbg_bscan_runtest,
  input  logic        dbg_bscan_sel,
  input  logic        dbg_bscan_shift,
  input  logic        dbg_bscan_tck,
  input  logic        dbg_bscan_tdi,
  input  logic        dbg_bscan_tms,
  input  logic        dbg_bscan_update,
  output logic        dbg_bscan_tdo,

  input  logic        phy_rmii_ref_clk,
  input  logic        phy_rmii_crs_dv,
  input  logic [1:0]  phy_rmii_rxd,
  output logic [1:0]  phy_rmii_txd,
  output logic        phy_rmii_tx_en,
  output logic        mdc,
  output logic        mdio_o,
  output logic        mdio_oe,
  input  logic        mdio_i,

  output logic [7:0]  uart_tx_tdata,
  output logic        uart_tx_tvalid,
  input  logic        uart_tx_tready,
  input  logic [7:0]  uart_rx_tdata,
  input  logic        uart_rx_tvalid,
  output logic        uart_rx_tready,
  output logic        swo,

  output logic [31:0] rm_id,
  output logic        dut_lockup,
  output logic        irq_out,

  output logic [15:0] dut_gpio_o,
  output logic [15:0] dut_gpio_oe,
  input  logic [15:0] dut_gpio_i,

  // Flash / QSPI XiP boundary (partition-pins.md v0.2). Inert stub: flash
  // driven to safe idle (deselected), io_i accepted.
  output logic        qspi_sclk,
  output logic        qspi_csn,
  output logic [3:0]  qspi_io_o,
  output logic [3:0]  qspi_io_oe,
  input  logic [3:0]  qspi_io_i
// END GENERATED[boundary-ports]
);

// BEGIN GENERATED[boundary-tieoffs] — gen_boundary.py — DO NOT EDIT BY HAND
  // --- inert tie-offs (all outputs driven to safe defaults) ------------------
  assign jtag_tdo      = 1'b0;

  assign dbg_bscan_tdo = 1'b0;

  assign phy_rmii_txd   = 2'b00;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc            = 1'b0;
  assign mdio_o         = 1'b0;
  assign mdio_oe        = 1'b0;         // MDIO tri-stated (input mode)

  assign uart_tx_tdata  = 8'h00;
  assign uart_tx_tvalid = 1'b0;
  assign uart_rx_tready = 1'b0;
  assign swo            = 1'b0;

  // rm_id readback = ASCII "STUB" (0x53_54_55_42): the coordinator/host reads
  // this off the rm_id pin (overlay-manifest.md rm_id verify) and can tell the
  // harness is carrying the inert stub RP, not a real RM checkpoint.
  assign rm_id         = 32'h53_54_55_42;
  assign dut_lockup    = 1'b0;
  assign irq_out       = 1'b0;

  assign dut_gpio_o    = 16'h0000;
  assign dut_gpio_oe   = 16'h0000;      // all DUT GPIO as inputs (high-Z)

  // QSPI flash pads: safe idle (deselected, clock low, lanes tri-stated).
  assign qspi_sclk     = 1'b0;
  assign qspi_csn      = 1'b1;          // active-low CS deasserted
  assign qspi_io_o     = 4'b0000;
  assign qspi_io_oe    = 4'b0000;
// END GENERATED[boundary-tieoffs]

endmodule
