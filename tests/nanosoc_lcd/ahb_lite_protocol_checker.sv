// ahb_lite_protocol_checker.sv — a reusable AHB-Lite SLAVE protocol checker,
// bound onto the DUT-side accelerator in the `nanosoc_exp` socket.
//
// Mirrors tests/common/sva/axi4lite_protocol_checker.sv in spirit and in
// failure style ($fatal unless +SVA_NOFATAL), but for AHB-Lite. It lives under
// tests/nanosoc_lcd/ (this bench owns it) rather than tests/common/sva/ because
// tests/common/ is a shared directory this agent does not own. It is a
// candidate to promote to tests/common/sva/ once a second AHB-Lite slave bench
// exists — nothing in it is specific to `ahb_clcd`.
//
// WHAT IT CHECKS — and why each one is here
// -----------------------------------------
// The socket README's §4 is a single screaming rule:
//
//     hreadyout MUST ALWAYS, EVENTUALLY, GO HIGH
//
// because `exp_*` is a real hole in nanosoc's bus matrix. A slave that holds
// hreadyout low stalls the AHB bus, which stalls the Cortex-M0, and the board
// simply STOPS: no exception, no watchdog, nothing on screen. That rule is the
// reason this checker exists, and A_LIVENESS / A_IDLE_READY / A_RESET_READY
// below are three complementary ways of catching a violation of it.
//
// Note the checker is written so that the CORRECT (zero-wait-state) slave the
// contract asks for passes every property TRIVIALLY — `assign hreadyout = 1'b1`
// satisfies all three. That is intentional and is not a weakness: the checker's
// job is to fail LOUDLY for the next student who "optimises" the block by adding
// `hreadyout = !fifo_full`, which is precisely the deadlock §4 forbids.
`timescale 1ns / 1ps

module ahb_lite_protocol_checker #(
  parameter int    ADDR_W    = 32,
  parameter int    DATA_W    = 32,
  // Max consecutive cycles hreadyout may be low. The contract's reference block
  // is zero-wait-state (hreadyout is a constant 1), so any low cycle at all is
  // already a deviation; MAX_WAIT is generous so that a *legal* bounded-wait
  // slave still passes, while an unbounded stall (the deadlock) always fires.
  parameter int    MAX_WAIT  = 16,
  // Set 1 only if the slave deliberately implements the two-cycle ERROR
  // response. The contract says drive OKAY (§3: "drive 0").
  parameter bit    ALLOW_ERR = 1'b0,
  parameter string NAME      = "ahb"
) (
  input logic              hclk,
  input logic              hresetn,
  input logic              hsel,
  input logic [ADDR_W-1:0] haddr,
  input logic [1:0]        htrans,
  input logic              hwrite,
  input logic [DATA_W-1:0] hwdata,
  input logic              hready,      // GLOBAL bus ready (matrix -> slave)
  input logic [DATA_W-1:0] hrdata,
  input logic              hreadyout,   // THIS slave's ready
  input logic              hresp
);

  localparam logic [1:0] TRANS_IDLE   = 2'b00;
  localparam logic [1:0] TRANS_BUSY   = 2'b01;
  localparam logic [1:0] TRANS_NONSEQ = 2'b10;

  // A real (non-IDLE/BUSY) transfer addressed at us, in its ADDRESS phase, on a
  // cycle the bus is actually advancing.
  wire addr_phase = hsel && htrans[1] && hready;

  // ---------------------------------------------------------------------------
  // Track OUR pending data phase. AHB-Lite is pipelined: the transfer whose
  // hwdata/hrdata is on the bus THIS cycle is the one whose address was on the
  // bus LAST cycle. hsel may already have moved to another slave by then, so a
  // naive `!hsel |-> hreadyout` would be WRONG. This registered copy is the
  // correct qualifier.
  // ---------------------------------------------------------------------------
  logic dp_sel_q;   // a data phase for THIS slave is in progress
  logic dp_wr_q;    // ...and it is a write

  always_ff @(posedge hclk or negedge hresetn) begin
    if (!hresetn) begin
      dp_sel_q <= 1'b0;
      dp_wr_q  <= 1'b0;
    end else if (hready) begin
      dp_sel_q <= hsel && htrans[1];
      dp_wr_q  <= hwrite;
    end
  end

  // Count consecutive low cycles of hreadyout (the stall detector).
  logic [31:0] low_run_q;
  always_ff @(posedge hclk or negedge hresetn) begin
    if (!hresetn)          low_run_q <= 32'd0;
    else if (hreadyout)    low_run_q <= 32'd0;
    else                   low_run_q <= low_run_q + 32'd1;
  end

  // ===========================================================================
  // THE ONE RULE (§4), three ways.
  // ===========================================================================

  // 1. Liveness: hreadyout is never low for more than MAX_WAIT cycles in a row.
  //    This is the direct statement of "eventually goes high" made checkable in
  //    a finite simulation. An unbounded FIFO-backpressure stall trips it.
  A_LIVENESS: assert property (@(posedge hclk) disable iff (!hresetn)
      (low_run_q <= MAX_WAIT))
    else er($sformatf("%s: hreadyout LOW for %0d consecutive cycles (> MAX_WAIT=%0d) -- the AHB bus is STALLED. On silicon this hangs the Cortex-M0 dead. See fpga/rp/nanosoc_exp/README.md SS4.", NAME, low_run_q, MAX_WAIT));

  // 2. A slave with no data phase of its own in flight must NEVER hold the bus.
  //    (§4: "When you are not selected, drive hreadyout = 1. Always.")
  A_IDLE_READY: assert property (@(posedge hclk) disable iff (!hresetn)
      (!dp_sel_q) |-> hreadyout)
    else er($sformatf("%s: hreadyout=0 with NO data phase pending for this slave -- a deselected slave is holding the whole bus. See SS4.", NAME));

  // 3. hreadyout must come OUT OF RESET high (§2 reset values: "hreadyout
  //    resets to 1, not 0"). A slave that resets to 0 and is then never selected
  //    still hangs the matrix the moment hready is routed through it.
  A_RESET_READY: assert property (@(posedge hclk) (!hresetn) |-> hreadyout)
    else er($sformatf("%s: hreadyout is not 1 during reset -- it MUST reset to 1 (SS2).", NAME));

  // ===========================================================================
  // Ordinary AHB-Lite legality.
  // ===========================================================================

  // OKAY-only unless the slave opted into ERROR responses.
  if (!ALLOW_ERR) begin : g_okay
    A_RESP_OKAY: assert property (@(posedge hclk) disable iff (!hresetn)
        (!hresp))
      else er($sformatf("%s: hresp=ERROR, but the block does not implement the two-cycle ERROR response (SS3: 'drive 0').", NAME));
  end

  // Read data must be resolved on the cycle the slave completes a read data
  // phase — that is the cycle the master samples it.
  A_RDATA_KNOWN: assert property (@(posedge hclk) disable iff (!hresetn)
      (dp_sel_q && !dp_wr_q && hreadyout) |-> !$isunknown(hrdata))
    else er($sformatf("%s: hrdata has X/Z on the cycle a read data phase completes.", NAME));

  // The control signals the slave decodes must never be X while a transfer is
  // being presented to it.
  A_CTRL_KNOWN: assert property (@(posedge hclk) disable iff (!hresetn)
      hready |-> (!$isunknown(hsel) && !$isunknown(htrans)))
    else er($sformatf("%s: hsel/htrans have X/Z while the bus is advancing.", NAME));

  A_ADDR_KNOWN: assert property (@(posedge hclk) disable iff (!hresetn)
      addr_phase |-> (!$isunknown(haddr) && !$isunknown(hwrite)))
    else er($sformatf("%s: haddr/hwrite have X/Z in a live address phase.", NAME));

  // hreadyout itself must always be a resolved 0 or 1 — an X here propagates
  // into the matrix's hready and poisons every other slave.
  A_READYOUT_KNOWN: assert property (@(posedge hclk) disable iff (!hresetn)
      !$isunknown(hreadyout))
    else er($sformatf("%s: hreadyout is X/Z.", NAME));

  // A BUSY transfer is only legal inside a burst (after a NONSEQ). The M0/DMAC
  // never issue a bare BUSY, and a slave that treats BUSY as a real transfer
  // would double-push a FIFO byte — worth catching if a future bench drives it.
  A_NO_LEADING_BUSY: assert property (@(posedge hclk) disable iff (!hresetn)
      (hsel && hready && (htrans == TRANS_BUSY)) |-> $past(hsel && hready && htrans[1], 1))
    else er($sformatf("%s: BUSY transfer with no preceding live transfer (illegal AHB-Lite).", NAME));

  // ---------------------------------------------------------------------------
  // Failure style: $fatal by default (a firing assertion turns the bench RED),
  // +SVA_NOFATAL downgrades to $error so a run can collect every firing. Same
  // convention as tests/common/sva/axi4lite_protocol_checker.sv.
  // ---------------------------------------------------------------------------
  bit nofatal;
  initial nofatal = $test$plusargs("SVA_NOFATAL");

  function automatic void er(input string msg);
    if (nofatal) $error("[SVA] %s", msg);
    else         $fatal(1, "[SVA] %s", msg);
  endfunction

endmodule
