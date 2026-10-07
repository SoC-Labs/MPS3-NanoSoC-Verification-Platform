// =============================================================================
// tests/identify_iice/tb_selftest.sv
//
// Testbench top for the IICE trace-harness CI gate.
//
// OWNED BY STREAM B (INTERFACES.md §5).
//
// THE TWO NAMES BELOW ARE A FROZEN CONTRACT WITH STREAM A:
//     module `tb`         <-- TB top
//     instance `u_dut`    <-- the DUT
// Stream A's signals_selftest.yaml hard-codes `tb.u_dut.<signal>`, and
// gen_shadow.py emits those as absolute hierarchical references inside a
// PORTLESS module. Renaming either one turns the generated shadow into an
// elaboration error. `tests/test_sim_smoke.py` parses this file to defend both
// names without needing a simulator licence.
//
// -----------------------------------------------------------------------------
// WHY THE SHADOW IS `ifdef`-GUARDED AND NOT PLUSARG-GUARDED
// -----------------------------------------------------------------------------
// INTERFACES.md §3.2 says the shadow is "enabled by plusarg +iice_shadow".
// Verilog has no conditional instantiation on a plusarg -- a plusarg is a
// runtime query, and module instantiation is elaboration-time. There is no
// `if ($test$plusargs(...)) module_inst u();`.
//
// So the split is, and BOTH halves are required:
//   * COMPILE TIME  `+define+IICE_SHADOW` instantiates the shadow, and the
//                   generated file must be on the VCS command line.
//   * RUN TIME      `+iice_shadow` actually arms the dump, and
//                   `+iice_fsdb=<path>` selects the output file. Both are
//                   handled INSIDE the generated module, not here: it wraps
//                   its $fsdbDumpfile/$fsdbDumpvars in
//                   `if ($test$plusargs("iice_shadow"))` and otherwise prints
//                   "IICE_SELFTEST IDLE: no FSDB written". So dropping the
//                   plusarg gives a green run with no trace -- which is why
//                   Makefile.sim asserts the FSDB file afterwards rather than
//                   trusting the verdict.
//
// Consequence, and the reason it is arranged this way: the DEFAULT build has no
// reference to `iice_shadow_IICE_SELFTEST` at all, so it compiles and passes
// with Stream A's generated file ABSENT. That keeps this stream unblocked, and
// keeps the plain `sim-selftest` gate free of any FSDB cost (plan §6 risk V5:
// FSDB dumping must never slow or perturb a default gate).
//
// Asking for `+iice_shadow` on a binary built WITHOUT `+define+IICE_SHADOW` is
// a hard FAIL here: it would otherwise run green and quietly produce no FSDB.
// =============================================================================
`timescale 1ns/1ps

module tb;

  // --- clock / reset --------------------------------------------------------
  localparam int CLK_HALF_NS = 5;      // 10 ns period, 100 MHz
  localparam int RST_CYCLES  = 4;
  localparam int MAX_CYCLES  = 2000;   // hard bound -- this TB never hangs

  // Mirrors `iice.depth` in signals_selftest.yaml (Stream A's manifest). Used
  // ONLY to reconstruct the window the offline cropper will keep, so this TB can
  // assert that the deliberate mid-run X actually lands inside the compared
  // data. It is a self-check, not a source of truth -- if Stream A changes the
  // depth, this assertion fires and says so, which is the point. The first
  // version of this bench injected X only during reset, entirely outside the
  // window, and the live compare report duly read "X-masked samples 0".
  localparam int IICE_DEPTH_HINT = 64;

  logic clk   = 1'b0;
  logic rst_n = 1'b0;
  logic done;

  always #(CLK_HALF_NS) clk = ~clk;

  // =========================================================================
  // FROZEN: instance name `u_dut`
  // =========================================================================
  selftest_dut u_dut (
    .clk   (clk),
    .rst_n (rst_n),
    .done  (done)
  );

  // =========================================================================
  // Stream A's generated shadow IICE. PORTLESS by construction
  // (INTERFACES.md §3.2) -- it reaches into tb.u_dut.* hierarchically and owns
  // its own $fsdbDumpfile/$fsdbDumpvars, including the +iice_fsdb= plusarg.
  // Nothing to wire.
  // =========================================================================
`ifdef IICE_SHADOW
  iice_shadow_IICE_SELFTEST u_iice_shadow();
`endif

  // =========================================================================
  // Observers. Everything is sampled on posedge clk, deliberately: that is the
  // same edge the shadow IICE registers its mirrors on, so what this TB checks
  // is what the trace actually contains -- not a combinational value between
  // edges that the IICE could never see.
  // =========================================================================
  int unsigned cycles;
  int unsigned trig_hits;
  int unsigned trig_cycle;
  time         trig_time;

  bit saw_trans_idle, saw_trans_nonseq, saw_trans_seq;
  bit saw_wait_state;
  bit saw_hrdata_x, saw_hrdata_def;
  bit saw_lockup;
  bit hrdata_x_before_def;   // the reset-phase X region came FIRST, as designed

  // Every sample-edge cycle at which hrdata was X. Recorded rather than counted
  // because the compared window is only known once the trigger has fired, and
  // it extends BACKWARDS from the trigger -- so X samples cannot be classified
  // online. TB-only, so a queue is fine.
  int unsigned x_cycles[$];

  // The selftest trigger condition, per signals_selftest.yaml.
  wire trig_now = (u_dut.htrans == 2'b10) && (u_dut.haddr == 32'h0000_0040);

  initial begin
    cycles     = 0;
    trig_hits  = 0;
    trig_cycle = 0;
    trig_time  = 0;
    saw_trans_idle = 0; saw_trans_nonseq = 0; saw_trans_seq = 0;
    saw_wait_state = 0;
    saw_hrdata_x = 0; saw_hrdata_def = 0;
    saw_lockup = 0; hrdata_x_before_def = 0;
  end

  always @(posedge clk) begin
    cycles <= cycles + 1;

    if (trig_now) begin
      trig_hits <= trig_hits + 1;
      if (trig_hits == 0) begin
        trig_cycle <= cycles;
        trig_time  <= $time;
        $display("IICE_SIM: TRIGGER  htrans=2'b10 haddr=0x%08h  cycle=%0d  time=%0t",
                 u_dut.haddr, cycles, $time);
      end
    end

    if (rst_n) begin
      case (u_dut.htrans)
        2'b00: saw_trans_idle   <= 1'b1;
        2'b10: saw_trans_nonseq <= 1'b1;
        2'b11: saw_trans_seq    <= 1'b1;
        default: ;   // 2'b01 BUSY is never issued by this DUT
      endcase

      if (u_dut.hready == 1'b0) saw_wait_state <= 1'b1;
      if (u_dut.lockup == 1'b1) saw_lockup     <= 1'b1;

      if ($isunknown(u_dut.hrdata)) begin
        saw_hrdata_x <= 1'b1;
        x_cycles.push_back(cycles);
      end
      else begin
        saw_hrdata_def <= 1'b1;
        // "X first, then defined" -- the X-masking path is exercised at the
        // START of the trace, which is where a real reset-phase X lives.
        if (saw_hrdata_x && !saw_hrdata_def) hrdata_x_before_def <= 1'b1;
      end
    end
  end

  // =========================================================================
  // Stimulus + verdict
  // =========================================================================
  bit shadow_compiled_in;
  bit shadow_requested;

  initial begin
`ifdef IICE_SHADOW
    shadow_compiled_in = 1'b1;
`else
    shadow_compiled_in = 1'b0;
`endif
    shadow_requested = $test$plusargs("iice_shadow");

    $display("IICE_SIM: start  shadow_compiled_in=%0d  +iice_shadow=%0d  max_cycles=%0d",
             shadow_compiled_in, shadow_requested, MAX_CYCLES);

    // reset
    rst_n = 1'b0;
    repeat (RST_CYCLES) @(posedge clk);
    @(negedge clk);
    rst_n = 1'b1;
    $display("IICE_SIM: reset released at time=%0t", $time);

    // run to completion, bounded
    fork
      begin : wait_done
        @(posedge done);
        repeat (4) @(posedge clk);
      end
      begin : watchdog
        wait (cycles >= MAX_CYCLES);
      end
    join_any
    disable fork;

    verdict();
    $finish;
  end

  task automatic verdict();
    int unsigned lo, hi;
    int unsigned win_lo, win_hi, x_in_window;
    bit ok_done, ok_trig_once, ok_trig_mid, ok_htrans, ok_wait, ok_x, ok_lockup;
    bit ok_x_in_window;

    // trigger "roughly mid-run": inside the middle half of the run. With
    // trigger_time: middle the cropper wants depth/2 samples of history on
    // BOTH sides, so a trigger in the first or last quarter is a bad selftest.
    lo = cycles / 4;
    hi = (cycles * 3) / 4;

    ok_done      = (done === 1'b1);
    ok_trig_once = (trig_hits == 1);
    ok_trig_mid  = (trig_hits >= 1) && (trig_cycle >= lo) && (trig_cycle <= hi);
    ok_htrans    = saw_trans_idle && saw_trans_nonseq && saw_trans_seq;
    ok_wait      = saw_wait_state;
    ok_x         = saw_hrdata_x && saw_hrdata_def && hrdata_x_before_def;
    ok_lockup    = saw_lockup;

    // The window the offline cropper keeps: `depth` samples centred on the
    // trigger (trigger_time: middle => depth/2 pre, depth/2 post). At least one
    // sim-X sample MUST land in here, or the one-way X-masking rule of
    // INTERFACES.md §4 is never executed against real FSDB data downstream.
    win_lo = (trig_cycle >= IICE_DEPTH_HINT / 2) ? trig_cycle - IICE_DEPTH_HINT / 2 : 0;
    win_hi = win_lo + IICE_DEPTH_HINT - 1;
    x_in_window = 0;
    foreach (x_cycles[k])
      if ((x_cycles[k] >= win_lo) && (x_cycles[k] <= win_hi)) x_in_window++;
    ok_x_in_window = (trig_hits >= 1) && (x_in_window > 0);

    $display("IICE_SIM: cycles=%0d  done=%0d", cycles, done);
    $display("IICE_SIM: trigger      hits=%0d (want 1)  cycle=%0d  time=%0t  window=[%0d..%0d]  %s",
             trig_hits, trig_cycle, trig_time, lo, hi,
             (ok_trig_once && ok_trig_mid) ? "OK" : "BAD");
    $display("IICE_SIM: htrans       idle=%0d nonseq=%0d seq=%0d  %s",
             saw_trans_idle, saw_trans_nonseq, saw_trans_seq, ok_htrans ? "OK" : "BAD");
    $display("IICE_SIM: wait states  hready_low_seen=%0d  %s",
             saw_wait_state, ok_wait ? "OK" : "BAD");
    $display("IICE_SIM: hrdata X     x_seen=%0d def_seen=%0d x_first=%0d  %s",
             saw_hrdata_x, saw_hrdata_def, hrdata_x_before_def, ok_x ? "OK" : "BAD");
    $display("IICE_SIM: X in window  total_x_samples=%0d  in_window=%0d  window=[%0d..%0d] (depth %0d)  %s",
             x_cycles.size(), x_in_window, win_lo, win_hi, IICE_DEPTH_HINT,
             ok_x_in_window ? "OK" : "BAD");
    if (x_cycles.size() > 0)
      $display("IICE_SIM: X cycles     first=%0d last=%0d",
               x_cycles[0], x_cycles[x_cycles.size() - 1]);
    if (!ok_x_in_window)
      $display("IICE_SIM: NOTE no sim-X lands in the cropped window, so the compare pipeline's one-way X-masking rule will never execute (it will report 0 masked samples).");
    $display("IICE_SIM: lockup       pulse_seen=%0d  %s",
             saw_lockup, ok_lockup ? "OK" : "BAD");

    if (shadow_requested && !shadow_compiled_in) begin
      $display("IICE_SIM: ERROR +iice_shadow was passed but the binary was built");
      $display("IICE_SIM:       WITHOUT +define+IICE_SHADOW, so no shadow IICE was");
      $display("IICE_SIM:       instantiated and NO FSDB can have been written.");
      $display("IICE_SIM: VERDICT=FAIL");
      return;
    end

    if (!ok_done) begin
      $display("IICE_SIM: TIMEOUT after %0d cycles (DUT never asserted done)", cycles);
      $display("IICE_SIM: VERDICT=FAIL");
      return;
    end

    $display("IICE_SIM: VERDICT=%s",
             (ok_done && ok_trig_once && ok_trig_mid && ok_htrans &&
              ok_wait && ok_x && ok_x_in_window && ok_lockup) ? "PASS" : "FAIL");
  endtask

endmodule
