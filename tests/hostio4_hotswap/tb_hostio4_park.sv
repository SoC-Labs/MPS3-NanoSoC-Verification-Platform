// tb_hostio4_park — L2a: can a DFX decoupler park the hostio4 target?
//
// THE QUESTION
//   In the harness the hostio4 CONTROLLER lives inside the reconfigurable
//   partition (nanoSoC drives ioreq1/ioreq2, receives ioack -- the P1 mapping).
//   The TARGET lives in the static shell. During a partial reconfiguration the
//   controller simply vanishes and the DFX decoupler drives constant "safe"
//   values onto the boundary signals, then the new DUT comes up reset.
//
//   So: is there ANY constant (ioreq1, ioreq2) the decoupler can drive that
//   returns the target to its idle state TXST from every state it might be
//   caught in? If not, the shell must reset the target on every swap.
//
// WHY IT MATTERS
//   ioack_o = fsm_state[0]. A target stuck in an odd-numbered state presents a
//   permanently-asserted ack, and tests/hostio4_link_cdc's neg_ack_stuck
//   control already proved a stuck ack hangs the controller forever. A wedged
//   target therefore hangs the NEXT DUT's first hostio4 transaction -- after
//   the swap, with no obvious culprit.
//
// METHOD
//   Instantiate the target alone. For each of its 9 states, force fsm_state,
//   hold a constant (ioreq1, ioreq2), release, and run PARK_CYCLES target
//   clocks. Ask whether it reached TXST. 9 states x 4 combinations.
//   Then do the same for a toggling escape sequence on ioreq2.
//
// Prints a matrix and a verdict line the Makefile greps.

`timescale 1ns/1ps

module tb_hostio4_park;

  localparam int PARK_CYCLES = 64;   // 30x the 2FF latency -- generous

  // Target FSM encoding, copied from hostio4_target_fsm.v. If these ever drift
  // the forced state will not match a case arm and the FSM lands in `default`
  // (= TXST), which would silently make every experiment "park". The
  // self-check below catches exactly that.
  localparam logic [7:0] TXST = 8'b0_001_000_0;
  localparam logic [7:0] RXC1 = 8'b0_000_001_1;
  localparam logic [7:0] RXDH = 8'b0_000_010_0;
  localparam logic [7:0] RXDL = 8'b0_000_100_1;
  localparam logic [7:0] RXDZ = 8'b0_000_000_0;
  localparam logic [7:0] TXSZ = 8'b0_001_000_1;
  localparam logic [7:0] TXCZ = 8'b1_000_000_0;
  localparam logic [7:0] TXDH = 8'b1_010_000_1;
  localparam logic [7:0] TXDL = 8'b1_100_000_0;

  logic [7:0] states [9];
  string      names  [9];

  initial begin
    states = '{TXST, RXC1, RXDH, RXDL, RXDZ, TXSZ, TXCZ, TXDH, TXDL};
    names  = '{"TXST","RXC1","RXDH","RXDL","RXDZ","TXSZ","TXCZ","TXDH","TXDL"};
  end

  logic clk = 1'b0, resetn = 1'b0, testmode = 1'b0;
  always #5.0 clk = ~clk;   // 100 MHz shell clock

  logic       ioreq1 = 1'b0, ioreq2 = 1'b0;
  logic [3:0] iodata4_drv = 4'h0;

  wire [3:0] t_o, t_e, t_t;
  wire       ioack;

  // AXIS ports: sources idle, sinks always ready. We only care about the FSM.
  wire rx0_tready, rx1_tready, tx0_tvalid, tx1_tvalid;
  wire [7:0] tx0_tdata8, tx1_tdata8;

  hostio4_target u_target (
    .clk(clk), .resetn(resetn), .testmode(testmode),
    .axis_rx0_tready(rx0_tready), .axis_rx0_tvalid(1'b0), .axis_rx0_tdata8(8'h00),
    .axis_rx1_tready(rx1_tready), .axis_rx1_tvalid(1'b0), .axis_rx1_tdata8(8'h00),
    .axis_tx0_tready(1'b1), .axis_tx0_tvalid(tx0_tvalid), .axis_tx0_tdata8(tx0_tdata8),
    .axis_tx1_tready(1'b1), .axis_tx1_tvalid(tx1_tvalid), .axis_tx1_tdata8(tx1_tdata8),
    .iodata4_i(iodata4_drv), .iodata4_o(t_o), .iodata4_e(t_e), .iodata4_t(t_t),
    .ioreq1_a(ioreq1), .ioreq2_a(ioreq2), .ioack_o(ioack)
  );

  `define FSM u_target.u_hostio4_target_fsm.fsm_state

  int n_wedge_total;
  int wedge_per_combo [4];

  // `force` may not take an automatic variable as its RHS (IEEE 1800),
  // so the target state is staged here, in static module scope.
  logic [7:0] force_val;

  // Drive a constant (r1,r2) from state `s` and report whether we reach TXST.
  function automatic bit reached_idle();
    return (`FSM === TXST);
  endfunction

  task automatic try_static(input logic [7:0] s, input bit r1, input bit r2,
                            output bit parked, output logic [7:0] final_state);
    // Reset, then force the state and let the 2FFs settle to (r1,r2).
    resetn = 1'b0; ioreq1 = r1; ioreq2 = r2;
    repeat (4) @(posedge clk);
    #0.1 resetn = 1'b1;
    repeat (6) @(posedge clk);           // synchronisers settle to (r1,r2)
    force_val = s;
    force `FSM = force_val;
    repeat (2) @(posedge clk);
    release `FSM;
    repeat (PARK_CYCLES) @(posedge clk);
    parked      = reached_idle();
    final_state = `FSM;
  endtask

  // The decoupler could instead be made to TOGGLE ioreq2 a few times before
  // going quiet. Does that park the target from every state?
  task automatic try_escape(input logic [7:0] s, input int n_toggles,
                            output bit parked, output logic [7:0] final_state);
    resetn = 1'b0; ioreq1 = 1'b0; ioreq2 = 1'b0;
    repeat (4) @(posedge clk);
    #0.1 resetn = 1'b1;
    repeat (6) @(posedge clk);
    force_val = s;
    force `FSM = force_val;
    repeat (2) @(posedge clk);
    release `FSM;
    for (int i = 0; i < n_toggles; i++) begin
      ioreq2 = ~ioreq2;
      repeat (6) @(posedge clk);         // >= 2FF latency + margin
    end
    ioreq1 = 1'b0; ioreq2 = 1'b0;
    repeat (PARK_CYCLES) @(posedge clk);
    parked      = reached_idle();
    final_state = `FSM;
  endtask

  // What if the decoupler holds ioreq quiet but leaves iodata4 UNDRIVEN?
  // hostio4_target_fsm has  RXC1: ... (iodata4_i[0]) ? TXCZ : RXDH
  // A `z` condition is not "false" -- Verilog merges both branches bitwise and
  // yields X. Ask whether a floating bus poisons the state register.
  task automatic try_float(input logic [7:0] s, input bit r1, input bit r2,
                           output bit saw_x, output logic [7:0] final_state);
    resetn = 1'b0; ioreq1 = r1; ioreq2 = r2; iodata4_drv = 4'h0;
    repeat (4) @(posedge clk);
    #0.1 resetn = 1'b1;
    repeat (6) @(posedge clk);
    force_val = s;
    force `FSM = force_val;
    // Float the bus BEFORE releasing. `release` lets the NBA scheduled at the
    // last forced edge land, and that NBA's RHS was evaluated when the data was
    // still driven -- so floating afterwards lets the FSM skip the very
    // data-dependent arm we are trying to test. (Cost me a false "FLOAT SAFE".)
    iodata4_drv = 4'bzzzz;
    repeat (2) @(posedge clk);
    release `FSM;
    saw_x = 1'b0;
    for (int i = 0; i < PARK_CYCLES; i++) begin
      @(posedge clk);
      if ($isunknown(`FSM)) saw_x = 1'b1;
    end
    final_state = `FSM;
    iodata4_drv = 4'h0;
  endtask

  function automatic string sname(input logic [7:0] s);
    for (int i = 0; i < 9; i++) if (states[i] === s) return names[i];
    return $sformatf("0b%b", s);
  endfunction

  bit         parked, saw_x;
  int         n_x;
  logic [7:0] fin;
  int         min_toggles;

  initial begin
    #1;

    // ---- self-check ---------------------------------------------------------
    // If the state encoding above ever drifts from the RTL, `force` lands on a
    // value no case arm matches, the FSM falls through to `default` (= TXST),
    // and EVERY experiment below reports "parks" -- a silent, total false pass.
    //
    // Detect that WITHOUT assuming the answer we are trying to measure: drive a
    // transition the case table must make. TXST + ioreq1 -> RXC1 exercises the
    // TXST arm and confirms the RXC1 encoding. Then TXST must hold under (0,0).
    resetn = 1'b0; ioreq1 = 1'b1; ioreq2 = 1'b0;
    repeat (4) @(posedge clk);
    #0.1 resetn = 1'b1;
    repeat (10) @(posedge clk);          // 2FF settles, TXST sees ioreq1_s=1
    if (`FSM !== RXC1) begin
      $display("  *** SELF-CHECK FAILED: TXST + ioreq1 did not reach RXC1 (got %s).",
               sname(`FSM));
      $display("      The state encoding in this file has drifted from the RTL, or");
      $display("      the FSM changed. Every result below would be meaningless.");
      $fatal(1);
    end
    try_static(TXST, 1'b0, 1'b0, parked, fin);
    if (!parked) begin
      $display("  *** SELF-CHECK FAILED: TXST does not hold idle under (0,0).");
      $fatal(1);
    end
    $display("  self-check OK: encoding matches the RTL (TXST+ioreq1 -> RXC1),");
    $display("                 the force takes, and TXST is a fixed point of (0,0)");
    $display("");

    // ---- exhaustive: 9 states x 4 static decoupler values -------------------
    $display("  Can a CONSTANT (ioreq1,ioreq2) park the target? (. = parks to TXST)");
    $display("");
    $display("            (0,0)      (0,1)      (1,0)      (1,1)");
    for (int si = 0; si < 9; si++) begin
      automatic string row;
      row = $sformatf("    %-6s", names[si]);
      for (int c = 0; c < 4; c++) begin
        try_static(states[si], c[1], c[0], parked, fin);
        if (parked) row = {row, "  .        "};
        else begin
          row = {row, $sformatf("  WEDGE:%-4s", sname(fin))};
          n_wedge_total++;
          wedge_per_combo[c]++;
        end
      end
      $display("%s", row);
    end
    $display("");
    for (int c = 0; c < 4; c++)
      $display("    (%0d,%0d) wedges %0d of 9 states", c[1], c[0], wedge_per_combo[c]);
    $display("");

    if (wedge_per_combo[0] == 0 || wedge_per_combo[1] == 0 ||
        wedge_per_combo[2] == 0 || wedge_per_combo[3] == 0) begin
      $display("  RESULT: a static decoupler value CAN park the target.");
      $display("  PARK STATIC OK");
    end else begin
      $display("  RESULT: NO constant (ioreq1,ioreq2) parks the target from every");
      $display("          state. The FSM advances on ioreq2 TRANSITIONS, so a held");
      $display("          level cannot unwind it. ioack_o = fsm_state[0], so a");
      $display("          wedged target presents a stuck ack and will hang the next");
      $display("          DUT's first transaction after the swap.");
      $display("  PARK STATIC IMPOSSIBLE");
    end
    $display("");

    // ---- can a toggle sequence escape instead? -----------------------------
    $display("  Does toggling ioreq2 (with ioreq1 low) park it? finding minimum:");
    min_toggles = -1;
    for (int nt = 1; nt <= 6 && min_toggles < 0; nt++) begin
      automatic bit all_ok = 1;
      for (int si = 0; si < 9; si++) begin
        try_escape(states[si], nt, parked, fin);
        if (!parked) all_ok = 0;
      end
      $display("    %0d toggle(s): %s", nt, all_ok ? "parks from ALL 9 states" : "not yet");
      if (all_ok) min_toggles = nt;
    end
    $display("");
    if (min_toggles > 0)
      $display("  PARK ESCAPE OK  min_toggles=%0d", min_toggles);
    else
      $display("  PARK ESCAPE IMPOSSIBLE  (up to 6 toggles)");
    $display("");

    // ---- and if the decoupler leaves iodata4 undriven? ----------------------
    $display("  iodata4 left FLOATING (z) by the decoupler -- does fsm_state go X?");
    $display("    (with ioreq2 LOW the outer ternary short-circuits, so the float is");
    $display("     never evaluated. The honest test is ioreq2 HIGH.)");
    for (int c = 0; c < 4; c++) begin
      automatic int nx = 0;
      automatic string who = "";
      for (int si = 0; si < 9; si++) begin
        try_float(states[si], c[1], c[0], saw_x, fin);
        if (saw_x) begin nx++; who = {who, " ", names[si]}; end
      end
      n_x += nx;
      if (nx == 0) $display("    ioreq=(%0d,%0d): no state goes X", c[1], c[0]);
      else         $display("    ioreq=(%0d,%0d): %0d of 9 go X ->%s", c[1], c[0], nx, who);
    end
    $display("");
    if (n_x == 0) begin
      $display("    A floating iodata4 never poisons the state register.");
      $display("  FLOAT SAFE");
    end else begin
      $display("    RXC1 branches on the RAW iodata4_i[0]; a `z` condition is not");
      $display("    `false` -- Verilog X-merges both arms. The decoupler must DRIVE");
      $display("    iodata4 to a defined value, not leave it undriven.");
      $display("  FLOAT UNSAFE");
    end

    $display("");
    $finish;
  end

endmodule
