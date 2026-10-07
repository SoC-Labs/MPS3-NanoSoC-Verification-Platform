// bind_dfx_ctl.sv — attach the reusable AXI4-Lite protocol checker onto the
// dfx_ctl slave. A5 verification infra (tests/common/sva/). Compiled by
// tests/dfx_ctl/Makefile (VERILOG_SOURCES += this + axi4lite_protocol_checker.sv);
// no RTL edit. See axi4lite_protocol_checker.sv for what is checked.
`timescale 1ns / 1ps
bind dfx_ctl axi4lite_protocol_checker #(
  .ADDR_WIDTH(12), .DATA_WIDTH(32), .NAME("dfx_ctl")
) u_axi4lite_sva (
  .aclk(s_axi_aclk), .aresetn(s_axi_aresetn),
  .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
  .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
  .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
  .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
  .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready)
);
