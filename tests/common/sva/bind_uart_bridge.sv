// bind_uart_bridge.sv — attach the AXI4-Lite protocol checker onto the
// uart_bridge slave AND the CDC gray-pointer checker onto every one of its five
// uartbr_async_fifo instances. A5 verification infra (tests/common/sva/).
// Compiled by tests/uart_bridge/Makefile; no RTL edit.
`timescale 1ns / 1ps

bind uart_bridge axi4lite_protocol_checker #(
  .ADDR_WIDTH(12), .DATA_WIDTH(32), .NAME("uart_bridge")
) u_axi4lite_sva (
  .aclk(s_axi_aclk), .aresetn(s_axi_aresetn),
  .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
  .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
  .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
  .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
  .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready)
);

// One bind targeting the FIFO module attaches into ALL FIVE instances
// (U0/U1 tx+rx, SWO rx). PTR_WIDTH = $clog2(DEPTH)+1 = 5 for the default
// DEPTH=16.
bind uartbr_async_fifo cdc_gray_checker #(
  .PTR_WIDTH(5), .NAME("uartbr_fifo")
) u_cdc_sva (
  .wclk_i(wclk_i), .wrst_n_i(wrst_n_i), .wr_en_i(wr_en_i), .full_o(full_o),
  .wptr_gray_q(wptr_gray_q), .wptr_bin_q(wptr_bin_q),
  .rclk_i(rclk_i), .rrst_n_i(rrst_n_i), .rd_en_i(rd_en_i), .empty_o(empty_o),
  .rptr_gray_q(rptr_gray_q), .rptr_bin_q(rptr_bin_q)
);
