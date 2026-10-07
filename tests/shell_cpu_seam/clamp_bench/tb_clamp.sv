// -----------------------------------------------------------------------------
// tests/shell_cpu_seam/clamp_bench/tb_clamp.sv
//
// Does the DFX decoupler clamp ASSERT in a given shell BD? The real dfx_ctl.sv,
// at the width the BD instantiates it (C_S_AXI_ADDR_WIDTH = 32 -- the CSR
// decode regression was a bench at the RTL default of 12), wired to POR and
// the watchdog EXACTLY as the BD dump says it is.
//
// WHY A BENCH AND NOT JUST A NET CHECK. The July Linux fork left
// dfx_ctl_0/ext_por_n_i and wdt_reset_i unconnected. IPI ties a dangling input
// to 0, which is a legal design and validates clean; what it MEANS is only
// visible in the RTL: ext_por_n_i = 0 reads as "POR held forever", and POR is
// the one reset that forces decouple_en_q to 0 -- every cycle. Firmware writes
// DECOUPLE=1, the write is accepted, and the clamp never asserts. A mid-swap RP
// then drives the static side unclamped. The net check (tools/shell_bd_guards
// .tcl, and check_clamp_wiring.py on the dump) says WHAT is wired; this says
// what that wiring DOES.
//
// The two isolation resets come from the dump (tests/shell_cpu_seam/golden/
// clamp_wiring_<cpu>.txt, via the pytest):
//   driven   -> the bench drives the pin from its real source (sys_rst_n / the
//               WDOG's wdt_reset);
//   UNDRIVEN -> `define POR_UNDRIVEN / WDT_UNDRIVEN: tied 0, as IPI would.
// The decoupler itself is Xilinx IP; one representative leg is modelled with
// the semantics the BD configures (MANAGEMENT manual, DECOUPLED_VALUE 0x0):
// static side = decouple ? 0 : RP side, and decouple_status follows decouple.
//
// Prints CLAMP_BENCH PASS, or CLAMP_BENCH FAIL <step>, then $finish.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_clamp;

  logic clk = 1'b0;
  always #5 clk = ~clk;

  logic sys_rst_n;   // USER_nPB0: the board POR (BD port sys_rst_n)
  logic wdt_reset;   // axi_timebase_wdt_0/wdt_reset (2nd expiry)
  logic aresetn;     // proc_sys_reset_shell/peripheral_aresetn: POR OR WDOG

`ifdef POR_UNDRIVEN
  wire ext_por_n = 1'b0;         // IPI's tie for a dangling input
`else
  wire ext_por_n = sys_rst_n;
`endif
`ifdef WDT_UNDRIVEN
  wire wdt_in = 1'b0;
`else
  wire wdt_in = wdt_reset;
`endif

  // AXI4-Lite master side
  logic [31:0] awaddr = '0, wdata = '0, araddr = '0;
  logic        awvalid = 0, wvalid = 0, bready = 0, arvalid = 0, rready = 0;
  logic [3:0]  wstrb = 4'hF;
  wire         awready, wready, bvalid, arready, rvalid;
  wire [1:0]   bresp, rresp;
  wire [31:0]  rdata;

  // decoupler model: one RP -> static leg (rm_id, clamp value 0)
  wire         decouple_en;
  wire [31:0]  rp_rm_id = 32'hDEAD_BEEF;          // a half-configured RP: garbage
  wire [31:0]  s_rm_id  = decouple_en ? 32'h0 : rp_rm_id;
  wire         decouple_status = decouple_en;
  wire         rp_resetn_gate, shutdown_req;

  dfx_ctl #(.C_S_AXI_ADDR_WIDTH(32)) u_dfx_ctl (
    .s_axi_aclk         (clk),
    .s_axi_aresetn      (aresetn),
    .ext_por_n_i        (ext_por_n),
    .wdt_reset_i        (wdt_in),
    .s_axi_awaddr       (awaddr),
    .s_axi_awprot       (3'b000),
    .s_axi_awvalid      (awvalid),
    .s_axi_awready      (awready),
    .s_axi_wdata        (wdata),
    .s_axi_wstrb        (wstrb),
    .s_axi_wvalid       (wvalid),
    .s_axi_wready       (wready),
    .s_axi_bresp        (bresp),
    .s_axi_bvalid       (bvalid),
    .s_axi_bready       (bready),
    .s_axi_araddr       (araddr),
    .s_axi_arprot       (3'b000),
    .s_axi_arvalid      (arvalid),
    .s_axi_arready      (arready),
    .s_axi_rdata        (rdata),
    .s_axi_rresp        (rresp),
    .s_axi_rvalid       (rvalid),
    .s_axi_rready       (rready),
    .decouple_en_o      (decouple_en),
    .decoupled_i        (decouple_status),
    .axi_shutdown_req_o (shutdown_req),
    .axi_shutdown_ack_i (shutdown_req),
    .rp_resetn_gate_o   (rp_resetn_gate),
    .rp_in_reset_i      (~rp_resetn_gate),
    .rm_id_i            (s_rm_id),
    .dut_lockup_i       (1'b0),
    .dut_eth_irq_i      (1'b0)
  );

  // DFXCTL.DECOUPLE is offset 0x00 of the 0x44A1_0000 page; the BD hands the
  // block the full 32-bit address, exactly as here.
  localparam logic [31:0] DECOUPLE = 32'h44A1_0000;

  task automatic axi_write(input logic [31:0] a, input logic [31:0] d);
    int n;
    @(negedge clk);
    awaddr = a; wdata = d; awvalid = 1; wvalid = 1; bready = 1;
    n = 0;
    while (!(awready && wready)) begin @(negedge clk); n++; if (n > 50) begin $display("CLAMP_BENCH FAIL axi-write-stall"); $finish; end end
    @(negedge clk);
    awvalid = 0; wvalid = 0;
    n = 0;
    while (!bvalid) begin @(negedge clk); n++; if (n > 50) begin $display("CLAMP_BENCH FAIL axi-bresp-stall"); $finish; end end
    @(negedge clk);
    bready = 0;
  endtask

  task automatic expect_clamp(input logic want, input string step);
    repeat (4) @(negedge clk);
    if (decouple_en !== want || s_rm_id !== (want ? 32'h0 : rp_rm_id)) begin
      $display("CLAMP_BENCH FAIL %s: decouple_en=%b s_rm_id=%h (want clamp=%0d)",
               step, decouple_en, s_rm_id, want);
      $finish;
    end
    $display("  ok  %s: decouple_en=%b s_rm_id=%h", step, decouple_en, s_rm_id);
  endtask

  initial begin
    // power-on: POR held, peripheral reset held with it
    sys_rst_n = 0; wdt_reset = 0; aresetn = 0;
    repeat (20) @(negedge clk);
    sys_rst_n = 1;
    repeat (10) @(negedge clk);
    aresetn = 1;
    expect_clamp(1'b0, "after-POR: boundary open");

    axi_write(DECOUPLE, 32'h1);
    expect_clamp(1'b1, "DECOUPLE=1: clamp asserts");       // the fork failed HERE

    axi_write(DECOUPLE, 32'h0);
    expect_clamp(1'b0, "DECOUPLE=0: clamp releases");

    // the watchdog: wdt_reset pulses and pulls the peripheral reset with it
    // (proc_sys_reset_shell aux_reset_in). SET-dominant: the clamp asserts and
    // must SURVIVE the peripheral reset that follows.
    @(negedge clk); wdt_reset = 1; aresetn = 0;
    repeat (4) @(negedge clk); wdt_reset = 0;
    repeat (16) @(negedge clk); aresetn = 1;
    expect_clamp(1'b1, "WDOG: clamp asserts and survives aresetn");

    // POR, and only POR, releases it
    @(negedge clk); sys_rst_n = 0; aresetn = 0;
    repeat (10) @(negedge clk); sys_rst_n = 1;
    repeat (10) @(negedge clk); aresetn = 1;
    expect_clamp(1'b0, "POR: clamp released");

    $display("CLAMP_BENCH PASS");
    $finish;
  end

  initial begin
    #100000 $display("CLAMP_BENCH FAIL timeout");
    $finish;
  end
endmodule
