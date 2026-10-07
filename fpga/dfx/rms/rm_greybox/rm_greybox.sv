// -----------------------------------------------------------------------------
// rm_greybox.sv — rm_greybox: the inert default Reconfigurable Module (RM)
// for the MPS3 DFX flow (IMPLEMENTATION_PLAN.md Phase 1.1, spec D14
// "greybox + NVM"). This is what the RP holds immediately after the shell's
// own bitstream loads, before the MicroBlaze streams a real default overlay
// in from NVM (spec Sec.8A.3) — and it is also the DFX *reference config*
// (config 1 in build_dfx.tcl / rm_list.tcl RM_ORDER) that
// static_routed_locked.dcp gets extracted from.
//
// Ports are EXACTLY the RP side of docs/contracts/partition-pins.md v0.1,
// including the I4 board-GPIO passthrough group — see
// fpga/dfx/rms/README.md "RM authoring contract" for the line-by-line
// derivation and fpga/rp/nanosoc/rp_nanosoc_wrapper.sv for the sibling this
// module's non-GPIO ports were checked against.
//
// Every output is tied to a safe, legally-driven, INERT constant: no clock
// domain logic, no state, nothing that could glitch or contend during a
// partial-reconfig boundary crossing. That is the whole point of a greybox —
// "legal fabric that does nothing" (rm_list.tcl comment).
// -----------------------------------------------------------------------------
//
// ============================================================================
// I19 — greybox-generation decision (hand tie-off wrapper vs Vivado
// `buffer_ports`): RESOLVED -> hand-authored tie-off wrapper (THIS file).
// ============================================================================
//
// rm_list.tcl's TODO(A2) posed the choice as:
//   (a) a hand-authored RTL wrapper (this style), synthesized OOC like any
//       other RM and linked into config 1 exactly like rm_led/rm_nanosoc; or
//   (b) Vivado's `update_design -cell $rp_cell -buffer_ports` on the
//       already-black-boxed+locked static checkpoint (the Z2 probe's
//       config_blank technique, dfx_impl_probe.tcl line ~117).
//
// Decision: (a), a hand-authored wrapper. Reasons this beats `buffer_ports`
// for THIS flow specifically (not a general verdict on the technique):
//
//   1. Ordering conflict with the "greybox is the reference config" role.
//      `buffer_ports` is a *post-extraction* operation — it runs AFTER
//      `update_design -cell $rp_cell -black_box` + `lock_design -level
//      routing` have already produced static_routed_locked.dcp (see the Z2
//      probe: buffer_ports happens on the SAME checkpoint object right after
//      the black_box call, i.e. it presupposes the static has already been
//      locked from a DIFFERENT reference RM). But rm_list.tcl / README.md
//      assign greybox itself as config 1, the reference RM the static gets
//      extracted FROM (not a second config built after). Using buffer_ports
//      for greybox would mean either (i) picking a different RM as the true
//      DFX reference (extra indirection, and that RM would need to exist and
//      route successfully first), or (ii) reordering build_dfx.tcl's loop
//      just for this one RM — both add real complexity to route around a
//      technique that was designed for the OTHER role (the "blank" partner
//      configs after the reference, not the reference itself).
//   2. Uniformity of the build loop. `build_dfx.tcl`'s `build_config` proc
//      treats every RM identically: link static + RM's own OOC synth
//      checkpoint, floorplan, implement. A hand-authored greybox needs no
//      special-casing in that loop — it is synthesized OOC exactly like
//      rm_led/rm_nanosoc/rm_eth_ss and just happens to be tiny. A
//      buffer_ports greybox would need its own branch in build_dfx.tcl
//      (skip synth, skip place/route-from-scratch, call buffer_ports+
//      place+route instead) — more special-case code for less benefit.
//   3. rm_id is real, driven RTL, not a floating buffer. The greybox must
//      drive `rm_id = 0x00000000` (rm_list.tcl) so the shell's RM-load-verify
//      (DFXCTL.RM_ID, shell-regmap.md) can positively confirm "greybox is
//      loaded", not merely "something legal is loaded". `buffer_ports`
//      produces floating/undriven-equivalent buffers on every partition pin
//      with no way to assert a specific constant on `rm_id` — it is built for
//      "make the boundary electrically legal", not "drive a real status
//      value". A hand wrapper trivially satisfies both.
//   4. Pblock-sizing note (D7, OPEN_ISSUES I17): a `buffer_ports` config
//      contains almost no logic, so its utilisation is not representative of
//      "the smallest real RM" for Pblock-sizing purposes anyway — a
//      hand-authored greybox costs the same near-zero resource either way,
//      so there is no sizing argument in buffer_ports' favour either.
//
// This does NOT rule out `buffer_ports` elsewhere in this flow — it is still
// the right tool if a later RM ever needs a true "electrically legal, no
// synthesizable source at all" stand-in (e.g. a fast placeholder while an RM
// is mid-development). For the one RM that must be both the DFX reference
// config and a positively-identifiable boot-time default, a real (if tiny)
// RTL wrapper is the simpler, more uniform choice. Flagged for A6 review —
// revisit if `build_dfx.tcl`'s config-1/reference-RM role changes.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rm_greybox #(
  parameter int NGPIO = 16   // partition-pins.md "Board-port / GPIO
                             // passthrough -- I4": build parameter, v0
                             // default 16 (matches shell-regmap.md GPIO
                             // block width).
) (
  // ===========================================================================
  // Clocks & resets -- shell -> RP (partition-pins.md "Clock/reset domain
  // rule" + "Clocks & resets" table). No clock generation inside this RM;
  // dut_clk/dut_resetn/rp_resetn/dbg_resetn are all accepted but unused --
  // a greybox has no sequential state to reset.
  // ===========================================================================
  input  logic dut_clk,
  input  logic dut_resetn,
  input  logic rp_resetn,
  input  logic dbg_resetn,

  // ===========================================================================
  // Processor debug -- internal JTAG / SWJ-DP (partition-pins.md "Processor
  // debug"). No TAP present in a greybox -- TCK/TMS/TDI ignored, TDO tied inert.
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
  // Ethernet -- RMII + MDIO (partition-pins.md "Ethernet"). No MAC present.
  // TX/MDIO outputs tied inert; per the IOB packing note these leave this RM
  // as plain fabric signals regardless (no OLOGIC/IOB attributes belong in
  // an RM -- HDPR-29, contract "IOB packing note").
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
  // No UART DUT present -- TX stream never valid (tvalid=0 means tdata is
  // don't-care to any AXI-Stream-compliant sink), RX/TREADY tied inert.
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
  // rm_id is the ONE signal this RM must drive correctly and permanently --
  // see rm_id assignment below.
  // ===========================================================================
  output logic [31:0] rm_id,
  output logic         dut_lockup,
  output logic         irq_out,

  // ===========================================================================
  // Board-port / GPIO passthrough -- shell <-> RP (partition-pins.md
  // "Board-port / GPIO passthrough -- I4"). Greybox owns nothing on the
  // board: drive value released, output-enable off on every bit, so the
  // shell's host-mux (shell-regmap.md GPIO.OWN) is free to drive the pads
  // directly while the greybox is resident.
  // ===========================================================================
  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // ===========================================================================
  // Flash / QSPI XiP -- RP <-> shell (partition-pins.md v0.2 "Flash / QSPI
  // XiP", the NON-CDC source-synchronous exception). No QSPI controller in a
  // greybox: drive the external flash to SAFE IDLE -- deselected (csn=1), clock
  // low, every data lane tri-stated (io_oe=0) -- so a resident greybox cannot
  // disturb the SST26 pads. io_i is accepted and unused.
  // ===========================================================================
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i
);

  // ===========================================================================
  // rm_id -- partition-pins.md "Status/misc: rm_id" / shell-regmap.md DFXCTL
  // RM_ID (0x44A1_0010). rm_list.tcl assigns rm_greybox,rm_id = 0x00000000 --
  // the coordinator's RM-load-verify (spec Sec.7/13) reads this back after
  // EVERY partial load, including the implicit "load" that is just the shell
  // itself powering on with the greybox baked in (spec D14). A constant
  // 0x00000000 is a deliberate, permanent, unambiguous "nothing real is
  // loaded" signal -- it must never collide with a real RM's assigned id
  // (rm_list.tcl keeps 0x00000000 reserved for exactly this module).
  //
  // ENCODING v2 CARVE-OUT (docs/VERSIONING_PLAN.md §3.2, 2026-07-14).
  // Every other RM now carries a design VERSION in the top half of rm_id --
  //     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
  // -- and every real design starts at v1.0 (e.g. nanosoc => 0x01000001).
  // The greybox is held at design 0x0000, version 0.0, so its rm_id stays
  // EXACTLY 0x00000000. This is not an oversight, it is the point:
  //   * the DFX decoupler clamps rm_id to DECOUPLED_VALUE 0x0 while DECOUPLE
  //     is asserted, so a non-zero greybox id would be indistinguishable from
  //     a decoupled RP;
  //   * firmware and pyverify (_rm_id_indicates_loaded()) both read an
  //     all-zero rm_id as "no RM loaded";
  //   * an inert tie-off is not a versioned design, so v0.0 is also honest.
  // DO NOT "promote" the greybox to v1.0 -- that would make it 0x01000000 and
  // silently break both conventions above. Hardware would tolerate it
  // (rm_id_valid is a gate + stability detector, NOT a zero-check); the
  // software contract would not.
  // ===========================================================================
  localparam logic [31:0] RM_ID_GREYBOX = 32'h0000_0000;  // design 0x0000 @ v0.0
  assign rm_id = RM_ID_GREYBOX;

  // ===========================================================================
  // Everything else -- tied to a safe, inert-but-legally-driven state. No
  // sequential logic anywhere in this module: a greybox has nothing to hold
  // in reset, and no clock-domain concern of its own.
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
  assign dut_gpio_o     = '0;
  assign dut_gpio_oe    = '0;

  // QSPI flash: safe idle (deselected, clock low, lanes tri-stated).
  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;      // active-low CS: 1 = deselected
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;   // all four lanes tri-stated (input)

endmodule
