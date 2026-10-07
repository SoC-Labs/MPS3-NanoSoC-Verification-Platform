// -----------------------------------------------------------------------------
// ahb_mon.sv — passive AHB-Lite bus observatory: counters, latency histogram,
//              and protocol checks, behind an AXI4-Lite CSR.
//
// WHY THIS EXISTS, AND WHY IT IS NOT A TRACE BUFFER
//   An IICE trace on this platform is 1024 samples ~= 20.5 us, and readout over
//   host XVC was MEASURED at ~340 bit/s (2026-07-30) — minutes to hours per
//   capture. That is a microscope: superb once you know WHERE to look, useless
//   for "something goes wrong somewhere in the next ten minutes".
//
//   Counters are the complementary instrument. They run at full clock rate,
//   indefinitely, for a few hundred LUTs, and they answer "did it happen, how
//   often, and how bad" — then you point the IICE at the cycle they identify.
//   The intended workflow is COUNTERS FIND IT, TRACE EXPLAINS IT.
//
//   Precedent in this repo: GENCHK (shell-regmap.md 0x44A6_0000) is exactly this
//   pattern for Ethernet frames — enable + inject + TX_CNT/RX_CNT. This is the
//   bus equivalent.
//
// WHERE IT GOES: INSIDE THE RP, next to the bus being watched. Not the shell.
//   docs/contracts/partition-pins.md is explicit that there is "no shell<->DUT
//   AXI in v0" and everything crossing the partition boundary is "a slow scalar
//   or a low-rate stream" — widening it re-keys static_id and forces every RM
//   partial to be rebuilt. So the shell CANNOT see a DUT bus, and any bus
//   instrument has to live on the DUT side of the boundary. Its CSR is reached
//   the same way the DUT's own peripherals are.
//
// PASSIVE BY CONSTRUCTION. Every AHB signal below is an `input`. There is no
//   path from this module back onto the monitored bus, so it cannot perturb the
//   thing it measures, and a bug in here cannot hang the DUT. Fault INJECTION is
//   deliberately a separate module (it must sit in the path, so it must be
//   reviewed as an intrusive component); mixing the two in one block would make
//   the safe thing carry the risk of the unsafe one.
//
// AHB-Lite ASSUMPTIONS, stated because they gate what the checks can mean:
//   - single master, so HTRANS BUSY (2'b01) is legal only inside a burst and
//     there is exactly one outstanding transfer. No ID tracking, no reordering.
//   - HRESP is 1 bit here (OKAY/ERROR), matching slcorem0.v:63 on this DUT
//     rather than the 2-bit AHB2 encoding. AHB5's 2-bit form would need the
//     port widened; the checks below do not depend on the width.
//   - the address phase of transfer N overlaps the data phase of N-1. So a
//     "transfer" is counted when its DATA phase completes (HREADY high), which
//     is also when HRESP is meaningful.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module ahb_mon #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,   // local offset decode only
  parameter int C_S_AXI_DATA_WIDTH = 32,
  parameter int AHB_ADDR_WIDTH     = 32,
  parameter int AHB_DATA_WIDTH     = 32,
  // Wait-state histogram bucket edges (inclusive lower bounds are 0/1/4/16).
  // Four buckets is a deliberate floor, not a limit: it distinguishes
  // "zero-wait", "a couple", "slow" and "pathological", which is the resolution
  // that changes what you do next. More buckets cost counters and tell you less
  // than the trace would.
  parameter int HIST_BUCKETS       = 4
) (
  // ---- AXI4-Lite CSR ----------------------------------------------------
  // AWPROT/ARPROT and the upper AWADDR/ARADDR bits are unused by design: this
  // is an unprotected debug register file inside one page, decoded on [7:0].
  /* verilator lint_off UNUSED */
  input  logic                              s_axi_aclk,
  input  logic                              s_axi_aresetn,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0]     s_axi_awaddr,
  input  logic [2:0]                        s_axi_awprot,
  input  logic                              s_axi_awvalid,
  output logic                              s_axi_awready,

  input  logic [C_S_AXI_DATA_WIDTH-1:0]     s_axi_wdata,
  input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb,
  input  logic                              s_axi_wvalid,
  output logic                              s_axi_wready,

  output logic [1:0]                        s_axi_bresp,
  output logic                              s_axi_bvalid,
  input  logic                              s_axi_bready,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0]     s_axi_araddr,
  input  logic [2:0]                        s_axi_arprot,
  input  logic                              s_axi_arvalid,
  output logic                              s_axi_arready,

  output logic [C_S_AXI_DATA_WIDTH-1:0]     s_axi_rdata,
  output logic [1:0]                        s_axi_rresp,
  output logic                              s_axi_rvalid,
  input  logic                              s_axi_rready,

  // ---- monitored AHB-Lite bus (ALL INPUTS — passive tap) ----------------
  // SAMPLED ON s_axi_aclk. There is no `hclk` port on purpose: the bus must
  // already be in the CSR clock domain. Watching a bus in another domain needs a
  // synchronizer on every signal and a rethink of what a "count" even means
  // across the crossing — a different, much larger module. Declaring an unused
  // hclk would imply this block handles that. It does not.
  //
  // No HWDATA/HRDATA either: counters, latency and protocol checks are entirely
  // a function of the control and handshake signals. Taking data would add
  // 2 x AHB_DATA_WIDTH of routing for nothing. Data is what the IICE trace is
  // for — this block's job is to tell you WHICH CYCLE to point it at.
  input  logic                              hresetn,
  input  logic [AHB_ADDR_WIDTH-1:0]         haddr,
  input  logic [1:0]                        htrans,
  input  logic                              hwrite,
  input  logic [2:0]                        hsize,
  input  logic [2:0]                        hburst,
  input  logic                              hready,
  input  logic                              hresp
);
  /* verilator lint_on UNUSED */

  // ---------------------------------------------------------------------
  // Register map (local offsets; 4-byte aligned, word writes)
  // ---------------------------------------------------------------------
  localparam logic [7:0] A_CTRL      = 8'h00; // [0] enable  [1] clear (W1P)
  localparam logic [7:0] A_STATUS    = 8'h04; // ro
  localparam logic [7:0] A_CNT_RD    = 8'h08; // ro
  localparam logic [7:0] A_CNT_WR    = 8'h0C; // ro
  localparam logic [7:0] A_CNT_ERR   = 8'h10; // ro
  localparam logic [7:0] A_CNT_WAIT  = 8'h14; // ro  total wait-state cycles
  localparam logic [7:0] A_CNT_IDLE  = 8'h18; // ro  cycles with HTRANS==IDLE
  localparam logic [7:0] A_VIOL      = 8'h1C; // RW1C sticky violation bitmap
  localparam logic [7:0] A_VIOL_ADDR = 8'h20; // ro  HADDR at the FIRST violation
  localparam logic [7:0] A_VIOL_INFO = 8'h24; // ro  [3:0] first code, [15:8] count
  localparam logic [7:0] A_HIST0     = 8'h28; // ro  0 wait states
  localparam logic [7:0] A_HIST1     = 8'h2C; // ro  1..3
  localparam logic [7:0] A_HIST2     = 8'h30; // ro  4..15
  localparam logic [7:0] A_HIST3     = 8'h34; // ro  16+
  localparam logic [7:0] A_MAX_WAIT  = 8'h38; // ro  worst single transfer

  // Violation codes. Kept as an explicit enum so the FIRST-violation code in
  // VIOL_INFO is decodable without reading this file's git history.
  localparam int V_ADDR_CHANGED  = 0; // HADDR moved during a wait state
  localparam int V_TRANS_CHANGED = 1; // HTRANS moved during a wait state
  localparam int V_WRITE_CHANGED = 2; // HWRITE moved during a wait state
  localparam int V_SIZE_ILLEGAL  = 3; // HSIZE wider than the data bus
  localparam int V_BUSY_NO_BURST = 4; // HTRANS==BUSY outside a burst
  localparam int V_RESP_ON_IDLE  = 5; // HRESP==ERROR with no transfer in flight
  localparam int N_VIOL          = 6;

  localparam logic [1:0] T_IDLE   = 2'b00;
  localparam logic [1:0] T_BUSY   = 2'b01;
  localparam logic [1:0] T_NONSEQ = 2'b10;
  localparam logic [1:0] T_SEQ    = 2'b11;

  // Largest legal HSIZE for this data width: log2(bytes).
  localparam int MAX_HSIZE = $clog2(AHB_DATA_WIDTH/8);

  // ---------------------------------------------------------------------
  // Monitor state
  // ---------------------------------------------------------------------
  logic        enable_q;
  logic [31:0] cnt_rd_q, cnt_wr_q, cnt_err_q, cnt_wait_q, cnt_idle_q;
  logic [31:0] hist_q [HIST_BUCKETS];
  logic [31:0] max_wait_q;
  logic [N_VIOL-1:0] viol_q;
  logic [AHB_ADDR_WIDTH-1:0] viol_addr_q;
  logic [3:0]  viol_first_q;
  logic [7:0]  viol_cnt_q;
  logic        viol_seen_q;

  // Address-phase shadow, for the "held during wait states" checks and for
  // attributing a completed data phase to the right transfer.
  logic [AHB_ADDR_WIDTH-1:0] a_addr_q;
  logic [1:0]  a_trans_q;
  logic        a_write_q;
  logic        a_valid_q;      // an address phase is present this cycle
  logic [31:0] wait_q;         // wait states accrued by the transfer in flight
  logic        in_burst_q;     // a burst is open (for the BUSY legality check)

  wire a_active = (htrans == T_NONSEQ) || (htrans == T_SEQ);

  // ---------------------------------------------------------------------
  // CSR write DECODE only. Every register lives in the one always_ff below.
  //
  // This split is not stylistic: enable_q and viol_q are written by both the
  // monitor and the CSR, and driving one variable from two always_ff blocks is
  // ILLEGAL SystemVerilog. Verilator's lint accepted it; VCS refused to
  // elaborate ("Variable viol_q is driven by an invalid combination of
  // procedural drivers"), which is the whole reason this bench exists.
  // ---------------------------------------------------------------------
  wire        csr_wr    = !s_axi_bvalid && s_axi_awvalid && s_axi_wvalid;
  wire [7:0]  csr_off   = s_axi_awaddr[7:0];
  // Only the low bits carry meaning (CTRL is 2 bits, VIOL is N_VIOL); the rest
  // of the data bus is legitimately ignored on a write.
  /* verilator lint_off UNUSED */
  wire [31:0] csr_data  = s_axi_wdata;
  /* verilator lint_on UNUSED */
  wire        csr_be0   = s_axi_wstrb[0];

  wire        wr_ctrl     = csr_wr && csr_be0 && (csr_off == A_CTRL);
  wire        clear_pulse = wr_ctrl && csr_data[1];
  wire [N_VIOL-1:0] viol_clr_mask =
      (csr_wr && csr_be0 && (csr_off == A_VIOL)) ? csr_data[N_VIOL-1:0]
                                                 : {N_VIOL{1'b0}};

  // Combinational violation vector for THIS cycle.
  logic [N_VIOL-1:0] viol_now;
  always_comb begin
    viol_now = '0;
    // Control must be held stable while the slave inserts wait states. Only
    // meaningful when an address phase was already presented and is still being
    // held (a_valid_q) and the bus has not completed it (hready low).
    if (a_valid_q && !hready) begin
      if (haddr  != a_addr_q)  viol_now[V_ADDR_CHANGED]  = 1'b1;
      if (htrans != a_trans_q) viol_now[V_TRANS_CHANGED] = 1'b1;
      if (hwrite != a_write_q) viol_now[V_WRITE_CHANGED] = 1'b1;
    end
    if (a_active && (hsize > MAX_HSIZE[2:0]))
      viol_now[V_SIZE_ILLEGAL] = 1'b1;
    // BUSY is only legal from a master that already has a burst open.
    if ((htrans == T_BUSY) && !in_burst_q)
      viol_now[V_BUSY_NO_BURST] = 1'b1;
    // An ERROR response with nothing in flight has nothing to respond to.
    if (hresp && hready && !a_valid_q)
      viol_now[V_RESP_ON_IDLE] = 1'b1;
  end

  // First set bit of viol_now, for the FIRST-violation code.
  logic [3:0] viol_now_first;
  always_comb begin
    viol_now_first = 4'd0;
    for (int i = N_VIOL-1; i >= 0; i--)
      if (viol_now[i]) viol_now_first = i[3:0];
  end

  function automatic int unsigned bucket_of(input logic [31:0] w);
    if (w == 0)      return 0;
    else if (w < 4)  return 1;
    else if (w < 16) return 2;
    else             return 3;
  endfunction

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      enable_q     <= 1'b0;
      a_valid_q    <= 1'b0;
      a_addr_q     <= '0;
      a_trans_q    <= T_IDLE;
      a_write_q    <= 1'b0;
      wait_q       <= '0;
      in_burst_q   <= 1'b0;
      cnt_rd_q     <= '0;
      cnt_wr_q     <= '0;
      cnt_err_q    <= '0;
      cnt_wait_q   <= '0;
      cnt_idle_q   <= '0;
      max_wait_q   <= '0;
      viol_q       <= '0;
      viol_addr_q  <= '0;
      viol_first_q <= '0;
      viol_cnt_q   <= '0;
      viol_seen_q  <= 1'b0;
      for (int b = 0; b < HIST_BUCKETS; b++) hist_q[b] <= '0;
    end else begin
      // CSR-driven clear wins over accumulation in the same cycle, so a read
      // after clear can never show a stale count.
      if (clear_pulse) begin
        cnt_rd_q     <= '0;
        cnt_wr_q     <= '0;
        cnt_err_q    <= '0;
        cnt_wait_q   <= '0;
        cnt_idle_q   <= '0;
        max_wait_q   <= '0;
        viol_q       <= '0;
        viol_addr_q  <= '0;
        viol_first_q <= '0;
        viol_cnt_q   <= '0;
        viol_seen_q  <= 1'b0;
        for (int b = 0; b < HIST_BUCKETS; b++) hist_q[b] <= '0;
      end else if (enable_q && hresetn) begin
        // ---- violations -------------------------------------------------
        // OR in what fired, then apply the RW1C mask, so a clear racing a new
        // violation cannot lose the new one.
        viol_q <= (viol_q | viol_now) & ~viol_clr_mask;
        if (viol_now != '0) begin
          if (viol_cnt_q != 8'hFF) viol_cnt_q <= viol_cnt_q + 8'd1;
          if (!viol_seen_q) begin
            viol_seen_q  <= 1'b1;
            viol_first_q <= viol_now_first;
            // Address of the transfer being violated, not of the next one.
            viol_addr_q  <= a_valid_q ? a_addr_q : haddr;
          end
        end

        // ---- idle / wait accounting -------------------------------------
        if (htrans == T_IDLE && !a_valid_q)
          cnt_idle_q <= cnt_idle_q + 32'd1;
        if (a_valid_q && !hready) begin
          wait_q     <= wait_q + 32'd1;
          cnt_wait_q <= cnt_wait_q + 32'd1;
        end

        // ---- data phase completion --------------------------------------
        if (a_valid_q && hready) begin
          if (a_write_q) cnt_wr_q <= cnt_wr_q + 32'd1;
          else           cnt_rd_q <= cnt_rd_q + 32'd1;
          if (hresp)     cnt_err_q <= cnt_err_q + 32'd1;
          hist_q[bucket_of(wait_q)] <= hist_q[bucket_of(wait_q)] + 32'd1;
          if (wait_q > max_wait_q) max_wait_q <= wait_q;
          wait_q <= '0;
        end
      end else begin
        // Disabled, or the monitored bus in reset: the RW1C must still work, so a
        // stuck bit can be cleared without having to enable the monitor first.
        viol_q <= viol_q & ~viol_clr_mask;
      end

      // ---- CTRL. Owned HERE so enable_q has exactly one procedural driver.
      if (wr_ctrl) enable_q <= csr_data[0];

      // ---- address-phase shadow (runs whenever the bus does) -------------
      // Deliberately OUTSIDE the enable gate for `a_*`: the shadow is what makes
      // the checks meaningful, and letting it go stale while disabled would make
      // the first cycle after enable look like a violation.
      if (!hresetn) begin
        a_valid_q  <= 1'b0;
        a_trans_q  <= T_IDLE;
        wait_q     <= '0;
        in_burst_q <= 1'b0;
      end else if (hready) begin
        a_valid_q <= a_active;
        a_addr_q  <= haddr;
        a_trans_q <= htrans;
        a_write_q <= hwrite;
        // A burst opens on a NONSEQ with a burst type other than SINGLE and
        // closes when an IDLE/NONSEQ arrives.
        if (htrans == T_NONSEQ) in_burst_q <= (hburst != 3'b000);
        else if (htrans == T_IDLE) in_burst_q <= 1'b0;
      end
    end
  end

  // ---------------------------------------------------------------------
  // AXI4-Lite slave. Deliberately the simple two-phase form used by the other
  // blocks in this repo: accept AW+W together, answer B, then AR->R.
  // ---------------------------------------------------------------------
  // Write-channel HANDSHAKE only. The register effects of a write are decoded
  // combinationally above (csr_wr / wr_ctrl / clear_pulse / viol_clr_mask) and
  // applied in the state block, which is the only writer of enable_q and viol_q.
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      s_axi_awready <= 1'b0;
      s_axi_wready  <= 1'b0;
      s_axi_bvalid  <= 1'b0;
      s_axi_bresp   <= 2'b00;
    end else begin
      if (csr_wr) begin
        s_axi_awready <= 1'b1;
        s_axi_wready  <= 1'b1;
        s_axi_bvalid  <= 1'b1;
        s_axi_bresp   <= 2'b00;   // always OKAY: unknown offsets are ignored
      end else begin
        s_axi_awready <= 1'b0;
        s_axi_wready  <= 1'b0;
        if (s_axi_bvalid && s_axi_bready) s_axi_bvalid <= 1'b0;
      end
    end
  end

  logic [31:0] rd_mux;
  always_comb begin
    case (s_axi_araddr[7:0])
      A_CTRL:      rd_mux = {30'd0, 1'b0, enable_q};
      A_STATUS:    rd_mux = {29'd0, (viol_q != '0), a_valid_q, enable_q};
      A_CNT_RD:    rd_mux = cnt_rd_q;
      A_CNT_WR:    rd_mux = cnt_wr_q;
      A_CNT_ERR:   rd_mux = cnt_err_q;
      A_CNT_WAIT:  rd_mux = cnt_wait_q;
      A_CNT_IDLE:  rd_mux = cnt_idle_q;
      A_VIOL:      rd_mux = {{(32-N_VIOL){1'b0}}, viol_q};
      A_VIOL_ADDR: rd_mux = viol_addr_q;
      A_VIOL_INFO: rd_mux = {16'd0, viol_cnt_q, 4'd0, viol_first_q};
      A_HIST0:     rd_mux = hist_q[0];
      A_HIST1:     rd_mux = hist_q[1];
      A_HIST2:     rd_mux = hist_q[2];
      A_HIST3:     rd_mux = (HIST_BUCKETS > 3) ? hist_q[3] : 32'd0;
      A_MAX_WAIT:  rd_mux = max_wait_q;
      default:     rd_mux = 32'd0;
    endcase
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      s_axi_arready <= 1'b0;
      s_axi_rvalid  <= 1'b0;
      s_axi_rresp   <= 2'b00;
      s_axi_rdata   <= 32'd0;
    end else begin
      if (!s_axi_rvalid && s_axi_arvalid) begin
        s_axi_arready <= 1'b1;
        s_axi_rvalid  <= 1'b1;
        s_axi_rresp   <= 2'b00;
        s_axi_rdata   <= rd_mux;
      end else begin
        s_axi_arready <= 1'b0;
        if (s_axi_rvalid && s_axi_rready) s_axi_rvalid <= 1'b0;
      end
    end
  end

endmodule
