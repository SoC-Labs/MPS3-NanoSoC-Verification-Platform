// -----------------------------------------------------------------------------
// mdio_phy_model.sv — RMII virtual-PHY MDIO register model (VPHY regmap,
// shell-regmap.md @ 0x44A3_0000).
//
// One of only two blocks the spec flags as needing genuinely new RTL (spec
// §12) and load-bearing: without this block answering the DUT's MDIO polls,
// the DUT's PHY bring-up / auto-negotiation poll hangs (spec §8.1, §16).
//
// This file is the AXI4-Lite front-end: it owns the VPHY regmap (PHY_STATE
// @0x00 / PHY_ID @0x04 / LINK_EVENT @0x08), synchronizes reset into the mdc
// partition-pin domain, and instantiates the two modules that do the real
// work:
//   - mdio_slave.sv    — Clause-22 MDIO (MDC/MDIO) frame engine (protocol)
//   - phy_reg_model.sv — BMCR/BMSR/PHYID1/PHYID2/ANAR/ANLPAR register file
//                        (content), including the AXI-Lite -> mdc CDC
//
// Single-file-build note: tests/mdio_phy_model/Makefile (outside this
// block's scope) lists only this file as VERILOG_SOURCES ("mdio_phy_model
// is a leaf module" -- written back when the whole thing was one file).
// Rather than fold the frame engine and register file back into one
// module, this file pulls them in with `include (resolved relative to this
// file's own directory per the Verilog LRM, so no -I/+incdir needed) --
// mdio_slave.sv and phy_reg_model.sv remain genuinely separate,
// independently lintable/instantiable modules; only the *build* stays
// single-file for the existing cocotb harness. See this block's README.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

`include "mdio_slave.sv"
`include "phy_reg_model.sv"

module mdio_phy_model #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; base
                                           // address (0x44A3_0000) set in the
                                           // BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32,
  parameter logic [4:0]  C_PHY_ADDR         = 5'd1,          // strapped MDIO
                                                              // PHY address
                                                              // this model
                                                              // answers to
  parameter logic [31:0] C_PHY_ID_DEFAULT   = 32'h0007_C0F1, // reset default
                                                              // PHY_ID (see
                                                              // README); host
                                                              // can override
                                                              // via PHY_ID reg
  parameter int           C_PULSE_HOLD_CYCLES = 32           // mdc cycles a
                                                              // LINK_EVENT.pulse
                                                              // holds the link
                                                              // down
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, VPHY regmap (shell-regmap.md
  // offsets 0x00 PHY_STATE / 0x04 PHY_ID / 0x08 LINK_EVENT)
  // ---------------------------------------------------------------------
  input  logic                          s_axi_aclk,
  input  logic                          s_axi_aresetn,

  // verilator lint_off UNUSED
  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,  // full local word address [11:2] decoded
                                                         // (word-select over this block's page); only
                                                         // the byte-offset [1:0] is unused
  input  logic [2:0]                    s_axi_awprot,  // protection attrs -- not decoded (no
                                                         // privilege/secure distinction in this block)
  // verilator lint_on UNUSED
  input  logic                          s_axi_awvalid,
  output logic                          s_axi_awready,

  input  logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_wdata,
  input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb,
  input  logic                          s_axi_wvalid,
  output logic                          s_axi_wready,

  output logic [1:0]                    s_axi_bresp,
  output logic                          s_axi_bvalid,
  input  logic                          s_axi_bready,

  // verilator lint_off UNUSED
  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,  // full local word address [11:2] decoded,
                                                         // see s_axi_awaddr above
  input  logic [2:0]                    s_axi_arprot,  // protection attrs -- not decoded (no
                                                         // privilege/secure distinction in this block)
  // verilator lint_on UNUSED
  input  logic                          s_axi_arvalid,
  output logic                          s_axi_arready,

  output logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_rdata,
  output logic [1:0]                    s_axi_rresp,
  output logic                          s_axi_rvalid,
  input  logic                          s_axi_rready,

  // ---------------------------------------------------------------------
  // MDIO partition pins — partition-pins.md lines 49-52, shell's view.
  // DUT is MDIO master; this model is the slave the DUT polls.
  // ---------------------------------------------------------------------
  input  logic  mdc_i,       // MDIO clock from DUT
  input  logic  mdio_o_i,    // MDIO data out from DUT (this model's input)
  input  logic  mdio_oe_i,   // DUT's output-enable (1 = DUT driving mdio_o_i)
  output logic  mdio_i_o     // this model's reply, sampled by DUT as its MDIO in
);

  // ===========================================================================
  // Reset synchronizer: s_axi_aresetn (s_axi_aclk domain) -> mdc_rst_n
  // (mdc_i domain). Async-assert / sync-deassert, 2-flop, so the mdc-domain
  // logic (mdio_slave.sv, phy_reg_model.sv) always starts from a known
  // state even if mdc_i hasn't ticked yet -- and settles well within the
  // long (>=32-bit) preamble of the DUT's first real MDIO transaction.
  // ===========================================================================
  logic mdc_rst_meta, mdc_rst_n;
  always_ff @(posedge mdc_i or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      mdc_rst_meta <= 1'b0;
      mdc_rst_n    <= 1'b0;
    end else begin
      mdc_rst_meta <= 1'b1;
      mdc_rst_n    <= mdc_rst_meta;
    end
  end

  // ===========================================================================
  // VPHY register file (AXI-Lite domain) — PHY_STATE / PHY_ID / LINK_EVENT.
  // ===========================================================================
  logic        link_up_q, speed100_q, full_duplex_q; // 0x00 PHY_STATE[2:0]
  logic [31:0] phy_id_q;                              // 0x04 PHY_ID
  logic        force_down_q;                          // 0x08 LINK_EVENT[0]
  logic        pulse_q;                                // 0x08 LINK_EVENT[1] (self-clearing readback)
  logic        pulse_tog_q;                             // internal: toggles per accepted pulse strobe

  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]).
  // ONLY the three mapped offsets (0x00 PHY_STATE, 0x04 PHY_ID, 0x08
  // LINK_EVENT) respond; every other offset in the page is unmapped — reads
  // return 0, writes are accepted (BRESP=OKAY) with NO effect. (Was: only
  // addr[3:2] were decoded, so e.g. 0x10 aliased onto PHY_STATE and a stray
  // write to 0x18 could alias onto LINK_EVENT and fire the PULSE strobe / the
  // mdc-domain link-down — see README RESOLVED note.)  The reads latch only
  // the decoded word INDEX (the truncated-register style), so widening the
  // fix means latching the FULL index; register writes still decode
  // s_axi_awaddr directly (see aw_accept below), no latched copy needed.
  //
  // DECODE-WIDTH CAP (added 2026-07-24 — the bug-#1 guard the eight shell CSR
  // blocks already carry; see fpga/shell/ip/dfx_ctl/dfx_ctl.sv "LOCAL_ADDR_W").
  // shell_bd.tcl instantiates every CSR slave with C_S_AXI_ADDR_WIDTH=32 and
  // drives the FULL interconnect address, not a page-local offset. Without this
  // cap, IDX_W would be 30 and waddr_idx for VPHY's base 0x44A3_0000 would be
  // 0x1128_C000 -- never equal to IDX_PHY_STATE ('h0). Every write ignored,
  // every read 0: the block is alive in simulation and DEAD ON SILICON. That is
  // exactly how bug #1 shipped once already.
  // Capping at 16 decodes the block's own 64 KiB page, so BASE+0x1_0000 cannot
  // alias offset 0. The ternary keeps a narrower instantiation legal, so this is
  // a NO-OP at the currently-shipped 12-bit width (12 < 16 => LOCAL_ADDR_W=12,
  // IDX_W=10, bit-identical to before) and only bites if the port is widened.
  // tests/csr_decode_width BLOCK=mdio_phy_model elaborates at 32 and drives
  // base+offset; reverting LOCAL_ADDR_W to C_S_AXI_ADDR_WIDTH turns it red.
  localparam int ADDR_LSB = 2;
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W    = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_PHY_STATE  = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_PHY_ID     = 'h1;  // 0x04
  localparam logic [IDX_W-1:0] IDX_LINK_EVENT = 'h2;  // 0x08

  logic [IDX_W-1:0] axi_araddr_q;   // latched read word index (full-width)
  logic axi_awready_q, axi_wready_q, axi_bvalid_q;
  logic axi_arready_q, axi_rvalid_q;

  wire [IDX_W-1:0] waddr_idx = s_axi_awaddr[LOCAL_ADDR_W-1:ADDR_LSB];

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00; // OKAY
  assign s_axi_bvalid  = axi_bvalid_q;
  assign s_axi_arready = axi_arready_q;
  assign s_axi_rresp   = 2'b00; // OKAY
  assign s_axi_rvalid  = axi_rvalid_q;

  // ---------------------------------------------------------------------
  // Write channel. Accept AW+W together (both must be valid in the same
  // cycle -- a common, simple, protocol-legal AXI4-Lite simplification;
  // this repo's AxiLiteMaster BFM asserts them together too). Register
  // writes commit in the SAME cycle AW+W are accepted, using the live
  // s_axi_wdata/wstrb -- not a cycle later derived from the registered
  // ready outputs, which would create a same-edge cross-register hazard
  // (see phy_reg_model.sv's sibling read-channel comment for the read-side
  // version of this same trap).
  // ---------------------------------------------------------------------
  logic aw_accept;
  assign aw_accept = ~axi_awready_q && s_axi_awvalid && s_axi_wvalid;

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      axi_awready_q <= 1'b0;
      axi_wready_q  <= 1'b0;
    end else if (aw_accept) begin
      axi_awready_q <= 1'b1;
      axi_wready_q  <= 1'b1;
    end else begin
      axi_awready_q <= 1'b0;
      axi_wready_q  <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      link_up_q     <= 1'b1; // link-up/100/full out of reset (README
      speed100_q    <= 1'b1; // "Link-up defaults") so DUT PHY bring-up
      full_duplex_q <= 1'b1; // never stalls waiting on a host write.
      phy_id_q      <= C_PHY_ID_DEFAULT;
      force_down_q  <= 1'b0;
      pulse_q       <= 1'b0;
      pulse_tog_q   <= 1'b0;
      axi_bvalid_q  <= 1'b0;
    end else begin
      pulse_q <= 1'b0; // self-clearing strobe: low by default every cycle

      if (aw_accept) begin
        case (waddr_idx)
          IDX_PHY_STATE: if (s_axi_wstrb[0]) begin // PHY_STATE
                  link_up_q     <= s_axi_wdata[0];
                  speed100_q    <= s_axi_wdata[1];
                  full_duplex_q <= s_axi_wdata[2];
                end
          IDX_PHY_ID: begin // PHY_ID
                  if (s_axi_wstrb[0]) phy_id_q[7:0]   <= s_axi_wdata[7:0];
                  if (s_axi_wstrb[1]) phy_id_q[15:8]  <= s_axi_wdata[15:8];
                  if (s_axi_wstrb[2]) phy_id_q[23:16] <= s_axi_wdata[23:16];
                  if (s_axi_wstrb[3]) phy_id_q[31:24] <= s_axi_wdata[31:24];
                end
          IDX_LINK_EVENT: if (s_axi_wstrb[0]) begin // LINK_EVENT
                  force_down_q <= s_axi_wdata[0];
                  if (s_axi_wdata[1]) begin
                    pulse_q     <= 1'b1;
                    pulse_tog_q <= ~pulse_tog_q;
                  end
                end
          default: ; // 0xC reserved + every unmapped offset -- no register,
                     // no effect (full-address decode; 0x10/0x18 no longer
                     // alias PHY_STATE/LINK_EVENT, so a stray write cannot fire
                     // the PULSE strobe / mdc-domain link-down)
        endcase
      end

      if (aw_accept && ~axi_bvalid_q) begin
        axi_bvalid_q <= 1'b1;
      end else if (axi_bvalid_q && s_axi_bready) begin
        axi_bvalid_q <= 1'b0;
      end
    end
  end

  // ---------------------------------------------------------------------
  // Read channel. arready and rvalid are triggered off the SAME
  // combinational `ar_accept` condition (both driven from axi_arready_q's
  // *old* value plus the live ARVALID -- not from each other's registered
  // outputs), so both become visible on the very same cycle. This matters
  // here because this repo's AxiLiteMaster BFM (tests/common/regmap.py)
  // drops ARVALID as soon as it observes ARREADY, with no separate wait for
  // RVALID -- if RVALID lagged ARREADY by even one cycle (e.g. by naively
  // gating it on the *registered* axi_arready_q the way a write channel's
  // response can safely do, since ARVALID/WVALID there are held through
  // BVALID), ARVALID would already be gone by the time RVALID's condition
  // got re-evaluated, and the read would hang forever.
  // ---------------------------------------------------------------------
  logic ar_accept;
  assign ar_accept = ~axi_arready_q && s_axi_arvalid;

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      axi_arready_q <= 1'b0;
      axi_araddr_q  <= '0;
    end else if (ar_accept) begin
      axi_arready_q <= 1'b1;
      axi_araddr_q  <= s_axi_araddr[LOCAL_ADDR_W-1:ADDR_LSB];
    end else begin
      axi_arready_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      axi_rvalid_q <= 1'b0;
    end else if (ar_accept) begin
      axi_rvalid_q <= 1'b1;
    end else if (axi_rvalid_q && s_axi_rready) begin
      axi_rvalid_q <= 1'b0;
    end
  end

  always_comb begin
    case (axi_araddr_q)
      IDX_PHY_STATE:  s_axi_rdata = {{(C_S_AXI_DATA_WIDTH-3){1'b0}}, full_duplex_q, speed100_q, link_up_q};
      IDX_PHY_ID:     s_axi_rdata = phy_id_q;
      IDX_LINK_EVENT: s_axi_rdata = {{(C_S_AXI_DATA_WIDTH-2){1'b0}}, pulse_q, force_down_q};
      default:        s_axi_rdata = '0;  // 0xC reserved + every unmapped offset reads 0
    endcase
  end

  // ===========================================================================
  // Submodules: Clause-22 frame engine + register content, both in the mdc
  // domain (see file header + phy_reg_model.sv for the CDC rationale).
  // ===========================================================================
  logic [4:0]  mdio_reg_addr;
  logic        mdio_reg_wr_en;
  logic [15:0] mdio_reg_wr_data;
  logic [15:0] mdio_reg_rd_data;

  mdio_slave #(
    .C_PHY_ADDR (C_PHY_ADDR)
  ) u_mdio_slave (
    .mdc           (mdc_i),
    .rst_n         (mdc_rst_n),
    .mdio_o_i      (mdio_o_i),
    .mdio_oe_i     (mdio_oe_i),
    .mdio_i_o      (mdio_i_o),
    .reg_addr_o    (mdio_reg_addr),
    .reg_wr_en_o   (mdio_reg_wr_en),
    .reg_wr_data_o (mdio_reg_wr_data),
    .reg_rd_data_i (mdio_reg_rd_data)
  );

  phy_reg_model #(
    .C_PHY_ID_DEFAULT    (C_PHY_ID_DEFAULT),
    .C_PULSE_HOLD_CYCLES (C_PULSE_HOLD_CYCLES)
  ) u_phy_reg_model (
    .mdc              (mdc_i),
    .rst_n            (mdc_rst_n),
    .cfg_link_up      (link_up_q),
    .cfg_speed100     (speed100_q),
    .cfg_full_duplex  (full_duplex_q),
    .cfg_phy_id       (phy_id_q),
    .cfg_force_down   (force_down_q),
    .cfg_pulse_toggle (pulse_tog_q),
    .reg_addr         (mdio_reg_addr),
    .reg_wr_en        (mdio_reg_wr_en),
    .reg_wr_data      (mdio_reg_wr_data),
    .reg_rd_data      (mdio_reg_rd_data)
  );

endmodule
