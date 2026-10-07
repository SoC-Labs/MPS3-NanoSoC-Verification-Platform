// -----------------------------------------------------------------------------
// swd_bb.sv — SWDBB regmap block (shell-regmap.md v0.1 @ 0x44A7_0000, I5)
//
// REAL, synthesizable AXI4-Lite slave. New file (the SWDBB block had no
// owned RTL directory until now — see fpga/shell/README.md's block
// inventory and docs/NEXT_WAVE_PLAN.md W-RTL-NEWIP).
//
// Responsibility (shell-regmap.md SWDBB table; partition-pins.md
// "Processor debug — internal SWD" group): the pin-wiggler backing the
// OpenOCD `remote_bitbang` server (net-protocol.md port 6920). This block
// is DELIBERATELY dumb — it is register state wired straight to the SWD
// partition pins, plus one synchronized sample path back:
//   - DRIVE @0x00 (rw): [0] swclk, [1] swdio_o, [2] swdio_oe — each bit
//     write-through to the swd_clk / swd_dio_o / swd_dio_oe partition pins;
//   - SAMPLE @0x04 (ro): [0] swdio_i — the swd_dio_i partition pin, 2-FF
//     synchronized into s_axi_aclk.
// The `swd_server` firmware (A3) times the SWD protocol entirely in
// software: every SWCLK edge is an AXI-Lite DRIVE write, every read-back
// an AXI-Lite SAMPLE read (remote_bitbang chars -> DRIVE/SAMPLE pokes;
// the srst char maps to CLKRST's dbg_resetn, NOT to anything here — see
// shell-regmap.md's SWDBB note).
//
// CDC rationale (partition-pins.md clock/reset-domain rule):
//   - Outputs need no synchronizer: the SWD interface is source-
//     synchronous with a clock THIS block generates (swclk is itself a
//     register bit). Signal pacing is firmware-write-limited — each SWCLK
//     half-period is >= one whole AXI-Lite write transaction (many
//     s_axi_aclk cycles, and vastly longer once the remote_bitbang TCP
//     path is in the loop) — so swdio_o/swdio_oe are guaranteed stable
//     long before and after every swclk edge. The DUT's DAP samples on
//     the swclk edges the firmware makes, never on shell clock edges.
//   - The input (swd_dio_i) IS asynchronous to s_axi_aclk (the DUT drives
//     it during turnaround, timed by its own logic off our swclk) — 2-FF
//     synchronized here, on the static side, per the contract. Firmware
//     reads SAMPLE at remote_bitbang pace (>= one AXI read after the
//     relevant swclk edge), which dwarfs the 2-cycle synchronizer latency.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module swd_bb #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; base
                                           // address (0x44A7_0000, 64 KB page)
                                           // is set in the BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, SWDBB regmap (shell-regmap.md
  // v0.1: 0x00 DRIVE / 0x04 SAMPLE(ro))
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
  // SWD partition pins (partition-pins.md "Processor debug" group, shell's
  // view; pin name + direction suffix per house convention — cf.
  // mdio_phy_model's mdio_i_o / board_gpio's dut_gpio_o_i). The split
  // tristate (o/oe/i) crosses the RP boundary as three scalars; the
  // recombination into the DUT's SWDIOTMS pad-style net happens RM-side
  // (nanosoc's own DAP wiring), same convention as mdio_*/dut_gpio_*.
  // ---------------------------------------------------------------------
  output logic                          swd_clk_o,     // partition pin: swd_clk (SWCLK -> nanosoc SWCLKTCK)
  output logic                          swd_dio_o_o,   // partition pin: swd_dio_o (SWDIO drive value)
  output logic                          swd_dio_oe_o,  // partition pin: swd_dio_oe (SWDIO output-enable)
  input  logic                          swd_dio_i_i    // partition pin: swd_dio_i (SWDIO sampled from DUT)
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
  // Register file (offsets per shell-regmap.md v0.1 SWDBB table)
  // ===========================================================================
  logic [2:0] drive_q;   // 0x00 DRIVE: [0] swclk, [1] swdio_o, [2] swdio_oe
                          // Reset 3'b000: SWCLK low, SWDIO not driven
                          // (oe=0) — the probe releases the wire until the
                          // swd_server firmware explicitly takes it.

  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]).
  // ONLY the two mapped offsets respond (0x00 DRIVE, 0x04 SAMPLE); every other
  // offset in the page is unmapped — reads return 0, writes are ignored
  // (BRESP=OKAY). This matches the SystemRDL-generated decode, which compares
  // the whole address (cpuif_addr == 12'h0 / 12'h4). (Was: only addr[2] was
  // decoded, so 0x08 aliased onto DRIVE and a stray write silently rewrote the
  // SWD pins — see RESOLVED ambiguity #1 below.)
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

  localparam logic [IDX_W-1:0] IDX_DRIVE  = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_SAMPLE = 'h1;  // 0x04 (ro)

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      drive_q <= 3'b000;
    end else if (slv_reg_wren) begin
      unique case (waddr_idx)
        IDX_DRIVE: if (s_axi_wstrb[0]) drive_q <= s_axi_wdata[2:0];
        default: ; // SAMPLE (ro) and every unmapped offset: write accepted
                    // (BRESP=OKAY) with NO effect. With the full-address decode
                    // a stray write (e.g. 0x08) can no longer alias onto DRIVE.
      endcase
    end
  end

  // Write-through to the partition pins: pure register-to-pin wiring, no
  // gating, no sequencing — the firmware IS the SWD engine (v1 of the
  // spec's SWD-probe plan; v2, a hardware CMSIS-DAP port, would replace
  // this whole block, see README).
  assign swd_clk_o    = drive_q[0];
  assign swd_dio_o_o  = drive_q[1];
  assign swd_dio_oe_o = drive_q[2];

  // ===========================================================================
  // SAMPLE path: swd_dio_i is asynchronous to s_axi_aclk (DUT-driven during
  // SWD turnaround) — 2-FF synchronize on the static side per the contract.
  // REAL crossing, single-bit -> plain 2-FF, (* ASYNC_REG *) for MTBF (R4).
  // (Outputs swd_clk/dio_o/dio_oe need NO sync: source-synchronous, firmware-
  // paced, each half-period >= a whole AXI write -- see the CDC rationale in
  // the header; adding output flops would only add latency.)
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [1:0] dio_i_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      dio_i_sync_q <= 2'b00;
    end else begin
      dio_i_sync_q <= {dio_i_sync_q[0], swd_dio_i_i};
    end
  end

  // ===========================================================================
  // Read data mux
  // ===========================================================================
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_DRIVE:  axi_rdata_q <= {29'd0, drive_q};
        IDX_SAMPLE: axi_rdata_q <= {31'd0, dio_i_sync_q[1]};
        default:    axi_rdata_q <= '0;
      endcase
    end
  end

endmodule

// -----------------------------------------------------------------------------
// AMBIGUITIES FOR A6 (see also this block's README):
//
// 1. RESOLVED (2026-07-09, SystemRDL equivalence gate). Previously only
//    address bit [2] was decoded, so offsets >= 0x08 aliased onto
//    DRIVE/SAMPLE (0x08 -> DRIVE, 0x0C -> SAMPLE, ...) instead of reading 0 —
//    and, worse, a WRITE to 0x08 aliased onto DRIVE and silently rewrote the
//    SWD partition pins. The equivalence TB (poc/systemrdl, hand vs the
//    SystemRDL-generated decode) flagged exactly this as its two divergences.
//    Fix: waddr_idx/raddr_idx now span the full local word address
//    (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]), so ONLY 0x00 (DRIVE) and 0x04
//    (SAMPLE) match; every other offset in the page falls to `default`
//    (read 0 / write ignored, BRESP=OKAY). This is the generated decode's
//    full-address compare (cpuif_addr == 12'h0 / 12'h4), so the two
//    divergences disappear and stray traffic can no longer touch the pins.
//    Mapped-offset behaviour (0x00 rw, 0x04 ro) is bit-for-bit unchanged.
// 2. SAMPLE has no "sample age" qualifier: firmware gets the live 2-FF
//    synchronized line level at AXI-read time (exactly what remote_bitbang
//    semantics want — OpenOCD's 'R' char means "read the wire now").
//    Nothing latches the value at a swclk edge; if a future hardware-
//    assisted mode wants edge-latched capture, that's the v2 CMSIS-DAP
//    block, not a patch here.
// 3. Reset drive state chosen as 3'b000 (SWCLK low, SWDIO released).
//    partition-pins.md/shell-regmap.md don't specify an idle polarity;
//    OpenOCD initializes all bitbang outputs explicitly on connect, so
//    only the pre-connect window is affected. Flag if the nanosoc DAP
//    prefers SWCLK idle-high before first connect.
// -----------------------------------------------------------------------------
