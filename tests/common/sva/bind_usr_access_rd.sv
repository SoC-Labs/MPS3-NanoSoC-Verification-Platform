// bind_usr_access_rd.sv — attach the reusable AXI4-Lite protocol checker onto
// the usr_access_rd slave (USRACC, the fabric build-identity readback).
// A5 verification infra (tests/common/sva/). Compiled by
// tests/usr_access_rd/Makefile (VERILOG_SOURCES += this +
// axi4lite_protocol_checker.sv); no RTL edit. See the checker for what is
// checked.
//
// ADDR_WIDTH(32), NOT 12 like bind_dfx_ctl.sv: this bench elaborates the block
// at the width shell_bd.tcl ships (-pvalue+usr_access_rd.C_S_AXI_ADDR_WIDTH=32),
// so the checker's address ports must match or elaboration truncates them.
`timescale 1ns / 1ps
bind usr_access_rd axi4lite_protocol_checker #(
  .ADDR_WIDTH(32), .DATA_WIDTH(32), .NAME("usr_access_rd")
) u_axi4lite_sva (
  .aclk(s_axi_aclk), .aresetn(s_axi_aresetn),
  .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
  .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
  .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
  .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
  .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready)
);
