// bind_clcd.sv — attach the reusable AXI4-Lite protocol checker onto the clcd
// slave. A5 verification infra; the checker module itself lives in
// tests/common/sva/axi4lite_protocol_checker.sv (pulled in via the clcd
// Makefile's SVA_MODULES). This bind file is the block's own, under tests/clcd/,
// and is added to VERILOG_SOURCES directly. No RTL edit. See the checker for
// what is checked (response OKAY-only, no-X-while-valid, one-response-per-
// request, ...). ADDR_WIDTH(12) matches the default elaboration of the clcd
// bench (the width-32 shipped decode is covered by tests/csr_decode_width/).
`timescale 1ns / 1ps
bind clcd axi4lite_protocol_checker #(
  .ADDR_WIDTH(12), .DATA_WIDTH(32), .NAME("clcd")
) u_axi4lite_sva (
  .aclk(s_axi_aclk), .aresetn(s_axi_aresetn),
  .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
  .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
  .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
  .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
  .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready)
);
