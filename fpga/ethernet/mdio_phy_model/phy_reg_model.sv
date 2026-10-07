// -----------------------------------------------------------------------------
// phy_reg_model.sv — Clause-22 PHY management register CONTENT
// (BMCR/BMSR/PHYID1/PHYID2/ANAR/ANLPAR) for the RMII virtual-PHY MDIO model.
//
// mdio_slave.sv is the frame *protocol* engine; this module is the register
// *file* it reads/writes through reg_addr/reg_wr_en/reg_wr_data/reg_rd_data.
// Clock domain: runs entirely on `mdc` (mdio_slave.sv is itself externally
// clocked by the DUT's MDIO clock, per its own header comment — keeping the
// register content in the same domain means the only real clock-domain
// crossing in this whole block is the handful of AXI-Lite-configured
// "what should this model present" values coming from the shell's
// s_axi_aclk domain (mdio_phy_model.sv's VPHY regmap), synchronized right
// here, at their single point of use.
//
// Read-back policy (resolves the Phase-0 stub's open TODO — "does the model
// reflect what the DUT wrote, or always reassert the AXI-Lite-configured
// values?"):
//   - BMSR / PHYID1 / PHYID2 / ANLPAR are READ-ONLY status registers, driven
//     unconditionally from the AXI-Lite-configured state (PHY_STATE/PHY_ID/
//     LINK_EVENT). The DUT can never write its way into a link state the
//     host didn't configure — that is what makes a host-injected LINK_EVENT
//     authoritative over whatever the DUT's own driver believes it
//     negotiated (spec §8.1's whole point of the exercise).
//   - BMCR / ANAR are real read/write registers (a PHY driver has to be
//     able to write them, e.g. to request auto-neg restart). Writes are
//     latched and echoed back on read, except the two self-clearing
//     control bits (BMCR[15] reset, BMCR[9] restart-auto-neg), which behave
//     like real PHY hardware: read back 1 for exactly the one mdc cycle
//     after being set, then autonomously clear. A BMCR[15] reset also
//     reloads ANAR to its default advertisement (mimics real PHY reset
//     behaviour). Neither touches the AXI-Lite-configured link state.
//
// CDC note: the AXI-Lite config (link_up/speed100/full_duplex/phy_id/
// force_down) is host/test-paced -- at most one update every many mdc
// cycles in practice -- so each bit (and each bit of phy_id) is
// independently double-flopped into this domain. A bit sampled in the one
// mdc cycle immediately following an AXI-Lite write can read stale-vs-fresh
// for that single cycle; every cycle after that it is consistent. The
// one-shot LINK_EVENT.pulse instead crosses as a toggle + edge-detect (the
// standard technique for a single-cycle source-domain event reaching a
// domain that may be slower, or fully idle when the event happens).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module phy_reg_model #(
  parameter logic [31:0] C_PHY_ID_DEFAULT    = 32'h0007_C0F1, // SMSC LAN8720A-
                                                                // style OUI/model
                                                                // ID -- "closest
                                                                // real-world
                                                                // analog" to the
                                                                // MPS3's own
                                                                // SMSC LAN9220
                                                                // (spec §12) --
                                                                // host-overridable
                                                                // via VPHY.PHY_ID.
  parameter int          C_PULSE_HOLD_CYCLES = 32  // mdc cycles a host
                                                    // LINK_EVENT.pulse holds
                                                    // the link down before
                                                    // auto-releasing
) (
  input  logic mdc,
  input  logic rst_n,          // already synchronized into the mdc domain
                                // by the caller (mdio_phy_model.sv)

  // -----------------------------------------------------------------------
  // AXI-Lite-domain configuration (VPHY regmap, shell-regmap.md), driven
  // straight from s_axi_aclk-domain registers in mdio_phy_model.sv.
  // -----------------------------------------------------------------------
  input  logic        cfg_link_up,       // PHY_STATE[0]
  input  logic        cfg_speed100,      // PHY_STATE[1]
  input  logic        cfg_full_duplex,   // PHY_STATE[2]
  input  logic [31:0] cfg_phy_id,        // PHY_ID
  input  logic        cfg_force_down,    // LINK_EVENT[0] (persistent level)
  input  logic        cfg_pulse_toggle,  // toggles once per LINK_EVENT[1] strobe

  // -----------------------------------------------------------------------
  // MDIO-visible register file access -- mdio_slave.sv, synchronous to mdc.
  // -----------------------------------------------------------------------
  input  logic [4:0]  reg_addr,
  input  logic        reg_wr_en,
  input  logic [15:0] reg_wr_data,
  output logic [15:0] reg_rd_data
);

  // ===========================================================================
  // CDC: 2-flop synchronizers, one per config bit / per phy_id bit.
  // ===========================================================================
  logic link_up_m0, link_up_m1;
  logic speed100_m0, speed100_m1;
  logic full_duplex_m0, full_duplex_m1;
  logic force_down_m0, force_down_m1;
  logic [31:0] phy_id_m0, phy_id_m1;
  logic pulse_tog_m0, pulse_tog_m1, pulse_tog_m2;

  always_ff @(posedge mdc or negedge rst_n) begin
    if (!rst_n) begin
      link_up_m0     <= 1'b1; link_up_m1     <= 1'b1; // link-up-by-default
      speed100_m0    <= 1'b1; speed100_m1    <= 1'b1; // (see README "Link-up
      full_duplex_m0 <= 1'b1; full_duplex_m1 <= 1'b1; //  defaults")
      force_down_m0  <= 1'b0; force_down_m1  <= 1'b0;
      phy_id_m0      <= C_PHY_ID_DEFAULT; phy_id_m1 <= C_PHY_ID_DEFAULT;
      pulse_tog_m0   <= 1'b0; pulse_tog_m1 <= 1'b0; pulse_tog_m2 <= 1'b0;
    end else begin
      link_up_m0     <= cfg_link_up;     link_up_m1     <= link_up_m0;
      speed100_m0    <= cfg_speed100;    speed100_m1    <= speed100_m0;
      full_duplex_m0 <= cfg_full_duplex; full_duplex_m1 <= full_duplex_m0;
      force_down_m0  <= cfg_force_down;  force_down_m1  <= force_down_m0;
      phy_id_m0      <= cfg_phy_id;      phy_id_m1      <= phy_id_m0;
      pulse_tog_m0   <= cfg_pulse_toggle; pulse_tog_m1 <= pulse_tog_m0; pulse_tog_m2 <= pulse_tog_m1;
    end
  end

  logic pulse_evt;
  assign pulse_evt = pulse_tog_m1 ^ pulse_tog_m2; // edge-detect on the synced toggle

  // ===========================================================================
  // Pulse hold counter -- forces the link down for C_PULSE_HOLD_CYCLES mdc
  // cycles after a host LINK_EVENT.pulse, then releases back to whatever
  // the level-type config (link_up / force_down) says.
  // ===========================================================================
  localparam int PULSE_CNT_W = $clog2(C_PULSE_HOLD_CYCLES + 1);
  logic [PULSE_CNT_W-1:0] pulse_cnt_q;

  always_ff @(posedge mdc or negedge rst_n) begin
    if (!rst_n) begin
      pulse_cnt_q <= '0;
    end else if (pulse_evt) begin
      pulse_cnt_q <= PULSE_CNT_W'(C_PULSE_HOLD_CYCLES);
    end else if (pulse_cnt_q != '0) begin
      pulse_cnt_q <= pulse_cnt_q - 1'b1;
    end
  end

  logic effective_link_up;
  assign effective_link_up = link_up_m1 & ~force_down_m1 & (pulse_cnt_q == '0);

  // ===========================================================================
  // BMCR (reg 0) / ANAR (reg 4) -- real read/write registers, DUT-writable.
  // ===========================================================================
  localparam logic [15:0] BMCR_DEFAULT = 16'h3100; // auto-neg-en | speed100 | full-duplex
  localparam logic [15:0] ANAR_DEFAULT = 16'h01E1; // sel=802.3 | 10BT | 10BT-FD | 100TX | 100TX-FD

  logic [15:0] bmcr_q, anar_q;

  always_ff @(posedge mdc or negedge rst_n) begin
    if (!rst_n) begin
      bmcr_q <= BMCR_DEFAULT;
      anar_q <= ANAR_DEFAULT;
    end else if (reg_wr_en && reg_addr == 5'd0) begin
      bmcr_q <= reg_wr_data;
      if (reg_wr_data[15]) anar_q <= ANAR_DEFAULT; // soft reset reloads ANAR default
    end else if (reg_wr_en && reg_addr == 5'd4) begin
      anar_q <= reg_wr_data;
    end else begin
      // self-clear the one-shot control bits one mdc cycle after being set
      if (bmcr_q[15]) bmcr_q[15] <= 1'b0;
      if (bmcr_q[9])  bmcr_q[9]  <= 1'b0;
    end
  end

  // ===========================================================================
  // Read-only status registers -- unconditionally AXI-Lite-configured.
  // ===========================================================================
  logic [15:0] bmsr, phyid1, phyid2, anlpar;

  assign phyid1 = phy_id_m1[31:16];
  assign phyid2 = phy_id_m1[15:0];

  always_comb begin
    bmsr[15]   = 1'b0;              // 100BASE-T4
    bmsr[14]   = 1'b1;              // 100BASE-X full duplex (capability)
    bmsr[13]   = 1'b1;              // 100BASE-X half duplex (capability)
    bmsr[12]   = 1'b1;              // 10 Mb/s full duplex (capability)
    bmsr[11]   = 1'b1;              // 10 Mb/s half duplex (capability)
    bmsr[10:9] = 2'b00;             // 100BASE-T2 FD/HD (n/a)
    bmsr[8]    = 1'b0;              // extended status
    bmsr[7]    = 1'b1;              // MF preamble suppression (we're tolerant of it)
    bmsr[6]    = 1'b0;              // reserved
    bmsr[5]    = effective_link_up; // auto-negotiation complete
    bmsr[4]    = 1'b0;              // remote fault (tie 0)
    bmsr[3]    = 1'b1;              // auto-negotiation ability (tie 1)
    bmsr[2]    = effective_link_up; // link status
    bmsr[1]    = 1'b0;              // jabber detect (tie 0)
    bmsr[0]    = 1'b1;              // extended capability (tie 1)
  end

  always_comb begin
    anlpar = 16'h0000; // no link -> nothing negotiated
    if (effective_link_up) begin
      anlpar[4:0]  = 5'b00001;                        // selector: IEEE 802.3
      anlpar[5]    = ~speed100_m1;                      // 10BASE-T
      anlpar[6]    = ~speed100_m1 & full_duplex_m1;      // 10BASE-T full duplex
      anlpar[7]    = speed100_m1;                        // 100BASE-TX
      anlpar[8]    = speed100_m1 & full_duplex_m1;       // 100BASE-TX full duplex
      anlpar[13:9] = 5'b00000;                           // T4 / pause / rsvd / remote-fault
      anlpar[14]   = 1'b1;                               // acknowledge
      anlpar[15]   = 1'b0;                               // next page
    end
  end

  always_comb begin
    case (reg_addr)
      5'd0:    reg_rd_data = bmcr_q;
      5'd1:    reg_rd_data = bmsr;
      5'd2:    reg_rd_data = phyid1;
      5'd3:    reg_rd_data = phyid2;
      5'd4:    reg_rd_data = anar_q;
      5'd5:    reg_rd_data = anlpar;
      default: reg_rd_data = 16'h0000; // regs 6-31: not implemented
    endcase
  end

endmodule
