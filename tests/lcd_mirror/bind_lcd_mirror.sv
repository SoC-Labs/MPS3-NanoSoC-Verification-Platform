// bind_lcd_mirror.sv -- attach the reusable AXI4-Lite protocol checker
// (tests/common/sva/axi4lite_protocol_checker.sv, pulled in by the Makefile's
// SVA_MODULES) onto the LCDMIR slave. ADDR_WIDTH(32) = the width the bench
// elaborates the DUT at (the width shell_bd.tcl ships). No RTL edit.
`timescale 1ns / 1ps
bind lcd_mirror axi4lite_protocol_checker #(
  .ADDR_WIDTH(32), .DATA_WIDTH(32), .NAME("lcd_mirror")
) u_axi4lite_sva (
  .aclk(s_axi_aclk), .aresetn(s_axi_aresetn),
  .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
  .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
  .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
  .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
  .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready)
);
