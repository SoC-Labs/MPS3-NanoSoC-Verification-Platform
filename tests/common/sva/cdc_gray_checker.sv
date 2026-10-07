// -----------------------------------------------------------------------------
// cdc_gray_checker.sv — CDC assertions for the UARTBR dual-clock gray-pointer
// FIFO (fpga/shell/ip/uart_bridge/uartbr_async_fifo.sv).
//
// A5 verification infrastructure (tests/common/sva/). Compiled alongside the
// FIFO and attached with `bind uartbr_async_fifo cdc_gray_checker ...` (see
// tests/common/sva/bind_uart_bridge.sv), so it binds into ALL FIVE FIFO
// instances the bridge holds (U0/U1 tx+rx, SWO rx) with no RTL edit. A firing
// assertion $error()s then $fatal()s (use +SVA_NOFATAL to collect every
// firing).
//
// THE headline CDC property: a gray-coded pointer that crosses a clock domain
// must change by AT MOST ONE BIT per update, so a 2-FF synchronizer sampling it
// mid-transition can only resolve to the old or the new value — never a phantom
// pointer. This checker asserts exactly that on the two LOCALLY-generated gray
// pointers (wptr_gray_q in the write domain, rptr_gray_q in the read domain),
// which advance by one binary step per push/pop and must therefore be one-hot
// in their delta. (The *synchronized* remote pointers are deliberately NOT
// checked for one-bit deltas: with unrelated clocks a synchronized pointer may
// legitimately jump several gray steps between local samples — that is the CDC
// working, not a bug.)
//
// It also pins the FIFO's overflow/underflow SAFETY: a push while full must not
// advance the write pointer, and a pop while empty must not advance the read
// pointer (the "silently ignore" policy uart_bridge.sv relies on).
//
// Signal names below are the FIFO's own internal nets (wptr_gray_q etc.) —
// reachable because `bind` gives the checker hierarchical visibility into the
// bound instance's scope.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module cdc_gray_checker #(
  parameter int    PTR_WIDTH = 5,   // AW+1 for the bound FIFO (DEPTH=16 -> AW=4)
  parameter bit    COVER_EN  = 1'b1,
  parameter string NAME      = "uartbr_fifo"
) (
  input logic                 wclk_i,
  input logic                 wrst_n_i,
  input logic                 wr_en_i,
  input logic                 full_o,
  input logic [PTR_WIDTH-1:0] wptr_gray_q,
  input logic [PTR_WIDTH-1:0] wptr_bin_q,

  input logic                 rclk_i,
  input logic                 rrst_n_i,
  input logic                 rd_en_i,
  input logic                 empty_o,
  input logic [PTR_WIDTH-1:0] rptr_gray_q,
  input logic [PTR_WIDTH-1:0] rptr_bin_q
);

  task automatic fail(input string why);
    $error("[SVA CDC %s] %s (t=%0t)", NAME, why, $time);
    if (!$test$plusargs("SVA_NOFATAL"))
      $fatal(1, "[SVA CDC %s] gray-pointer / FIFO assertion failed", NAME);
  endtask

  // -- Gray-code one-bit-change invariant (the CDC-safety property). --------
  a_wptr_gray_onehot_delta: assert property (@(posedge wclk_i) disable iff (!wrst_n_i)
    ($countones(wptr_gray_q ^ $past(wptr_gray_q)) <= 1))
    else fail($sformatf("write gray pointer changed by >1 bit: %b -> %b",
                        $past(wptr_gray_q), wptr_gray_q));

  a_rptr_gray_onehot_delta: assert property (@(posedge rclk_i) disable iff (!rrst_n_i)
    ($countones(rptr_gray_q ^ $past(rptr_gray_q)) <= 1))
    else fail($sformatf("read gray pointer changed by >1 bit: %b -> %b",
                        $past(rptr_gray_q), rptr_gray_q));

  // -- Gray/binary consistency: the registered gray pointer must always equal
  //    the gray encoding of the registered binary pointer. --------------------
  a_wptr_gray_matches_bin: assert property (@(posedge wclk_i) disable iff (!wrst_n_i)
    (wptr_gray_q == ((wptr_bin_q >> 1) ^ wptr_bin_q)))
    else fail("write gray pointer inconsistent with its binary pointer");
  a_rptr_gray_matches_bin: assert property (@(posedge rclk_i) disable iff (!rrst_n_i)
    (rptr_gray_q == ((rptr_bin_q >> 1) ^ rptr_bin_q)))
    else fail("read gray pointer inconsistent with its binary pointer");

  // -- Overflow/underflow safety: full-push / empty-pop must not move a ptr. --
  a_no_push_when_full: assert property (@(posedge wclk_i) disable iff (!wrst_n_i)
    ((full_o && wr_en_i) |=> $stable(wptr_bin_q)))
    else fail("write pointer advanced on a push while FULL");
  a_no_pop_when_empty: assert property (@(posedge rclk_i) disable iff (!rrst_n_i)
    ((empty_o && rd_en_i) |=> $stable(rptr_bin_q)))
    else fail("read pointer advanced on a pop while EMPTY");

  // ===========================================================================
  // FUNCTIONAL COVER POINTS (A5 constrained-random wave). `cover property`
  // directives — never fail the bench; VCS counts hits and urg tabulates them.
  // The CDC-stress scenarios that matter: the FIFO reaching FULL and EMPTY, the
  // gray pointers WRAPPING (MSB region toggle — the case a same-ratio bench of
  // only a few bytes never reaches), and the drop-on-full / ignore-on-empty
  // safety paths actually being taken. A cover point at 0 hits means the CDC
  // was never pushed into that corner.
  // ===========================================================================
  if (COVER_EN) begin : g_cover
    // FULL / EMPTY reached (write- and read-domain respectively).
    c_full:  cover property (@(posedge wclk_i) disable iff (!wrst_n_i) (full_o));
    c_empty: cover property (@(posedge rclk_i) disable iff (!rrst_n_i) (empty_o));

    // Pointer wrap: a push/pop at the last slot before the index rolls over
    // (all low-order bits set — the gray MSB is about to flip).
    c_wptr_wrap: cover property (@(posedge wclk_i) disable iff (!wrst_n_i)
      (wr_en_i && !full_o && (&wptr_bin_q[PTR_WIDTH-2:0])));
    c_rptr_wrap: cover property (@(posedge rclk_i) disable iff (!rrst_n_i)
      (rd_en_i && !empty_o && (&rptr_bin_q[PTR_WIDTH-2:0])));

    // Safety paths taken: a push while FULL (dropped) and a pop while EMPTY.
    c_push_when_full: cover property (@(posedge wclk_i) disable iff (!wrst_n_i)
      (full_o && wr_en_i));
    c_pop_when_empty: cover property (@(posedge rclk_i) disable iff (!rrst_n_i)
      (empty_o && rd_en_i));
  end

endmodule
