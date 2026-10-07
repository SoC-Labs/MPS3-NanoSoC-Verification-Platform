// -----------------------------------------------------------------------------
// uart_bridge.sv — UARTBR regmap block (shell-regmap.md v0.1 @ 0x44A9_0000, I7)
//
// REAL, synthesizable AXI4-Lite slave. New file (the UARTBR block had no
// owned RTL directory until now — see fpga/shell/README.md's block
// inventory and docs/NEXT_WAVE_PLAN.md W-RTL-NEWIP).
//
// Responsibility (shell-regmap.md UARTBR table; partition-pins.md "Console
// / trace" group): AXIS <-> register-FIFO bridge for the three console
// streams the `uart_over_eth` firmware relays to TCP 6930/6931/6932:
//   - UART0 (boot monitor): the DUT's cmsdk_apb_usrt AXIS pair
//     (`uart_tx_*` DUT->host, `uart_rx_*` host->DUT partition pins);
//   - UART1 (application): register surface + FIFOs fully implemented, but
//     single-core nanosoc (I1) exposes no second UART across the RP
//     boundary in partition-pins.md v0.1 — the `uart1_*` ports below are a
//     RESERVED seam the BD ties off until the multicore RM widens the
//     contract (regmap: "present but unused until multicore");
//   - SWO/ITM trace: the raw 1-bit `swo` partition pin, deserialised by
//     swo_uart_rx.sv (UART/NRZ mode only, programmable divisor — see that
//     file's header for the Manchester-mode disposition).
//
// CDC: THE PARTITION-BOUNDARY CROSSING FOR ALL THREE CONSOLE STREAMS LIVES
// HERE, per partition-pins.md's clock/reset-domain rule ("shell owns the
// synchronizers/async FIFOs"). Every stream direction runs through its own
// gray-pointer dual-clock FIFO (uartbr_async_fifo.sv, 5 instances) between
// dut_clk (partition-pin side) and s_axi_aclk (MicroBlaze side). The RM
// sees only already-safe, dut_clk-domain AXIS handshakes.
//
// Single-file-build note: like mdio_phy_model.sv, this top pulls its two
// helper modules in with `include (resolved relative to this file's own
// directory per the Verilog LRM) so a bench Makefile can list just this
// file as VERILOG_SOURCES — uartbr_async_fifo.sv / swo_uart_rx.sv remain
// genuinely separate, independently lintable/instantiable modules; only
// the *build* stays single-file. See this block's README.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

`include "uartbr_async_fifo.sv"
`include "swo_uart_rx.sv"

module uart_bridge #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; base
                                           // address (0x44A9_0000, 64 KB page)
                                           // is set in the BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32,
  parameter int FIFO_DEPTH         = 16   // per-stream async-FIFO depth
                                           // (power of two, >= 4)
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, UARTBR regmap (shell-regmap.md
  // v0.1: 0x00 U0_TX/U0_RX / 0x08 U1_TX/U1_RX / 0x10 SWO_RX(ro) /
  // 0x14 FIFO_STATUS(ro); 0x18 SWO_CFG added at a spare offset — see the
  // ambiguity list at the bottom).
  // ---------------------------------------------------------------------
  input  logic                          s_axi_aclk,
  input  logic                          s_axi_aresetn,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,
  input  logic [2:0]                    s_axi_awprot,
  input  logic                          s_axi_awvalid,
  output logic                          s_axi_awready,

  input  logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_wdata,
  input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb,
  input  logic                          s_axi_wvalid,
  output logic                          s_axi_wready,

  output logic [1:0]                    s_axi_bresp,
  output logic                          s_axi_bvalid,
  input  logic                          s_axi_bready,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,
  input  logic [2:0]                    s_axi_arprot,
  input  logic                          s_axi_arvalid,
  output logic                          s_axi_arready,

  output logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_rdata,
  output logic [1:0]                    s_axi_rresp,
  output logic                          s_axi_rvalid,
  input  logic                          s_axi_rready,

  // ---------------------------------------------------------------------
  // DUT clock domain tap — destination/source domain for every partition-
  // pin-facing FIFO side below (partition-pins.md: the console AXIS group
  // is DUT-side logic, i.e. dut_clk).
  // ---------------------------------------------------------------------
  input  logic                          dut_clk_i,

  // ---------------------------------------------------------------------
  // UART0 partition pins (partition-pins.md "Console / trace" group,
  // shell's view; pin name + direction suffix per house convention —
  // cf. mdio_phy_model's mdio_i_o / board_gpio's dut_gpio_o_i):
  //   uart_tx_* : DUT -> host console bytes (AXIS in,  dut_clk domain)
  //   uart_rx_* : host -> DUT console bytes (AXIS out, dut_clk domain)
  // ---------------------------------------------------------------------
  input  logic [7:0]                    uart_tx_tdata_i,
  input  logic                          uart_tx_tvalid_i,
  output logic                          uart_tx_tready_o,

  output logic [7:0]                    uart_rx_tdata_o,
  output logic                          uart_rx_tvalid_o,
  input  logic                          uart_rx_tready_i,

  // ---------------------------------------------------------------------
  // SWO partition pin — raw 1-bit trace line (async serial; deserialised
  // by swo_uart_rx.sv in the dut_clk domain).
  // ---------------------------------------------------------------------
  input  logic                          swo_i,

  // ---------------------------------------------------------------------
  // UART1 — RESERVED seam (NOT partition pins in partition-pins.md v0.1;
  // the single-core I1 boundary has one console UART). Fully implemented
  // and symmetric with UART0 so the multicore RM is a re-plumb, not a
  // redesign; until then the BD ties uart1_tx_tvalid_i = 1'b0 and
  // uart1_rx_tready_i = 1'b0 (see ambiguity list / README).
  // ---------------------------------------------------------------------
  input  logic [7:0]                    uart1_tx_tdata_i,
  input  logic                          uart1_tx_tvalid_i,
  output logic                          uart1_tx_tready_o,

  output logic [7:0]                    uart1_rx_tdata_o,
  output logic                          uart1_rx_tvalid_o,
  input  logic                          uart1_rx_tready_i
);

  // ===========================================================================
  // AXI4-Lite slave — standard Xilinx-template write/read channel FSM
  // (identical structure to dfx_ctl.sv / dut_clkrst.sv / board_gpio.sv for
  // consistency across the shell's custom IP).
  // ===========================================================================

  localparam int ADDR_LSB = 2;

  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_awaddr_q;
  logic                          axi_awready_q, axi_wready_q, aw_en_q;
  logic                          axi_bvalid_q;
  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_araddr_q;
  logic                          axi_arready_q, axi_rvalid_q;
  logic [C_S_AXI_DATA_WIDTH-1:0] axi_rdata_q;

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00;   // OKAY always -- contract defines no error cases
  assign s_axi_bvalid  = axi_bvalid_q;
  assign s_axi_arready = axi_arready_q;
  assign s_axi_rresp   = 2'b00;   // OKAY
  assign s_axi_rvalid  = axi_rvalid_q;
  assign s_axi_rdata   = axi_rdata_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awready_q <= 1'b0;
      aw_en_q       <= 1'b1;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awready_q <= 1'b1;
      aw_en_q       <= 1'b0;
    end else if (s_axi_bready && axi_bvalid_q) begin
      aw_en_q       <= 1'b1;
      axi_awready_q <= 1'b0;
    end else begin
      axi_awready_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awaddr_q <= '0;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awaddr_q <= s_axi_awaddr;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_wready_q <= 1'b0;
    end else if (~axi_wready_q && s_axi_wvalid && s_axi_awvalid && aw_en_q) begin
      axi_wready_q <= 1'b1;
    end else begin
      axi_wready_q <= 1'b0;
    end
  end

  wire slv_reg_wren = axi_wready_q && s_axi_wvalid && axi_awready_q && s_axi_awvalid;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_bvalid_q <= 1'b0;
    end else if (slv_reg_wren && ~axi_bvalid_q) begin
      axi_bvalid_q <= 1'b1;
    end else if (s_axi_bready && axi_bvalid_q) begin
      axi_bvalid_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_arready_q <= 1'b0;
      axi_araddr_q  <= '0;
    end else if (~axi_arready_q && s_axi_arvalid) begin
      axi_arready_q <= 1'b1;
      axi_araddr_q  <= s_axi_araddr;
    end else begin
      axi_arready_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rvalid_q <= 1'b0;
    end else if (axi_arready_q && s_axi_arvalid && ~axi_rvalid_q) begin
      axi_rvalid_q <= 1'b1;
    end else if (axi_rvalid_q && s_axi_rready) begin
      axi_rvalid_q <= 1'b0;
    end
  end

  wire slv_reg_rden = axi_arready_q && s_axi_arvalid && ~axi_rvalid_q;

  // ===========================================================================
  // Register-index decode (offsets per shell-regmap.md v0.1 UARTBR table;
  // 0x04/0x0C are reserved gaps in the contract and read back 0).
  //
  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]): ONLY
  // the mapped offsets respond; every other offset in the page is unmapped --
  // reads return 0 (and, CRITICALLY, pop NOTHING), writes are ignored
  // (BRESP=OKAY). This matches the SystemRDL-generated decode, which compares
  // the whole address.
  //
  // *** DATA-LOSS HAZARD REMOVED (2026-07-09) ***
  // Previously only addr[4:2] was decoded, so raddr_idx aliased offsets >= 0x20
  // onto the mapped windows (0x20 -> U0_DATA, ...). Because U0_DATA/U1_DATA/
  // SWO_RX are DESTRUCTIVE-READ FIFO ports, an aliased READ of an "unmapped"
  // offset (e.g. 0x20) did not merely return a wrong value -- it POPPED the U0
  // RX FIFO via u0_rx_pop, silently losing a byte of DUT console/trace data on
  // stray traffic. Widening raddr_idx to the full local word address makes the
  // (raddr_idx == IDX_U0_DATA) equality FALSE for every unmapped offset, so no
  // FIFO rd_en can assert outside a genuinely-mapped data-window read.
  // (See RESOLVED ambiguity #7 below.)
  // LOCAL page decode -- NOT C_S_AXI_ADDR_WIDTH.
  //
  // shell_bd.tcl instantiates every CSR block with C_S_AXI_ADDR_WIDTH = 32 (the
  // RTL default of 12 is never what ships), so the interconnect hands this slave
  // the FULL system address -- 0x44A9_0000, not 0x0. Decoding
  // addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] therefore compares 0x1128_4000 against
  // 'h0 and never matches: on silicon every write was ignored and every read
  // returned 0, in all six blocks at once. (Xilinx's HWICAP, on the same bus,
  // worked -- which is what isolated it.)
  //
  // Decode the block's own 64 KiB page instead. 64 KiB because that is the
  // spacing assign_bd_address gives these slaves; decoding fewer bits would let
  // BASE+0x1000 alias offset 0 and re-create the destructive alias that widening
  // this decode was meant to kill (a read of 0x20 used to POP a console byte).
  //
  // Guarded against a narrower instantiation so the block still elaborates at
  // its 12-bit default. tests/csr_decode_width/ elaborates at 32 and drives
  // base+offset, and fails on the un-fixed decode.
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_U0_DATA     = 'h0;  // 0x00 W: U0_TX push / R: U0_RX pop
  localparam logic [IDX_W-1:0] IDX_U1_DATA     = 'h2;  // 0x08 W: U1_TX push / R: U1_RX pop
  localparam logic [IDX_W-1:0] IDX_SWO_RX      = 'h4;  // 0x10 R: SWO_RX pop (ro)
  localparam logic [IDX_W-1:0] IDX_FIFO_STATUS = 'h5;  // 0x14 (ro)
  localparam logic [IDX_W-1:0] IDX_SWO_CFG     = 'h6;  // 0x18 (added, see ambiguities)

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  // ===========================================================================
  // DUT-domain reset: s_axi_aresetn asserted asynchronously into dut_clk,
  // deasserted synchronized (Cummings pattern, same as dut_clkrst.sv's
  // generators). The bridge's lifetime is tied to the SHELL's reset, not to
  // dut_resetn/rp_resetn — console bytes queued for a DUT that is being
  // reset/swapped stay queued (the decouple/drain policy across a swap is
  // firmware's, not this module's — see README).
  // R4: reset synchronizer (async-assert s_axi_aresetn -> sync-deassert into
  // dut_clk); the deassert edge crosses domains -> (* ASYNC_REG *).
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [2:0] dut_rstn_sync_q;

  always_ff @(posedge dut_clk_i or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      dut_rstn_sync_q <= 3'b000;
    end else begin
      dut_rstn_sync_q <= {dut_rstn_sync_q[1:0], 1'b1};
    end
  end

  wire dut_rst_n = dut_rstn_sync_q[2];

  // ===========================================================================
  // SWO_CFG register (s_axi_aclk domain): [16] enable, [15:0] divisor
  // (bit period = divisor+1 dut_clk cycles — see swo_uart_rx.sv's timing
  // contract; >= 7 recommended). Read-back additionally reports two sticky
  // dut-domain status bits, [31] frame_err and [30] overflow (both clear
  // while enable=0) — added-value RO bits, flagged for A6 below.
  // ===========================================================================
  logic        swo_en_q;
  logic [15:0] swo_div_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      swo_en_q  <= 1'b0;
      swo_div_q <= 16'h0000;
    end else if (slv_reg_wren && (waddr_idx == IDX_SWO_CFG)) begin
      if (s_axi_wstrb[0]) swo_div_q[7:0]  <= s_axi_wdata[7:0];
      if (s_axi_wstrb[1]) swo_div_q[15:8] <= s_axi_wdata[15:8];
      if (s_axi_wstrb[2]) swo_en_q        <= s_axi_wdata[16];
    end
  end

  // --- SWO_CFG -> dut_clk CDC: the enable level goes through a 2-FF
  // synchronizer; the multi-bit divisor is CAPTURED in the dut domain on
  // the synchronized enable's rising edge (data-with-qualifier pattern: by
  // the time the enable edge emerges from the synchronizer, the divisor
  // bits — written by the same or an earlier AXI write — have been stable
  // for >= 2 dut_clk cycles, so the capture can never tear). Consequence,
  // documented in the README: changing the divisor requires toggling
  // enable off/on.
  // R4: swo_en level crosses s_axi_aclk -> dut_clk. (* ASYNC_REG *) on the
  // 2-FF sync. The MULTI-BIT swo_div is handled correctly WITHOUT bit-wise
  // synchronizing it: it is captured in the dut domain only on the
  // synchronized enable's rising edge (data-with-qualifier), by which point
  // the divisor bits have been stable for >= 2 dut_clk cycles -> the capture
  // can never tear. swo_en_dut_q/swo_div_dut_q are dut-domain working regs,
  // not sync flops, so only swo_en_sync_q carries the attribute.
  (* ASYNC_REG = "TRUE" *) logic [1:0]  swo_en_sync_q;
  logic        swo_en_dut_q;
  logic [15:0] swo_div_dut_q;

  always_ff @(posedge dut_clk_i) begin
    if (!dut_rst_n) begin
      swo_en_sync_q <= 2'b00;
      swo_en_dut_q  <= 1'b0;
      swo_div_dut_q <= 16'h0000;
    end else begin
      swo_en_sync_q <= {swo_en_sync_q[0], swo_en_q};
      if (swo_en_sync_q[1] && !swo_en_dut_q) begin
        swo_div_dut_q <= swo_div_q;   // quasi-static by construction (see above)
      end
      swo_en_dut_q <= swo_en_sync_q[1];
    end
  end

  // ===========================================================================
  // SWO deserialiser (dut_clk domain) + sticky status. Frame errors discard
  // the byte; a byte arriving while the SWO FIFO is full is dropped
  // (drop-newest). Both conditions latch sticky dut-domain bits, cleared
  // whenever capture is disabled, and are 2-FF synchronized back into
  // s_axi_aclk for SWO_CFG read-back (slow status levels, not data — plain
  // 2-FF is sufficient).
  // ===========================================================================
  logic [7:0] swo_byte;
  logic       swo_byte_valid;
  logic       swo_frame_err;

  swo_uart_rx #(
    .DIV_WIDTH (16)
  ) u_swo_rx (
    .clk_i        (dut_clk_i),
    .rst_n_i      (dut_rst_n),
    .enable_i     (swo_en_dut_q),
    .divisor_i    (swo_div_dut_q),
    .swo_i        (swo_i),
    .byte_o       (swo_byte),
    .byte_valid_o (swo_byte_valid),
    .frame_err_o  (swo_frame_err)
  );

  logic swo_fifo_full;   // write-domain (dut_clk) flag of the SWO FIFO

  logic swo_ferr_sticky_q, swo_ovfl_sticky_q;

  always_ff @(posedge dut_clk_i) begin
    if (!dut_rst_n) begin
      swo_ferr_sticky_q <= 1'b0;
      swo_ovfl_sticky_q <= 1'b0;
    end else if (!swo_en_dut_q) begin
      swo_ferr_sticky_q <= 1'b0;
      swo_ovfl_sticky_q <= 1'b0;
    end else begin
      if (swo_frame_err)                    swo_ferr_sticky_q <= 1'b1;
      if (swo_byte_valid && swo_fifo_full)  swo_ovfl_sticky_q <= 1'b1;
    end
  end

  // R4: two sticky dut-domain status bits crossing back into s_axi_aclk.
  // Single-bit levels, plain 2-FF, (* ASYNC_REG *).
  (* ASYNC_REG = "TRUE" *) logic [1:0] swo_ferr_sync_q, swo_ovfl_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      swo_ferr_sync_q <= 2'b00;
      swo_ovfl_sync_q <= 2'b00;
    end else begin
      swo_ferr_sync_q <= {swo_ferr_sync_q[0], swo_ferr_sticky_q};
      swo_ovfl_sync_q <= {swo_ovfl_sync_q[0], swo_ovfl_sticky_q};
    end
  end

  // ===========================================================================
  // Register-window strobes.
  //
  // Writes: a write to U0_TX/U1_TX pushes wdata[7:0] into the respective
  // host->DUT FIFO. Bit [8] of the write data is IGNORED (the regmap's
  // "[8] valid" describes the READ side; see ambiguity list). A push while
  // tx_full is silently dropped — firmware's contract is to poll
  // FIFO_STATUS.tx_full first (matching the "[8] valid" poll on the read
  // side).
  //
  // Reads: a read of U0_RX/U1_RX/SWO_RX pops one byte if available (the
  // AXI-Lite FSM guarantees exactly one slv_reg_rden strobe per read
  // transaction), returning {valid=1, byte}; on empty it returns
  // {valid=0, 8'h00} and pops nothing. Reads are therefore destructive —
  // documented for A5's bench (no speculative/debugger reads of the data
  // windows).
  // ===========================================================================
  // Every FIFO pop/push is gated on the DECODED, MAPPED register select
  // (full-width raddr_idx/waddr_idx == IDX_*), never on bare slv_reg_rden/
  // slv_reg_wren. With the full-address decode above, an unmapped read (e.g.
  // 0x20) makes raddr_idx != IDX_U0_DATA, so u0_rx_pop stays low and no FIFO
  // rd_en asserts -- the data-loss alias is gone (see the header note).
  wire u0_tx_push = slv_reg_wren && (waddr_idx == IDX_U0_DATA) && s_axi_wstrb[0];
  wire u1_tx_push = slv_reg_wren && (waddr_idx == IDX_U1_DATA) && s_axi_wstrb[0];

  wire u0_rx_pop  = slv_reg_rden && (raddr_idx == IDX_U0_DATA);
  wire u1_rx_pop  = slv_reg_rden && (raddr_idx == IDX_U1_DATA);
  wire swo_rx_pop = slv_reg_rden && (raddr_idx == IDX_SWO_RX);

  // ===========================================================================
  // The five stream FIFOs (uartbr_async_fifo.sv — gray-pointer dual-clock,
  // FIFO_DEPTH deep each). Naming: *_tx_* = MicroBlaze -> DUT (regmap
  // "TX"), *_rx_* = DUT -> MicroBlaze (regmap "RX"). Note the deliberate
  // crossover at the partition pins: the MB's U0_TX stream feeds the DUT's
  // uart_rx_* AXIS input, and the DUT's uart_tx_* AXIS output feeds the
  // MB's U0_RX window (partition-pins.md names the pins from the DUT's
  // perspective; the regmap names the registers from the MB's).
  // ===========================================================================

  // -- U0 host->DUT: write side s_axi_aclk (U0_TX pushes), read side
  //    dut_clk (drives the uart_rx_* AXIS partition pins) --
  logic       u0_tx_full, u0_tx_empty;
  logic [7:0] u0_tx_rdata;

  uartbr_async_fifo #(
    .WIDTH (8),
    .DEPTH (FIFO_DEPTH)
  ) u_u0_tx_fifo (
    .wclk_i    (s_axi_aclk),
    .wrst_n_i  (s_axi_aresetn),
    .wr_en_i   (u0_tx_push),
    .wr_data_i (s_axi_wdata[7:0]),
    .full_o    (u0_tx_full),
    .rclk_i    (dut_clk_i),
    .rrst_n_i  (dut_rst_n),
    .rd_en_i   (uart_rx_tready_i && !u0_tx_empty),
    .rd_data_o (u0_tx_rdata),
    .empty_o   (u0_tx_empty)
  );

  assign uart_rx_tdata_o  = u0_tx_rdata;
  assign uart_rx_tvalid_o = ~u0_tx_empty;

  // -- U0 DUT->host: write side dut_clk (uart_tx_* AXIS partition pins),
  //    read side s_axi_aclk (U0_RX pops) --
  logic       u0_rx_full, u0_rx_empty;
  logic [7:0] u0_rx_rdata;

  uartbr_async_fifo #(
    .WIDTH (8),
    .DEPTH (FIFO_DEPTH)
  ) u_u0_rx_fifo (
    .wclk_i    (dut_clk_i),
    .wrst_n_i  (dut_rst_n),
    .wr_en_i   (uart_tx_tvalid_i && !u0_rx_full),
    .wr_data_i (uart_tx_tdata_i),
    .full_o    (u0_rx_full),
    .rclk_i    (s_axi_aclk),
    .rrst_n_i  (s_axi_aresetn),
    .rd_en_i   (u0_rx_pop),
    .rd_data_o (u0_rx_rdata),
    .empty_o   (u0_rx_empty)
  );

  // AXIS backpressure toward the DUT: never drop a DUT console byte — hold
  // tready low while the FIFO is full (the DUT's cmsdk_apb_usrt stalls its
  // own TX, exactly as it would against a busy physical UART).
  assign uart_tx_tready_o = ~u0_rx_full;

  // -- U1 host->DUT (reserved seam, symmetric with U0) --
  logic       u1_tx_full, u1_tx_empty;
  logic [7:0] u1_tx_rdata;

  uartbr_async_fifo #(
    .WIDTH (8),
    .DEPTH (FIFO_DEPTH)
  ) u_u1_tx_fifo (
    .wclk_i    (s_axi_aclk),
    .wrst_n_i  (s_axi_aresetn),
    .wr_en_i   (u1_tx_push),
    .wr_data_i (s_axi_wdata[7:0]),
    .full_o    (u1_tx_full),
    .rclk_i    (dut_clk_i),
    .rrst_n_i  (dut_rst_n),
    .rd_en_i   (uart1_rx_tready_i && !u1_tx_empty),
    .rd_data_o (u1_tx_rdata),
    .empty_o   (u1_tx_empty)
  );

  assign uart1_rx_tdata_o  = u1_tx_rdata;
  assign uart1_rx_tvalid_o = ~u1_tx_empty;

  // -- U1 DUT->host (reserved seam, symmetric with U0) --
  logic       u1_rx_full, u1_rx_empty;
  logic [7:0] u1_rx_rdata;

  uartbr_async_fifo #(
    .WIDTH (8),
    .DEPTH (FIFO_DEPTH)
  ) u_u1_rx_fifo (
    .wclk_i    (dut_clk_i),
    .wrst_n_i  (dut_rst_n),
    .wr_en_i   (uart1_tx_tvalid_i && !u1_rx_full),
    .wr_data_i (uart1_tx_tdata_i),
    .full_o    (u1_rx_full),
    .rclk_i    (s_axi_aclk),
    .rrst_n_i  (s_axi_aresetn),
    .rd_en_i   (u1_rx_pop),
    .rd_data_o (u1_rx_rdata),
    .empty_o   (u1_rx_empty)
  );

  assign uart1_tx_tready_o = ~u1_rx_full;

  // -- SWO DUT->host: write side dut_clk (deserialised trace bytes), read
  //    side s_axi_aclk (SWO_RX pops). Trace is lossy-by-nature under
  //    overrun: drop-newest, with the sticky overflow bit above for
  //    visibility (a trace stream has no backpressure to give). --
  logic       swo_rx_empty;
  logic [7:0] swo_rx_rdata;

  uartbr_async_fifo #(
    .WIDTH (8),
    .DEPTH (FIFO_DEPTH)
  ) u_swo_fifo (
    .wclk_i    (dut_clk_i),
    .wrst_n_i  (dut_rst_n),
    .wr_en_i   (swo_byte_valid),
    .wr_data_i (swo_byte),
    .full_o    (swo_fifo_full),
    .rclk_i    (s_axi_aclk),
    .rrst_n_i  (s_axi_aresetn),
    .rd_en_i   (swo_rx_pop),
    .rd_data_o (swo_rx_rdata),
    .empty_o   (swo_rx_empty)
  );

  // ===========================================================================
  // Read data mux. Data windows return {valid, byte} per the regmap's
  // "[7:0] data, [8] valid" field spec; byte forced to 0 when invalid so
  // firmware can't accidentally consume stale FIFO contents.
  // ===========================================================================
  wire [8:0] u0_rx_word  = u0_rx_empty  ? 9'h000 : {1'b1, u0_rx_rdata};
  wire [8:0] u1_rx_word  = u1_rx_empty  ? 9'h000 : {1'b1, u1_rx_rdata};
  wire [8:0] swo_rx_word = swo_rx_empty ? 9'h000 : {1'b1, swo_rx_rdata};

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_U0_DATA:     axi_rdata_q <= {23'd0, u0_rx_word};
        IDX_U1_DATA:     axi_rdata_q <= {23'd0, u1_rx_word};
        IDX_SWO_RX:      axi_rdata_q <= {23'd0, swo_rx_word};
        IDX_FIFO_STATUS: axi_rdata_q <= {27'd0, swo_rx_empty,
                                         u1_rx_empty, u1_tx_full,
                                         u0_rx_empty, u0_tx_full};
        IDX_SWO_CFG:     axi_rdata_q <= {swo_ferr_sync_q[1], swo_ovfl_sync_q[1],
                                         13'd0, swo_en_q, swo_div_q};
        default:         axi_rdata_q <= '0;   // 0x04/0x0C reserved + unmapped
      endcase
    end
  end

endmodule

// -----------------------------------------------------------------------------
// AMBIGUITIES FOR A6 (see also this block's README):
//
// 1. SWO capture mode: the Cortex-M TPIU emits SWO as either UART/NRZ or
//    Manchester. Only UART (NRZ) mode is implemented (swo_uart_rx.sv);
//    the DUT-side TPIU must be configured for NRZ and the SWO_CFG divisor
//    programmed to match its baud. If Manchester capture is ever wanted,
//    it is a drop-in sibling of swo_uart_rx.sv behind the same
//    byte+valid seam — confirm which mode the nanosoc/CMSDK trace path
//    actually uses before building it.
// 2. SWO_CFG @ 0x18 is NOT in shell-regmap.md v0.1 (the contract's UARTBR
//    table ends at 0x14). The deliverable brief explicitly asked for "a
//    programmable divisor register you add at a spare offset, documented"
//    — codify {[31] frame_err(ro), [30] overflow(ro), [16] enable,
//    [15:0] divisor} into regmap v0.2, or strike it and fix the divisor at
//    build time.
// 3. "[8] valid" on the U0_TX/U1_TX WRITE side: read as a read-side-only
//    field here (writes push unconditionally, bit 8 ignored, full-drop
//    policy + FIFO_STATUS poll). If A6 prefers write-qualified pushes
//    (only push when wdata[8]=1), it is a one-line change to the
//    *_tx_push strobes.
// 4. FIFO_STATUS bit assignment: the contract says "per-stream
//    tx_full/rx_empty" without concrete bit positions. Chosen here:
//    [0] u0_tx_full, [1] u0_rx_empty, [2] u1_tx_full, [3] u1_rx_empty,
//    [4] swo_rx_empty. Needs codifying into regmap v0.2.
// 5. uart1_* ports are not partition pins in partition-pins.md v0.1
//    (single-core I1 boundary = one console UART). Implemented anyway per
//    the regmap's "present but unused until multicore"; the BD must tie
//    uart1_tx_tvalid_i = 0 / uart1_rx_tready_i = 0 (and the tdata don't-
//    care) until the multicore RM widens the pin contract via A6.
// 6. DUT-side FIFO reset rides s_axi_aresetn (synchronized), NOT
//    dut_resetn/rp_resetn — console bytes survive a DUT reset/swap, and
//    whether to drain stale console data across a swap is left to the
//    coordinator firmware. If A6 wants hardware flushes instead, add a
//    FIFO_CTRL register (regmap change) rather than wiring rp_resetn in.
// 7. RESOLVED (2026-07-09, SystemRDL equivalence gate) -- SILENT DATA-LOSS FIX.
//    Previously only address bits [4:2] were decoded, so raddr_idx aliased
//    offsets >= 0x20 onto the mapped windows (0x20 -> U0_DATA, ...). Because
//    U0_DATA/U1_DATA/SWO_RX are DESTRUCTIVE-READ FIFO ports, an aliased READ of
//    an unmapped offset asserted u0_rx_pop and POPPED the U0 RX FIFO, silently
//    dropping a byte of DUT console/trace data on stray/speculative traffic --
//    not merely returning a wrong value. The equivalence TB (poc/systemrdl)
//    flagged the U0_TXRX window as not-comparable and called out exactly this
//    pop-on-aliased-read hazard. Fix: waddr_idx/raddr_idx now span the full
//    local word address (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]), so the
//    (raddr_idx == IDX_*) pop-strobe equalities are FALSE for every unmapped
//    offset -- no FIFO rd_en can assert outside a genuinely-mapped data-window
//    read. Mapped-offset behaviour (a mapped read still pops exactly as before)
//    is bit-for-bit unchanged; an unmapped read now pops nothing and returns 0.
// -----------------------------------------------------------------------------
