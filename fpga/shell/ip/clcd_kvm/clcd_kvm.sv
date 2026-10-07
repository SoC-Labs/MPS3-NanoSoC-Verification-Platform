// -----------------------------------------------------------------------------
// clcd_kvm.sv — the CLCD KVM: panel arbiter between the harness and the DUT.
//
// Implements the FROZEN contract in this directory's README.md (v1.0, W1) and
// docs/contracts/shell-regmap.md §CLCDKVM (v0.5 @ 0x44AD_0000). Read the README
// first: it is normative for the FSM, the quiescence definition, the CDC and the
// debounce, and this file deliberately does not restate its reasoning.
//
// One physical HX8347-D 8080 bus; two independent masters want it:
//   A  the harness   -- clcd_0 (fpga/shell/ip/clcd/clcd.sv), AXI-Lite @ 0x44AC,
//                       SAME clock domain (s_axi_aclk). No synchroniser.
//   B  the DUT       -- a student accelerator inside the nanosoc RM, reaching us
//                       over the TUNNEL on the spare upper 8 bits of
//                       dut_gpio_o/dut_gpio_oe, POST-decoupler, in the dut_clk
//                       domain (50 MHz shipped). ASYNC -> synchronised and
//                       stability-filtered here (§12 / the CDC section below).
//
// This is NOT a 2:1 mux. Three facts force real logic, and each maps to one part
// of this file:
//   1. The bus is STROBED -> the safe-switch gate (S_DRAIN + S_GRANT). Cutting
//      over mid-cycle truncates a WR pulse (a garbage byte into GRAM) or leaves
//      CS asserted across the seam (desyncing the panel's command/parameter
//      state machine). The gate is TWO-SIDED: gating only the OUTGOING source
//      still lets the pads jump into the middle of a cycle the INCOMING source
//      was already running -- it has no idea it is about to be granted.
//   2. The panel IS the framebuffer and its state is NOT shared -> every
//      handover hard-resets the panel (S_RST/S_SETTLE) and tells the new owner
//      to re-init and repaint (EVENT).
//   3. The DUT is in a RECONFIGURABLE PARTITION -> forced revert to the harness
//      whenever decouple_status is asserted or rp_resetn is low. That is the
//      reason this block lives in the STATIC shell and nowhere else.
//
// TUNNEL STROBES ARE CARRIED ACTIVE-HIGH and are inverted to the panel's
// active-low pads HERE, and nowhere else. That is a SAFETY property, not a style
// choice: it is what makes the decoupler's existing DECOUPLED_VALUE 0x0 clamp
// mean "all strobes idle, nothing requested, nothing in flight" by construction
// for the whole duration of every partial reconfiguration. The encoding, and the
// full argument for why inverting it re-introduces a silicon-only,
// panel-corrupting failure, are in docs/contracts/dut-display-tunnel.md §3. Do
// not "tidy this up".
//
// Static-side peripheral: it never crosses the RP boundary and takes no DFX
// decoupler entry of its own. All times are in MICROSECONDS, counted against a
// 1 us tick derived from s_axi_aclk (TICK_DIV = CLK_HZ / 1_000_000).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module clcd_kvm #(
  // Local offset decode only. The base (0x44AD_0000, 64 KiB page) is set in the
  // BD Address Editor. shell_bd.tcl instantiates every CSR block with
  // C_S_AXI_ADDR_WIDTH = 32 -- the RTL default of 12 is NEVER what ships (see
  // the "Decode width" section below).
  parameter int C_S_AXI_ADDR_WIDTH = 12,
  parameter int C_S_AXI_DATA_WIDTH = 32,

  // s_axi_aclk frequency, in Hz. Every time in this block is expressed in
  // MICROSECONDS, in both the RTL and the CSRs; this is the only place the clock
  // period enters. The shipped shell clock is clk_wiz_shell CLKOUT1 = 100 MHz
  // (shell_bd.tcl:199, :258) => TICK_DIV = 100. A bench may lower CLK_HZ to
  // shorten the tick; it must not change the units.
  parameter int CLK_HZ = 100_000_000,

  // Async-input synchroniser depth for the USER_nPB1 pad. Three, not two: it is
  // a true asynchronous, slow-edged, HUMAN-driven pad with no upstream register,
  // and the extra stage costs 1 FF (README §11).
  parameter int PB_SYNC_STAGES = 3,

  // CSR reset values, in MICROSECONDS (README §5).
  parameter int RST_US_INIT      = 2000,   // PANEL_TMR.rst_us     -- CLCD_RST low
  parameter int SETTLE_US_INIT   = 5000,   // PANEL_TMR.settle_us  -- post-reset idle
  parameter int TIMEOUT_US_INIT  = 1000,   // TIMEOUT.timeout_us   -- hung-owner
  parameter int DEBOUNCE_US_INIT = 10000   // DEBOUNCE.debounce_us -- PB1
) (
  input  logic                            s_axi_aclk,     // shell clock, 100 MHz
  input  logic                            s_axi_aresetn,  // shell reset, active-low

  // ==== AXI4-Lite slave -- MicroBlaze data bus @ 0x44AD_0000 =================
  input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_awaddr,
  input  logic [2:0]                      s_axi_awprot,
  input  logic                            s_axi_awvalid,
  output logic                            s_axi_awready,
  input  logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_wdata,
  input  logic [C_S_AXI_DATA_WIDTH/8-1:0] s_axi_wstrb,
  input  logic                            s_axi_wvalid,
  output logic                            s_axi_wready,
  output logic [1:0]                      s_axi_bresp,
  output logic                            s_axi_bvalid,
  input  logic                            s_axi_bready,
  input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_araddr,
  input  logic [2:0]                      s_axi_arprot,
  input  logic                            s_axi_arvalid,
  output logic                            s_axi_arready,
  output logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_rdata,
  output logic [1:0]                      s_axi_rresp,
  output logic                            s_axi_rvalid,
  input  logic                            s_axi_rready,

  // ==== SOURCE A -- the harness CLCD block (clcd_0) ==========================
  // Same clock domain. These are clcd_0's existing pad outputs, re-routed:
  // clcd_0 no longer reaches shell_top's IOBUFs, it feeds us.
  input  logic [7:0]                      h_pd_o,
  input  logic                            h_pd_oe,       // const 1 at READ_PATH=0
  input  logic                            h_cs_n,        // ACTIVE-LOW
  input  logic                            h_wr_n,        // ACTIVE-LOW
  input  logic                            h_rd_n,        // ACTIVE-LOW; const 1 at READ_PATH=0
  input  logic                            h_rs,          // 0=cmd, 1=data
  input  logic                            h_bl,          // ACTIVE-HIGH
  input  logic                            h_rst_n,       // ACTIVE-LOW
  input  logic                            h_busy,        // clcd_0.busy_o
  input  logic                            h_fifo_empty,  // clcd_0.fifo_empty_o
  output logic [7:0]                      h_pd_i,        // -> clcd_0.clcd_pd_i

  // ==== SOURCE B -- the DUT tunnel, POST-DECOUPLER ===========================
  // ASYNC (dut_clk). Encoding FROZEN in docs/contracts/dut-display-tunnel.md §2.
  // TAP, DO NOT STEAL: board_gpio_0 keeps its own connection to these same nets
  // and bits [7:0] still drive the LEDs.
  input  logic [15:0]                     dut_gpio_o_i,  // [15:8] = PD[7:0]
  input  logic [15:0]                     dut_gpio_oe_i, // [15:8] = control/status

  // ==== DFX interlock ========================================================
  input  logic                            decouple_status, // 1 = RP boundary CLAMPED
  input  logic                            rp_resetn,       // 0 = RP HELD IN RESET

  // ==== The button -- RAW ASYNC PAD, ACTIVE-LOW (MPS3 AT32) ==================
  // Not debounced upstream, not synchronised upstream. Both happen here, in
  // HARDWARE, so the switch still works when the harness firmware is wedged.
  input  logic                            user_npb1,

  // ==== Panel pads -- to shell_top's IOBUFs ==================================
  output logic [7:0]                      clcd_pd_o,
  input  logic [7:0]                      clcd_pd_i,
  output logic                            clcd_pd_oe,    // 1 = drive pads
  output logic                            clcd_cs_n_o,   // CLCD_CS     (AP15, ACTIVE-LOW)
  output logic                            clcd_wr_n_o,   // CLCD_WR_SCL (AP14, ACTIVE-LOW)
  output logic                            clcd_rd_n_o,   // CLCD_RD     (AM15, ACTIVE-LOW)
  output logic                            clcd_rs_o,     // CLCD_RS     (AN14, 0=cmd 1=data)
  output logic                            clcd_bl_o,     // CLCD_BL     (AJ16, ACTIVE-HIGH)
  output logic                            clcd_rst_n_o,  // CLCD_RST    (AK18, ACTIVE-LOW)

  // ==== Status tap ===========================================================
  output logic                            owner_o        // 0 = HARNESS, 1 = DUT
);

  localparam logic OWNER_HARNESS = 1'b0;
  localparam logic OWNER_DUT     = 1'b1;

  // ===========================================================================
  // AXI4-Lite slave -- standard Xilinx-template write/read channel FSM,
  // identical in structure to clcd.sv / dfx_ctl.sv / telem.sv (shell-regmap.md
  // "AXI4-Lite slave conventions", style 1). Single in-flight transaction;
  // awready pulses one cycle then bvalid asserts. It NEVER stalls -- nothing in
  // this block can back-pressure the bus.
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
  assign s_axi_bresp   = 2'b00;   // OKAY always -- the contract defines no error cases
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
  // Address decode -- the block's own 64 KiB page, NOT C_S_AXI_ADDR_WIDTH.
  //
  // shell_bd.tcl sets CONFIG.C_S_AXI_ADDR_WIDTH {32} on every CSR block, so the
  // interconnect hands this slave the FULL system address (0x44AD_xxxx, not
  // 0x0). Decoding addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] would compare the full
  // address against 'h0 and match nothing -- that exact bug killed every CSR
  // register on silicon at once. Decode the 64 KiB page assign_bd_address gives
  // these slaves (the dfx_ctl.sv:229-258 / clcd.sv:215-216 idiom, verbatim);
  // decoding fewer bits would let BASE+0x1000 alias offset 0. Only the seven
  // mapped offsets respond; every other offset in the page is unmapped (reads 0,
  // writes accepted with no effect, BRESP=OKAY). Guarded so the block still
  // elaborates at the 12-bit default. tests/csr_decode_width/ elaborates at
  // width 32 and drives base+offset.
  // ===========================================================================
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W        = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_CTRL      = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_STATUS    = 'h1;  // 0x04 (ro)
  localparam logic [IDX_W-1:0] IDX_EVENT     = 'h2;  // 0x08 (RW1C)
  localparam logic [IDX_W-1:0] IDX_PANEL_TMR = 'h3;  // 0x0C
  localparam logic [IDX_W-1:0] IDX_TIMEOUT   = 'h4;  // 0x10
  localparam logic [IDX_W-1:0] IDX_DEBOUNCE  = 'h5;  // 0x14
  localparam logic [IDX_W-1:0] IDX_TUNNEL    = 'h6;  // 0x18 (ro)

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  // ===========================================================================
  // 1 us timebase. TICK_DIV = CLK_HZ / 1_000_000 (= 100 at the shipped 100 MHz).
  // Every microsecond field in this block counts `tick` pulses; a field written
  // as 0 is floored to ONE tick (us_load below, the clcd.sv:363-365 phase_load
  // idiom) -- no field may degenerate to zero time.
  // ===========================================================================
  localparam int TICK_DIV = (CLK_HZ < 1_000_000) ? 1 : (CLK_HZ / 1_000_000);
  localparam int TICK_W   = (TICK_DIV <= 1) ? 1 : $clog2(TICK_DIV);

  logic [TICK_W-1:0] tick_cnt_q;
  wire               tick = (tick_cnt_q == '0);

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      tick_cnt_q <= TICK_W'(TICK_DIV - 1);
    end else begin
      tick_cnt_q <= tick ? TICK_W'(TICK_DIV - 1) : (tick_cnt_q - 1'b1);
    end
  end

  function automatic logic [31:0] us_load(input logic [31:0] v);
    us_load = (v == 32'd0) ? 32'd0 : (v - 32'd1);
  endfunction

  // ===========================================================================
  // CDC -- the tunnel is ASYNCHRONOUS (README §12; dut-display-tunnel.md §5).
  //
  // dut_gpio_o/oe arrive from the RP in dut_clk (50 MHz shipped); we run in
  // s_axi_aclk (100 MHz). board_gpio gets away with a purely combinational mux
  // because it only DRIVES A PAD; we DECODE A STROBED PROTOCOL, and per-bit
  // synchroniser skew would tear the 8080 vector apart -- WR could assert at the
  // pads one cycle before PD settled, latching a garbage byte into GRAM.
  //
  // So: 2-FF synchronise all 32 bits, then STABILITY-FILTER them -- the
  // pad-facing registers update only when the synchronised vector has been
  // IDENTICAL on two consecutive s_axi_aclk cycles (sync2 == sync3). A
  // half-updated DUT vector can therefore never reach the panel. Cost: <=2 shell
  // cycles (20 ns) of latency and 32 FFs. The DUT pays for this with a timing
  // floor (>=8 dut_clk cycles per 8080 phase, >=4 of PD/RS guard either side of
  // wr) -- 8x and 4x the filter latency at the shipped 50 MHz.
  //
  // decouple_status / rp_resetn are shell-domain nets, but they are synchronised
  // anyway so the block is safe to instantiate from any clock and a bench can
  // wiggle them freely.
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [31:0] tun_sync1_q, tun_sync2_q;
  logic [31:0]                          tun_sync3_q;
  logic [31:0]                          tun_f_q;      // filtered: {gpio_oe, gpio_o}

  (* ASYNC_REG = "TRUE" *) logic [1:0]  decouple_sync_q;
  (* ASYNC_REG = "TRUE" *) logic [1:0]  rp_resetn_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      tun_sync1_q      <= '0;
      tun_sync2_q      <= '0;
      tun_sync3_q      <= '0;
      tun_f_q          <= '0;
      decouple_sync_q  <= 2'b00;
      rp_resetn_sync_q <= 2'b00;   // reset as "RP IN RESET": fail safe
    end else begin
      tun_sync1_q      <= {dut_gpio_oe_i, dut_gpio_o_i};
      tun_sync2_q      <= tun_sync1_q;
      tun_sync3_q      <= tun_sync2_q;
      if (tun_sync2_q == tun_sync3_q) tun_f_q <= tun_sync2_q;
      decouple_sync_q  <= {decouple_sync_q[0],  decouple_status};
      rp_resetn_sync_q <= {rp_resetn_sync_q[0], rp_resetn};
    end
  end

  // Tunnel field extraction. Bit map FROZEN in docs/contracts/dut-display-tunnel.md
  // §2 -- do not restate it elsewhere without citing that file. tun_f_q[15:0] is
  // dut_gpio_o, tun_f_q[31:16] is dut_gpio_oe.
  wire [7:0] dut_pd_f    = tun_f_q[15:8];    // gpio_o [15:8] = PD[7:0]
  wire       dut_cs_f    = tun_f_q[24];      // gpio_oe[8]  cs    ACTIVE-HIGH
  wire       dut_wr_f    = tun_f_q[25];      // gpio_oe[9]  wr    ACTIVE-HIGH
  wire       dut_rs_f    = tun_f_q[26];      // gpio_oe[10] rs    0=cmd 1=data
  wire       dut_busy_f  = tun_f_q[29];      // gpio_oe[13] busy  ACTIVE-HIGH
  wire       dut_req_f   = tun_f_q[30];      // gpio_oe[14] req   ACTIVE-HIGH
  // gpio_oe[11] rd, [12] pd_oe and [15] spare are RESERVED in v1: the DUT drives
  // them 0 and we IGNORE them (the harness owns the read path; §10 of the
  // README). They are still visible to the host in TUNNEL @ 0x18.

  wire decoupled    = decouple_sync_q[1];
  wire rp_in_reset  = ~rp_resetn_sync_q[1];

  // ===========================================================================
  // USER_nPB1 -- synchroniser + integrating debounce + press edge (README §11).
  // ALL HARDWARE: it must work with the MicroBlaze halted, the harness firmware
  // wedged or the DUT hung. CTRL.pb_en (reset 1) is the only thing that can
  // disable it.
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [PB_SYNC_STAGES-1:0] pb_sync_q;

  logic        pb_level_q;    // debounced, PRESSED sense (1 = pressed)
  logic        pb_level_d1_q; // for the press edge
  logic [15:0] pb_cnt_q;

  // The pad is ACTIVE-LOW (pressed = 0); everything downstream of here is in the
  // pressed sense.
  wire pb_raw = ~pb_sync_q[PB_SYNC_STAGES-1];

  logic [15:0] debounce_us_q;
  wire  [15:0] pb_load = (debounce_us_q == 16'd0) ? 16'd0 : (debounce_us_q - 16'd1);

  // pb_level (and the edge detector's delayed copy below) RESET TO "PRESSED".
  // A button HELD through reset must produce NO press edge when reset
  // releases: the D13 boot hook reads "PB1 held at power-up" as "skip the
  // default overlay load", and that same hold must never also flip the panel
  // to the DUT. Reset to "released" (as this was until 2026-09-23) turned a
  // held button into a press on the first 1 us tick after s_axi_aresetn rose
  // -- and a WDOG reset is an s_axi_aresetn too.
  //   * held at reset: once the synchroniser has filled (PB_SYNC_STAGES
  //     cycles), pb_raw == pb_level == 1 and the counter only reloads -- no
  //     edge until a real release and then a real press.
  //   * not held: pb_raw != pb_level from the first cycle, and the first tick
  //     (pb_cnt_q resets to 0) accepts it: pb_level FALLS ~1 us after reset --
  //     a release, never an event. STATUS.pb_level reads "pressed" until then,
  //     and a press made inside that first microsecond is folded into it.
  // ASSUMES TICK_DIV > PB_SYNC_STAGES (100 > 3 at the shipped 100 MHz): the
  // first tick must land after the synchroniser has filled, or a held button
  // reads "released" on it for one tick and then presses. Any CLK_HZ >= 4 MHz.
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      pb_sync_q  <= '1;          // unpressed (pad idles HIGH)
      pb_level_q <= 1'b1;        // PRESSED -- see the block comment
      pb_cnt_q   <= '0;
    end else begin
      pb_sync_q <= {pb_sync_q[PB_SYNC_STAGES-2:0], user_npb1};
      // Integrate, don't just delay: the counter reloads whenever the raw level
      // agrees with the accepted level, and only a NEW level that holds unbroken
      // for the whole debounce time is accepted. Any bounce back restarts it.
      if (pb_raw == pb_level_q) begin
        pb_cnt_q <= pb_load;
      end else if (tick) begin
        if (pb_cnt_q == 16'd0) begin
          pb_level_q <= pb_raw;
          pb_cnt_q   <= pb_load;
        end else begin
          pb_cnt_q <= pb_cnt_q - 16'd1;
        end
      end
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) pb_level_d1_q <= 1'b1;   // == pb_level_q's reset: no edge out of reset
    else                pb_level_d1_q <= pb_level_q;
  end

  // PRESS only (the pad falling, i.e. the button going down). Release does
  // nothing -- one toggle per press, never two.
  wire pb_press = pb_level_q && !pb_level_d1_q;

  // ===========================================================================
  // CTRL (0x00) register file. src_sel (CTRL[0]) is NOT stored here: it reads
  // and writes tgt_owner_q, which lives with the ownership FSM below.
  //
  // Byte strobes are honoured (the clcd.sv:263-274 idiom): wstrb[0] gates [7:0],
  // wstrb[1] gates [15:8], wstrb[2] gates [23:16]. So a write with wstrb[0]=0
  // can never change src_sel/backlight/panel_rst_n...
  // ===========================================================================
  logic force_harness_q;  // CTRL[1]
  logic timeout_en_q;     // CTRL[2], reset 1
  logic pb_en_q;          // CTRL[3], reset 1
  logic dut_req_en_q;     // CTRL[4], reset 0
  logic backlight_q;      // CTRL[5]
  logic panel_rst_n_q;    // CTRL[6]
  logic bl_rst_src_q;     // CTRL[7], reset 0 = follow clcd_0 (drop-in compatible)

  logic [15:0] rst_us_q, settle_us_q;   // PANEL_TMR
  logic [31:0] timeout_us_q;            // TIMEOUT

  wire ctrl_wr = slv_reg_wren && (waddr_idx == IDX_CTRL);

  // The three W1P bits. None is stored; all read back 0.
  wire panel_rst_pulse_w = ctrl_wr && s_axi_wstrb[1] && s_axi_wdata[8];
  wire force_switch_w    = ctrl_wr && s_axi_wstrb[1] && s_axi_wdata[9];
  // src_sel_we (CTRL[16], byte 2) is the write-enable for src_sel (CTRL[0],
  // byte 0), so BOTH byte strobes must be present. Without this gate any
  // read-modify-write of CTRL -- e.g. to set the backlight -- would silently
  // clobber an ownership change made by a concurrent USER_nPB1 press. With it, a
  // plain RMW of CTRL never touches ownership.
  wire src_sel_we_w      = ctrl_wr && s_axi_wstrb[2] && s_axi_wdata[16]
                                   && s_axi_wstrb[0];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      force_harness_q <= 1'b0;
      timeout_en_q    <= 1'b1;   // a KVM must not be able to get stuck on a dead input
      pb_en_q         <= 1'b1;   // press the button and it works, with no firmware at all
      dut_req_en_q    <= 1'b0;   // an unprovisioned RM must not grab the panel at power-on
      backlight_q     <= 1'b0;
      panel_rst_n_q   <= 1'b0;   // panel held in reset, matching clcd.sv's CTRL reset
      bl_rst_src_q    <= 1'b0;   // BL/RST follow clcd_0 -> the shipped firmware still lights it
      rst_us_q        <= 16'(RST_US_INIT);
      settle_us_q     <= 16'(SETTLE_US_INIT);
      timeout_us_q    <= 32'(TIMEOUT_US_INIT);
      debounce_us_q   <= 16'(DEBOUNCE_US_INIT);
    end else if (slv_reg_wren) begin
      case (waddr_idx)
        IDX_CTRL: if (s_axi_wstrb[0]) begin
          // wdata[0] (src_sel) is handled by the ownership FSM (it needs [16]
          // too); wdata[8]/[9]/[16] are W1P pulses and are never stored.
          force_harness_q <= s_axi_wdata[1];
          timeout_en_q    <= s_axi_wdata[2];
          pb_en_q         <= s_axi_wdata[3];
          dut_req_en_q    <= s_axi_wdata[4];
          backlight_q     <= s_axi_wdata[5];
          panel_rst_n_q   <= s_axi_wdata[6];
          bl_rst_src_q    <= s_axi_wdata[7];
        end
        IDX_PANEL_TMR: begin
          if (s_axi_wstrb[0]) rst_us_q[7:0]     <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1]) rst_us_q[15:8]    <= s_axi_wdata[15:8];
          if (s_axi_wstrb[2]) settle_us_q[7:0]  <= s_axi_wdata[23:16];
          if (s_axi_wstrb[3]) settle_us_q[15:8] <= s_axi_wdata[31:24];
        end
        IDX_TIMEOUT: begin
          if (s_axi_wstrb[0]) timeout_us_q[7:0]   <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1]) timeout_us_q[15:8]  <= s_axi_wdata[15:8];
          if (s_axi_wstrb[2]) timeout_us_q[23:16] <= s_axi_wdata[23:16];
          if (s_axi_wstrb[3]) timeout_us_q[31:24] <= s_axi_wdata[31:24];
        end
        IDX_DEBOUNCE: begin
          if (s_axi_wstrb[0]) debounce_us_q[7:0]  <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1]) debounce_us_q[15:8] <= s_axi_wdata[15:8];
        end
        default: ; // STATUS/TUNNEL read-only; EVENT is W1C below; unmapped
                   // offsets accepted-no-effect (BRESP=OKAY).
      endcase
    end
  end

  // ===========================================================================
  // The safe-switch gate -- QUIESCENCE (README §6). One definition, two
  // instances: a source is quiescent when it has nothing in flight and nothing
  // queued, and its CS is deasserted.
  //
  //   HARNESS: !busy <=> clcd's FSM is in ST_IDLE <=> cs_n is HIGH (CS is
  //            asserted only in the three non-IDLE states). fifo_empty
  //            additionally guarantees no QUEUED bytes: bytes stranded in the
  //            FIFO across a handover would be emitted on the way BACK, out of
  //            sequence, into a panel that has since been reset.
  //   DUT:     the tunnel's own busy bit means "any byte in flight OR queued" --
  //            the exact analogue. Because the tunnel is active-high, a clamped
  //            or absent RP reads as QUIESCENT by construction, so this gate can
  //            never hang on a dead RP.
  // ===========================================================================
  wire harness_quiet = h_fifo_empty && !h_busy;
  wire dut_quiet     = !dut_cs_f && !dut_busy_f;

  // ===========================================================================
  // The DFX interlock (README §9) -- the reason this block is in the STATIC
  // shell. While it is asserted the DUT cannot be granted the panel and cannot
  // keep it, and S_DRAIN does not wait for a clamped or reconfiguring RP to
  // declare itself quiescent.
  //
  // The decoupler's 0x0 clamp on the active-high tunnel is a SECOND, independent
  // line of defence (dut-display-tunnel.md §3) and BOTH are load-bearing:
  // decouple_status also covers the window where the RP is being RESET but not
  // yet clamped.
  // ===========================================================================
  wire interlock = decoupled | rp_in_reset | force_harness_q;

  // ===========================================================================
  // Ownership FSM (README §7 -- NORMATIVE).
  //
  // TWO registers carry ownership and they must stay distinct; most bugs here
  // come from merging them.
  //   tgt_owner_q -- the REQUESTED owner.
  //   owner_q     -- the COMMITTED owner (what reaches the pads).
  //   switch_pending == (tgt_owner_q != owner_q). DERIVED, never a flag.
  // ===========================================================================
  typedef enum logic [2:0] {
    S_OWN    = 3'd0,
    S_DRAIN  = 3'd1,
    S_RST    = 3'd2,
    S_SETTLE = 3'd3,
    S_GRANT  = 3'd4
  } state_e;

  state_e      state_q, state_d;
  logic        owner_q, tgt_owner_q;
  logic [31:0] phase_q;
  logic        phase_ld;
  logic [31:0] phase_ld_val;
  logic        dut_req_d1_q;
  logic        force_switch_q;    // latched CTRL.force_switch, consumed in S_DRAIN
  logic        panel_rst_req_q;   // latched CTRL.panel_rst_pulse, consumed in S_OWN

  wire switch_pending = (tgt_owner_q != owner_q);

  wire cur_owner_quiet = (owner_q     == OWNER_HARNESS) ? harness_quiet : dut_quiet;
  wire tgt_owner_quiet = (tgt_owner_q == OWNER_HARNESS) ? harness_quiet : dut_quiet;

  // The shared phase counter. S_DRAIN/S_GRANT time the hung-owner TIMEOUT;
  // S_RST/S_SETTLE time the panel-reset pulse and the post-reset settle. No two
  // of them ever run at once, so one counter does the lot.
  wire phase_expired = tick && (phase_q == 32'd0);
  wire timeout_hit   = timeout_en_q && phase_expired;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      phase_q <= 32'd0;
    end else if (phase_ld) begin
      phase_q <= phase_ld_val;
    end else if (tick && (phase_q != 32'd0)) begin
      phase_q <= phase_q - 32'd1;
    end
  end

  // -- next-state (README §7's state table, verbatim) -------------------------
  always_comb begin
    state_d      = state_q;
    phase_ld     = 1'b0;
    phase_ld_val = 32'd0;

    case (state_q)
      S_OWN: begin
        if (switch_pending) begin
          state_d      = S_DRAIN;
          phase_ld     = 1'b1;
          phase_ld_val = us_load(timeout_us_q);
        end else if (panel_rst_req_q) begin
          // A plain CTRL.panel_rst_pulse: the SAME sequencer, with no owner
          // change. One sequencer, two callers.
          state_d      = S_RST;
          phase_ld     = 1'b1;
          phase_ld_val = us_load({16'd0, rst_us_q});
        end
      end

      S_DRAIN: begin
        // The OUTGOING gate. The current owner still drives the pads: do not
        // stop driving it until it is quiescent, or you truncate ITS cycle.
        if (!switch_pending) begin
          state_d = S_OWN;                  // a second press cancelled the switch
        end else if (cur_owner_quiet || force_switch_q || timeout_hit ||
                     (interlock && (owner_q == OWNER_DUT))) begin
          // interlock: do NOT wait for a clamped or reconfiguring RP to declare
          // itself quiescent -- that is exactly the wrong thing to wait for.
          state_d      = S_RST;
          phase_ld     = 1'b1;
          phase_ld_val = us_load({16'd0, rst_us_q});
        end
      end

      S_RST: begin
        // The KVM drives the pads (idle pattern). CLCD_RST forced LOW, and
        // CLCD_BL forced off with it.
        if (phase_expired) begin
          state_d      = S_SETTLE;
          phase_ld     = 1'b1;
          phase_ld_val = us_load({16'd0, settle_us_q});
        end
      end

      S_SETTLE: begin
        // CLCD_RST released; pads still held idle by the KVM while the panel's
        // power-on sequence runs.
        if (phase_expired) begin
          state_d      = S_GRANT;
          phase_ld     = 1'b1;
          phase_ld_val = us_load(timeout_us_q);
        end
      end

      S_GRANT: begin
        // The INCOMING gate, and the half that a one-sided design gets wrong: do
        // not START driving the new owner until IT is quiescent either, or the
        // pads jump into the middle of a cycle it was already running (it has no
        // idea it is about to be granted) -- CS would assert mid-strobe with no
        // setup.
        if (tgt_owner_quiet || timeout_hit) state_d = S_OWN;
      end

      default: state_d = S_OWN;
    endcase
  end

  wire commit = (state_q == S_GRANT) && (state_d == S_OWN);

  // -- request sources -> tgt_owner_q (PRIORITY, highest first -- normative) --
  // Two sources can fire in the same cycle; this if/else chain IS the priority.
  wire dut_req_rise = dut_req_f && !dut_req_d1_q;
  wire dut_req_fall = !dut_req_f && dut_req_d1_q;

  // A press is ACCEPTED, and logs EVENT.pb_toggle, only when it actually toggles
  // the target: pb_en set, not masked by the interlock, and not preempted by a
  // same-cycle CSR ownership write (priority 2 beats the button, priority 3).
  // A masked or preempted press is not a toggle, so it is not an event.
  wire pb_accepted  = pb_press && pb_en_q && !interlock && !src_sel_we_w;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      tgt_owner_q  <= OWNER_HARNESS;
      dut_req_d1_q <= 1'b0;
    end else begin
      dut_req_d1_q <= dut_req_f;

      if (interlock) begin
        // 1. The interlock. Forces HARNESS and MASKS every DUT-requesting source
        //    below it.
        tgt_owner_q <= OWNER_HARNESS;
      end else if (src_sel_we_w) begin
        // 2. The CSR.
        tgt_owner_q <= s_axi_wdata[0];
      end else if (pb_press && pb_en_q) begin
        // 3. The button. It toggles the TARGET, not the owner -- so a second
        //    press during a switch-in-flight cancels it and you end up where you
        //    started. Least-surprising behaviour.
        tgt_owner_q <= ~tgt_owner_q;
      end else if (dut_req_en_q && (dut_req_rise || dut_req_fall)) begin
        // 4. The DUT's req bit, as an EDGE, not a level: a level would let a
        //    stuck-high req veto every attempt to take the panel away with the
        //    button. Rising = "I want the panel", falling = "you can have it
        //    back".
        tgt_owner_q <= dut_req_rise ? OWNER_DUT : OWNER_HARNESS;
      end
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      state_q <= S_OWN;
      owner_q <= OWNER_HARNESS;
    end else begin
      state_q <= state_d;
      if (commit) owner_q <= tgt_owner_q;
    end
  end

  assign owner_o = owner_q;

  // -- the two latched W1P actions --------------------------------------------
  // force_switch: latched so it works whether firmware writes it before, with,
  // or after the ownership request. Consumed on the S_DRAIN exit; dropped if it
  // was armed with nothing pending (otherwise it would silently break the NEXT
  // drain gate -- a nasty, delayed footgun).
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      force_switch_q <= 1'b0;
    end else if (force_switch_w) begin
      force_switch_q <= 1'b1;
    end else if (state_q == S_DRAIN) begin
      if (state_d != S_DRAIN) force_switch_q <= 1'b0;
    end else if ((state_q == S_OWN) && !switch_pending) begin
      force_switch_q <= 1'b0;
    end
  end

  // panel_rst_pulse: latched, and satisfied by ANY panel reset -- including one
  // that a handover was going to do anyway.
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      panel_rst_req_q <= 1'b0;
    end else if (panel_rst_pulse_w) begin
      panel_rst_req_q <= 1'b1;
    end else if (state_d == S_RST) begin
      panel_rst_req_q <= 1'b0;
    end
  end

  // ===========================================================================
  // EVENT (0x08) -- sticky handover events. RW1C: write 1 to clear, write 0 to
  // leave. READS DO NOT CLEAR -- csr_decode_width sweeps every offset in the page
  // and the platform dumps whole CSR pages over SWD/XVC, so a read-to-clear here
  // would silently destroy handover events on every debugger attach.
  //
  // SET is dominant over CLEAR in the same cycle: an event that lands exactly as
  // firmware acknowledges the previous one is never lost.
  // ===========================================================================
  localparam int EV_HARNESS_GAINED   = 0;
  localparam int EV_HARNESS_LOST     = 1;
  localparam int EV_DUT_GAINED       = 2;
  localparam int EV_DUT_LOST         = 3;
  localparam int EV_TIMEOUT_FIRED    = 4;
  localparam int EV_FORCED_REVERT    = 5;
  localparam int EV_PB_TOGGLE        = 6;
  localparam int EV_PANEL_RESET_DONE = 7;

  logic [7:0] event_q;
  logic [7:0] event_set;

  wire [7:0] event_clr = (slv_reg_wren && (waddr_idx == IDX_EVENT) && s_axi_wstrb[0])
                         ? s_axi_wdata[7:0] : 8'h00;

  // A real handover is one that CHANGES the owner; a panel_rst_pulse commits
  // through the same S_GRANT exit with tgt == owner, so it fires
  // panel_reset_done and nothing else. That asymmetry is exactly why the
  // firmware rule is `harness_gained | panel_reset_done` and not
  // `harness_gained` alone: an interlock firing during S_SETTLE of a
  // HARNESS->DUT switch flips tgt back to HARNESS mid-sequence, so the harness
  // gets a freshly-reset panel with NO harness_gained -- it never actually lost
  // it.
  always_comb begin
    event_set = 8'h00;
    if (commit) begin
      event_set[EV_PANEL_RESET_DONE] = 1'b1;        // ALWAYS -- the panel WAS reset
      if (tgt_owner_q != owner_q) begin
        event_set[EV_DUT_GAINED]     = (tgt_owner_q == OWNER_DUT);
        event_set[EV_HARNESS_LOST]   = (tgt_owner_q == OWNER_DUT);
        event_set[EV_HARNESS_GAINED] = (tgt_owner_q == OWNER_HARNESS);
        event_set[EV_DUT_LOST]       = (tgt_owner_q == OWNER_HARNESS);
      end
    end
    // A wait that was PREEMPTED, on either side of the gate.
    if (((state_q == S_DRAIN) || (state_q == S_GRANT)) && timeout_hit) begin
      event_set[EV_TIMEOUT_FIRED] = 1'b1;
    end
    // The interlock actually took the panel away from (or denied it to) the DUT.
    if (interlock && ((owner_q == OWNER_DUT) || (tgt_owner_q == OWNER_DUT))) begin
      event_set[EV_FORCED_REVERT] = 1'b1;
    end
    if (pb_accepted) event_set[EV_PB_TOGGLE] = 1'b1;
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) event_q <= 8'h00;
    else                event_q <= (event_q & ~event_clr) | event_set;
  end

  // ===========================================================================
  // The pad mux (README §8). The board_gpio.sv:348-349 shape -- pure
  // combinational, no added latency, one selector for the whole group. Between
  // the two gates the KVM drives the pads ITSELF (the idle pattern), so no
  // source can reach the panel while a switch is in flight.
  //
  // Note the INVERSION on the DUT side: the tunnel carries strobes ACTIVE-HIGH
  // and the pads are ACTIVE-LOW. That inversion lives here and nowhere else --
  // docs/contracts/dut-display-tunnel.md §3 for why it is a safety property.
  //
  // The current owner drives in S_OWN AND S_DRAIN: S_DRAIN is the OUTGOING gate,
  // and its entire purpose is to keep letting the owner finish its cycle until
  // it is quiescent (README §6: "do not stop driving the current owner until it
  // is quiescent, or you truncate its cycle"). Only in S_RST/S_SETTLE/S_GRANT
  // does the KVM itself drive the idle pattern -- which is exactly STATUS[7]
  // kvm_drives_pads = FSM ∈ {S_RST,S_SETTLE,S_GRANT}. (README §8's one-line
  // formula lists S_OWN only; that is inconsistent with §6, §7's state table
  // and the STATUS[7] definition, all of which require S_DRAIN here. The
  // safety-correct reading -- owner drives through the drain -- is implemented;
  // see this agent's report.)
  // ===========================================================================
  wire owner_holds    = (state_q == S_OWN) || (state_q == S_DRAIN);
  wire harness_drives = owner_holds && (owner_q == OWNER_HARNESS);
  wire dut_drives     = owner_holds && (owner_q == OWNER_DUT);
  wire kvm_drives     = !harness_drives && !dut_drives;

  assign clcd_cs_n_o = harness_drives ? h_cs_n : (dut_drives ? ~dut_cs_f : 1'b1);
  assign clcd_wr_n_o = harness_drives ? h_wr_n : (dut_drives ? ~dut_wr_f : 1'b1);
  assign clcd_rs_o   = harness_drives ? h_rs   : (dut_drives ?  dut_rs_f : 1'b0);
  assign clcd_pd_o   = harness_drives ? h_pd_o : (dut_drives ?  dut_pd_f : 8'h00);

  // rd_n / pd_oe pass through ONLY while the harness owns the pads, and are 1/1
  // in every other case. Under the shipped READ_PATH=0 both of clcd_0's are
  // constant 1, so v1 is bit-identical to today -- but a future READ_PATH=1
  // harness read cycle then works with NO port change, and h_pd_i already
  // forwards the captured byte back. The DUT's rd and pd_oe tunnel bits are
  // RESERVED and IGNORED in v1 (the DUT drives them 0).
  //
  // clcd_pd_oe is therefore held 1 at ALL times in v1, in every state and under
  // either owner: never float the bus toward the panel.
  assign clcd_rd_n_o = harness_drives ? h_rd_n  : 1'b1;
  assign clcd_pd_oe  = harness_drives ? h_pd_oe : 1'b1;

  assign h_pd_i = clcd_pd_i;

  // ===========================================================================
  // CLCD_BL / CLCD_RST (README §10). THE DUT CAN NEVER DRIVE EITHER -- there are
  // no BL/RST bits in the tunnel and there never will be. That is what makes a
  // hung or garbage DUT always recoverable.
  //
  // bl_rst_src selects the STEADY-STATE source only. Reset 0 = follow clcd_0's
  // CTRL[1]/CTRL[2], i.e. exactly today's behaviour, so the SHIPPED FIRMWARE
  // STILL LIGHTS THE PANEL on the new bitstream with no change. (A bitstream that
  // bricks the display for every previously-working ELF is not acceptable, and
  // that is what a literal "KVM registers only, reset 0" reading would have
  // produced.) The auto-reset sequencer ALWAYS wins, in either mode -- it is the
  // recovery lever, and it is pure hardware.
  // ===========================================================================
  wire bl_src    = bl_rst_src_q ? backlight_q   : h_bl;
  wire rst_n_src = bl_rst_src_q ? panel_rst_n_q : h_rst_n;

  assign clcd_rst_n_o = rst_n_src & ~(state_q == S_RST);
  assign clcd_bl_o    = bl_src    &  clcd_rst_n_o;   // BL forced off whenever the
                                                     // panel is in reset, from ANY cause

  // ===========================================================================
  // Read data mux (shell-regmap.md §CLCDKVM). PURELY combinational-from-registers:
  // reading ANY offset -- EVENT and TUNNEL included -- has NO side effect. Every
  // action in this block is armed by a WRITE (panel_rst_pulse, force_switch,
  // src_sel_we, the EVENT W1C). The shell has shipped one destructive read
  // already (a read of DFXCTL 0x20 popped a UART console byte through a decode
  // alias, dfx_ctl.sv:229-258). Do not re-introduce the shape.
  // ===========================================================================
  wire [31:0] ctrl_rdata = {15'd0,
                            1'b0,             // [16] src_sel_we   -- W1P, reads 0
                            6'd0,             // [15:10] reserved
                            1'b0,             // [9]  force_switch -- W1P, reads 0
                            1'b0,             // [8]  panel_rst_pulse -- W1P, reads 0
                            bl_rst_src_q,     // [7]
                            panel_rst_n_q,    // [6]
                            backlight_q,      // [5]
                            dut_req_en_q,     // [4]
                            pb_en_q,          // [3]
                            timeout_en_q,     // [2]
                            force_harness_q,  // [1]
                            tgt_owner_q};     // [0]  src_sel reads the REQUESTED owner

  wire [31:0] status_rdata = {13'd0,
                              state_q,          // [18:16]
                              interlock,        // [15]
                              rp_in_reset,      // [14]
                              decoupled,        // [13]
                              pb_raw,           // [12]
                              pb_level_q,       // [11]
                              dut_req_f,        // [10]
                              dut_quiet,        // [9]
                              harness_quiet,    // [8]
                              kvm_drives,       // [7]
                              (state_q == S_DRAIN),   // [6] draining
                              (state_q == S_GRANT),   // [5] granting
                              (state_q == S_SETTLE),  // [4] panel_settling
                              ~clcd_rst_n_o,          // [3] panel_rst_active
                              tgt_owner_q,      // [2]
                              switch_pending,   // [1]
                              owner_q};         // [0]

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      case (raddr_idx)
        IDX_CTRL:      axi_rdata_q <= ctrl_rdata;
        IDX_STATUS:    axi_rdata_q <= status_rdata;
        IDX_EVENT:     axi_rdata_q <= {24'd0, event_q};
        IDX_PANEL_TMR: axi_rdata_q <= {settle_us_q, rst_us_q};
        IDX_TIMEOUT:   axi_rdata_q <= timeout_us_q;
        IDX_DEBOUNCE:  axi_rdata_q <= {16'd0, debounce_us_q};
        IDX_TUNNEL:    axi_rdata_q <= tun_f_q;   // {gpio_oe, gpio_o}, filtered
        default:       axi_rdata_q <= '0;        // unmapped
      endcase
    end
  end

  // ===========================================================================
  // Unused-bit sink for the -Wall self-check (narrow), the clcd.sv:543-551
  // idiom: the AxPROT qualifiers, the wdata byte lanes above the widest CTRL
  // field, and the address bits outside the decoded page (the sub-word [1:0]
  // plus, at the BD's width 32, everything above LOCAL_ADDR_W -- the whole
  // registers are AND-reduced here, and the decoded bits are already read by
  // waddr_idx/raddr_idx, so this is a harmless second reader that stays
  // width-agnostic). Sinking them keeps the check clean without a module-wide
  // waiver that could hide a real unused signal.
  // ===========================================================================
  // verilator lint_off UNUSED
  wire _unused_ok = &{1'b0,
                      s_axi_awprot, s_axi_arprot,
                      s_axi_wdata[C_S_AXI_DATA_WIDTH-1:17],
                      s_axi_wdata[15:10],
                      axi_awaddr_q, axi_araddr_q,
                      1'b0};
  // verilator lint_on UNUSED

endmodule
