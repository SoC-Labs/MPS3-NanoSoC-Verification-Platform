// counter.sv — sim_smoke sanity DUT (W-SIM, closes the "prove the harness
// independent of any real RTL" half of I24). Deliberately trivial and
// self-contained in tests/sim_smoke/ (NOT under fpga/ — this is test
// infrastructure, owned by A5, and must never be mistaken for shell RTL).
// If `make -C tests BLOCK=sim_smoke run-one` is red, the problem is the
// simulator/cocotb/license environment, not any DUT.
`timescale 1ns / 1ps

module smoke_counter #(
  parameter int WIDTH = 8
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             en,
  output logic [WIDTH-1:0] count
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)   count <= '0;
    else if (en)  count <= count + 1'b1;
  end
endmodule
