// -----------------------------------------------------------------------------
// board_gpio.sv — GPIO regmap block (shell-regmap.md v0.1 @ 0x44AA_0000, I4)
//
// REAL, synthesizable AXI4-Lite slave. New file (I4 board-port passthrough
// had no owned RTL directory until now — see fpga/shell/README.md's block
// inventory table and tests/board_gpio/dut_notes.md).
//
// Responsibility (shell-regmap.md GPIO table; partition-pins.md "Board-port
// / GPIO passthrough" group): the DUT reaches MPS3 board I/O (LEDs, buttons,
// PMOD/Arduino header, spare FMC single-ended) only through the shell, which
// owns the physical pads and brokers a generic NGPIO-wide bus across the RP
// boundary. Default = the DUT's dut_gpio_o/dut_gpio_oe drive the pads
// (OWN=0 per bit); the host can override per-bit via OUT/OE when OWN=1.
// Two existing RMs already drive the RP side of this for real —
// fpga/dfx/rms/rm_led/rm_led.sv (blinks BLINK_BITS of dut_gpio_o/oe) and
// fpga/dfx/rms/rm_greybox/rm_greybox.sv (ties both to '0) — so NGPIO=16 and
// the dut_gpio_o/dut_gpio_oe/dut_gpio_i port shapes are not a guess.
//
// Port names: this file adopts the exact names already assumed by
// tests/board_gpio/test_board_gpio.py / dut_notes.md (the forward-looking
// cocotb bench written ahead of this RTL landing) — `dut_gpio_o_i` /
// `dut_gpio_oe_i` (RP -> this module), `dut_gpio_i_o` (this module -> RP),
// `board_pad_o` / `board_pad_oe` / `board_pad_i` (this module <-> the
// physical pad, split-tristate style matching how swd_dio/mdio/dut_gpio
// are already split at other partition/BD boundaries in this repo — see
// shell_bd.tcl's own ambiguity note #6 on that convention). Matching these
// already-authored names/widths means tests/board_gpio/test_board_gpio.py's
// currently-skipped benches bind cleanly once dut_presence.rtl_ready() flips
// true for this file (no Phase-0-stub marker below -- see that module's
// docstring on why marker-absence is the graduation signal; this comment
// deliberately avoids quoting the literal marker string, which is exactly
// what kept this file mis-detected as a stub until the W-SIM fix).
//
// CDC note: this module deliberately does NOT take a separate dut_clk_i
// port to re-synchronize dut_gpio_i_o into the RP's own clock domain (only
// board_pad_i -> s_axi_aclk is synchronized here, shared by both the IN
// register and dut_gpio_i_o so they can never disagree). See the ambiguity
// list at the bottom for why, and what a stricter alternative would need.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module board_gpio #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; base
                                           // address (0x44AA_0000, 64 KB page)
                                           // is set in the BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32,
  parameter int NGPIO               = 16  // partition-pins.md board-port
                                           // group: "Width NGPIO ... v0: 16"
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, GPIO regmap (shell-regmap.md
  // offsets 0x00 IN(ro) / 0x04 OUT / 0x08 OE / 0x0C OWN)
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
  // RP-facing partition pins (partition-pins.md "Board-port / GPIO
  // passthrough"): dut_gpio_o/dut_gpio_oe are the RP's drive/OE, sampled
  // as inputs here; dut_gpio_i is this module's sampled-pad output back to
  // the RP.
  // ---------------------------------------------------------------------
  input  logic [NGPIO-1:0]              dut_gpio_o_i,
  input  logic [NGPIO-1:0]              dut_gpio_oe_i,
  output logic [NGPIO-1:0]              dut_gpio_i_o,

  // ---------------------------------------------------------------------
  // Physical board-pad side (split-tristate triplet; the actual IOBUF
  // instantiation + pin constraints live in fpga/shell/constraints/, out of
  // this module's scope — partition-pins.md: "Which physical board pin
  // each dut_gpio_* bit maps to is fixed in the shell constraints... not
  // here").
  // ---------------------------------------------------------------------
  output logic [NGPIO-1:0]              board_pad_o,
  output logic [NGPIO-1:0]              board_pad_oe,
  input  logic [NGPIO-1:0]              board_pad_i
);

  // ===========================================================================
  // AXI4-Lite slave — standard Xilinx-template write/read channel FSM
  // (identical structure to dfx_ctl.sv / dut_clkrst.sv for consistency
  // across the shell's custom IP).
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
  assign s_axi_bresp   = 2'b00;   // OKAY
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
  // Register file (offsets per shell-regmap.md GPIO table). IN is read-only
  // (sampled pad, see synchronizer below); OUT/OE/OWN are host R/W. All are
  // NGPIO-wide (v0: 16, so a single 16-bit-wide byte-strobe write covers the
  // whole register — bits [C_S_AXI_DATA_WIDTH-1:NGPIO] are unimplemented and
  // read back as 0).
  // ===========================================================================
  logic [NGPIO-1:0] out_q;  // 0x04 OUT -- reset 0
  logic [NGPIO-1:0] oe_q;   // 0x08 OE  -- reset 0 (host drives nothing by default)
  logic [NGPIO-1:0] own_q;  // 0x0C OWN -- reset 0 (DUT owns every bit by default,
                             // shell-regmap.md: "0 = DUT owns the bit (default)" --
                             // taken as the literal reset value per
                             // tests/board_gpio/dut_notes.md's stated assumption)

  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]).
  // ONLY the four mapped offsets respond (0x00 IN, 0x04 OUT, 0x08 OE, 0x0C
  // OWN); every other offset in the 64 KB page is unmapped — reads return 0,
  // writes are ignored (BRESP=OKAY). This matches the SystemRDL-generated
  // decode, which compares the whole address. (Was: only addr[3:2] was
  // decoded, so 0x10 aliased onto IN and a stray write to 0x14 silently
  // rewrote OUT — see RESOLVED note in README, 2026-07-09.)
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

  localparam logic [IDX_W-1:0] IDX_IN  = 'h0;  // 0x00 (ro)
  localparam logic [IDX_W-1:0] IDX_OUT = 'h1;  // 0x04
  localparam logic [IDX_W-1:0] IDX_OE  = 'h2;  // 0x08
  localparam logic [IDX_W-1:0] IDX_OWN = 'h3;  // 0x0C

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      out_q <= '0;
      oe_q  <= '0;
      own_q <= '0;
    end else if (slv_reg_wren) begin
      unique case (waddr_idx)
        IDX_OUT: begin
          if (s_axi_wstrb[0]) out_q[7:0]  <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1] && NGPIO > 8) out_q[NGPIO-1:8] <= s_axi_wdata[NGPIO-1:8];
        end
        IDX_OE: begin
          if (s_axi_wstrb[0]) oe_q[7:0]  <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1] && NGPIO > 8) oe_q[NGPIO-1:8] <= s_axi_wdata[NGPIO-1:8];
        end
        IDX_OWN: begin
          if (s_axi_wstrb[0]) own_q[7:0]  <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1] && NGPIO > 8) own_q[NGPIO-1:8] <= s_axi_wdata[NGPIO-1:8];
        end
        default: ; // IN (ro) and every unmapped offset: write accepted
                    // (BRESP=OKAY) with NO effect. With the full-address decode
                    // a stray write (e.g. 0x14) can no longer alias onto OUT.
      endcase
    end
  end

  // ===========================================================================
  // board_pad_i synchronizer (s_axi_aclk domain) — shared by BOTH the IN
  // register readback and dut_gpio_i_o, so the two readers can never
  // disagree (partition-pins.md: "the shell brokers one physical net to
  // both sides"; test_gpio_mux_logic.py's
  // test_in_and_dut_gpio_i_see_the_identical_resolved_pad_value() /
  // test_board_gpio.py's test_gpio_in_and_dut_gpio_i_both_sample_the_
  // resolved_pad() both require exact equality). 2-FF synchronizer since
  // board_pad_i is an actual physical board pin (async by nature).
  //
  // R4 CDC: (* ASYNC_REG = "TRUE" *) on BOTH synchronizer stages so the
  // placer keeps them in the same slice (max metastability-resolution MTBF)
  // and the methodology checker stops flagging TIMING-10 "missing property
  // on synchronizer". board_pad_i is genuinely async, so this IS a real
  // crossing that needs the attribute.
  //
  // COHERENCY NOTE (why per-bit 2-FF is CORRECT here, unlike rm_id in
  // dfx_ctl.sv): the NGPIO bits are INDEPENDENT physical board nets (LEDs,
  // buttons, PMOD/Arduino/FMC single-ended). There is no multi-bit "word" to
  // keep coherent — bit 3 and bit 7 resolving from different s_axi_aclk
  // cycles is not a torn value, it is just two unrelated pins sampled at
  // their own times, exactly as intended. A handshake/gray/qualifier scheme
  // would be WRONG for free-running async GPIO inputs (you cannot handshake a
  // button). So per-bit 2-FF is the textbook-correct synchronizer for this
  // bus; the multi-bit-atomicity rule applies only to buses read as one
  // value (rm_id), not to GPIO.
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [NGPIO-1:0] pad_in_sync0_q, pad_in_sync1_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      pad_in_sync0_q <= '0;
      pad_in_sync1_q <= '0;
    end else begin
      pad_in_sync0_q <= board_pad_i;
      pad_in_sync1_q <= pad_in_sync0_q;
    end
  end

  // R4 CROSSING (dut_gpio_i_o -> RP): dut_gpio_i_o is driven from the
  // s_axi_aclk-domain synchronizer above, but the RP samples it on dut_clk
  // -> this IS an s_axi_aclk -> dut_clk crossing whose final flop (here) is
  // clocked by the WRONG domain, so strictly the RP does not receive a
  // dut_clk-safe net. The contract-correct fix is a SECOND, dut_clk-domain
  // 2-FF stage feeding dut_gpio_i_o (per-bit — GPIO independence, see the
  // coherency note above), which requires a new `dut_clk_i` port on this
  // module and a matching BD wire.
  //
  // DELIBERATELY NOT LANDED IN THIS FILE — reasons (see the R4 handoff):
  //   1. It needs `connect_bd_net $dut_clk_sig board_gpio_0/dut_clk_i` in
  //      fpga/shell/bd/shell_bd.tcl, which is another agent's file (I must
  //      not touch it). An unwired dut_clk_i ties to GND at synth -> the new
  //      2-FF never advances -> every GPIO input freezes to 0 on silicon
  //      (buttons dead). That is a worse, silent regression than the status
  //      quo, so this half must land TOGETHER with the BD wire, not before.
  //   2. The current destination already synchronizes: the only real
  //      consumer, rp_nanosoc_wrapper.sv, feeds dut_gpio_i into a CMSDK GPIO
  //      block (its own input sync) AND `set_false_path`s dut_gpio_i[*] in
  //      nanosoc_ooc.xdc; eth_ss ties it unused. So today's crossing lands on
  //      an RM that treats it as async and re-registers it — low severity,
  //      quasi-static (buttons/switches). Defense-in-depth, not load-bearing.
  // The ready RTL+BD+bench patch is handed to the BD agent for the batched
  // P1 rebuild. Until then this stays a single s_axi_aclk chain shared by IN
  // and dut_gpio_i_o (they can never disagree, which the cocotb bench needs).
  assign dut_gpio_i_o = pad_in_sync1_q;

  // ===========================================================================
  // OWN mux — per shell-regmap.md GPIO table: "0 = DUT owns the bit
  // (default), 1 = host mux overrides". Pure combinational bitwise 2:1 mux,
  // selector = own_q; independently verified exhaustively (all 32 rows of
  // the single-bit truth table) by tests/board_gpio/test_gpio_mux_logic.py.
  // No registration here -- board_pad_o/oe must reflect a same-cycle change
  // in dut_gpio_o_i/dut_gpio_oe_i (the DUT's own drive can change every
  // dut_clk cycle; the shell must not add sampling latency to a live GPIO
  // toggle, e.g. rm_led.sv's blink pattern).
  // ===========================================================================
  assign board_pad_o  = (own_q & out_q) | (~own_q & dut_gpio_o_i);
  assign board_pad_oe = (own_q & oe_q)  | (~own_q & dut_gpio_oe_i);

  // ===========================================================================
  // Read data mux
  // ===========================================================================
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_IN:  axi_rdata_q <= {{(C_S_AXI_DATA_WIDTH-NGPIO){1'b0}}, pad_in_sync1_q};
        IDX_OUT: axi_rdata_q <= {{(C_S_AXI_DATA_WIDTH-NGPIO){1'b0}}, out_q};
        IDX_OE:  axi_rdata_q <= {{(C_S_AXI_DATA_WIDTH-NGPIO){1'b0}}, oe_q};
        IDX_OWN: axi_rdata_q <= {{(C_S_AXI_DATA_WIDTH-NGPIO){1'b0}}, own_q};
        default: axi_rdata_q <= '0;
      endcase
    end
  end

endmodule

// -----------------------------------------------------------------------------
// AMBIGUITIES FOR A6 (see also the top-level reply this file ships with):
//
// 1. No dut_clk_i port / no dut_clk-domain resynchronization of dut_gpio_i_o.
//    partition-pins.md's general CDC rule ("every crossing is CDC'd on the
//    static side... the RM sees already-safe signals") would, read strictly,
//    want dut_gpio_i_o re-synchronized into the RP's own dut_clk domain
//    here, not just into s_axi_aclk. This file matches the already-authored
//    tests/board_gpio/test_board_gpio.py / dut_notes.md port list instead
//    (which has no second clock at all), reasoning that GPIO is inherently
//    slow/quasi-static (LEDs, buttons, PMOD) and any real GPIO peripheral
//    inside the RP (e.g. a CMSDK GPIO block) already carries its own input
//    synchronizer, making a second static-side stage defense-in-depth
//    rather than load-bearing. If A6 wants the stricter reading, add a
//    dut_clk_i tap (same pattern as dut_clkrst.sv) and a second 2-FF chain
//    feeding dut_gpio_i_o specifically, leaving the s_axi_aclk chain here
//    for the IN register only.
// 2. Read-only-register writes (IN@0x00) and unmapped offsets are accepted
//    at the AXI-Lite protocol level (BRESP=OKAY, no effect) rather than
//    SLVERR/DECERR -- shell-regmap.md doesn't specify error-response
//    semantics for this case; same convention used in dfx_ctl.sv/
//    dut_clkrst.sv for consistency.
// -----------------------------------------------------------------------------
