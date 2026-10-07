// -----------------------------------------------------------------------------
// rm_uart_echo.sv — rm_uart_echo: the first DUT that moves DATA across the RP
// boundary, not just a constant register.
//
// Every RM proven so far (rm_greybox / rm_led / rm_regdemo_a / rm_regdemo_b /
// rm_nanosoc / rm_eth_ss) either exposes an inert boundary or a fixed rm_id /
// GPIO pattern. This one exercises the partition-pins.md "Console / trace"
// AXI-Stream byte pair (uart_tx_* DUT->host, uart_rx_* host->DUT) — the first
// RM that genuinely puts the decoupler boundary and the shell's uart_bridge
// (fpga/shell/ip/uart_bridge/uart_bridge.sv) through a real data round trip:
//
//   1. On reset it emits a short ASCII banner on uart_tx (proves DUT->host with
//      NO host->DUT path needed — visible on the host's UART0 console the
//      instant the partial lands and the RP comes out of reset).
//   2. Thereafter it echoes every byte accepted on uart_rx straight back out on
//      uart_tx (proves the full host->DUT->host round trip).
//
// Ports are EXACTLY the RP side of docs/contracts/partition-pins.md v0.1,
// byte-for-byte identical to fpga/dfx/rms/rm_led/rm_led.sv and
// fpga/dfx/rms/rm_greybox/rm_greybox.sv (and the shell's rp_dut_stub.sv), so it
// links into u_rp_dut and passes build_dfx.tcl's pr_verify against the
// greybox-locked static unchanged. Every group except the console pair + rm_id
// is tied off to the same safe idle those RMs use — see the tie-off block at
// the bottom.
//
// CLOCK/RESET DOMAIN (contract "Clock/reset domain rule"): everything here runs
// on dut_clk. The RP-boundary AXIS handshakes are ALREADY in the dut_clk domain
// — the shell's uart_bridge owns the async FIFOs that cross to the MicroBlaze
// (s_axi_aclk) side, so this RM sees only same-clock, already-safe handshakes.
// It therefore contains NO clock generation and NO clock-domain crossing of its
// own: the internal FIFO is a plain single-clock synchronous FIFO. Consequence
// for the DFX flow: this RM ships an <rm>_ooc.xdc (standalone OOC timing sign-
// off, create_clock on dut_clk) but NO <rm>_rm.xdc — there are no RM-internal
// generated clocks or async groups to reapply scoped to the RP at link
// (partition-timing.md O2; same disposition as rm_nanosoc).
//
// dut_resetn arrives already deasserted-synchronized-to-domain from the shell;
// this module must NOT re-synchronize it (same note as rm_led.sv). It is used
// directly as the async-assert reset of the two pointer registers below.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rm_uart_echo #(
  parameter int NGPIO = 16   // partition-pins.md "Board-port / GPIO
                             // passthrough -- I4": build parameter, v0
                             // default 16 (matches the other RMs' boundary).
) (
  // ===========================================================================
  // Clocks & resets -- shell -> RP (partition-pins.md "Clocks & resets").
  // dut_clk/dut_resetn drive the banner FSM + FIFO below. rp_resetn/dbg_resetn
  // are accepted but unused (this RM has no debug logic; the console datapath
  // is held in reset by dut_resetn like any other RP logic).
  // ===========================================================================
  input  logic dut_clk,
  input  logic dut_resetn,
  input  logic rp_resetn,
  input  logic dbg_resetn,

  // ===========================================================================
  // Processor debug -- internal JTAG / SWJ-DP (partition-pins.md "Processor
  // debug"). No TAP in this RM -- TCK/TMS/TDI ignored, TDO tied inert.
  // ===========================================================================
  input  logic jtag_tck,
  input  logic jtag_tms,
  input  logic jtag_tdi,
  output logic jtag_tdo,

  // ===========================================================================
  // RM debug -- BSCAN to an RM debug hub (partition-pins.md "RM debug",
  // 2026-10 ILA mint). This RM carries no hub: the 11 legs are ignored and
  // dbg_bscan_tdo is tied low.
  // ===========================================================================
  input  logic dbg_bscan_bscanid_en,
  input  logic dbg_bscan_capture,
  input  logic dbg_bscan_drck,
  input  logic dbg_bscan_reset,
  input  logic dbg_bscan_runtest,
  input  logic dbg_bscan_sel,
  input  logic dbg_bscan_shift,
  input  logic dbg_bscan_tck,
  input  logic dbg_bscan_tdi,
  input  logic dbg_bscan_tms,
  input  logic dbg_bscan_update,
  output logic dbg_bscan_tdo,

  // ===========================================================================
  // Ethernet -- RMII + MDIO (partition-pins.md "Ethernet"). No MAC in this
  // RM -- TX/MDIO outputs tied inert (no IOB/OLOGIC attributes belong in an
  // RM regardless -- HDPR-29, contract "IOB packing note").
  // ===========================================================================
  input  logic       phy_rmii_ref_clk,
  input  logic       phy_rmii_crs_dv,
  input  logic [1:0] phy_rmii_rxd,
  output logic [1:0] phy_rmii_txd,
  output logic       phy_rmii_tx_en,
  output logic       mdc,
  output logic       mdio_o,
  output logic       mdio_oe,
  input  logic       mdio_i,

  // ===========================================================================
  // Console / trace -- AXI-Stream byte (partition-pins.md "Console / trace").
  // THIS is the group this RM actually drives:
  //   uart_tx_* : DUT -> host (this RM is the SOURCE: drives tdata+tvalid,
  //               consumes tready). Carries the banner then the echoes.
  //   uart_rx_* : host -> DUT (this RM is the SINK: consumes tdata+tvalid,
  //               drives tready). Every accepted byte is echoed back on
  //               uart_tx.
  // swo is an unused trace pin here -- tied inert.
  // ===========================================================================
  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,
  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready,
  output logic       swo,

  // ===========================================================================
  // Status / misc -- RP -> shell (partition-pins.md "Status / misc").
  // ===========================================================================
  output logic [31:0] rm_id,
  output logic        dut_lockup,
  output logic        irq_out,

  // ===========================================================================
  // Board-port / GPIO passthrough -- shell <-> RP (partition-pins.md
  // "Board-port / GPIO passthrough -- I4"). Not driven by this RM: released
  // (oe=0), same posture as rm_greybox, so the shell's host-mux (board_gpio
  // GPIO.OWN) is free to drive the pads.
  // ===========================================================================
  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // ===========================================================================
  // Flash / QSPI XiP -- RP <-> shell (partition-pins.md v0.2, the NON-CDC
  // source-synchronous exception). No QSPI controller here: drive the external
  // flash to safe idle (deselected, clock low, lanes tri-stated).
  // ===========================================================================
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i
);

  // ===========================================================================
  // rm_id -- partition-pins.md "Status/misc: rm_id" / shell-regmap.md DFXCTL
  // RM_ID (0x44A1_0010). Read back by the coordinator after a partial load to
  // confirm THIS RM landed.
  //
  // *** RE-NUMBERED 2026-07-14: 0x4543484F -> 0x01000004. ***
  //
  // This RM's id USED to be ASCII "ECHO" (0x4543484F), a self-describing
  // readback value in the spirit of rp_dut_stub.sv's "STUB" (0x53545542).
  // That mnemonic is now GONE, and it had to go: it spent all 32 bits of
  // rm_id, and under encoding v2 (docs/VERSIONING_PLAN.md §3.2) the top half
  // of rm_id is the DESIGN VERSION --
  //
  //     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
  //
  // -- so "ECHO" would decode as design 0x484F at version 69.67. It is the
  // ONE id in the whole library that could not be preserved; every other RM
  // kept its id in the low half (greybox 0x0000, nanosoc 0x0001,
  // eth_ss 0x0002, nanosoc_multicore 0x0003, led 0x001E, regdemo_a 0x00A1,
  // regdemo_b 0x00B2).
  //
  // uart_echo therefore takes design_id 0x0004 -- the next free id after
  // greybox/nanosoc/eth_ss/nanosoc_multicore -- at version 1.0 => 0x01000004.
  // Keep in lockstep with rm_list.tcl; check_rm_id_encoding.py gates it.
  // ===========================================================================
  localparam logic [31:0] RM_ID_UART_ECHO = 32'h0100_0004;  // design 0x0004 @ v1.0
  assign rm_id = RM_ID_UART_ECHO;

  // ===========================================================================
  // Reset-time banner (DUT -> host). Packed ASCII; SystemVerilog right-
  // justifies a string literal into a packed vector, so BANNER[7:0] is the
  // LAST char and the FIRST char sits in the most-significant byte. Char at
  // front-index i is therefore BANNER[(BANNER_LEN-1-i)*8 +: 8].
  // Keep it short + printable; edit here and in tests/rm_uart_echo/ together.
  // ===========================================================================
  // CR is written as the OCTAL escape \015 (0x0D): IEEE 1800 string literals
  // define \n \t \\ \" \v \f \a \ddd — but NOT \r, so "\r" would silently
  // become a bare 'r' under a conforming tool (observed under VCS). \015\012 =
  // CR LF, the idiomatic UART console line ending.
  localparam int BANNER_LEN = 20;
  localparam logic [BANNER_LEN*8-1:0] BANNER = "rm_uart_echo ready\015\012";

  localparam int BIDX_W = $clog2(BANNER_LEN + 1);
  logic [BIDX_W-1:0] banner_idx;
  wire banner_done = (banner_idx == BANNER_LEN[BIDX_W-1:0]);
  // int'() keeps the index arithmetic 32-bit so the variable part-select's
  // base is width-clean (verilator --lint-only silent, matching the sibling
  // RMs); banner_idx is bounded 0..BANNER_LEN so the base never goes negative.
  wire [7:0] banner_byte = BANNER[(BANNER_LEN-1-int'(banner_idx))*8 +: 8];

  // ===========================================================================
  // Single-clock synchronous FWFT FIFO (dut_clk domain). The whole point of
  // the RM: it decouples byte PRODUCTION (banner injector + rx echo) from byte
  // CONSUMPTION (the uart_tx AXIS output) so that AXIS backpressure on either
  // side is handled without ever dropping a byte.
  //
  // DEPTH is a power of two and comfortably exceeds BANNER_LEN, so the whole
  // banner fits even if the host never drains during injection. The one extra
  // MSB on each pointer distinguishes full from empty (classic gray-free
  // single-clock FIFO).
  // ===========================================================================
  localparam int DEPTH = 64;
  localparam int PTR_W = $clog2(DEPTH);        // index width (6)
  logic [7:0]     fifo_mem [0:DEPTH-1];
  logic [PTR_W:0] wptr, rptr;                  // PTR_W+1 bits (wrap bit + index)

  wire [PTR_W-1:0] waddr = wptr[PTR_W-1:0];
  wire [PTR_W-1:0] raddr = rptr[PTR_W-1:0];
  wire fifo_empty = (wptr == rptr);
  wire fifo_full  = (wptr[PTR_W] != rptr[PTR_W]) &&
                    (wptr[PTR_W-1:0] == rptr[PTR_W-1:0]);

  // --- FIFO write port: banner injector has priority; rx echo takes over once
  //     the banner has fully drained INTO the fifo. The two are mutually
  //     exclusive by construction (banner_wr needs !banner_done, rx_accept
  //     needs banner_done), so there is never a same-cycle write conflict.
  wire banner_wr = !banner_done && !fifo_full;
  // uart_rx_tready is asserted ONLY when an accepted byte can be written this
  // cycle (fifo has room AND the banner is out of the way). This is the crux
  // of the no-loss guarantee on the host->DUT side: we never accept a byte we
  // cannot immediately enqueue. (AXIS: tready may combinationally depend on
  // state; it does NOT depend on tvalid, so no tvalid<->tready combinational
  // loop.)
  assign uart_rx_tready = banner_done && !fifo_full;
  wire rx_accept = uart_rx_tvalid && uart_rx_tready;

  wire       fifo_wr_en   = banner_wr | rx_accept;
  wire [7:0] fifo_wr_data = banner_wr ? banner_byte : uart_rx_tdata;

  // --- FIFO read/pop port: pop exactly when the uart_tx beat commits.
  wire fifo_rd_en = uart_tx_tvalid && uart_tx_tready;

  always_ff @(posedge dut_clk or negedge dut_resetn) begin
    if (!dut_resetn) begin
      banner_idx <= '0;
    end else if (banner_wr) begin
      banner_idx <= banner_idx + 1'b1;
    end
  end

  always_ff @(posedge dut_clk or negedge dut_resetn) begin
    if (!dut_resetn) begin
      wptr <= '0;
    end else if (fifo_wr_en) begin
      fifo_mem[waddr] <= fifo_wr_data;
      wptr <= wptr + 1'b1;
    end
  end

  always_ff @(posedge dut_clk or negedge dut_resetn) begin
    if (!dut_resetn) begin
      rptr <= '0;
    end else if (fifo_rd_en) begin
      rptr <= rptr + 1'b1;
    end
  end

  // --- uart_tx AXIS output (DUT -> host). FWFT: the head byte is presented
  //     combinationally whenever the fifo is non-empty. tvalid is a pure
  //     function of !empty, so once asserted it stays asserted until the byte
  //     is accepted (fifo_rd_en) -- it CANNOT drop before tready (AXIS rule),
  //     and tdata holds the same head byte across any number of stalled
  //     cycles while tready is low. fifo_mem is unreset, but tdata is a
  //     don't-care whenever tvalid is low, so the X-until-first-write head is
  //     never observed as a valid beat.
  assign uart_tx_tvalid = !fifo_empty;
  assign uart_tx_tdata  = fifo_mem[raddr];

  // ===========================================================================
  // Everything else -- tied to a safe, inert-but-legally-driven state,
  // identical to rm_greybox.sv / rm_led.sv. No SW-DP, no MAC, no trace, no
  // lockup/IRQ source, no board-GPIO drive in this RM.
  // ===========================================================================
  assign jtag_tdo       = 1'b0;
  assign dbg_bscan_tdo  = 1'b0;
  assign phy_rmii_txd   = 2'b00;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc            = 1'b0;
  assign mdio_o         = 1'b0;
  assign mdio_oe        = 1'b0;   // MDIO tri-stated (input mode)
  assign swo            = 1'b0;
  assign dut_lockup     = 1'b0;
  assign irq_out        = 1'b0;
  assign dut_gpio_o     = '0;
  assign dut_gpio_oe    = '0;     // all DUT GPIO released (host-mux owns pads)

  // QSPI flash: safe idle (deselected, clock low, lanes tri-stated).
  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;      // active-low CS: 1 = deselected
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;   // all four lanes tri-stated (input)

endmodule
