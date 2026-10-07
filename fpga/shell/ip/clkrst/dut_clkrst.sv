// -----------------------------------------------------------------------------
// dut_clkrst.sv — CLKRST regmap block (shell-regmap.md v0.1 @ 0x44A0_0000)
//
// REAL, synthesizable AXI4-Lite slave (graduated from the A1 Phase-0 stub).
// Port list is UNCHANGED from the stub (already correct/confirmed by
// tests/clkrst/dut_notes.md) — only the internal logic is new.
//
// Responsibility (spec §5, partition-pins.md "Clock/reset domain rule"):
//   - AXI-Lite front-end for the DUT-clock DRP MMCM (the MMCM itself is a
//     Vivado clk_wiz BD cell, `clk_wiz_dut` in shell_bd.tcl — this module is
//     the register/sequencing layer around it, not the MMCM). The DRP master
//     sequencing itself is left as a documented placeholder (den/dwe/daddr/di
//     tied idle) per the task brief — only the regmap decode of DUT_CLK_SEL/
//     DUT_CLK_DRP is real; wiring an actual preset ROM + DRP FSM is future
//     work explicitly out of this deliverable's scope.
//   - Generates the three DUT-domain resets (dut_resetn, rp_resetn,
//     dbg_resetn) that leave the shell as partition pins: asserted
//     ASYNCHRONOUSLY, deasserted SYNCHRONIZED to dut_clk (contract "Clock/
//     reset domain rule"; spec §5 "Reset") — see the Cummings-pattern
//     generators below, one per reset, all in the dut_clk_i domain (the
//     signals' documented destination domain per partition-pins.md's
//     signal table: "Domain: dut" for all three).
//
// NOT this module's job:
//   - rp_resetn is also gated by fpga/shell/ip/dfx_ctl/dfx_ctl.sv during a
//     reconfig sequence (DFXCTL owns "RP reset gate" per shell-regmap.md) —
//     see rp_resetn_gate_i below, ANDed into the async-assert source here.
//   - dbg_resetn's SRST-from-OpenOCD mapping lives in the not-yet-stubbed
//     swd_probe block; this module just deals with the synchronizer/
//     async-assert plumbing once dbg_reset_req_i arrives.
//
// IMPORTANT NOTE FOR A6 (see also the top-level reply this file ships with):
// tests/clkrst/test_clkrst.py's `_bring_up()` never drives dut_clk_i with a
// Clock() (only s_axi_aclk gets one) and every test samples the *_resetn_o
// outputs exactly one s_axi_aclk RisingEdge after a RESET_CTRL write
// commits. A genuine multi-stage async-assert/sync-deassert generator (what
// this file implements, and what the task brief + partition-pins.md both
// explicitly require) needs the destination clock (dut_clk_i) to actually
// tick, and needs 2-3 of ITS OWN edges after the assert condition clears —
// neither of which the current bench provides, in either clock domain (a
// same-domain s_axi_aclk resync doesn't fit the bench's zero-slack timing
// either — worked through by hand while writing this). This is a bench gap,
// not an RTL bug: recommend adding `Clock(dut.dut_clk_i, ...)` and replacing
// the single post-write RisingEdge with a poll/wait for the expected value.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module dut_clkrst #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; the
                                           // 64 KB page base (shell-regmap.md)
                                           // is set in the BD Address Editor,
                                           // not hardcoded here.
  parameter int C_S_AXI_DATA_WIDTH = 32,
  parameter int C_DRP_ADDR_WIDTH   = 7,   // matches Xilinx clk_wiz DRP DADDR width
  parameter int C_DRP_DATA_WIDTH   = 16
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, CLKRST regmap (shell-regmap.md
  // offsets 0x00 RESET_CTRL / 0x04 DUT_CLK_SEL / 0x08 DUT_CLK_DRP / 0x0C STATUS)
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
  // DRP master toward the external clk_wiz_dut BD cell (Xilinx Clocking
  // Wizard, CONFIG.DYNAMIC_RECONFIG=true) — drives DUT_CLK_DRP register
  // writes/reads through to the MMCM's DRP port (spec §5). Kept as a
  // documented placeholder (idle-tied) per the task brief: "The MMCM/DRP
  // itself can stay an instantiated-IP placeholder."
  // ---------------------------------------------------------------------
  output logic                          drp_den_o,
  output logic                          drp_dwe_o,
  output logic [C_DRP_ADDR_WIDTH-1:0]   drp_daddr_o,
  output logic [C_DRP_DATA_WIDTH-1:0]   drp_di_o,
  // verilator lint_off UNUSED
  input  logic [C_DRP_DATA_WIDTH-1:0]   drp_do_i,
  input  logic                          drp_drdy_i,
  // verilator lint_on UNUSED
  input  logic                          mmcm_locked_i,     // -> STATUS[0]

  // ---------------------------------------------------------------------
  // DUT clock domain tap — for the STATUS.dut_clk_alive heartbeat detector
  // AND the destination domain for the three reset synchronizers below
  // (partition-pins.md: "Domain: dut" for dut_resetn/rp_resetn/dbg_resetn).
  // ---------------------------------------------------------------------
  input  logic                          dut_clk_i,

  // ---------------------------------------------------------------------
  // Reset outputs (dut clock domain) — become partition pins 1:1
  // (partition-pins.md lines 28-31). Async-assert / sync-deassert per rule.
  // ---------------------------------------------------------------------
  input  logic                          ext_por_n_i,       // board-level power-on reset in (async assert source)
  input  logic                          rp_resetn_gate_i,  // from dfx_ctl.sv — ANDed into rp_resetn_o (active-low)
  input  logic                          dbg_reset_req_i,   // from swd_probe (OpenOCD srst), active-high pulse/level

  output logic                          dut_resetn_o,      // partition pin: dut_resetn
  output logic                          rp_resetn_o,       // partition pin: rp_resetn
  output logic                          dbg_resetn_o       // partition pin: dbg_resetn
);

  // ===========================================================================
  // AXI4-Lite slave — standard Xilinx-template write/read channel FSM
  // (identical structure to dfx_ctl.sv / board_gpio.sv for consistency
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
  // Register file (offsets per shell-regmap.md CLKRST table)
  // ===========================================================================
  logic [2:0]  reset_ctrl_q;   // 0x00: [0]=dut_resetn_req [1]=rp_resetn_req [2]=dbg_resetn_req (1=released)
  logic [7:0]  dut_clk_sel_q;  // 0x04: preset id
  logic [15:0] dut_clk_drp_q;  // 0x08: arbitrary DRP window (spec §5 "set DUT clock" register)
  // 0x0C STATUS is read-only: {mmcm_locked_sync, dut_clk_alive}

  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]).
  // ONLY the four mapped offsets respond (0x00 RESET_CTRL, 0x04 DUT_CLK_SEL,
  // 0x08 DUT_CLK_DRP, 0x0C STATUS); every other offset in the 64 KB page is
  // unmapped — reads return 0, writes are ignored (BRESP=OKAY). This matches
  // the SystemRDL-generated decode, which compares the whole address. (Was:
  // only addr[3:2] was decoded, so 0x10 aliased onto RESET_CTRL and a stray
  // write silently rewrote the DUT/RP/dbg reset-request register — see
  // RESOLVED note in README, 2026-07-09.)
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

  localparam logic [IDX_W-1:0] IDX_RESET_CTRL  = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_CLK_SEL     = 'h1;  // 0x04
  localparam logic [IDX_W-1:0] IDX_CLK_DRP     = 'h2;  // 0x08
  localparam logic [IDX_W-1:0] IDX_STATUS      = 'h3;  // 0x0C (ro)

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      // Safe default: all three resets held asserted (not released) until
      // firmware explicitly writes RESET_CTRL -- the RP must never come out
      // of reset just because the shell's AXI-Lite fabric came up.
      reset_ctrl_q  <= 3'b000;
      dut_clk_sel_q <= 8'h00;
      dut_clk_drp_q <= 16'h0000;
    end else if (slv_reg_wren) begin
      unique case (waddr_idx)
        IDX_RESET_CTRL: if (s_axi_wstrb[0]) reset_ctrl_q <= s_axi_wdata[2:0];
        IDX_CLK_SEL:    if (s_axi_wstrb[0]) dut_clk_sel_q <= s_axi_wdata[7:0];
        IDX_CLK_DRP: begin
          if (s_axi_wstrb[0]) dut_clk_drp_q[7:0]  <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1]) dut_clk_drp_q[15:8] <= s_axi_wdata[15:8];
        end
        default: ; // STATUS (ro) and every unmapped offset: write accepted
                    // (BRESP=OKAY) with NO effect. With the full-address decode
                    // a stray write (e.g. 0x10) can no longer alias onto
                    // RESET_CTRL and rewrite the reset-request bits.
      endcase
    end
  end

  // ===========================================================================
  // DRP master — documented placeholder per task brief (real preset-ROM /
  // DRP sequencing FSM is future work). Tied idle/safe: never issues a DRP
  // transaction, so the MMCM's current configuration is left untouched.
  // Xilinx clk_wiz DRP interface for reference: DEN (enable strobe), DWE
  // (write enable), DADDR[6:0], DI[15:0] (write data), DO[15:0] (read data),
  // DRDY (transaction-complete strobe), DCLK (this module's s_axi_aclk, per
  // clk_wiz's "same clock as DRP logic" requirement -- confirm with A6 if a
  // separate DRP clock is used instead).
  // ===========================================================================
  assign drp_den_o   = 1'b0;
  assign drp_dwe_o   = 1'b0;
  assign drp_daddr_o = '0;
  assign drp_di_o    = '0;

  // ===========================================================================
  // mmcm_locked synchronizer (s_axi_aclk domain -- mmcm_locked_i is a
  // clk_wiz status output, asynchronous to s_axi_aclk). REAL crossing,
  // single-bit -> plain 2-FF, (* ASYNC_REG *) for MTBF/TIMING-10 (R4).
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [1:0] mmcm_locked_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      mmcm_locked_sync_q <= 2'b00;
    end else begin
      mmcm_locked_sync_q <= {mmcm_locked_sync_q[0], mmcm_locked_i};
    end
  end

  // ===========================================================================
  // dut_clk_alive heartbeat detector: a free-running toggle in the dut_clk_i
  // domain, double-flop synchronized + edge-detected in s_axi_aclk, with a
  // saturating "stale" timeout so the bit correctly reads 0 once dut_clk_i
  // stops ticking (not just "was seen alive once"). This is a liveness
  // heartbeat, not a frequency counter -- ALIVE_TIMEOUT is a placeholder
  // count (tune against the real s_axi_aclk:dut_clk_i ratio once both clocks
  // are finalized; flagged for A6).
  // ===========================================================================
  // Source toggle lives in the dut_clk domain (SOURCE of the heartbeat CDC,
  // not a synchronizer -> no ASYNC_REG here). SHELL_BD_CONFIDENCE_HANDOFF §2.3
  // suggests a `= 1'b0` declaration initializer to keep it out of X in 4-state
  // sim, but that is DELIBERATELY NOT applied: VCS's initializer_driver_checks
  // rejects a declaration initializer on an always_ff-driven variable
  // (ICPD_INIT "illegal combination of procedural drivers"), which breaks the
  // tests/clkrst bench compile. On FPGA GSR gives it 0 regardless; the real
  // sim fix (per this module's own header) is for the bench to drive dut_clk_i
  // with a Clock() so the heartbeat is exercised. Left as §2.3 flag.
  logic dutclk_toggle_q;

  always_ff @(posedge dut_clk_i) begin
    dutclk_toggle_q <= ~dutclk_toggle_q;
  end

  // Destination sync of the dut_clk->s_axi_aclk heartbeat toggle: 3 deep
  // (extra margin, then edge-detect), single-bit gray (a toggle) -> correct
  // scheme. (* ASYNC_REG *) on the whole chain (R4).
  (* ASYNC_REG = "TRUE" *) logic [2:0] toggle_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      toggle_sync_q <= 3'b000;
    end else begin
      toggle_sync_q <= {toggle_sync_q[1:0], dutclk_toggle_q};
    end
  end

  wire toggle_edge = toggle_sync_q[2] ^ toggle_sync_q[1];

  localparam int ALIVE_TIMEOUT = 255;

  logic [7:0] alive_cnt_q;
  logic       dut_clk_alive_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      alive_cnt_q     <= '0;
      dut_clk_alive_q <= 1'b0;
    end else if (toggle_edge) begin
      alive_cnt_q     <= '0;
      dut_clk_alive_q <= 1'b1;
    end else if (alive_cnt_q == ALIVE_TIMEOUT[7:0]) begin
      dut_clk_alive_q <= 1'b0;
    end else begin
      alive_cnt_q <= alive_cnt_q + 1'b1;
    end
  end

  // ===========================================================================
  // Reset generation — THREE independent domains, same pattern each: the
  // async-assert net (combinational OR of every reason this reset must be
  // held low) directly async-clears a 3-FF synchronizer chain clocked by
  // dut_clk_i (the canonical Cummings async-assert/sync-deassert pattern —
  // the assert edge needs no synchronization by construction, only the
  // release edge does, which is exactly what this buys). See the header
  // note above re: the current cocotb bench not exercising this timing.
  // ===========================================================================
  // R7 FIX: FF-driven async reset (was LUT-driven).
  //
  // Each *_assert_n used to be a COMBINATIONAL AND feeding the negedge in the
  // synchronizer chains below. A LUT driving an async reset can glitch on the
  // assert edge as its inputs settle at different times -- Vivado reports this
  // as LUTAR-1. Registering the term makes an FF output drive the async reset.
  //
  // ext_por_n_i must still assert reset with NO CLOCK RUNNING, so it cannot be
  // registered away. It stays asynchronous, as each register's OWN async clear.
  // The remaining terms are both s_axi_aclk-domain -- reset_ctrl_q is this
  // module's CSR, and rp_resetn_gate_i comes from dfx_ctl.sv -- so they
  // register cleanly there, and the AND now feeds a D input rather than a reset
  // pin. dbg_reset_req_i is tied to 0 in shell_bd.tcl today; if swd_probe ever
  // drives it from an asynchronous domain it needs a 2-FF ASYNC_REG
  // synchronizer AHEAD of this register.
  //
  // Cost: a software-requested assert is delayed by one s_axi_aclk edge. POR
  // behaviour is unchanged -- reset_ctrl_q also resets to 3'b000, so these
  // registers reload 0 and the resets stay held until software releases them.
  // The DEASSERT edge still crosses into dut_clk and is still handled by the
  // async-assert / sync-deassert chains below (R4's ASYNC_REG).
  // ===========================================================================
  logic dut_assert_n_q, rp_assert_n_q, dbg_assert_n_q;

  always_ff @(posedge s_axi_aclk or negedge ext_por_n_i) begin
    if (!ext_por_n_i) begin
      dut_assert_n_q <= 1'b0;
      rp_assert_n_q  <= 1'b0;
      dbg_assert_n_q <= 1'b0;
    end else begin
      dut_assert_n_q <= reset_ctrl_q[0];
      rp_assert_n_q  <= reset_ctrl_q[1] & rp_resetn_gate_i;
      dbg_assert_n_q <= reset_ctrl_q[2] & ~dbg_reset_req_i;
    end
  end

  wire dut_assert_n = dut_assert_n_q;
  wire rp_assert_n  = rp_assert_n_q;
  wire dbg_assert_n = dbg_assert_n_q;

  // Reset synchronizers (async-assert / sync-deassert, dut_clk domain). The
  // deassert edge crosses s_axi_aclk (reset_ctrl_q) -> dut_clk, so the first
  // flop can go metastable -> (* ASYNC_REG *) on all three chains (R4).
  (* ASYNC_REG = "TRUE" *) logic [2:0] dut_sync_q;
  (* ASYNC_REG = "TRUE" *) logic [2:0] rp_sync_q;
  (* ASYNC_REG = "TRUE" *) logic [2:0] dbg_sync_q;

  always_ff @(posedge dut_clk_i or negedge dut_assert_n) begin
    if (!dut_assert_n) dut_sync_q <= 3'b000;
    else                dut_sync_q <= {dut_sync_q[1:0], 1'b1};
  end

  always_ff @(posedge dut_clk_i or negedge rp_assert_n) begin
    if (!rp_assert_n) rp_sync_q <= 3'b000;
    else               rp_sync_q <= {rp_sync_q[1:0], 1'b1};
  end

  always_ff @(posedge dut_clk_i or negedge dbg_assert_n) begin
    if (!dbg_assert_n) dbg_sync_q <= 3'b000;
    else                dbg_sync_q <= {dbg_sync_q[1:0], 1'b1};
  end

  assign dut_resetn_o = dut_sync_q[2];
  assign rp_resetn_o  = rp_sync_q[2];
  assign dbg_resetn_o = dbg_sync_q[2];

  // ===========================================================================
  // Read data mux
  // ===========================================================================
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_RESET_CTRL: axi_rdata_q <= {29'd0, reset_ctrl_q};
        IDX_CLK_SEL:    axi_rdata_q <= {24'd0, dut_clk_sel_q};
        IDX_CLK_DRP:    axi_rdata_q <= {16'd0, dut_clk_drp_q};
        IDX_STATUS:     axi_rdata_q <= {30'd0, dut_clk_alive_q, mmcm_locked_sync_q[1]};
        default:        axi_rdata_q <= '0;
      endcase
    end
  end

endmodule
