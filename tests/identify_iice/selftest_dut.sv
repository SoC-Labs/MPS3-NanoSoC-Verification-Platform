// =============================================================================
// tests/identify_iice/selftest_dut.sv
//
// The CI-gate DUT for the sim-vs-hardware IICE trace harness. Deliberately
// TINY: the real nanosoc reference bench takes minutes-to-tens-of-minutes to
// reach a functional gate, which is far too slow to gate the harness on. This
// DUT reaches its gate in ~300 clocks (~3 us) so `make check` runs in seconds.
//
// OWNED BY STREAM B (INTERFACES.md §5). Contract with STREAM A: this module
// exposes, AT THE INSTANCE LEVEL, exactly these probe signals, because
// signals_selftest.yaml references them by the hierarchical paths
// `tb.u_dut.<name>`:
//
//     clk     1     the sample clock (manifest iice.clock.sim = tb.u_dut.clk)
//     haddr  32     AHB-Lite address
//     htrans  2     AHB-Lite transfer type
//     hwrite  1     AHB-Lite direction
//     hrdata 32     AHB-Lite read data          <-- deliberately X early on
//     hready  1     AHB-Lite ready              <-- goes low (wait states)
//     state   3     small FSM state
//     lockup  1     normally 0
//
// That list is FROZEN. Renaming or re-widening any of them silently breaks the
// generated shadow module (which uses absolute hierarchical references, so a
// typo is a compile error in Stream A's file, not here) and the compare
// pipeline. `tests/test_sim_smoke.py` parses this file to defend the names.
// `rst_n` and `done` are extra ports, not part of the probe set.
//
// BEHAVIOUR — a plausible AHB-Lite master walking addresses in bursts of 4
// (NONSEQ, SEQ, SEQ, SEQ, IDLE), with deterministic wait states.
//
// THE TRIGGER. The selftest trigger condition is
//
//     htrans == 2'b10 && haddr == 32'h0000_0040
//
// and it must occur EXACTLY ONCE, ROUGHLY MID-RUN. Both properties are load
// bearing and both are asserted by tb_selftest.sv:
//
//   * exactly once, because the offline cropper keys the window on the
//     trigger marker; several markers would make "the" trigger time ambiguous,
//     whereas a real IICE arms, fires once, fills its post-trigger half and
//     stops. Note real AHB HOLDS htrans/haddr through a wait state, so a
//     wait-stated NONSEQ at 0x40 would assert the condition for several
//     consecutive cycles. The address plan below therefore places 0x40 on a
//     beat with ZERO wait states (see beat_waits()).
//   * mid-run, because `trigger_time: middle` crops depth/2 pre-trigger and
//     depth/2 post-trigger samples. A trigger near reset leaves the
//     pre-trigger half empty and the comparison then proves nothing.
//
// The address plan is three phases, so that address 0x40 is reachable ONCE and
// at the mid-point rather than at beat 16 of the run:
//
//   phase 0   80 beats   0x0000_0100 .. 0x0000_023C   pre-trigger history
//   phase 1   64 beats   0x0000_0000 .. 0x0000_00FC   contains 0x40 at idx 16
//   phase 2   48 beats   0x0000_0200 .. 0x0000_02BC   post-trigger history
//
// 0x40 appears in phase 1 only, at index 16, which is 16 % 4 == 0 => the first
// beat of a burst => htrans == NONSEQ. Phases 0 and 2 never generate 0x40.
//
// X-INJECTION (deliberate, do not "fix"). The compare pipeline's X-masking rule
// -- sim-X vs hardware-0/1 is don't-care, one way only (INTERFACES.md §4) -- is
// otherwise dead code that nothing in the gate ever executes. A masking rule
// that is never exercised is a masking rule that is not known to work.
//
// There are TWO X regions, and the second one is the one that matters:
//
//   1. RESET-PHASE X. `hrdata` is the one signal with no reset value, so it is
//      X from time 0 until the first read data phase (~7 clocks in). This is
//      the realistic one -- it is what a real reset looks like.
//
//   2. MID-RUN X, inside the cropped window. Region 1 is NOT enough, and the
//      first version of this file got that wrong. The offline cropper keeps
//      only `depth` samples centred on the trigger: with the selftest
//      manifest's depth 64 / trigger_time middle and the trigger at cycle 149,
//      the compared window is [117..180]. Region 1 lives at cycles ~0-12 and is
//      therefore thrown away before the comparison, so the live report read
//      "X-masked samples 0 (0.00%)" -- the masking rule was unit-tested but
//      never executed end-to-end on FSDB data.
//
//      So two READ beats in phase 1 get NO slave response (beat_no_response()),
//      leaving `hrdata` X for a couple of samples at ~cycle 155 -- comfortably
//      inside [117..180] and safely after the trigger at 149. `hrdata` is not
//      part of the trigger expression, so this cannot perturb the trigger.
//
// tb_selftest.sv ASSERTS that an X sample actually lands inside the window,
// computed from the trigger cycle rather than hardcoded -- otherwise this whole
// arrangement silently rots the moment the address plan or the depth changes,
// which is exactly how region 1 came to be useless.
//
// `lockup` is 0 for the whole functional run and pulses high for 2 cycles at
// the very end. A permanently-constant probe would be compared trivially and
// could not distinguish a working comparator from a broken one.
// =============================================================================
`timescale 1ns/1ps

module selftest_dut (
  input  logic        clk,
  input  logic        rst_n,
  output logic        done
);

  // --- AHB-Lite HTRANS encoding ---------------------------------------------
  localparam logic [1:0] TRANS_IDLE   = 2'b00;
  localparam logic [1:0] TRANS_BUSY   = 2'b01;   // never issued (no bursts held)
  localparam logic [1:0] TRANS_NONSEQ = 2'b10;
  localparam logic [1:0] TRANS_SEQ    = 2'b11;

  // --- FSM state encoding (3 bits -- `state` is a probed signal) ------------
  localparam logic [2:0] ST_RESET  = 3'd0;
  localparam logic [2:0] ST_IDLE   = 3'd1;
  localparam logic [2:0] ST_NONSEQ = 3'd2;
  localparam logic [2:0] ST_SEQ    = 3'd3;
  localparam logic [2:0] ST_WAIT   = 3'd4;
  localparam logic [2:0] ST_LOCKUP = 3'd5;
  localparam logic [2:0] ST_DONE   = 3'd6;

  // --- address plan ---------------------------------------------------------
  localparam int PH0_BEATS = 80;
  localparam int PH1_BEATS = 64;
  localparam int PH2_BEATS = 48;
  localparam int NBEATS    = PH0_BEATS + PH1_BEATS + PH2_BEATS;   // 192

  localparam logic [31:0] PH0_BASE = 32'h0000_0100;
  localparam logic [31:0] PH1_BASE = 32'h0000_0000;
  localparam logic [31:0] PH2_BASE = 32'h0000_0200;

  localparam int LOCKUP_CYCLES = 2;

  // =========================================================================
  // THE PROBE SET -- these eight names are the frozen contract with Stream A.
  // =========================================================================
  logic [31:0] haddr;
  logic [1:0]  htrans;
  logic        hwrite;
  logic [31:0] hrdata;      // NO reset value: X until the first read response
  logic        hready;
  logic [2:0]  state;
  logic        lockup;
  // `clk` is a port, above.

  // --- internal bookkeeping (not probed) -----------------------------------
  logic [7:0]  beat;        // 0 .. NBEATS-1
  logic [1:0]  wait_rem;    // wait cycles still owed on the current beat
  logic [1:0]  lk_cnt;

  // =========================================================================
  // Pure functions describing the plan for beat `b`. Kept as functions so the
  // plan is stated once and cannot drift between the address, the burst
  // position and the wait-state pattern.
  // =========================================================================
  function automatic logic [7:0] beat_ph_index(input logic [7:0] b);
    if (b < PH0_BEATS)                   beat_ph_index = b;
    else if (b < PH0_BEATS + PH1_BEATS)  beat_ph_index = b - 8'(PH0_BEATS);
    else                                 beat_ph_index = b - 8'(PH0_BEATS + PH1_BEATS);
  endfunction

  function automatic logic [31:0] beat_ph_base(input logic [7:0] b);
    if (b < PH0_BEATS)                   beat_ph_base = PH0_BASE;
    else if (b < PH0_BEATS + PH1_BEATS)  beat_ph_base = PH1_BASE;
    else                                 beat_ph_base = PH2_BASE;
  endfunction

  // word-aligned walk inside the phase
  function automatic logic [31:0] beat_addr(input logic [7:0] b);
    beat_addr = beat_ph_base(b) + ({24'd0, beat_ph_index(b)} << 2);
  endfunction

  // NOTE: VCS rejects a bit-select applied directly to a function call
  // ("Select on function call"), so each of these takes the index into a local
  // first. Do not collapse them back into one expression.

  // bursts of 4: the first beat of each burst is NONSEQ, the rest are SEQ
  function automatic logic beat_is_nonseq(input logic [7:0] b);
    logic [7:0] i;
    i = beat_ph_index(b);
    beat_is_nonseq = (i[1:0] == 2'b00);
  endfunction

  function automatic logic beat_is_last_of_burst(input logic [7:0] b);
    logic [7:0] i;
    i = beat_ph_index(b);
    beat_is_last_of_burst = (i[1:0] == 2'b11);
  endfunction

  // direction alternates per burst: burst 0 writes, burst 1 reads, ...
  // => the first READ is burst 1 (index 4), which is what ends the X region on
  //    hrdata a handful of clocks into the run.
  function automatic logic beat_hwrite(input logic [7:0] b);
    logic [7:0] i;
    i = beat_ph_index(b);
    beat_hwrite = ~i[2];
  endfunction

  // MID-RUN X INJECTION (see the header, region 2). These two phase-1 read
  // beats get no slave response, so hrdata is X for a couple of samples inside
  // the cropped compare window.
  //
  // Both are READ beats -- ph_index 20 and 21 have bit[2] set, so
  // beat_hwrite() is 0 -- which is what makes "no response" a coherent story
  // rather than an arbitrary poke. Neither is the trigger beat (ph_index 16),
  // and hrdata is not in the trigger expression, so the trigger is untouched.
  function automatic logic beat_no_response(input logic [7:0] b);
    logic [7:0] i;
    beat_no_response = 1'b0;
    if ((b >= 8'(PH0_BEATS)) && (b < 8'(PH0_BEATS + PH1_BEATS))) begin
      i = beat_ph_index(b);
      if ((i == 8'd20) || (i == 8'd21)) beat_no_response = 1'b1;
    end
  endfunction

  // Deterministic wait-state pattern, chosen with POWER-OF-TWO strides so it is
  // trivially checkable that index 16 -- the trigger beat -- gets zero waits:
  //   16 & 3'h7 == 0 (not 3)   and   16 & 4'hF == 0 (not 10).
  function automatic logic [1:0] beat_waits(input logic [7:0] b);
    logic [7:0] i;
    i = beat_ph_index(b);
    if      (i[3:0] == 4'd10) beat_waits = 2'd2;
    else if (i[2:0] == 3'd3)  beat_waits = 2'd1;
    else                      beat_waits = 2'd0;
  endfunction

  // =========================================================================
  // Master FSM
  // =========================================================================
  logic beat_completes;
  assign beat_completes = (wait_rem == 2'd0);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state    <= ST_RESET;
      haddr    <= 32'h0000_0000;
      htrans   <= TRANS_IDLE;
      hwrite   <= 1'b0;
      hready   <= 1'b0;          // slave not ready out of reset
      lockup   <= 1'b0;
      beat     <= 8'd0;
      wait_rem <= 2'd0;
      lk_cnt   <= 2'd0;
      done     <= 1'b0;
      // hrdata is DELIBERATELY not reset -- see the header.
    end
    else begin
      unique case (state)

        // ---- leave reset by issuing beat 0 -------------------------------
        ST_RESET: begin
          state    <= beat_is_nonseq(8'd0) ? ST_NONSEQ : ST_SEQ;
          haddr    <= beat_addr(8'd0);
          htrans   <= TRANS_NONSEQ;
          hwrite   <= beat_hwrite(8'd0);
          wait_rem <= beat_waits(8'd0);
          hready   <= (beat_waits(8'd0) == 2'd0);
          beat     <= 8'd0;
        end

        // ---- an address phase is on the bus ------------------------------
        ST_NONSEQ, ST_SEQ, ST_WAIT: begin
          if (!beat_completes) begin
            // Wait state: HOLD haddr/htrans (real AHB behaviour) and pull
            // hready low. hready rises on the final wait cycle so the transfer
            // completes on a ready cycle.
            state    <= ST_WAIT;
            wait_rem <= wait_rem - 2'd1;
            hready   <= (wait_rem == 2'd1);
          end
          else if (beat == 8'(NBEATS - 1)) begin
            // last beat of the run
            state  <= ST_LOCKUP;
            htrans <= TRANS_IDLE;
            lockup <= 1'b1;
            lk_cnt <= 2'(LOCKUP_CYCLES - 1);
          end
          else if (beat_is_last_of_burst(beat)) begin
            // one IDLE cycle between bursts
            state  <= ST_IDLE;
            htrans <= TRANS_IDLE;
            beat   <= beat + 8'd1;
          end
          else begin
            state    <= beat_is_nonseq(beat + 8'd1) ? ST_NONSEQ : ST_SEQ;
            haddr    <= beat_addr(beat + 8'd1);
            htrans   <= beat_is_nonseq(beat + 8'd1) ? TRANS_NONSEQ : TRANS_SEQ;
            hwrite   <= beat_hwrite(beat + 8'd1);
            wait_rem <= beat_waits(beat + 8'd1);
            hready   <= (beat_waits(beat + 8'd1) == 2'd0);
            beat     <= beat + 8'd1;
          end
        end

        // ---- dead cycle between bursts -----------------------------------
        ST_IDLE: begin
          state    <= beat_is_nonseq(beat) ? ST_NONSEQ : ST_SEQ;
          haddr    <= beat_addr(beat);
          htrans   <= beat_is_nonseq(beat) ? TRANS_NONSEQ : TRANS_SEQ;
          hwrite   <= beat_hwrite(beat);
          wait_rem <= beat_waits(beat);
          hready   <= (beat_waits(beat) == 2'd0);
        end

        // ---- brief lockup, then done --------------------------------------
        ST_LOCKUP: begin
          if (lk_cnt != 2'd0) begin
            lk_cnt <= lk_cnt - 2'd1;
          end
          else begin
            state  <= ST_DONE;
            lockup <= 1'b0;
            done   <= 1'b1;
          end
        end

        ST_DONE: begin
          state  <= ST_DONE;
          htrans <= TRANS_IDLE;
          hready <= 1'b1;
          done   <= 1'b1;
        end

        default: state <= ST_DONE;
      endcase
    end
  end

  // =========================================================================
  // Trivial slave read-data model.
  //
  // Separate always_ff with NO reset, so hrdata holds X until the first read
  // data phase. Data is a function of the address so a one-bit perturbation
  // anywhere upstream shows up here (this is the signal the negative control
  // has the best chance of catching).
  // =========================================================================
  always_ff @(posedge clk) begin
    if (rst_n && hready && !hwrite &&
        (state == ST_NONSEQ || state == ST_SEQ || state == ST_WAIT)) begin
      if (beat_no_response(beat))
        hrdata <= 32'hxxxx_xxxx;                   // mid-run X, see the header
      else
        hrdata <= {~haddr[15:0], haddr[15:0]} ^ 32'hA5A5_0000;
    end
  end

endmodule
