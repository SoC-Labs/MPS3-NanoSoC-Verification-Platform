// -----------------------------------------------------------------------------
// rp_template_wrapper.sv -- THE COPYABLE RM SKELETON.
//
// This file is not built by anything. It is the starting point for a NEW
// Reconfigurable Module (RM): copy `fpga/rp/_template/` to `fpga/rp/<name>/`,
// rename this file and the module inside it to `rp_<name>_wrapper`, register the
// RM in `fpga/dfx/rm_list.tcl`, and fill in the DUT instance where marked.
// The step-by-step is docs/site/docs/guides/adding-an-rm.md; the rules the
// finished wrapper must obey are fpga/dfx/rms/README.md "RM authoring contract".
//
// WHAT IS LOAD-BEARING HERE, AND WHY
// ----------------------------------
// The port list below is the RP side of the frozen partition boundary --
// docs/contracts/partition-pins.md v0.2, 47 ports / 148 bits. It is NOT a
// suggestion. Every RM links into the SAME black-boxed `u_rp_dut` cell in the
// SAME locked static checkpoint, so a wrapper whose boundary differs by one
// port, one bit of width, or one direction does not "mostly work" -- it fails
// `link_design`/`pr_verify` after a full out-of-context synth, place and route.
// `make -C fpga/dfx pin-check` (root `make check` / `make check-ci` stage 2)
// catches all five drift classes (missing / wrong direction / wrong width /
// extra port / bad NGPIO) in about a second instead. Run it first.
//
// DIRECTIONS ARE MIRRORED. partition-pins.md states directions from the SHELL's
// view; an RM sits on the other side, so every one inverts:
//     contract O (shell drives) -> `input`  here
//     contract I (shell samples) -> `output` here
// (fpga/dfx/pin_check.py `RM_DIR`.)
//
// PROVENANCE OF THIS PORT LIST. It is NOT this file's to own. The authority is
// `fpga/shell/generated/rp_wrapper_skeleton.sv`, written by `tools/gen_boundary.py`
// from `fpga/shell/boundary.yaml`, and the two are held together by
//
//     python3 fpga/rp/_template/new_rm.py --check
//
// which compares them semantically -- (name, direction, width) in order -- and
// which `new_rm.py <name>` runs before it will scaffold anything. So do not
// "improve" the list below: change the YAML, regenerate, and re-run the check.
// What this template DOES own is the body the generator cannot write: the rm_id
// block and the safe-idle tie-offs.
//
// THREE RULES THAT ARE NOT NEGOTIABLE (partition-pins.md, fpga/dfx/rms/README.md):
//   1. No clock generation inside an RM. Every clock arrives as a partition pin.
//   2. No pin-facing IOB/OLOGIC attributes inside an RM -- OLOGIC pad sites are
//      static-only in a DFX design (partition-pins.md "IOB packing note").
//   3. No shell<->DUT AXI. The boundary is slow scalars and low-rate streams.
//
// AND ONE THAT BITES SILENTLY: every port this RM does not implement must be
// driven to a safe, legal, inert constant -- not left floating. The tie-off
// block at the bottom is the template's real payload; delete only the lines for
// the groups your DUT actually drives.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_template_wrapper #(
  // partition-pins.md "Board-port / GPIO passthrough -- I4": a build parameter,
  // v0 value 16. pin_check.py REQUIRES the literal `parameter int NGPIO = 16`
  // to be present and to be 16 -- keep the declaration, do not inline the width.
  parameter int NGPIO = 16
) (
  // ===========================================================================
  // Clocks & resets -- shell -> RP (partition-pins.md "Clocks & resets").
  // Do NOT re-synchronise these: the shell already deasserts each reset
  // synchronised to its destination domain ("Clock/reset domain rule").
  // ===========================================================================
  input  logic        dut_clk,          // DRP-reconfigurable DUT system clock
  input  logic        dut_resetn,       // reset #1 -- DUT system reset
  input  logic        rp_resetn,        // reset #2 -- held through a swap
  input  logic        dbg_resetn,       // reset #3 -- debug/SRST from OpenOCD

  // ===========================================================================
  // Processor debug -- internal JTAG / SWJ-DP (partition-pins.md
  // "Processor debug"). 3 out / 1 in. If your DUT has a SoC-400 SWJ-DP, wire
  // these to it and tie the straps (swj_enable / ntrst / npotrst) INSIDE the
  // wrapper -- they are deliberately not carried across the boundary.
  // ===========================================================================
  input  logic        jtag_tck,
  input  logic        jtag_tms,
  input  logic        jtag_tdi,
  output logic        jtag_tdo,

  // ===========================================================================
  // RM debug -- BSCAN to an RM debug hub (partition-pins.md "RM debug",
  // 2026-10 ILA mint). This RM carries no hub: the 11 legs are ignored and
  // dbg_bscan_tdo is tied low.
  // ===========================================================================
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

  // ===========================================================================
  // Ethernet -- RMII + MDIO (partition-pins.md "Ethernet"). The shell is the
  // virtual PHY: it sources the 50 MHz reference and answers MDIO.
  // ===========================================================================
  input  logic        phy_rmii_ref_clk,
  input  logic        phy_rmii_crs_dv,
  input  logic [1:0]  phy_rmii_rxd,
  output logic [1:0]  phy_rmii_txd,
  output logic        phy_rmii_tx_en,
  output logic        mdc,
  output logic        mdio_o,
  output logic        mdio_oe,
  input  logic        mdio_i,

  // ===========================================================================
  // Console / trace -- AXI-Stream byte pair + SWO (partition-pins.md
  // "Console / trace"). Relayed by the shell on TCP 6930/6931/6932.
  // ===========================================================================
  output logic [7:0]  uart_tx_tdata,
  output logic        uart_tx_tvalid,
  input  logic        uart_tx_tready,
  input  logic [7:0]  uart_rx_tdata,
  input  logic        uart_rx_tvalid,
  output logic        uart_rx_tready,
  output logic        swo,

  // ===========================================================================
  // Status / misc -- RP -> shell (partition-pins.md "Status / misc").
  // ===========================================================================
  output logic [31:0] rm_id,
  output logic        dut_lockup,
  output logic        irq_out,

  // ===========================================================================
  // Board-port / GPIO passthrough -- shell <-> RP (partition-pins.md I4).
  // The DUT reaches MPS3 board I/O only through here; the shell owns the pads.
  // ===========================================================================
  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // ===========================================================================
  // Flash / QSPI XiP -- RP <-> shell (partition-pins.md v0.2). The one
  // documented NON-CDC, source-synchronous group. If your DUT has no QSPI
  // controller, leave the safe-idle tie-off below exactly as it is:
  // qspi_csn = 1 (deselected) is the single intentional non-zero safe value in
  // the whole boundary, and matches the DFX decoupler's clamp.
  // ===========================================================================
  output logic        qspi_sclk,
  output logic        qspi_csn,
  output logic [3:0]  qspi_io_o,
  output logic [3:0]  qspi_io_oe,
  input  logic [3:0]  qspi_io_i
);

  // ===========================================================================
  // rm_id -- the RM-load-verify ground truth.
  //
  // The shell reads this back at DFXCTL.RM_ID (0x44A1_0010) after a partial
  // load and compares it with the value carried in the pushed partial's own
  // header, which is generated from the overlay manifest. If they disagree the
  // swap fails verify rather than quietly running the wrong design.
  //
  // ENCODING v2 (docs/VERSIONING_PLAN.md §3.2):
  //     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
  // The value here MUST equal `rm_id_of` in fpga/dfx/rm_list.tcl for this RM
  // and the `rm_id` in overlay/<rm_name>/manifest.json. All three are held in
  // lockstep by scripts/harness_gates/check_rm_id_encoding.py (make check
  // stage 2) -- a disagreement is a named CI failure, not a hardware surprise.
  //
  // ==> REPLACE BOTH HALVES WHEN YOU COPY THIS TEMPLATE. 0x00FF is a
  //     deliberately UNALLOCATED design_id placeholder: it is not in
  //     rm_list.tcl, so nothing can accidentally ship carrying it.
  // ===========================================================================
  // Start at v0.1: the registry convention (fpga/dfx/rm_list.tcl) is 0.1.0
  // until the commit that records a silicon run, then 1.0.0. Keep these three
  // sized exactly 8/8/16: check_rm_id_encoding.py reads the composition below
  // member by member and refuses any other width.
  localparam logic [7:0]  RM_VER_MAJOR = 8'd0;
  localparam logic [7:0]  RM_VER_MINOR = 8'd1;
  localparam logic [15:0] RM_DESIGN_ID = 16'h00FF;   // <-- allocate in rm_list.tcl
  localparam logic [31:0] RM_ID_TEMPLATE =
      {RM_VER_MAJOR, RM_VER_MINOR, RM_DESIGN_ID};

  assign rm_id = RM_ID_TEMPLATE;

  // ===========================================================================
  // >>> YOUR DUT GOES HERE <<<
  //
  // Instantiate the design under test and connect it to the groups it actually
  // uses, e.g.:
  //
  //   my_soc u_dut (
  //     .HCLK        (dut_clk),
  //     .HRESETn     (dut_resetn),
  //     ...
  //   );
  //
  // Two patterns worth copying rather than reinventing:
  //   * A byte-serial UART pin -> AXI-Stream byte pair: fpga/rp/nanosoc/
  //     uart_axis_shim.sv (shared by nanosoc and nanosoc_multicore).
  //   * Sources that live OUTSIDE this repo: do not vendor them. Reference the
  //     sibling checkout through an environment variable in a `filelist.tcl`
  //     next to this file -- fpga/rp/eth_ss/filelist.tcl and
  //     fpga/dfx/rms/rm_socscope/filelist.tcl are the two worked examples, and
  //     ooc_synth.tcl here already has the guarded `source` for it.
  //
  // Delete the matching lines from the tie-off block below as you connect each
  // group. Anything you leave tied off stays legal and inert.
  // ===========================================================================

  // ===========================================================================
  // Inert tie-offs -- every output not driven by a real DUT above.
  // "Safe, legally-driven, inert" (fpga/dfx/rms/README.md rule 5): not
  // floating, and not asserted without a reason.
  // ===========================================================================
  assign jtag_tdo       = 1'b0;
  assign dbg_bscan_tdo  = 1'b0;

  assign phy_rmii_txd   = 2'b00;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc            = 1'b0;
  assign mdio_o         = 1'b0;
  assign mdio_oe        = 1'b0;          // MDIO tri-stated (input mode)

  assign uart_tx_tdata  = 8'h00;
  assign uart_tx_tvalid = 1'b0;
  assign uart_rx_tready = 1'b0;
  assign swo            = 1'b0;

  assign dut_lockup     = 1'b0;
  assign irq_out        = 1'b0;

  assign dut_gpio_o     = {NGPIO{1'b0}};
  assign dut_gpio_oe    = {NGPIO{1'b0}};  // all bits released -> the shell's
                                          // GPIO.OWN mux can still drive them

  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;           // active-low CS DEASSERTED (safe idle)
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;

  // Inputs this skeleton does not consume. Named here so `verilator --lint-only
  // -Wall` attributes them, and so a reader can see nothing was forgotten.
  wire _unused_ok = &{1'b0,
                      dut_clk, dut_resetn, rp_resetn, dbg_resetn,
                      jtag_tck, jtag_tms, jtag_tdi,
                      phy_rmii_ref_clk, phy_rmii_crs_dv, phy_rmii_rxd, mdio_i,
                      uart_tx_tready, uart_rx_tdata, uart_rx_tvalid,
                      dut_gpio_i, qspi_io_i,
                      1'b0};

endmodule
