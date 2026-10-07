// -----------------------------------------------------------------------------
// dutegr_cfifo.sv — dual-clock gray-pointer FIFO with COMMIT / ROLLBACK, for the
// DUT-egress frame capture block (fpga/shell/ip/dut_egress/dut_egress.sv).
//
// WHY A COMMIT POINTER AND NOT A PLAIN ASYNC FIFO
// -----------------------------------------------
// This FIFO carries Ethernet frames the DUT transmitted, and the reader (the
// MicroBlaze, over AXI4-Lite) must never be able to observe a TORN frame. A
// frame's length is not known until its last beat, so the only way to guarantee
// that is store-and-forward: write speculatively, and publish the write pointer
// to the read domain ONLY when a whole frame has landed. A frame that ran out of
// room — or that turned out to be a giant — is rolled back and never becomes
// visible at all; the caller counts it instead (dut_egress.sv's DROP_FULL /
// DROP_GIANT). A silently TRUNCATED frame would be worse than a dropped one: it
// looks like a real frame with corrupt bytes.
//
// Two pointers therefore live in the write domain:
//   wptr_q  — speculative. Advances on every accepted push.
//   wcmt_q  — committed. Advances only on wcommit_i, to wptr_q's next value.
//             THIS is the pointer that is gray-coded and crossed to the reader,
//             so the reader can only ever address bytes of complete frames.
// wrollback_i returns wptr_q to wcmt_q, discarding the partial frame. Rollback
// takes priority over a push in the same cycle.
//
// Safety of the shared memory: the reader addresses rptr_q < wcmt_r <= wcmt_q,
// and every write lands at an address >= wcmt_q. A rolled-back-and-rewritten
// address is therefore never an address the reader can reach.
//
// CDC: the canonical Cummings gray-pointer pattern, the same one
// fpga/shell/ip/uart_bridge/uartbr_async_fifo.sv uses (gray coding makes the
// multi-bit pointer safe to synchronize: at most one bit changes per update, so
// a metastable sample resolves to old-or-new, never to a phantom pointer).
// Both stages of both synchronizers carry (* ASYNC_REG *).
//
// The occupancy/vacancy outputs are deliberately CONSERVATIVE: each is computed
// from the OTHER domain's pointer as most recently synchronized, so `wfree_o`
// can only under-report free space and `rlevel_o` can only under-report
// available words. Neither can ever over-report, which is the direction that
// would corrupt.
//
// DEPTH must be a power of two and >= 4. Reset on each side is the caller's
// (async-assert / sync-deassert into that domain); dut_egress.sv derives both
// from one reset so the two sides cannot disagree.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module dutegr_cfifo #(
  parameter int WIDTH = 9,
  parameter int DEPTH = 2048   // power of two, >= 4
) (
  // ---------------------------------------------------------------------
  // Write side (producer domain)
  // ---------------------------------------------------------------------
  input  logic                    wclk_i,
  input  logic                    wrst_n_i,
  input  logic                    wpush_i,       // ignored while wfull_o
  input  logic [WIDTH-1:0]        wdata_i,
  input  logic                    wcommit_i,     // publish through this cycle's push
  input  logic                    wrollback_i,   // discard back to the commit point
  output logic                    wfull_o,       // no speculative room left
  output logic [$clog2(DEPTH):0]  wfree_o,       // speculative free words (conservative)

  // ---------------------------------------------------------------------
  // Read side (consumer domain)
  // ---------------------------------------------------------------------
  input  logic                    rclk_i,
  input  logic                    rrst_n_i,
  input  logic                    rpop_i,        // ignored while rlevel_o == 0
  output logic [WIDTH-1:0]        rdata_o,       // head word, first-word-fall-through
  output logic [$clog2(DEPTH):0]  rlevel_o       // COMMITTED words available
);

  localparam int AW = $clog2(DEPTH);

  logic [WIDTH-1:0] mem [0:DEPTH-1];

  // -- gray/binary helpers ------------------------------------------------
  function automatic logic [AW:0] bin2gray(input logic [AW:0] b);
    bin2gray = b ^ (b >> 1);
  endfunction

  function automatic logic [AW:0] gray2bin(input logic [AW:0] g);
    logic [AW:0] b;
    b[AW] = g[AW];
    for (int i = AW - 1; i >= 0; i--) b[i] = b[i+1] ^ g[i];
    gray2bin = b;
  endfunction

  // -- state (declared together: each domain's always_ff reads the OTHER
  //    domain's published gray pointer, so neither can be declared after both)
  logic [AW:0] wptr_q;        // speculative write pointer
  logic [AW:0] wcmt_q;        // committed write pointer (published)
  logic [AW:0] wcmt_gray_q;
  logic [AW:0] rptr_w_q;      // read pointer as seen in the write domain, binary
  logic [AW:0] rptr_q;
  logic [AW:0] rptr_gray_q;
  logic [AW:0] wcmt_r_q;      // committed write pointer as seen in read domain

  (* ASYNC_REG = "TRUE" *) logic [AW:0] rptr_gray_wsync0_q, rptr_gray_wsync1_q;
  (* ASYNC_REG = "TRUE" *) logic [AW:0] wcmt_gray_rsync0_q, wcmt_gray_rsync1_q;

  localparam logic [AW:0] DEPTH_W = (AW+1)'(DEPTH);

  // -- write domain -------------------------------------------------------

  wire        push       = wpush_i && !wfull_o;
  wire [AW:0] wptr_next  = wrollback_i ? wcmt_q
                                       : (wptr_q + {{AW{1'b0}}, push});

  // THE MEMORY GETS ITS OWN always_ff, WITH NO RESET CLAUSE AT ALL.
  //
  // This is not a stylistic split. A memory written inside an
  // `always_ff @(posedge clk or negedge rst_n)` cannot infer a block RAM -- a
  // BRAM write port has no asynchronous reset -- so the tool falls back to a
  // flop array. MEASURED out of context at the shipped 2048 x 9, with the write
  // inside the reset block: 19201 registers + 7808 LUTs and ZERO BRAM. Pointer
  // discipline, not a reset, is what makes stale contents unreachable (the same
  // reasoning as uartbr_async_fifo.sv's "memory has no reset").
  always_ff @(posedge wclk_i) begin
    if (push && !wrollback_i) mem[wptr_q[AW-1:0]] <= wdata_i;
  end

  always_ff @(posedge wclk_i or negedge wrst_n_i) begin
    if (!wrst_n_i) begin
      wptr_q             <= '0;
      wcmt_q             <= '0;
      wcmt_gray_q        <= '0;
      rptr_gray_wsync0_q <= '0;
      rptr_gray_wsync1_q <= '0;
      rptr_w_q           <= '0;
    end else begin
      wptr_q             <= wptr_next;
      if (wcommit_i && !wrollback_i) begin
        wcmt_q      <= wptr_next;
        wcmt_gray_q <= bin2gray(wptr_next);
      end
      rptr_gray_wsync0_q <= rptr_gray_q;
      rptr_gray_wsync1_q <= rptr_gray_wsync0_q;
      rptr_w_q           <= gray2bin(rptr_gray_wsync1_q);
    end
  end

  // Conservative vacancy: rptr_w_q lags, so this can only under-report.
  assign wfree_o = DEPTH_W - (wptr_q - rptr_w_q);
  assign wfull_o = (wfree_o == '0);

  // -- read domain --------------------------------------------------------
  wire        pop       = rpop_i && (rlevel_o != '0);
  wire [AW:0] rptr_next = rptr_q + {{AW{1'b0}}, pop};

  always_ff @(posedge rclk_i or negedge rrst_n_i) begin
    if (!rrst_n_i) begin
      rptr_q             <= '0;
      rptr_gray_q        <= '0;
      wcmt_gray_rsync0_q <= '0;
      wcmt_gray_rsync1_q <= '0;
      wcmt_r_q           <= '0;
    end else begin
      rptr_q             <= rptr_next;
      rptr_gray_q        <= bin2gray(rptr_next);
      wcmt_gray_rsync0_q <= wcmt_gray_q;
      wcmt_gray_rsync1_q <= wcmt_gray_rsync0_q;
      wcmt_r_q           <= gray2bin(wcmt_gray_rsync1_q);
    end
  end

  // Conservative occupancy: wcmt_r_q lags, so this can only under-report.
  assign rlevel_o = wcmt_r_q - rptr_q;

  // First-word-fall-through head, via a REGISTERED read so `mem` infers a
  // block RAM instead of distributed RAM.
  //
  // rdata_q is loaded every cycle from mem[rptr_next], and rptr_q(T) IS
  // rptr_next(T-1) -- so rdata_q(T) == mem[rptr_q(T)], the head word, with no
  // extra latency at the pop. While nothing is popping, rptr_next is constant
  // and the same address is re-read every cycle, so a word committed after the
  // pointer stopped there still appears (one rclk later, and the reader cannot
  // look before rlevel_o rises, which is >= 2 sync cycles behind the commit).
  // Undefined while rlevel_o == 0 -- the caller must gate on rlevel_o, which
  // dut_egress.sv does.
  //
  // WHY THIS MATTERS AT THE SHIPPED SIZE: the data FIFO is 2048 x 9. A
  // COMBINATIONAL mem[rptr_q] read cannot map to a BRAM at all, so this one
  // line is the difference between one RAMB18 and several hundred LUTs of
  // cascaded distributed RAM -- plus the mux delay that comes with it.
  logic [WIDTH-1:0] rdata_q;

  always_ff @(posedge rclk_i) begin
    rdata_q <= mem[rptr_next[AW-1:0]];
  end

  assign rdata_o = rdata_q;

endmodule
