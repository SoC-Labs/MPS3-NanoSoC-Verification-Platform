// -----------------------------------------------------------------------------
// jtag_bb.sv — JTAGBB regmap block (STAGED / UNVALIDATED scaffolding, 2026-07-28)
//
//   ***  THIS IS "TEST-TOMORROW" SCAFFOLDING.  ***
//   It has NOT been linted, elaborated, or simulated. It is written to be
//   compile-SHAPED (a one-for-one structural mirror of the proven
//   fpga/shell/ip/swd_bb/swd_bb.sv), but NOTHING here claims to elaborate.
//   See docs/planning/MPS3_JTAG_DEBUG_DESIGN.md for the whole design + the
//   proven-vs-unproven ledger.
//
// This is the JTAG analogue of swd_bb.sv: a DELIBERATELY dumb AXI4-Lite CSR,
// register state wired straight to the JTAG partition pins, plus one
// synchronized sample path back. It backs an OpenOCD `remote_bitbang` JTAG
// server (firmware/jtag_server/, TCP 6921) the same way swd_bb backs the SWD
// `swd_server` on 6920 — the FIRMWARE IS THE JTAG ENGINE, there is zero
// protocol/sequencing logic here.
//
//   - DRIVE  @0x00 (rw): [0] tck, [1] tms, [2] tdi — each bit write-through to
//     the jtag_tck / jtag_tms / jtag_tdi partition pins;
//   - SAMPLE @0x04 (ro): [0] tdo — the jtag_tdo partition pin, 2-FF
//     synchronized into s_axi_aclk.
//
// Base address is 0x44B1_0000 (a distinct 64 KiB page — set in the BD Address
// Editor, NOT here). NOTE: jtag_bb (Option A, software bit-bang) and the
// encrypted axi_jtag hardware shifter (Option B, 0x44AF_0000) are
// MUTUALLY-EXCLUSIVE DUT-debug options — both drive the one DUT SWJ-DP in JTAG
// mode; a build instantiates at most one. The distinct base only lets them
// coexist on the address map on paper while the choice is being made.
//
// vs swd_bb — the JTAG boundary is SIMPLER than SWD: TCK/TMS/TDI are all plain
// shell->DUT outputs and TDO is a plain DUT->shell input. There is NO
// tristate (no o/oe/i split) because JTAG's TDI and TDO are separate
// unidirectional wires. So DRIVE carries 3 output bits (vs swd_bb's
// clk+dio_o+dio_oe) and SAMPLE carries 1 input bit (vs swd_bb's dio_i).
//
// CDC rationale (mirrors swd_bb.sv, partition-pins.md clock/reset-domain rule):
//   - Outputs need no synchronizer: the JTAG interface is source-synchronous
//     with a clock THIS block generates (tck is itself a register bit). Signal
//     pacing is firmware-write-limited — each TCK half-period is >= one whole
//     AXI-Lite DRIVE write (many s_axi_aclk cycles, and vastly longer once the
//     remote_bitbang TCP path is in the loop) — so tms/tdi are guaranteed
//     stable long before and after every tck edge. The DUT's TAP samples TMS/
//     TDI on the tck edges the firmware makes, never on shell clock edges.
//   - The input (jtag_tdo) IS asynchronous to s_axi_aclk (the DUT drives TDO
//     off its own logic, updated on the TCK falling edge) — 2-FF synchronized
//     here, on the static side, per the contract. Firmware reads SAMPLE at
//     remote_bitbang pace (>= one AXI read after the relevant tck edge), which
//     dwarfs the 2-cycle synchronizer latency.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module jtag_bb #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; base
                                           // address (0x44B1_0000, 64 KB page)
                                           // is set in the BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, JTAGBB regmap
  // (0x00 DRIVE / 0x04 SAMPLE(ro)). Standard Xilinx-template FSM, identical
  // structure to swd_bb.sv / dfx_ctl.sv / dut_clkrst.sv / board_gpio.sv for
  // consistency across the shell's custom IP.
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
  // JTAG partition pins (partition-pins.md "Processor debug" group AFTER the
  // SWD->JTAG boundary re-mint, shell's view; pin name + direction suffix per
  // house convention — cf. swd_bb's swd_clk_o / mdio_phy_model's mdio_i_o /
  // board_gpio's dut_gpio_o_i). All four cross the RP boundary as scalars; the
  // RM wrapper wires them to the nanosoc SWJ-DP (SWCLKTCK/SWDIOTMS/nTDI/TDO)
  // and straps the SWJ for JTAG (dap_swj_enable=1, dap_ntrst=1). See the
  // design note §1. Unlike swd_bb there is no o/oe/i tristate split — TDI (out)
  // and TDO (in) are separate wires.
  // ---------------------------------------------------------------------
  output logic                          jtag_tck_o,   // partition pin: jtag_tck (TCK -> nanosoc SWCLKTCK, JTAG mode)
  output logic                          jtag_tms_o,   // partition pin: jtag_tms (TMS -> nanosoc SWDIOTMS, JTAG mode)
  output logic                          jtag_tdi_o,   // partition pin: jtag_tdi (TDI -> nanosoc TDI)
  input  logic                          jtag_tdo_i    // partition pin: jtag_tdo (TDO sampled from DUT)
);

  // ===========================================================================
  // AXI4-Lite slave — standard Xilinx-template write/read channel FSM.
  // (Copied verbatim from swd_bb.sv so the two blocks stay bit-identical in
  // their bus behaviour; only the register file below differs.)
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
  // Register file (JTAGBB — the JTAG analogue of the SWDBB table)
  // ===========================================================================
  logic [2:0] drive_q;   // 0x00 DRIVE: [0] tck, [1] tms, [2] tdi
                          // Reset 3'b000: TCK low, TMS/TDI low — the probe
                          // releases the wires until the jtag_server firmware
                          // explicitly drives them. (swd_bb flag #3 analogue;
                          // flag if the nanosoc TAP prefers TMS idle-high.)

  // Full local word-address decode — SAME REASONING AS swd_bb.sv (do NOT
  // narrow this): shell_bd.tcl instantiates every CSR block with
  // C_S_AXI_ADDR_WIDTH = 32 (the RTL default of 12 is never what ships), so
  // the interconnect hands this slave the FULL system address (0x44B1_0000,
  // not 0x0). Decoding addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] would compare the
  // top of the system address against 'h0 and never match -> every write
  // ignored, every read 0 on silicon (the exact CSR-decode regression the
  // swd_bb header documents). Decode the block's own 64 KiB page instead. Only
  // the two mapped offsets respond (0x00 DRIVE, 0x04 SAMPLE); every other
  // offset in the page is unmapped (read 0 / write inert, BRESP=OKAY), so a
  // stray write cannot alias onto DRIVE and rewrite the JTAG pins.
  //
  // Guarded against a narrower instantiation so the block still elaborates at
  // its 12-bit default (mirrors swd_bb; a tests/csr_decode_width-style bench
  // should elaborate at 32 and drive base+offset).
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
                    // (BRESP=OKAY) with NO effect.
      endcase
    end
  end

  // Write-through to the partition pins: pure register-to-pin wiring, no
  // gating, no sequencing — the firmware IS the JTAG engine (the Option-A
  // software bit-bang; Option B's axi_jtag hardware shifter would replace this
  // whole block, see the design note §2).
  assign jtag_tck_o = drive_q[0];
  assign jtag_tms_o = drive_q[1];
  assign jtag_tdi_o = drive_q[2];

  // ===========================================================================
  // SAMPLE path: jtag_tdo is asynchronous to s_axi_aclk (DUT-driven off its
  // own TCK-falling-edge logic) — 2-FF synchronize on the static side per the
  // contract. Single-bit crossing -> plain 2-FF, (* ASYNC_REG *) for MTBF.
  // (Outputs tck/tms/tdi need NO sync: source-synchronous, firmware-paced,
  // each half-period >= a whole AXI write — see the CDC rationale in the
  // header; adding output flops would only add latency.)
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [1:0] tdo_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      tdo_sync_q <= 2'b00;
    end else begin
      tdo_sync_q <= {tdo_sync_q[0], jtag_tdo_i};
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
        IDX_SAMPLE: axi_rdata_q <= {31'd0, tdo_sync_q[1]};
        default:    axi_rdata_q <= '0;
      endcase
    end
  end

endmodule

// -----------------------------------------------------------------------------
// STAGED-SCAFFOLD FLAGS (see docs/planning/MPS3_JTAG_DEBUG_DESIGN.md §7):
//
// 1. NOT elaborated / linted / simulated. This is a structural mirror of the
//    proven swd_bb.sv; treat it as a proposal until a `verilator --lint-only`
//    and a tests/jtag_bb/ regmap bench (write DRIVE 0..7, check the three pins
//    follow combinationally + read-back; wiggle jtag_tdo_i, check SAMPLE[0]
//    follows after the 2-FF latency; check reset pins 000) actually pass.
// 2. SAMPLE has no "sample age" qualifier: firmware gets the live 2-FF
//    synchronized TDO level at AXI-read time — exactly remote_bitbang 'R'
//    semantics ("read the wire now"). Edge-latched capture would be Option B's
//    axi_jtag hardware shifter, not a patch here.
// 3. Reset drive state chosen 3'b000 (TCK low, TMS/TDI low). partition-pins.md
//    does not specify an idle polarity; OpenOCD initializes all bitbang
//    outputs on connect, so only the pre-connect window is affected. Flag if
//    the nanosoc TAP prefers TMS idle-high before first connect.
// 4. jtag_bb and axi_jtag (0x44AF_0000) are MUTUALLY-EXCLUSIVE — one SWJ-DP,
//    one JTAG wire-set. A build instantiates at most one.
// -----------------------------------------------------------------------------
