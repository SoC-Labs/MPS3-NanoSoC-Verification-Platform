//----------------------------------------------------------------------------
// rm_socscope -- the SoCScope trace plane as a reconfigurable module.
//
// MPS3_BRINGUP_TESTS.md B1: "`swo` is currently `assign swo = 1'b0;` ... a fully
// plumbed, decoupler-clamped, FIFO'd, TCP-relayed channel whose RP end is dead.
// Drive it from socscope_egress_serial instead." This RM is that, and it costs
// ZERO boundary change and ZERO static re-key -- the `swo` port already exists in
// the frozen RP boundary, so this is a partial build against the locked static
// and every fielded overlay keeps working. WHICH static is not this file's to
// say: an RM does not know its static (none does; it is a CRC of the locked
// shell DCP, stamped at `make overlays`). fpga/dfx/overlay/socscope/manifest.json
// records what this partial is keyed to, and docs/FIELDED_SHELL.md says what is
// on the board. A literal here was wrong within a week of being written.
//
// WHY A NEW RM RATHER THAN EDITING GREYBOX. rm_greybox's module header states it
// has "no sequential logic anywhere ... a greybox has nothing to hold in reset,
// and no clock-domain concern of its own." That property is the whole point of a
// greybox and it is what makes it the DFX reference config. Putting a clocked
// trace engine inside it would quietly destroy it.
//
// WHY NOT A REAL DUT YET. B1 proves the TRANSPORT: probe, record former, ring,
// domain crossing, framer, COBS+CRC, egress, and the host's decode of all of it,
// against traffic whose every field is predictable. Attaching the probe to the
// nanoSoC's live AHB is B2, and its control is "the DUT behaves identically with
// the probe enabled and disabled" -- a different measurement that is only worth
// making once the transport is known good. Debugging a transport fault through a
// live SoC is how a week disappears.
//
// WHAT LANDS ON THE WIRE. socscope_selftest configures the block over its own
// AHB-Lite CSR port (so socscope_csr_ahb is genuinely exercised on silicon, not
// bypassed) and then drives synthetic AHB traffic past the probe, some of it
// deliberately outside the configured filter. The host recomputes what it should
// have received from the formula in socscope_selftest.sv and requires exactly
// that -- see hw/bench/check_selftest.py in the SoCScope tree, which is the SAME
// function used for the Icarus bench and the board capture.
//
// THE DIVISOR MUST MATCH THE SHELL. Bit period is `divisor+1` dut_clk cycles on
// both sides. The shell's swo_uart_rx is programmed by
// firmware/uart_over_eth/uart_over_eth.c with UART_OVER_ETH_SWO_DIVISOR = 24, so
// this RM transmits at 24. Mis-set by one and every byte is garbage -- which is
// exactly B1's negative control, and the reason the framing carries a CRC: without
// it the host cannot tell a lost byte from a good one.
//
// TWO CLOCKS NOW -- THE EGRESS IS OFF THE CLOCK THAT STOPS (Stage C, C2).
// B1 tied trace_clk to dut_clk and said the split earns its keep when something
// STOPS the DUT clock. Stage C is that something, so the tie is gone:
// `trace_clk` is `phy_rmii_ref_clk`, the shell's free-running 50 MHz RMII
// reference (partition-pins.md "Ethernet"), and the crossings socscope_trace_top
// already instantiates now cross a real boundary instead of degenerating.
//
// TRACE_CLK_FREE=0 restores the single-clock tie EXACTLY -- it is the negative
// control for C2 ("run it once without the domain split and confirm it fails
// with framing errors") and the back-out that reproduces the fielded build.
//
// WHAT THIS BUY DOES *NOT* INCLUDE, AND IT MATTERS ON A BOARD. The shell's
// receiver is still in the DUT domain: `fpga/shell/ip/uart_bridge/swo_uart_rx.sv`
// 2-FF-synchronises `swo_i` into `clk_i` = dut_clk, and its bit period is
// `divisor+1` DUT clocks while this RM's is now `divisor+1` TRACE clocks. The two
// agree only while dut_clk is at its 50 MHz default. `dut_clk` is
// DRP-RECONFIGURABLE (partition-pins.md), so a host `set_clk` to 25 MHz -- which
// has been done on this board, HW-008 -- used to move both ends together and now
// moves only the receiver. Moving swo_uart_rx onto the free-running clock is the
// SHELL half of this change and it is NOT in this lane; until it lands, do not
// retune dut_clk while capturing. That is a real regression and it is stated
// rather than discovered.
//
// FREEZE: THE ENGINE IS NOT HERE, AND THAT IS THE DECISION, NOT AN OMISSION.
// HW-013 says a clock buffer cannot be placed in this RP at all -- the BUFGCE
// sites serving the RP's clock regions are in the column carrying the shell's own
// I/O pads -- so the gate the enable drives MUST live static-side. The replan took
// the further step and put the whole engine static-side (the shell owns the BUFGCE
// and the control surface, driven from its regmap), which is why FREEZE_IN_RM
// DEFAULTS TO 0: this RM's Stage-C obligation is to SURVIVE a stopped dut_clk,
// and the trace-domain split above is the whole of that obligation.
//
// FREEZE_IN_RM=1 builds the other HW-013 option -- engine and control surface
// inside the RM, on the free-running trace clock, enable exported across the
// boundary -- because the choice between them was recorded as open and a
// parameter that is benched is a cheaper way to hold it open than a branch.
// Its command path is the DIP switches (`dut_gpio_i[10:8]`): `dut_gpio_i` is
// FULLY ALLOCATED (dut-display-tunnel.md §6 -- [15:8] USER_SW, [7:0] a loopback
// of the DUT's own LED drive), so there is no host-driven path in and the switches
// are the only one that exists. The watchdog in socscope_freeze_ctl is what makes
// that safe rather than the path back in.
//----------------------------------------------------------------------------

`timescale 1ns/1ps
`include "socscope_records.vh"

// The RP boundary is FROZEN at 47 ports, and this RM legitimately uses a handful
// of them. Every unused input is an unused input on purpose -- there is no TAP, no
// MAC, no console and no flash in a trace-plane RM -- and the boundary cannot be
// trimmed without a static re-key, which is the whole reason this build is cheap.
/* verilator lint_off UNUSED */

module rm_socscope #(
  parameter int NGPIO = 16,  // partition-pins.md "Board-port / GPIO
                             // passthrough -- I4": build parameter, v0
                             // default 16 (matches shell-regmap.md GPIO
                             // block width).

  // 1 = egress on `phy_rmii_ref_clk`, the shell's free-running 50 MHz RMII
  // reference, so bytes captured before a freeze can still leave during one.
  // 0 = the B1 single-clock tie to dut_clk, bit-for-bit: the C2 negative
  // control, and the back-out if the shell's receiver cannot move with it.
  parameter int TRACE_CLK_FREE = 1,

  // 1 = the HW-013 "enable crosses on a partition pin" option: freeze engine
  // and control surface inside this RM, on the free-running trace clock, the
  // enable exported on dut_gpio_o[4] in the FAIL-SAFE sense (see below).
  // 0 = the replan's decision: the engine is static-side and this RM carries
  // no freeze logic at all -- only the ability to survive a stopped dut_clk.
  parameter int FREEZE_IN_RM = 0,

  // Cycles a switch-issued STEP advances the DUT by. A 32-bit N cannot cross
  // three DIP switches, so the count is built in; the exactness argument (C1:
  // exactly 1, exactly 1,000,000) is a property of socscope_freeze and is
  // benched by sweeping THIS parameter, not by a runtime field that does not
  // fit through the boundary that exists.
  parameter int STEP_N = 1,

  // Trace clocks a freeze may outlast nothing: socscope_freeze_ctl's watchdog.
  // ~1.3 s at 50 MHz, and it is the WHOLE safety argument for a state plane whose
  // only command path is three DIP switches -- so it is a parameter, because a
  // safety property that can only be exercised by waiting 1.3 seconds of
  // simulated time is a safety property that is never exercised.
  parameter int FREEZE_WDOG_CYCLES = 32'd64_000_000,

  // --- the self-test fabric's two shipped numbers, hoisted so a bench can move
  // --- them. Both DEFAULT to the values the board runs and neither should be
  // --- changed for a build that ships.
  //
  // EGRESS_DIVISOR must equal the shell's UART_OVER_ETH_SWO_DIVISOR
  // (firmware/uart_over_eth/uart_over_eth.c). Off by one and every byte is
  // garbage -- B1's negative control.
  parameter int EGRESS_DIVISOR = 24,

  // GAP between records, set by the RECEIVER: HW-007's 16-byte polled FIFO in the
  // shell holds exactly one frame, so the shipped 1,000,000 dut_clk cycles (20 ms)
  // is the shell's limit expressed as silence. At that pacing ONE record costs
  // 20 ms of simulated time, which is why this was unbenchable end-to-end before
  // it was a parameter.
  parameter int SELFTEST_GAP_CYCLES = 1000000
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
  // rm_id -- the ONE signal this RM must drive correctly and permanently.
  // design 0x0006 @ v1.0 (rm_list.tcl). Read back at DFXCTL.RM_ID 0x44A1_0010
  // after a load to confirm the right RM landed.
  // ===========================================================================
  localparam logic [31:0] RM_ID_SOCSCOPE = 32'h0100_0006;
  assign rm_id = RM_ID_SOCSCOPE;

  // ===========================================================================
  // THE TRACE DOMAIN -- the clock that does not stop.
  //
  // `phy_rmii_ref_clk` is an ordinary RP input in the frozen boundary and is
  // already routed to this RP, so putting the egress on it costs NO boundary
  // change, NO static re-key and no new partition pin. The selection is a
  // PARAMETER, not a mux: a fabric clock mux inside an RP with no clock
  // resources (HW-013) would be routed on local interconnect, and both arms of
  // this generate collapse to a direct connection at elaboration.
  //
  // The reset is synchronised rather than carried across: `dut_resetn` is a
  // level from the shell in the DUT domain, and releasing it asynchronously into
  // a 50 MHz domain is the classic way to bring half a FIFO out of reset a cycle
  // before the other half. Assert asynchronously (so a reset still works with the
  // trace clock absent), release synchronously.
  // ===========================================================================
  wire trace_clk;
  wire trace_resetn;

  generate if (TRACE_CLK_FREE != 0) begin : g_trace_free
    assign trace_clk = phy_rmii_ref_clk;

    reg [1:0] tr_rst_sync;
    always_ff @(posedge phy_rmii_ref_clk or negedge dut_resetn) begin
      if (!dut_resetn) tr_rst_sync <= 2'b00;
      else             tr_rst_sync <= {tr_rst_sync[0], 1'b1};
    end
    assign trace_resetn = tr_rst_sync[1];
  end else begin : g_trace_tied
    // B1, unchanged: one clock, and the crossings degenerate to two cycles of
    // latency. This arm exists to be RUN, as C2's control, not to be read.
    assign trace_clk    = dut_clk;
    assign trace_resetn = dut_resetn;
  end endgenerate

  // ===========================================================================
  // The instrument.
  // ===========================================================================
  wire        st_csr_hsel, st_csr_hwrite;
  wire [31:0] st_csr_haddr, st_csr_hwdata;
  wire [1:0]  st_csr_htrans;
  wire [2:0]  st_csr_hsize;
  wire [31:0] st_csr_hrdata;
  wire        st_csr_hreadyout, st_csr_hresp;

  wire [31:0] st_obs_haddr, st_obs_hwdata, st_obs_hrdata;
  wire [1:0]  st_obs_htrans;
  wire        st_obs_hwrite, st_obs_hready, st_obs_hresp;
  wire [2:0]  st_obs_hsize;
  wire        st_configured;

  socscope_selftest #(
      .DIVISOR    (EGRESS_DIVISOR),     // MUST equal the shell's UART_OVER_ETH_SWO_DIVISOR
      .FILT_BASE  (32'h2000_0000),
      .FILT_MASK  (32'hFFFF_0000),
      .N_XFER     (8),
      .GAP_CYCLES (SELFTEST_GAP_CYCLES)
  ) u_selftest (
      .clk(dut_clk), .resetn(dut_resetn),
      .csr_hsel(st_csr_hsel), .csr_haddr(st_csr_haddr), .csr_htrans(st_csr_htrans),
      .csr_hwrite(st_csr_hwrite), .csr_hsize(st_csr_hsize), .csr_hwdata(st_csr_hwdata),
      .csr_hreadyout(st_csr_hreadyout),
      .obs_haddr(st_obs_haddr), .obs_htrans(st_obs_htrans), .obs_hwrite(st_obs_hwrite),
      .obs_hsize(st_obs_hsize), .obs_hwdata(st_obs_hwdata), .obs_hrdata(st_obs_hrdata),
      .obs_hready(st_obs_hready), .obs_hresp(st_obs_hresp),
      .configured(st_configured)
  );

  // ===========================================================================
  // THE FREEZE COMMAND PLANE (FREEZE_IN_RM=1 only).
  //
  // The commands are generated in the TRACE domain, not the DUT domain, even
  // though socscope_freeze_ctl declares its `cmd_stb` port as arriving from the
  // DUT side and re-synchronises it. HW-009 is the reason: a command generator
  // on the clock it stops can issue exactly one freeze, for ever. Feeding
  // freeze_ctl a same-domain pulse costs two cycles of latency in its own
  // synchroniser and buys a RELEASE that still works while the DUT is stopped.
  // The watchdog remains the safety argument; this is the second belt.
  //
  // Encoding, on the DIP switches (dut_gpio_i[15:8] = USER_SW):
  //     dut_gpio_i[9:8]  cmd   0 run, 1 freeze, 2 step (socscope_freeze_ctl)
  //     dut_gpio_i[10]   stb   RISING edge issues the command in [9:8]
  // ===========================================================================
  wire       fz_cmd_stb;
  wire [1:0] fz_cmd;

  generate if (FREEZE_IN_RM != 0) begin : g_fz_cmd
    reg [2:0] sw_s1, sw_s2;
    reg       stb_q;
    always_ff @(posedge trace_clk or negedge trace_resetn) begin
      if (!trace_resetn) begin
        sw_s1 <= 3'd0; sw_s2 <= 3'd0; stb_q <= 1'b0;
      end else begin
        sw_s1 <= dut_gpio_i[10:8];
        sw_s2 <= sw_s1;
        stb_q <= sw_s2[2];
      end
    end
    assign fz_cmd_stb = sw_s2[2] && !stb_q;   // rising edge of the strobe switch
    assign fz_cmd     = sw_s2[1:0];
  end else begin : g_no_fz_cmd
    assign fz_cmd_stb = 1'b0;
    assign fz_cmd     = 2'd0;
  end endgenerate

  // Status out of the state plane. Driven whatever FREEZE_IN_RM is -- with the
  // engine absent socscope_trace_top ties these off itself (clk_en HIGH, so a
  // build with no freeze plane does not look like one whose clock is stopped),
  // and reading the tie-off through the same wires is how the bench proves it.
  wire fz_dut_clk_en, fz_frozen, fz_stepping, fz_wdog_fired;

  socscope_trace_top #(
      .DEPTH    (64),
      .CSR_BASE (32'h0000_0000),
      // HW-010: at TICKDIV=1 every stamp in the passing board capture SATURATED --
      // the receiver forces ~25 ms between frames and a 16-bit delta spans 1.31 ms.
      // 64 gives 84 ms of range at 1.28 us resolution, and the stream announces it.
      .TICKDIV  (64),
      .FREEZE   (FREEZE_IN_RM),
      .WDOG_CYCLES (FREEZE_WDOG_CYCLES)
  ) u_socscope (
      .clk(dut_clk), .resetn(dut_resetn),
      .trace_clk(trace_clk), .trace_resetn(trace_resetn),   // free-running -- see header
      .obs_haddr(st_obs_haddr), .obs_htrans(st_obs_htrans), .obs_hwrite(st_obs_hwrite),
      .obs_hsize(st_obs_hsize), .obs_hwdata(st_obs_hwdata), .obs_hrdata(st_obs_hrdata),
      .obs_hready(st_obs_hready), .obs_hresp(st_obs_hresp),
      .csr_hsel(st_csr_hsel), .csr_haddr(st_csr_haddr), .csr_htrans(st_csr_htrans),
      .csr_hwrite(st_csr_hwrite), .csr_hsize(st_csr_hsize), .csr_hwdata(st_csr_hwdata),
      .csr_hrdata(st_csr_hrdata), .csr_hreadyout(st_csr_hreadyout), .csr_hresp(st_csr_hresp),
      .txd(swo),

      // ---- the state plane -------------------------------------------------
      // The engine is instantiated ONLY at FREEZE_IN_RM=1 (the generate inside
      // socscope_trace_top), and even then it gates nothing HERE: HW-013 forbids a
      // clock buffer in this RP, so `dut_clk_en` leaves the RP and the gate it
      // drives is the static's. The commands come from the trace domain (above),
      // never from the DUT domain, or the first freeze would be the last.
      .freeze_cmd_stb(fz_cmd_stb), .freeze_cmd(fz_cmd), .freeze_cmd_n(32'(STEP_N)),
      // `ext_run` is the override the shell would drive if the engine were reachable
      // from it. Nothing in this boundary reaches it, so it is tied LOW and the
      // watchdog carries the safety argument alone -- stated, not assumed.
      .freeze_ext_run(1'b0),
      // step_rem/delivered/wdog_rem are 32-bit counters with nowhere to go: the
      // boundary is frozen and `delivered` -- the C1 acceptance measurement -- is
      // measured on the GATED CLOCK by whoever owns the gate, which is the point of
      // measuring it there rather than trusting the engine's own count.
      /* verilator lint_off PINCONNECTEMPTY */
      .dut_clk_en(fz_dut_clk_en), .frozen(fz_frozen), .stepping(fz_stepping),
      .wdog_fired(fz_wdog_fired),
      .step_rem(), .delivered(), .wdog_rem()
      /* verilator lint_on PINCONNECTEMPTY */
  );

  // ===========================================================================
  // Everything else: inert, exactly as rm_greybox drives it. This RM owns the
  // trace wire and nothing else -- no MAC, no TAP, no console, no flash.
  //
  // dut_gpio_o[0] shows `configured`, and it is worth the one bit: on a board a
  // dead `swo` cannot distinguish "the CSR never answered" from "configured but
  // not transmitting", and those two have completely different first moves.
  //
  // At FREEZE_IN_RM=1 four more bits go out, and bit 4 is the load-bearing one:
  //
  //     [1] frozen  [2] stepping  [3] wdog_fired  [4] dut_clk_HOLD
  //
  // HOLD, NOT ENABLE, AND THE POLARITY IS THE WHOLE SAFETY ARGUMENT. The DFX
  // decoupler clamps dut_gpio_o to 0 during a swap (dut-display-tunnel.md
  // "The decoupler clamps"). Exporting the enable directly would clamp it to
  // "stopped" and freeze the DUT's clock for the duration of every partial
  // reconfiguration -- including the one that would replace this RM. Exporting
  // its complement makes the clamped value mean RUN, so the fail-safe state of
  // the wire and the fail-safe state of the board are the same state.
  //
  // At FREEZE_IN_RM=0 only bit 0 is driven, and dut_gpio_o/oe are bit-for-bit
  // what the fielded overlay drives. A back-out is a parameter, not a diff.
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
  assign dut_lockup     = 1'b0;
  assign irq_out        = 1'b0;
  // dut_gpio_o/oe[15:8] stay 0: they are the CLCD display tunnel's data byte and
  // control byte (dut-display-tunnel.md), and an RM with no display drives 0.
  wire [4:0] gpio_state_plane = {~fz_dut_clk_en,   // [4] HOLD -- clamped 0 = run
                                 fz_wdog_fired,    // [3]
                                 fz_stepping,      // [2]
                                 fz_frozen,        // [1]
                                 st_configured};   // [0]

  generate if (FREEZE_IN_RM != 0) begin : g_gpio_fz
    assign dut_gpio_o  = {{(NGPIO-5){1'b0}}, gpio_state_plane};
    assign dut_gpio_oe = {{(NGPIO-5){1'b0}}, 5'b11111};
  end else begin : g_gpio_plain
    assign dut_gpio_o  = {{(NGPIO-1){1'b0}}, st_configured};
    assign dut_gpio_oe = {{(NGPIO-1){1'b0}}, 1'b1};
  end endgenerate

  // QSPI flash: safe idle (deselected, clock low, lanes tri-stated).
  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;

endmodule
/* verilator lint_on UNUSED */
