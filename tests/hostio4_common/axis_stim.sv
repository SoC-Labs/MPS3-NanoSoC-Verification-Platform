// axis_stim.sv — self-checking AXIS byte source/sink shared by the hostio4
// benches (tests/hostio4_link_cdc, tests/hostio4_hotswap).
//
// There is no golden file. Each channel carries a 32-bit LFSR stream with its
// own seed; the sink regenerates the sequence from the same seed. That catches
// a dropped byte, a duplicated byte, a corrupted byte, a channel swap and a
// loopback -- and it never drifts.
//
// Both models reset from `resetn`. In the hot-swap bench that is a stimulus
// reset distinct from either DUT's reset, so a swap can restart both ends'
// sequences together while the experiment stays about the DUTs.

`timescale 1ns/1ps

// 32-bit maximal-length LFSR, taps 32,22,2,1. Shared so source and sink cannot
// disagree about the sequence.
`define HOSTIO4_LFSR_NEXT(s) {s[30:0], s[31] ^ s[21] ^ s[1] ^ s[0]}

// ---------------------------------------------------------------------------
// Byte source: emits `nbytes` from the LFSR with random idle gaps.
// tvalid is never retracted without a handshake (AXIS rule).
// ---------------------------------------------------------------------------
module axis_src #(
    parameter int unsigned SEED  = 32'h1,
    parameter int unsigned GSEED = 32'hACE1
) (
    input  wire         clk,
    input  wire         resetn,
    input  wire  [31:0] nbytes,
    output wire         tvalid,
    output wire  [7:0]  tdata8,
    input  wire         tready,
    output int unsigned sent
);
  function automatic int unsigned nxt(input int unsigned s);
    nxt = `HOSTIO4_LFSR_NEXT(s);
  endfunction

  int unsigned lfsr, grnd;
  logic        vld;

  assign tvalid = vld;
  assign tdata8 = lfsr[7:0];   // stable while vld: lfsr only moves on handshake

  wire gap_ok = (grnd[1:0] != 2'b00);   // ~75% offered load

  always_ff @(posedge clk or negedge resetn) begin
    if (!resetn) begin
      lfsr <= SEED; grnd <= GSEED; vld <= 1'b0; sent <= 0;
    end else begin
      grnd <= nxt(grnd);
      if (tvalid && tready) begin
        sent <= sent + 1;
        lfsr <= nxt(lfsr);
        vld  <= ((sent + 1) < nbytes) ? gap_ok : 1'b0;
      end else if (!vld && (sent < nbytes)) begin
        vld  <= gap_ok;
      end
    end
  end
endmodule

// ---------------------------------------------------------------------------
// Byte sink: regenerates the expected stream and compares. Randomly deasserts
// tready (legal, and exercises backpressure).
//
// `drain` consumes and DISCARDS bytes without comparing or counting. The
// hot-swap bench needs it: when a reconfiguration is simulated mid-stream the
// link still holds part-assembled bytes, and those must be flushed before the
// post-swap sequence is scored. Without it a leftover byte pops out first and
// looks like corruption.
// ---------------------------------------------------------------------------
module axis_sink #(
    parameter int unsigned SEED  = 32'h1,
    parameter int unsigned GSEED = 32'hBEEF,
    parameter string       NAME  = "sink"
) (
    input  wire         clk,
    input  wire         resetn,
    input  wire         drain,
    input  wire         tvalid,
    input  wire  [7:0]  tdata8,
    output wire         tready,
    output int unsigned got,
    output int unsigned errs
);
  function automatic int unsigned nxt(input int unsigned s);
    nxt = `HOSTIO4_LFSR_NEXT(s);
  endfunction

  int unsigned lfsr, grnd;
  logic        rdy;

  assign tready = drain ? 1'b1 : rdy;

  always_ff @(posedge clk or negedge resetn) begin
    if (!resetn) begin
      lfsr <= SEED; grnd <= GSEED; rdy <= 1'b0; got <= 0; errs <= 0;
    end else begin
      grnd <= nxt(grnd);
      rdy  <= (grnd[1:0] != 2'b00);   // ~75% ready
      if (tvalid && tready && !drain) begin
        if (tdata8 !== lfsr[7:0]) begin
          if (errs < 8)
            $display("    %-10s byte %0d: got 0x%02h expected 0x%02h",
                     NAME, got, tdata8, lfsr[7:0]);
          errs <= errs + 1;
        end
        lfsr <= nxt(lfsr);
        got  <= got + 1;
      end
    end
  end
endmodule
