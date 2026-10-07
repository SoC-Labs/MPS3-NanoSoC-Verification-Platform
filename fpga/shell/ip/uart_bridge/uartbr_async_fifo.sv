// -----------------------------------------------------------------------------
// uartbr_async_fifo.sv — dual-clock (gray-pointer) byte FIFO for the UARTBR
// console-stream bridge (fpga/shell/ip/uart_bridge/uart_bridge.sv).
//
// THIS is where the partition-boundary CDC for the console streams lives,
// per docs/contracts/partition-pins.md "Clock/reset domain rule": "Every
// crossing is CDC'd on the static side. The RM sees already-safe signals;
// shell owns the synchronizers/async FIFOs (clkrst + per-block)." One
// instance per console stream direction (5 total in uart_bridge.sv:
// U0 tx/rx, U1 tx/rx, SWO rx) crosses dut_clk <-> s_axi_aclk.
//
// Structure: the canonical Cummings async-FIFO pattern —
//   - binary + gray-coded write/read pointers, one extra MSB for the
//     full/empty distinction;
//   - each side's gray pointer crosses into the other domain through a
//     2-FF synchronizer (gray coding guarantees at most one bit changes
//     per update, so a metastable sample can only resolve to the old or
//     the new value — never to a phantom pointer);
//   - full/empty are REGISTERED in their own domain from the *next* local
//     gray pointer vs the *synchronized* remote one, so both flags are
//     conservative (may pessimistically assert one remote update late,
//     never optimistically deassert early);
//   - memory has no reset (pointer discipline makes stale contents
//     unreachable); the head word is read combinationally
//     (first-word-fall-through), which lets uart_bridge.sv capture
//     {valid, data} and pop in the same cycle an AXI-Lite read commits.
//
// Reset: each side takes its OWN active-low reset (async-assert /
// sync-deassert is the caller's job — uart_bridge.sv feeds the write and
// read sides resets that are asserted together and released synchronized
// into each destination domain, so the two sides can never disagree about
// pointer state across a reset).
//
// Constraints on parameters: DEPTH must be a power of two and >= 4 (the
// {~msb2, lsbs} full-comparison below needs AW >= 2).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module uartbr_async_fifo #(
  parameter int WIDTH = 8,
  parameter int DEPTH = 16   // power of two, >= 4
) (
  // ---------------------------------------------------------------------
  // Write side (producer domain)
  // ---------------------------------------------------------------------
  input  logic             wclk_i,
  input  logic             wrst_n_i,   // active-low, write domain
  input  logic             wr_en_i,    // push request (ignored while full_o)
  input  logic [WIDTH-1:0] wr_data_i,
  output logic             full_o,

  // ---------------------------------------------------------------------
  // Read side (consumer domain)
  // ---------------------------------------------------------------------
  input  logic             rclk_i,
  input  logic             rrst_n_i,   // active-low, read domain
  input  logic             rd_en_i,    // pop request (ignored while empty_o)
  output logic [WIDTH-1:0] rd_data_o,  // head word, first-word-fall-through
  output logic             empty_o
);

  localparam int AW = $clog2(DEPTH);

  logic [WIDTH-1:0] mem [0:DEPTH-1];

  // Local pointers (binary for addressing, gray for crossing).
  logic [AW:0] wptr_bin_q, wptr_gray_q;
  logic [AW:0] rptr_bin_q, rptr_gray_q;

  // Remote gray pointers, 2-FF synchronized into the local domain.
  // R4 CDC: (* ASYNC_REG = "TRUE" *) on BOTH stages of each gray-pointer
  // synchronizer -- these are THE metastability-critical flops of the
  // dual-clock FIFO (the SVA cdc_gray_checker binds here). Gray coding makes
  // the MULTI-BIT pointer safe to synchronize (<=1 bit changes per update, so
  // a metastable sample resolves to old-or-new, never a phantom pointer), so
  // no handshake is needed -- but the flops still need the attribute for MTBF
  // and to clear TIMING-10.
  (* ASYNC_REG = "TRUE" *) logic [AW:0] rptr_gray_wsync0_q, rptr_gray_wsync1_q;  // read ptr -> write domain
  (* ASYNC_REG = "TRUE" *) logic [AW:0] wptr_gray_rsync0_q, wptr_gray_rsync1_q;  // write ptr -> read domain

  // Qualified push/pop (a full push / empty pop is silently ignored — the
  // caller polls full_o/empty_o or uses the per-stream policy documented
  // in uart_bridge.sv).
  wire push = wr_en_i && !full_o;
  wire pop  = rd_en_i && !empty_o;

  wire [AW:0] wptr_bin_next  = wptr_bin_q + {{AW{1'b0}}, push};
  wire [AW:0] wptr_gray_next = (wptr_bin_next >> 1) ^ wptr_bin_next;
  wire [AW:0] rptr_bin_next  = rptr_bin_q + {{AW{1'b0}}, pop};
  wire [AW:0] rptr_gray_next = (rptr_bin_next >> 1) ^ rptr_bin_next;

  // -- Memory (write domain; no reset — see header) --
  always_ff @(posedge wclk_i) begin
    if (push) mem[wptr_bin_q[AW-1:0]] <= wr_data_i;
  end

  // -- Write domain: pointer, remote-pointer sync, registered full --
  always_ff @(posedge wclk_i or negedge wrst_n_i) begin
    if (!wrst_n_i) begin
      wptr_bin_q         <= '0;
      wptr_gray_q        <= '0;
      rptr_gray_wsync0_q <= '0;
      rptr_gray_wsync1_q <= '0;
      full_o             <= 1'b0;
    end else begin
      wptr_bin_q         <= wptr_bin_next;
      wptr_gray_q        <= wptr_gray_next;
      rptr_gray_wsync0_q <= rptr_gray_q;
      rptr_gray_wsync1_q <= rptr_gray_wsync0_q;
      // Full when the next write-gray equals the synchronized read-gray
      // with the two MSBs inverted (the gray-code "wrapped once more"
      // condition).
      full_o <= (wptr_gray_next ==
                 {~rptr_gray_wsync1_q[AW:AW-1], rptr_gray_wsync1_q[AW-2:0]});
    end
  end

  // -- Read domain: pointer, remote-pointer sync, registered empty --
  always_ff @(posedge rclk_i or negedge rrst_n_i) begin
    if (!rrst_n_i) begin
      rptr_bin_q         <= '0;
      rptr_gray_q        <= '0;
      wptr_gray_rsync0_q <= '0;
      wptr_gray_rsync1_q <= '0;
      empty_o            <= 1'b1;
    end else begin
      rptr_bin_q         <= rptr_bin_next;
      rptr_gray_q        <= rptr_gray_next;
      wptr_gray_rsync0_q <= wptr_gray_q;
      wptr_gray_rsync1_q <= wptr_gray_rsync0_q;
      // Empty when the next read-gray catches the synchronized write-gray.
      empty_o <= (rptr_gray_next == wptr_gray_rsync1_q);
    end
  end

  // First-word-fall-through head (combinational; stale/undefined while
  // empty_o — the caller must gate on empty_o, which uart_bridge.sv does).
  assign rd_data_o = mem[rptr_bin_q[AW-1:0]];

endmodule
