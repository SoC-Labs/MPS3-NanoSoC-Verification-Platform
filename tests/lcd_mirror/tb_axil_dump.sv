// -----------------------------------------------------------------------------
// tb_axil_dump.sv -- a bench-only AXI4-Lite READ master that reads a range of
// words from the slave and appends them to a text file, one hex word per line.
//
// Why it exists: a pixel-for-pixel check reads the whole frame buffer back --
// 38,400 AXI-Lite reads per frame. Through the cocotb BFM (tests/common/
// regmap.py) that is ~40 s of Python per frame; this SV master does the same
// reads through the SAME slave port in well under a second, so every stream
// can be checked in full. It is a real AXI-Lite master (ARVALID held until
// ARREADY, RREADY held until RVALID, one read in flight) and the bench also
// spot-checks words through the cocotb BFM so the two masters vouch for each
// other.
//
// Control: pulse `go` for one cycle with base/count set; `busy` falls when the
// last word has been written. The output path comes from +DUMP_FILE=<path>.
// While `busy` is low it drives nothing (ARVALID/RREADY low) and the owning
// testbench muxes the cocotb BFM onto the slave instead.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_axil_dump #(
  parameter int AW = 32
) (
  input  logic          clk,
  input  logic          go,
  input  logic [AW-1:0] base,
  input  logic [31:0]   count,
  output logic          busy,
  // AXI4-Lite read channel, master side
  output logic [AW-1:0] araddr,
  output logic          arvalid,
  input  logic          arready,
  input  logic [31:0]   rdata,
  input  logic          rvalid,
  output logic          rready
);

  string  path;
  integer fd;
  logic [31:0] left;
  logic [AW-1:0] addr;
  logic in_ar;

  initial begin
    busy    = 1'b0;
    arvalid = 1'b0;
    rready  = 1'b0;
    araddr  = '0;
    in_ar   = 1'b0;
    fd      = 0;
    if (!$value$plusargs("DUMP_FILE=%s", path)) path = "fb_dump.hex";
  end

  always @(posedge clk) begin
    if (!busy) begin
      if (go) begin
        fd      <= $fopen(path, "w");
        addr    <= base;
        left    <= count;
        busy    <= 1'b1;
        araddr  <= base;
        arvalid <= 1'b1;
        rready  <= 1'b1;
        in_ar   <= 1'b1;
      end
    end else begin
      if (in_ar && arvalid && arready) begin
        arvalid <= 1'b0;
        in_ar   <= 1'b0;
      end
      if (rvalid && rready) begin
        $fdisplay(fd, "%08x", rdata);
        if (left == 32'd1) begin
          $fclose(fd);
          rready <= 1'b0;
          busy   <= 1'b0;
        end else begin
          left    <= left - 32'd1;
          addr    <= addr + AW'(4);
          araddr  <= addr + AW'(4);
          arvalid <= 1'b1;
          in_ar   <= 1'b1;
        end
      end
    end
  end

endmodule
