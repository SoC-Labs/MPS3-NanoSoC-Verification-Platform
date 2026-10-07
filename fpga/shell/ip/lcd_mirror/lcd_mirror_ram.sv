// -----------------------------------------------------------------------------
// lcd_mirror_ram.sv -- simple dual-port block RAM for the mirror's frame buffer.
//
// One write port (the snooper), one read port (the AXI-Lite slave), one clock.
// Written as the Vivado block-RAM inference template: synchronous write,
// registered read, plus an output register (DO*_REG=1) -- read latency 2
// cycles. No reset on the array (BRAM contents cannot be reset); the output
// registers are not reset either, the consumer samples them two cycles after
// presenting an address. lcd_mirror.sv instantiates two of these (even and odd
// pixels), so there is no sub-word write enable to infer.
//
// OLD_DATA = 1 adds a READ-FIRST read on the WRITE port: `wold` returns, two
// cycles after a write, the word that write REPLACED. That is compare-on-write
// dirty tracking (HM LCD_MIRROR.md §4.2 / H4) at no BRAM cost: the RAMB36 goes
// from simple-dual-port to true-dual-port (port A read-first RW, port B read),
// which at x16/x18 per port holds the same 2K words per RAMB36. OLD_DATA = 0 is
// the plain SDP (Vivado trims the unused read).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module lcd_mirror_ram #(
  parameter int DEPTH = 38400,
  parameter int WIDTH = 16,
  parameter int AW    = 16,
  parameter int OLD_DATA = 0
) (
  input  logic             clk,
  input  logic             we,
  input  logic [AW-1:0]    waddr,
  input  logic [WIDTH-1:0] wdata,
  output logic [WIDTH-1:0] wold,      // OLD_DATA=1: the replaced word, 2 cycles later
  input  logic [AW-1:0]    raddr,
  output logic [WIDTH-1:0] rdata
);

  (* ram_style = "block" *) logic [WIDTH-1:0] mem [0:DEPTH-1];

  // Power-on content: black. Vivado honours this as the BRAM INIT; the bench
  // relies on it for a frame that was never written.
  initial begin
    for (int i = 0; i < DEPTH; i++) mem[i] = '0;
  end

  logic [WIDTH-1:0] rd_r, rd_q, old_r, old_q;

  // Port A. Plain `always`, not always_ff: the array also has an initial block
  // (the power-on content), and SystemVerilog forbids any second writer of a
  // variable an always_ff drives. The read in the same process, before the
  // non-blocking write lands, is the read-first template.
  always @(posedge clk) begin
    if (we) mem[waddr] <= wdata;
    if (OLD_DATA != 0) old_r <= mem[waddr];
  end

  always_ff @(posedge clk) old_q <= old_r;
  assign wold = (OLD_DATA != 0) ? old_q : '0;

  always_ff @(posedge clk) begin
    rd_r <= mem[raddr];
    rd_q <= rd_r;
  end

  assign rdata = rd_q;

endmodule
