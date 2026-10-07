// -----------------------------------------------------------------------------
// axi4lite_protocol_checker.sv — reusable SystemVerilog-assertion (SVA)
// protocol monitor for the shell's custom AXI4-Lite slaves.
//
// A5 verification infrastructure (tests/common/sva/). NOT synthesizable RTL and
// NOT under fpga/ — this module is compiled ALONGSIDE a DUT and attached to it
// with a `bind` statement (see tests/common/sva/bind_*.sv), so it needs no edit
// to the RTL under test. VCS compiles SVA natively (`-sverilog -assert svaext`);
// a firing assertion calls fail() below, which $error()s a descriptive message
// and then $fatal()s so the containing cocotb bench goes RED (run a bench with
// the `+SVA_NOFATAL` plusarg to keep going and collect *every* firing instead).
//
// WHAT IT CHECKS (the two contract-legal slave handshake styles from
// docs/contracts/shell-regmap.md "AXI4-Lite slave conventions" — the
// Xilinx-template FSM used by clkrst/dfx_ctl/board_gpio/swd_bb/telem/uart_bridge
// AND the accept-on-valid style used by mdio_phy_model/gen_checker — are both
// handled by the same properties here):
//
//   * Response payloads are never X while their VALID is asserted
//     (BRESP with BVALID; RDATA/RRESP with RVALID). This is the assertion that
//     catches an unimplemented / tied-'x read-data path.
//   * Response payloads are OKAY only (BRESP/RRESP == 2'b00) — the contract's
//     "unmapped/RO writes are accepted at the protocol level (BRESP=OKAY);
//     unmapped reads return 0" convention. Disable with CHECK_OKAY_ONLY=0 if a
//     future slave grows a legitimate SLVERR path.
//   * Response VALID/payload are held stable until the corresponding READY
//     (slave must not retract or mutate an offered B/R beat before it is
//     accepted). Note: the reference AxiLiteMaster BFM (tests/common/regmap.py)
//     holds BREADY/RREADY high for the whole transaction, so these particular
//     properties pass *vacuously* under the current benches — they are here as
//     correct, future-proof slave-side rules, not as today's active net.
//   * One response per request: outstanding (AW-accepted minus B-accepted, and
//     AR minus R) counters stay within [0, MAX_OUTSTANDING]. A spurious/extra
//     response underflows (negative) and a dropped/stuck response overflows the
//     bound — either fires. This is the "one-response-per-request" check.
//   * Slave-driven handshake/valid controls are never X after reset.
//
// WHAT IT DELIBERATELY DOES NOT CHECK BY DEFAULT (CHECK_MASTER_STABLE=0):
//   master-side AWVALID/WVALID/ARVALID "stable-until-READY". The shared
//   AxiLiteMaster BFM holds AWVALID/WVALID asserted through the whole
//   write() (including a cycle or two AFTER the single-cycle AWREADY/WREADY
//   pulse of the Xilinx-template slaves) and only deasserts them once the B
//   response is captured. That lazy-deassert is benign against these
//   single-in-flight slaves but is a technically-non-compliant master
//   behaviour that would false-fire a naive master-side stability property.
//   Flagged for the BFM owner (A5); enable CHECK_MASTER_STABLE=1 only against a
//   BFM that deasserts VALID the cycle after its handshake.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module axi4lite_protocol_checker #(
  parameter int    ADDR_WIDTH         = 12,
  parameter int    DATA_WIDTH         = 32,
  parameter int    MAX_OUTSTANDING    = 4,
  parameter bit    CHECK_OKAY_ONLY    = 1'b1,
  parameter bit    CHECK_MASTER_STABLE= 1'b0,
  parameter bit    COVER_EN           = 1'b1,
  parameter string NAME               = "axi4lite"
) (
  input logic                    aclk,
  input logic                    aresetn,

  input logic [ADDR_WIDTH-1:0]   awaddr,
  input logic                    awvalid,
  input logic                    awready,

  input logic [DATA_WIDTH-1:0]   wdata,
  input logic [DATA_WIDTH/8-1:0] wstrb,
  input logic                    wvalid,
  input logic                    wready,

  input logic [1:0]              bresp,
  input logic                    bvalid,
  input logic                    bready,

  input logic [ADDR_WIDTH-1:0]   araddr,
  input logic                    arvalid,
  input logic                    arready,

  input logic [DATA_WIDTH-1:0]   rdata,
  input logic [1:0]              rresp,
  input logic                    rvalid,
  input logic                    rready
);

  // Single failure action: descriptive $error, then $fatal so the bench turns
  // RED (unless the operator asked to keep going with +SVA_NOFATAL).
  task automatic fail(input string why);
    $error("[SVA %s] %s (t=%0t)", NAME, why, $time);
    if (!$test$plusargs("SVA_NOFATAL"))
      $fatal(1, "[SVA %s] AXI4-Lite protocol assertion failed", NAME);
  endtask

  // ===========================================================================
  // Handshakes + outstanding-transaction counters (one response per request).
  // ===========================================================================
  wire aw_hs = awvalid && awready;
  wire w_hs  = wvalid  && wready;
  wire b_hs  = bvalid  && bready;
  wire ar_hs = arvalid && arready;
  wire r_hs  = rvalid  && rready;

  int wr_out, rd_out;

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      wr_out <= 0;
      rd_out <= 0;
    end else begin
      wr_out <= wr_out + (aw_hs ? 1 : 0) - (b_hs ? 1 : 0);
      rd_out <= rd_out + (ar_hs ? 1 : 0) - (r_hs ? 1 : 0);
    end
  end

  a_wr_outstanding_lo: assert property (@(posedge aclk) disable iff (!aresetn)
    (wr_out >= 0))
    else fail($sformatf("write response with no outstanding request (wr_out=%0d)", wr_out));
  a_wr_outstanding_hi: assert property (@(posedge aclk) disable iff (!aresetn)
    (wr_out <= MAX_OUTSTANDING))
    else fail($sformatf("write responses not keeping up / stuck (wr_out=%0d > %0d)", wr_out, MAX_OUTSTANDING));
  a_rd_outstanding_lo: assert property (@(posedge aclk) disable iff (!aresetn)
    (rd_out >= 0))
    else fail($sformatf("read data with no outstanding request (rd_out=%0d)", rd_out));
  a_rd_outstanding_hi: assert property (@(posedge aclk) disable iff (!aresetn)
    (rd_out <= MAX_OUTSTANDING))
    else fail($sformatf("read responses not keeping up / stuck (rd_out=%0d > %0d)", rd_out, MAX_OUTSTANDING));

  // ===========================================================================
  // Response payloads must be known (no X) whenever their VALID is high.
  // This is the property that catches a tied-'x / unimplemented read-data mux.
  // ===========================================================================
  a_bresp_known: assert property (@(posedge aclk) disable iff (!aresetn)
    (bvalid |-> !$isunknown(bresp)))
    else fail("BRESP is X while BVALID asserted");
  a_rdata_known: assert property (@(posedge aclk) disable iff (!aresetn)
    (rvalid |-> !$isunknown(rdata)))
    else fail("RDATA is X while RVALID asserted");
  a_rresp_known: assert property (@(posedge aclk) disable iff (!aresetn)
    (rvalid |-> !$isunknown(rresp)))
    else fail("RRESP is X while RVALID asserted");

  // Slave-driven handshake/valid controls must never be X after reset.
  a_ctrl_known: assert property (@(posedge aclk) disable iff (!aresetn)
    (!$isunknown({awready, wready, bvalid, arready, rvalid})))
    else fail("a slave handshake/valid control line is X");

  // ===========================================================================
  // Response is OKAY only (contract convention). Parameterised off if a future
  // slave grows a real SLVERR/DECERR path.
  // ===========================================================================
  if (CHECK_OKAY_ONLY) begin : g_okay
    a_bresp_okay: assert property (@(posedge aclk) disable iff (!aresetn)
      (bvalid |-> (bresp == 2'b00)))
      else fail($sformatf("BRESP != OKAY (got %02b) — contract says OKAY-only", bresp));
    a_rresp_okay: assert property (@(posedge aclk) disable iff (!aresetn)
      (rvalid |-> (rresp == 2'b00)))
      else fail($sformatf("RRESP != OKAY (got %02b) — contract says OKAY-only", rresp));
  end

  // ===========================================================================
  // Response VALID + payload held stable until READY (slave must not retract or
  // mutate an offered beat before acceptance). Vacuous under the current
  // always-ready BFM; correct and active against any back-pressuring master.
  // ===========================================================================
  a_bvalid_until_bready: assert property (@(posedge aclk) disable iff (!aresetn)
    ((bvalid && !bready) |=> bvalid))
    else fail("BVALID dropped before BREADY");
  a_bresp_stable: assert property (@(posedge aclk) disable iff (!aresetn)
    ((bvalid && !bready) |=> $stable(bresp)))
    else fail("BRESP changed while BVALID waiting for BREADY");
  a_rvalid_until_rready: assert property (@(posedge aclk) disable iff (!aresetn)
    ((rvalid && !rready) |=> rvalid))
    else fail("RVALID dropped before RREADY");
  a_rdata_stable: assert property (@(posedge aclk) disable iff (!aresetn)
    ((rvalid && !rready) |=> ($stable(rdata) && $stable(rresp))))
    else fail("RDATA/RRESP changed while RVALID waiting for RREADY");

  // ===========================================================================
  // Master-side VALID stable-until-READY (opt-in — see header note on why this
  // is off by default against the shared lazy-deassert BFM).
  // ===========================================================================
  if (CHECK_MASTER_STABLE) begin : g_master
    a_awvalid_until_awready: assert property (@(posedge aclk) disable iff (!aresetn)
      ((awvalid && !awready) |=> awvalid))
      else fail("AWVALID dropped before AWREADY");
    a_awaddr_stable: assert property (@(posedge aclk) disable iff (!aresetn)
      ((awvalid && !awready) |=> $stable(awaddr)))
      else fail("AWADDR changed while AWVALID waiting for AWREADY");
    a_wvalid_until_wready: assert property (@(posedge aclk) disable iff (!aresetn)
      ((wvalid && !wready) |=> wvalid))
      else fail("WVALID dropped before WREADY");
    a_wdata_stable: assert property (@(posedge aclk) disable iff (!aresetn)
      ((wvalid && !wready) |=> ($stable(wdata) && $stable(wstrb))))
      else fail("WDATA/WSTRB changed while WVALID waiting for WREADY");
    a_arvalid_until_arready: assert property (@(posedge aclk) disable iff (!aresetn)
      ((arvalid && !arready) |=> arvalid))
      else fail("ARVALID dropped before ARREADY");
    a_araddr_stable: assert property (@(posedge aclk) disable iff (!aresetn)
      ((arvalid && !arready) |=> $stable(araddr)))
      else fail("ARADDR changed while ARVALID waiting for ARREADY");
  end

  // ===========================================================================
  // FUNCTIONAL COVER POINTS (A5 constrained-random wave).
  //
  // These are `cover property` directives, not assertions — they never fail
  // the bench; VCS counts how many times each scenario is hit and urg reports
  // the tally (collected under `-assert svaext`, and scored in the coverage DB
  // when the run adds `-cm assert`). They pin exactly the scenarios the new
  // randomized benches are meant to reach: the AW/W arrival orderings (the
  // `aw_en` condition hole), both slave handshake/response styles, and every
  // WSTRB lane pattern. A cover point that stays at 0 hits after the random
  // wave means that scenario was NOT exercised — a coverage hole made visible.
  // Disable with COVER_EN=0 for a simulator that dislikes cover directives.
  // ===========================================================================
  localparam int NBYTES = DATA_WIDTH/8;

  if (COVER_EN) begin : g_cover
    // -- AW/W arrival orderings (the FSM aw_en corner) --
    c_same_cycle_aw_w: cover property (@(posedge aclk) disable iff (!aresetn)
      (aw_hs && w_hs));                       // AW & W accepted the same cycle
    c_aw_first: cover property (@(posedge aclk) disable iff (!aresetn)
      (awvalid && !wvalid && !awready));      // AW presented before W arrives
    c_w_first: cover property (@(posedge aclk) disable iff (!aresetn)
      (wvalid && !awvalid && !wready));       // W presented before AW arrives

    // -- Back-to-back write acceptance (aw_en re-arm path) --
    c_b2b_write: cover property (@(posedge aclk) disable iff (!aresetn)
      (b_hs ##[1:2] aw_hs));                  // new AW within 2 cyc of a B

    // -- Both response styles the two slave families use --
    c_resp_same_cycle: cover property (@(posedge aclk) disable iff (!aresetn)
      (aw_hs && bvalid));                      // accept-on-valid (mdio/gen_checker)
    c_resp_registered: cover property (@(posedge aclk) disable iff (!aresetn)
      (aw_hs && !bvalid ##1 bvalid));          // registered (Xilinx-template FSM)

    // -- Read-during-write (AR handshake while a write is in flight: its
    //    address/data channels still valid or its B response pending) --
    c_read_during_write: cover property (@(posedge aclk) disable iff (!aresetn)
      (ar_hs && (awvalid || wvalid || bvalid)));

    // -- WSTRB lane patterns --
    c_wstrb_full: cover property (@(posedge aclk) disable iff (!aresetn)
      (w_hs && (wstrb == {NBYTES{1'b1}})));
    c_wstrb_zero: cover property (@(posedge aclk) disable iff (!aresetn)
      (w_hs && (wstrb == '0)));
    c_wstrb_partial: cover property (@(posedge aclk) disable iff (!aresetn)
      (w_hs && (wstrb != '0) && (wstrb != {NBYTES{1'b1}})));
    for (genvar gi = 0; gi < NBYTES; gi++) begin : g_wstrb_lane
      c_wstrb_lane: cover property (@(posedge aclk) disable iff (!aresetn)
        (w_hs && wstrb[gi]));                   // each byte lane driven at least once
    end

    // -- Read-data path exercised (RVALID with a non-zero readback), and the
    //    unmapped/zero readback path (RDATA==0 with RVALID) --
    c_rdata_nonzero: cover property (@(posedge aclk) disable iff (!aresetn)
      (rvalid && (rdata != '0)));
    c_rdata_zero: cover property (@(posedge aclk) disable iff (!aresetn)
      (rvalid && (rdata == '0)));

    // -- Reset applied then released (every reset combination the bench runs) --
    c_reset_release: cover property (@(posedge aclk)
      (!$past(aresetn) && aresetn));
  end

endmodule
