// =============================================================================
// tests/gpio_shield/tb_gpio_shield.sv
//
// Output-only GPIO passthrough check: does a DUT-driven dut_gpio_o[n] reach a
// physical shield pad (SH0_IO[n]) through the REAL board_gpio block plus the
// per-bit IOBUF proposed in docs/CONNECTOR_SURVEY.md §6?
//
// Validates the exact end-to-end path the "route dut_gpio to the shield/Pmod"
// change would deploy:
//
//   [RP] dut_gpio_o/oe  ->  board_gpio OWN mux (own=0 => DUT owns)
//        -> board_pad_o/oe -> IOBUF (I/T/O/IO) -> SH0_IO pad
//        pad -> board_pad_i -> 2-FF sync -> dut_gpio_i  (read-back loop)
//
// board_gpio is the real synthesizable RTL (fpga/shell/ip/board_gpio). The
// IOBUF is modelled behaviourally (I=board_pad_o, T=~board_pad_oe, O=board_pad_i,
// IO=SH0_IO) -- identical function to the Xilinx IOBUF primitive in §6's sketch;
// a weak external pulldown on each pad lets a hi-Z (OE=0) pad be observed.
//
// Focus = OUTPUT-ONLY (the bench decision, 2026-07-10): the DUT drives the pad,
// and when OE=0 the pad must go hi-Z so it never fights an external driver.
// Pure-VCS SV, standalone like tests/lan8720_sut. Not wired into `make check`.
// =============================================================================
`timescale 1ns / 1ps

module tb_gpio_shield;

  localparam int NGPIO = 16;

  logic clk = 1'b0;  always #5 clk = ~clk;    // 100 MHz s_axi_aclk
  logic rstn = 1'b0;

  // ---- RP (DUT) side drives ----
  logic [NGPIO-1:0] dut_gpio_o  = '0;
  logic [NGPIO-1:0] dut_gpio_oe = '0;
  wire  [NGPIO-1:0] dut_gpio_i;

  // ---- board-pad triplet (board_gpio <-> IOBUF) ----
  wire  [NGPIO-1:0] board_pad_o, board_pad_oe;
  wire  [NGPIO-1:0] board_pad_i;

  // ---- REAL board_gpio; AXI held idle so own_q stays 0 after reset => DUT owns
  board_gpio #(.C_S_AXI_ADDR_WIDTH(12), .C_S_AXI_DATA_WIDTH(32), .NGPIO(NGPIO)) u_gpio (
    .s_axi_aclk(clk), .s_axi_aresetn(rstn),
    .s_axi_awaddr('0), .s_axi_awprot('0), .s_axi_awvalid(1'b0), .s_axi_awready(),
    .s_axi_wdata('0), .s_axi_wstrb('0), .s_axi_wvalid(1'b0), .s_axi_wready(),
    .s_axi_bresp(), .s_axi_bvalid(), .s_axi_bready(1'b1),
    .s_axi_araddr('0), .s_axi_arprot('0), .s_axi_arvalid(1'b0), .s_axi_arready(),
    .s_axi_rdata(), .s_axi_rresp(), .s_axi_rvalid(), .s_axi_rready(1'b1),
    .dut_gpio_o_i(dut_gpio_o), .dut_gpio_oe_i(dut_gpio_oe), .dut_gpio_i_o(dut_gpio_i),
    .board_pad_o(board_pad_o), .board_pad_oe(board_pad_oe), .board_pad_i(board_pad_i)
  );

  // ---- §6 per-bit IOBUF (behavioural) + weak external pulldown on the pad ----
  wire [NGPIO-1:0] SH0_IO;
  genvar g;
  generate
    for (g = 0; g < NGPIO; g++) begin : g_iobuf
      assign SH0_IO[g]      = board_pad_oe[g] ? board_pad_o[g] : 1'bz;  // IOBUF I/T/IO
      assign board_pad_i[g] = SH0_IO[g];                                // IOBUF O
      pulldown (SH0_IO[g]);                                             // external, models unconnected/pulled pad
    end
  endgenerate

  int errors = 0;

  // drive dut_gpio_o output-only (OE=1, DUT owns), expect the pad to follow
  task automatic drive_check(input [NGPIO-1:0] val);
    dut_gpio_o  = val;
    dut_gpio_oe = '1;                 // output-only: every bit an output
    #1;                              // combinational OWN mux + IOBUF settle
    if (board_pad_oe !== '1) begin errors++;
      $display("GPIO_SH: FAIL board_pad_oe=%h (own=0 should pass DUT OE=1)", board_pad_oe); end
    if (SH0_IO !== val) begin errors++;
      $display("GPIO_SH: FAIL drive: SH0_IO=%h expected %h", SH0_IO, val); end
    else $display("GPIO_SH:   ok  drive dut_gpio_o=%h -> SH0_IO=%h", val, SH0_IO);
    // read-back loop: pad -> board_pad_i -> 2-FF sync -> dut_gpio_i
    repeat (3) @(posedge clk);
    if (dut_gpio_i !== val) begin errors++;
      $display("GPIO_SH: FAIL read-back: dut_gpio_i=%h expected %h", dut_gpio_i, val); end
  endtask

  initial begin
    rstn = 1'b0; repeat (4) @(posedge clk);
    rstn = 1'b1; repeat (4) @(posedge clk);

    $display("GPIO_SH: === output-only passthrough: dut_gpio_o -> SH0_IO pad ===");
    // walking-1 across all 16 bits
    for (int b = 0; b < NGPIO; b++) drive_check(16'h1 << b);
    // corners
    drive_check(16'hFFFF);
    drive_check(16'h0000);
    drive_check(16'hA5A5);
    drive_check(16'h5A5A);

    // ---- tristate: OE=0 must take the pad hi-Z (output-only safety) ----------
    // Even with the DUT still driving a '1 value, OE=0 must release the pad so
    // it never fights an external driver on the shield header.
    $display("GPIO_SH: === tristate: OE=0 releases the pad (no external fight) ===");
    dut_gpio_o = '1; dut_gpio_oe = '0; #1;
    if (board_pad_oe !== '0) begin errors++;
      $display("GPIO_SH: FAIL board_pad_oe=%h with DUT OE=0", board_pad_oe); end
    if (SH0_IO !== '0) begin errors++;   // hi-Z pad -> external pulldown -> 0
      $display("GPIO_SH: FAIL tristate: SH0_IO=%h, expected pulled-0 (pad not released)", SH0_IO); end
    else $display("GPIO_SH:   ok  OE=0 -> SH0_IO hi-Z -> reads external pulldown (0)");

    // ---- per-bit independence: half out=1, half released ---------------------
    dut_gpio_o = 16'hFFFF; dut_gpio_oe = 16'h00FF; #1;
    if (SH0_IO !== 16'h00FF) begin errors++;
      $display("GPIO_SH: FAIL mixed OE: SH0_IO=%h expected 0x00FF", SH0_IO); end
    else $display("GPIO_SH:   ok  mixed OE 0x00FF -> SH0_IO=%h (driven bits high, released bits pulled 0)", SH0_IO);

    $display("");
    $display("GPIO_SH: ============================================================");
    $display("GPIO_SH: output-only dut_gpio -> shield pad (board_gpio + §6 IOBUF):");
    $display("GPIO_SH:   errors=%0d  VERDICT=%s", errors, (errors==0)?"PASS":"FAIL");
    $display("GPIO_SH: ============================================================");
    $finish;
  end

  initial begin
    #500_000;
    $display("GPIO_SH: TIMEOUT  VERDICT=FAIL");
    $finish;
  end

endmodule
