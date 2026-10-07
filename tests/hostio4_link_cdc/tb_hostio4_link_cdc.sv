// tb_hostio4_link_cdc — L1: hostio4_controller <-> hostio4_target across TWO
// INDEPENDENT CLOCKS, with per-wire skew injection.
//
// WHY THIS EXISTS
//   The vendor bench (tests/hostio4_golden, L0) drives both ends from ONE
//   oscillator: `assign C_clk = clk; assign T_clk = !clk;`. Same frequency,
//   zero drift, fixed 180 deg phase. It therefore proves nothing about the
//   hostio4_*_sync synchronisers or about what happens when the two ends are
//   genuinely asynchronous -- which is the case in the harness, where the
//   controller is inside the DUT on dut_clk and the target is in the shell.
//
// TOPOLOGY (mirrors the harness, not the vendor tb)
//   nanoSoC drives ioreq1/ioreq2 and receives ioack (see nanosoc P1 mapping),
//   so the DUT holds the CONTROLLER. C_clk == dut_clk, T_clk == shell clock.
//
// WHAT IS SKEWED, AND WHY IT IS THE INTERESTING KNOB
//   hostio4_target synchronises ONLY ioreq1_a/ioreq2_a (2FF). It samples
//   iodata4_i RAW, and its FSM even branches on it directly:
//       RXC1: nxt_fsm_state = (!ioreq2_s) ? RXC1 : (iodata4_i[0]) ? TXCZ : RXDH;
//   The 2FF latency on ioreq2 is what buys iodata4 time to settle. So the
//   quantity that actually matters at a pad / partition boundary is the SKEW
//   between iodata4 and ioreq2. This bench injects it and measures the budget.
//
// SELF-CHECKING
//   Each of the 4 byte channels carries an independent LFSR stream with a
//   distinct seed. The sink regenerates the sequence from the same seed, so a
//   channel mix-up, a dropped byte, a duplicated byte and a corrupted byte are
//   all caught -- there is no golden file to drift.
//
// RUNTIME KNOBS (one compile, whole sweep -- no recompiles)
//   +cper=<ns>      controller clock period      (default 10.0)
//   +tper=<ns>      target clock period          (default  7.3)
//   +tskew=<ns>     initial phase offset of T_clk (default 0)
//   +ddata=<ns>     iodata4 wire delay, both receivers   (default 0)
//   +ddata_t=<ns>   iodata4 delay into the TARGET only   (default: ddata)
//   +ddata_c=<ns>   iodata4 delay into the CONTROLLER only (default: ddata)
//   +dreq=<ns>      ioreq1/2 wire delay          (default 0)
//   +dack=<ns>      ioack wire delay             (default 0)
//   +nbytes=<n>     bytes per channel            (default 256)
//   +testmode       bypass every 2FF synchroniser (NEGATIVE CONTROL)
//   +neg_ack_stuck  tie ioack high               (NEGATIVE CONTROL)
//   +neg_data_rev   bit-reverse iodata4 into the target (NEGATIVE CONTROL)
//
// EXIT: prints "LINK PASS" / "LINK FAIL <reason>" and $finish. Non-zero-ish
// failure is signalled to make(1) by the absence of "LINK PASS" (VCS always
// exits 0), which the Makefile greps for.

`timescale 1ns/1ps

// axis_src / axis_sink now live in tests/hostio4_common/axis_stim.sv, shared
// with tests/hostio4_hotswap so the two benches cannot drift apart on what
// "byte-exact" means. The Makefile compiles that file first.

// ---------------------------------------------------------------------------
module tb_hostio4_link_cdc;

  // ---- runtime knobs ------------------------------------------------------
  real c_per = 10.0, t_per = 7.3, t_skew = 0.0;
  real d_data = 0.0, d_req = 0.0, d_ack = 0.0;
  // Default 0.0, never negative: the generate-block `always` below can fire
  // at time 0, before the plusarg initial runs, and a negative delay is fatal.
  real d_data_t = 0.0, d_data_c = 0.0;
  int  unsigned nbytes = 256;
  bit  neg_testmode, neg_ack_stuck, neg_data_rev;

  initial begin
    void'($value$plusargs("cper=%f",   c_per));
    void'($value$plusargs("tper=%f",   t_per));
    void'($value$plusargs("tskew=%f",  t_skew));
    void'($value$plusargs("ddata=%f",   d_data));
    if (!$value$plusargs("ddata_t=%f", d_data_t)) d_data_t = d_data;
    if (!$value$plusargs("ddata_c=%f", d_data_c)) d_data_c = d_data;
    void'($value$plusargs("dreq=%f",   d_req));
    void'($value$plusargs("dack=%f",   d_ack));
    void'($value$plusargs("nbytes=%d", nbytes));
    neg_testmode  = $test$plusargs("testmode");
    neg_ack_stuck = $test$plusargs("neg_ack_stuck");
    neg_data_rev  = $test$plusargs("neg_data_rev");
  end

  // ---- two genuinely independent clocks -----------------------------------
  logic C_clk = 1'b0, T_clk = 1'b0;
  logic resetn = 1'b0;

  initial begin
    void'($value$plusargs("cper=%f", c_per));
    forever #(c_per / 2.0) C_clk = ~C_clk;
  end
  initial begin
    void'($value$plusargs("tper=%f",  t_per));
    void'($value$plusargs("tskew=%f", t_skew));
    if (t_skew > 0.0) #(t_skew);
    forever #(t_per / 2.0) T_clk = ~T_clk;
  end

  wire testmode = neg_testmode;

  // ---- the 7-wire link ----------------------------------------------------
  wire [3:0] iodata4;                     // shared tristate net
  wire [3:0] C_iodata4_o, C_iodata4_e, C_iodata4_t;
  wire [3:0] T_iodata4_o, T_iodata4_e, T_iodata4_t;
  wire       ioreq1, ioreq2, ioack;

  // Tristate emulation, exactly as the vendor tb does it (bufif0: iodata4_t
  // is an active-high DISABLE). No pullups -- the net floats to z during the
  // FSMs' turnaround (*Z) states, same as the vendor bench.
  bufif0 (iodata4[3], C_iodata4_o[3], C_iodata4_t[3]);
  bufif0 (iodata4[2], C_iodata4_o[2], C_iodata4_t[2]);
  bufif0 (iodata4[1], C_iodata4_o[1], C_iodata4_t[1]);
  bufif0 (iodata4[0], C_iodata4_o[0], C_iodata4_t[0]);
  bufif0 (iodata4[3], T_iodata4_o[3], T_iodata4_t[3]);
  bufif0 (iodata4[2], T_iodata4_o[2], T_iodata4_t[2]);
  bufif0 (iodata4[1], T_iodata4_o[1], T_iodata4_t[1]);
  bufif0 (iodata4[0], T_iodata4_o[0], T_iodata4_t[0]);

  // Per-wire TRANSPORT delay (non-blocking intra-assignment queues events, so
  // unlike `assign #d` it does not swallow pulses shorter than the delay --
  // essential once the skew approaches a clock period).
  logic [3:0] c_din_r, t_din_r;
  logic       t_req1_r, t_req2_r, c_ack_r;

  genvar b;
  generate
    for (b = 0; b < 4; b++) begin : g_data_dly
      always @(iodata4[b]) begin
        c_din_r[b] <= #(d_data_c) iodata4[b];
        t_din_r[b] <= #(d_data_t) iodata4[b];
      end
    end
  endgenerate
  always @(ioreq1) t_req1_r <= #(d_req) ioreq1;
  always @(ioreq2) t_req2_r <= #(d_req) ioreq2;
  always @(ioack)  c_ack_r  <= #(d_ack) ioack;

  // Negative-control muxes on the received (delayed) values.
  wire [3:0] T_iodata4_i = neg_data_rev
                         ? {t_din_r[0], t_din_r[1], t_din_r[2], t_din_r[3]}
                         : t_din_r;
  wire [3:0] C_iodata4_a = c_din_r;
  wire       T_ioreq1_a  = t_req1_r;
  wire       T_ioreq2_a  = t_req2_r;
  wire       C_ioack_a   = neg_ack_stuck ? 1'b1 : c_ack_r;

  // ---- AXIS plumbing ------------------------------------------------------
  wire C_rx0_tready, C_rx1_tready, T_rx0_tready, T_rx1_tready;
  wire C_rx0_tvalid, C_rx1_tvalid, T_rx0_tvalid, T_rx1_tvalid;
  wire [7:0] C_rx0_tdata8, C_rx1_tdata8, T_rx0_tdata8, T_rx1_tdata8;

  wire C_tx0_tready, C_tx1_tready, T_tx0_tready, T_tx1_tready;
  wire C_tx0_tvalid, C_tx1_tvalid, T_tx0_tvalid, T_tx1_tvalid;
  wire [7:0] C_tx0_tdata8, C_tx1_tdata8, T_tx0_tdata8, T_tx1_tdata8;

  // Four distinct seeds: if the link crossed channels, or looped a stream
  // back, the sinks would see the wrong sequence.
  localparam int unsigned SEED_C0 = 32'h1234_5678;  // C.rx0 -> T.tx0
  localparam int unsigned SEED_C1 = 32'h0BAD_F00D;  // C.rx1 -> T.tx1
  localparam int unsigned SEED_T0 = 32'hFEED_FACE;  // T.rx0 -> C.tx0
  localparam int unsigned SEED_T1 = 32'h5A5A_C3C3;  // T.rx1 -> C.tx1

  int unsigned sent_c0, sent_c1, sent_t0, sent_t1;
  int unsigned got_t0, got_t1, got_c0, got_c1;
  int unsigned err_t0, err_t1, err_c0, err_c1;

  axis_src #(.SEED(SEED_C0), .GSEED(32'h1111_0001)) u_src_c0
    (.clk(C_clk), .resetn(resetn), .nbytes(nbytes),
     .tvalid(C_rx0_tvalid), .tdata8(C_rx0_tdata8), .tready(C_rx0_tready), .sent(sent_c0));
  axis_src #(.SEED(SEED_C1), .GSEED(32'h1111_0002)) u_src_c1
    (.clk(C_clk), .resetn(resetn), .nbytes(nbytes),
     .tvalid(C_rx1_tvalid), .tdata8(C_rx1_tdata8), .tready(C_rx1_tready), .sent(sent_c1));
  axis_src #(.SEED(SEED_T0), .GSEED(32'h1111_0003)) u_src_t0
    (.clk(T_clk), .resetn(resetn), .nbytes(nbytes),
     .tvalid(T_rx0_tvalid), .tdata8(T_rx0_tdata8), .tready(T_rx0_tready), .sent(sent_t0));
  axis_src #(.SEED(SEED_T1), .GSEED(32'h1111_0004)) u_src_t1
    (.clk(T_clk), .resetn(resetn), .nbytes(nbytes),
     .tvalid(T_rx1_tvalid), .tdata8(T_rx1_tdata8), .tready(T_rx1_tready), .sent(sent_t1));

  axis_sink #(.SEED(SEED_C0), .GSEED(32'h2222_0001), .NAME("T.tx0")) u_snk_t0
    (.clk(T_clk), .resetn(resetn), .drain(1'b0),
     .tvalid(T_tx0_tvalid), .tdata8(T_tx0_tdata8), .tready(T_tx0_tready), .got(got_t0), .errs(err_t0));
  axis_sink #(.SEED(SEED_C1), .GSEED(32'h2222_0002), .NAME("T.tx1")) u_snk_t1
    (.clk(T_clk), .resetn(resetn), .drain(1'b0),
     .tvalid(T_tx1_tvalid), .tdata8(T_tx1_tdata8), .tready(T_tx1_tready), .got(got_t1), .errs(err_t1));
  axis_sink #(.SEED(SEED_T0), .GSEED(32'h2222_0003), .NAME("C.tx0")) u_snk_c0
    (.clk(C_clk), .resetn(resetn), .drain(1'b0),
     .tvalid(C_tx0_tvalid), .tdata8(C_tx0_tdata8), .tready(C_tx0_tready), .got(got_c0), .errs(err_c0));
  axis_sink #(.SEED(SEED_T1), .GSEED(32'h2222_0004), .NAME("C.tx1")) u_snk_c1
    (.clk(C_clk), .resetn(resetn), .drain(1'b0),
     .tvalid(C_tx1_tvalid), .tdata8(C_tx1_tdata8), .tready(C_tx1_tready), .got(got_c1), .errs(err_c1));

  // ---- DUTs ---------------------------------------------------------------
  hostio4_controller u_controller (
    .clk(C_clk), .resetn(resetn), .testmode(testmode),
    .axis_rx0_tready(C_rx0_tready), .axis_rx0_tvalid(C_rx0_tvalid), .axis_rx0_tdata8(C_rx0_tdata8),
    .axis_rx1_tready(C_rx1_tready), .axis_rx1_tvalid(C_rx1_tvalid), .axis_rx1_tdata8(C_rx1_tdata8),
    .axis_tx0_tready(C_tx0_tready), .axis_tx0_tvalid(C_tx0_tvalid), .axis_tx0_tdata8(C_tx0_tdata8),
    .axis_tx1_tready(C_tx1_tready), .axis_tx1_tvalid(C_tx1_tvalid), .axis_tx1_tdata8(C_tx1_tdata8),
    .iodata4_a(C_iodata4_a), .iodata4_o(C_iodata4_o), .iodata4_e(C_iodata4_e), .iodata4_t(C_iodata4_t),
    .ioreq1_o(ioreq1), .ioreq2_o(ioreq2), .ioack_a(C_ioack_a)
  );

  hostio4_target u_target (
    .clk(T_clk), .resetn(resetn), .testmode(testmode),
    .axis_rx0_tready(T_rx0_tready), .axis_rx0_tvalid(T_rx0_tvalid), .axis_rx0_tdata8(T_rx0_tdata8),
    .axis_rx1_tready(T_rx1_tready), .axis_rx1_tvalid(T_rx1_tvalid), .axis_rx1_tdata8(T_rx1_tdata8),
    .axis_tx0_tready(T_tx0_tready), .axis_tx0_tvalid(T_tx0_tvalid), .axis_tx0_tdata8(T_tx0_tdata8),
    .axis_tx1_tready(T_tx1_tready), .axis_tx1_tvalid(T_tx1_tvalid), .axis_tx1_tdata8(T_tx1_tdata8),
    .iodata4_i(T_iodata4_i), .iodata4_o(T_iodata4_o), .iodata4_e(T_iodata4_e), .iodata4_t(T_iodata4_t),
    .ioreq1_a(T_ioreq1_a), .ioreq2_a(T_ioreq2_a), .ioack_o(ioack)
  );

  // ---- run / watchdog / verdict -------------------------------------------
  int unsigned last_total, stall_cnt;
  wire [31:0]  total_got = got_t0 + got_t1 + got_c0 + got_c1;
  wire done = (got_t0 == nbytes) && (got_t1 == nbytes)
           && (got_c0 == nbytes) && (got_c1 == nbytes);
  wire [31:0] errs_all = err_t0 + err_t1 + err_c0 + err_c1;

  // Stall watchdog on C_clk: if no byte lands anywhere for STALL_LIM cycles
  // the link has locked up. L0 measured ~129 C_clk cycles/byte/channel, so
  // 100_000 is ~800x the worst legitimate inter-byte gap.
  localparam int unsigned STALL_LIM = 100_000;

  always_ff @(posedge C_clk or negedge resetn) begin
    if (!resetn) begin
      stall_cnt <= 0; last_total <= 0;
    end else begin
      if (total_got != last_total) begin
        last_total <= total_got; stall_cnt <= 0;
      end else begin
        stall_cnt <= stall_cnt + 1;
      end
    end
  end

  task automatic verdict(input string reason);
    $display("");
    $display("    clocks   C=%0.3f ns  T=%0.3f ns  (ratio %0.4f)  T phase +%0.3f ns",
             c_per, t_per, c_per / t_per, t_skew);
    $display("    skew     iodata4->T=%0.3f ns  iodata4->C=%0.3f ns  ioreq=%0.3f ns  ioack=%0.3f ns",
             d_data_t, d_data_c, d_req, d_ack);
    $display("    budget   target 2FF = 2*T = %0.3f ns   controller 2FF = 2*C = %0.3f ns",
             2.0 * t_per, 2.0 * c_per);
    if (neg_testmode)  $display("    NEG      testmode=1 (all 2FF synchronisers bypassed)");
    if (neg_ack_stuck) $display("    NEG      ioack tied high");
    if (neg_data_rev)  $display("    NEG      iodata4 bit-reversed into the target");
    $display("    sent     C.rx0=%0d C.rx1=%0d  T.rx0=%0d T.rx1=%0d", sent_c0, sent_c1, sent_t0, sent_t1);
    $display("    got      T.tx0=%0d T.tx1=%0d  C.tx0=%0d C.tx1=%0d", got_t0, got_t1, got_c0, got_c1);
    $display("    errors   T.tx0=%0d T.tx1=%0d  C.tx0=%0d C.tx1=%0d", err_t0, err_t1, err_c0, err_c1);
    if (reason == "")
      $display("    LINK PASS  (%0d bytes/channel, %0t)", nbytes, $time);
    else
      $display("    LINK FAIL  %s", reason);
    $display("");
    $finish;
  endtask

  initial begin
    resetn = 1'b0;
    repeat (20) @(posedge C_clk);
    repeat (20) @(posedge T_clk);
    // Release reset OFF any clock edge. Deasserting it AT a posedge races the
    // active-region read of `resetn` inside every always_ff in both domains.
    #0.1 resetn = 1'b1;

    fork
      begin : b_done
        wait (done);
        repeat (20) @(posedge C_clk);          // settle; catch trailing bytes
        if (errs_all != 0) verdict($sformatf("%0d byte mismatches", errs_all));
        else if (got_t0 != nbytes || got_t1 != nbytes || got_c0 != nbytes || got_c1 != nbytes)
          verdict("byte-count mismatch after settle");
        else verdict("");
      end
      begin : b_stall
        wait (stall_cnt >= STALL_LIM);
        verdict($sformatf("link stalled -- no byte moved for %0d C_clk cycles", STALL_LIM));
      end
      begin : b_err
        wait (errs_all > 0);
        repeat (50) @(posedge C_clk);
        verdict($sformatf("%0d byte mismatches", errs_all));
      end
    join_any
  end

endmodule
