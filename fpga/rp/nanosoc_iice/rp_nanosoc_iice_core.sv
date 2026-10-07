// -----------------------------------------------------------------------------
// rp_nanosoc_iice_core.sv — the SYNPLIFY / IDENTIFY synthesis top for the
// IICE-instrumented nanosoc reconfigurable module.
//
// WHY THIS FILE EXISTS (read this before "simplifying" it away)
// ------------------------------------------------------------
// `device jtagport soft` does NOT reuse pre-declared nets: the Identify
// instrumentor CREATES four brand-new external ports on the synthesis top and
// prints
//
//     "The following external ports have been added to your design for
//      communication:
//          identify_jtag_tck : Identify Testport TCK
//          identify_jtag_tms : Identify Testport TMS
//          identify_jtag_tdi : Identify Testport TDI
//          identify_jtag_tdo : Identify Testport TDO"
//
// (verbatim from hwg_comm_gen_c::PrintJTAGUserMessage() in the 2022.09-SP2
// instrumentor binary; corroborated by identify_debugger_ug_synplify.pdf p.83
// "The debugger connects the TAP controller to four top-level I/O connections
// to the design").
//
// Consequence: the Synplify top CANNOT be the DFX RM top, because the DFX RM
// top must carry EXACTLY the 47 ports of docs/contracts/partition-pins.md
// (148 bits) and nothing else, and there is no RTL scope above the synthesis
// top in which to tie the four added ports to anything.
//
// So the boundary is split in two:
//
//   rp_nanosoc_iice_core   <- THIS FILE. Synplify/Identify top.
//                             47 contract ports in RTL. After instrumentation
//                             the EDIF carries 47 + 4 = 51 ports.
//   rp_nanosoc_iice_shim   <- the DFX RM top. 47 contract ports, Vivado-side,
//                             instantiates the 51-port EDIF cell and splices
//                             identify_jtag_{tck,tms,tdi,tdo} into the ONE
//                             jtag_* partition wire-set as a 1149.1 daisy
//                             chain (docs/planning/IICE_JTAG_CHAIN.md).
//
// Both files carry exactly 47 ports and both are checked by
// fpga/dfx/pin_check.py. Nothing is added to the boundary, so no shell
// rebuild and no static_id re-mint (IDENTIFY_IICE_DFX_PLAN.md §3).
//
// This module is a PURE PASS-THROUGH. It deliberately contains no logic: any
// logic here would be logic the shim cannot see, and the whole point of the
// split is that everything reinterpretable lives in the shim.
//
// UNINSTRUMENTED BUILDS: with `IICE=0` (Makefile) Synplify produces a 47-port
// EDIF for this module and the shim is not used at all — rp_nanosoc_iice_core
// is then itself the RM top. That is the plan's Phase-3 "Synplify swap without
// Identify" configuration, and it is why this module's port list is the
// contract and not "the contract plus four". In that configuration jtag_tdi /
// jtag_tdo below reach the DUT's SWJ-DP directly and the RM is a SINGLE-TAP
// target, byte-for-byte the fielded rm_nanosoc debug topology.
//
// 2026-09-10 REBASE onto the then-FIELDED boundary (mint 0xA8C1C535)
// ------------------------------------------------------------
// This file used to carry the four `swd_*` ports. They NO LONGER EXIST: the A6
// SWD->JTAG cutover replaced the shell's swd_bb with jtag_bb and the partition
// boundary's "Processor debug" group is now jtag_tck / jtag_tms / jtag_tdi
// (shell drives) + jtag_tdo (shell receives) — fpga/shell/boundary.yaml, group
// `jtag`. An RM still declaring swd_* cannot link into the fielded static at
// all, which is why this module had to be re-derived rather than patched.
//
// The consequence for Identify is structural, not cosmetic. The old design gave
// the soft TAP the four SWD pins OUTRIGHT (`IICE_OWNS_SWD=1`) and the DUT's
// SW-DP went dark for the life of the bitstream. There is no longer a spare
// wire-set to steal: jtag_* is the ONLY debug wire-set the boundary has, and
// the DUT's SoC-400 SWJ-DP is on it. So the two TAPs are put in an IEEE 1149.1
// DAISY CHAIN on the one wire-set instead of being muxed — see
// rp_nanosoc_iice_shim.sv and docs/planning/IICE_JTAG_CHAIN.md. This module's
// job in that chain is unchanged: hand jtag_tdi/jtag_tdo straight to u_rm's
// SWJ-DP leg. The SHIM decides where in the chain that leg sits.
//
// Hierarchy note for Identify probe paths (`hw:` in signals_nanosoc.yaml):
// Identify's "/" IS the synthesis top's own scope and the top MODULE NAME is
// never part of the path (identify_debug_env_reference.pdf p.14, "Design
// Hierarchy References": "Absolute path names begin with a path separator
// character. The top-level design unit is represented by the initial '/'").
// With this file as the synthesis top, the wrapper instance name below (u_rm)
// is therefore the FIRST path element. It is FROZEN — renaming it silently
// invalidates every hw: path in the manifest.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_nanosoc_iice_core #(
  // partition-pins.md "Board-port / GPIO passthrough -- I4". v0 = 16.
  // fpga/dfx/pin_check.py REQUIRES this declaration and requires it to be 16.
  parameter int NGPIO = 16
) (
  // ---- Clocks & resets (shell -> RP) ---------------------------------------
  input  logic dut_clk,
  input  logic dut_resetn,
  input  logic rp_resetn,
  input  logic dbg_resetn,

  // ---- Processor debug — 4-wire JTAG (fpga/shell/boundary.yaml `jtag`) -----
  // NOTE for the INSTRUMENTED build: jtag_tck/jtag_tms arrive from the shell
  // unchanged (both TAPs of the chain share them), but jtag_tdi is NOT the
  // shell's TDI and jtag_tdo is NOT the shell's TDO — the shim splices the
  // Identify soft TAP into the serial path, so these two are this cell's
  // position IN THE CHAIN. At this level that is invisible: a TAP neither knows
  // nor cares who feeds its TDI. See rp_nanosoc_iice_shim.sv.
  input  logic jtag_tck,
  input  logic jtag_tms,
  input  logic jtag_tdi,
  output logic jtag_tdo,

  // ---- RM debug — BSCAN to an RM debug hub (boundary.yaml `dbgbscan`) -----
  // Passed straight through; no hub at this level (2026-10 ILA mint).
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
  output logic dbg_bscan_tdo,

  // ---- Ethernet — RMII + MDIO ----------------------------------------------
  input  logic       phy_rmii_ref_clk,
  input  logic       phy_rmii_crs_dv,
  input  logic [1:0] phy_rmii_rxd,
  output logic [1:0] phy_rmii_txd,
  output logic       phy_rmii_tx_en,
  output logic       mdc,
  output logic       mdio_o,
  output logic       mdio_oe,
  input  logic       mdio_i,

  // ---- Console / trace — AXI-Stream byte -----------------------------------
  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,
  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready,
  output logic       swo,

  // ---- Status / misc (RP -> shell) -----------------------------------------
  output logic [31:0] rm_id,
  output logic        dut_lockup,
  output logic        irq_out,

  // ---- Board-port / GPIO passthrough ---------------------------------------
  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // ---- Flash / QSPI XiP ----------------------------------------------------
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i
);

  // ---------------------------------------------------------------------------
  // The real RM. Instance name `u_rm` is FROZEN: it is the first element of
  // every Identify probe path (see the header note).
  //
  // Parameters are DELIBERATELY not forwarded except NGPIO. rp_nanosoc_wrapper
  // owns the product defaults (UART_CLK_HZ / UART_BAUD / IMEM_MEM_FPGA_IMG /
  // IMEM_RAM_ADDR_W / DMEM_RAM_ADDR_W) and fpga/rp/nanosoc/ooc_synth.tcl does
  // not override them either — mirroring that keeps the instrumented RM
  // bit-comparable with the Vivado baseline. Restating the defaults here would
  // just create a second place for them to drift.
  //
  // IMEM image: rp_nanosoc_wrapper's IMEM_MEM_FPGA_IMG default is a bare
  // "hello_image.hex". gen_prj.tcl bakes the ABSOLUTE path of
  // <repo>/fpga/rp/nanosoc/hello_image.hex into the generated copy of the
  // wrapper it hands Synplify -- the same file ooc_synth.tcl passes to the
  // Vivado baseline as a generic, so an IICE build and a baseline build read
  // the same hex, which is what we want for A/B.
  // ---------------------------------------------------------------------------
  rp_nanosoc_wrapper #(
    .NGPIO (NGPIO)
  ) u_rm (
    .dut_clk          (dut_clk),
    .dut_resetn       (dut_resetn),
    .rp_resetn        (rp_resetn),
    .dbg_resetn       (dbg_resetn),

    .jtag_tck         (jtag_tck),
    .jtag_tms         (jtag_tms),
    .jtag_tdi         (jtag_tdi),
    .jtag_tdo         (jtag_tdo),

    .dbg_bscan_bscanid_en (dbg_bscan_bscanid_en),
    .dbg_bscan_capture (dbg_bscan_capture),
    .dbg_bscan_drck   (dbg_bscan_drck),
    .dbg_bscan_reset  (dbg_bscan_reset),
    .dbg_bscan_runtest (dbg_bscan_runtest),
    .dbg_bscan_sel    (dbg_bscan_sel),
    .dbg_bscan_shift  (dbg_bscan_shift),
    .dbg_bscan_tck    (dbg_bscan_tck),
    .dbg_bscan_tdi    (dbg_bscan_tdi),
    .dbg_bscan_tms    (dbg_bscan_tms),
    .dbg_bscan_update (dbg_bscan_update),
    .dbg_bscan_tdo    (dbg_bscan_tdo),

    .phy_rmii_ref_clk (phy_rmii_ref_clk),
    .phy_rmii_crs_dv  (phy_rmii_crs_dv),
    .phy_rmii_rxd     (phy_rmii_rxd),
    .phy_rmii_txd     (phy_rmii_txd),
    .phy_rmii_tx_en   (phy_rmii_tx_en),
    .mdc              (mdc),
    .mdio_o           (mdio_o),
    .mdio_oe          (mdio_oe),
    .mdio_i           (mdio_i),

    .uart_tx_tdata    (uart_tx_tdata),
    .uart_tx_tvalid   (uart_tx_tvalid),
    .uart_tx_tready   (uart_tx_tready),
    .uart_rx_tdata    (uart_rx_tdata),
    .uart_rx_tvalid   (uart_rx_tvalid),
    .uart_rx_tready   (uart_rx_tready),
    .swo              (swo),

    .rm_id            (rm_id),
    .dut_lockup       (dut_lockup),
    .irq_out          (irq_out),

    .dut_gpio_o       (dut_gpio_o),
    .dut_gpio_oe      (dut_gpio_oe),
    .dut_gpio_i       (dut_gpio_i),

    .qspi_sclk        (qspi_sclk),
    .qspi_csn         (qspi_csn),
    .qspi_io_o        (qspi_io_o),
    .qspi_io_oe       (qspi_io_oe),
    .qspi_io_i        (qspi_io_i)
  );

endmodule
