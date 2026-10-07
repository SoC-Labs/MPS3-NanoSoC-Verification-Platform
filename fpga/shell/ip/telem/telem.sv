// -----------------------------------------------------------------------------
// telem.sv — TELEM regmap block (shell-regmap.md v0.1 @ 0x44A5_0000, I9)
//
// REAL, synthesizable AXI4-Lite slave. New file (the TELEM block had no
// owned RTL directory until now — see fpga/shell/README.md's block
// inventory and docs/NEXT_WAVE_PLAN.md W-RTL-NEWIP).
//
// Responsibility (shell-regmap.md TELEM table): the MicroBlaze-facing
// register surface for board power telemetry (INA228 current/voltage/power
// monitor on I2C) — CTRL @0x00, BUS_MV @0x04(ro), CURR_UA @0x08(ro),
// POWER_MW @0x0C(ro), STATUS @0x10(ro) — which `net-protocol.md`'s
// `telemetry` verb ultimately reports to the host (mv/ma/lockup).
//
// WHAT IS REAL vs WHAT IS SEAMED (deliverable brief, W-RTL-NEWIP task 3):
//   - REAL: the whole CSR side — AXI-Lite FSM, register file, atomic
//     sample latching, sticky i2c_err, alarm gating/synchronization, and
//     a SIM_FAKE_DATA self-stimulus mode for benches.
//   - SEAMED: the INA228 I2C master engine itself. This module ends at a
//     clean sample-injection seam (the `ina228_*` port group below); the
//     I2C PHY/transaction FSM is a clearly-bounded FOLLOW-UP MODULE
//     (`ina228_i2c_master`, this directory, not yet written — see the
//     README for its agreed port contract). NOTHING here fakes I2C:
//     with no engine attached and SIM_FAKE_DATA=0, the readings stay 0
//     and STATUS reads benign — visibly "no data source", not plausible
//     garbage.
//
// Clock domains: single-domain block. The seam is DEFINED to be
// s_axi_aclk-synchronous (an I2C engine at 100/400 kHz is trivially run
// from the same clock that runs this register file — no reason to give it
// its own domain). The only async input is alarm_i (level; possibly a
// board ALERT pad — see ambiguity list), 2-FF synchronized here on the
// static side. No partition pins touch this block (telemetry measures the
// board rails around the DUT; it never crosses the RP boundary — the
// dut_lockup half of the "RM-load verify / watchdog" story lives in
// DFXCTL's RM_STATUS, not here).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module telem #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; base
                                           // address (0x44A5_0000, 64 KB page)
                                           // is set in the BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32,

  // Bench/self-stimulus mode: when 1, an internal generator (see below)
  // replaces the ina228_* sample seam so A5 can verify the whole CSR
  // surface with no I2C engine in the build. Synthesis builds use 0.
  // (int, not bit, so bare -G/-pvalue+ overrides work across tools.)
  parameter int SIM_FAKE_DATA      = 0,
  // s_axi_aclk cycles between fake samples (SIM_FAKE_DATA=1 only).
  parameter int FAKE_PERIOD        = 64
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, TELEM regmap (shell-regmap.md
  // v0.1: 0x00 CTRL / 0x04 BUS_MV(ro) / 0x08 CURR_UA(ro) /
  // 0x0C POWER_MW(ro) / 0x10 STATUS(ro))
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
  // INA228 engine seam (toward the follow-up ina228_i2c_master module —
  // NOT partition pins; the I2C pads belong to that engine, not here).
  // Contract: everything in this group is s_axi_aclk-synchronous except
  // alarm_i (async level allowed, 2-FF'd inside). The engine latches
  // ina228_enable_o to run/stop its sampling loop; each completed
  // conversion round-trip presents all three scaled readings together
  // with a 1-cycle ina228_sample_valid_i strobe (atomic set — firmware
  // never sees mid-update mixes of old/new readings). ina228_i2c_err_i
  // strobes (or holds) on any failed I2C transaction (NACK/timeout).
  // ---------------------------------------------------------------------
  output logic                          ina228_enable_o,       // CTRL.enable -> engine run
  output logic                          ina228_alarm_en_o,     // CTRL.alarm_en -> engine (DIAG_ALRT config)
  input  logic                          ina228_sample_valid_i, // 1-cycle strobe: latch the three readings
  input  logic [31:0]                   ina228_bus_mv_i,       // bus voltage, mV (engine pre-scaled)
  input  logic [31:0]                   ina228_curr_ua_i,      // current, µA
  input  logic [31:0]                   ina228_power_mw_i,     // power, mW
  input  logic                          ina228_i2c_err_i,      // I2C transaction failure -> STATUS.i2c_err (sticky)
  input  logic                          alarm_i                // alert level (engine DIAG_ALRT poll or board ALERT pad)
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
  // Register file (offsets per shell-regmap.md v0.1 TELEM table)
  // ===========================================================================
  logic [1:0] ctrl_q;   // 0x00 CTRL: [0] enable, [1] alarm_en — reset 0
                         // (sampling off until firmware turns it on)

  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]).
  // ONLY the five mapped offsets respond; every other offset in the page is
  // unmapped -- reads return 0, writes are ignored (BRESP=OKAY). This matches
  // the SystemRDL-generated decode, which compares the whole address. (Was:
  // only addr[4:2] was decoded, so 0x20 aliased onto CTRL and a stray write
  // silently toggled ina228_enable_o/alarm_en_o -- see RESOLVED ambiguity #5.)
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

  localparam logic [IDX_W-1:0] IDX_CTRL     = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_BUS_MV   = 'h1;  // 0x04 (ro)
  localparam logic [IDX_W-1:0] IDX_CURR_UA  = 'h2;  // 0x08 (ro)
  localparam logic [IDX_W-1:0] IDX_POWER_MW = 'h3;  // 0x0C (ro)
  localparam logic [IDX_W-1:0] IDX_STATUS   = 'h4;  // 0x10 (ro)

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  wire ctrl_wr = slv_reg_wren && (waddr_idx == IDX_CTRL) && s_axi_wstrb[0];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      ctrl_q <= 2'b00;
    end else if (ctrl_wr) begin
      ctrl_q <= s_axi_wdata[1:0];
    end
    // BUS_MV/CURR_UA/POWER_MW/STATUS are read-only -- writes accepted
    // (BRESP=OKAY) but have no effect; unmapped offsets same.
  end

  assign ina228_enable_o   = ctrl_q[0];
  assign ina228_alarm_en_o = ctrl_q[1];

  // ===========================================================================
  // SIM_FAKE_DATA generator: deterministic, bench-checkable stimulus that
  // stands in for the (not-yet-written) I2C engine. While CTRL.enable=1 it
  // strobes a fake sample every FAKE_PERIOD cycles:
  //   BUS_MV   = 1200                      (a 1.2 V core rail)
  //   CURR_UA  = 1000 + 10 * sample_index  (recognizable ramp)
  //   POWER_MW = CURR_UA >> 10             (placeholder math, ~mW at 1.2 V;
  //                                         plumbing-test values, NOT physics)
  // It never raises i2c_err (there is no I2C to fail) — a bench asserts
  // STATUS.i2c_err stays 0 in this mode. alarm_i stays live-from-port in
  // both modes (it is not part of the sample seam), so a bench can poke it
  // directly. Constant-parameter muxing below folds away in synthesis.
  // ===========================================================================
  localparam logic [31:0] FAKE_PERIOD_M1 = FAKE_PERIOD - 1;

  logic [31:0] fake_period_cnt_q;
  logic [15:0] fake_idx_q;
  logic        fake_valid_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      fake_period_cnt_q <= '0;
      fake_idx_q        <= '0;
      fake_valid_q      <= 1'b0;
    end else begin
      fake_valid_q <= 1'b0;
      if (!ctrl_q[0]) begin
        fake_period_cnt_q <= '0;
      end else if (fake_period_cnt_q == FAKE_PERIOD_M1) begin
        fake_period_cnt_q <= '0;
        fake_idx_q        <= fake_idx_q + 1'b1;
        fake_valid_q      <= 1'b1;
      end else begin
        fake_period_cnt_q <= fake_period_cnt_q + 1'b1;
      end
    end
  end

  wire [31:0] fake_curr_ua = 32'd1000 + ({16'd0, fake_idx_q} * 32'd10);
  wire [31:0] fake_bus_mv  = 32'd1200;
  wire [31:0] fake_pow_mw  = fake_curr_ua >> 10;

  // Seam-vs-fake select (SIM_FAKE_DATA is a parameter: one side of each
  // ternary is dead logic and folds away).
  localparam bit FAKE_MODE = (SIM_FAKE_DATA != 0);

  wire        smp_valid  = FAKE_MODE ? fake_valid_q : ina228_sample_valid_i;
  wire [31:0] smp_bus    = FAKE_MODE ? fake_bus_mv  : ina228_bus_mv_i;
  wire [31:0] smp_curr   = FAKE_MODE ? fake_curr_ua : ina228_curr_ua_i;
  wire [31:0] smp_pow    = FAKE_MODE ? fake_pow_mw  : ina228_power_mw_i;
  wire        i2c_err_in = FAKE_MODE ? 1'b0         : ina228_i2c_err_i;

  // ===========================================================================
  // Reading registers: latched ATOMICALLY (all three together) on the
  // sample strobe, gated by CTRL.enable — a disabled block holds its last
  // readings (firmware can still read the final values after disabling;
  // reset is the only thing that zeroes them).
  // ===========================================================================
  logic [31:0] bus_mv_q, curr_ua_q, power_mw_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      bus_mv_q   <= '0;
      curr_ua_q  <= '0;
      power_mw_q <= '0;
    end else if (smp_valid && ctrl_q[0]) begin
      bus_mv_q   <= smp_bus;
      curr_ua_q  <= smp_curr;
      power_mw_q <= smp_pow;
    end
  end

  // ===========================================================================
  // STATUS bits.
  //   i2c_err (sticky): any engine-reported failure latches until firmware
  //   writes CTRL (any value — the natural "re-arm" poke; set wins over a
  //   simultaneous clear so an error can never be lost). Contract defines
  //   no clear semantics — flagged for A6 below.
  //   alarm (live level): alarm_i 2-FF synchronized (async pad allowed),
  //   AND-gated by CTRL.alarm_en.
  // ===========================================================================
  logic i2c_err_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      i2c_err_q <= 1'b0;
    end else if (i2c_err_in) begin
      i2c_err_q <= 1'b1;
    end else if (ctrl_wr) begin
      i2c_err_q <= 1'b0;
    end
  end

  // alarm_i may be an async board ALERT pad (ambiguity #3) -> treat as a REAL
  // crossing: single-bit level, plain 2-FF, (* ASYNC_REG *) for MTBF (R4).
  // If it turns out to be the engine's DIAG_ALRT poll (s_axi_aclk-synchronous)
  // the attribute is harmless. The whole ina228_* seam is DEFINED same-domain
  // (header "Clock domains") so it needs no synchronizer.
  (* ASYNC_REG = "TRUE" *) logic [1:0] alarm_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      alarm_sync_q <= 2'b00;
    end else begin
      alarm_sync_q <= {alarm_sync_q[0], alarm_i};
    end
  end

  wire status_alarm = alarm_sync_q[1] & ctrl_q[1];

  // ===========================================================================
  // Read data mux
  // ===========================================================================
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_CTRL:     axi_rdata_q <= {30'd0, ctrl_q};
        IDX_BUS_MV:   axi_rdata_q <= bus_mv_q;
        IDX_CURR_UA:  axi_rdata_q <= curr_ua_q;
        IDX_POWER_MW: axi_rdata_q <= power_mw_q;
        IDX_STATUS:   axi_rdata_q <= {30'd0, i2c_err_q, status_alarm};
        default:      axi_rdata_q <= '0;
      endcase
    end
  end

endmodule

// -----------------------------------------------------------------------------
// AMBIGUITIES FOR A6 (see also this block's README):
//
// 1. The INA228 I2C master engine is a FOLLOW-UP MODULE
//    (ina228_i2c_master, this directory), deliberately not written this
//    wave — the seam port group above is its agreed contract (README
//    documents the expected engine port list, including the I2C pads and
//    the INA228 register/scaling plan). Do not let anything else grow an
//    I2C master for this board rail in the meantime.
// 2. STATUS.i2c_err clear semantics: contract gives the bit but no clear
//    mechanism. Chosen: sticky, cleared by any CTRL write (set-dominant).
//    Alternatives (W1C on STATUS, clear-on-read) are one-line swaps —
//    codify one into regmap v0.2.
// 3. alarm_i source: INA228's ALERT can surface as (a) a board pad wired
//    to the FPGA (async — why the 2-FF is here) or (b) the engine polling
//    DIAG_ALRT over I2C (synchronous — 2-FF harmless). Which one the MPS3
//    rev actually provides decides whether the engine or the XDC drives
//    this port; the CSR side is agnostic. STATUS.alarm is the LIVE gated
//    level, not sticky (matches "alarm" reading as a condition, not an
//    event) — flag if latching semantics are wanted instead.
// 4. Reading scaling is defined as ENGINE-side (this block stores final
//    mV/µA/mW integers; the engine owns the INA228 LSB math + SHUNT_CAL).
//    Keeps the CSR block free of the shunt-resistor build parameter. If
//    A6 prefers raw-register readback + firmware scaling, the seam widths
//    stay the same — only the engine's documented output units change.
// 5. RESOLVED (2026-07-09, SystemRDL equivalence gate). Previously only
//    address bits [4:2] were decoded, so offsets >= 0x20 aliased onto the
//    mapped registers (0x20 -> CTRL, ...) instead of reading 0 -- and a WRITE
//    to 0x20 aliased onto CTRL, silently toggling ina228_enable_o/alarm_en_o
//    (and clearing the sticky i2c_err). The equivalence TB (poc/systemrdl,
//    hand vs generated decode) flagged exactly this. Fix: waddr_idx/raddr_idx
//    now span the full local word address (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]),
//    so ONLY the five mapped offsets match; every other offset falls to
//    `default` (read 0 / write ignored, BRESP=OKAY). Mapped-offset behaviour
//    is bit-for-bit unchanged.
// -----------------------------------------------------------------------------
