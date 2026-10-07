// tb_hostio4_swap_live — L2b: a live hostio4 link survives (or does not survive)
// a simulated DFX partial reconfiguration.
//
// L2a (tb_hostio4_park) forced the target into each FSM state and showed that no
// constant decoupler value parks it. That is an analysis. THIS bench is the
// demonstration: run real traffic on all four channels, perform a swap in the
// middle of it, and see whether the link comes back.
//
// THE SWAP, modelled as the hardware does it
//   1. Traffic flows. Both DUTs live, on independent clocks.
//   2. DECOUPLE. The DFX decoupler replaces every RP-sourced boundary signal
//      with a constant: ioreq1=0, ioreq2=0, iodata4 DRIVEN to 0. (Both of those
//      choices are L2a requirements: ioreq1 high drags an idle target out of
//      TXST, and a floating iodata4 X-merges the state register.)
//   3. The controller is held in reset -- it is being overwritten by ICAP.
//   4. SWAP POLICY (the variable under test):
//        +pol_none     nothing else. The target is left where it was.
//        +pol_treset   the shell also resets the target.
//        +pol_escape   the shell toggles ioreq2 three times (L2a's minimum).
//   5. RECOUPLE, release the controller's reset. The new DUT starts talking.
//   6. Fresh traffic must flow, byte-exact, on all four channels.
//
// EXPECTED (this is the point of the bench)
//   pol_none   -> FAIL. The target is wedged; ioack_o = fsm_state[0] is stuck,
//                 and the controller waits on an ack transition forever. So the
//                 next DUT hangs on its FIRST transaction.
//   pol_treset -> PASS.
//   pol_escape -> PASS.
//
// SCORING ACROSS THE SWAP
//   In-flight bytes are lost when the controller is reconfigured; that is
//   correct behaviour, not a defect. So the stimulus models carry their own
//   reset (`stim_rstn`), separate from either DUT's, and the sinks are put in
//   `drain` while the swap happens. Phase 2 is scored as a fresh sequence.
//   Phase 1 is scored normally, so a bench that never worked cannot pass.
//
// KNOBS
//   +cper/+tper=<ns>  clocks (default 10.0 / 7.3 -- non-harmonic, see L1)
//   +n1=<n> +n2=<n>   bytes per channel before / after the swap
//   +swap_at=<n>      total bytes delivered before the swap fires (default 40)
//   +swap_state=<S>   fire the swap only when the target FSM is in S
//                     (TXST RXC1 RXDH RXDL RXDZ TXSZ TXCZ TXDH TXDL; default:
//                      any state that is NOT TXST -- i.e. mid-transaction)
//   +dec_ioack=<0|1>  the value the decoupler drives onto the RP's ioack input
//                     (default 0). This turns out to matter enormously.
//   +pol_none | +pol_treset | +pol_escape
//
// THE SWAP MUST LAND MID-TRANSACTION. Swapping once phase-1 has drained finds
// the target idle in TXST with nothing to wedge, and every policy then "passes"
// -- because the fresh controller's own ioreq toggles walk an idle target
// forward anyway. The first version of this bench did exactly that and reported
// a cheerful, worthless green.

`timescale 1ns/1ps

module tb_hostio4_swap_live;

  real c_per = 10.0, t_per = 7.3;
  int unsigned n1 = 64, n2 = 48, swap_at = 40;
  bit pol_treset, pol_escape;
  int unsigned dec_ioack = 0;
  string swap_state_s = "";

  initial begin
    void'($value$plusargs("n1=%d", n1));
    void'($value$plusargs("n2=%d", n2));
    void'($value$plusargs("swap_at=%d", swap_at));
    void'($value$plusargs("swap_state=%s", swap_state_s));
    void'($value$plusargs("dec_ioack=%d", dec_ioack));
    pol_treset = $test$plusargs("pol_treset");
    pol_escape = $test$plusargs("pol_escape");
  end

  // Target FSM encoding, from hostio4_target_fsm.v (see tb_hostio4_park.sv).
  localparam logic [7:0] TXST = 8'b0_001_000_0;
  localparam logic [7:0] RXC1 = 8'b0_000_001_1;
  localparam logic [7:0] RXDH = 8'b0_000_010_0;
  localparam logic [7:0] RXDL = 8'b0_000_100_1;
  localparam logic [7:0] RXDZ = 8'b0_000_000_0;
  localparam logic [7:0] TXSZ = 8'b0_001_000_1;
  localparam logic [7:0] TXCZ = 8'b1_000_000_0;
  localparam logic [7:0] TXDH = 8'b1_010_000_1;
  localparam logic [7:0] TXDL = 8'b1_100_000_0;

  `define TFSM u_target.u_hostio4_target_fsm.fsm_state

  function automatic logic [7:0] state_of(input string n);
    case (n)
      "TXST": return TXST; "RXC1": return RXC1; "RXDH": return RXDH;
      "RXDL": return RXDL; "RXDZ": return RXDZ; "TXSZ": return TXSZ;
      "TXCZ": return TXCZ; "TXDH": return TXDH; "TXDL": return TXDL;
      default: return 8'hxx;
    endcase
  endfunction

  function automatic string name_of(input logic [7:0] v);
    case (v)
      TXST: return "TXST"; RXC1: return "RXC1"; RXDH: return "RXDH";
      RXDL: return "RXDL"; RXDZ: return "RXDZ"; TXSZ: return "TXSZ";
      TXCZ: return "TXCZ"; TXDH: return "TXDH"; TXDL: return "TXDL";
      default: return $sformatf("0b%b", v);
    endcase
  endfunction

  logic C_clk = 1'b0, T_clk = 1'b0;
  initial begin
    void'($value$plusargs("cper=%f", c_per));
    forever #(c_per / 2.0) C_clk = ~C_clk;
  end
  initial begin
    void'($value$plusargs("tper=%f", t_per));
    forever #(t_per / 2.0) T_clk = ~T_clk;
  end

  // Three independent resets. c_resetn is the RP's; t_resetn is the shell's;
  // stim_rstn belongs to the testbench models only.
  logic c_resetn = 1'b0, t_resetn = 1'b0, stim_rstn = 1'b0;
  logic decouple = 1'b0, drain = 1'b0;
  logic esc_ioreq2 = 1'b0;          // driven by the escape policy
  wire  testmode = 1'b0;

  // ---- link ---------------------------------------------------------------
  wire [3:0] iodata4;
  wire [3:0] C_iodata4_o, C_iodata4_e, C_iodata4_t;
  wire [3:0] T_iodata4_o, T_iodata4_e, T_iodata4_t;
  wire       c_ioreq1, c_ioreq2, t_ioack;

  bufif0 (iodata4[3], C_iodata4_o[3], C_iodata4_t[3]);
  bufif0 (iodata4[2], C_iodata4_o[2], C_iodata4_t[2]);
  bufif0 (iodata4[1], C_iodata4_o[1], C_iodata4_t[1]);
  bufif0 (iodata4[0], C_iodata4_o[0], C_iodata4_t[0]);
  bufif0 (iodata4[3], T_iodata4_o[3], T_iodata4_t[3]);
  bufif0 (iodata4[2], T_iodata4_o[2], T_iodata4_t[2]);
  bufif0 (iodata4[1], T_iodata4_o[1], T_iodata4_t[1]);
  bufif0 (iodata4[0], T_iodata4_o[0], T_iodata4_t[0]);

  // The decoupler. While `decouple` is high the target sees constants instead
  // of the (vanishing) RP, and the controller sees a benign ack.
  wire [3:0] T_iodata4_i = decouple ? 4'h0     : iodata4;
  wire       T_ioreq1_a  = decouple ? 1'b0     : c_ioreq1;
  wire       T_ioreq2_a  = decouple ? esc_ioreq2 : c_ioreq2;
  wire [3:0] C_iodata4_a = decouple ? 4'h0     : iodata4;
  wire       C_ioack_a   = decouple ? dec_ioack[0] : t_ioack;

  // ---- AXIS ---------------------------------------------------------------
  wire C_rx0_tready, C_rx1_tready, T_rx0_tready, T_rx1_tready;
  wire C_rx0_tvalid, C_rx1_tvalid, T_rx0_tvalid, T_rx1_tvalid;
  wire [7:0] C_rx0_tdata8, C_rx1_tdata8, T_rx0_tdata8, T_rx1_tdata8;
  wire C_tx0_tready, C_tx1_tready, T_tx0_tready, T_tx1_tready;
  wire C_tx0_tvalid, C_tx1_tvalid, T_tx0_tvalid, T_tx1_tvalid;
  wire [7:0] C_tx0_tdata8, C_tx1_tdata8, T_tx0_tdata8, T_tx1_tdata8;

  localparam int unsigned SEED_C0 = 32'h1234_5678;
  localparam int unsigned SEED_C1 = 32'h0BAD_F00D;
  localparam int unsigned SEED_T0 = 32'hFEED_FACE;
  localparam int unsigned SEED_T1 = 32'h5A5A_C3C3;

  int unsigned sent_c0, sent_c1, sent_t0, sent_t1;
  int unsigned got_t0, got_t1, got_c0, got_c1;
  int unsigned err_t0, err_t1, err_c0, err_c1;
  int unsigned nbytes;                       // phase byte target, set per phase

  axis_src #(.SEED(SEED_C0), .GSEED(32'h1111_0001)) u_src_c0
    (.clk(C_clk), .resetn(stim_rstn), .nbytes(nbytes),
     .tvalid(C_rx0_tvalid), .tdata8(C_rx0_tdata8), .tready(C_rx0_tready), .sent(sent_c0));
  axis_src #(.SEED(SEED_C1), .GSEED(32'h1111_0002)) u_src_c1
    (.clk(C_clk), .resetn(stim_rstn), .nbytes(nbytes),
     .tvalid(C_rx1_tvalid), .tdata8(C_rx1_tdata8), .tready(C_rx1_tready), .sent(sent_c1));
  axis_src #(.SEED(SEED_T0), .GSEED(32'h1111_0003)) u_src_t0
    (.clk(T_clk), .resetn(stim_rstn), .nbytes(nbytes),
     .tvalid(T_rx0_tvalid), .tdata8(T_rx0_tdata8), .tready(T_rx0_tready), .sent(sent_t0));
  axis_src #(.SEED(SEED_T1), .GSEED(32'h1111_0004)) u_src_t1
    (.clk(T_clk), .resetn(stim_rstn), .nbytes(nbytes),
     .tvalid(T_rx1_tvalid), .tdata8(T_rx1_tdata8), .tready(T_rx1_tready), .sent(sent_t1));

  axis_sink #(.SEED(SEED_C0), .GSEED(32'h2222_0001), .NAME("T.tx0")) u_snk_t0
    (.clk(T_clk), .resetn(stim_rstn), .drain(drain),
     .tvalid(T_tx0_tvalid), .tdata8(T_tx0_tdata8), .tready(T_tx0_tready), .got(got_t0), .errs(err_t0));
  axis_sink #(.SEED(SEED_C1), .GSEED(32'h2222_0002), .NAME("T.tx1")) u_snk_t1
    (.clk(T_clk), .resetn(stim_rstn), .drain(drain),
     .tvalid(T_tx1_tvalid), .tdata8(T_tx1_tdata8), .tready(T_tx1_tready), .got(got_t1), .errs(err_t1));
  axis_sink #(.SEED(SEED_T0), .GSEED(32'h2222_0003), .NAME("C.tx0")) u_snk_c0
    (.clk(C_clk), .resetn(stim_rstn), .drain(drain),
     .tvalid(C_tx0_tvalid), .tdata8(C_tx0_tdata8), .tready(C_tx0_tready), .got(got_c0), .errs(err_c0));
  axis_sink #(.SEED(SEED_T1), .GSEED(32'h2222_0004), .NAME("C.tx1")) u_snk_c1
    (.clk(C_clk), .resetn(stim_rstn), .drain(drain),
     .tvalid(C_tx1_tvalid), .tdata8(C_tx1_tdata8), .tready(C_tx1_tready), .got(got_c1), .errs(err_c1));

  hostio4_controller u_controller (
    .clk(C_clk), .resetn(c_resetn), .testmode(testmode),
    .axis_rx0_tready(C_rx0_tready), .axis_rx0_tvalid(C_rx0_tvalid), .axis_rx0_tdata8(C_rx0_tdata8),
    .axis_rx1_tready(C_rx1_tready), .axis_rx1_tvalid(C_rx1_tvalid), .axis_rx1_tdata8(C_rx1_tdata8),
    .axis_tx0_tready(C_tx0_tready), .axis_tx0_tvalid(C_tx0_tvalid), .axis_tx0_tdata8(C_tx0_tdata8),
    .axis_tx1_tready(C_tx1_tready), .axis_tx1_tvalid(C_tx1_tvalid), .axis_tx1_tdata8(C_tx1_tdata8),
    .iodata4_a(C_iodata4_a), .iodata4_o(C_iodata4_o), .iodata4_e(C_iodata4_e), .iodata4_t(C_iodata4_t),
    .ioreq1_o(c_ioreq1), .ioreq2_o(c_ioreq2), .ioack_a(C_ioack_a)
  );

  hostio4_target u_target (
    .clk(T_clk), .resetn(t_resetn), .testmode(testmode),
    .axis_rx0_tready(T_rx0_tready), .axis_rx0_tvalid(T_rx0_tvalid), .axis_rx0_tdata8(T_rx0_tdata8),
    .axis_rx1_tready(T_rx1_tready), .axis_rx1_tvalid(T_rx1_tvalid), .axis_rx1_tdata8(T_rx1_tdata8),
    .axis_tx0_tready(T_tx0_tready), .axis_tx0_tvalid(T_tx0_tvalid), .axis_tx0_tdata8(T_tx0_tdata8),
    .axis_tx1_tready(T_tx1_tready), .axis_tx1_tvalid(T_tx1_tvalid), .axis_tx1_tdata8(T_tx1_tdata8),
    .iodata4_i(T_iodata4_i), .iodata4_o(T_iodata4_o), .iodata4_e(T_iodata4_e), .iodata4_t(T_iodata4_t),
    .ioreq1_a(T_ioreq1_a), .ioreq2_a(T_ioreq2_a), .ioack_o(t_ioack)
  );

  // ---- progress / watchdog -------------------------------------------------
  wire [31:0] total_got = got_t0 + got_t1 + got_c0 + got_c1;
  wire [31:0] errs_all  = err_t0 + err_t1 + err_c0 + err_c1;
  wire        phase_done = (got_t0 == nbytes) && (got_t1 == nbytes)
                        && (got_c0 == nbytes) && (got_c1 == nbytes);

  localparam int unsigned STALL_LIM = 60_000;   // ~470x the worst inter-byte gap
  int unsigned last_total, stall_cnt;
  logic        watch_en;

  always_ff @(posedge C_clk) begin
    if (!watch_en) begin
      stall_cnt <= 0; last_total <= total_got;
    end else if (total_got != last_total) begin
      last_total <= total_got; stall_cnt <= 0;
    end else begin
      stall_cnt <= stall_cnt + 1;
    end
  end

  string fail_reason = "";
  logic [7:0] swap_fsm_state;

  task automatic run_phase(input string label, input int unsigned n);
    nbytes    = n;
    // Hold the models in reset while `drain` is STILL high, so anything left in
    // the DUT is flushed and discarded, then start scoring and stop draining in
    // the same instant. Dropping drain even a few cycles early lets a leftover
    // byte pop out and be scored against the fresh sequence -- it shows up as a
    // mismatch on "byte 0" (or on byte N of the previous phase). Cost me a run.
    stim_rstn = 1'b0;
    repeat (4) @(posedge C_clk);
    #0.1 stim_rstn = 1'b1; drain = 1'b0;   // off-edge, together
    watch_en = 1'b1;
    fork
      begin wait (phase_done); end
      begin wait (stall_cnt >= STALL_LIM);
            fail_reason = $sformatf("%s: link stalled (no byte for %0d C_clk)", label, STALL_LIM); end
      begin wait (errs_all > 0);
            fail_reason = $sformatf("%s: byte mismatch", label); end
    join_any
    // join_any leaves the losing siblings RUNNING. Left alive, phase-1's error
    // watcher fires during phase-2 and mislabels the failure. Kill them.
    disable fork;
    watch_en = 1'b0;

    // Settle: if the link emits a byte the source never sent, catch it here
    // rather than let the next phase's reset sweep it under the rug.
    if (fail_reason == "") begin
      repeat (20) @(posedge C_clk);
      if (errs_all != 0)
        fail_reason = $sformatf("%s: %0d spurious byte(s) after the stream ended", label, errs_all);
    end
    if (fail_reason == "")
      $display("    %-8s %0d bytes x 4 channels, byte-exact", label, n);
  endtask

  initial begin
    #1;
    $display("");
    $display("    policy: %s", pol_treset ? "pol_treset (shell resets the target)" :
                               pol_escape ? "pol_escape (shell toggles ioreq2 x3)" :
                                            "pol_none   (target left alone)");
    $display("    clocks: C=%0.3f ns  T=%0.3f ns   decoupler drives RP ioack=%0d", c_per, t_per, dec_ioack);
    $display("");

    // ---- bring-up -----------------------------------------------------------
    c_resetn = 1'b0; t_resetn = 1'b0; stim_rstn = 1'b0;
    repeat (20) @(posedge C_clk);
    repeat (20) @(posedge T_clk);
    #0.1 c_resetn = 1'b1; t_resetn = 1'b1;
    repeat (10) @(posedge C_clk);

    // ---- phase 1: traffic is FLOWING when the swap lands ---------------------
    // Do not wait for phase-1 to finish. A swap that arrives after the stream
    // has drained finds the target idle in TXST, and then every policy passes.
    nbytes = n1;
    stim_rstn = 1'b0;
    repeat (4) @(posedge C_clk);
    #0.1 stim_rstn = 1'b1; drain = 1'b0;
    watch_en = 1'b1;

    fork
      begin wait (total_got >= swap_at); end
      begin wait (stall_cnt >= STALL_LIM);
            fail_reason = "phase-1: link stalled before the swap even fired"; end
    join_any
    disable fork;

    if (fail_reason != "" || errs_all != 0) begin
      $display("    LIVE SWAP FAIL  phase-1 broken before the swap (%0d errs) -- the",
               errs_all);
      $display("                    bench never worked; the swap is not implicated.");
      $finish;
    end
    $display("    phase-1  %0d bytes delivered, byte-exact, link still streaming", total_got);

    // Hold until the target is where we want to catch it. Default: anything but
    // idle, i.e. genuinely mid-transaction.
    begin : catch_state
      logic [7:0] want;
      int guard;
      want  = (swap_state_s == "") ? 8'hxx : state_of(swap_state_s);
      guard = 0;
      forever begin
        @(posedge T_clk);
        if (swap_state_s == "") begin
          if (`TFSM !== TXST) break;
        end else if (`TFSM === want) break;
        if (++guard > 200_000) begin
          $display("    LIVE SWAP FAIL  never caught the target in state %s",
                   (swap_state_s == "") ? "!=TXST" : swap_state_s);
          $finish;
        end
      end
    end
    // Freeze the boundary IN THE SAME TIME STEP we catch the state. Any delay
    // and the still-live target walks on out of the state we wanted to trap.
    swap_fsm_state = `TFSM;
    decouple = 1'b1;                    // safe values: ioreq1=0, ioreq2=0, iodata4=0
    drain    = 1'b1;                    // sinks swallow whatever is in flight
    esc_ioreq2 = 1'b0;
    watch_en = 1'b0;
    // Silence the sources too. Left running they keep pushing phase-1 bytes into
    // the recoupled link, and one lands as phase-2's "byte 0" no matter which
    // policy ran -- which made all three policies fail identically. The sinks
    // still consume (drain forces tready) but score nothing while in reset.
    #0.1 stim_rstn = 1'b0;
    $display("    swap    fires with the target mid-transaction, in %s", name_of(swap_fsm_state));

    repeat (100) @(posedge C_clk);      // link quiesces under the safe values
    $display("    decoup  after the safe values settle, target sits in %s", name_of(`TFSM));

    // ---- the swap -----------------------------------------------------------
    #0.1 c_resetn = 1'b0;               // ICAP overwrites the RP
    repeat (40) @(posedge C_clk);

    if (pol_treset) begin
      #0.1 t_resetn = 1'b0;
      repeat (10) @(posedge T_clk);
      #0.1 t_resetn = 1'b1;
      repeat (10) @(posedge T_clk);
    end else if (pol_escape) begin
      for (int i = 0; i < 3; i++) begin
        esc_ioreq2 = ~esc_ioreq2;
        repeat (8) @(posedge T_clk);
      end
      esc_ioreq2 = 1'b0;
      repeat (8) @(posedge T_clk);
    end

    $display("    policy  applied, target now in %s", name_of(`TFSM));

    #0.1 c_resetn = 1'b1;               // the new DUT is loaded and released
    repeat (20) @(posedge C_clk);
    decouple = 1'b0;                    // recouple the boundary
    repeat (400) @(posedge C_clk);      // flush anything the link emits, drain high
    $display("    recoup  400 C_clk after recouple, target sits in %s", name_of(`TFSM));

    // `drain` stays high until run_phase() drops it in lockstep with the
    // stimulus reset -- see the note there.
    // ---- phase 2: does the link come back? ----------------------------------
    run_phase("phase-2", n2);

    $display("");
    $display("    sent   C.rx0=%0d C.rx1=%0d  T.rx0=%0d T.rx1=%0d", sent_c0, sent_c1, sent_t0, sent_t1);
    $display("    got    T.tx0=%0d T.tx1=%0d  C.tx0=%0d C.tx1=%0d", got_t0, got_t1, got_c0, got_c1);
    $display("    errors T.tx0=%0d T.tx1=%0d  C.tx0=%0d C.tx1=%0d", err_t0, err_t1, err_c0, err_c1);
    if (fail_reason == "")
      $display("    LIVE SWAP PASS  (the link recovered, %0t)", $time);
    else
      $display("    LIVE SWAP FAIL  %s", fail_reason);
    $display("");
    $finish;
  end

endmodule
