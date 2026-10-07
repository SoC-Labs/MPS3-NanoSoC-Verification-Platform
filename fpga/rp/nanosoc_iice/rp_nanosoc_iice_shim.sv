// -----------------------------------------------------------------------------
// rp_nanosoc_iice_shim.sv — the DFX RM top for the IICE-instrumented nanosoc.
//
// EXACTLY the 47 ports / 148 bits of fpga/shell/boundary.yaml (the boundary of
// the 2026-10 ILA mint; 35 / 136 before its `dbgbscan` group, whose 12 legs this
// shim passes straight through to the core). Identify adds zero partition pins,
// therefore: no contract edit, no pin_check.py change, no shell rebuild, no
// static_id re-mint, no overlay re-key. This is a partial-bitstream-only change.
//
// =============================================================================
// WHAT CHANGED, AND WHY IT HAD TO
// =============================================================================
// This file used to declare `swd_clk / swd_dio_o / swd_dio_oe / swd_dio_i` and
// hand all four to the Identify soft TAP outright (`IICE_OWNS_SWD = 1`), which
// left the DUT's own debug port dark for the life of the bitstream. That design
// died with the A6 SWD->JTAG cutover:
//
//   * the swd_* group NO LONGER EXISTS in the boundary — fpga/shell/boundary.yaml
//     group `jtag` is jtag_tck / jtag_tms / jtag_tdi (shell drives) + jtag_tdo
//     (shell receives), and fpga/shell/ip/swd_bb was replaced by
//     fpga/shell/ip/jtag_bb (shell_bd.tcl:560, JTAGBB @ 0x44A7_0000);
//   * so an RM declaring swd_* cannot link into the fielded static at all;
//   * and there is no longer a SPARE wire-set to steal. jtag_* is the only debug
//     wire-set the boundary has, and the DUT's CoreSight SoC-400 SWJ-DP is
//     already on it (fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:519-528).
//
// "Steal the pins" is therefore no longer a design option — taking them would
// make the M0 unreachable, which is the one thing this platform exists to do.
//
// =============================================================================
// THE ANSWER: AN IEEE 1149.1 DAISY CHAIN, NOT A MUX
// =============================================================================
// Two TAPs, one wire-set, both live at once:
//
//        shell jtag_tck  ────────┬────────────────────┐
//        shell jtag_tms  ──────┬─│──────────────────┐ │
//                              │ │                  │ │
//                            TMS TCK              TMS TCK
//        shell jtag_tdi ───▶ ┌─────────────┐ ───▶ ┌─────────────┐ ───▶ shell
//                        TDI │ Identify    │ TDO  │ SoC-400     │ TDO   jtag_tdo
//                            │ soft TAP    │  TDI │ SWJ-DP      │
//                            │ (IICE)      │      │ (the M0 DAP)│
//                            └─────────────┘      └─────────────┘
//                              in the EDIF          inside u_rm
//
// TCK and TMS are shared, TDI/TDO are cascaded. That is the whole of 1149.1
// multi-drop, and it is why this works with no arbitration and no ownership
// register: BOTH TAPs are always in the SAME controller state (they see the same
// TMS on the same TCK edges), and whichever one is not being addressed sits in
// BYPASS contributing exactly one shift-register bit.
//
// It also disposes of a correctness worry that looks alarming and is not: the
// SWJ-DP's TDO (`dap_tdo`) is only meaningful during the Shift-DR/Shift-IR
// states, and its output enable `dap_ntdoen` is deliberately left open by
// rp_nanosoc_wrapper. In a chain that is harmless PRECISELY BECAUSE TMS/TCK are
// shared: the downstream TAP only samples its TDI while it is itself in a Shift
// state, which is the same instant the upstream TAP is driving a real value.
//
// -----------------------------------------------------------------------------
// CHAIN ORDER — DECIDED, AND HERE IS THE REASONING
// -----------------------------------------------------------------------------
// Default `IICE_TAP_NEAREST_TDI = 1`:
//
//        shell TDI -> [Identify soft TAP] -> [SoC-400 SWJ-DP] -> shell TDO
//
// i.e. the DUT's DAP is the TAP NEAREST THE SHELL'S TDO. Three reasons, all
// checkable:
//
//  1. OPENOCD DECLARATION ORDER. OpenOCD's first-declared `jtag newtap` is the
//     TAP nearest TDO. That is not folklore: `jtag_tap_add()` appends to the
//     tail of the tap list (openocd src/jtag/core.c:213-224), the tap list is
//     walked head-first into `fields[]` (src/jtag/drivers/driver.c:152),
//     `jtag_build_buffer()` lays fields into the wire buffer in index order from
//     bit 0 (src/jtag/commands.c:215-221), and bitbang.c shifts buffer bit 0 out
//     FIRST (src/jtag/drivers/bitbang.c:215-227) — and the first bits back on
//     TDO are the capture contents of the device nearest TDO. The user guide
//     states the same thing by worked example (doc/openocd.texi:13861-13895, the
//     STM32+Xilinx "JTAG TAP Order" FAQ).
//     So putting the DAP nearest TDO keeps `jtag newtap nanosoc cpu` as
//     declaration #0 — exactly where host/openocd/nanosoc_mps3_jtag.cfg:80 has
//     it today — and every `dap create ... -chain-position nanosoc.cpu`,
//     `target create ... -ap-num 0` and `nanosoc.dap apreg` line in
//     host/openocd/nanosoc_ops.tcl carries across UNCHANGED.
//
//  2. THE LEGACY CONFIG FAILS LEGIBLY. Someone will point the existing
//     single-TAP cfg at a chained RM. With the DAP nearest TDO, OpenOCD reads
//     the first 32 IDCODE bits as nanosoc.cpu, gets 0x6BA00477, MATCHES, and
//     then complains about a device after the end of the chain — a message that
//     names the real problem. With the order reversed it would report
//     "nanosoc.cpu UNEXPECTED: 0x<the Identify TAP's id>", which sends the
//     reader to debug a DAP that is fine.
//
//  3. THE PROVEN RETURN LEG IS LEFT ALONE. With this order the RM's `jtag_tdo`
//     output is driven by the DAP's TDO and nothing else — structurally
//     identical to the fielded rm_nanosoc, whose M0-halt-over-JTAG is
//     silicon-proven. The new, unproven logic is spliced onto the TDI side,
//     where TDI is a shell-driven input that jtag_bb's own CDC rationale
//     already guarantees is stable for a whole AXI write around every TCK edge
//     (fpga/shell/ip/jtag_bb/jtag_bb.sv header). `set_false_path -to
//     [get_ports jtag_tdo]` in nanosoc_iice.fdc therefore keeps meaning exactly
//     what the same line means in fpga/rp/nanosoc/nanosoc_ooc.xdc:137.
//
// Set `IICE_TAP_NEAREST_TDI = 0` to build the mirror image (DAP nearest TDI).
// It is a legal chain and it is what tests/jtag_chain uses as its RTL-side
// negative control: with the order flipped in the fabric but not in the host
// config, the two IDCODEs come back SWAPPED, which is the failure an operator
// would otherwise diagnose as "the DAP is dead".
//
// -----------------------------------------------------------------------------
// WHAT THE HOST MUST NOW DO DIFFERENTLY
// -----------------------------------------------------------------------------
//   * OpenOCD: host/openocd/nanosoc_iice_chain.cfg declares BOTH TAPs. The
//     single-TAP host/openocd/nanosoc_mps3_jtag.cfg is still correct for every
//     NON-instrumented RM and is deliberately not edited.
//   * The firmware XVC engine (firmware/xvc_server/) must target JTAGBB, not
//     the retired SWDBB: `XVC_TARGET=jtagbb` / -DMPS3_XVC_TARGET_JTAGBB. It is
//     a bit shifter and is chain-agnostic — the BYPASS padding is the CLIENT's
//     job, in both OpenOCD and Identify.
//   * Identify's debugger is told about the chain with `idcode add` +
//     `chain add` + `chain select` (host/identify/debug_session.tcl already has
//     the seam; IICE_CHAIN_* environment). The debugger's own manual documents
//     a two-device example, so a chain is supported.
//
// -----------------------------------------------------------------------------
// THE OPERATIONAL HAZARD THIS CREATES (read before the first board session)
// -----------------------------------------------------------------------------
// rp_nanosoc_wrapper ties `dap_npotrst` to the DUT's system reset. Asserting
// dut_resetn therefore RESETS THE SWJ-DP's TAP CONTROLLER to Test-Logic-Reset,
// where 1149.1 reloads the IDCODE instruction — so that TAP's DR width jumps
// from 1 bit (BYPASS) to 32 bits (IDCODE) under the host's feet. Any Identify
// scan in flight across a DUT reset is silently misaligned by 31 bits. The rule
// is: after any dut_resetn pulse, re-run the host's chain scan (OpenOCD
// `jtag arp_init` / Identify `chain info -raw`) before trusting a capture.
// This is a property of the chain, not of the order, and it applies to both.
//
// TIMING: the soft TAP's TCK is now the real jtag_tck partition pin.
// nanosoc_iice.fdc declares it at 100 ns (10 MHz), deliberately faster than any
// firmware bit-bang rate and tighter than Identify's own auto-constraint on
// identify_jtag_tck (250 ns / 4 MHz, $SYNPLIFY_HOME/lib/share/synthesis/syn.sdc).
// `device skewfree 1` builds master/slave flops on the JTAG chain so the TAP
// needs no global clock buffer — mandatory here, because the RP pblock has ZERO
// BUFG sites.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_nanosoc_iice_shim #(
  // fpga/shell/boundary.yaml "gpio" group. v0 = 16.
  // fpga/dfx/pin_check.py REQUIRES this declaration and requires it to be 16.
  parameter int NGPIO = 16,

  // 1 (DEFAULT) = Identify soft TAP nearest the shell's TDI, SWJ-DP nearest the
  //     shell's TDO. See "CHAIN ORDER" in the header for why.
  // 0 = the mirror image. Legal, and used as tests/jtag_chain's RTL-side
  //     negative control; it invalidates host/openocd/nanosoc_iice_chain.cfg's
  //     declaration order, so do not ship it without swapping the cfg too.
  parameter bit IICE_TAP_NEAREST_TDI = 1'b1
) (
  // ---- Clocks & resets (shell -> RP) ---------------------------------------
  input  logic dut_clk,
  input  logic dut_resetn,
  input  logic rp_resetn,
  input  logic dbg_resetn,

  // ---- Processor debug — the ONE 4-wire JTAG wire-set, carrying TWO TAPs ----
  input  logic jtag_tck,      // shared by both TAPs
  input  logic jtag_tms,      // shared by both TAPs
  input  logic jtag_tdi,      // head of the chain
  output logic jtag_tdo,      // tail of the chain

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
  // The chain. Four nets, no logic: a 1149.1 daisy chain is wiring.
  //
  //   iice_tdi / iice_tdo  the Identify soft TAP's serial legs, at the
  //                        instrumented EDIF cell's added ports.
  //   dap_tdi  / dap_tdo   the SWJ-DP's serial legs, at the core cell's
  //                        jtag_tdi / jtag_tdo contract ports (the core passes
  //                        them straight through to u_rm).
  //
  // Written as an explicit two-branch generate rather than a ternary so that
  // each order reads as the picture in the header, and so that the unused
  // branch does not exist at all rather than constant-folding.
  // ---------------------------------------------------------------------------
  logic iice_tdi, iice_tdo;   // Identify soft TAP
  logic dap_tdi,  dap_tdo;    // SoC-400 SWJ-DP (inside u_core/u_rm)

  generate
    if (IICE_TAP_NEAREST_TDI) begin : g_iice_first
      // shell TDI -> Identify -> SWJ-DP -> shell TDO   (THE DEFAULT)
      assign iice_tdi = jtag_tdi;
      assign dap_tdi  = iice_tdo;
      assign jtag_tdo = dap_tdo;
    end else begin : g_dap_first
      // shell TDI -> SWJ-DP -> Identify -> shell TDO   (mirror image)
      assign dap_tdi  = jtag_tdi;
      assign iice_tdi = dap_tdo;
      assign jtag_tdo = iice_tdo;
    end
  endgenerate
  // ---------------------------------------------------------------------------
  // Identity. This RM is registered in fpga/dfx/rm_list.tcl as its OWN design
  // (design_id 0x0008 @ 0.1.0 => 0x00010008), not as an alias of rm_nanosoc: a
  // mint that carries both needs two ids, or DFXCTL.RM_ID cannot say which is
  // in the RP and the firmware's masked name lookup names the wrong one. The
  // core still emits rm_nanosoc's 0x01000001; it is left dangling on purpose
  // and the shim drives the partition pin itself. Consequence: the `make stage`
  // A/B path (hand this checkpoint to build_dfx AS rm_nanosoc) is no longer
  // identity-compatible -- build through the registry entry instead
  // (RM_SYNTH_REUSE_rm_nanosoc_iice). check_rm_id_encoding.py reads the
  // localparam below; the registry and any manifest must agree with it.
  // ---------------------------------------------------------------------------
  localparam logic [31:0] RM_ID_NANOSOC_IICE = 32'h0001_0008;
  logic [31:0] rm_id_inner_unused;
  assign rm_id = RM_ID_NANOSOC_IICE;


  // ---------------------------------------------------------------------------
  // The instrumented core.
  //
  // In the INSTRUMENTED flow this instance resolves to the Synplify EDIF cell
  // (fpga/rp/nanosoc_iice/ooc_synth_synplify.tcl: read_edif + read_verilog +
  // synth_design -mode out_of_context -top rp_nanosoc_iice_shim), which carries
  // 51 ports: the 47 declared in rp_nanosoc_iice_core.sv plus the four
  // identify_jtag_* the instrumentor added. The four connections below are
  // therefore ONLY valid against an instrumented EDIF — an uninstrumented build
  // has no such ports and must use rp_nanosoc_iice_core directly as the RM top
  // (Makefile IICE=0), not this shim. In that IICE=0 configuration there is no
  // chain at all: the core's jtag_tdi/jtag_tdo are the shell's, and the RM is
  // the same single-TAP target as the fielded rm_nanosoc.
  //
  // Instance name `u_core` matters for Identify probe paths only in the sense
  // that it does NOT appear in them: Identify resolves paths against the
  // SYNTHESIS top (rp_nanosoc_iice_core), which is this cell, so "/" is
  // u_core's own scope and the first path element is u_core's child `u_rm`.
  // ---------------------------------------------------------------------------
  rp_nanosoc_iice_core #(
    .NGPIO (NGPIO)
  ) u_core (
    .dut_clk           (dut_clk),
    .dut_resetn        (dut_resetn),
    .rp_resetn         (rp_resetn),
    .dbg_resetn        (dbg_resetn),

    // The SWJ-DP's position in the chain. tck/tms are the shared wires; tdi/tdo
    // are spliced above.
    .jtag_tck          (jtag_tck),
    .jtag_tms          (jtag_tms),
    .jtag_tdi          (dap_tdi),
    .jtag_tdo          (dap_tdo),

    .dbg_bscan_bscanid_en (dbg_bscan_bscanid_en),
    .dbg_bscan_capture (dbg_bscan_capture),
    .dbg_bscan_drck    (dbg_bscan_drck),
    .dbg_bscan_reset   (dbg_bscan_reset),
    .dbg_bscan_runtest (dbg_bscan_runtest),
    .dbg_bscan_sel     (dbg_bscan_sel),
    .dbg_bscan_shift   (dbg_bscan_shift),
    .dbg_bscan_tck     (dbg_bscan_tck),
    .dbg_bscan_tdi     (dbg_bscan_tdi),
    .dbg_bscan_tms     (dbg_bscan_tms),
    .dbg_bscan_update  (dbg_bscan_update),
    .dbg_bscan_tdo     (dbg_bscan_tdo),

    .phy_rmii_ref_clk  (phy_rmii_ref_clk),
    .phy_rmii_crs_dv   (phy_rmii_crs_dv),
    .phy_rmii_rxd      (phy_rmii_rxd),
    .phy_rmii_txd      (phy_rmii_txd),
    .phy_rmii_tx_en    (phy_rmii_tx_en),
    .mdc               (mdc),
    .mdio_o            (mdio_o),
    .mdio_oe           (mdio_oe),
    .mdio_i            (mdio_i),

    .uart_tx_tdata     (uart_tx_tdata),
    .uart_tx_tvalid    (uart_tx_tvalid),
    .uart_tx_tready    (uart_tx_tready),
    .uart_rx_tdata     (uart_rx_tdata),
    .uart_rx_tvalid    (uart_rx_tvalid),
    .uart_rx_tready    (uart_rx_tready),
    .swo               (swo),

    .rm_id             (rm_id_inner_unused),   // the core emits rm_nanosoc's 0x01000001: NOT ours, see above
    .dut_lockup        (dut_lockup),
    .irq_out           (irq_out),

    .dut_gpio_o        (dut_gpio_o),
    .dut_gpio_oe       (dut_gpio_oe),
    .dut_gpio_i        (dut_gpio_i),

    .qspi_sclk         (qspi_sclk),
    .qspi_csn          (qspi_csn),
    .qspi_io_o         (qspi_io_o),
    .qspi_io_oe        (qspi_io_oe),
    .qspi_io_i         (qspi_io_i),

    // --- the four ports the Identify instrumentor ADDED (soft TAP) ----------
    // TCK/TMS are the shell's, unmodified — that is what makes both TAPs walk
    // the 1149.1 state graph in lockstep. TDI/TDO are the chain splice.
    .identify_jtag_tck (jtag_tck),
    .identify_jtag_tms (jtag_tms),
    .identify_jtag_tdi (iice_tdi),
    .identify_jtag_tdo (iice_tdo)
  );

endmodule
