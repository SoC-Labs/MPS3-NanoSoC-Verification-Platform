// uart_echo_harness.sv — co-simulate the DUT and the harness UART path.
//
// WHY THIS EXISTS
//   Two benches already cover the halves in isolation: tests/rm_uart_echo drives
//   the RM's AXI-Stream console pair directly, and tests/uart_bridge drives the
//   bridge's stream ports with a BFM. NOTHING exercises the join -- the actual
//   DUT<->harness contract:
//
//       rm_uart_echo (dut_clk) --AXIS--> uart_bridge async FIFO --> CSR (s_axi_aclk)
//
//   That join is a real CDC. The RM runs on dut_clk; the CSR side runs on
//   s_axi_aclk; the bridge's gray-pointer async FIFO is the only thing between
//   them. A byte the RM emits has to survive it, in order, with backpressure
//   applied from the far side by a host that reads the CSR at its own pace.
//
//   This harness wires the two together exactly as shell_bd.tcl does, so the
//   cocotb bench can act as the MicroBlaze: poll FIFO_STATUS, pop U0_DATA, push
//   bytes back, and watch them echo. It is the simulation twin of the
//   over-the-wire demo (swap in rm_uart_echo, read its banner on TCP 6930).
//
//   Deliberately unrelated clock periods (10 ns AXI / 7 ns DUT) so every byte
//   crosses a genuine, non-harmonic boundary.

`timescale 1ns / 1ps

module uart_echo_harness #(
    parameter int C_S_AXI_ADDR_WIDTH = 32,   // as shell_bd.tcl instantiates it
    parameter int C_S_AXI_DATA_WIDTH = 32
) (
    input  logic                            s_axi_aclk,
    input  logic                            s_axi_aresetn,

    input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_awaddr,
    input  logic [2:0]                      s_axi_awprot,
    input  logic                            s_axi_awvalid,
    output logic                            s_axi_awready,
    input  logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_wdata,
    input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb,
    input  logic                            s_axi_wvalid,
    output logic                            s_axi_wready,
    output logic [1:0]                      s_axi_bresp,
    output logic                            s_axi_bvalid,
    input  logic                            s_axi_bready,
    input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_araddr,
    input  logic [2:0]                      s_axi_arprot,
    input  logic                            s_axi_arvalid,
    output logic                            s_axi_arready,
    output logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_rdata,
    output logic [1:0]                      s_axi_rresp,
    output logic                            s_axi_rvalid,
    input  logic                            s_axi_rready,

    input  logic                            dut_clk,
    input  logic                            dut_resetn,

    // Observability for the bench.
    output logic [31:0]                     rm_id
);

  // ---- the console AXI-Stream pair, RM <-> bridge -------------------------
  logic [7:0] rp_uart_tx_tdata;
  logic       rp_uart_tx_tvalid;
  logic       rp_uart_tx_tready;

  logic [7:0] rp_uart_rx_tdata;
  logic       rp_uart_rx_tvalid;
  logic       rp_uart_rx_tready;

  // ---- the DUT (reconfigurable module) ------------------------------------
  // Every non-console RP port is tied to its safe idle, exactly as the RM's own
  // bench does, so this harness cannot accidentally depend on them.
  rm_uart_echo u_rm (
      .dut_clk          (dut_clk),
      .dut_resetn       (dut_resetn),
      .rp_resetn        (dut_resetn),
      .dbg_resetn       (dut_resetn),

      .swd_clk          (1'b0),
      .swd_dio_o        (1'b0),
      .swd_dio_oe       (1'b0),
      .swd_dio_i        (),

      .phy_rmii_ref_clk (1'b0),
      .phy_rmii_crs_dv  (1'b0),
      .phy_rmii_rxd     (2'b00),
      .phy_rmii_txd     (),
      .phy_rmii_tx_en   (),
      .mdc              (),
      .mdio_o           (),
      .mdio_oe          (),
      .mdio_i           (1'b0),

      .uart_tx_tdata    (rp_uart_tx_tdata),
      .uart_tx_tvalid   (rp_uart_tx_tvalid),
      .uart_tx_tready   (rp_uart_tx_tready),
      .uart_rx_tdata    (rp_uart_rx_tdata),
      .uart_rx_tvalid   (rp_uart_rx_tvalid),
      .uart_rx_tready   (rp_uart_rx_tready),
      .swo              (),

      .rm_id            (rm_id),
      .dut_lockup       (),
      .irq_out          (),
      .dut_gpio_o       (),
      .dut_gpio_oe      (),
      .dut_gpio_i       (16'h0000)
  );

  // ---- the harness UART bridge --------------------------------------------
  // Note the direction flip, and that it is the whole point: the RM's uart_tx
  // (an output) is the bridge's uart_tx_tdata_i (an input). U0 is the console.
  uart_bridge #(
      .C_S_AXI_ADDR_WIDTH (C_S_AXI_ADDR_WIDTH),
      .C_S_AXI_DATA_WIDTH (C_S_AXI_DATA_WIDTH)
  ) u_bridge (
      .s_axi_aclk      (s_axi_aclk),
      .s_axi_aresetn   (s_axi_aresetn),
      .s_axi_awaddr    (s_axi_awaddr),
      .s_axi_awprot    (s_axi_awprot),
      .s_axi_awvalid   (s_axi_awvalid),
      .s_axi_awready   (s_axi_awready),
      .s_axi_wdata     (s_axi_wdata),
      .s_axi_wstrb     (s_axi_wstrb),
      .s_axi_wvalid    (s_axi_wvalid),
      .s_axi_wready    (s_axi_wready),
      .s_axi_bresp     (s_axi_bresp),
      .s_axi_bvalid    (s_axi_bvalid),
      .s_axi_bready    (s_axi_bready),
      .s_axi_araddr    (s_axi_araddr),
      .s_axi_arprot    (s_axi_arprot),
      .s_axi_arvalid   (s_axi_arvalid),
      .s_axi_arready   (s_axi_arready),
      .s_axi_rdata     (s_axi_rdata),
      .s_axi_rresp     (s_axi_rresp),
      .s_axi_rvalid    (s_axi_rvalid),
      .s_axi_rready    (s_axi_rready),

      .dut_clk_i       (dut_clk),

      // RM -> bridge (console RX, what the host reads)
      .uart_tx_tdata_i (rp_uart_tx_tdata),
      .uart_tx_tvalid_i(rp_uart_tx_tvalid),
      .uart_tx_tready_o(rp_uart_tx_tready),

      // bridge -> RM (console TX, what the host writes)
      .uart_rx_tdata_o (rp_uart_rx_tdata),
      .uart_rx_tvalid_o(rp_uart_rx_tvalid),
      .uart_rx_tready_i(rp_uart_rx_tready),

      .swo_i           (1'b1),   // 8N1 idle-high; unused here

      .uart1_tx_tdata_i (8'h00),
      .uart1_tx_tvalid_i(1'b0),
      .uart1_tx_tready_o(),
      .uart1_rx_tdata_o (),
      .uart1_rx_tvalid_o(),
      .uart1_rx_tready_i(1'b0)
  );

endmodule
