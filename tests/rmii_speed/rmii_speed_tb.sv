// =============================================================================
// tests/rmii_speed/rmii_speed_tb.sv
//
// Drives a 100 Mb/s RMII receive stream (one di-bit per 50 MHz REF_CLK) into
// the THIRD-PARTY rmii_to_mii bridge and checks what comes out on its MII RX
// port, for a given value of `mode_speed`.
//
// WHY THIS BENCH EXISTS
// ---------------------
// nanosoc_multicore_vivado_wrapper.v instantiates rmii_to_mii and does NOT
// connect .mode_speed. Vivado's own log says so:
//     WARNING: [Synth 8-7071] port 'mode_speed' of module 'rmii_to_mii' is
//     unconnected for instance 'u_rmii_bridge'
// and speed_div_reg[3:0] survives into the routed netlist, which it could not
// if mode_speed had resolved to 1 (line 145 would hold the counter at zero).
// So the shipped bitstream runs the bridge with mode_speed = 0.
//
// rmii_to_mii.v: "1 = 100 Mbps, 0 = 10 Mbps"; tick = mode_speed | (speed_div==0).
// Meanwhile the firmware writes ANAR = 0x0181 -> advertises 100BASE-TX ONLY.
// So the PHY negotiates 100 Mb/s into a bridge clocked for 10 Mb/s.
//
// This bench is a FALSIFICATION test, not a confirmation:
//   mode_speed=1  MUST recover the frame            (positive control)
//   mode_speed=0  MUST FAIL to recover it           (the defect)
// If mode_speed=0 recovers the frame, the defect claim is WRONG and the
// Makefile says so loudly and exits non-zero.
//
// ALIGNMENT: the stimulus is a real MII/RMII frame -- 7x 0x55 preamble then the
// 0xD5 SFD -- and the receiver hunts for the SFD nibble pair exactly as a MAC
// does. Without that, a one-nibble offset silently corrupts every byte and the
// bench reports a failure that is its own fault. (It did, on the first run.)
//
// NOTE: tests/rmii_conformance/ cannot answer this. It compiles a DIFFERENT
// copy of rmii_to_mii (md5 5dd9b16f) whose header says "Removed the mode_speed
// input -- hard-wired to 100 Mbps operation". This bench compiles the copy the
// multicore actually ships (md5 5e38c43c). The Makefile refuses to run against
// a copy without the port. See README.md.
// =============================================================================
`timescale 1ns / 1ps

module rmii_speed_tb;

  localparam int NPRE     = 7;          // 0x55 preamble bytes
  localparam int NBYTES   = 16;         // payload bytes
  localparam int MAXNIB   = 512;
  localparam int TIMEOUT_NS = 2_000_000;

  // ---- 50 MHz RMII reference clock -----------------------------------------
  logic refclk = 1'b0;
  always #10 refclk = ~refclk;          // 20 ns period

  logic rstn = 1'b0;
  logic mode_speed;

  // ---- RMII receive stimulus (PHY -> MAC) ----------------------------------
  logic [1:0] rmii_rxd    = 2'b00;
  logic       rmii_crs_dv = 1'b0;

  // ---- DUT -----------------------------------------------------------------
  wire [1:0] rmii_txd;
  wire       rmii_tx_en;
  wire       mtx_clk, mrx_clk, mrxdv, mrxerr, mcoll, mcrs;
  wire [3:0] mrxd;

  rmii_to_mii dut (
    .RESETn       (rstn),
    .mode_speed   (mode_speed),
    .rmii_ref_clk (refclk),
    .rmii_txd     (rmii_txd),
    .rmii_tx_en   (rmii_tx_en),
    .rmii_rxd     (rmii_rxd),
    .rmii_crs_dv  (rmii_crs_dv),
    .mtx_clk      (mtx_clk),
    .mtxd         (4'h0),
    .mtxen        (1'b0),
    .mtxerr       (1'b0),
    .mrx_clk      (mrx_clk),
    .mrxd         (mrxd),
    .mrxdv        (mrxdv),
    .mrxerr       (mrxerr),
    .mcoll        (mcoll),
    .mcrs         (mcrs)
  );

  logic [7:0] tx_payload [0:NBYTES-1];

  // ---- capture every MII RX nibble; align later, like a MAC -----------------
  logic [3:0] nib [0:MAXNIB-1];
  int         nnib = 0;

  always @(posedge mrx_clk) begin
    if (rstn && mrxdv && nnib < MAXNIB) begin
      nib[nnib] = mrxd;     // blocking: this array is only read after $finish-time
      nnib      = nnib + 1;
    end
  end

  // ---- measure the MII RX clock period --------------------------------------
  real t_prev = 0.0, mrx_period = 0.0;
  int  edges = 0;
  always @(posedge mrx_clk) begin
    if (rstn) begin
      edges = edges + 1;
      if (edges > 2) mrx_period = $realtime - t_prev;
      t_prev = $realtime;
    end
  end

  // ---- stimulus: one di-bit per REF_CLK = 100 Mb/s RMII ---------------------
  task automatic put_byte(input logic [7:0] b);
    int d;
    for (d = 0; d < 4; d++) begin          // di-bits, LSB pair first
      rmii_rxd <= b[2*d +: 2];
      @(negedge refclk);
    end
  endtask

  task automatic drive_frame();
    int i;
    @(negedge refclk);
    rmii_crs_dv <= 1'b1;
    for (i = 0; i < NPRE; i++) put_byte(8'h55);   // preamble
    put_byte(8'hD5);                              // SFD
    for (i = 0; i < NBYTES; i++) put_byte(tx_payload[i]);
    rmii_crs_dv <= 1'b0;
    rmii_rxd    <= 2'b00;
  endtask

  // ---- main -----------------------------------------------------------------
  int i, j, sfd_at, errors, recovered;
  logic [7:0] got;
  string verdict;

  initial begin
    if (!$value$plusargs("mode_speed=%d", i)) begin
      $display("RMII_SPEED: FATAL -- pass +mode_speed=0 or +mode_speed=1");
      $finish;
    end
    mode_speed = i[0];

    for (i = 0; i < NBYTES; i++) tx_payload[i] = 8'hA0 + i[7:0];

    repeat (8) @(negedge refclk);
    rstn = 1'b1;
    repeat (8) @(negedge refclk);

    drive_frame();
    repeat (mode_speed ? 400 : 4000) @(negedge refclk);   // drain

    // ---- MAC-style alignment: find the SFD nibble pair (0x5 then 0xD) -------
    sfd_at = -1;
    for (i = 0; i + 1 < nnib; i++)
      if (nib[i] === 4'h5 && nib[i+1] === 4'hD) begin
        sfd_at = i + 2;   // payload starts here, low nibble first
        break;
      end

    errors = 0; recovered = 0;
    if (sfd_at < 0) begin
      errors = NBYTES;    // never synchronised
    end else begin
      for (j = 0; j < NBYTES; j++) begin
        if (sfd_at + 2*j + 1 < nnib) begin
          got = {nib[sfd_at + 2*j + 1], nib[sfd_at + 2*j]};   // low nibble first
          recovered++;
          if (got !== tx_payload[j]) errors++;
        end else begin
          errors++;   // truncated
        end
      end
    end

    verdict = (errors == 0 && recovered == NBYTES) ? "PASS" : "FAIL";

    $display("");
    $display("RMII_SPEED: mode_speed=%0d  mrx_clk_period=%0.1f ns  nibbles=%0d  sfd_at=%0d  recovered=%0d/%0d  errors=%0d  VERDICT=%s",
             mode_speed, mrx_period, nnib, sfd_at, recovered, NBYTES, errors, verdict);
    $display("RMII_SPEED: mrx_clk should be  40.0 ns (25 MHz, 100 Mb/s) when mode_speed=1");
    $display("RMII_SPEED:                   400.0 ns (2.5 MHz, 10 Mb/s) when mode_speed=0");
    $display("");
    $finish;
  end

  initial begin
    #(TIMEOUT_NS);
    $display("RMII_SPEED: mode_speed=%0d  TIMEOUT  nibbles=%0d  VERDICT=FAIL", mode_speed, nnib);
    $finish;
  end

endmodule
