// -----------------------------------------------------------------------------
// rp_clcd_demo_wrapper.sv -- RM `clcd_demo`: THE PROOF THAT A DUT CAN DRIVE THE
// ON-BOARD PANEL.
//
// The CLCD KVM, the display tunnel and the DUT-side socket are all FIELDED on
// mint 0xA8C1C535 -- and no DUT has ever been observed driving the glass. Every
// other candidate for that proof needs a CPU to boot, a firmware image to be
// right and an accelerator to be enabled. This RM needs none of them: it is
// pure RTL, it starts on reset, and its whole job is to put a photographable
// test card with a live frame counter on the panel.
//
//   what it draws, and how to read it   fpga/rp/clcd_demo/clcd_demo_gen.sv
//   what it proves, and how to run it   fpga/rp/clcd_demo/README.md
//   the bench                           tests/clcd_demo/
//   the wire encoding it speaks         docs/contracts/dut-display-tunnel.md
//
// It is a PROOF, not a demo application: no CPU, no bus, no framebuffer, ~a few
// hundred LUT. The interesting DUT-side display work is the student socket in
// fpga/rp/nanosoc_exp/ -- this RM exists so that socket is built on top of a
// path somebody has actually seen work.
//
// Everything below this line down to the DUT instance is the template's
// (fpga/rp/_template/), unchanged.
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

module rp_clcd_demo_wrapper #(
  // partition-pins.md "Board-port / GPIO passthrough -- I4": a build parameter,
  // v0 value 16. pin_check.py REQUIRES the literal `parameter int NGPIO = 16`
  // to be present and to be 16 -- keep the declaration, do not inline the width.
  parameter int NGPIO = 16,

  // dut_clk frequency, in Hz -- the shipped DRP MMCM preset (docs/contracts/
  // dut-display-tunnel.md section 5: "50 MHz shipped", and its 100 MHz hard
  // ceiling while the DUT owns the panel). It reaches clcd_demo_gen, where the
  // firmware init table's MILLISECOND delays are counted against it. A bench
  // overrides it (VCS `-pvalue+rp_clcd_demo_wrapper.DUT_CLK_HZ=...`) to shrink
  // those delays; nothing else in this wrapper depends on it.
  //
  // If the board is re-clocked through the MMCM DRP at 0x44AB_0000, this
  // parameter is the ONE thing that has to follow, and the only symptom of not
  // following it is panel init delays that are too short or too long.
  parameter int DUT_CLK_HZ = 50_000_000,

  // ---------------------------------------------------------------------------
  // The rest of clcd_demo_gen's build parameters, surfaced HERE so a bench can
  // reach them with a plain `-pvalue+rp_clcd_demo_wrapper.<P>=<v>` on the
  // elaboration line (cocotb's TOPLEVEL is this module, so a sub-module's
  // parameters are not addressable). Every default below is the SHIPPING value:
  // an RM built with no overrides is the RM that goes on the board.
  //
  //   PIX_W / PIX_H  0 = take the panel's real geometry from the firmware init
  //                  table's window END registers. A bench shrinks the frame so
  //                  a full repaint is a few thousand bytes instead of 153,600.
  //   CS_SETUP / WR_LO / WR_HI   8080 phase widths in dut_clk cycles.
  //                  8 is the FLOOR in docs/contracts/dut-display-tunnel.md
  //                  section 5, and the contract's own reference default. NOTE
  //                  that clcd_core's OWN defaults are 2/4/4 -- correct for the
  //                  100 MHz shell clock it was written for and BELOW the floor
  //                  at 50 MHz, which is why they are overridden here rather
  //                  than inherited. tests/clcd_demo drives them sub-floor on
  //                  purpose, as the control that proves the check can fail.
  //   REQ_AFTER_MS   when `req` rises after reset (see clcd_demo_gen.sv).
  // ---------------------------------------------------------------------------
  parameter int PIX_W        = 0,
  parameter int PIX_H        = 0,
  parameter int CS_SETUP     = 8,
  parameter int WR_LO        = 8,
  parameter int WR_HI        = 8,
  parameter int REQ_AFTER_MS = 100
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
  // v0.1: registered in fpga/dfx/rm_list.tcl as design_id 0x0007 @ 0.1.0 =>
  // rm_id 0x00010007, the value this directory's README PASS criteria expect
  // from `pyverify ping`. The template's 1.0 default was a claim of a silicon
  // run this RM has never had; bump to 1.0 in the commit that records one.
  // check_rm_id_encoding.py composes these three the same way and compares.
  localparam logic [7:0]  RM_VER_MAJOR = 8'd0;
  localparam logic [7:0]  RM_VER_MINOR = 8'd1;
  localparam logic [15:0] RM_DESIGN_ID = 16'h0007;   // allocated in rm_list.tcl
  localparam logic [31:0] RM_ID_CLCD_DEMO =
      {RM_VER_MAJOR, RM_VER_MINOR, RM_DESIGN_ID};

  assign rm_id = RM_ID_CLCD_DEMO;

  // ===========================================================================
  // THE DUT: clcd_demo_gen -- a CPU-less test-card generator on the display
  // tunnel. Read clcd_demo_gen.sv's header for what it draws and why; read
  // README.md in this directory for what it PROVES and how to run the proof.
  //
  // Reset: dut_resetn only. It is already deasserted synchronised to dut_clk by
  // the shell ("Clock/reset domain rule", docs/contracts/partition-pins.md), and
  // the template's rule 1 forbids generating anything here. rp_resetn is NOT
  // ANDed in: the shell holds it through a swap and the decoupler already clamps
  // the tunnel to 0x0 (= all strobes idle) for that whole window, so folding it
  // in would buy nothing and would add a second reset to reason about.
  // ===========================================================================
  logic [7:0]  lcd_pd;
  logic        lcd_cs, lcd_wr, lcd_rs, lcd_busy, lcd_req;
  logic [15:0] frame_count;

  clcd_demo_gen #(
    .CLK_HZ       (DUT_CLK_HZ),
    .PIX_W        (PIX_W),
    .PIX_H        (PIX_H),
    .CS_SETUP     (CS_SETUP),
    .WR_LO        (WR_LO),
    .WR_HI        (WR_HI),
    .REQ_AFTER_MS (REQ_AFTER_MS)
  ) u_demo (
    .clk         (dut_clk),
    .rst_n       (dut_resetn),
    // USER_SW[0] reaches the RP as dut_gpio_i[8] (shell_top.sv:463 drives
    // board_gpio_pad_i = { USER_SW, led_drive }). It FREEZES the frame counter
    // without stopping the repaint -- the on-board negative control for
    // "is that counter really being drawn by the DUT?".
    .freeze      (dut_gpio_i[8]),
    .lcd_pd      (lcd_pd),
    .lcd_cs      (lcd_cs),
    .lcd_wr      (lcd_wr),
    .lcd_rs      (lcd_rs),
    .lcd_busy    (lcd_busy),
    .lcd_req     (lcd_req),
    .frame_count (frame_count)
  );

  // ===========================================================================
  // The display tunnel. Bit map is FROZEN in
  // docs/contracts/dut-display-tunnel.md section 2 -- do NOT restate it without
  // citing that file. Strobes are carried ACTIVE-HIGH (section 3, a SAFETY
  // property: the decoupler clamps this vector to 0x0 for the whole of every
  // partial reconfiguration, and active-high makes 0x0 decode to "all strobes
  // idle, nothing requested, nothing in flight" by construction). The RESERVED
  // bits -- rd, pd_oe, spare -- are driven 0, which is the same value the clamp
  // presents, so a decoupled RP and this RM look identical to the KVM.
  //
  // The shape below is lifted from the fielded fpga/rp/nanosoc/
  // rp_nanosoc_wrapper.sv:307-317 mux, minus the GPIO alternative: this RM has
  // no CPU and no Tier-0 bit-bang path, so it always drives the tunnel.
  //
  //   dut_gpio_o[15:8]  = PD[7:0]
  //   dut_gpio_oe[15:8] = {spare, req, busy, pd_oe, rd, rs, wr, cs}  (MSB..LSB)
  //
  // The LOW byte of both vectors is untouched by the tunnel and is still
  // board_gpio's LED path (shell_top.sv:461-462, USER_nLED = ~(o & oe)). It
  // mirrors the low 8 bits of the same frame counter the panel shows, so the
  // board reports liveness on the LEDs even if the panel handover never
  // happened -- which is exactly the discrimination the proof needs. It works
  // because board_gpio's OWN register resets to 0 = "the DUT owns every bit"
  // (board_gpio.sv:205,250); a harness that claims the LEDs for itself takes
  // this mirror away and only the panel remains.
  // ===========================================================================
  assign dut_gpio_o  = { lcd_pd, frame_count[7:0] };
  assign dut_gpio_oe = { 1'b0,      // [15] spare  RESERVED = 0
                         lcd_req,   // [14] req
                         lcd_busy,  // [13] busy
                         1'b0,      // [12] pd_oe  RESERVED = 0
                         1'b0,      // [11] rd     RESERVED = 0
                         lcd_rs,    // [10] rs
                         lcd_wr,    // [9]  wr
                         lcd_cs,    // [8]  cs
                         8'hFF };   // [7:0] LEDs driven (mirror of the counter)

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
