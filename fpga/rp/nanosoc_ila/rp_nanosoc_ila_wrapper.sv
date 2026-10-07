// -----------------------------------------------------------------------------
// rp_nanosoc_ila_wrapper.sv -- rm_nanosoc_ila: the single-core nanoSoC
// (rp_nanosoc_wrapper, UNCHANGED, nested exactly as rp_nanosoc_upy_wrapper.sv
// nests it) plus an RM debug hub and ONE ILA on the DUT's BOUNDARY-VISIBLE nets.
// The write-up's ILA demo (docs/planning/ILA_MINT_PLAN_2026-09-23.md §0b).
//
// WHAT IT PROBES, AND WHY ONLY THESE
//   Vivado synthesis cannot reach into the SoC hierarchy (no XMR, and the SoC
//   tree is read-only), and netlist insertion (mark_debug + create_debug_core
//   on the linked RM) was never spiked (handover §4.6). So the ILA sees only
//   what rp_nanosoc_wrapper DRIVES OUT to this wrapper:
//     probe0  uart_tx_tdata[7:0]  the console byte (UART2 TXD, already
//                                 deserialised by uart_axis_shim INSIDE
//                                 rp_nanosoc_wrapper -- the serial TXD pin is
//                                 NOT a port of it, so the bytes are what is
//                                 visible; the banner decodes directly)
//     probe1  uart_tx_tvalid
//     probe2  uart_tx_tready      (shell-driven; completes the handshake)
//     probe3  dut_gpio_o[15:0]    the GPIO outputs (low byte = LEDs)
//     probe4  jtag_tdo            the SWJ-DP's TDO
//     probe5  dut_resetn          the DUT reset input
//     probe6  cycle[31:0]         free-running dut_clk cycle counter
//   depth 16384, CAPTURE (storage) QUALIFICATION enabled: with the capture
//   condition `uart_tx_tvalid && uart_tx_tready` each stored sample is one
//   console byte (with its cycle stamp), so the whole boot banner fits; with no
//   qualifier it is a plain 16384-cycle (328 us) window.
//
// clk: the ILA on dut_clk (what it samples); the hub on phy_rmii_ref_clk
// (always on, F12) via fpga/rp/common/rp_dbg_hub.sv.
//
// rm_id: design_id 0x000A @ v1.0 => 0x0100000A (rm_list.tcl derives it); the
// inner wrapper's own 0x01000001 is left dangling, exactly as the upy wrapper
// does, so DFXCTL.RM_ID says WHICH nanosoc is loaded.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_nanosoc_ila_wrapper #(
  parameter int NGPIO = 16,
  parameter        UART_CLK_HZ = 50_000_000,
  parameter        UART_BAUD   = 76800,
  // ooc_synth.tcl always passes the absolute path (default
  // <repo>/fpga/rp/nanosoc/hello_image.hex, or $IMEM_IMG) as a generic.
  parameter        IMEM_MEM_FPGA_IMG = "hello_image.hex"
) (
  input  logic        dut_clk,
  input  logic        dut_resetn,
  input  logic        rp_resetn,
  input  logic        dbg_resetn,

  input  logic        jtag_tck,
  input  logic        jtag_tms,
  input  logic        jtag_tdi,
  output logic        jtag_tdo,

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

  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  output logic        qspi_sclk,
  output logic        qspi_csn,
  output logic [3:0]  qspi_io_o,
  output logic [3:0]  qspi_io_oe,
  input  logic [3:0]  qspi_io_i
);

  // --- the DUT, unchanged ------------------------------------------------------
  logic [31:0] inner_rm_id;
  logic        unused_inner_bscan_tdo;

  rp_nanosoc_wrapper #(
    .NGPIO             (NGPIO),
    .UART_CLK_HZ       (UART_CLK_HZ),
    .UART_BAUD         (UART_BAUD),
    .IMEM_MEM_FPGA_IMG (IMEM_MEM_FPGA_IMG)
  ) u_dut (
    .dut_clk          (dut_clk),
    .dut_resetn       (dut_resetn),
    .rp_resetn        (rp_resetn),
    .dbg_resetn       (dbg_resetn),
    .jtag_tck         (jtag_tck),
    .jtag_tms         (jtag_tms),
    .jtag_tdi         (jtag_tdi),
    .jtag_tdo         (jtag_tdo),
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
    .rm_id            (inner_rm_id),
    .dut_lockup       (dut_lockup),
    .irq_out          (irq_out),
    .dut_gpio_o       (dut_gpio_o),
    .dut_gpio_oe      (dut_gpio_oe),
    .dut_gpio_i       (dut_gpio_i),
    .qspi_sclk        (qspi_sclk),
    .qspi_csn         (qspi_csn),
    .qspi_io_o        (qspi_io_o),
    .qspi_io_oe       (qspi_io_oe),
    .qspi_io_i        (qspi_io_i),
    // The BSCAN legs are NOT passed down: THIS wrapper's hub owns them. The
    // inner wrapper carries no hub (it ties its tdo low); its inputs are held
    // at 0 and its tdo is left dangling.
    .dbg_bscan_bscanid_en (1'b0),
    .dbg_bscan_capture    (1'b0),
    .dbg_bscan_drck       (1'b0),
    .dbg_bscan_reset      (1'b0),
    .dbg_bscan_runtest    (1'b0),
    .dbg_bscan_sel        (1'b0),
    .dbg_bscan_shift      (1'b0),
    .dbg_bscan_tck        (1'b0),
    .dbg_bscan_tdi        (1'b0),
    .dbg_bscan_tms        (1'b0),
    .dbg_bscan_update     (1'b0),
    .dbg_bscan_tdo        (unused_inner_bscan_tdo)
  );

  // --- identity ----------------------------------------------------------------
  localparam logic [31:0] RM_ID_NANOSOC_ILA = 32'h0100_000A;
  assign rm_id = RM_ID_NANOSOC_ILA;
  wire [31:0] unused_inner_rm_id = inner_rm_id;

  // --- cycle stamp ---------------------------------------------------------------
  // Free-running, NO reset: it must keep counting through dut_resetn so a
  // capture spanning a reset still has a monotonic time axis.
  logic [31:0] cycle = '0;
  always_ff @(posedge dut_clk) cycle <= cycle + 32'd1;

  // --- the ILA -------------------------------------------------------------------
  ila_nanosoc u_ila_nanosoc (   // distinct per debug RM: the ILA uuid follows this name (findings #6; ltx_sidecar.py gate)
    .clk    (dut_clk),
    .probe0 (uart_tx_tdata),
    .probe1 (uart_tx_tvalid),
    .probe2 (uart_tx_tready),
    .probe3 (dut_gpio_o),
    .probe4 (jtag_tdo),
    .probe5 (dut_resetn),
    .probe6 (cycle)
  );

  // --- the RM's debug hub (xsdbm) on the always-on clock -------------------------
  rp_dbg_hub u_dbg_hub (
    .clk                  (phy_rmii_ref_clk),
    .dbg_bscan_bscanid_en (dbg_bscan_bscanid_en),
    .dbg_bscan_capture    (dbg_bscan_capture),
    .dbg_bscan_drck       (dbg_bscan_drck),
    .dbg_bscan_reset      (dbg_bscan_reset),
    .dbg_bscan_runtest    (dbg_bscan_runtest),
    .dbg_bscan_sel        (dbg_bscan_sel),
    .dbg_bscan_shift      (dbg_bscan_shift),
    .dbg_bscan_tck        (dbg_bscan_tck),
    .dbg_bscan_tdi        (dbg_bscan_tdi),
    .dbg_bscan_tms        (dbg_bscan_tms),
    .dbg_bscan_update     (dbg_bscan_update),
    .dbg_bscan_tdo        (dbg_bscan_tdo)
  );

endmodule
