// bind_usd_spi.sv — attach the reusable AXI4-Lite protocol checker onto the
// usd_spi slave (USD @ 0x44A4_0000). A5 verification infra (tests/common/sva/).
// Compiled by tests/usd_spi/Makefile (VERILOG_SOURCES += this +
// axi4lite_protocol_checker.sv); no RTL edit. See axi4lite_protocol_checker.sv
// for what is checked.
//
// ADDR_WIDTH follows the bound instance's own C_S_AXI_ADDR_WIDTH (a bind
// resolves its parameter expressions in the target module's scope). The other
// bind_*.sv files hard-code 12; this bench elaborates at 32 -- the width
// shell_bd.tcl instantiates -- as well as at 12, and a fixed 12 would silently
// truncate the address the checker sees in the shipped configuration.
`timescale 1ns / 1ps
bind usd_spi axi4lite_protocol_checker #(
  .ADDR_WIDTH(C_S_AXI_ADDR_WIDTH), .DATA_WIDTH(32), .NAME("usd_spi")
) u_axi4lite_sva (
  .aclk(s_axi_aclk), .aresetn(s_axi_aresetn),
  .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
  .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
  .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
  .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
  .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready)
);
