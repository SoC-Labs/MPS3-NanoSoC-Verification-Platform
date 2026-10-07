// -----------------------------------------------------------------------------
// dut_egress.sv — DUTEGR regmap block: the DUT's Ethernet RETURN path.
//
// THE HOLE THIS FILLS
// -------------------
// DUT *reception* has been silicon-proven since 2026-07-30: the shell's virtual
// PHY drives the DUT's RMII and DFXCTL.RM_STATUS[2] was observed going 0 -> 1
// under gen_checker traffic on the fielded static. The RETURN path was designed
// (docs/DUT_ETHERNET_EGRESS.md, Option C) and never built — shell_bd.tcl
// SECTION 5 SAFE-TIES the bridge's management egress port with
// `mgmt_m_tready = 1`, i.e. every frame the DUT transmits is drained into a
// constant and thrown away. A DUT could be talked to and could not answer.
//
// This block is the consumer that tie-off was standing in for: it captures the
// frames the DUT transmits on its RMII — recovered by rmii_phy_if ->
// link_partner_mac and forwarded by eth_bridge_3port's mgmt port — into a
// dual-clock FIFO the MicroBlaze reads over AXI4-Lite, and it COUNTS what it
// could not keep.
//
// WHY tready IS HARD-WIRED TO 1 (read this before "fixing" it)
// -----------------------------------------------------------
// `frm_tready_o` is a constant 1. That is deliberate and it is the single most
// load-bearing decision in this file. eth_bridge_3port is store-and-forward
// with ONE shared round-robin sequencer and head-of-line blocking BY DESIGN
// (eth_bridge_3port.sv's v1 policy header), and it FLOODS broadcast/unknown-
// destination frames to every non-ingress port. A sink that backpressures on
// this port therefore does not "apply flow control" — it PARKS the bridge's
// only sequencer, killing the DUT's ingress scoring and the LAN9220 uplink
// along with the egress path it was trying to protect. The same reasoning is
// already written into shell_bd.tcl SECTION 5 for the tie-off this block
// replaces.
//
// So this FIFO can lose frames, and the whole design of its status surface
// follows from that: it must never lose one SILENTLY. A frame that does not
// fit is rolled back whole (never truncated), and exactly one of three
// counters moves for every frame the block accepts:
//
//     RX_FRAMES + DROP_FULL + DROP_GIANT == frames seen while CTRL.EN
//
// an invariant the bench asserts directly. STATUS.OVF is the one sticky bit a
// polling loop needs to notice that any of it happened.
//
// STORE-AND-FORWARD, NOT CUT-THROUGH
// ----------------------------------
// A reader must never be able to observe a torn frame, and a frame's length is
// not known until its last beat. dutegr_cfifo.sv therefore publishes a separate
// COMMIT pointer to the read domain; bytes are written speculatively and become
// readable only when the whole frame has landed. See that file's header.
//
// Two FIFOs, and the redundancy is a CHECK
// ----------------------------------------
//   * the DATA fifo carries {end_of_frame, byte} — 9 bits per byte;
//   * the DESC fifo carries one 16-bit byte-count per committed frame.
// The descriptor is what lets firmware size a buffer BEFORE it starts reading
// (FRAME_LEN), which the stored end-of-frame bit cannot do. Having both is
// redundant, so the block checks them against each other: if the stored
// end-of-frame marker does not land exactly on the descriptor's last byte,
// STATUS.DESYNC latches. A redundancy that is checked is not drift.
//
// CLOCKS
// ------
//   s_axi_aclk  — shell AXI-Lite, 100 MHz (shell_bd.tcl SECTION 6).
//   rmii_clk_i  — the 50 MHz RMII reference domain that eth_mac_test_subsystem
//                 (and therefore the bridge's mgmt egress) runs in.
// The crossing lives HERE, on the static side, per
// docs/contracts/partition-pins.md's clock/reset domain rule. Like
// uart_bridge.sv this block takes only ONE reset (s_axi_aresetn) and derives
// the rmii-domain reset from it (async-assert / sync-deassert), so the two
// sides of the FIFO can never disagree about pointer state across a reset.
//
// Single-file-build note: like uart_bridge.sv / mdio_phy_model.sv this top
// `include's its helper so a bench Makefile can list just this file;
// dutegr_cfifo.sv remains a separately lintable, instantiable module.
//
// THE INJECT SIDE (host -> DUT), added for mint 3
// -----------------------------------------------
// docs/planning/HANDOVER_DUT_INJECT.md. The same page grows six registers
// (0x20-0x34) and a SECOND dutegr_cfifo with its clocks swapped (write =
// s_axi_aclk, read = rmii_clk_i), feeding a small framer that drives the
// bridge's port-B INGRESS (eth_mac_test_subsystem.mgmt_s_*) through the new
// inj_m_* port. Everything the RX side decided still binds it; three things are
// different, and each is argued where it is implemented below:
//
//   * IT MAY WAIT. The capture port above must never backpressure, because a
//     stalled SINK parks the bridge's one shared sequencer. This port is a
//     SOURCE: waiting on inj_m_tready holds up only our own frame and nothing
//     else in the bridge. What it must never do is START a frame it does not
//     wholly have — so it starts only once every byte of the frame is committed
//     and visible, and then streams it back to back.
//   * IT BUILDS THE FCS. link_partner_mac neither inserts nor strips an FCS
//     (its AXIS frames carry one in both directions), so with TX_CTRL.RAW = 0
//     this block pads to 60 bytes and appends the IEEE CRC-32 itself. RAW = 1
//     sends the staged bytes exactly as written (fault injection).
//   * THE COMMIT IS SOFTWARE'S. Bytes are staged one TX_DATA write at a time
//     and become visible to the framer only at TX_CTRL.COMMIT; a frame that
//     overflowed, or is too short or too long, is rolled back whole and counted
//     in TX_REJECT, and a committed frame discarded by TX_CTRL.FLUSH is counted
//     in TX_FLUSHED. So
//         TX_FRAMES + TX_REJECT + TX_FLUSHED + frames queued == COMMITs.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

`include "dutegr_cfifo.sv"

module dut_egress #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,   // local decode only; the BD ships 32
  parameter int C_S_AXI_DATA_WIDTH = 32,
  parameter int DATA_DEPTH         = 2048, // captured bytes, power of two
  parameter int FRAME_DEPTH        = 16,   // queued frames, power of two
  parameter int MAX_FRAME          = 1536  // bytes; longer => DROP_GIANT
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus (DUTEGR, docs/contracts/shell-regmap.md)
  // ---------------------------------------------------------------------
  input  logic                          s_axi_aclk,
  input  logic                          s_axi_aresetn,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,
  input  logic [2:0]                    s_axi_awprot,
  input  logic                          s_axi_awvalid,
  output logic                          s_axi_awready,

  input  logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_wdata,
  input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb,
  input  logic                          s_axi_wvalid,
  output logic                          s_axi_wready,

  output logic [1:0]                    s_axi_bresp,
  output logic                          s_axi_bvalid,
  input  logic                          s_axi_bready,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,
  input  logic [2:0]                    s_axi_arprot,
  input  logic                          s_axi_arvalid,
  output logic                          s_axi_arready,

  output logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_rdata,
  output logic [1:0]                    s_axi_rresp,
  output logic                          s_axi_rvalid,
  input  logic                          s_axi_rready,

  // ---------------------------------------------------------------------
  // Capture port — the bridge's management EGRESS AXI-Stream, in the RMII
  // reference domain. Byte stream, tlast on the frame's final byte; frames
  // INCLUDE their 4-byte FCS (link_partner_mac.sv's AXIS convention).
  // ---------------------------------------------------------------------
  input  logic                          rmii_clk_i,
  input  logic [7:0]                    frm_tdata_i,
  input  logic                          frm_tvalid_i,
  input  logic                          frm_tlast_i,
  output logic                          frm_tready_o,

  // ---------------------------------------------------------------------
  // Inject port — the bridge's management INGRESS AXI-Stream (shell_bd.tcl
  // SECTION 5 wires it to eth_mac_test_subsystem.mgmt_s_*), in the SAME RMII
  // reference domain as the capture port. Byte stream, tlast on the frame's
  // final byte; frames INCLUDE their 4-byte FCS (appended here when RAW = 0).
  // A SOURCE, so it waits on inj_m_tready — see the INJECT SIDE header.
  // ---------------------------------------------------------------------
  output logic [7:0]                    inj_m_tdata,
  output logic                          inj_m_tvalid,
  output logic                          inj_m_tlast,
  input  logic                          inj_m_tready
);

  // The bridge must never be backpressured on this port — see the header.
  assign frm_tready_o = 1'b1;

  // ===========================================================================
  // AXI4-Lite slave — the same write/read channel FSM as dfx_ctl.sv /
  // uart_bridge.sv / board_gpio.sv, for consistency across the shell's IP.
  // ===========================================================================
  localparam int ADDR_LSB = 2;

  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_awaddr_q;
  logic                          axi_awready_q, axi_wready_q, aw_en_q;
  logic                          axi_bvalid_q;
  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_araddr_q;
  logic                          axi_arready_q, axi_rvalid_q;
  logic [C_S_AXI_DATA_WIDTH-1:0] axi_rdata_q;

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00;   // OKAY always — the contract defines no errors
  assign s_axi_bvalid  = axi_bvalid_q;
  assign s_axi_arready = axi_arready_q;
  assign s_axi_rresp   = 2'b00;   // OKAY
  assign s_axi_rvalid  = axi_rvalid_q;
  assign s_axi_rdata   = axi_rdata_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awready_q <= 1'b0;
      aw_en_q       <= 1'b1;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awready_q <= 1'b1;
      aw_en_q       <= 1'b0;
    end else if (s_axi_bready && axi_bvalid_q) begin
      aw_en_q       <= 1'b1;
      axi_awready_q <= 1'b0;
    end else begin
      axi_awready_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) axi_awaddr_q <= '0;
    else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q)
      axi_awaddr_q <= s_axi_awaddr;
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) axi_wready_q <= 1'b0;
    else if (~axi_wready_q && s_axi_wvalid && s_axi_awvalid && aw_en_q)
      axi_wready_q <= 1'b1;
    else axi_wready_q <= 1'b0;
  end

  wire slv_reg_wren = axi_wready_q && s_axi_wvalid && axi_awready_q && s_axi_awvalid;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) axi_bvalid_q <= 1'b0;
    else if (slv_reg_wren && ~axi_bvalid_q) axi_bvalid_q <= 1'b1;
    else if (s_axi_bready && axi_bvalid_q)  axi_bvalid_q <= 1'b0;
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_arready_q <= 1'b0;
      axi_araddr_q  <= '0;
    end else if (~axi_arready_q && s_axi_arvalid) begin
      axi_arready_q <= 1'b1;
      axi_araddr_q  <= s_axi_araddr;
    end else begin
      axi_arready_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) axi_rvalid_q <= 1'b0;
    else if (axi_arready_q && s_axi_arvalid && ~axi_rvalid_q) axi_rvalid_q <= 1'b1;
    else if (axi_rvalid_q && s_axi_rready) axi_rvalid_q <= 1'b0;
  end

  wire slv_reg_rden = axi_arready_q && s_axi_arvalid && ~axi_rvalid_q;

  // ===========================================================================
  // Register decode.
  //
  // FULL 64 KiB-page decode, not addr[4:2] — bug #1 (the CSR decode escape that
  // made every shell CSR read 0 on silicon: the BD instantiates these blocks at
  // C_S_AXI_ADDR_WIDTH=32, so the slave is handed the SYSTEM address) and the
  // UARTBR destructive-alias hazard (an "unmapped" offset aliasing onto a
  // FIFO-pop window and silently eating a byte) are the same two defects this
  // idiom exists to prevent. DATA here is a destructive-read FIFO port, so the
  // second one would eat a byte of the DUT's reply.
  // tests/dut_egress elaborates at 32 and drives base+offset.
  // ===========================================================================
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_CTRL       = 'h0;  // 0x00 rw enable / flush / clear-counters
  localparam logic [IDX_W-1:0] IDX_STATUS     = 'h1;  // 0x04 ro frame-ready, empty, full, sticky flags
  localparam logic [IDX_W-1:0] IDX_LEVEL      = 'h2;  // 0x08 ro committed bytes and frames waiting
  localparam logic [IDX_W-1:0] IDX_FRAME_LEN  = 'h3;  // 0x0C ro head frame bytes remaining and total
  localparam logic [IDX_W-1:0] IDX_DATA       = 'h4;  // 0x10 ro destructive byte pop with valid/last
  localparam logic [IDX_W-1:0] IDX_RX_FRAMES  = 'h5;  // 0x14 ro frames captured whole
  localparam logic [IDX_W-1:0] IDX_DROP_FULL  = 'h6;  // 0x18 ro frames dropped for want of room
  localparam logic [IDX_W-1:0] IDX_DROP_GIANT = 'h7;  // 0x1C ro frames dropped for exceeding MAX_FRAME
  // ---- the inject side (host -> DUT). Additive: 0x00-0x1C above are unchanged.
  localparam logic [IDX_W-1:0] IDX_TX_CTRL    = 'h8;  // 0x20 rw commit / abort / clear-counters / flush, RAW mode
  localparam logic [IDX_W-1:0] IDX_TX_STATUS  = 'h9;  // 0x24 ro room, empty, full, staging, sticky flags
  localparam logic [IDX_W-1:0] IDX_TX_SPACE   = 'hA;  // 0x28 ro free bytes and free frame slots
  localparam logic [IDX_W-1:0] IDX_TX_DATA    = 'hB;  // 0x2C wo stage one byte (reads 0, no side effect)
  localparam logic [IDX_W-1:0] IDX_TX_FRAMES  = 'hC;  // 0x30 ro frames handed to the bridge whole
  localparam logic [IDX_W-1:0] IDX_TX_REJECT  = 'hD;  // 0x34 ro commits refused (empty, short, long, no room)
  localparam logic [IDX_W-1:0] IDX_TX_FLUSHED = 'hE;  // 0x38 ro committed frames discarded by TX_CTRL.FLUSH

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  // ===========================================================================
  // rmii-domain reset, derived from s_axi_aresetn (async-assert, sync-deassert
  // — the Cummings pattern uart_bridge.sv uses). ONE reset source means the two
  // sides of each FIFO can never disagree about pointer state.
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [2:0] rmii_rstn_sync_q;

  always_ff @(posedge rmii_clk_i or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) rmii_rstn_sync_q <= 3'b000;
    else                rmii_rstn_sync_q <= {rmii_rstn_sync_q[1:0], 1'b1};
  end

  wire rmii_rst_n = rmii_rstn_sync_q[2];

  // ===========================================================================
  // CTRL (s_axi_aclk domain).
  //
  // EN DEFAULTS TO 1. The failure this platform has actually suffered is
  // silence-by-construction — SECTION 5 shipped the DUT's RX tied to a constant
  // zero "by design" and nobody could tell from the board. A capture block that
  // defaults to OFF is one more way for a live DUT to look dead, so the default
  // is ON: RX_FRAMES moves the moment a DUT transmits, with no setup at all.
  // With nobody draining, DROP_FULL then climbs — which is honest telemetry,
  // not a fault. CTRL.EN = 0 is the explicit opt-out.
  // ===========================================================================
  logic en_q;
  logic flush_q;

  wire ctrl_wr = slv_reg_wren && (waddr_idx == IDX_CTRL) && s_axi_wstrb[0];
  wire ctrl_clr_cnt = ctrl_wr && s_axi_wdata[2];

  // ===========================================================================
  // CAPTURE-ENABLE CDC: a single level, plain 2-FF, (* ASYNC_REG *). The write
  // FSM only ever samples it at a frame's FIRST beat, so a toggle mid-frame can
  // never produce a half-captured frame.
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [1:0] en_sync_q;

  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) en_sync_q <= 2'b00;
    else             en_sync_q <= {en_sync_q[0], en_q};
  end

  wire en_rmii = en_sync_q[1];

  // ===========================================================================
  // FIFOs.
  // ===========================================================================
  localparam int DAW = $clog2(DATA_DEPTH);
  localparam int FAW = $clog2(FRAME_DEPTH);

  logic             data_push, data_commit, data_rollback;
  logic [8:0]       data_wdata;          // {end_of_frame, byte}
  logic             data_wfull;
  logic [DAW:0]     data_wfree;
  logic             data_pop;
  logic [8:0]       data_rdata;
  logic [DAW:0]     data_rlevel;

  logic             desc_push;
  logic [15:0]      desc_wdata;
  logic             desc_wfull;
  logic [FAW:0]     desc_wfree;
  logic             desc_pop;
  logic [15:0]      desc_rdata;
  logic [FAW:0]     desc_rlevel;

  dutegr_cfifo #(.WIDTH(9), .DEPTH(DATA_DEPTH)) u_data_fifo (
    .wclk_i      (rmii_clk_i),
    .wrst_n_i    (rmii_rst_n),
    .wpush_i     (data_push),
    .wdata_i     (data_wdata),
    .wcommit_i   (data_commit),
    .wrollback_i (data_rollback),
    .wfull_o     (data_wfull),
    .wfree_o     (data_wfree),
    .rclk_i      (s_axi_aclk),
    .rrst_n_i    (s_axi_aresetn),
    .rpop_i      (data_pop),
    .rdata_o     (data_rdata),
    .rlevel_o    (data_rlevel)
  );

  // The descriptor FIFO commits on push: one word per frame, already whole.
  dutegr_cfifo #(.WIDTH(16), .DEPTH(FRAME_DEPTH)) u_desc_fifo (
    .wclk_i      (rmii_clk_i),
    .wrst_n_i    (rmii_rst_n),
    .wpush_i     (desc_push),
    .wdata_i     (desc_wdata),
    .wcommit_i   (desc_push),
    .wrollback_i (1'b0),
    .wfull_o     (desc_wfull),
    .wfree_o     (desc_wfree),
    .rclk_i      (s_axi_aclk),
    .rrst_n_i    (s_axi_aresetn),
    .rpop_i      (desc_pop),
    .rdata_o     (desc_rdata),
    .rlevel_o    (desc_rlevel)
  );

  // ===========================================================================
  // WRITE SIDE (rmii_clk_i) — store-and-forward capture with rollback.
  //
  // tready is 1, so every cycle with tvalid IS a transfer. Frame position is
  // tracked from the stream itself (mid_q), independently of CTRL.EN, so a
  // frame can only ever be ARMED at its true first beat.
  // ===========================================================================
  logic        mid_q;          // a frame is in progress on the wire
  logic        cap_q;          // ...and we armed capture for it
  logic        doom_full_q;    // ...and it will not fit
  logic        doom_giant_q;   // ...and it is longer than MAX_FRAME
  logic [15:0] len_q;          // bytes of this frame seen so far

  wire beat  = frm_tvalid_i;
  wire first = beat && !mid_q;
  wire last  = beat && frm_tlast_i;

  // Combinational "as of this beat" views, so a single-beat frame (first and
  // last together) is handled by exactly the same expressions.
  wire        cap_now        = first ? en_rmii : cap_q;
  wire [15:0] len_now        = first ? 16'd1
                                     : ((len_q == 16'hFFFF) ? len_q : len_q + 16'd1);
  wire        doom_full_now  = first ? (data_wfree == '0)
                                     : (doom_full_q  || (data_wfree == '0));
  wire        doom_giant_now = first ? 1'b0
                                     : (doom_giant_q || (len_q >= MAX_FRAME[15:0]));
  wire        doom_now       = doom_full_now || doom_giant_now;

  assign data_push  = beat && cap_now && !doom_now;
  assign data_wdata = {frm_tlast_i, frm_tdata_i};

  // A frame is committed only if it was armed, never doomed, AND there is a
  // descriptor slot to record it in — a frame with no descriptor could not be
  // sized by firmware, so it is a drop, not a commit.
  wire commit_ok = last && cap_now && !doom_now && (desc_wfree != '0);

  assign data_commit   = commit_ok;
  assign data_rollback = last && cap_now && !commit_ok;

  // Exactly one of these fires per ARMED frame. GIANT is reported in preference
  // to FULL: a giant is a DUT defect, while the FIFO filling up behind one is a
  // consequence of it.
  wire commit_evt = commit_ok;
  wire giant_evt  = last && cap_now && !commit_ok &&  doom_giant_now;
  wire full_evt   = last && cap_now && !commit_ok && !doom_giant_now;

  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) begin
      mid_q        <= 1'b0;
      cap_q        <= 1'b0;
      doom_full_q  <= 1'b0;
      doom_giant_q <= 1'b0;
      len_q        <= 16'd0;
    end else begin
      if (beat) mid_q <= ~frm_tlast_i;
      if (last) begin
        cap_q        <= 1'b0;
        doom_full_q  <= 1'b0;
        doom_giant_q <= 1'b0;
        len_q        <= 16'd0;
      end else if (beat) begin
        cap_q        <= cap_now;
        doom_full_q  <= doom_full_now;
        doom_giant_q <= doom_giant_now;
        len_q        <= len_now;
      end
    end
  end

  // ---- descriptor push, ONE CYCLE AFTER the data commit ---------------------
  // Ordering guarantee, not an optimisation: both FIFOs cross to the reader
  // through identical 2-FF gray synchronizers, so publishing the descriptor a
  // cycle later means the reader can NEVER see a descriptor before the bytes it
  // describes. Reversed, a read of FRAME_LEN could name a length whose bytes
  // were not yet addressable.
  logic        desc_push_q;
  logic [15:0] desc_len_q;

  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) begin
      desc_push_q <= 1'b0;
      desc_len_q  <= 16'd0;
    end else begin
      desc_push_q <= commit_evt;
      if (commit_evt) desc_len_q <= len_now;
    end
  end

  assign desc_push  = desc_push_q;
  assign desc_wdata = desc_len_q;

  // ===========================================================================
  // EVENT CDC — rmii-domain frame events into s_axi_aclk-domain counters.
  //
  // Toggle + 2-FF + edge detect, NOT a gray-coded counter: every event is a
  // +1, the counters are then plain binary in the domain that reads them (so
  // CTRL.CLR_CNT is a local synchronous clear with no crossing at all), and
  // there is no multi-bit value in flight. The pattern's one requirement is
  // that events be further apart than the destination's sync depth: these are
  // whole Ethernet frames, so the closest two can be is a 64-byte frame plus
  // the 96-bit IFG — ~300 rmii_clk cycles — against 3 s_axi_aclk cycles.
  // ===========================================================================
  logic commit_tog_q, full_tog_q, giant_tog_q;

  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) begin
      commit_tog_q <= 1'b0;
      full_tog_q   <= 1'b0;
      giant_tog_q  <= 1'b0;
    end else begin
      if (commit_evt) commit_tog_q <= ~commit_tog_q;
      if (full_evt)   full_tog_q   <= ~full_tog_q;
      if (giant_evt)  giant_tog_q  <= ~giant_tog_q;
    end
  end

  (* ASYNC_REG = "TRUE" *) logic [2:0] commit_sync_q, full_sync_q, giant_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      commit_sync_q <= 3'b000;
      full_sync_q   <= 3'b000;
      giant_sync_q  <= 3'b000;
    end else begin
      commit_sync_q <= {commit_sync_q[1:0], commit_tog_q};
      full_sync_q   <= {full_sync_q[1:0],   full_tog_q};
      giant_sync_q  <= {giant_sync_q[1:0],  giant_tog_q};
    end
  end

  wire commit_pulse = commit_sync_q[2] ^ commit_sync_q[1];
  wire full_pulse   = full_sync_q[2]   ^ full_sync_q[1];
  wire giant_pulse  = giant_sync_q[2]  ^ giant_sync_q[1];

  logic [31:0] rx_frames_q, drop_full_q, drop_giant_q;
  logic        ovf_sticky_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      rx_frames_q  <= 32'd0;
      drop_full_q  <= 32'd0;
      drop_giant_q <= 32'd0;
      ovf_sticky_q <= 1'b0;
    end else if (ctrl_clr_cnt) begin
      rx_frames_q  <= 32'd0;
      drop_full_q  <= 32'd0;
      drop_giant_q <= 32'd0;
      ovf_sticky_q <= 1'b0;
    end else begin
      if (commit_pulse) rx_frames_q  <= rx_frames_q  + 32'd1;
      if (full_pulse)   drop_full_q  <= drop_full_q  + 32'd1;
      if (giant_pulse)  drop_giant_q <= drop_giant_q + 32'd1;
      if (full_pulse || giant_pulse) ovf_sticky_q <= 1'b1;
    end
  end

  // ===========================================================================
  // READ SIDE (s_axi_aclk) — head-frame tracking and the destructive DATA port.
  // ===========================================================================
  logic        have_head_q;
  logic [15:0] rem_q;        // bytes left in the head frame
  logic [15:0] hlen_q;       // the head frame's total length
  logic        desync_q;     // sticky: stored end-of-frame vs descriptor length

  wire head_load = !have_head_q && (desc_rlevel != '0);

  // A byte is poppable only while a whole frame is at the head — which, being
  // store-and-forward, guarantees its bytes are committed and addressable.
  wire data_avail = have_head_q && (data_rlevel != '0);
  wire axi_pop    = slv_reg_rden && (raddr_idx == IDX_DATA) && data_avail;
  wire flush_pop  = flush_q && data_avail;

  assign data_pop = axi_pop || flush_pop;
  assign desc_pop = data_pop && (rem_q == 16'd1);

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      have_head_q <= 1'b0;
      rem_q       <= 16'd0;
      hlen_q      <= 16'd0;
      desync_q    <= 1'b0;
    end else begin
      if (data_pop) begin
        rem_q <= rem_q - 16'd1;
        // The two records of "where this frame ends" must agree.
        if (data_rdata[8] != (rem_q == 16'd1)) desync_q <= 1'b1;
        if (rem_q == 16'd1) have_head_q <= 1'b0;
      end else if (head_load) begin
        have_head_q <= 1'b1;
        rem_q       <= desc_rdata;
        hlen_q      <= desc_rdata;
      end
      if (ctrl_clr_cnt) desync_q <= 1'b0;
    end
  end

  // ---- CTRL / FLUSH ---------------------------------------------------------
  // FLUSH is a BOUNDED drain in this domain only — no second reset, no reset
  // crossing. It pops for at most DATA_DEPTH + FRAME_DEPTH cycles, which is
  // strictly more than the FIFO can hold, then stops whether or not the DUT is
  // still transmitting. Its purpose is discarding the previous RM's frames
  // after a DFX swap.
  localparam int FLUSH_CYCLES = DATA_DEPTH + FRAME_DEPTH;
  localparam int FLUSH_W      = $clog2(FLUSH_CYCLES + 1);

  logic [FLUSH_W-1:0] flush_cnt_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      en_q        <= 1'b1;          // see the CTRL header: default ON
      flush_q     <= 1'b0;
      flush_cnt_q <= '0;
    end else begin
      // Expiry first, so a FLUSH written WHILE flushing restarts the drain
      // rather than cancelling it.
      if (flush_q) begin
        if (flush_cnt_q == '0 || (!have_head_q && desc_rlevel == '0))
          flush_q <= 1'b0;
        else
          flush_cnt_q <= flush_cnt_q - 1'b1;
      end
      if (ctrl_wr) begin
        en_q <= s_axi_wdata[0];
        if (s_axi_wdata[1]) begin
          flush_q     <= 1'b1;
          flush_cnt_q <= FLUSH_W'(FLUSH_CYCLES);
        end
      end
    end
  end

  // ---- live full flags, rmii domain -> AXI (single levels, plain 2-FF) ------
  //
  // REGISTERED IN THE SOURCE DOMAIN FIRST. `wfull_o` is combinational
  // (`wfree_o == 0`, i.e. a subtraction and a compare), and feeding that
  // straight into a synchronizer is `report_cdc`'s CDC-10 "Combinational logic
  // detected before a synchronizer" -- a CRITICAL, and a real one: a glitch on
  // the compare can be captured as a 1. MEASURED: the first OOC implementation
  // of this block reported exactly two CDC-10s, both here. One flop in the
  // rmii domain removes them, and these are status bits nothing gates on, so
  // the extra cycle of latency costs nothing.
  logic data_full_q, desc_full_q;

  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) begin
      data_full_q <= 1'b0;
      desc_full_q <= 1'b0;
    end else begin
      data_full_q <= data_wfull;
      desc_full_q <= desc_wfull;
    end
  end

  (* ASYNC_REG = "TRUE" *) logic [1:0] data_full_sync_q, desc_full_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      data_full_sync_q <= 2'b00;
      desc_full_sync_q <= 2'b00;
    end else begin
      data_full_sync_q <= {data_full_sync_q[0], data_full_q};
      desc_full_sync_q <= {desc_full_sync_q[0], desc_full_q};
    end
  end

  // ###########################################################################
  // ###########################################################################
  // INJECT SIDE — host -> DUT (docs/planning/HANDOVER_DUT_INJECT.md §3-§4).
  //
  // The mirror image of everything above, with the clocks swapped: software
  // STAGES a frame byte by byte in the s_axi_aclk domain, COMMITs it, and a
  // framer in the rmii_clk_i domain streams it into the bridge's port-B
  // ingress. Nothing below reads or writes any RX-side state, and nothing above
  // reads or writes any of this: TX and RX share only the AXI-Lite FSM and the
  // derived rmii reset.
  // ###########################################################################
  // ###########################################################################

  // IEEE 802.3 CRC-32 (reflected 0xEDB88320, init all-ones, FCS = ~crc sent
  // LSB first) — the SAME function and constants as link_partner_mac.sv and
  // gen_checker.sv, kept module-local so this stays a single-file build.
  function automatic logic [31:0] crc32_byte(input logic [31:0] c,
                                             input logic [7:0]  b);
    logic [31:0] x;
    x = c ^ {24'h0, b};
    for (int i = 0; i < 8; i++)
      x = x[0] ? ((x >> 1) ^ 32'hEDB8_8320) : (x >> 1);
    return x;
  endfunction

  localparam logic [31:0] CRC_INIT = 32'hFFFF_FFFF;

  // Length limits — handover §4 rule 3 as AMENDED (A1, decided by david
  // 2026-09-23). RAW = 0 is a normal NIC and produces only frames an 802.3 MAC
  // accepts: 14 .. 1514 staged bytes, i.e. 64 .. 1518 on the wire after the
  // pad and the FCS. (The original rule, MAX_FRAME - 4 = 1532, put 1536-byte
  // giants on the wire from the "normal" mode.) RAW = 1 is the fault-injection
  // mode and keeps 1 .. MAX_FRAME, oversize included, on purpose. The bridge's
  // per-port ingress buffer is 2 KiB, so a MAX_FRAME frame always fits it.
  // (min() only so a smaller MAX_FRAME can never let pad + FCS exceed it.)
  localparam int          TX_MAX_NORM_I = (MAX_FRAME - 4 < 1514) ? (MAX_FRAME - 4) : 1514;
  localparam logic [15:0] TX_PAD_TO   = 16'd60;   // 64-byte minimum minus FCS
  localparam logic [15:0] TX_MIN_NORM = 16'd14;   // dst + src + ethertype
  localparam logic [15:0] TX_MAX_NORM = 16'(TX_MAX_NORM_I);   // 1514: 1518 on the wire
  localparam logic [15:0] TX_MAX_RAW  = 16'(MAX_FRAME);

  localparam logic [FAW:0] TXQ_DEPTH_W = (FAW+1)'(FRAME_DEPTH);

  // ===========================================================================
  // TX_CTRL decode (s_axi_aclk). [0] COMMIT, [1] ABORT, [2] CLR_CNT, [3] FLUSH
  // are W1 / self-clearing actions in byte lane 0; [8] RAW is a plain rw bit in
  // byte lane 1. ABORT wins over COMMIT written in the same word: the frame is
  // discarded and the write is NOT a commit (it moves no counter).
  // ===========================================================================
  wire txctl_wr    = slv_reg_wren && (waddr_idx == IDX_TX_CTRL);
  wire txctl_b0    = txctl_wr && s_axi_wstrb[0];
  wire tx_abort    = txctl_b0 && s_axi_wdata[1];
  wire tx_commit   = txctl_b0 && s_axi_wdata[0] && !s_axi_wdata[1];
  wire tx_clr_cnt  = txctl_b0 && s_axi_wdata[2];
  wire tx_flush_wr = txctl_b0 && s_axi_wdata[3];
  wire tx_raw_wr   = txctl_wr && s_axi_wstrb[1];
  wire txdata_wr   = slv_reg_wren && (waddr_idx == IDX_TX_DATA) && s_axi_wstrb[0];

  logic tx_raw_q;

  // The RAW a COMMIT uses is the one its own write carries (when lane 1 is
  // strobed), so `TX_CTRL = RAW<<8 | COMMIT` means what it says. A full-word
  // write of TX_CTRL writes RAW like any rw bit — write COMMIT as
  // (raw << 8) | 1, not as a bare 1, to keep RAW = 1.
  wire tx_raw_eff = tx_raw_wr ? s_axi_wdata[8] : tx_raw_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) tx_raw_q <= 1'b0;
    else if (tx_raw_wr) tx_raw_q <= s_axi_wdata[8];
  end

  // ===========================================================================
  // INJECT FIFOs — the RX pair with the clocks swapped. Write side on the AXI
  // clock (software stages), read side on rmii_clk_i (the framer).
  //   * DATA fifo {end_of_frame, byte}, DATA_DEPTH deep (2048 x 9 = one RAMB18);
  //   * DESC fifo {RAW, byte-count}, one word per committed frame.
  // As on the RX side the two records of where a frame ends are CHECKED against
  // each other (TX_STATUS.DESYNC), not trusted.
  // ===========================================================================
  logic             txd_push, txd_publish, txd_rollback;
  logic [8:0]       txd_wdata;
  logic             txd_wfull;
  logic [DAW:0]     txd_wfree;
  logic             txd_pop;
  logic [8:0]       txd_rdata;
  logic [DAW:0]     txd_rlevel;

  logic             txq_push;
  logic [16:0]      txq_wdata;
  logic [FAW:0]     txq_wfree;       // (its wfull_o is unused: DESC_FULL is
                                     //  judged net of an in-flight push)
  logic             txq_pop;
  logic [16:0]      txq_rdata;
  logic [FAW:0]     txq_rlevel;

  dutegr_cfifo #(.WIDTH(9), .DEPTH(DATA_DEPTH)) u_tx_data_fifo (
    .wclk_i      (s_axi_aclk),
    .wrst_n_i    (s_axi_aresetn),
    .wpush_i     (txd_push),
    .wdata_i     (txd_wdata),
    .wcommit_i   (txd_publish),
    .wrollback_i (txd_rollback),
    .wfull_o     (txd_wfull),
    .wfree_o     (txd_wfree),
    .rclk_i      (rmii_clk_i),
    .rrst_n_i    (rmii_rst_n),
    .rpop_i      (txd_pop),
    .rdata_o     (txd_rdata),
    .rlevel_o    (txd_rlevel)
  );

  // Commits on push: one word per frame, already whole.
  dutegr_cfifo #(.WIDTH(17), .DEPTH(FRAME_DEPTH)) u_tx_desc_fifo (
    .wclk_i      (s_axi_aclk),
    .wrst_n_i    (s_axi_aresetn),
    .wpush_i     (txq_push),
    .wdata_i     (txq_wdata),
    .wcommit_i   (txq_push),
    .wrollback_i (1'b0),
    .wfull_o     (),
    .wfree_o     (txq_wfree),
    .rclk_i      (rmii_clk_i),
    .rrst_n_i    (rmii_rst_n),
    .rpop_i      (txq_pop),
    .rdata_o     (txq_rdata),
    .rlevel_o    (txq_rlevel)
  );

  // ===========================================================================
  // STAGING (s_axi_aclk) — frame-atomic, by the FIFO's commit/rollback.
  //
  // WHY A ONE-BYTE HOLDING REGISTER: the data FIFO stores an end-of-frame bit
  // with every byte, and which byte is the last is not known until COMMIT. So
  // the newest staged byte is HELD here rather than pushed; the next TX_DATA
  // write pushes it with eof = 0, and a good COMMIT pushes it with eof = 1 and
  // publishes the frame IN THE SAME CYCLE (dutegr_cfifo's wcommit_i publishes
  // through that cycle's push). The framer therefore never sees a byte of a
  // frame before the whole frame, eof included, is committed.
  //
  // A push that finds the FIFO full is refused by the FIFO and DOOMS the frame
  // (txs_ovf_q): it is rolled back whole at its COMMIT and counted, never sent
  // truncated. txs_len_q counts every staged byte, the held one included,
  // saturating — so a too-long frame is recognised at COMMIT however long.
  // ===========================================================================
  logic        txs_hold_vld_q;
  logic [7:0]  txs_hold_q;
  logic [15:0] txs_len_q;
  logic        txs_ovf_q;

  logic        txq_push_q;    // descriptor push, one cycle after the data commit
  logic [16:0] txq_wdata_q;

  // Bytes that can still be staged into the current frame: the FIFO's own
  // (conservative) vacancy, less the held byte that still needs a slot at
  // COMMIT. Saturates at 0 — a full FIFO with a byte held is a doomed frame.
  wire [DAW:0] txd_free_eff = (txd_wfree == '0) ? '0
                              : (txd_wfree - {{DAW{1'b0}}, txs_hold_vld_q});
  // Descriptor slots, less one already promised to a descriptor push in flight.
  wire [FAW:0] txq_free_eff = txq_wfree - {{FAW{1'b0}}, txq_push_q};

  wire tx_len_ok = tx_raw_eff
                 ? ((txs_len_q >= 16'd1)       && (txs_len_q <= TX_MAX_RAW))
                 : ((txs_len_q >= TX_MIN_NORM) && (txs_len_q <= TX_MAX_NORM));

  // The COMMIT verdict. Every reason a commit can fail is here, in one place:
  // empty / too short / too long (tx_len_ok), a staged byte was refused
  // (txs_ovf_q), no slot for the final held byte (txd_wfull), no descriptor
  // slot (txq_free_eff).
  wire tx_commit_ok  = tx_commit && tx_len_ok && !txs_ovf_q && !txd_wfull
                       && (txq_free_eff != '0);
  wire tx_commit_rej = tx_commit && !tx_commit_ok;

  wire txd_push_mid  = txdata_wr && txs_hold_vld_q;

  assign txd_push     = txd_push_mid || tx_commit_ok;
  assign txd_wdata    = {tx_commit_ok, txs_hold_q};    // eof on the final byte only
  // The two lines the benches' controls mutate (tests/dut_egress/mutate.py):
  //   tornframe  — publish every byte as it is staged (a plain async FIFO);
  //   norollback — never roll back, so a discarded frame's bytes stay queued.
  assign txd_publish  = tx_commit_ok;                  // MUTATION-POINT(tornframe)
  assign txd_rollback = tx_commit_rej || tx_abort;     // MUTATION-POINT(norollback)

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      txs_hold_vld_q <= 1'b0;
      txs_hold_q     <= 8'd0;
      txs_len_q      <= 16'd0;
      txs_ovf_q      <= 1'b0;
    end else if (tx_commit || tx_abort) begin
      // Committed, rejected or aborted: either way staging starts again empty.
      txs_hold_vld_q <= 1'b0;
      txs_len_q      <= 16'd0;
      txs_ovf_q      <= 1'b0;
    end else if (txdata_wr) begin
      txs_hold_q     <= s_axi_wdata[7:0];
      txs_hold_vld_q <= 1'b1;
      txs_len_q      <= (txs_len_q == 16'hFFFF) ? txs_len_q : (txs_len_q + 16'd1);
      if (txd_push_mid && txd_wfull) txs_ovf_q <= 1'b1;
    end
  end

  // ---- descriptor push, ONE CYCLE AFTER the data commit ---------------------
  // The RX side's ordering guarantee, reused: both FIFOs cross through
  // identical gray synchronizers, so the framer can never see a descriptor
  // before the bytes it describes. The framer ALSO checks that every described
  // byte is visible before it starts (txr_whole), so this ordering is a second
  // line of defence rather than the only one.
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      txq_push_q  <= 1'b0;
      txq_wdata_q <= 17'd0;
    end else begin
      txq_push_q <= tx_commit_ok;
      if (tx_commit_ok) txq_wdata_q <= {tx_raw_eff, txs_len_q};
    end
  end

  assign txq_push  = txq_push_q;
  assign txq_wdata = txq_wdata_q;

  // ===========================================================================
  // FLUSH — a bounded, four-phase request/acknowledge into the rmii domain,
  // carrying an EXACT target.
  //
  // Discarding committed frames needs their READ pointer to move, and that
  // pointer lives in the rmii domain, so unlike the RX side's FLUSH this one
  // must cross. A level request (registered, then 2-FF) and a level
  // acknowledge back (registered, then 2-FF): no pulse is ever narrower than a
  // destination clock, and FLUSH_BUSY stays up until the acknowledge has
  // returned to zero.
  //
  // WHAT IT DISCARDS, EXACTLY: every frame COMMITTED BEFORE (or in the same
  // write as) the FLUSH that has not started by the time the request reaches
  // the framer. "Committed" is counted HERE, where COMMIT happens
  // (tx_acc_q), and the count at the FLUSH write travels with the request as
  // a target (tx_flush_tgt_q). Without it, a frame committed a few cycles
  // before FLUSH — its descriptor still inside the FIFO's pointer synchronizer
  // when the request arrived — escaped the flush and was sent afterwards; with
  // TX_FLUSHED an exact count (amendment A2), that race had to go.
  //
  // tx_flush_tgt_q is a bundled-data crossing: it is written in the SAME cycle
  // the request rises and never again until the acknowledge has come back
  // down. The rmii side CAPTURES it into its own register (txr_tgt_q) exactly
  // once, clock-enabled by the synchronized request's rising edge — by then
  // it has been stable for >= 1 full rmii period — and the framer reads only
  // that copy. MEASURED (OOC, 2026-09-24): the first version let the framer
  // read tx_flush_tgt_q combinationally, and report_cdc flagged 73 CDC-1
  // CRITICALs ("unknown CDC circuitry") on it, fanning into the FSM's clock
  // enables. The capture register is the CE-controlled structure report_cdc
  // recognises. The counters are FAW+2 bits: at most FRAME_DEPTH frames can
  // be committed-and-unfinished, so the signed difference target - finished
  // is always in range.
  //
  // A FLUSH written while FLUSH_BUSY is IGNORED (it neither restarts nor
  // extends the one in progress). Software polls FLUSH_BUSY.
  // ===========================================================================
  localparam int TCW = FAW + 2;

  logic           tx_flush_req_q;
  logic [TCW-1:0] tx_acc_q;          // COMMITs accepted, mod 2^TCW
  logic [TCW-1:0] tx_flush_tgt_q;    // tx_acc_q as of the FLUSH write
  logic           txr_flush_ack_q;   // rmii domain, declared here for the sync below

  (* ASYNC_REG = "TRUE" *) logic [1:0] tx_flush_ack_sync_q;

  wire tx_flush_busy  = tx_flush_req_q || tx_flush_ack_sync_q[1];
  wire tx_flush_start = tx_flush_wr && !tx_flush_busy;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      tx_flush_req_q      <= 1'b0;
      tx_flush_ack_sync_q <= 2'b00;
      tx_acc_q            <= '0;
      tx_flush_tgt_q      <= '0;
    end else begin
      tx_flush_ack_sync_q <= {tx_flush_ack_sync_q[0], txr_flush_ack_q};
      tx_acc_q            <= tx_acc_q + TCW'(tx_commit_ok);
      if (tx_flush_req_q && tx_flush_ack_sync_q[1]) begin
        tx_flush_req_q <= 1'b0;
      end else if (tx_flush_start) begin
        tx_flush_req_q <= 1'b1;
        // COMMIT|FLUSH in one write flushes that frame too.
        tx_flush_tgt_q <= tx_acc_q + TCW'(tx_commit_ok);
      end
    end
  end

  // ===========================================================================
  // THE FRAMER (rmii_clk_i) — committed frames out onto inj_m_*.
  //
  // RULE 1 (handover §4): A SOURCE MAY WAIT, but it never STARTS a frame it
  // does not wholly have. txr_whole requires a committed descriptor AND every
  // byte it describes to be visible in the data FIFO; only then, and only when
  // the bridge's port-B ingress is free (inj_m_tready), does the framer start —
  // and from there it streams the frame back to back.
  //
  // WHY IT CHECKS tready BEFORE RAISING tvalid (a deliberate choice): it keeps
  // FLUSH bounded. A frame that has been presented must, by AXI-Stream rules,
  // stay presented until it is taken; one that has not can still be discarded.
  // The sink here is eth_bridge_3port, whose tready is its own recv_q — it
  // does not wait for tvalid — so waiting on tready cannot deadlock. Once a
  // frame starts, the bridge holds tready high until tlast (its ingress buffer
  // is a whole frame), so the stream is back to back; if some other sink ever
  // stalled mid-frame the framer would simply wait, holding tvalid and tdata.
  //
  // RAW = 0: data, then zero pad up to 60 bytes, then the 4 FCS bytes (tlast on
  // the last). RAW = 1: data only, tlast on its final byte.
  //
  // After every frame end — sent or discarded — a short GAP spaces the frame
  // events that cross into the AXI domain by >= 4 rmii cycles, i.e. 8 AXI
  // cycles against a 3-deep toggle synchronizer, even for 1-byte RAW frames
  // back to back into an always-ready sink.
  // ===========================================================================
  typedef enum logic [2:0] {
    TXR_IDLE  = 3'd0,
    TXR_DATA  = 3'd1,
    TXR_PAD   = 3'd2,
    TXR_FCS   = 3'd3,
    TXR_DRAIN = 3'd4,
    TXR_GAP   = 3'd5
  } txr_state_e;

  txr_state_e  txr_st_q;
  logic [15:0] txr_lastidx_q;   // index of the frame's final DATA byte (len - 1)
  logic        txr_raw_q;       // this frame's RAW, from its descriptor
  logic        txr_short_q;     // this frame needs padding (len < 60)
  logic [15:0] txr_cnt_q;       // bytes emitted (data + pad) or drained so far
  logic [31:0] txr_crc_q;
  logic [1:0]  txr_fcs_q;       // FCS byte index
  logic        txr_bad_q;       // an end-of-frame bit disagreed with the length
  logic        txr_gap_q;
  logic [TCW-1:0] txr_fin_q;    // frames FINISHED (sent or discarded), mod 2^TCW

  // The request's 2-FF synchronizer, then ONE plain flop (txr_flush_q) that
  // IS the request as the framer sees it. The rising edge is taken between
  // the synchronizer's OUTPUT and that flop — never off its first stage:
  // MEASURED (OOC, 2026-09-24), an edge detector reading stage 0 made
  // report_cdc demote the whole synchronizer to CDC-1 "unknown circuitry",
  // because stage 0 then fed logic other than stage 1.
  (* ASYNC_REG = "TRUE" *) logic [1:0] txr_flush_sync_q;
  logic txr_flush_q;
  wire txr_flush_req  = txr_flush_q;
  // The one cycle the target is captured into this domain (FLUSH header); at
  // the same edge txr_flush_q rises, so txr_owed never sees a stale target.
  wire txr_flush_rise = txr_flush_sync_q[1] && !txr_flush_q;
  logic [TCW-1:0] txr_tgt_q;    // tx_flush_tgt_q, captured on txr_flush_rise

  wire [15:0] txq_len   = txq_rdata[15:0];
  wire        txq_raw   = txq_rdata[16];
  wire        txq_avail = (txq_rlevel != '0);
  wire        txd_avail = (txd_rlevel != '0);

  // THE FRAME-ATOMIC START RULE: a committed descriptor, AND every byte it
  // describes committed and visible. A frame's bytes only ever leave through
  // this framer, so once whole it stays whole until it is streamed.
  wire txr_whole = txq_avail && (16'(txd_rlevel) >= txq_len);
  wire txr_idle  = (txr_st_q == TXR_IDLE);

  wire txr_go       = txr_idle && txr_whole && !txr_flush_req && inj_m_tready;  // MUTATION-POINT(tornframe)

  // Frames the FLUSH in progress still owes: target - finished, SIGNED (a
  // frame that started before the request arrived may carry `finished` past
  // the target — that is "nothing owed", not "2^TCW - 1 owed").
  wire [TCW-1:0] txr_owed_d = txr_tgt_q - txr_fin_q;
  wire           txr_owed   = txr_flush_req && !txr_owed_d[TCW-1] && (txr_owed_d != '0);

  // An owed frame whose descriptor is still crossing is simply waited for: it
  // WILL become whole, because it was committed.
  wire txr_drain_go = txr_idle && txr_whole && txr_owed && !txr_flush_ack_q;

  wire        txr_last_data = (txr_cnt_q == txr_lastidx_q);
  wire [31:0] txr_fcs_word  = ~txr_crc_q;

  always_comb begin
    inj_m_tvalid = 1'b0;
    inj_m_tdata  = 8'h00;
    inj_m_tlast  = 1'b0;
    case (txr_st_q)
      TXR_DATA: begin
        // Always true in practice — the whole frame was visible at the start
        // and only this framer consumes it — but never present a byte that is
        // not there.
        inj_m_tvalid = txd_avail;
        inj_m_tdata  = txd_rdata[7:0];
        inj_m_tlast  = txr_raw_q && txr_last_data;
      end
      TXR_PAD: begin
        inj_m_tvalid = 1'b1;
        inj_m_tdata  = 8'h00;
      end
      TXR_FCS: begin
        inj_m_tvalid = 1'b1;
        inj_m_tdata  = txr_fcs_word[{txr_fcs_q, 3'b000} +: 8];   // LSB first
        inj_m_tlast  = (txr_fcs_q == 2'd3);
      end
      default: ;
    endcase
  end

  wire txr_beat      = inj_m_tvalid && inj_m_tready;
  wire txr_data_beat = (txr_st_q == TXR_DATA) && txr_beat;
  wire txr_drain_pop = (txr_st_q == TXR_DRAIN) && txd_avail;

  // A frame is handed over WHOLE at its tlast beat; a flushed one is gone at
  // its last drained byte. The descriptor is popped THEN, not at the start, so
  // the descriptor FIFO's occupancy is exactly "committed and not yet
  // finished" — which is what TX_STATUS.EMPTY and TX_SPACE report.
  wire txr_sent  = txr_beat && inj_m_tlast;
  wire txr_dropd = txr_drain_pop && txr_last_data;

  // The end-of-frame bit must be set on the descriptor's last byte and nowhere
  // else; judged at the last data byte, whether it is being sent or drained.
  wire txr_eof_bad   = (txd_rdata[8] != txr_last_data);
  wire txr_desync_ev = (txr_data_beat || txr_drain_pop) && txr_last_data
                       && (txr_bad_q || txr_eof_bad);

  assign txd_pop = txr_data_beat || txr_drain_pop;
  assign txq_pop = txr_sent || txr_dropd;

  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) begin
      txr_st_q      <= TXR_IDLE;
      txr_lastidx_q <= 16'd0;
      txr_raw_q     <= 1'b0;
      txr_short_q   <= 1'b0;
      txr_cnt_q     <= 16'd0;
      txr_crc_q     <= CRC_INIT;
      txr_fcs_q     <= 2'd0;
      txr_bad_q     <= 1'b0;
      txr_gap_q     <= 1'b0;
    end else begin
      case (txr_st_q)
        TXR_IDLE: begin
          if (txr_go || txr_drain_go) begin
            txr_st_q      <= txr_go ? TXR_DATA : TXR_DRAIN;
            txr_lastidx_q <= txq_len - 16'd1;
            txr_raw_q     <= txq_raw;
            txr_short_q   <= (txq_len < TX_PAD_TO);
            txr_cnt_q     <= 16'd0;
            txr_crc_q     <= CRC_INIT;
            txr_fcs_q     <= 2'd0;
            txr_bad_q     <= 1'b0;
          end
        end

        TXR_DATA: begin
          if (txr_beat) begin
            txr_crc_q <= crc32_byte(txr_crc_q, txd_rdata[7:0]);
            txr_cnt_q <= txr_cnt_q + 16'd1;
            if (txr_eof_bad) txr_bad_q <= 1'b1;
            if (txr_last_data) begin
              txr_st_q  <= txr_raw_q   ? TXR_GAP
                         : txr_short_q ? TXR_PAD
                                       : TXR_FCS;
              txr_gap_q <= 1'b1;
            end
          end
        end

        TXR_PAD: begin
          if (txr_beat) begin
            txr_crc_q <= crc32_byte(txr_crc_q, 8'h00);
            txr_cnt_q <= txr_cnt_q + 16'd1;
            if (txr_cnt_q == (TX_PAD_TO - 16'd1)) txr_st_q <= TXR_FCS;
          end
        end

        TXR_FCS: begin
          if (txr_beat) begin
            txr_fcs_q <= txr_fcs_q + 2'd1;
            if (txr_fcs_q == 2'd3) begin
              txr_st_q  <= TXR_GAP;
              txr_gap_q <= 1'b1;
            end
          end
        end

        TXR_DRAIN: begin
          if (txd_avail) begin
            txr_cnt_q <= txr_cnt_q + 16'd1;
            if (txr_eof_bad) txr_bad_q <= 1'b1;
            if (txr_last_data) begin
              txr_st_q  <= TXR_GAP;
              txr_gap_q <= 1'b1;
            end
          end
        end

        TXR_GAP: begin
          if (txr_gap_q) txr_gap_q <= 1'b0;
          else           txr_st_q  <= TXR_IDLE;
        end

        default: txr_st_q <= TXR_IDLE;
      endcase
    end
  end

  // ---- FLUSH, rmii side ------------------------------------------------------
  // While the request is up: finish the frame already on the port (a frame
  // that has started is never cut — it counts in TX_FRAMES), then discard every
  // frame the target says was committed before the FLUSH, then acknowledge.
  // Bounded: at most FRAME_DEPTH frames can be owed, and frames committed
  // AFTER the FLUSH are not owed — nothing new starts until the request has
  // dropped, and then they go out normally.
  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) begin
      txr_flush_sync_q <= 2'b00;
      txr_flush_q      <= 1'b0;
      txr_flush_ack_q  <= 1'b0;
      txr_fin_q        <= '0;
      txr_tgt_q        <= '0;
    end else begin
      txr_flush_sync_q <= {txr_flush_sync_q[0], tx_flush_req_q};
      txr_flush_q      <= txr_flush_sync_q[1];
      txr_fin_q        <= txr_fin_q + TCW'(txq_pop);
      // Captured on the SAME edge at which txr_flush_req first reads 1.
      if (txr_flush_rise) txr_tgt_q <= tx_flush_tgt_q;
      if (!txr_flush_req)
        txr_flush_ack_q <= 1'b0;
      else if (txr_idle && !txr_owed)
        txr_flush_ack_q <= 1'b1;
    end
  end

  // ===========================================================================
  // TX EVENT CDC — the RX side's toggle + 3-FF + edge-detect pattern, reversed
  // (rmii -> AXI). The counters live in the AXI domain, so TX_CTRL.CLR_CNT is a
  // local synchronous clear with no crossing. Events are >= 4 rmii cycles apart
  // by construction (TXR_GAP), i.e. >= 8 AXI cycles.
  // ===========================================================================
  logic txr_sent_tog_q, txr_dropd_tog_q, txr_desync_tog_q;

  always_ff @(posedge rmii_clk_i or negedge rmii_rst_n) begin
    if (!rmii_rst_n) begin
      txr_sent_tog_q   <= 1'b0;
      txr_dropd_tog_q  <= 1'b0;
      txr_desync_tog_q <= 1'b0;
    end else begin
      if (txr_sent)      txr_sent_tog_q   <= ~txr_sent_tog_q;
      if (txr_dropd)     txr_dropd_tog_q  <= ~txr_dropd_tog_q;
      if (txr_desync_ev) txr_desync_tog_q <= ~txr_desync_tog_q;
    end
  end

  (* ASYNC_REG = "TRUE" *) logic [2:0] tx_sent_sync_q, tx_dropd_sync_q, tx_desync_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      tx_sent_sync_q   <= 3'b000;
      tx_dropd_sync_q  <= 3'b000;
      tx_desync_sync_q <= 3'b000;
    end else begin
      tx_sent_sync_q   <= {tx_sent_sync_q[1:0],   txr_sent_tog_q};
      tx_dropd_sync_q  <= {tx_dropd_sync_q[1:0],  txr_dropd_tog_q};
      tx_desync_sync_q <= {tx_desync_sync_q[1:0], txr_desync_tog_q};
    end
  end

  wire tx_sent_pulse   = tx_sent_sync_q[2]   ^ tx_sent_sync_q[1];
  wire tx_dropd_pulse  = tx_dropd_sync_q[2]  ^ tx_dropd_sync_q[1];
  wire tx_desync_pulse = tx_desync_sync_q[2] ^ tx_desync_sync_q[1];

  // ---- TX counters (s_axi_aclk) ----------------------------------------------
  // Every COMMIT ends in exactly one of: TX_REJECT (refused at COMMIT, at
  // once), TX_FRAMES (handed to the bridge whole), or TX_FLUSHED (accepted,
  // then discarded by TX_CTRL.FLUSH before it started — amendment A2, decided
  // by david 2026-09-23; it was folded into TX_REJECT before). Hence the §3
  // invariant, as amended:
  //     TX_FRAMES + TX_REJECT + TX_FLUSHED + frames_queued == COMMITs since CLR_CNT
  // with frames_queued = FRAME_DEPTH - TX_SPACE[31:16] (a frame on the wire
  // still counts as queued until its tlast). REJ is "TX_REJECT has moved" —
  // a FLUSH does not set it. CLR_CNT clears first and this cycle's events then
  // count, so an event can never be lost to a coincident clear.
  logic [31:0] tx_frames_q, tx_reject_q, tx_flushed_q;
  logic        tx_rej_sticky_q, tx_desync_sticky_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      tx_frames_q        <= 32'd0;
      tx_reject_q        <= 32'd0;
      tx_flushed_q       <= 32'd0;
      tx_rej_sticky_q    <= 1'b0;
      tx_desync_sticky_q <= 1'b0;
    end else begin
      tx_frames_q        <= (tx_clr_cnt ? 32'd0 : tx_frames_q)
                            + {31'd0, tx_sent_pulse};
      tx_reject_q        <= (tx_clr_cnt ? 32'd0 : tx_reject_q)
                            + {31'd0, tx_commit_rej};                  // MUTATION-POINT(flushasreject)
      tx_flushed_q       <= (tx_clr_cnt ? 32'd0 : tx_flushed_q)
                            + {31'd0, tx_dropd_pulse};
      tx_rej_sticky_q    <= (tx_clr_cnt ? 1'b0 : tx_rej_sticky_q)
                            | tx_commit_rej;
      tx_desync_sticky_q <= (tx_clr_cnt ? 1'b0 : tx_desync_sticky_q)
                            | tx_desync_pulse;
    end
  end

  // ===========================================================================
  // Read mux. Every FIFO pop is gated on the DECODED, MAPPED register select
  // above, never on bare slv_reg_rden — an unmapped read must return 0 and pop
  // NOTHING.
  // ===========================================================================
  logic [31:0] rd_status, rd_level, rd_frame_len, rd_data;
  logic [31:0] rd_tx_status, rd_tx_space;

  always_comb begin
    rd_status                = 32'd0;
    rd_status[0]             = have_head_q;                 // FRAME_RDY
    rd_status[1]             = (data_rlevel == '0);         // EMPTY
    rd_status[2]             = data_full_sync_q[1];         // DATA_FULL
    rd_status[3]             = desc_full_sync_q[1];         // DESC_FULL
    rd_status[4]             = ovf_sticky_q;                // OVF   (sticky)
    rd_status[5]             = flush_q;                     // FLUSH_BUSY
    rd_status[6]             = desync_q;                    // DESYNC (sticky)
    rd_status[8]             = en_sync_q[1];                // EN, as the capture side sees it

    rd_level                 = 32'd0;
    rd_level[DAW:0]          = data_rlevel;
    rd_level[16+FAW:16]      = desc_rlevel;

    rd_frame_len             = 32'd0;
    rd_frame_len[15:0]       = have_head_q ? rem_q  : 16'd0;
    rd_frame_len[31:16]      = have_head_q ? hlen_q : 16'd0;

    rd_data                  = 32'd0;
    rd_data[7:0]             = data_avail ? data_rdata[7:0] : 8'd0;
    rd_data[8]               = data_avail;                  // VALID
    rd_data[9]               = data_avail && data_rdata[8]; // LAST

    // ---- inject side: every bit below is s_axi_aclk-domain state or an
    // already-synchronized crossing; no TX read has a side effect.
    rd_tx_status             = 32'd0;
    rd_tx_status[0]          = (16'(txd_free_eff) >= TX_MAX_RAW)       // ROOM: a MAX_FRAME
                               && (txq_free_eff != '0);                //   frame can be staged
    rd_tx_status[1]          = (txq_wfree == TXQ_DEPTH_W) && !txq_push_q; // EMPTY: nothing left to send
    rd_tx_status[2]          = (txd_free_eff == '0);                   // DATA_FULL
    rd_tx_status[3]          = (txq_free_eff == '0);                   // DESC_FULL
    rd_tx_status[4]          = tx_rej_sticky_q;                        // REJ    (sticky)
    rd_tx_status[5]          = tx_flush_busy;                          // FLUSH_BUSY
    rd_tx_status[6]          = (txs_len_q != 16'd0);                   // STAGING
    rd_tx_status[7]          = tx_desync_sticky_q;                     // DESYNC (sticky)

    rd_tx_space              = 32'd0;
    rd_tx_space[DAW:0]       = txd_free_eff;                           // free bytes
    rd_tx_space[16+FAW:16]   = txq_free_eff;                           // free frame slots
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      case (raddr_idx)
        IDX_CTRL:       axi_rdata_q <= {31'd0, en_q};
        IDX_STATUS:     axi_rdata_q <= rd_status;
        IDX_LEVEL:      axi_rdata_q <= rd_level;
        IDX_FRAME_LEN:  axi_rdata_q <= rd_frame_len;
        IDX_DATA:       axi_rdata_q <= rd_data;
        IDX_RX_FRAMES:  axi_rdata_q <= rx_frames_q;
        IDX_DROP_FULL:  axi_rdata_q <= drop_full_q;
        IDX_DROP_GIANT: axi_rdata_q <= drop_giant_q;
        IDX_TX_CTRL:    axi_rdata_q <= {23'd0, tx_raw_q, 8'd0};   // actions read 0
        IDX_TX_STATUS:  axi_rdata_q <= rd_tx_status;
        IDX_TX_SPACE:   axi_rdata_q <= rd_tx_space;
        IDX_TX_DATA:    axi_rdata_q <= 32'd0;   // write-only: reads 0, stages nothing
        IDX_TX_FRAMES:  axi_rdata_q <= tx_frames_q;
        IDX_TX_REJECT:  axi_rdata_q <= tx_reject_q;
        IDX_TX_FLUSHED: axi_rdata_q <= tx_flushed_q;              // MUTATION-POINT(noflushed)
        default:        axi_rdata_q <= 32'd0;   // unmapped: 0, and pops nothing
      endcase
    end
  end

endmodule
