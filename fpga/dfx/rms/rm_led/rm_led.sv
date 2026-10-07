// -----------------------------------------------------------------------------
// rm_led.sv — rm_led: the "does the swap machinery work" Reconfigurable
// Module (RM) for the MPS3 DFX inner loop (IMPLEMENTATION_PLAN.md Phase
// 1.1/1.2 acceptance: "pr_verify clean across greybox<->LED-counter RM",
// then "two swaps, shell (LEDs/clocks) undisturbed").
//
// This RM is deliberately trivial: a free-running counter driving a handful
// of dut_gpio_o bits as a blinking pattern, plus a fixed rm_id constant.
// There is NO SoC inside it. Its only job is to prove — on real hardware,
// with a human-visible result — that partial reconfiguration, the
// clearing+partial artefact pipeline, and pr_verify all actually work
// end to end against the static shell, before rm_nanosoc's much larger real
// DUT is attempted. If a swap from rm_greybox to rm_led makes an LED start
// blinking (and swapping back makes it stop, cleanly, via the clearing
// bitstream), the DFX machinery is proven independent of anything nanosoc-
// specific.
//
// Ports are EXACTLY the RP side of docs/contracts/partition-pins.md v0.1,
// including the I4 board-GPIO passthrough group — see
// fpga/dfx/rms/README.md "RM authoring contract". This module is otherwise
// the same shape as fpga/dfx/rms/rm_greybox/rm_greybox.sv (read that file's
// header for the I19 greybox-generation-decision writeup) — every group
// except the counter/GPIO drive below is tied off exactly the same way.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rm_led #(
  parameter int NGPIO      = 16,  // partition-pins.md "Board-port / GPIO
                                    // passthrough -- I4": build parameter,
                                    // v0 default 16.
  parameter int COUNTER_W  = 26,  // Free-running counter width. Exact blink
                                    // rate is irrelevant to the DFX machinery
                                    // this RM exists to exercise -- picked so
                                    // the top bit blinks at a
                                    // human-observable rate (low single-digit
                                    // Hz) across the shell's expected DUT
                                    // clock range (spec Sec.5, tens of MHz),
                                    // not tied to any specific preset.
  parameter int BLINK_BITS = 4     // number of dut_gpio_o/oe bits this RM
                                    // actually drives (rest of the bus stays
                                    // released, oe=0, same posture as
                                    // rm_greybox, so the shell's host-mux
                                    // (shell-regmap.md GPIO.OWN) can still
                                    // reach the other bits).
) (
  // ===========================================================================
  // Clocks & resets -- shell -> RP (partition-pins.md "Clock/reset domain
  // rule" + "Clocks & resets" table). dut_clk/dut_resetn drive the blink
  // counter below; this module must NOT re-synchronize dut_resetn itself --
  // the shell already deasserts it synchronized-to-domain (contract
  // "Clock/reset domain rule"; same note as rp_nanosoc_wrapper.sv).
  // ===========================================================================
  input  logic dut_clk,
  input  logic dut_resetn,
  input  logic rp_resetn,
  input  logic dbg_resetn,

  // ===========================================================================
  // Processor debug -- internal JTAG / SWJ-DP (partition-pins.md "Processor
  // debug"). No TAP in this RM -- TCK/TMS/TDI ignored, TDO tied inert.
  // ===========================================================================
  input  logic jtag_tck,
  input  logic jtag_tms,
  input  logic jtag_tdi,
  output logic jtag_tdo,

  // ===========================================================================
  // RM debug -- BSCAN to an RM debug hub (partition-pins.md "RM debug",
  // 2026-10 ILA mint). This RM carries no hub: the 11 legs are ignored and
  // dbg_bscan_tdo is tied low.
  // ===========================================================================
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

  // ===========================================================================
  // Ethernet -- RMII + MDIO (partition-pins.md "Ethernet"). No MAC in this
  // RM -- TX/MDIO outputs tied inert (see HDPR-29 note: no IOB/OLOGIC
  // attributes belong in an RM regardless).
  // ===========================================================================
  input  logic       phy_rmii_ref_clk,
  input  logic       phy_rmii_crs_dv,
  input  logic [1:0] phy_rmii_rxd,
  output logic [1:0] phy_rmii_txd,
  output logic       phy_rmii_tx_en,
  output logic       mdc,
  output logic       mdio_o,
  output logic       mdio_oe,
  input  logic       mdio_i,

  // ===========================================================================
  // Console / trace -- AXI-Stream byte (partition-pins.md "Console / trace").
  // No UART DUT in this RM -- TX stream never valid, RX/TREADY tied inert.
  // ===========================================================================
  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,
  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready,
  output logic       swo,

  // ===========================================================================
  // Status / misc -- RP -> shell (partition-pins.md "Status / misc").
  // ===========================================================================
  output logic [31:0] rm_id,
  output logic         dut_lockup,
  output logic         irq_out,

  // ===========================================================================
  // Board-port / GPIO passthrough -- shell <-> RP (partition-pins.md
  // "Board-port / GPIO passthrough -- I4"). This is the ONLY group this RM
  // drives meaningfully: the blink pattern below.
  // ===========================================================================
  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // ===========================================================================
  // Flash / QSPI XiP -- RP <-> shell (partition-pins.md v0.2, the NON-CDC
  // source-synchronous exception). No QSPI controller in this RM: drive the
  // external flash to SAFE IDLE (deselected, clock low, lanes tri-stated).
  // ===========================================================================
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i
);

  // ===========================================================================
  // rm_id -- partition-pins.md "Status/misc: rm_id" / shell-regmap.md DFXCTL
  // RM_ID (0x44A1_0010). This is the RM-load-verify ground truth the shell
  // coordinator reads back after a partial load (spec Sec.7/13) to confirm
  // rm_led -- and only rm_led -- actually landed in the RP.
  //
  // rm_list.tcl assigns rm_led,rm_id = 0x0000001E. Note on the mnemonic: the
  // task brief that originated this RM spelled the constant "0x0000_00LE" --
  // but 'L' is not a legal hex digit (0-9/A-F), so a register genuinely
  // cannot hold a literal 'L' nibble. Reading it as leetspeak (L -> 1)
  // instead resolves to a real, legal 32-bit constant: 0x0000001E ("1E").
  // Flagged for A6 in case a different literal id was actually intended --
  // easy to change here and in rm_list.tcl together (rm_id is a single
  // source of truth per RM, assigned RM-design-time per rm_list.tcl's own
  // header comment).
  // ===========================================================================
  //
  // ENCODING v2 (docs/VERSIONING_PLAN.md §3.2), 2026-07-14:
  //     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
  //   design_id 0x001E (UNCHANGED — the "1E"/leetspeak-"LE" mnemonic above
  //   survives intact in the low half), version 1.0 => 0x0100001E.
  // Keep in lockstep with rm_list.tcl; check_rm_id_encoding.py gates it.
  localparam logic [31:0] RM_ID_LED = 32'h0100_001E;
  assign rm_id = RM_ID_LED;

  // ===========================================================================
  // Free-running blink counter -- the entire "DUT" in this RM. dut_clk is the
  // only clock available at the RP boundary (no clock generation inside an
  // RM, contract "Clock/reset domain rule") and dut_resetn arrives already
  // synchronized-for-deassert from the shell.
  // ===========================================================================
  logic [COUNTER_W-1:0] blink_counter;

  always_ff @(posedge dut_clk or negedge dut_resetn) begin
    if (!dut_resetn) begin
      blink_counter <= '0;
    end else begin
      blink_counter <= blink_counter + 1'b1;
    end
  end

  // Drive the top BLINK_BITS bits of the counter onto the low BLINK_BITS
  // GPIO bits (a slower-changing slice than the LSBs, so the pattern is
  // visibly a blink rather than a shimmer at typical DUT clock rates). Own
  // only those bits (dut_gpio_oe asserted for [BLINK_BITS-1:0]); release the
  // rest (oe=0), same posture as rm_greybox, so the shell's host-mux
  // (shell-regmap.md GPIO.OWN) can still reach them.
  always_comb begin
    dut_gpio_o                  = '0;
    dut_gpio_o[BLINK_BITS-1:0]  = blink_counter[COUNTER_W-1 -: BLINK_BITS];
    dut_gpio_oe                 = '0;
    dut_gpio_oe[BLINK_BITS-1:0] = {BLINK_BITS{1'b1}};
  end

  // ===========================================================================
  // Everything else -- tied to a safe, inert-but-legally-driven state,
  // identical to rm_greybox.sv. This RM has no SW-DP, no MAC, no UART DUT, no
  // lockup/IRQ source: only the blink pattern above is real.
  // ===========================================================================
  assign jtag_tdo       = 1'b0;
  assign dbg_bscan_tdo  = 1'b0;
  assign phy_rmii_txd   = 2'b00;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc            = 1'b0;
  assign mdio_o         = 1'b0;
  assign mdio_oe        = 1'b0;
  assign uart_tx_tdata  = 8'h00;
  assign uart_tx_tvalid = 1'b0;
  assign uart_rx_tready = 1'b0;
  assign swo            = 1'b0;
  assign dut_lockup     = 1'b0;
  assign irq_out        = 1'b0;

  // QSPI flash: safe idle (deselected, clock low, lanes tri-stated).
  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;      // active-low CS: 1 = deselected
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;   // all four lanes tri-stated (input)

endmodule
